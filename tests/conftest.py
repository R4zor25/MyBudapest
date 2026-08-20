from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def config_path(repo_root: Path) -> Path:
    return repo_root / "config.yaml"


@pytest.fixture
def sources_dir(repo_root: Path) -> Path:
    return repo_root / "sources"


@pytest.fixture
def budapest() -> ZoneInfo:
    return ZoneInfo("Europe/Budapest")


@pytest.fixture
def start(budapest: ZoneInfo) -> datetime:
    """One reference instant for the whole suite: every Event carries a tz-aware
    Europe/Budapest datetime (§4), and a shared one keeps ids comparable across modules."""
    return datetime(2026, 8, 14, 19, 0, tzinfo=budapest)
