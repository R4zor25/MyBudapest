from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import structlog
import typer
from pydantic import ValidationError

from digest.config import Config, load_config, require_llm_extra
from digest.delivery.smtp import SmtpDeliverer
from digest.errors import ConfigError, DigestError, FetchError, ParseError
from digest.fetch.api import ApiFetcher
from digest.fetch.base import FetchResult, FetchTask
from digest.fetch.http import HttpFetcher
from digest.llm.gemini import GeminiCategorizer
from digest.models import Event, RawEvent
from digest.overrides import load_overrides
from digest.pipeline.categorize import categorize as categorize_events
from digest.pipeline.categorize import explain_event, score_category
from digest.pipeline.dedup import dedup
from digest.pipeline.filter import (
    GEO_REASONS,
    content_filter,
    content_filter_with_reasons,
    exclude_already_sent,
)
from digest.pipeline.group import group, group_with_counts
from digest.pipeline.normalize import normalize, normalize_with_reasons
from digest.pipeline.recurrence import recurrence
from digest.pipeline.score import score, score_one
from digest.render.email import RenderedEmail, render_email
from digest.render.web import render_web, write_site
from digest.sources.registry import Source, load_sources
from digest.state import (
    RunLogEntry,
    SourceHealth,
    State,
    load_state,
    purge,
    record_run,
    record_sent,
    save_state,
    was_sent,
)

log = structlog.get_logger()

app = typer.Typer(
    help="Budapest Event Digest — daily event newsletter pipeline.",
    no_args_is_help=True,
)

_CONFIG_PATH = Path("config.yaml")
_SOURCES_DIR = Path("sources")
_STATE_PATH = Path("state/state.json")
_SITE_DIR = Path("site")
_OVERRIDES_PATH = Path("overrides.yaml")

_FETCHERS: dict[str, type[HttpFetcher]] = {"http": HttpFetcher, "api": ApiFetcher}
_DELIVERERS: dict[str, type] = {"smtp": SmtpDeliverer}

# SPEC 13: 5 consecutive failures auto-disables a source for a week; a source that
# previously returned more than 10 events and now returns 0 is selector drift, not
# "no events today" — logged and surfaced, but it does not count as a failure.
_DISABLE_AFTER_FAILURES = 5
_DISABLE_DAYS = 7
_DRIFT_MIN_PREVIOUS_COUNT = 10


@dataclass(frozen=True)
class RunSummary:
    source_counts: dict[str, int]
    # Records normalize dropped for being over, per source. Not a subset of any other
    # figure here — these never became Events at all, so `source_counts` still counts them
    # and nothing downstream ever saw them. Per source because that is the whole signal: a
    # feed that stops rolling its dates forward keeps parsing cleanly and just goes quiet,
    # and §13's drift check counts records parsed, so this is where it becomes visible.
    dropped_as_past: dict[str, int]
    merged: int
    # Dropped by the CONTENT filters — horizon, geography, category, price, blocked
    # keywords, explicit hides. A property of the event, so this number is reproducible:
    # the same corpus drops the same events tomorrow.
    dropped_by_content: int
    # A subset of dropped_by_filter, not an addition to it: the geographic cut is one of
    # filter()'s reasons (§7.6), reported separately because it is the one rule that used
    # to live inside individual sources.
    dropped_by_geo: int
    dropped_by_min_score: int
    # Suppressed from TODAY'S EMAIL by the ledger — NOT dropped. Every one of these is on
    # the site. Counted apart from `dropped_by_content` because it answers a different
    # question and moves for a different reason: this one grows as the reader's history
    # grows, while the content drops stay flat.
    suppressed_by_ledger: int
    # Events that skipped §7.4 grouping for having no venue_name. Not a drop -- they are
    # all still in the digest, individually. Reported so a source that stops supplying
    # venues is visible instead of quietly reshaping the output.
    ungrouped_venueless: int
    sent: int
    drifted: list[str]
    seconds: float


