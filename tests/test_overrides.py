from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from structlog.testing import capture_logs

from digest.config import Config
from digest.models import Event, make_event_id
from digest.overrides import Overrides, load_overrides
from digest.pipeline.filter import filter as filter_events
from digest.pipeline.score import PINNED_BONUS, score

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=BUDAPEST)


def make_event(**overrides: Any) -> Event:
    title = overrides.pop("title", "Sub Focus")
    start = overrides.pop("start", datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST))
    venue_name = overrides.pop("venue_name", "A38 Hajó")
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": ["https://port.hu/esemeny/x"],
        "title": title,
        "description": None,
        "start": start,
        "end": None,
        "effective_date": start.date(),
        "venue_name": venue_name,
        "district": None,
        "lat": None,
        "lon": None,
        "distance_km": None,
        "price_min": None,
        "price_max": None,
        "categories": ["koncert"],
        "image_url": None,
    }
    return Event(**{**base, **overrides})


# --- load_overrides -----------------------------------------------------------------


def test_a_missing_overrides_file_yields_empty_overrides(tmp_path: Path) -> None:
    result = load_overrides(tmp_path / "does-not-exist.yaml")

    assert result == Overrides()


def test_a_valid_overrides_file_parses_hidden_and_pinned_ids(tmp_path: Path) -> None:
    path = tmp_path / "overrides.yaml"
    path.write_text("hidden:\n  - aaa\n  - bbb\npinned:\n  - ccc\n", encoding="utf-8")

    result = load_overrides(path)

    assert result.hidden == ["aaa", "bbb"]
    assert result.pinned == ["ccc"]


def test_a_corrupt_overrides_file_yields_empty_overrides_and_logs_an_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "overrides.yaml"
    path.write_text("hidden: [this is not: valid: yaml", encoding="utf-8")

    with capture_logs() as logs:
        result = load_overrides(path)

    assert result == Overrides()
    errors = [entry for entry in logs if entry["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["event"] == "overrides_corrupt"


def test_an_empty_hidden_list_does_not_discard_a_populated_pinned_list(tmp_path: Path) -> None:
    # "hidden:" with nothing under it parses as null, not [] — the write UI's own
    # serializer (index.html.j2) writes "hidden: []" specifically to avoid this, but
    # load_overrides must tolerate the null form too, since a hand-edit can produce it.
    path = tmp_path / "overrides.yaml"
    path.write_text("hidden:\npinned:\n  - abc\n", encoding="utf-8")

    result = load_overrides(path)

    assert result.hidden == []
    assert result.pinned == ["abc"]


def test_an_unknown_top_level_key_yields_empty_overrides_and_logs_an_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "overrides.yaml"
    path.write_text("hidden: [aaa]\nscoring:\n  free_bonus: 99\n", encoding="utf-8")

    with capture_logs() as logs:
        result = load_overrides(path)

    # extra="forbid": a hand-edited file (or one written by a bug) trying to sneak
    # scoring weights in here does not get to override the private profile silently.
    assert result == Overrides()
    assert any(entry["log_level"] == "error" for entry in logs)


# --- filter() honors hidden_ids -------------------------------------------------------


def test_filter_excludes_a_hidden_event_and_keeps_the_rest() -> None:
    hidden = make_event(title="Hidden one")
    visible = make_event(title="Still here", venue_name="Akvárium Klub")

    result = filter_events([hidden, visible], Config(), hidden_ids=frozenset({hidden.id}), now=NOW)

    assert [event.id for event in result] == [visible.id]


def test_filter_without_any_hidden_ids_excludes_nothing_extra() -> None:
    events = [make_event(title="A"), make_event(title="B", venue_name="Akvárium Klub")]

    result = filter_events(events, Config(), now=NOW)

    assert len(result) == 2


# --- score() honors pinned_ids ---------------------------------------------------------


def test_score_gives_a_pinned_event_the_pinned_bonus() -> None:
    pinned = make_event(title="Pin me")
    plain = make_event(title="Ordinary", venue_name="Akvárium Klub")

    (pinned_result,) = score([pinned], Config(), pinned_ids=frozenset({pinned.id}), now=NOW)
    (plain_result,) = score([plain], Config(), pinned_ids=frozenset({pinned.id}), now=NOW)

    assert pinned_result.score_breakdown["pinned_bonus"] == PINNED_BONUS
    assert plain_result.score_breakdown["pinned_bonus"] == 0
    assert pinned_result.score - plain_result.score == PINNED_BONUS


def test_a_pinned_event_survives_min_score_even_when_everything_else_is_zero() -> None:
    # Config() alone: no scoring weights configured, so every ordinary term is 0 and this
    # event would normally be dropped by min_score >= 0 only by a hair — pinning should
    # make that margin overwhelming, not marginal.
    config = Config()
    pinned = make_event(title="Barely qualifies otherwise")

    result = score([pinned], config, pinned_ids=frozenset({pinned.id}), now=NOW)

    assert len(result) == 1
    assert result[0].score >= PINNED_BONUS
