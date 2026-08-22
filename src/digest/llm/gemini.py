from __future__ import annotations

import hashlib
import json
import os
from typing import Protocol

import structlog

from digest.config import Config
from digest.models import Event

log = structlog.get_logger()

# SPEC 7.5: only a fallback-category event with a description worth reading is worth an
# LLM call — a short description gives the model nothing the rule-based keywords didn't.
_MIN_DESCRIPTION_LENGTH = 200

# Budget for what actually goes in a prompt — the full description is not needed to
# categorize, and a shorter prompt is a cheaper, faster call.
_PROMPT_DESCRIPTION_CHARS = 500


class GenerativeClient(Protocol):
    """The one call this module needs from a Gemini client — narrow on purpose, so tests
    can inject a fake without installing `google-genai` (SPEC 15's optional `llm` extra,
    not part of the base install the production workflow uses)."""

    def generate(self, *, model: str, prompt: str) -> str: ...


class _RealGeminiClient:
    """Constructed only when GeminiCategorizer actually needs to make a call and no
    client was injected — the import is deferred here, not at module scope, so merely
    importing digest.llm.gemini never requires the optional extra to be installed."""

    def __init__(self) -> None:
        from google import genai

        self._client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    def generate(self, *, model: str, prompt: str) -> str:
        response = self._client.models.generate_content(model=model, contents=prompt)
        return response.text


def content_hash(event: Event) -> str:
    """The identity a cache entry is keyed on: the exact text sent to the model. Two
    events with the same title+description are the same question asked twice."""
    basis = f"{event.title}\n{event.description or ''}"
    return hashlib.sha256(basis.encode()).hexdigest()


def _is_eligible(event: Event, config: Config) -> bool:
    return (
        event.categories == [config.fallback_category]
        and event.description is not None
        and len(event.description) > _MIN_DESCRIPTION_LENGTH
    )


def _batches(items: list[tuple[str, Event]], size: int) -> list[list[tuple[str, Event]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _build_prompt(events: list[Event], config: Config) -> str:
    allowed = list(config.categories.keys())
    lines = [
        "Categorize each Budapest event below into exactly one of these categories: "
        + ", ".join(allowed)
        + ".",
        (
            'Respond with strict JSON only: one object mapping each event\'s "id" to one '
            "category from the list above. No prose, no markdown code fences."
        ),
        "",
    ]
    for event in events:
        lines.append(
            json.dumps(
                {
                    "id": event.id,
                    "title": event.title,
                    "description": (event.description or "")[:_PROMPT_DESCRIPTION_CHARS],
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    """Requirement 6: the prompt forbids markdown fences, but the parser tolerates them
    anyway — a ```json ... ``` wrapper is stripped rather than trusted to never appear."""
    stripped = text.strip()
    if stripped.startswith("```"):
        _, _, stripped = stripped.partition("\n")
    stripped = stripped.removesuffix("```")
    return stripped.strip()


def _parse_response(text: str) -> dict[str, str]:
    try:
        data = json.loads(_strip_fences(text))
    except (json.JSONDecodeError, ValueError):
        log.warning("llm_response_unparseable")
        return {}
    if not isinstance(data, dict):
        log.warning("llm_response_not_an_object")
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


class GeminiCategorizer:
    """The optional half of the Categorizer protocol (llm/base.py). Runs only after
    RuleCategorizer, never instead of it: `events` must already carry rule-based
    `categories`, and this only reconsiders the ones rules gave up on (§7.5).

    `cache` is injected rather than owned: content_hash -> category, mutated in place as
    calls succeed. A fresh instance with no cache only avoids re-sending duplicate text
    within one run; a caller that persists this dict across process runs (state.json is
    the natural place, but wiring that is outside llm/ — package 13's own scope) gets the
    "never sent twice, across runs too" half of requirement 4 for free."""

    def __init__(
        self, client: GenerativeClient | None = None, cache: dict[str, str] | None = None
    ) -> None:
        self._client = client
        self.cache: dict[str, str] = cache if cache is not None else {}

    def categorize(self, events: list[Event], config: Config) -> list[Event]:
        if not config.llm.enabled:
            return events
        eligible = [event for event in events if _is_eligible(event, config)]
        if not eligible:
            return events

        by_hash: dict[str, list[Event]] = {}
        for event in eligible:
            by_hash.setdefault(content_hash(event), []).append(event)

        updates: dict[str, str] = {}
        # dict preserves insertion order (CPython 3.7+), and by_hash was built by walking
        # `eligible` in its own order — so batches are formed in input order, deterministically,
        # not by hash value. max_calls_per_run therefore always favors the same events first.
        to_call: list[tuple[str, Event]] = []
        for content_key, group in by_hash.items():
            cached = self.cache.get(content_key)
            if cached is not None:
                for event in group:
                    updates[event.id] = cached
            else:
                to_call.append((content_key, group[0]))

        if to_call:
            self._run_batches(to_call, by_hash, updates, config)

        if not updates:
            return events
        return [
            event.model_copy(update={"categories": [updates[event.id]]})
            if event.id in updates
            else event
            for event in events
        ]

    def _run_batches(
        self,
        to_call: list[tuple[str, Event]],
        by_hash: dict[str, list[Event]],
        updates: dict[str, str],
        config: Config,
    ) -> None:
        try:
            client = self._client or _RealGeminiClient()
        except Exception as exc:  # noqa: BLE001 — same rule as the call below (CLAUDE.md 4)
            # BUILDING the client is a second, separate way this layer can fail, and it
            # used to sit outside the guard: `on_quota_error` covers a 429, which arrives
            # from `generate()`, but a MISSING or invalid GEMINI_API_KEY fails here instead
            # — as does the optional `llm` extra not being installed. categorize is a
            # pipeline stage, so nothing above it catches this: the whole run died on a
            # missing key, which is exactly the critical path the LLM must never be on.
            log.warning("llm_client_unavailable", error=str(exc))
            return
        calls_made = 0
        for batch in _batches(to_call, config.llm.batch_size):
            if calls_made >= config.llm.max_calls_per_run:
                log.warning("llm_max_calls_reached", max_calls=config.llm.max_calls_per_run)
                return
            try:
                text = client.generate(
                    model=config.llm.model,
                    prompt=_build_prompt([event for _, event in batch], config),
                )
            except Exception as exc:  # noqa: BLE001 — never on the critical path (CLAUDE.md 4):
                # any client failure, 429 or otherwise, falls back to the rule-based result
                # for every remaining event and the run continues, per on_quota_error.
                log.warning("llm_call_failed", error=str(exc), batch_size=len(batch))
                return
            calls_made += 1
            parsed = _parse_response(text)
            for content_key, representative in batch:
                category = parsed.get(representative.id)
                if category is None or category not in config.categories:
                    continue
                self.cache[content_key] = category
                for event in by_hash[content_key]:
                    updates[event.id] = category
