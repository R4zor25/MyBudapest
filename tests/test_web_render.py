from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from digest.config import Config
from digest.models import Event, make_event_id
from digest.render.web import (
    WEB_PROFILE_FIELDS,
    WebOutput,
    purge_archive,
    render_web,
    write_site,
)
from digest.state import SourceHealth

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 4, 34, tzinfo=BUDAPEST)

# Taken from the CODE, not from the spec. This used to be a hand-copy of SPEC 9.1's
# events.json example, with the document cited as the source of truth — so the document was
# authoritative for a field list it could not enforce, and any divergence between the two
# would have been invisible here. `render.web.WEB_PROFILE_FIELDS` is the authority now;
# SPEC 9.1 describes what the fields mean and deliberately carries no copy of them.
#
# What this alone cannot catch is a field wrongly ADDED to both at once, which is why the
# absence assertions below name the excluded fields explicitly (§9.0, and AUDIT-1
# BLOCKER-2 for `breakdown`: score_breakdown terms are the private profile's numbers
# verbatim, e.g. breakdown.category == category_weights[cat]).
_EXPECTED_EVENT_KEYS = set(WEB_PROFILE_FIELDS)


def make_event(index: int = 0, **overrides: Any) -> Event:
    title = overrides.pop("title", f"Event {index}")
    start = overrides.pop("start", datetime(2026, 8, 19, 20, 0, tzinfo=BUDAPEST))
    venue_name = overrides.pop("venue_name", "A38 Hajó")
    score = overrides.pop("score", float(index))
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": [f"https://port.hu/esemeny/{index}"],
        "title": title,
        # Populated on purpose, like tests/test_render.py's make_event: the "no leak"
        # assertions below are only meaningful if these fields have something to leak.
        "description": "Egy nagyszerű este vár mindenkire, tele meglepetésekkel.",
        "start": start,
        "end": None,
        "effective_date": start.date(),
        "venue_name": venue_name,
        "district": "XI.",
        "lat": None,
        "lon": None,
        "distance_km": None,
        "price_min": 2500,
        "price_max": None,
        "categories": ["koncert"],
        "image_url": "https://media.port.hu/images/example.jpg",
        "score": score,
    }
    return Event(**{**base, **overrides})


def _img_tags(html: str) -> list[str]:
    return re.findall(r"<img\b[^>]*>", html, flags=re.IGNORECASE)


def _img_srcs(html: str) -> list[str]:
    tags = _img_tags(html)
    return [m.group(1) for tag in tags for m in [re.search(r'src="([^"]*)"', tag)] if m]


def test_events_json_has_exactly_the_web_profile_fields_no_description_no_image() -> None:
    events = [make_event(0, title="Villon-est"), make_event(1, title="HØT SPØT")]

    output = render_web(events, Config(), now=NOW)
    payload = json.loads(output.events_json)

    assert set(payload.keys()) == {"generated_at", "events"}
    assert len(payload["events"]) == 2
    for record in payload["events"]:
        # The builder produces exactly what the constant declares — neither may drift from
        # the other unnoticed, and both live in render/web.py where a reviewer sees them
        # together.
        assert set(record.keys()) == _EXPECTED_EVENT_KEYS
        # And the §9.0 exclusions by name, which hold no matter what the constant says.
        for excluded in (
            "description",
            "image",
            "image_url",
            "breakdown",
            "score_breakdown",
            "lat",
            "lon",
            "urls",
            "price_max",
        ):
            assert excluded not in record


def test_events_json_publishes_the_night_shift_result_not_its_inputs() -> None:
    """CLAUDE.md rule 12, pinned on the one case that motivated it: a 01:00 start belongs
    to the previous evening, and the page must be able to read that rather than work it
    out. Before this, index.html.j2 carried `isNight = l.h < 5` — the §7.7 rule
    reimplemented in the browser, where no Python test could reach it."""
    # effective_date is passed in, not derived here, because that is the contract under
    # test: whatever §7.7 decided is what the payload must carry. normalize files a 01:00
    # start on the previous evening; the renderer's job is to pass that through.
    small_hours = make_event(
        0, start=datetime(2026, 8, 20, 1, 0, tzinfo=BUDAPEST), effective_date=date(2026, 8, 19)
    )
    evening = make_event(1, start=datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST))

    payload = json.loads(render_web([small_hours, evening], Config(), now=NOW).events_json)
    by_start = {record["start"]: record["effective_date"] for record in payload["events"]}

    assert by_start["2026-08-20T01:00:00+02:00"] == "2026-08-19"
    assert by_start["2026-08-20T20:00:00+02:00"] == "2026-08-20"


def test_events_json_generated_at_is_utc_and_event_start_keeps_its_offset() -> None:
    output = render_web([make_event(0)], Config(), now=NOW)
    payload = json.loads(output.events_json)

    assert payload["generated_at"] == "2026-08-16T02:34:00Z"
    assert payload["events"][0]["start"] == "2026-08-19T20:00:00+02:00"


def test_events_json_never_publishes_the_score_breakdown() -> None:
    # AUDIT-1 BLOCKER-2: score.py's real term names are the private profile's numbers
    # verbatim (category_weight == category_weights[that category], etc.) — no key
    # remapping can make that safe to publish, so the whole field is gone (SPEC 9.0).
    raw_breakdown = {
        "category_weight": 4.0,
        "keyword_boosts": 1.0,
        "free_bonus": 0.0,
        "cheap_bonus": 0.0,
        "same_district_bonus": 2.0,
        "distance_penalty": -0.9,
        "novelty_bonus": 2.0,
        "soon_bonus": 1.0,
        "weekday_weight": 1.0,
    }
    event = make_event(0, score_breakdown=raw_breakdown, score=sum(raw_breakdown.values()))

    output = render_web([event], Config(), now=NOW)
    record = json.loads(output.events_json)["events"][0]

    assert "breakdown" not in record
    assert record["score"] == pytest.approx(sum(raw_breakdown.values()))


