from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from structlog.testing import capture_logs

from digest.config import Config, GroupingConfig
from digest.models import Event, make_event_id
from digest.pipeline.group import group, group_with_counts

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


# --------------------------------------------------------------------------------------
# Venue-less events are excluded from grouping (§7.4)
# --------------------------------------------------------------------------------------


def test_venueless_events_all_survive_individually() -> None:
    """Six events with no venue, same day and category. Under the old key they were one
    bucket and collapsed into a single "None — 6 program" row; a venue group needs a
    venue, and these events have nothing to do with each other."""
    events = [make_event(i, venue_name=None, title=f"Program {i}") for i in range(6)]

    result = group(events, Config())

    assert len(result) == 6
    assert {event.title for event in result} == {f"Program {i}" for i in range(6)}
    assert all(event.group_size == 1 for event in result)


def test_venueless_events_pass_through_while_a_real_venue_still_collapses() -> None:
    """The two halves in one run: the venue group does its job, the venue-less events do
    not become a second, meaningless one."""
    venueless = [make_event(i, venue_name=None, title=f"Program {i}") for i in range(6)]
    at_venue = [make_event(10 + i, venue_name="Sziget Fesztivál") for i in range(5)]

    result = group(venueless + at_venue, Config())

    collapsed = [event for event in result if event.group_size > 1]
    assert len(collapsed) == 1
    assert collapsed[0].title == "Sziget Fesztivál — 5 program"
    assert len(result) == 7
    assert {f"Program {i}" for i in range(6)} <= {event.title for event in result}


def test_max_per_venue_does_not_apply_to_venueless_events() -> None:
    """A cap of one would leave a single event of any real venue group under
    min_group_size. There is no venue to cap here, so all four stay."""
    config = Config(grouping=GroupingConfig(min_group_size=99, max_per_venue=1))
    events = [make_event(i, venue_name=None, title=f"Program {i}") for i in range(4)]

    assert len(group(events, config)) == 4


def test_venueless_events_keep_their_position_in_the_output() -> None:
    """Excluding them must not reshuffle everything else."""
    events = [
        make_event(0, venue_name=None, title="első"),
        make_event(1, venue_name="Akvárium"),
        make_event(2, venue_name=None, title="utolsó"),
    ]

    result = group(events, Config())

    assert [event.title for event in result] == ["első", "Act 1", "utolsó"]


def test_the_stage_counts_and_logs_the_venueless_events() -> None:
    """Requirement 3: a source that stops supplying venue names has to be visible in the
    run summary, not just quietly reshape the digest."""
    events = [make_event(i, venue_name=None) for i in range(6)]
    events += [make_event(10 + i, venue_name="Sziget Fesztivál") for i in range(5)]

    with capture_logs() as logs:
        outcome = group_with_counts(events, Config())

    assert outcome.ungrouped_venueless == 6
    (entry,) = [line for line in logs if line["event"] == "grouping_skipped_venueless"]
    assert entry["count"] == 6
    assert entry["sources"] == ["port-hu"]


def test_nothing_is_logged_when_every_event_has_a_venue() -> None:
    with capture_logs() as logs:
        outcome = group_with_counts(make_lineup(5), Config())

    assert outcome.ungrouped_venueless == 0
    assert not [line for line in logs if line["event"] == "grouping_skipped_venueless"]
