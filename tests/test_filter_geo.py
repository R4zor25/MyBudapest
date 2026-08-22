from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from digest.config import Config, FiltersConfig, GeoFilterConfig, HomeConfig
from digest.models import Event, RawEvent, make_event_id
from digest.pipeline.filter import GEO_REASONS, filter_with_reasons
from digest.pipeline.filter import filter as filter_events
from digest.pipeline.normalize import normalize

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
        "city": None,
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


def geo_config(**geo: Any) -> Config:
    return Config(filters=FiltersConfig(geo=GeoFilterConfig(**geo)))


# --------------------------------------------------------------------------------------
# The city cut
# --------------------------------------------------------------------------------------


def test_an_event_in_gyor_is_excluded_when_the_city_is_budapest() -> None:
    gyor = make_event(title="Elefánt // Bridge Garden", city="Győr")
    budapest = make_event(title="Sub Focus", city="Budapest")

    survivors = filter_events([gyor, budapest], geo_config(city="Budapest"), now=NOW)

    assert [e.title for e in survivors] == ["Sub Focus"]


def test_the_city_match_ignores_case_and_accents() -> None:
    # Sources spell it however they like; "GYOR" from an ASCII-folded feed is still Győr.
    config = geo_config(city="Győr")
    survivors = filter_events([make_event(city="gyor")], config, now=NOW)
    assert len(survivors) == 1


def test_an_event_with_no_city_is_kept_when_allow_missing_city_is_true() -> None:
    """The default, and it matters: most sources publish no settlement at all, so
    dropping these would quietly lose good events (§7.6)."""
    config = geo_config(city="Budapest", allow_missing_city=True)

    survivors = filter_events([make_event(city=None)], config, now=NOW)

    assert len(survivors) == 1


def test_an_event_with_no_city_is_dropped_when_allow_missing_city_is_false() -> None:
    config = geo_config(city="Budapest", allow_missing_city=False)

    outcome = filter_with_reasons([make_event(city=None)], config, now=NOW)

    assert outcome.events == []
    assert outcome.excluded["geo_city_missing"] == 1


def test_nothing_is_excluded_when_no_city_is_configured() -> None:
    """Neutral defaults: an absent `filters.geo` block must not filter anything, even
    with allow_missing_city switched off — there is no city to compare against."""
    events = [make_event(city="Győr"), make_event(title="B", city=None)]

    assert len(filter_events(events, Config(), now=NOW)) == 2
    assert len(filter_events(events, geo_config(allow_missing_city=False), now=NOW)) == 2


# --------------------------------------------------------------------------------------
# The distance cut — a hard exclusion, not the scoring penalty
# --------------------------------------------------------------------------------------


def test_max_distance_km_excludes_independently_of_the_scoring_penalty() -> None:
    """`filters.geo.max_distance_km` removes the event. `scoring.proximity` is untouched
    here — it is not even configured — which is the point: the two knobs share a name and
    do different jobs (§7.6)."""
    near = make_event(title="Közeli", distance_km=3.0)
    far = make_event(title="Távoli", distance_km=42.0)
    config = geo_config(max_distance_km=8)

    assert config.scoring.proximity is None
    outcome = filter_with_reasons([near, far], config, now=NOW)

    assert [e.title for e in outcome.events] == ["Közeli"]
    assert outcome.excluded["geo_too_far"] == 1


def test_the_scoring_proximity_bound_does_not_filter() -> None:
    """The mirror of the test above: setting only `scoring.proximity.max_distance_km`
    must exclude nothing, because that field bounds a penalty and never removes an event.
    Merging the two would silently turn a ranking preference into a hard cut."""
    from digest.config import ProximityConfig, ScoringConfig

    config = Config(scoring=ScoringConfig(proximity=ProximityConfig(max_distance_km=8)))

    assert len(filter_events([make_event(distance_km=42.0)], config, now=NOW)) == 1


