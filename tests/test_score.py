from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from digest.config import (
    CheapBonus,
    Config,
    FiltersConfig,
    HomeConfig,
    NightShiftConfig,
    ProximityConfig,
    ScoringConfig,
    SoonBonus,
)
from digest.models import Event, make_event_id
from digest.pipeline.score import score, score_one

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=BUDAPEST)

FRIDAY_NIGHT = datetime(2026, 8, 22, 2, 0, tzinfo=BUDAPEST)  # small hours of Saturday
FRIDAY_EFFECTIVE_DATE = (FRIDAY_NIGHT - timedelta(hours=5)).date()


def make_event(**overrides: Any) -> Event:
    title = overrides.pop("title", "Sub Focus")
    start = overrides.pop("start", datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST))
    venue_name = overrides.pop("venue_name", "A38 Hajó")
    effective_date = overrides.pop("effective_date", start.date())
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": ["https://port.hu/esemeny/x"],
        "title": title,
        "description": None,
        "start": start,
        "end": None,
        "effective_date": effective_date,
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


def test_weekday_weight_uses_effective_date_not_start() -> None:
    config = Config(
        scoring=ScoringConfig(weekday_weights={"fri": 2, "sat": 0}),
        night_shift=NightShiftConfig(before_hour=5),
    )
    # start is Saturday 02:00, but the night shift already moved effective_date to Friday.
    assert FRIDAY_NIGHT.date().weekday() == 5  # Saturday
    assert FRIDAY_EFFECTIVE_DATE.weekday() == 4  # Friday

    event = make_event(start=FRIDAY_NIGHT, effective_date=FRIDAY_EFFECTIVE_DATE)

    (result,) = score([event], config, now=NOW)

    assert result.score_breakdown["weekday_weight"] == 2


def test_a_free_event_receives_free_bonus() -> None:
    config = Config(scoring=ScoringConfig(free_bonus=2))
    event = make_event(is_free=True, price_min=0)

    (result,) = score([event], config, now=NOW)

    assert result.score_breakdown["free_bonus"] == 2


def test_a_non_free_event_gets_no_free_bonus() -> None:
    config = Config(scoring=ScoringConfig(free_bonus=2))
    event = make_event(is_free=False)

    (result,) = score([event], config, now=NOW)

    assert result.score_breakdown["free_bonus"] == 0


def test_a_distant_event_receives_a_negative_distance_term() -> None:
    # score_one, not score(): with every other term at zero the total lands below the
    # default min_score and score() would drop it before this test could inspect it.
    config = Config(scoring=ScoringConfig(proximity=ProximityConfig(distance_penalty_per_km=0.3)))
    event = make_event(distance_km=10)

    result = score_one(event, config, now=NOW)

    assert result.score_breakdown["distance_penalty"] == pytest.approx(-3.0)


def _capped(cap: float | None) -> Config:
    return Config(
        scoring=ScoringConfig(
            proximity=ProximityConfig(distance_penalty_per_km=0.3, penalty_cap_km=cap)
        )
    )


def test_the_penalty_stops_growing_past_the_cap() -> None:
    """A far event ranks below a near one, but the distance term must not swamp the rest
    of the formula: uncapped, 40 km at 0.3/km is -12, more than every category weight,
    keyword boost and bonus in the template put together."""
    config = _capped(8)

    at_20 = score_one(make_event(distance_km=20), config, now=NOW)
    at_40 = score_one(make_event(distance_km=40), config, now=NOW)

    assert at_40.score_breakdown["distance_penalty"] == at_20.score_breakdown["distance_penalty"]
    assert at_40.score_breakdown["distance_penalty"] == pytest.approx(-2.4)


def test_an_event_inside_the_cap_is_charged_the_full_distance() -> None:
    result = score_one(make_event(distance_km=5), _capped(8), now=NOW)

    assert result.score_breakdown["distance_penalty"] == pytest.approx(-1.5)


def test_the_breakdown_records_the_capped_penalty_not_the_raw_distance() -> None:
    """`digest explain` reads this dict, so it has to show the number that was actually
    charged -- otherwise the breakdown would not sum to the score."""
    result = score_one(make_event(distance_km=40), _capped(8), now=NOW)

    assert result.score_breakdown["distance_penalty"] == pytest.approx(-2.4)
    assert result.score == pytest.approx(sum(result.score_breakdown.values()))


