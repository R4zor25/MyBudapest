# Audit 1 — Spec conformance

Scope: `SPEC.md`, `CLAUDE.md`, every file under `src/digest/`. Report only — no source file
was modified. Baseline: `git rev-parse --short HEAD` = `0787bac`, working tree as found.
Test baseline: `./.venv/bin/python -m pytest -q` → **319 passed in 1.19s**.

## Summary

Nine of the ten targeted checks pass on the actual code path, several of them precisely at
the point where a plausible-looking wrong implementation would have slipped through: the id
path is provably separated from `strip_venue_suffix`, the fuzzy dedup condition is a true
three-way AND with hardcoded (non-configurable) thresholds, the stage composition matches
`normalize → dedup → recurrence → categorize → filter → score → group`, `weekday_weights` is
indexed by `effective_date`, `score_breakdown` is complete and sums exactly to `score`, and
`was_sent`'s fuzzy branch is live rather than shadowed by an early return. The tenth (LLM
layer) is not conformant-or-broken but **unreachable**: `GeminiCategorizer` is never imported
by the run.

Findings: **2 BLOCKER, 3 MAJOR, 14 MINOR, 3 NIT**, plus 13 items of undocumented behaviour.
BLOCKER-1 stops the system from ever completing a real run: `save_state` writes to
`state/state.json` without creating `state/`, and that directory does not exist in the repo —
so `digest run` raises `FileNotFoundError` *after* the email has been delivered, on every run,
forever. No test catches it because every integration test passes a `tmp_path`-derived state
path whose parent already exists. BLOCKER-2 is a secret leak on deploy: the public
`events.json` breakdown discloses `category_weights`, `weekday_weights` and an inferable home
district — data §12 declares secret — because §9.0/§9.1 explicitly mandate publishing it.
That one is a spec-vs-spec contradiction rather than a code defect, and it is graded by
consequence, per the scale below; the required action is a spec decision, not an edit to
`render/web.py`. The MAJORs are: collapsed festival rows are written to the ledger but never
checked against it (demonstrated re-sending on three consecutive simulated runs); one
malformed source descriptor aborts the entire run, contra §13 / CLAUDE.md 6; and keyword
matching uses word boundaries where the spec says "szerepel" (contains), which makes
`blocked_keywords` fail open on Hungarian inflected forms.

**Verdict: not deployable as-is.** BLOCKER-1 must be fixed before the first run and BLOCKER-2
decided before the site goes public. Note the fix ordering: **BLOCKER-1 currently masks
MAJOR-1.** While the ledger never persists, `was_sent` always returns `False` and *everything*
re-sends daily, so fixing `save_state` will make the digest appear to start working — and
MAJOR-1 will not become visible until the next multi-day festival. Both must land before then.
Nothing in the ten targeted checks was found to be quietly weakened — the code is careful and
generally faithful to the spec. The damage is concentrated in the seams between modules
(state directory creation, source construction, post-group ledger check), which is exactly
where per-module tests do not look.

---

## Findings

### [BLOCKER] `save_state` cannot write the ledger — `state/` does not exist and is never created

- **Spec reference:** SPEC.md §3 (repo structure: `state/state.json` "a ledger, committolva"),
  §8.1, §11 (workflow `actions/checkout@v4` then `digest run`)
- **Evidence:**
  - `src/digest/state.py:76-77`
    ```python
    def save_state(state: State, path: Path) -> None:
        path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    ```
    No `path.parent.mkdir(...)`. Compare `src/digest/render/web.py:89`, which *does*
    `site_dir.mkdir(parents=True, exist_ok=True)` for the sibling output.
  - `grep -rn "mkdir" src/` → only `src/digest/render/web.py:89` and `:95`. Nothing creates
    `state/`.
  - `ls -la state` → `ls: state: No such file or directory`
  - `git ls-files | grep -c "^state/"` → `0`
  - Reproduced directly:
    ```
    load_state on missing file -> State           # tolerated, as designed
    save_state raises FileNotFoundError: [Errno 2] No such file or directory:
        '/var/folders/.../state/state.json'
    ```
  - Call site: `src/digest/cli.py:205` `save_state(state, state_path)`, with
    `_STATE_PATH = Path("state/state.json")` at `src/digest/cli.py:55`.
  - **Why the suite is green:** every integration test supplies a flat path whose parent
    pytest already made —
    `grep -n "state_path" tests/test_run_integration.py` → `state_path = tmp_path / "state.json"`
    at lines 127, 157, 177, 195. No test ever exercises the production default
    `Path("state/state.json")`.
- **What the spec requires:** §8 — the ledger is the one thing that must survive a run
  ("Egyetlen dolognak muszáj átmennie: mit küldtünk már ki"). §11 checks out the repo fresh
  and runs `digest run`, then commits `state/state.json`.
- **What the code does:** `_run_pipeline` delivers the email (`cli.py:189-190`), records the
  sent events in memory (`cli.py:192`), then dies at `cli.py:205` with `FileNotFoundError`.
  The exception is not a `DigestError`, is not caught anywhere, and propagates out of
  `digest run`.