@app.command()
def run(
    dry: Annotated[
        bool, typer.Option("--dry", help="Render the email to a file instead of sending it.")
    ] = False,
    source_id: Annotated[str, typer.Option("--source", help="Source to read events from.")] = (
        "port-hu"
    ),
    fixture: Annotated[
        Path | None,
        typer.Option(help="Parse this saved response instead of calling the source."),
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", help="Where --dry writes the rendered HTML.")
    ] = Path("digest_dry_run.html"),
) -> None:
    """Run the full pipeline and deliver the digest.

    `--dry` is a separate, self-contained path (package 9): one source, one fixture, no
    fetch, no state — it must stay that way, since its whole point is a side-effect-free
    preview. It never reaches _run_real, so it can never touch state/state.json."""
    if dry:
        if fixture is None:
            raise NotImplementedError
        config, raw = _load_raw_events(source_id, fixture)
        events = recurrence(dedup(normalize(raw, config), config), config)
        events = categorize_events(events, config)
        # No ledger here: --dry has no state to read, and a preview that changed with
        # yesterday's email would not be a preview of the content rules.
        events = content_filter(events, config)
        events = score(events, config)
        events = group(events, config)

        rendered = render_email(events, config)
        out.write_text(rendered.html, encoding="utf-8")
        typer.echo(f"wrote {out} ({len(events)} events, subject: {rendered.subject!r})")
        return

    _run_real(_CONFIG_PATH, _SOURCES_DIR, _STATE_PATH, _SITE_DIR, _OVERRIDES_PATH)


def _run_real(
    config_path: Path, sources_dir: Path, state_path: Path, site_dir: Path, overrides_path: Path
) -> None:
    """The real entry point: load everything from disk/environment, then hand off to
    _run_pipeline, which is what tests call directly with fake sources instead."""
    try:
        config = load_config(config_path, sources_dir, os.environ.get("PROFILE_YAML"))
    except (ConfigError, ValidationError) as exc:
        # AUDIT-3 BLOCKER: this is the only call site whose output reaches a real, public
        # Actions log. pydantic.ValidationError's default str() includes "input_value=..."
        # for every failing field, and the merged config a mistyped PROFILE_YAML key would
        # fail on is deep-merged with the secret itself — raising `exc` uncaught here would
        # let Typer's default pretty-exception handler print it (and its full cause chain)
        # verbatim. Catching, logging a redacted summary, and exiting cleanly (not
        # re-raising) is what actually keeps the raw value out of the log; `from None`
        # deliberately drops the chain so nothing later re-attaches it.
        log.error("config_invalid", error=_redact_config_error(exc))
        raise typer.Exit(code=1) from None
    state = load_state(state_path)
    sources = load_sources(config)
    _run_pipeline(config, sources, state, state_path, site_dir, overrides_path)


def _redact_config_error(exc: ConfigError | ValidationError) -> str:
    if isinstance(exc, ValidationError):
        # pydantic's own error `msg` templates (missing, extra_forbidden, int_parsing, ...)
        # are static and never echo the input. "value_error" is the one type a custom
        # @field_validator can raise with its own message (e.g. ScoringConfig's weekday
        # check) — that message could in principle embed profile content, so it is dropped
        # too; the field path already says which section of the profile to look at.
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: "
            f"{error['type'] if error['type'] == 'value_error' else error['msg']} "
            f"({error['type']})"
            for error in exc.errors()
        )
        return f"config is invalid ({len(exc.errors())} error(s)): {problems}"
    return str(exc)


