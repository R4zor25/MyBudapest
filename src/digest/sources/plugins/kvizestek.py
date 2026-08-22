from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import structlog

from digest.config import Config
from digest.errors import ParseError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent, district_from_zip

log = structlog.get_logger()

_EVENT_BASE_URL = "https://foglalas.kvizestek.hu/esemenyek"

# The booking API is nationwide ("kvízestek országszerte"): 41 of 132 sampled records are
# outside Budapest. §7.6's geographic stage is the authoritative rule and now has `city` to
# apply it, but that section keeps the source-level cut too — not carrying through what you
# would discard is politeness, not duplication. Matched on `venueCountry` as well, because
# one sampled record was Dunaszerdahely/SK.
_CITY = "budapest"
_COUNTRY = "HU"

# `eventDate` is a date marker pinned to noon UTC ("2026-08-22T12:00:00.000Z" on all 132
# sampled records) — reading it as an instant puts every event at 14:00 local. The real
# clock time is the separate `eventTime` field ("19:00"), so the two are recombined here
# into the naive local string §7.1 already knows how to read.
_DATE_MARKER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# Bulk-loaded records carry "[Excel import – 2026-07-15]" as their description — an
# internal provenance note, not a description. It reaches the reader in the email body and
# scores as keyword text, so it is dropped rather than shown.
_PLACEHOLDER_DESCRIPTION_RE = re.compile(r"^\[excel import", re.IGNORECASE)

_ZIP_RE = re.compile(r"\b(\d{4})\b")


def combine_start(event_date: str | None, event_time: str | None) -> str | None:
    """`("2026-08-22T12:00:00.000Z", "19:00")` -> `"2026-08-22 19:00:00"`. Returns None if
    either half is missing or malformed, which skips the record."""
    date_match = _DATE_MARKER_RE.match(str(event_date or ""))
    time_match = _TIME_RE.match(str(event_time or "").strip())
    if date_match is None or time_match is None:
        return None
    hour, minute = int(time_match[1]), int(time_match[2])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{date_match[1]} {hour:02d}:{minute:02d}:00"


def _is_budapest(record: dict[str, Any]) -> bool:
    """`venueCity` is the answer whenever the API gives one. It does not always: both
    "Szabadtéri kvíz a Margit-szigeten" records carry `venueCity: ""` with
    `venueAddress: "1138 Budapest, Perzsa varázsfa"`. Falling back to the address only
    when the city is blank keeps a stated city authoritative — a record that says Győr is
    never re-read out of its address."""
    if str(record.get("venueCountry") or _COUNTRY).strip().upper() != _COUNTRY:
        return False
    city = str(record.get("venueCity") or "").strip().casefold()
    if city:
        return city == _CITY
    address = str(record.get("venueAddress") or "")
    return _CITY in address.casefold() or district_from_zip(_postal_code(address)) is not None


def _postal_code(address: str | None) -> str | None:
    match = _ZIP_RE.search(str(address or ""))
    return match[1] if match else None


def _description(record: dict[str, Any]) -> str | None:
    text = str(record.get("description") or "").strip()
    if not text or _PLACEHOLDER_DESCRIPTION_RE.match(text):
        return None
    return text


def _price_raw(record: dict[str, Any]) -> str | None:
    """`entryFee` is null on 122 of 132 sampled records — an unpriced record means "not
    published", not "free", so it stays None rather than becoming a 0 that would earn the
    `free_bonus` (§7.7)."""
    fee = record.get("entryFee")
    if fee in (None, ""):
        return None
    currency = str(record.get("entryFeeCurrency") or "HUF")
    return f"{fee} {currency}"


class KvizestekSource:
    """kvizestek.hu's booking backend (§6.1 step 2). kvizestek.hu/esemenyek itself no
    longer lists anything — since February 2026 it only links to foglalas.kvizestek.hu,
    a React SPA whose events page reads one un-paginated JSON endpoint."""

    def __init__(self, spec: dict[str, Any], config: Config) -> None:
        self.id: str = spec["id"]
        self.name: str = spec.get("name", "Kvízestek")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.priority: int = int(spec.get("priority", 20))
        self.fetcher: str = spec.get("fetcher", "api")
        self.rate_limit_seconds: float = float(
            spec.get("rate_limit_seconds", config.fetch.default_rate_limit_seconds)
        )
        self._listing_urls: list[str] = list((spec.get("listing") or {}).get("urls") or [])

    def discover(self) -> Iterable[FetchTask]:
        # No pagination: the endpoint returns every upcoming event in one array and the
        # site's own filtering (city, venue, topic) happens client-side.
        for url in self._listing_urls:
            yield FetchTask(url=url)

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        payload = result.json
        if not isinstance(payload, list):
            raise ParseError(
                f"{self.id}: expected an array of events, got {type(payload).__name__}"
            )
        for index, record in enumerate(payload):
            event = self._parse_record(index, record)
            if event is not None:
                yield event

    def _parse_record(self, index: int, record: Any) -> RawEvent | None:
        if not isinstance(record, dict):
            self._skip(str(index), "record is not an object")
            return None

        key = str(record.get("id") or index)
        if record.get("isCancelled"):
            log.info("skipped_cancelled", source_id=self.id, key=key)
            return None
        if not _is_budapest(record):
            log.info(
                "skipped_outside_budapest",
                source_id=self.id,
                key=key,
                city=record.get("venueCity"),
            )
            return None

        title = str(record.get("title") or "").strip()
        slug = str(record.get("slug") or "").strip()
        if not title:
            self._skip(key, "missing title")
            return None
        if not slug:
            self._skip(key, "missing slug")
            return None

        start_raw = combine_start(record.get("eventDate"), record.get("eventTime"))
        if start_raw is None:
            self._skip(
                key,
                f"unusable eventDate/eventTime {record.get('eventDate')!r}/"
                f"{record.get('eventTime')!r}",
            )
            return None

        address = str(record.get("venueAddress") or "").strip() or None
        postal_code = _postal_code(address)

        return RawEvent(
            source_id=self.id,
            source_event_key=key,
            title=title,
            url=f"{_EVENT_BASE_URL}/{slug}",
            description=_description(record),
            start_raw=start_raw,
            # `eventEndTime` exists in the schema but was null on every sampled record.
            end_raw=None,
            venue_name=str(record.get("venueName") or "").strip() or None,
            # `venueCity` is blank on some records (both Margit-sziget ones), and a blank
            # stays None rather than being recovered from the address here: §7.1 already
            # derives what it can from the postal code and then the address, in that
            # order. Duplicating it would be a second implementation to keep in step.
            city=str(record.get("venueCity") or "").strip() or None,
            address_raw=address,
            postal_code=postal_code,
            price_raw=_price_raw(record),
            # `imageUrl` is set on 2 of 132 records and `venueImageUrl` is a venue photo,
            # not an event image — a generic bar interior on every card is worse than none.
            image_url=str(record.get("imageUrl") or "").strip() or None,
            # `eventType` is "public" on every record: an access flag, not a topic, so it
            # would only add a category signal that means nothing. `categories` was null
            # throughout, so there is no native category to carry either.
            native_category=None,
        )

    def _skip(self, key: str, reason: str) -> None:
        log.warning("record_skipped", source_id=self.id, key=key, reason=reason)


def build(spec: dict[str, Any], config: Config) -> KvizestekSource:
    return KvizestekSource(spec, config)