- **Impact:** The system cannot bootstrap. Run 1: email goes out, process exits non-zero, the
  workflow's "Commit state" and Pages-deploy steps never execute, `state/state.json` is never
  created. Run 2 is identical, and so is every run after it. The ledger never exists, so §1's
  success criterion ("nincs benne olyan, amit korábban már láttál") can never be met — the
  same events are mailed every morning until they pass. Data loss (the ledger) plus a hard
  run failure that happens *after* the side effect it was supposed to guard.
  **Fix ordering:** this masks MAJOR-1. With no persisted ledger, `was_sent` always returns
  `False`, so *every* event re-sends daily and the festival-row case is indistinguishable from
  the general one. Repairing `save_state` will make the digest look correct; MAJOR-1 only
  becomes observable at the next multi-day festival.

---

### [BLOCKER] The public `events.json` breakdown discloses profile data §12 declares secret

- **Spec reference:** SPEC.md §9.0 (web profile table: "Ár, kategória, pontszám, **bontás** ✅")
  and §9.1 (events.json example literally contains `"breakdown": { "category": 4, ...
  "proximity": 2, "weekday": 2, ... }`) **versus** §12 and CLAUDE.md rule 5
- **Evidence:**
  - `src/digest/render/web.py:131-144` publishes `"score"` and `"breakdown"` for every event;
    `_map_breakdown` (`:173-183`) emits `category`, `keyword`, `free`, `cheap`, `novelty`,
    `soon`, `weekday`, `proximity`.
  - Real output from `_event_to_json` on a scored event, with the §5.2 sample profile
    (`category_weights: {koncert: 4}`, `weekday_weights: {fri: 2}`, `same_district_bonus: 2`,
    `distance_penalty_per_km: 0.3`):
    ```
    web breakdown: {'category': 4.0, 'keyword': 3.0, 'free': 0.0, 'cheap': 1.0,
                    'novelty': 2.0, 'soon': 1.0, 'weekday': 2.0, 'proximity': -0.49}
    ```
    `category: 4.0` *is* `category_weights["koncert"]`; `weekday: 2.0` *is*
    `weekday_weights["fri"]`. No inference step required for those two.
  - `district` is also published per event (`web.py:137`), and `start` (`:135`) yields the
    weekday that keys the `weekday` term.
- **What the spec requires:** §9.0/§9.1 require the breakdown on the public site. §12 requires
  that "pontozási súlyok, `keyword_boosts`, `home`" stay in the `PROFILE_YAML` secret,
  because "a súlyok, a kulcsszó-boostok és a kerület együtt olvasható térkép az ízlésedről és
  arról, hol laksz."
- **What the code does:** Exactly what §9.0/§9.1 instruct. It is careful where it can be —
  `pinned_bonus` is dropped from the breakdown *and* subtracted back out of the published
  `score` (`web.py:130-141, 165-170`), and `description`/`image_url`/`lat`/`lon` are absent.
  But the mandated terms themselves are the disclosure.
