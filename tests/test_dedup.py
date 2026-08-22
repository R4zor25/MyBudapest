from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from rapidfuzz.fuzz import token_set_ratio
from structlog.testing import capture_logs

from digest.config import Config
from digest.fetch.base import FetchResult, FetchTask
from digest.models import Event, make_event_id, normalize_title
from digest.pipeline.dedup import dedup, fuzzy_title, normalize_url
from digest.pipeline.normalize import normalize
from digest.sources.plugins.port_hu import PortHuSource

BUDAPEST = ZoneInfo("Europe/Budapest")
START = datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST)

CONFIG = Config(
    sources={
        "port-hu": {"priority": 10},
        "jegy-hu": {"priority": 20},
        # Weakest of the enabled sources, and the only date-only one -- so it is never the
        # merge base against a timed record. See the note in dedup._merge.
        "programturizmus": {"priority": 40},
    }
)


def make_event(**overrides: Any) -> Event:
    title = overrides.pop("title", "Sub Focus")
    start = overrides.pop("start", START)
    venue_name = overrides.pop("venue_name", "A38 Hajó")
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": ["https://port.hu/esemeny/zene/sub-focus/event-1"],
        "title": title,
        "description": None,
        "start": start,
        "end": None,
        "effective_date": start.date(),
        "venue_name": venue_name,
        "district": None,
        "lat": None,
        "lon": None,
        "distance_km": None,
        "price_min": None,
        "price_max": None,
        "categories": [],
        "image_url": None,
    }
    return Event(**{**base, **overrides})


def test_a_parenthesised_country_tag_merges_at_the_exact_level() -> None:
    # normalize_title drops "(UK)", so both events already carry the same id (SPEC 4.1).
    a = make_event(title="Sub Focus")
    b = make_event(title="Sub Focus (UK)", urls=["https://jegy.hu/x"], source_ids=["jegy-hu"])
    assert a.id == b.id

    (merged,) = dedup([a, b], CONFIG)

    assert merged.source_ids == ["port-hu", "jegy-hu"]
    assert len(merged.urls) == 2


def test_identical_titles_at_different_hours_of_one_day_share_an_id() -> None:
    # Documents why the 90 minute rule below can only govern the fuzzy level: the id is
    # built from the DATE, so two screenings of one film on one day are one event (4.1).
    a = make_event()
    b = make_event(start=START + timedelta(hours=3))

    assert a.id == b.id
    assert len(dedup([a, b], CONFIG)) == 1


def test_a_three_hour_gap_blocks_a_fuzzy_merge() -> None:
    a = make_event(title="Sub Focus lemezbemutató")
    b = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        start=START + timedelta(hours=3),
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
    )
    assert a.id != b.id

    assert len(dedup([a, b], CONFIG)) == 2


def test_the_same_pair_within_ninety_minutes_merges() -> None:
    a = make_event(title="Sub Focus lemezbemutató")
    b = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        start=START + timedelta(minutes=89),
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
    )

    assert len(dedup([a, b], CONFIG)) == 1


def test_strip_venue_suffix_is_what_makes_that_pair_comparable() -> None:
    # Without it the two titles score 56 and would never reach the 88 threshold.
    a = make_event(title="Sub Focus lemezbemutató")
    b = make_event(title="Sub Focus | A38 Hajó Nagyterem")

    assert token_set_ratio(normalize_title(a.title), normalize_title(b.title)) < 88
    assert token_set_ratio(fuzzy_title(a), fuzzy_title(b)) == 100


def test_hot_spot_merges_after_the_venue_suffix_is_stripped() -> None:
    a = make_event(title="HØT SPØT 2026 | A38")
    b = make_event(
        title="HØT SPØT 2026",
        urls=["https://jegy.hu/hot-spot"],
        source_ids=["jegy-hu"],
    )
    assert a.id != b.id

    (merged,) = dedup([a, b], CONFIG)

    assert merged.source_ids == ["port-hu", "jegy-hu"]


def test_a_different_venue_blocks_the_merge() -> None:
    a = make_event(venue_name="A38 Hajó")
    b = make_event(
        venue_name="Akvárium Klub",
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
    )
    assert a.id != b.id

    assert len(dedup([a, b], CONFIG)) == 2


def test_a_missing_venue_on_one_side_still_merges() -> None:
    a = make_event(venue_name="A38 Hajó")
    b = make_event(venue_name=None, urls=["https://jegy.hu/sub-focus"], source_ids=["jegy-hu"])
    assert a.id != b.id

    (merged,) = dedup([a, b], CONFIG)

    assert merged.venue_name == "A38 Hajó"


def test_a_score_in_the_ambiguous_band_is_logged_but_not_merged() -> None:
    a = make_event(title="Bach hangverseny")
    b = make_event(
        title="Bach orgonahangverseny",
        urls=["https://jegy.hu/bach"],
        source_ids=["jegy-hu"],
    )

    with capture_logs() as logs:
        events = dedup([a, b], CONFIG)

    assert len(events) == 2
    (ambiguous,) = [entry for entry in logs if entry["event"] == "ambiguous_dedup"]
    assert 80 <= ambiguous["score"] < 88
    assert ambiguous["source_a"] == "port-hu"
    assert ambiguous["source_b"] == "jegy-hu"