def _run_pipeline(
    config: Config,
    sources: list[Source],
    state: State,
    state_path: Path,
    site_dir: Path,
    overrides_path: Path,
    *,
    now: datetime | None = None,
) -> RunSummary:
    """SPEC 11's composition: fetch -> normalize -> dedup -> recurrence -> categorize ->
    filter -> score -> group -> limit -> render -> deliver -> state save. "limit" has no
    pipeline module of its own (package 9's report) — render_email applies it internally,
    so there is no separate call for it here."""
    started = perf_counter()
    tz = ZoneInfo(config.schedule.timezone)
    moment = now.astimezone(tz) if now is not None else datetime.now(tz)
    today = moment.date()

    state = purge(state, today=today)
    raw_events, state, source_counts, drifted = _run_sources(sources, config, state, today)

    normalized = normalize_with_reasons(raw_events, config, now=moment)
    events = normalized.events
    after_normalize = len(events)
    events = dedup(events, config)
    merged = after_normalize - len(events)
    after_dedup = len(events)
    events = recurrence(events, config)
    events = categorize_events(events, config)
    events = _llm_categorize(events, config)

    # Resolve was_sent()'s fuzzy branch once per candidate event, then hand the resulting
    # id set to the two places that read history: score()'s novelty bonus, and the email
    # branch's `exclude_already_sent`. Their own exact-membership checks do the rest
    # (§4.1, §8.2). The CONTENT filters never see it — that is the point of the split.
    sent_ids = frozenset(event.id for event in events if was_sent(state, event))
    # The write UI's overrides.yaml (package 14) — hand-edited via the GitHub Contents
    # API, never by the pipeline itself. Missing/corrupt file -> empty, never raises.
    overrides = load_overrides(overrides_path)
    hidden_ids = frozenset(overrides.hidden)
    pinned_ids = frozenset(overrides.pinned)

    before_filter = len(events)
    filtered = content_filter_with_reasons(events, config, hidden_ids=hidden_ids, now=moment)
    events = filtered.events
    dropped_by_content = before_filter - len(events)
    dropped_by_geo = sum(filtered.excluded[reason] for reason in GEO_REASONS)

    before_score = len(events)
    events = score(events, config, sent_ids=sent_ids, pinned_ids=pinned_ids, now=moment)
    dropped_by_min_score = before_score - len(events)

    grouped = group_with_counts(events, config)
    events = grouped.events

    # THE FORK. Everything above answers "does this event interest the reader" and is
    # identical on every run, so `events` is the site's full current view. Only below here
    # does the reader's history enter, and only on the email branch: the site is the full
    # view, the email is the delta. Skipping a day therefore costs an email, never a site
    # entry — the site is rebuilt from the same content-filtered set either way.
    #
    # Grouping runs BEFORE the fork on purpose. A collapsed row must be composed from every
    # event at that venue, not from whichever ones happened not to be in yesterday's mail;
    # otherwise the site's rows would change shape according to email history, which is the
    # same defect one level up.
    email_events = exclude_already_sent(events, sent_ids)
    suppressed_by_ledger = len(events) - len(email_events)

    # `events` is what the SITE publishes in full; the email shows a per-category slice of
    # what survives the ledger. The button at the top of the email promises the whole set,
    # so it is labelled with the site's count and not with the email's own.
    rendered = render_email(
        email_events,
        config,
        source_health=state.source_health,
        published_count=len(events),
        now=moment,
    )

    # The public site is not the email digest: it gets every post-group event (no
    # per_category_limit/total_limit, see render/web.py), and it is written regardless
    # of send_when_empty — the site's own heartbeat is "generated_at updated today",
    # independent of whether the email went out.
    web_output = render_web(events, config, source_health=state.source_health, now=moment)
    write_site(web_output, site_dir, archive_keep_days=config.site.archive_keep_days)

    delivered = True
    if rendered.sent_events or config.newsletter.send_when_empty:
        delivered = _deliver(rendered, config)

    # AUDIT-2 BLOCKER: record_sent must not fire for events that were never actually
    # delivered — otherwise a missing/misconfigured PROFILE_YAML (recipient_email absent)
    # or an all-disabled delivery list permanently poisons the ledger against that day's
    # events, and fixing the misconfiguration afterward can never recover them. When
    # nothing was delivered, skip the ledger write instead so was_sent() stays False and
    # the same events are legitimately re-offered on the next run.
    if delivered:
        # `newsletter.ledger_records` decides what "shown" means (§8.2). Under "web" the
        # whole published set is recorded, because the site showed it — the email is only
        # the top slice. Under "email" the surplus stays unrecorded and queues for a later
        # run, which is the older behaviour from when the email was the only output.
        #
        # The gate above is unchanged either way: nothing is recorded when nothing was
        # delivered (AUDIT-2 BLOCKER). The site is written before this point regardless,
        # but a run that could not mail is a run the reader never heard about, and marking
        # its events seen would silence them permanently.
        seen = events if config.newsletter.ledger_records == "web" else rendered.sent_events
        state = record_sent(state, seen, sent_on=today)
        log.info(
            "ledger_recorded",
            scope=config.newsletter.ledger_records,
            recorded=len(seen),
            in_email=len(rendered.sent_events),
        )
    elif rendered.sent_events:
        log.error("delivery_no_op_ledger_not_updated", event_count=len(rendered.sent_events))
    elapsed = perf_counter() - started
    state = record_run(
        state,
        RunLogEntry(
            date=today,
            raw=len(raw_events),
            after_dedup=after_dedup,
            sent=len(rendered.sent_events),
            seconds=elapsed,
        ),
    )
    state = state.model_copy(update={"last_run": moment})
    save_state(state, state_path)

    summary = RunSummary(
        source_counts=source_counts,
        dropped_as_past=dict(normalized.dropped_as_past),
        merged=merged,
        dropped_by_content=dropped_by_content,
        dropped_by_geo=dropped_by_geo,
        dropped_by_min_score=dropped_by_min_score,
        suppressed_by_ledger=suppressed_by_ledger,
        ungrouped_venueless=grouped.ungrouped_venueless,
        sent=len(rendered.sent_events),
        drifted=drifted,
        seconds=elapsed,
    )
    log.info(
        "run_summary",
        sources=summary.source_counts,
        dropped_as_past=summary.dropped_as_past,
        merged=summary.merged,
        dropped_by_content=summary.dropped_by_content,
        dropped_by_geo=summary.dropped_by_geo,
        dropped_by_min_score=summary.dropped_by_min_score,
        suppressed_by_ledger=summary.suppressed_by_ledger,
        ungrouped_venueless=summary.ungrouped_venueless,
        sent=summary.sent,
        drifted=summary.drifted,
        seconds=round(summary.seconds, 2),
    )
    return summary


