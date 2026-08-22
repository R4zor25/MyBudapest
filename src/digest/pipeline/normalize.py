from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

import structlog

from digest.config import Config
from digest.models import (
    Event,
    RawEvent,
    district_from_zip,
    fold_text,
    make_event_id,
    roman_district,
)

log = structlog.get_logger()

_WHITESPACE_RE = re.compile(r"\s+")
_FREE_RE = re.compile(r"ingyenes|free|díjtalan|dijtalan", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\d[\d\s.]*")  # \s already covers the non-breaking space
_NON_DIGIT_RE = re.compile(r"\D")

# `fromisoformat` covers "2026-08-14 19:00:00" and every ISO 8601 shape with an offset;
# these are the Hungarian display formats it cannot read (§7.1). The dotted-without-spaces
# pair ("%Y.%m.%d.") was added for package 11's declarative sources — Port.hu's own format
# goes through fromisoformat and never touched this list until a real site needed it.
# Each format is paired with whether it carries a clock. A date-only format yields
# midnight, and that midnight must not be read as a time (§7.1) — it is the absence of one.
_DISPLAY_FORMATS: tuple[tuple[str, bool], ...] = (
    ("%Y. %m. %d. %H:%M", True),
    ("%Y. %m. %d. %H:%M:%S", True),
    ("%Y. %m. %d.", False),
    ("%Y.%m.%d. %H:%M", True),
    ("%Y.%m.%d.", False),
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


def parse_datetime(value: str, tz: ZoneInfo) -> tuple[datetime, bool] | None:
    """`(start, time_known)`, or None if nothing matched.

    Naive input is Budapest local time, not UTC: every source in the list publishes local
    times, and reading them as UTC would move an evening concert to the afternoon.

    `time_known` is decided HERE, by which format matched, and nowhere else. A source that
    publishes "2026.09.19." carries no clock, so the 00:00 that comes out is a missing
    value; a source that publishes "2026-08-16 00:00:00" is stating midnight. The two are
    identical afterwards, which is why the answer has to travel with the value instead of
    being recovered from it later (§7.1)."""
    text = value.strip()
    if not text:
        return None
    return _parse_any_format(text, tz)


def _parse_any_format(text: str, tz: ZoneInfo) -> tuple[datetime, bool] | None:
    # A bare ISO date parses as a datetime too, so it is tested first and separately —
    # `date.fromisoformat` rejects anything carrying a clock, which makes this a decision
    # about the input's shape rather than about the value it produced.
    try:
        day = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        return _localize(datetime(day.year, day.month, day.day), tz), False  # noqa: DTZ001

    try:
        return _localize(datetime.fromisoformat(text), tz), True
    except ValueError:
        pass
    for fmt, time_known in _DISPLAY_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)  # noqa: DTZ007 — _localize attaches the zone
        except ValueError:
            continue
        return _localize(parsed, tz), time_known
    # The Hungarian long form's regex requires HH:MM, so a match always carries a clock.
    long_form = _parse_hungarian_long_date(text)
    return (_localize(long_form, tz), True) if long_form is not None else None


def _localize(value: datetime, tz: ZoneInfo) -> datetime:
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


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


# Every Budapest district in the project's canonical spelling, reversed. Built from
# `roman_district` rather than written out, so the two can never disagree; that function
# returns None past the last real district, which is what ends the comprehension.
_ROMAN_TO_NUMBER: dict[str, int] = {
    roman.rstrip("."): number for number in range(1, 40) if (roman := roman_district(number) or "")
}
_ROMAN_DISTRICT_RE = re.compile(r"^([IVX]+)\.?(?:\s*ker(?:ület)?\.?)?$", re.IGNORECASE)
_LEADING_NUMBER_RE = re.compile(r"^(\d{1,2})\b")
_POSTAL_CODE_RE = re.compile(r"^\d{4}$")


def normalize_district(value: str | int | None) -> str | None:
    """Any shape a source publishes a Budapest district in -> the canonical Roman form.

        11                        -> "XI."
        "XI." / "XI" / "xi ker."  -> "XI."
        "IX. kerület"             -> "IX."
        "9. kerület - Ferencváros" -> "IX."
        "1113"                    -> "XI."

    §7.7 compares `district == home.district` by equality, so a source that spells it any
    other way silently never earns the proximity bonus — the failure is invisible, which
    is why this is one function every source goes through rather than a per-source mapping.

    Anything unrecognised is None, never a guess: a wrong district is worse than a missing
    one, because it scores."""
    if value is None:
        return None
    if isinstance(value, int):
        return roman_district(value)

    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    if not text:
        return None

    # A four-digit run is a postal code and is read as one, terminally. Falling through to
    # the leading-number branch would turn Szigethalom's 2315 into district XXIII.
    if _POSTAL_CODE_RE.match(text):
        return _log_unknown(text) if district_from_zip(text) is None else district_from_zip(text)

    roman = _ROMAN_DISTRICT_RE.match(text)
    if roman is not None:
        number = _ROMAN_TO_NUMBER.get(roman[1].upper())
        return roman_district(number) if number is not None else _log_unknown(text)

    # "13. kerület", "9. kerület - Ferencváros", "5" — the district is the leading number.
    leading = _LEADING_NUMBER_RE.match(text)
    if leading is not None:
        return roman_district(int(leading[1])) or _log_unknown(text)

    return _log_unknown(text)


def _log_unknown(text: str) -> None:
    log.debug("district_unrecognised", value=text)


def _district(raw: RawEvent) -> str | None:
    """The source's own district field first, then the postal code — both through the same
    normalizer, so a source needs to supply either one and never its own conversion."""
    return normalize_district(raw.district_raw) or normalize_district(raw.postal_code)


_BUDAPEST = "Budapest"
_ZIP_IN_TEXT_RE = re.compile(r"\b(\d{4})\b")
_BUDAPEST_RE = re.compile(r"(?<!\w)budapest(?!\w)")


def _city(raw: RawEvent) -> str | None:
    """The settlement, for §7.6's geographic cut. Three sources of truth, in descending
    order of trust — and `None` wherever none of them applies, because "unknown" is a real
    answer here: §7.6 keeps unknown-city events by default rather than guessing.

    1. What the source says (`RawEvent.city`) — cooltix, kvizestek and tokenklub
       populate it. First because a stated city must never be overruled by an address
       string.
    2. The postal code, from the field or from the address text. `1XYZ` is Budapest
       (§7.1). A four-digit code that is NOT Budapest's proves the event is elsewhere, but
       naming that settlement would need a gazetteer this project does not carry — so it
       returns None, not a guess.
    3. Only when there is no readable postal code at all: the word "Budapest" in the
       address. Kept behind the postal-code check on purpose, so "9026 Győr, Budapest út
       5." cannot be read as a Budapest address. This branch assumes the first four-digit
       run in the address is its postal code, which is true of every address the current
       sources publish but is not robust in general — a house number of "2024" would read
       as a postal code and leave the city unknown. Unknown fails open (§7.6), so the cost
       is a missed match, never a wrong exclusion.
    """
    if raw.city and raw.city.strip():
        return _canonical_city(raw.city.strip())

    zip_code = raw.postal_code or _first_zip(raw.address_raw)
    if zip_code:
        return _BUDAPEST if district_from_zip(zip_code) is not None else None

    if raw.address_raw and _BUDAPEST_RE.search(fold_text(raw.address_raw)):
        return _BUDAPEST
    return None


def _canonical_city(city: str) -> str:
    """A stated city is authoritative and is otherwise passed through untouched — but §7.6
    compares it for exact equality, so "Budapest XI.", "Budapest, XI. kerület" and
    "Budapest 1117" would each read as a different settlement and get the event excluded.
    The district is a separate field; the settlement is Budapest. Word-anchored, so a name
    merely starting with those letters is not swallowed."""
    if _BUDAPEST_RE.match(fold_text(city)):
        return _BUDAPEST
    return city


def _first_zip(text: str | None) -> str | None:
    match = _ZIP_IN_TEXT_RE.search(text or "")
    return match[1] if match else None


def _effective_date(start: datetime, start_time_known: bool, config: Config) -> date:
    """§7.7's night shift: a 02:00 festival set belongs to the previous evening. It applies
    only when the clock reading is real. Shifting a source that published a bare date files
    it a day early — the shift would be reading a missing value as "just after midnight",
    which is the one thing 00:00 does not mean there."""
    if not start_time_known:
        return start.date()
    return (start - timedelta(hours=config.night_shift.before_hour)).date()


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
    parsed_start = parse_datetime(raw.start_raw or "", tz)
    if parsed_start is None:
        log.warning(
            "unparseable_start",
            source=raw.source_id,
            key=raw.source_event_key,
            value=raw.start_raw,
        )
        return None
    start, start_time_known = parsed_start

    parsed_end = parse_datetime(raw.end_raw, tz) if raw.end_raw else None
    end = parsed_end[0] if parsed_end else None
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
        start_time_known=start_time_known,
        effective_date=_effective_date(start, start_time_known, config),
        venue_name=venue_name,
        city=_city(raw),
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
