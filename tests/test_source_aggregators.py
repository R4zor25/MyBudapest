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
from digest.models import Event, RawEvent, roman_district
from digest.pipeline.dedup import _MAX_START_GAP, _starts_match, _venues_match, dedup, fuzzy_title
from digest.pipeline.normalize import normalize
from digest.sources.declarative import DeclarativeSource
from digest.sources.registry import load_sources

BUDAPEST = ZoneInfo("Europe/Budapest")
PT_PAGE_1 = "https://www.programturizmus.hu/telepules-budapest-fovaros.html?fs=dc"
PT_PAGE_2 = "https://www.programturizmus.hu/telepules-budapest-fovaros.html?fs=dc&f=10"


def make_result(url: str, text: str = "", json_body: Any = None) -> FetchResult:
    return FetchResult(
        task=FetchTask(url=url), status=200, text=text, json=json_body, from_cache=False
    )


@pytest.fixture
def config(config_path: Path, sources_dir: Path) -> Config:
    return load_config(config_path, sources_dir, None)


@pytest.fixture
def programturizmus(config: Config) -> DeclarativeSource:
    source = next(s for s in load_sources(config) if s.id == "programturizmus")
    assert isinstance(source, DeclarativeSource)
    return source


def parse_programturizmus(
    source: DeclarativeSource, repo_root: Path, pages: tuple[str, ...] = ("p1", "p2")
) -> list[RawEvent]:
    urls = {"p1": PT_PAGE_1, "p2": PT_PAGE_2}
    events: list[RawEvent] = []
    for page in pages:
        html = (repo_root / f"tests/fixtures/programturizmus_budapest_{page}.html").read_text(
            encoding="utf-8"
        )
        events.extend(source.parse(make_result(urls[page], text=html)))
    return events


# --------------------------------------------------------------------------------------
# programturizmus — the one of the three that landed on a usable tier (§6.1 step 3)
# --------------------------------------------------------------------------------------


def test_the_listing_urls_are_budapest_scoped_and_are_two_explicit_offsets(
    sources_dir: Path,
) -> None:
    # Budapest scoping happens at the source (the brief's requirement): both URLs are the
    # Budapest settlement page. `f` is a record OFFSET, so the two pages are listed
    # explicitly rather than driven by §6.3's increment-by-one pagination.
    spec = yaml.safe_load((sources_dir / "programturizmus.yaml").read_text(encoding="utf-8"))

    assert spec["enabled"] is True
    assert spec["listing"]["urls"] == [PT_PAGE_1, PT_PAGE_2]
    assert all("budapest-fovaros" in url for url in spec["listing"]["urls"])
    assert "pagination" not in spec["listing"]
    assert spec["rate_limit_seconds"] == 3


