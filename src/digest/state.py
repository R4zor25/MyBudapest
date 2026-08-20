from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict
from rapidfuzz.fuzz import token_set_ratio

from digest.models import Event, normalize_title

log = structlog.get_logger()

_KEEP_RUN_LOG = 30
_FUZZY_TITLE_RATIO = 92


class SentEntry(BaseModel):
    """Field names are the short form from SPEC 8.1 (`id`, `t`, `d`, `s`, `u`), not
    expanded — this is committed daily and the abbreviation is deliberate, not an
    oversight."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    t: str  # normalized title
    d: date  # the event's date (effective_date)
    s: date  # the date it was sent
    # AUDIT-5 BLOCKER: purge() must not drop this entry while normalize() would still
    # keep re-offering the same still-running event (a multi-day exhibition, a weekly
    # series) — that carve-out (normalize.py's "(end or start) < now") keys off `end`,
    # not `effective_date`, so the purge cutoff has to too. event.end.date() when set,
    # else effective_date (same as `d`) for an ordinary single-instant event.
    u: date


class SourceHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    consecutive_failures: int = 0
    last_ok: date | None = None
    last_count: int = 0
    etag: str | None = None
    disabled_until: date | None = None


class RunLogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    raw: int
    after_dedup: int
    sent: int
    seconds: float


class State(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    last_run: datetime | None = None
    sent: list[SentEntry] = []
    source_health: dict[str, SourceHealth] = {}
    run_log: list[RunLogEntry] = []


def load_state(path: Path) -> State:
    """Never raises: losing the ledger for a day means at worst a repeated event in the
    newsletter, not a failed run (CLAUDE.md 6) — so a missing or corrupt file starts fresh
    instead of stopping anything."""
    if not path.exists():
        log.warning("state_missing", path=str(path))
        return State()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return State.model_validate(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.error("state_corrupt", path=str(path), error=str(exc))
        return State()


def save_state(state: State, path: Path) -> None:
    # AUDIT-1 BLOCKER-1: state/ does not exist in a fresh checkout (§11 checks out the
    # repo, then runs `digest run` before anything else creates the directory), so a
    # bare write_text raised FileNotFoundError on every real run, after the email had
    # already gone out. Same mkdir pattern as render/web.py's write_site().
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")


def purge(state: State, today: date) -> State:
    """Drops every `sent` entry whose protection window (`u`) has passed (§8.2) — the
    guard against resending only needs to cover events that could still go out. AUDIT-5
    BLOCKER: this used to key off `d` (effective_date), which expires the day after a
    still-running event first sends — one day before normalize() stops re-offering it."""
    return state.model_copy(update={"sent": [entry for entry in state.sent if entry.u >= today]})


def was_sent(state: State, event: Event) -> bool:
    """Exact id match, or (same event date AND a fuzzy title match) — the fuzzy branch
    exists because a source can rewrite a title, which changes the id (§4.1, §8.2)."""
    title_norm = normalize_title(event.title)
    for entry in state.sent:
        if entry.id == event.id:
            return True
        if entry.d == event.effective_date and token_set_ratio(entry.t, title_norm) >= (
            _FUZZY_TITLE_RATIO
        ):
            return True
    return False


def record_sent(state: State, events: list[Event], sent_on: date) -> State:
    """Appends one ledger entry per event that went out, so a later run's was_sent() can
    recognize it. Not named in SPEC 8.2, but purge()/was_sent() are only meaningful once
    something populates `sent` — this is that something."""
    new_entries = [
        SentEntry(
            id=event.id,
            t=normalize_title(event.title),
            d=event.effective_date,
            s=sent_on,
            u=event.end.date() if event.end else event.effective_date,
        )
        for event in events
    ]
    return state.model_copy(update={"sent": [*state.sent, *new_entries]})


def record_run(state: State, entry: RunLogEntry) -> State:
    """run_log keeps only the last 30 entries (§8.2)."""
    return state.model_copy(update={"run_log": [*state.run_log, entry][-_KEEP_RUN_LOG:]})