class _CountingClient:
    """Wraps the real client so the comparison can report calls made without
    GeminiCategorizer having to keep a counter for one caller's benefit."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def generate(self, *, model: str, prompt: str) -> str:
        self.calls += 1
        return self._inner.generate(model=model, prompt=prompt)


def _compare_with_llm(events: list[Event], config: Config) -> list[str]:
    """One measured answer to "what would the LLM do", instead of a week of watching
    production. Dry by construction: it renders nothing, sends nothing, writes no state and
    never touches the site — it is a read of the corpus and a print.

    It asks about EVERY event, not just the ones the rules gave up on, because the
    interesting disagreements include the ones where the rules were confident and wrong.
    That is `scope="all"`, and it exists for this command only."""
    require_llm_extra("--compare-llm needs the Gemini layer, but")
    if not os.environ.get("GEMINI_API_KEY"):
        raise ConfigError(
            "--compare-llm needs GEMINI_API_KEY in the environment. It is the same key the "
            "scheduled run uses; export it locally to measure before switching llm.enabled on."
        )

    from digest.llm.gemini import _RealGeminiClient

    # The layer refuses to run when the flag is off, and this command is exactly what you
    # run to decide whether to turn it on — so it is forced on for this call only. The
    # config object is frozen; this copy never leaves the function.
    measuring = config.model_copy(update={"llm": config.llm.model_copy(update={"enabled": True})})
    ceiling = measuring.llm.batch_size * measuring.llm.max_calls_per_run
    left_out = max(0, len(events) - ceiling)

    client = _CountingClient(_RealGeminiClient())
    started = perf_counter()
    judged = GeminiCategorizer(client=client, scope="all").categorize(events, measuring)
    elapsed = perf_counter() - started

    rules_by_id = {event.id: event for event in events}
    disagreements = [
        (rules_by_id[event.id], event)
        for event in judged
        if event.categories != rules_by_id[event.id].categories
    ]
    left_fallback = [
        (before, after)
        for before, after in disagreements
        if before.categories == [config.fallback_category]
    ]

    lines = [
        f"corpus                : {len(events)} events",
        f"asked the model about : {min(len(events), ceiling)}"
        + (f"  ({left_out} left out by max_calls_per_run)" if left_out else ""),
        f"agree                 : {len(events) - len(disagreements)}",
        f"disagree              : {len(disagreements)}",
        f"left {config.fallback_category:<17}: {len(left_fallback)}",
        f"api calls             : {client.calls}",
        f"wall clock            : {elapsed:.1f}s",
        "",
    ]
    if not disagreements:
        lines.append("no disagreements.")
        return lines

    lines.append("DISAGREEMENTS")
    for before, after in disagreements:
        rule_category = before.categories[0] if before.categories else "—"
        llm_category = after.categories[0] if after.categories else "—"
        lines.append(f"  {before.title[:64]}")
        lines.append(f"    rules: {rule_category:<14} llm: {llm_category}")
        lines.append(f"    signals: {_rule_signals(before, config) or '(none — fell through)'}")
    return lines


def _rule_signals(event: Event, config: Config) -> str:
    """What produced the rule answer, so a disagreement can be judged rather than counted."""
    category = event.categories[0] if event.categories else None
    rules = config.categories.get(category) if category else None
    if rules is None:
        return ""
    scored = score_category(event, rules)
    return ", ".join(f"{name}={weight:g}" for name, weight in sorted(scored.signals.items()))


def _llm_categorize(events: list[Event], config: Config) -> list[Event]:
    """§7.5's optional second pass, behind `llm.enabled`. Until now nothing called it, so
    the flag was on in config.yaml and changed nothing — the file said one thing and the
    system did another.

    It is wired HERE and not inside `pipeline/categorize.py` because it makes a network
    call, and CLAUDE.md fixes the pipeline stages as pure functions: fetch and delivery are
    the only I/O. This is the composition layer, which is where that call belongs.

    Only the real run. `--dry`, `digest categorize` and `digest explain` stay rule-only:
    they exist to inspect the rules against a saved fixture, and a debug command that
    spends API quota and returns something different each time is not that.

    The cache is per-run — identical text is not sent twice within one run. Persisting it
    across runs (state.json is the natural home) is not wired; see GeminiCategorizer."""
    if not config.llm.enabled:
        return events
    return GeminiCategorizer().categorize(events, config)


def _deliver(rendered: RenderedEmail, config: Config) -> bool:
    """Returns whether at least one target actually delivered. AUDIT-2 BLOCKER: the
    caller must not call record_sent when this is False, or a disabled/unimplemented
    target list, or a deliverer's own graceful skip (SPEC 5.3's missing-recipient case),
    would permanently mark that day's events as sent without ever delivering them."""
    delivered = False
    for target in config.delivery:
        if not target.enabled:
            continue
        deliverer_cls = _DELIVERERS.get(target.type)
        if deliverer_cls is None:
            log.warning("deliverer_not_implemented", type=target.type)
            continue
        if deliverer_cls().send(rendered.subject, rendered.html, rendered.text, config):
            delivered = True
    return delivered


