from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from structlog.testing import capture_logs

from digest.config import load_config
from digest.errors import ConfigError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent, normalize_venue
from digest.pipeline.categorize import categorize
from digest.pipeline.normalize import normalize
from digest.sources.plugins import tarsasjatekos as tj
from digest.sources.registry import load_sources

# The instant every fixture in this module was saved. Nothing here reads the clock, so the
# asserted counts stay true in September (§14).
FIXTURE_DAY = datetime(2026, 8, 22, 10, 0, tzinfo=ZoneInfo("Europe/Budapest"))


def make_result(url: str, text: str = "", json_body: Any = None) -> FetchResult:
    return FetchResult(
        task=FetchTask(url=url), status=200, text=text, json=json_body, from_cache=False
    )


@pytest.fixture
def config(config_path: Path, sources_dir: Path):
    return load_config(config_path, sources_dir, None)


def source_by_id(config, source_id: str):
    return next(s for s in load_sources(config) if s.id == source_id)


def fixture_text(repo_root: Path, name: str) -> str:
    return (repo_root / "tests/fixtures" / name).read_text(encoding="utf-8")


def in_horizon(raw: list[RawEvent], config) -> list:
    """The number this batch is measured by: parsed records minus the past ones and the
    ones past the 14-day horizon (§7.1), not the raw parse count."""
    return normalize(raw, config, now=FIXTURE_DAY)


# --------------------------------------------------------------------------------------
# cooltix — §6.1 step 2, GraphQL. The one new source that carries volume.
# --------------------------------------------------------------------------------------


@pytest.fixture
def cooltix_payload(repo_root: Path) -> dict[str, Any]:
    return json.loads(fixture_text(repo_root, "cooltix_events.json"))


@pytest.fixture
def cooltix_events(config, cooltix_payload) -> list[RawEvent]:
    source = source_by_id(config, "cooltix")
    return list(
        source.parse(make_result("https://api.cooltix.com/graphql", json_body=cooltix_payload))
    )


def test_cooltix_keeps_only_dated_budapest_events(cooltix_payload, cooltix_events) -> None:
    nodes = [edge["node"] for edge in cooltix_payload["data"]["events"]["edges"]]
    assert len(nodes) == 500, "fixture should be one full page of 500 records"

    undated = [n for n in nodes if not n["startDate"]]
    assert len(undated) == 369, (
        "vouchers and permanent exhibitions sort first under orderBy: startDate_ASC -- "
        "this is what the page budget in sources/cooltix.yaml is sized for"
    )
    assert len(cooltix_events) == 83
    assert all(event.start_raw for event in cooltix_events)


def test_cooltix_drops_events_outside_budapest(cooltix_payload, cooltix_events) -> None:
    # The endpoint is countryCode: HU, not city-scoped, and no pipeline stage filters on
    # settlement -- so this cut has to happen in the source or not at all.
    nodes = [e["node"] for e in cooltix_payload["data"]["events"]["edges"]]
    elsewhere = {
        (n.get("venue") or {}).get("name")
        for n in nodes
        if n["startDate"] and (n.get("venue") or {}).get("address", {}).get("city") != "Budapest"
    }
    assert elsewhere, "fixture must contain non-Budapest events for this test to mean anything"
    # Every venue the source kept must be absent from the set of venues it should have cut.
    assert {e.venue_name for e in cooltix_events}.isdisjoint(elsewhere)


def test_cooltix_maps_the_board_game_record(cooltix_events) -> None:
    event = next(e for e in cooltix_events if "Játsszunk Haver" in e.title)
    assert event.url == "https://cooltix.hu/event/6a7cadd332153dde50aadc1c"
    assert event.venue_name == "Játsz/Ma Társasjáték Kávézó"
    assert event.start_raw == "2026-08-28T16:30:00.000Z"
    assert event.postal_code == "1053"
    # The district itself is derived by §7.1 from this postal code -- the source no
    # longer converts it (see normalize_district).
    assert event.district_raw is None
    assert event.lat == pytest.approx(47.4932103)
    assert event.lon == pytest.approx(19.0593045)
    # `summary`, not `description`: the latter is HTML and §7.1 does not strip tags.
    assert event.description and "<" not in event.description


def test_cooltix_lowercases_native_category(cooltix_events) -> None:
    # Cooltix spells them "Concert"/"Party"; config.yaml's native_types are lowercase and
    # §7.5 compares them as exact strings.
    natives = {e.native_category for e in cooltix_events if e.native_category}
    assert natives
    assert all(name == name.casefold() for name in natives)
    assert "concert" in natives


