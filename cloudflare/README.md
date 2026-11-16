# nextbike-predict 🚲 Luzern

Kostenloser Velo-Verfügbarkeits-Tracker mit Prognose für **nextbike Luzern**, als
Cloudflare Worker (TypeScript) mit Cron Trigger und D1-Datenbank. Läuft komplett im Free Plan.

- alle 5 Minuten: aktuellen Stand holen und speichern
  - pro eigener Station: Velos total, davon E-Bikes, freie Plätze
  - zusätzlich ein kompakter Snapshot **aller ~110 Luzerner Stationen** (≈1.5 KB) für spätere
    Analysen (Verteilung, Umverteilung, Tagesgänge)
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
8. [Alternativen zu Cloudflare](#alternativen-zu-cloudflare)
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

### Robustheit

- Feed nicht erreichbar / kein JSON / falsche Struktur / weniger als 20 Stationen → **nichts wird geschrieben**,
  Fehler wird geloggt (`npx wrangler tail`) und als `last_error` gemerkt (Statusseite zeigt ihn an).
- Ein Retry nach 1.5 s, Timeout 10 s.
- Unplausible Werte (negativ, kein Integer, > 500) → diese Station wird in diesem Lauf ausgelassen.
- Station fehlt oder ist in Wartung → Lücke statt Null-Zeile. Die Statistik zählt nur vorhandene Beobachtungen.
- Zeitstempel = tatsächlicher Abrufzeitpunkt, auf 5 Min abgerundet. Verspätete Läufe landen im richtigen
  Slot, doppelte Läufe im selben 5-Min-Fenster werden per Primärschlüssel ignoriert.
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

Ausgabe: `https://nextbike-predict.<subdomain>.workers.dev`. Ab jetzt läuft der Cron alle 5 Minuten.

**8. Prüfen** (nach ≤ 5 Minuten)

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
| `GET /export?table=obs\|agg\|snap\|stations[&cursor=…][&limit=…]` | CSV-Export, seitenweise |

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
Da die Netz-Snapshots alle Stationen enthalten, lässt sich die Historie einer neuen Station auch
nachträglich aus `snap` rekonstruieren (z. B. in der Offline-Analyse).

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

Offline mit dem Feed-Ausschnitt aus `test/fixtures/` und mehreren Läufen:

```bash
FIXTURE=1 RUNS=3 npm run simulate-cron
```

Dev-Server manuell starten:

```bash
npm run dev
```

Dann `http://localhost:8787/?token=<API_TOKEN aus .dev.vars>` öffnen, oder einen Cron-Lauf auslösen:

```bash
curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"
```

**Tests und Typprüfung** (vitest, ohne Netzwerk; SQL wird gegen echtes SQLite (`node:sqlite`) mit den Migrations getestet):

```bash
npm test
```

```bash
npm run typecheck
```

Die Tests decken ab: Feed-Parsing anhand eines echten Feed-Ausschnitts, Zeitzonen und Slots (gegen
`Intl` über 7 Jahre inkl. aller DST-Wechsel), Prognose und Empfehlung, Cron-Idempotenz, Feed-Ausfälle,
die Verdichtung (Prognose vor und nach der Verdichtung identisch), Auth (401 ohne D1-Zugriff), Cache und Export-Paging.

---

## Daten exportieren und analysieren

**Variante A: Export-Script** (inkrementell, gut für regelmässige Backups):

```bash
NEXTBIKE_URL=$URL NEXTBIKE_TOKEN=$TOKEN npm run export
```

Schreibt nach `./export/` (per `.gitignore` ausgeschlossen):

| Datei | Inhalt |
|---|---|
| `obs.csv` | Rohbeobachtungen eigener Stationen (letzte 60 Tage) |
| `agg.csv` | verdichtete Statistik (Station × Wochen-Slot, ältere Daten) |
| `stations.csv` | alle Stationen mit Name, Nummer, Koordinaten |
| `snap.csv` | Netz-Snapshots roh (`data` = JSON `[[uid, bikes, ebikes], …]`; ein 4. Element `0` heisst inaktiv) |
| `snap_long.csv` | Snapshots im Langformat `ts, zeit_utc, uid, bikes, ebikes, active` – direkt für pandas/R |

`snap.csv` und `snap_long.csv` werden bei jedem Aufruf nur ergänzt. Tipp: einmal im Monat laufen
lassen, dann hast du die Rohdaten unbegrenzt lokal, unabhängig von der Aufbewahrung in D1.

**Variante B: Ganze DB als SQL-Dump:**

```bash
npx wrangler d1 export nextbike --remote --output=nextbike.sql
```

**Variante C: Im Browser** über die Download-Links unten auf der Statusseite (je eine Seite; Snapshots der letzten 24 h).

Ideen für Analysen: Tagesgang pro Station, Netto-Zu-/Abfluss pro Station und Stunde (Differenzen
aufeinanderfolgender Snapshots), Umverteilungsfahrten erkennen (viele Velos verschwinden an einer Station
und tauchen gleichzeitig an einer anderen auf), Anteil E-Bikes über die Zeit, leere/volle Stationen pro Wochentag.

---

## Verbrauch gegen die Free-Limits

Gemessen bzw. abgeschätzt für 5 eigene Stationen und ≈ 110 Stationen im Netz:

| Ressource | Free-Limit | Verbrauch | Anteil |
|---|---|---|---|
| Cron Triggers | 5 pro Account | 1 | – |
| Requests | 100 000 / Tag | 288 Cron-Läufe + eigene Aufrufe (≈ 50–200) | < 1 % |
| CPU pro Aufruf | 10 ms | Cron: JSON-Parse + Auswertung ≈ 0.3 ms (lokal gemessen), realistisch 1–3 ms. HTTP: < 2 ms | ok |
| D1 Rows written | 100 000 / Tag | 6 pro Lauf × 288 = 1 728; Wartung ≈ 2 300 (1 440 obs löschen, ≤ 480 agg-Upserts, 288 alte Snapshots, ≈ 115 Stammdaten) → **≈ 4 000** | ≈ 4 % |
| D1 Rows read | 5 Mio. / Tag | Cron 288; Wartung ≈ 2 × 86 400 (Scan der 60-Tage-Rohdaten); `/predict` ≈ 100; Statusseite ≈ 3 500 → **≈ 200 000** bei normaler Nutzung | ≈ 4 % |
| D1 Speicher | 500 MB pro DB (5 GB total) | obs ≈ 86 000 Zeilen ≈ 3 MB; Snapshots ≈ 430 KB/Tag ≈ **160 MB/Jahr** | ≈ 35 % nach 1 Jahr |

Designentscheide, die das klein halten:

- Gefilterter Abruf (`city=126&bikes=false`, 51 KB statt Weltfeed mit mehreren MB).
- `obs` hat den Primärschlüssel `(station_id, wslot, ts)` mit `WITHOUT ROWID`. Es gibt keinen Zusatzindex,
  also kostet jeder Insert genau 1 geschriebene Zeile, und Prognose-Abfragen sind reine Range-Scans auf dem Schlüssel.
- Das Netz wird als **eine** Snapshot-Zeile pro Lauf gespeichert statt ≈ 110 Zeilen. Pro Station und Lauf
  einzeln wären das ≈ 32 000 Writes/Tag plus Verdichtung, zu knapp am Limit.
- Stammdaten nur 1× täglich, als ein einziges Statement (`json_each`).
- Antwort-Cache 60 s: Isolate-Speicher, zusätzlich die Cache API. **Achtung:** Die Cache API wirkt nur auf einer
  eigenen Domain, auf `*.workers.dev` ist sie ein No-Op. Der Speicher-Cache wirkt trotzdem.
- Export seitenweise (max. 5 000 Zeilen bzw. 288 Snapshots pro Request).

Stellschrauben in `wrangler.toml`: `SNAPSHOT_RETENTION_DAYS` (Standard 365, `0` = für immer, dann nach ≈ 3 Jahren
an der 500-MB-Grenze; vorher exportieren) und `RAW_RETENTION_DAYS` (Standard 60).

Seit 09/2026 setzt Cloudflare die D1-Tageslimits hart durch: Bei Überschreitung schlagen Queries bis 00:00 UTC
fehl. Der Cron loggt das dann als Fehler und schreibt nichts. Bei den Werten oben ist viel Reserve.

---

## Alternativen zu Cloudflare

Cloudflare Workers + D1 passt hier am besten: echter 5-Minuten-Cron, SQL-Datenbank, HTTP-Endpunkte,
alles gratis und ohne Server. Geprüfte Alternativen:

- **GitHub Actions (Cron) + CSV im Repo:** gratis, aber `schedule` ist unzuverlässig (oft 10–30 Min Verspätung,
  bei Last ausgelassen) und es gibt keine HTTP-API für `/plan`. Für die Datensammlung allein geht es, für die Prognose nicht.
- **Raspberry Pi / eigener Rechner mit SQLite:** volle Kontrolle und unbegrenzte Rohdaten, aber er muss 24/7 laufen.
- **Supabase/Neon (Postgres) + externer Cron:** mehr SQL-Komfort, aber zwei Dienste und Free-Projekte pausieren bei Inaktivität.

Erweiterung für später: Das Feld `bike_numbers` (Velonummern pro Station) ist im Feed enthalten. Mit ihm könnte man
einzelne Velos verfolgen und so Fahrten und Umverteilung exakt rekonstruieren. Das kostet ≈ 3 KB mehr pro Snapshot
(≈ 300 MB/Jahr, dann besser kürzere Aufbewahrung und regelmässiger Export).

---

## Datenmodell

Siehe `migrations/0001_init.sql`.

| Tabelle | Inhalt |
|---|---|
| `obs` | `station_id, wslot, ts, bikes, ebikes, free_racks`. Rohdaten eigener Stationen, 60 Tage |
| `agg` | `station_id, wslot, n, n_avail, sum_bikes, n_ebike_avail, sum_ebikes, first_ts, last_ts`. Ältere Rohdaten verdichtet |
| `snap` | `ts, n_stations, total_bikes, total_ebikes, free_floating, booked, data`. Netz-Snapshot pro Lauf |
| `stations` | `uid, number, name, lat, lng, place_type, first_seen, last_seen` |
| `meta` | `last_maint`, `last_error` |

- `ts` = Unix-Sekunden UTC, auf 5 Minuten abgerundet.
- `wslot` = lokaler Wochen-Slot (Europe/Zurich): `Wochentag (Mo=0 … So=6) × 96 + Stunde × 4 + Minute ÷ 15`.
- `bikes` = `bikes_available_to_rent` (ohne reservierte Velos).

Projektstruktur:

```
src/
  index.ts    Einstieg: fetch (Auth → Routing) + scheduled (Cron)
  config.ts   Feed-URL, city-ID, Stationen, Schwellwerte
  feed.ts     Feed abrufen + validieren + parsen
  cron.ts     Cron-Lauf + Entscheidung über tägliche Wartung
  db.ts       alle D1-Queries
  time.ts     Europe/Zurich-Zeitzone + Wochen-Slots (ohne Intl)
  predict.ts  Prognose + Empfehlung
  api.ts      /now, /predict, /plan, Daten der Statusseite
  html.ts     Statusseite
  export.ts   CSV-Export
  auth.ts     Token-Prüfung in konstanter Zeit
  cache.ts    60-s-Cache
migrations/   D1-Schema
scripts/      simulate-cron.sh, export.mjs
test/         vitest + Feed-Fixture + D1-Shim (node:sqlite)
```
