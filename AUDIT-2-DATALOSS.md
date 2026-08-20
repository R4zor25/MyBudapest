# Audit 2 — Silent data loss

Scope: SPEC.md §4.1, §7.2, §7.4, §7.6, §8, §13; every file under `src/digest/`. Report only —
no source file was modified. Baseline: `git rev-parse --short HEAD` = `3908efc` ("fix(audit):
resolve blocker findings from audit 1"), working tree as found. Test baseline:
`./.venv/bin/python -m pytest -q` → **320 passed in 1.12s**.

## Summary

**1 BLOCKER, 4 MAJOR, 2 MINOR.** One of the four MAJORs is Audit 1's MAJOR-1 (the collapsed-
festival-row ledger duplication), unchanged by the 3908efc fix and reproduced independently
here under this audit's own lens, over three fresh simulated consecutive runs. The other three
MAJORs and the BLOCKER are new to this audit.

The standout finding, and the reason this system cannot deploy as-is: when delivery silently
no-ops — a missing `recipient_email` (e.g. `PROFILE_YAML` unset or misconfigured), or every
`delivery:` target disabled/unimplemented — `_run_pipeline` still calls `record_sent()`
unconditionally and writes the ledger, marking every computed event as "sent" even though
**nothing was ever delivered to anyone**. The run exits 0, no exception is raised, nothing
appears on `/status.html`, and the ledger is now permanently poisoned against every one of that
day's events — fixing the misconfiguration afterwards does not recover them. Notably, Audit 1
graded `smtp.py`'s silent `smtp_skipped`-and-return as **conformant** to §5.3 ("a missing
profile must not fail the run") — correctly, in isolation. This finding is not a contradiction
of that: it is what happens when that same graceful, by-design skip meets the unconditional
`record_sent()` call downstream. Neither half is wrong on its own; the seam between them is.
Demonstrated directly against `_run_pipeline`, not inferred from reading.

The four MAJORs, roughly in order of how likely they are to occur in production: (1) `was_sent`
(§8.2)'s fuzzy branch checks title-ratio and date only — no venue, matching SPEC's own literal
two-condition text — and `token_set_ratio` treats any title that is a token superset/subset of
a previously-sent title as a ~100% match, so a genuinely different event at a genuinely
different venue on the same `effective_date` can be silently and permanently suppressed by
unrelated title overlap; demonstrated with entirely ordinary titles, no adversarial contrivance
needed. (2) SPEC §13's explicit requirement that selector drift "megjelenik a `/status.html`-en"
is not implemented — `drifted` is computed, logged once via `structlog` ERROR, and then
discarded, never reaching `SourceHealth`, `State`, or any template; compounded by the detection
threshold itself never being reachable for a source that is broken from the day it's added
(`last_count` starts at `0`, so the `>10 → 0` drift condition can never fire until a source has
first proven itself for at least one good day). (3) The collapsed-festival-row ledger bug from
Audit 1 (MAJOR-1). (4) `filter()`'s five exclusion reasons are logged per-event but never
aggregated — the run summary carries one undifferentiated `dropped_by_filter` count, so a
misconfigured `blocked_keywords` that wrongly excludes everything looks identical to a quiet
day. Two smaller, narrower risks are logged as MINOR: a real (if rarer, and requiring an exact
rather than fuzzy match) `make_event_id` collision at the spec-accepted 2-token
parenthesised-suffix boundary, and the `git push` step in the workflow having no retry, so a
rejected push after a fully successful `digest run` causes tomorrow's resend of today's events
(a duplicate, not a loss). **The three ledger-poisoning categories the task explicitly asked me
to test on `make_event_id`** — shared long prefixes, splits after the last separator, and
3+-token parenthesised suffixes — **produced zero collisions**, confirming §4.1's own design
holds exactly where it claims to.

**Verdict: not deployable as-is.** The BLOCKER is silent, total, and permanent data loss
triggered by the single most likely real-world misconfiguration this project has (a bad or
missing `PROFILE_YAML` secret), and it defeats `send_when_empty`'s entire purpose as "the
system's only monitoring" — because the ledger write does not depend on whether the heartbeat
(or any email) actually reached anyone. It must be fixed before the first real deploy. The
`was_sent` MAJOR should be fixed alongside it or very shortly after — it is the widest silent-
suppression surface found here that can trigger under entirely normal operation, no
misconfiguration required. The other MAJORs should be fixed before the operator can trust
`/status.html` or the run summary for anything beyond "the process didn't crash."

---

## Findings

### [BLOCKER] `record_sent` fires even when delivery never happened — a bad profile or a disabled delivery target permanently erases every event of the day

- **Spec reference:** SPEC.md §1 (success criterion — nothing already-seen may reappear, which
  implicitly requires the inverse: nothing not-yet-seen may be marked seen), §8.1 ("Egyetlen
  dolognak muszáj átmennie: mit küldtünk már ki" — the ledger's only reason to exist is to track
  what was *actually sent*), §10 (`send_when_empty` is described as "a rendszer teljes
  monitoringja" — the system's entire monitoring), §5.3 (a missing `PROFILE_YAML` "nem hal el" —
  the run must not die, which the code satisfies, but at the cost of this bug)
