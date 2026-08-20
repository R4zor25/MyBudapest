from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from digest.config import Config, load_config
from digest.errors import ConfigError
from digest.fetch.base import FetchResult, FetchTask
from digest.sources.declarative import (
    DeclarativeSource,
    apply_transforms,
    resolve_json_path,
    resolve_json_path_value,
)
from digest.sources.registry import load_sources


def make_result(url: str, text: str = "", json: Any = None) -> FetchResult:
    return FetchResult(task=FetchTask(url=url), status=200, text=text, json=json, from_cache=False)


def _drive(
    source: DeclarativeSource, responses: dict[str, str | Any]
) -> tuple[list[Any], list[str]]:
    """Mirrors cli.py's _fetch_source loop (package 10) closely enough to exercise
    discover()'s stop_when_empty behaviour without a real Fetcher or the network."""
    fetched: list[str] = []
    events: list[Any] = []
    for task in source.discover():
        key = task.url if not task.params else f"{task.url}?page={task.params.get('page')}"
        fetched.append(key)
        body = responses.get(key, "")
        result = (
            make_result(task.url, text=body)
            if isinstance(body, str)
            else make_result(task.url, json=body)
        )
        events.extend(source.parse(result))
    return events, fetched


def test_css_extraction_of_text_and_of_an_attribute() -> None:
    spec = {
        "id": "demo",
        "fetcher": "http",
        "listing": {"urls": ["https://example.com/list"], "item_selector": "div.card"},
        "fields": {
            "title": {"selector": "h3", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
        },
    }
    source = DeclarativeSource(spec, Config())
    html = '<div class="card"><h3>Villon-est</h3><a href="/e/1">link</a></div>'

    (event,) = list(source.parse(make_result("https://example.com/list", text=html)))

    assert event.title == "Villon-est"
    assert event.url == "/e/1"


def test_jsonpath_rejects_a_non_trailing_wildcard_instead_of_silently_truncating() -> None:
    payload = {"a": {"events": [{"b": [1, 2]}]}}

    with capture_logs() as logs:
        items = resolve_json_path(payload, "a.events[*].b")

    assert items == []
    warnings = [entry for entry in logs if entry["event"] == "unsupported_json_path"]
    assert len(warnings) == 1
    assert warnings[0]["path"] == "a.events[*].b"


def test_jsonpath_extraction() -> None:
    payload = {
        "data": {
            "events": [
                {"title": "Sub Focus", "eventStart": "2026-08-20 20:00:00"},
                {"title": "Chase & Status", "eventStart": "2026-08-21 20:00:00"},
            ]
        }
    }

    items = resolve_json_path(payload, "data.events[*]")

    assert [item["title"] for item in items] == ["Sub Focus", "Chase & Status"]
    assert resolve_json_path_value(items[0], "title") == "Sub Focus"
    assert resolve_json_path_value(items[0], "eventStart") == "2026-08-20 20:00:00"


def test_jsonpath_field_extraction_end_to_end() -> None:
    spec = {
        "id": "demo-api",
        "fetcher": "api",
        "listing": {"urls": ["https://example.com/api"], "json_path": "data.events[*]"},
        "fields": {
            "title": {"path": "title"},
            "url": {"path": "url"},
        },
    }
    source = DeclarativeSource(spec, Config())
    payload = {"data": {"events": [{"title": "Sub Focus", "url": "https://x/1"}]}}

    (event,) = list(source.parse(make_result("https://example.com/api", json=payload)))

    assert event.title == "Sub Focus"
    assert event.url == "https://x/1"


def test_a_transform_chain_runs_left_to_right() -> None:
    # truncate:5 after html_unescape+strip must cut the unescaped, stripped text — not
    # the raw entity-laden original, which proves the chain actually runs in order.
    value = apply_transforms(
        "  Rock &amp; Roll Night  ", ["html_unescape", "strip", "truncate:5"], "https://x/"
    )

    assert value == "Rock "


def test_a_missing_required_field_skips_that_item_and_siblings_survive() -> None:
    spec = {
        "id": "demo",
        "fetcher": "http",
        "listing": {"urls": ["https://example.com/list"], "item_selector": "div.card"},
        "fields": {
            "title": {"selector": "h3", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
        },
    }
    source = DeclarativeSource(spec, Config())
    html = (
        '<div class="card"><h3>Has a title</h3><a href="/e/1"></a></div>'
        '<div class="card"><a href="/e/2"></a></div>'  # no <h3>: title is missing
        '<div class="card"><h3>Also has a title</h3><a href="/e/3"></a></div>'
    )

    with capture_logs() as logs:
        events = list(source.parse(make_result("https://example.com/list", text=html)))

    assert [event.title for event in events] == ["Has a title", "Also has a title"]
    warnings = [entry for entry in logs if entry["event"] == "declarative_field_missing"]
    assert len(warnings) == 1
    assert warnings[0]["source_id"] == "demo"
    assert warnings[0]["field"] == "title"


def test_pagination_stops_on_an_empty_page() -> None:
    spec = {
        "id": "paged",
        "fetcher": "http",
        "listing": {
            "urls": ["https://example.com/list"],
            "pagination": {"param": "page", "start": 1, "max": 5, "stop_when_empty": True},
            "item_selector": "div.card",
        },
        "fields": {
            "title": {"selector": "h3", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
        },
    }
    source = DeclarativeSource(spec, Config())
    responses = {
        "https://example.com/list?page=1": '<div class="card"><h3>A</h3><a href="/a"></a></div>',
        "https://example.com/list?page=2": "<p>nothing here</p>",
        # page 3 would prove the engine kept going if it ever showed up in `fetched`
        "https://example.com/list?page=3": '<div class="card"><h3>C</h3><a href="/c"></a></div>',
    }

    events, fetched = _drive(source, responses)

    assert fetched == [
        "https://example.com/list?page=1",
        "https://example.com/list?page=2",
    ]
    assert [event.title for event in events] == ["A"]


def test_pagination_keeps_going_when_stop_when_empty_is_off() -> None:
    spec = {
        "id": "paged",
        "fetcher": "http",
        "listing": {
            "urls": ["https://example.com/list"],
            "pagination": {"param": "page", "start": 1, "max": 3, "stop_when_empty": False},
            "item_selector": "div.card",
        },
        "fields": {
            "title": {"selector": "h3", "attr": "text"},
            "url": {"selector": "a", "attr": "href"},
        },
    }
    source = DeclarativeSource(spec, Config())
    responses = {
        "https://example.com/list?page=1": '<div class="card"><h3>A</h3><a href="/a"></a></div>',
        "https://example.com/list?page=2": "<p>nothing here</p>",
        "https://example.com/list?page=3": '<div class="card"><h3>C</h3><a href="/c"></a></div>',
    }

    events, fetched = _drive(source, responses)

    assert len(fetched) == 3
    assert [event.title for event in events] == ["A", "C"]


def test_absolute_resolves_relative_urls_against_the_listing_page() -> None:
    spec = {
        "id": "demo",
        "fetcher": "http",
        "listing": {"urls": ["https://example.com/programs/list"], "item_selector": "div.card"},
        "fields": {
            "title": {"selector": "h3", "attr": "text"},
            "url": {"selector": "a", "attr": "href", "absolute": True},
        },
    }
    source = DeclarativeSource(spec, Config())
    html = '<div class="card"><h3>X</h3><a href="/e/1">link</a></div>'

    (event,) = list(source.parse(make_result("https://example.com/programs/list", text=html)))

    assert event.url == "https://example.com/e/1"


def test_a_disabled_source_may_omit_mandatory_fields() -> None:
    # A placeholder YAML (welovebudapest.yaml and friends) is `enabled: false` with no
    # fields at all — that must not raise, or every disabled placeholder breaks config
    # loading for the whole run.
    DeclarativeSource({"id": "placeholder", "enabled": False}, Config())


def test_an_enabled_source_without_title_or_url_fields_fails_fast() -> None:
    with pytest.raises(ConfigError, match="title"):
        DeclarativeSource({"id": "broken", "fields": {}}, Config())


def test_registry_builds_every_declarative_source_from_the_real_sources_dir(
    config_path: Path, sources_dir: Path
) -> None:
    config = load_config(config_path, sources_dir, None)

    sources = load_sources(config)

    declarative_ids = {s.id for s in sources if isinstance(s, DeclarativeSource)}
    assert declarative_ids == {
        "bigcitylife",
        "welovebudapest",
        "fidelio",
        "programturizmus",
        "szinhazak",
    }


def test_bigcitylife_parses_a_plausible_event_count_from_the_real_fixture(
    repo_root: Path, config_path: Path, sources_dir: Path
) -> None:
    config = load_config(config_path, sources_dir, None)
    source = next(s for s in load_sources(config) if s.id == "bigcitylife")
    html = (repo_root / "tests/fixtures/bigcitylife_list.html").read_text(encoding="utf-8")

    events = list(
        source.parse(make_result("https://bigcitylife.hu/hetvegi-programok-budapesten", text=html))
    )

    assert 5 <= len(events) <= 20
    first = events[0]
    assert first.title
    assert first.url.startswith("https://bigcitylife.hu/")
    assert first.start_raw
