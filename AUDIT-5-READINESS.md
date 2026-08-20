# Audit 5 — Operational readiness

Scope: SPEC.md §10, §11, §13; `.github/workflows/digest.yml`; `README.md`; `pyproject.toml`;
`src/digest/cli.py`; `src/digest/state.py`. Report only — no source file was modified. Baseline:
`git rev-parse HEAD` = `42a3a7d` ("chore(audit): record audit 4 findings — no blockers to fix"),
working tree as found. Test baseline: `./.venv/bin/python -m pytest -q` → **323 passed in
1.13s** (same count as Audit 4's baseline — no source changes between audits).

The repo's own `state/state.json` does not exist in this checkout (no `state/` directory at
all). All state-related checks (items 6, 7) were therefore run against a scratch `State`/
`state.json` in throwaway `tempfile.mkdtemp()` directories under the OS temp dir, never
against a real file inside the repo — there was no real one to corrupt. Item 1's clean-install
check used a `uv`-managed Python 3.12.14 interpreter and virtualenvs created under `/tmp`,
entirely outside the repo.

## Summary

**2 BLOCKER, 1 MAJOR, 1 MINOR.** Both BLOCKERs were found by actually running the pipeline
against the real, committed fixtures and the real, committed source configs — not by reading
code. Framed against the task's own question ("will this survive contact with the real world
on Monday morning"): as configured right now, no. The first live run would deliver a thin,
single-source, weekend-only digest because the flagship source's listing URL is an unfilled
placeholder, and the very next day's run would silently re-send some of that first digest's
events, because the ledger's purge logic and the "still-running event" carve-out in `normalize`
disagree with each other about what "in the past" means.

