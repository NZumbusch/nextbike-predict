#!/bin/sh
# Kopiert neue/geänderte Dateien aus data/archive und data/raw zum rclone-Remote.
# Auf dem Remote wird NIE gelöscht (das ist dein Datensatz). Ergebnis → data/upload-status.txt
REMOTE="${BACKUP_REMOTE:-gdrive:sharedmob}"
INTERVAL="${BACKUP_UPLOAD_INTERVAL:-3600}"
DIRS="${BACKUP_DIRS:-archive raw}"
STATUS=/data/upload-status.txt

while true; do
  now="$(date -u '+%Y-%m-%d %H:%M UTC')"
  if [ ! -s /config/rclone/rclone.conf ]; then
    echo "$now: rclone nicht eingerichtet (rclone/rclone.conf fehlt) – siehe README" | tee "$STATUS"
  else
    ok=1
    for d in $DIRS; do
      [ -d "/data/$d" ] || continue
      rclone copy "/data/$d" "$REMOTE/$d" --exclude '.*' --transfers 2 --log-level NOTICE || ok=0
    done
    if [ "$ok" = 1 ]; then
      size="$(rclone size "$REMOTE" --json 2>/dev/null | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')"
      echo "$now: ok – $REMOTE ($(( ${size:-0} / 1000000 )) MB)" | tee "$STATUS"
    else
      echo "$now: FEHLER beim Upload nach $REMOTE (docker compose logs upload)" | tee "$STATUS"
    fi
  fi
  sleep "$INTERVAL"
done
