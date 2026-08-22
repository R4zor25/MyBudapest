from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from digest.config import CategoryRules, Config, LLMConfig
from digest.llm.gemini import GeminiCategorizer, content_hash
from digest.models import Event, make_event_id

BUDAPEST = ZoneInfo("Europe/Budapest")
START = datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST)

_LONG_DESCRIPTION = "Egy hosszú, részletes leírás következik. " * 6  # well over 200 chars
assert len(_LONG_DESCRIPTION) > 200
_SHORT_DESCRIPTION = "Rövid."


def make_config(**llm_overrides: Any) -> Config:
    return Config(
        categories={"koncert": CategoryRules(), "kviz": CategoryRules()},
        fallback_category="egyeb",
        llm=LLMConfig(enabled=True, **llm_overrides),
    )


def make_event(index: int, **overrides: Any) -> Event:
    title = overrides.pop("title", f"Rejtélyes program #{index}")
    # Distinct per index on purpose: gives every default-built event its own content_hash,
    # so the batching tests get one real API call per event instead of accidental dedup.
    # test_two_events_with_identical_text_... passes an explicit shared description instead.
    description = overrides.pop("description", f"{_LONG_DESCRIPTION} ({index})")
    start = overrides.pop("start", START)
    categories = overrides.pop("categories", ["egyeb"])
    base: dict[str, Any] = {
        "id": make_event_id(title, start, None),
        "source_ids": ["port-hu"],
        "urls": [f"https://port.hu/esemeny/{index}"],
        "title": title,
        "description": description,
        "start": start,
        "end": None,
        "effective_date": start.date(),
        "venue_name": None,
        "district": None,
        "lat": None,
        "lon": None,
        "distance_km": None,
        "price_min": None,
        "price_max": None,
        "categories": categories,
        "image_url": None,
        "score": 0.0,
    }
    return Event(**{**base, **overrides})


