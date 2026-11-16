# sharedmob – Shared-Mobility-Sammler (Schweiz) für den Raspberry Pi

Sammelt **jede Minute** den Stand aller Shared-Mobility-Angebote der Schweiz und legt ihn verlustarm
für die Datenanalyse ab:
- Velos, E-Bikes, E-Trottinette, Cargo-Bikes, E-Mopeds und Carsharing von 46 Anbietern
- ≈ 23 000 Fahrzeuge und ≈ 12 600 Stationen

Läuft 24/7 auf einem Raspberry Pi in Docker, mit Statusseite im WLAN und automatischem Upload zu Google Drive.

```
┌──────────── docker compose ──────────────────────────────────────────────────────┐
│ collector  jede Minute: sharedmobility.ch (GBFS, alle Anbieter) + nextbike direkt │
│            → Zustände & Fahrten in SQLite (data/hot)                              │
│            → alle 6 h Rohfeed komprimiert (data/raw)                              │
│            → täglich 02:00: Vortag als Parquet (data/archive), DB-Backup          │
│ web        Statusseite :8080 – läuft alles? Lücken? Datenqualität? Speicher?      │
│ watchdog   Push aufs Handy (ntfy) bei Ausfall, Speichermangel, Upload-Fehler      │
│ upload     rclone: data/archive + data/raw stündlich → Google Drive               │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## Was die Daten können – und was nicht

Am echten Feed gemessen (11/2026):

| | sharedmobility.ch (GBFS) | nextbike direkt |
|---|---|---|
| Abdeckung | 46 Anbieter, ganze Schweiz | nextbike Schweiz + invia (≈ 480 Stationen, ≈ 1 400 Velos) |
| Fahrzeug-ID | **wechselt nach jeder Fahrt** (GBFS-Datenschutzregel); Bird wechselt sogar jede Minute alle IDs | **feste Velonummer** |
| Einzelfahrten (Start → Ziel) | nur bei Anbietern ohne ID-Wechsel. Die Statusseite zeigt das pro Anbieter (Spalte „neue IDs/Abruf“) | **exakt** |
| Abfahrten und Ankünfte mit Ort und Zeit | ja, für alle | ja |
| Stationsbestände | ja | ja, inkl. Velotypen |

Für die Frage „wiederkehrende vs. überraschende Fahrten“ heisst das: Mit nextbike geht es exakt. Bei
Anbietern mit ID-Wechsel liegen Abfahrten und Ankünfte vor; diese zu Fahrten zuzuordnen, ist ein
statistischer Schritt in der Analyse. Der Sammler speichert dafür alles Nötige, rät aber nichts.

**Lizenz der Daten** (sharedmobility.ch, Bundesamt für Energie): „Open use. Must provide the source.“
Speichern, auswerten und veröffentlichen ist erlaubt, **mit Quellenangabe** (Autor, Titel, Link):
<https://github.com/SFOE/sharedmobility>.

---

## Datenmodell

Gespeichert wird nur, was sich ändert. Ein Velo, das 3 Stunden steht, ist **eine** Zeile (ein „Zustand“, engl. *spell*),
nicht 180. Kleine Positionsänderungen (< 25 m, GPS-Rauschen) und schwankende Akkustände erzeugen keinen neuen Zustand.

| Tabelle | Inhalt |
|---|---|
| `vehicle_spell` | Fahrzeug X war von `start_ts` bis `end_ts` unverändert an Ort/Station Y (Typ, reserviert, defekt, Reichweite) |
| `station_spell` | Station hatte von `start_ts` bis `end_ts` genau diesen Bestand (verfügbar, freie Plätze, ggf. pro Typ) |
| `trip` | Fahrt eines Fahrzeugs mit gleicher ID: Start, Ziel, Luftlinie, Zeitfenster |
| `poll` | jeder Abruf: Erfolg, Dauer, Datenmenge, Fehler |
| `provider_stat` | pro Anbieter und 5 Min: Fahrzeuge, neue/verschwundene IDs, Zustandswechsel (Datenqualität) |
| `gap` | **Datenlücken**: Zeiträume ohne frische Daten je Quelle, mit Ursache (`collector_down`, `fetch_error`, `stale_feed`) |
| `station_info`, `provider_info`, `vehicle_type_info`, `bike_info` | Stammdaten |

**Zeitfenster statt Scheingenauigkeit.** Jeder Zustand speichert auch die Beobachtung davor und danach:

```
start_prev_ts < Zustand beginnt ≤ start_ts      end_ts ≤ Zustand endet < end_next_ts
Fahrt: Abfahrt in (dep_ts, dep_next_ts], Ankunft in (arr_prev_ts, arr_ts]
```

Fällt ein Abruf aus, werden die Fenster entsprechend breiter. Es geht nichts verloren, und die
Unsicherheit ist ehrlich ausgewiesen.

**Gründe:** `start_reason` = `initial` (erster Abruf) · `appeared` (**Ankunft**) · `moved` · `changed`
(z. B. reserviert) · `rekey`. `end_reason` = `gone` (**Abfahrt**) · `moved` · `changed` · `rekey`.

**Fahrt-Arten:**
- `ride`: Fahrzeug verschwindet an A und taucht an B auf.
- `round_trip`: ≥ 5 Min weg, zurück am selben Ort.
- `visible_move`: Wechsel zwischen zwei Abrufen direkt von A nach B (kurze Fahrt oder Umverteilung).

**Identität:** `id_mode = 'id'` verfolgt die Fahrzeug-ID. Vergibt ein Anbieter in einem Abruf (fast) alle IDs
neu, schliesst der Sammler die Zustände als `rekey` und eröffnet sie neu, ohne Schein-Abfahrten oder -Ankünfte.
Passiert das **≥ 3× in 24 h**, wird der Anbieter dauerhaft über die Position verfolgt
(`id_mode = 'pos'`, Schlüssel `@lat,lon,typ#n`). Geparkte Fahrzeuge bleiben so ein einziger Zustand.
Beobachtet: Bird vergibt jede Minute alle IDs neu, Lime periodisch. Eine seltene Neuvergabe schaltet die
exakten Fahrten eines Anbieters dagegen nicht ab.