def test_cooltix_pagination_uses_the_cursor_from_the_previous_page(config, cooltix_payload) -> None:
    source = source_by_id(config, "cooltix")
    tasks = source.discover()

    first = next(tasks)
    assert first.method == "POST"
    assert "cursor" not in first.json_body["variables"]
    assert first.json_body["variables"]["count"] == 500

    list(source.parse(make_result(first.url, json_body=cooltix_payload)))
    second = next(tasks)
    expected = cooltix_payload["data"]["events"]["pageInfo"]["endCursor"]
    assert second.json_body["variables"]["cursor"] == expected


def test_cooltix_stops_paging_when_the_api_says_there_is_no_next_page(config) -> None:
    source = source_by_id(config, "cooltix")
    empty = {
        "data": {"events": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "edges": []}}
    }
    tasks = source.discover()
    first = next(tasks)
    list(source.parse(make_result(first.url, json_body=empty)))
    assert list(tasks) == []


def test_cooltix_raises_on_graphql_errors(config) -> None:
    from digest.errors import ParseError

    source = source_by_id(config, "cooltix")
    with pytest.raises(ParseError):
        list(
            source.parse(
                make_result(
                    "https://api.cooltix.com/graphql", json_body={"errors": [{"message": "boom"}]}
                )
            )
        )


# --------------------------------------------------------------------------------------
# tixa — §6.1 step 3, server-rendered JSON-LD
# --------------------------------------------------------------------------------------


@pytest.fixture
def tixa_events(config, repo_root: Path) -> list[RawEvent]:
    source = source_by_id(config, "tixa")
    html = fixture_text(repo_root, "tixa_jatszohazprojekt.html")
    return list(source.parse(make_result("https://www.tixa.hu/jatszohazprojekt", text=html)))


def test_tixa_requests_hungarian(config) -> None:
    # Without this header the whole listing comes back in English ("Board game night at
    # Treffort Kert") in an otherwise Hungarian newsletter.
    tasks = list(source_by_id(config, "tixa").discover())
    assert tasks
    assert all(t.headers["Accept-Language"].startswith("hu") for t in tasks)


def test_tixa_maps_the_board_game_record(tixa_events) -> None:
    event = next(e for e in tixa_events if "társasozás" in e.title)
    assert event.title == "Hétfői társasozás a Treffort Kertben // Játszóház Projekt"
    assert event.url == "https://www.tixa.hu/jhz260824"
    assert event.start_raw == "2026-08-24T16:50:00+02:00"
    assert event.venue_name == "Treffort Kert & Könyvtár Klub"
    assert event.postal_code == "1088"
    assert event.district_raw is None


def test_tixa_drops_midnight_placeholder_starts(config) -> None:
    """Tixa's startDate is `T00:00:00` whenever no time was recorded, and the real time
    exists only as Hungarian prose in `customDate` that §7.1 cannot read. Publishing the
    placeholder would claim the event starts at midnight, so the record is dropped."""
    source = source_by_id(config, "tixa")
    page = _tixa_page(
        {
            "@type": "Event",
            "name": "Placeholder start",
            "startDate": "2026-08-22T00:00:00+02:00",
            "customDate": "2026. augusztus 22. 19:00",
            "location": {
                "@type": "Place",
                "name": "Dürer Kert",
                "address": "1117 Budapest, Öböl utca 1.",
            },
            "url": "https://www.tixa.hu/placeholder",
        }
    )
    with capture_logs() as logs:
        events = list(source.parse(make_result("https://www.tixa.hu/durerkert", text=page)))
    assert events == []
    assert any(entry["event"] == "skipped_placeholder_start" for entry in logs)


def test_tixa_keeps_a_real_time_on_the_same_page_shape(config) -> None:
    source = source_by_id(config, "tixa")
    page = _tixa_page(
        {
            "@type": "Event",
            "name": "Real start",
            "startDate": "2026-08-23T17:30:00+02:00",
            "location": {
                "@type": "Place",
                "name": "Dürer Kert",
                "address": "1117 Budapest, Öböl utca 1.",
            },
            "url": "https://www.tixa.hu/real",
        }
    )
    events = list(source.parse(make_result("https://www.tixa.hu/durerkert", text=page)))
    assert [e.start_raw for e in events] == ["2026-08-23T17:30:00+02:00"]


