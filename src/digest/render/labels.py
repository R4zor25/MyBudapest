from __future__ import annotations

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
