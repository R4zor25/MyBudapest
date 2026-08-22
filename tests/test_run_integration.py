from __future__ import annotations

import json
import smtplib
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Self
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
import typer
from structlog.testing import capture_logs

from digest import cli
from digest.cli import _run_pipeline, run
from digest.config import Config, DeliveryTarget, FetchConfig
from digest.fetch.base import FetchResult, FetchTask
from digest.models import RawEvent, make_event_id
from digest.state import SourceHealth, State, load_state

BUDAPEST = ZoneInfo("Europe/Budapest")
NOW = datetime(2026, 8, 16, 4, 30, tzinfo=BUDAPEST)


class FakeSource:
    """A minimal digest.sources.registry.Source, built directly instead of through the
    YAML/plugin loader — real HTTP calls still happen, respx mocks the transport, so this
    exercises the real HttpFetcher/ApiFetcher path (CLAUDE.md 7: no test hits the network)."""

    def __init__(
        self,
        source_id: str,
        *,
        url: str,
        events: list[RawEvent] | None = None,
        enabled: bool = True,
        fetcher: str = "api",
    ) -> None:
        self.id = source_id
        self.name = source_id
        self.enabled = enabled
        self.priority = 10
        self.fetcher = fetcher
        self.rate_limit_seconds = 0.0
        self.url = url
        self._events = events or []

    def discover(self) -> Iterable[FetchTask]:
        yield FetchTask(url=self.url)

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        return self._events


