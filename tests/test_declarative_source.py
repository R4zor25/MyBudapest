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


def test_a_mapped_city_reaches_raw_event_verbatim() -> None:
    """`city` is §7.1's first-choice input for the settlement §7.6 filters on, and the
    engine is the only way a declarative source (tokenklub) can supply it. Asserted with a
    settlement that is not Budapest, because that is the case no saved fixture contains:
    tokenklub's 18 real records all say Budapest, and the two plugin sources cut a
    non-Budapest record before it ever becomes a RawEvent.

    Verbatim on purpose — the engine maps, it does not canonicalize. "Budapest XI." is
    shortened by §7.1's `_canonical_city`, in one place, downstream."""
    spec = {
        "id": "demo-api",
        "fetcher": "api",
        "listing": {"urls": ["https://example.com/api"], "json_path": "events[*]"},
        "fields": {
            "title": {"path": "title"},
            "url": {"path": "url"},
            "city": {"path": "venue.city", "optional": True},
        },
    }
    source = DeclarativeSource(spec, Config())
    payload = {
        "events": [
            {"title": "Kvíz", "url": "https://x/1", "venue": {"city": "Győr"}},
            {"title": "Klub", "url": "https://x/2", "venue": {"city": "Budapest XI."}},
            {"title": "Est", "url": "https://x/3", "venue": {}},
        ]
    }

    events = list(source.parse(make_result("https://example.com/api", json=payload)))

    assert [e.city for e in events] == ["Győr", "Budapest XI.", None]


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


def test_every_source_yaml_builds_a_usable_source(config_path: Path, sources_dir: Path) -> None:
    """A property over the directory, not a fixed list. The previous version asserted set
    equality against the ids it expected, so every new source needed this test edited —
    friction whose usual end state is that the test gets deleted rather than updated. What
    actually matters is that nothing in sources/ silently fails to load: a spec that
    raises, or that the registry skips, would otherwise show up only as a source that
    quietly contributes nothing."""
    config = load_config(config_path, sources_dir, None)

    sources = load_sources(config)

    yaml_stems = {path.stem for path in sources_dir.glob("*.yaml")}
    assert yaml_stems, "sources/ should not be empty"
    assert len(sources) == len(yaml_stems), (
        f"{len(yaml_stems)} YAML files produced {len(sources)} sources -- one did not load"
    )

    ids = [source.id for source in sources]
    assert len(set(ids)) == len(ids), f"duplicate source ids: {sorted(ids)}"

    # The id is the filename stem (SPEC 6.3: "kötelező, egyedi, = fájlnév"). Registry
    # lookups, `digest fetch <id>`, state.json's health keys and the `plugin:` module name
    # all assume the two agree.
    assert set(ids) == yaml_stems

    for source in sources:
        assert callable(source.discover), f"{source.id} has no discover()"
        assert callable(source.parse), f"{source.id} has no parse()"


def test_every_source_declares_a_fetcher_the_runtime_can_build(
    config_path: Path, sources_dir: Path
) -> None:
    """Checked against cli's own registry rather than a literal, so the two cannot drift.
    A `fetcher:` the runtime has no class for raises FetchError at the first request
    (cli._fetch_source), i.e. one dead source per run, discovered in production.

    SPEC 6.3 lists `playwright` as a schema value with no implementation -- that is exactly
    the kind of spec this catches before it ships enabled."""
    from digest.cli import _FETCHERS

    config = load_config(config_path, sources_dir, None)

    unbuildable = {
        source.id: source.fetcher
        for source in load_sources(config)
        if source.fetcher not in _FETCHERS
    }

    assert not unbuildable, (
        f"{unbuildable} name fetchers the runtime cannot build; known: {sorted(_FETCHERS)}"
    )


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
