from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

import structlog

from digest.config import Config
from digest.models import Event, RawEvent, district_from_zip, make_event_id, roman_district

log = structlog.get_logger()

_WHITESPACE_RE = re.compile(r"\s+")
_FREE_RE = re.compile(r"ingyenes|free|díjtalan|dijtalan", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\d[\d\s.]*")  # \s already covers the non-breaking space
_NON_DIGIT_RE = re.compile(r"\D")

# `fromisoformat` covers "2026-08-14 19:00:00" and every ISO 8601 shape with an offset;
# these are the Hungarian display formats it cannot read (§7.1). The dotted-without-spaces
# pair ("%Y.%m.%d.") was added for package 11's declarative sources — Port.hu's own format
# goes through fromisoformat and never touched this list until a real site needed it.
_DISPLAY_FORMATS = (
    "%Y. %m. %d. %H:%M",
    "%Y. %m. %d. %H:%M:%S",
    "%Y. %m. %d.",
    "%Y.%m.%d. %H:%M",
    "%Y.%m.%d.",
)

# strptime has no locale-independent way to read a spelled-out month name, and setting the
# process locale is exactly the non-portable approach this project avoids elsewhere (see
# render/email.py's own hardcoded month/weekday tables) — so this is a small fixed lookup,
# not a general solution, matching what package 11's bigcitylife.hu fixture actually prints:
# "2026. augusztus 16., vasárnap 18:00".
_HUN_MONTHS = {
    "január": 1,
    "február": 2,
    "március": 3,
    "április": 4,
    "május": 5,
    "június": 6,
    "július": 7,
    "augusztus": 8,
    "szeptember": 9,
    "október": 10,
    "november": 11,
    "december": 12,
}
_HUN_LONG_DATE_RE = re.compile(
    r"^(?P<year>\d{4})\.\s+(?P<month>\w+)\s+(?P<day>\d{1,2})\.,\s+\w+\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})$"
)

_DESCRIPTION_LIMIT = 400
_EARTH_RADIUS_KM = 6371.0


def parse_datetime(value: str, tz: ZoneInfo) -> datetime | None:
    """Naive input is Budapest local time, not UTC: every source in the list publishes
    local times, and reading them as UTC would move an evening concert to the afternoon."""
    text = value.strip()
    if not text:
        return None
    parsed = _parse_any_format(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _parse_any_format(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _DISPLAY_FORMATS:
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007 — the caller attaches the zone
        except ValueError:
            continue
    return _parse_hungarian_long_date(text)


def _parse_hungarian_long_date(text: str) -> datetime | None:
    match = _HUN_LONG_DATE_RE.match(text)
    if match is None:
        return None
    month = _HUN_MONTHS.get(match["month"].lower())
    if month is None:
        return None
    return datetime(  # noqa: DTZ001 — the caller attaches the zone
        int(match["year"]), month, int(match["day"]), int(match["hour"]), int(match["minute"])
    )


def parse_price(price_raw: str | None) -> tuple[int | None, int | None, bool]:
    """Returns `(price_min, price_max, is_free)`. An unreadable price is not an error —
    most sources simply do not publish one (§7.1)."""
    if not price_raw:
        return None, None, False
    text = price_raw.replace(" ", " ")
    if _FREE_RE.search(text):
        return 0, None, True
    amounts = [
        int(digits)
        for digits in (_NON_DIGIT_RE.sub("", match) for match in _AMOUNT_RE.findall(text))
        if digits
    ]
    if not amounts:
        return None, None, False
    if len(amounts) == 1:
        return amounts[0], None, False
    return min(amounts), max(amounts), False


def clean_description(text: str | None) -> str | None:
    if not text:
        return None
    collapsed = _WHITESPACE_RE.sub(" ", html.unescape(text)).strip()
    if not collapsed:
        return None
    if len(collapsed) <= _DESCRIPTION_LIMIT:
        return collapsed
    cut = collapsed[:_DESCRIPTION_LIMIT]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > 0 else cut).rstrip()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_r, lon1_r, lat2_r, lon2_r = map(radians, (lat1, lon1, lat2, lon2))
    a = (
        sin((lat2_r - lat1_r) / 2) ** 2
        + cos(lat1_r) * cos(lat2_r) * sin((lon2_r - lon1_r) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


def _district(raw: RawEvent) -> str | None:
    if isinstance(raw.district_raw, int):
        return roman_district(raw.district_raw)
    if isinstance(raw.district_raw, str) and raw.district_raw.strip():
        return raw.district_raw.strip()
    return district_from_zip(raw.postal_code)


def _distance_km(raw: RawEvent, config: Config) -> float | None:
    home = config.home
    if home is None or raw.lat is None or raw.lon is None:
        return None
    return round(haversine_km(home.lat, home.lon, raw.lat, raw.lon), 2)


def normalize(
    raw: list[RawEvent],
    config: Config,
    now: datetime | None = None,
) -> list[Event]:
    """`now` is injectable so the horizon and the past-event cut are testable; the rest of
    the stage is pure."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    horizon = moment + timedelta(days=config.schedule.horizon_days)

    events: list[Event] = []
    for item in raw:
        event = _normalize_one(item, config, tz, moment, horizon)
        if event is not None:
            events.append(event)
    return events


def _normalize_one(
    raw: RawEvent,
    config: Config,
    tz: ZoneInfo,
    now: datetime,
    horizon: datetime,
) -> Event | None:
    start = parse_datetime(raw.start_raw or "", tz)
    if start is None:
        log.warning(
            "unparseable_start",
            source=raw.source_id,
            key=raw.source_event_key,
            value=raw.start_raw,
        )
        return None

    end = parse_datetime(raw.end_raw, tz) if raw.end_raw else None
    if raw.end_raw and end is None:
        log.warning(
            "unparseable_end",
            source=raw.source_id,
            key=raw.source_event_key,
            value=raw.end_raw,
        )

    # An event that is still running is not in the past — a festival that opened in May is
    # dropped only once it is over, otherwise the recurrence stage would never see it.
    if (end or start) < now:
        log.info("dropped_past", source=raw.source_id, key=raw.source_event_key)
        return None
    if start > horizon:
        log.info("dropped_beyond_horizon", source=raw.source_id, key=raw.source_event_key)
        return None

    price_min, price_max, is_free = parse_price(raw.price_raw)
    title = _WHITESPACE_RE.sub(" ", raw.title).strip()
    venue_name = _WHITESPACE_RE.sub(" ", raw.venue_name).strip() if raw.venue_name else None

    return Event(
        id=make_event_id(title, start, venue_name),
        source_ids=[raw.source_id],
        urls=[raw.url],
        title=title,
        description=clean_description(raw.description),
        start=start,
        end=end,
        effective_date=(start - timedelta(hours=config.night_shift.before_hour)).date(),
        venue_name=venue_name,
        district=_district(raw),
        lat=raw.lat,
        lon=raw.lon,
        distance_km=_distance_km(raw, config),
        price_min=price_min,
        price_max=price_max,
        is_free=is_free,
        categories=[],
        native_categories=[raw.native_category] if raw.native_category else [],
        image_url=raw.image_url,
    )
