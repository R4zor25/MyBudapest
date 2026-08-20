from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import structlog

from digest.config import Config
from digest.errors import ParseError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent, district_from_zip

log = structlog.get_logger()

_BASE_URL = "https://port.hu"
_START_FORMAT = "%Y-%m-%d %H:%M:%S"
_END_FORMAT = "%m. %d. %H:%M"


def resolve_end(end_display: str, start: datetime) -> str | None:
    """The `end` display string carries no year (`" - 08. 21. 23:59"`). Take the start's
    year, and roll over when that would place the end before the start (§6.5)."""
    text = end_display.strip().lstrip("-").strip()
    if not text:
        return None
    try:
        # DTZ007 suppressed: Port.hu sends local times without a zone (§6.5), and
        # attaching Europe/Budapest is the normalizer's job (§7.1). Here the two only
        # have to be comparable, so the end borrows whatever the start carries.
        parsed = datetime.strptime(text, _END_FORMAT)  # noqa: DTZ007
    except ValueError:
        log.warning("end_unparseable", value=end_display)
        return None
    end = parsed.replace(year=start.year, tzinfo=start.tzinfo)
    if end < start:
        end = end.replace(year=start.year + 1)
    return end.strftime(_START_FORMAT)


def _url_category(url: str) -> str | None:
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    return segments[1] if len(segments) > 1 else None


def _geo_point(address: dict[str, Any]) -> tuple[float | None, float | None]:
    point = (address.get("gps") or {}).get("geoPoint") or {}
    lat = point.get("lat")
    lon = point.get("lon")
    return (lat if isinstance(lat, (int, float)) else None), (
        lon if isinstance(lon, (int, float)) else None
    )


class PortHuSource:
    def __init__(self, spec: dict[str, Any], config: Config) -> None:
        self.id: str = spec["id"]
        self.name: str = spec.get("name", "Port.hu")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.priority: int = int(spec.get("priority", 10))
        self.fetcher: str = spec.get("fetcher", "api")
        self.rate_limit_seconds: float = float(
            spec.get("rate_limit_seconds", config.fetch.default_rate_limit_seconds)
        )
        self._listing_urls: list[str] = list((spec.get("listing") or {}).get("urls") or [])

    def discover(self) -> Iterable[FetchTask]:
        if not self._listing_urls:
            log.warning(
                "no_listing_urls",
                source_id=self.id,
                reason="listing endpoint is still an open question (SPEC 17.1)",
            )
        for url in self._listing_urls:
            yield FetchTask(url=url)

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        payload = result.json
        if not isinstance(payload, dict):
            raise ParseError(
                f"{self.id}: expected an object keyed by event id, got {type(payload).__name__}"
            )
        for key, record in payload.items():
            event = self._parse_record(str(key), record)
            if event is not None:
                yield event

    def _parse_record(self, key: str, record: Any) -> RawEvent | None:
        if not isinstance(record, dict):
            self._skip(key, "record is not an object")
            return None

        event_key = record.get("id") or key
        title = record.get("title")
        path = record.get("url")
        if not title:
            self._skip(key, "missing title")
            return None
        if not path:
            self._skip(key, "missing url")
            return None

        start_raw = record.get("eventStart")
        try:
            # DTZ007 suppressed: see resolve_end — the source is tz-naive by design.
            start = datetime.strptime(str(start_raw), _START_FORMAT)  # noqa: DTZ007
        except ValueError:
            self._skip(key, f"unparseable eventStart {start_raw!r}")
            return None

        url = f"{_BASE_URL}{path}" if str(path).startswith("/") else str(path)
        address = record.get("address") or {}
        lat, lon = _geo_point(address)
        description = record.get("description")
        zip_code = address.get("zip")
        district = address.get("district")

        return RawEvent(
            source_id=self.id,
            source_event_key=str(event_key),
            title=str(title),
            url=url,
            description=html.unescape(str(description)).strip() if description else None,
            start_raw=str(start_raw),
            end_raw=resolve_end(str(record.get("end") or ""), start),
            venue_name=record.get("place") or None,
            address_raw=address.get("fullAddress") or None,
            postal_code=str(zip_code) if zip_code else None,
            district_raw=district if isinstance(district, int) else district_from_zip(zip_code),
            lat=lat,
            lon=lon,
            # The `ticket` array is empty on every record: Port.hu carries no price (§6.5).
            price_raw=None,
            # `thumbnail` only. `gallery` holds up to 24 loosely related images and never
            # reaches a RawEvent — that rule is what keeps ~90% of the payload out (§6.5).
            image_url=record.get("thumbnail") or None,
            native_category=record.get("type") or None,
            url_category=_url_category(url),
        )

    def _skip(self, key: str, reason: str) -> None:
        log.warning("record_skipped", source_id=self.id, key=key, reason=reason)


def build(spec: dict[str, Any], config: Config) -> PortHuSource:
    return PortHuSource(spec, config)
