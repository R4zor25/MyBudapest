from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from structlog.testing import capture_logs

from digest.config import Config, load_config
from digest.errors import ConfigError, ParseError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent
from digest.sources.declarative import DeclarativeSource
from digest.sources.plugins.port_hu import PortHuSource, district_from_zip, resolve_end
from digest.sources.registry import load_sources


@pytest.fixture
def config(config_path: Path, sources_dir: Path) -> Config:
    return load_config(config_path, sources_dir, None)


@pytest.fixture
def source(config: Config) -> PortHuSource:
    built = {source.id: source for source in load_sources(config)}
    return built["port-hu"]


@pytest.fixture
def payload(repo_root: Path) -> dict[str, Any]:
    fixture = repo_root / "tests" / "fixtures" / "port_hu_list.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def make_result(payload: Any) -> FetchResult:
    return FetchResult(
        task=FetchTask(url="https://port.hu/fixture"),
        status=200,
        text="",
        json=payload,
        from_cache=False,
    )


@pytest.fixture
def events(source: PortHuSource, payload: dict[str, Any]) -> list[RawEvent]:
    return list(source.parse(make_result(payload)))


def test_every_fixture_record_becomes_a_raw_event(
    source: PortHuSource, payload: dict[str, Any]
) -> None:
    with capture_logs() as logs:
        events = list(source.parse(make_result(payload)))

    assert len(events) == len(payload)
    assert {event.source_event_key for event in events} == set(payload)
    assert [entry for entry in logs if entry["event"] == "record_skipped"] == []


def test_gallery_never_reaches_a_raw_event(events: list[RawEvent], payload: dict[str, Any]) -> None:
    gallery_urls = {
        image["url"]
        for record in payload.values()
        for image in record.get("gallery") or []
        if isinstance(image, dict) and image.get("url")
    }
    assert gallery_urls, "the fixture must contain gallery images for this test to mean anything"

    serialised = json.dumps([event.model_dump() for event in events], ensure_ascii=False)
    leaked = sorted(url for url in gallery_urls if url in serialised)
    assert not leaked, f"gallery images reached RawEvent: {leaked[:3]}"

    by_key = {event.source_event_key: event for event in events}
    for key, record in payload.items():
        assert by_key[key].image_url == record["thumbnail"]
        assert by_key[key].extra == {}


def test_district_field_is_used_directly_when_it_is_an_integer(
    events: list[RawEvent], payload: dict[str, Any]
) -> None:
    with_district = {
        key: record
        for key, record in payload.items()
        if isinstance((record.get("address") or {}).get("district"), int)
    }
    assert with_district, "the fixture must contain a record with a non-null district"

    by_key = {event.source_event_key: event for event in events}
    for key, record in with_district.items():
        assert by_key[key].district_raw == record["address"]["district"]


def test_district_falls_back_to_the_postal_code(
    events: list[RawEvent], payload: dict[str, Any]
) -> None:
    by_key = {event.source_event_key: event for event in events}
    checked = 0
    for key, record in payload.items():
        address = record.get("address") or {}
        if isinstance(address.get("district"), int) or address.get("zip") != 1033:
            continue
        assert by_key[key].district_raw == "III."
        checked += 1
    assert checked, "the fixture must contain a 1033 record without a district"


@pytest.mark.parametrize(
    ("zip_code", "expected"),
    [
        (1113, "XI."),
        (1033, "III."),
        ("1033", "III."),
        (1011, "I."),
        (1239, "XXIII."),
        (1000, None),  # the online-only record: "00" is not a district
        (1240, None),  # there is no 24th district
        (9021, None),  # Győr, not Budapest
        (None, None),
        ("", None),
    ],
)
def test_district_from_zip(zip_code: Any, expected: str | None) -> None:
    assert district_from_zip(zip_code) == expected


def test_entity_encoded_description_is_decoded(
    events: list[RawEvent], payload: dict[str, Any]
) -> None:
    encoded = {
        key: record["description"]
        for key, record in payload.items()
        if "&" in (record.get("description") or "") and ";" in (record.get("description") or "")
    }
    assert encoded, "the fixture must contain an entity-encoded description"

    by_key = {event.source_event_key: event for event in events}
    for key in encoded:
        description = by_key[key].description
        assert description is not None
        assert "&eacute;" not in description
        assert "&aacute;" not in description
        assert description == description.strip()

    villon = by_key["event-6258530"].description
    assert villon is not None
    assert villon.startswith("Miért ilyen népszerűek Magyarországon")


def test_url_is_absolute_and_the_category_is_extracted(
    events: list[RawEvent], payload: dict[str, Any]
) -> None:
    by_key = {event.source_event_key: event for event in events}
    for key, record in payload.items():
        event = by_key[key]
        assert event.url == f"https://port.hu{record['url']}"
        assert event.url_category == record["url"].split("/")[2]
    assert {event.url_category for event in events} == {"zene"}


