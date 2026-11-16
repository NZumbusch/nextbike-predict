# nextbike-predict für den Raspberry Pi

Selbst gehostete Variante ohne Cloud-Limits: sammelt **jede Minute** den kompletten Stand des Luzerner
nextbike-Netzes inklusive jedes einzelnen Velos, erkennt Fahrten und Umverteilungen und zeigt alles in
einer Web-GUI im WLAN. Backups gehen täglich automatisch zu Google Drive.

**Stack:** Python 3.12 + SQLite (WAL) + Flask/waitress, alles in Docker Compose, Backups per rclone.
Begründung: Python und SQLite brauchen auf dem Pi kaum Ressourcen (≈ 50 MB RAM), es gibt keinen
Datenbankserver, der gewartet werden muss, und die SQLite-Datei lässt sich direkt in pandas, R oder
„DB Browser for SQLite“ öffnen. Docker sorgt für Neustart nach Stromausfall und einfache Updates.

```
┌──────────── docker compose ────────────────────────────────────────────┐
│ collector  Feed jede Minute → SQLite (data/nextbike.sqlite)            │
│            03:00 Uhr: konsistentes Backup → data/backups/*.sqlite.gz   │
│ web        Web-GUI + JSON-API auf Port 8080 (liest dieselbe DB)        │
│ backup     rclone: lädt data/backups stündlich zu Google Drive hoch    │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Was gesammelt wird

| Tabelle | Inhalt | Auflösung |
|---|---|---|
| `obs` | Velos, E-Bikes, reserviert für **alle** Stationen | alle 5 Min (Basis der Prognose) |
| `station_change` | jede Bestandsänderung jeder Station | jede Minute |
| `bike_event` | jede Bewegung jedes einzelnen Velos (von → nach, mit Koordinaten) | jede Minute |
| `trip` | erkannte Fahrten: Velo, Start, Ziel, Dauer, Distanz, Hinweis auf Umverteilung | – |
| `stations`, `bikes` | Namen, Nummern, Koordinaten, Velotyp (Velo / E-Bike / E-Cargo) | stündlich |
| `poll` | Protokoll jedes Abrufs (ok, Dauer, Fehler), 30 Tage | jede Minute |

So entsteht eine **Fahrt**: Ein Velo verschwindet an Station A (ausgeliehen) und taucht später an B auf.
- `direct = 1`: Das Velo wechselte innerhalb eines Abrufintervalls direkt von A nach B (sehr kurze Fahrt
  oder Umverteilung).
- `arrivals_same_poll ≥ 3`: Mehrere Velos kamen gleichzeitig an derselben Station an. Das ist mit grosser
  Wahrscheinlichkeit ein Umverteilungs-Transporter.

Die Prognose ist dieselbe wie in der Cloudflare-Version (Anteil ≥ 1 Velo im Wochen-Slot ±15 Min,
mindestens 5 Beobachtungen). Hier funktioniert sie aber für **jede** Station.

---

## Installation

### 1. Raspberry Pi vorbereiten

- Geeignet ist ein Pi 3B+, 4, 5 oder Zero 2 W. Am besten mit **Raspberry Pi OS Lite (64-bit)**,
  geflasht mit dem Raspberry Pi Imager. Dabei Hostname (z. B. `velo`), WLAN und SSH gleich im Imager einstellen.
- Als Speicher eine „High Endurance“-SD-Karte ab 32 GB oder besser eine USB-SSD (siehe [Speicher](#speicher-und-sd-karte)).

Per SSH verbinden:

```bash
ssh <user>@velo.local
```

### 2. Docker installieren

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
sudo usermod -aG docker $USER
```

Danach einmal ab- und wieder anmelden (damit `docker` ohne `sudo` geht).

### 3. Projekt holen und starten

```bash
git clone https://github.com/NZumbusch/nextbike-predict.git
```