def test_an_event_with_no_distance_is_kept() -> None:
    """`distance_km` is None unless `home` is set AND the source gave coordinates. Failing
    open here is the same rule as allow_missing_city."""
    config = geo_config(max_distance_km=1)

    assert len(filter_events([make_event(distance_km=None)], config, now=NOW)) == 1


def test_distance_exclusion_works_end_to_end_from_coordinates() -> None:
    """Guards the test above from proving nothing: with `home` configured and a source
    that publishes lat/lon, normalize produces a real distance_km and the cut bites."""
    config = Config(
        home=HomeConfig(district="XI.", lat=47.47, lon=19.05),
        filters=FiltersConfig(geo=GeoFilterConfig(max_distance_km=10)),
    )
    records = [
        RawEvent(
            source_id="cooltix",
            source_event_key="near",
            title="Belvárosi est",
            url="https://cooltix.hu/event/near",
            start_raw="2026-08-18T18:00:00+02:00",
            lat=47.4932,
            lon=19.0593,
        ),
        RawEvent(
            source_id="cooltix",
            source_event_key="far",
            title="Gödöllői est",
            url="https://cooltix.hu/event/far",
            start_raw="2026-08-18T18:00:00+02:00",
            lat=47.5964,
            lon=19.3560,
        ),
    ]
    events = normalize(records, config, now=NOW)
    assert [round(e.distance_km or 0) for e in events] == [3, 27]

    outcome = filter_with_reasons(events, config, now=NOW)

    assert [e.title for e in outcome.events] == ["Belvárosi est"]
    assert outcome.excluded["geo_too_far"] == 1


# --------------------------------------------------------------------------------------
# Logging and the run summary
# --------------------------------------------------------------------------------------


def test_every_geographic_exclusion_logs_its_reason_and_the_value() -> None:
    config = Config(
        filters=FiltersConfig(
            geo=GeoFilterConfig(city="Budapest", allow_missing_city=False, max_distance_km=8)
        )
    )
    events = [
        make_event(title="Győri", city="Győr"),
        make_event(title="Ismeretlen", city=None),
        make_event(title="Messzi", city="Budapest", distance_km=42.0),
    ]

    with capture_logs() as logs:
        filter_events(events, config, now=NOW)

    by_reason = {entry["reason"]: entry for entry in logs if entry["event"] == "filtered"}
    assert set(by_reason) == GEO_REASONS

    assert by_reason["geo_city_mismatch"]["city"] == "Győr"
    assert by_reason["geo_city_mismatch"]["expected"] == "Budapest"
    assert by_reason["geo_city_missing"]["city"] is None
    # Both the observed value and the threshold, so an over-aggressive filter can be
    # diagnosed from the run log alone.
    assert by_reason["geo_too_far"]["distance_km"] == 42.0
    assert by_reason["geo_too_far"]["max_distance_km"] == 8


def test_the_run_summary_reports_the_geographic_exclusion_count(tmp_path) -> None:
    from digest.cli import RunSummary

    config = Config(
        filters=FiltersConfig(geo=GeoFilterConfig(city="Budapest", allow_missing_city=False))
    )
    events = [
        make_event(title="Győri", city="Győr"),
        make_event(title="Ismeretlen", city=None),
        make_event(title="Pesti", city="Budapest"),
    ]

    outcome = filter_with_reasons(events, config, now=NOW)
    dropped_by_geo = sum(outcome.excluded[reason] for reason in GEO_REASONS)

    assert dropped_by_geo == 2
    summary = RunSummary(
        source_counts={},
        merged=0,
        dropped_by_filter=2,
        dropped_by_geo=dropped_by_geo,
        dropped_by_min_score=0,
        sent=1,
        drifted=[],
        seconds=0.0,
    )
    assert summary.dropped_by_geo == 2


def test_geo_is_a_subset_of_dropped_by_filter_not_an_addition() -> None:
    config = geo_config(city="Budapest")
    events = [make_event(title="Győri", city="Győr"), make_event(title="Pesti", city="Budapest")]

    outcome = filter_with_reasons(events, config, now=NOW)

    assert len(events) - len(outcome.events) == 1
    assert sum(outcome.excluded[reason] for reason in GEO_REASONS) == 1