def test_end_is_resolved_against_the_start_year(events: list[RawEvent]) -> None:
    by_key = {event.source_event_key: event for event in events}
    assert by_key["event-6258530"].end_raw == "2026-08-21 23:59:00"
    assert by_key["event-6262316"].end_raw == "2026-09-30 22:00:00"
    assert by_key["event-6267230"].end_raw is None


def test_end_rolls_over_into_the_next_year(budapest: ZoneInfo) -> None:
    # No fixture record crosses a year boundary, but the rule in SPEC 6.5 exists for it.
    start = datetime(2026, 12, 28, 22, 0, tzinfo=budapest)
    assert resolve_end(" - 01. 03. 23:59", start) == "2027-01-03 23:59:00"
    assert resolve_end(" - 12. 30. 23:59", start) == "2026-12-30 23:59:00"


@pytest.mark.parametrize("raw", ["", "   ", " - ", " - not a date"])
def test_unusable_end_becomes_none(raw: str, budapest: ZoneInfo) -> None:
    assert resolve_end(raw, datetime(2026, 8, 14, 19, 0, tzinfo=budapest)) is None


def test_start_and_venue_are_taken_verbatim(
    events: list[RawEvent], payload: dict[str, Any]
) -> None:
    by_key = {event.source_event_key: event for event in events}
    for key, record in payload.items():
        assert by_key[key].start_raw == record["eventStart"]
        assert by_key[key].venue_name == record["place"]
        assert by_key[key].native_category == record["type"]


def test_coordinates_are_present_on_every_record(events: list[RawEvent]) -> None:
    assert all(event.lat is not None and event.lon is not None for event in events)


def test_no_price_is_extracted(events: list[RawEvent], payload: dict[str, Any]) -> None:
    assert all(not record["ticket"] for record in payload.values())
    assert all(event.price_raw is None for event in events)


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        ({"id": "event-1", "url": "/esemeny/zene/x", "eventStart": "2026-08-14 19:00:00"}, "title"),
        ({"id": "event-1", "title": "X", "eventStart": "2026-08-14 19:00:00"}, "url"),
        ({"id": "event-1", "title": "X", "url": "/x", "eventStart": "tegnap"}, "eventStart"),
        ("not an object", "object"),
    ],
)
def test_a_broken_record_is_skipped_not_raised(
    source: PortHuSource, payload: dict[str, Any], broken: Any, reason: str
) -> None:
    mixed = {"event-broken": broken, **payload}

    with capture_logs() as logs:
        events = list(source.parse(make_result(mixed)))

    assert len(events) == len(payload)
    skipped = [entry for entry in logs if entry["event"] == "record_skipped"]
    assert len(skipped) == 1
    assert reason in skipped[0]["reason"]


def test_a_payload_that_is_not_an_object_raises(source: PortHuSource) -> None:
    with pytest.raises(ParseError, match="object keyed by event id"):
        list(source.parse(make_result([{"id": "event-1"}])))


def test_registry_builds_the_plugin_source(source: PortHuSource) -> None:
    assert source.id == "port-hu"
    assert source.name == "Port.hu"
    # AUDIT-5 BLOCKER: disabled until SPEC 17 question 1 (the real listing endpoint) is
    # resolved — see sources/port-hu.yaml's own comment. This was `True`; it is not an
    # oversight that it changed.
    assert source.enabled is False
    assert source.priority == 10
    assert source.fetcher == "api"
    assert source.rate_limit_seconds == 2


def test_registry_routes_specs_without_a_plugin_key_to_the_declarative_engine(
    config: Config,
) -> None:
    spec = {
        "id": "example",
        "fetcher": "http",
        "fields": {"title": {"selector": "h3"}, "url": {"selector": "a", "attr": "href"}},
    }
    declarative = config.model_copy(update={"sources": {"example": spec}})

    (built,) = load_sources(declarative)

    assert isinstance(built, DeclarativeSource)
    assert built.id == "example"


def test_registry_raises_for_an_unknown_plugin(config: Config) -> None:
    broken = config.model_copy(update={"sources": {"x": {"id": "x", "plugin": "no_such_module"}}})
    with pytest.raises(ConfigError, match="no_such_module"):
        load_sources(broken)


def test_discover_yields_nothing_while_the_endpoint_is_open(source: PortHuSource) -> None:
    with capture_logs() as logs:
        assert list(source.discover()) == []
    assert [entry["event"] for entry in logs] == ["no_listing_urls"]


def test_cli_fixture_table_lists_every_event(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path, payload: dict[str, Any]
) -> None:
    from digest.cli import fixture_table

    monkeypatch.chdir(repo_root)
    monkeypatch.delenv("PROFILE_YAML", raising=False)

    lines = fixture_table("port-hu", Path("tests/fixtures/port_hu_list.json"))

    assert lines[0].split() == ["START", "DISTRICT", "VENUE", "TITLE"]
    assert lines[-1] == f"{len(payload)} events"
    assert len(lines) == len(payload) + 2
    assert any("A38 Hajó" in line for line in lines)
