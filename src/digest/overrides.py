from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, field_validator

log = structlog.get_logger()


class Overrides(BaseModel):
    """The browser write UI's one pipeline-facing artifact (package 14): event ids the
    user explicitly hid or pinned, written to overrides.yaml via the GitHub Contents API.
    Two flat lists of ids — nothing else lives here; scoring weights stay in the private
    PROFILE_YAML secret (CLAUDE.md 5), not in this public, user-editable file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hidden: list[str] = []
    pinned: list[str] = []

    @field_validator("hidden", "pinned", mode="before")
    @classmethod
    def _null_becomes_empty(cls, value: list[str] | None) -> list[str]:
        # A YAML key with nothing under it ("hidden:\n") parses as None, not []. Both a
        # hand-edit and the write UI's own serializer can produce that for an empty list
        # — treat it as empty rather than failing validation and discarding the OTHER
        # list too (load_overrides falls back to a fully empty Overrides on any error).
        return [] if value is None else value


def load_overrides(path: Path) -> Overrides:
    """overrides.yaml is optional and hand-written by the browser UI, not the pipeline —
    a missing or malformed file must never break the run, the same discipline as
    state.py's load_state()."""
    if not path.exists():
        return Overrides()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Overrides.model_validate(data)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        log.error("overrides_corrupt", path=str(path), error=str(exc))
        return Overrides()
