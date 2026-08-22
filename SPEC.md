# Budapest Event Digest — Technikai specifikáció v1.0

> Ez a dokumentum a projekt egyetlen igazságforrása. A `CLAUDE.md` ennek a desztillátuma
> az agent számára; a `prompt-packages.md` a mérföldkövenkénti feladatcsomagokat tartalmazza.
> Ha a kód és a spec ellentmond, a spec nyer — vagy a specet kell módosítani, előbb.

---

# 1. Cél

Napi egyszer összegyűjti a budapesti programokat több forrásból, normalizálja, deduplikálja,
kategorizálja, szabályok szerint pontozza, és reggel emailben kiküldi az újakat. Emellett
publikál egy böngészhető, szűrhető statikus oldalt. Szerver nélkül, GitHub Actionsön,
nulla forintból.

**Nem cél:** jegyvásárlás, naptár-szinkron, több felhasználó, valós idejű frissítés,
mobilalkalmazás.

**Sikerkritérium:** minden reggel 7 körül megérkezik egy email, amiben legfeljebb ~25 program
van, kategóriák szerint rendezve, és nincs benne olyan, amit korábban már láttál.

---

# 2. Rendszerkép

```
GitHub Actions (cron 04:30 UTC, workflow_dispatch)
  │
  ├─ 1. checkout (config + sources + state)
  ├─ 2. profil-titok betöltése és merge-elése
  ├─ 3. digest run
  │      fetch → normalize → dedup → recurrence → group
  │            → categorize → filter → score → render → deliver
  ├─ 4. state/state.json commit vissza a default branchre
  └─ 5. site/ deploy → GitHub Pages
```

Nincs szerver, nincs adatbázis, nincs böngésző, nincs webframework. A futás egy 3-5 perces
batch job, ami után minden leáll. Az egyetlen perzisztens állapot egy ~120 KB-os JSON a repóban.

---

# 3. Repo struktúra

```
budapest-event-digest/
├── .github/workflows/digest.yml
├── CLAUDE.md                       # agent-konvenciók
├── SPEC.md                         # ez a fájl
├── README.md
├── pyproject.toml
├── config.yaml                     # PUBLIKUS beállítások
├── sources/                        # forrásleírók
│   ├── port-hu.yaml
│   ├── jegy-hu.yaml
│   ├── welovebudapest.yaml
│   ├── fidelio.yaml
│   ├── bigcitylife.yaml
│   ├── szinhazak.yaml
│   ├── kvizestek.yaml
│   ├── redandblack.yaml
│   ├── kedvesidegen.yaml
│   └── meetup.yaml
├── state/
│   └── state.json                  # a ledger, committolva
├── site/                           # Pages kimenet (generált, committolva)
│   ├── index.html
│   ├── events.json
│   ├── status.html
│   └── archive/YYYY-MM-DD.html
├── src/digest/
│   ├── __init__.py
│   ├── cli.py                      # Typer app
│   ├── config.py                   # config betöltés + profil merge + validáció
│   ├── models.py                   # RawEvent, Event, SourceSpec, ...
│   ├── state.py                    # ledger load/save/purge
│   ├── errors.py
│   ├── fetch/
│   │   ├── base.py                 # FetchTask, FetchResult, FetchError
│   │   ├── http.py                 # httpx GET + retry + rate limit + ETag
│   │   └── api.py                  # JSON GET, azonos alap
│   ├── sources/
│   │   ├── registry.py             # auto-discovery: YAML + plugin
│   │   ├── declarative.py          # YAML-vezérelt forrásmotor
│   │   └── plugins/
│   │       ├── port_hu.py
│   │       └── meetup.py
│   ├── pipeline/
│   │   ├── normalize.py
│   │   ├── dedup.py
│   │   ├── recurrence.py
│   │   ├── group.py
│   │   ├── categorize.py
│   │   ├── filter.py
│   │   └── score.py
│   ├── llm/
│   │   ├── base.py                 # Categorizer protokoll
│   │   └── gemini.py
│   ├── render/
│   │   ├── email.py
│   │   ├── web.py
│   │   └── templates/
│   │       ├── email.html.j2
│   │       ├── email.txt.j2
│   │       ├── index.html.j2
│   │       └── status.html.j2
│   └── delivery/
│       ├── base.py                 # Deliverer protokoll
│       ├── smtp.py
│       └── telegram.py
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── port_hu_list.json
    │   ├── meetup_next_data.json
    │   └── ...
    ├── test_config.py
    ├── test_normalize.py
    ├── test_dedup.py
    ├── test_recurrence.py
    ├── test_group.py
    ├── test_categorize.py
    ├── test_score.py
    ├── test_state.py
    ├── test_declarative_source.py
    ├── test_source_port_hu.py
    ├── test_render.py
    └── test_config_privacy.py      # a publikus config nem tartalmaz profilt
```

---

# 4. Adatmodell

`src/digest/models.py`. Pydantic v2, `model_config = ConfigDict(frozen=True)` ahol lehet.

```python
class RawEvent(BaseModel):
    """Amit egy forrás ad, még normalizálás előtt. Minden mező opcionális,
    a normalizáló feladata a hiányzókat kezelni vagy a rekordot eldobni."""
    source_id: str
    source_event_key: str          # a forrás saját azonosítója
    title: str
    url: str
    description: str | None = None
    start_raw: str | None = None
    end_raw: str | None = None
    venue_name: str | None = None
    address_raw: str | None = None
    postal_code: str | None = None
    district_raw: int | str | None = None
    lat: float | None = None
    lon: float | None = None
    price_raw: str | None = None
    image_url: str | None = None
    native_category: str | None = None   # pl. Port.hu "type"
    url_category: str | None = None      # pl. az URL 2. szegmense
    extra: dict[str, Any] = {}


class Event(BaseModel):
    id: str                        # lásd 4.1
    source_ids: list[str]
    urls: list[str]
    title: str
    description: str | None
    start: datetime                # tz-aware, Europe/Budapest
    end: datetime | None
    effective_date: date           # a hajnali eltolás után (§7.7)
    is_series: bool = False        # end - start > series_threshold_days
    venue_name: str | None
    district: str | None           # "XI." vagy None
    lat: float | None
    lon: float | None
    distance_km: float | None
    price_min: int | None          # HUF
    price_max: int | None
    is_free: bool = False
    categories: list[str]
    image_url: str | None
    score: float = 0.0
    score_breakdown: dict[str, float] = {}
    group_key: str | None = None   # ha fesztivál-csoport tagja
    group_size: int = 1            # összevont sorban a csoport mérete
```

## 4.1 Az `id` képzése

```python
def make_event_id(title: str, start: datetime, venue: str | None) -> str:
    basis = f"{normalize_title(title)}|{start.date().isoformat()}|{normalize_venue(venue)}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]
```

Szándékosan **nem** a forrás saját id-je: ugyanaz az esemény több forrásból is jön, és a
ledgernek forrásfüggetlenül kell azonosítania. A `normalize_title` és `normalize_venue`
ugyanaz a függvény, amit a dedup használ (§7.2) — így az id-egyezés a dedup 0. szintje.

### Két függvény, két feladat

A cím-normalizálás **két** függvényre bomlik, és nem cserélhetők fel:

```python
def normalize_title(s: str) -> str:
    """Konzervatív. Kizárólag: kisbetűsítés, ékezet-eltávolítás (NFKD), legfeljebb
    2 tokenes zárójeles utótag levágása, whitespace összevonása. Elválasztónál NEM vág."""

def strip_venue_suffix(title: str, venue: str | None) -> str:
    """Helyszín-tudatos. Kizárólag a dedup fuzzy szintje (§7.2) hívja."""
```

Az `id` és a ledger a `normalize_title`-t használja. A `make_event_id` **soha** nem hívja a
`strip_venue_suffix`-et: az id-nek a legóvatosabb normalizálásra kell épülnie.

### Miért konzervatív a `normalize_title` — ellenpélda

Kézenfekvőnek tűnik az elválasztó (` | `, ` - `) utáni részt levágni, hiszen gyakran a
helyszín áll ott. **Bármelyik oldalon vágunk, események olvadnak össze:**

| Vágás | Bemenet | `normalize_title` | Eredmény |
|---|---|---|---|
| első elválasztónál | `Koncert - Sub Focus`<br>`Koncert - Chase & Status` | `koncert`<br>`koncert` | azonos id |
| utolsó elválasztónál | `A38 \| Koncert X`<br>`A38 \| Koncert Y` | `a38`<br>`a38` | azonos id |

A két hiba egymás tükörképe, és nincs olyan oldal, amelyik biztonságos: a címformátum
forrásonként — sőt rekordonként — más. Van, ahol a helyszín a végén áll (`Sub Focus | A38`),
van, ahol az elején (`A38 | Koncert X`). Az elválasztó önmagában nem árulja el, melyik.

Az azonos id a rendszer legdrágább hibája: a dedup 0. szintje összevonja a két eseményt, a
ledger pedig a másodikat **véglegesen elnémítja** — soha nem megy ki, és nyoma sem marad.
Ezért a `normalize_title` csak azt vágja le, ami bizonyítottan zaj: a rövid, zárójeles
ország- vagy városjelölést. A `(Budapest)`, `(HU)`, `(UK)` megy; a
`(Deluxe Anniversary Edition)` marad, mert 2 tokennél hosszabb, tehát a címhez tartozik.

A bent maradt zajt a dedup fuzzy szintje fogja el a futáson belül. **Az alulvágás ára egy
ismétlődő email, a túlvágásé egy végleg elveszett esemény — a kettő nem egyenrangú.**

### `strip_venue_suffix` — mindhárom feltétel kötelező

A dedup fuzzy szintjének kell a helyszín-utótag levágása, de csak akkor szabad, ha
bizonyítható, hogy tényleg a helyszín az:

1. `venue` nem `None`, és normalizálva nem üres;
2. a címben van elválasztóval (` | ` vagy ` - `) bevezetett **záró** szegmens;
3. `token_set_ratio(normalize_venue(szegmens), normalize_venue(venue)) >= 85` — ugyanaz a
   primitív és küszöb, amit a §7.2 a helyszín-összehasonlításra használ.

Ha bármelyik nem teljesül, a függvény a címet **változatlanul** adja vissza. A 3. feltétel
zárja ki a fenti tükörhibát: az `A38 | Koncert X` záró szegmense (`Koncert X`) nem egyezik
az `A38 Hajó` helyszínnel, tehát nem vágunk.

**Következmény, amit tudni kell:** ha egy forrás úgy írja át a címet, hogy a
`normalize_title` nem tünteti el a különbséget (`Sub Focus` → `Sub Focus | A38`), az id
megváltozik és az esemény újra kimegy. Ezt a dedup fuzzy szintje fogja el a futáson belül,
a ledger viszont csak az id-t tárolja. Ezért a ledger a `(id, start_date, title_norm)`
hármast tárolja, és a `sent_before()` ellenőrzés fuzzy is (§8.2).

---

# 5. Konfiguráció

Három forrásból áll össze, ebben a sorrendben (később felülír):

1. `config.yaml` — a repóban, **publikus**
2. `sources/*.yaml` — a repóban, **publikus**
3. `PROFILE_YAML` GitHub Actions secret — **privát**, a személyes rész

## 5.1 `config.yaml` (publikus)

