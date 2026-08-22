from __future__ import annotations

import itertools
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

from digest.config import Config, load_config
from digest.fetch.base import FetchResult, FetchTask
from digest.models import Event, RawEvent
from digest.pipeline.dedup import _MAX_START_GAP, _venues_match, dedup, fuzzy_title
from digest.pipeline.normalize import normalize
from digest.sources.registry import load_sources

BUDAPEST = ZoneInfo("Europe/Budapest")


def make_result(url: str, text: str = "", json_body: Any = None) -> FetchResult:
    return FetchResult(
        task=FetchTask(url=url), status=200, text=text, json=json_body, from_cache=False
    )


@pytest.fixture
def config(config_path: Path, sources_dir: Path) -> Config:
    return load_config(config_path, sources_dir, None)


# --------------------------------------------------------------------------------------
# funzine — reached §6.1 step 3, dropped: a live archive of eight-year-old events
# --------------------------------------------------------------------------------------


def test_funzine_is_disabled_with_no_urls(sources_dir: Path) -> None:
    spec = yaml.safe_load((sources_dir / "funzine.yaml").read_text(encoding="utf-8"))

    assert spec["enabled"] is False
    assert spec["listing"]["urls"] == []


def test_funzine_upcoming_events_archive_contains_nothing_newer_than_2018(
    repo_root: Path,
) -> None:
    # The finding, pinned. The page is headed "Következő események" (upcoming events) and
    # the markup parses cleanly — it is the content that is eight years stale.
    html = (repo_root / "tests/fixtures/funzine_events_archive.html").read_text(encoding="utf-8")

    assert "Következő események" in html
    listed = re.findall(
        r"\b(20[0-2]\d)\s+(?:Jan|Feb|Már|Ápr|Máj|Jún|Júl|Aug|Sep|Okt|Nov|Dec)", html
    )
    assert listed, "the archive's date markup changed; re-check whether the CPT came back"
    assert max(listed) == "2018"


def test_funzine_still_exposes_event_detail_links_which_is_why_it_looks_alive(
    repo_root: Path,
) -> None:
    html = (repo_root / "tests/fixtures/funzine_events_archive.html").read_text(encoding="utf-8")
    tree = HTMLParser(html)

    events = {
        node.attributes.get("href")
        for node in tree.css('a[href*="/esemeny/"]')
        if node.attributes.get("href")
    }
    real = {href for href in events if "/page/" not in href and "/feed/" not in href}

    # A working archive with real per-event pages and pagination — every technical
    # precondition met, and not one current event behind it.
    assert len(real) >= 5


# --------------------------------------------------------------------------------------
# welovebudapest — deliberate non-delivery: no fixture, no selectors
# --------------------------------------------------------------------------------------


def test_welovebudapest_stays_unfetched_and_has_no_fixture(
    sources_dir: Path, repo_root: Path
) -> None:
    # welovebudapest.com/robots.txt names `anthropic-ai` and disallows it site-wide
    # (re-checked 2026-08-22). The batch asked for this source; the answer is still no, and
    # the absence of a fixture is the point, not an oversight.
    spec = yaml.safe_load((sources_dir / "welovebudapest.yaml").read_text(encoding="utf-8"))

    assert spec["enabled"] is False
    assert spec["listing"]["urls"] == []
    assert not list((repo_root / "tests/fixtures").glob("*welovebudapest*"))


# --------------------------------------------------------------------------------------
# The batch's headline measurement: cross-source dedup over every fixture we have
# --------------------------------------------------------------------------------------


def load_every_fixture_source(config: Config, repo_root: Path) -> list[RawEvent]:
    sources = {s.id: s for s in load_sources(config)}
    plan = [
        ("port-hu", "port_hu_list.json", "https://port.hu/list", True),
        ("bigcitylife", "bigcitylife_list.html", "https://bigcitylife.hu/x", False),
        (
            "kvizestek",
            "kvizestek_upcoming.json",
            "https://foglalas.kvizestek.hu/api/events/upcoming",
            True,
        ),
    ]
    raw: list[RawEvent] = []
    for source_id, filename, url, is_json in plan:
        body = (repo_root / "tests/fixtures" / filename).read_text(encoding="utf-8")
        result = (
            make_result(url, json_body=json.loads(body)) if is_json else make_result(url, text=body)
        )
        raw.extend(sources[source_id].parse(result))
    return raw


