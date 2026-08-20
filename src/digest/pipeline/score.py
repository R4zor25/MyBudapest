from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from digest.config import Config, ScoringConfig
from digest.models import Event, contains_word

log = structlog.get_logger()

# date.weekday(): Monday=0 .. Sunday=6, matching config.yaml's weekday_weights keys.
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# The write UI's "pin" action (package 14, overrides.py/overrides.yaml): deliberately
# large and hardcoded, not a tuned profile weight — "pinned" means "show me this no
# matter what", so the bonus only has to reliably clear min_score and outrank everything
# else, not be calibrated against the other terms. Kept out of ScoringConfig on purpose:
# that model is the private PROFILE_YAML surface (CLAUDE.md 5), and a pin is a
# day-to-day curation action, not a twice-a-year weight tune. render/web.py excludes
# this term from the public events.json breakdown — "pinned" is exactly the kind of
# personal signal SPEC 9.0 keeps out of the web profile.
PINNED_BONUS = 100.0


def score(
    events: list[Event],
    config: Config,
    sent_ids: frozenset[str] = frozenset(),
    pinned_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> list[Event]:
    """Implements SPEC 7.7 term by term, then applies the min_score exclusion that filter()
    could not (SPEC 7.6's sixth reason): this is the first point in the pipeline where
    Event.score exists to compare against. See filter.py for the other five reasons.

    `sent_ids` mirrors filter()'s parameter: it is what novelty_bonus needs to know "did
    this event already appear in the ledger" — empty by default until state.py (package 8)
    supplies the real set. `pinned_ids` is the write UI's overrides.yaml (package 14)."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)

    scored = [_score_one(event, config, sent_ids, pinned_ids, moment) for event in events]
    survivors = [event for event in scored if event.score >= config.filters.min_score]
    dropped = len(scored) - len(survivors)
    if dropped:
        log.info("dropped_below_min_score", count=dropped, min_score=config.filters.min_score)
    return survivors


def score_one(
    event: Event,
    config: Config,
    sent_ids: frozenset[str] = frozenset(),
    pinned_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> Event:
    """The per-event half of `score`, without the min_score cut — this is what
    `digest explain` uses, since the point of that command is showing the breakdown of an
    event that might end up excluded, not hiding it before it can be inspected."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    return _score_one(event, config, sent_ids, pinned_ids, moment)


def _score_one(
    event: Event,
    config: Config,
    sent_ids: frozenset[str],
    pinned_ids: frozenset[str],
    now: datetime,
) -> Event:
    scoring = config.scoring
    breakdown: dict[str, float] = {
        "category_weight": _category_weight(event, scoring),
        "keyword_boosts": _keyword_boosts(event, scoring),
        "free_bonus": scoring.free_bonus if event.is_free else 0.0,
        "cheap_bonus": _cheap_bonus(event, scoring),
        "same_district_bonus": _same_district_bonus(event, config),
        "distance_penalty": _distance_penalty(event, scoring),
        "novelty_bonus": scoring.novelty_bonus if event.id not in sent_ids else 0.0,
        "soon_bonus": _soon_bonus(event, scoring, now),
        "weekday_weight": scoring.weekday_weights.get(
            _WEEKDAY_KEYS[event.effective_date.weekday()], 0.0
        ),
        "pinned_bonus": PINNED_BONUS if event.id in pinned_ids else 0.0,
    }
    return event.model_copy(update={"score": sum(breakdown.values()), "score_breakdown": breakdown})


def _category_weight(event: Event, scoring: ScoringConfig) -> float:
    primary = event.categories[0] if event.categories else None
    if primary is None:
        return 0.0
    return scoring.category_weights.get(primary, 0.0)


def _keyword_boosts(event: Event, scoring: ScoringConfig) -> float:
    text = f"{event.title} {event.description or ''}"
    return sum(
        weight for keyword, weight in scoring.keyword_boosts.items() if contains_word(text, keyword)
    )


def _cheap_bonus(event: Event, scoring: ScoringConfig) -> float:
    cheap = scoring.cheap_bonus
    if cheap is None or event.price_min is None or event.price_min >= cheap.under_huf:
        return 0.0
    return cheap.points


def _same_district_bonus(event: Event, config: Config) -> float:
    proximity = config.scoring.proximity
    home = config.home
    if proximity is None or home is None or event.district != home.district:
        return 0.0
    return proximity.same_district_bonus


def _distance_penalty(event: Event, scoring: ScoringConfig) -> float:
    proximity = scoring.proximity
    if proximity is None or event.distance_km is None:
        return 0.0
    return -(event.distance_km * proximity.distance_penalty_per_km)


def _soon_bonus(event: Event, scoring: ScoringConfig, now: datetime) -> float:
    soon = scoring.soon_bonus
    if soon is None or (event.start - now) > timedelta(days=soon.within_days):
        return 0.0
    return soon.points