def _tixa_page(item: dict[str, Any]) -> str:
    """The two placeholder tests need a page shape the real fixture does not contain (a
    midnight record). Everything asserted about real data uses the saved fixture."""
    block = {
        "@context": "http://schema.org",
        "@type": "ItemList",
        "@id": "locationEvents",
        "itemListElement": [{"@type": "ListItem", "position": 1, "item": item}],
    }
    return (
        f'<html><body><script type="application/ld+json">{json.dumps(block)}</script></body></html>'
    )


# --------------------------------------------------------------------------------------
# tokenklub — §6.1 step 2, The Events Calendar REST API, declarative
# --------------------------------------------------------------------------------------


def test_tokenklub_parses_the_saved_api_response(config, repo_root: Path) -> None:
    source = source_by_id(config, "tokenklub")
    payload = json.loads(fixture_text(repo_root, "tokenklub_events.json"))
    events = list(
        source.parse(
            make_result("https://tokenklub.hu/wp-json/tribe/events/v1/events", json_body=payload)
        )
    )

    assert len(events) == 18
    first = events[0]
    assert first.title == "TOKEN Társasjáték Klub (március 28.)"
    assert first.start_raw == "2025-03-28 17:00:00"
    assert first.venue_name == "Tomory Lajos Múzeum"
    assert first.postal_code == "1181"
    # The declarative engine maps fields, it does not derive: §7.1's _district turns the
    # postal code into the roman district downstream.
    assert first.district_raw is None
    assert first.url.startswith("https://tokenklub.hu/esemenynaptar/")
    # Deliberately unmapped: the API returns it as raw HTML and §7.1 keeps tags.
    assert all(e.description is None for e in events)


def test_tokenklub_upcoming_is_honestly_empty(config, repo_root: Path) -> None:
    """The club is seasonal. The endpoint defaults to start_date = today, so an empty
    array means "nothing scheduled yet", not a selector that stopped matching -- which is
    why this ships enabled rather than as a placeholder."""
    source = source_by_id(config, "tokenklub")
    payload = json.loads(fixture_text(repo_root, "tokenklub_upcoming.json"))
    assert payload["total"] == 0
    assert (
        list(
            source.parse(
                make_result(
                    "https://tokenklub.hu/wp-json/tribe/events/v1/events", json_body=payload
                )
            )
        )
        == []
    )


# --------------------------------------------------------------------------------------
# tarsasjatekos — §6.1 step 1, Google Calendar API. Disabled until GCAL_API_KEY exists.
# --------------------------------------------------------------------------------------


def test_tarsasjatekos_is_disabled_and_says_why(sources_dir: Path) -> None:
    spec = yaml.safe_load((sources_dir / "tarsasjatekos.yaml").read_text(encoding="utf-8"))
    assert spec["enabled"] is False
    assert spec["listing"]["urls"] == [
        (
            "https://www.googleapis.com/calendar/v3/calendars/"
            "klubokklubja_esemenyek%40tarsasjatekos.hu/events"
        )
    ]


def test_tarsasjatekos_without_an_api_key_fails_loudly(config, monkeypatch) -> None:
    # A source that cannot authenticate must not quietly contribute zero -- that is the
    # silent-zero mode AUDIT-5 caught on port-hu.
    monkeypatch.delenv("GCAL_API_KEY", raising=False)
    source = source_by_id(config, "tarsasjatekos")
    with pytest.raises(ConfigError, match="GCAL_API_KEY"):
        list(source.discover())


def test_tarsasjatekos_requests_only_the_horizon(config, monkeypatch) -> None:
    monkeypatch.setenv("GCAL_API_KEY", "test-key")
    task = next(iter(source_by_id(config, "tarsasjatekos").discover()))
    assert task.params["key"] == "test-key"
    # singleEvents expands recurrence server-side, which is why this plugin has no RRULE
    # handling of its own; orderBy=startTime is only legal alongside it.
    assert task.params["singleEvents"] == "true"
    assert task.params["orderBy"] == "startTime"
    assert task.params["timeMin"] < task.params["timeMax"]


