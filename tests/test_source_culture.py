from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from digest.config import load_config
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent
from digest.sources.registry import load_sources


def make_result(url: str, text: str = "", json_body: Any = None) -> FetchResult:
    return FetchResult(
        task=FetchTask(url=url), status=200, text=text, json=json_body, from_cache=False
    )


def fixture_text(repo_root: Path, name: str) -> str:
    return (repo_root / "tests/fixtures" / name).read_text(encoding="utf-8")


@pytest.fixture
def config(config_path: Path, sources_dir: Path):
    return load_config(config_path, sources_dir, None)


# --------------------------------------------------------------------------------------
# Fidelio — the finder works, the database behind it is empty
# --------------------------------------------------------------------------------------


def test_fidelio_search_accepts_the_filters_it_was_sent(repo_root: Path) -> None:
    """The saved response is a real query for Budapest, 2026.08.22–2026.09.05. If the
    server had ignored or rejected the filters, none of this would be echoed back — which
    is what makes the empty result below a statement about the data, not the request."""
    html = fixture_text(repo_root, "fidelio_programkereso.html")

    assert '<option value="1" selected>' in html
    assert 'value="2026.08.22"' in html
    assert 'value="2026.09.05"' in html
    # The parameter names a future batch would otherwise have to re-derive.
    for field in ("query", "city", "category", "date_from", "date_to", "daypart"):
        assert f"ProgramSearch[{field}]" in html


def test_fidelio_programme_database_returns_nothing(repo_root: Path) -> None:
    """§6.1 step 5, and the reason this source is disabled. Not a JS-rendering problem:
    the results container is server-rendered, and what it renders is its own empty state."""
    html = fixture_text(repo_root, "fidelio_programkereso.html")

    assert 'class="search-wrapper-container programs' in html
    assert re.search(r'<h4 class="search-counts">\s*0 találat\s*</h4>', html)
    assert re.search(r'<div class="search-items">\s*</div>', html)
    assert "Nincs találat" in html


def test_fidelio_ships_disabled_with_no_guessed_selectors(sources_dir: Path) -> None:
    spec = yaml.safe_load((sources_dir / "fidelio.yaml").read_text(encoding="utf-8"))

    assert spec["enabled"] is False
    assert (spec.get("listing") or {}).get("urls") == []
    assert "fields" not in spec


# --------------------------------------------------------------------------------------
# Színházak.hu — wrong domain is parked, right domain is a blog that stopped in 2020
# --------------------------------------------------------------------------------------


def test_szinhaz_hu_is_an_editorial_blog_not_a_repertoire(repo_root: Path) -> None:
    html = fixture_text(repo_root, "szinhaz_hu_home.html")

    # No per-performance markup of any kind.
    assert "<time" not in html
    assert "datetime=" not in html

    # Every dated article link is /YYYY/MM/DD/slug; the newest is from 2020.
    years = sorted(set(re.findall(r'href="/(20\d{2})/\d{2}/\d{2}/', html)))
    assert years, "fixture should contain the blog's dated article links"
    assert max(years) == "2020"


def test_szinhaz_hu_delegates_ticketing_to_a_source_we_already_have(repo_root: Path) -> None:
    """Why looking for another 'theatre portal' is the wrong next move: this one points at
    Port.hu, so the repertoire gap is a Port.hu coverage question (§17.1)."""
    assert "port.hu/jegy" in fixture_text(repo_root, "szinhaz_hu_home.html")


def test_szinhazak_ships_disabled_with_no_guessed_selectors(sources_dir: Path) -> None:
    spec = yaml.safe_load((sources_dir / "szinhazak.yaml").read_text(encoding="utf-8"))

    assert spec["enabled"] is False
    assert (spec.get("listing") or {}).get("urls") == []


# --------------------------------------------------------------------------------------
# bigcitylife — the one culture source that ships, and what shape its records take
# --------------------------------------------------------------------------------------


@pytest.fixture
def bigcitylife_events(config, repo_root: Path) -> list[RawEvent]:
    source = next(s for s in load_sources(config) if s.id == "bigcitylife")
    html = fixture_text(repo_root, "bigcitylife_list.html")
    return list(
        source.parse(make_result("https://bigcitylife.hu/hetvegi-programok-budapesten", text=html))
    )


def test_bigcitylife_emits_individual_dates_not_ranges(bigcitylife_events) -> None:
    """Requirement 2 for the one source of the three that yields records. Every record is
    a single dated occurrence: none carries an end date on another day."""
    assert bigcitylife_events
    spanning = [
        e for e in bigcitylife_events if e.end_raw and e.end_raw[:10] != (e.start_raw or "")[:10]
    ]
    assert spanning == []


def test_bigcitylife_repeats_no_production_across_dates(bigcitylife_events) -> None:
    """The flooding pattern the culture brief warns about -- one production, twenty dates,
    which §7.4 grouping cannot collapse because it keys on (venue, date, category) -- does
    NOT appear here. A curated weekend list cannot show it either way, so this pins the
    observation, not a conclusion about theatre sources in general."""
    pairs = Counter((e.title.strip(), (e.venue_name or "").strip()) for e in bigcitylife_events)

    assert pairs, "fixture should parse at least one record"
    assert [pair for pair, n in pairs.items() if n > 1] == []


def test_bigcitylife_stays_below_port_hu_in_the_merge(config) -> None:
    """It is a curated pick list, not an authoritative repertoire feed, so it has no field
    on which it should outrank Port.hu (§7.2 merges base-first by priority)."""
    priorities = {s.id: s.priority for s in load_sources(config)}

    assert priorities["bigcitylife"] > priorities["port-hu"]


# --------------------------------------------------------------------------------------
# The repertoire question itself, on the data the project actually has
# --------------------------------------------------------------------------------------


def test_port_hu_publishes_a_running_series_as_one_ranged_record(config, repo_root: Path) -> None:
    """The good case, and the one §7.3 is built for: a months-long weekly series arrives as
    a SINGLE record with a start and a far end, not as one record per occurrence. Recorded
    here because the culture batch's premise -- that a repertoire source floods the digest
    with one production repeated -- is not what any source in the project does today."""
    source = next(s for s in load_sources(config) if s.id == "port-hu")
    text = fixture_text(repo_root, "port_hu_list.json")
    events = list(
        source.parse(make_result("https://port.hu/x", text=text, json_body=json.loads(text)))
    )

    ranged = [e for e in events if e.end_raw and e.end_raw[:10] != (e.start_raw or "")[:10]]
    assert any("HØT SPØT" in e.title for e in ranged)

    pairs = Counter((e.title.strip(), (e.venue_name or "").strip()) for e in events)
    assert [pair for pair, n in pairs.items() if n > 1] == []
