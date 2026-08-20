# Budapest Event Digest

Napi egyszer futó Python batch job: budapesti programokat gyűjt több forrásból,
deduplikál, pontoz, és emailben kiküldi az újakat. Nincs szerver, nincs adatbázis.

A teljes technikai terv a [SPEC.md](SPEC.md)-ben, az agent-konvenciók a
[CLAUDE.md](CLAUDE.md)-ben vannak.

## Telepítés

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Használat

```bash
digest --help
```

## Teszt

```bash
pytest -v
ruff check .
```

## GitHub Secrets

A `.github/workflows/digest.yml` ezt az öt secretet olvassa (Settings → Secrets and
variables → Actions → New repository secret):

| Secret | Kötelező? | Mire kell |
|---|---|---|
| `PROFILE_YAML` | igen | A privát profil — pontozási súlyok, `home`, `recipient_email`, `filters` (SPEC.md §5.2). Ennek hiányában a futás lemegy, de személyre szabás nélkül (§5.3), és email sem megy ki, mert nincs `recipient_email`. |
| `SMTP_HOST` | igen az emailhez | Az SMTP szerver címe, pl. `smtp.gmail.com`. |
| `SMTP_USER` | igen az emailhez | A küldő fiók, Gmailnél a teljes email cím. |
| `SMTP_PASSWORD` | igen az emailhez | Gmailnél **app password**, nem a fiók jelszava — Google Account → Security → 2-Step Verification → App passwords. |
| `GEMINI_API_KEY` | nem | Opcionális, a jövőbeli LLM-kategorizáláshoz (`llm.enabled: false` alapból, CLAUDE.md 4). |

A `PROFILE_YAML` tartalma egy teljes YAML dokumentum egy sorban/több sorban, pl.:

```yaml
recipient_email: "te@example.com"
home:
  district: "XI."
  lat: 47.47
  lon: 19.05
scoring:
  category_weights: { koncert: 4, tarsasjatek: 5 }
filters:
  min_score: 3
```

## Helyi futtatás

Valódi forrásfetch és email-küldés nélkül, egy lementett fixture-ből renderelve:

```bash
digest run --dry --source port-hu --fixture tests/fixtures/port_hu_list.json --out preview.html
open preview.html   # vagy: a fájlt bármelyik böngészőben
```

A teljes, éles futtatáshoz (valódi fetch, valódi email, `state/state.json` írása) a
fenti secreteknek környezeti változóként kell elérhetőnek lenniük:

```bash
export PROFILE_YAML="$(cat profile.local.yaml)"   # ne kerüljön commitba
export SMTP_HOST=smtp.gmail.com SMTP_USER=... SMTP_PASSWORD=...
digest run
```

## Manuális indítás

A workflow `workflow_dispatch`-csel is indítható a normál napi cron mellett: a repo
Actions fülén a **digest** workflow, majd **Run workflow**. Ugyanazt csinálja, mint az
éjszakai futás — ugyanazokat a secreteket olvassa, ugyanúgy commitolja a `state/state.json`-t.

## Böngészős író UI

A publikus oldalon (`site/index.html`) van egy ⚙ gomb jobb fent. Amíg nincs elmentve
token, ez a panel csak egy token-beviteli mezőt mutat — az oldal többi része (szűrés,
keresés) mindig működik token nélkül is, mert az olvasáshoz nem kell hitelesítés.

### A token beszerzése

A GitHub-on: **Settings → Developer settings → Personal access tokens → Fine-grained
tokens → Generate new token**.

- **Repository access:** "Only select repositories" → ez az egy repó.
- **Permissions:**
  - `Contents` → **Read and write**
  - `Actions` → **Read and write**
- Más jogosultság nem kell. Ne adj `Read and write` jogot semmi másnak.

Lejárati dátumot érdemes adni (pl. 90 nap) — a fine-grained PAT-ok maximum 1 évig
élnek, utána új tokent kell generálni és beírni.

### A panel használata

1. ⚙ → illeszd be a GitHub felhasználóneved, a repó nevét és a tokent → **Token
   mentése**. (Ha az oldal `<felhasználó>.github.io/<repó>/...` címről fut, az első
   kettő automatikusan kitöltődik.)
2. Innentől minden eseménykártyán megjelenik egy 📌 **kitűzés** és egy 🙈 **elrejtés**
   gomb — ezek az `overrides.yaml`-t írják (kitűzött id-k mindig bekerülnek a listába,
   `pinned_bonus`-szal; elrejtett id-k a következő futástól kimaradnak).
