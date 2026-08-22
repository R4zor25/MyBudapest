from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from digest.config import Config, ExpiringSectionConfig, NewsletterConfig, SiteConfig
from digest.models import Event, make_event_id
from digest.pipeline.group import group
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


def test_a_collapsed_group_of_time_unknown_events_shows_no_clock() -> None:
    """WAS about the standalone block's "19:00-tól" suffix, which had to drop out whole
    when there was no time. That block is gone — a collapsed row is an ordinary row inside
    its category section now — but the property it protected still holds: a group whose
    members published no clock must not print one, nor a dangling "-tól"."""
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

    assert "Szentendre — 5 program" in rendered.text
    assert "-tól" not in rendered.text
    assert "-tól" not in rendered.html
    assert "00:00" not in rendered.html
    assert "19:00" not in rendered.text
    # The row still says what it is, in place, without the block that used to say it.
    assert "Egy helyszín, több program · 5 program" in rendered.text
    # The district still renders — the clock is what drops, not the whole meta line.
    assert "XI. kerület" in rendered.text


def test_a_collapsed_group_with_times_shows_its_clock() -> None:
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

    assert "19:00" in rendered.text
    assert "XI. kerület" in rendered.text


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


# --------------------------------------------------------------------------------------
# The web-view button at the top of the email
# --------------------------------------------------------------------------------------

SITE = "https://example.github.io/digest"


def site_config(**overrides: Any) -> Config:
    site = SiteConfig(base_url=SITE)
    return Config(site=site, **overrides)


def test_the_web_button_sits_above_the_first_category_section() -> None:
    """Above the sections, below the header line. Its position is the point of it: a link
    in the footer is a link nobody scrolls to."""
    events = [make_event(i, title=f"Koncert {i}") for i in range(4)]

    html = render_email(events, site_config(), published_count=114, now=NOW).html

    button_at = html.index(SITE)
    first_section_at = html.index("KONCERT") if "KONCERT" in html else html.index("Koncert")
    assert button_at < first_section_at
    # And below the header, not above it — the date line still opens the mail.
    assert html.index("Budapesti napi lista") < button_at


def test_the_button_is_a_table_not_a_padded_anchor() -> None:
    """Outlook's Word rendering engine drops padding on an <a>, which turns a padded-anchor
    button into underlined text. The clickable cell has to be a <td>."""
    html = render_email([make_event(0)], site_config(), published_count=9, now=NOW).html

    href_at = html.index(f'href="{SITE}"')
    enclosing = html[html.rindex("<table", 0, href_at) : href_at]
    assert 'role="presentation"' in enclosing
    assert "<td" in enclosing
    assert "padding" in enclosing


def test_the_label_counts_the_published_set_not_the_email_contents() -> None:
    """The button promises the whole site. With per_category_limit at 3 the email holds a
    fraction of it, and labelling the button with the email's own count would advertise
    the smaller number and undersell the page."""
    events = [make_event(i, title=f"Koncert {i}") for i in range(10)]
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))

    rendered = render_email(events, config, published_count=114, now=NOW)

    assert "Mind a 114 program megtekintése" in rendered.html
    assert len(rendered.sent_events) == 3
    assert "Mind a 3 program" not in rendered.html


def test_the_text_alternative_carries_the_same_link() -> None:
    rendered = render_email([make_event(0)], site_config(), published_count=114, now=NOW)

    assert SITE in rendered.text
    assert f"Mind a 114 program: {SITE}" in rendered.text


def test_without_a_configured_base_url_the_button_is_omitted_not_broken() -> None:
    # An empty base_url must not produce href="" or a bare path: a relative href in a mail
    # client resolves against nothing.
    html = render_email([make_event(0)], Config(), published_count=114, now=NOW).html

    assert "megtekintése" not in html
    assert 'href=""' not in html


def test_the_footer_archive_link_survives_the_new_button() -> None:
    html = render_email([make_event(0)], site_config(), published_count=114, now=NOW).html

    assert "Archívum" in html
    assert html.count(SITE) >= 2


def test_exactly_three_per_category_when_more_qualify() -> None:
    """`per_category_limit` cuts per category, not overall — and the cut is by score, so
    the three that survive are the top three."""
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))
    concerts = [make_event(i, title=f"Koncert {i}", score=float(i)) for i in range(6)]
    quizzes = [
        make_event(10 + i, title=f"Kvíz {i}", categories=["kviz"], score=float(i)) for i in range(5)
    ]

    rendered = render_email(concerts + quizzes, config, published_count=11, now=NOW)

    kept = {event.title for event in rendered.sent_events}
    assert kept == {"Koncert 5", "Koncert 4", "Koncert 3", "Kvíz 4", "Kvíz 3", "Kvíz 2"}
    assert len(rendered.sent_events) == 6


