# Promptcsomagok — Claude Code

Minden csomag egy függőleges szelet: önmagában lefut, legfeljebb 3-5 fájlt érint, saját
futtatható teszttel zárul, egy commit. **A review a csomag után azonnal történjen**, ne
halmozódjon a végére. Ha egy csomag review-ja 15 percnél többe kerülne, vágd ketté.

A "NEM része" blokk a legfontosabb sor mindegyikben — enélkül az agent megírja a következő
három mérföldkövet is.

Előfeltétel: a `SPEC.md` és a `CLAUDE.md` a repo gyökerében van, és a Claude Code látja őket.

---

## M0.1 — Váz és adatmodell

```
## Kontextus
Új, üres repo. Olvasd el a SPEC.md §3 (repo struktúra), §4 (adatmodell), §15 (tech stack)
szakaszokat és a CLAUDE.md-t.

## Feladat
Hozd létre a projekt vázát: pyproject.toml, a §3-ban leírt könyvtárstruktúra üres
__init__.py fájlokkal, és a models.py teljes tartalmát a §4 szerint.

## Elfogadási kritériumok
- [ ] `pip install -e .` sikeres
- [ ] `digest --help` fut (Typer app, üres parancsokkal)
- [ ] RawEvent és Event pydantic v2 modellek a §4 szerint, teljes type hinttel
- [ ] make_event_id() a §4.1 szerint, normalize_title/normalize_venue segédfüggvényekkel
- [ ] ruff check tiszta

## Teszt
tests/test_models.py
- make_event_id determinisztikus és stabil
- normalize_title levágja: "(Budapest)", "(HU)", " | A38", " - élő koncert"
- normalize_title ékezetet távolít és kisbetűsít
Futtatás: pytest tests/test_models.py -v

## NEM része
Fetch réteg, pipeline, config betöltés, bármely forrás. Csak a váz és a modellek.

## Commit
feat(core): project skeleton and data models
```

---

## M0.2 — Config betöltés és privacy-védőteszt

```
## Kontextus
SPEC.md §5 (konfiguráció) és §12 (configszétvágás). A models.py már létezik.

## Feladat
Írd meg a config.py-t: config.yaml + sources/*.yaml + PROFILE_YAML secret betöltése,
mély merge, pydantic validáció. Készítsd el a config.yaml-t a §5.1 teljes tartalmával.

## Elfogadási kritériumok
- [ ] load_config() a §5.3 szignatúrája szerint
- [ ] mély merge: a profil felülírja a configot, lista felülír, dict rekurzívan merge-el
- [ ] ismeretlen kulcs -> ValidationError, nem néma átugrás
- [ ] hiányzó PROFILE_YAML esetén semleges alapértelmezés, NEM hiba
- [ ] config.yaml a repóban, a §5.1 szerint kitöltve

## Teszt
tests/test_config.py és tests/test_config_privacy.py
- merge felülír helyesen
- ismeretlen kulcs hibát dob
- profil nélkül is betölt
- test_config_privacy: a config.yaml NEM tartalmaz scoring/home/recipient_email/filters kulcsot
Futtatás: pytest tests/test_config.py tests/test_config_privacy.py -v

## NEM része
Fetch, pipeline, források. A sources/*.yaml fájlokat még ne hozd létre.

## Commit
feat(config): config loading with profile merge and privacy guard
```

---

## M0.3 — Fetch réteg

```
## Kontextus
SPEC.md §6.4 (fetch réteg). A config.py már létezik.

## Feladat
Írd meg a fetch/base.py, fetch/http.py és fetch/api.py modulokat.

## Elfogadási kritériumok
- [ ] FetchTask és FetchResult a §6.4 szerint
- [ ] httpx.Client, közös User-Agent a configból
- [ ] forrásonkénti rate limit (késleltetés a kérések között)
- [ ] retry exponenciális backoff-fal, CSAK 5xx és hálózati hibára
- [ ] 429 esetén NEM retryolunk azonnal: Retry-After betartása, vagy FetchError
- [ ] If-None-Match küldése, ha kaptunk etaget; 304 -> from_cache=True
- [ ] robots.txt ellenőrzés forrásonként egyszer, futásra cache-elve

## Teszt
tests/test_fetch.py, respx mockkal
- retry 500-ra megtörténik, 404-re nem
- 429 + Retry-After tiszteletben tartva
- 304 -> from_cache
- rate limit késleltet (monkeypatchelt time.sleep hívásszám)
Futtatás: pytest tests/test_fetch.py -v

## NEM része
Bármely konkrét forrás, parseolás, pipeline.

## Commit
feat(fetch): http and api fetchers with retry, rate limit and etag
```

---

