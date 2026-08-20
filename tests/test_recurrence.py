from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from digest.config import Config, RecurrenceConfig
from digest.models import Event, make_event_id
from digest.pipeline.recurrence import recurrence

BUDAPEST = ZoneInfo("Europe/Budapest")


def make_event(**overrides: Any) -> Event:
    title = overrides.pop("title", "Sub Focus")
    start = overrides.pop("start", datetime(2026, 8, 20, 19, 0, tzinfo=BUDAPEST))
    venue_name = overrides.pop("venue_name", "A38 Hajó")
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": ["https://port.hu/esemeny/x"],
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


CONFIG = Config(recurrence=RecurrenceConfig(series_threshold_days=7))


def test_a_summer_long_festival_is_marked_a_series() -> None:
    event = make_event(
        start=datetime(2026, 5, 6, 17, 0, tzinfo=BUDAPEST),
        end=datetime(2026, 9, 30, 22, 0, tzinfo=BUDAPEST),
    )

    (result,) = recurrence([event], CONFIG)

    assert result.is_series is True


def test_exactly_at_the_threshold_is_not_a_series() -> None:
    event = make_event(
        start=datetime(2026, 8, 14, 19, 0, tzinfo=BUDAPEST),
        end=datetime(2026, 8, 21, 19, 0, tzinfo=BUDAPEST),
    )
    assert (event.end - event.start).days == 7

    (result,) = recurrence([event], CONFIG)

    assert result.is_series is False


def test_one_day_over_the_threshold_is_a_series() -> None:
    event = make_event(
        start=datetime(2026, 8, 14, 19, 0, tzinfo=BUDAPEST),
        end=datetime(2026, 8, 22, 19, 0, tzinfo=BUDAPEST),
    )
    assert (event.end - event.start).days == 8

    (result,) = recurrence([event], CONFIG)

    assert result.is_series is True


def test_no_end_is_not_a_series() -> None:
    event = make_event(end=None)

    (result,) = recurrence([event], CONFIG)

    assert result.is_series is False


def test_the_threshold_is_configurable() -> None:
    event = make_event(
        start=datetime(2026, 8, 14, 19, 0, tzinfo=BUDAPEST),
        end=datetime(2026, 8, 17, 19, 0, tzinfo=BUDAPEST),  # 3 days
    )
    lenient = Config(recurrence=RecurrenceConfig(series_threshold_days=2))
    strict = Config(recurrence=RecurrenceConfig(series_threshold_days=5))

    (as_series,) = recurrence([event], lenient)
    (not_series,) = recurrence([event], strict)

    assert as_series.is_series is True
    assert not_series.is_series is False


def test_recurrence_does_not_mutate_its_input() -> None:
    event = make_event(
        start=datetime(2026, 5, 6, 17, 0, tzinfo=BUDAPEST),
        end=datetime(2026, 9, 30, 22, 0, tzinfo=BUDAPEST),
    )

    recurrence([event], CONFIG)

    assert event.is_series is False
