from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from structlog.testing import capture_logs

from digest.config import CategoryRules, Config, FiltersConfig, ScheduleConfig
from digest.models import Event, make_event_id
from digest.pipeline.categorize import score_category
from digest.pipeline.filter import content_filter as filter_events
from digest.pipeline.filter import exclude_already_sent

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


# --------------------------------------------------------------------------------------
# blocked_keywords: whole words by default, prefix on request (§5.2, §7.6)
# --------------------------------------------------------------------------------------


def test_a_block_does_not_delete_an_event_that_merely_starts_with_the_word() -> None:
    """The reason this call site does not share categorize's default. Both of these were
    excluded while blocked_keywords ran on prefix matching, and the second is an
    adults-only event -- the exact opposite of what a "gyerek" block asks for. A
    mislabelled event is visible in the digest; a deleted one is not."""
    config = Config(filters=FiltersConfig(blocked_keywords=["gyerek"]))
    adults_only = make_event(title="Gyerekzsivaj nélküli felnőtt est")
    reminiscing = make_event(title="Sub Focus", description="Gyerekkori álmom volt ez a koncert")

    kept = filter_events([adults_only, reminiscing], config, now=NOW)

    assert kept == [adults_only, reminiscing]


def test_the_whole_word_boundary_is_the_word_not_its_beginning() -> None:
    """The pair that pins what "whole word" means here. Hungarian writes compounds closed,
    so "gyerek" is a whole word in "Gyerek nap" and a prefix in "Gyerekprogram" -- the
    block fires on the first and not on the second. Anything that also excluded the second
    would be prefix matching again under another name."""
    config = Config(filters=FiltersConfig(blocked_keywords=["gyerek"]))
    stands_alone = make_event(title="Gyerek nap a ligetben")
    compound = make_event(title="Gyerekprogram a Városligetben")

    assert filter_events([stands_alone, compound], config, now=NOW) == [compound]


def test_a_trailing_star_asks_for_prefix_matching_and_gets_it() -> None:
    # The aggressive behaviour is still reachable, it just has to be requested. A reader
    # who really does mean "anything starting with gyerek" writes it out.
    config = Config(filters=FiltersConfig(blocked_keywords=["gyerek*"]))
    events = [
        make_event(title="Gyerek nap a ligetben"),
        make_event(title="Gyerekprogram a Városligetben"),
        make_event(title="Hétvégi gyerekprogramok a ligetben"),
    ]

    assert filter_events(events, config, now=NOW) == []


def test_a_longer_block_still_needs_the_star_for_its_inflected_forms() -> None:
    # What the revert costs, stated as a test rather than left to be discovered: the
    # plural no longer falls out of the singular by itself.
    inflected = make_event(title="Hétvégi gyerekprogramok a ligetben")
    bare = Config(filters=FiltersConfig(blocked_keywords=["gyerekprogram"]))
    starred = Config(filters=FiltersConfig(blocked_keywords=["gyerekprogram*"]))

    assert filter_events([inflected], bare, now=NOW) == [inflected]
    assert filter_events([inflected], starred, now=NOW) == []


def test_a_trailing_dollar_is_redundant_here_but_still_valid() -> None:
    """`$` asked for the behaviour that is now the default, so every existing profile that
    carries one keeps working — it must not silently become a literal that matches
    nothing."""
    config = Config(filters=FiltersConfig(blocked_keywords=["gyerekprogram$"]))
    exact = make_event(title="Gyerekprogram a Városligetben")
    inflected = make_event(title="Hétvégi gyerekprogramok a ligetben")

    assert filter_events([exact, inflected], config, now=NOW) == [inflected]


def test_a_categorization_keyword_still_matches_a_prefix() -> None:
    """The other side of the split, guarded here so a change to this call site's default
    cannot quietly travel to the shared matcher: §7.5 still matches suffixed forms."""
    rules = CategoryRules(keywords={"társasjáték": 3})

    assert score_category(make_event(title="Társasjátékos est"), rules).total == 3
    assert score_category(make_event(title="Társasjátékok"), rules).total == 3
    # And `$` there still opts out, unchanged.
    exact = CategoryRules(keywords={"társasjáték$": 3})
    assert score_category(make_event(title="Társasjátékos est"), exact).total == 0


def test_the_content_filter_knows_nothing_about_the_ledger() -> None:
    """WAS "an already sent event is excluded", when the ledger lived in this chain. It is
    a separate stage now, and this asserts the property that makes the site stable: the
    content filter gives the same answer whatever the reader has been sent."""
    event = make_event()

    assert filter_events([event], Config(), now=NOW) == [event]


def test_the_sent_ledger_is_its_own_stage() -> None:
    event = make_event()
    other = make_event(title="Másik")

    assert exclude_already_sent([event, other], frozenset()) == [event, other]
    assert exclude_already_sent([event, other], frozenset({event.id})) == [other]


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
