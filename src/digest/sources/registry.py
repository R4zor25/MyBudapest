from __future__ import annotations

import importlib
from collections.abc import Iterable
from typing import Any, Literal, Protocol

import structlog

from digest.config import Config
from digest.errors import ConfigError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent
from digest.sources.declarative import DeclarativeSource

log = structlog.get_logger()

_PLUGIN_PACKAGE = "digest.sources.plugins"


class Source(Protocol):
    id: str
    name: str
    enabled: bool
    priority: int
    fetcher: Literal["http", "api"]
    rate_limit_seconds: float

    def discover(self) -> Iterable[FetchTask]:
        """Which URLs to retrieve. Pagination unfolds here."""

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        """Raw events out of one downloaded response. Never raises for a single bad
        record — it logs it and moves on."""


def load_sources(config: Config) -> list[Source]:
    """Specs come from `config.sources`, which `load_config` already read from the sources
    directory — reading the same files twice would give the two a chance to disagree."""
    sources: list[Source] = []
    for source_id, spec in sorted(config.sources.items()):
        plugin = spec.get("plugin")
        if plugin is None:
            # A spec without a `plugin:` key describes its own parsing (§6.3) — no Python
            # module needed, DeclarativeSource reads the spec directly.
            sources.append(DeclarativeSource(spec, config))
            continue
        sources.append(_build_plugin_source(source_id, str(plugin), spec, config))
    return sources


def _build_plugin_source(
    source_id: str,
    plugin: str,
    spec: dict[str, Any],
    config: Config,
) -> Source:
    module_name = f"{_PLUGIN_PACKAGE}.{plugin}"
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"source {source_id!r} names plugin {plugin!r}: {exc}") from exc
    builder = getattr(module, "build", None)
    if builder is None:
        raise ConfigError(f"{module_name} has no build(spec, config) function")
    return builder(spec, config)