class _FakeSmtp:
    """Stands in for smtplib.SMTP — see tests/test_delivery.py for the same double."""

    instances: ClassVar[list[_FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.sent: Any = None
        _FakeSmtp.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def send_message(self, message: Any) -> None:
        self.sent = message


@pytest.fixture(autouse=True)
def _reset_fake_smtp() -> None:
    _FakeSmtp.instances.clear()


@pytest.fixture(autouse=True)
def _smtp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)


def make_raw(
    source_id: str,
    key: str,
    *,
    title: str = "Event",
    start: str = "2026-08-20T20:00:00+02:00",
    end: str | None = None,
) -> RawEvent:
    return RawEvent(
        source_id=source_id,
        source_event_key=key,
        title=title,
        url=f"https://example.com/{source_id}/{key}",
        start_raw=start,
        end_raw=end,
    )


def make_config() -> Config:
    return Config(
        recipient_email="me@example.com",
        delivery=[DeliveryTarget(type="smtp", enabled=True)],
        # No retries and no robots.txt fetch: one mocked response per source is enough,
        # and it keeps the failing-source test from needing a sleep() patch at all.
        fetch=FetchConfig(max_retries=0, respect_robots_txt=False),
    )


@respx.mock
def test_a_source_whose_dates_stopped_rolling_forward_is_named_in_the_summary(
    tmp_path: Path,
) -> None:
    """The failure this count exists for. A frozen feed does not error and does not stop
    parsing — `source_counts` still reports 2, and §13's drift check counts records parsed,
    so neither of them notices. `dropped_as_past` names the source that went quiet."""
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://example.com/stale").mock(return_value=httpx.Response(200, json={}))
    fresh = FakeSource("good", url="https://example.com/good", events=[make_raw("good", "1")])
    stale = FakeSource(
        "stale",
        url="https://example.com/stale",
        events=[
            make_raw("stale", "1", start="2026-07-01T20:00:00+02:00"),
            make_raw("stale", "2", start="2026-07-02T20:00:00+02:00"),
        ],
    )

    summary = _run_pipeline(
        make_config(),
        [fresh, stale],
        State(),
        tmp_path / "state.json",
        tmp_path / "site",
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    assert summary.source_counts == {"good": 1, "stale": 2}
    assert summary.dropped_as_past == {"stale": 2}


@respx.mock
def test_the_full_pipeline_runs_and_produces_output(tmp_path: Path) -> None:
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    source = FakeSource(
        "good",
        url="https://example.com/good",
        events=[
            make_raw("good", "1", title="Villon-est"),
            make_raw("good", "2", title="HØT SPØT"),
        ],
    )
    state_path = tmp_path / "state.json"
    site_dir = tmp_path / "site"
    overrides_path = tmp_path / "overrides.yaml"

    summary = _run_pipeline(
        make_config(), [source], State(), state_path, site_dir, overrides_path, now=NOW
    )

    assert summary.source_counts == {"good": 2}
    assert summary.dropped_as_past == {}
    assert summary.sent == 2
    (smtp,) = _FakeSmtp.instances
    assert smtp.sent is not None
    assert state_path.exists()
    reloaded = load_state(state_path)
    start = datetime(2026, 8, 20, 20, 0, tzinfo=BUDAPEST)
    expected_ids = {
        make_event_id("Villon-est", start, None),
        make_event_id("HØT SPØT", start, None),
    }
    assert {entry.id for entry in reloaded.sent} == expected_ids


@respx.mock
def test_a_still_running_event_is_not_resent_once_its_effective_date_is_in_the_past(
    tmp_path: Path,
) -> None:
    # AUDIT-5 BLOCKER: reproduces the audit's own two-consecutive-runs check (DEPLOY.md's
    # "most important" check) against a still-running event -- a multi-day exhibition or a
    # weekly series, exactly normalize()'s "(end or start) < now" carve-out. Before this
    # fix, purge() keyed the ledger's protection off effective_date alone, so it expired
    # the day after the event first sent -- long before normalize() itself stopped
    # re-offering the same event, which only happens once `end` passes.
    respx.get("https://example.com/still-running").mock(return_value=httpx.Response(200, json={}))
    source = FakeSource(
        "still-running",
        url="https://example.com/still-running",
        events=[
            make_raw(
                "still-running",
                "1",
                title="Villon-est",
                start="2026-08-10T19:00:00+02:00",
                end="2026-08-25T23:59:00+02:00",
            )
        ],
    )
    state_path = tmp_path / "state.json"
    site_dir = tmp_path / "site"
    overrides_path = tmp_path / "overrides.yaml"
    config = make_config()

    summary1 = _run_pipeline(
        config,
        [source],
        State(),
        state_path,
        site_dir,
        overrides_path,
        now=datetime(2026, 8, 16, 4, 30, tzinfo=BUDAPEST),
    )
    # A fresh load, mirroring the real system: each day's `digest run` starts a new
    # process that checks out yesterday's committed state.json from scratch.
    reloaded_state = load_state(state_path)
    summary2 = _run_pipeline(
        config,
        [source],
        reloaded_state,
        state_path,
        site_dir,
        overrides_path,
        now=datetime(2026, 8, 20, 4, 30, tzinfo=BUDAPEST),
    )

    assert summary1.sent == 1
    assert summary2.sent == 0


@respx.mock
def test_a_missing_recipient_email_does_not_poison_the_ledger(tmp_path: Path) -> None:
    # AUDIT-2 BLOCKER: recipient_email=None (the exact shape of a missing or misconfigured
    # PROFILE_YAML — SPEC 5.3's "must not fail the run" case) makes SmtpDeliverer.send() a
    # graceful no-op: nothing is ever delivered. Before this fix, record_sent() fired
    # anyway and permanently marked the event as sent — fixing the profile afterward could
    # never recover it, since was_sent() would keep saying "already sent" forever.
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    source = FakeSource(
        "good", url="https://example.com/good", events=[make_raw("good", "1", title="Villon-est")]
    )
    state_path = tmp_path / "state.json"
    site_dir = tmp_path / "site"
    overrides_path = tmp_path / "overrides.yaml"
    config = Config(
        recipient_email=None,
        delivery=[DeliveryTarget(type="smtp", enabled=True)],
        fetch=FetchConfig(max_retries=0, respect_robots_txt=False),
    )

    with capture_logs() as logs:
        summary = _run_pipeline(
            config, [source], State(), state_path, site_dir, overrides_path, now=NOW
        )

    assert summary.sent == 1
    assert _FakeSmtp.instances == []
    reloaded = load_state(state_path)
    assert reloaded.sent == []
    no_op_entries = [
        entry for entry in logs if entry["event"] == "delivery_no_op_ledger_not_updated"
    ]
    assert len(no_op_entries) == 1
    assert no_op_entries[0]["log_level"] == "error"
    assert no_op_entries[0]["event_count"] == 1


@respx.mock
def test_one_failing_source_does_not_fail_the_run_and_the_other_survives(tmp_path: Path) -> None:
    respx.get("https://example.com/bad").mock(return_value=httpx.Response(500))
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    bad = FakeSource("bad", url="https://example.com/bad", events=[make_raw("bad", "1")])
    good = FakeSource(
        "good", url="https://example.com/good", events=[make_raw("good", "1", title="Survivor")]
    )
    state_path = tmp_path / "state.json"
    site_dir = tmp_path / "site"
    overrides_path = tmp_path / "overrides.yaml"

    summary = _run_pipeline(
        make_config(), [bad, good], State(), state_path, site_dir, overrides_path, now=NOW
    )

    assert summary.source_counts == {"bad": 0, "good": 1}
    assert summary.sent == 1
    reloaded = load_state(state_path)
    assert reloaded.source_health["bad"].consecutive_failures == 1
    assert reloaded.source_health["good"].consecutive_failures == 0


@respx.mock
def test_a_source_disables_itself_after_five_consecutive_failures(tmp_path: Path) -> None:
    respx.get("https://example.com/bad").mock(return_value=httpx.Response(500))
    bad = FakeSource("bad", url="https://example.com/bad")
    state = State(source_health={"bad": SourceHealth(consecutive_failures=4)})
    state_path = tmp_path / "state.json"
    site_dir = tmp_path / "site"
    overrides_path = tmp_path / "overrides.yaml"

    _run_pipeline(make_config(), [bad], state, state_path, site_dir, overrides_path, now=NOW)

    reloaded = load_state(state_path)
    health = reloaded.source_health["bad"]
    assert health.consecutive_failures == 5
    assert health.disabled_until == NOW.date() + timedelta(days=7)


@respx.mock
def test_a_disabled_source_is_skipped_without_being_fetched(tmp_path: Path) -> None:
    route = respx.get("https://example.com/bad").mock(return_value=httpx.Response(200, json={}))
    disabled_until = NOW.date() + timedelta(days=3)
    bad = FakeSource("bad", url="https://example.com/bad", events=[make_raw("bad", "1")])
    state = State(source_health={"bad": SourceHealth(disabled_until=disabled_until)})
    state_path = tmp_path / "state.json"
    site_dir = tmp_path / "site"
    overrides_path = tmp_path / "overrides.yaml"

    summary = _run_pipeline(
        make_config(), [bad], state, state_path, site_dir, overrides_path, now=NOW
    )

    assert route.call_count == 0
    assert summary.source_counts == {}
    # A skip must leave health exactly as it was — no last_ok stamp, no reset counter.
    reloaded = load_state(state_path)
    health = reloaded.source_health["bad"]
    assert health.disabled_until == disabled_until
    assert health.last_ok is None
    assert health.last_count == 0


@respx.mock
def test_a_source_returning_zero_after_a_high_previous_count_logs_selector_drift(
    tmp_path: Path,
) -> None:
    respx.get("https://example.com/quiet").mock(return_value=httpx.Response(200, json={}))
    quiet = FakeSource("quiet", url="https://example.com/quiet", events=[])
    state = State(source_health={"quiet": SourceHealth(last_count=40)})
    state_path = tmp_path / "state.json"
    site_dir = tmp_path / "site"
    overrides_path = tmp_path / "overrides.yaml"

    with capture_logs() as logs:
        summary = _run_pipeline(
            make_config(), [quiet], state, state_path, site_dir, overrides_path, now=NOW
        )

    assert summary.drifted == ["quiet"]
    drift_entries = [entry for entry in logs if entry["event"] == "selector_drift"]
    assert len(drift_entries) == 1
    assert drift_entries[0]["log_level"] == "error"
    assert drift_entries[0]["source_id"] == "quiet"
    reloaded = load_state(state_path)
    # Drift is a signal, not a failure: it must not trip the auto-disable counter.
    assert reloaded.source_health["quiet"].consecutive_failures == 0


def test_dry_run_never_reaches_the_real_pipeline_or_touches_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _must_not_be_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("--dry must never reach _run_real")

    monkeypatch.setattr(cli, "_run_real", _must_not_be_called)
    out = tmp_path / "dry.html"

    run(dry=True, source_id="port-hu", fixture=Path("tests/fixtures/port_hu_list.json"), out=out)

    assert out.exists()
    assert out.read_text(encoding="utf-8")


def test_a_malformed_profile_yaml_key_does_not_leak_the_secret_value_to_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path, sources_dir: Path
) -> None:
    # AUDIT-3 BLOCKER: a mistyped PROFILE_YAML key (an easy mistake hand-editing a GitHub
    # secret textarea, per DEPLOY.md) used to reach pydantic.ValidationError's default
    # str(), which embeds the raw offending value -- printed straight into a real, public
    # Actions log once this repo is public. _run_real must catch it, log a redacted
    # summary, and exit cleanly instead of letting the raw exception propagate.
    monkeypatch.setenv("PROFILE_YAML", 'recipient_emailx: "realsecret.person@gmail.com"\n')

    with capture_logs() as logs, pytest.raises(typer.Exit) as exc_info:
        cli._run_real(
            config_path,
            sources_dir,
            tmp_path / "state.json",
            tmp_path / "site",
            tmp_path / "overrides.yaml",
        )

    assert exc_info.value.exit_code == 1
    invalid_entries = [entry for entry in logs if entry["event"] == "config_invalid"]
    assert len(invalid_entries) == 1
    assert invalid_entries[0]["log_level"] == "error"
    assert "realsecret.person@gmail.com" not in repr(invalid_entries[0])


def test_a_broken_profile_yaml_syntax_error_does_not_leak_a_source_snippet_to_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path, sources_dir: Path
) -> None:
    # Same finding, the other trigger: a YAML syntax error (indentation damage from a
    # copy-paste into a secret textarea) reaches yaml.YAMLError's default str(), which
    # reproduces a verbatim source-line snippet -- here, a keyword straight from the
    # secret profile.
    monkeypatch.setenv(
        "PROFILE_YAML", 'scoring:\n  keyword_boosts: { realsecretkeyword: 3, "craft beer": 2\n'
    )

    with capture_logs() as logs, pytest.raises(typer.Exit) as exc_info:
        cli._run_real(
            config_path,
            sources_dir,
            tmp_path / "state.json",
            tmp_path / "site",
            tmp_path / "overrides.yaml",
        )

    assert exc_info.value.exit_code == 1
    invalid_entries = [entry for entry in logs if entry["event"] == "config_invalid"]
    assert len(invalid_entries) == 1
    assert "realsecretkeyword" not in repr(invalid_entries[0])


