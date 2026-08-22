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


def content_filter_with_reasons(
    events: list[Event],
    config: Config,
    hidden_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> FilterOutcome:
    """`content_filter()` plus the per-reason tally the run summary needs. Split out rather
    than changing its return type, because CLAUDE.md fixes every pipeline stage at
    `(list[Event], Config) -> list[Event]`."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    horizon = moment + timedelta(days=config.schedule.horizon_days)

    survivors: list[Event] = []
    excluded: Counter[str] = Counter()
    for event in events:
        exclusion = _exclusion_reason(event, config, hidden_ids, horizon)
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


def content_filter(
    events: list[Event],
    config: Config,
    hidden_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> list[Event]:
    """"Does this event interest the reader" — and nothing else (§7.6).

    Every cut here is a property of the EVENT and the reader's standing preferences, so it
    answers the same on every run: horizon, geography, category, price, blocked keywords,
    and the write UI's explicit hides. The same input gives the same output tomorrow, which
    is what lets the published site be a stable full view rather than a shrinking one.

    What is deliberately NOT here: the sent-ledger. "Have I already emailed about this" is
    a property of the reader's HISTORY, not of the event, and it belongs only to the email
    branch — see `exclude_already_sent`. It used to sit in this chain, and because it did,
    every run removed from the SITE whatever the last email had covered, and a skipped day
    lost those events from the site permanently.

    `min_score` is the one §7.6 reason that is not here either, for an unrelated reason: it
    needs `Event.score`, which does not exist until score() runs after this stage.

    `hidden_ids` is overrides.yaml (package 14) — a user asked never to see this event
    again. That is a standing preference, so it is content, and it applies to both outputs.
    Checked first: nothing else about the event matters once it is hidden."""
    return content_filter_with_reasons(events, config, hidden_ids, now).events


def exclude_already_sent(events: list[Event], sent_ids: frozenset[str]) -> list[Event]:
    """"Was this in a previous email" — the EMAIL branch only (§7.6, §8.2).

    Kept apart from `content_filter` on purpose, and not as a flag on it: the two answer
    different questions about different subjects. This one reads the reader's history, so
    its answer changes between runs by design; the content filters must not. Applying it to
    the web output is what made the site lose events it had already shown.

    No `Config`, no clock, no logging of its own — the caller reports the count, because
    "suppressed from today's email" is a fact about one output, not a pipeline-wide
    exclusion like the reasons in `FilterOutcome`."""
    return [event for event in events if event.id not in sent_ids]


def _exclusion_reason(
    event: Event,
    config: Config,
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
        # ANY of the event's categories, not just the primary one. The allow-list is a
        # statement about interests, and an event that carries an allowed interest at all
        # is of interest — a concert that happens to be classified `egyeb` first is still a
        # concert. Reading `categories[0]` alone dropped exactly those.
        #
        # Only the INCLUSION test widens. The primary category still decides the section
        # and everything §7.5 and §7.7 do with it, so an event kept by its secondary
        # category still sections under its primary.
        allowed = set(config.filters.categories)
        if not any(category in allowed for category in event.categories):
            return _Exclusion("category_not_allowed")

    if (
        config.filters.max_price_huf is not None
        and event.price_min is not None
        and event.price_min > config.filters.max_price_huf
    ):
        return _Exclusion("price_too_high")

    text = f"{event.title} {event.description or ''}"
    # WHOLE WORDS by default here, unlike every other caller of contains_word. The shared
    # matcher defaults to prefix matching because Hungarian agglutinates, and for
    # categorization that is right: a false positive mislabels an event, which shows up in
    # the digest and can be corrected. This is the one call site where a false positive
    # DELETES the event instead — silently, with nothing in the output to notice.
    #
    # Measured on the widened rule: a "gyerek" block also excluded "Gyerekkori álmom volt
    # ez a koncert" and "Gyerekzsivaj nélküli felnőtt est" — the second an adults-only
    # event, the exact opposite of what the block asked for. It is also the same principle
    # the geographic cut is built on (§7.6): where the system is unsure, it KEEPS the
    # event. A block that fires too narrowly leaves something visible in the digest; one
    # that fires too widely leaves nothing at all.
    #
    # Prefix matching is still available, just asked for rather than assumed: "gyerek*".
    # The safe behaviour is the default and the aggressive one is opt-in, not the reverse.
    if any(
        contains_word(text, keyword, prefix_by_default=False)
        for keyword in config.filters.blocked_keywords
    ):
        return _Exclusion("blocked_keyword")

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
