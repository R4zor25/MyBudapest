from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from structlog.testing import capture_logs

from digest.config import Config, load_config
from digest.errors import ConfigError
from digest.fetch.base import FetchResult, FetchTask
from digest.sources.declarative import (
    MAPPABLE_FIELDS,
    TRANSFORM_NAMES,
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


# --------------------------------------------------------------------------------------
# Unknown keys fail at LOAD time (§6.3)
# --------------------------------------------------------------------------------------


def minimal(**spec: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "demo",
        "fetcher": "api",
        "listing": {"urls": ["https://example.com/api"], "json_path": "events[*]"},
        "fields": {"title": {"path": "title"}, "url": {"path": "url"}},
    }
    return {**base, **spec}


def test_a_misspelled_field_names_the_source_and_the_field_it_meant() -> None:
    """The defect this closes: `city: { path: "venue.city" }` sat in tokenklub.yaml being
    extracted and dropped, and the symptom was a field that was simply always empty —
    which looks exactly like a source that does not publish it. A typo has to fail where
    it is written."""
    spec = minimal(fields={"title": {"path": "t"}, "url": {"path": "u"}, "citty": {"path": "c"}})

    with pytest.raises(ConfigError) as excinfo:
        DeclarativeSource(spec, Config())

    message = str(excinfo.value)
    assert "'demo'" in message
    assert "citty" in message
    assert "'city'" in message


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("source_id", "id:"),
        ("source_event_key", "url"),
        ("extra", "free-form dict"),
    ],
)
def test_the_three_unmappable_fields_are_refused_with_the_reason(field: str, reason: str) -> None:
    """These are real RawEvent fields, so a "did you mean" would be nonsense — they are
    plausible mistakes with specific answers, and the error gives the answer."""
    spec = minimal(fields={"title": {"path": "t"}, "url": {"path": "u"}, field: {"path": "x"}})

    with pytest.raises(ConfigError, match=reason):
        DeclarativeSource(spec, Config())

    assert field not in MAPPABLE_FIELDS


def test_an_unknown_transform_is_the_same_class_of_error() -> None:
    spec = minimal(transforms={"title": ["html_unescape", "uppercase"]})

    with pytest.raises(ConfigError) as excinfo:
        DeclarativeSource(spec, Config())

    assert "uppercase" in str(excinfo.value)
    assert "'demo'" in str(excinfo.value)


def test_a_transform_keyed_on_a_field_that_does_not_exist_is_rejected() -> None:
    with pytest.raises(ConfigError, match="titel"):
        DeclarativeSource(minimal(transforms={"titel": ["strip"]}), Config())


def test_an_argument_carrying_transform_is_validated_on_its_name_only() -> None:
    # `truncate:400` and `regex:pat:1` are valid; only the part before the first colon is
    # the name. A bare `regex` with no argument is a name too -- it is the extraction that
    # would do nothing, not the spec that is wrong.
    DeclarativeSource(minimal(transforms={"title": ["truncate:400", "regex:(x):1"]}), Config())
    DeclarativeSource(minimal(transforms={"title": ["regex"]}), Config())


@pytest.mark.parametrize(
    ("section", "block", "expected"),
    [
        ("listing", {"urls": [], "item_selectors": "div"}, "item_selector"),
        ("pagination", {"parm": "page"}, "param"),
    ],
)
def test_an_unknown_listing_or_pagination_key_is_rejected(
    section: str, block: dict[str, Any], expected: str
) -> None:
    listing = block if section == "listing" else {"urls": [], "pagination": block}
    spec = minimal(listing={**listing, "json_path": "events[*]"})

    with pytest.raises(ConfigError, match=expected):
        DeclarativeSource(spec, Config())


def test_a_misspelled_inner_field_key_names_the_field_and_the_key_it_meant() -> None:
    """The original bug's exact shape one level down: `{ selctor: "h3" }` extracted nothing
    and said nothing about it, so the field looked like data the site does not publish."""
    spec = minimal(fields={"title": {"selctor": "h3"}, "url": {"path": "u"}})

    with pytest.raises(ConfigError) as excinfo:
        DeclarativeSource(spec, Config())

    message = str(excinfo.value)
    assert "'demo'" in message
    assert "fields.title" in message
    assert "selctor" in message
    assert "'selector'" in message


