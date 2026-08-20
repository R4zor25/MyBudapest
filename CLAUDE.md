# CLAUDE.md — Budapest Event Digest

Ezt a fájlt minden futásnál elolvasod. A teljes indoklás a `SPEC.md`-ben van;
ez a kötelező szabályok listája.

## Mi ez a projekt

Napi egyszer futó Python batch job GitHub Actionsön. Budapesti programokat gyűjt több
forrásból, deduplikál, pontoz, és emailben kiküldi az újakat. Emellett kirak egy statikus
Pages oldalt. Nincs szerver, nincs adatbázis, nincs webframework.

## Architektúra

```
fetch → normalize → dedup → recurrence → categorize → filter → score → group → limit
      → render (email + web) → deliver → state commit
```

Minden pipeline szakasz **tiszta függvény** `(list[Event], Config) -> list[Event]` alakban,
külön modulban a `src/digest/pipeline/` alatt. Nincs globális állapot, nincs I/O a
pipeline-ban — a fetch és a delivery az egyetlen kivétel.

## Kötelező szabályok

1. **Nincs új dependency indoklás nélkül.** A megengedett lista a `SPEC.md` §15-ben.
   Ha valami hiányzik, előbb kérdezz.
2. **Nincs Playwright, nincs böngésző, nincs Selenium.** A `fetcher` mező sémájában
   szerepel a `playwright` érték, de implementáció nincs és nem is kell.
3. **Nincs adatbázis.** Az állapot egyetlen JSON fájl: `state/state.json`.
4. **Az LLM soha nincs a kritikus úton.** Minden LLM hívás opcionális, kötegelt, cache-elt,
   és 429-re némán visszaesik a szabályrendszerre.
5. **Semmi személyes adat a publikus configba.** `scoring`, `home`, `recipient_email`,
   `filters` kizárólag a `PROFILE_YAML` secretben él. A `test_config_privacy.py` ezt őrzi.
6. **Egy forrás hibája sosem buktatja el a futást.** Per-source `try/except`, logolás,
   továbbmegyünk.
7. **Egyetlen teszt sem megy ki a hálózatra.** `respx` mock + lementett fixture.
8. **Nem generálsz újra HTML markupot.** Az `email.html.j2` és `index.html.j2` alapja a
   design artefakt; te csak Jinja2 változókat és ciklusokat helyezel el benne.
9. **Két renderelési profil, `SPEC.md` §9.0.** Az emailbe mehet átvett leírás és
   forrásoldali kép; a publikus Pages kimenetbe **soha**. Az `events.json` explicit
   mezőlistából épül, nem `model_dump()`-pal.
10. **Udvarias crawler.** Soros kérések, forrásonkénti rate limit, `robots.txt`,
   és őszinte `User-Agent` elérhetőséggel. Ez utóbbi nem formalitás: ez a legfőbb oka
   annak, ha egy oldal békén hagy.
11. A pipeline szakaszok nem mutálják a bemeneti listájukat és nem mutálják a bemeneti
   Event objektumokat. Módosítás helyett `model_copy(update=...)` és új lista. Ez különösen
   a group szakaszra vonatkozik, ahol az összevont sor és a tagjai egyszerre élnek.

## Kódstílus

- Python 3.12, teljes type hint, `from __future__ import annotations`.
- Pydantic v2 modellek, nem dataclass, ahol validáció kell.
- `ruff` formázás és lint, alapbeállítás, sorhossz 100.
- Docstring csak ott, ahol a "miért" nem nyilvánvaló. A "mit" a típusokból látszik.
- Kivételek: saját típusok az `errors.py`-ban, nem `Exception`.
- Logolás `structlog`-gal, kulcs-érték párokkal: `log.warning("parse_failed", source=sid, url=u)`.
  Soha nem `print`.
- Magyar szöveg csak a felhasználónak szánt kimenetben (email, UI). Kód, változónév,
  commit üzenet, log: angol.

## Tesztelés

Minden promptcsomag futtatható teszttel zárul. Egy csomag nincs kész, amíg a
`pytest tests/test_<modul>.py -v` nem zöld.

Fixture-ök a `tests/fixtures/` alatt, valós lementett válaszokból. Nem gyártunk szintetikus
HTML-t olyan forráshoz, amiről van valós minta.

## Commit konvenció

`<type>(<scope>): <üzenet>` — pl. `feat(dedup): add fuzzy title matching`.
Típusok: `feat`, `fix`, `refactor`, `test`, `chore`, `docs`.
Egy promptcsomag = egy commit. Nem commitolsz félkész állapotot.

## Amit soha nem csinálsz

- Nem kezdesz bele a következő mérföldkőbe, ha a jelenlegi csomag kész.
- Nem írod át más modul kódját "amíg úgyis ott vagy".
- Nem vezetsz be absztrakciót egyetlen használati helyhez.
- Nem írsz `# type: ignore`-t magyarázat nélkül.
- Nem hagysz `TODO`-t commitban — vagy megcsinálod, vagy a csomag hatókörén kívül van
  és szólsz róla.