**Bekannte Eigenheiten der Daten** (bewusst *nicht* weggefiltert, damit nichts verloren geht):
- `visible_move`-Fahrten von 100–500 m innerhalb einer Minute, die kurz danach exakt zurückspringen, sind
  Positions-Ausreisser im Feed. Beobachtet z. B. bei nextbike: Station → frei abgestellt 526 m weg → zurück.
  In der Analyse lassen sie sich als „Hin- und Rücksprung desselben Fahrzeugs innerhalb weniger Minuten“ filtern.
- Einige Trottinett-Anbieter melden Fahrzeuge während der Fahrt weiter als verfügbar, was ebenfalls als `visible_move` erscheint.

**Schutz vor Scheindaten:** Fehlt ein Anbieter mit ≥ 10 Fahrzeugen in einem Abruf komplett (Teilausfall
beim Aggregator), bleiben seine Zustände bis zu 6 h offen, statt Hunderte Scheinfahrten zu erzeugen. Bleibt
der Feed unverändert (gleiches `last_updated`), wird nichts geschlossen.

---

## Ablage und Datenmengen

```
data/
  hot/sharedmob.sqlite                         Arbeits-DB: letzte 4 Tage + alle offenen Zustände
  archive/<tabelle>/date=JJJJ-MM-TT/part-0.parquet   ← dein Analyse-Datensatz (unveränderlich, zstd)
  archive/open/*.parquet, archive/dim/*.parquet      offene Zustände, Stammdaten (täglich aktualisiert)
  raw/JJJJ-MM-TT/HHMM_<quelle>_<feed>.json.zst      Rohfeed alle 6 h (zum Nachrechnen)
  backups/sharedmob-JJJJ-MM-TT.sqlite.zst           DB-Kopie (lokal, letzte 3)
```

Ins Archiv kommt ein Tag nach dem Zeitpunkt, an dem eine Zeile *abgeschlossen* wurde. Diese Zeit steigt
monoton. Ein archivierter Tag ändert sich deshalb nie mehr, und der Upload überträgt jede Datei nur einmal.