1. **[BLOCKER] Idempotence fails for any event still active past its listed start date**
   (multi-day exhibitions, "available until X" online events, weeks-long recurring series).
   `purge()` drops a ledger entry once `entry.d` (the event's `effective_date`, fixed at first
   sight) is before "today" — but `normalize()` deliberately keeps such an event in the pipeline
   until its `end` date passes, not its `start`/`effective_date`. The two assumptions
   contradict each other. Reproduced end-to-end, twice, against the real `port-hu` fixture: run
   1 sends 2 events and records 2 ledger entries; run 2, against the *same persisted ledger*,
   sends the *same* 2 events again. `dropped_by_filter` is 0 in both runs — the `already_sent`
   exclusion never fires because its ledger entry is already gone by the time `filter()` runs.
   This is exactly the check DEPLOY.md §7 itself calls "the most important" ("Ha nem nullát ad,
   a ledger nem működik, és ne menj tovább") and exactly the coverage gap Audit 4 already
   flagged abstractly (its MAJOR: "no test exercises the production `was_sent` → `sent_ids` →
   `filter`/`score` wiring end-to-end"). This is that gap's concrete, reproducible consequence.

2. **[BLOCKER] The backbone source (`port-hu`, priority 10, `enabled: true`) has
   `listing.urls: []`** — `discover()` yields zero fetch tasks, forever, logging only a
   `WARNING`. This is a known, deliberate deferral (SPEC.md §17, open question 1; the file's
   own comment says "Do not guess a URL here"), still open at this deploy gate. Its
   consequence: on every real run, `port-hu` — described in its own file as "the backbone
   source: most complete record" — contributes **0 events**, and the automated
   selector-drift detector (SPEC §13) can structurally never catch this, because its trigger
   (`last_count > 10 and count == 0`) needs a `last_count` that this source can never earn: it
   returns 0 every day, so `last_count` never exceeds 0. Only `bigcitylife` (a single,
   non-paginated "this weekend" page) is both enabled and functional. Running its real parser
   against its real fixture gives **9 raw events**, all inside the next two or three days,
   regardless of `horizon_days: 14`. First-run estimate: **~9 events total**, not the "hatalmas"
   (huge) first email DEPLOY.md's own troubleshooting table predicts for a healthy multi-source
   system — that guidance is itself wrong under the current source config.

3. **[MAJOR] The commit step's `git push` can lose a day's ledger and skip that day's Pages
   deploy** on a race with the browser write-UI's direct GitHub Contents-API commits (pinning/
   hiding an event, toggling a source). The workflow's `concurrency: {group: digest}` block
   only serializes *this workflow's own* runs (cron vs. `workflow_dispatch`) against each
   other — the browser UI writes directly via the Contents API, entirely outside Actions, so
   the concurrency group has no effect on that path. Reproduced empirically with two plain git
   clones simulating the two writers: the workflow's `git push` is rejected
   (`! [rejected] main -> main (fetch first)`, exit 1). Because no step in the workflow uses
   `if: always()`, that rejection fails the job and skips `configure-pages`/
   `upload-pages-artifact`/`deploy-pages` for the day — even though `site/` was already
   correctly generated. It also means today's `state/state.json` (already updated locally,
   including the events actually delivered in the earlier "Run digest" step) never reaches
   `origin`; tomorrow's checkout starts from yesterday's ledger, which combined with Finding 1
   widens the resend surface further. This is loud (GitHub's failure-workflow email fires) and
   has a documented one-line recovery (re-run the workflow, per README.md's own write-up of
   this exact race) — not silent, hence MAJOR rather than BLOCKER.

4. **[MINOR] Partial-source-failure visibility exists outside `/status.html`, but is easy to
   miss.** `source_health_line()` ("N forrásból M rendben") is rendered into the footer of
   *every* email — normal and empty-state alike (`email.html.j2:207`, `email-empty.html.j2:96`)
   — so a run where some sources failed is not exclusively a `/status.html` fact. But it is
   12px gray footer text carrying only an aggregate ratio, no source names, no reasons, and no
   visual distinction from a healthy run's footer — a recipient skimming for events would
   plausibly never notice `"5 forrásból 3 rendben"` at the bottom of the email.

**Verdict: not deployable as-is.** Both BLOCKERs are independent of each other and each is
sufficient on its own to make the first week of real operation misleading: Finding 2 means the
first email undersells what the system is supposed to do, and Finding 1 means some of what it
does show will repeat the next morning. Fix both, then re-run the exact two-consecutive-runs
check from DEPLOY.md §7 against real (not fixture) data before flipping on the schedule.

---

## Findings

### [BLOCKER] Ledger purge and the "still-running" carve-out disagree, causing real, reproducible resends

- Spec reference: SPEC.md §8.1/§8.2 ("Egyetlen dolognak muszáj átmennie: mit küldtünk már ki");
  CLAUDE.md rule 11 (pipeline stages must not silently misbehave); item 6 of this audit's own
  brief, which pre-classifies any non-zero second-run send as a BLOCKER.
- Evidence: two consecutive `_run_pipeline` calls against the real `port-hu` fixture
  (`tests/fixtures/port_hu_list.json`), the real `config.yaml`/`sources/port-hu.yaml`, a
  persistent `state.json` in a scratch tmp dir, `now` fixed at `2026-08-17 04:30
  Europe/Budapest` for both calls, and only the HTTP transport/robots-fetch/SMTP layers
  substituted (never `purge`, `was_sent`, or `effective_date`):
  ```
  RUN 1: RunSummary(source_counts={'port-hu': 20}, sent=2, dropped_by_filter=0, ...)
  state after run 1: 2 sent-ledger entries
    ledger: id='63e2c7251ebb8dae' t='befogad es kitaszit...' d=2026-08-14 s=2026-08-17
    ledger: id='87a0ae49b8f00a5c' t='høt spøt 2026 / every wednesday / a38' d=2026-05-06 s=2026-08-17
  RUN 2: RunSummary(source_counts={'port-hu': 20}, sent=2, dropped_by_filter=0, ...)
  state after run 2: 2 sent-ledger entries   # same 2 events, sent twice
  ```
  Direct proof of the mechanism — `purge()` alone, called the way `_run_pipeline` calls it at
  the top of every run, with `today=2026-08-17`:
  ```
  before purge: 2 [('63e2c7251ebb8dae', date(2026, 8, 14)), ('87a0ae49b8f00a5c', date(2026, 5, 6))]
  after purge(today=2026-08-17): 0
  ```
  Both surviving events are legitimately still active per `normalize.py`'s own "still running"
  rule (`normalize.py:202-206`, `if (end or start) < now: drop`): the Villon-est event's raw
  record has `eventStart: "2026-08-14 19:00:00"` and `end: " - 08. 21. 23:59"` (an online
  recording available through Aug 21); the HØT SPØT record has `eventStart: "2026-05-06
  17:00:00"` and `end: " - 09. 30. 22:00"` (a weekly season through Sept 30). Both correctly
  survive `normalize()`'s past-cut and `filter()`'s `beyond_horizon` cut (both checks use
  `start`/`end`, never `effective_date`), which is by design (`normalize.py:202-203`'s own
  comment: "a festival that opened in May is dropped only once it is over"). But `purge()`
  (`state.py:85-88`) drops the ledger entry once `entry.d` (`effective_date`, fixed to the
  *start* date at `record_sent` time) is before `today` — and `_run_pipeline` calls `purge()`
  before it ever calls `was_sent()` (`cli.py:180` vs. `cli.py:194`), so the entry is gone before
  it has a chance to protect anything.
- What the spec requires: SPEC §8.1 states plainly that the one thing that has to work is
  "what we already sent" — an event already delivered must never be re-offered while it is
  still being shown at all.
- What the code does: for any event whose `end` date is later than its `start`/`effective_date`
  by more than one day, the ledger's protection window is shorter than the event's actual
  active window. The protection silently expires the day after the event is first sent, and
  the event — still legitimately in the pipeline — is treated as new again, every day, until
  its `end` date finally passes.
- Impact: any multi-day exhibition, "watch until" online listing, or weeks/months-long
  recurring series (exactly the kind of listing SPEC §7.3's own regression test targets —
  "5 hónapos rekord → `is_series = True`") gets re-sent on every run after the first, for its
  entire remaining run. This is not a rare edge case: in the real, committed 20-event `port-hu`
  fixture, both of the two events that survive the horizon/past cuts hit this exact case.

### [BLOCKER] The backbone source's listing URL is an empty placeholder — the system's real first-run output is far smaller than expected, and no automated check can ever catch it

- Spec reference: SPEC.md §6.6 (forráslista), §13 (selector drift), §17 open question 1; this
  audit's item 11 ("estimate how many events the first run will produce... state the number").
- Evidence: `sources/port-hu.yaml`:
  ```yaml
  id: port-hu
  name: Port.hu
  enabled: true
  priority: 10          # the backbone source: most complete record, wins dedup merges
  ...
  listing:
    urls: []
  ```
  and `src/digest/sources/plugins/port_hu.py:69-77`:
  ```python
  def discover(self) -> Iterable[FetchTask]:
      if not self._listing_urls:
          log.warning("no_listing_urls", source_id=self.id,
                      reason="listing endpoint is still an open question (SPEC 17.1)")
      for url in self._listing_urls:
          yield FetchTask(url=url)
  ```
  With `self._listing_urls == []`, the `for` loop yields nothing — zero `FetchTask`s, so
  `_fetch_source` in `cli.py` never calls the fetcher and `events = []`, every single run.
  In `_run_sources` (`cli.py:338-355`), `count = 0` and `health.last_count` (starting at `0`
  for a fresh source) updates to `0` again — `health.last_count > 10 and count == 0`
  (`cli.py:340`) is `False` forever, so `selector_drift` can never fire for this source. No
  exception, no ERROR log — only the one `WARNING` above, once per run.
  Actual first-run source inventory (`sources/*.yaml`): of the 6 configured sources, only
  `port-hu` and `bigcitylife` are `enabled: true`; `fidelio`, `programturizmus`, `szinhazak`,
  `welovebudapest` are `enabled: false` (each with its own documented reason per Audit 4's
  review — robots.txt disallow, no discoverable listing page, fragile markup, dead domain —
  not a new finding here, cited for the full picture).
  Running the real declarative parser against the real, checked-in `bigcitylife` fixture:
  ```
  9 raw events parsed from bigcitylife fixture
   - 10 éves jubileumi koncert - NewSkool Fesztivál 2026. | 2026. augusztus 16., vasárnap 18:00
   - Hot Jazz Band // A Nyughatatlan | 2026. augusztus 16., vasárnap 19:00
   - Csaknekedkislány | 2026. augusztus 14., péntek 19:55
   - Kobuci Alter Bugi // péntek | 2026. augusztus 14., péntek 22:01
   - H2O Disney Allstars Party on Boat! | 2026. augusztus 14., péntek 23:00
   - White Girl Music Party | 2026. augusztus 15., szombat 17:00
   - Punnany Massif | 2026. augusztus 15., szombat 20:00
   - Kobuci Alter Bugi // szombat | 2026. augusztus 15., szombat 22:01
   - Vice City 80's Party | 2026. augusztus 15., szombat 23:00
  ```
  (`bigcitylife.yaml`'s own comment: the source page is `hetvegi-programok-budapesten` —
  "weekend programs" — not paginated, and not date-range-scoped by `horizon_days` at all.)
- What the spec requires: §6.6 lists `port-hu` as a real source, and CLAUDE.md's own one-line
  project description promises programs collected "több forrásból" (from multiple sources).
  §13 promises that a broken source shows up as `ERROR` and on `/status.html`.
- What the code does: the only functioning, enabled source is `bigcitylife`, and it returns a
  weekend-scoped list unrelated to the configured 14-day horizon. `port-hu` is `enabled: true`
  and structurally silent about contributing nothing.
- Impact: **first-run estimate: ~9 events, all within the next 2-3 days**, filtered/limited by
  nothing (`filters.min_score` defaults to `0` with no `PROFILE_YAML`, and `newsletter.
  per_category_limit: 5` / `total_limit: 25` are both far above 9) — so all 9 will appear,
  unfiltered, unlimited. This directly contradicts DEPLOY.md §"Ami elsőként el szokott romlani"
  item 6, which warns operators to expect "Az első reggeli email hatalmas" (the first morning's
  email will be huge) and suggests shrinking `horizon_days` to cope — that expectation, and its
  suggested remedy, do not match what the system will actually send while `port-hu`'s listing
  URL remains empty. DEPLOY.md §8 step 4 does tell the operator to check `/status.html` for a
  source stuck at 0 during the first manual run — the one human checkpoint that can catch this
  — but nothing in the automated system reinforces or repeats that check afterward.

### [MAJOR] The commit step's `git push` has no protection against the browser write-UI's direct commits, and a rejected push skips that day's Pages deploy

- Spec reference: SPEC.md §11 (GitHub Actions workflow, the `concurrency` block); this audit's
  item 4.
- Evidence: `.github/workflows/digest.yml:13-15`:
  ```yaml
  concurrency:
    group: digest
    cancel-in-progress: false
  ```
  This groups only runs of `digest.yml` itself (schedule- and `workflow_dispatch`-triggered)
  against each other. The write-UI (`src/digest/render/templates/index.html.j2:663-686`) calls
  `PUT /repos/{owner}/{repo}/contents/{path}` directly from the browser — a GitHub-side commit
  with no relationship to any Actions run or its concurrency group.
  Reproduced the race with two plain git clones of a bare "origin", standing in for the
  Actions runner's checkout and the browser UI's direct commit:
  ```
  $ git push origin main   # the Actions runner's "Commit state" step, after a browser
                            # commit has already landed on origin in between
  ! [rejected]        main -> main (fetch first)
  error: failed to push some refs to '.../origin'
  push exit code: 1
  ```
  The `git diff --staged --quiet || git commit` idiom itself was verified separately and
  behaves correctly in both directions (confirmed empirically: no staged diff → exit 0, commit
  skipped; a staged diff → commit created, exit 0) — the failure mode is specifically the
  unconditional `git push` that follows it, which has no retry/rebase and is not the compound
  command being asked about.
  `grep -n "if:" .github/workflows/digest.yml` → no matches: no step in the job uses
  `if: always()` or `continue-on-error`, so GitHub Actions' default behavior applies — a failed
  "Commit state" step aborts the job before `configure-pages`/`upload-pages-artifact`/
  `deploy-pages` ever run.
- What the spec requires: SPEC §11 lists the concurrency block as the race-prevention
  mechanism, without qualifying which writers it does and does not cover.
- What the code does: prevents exactly one race (two `digest.yml` runs), not the one README.md
  itself already documents as the realistic risk (browser write vs. nightly run).
- Impact: on this race, (1) the job fails, which is loud — GitHub's default workflow-failure
  notification fires, matching README.md's own claim ("Ez hangos hiba... nem csendes
  adatvesztés"); (2) that day's Pages deploy is skipped as a side effect, even though `site/`
  was already correctly generated on the runner; (3) that day's `state/state.json` update
  (covering events already delivered by the earlier "Run digest" step) never reaches `origin`,
  so tomorrow's checkout starts from yesterday's ledger — compounding the BLOCKER above by
  widening its resend window by one more day. The prescribed recovery (re-run the workflow) is
  documented and effective, which is why this is MAJOR and not BLOCKER — but the Pages-skip and
  ledger-staleness side effects are not called out anywhere the operator would see them before
  they happen.

### [MINOR] Partial-failure visibility is real but low-salience

- Spec reference: SPEC.md §13 (per-source failures should be visible); this audit's item 10.
- Evidence: `src/digest/render/common.py`'s `source_health_line()` ("N forrásból M rendben")
  is included in both `email.html.j2:207` and `email-empty.html.j2:96` at `font-size:12px;
  color:#9397ab` (light gray), identically formatted whether every source is healthy or not.
- What the spec requires: a broken source should be noticeable, not just technically present
  somewhere.
- What the code does: the ratio is genuinely in the email every day (not exclusively on
  `/status.html`, contrary to what item 10's own phrasing might suggest), but it carries no
  per-source names, no failure reasons, and no visual distinction from a fully healthy run.
- Impact: an operator who only reads the digest for the events, not the footer, would not
  reliably notice that, say, 2 of 2 sources failed that day and the "9 new events" they're
  looking at is stale carry-over rather than a fresh scan.

---

## Checked and conformant

1. **Item 1 — clean install.** CONFORMANT. Fresh `uv`-managed Python 3.12.14 virtualenv
   entirely under `/tmp`, `pip install -e .` (both via `uv pip` and stock `pip`): 25 packages
   resolved, zero warnings beyond pip's own "a newer pip is available" notice, zero version
   conflicts. Every top-level dependency pulled in is either explicitly listed in SPEC.md §15
   (`httpx`, `selectolax`, `pydantic`, `Jinja2`, `rapidfuzz`, `PyYAML`, `typer`, `structlog`)
   or a transitive dependency of one of those (e.g. `anyio`/`certifi`/`h11`/`httpcore`/`idna`
   under `httpx`; `rich`/`shellingham`/`annotated-doc` under `typer`) — nothing extraneous was
   declared by hand. `digest --help` exits 0 and lists all four commands (`run`, `fetch`,
   `categorize`, `explain`).
2. **Item 2 — dry run end to end.** CONFORMANT. `digest run --dry --source port-hu --fixture
   tests/fixtures/port_hu_list.json --out ...`: exit 0, wrote a 14 KB HTML file, no `state/`
   directory was created (confirmed both before and after — the repo has none to begin with),
   `git status --short` unchanged across the run. **Elapsed: 0.229s wall** (`time` around the
   whole process, fixtures only, no network) — comfortably under the audit's 60s concern
   threshold; real network latency across 2 live sources will add seconds, not minutes, based
   on this baseline, though this was not itself measured against a live network.
3. **Item 3 — workflow static correctness.** CONFORMANT, byte-for-byte. `diff` between
   `.github/workflows/digest.yml` and the fenced code block in SPEC.md §11 shows a difference
   only in the markdown fence markers — the YAML content is identical. Checked individually
   per the item's own five sub-points: **action versions** (`checkout@v4`, `setup-python@v5`,
   `configure-pages@v5`, `upload-pages-artifact@v3`, `deploy-pages@v4`) match spec exactly;
   **permissions block** (`contents: write`, `pages: write`, `id-token: write`) matches;
   **concurrency block** (`group: digest`, `cancel-in-progress: false`) matches (see the MAJOR
   finding above for what this block does and does not cover, which is a behavioral gap in the
   *design*, not a deviation from *this* spec text); **Pages environment block**
   (`environment: {name: github-pages, url: ${{ steps.deploy.outputs.page_url }}}`) matches;
   **commit-then-deploy step order** matches — "Commit state" runs before
   `configure-pages`/`upload-pages-artifact`/`deploy-pages`, which is sound because the Pages
   artifact is built from the runner's local `site/` directory, not from anything requiring the
   git push to have succeeded first (the consequence of the push failing anyway is covered in
   the MAJOR finding, not a spec deviation). `python -c "import yaml; yaml.safe_load(...)"`
   parses the file without error.
4. **Item 7 — state recovery.** CONFORMANT, verified at both the `load_state()` level and the
   full `_run_pipeline` level, never against the repo's own `state/state.json` (which does not
   exist in this checkout). A scratch file truncated mid-JSON —
   `{"version": 1, "last_run": "2026-08-16T04:30:00+02:00", "sent": [{"id": "abc123", "t": "s`
   — loaded via `load_state()`:
   ```
   load_state() returned: sent=0 run_log=0
   state_corrupt log entries: 1 -> [{'error': 'Unterminated string starting at: line 1 column 88 (char 87)', 'event': 'state_corrupt', 'log_level': 'error'}]
   ```
   and then fed straight into a real `_run_pipeline` call (real config, real `port-hu` fixture,
   mocked transport/SMTP only): the run completed normally without raising —
   `RunSummary(source_counts={'port-hu': 20}, sent=2, ...)` — and rewrote `state.json` with a
   fresh, valid ledger (2 entries) afterward. No crash at either the module or the pipeline
   level; the file was restored to its original truncated form for no reason, since it lived
   only in a `tempfile.mkdtemp()` directory the whole time.