def test_a_collapsed_row_is_not_labelled_a_festival() -> None:
    """§7.4 collapses any 4+ events sharing (venue, day, primary category) — a cinema's
    four screenings as readily as a festival's stages. The heading used to say "Fesztivál"
    for all of them: on the shipped fixtures the one collapsed row is four FILM screenings
    at Bem Mozi, and that is what went out above them."""
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))
    films = [
        make_event(i, title=f"Vetítés {i}", categories=["film"], venue_name="Bem Mozi", score=3.4)
        for i in range(4)
    ]
    # render_email does not group — §7.4 runs before it in the pipeline, so the collapsed
    # row has to exist before the renderer sees it.
    collapsed = group(films, config)
    assert [event.group_size for event in collapsed] == [4], "the row must actually collapse"

    rendered = render_email(collapsed, config, published_count=4, now=NOW)

    assert "Bem Mozi — 4 program" in rendered.html
    assert "Fesztivál" not in rendered.html
    assert "FESZTIVÁL" not in rendered.text


def test_the_uncategorised_section_renders_last_whatever_it_scores() -> None:
    """The fallback bucket is "nothing matched", not a recommendation, so it never leads —
    even when its best event outscores every other section's. It is also the feedback
    channel for the category rules, which is why it is shown at all rather than dropped."""
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))
    uncategorised = [
        make_event(i, title=f"Ismeretlen {i}", categories=["egyeb"], score=99.0) for i in range(2)
    ]
    concerts = [make_event(10 + i, title=f"Koncert {i}", score=1.0) for i in range(2)]

    rendered = render_email(uncategorised + concerts, config, published_count=4, now=NOW)

    assert "Egyéb" in rendered.html
    assert rendered.html.index("Koncert") < rendered.html.index("Egyéb")
    assert rendered.text.index("KONCERT") < rendered.text.index("EGYÉB")
    # And it is in the mail at all — this is the half that needs filters.categories to
    # include the fallback name; see the profile note in the report.
    assert {event.title for event in rendered.sent_events} >= {"Ismeretlen 0", "Ismeretlen 1"}


# --------------------------------------------------------------------------------------
# A collapsed row is ranked inside its section, not printed above every one of them
# --------------------------------------------------------------------------------------


def _film_group(score: float) -> list[Event]:
    films = [
        make_event(i, title=f"Vetítés {i}", categories=["film"], venue_name="Bem Mozi", score=score)
        for i in range(4)
    ]
    return group(films, Config())


def test_a_low_scoring_collapsed_row_renders_after_the_section_items() -> None:
    """The defect, stated as a test: a 3.476-point §7.4 row led an email whose best item
    scored 11.0, because the template printed grouped rows above every category section
    regardless of score."""
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))
    strong = [
        make_event(10 + i, title=f"Erős film {i}", categories=["film"], score=9.0 - i)
        for i in range(2)
    ]

    html = render_email(_film_group(3.4) + strong, config, published_count=6, now=NOW).html

    assert html.index("Erős film 0") < html.index("Bem Mozi — 4 program")


def test_a_high_scoring_collapsed_row_renders_first() -> None:
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))
    weak = [
        make_event(10 + i, title=f"Gyenge film {i}", categories=["film"], score=1.0)
        for i in range(2)
    ]

    html = render_email(_film_group(9.9) + weak, config, published_count=6, now=NOW).html

    assert html.index("Bem Mozi — 4 program") < html.index("Gyenge film 0")


def test_a_collapsed_row_counts_as_one_item_against_the_category_limit() -> None:
    """One, not zero and not four. Zero was the old behaviour and let a section carry
    per_category_limit + however many groups existed; four would make one row consume the
    whole section, which defeats the compression the row exists for."""
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))
    singles = [
        make_event(10 + i, title=f"Film {i}", categories=["film"], score=5.0 + i) for i in range(5)
    ]

    # Scored into the top three on purpose: the question is whether it OCCUPIES a slot,
    # which only shows when it qualifies. Before the fix the section held four.
    rendered = render_email(_film_group(9.5) + singles, config, published_count=9, now=NOW)

    titles = [event.title for event in rendered.sent_events]
    assert len(titles) == 3
    assert "Bem Mozi — 4 program" in titles