```yaml
version: 1

schedule:
  timezone: Europe/Budapest
  horizon_days: 20

fetch:
  user_agent: "budapest-event-digest/1.0 (+https://github.com/<user>/<repo>)"
  timeout_seconds: 20
  max_retries: 3
  backoff_base_seconds: 2
  default_rate_limit_seconds: 1.5
  respect_robots_txt: true

categories:
  # kategória -> jelek. A pontszám összeadódik, a legmagasabb nyer.
  koncert:
    keywords: { koncert: 3, "élő zene": 3, lemezbemutató: 2, zenekar: 2, akusztik: 2 }
    venue_prior: { "A38 Hajó": 2, "Akvárium Klub": 2, "Dürer Kert": 2, "Kobuci Kert": 2 }
    url_patterns: ["/koncert/", "/zene/"]
    native_types: ["concert"]
  klub:
    keywords: { dj: 3, techno: 3, house: 2, party: 2, "lemezlovas": 2, rave: 3 }
    venue_prior: { "Ötkert": 2, "Instant": 2, "Turbina": 2 }
    native_types: []
  szinhaz:
    keywords: { előadás: 2, színház: 4, dráma: 2, bemutató: 2, stúdió: 1 }
    url_patterns: ["/szinhaz/"]
    native_types: ["theater", "theatre"]
  kiallitas:
    keywords: { kiállítás: 4, tárlat: 3, galéria: 2, múzeum: 2, "enteriőr": 1 }
    url_patterns: ["/kiallitas/"]
    native_types: ["exhibition"]
  film:
    keywords: { film: 3, vetítés: 3, mozi: 3, premier: 2 }
    url_patterns: ["/film/", "/mozi/"]
    native_types: ["movie", "screening"]
  meetup:
    keywords: { meetup: 4, workshop: 2, előadás: 1, "közösségi": 2, networking: 3 }
  tarsasjatek:
    keywords: { társasjáték: 4, "board game": 4, játékest: 4, "társasozás": 4, "játékklub": 3 }
    venue_prior: { "Red & Black": 3, "Játsz/Ma": 3 }
  kviz:
    keywords: { kvíz: 4, quiz: 4, vetélkedő: 3, "kvízest": 4, pubquiz: 4 }
  gasztro:
    keywords: { borkóstoló: 3, sörkóstoló: 3, gasztro: 3, vacsora: 2, piac: 2, street food: 3 }
  fesztival:
    keywords: { fesztivál: 4, festival: 4 }
  outdoor:
    keywords: { túra: 3, séta: 2, kirándulás: 3, futás: 2, kerékpár: 2 }
  sport:
    keywords: { mérkőzés: 3, bajnokság: 2, verseny: 2, edzés: 2 }
  csaladi:
    keywords: { gyerek: 3, családi: 3, bábszínház: 3 }
min_category_score: 2
fallback_category: egyeb

grouping:
  collapse_by: [venue_name, effective_date, primary_category]
  min_group_size: 4
  max_per_venue: 3

recurrence:
  series_threshold_days: 7
  series_behavior: send_once
  run_behavior: send_at_start

night_shift:
  before_hour: 5          # 00:00-04:59 az előző naphoz tartozik

newsletter:
  per_category_limit: 10
  total_limit: 50
  send_when_empty: true   # heartbeat — az email hiánya a riasztás
  expiring_section:
    enabled: true
    within_days: 3

llm:
  enabled: true           # M7 óta bekapcsolva
  provider: gemini
  model: gemini-2.5-flash-lite
  batch_size: 35
  max_calls_per_run: 12
  on_quota_error: fallback_to_rules
  only_for: [uncategorized, ambiguous_dedup]

delivery:
  - type: smtp
    enabled: true
  - type: telegram
    enabled: false

site:
  base_path: "/budapest-event-digest"
  archive_keep_days: 90
```

## 5.2 `PROFILE_YAML` secret (privát)

```yaml
recipient_email: "..."
home:
  district: "XI."
  lat: 47.47
  lon: 19.05
scoring:
  category_weights:
    tarsasjatek: 5
    koncert: 4
    kviz: 4
    gasztro: 3
    kiallitas: 2
    klub: 2
    szinhaz: 2
    film: 1
  keyword_boosts: { koreai: 3, "craft beer": 2, sörkóstoló: 2, "board game": 2 }
  free_bonus: 2
  cheap_bonus: { under_huf: 4000, points: 1 }
  proximity:
    same_district_bonus: 2
    penalty_cap_km: 8          # a büntetés felső határa, NEM szűrő
    distance_penalty_per_km: 0.3
  novelty_bonus: 2
  soon_bonus: { within_days: 7, points: 1 }
  weekday_weights: { mon: 0, tue: 0, wed: 1, thu: 1, fri: 2, sat: 2, sun: 1 }
filters:
  categories: [koncert, klub, szinhaz, kiallitas, film, meetup, tarsasjatek, kviz, gasztro, fesztival, outdoor]
  max_price_huf: 12000
  blocked_keywords: ["gyerekprogram*", "bábszínház*"]  # teljes szavas; a "*" kér szóeleji illesztést
  min_score: 3
  geo:
    city: "Budapest"          # a normalizált `city` mezőhöz hasonlítva
    allow_missing_city: true  # ismeretlen városú esemény MARAD, nem esik ki
    max_distance_km: null     # KIZÁRJA az eseményt; a scoring.proximity.penalty_cap_km csak büntetést határol
```

