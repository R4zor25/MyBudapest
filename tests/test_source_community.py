from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from selectolax.parser import HTMLParser
from structlog.testing import capture_logs

from digest.config import load_config
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent
from digest.pipeline.categorize import categorize
from digest.pipeline.normalize import normalize
from digest.sources.plugins.kvizestek import KvizestekSource, combine_start
from digest.sources.registry import load_sources

# The instant the three fixtures were saved. Everything date-dependent is pinned to it so
# the suite does not start failing in September (§14: no test depends on the clock).
FIXTURE_DAY = datetime(2026, 8, 22, 10, 0, tzinfo=ZoneInfo("Europe/Budapest"))


def make_result(url: str, text: str = "", json_body: Any = None) -> FetchResult:
    return FetchResult(
        task=FetchTask(url=url), status=200, text=text, json=json_body, from_cache=False
    )


@pytest.fixture
def kvizestek_payload(repo_root: Path) -> list[dict[str, Any]]:
    path = repo_root / "tests/fixtures/kvizestek_upcoming.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def kvizestek_source(config_path: Path, sources_dir: Path) -> KvizestekSource:
    config = load_config(config_path, sources_dir, None)
    source = next(s for s in load_sources(config) if s.id == "kvizestek")
    assert isinstance(source, KvizestekSource)
    return source


def parse_fixture(source: KvizestekSource, payload: list[dict[str, Any]]) -> list[RawEvent]:
    return list(
        source.parse(
            make_result("https://foglalas.kvizestek.hu/api/events/upcoming", json_body=payload)
        )
    )


# --------------------------------------------------------------------------------------
# kvizestek — the one source of the three that landed on a usable tier (§6.1 step 2)
# --------------------------------------------------------------------------------------


def test_the_yaml_points_at_the_booking_backend_not_the_dead_listing_page(
    sources_dir: Path,
) -> None:
    # kvizestek.hu/esemenyek (the URL in the batch brief) now only links onward to the
    # booking site; parsing it would produce a permanently-empty source. If someone
    # "fixes" this back to the brief's URL, this is where it fails.
    spec = yaml.safe_load((sources_dir / "kvizestek.yaml").read_text(encoding="utf-8"))

    assert spec["listing"]["urls"] == ["https://foglalas.kvizestek.hu/api/events/upcoming"]
    assert spec["rate_limit_seconds"] == 3
    assert spec["plugin"] == "kvizestek"


def test_discover_yields_one_task_because_the_endpoint_is_not_paginated(
    kvizestek_source: KvizestekSource,
) -> None:
    tasks = list(kvizestek_source.discover())

    assert [task.url for task in tasks] == ["https://foglalas.kvizestek.hu/api/events/upcoming"]
    assert tasks[0].params is None


def test_the_saved_fixture_carries_no_host_pii(repo_root: Path) -> None:
    # The live payload exposes quizmasters' email addresses and a 64-hex confirmation
    # token. The fixture is committed to a public repo, so it must not — and this test is
    # what stops a future re-save from quietly putting them back.
    blob = (repo_root / "tests/fixtures/kvizestek_upcoming.json").read_text(encoding="utf-8")
    records = json.loads(blob)

    assert re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", blob) is None
    for key in ("hostName", "hostEmail", "hostId", "host2Name", "host2Email", "host2Id"):
        assert {record[key] for record in records} == {None}, key
    assert {record["hostConfirmationToken"] for record in records} == {None}


def test_the_fixture_still_has_the_shape_the_plugin_was_written_against(
    kvizestek_payload: list[dict[str, Any]],
) -> None:
    assert len(kvizestek_payload) == 132
    # Recombining these two is the plugin's whole reason to exist — if the API ever grows
    # a real timestamp field, this is the assertion that should be revisited.
    assert {record["eventDate"][10:] for record in kvizestek_payload} == {"T12:00:00.000Z"}
    assert all(record["eventTime"] for record in kvizestek_payload)