```bash
cd nextbike-predict/raspberry-pi
```

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

Der erste Build dauert auf dem Pi ein paar Minuten. Danach:

```bash
docker compose ps
```

```bash
docker compose logs -f collector
```

Im Log sollte jede Minute eine Zeile `Abruf HH:MM: {'ok': True, …}` erscheinen.

### 4. Web-GUI öffnen

Im selben WLAN: **http://velo.local:8080** bzw. `http://<IP-des-Pi>:8080`. Die IP zeigt:

```bash
hostname -I
```

Optionaler Schutz: In `.env` `WEB_TOKEN=<geheim>` setzen und `docker compose up -d` ausführen. Danach einmal
`http://velo.local:8080/?token=<geheim>` öffnen; ein Cookie merkt sich den Token für ein Jahr. Ohne Token ist
die GUI für alle im WLAN offen. Es sind öffentliche Daten, also im Heimnetz meist okay.

---

## Backups zu Google Drive

Der Sammler erstellt täglich ab 03:00 Uhr ein konsistentes Backup (`data/backups/nextbike-JJJJ-MM-TT.sqlite.gz`,
die letzten 7 bleiben lokal). Der `backup`-Container lädt neue Backups stündlich per **rclone** hoch und löscht
dort Backups, die älter als `BACKUP_KEEP_DAYS` sind (Standard 14). Der Status steht unten auf der Übersichtsseite.

Einmalige Einrichtung (der Pi hat keinen Browser, deshalb hilft ein Laptop beim Google-Login):

**a) Auf dem Pi** die rclone-Konfiguration starten:

```bash
docker compose run --rm --entrypoint rclone backup config
```

Antworten: `n` (neues Remote) → Name **`gdrive`** → Storage **`drive`** → client_id/secret leer lassen →
scope **`drive.file`** (rclone sieht nur die Dateien, die es selbst angelegt hat) → root_folder_id und
service_account leer → „Edit advanced config?“ `n` → „Use web browser to automatically authenticate?“ **`n`**.

rclone zeigt jetzt einen Befehl der Form `rclone authorize "drive" "eyJ…"`. Das Fenster offen lassen.

**b) Auf dem Laptop** rclone installieren (<https://rclone.org/downloads/>) und genau diesen angezeigten
Befehl ausführen. Im Browser mit Google anmelden. Das Terminal gibt danach einen Token-Block aus; den kopieren.

**Zurück auf dem Pi:** Den Token-Block bei `config_token>` einfügen → „Shared Drive?“ `n` → `y` (ok) → `q`.

**c) Testen und aktivieren:**

```bash
docker compose run --rm --entrypoint rclone backup lsd gdrive:
```

```bash
docker compose exec -u nb collector python -m nextbike.collector --backup-now
```

```bash
docker compose restart backup
```

Nach ein paar Sekunden steht auf der Übersichtsseite „Upload: … ok – 1 Backups in gdrive:nextbike-backups“,
und in Google Drive gibt es den Ordner `nextbike-backups`.

Statt Google Drive geht jedes rclone-Ziel (OneDrive, Dropbox, Nextcloud/WebDAV, SFTP, S3 …): In a) einen
anderen Storage-Typ wählen und `BACKUP_REMOTE` in `.env` anpassen.

**Wiederherstellen:** Backup herunterladen, dann:

```bash
docker compose down
```

```bash
gunzip -c nextbike-2026-11-16.sqlite.gz > data/nextbike.sqlite && rm -f data/nextbike.sqlite-wal data/nextbike.sqlite-shm
```

```bash
docker compose up -d
```

---

## Web-GUI

