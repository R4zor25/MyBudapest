from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

from digest.config import Config
from digest.models import Event
from digest.render.common import source_health_line
from digest.state import SourceHealth

log = structlog.get_logger()

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# index 0 = Monday, matching date.weekday() (§ design: rail weekday reflects effective_date,
# not the raw start — see _build_event_row).
_WEEKDAY_NAMES = ("Hétfő", "Kedd", "Szerda", "Csütörtök", "Péntek", "Szombat", "Vasárnap")
_WEEKDAY_DATIVE = (
    "hétfőre",
    "keddre",
    "szerdára",
    "csütörtökre",
    "péntekre",
    "szombatra",
    "vasárnapra",
)
_MONTH_NAMES = (
    "január",
    "február",
    "március",
    "április",
    "május",
    "június",
    "július",
    "augusztus",
    "szeptember",
    "október",
    "november",
    "december",
)

# Hungarian display labels for the category slugs in SPEC 5.1's config.yaml. A category
# present in the data but missing from config.categories (chiefly "egyeb", the fallback —
# it is deliberately not a configurable category, see config.py) falls back to its slug.
_CATEGORY_LABELS = {
    "koncert": "Koncert",
    "klub": "Klub",
    "szinhaz": "Színház",
    "kiallitas": "Kiállítás",
    "film": "Film",
    "meetup": "Meetup",
    "tarsasjatek": "Társasjáték",
    "kviz": "Kvíz",
    "gasztro": "Gasztro",
    "fesztival": "Fesztivál",
    "outdoor": "Outdoor",
    "sport": "Sport",
    "csaladi": "Családi",
    "egyeb": "Egyéb",
}

# The rail's seven segments step every 2 score points — the footer says so verbatim
# ("a hét sáv 2 pontonként lép"), and it matches every worked example in the design.
_SCORE_BAR_SEGMENTS = 7
_SCORE_BAR_STEP = 2.0
_SCORE_BAR_MAX_COLOR = "#b5abfc"  # only when all 7 segments are lit
_SCORE_BAR_COLOR = "#9184d9"

_PRICE_COLOR = "#cfd3e5"
_PRICE_UNKNOWN_COLOR = "#75798c"

_html_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
_text_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str
    # Every event that got an actual card — grouped/festival rows and the category
    # sections, but deliberately NOT the "Hamarosan lejár" callout: that section is a
    # last-chance nudge for something that lost its main-section slot (package 9's
    # report), and recording it as sent would mean it can never earn a proper card later.
    # This is what a caller should pass to state.record_sent (package 10).
    sent_events: list[Event]


