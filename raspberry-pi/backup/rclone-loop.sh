#!/bin/sh
# Lädt neue Backups aus /backups zum rclone-Remote hoch und löscht dort alte.
# Schreibt das Ergebnis nach /backups/upload-status.txt (die Web-GUI zeigt es an).
REMOTE="${BACKUP_REMOTE:-gdrive:nextbike-backups}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
INTERVAL="${BACKUP_UPLOAD_INTERVAL:-3600}"
STATUS=/backups/upload-status.txt

while true; do
  now="$(date -u '+%Y-%m-%d %H:%M UTC')"
  if [ ! -s /config/rclone/rclone.conf ]; then
    echo "$now: rclone nicht eingerichtet (rclone/rclone.conf fehlt) – siehe README" | tee "$STATUS"
  elif rclone copy /backups "$REMOTE" --include 'nextbike-*.sqlite.gz' --log-level NOTICE; then
    rclone delete "$REMOTE" --include 'nextbike-*.sqlite.gz' --min-age "${KEEP}d" --log-level NOTICE || true
    n="$(rclone lsf "$REMOTE" --include 'nextbike-*.sqlite.gz' 2>/dev/null | wc -l)"
    echo "$now: ok – $n Backups in $REMOTE" | tee "$STATUS"
  else
    echo "$now: FEHLER beim Upload nach $REMOTE (Logs: docker compose logs backup)" | tee "$STATUS"
  fi
  sleep "$INTERVAL"
done
