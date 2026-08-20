from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date

import structlog

from digest.config import Config
from digest.models import Event, normalize_venue

log = structlog.get_logger()

# The titles making up the collapsed row's description (§7.4).
_DESCRIPTION_MEMBERS = 3


def group(events: list[Event], config: Config) -> list[Event]:
    """Runs after score() — a collapsed row's score is the max of its members', so nothing
    upstream of score can compute it (§7.4). Wire this stage accordingly."""
    grouping = config.grouping
    buckets: dict[tuple[str | None, date, str | None], list[Event]] = {}
    order: list[tuple[str | None, date, str | None]] = []
    for event in events:
        key = _group_key(event)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(event)

    result: list[Event] = []
    for key in order:
        members = buckets[key]
        if len(members) >= grouping.min_group_size:
            result.append(_collapse(key, members))
        else:
            result.extend(_cap(key, members, grouping.max_per_venue))
    return result


def _group_key(event: Event) -> tuple[str | None, date, str | None]:
    primary = event.categories[0] if event.categories else None
    return (event.venue_name, event.effective_date, primary)


def _cap(
    key: tuple[str | None, date, str | None], members: list[Event], max_per_venue: int
) -> list[Event]:
    if len(members) <= max_per_venue:
        return members
    ranked = sorted(members, key=lambda event: event.score, reverse=True)
    venue_name, effective_date, _ = key
    log.info(
        "capped_at_venue",
        venue_name=venue_name,
        effective_date=effective_date.isoformat(),
        kept=max_per_venue,
        dropped=len(members) - max_per_venue,
    )
    return ranked[:max_per_venue]


def _collapse(key: tuple[str | None, date, str | None], members: list[Event]) -> Event:
    venue_name, effective_date, primary_category = key
    ranked = sorted(members, key=lambda event: event.score, reverse=True)
    top = ranked[0]

    log.info(
        "group_collapsed",
        venue_name=venue_name,
        effective_date=effective_date.isoformat(),
        primary_category=primary_category,
        group_size=len(members),
    )

    return top.model_copy(
        update={
            "id": _group_id(venue_name, effective_date, primary_category),
            "title": f"{venue_name} — {len(members)} program",
            "description": ", ".join(event.title for event in ranked[:_DESCRIPTION_MEMBERS]),
            "urls": _group_urls(ranked),
            "source_ids": _union(event.source_ids for event in members),
            "categories": _union(event.categories for event in members),
            "native_categories": _union(event.native_categories for event in members),
            # The top scorer's own start has no particular meaning for the row as a whole;
            # rendering a collapsed row needs "doors open" (§ package 9), which is the
            # earliest start across every member, not the highest-scoring one's.
            "start": min(event.start for event in members),
            "score": top.score,
            "group_key": f"{venue_name}|{effective_date.isoformat()}|{primary_category}",
            "group_size": len(members),
        }
    )


def _group_urls(ranked_members: list[Event]) -> list[str]:
    """ "The venue's collection URL, if the source provides one" (SPEC 7.4) has no data path
    yet — no source or config field carries a per-venue collection URL today, only a
    per-event one. Until one exists, this always takes the documented fallback: the
    top-scoring member's own url."""
    top = ranked_members[0]
    return [top.urls[0]] if top.urls else []


def _group_id(venue_name: str | None, effective_date: date, primary_category: str | None) -> str:
    """A festival slot's identity is the group key itself, not a member's id or the display
    title — the display title embeds the member count, which can shift run to run as acts
    are added or dropped, and that must not make the ledger treat it as a new event."""
    basis = (
        f"group|{normalize_venue(venue_name)}|{effective_date.isoformat()}|{primary_category or ''}"
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _union(lists: Iterable[Iterable[str]]) -> list[str]:
    merged: list[str] = []
    for items in lists:
        merged.extend(item for item in items if item not in merged)
    return merged