3. **Forrás ki/bekapcsolása:** írd be a forrás id-jét (a `sources/` alatti fájlnév,
   kiterjesztés nélkül — pl. `bigcitylife`) → **Betöltés** mutatja a jelenlegi
   `enabled:` értéket → kapcsold át → **Mentés**.
4. **Futtatás most:** azonnal elindítja a napi workflow-t, a cronra várás nélkül.
5. **Token elfelejtése:** törli a tokent a böngésző localStorage-ából. Ez NEM vonja
   vissza magát a tokent a GitHub oldalán — ahhoz a Developer settings alatt kell
   törölni vagy lejáratni.

### Amit tudni kell róla

- **A token cleartext van tárolva a böngésző localStorage-ában.** Bárki/bármi, aminek
  hozzáférése van ehhez a böngészőhöz vagy géphez — beleértve bármely, ezen az oldalon
  futó rosszindulatú scriptet is — ki tudja olvasni. Ne oszd meg, ne közös gépen mentsd
  el, és állíts be lejáratot.
- **Az `overrides.yaml` a publikus repóba kerül**, mert az író UI-nak nincs backendje —
  a GitHub Contents API-n át csakis ebbe az (amúgy is publikus) repóba tud írni. A fájl
  csak esemény-id hasheket tartalmaz, nem címeket — de valaki manuálisan
  összevethetné egy id-t az `events.json`/archívum tartalmával, és rájönne, melyik
  konkrét publikus eseményt tűzted ki vagy rejtetted el. (A pontszám-bontásból ez a
  jel direktben nem derül ki — a `pinned_bonus` szándékosan nincs benne az
  `events.json`-ban.)
- **Ütközhet a napi automatikus futással.** Ha a böngészős írás és az éjszakai
  `digest run` commitja időben túl közel esik egymáshoz, a workflow saját `git push`-a
  elakadhat egy non-fast-forward hibán. Ez hangos hiba (a GitHub emailt küld a sikertelen
  workflow-ról), nem csendes adatvesztés — újra kell futtatni a workflow-t.

### Manuális teszt-lépések (nincs Python felület ehhez)

1. Nyisd meg a publikus oldalt, token nélkül → a ⚙ panel csak a token-mezőt mutatja,
   egyetlen eseménykártyán sincs 📌/🙈 gomb.
2. Illeszd be egy erre a repóra korlátozott, `Contents`+`Actions` write jogú tokent →
   **Token mentése** → a 📌/🙈 gombok megjelennek minden kártyán.
3. Kattints egy 📌-ra → nézd meg a repóban, hogy `overrides.yaml` létrejött/frissült a
   `pinned:` listában az adott id-vel, és a commit üzenete nem tartalmazza a tokent.
4. Kattints egy 🙈-ra, erősítsd meg a megerősítő ablakban → `overrides.yaml`
   `hidden:` listája bővül.
5. Írj be egy létező forrás-id-t (pl. `bigcitylife`) → **Betöltés** → a kapcsoló a
   fájl valódi `enabled:` értékét mutatja → kapcsold át → **Mentés** → nézd meg a
   repóban, hogy csak az `enabled:` sor változott, minden komment megmaradt.
6. **Futtatás most** → az Actions fülön pár másodpercen belül megjelenik egy új,
   `workflow_dispatch`-csel indított futás.
7. Módosítsd az `overrides.yaml`-t közvetlenül a GitHub webes szerkesztőjével (más
   sha), majd próbálj a panelból is írni ugyanabba a fájlba → egyértelmű
   ütközés-üzenetet kell kapnod, nem csendes felülírást.
8. **Token elfelejtése** → a 📌/🙈 gombok eltűnnek, a panel visszaáll a
   token-bekérő állapotba.

**Ezek a lépések nincsenek végrehajtva a valódi repó ellen** — ehhez éles PAT és egy
böngésző kell, egyik sincs ebben a munkamenetben. Amit ellenőriztem: a JS szintaktikai
helyessége (`node --check`), és a teljes viselkedés egy valós DOM-ban (jsdom) — token
mentése/elfelejtése, a 📌/🙈 gombok meg- és eltűnése, a hiba-ág (elérhetetlen API
esetén nincs elkapatlan kivétel, a hiba megjelenik a panelen). A tényleges GitHub
API-hívásokat (sha-kezelés, 409-válasz, workflow-indítás) csak kódolvasással, nem
futtatással ellenőriztem.