def test_events_json_never_reveals_that_an_event_was_pinned() -> None:
    # package 14: "pinned" is a personal curation signal from the write UI's
    # overrides.yaml. Publishing the raw score unadjusted would announce which specific
    # event was pinned via a 100-point jump, no cross-referencing required — PINNED_BONUS
    # must be subtracted back out even though the breakdown itself is already gone.
    raw_breakdown = {"category_weight": 4.0, "pinned_bonus": 100.0}
    event = make_event(0, score_breakdown=raw_breakdown, score=sum(raw_breakdown.values()))

    output = render_web([event], Config(), now=NOW)
    record = json.loads(output.events_json)["events"][0]

    assert "breakdown" not in record
    assert record["score"] == pytest.approx(4.0)


def test_no_img_tag_in_any_generated_html_points_at_a_non_local_domain() -> None:
    events = [make_event(0, title="Villon-est")]
    source_health = {
        "port-hu": SourceHealth(consecutive_failures=0, last_count=5, last_ok=date(2026, 8, 16))
    }

    output = render_web(events, Config(), source_health=source_health, now=NOW)

    for html in (output.index_html, output.archive_html, output.status_html):
        for src in _img_srcs(html):
            assert not src.startswith("http"), f"remote <img> src found: {src!r}"


def test_index_html_fetches_events_json_and_does_not_embed_a_data_literal() -> None:
    output = render_web([make_event(0)], Config(), now=NOW)

    assert "fetch('./events.json')" in output.index_html
    assert "const DATA = {" not in output.index_html


def test_archive_html_embeds_that_days_data_and_does_not_fetch_events_json() -> None:
    events = [make_event(0, title="Villon-est")]

    output = render_web(events, Config(), now=NOW)

    # Not "no fetch( anywhere": the write UI (package 14) legitimately calls fetch()
    # against the GitHub API on every page, archive included. What must not happen is
    # the *live-events* fetch branch — the archive is a frozen snapshot, not a pointer
    # to whatever events.json holds today.
    assert "fetch('./events.json')" not in output.archive_html
    assert "const DATA = await" not in output.archive_html
    assert events[0].id in output.archive_html
    assert "Villon-est" in output.archive_html


def test_masthead_source_health_line_is_templated_not_the_design_placeholder() -> None:
    source_health = {
        "port-hu": SourceHealth(consecutive_failures=0),
        "bigcitylife": SourceHealth(consecutive_failures=2),
    }

    output = render_web([make_event(0)], Config(), source_health=source_health, now=NOW)

    assert "2 forrásból 1 rendben" in output.index_html
    assert "11 forrásból 10 rendben" not in output.index_html


def test_an_event_title_with_a_script_close_tag_cannot_break_the_archive_page() -> None:
    events = [make_event(0, title="Vége </script><script>alert(1)</script> este")]

    output = render_web(events, Config(), now=NOW)

    assert "</script><script>alert(1)</script>" not in output.archive_html


def test_status_html_lists_the_four_source_health_fields() -> None:
    source_health = {
        "port-hu": SourceHealth(
            consecutive_failures=3,
            last_ok=date(2026, 8, 10),
            last_count=42,
            disabled_until=date(2026, 8, 20),
        )
    }

    output = render_web([], Config(), source_health=source_health, now=NOW)

    assert "port-hu" in output.status_html
    assert "2026-08-10" in output.status_html
    assert "42" in output.status_html
    assert "2026-08-20" in output.status_html


def test_write_site_creates_events_json_index_status_and_archive(tmp_path: Path) -> None:
    output = render_web([make_event(0)], Config(), now=NOW)
    site_dir = tmp_path / "site"

    write_site(output, site_dir, archive_keep_days=90)

    assert (site_dir / "events.json").read_text(encoding="utf-8") == output.events_json + "\n"
    assert (site_dir / "index.html").read_text(encoding="utf-8") == output.index_html
    assert (site_dir / "status.html").read_text(encoding="utf-8") == output.status_html
    archive_file = site_dir / "archive" / "2026-08-16.html"
    assert archive_file.read_text(encoding="utf-8") == output.archive_html


def test_archive_purge_removes_entries_older_than_keep_days_and_keeps_newer_ones(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    old = archive_dir / "2026-05-01.html"
    kept = archive_dir / "2026-08-01.html"
    old.write_text("old", encoding="utf-8")
    kept.write_text("kept", encoding="utf-8")

    purge_archive(archive_dir, keep_after=date(2026, 8, 16) - timedelta(days=90))

    assert not old.exists()
    assert kept.exists()


def test_archive_purge_on_a_missing_directory_does_not_raise(tmp_path: Path) -> None:
    purge_archive(tmp_path / "does-not-exist", keep_after=date(2026, 8, 16))


def test_write_site_purges_old_archive_entries_before_the_commit_step(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    archive_dir = site_dir / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "2020-01-01.html").write_text("stale", encoding="utf-8")
    output = WebOutput(
        events_json="{}",
        index_html="<html></html>",
        status_html="<html></html>",
        archive_html="<html></html>",
        archive_date=date(2026, 8, 16),
    )

    write_site(output, site_dir, archive_keep_days=90)

    assert not (archive_dir / "2020-01-01.html").exists()
    assert (archive_dir / "2026-08-16.html").exists()