def test_merge_keeps_the_longer_description_and_both_urls() -> None:
    a = make_event(
        title="Sub Focus lemezbemutató",
        description="Rövid.",
        urls=["https://port.hu/esemeny/zene/sub-focus/event-1"],
    )
    b = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        description="Sokkal hosszabb leírás ugyanarról az eseményről.",
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
    )

    (merged,) = dedup([a, b], CONFIG)

    assert merged.description == b.description
    assert merged.urls == [*a.urls, *b.urls]
    assert merged.source_ids == ["port-hu", "jegy-hu"]


def test_the_lower_priority_source_is_the_base_whatever_the_order() -> None:
    strong = make_event(title="Sub Focus lemezbemutató", source_ids=["port-hu"])
    weak = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
    )

    (weak_first,) = dedup([weak, strong], CONFIG)
    (strong_first,) = dedup([strong, weak], CONFIG)

    assert weak_first.title == strong.title
    assert strong_first.title == strong.title
    assert weak_first.id == strong.id


def test_an_unknown_source_never_wins_the_base() -> None:
    known = make_event(title="Sub Focus lemezbemutató", source_ids=["port-hu"])
    unknown = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://elsewhere.example/x"],
        source_ids=["mystery"],
    )

    (merged,) = dedup([unknown, known], CONFIG)

    assert merged.title == known.title


def test_price_and_coordinates_come_from_whoever_has_them() -> None:
    base = make_event(title="Sub Focus lemezbemutató", source_ids=["port-hu"])
    richer = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
        price_min=4500,
        price_max=6000,
        lat=47.4757,
        lon=19.0603,
        distance_km=1.2,
        image_url="https://jegy.hu/cover.jpg",
        district="XI.",
        categories=["koncert"],
    )

    (merged,) = dedup([base, richer], CONFIG)

    assert (merged.price_min, merged.price_max) == (4500, 6000)
    assert (merged.lat, merged.lon, merged.distance_km) == (47.4757, 19.0603, 1.2)
    assert merged.image_url == "https://jegy.hu/cover.jpg"
    assert merged.district == "XI."
    assert merged.categories == ["koncert"]


def test_a_known_city_survives_a_merge_with_a_city_less_base() -> None:
    """§7.6 can exclude on city, so this is not the cosmetic fill-in that district is: if
    the higher-priority record has no address data and overwrites a source that does know
    the settlement, a Budapest event both sources agree on gets dropped as unknown."""
    base = make_event(source_ids=["port-hu"], city=None)
    knows_the_city = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://cooltix.hu/event/x"],
        source_ids=["cooltix"],
        city="Budapest",
    )

    (merged,) = dedup([base, knows_the_city], CONFIG)

    assert merged.city == "Budapest"


def test_a_stated_city_is_not_overwritten_by_a_lower_priority_one() -> None:
    base = make_event(source_ids=["port-hu"], city="Budapest")
    other = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://cooltix.hu/event/x"],
        source_ids=["cooltix"],
        city="Győr",
    )

    (merged,) = dedup([base, other], CONFIG)

    assert merged.city == "Budapest"


def test_a_date_only_event_merges_with_a_timed_one_on_the_same_day() -> None:
    """The blindness this removes: a source publishing a bare date lands on 00:00, so the
    90-minute gate could only ever compare it against 00:00-01:30 starts. Title and venue
    are unchanged as conditions -- only the start gate widens, and only when a clock is
    genuinely unknown."""
    timed = make_event(source_ids=["port-hu"], start=START, start_time_known=True)
    date_only = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://programturizmus.hu/x"],
        source_ids=["programturizmus"],
        start=START.replace(hour=0, minute=0),
        start_time_known=False,
    )

    merged = dedup([timed, date_only], CONFIG)

    assert len(merged) == 1
    assert set(merged[0].source_ids) == {"port-hu", "programturizmus"}
    # The timed record is the merge base (lower priority number), so the real clock and
    # its flag survive -- see the note in dedup._merge.
    assert merged[0].start_time_known is True
    assert merged[0].start == START


def test_a_date_only_event_does_not_merge_across_days() -> None:
    """The widened gate is a same-CALENDAR-DAY gate, not "no gate at all"."""
    timed = make_event(source_ids=["port-hu"], start=START, start_time_known=True)
    next_day = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://programturizmus.hu/x"],
        source_ids=["programturizmus"],
        start=(START + timedelta(days=1)).replace(hour=0, minute=0),
        start_time_known=False,
    )

    assert len(dedup([timed, next_day], CONFIG)) == 2


