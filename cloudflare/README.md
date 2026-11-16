# nextbike-predict 🚲 Luzern

Kostenloser Velo-Verfügbarkeits-Tracker mit Prognose für **nextbike Luzern**, als
Cloudflare Worker (TypeScript) mit Cron Trigger und D1-Datenbank. Läuft komplett im Free Plan.

- **jede Minute** (nachts 01–05 Uhr alle 5 Min) den Stand des ganzen Luzerner Netzes holen und
  **nur die Änderungen** speichern: Bestände aller ≈ 107 Stationen und die Bewegung **jedes einzelnen
  Velos** (Ausleihe, Rückgabe, Umverteilung). Alle 6 h ein vollständiger Keyframe.
- Daraus lassen sich offline minutengenaue Stationsverläufe, alle Velobewegungen und **Fahrten**
  (Start → Ziel, Dauer, Distanz, Umverteilungs-Hinweis) rekonstruieren (`npm run export`).
- pro eigener Station alle 5 Min eine Beobachtung (Velos, E-Bikes, freie Plätze) für die Prognose
- Prognose: Wie wahrscheinlich steht zu Uhrzeit X an Station Y mindestens ein Velo?
- HTTP-Endpunkte für Statusseite (Handy-Lesezeichen), Prognose, Planung (Morgenbriefing) und CSV-Export

---

## Inhalt