def test_without_a_cap_the_penalty_is_unbounded() -> None:
    """The pre-existing behaviour, kept for anyone who leaves the field unset: the cap is
    opt-in, not a default."""
    result = score_one(make_event(distance_km=40), _capped(None), now=NOW)

    assert result.score_breakdown["distance_penalty"] == pytest.approx(-12.0)


def test_the_cap_bounds_the_penalty_and_never_excludes() -> None:
    """The whole point of the rename: this field cannot remove an event from the digest.
    Exclusion is filters.geo.max_distance_km (§7.6), tested in test_filter_geo.py."""
    from digest.pipeline.filter import content_filter as filter_events

    event = make_event(distance_km=40)

    assert filter_events([event], _capped(8), now=NOW) == [event]


def test_the_breakdown_sums_to_the_score() -> None:
    config = Config(
        scoring=ScoringConfig(
            category_weights={"koncert": 4},
            keyword_boosts={"koreai": 3},
            free_bonus=2,
            cheap_bonus=CheapBonus(under_huf=4000, points=1),
            proximity=ProximityConfig(same_district_bonus=2, distance_penalty_per_km=0.3),
            novelty_bonus=2,
            soon_bonus=SoonBonus(within_days=7, points=1),
            weekday_weights={"thu": 1},
        ),
        home=HomeConfig(district="XI.", lat=47.47, lon=19.05),
    )
    event = make_event(
        title="Koreai koncert",
        is_free=True,
        price_min=0,
        distance_km=1.5,
        district="XI.",
        start=datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST),  # a Thursday
        effective_date=datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST).date(),
    )

    (result,) = score([event], config, now=NOW)

    assert sum(result.score_breakdown.values()) == pytest.approx(result.score)
    assert result.score > 0


def test_a_zero_term_is_still_present_in_the_breakdown() -> None:
    config = Config()  # no proximity, no cheap_bonus, no soon_bonus, no weekday_weights
    event = make_event(is_free=False)

    (result,) = score([event], config, now=NOW)

    assert result.score_breakdown.keys() == {
        "category_weight",
        "keyword_boosts",
        "free_bonus",
        "cheap_bonus",
        "same_district_bonus",
        "distance_penalty",
        "novelty_bonus",
        "soon_bonus",
        "weekday_weight",
        "pinned_bonus",  # package 14: 0 unless the event's id is in pinned_ids
    }
    assert result.score_breakdown["cheap_bonus"] == 0
    assert result.score_breakdown["same_district_bonus"] == 0
    assert result.score_breakdown["soon_bonus"] == 0
    assert result.score_breakdown["pinned_bonus"] == 0


def test_category_weight_uses_the_primary_category() -> None:
    config = Config(scoring=ScoringConfig(category_weights={"koncert": 4, "klub": 2}))
    event = make_event(categories=["koncert", "klub"])

    (result,) = score([event], config, now=NOW)

    assert result.score_breakdown["category_weight"] == 4


def test_an_event_with_no_category_gets_zero_category_weight() -> None:
    config = Config(scoring=ScoringConfig(category_weights={"koncert": 4}))
    event = make_event(categories=[])

    (result,) = score([event], config, now=NOW)

    assert result.score_breakdown["category_weight"] == 0


def test_keyword_boosts_are_accent_and_case_insensitive() -> None:
    config = Config(scoring=ScoringConfig(keyword_boosts={"koreai": 3}))
    event = make_event(title="KOREAI est", description=None)

    (result,) = score([event], config, now=NOW)

    assert result.score_breakdown["keyword_boosts"] == 3


def test_cheap_bonus_applies_strictly_under_the_threshold() -> None:
    config = Config(scoring=ScoringConfig(cheap_bonus=CheapBonus(under_huf=4000, points=1)))

    (cheap,) = score([make_event(price_min=3500)], config, now=NOW)
    (boundary,) = score([make_event(price_min=4000)], config, now=NOW)
    (unknown,) = score([make_event(price_min=None)], config, now=NOW)

    assert cheap.score_breakdown["cheap_bonus"] == 1
    assert boundary.score_breakdown["cheap_bonus"] == 0
    assert unknown.score_breakdown["cheap_bonus"] == 0


