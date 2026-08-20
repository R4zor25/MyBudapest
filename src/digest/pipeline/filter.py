from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from digest.config import Config
from digest.models import Event, contains_word

log = structlog.get_logger()


def filter(
    events: list[Event],
    config: Config,
    sent_ids: frozenset[str] = frozenset(),
    hidden_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> list[Event]:
    """Five of SPEC 7.6's six exclusion reasons. The sixth, min_score, needs Event.score,
    which does not exist yet here — filter runs before score in the pipeline (SPEC 7.4,
    CLAUDE.md). score() applies that cut once it has something to compare against.

    `sent_ids` defaults to "nothing sent yet": the ledger (state.py, package 8) does not
    exist yet either. Once it does, its `was_sent()` result is what a caller passes in.

    `hidden_ids` is the write UI's overrides.yaml (package 14) — a user explicitly asked
    never to see this event again. Checked first: nothing else about the event matters
    once it is hidden."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    horizon = moment + timedelta(days=config.schedule.horizon_days)

    survivors = []
    for event in events:
        reason = _exclusion_reason(event, config, sent_ids, hidden_ids, horizon)
        if reason is None:
            survivors.append(event)
        else:
            log.info("filtered", reason=reason, event_id=event.id, title=event.title)
    return survivors


def _exclusion_reason(
    event: Event,
    config: Config,
    sent_ids: frozenset[str],
    hidden_ids: frozenset[str],
    horizon: datetime,
) -> str | None:
    if event.id in hidden_ids:
        return "hidden_by_override"

    if event.start > horizon:
        return "beyond_horizon"

    if config.filters.categories is not None:
        primary = event.categories[0] if event.categories else None
        if primary not in config.filters.categories:
            return "category_not_allowed"

    if (
        config.filters.max_price_huf is not None
        and event.price_min is not None
        and event.price_min > config.filters.max_price_huf
    ):
        return "price_too_high"

    text = f"{event.title} {event.description or ''}"
    if any(contains_word(text, keyword) for keyword in config.filters.blocked_keywords):
        return "blocked_keyword"

    if event.id in sent_ids:
        return "already_sent"

    return None