def test_two_timed_events_still_use_the_ninety_minute_gate() -> None:
    """The widening must not leak into the ordinary case: same day, same title, same
    venue, but four hours apart and both clocks real -- still two events."""
    early = make_event(source_ids=["port-hu"], start=START.replace(hour=14, minute=0))
    late = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://jegy.hu/x"],
        source_ids=["jegy-hu"],
        start=START.replace(hour=18, minute=0),
    )

    assert len(dedup([early, late], CONFIG)) == 2


def test_a_free_flag_travels_with_its_price() -> None:
    base = make_event(title="Sub Focus lemezbemutató")
    free = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
        price_min=0,
        is_free=True,
    )

    (merged,) = dedup([base, free], CONFIG)

    assert merged.price_min == 0
    assert merged.is_free is True


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://port.hu/esemeny/x?utm_source=news", "https://port.hu/esemeny/x"),
        ("https://port.hu/esemeny/x#tickets", "https://port.hu/esemeny/x"),
        ("https://PORT.hu/esemeny/x/", "https://port.hu/esemeny/x"),
        ("https://port.hu/esemeny/x?a=1&b=2", "https://port.hu/esemeny/x"),
    ],
)
def test_url_normalization(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


def test_the_same_url_with_tracking_parameters_merges() -> None:
    a = make_event(title="Sub Focus lemezbemutató", urls=["https://port.hu/esemeny/x"])
    b = make_event(
        title="Teljesen más cím",
        urls=["https://port.hu/esemeny/x?utm_source=hirlevel#jegyek"],
        source_ids=["jegy-hu"],
    )

    with capture_logs() as logs:
        (merged,) = dedup([a, b], CONFIG)

    assert merged.title == a.title
    assert [entry["reason"] for entry in logs if entry["event"] == "dedup_merge"] == ["strong"]


def test_a_url_match_ignores_the_start_time() -> None:
    # Pinned, not endorsed: SPEC 7.2 gives the strong level no time condition, so a series
    # that publishes every occurrence under one URL collapses into a single event.
    a = make_event(title="Kvízest", urls=["https://kvizestek.hu/esemenyek"])
    b = make_event(
        title="Kvízest",
        start=START + timedelta(days=7),
        urls=["https://kvizestek.hu/esemenyek"],
    )

    assert len(dedup([a, b], CONFIG)) == 1


def test_dedup_does_not_mutate_its_input() -> None:
    a = make_event(title="Sub Focus lemezbemutató", description="Rövid.")
    b = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        description="Hosszabb leírás.",
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
    )
    events = [a, b]
    before = [event.model_dump() for event in events]

    dedup(events, CONFIG)

    assert events == [a, b]
    assert [event.model_dump() for event in events] == before


def test_unrelated_events_are_left_alone() -> None:
    events = [
        make_event(title="Sub Focus", urls=["https://port.hu/a"]),
        make_event(title="Chase & Status", urls=["https://port.hu/b"]),
        make_event(title="Kaláka", venue_name="Kobuci Kert", urls=["https://port.hu/c"]),
    ]

    assert len(dedup(events, CONFIG)) == 3


def test_real_festival_line_up_survives_dedup_intact(repo_root: Path) -> None:
    """The hardest real case: eighteen of the twenty fixture records are sets at one venue,
    many within ninety minutes of each other. Nothing here may merge."""
    payload = json.loads(
        (repo_root / "tests" / "fixtures" / "port_hu_list.json").read_text(encoding="utf-8")
    )
    source = PortHuSource({"id": "port-hu"}, CONFIG)
    raw = list(
        source.parse(
            FetchResult(
                task=FetchTask(url="https://port.hu/fixture"),
                status=200,
                text="",
                json=payload,
                from_cache=False,
            )
        )
    )
    events = normalize(raw, CONFIG, now=datetime(2026, 8, 14, 10, 0, tzinfo=BUDAPEST))

    with capture_logs() as logs:
        deduped = dedup(events, CONFIG)

    assert len(deduped) == len(events) == 20
    assert [entry for entry in logs if entry["event"] == "dedup_merge"] == []

    # The shared stage prefix of the "C*NZÚRA FRISS: <artist>" sets lands in the band that
    # is reported rather than acted on — merging them would delete real events.
    ambiguous = [entry for entry in logs if entry["event"] == "ambiguous_dedup"]
    assert len(ambiguous) == 2
    assert all(80 <= entry["score"] < 88 for entry in ambiguous)


def test_three_sources_collapse_into_one_event() -> None:
    a = make_event(title="Sub Focus lemezbemutató", source_ids=["port-hu"])
    b = make_event(
        title="Sub Focus | A38 Hajó Nagyterem",
        urls=["https://jegy.hu/sub-focus"],
        source_ids=["jegy-hu"],
    )
    c = make_event(title="Sub Focus (UK)", urls=["https://welove.hu/x"], source_ids=["welove"])

    (merged,) = dedup([a, b, c], CONFIG)

    assert merged.source_ids == ["port-hu", "jegy-hu", "welove"]
    assert len(merged.urls) == 3