def test_a_misspelled_top_level_key_is_rejected() -> None:
    """The worst of the silent ignores. Every other one loses a field; this one makes the
    YAML disagree with reality about whether the source runs at all."""
    with pytest.raises(ConfigError) as excinfo:
        DeclarativeSource(minimal(enabledd=True), Config())

    assert "enabledd" in str(excinfo.value)
    assert "'enabled'" in str(excinfo.value)


def test_a_disabled_source_is_validated_too() -> None:
    # A typo in a switched-off source is still a typo, and should not be lying in wait for
    # the day someone switches it on.
    with pytest.raises(ConfigError, match="citty"):
        DeclarativeSource(minimal(enabled=False, fields={"citty": {"path": "c"}}), Config())


# --------------------------------------------------------------------------------------
# What the `plugin:` exemption does and does not cover
# --------------------------------------------------------------------------------------


def cooltix_spec(sources_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((sources_dir / "cooltix.yaml").read_text(encoding="utf-8"))


def test_a_plugin_keeps_its_own_listing_vocabulary(sources_dir: Path) -> None:
    """`listing.pagination.page_size` is cooltix's, not §6.3's — the plugin drives its own
    cursor pagination. Validating a plugin's listing block against the declarative list
    would reject a working source, which is why that block is exempt."""
    spec = cooltix_spec(sources_dir)
    assert spec["listing"]["pagination"]["page_size"], "the premise of this test"

    (source,) = load_sources(Config(sources={"cooltix": spec}))

    assert source.id == "cooltix"


def test_a_plugin_does_not_get_an_exemption_at_the_top_level(sources_dir: Path) -> None:
    """The exemption belongs to `listing:` and `fields:`, not to the whole spec. Nothing
    owns `enabled:` except the registry, and it reads cooltix's the same way it reads
    tokenklub's."""
    spec = {**cooltix_spec(sources_dir), "enabledd": True}

    with pytest.raises(ConfigError, match="enabledd"):
        load_sources(Config(sources={"cooltix": spec}))


def test_every_mappable_field_survives_the_trip_into_raw_event() -> None:
    """The half the validator cannot prove on its own, and the half that actually failed.

    MAPPABLE_FIELDS comes from `RawEvent.model_fields`, so a new field is accepted in YAML
    the day it is added — but `_build_event` still names each field one by one, and a field
    it forgets is extracted and dropped exactly as `city` was. This maps every mappable
    field at once and checks each one arrived, so the two halves cannot drift apart
    silently. It iterates the derived set on purpose: a written-out list here would be the
    same hand-maintained copy that caused the bug."""
    samples = {"lat": "47.5", "lon": "19.05"}
    item = {name: samples.get(name, f"value-{name}") for name in sorted(MAPPABLE_FIELDS)}
    spec = minimal(fields={name: {"path": name} for name in sorted(MAPPABLE_FIELDS)})
    source = DeclarativeSource(spec, Config())

    (event,) = list(source.parse(make_result("https://example.com/api", json={"events": [item]})))

    dropped = sorted(name for name in MAPPABLE_FIELDS if getattr(event, name) is None)
    assert dropped == [], f"mapped in the spec but never reached RawEvent: {dropped}"


def test_every_declared_transform_name_is_one_apply_transforms_actually_runs() -> None:
    """TRANSFORM_NAMES is a second copy of what `apply_transforms` dispatches on — there is
    no vocabulary object to derive it from. This runs a value through every name in the
    set and fails if the chain does not recognise one, which is what keeps the copies in
    step."""
    arguments = {"truncate": "truncate:3", "regex": "regex:(x):1"}

    with capture_logs() as logs:
        for name in sorted(TRANSFORM_NAMES):
            apply_transforms("x", [arguments.get(name, name)], "https://example.com/")

    assert [entry for entry in logs if entry["event"] == "unknown_transform"] == []


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

    # The other half of the same property: a spec that is NOT valid must fail to load,
    # loudly. Until §6.3 validation existed, an unrecognised key was extracted and dropped,
    # so this half was silently false for every typo anyone had ever written.
    with pytest.raises(ConfigError, match="citty"):
        DeclarativeSource(
            {"id": "bogus", "fields": {"title": {"path": "t"}, "citty": {"path": "c"}}},
            config,
        )


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
