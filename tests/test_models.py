from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from digest.models import (
    Event,
    RawEvent,
    make_event_id,
    normalize_title,
    normalize_venue,
    strip_venue_suffix,
)

VILLON = "Befogad és kitaszít a világ - Mácsai Pál és Huzella Péter Villon-estje"


def test_make_event_id_is_deterministic(start: datetime) -> None:
    a = make_event_id("Sub Focus (UK)", start, "A38 Hajó")
    b = make_event_id("Sub Focus (UK)", start, "A38 Hajó")
    assert a == b


def test_make_event_id_is_short_hex(start: datetime) -> None:
    event_id = make_event_id("Sub Focus", start, "A38")
    assert len(event_id) == 16
    assert set(event_id) <= set("0123456789abcdef")


def test_make_event_id_differs_when_the_date_differs(start: datetime, budapest: ZoneInfo) -> None:
    other_day = datetime(2026, 8, 15, 19, 0, tzinfo=budapest)
    assert make_event_id("Sub Focus", start, "A38") != make_event_id("Sub Focus", other_day, "A38")


def test_make_event_id_differs_by_title_and_venue(start: datetime) -> None:
    base = make_event_id("Sub Focus", start, "A38")
    assert base != make_event_id("Sub Focus", start, "Akvárium Klub")
    assert base != make_event_id("Chase & Status", start, "A38")


def test_make_event_id_ignores_time_of_day(start: datetime, budapest: ZoneInfo) -> None:
    late = datetime(2026, 8, 14, 23, 30, tzinfo=budapest)
    assert make_event_id("Sub Focus", start, "A38") == make_event_id("Sub Focus", late, "A38")


def test_make_event_id_differs_for_titles_sharing_a_leading_segment(start: datetime) -> None:
    # Cutting at the first separator would reduce both to "koncert" and fuse them (§4.1).
    assert make_event_id("Koncert - Sub Focus", start, "A38 Hajó") != make_event_id(
        "Koncert - Chase & Status", start, "A38 Hajó"
    )


def test_make_event_id_differs_for_titles_sharing_a_leading_venue(start: datetime) -> None:
    # The mirror case: cutting at the last separator would reduce both to "a38" (§4.1).
    assert make_event_id("A38 | Koncert X", start, "A38 Hajó") != make_event_id(
        "A38 | Koncert Y", start, "A38 Hajó"
    )


def test_make_event_id_ignores_case_accents_and_whitespace(start: datetime) -> None:
    assert make_event_id("Élő Zene", start, "A38 Hajó") == make_event_id(
        "élő  zene ", start, "a38   hajó"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sub Focus (Budapest)", "sub focus"),
        ("Sub Focus (HU)", "sub focus"),
        ("Sub Focus (UK)", "sub focus"),
        ("Sub Focus (UK) (Budapest)", "sub focus"),
        ("Sub Focus (Live In Budapest)", "sub focus (live in budapest)"),
        ("Sub Focus (Deluxe Anniversary Edition)", "sub focus (deluxe anniversary edition)"),
    ],
)
def test_normalize_title_strips_only_short_parenthesised_suffixes(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sub Focus | A38", "sub focus | a38"),
        ("Sub Focus - élő koncert", "sub focus - elo koncert"),
        ("A38 | Koncert X", "a38 | koncert x"),
        ("Sub Focus (UK) | A38 Hajó", "sub focus (uk) | a38 hajo"),
    ],
)
def test_normalize_title_never_cuts_at_a_separator(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_normalize_title_leaves_a_real_title_intact() -> None:
    assert normalize_title(VILLON) == (
        "befogad es kitaszit a vilag - macsai pal es huzella peter villon-estje"
    )


def test_normalize_title_removes_accents_and_lowercases() -> None:
    assert normalize_title("Élő Koncert az Akvárium Klubban") == "elo koncert az akvarium klubban"
    assert normalize_title("ÁRVÍZTŰRŐ TÜKÖRFÚRÓGÉP") == "arvizturo tukorfurogep"


def test_normalize_title_collapses_whitespace() -> None:
    assert normalize_title("  Sub \t Focus\nLive  ") == "sub focus live"


def test_normalize_title_keeps_inner_hyphen() -> None:
    assert normalize_title("Élő koncert az A38-on") == "elo koncert az a38-on"


def test_strip_venue_suffix_removes_a_matching_venue() -> None:
    assert strip_venue_suffix("HØT SPØT 2026 | A38", "A38 Hajó") == "HØT SPØT 2026"


@pytest.mark.parametrize(
    ("title", "venue"),
    [
        ("HØT SPØT 2026 | A38", None),
        ("HØT SPØT 2026 | A38", "Akvárium Klub"),
        ("A38 | Koncert X", "A38 Hajó"),
        ("HØT SPØT 2026", "A38 Hajó"),
    ],
)
def test_strip_venue_suffix_leaves_the_title_alone(title: str, venue: str | None) -> None:
    assert strip_venue_suffix(title, venue) == title


def test_strip_venue_suffix_is_not_used_by_the_id(start: datetime) -> None:
    assert make_event_id("HØT SPØT 2026 | A38", start, "A38 Hajó") != make_event_id(
        "HØT SPØT 2026", start, "A38 Hajó"
    )


def test_normalize_venue_normalizes_like_title() -> None:
    assert normalize_venue("  Akvárium   Klub ") == "akvarium klub"


def test_normalize_venue_accepts_none() -> None:
    assert normalize_venue(None) == ""


def test_raw_event_defaults_and_is_frozen() -> None:
    raw = RawEvent(
        source_id="port-hu",
        source_event_key="123456",
        title="Sub Focus",
        url="https://port.hu/esemeny/123456",
    )
    assert raw.description is None
    assert raw.extra == {}
    with pytest.raises(ValidationError):
        raw.title = "other"


def test_event_requires_the_nullable_fields_explicitly(start: datetime) -> None:
    fields = {
        "id": make_event_id("Sub Focus", start, "A38 Hajó"),
        "source_ids": ["port-hu"],
        "urls": ["https://port.hu/esemeny/123456"],
        "title": "Sub Focus",
        "description": None,
        "start": start,
        "end": None,
        "effective_date": start.date(),
        "venue_name": "A38 Hajó",
        "district": "XI.",
        "lat": 47.4757,
        "lon": 19.0603,
        "distance_km": None,
        "price_min": 6900,
        "price_max": None,
        "categories": ["koncert"],
        "image_url": None,
    }
    event = Event(**fields)
    assert event.is_series is False
    assert event.score == 0.0
    assert event.group_size == 1
    assert event.score_breakdown == {}

    with pytest.raises(ValidationError):
        Event(**{k: v for k, v in fields.items() if k != "description"})
