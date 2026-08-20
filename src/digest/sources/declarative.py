from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

import structlog
from selectolax.parser import HTMLParser, Node

from digest.config import Config
from digest.errors import ConfigError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent

log = structlog.get_logger()

# RawEvent has no default for these two — a source YAML that doesn't map them would crash
# at parse time instead of producing a useless-but-harmless event, so this is checked once
# at construction (SPEC 6.3's example never omits them either).
_MANDATORY_RAW_EVENT_FIELDS = ("title", "url")


class DeclarativeSource:
    """A Source built entirely from YAML (SPEC 6.3) — adding a static source needs no
    Python. `registry.py` builds one of these for every sources/*.yaml without a
    `plugin:` key."""

    def __init__(self, spec: dict[str, Any], config: Config) -> None:
        self.id: str = spec["id"]
        self.name: str = spec.get("name", self.id)
        self.enabled: bool = bool(spec.get("enabled", True))
        self.priority: int = int(spec.get("priority", 50))
        self.fetcher: str = spec.get("fetcher", "http")
        self.rate_limit_seconds: float = float(
            spec.get("rate_limit_seconds", config.fetch.default_rate_limit_seconds)
        )

        listing = spec.get("listing") or {}
        self._urls: list[str] = list(listing.get("urls") or [])
        pagination = listing.get("pagination") or {}
        self._page_param: str | None = pagination.get("param")
        self._page_start: int = int(pagination.get("start", 1))
        self._page_max: int = int(pagination.get("max", 1))
        self._stop_when_empty: bool = bool(pagination.get("stop_when_empty", True))
        self._item_selector: str | None = listing.get("item_selector")
        self._json_path: str | None = listing.get("json_path")

        self._fields: dict[str, dict[str, Any]] = spec.get("fields") or {}
        self._transforms: dict[str, list[str]] = spec.get("transforms") or {}
        # Set by parse() after each page, read by discover() before yielding the next one
        # — see discover()'s docstring for why this is the only way stop_when_empty can
        # work within the existing discover()/parse() protocol (SPEC 6.2 is fixed).
        self._page_had_items = True

        if self.enabled:
            missing = [f for f in _MANDATORY_RAW_EVENT_FIELDS if f not in self._fields]
            if missing:
                raise ConfigError(f"source {self.id!r}: fields {missing} are not optional")

    def discover(self) -> Iterable[FetchTask]:
        """SPEC 6.2: "pagination unfolds here." Whether page N+1 is worth fetching depends
        on what page N returned, which this generator cannot know in advance — so it
        checks `self._page_had_items`, which parse() sets as a side effect on the
        previous page, right before yielding each subsequent page. This relies on
        discover() and parse() being driven by one sequential for-loop (cli.py's
        _fetch_source, package 10), which is the only consumer of the Source protocol."""
        if not self._urls:
            log.warning("no_listing_urls", source_id=self.id)
            return
        for url in self._urls:
            self._page_had_items = True
            if self._page_param is None:
                yield FetchTask(url=url)
                continue
            for page in range(self._page_start, self._page_start + self._page_max):
                if self._stop_when_empty and not self._page_had_items:
                    break
                yield FetchTask(url=url, params={self._page_param: page})

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        items = list(self._extract_items(result))
        self._page_had_items = bool(items)
        for item in items:
            event = self._parse_item(item, result.task.url)
            if event is not None:
                yield event

    def _extract_items(self, result: FetchResult) -> list[Any]:
        if self.fetcher == "api":
            if not self._json_path or result.json is None:
                return []
            return resolve_json_path(result.json, self._json_path)
        if not self._item_selector:
            return []
        return HTMLParser(result.text).css(self._item_selector)

    def _parse_item(self, item: Any, listing_url: str) -> RawEvent | None:
        values: dict[str, Any] = {}
        for field_name, field_spec in self._fields.items():
            value = self._extract_field(item, field_spec)
            if value:
                transforms = self._transforms.get(field_name, [])
                value = apply_transforms(value, transforms, listing_url)
                if field_spec.get("absolute") and value:
                    value = urljoin(listing_url, value)
            if not value:
                if not field_spec.get("optional", False):
                    log.warning("declarative_field_missing", source_id=self.id, field=field_name)
                    return None
                value = None
            values[field_name] = value

        return RawEvent(
            source_id=self.id,
            source_event_key=values["url"],
            title=values["title"],
            url=values["url"],
            description=values.get("description"),
            start_raw=values.get("start_raw"),
            end_raw=values.get("end_raw"),
            venue_name=values.get("venue_name"),
            address_raw=values.get("address_raw"),
            postal_code=values.get("postal_code"),
            district_raw=values.get("district_raw"),
            lat=values.get("lat"),
            lon=values.get("lon"),
            price_raw=values.get("price_raw"),
            image_url=values.get("image_url"),
            native_category=values.get("native_category"),
            url_category=values.get("url_category"),
        )

    def _extract_field(self, item: Any, field_spec: dict[str, Any]) -> str | None:
        if self.fetcher == "api":
            path = field_spec.get("path")
            if not path:
                return None
            value = resolve_json_path_value(item, path)
            return None if value is None else str(value)

        selector = field_spec.get("selector")
        node: Node | None = item if not selector else item.css_first(selector)
        if node is None:
            return None
        attr = field_spec.get("attr", "text")
        if attr == "text":
            text = node.text(deep=True, separator=" ", strip=True)
            return text or None
        if attr == "html":
            return node.html
        return node.attributes.get(attr)


def apply_transforms(value: str, transforms: list[str], base_url: str) -> str:
    """SPEC 6.3's fixed transform vocabulary, run left to right — nothing beyond these
    six (requirement 1)."""
    for transform in transforms:
        name, _, arg = transform.partition(":")
        if name == "html_unescape":
            value = html.unescape(value)
        elif name == "strip":
            value = value.strip()
        elif name == "lower":
            value = value.lower()
        elif name == "truncate":
            value = value[: int(arg)]
        elif name == "absolute_url":
            value = urljoin(base_url, value)
        elif name == "regex":
            pattern, _, group = arg.rpartition(":")
            match = re.search(pattern, value)
            if match:
                value = match.group(int(group))
        else:
            log.warning("unknown_transform", transform=name)
    return value


def resolve_json_path(data: Any, path: str) -> list[Any]:
    """The listing-level `json_path`, e.g. "data.events[*]" — a dotted walk down to a
    list, iterated at the trailing `[*]`. Not a general JSONPath implementation: SPEC
    6.3's only example is exactly this shape, and no allowed dependency provides real
    JSONPath (SPEC 15) — see the package 11 report. A `[*]` anywhere but the last
    segment is rejected rather than silently truncating the walk."""
    segments = path.split(".")
    target = data
    for index, segment in enumerate(segments):
        iterate = segment.endswith("[*]")
        if iterate and index != len(segments) - 1:
            log.warning("unsupported_json_path", path=path, reason="[*] must be the last segment")
            return []
        key = segment[:-3] if iterate else segment
        if key:
            if not isinstance(target, dict):
                return []
            target = target.get(key)
        if iterate:
            return target if isinstance(target, list) else []
    return target if isinstance(target, list) else []


def resolve_json_path_value(data: Any, path: str) -> Any:
    """The field-level `path`, e.g. "title" or "address.zip" — a plain dotted walk to one
    scalar within a single already-selected item."""
    target = data
    for segment in path.split("."):
        if not isinstance(target, dict):
            return None
        target = target.get(segment)
    return target
