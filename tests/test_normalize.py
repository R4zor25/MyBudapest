from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from digest.config import Config, HomeConfig, ScheduleConfig
from digest.models import RawEvent
from digest.pipeline.normalize import (
    clean_description,
    normalize,
    normalize_with_reasons,
    parse_price,
)

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=BUDAPEST)
# A run that starts after midnight, which every real run does. The whole defect below only
# shows itself at such an instant: at 00:00 exactly, a date-only start is not yet behind.
MORNING = datetime(2026, 8, 16, 9, 0, tzinfo=BUDAPEST)


def make_raw(**overrides: Any) -> RawEvent:
    base: dict[str, Any] = {
        "source_id": "port-hu",
        "source_event_key": "event-1",
        "title": "Sub Focus",
        "url": "https://port.hu/esemeny/zene/sub-focus/event-1",
        "start_raw": "2026-08-20 19:00:00",
    }
    return RawEvent(**{**base, **overrides})


@pytest.mark.parametrize(
    "start_raw",
    [
        "2026-08-20 19:00:00",
        "2026. 08. 20. 19:00",
        "2026-08-20T19:00:00+02:00",
        "2026-08-20T17:00:00Z",
        "2026.08.20. 19:00",  # programturizmus.hu's shape — dotted, no spaces (§ package 11)
        "2026. augusztus 20., csütörtök 19:00",  # bigcitylife.hu's shape — spelled-out month
    ],
)
def test_all_date_shapes_parse_to_the_same_instant(start_raw: str) -> None:
    (event,) = normalize([make_raw(start_raw=start_raw)], Config(), now=NOW)
    assert event.start == datetime(2026, 8, 20, 19, 0, tzinfo=BUDAPEST)


def test_naive_input_is_budapest_local_time_not_utc() -> None:
    (event,) = normalize([make_raw(start_raw="2026-08-20 19:00:00")], Config(), now=NOW)

    assert event.start.utcoffset() == timedelta(hours=2)
    assert event.start.astimezone(UTC).hour == 17
    assert event.start != datetime(2026, 8, 20, 19, 0, tzinfo=UTC)


def test_an_offset_is_converted_into_budapest() -> None:
    (event,) = normalize([make_raw(start_raw="2026-08-20T12:00:00-04:00")], Config(), now=NOW)

    assert event.start.tzinfo is not None
    assert (event.start.hour, event.start.minute) == (18, 0)


def test_end_is_parsed_when_present() -> None:
    raw = make_raw(start_raw="2026-08-20 19:00:00", end_raw="2026-08-21 23:59:00")
    (event,) = normalize([raw], Config(), now=NOW)

    assert event.end == datetime(2026, 8, 21, 23, 59, tzinfo=BUDAPEST)


def test_an_unparseable_end_keeps_the_event_but_drops_the_end() -> None:
    raw = make_raw(end_raw="jövő héten")
    with capture_logs() as logs:
        (event,) = normalize([raw], Config(), now=NOW)

    assert event.end is None
    assert [entry["event"] for entry in logs] == ["unparseable_end"]


def test_an_unparseable_start_is_dropped_and_the_others_survive() -> None:
    records = [
        make_raw(source_event_key="good-1"),
        make_raw(source_event_key="broken", start_raw="valamikor jövőre"),
        make_raw(source_event_key="good-2", start_raw="2026. 08. 21. 20:00"),
    ]

    with capture_logs() as logs:
        events = normalize(records, Config(), now=NOW)

    assert len(events) == 2
    dropped = [entry for entry in logs if entry["event"] == "unparseable_start"]
    assert len(dropped) == 1
    assert dropped[0]["value"] == "valamikor jövőre"


@pytest.mark.parametrize(
    "raw_price", ["ingyenes", "Ingyenes", "free", "díjtalan", "A belépés free"]
)
def test_free_prices(raw_price: str) -> None:
    assert parse_price(raw_price) == (0, None, True)


@pytest.mark.parametrize(
    ("raw_price", "expected"),
    [
        ("2000-4500 Ft", (2000, 4500, False)),
        ("2000 - 4500 Ft", (2000, 4500, False)),
        ("3 500 Ft", (3500, None, False)),
        ("3 500 Ft", (3500, None, False)),
        ("3.500 Ft", (3500, None, False)),
        ("4500 HUF", (4500, None, False)),
        ("a helyszínen", (None, None, False)),
        ("", (None, None, False)),
        (None, (None, None, False)),
    ],
)
def test_price_parsing(
    raw_price: str | None, expected: tuple[int | None, int | None, bool]
) -> None:
    assert parse_price(raw_price) == expected


def test_price_reaches_the_event() -> None:
    (event,) = normalize([make_raw(price_raw="2000-4500 Ft")], Config(), now=NOW)
    assert (event.price_min, event.price_max, event.is_free) == (2000, 4500, False)

    (free,) = normalize([make_raw(price_raw="ingyenes")], Config(), now=NOW)
    assert (free.price_min, free.is_free) == (0, True)


