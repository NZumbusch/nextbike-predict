#!/bin/sh
# Docker legt ./data beim ersten Start oft als root an ⇒ Rechte einmal korrigieren,
# dann als unprivilegierter User "nb" weiterlaufen.
set -e
mkdir -p /data/backups
find /data -maxdepth 2 ! -user nb -exec chown nb:nb {} + 2>/dev/null || true
exec setpriv --reuid=nb --regid=nb --init-groups "$@"
