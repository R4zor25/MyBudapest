# Deploy — lépésről lépésre

Sorrendben. A 0. lépés az egyetlen, amit nem lehet visszacsinálni, ezért az van elöl.

---

## 0. Előfeltétel: az auditok

Ne kezdd el, amíg az **Audit 3** (titkok és szivárgás) le nem futott és minden `BLOCKER`
nincs javítva. A repo publikussá tétele után a git history már nyilvános — egy véletlenül
becommittolt jelszó akkor is kikerült, ha utána törlöd.

Ha bármi kétség van a history-val kapcsolatban, **ne a régi repót tedd publikussá**.
Csinálj újat, tiszta első committal:

```bash
cd ..
git clone --depth 1 budapest-event-digest budapest-event-digest-clean
cd budapest-event-digest-clean
rm -rf .git
git init && git add -A && git commit -m "chore: initial commit"
```

Ezzel elveszted a fejlesztési history-t, de az itt nem érték.

---

## 1. Gmail app password

Az SMTP-hez nem a Google-fiókod jelszava kell, hanem külön app password. Ehhez **kötelező
a kétlépcsős azonosítás**, e nélkül a menüpont meg sem jelenik.

1. Google-fiók → Biztonság → Kétlépcsős azonosítás → bekapcsolás, ha még nincs
2. Ugyanott lentebb: **Alkalmazásjelszavak** → új jelszó generálása, név: `digest`
3. A kapott 16 karakteres jelszót másold ki. **Egyszer látod.**

Beállítások, amiket a secretekbe fogsz írni:
- `SMTP_HOST` = `smtp.gmail.com`
- `SMTP_USER` = a teljes email címed
- `SMTP_PASSWORD` = a 16 karakteres app password, szóközök nélkül
- port: 587, STARTTLS

**Gmail küldési limit:** napi 500 üzenet. Napi egy levélnél ez irreleváns.

---

## 2. Gemini API kulcs — csak ha `llm.enabled: true`

Google AI Studio → Get API key → új kulcs. Ha a `config.yaml`-ban az `llm.enabled` hamis,
ezt a lépést hagyd ki, és a `GEMINI_API_KEY` secretet se hozd létre.

---

## 3. A repo publikussá tétele

Settings → General → legalul: Danger Zone → **Change repository visibility** → Public.

Miért kell: az ingyenes GitHub-fiókon a **Pages csak publikus repóhoz jár**. Privát repóhoz
Pro-előfizetés kell, és a publikált oldal még akkor is publikus marad.

Ha nem akarod publikussá tenni: a hírlevél privát repóban is pontosan ugyanígy működik,
csak a Pages archívum és a webes olvasó esik ki. Ez esetben hagyd ki a 6. lépést.

---

## 4. Secretek

Settings → Secrets and variables → **Actions** → New repository secret.

| Név | Tartalom |
|---|---|
| `PROFILE_YAML` | a teljes profil YAML, ahogy a `SPEC.md` §5.2-ben van. Többsoros tartalom itt gond nélkül működik — másold be, ahogy van. |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_USER` | a küldő email cím |
| `SMTP_PASSWORD` | a 16 karakteres app password |
| `GEMINI_API_KEY` | csak ha az LLM réteg be van kapcsolva |

Ellenőrzés, hogy semmit nem hagytál ki:

```bash
grep -o 'secrets\.[A-Z_]*' .github/workflows/digest.yml | sort -u
```

Minden itt kilistázott névnek léteznie kell a Secrets oldalon.

---

## 5. Workflow jogosultságok

Settings → Actions → General → legalul **Workflow permissions** →
**Read and write permissions** → Save.

Ez azért kell, mert a workflow visszacommittolja a `state/state.json`-t. A workflow-ban
deklarált `permissions: contents: write` **nem tudja túllépni a repo-szintű beállítást** —
ha az „read-only"-n áll, a push 403-mal elhasal, és ez a leggyakoribb első hiba.

---

## 6. GitHub Pages

Settings → Pages → **Source: GitHub Actions**.

Nem „Deploy from a branch". Az `actions/deploy-pages` action kizárólag akkor működik, ha a
forrás GitHub Actionsre van állítva. A publikált URL az első sikeres deploy után jelenik meg.

---

## 7. Lokális próbafutás — hálózat és küldés nélkül

Mielőtt bármit élesben futtatnál:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

export PROFILE_YAML="$(cat ~/profile.yaml)"
export SMTP_HOST=smtp.gmail.com
export SMTP_USER=...
export SMTP_PASSWORD=...

digest run --dry
```

Amit ellenőrizz a kimeneten:
- megnyílik-e a generált HTML böngészőben, és olvasható-e telefonméretben is
- a forrásonkénti darabszámok reálisak-e (egy forrás 0-val: törött parser, nem üres nap)
- **hány esemény jött összesen** — ez a mennyiség fog reggel is érkezni