def test_neither_template_emits_a_standalone_grouped_block() -> None:
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))

    rendered = render_email(_film_group(4.0), config, published_count=4, now=NOW)

    # The block had its own heading in both templates; the label now travels with the row.
    assert "EGY HELYSZÍN, TÖBB PROGRAM\n" not in rendered.text
    assert rendered.html.count("Egy helyszín, több program") == 1
    assert "Egy helyszín, több program · 4 program" in rendered.html


def test_an_event_kept_by_a_secondary_category_sections_under_its_primary() -> None:
    """The other half of the widened allow-list: inclusion looks at every category, but the
    section is still decided by the primary one — so this lands in Egyéb, last."""
    config = site_config(newsletter=NewsletterConfig(per_category_limit=3, total_limit=50))
    secondary = make_event(0, title="Rejtélyes koncert", categories=["egyeb", "koncert"])
    plain = make_event(1, title="Sima koncert", categories=["koncert"])

    rendered = render_email([secondary, plain], config, published_count=2, now=NOW)

    assert rendered.html.index("Sima koncert") < rendered.html.index("Rejtélyes koncert")
    assert "Egyéb" in rendered.html


# --------------------------------------------------------------------------------------
# Every row carries its calendar date (the same defect the site had)
# --------------------------------------------------------------------------------------


def test_two_wednesdays_produce_two_distinct_row_labels() -> None:
    """The defect, measured rather than assumed: both rows read "Szerda 19:00 — A38 Hajó ·
    XI. kerület" and nothing told them apart over a 20-day horizon."""
    config = site_config()
    first = make_event(0, title="Szerda egy", start=datetime(2026, 9, 2, 19, 0, tzinfo=BUDAPEST))
    second = make_event(1, title="Szerda kettő", start=datetime(2026, 9, 9, 19, 0, tzinfo=BUDAPEST))

    rendered = render_email([first, second], config, published_count=2, now=NOW)

    assert "szerda, szeptember 2., 19:00" in rendered.text
    assert "szerda, szeptember 9., 19:00" in rendered.text
    assert "szerda, szeptember 2., 19:00" in rendered.html
    assert "szerda, szeptember 9., 19:00" in rendered.html


def test_a_clockless_row_carries_the_date_and_no_placeholder() -> None:
    config = site_config()
    event = make_event(0, start=datetime(2026, 9, 4, 0, 0, tzinfo=BUDAPEST), start_time_known=False)

    rendered = render_email([event], config, published_count=1, now=NOW)

    assert "péntek, szeptember 4." in rendered.text
    assert "00:00" not in rendered.text
    assert "00:00" not in rendered.html
    assert "péntek, szeptember 4., " not in rendered.text, "no trailing comma with no time"


def test_today_and_tomorrow_keep_the_relative_word_and_the_date() -> None:
    config = site_config()
    today = make_event(0, title="Ma", start=datetime(2026, 8, 16, 20, 0, tzinfo=BUDAPEST))
    tomorrow = make_event(1, title="Holnap", start=datetime(2026, 8, 17, 20, 0, tzinfo=BUDAPEST))

    rendered = render_email([today, tomorrow], config, published_count=2, now=NOW)

    assert "ma, augusztus 16., 20:00" in rendered.text
    assert "holnap, augusztus 17., 20:00" in rendered.text


def test_a_grouped_row_carries_the_date_too() -> None:
    config = site_config()
    films = [
        make_event(
            i,
            title=f"Vetítés {i}",
            categories=["film"],
            venue_name="Bem Mozi",
            start=datetime(2026, 9, 3, 18, 0, tzinfo=BUDAPEST),
            score=3.0,
        )
        for i in range(4)
    ]

    rendered = render_email(group(films, config), config, published_count=4, now=NOW)

    assert "csütörtök, szeptember 3." in rendered.html
    assert "csütörtök, szeptember 3., 18:00" in rendered.text


def test_an_expiring_row_carries_the_date_too() -> None:
    # expiring_section lives under newsletter, not at the top level.
    config = site_config(
        newsletter=NewsletterConfig(
            expiring_section=ExpiringSectionConfig(enabled=True, within_days=3)
        )
    )
    soon = make_event(0, title="Lejár", start=datetime(2026, 8, 17, 19, 0, tzinfo=BUDAPEST))

    rendered = render_email([soon], config, published_count=1, now=NOW)

    assert "HAMAROSAN LEJÁR" in rendered.text
    assert rendered.text.count("holnap, augusztus 17., 19:00") >= 1
    # The old trailing "holnap" part is gone -- the label already says it.
    assert "· holnap —" not in rendered.text