def _run_sources(
    sources: list[Source], config: Config, state: State, today: date
) -> tuple[list[RawEvent], State, dict[str, int], list[str]]:
    """Serial, per-source try/except (CLAUDE.md 6 and 10): one source's fetch or parse
    failure never fails the run, it only ever costs that source's events for today."""
    fetchers: dict[str, HttpFetcher] = {name: cls(config) for name, cls in _FETCHERS.items()}
    raw_events: list[RawEvent] = []
    counts: dict[str, int] = {}
    drifted: list[str] = []
    try:
        for source in sources:
            if not source.enabled:
                log.info("source_disabled_in_config", source_id=source.id)
                continue
            health = state.source_health.get(source.id, SourceHealth())
            if health.disabled_until is not None and health.disabled_until > today:
                log.info(
                    "source_skipped",
                    source_id=source.id,
                    disabled_until=health.disabled_until.isoformat(),
                )
                continue

            try:
                events = _fetch_source(source, fetchers, health)
            except DigestError as exc:
                failures = health.consecutive_failures + 1
                disabled_until = (
                    today + timedelta(days=_DISABLE_DAYS)
                    if failures >= _DISABLE_AFTER_FAILURES
                    else health.disabled_until
                )
                log.error(
                    "source_failed",
                    source_id=source.id,
                    error=str(exc),
                    consecutive_failures=failures,
                )
                state = _update_health(
                    state,
                    source.id,
                    health.model_copy(
                        update={
                            "consecutive_failures": failures,
                            "disabled_until": disabled_until,
                        }
                    ),
                )
                counts[source.id] = 0
                continue

            count = len(events)
            counts[source.id] = count
            if health.last_count > _DRIFT_MIN_PREVIOUS_COUNT and count == 0:
                log.error("selector_drift", source_id=source.id, previous_count=health.last_count)
                drifted.append(source.id)

            state = _update_health(
                state,
                source.id,
                health.model_copy(
                    update={
                        "consecutive_failures": 0,
                        "last_ok": today,
                        "last_count": count,
                        "disabled_until": None,
                    }
                ),
            )
            raw_events.extend(events)
    finally:
        for fetcher in fetchers.values():
            fetcher.close()
    return raw_events, state, counts, drifted


