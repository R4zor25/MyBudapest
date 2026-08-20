from __future__ import annotations

from typing import Protocol

from digest.config import Config


class Deliverer(Protocol):
    """SPEC 10. Each implementation is one delivery channel (smtp, telegram, ...); the
    caller picks which ones to invoke from `config.delivery`."""

    type: str

    def send(self, subject: str, html: str, text: str, config: Config) -> bool:
        """Returns whether this call actually delivered — as opposed to a deliverer's own
        graceful, by-design skip (e.g. SPEC 5.3's missing-recipient no-op). AUDIT-2
        BLOCKER: cli._deliver uses this to decide whether record_sent may fire; a missing
        or misconfigured profile must not silently mark that day's events as sent when
        nothing actually went out."""
        ...