def test_a_small_hours_event_belongs_to_the_previous_evening() -> None:
    (event,) = normalize([make_raw(start_raw="2026-08-22 02:00:00")], Config(), now=NOW)

    assert event.start.date() == date(2026, 8, 22)
    assert event.effective_date == date(2026, 8, 21)


def test_an_evening_event_keeps_its_own_date() -> None:
    (event,) = normalize([make_raw(start_raw="2026-08-22 19:00:00")], Config(), now=NOW)
    assert event.effective_date == date(2026, 8, 22)


def test_past_events_are_dropped() -> None:
    past = make_raw(start_raw="2026-08-15 19:00:00")
    assert normalize([past], Config(), now=NOW) == []


def test_a_running_multi_day_event_is_not_past() -> None:
    running = make_raw(start_raw="2026-05-06 17:00:00", end_raw="2026-09-30 22:00:00")
    (event,) = normalize([running], Config(), now=NOW)

    assert event.start.date() == date(2026, 5, 6)
    assert event.end is not None


# --------------------------------------------------------------------------------------
# The past cut, and what 00:00 means on each side of it (§7.1)
# --------------------------------------------------------------------------------------


def test_a_date_only_event_dated_today_survives_a_morning_run() -> None:
    """The defect: a bare date parses to 00:00, so comparing it to `now` as an instant put
    every one of today's date-only events in the past by 00:00:01. It was silent — the
    events simply never appeared."""
    (event,) = normalize([make_raw(start_raw="2026.08.16.")], Config(), now=MORNING)

    assert event.start_time_known is False
    assert event.start.date() == date(2026, 8, 16)


def test_a_date_only_event_dated_yesterday_is_dropped() -> None:
    assert normalize([make_raw(start_raw="2026.08.15.")], Config(), now=MORNING) == []


def test_a_timed_event_that_started_three_hours_ago_is_dropped() -> None:
    # The clock is real here, so the instant comparison is the correct one: a concert that
    # started at 06:00 is genuinely over, not a record missing its time.
    assert normalize([make_raw(start_raw="2026-08-16 06:00:00")], Config(), now=MORNING) == []


def test_a_timed_event_starting_in_three_hours_survives() -> None:
    (event,) = normalize([make_raw(start_raw="2026-08-16 12:00:00")], Config(), now=MORNING)

    assert event.start_time_known is True
    assert event.start.hour == 12


def test_a_genuine_midnight_event_today_is_still_past_by_the_morning() -> None:
    """The bit comes from the parser, never from `start.time() == midnight` (§7.1). A
    source that published "00:00:00" stated midnight, so by 09:00 that event is over —
    exactly the case that makes reading the time back out of the value wrong."""
    midnight = make_raw(start_raw="2026-08-16 00:00:00")

    assert normalize([midnight], Config(), now=MORNING) == []


def test_a_date_only_run_is_not_past_on_its_final_day() -> None:
    """The same missing value at the closing boundary. programturizmus publishes ranges as
    two bare dates ("2026.08.20." — "2026.08.22."), and the end is what the past cut reads
    once there is one: read as an instant it ended at 00:00 on its last day, so the whole
    of that day was lost."""
    run = make_raw(start_raw="2026.08.14.", end_raw="2026.08.16.")

    (event,) = normalize([run], Config(), now=MORNING)

    assert event.end is not None and event.end.date() == date(2026, 8, 16)


def test_a_timed_run_that_ended_this_morning_is_past() -> None:
    ended = make_raw(start_raw="2026-08-14 19:00:00", end_raw="2026-08-16 02:00:00")

    assert normalize([ended], Config(), now=MORNING) == []


def test_the_past_drop_is_logged_with_the_reason_and_the_clock_bit() -> None:
    raw = [
        make_raw(source_event_key="bare", start_raw="2026.08.15."),
        make_raw(source_event_key="timed", start_raw="2026-08-16 06:00:00"),
    ]

    with capture_logs() as logs:
        normalize(raw, Config(), now=MORNING)

    dropped = {entry["key"]: entry for entry in logs if entry["event"] == "dropped_past"}
    assert set(dropped) == {"bare", "timed"}
    assert dropped["bare"]["time_known"] is False
    assert dropped["timed"]["time_known"] is True
    assert {entry["boundary"] for entry in dropped.values()} == {"start"}


def test_past_drops_are_counted_per_source_for_the_run_summary() -> None:
    """A source whose dates stop rolling forward keeps parsing cleanly and just goes quiet.
    Counting per source is what makes that visible instead of the run merely shrinking."""
    raw = [
        make_raw(source_id="stalled", source_event_key="a", start_raw="2026.08.15."),
        make_raw(source_id="stalled", source_event_key="b", start_raw="2026-08-01 19:00:00"),
        make_raw(source_id="port-hu", source_event_key="c", start_raw="2026-08-20 19:00:00"),
    ]

    outcome = normalize_with_reasons(raw, Config(), now=MORNING)

    assert [event.urls for event in outcome.events] == [[raw[2].url]]
    assert outcome.dropped_as_past == {"stalled": 2}


