from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from digest.config import Config, GroupingConfig
from digest.models import Event, make_event_id
from digest.pipeline.group import group

BUDAPEST = ZoneInfo("Europe/Budapest")
EFFECTIVE_DATE = datetime(2026, 8, 16, 20, 0, tzinfo=BUDAPEST).date()


def make_event(index: int, **overrides: Any) -> Event:
    title = overrides.pop("title", f"Act {index}")
    start = overrides.pop("start", datetime(2026, 8, 16, 20 + index % 4, 0, tzinfo=BUDAPEST))
    venue_name = overrides.pop("venue_name", "Sziget Fesztivál")
    score = overrides.pop("score", float(index))
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": [f"https://port.hu/esemeny/{index}"],
        "title": title,
        "description": None,
        "start": start,
        "end": None,
        "effective_date": EFFECTIVE_DATE,
        "venue_name": venue_name,
        "district": None,
        "lat": None,
        "lon": None,
        "distance_km": None,
        "price_min": None,
        "price_max": None,
        "categories": ["koncert"],
        "image_url": None,
        "score": score,
    }
    return Event(**{**base, **overrides})


def make_lineup(count: int) -> list[Event]:
    return [make_event(i) for i in range(count)]


def test_a_full_festival_lineup_collapses_to_one_row() -> None:
    events = make_lineup(17)

    (result,) = group(events, Config())

    assert result.group_size == 17
    assert result.title == "Sziget Fesztivál — 17 program"


def test_below_min_group_size_stays_uncollapsed() -> None:
    events = make_lineup(3)
    config = Config(grouping=GroupingConfig(min_group_size=4, max_per_venue=3))

    result = group(events, config)

    assert len(result) == 3
    assert {event.title for event in result} == {event.title for event in events}
    assert all(event.group_size == 1 for event in result)


def test_below_min_group_size_but_above_max_per_venue_is_capped_to_the_top_scorers() -> None:
    events = make_lineup(6)  # scores 0..5
    config = Config(grouping=GroupingConfig(min_group_size=8, max_per_venue=3))

    result = group(events, config)

    assert len(result) == 3
    assert {event.title for event in result} == {"Act 5", "Act 4", "Act 3"}


def test_the_collapsed_rows_score_is_the_highest_member_score() -> None:
    events = make_lineup(17)  # scores 0..16, highest is Act 16 with score 16

    (result,) = group(events, Config())

    assert result.score == 16.0


def test_the_collapsed_rows_start_is_the_earliest_members_not_the_top_scorers() -> None:
    # The top scorer (highest index/score) starts latest here — "doors open" for a
    # collapsed row means the earliest start across every member, not the headliner's.
    early = make_event(0, start=datetime(2026, 8, 16, 18, 0, tzinfo=BUDAPEST), score=1)
    mid = make_event(1, start=datetime(2026, 8, 16, 20, 0, tzinfo=BUDAPEST), score=2)
    late_top_scorer = make_event(2, start=datetime(2026, 8, 16, 22, 0, tzinfo=BUDAPEST), score=3)
    config = Config(grouping=GroupingConfig(min_group_size=3, max_per_venue=3))

    (result,) = group([mid, late_top_scorer, early], config)

    assert result.start == early.start


def test_the_description_lists_the_three_highest_scoring_titles() -> None:
    events = make_lineup(5)
    config = Config(grouping=GroupingConfig(min_group_size=4, max_per_venue=3))

    (result,) = group(events, config)

    assert result.description == "Act 4, Act 3, Act 2"


def test_urls_fall_back_to_the_top_scoring_members_url() -> None:
    events = make_lineup(5)
    config = Config(grouping=GroupingConfig(min_group_size=4, max_per_venue=3))

    (result,) = group(events, config)

    assert result.urls == ["https://port.hu/esemeny/4"]


def test_different_venues_are_not_merged_together() -> None:
    a38 = make_event(0, venue_name="A38 Hajó", urls=["https://port.hu/esemeny/a"])
    sziget = make_event(1, venue_name="Sziget Fesztivál", urls=["https://port.hu/esemeny/b"])

    result = group([a38, sziget], Config())

    assert len(result) == 2


def test_different_effective_dates_are_not_merged_together() -> None:
    day_one = make_event(0, effective_date=datetime(2026, 8, 14, tzinfo=BUDAPEST).date())
    day_two = make_event(
        1,
        effective_date=datetime(2026, 8, 15, tzinfo=BUDAPEST).date(),
        urls=["https://port.hu/esemeny/b"],
    )

    result = group([day_one, day_two], Config())

    assert len(result) == 2


def test_different_primary_categories_are_not_merged_together() -> None:
    concert = make_event(0, categories=["koncert"], urls=["https://port.hu/esemeny/a"])
    theatre = make_event(1, categories=["szinhaz"], urls=["https://port.hu/esemeny/b"])

    result = group([concert, theatre], Config())

    assert len(result) == 2


def test_the_collapsed_id_is_stable_across_a_different_line_up_size() -> None:
    # The group's identity is the venue/date/category slot, not the member count — adding
    # or dropping an act between runs must not make the ledger treat it as a new event.
    (seventeen,) = group(make_lineup(17), Config())
    (eighteen,) = group(make_lineup(18), Config())

    assert seventeen.id == eighteen.id


def test_source_ids_and_categories_are_unioned_across_members() -> None:
    a = make_event(0, source_ids=["port-hu"], categories=["koncert"])
    b = make_event(
        1,
        source_ids=["jegy-hu"],
        categories=["koncert", "klub"],
        urls=["https://port.hu/esemeny/b"],
    )
    c = make_event(
        2, source_ids=["port-hu"], categories=["koncert"], urls=["https://port.hu/esemeny/c"]
    )
    d = make_event(
        3, source_ids=["welove"], categories=["koncert"], urls=["https://port.hu/esemeny/d"]
    )

    (result,) = group([a, b, c, d], Config())

    assert result.source_ids == ["port-hu", "jegy-hu", "welove"]
    assert result.categories == ["koncert", "klub"]


def test_group_does_not_mutate_its_input() -> None:
    events = make_lineup(17)
    originals = [event.model_copy() for event in events]

    group(events, Config())

    assert events == originals