# --------------------------------------------------------------------------------------
# What the ledger records: the site's full set, or the email's slice (§8.2)
# --------------------------------------------------------------------------------------


def _ledger_config(scope: str, per_category_limit: int = 1) -> Config:
    config = make_config()
    return config.model_copy(
        update={
            "newsletter": config.newsletter.model_copy(
                update={"ledger_records": scope, "per_category_limit": per_category_limit}
            )
        }
    )


def _two_events() -> list[RawEvent]:
    # Same category, so per_category_limit=1 puts one in the email and leaves the other to
    # the site alone. Distinct scores so which one is which is deterministic.
    return [
        make_raw("good", "1", title="Villon-est", start="2026-08-20T20:00:00+02:00"),
        make_raw("good", "2", title="HØT SPØT", start="2026-08-21T20:00:00+02:00"),
    ]


@respx.mock
def test_web_scope_records_an_event_the_email_never_showed(tmp_path: Path) -> None:
    """The site published it, so the reader has had their chance to see it. Recording only
    the email's slice would spend tomorrow's three slots on today's leftovers — which the
    page already showed — instead of tomorrow's best."""
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    source = FakeSource("good", url="https://example.com/good", events=_two_events())
    state_path = tmp_path / "state.json"

    summary = _run_pipeline(
        _ledger_config("web"),
        [source],
        State(),
        state_path,
        tmp_path / "site",
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    assert summary.sent == 1, "the email carries one of the two"
    assert len(load_state(state_path).sent) == 2, "the ledger records both"


@respx.mock
def test_web_scope_means_the_surplus_does_not_come_back_tomorrow(tmp_path: Path) -> None:
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    state_path = tmp_path / "state.json"
    config = _ledger_config("web")
    for _ in range(2):
        source = FakeSource("good", url="https://example.com/good", events=_two_events())
        summary = _run_pipeline(
            config,
            [source],
            load_state(state_path) if state_path.exists() else State(),
            state_path,
            tmp_path / "site",
            tmp_path / "overrides.yaml",
            now=NOW,
        )

    assert summary.sent == 0, "both were recorded on the first run, so nothing is left"


@respx.mock
def test_email_scope_keeps_the_old_queueing_behaviour(tmp_path: Path) -> None:
    """The switch is reversible in one config line, and this is the other position: only
    what the email showed is recorded, so the surplus is re-offered on the next run."""
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    state_path = tmp_path / "state.json"
    config = _ledger_config("email")
    source = FakeSource("good", url="https://example.com/good", events=_two_events())

    first = _run_pipeline(
        config,
        [source],
        State(),
        state_path,
        tmp_path / "site",
        tmp_path / "overrides.yaml",
        now=NOW,
    )
    assert first.sent == 1
    assert len(load_state(state_path).sent) == 1

    source = FakeSource("good", url="https://example.com/good", events=_two_events())
    second = _run_pipeline(
        config,
        [source],
        load_state(state_path),
        state_path,
        tmp_path / "site",
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    assert second.sent == 1, "yesterday's leftover is served today"


# --------------------------------------------------------------------------------------
# The site is the full current view; the email is the delta (§7.6)
# --------------------------------------------------------------------------------------


def _site_events(site_dir: Path) -> list[str]:
    payload = json.loads((site_dir / "events.json").read_text(encoding="utf-8"))
    return sorted(record["id"] for record in payload["events"])


@respx.mock
def test_two_runs_publish_the_same_site_with_a_full_ledger_in_between(tmp_path: Path) -> None:
    """The defect this closes: the ledger sat among the content filters, so every run
    removed from the SITE whatever the last email had covered, and the page shrank toward
    empty. Re-running must be idempotent for the site."""
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    state_path, site_dir = tmp_path / "state.json", tmp_path / "site"
    config = make_config()

    first = _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        State(),
        state_path,
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )
    after_first = _site_events(site_dir)
    ledger = load_state(state_path)
    assert ledger.sent, "the second run must start from a non-empty ledger"

    second = _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        ledger,
        state_path,
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    assert _site_events(site_dir) == after_first
    assert first.dropped_by_content == second.dropped_by_content
    # The email is where the history shows up, and only there.
    assert second.sent == 0
    assert second.suppressed_by_ledger == len(after_first)


@respx.mock
def test_a_sent_event_stays_on_the_site_and_leaves_the_email(tmp_path: Path) -> None:
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    state_path, site_dir = tmp_path / "state.json", tmp_path / "site"
    config = _ledger_config("email", per_category_limit=1)

    _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        State(),
        state_path,
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )
    # state.sent holds SentEntry records, not bare ids (§8.2) — the id is one field.
    sent_ids = {entry.id for entry in load_state(state_path).sent}
    assert len(sent_ids) == 1

    summary = _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        load_state(state_path),
        state_path,
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    on_site = set(_site_events(site_dir))
    assert sent_ids <= on_site, "an emailed event is still part of the site's full view"
    assert summary.suppressed_by_ledger == 1


