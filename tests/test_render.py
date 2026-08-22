from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from digest.config import Config, ExpiringSectionConfig, NewsletterConfig
from digest.models import Event, make_event_id
from digest.render.email import render_email

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 4, 34, tzinfo=BUDAPEST)


def make_event(index: int = 0, **overrides: Any) -> Event:
    title = overrides.pop("title", f"Event {index}")
    start = overrides.pop("start", datetime(2026, 8, 19, 20, 0, tzinfo=BUDAPEST))
    venue_name = overrides.pop("venue_name", "A38 Hajó")
    score = overrides.pop("score", float(index))
    base: dict[str, Any] = {
        "id": make_event_id(title, start, venue_name),
        "source_ids": ["port-hu"],
        "urls": [f"https://port.hu/esemeny/{index}"],
        "title": title,
        "description": "Egy nagyszerű este vár mindenkire.",
        "start": start,
        "end": None,
        "effective_date": start.date(),
        "venue_name": venue_name,
        "district": "XI.",
        "lat": None,
        "lon": None,
        "distance_km": None,
        "price_min": 2500,
        "price_max": None,
        "categories": ["koncert"],
        "image_url": "https://media.port.hu/images/example.jpg",
        "score": score,
    }
    return Event(**{**base, **overrides})


def make_lineup(count: int, **shared: Any) -> list[Event]:
    return [make_event(i, **shared) for i in range(count)]


def test_a_time_unknown_event_renders_without_a_clock() -> None:
    """A source that published only a date must not be given a 00:00 the reader would
    take literally (§7.1). The date and weekday still render; only the clock is absent."""
    event = make_event(
        0,
        title="Szentendrei kiállítás",
        start=datetime(2026, 8, 19, 0, 0, tzinfo=BUDAPEST),
        start_time_known=False,
    )

    rendered = render_email([event], Config(), now=NOW)

    assert "Szentendrei kiállítás" in rendered.html
    assert "00:00" not in rendered.html
    assert "00:00" not in rendered.text
    # The suffix form the group row uses must not survive with nothing in front of it.
    assert "-tól" not in rendered.text.replace("19:00-tól", "")


def test_a_collapsed_group_of_time_unknown_events_drops_the_suffix_clause() -> None:
    """The group row renders "19:00-tól" ("from 19:00"). With no time that suffix would be
    left dangling with nothing in front of it -- worse than the 00:00 this replaces -- so
    the whole clause drops out instead."""
    from digest.pipeline.group import group

    events = [
        make_event(
            i,
            title=f"Program {i}",
            venue_name="Szentendre",
            start=datetime(2026, 8, 19, 0, 0, tzinfo=BUDAPEST),
            start_time_known=False,
            urls=[f"https://x/{i}"],
        )
        for i in range(5)
    ]

    rendered = render_email(group(events, Config()), Config(), now=NOW)

    assert "5 program" in rendered.text
    assert "-tól" not in rendered.text
    assert "-tól" not in rendered.html
    assert "00:00" not in rendered.html
    # The district still renders, so the parenthetical is trimmed rather than emptied.
    assert "(XI. kerület)" in rendered.text


def test_a_collapsed_group_with_times_keeps_its_suffix_clause() -> None:
    from digest.pipeline.group import group

    events = [
        make_event(
            i,
            title=f"Program {i}",
            venue_name="Szentendre",
            start=datetime(2026, 8, 19, 19, 0, tzinfo=BUDAPEST),
            urls=[f"https://x/{i}"],
        )
        for i in range(5)
    ]

    rendered = render_email(group(events, Config()), Config(), now=NOW)

    assert "(19:00-tól, XI. kerület)" in rendered.text


def test_a_timed_event_still_renders_its_clock() -> None:
    rendered = render_email(
        [make_event(0, start=datetime(2026, 8, 19, 20, 0, tzinfo=BUDAPEST))], Config(), now=NOW
    )

    assert "20:00" in rendered.html
    assert "20:00" in rendered.text


def test_every_input_events_title_appears_in_the_rendered_html() -> None:
    events = [
        make_event(0, title="Villon-est", categories=["koncert"], urls=["https://x/0"]),
        make_event(1, title="HØT SPØT", categories=["klub"], urls=["https://x/1"]),
        make_event(2, title="Kvízest", categories=["kviz"], urls=["https://x/2"]),
    ]

    rendered = render_email(events, Config(), now=NOW)

    for event in events:
        assert event.title in rendered.html


def test_an_empty_event_list_still_renders_the_zero_state() -> None:
    rendered = render_email([], Config(), now=NOW)

    assert "Ma este szabad vagy." in rendered.html
    assert "0 új program" in rendered.html


def test_the_plain_text_variant_contains_no_html_tags() -> None:
    events = make_lineup(3)

    rendered = render_email(events, Config(), now=NOW)

    assert re.search(r"<[^>]+>", rendered.text) is None


def test_the_plain_text_variant_contains_no_html_tags_when_empty() -> None:
    rendered = render_email([], Config(), now=NOW)

    assert re.search(r"<[^>]+>", rendered.text) is None


def test_per_category_limit_is_respected() -> None:
    events = make_lineup(5, categories=["koncert"])  # scores 0..4
    config = Config(
        newsletter=NewsletterConfig(
            per_category_limit=2,
            total_limit=25,
            expiring_section=ExpiringSectionConfig(enabled=False),
        )
    )

    rendered = render_email(events, config, now=NOW)

    assert "Event 4" in rendered.html
    assert "Event 3" in rendered.html
    assert "Event 2" not in rendered.html
    assert "Event 1" not in rendered.html
    assert "Event 0" not in rendered.html


def test_total_limit_is_respected_across_categories() -> None:
    events = [
        make_event(0, title="Koncert A", categories=["koncert"], score=1, urls=["https://x/0"]),
        make_event(1, title="Koncert B", categories=["koncert"], score=2, urls=["https://x/1"]),
        make_event(2, title="Klub A", categories=["klub"], score=3, urls=["https://x/2"]),
        make_event(3, title="Klub B", categories=["klub"], score=4, urls=["https://x/3"]),
    ]
    config = Config(
        newsletter=NewsletterConfig(
            per_category_limit=5,
            total_limit=2,
            expiring_section=ExpiringSectionConfig(enabled=False),
        )
    )

    rendered = render_email(events, config, now=NOW)

    shown = sum(title in rendered.html for title in ("Koncert A", "Koncert B", "Klub A", "Klub B"))
    assert shown == 2
    # The two weakest (score 1 and 2) are the ones dropped by the global cut.
    assert "Klub B" in rendered.html
    assert "Klub A" in rendered.html
    assert "Koncert A" not in rendered.html


def test_a_collapsed_row_renders_once_not_seventeen_times() -> None:
    members = make_lineup(17, venue_name="Sziget Fesztivál", categories=["egyeb"])
    ranked = sorted(members, key=lambda event: event.score, reverse=True)
    collapsed = ranked[0].model_copy(
        update={
            "id": "collapsed-sziget",
            "title": "Sziget Fesztivál — 17 program",
            "description": ", ".join(event.title for event in ranked[:3]),
            "group_size": 17,
        }
    )

    rendered = render_email([collapsed], Config(), now=NOW)

    # The venue name can legitimately appear a second time, in the hidden preheader's
    # "legerősebb" callout — what must not happen is one row per lineup member.
    assert rendered.html.count('href="https://port.hu/esemeny/16"') == 1
    assert "17 program" in rendered.html
    # Only the top 3 members are named; index 0 is not among them, and unlike most other
    # indices its title ("Event 0") is not a substring of any other member's title either.
    assert "Event 0" not in rendered.html