- **Impact:** From a public `events.json` an observer recovers `category_weights` directly
  (the `category` term is the weight for that event's primary category), `weekday_weights`
  directly (the `weekday` term keyed by each event's date), and the home district by grouping
  the published `district` against the jump in `proximity` (`same_district_bonus` is a
  constant added only for the matching district). `keyword_boosts` is **not** individually
  recoverable — `score.py:99-103` publishes only the *sum* of matched boosts — so that part
  should not be overstated. Net: §12's stated threat model is defeated by §9.0's own table.
  **Graded BLOCKER by consequence, not by culpability:** the scale reads "must fix before
  deploy — data loss, secret leak, or silent system failure", and deploying this publishes
  data the spec itself designates secret. The code is not at fault and
  **`_event_to_json` should not be "fixed"** — editing it would put the code out of
  conformance with §9.1's schema and break the design artefact's `BD` lookup table. The
  required action is a spec decision before the site goes public: either drop `bontás` from
  §9.0's web column and amend §9.1's schema, or accept the disclosure and amend §12's
  guarantee. "Accept and amend §12" is a legitimate resolution and needs no code change —
  but it has to be a decision, not a default.

---

### [MAJOR] Collapsed group rows are recorded in the ledger but never checked against it

- **Spec reference:** SPEC.md §7.4 (grouping is mandatory), §7.6 (filter excludes "a ledger
  szerint már kiküldött"), §8.2, §1 (success criterion)
- **Evidence:**
  - The ledger is consulted in exactly one place, and it is upstream of `group`:
    `grep -rn "sent_ids" src/digest/` → `cli.py:164, 172, 176`, `filter.py:17/37/48/74`,
    `score.py:30/44/55/70/82`. **`src/digest/pipeline/group.py` contains no reference to
    `sent_ids`, `was_sent` or `State`.**
  - Ordering, `src/digest/cli.py:164-192`: `sent_ids` is computed at :164, consumed by
    `filter_events` at :172, then `group(events, config)` at :179 *creates new events with
    new ids* (`src/digest/pipeline/group.py:78` → `_group_id`, `:105-112`), and
    `record_sent(state, rendered.sent_events, ...)` at :192 writes those new ids.
  - Reproduced over three simulated consecutive runs (17 same-venue events, `min_group_size`
    4, ledger carried forward via `record_sent`/`was_sent`):
    ```
    run 1: filter kept 17, after group 1, email rows 1, titles=['Hajogyari-sziget — 17 program']
              ledger before: 0 entries; ids in ledger: []
    run 2: filter kept 17, after group 1, email rows 1, titles=['Hajogyari-sziget — 17 program']
              ledger before: 1 entries; ids in ledger: ['b92f08a7bac3fcc3']
    run 3: filter kept 17, after group 1, email rows 1, titles=['Hajogyari-sziget — 17 program']
              ledger before: 2 entries; ids in ledger: ['b92f08a7bac3fcc3', 'b92f08a7bac3fcc3']
    ```
- **What the spec requires:** §1 — the morning email must not contain anything already seen.
  §8's whole justification is that "enélkül egy két hét múlva induló koncert 14 egymást
  követő reggelen bekerülne a hírlevélbe."
- **What the code does:** The collapsed row's id (`_group_id`, correctly stable across runs
  by design) is recorded as sent, but the members that regenerate it next run carry their own
  ids, which are not in the ledger. They pass `filter`, re-collapse to the identical group id,
  and the row is mailed again. Note the code follows the spec's **mandated** ordering
  (§7.4: `... filter → score → group → limit`) — a ledger check that would catch this has to
  run *after* `group`, and §7.6 does not describe one. This is a hole in the spec that the
  code faithfully inherited, not a misimplementation.
- **Impact:** Exactly the case §7.4 was written for regresses. The Port.hu sample was 17 of 20
  records from one festival; that collapsed row would be the top item in the digest every
  morning for the festival's whole run. Secondary effect: `record_sent`
  (`src/digest/state.py:100-108`) appends unconditionally, so the `sent` list accumulates a
  duplicate entry for the same group id on every run until `purge` drops it by date.

---

### [MAJOR] One malformed source descriptor aborts the entire run

- **Spec reference:** SPEC.md §13 ("Per-source izoláció: egy forrás hibája sosem buktatja el
  a futást"), CLAUDE.md rule 6
- **Evidence:**
  - `src/digest/cli.py:127` — `sources = load_sources(config)` is called in `_run_real`
    **outside** any `try/except`, and before `_run_pipeline`. The per-source `try/except`
    lives at `src/digest/cli.py:263-289`, inside `_run_sources`, which only runs after every
    source object has already been constructed.
  - `src/digest/sources/registry.py:36-48` constructs all sources eagerly in one loop;
    `_build_plugin_source` (`:51-65`) raises `ConfigError` on import failure or a missing
    `build`.
  - `src/digest/sources/declarative.py:57-60` raises `ConfigError` when a mandatory field
    mapping is absent; `:31` does `spec["id"]` with no guard.
  - Reproduced against the real config:
    ```
    FIELDS-MISSING raises ConfigError: source 'broken': fields ['title'] are not optional
    NO-ID raises KeyError: KeyError('id')
    BAD-PLUGIN raises ConfigError: source 'ghost' names plugin 'nope': No module named
        'digest.sources.plugins.nope'

    Is ConfigError a DigestError? True
    Is KeyError a DigestError? False
    ```
  - Upstream of that, `load_config` → `_load_source_specs` → `_read_yaml_file`
    (`src/digest/config.py:177-189`) raises `ConfigError` for any unreadable or invalid
    `sources/*.yaml`, also outside per-source isolation.
- **What the spec requires:** A single source's failure costs that source's events for the
  day and nothing else.
- **What the code does:** A typo in any one of the six `sources/*.yaml` files — a dropped
  `title:` mapping, a bad `plugin:` name, a YAML syntax error, an omitted `id:` — kills the
  whole digest before the first HTTP request. The `id:`-missing case escapes as a bare
  `KeyError`, which is not a `DigestError` and would not be caught even if the call were
  moved inside the guarded region.
- **Impact:** Adding a source "kódolás nélkül" (§16 M5, the stated point of the declarative
  engine) is exactly the operation most likely to produce a malformed descriptor, and it takes
  down every other source with it. The failure mode is loud (workflow email per §13), so it is
  not silent — but it is precisely the isolation guarantee §13 promises.

---

### [MAJOR] Keyword matching is word-boundary, not containment — `blocked_keywords` fails open

- **Spec reference:** SPEC.md §7.5 (`keywords` — cím + leírás, súlyozva), §7.6
  (`blocked_keywords` egyezés), §7.7 ("Σ keyword_boosts ahol a kulcsszó **szerepel** a címben
  vagy leírásban")
- **Evidence:**
  - `src/digest/models.py:127-133`
    ```python
    def contains_word(text: str, phrase: str) -> bool:
        pattern = r"(?<!\w)" + re.escape(fold_text(phrase)) + r"(?!\w)"
        return re.search(pattern, fold_text(text)) is not None
    ```
    Used by all three consumers: `pipeline/categorize.py:29`, `pipeline/filter.py:71`,
    `pipeline/score.py:102`.
  - `blocked_keywords` failing open (config.yaml's own §5.2 values):
    ```
    blocked=True  keyword='bábszínház'    title='Bábszínház a Kolibriben'
    blocked=False keyword='bábszínház'    title='Bábszínházi előadás gyerekeknek'
    blocked=False keyword='gyerekprogram' title='Gyerekprogramok a Millenárison'
    blocked=True  keyword='gyerekprogram' title='gyerekprogram a Millenárison'
    ```
  - Categorization/scoring under-matching, same primitive:
    ```
    False  keyword='koncert'      text='Koncertek hétvégén'
    False  keyword='koncert'      text='Ingyenes koncertre várunk'
    False  keyword='kiállítás'    text='A kiállítást ma nyitják'
    False  keyword='társasjáték'  text='Társasjátékozás a Red & Blackben'
    False  keyword='sörkóstoló'   text='Sörkóstolóval egybekötött est'
    False  keyword='akusztik'     text='Akusztikus est a Dürer Kertben'
    ```
    `config.yaml`'s `akusztik: 2` is a stem, not a Hungarian word — under this rule it can
    never fire on any real text. Same for the intent behind `lemezbemutató`, `borkóstoló`
    etc. once inflected.
  - **Status split — read this before the argument below:**
    - *Demonstrated, live now, fixture-independent:* `blocked_keywords` fails **open**. §7.6's
      exclusion does not fire on inflected forms of the user's own block list.
    - *Latent, not currently observable:* the categorization/scoring side fails **closed**.
      **0 instances across both repo fixtures** — Port.hu is 20/20 → `koncert` via
      `native_type` (+4), bigcitylife is 6/6 → `koncert` via `venue_prior`; nothing reaches
      `egyeb`. Other signals mask it today. It surfaces as sources lacking `native_types`
      and `venue_prior` coverage are added (`grep -l "native_category" sources/*.yaml` →
      none map it).
- **What the spec requires:** "szerepel a címben vagy leírásban" — plain containment.
  §7.6 requires a `blocked_keywords` match to exclude the event.
- **What the code does:** Requires a `\w` boundary on both sides. Hungarian is agglutinative,
  so most real occurrences are suffixed and do not match. The docstring justifies the rule
  with `koncert` ⊄ `koncertterem` — a real concern, but it is traded against the far more
  common inflected case, and the spec chose containment.
- **Impact:** Two directions, of different weight — severity rests on the first.
  *Fails open (live, and why this is MAJOR):* `blocked_keywords` is the user's explicit
  "never show me this" control, and it is the one filter whose failure the user cannot work
  around from config. `"bábszínház"` does not block `"Bábszínházi előadás"`; §7.6 is not
  enforced.
  *Fails closed (latent):* fewer keyword hits → lower category scores → more events fall to
  `egyeb` → §5.2's `filters.categories` list does not contain `egyeb`, so `filter.py:58-61`
  drops them with `category_not_allowed`, silently and with no user-visible trace. Currently
  masked by `native_type`/`venue_prior` (see the status split above); the masking is
  incidental, not designed, and thins out as §6.6's remaining sources land.

---

### MINOR findings

MINOR, NIT and undocumented items are tabulated rather than expanded to the five-bullet
format — at 26 items the tables are more usable than 26 sections. Each row still carries the
spec reference, `file:line` evidence, and an impact note.

| # | Finding | Spec ref | Evidence | Note |
|---|---|---|---|---|
| m1 | ETag is sent but never stored. `SourceHealth.etag` is read at `cli.py:332` and turned into `If-None-Match` at `http.py:93-94`, but nothing ever assigns it — `_update_health` (`cli.py:297-308`) omits `etag`, and `_build_result` never reads the response header. `If-Modified-Since` is never sent at all. | §6.4 | `grep -rn "etag" src/` → `cli.py:332`, `state.py:37`, `http.py:65/71/91/93/94` only | Conditional requests never actually happen. Latent hazard: a 304 yields `FetchResult(text="", json=None)` (`http.py:72-79`), which `PortHuSource.parse` (`port_hu.py:81-84`) turns into `ParseError` → counted as a source failure toward the 5-strike auto-disable. |
| m2 | `min_score` is applied in `score()`, not `filter()`. | §7.6 lists it as a filter reason | `score.py:45`; `filter.py:21-23` documents why | Outcome-equivalent given the mandated ordering (filter runs before score, so the value does not exist yet). The spec is stale, not the code. |
| m3 | There is no `limit` pipeline stage. `render_email._select_and_limit` (`email.py:343-381`) owns `per_category_limit`/`total_limit`. | §7.4 ordering line, CLAUDE.md architecture diagram | `cli.py:141-144` documents the deviation | Consequence: `render_web` applies no limit at all (`web.py:50-55`), so email and site diverge in content by design. |
| m4 | `scoring.proximity.max_distance_km` is parsed and never read. | §5.2 | `grep -rn "max_distance_km" src/` → only `config.py:112` | Config key with zero effect; §7.7's formula never mentions it either. |
| m5 | `novelty_bonus` is a constant for every event that reaches scoring. `filter` already removed everything in `sent_ids` (`filter.py:74`), so `score.py:82`'s `event.id not in sent_ids` is always true post-filter. | §7.7 | `cli.py:172` then `:176`, same `sent_ids` | The term adds a flat offset rather than discriminating. Inherent to the mandated ordering. |
| m6 | `sources/*.yaml` content is not schema-validated. `Config.sources` is `dict[str, dict[str, Any]]` (`config.py:155`), so unknown keys in a source descriptor are silently accepted, and §6.3's "id … = fájlnév" is unenforced. | §5.3 ("ismeretlen kulcs → hiba, nem néma átugrás"), §6.3 | `config.py:185-189`; all six files happen to match today (`id` == stem, verified) | A stem/`id` mismatch would silently break dedup priority lookup (`dedup.py:163-165` falls back to `_UNKNOWN_SOURCE_PRIORITY`). |
| m7 | `max_per_venue` caps per `(venue_name, effective_date, primary_category)`, not per venue. | §7.4 ("venue-nként legfeljebb `max_per_venue`") | `group.py:41-43` builds the key, `:37` passes it to `_cap` | A venue with three sub-threshold categories on one day emits up to 9 rows, not 3. |
| m8 | Category qualification uses `>=` where the spec says "fölötti" (strictly above). | §7.5 | `categorize.py:61` `score.total >= config.min_category_score` | Non-trivial: `min_category_score: 2` and many `config.yaml` keywords/venue_priors are weighted exactly 2, so this decides real membership (e.g. `venue_prior: 2.0` alone qualifies — see the bigcitylife evidence above). |
| m9 | `RawEvent.url_category` is computed and then discarded. `_normalize_one` (`normalize.py:215-235`) never carries it onto `Event`; `categorize` matches `url_patterns` against full `event.urls` instead (`categorize.py:39-42`). | §4 (RawEvent field), §6.5 | `grep -rn "url_category" src/` → set in `port_hu.py:140` and `declarative.py:132`, read nowhere | Dead data. Matching the full URL is a reasonable substitute, but then the field should go. |
| m10 | The LLM layer is unreachable from `digest run` — see the Unknown section. | §7.5 | `cli.py` never imports `digest.llm` | |
| m11 | Bare `raise NotImplementedError` for a missing `--fixture`. | CLAUDE.md ("Kivételek: saját típusok az `errors.py`-ban, nem `Exception`") | `cli.py:104, 348, 369, 395` | Should be a `DigestError` subclass or a Typer usage error. |
| m12 | A string `district_raw` is passed through verbatim: `normalize.py:144-146` returns `raw.district_raw.strip()`. A source emitting `"11"` yields `district == "11"`, which can never equal `home.district == "XI."`. | §7.1, §5.2 | `normalize.py:142-147` | Only the `int` branch goes through `roman_district`. No current source emits a numeric string, so latent. |
| m13 | `robots.txt` is fetched per fetcher instance, not per run, and bypasses the rate limiter. `cli.py:245` builds two fetchers (`http`, `api`), each with its own `self._robots` cache (`http.py:43`); `_load_robots` (`http.py:182`) calls `self._client.get` directly without `_respect_rate_limit`. | §6.4 ("forrásonként egyszer, cache-elve a futás idejére"), CLAUDE.md 10 | `http.py:168-191` | Up to two robots requests per origin per run, both unthrottled. |
| m14 | No source supplies price data except one optional selector. §6.6 designates Jegy.hu as the price source; `sources/jegy-hu.yaml` does not exist. | §6.6, §5.2 | `grep -rn "price_raw" sources/` → only `bigcitylife.yaml:29` (`optional: true`); `port_hu.py:135` sets `price_raw=None` | `free_bonus`, `cheap_bonus` and `filters.max_price_huf` are near-inert today. Milestone-scoped (M1), noted for completeness. |

### NIT findings

| # | Finding | Evidence |
|---|---|---|
| n1 | `Event` is not frozen, though `RawEvent` and every config/state model are. §4 says `frozen=True` "ahol lehet", and every stage already uses `model_copy`, so it is possible here. | `models.py:54` (no `model_config`) vs `models.py:32`, `state.py:23/32/42/52`, `config.py:21` |
| n2 | `filter` shadows the builtin, forcing `from digest.pipeline.filter import filter as filter_events` at every call site. | `filter.py:14`, `cli.py:26` |
| n3 | §7.1 says past events are dropped; the code keeps an event whose `end` is still in the future. Deliberate and documented (a festival that opened in May must reach `recurrence`), but it is a spec deviation. | `normalize.py:202-206` |

### Undocumented behaviour (spec is silent; either it is stale or the code invented this)

| # | Behaviour | Location |
|---|---|---|
| u1 | **The whole overrides feature.** `overrides.yaml` with `hidden`/`pinned` id lists, a `hidden_by_override` filter reason, and `PINNED_BONUS = 100.0` folded into `Event.score`. SPEC §16 M8 mentions an "Író UI" but specifies no file, no schema, no scoring effect. The 100-point bonus flows into the **email's** displayed score and score bar and into all ordering. Also note `overrides.yaml` sits in the public repo root and encodes personal preferences — a §12 concern the spec never addresses. | `overrides.py` (whole file), `score.py:24/31/87`, `filter.py:18/52-53`, `cli.py:57/167-169` |
| u2 | `Event.native_categories` — a field not in §4's model, added to carry §7.5's `native_types` signal. Self-documented at `models.py:73-76`. | `models.py:76` |
| u3 | `_collapse` overrides the row's `start` with `min(member starts)`, synthesizes a new `id` via `_group_id`, and sets `group_key` to a `venue|date|category` string. §7.4 specifies `title`, `score`, `group_size`, `urls`, `description` only. | `group.py:76-93`, `:105-112` |
| u4 | `_URL_PATTERN_SCORE = 3.0`. §7.5 numbers only `native_types` (+4); this value was chosen by the implementer. | `categorize.py:16` |
| u5 | A fifth template, `email-empty.html.j2`, for the `send_when_empty` heartbeat. §9.1's table lists four. | `email.py:150-153`, `render/templates/email-empty.html.j2` |
| u6 | `render_email` selects the "Hamarosan lejár" section itself, and deliberately excludes those rows from `sent_events` so they are never recorded in the ledger. | `email.py:98-102`, `:384-399` |
| u7 | The site is written on every run regardless of `send_when_empty`, and with no `per_category_limit`/`total_limit`. | `cli.py:182-187`, `web.py:50-55` |
| u8 | The web breakdown key remapping (`category_weight`→`category` etc.), the merging of `same_district_bonus` + `distance_penalty` into one signed `proximity` term, and the subtraction of `pinned_bonus` from the published `score`. | `web.py:154-183`, `:130/141` |
| u9 | `contains_word`'s word-boundary rule itself (see MAJOR-3). | `models.py:127-133` |
| u10 | Hungarian spelled-out month parsing and the `%Y.%m.%d.` formats. Arguably inside §7.1's "magyar display formátumok", but the month table is a hardcoded lookup. | `normalize.py:25-55, 88-97` |
| u11 | Archive purging runs inside `digest run` rather than as a workflow step. Documented reasoning: §11's YAML is pinned byte-for-byte. | `web.py:99-116` |
| u12 | `_with_neutral_category_weights` injects weight `1.0` for every configured category when the profile omits it — the code's interpretation of §5.3's "minden súly 1". Every *other* weight (`free_bonus`, `novelty_bonus`, `weekday_weights`, …) neutralises to 0, not 1. | `config.py:203-212`, `:17` |
| u13 | `_UNKNOWN_SOURCE_PRIORITY = 1000` for a source the config does not describe, in dedup merge-base selection. | `dedup.py:21`, `:156-165` |

---

## Checked and conformant

1. **SPEC §4.1 — `make_event_id` uses the conservative `normalize_title`.** CONFORMANT.
   Call graph traced, not docstrings: `make_event_id` (`models.py:153-157`) calls only
   `normalize_title` (`:89`) and `normalize_venue` (`:117`). `normalize_title`'s body touches
   only `_strip_accents`, `_TRAILING_PARENS_RE` and `_WHITESPACE_RE` — `_SEPARATOR_RE`
   (`models.py:12`) is referenced **exclusively** inside `strip_venue_suffix` (`:107`), so
   nothing in the id path cuts at a separator. `grep -rn "strip_venue_suffix" src/` returns
   exactly one non-definition call site: `dedup.py:32`, inside `fuzzy_title`, which is used
   only by `_fuzzy_score` (`dedup.py:103`). `strip_venue_suffix` is unreachable from
   `make_event_id`. Both of §4.1's mirror-image counterexamples verified to produce distinct
   ids:
   ```
   A: 'koncert - sub focus' | 'koncert - chase & status'   ids differ: True
   B: 'a38 | koncert x'     | 'a38 | koncert y'            ids differ: True
   parens: 'sub focus'  ||  'album (deluxe anniversary edition)'   (2-token rule holds)
   ```
2. **SPEC §7.2 — the fuzzy dedup AND is intact.** CONFORMANT. `_fuzzy_score`
   (`dedup.py:97-103`) returns `None` — not a low score — when `abs(a.start - b.start) >
   timedelta(minutes=90)` (`:99`) or when `_venues_match` is false (`:100`), and only then
   computes the title ratio. `_venues_match` (`:106-110`) is `>= 85` OR one venue `None`,
   as specified. The caller requires `>= 88` (`:74`). None of the three was weakened to OR
   or dropped. **Not configurable:** `_TITLE_MERGE_RATIO = 88`, `_TITLE_AMBIGUOUS_RATIO = 80`,
   `_VENUE_RATIO = 85`, `_MAX_START_GAP = 90 minutes` are module constants at
   `dedup.py:15-18`, and no `Config` model exposes any of them — the
   "configurable-with-a-lax-default" failure mode is ruled out. The 80–88 ambiguous band logs
   without merging (`:78-89`), per spec.
3. **SPEC §7.4 + §7.7 — stage ordering.** CONFORMANT. Composed in `_run_pipeline`,
   `cli.py:153-179`: `normalize` (:153) → `dedup` (:156) → `recurrence` (:158) →
   `categorize_events` (:159) → `filter_events` (:172) → `score` (:176) → `group` (:179).
   `group` runs after `score`, and `_collapse` reads `top.score` (`group.py:65, 91`) as the
   spec requires. The `--dry` path (`cli.py:106-110`) composes the same order independently.
4. **SPEC §7.7 — `weekday_weights` indexed by `effective_date`.** CONFORMANT.
   `score.py:84-86`: `scoring.weekday_weights.get(_WEEKDAY_KEYS[event.effective_date.weekday()], 0.0)`
   — `event.start` is not used for the weekday anywhere. Verified end-to-end with a
   Saturday 02:00 event and `night_shift.before_hour: 5`:
   `start = 2026-08-22 (Sat)` → `effective_date = 2026-08-21 (Fri)` → `weekday_weight: 2.0`
   (the `fri` weight), not the `sat` one. `effective_date` itself is set correctly at
   `normalize.py:223`.
5. **SPEC §7.7 — `score_breakdown` is complete and sums to `score`.** CONFORMANT. The dict is
   built unconditionally with all ten terms (`score.py:75-88`) — zero-valued terms are
   present rather than skipped, and the distance term is stored negative
   (`_distance_penalty`, `:121-125`). `score` is literally `sum(breakdown.values())`
   (`score.py:89`). Verified on a non-trivial event (paid, in-district, 8.3 km away,
   keyword hit, Friday):
   ```
   breakdown: {'category_weight': 4.0, 'keyword_boosts': 3.0, 'free_bonus': 0.0,
               'cheap_bonus': 1.0, 'same_district_bonus': 2.0, 'distance_penalty': -2.49,
               'novelty_bonus': 2.0, 'soon_bonus': 1.0, 'weekday_weight': 2.0,
               'pinned_bonus': 0.0}
   score: 12.51   sum: 12.51   EQUAL: True
   ```
   Two zero terms and the negative distance term are all present. (Term *names* differ from
   §9.1's short vocabulary; `render/web.py` maps them — see u8.)
6. **SPEC §8.2 — `was_sent`'s fuzzy branch is live, not dead code.** CONFORMANT, and the
   named failure mode is specifically absent. `state.py:86-97`: the exact-id check sits
   **inside** the per-entry `for` loop (`:90-91`), so a non-matching id falls through to the
   fuzzy check (`:93-96`) on that same entry and on every subsequent one. There is no early
   `return` before the loop and no `else`. Both conditions are ANDed as specified
   (`entry.d == event.effective_date` and `token_set_ratio >= 92`). It is genuinely reached
   in production: `cli.py:164` computes `sent_ids` through `was_sent`, not through raw id
   membership. `record_sent` (`state.py:105`) stores `t` with the same `normalize_title`,
   so the two sides agree.
7. **SPEC §9.0 — the web profile is an inclusion list.** CONFORMANT. `_event_to_json`
   (`web.py:119-144`) is a literal dict of 11 named keys, not `model_dump()` with
   exclusions — a new `Event` field cannot leak by default. Verified output keys:
   `['breakdown', 'categories', 'district', 'group_size', 'id', 'is_free', 'price_min',
   'score', 'start', 'title', 'url', 'venue']` — no `description`, no `image_url`, no
   `lat`/`lon`. The templates agree: `grep -n "img\|description\|image"` over
   `index.html.j2`/`status.html.j2` returns one hit, `role="img"` on a `<div>`
   (`index.html.j2:424`) — no `<img>` tag and no description anywhere. `email.html.j2:141/147`
   does carry both, which is correct for the email profile. The archive page reuses
   `index.html.j2` with the same `events_json` embedded (`web.py:67-69`), so it inherits the
   web profile. (See BLOCKER-2 for the `breakdown` question, which is a spec conflict, not an
   inclusion-list defect.)
8. **SPEC §6.5 — Port.hu's three mandatory rules.** CONFORMANT.
   *Gallery unreachable:* `grep -rn "gallery" src/` → no hits. `_parse_record`
   (`port_hu.py:90-141`) reads only `id, title, url, eventStart, end, place, address,
   description, thumbnail, type`. The fixture confirms the field exists and is large —
   `has gallery: True, n gallery: 24` — and it is never touched; `image_url` takes
   `thumbnail` only (`:138`).
   *District falls back to the postal code:* `port_hu.py:131` —
   `district_raw=district if isinstance(district, int) else district_from_zip(zip_code)`.
   Verified: `district_from_zip("1113") → 'XI.'`, `("1033") → 'III.'`, and the fixture's
   online-only `zip: 1000` → `None` (guarded by `roman_district`'s 1–23 range,
   `models.py:136-141`).
   *No detail-page request:* `discover` (`port_hu.py:69-77`) yields only `listing.urls`
   (currently an intentionally empty placeholder), and `parse` (`:79-88`) performs no I/O —
   it has no fetcher reference at all. One request per listing page, zero per event.
9. **SPEC §5.3 — a missing `PROFILE_YAML` does not fail the run.** CONFORMANT.
   `load_config` (`config.py:215-232`): a `None`/empty/whitespace profile logs
   `profile_missing` and continues with `profile = {}` (`:221-226`); no raise. Every
   profile-sourced field on `Config` has a neutral default (`config.py:159-162`):
   `recipient_email=None`, `home=None`, `scoring=ScoringConfig()`, `filters=FiltersConfig()`.
   Downstream guards are all present: `_same_district_bonus` and `_distance_penalty` return
   `0.0` when `proximity`/`home` is `None` (`score.py:113-125`), `_distance_km` returns `None`
   (`normalize.py:150-154`), `filters.categories=None` disables the category gate
   (`filter.py:58`), `min_score=0`, and `SmtpDeliverer` logs `smtp_skipped` and returns rather
   than raising when there is no recipient (`smtp.py:26-28`). Category weights neutralise to
   `1.0` via `_with_neutral_category_weights` (`config.py:203-212`). Executed against the real
   `config.yaml` with `profile_yaml=None` — loads cleanly.
   Two related §5.3 requirements also hold: unknown keys raise (every model inherits
   `_Section` with `extra="forbid"`, `config.py:20-21`), and the privacy split is clean —
   reading `config.yaml` end to end, it contains no `scoring`, `home`, `recipient_email` or
   `filters` key.

## Unknown

- **Item 10 — SPEC §7.5, the LLM layer.** Partly verified, partly **UNKNOWN because the code
  is unreachable.**
  What *is* verified in isolation: the `enabled` guard is the first statement of
  `GeminiCategorizer.categorize` and returns the input list untouched
  (`gemini.py:131-133`); no client is constructed before it (`_RealGeminiClient` is built
  lazily inside `_run_batches`, `gemini.py:174`, and even its `google.genai` import is
  deferred to `__init__`, `:38`); quota and every other client error are swallowed into the
  rule-based result and the run continues (`gemini.py:180-189`, `except Exception` → log →
  `return`, leaving already-computed rule categories in place); `on_quota_error` is pinned to
  `Literal["fallback_to_rules"]` so it cannot be configured off (`config.py:80`); and
  eligibility matches §7.5 (`egyeb` **and** description > 200 chars, `gemini.py:54-59`).
  What is unknown: **`GeminiCategorizer` is never wired into the pipeline.**
  `grep -rn "GeminiCategorizer\|digest.llm" src/` returns hits only inside `src/digest/llm/`
  itself and in `tests/`; `cli.py:23-24` imports the rule categorizer directly. So
  "unreachable when `llm.enabled` is false" passes *vacuously* — it is unreachable when
  enabled, too. Flipping `llm.enabled: true` in `config.yaml` would change nothing.
  To decide whether this is a defect or correct milestone sequencing I would need: the
  intended M7 scope boundary (SPEC §16 places the Gemini layer in M7, and `prompt-packages.md`
  would say whether the wiring package has been run yet) — i.e. is `llm/` shipped-but-unwired
  by design, or was the wiring step missed? Everything needed to answer that is outside
  `src/digest/`.
- **§7.2's `ambiguous_dedup` → LLM hook.** §7.2 says the 80–88 band "ez az opcionális LLM hook
  bemenete". The band is logged (`dedup.py:78-89`) but nothing consumes it — no structure is
  returned and `llm.only_for` includes `ambiguous_dedup` (`config.py:81`) with no reader.
  Whether the hook is expected to exist at this milestone is the same M7 question as above.
- **§6.5's open items.** The Port.hu listing endpoint is still §17's open question 1;
  `sources/port-hu.yaml` carries an intentionally empty `listing.urls` and documents why.
  Conformance of the real fetch path therefore cannot be assessed — only the fixture path can.
  Same for the `type` dictionary (§17 question 2): the fixture contains only `"concert"`, so
  `native_types` coverage for other categories is untested against reality.
- **§9.1 archive rendering fidelity.** `index.html.j2` is 48 KB of design-derived markup with
  inline JS. I verified it contains no `<img>` and no description field, and that the embedded
  archive payload is the same web-profile JSON. I did **not** audit the JS for other
  behaviours. Deciding whether the template still matches the design artefact needs the
  Claude Design link, which §18 leaves blank (`_<ide illeszd be>_`).