@respx.mock
def test_clearing_the_ledger_leaves_the_site_unchanged(tmp_path: Path) -> None:
    # The other direction of the same property: the site does not depend on ledger state at
    # all, so wiping it changes nothing about what gets published.
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    site_dir = tmp_path / "site"
    config = make_config()

    _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        State(),
        tmp_path / "a.json",
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )
    with_ledger = _site_events(site_dir)

    _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        State(),
        tmp_path / "b.json",
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    assert _site_events(site_dir) == with_ledger


@respx.mock
def test_the_summary_counts_content_drops_and_ledger_suppressions_apart(tmp_path: Path) -> None:
    """Two numbers, because they answer different questions and move for different reasons:
    content drops are reproducible, ledger suppressions grow with the reader's history."""
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    state_path, site_dir = tmp_path / "state.json", tmp_path / "site"
    config = make_config()
    config = config.model_copy(
        update={"filters": config.filters.model_copy(update={"blocked_keywords": ["HØT"]})}
    )

    first = _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        State(),
        state_path,
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    assert first.dropped_by_content == 1, "the blocked keyword, not the ledger"
    assert first.suppressed_by_ledger == 0, "nothing has been sent yet"

    second = _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=_two_events())],
        load_state(state_path),
        state_path,
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    assert second.dropped_by_content == 1, "content drops are the same every run"
    assert second.suppressed_by_ledger == 1, "and the ledger number is the one that grew"


