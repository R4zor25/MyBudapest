from __future__ import annotations

from typing import Protocol

from digest.config import Config
from digest.models import Event


class Categorizer(Protocol):
    def categorize(self, events: list[Event], config: Config) -> list[Event]:
        """Assign categories to events. Must never raise on a quota error or an LLM
        outage — CLAUDE.md 4: the LLM is never on the critical path."""