class FakeClient:
    """Records every prompt it was asked to answer and returns queued responses in
    order — a plain stand-in, no google-genai import needed anywhere in this file."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def generate(self, *, model: str, prompt: str) -> str:
        self.calls.append(prompt)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response_for(events: list[Event], category: str) -> str:
    return json.dumps({event.id: category for event in events})


def test_100_eligible_events_with_batch_size_35_makes_exactly_3_calls() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    events = [make_event(i) for i in range(100)]
    batches = [events[0:35], events[35:70], events[70:100]]
    client = FakeClient([_response_for(b, "koncert") for b in batches])

    result = GeminiCategorizer(client=client).categorize(events, config)

    assert len(client.calls) == 3
    assert all(event.categories == ["koncert"] for event in result)


def test_a_429_on_the_first_call_preserves_rule_based_results_and_does_not_raise() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    events = [make_event(i) for i in range(100)]
    client = FakeClient([RuntimeError("429 Too Many Requests")])

    result = GeminiCategorizer(client=client).categorize(events, config)

    assert len(client.calls) == 1
    assert all(event.categories == ["egyeb"] for event in result)


def test_max_calls_per_run_2_with_100_events_makes_exactly_2_calls_rest_unchanged() -> None:
    config = make_config(batch_size=35, max_calls_per_run=2)
    events = [make_event(i) for i in range(100)]
    batches = [events[0:35], events[35:70]]
    client = FakeClient([_response_for(b, "koncert") for b in batches])

    result = GeminiCategorizer(client=client).categorize(events, config)

    assert len(client.calls) == 2
    # Not asserted by position: categorize() promises batches follow input order
    # (see the comment above `to_call` in gemini.py), but the count is the actual
    # requirement — "the rest unchanged" — and doesn't depend on that internal detail.
    recategorized = sum(1 for event in result if event.categories == ["koncert"])
    untouched = sum(1 for event in result if event.categories == ["egyeb"])
    assert recategorized == 70
    assert untouched == 30


def test_a_response_wrapped_in_json_fences_still_parses() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    events = [make_event(0)]
    fenced = "```json\n" + _response_for(events, "kviz") + "\n```"
    client = FakeClient([fenced])

    result = GeminiCategorizer(client=client).categorize(events, config)

    assert result[0].categories == ["kviz"]


def test_a_category_outside_the_taxonomy_leaves_that_event_unchanged() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    events = [make_event(0), make_event(1)]
    response = json.dumps({events[0].id: "koncert", events[1].id: "sci-fi-slam"})
    client = FakeClient([response])

    result = GeminiCategorizer(client=client).categorize(events, config)

    assert result[0].categories == ["koncert"]
    assert result[1].categories == ["egyeb"]


def test_a_cached_content_hash_is_not_sent_again() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    cached_event = make_event(0, title="Ismételt program")
    new_event = make_event(1, title="Új program")
    cache = {content_hash(cached_event): "koncert"}
    client = FakeClient([_response_for([new_event], "kviz")])

    result = GeminiCategorizer(client=client, cache=cache).categorize(
        [cached_event, new_event], config
    )

    assert len(client.calls) == 1
    prompt = client.calls[0]
    assert cached_event.id not in prompt
    assert new_event.id in prompt
    assert result[0].categories == ["koncert"]
    assert result[1].categories == ["kviz"]


def test_two_events_with_identical_text_are_sent_only_once_in_the_same_run() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    twin_a = make_event(0, title="Ugyanaz a cím", description=_LONG_DESCRIPTION)
    twin_b = make_event(1, title="Ugyanaz a cím", description=_LONG_DESCRIPTION)
    assert content_hash(twin_a) == content_hash(twin_b)
    client = FakeClient([_response_for([twin_a], "koncert")])

    result = GeminiCategorizer(client=client).categorize([twin_a, twin_b], config)

    assert len(client.calls) == 1
    assert client.calls[0].count(twin_a.id) == 1
    assert all(event.categories == ["koncert"] for event in result)


# --------------------------------------------------------------------------------------
# Credentials: a missing or rejected key degrades, it does not fail (CLAUDE.md 4)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        # What the google-genai SDK raises when no key is given and none is in the env.
        ValueError("Missing key inputs argument! To use the Google AI API, provide..."),
        # And what `from google import genai` raises when the optional extra is absent —
        # the same guard, because both happen while BUILDING the client, not while calling.
        ModuleNotFoundError("No module named 'google'"),
    ],
    ids=["missing-api-key", "extra-not-installed"],
)
def test_a_client_that_cannot_be_built_leaves_the_rule_based_categories(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """`on_quota_error: fallback_to_rules` covers a 429, which arrives from `generate()`.
    A missing GEMINI_API_KEY never gets that far — it fails while constructing the client,
    which used to be outside the guard. categorize is a pipeline stage, so the per-source
    try/except in the run loop does not stand between this and a dead run."""

    def explode() -> None:
        raise failure

    monkeypatch.setattr("digest.llm.gemini._RealGeminiClient", explode)
    config = make_config(batch_size=35, max_calls_per_run=12)
    events = [make_event(0), make_event(1)]

    with capture_logs() as logs:
        result = GeminiCategorizer().categorize(events, config)

    assert [event.categories for event in result] == [["egyeb"], ["egyeb"]]
    (entry,) = [line for line in logs if line["event"] == "llm_client_unavailable"]
    assert str(failure) in entry["error"]


def test_a_rejected_api_key_leaves_the_rule_based_categories() -> None:
    """The other half: the key exists, the client builds, and the API refuses it. This one
    already degraded — the failure arrives from `generate()`, inside the guard — and it is
    pinned so it stays that way."""

    class Rejecting:
        def generate(self, *, model: str, prompt: str) -> str:
            raise RuntimeError("401 UNAUTHENTICATED: API key not valid")

    config = make_config(batch_size=35, max_calls_per_run=12)
    events = [make_event(0), make_event(1)]

    with capture_logs() as logs:
        result = GeminiCategorizer(client=Rejecting()).categorize(events, config)

    assert [event.categories for event in result] == [["egyeb"], ["egyeb"]]
    assert [line for line in logs if line["event"] == "llm_call_failed"]


def test_disabled_llm_never_calls_the_client() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    config = config.model_copy(update={"llm": config.llm.model_copy(update={"enabled": False})})
    events = [make_event(0)]
    client = FakeClient([])

    result = GeminiCategorizer(client=client).categorize(events, config)

    assert len(client.calls) == 0
    assert result[0].categories == ["egyeb"]


@pytest.mark.parametrize(
    ("categories", "description"),
    [
        (["koncert"], _LONG_DESCRIPTION),  # not the fallback category
        (["egyeb"], _SHORT_DESCRIPTION),  # description too short
    ],
)
def test_ineligible_events_never_reach_the_client(categories: list[str], description: str) -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    event = make_event(0, categories=categories, description=description)
    client = FakeClient([])

    result = GeminiCategorizer(client=client).categorize([event], config)

    assert len(client.calls) == 0
    assert result[0].categories == categories


def test_a_partial_batch_response_leaves_missing_events_on_their_rule_based_category() -> None:
    config = make_config(batch_size=35, max_calls_per_run=12)
    events = [make_event(0), make_event(1)]
    partial = json.dumps({events[0].id: "koncert"})  # events[1] missing from the response
    client = FakeClient([partial])

    result = GeminiCategorizer(client=client).categorize(events, config)

    assert result[0].categories == ["koncert"]
    assert result[1].categories == ["egyeb"]
