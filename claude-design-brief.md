# Claude Design brief — Budapest Event Digest

> Ezt a fájlt egy az egyben bemásolod a Claude Designba. Két artefaktot kérünk tőle.
> Az eredményül kapott HTML lesz a Jinja2 sablonok alapja — a Claude Code nem generálja
> újra a markupot, csak változókat helyez el benne.

---

## A projekt

Egy személyes napi hírlevél budapesti programokról. Minden reggel ~7-kor érkezik egy email
15-25 programmal, kategóriák szerint rendezve, egy szabályrendszer által pontozva.
Mellette van egy statikus weboldal ugyanezekkel az adatokkal, ahol szűrni és keresni lehet.

**Egy olvasó van: a tulajdonos.** Nem termék, nem marketing, nem konverzió. A feladat:
harminc másodperc alatt eldönteni, van-e ma este bármi, amiért érdemes elmenni otthonról.
Ez a design egyetlen munkája.

**A tartalom valódi karaktere:** magyar programcímek, vegyes hosszúságúak és néha nagyon
hosszúak. Sok az ékezet. Az időpontok gyakran éjszakaiak. Az árak hiányozhatnak. A képek
minősége egyenetlen, és néha nincs kép. **A designnak mindezt el kell bírnia** — nem
egyenletes, szép, azonos méretű kártyák világa ez.

---

## 1. artefakt: az email

`email.html` — a napi hírlevél HTML törzse.

### Kemény megkötések, ezek nem tárgyalhatók

Az email kliensek HTML-motorja 2005 körül megállt. Ezért:

- **Táblázat-alapú layout.** `<table>` a szerkezethez. Nincs flexbox, nincs grid,
  nincs `position`, nincs `float`.
- **Minden CSS inline**, a `style` attribútumban. `<style>` blokk legfeljebb a
  media queryknek, de a design ne függjön tőle — Gmail egy részét levágja.
- **Nincs webfont**, nincs `@font-face`. Csak rendszer-fontstack.
- **Nincs CSS változó**, nincs `calc()`, nincs `clamp()`.
- **Nincs háttérkép.** Outlook nem rendereli.
- **Max szélesség 600px**, mobilon folyékony.
- **Gombok táblázatból**, nem `<button>` és nem `<a>` paddinggel — az Outlook elrontja.
- **Képek `width` és `height` attribútummal és `alt` szöveggel.** A designnak akkor is
  működnie kell, ha a kliens minden képet letilt — ez az alapértelmezés sok helyen.
- **Preheader szöveg**: rejtett első sor, ami az értesítésben látszik.
- **Dark mode**: Gmail és Outlook automatikusan invertál. Ne legyen olyan színpár, ami
  invertálva olvashatatlan. Ne használj tiszta fehér hátteret sötét szövegen kontraszt nélkül.

### Amit tartalmaznia kell

1. **Fejléc**: dátum, és egy egysoros összefoglaló ("18 új program, ebből 4 ingyenes").
2. **Kategória-szekciók**, kategóriánként max 5 program. A kategóriák magyarul:
   koncert, klub, színház, kiállítás, film, meetup, társasjáték, kvíz, gasztro, fesztivál,
   outdoor. Csak az kerül be, amiben van tétel.
3. **Programtétel**: cím (link), időpont, helyszín, kerület, ár, egy rövid leírás,
   opcionális bélyegkép. Az ár lehet "ingyenes", konkrét összeg, vagy hiányzó — mindhárom
   esetnek jól kell kinéznie.
4. **Összevont fesztivál-sor**: külön vizuális kezelés. Így néz ki:
   `Sziget Fesztivál — ma 17 program`. Ez egy sor, nem tizenhét.
5. **"Hamarosan lejár" szekció**: korábban már szerepelt, de 3 napon belül van.
   Vizuálisan halkabb, mint a fő szekciók.
6. **Üres állapot**: `send_when_empty` miatt az email 0 programmal is kimegy. Ez nem hiba,
   hanem életjel. Kezeld szándékos állapotként, ne hibaüzenetként.
7. **Lábléc**: forrás-health egysoros ("11 forrásból 10 rendben"), és link az archívumra.

---

## 2. artefakt: a webes olvasó

`index.html` — statikus oldal, ami betölti az `events.json`-t és kliensoldalon szűr.

### Kemény megkötések

- **Egyetlen önálló fájl.** Nincs build step, nincs bundler, nincs npm.
- **Vanilla JS.** Nincs React, Vue, Svelte. `fetch('./events.json')`.
- CSS: kézzel írt a `<style>` blokkban, vagy Tailwind CDN — de akkor csak alaposztályok,
  mert nincs fordító.