## M0.4 — Port.hu plugin

```
## Kontextus
SPEC.md §6.2 (Source protokoll), §6.5 (Port.hu igazolt leképezés).
A tests/fixtures/port_hu_list.json fájlba előre bemásoltam egy VALÓS választ — ezt használd,
ne gyárts szintetikusat.

## Feladat
Írd meg a sources/registry.py-t (egyelőre csak plugin-felfedezéssel) és a
sources/plugins/port_hu.py-t a §6.5 leképezése szerint.

## Elfogadási kritériumok
- [ ] Source protokoll a §6.2 szerint
- [ ] a válasz {"event-<id>": {...}} alakú objektum, végig kell iterálni az értékeken
- [ ] a `gallery` tömb SOHA nem kerül a RawEvent-be
- [ ] description: html.unescape()
- [ ] district: address.district, ha None akkor az irányítószámból (1113 -> "XI.", 1033 -> "III.")
- [ ] url: relatív -> https://port.hu prefix
- [ ] url_category: az url 2. szegmense
- [ ] lat/lon: address.gps.geoPoint
- [ ] az `end` display-string parseolása a §6.5 év-szabálya szerint
- [ ] `digest fetch port-hu --fixture <path>` kiírja a talált eseményeket

## Teszt
tests/test_source_port_hu.py a valós fixture-rel
- a fixture minden rekordja RawEvent-té alakul, vagy naplózott okkal kimarad
- gallery nincs a kimenetben
- 1113 -> "XI.", 1033 -> "III."
- entitáskódolt leírás feloldódik
Futtatás: pytest tests/test_source_port_hu.py -v

## NEM része
Hálózati hívás (a listázó URL még nyitott kérdés, §17.1), más forrás, pipeline.

## Commit
feat(sources): port.hu plugin with verified field mapping
```

---

## M1.1 — Normalizálás

```
## Kontextus
SPEC.md §7.1. A RawEvent-et már elő tudjuk állítani a Port.hu pluginból.

## Feladat
Írd meg a pipeline/normalize.py-t: RawEvent lista -> Event lista.

## Elfogadási kritériumok
- [ ] dátum parseolás: ISO, "2026. 08. 14. 19:00", ISO 8601 datetime attribútum
- [ ] minden datetime tz-aware Europe/Budapest
- [ ] ismeretlen dátumformátum -> rekord eldobva + WARNING, nem kivétel
- [ ] ár parseolás: "3 500 Ft", "ingyenes", "2000-4500 Ft"
- [ ] effective_date a §7.7 hajnali eltolással
- [ ] distance_km haversine-nal, ha van home koordináta és event koordináta
- [ ] horizonton kívüli és múltbeli események kizárva

## Teszt
tests/test_normalize.py
- mind a három dátumformátum
- "ingyenes" -> is_free=True, price_min=0
- 02:00-s esemény effective_date-je az előző nap
- rossz dátum -> kimarad, nem crashel
Futtatás: pytest tests/test_normalize.py -v

## NEM része
Dedup, kategorizálás, pontozás.

## Commit
feat(pipeline): normalize raw events into canonical model
```

---

## M1.2 — Deduplikálás

```
## Kontextus
SPEC.md §7.2. A normalize.py már létezik.

## Feladat
Írd meg a pipeline/dedup.py-t a három szintű algoritmussal.

## Elfogadási kritériumok
- [ ] exact (id), strong (normalizált URL), fuzzy (§7.2 hármas feltétele)
- [ ] URL normalizálás: query string, UTM, fragment levágva
- [ ] merge a forrás priority sorrendjében, mezőnkénti szabályokkal
- [ ] a 80-88 közötti fuzzy sáv NEM merge-el, de ambiguous_dedup jelöléssel naplózódik
- [ ] minden merge döntés a futásnaplóba kerül

## Teszt
tests/test_dedup.py kézzel írt Event listákkal
- "Sub Focus" és "Sub Focus (UK)" azonos időpont+helyszín -> merge
- azonos cím, 3 óra eltérés -> NEM merge
- azonos cím+idő, eltérő helyszín -> NEM merge
- az egyik venue None -> merge
- a hosszabb description nyer
Futtatás: pytest tests/test_dedup.py -v

## NEM része
LLM-alapú dedup, pontozás.

## Commit
feat(pipeline): three-level deduplication with fuzzy title matching
```

---

## M2.1 — Kategorizálás

