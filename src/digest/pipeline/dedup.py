from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import structlog
from rapidfuzz.fuzz import token_set_ratio

from digest.config import Config
from digest.models import Event, normalize_title, strip_venue_suffix, venue_matches

log = structlog.get_logger()

_TITLE_MERGE_RATIO = 88
_TITLE_AMBIGUOUS_RATIO = 80
_MAX_START_GAP = timedelta(minutes=90)

# A source the config does not describe must never win the merge base against one it does.
_UNKNOWN_SOURCE_PRIORITY = 1000


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def fuzzy_title(event: Event) -> str:
    """The only place `strip_venue_suffix` is allowed to run (§4.1): the id and the ledger
    stay on the conservative `normalize_title`, this comparison may go one step further."""
    return normalize_title(strip_venue_suffix(event.title, event.venue_name))


def dedup(events: list[Event], config: Config) -> list[Event]:
    merged = _merge_by_key(events, config, lambda event: [event.id], "exact")
    merged = _merge_by_key(merged, config, _url_keys, "strong")
    return _merge_fuzzy(merged, config)


def _url_keys(event: Event) -> list[str]:
    return [normalize_url(url) for url in event.urls]


def _merge_by_key(
    events: list[Event],
    config: Config,
    key_of: Callable[[Event], list[str]],
    reason: str,
) -> list[Event]:
    result: list[Event] = []
    positions: dict[str, int] = {}
    for event in events:
        target = next((positions[key] for key in key_of(event) if key in positions), None)
        if target is None:
            result.append(event)
            target = len(result) - 1
        else:
            _log_merge(result[target], event, reason, 100.0)
            result[target] = _merge(result[target], event, config)
        for key in key_of(result[target]):
            positions[key] = target
    return result


def _merge_fuzzy(events: list[Event], config: Config) -> list[Event]:
    result: list[Event] = []
    for event in events:
        target = None
        for index, existing in enumerate(result):
            score = _fuzzy_score(existing, event)
            if score is None:
                continue
            if score >= _TITLE_MERGE_RATIO:
                target = index
                _log_merge(existing, event, "fuzzy", score)
                break
            if score >= _TITLE_AMBIGUOUS_RATIO:
                # Close enough to be worth a second look, not close enough to act on.
                # This is the input the optional LLM hook consumes later (§7.2).
                log.info(
                    "ambiguous_dedup",
                    source_a=",".join(existing.source_ids),
                    source_b=",".join(event.source_ids),
                    score=round(score, 1),
                    reason="fuzzy_title_band",
                    title_a=existing.title,
                    title_b=event.title,
                )
        if target is None:
            result.append(event)
        else:
            result[target] = _merge(result[target], event, config)
    return result


def _fuzzy_score(a: Event, b: Event) -> float | None:
    """None when the pair fails a non-title condition — all three are mandatory (§7.2)."""
    if not _starts_match(a, b):
        return None
    if not _venues_match(a, b):
        return None
    return token_set_ratio(fuzzy_title(a), fuzzy_title(b))


def _starts_match(a: Event, b: Event) -> bool:
    """90 minutes apart when both clocks are real, the same calendar day when either one
    is not.

    A source that publishes a bare date lands on 00:00, so under the 90-minute rule it
    could only ever match something starting between 00:00 and 01:30 — which is to say it
    was structurally unable to deduplicate against an evening listing of the same event.
    That was permanent, silent blindness, not a tuning problem: no gap setting fixes a
    comparison between a real time and a missing one. Comparing the day is the most the
    coarser record supports, and it is exactly what that record actually asserts.

    `start.date()` and not `effective_date`: the latter has already had the night shift
    applied to whichever side had a real clock, so an 00:30 event would be compared on the
    previous day and never match the date-only record naming its actual day."""
    if a.start_time_known and b.start_time_known:
        return abs(a.start - b.start) <= _MAX_START_GAP
    return a.start.date() == b.start.date()


def _venues_match(a: Event, b: Event) -> bool:
    """A missing venue does not block a merge — that is this stage's own rule, and the only
    part not shared with §7.5. The comparison itself is `venue_matches`."""
    if a.venue_name is None or b.venue_name is None:
        return True
    return venue_matches(a.venue_name, b.venue_name)


def _log_merge(a: Event, b: Event, reason: str, score: float) -> None:
    log.info(
        "dedup_merge",
        source_a=",".join(a.source_ids),
        source_b=",".join(b.source_ids),
        score=round(score, 1),
        reason=reason,
        title=a.title,
    )