- **300+ eseményt kell elbírnia akadás nélkül.** Egyszerű DOM, szükség esetén szakaszos
  renderelés.
- **Mobil első.** Telefonon fogják olvasni, reggel, ágyban.
- Billentyűzet-fókusz látható. `prefers-reduced-motion` tiszteletben tartva.
- A szűrőállapot mehet `localStorage`-ba — ez valódi weboldal, nem beágyazott artefakt.

### Amit tartalmaznia kell

- **Szűrők**: kategória (többszörös választás), dátumtartomány, "csak ingyenes",
  ár-plafon, kerület, szabadszavas kereső.
- **Rendezés**: pontszám vagy időpont szerint.
- **Programkártya**: ugyanazok a mezők, mint az emailben, plusz egy lenyitható
  **"miért ennyi a pont"** rész, ami a `breakdown` objektumot mutatja emberi néven
  (kategória +4, ingyenes +2, közel +2, péntek +2, új +2).
- **Üres szűrő-eredmény**: legyen irány, ne hangulat. Mondja meg, melyik szűrőt lazítsa.
- Fejlécben: mikor futott le utoljára, hány esemény van.

---

## Az adat, amivel dolgozol

Ez valódi kimenet a rendszerből. **Ezzel építs, ne kitalált placeholderrel** — a
tördelést pont a hosszú magyar címek és a hiányzó mezők fogják próbára tenni.

```json
{
  "generated_at": "2026-08-16T04:34:11+02:00",
  "events": [
    {
      "id": "a3f9c21e8b04d7f6",
      "title": "Befogad és kitaszít a világ – Mácsai Pál és Huzella Péter Villon-estje",
      "url": "https://port.hu/esemeny/zene/...",
      "start": "2026-08-19T19:00:00+02:00",
      "venue": "Klebelsberg Kultúrkúria",
      "district": "II.",
      "categories": ["koncert", "szinhaz"],
      "price_min": 4900, "is_free": false,
      "image": "https://media.port.hu/images/001/409/400x400/650.jpg",
      "description": "Miért ilyen népszerűek Magyarországon ezek a fél évezredes francia balladák? A páratlan Villon-est Mácsai Pál és Huzella Péter előadásában.",
      "score": 9.2,
      "breakdown": { "category": 4, "proximity": -0.9, "weekday": 1, "novelty": 2, "soon": 1, "keyword": 2 },
      "group_size": 1
    },
    {
      "id": "7c1e04ab9d3f2185",
      "title": "Sziget Fesztivál",
      "url": "https://port.hu/helyszin/koncert/sziget-fesztival/place-20679",
      "start": "2026-08-16T20:00:00+02:00",
      "venue": "Sziget Fesztivál",
      "district": "III.",
      "categories": ["fesztival"],
      "price_min": null, "is_free": false,
      "image": null,
      "description": "Soulwax (BE), Sub Focus (UK), 2manydjs – DJ Set (BE) és további 14 program",
      "score": 8.0,
      "breakdown": { "category": 4, "weekday": 2, "novelty": 2 },
      "group_size": 17
    },
    {
      "id": "b20d7e5591ca3f84",
      "title": "HØT SPØT 2026 / Every Wednesday / A38",
      "url": "https://port.hu/esemeny/zene/hot-spot-2026-every-wednesday-a38/event-6262316",
      "start": "2026-08-19T17:00:00+02:00",
      "venue": "A38 Hajó",
      "district": "XI.",
      "categories": ["klub"],
      "price_min": 0, "is_free": true,
      "image": "https://media.port.hu/images/001/830/400x400/314.webp",
      "description": "Idén a fennállásának 10. évét ünnepli HØT SPØT, ezért minden szerdán…",
      "score": 11.4,
      "breakdown": { "category": 2, "free": 2, "proximity": 2, "weekday": 1, "novelty": 2, "soon": 1, "keyword": 1.4 },
      "group_size": 1
    },
    {
      "id": "e8814f0c62b7d9a3",
      "title": "Társasjáték est játékmesterrel",
      "url": "https://redandblack.hu/programok/...",
      "start": "2026-08-21T18:00:00+02:00",
      "venue": "Red & Black Társasjátékszalon",
      "district": "VII.",
      "categories": ["tarsasjatek"],
      "price_min": 1500, "is_free": false,
      "image": null,
      "description": "Ha csütörtök, akkor játékmesterünk segít megismerni új játékokat egy koktél vagy egy pohár bor mellett.",
      "score": 13.1,
      "breakdown": { "category": 5, "cheap": 1, "weekday": 1, "novelty": 2, "soon": 1, "keyword": 3.1 },
      "group_size": 1
    },
    {
      "id": "44a9b1cd7e0326f8",
      "title": "Illegál Kvízest – 90-es évek zenéje",
      "url": "https://kvizestek.hu/esemenyek/...",
      "start": "2026-08-19T19:30:00+02:00",
      "venue": "Illegál Budapest",
      "district": "VII.",
      "categories": ["kviz"],
      "price_min": 2000, "is_free": false,
      "image": null,
      "description": null,
      "score": 10.0,
      "breakdown": { "category": 4, "cheap": 1, "weekday": 1, "novelty": 2, "soon": 1, "keyword": 1 },
      "group_size": 1
    },
    {
      "id": "9f3c72e1a5b80d64",
      "title": "Groove Generation (HU)",
      "url": "https://port.hu/esemeny/zene/groove-generation-hu/event-6267230",
      "start": "2026-08-22T01:00:00+02:00",
      "venue": "Turbina Kulturális Központ",
      "district": null,
      "categories": ["klub"],
      "price_min": null, "is_free": false,
      "image": "https://media.port.hu/images/001/835/400x400/284.webp",
      "description": "A GrooveGeneration magyar DJ- és producerduó, akik évek óta meghatározó szereplői a hazai klub- és…",
      "score": 6.4,
      "breakdown": { "category": 2, "weekday": 2, "novelty": 2, "soon": 0.4 },
      "group_size": 1
    }
  ]
}
```

