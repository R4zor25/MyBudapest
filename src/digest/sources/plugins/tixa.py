from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

import structlog
from selectolax.parser import HTMLParser

from digest.config import Config
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent, district_from_zip

log = structlog.get_logger()

# Tixa renders one JSON-LD block per page region. Only these two are event listings —
# `pageConfig`, `organizerData` and `locationData` are page furniture.
_EVENT_LIST_IDS = frozenset({"organizerEvents", "locationEvents"})

# The site serves English copy unless asked otherwise, and `pageConfig.language` follows
# the request. Without this the digest would carry "Board game night at Treffort Kert"
# in an otherwise Hungarian newsletter.
_ACCEPT_LANGUAGE = "hu-HU,hu;q=0.9"

_ZIP_RE = re.compile(r"\b(\d{4})\b")
_CITY = "budapest"

# See the class docstring: a start of exactly 00:00:00 is Tixa's "no time recorded"
# placeholder, not a midnight event.
_MIDNIGHT_RE = re.compile(r"T00:00:00")


class TixaSource:
    """tixa.hu organizer and venue pages (§6.1 step 3). The listing itself is server-side
    JSON-LD, which is why this reads the markup and not the `POST /search` endpoint the
    site's own search box uses: that endpoint returns `startDate` as Hungarian prose
    ("2026. augusztus 24. 17:00"), which §7.1 cannot read.

    ON THE MIDNIGHT PLACEHOLDER. Tixa's `startDate` is machine-readable but frequently
    wrong: on the sampled Dürer Kert page 16 of 24 records carried `T00:00:00` while the
    real clock time appeared only in `customDate`, as prose with no weekday
    ("2026. augusztus 22. 19:00") — a shape neither `_DISPLAY_FORMATS` nor
    `_HUN_LONG_DATE_RE` matches. The event detail pages repeat the same `T00:00:00`, so
    there is no second source to fall back on. Reading `customDate` would mean writing a
    Hungarian prose date parser, which §6.1 and this batch's brief both rule out. So a
    midnight start is dropped and logged rather than published as an event that claims to
    begin at 00:00. That is also why `listing.urls` is a short hand-picked list and not
    every Budapest venue: widening it imports the defect at scale."""

    def __init__(self, spec: dict[str, Any], config: Config) -> None:
        self.id: str = spec["id"]
        self.name: str = spec.get("name", "Tixa")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.priority: int = int(spec.get("priority", 35))
        self.fetcher: str = spec.get("fetcher", "http")
        self.rate_limit_seconds: float = float(
            spec.get("rate_limit_seconds", config.fetch.default_rate_limit_seconds)
        )
        self._listing_urls: list[str] = list((spec.get("listing") or {}).get("urls") or [])

    def discover(self) -> Iterable[FetchTask]:
        if not self._listing_urls:
            log.warning("no_listing_urls", source_id=self.id)
            return
        for url in self._listing_urls:
            yield FetchTask(url=url, headers={"Accept-Language": _ACCEPT_LANGUAGE})

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        for item in _event_items(result.text, self.id):
            event = self._parse_item(item)
            if event is not None:
                yield event

    def _parse_item(self, item: dict[str, Any]) -> RawEvent | None:
        url = str(item.get("url") or "").strip()
        title = str(item.get("name") or "").strip()
        if not url or not title:
            self._skip(url or title or "?", "missing url or name")
            return None

        start_raw = str(item.get("startDate") or "").strip()
        if not start_raw:
            self._skip(url, "missing startDate")
            return None
        if _MIDNIGHT_RE.search(start_raw):
            log.warning(
                "skipped_placeholder_start",
                source_id=self.id,
                url=url,
                start_raw=start_raw,
                custom_date=item.get("customDate"),
            )
            return None

        location = item.get("location") or {}
        address = str(location.get("address") or "").strip() or None
        if address and _CITY not in address.casefold():
            log.info("skipped_outside_budapest", source_id=self.id, url=url, address=address)
            return None

        postal_code = _postal_code(address)
        images = item.get("image")
        image_url = str(images[0]).strip() if isinstance(images, list) and images else None

        return RawEvent(
            source_id=self.id,
            source_event_key=url,
            title=title,
            url=url,
            # The listing carries no blurb — only name, place, times and a poster.
            description=None,
            start_raw=start_raw,
            end_raw=str(item.get("endDate") or "").strip() or None,
            venue_name=str(location.get("name") or "").strip() or None,
            address_raw=address,
            postal_code=postal_code,
            district_raw=district_from_zip(postal_code),
            price_raw=None,
            image_url=image_url,
            native_category=None,
        )

    def _skip(self, key: str, reason: str) -> None:
        log.warning("record_skipped", source_id=self.id, key=key, reason=reason)


def _event_items(html: str, source_id: str) -> Iterable[dict[str, Any]]:
    """Every `Event` inside the page's `organizerEvents` / `locationEvents` ItemLists."""
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        text = node.text(deep=True, strip=False)
        if not text:
            continue
        try:
            block = json.loads(text)
        except ValueError:
            log.warning("ld_json_unparseable", source_id=source_id)
            continue
        if not isinstance(block, dict) or block.get("@id") not in _EVENT_LIST_IDS:
            continue
        for entry in block.get("itemListElement") or []:
            item = entry.get("item") if isinstance(entry, dict) else None
            if isinstance(item, dict):
                yield item


def _postal_code(address: str | None) -> str | None:
    match = _ZIP_RE.search(address or "")
    return match[1] if match else None


def build(spec: dict[str, Any], config: Config) -> TixaSource:
    return TixaSource(spec, config)
