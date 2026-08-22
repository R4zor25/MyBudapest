from __future__ import annotations

import os
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from selectolax.parser import HTMLParser

from digest.config import Config
from digest.errors import ConfigError, ParseError
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent, district_from_zip

log = structlog.get_logger()

_API_KEY_ENV = "GCAL_API_KEY"

# Every record on this calendar is a board-game club session, so the source states the
# topic instead of leaving it to be guessed from a title like "Visszapillantó 2010: Dixit"
# — see config.yaml's `tarsasjatek.native_types` (§7.5 scores a native type at 4.0).
_NATIVE_CATEGORY = "boardgame"

_CITY = "budapest"
_ZIP_RE = re.compile(r"\b(\d{4})\b")

# Google Calendar appends its own conferencing footer to every description on a calendar
# with Meet enabled — join link, dial-in number and PIN. It is not event copy: it would
# reach the newsletter body and score as keyword text.
_MEET_FOOTER_RE = re.compile(
    r"\n*(Csatlakozás a Google Meet szolgáltatással|Join with Google Meet)\b.*",
    re.IGNORECASE | re.DOTALL,
)
_WHITESPACE_RE = re.compile(r"\s+")

# "Board Game Café - Budapest" -> "Board Game Café". The city is repeated into the venue
# name on most records of this calendar; leaving it in would stop `venue_prior` matching,
# which compares venue names for exact equality after casefolding (§7.5).
_CITY_SUFFIX_RE = re.compile(r"\s*[-–]\s*Budapest\s*$", re.IGNORECASE)


class TarsasjatekosSource:
    """The Hungarian Board Gamers Association's national club calendar (§6.1 step 1 — a
    published calendar feed), read through the Google Calendar API rather than scraped.

    ON THE TRANSPORT. tarsasjatekos.hu/klubok.html publishes its clubs twice: as prose
    ("minden hónap 2. szombatján"), which carries no dates at all, and as an embedded
    Google Calendar. The calendar is the only dated form. Its public `.ics` export lives
    on `calendar.google.com`, whose robots.txt is `Allow: /$` + `Disallow: /` — so under
    `respect_robots_txt` the fetcher refuses it, and following the legacy
    `www.google.com/calendar/ical/...` redirect would just be routing around that. The
    Calendar API on `www.googleapis.com` serves the same public calendar, is the route
    Google publishes for programmatic reads, and that host has no robots.txt at all
    (404 -> allow all). It needs an API key, which is why this source carries one.

    `singleEvents=true` also makes the API expand recurrence server-side, so the weekly
    clubs ("Szenior társasjáték klub", "Közjáték") arrive as dated occurrences and this
    plugin never has to interpret an RRULE."""

    def __init__(self, spec: dict[str, Any], config: Config) -> None:
        self.id: str = spec["id"]
        self.name: str = spec.get("name", "Magyar Társasjátékos Egyesület")
        self.enabled: bool = bool(spec.get("enabled", True))
        self.priority: int = int(spec.get("priority", 20))
        self.fetcher: str = spec.get("fetcher", "api")
        self.rate_limit_seconds: float = float(
            spec.get("rate_limit_seconds", config.fetch.default_rate_limit_seconds)
        )
        self._listing_urls: list[str] = list((spec.get("listing") or {}).get("urls") or [])
        self._horizon_days: int = config.schedule.horizon_days
        self._timezone = ZoneInfo(config.schedule.timezone)

    def discover(self) -> Iterable[FetchTask]:
        """`timeMin`/`timeMax` bound the request to the digest's own horizon, so this pulls
        roughly a dozen records instead of the calendar's full year. Reading the clock is
        confined to here: parse() stays a pure function of the response, which is what
        lets the fixture test assert exact counts."""
        api_key = os.environ.get(_API_KEY_ENV)
        if not api_key:
            raise ConfigError(
                f"{self.id}: {_API_KEY_ENV} is not set. It is required to read the public "
                "calendar through the Google Calendar API; see sources/tarsasjatekos.yaml."
            )
        now = datetime.now(tz=self._timezone)
        for url in self._listing_urls:
            yield FetchTask(
                url=url,
                params={
                    "key": api_key,
                    # Expands recurring events into individual dated occurrences, and is a
                    # precondition for orderBy=startTime.
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "timeMin": now.isoformat(),
                    "timeMax": (now + timedelta(days=self._horizon_days)).isoformat(),
                    "maxResults": "250",
                },
            )

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        payload = result.json
        if not isinstance(payload, dict):
            raise ParseError(f"{self.id}: expected a JSON object, got {type(payload).__name__}")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ParseError(f"{self.id}: items missing or not a list")
        # A horizon of 14 days cannot overflow maxResults=250 on a calendar that published
        # 160 events in a year, so paging is not implemented — but if that ever stops being
        # true, this says so instead of silently truncating.
        if payload.get("nextPageToken"):
            log.warning("pagination_needed", source_id=self.id, horizon_days=self._horizon_days)
        for index, item in enumerate(items):
            event = self._parse_item(index, item)
            if event is not None:
                yield event

    def _parse_item(self, index: int, item: Any) -> RawEvent | None:
        if not isinstance(item, dict):
            self._skip(str(index), "item is not an object")
            return None

        key = str(item.get("id") or index)
        if item.get("status") == "cancelled":
            log.info("skipped_cancelled", source_id=self.id, key=key)
            return None

        title = str(item.get("summary") or "").strip()
        if not title:
            self._skip(key, "missing summary")
            return None

        start = item.get("start")
        start_raw = _timestamp(start)
        if start_raw is None:
            reason = (
                "all-day event has no clock time"
                if isinstance(start, dict) and start.get("date")
                else f"unusable start {start!r}"
            )
            self._skip(key, reason)
            return None

        location = str(item.get("location") or "").strip()
        postal_code = _postal_code(location)
        if not _is_budapest(location, postal_code):
            log.info("skipped_outside_budapest", source_id=self.id, key=key, location=location)
            return None

        return RawEvent(
            source_id=self.id,
            source_event_key=key,
            title=title,
            # Always present on a Calendar API record, which is what makes this source
            # usable at all: only 88 of the 160 events on the .ics carried a `URL:` line.
            url=str(item.get("htmlLink") or "").strip(),
            description=_description(item.get("description")),
            start_raw=start_raw,
            end_raw=_timestamp(item.get("end")),
            venue_name=_venue_name(location),
            address_raw=location or None,
            postal_code=postal_code,
            price_raw=None,
            image_url=None,
            native_category=_NATIVE_CATEGORY,
        )

    def _skip(self, key: str, reason: str) -> None:
        log.warning("record_skipped", source_id=self.id, key=key, reason=reason)