5. **Item 8 — timezone and DST.** CONFORMANT. The cron `30 4 * * *` is UTC. In 2026, EU DST
   runs from 2026-03-29 to 2026-10-25; outside that window Budapest is UTC+1 (CET), inside it
   UTC+2 (CEST). So the run lands at **06:30 Europe/Budapest during CEST** (late March–late
   October, most of the year, matching SPEC's own "~06:30 ... nyáron" comment) and **05:30
   Europe/Budapest during CET** (late October–late March) — a real, accepted one-hour drift
   the project already documents (SPEC §11, DEPLOY.md §9) rather than compensates for.
   `grep -rn "timedelta(hours=1)\|+01:00\|+02:00" src/` → **no matches**. `config.py:25`
   defaults `schedule.timezone` to `"Europe/Budapest"`, and `cli.py`/every pipeline stage reads
   it through `ZoneInfo(config.schedule.timezone)`, which resolves DST from the IANA tzdata
   database rather than a fixed offset anywhere in the code.
6. **Item 9 — README completeness.** CONFORMANT, exact match.
   `grep -o 'secrets\.[A-Z_]*' .github/workflows/digest.yml | sort -u` →
   `GEMINI_API_KEY, PROFILE_YAML, SMTP_HOST, SMTP_PASSWORD, SMTP_USER`. README.md's "GitHub
   Secrets" table lists exactly these five, by the same names, each with a description of what
   it's for and whether it's required. (DEPLOY.md §4 independently repeats the identical
   `grep` command as its own pre-flight check — the two documents agree.)
7. **Item 5 — permissions (informational, not a code check).** The workflow correctly declares
   `permissions: {contents: write, pages: write, id-token: write}` — nothing to fix in the file.
   Noting for the report as the item asks: this declaration is a ceiling, not a guarantee. If
   the repository's own Settings → Actions → General → "Workflow permissions" is left at its
   "Read repository contents permission" default, the `git push` in "Commit state" fails with a
   403 regardless of what the workflow file declares. **The operator must set Settings →
   Actions → General → Workflow permissions → "Read and write permissions" → Save** before the
   first real run. DEPLOY.md §5 already documents this exact requirement in the same words
   ("nem tudja túllépni a repo-szintű beállítást"); this could not be verified against the
   actual GitHub repository settings from this sandboxed environment (no GitHub access), so it
   is restated here rather than independently confirmed.

## Unknown

- **Whether the repository's actual "Workflow permissions" setting is already correct.**
  Outside this environment's reach (no GitHub API/network access); see item 7 above under
  Checked and conformant for what to set and why.
- **Who receives GitHub's scheduled-workflow-failure notification email in practice.** Item 10
  asks for "the actual mechanism" — that mechanism is GitHub's default workflow-failure email,
  confirmed by README.md's own description of the browser-write race ("a GitHub emailt küld a
  sikertelen workflow-ról"). Exactly which account(s) receive it for a `schedule`-triggered (as
  opposed to manually dispatched) run was not independently verified against GitHub's current
  documentation or behavior in this session, and is left here rather than asserted.
