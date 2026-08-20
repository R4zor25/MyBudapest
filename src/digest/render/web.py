from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

from digest.config import Config
from digest.models import Event
from digest.render.common import source_health_line
from digest.state import SourceHealth

log = structlog.get_logger()

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_UTC = ZoneInfo("UTC")

_html_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(default=True, default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)

_ARCHIVE_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")


@dataclass(frozen=True)
class WebOutput:
    events_json: str
    index_html: str
    status_html: str
    archive_html: str
    archive_date: date


def render_web(
    events: list[Event],
    config: Config,
    *,
    source_health: dict[str, SourceHealth] | None = None,
    now: datetime | None = None,
) -> WebOutput:
    """Builds the three static Pages outputs (§9.0/§9.1, web profile): the full event
    list as JSON, the reader UI (fetches that JSON), and a health table. Unlike
    render_email, this applies no per_category_limit/total_limit — those live under
    `newsletter:` in config.yaml and are an email-digest concept; the public site is the
    canonical listing, and its own client-side filters (search, category chips, date
    range) are how a reader narrows a few hundred events, not a server-side cap."""
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    today = moment.date()
    health = source_health or {}
    health_line = source_health_line(health)

    events_json = _build_events_json(events, moment)

    index_html = _html_env.get_template("index.html.j2").render(
        source_health_line=health_line, embedded_data=None
    )
    archive_html = _html_env.get_template("index.html.j2").render(
        source_health_line=health_line, embedded_data=_escape_for_inline_script(events_json)
    )
    status_html = _html_env.get_template("status.html.j2").render(
        generated_at_label=moment.strftime("%Y-%m-%d %H:%M"),
        rows=_status_rows(health),
    )

    log.info("web_rendered", event_count=len(events))
    return WebOutput(
        events_json=events_json,
        index_html=index_html,
        status_html=status_html,
        archive_html=archive_html,
        archive_date=today,
    )


def write_site(output: WebOutput, site_dir: Path, *, archive_keep_days: int) -> None:
    """The I/O counterpart to render_web, same split as state.py's pure functions plus
    load_state/save_state — render_web stays a pure function, this is where bytes
    actually land on disk (and where old archive pages are removed)."""
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "events.json").write_text(output.events_json + "\n", encoding="utf-8")
    (site_dir / "index.html").write_text(output.index_html, encoding="utf-8")
    (site_dir / "status.html").write_text(output.status_html, encoding="utf-8")

    archive_dir = site_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"{output.archive_date.isoformat()}.html").write_text(
        output.archive_html, encoding="utf-8"
    )
    purge_archive(archive_dir, keep_after=output.archive_date - timedelta(days=archive_keep_days))


def purge_archive(archive_dir: Path, *, keep_after: date) -> None:
    """Deletes site/archive/YYYY-MM-DD.html files older than `keep_after` (requirement 4:
    "before the commit step" — SPEC 11's workflow YAML is pinned byte-for-byte and has no
    separate purge step, so this must run as part of the same `digest run` that writes
    the archive page, not as a workflow addition)."""
    if not archive_dir.exists():
        return
    for path in sorted(archive_dir.glob("*.html")):
        match = _ARCHIVE_FILENAME_RE.match(path.name)
        if match is None:
            continue
        file_date = date.fromisoformat(match.group(1))
        if file_date < keep_after:
            path.unlink()
            log.info("archive_purged", file=path.name)


def _event_to_json(event: Event) -> dict[str, Any]:
    """The explicit web-profile field list (§9.0/§9.1) — deliberately NOT model_dump(),
    so a future field added to Event cannot leak into the public site by default. No
    `description`, no `image_url`, no `lat`/`lon`, no `urls` (plural), no `price_max`,
    and — since AUDIT-1 BLOCKER-2 — no `breakdown` either: individual score_breakdown
    terms are literally the private profile's numbers (`category` == that category's
    weight, `weekday` == that day's weight, no inference needed), exactly the "readable
    map of your taste" §12 keeps in PROFILE_YAML. The aggregate `score` stays public
    (sorting, the meter bar); the term-by-term breakdown does not. This function is the
    only thing that ever stripped it — render_email operates on the same Event objects
    untouched by this change.

    `score` has PINNED_BONUS subtracted back out for the same reason `breakdown` is
    gone: publishing it unadjusted would announce which specific event was pinned via
    the write UI, no cross-referencing required."""
    pinned_signal = event.score_breakdown.get("pinned_bonus", 0.0)
    return {
        "id": event.id,
        "title": event.title,
        "url": event.urls[0] if event.urls else "",
        "start": event.start.isoformat(),
        "venue": event.venue_name,
        "district": event.district,
        "categories": event.categories,
        "price_min": event.price_min,
        "is_free": event.is_free,
        "score": event.score - pinned_signal,
        "group_size": event.group_size,
    }


def _build_events_json(events: list[Event], moment: datetime) -> str:
    ordered = sorted(events, key=lambda event: (-event.score, event.start))
    payload = {
        "generated_at": moment.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": [_event_to_json(event) for event in ordered],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _escape_for_inline_script(json_text: str) -> str:
    """A title or url containing the literal substring "</script>" would otherwise
    terminate the surrounding <script> tag early when this JSON is embedded verbatim
    into the archive page (requirement 3) — standard inline-JSON escaping."""
    return json_text.replace("</", "<\\/")


def _status_rows(source_health: dict[str, SourceHealth]) -> list[dict[str, str]]:
    return [
        {
            "source_id": source_id,
            "last_ok": health.last_ok.isoformat() if health.last_ok else "—",
            "consecutive_failures": str(health.consecutive_failures),
            "last_count": str(health.last_count),
            "disabled_until": health.disabled_until.isoformat() if health.disabled_until else "—",
        }
        for source_id, health in sorted(source_health.items())
    ]
