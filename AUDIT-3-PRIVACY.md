# Audit 3 — Public-repo privacy and secret hygiene

Scope: SPEC.md §9.0, §11, §12; `config.yaml`, `sources/*.yaml`, `.github/workflows/`,
`.gitignore`; `src/digest/render/`, `src/digest/config.py`. Report only — no source file was
modified. Baseline: `git rev-parse --short HEAD` = `4f946ec` ("fix(audit): resolve blocker
findings from audit 2"), working tree as found before this audit started:
`M claude-design-brief.md`, `M prompt-packages.md`, `?? DEPLOY.md`, `?? audit-prompts.md` —
none of these four are touched by this audit; the only change this audit makes is adding this
file. Test baseline: `./.venv/bin/python -m pytest -q` → **321 passed in 1.01s**.

**Premise used throughout:** GitHub Actions workflow run logs on a *public* repository are
world-readable without authentication, by default, forever (unless the operator later restricts
it). This is the mechanism that makes the two BLOCKER findings below real: they are not about
what's committed to git, they're about what a normal, successful (or one-typo-away-from-normal)
workflow run *prints*.

## Summary

**2 BLOCKER, 3 MAJOR, 2 MINOR, 1 NIT.** The static split the project is built around —
`config.yaml`/`sources/*.yaml` public, `scoring`/`home`/`recipient_email`/`filters` in
`PROFILE_YAML` — holds up under direct inspection: `config.yaml` contains none of the four
guarded keys, no email address, no home district, no personal keyword boost; `test_config_privacy.py`
is real and passing; git history has no deleted secret files and no leaked passwords/API keys in
file content; the workflow passes every secret via `env:`, never as a CLI argument, with no
`echo`/`set -x`; `respect_robots_txt` is a single global flag with no per-source bypass; and the
rendered `site/events.json` and `site/*.html`, verified against actual produced output (not just
unit tests), contain none of `description`/`image`/`breakdown` and every event carries a
non-empty outbound `url`.

The two BLOCKERs are both about the same failure mode reached two different ways: **the
recipient's real email address and other `PROFILE_YAML` field values are printed, in clear text,
to stdout — which becomes the public Actions log.** One path fires on *every single successful
run* (`smtp.py`'s own `email_sent` log line). The other fires on a plausible, not contrived,
operator mistake — a copy-paste indentation error or a mistyped key in the `PROFILE_YAML`
secret textarea, both of which the project's own `DEPLOY.md` walks the operator into risking —
and it is not limited to the email address: depending on which field is malformed, it can print
`home.district`, a `scoring` weight, or a raw `keyword_boosts` line. Neither path is caught by
any exception handler; both were reproduced directly against the real code, not inferred.

Three MAJOR findings are less mechanical but still real for an adversary who, per this audit's
brief, reads every commit: every commit's author identity is a real Gmail address, permanently,
starting from the first commit; the honest, reachable User-Agent CLAUDE.md rule 10 calls "the
main reason a site leaves you alone" is currently a literal unfilled `<user>/<repo>` placeholder,
with no step in `DEPLOY.md` telling the operator to fix it; and the very fact that the public
site is a *filtered* view of otherwise-public source listings gives a motivated adversary who
also scrapes those same public sources (port.hu, bigcitylife, …) a way to infer parts of
`filters` — which category/price/keyword bar an event had to clear to survive — from what is
conspicuously absent from `site/events.json`, without any code defect being involved.

**Verdict: not safe to flip public yet.** Both BLOCKERs are one-line fixes (stop logging the
raw recipient address; wrap `load_config` so a `ConfigError`/`ValidationError` is summarized,
not dumped, before it reaches the console) but neither is fixed today, and DEPLOY.md's own step
0 already conditions going public on this audit's BLOCKERs being clear — so this finding is
exactly what that gate exists to catch.

---

## Findings

### [BLOCKER] `smtp.py` logs the real recipient email address on every successful send

- **Spec reference:** SPEC.md §12 (`recipient_email` is `PROFILE_YAML`-only, "personal use");
  CLAUDE.md rule 5 ("Semmi személyes adat a publikus configba")
- **Evidence:**
  - `src/digest/delivery/smtp.py:48`
    ```python
    log.info("email_sent", to=config.recipient_email, subject=subject)
    ```
    This line runs unconditionally at the end of every successful `send()`, i.e. every normal
    day's run once delivery is configured — not an edge case.
  - No `structlog.configure(...)` call anywhere in `src/` (`grep -rn "structlog.configure" src/`
    → no hits), so structlog's default renders to **stdout**. Reproduced directly:
    ```
    $ python3 -c "
    import structlog
    log = structlog.get_logger()
    log.info('email_sent', to='realuser@gmail.com', subject='test')
    " 1>/tmp/stdout_out.txt 2>/tmp/stderr_out.txt
    STDOUT: ... email_sent ... subject=test to=realuser@gmail.com
    STDERR: (empty)
    ```
  - `.github/workflows/digest.yml`'s `Run digest` step has no output redirection or filtering —
    whatever `digest run` prints to stdout lands verbatim in the step's log, which is the public
    Actions log on a public repo.
- **What the spec requires:** §12's whole config split exists so that `recipient_email` (and
  the other three profile keys) never appear anywhere in the public repo's reachable surface —
  file content *or* generated output.
- **What the code does:** Prints the address in plain text on every send, with no redaction.
- **Impact — stated with the honest caveat, not overstated:** whether this specific string
  survives into a *readable* log line also depends on a coincidence the project neither controls
  nor documents: GitHub's secret masking replaces exact occurrences of each *registered* secret
  value. `recipient_email` lives **inside** the multi-line `PROFILE_YAML` secret and is never
  registered as its own secret, so `to=<address>` matches no masking token registered from
  `PROFILE_YAML` unless the printed substring happens to reproduce a full line of that secret
  verbatim (it doesn't here — the log line has no surrounding `recipient_email:` key or quotes).
  The one case where masking *would* catch it is if the operator sends the digest to themselves,
  so `recipient_email == SMTP_USER` — and `SMTP_USER` *is* registered as its own secret. Nothing
  in `DEPLOY.md` requires or suggests that the two addresses match; a different-recipient setup
  (the more common real case: forwarding to a partner, a second inbox, a Telegram-adjacent
  email, etc.) gets no such accidental cover. **Grade: BLOCKER regardless** — the code
  unconditionally writes a personal address to a channel that is public, by design, once the
  repo is public; relying on an unrelated coincidence for redaction is not a safeguard, and the
  fix (drop the `to=` field, or replace it with a boolean/domain-only signal) is a one-line
  change with no design cost.

---

### [BLOCKER] An uncaught config error can print raw `PROFILE_YAML` field values to the public Actions log

- **Spec reference:** SPEC.md §5.3 (`load_config` validates onto Pydantic, "ismeretlen kulcs →
  hiba"); §12
- **Evidence — two independent triggers, both reproduced directly against `load_config`:**
  1. **A malformed key or type** (e.g. a typo'd `recipient_email` field name — an easy mistake
     to make hand-editing the secret) hits Pydantic's `extra="forbid"`/type validation, and
     `ValidationError.__str__()` includes the raw offending value by default:
     ```
     $ PROFILE_YAML='
     recipient_emailx: "realsecret.person@gmail.com"
     home:
       district: "XI."
       lat: 47.47
       lon: 19.05
     ' digest run
     ...
     ValidationError: 1 validation error for Config
     recipient_emailx
       Extra inputs are not permitted [type=extra_forbidden,
     input_value='realsecret.person@gmail.com', input_type=str]
     ```
     Confirmed end-to-end through the actual `digest run` CLI entry point (Typer 0.27.1's
     `pretty_exceptions_show_locals` defaults to `False` — so full local-variable dumps are not
     also exposed — but the exception's own message, containing `input_value`, still is).
  2. **A YAML syntax error** (indentation damage from copy-pasting a multi-line secret into a
     GitHub textarea — the exact scenario `DEPLOY.md` §4 walks the operator through: "másold be,
     ahogy van") hits `_parse_yaml_mapping`'s `except yaml.YAMLError`
     (`src/digest/config.py:166-169`), and PyYAML's error message embeds a verbatim snippet of
     the surrounding source lines:
     ```python
     # config.py:168-169
     raise ConfigError(f"{origin} is not valid YAML: {exc}") from exc
     ```
     Reproduced with a broken profile:
     ```
     ConfigError: PROFILE_YAML is not valid YAML: while parsing a flow mapping
       in "<unicode string>", line 11, column 19:
           keyword_boosts: { koreai: 3, "craft beer": 2
                           ^
     expected ',' or '}', but got ':'
     ```
     The `keyword_boosts` line is reproduced verbatim, unredacted. Depending on exactly where
     the paste damage lands, this same mechanism can equally surface `home.district`,
     `recipient_email`, or a `scoring` weight line — whichever line sits next to the break.
  - Neither `load_config` (`src/digest/config.py:215-232`) nor its only caller, `_run_real`
    (`src/digest/cli.py:120-128`), nor `run()` (`src/digest/cli.py:81-117`) wraps this call in a
    `try/except`. `grep -n "except" src/digest/cli.py` shows exactly two handlers — the
    per-source one inside `_run_sources` (§13's isolation, correctly scoped to fetching) and one
    unrelated `except ValueError` deep in a debug table renderer. Nothing catches a config error.
  - The exception therefore propagates out of the Typer command uncaught, and Typer's default
    "pretty exceptions" renderer (enabled by default, `pretty_exceptions_enable=True`) prints it
    — message and all — to stderr, which is captured by the workflow step's log the same as
    stdout.
- **What the spec requires:** §5.3 says an *unknown key* should raise "not néma átugrás" (a
  loud, not silent, failure) — correct as far as it goes — but neither §5.3 nor §12 anticipates
  that the loud failure's own message is where the leak happens.
- **What the code does:** Validates loudly, exactly as specified, through library defaults that
  were never designed with a secret-bearing input in mind, and lets the result reach the console
  unfiltered.
- **Impact:** Unlike the smtp.py finding, this path is not conditioned on any redaction
  coincidence at all — the printed substring (a bare value or a raw YAML line) does not
  reproduce a registered secret's full-line form, so GitHub's masking has nothing to match. It
  requires an operator mistake to trigger, but it is a *plausible, likely-eventually* mistake
  (hand-editing YAML in a browser textarea, per the project's own deploy instructions), not an
  adversarial one, and a single occurrence permanently publishes whatever profile fragment was
  near the break, in a log that (depending on repo settings) may remain readable indefinitely.
  **Fix:** catch `ConfigError`/`pydantic.ValidationError` at the `run()`/`_run_real` boundary and
  re-raise (or log) a message that never includes `input_value` or the raw YAML snippet —
  e.g. field path and error *type* only.

---

### [MAJOR] Every commit's author identity is a real, personal Gmail address

- **Spec reference:** SPEC.md §12's stated adversary model ("olvasható térkép... arról, hol
  laksz"); this audit's own premise ("an adversary who reads every commit")
- **Evidence:**
  ```
  $ git log --all --pretty=format:"%an <%ae>" | sort -u
  demetermate <demetermate@gmail.com>
  $ git log --all -p -- config.yaml | sed -n '1,3p'
  commit f85a8803bf4d52c0833a497f41dfb8858c842f06
  Author: demetermate <demetermate@gmail.com>
  Date:   Sun Aug 16 07:33:10 2026 +0200
  ```
  Every commit in the history (`git log --all`) carries this same author line. The one commit
  that will be made by CI (`.github/workflows/digest.yml:44-45`) correctly uses
  `digest-bot@users.noreply.github.com` — this finding is about the human's own commits, which
  is everything up to and including the initial `feat(core)` commit and every local commit since.
- **What the spec requires:** §12 frames the whole config split around not letting the repo
  become "a readable map of... where you live"; a personal Gmail address tied to a real-looking
  username is the same category of exposure, reached by a different file (`.git/`, not
  `config.yaml`).
- **What the code does:** Nothing — this is a repo-configuration fact, not a code defect, and it
  predates and is independent of the `PROFILE_YAML` split entirely.
- **Impact:** Once public, this address is trivially harvestable by anyone who runs
  `git log`, forever — a standing target for spam/phishing correlated with a real project and
  (via the GitHub account) a real username. It cannot be fixed by a later commit — history is
  public from the moment visibility flips, and deleting/amending afterward does not un-publish
  what was already fetched — so it has to be handled *before* going public. `DEPLOY.md` §0
  already documents exactly this class of problem in general ("egy véletlenül becommittolt
  jelszó akkor is kikerült, ha utána törlöd") and gives the fix path (fresh repo, single initial
  commit, `rm -rf .git`) — but it does not call out the commit-author email specifically as a
  reason to use it, and that clean-reinit path only helps if the *new* initial commit is made
  with a different (e.g. GitHub noreply) `user.email`. `git config user.email` for future local
  commits needs the same fix; `digest-bot`'s address is already correct and needs no change.

---

### [MAJOR] The crawler's contact URL is an unfilled placeholder, not a working link

- **Spec reference:** CLAUDE.md rule 10 ("udvarias crawler... őszinte User-Agent
  elérhetőséggel. Ez utóbbi nem formalitás: ez a legfőbb oka annak, ha egy oldal békén hagy.")
- **Evidence:**
  - `config.yaml:8`:
    ```yaml
    user_agent: "budapest-event-digest/1.0 (+https://github.com/<user>/<repo>)"
    ```
    `<user>` and `<repo>` are template placeholders, not filled in with the real GitHub path.
    This is the value actually sent: `src/digest/fetch/http.py:38` sets
    `headers={"User-Agent": config.fetch.user_agent}` directly from `config.fetch.user_agent`,
    which comes straight from `config.yaml` (not part of `PROFILE_YAML`, so nothing overrides
    it).
  - `git remote -v` on this local repo returns nothing (not yet pushed anywhere), so the correct
    final value isn't fixed in code today — but the placeholder syntax itself (`<user>/<repo>`,
    literal angle brackets) is not a valid URL under any substitution the operator forgets to
    make.
  - `grep -in "user_agent\|<user>\|<repo>" DEPLOY.md` → no hits. No step in `DEPLOY.md` tells the
    operator to replace the placeholder before the first real run.
- **What the spec requires:** CLAUDE.md is explicit that this is not cosmetic — a site operator
  who notices unusual traffic and checks the User-Agent string is the entire mechanism by which
  a polite, identifiable crawler avoids being blocked or reported. A dead link defeats that.
- **What the code does:** Sends the literal placeholder string on every HTTP request to every
  source, including the `robots.txt` fetch itself (`http.py:182`), for as long as the operator
  doesn't manually edit `config.yaml` and no step tells them to.
- **Impact:** Not a data leak, but a real deploy-readiness gap directly named by the project's
  own rules: any site operator who tries to identify or contact the crawler hits a broken URL,
  undermining the "honest, reachable" contract CLAUDE.md rule 10 says is the reason sources stay
  tolerant of the scraper. Cheap to fix (one line, before first real run) but currently silent —
  nothing validates the format or flags the placeholder.

---

### [MAJOR] The public site structurally leaks parts of `filters` by omission, independent of any code defect

- **Spec reference:** SPEC.md §12 (`filters` is `PROFILE_YAML`-only); §7.6/§7.7 pipeline
  ordering
- **Evidence:**
  - Pipeline order in `_run_pipeline` (`src/digest/cli.py:172-186`): `filter_events` (applies
    `filters.categories`, `filters.max_price_huf`, `filters.blocked_keywords`) and `score`
    (applies `filters.min_score` — see `score.py`, consistent with Audit 1's m2 finding that
    `min_score` is enforced in `score()`, not `filter()`) **both run before** `group()`, and
    both `render_email` and `render_web` (`cli.py:180-186`) consume the *identical* post-filter,
    post-score `events` list. Confirmed: `site/events.json` is therefore already the
    survivor-only view — anything `filters` excluded is invisible on the site, but the
    `score` field of everything that *did* survive is published (`_event_to_json`,
    `render/web.py:135-147`).
  - The sources this project scrapes (`sources/*.yaml`: port.hu, bigcitylife, fidelio,
    programturizmus, welovebudapest) are themselves ordinary public listing sites, independently
    browsable by anyone, not just this pipeline.
  - Consequence, reasoned from the code (not independently executed against live sites, since
    that would require live network access this audit does not take): an adversary who also
    browses those same public source sites can diff their listings against `site/events.json`.
    Every source event whose title/venue/date fuzzy-matches nothing in `events.json` was
    excluded by `filters` — narrowing `filters.categories` (which categories never appear at
    all, beyond what `min_category_score`/`fallback_category` already explain), roughly
    bounding `filters.max_price_huf` (comparing excluded events' listed prices against included
    events' published `price_min`), and giving positive examples of `filters.blocked_keywords`
    (a title containing an otherwise-plausible, on-topic word that never appears in
    `events.json`). Because every surviving event's `score` is public, the adversary can also
    lower-bound `filters.min_score` from the minimum score actually published.
  - This is distinct from — and not fixed by — the AUDIT-1 BLOCKER-2 remediation already in
    `render/web.py`, which strips the `breakdown` field itself. No field here is wrongly
    published; the leak is entirely in what's *absent*, correlated against outside data the
    pipeline does not control.
- **What the spec requires:** §12 places `filters` in the private profile for the same reason as
  `scoring`/`home` — it is part of "a readable map of your taste."
- **What the code does:** Faithfully implements the mandated pipeline order; there is no
  misimplementation to point at.
- **Impact:** Graded MAJOR rather than BLOCKER because it requires a motivated adversary to
  independently scrape the same public sources and do correlation work — it is not a one-command
  disclosure like the two BLOCKERs above — and because `filters.categories` in the SPEC §5.2
  sample is already a large subset of the fully-public `categories` list in `config.yaml`, so
  the marginal information recovered is smaller than for `scoring`/`home`. It is, however, a
  structural property of "publish a filtered public site built from public sources," not a bug
  with a one-line fix — flagged for awareness and a possible future spec decision (in the same
  spirit as AUDIT-1 BLOCKER-2's "this needs a decision, not an edit"), not as something
  `render/web.py` should be changed to suppress today.

---

### [MINOR] `.gitignore` is missing patterns for both generic env files and the operator's own local profile file

- **Spec reference:** CLAUDE.md rule 5; SPEC.md §12
- **Evidence:**
  - `.gitignore` (full contents, 11 lines): covers `__pycache__/`, `*.py[cod]`, `*.egg-info/`,
    `.eggs/`, `build/`, `dist/`, `.venv/`, `venv/`, `.pytest_cache/`, `.ruff_cache/`, `.env`.
    It does **not** cover `*.env` or `.env.*` (only the exact filename `.env`), and it does
    **not** cover any variant of `profile.yaml`.
  - `DEPLOY.md:116`, step 7's own local-testing instructions: `export PROFILE_YAML="$(cat
    ~/profile.yaml)"` — i.e. the project's documented workflow has the operator keep a plaintext
    copy of the *entire private profile* on disk, by convention outside the repo (`~/`), but
    nothing in `.gitignore` would stop a `profile.yaml` (or `my-profile.yml`, etc.) dropped
    *inside* the repo directory — e.g. for convenience while iterating — from being swept up by
    a routine `git add -A`.
  - `git ls-files | grep -E "\.env|__pycache__|\.venv"` → no output: none of the covered
    patterns are currently tracked, so this is a latent gap, not an active leak.
- **What the spec requires:** No explicit `.gitignore` content list in SPEC.md, but CLAUDE.md
  rule 5's "semmi személyes adat a publikus configba" implies the tooling should make the
  accidental version.
- **What the code does:** Covers the tooling/cache noise well; covers exactly one secret-shaped
  filename (`.env`) and no others.
- **Impact:** Low today (nothing is tracked), but this is the cheapest possible improvement in
  this report — add `*.env`, `.env.*`, and a `profile.yaml`/`*profile*.y*ml` pattern (matching
  `DEPLOY.md`'s own recommended local filename) so a future `git add -A` from inside the repo
  can't commit the full private profile in one step.

---

### [MINOR] `test_config_privacy.py` only guards `config.yaml`, not `sources/*.yaml`

- **Spec reference:** SPEC.md §12 (both `config.yaml` and `sources/*.yaml` are listed as public
  repo content); §5.3 ("Kötelező védőteszt")
- **Evidence:**
  - `tests/test_config_privacy.py:18-29` — both tests take a `config_path` fixture.
  - `tests/conftest.py:15-17` — `config_path` fixture resolves to `repo_root / "config.yaml"`
    only. There is a separate `sources_dir` fixture (`conftest.py:20-22`) but
    `test_config_privacy.py` never uses it.
  - Manual check performed for this audit, since the test doesn't do it:
    `grep -inE "district|kerület|@gmail|recipient|home:|lat:|lon:" sources/*.yaml` → no genuine
    hits (the one match, `tárlat: 3` in a `szinhazak.yaml`-adjacent comment, is `lat:` as a
    substring of a Hungarian word, not a coordinate). `sources/szinhazak.yaml`'s comment
    mentioning `blfr4@blog.hu` is blog.hu's own RSS generator string, quoted while explaining why
    that source is a dead end — not a personal address.
  - **Currently no active leak in `sources/*.yaml`** — this finding is about test coverage, not
    a present violation.
- **What the spec requires:** §5.3's guard test exists specifically so a mistyped commit can't
  publish the profile; §12 treats `sources/*.yaml` as part of the same public surface as
  `config.yaml`.
- **What the code does:** Guards one of the two public config surfaces the spec names.
- **Impact:** If a future edit to any `sources/*.yaml` (there are six, and more are anticipated
  per SPEC §6.6) ever introduced a personal keyword boost, a home-adjacent venue filter, or an
  email address, `test_config_privacy.py` would stay green while publishing it. Cheap fix:
  parametrize both existing tests over `config.yaml` plus every file in `sources/*.yaml`.

---

### [NIT] The public site's write-UI placeholder text uses the real GitHub username as its example value

- **Spec reference:** none directly (this is adjacent to, not part of, the eight numbered
  checks) — noted per this task's "report anything you're unsure about"
- **Evidence:** `src/digest/render/templates/index.html.j2:287`:
  ```html
  <input class="input" id="ghOwner" type="text" autocomplete="off" placeholder="pl. demetermate">
  ```
- **What the code does:** Uses the developer's real GitHub username as the example text for a
  "GitHub owner" input field in the (package-14) browser write UI, which ships to the public
  Pages site.
- **Impact:** Judged not meaningfully additive: this is a single-operator personal tool hosted
  on that operator's own public GitHub account, so the username is already exposed by the repo's
  URL itself (`github.com/<username>/<repo>`) the moment it goes public — the placeholder text
  doesn't disclose anything the hosting arrangement doesn't already. Noted for completeness, not
  actioned.

---

## Checked and conformant

1. **Profile leakage into `config.yaml`.** CONFORMANT.
   ```
   $ python -c "import yaml;d=yaml.safe_load(open('config.yaml'));print([k for k in d if k in ('scoring','home','recipient_email','filters')])"
   []
   ```
   Manual grep for a home district, an email address, or a personal keyword boost across
   `config.yaml` and all six `sources/*.yaml` files: no genuine hits (see the MINOR-2 write-up
   above for the two false positives found and ruled out).

2. **Secrets in git history.** CONFORMANT — no deleted-then-still-public secret content found.
   ```
   $ git log --all -p -- config.yaml | grep -inE "password|secret|api[_-]?key|@gmail|smtp"
   ```
   → only the commit-author line (see the MAJOR finding above, which is about author *identity*,
   not a secret in file content) and one doc line mentioning "smtp" as a delivery type name.
   ```
   $ git log --all --oneline -- '*.env' '*.env.*' 'secrets*'
   ```
   → no output; no such file was ever committed.
   ```
   $ git log --all --diff-filter=D --name-only --pretty=format: | sort -u | grep -v '^$'
   ```
   → no output; **no file has ever been deleted from this repository's history** — nothing to
   worry about resurfacing via `git log -p` on a path no longer in the tree.
   Broader sanity sweep across the *entire* history (not just `config.yaml`), for API-key-shaped
   strings and real-looking recipient addresses:
   `git log --all -p | grep -inE "recipient_email:|smtp_password|api_key.*=.*['\"][a-zA-Z0-9_-]{15,}|AIza[0-9A-Za-z_-]{20,}"`
   → every hit is either a code identifier (`config.recipient_email`), a documentation line
   naming an env var, a test's fake literal (`"app-password"`), or a placeholder example
   (`recipient_email: "someone@example.com"` / `"te@example.com"`). No real credential anywhere
   in history.

3. **`.gitignore` completeness — the tracked-files half.** CONFORMANT.
   ```
   $ git ls-files | grep -E "\.env|__pycache__|\.venv"
   ```
   → no output; none of `.env`, `__pycache__/`, or `.venv/` content is tracked. (The *coverage*
   half — whether the patterns are broad enough — is the MINOR-1 finding above.)

4. **PROFILE_YAML in logs — everywhere except the two BLOCKERs above.** Swept every
   `log.info/warning/error/debug` call across `src/digest/` for anything touching
   config/profile/recipient/home/scoring:
   ```
   grep -rn "log\.\(info\|warning\|error\|debug\)(" src/digest/ | grep -iE "config|profile|recipient|home|scoring"
   ```
   Every other hit is safe: `sources_dir_missing` (a path), `profile_missing` (a fixed string,
   no secret value), `source_disabled_in_config` (a source id), `dropped_below_min_score`
   (a count and the numeric threshold — `min_score` itself is a small integer with low
   sensitivity on its own, and this one wasn't flagged separately given MAJOR-4 already covers
   `filters` inference through a stronger channel), `smtp_skipped` (a fixed reason string, no
   address), and `llm_max_calls_reached` (a config int, not a secret). The `llm_call_failed`
   line logs `str(exc)` from the Gemini client; `GEMINI_API_KEY` is passed to `genai.Client` as
   `api_key=` (`src/digest/llm/gemini.py:40`), which SDKs of this shape typically send as a
   header rather than embedding in the request line an exception would echo — not independently
   verified against the SDK's internals, and moot today regardless since Audit 1 already
   established `GeminiCategorizer` is never wired into `digest run`.

5. **Web output purity — verified against actually-produced files, not just the unit tests.**
   Ran the real pipeline against the committed `tests/fixtures/port_hu_list.json` fixture,
   through every stage (`normalize → dedup → recurrence → categorize → filter → score → group →
   render_web → write_site`) into a scratch directory, then inspected the output directly:
   - `set(payload.keys()) for every record in events.json` → exactly
     `{'categories','district','group_size','id','is_free','price_min','score','start','title','url','venue'}`
     — no `description`, `image`/`image_url`, or `breakdown` in the key union across all
     records (not just a substring grep — an actual key-set diff).
   - `grep -inoE '<img[^>]*>' site/index.html site/status.html site/archive/*.html` → no output;
     **zero** `<img>` tags anywhere in the generated HTML, so there is nothing to check for
     "non-local host" — the stronger property (no image tags at all) already holds.
   - `all(bool(r["url"]) for r in payload["events"])` → `True`; every event carries a non-empty
     outbound link.
   - Also ran `pytest tests/test_web_render.py -v` (14 tests, the file's own suite covering this
     exact contract plus the `</script>`-escaping and archive/index divergence cases) → all pass.

6. **Workflow secret exposure.** CONFORMANT. `.github/workflows/digest.yml:34-39` passes
   `PROFILE_YAML`, `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `GEMINI_API_KEY` exclusively via
   the step's `env:` block — none appears as a command-line argument anywhere in the file. No
   `echo` and no `set -x` (or any other shell tracing flag) anywhere in the workflow — checked
   the full 56-line file directly, not just grepped.

8. **Third-party terms — robots.txt half.** CONFORMANT. `respect_robots_txt` is a single
   boolean on the shared `FetchConfig` (`src/digest/config.py:35`, default `True`, and set to
   `true` in `config.yaml:13`), checked once per fetch at `http.py:67`. `grep -rn
   "respect_robots_txt\|robots" src/digest/` shows no per-source override path — `sources/*.yaml`
   descriptors carry no such key, and `Source`/`declarative.py` never reads one. A per-source
   bypass would require a new config field that does not exist today. (The User-Agent half of
   this check is the MAJOR finding above.)

---

## Unknown

- **Whether the printed values in the two BLOCKER paths would actually reach a *human reader* on
  a public repo, versus being caught by some GitHub-side mitigation not modeled here.** I
  reproduced both leaks against the real code and reasoned through GitHub's documented
  line-based masking of multi-line secrets, concluding neither printed substring matches a
  registered token — but I have not run this against an actual GitHub Actions execution, and
  GitHub's masking behavior for structured/multi-line secrets is not something this audit could
  verify empirically without live infrastructure. Recommend the operator not rely on this
  reasoning either way and just fix the two code paths.
- **Exact final Pages URL.** SPEC §12 suggests a random path segment "ha nem szeretnéd, hogy
  találomra megtalálják." I initially read `site.base_path` (`"/budapest-event-digest"`,
  `config.yaml:95`) as the thing that would need randomizing, but traced its only use to
  `render/email.py:122`, where it is solely the *email's* archive-link fallback (never actually
  overridden — `render_email` is called without `archive_url` at either call site in `cli.py`) —
  it does not determine the live Pages URL at all. The actual deployed URL for a GitHub Pages
  *project* site is `https://<owner>.github.io/<repo>/`, driven by the repository's own name (or
  a custom domain via `CNAME`), which is not fixed in this local checkout — `git remote -v`
  returns nothing, and no `CNAME` file exists in `site/` or the repo root. So: **no random
  segment is in use anywhere today**, and whatever repo name the operator picks (SPEC's own
  examples and `config.yaml`'s `base_path` both suggest `budapest-event-digest`, which is not
  random) will be the guessable, discoverable URL unless changed at repo-creation time — a
  decision this audit cannot make on the operator's behalf, only flag as unaddressed. Separately,
  since `base_path` is a bare path with no scheme/host and is never overridden with a real
  archive URL, the email's "archive" link is presently not a working absolute URL — a latent
  correctness bug, not a privacy issue, noted here rather than as its own finding since it's
  outside this audit's brief.
- **Whether `filters`/`min_score` values are meaningfully recoverable in practice (MAJOR-4)**,
  versus just theoretically — this depends on how distinctive Budapest event listings are and
  how much manual correlation work an adversary would actually do; I could not test this without
  independently scraping the live source sites, which is out of scope for a report-only,
  no-network audit. Flagged at MAJOR on the strength of the mechanism being real and
  spec-relevant, not on a demonstrated live exploit.

---

## Pre-public checklist

A yes/no list the operator must be able to answer **YES** to before flipping the repo public:

1. Has the `smtp.py` `email_sent` log line been changed to stop printing `config.recipient_email`
   (BLOCKER-1)?
2. Does `run()`/`_run_real()` now catch `ConfigError`/`pydantic.ValidationError` from
   `load_config` and log/print only a redacted summary — never `input_value` or a raw YAML
   snippet (BLOCKER-2)?
3. Has the git history's author identity been dealt with — either a clean re-init per
   `DEPLOY.md` §0 with a non-personal `user.email` for all future human commits, or a conscious
   decision that a real Gmail address permanently attached to every commit is acceptable
   (MAJOR-1)?
4. Has `config.yaml`'s `user_agent` placeholder (`<user>/<repo>`) been replaced with the real,
   working repository URL (MAJOR-2)?
5. Has a decision been made about `filters` being partially inferable from `site/events.json`
   by diffing against the public source sites — accept it (like AUDIT-1's resolution for
   `scoring`/`breakdown` was a spec decision, not a code fix), or restrict it (MAJOR-3)?
6. Has `.gitignore` been extended to cover `*.env`, `.env.*`, and a profile-file pattern, and
   has `test_config_privacy.py` been extended to cover `sources/*.yaml` (MINOR-1, MINOR-2)?
7. Has the repo name (or a `CNAME`) been chosen deliberately, with the operator's own informed
   choice about discoverability — not left as the default, guessable `budapest-event-digest`,
   if that matters to them (Unknown/check 7)?
8. Has a first real `digest run --dry` and a first real scheduled run's Actions log actually been
   read end-to-end by a human, once, specifically looking for anything that looks like a
   personal value — not just relying on this report?
