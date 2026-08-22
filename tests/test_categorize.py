from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from digest.config import CategoryRules, Config, load_config
from digest.llm.base import Categorizer
from digest.models import make_event_id
from digest.pipeline.categorize import RuleCategorizer, categorize, explain_event, score_category

BUDAPEST = ZoneInfo("Europe/Budapest")
START = datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST)


def make_event(**overrides: Any):
    from digest.models import Event

    title = overrides.pop("title", "Sub Focus")
    start = overrides.pop("start", START)
    venue_name = overrides.pop("venue_name", None)
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        # A neutral path: it must not coincide with any category's url_patterns
        # ("/zene/", "/koncert/", ...), or every test would silently pick up that signal.
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


@pytest.fixture
def config(config_path: Path, sources_dir: Path) -> Config:
    # The real config.yaml's category rules, exactly what production scores against.
    return load_config(config_path, sources_dir, None)


def test_native_type_concert_wins(config: Config) -> None:
    event = make_event(title="Esti program", native_categories=["concert"])

    (result,) = categorize([event], config)

    assert result.categories[0] == "koncert"


def test_keyword_and_venue_prior_both_contribute(config: Config) -> None:
    # The venue name has to be the exact string a source emits: venue_prior is compared
    # with normalize_venue(a) == normalize_venue(b), never as a substring. This used to say
    # "Red & Black", a short-hand no source publishes, so the assertion below passed against
    # a config value that could never fire on real data (package 15).
    event = make_event(
        title="Társasjáték est a Red & Blackben",
        venue_name="Red&Black Társasjátékszalon",
    )

    scores = explain_event(event, config)
    tarsasjatek = scores["tarsasjatek"]

    assert any(signal.startswith("keyword:") for signal in tarsasjatek.signals)
    assert "venue_prior" in tarsasjatek.signals

    (result,) = categorize([event], config)
    assert result.categories[0] == "tarsasjatek"


def test_two_categories_above_threshold_both_appear_highest_first(config: Config) -> None:
    event = make_event(title="Kvízest és borkóstoló a klubban")

    (result,) = categorize([event], config)

    assert result.categories == ["kviz", "gasztro"]


def test_a_neutral_title_falls_back(config: Config) -> None:
    event = make_event(title="Csendes délután a parkban", description="Semmi különös.")

    (result,) = categorize([event], config)

    assert result.categories == [config.fallback_category]
    assert result.categories == ["egyeb"]


def test_accent_insensitive_matching(config: Config) -> None:
    accented = make_event(title="Társasjáték klub")
    plain = make_event(title="Tarsasjatek klub")

    accented_result, plain_result = categorize([accented, plain], config)

    assert accented_result.categories[0] == "tarsasjatek"
    assert plain_result.categories[0] == "tarsasjatek"


def test_keyword_does_not_match_in_the_middle_of_a_word() -> None:
    """Prefix matching anchors at the start of a word, so a keyword buried inside one
    still does not fire."""
    rules = CategoryRules(keywords={"koncert": 3})
    buried = make_event(title="Szimfonikuskoncert-bérlet")
    standalone = make_event(title="Ma este koncert")

    assert score_category(buried, rules).total == 0
    assert score_category(standalone, rules).total == 3


def test_a_leading_compound_does_match_and_this_is_the_known_trade_off() -> None:
    """The limitation prefix matching cannot dodge: Hungarian writes compounds closed, so
    "koncertterem" (concert HALL, a venue-rental listing) is indistinguishable from
    "koncertje" (his concert, an inflection) by shape alone. Both start at a word boundary
    and continue with word characters. Matching the inflections is worth far more than the
    occasional compound costs -- package 16 measured 12 new matches, 11 of them correct --
    and `$` is the escape hatch where a specific stem proves otherwise."""
    rules = CategoryRules(keywords={"koncert": 3})
    compound = make_event(title="Koncertterem bérlés")

    assert score_category(compound, rules).total == 3
    assert score_category(compound, CategoryRules(keywords={"koncert$": 3})).total == 0


@pytest.mark.parametrize(
    ("title", "keyword"),
    [
        # The case that started this: Hungarian attaches suffixes, so the base form has to
        # carry the declensions or every category silently under-matches.
        ("Társasjátékos est", "társasjáték"),
        ("koncertek a kertben", "koncert"),
        ("kiállításon jártunk", "kiállítás"),
        # Real surface forms from the fixtures, package 16 Part A.
        ("A csapat hazai mérkőzése", "mérkőzés"),
        ("ZSAZSA – Létay Dóra előadásában", "előadás"),
        ("Társasjátékok Éjszakája", "társasjáték"),
    ],
)
def test_a_keyword_matches_its_suffixed_forms(title: str, keyword: str) -> None:
    assert score_category(make_event(title=title), CategoryRules(keywords={keyword: 3})).total == 3


