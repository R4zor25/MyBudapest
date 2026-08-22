from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from rapidfuzz.fuzz import token_set_ratio

_SEPARATOR_RE = re.compile(r"\s+[|-]\s+")
_TRAILING_PARENS_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
_WHITESPACE_RE = re.compile(r"\s+")

# A parenthesised suffix is dropped only when it is short enough to be a country or city
# tag: "(HU)" is noise, "(Deluxe Anniversary Edition)" is part of the title (§4.1).
_MAX_PARENTHESISED_SUFFIX_TOKENS = 2

# Same primitive and threshold the fuzzy dedup stage uses for venues (§7.2).
_VENUE_SUFFIX_MATCH_RATIO = 85

_ROMAN_TENS = ("", "X", "XX")
_ROMAN_UNITS = ("", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX")
_MAX_DISTRICT = 23


class RawEvent(BaseModel):
    """What a source yields, before normalization. Every field is optional except the
    identifying ones; handling or dropping incomplete records is the normalizer's job."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    source_event_key: str
    title: str
    url: str
    description: str | None = None
    start_raw: str | None = None
    end_raw: str | None = None
    venue_name: str | None = None
    address_raw: str | None = None
    # The settlement as the source states it. Optional and unset by every source today:
    # the geographic filter stage (§7.6) reads it, and sources map it as they are revisited
    # — cooltix (`venue.address.city`), kvizestek (`venueCity`) and tokenklub
    # (`venue.city`) all publish one. Until then normalize derives what it can from the
    # postal code (§7.1).
    city: str | None = None
    postal_code: str | None = None
    district_raw: int | str | None = None
    lat: float | None = None
    lon: float | None = None
    price_raw: str | None = None
    image_url: str | None = None
    native_category: str | None = None
    url_category: str | None = None
    extra: dict[str, Any] = {}


class Event(BaseModel):
    id: str
    source_ids: list[str]
    urls: list[str]
    title: str
    description: str | None
    start: datetime
    end: datetime | None
    effective_date: date
    is_series: bool = False
    venue_name: str | None
    # Defaulted, unlike `district`, so that the many Event(...) call sites that predate
    # the geographic filter keep working: an absent city means "unknown", which §7.6
    # treats as keep-by-default, not as a missing required value.
    city: str | None = None
    district: str | None
    lat: float | None
    lon: float | None
    distance_km: float | None
    price_min: int | None
    price_max: int | None
    is_free: bool = False
    categories: list[str]
    # Every source-native type this event was tagged with (Port.hu "concert" and so on).
    # Not in SPEC §4's original list: §7.5's native_types signal needs it and RawEvent
    # already carries it, so it survives normalize instead of being read back from nowhere.
    native_categories: list[str] = []
    image_url: str | None
    score: float = 0.0
    score_breakdown: dict[str, float] = {}
    group_key: str | None = None
    group_size: int = 1


def _strip_accents(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(s: str) -> str:
    """Deliberately conservative — it never cuts at a separator (§4.1). Which side of a
    separator carries the venue varies per record, so cutting either end fuses distinct
    events into one id, and the ledger then silences the second one forever."""
    s = _strip_accents(s).lower()
    while (match := _TRAILING_PARENS_RE.search(s)) and (
        len(match.group(1).split()) <= _MAX_PARENTHESISED_SUFFIX_TOKENS
    ):
        s = s[: match.start()]
    return _WHITESPACE_RE.sub(" ", s).strip()


def strip_venue_suffix(title: str, venue: str | None) -> str:
    """Only the fuzzy dedup stage may call this (§7.2); `make_event_id` must not. All three
    conditions are mandatory — the third is what keeps "A38 | Koncert X" intact."""
    venue_norm = normalize_venue(venue)
    if not venue_norm:
        return title
    separators = list(_SEPARATOR_RE.finditer(title))
    if not separators:
        return title
    last = separators[-1]
    suffix_norm = normalize_venue(title[last.end() :])
    if token_set_ratio(suffix_norm, venue_norm) < _VENUE_SUFFIX_MATCH_RATIO:
        return title
    return title[: last.start()]


def normalize_venue(venue: str | None) -> str:
    if venue is None:
        return ""
    return _WHITESPACE_RE.sub(" ", _strip_accents(venue).lower()).strip()


def fold_text(s: str) -> str:
    return _strip_accents(s).lower()


# Below this length a keyword keeps whole-word matching. A short stem is a common
# beginning of unrelated words, and dropping its trailing boundary is where prefix
# matching turns into noise: of config.yaml's four-character keywords, "rave" would fire
# on "ravasz", "piac" on "piackutatás", and "dj" on "djembe" — none of which are the
# category they were written for. The suffix gain is also smallest there, because a
# four-letter stem is usually already the whole word.
#
# The cost is real and accepted: "film" stays whole-word, so "filmek" is still missed.
# Lowering this threshold is a config-wide decision that wants the Part A measurement
# rerun, not a per-keyword judgement call.
_MIN_PREFIX_KEYWORD_LENGTH = 5

# A keyword written with a trailing "$" in the config opts out of prefix matching and is
# matched as a whole word only — the escape hatch for the rare stem that over-matches.
_EXACT_MATCH_MARKER = "$"


def contains_word(text: str, phrase: str) -> bool:
    """Accent- and case-insensitive containment of a config keyword.

    Hungarian is agglutinative: "társasjáték" appears in real titles as "Társasjátékos",
    "Társasjátékok", "társasjátékot". Whole-word matching missed every one of them, in
    every category — "koncert" missed "koncertje", "mérkőzés" missed "mérkőzése",
    "előadás" missed "előadásában" — so the keyword must be allowed to carry a suffix.
    The match therefore anchors at the START of a word and lets word characters follow;
    it never matches mid-word, so "koncert" still does not fire inside "szimfonikuskoncert".

    What it cannot do is tell an inflection from a compound. Hungarian writes compounds
    closed, so "koncertje" (wanted) and "koncertterem" (a hall rental, not wanted) have
    the same shape. Prefix matching accepts both. That is the deliberate trade: the
    inflections are common and the compounds are rare — measured over the saved Port.hu,
    Cooltix and kvizestek fixtures, 12 new matches, 11 of them correct — and `$` is the
    per-keyword escape hatch when a specific stem turns out to be the exception.

    Two guards on that:
    - A phrase shorter than `_MIN_PREFIX_KEYWORD_LENGTH` keeps the old whole-word rule.
    - A phrase ending in `$` is whole-word regardless of length (`_EXACT_MATCH_MARKER`).

    Shared by categorize's keyword scoring, filter's blocked_keywords, and score's
    keyword_boosts (§7.5, §7.6, §7.7) — the same phrase must count as present the same way
    everywhere, not almost the same way three times. Widening it here widens all three by
    design: the agglutination problem is not specific to categorization."""
    folded = fold_text(phrase).strip()
    exact_only = folded.endswith(_EXACT_MATCH_MARKER)
    if exact_only:
        folded = folded[: -len(_EXACT_MATCH_MARKER)].strip()
    if not folded:
        return False

    pattern = r"(?<!\w)" + re.escape(folded)
    if exact_only or len(folded) < _MIN_PREFIX_KEYWORD_LENGTH:
        pattern += r"(?!\w)"
    return re.search(pattern, fold_text(text)) is not None


def roman_district(number: int) -> str | None:
    """None for anything outside Budapest's I–XXIII, so a bogus code cannot become a
    district that the proximity bonus would then score against."""
    if not 1 <= number <= _MAX_DISTRICT:
        return None
    return f"{_ROMAN_TENS[number // 10]}{_ROMAN_UNITS[number % 10]}."


def district_from_zip(zip_code: str | int | None) -> str | None:
    """Budapest postal codes are `1XYZ` where `XY` is the district. `1000` exists in the
    Port.hu sample for online-only events: `00` is not a district (§6.5)."""
    text = str(zip_code or "").strip()
    if len(text) != 4 or not text.isdigit() or not text.startswith("1"):
        return None
    return roman_district(int(text[1:3]))


def make_event_id(title: str, start: datetime, venue: str | None) -> str:
    """Source-independent: the same event coming from several sources must get the same id,
    so the ledger can recognize it whichever source delivered it first."""
    basis = f"{normalize_title(title)}|{start.date().isoformat()}|{normalize_venue(venue)}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]
