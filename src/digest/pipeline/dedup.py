from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import structlog
from rapidfuzz.fuzz import token_set_ratio

from digest.config import Config
from digest.models import Event, normalize_title, normalize_venue, strip_venue_suffix

log = structlog.get_logger()

_TITLE_MERGE_RATIO = 88
_TITLE_AMBIGUOUS_RATIO = 80
_VENUE_RATIO = 85
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
    if abs(a.start - b.start) > _MAX_START_GAP:
        return None
    if not _venues_match(a, b):
        return None
    return token_set_ratio(fuzzy_title(a), fuzzy_title(b))


def _venues_match(a: Event, b: Event) -> bool:
    if a.venue_name is None or b.venue_name is None:
        return True
    ratio = token_set_ratio(normalize_venue(a.venue_name), normalize_venue(b.venue_name))
    return ratio >= _VENUE_RATIO


def _log_merge(a: Event, b: Event, reason: str, score: float) -> None:
    log.info(
        "dedup_merge",
        source_a=",".join(a.source_ids),
        source_b=",".join(b.source_ids),
        score=round(score, 1),
        reason=reason,
        title=a.title,
    )


def _merge(first: Event, second: Event, config: Config) -> Event:
    base, other = _order_by_priority(first, second, config)
    update: dict[str, object] = {
        "source_ids": _union(base.source_ids, other.source_ids),
        "urls": _union(base.urls, other.urls),
        "categories": _union(base.categories, other.categories),
        "native_categories": _union(base.native_categories, other.native_categories),
    }
    if len(other.description or "") > len(base.description or ""):
        update["description"] = other.description
    if base.price_min is None and other.price_min is not None:
        update["price_min"] = other.price_min
        update["price_max"] = other.price_max
        update["is_free"] = other.is_free
    if base.lat is None and other.lat is not None:
        # Coordinates travel with the distance derived from them, or the two disagree.
        update["lat"] = other.lat
        update["lon"] = other.lon
        update["distance_km"] = other.distance_km
    if base.image_url is None and other.image_url is not None:
        update["image_url"] = other.image_url
    if base.district is None and other.district is not None:
        update["district"] = other.district
    # Same fill-if-missing rule as district, and load-bearing in a way district never was:
    # §7.6 can EXCLUDE on city, so letting a city-less base overwrite a source that does
    # know the settlement would drop an event both sources agree is in town.
    if base.city is None and other.city is not None:
        update["city"] = other.city
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