def render_email(
    events: list[Event],
    config: Config,
    *,
    source_health: dict[str, SourceHealth] | None = None,
    archive_url: str | None = None,
    published_count: int | None = None,
    now: datetime | None = None,
) -> RenderedEmail:
    """Builds the subject, HTML body and plain-text alternative for one run's digest.

    `events` is the full post-group event list — this function does its own
    per-category/total limiting (requirement 2) and its own "expiring soon" selection
    (requirement 3); neither is a pipeline stage today (§ package 9 report).

    `published_count` is how many events reach the SITE, which is a different number from
    how many reach this email and has to be passed in rather than counted here: the email
    shows a per-category top slice, the site shows everything. The button at the top
    promises the full set, so it must not be labelled with the email's own item count."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    today = moment.date()
    health = source_health or {}
    # An email link has to be absolute; `base_path` is a path and cannot be resolved by a
    # mail client, so it is only the last resort it always was.
    site_url = config.site.base_url.rstrip("/")
    archive = archive_url or site_url or config.site.base_path or "#"

    grouped_rows, category_sections = _select_and_limit(events, config)
    displayed = [
        *grouped_rows,
        *(event for section in category_sections for event in section["events"]),
    ]
    expiring = _expiring_candidates(events, config, today)

    new_count = len(displayed)
    free_count = sum(1 for event in displayed if event.is_free)
    tonight_count = sum(1 for event in displayed if event.effective_date == today)

    context = {
        "weekday_label": _WEEKDAY_NAMES[today.weekday()],
        "date_label": _format_date(today),
        "new_count": new_count,
        "free_count": free_count,
        "tonight_count": tonight_count,
        "preheader": _preheader(displayed, new_count, free_count),
        "grouped_rows": [_build_grouped_row(event, today) for event in grouped_rows],
        "category_sections": _section_contexts(category_sections, has_festival=bool(grouped_rows)),
        "expiring_rows": [_build_expiring_row(event, config, today) for event in expiring],
        "source_health_line": source_health_line(health),
        "run_time_label": moment.strftime("%H:%M"),
        "archive_url": archive,
        # Empty when no base_url is configured — the template omits the button entirely
        # rather than rendering one that goes nowhere.
        "site_url": site_url,
        "published_count": len(events) if published_count is None else published_count,
    }

    if displayed:
        html = _html_env.get_template("email.html.j2").render(**context)
    else:
        html = _html_env.get_template("email-empty.html.j2").render(**context)
    text = _text_env.get_template("email.txt.j2").render(**context, has_events=bool(displayed))

    subject = _subject(today, new_count)
    log.info("email_rendered", new_count=new_count, expiring_count=len(expiring))
    return RenderedEmail(subject=subject, html=html, text=text, sent_events=displayed)


def _subject(today: date, new_count: int) -> str:
    date_label = _format_date(today)
    if new_count == 0:
        return f"Budapest — {date_label}: ma nincs semmi"
    return f"Budapest — {date_label}: {new_count} új program"


def _preheader(displayed: list[Event], new_count: int, free_count: int) -> str:
    if not displayed:
        return ""
    top = max(displayed, key=lambda event: event.score)
    return (
        f"{new_count} új program ma és a héten — {free_count} ingyenes. "
        f"A legerősebb: {top.title}, {_format_score(top.score)} pont."
    )


def _format_date(d: date) -> str:
    return f"{d.year}. {_MONTH_NAMES[d.month - 1]} {d.day}."


def _format_score(score: float) -> str:
    return f"{score:.1f}".replace(".", ",")


def _format_price(huf: int) -> str:
    return f"{huf:,}".replace(",", " ") + " Ft"


def _score_bar(score: float) -> tuple[int, str]:
    lit = max(0, min(_SCORE_BAR_SEGMENTS, math.ceil(score / _SCORE_BAR_STEP)))
    color = _SCORE_BAR_MAX_COLOR if lit >= _SCORE_BAR_SEGMENTS else _SCORE_BAR_COLOR
    return lit, color


def _price_part(event: Event) -> dict[str, object] | None:
    """The one price/free/unknown fragment shown per card. price_max (a genuine range) has
    no precedent anywhere in the design — only price_min is ever shown, so a range is not
    invented here; see the package 9 report."""
    if event.is_free:
        return {
            "html": True,
            "text": (
                '<span style="display:inline-block; font-size:11px; line-height:1.5; '
                "padding:1px 8px; border-radius:5px; background-color:#423a6a; "
                'color:#f5f4ff;">ingyenes</span>'
            ),
        }
    if event.price_min is not None:
        return {
            "html": True,
            "text": f'<span style="color:{_PRICE_COLOR};">{_format_price(event.price_min)}</span>',
        }
    return {
        "html": True,
        "text": f'<span style="color:{_PRICE_UNKNOWN_COLOR};">ár nincs megadva</span>',
    }


def _meta_parts(event: Event) -> list[dict[str, object]]:
    parts: list[dict[str, object]] = []
    if event.venue_name:
        parts.append({"html": False, "text": event.venue_name})
    if event.district:
        parts.append({"html": False, "text": f"{event.district} kerület"})
    price = _price_part(event)
    if price is not None:
        parts.append(price)
    return parts


def _meta_text(event: Event) -> str:
    """Plain-text twin of _meta_parts for email.txt.j2 — that template has no markup to
    strip, so the styled price/free span from _price_part cannot be reused there."""
    parts = [event.venue_name, f"{event.district} kerület" if event.district else None]
    parts.append(_price_plain(event))
    return " · ".join(part for part in parts if part)


def _time_label(event: Event) -> str:
    """Empty when the source published no clock. The templates that render a bare label
    show the date's weekday beside it and simply lose the time; the two that append a
    suffix ("19:00-tól") guard on the value, because "-tól" with nothing before it reads
    worse than the 00:00 this replaces."""
    return event.start.strftime("%H:%M") if event.start_time_known else ""


def _night_note(event: Event) -> str | None:
    if event.start.date() == event.effective_date:
        return None
    return f"éjjel, {_WEEKDAY_DATIVE[event.start.date().weekday()]}"


def _build_event_row(event: Event) -> dict[str, object]:
    lit, color = _score_bar(event.score)
    return {
        "url": event.urls[0] if event.urls else None,
        "title": event.title,
        "weekday_label": _WEEKDAY_NAMES[event.effective_date.weekday()],
        "time_label": _time_label(event),
        "night_note": _night_note(event),
        "bar_lit": lit,
        "score_color": color,
        "score_label": _format_score(event.score),
        "meta_parts": _meta_parts(event),
        "meta_text": _meta_text(event),
        "description": event.description,
        "image_url": event.image_url,
    }


def _section_contexts(
    sections: list[dict[str, object]], *, has_festival: bool
) -> list[dict[str, object]]:
    """Each category header's top padding depends on what precedes it in the design: 22px
    when nothing does (straight after the header block), 30px right after the festival
    block's own boxed row, 26px after a plain event row — see the package 9 report."""
    result = []
    for index, section in enumerate(sections):
        if index == 0:
            padding = 30 if has_festival else 22
        else:
            padding = 26
        events = section["events"]
        result.append(
            {
                "label": section["label"],
                "count": len(events),
                "header_top_padding": padding,
                "rows": [_build_event_row(event) for event in events],
            }
        )
    return result


