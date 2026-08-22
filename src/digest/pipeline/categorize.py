from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from digest.config import CategoryRules, Config
from digest.models import Event, contains_word, venue_matches

log = structlog.get_logger()

# A native_types match is the strongest signal (§7.5): the source itself said what this
# is, which is more trustworthy than any keyword we guessed.
_NATIVE_TYPE_SCORE = 4.0

# Not specified numerically in §7.5 — a URL segment like "/koncert/" is a real signal but
# a looser one than a native type (the segment can be a site-wide section, not a per-event
# fact), so it sits below native_types and around a strong keyword.
_URL_PATTERN_SCORE = 3.0


@dataclass(frozen=True)
class CategoryScore:
    total: float
    signals: dict[str, float] = field(default_factory=dict)


def score_category(event: Event, rules: CategoryRules) -> CategoryScore:
    signals: dict[str, float] = {}
    text = f"{event.title} {event.description or ''}"

    for keyword, weight in rules.keywords.items():
        if contains_word(text, keyword):
            signals[f"keyword:{keyword}"] = weight

    if event.venue_name is not None and rules.venue_prior:
        for venue, weight in rules.venue_prior.items():
            if venue_matches(event.venue_name, venue):
                # The entry that fired is named, so `digest categorize --explain` shows
                # which assertion about the world produced the points.
                signals[f"venue_prior:{venue}"] = weight
                break

    if rules.url_patterns and any(
        pattern in url for pattern in rules.url_patterns for url in event.urls
    ):
        signals["url_pattern"] = _URL_PATTERN_SCORE

    if rules.native_types and set(event.native_categories) & set(rules.native_types):
        signals["native_type"] = _NATIVE_TYPE_SCORE

    return CategoryScore(total=sum(signals.values()), signals=signals)


def explain_event(event: Event, config: Config) -> dict[str, CategoryScore]:
    """One score per configured category, in config order — this is what
    `digest categorize --explain` renders, and what `_categorize_one` ranks."""
    return {name: score_category(event, rules) for name, rules in config.categories.items()}


def _categorize_one(event: Event, config: Config) -> Event:
    scores = explain_event(event, config)
    # dict.items() preserves config.yaml's category order, so a tie keeps whichever
    # category was declared first — a stable sort makes that deterministic.
    ranked = sorted(scores.items(), key=lambda item: item[1].total, reverse=True)
    qualifying = [name for name, score in ranked if score.total >= config.min_category_score]
    categories = qualifying or [config.fallback_category]
    return event.model_copy(update={"categories": categories})


def categorize(events: list[Event], config: Config) -> list[Event]:
    result = [_categorize_one(event, config) for event in events]
    _log_unmatched_venue_priors(events, config)
    return result


def _log_unmatched_venue_priors(events: list[Event], config: Config) -> None:
    """A venue_prior entry is an assertion about the world — "this venue exists and events
    there are board games" — and assertions rot: venues close, rename, or stop being
    carried by any source. Nothing else notices, because the failure mode is a bonus that
    quietly never fires. Once per run, per category, the entries that matched nothing at
    all are named.

    Reported over the whole corpus rather than per event, because matching nothing on ONE
    event is the normal case; matching nothing on ALL of them is the signal."""
    venues = [event.venue_name for event in events if event.venue_name]
    for category, rules in config.categories.items():
        if not rules.venue_prior:
            continue
        unmatched = [
            entry
            for entry in rules.venue_prior
            if not any(venue_matches(venue, entry) for venue in venues)
        ]
        if unmatched:
            log.info(
                "venue_prior_unmatched",
                category=category,
                entries=unmatched,
                venues_seen=len(venues),
            )


class RuleCategorizer:
    """The always-available implementation of the Categorizer protocol (llm/base.py).
    A future LLM-backed one only ever runs after this, never instead of it."""

    def categorize(self, events: list[Event], config: Config) -> list[Event]:
        return categorize(events, config)