def test_same_district_bonus_needs_a_configured_home() -> None:
    config = Config(
        scoring=ScoringConfig(proximity=ProximityConfig(same_district_bonus=2)),
        home=HomeConfig(district="XI.", lat=47.47, lon=19.05),
    )

    (same,) = score([make_event(district="XI.")], config, now=NOW)
    (other,) = score([make_event(district="II.")], config, now=NOW)
    (unknown,) = score([make_event(district=None)], config, now=NOW)

    assert same.score_breakdown["same_district_bonus"] == 2
    assert other.score_breakdown["same_district_bonus"] == 0
    assert unknown.score_breakdown["same_district_bonus"] == 0


def test_soon_bonus_applies_within_the_configured_window() -> None:
    config = Config(scoring=ScoringConfig(soon_bonus=SoonBonus(within_days=7, points=1)))
    soon = make_event(start=NOW + timedelta(days=3))
    later = make_event(start=NOW + timedelta(days=30))

    (soon_result,) = score([soon], config, now=NOW)
    (later_result,) = score([later], config, now=NOW)

    assert soon_result.score_breakdown["soon_bonus"] == 1
    assert later_result.score_breakdown["soon_bonus"] == 0


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        # The defect: `start - now` is negative for anything already begun, and only the
        # upper bound was checked, so "soon" paid out best to the events that started
        # longest ago. A multi-day pass that opened in April is kept by §7.1 because it is
        # still running, and it was collecting the full bonus 130 days in.
        (timedelta(days=-130), 0),
        (timedelta(hours=-1), 0),
        # Both ends of the window are inclusive: starting this instant is not "past", and
        # starting exactly `within_days` out is still soon.
        (timedelta(0), 1),
        (timedelta(days=1), 1),
        (timedelta(days=7), 1),
        (timedelta(days=8), 0),
    ],
    ids=["130-days-ago", "an-hour-ago", "now", "tomorrow", "exactly-the-window", "one-past"],
)
def test_soon_bonus_is_bounded_at_both_ends(offset: timedelta, expected: float) -> None:
    config = Config(scoring=ScoringConfig(soon_bonus=SoonBonus(within_days=7, points=1)))

    (result,) = score([make_event(start=NOW + offset)], config, now=NOW)

    assert result.score_breakdown["soon_bonus"] == expected


def test_novelty_bonus_is_zero_for_an_id_already_in_the_ledger() -> None:
    config = Config(scoring=ScoringConfig(novelty_bonus=2))
    event = make_event()

    (novel,) = score([event], config, sent_ids=frozenset(), now=NOW)
    (known,) = score([event], config, sent_ids=frozenset({event.id}), now=NOW)

    assert novel.score_breakdown["novelty_bonus"] == 2
    assert known.score_breakdown["novelty_bonus"] == 0


def test_min_score_drops_low_scoring_events() -> None:
    config = Config(
        scoring=ScoringConfig(category_weights={"koncert": 1}, keyword_boosts={"koreai": 5}),
        filters=FiltersConfig(min_score=3),
    )
    low = make_event(title="Sub Focus", source_ids=["low"])
    high = make_event(
        title="Koreai koncert",
        source_ids=["high"],
        urls=["https://port.hu/esemeny/y"],
    )

    survivors = score([low, high], config, now=NOW)

    assert [event.source_ids for event in survivors] == [["high"]]


def test_score_does_not_mutate_its_input() -> None:
    event = make_event()

    score([event], Config(), now=NOW)

    assert event.score == 0.0
    assert event.score_breakdown == {}


def test_score_one_matches_the_batch_result_before_the_min_score_cut() -> None:
    config = Config(scoring=ScoringConfig(category_weights={"koncert": 1}))
    event = make_event()

    direct = score_one(event, config, now=NOW)
    (batched,) = score([event], config, now=NOW)

    assert direct.score == batched.score
    assert direct.score_breakdown == batched.score_breakdown
