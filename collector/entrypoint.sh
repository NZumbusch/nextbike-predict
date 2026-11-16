#!/bin/sh
# Docker legt ./data beim ersten Start oft als root an ⇒ Rechte korrigieren,
# danach als unprivilegierter User "sm" weiterlaufen.
set -e
mkdir -p /data/hot /data/archive /data/raw /data/backups
find /data -maxdepth 2 ! -user sm -exec chown sm:sm {} + 2>/dev/null || true
exec setpriv --reuid=sm --regid=sm --init-groups "$@"
