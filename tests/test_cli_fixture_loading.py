from __future__ import annotations

from pathlib import Path

import pytest

from digest.cli import _load_raw_events, fixture_table
from digest.errors import ParseError


@pytest.mark.parametrize(
    ("source_id", "fixture", "fetcher"),
    [
        # An http source: the parser reads result.text. This combination raised ParseError
        # before package 22 -- json.loads ran unconditionally, so no HTML source could be
        # exercised through the CLI at all.
        ("bigcitylife", "bigcitylife_list.html", "http"),
        ("tixa", "tixa_durerkert.html", "http"),
        # An api source: the parser reads result.json.
        ("port-hu", "port_hu_list.json", "api"),
        ("cooltix", "cooltix_events.json", "api"),
        ("tokenklub", "tokenklub_events.json", "api"),
    ],
)
def test_digest_fetch_runs_against_a_saved_fixture(
    repo_root: Path, source_id: str, fixture: str, fetcher: str
) -> None:
    _, raw = _load_raw_events(source_id, repo_root / "tests/fixtures" / fixture)

    assert raw, f"{source_id} should parse at least one record from {fixture}"
    assert all(event.source_id == source_id for event in raw)
    # The same call the `digest fetch` command makes, so the rendering path is covered too.
    assert fixture_table(source_id, repo_root / "tests/fixtures" / fixture)


def test_a_relative_href_resolves_against_the_configured_listing_url(repo_root: Path) -> None:
    """`absolute: true` resolves against the task URL (§6.3). Passing the fixture's local
    path would turn every relative href into a file:// URL, so the CLI's output would
    silently disagree with a real run -- which is exactly the kind of divergence a smoke
    test through this path exists to catch.

    Was exercised on programturizmus until §6.6 dropped it; bigcitylife is the remaining
    http source whose cards link with a bare "/slug"."""
    _, raw = _load_raw_events("bigcitylife", repo_root / "tests/fixtures/bigcitylife_list.html")

    assert raw
    assert all(event.url.startswith("https://bigcitylife.hu/") for event in raw)


def test_an_api_source_still_rejects_a_non_json_fixture(repo_root: Path) -> None:
    """The dispatch loosens the rule for http sources; it must not loosen it for api ones,
    where a parser reading result.json would otherwise get None and report nothing."""
    with pytest.raises(ParseError, match="not valid JSON"):
        _load_raw_events("port-hu", repo_root / "tests/fixtures/bigcitylife_list.html")


def test_an_unknown_source_is_named_in_the_error(repo_root: Path) -> None:
    from digest.errors import ConfigError

    with pytest.raises(ConfigError, match="unknown source"):
        _load_raw_events("nope", repo_root / "tests/fixtures/port_hu_list.json")