1. [Wie es funktioniert](#wie-es-funktioniert)
2. [Setup Schritt für Schritt](#setup-schritt-für-schritt)
3. [Endpunkte und curl-Beispiele](#endpunkte-und-curl-beispiele)
4. [Stationen ändern](#stationen-ändern)
5. [Lokal entwickeln und Cron simulieren](#lokal-entwickeln-und-cron-simulieren)
6. [Daten exportieren und analysieren](#daten-exportieren-und-analysieren)
7. [Verbrauch gegen die Free-Limits](#verbrauch-gegen-die-free-limits)
8. [Velo-Tracking und Takt](#velo-tracking-und-takt)
9. [Datenmodell](#datenmodell)

---

## Wie es funktioniert

### Datenquelle (am echten Feed geprüft, Stand 11/2026)

| Was | Wert |
|---|---|
| Feed | `https://maps2.nextbike.net/maps/nextbike-live.json?city=126&bikes=false` |
| city-ID Luzern | **126** (Netz-Alias `vdl`, Domain `ch`) |
| Südpol / Kriens-Allmend | gehört ebenfalls zu city 126 → **ein** gefilterter Abruf reicht |
| Antwortgrösse | ≈ 51 KB (`bikes=false` lässt die Einzel-Velo-Liste weg; ohne: ≈ 115 KB) |

Hinweise:

- `api.nextbike.net` antwortet mit **301 → `maps2.nextbike.net`**; der Worker fragt deshalb direkt maps2 ab.
- Einen eigenen GBFS-Feed nur für Luzern gibt es nicht (nur `nextbike_ch` für die ganze Schweiz →
  zu gross für das CPU-Budget). GBFS diente nur dazu, die Velo-Typen zu bestimmen:
  `150`/`196` = normales Velo, **`183` = E-Bike, `371` = E-Cargobike**.
- Die Luzerner Stationen sind fast alle *virtuell* (Schild statt Ständer, `bike_racks = 0`).
  „Freie Plätze“ gibt es dort nicht → wird als `NULL` gespeichert.
- Frei abgestellte Velos (`spot=false`, Name „BIKE 4xxxxx“) werden nicht als Station gezählt,
  aber im Snapshot als `free_floating` mitgezählt.

### Konfigurierte Stationen (`src/config.ts`)

| Alias | uid | Name | Rolle |
|---|---|---|---|
| `home` | 3024956 | Neustadtstrasse 10 | Start ★ |
| `moosstrasse` | 39638 | Bundesplatz / Moosstrasse | Ausweich Start (157 m) |
| `zentralstrasse` | 34397 | Bundesplatz / Zentralstrasse 1 | Ausweich Start (200 m) |
| `suedpol` | 70900 | Südpol / Arsenalstrasse 28 | Ziel ★ |
| `messe` | 3024979 | Messe Allmend / Horwerstrasse | Ausweich Ziel (460 m) |

### Prognose

Für Station + Zeitpunkt (Europe/Zurich, Sommer-/Winterzeit korrekt) wird der **Wochen-Slot**
bestimmt (Wochentag × 15 Minuten, 672 Slots pro Woche). Dann zählt der Worker alle historischen
Beobachtungen im selben Slot **±1 Slot** (also ±15 Min, auch über Mitternacht/Wochenwechsel):

- `probability` = Anteil der Beobachtungen mit ≥ 1 Velo
- `avg_bikes` = durchschnittliche Anzahl Velos
- `probability_ebike`, `avg_ebikes` = dasselbe nur für E-Bikes
- `n` = Anzahl Beobachtungen

Bei weniger als **5** Beobachtungen kommt `"status": "zu_wenig_daten"` statt einer geratenen Zahl.
Nach einer Woche Betrieb gibt es pro Slot ≈ 9 Beobachtungen (3 Slots × 3 Läufe), nach 8 Wochen ≈ 70.

### Velo-Tracking (Überblick)

Jeder Abruf wird mit dem vorherigen Zustand (Tabelle `state`, 1 Zeile) verglichen. Ins Log (`log`)
kommt nur, was sich geändert hat. Bleibt alles gleich, wird keine Log-Zeile geschrieben. Details und die
Begründung für Takt und Nachtmodus siehe [Velo-Tracking und Takt](#velo-tracking-und-takt).

### Robustheit

- Feed nicht erreichbar / kein JSON / falsche Struktur / weniger als 20 Stationen → **nichts wird geschrieben**,
  Fehler wird geloggt (`npx wrangler tail`) und als `last_error` gemerkt (Statusseite zeigt ihn an).
- Ein Retry nach 1.5 s, Timeout 10 s.
- Unplausible Werte (negativ, kein Integer, > 500) → diese Station wird in diesem Lauf ausgelassen.
- Station fehlt oder ist in Wartung → Lücke statt Null-Zeile. Die Statistik zählt nur vorhandene Beobachtungen.
- Zeitstempel = tatsächlicher Abrufzeitpunkt, auf die Minute abgerundet (obs: auf 5 Min). Verspätete Läufe landen
  im richtigen Slot. Doppelte Auslösungen werden übersprungen, ohne den Feed abzurufen.
- Fällt ein Abruf aus, bleibt der alte Zustand stehen. Das nächste Delta umfasst dann die ganze Lücke
  (`prev_ts` zeigt, wie lang sie war). Es geht keine Bewegung verloren, nur die Zeitauflösung ist dort gröber.
- Tägliche Wartung läuft im selben Cron, sobald es in Zürich ≥ 03:00 ist und heute noch keine lief.
  Ein verpasster Lauf wird also beim nächsten Cron nachgeholt. Die Wartung läuft als eine Transaktion
  (verdichten + löschen), es kann also nichts doppelt gezählt werden oder verloren gehen.

### Sicherheit

- Jeder HTTP-Endpunkt verlangt `Authorization: Bearer <TOKEN>`. Für die Statusseite `/` und die
  CSV-Downloads (`/export`) geht zusätzlich `?token=…` (für Handy-Lesezeichen).
- Der Token ist ein Worker-Secret (`wrangler secret put API_TOKEN`) und steht nie im Code. Lokal liegt er in `.dev.vars`
  (per `.gitignore` ausgeschlossen).
- Vergleich in konstanter Zeit (SHA-256 beider Seiten, XOR-Vergleich ohne frühen Abbruch).
- Ohne gültigen Token kommt sofort `401`, bevor irgendetwas aus D1 gelesen wird (per Test abgesichert).
- Fehlt das Secret, wird alles abgelehnt.
- Der Cron-Handler ist nicht über HTTP erreichbar. `/__scheduled` existiert nur lokal bei `wrangler dev --test-scheduled`.
- Die HTML-Seite enthält den Token nicht (`Referrer-Policy: no-referrer`, `noindex`).

---

## Setup Schritt für Schritt

Voraussetzungen: Node.js ≥ 20, ein kostenloser Cloudflare-Account (keine Kreditkarte nötig).

**1. Cloudflare-Account anlegen** unter <https://dash.cloudflare.com/sign-up>. Beim ersten Deploy
wird nach einer `*.workers.dev`-Subdomain gefragt (frei wählbar).

**2. Abhängigkeiten installieren**

```bash
npm install
```

**3. Bei Cloudflare anmelden** (öffnet den Browser)

```bash
npx wrangler login
```

**4. D1-Datenbank anlegen**

```bash
npx wrangler d1 create nextbike
```

Die ausgegebene `database_id` in `wrangler.toml` bei `database_id = "…"` eintragen.
Die ID ist kein Geheimnis und darf committed werden.

**5. Migrations anwenden** (legt die Tabellen an)

```bash
npm run db:migrate:remote
```

**6. Token erzeugen und als Secret setzen**

```bash
openssl rand -base64 32
```

```bash
npx wrangler secret put API_TOKEN
```

(Den Token aus dem vorherigen Befehl einfügen und gut aufbewahren, z. B. im Passwortmanager.)

**7. Deployen**

```bash
npm run deploy
```

Ausgabe: `https://nextbike-predict.<subdomain>.workers.dev`. Ab jetzt läuft der Cron jede Minute (nachts alle 5 Min).

**8. Prüfen** (nach 1–2 Minuten)

```bash
curl -H "Authorization: Bearer $TOKEN" https://nextbike-predict.<subdomain>.workers.dev/now
```

```bash
npx wrangler tail
```

(`tail` zeigt Live-Logs inkl. Cron-Läufen und Fehlern.)

Lesezeichen fürs Handy: `https://nextbike-predict.<subdomain>.workers.dev/?token=<TOKEN>`

---

## Endpunkte und curl-Beispiele

Für die Beispiele:

```bash
export URL=https://nextbike-predict.<subdomain>.workers.dev
export TOKEN=<dein-token>
```

| Endpunkt | Zweck |
|---|---|
| `GET /` | HTML-Statusseite: Stand jetzt + Prognose nächste 4 h (30-Min-Raster) + Download-Links |
| `GET /now` | aktueller Stand der eigenen Stationen (aus dem letzten Cron-Lauf) |
| `GET /predict?station=<id\|alias>&at=<…>` | Wahrscheinlichkeit, Ø Velos, n |
| `GET /plan?at=<…>[&dir=start\|ziel]` | Empfehlung Stamm- vs. Ausweichstation |
| `GET /export?table=log\|bikes\|stations\|obs\|agg\|snap[&cursor=…][&limit=…]` | CSV-Export, seitenweise |

`at` akzeptiert:
- `HH:MM` → heute, Zürcher Zeit
- `2026-11-17T07:45` → Zürcher Wanduhrzeit
- `2026-11-17T06:45Z` oder `…+01:00` → absoluter Zeitpunkt
- weglassen → jetzt

Antworten werden 60 s gecacht (Header `x-cache: miss|mem|edge`).

**Aktueller Stand**

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$URL/now"
```

**Prognose für heute 07:45 an der Stammstation**

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$URL/predict?station=home&at=07:45"
```

```json
{
  "station": { "id": 3024956, "alias": "home", "name": "Neustadtstrasse 10", "group": "start", "primary": true },
  "at": { "local": "2026-11-17T07:45+01:00", "utc": "2026-11-17T06:45:00.000Z" },
  "slot": "Di 07:45",
  "slots_used": ["Di 07:30", "Di 07:45", "Di 08:00"],
  "n": 54,
  "status": "ok",
  "probability": 0.852,
  "avg_bikes": 1.94,
  "probability_ebike": 0.13,
  "avg_ebikes": 0.15
}
```

**Prognose für einen bestimmten Tag, per uid**

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$URL/predict?station=70900&at=2026-11-20T17:30"
```

**Plan fürs Morgenbriefing** (Hinweg: Stamm Neustadtstrasse vs. Ausweich Bundesplatz)

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$URL/plan?at=07:45"
```

```json
{
  "recommendation": {
    "alias": "moosstrasse",
    "probability": 0.93,
    "text": "Neustadtstrasse 10 nur 62 % – besser Bundesplatz / Moosstrasse (157 m): 93 % (Ø 3.1 Velos)."
  },
  "options": [ { "station": { … }, "prediction": { … }, "now": { "bikes": 1, "ebikes": 0 } }, … ]
}
```

Rückweg ab Südpol: `&dir=ziel`. Die Stammstation wird empfohlen, solange ihre Wahrscheinlichkeit ≥ 80 %
ist (`PLAN_THRESHOLD` in `src/config.ts`), sonst die Ausweichstation mit der höchsten Wahrscheinlichkeit.
Nur Kurztext fürs Briefing:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$URL/plan?at=07:45" | jq -r .recommendation.text
```

**Ohne Token → 401**

```bash
curl -i "$URL/now"
```

---

## Stationen ändern

1. uid der gewünschten Station im Feed suchen (Beispiel: alles mit „Bahnhof“):

   ```bash
   curl -sL "https://maps2.nextbike.net/maps/nextbike-live.json?city=126&bikes=false" | jq -r '.countries[].cities[].places[] | select(.spot) | "\(.uid)\t\(.bikes)\t\(.name)"' | grep -i bahnhof
   ```

2. In `src/config.ts` im Array `STATIONS` eintragen oder ersetzen:

   ```ts
   { id: 12345, alias: "bahnhof", name: "Bahnhof / …", group: "start", primary: false, distance_m: 300 },
   ```

   - `group`: `"start"` (Hinweg) oder `"ziel"` (Rückweg); pro Gruppe genau eine `primary: true`.
   - `alias`: kurzer Name für `/predict?station=…`.

3. Testen und deployen:

   ```bash
   npm test
   ```

   ```bash
   npm run deploy
   ```

Neue Stationen starten bei 0 Beobachtungen. Alte Daten entfernter Stationen bleiben erhalten und stören nicht.
Da das Änderungs-Log alle Stationen enthält, lässt sich die Historie einer neuen Station auch
nachträglich aus dem Änderungs-Log (`station_changes.csv` des Exports) rekonstruieren.

---

## Lokal entwickeln und Cron simulieren

```bash
cp .dev.vars.example .dev.vars
```

```bash
npm run db:migrate:local
```

**Einen Cron-Lauf simulieren** (startet `wrangler dev --test-scheduled`, ruft `/__scheduled` auf, zeigt den
Inhalt der lokalen D1 sowie `/now` und `/plan`):

```bash
npm run simulate-cron
```

Offline mit dem Feed-Ausschnitt aus `test/fixtures/` und mehreren Läufen (zwischen den Läufen wird je 61 s
gewartet, weil höchstens 1 Abruf pro Minute fällig ist):

```bash
FIXTURE=1 RUNS=3 npm run simulate-cron
```

Dev-Server manuell starten:

```bash
npm run dev
```

Dann `http://localhost:8787/?token=<API_TOKEN aus .dev.vars>` öffnen, oder einen Cron-Lauf auslösen:

```bash
curl "http://localhost:8787/__scheduled?cron=*+*+*+*+*"
```

**Tests und Typprüfung** (vitest, ohne Netzwerk; SQL wird gegen echtes SQLite (`node:sqlite`) mit den Migrations getestet):

```bash
npm test
```

```bash
npm run typecheck
```

Die Tests decken ab: Feed-Parsing anhand eines echten Feed-Ausschnitts, Zeitzonen und Slots (gegen
`Intl` über 7 Jahre inkl. aller DST-Wechsel), Prognose und Empfehlung, Takt/Nachtmodus, Delta- und Keyframe-Logik,
Velo-Bewegungen, Cron-Idempotenz, Feed-Ausfälle (Lücken), die Verdichtung (Prognose vor und nach der Verdichtung
identisch), Grössen-Schutz, Auth (401 ohne D1-Zugriff), Cache, Export-Paging und die Rekonstruktion von Fahrten
von Cron-Lauf bis CSV (auch über eine Keyframe-Minute hinweg).

---

## Daten exportieren und analysieren

**Variante A: Export-Script** (inkrementell, empfohlen, z. B. einmal pro Woche):

```bash
NEXTBIKE_URL=$URL NEXTBIKE_TOKEN=$TOKEN npm run export
```

Schreibt nach `./export/` (per `.gitignore` ausgeschlossen):

| Datei | Inhalt |
|---|---|
| `trips.csv` | **Fahrten**: `bike, type, start_ts, start_uid/lat/lng, end_ts, end_uid/lat/lng, duration_min, distance_m, direct, arrivals_same_minute` |
| `bike_events.csv` | jede Bewegung jedes Velos: `ts, prev_ts, bike, from_uid/lat/lng, to_uid/lat/lng` (leer = nicht sichtbar) |
| `station_changes.csv` | jede Bestandsänderung jeder Station: `ts, uid, bikes, ebikes, booked, active` |
| `log.csv` | Änderungs-Log roh (JSON) |
| `stations.csv`, `bikes.csv` | Stammdaten: Name, Koordinaten, Velotyp |
| `obs.csv`, `agg.csv` | Prognose-Daten der eigenen Stationen |
| `snap.csv`, `snap_long.csv` | alte v1-Snapshots, falls vorhanden |

Die Log-Dateien werden bei jedem Aufruf nur ergänzt (Zwischenstand in `export/.reconstruct-state.json`).
So hast du die Rohdaten unbegrenzt lokal, unabhängig von der Aufbewahrung in D1.

So liest du die Fahrten:
- `direct = 0`: Das Velo verschwand an A (ausgeliehen) und tauchte später an B auf. `start_ts` ist der erste
  Abruf, in dem es fehlte. Die Dauer ist also auf ±1 Minute genau (nachts ±5 Min).
- `direct = 1`: Das Velo war zwischen zwei Abrufen direkt von A nach B gewechselt. Das ist eine sehr kurze
  Fahrt oder eine Umverteilung.
- `arrivals_same_minute ≥ 3`: Mehrere Velos kamen in derselben Minute an derselben Station an. Das spricht
  stark für einen Umverteilungs-Transporter.
- Sehr lange Fahrten (Stunden bis Tage) sind meist Werkstatt oder Depot.

**Variante B: Ganze DB als SQL-Dump:**

```bash
npx wrangler d1 export nextbike --remote --output=nextbike.sql
```

**Variante C: Im Browser** über die Download-Links unten auf der Statusseite (Log der letzten 24 h, Stammdaten).

Ideen für Analysen: Tagesgang pro Station, Netto-Zu-/Abfluss pro Station und Stunde, Quelle-Ziel-Matrix der
Fahrten, typische Fahrdauern, Auslastung einzelner Velos, E-Bike-Anteil, Umverteilungsmuster (wann, wohin,
wie viele), leere Stationen pro Wochentag.

---

## Velo-Tracking und Takt

**Warum jede Minute?** Bei 5 Minuten verschwimmen kurze Fahrten (eine Fahrt Bundesplatz → Bahnhof dauert
oft < 5 Min), und Umverteilungen sind schwer von normalen Fahrten zu trennen. 1 Minute ist der kleinste
Cron-Takt und gibt Fahrdauern auf ±1 Min genau.

**Lässt sich nachts sparen?** Ja, und das ist umgesetzt: Von 01 bis 05 Uhr wird nur alle 5 Minuten
abgerufen. Ehrlich gesagt bringt das aber weniger, als man denkt, weil die Delta-Speicherung den
grösseren Effekt hat:

- Die Aufrufe sind nicht das Limit: 1 440 Cron-Auslösungen pro Tag sind 1.4 % von 100 000.
- Die knappen Ressourcen sind D1-Writes und Speicherplatz. Wenn sich nichts ändert (nachts der Normalfall),
  wird ohnehin keine Log-Zeile geschrieben, nur der Zustand (1 Write).
- Der Nachtmodus spart so ≈ 190 Abrufe, ≈ 190 Writes und etwas CPU pro Tag. Takt und Nachtfenster
  stellst du in `src/config.ts` ein (`DAY_INTERVAL_MIN`, `NIGHT_INTERVAL_MIN`, `NIGHT_FROM_H`, `NIGHT_TO_H`).

**Speicherformat** (`log`, 1 Zeile pro Abruf mit Änderungen):

```
kind 'D', st = [[uid, verfügbar, E, reserviert, aktiv], …]   nur geänderte Stationen
          bk = [["401197", 3024956, 0], …]                  Velo, von, nach
               Ort = Station-uid | [lat, lng] (frei abgestellt) | 0 (nicht sichtbar = ausgeliehen/Depot)
kind 'K'  alle 6 h: kompletter Zustand (≈ 9 KB). Damit lässt sich ab jedem Keyframe rekonstruieren,
          und ein verlorenes Delta wirkt sich höchstens bis zum nächsten Keyframe aus.
```

Am echten Feed gemessen (Montagnachmittag): 1–12 Velobewegungen pro Minute, Delta ≈ 70–400 Byte.

Velotypen (`bikes.type`) kommen aus einem Vollabruf mit Velo-Liste, 1× täglich während der Wartung.
Sonst wird die schlanke Variante (`bikes=false`, 51 KB) abgerufen, die die Velonummern ebenfalls enthält.

---

## Verbrauch gegen die Free-Limits

Gemessen (lokal) bzw. abgeschätzt für ≈ 107 Stationen, ≈ 350 Velos, 5 eigene Stationen:

| Ressource | Free-Limit | Verbrauch | Anteil |
|---|---|---|---|
| Cron Triggers | 5 pro Account | 1 (`* * * * *`) | – |
| Requests | 100 000 / Tag | 1 440 Cron-Auslösungen + eigene Aufrufe | ≈ 1.5 % |
| CPU pro Aufruf | 10 ms | Abruf-Lauf (Feed 51 KB + Zustand 8 KB parsen, Diff, Zustand schreiben): **≈ 0.7 ms warm, ≈ 2.8 ms kalt** (lokal gemessen); übersprungene Auslösung < 0.5 ms; HTTP < 2 ms | ok |
| D1 Rows written | 100 000 / Tag | ≈ 1 250 Abrufe × (1 Zustand + 1 Log-Zeile, falls Änderung) ≈ 2 400; obs 288 × 5 = 1 440; Wartung ≈ 2 400 (obs verdichten/löschen, ≈ 110 Stationen, ≈ 350 Velos) → **≈ 6 300** | ≈ 6 % |
| D1 Rows read | 5 Mio. / Tag | 1 440 (Zustand je Auslösung); Wartung ≈ 175 000 (Scan der 60-Tage-obs); Statusseite ≈ 3 500 → **≈ 180 000** | ≈ 4 % |
| D1 Speicher | 500 MB pro DB | Log ≈ 0.3–0.4 MB/Tag ≈ **130 MB/Jahr**; obs ≈ 3 MB konstant | ≈ 3 Jahre bis zur Schutzgrenze |

Designentscheide, die das klein halten:

- Gefilterter Abruf (`city=126&bikes=false`, 51 KB statt Weltfeed mit mehreren MB).
- **Delta statt Snapshot**: Pro Minute ein vollständiger Stand aller Stationen und Velos wären ≈ 9 KB, also
  ≈ 4.5 GB pro Jahr. Mit Deltas ist es ≈ 1/30 davon.
- Ein Abruf = ein D1-Batch (Transaktion). Der Zustand ist 1 Zeile, statt Hunderte Zeilen pro Station/Velo zu
  aktualisieren (das wären > 400 000 Writes/Tag).
- `obs` hat den Primärschlüssel `(station_id, wslot, ts)` mit `WITHOUT ROWID`, ohne Zusatzindex.
  Prognose-Abfragen sind reine Range-Scans auf dem Schlüssel.
- Stamm- und Velodaten nur 1× täglich, je als ein einziges Statement (`json_each`).
- Antwort-Cache 60 s: Isolate-Speicher, zusätzlich die Cache API. **Achtung:** Die Cache API wirkt nur auf einer
  eigenen Domain, auf `*.workers.dev` ist sie ein No-Op. Der Speicher-Cache wirkt trotzdem.
- Export seitenweise (max. 5 000 Zeilen bzw. 1 440 Log-Zeilen pro Request).

Stellschrauben in `wrangler.toml`:

| Variable | Standard | Bedeutung |
|---|---|---|
| `LOG_RETENTION_DAYS` | `0` (für immer) | Aufbewahrung des Änderungs-Logs |
| `DB_SIZE_LIMIT_MB` | `450` | Schutz: Wird die DB grösser, löscht die tägliche Wartung die ältesten 30 Tage Log. Vorher exportieren! |
| `RAW_RETENTION_DAYS` | `60` | obs → agg verdichten |
| `SNAPSHOT_RETENTION_DAYS` | `365` | alte v1-Snapshots |

Die DB-Grösse zeigt die Statusseite (`DB … MB / 500 MB`) und `/now` (`network.db_mb`).

Seit 09/2026 setzt Cloudflare die D1-Tageslimits hart durch: Bei Überschreitung schlagen Queries bis 00:00 UTC
fehl. Der Cron loggt das dann als Fehler und schreibt nichts. Bei den Werten oben ist viel Reserve.

Mehr Daten ohne Limits und eine Analyse-GUI bietet die Variante [`../raspberry-pi`](../raspberry-pi/README.md).

---

## Datenmodell

Siehe `migrations/0001_init.sql` und `migrations/0002_tracking.sql`.

| Tabelle | Inhalt |
|---|---|
| `log` | `ts, prev_ts, kind, n_st, n_bk, st, bk`. Änderungs-Log (Stationen + Velos), Keyframes alle 6 h |
| `state` | 1 Zeile: aktueller Zustand (Basis für Delta und `/now`), letzte Wartung, DB-Grösse |
| `bikes` | `number, type, first_seen, last_seen` |
| `stations` | `uid, number, name, lat, lng, place_type, first_seen, last_seen` |
| `obs` | `station_id, wslot, ts, bikes, ebikes, free_racks`. Prognose-Rohdaten eigener Stationen, 60 Tage |
| `agg` | `station_id, wslot, n, n_avail, sum_bikes, n_ebike_avail, sum_ebikes, first_ts, last_ts`. Ältere obs verdichtet |
| `snap` | v1-Snapshots (wird nicht mehr beschrieben) |
| `meta` | `last_error` |

- `ts` = Unix-Sekunden UTC (log: auf Minute, obs: auf 5 Minuten abgerundet).
- `wslot` = lokaler Wochen-Slot (Europe/Zurich): `Wochentag (Mo=0 … So=6) × 96 + Stunde × 4 + Minute ÷ 15`.
- `bikes` = `bikes_available_to_rent` (ohne reservierte Velos).

**Update von v1:** Neue Migration anwenden und deployen, dann steht der Cron automatisch auf jede Minute:

```bash
npm run db:migrate:remote
```

```bash
npm run deploy
```

Projektstruktur:

```
src/
  index.ts    Einstieg: fetch (Auth → Routing) + scheduled (Cron)
  config.ts   Feed-URL, city-ID, Stationen, Schwellwerte
  feed.ts     Feed abrufen + validieren + parsen
  cron.ts     Cron-Lauf: Takt/Nachtmodus, Delta/Keyframe, obs, tägliche Wartung
  tracker.ts  Änderungserkennung Stationen + Velos (rein funktional)
  db.ts       alle D1-Queries
  time.ts     Europe/Zurich-Zeitzone + Wochen-Slots (ohne Intl)
  predict.ts  Prognose + Empfehlung
  api.ts      /now, /predict, /plan, Daten der Statusseite
  html.ts     Statusseite
  export.ts   CSV-Export
  auth.ts     Token-Prüfung in konstanter Zeit
  cache.ts    60-s-Cache
migrations/   D1-Schema
scripts/      simulate-cron.sh, export.mjs, reconstruct.mjs (Log → Fahrten/Bewegungen)
test/         vitest + Feed-Fixture + D1-Shim (node:sqlite)
```