**A két illesztési mód és a két jelölő.** Ugyanaz a `contains_word` szolgálja a
kategóriák kulcsszavait, a `keyword_boosts`-ot és a `blocked_keywords`-öt — a kérdés
(„szerepel-e ez a kifejezés a szövegben") egy helyen van megválaszolva. Ami hívási
helyenként **eltér**, az az alapértelmezés, mert nem a kifejezés tulajdonsága, hanem azé,
hogy mennyibe kerül egy téves találat:

| hívási hely | alapértelmezés | egy téves találat ára |
|---|---|---|
| kategória `keywords` (§7.5) | **szóeleji** | félrecímkéz — látszik a digestben, javítható |
| `scoring.keyword_boosts` (§7.7) | **szóeleji** | pontot mozdít — pozitív boostoknál nem tüntet el semmit |
| `filters.blocked_keywords` (§7.6) | **teljes szavas** | **eldobja** az eseményt — néma és visszafordíthatatlan |

A `keyword_boosts` sora egy feltételt rejt: **negatív** boost a `min_score` alá tolhat egy
eseményt, és akkor ott is eldob. A §5.2 példája végig pozitív, tehát a mai configra igaz —
de ha valaha negatív boost kerül bele, az a bejegyzés ugyanolyan fail-closed lesz, mint egy
tiltószó, és `$`-t érdemel.

Mindkét mód mindenhol elérhető, csak kérni kell:

- `kulcsszó*` → **szóeleji** illesztés, a hosszkorláttól és az alapértelmezéstől függetlenül.
- `kulcsszó$` → **teljes szavas** illesztés, ugyanígy.
- Jelölő nélkül az adott hívási hely alapértelmezése dönt (a §7.5 ötkarakteres alsó
  határával együtt). A kifejezés **utolsó** karaktere számít jelölőnek, tehát a kettő nem
  kombinálható.

**Figyelem a `blocked_keywords`-re.** Itt a biztonságos viselkedés az alapértelmezett, és
a tág az, amit külön ki kell mondani — nem fordítva. A `gyerek` tiltás így **nem** dobja el
a „Gyerekkori álmom volt ez a koncert" leírású koncertet, sem a „Gyerekzsivaj nélküli
felnőtt est"-et, ami ráadásul felnőtt program, vagyis pont az ellenkezője annak, amit a
szabály kért. Ugyanaz az elv, mint a §7.6 földrajzi szűrésénél: **ha a rendszer bizonytalan,
megtartja az eseményt.** A magyar egybeírja az összetett szavakat, tehát a teljes szavas
`gyerek` a „Gyerek nap"-ra illeszkedik, a „Gyerekprogram"-ra nem — ha a toldalékos és
összetett alakok is kellenek, `gyerekprogram*` a helyes írásmód. Ezért van a fenti példában
mindkét bejegyzésen csillag: a „bábszínház" toldalékolva „bábszínházi", és a csillag nélkül
a „Bábszínházi előadás" átmenne a tiltáson.

## 5.3 Merge és validáció

`config.py`:

```python
def load_config(
    config_path: Path,
    sources_dir: Path,
    profile_yaml: str | None,   # a secret tartalma, nem fájlnév
) -> Config: ...
```

- A profil **mélyen merge-elődik** a configra (dict update rekurzívan, lista felülír).
- Pydantic modellre validál; ismeretlen kulcs → hiba, nem néma átugrás.
- **Kötelező védőteszt (`test_config_privacy.py`):** a `config.yaml` nem tartalmazhat
  `scoring`, `home`, `recipient_email` vagy `filters` kulcsot. Ha igen, a teszt elhasal.
  Ez véd attól, hogy egy elgépelt commit kirakja a profilodat a publikus repóba.
- Ha a `PROFILE_YAML` hiányzik, a futás **nem hal el**, hanem beépített semleges
  alapértelmezéssel megy (minden súly 1, nincs proximity). Így a repo klónozható és
  futtatható idegen által is, csak nem lesz személyre szabott.

---

# 6. Forrásréteg

## 6.1 Döntési sorrend új forrásnál

Végig kell menni, és az **elsőnél megállni, ami működik**:

1. Van hivatalos API / RSS / iCal? → `fetcher: api`
2. Nincs? DevTools → Network → Fetch/XHR, töltsd újra a listát. Van JSON végpont? → `fetcher: api`
3. A HTML-ben ott az adat (SSR)? → `fetcher: http`, deklaratív YAML
4. Csak JS után létezik és nincs végpont? → elvileg Playwright — **de a v1-ben nincs implementáció**
5. Blokkol vagy hetente törik? → **dobd a forrást.** Van másik tizenöt.

**Miért nincs Playwright.** Egyetlen dolgot ad, a JS-futtatást, amit a 2. lépés általában
kivált. Cserébe 500-900 MB RAM és oldalanként 2-8 másodperc. És amit gyakran tévesen várnak
tőle: **nem véd a blokkolástól, hanem rontja** — a headless Chromiumot az anti-bot rendszerek
célzottan azonosítják (`navigator.webdriver`, CDP-nyomok, canvas/WebGL fingerprint,
TLS/JA3 eltérés). A `fetcher` mező a sémában marad, hogy egy későbbi backend plugin legyen,
ne refactor.

## 6.2 A `Source` protokoll

```python
class Source(Protocol):
    id: str
    name: str
    enabled: bool
    priority: int                  # dedup-merge sorrend, kisebb = erősebb
    fetcher: Literal["http", "api"]
    rate_limit_seconds: float

    def discover(self) -> Iterable[FetchTask]:
        """Milyen URL-eket kell lehívni. Lapozás itt bomlik ki."""

    def parse(self, result: FetchResult) -> Iterable[RawEvent]:
        """Egy letöltött válaszból nyers eseményeket ad. Nem dob kivételt
        egyedi rekord hibájára — azt logolja és kihagyja."""
```

Két implementáció: `DeclarativeSource` (YAML-ből példányosítva) és a `plugins/` alatti
Python osztályok. A `registry.py` mindkettőt felfedezi:

```python
def load_sources(sources_dir: Path, config: Config) -> list[Source]:
    # 1. minden sources/*.yaml -> DeclarativeSource, KIVÉVE ha van `plugin:` kulcs
    # 2. plugin: port_hu  ->  importlib import digest.sources.plugins.port_hu
    #    és a modul `build(spec, config) -> Source` függvényét hívja
```

## 6.3 Deklaratív YAML forrásmotor

Teljes mezőkészlet:

```yaml
id: welovebudapest            # kötelező, egyedi, = fájlnév
name: We Love Budapest
enabled: true
priority: 30
fetcher: http                 # http | api
rate_limit_seconds: 2
plugin: null                  # ha kitöltött, Python plugin veszi át

listing:
  urls:
    - "https://welovebudapest.com/programok/?page={page}"
  pagination:
    param: page
    start: 1
    max: 5
    stop_when_empty: true     # ha egy oldal 0 elemet ad, ne lapozz tovább
  item_selector: "article.program-card"      # http esetén CSS
  json_path: "data.events[*]"                # api esetén JSONPath

fields:
  title:        { selector: "h3", attr: text }
  url:          { selector: "a", attr: href, absolute: true }
  start_raw:    { selector: "time", attr: datetime }
  venue_name:   { selector: ".venue", attr: text, optional: true }
  price_raw:    { selector: ".price", attr: text, optional: true }
  image_url:    { selector: "img", attr: src, optional: true }
  description:  { selector: ".lead", attr: text, optional: true }

# api esetén a `selector` helyett `path` (JSONPath a tételen belül):
# fields:
#   title: { path: "title" }
#   start_raw: { path: "eventStart" }

transforms:                   # opcionális, mezőnként, sorrendben fut
  description: [html_unescape, strip, truncate:400]
  title: [html_unescape, strip]
```

Támogatott `attr` értékek: `text`, `html`, vagy bármely attribútumnév (`href`, `src`, `datetime`).
Támogatott transzformok: `html_unescape`, `strip`, `lower`, `truncate:<n>`, `absolute_url`,
`regex:<pattern>:<group>`.

**Kötelező viselkedés:** ha egy nem-`optional` mező hiányzik egy tételnél, azt a tételt
kihagyjuk és `WARNING`-ot logolunk a forrás id-jével — de a futás megy tovább.

**A fenti kulcskészlet zárt, és betöltéskor ellenőrzött.** Ismeretlen kulcs a spec
**bármelyik** szintjén — a legfelső szinten, a `listing:`, `listing.pagination:`,
`fields:`, egy `fields.<mező>:` bejegyzésen belül vagy a `transforms:` alatt —, valamint
ismeretlen transzformnév: `ConfigError`, a forrás nevével és a legközelebbi érvényes
kulccsal. Nem parse-időben: a
`DeclarativeSource` felépítésekor, `enabled` állásától függetlenül. Korábban az ismeretlen
kulcsot a motor kiolvasta és eldobta, és a tünet egy mindig üres mező volt —
megkülönböztethetetlen attól, hogy a forrás nem közli az adatot. A `tokenklub.yaml`
`city:` sora pontosan így élt holtan. A `fields:` érvényes készlete a
`RawEvent.model_fields`-ből származik, nem kézzel karbantartott listából; három mező ki van
zárva belőle, és az elutasítás megmondja, miért: `source_id` és `source_event_key` a motoré,
az `extra` pedig szabad dict, amibe semmilyen kinyerés nem ír.

A `plugin:` kulcsot hordozó specek **részben** esnek át ezen. A `listing:` és a `fields:`
blokkjuk a pluginé, saját szótárral — a Cooltix `listing.pagination.page_size`-t olvas, ami
itt semmit nem jelentene —, tehát az a két blokk kimarad. A **legfelső szint** és a
`transforms:` viszont nem: azokat a kulcsokat a registry és a fetch réteg olvassa, nem a
plugin, és a Cooltix `enabled:`-jét pontosan ugyanaz a kód nézi, mint a tokenklubét. Egy
elgépelt `enabledd:` a legrosszabb néma hiba a készletben: minden másik egy mezőt veszít
el, ez viszont arra a kérdésre ad rossz választ, hogy fut-e egyáltalán a forrás — arra,
amit az ember a YAML-ból olvas ki.

## 6.4 Fetch réteg

```python
@dataclass(frozen=True)
class FetchTask:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None
    json_body: dict | None = None

@dataclass(frozen=True)
class FetchResult:
    task: FetchTask
    status: int
    text: str
    json: Any | None
    from_cache: bool
```

Követelmények:
- `httpx.Client`, közös `User-Agent` a configból
- Forrásonként `rate_limit_seconds` késleltetés a kérések között
- Retry: `max_retries`, exponenciális backoff, csak 5xx és hálózati hibára. **429-re nem
  retryolunk azonnal**, hanem megvárjuk a `Retry-After`-t, vagy kihagyjuk a forrást
- `robots.txt` ellenőrzés forrásonként egyszer, cache-elve a futás idejére
- `If-None-Match` / `If-Modified-Since` küldése, ha a ledgerben van korábbi ETag

## 6.5 Port.hu — igazolt leképezés

A válasz `{"event-<id>": {...}}` objektum. Plugin: `plugins/port_hu.py`.

| `Event` / `RawEvent` mező | Port.hu forrás | Megjegyzés |
|---|---|---|
| `source_event_key` | `id` (`"event-6258530"`) | stabil |
| `title` | `title` | tiszta |
| `description` | `description` | **HTML-entitás kódolt ÉS csonkolt** → `html.unescape()` |
| `start_raw` | `eventStart` (`"2026-08-14 19:00:00"`) | **nincs tz** → `Europe/Budapest` |
| `end_raw` | `end` (`"- 08. 21. 23:59"`) | **év nélküli display string**, lásd lent |
| `venue_name` | `place` | |
| `url` | `url` | relatív → `https://port.hu` prefix |
| `lat` / `lon` | `address.gps.geoPoint.lat` / `.lon` | **minden rekordon** |
| `district_raw` | `address.district` | **megbízhatatlan**, lásd lent |
| `postal_code` | `address.zip` | |
| `native_category` | `type` (`"concert"`) | |
| `url_category` | az `url` 2. szegmense (`/esemeny/zene/...` → `zene`) | |
| `image_url` | `thumbnail` | |
| ár | — | **nincs**, a `ticket` tömb üres |

**Három kötelező szabály ehhez a forráshoz:**

1. **A `gallery` tömböt teljesen el kell dobni.** Egy rekordban 24 kép is lehet, köztük az
   eseményhez nem kapcsolódók. Csak a `thumbnail` kell. Ez a payload ~90%-át levágja.
2. **A `district` fallback az irányítószámból.** Az A38-nál `11`, a Szigetnél `null` — pedig
   mindkettő Budapest. Budapesti `1XYZ` irányítószám esetén `XY` a kerület:
   `1113` → `XI.`, `1033` → `III.`. Determinisztikus, a mintaadat megerősíti.
3. **Detail oldalak kihagyva.** A csonkolt leírás email-digesthez elég, cserébe futásonként
   ~300 HTTP-kéréssel kevesebb.

**Az `end` mező parseolása.** Formátuma `" - 08. 21. 23:59"` — nincs benne év. Szabály:
vedd a `start` évét; ha az így kapott `end` korábbi lenne, mint a `start`, akkor `start.year + 1`.

**Nyitott: a listázó végpont URL-je és paraméterei.** A specbe be kell írni, amint megvan.
Amíg nincs, a `sources/port-hu.yaml` `listing.urls` mezője placeholder, és a plugin egy
fixture-ből olvas a tesztekben. Ugyanígy nyitott a `type` mező teljes szótára — a mintában
csak `"concert"` szerepelt. Az M0 egyik feladata ezt feltárni (`digest fetch port-hu --dump-types`).

## 6.6 Forráslista

**A. Gerinc** — az „Út" a §6.1 lépése, ameddig a forrás eljutott; az „Állapot" külön
oszlop, mert a kettő nem ugyanaz (lásd B tábla).

**A „Horizonton belül" oszlop a §7.1 után megmaradó eseményszám**, nem a parse-olt
rekordoké. Ez a kettő nagyságrendileg eltér — a kvizestek 91 budapesti rekordot ad, de
26 esik a 14 napos horizontba —, és a digest szempontjából csak az utóbbi létezik. A
parse-olt szám zárójelben marad ott, ahol a különbség maga a lényeg. Minden érték a
`tests/fixtures/` alatti mentett válaszból mérve, a fixture mentési napjához viszonyítva;
`—` = nincs mért adat (felderítetlen vagy elvetett forrás). **A tábla értékei 14 napos
horizonton készültek**; a szállított `schedule.horizon_days` azóta 20, tehát a mai számok
ennél magasabbak (a kvizestek például 26 helyett 40) — a tábla összehasonlításra jó, aktuális
darabszámnak nem.

| Forrás | Út | Állapot | Horizonton belül | Mit ad |
|---|---|---|---|---|
| Port.hu | 2. — JSON, igazolva | **függőben** | 20 (20 parse-olt) | minden, ár kivételével — a listázó URL nyitott (§17.1) |
| Jegy.hu | 2. — JSON | felderítetlen | — | **árat** — ez tölti a `free_bonus`/`cheap_bonus` szabályt |
| bigcitylife.hu | 3. — SSR, igazolva | **él** | 2 (9 parse-olt) | koncert, klub, fesztivál — kurált hétvégi válogatás |
| Programturizmus | 3. — SSR, igazolva | **elvetve** | 16 → 2 öt nap alatt | 20 kártyából 3 valódi esemény; lásd lent |
| We Love Budapest | — | **elvetve** | — | semmit: a robots.txt névre tiltja az `anthropic-ai`-t |
| Funzine | 3. — SSR, igazolva | **elvetve** | — | semmit: az esemény poszttípus 2018 óta halott |
| Fidelio | 3. — SSR kereső, igazolva | **elvetve** | 0 | semmit: a kereső működik, az adatbázis üres — „0 találat" |
| Színházak.hu | — | **elvetve** | — | semmit: a domain parkolt, a szinhaz.hu 2020 óta halott blog |
| Cooltix | 2. — GraphQL, igazolva | **él** | 79 (83 budapesti / 500 lekért) | vegyes budapesti merítés |

**We Love Budapest — nem technikai akadály.** A `welovebudapest.com/robots.txt` név
szerint tiltja az `anthropic-ai` user agentet az egész oldalra (`Disallow: /`), a GPTBot
és a CCBot mellett. A `/programok/` a `User-agent: *` alatt nem tiltott, tehát egy
másképp nevezett kliens letölthetné — de az oldal egyértelműen megmondta, hogy egy
Anthropic-modellt nem szeretne ott látni, és ezt a fixture-t pontosan egy az. Más néven
bekopogni azért, hogy megkerüljünk egy ránk szabott szabályt, nem opció: nincs fixture,
nincsenek szelektorok. Újraellenőrizve 2026-08-22, a szabály változatlan.

**Programturizmus — technikailag működött, tartalmilag nem. Eltávolítva 2026-08-22.**
A forrás elért a §6.1 3. lépéséig: a szelektorok illeszkedtek, a Budapest-hatókör megvolt,
két oldal 20 rekordot adott megbízhatóan. A mérés mégis ellene döntött, és a számok azért
állnak itt, hogy fél év múlva ne kelljen újra felfedezni:

| mérés | érték |
|---|---|
| valódi, egyedi esemény a 20 kártyából | **3 (15%)** — a többi gyűjtőoldal („Budapest Jazz Club programok 2026") |
| több különböző dátumot hordozó részletoldal | **11 / 20**, a legrosszabb egyetlen oldalon **71** dátummal |
| helyszínnel | **0 / 20** — a helyszínsáv csak megye/város/kerület |
| órával | **0 / 20** — minden rekord dátum, a 00:00 hiányzó érték |
| dátumtartomány (`end_raw`) | 13 / 20 |

**A döntő szám nem a minőség, hanem az avulás.** Ugyanaz a 20 mentett rekord, a szállított
14 napos horizonton, `digest` végig (normalize → … → group):

| futás napja | ebből a forrásból a digestben |
|---|---|
| 2026-08-16 | 15 |
| 2026-08-22 (a fixture mentési napja) | **16** |
| 2026-08-27 (+5 nap) | **2** |
| 2026-09-01 (+10 nap) | 2 |
| 2026-09-05 (+14 nap) | 4 (egy későbbi rekord lép a horizontba) |

Öt nap alatt 16-ról 2-re esik, és nem azért, mert a horizont mozog, hanem mert a forrás
nem görgeti előre a dátumait. Bent hagyva a semmi felé tart, miközben naponta két
oldalletöltésbe kerül, és nincs mit később visszakapcsolni.

A törlés ára a teljes digestben, ugyanezen a mentett merítésen, az engedélyezett források
felett: **127 → 111** esemény a fixture napján (−16), és **65 → 63** öt nappal később
(−2). A második szám az igazi: ennyit ért volna a forrás egy hét múlva.

**Miért törlés és nem `enabled: false`.** A letiltott forrás bent marad a registryben,
bent marad ezekben a táblákban, karbantartást kér a szelektoraira, és azt a látszatot
kelti, hogy van egy tartalék, ami valójában nincs. A mérés viszont megmarad — ez a szakasz
maga a megőrzött eredmény.

**Amit a forrás csak megmutatott, az marad.** A §7.1 `normalize_district`-je (magyar
kerületszöveg), a §7.4 helyszín nélküli csoportosítási szabálya, a `start_time_known` mező
és a §7.7 hajnali eltolása mind általános szabályok, és mind a helyükön vannak. Két
következménye van a törlésnek, mindkettő tudatos: a repóban **egyetlen fixture sem gyárt
többé helyszín nélküli rekordot** (a §7.4 szabályát a `tests/test_group.py` szintetikus
eseményeken őrzi), és a `%Y.%m.%d.` / `%Y. %m. %d.` dátumformátumoknak sem maradt élő
kibocsátója — a parserben maradnak, mert egy formátum olcsó és a következő magyar oldal
ugyanígy fog írni.

**Fidelio — a kereső megvan, a mögötte lévő adatbázis üres.** A korábbi jegyzet azt
mondta, nincs listaoldal; ez tévedés volt. A `fidelio.hu/programok` **a** Programkereső,
szerveroldalon renderelt, és teljesen paraméterezett GET-et fogad:
`ProgramSearch[city]` (1 = Budapest), `[category]` (7 Klasszikus, 1 Színház, 4 Zenés
színház, 19 Tánc, 16 Kiállítás), `[date_from]`, `[date_to]`, `[daypart]`. A dátum formátuma
`ÉÉÉÉ.HH.NN` — ISO alakkal a szerver 302-vel a főoldalra dob, és pont ez olvasódik
„nincs listaoldal"-ként egy URL-találgatós körben. A robots.txt egyetlen Sitemap sor.
Mégis elvetve, más okból: **minden lekérdezés nulla sort ad.** A szűrők vissza vannak
echózva (`<option value="1" selected>` Budapesten, a beküldött dátumok az inputokban),
a találati konténer pedig a saját üres állapotát rendereli:
`<h4 class="search-counts">0 találat</h4>` és „Nincs találat" — 2018-ra, 2019-re és egy
teljes évre előre ugyanúgy. A szűretlen oldalhoz képest a diff 14 sor: CSRF token,
echózott szűrőértékek, Cloudflare email-obfuszkáció. Tehát nem JS-renderelési és nem
hozzáférési kérdés — ugyanaz az alakzat, mint a Funzine: élő felület, halott adat. A
szelektorok a `sources/fidelio.yaml`-ban rögzítve arra az esetre, ha feltöltenék.

**Színházak.hu — rossz domain parkolt, a jó domain 2020-ban megállt.** A `szinhazak.hu`
az `old.byte.hu`-ra megy és HTTP 526-ot ad (érvénytelen origin-tanúsítvány) — a byte.hu a
tárhelyszolgáltató, ez az ő parkolója. A `szinhaz.hu` él, de blog.hu-alapú szerkesztőségi
blog: a címlap legfrissebb cikke 2020-11-25, nincs `<time>`, nincs előadásonkénti markup.
**Amit érdemes tudni:** a saját jegylinkje a `//port.hu/jegy`-re mutat. Vagyis a
színházi repertoár hiánya **Port.hu-lefedettségi kérdés** (§17.1), nem hiányzó forrás —
másik „színházportál" keresése nagy eséllyel megint magazint talál.

**Funzine — élő archívum, halott tartalom.** A WordPress `event` poszttípus minden
technikai feltételt teljesít (saját archívum lapozással, tiszta SSR, nyitott robots.txt),
csak épp 2018 decembere óta nem került bele semmi: az `event-sitemap.xml` 356 URL-jének
`lastmod` értékei 2017-01-05 és 2018-12-18 közé esnek, a „Következő események" fejlécű
archívum legfrissebb tétele 2018. dec. 26. Ami ma frissül, az szerkesztőségi listacikk
(`/2026/08/19/goodapest/35-fergeteges-program...`), nem eseményrekord — annak parse-olása
ugyanaz a csapda, amit a We Love Budapest kapcsán a spec már kizár.

**B. Közösségi réteg** — kis oldalak, heti pár eseménnyel. Épp az alacsony volumenük az
érték: ellensúlyozzák a fesztivál-elárasztást.

| Forrás | Út | Állapot | Horizonton belül | Mit ad |
|---|---|---|---|---|
| kvizestek.hu | 2. — JSON, igazolva | **él** | **26** (91 parse-olt) | budapesti kvízestek |
| Tixa | 3. — SSR JSON-LD, igazolva | **él** | 1 | egy társasest; a lista szándékosan szűk, lásd lent |
| tokenklub.hu | 2. — REST API, igazolva | **él, de üres** | 0 | szezonális klub, nyári szünet — magától éled fel |
| tarsasjatekos.hu | 1. — naptár API | **kulcsra vár** | ~12 (becslés) | társasklubok; `GCAL_API_KEY` kell, lásd lent |
| redandblack.hu | 3. — SSR, igazolva | **elvetve** | — | semmit: a saját oldal halott, a hely nem — lásd lent |
| esemenyek.kedvesidegen.hu | 2. — JSON, igazolva | **elvetve** | — | semmit: nincs gépi dátum |
| Játsz/Ma Társasjáték Kávézó | — | **lefedve** | — | a Cooltixon keresztül jön, nem kell külön forrás |
| Illegál kvízest | — | felderítetlen | — | — |
| Meetup | 3. — `__NEXT_DATA__` | felderítetlen | — | lásd lent |

Az „Út" a §6.1 döntési sorrend lépése, ameddig a forrás eljutott — az „Állapot" külön
oszlop, mert a kettő nem ugyanaz: mindhárom felderített forrás elért egy működő lépésig,
kettő mégis használhatatlan. A részletes indoklás a `sources/<id>.yaml` fejlécében van,
a bizonyíték a `tests/fixtures/` alatti mentett válaszokban, az állítások pedig a
`tests/test_source_community.py`-ben vannak rögzítve.

**kvizestek.hu — a lista átköltözött.** A `kvizestek.hu/esemenyek` oldal 2026 februárja óta
csak átirányít: az események a `foglalas.kvizestek.hu` React SPA-ban élnek, amely egyetlen
lapozás nélküli `/api/events/upcoming` végpontot kérdez. Plugin, nem deklaratív spec, mert
a kezdési időpont két mezőből áll össze (`eventDate` dél-UTC dátumjelölő + `eventTime`), és
mert a végpont országos: a 132 rekordból 41 Budapesten kívüli. A településre szűrés **ma már
pipeline szakasz** — a §7.6 földrajzi kizárása, ami a mérvadó szabály —, de a forrás
ettől függetlenül elvégzi a magáét: nem hordunk végig azon, amit úgyis eldobnánk (§7.6
zárómondata). Ez a mondat 2026-08-22-ig azt állította, hogy ilyen szakasz nincs; a geo
szakasz megérkezésével az állítás elavult.

**tarsasjatekos.hu — a legjobb forrás a kategóriában, de kulcs kell hozzá.** A Magyar
Társasjátékos Egyesület országos klubnaptára 160 eseményt tartott egy évre előre; ebből 8
esett budapesti 14 napos horizontba, plusz 4 a két heti klubból, amint a sorozat ki van
bontva. A `klubok.html` maga **nem** ez: az klubkatalógus, prózában megadott
ismétlődéssel („minden hónap 2. szombatján", „eseménynaptár szerint"), dátum, `<time>` és
JSON-LD nélkül — abból csak találgatással lehetne időpontot csinálni. A dátumos alak
kizárólag a beágyazott Google-naptár. Annak nyilvános `.ics` exportja viszont a
`calendar.google.com`-on él, aminek a robots.txt-je `Allow: /$` után `Disallow: /`, és a
Python `RobotFileParser` — amit a `fetch/http.py` használ — `False`-t ad rá; a régi
`www.google.com/calendar/ical/...` út pedig 302-vel ugyanoda visz. A forrás ezért a Google
Calendar API-n olvas, `singleEvents=true`-val szerveroldalon kibontott ismétlődéssel.

**Miért helyes ez, és mi NEM az indoklás.** Két okból:

1. A `www.googleapis.com` **dokumentált programozói felület**, API-kulccsal, pontosan erre
   a célra. Egy publikált API-t használni ahelyett, hogy a HTML- vagy ICS-frontendet
   kapargatnánk, nem megkerülés, hanem a **helyesebb** viselkedés: ez az a csatorna,
   amelyet az üzemeltető a gépi olvasásra szánt, kulccsal azonosított hívóval és ismert
   kvótával.
2. A naptár **tartalma a tarsasjatekos.hu-é, nem a Google-é**. Az egyesület nyilvánosra
   állította és a saját oldalába ágyazva közzétette — olvasásra. A `calendar.google.com`
   robots.txt-je a Google saját webalkalmazására vonatkozik, nem az egyesület adatára; az
   API ugyanazt a nyilvános naptárat adja vissza, csak a szánt csatornán.

**Az indoklás NEM az, hogy a `www.googleapis.com`-nak nincs robots.txt-je.** Ez a tény
igaz, de önmagában semmit nem engedélyez, és tilos így hivatkozni rá: a „nincs robots.txt,
tehát bármit lehet" általánosítás hamis, és pont az ellenkezője annak, amit a §6.1 10.
pontja és a We Love Budapest esete (ahol a robots.txt névre tiltott minket, és emiatt
elvetettük a forrást) rögzít. A hiányzó robots.txt legfeljebb azt jelenti, hogy **nincs
külön tiltás**; az engedélyt a fenti két pont adja, nem a hiánya. Egyetlen hiányzó darab a `GCAL_API_KEY`; addig
`enabled: false`, és kulcs nélkül hangosan hasal el, nem csendben (a futásból a
`cli.py` `source_disabled_in_config` ággal esik ki, tehát nem termel hibát sem).

**A `parse()` még soha nem futott valódi API-válaszon, és a 12-es szám becslés.** Ugyanannak a naptárnak az `.ics` exportjából
számolt — az az egyetlen alak, amit robots-tiltás nélkül *nem* lehetett letölteni, de
elemezni igen: 160 esemény, ebből 8 budapesti a 14 napos horizonton, plusz 4 a két heti
klubból. A `plugins/tarsasjatekos.py` `parse()` függvénye viszont **még nem futott valódi
Calendar API válaszon**, mert ahhoz kulcs kell. A mezőleképezést a dokumentált API-séma
alapján írtuk; az első kulcsos futás egyben az első éles próbája is, és akkor kell
elkészülnie a `tests/fixtures/tarsasjatekos_calendar.json` fixture-nek.

**Cooltix — jegyértékesítő, aggregátor méretben.** GraphQL végpont
(`api.cooltix.com/graphql`), hitelesítés nélkül válaszol, introspection nyitva, mindkét
érintett host robots.txt-je 404 (= minden engedett). Az `events(status: LIVE, countryCode:
HU, orderBy: startDate_ASC)` kurzoros lapozással megy, és a dátum szerinti rendezés miatt
a horizont a találati halmaz prefixe. Egy csapda van benne: a dátum nélküli tételek
(utalványok, állandó kiállítások) rendeződnek **előre** — 2026-08-22-én 369 darab —, ezért
`page_size: 500` és `max: 3`, nem a szokásos százas lapozás. Cserébe ez az egyetlen gépi
bizonyíték arra, hogy a Red&Black Társasjátékszalon újra tart nyilvános programot.

**Tixa — gépi dátum, ami nem mindig igaz.** A szervezői és helyszíni oldalak
szerveroldali JSON-LD `ItemList`-et adnak ISO `startDate`-tel, tehát §6.1 3. lépés. A
`POST /search` végpontot **nem** használjuk: az `startDate`-et magyar prózaként adja
(„2026. augusztus 24. 17:00"), amit a §7.1 nem olvas. A valódi baj viszont a listában is
ott van: a Dürer Kert oldalán 24-ből 16 rekord `T00:00:00`-t hordoz, a tényleges kezdés
pedig csak a `customDate` prózában létezik — és az esemény saját aloldala ugyanezt a
`T00:00:00`-t ismétli, tehát nincs mire visszaesni. A plugin ezért az éjfélt eldobja és
logolja (`skipped_placeholder_start`), a `listing.urls` pedig ezért rövid: széles
helyszínlistával csendben elveszne az események kétharmada. A bővítés feltétele, hogy a
§7.1 megtanulja a csupasz „ÉÉÉÉ. hónap NN. ÓÓ:PP" alakot.

**tokenklub.hu — jó API, üres naptár.** The Events Calendar (WordPress) REST végpontja
teljes gépi dátumot ad, a robots.txt csak a `/wp-admin/`-t tiltja. 2026-08-22-én
`{"events": [], "total": 0}` a válasz: 18 esemény volt 2025-03-28 és 2026-06-06 között,
azóta nyári szünet. Ez **nem** az AUDIT-5 által elkapott néma nulla: nem félreparse-olunk
semmit, a forrás maga mondja, hogy nincs mit adnia — a végpont alapból `start_date = ma`.
Ezért `enabled: true`, napi egy kéréssel, és magától éled fel az őszi szezonban.

**redandblack.hu — nincs aktuális program.** A markup parse-olható és a dátumok gépiek, de
az „Aktuális programjaink" konténer szerveroldalon üres, és a legfrissebb dátum az egész
oldalon 2024-08-26. A szalon saját közlése szerint „csak rendezvények esetén" tart nyitva.
Havi lapozás nincs: minden `?honap=` / `/2026-08` variáns ugyanazt az oldalt adja vissza,
a `#program-calendar` pedig halott markup — az oldal egyetlen JS fájlt sem tölt be.

**Második nekifutás (2026-08-22), a gyökér úton.** Léteznek esemény-aloldalak a `/programok`
alatti út helyett a gyökéren (21 darab, pl. `/tarsas-ismerkedo-est-...`), de dátum nincs
rajtuk semmilyen formában: se `<time>`, se `datetime=`, se JSON-LD, és prózában is csak egy
óraérték („Kezdés: 18:00") meg egy napnév („kedden este"). A `sitemap.xml` 21 URL-t sorol,
mindegyik `lastmod`-ja `2023-11-08T13:30:25+00:00`, és az esemény-aloldalakat nem is
tartalmazza. **A helyszín viszont él:** a Cooltix 2026-09-19-re árul nála eseményt. Ezért a
`config.yaml` `tarsasjatek.venue_prior` kulcsából a „Red & Black" rövid alak nem egyszerűen
kikerült, hanem a Cooltix által ténylegesen kiadott „Red&Black Társasjátékszalon" váltotta —
a régi érték amúgy sem illeszkedhetett soha, mert a `venue_prior` `normalize_venue` utáni
**pontos egyezés**, nem részszöveg (§7.5).

**kedvesidegen — nincs gépi dátum, sehol.** A WooCommerce Store API nyilvánosan válaszol,
de a termékséma egyetlen dátummezőt sem tartalmaz. A dátum kizárólag a terméknévben van,
évszám nélkül („November 22. – Játékest"), és a nevek újrahasznosítottak: a 439-es termék
neve „Április 19.", a slugja `marcius-6-jatekest`. A jövőbeli időpontok `<br>`-rel
elválasztott prózaként állnak egyetlen bekezdésben. Kiolvasásuk prózaparsert és
évszám-találgatást igényelne — §6.1 5. lépés.

**Meetup: az API nem járható.** A nyílt REST API megszűnt; a GraphQL érdemi elérése Meetup Pro
előfizetéshez és jóváhagyott OAuth consumerhez kötött, és a Pro sem garantálja a jóváhagyást.
Helyette: a publikus csoportoldalak beágyazott `__NEXT_DATA__` blokkja bejelentkezés nélkül
parse-olható. Plugin: `plugins/meetup.py`, a configban **konkrét csoport-slugok listája** —
"minden budapesti esemény" nem kérdezhető le.

**C. Gazdagító (nem `Source`)** — Ticketswap. Viszontértékesítő piactér, a listái már létező
eseményekhez tartoznak. `Enricher` interfészen tesz rá `resale_available` flaget. M8.

**D. Elvetve** — Facebook Events (ToS). Ticketmaster (gyenge magyar lefedettség, itt az Eventim
domináns). Bandsintown/Songkick (**előadó-központú**: előadónként kérdezel le, nem városra —
városi digesthez rossz illeszkedés). Eventbrite (alacsony budapesti volumen).

---

# 7. Pipeline

Minden szakasz tiszta függvény, `(list[Event], Config) -> list[Event]` alakú, kivéve ahol jelezve.
Sorrend kötött.

## 7.1 `normalize(raw: list[RawEvent], config) -> list[Event]`

- Dátum parseolás: `eventStart`-szerű ISO, magyar display formátumok (`2026. 08. 14. 19:00`),
  ISO 8601 `datetime` attribútum. Ismeretlen formátum → rekord eldobva + WARNING.
- Minden `datetime` `Europe/Budapest`-re tz-aware-ré alakítva.
- `district`: a `normalize_district` egyetlen függvényén megy át, bármilyen alakban jön.
  Elfogad egészet (`11`), római alakot (`XI.`, `XI`), magyar szöveget
  (`9. kerület - Ferencváros`, `IX. kerület`) és budapesti irányítószámot (`1113`) —
  mindegyikből `XI.` lesz. A forrás azt adja át, amit publikál; **nem** a forrás dolga
  átváltani. Ami nem ismerhető fel, az `None` és egy `district_unrecognised` debug sor,
  soha nem tipp: a §7.7 egyenlőséggel hasonlít, tehát a rossz kerület pontot ad, a hiányzó
  csak nem ad. A négyjegyű bemenet **mindig** irányítószám, és ott meg is áll — különben
  Szigethalom 2315-e XXIII. kerület lenne.
- `start_time_known`: a §7.7 hajnali eltolás, a §7.2 indulási kapu és a lenti múltbeli
  vágás bemenete. Az az egy bit, hogy a forrás közölt-e órát — a parser állítja be
  aszerint, melyik formátumra illeszkedett (`2026.09.19.` → False,
  `2026-08-16 01:00:00` → True), mert utólag a 00:00-ból ez már nem visszafejthető.
- `city`: a §7.6 földrajzi szűrésének bemenete, három lépcsőben. (1) amit a forrás mond
  (`RawEvent.city`); (2) az irányítószám — `1XYZ` = Budapest, más négyjegyű kód
  bizonyítja, hogy nem Budapest, de a település nevét **nem találjuk ki**, ott `None`
  marad; (3) csak ha egyáltalán nincs olvasható irányítószám: a „Budapest" szó a címben.
  A sorrend szándékos — így a „9026 Győr, Budapest út 5." nem lesz budapesti cím.
  A forrás által mondott város egy helyen normalizálódik: a „Budapest XI." és a
  „Budapest, XI. kerület" alakok „Budapest"-re rövidülnek, mert a §7.6 pontos egyezést
  néz, és különben egy budapesti esemény esne ki. Minden más településnél a hasonlítás
  szó szerinti.
- `description`: `html.unescape()`, whitespace normalizálás, 400 karakteren csonkolva.
- Ár: `price_raw`-ból regexszel (`"3 500 Ft"`, `"ingyenes"`, `"2000-4500 Ft"`).
  `ingyenes|free|díjtalan` → `is_free = True, price_min = 0`.
- Kerület: `district_raw`, ha nincs → irányítószámból, ha az sincs → `None`.
- `distance_km`: haversine a profil `home.lat/lon`-jától, ha van `lat/lon`.
- `effective_date`: §7.7.
- Horizonton kívüli és múltbeli események eldobva. **A vágás órát hasonlít, ha van óra,
  és dátumot, ha nincs.** Az óra nélküli rekord 00:00-ra esik, és az ott hiányzó érték
  (ugyanaz a gyökérok, mint a §7.7 hajnali eltolásánál) — időbélyegként hasonlítva minden
  éjfél utáni futás, tehát **minden** futás, eldobta a MAI dátumú, óra nélküli
  eseményeket, némán. Ezért `start_time_known: False` mellett a szabály `start.date() >=
  ma`; ismert óra mellett marad az időbélyeg, mert egy három órája elkezdődött koncert
  valóban elmúlt. Ahol a forrás közölt záró dátumot, a vágás azt olvassa, és annak a
  mezőnek a saját óra-bitjével: a „2026.08.22." záródátum az egész 22-ét jelenti. A
  horizont oldalán ugyanez a felosztás — ott ma egyetlen kimenetet sem változtat, mert a
  00:00 a nap első pillanata, tehát a két alak eleve egyetért; azért van kiírva, mert ez
  a parser tulajdonsága, nem a vágásé.
- A múltbeli eldobások **forrásonként** számlálódnak, és a futásösszegzőbe kerülnek
  (`dropped_as_past`). Egy forrás, amelynek a dátumai megállnak, nem hibázik és nem áll le:
  továbbra is tisztán parse-ol, csak minden rekordja `now` mögé kerül — a §13 drift-vizsgálat
  pedig a **parse-olt** rekordokat számolja, tehát ezt nem látja.

## 7.2 `dedup(events, config) -> list[Event]`

Normalizáló segédfüggvények (a dedup és az `id` is ezt használja):

```python
def normalize_title(s: str) -> str:
    # kisbetűsítés, ékezet-eltávolítás (unicodedata NFKD),
    # zárójeles utótagok levágása: "(Budapest)", "(HU)", "(UK)"
    # elválasztó utáni helyszín-utótag levágása: " | A38", " - élő koncert"
    # többszörös whitespace -> egy szóköz
```

Három szint, ebben a sorrendben:

1. **Exact:** azonos `id` → merge.
2. **Strong:** normalizált URL egyezés (query string, UTM, fragment levágva) → merge.
3. **Fuzzy:** `rapidfuzz.fuzz.token_set_ratio(t_a, t_b) >= 88`
   **ÉS** `abs(start_a - start_b) <= 90 perc`
   **ÉS** (`token_set_ratio(venue_a, venue_b) >= 85` **VAGY** az egyik venue `None`).

**Indulási kapu:** 90 perc, ha **mindkét** rekord órája ismert; **azonos naptári nap**, ha
bármelyiké nem (`start_time_known`, §7.1). Óra nélküli forrás 00:00-ra esik, tehát a 90
perces szabály alatt csak 00:00–01:30 között indulókkal volt egyáltalán összevethető — ez
állandó, néma vakfolt volt, nem hangolási kérdés. A cím- és helyszínkapu változatlan.

**Merge szabály:** a kisebb `priority` értékű forrás rekordja a bázis.

**Az invariáns: a merge soha nem csökkent információt.** Ha a bázis mezője `None` és a
másik rekordé nem, a **másiké nyer** — ez az alapértelmezés **minden** skalár mezőre, és
egyszer van megírva, nem mezőnként. A mezőlista magából az `Event` modellből származik
(`FILL_IF_MISSING_FIELDS`), tehát egy később hozzáadott mező **automatikusan** benne van.

Korábban ez fordítva működött: minden kitöltendő mező kapott egy saját `if base.x is None`
sort, és amelyik nem kapott, az csendben megtartotta a bázis `None`-ját. Ez ártalmatlan
volt, amíg minden mező kozmetikai vagy pontozási célú — és megszűnt annak lenni a `city`
érkezésekor, mert a §7.6 **kizárhat** város alapján: egy város nélküli bázis (port-hu)
felülírta volna azt a forrást, amelyik ismerte a települést (cooltix), és `allow_missing_city:
false` mellett kiesett volna egy esemény, amiről mindkét forrás tudta, hogy budapesti.

Négy kivétel, mindegyik egy helyen felsorolva:

| csoport | mezők | szabály |
|---|---|---|
| unió | `source_ids`, `urls`, `categories`, `native_categories` | mindkettő hozzátesz |
| leghosszabb nyer | `description` | akkor is, ha a bázisnak van |
| **csatolt** | `price_min`+`price_max`+`is_free`; `lat`+`lon`+`distance_km` | egységként töltődik |
| a bázis nyer | `id`, `title`, `start`, `effective_date`, `start_time_known`, `group_key`, `group_size`, `score`, `score_breakdown` | identitás, vagy egy későbbi szakasz tulajdona |

A **csatolt** csoportok azért nem mezőnként töltődnek, mert félig kitöltött állapot
keletkezne: egy `is_free: true` a bázisról, mellette a másik rekord `price_max`-ja, vagy
egy `distance_km`, amit más koordinátákból számoltak, mint a mellette álló `lat/lon`.

A `start_time_known` azért a bázisé, mert csak a `start`-tal együtt értelmes: `True`-t
átvenni egy olyan rekordtól, aminek a `start`-ját nem vesszük át, azt jelentené, hogy
valódi órát állítunk egy éjféli helyőrzőre (§7.1).

Minden merge döntés a futásnaplóba kerül (`source_a`, `source_b`, `score`, `reason`).
A 80-88 közötti fuzzy sáv **nem** merge-el, de `ambiguous_dedup` jelöléssel naplózódik —
ez az opcionális LLM hook bemenete.

## 7.3 `recurrence(events, config) -> list[Event]`

A mintaadat két esetet mutat:
- **Fut egy ideig:** `2026-08-14` → `2026-08-21` (egy hét)
- **Sorozat egy rekordként:** `2026-05-06` → `2026-09-30`, "minden szerdán"

```python
if event.end and (event.end - event.start).days > config.recurrence.series_threshold_days:
    event.is_series = True
```

`series_behavior: send_once` → a ledger úgy kezeli, mint bármely eseményt: egyszer kimegy,
utána néma. `run_behavior: send_at_start` → a többnapos futás a kezdőnapján megy ki.

**A repertoár-kockázat: felmérve, nem cáfolva.** Egy színházi repertoár a legrosszabb eset
ezeknek a szabályoknak: ugyanaz a produkció húszszor megy három hónap alatt. Ha egy forrás
ezt **húsz külön rekordként** adja, sem a §7.3, sem a §7.4 nem fogja meg — a recurrence
egyetlen rekord `end - start` távolságát nézi, a grouping kulcsa pedig
`(venue_name, effective_date, primary_category)`, és ezeknél épp a dátum tér el. A mentett
fixture-ökön ez **ma nem fordul elő**: a Port.hu 20 rekordjában nincs ismétlődő
(cím, helyszín) pár, viszont van két valódi dátumtartomány, és a §7.3 helyesen sorozatnak
jelöli a „HØT SPØT 2026 / Every Wednesday" 2026-05-06 → 2026-09-30 rekordot. A
bigcitylife 8 rekordja szintén egyedi, tartomány nélkül. Tehát minden jelenlegi forrás a
**jó** alakot adja (egy rekord tartománnyal), a rossz alak pedig **megfigyeletlen**, nem
kizárt — az első valódi repertoárforrás hozná be. Addig nem vezetünk be új grouping
szabályt: nincs mihez tervezni.

## 7.4 `group(events, config) -> list[Event]`

**Kötelező, nem opcionális.** A Port.hu mintaadatban húsz rekordból tizenhét egyetlen fesztivál
(Sziget) fellépése volt. Összevonás nélkül egy többnapos fesztivál augusztusban kiszorítana
mindent.

```
ha venue_name is None:
    az esemény kimarad a csoportosításból, egyedül megy tovább
csoportkulcs = (venue_name, effective_date, primary_category)
ha a csoport mérete >= min_group_size (4):
    egyetlen összevont Event jön létre:
        title       = f"{venue_name} — {len(group)} program"
        score       = max(e.score for e in group)     # a scoring UTÁN fut, lásd sorrend
        group_size  = len(group)
        urls        = [a helyszín gyűjtő URL-je, ha van, különben a legmagasabb pontszámú tagé]
        description = a 3 legmagasabb pontszámú tag címe, vesszővel
egyébként:
    a csoport tagjai változatlanul mennek tovább, de venue-nként
    legfeljebb max_per_venue (3) darab, pontszám szerint
```

**Helyszín nélküli esemény nem csoportosul.** A §7.4 azért létezik, hogy **egy** fesztivál
**egy** helyszínen ne szorítson ki mindent. A `venue_name is None` rekordok viszont nem
helyszínt osztanak, hanem a helyszín hiányát: a belőlük képzett kosár azt jelenti, hogy
„minden helyszín nélküli X kategóriájú esemény Y napon" — egymáshoz semmi közük, és az
összevonás valódi, különböző eseményeket rejtene el egy értelmetlen összegző sor mögött.
A programturizmus mind a 20 kártyája ilyen volt (a helyszínsáv csak megye/város/kerület), és
a régi kulcs ezekből `"None — 4 program"` című sorokat gyártott, ami így ment volna ki a
levélben. Az a forrás 2026-08-22-én kikerült (§6.6), tehát ma egyetlen fixture sem gyárt
helyszín nélküli rekordot — a szabály marad, a `tests/test_group.py` őrzi. Ezek az események tehát **változatlanul, egyenként** haladnak tovább, és a
`max_per_venue` sem vonatkozik rájuk — nincs mit korlátozni. A kimaradás naplózódik
(`grouping_skipped_venueless`, forrásonként) és bekerül a futásösszegzőbe
(`ungrouped_venueless`), hogy egy forrás, amelyik elkezd helyszín nélkül publikálni,
látszódjon, ne csak csendben átformálja a digestet.

**Fontos sorrend:** a `group` a `score` UTÁN fut, mert a csoport pontszáma a tagok
pontszámától függ. A pipeline tényleges sorrendje tehát:
`normalize → dedup → recurrence → categorize → filter → score → group → limit`.

## 7.5 `categorize(events, config) -> list[Event]`

Kategóriánként pontszám négy jelből: `keywords` (cím + leírás, súlyozva),
`venue_prior`, `url_patterns`, `native_types`. A `native_types` egyezés **erős**: +4.

**A `venue_prior` fuzzyn illeszt, ugyanazzal az összehasonlítással, mint a §7.2 harmadik
kapuja** (`venue_matches`, `token_set_ratio >= 85`). Korábban `normalize_venue` utáni
pontos egyezés volt, ami egy forrásnál működik, többnél nem: a Cooltix
„Red&Black Társasjátékszalon"-t ad, egy másik oldal „Red and Black"-et, egy harmadik
„Red & Black Társasjáték Szalon"-t — pontos egyezésnél mindegyik külön configsort kér, és
ha hiányzik, a bónusz **némán** nem tüzel. Ezért a configban a helyszín **neve** áll, nem
egy forrás írásmódja.

A `normalize_venue` a központozást meghagyja, ami egyenlőséghez elég, tokenizáláshoz nem:
a „Red&Black" **egy** token, tehát a „Red & Black"-kel semmi közös nincs benne
(`token_set_ratio` = 47). A `venue_matches` ezért előbb központozás mentén szétvágja a
nevet — így mindkettő „red black", az érték 100. A 85-ös küszöb mérve tartható: a mentett
fixture-ök 27 valódi budapesti helyszínnevén minden szándékolt találat 100, a legközelebbi
nem szándékolt pár a „Kobuci Kert" ~ „Kopaszi Kert" 70-nel — a küszöb a résben ül, nem a
peremén. A szétvágás a §7.2 viselkedését nem változtatja: 351 helyszínpárból **egy sem**
kerül át a küszöb másik oldalára tőle.

**A rothadás látszik.** Egy `venue_prior` bejegyzés állítás a világról, és az állítások
avulnak — a helyszín bezár, nevet vált, vagy egyszerűen egyik forrás sem hozza. Ezért a
`categorize` futásonként egyszer, kategóriánként `venue_prior_unmatched` INFO sorban
kiírja azokat a bejegyzéseket, amelyek az **egész** merítésben semmire nem illeszkedtek.

**Kulcsszó-illesztés: szóeleji, nem teljes szavas.** A magyar toldalékol, ezért a teljes
szavas illesztés minden kategóriában alulmért: a „társasjáték" nem fogta a
„Társasjátékos"-t, a „koncert" a „koncertje"-t, a „mérkőzés" a „mérkőzése"-t, az „előadás"
az „előadásában"-t. A `contains_word` ezért **szó elején horgonyoz, és a végén nyitva
hagyja**: `(?<!\w)kulcsszó`. Szó közepén továbbra sem talál — a „koncert" nem tüzel a
„szimfonikuskoncert" belsejében.

Két korlát:

- **5 karakternél rövidebb kulcsszó marad teljes szavas.** Rövid tő túl sok idegen szó
  eleje: a `rave` a „ravasz"-ra, a `piac` a „piackutatás"-ra, a `dj` a „djembe"-re tüzelne.
  Ennek ára is van, és vállaljuk: a `film` így nem fogja a „filmek"-et.
- **A `$` végződésű kulcsszó kimarad a szóeleji illesztésből** és csak pontos szóra
  illeszkedik. Ez a menekülőút arra a ritka tőre, ami túlilleszt. A párja a `*`, ami
  szóeleji illesztést kér ott, ahol nem az az alapértelmezés (§5.2).

Amit a szóeleji illesztés **nem** tud: a toldalékot megkülönböztetni az összetételtől. A
magyar egybeírja az összetett szavakat, így a „koncertje" (kell) és a „koncertterem"
(nem kell) alakilag azonos. A csere ezt tudatosan vállalja — a mentett Port.hu, Cooltix
és kvizestek fixture-ökön mérve 12 új találat, ebből 11 helyes —, a `$` pedig ott van
arra az esetre, amikor egy konkrét tő mégis rosszul viselkedik.

Ugyanez a függvény szolgálja a §7.6 `blocked_keywords`-öt és a §7.7 `keyword_boosts`-ot —
a toldalékolás nem a kategorizálás sajátja. A **függvény** tehát közös, az
**alapértelmezése** nem: a `keyword_boosts` a kategóriákkal együtt szóeleji, a
`blocked_keywords` viszont teljes szavas, mert ott a téves találat nem félrecímkéz, hanem
eldob (§5.2 táblázata). Egy időben mindhárom szóeleji volt; ez a `blocked_keywords`-nél
fail-closed viselkedés volt, és 2026-08-22-én visszaállt teljes szavasra.

A legmagasabb pontszámú kategória a `primary_category`; minden `min_category_score` fölötti
bekerül a `categories` listába. Ha egy sem éri el, `fallback_category` (`egyeb`).

`digest categorize --explain <id>` kiírja a pontok eredetét.

**Opcionális Gemini réteg.** `Categorizer` protokoll, két implementáció. Csak akkor hívódik,
ha `llm.enabled` és az esemény `egyeb` lett és a leírás > 200 karakter.

Kvótaszámítás: ~12 forrás × ~40 esemény ≈ 480 nyers, dedup után ~300; ebből ~25% `egyeb`
→ 75 esemény → 35-ös kötegekkel 3 hívás, plusz 1-2 a dedup-párokra. **Napi 4-6 hívás.**
A publikus források a napi kvótára 250 / 1000 / 1500 közötti számokat mondanak, csúcsidőben
kevesebbet — a kötegelés miatt ez mindegy. **Az `on_quota_error: fallback_to_rules` nem opció,
hanem követelmény: az LLM soha nem lehet a pipeline kritikus útján.**

## 7.6 `filter(events, config) -> list[Event]`

Kizár: horizonton kívül · **földrajz** · nem engedett kategória ·
`price_min > max_price_huf` · `blocked_keywords` egyezés · a ledger szerint már kiküldött
(§8.2) · `min_score` alatt.

**Földrajzi kizárás.** Több forrás országos — a kvizestek végpontja 132 rekordból 41-et ad
Budapesten kívül, és a jegyértékesítők ugyanilyenek. Enélkül a
szabály forrásonként íródna újra, egymástól eltérően. Három ok, mindegyik `filters.geo`
alól (§5.2), és mindegyik **nyitva bukik**, ha hiányzik a tény, amire szüksége van:

| ok | mikor | mit logol |
|---|---|---|
| `geo_city_mismatch` | van `city`, és nem a beállított település | `city`, `expected` |
| `geo_city_missing` | nincs `city`, és `allow_missing_city: false` | `city`, `expected` |
| `geo_too_far` | `distance_km > filters.geo.max_distance_km` | `distance_km`, `max_distance_km` |

`allow_missing_city` alapból **true**: a legtöbb forrás egyáltalán nem ad települést, és a
kidobásuk csendben veszítene el jó eseményeket. A `filters.geo.max_distance_km` **kemény
kizárás**, és nem azonos a `scoring.proximity.penalty_cap_km`-mel, ami csak a pontlevonást
határolja — a kettőt soha nem vonjuk össze. 2026-08-22-ig **azonos nevűek** voltak, és a
scoringoldali nem csinált semmit; ez a névütközés volt a csapda.

Az `_exclusion_reason` az **első** találó okot adja vissza, tehát egy horizonton kívüli
*és* vidéki esemény `beyond_horizon`-ként számít. A futásösszegző `dropped_by_geo` mezője
ezért „földrajz miatt kizárva", nem „hány esemény esett Budapesten kívülre".

**A `city` túléli a dedupot.** A §7.2 merge a kisebb `priority`-jű rekordot veszi
bázisnak, és a `city`-re ugyanaz a „töltsd ki, ha hiányzik" szabály vonatkozik, mint a
`district`-re — csak itt nem kozmetika: ha egy cím nélküli bázis felülírná azt a forrást,
amelyik ismeri a települést, egy budapesti esemény esne ki ismeretlenként.

**A forrásszintű szűrés marad.** Ahol egy forrás tud településre szűrni (kvizestek,
cooltix), ott szűrjön: ne töltsük le, amit úgyis eldobunk. Ez udvariassági kérdés — a
mérvadó szabály ez a szakasz, nem a forrás.

## 7.7 `score(events, config) -> list[Event]`

```
score = category_weight(primary_category)
      + Σ keyword_boosts ahol a kulcsszó szerepel a címben vagy leírásban
      + (free_bonus ha is_free)
      + (cheap_bonus.points ha price_min < cheap_bonus.under_huf)
      + (proximity.same_district_bonus ha district == home.district)
      - (min(distance_km, proximity.penalty_cap_km) * proximity.distance_penalty_per_km)
      + (novelty_bonus ha most jelent meg először a ledgerben)
      + (soon_bonus.points ha start - now <= soon_bonus.within_days)
      + weekday_weights[effective_date.weekday()]
```

**A távolság-büntetés felső határa.** A levonás `min(distance_km, penalty_cap_km)`-re
számol, tehát a távoli esemény hátrébb kerül, de nem nyomja el a képlet többi tagját: a
sablon 0.3/km-jével egy 40 km-es esemény korlát nélkül -12-t kapna, ami több, mint az
összes kategóriasúly, kulcsszóbónusz és jutalom együtt. A mező deklarálva és dokumentálva
volt, de a `score.py` **soha nem olvasta** — a viselkedés nem létezett. A `penalty_cap_km`
**nem** zár ki semmit; az a `filters.geo.max_distance_km` dolga (§7.6), és a kettő
2026-08-22-ig azonos nevű volt.

Minden tag bekerül a `score_breakdown` dictbe a saját nevén — ez hajtja a `digest explain`
parancsot (a Pages UI-nak nincs ilyen nézete, lásd §9.0 AUDIT-1 BLOCKER-2).

**Hajnali eltolás.** A fesztiválszettek 00:00 és 05:00 közé esnek, de az **előző** estéhez
tartoznak. Eltolás nélkül egy péntek éjjeli 02:00-s buli szombati `weekday_weight`-et kapna.

```python
if not event.start_time_known:
    effective_date = start.date()
else:
    effective_date = (start - timedelta(hours=config.night_shift.before_hour)).date()
```

**Csak ismert órára.** Ha a forrás óra nélküli dátumot közöl, a parser 00:00-t ad — az ott
**hiányzó érték, nem időpont**. Öt órát visszalépni belőle egy nappal korábbra iktatja az
eseményt; a programturizmus mind a 20 rekordja így volt hibás (az a forrás azóta kikerült,
§6.6). A döntést a §7.1 parsere hozza meg, abból, hogy melyik formátumra illeszkedett (`Event.start_time_known`), és
**soha nem** a `start.time() == éjfél` vizsgálatból: valódi éjféli esemény létezik, és annak
tovább kell tolódnia. A Port.hu időbélyegei valódi órát hordoznak, tehát a 01:00-s és
03:00-s szettjei változatlanul az előző estéhez kerülnek.

---

# 8. Állapot

## 8.1 `state/state.json`

A teljes eseménytörzsek **nem** élik túl a futást — az email amúgy is tartalmazza őket.
Egyetlen dolognak muszáj átmennie: mit küldtünk már ki. Enélkül egy két hét múlva induló
koncert 14 egymást követő reggelen bekerülne a hírlevélbe.

```json
{
  "version": 1,
  "last_run": "2026-08-16T04:34:11Z",
  "sent": [
    { "id": "a3f9c21e8b04d7f6", "t": "sub focus", "d": "2026-08-29", "s": "2026-08-16", "u": "2026-08-29" }
  ],
  "source_health": {
    "port-hu": {
      "consecutive_failures": 0,
      "last_ok": "2026-08-16",
      "last_count": 312,
      "etag": "W/\"a1b2c3\"",
      "disabled_until": null
    }
  },
  "run_log": [
    { "date": "2026-08-16", "raw": 487, "after_dedup": 301, "sent": 18, "seconds": 142 }
  ]
}
```

Mezőrövidítések a `sent` tömbben szándékosak (`t` = normalizált cím, `d` = esemény dátuma,
`s` = kiküldés dátuma, `u` = eddig védett — lásd lent) — 2000 bejegyzésnél ez ~120 KB
helyett ~180 KB-ot spórol.

**`u` külön mező, nem `d` (AUDIT-5 BLOCKER, javítás).** Egy még futó esemény (több napos
kiállítás, heti sorozat) a `normalize()` "még nem múlt el" kivétele miatt napokig,
hetekig a pipeline-ban marad — de a `d` mező az *első* megjelenés dátumára fagy. Ha a
purge is `d`-t nézné, a ledger védelme egy nappal az első kiküldés után lejárna, miközben
az esemény még mindig újra és újra "új"-ként jelenne meg. `u` az esemény tényleges
záródátuma (`event.end.date()`, ha van `end`; egyébként ugyanaz, mint `d`) — eddig
tartja életben a ledger bejegyzést a `purge()`.

## 8.2 Műveletek

```python
def purge(state, today) -> State:
    """Minden `sent` bejegyzés törlődik, aminek a `u` mezője < today."""

def was_sent(state, event) -> bool:
    """Exact id egyezés VAGY (azonos `d` ÉS token_set_ratio(t, title_norm) >= 92).
    A fuzzy ág azért kell, mert a forrás átírhatja a címet, és akkor az id megváltozik."""
```

`run_log`: csak az utolsó 30 futás marad meg.

---

# 9. Renderelés

## 9.0 Két renderelési profil — kötelező

A két kimenet **jogilag és gyakorlatilag különböző helyzetben van**, ezért különböző
mezőkészletet kap.

| | `email` profil | `web` profil |
|---|---|---|
| Jelleg | személyes használat, egy címzett | **publikálás a nyílt neten** |
| Cím, időpont, helyszín, kerület | ✅ | ✅ |
| Ár, kategória, pontszám | ✅ | ✅ |
| **Pontszám-bontás** (miért ennyi: `score_breakdown`) | ✅ | ❌ |
| **Átvett leírás** | ✅ | ❌ |
| **Forrásoldali kép** | ✅ (hotlink elfogadható) | ❌ **soha, se beágyazva, se hotlinkelve** |
| Link a forráshoz | ✅ | ✅ (kötelező, minden tételen) |

**Indoklás.** Az emailbe bemásolni mások leírását és bélyegképét személyes felhasználás.
A Pages oldal viszont publikálás: ott mások által írt szöveget tennénk ki a nyílt netre, és
hotlinkelt képekkel az ő sávszélességüket használnánk. Ettől néz ki a projekt konkurens
aggregátornak a személyes eszköz helyett. A szűrhetőség ettől nem sérül, mert az a
strukturált mezőkön megy.

**A pontszám-bontás nem publikus (AUDIT-1 BLOCKER-2, döntés).** A `score_breakdown`
egyes tagjai szó szerint a privát profil számai — `breakdown.category` pontosan
`category_weights[az esemény kategóriája]`, `breakdown.weekday` pontosan
`weekday_weights[az esemény napja]`, következtetés nélkül. Ez ugyanaz a "olvasható térkép
az ízlésedről" probléma, amiért a `scoring` blokk egyáltalán a `PROFILE_YAML` secretben él
(§12) — a bontás publikálása megkerülte volna a szétvágást. Az összesített `score` marad
publikus (rendezéshez, a pontsávhoz), csak a tagonkénti bontás nem. Ez a döntés kizárólag
a `web` profilt érinti; a `render/email.py` felé menő `Event` objektumokból ez a javítás
semmit nem vágott le.

Következmény: az `events.json`-ban **nincs `description`, nincs `image` és nincs
`breakdown` mező**. A `render/web.py` explicit mezőlistával épít, nem `model_dump()`-pal —
hogy egy jövőbeli új mező ne szivárogjon ki magától.

## 9.1 Sablonok

Három kimenet, mind Jinja2:

| Sablon | Kimenet | Megkötés |
|---|---|---|
| `email.html.j2` | SMTP HTML body | táblázat-alapú, inline CSS, max 600px, lásd Claude Design brief |
| `email.txt.j2` | plain text alternatíva | kötelező, nem opcionális |
| `index.html.j2` | `site/index.html` | statikus, kliensoldali szűrés `events.json` fölött |
| `status.html.j2` | `site/status.html` | forrás-health tábla |

A `render/web.py` az `index.html` mellé kiírja a `site/events.json`-t is. **A `web` profil
szerint (§9.0): nincs benne `description`, `image`, sem `breakdown`.**

```json
{
  "generated_at": "2026-08-16T04:34:11Z",
  "events": [
    {
      "id": "a3f9c21e8b04d7f6",
      "title": "...",
      "url": "...",
      "start": "2026-08-29T20:00:00+02:00",
      "venue": "A38 Hajó",
      "district": "XI.",
      "categories": ["koncert"],
      "price_min": 4500,
      "is_free": false,
      "score": 11.3,
      "group_size": 1
    }
  ]
}
```

Az archívum `site/archive/YYYY-MM-DD.html` néven **a `web` profillal** renderelődik —
nem az email HTML másolata. `archive_keep_days` fölött a régiek törlődnek a commit előtt.

**Kötelező teszt (`test_web_render.py`):** az `events.json` egyetlen rekordja sem
tartalmazhat `description`, `image` vagy `breakdown` kulcsot, és a generált `site/*.html`
egyetlen `<img>` tagje sem mutathat forrásoldali domainre.

---

# 10. Delivery

```python
class Deliverer(Protocol):
    type: str
    def send(self, subject: str, html: str, text: str, config: Config) -> bool: ...
```

- **smtp**: Gmail app password, `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` envből,
  `recipient_email` a profilból. `EmailMessage` multipart/alternative.
- **telegram**: rövidített változat, csak a top 5, Markdown.

**A visszatérési érték kötelező (AUDIT-2 BLOCKER).** `send` `True`-t ad vissza, ha ténylegesen
kiment valami, és `False`-t egy kegyelmi, szándékos no-op esetén (pl. hiányzó
`recipient_email`, §5.3). A hívó (`cli._deliver`) ez alapján dönti el, hogy `record_sent`
lefusson-e — enélkül egy hiányzó/hibás `PROFILE_YAML` némán és véglegesen "kiküldöttként"
jelölné meg az adott nap összes eseményét, holott soha senkihez nem jutottak el.

`newsletter.send_when_empty: true` → **a hírlevél akkor is kimegy, ha nulla új program van**,
"ma 0 új találat" sorral. Így az email *hiánya* a riasztás. Ez a rendszer teljes monitoringja —
de csak akkor, ha a `send_when_empty` melletti tényleges kiküldés is sikerül; ha a levelezés
maga hibázik, ugyanez a hiba vonatkozik rá is.

---

# 11. GitHub Actions

`.github/workflows/digest.yml`:

```yaml
name: digest

on:
  schedule:
    - cron: "30 4 * * *"        # ~06:30 Europe/Budapest nyáron
  workflow_dispatch:

permissions:
  contents: write
  pages: write
  id-token: write

concurrency:
  group: digest
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -e .

      - name: Run digest
        env:
          PROFILE_YAML:    ${{ secrets.PROFILE_YAML }}
          SMTP_HOST:       ${{ secrets.SMTP_HOST }}
          SMTP_USER:       ${{ secrets.SMTP_USER }}
          SMTP_PASSWORD:   ${{ secrets.SMTP_PASSWORD }}
          GEMINI_API_KEY:  ${{ secrets.GEMINI_API_KEY }}
        run: digest run

      - name: Commit state
        run: |
          git config user.name  "digest-bot"
          git config user.email "digest-bot@users.noreply.github.com"
          git add state/state.json site/
          git diff --staged --quiet || git commit -m "chore: digest run $(date -u +%F)"
          git push

      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: site
      - id: deploy
        uses: actions/deploy-pages@v4
```

**Költség.** Privát repóban havi 2000 ingyenes Linux-perc, publikusban korlátlan.
Napi 3-5 perces futás ≈ 150 perc/hó.

**A 60 napos szabály.** Publikus repóban az ütemezett workflow letiltódik 60 nap repo-aktivitás
nélkül. "Aktivitás" = commit a default branchre; issue-komment és csillagozás nem számít.
Nálunk a napi `state.json` commit ezt lefedi, külön keepalive nem kell.

**Az ütemezés nem pontos.** A GitHub cron UTC-ben jár, csúcsidőben 5-30 perc csúszás normális,
és explicit "best effort" SLA nélkül. Reggeli hírlevélnél ez irreleváns. A nyári/téli
időszámítás nincs kezelve — vagy elfogadod az egy óra elcsúszást, vagy évente kétszer átírod.

---

# 12. Biztonság és configszétvágás

| Hol | Mi |
|---|---|
| repo, publikus | `sources/*.yaml`, kategória-szabályok, `schedule`, sablonok, kód |
| Actions Secret `PROFILE_YAML` | pontozási súlyok, `keyword_boosts`, `home`, `recipient_email`, `filters` |
| Actions Secrets | `SMTP_*`, `GEMINI_API_KEY` |

A profil azért secret, mert a súlyok, a kulcsszó-boostok és a kerület együtt **olvasható térkép
az ízlésedről és arról, hol laksz.** A `test_config_privacy.py` védőteszt ezt kényszeríti ki
(mind a `config.yaml`-ra, mind a `sources/*.yaml`-ra).

A Pages oldal publikus (a privát Pages Enterprise Cloudot igényelne). A tartalma nyilvános
programlisták, tehát ez nem szivárogtat semmit — feltéve, hogy a szétvágás megvan.
Az URL-be kerüljön egy véletlen szegmens, ha nem szeretnéd, hogy találomra megtalálják.

**Elfogadott, nem javított kockázat (AUDIT-3 MAJOR-3, döntés).** A publikus `events.json`
a `filters` szerint már megszűrt eseménylista — aki ugyanazokat a nyilvános forrásokat
(port.hu, bigcitylife, …) is böngészi, és a kettőt összeveti, közvetetten következtethet
arra, mit zárt ki a `filters` (mely kategóriák, hozzávetőleges ár-plafon, mely kulcsszavak
sosem jelennek meg). Ez nem kódhiba — a pipeline pontosan a specifikált sorrendben szűr —,
hanem szerkezeti következménye annak, hogy "publikálj egy szűrt nézetet nyilvános
forrásokból". Elfogadva: motivált támadót és tényleges korrelációs munkát igényel, nem
egyetlen paranccsal kiolvasható adat, és a `filters.categories` amúgy is a `config.yaml`
teljesen publikus kategórialistájának nagy részhalmaza — a marginális információ kicsi.
Nem lett belőle kód-változtatás a `render/web.py`-ban.

---

# 13. Hibakezelés és health

- **Per-source izoláció:** egy forrás hibája sosem buktatja el a futást.
- **Selector drift:** ha egy forrás korábban >10 eseményt adott és most 0-t, az nem
  "nincs program", hanem törött parser → `ERROR` szint és megjelenik a `/status.html`-en.
- **Auto-disable:** 5 egymást követő hiba után `disabled_until = today + 7 nap`.
- **Strukturált logolás** (`structlog`), futás végén összefoglaló: forrásonként darabszám,
  dedup merge-ek, kiszűrtek, időtartam.
- Ha a teljes futás elhasal, a GitHub emailben értesít a workflow hibáról.

---

# 14. Tesztelés

- `pytest` + `respx` (httpx mockolás). **Egyetlen teszt sem megy ki a hálózatra.**
- Minden forráshoz egy valós, lementett fixture a `tests/fixtures/` alatt.
  A Port.hu fixture a már meglévő minta legyen, csonkolatlanul.
- Külön teszt minden pipeline szakaszra, kézzel írt `Event` listákkal.
- **Kötelező regressziós tesztek:**
  - `test_group.py`: 17 azonos helyszínű esemény → 1 összevont sor
  - `test_recurrence.py`: 5 hónapos rekord → `is_series = True`
  - `test_score.py`: 02:00-s péntek éjjeli esemény pénteki súlyt kap
  - `test_state.py`: átírt cím esetén a fuzzy ág elfogja
  - `test_config_privacy.py`: a publikus config nem tartalmaz profil-kulcsot
  - `test_source_port_hu.py`: `gallery` nem kerül a `RawEvent`-be; `1113` → `XI.`

---

# 15. Tech stack

Python 3.12 · `httpx` · `selectolax` · `pydantic v2` · `Jinja2` · `rapidfuzz` · `PyYAML` ·
`typer` · `structlog` · `google-genai` (opcionális extra) · `pytest` + `respx` · `ruff`

Nincs: adatbázis-szerver, webframework, Docker, böngésző, VPS, ORM.
`pyproject.toml`, `[project.scripts] digest = "digest.cli:app"`.

---

# 16. Mérföldkövek

| # | Tartalom | Kimenet |
|---|---|---|
| M0 | Repo, `CLAUDE.md`, modellek, config-betöltés, CLI váz, Port.hu plugin | `digest fetch port-hu` fut fixture-ből |
| M1 | normalize + dedup + recurrence, Jegy.hu és Meetup | `digest run --dry` rendezett listát ad |
| M2 | categorize + filter + score + group, `state.json` ledger | pontozott, összevont lista |
| M3 | email sablonok, SMTP delivery, "0 találat" heartbeat | első valódi hírlevél lokálisan |
| M4 | GitHub Actions, secrets, `PROFILE_YAML` merge, state-commit | autonóm napi futás |
| M5 | deklaratív YAML motor + 5 SSR forrás | forrás hozzáadása kód nélkül |
| M6 | Pages deploy, `events.json`, olvasó UI, `status.html` | böngészhető, szűrhető archívum |
| M7 | Gemini réteg, `.ics` export, Telegram | finomhangolás |
| M8 | Író UI: PAT + Contents API + `workflow_dispatch`; Ticketswap enricher | böngészőből kapcsolható |

**M0-M4 után a rendszer működik és hasznos.** Minden további javítás, nem feltétel.

---

# 17. Nyitott kérdések

1. **Port.hu listázó végpont pontos URL-je és paraméterei** — ez blokkolja az M0 véglegesítését.
2. **A Port.hu `type` mező teljes szótára** — a minta csak `"concert"`-et tartalmazott.
3. **Meetup csoport-slugok** — konkrét lista kell.
4. **Delivery:** csak email, vagy Telegram is?
5. **Fesztiválok:** az összevont sor elég, vagy a nagy fesztiváloknak külön szekció kell?

**Eldőlt:** futtatás GitHub Actionsön · Playwright kimarad · publikus repo + configszétvágás ·
UI kétlépcsős (olvasó M6, író M8) · **geokódolás nem kell** (a Port.hu ad `lat`/`lon`-t,
a kerület az irányítószámból determinisztikus).

---

# 18. Design

Az email és a webes olvasó UI vizuális terve külön briefből készül
(`claude-design-brief.md`). Az elkészült Claude Design artefakt linkje:

**Claude Design link:** _<ide illeszd be>_

A sablonok abból a HTML-ből származnak: az agent **nem generálja újra a markupot**, csak
Jinja2 változókat és ciklusokat helyez el benne.
