# Audit 4 — Adversarial Test Quality

Method: manual mutation testing. Each mutation below was applied inside an isolated git
worktree (branched from HEAD `e6810f9`, its own fresh `.venv` editable-installed against
the worktree's own `src/`, verified via `python -c "import digest; print(digest.__file__)"`
before any mutation was applied), the full suite (`pytest tests/ -q`, 323 tests) was run,
the result recorded, then the file was reverted with `git checkout --` and `git status
--short` confirmed clean before moving to the next mutation. All 12 required mutations were
applied one at a time — never combined — and the worktree was empty of diffs after every
single revert.

## Mutation results

| # | Mutation | Killed / Survived | Killing test(s) |
|---|---|---|---|
| 1 | Fuzzy dedup: 90 min → 900 min window | **KILLED** | `test_dedup.py::test_a_three_hour_gap_blocks_a_fuzzy_merge`, `test_dedup.py::test_real_festival_line_up_survives_dedup_intact` |
| 2 | Fuzzy dedup: title-ratio AND time-window → OR | **KILLED** | `test_dedup.py::test_a_three_hour_gap_blocks_a_fuzzy_merge`, `test_dedup.py::test_a_score_in_the_ambiguous_band_is_logged_but_not_merged`, `test_dedup.py::test_unrelated_events_are_left_alone`, `test_dedup.py::test_real_festival_line_up_survives_dedup_intact`, `test_run_integration.py::test_the_full_pipeline_runs_and_produces_output` |
| 3 | `score`: index `weekday_weights` by `start.date()` not `effective_date` | **KILLED** | `test_score.py::test_weekday_weight_uses_effective_date_not_start` |
| 4 | `group`: min_group_size handling disabled, groups never collapse | **KILLED** | `test_group.py::test_a_full_festival_lineup_collapses_to_one_row` (+6 more: `test_the_collapsed_rows_score_is_the_highest_member_score`, `test_the_collapsed_rows_start_is_the_earliest_members_not_the_top_scorers`, `test_the_description_lists_the_three_highest_scoring_titles`, `test_urls_fall_back_to_the_top_scoring_members_url`, `test_the_collapsed_id_is_stable_across_a_different_line_up_size`, `test_source_ids_and_categories_are_unioned_across_members`) |
| 5 | `group`: collapsed row's score = MIN member score, not MAX | **KILLED** | `test_group.py::test_the_collapsed_rows_score_is_the_highest_member_score` |
| 6 | `was_sent`: fuzzy branch deleted, exact id only | **KILLED** | `test_state.py::test_was_sent_catches_a_title_rewrite_that_changed_the_id` |
| 7 | `normalize_title`: add a cut at the last `" - "` separator | **KILLED** | `test_models.py::test_make_event_id_differs_for_titles_sharing_a_leading_segment`, `test_models.py::test_normalize_title_never_cuts_at_a_separator[...]`, `test_models.py::test_normalize_title_leaves_a_real_title_intact` |
| 8 | Web render: `description` added back to `events.json` field list | **KILLED** | `test_web_render.py::test_events_json_has_exactly_the_web_profile_fields_no_description_no_image` |
| 9 | `normalize`: naive datetimes treated as UTC, not Europe/Budapest | **KILLED** | `test_normalize.py::test_naive_input_is_budapest_local_time_not_utc` (+5 more, incl. all of `test_all_date_shapes_parse_to_the_same_instant` and `test_end_is_parsed_when_present`) |
| 10 | LLM layer: `max_calls_per_run` ceiling removed | **KILLED** | `test_gemini.py::test_max_calls_per_run_2_with_100_events_makes_exactly_2_calls_rest_unchanged` |
| 11 | Fetch layer: retry on 4xx as well as 5xx | **KILLED** | `test_fetch.py::test_4xx_is_never_retried` |
| 12 | `filter`: already-sent exclusion removed entirely | **KILLED** | `test_filter.py::test_an_already_sent_event_is_excluded` |