def test_events_beyond_the_horizon_are_dropped() -> None:
    config = Config(schedule=ScheduleConfig(horizon_days=14))
    inside = make_raw(source_event_key="inside", start_raw="2026-08-29 19:00:00")
    outside = make_raw(source_event_key="outside", start_raw="2026-09-30 19:00:00")

    events = normalize([inside, outside], config, now=NOW)

    assert [event.urls for event in events] == [[inside.url]]


@pytest.mark.parametrize(
    ("start_raw", "kept"),
    [
        # The horizon is 2026-08-30 09:00. Its own day is inside for both kinds, the next
        # day is outside for both — the split does not move the boundary.
        ("2026.08.30.", True),
        ("2026.08.31.", False),
        ("2026-08-30 08:00:00", True),
        ("2026-08-31 08:00:00", False),
        # A known clock still resolves within the day, which is the intended asymmetry:
        # 10:00 on the horizon's own day is past it by an hour.
        ("2026-08-30 10:00:00", False),
    ],
)
def test_the_horizon_boundary_treats_both_kinds_the_same_day(start_raw: str, kept: bool) -> None:
    config = Config(schedule=ScheduleConfig(horizon_days=14))

    events = normalize([make_raw(start_raw=start_raw)], config, now=MORNING)

    assert bool(events) is kept


def test_distance_is_measured_from_home() -> None:
    config = Config(home=HomeConfig(district="XI.", lat=47.47, lon=19.05))
    raw = make_raw(lat=47.4757, lon=19.0603)

    (event,) = normalize([raw], config, now=NOW)

    assert event.distance_km is not None
    assert 0.5 < event.distance_km < 2.0


def test_distance_needs_both_sides() -> None:
    with_home = Config(home=HomeConfig(district="XI.", lat=47.47, lon=19.05))

    (no_coords,) = normalize([make_raw()], with_home, now=NOW)
    (no_home,) = normalize([make_raw(lat=47.47, lon=19.05)], Config(), now=NOW)

    assert no_coords.distance_km is None
    assert no_home.distance_km is None


@pytest.mark.parametrize(
    ("district_raw", "postal_code", "expected"),
    [
        (11, None, "XI."),
        (3, None, "III."),
        ("III.", None, "III."),
        (None, "1113", "XI."),
        (None, "1033", "III."),
        (None, "1000", None),  # online-only events carry a postal code that is not a district
        (None, None, None),
        (99, None, None),
    ],
)
def test_district_resolution(
    district_raw: int | str | None, postal_code: str | None, expected: str | None
) -> None:
    raw = make_raw(district_raw=district_raw, postal_code=postal_code)
    (event,) = normalize([raw], Config(), now=NOW)
    assert event.district == expected


def test_description_is_decoded_collapsed_and_truncated() -> None:
    assert clean_description("Mi&eacute;rt   ilyen\n n&eacute;pszerű?") == "Miért ilyen népszerű?"
    assert clean_description("   ") is None
    assert clean_description(None) is None

    long_text = "szó " * 200
    truncated = clean_description(long_text)
    assert truncated is not None
    assert len(truncated) <= 400
    assert not truncated.endswith(" ")
    assert truncated.split()[-1] == "szó"  # cut on a word boundary, not mid-word


def test_the_canonical_fields_are_filled() -> None:
    raw = make_raw(venue_name="  A38   Hajó ", title="  Sub   Focus ")
    (event,) = normalize([raw], Config(), now=NOW)

    assert event.title == "Sub Focus"
    assert event.venue_name == "A38 Hajó"
    assert event.source_ids == ["port-hu"]
    assert event.urls == [raw.url]
    assert event.categories == []
    assert event.score == 0.0
    assert event.is_series is False
    assert len(event.id) == 16


def test_the_same_event_from_two_sources_gets_one_id() -> None:
    a = make_raw(source_id="port-hu", venue_name="A38 Hajó")
    b = make_raw(source_id="jegy-hu", venue_name="a38  hajó", url="https://jegy.hu/x")

    first, second = normalize([a, b], Config(), now=NOW)

    assert first.id == second.id


def test_real_port_hu_records_normalize_without_loss(repo_root: Path) -> None:
    from digest.fetch.base import FetchResult, FetchTask
    from digest.sources.plugins.port_hu import PortHuSource

    payload = json.loads(
        (repo_root / "tests" / "fixtures" / "port_hu_list.json").read_text(encoding="utf-8")
    )
    source = PortHuSource({"id": "port-hu"}, Config())
    result = FetchResult(
        task=FetchTask(url="https://port.hu/fixture"),
        status=200,
        text="",
        json=payload,
        from_cache=False,
    )
    raw = list(source.parse(result))

    events = normalize(raw, Config(), now=datetime(2026, 8, 14, 10, 0, tzinfo=BUDAPEST))

    assert len(events) == len(raw)
    assert all(event.start.tzinfo is not None for event in events)
    assert all(event.price_min is None for event in events)  # Port.hu publishes no price
    assert {event.district for event in events} == {"III.", "XI.", None}
    # The 02:00 Sziget sets belong to the previous evening.
    small_hours = [event for event in events if event.start.hour < 5]
    assert small_hours
    assert all(event.effective_date < event.start.date() for event in small_hours)