def _update_health(state: State, source_id: str, health: SourceHealth) -> State:
    return state.model_copy(update={"source_health": {**state.source_health, source_id: health}})


def _fetch_source(
    source: Source, fetchers: dict[str, HttpFetcher], health: SourceHealth
) -> list[RawEvent]:
    fetcher = fetchers.get(source.fetcher)
    if fetcher is None:
        raise FetchError(f"{source.id}: unsupported fetcher {source.fetcher!r}")
    events: list[RawEvent] = []
    for task in source.discover():
        result = fetcher.fetch(
            task,
            source_id=source.id,
            rate_limit_seconds=source.rate_limit_seconds,
            etag=health.etag,
        )
        events.extend(source.parse(result))
    return events


@app.command()
def fetch(
    source_id: str,
    fixture: Annotated[
        Path | None,
        typer.Option(help="Parse this saved response instead of calling the source."),
    ] = None,
) -> None:
    """Fetch raw events from a single source."""
    if fixture is None:
        raise NotImplementedError
    for line in fixture_table(source_id, fixture):
        typer.echo(line)


@app.command()
def categorize(
    source_id: Annotated[str, typer.Option("--source", help="Source to read events from.")] = (
        "port-hu"
    ),
    fixture: Annotated[
        Path | None,
        typer.Option(help="Parse this saved response instead of calling the source."),
    ] = None,
    explain: Annotated[
        str | None,
        typer.Option(help="Print the score breakdown for this event id, instead of a table."),
    ] = None,
    compare_llm: Annotated[
        bool,
        typer.Option(
            "--compare-llm",
            help="Categorize twice — rules only, then rules plus Gemini — and print the diff.",
        ),
    ] = False,
) -> None:
    """Categorize events and show where the category scores come from."""
    if fixture is None:
        raise NotImplementedError
    config, raw = _load_raw_events(source_id, fixture)
    events = categorize_events(normalize(raw, config), config)

    if compare_llm:
        typer.echo("\n".join(_compare_with_llm(events, config)))
        return
    if explain is None:
        typer.echo("\n".join(_render_categories_table(events)))
        return
    matched = next((event for event in events if event.id == explain), None)
    if matched is None:
        raise ConfigError(f"no event with id {explain!r} in this fixture")
    typer.echo("\n".join(_render_explain_table(matched, config)))