def test_tarsasjatekos_strips_the_google_meet_footer() -> None:
    raw = (
        "További információ a Csepeli Társasjáték Klub Facebook csoportban!<br>"
        '<a class="moz-txt-link-freetext" href="https://fb.com/g">https://fb.com/g</a>\n\n'
        "Csatlakozás a Google Meet szolgáltatással: https://meet.google.com/izd-tecf-hxj\n"
        "Vagy hívja a következő számot: (HU) +36 21 262 4728 PIN-kód: 594632195#"
    )
    cleaned = tj._description(raw)
    assert cleaned is not None
    assert "meet.google.com" not in cleaned
    assert "PIN" not in cleaned
    assert "<" not in cleaned
    assert cleaned.startswith("További információ")


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Board Game Café - Budapest", "Board Game Café"),
        ("Csepeli napközis tábor - Budapest, Hollandi út 18, 1213", "Csepeli napközis tábor"),
        ("Csörsz u. 18, Budapest, Csörsz u. 18, 1124 Hungary", "Csörsz u. 18"),
    ],
)
def test_tarsasjatekos_venue_name_drops_the_repeated_city(location: str, expected: str) -> None:
    # venue_prior is exact equality after normalize_venue (§7.5), so "Board Game Café -
    # Budapest" would never match the configured "Board Game Café".
    assert tj._venue_name(location) == expected


