from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from structlog.testing import capture_logs

from digest.models import Event, make_event_id
from digest.state import (
    RunLogEntry,
    SentEntry,
    State,
    load_state,
    purge,
    record_run,
    record_sent,
    save_state,
    was_sent,
)

BUDAPEST = ZoneInfo("Europe/Budapest")


def make_event(**overrides: Any) -> Event:
    title = overrides.pop("title", "Sub Focus")
    start = overrides.pop("start", datetime(2026, 8, 29, 20, 0, tzinfo=BUDAPEST))
    venue_name = overrides.pop("venue_name", "A38 Hajó")
    effective_date = overrides.pop("effective_date", start.date())
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": ["https://port.hu/esemeny/x"],
        "title": title,
        "description": None,
        "start": start,
        "end": None,
        "effective_date": effective_date,
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


def test_purge_removes_past_entries_and_keeps_future_ones() -> None:
    state = State(
        sent=[
            SentEntry(
                id="a", t="past", d=date(2026, 8, 1), s=date(2026, 7, 30), u=date(2026, 8, 1)
            ),
            SentEntry(
                id="b", t="today", d=date(2026, 8, 16), s=date(2026, 8, 2), u=date(2026, 8, 16)
            ),
            SentEntry(
                id="c", t="future", d=date(2026, 8, 29), s=date(2026, 8, 16), u=date(2026, 8, 29)
            ),
        ]
    )

    result = purge(state, today=date(2026, 8, 16))

    assert [entry.id for entry in result.sent] == ["b", "c"]


def test_purge_keys_off_u_not_d_for_a_still_running_events_protection() -> None:
    # AUDIT-5 BLOCKER: an event still running well past its effective_date (`d`) must keep
    # its ledger protection alive until `u` (event.end.date()) — otherwise purge() drops it
    # a day after it first sends, while normalize() is still re-offering the same event.
    still_running = make_event(
        title="Villon-est",
        start=datetime(2026, 8, 10, 19, 0, tzinfo=BUDAPEST),
        effective_date=date(2026, 8, 10),
        end=datetime(2026, 8, 25, 23, 59, tzinfo=BUDAPEST),
    )
    state = record_sent(State(), [still_running], sent_on=date(2026, 8, 16))
    (entry,) = state.sent
    assert entry.d == date(2026, 8, 10)
    assert entry.u == date(2026, 8, 25)

    # The old bug: `d` (Aug 10) is already in the past by Aug 20, but the event is still
    # running (end is Aug 25) — the entry must survive.
    still_protected = purge(state, today=date(2026, 8, 20))
    assert [e.id for e in still_protected.sent] == [still_running.id]
    assert was_sent(still_protected, still_running) is True

    # Once `u` itself has passed, purge may finally drop it.
    now_expired = purge(state, today=date(2026, 8, 26))
    assert now_expired.sent == []


def test_was_sent_matches_by_exact_id() -> None:
    event = make_event()
    state = record_sent(State(), [event], sent_on=date(2026, 8, 16))

    assert was_sent(state, event) is True


def test_was_sent_catches_a_title_rewrite_that_changed_the_id() -> None:
    # "Sub Focus" and "Sub Focus | A38" get DIFFERENT ids (the venue suffix survives
    # normalize_title by design, SPEC 4.1) — this is exactly the case the fuzzy branch
    # exists for: a source rewrote the title, so only date + fuzzy title still match.
    original = make_event(title="Sub Focus")
    rewritten = make_event(title="Sub Focus | A38")
    assert original.id != rewritten.id

    state = record_sent(State(), [original], sent_on=date(2026, 8, 16))

    assert was_sent(state, rewritten) is True


def test_was_sent_is_false_for_the_same_title_on_a_different_date() -> None:
    sent_event = make_event(start=datetime(2026, 8, 29, 20, 0, tzinfo=BUDAPEST))
    other_day_event = make_event(start=datetime(2026, 9, 5, 20, 0, tzinfo=BUDAPEST))
    assert sent_event.id != other_day_event.id

    state = record_sent(State(), [sent_event], sent_on=date(2026, 8, 16))

    assert was_sent(state, other_day_event) is False


def test_was_sent_is_false_for_an_unrelated_event() -> None:
    state = record_sent(State(), [make_event(title="Sub Focus")], sent_on=date(2026, 8, 16))
    unrelated = make_event(title="Chase & Status", urls=["https://port.hu/esemeny/y"])

    assert was_sent(state, unrelated) is False


def test_a_missing_state_file_yields_an_empty_state(tmp_path: Path) -> None:
    with capture_logs() as logs:
        state = load_state(tmp_path / "absent.json")

    assert state == State()
    assert [entry["event"] for entry in logs] == ["state_missing"]


def test_a_corrupt_state_file_yields_an_empty_state_and_an_error_log(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")

    with capture_logs() as logs:
        state = load_state(path)

    assert state == State()
    errors = [entry for entry in logs if entry["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["event"] == "state_corrupt"


def test_a_schema_invalid_state_file_yields_an_empty_state_and_an_error_log(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "sent": "not a list"}', encoding="utf-8")

    with capture_logs() as logs:
        state = load_state(path)

    assert state == State()
    assert any(entry["event"] == "state_corrupt" for entry in logs)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = record_sent(State(), [make_event()], sent_on=date(2026, 8, 16))

    save_state(original, path)
    reloaded = load_state(path)

    assert reloaded == original


def test_save_state_creates_a_missing_parent_directory(tmp_path: Path) -> None:
    # AUDIT-1 BLOCKER-1: production's real path is state/state.json, and state/ does
    # not exist in a fresh checkout — this must not raise FileNotFoundError.
    path = tmp_path / "state" / "state.json"
    assert not path.parent.exists()

    save_state(State(), path)

    assert path.exists()
    assert load_state(path) == State()


def test_state_json_uses_the_abbreviated_field_names(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = record_sent(State(), [make_event()], sent_on=date(2026, 8, 16))
    save_state(state, path)

    text = path.read_text(encoding="utf-8")
    assert '"t":' in text
    assert '"d":' in text
    assert '"s":' in text
    assert '"u":' in text
    assert "title" not in text  # not "title", not "sent_date" — the short forms only


def test_run_log_keeps_only_the_last_thirty_entries() -> None:
    start = date(2026, 1, 1)
    state = State()
    for day in range(35):
        state = record_run(
            state,
            RunLogEntry(
                date=start + timedelta(days=day), raw=10, after_dedup=8, sent=1, seconds=1.0
            ),
        )

    assert len(state.run_log) == 30
    assert state.run_log[0].date == start + timedelta(days=5)  # the oldest 5 fell off
    assert state.run_log[-1].date == start + timedelta(days=34)


def test_record_sent_appends_without_mutating_the_original_state() -> None:
    original = State()
    updated = record_sent(original, [make_event()], sent_on=date(2026, 8, 16))

    assert original.sent == []
    assert len(updated.sent) == 1


def test_purge_does_not_mutate_its_input() -> None:
    state = State(
        sent=[SentEntry(id="a", t="x", d=date(2026, 8, 1), s=date(2026, 7, 30), u=date(2026, 8, 1))]
    )

    purge(state, today=date(2026, 8, 16))

    assert len(state.sent) == 1
