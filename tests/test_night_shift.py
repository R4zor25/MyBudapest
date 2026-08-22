from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from digest.config import Config, load_config
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent
from digest.pipeline.normalize import normalize, parse_datetime
from digest.sources.registry import load_sources

BUDAPEST = ZoneInfo("Europe/Budapest")
# Early enough that nothing in these cases is dropped as past (§7.1).
NOW = datetime(2026, 8, 10, 9, 0, tzinfo=BUDAPEST)


def raw(start_raw: str, **overrides: Any) -> RawEvent:
    base: dict[str, Any] = {
        "source_id": "test",
        "source_event_key": "k",
        "title": "Esemény",
        "url": "https://example.hu/x",
        "start_raw": start_raw,
    }
    return RawEvent(**{**base, **overrides})


def normalize_one(start_raw: str, config: Config | None = None):
    (event,) = normalize([raw(start_raw)], config or Config(), now=NOW)
    return event


# --------------------------------------------------------------------------------------
# The three cases the rule has to tell apart
# --------------------------------------------------------------------------------------


def test_a_late_night_event_still_belongs_to_the_previous_evening() -> None:
    """Unchanged behaviour, and the reason night_shift exists: a 02:00 Saturday set is
    part of Friday night."""
    event = normalize_one("2026-08-15 02:00:00")

    assert event.start_time_known is True
    assert event.start.date() == date(2026, 8, 15)
    assert event.effective_date == date(2026, 8, 14)


def test_a_date_only_event_keeps_its_own_day() -> None:
    """The defect this fixes. "2026.08.15." carries no clock, so the 00:00 it parses to is
    a missing value — shifting it back five hours filed the event on the 14th."""
    event = normalize_one("2026.08.15.")

    assert event.start_time_known is False
    assert event.effective_date == date(2026, 8, 15)


def test_a_genuine_midnight_event_still_shifts() -> None:
    """The case that makes `start.time() == midnight` the wrong test. This source stated
    00:00, so it means midnight, so it belongs to the previous evening like any other
    small-hours event."""
    event = normalize_one("2026-08-15 00:00:00")

    assert event.start_time_known is True
    assert event.effective_date == date(2026, 8, 14)


# --------------------------------------------------------------------------------------
# Which formats carry a clock
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "time_known"),
    [
        ("2026-08-15", False),
        ("2026.08.15.", False),
        ("2026. 08. 15.", False),
        ("2026-08-15 19:30:00", True),
        ("2026-08-15T19:30:00", True),
        ("2026-08-15T19:30:00+02:00", True),
        ("2026-08-28T16:30:00.000Z", True),
        ("2026. 08. 15. 19:30", True),
        ("2026.08.15. 19:30", True),
        ("2026. augusztus 15., szombat 19:30", True),
    ],
)
def test_the_parser_reports_whether_the_format_carried_a_clock(text: str, time_known: bool) -> None:
    parsed = parse_datetime(text, BUDAPEST)

    assert parsed is not None, f"{text!r} should parse"
    assert parsed[1] is time_known


def test_an_unparseable_start_is_still_none() -> None:
    assert parse_datetime("jövő kedden", BUDAPEST) is None
    assert parse_datetime("", BUDAPEST) is None


# --------------------------------------------------------------------------------------
# Requirement 6, against real data rather than a synthetic event
# --------------------------------------------------------------------------------------


@pytest.fixture
def port_hu_events(config_path: Path, sources_dir: Path):
    config = load_config(config_path, sources_dir, None)
    source = next(s for s in load_sources(config) if s.id == "port-hu")
    text = (Path(__file__).parent / "fixtures/port_hu_list.json").read_text(encoding="utf-8")
    result = FetchResult(
        task=FetchTask(url="https://port.hu/x"),
        status=200,
        text=text,
        json=json.loads(text),
        from_cache=False,
    )
    return normalize(
        list(source.parse(result)), config, now=datetime(2026, 8, 14, 9, tzinfo=BUDAPEST)
    )


def test_port_hu_timestamps_all_count_as_known(port_hu_events) -> None:
    assert port_hu_events
    assert all(e.start_time_known for e in port_hu_events)


def test_port_hu_night_sets_still_shift(port_hu_events) -> None:
    """The festival sets this rule was built for: Port.hu publishes real clock values, so
    its 01:00 and 03:00 starts must keep landing on the previous evening."""
    small_hours = [e for e in port_hu_events if e.start.hour < 5]

    assert small_hours, "fixture should contain small-hours sets"
    assert all(e.effective_date == (e.start.date() - timedelta(days=1)) for e in small_hours)