@respx.mock
def test_a_dated_event_with_no_clock_reaches_both_outputs(tmp_path: Path) -> None:
    """ "Undated" has meant two different things in this codebase, and only one of them is a
    reason to drop anything. An event with a DATE and no CLOCK is a fully supported record
    since `start_time_known` (§7.1) — several sources publish exactly that shape — and no
    content filter may exclude it. What cooltix drops is the other case, a record with no
    start at all, and it drops it at the parser, not here."""
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    clockless = make_raw("good", "1", title="Dátum óra nélkül", start="2026-08-20")
    timed = make_raw("good", "2", title="Órával", start="2026-08-20T20:00:00+02:00")
    site_dir = tmp_path / "site"

    summary = _run_pipeline(
        make_config(),
        [FakeSource("good", url="https://example.com/good", events=[clockless, timed])],
        State(),
        tmp_path / "state.json",
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    published = json.loads((site_dir / "events.json").read_text(encoding="utf-8"))["events"]
    by_title = {record["title"]: record for record in published}
    assert set(by_title) == {"Dátum óra nélkül", "Órával"}
    assert by_title["Dátum óra nélkül"]["start_time_known"] is False
    assert by_title["Órával"]["start_time_known"] is True
    assert summary.dropped_by_content == 0
    assert summary.sent == 2, "and it is in the email too, not only on the site"


@respx.mock
def test_a_fallback_category_event_reaches_both_outputs(tmp_path: Path) -> None:
    """With the fallback name present in `filters.categories`, an event no rule recognised
    is published and mailed rather than silently discarded. Without it the content filter
    removes every one of them as `category_not_allowed` — which is a PROFILE omission, not
    a pipeline defect, and is what was happening on the shipped profile."""
    respx.get("https://example.com/good").mock(return_value=httpx.Response(200, json={}))
    config = make_config()
    allowed = [*config.categories, config.fallback_category]
    config = config.model_copy(
        update={"filters": config.filters.model_copy(update={"categories": allowed})}
    )
    site_dir = tmp_path / "site"

    summary = _run_pipeline(
        config,
        [FakeSource("good", url="https://example.com/good", events=[make_raw("good", "1")])],
        State(),
        tmp_path / "state.json",
        site_dir,
        tmp_path / "overrides.yaml",
        now=NOW,
    )

    published = json.loads((site_dir / "events.json").read_text(encoding="utf-8"))["events"]
    assert [record["categories"] for record in published] == [[config.fallback_category]]
    assert summary.dropped_by_content == 0
    assert summary.sent == 1