| Seite | Inhalt |
|---|---|
| **Übersicht** `/` | meine Stationen jetzt, Prognose nächste 4 h, Netz-Kennzahlen (Velos, frei abgestellt, unterwegs, Fahrten heute), Top-Start/-Ziel-Stationen, Systemstatus (Abrufe, DB-Grösse, Backup, Upload) |
| **Stationen** `/stations` | alle Stationen mit uid (für `config.toml`), Bestand, Ausleihen/Rückgaben heute |
| **Station** `/station/<uid>` | Bestandsverlauf (12 h bis 14 Tage), Prognose, **Wochenmuster** (Heatmap Wochentag × Stunde), Velos vor Ort, letzte Bewegungen |
| **Karte** `/map` | alle Stationen nach Bestand, frei abgestellte Velos (offline, ohne Kartenkacheln) |
| **Fahrten** `/trips` | letzte Fahrten (Filter Velo/Station), häufigste Verbindungen (7 Tage), vermutliche Umverteilungen |
| **Velo** `/bike/<nr>` | Typ, aktueller Ort, alle Fahrten, km und Stunden |
| **Daten** `/export` | ganze DB als SQLite-Datei, jede Tabelle als CSV (alles / 24 h / 7 Tage), lokale Backups |

### JSON-API (gleiches Format wie die Cloudflare-Version)

```bash
curl http://velo.local:8080/api/now
```

```bash
curl "http://velo.local:8080/api/predict?station=home&at=07:45"
```

```bash
curl "http://velo.local:8080/api/plan?at=07:45"
```

```bash
curl "http://velo.local:8080/api/plan?at=17:30&dir=ziel"
```

`/api/predict` nimmt auch die uid jeder beliebigen Station. Mit Token zusätzlich `-H "Authorization: Bearer <geheim>"` angeben.
`/now`, `/predict` und `/plan` leiten auf `/api/…` weiter. `/healthz` liefert 200, solange der Sammler läuft.

Von unterwegs (z. B. fürs Morgenbriefing) erreichst du den Pi am einfachsten über ein VPN wie Tailscale oder
WireGuard, statt Port 8080 ins Internet freizugeben.

---

## Konfiguration

`config.toml` (wird in den Container gemountet). Nach Änderungen:

```bash
docker compose restart
```

- **Stationen:** uid auf der GUI-Seite *Stationen* nachschlagen und im Block `[[stations]]` eintragen.
  `group = "start"` (Hinweg) oder `"ziel"` (Rückweg), pro Gruppe genau eine Stammstation mit `primary = true`.
  Weil ohnehin alle Stationen gesammelt werden, gibt es für neu eingetragene Stationen sofort die volle Historie.
- **Takt:** `poll_seconds` (Standard 60, minimal 30) und `night_poll_seconds` für 01–05 Uhr. Auf dem Pi
  gibt es keine Limits, deshalb ist der Standard auch nachts 60 s. Wer Speicher sparen will, setzt 300.
- **Prognose:** `min_obs`, `plan_threshold`.
- **Wartung:** Stunde des Backups, Anzahl lokaler Backups, Aufbewahrung des Abruf-Protokolls.

`.env`: `WEB_PORT`, `WEB_TOKEN`, `BACKUP_REMOTE`, `BACKUP_KEEP_DAYS`, `BACKUP_UPLOAD_INTERVAL`.

---

## Analyse

Am einfachsten lädst du unter *Daten* die ganze `nextbike.sqlite` herunter und öffnest sie in pandas:

```python
import sqlite3, pandas as pd
con = sqlite3.connect("nextbike.sqlite")
trips = pd.read_sql("SELECT t.*, s.name AS start_name, e.name AS end_name FROM trip t "
                    "LEFT JOIN stations s ON s.uid = t.start_uid LEFT JOIN stations e ON e.uid = t.end_uid "
                    "WHERE direct = 0", con)
trips["start"] = pd.to_datetime(trips.start_ts, unit="s", utc=True).dt.tz_convert("Europe/Zurich")
trips.groupby(trips.start.dt.hour).size().plot.bar()            # Ausleihen pro Stunde
trips.groupby(["start_name", "end_name"]).size().nlargest(20)    # Quelle-Ziel-Matrix
```