```
## Kontextus
SPEC.md §7.5 és §5.1 (a categories blokk).

## Feladat
pipeline/categorize.py, szabályalapú, négy jellel (keywords, venue_prior, url_patterns,
native_types). Plusz a `digest categorize --explain <id>` parancs.

## Elfogadási kritériumok
- [ ] native_types egyezés +4 (erős jel)
- [ ] a legmagasabb pontszámú kategória a primary_category
- [ ] minden min_category_score fölötti bekerül a categories listába
- [ ] küszöb alatt fallback_category
- [ ] --explain kiírja, melyik jel hány pontot adott

## Teszt
tests/test_categorize.py
- Port.hu type="concert" -> koncert
- "Társasjáték est a Red & Blackben" -> tarsasjatek (keyword + venue_prior)
- semleges cím és leírás -> egyeb
Futtatás: pytest tests/test_categorize.py -v

## NEM része
Gemini réteg (az M7). Csak a RuleCategorizer és a protokoll.

## Commit
feat(pipeline): rule-based categorization with explain command
```

---

## M2.2 — Pontozás, szűrés, sorozatkezelés

```
## Kontextus
SPEC.md §7.3 (recurrence), §7.6 (filter), §7.7 (score).

## Feladat
pipeline/recurrence.py, pipeline/filter.py, pipeline/score.py.

## Elfogadási kritériumok
- [ ] recurrence: end - start > series_threshold_days -> is_series=True
- [ ] filter: mind a hat kizárási ok a §7.6-ból
- [ ] score: a §7.7 képlete pontosan, minden tag a score_breakdown-ba
- [ ] `digest explain <id>` kiírja a bontást

## Teszt
tests/test_recurrence.py és tests/test_score.py
- 2026-05-06 -> 2026-09-30 rekord -> is_series=True
- 2026-08-14 -> 2026-08-21 -> is_series=False
- péntek 02:00-s esemény PÉNTEKI weekday_weight-et kap (nem szombatit)
- ingyenes esemény megkapja a free_bonus-t
- 10 km-re lévő esemény distance penaltyt kap
Futtatás: pytest tests/test_recurrence.py tests/test_score.py -v

## NEM része
Csoportosítás (következő csomag), ledger.

## Commit
feat(pipeline): recurrence detection, filtering and scoring
```

---

## M2.3 — Fesztivál-összevonás és ledger

```
## Kontextus
SPEC.md §7.4 (group) és §8 (állapot). FONTOS: a group a score UTÁN fut.

## Feladat
pipeline/group.py és state.py.

## Elfogadási kritériumok
- [ ] csoportkulcs (venue_name, effective_date, primary_category)
- [ ] min_group_size fölött egyetlen összevont Event, a §7.4 mezőkitöltésével
- [ ] alatta max_per_venue plafon, pontszám szerint
- [ ] state.json séma a §8.1 szerint, rövidített mezőnevekkel
- [ ] purge(): d < today bejegyzések törlése
- [ ] was_sent(): exact id VAGY (azonos d ÉS token_set_ratio >= 92)
- [ ] run_log: csak az utolsó 30 futás

## Teszt
tests/test_group.py és tests/test_state.py
- 17 azonos helyszínű, azonos napi koncert -> 1 összevont sor, group_size=17
- 3 azonos helyszínű -> NEM vonódik össze (min_group_size=4)
- 6 azonos helyszínű, min_group_size=8 esetén -> max_per_venue=3 érvényesül
- was_sent: "Sub Focus" kiküldve, "Sub Focus (UK)" ugyanaznap -> True
Futtatás: pytest tests/test_group.py tests/test_state.py -v

## NEM része
Renderelés, delivery.

## Commit
feat(pipeline): festival collapsing and sent-ledger state
```

---

## M3 — Email renderelés és kiküldés

```
## Kontextus
SPEC.md §9 (renderelés) és §10 (delivery).
A templates/email.html.j2 alapja a design artefakt HTML-je, amit a
templates/ mappába előre bemásoltam. NE generáld újra a markupot — csak Jinja2
változókat és ciklusokat helyezz el benne.

## Feladat
render/email.py, delivery/base.py, delivery/smtp.py, és az email.txt.j2 sablon.

## Elfogadási kritériumok
- [ ] a HTML sablon szerkezete változatlan, csak Jinja2 kifejezések kerülnek bele
- [ ] plain text alternatíva, multipart/alternative EmailMessage
- [ ] kategóriánként per_category_limit, összesen total_limit
- [ ] "lejáró" szekció a within_days szerint
- [ ] send_when_empty=true esetén 0 találatnál is kimegy, "ma 0 új találat" sorral
- [ ] lábléc: forrás-health összefoglaló
- [ ] `digest run --dry` fájlba írja a HTML-t, nem küld

## Teszt
tests/test_render.py
- a renderelt HTML tartalmazza minden bemeneti esemény címét
- üres lista esetén is renderel, és tartalmazza a "0 új" szöveget
- a plain text változat nem tartalmaz HTML tageket
Futtatás: pytest tests/test_render.py -v

## NEM része
Webes UI renderelés, Pages deploy, Telegram.

## Commit
feat(render): email templates and smtp delivery
```