**Futtasd le kétszer egymás után**, immár `--dry` nélküli állapotmentéssel egy teszt
state fájlra. A második futásnak **nulla új eseményt** kell adnia. Ha nem nullát ad,
a ledger nem működik, és ne menj tovább.

---

## 8. Első éles futás — kézzel

Actions fül → `digest` workflow → **Run workflow** → Run.

Nézd végig a logot, és utána ellenőrizd mind a négyet:

1. **Megérkezett az email?** Nézd meg a spam mappát is — az első levél gyakran oda kerül.
   Ha igen, jelöld „nem spam"-nek, különben a napi levél is oda fog menni.
2. **Létrejött a commit?** A `state/state.json` és a `site/` mappa változásával.
3. **Deployolt a Pages?** A workflow összefoglalójában megjelenik az URL.
4. **A `/status.html` mit mutat?** Ha egy forrás 0 eseménnyel szerepel, az most, kézi
   futásnál még olcsón javítható.

---

## 9. Az ütemezés élesítése

Az ütemezett futáshoz semmit nem kell csinálni azon túl, hogy a workflow fájl a
**default branchen** van. Amit tudni kell:

- A cron **UTC-ben** jár. A `30 4 * * *` nyári időszámításkor 06:30, télen 05:30 helyi idő.
  Vagy elfogadod az egy óra elcsúszást, vagy évente kétszer átírod.
- Az első ütemezett futás **akár egy napot is csúszhat**, és csúcsidőben 5-30 perc
  késés normális. A GitHub ezt explicit „best effort"-ként dokumentálja, SLA nélkül.
- Publikus repóban az ütemezett workflow letiltódik 60 nap repo-aktivitás nélkül.
  A napi `state.json` commit ezt lefedi, tehát nálad nem fordulhat elő — hacsak nem
  állítod le a projektet hosszabb időre.

---

## 10. Az első hét

| Mikor | Mit nézz |
|---|---|
| 2. nap | **Nincs ismétlődés?** Ha ugyanaz a program másodszor is bejön, a ledger hibás. Ez a legfontosabb ellenőrzés. |
| 2. nap | Az első nap kimaradt eseményei megjönnek-e. Az első futáskor minden esemény új, a `total_limit` viszont 25-nél vág — a maradéknak a következő napokon kell csordogálnia, nem eltűnnie. |
| 3-4. nap | `/status.html`: van-e forrás, ami folyamatosan 0-t ad. |
| 5-7. nap | A pontozás értelmes sorrendet ad-e. Ha nem, a `PROFILE_YAML` súlyait hangold — ehhez nem kell kód, csak a secret átírása. |
| bármikor | Ha egy reggel **nem jön email**, az a riasztás. A rendszer 0 találatnál is küld. |

---

## 11. Leállítás és visszavonás

| Mit akarsz | Hogyan |
|---|---|
| Ideiglenes leállítás | Actions fül → a `digest` workflow → jobb felül `...` → Disable workflow |
| Egy forrás kikapcsolása | `sources/<id>.yaml` → `enabled: false` → commit |
| Az email leállítása, de a gyűjtés maradjon | `config.yaml` → `delivery` → az smtp bejegyzés `enabled: false` |
| A Pages oldal levétele | Settings → Pages → Source: None |
| Teljes visszaállás nulláról | `state/state.json` törlése → a következő futás mindent újnak lát |

---

## Ami elsőként el szokott romlani

Sorrendben, tapasztalati gyakoriság szerint:

1. **A workflow push-a 403-mal hasal el** — a repo-szintű Workflow permissions read-only.
   Ez az 5. lépés.
2. **Az SMTP 535-tel elutasít** — a fiók jelszavát adtad meg app password helyett, vagy
   a szóközöket is bemásoltad a 16 karakteres jelszóból.
3. **A Pages deploy elhasal** — a Source nincs GitHub Actionsre állítva. Ez a 6. lépés.
4. **Az első email a spamben landol** — a Gmail nem szereti a saját magadnak küldött,
   sok linket tartalmazó automatikus levelet. Jelöld „nem spam"-nek egyszer.
5. **Egy forrás 403-at ad** — a runner IP-je datacenter-tartományból jön, és az oldal
   tiltja. Ez nem a te hibád és nem is javítható; kapcsold ki a forrást.
6. **Az első reggeli email kicsi, nem hatalmas** — AUDIT-5 BLOCKER (javítás): a `port-hu`
   forrás `enabled: false`, amíg a valódi listázó végpontja nincs meg (SPEC §17,
   1. kérdés) — csak a `bigcitylife` fut ténylegesen, és az is csak a hétvégi programokat
   listázza, a `horizon_days: 14`-től függetlenül. Számíts kb. 10 esemény körüli első
   levélre, nem tucatnyi forrásnyi tartalomra. Ha/amikor a `port-hu` valódi URL-je
   megvan és a forrás vissza lett kapcsolva (`sources/port-hu.yaml`), *akkor* válik újra
   igazzá az eredeti tanács: az első futásnál minden esemény új, és ha zavaró, csökkentsd
   egy hétre a `horizon_days`-t az első pár napra.
