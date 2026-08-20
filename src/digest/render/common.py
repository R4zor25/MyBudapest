from __future__ import annotations

from digest.state import SourceHealth


def source_health_line(source_health: dict[str, SourceHealth]) -> str:
    """ "N forrásból M rendben" — the email footer (§10 requirement 6) and the web
    masthead (package 12) both need the same one-line health summary; this is the one
    place it is computed, so the two renderers cannot drift apart."""
    total = len(source_health)
    ok = sum(1 for health in source_health.values() if health.consecutive_failures == 0)
    return f"{total} forrásból {ok} rendben"