def _build_grouped_row(event: Event, today: date) -> dict[str, object]:
    named = event.description.split(", ") if event.description else []
    remaining = max(0, event.group_size - len(named))
    days_until = (event.effective_date - today).days
    day_word = "ma" if days_until == 0 else f"{days_until} nap múlva"
    return {
        "url": event.urls[0] if event.urls else None,
        "venue_name": event.venue_name,
        "day_word": day_word,
        "time_label": _time_label(event),
        "district_label": f"{event.district} kerület" if event.district else None,
        "description": event.description,
        "remaining": remaining,
        "group_size": event.group_size,
    }


def _build_expiring_row(event: Event, config: Config, today: date) -> dict[str, object]:
    category = event.categories[0] if event.categories else config.fallback_category
    category_label = _CATEGORY_LABELS.get(category, category)
    venue_district = event.venue_name
    if event.venue_name and event.district:
        venue_district = f"{event.venue_name}, {event.district}"
    elif not event.venue_name and event.district:
        venue_district = event.district
    price = _price_part(event)
    days_until = (event.effective_date - today).days
    relative = "ma" if days_until == 0 else f"{days_until} nap múlva"

    parts: list[dict[str, object]] = [{"html": False, "text": category_label}]
    if venue_district:
        parts.append({"html": False, "text": venue_district})
    if price is not None:
        # No styled pill/span precedent in this compact row — plain text only.
        parts.append({"html": False, "text": _price_plain(event)})
    parts.append({"html": False, "text": relative})

    return {
        "url": event.urls[0] if event.urls else None,
        "title": event.title,
        "weekday_label": _WEEKDAY_NAMES[event.effective_date.weekday()],
        "time_label": _time_label(event),
        "meta_parts": parts,
        "score_label": _format_score(event.score),
    }


def _price_plain(event: Event) -> str:
    if event.is_free:
        return "ingyenes"
    if event.price_min is not None:
        return _format_price(event.price_min)
    return "ár nincs megadva"


def _select_and_limit(
    events: list[Event], config: Config
) -> tuple[list[Event], list[dict[str, object]]]:
    """Per-category and total limiting (requirement 2). No pipeline `limit` stage exists yet
    (package 10 wires `... group -> limit -> render -> ...`) so this function, not a
    pipeline module, owns it for now — see the package 9 report."""
    grouped = [event for event in events if event.group_size > 1]
    singles = [event for event in events if event.group_size == 1]

    buckets: dict[str, list[Event]] = {}
    for event in singles:
        category = event.categories[0] if event.categories else config.fallback_category
        buckets.setdefault(category, []).append(event)

    capped: dict[str, list[Event]] = {}
    for category, members in buckets.items():
        ranked = sorted(members, key=lambda event: event.score, reverse=True)
        capped[category] = ranked[: config.newsletter.per_category_limit]

    eligible = [*grouped, *(event for members in capped.values() for event in members)]
    overflow = len(eligible) - config.newsletter.total_limit
    dropped: set[str] = set()
    if overflow > 0:
        weakest = sorted(eligible, key=lambda event: event.score)[:overflow]
        dropped = {event.id for event in weakest}
        log.info("newsletter_total_limit_trimmed", dropped=len(dropped))

    kept_grouped = [event for event in grouped if event.id not in dropped]
    # The fallback bucket goes LAST, whatever it scores. It is not a recommendation — it
    # is the pile of events no rule recognised — so it must never lead the mail, and it is
    # also the feedback channel for the category rules: a section that keeps growing means
    # the keyword config needs work, and burying it inside the order would hide that.
    fallback = config.fallback_category
    order = [c for c in config.categories if c in capped and c != fallback]
    order += sorted(c for c in capped if c not in order and c != fallback)
    if fallback in capped:
        order.append(fallback)
    sections = [
        {
            "label": _CATEGORY_LABELS.get(category, category),
            "events": [event for event in capped[category] if event.id not in dropped],
        }
        for category in order
    ]
    sections = [section for section in sections if section["events"]]
    return kept_grouped, sections


def _expiring_candidates(events: list[Event], config: Config, today: date) -> list[Event]:
    """Independent of what the main sections show (a row can appear in both) — see the
    package 9 report for why. The boundary is inclusive, matching score.py's soon_bonus
    (`> within_days` is what excludes, so `within_days` itself still qualifies): the
    design's own "Hamarosan lejár" example is exactly "3 nap múlva" with within_days=3.
    Collapsed festival rows are excluded — that compact card style has no precedent for
    them, and their synthetic "— N program" title does not fit it."""
    section = config.newsletter.expiring_section
    if not section.enabled:
        return []
    candidates = [
        event
        for event in events
        if event.group_size == 1 and 0 <= (event.effective_date - today).days <= section.within_days
    ]
    return sorted(candidates, key=lambda event: event.score, reverse=True)