- **Evidence:**
  - `src/digest/cli.py:189-192`:
    ```python
    if rendered.sent_events or config.newsletter.send_when_empty:
        _deliver(rendered, config)

    state = record_sent(state, rendered.sent_events, sent_on=today)
    ```
    `record_sent` is called unconditionally after `_deliver`, with no return value or exception
    from `_deliver` ever inspected.
  - `src/digest/cli.py:229-237`:
    ```python
    def _deliver(rendered: RenderedEmail, config: Config) -> None:
        for target in config.delivery:
            if not target.enabled:
                continue
            deliverer_cls = _DELIVERERS.get(target.type)
            if deliverer_cls is None:
                log.warning("deliverer_not_implemented", type=target.type)
                continue
            deliverer_cls().send(rendered.subject, rendered.html, rendered.text, config)
    ```
    If `config.delivery` is empty, or every target is `enabled: false`, or every configured
    `type` is unimplemented, this loop does nothing and returns `None` — no exception, no
    return signal of "0 successful deliveries."
  - `src/digest/delivery/smtp.py:25-28`:
    ```python
    def send(self, subject: str, html: str, text: str, config: Config) -> None:
        if not config.recipient_email:
            log.warning("smtp_skipped", reason="no recipient_email configured")
            return
    ```
    A missing `recipient_email` — the exact state §5.3 says must not crash the run, and which
    Audit 1 correctly graded conformant on that narrow question — logs one `WARNING` and returns
    silently. Compare: the *other* SMTP misconfiguration (`SMTP_HOST`/`USER`/`PASSWORD` env
    vars unset) does `raise DeliveryError(...)` (`smtp.py:34`) and correctly aborts the run
    before `record_sent` is reached — only the recipient-address case is silent. This finding is
    exactly the seam between two individually-correct decisions: "don't crash on a missing
    profile" (§5.3, correct) and "always record what render computed as sent" (`cli.py:192`,
    wrong when nothing was actually delivered).
  - Reproduced directly against `_run_pipeline` (not inferred), three independent
    misconfigurations, one real event each, with `smtplib.SMTP` replaced by a stub that raises
    `AssertionError` if ever constructed — confirming no network connection was even attempted:
    ```
    scenario: recipient_email is None (e.g. PROFILE_YAML missing/misconfigured)
      summary.sent (email rows computed): 1
      ledger entries recorded as sent: 1  ids=['f89951ca83bf6e45']
      run exited without raising: True

    scenario: all delivery targets disabled
      summary.sent: 1   ledger entries recorded: 1   run exited without raising: True

    scenario: delivery list empty
      summary.sent: 1   ledger entries recorded: 1   run exited without raising: True
    ```
- **What the spec requires:** The ledger exists solely to record what was sent (§8.1). §10's
  `send_when_empty` is the system's *only* monitoring signal, which only works if "the email
  went out" and "the ledger says it went out" cannot diverge.
- **What the code does:** Treats "the delivery loop finished without raising" as equivalent to
  "the email was delivered." Those are different things whenever every target is disabled,
  unimplemented, or (the realistic case) `recipient_email` is absent because the private
  `PROFILE_YAML` secret was never set, was deleted, expired, or has a typo that config.py's
  §5.3 "graceful fallback to neutral defaults" absorbs into `recipient_email=None` without
  raising.
- **Impact:** Total, permanent, silent loss of every event computed on any affected day. The
  run is green (exit 0), the `run_summary` log line looks completely normal (`sent=N`),
  `/status.html` shows nothing (source health is unaffected — this is a delivery problem, not a
  source problem), and `state.json` gets committed to the repo with those event ids now marked
  sent. When the operator eventually notices they haven't received an email in days and fixes
  `PROFILE_YAML`, the fix does **not** un-poison the ledger — those specific events are gone
  forever; only genuinely new events will appear afterward. Would the operator notice? Not from
  anything the system surfaces. The only way to notice is external: "I haven't gotten an email
  in a while," days after the loss already happened and after it is already unrecoverable.

---

### [MAJOR] `was_sent`'s fuzzy branch has no venue check — a same-day title overlap silently, permanently suppresses a genuinely different event