def test_a_short_keyword_keeps_whole_word_matching() -> None:
    """Under five characters the stem is a common beginning of unrelated words, so prefix
    matching would cost more than it gains: "rave" would fire on "ravasz"."""
    rules = CategoryRules(keywords={"rave": 3})

    assert score_category(make_event(title="Ravasz terv"), rules).total == 0
    assert score_category(make_event(title="rave a kertben"), rules).total == 3


def test_a_trailing_dollar_forces_whole_word_matching() -> None:
    """The opt-out for a stem that over-matches. "party" is the measured one: it should
    fire on "partysorozat" but not on "partyjátékokat" in a board-game description."""
    prefix = CategoryRules(keywords={"party": 3})
    exact = CategoryRules(keywords={"party$": 3})
    inflected = make_event(title="Kommunikációs, partyjátékokat játsszunk")

    assert score_category(inflected, prefix).total == 3
    assert score_category(inflected, exact).total == 0
    # The exact form still matches, so `$` narrows rather than disables.
    assert score_category(make_event(title="party a kertben"), exact).total == 3


def test_matching_stays_accent_and_case_insensitive_under_prefix_rules() -> None:
    rules = CategoryRules(keywords={"kiállítás": 3})

    assert score_category(make_event(title="KIALLITASON jartunk"), rules).total == 3


def test_keyword_matches_a_multi_word_phrase() -> None:
    rules = CategoryRules(keywords={"élő zene": 3})
    event = make_event(title="Ma este élő zene lesz")

    score = score_category(event, rules)

    assert score.total == 3
    assert score.signals == {"keyword:élő zene": 3}


def test_url_pattern_signal() -> None:
    rules = CategoryRules(url_patterns=["/koncert/"])
    matching = make_event(urls=["https://port.hu/koncert/sub-focus"])
    not_matching = make_event(urls=["https://port.hu/klub/sub-focus"])

    assert score_category(matching, rules).total > 0
    assert score_category(not_matching, rules).total == 0


def test_native_type_signal_is_the_strongest_single_signal() -> None:
    native_rules = CategoryRules(native_types=["concert"])
    keyword_rules = CategoryRules(keywords={"koncert": 3})
    event = make_event(title="X", native_categories=["concert"])

    native_score = score_category(event, native_rules).total
    keyword_score = score_category(make_event(title="koncert"), keyword_rules).total

    assert native_score > keyword_score


def test_venue_prior_matches_regardless_of_accents_and_case() -> None:
    rules = CategoryRules(venue_prior={"A38 Hajó": 2})
    event = make_event(venue_name="a38   hajo")

    assert score_category(event, rules).signals == {"venue_prior": 2}


def test_missing_venue_never_scores_venue_prior() -> None:
    rules = CategoryRules(venue_prior={"A38 Hajó": 2})
    event = make_event(venue_name=None)

    assert "venue_prior" not in score_category(event, rules).signals


def test_categories_below_threshold_do_not_appear() -> None:
    config = Config(
        categories={"koncert": CategoryRules(keywords={"koncert": 1})},
        min_category_score=2,
    )
    event = make_event(title="koncert")

    (result,) = categorize([event], config)

    assert result.categories == [config.fallback_category]


def test_a_tie_is_broken_by_config_order() -> None:
    config = Config(
        categories={
            "first": CategoryRules(keywords={"buli": 3}),
            "second": CategoryRules(keywords={"parti": 3}),
        },
        min_category_score=1,
    )
    event = make_event(title="Buli és parti egyszerre")

    (result,) = categorize([event], config)

    assert result.categories == ["first", "second"]


def test_categorize_does_not_mutate_its_input(config: Config) -> None:
    event = make_event(title="Kvízest", categories=[])

    categorize([event], config)

    assert event.categories == []


def test_rule_categorizer_satisfies_the_protocol(config: Config) -> None:
    categorizer: Categorizer = RuleCategorizer()
    event = make_event(title="Esti program", native_categories=["concert"])

    (result,) = categorizer.categorize([event], config)

    assert result.categories[0] == "koncert"


def test_score_breakdown_credits_every_matched_keyword() -> None:
    rules = CategoryRules(keywords={"kvíz": 4, "kvízest": 4})
    event = make_event(title="Nagy kvízest ma este")

    signals = score_category(event, rules).signals

    assert signals == {"keyword:kvízest": 4}  # "kvíz" alone does not appear as a word


@pytest.mark.parametrize("field_name", ["title", "description"])
def test_keywords_are_matched_in_both_title_and_description(field_name: str) -> None:
    rules = CategoryRules(keywords={"koncert": 3})
    kwargs = {field_name: "Ma este koncert lesz"}
    if field_name == "title":
        kwargs.setdefault("description", None)
    else:
        kwargs.setdefault("title", "Semleges cím")
    event = make_event(**kwargs)

    assert score_category(event, rules).total == 3
