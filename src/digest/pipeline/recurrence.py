from __future__ import annotations

from digest.config import Config
from digest.models import Event


def recurrence(events: list[Event], config: Config) -> list[Event]:
    threshold = config.recurrence.series_threshold_days
    return [_mark(event, threshold) for event in events]


def _mark(event: Event, threshold: int) -> Event:
    if event.end and (event.end - event.start).days > threshold:
        return event.model_copy(update={"is_series": True})
    return event