@app.command()
def explain(
    event_id: str,
    source_id: Annotated[str, typer.Option("--source", help="Source to read events from.")] = (
        "port-hu"
    ),
    fixture: Annotated[
        Path | None,
        typer.Option(help="Parse this saved response instead of calling the source."),
    ] = None,
) -> None:
    """Print the score breakdown of a single event."""
    if fixture is None:
        raise NotImplementedError
    config, raw = _load_raw_events(source_id, fixture)
    # filter() and score()'s min_score cut are deliberately skipped: this is a debugging
    # tool for "why did this event get this score", and an event that would end up
    # excluded is exactly the one someone wants to inspect. dedup, recurrence and
    # categorize still run — score_breakdown reflects the event as it actually reaches
    # scoring, just without discarding it afterwards.
    events = recurrence(dedup(normalize(raw, config), config), config)
    events = categorize_events(events, config)

    matched = next((event for event in events if event.id == event_id), None)
    if matched is None:
        raise ConfigError(f"no event with id {event_id!r} in this fixture")
    typer.echo("\n".join(_render_score_table(score_one(matched, config))))


def _load_raw_events(source_id: str, fixture: Path) -> tuple[Config, list[RawEvent]]:
    config = load_config(Path("config.yaml"), Path("sources"), os.environ.get("PROFILE_YAML"))
    sources: dict[str, Source] = {source.id: source for source in load_sources(config)}
    if source_id not in sources:
        raise ConfigError(f"unknown source {source_id!r}; known sources: {sorted(sources)}")

    source = sources[source_id]
    text = fixture.read_text(encoding="utf-8")

    # Dispatch on what the source actually asked for. This used to call json.loads
    # unconditionally, which made `digest fetch --fixture` unusable for every `http`
    # source — bigcitylife and tixa could only ever be exercised from
    # their unit tests, never through the code path a real run takes.
    payload: Any = None
    if source.fetcher == "api":
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ParseError(f"{fixture} is not valid JSON: {exc}") from exc

    result = FetchResult(
        task=FetchTask(url=_fixture_base_url(source_id, config, fixture)),
        status=200,
        text=text,
        json=payload,
        from_cache=False,
    )
    return config, list(source.parse(result))


def _fixture_base_url(source_id: str, config: Config, fixture: Path) -> str:
    """The listing URL the fixture was saved from, when the config names one. It is what
    `absolute: true` fields resolve against (§6.3), so passing the local path instead
    turns every relative href into a file:// URL and the output silently disagrees with a
    real run."""
    urls = ((config.sources.get(source_id) or {}).get("listing") or {}).get("urls") or []
    return str(urls[0]) if urls else str(fixture)


def fixture_table(source_id: str, fixture: Path) -> list[str]:
    _, raw = _load_raw_events(source_id, fixture)
    return _render_raw_table(raw)


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]


def _render_raw_table(events: list[RawEvent]) -> list[str]:
    rows = [("START", "DISTRICT", "VENUE", "TITLE")]
    rows += [
        (
            event.start_raw or "",
            str(event.district_raw or ""),
            (event.venue_name or "")[:24],
            event.title[:60],
        )
        for event in events
    ]
    lines = _table(rows)
    lines.append(f"{len(events)} events")
    return lines


def _render_categories_table(events: list[Event]) -> list[str]:
    rows = [("ID", "CATEGORIES", "TITLE")]
    rows += [
        (event.id, ",".join(event.categories), event.title[:60])
        for event in sorted(events, key=lambda event: event.id)
    ]
    lines = _table(rows)
    lines.append(f"{len(events)} events")
    return lines


def _render_explain_table(event: Event, config: Config) -> list[str]:
    scores = explain_event(event, config)
    ranked = sorted(scores.items(), key=lambda item: item[1].total, reverse=True)

    rows = [("CATEGORY", "TOTAL", "SIGNALS")]
    rows += [
        (
            name,
            f"{score.total:g}",
            ", ".join(f"{signal}={value:g}" for signal, value in score.signals.items()) or "—",
        )
        for name, score in ranked
    ]
    lines = [f"event {event.id}: {event.title}", *_table(rows)]
    lines.append(f"assigned: {', '.join(event.categories)}")
    return lines


def _render_score_table(event: Event) -> list[str]:
    rows = [("TERM", "VALUE")]
    rows += [(name, f"{value:+g}") for name, value in event.score_breakdown.items()]
    rows.append(("TOTAL", f"{event.score:+g}"))
    return [f"event {event.id}: {event.title}", *_table(rows)]