- **Spec reference:** SPEC.md §8.2 (`was_sent`: "Exact id egyezés VAGY (azonos `d` ÉS
  `token_set_ratio(t, title_norm) >= 92`)" — literally two conditions, no venue term), contrast
  with §4.1 (`make_event_id`'s basis includes `normalize_venue(venue)`) and §7.2 (the dedup
  fuzzy level's third mandatory AND-condition is exactly a venue-ratio check, "VAGY az egyik
  venue `None`")
- **Evidence:**
  - `src/digest/state.py:91-102`:
    ```python
    def was_sent(state: State, event: Event) -> bool:
        title_norm = normalize_title(event.title)
        for entry in state.sent:
            if entry.id == event.id:
                return True
            if entry.d == event.effective_date and token_set_ratio(entry.t, title_norm) >= (
                _FUZZY_TITLE_RATIO
            ):
                return True
        return False
    ```
    No venue term anywhere in this function or in `SentEntry` (`state.py:19-28`, fields are
    only `id`, `t`, `d`, `s`) — this is a faithful, literal implementation of §8.2 as written.
    The spec itself never asks for a venue check here, unlike the two sibling comparisons
    (§4.1, §7.2) that both fold venue in specifically to prevent this class of false match.
  - `token_set_ratio` is forgiving of a strict subset/superset relationship by construction —
    confirmed directly:
    ```
    'Sub Focus' vs 'Sub Focus Live':              token_set_ratio = 100.0  (>=92: True)
    'Sub Focus' vs 'Sub Focus Live Set':          token_set_ratio = 100.0  (>=92: True)
    'Anna Kovács' vs 'Anna Kovács Trio':          token_set_ratio = 100.0  (>=92: True)
    'Koncert' vs 'Nagy Koncert Este':             token_set_ratio = 100.0  (>=92: True)
    'Sziget' vs 'Sziget Warmup Party':            token_set_ratio = 100.0  (>=92: True)
    ```
  - Reproduced end to end against real `_run_pipeline` runs, two unrelated events, no
    adversarial title construction — just ordinary short-vs-verbose naming, which real sources
    routinely disagree on:
    ```
    run 1: sent 'Sub Focus' @ A38 Hajo -- ledger=[('042a922e2091ada5', 'sub focus')]

    run 2: genuinely different event 'Sub Focus Live at Akvarium' @ Akvarium Klub, same date
      summary.sent=0
      filtered log entries: [{'reason': 'already_sent', 'event_id': '5d0aae37910ee8eb',
                               'title': 'Sub Focus Live at Akvarium', ...}]
      email delivered this run: True   (i.e. the heartbeat fired — with 0 new events)
    ```
    Two different ids (`make_event_id` correctly disagreed, since venue and title both differ),
    different venues, different times — and the second is filtered as `already_sent` purely
    because its title's tokens are a superset of the first's, on the same `effective_date`.
- **What the spec requires:** §8.2's stated purpose (§4.1: "Ezt a dedup fuzzy szintje fogja el a
  futáson belül, a ledger viszont csak az id-t tárolja. Ezért a ledger a `(id, start_date,
  title_norm)` hármast tárolja, és a `sent_before()` ellenőrzés fuzzy is") is narrowly aimed at
  *one* source rewriting *the same event's* title between runs — not at distinguishing two
  different events that happen to share words on the same day. The spec's own design elsewhere
  (§4.1, §7.2) treats venue as the necessary safety net against exactly this kind of coincidence
  whenever fuzzy title matching is involved; §8.2 is the one place that safety net is missing.
- **What the code does:** Exactly what §8.2 specifies, letter for letter — this is not a code
  defect, and per Audit 1's own finding #6, the fuzzy branch is correctly live and reachable in
  production. The gap is in the spec's comparison, which this code faithfully implements.
- **Impact:** This is the widest silent-suppression surface found in this audit that requires no
  misconfiguration and no adversarial title construction — only two different events, same day,
  with any meaningful token overlap in their titles (a shared artist name fragment, a shared
  generic word like "koncert" or "kiállítás" appearing as a substring of a longer title from a
  different source). Given §6.6 M5's dozen-plus sources with inconsistent titling conventions
  (short act names from one source, full descriptive titles from another) and a 14-day horizon
  with hundreds of daily candidates, this is materially more likely to actually occur than the
  `make_event_id` 2-token-suffix collision below, and — unlike that one — needs no exact
  parenthesised-suffix pattern, just token overlap. The suppressed event produces no distinct
  signal: it logs exactly the same `filtered`/`reason=already_sent` line a legitimate duplicate
  would, so even an operator grepping logs cannot tell this case apart from a correct exclusion
  without independently knowing the two events were unrelated.

---

### [MAJOR] Selector drift is logged once and discarded — never reaches `/status.html`, and can never fire at all for a source broken from day one

- **Spec reference:** SPEC.md §13: "ha egy forrás korábban >10 eseményt adott és most 0-t, az
  nem 'nincs program', hanem törött parser → `ERROR` szint **és megjelenik a `/status.html`-en**."
  ("ERROR level *and* appears on status.html" — both, not either.)
- **Evidence — gap 1, never reaches status.html:**
  - `src/digest/cli.py:293-295`:
    ```python
    if health.last_count > _DRIFT_MIN_PREVIOUS_COUNT and count == 0:
        log.error("selector_drift", source_id=source.id, previous_count=health.last_count)
        drifted.append(source.id)
    ```
    This is the only place `drifted`/`selector_drift` is produced.
  - `grep -rn "drift" src/digest/render/ src/digest/state.py`: **zero hits.** `drifted` is
    threaded through `_run_sources` → `RunSummary.drifted` → one `log.info("run_summary", ...,
    drifted=summary.drifted, ...)` call (`cli.py:213`, `:223`) and nowhere else.
  - `SourceHealth` (`state.py:31-38`) has fields for `consecutive_failures`, `last_ok`,
    `last_count`, `etag`, `disabled_until` — no drift flag, no drift timestamp, no drift count.
  - `_status_rows` (`render/web.py:166-176`) builds exactly those five fields per source for
    `status.html.j2` — nothing else is available to render, because nothing else was persisted.
  - Immediately after a drift is detected, the same run's success path still runs
    (`cli.py:297-308`) and sets `last_count: count` (i.e. `0`) on that source's health — so the
    one piece of state that would let a human retroactively notice "this used to be >10" is
    overwritten within the same run that detected the drop.
- **Evidence — gap 2, unreachable for a source broken from the start:**
  - `_DISABLE_AFTER_FAILURES`/`_DRIFT_MIN_PREVIOUS_COUNT = 10` (`cli.py:65-67`); `SourceHealth`
    defaults `last_count: int = 0` (`state.py:36`). The drift condition is
    `health.last_count > 10 and count == 0`. A newly added source (e.g. via SPEC §16 M5's stated
    goal, "forrás hozzáadása kód nélkül" — adding a source with no code, exactly the moment a
    typo'd CSS selector or JSON path is most likely) starts with `last_count == 0`. If its
    `item_selector`/`json_path`/field mapping is wrong from day one, every run yields 0 events,
    `0 > 10` is always `False`, and `selector_drift` **can never fire for this source, ever** —
    regardless of how many days pass.
  - Compounding this: if the wrong element is the *listing-level* `item_selector`/`json_path`
    itself (not an individual field), `DeclarativeSource._extract_items`
    (`declarative.py:90-97`) returns `[]` and **no log line is emitted at all** — contrast
    `_parse_item`'s per-record `declarative_field_missing` WARNING (`:110`), which only fires
    once a per-item selector has already matched something to iterate over.
- **What the spec requires:** A source that flips from >10 events to 0 must be visibly flagged
  on the durable, browsable status page.
- **What the code does:** Produces exactly one `ERROR`-level structlog line, inside that day's
  GitHub Actions run log, and nothing durable — and even that one line is unreachable for the
  specific case (a newly misconfigured source) that §16 M5 identifies as the primary risk this
  mechanism exists to catch.
- **Impact:** A source can be silently, completely dead — either from the day it's added, or
  after a site redesign breaks a previously-working selector — and the entire mechanism SPEC
  §13 designed to catch it either never fires (day-one case) or fires exactly once into a log
  stream nobody proactively reads on a project whose whole premise is "you interact with this by
  reading your inbox in the morning" (regression case). `/status.html`, the medium SPEC says
  must show it, never will.

---

### [MAJOR] Collapsed festival rows are still resent and re-recorded every run — Audit 1's MAJOR-1 is unchanged by the 3908efc fix, reproduced fresh here

- **Spec reference:** SPEC.md §7.4 (grouping mandatory), §7.6 (filter excludes "a ledger szerint
  már kiküldött"), §8.2, §1 (success criterion)
- **Evidence:** `git show 3908efc --stat` touches `SPEC.md`, `render/web.py`, `state.py`,
  `state/`, and one test file — `src/digest/pipeline/group.py` and `src/digest/cli.py`'s
  `sent_ids`/`group` ordering are byte-for-byte unchanged since Audit 1. The mechanism is the
  same one Audit 1 documented: `sent_ids` (from `was_sent`) is computed on **pre-group**
  individual event ids (`cli.py:164`) and consumed by `filter`/`score`
  (`cli.py:172`, `:176`); `group()` then runs *after* both and synthesizes a brand-new,
  deterministic id from `(venue_name, effective_date, primary_category)`
  (`group.py:105-112`) that was never checked against the ledger, because nothing downstream of
  `group()` re-filters by it. Reproduced fresh, independently, over three simulated consecutive
  daily runs of the same 17-member festival fixture (`min_group_size` default 4, distinct
  artist-name titles so dedup does not collapse them first):
  ```
  run 1: summary.sent=1  ledger size after=1  ledger ids=['d16cdc539e7a0579']
  run 2: summary.sent=1  ledger size after=2  ledger ids=['d16cdc539e7a0579', 'd16cdc539e7a0579']
  run 3: summary.sent=1  ledger size after=3  ledger ids=['d16cdc539e7a0579', 'd16cdc539e7a0579', 'd16cdc539e7a0579']
  ```
- **What the spec requires:** §1 — the morning email must not contain anything already seen.
- **What the code does, and which of the task's two hypothetical answers applies:** Only the
  collapsed row is recorded in the ledger — the 17 individual member events are never recorded
  under their own ids. The task's item 3 offers two possible consequences depending on which
  design is chosen: "if only the row is recorded, tomorrow the same 17 sets are 'new' again."
  That is exactly what happens, verified above: the group's own id is deterministic and *would*
  be recognized by `was_sent` if it were ever checked, but the check that would catch it
  (`filter`'s `already_sent`) runs upstream of the stage that produces that id. This is not
  literal event-content loss (nothing vanishes; the row's content is well-formed each time) —
  it is a duplicate resend, the mirror-image failure mode, but it directly violates §1's
  success criterion and it is the case §7.4 was explicitly written to prevent.
- **Impact:** Every day of a multi-day festival, the collapsed row reappears in the digest as if
  new, and the ledger accumulates one duplicate entry per day until `purge()` drops the
  festival's own past-dated entries. Not silent in the sense of vanishing, but silent in the
  sense that nothing about the system flags "you are seeing this again" — the operator has no
  way to distinguish today's row from yesterday's without noticing the description text
  themselves.

---

### [MAJOR] Filter exclusions are logged per-event but never aggregated — the run summary cannot answer "how many were blocked, and why"

- **Spec reference:** SPEC.md §7.6 (six exclusion reasons), §13 ("Strukturált logolás …, futás
  végén összefoglaló: forrásonként darabszám, dedup merge-ek, **kiszűrtek**, időtartam")
- **A narrower reading, addressed up front:** on the most literal parse of §13's "kiszűrtek," a
  single combined count (which the code does produce, via `RunSummary.dropped_by_filter`)
  arguably satisfies the letter of that one word. This finding does not rest on that word alone —
  it rests on this task's item 6, which states the requirement explicitly ("each exclusion to be
  logged with its reason **and counted**") and gives the concrete failure mode: without a
  per-reason breakdown, a filter that wrongly excludes everything is indistinguishable from a
  quiet day using anything the system surfaces by default. That consequence is demonstrated
  below, independent of how strictly §13's one word is read.
- **Evidence:**
  - `src/digest/pipeline/filter.py:36-42`: every excluded event gets its own
    `log.info("filtered", reason=reason, event_id=event.id, title=event.title)` line — six
    distinct reason strings possible (`hidden_by_override`, `beyond_horizon`,
    `category_not_allowed`, `price_too_high`, `blocked_keyword`, `already_sent`, per
    `_exclusion_reason`). Nothing counts occurrences by reason anywhere in this file.
  - `src/digest/cli.py:70-78` (`RunSummary`) and `:171-177`: `dropped_by_filter` is a single
    `int` — `before_filter - len(events)` — with no breakdown.
  - `grep -rn "reason" src/digest/` → the only place a *reason* is aggregated into a count is
    `src/digest/pipeline/score.py:46-48`
    (`dropped = len(scored) - len(survivors); log.info("dropped_below_min_score", count=dropped, ...)`)
    — but that stage has exactly one possible reason (`min_score`), so a single count happens to
    be a complete breakdown there. `filter.py`'s six reasons are compressed into per-event logs
    plus one undifferentiated total in `RunSummary`.
  - Confirmed by test survey: `grep -rn "reason" tests/test_filter.py` shows tests asserting the
    *per-event* log line's `reason` field for one event at a time (`test_filter.py:61`, `:143`)
    — no test anywhere asserts a per-reason count.
  - `/status.html` (`_status_rows`, `web.py:166-176`) carries no filter information at all —
    only source health.
- **What the spec requires:** §13's own list of what the end-of-run summary must contain
  includes "kiszűrtek" alongside per-source counts and dedup merges — both of which *do* get
  their own structured fields (`source_counts`, `merged`). Filtered-out gets only the single
  combined number.
- **What the code does:** Produces complete per-event provenance (grep-able, if you already
  suspect a problem and know to look) but no computed aggregate an operator would actually see
  without writing a log-parsing script.
- **Impact:** If `blocked_keywords` in the private profile is misconfigured to match something
  common (a typo, an overly broad phrase), the digest goes quiet or thin, `dropped_by_filter` is
  a big number, and the run summary gives no way to tell "everything was blocked by keyword X"
  apart from "categories/price/horizon legitimately excluded most of today's events" or simply
  "it was a quiet day." The operator would have to go pull the raw structured logs for that run
  and grep+tally `reason=` values themselves.

---

### [MINOR] `make_event_id` has a real, if narrower, collision surface at the spec-accepted 2-token parenthesised-suffix boundary

- **Spec reference:** SPEC.md §4.1 (`normalize_title`'s 2-token parenthesised-suffix cut,
  justified by the `(HU)`/`(UK)` example; the spec's own text acknowledges "az alulvágás ára egy
  ismétlődő email, a túlvágásé egy végleg elveszett esemény — a kettő nem egyenrangú")
- **Evidence:** Task item 1's three prescribed adversarial categories were run against
  `make_event_id` and produced **zero collisions**:
  ```
  [long-shared-prefix]                 distinct  ("Sziget Fesztivál 2026 — Sub Focus koncert" / "...Chase & Status koncert")
  [after-last-separator-pipe]          distinct  ("Sub Focus | A38 Hajó" / "Chase & Status | A38 Hajó")
  [before-first-separator, mirror]     distinct  ("A38 Hajó | Koncert X" / "A38 Hajó | Koncert Y")
  [parenthesised-suffix-3-tokens]      distinct  ("Sub Focus (Live DJ Set Version)" / "...(Extended Encore Edition)")
  [parenthesised-suffix-4-tokens]      distinct
  ```
  §4.1's design holds cleanly on all of them. But probing one boundary condition the task did
  not explicitly list — a realistic same-artist, same-venue, same-date double-header
  distinguished only by a **2-token** suffix (within the spec's own accepted cut length) —
  does collide:
  ```
  raw_a='Sub Focus (Early Show)'          raw_b='Sub Focus (Late Show)'
  norm_a='sub focus'  norm_b='sub focus'  ids EQUAL: 51c533deb3edacbe

  raw_a='Anna Kovács (Acoustic Set)'      raw_b='Anna Kovács (Electric Set)'
  ids EQUAL: 3f79d3767d66708d
  ```
- **What the spec requires:** §4.1 deliberately accepts this class of risk — it caps the cut at
  2 tokens specifically to keep `(HU)`/`(UK)`-style noise out while (it argues) not eating real
  title content, and states outright that over-cutting is the worse of the two failure modes it
  is trading off.
- **What the code does:** Exactly what the spec specifies — this is not a code defect, the
  threshold is a token *count*, not a semantic judgement of "is this noise," so it cannot tell
  "(HU)" apart from "(Early Show)."
- **Impact:** Narrower and harder to trigger than the `was_sent` MAJOR above — this needs an
  *exact* id collision (same normalized title, same date, same venue, differing only inside a
  matching-length parenthesis), not mere token overlap — so it is graded MINOR rather than
  MAJOR. When it does fire, it is permanent and silent: `make_event_id` collision → dedup's
  exact-id level 0 merges the two before either reaches the ledger, so only one of the two shows
  ever gets sent, ever. This is a known, already-argued, narrow residual risk (SPEC's own text
  names the tradeoff) rather than an unexamined one, which is the other reason it sits at MINOR.

---

### [MINOR] `git push` in the "Commit state" workflow step has no retry — a rejected push discards a fully successful run's ledger update

- **Spec reference:** SPEC.md §8 ("Egyetlen dolognak muszáj átmennie: mit küldtünk már ki"), §11
  (workflow YAML, `concurrency: {group: digest, cancel-in-progress: false}`)
- **Evidence:** `.github/workflows/digest.yml`:
  ```yaml
  - name: Commit state
    run: |
      git config user.name  "digest-bot"
      git config user.email "digest-bot@users.noreply.github.com"
      git add state/state.json site/
      git diff --staged --quiet || git commit -m "chore: digest run $(date -u +%F)"
      git push
  ```
  No `git pull --rebase` / retry loop before `git push`. Within-process, `_run_pipeline`'s
  ordering (`cli.py:189-205`) is send → `record_sent` → `save_state` — i.e. send-then-save, and
  correctly so for the *loud*-failure case: `SmtpDeliverer.send` raising `DeliveryError`
  (missing SMTP env vars) propagates before `record_sent`/`save_state` ever run, so a failed
  send never gets marked sent. But once `digest run` exits 0 (email sent, `state.json` written
  to the local checkout), the workflow's separate `git push` is a second point of failure this
  audit's item 8 explicitly asks about, and it is unprotected.
- **What the spec requires:** The ledger surviving the run is the one thing §8 says must not be
  lost.
- **What the code does:** If `git push` is rejected (a concurrent manual `workflow_dispatch` run
  landed first, a branch-protection rule, a transient network error), the workflow step fails,
  the job goes red, and the *local* `state.json` update — which correctly recorded today's sent
  events — never reaches the repository. `concurrency: {group: digest, cancel-in-progress:
  false}` serializes *runs* of this workflow so two cannot race each other, but that only
  protects against this workflow's own concurrent invocations, not e.g. a manual commit to
  `state/state.json` by a human in between.
- **Impact:** The next day's `actions/checkout@v4` pulls the older, pre-loss state, so today's
  already-delivered events are not in the ledger and get resent tomorrow — a duplicate, not a
  loss (the events did reach the inbox once). Would the operator notice? Partially: GitHub's
  default failure-mode email fires because the job exited non-zero (SPEC §13's "Ha a teljes
  futás elhasal, a GitHub emailben értesít"), so the failure itself is loud — but nothing in
  that notification says "and tomorrow's digest will repeat today's," so the consequence is not
  obvious from the alert alone.

---

## Checked and conformant

1. **Item 1 — ledger poisoning, the three prescribed adversarial categories.** CONFORMANT. Zero
   collisions for shared-long-prefix, split-after-last-separator (both directions), and 3+-token
   parenthesised-suffix pairs. See evidence under the parenthesis MINOR finding above for the
   full input/output pairs. (Real, narrower collision surfaces exist elsewhere — the 2-token
   `make_event_id` boundary, MINOR, and the venue-blind `was_sent` fuzzy branch, MAJOR — but
   neither is a failure of these three specific prescribed checks.)
2. **Item 2 — the first-run flood.** CONFORMANT, demonstrated directly with an actual overflow
   past `total_limit`, not just read. `render_email`'s `_select_and_limit` (`email.py:343-381`)
   computes `RenderedEmail.sent_events` **after** applying `per_category_limit` and
   `total_limit` — events dropped by either cap never enter `displayed`, and `_run_pipeline`
   passes only `rendered.sent_events` to `record_sent` (`cli.py:192`). Simulated with 36 fresh
   raw events spread across 6 categories against an empty ledger (`per_category_limit=5` →
   30 eligible, `total_limit=25` config default):
   ```
   raw events fed in: 36 across 6 categories
   summary.sent: 25
   newsletter_total_limit_trimmed log fired: True, entry: [{'dropped': 5, ...}]
   ledger entries recorded: 25
   OK: sent count == total_limit(25); the other 11 eligible-but-trimmed events are NOT in the ledger
   ```
   A second run against the same 36 raw events plus the persisted ledger surfaced 10 of the 11
   previously-trimmed events (the per-category cap trimmed 6, the total cap trimmed a further 5;
   `already_sent` correctly re-excluded the 25 already delivered):
   ```
   run 2 summary.sent: 10   run 2 ledger total: 35
   ```
   Events beyond either cap are not marked sent and correctly resurface on a later run.
   **Not a BLOCKER** — the code already does the safe thing here.
3. **Item 4 — broad exception handling.** CONFORMANT, essentially. Full survey:
   `grep -rn "except" src/digest/` → no bare `except:` anywhere in the project. Exactly one
   `except Exception` in the whole tree: `src/digest/llm/gemini.py:185`, inside
   `GeminiCategorizer._run_batches`, explicitly `noqa`'d and commented ("never on the critical
   path, CLAUDE.md 4"), and it logs `llm_call_failed` with the error string and batch size before
   falling back to the already-computed rule-based categories — a legitimate, narrow, documented
   catch-all around a third-party client call, on a stage that is currently unreachable from
   `cli.py` anyway (confirmed again here: `grep -rn "GeminiCategorizer\|digest.llm" src/digest/cli.py`
   → no hits). Every other `except` in the tree is narrowly typed (`ValueError`, `OSError`,
   `json.JSONDecodeError`, `httpx.HTTPError`, `yaml.YAMLError`, `ImportError`,
   `digest.errors.DigestError`) and each site that drops a record logs a `WARNING` with the
   source id and a reason (`normalize.py:184-190`/`:195-200`; `declarative.py:110`;
   `port_hu.py:143-144`; `gemini.py:105-107`). None of the per-record `continue`/`return None`
   drop paths is a bare `except Exception: continue` masking a systematic bug as "a few bad
   records" in the way the task's example describes — though none of these per-record warnings
   is counted anywhere either, the same root problem as the filter-attribution MAJOR, one level
   upstream (§7.1/§6.3 instead of §7.6).
4. **Item 5 (partial) — selector-health persistence across runs.** CONFORMANT for the
   persistence half. `SourceHealth.last_count` is untouched by the failure branch
   (`cli.py:278-289` only updates `consecutive_failures`/`disabled_until`) and is only
   overwritten on an actual successful fetch (`cli.py:297-308`), so a multi-day run of fetch
   failures does not erase the drift baseline. Round-trips correctly through `save_state`/
   `load_state` (`state.py:61-82`), and `test_a_source_returning_zero_after_a_high_previous_count_logs_selector_drift`
   (`tests/test_run_integration.py:214-236`) exercises exactly this with a pre-seeded
   `last_count=40`. (The *detection* half — does it reach `/status.html`, and can it fire for a
   source broken from day one — is the MAJOR finding above; only the persistence mechanism
   itself is conformant.)
5. **Item 7 — the heartbeat.** CONFORMANT, verified by direct execution of both halves the task
   asks for. First, `render_email` called directly with an empty list:
   ```
   subject: 'Budapest — 2026. augusztus 16.: ma nincs semmi'
   sent_events: []
   'ma nincs semmi' in subject: True
   a line unique to email-empty.html.j2 is present in rendered.html: True
   ```
   confirming `email-empty.html.j2` (not the normal template) is actually the one used
   (`email.py:150-153`) when `displayed` is empty. Second, the full pipeline run against a real
   source that yields zero raw events:
   ```
   summary.sent: 0
   email actually delivered (smtp.sent is not None): True
   email subject: Budapest — 2026. augusztus 16.: ma nincs semmi
   ledger entries: 0
   ```
   `send_when_empty: true` (config default) does cause `_deliver` to run with zero events
   (`cli.py:189`), and an actual "0 new" email reaches the fake SMTP transport. This mechanism
   itself works. It simply is not sufficient on its own — see the BLOCKER above, which shows the
   heartbeat's *record of having fired* (the ledger write) is decoupled from whether delivery
   itself actually succeeded, undermining the very "monitoring" role §10 assigns it.

## Unknown

- **Real-world Gmail app-password failure modes.** `SmtpDeliverer.send` raises `DeliveryError`
  only for missing env vars; an *authentication* failure from Gmail itself (wrong app password,
  revoked credential) would surface as whatever `smtplib.SMTP.login`/`send_message` raises
  (typically `smtplib.SMTPAuthenticationError` or similar), which is **not caught** by anything
  in `smtp.py` or `_deliver` — it would propagate as an unhandled exception, aborting the run
  before `record_sent`/`save_state`, which is the *safe* direction (loud, no state written). I
  did not verify this against a real SMTP server, only by reading `smtplib`'s documented
  exception hierarchy; this is inferred, not executed against real infrastructure. If it is safe
  (which the code structure suggests), the BLOCKER above is specifically about the paths that do
  *not* raise — recipient email absent, or every delivery target disabled/unimplemented — not
  about SMTP transport errors.
- **Actual GitHub Actions push-conflict frequency.** I could not exercise a real concurrent
  `git push` rejection (no live GitHub remote in this environment) — the MINOR finding about the
  unretried `git push` is based on reading the workflow YAML and the concurrency block's actual
  scope, not a reproduced race. To fully assess it I would need to trigger two overlapping real
  workflow runs against a real repository.
- **Whether any downstream consumer of the raw structlog stream aggregates filter/drift reasons
  today.** I confirmed nothing in `src/digest/` does. If the operator has an external log
  pipeline (e.g. GitHub Actions' own log search, or something outside this repo) that already
  aggregates `reason=` fields, the MAJOR filter-attribution finding's practical severity would be
  lower — but nothing in this repository provides or documents such a thing, so I have treated
  its absence as the current state.

## Silent paths inventory

| Path | Can data vanish? | Would the operator notice? | How |
|---|---|---|---|
| `record_sent` after a no-op `_deliver` (missing `recipient_email`, all delivery targets disabled/unimplemented) — **BLOCKER** | **Yes, permanently.** | No. Exit 0, normal-looking `run_summary`, nothing on `/status.html`. | `cli.py:189-192`; `smtp.py:26-28`; `cli.py:229-237` |
| `was_sent`'s venue-blind fuzzy branch (same date + title token overlap) — MAJOR | **Yes, permanently**, for the suppressed event. | No — logs identically to a legitimate `already_sent` exclusion. | `state.py:91-102`, §8.2 |
| Selector drift (>10 → 0, or broken from day one) — MAJOR | No (raw data isn't lost that day), but the *signal that something is broken* is lost after one log line, or never produced at all for a new source. | Only by reading that exact day's Actions log, and only for the regression case. `/status.html` shows nothing either way. | `cli.py:293-295`; absent from `state.py`/`web.py`; `declarative.py:90-97` for the silent-zero-items sub-case |
| Collapsed festival group re-sent every run — MAJOR | No (content isn't lost, it repeats) — but violates "never see it twice." | No signal distinguishes a repeat from a new group. | `group.py:105-112`; `cli.py:164-179` |
| Filter exclusions (6 reasons) — MAJOR (attribution, not the exclusion itself) | No — excluded events are working as designed, this is about diagnosability. | Only by grepping raw structured logs and tallying `reason=` by hand. | `filter.py:36-42`; `cli.py:70-78` |
| `make_event_id` collision at the 2-token parenthesised-suffix boundary — MINOR | Yes, permanently, for the second of two colliding events. | No — dedup's exact-id merge is silent by design. | `models.py:89-98`, §4.1 |
| `git push` rejected after a successful `digest run` — MINOR | No (events were delivered once) — causes a resend, not a loss. | Partially — GitHub's failure-mode email fires, but doesn't explain the resend consequence. | `.github/workflows/digest.yml` "Commit state" step |
| `normalize`'s per-record drops: unparseable start, past, beyond horizon | No — these are correct exclusions by design (§7.1). | Per-record `WARNING`/`INFO` logged with source+key, but never aggregated into a count anywhere. | `normalize.py:184-209` |
| Declarative source: missing non-optional field | No — correct exclusion by design (§6.3). | Per-record `WARNING` with field name; not aggregated. If the *item selector itself* matches nothing, there is no log line at all for that page. | `declarative.py:108-111` |
| Declarative source: `item_selector`/`json_path` matches zero items on a page | Yes, in effect — every event on that page is silently absent that run. | **No log line at all** for this case (only `_page_had_items=False`, used solely for pagination early-stop). Folds into the selector-drift MAJOR above. | `declarative.py:82-97` |
| `filter`: `beyond_horizon`, `category_not_allowed`, `price_too_high`, `blocked_keyword`, `hidden_by_override`, `already_sent` | No — correct exclusions by design (§7.6), except `blocked_keyword`'s word-boundary matching (Audit 1 MAJOR-3, unchanged, out of this audit's re-scope). | Per-event `INFO` log with reason; not aggregated (see MAJOR above). | `filter.py:45-77` |
| `score`: `min_score` cut | No — correct exclusion by design. | Aggregated and logged (`dropped_below_min_score`, with count) — this is the one exclusion stage that *does* meet the bar item 6 asks for. | `score.py:44-49` |
| `render_email`'s `per_category_limit`/`total_limit` | No — confirmed above (item 2): excess events are not marked sent and resurface later. | `newsletter_total_limit_trimmed` is logged with a count when the total cap trims anything; the per-category cap trims silently (events simply never enter `capped[category]`, no log line for how many were dropped per category). | `email.py:357-368` |
| `_expiring_candidates` ("Hamarosan lejár") rows | No — deliberately excluded from `sent_events` so they can still earn a full card later (documented intent, `email.py:98-102`). | N/A — working as designed. | `email.py:384-399` |
| `GeminiCategorizer`'s `except Exception` around the client call | No — falls back to already-computed rule-based categories, run continues. Currently unreachable from `cli.py` regardless. | `llm_call_failed` logged with error + batch size. | `gemini.py:180-189` |