Datenmengen (Messung siehe [unten](#gemessene-werte)):

| | pro Tag | pro Jahr |
|---|---|---|
| Download aus dem Internet (1 Abruf/Min) | ≈ 1.7 GB | – |
| Parquet-Archiv | ≈ 5–8 MB | ≈ 2–3 GB |
| Rohdaten (alle 6 h) | ≈ 3.7 MB | ≈ 1.4 GB |
| Arbeits-DB (4 Tage) | – | bleibt bei ≈ 150 MB |

Mit Archiv und Rohdaten zusammen sind das etwa 4 GB pro Jahr. Die 15 GB von Google Drive reichen damit mehrere Jahre.

---

## Installation

### 1. Raspberry Pi

- **Raspberry Pi 4 oder 5** mit **64-bit**-Raspberry-Pi-OS-Lite. Der Raspberry Pi Imager kann dabei Hostname, WLAN und SSH gleich einrichten.
  Das 32-bit-OS geht nicht, weil es pyarrow dafür nicht als fertiges Paket gibt.
- **Speicher: USB-SSD empfohlen** (oder eine „High Endurance“-SD-Karte ab 64 GB). Der Sammler schreibt jede Minute.
- RAM-Bedarf ≈ 200 MB.

```bash
ssh <user>@<hostname>.local
```

### 2. Docker

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
sudo usermod -aG docker $USER
```

Danach einmal ab- und wieder anmelden.

### 3. Sammler starten

```bash
git clone https://github.com/NZumbusch/nextbike-predict.git
```

```bash
cd nextbike-predict/collector
```

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

```bash
docker compose logs -f collector
```

Im Log erscheint jede Minute pro Quelle eine Zeile, z. B.
`[sharedmobility] 16:44:04: 22890 Fahrzeuge, 12598 Stationen, Δ 162/54, Fahrten 1, 1099 KB`.

Statusseite: **http://\<hostname\>.local:8080**.

### 4. Optional: GBFS 2.3 mit E-Mail

sharedmobility.ch empfiehlt den GBFS-2.3-Endpunkt. Dieser verlangt eine gültige E-Mail im
`Authorization`-Header. Ohne E-Mail nutzt der Sammler den offenen 2.0-Endpunkt; die Fahrzeugdaten sind dieselben.

In `.env` eintragen:

```
GBFS_AUTH_EMAIL=deine@adresse.ch
```

Danach `docker compose up -d` ausführen. Schlägt 2.3 fehl, fällt der Sammler automatisch auf 2.0 zurück und zeigt den Grund in Log und
Statusseite (Spalte „Endpunkt“). Hinweis: Der 2.3-Endpunkt ist ungetestet, weil er ohne echte E-Mail nicht
erreichbar war.

---

## Upload zu Google Drive

Der `upload`-Container kopiert stündlich neue Dateien aus `data/archive` und `data/raw` nach
`gdrive:sharedmob`. Auf Google Drive wird **nie gelöscht**, denn das ist dein Datensatz. Einmalige Einrichtung:

**a) Auf dem Pi:**

```bash
docker compose run --rm --entrypoint rclone upload config
```

Antworten: `n` (neues Remote) → Name **`gdrive`** → Storage **`drive`** → client_id/secret leer lassen →
scope **`drive.file`** (rclone sieht nur seine eigenen Dateien) → root_folder_id und service_account leer →
„Edit advanced config?“ `n` → „Use web browser to automatically authenticate?“ **`n`**.
rclone zeigt jetzt einen Befehl `rclone authorize "drive" "eyJ…"`. Das Fenster offen lassen.

**b) Auf dem Laptop:** rclone installieren (<https://rclone.org/downloads/>), genau den angezeigten Befehl
ausführen und im Browser mit Google anmelden. Den ausgegebenen Token-Block kopieren.

**c) Zurück auf dem Pi:** Den Token-Block bei `config_token>` einfügen → „Shared Drive?“ `n` → `y` → `q`. Dann:

```bash
docker compose restart upload
```

```bash
docker compose logs upload
```

Erwartet wird `ok – gdrive:sharedmob (… MB)`. Den Stand zeigt auch die Statusseite. Andere Ziele (OneDrive,
Dropbox, Nextcloud, S3 …) gehen genauso: In a) einen anderen Storage-Typ wählen und `BACKUP_REMOTE` anpassen.
Mit `BACKUP_DIRS=archive raw backups` werden zusätzlich die DB-Kopien hochgeladen.

---

## Überwachung

**Statusseite** (`http://<pi>:8080`):
- Quellen: Erfolgsquote, Dauer, Datenmenge, letzter Fehler.
- **Zeitleiste der Datenlücken** (14 Tage) mit Ursache.
- Anbieter-Tabelle: Fahrzeuge, ID-Wechsel, Fahrten. Ein Klick öffnet die **Anbieter-Seite** mit Verlauf über
  4 Tage (Fahrzeuge im Feed, Aktivität, Anteil neuer IDs). Dort siehst du sofort, wenn ein Anbieter ausfällt
  oder sein Verhalten ändert.
- Speicher mit **Prognose** „voll in ≈ X Tagen“, berechnet aus dem Wachstum der letzten bis zu 7 Tage.
- Status von Archiv, Backup und Upload sowie aktive Warnungen.

**Push-Meldungen aufs Handy** (Dienst `watchdog`, über [ntfy](https://ntfy.sh), gratis, ohne Konto):
1. App „ntfy“ installieren (Android/iOS) und ein **langes, zufälliges Thema** abonnieren, z. B.
   `sharedmob-7f3k9q2x`. Wer das Thema kennt, kann mitlesen; die Meldungen enthalten aber nur Betriebsdaten.
2. Dasselbe Thema in `.env` als `NTFY_TOPIC` eintragen und die Dienste neu starten:

   ```bash
   docker compose up -d
   ```

3. Den Versand testen:

   ```bash
   docker compose exec -u sm watchdog python -m sharedmob.watchdog --test
   ```

Gemeldet wird (Schwellen in `config.toml`, Abschnitt `[alerts]`):

| Meldung | Wann |
|---|---|
| Sammler meldet sich nicht | 15 Min ohne Durchgang |
| Quelle liefert keine frischen Daten | 15 Min, mit letztem Fehler |
| Speicher fast voll / bald voll | < 10 % frei bzw. Prognose < 60 Tage |
| Upload gestört | Upload-Fehler oder 3 h kein Versuch |
| Anbieter fehlt im Feed | hatte vor 24 h ≥ 20 Fahrzeuge, seit 1 h keine |
| Neuer Anbieter | erscheint neu im Feed |
| Wartung fehlgeschlagen | Archiv oder Backup mit Fehler |

Solange ein Problem besteht, wiederholt sich die Meldung alle 6 h. Ist es behoben, kommt „✓ … wieder ok“ mit
der Dauer. Ohne `NTFY_TOPIC` erscheinen die Meldungen nur im Log (`docker compose logs watchdog`).

Die Prüfung läuft als eigener Dienst, damit sie auch dann meldet, wenn der Sammler hängt. Fällt der ganze
Pi aus (Strom, Netz), kann er selbst nichts mehr senden. Dafür eignet sich ein externer Ping-Dienst, z. B.
ein kostenloser Uptime-Check auf `/healthz` über einen Tunnel. Die Lücke selbst wird nach dem Neustart in jedem Fall erfasst.

## Analyse

Auf dem Laptop mit DuckDB direkt auf den Parquet-Dateien. Die Dateien entweder von Google Drive holen
oder über die Statusseite herunterladen.

Archiv von Google Drive holen (rclone auf dem Laptop):

```bash
rclone copy gdrive:sharedmob/archive ./archive
```

Python-Umgebung einrichten und Beispiele ausführen:

```bash
cd analysis && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python examples.py ../archive
```

In eigenen Skripten oder Notebooks:

```python
import load
con = load.connect("../archive")
df = con.sql("SELECT * FROM trip WHERE source = 'nextbike' AND kind = 'ride'").df()
```

Sichten (`analysis/views.sql`):

| Sicht | Inhalt |
|---|---|
| `trip` | Fahrten inkl. `dep_local`, `arr_local`, `duration_min_lower` und `duration_min_upper` |
| `departure` | Abfahrten aller Anbieter |
| `arrival` | Ankünfte aller Anbieter |
| `vehicle_spell` | abgeschlossene + offene Fahrzeug-Zustände |
| `station_spell` | abgeschlossene + offene Stations-Zustände |
| `gap` | Datenlücken, z. B. um Tage mit Ausfällen aus Auswertungen auszuschliessen |
| `station_info`, `provider_info`, `bike_info`, `poll`, `provider_stat` | Stammdaten und Betriebsdaten |

`examples.py` enthält: Überblick pro Anbieter, Fahrten pro Stunde, häufigste Verbindungen, Abfahrten pro
Anbieter, und eine **Beispiel-Definition „wiederkehrend vs. überraschend“**. Dabei gilt eine Fahrt als
wiederkehrend, wenn dieselbe Verbindung (Station oder ≈ 250-m-Zelle) im selben Zeitfenster (Werktag/Wochenende × Stunde)
in ≥ 3 verschiedenen Wochen vorkommt.

Hinweis: nextbike ist doppelt vorhanden, einmal direkt (`source = 'nextbike'`, feste Velonummern) und
einmal im Aggregator. Für nextbike-Fahrten immer die direkte Quelle nehmen.

---

## Konfiguration

`config.toml` (Struktur), `.env` (Ports, Geheimnisse). Nach Änderungen:

```bash
docker compose up -d
```

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `collector.interval_s` / `night_interval_s` | 60 / 60 | Abruf-Takt tags / nachts (01–05 Uhr) |
| `tracking.move_tolerance_m` | 25 | darunter: GPS-Rauschen |
| `tracking.trip_min_distance_m` | 100 | ab dieser Luftlinie: Fahrt |
| `tracking.round_trip_min_s` | 300 | so lange weg + zurück am selben Ort ⇒ Rundfahrt |
| `tracking.trip_link_max_h` | 72 | länger verschwunden ⇒ keine Fahrt (Werkstatt, Depot) |
| `tracking.id_strategy` | `auto` | `id`, `position` oder `auto` (automatische Erkennung) |
| `storage.hot_days` | 4 | Tage in der Arbeits-DB. Mindestens `trip_link_max_h / 24 + 1`, damit Fahrten über einen Neustart verknüpft bleiben und der Vortag archiviert werden kann. Kleinere Werte werden automatisch angehoben. |
| `storage.raw_every_min` | 360 | Rohfeed alle 6 h (`0` = aus) |
| `storage.archive_hour` | 2 | ab dieser Stunde wird der Vortag archiviert |
| `sources[].providers_exclude` | `[]` | Anbieter auslassen, z. B. `["mobility"]` |

Eine weitere GBFS-Quelle (z. B. ein Anbieter, der nicht im Aggregator ist) kommt als zusätzlicher
`[[sources]]`-Block mit `kind = "gbfs"` und der URL seiner `gbfs.json` dazu.

---

## Betrieb

| Was | Befehl |
|---|---|
| Status | `docker compose ps` bzw. die Statusseite |
| Sammler-Log | `docker compose logs -f collector` |
| Archiv + Backup sofort | `docker compose exec -u sm collector python -m sharedmob.collector --archive-now` |
| Update | `git pull && docker compose up -d --build` |
| Stoppen | `docker compose down` (Daten bleiben in `data/`) |

**Robustheit:**
- Die Container starten nach einem Stromausfall neu.
- Offene Zustände werden aus der DB fortgesetzt, auch Fahrten, die über einen Neustart hinweg laufen.
- Jede Datei wird atomar geschrieben (erst temporär, dann umbenannt); Reste eines Abbruchs räumt der Sammler beim Start auf.
- Pro Abruf gibt es genau eine Datenbank-Transaktion.
- Archiv und Backup laufen in einem eigenen Thread, damit das Sammeln weiterläuft.

**Wiederherstellen** aus einem DB-Backup: Erst `docker compose down` ausführen. Dann die Datei
`sharedmob-….sqlite.zst` entpacken (`zstd -d`), nach `data/hot/sharedmob.sqlite` legen und die alten
`-wal`/`-shm`-Dateien löschen. Danach `docker compose up -d`. Das Archiv ist ohnehin auf Google Drive.

---

## Entwicklung

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest
```

```bash
DATA_DIR=./data .venv/bin/python -m sharedmob.collector --once
```

Die Tests laufen ohne Netzwerk, mit Ausschnitten der echten Feeds. Sie decken ab:
- Parser für GBFS v2/v3, sharedmobility 2.0/2.3 und nextbike; Endpunktwahl mit Fallback
- Zustände und Zeitfenster, Fahrten, Rundfahrten, Feed-Aussetzer
- Positions-Identität bei ID-Rotation, Teilausfall-Schutz, unveränderter Feed
- Neustart mitten in einer Fahrt
- Archiv (auch an Tagen mit Zeitumstellung, 23/25 h), Idempotenz und Aufräumen
- Rohdaten-Takt, Backup und Statusseite inkl. Zugriffsschutz
- Lückenerkennung samt Ursachen, Speicherprognose, Watchdog-Meldungen inkl. Wiederholung und Entwarnung

```
sharedmob/
  sources/gbfs.py, sources/nextbike.py   Abruf + Parser → einheitliches Modell (model.py)
  tracker.py     Zustände, Fahrten, Identität, Ausfallschutz (rein funktional)
  store.py       SQLite-Schema, Schreiben, Wiederaufnahme
  archive.py     Parquet-Export, Rohdaten, Backup
  collector.py   Takt, Ablauf, Wartung
  web.py         Statusseite, Anbieter-Seiten, /api/status, /healthz
  charts.py      SVG-Diagramme und Lücken-Zeitleiste
  usage.py       Speicherverbrauch und Prognose
  watchdog.py    Überwachung + Push (ntfy)
analysis/        DuckDB-Sichten, Beispiele
```

## Gemessene Werte

Testlauf am echten Feed, Montag 16.11.2026, 16:45–17:20 Uhr (Abendspitze), beide Quellen, 1-Min-Takt:

| Messgrösse | Wert |
|---|---|
| Fahrzeuge / Stationen im Feed | ≈ 24 300 / ≈ 13 050 (sharedmobility + nextbike) |
| neue Fahrzeug-Zustände | ≈ 165 pro Minute (Spitze) |
| Stationsänderungen | ≈ 73 pro Minute (Spitze) |
| Fahrten mit gleicher ID | ≈ 13 pro Minute, davon nextbike-Ausleihen ≈ 6 pro Minute |
| Download | 1.2 MB pro Minute ⇒ ≈ 1.7 GB pro Tag |
| Abruf + Verarbeitung | sharedmobility ≈ 2.5 s (davon ≈ 0.1–0.3 s Verarbeitung), nextbike ≈ 1 s |
| RAM / CPU des Sammlers | ≈ 230 MB / ≈ 1–2 % im Mittel |
| Parquet pro Zeile | Fahrzeug-Zustand ≈ 40 Byte, Stationsänderung ≈ 12 Byte, Fahrt ≈ 73 Byte |
| Rohdaten pro Snapshot (zstd) | ≈ 0.9 MB (7 MB + 2.5 MB unkomprimiert) |

Hochgerechnet auf einen Tag (Nacht deutlich ruhiger): **≈ 7 MB Parquet + 3.7 MB Rohdaten pro Tag ≈ 4 GB pro Jahr**.

**Fahrten bei rotierenden IDs** (`analysis/match_rotating.py --validate`, gleicher Testlauf): Der Aggregator
liefert nextbike mit ≈ 2 min Verzögerung und rotierenden IDs. Die einfache Zuordnung Abfahrt → Ankunft trifft
**≈ 25 %** der exakten Fahrten aus der direkten Quelle, bei einer Genauigkeit von ≈ 22 %. Einzelfahrten lassen sich bei
rotierenden IDs also nur unsicher rekonstruieren. Für Fragen auf Fahrt-Ebene sind nextbike direkt und Anbieter mit
stabilen IDs die Basis. Für die übrigen Anbieter eignen sich aggregierte Ströme (Abfahrten/Ankünfte pro Gebiet und Zeit).
Die Messung über ganze Tage sowie bessere Modelle (Dauer abhängig von der Distanz, optimale statt gieriger Zuordnung)
sind ein sinnvoller erster Analyse-Schritt.