def _timestamp(slot: Any) -> str | None:
    """Only the timed shape, `{"dateTime": ...}`. A Calendar API all-day event is
    `{"date": "2026-08-29"}` with no clock time, and §7.1 would read that as 00:00 local —
    which `night_shift.before_hour: 5` then files under the *previous* day. That is exactly
    the defect commit 499bfc4 was written about, so an all-day record is skipped rather
    than given a midnight that the calendar never claimed. None of the 160 events sampled
    on this calendar used the all-day shape; if that changes, `record_skipped` says so."""
    if not isinstance(slot, dict):
        return None
    value = slot.get("dateTime")
    return str(value) if value else None


def _description(raw: Any) -> str | None:
    text = str(raw or "")
    if not text.strip():
        return None
    text = _MEET_FOOTER_RE.sub("", text)
    # Organisers paste rich text into this field: the Csepeli club's entry arrives as
    # `<br><a class="moz-txt-link-freetext" href="...">`. §7.1 unescapes but keeps tags.
    if "<" in text:
        text = HTMLParser(text).text(deep=True, separator=" ", strip=False)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def _venue_name(location: str) -> str | None:
    if not location:
        return None
    return _CITY_SUFFIX_RE.sub("", location.split(",")[0]).strip() or None


def _postal_code(location: str) -> str | None:
    for candidate in _ZIP_RE.findall(location):
        if district_from_zip(candidate) is not None:
            return candidate
    return None


def _is_budapest(location: str, postal_code: str | None) -> bool:
    """The calendar is national — Biatorbágy, Veszprém, Poroszló and Szigethalom all
    appear — and no pipeline stage filters on settlement. `location` is free text, so both
    a name check and a postal-code check are needed: "Board Game Café - Budapest" has no
    zip, and "Csörsz u. 18, Budapest, Csörsz u. 18, 1124 Hungary" has both."""
    return _CITY in location.casefold() or postal_code is not None


def build(spec: dict[str, Any], config: Config) -> TarsasjatekosSource:
    return TarsasjatekosSource(spec, config)