# THE MERGE INVARIANT (§7.2): a merge must never reduce information. If the base holds
# None and the other record holds a value, the value wins. That is the DEFAULT for every
# scalar field on Event, computed from the model below rather than written out field by
# field — so a field added later is covered without anyone remembering to add a rule.
#
# It used to be the opposite: each field that needed filling got its own `if base.x is
# None` line, and a field with no line silently kept the base's None. That was harmless
# while every field was cosmetic or scoring-only, and stopped being harmless the moment
# `city` arrived, because §7.6 can EXCLUDE on city — a city-less base (port-hu) would
# overwrite a source that knew the settlement (cooltix) and drop an event both agreed on.

# Collections union rather than fill: two sources describing one event each contribute.
_UNION_FIELDS = ("source_ids", "urls", "categories", "native_categories")

# The longer description wins even when the base already has one — the invariant does not
# weaken this, it is a stronger rule of the same kind.
_LONGEST_WINS_FIELDS = ("description",)

# Filled as a unit, keyed on the first field. Per-field filling would let a half-filled
# group through: `is_free=True` from a free base standing next to a `price_max` scraped
# from the other record, or a `distance_km` that was computed from different coordinates
# than the `lat`/`lon` beside it.
_COUPLED_FIELD_GROUPS = (
    ("price_min", "price_max", "is_free"),
    ("lat", "lon", "distance_km"),
)

# The deliberate exceptions, and why each one is not fill-if-missing.
_BASE_ALWAYS_WINS = {
    # Identity. §4.1 derives it from title/date/venue and the ledger keys on it; taking
    # the other record's id would make the merged event a different event.
    "id": "identity, not information",
    "title": "the display string, and the id is derived from it",
    # start and effective_date are the base's reading of when this happens. Swapping
    # either alone contradicts the other, and effective_date is derived from start in §7.1.
    "start": "paired with effective_date and start_time_known",
    "effective_date": "derived from start (§7.1), never merged independently",
    # A base that does not know its clock must not be handed a True from a record whose
    # `start` it is not taking: that would claim a real time for a midnight placeholder.
    # Promoting it means promoting start and effective_date together — see the note in
    # _merge's caller about when that becomes reachable.
    "start_time_known": "only meaningful together with start",
    # Written by §7.4, which runs after this stage; always None here.
    "group_key": "owned by the group stage",
    "group_size": "owned by the group stage",
    # Written by §7.7, which also runs later.
    "score": "owned by the score stage",
    "score_breakdown": "owned by the score stage",
}

_SPECIAL_FIELDS = (
    set(_UNION_FIELDS)
    | set(_LONGEST_WINS_FIELDS)
    | {field for group in _COUPLED_FIELD_GROUPS for field in group}
    | set(_BASE_ALWAYS_WINS)
)
# Everything else, by default. Derived from the model so that adding a field to Event puts
# it here automatically; tests/test_dedup.py asserts this set stays exhaustive.
FILL_IF_MISSING_FIELDS = tuple(name for name in Event.model_fields if name not in _SPECIAL_FIELDS)


def _merge(first: Event, second: Event, config: Config) -> Event:
    base, other = _order_by_priority(first, second, config)
    update: dict[str, object] = {
        field: _union(getattr(base, field), getattr(other, field)) for field in _UNION_FIELDS
    }

    for field in _LONGEST_WINS_FIELDS:
        mine, theirs = getattr(base, field), getattr(other, field)
        if len(theirs or "") > len(mine or ""):
            update[field] = theirs

    for group in _COUPLED_FIELD_GROUPS:
        lead = group[0]
        if getattr(base, lead) is None and getattr(other, lead) is not None:
            update.update({field: getattr(other, field) for field in group})

    for field in FILL_IF_MISSING_FIELDS:
        theirs = getattr(other, field)
        if getattr(base, field) is None and theirs is not None:
            update[field] = theirs

    return base.model_copy(update=update)


def _order_by_priority(first: Event, second: Event, config: Config) -> tuple[Event, Event]:
    if _priority(second, config) < _priority(first, config):
        return second, first
    return first, second


def _priority(event: Event, config: Config) -> int:
    return min(
        (_source_priority(source_id, config) for source_id in event.source_ids),
        default=_UNKNOWN_SOURCE_PRIORITY,
    )


def _source_priority(source_id: str, config: Config) -> int:
    value = (config.sources.get(source_id) or {}).get("priority")
    return value if isinstance(value, int) else _UNKNOWN_SOURCE_PRIORITY


def _union(first: Iterable[str], second: Iterable[str]) -> list[str]:
    merged = list(first)
    merged.extend(item for item in second if item not in merged)
    return merged
