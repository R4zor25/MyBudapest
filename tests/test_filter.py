from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from structlog.testing import capture_logs

from digest.config import Config, FiltersConfig, ScheduleConfig
from digest.models import Event, make_event_id
from digest.pipeline.filter import filter as filter_events

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=BUDAPEST)


def make_event(**overrides: Any) -> Event:
    title = overrides.pop("title", "Sub Focus")
    start = overrides.pop("start", NOW + timedelta(days=2))
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
        "categories": ["koncert"],
        "image_url": None,
    }
    return Event(**{**base, **overrides})


def test_an_unremarkable_event_survives() -> None:
    assert filter_events([make_event()], Config(), now=NOW) == [make_event()]


def test_beyond_the_horizon_is_excluded() -> None:
    config = Config(schedule=ScheduleConfig(horizon_days=14))
    inside = make_event(source_ids=["inside"], start=NOW + timedelta(days=13))
    outside = make_event(
        source_ids=["outside"],
        start=NOW + timedelta(days=15),
        urls=["https://port.hu/esemeny/y"],
    )

    with capture_logs() as logs:
        survivors = filter_events([inside, outside], config, now=NOW)

    assert [event.source_ids for event in survivors] == [["inside"]]
    (entry,) = [log for log in logs if log["event"] == "filtered"]
    assert entry["reason"] == "beyond_horizon"


def test_a_disallowed_category_is_excluded() -> None:
    config = Config(filters=FiltersConfig(categories=["koncert", "klub"]))
    allowed = make_event(source_ids=["allowed"], categories=["koncert"])
    blocked = make_event(
        source_ids=["blocked"],
        categories=["kviz"],
        urls=["https://port.hu/esemeny/y"],
    )

    survivors = filter_events([allowed, blocked], config, now=NOW)

    assert [event.source_ids for event in survivors] == [["allowed"]]


def test_no_category_restriction_means_everything_passes() -> None:
    config = Config(filters=FiltersConfig(categories=None))
    event = make_event(categories=["kviz"])

    assert filter_events([event], config, now=NOW) == [event]


def test_an_uncategorized_event_is_excluded_when_a_restriction_is_active() -> None:
    config = Config(filters=FiltersConfig(categories=["koncert"]))
    event = make_event(categories=[])

    assert filter_events([event], config, now=NOW) == []


def test_a_price_above_the_maximum_is_excluded() -> None:
    config = Config(filters=FiltersConfig(max_price_huf=12000))
    cheap = make_event(source_ids=["cheap"], price_min=6000)
    expensive = make_event(
        source_ids=["expensive"],
        price_min=15000,
        urls=["https://port.hu/esemeny/y"],
    )
    unknown = make_event(
        source_ids=["unknown"],
        price_min=None,
        urls=["https://port.hu/esemeny/z"],
    )

    survivors = filter_events([cheap, expensive, unknown], config, now=NOW)

    assert {event.source_ids[0] for event in survivors} == {"cheap", "unknown"}


def test_a_blocked_keyword_is_excluded_accent_and_case_insensitive() -> None:
    config = Config(filters=FiltersConfig(blocked_keywords=["bábszínház"]))
    blocked = make_event(title="Ma este BÁBSZÍNHÁZ a gyerekeknek")
    allowed = make_event(title="Esti koncert", urls=["https://port.hu/esemeny/y"])

    survivors = filter_events([blocked, allowed], config, now=NOW)

    assert survivors == [allowed]


def test_a_keyword_does_not_match_in_the_middle_of_a_word() -> None:
    config = Config(filters=FiltersConfig(blocked_keywords=["koncert"]))
    event = make_event(title="Szimfonikuskoncert-bérlet")

    assert filter_events([event], config, now=NOW) == [event]


def test_blocked_keywords_widened_with_the_shared_matcher() -> None:
    """blocked_keywords runs through the same contains_word as categorize (§7.6), so
    prefix matching widened exclusion too -- deliberately: "gyerekprogram" should also
    block "gyerekprogramok". The cost is the same compound ambiguity, and the same `$`
    opt-out applies."""
    config = Config(filters=FiltersConfig(blocked_keywords=["gyerekprogram"]))
    inflected = make_event(title="Hétvégi gyerekprogramok a ligetben")

    assert filter_events([inflected], config, now=NOW) == []
    exact = Config(filters=FiltersConfig(blocked_keywords=["gyerekprogram$"]))
    assert filter_events([inflected], exact, now=NOW) == [inflected]


def test_an_already_sent_event_is_excluded() -> None:
    event = make_event()

    assert filter_events([event], Config(), sent_ids=frozenset(), now=NOW) == [event]
    assert filter_events([event], Config(), sent_ids=frozenset({event.id}), now=NOW) == []


def test_every_exclusion_reason_is_logged() -> None:
    config = Config(filters=FiltersConfig(max_price_huf=1000))
    event = make_event(price_min=5000)

    with capture_logs() as logs:
        filter_events([event], config, now=NOW)

    (entry,) = [log for log in logs if log["event"] == "filtered"]
    assert entry["reason"] == "price_too_high"
    assert entry["event_id"] == event.id


def test_min_score_is_not_one_of_the_five_reasons_here() -> None:
    # SPEC 7.6's sixth reason needs Event.score, which does not exist until score() runs
    # (filter precedes score in the pipeline). filter() must not invent a stand-in check.
    config = Config(filters=FiltersConfig(min_score=999))
    event = make_event()

    assert filter_events([event], config, now=NOW) == [event]


def test_filter_does_not_mutate_its_input() -> None:
    event = make_event()
    events = [event]

    filter_events(events, Config(), now=NOW)

    assert events == [event]