@pytest.fixture
def all_fixture_events(config: Config, repo_root: Path) -> list[Event]:
    """Every source that has a fixture, normalized together at the EARLIEST fixture day
    with a horizon wide enough to cover all three capture windows. Both knobs are
    deliberate: at the shipped 14-day horizon and a later `now`, Port.hu and bigcitylife
    fall entirely into the past and dedup would be measuring one source, not three —
    while reporting a merge count of zero as if it meant something."""
    wide = config.model_copy(
        update={"schedule": config.schedule.model_copy(update={"horizon_days": 60})}
    )
    raw = load_every_fixture_source(config, repo_root)
    return normalize(raw, wide, now=datetime(2026, 8, 14, 0, 1, tzinfo=BUDAPEST))


def test_all_three_fixture_sources_actually_reach_dedup(all_fixture_events: list[Event]) -> None:
    # Guard on the measurement itself: a merge count means nothing about a source that
    # contributed no events. Was four until programturizmus was removed (§6.6).
    per_source = Counter(sid for event in all_fixture_events for sid in event.source_ids)

    assert per_source == {
        "kvizestek": 91,
        "port-hu": 20,
        "bigcitylife": 9,
    }
    assert len(all_fixture_events) == 120


def test_cross_source_dedup_finds_no_merges_because_there_is_nothing_to_merge(
    all_fixture_events: list[Event], config: Config
) -> None:
    """THE BATCH MEASUREMENT. Zero merges — and the brief's hypothesis for that ("the fuzzy
    matcher is failing on real cross-source data") is not what the numbers show. Three
    independent causes, each verified below, mean the matcher is never consulted:

    1. The capture windows barely overlap. Port.hu covers 05-06..08-16 and kvizestek
       08-22..10-01 — zero shared calendar days between the two largest sources.
    2. `_fuzzy_score` returns None unless the starts are within 90 minutes. Of 2819
       cross-source pairs only 39 share a calendar day and only 5 clear that gate.
    3. Those 5 all fail the venue gate, and they are genuinely different events.

    So the title ratio is compared zero times across sources. See the sibling test for what
    the ratios would have been.
    """
    with capture_logs() as logs:
        deduped = dedup(all_fixture_events, config)

    merges = [entry for entry in logs if entry["event"] == "dedup_merge"]
    cross_source = [
        entry
        for entry in merges
        if len(set(entry["source_a"].split(",")) | set(entry["source_b"].split(","))) > 1
    ]

    assert len(deduped) == 120
    assert merges == []
    assert cross_source == []


def test_the_ninety_minute_start_gate_is_what_stops_the_pairs_not_the_title_ratio(
    all_fixture_events: list[Event],
) -> None:
    pairs = [
        (a, b)
        for a, b in itertools.combinations(all_fixture_events, 2)
        if set(a.source_ids) != set(b.source_ids)
    ]
    same_day = [(a, b) for a, b in pairs if a.start.date() == b.start.date()]
    within_gap = [(a, b) for a, b in pairs if abs(a.start - b.start) <= _MAX_START_GAP]

    assert len(pairs) == 2819
    assert len(same_day) == 39
    assert len(within_gap) == 5
    # And every one of those five is stopped by the venue gate before any title comparison.
    assert [(a, b) for a, b in within_gap if _venues_match(a, b)] == []


def test_no_same_day_cross_source_pair_is_even_close_to_the_title_threshold(
    all_fixture_events: list[Event],
) -> None:
    # The direct answer to "is the matcher underperforming?": with BOTH gates removed, the
    # best cross-source title similarity on any shared day is ~37, against a merge
    # threshold of 88 and an ambiguous band starting at 80. These are not near-misses —
    # they are unrelated events that happen to fall on the same date. There is no true
    # duplicate in this fixture set for the matcher to have missed.
    from rapidfuzz.fuzz import token_set_ratio

    best = max(
        token_set_ratio(fuzzy_title(a), fuzzy_title(b))
        for a, b in itertools.combinations(all_fixture_events, 2)
        if set(a.source_ids) != set(b.source_ids) and a.start.date() == b.start.date()
    )

    assert best < 40
