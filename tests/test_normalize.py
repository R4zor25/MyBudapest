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
from digest.pipeline.normalize import clean_description, normalize, parse_price

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=BUDAPEST)


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


def test_events_beyond_the_horizon_are_dropped() -> None:
    config = Config(schedule=ScheduleConfig(horizon_days=14))
    inside = make_raw(source_event_key="inside", start_raw="2026-08-29 19:00:00")
    outside = make_raw(source_event_key="outside", start_raw="2026-09-30 19:00:00")

    events = normalize([inside, outside], config, now=NOW)

    assert [event.urls for event in events] == [[inside.url]]


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