def test_parse_keeps_budapest_and_drops_the_rest_of_the_country(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    # 132 nationwide in, 91 Budapest out, 41 dropped. The endpoint is "kvízestek
    # országszerte"; §7.6 is the authoritative geographic rule and keeps this cut too, so
    # that 41 records are never carried past the parser.
    with capture_logs() as logs:
        events = parse_fixture(kvizestek_source, kvizestek_payload)

    assert len(events) == 91
    skipped = [entry for entry in logs if entry["event"] == "skipped_outside_budapest"]
    assert len(skipped) == 41
    assert "Győr" in {entry["city"] for entry in skipped}
    # The one non-Hungarian venue in the sample must not survive on its address either.
    assert "Dunaszerdahely" in {entry["city"] for entry in skipped}


def test_a_budapest_event_with_a_blank_city_is_recovered_from_its_address(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    # Both Margit-sziget records carry `venueCity: ""` and
    # `venueAddress: "1138 Budapest, Perzsa varázsfa"`. Trusting venueCity alone would
    # silently drop two real Budapest quiz nights.
    blank_city = [r for r in kvizestek_payload if not (r["venueCity"] or "").strip()]
    assert len(blank_city) == 2

    events = parse_fixture(kvizestek_source, blank_city)

    assert len(events) == 2
    assert {event.title for event in events} == {"Szabadtéri kvíz a Margit-szigeten"}
    assert {event.postal_code for event in events} == {"1138"}
    # A blank stays None rather than being recovered into `city` here. §7.1 owns the
    # derivation and its step 2 reads the same postal code -- doing it twice would be two
    # implementations to keep in step.
    assert {event.city for event in events} == {None}


def test_venue_city_is_mapped_onto_the_field_the_geo_stage_reads(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    """`venueCity` is §7.1's first-choice input for `Event.city`, which §7.6 filters on.

    As with cooltix there is no "a Győr record maps to Győr" half: the cut above runs
    first, so the only settlement that survives into a RawEvent is Budapest. What the
    mapping buys is the two blank-city records staying honestly unknown at the source and
    being resolved by §7.1 instead -- asserted below on the normalized events."""
    events = parse_fixture(kvizestek_source, kvizestek_payload)

    assert Counter(e.city for e in events) == {"Budapest": 89, None: 2}


def test_normalize_fills_the_blank_city_from_the_postal_code(
    kvizestek_source: KvizestekSource,
    kvizestek_payload: list[dict[str, Any]],
    config_path: Path,
    sources_dir: Path,
) -> None:
    # §7.1's three steps in order: the two Margit-sziget records state no settlement, so
    # step 2 reads 1138 and answers Budapest. The source does not do this itself.
    raw = parse_fixture(kvizestek_source, kvizestek_payload)
    config = load_config(config_path, sources_dir, None)

    events = normalize(raw, config, now=FIXTURE_DAY)

    assert events
    assert {event.city for event in events} == {"Budapest"}


def test_a_stated_non_budapest_city_is_never_re_read_out_of_its_address(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    # The address fallback must not become a general "does the string mention Budapest"
    # rule: a record that names another city keeps that answer.
    record = dict(next(r for r in kvizestek_payload if r["venueCity"] == "Győr"))
    record["venueAddress"] = "9021 Győr, Budapest út 1."

    assert parse_fixture(kvizestek_source, [record]) == []


def test_start_is_the_date_marker_recombined_with_the_separate_clock_time() -> None:
    # Noon-UTC marker + "19:00" -> 19:00 local, not 14:00 local.
    assert combine_start("2026-08-22T12:00:00.000Z", "19:00") == "2026-08-22 19:00:00"
    assert combine_start("2026-08-22T12:00:00.000Z", "18:00") == "2026-08-22 18:00:00"
    assert combine_start("2026-08-22T12:00:00.000Z", "9:30") == "2026-08-22 09:30:00"


@pytest.mark.parametrize(
    ("event_date", "event_time"),
    [
        (None, "19:00"),
        ("2026-08-22T12:00:00.000Z", None),
        ("2026-08-22T12:00:00.000Z", ""),
        ("2026-08-22T12:00:00.000Z", "este"),
        ("2026-08-22T12:00:00.000Z", "25:00"),
        ("not-a-date", "19:00"),
    ],
)
def test_an_unusable_date_or_time_yields_no_start(
    event_date: str | None, event_time: str | None
) -> None:
    assert combine_start(event_date, event_time) is None


def test_a_record_with_an_unusable_time_is_skipped_and_its_siblings_survive(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    broken = dict(next(r for r in kvizestek_payload if r["venueCity"] == "Budapest"))
    broken["eventTime"] = None
    payload = [broken, *(r for r in kvizestek_payload if r["venueCity"] == "Budapest")]

    with capture_logs() as logs:
        events = parse_fixture(kvizestek_source, payload)

    assert len(events) == 89
    reasons = [entry for entry in logs if entry["event"] == "record_skipped"]
    assert len(reasons) == 1
    assert "eventDate/eventTime" in reasons[0]["reason"]


def test_a_cancelled_event_never_becomes_a_raw_event(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    # No record in the fixture is cancelled, so the flag is exercised on a copy — the
    # field is in the schema and a cancelled quiz night must not go out by email.
    cancelled = dict(next(r for r in kvizestek_payload if r["venueCity"] == "Budapest"))
    cancelled["isCancelled"] = True

    with capture_logs() as logs:
        events = parse_fixture(kvizestek_source, [cancelled])

    assert events == []
    assert [entry["event"] for entry in logs] == ["skipped_cancelled"]


def test_field_mapping_on_a_real_record(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    events = parse_fixture(kvizestek_source, kvizestek_payload)
    event = next(e for e in events if e.title == "Magyarország kvíz")

    assert event.source_id == "kvizestek"
    # The API's own UUID, not the URL: it survives a slug rename.
    assert event.source_event_key == "998da2ab-e90e-4a4d-9156-330d871486ff"
    assert event.url == (
        "https://foglalas.kvizestek.hu/esemenyek/magyarorszag-kviz-comics-bar-2026-08-22"
    )
    assert event.start_raw == "2026-08-22 19:00:00"
    assert event.venue_name == "Comics Bar"
    assert event.address_raw == "1077 Budapest, Wesselényi utca 19."
    assert event.postal_code == "1077"
    assert event.district_raw is None
    # "[Excel import – 2026-07-15]" is an internal provenance note, not a description.
    assert event.description is None
    # entryFee is null here, and an unpriced record must not read as free (§7.7).
    assert event.price_raw is None


def test_almost_no_budapest_event_publishes_a_price(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    # A reported finding, pinned. Only 10 of 132 records carry an `entryFee` at all, and
    # just 2 of the 91 Budapest ones do — so `free_bonus` and `cheap_bonus` (§7.7) are
    # effectively dead for this source. Both are the Margit-sziget records, which is to
    # say the blank-city fallback is also what recovered the only priced Budapest events
    # in the sample.
    assert len([r for r in kvizestek_payload if r["entryFee"]]) == 10

    events = parse_fixture(kvizestek_source, kvizestek_payload)
    priced = [event for event in events if event.price_raw]

    assert len(priced) == 2
    assert {event.price_raw for event in priced} == {"1500 HUF"}
    assert {event.title for event in priced} == {"Szabadtéri kvíz a Margit-szigeten"}


def test_a_published_entry_fee_would_survive_into_the_price(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    # Exercised on a copy of a real priced record moved to Budapest, because no genuine
    # Budapest record carries a fee (see above) — the mapping still has to be right for
    # the day one does.
    record = dict(next(r for r in kvizestek_payload if r["entryFee"] == 3000))
    record["venueCity"] = "Budapest"

    (event,) = parse_fixture(kvizestek_source, [record])

    assert event.price_raw == "3000 HUF"


def test_a_missing_entry_fee_does_not_become_free(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    from digest.pipeline.normalize import parse_price

    events = parse_fixture(kvizestek_source, kvizestek_payload)
    unpriced = [event for event in events if event.price_raw is None]

    assert unpriced
    # None, not "0" — a null fee means "not published", and parse_price only reads
    # is_free from text it is actually given.
    assert parse_price(unpriced[0].price_raw) == (None, None, False)


def test_a_real_description_is_kept(
    kvizestek_source: KvizestekSource, kvizestek_payload: list[dict[str, Any]]
) -> None:
    events = parse_fixture(kvizestek_source, kvizestek_payload)

    described = [event for event in events if event.description]
    assert described
    assert "Interaktív online nyomozós játék" in {event.description for event in described}


def test_parse_rejects_a_payload_shape_it_does_not_understand(
    kvizestek_source: KvizestekSource,
) -> None:
    from digest.errors import ParseError

    with pytest.raises(ParseError, match="expected an array"):
        list(kvizestek_source.parse(make_result("https://x/api", json_body={"a": 1})))


def test_the_events_reach_the_kviz_category_which_is_the_point_of_this_batch(
    kvizestek_source: KvizestekSource,
    kvizestek_payload: list[dict[str, Any]],
    config_path: Path,
    sources_dir: Path,
) -> None:
    """The batch exists because `kviz` and `tarsasjatek` had zero sources, so this asserts
    that config.yaml's rules actually land these events there — end to end, through the
    real normalize and categorize, rather than assuming it.

    The horizon is widened for the measurement only. `schedule.horizon_days` is a delivery
    window, not a statement about categorisation, and leaving it at the shipped value would
    silently reduce this to whichever events happen to fall in the weeks after the fixture
    day — a number that says nothing about whether the rules work, and one that moves every
    time somebody retunes the window.
    """
    config = load_config(config_path, sources_dir, None)
    wide = config.model_copy(
        update={"schedule": config.schedule.model_copy(update={"horizon_days": 60})}
    )
    raw = parse_fixture(kvizestek_source, kvizestek_payload)

    events = categorize(normalize(raw, wide, now=FIXTURE_DAY), wide)
    primary = Counter(event.categories[0] for event in events)

    assert len(events) == 91
    # 73 of 91. The answer to the batch's question for `kviz` is yes.
    assert primary["kviz"] == 73

    titles_by_category: dict[str, set[str]] = {}
    for event in events:
        titles_by_category.setdefault(event.categories[0], set()).add(event.title)

    # FINDING 1 (reported, not patched — category rules are out of scope for this batch):
    # the `kvíz` keyword does not fire inside the Hungarian compound "Filmkvíz". Package 16
    # made keyword matching word-PREFIX based, which fixes the suffix case ("kvízestek"),
    # but "Filmkvíz" puts the keyword at the END of a compound, where no prefix rule
    # reaches it — and `kvíz` is under the five-character threshold anyway, so it stays
    # whole-word. Still `egyeb`, for a reason that is now precisely stated.
    assert "Filmkvíz" in titles_by_category["egyeb"]

    # FINDING 2: the murder-mystery nights genuinely are not quizzes, so landing outside
    # `kviz` is correct behaviour, not a rule bug. Recorded so the two are not conflated.
    assert any("NYOMOZÓS JÁTÉK" in title for title in titles_by_category["egyeb"])

    # And the gap this batch could NOT close: nothing here feeds `tarsasjatek`.
    assert primary["tarsasjatek"] == 0


def test_the_shipped_horizon_still_ships_a_useful_number_of_quiz_nights(
    kvizestek_source: KvizestekSource,
    kvizestek_payload: list[dict[str, Any]],
    config_path: Path,
    sources_dir: Path,
) -> None:
    # What the source contributes under the shipped config, as opposed to the measurement
    # above: 40 of the 91 fall inside `horizon_days: 20`, 32 of them `kviz`. That is the
    # number that actually reaches an email on the fixture day. Both counts move with
    # `schedule.horizon_days` — they were 26 and 20 while it was 14 — so the assertion is
    # about this source being worth its slot, not about the window being any one length.
    config = load_config(config_path, sources_dir, None)
    raw = parse_fixture(kvizestek_source, kvizestek_payload)

    events = categorize(normalize(raw, config, now=FIXTURE_DAY), config)

    assert len(events) == 40
    assert Counter(event.categories[0] for event in events)["kviz"] == 32


# --------------------------------------------------------------------------------------
# redandblack — reached §6.1 step 3, dropped: parseable markup, no current events
# --------------------------------------------------------------------------------------


def test_redandblack_is_disabled_with_no_urls(sources_dir: Path) -> None:
    spec = yaml.safe_load((sources_dir / "redandblack.yaml").read_text(encoding="utf-8"))

    assert spec["enabled"] is False
    assert spec["listing"]["urls"] == []


def test_redandblack_current_programme_list_is_server_rendered_empty(repo_root: Path) -> None:
    # This is the finding, pinned. The selectors below are the ones a declarative spec
    # would use, and they work — the source is dropped only because the container they
    # point into has nothing in it.
    html = (repo_root / "tests/fixtures/redandblack_programok.html").read_text(encoding="utf-8")
    tree = HTMLParser(html)

    active = tree.css_first("div.programs__active")
    assert active is not None, "the 'Aktuális programjaink' container is gone; re-check the site"
    assert active.css("div.card") == []


def test_redandblack_every_card_on_the_page_is_a_past_event(repo_root: Path) -> None:
    html = (repo_root / "tests/fixtures/redandblack_programok.html").read_text(encoding="utf-8")
    tree = HTMLParser(html)

    dates = [node.text(strip=True) for node in tree.css("div.news__date")]
    years = {year for text in dates for year in re.findall(r"(\d{4})\.", text)}

    assert dates, "the card markup changed; the selectors in this file need re-checking"
    # Saved 2026-08-22. Nothing on the page is from 2025 or 2026.
    assert max(years) == "2024"


# --------------------------------------------------------------------------------------
# kedvesidegen — reached §6.1 step 2, dropped: a public API with no date in it
# --------------------------------------------------------------------------------------


def test_kedvesidegen_is_disabled_with_no_urls(sources_dir: Path) -> None:
    spec = yaml.safe_load((sources_dir / "kedvesidegen.yaml").read_text(encoding="utf-8"))

    assert spec["enabled"] is False
    assert spec["listing"]["urls"] == []


def test_kedvesidegen_store_api_exposes_no_date_field_at_all(repo_root: Path) -> None:
    # The stop condition, pinned. The WooCommerce Store API answers publicly and returns
    # clean JSON — it simply has nowhere to put an event date, and the site sets none.
    products = json.loads(
        (repo_root / "tests/fixtures/kedvesidegen_products.json").read_text(encoding="utf-8")
    )

    assert products
    for product in products:
        date_keys = [key for key in product if "date" in key.lower() or "time" in key.lower()]
        assert date_keys == [], (
            f"{product['slug']} grew {date_keys} — the source is worth re-checking"
        )
        assert product["attributes"] == []
        assert product["categories"] == []


def test_kedvesidegen_dates_exist_only_inside_product_names_and_carry_no_year(
    repo_root: Path,
) -> None:
    products = json.loads(
        (repo_root / "tests/fixtures/kedvesidegen_products.json").read_text(encoding="utf-8")
    )
    dated = [
        p for p in products if re.match(r"^[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+ \d{1,2}\.", p["name"])
    ]

    assert dated, "product naming changed; re-check whether a real date field appeared"
    for product in dated:
        assert not re.search(r"\b20\d{2}\b", product["name"])


def test_kedvesidegen_product_names_and_slugs_disagree_so_neither_is_a_date_source(
    repo_root: Path,
) -> None:
    # Product 439 is named "Április 19. – Játékest" but its slug is `marcius-6-jatekest`:
    # the record was recycled for a later event and only half of it was updated. Parsing a
    # date out of either half would silently invent one.
    products = json.loads(
        (repo_root / "tests/fixtures/kedvesidegen_products.json").read_text(encoding="utf-8")
    )
    recycled = next(p for p in products if p["id"] == 439)

    assert recycled["slug"] == "marcius-6-jatekest"
    assert recycled["name"].startswith("Április 19.")