@pytest.mark.parametrize(
    ("location", "is_budapest"),
    [
        ("Board Game Café - Budapest", True),
        ("Csörsz u. 18, Budapest, Csörsz u. 18, 1124 Hungary", True),
        ("Sport u. 4, Szigethalom, Sport u. 4, 2315 Hungary", False),
        ("Veszprém Aulich Lajos utca 3", False),
    ],
)
def test_tarsasjatekos_budapest_detection(location: str, is_budapest: bool) -> None:
    # The calendar is national: Biatorbágy, Veszprém, Poroszló and Szigethalom all appear.
    assert tj._is_budapest(location, tj._postal_code(location)) is is_budapest


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        ({"dateTime": "2026-08-29T12:00:00+02:00"}, "2026-08-29T12:00:00+02:00"),
        # All-day: deliberately unsupported. "2026-08-29" parses to 00:00 local, which
        # night_shift.before_hour then files under 2026-08-28 -- the day-shift defect
        # commit 499bfc4 was written about. Skipped and logged instead.
        ({"date": "2026-08-29"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_tarsasjatekos_reads_only_the_timed_shape(slot: Any, expected: str | None) -> None:
    assert tj._timestamp(slot) == expected


def test_tarsasjatekos_skips_all_day_events_loudly(config, monkeypatch) -> None:
    monkeypatch.setenv("GCAL_API_KEY", "test-key")
    source = source_by_id(config, "tarsasjatekos")
    payload = {
        "items": [
            {
                "id": "allday",
                "status": "confirmed",
                "summary": "Egész napos klubnap",
                "location": "Board Game Café - Budapest",
                "htmlLink": "https://www.google.com/calendar/event?eid=allday",
                "start": {"date": "2026-08-29"},
                "end": {"date": "2026-08-30"},
            }
        ]
    }
    with capture_logs() as logs:
        events = list(source.parse(make_result("https://www.googleapis.com/x", json_body=payload)))
    assert events == []
    assert any("all-day" in str(entry.get("reason", "")) for entry in logs)


def test_tarsasjatekos_asserts_the_board_game_topic_itself(config) -> None:
    """The calendar is the association's own club listing, so every record is a board-game
    session. That matters because its titles are often bare game names ("Visszapillantó
    2010: Dixit") that no keyword can catch -- native_types is the only signal that reaches
    them, and it is worth 4.0 on its own (§7.5)."""
    monkey_free_raw = RawEvent(
        source_id="tarsasjatekos",
        source_event_key="k",
        title="Visszapillantó 2010: Dixit",
        url="https://www.google.com/calendar/event?eid=x",
        start_raw="2026-08-25T18:00:00+02:00",
        venue_name="Board Game Café",
        native_category="boardgame",
    )
    (event,) = categorize(in_horizon([monkey_free_raw], config), config)
    assert event.native_categories == ["boardgame"]
    assert event.categories[0] == "tarsasjatek"


def test_disabled_sources_never_reach_discover(config) -> None:
    """tarsasjatekos raises without its key, so it must not be entered at all while
    disabled -- cli.py's run loop skips on `enabled` before calling discover()."""
    source = source_by_id(config, "tarsasjatekos")
    assert source.enabled is False


# --------------------------------------------------------------------------------------
# redandblack — second attempt on the root path (the brief's retry). Still dead.
# --------------------------------------------------------------------------------------


def test_redandblack_root_event_page_carries_no_date(repo_root: Path) -> None:
    """The brief was right that event pages exist outside /programok. They have no date in
    any machine-readable form, and none in prose either -- only a clock time and a weekday
    word. That is §6.1 step 5, and this batch's explicit stop condition."""
    html = fixture_text(repo_root, "redandblack_event_page.html")
    assert "Társas-ismerkedő est" in html
    assert "<time" not in html
    assert "datetime=" not in html
    assert "application/ld+json" not in html
    assert "Kezdés:" in html
    assert not re.search(r"\b20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2}\b", html)


def test_redandblack_sitemap_is_frozen_in_2023(repo_root: Path) -> None:
    xml = fixture_text(repo_root, "redandblack_sitemap.xml")
    lastmods = set(re.findall(r"<lastmod>(.*?)</lastmod>", xml))
    assert lastmods == {"2023-11-08T13:30:25+00:00"}
    # The sitemap does not even list the event pages -- the root URLs it carries are game
    # descriptions.
    assert "/tarsas-ismerkedo-est" not in xml


def test_redandblack_active_programme_list_is_still_empty(repo_root: Path) -> None:
    html = fixture_text(repo_root, "redandblack_programok.html")
    assert "programs__active" in html
    active = re.search(r"programs__active.*?</div>", html, re.DOTALL)
    assert active is not None
    assert "card" not in active.group(0)


# --------------------------------------------------------------------------------------
# tarsasjatekos.hu/klubok.html — why the directory page itself is not the source
# --------------------------------------------------------------------------------------


def test_klubok_page_states_recurrence_as_prose_not_dates(repo_root: Path) -> None:
    html = fixture_text(repo_root, "tarsasjatekos_klubok.html")
    assert "<time" not in html
    assert "application/ld+json" not in html
    # What it publishes instead of dates:
    for prose in ("minden hónap", "szombatján", "eseménynaptár", "Időpontok a Facebookon"):
        assert prose in html
    # And the one dated form it does have is the embedded calendar this batch reads.
    assert "calendar.google.com/calendar/embed" in html


# --------------------------------------------------------------------------------------
# The category itself — the reason batch A's sources would have reported zero anyway
# --------------------------------------------------------------------------------------


def test_venue_prior_strings_match_what_sources_actually_emit(config, cooltix_events) -> None:
    """venue_prior is exact equality after normalize_venue, never substring (§7.5). The
    previous entries ("Red & Black", "Játsz/Ma") were short-hand names no source emits, so
    neither could ever fire. This pins them to real fixture output."""
    priors = {normalize_venue(v) for v in config.categories["tarsasjatek"].venue_prior}
    emitted = {normalize_venue(e.venue_name) for e in cooltix_events}
    assert normalize_venue("Játsz/Ma Társasjáték Kávézó") in priors & emitted


def test_board_game_events_reach_the_tarsasjatek_category(
    config, cooltix_events, tixa_events
) -> None:
    """The number that batch existed to move: before it, every one of these landed in
    `egyeb`. It was fixed twice -- first by listing the suffixed forms as extra keywords,
    then properly in package 16, which made contains_word match a word PREFIX so the base
    form covers "Társasjátékos kaland" on its own. The extra keywords are gone; this still
    passes, which is the point."""
    raw = [e for e in cooltix_events + tixa_events]
    events = categorize(in_horizon(raw, config), config)
    board_games = [e for e in events if "tarsasjatek" in e.categories]
    titles = {e.title for e in board_games}
    assert "Játsszunk Haver - a Játsz/mában" in titles
    assert "Hétfői társasozás a Treffort Kertben // Játszóház Projekt" in titles


@pytest.mark.parametrize(
    "title",
    [
        "Bunny Kingdom - Társasjátékos kaland",
        "Matchy Matchy és Palackposta - Játékbemutató",
        "Társasjátékok Éjszakája a TOKEN Klubban",
        "TOKEN Társasjáték Klub (március 28.)",
        "Hétfői társasozás a Treffort Kertben // Játszóház Projekt",
    ],
)
def test_hungarian_suffixed_forms_score_as_board_games(config, title: str) -> None:
    from digest.pipeline.categorize import score_category

    score = score_category(_bare_event(title, config), config.categories["tarsasjatek"])
    assert score.total >= config.min_category_score, f"{title!r} scored {score.total}"


def _bare_event(title: str, config):
    from digest.models import Event, make_event_id

    start = FIXTURE_DAY
    return Event(
        id=make_event_id(title, start, None),
        source_ids=["test"],
        urls=["https://example.hu/x"],
        title=title,
        description=None,
        start=start,
        end=None,
        effective_date=start.date(),
        venue_name=None,
        district=None,
        lat=None,
        lon=None,
        distance_km=None,
        price_min=None,
        price_max=None,
        is_free=False,
        categories=[],
        native_categories=[],
        image_url=None,
        score=0.0,
        score_reasons={},
        group_size=1,
        members=[],
    )
