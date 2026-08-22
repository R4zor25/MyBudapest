from __future__ import annotations

import difflib
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

# The three RawEvent fields a spec may NOT map, each with the reason it is refused. They
# are not typos, so they do not get a "did you mean" — they get an answer.
_UNMAPPABLE_FIELDS = {
    "source_id": "the engine fills it from the spec's own `id:`",
    "source_event_key": "the engine fills it from the mapped `url`",
    "extra": "it is a free-form dict, and no extraction produces one",
}

# Everything else on the model, derived from the model itself. A field added to RawEvent
# is mappable the same day, with nothing here to remember to update — which is the whole
# point: the hand-maintained alternative is what let `city` sit in tokenklub.yaml doing
# nothing. What this does NOT prove is that the engine passes the field on;
# `_build_event` still names each one, and the test that maps every field at once is what
# guards that half.
MAPPABLE_FIELDS = frozenset(RawEvent.model_fields) - set(_UNMAPPABLE_FIELDS)

# Every key a spec may carry at the top level, plugin-backed or not (§6.3). These are the
# runtime's own — `enabled`, `priority` and `fetcher` are read by the registry and the
# fetch layer, never by a plugin — so a plugin spec gets no exemption here. A typo in this
# block is the worst of the silent ignores: every other one loses a field, but `enabledd:
# true` makes a source's on/off state differ from what the YAML says, and the YAML is what
# a person reads to answer whether a source is running at all.
_TOP_LEVEL_KEYS = frozenset(
    {
        "id",
        "name",
        "enabled",
        "priority",
        "fetcher",
        "rate_limit_seconds",
        "plugin",
        "listing",
        "fields",
        "transforms",
    }
)

_LISTING_KEYS = frozenset({"urls", "pagination", "item_selector", "json_path"})
_PAGINATION_KEYS = frozenset({"param", "start", "max", "stop_when_empty"})
# What one entry under `fields:` may say. `selector`+`attr` is the http form, `path` the
# api one; `optional` and `absolute` apply to both. A typo here is the original bug's exact
# shape — `{ selctor: "h3" }` extracted nothing and said nothing about it.
_FIELD_SPEC_KEYS = frozenset({"selector", "attr", "path", "optional", "absolute"})

# SPEC 6.3's fixed transform vocabulary, by name — `truncate:400` and `regex:pat:1` carry
# an argument after the colon, so only the part before it is a name. This is a second copy
# of what `apply_transforms` dispatches on; the test that runs a value through every name
# in this set is what keeps the two from drifting apart.
TRANSFORM_NAMES = frozenset(
    {"html_unescape", "strip", "lower", "truncate", "absolute_url", "regex"}
)


def validate_spec(source_id: str, spec: dict[str, Any], *, declarative: bool) -> None:
    """Reject a key no part of the engine reads, at LOAD time (§6.3).

    An unrecognised key used to be extracted and dropped, and the symptom was a field that
    was quietly always empty — indistinguishable from a source that simply does not
    publish it. `city: { path: "venue.city" }` sat in tokenklub.yaml exactly that way. A
    typo has to fail where it is written, not turn into an absence somebody has to notice.

    What the `plugin:` exemption covers, and what it does not:

    - NOT exempt, for any spec: the top level, and `transforms:`. Those keys belong to the
      registry, the fetch layer and this engine, not to a plugin — cooltix's `enabled:` is
      read by exactly the same code as tokenklub's.
    - Exempt for a plugin spec: `listing:` and `fields:`. A plugin parses its own
      responses and drives its own pagination, so those blocks carry its vocabulary, not
      §6.3's — cooltix reads `listing.pagination.page_size`, which means nothing here, and
      validating it against this list would reject a working source.

    Called from `registry.load_sources` for every spec and again from
    `DeclarativeSource.__init__`, so a source built directly is checked too. It is a pure
    function of the spec, so running it twice costs nothing and means neither entry point
    can be the unguarded one."""
    _reject_unknown(source_id, "the top level", spec, _TOP_LEVEL_KEYS)

    if declarative:
        listing = spec.get("listing") or {}
        _reject_unknown(source_id, "listing:", listing, _LISTING_KEYS)
        _reject_unknown(
            source_id, "listing.pagination:", listing.get("pagination") or {}, _PAGINATION_KEYS
        )
        fields = spec.get("fields") or {}
        _reject_unknown(source_id, "fields:", fields, MAPPABLE_FIELDS)
        for field, field_spec in fields.items():
            # A non-dict entry has no keys to check; it still fails later, exactly as it
            # does today. This validator's job is unknown keys, not the wrong shape.
            if isinstance(field_spec, dict):
                _reject_unknown(source_id, f"fields.{field}:", field_spec, _FIELD_SPEC_KEYS)

    transforms = spec.get("transforms") or {}
    _reject_unknown(source_id, "transforms:", transforms, MAPPABLE_FIELDS)
    for field, names in transforms.items():
        for transform in names or []:
            name = str(transform).partition(":")[0]
            if name not in TRANSFORM_NAMES:
                raise ConfigError(
                    f"source {source_id!r}: unknown transform {name!r} on "
                    f"transforms.{field}{_suggest(name, TRANSFORM_NAMES)}"
                )


def _reject_unknown(
    source_id: str, section: str, block: dict[str, Any], valid: frozenset[str]
) -> None:
    for key in block:
        if key in valid:
            continue
        reason = _UNMAPPABLE_FIELDS.get(key) if valid is MAPPABLE_FIELDS else None
        if reason is not None:
            raise ConfigError(
                f"source {source_id!r}: {section[:-1]}.{key} cannot be mapped — {reason}"
            )
        raise ConfigError(
            f"source {source_id!r}: unknown key {key!r} under {section}{_suggest(key, valid)}"
        )


def _suggest(key: str, valid: frozenset[str]) -> str:
    close = difflib.get_close_matches(key, sorted(valid), n=1, cutoff=0.6)
    if close:
        return f" — did you mean {close[0]!r}?"
    return f" — valid: {', '.join(sorted(valid))}"


class DeclarativeSource:
    """A Source built entirely from YAML (SPEC 6.3) — adding a static source needs no
    Python. `registry.py` builds one of these for every sources/*.yaml without a
    `plugin:` key."""

    def __init__(self, spec: dict[str, Any], config: Config) -> None:
        self.id: str = spec["id"]
        # Before anything is read out of the spec, and regardless of `enabled`: a typo in
        # a source that is switched off is still a typo, and it should not be waiting to
        # surface on the day someone switches the source on.
        validate_spec(self.id, spec, declarative=True)
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
            city=values.get("city"),
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