def test_priority_is_weaker_than_every_other_enabled_source(sources_dir: Path) -> None:
    # The batch requires Port.hu to win shared fields in a merge. Higher number = weaker
    # (§6.2), and dedup's `_order_by_priority` picks the minimum, so this source must carry
    # the largest priority of any source. Every spec counts, enabled or not: dedup's
    # `_source_priority` reads config.sources directly and never looks at `enabled`.
    priorities = {}
    for path in sorted(sources_dir.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        priorities[spec["id"]] = spec["priority"]

    assert priorities["programturizmus"] == max(priorities.values())
    # The brief said "above Port.hu's 20". Port.hu is priority 10; 20 is kvizestek's, from
    # the previous batch. Both are satisfied by being weaker than either.
    assert priorities["port-hu"] == 10
    assert priorities["programturizmus"] > priorities["port-hu"]
    assert priorities["programturizmus"] > priorities["kvizestek"]


def test_each_page_yields_ten_events_and_the_two_pages_do_not_overlap(
    programturizmus: DeclarativeSource, repo_root: Path
) -> None:
    page_1 = parse_programturizmus(programturizmus, repo_root, ("p1",))
    page_2 = parse_programturizmus(programturizmus, repo_root, ("p2",))

    assert len(page_1) == 10
    assert len(page_2) == 10
    assert not {e.url for e in page_1} & {e.url for e in page_2}


def test_the_item_selector_takes_each_event_once_despite_triple_rendering(
    repo_root: Path,
) -> None:
    # Every event is emitted three times, once per responsive wrapper. Without the
    # `div.d-desktop` prefix the source would report 30 events for 10, and each would then
    # be deduped by exact id — quietly hiding the bug behind a merge count.
    html = (repo_root / "tests/fixtures/programturizmus_budapest_p1.html").read_text(
        encoding="utf-8"
    )
    tree = HTMLParser(html)
    card = 'section[class*="ContentItemCard_contentItemCardContainer"]'

    assert len(tree.css(card)) == 30
    for wrapper in ("div.d-desktop", "div.d-tablet", "div.d-mobile"):
        assert len(tree.css(f"{wrapper} {card}")) == 10


def test_no_selector_depends_on_a_build_hash(sources_dir: Path) -> None:
    # CSS-module classes are hash-suffixed ("ContentItemCard_titleContainer__7403T") and
    # the hash changes on every deploy. Every selector must match the stable module prefix
    # via [class*="..."], never the full class name.
    spec = yaml.safe_load((sources_dir / "programturizmus.yaml").read_text(encoding="utf-8"))
    selectors = [spec["listing"]["item_selector"]] + [
        field["selector"] for field in spec["fields"].values() if "selector" in field
    ]

    for selector in selectors:
        assert not re.search(r"__[A-Za-z0-9_-]{5}\b", selector), selector


def test_a_single_date_card_gets_a_start_and_no_end(
    programturizmus: DeclarativeSource, repo_root: Path
) -> None:
    events = parse_programturizmus(programturizmus, repo_root)
    event = next(e for e in events if e.url.endswith("ajanlat-budapest-jazz-club-program.html"))

    assert event.start_raw == "2026.08.22."
    # The card reads "2026.08.22. (szombat)": no range, so end_raw clears rather than
    # keeping the raw text and logging unparseable_end on every single-date card.
    assert event.end_raw is None


def test_a_date_range_card_gets_both_ends(
    programturizmus: DeclarativeSource, repo_root: Path
) -> None:
    events = parse_programturizmus(programturizmus, repo_root)
    event = next(e for e in events if e.url.endswith("ajanlat-lurdy-haz-program.html"))

    # "2026.08.17. (hétfő) - 2026.08.23. (vasárnap)"
    assert event.start_raw == "2026.08.17."
    assert event.end_raw == "2026.08.23."


def test_both_date_shapes_are_readable_by_normalize(
    programturizmus: DeclarativeSource, repo_root: Path, config: Config
) -> None:
    # Parsing, not filtering: the horizon is widened so that a dropped event can only mean
    # a date normalize could not read. At the shipped 14-day horizon 5 of the 20 fall
    # outside the window, which says nothing about the transforms.
    wide = config.model_copy(
        update={"schedule": config.schedule.model_copy(update={"horizon_days": 60})}
    )
    raw = parse_programturizmus(programturizmus, repo_root)

    with capture_logs() as logs:
        events = normalize(raw, wide, now=datetime(2026, 8, 16, 9, 0, tzinfo=BUDAPEST))

    assert not [entry for entry in logs if entry["event"] == "unparseable_start"]
    assert not [entry for entry in logs if entry["event"] == "unparseable_end"]
    assert len(events) == 20
    assert len(normalize(raw, config, now=datetime(2026, 8, 16, 9, 0, tzinfo=BUDAPEST))) == 15


def test_a_multi_day_run_that_already_started_still_survives(
    programturizmus: DeclarativeSource, repo_root: Path, config: Config
) -> None:
    # Lurdy Ház runs 08-17..08-23. On the 22nd its start is in the past; only the parsed
    # end keeps it alive (normalize drops on `(end or start) < now`). This is what the
    # end_raw transform buys — 13 of the 20 sampled cards carry a range.
    raw = parse_programturizmus(programturizmus, repo_root)
    lurdy = [e for e in raw if e.url.endswith("ajanlat-lurdy-haz-program.html")]

    events = normalize(lurdy, config, now=datetime(2026, 8, 22, 9, 0, tzinfo=BUDAPEST))

    assert len(events) == 1
    assert events[0].end is not None


def test_relative_hrefs_resolve_against_the_listing_page(
    programturizmus: DeclarativeSource, repo_root: Path
) -> None:
    # The cards link with a bare "ajanlat-foo.html" (no leading slash). The resolved URL
    # becomes source_event_key, so a wrong base would poison the ledger silently.
    events = parse_programturizmus(programturizmus, repo_root)

    assert all(e.url.startswith("https://www.programturizmus.hu/ajanlat-") for e in events)
    assert all(e.url == e.source_event_key for e in events)


def test_the_district_text_is_normalized_to_the_canonical_roman_form(
    programturizmus: DeclarativeSource, repo_root: Path, config: Config
) -> None:
    """WAS left unmapped: the cards carry "13. kerület" / "9. kerület - Ferencváros", and
    normalize passed a string through verbatim, so mapping the field would have produced
    `district="9. kerület - Ferencváros"` against Port.hu's `"IX."` — and §7.7 compares by
    equality, so the proximity bonus would silently never have fired.

    §7.1's `normalize_district` now converts every published shape, so the field is mapped
    and the district arrives in the same spelling as every other source's."""
    raw = parse_programturizmus(programturizmus, repo_root)

    assert any(e.district_raw for e in raw), "the cards do carry a district"
    events = normalize(raw, config, now=datetime(2026, 8, 16, 9, 0, tzinfo=BUDAPEST))
    districts = {e.district for e in events if e.district is not None}

    assert districts, "at least one card should yield a district"
    assert districts <= {roman_district(n) for n in range(1, 24)}
    assert "IX." in districts


def test_the_records_are_mostly_umbrella_programme_pages_not_single_events(
    programturizmus: DeclarativeSource, repo_root: Path
) -> None:
    # The headline quality finding, pinned. A card's <a> title names a venue's or artist's
    # whole season ("Budapest Jazz Club programok 2026") while the specific event sits in a
    # sibling label the schema has nowhere to put. This is why these records cannot fuzzy
    # match Port.hu's per-performance titles even when the dates line up.
    events = parse_programturizmus(programturizmus, repo_root)
    umbrella = [e for e in events if re.search(r"\b20\d{2}\b", e.title)]

    assert len(umbrella) >= 15
    assert any(e.title == "Budapest Jazz Club programok 2026" for e in events)


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
        ("programturizmus", "programturizmus_budapest_p1.html", PT_PAGE_1, False),
        ("programturizmus", "programturizmus_budapest_p2.html", PT_PAGE_2, False),
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
    with a horizon wide enough to cover all four capture windows. Both knobs are
    deliberate: at the shipped 14-day horizon and a later `now`, Port.hu and bigcitylife
    fall entirely into the past and dedup would be measuring three sources, not four —
    while reporting a merge count of zero as if it meant something."""
    wide = config.model_copy(
        update={"schedule": config.schedule.model_copy(update={"horizon_days": 60})}
    )
    raw = load_every_fixture_source(config, repo_root)
    return normalize(raw, wide, now=datetime(2026, 8, 14, 0, 1, tzinfo=BUDAPEST))


def test_all_four_fixture_sources_actually_reach_dedup(all_fixture_events: list[Event]) -> None:
    # Guard on the measurement itself: a merge count means nothing about a source that
    # contributed no events.
    per_source = Counter(sid for event in all_fixture_events for sid in event.source_ids)

    assert per_source == {
        "kvizestek": 91,
        "port-hu": 20,
        "programturizmus": 20,
        "bigcitylife": 9,
    }
    assert len(all_fixture_events) == 140


def test_cross_source_dedup_finds_no_merges_because_there_is_nothing_to_merge(
    all_fixture_events: list[Event], config: Config
) -> None:
    """THE BATCH MEASUREMENT. Zero merges — and the brief's hypothesis for that ("the fuzzy
    matcher is failing on real cross-source data") is not what the numbers show. Three
    independent causes, each verified below, mean the matcher is never consulted:

    1. The capture windows barely overlap. Port.hu covers 05-06..08-16 and kvizestek
       08-22..10-01 — zero shared calendar days between the two largest sources.
    2. `_fuzzy_score` returns None unless the starts are within 90 minutes. Of 5219
       cross-source pairs only 58 share a calendar day and only 5 clear that gate.
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

    assert len(deduped) == 140
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

    assert len(pairs) == 5219
    assert len(same_day) == 58
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


def test_programturizmus_events_keep_their_own_day(
    all_fixture_events: list[Event],
) -> None:
    """WAS a pinned defect: the listing publishes a date with no clock, `%Y.%m.%d.` parsed
    to 00:00, and §7.7's night shift then subtracted five hours and filed every event on
    the previous day — feeding `weekday_weights`, the group key and the email's day
    headings a date that was simply wrong.

    Fixed by carrying `start_time_known` out of the parser (§7.1): the shift now applies
    only to a real clock reading. Port.hu's genuine 00:00-04:00 sets still shift; see
    test_port_hu_night_sets_still_shift below."""
    programturizmus = [e for e in all_fixture_events if e.source_ids == ["programturizmus"]]

    assert len(programturizmus) == 20
    # The source publishes no clock, so the 00:00 is a missing value, not midnight.
    assert all(not e.start_time_known for e in programturizmus)
    assert all(e.start.hour == 0 and e.start.minute == 0 for e in programturizmus)
    # ...and therefore every event stays on the day the listing actually named.
    assert [e for e in programturizmus if e.effective_date != e.start.date()] == []


def test_programturizmus_is_no_longer_structurally_unable_to_merge(
    all_fixture_events: list[Event],
) -> None:
    """WAS the structural half of the zero-merge result: every event from this source
    starts at 00:00, and the old `_fuzzy_score` gate returned None beyond a 90-minute gap,
    so a date-only record could only ever be compared against events starting 00:00-01:30.
    No capture window could fix that — it held on every real run.

    The gate is now same-calendar-day whenever either side's clock is unknown, so these
    records can finally be compared at all: 0 comparable pairs before, 19 after, on this
    same fixture set. They still do not merge, but now for an ordinary reason (title and
    venue), not because the comparison was impossible."""
    programturizmus = [e for e in all_fixture_events if e.source_ids == ["programturizmus"]]
    others = [e for e in all_fixture_events if e.source_ids != ["programturizmus"]]

    by_old_gate = [
        (a, b) for a in programturizmus for b in others if abs(a.start - b.start) <= _MAX_START_GAP
    ]
    by_new_gate = [(a, b) for a in programturizmus for b in others if _starts_match(a, b)]

    assert by_old_gate == []
    assert len(by_new_gate) == 19


def test_venueless_events_are_never_collapsed_into_a_none_titled_row(
    all_fixture_events: list[Event], config: Config
) -> None:
    """WAS a pinned defect: programturizmus cards carry no venue (their location strip is
    county/city/district only), so §7.4's key put them all in the same bucket and built the
    collapsed title as f"{venue_name} — ...", which rendered "None — 4 program" straight
    into the email.

    Fixed by excluding venue-less events from grouping entirely, not by patching the
    title: a bucket of "every venue-less event of category X on day Y" is not a venue
    group, and collapsing it hides unrelated events behind a meaningless summary row."""
    from digest.pipeline.categorize import categorize
    from digest.pipeline.dedup import dedup
    from digest.pipeline.filter import filter as filter_events
    from digest.pipeline.group import group_with_counts
    from digest.pipeline.recurrence import recurrence
    from digest.pipeline.score import score

    # The real order (§7.4): normalize -> dedup -> recurrence -> categorize -> filter ->
    # score -> group. The group key includes primary_category, so skipping categorize
    # would group different events than a real run does.
    events = categorize(recurrence(dedup(all_fixture_events, config), config), config)
    scored = score(filter_events(events, config), config)
    outcome = group_with_counts(scored, config)
    collapsed = [event for event in outcome.events if event.group_size > 1]

    assert not any(event.title.startswith("None") for event in collapsed)
    assert all(event.venue_name is not None for event in collapsed)
    # Every venue-less event survives individually, and the stage says how many.
    venueless = [event for event in scored if event.venue_name is None]
    assert venueless, "fixture should contain venue-less events"
    assert outcome.ungrouped_venueless == len(venueless)
    assert {event.id for event in venueless} <= {event.id for event in outcome.events}
    # The stage still does its actual job where a venue IS present.
    assert "Sziget Fesztivál — 18 program" in {event.title for event in collapsed}