**Tally: 12 KILLED / 0 SURVIVED.**

### Mutations 6 and 12 — the "bypassed by construction" question

The task flagged mutations 6 (`was_sent`) and 12 (`filter`'s already-sent exclusion) as
needing special attention, since `filter()` receives `sent_ids` as a precomputed
frozenset built in `cli.py` via `was_sent(state, event)`, and `filter()` itself never
calls `was_sent`. Both mutations were killed, but by two *different, non-overlapping*
tests, and both kills are genuine unit-level kills, not accidental:

- **Mutation 6** (`was_sent`) was killed by `tests/test_state.py::test_was_sent_catches_a_title_rewrite_that_changed_the_id`,
  which calls `was_sent()` directly. This is not bypassed — it is exactly the right level
  of test for a change inside `was_sent()` itself.
- **Mutation 12** (`filter`'s already-sent check) was killed by
  `tests/test_filter.py::test_an_already_sent_event_is_excluded`, which passes `sent_ids`
  directly as a parameter to `filter_events(...)`, never going through `was_sent`. This is
  also not bypassed in a *bad* sense — it is the correct unit test for `filter()`'s own
  exclusion logic, independent of who computes `sent_ids`.

So neither mutation exposes a "bypassed by construction" gap on its own. However, probing
one level up exposed a real gap that neither of these unit tests can see (see BLOCKER-
adjacent finding below): **no test exercises the `cli.py` line that wires `was_sent()`
into `sent_ids` and threads it into `filter()`/`score()` inside a real `_run_pipeline`
run.** That wiring — `sent_ids = frozenset(event.id for event in events if was_sent(state,
event))` — was deleted outright (replaced with `frozenset()`) as an additional, unscripted
probe and the full suite still passed 323/323. This is the actual "integration-level"
gap the task's framing anticipated; it just isn't reachable by mutating `was_sent` or
`filter` individually, because both of those already have solid direct unit coverage.

## Findings

### [MAJOR] No test exercises the production `was_sent` → `sent_ids` → `filter`/`score` wiring end-to-end

**Spec reference:** SPEC §8.1 ("Enélkül egy két hét múlva induló koncert 14 egymást követő
reggelen bekerülne a hírlevélbe" — without the ledger, a concert starting two weeks out
would land in the newsletter 14 mornings running), §8.2 (`was_sent`), §7.6 (filter's
already-sent exclusion).

**Evidence:** In the audit worktree, `src/digest/cli.py:194`
(`sent_ids = frozenset(event.id for event in events if was_sent(state, event))`) was
replaced with `sent_ids: frozenset[str] = frozenset()`, unconditionally disabling the only
call site that ever populates `sent_ids` from real ledger state. `pytest tests/ -q` still
reported **323 passed, 0 failed.**

**What the spec requires:** the whole reason `state/state.json` exists is so an event is
never re-offered once it has actually been delivered (§8.1's explicit worked consequence).
That guarantee depends on three things composing correctly in one real run: `was_sent()`
recognizing a previously-sent event, `cli.py` collecting those ids into `sent_ids`, and
`filter()`/`score()` receiving and using that set.

**What the code does:** each of the three pieces above is well covered *in isolation*
(`test_state.py`'s `was_sent` tests, `test_filter.py`/`test_score.py`'s `sent_ids=...`
parameter tests), but nothing in `test_run_integration.py` — or anywhere else — ever
seeds a `State` with a `sent` entry matching an event a `_run_pipeline` run will actually
produce, and then checks that event is excluded from that run's output. Every
`test_run_integration.py` scenario starts from `State()` (empty) or a `State` populated
only with `source_health`.

**Impact:** a regression in that one line of wiring — e.g. someone changes
`_run_pipeline` to compute `sent_ids` from `raw_events` instead of `events` (ids wouldn't
match post-dedup/post-group), forgets to pass `sent_ids=sent_ids` into `filter_events`, or
inverts the comprehension — would ship with a fully green test suite. In production this
is exactly CLAUDE.md's stated worst case: silent, and it would resend every open event
every single day rather than crash or log an error.

### [MAJOR] The email profile's inclusion of `description` and source-side `image_url` is never positively asserted

**Spec reference:** SPEC §9.0's table — email profile row for "Átvett leírás" (adopted
description) and "Forrásoldali kép" (source-side image) are both marked ✅.

**Evidence:** `tests/test_render.py`'s `make_event()` fixture always sets `description`
and `image_url` to non-trivial values (`"Egy nagyszerű este vár mindenkire."`,
`"https://media.port.hu/images/example.jpg"`), but no test in that file ever asserts
either value actually reaches `rendered.html` or `rendered.text`. As a probe (reverted),
`src/digest/render/email.py`'s `_build_event_row` was changed to hardcode
`"description": None` and `"image_url": None` regardless of the event. `pytest tests/ -q`
still reported **323 passed, 0 failed.**

**What the spec requires:** the email profile is explicitly supposed to carry the
description and image through, in contrast to the web profile, which is explicitly
supposed to drop them (§9.0). `tests/test_web_render.py` correctly asserts the negative
side of this contrast (`"description" not in record`, no non-local `<img>` src); nothing
asserts the positive side for email.

**What the code does:** `render/email.py` does pass `event.description` and
`event.image_url` into the template context, and `email.html.j2` does render them
conditionally (`{% if event.description %}`, `{% if event.image_url %}`) — the current
implementation is correct. Only the *test* for it is missing.

**Impact:** a future edit that silently drops the description/image from the email (the
exact mirror image of the AUDIT-1 BLOCKER-2 case the web profile already guards against)
would not be caught by any test — the email would quietly become as stripped-down as the
public site, defeating the entire premise of two profiles.

### [NIT] A handful of `filter`/`group` tests would pass unchanged against a no-op stage stub

**Spec reference:** none directly — a test-design observation, item 14 of the audit task.

**Evidence:**
- `tests/test_filter.py:43-44` (`test_an_unremarkable_event_survives`), `:78-82`
  (`test_no_category_restriction_means_everything_passes`), `:147-153`
  (`test_min_score_is_not_one_of_the_five_reasons_here`)
- `tests/test_overrides.py:120-125` (`test_filter_without_any_hidden_ids_excludes_nothing_extra`)
- `tests/test_dedup.py:310-317` (`test_unrelated_events_are_left_alone`)
- `tests/test_group.py:116-144` (`test_different_venues_are_not_merged_together`,
  `test_different_effective_dates_are_not_merged_together`,
  `test_different_primary_categories_are_not_merged_together`)

Each of these tests' sole assertion is "the input passed through with the same count/
content" — a stage function replaced by `lambda events, config, **kw: events` (identity)
would satisfy every one of them.

**What the spec requires / what the code does:** not applicable — this is not a behavior
gap. Every module named above already has *other* tests in the same file that a no-op
stub would fail (confirmed directly: mutations 1, 2, 4, 5 and 12 above were all killed by
tests living alongside these weaker ones). The weak tests are legitimate boundary checks
("a non-matching input is not wrongly excluded/merged"), just not, on their own,
proof the stage does anything.

**Impact:** none currently — rated NIT, not MINOR, specifically because the mutation
sweep above empirically found a killing test in every one of these files for every
assigned mutation. Listed for completeness per the audit's item 14, not as a live risk.

## Checks 13, 15, 16, 17 — no findings

- **13 (hardcoded hash/score total/id assertions):** `grep -rnE '"[0-9a-f]{16}"' tests/*.py`
  found nothing but the char-set check in `test_make_event_id_is_short_hex`
  (`set(event_id) <= set("0123456789abcdef")`, not a hardcoded value). Every
  `score_breakdown[...] == N` assertion in `test_score.py` sets the corresponding config
  weight explicitly in the same test (e.g. `ScoringConfig(free_bonus=2)` →
  `assert ... == 2`), so these test the actual wiring, not a frozen implementation
  snapshot. `test_group.py:82` (`assert result.score == 16.0`) is likewise derived from
  `make_lineup(17)`'s documented `scores 0..16` comment, not an arbitrary literal.
- **15 (network access):** `grep -rnE "httpx\.(get|post|Client)\(|requests\." tests/*.py`
  returned no matches. `test_fetch.py` and `test_run_integration.py` both route every
  request through `respx.mock`/`respx.get(...)`.
- **16 (synthetic fixtures where a real one was required):** the only two source fixtures
  in `tests/fixtures/` are `port_hu_list.json` (SPEC §6.5, "igazolt leképezés" — verified)
  and `bigcitylife_list.html` (real, saved 2026-08-16, documented in
  `sources/bigcitylife.yaml`'s header comment). The other four declarative sources
  (`welovebudapest`, `fidelio`, `programturizmus`, `szinhazak`) are `enabled: false`
  placeholders with `listing.urls: []`, each with a comment explaining why no real fixture
  could honestly be fetched (robots.txt naming Anthropic's crawler, no discoverable
  listing page, fragile/triple-counted/nationwide-scoped markup, dead or non-listing
  domain respectively) — exactly what prompt-packages.md package 11 requirement 7 asks
  for when a real fixture isn't obtainable. The hand-written HTML snippets inside
  `test_declarative_source.py` (e.g. `'<div class="card"><h3>Villon-est</h3>...'`) test the
  generic CSS/JSONPath engine's mechanics, not a specific source's real selectors, which is
  the correct use of synthetic markup.
- **17 (order dependence):** `pytest -p no:randomly tests/ -v` (323 passed — no
  `pytest-randomly` plugin is installed, so this only confirms the default deterministic
  order is green) and `pytest tests/ -v --deselect
  tests/test_config.py::test_every_category_gets_a_weight` (the slowest test at 0.02s;
  323 passed, 1 deselected). As a further check beyond the letter of the task, the full
  suite was also run with every test file in fully reversed order
  (`test_web_render.py` first, `test_categorize.py` last): still 323 passed. No
  order-dependent test found.

## Coverage gaps

Requirements from SPEC.md with no test that would catch their violation:

1. **§8.1/§8.2, end-to-end "don't resend."** No test seeds a `State` with a `sent` entry
   for an event a `_run_pipeline` run will produce and confirms that event is excluded
   from that run's output. See the MAJOR finding above — confirmed by deleting the
   `sent_ids` wiring in `cli.py` outright with no test failures.
2. **§9.0, email profile positively includes `description`/`image_url`.** No test asserts
   these fields actually appear in the rendered email HTML/text, only that the web
   profile correctly omits them. See the MAJOR finding above — confirmed by hardcoding
   both fields to `None` in `render/email.py` with no test failures.
3. **§6.4, `If-None-Match` / conditional request driven by a real ledger ETag inside a
   full run.** `test_fetch.py::test_etag_produces_a_conditional_request_and_304_is_cached`
   exercises `HttpFetcher.fetch(..., etag=...)` directly, and
   `test_run_integration.py` never seeds `state.source_health[...].etag` before a
   `_run_pipeline` call to confirm the stored ETag actually reaches the fetcher on a
   real run (only that the fetcher forwards whatever etag it's given, and that
   `_fetch_source` reads `health.etag` — the latter is exercised only incidentally,
   never asserted against the request headers).
4. **§7.5, `min_category_score` boundary combined with `native_types`' "+4, strong"
   claim.** `test_categorize.py::test_native_type_signal_is_the_strongest_single_signal`
   compares totals for two *different* events/rules rather than confirming the documented
   `+4` constant itself, so a change from `+4` to, say, `+3` (still likely the single
   strongest signal in that comparison) would not necessarily be caught.