def test_horizon_wins_attribution_over_geography() -> None:
    """`_exclusion_reason` returns the first match, so `dropped_by_geo` counts "excluded
    because of geography", not "was outside the city". Pinned so the summary is not read
    later as a census."""
    config = geo_config(city="Budapest")
    outside_and_late = make_event(city="Győr", start=NOW + timedelta(days=90))

    outcome = filter_with_reasons([outside_and_late], config, now=NOW)

    assert outcome.excluded["beyond_horizon"] == 1
    assert sum(outcome.excluded[reason] for reason in GEO_REASONS) == 0


# --------------------------------------------------------------------------------------
# Where Event.city comes from (§7.1)
# --------------------------------------------------------------------------------------


def raw(**overrides: Any) -> RawEvent:
    base: dict[str, Any] = {
        "source_id": "test",
        "source_event_key": "k",
        "title": "Esemény",
        "url": "https://example.hu/x",
        "start_raw": "2026-08-18T18:00:00+02:00",
    }
    return RawEvent(**{**base, **overrides})


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        # 1. What the source says wins outright.
        (raw(city="Szeged", postal_code="1053"), "Szeged"),
        # 2. A Budapest postal code, from the field or from the address text.
        (raw(postal_code="1053"), "Budapest"),
        (raw(address_raw="Reáltanoda utca 16, 1053 Budapest"), "Budapest"),
        # A four-digit code that is not Budapest's proves the event is elsewhere, but
        # naming the settlement would need a gazetteer -- so it stays unknown, not wrong.
        (raw(postal_code="9026"), None),
        # 3. The bare word, only when there is no readable postal code at all.
        (raw(address_raw="Budapest, Öböl utca 1."), "Budapest"),
        # ...and the reason that fallback sits behind the postal-code check:
        (raw(address_raw="9026 Győr, Budapest út 5."), None),
        (raw(), None),
    ],
)
def test_city_derivation_precedence(record: RawEvent, expected: str | None) -> None:
    config = Config()
    (event,) = normalize([record], config, now=NOW)
    assert event.city == expected


def test_a_source_city_is_not_overruled_by_a_budapest_postal_code() -> None:
    """Pinned before any source depends on it: a stated city is authoritative. A venue
    with a Budapest mailing address but a stated city elsewhere keeps the stated one."""
    (event,) = normalize([raw(city="Győr", postal_code="1117")], Config(), now=NOW)
    assert event.city == "Győr"


@pytest.mark.parametrize(
    "stated",
    ["Budapest", "Budapest XI.", "Budapest, XI. kerület", "budapest 1117", "BUDAPEST"],
)
def test_district_suffixed_spellings_still_count_as_budapest(stated: str) -> None:
    """§7.6 compares cities for exact equality, so an unnormalized "Budapest XI." would
    read as a different settlement and get a Budapest event EXCLUDED — a false negative
    that looks exactly like the filter working. The district is its own field."""
    (event,) = normalize([raw(city=stated)], Config(), now=NOW)
    assert event.city == "Budapest"

    config = geo_config(city="Budapest")
    assert len(filter_events([make_event(city=event.city)], config, now=NOW)) == 1


def test_a_city_merely_starting_with_those_letters_is_left_alone() -> None:
    (event,) = normalize([raw(city="Budapesti Agglomeráció")], Config(), now=NOW)
    assert event.city == "Budapesti Agglomeráció"


def test_other_cities_are_matched_exactly_and_not_canonicalized() -> None:
    """The contract for the batch that wires sources to `RawEvent.city`: outside Budapest
    the comparison is exact (after folding). A source emitting a decorated name for some
    other settlement will not match, and this is where that is discovered."""
    (event,) = normalize([raw(city="Győr-Ménfőcsanak")], Config(), now=NOW)
    assert event.city == "Győr-Ménfőcsanak"

    config = geo_config(city="Győr")
    assert filter_events([make_event(city=event.city)], config, now=NOW) == []