---

## M4 — GitHub Actions

```
## Kontextus
SPEC.md §11 (workflow) és §12 (biztonság).

## Feladat
.github/workflows/digest.yml a §11 szerint, plusz a `digest run` teljes összekötése
(fetch -> pipeline -> render -> deliver -> state mentés).

## Elfogadási kritériumok
- [ ] a workflow pontosan a §11 YAML-je
- [ ] a PROFILE_YAML secret betöltődik és merge-elődik
- [ ] state/state.json és site/ visszacommittolódik, üres diff esetén nem hasal el
- [ ] per-source try/except: egy forrás hibája nem buktatja a futást
- [ ] structlog összefoglaló a futás végén
- [ ] README.md: setup lépések, secretek listája, első futás

## Teszt
tests/test_run_integration.py
- teljes pipeline lefut fixture forrásokkal, mock SMTP-vel
- egy szándékosan hibázó forrás mellett a futás sikeres marad

## NEM része
Pages deploy (M6), deklaratív YAML motor (M5).

## Commit
feat(ci): github actions workflow with state commit
```

---

## M5 — Deklaratív YAML forrásmotor

```
## Kontextus
SPEC.md §6.3 (teljes mezőkészlet).

## Feladat
sources/declarative.py + a registry kiegészítése + 5 forrás YAML-ja:
welovebudapest, fidelio, bigcitylife, programturizmus, szinhazak.

## Elfogadási kritériumok
- [ ] minden §6.3-ban felsorolt mező és transzform támogatott
- [ ] pagination stop_when_empty működik
- [ ] hiányzó nem-optional mező -> tétel kihagyva + WARNING, futás megy tovább
- [ ] api fetcher esetén `path` (JSONPath) a `selector` helyett
- [ ] minden új forráshoz lementett fixture a tests/fixtures/ alatt

## Teszt
tests/test_declarative_source.py + forrásonként egy teszt fixture-rel
- CSS szelektoros kinyerés
- JSONPath kinyerés
- transzformlánc sorrendben fut
- hiányzó kötelező mező -> a tétel kimarad, a többi megmarad
Futtatás: pytest tests/test_declarative_source.py -v

## NEM része
Meetup plugin (külön csomag), Pages.

## Commit
feat(sources): declarative yaml source engine with five sources
```

---

## M6 — Pages: events.json és olvasó UI

```
## Kontextus
SPEC.md §9 (events.json séma) és §11 (Pages deploy).
Az index.html.j2 alapja a design artefakt — NE generáld újra a markupot.

## Feladat
render/web.py, a Pages deploy lépések a workflowban, status.html.j2, archívum-kezelés.

## Elfogadási kritériumok
- [ ] site/events.json pontosan a §9 sémája szerint
- [ ] site/index.html a design sablonból, Jinja2 behelyettesítéssel
- [ ] site/archive/YYYY-MM-DD.html, archive_keep_days fölött törölve
- [ ] site/status.html: forrás-health tábla
- [ ] a workflow deployol Pages-re

## Teszt
tests/test_web_render.py
- events.json sémavalidáció
- archívum purge a keep_days szerint
Futtatás: pytest tests/test_web_render.py -v

## NEM része
Író UI (M8), Gemini.

## Commit
feat(web): pages output with events.json and reader ui
```

---

## M7 — Gemini réteg

```
## Kontextus
SPEC.md §7.5 (opcionális Gemini réteg) és §5.1 llm blokk.

## Feladat
llm/base.py (Categorizer protokoll), llm/gemini.py.

## Elfogadási kritériumok
- [ ] csak akkor hívódik, ha llm.enabled ÉS az esemény egyeb ÉS a leírás > 200 karakter
- [ ] kötegelés batch_size szerint, NEM eseményenként
- [ ] max_calls_per_run kemény plafon
- [ ] cache content_hash alapján
- [ ] 429 -> on_quota_error szerint NÉMÁN visszaesik a szabályrendszerre, a futás nem hal meg
- [ ] a prompt szigorúan JSON-t kér, a válasz parseolása hibatűrő

## Teszt
tests/test_gemini.py, mockolt klienssel
- 100 uncategorized esemény, batch_size=35 -> pontosan 3 hívás
- 429 -> a szabályalapú eredmény marad, nincs kivétel
- max_calls_per_run túllépése -> a maradék szabályalapú
Futtatás: pytest tests/test_gemini.py -v

## NEM része
Bármi más.

## Commit
feat(llm): optional gemini categorization with quota guards
```