- **Real-network timing.** Item 2's 0.229s figure is fixtures-only, one source, no network. The
  real first live run touches 2 enabled sources (once Finding 2 is fixed, more) with real
  latency, retry/backoff (`fetch.max_retries: 3`, `backoff_base_seconds: 2`) and a 1.5s
  default per-source rate limit; this was not measured and could plausibly land anywhere from a
  few seconds to a couple of minutes depending on how many pages each source paginates through.
- **Whether `port-hu`'s real listing endpoint (once filled in) would push the first-run event
  count meaningfully higher than the ~9 estimated here**, and whether that volume would then
  actually engage `per_category_limit`/`total_limit`. Not knowable without the real URL SPEC
  §17 question 1 is still waiting on.

## Go / No-go

**No-go.** Minimal blocker list that must clear before the schedule is turned on:

1. Fix the ledger/purge vs. still-running-event disagreement (Finding 1) — either stop purging
   a `sent` entry while its event is still active (e.g. key `purge()` off the event's actual
   `end` date when present, not `effective_date`), or stop granting still-running events an
   exemption from the past-cut without also keeping their ledger protection alive for the same
   window. Re-run DEPLOY.md §7's own two-consecutive-runs check afterward and confirm zero
   resends.
2. Resolve SPEC.md §17 open question 1 and fill in `sources/port-hu.yaml`'s `listing.urls`
   (Finding 2), or explicitly demote `port-hu` to `enabled: false` until it is — either way, do
   not ship with a source labeled "the backbone source" silently contributing nothing.
