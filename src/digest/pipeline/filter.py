from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from digest.config import Config
from digest.models import Event, contains_word, fold_text

log = structlog.get_logger()

# The three geographic exclusions, named so cli.py can total them for the run summary
# without re-deriving the rule. Note what the total means: `_exclusion_reason` returns the
# FIRST match, so an event that is both out of city and past the horizon is counted as
# `beyond_horizon`. These are "excluded because of geography", not a census of how many
# events were out of area.
GEO_REASONS = frozenset({"geo_city_mismatch", "geo_city_missing", "geo_too_far"})


@dataclass(frozen=True)
class _Exclusion:
    reason: str
    # What made the call — logged alongside the reason so an over-aggressive filter can be
    # diagnosed from the run log alone (§7.6): the observed value and the threshold.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FilterOutcome:
    events: list[Event]
    excluded: Counter[str]


def filter_with_reasons(
    events: list[Event],
    config: Config,
    sent_ids: frozenset[str] = frozenset(),
    hidden_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> FilterOutcome:
    """`filter()` plus the per-reason tally the run summary needs. Split out rather than
    changing `filter()`'s return type, because CLAUDE.md fixes every pipeline stage at
    `(list[Event], Config) -> list[Event]`."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    horizon = moment + timedelta(days=config.schedule.horizon_days)

    survivors: list[Event] = []
    excluded: Counter[str] = Counter()
    for event in events:
        exclusion = _exclusion_reason(event, config, sent_ids, hidden_ids, horizon)
        if exclusion is None:
            survivors.append(event)
            continue
        excluded[exclusion.reason] += 1
        log.info(
            "filtered",
            reason=exclusion.reason,
            event_id=event.id,
            title=event.title,
            **exclusion.detail,
        )
    return FilterOutcome(events=survivors, excluded=excluded)


def filter(
    events: list[Event],
    config: Config,
    sent_ids: frozenset[str] = frozenset(),
    hidden_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> list[Event]:
    """Five of SPEC 7.6's six exclusion reasons. The sixth, min_score, needs Event.score,
    which does not exist yet here — filter runs before score in the pipeline (SPEC 7.4,
    CLAUDE.md). score() applies that cut once it has something to compare against.

    `sent_ids` defaults to "nothing sent yet": the ledger (state.py, package 8) does not
    exist yet either. Once it does, its `was_sent()` result is what a caller passes in.

    `hidden_ids` is the write UI's overrides.yaml (package 14) — a user explicitly asked
    never to see this event again. Checked first: nothing else about the event matters
    once it is hidden."""
    return filter_with_reasons(events, config, sent_ids, hidden_ids, now).events


def _exclusion_reason(
    event: Event,
    config: Config,
    sent_ids: frozenset[str],
    hidden_ids: frozenset[str],
    horizon: datetime,
) -> _Exclusion | None:
    if event.id in hidden_ids:
        return _Exclusion("hidden_by_override")

    if event.start > horizon:
        return _Exclusion("beyond_horizon")

    # Before the taste-based cuts: geography is a hard fact about the event, cheap to
    # check, and the same rule for every source (§7.6). Sources that can filter by city
    # themselves still should — not fetching what you would discard is politeness, not
    # duplication — but this stage is the authoritative rule, not a fallback.
    geo = _geo_exclusion(event, config)
    if geo is not None:
        return geo

    if config.filters.categories is not None:
        primary = event.categories[0] if event.categories else None
        if primary not in config.filters.categories:
            return _Exclusion("category_not_allowed")

    if (
        config.filters.max_price_huf is not None
        and event.price_min is not None
        and event.price_min > config.filters.max_price_huf
    ):
        return _Exclusion("price_too_high")

    text = f"{event.title} {event.description or ''}"
    if any(contains_word(text, keyword) for keyword in config.filters.blocked_keywords):
        return _Exclusion("blocked_keyword")

    if event.id in sent_ids:
        return _Exclusion("already_sent")

    return None


def _geo_exclusion(event: Event, config: Config) -> _Exclusion | None:
    """Two independent cuts, both failing open when the fact they need is absent.

    The city test only runs when `filters.geo.city` is configured — an unset city means
    "anywhere", so a default profile excludes nothing. An event whose own city is unknown
    is kept unless `allow_missing_city` is switched off, because most sources publish no
    settlement and dropping them would quietly lose good events (requirement 2).

    The distance test is a HARD exclusion, unrelated to `scoring.proximity`, which only
    scales a penalty. It needs `home` (profile) and source coordinates to produce a
    `distance_km` at all; when either is missing the event is kept, for the same
    fail-open reason."""
    geo = config.filters.geo

    if geo.city is not None:
        if event.city is None:
            if not geo.allow_missing_city:
                return _Exclusion("geo_city_missing", {"city": None, "expected": geo.city})
        elif fold_text(event.city).strip() != fold_text(geo.city).strip():
            return _Exclusion("geo_city_mismatch", {"city": event.city, "expected": geo.city})

    if (
        geo.max_distance_km is not None
        and event.distance_km is not None
        and event.distance_km > geo.max_distance_km
    ):
        return _Exclusion(
            "geo_too_far",
            {"distance_km": event.distance_km, "max_distance_km": geo.max_distance_km},
        )

    return None