Weitere Ideen: Tagesgang und Leerstand pro Station (`station_change`), Umverteilungen
(`arrivals_same_poll >= 3`) nach Uhrzeit und Station, Auslastung pro Velo und Velotyp, typische Fahrdauern
je Strecke, Netto-Zu-/Abfluss pro Quartier.

---

## Speicher und SD-Karte

Gemessen bzw. hochgerechnet (≈ 107 Stationen, ≈ 350 Velos, 1-Min-Takt):

| | pro Tag | pro Jahr |
|---|---|---|
| Datenbank | ≈ 1.2 MB | ≈ 450 MB |
| Backup (gzip) | ≈ 0.4 MB pro gesammeltem Tag | eine Vollkopie nach 1 Jahr ≈ 150 MB |
| Google Drive (14 Tage × Vollkopie) | | ≈ 2 GB nach 1 Jahr (Free: 15 GB) |

Den grössten Anteil hat `obs` (alle Stationen alle 5 Min). Sinkt der Platz, kann `BACKUP_KEEP_DAYS` kleiner
gewählt werden. Grundsätzlich reicht eine 32-GB-Karte viele Jahre.

SD-Karten-Schonung ist eingebaut: SQLite im WAL-Modus mit `synchronous=NORMAL` (ein Abruf = eine kleine
Transaktion) und begrenzte Docker-Logs. Für jahrelangen 24/7-Betrieb ist eine USB-SSD trotzdem die robustere Wahl.

---

## Betrieb

| Was | Befehl |
|---|---|
| Status | `docker compose ps` |
| Logs Sammler | `docker compose logs -f collector` |
| Logs Upload | `docker compose logs -f backup` |
| Backup sofort | `docker compose exec -u nb collector python -m nextbike.collector --backup-now` |
| Update | `git pull && docker compose up -d --build` |
| Stoppen | `docker compose down` (Daten bleiben in `data/`) |

Alle Container starten nach einem Stromausfall automatisch neu (`restart: unless-stopped`). Ausfälle
des Feeds werden im Abruf-Protokoll vermerkt, und es wird dann nichts geschrieben. Velos, die während einer
Lücke bewegt wurden, erscheinen danach als Bewegung über die ganze Lücke; es geht nichts verloren.

---

## Entwickeln ohne Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/pytest
```

```bash
DATA_DIR=./data .venv/bin/python -m nextbike.collector --once
```

```bash
DATA_DIR=./data .venv/bin/python -m nextbike.web
```

Tests (pytest, ohne Netzwerk): Feed-Parsing anhand eines echten Feed-Ausschnitts (mit Velo-Liste),
Zeitzone/DST, Prognose und Empfehlung, Fahrten- und Umverteilungserkennung von Abruf bis Datenbank,
Neustart mit „unterwegs“-Velos, Feed-Ausfälle, Backup und Rotation, alle Seiten der Web-GUI, API-Fehler, Token-Schutz.

```
nextbike/
  config.py     config.toml + Umgebungsvariablen
  feed.py       Feed abrufen, validieren, parsen (Stationen, Velonummern, Velotypen)
  tracker.py    Änderungs- und Fahrten-Erkennung
  db.py         Schema, Schreiben, Abfragen
  collector.py  Abruf-Schleife, Wartung, Backup-Auslösung
  backup.py     SQLite-Backup-API → gzip, Rotation
  predict.py    Prognose + Empfehlung
  timeutil.py   Europe/Zurich, Wochen-Slots
  web.py        Flask-App: Seiten, CSV/SQLite-Export, JSON-API
  svg.py        Diagramme, Heatmap, Karte (serverseitiges SVG)
  templates/, static/
backup/rclone-loop.sh   Upload-Schleife im rclone-Container
entrypoint.sh           korrigiert Rechte von data/, startet als unprivilegierter User
```
