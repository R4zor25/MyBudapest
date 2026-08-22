from __future__ import annotations

from datetime import date

import structlog

log = structlog.get_logger()

# THE display names for the category slugs — one table, read by both renderers and
# published to the site so the browser never carries a second copy.
#
# There used to be two: this one (then living in render/email.py) and a `CAT` object
# hand-written into index.html.j2. They diverged exactly as two hand-maintained lists do —
# `sport`, `csaladi` and `egyeb` were added to one and not the other, so events in those
# categories were on the page with no filter chip and a raw lowercase slug for a label.
#
# The slugs themselves are the taxonomy, and they live in config.yaml. This table cannot be
# derived from them (a slug is not a Hungarian display name), which is why it must fail
# LOUDLY on a slug it does not know rather than quietly rendering the slug.
CATEGORY_LABELS = {
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


def category_label(slug: str) -> str:
    """The display name, or the slug plus a WARNING line naming what is missing.

    Falling back silently to the slug is what made the divergence invisible: `egyeb` simply
    rendered as "egyeb" and nobody could tell it from a styling choice."""
    label = CATEGORY_LABELS.get(slug)
    if label is None:
        log.warning("category_label_missing", category=slug, add_to="render/labels.py")
        return slug
    return label


# Hungarian weekday and month names. They live HERE, beside the category labels, for the
# same reason: the email and the site both need them, and a second copy is how the last
# three defects happened. The site gets its own from `Intl`, which is the browser's
# equivalent of this table — what matters is that neither renderer keeps a private one.
#
# Index 0 = Monday, matching date.weekday().
WEEKDAY_NAMES = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")
WEEKDAY_DATIVE = (
    "hétfőre",
    "keddre",
    "szerdára",
    "csütörtökre",
    "péntekre",
    "szombatra",
    "vasárnapra",
)
MONTH_NAMES = (
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


def month_day(day: date) -> str:
    """"szeptember 2." — the Hungarian form, trailing period included."""
    return f"{MONTH_NAMES[day.month - 1]} {day.day}."


def full_date(day: date) -> str:
    """"2026. augusztus 22." — the header form, with the year."""
    return f"{day.year}. {month_day(day)}"


def when_label(day: date, today: date, time_label: str = "") -> str:
    """THE row label, for both renderers: "szerda, szeptember 2., 19:00".

    The date is the point. Without it a row over a 20-day horizon shows only a weekday, and
    three Wednesdays are indistinguishable — worst in a score-sorted view, which has no day
    headings to fall back on. Today and tomorrow keep the relative word AND the date,
    because "ma" alone is the one case where a reader still has to look something up.

    `time_label` is empty when the source published no clock (§7.1), and then no time is
    shown and no placeholder stands in for it: a 00:00 that means "unknown" is exactly what
    `start_time_known` exists to keep out of the output."""
    if day == today:
        weekday = "ma"
    elif (day - today).days == 1:
        weekday = "holnap"
    else:
        weekday = WEEKDAY_NAMES[day.weekday()]
    label = f"{weekday}, {month_day(day)}"
    return f"{label}, {time_label}" if time_label else label
