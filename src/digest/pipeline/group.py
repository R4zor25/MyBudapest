from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

import structlog

from digest.config import Config
from digest.models import Event, normalize_venue

log = structlog.get_logger()

# The titles making up the collapsed row's description (§7.4).
_DESCRIPTION_MEMBERS = 3

_GroupKey = tuple[str, date, str | None]


@dataclass(frozen=True)
class GroupOutcome:
    events: list[Event]
    # How many events skipped grouping for having no venue. Reported so that a source
    # which stops supplying venue names shows up in the run summary instead of quietly
    # changing the shape of the digest.
    ungrouped_venueless: int


def group_with_counts(events: list[Event], config: Config) -> GroupOutcome:
    """`group()` plus the venue-less tally the run summary needs. Split out rather than
    changing `group()`'s return type, because CLAUDE.md fixes every pipeline stage at
    `(list[Event], Config) -> list[Event]`."""
    grouping = config.grouping
    buckets: dict[_GroupKey, list[Event]] = {}
    # One slot per output position: either a passthrough Event or a group key, in the
    # order they were first seen — so excluding the venue-less ones does not reshuffle
    # everything else.
    slots: list[Event | _GroupKey] = []
    venueless: list[Event] = []

    for event in events:
        if event.venue_name is None:
            # §7.4 exists to collapse ONE festival at ONE venue. Keying venue-less events
            # together produces "every venueless event of category X on day Y", which is
            # not a venue group: those events have nothing to do with each other, and
            # collapsing them would hide real, distinct events behind a summary row whose
            # title reads "None — 4 program". They pass through individually instead.
            venueless.append(event)
            slots.append(event)
            continue
        key = (event.venue_name, event.effective_date, _primary_category(event))
        if key not in buckets:
            buckets[key] = []
            slots.append(key)
        buckets[key].append(event)

    if venueless:
        log.info(
            "grouping_skipped_venueless",
            count=len(venueless),
            sources=sorted({source for event in venueless for source in event.source_ids}),
        )

    result: list[Event] = []
    for slot in slots:
        if isinstance(slot, Event):
            # max_per_venue does not apply either: there is no venue to cap.
            result.append(slot)
            continue
        key = slot
        members = buckets[key]
        if len(members) >= grouping.min_group_size:
            result.append(_collapse(key, members))
        else:
            result.extend(_cap(key, members, grouping.max_per_venue))
    return GroupOutcome(events=result, ungrouped_venueless=len(venueless))


def group(events: list[Event], config: Config) -> list[Event]:
    """Runs after score() — a collapsed row's score is the max of its members', so nothing
    upstream of score can compute it (§7.4). Wire this stage accordingly."""
    return group_with_counts(events, config).events


def _primary_category(event: Event) -> str | None:
    return event.categories[0] if event.categories else None


def _cap(key: _GroupKey, members: list[Event], max_per_venue: int) -> list[Event]:
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


def _collapse(key: _GroupKey, members: list[Event]) -> Event:
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


def _group_id(venue_name: str, effective_date: date, primary_category: str | None) -> str:
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