**Figyelj rá, mit tesztel ez a hat rekord:** egy nagyon hosszú cím · egy 17-es összevont
csoport kép nélkül · egy ingyenes esemény · egy `null` leírás · egy `null` kerület ·
egy hajnali 01:00-s időpont, ami valójában az előző estéhez tartozik · negatív tag a
breakdownban · három hiányzó kép a hatból. **Mindegyiknek jól kell kinéznie.**

---

## Vizuális irány

Az esztétikai döntés a tiéd, de három dolgot kérek.

**Egy: kerüld a jelenlegi AI-alapértelmezéseket.** Konkrétan ezt a hármat: krémszín háttér
(#F4F1EA környéke) magas kontrasztú serif címmel és terrakotta akcenttel; majdnem fekete
háttér egyetlen rikító savzöld vagy cinóber akcenttel; újságszerű hairline-vonalas,
nulla lekerekítésű, sűrű hasábos elrendezés. Mindhárom legitim valamire, de itt
alapértelmezés lenne, nem döntés.

**Kettő: a tipográfia hordozza a személyiséget.** Emailben rendszer-fontstackre vagy
korlátozva, tehát ott a méretskála, a súlyok és a térközök viszik a karaktert. A weboldalon
szabadabb vagy. Az ékezetes magyar szöveg jól nézzen ki — az „ő" és az „ű" hosszú
ékezete sok betűtípusban rosszul sül el, ezt nézd meg.

**Három: az információs hierarchia az időpont köré épüljön, ne a kép köré.** Az olvasó azt
kérdezi: „ma este van valami?" Nem galériát nézeget. A képek háromból egyszer hiányoznak,
tehát a layout nem támaszkodhat rájuk.

Egy helyre költsd a merészséget. A legkézenfekvőbb jelölt a **pontszám vizualizálása** —
ez a rendszer egyetlen véleménye, és az egyetlen dolog, ami megkülönbözteti egy sima
programlistától. De ha jobb ötleted van, éld ki azon.

---

## Amit vissza kell adnod

1. `email.html` — teljes, önálló, valós adattal kitöltve, a fenti hat eseményből
   legalább öttel, kategória-szekciókba rendezve. Tartalmazza a preheadert, az összevont
   fesztivál-sort, a „hamarosan lejár" szekciót és a láblécet.
2. `email-empty.html` — ugyanaz, üres állapotban.
3. `index.html` — a webes olvasó, működő kliensoldali szűréssel, a JSON beágyazva
   `const DATA = {...}` formában (a Jinja2 lépés majd `fetch`-re cseréli).
4. Egy rövid **design note**: a paletta 4-6 megnevezett hexértékkel, a betűtípus-szerepek,
   és egy bekezdés arról, mi a lap „aláírás-eleme" és miért az.

Ne adj vissza React komponenst, ne használj olyan könyvtárat, ami build stepet igényel,
és ne találj ki új adatmezőket a fenti sémán túl.
