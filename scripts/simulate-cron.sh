#!/usr/bin/env bash
# Simuliert lokal einen Cron-Lauf:
#   1. lokale D1-Migrations anwenden
#   2. `wrangler dev --test-scheduled` starten
#   3. /__scheduled aufrufen (nur lokal vorhanden, in Produktion gibt es diesen Pfad nicht)
#   4. Ergebnis in der lokalen D1 zeigen und /now abfragen
# Aufruf: npm run simulate-cron            (echter Feed)
#         FIXTURE=1 npm run simulate-cron  (offline: Feed-Ausschnitt aus test/fixtures)
#         RUNS=3 …                         (mehrere Läufe)
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8787}"
RUNS="${RUNS:-1}"

if [[ ! -f .dev.vars ]]; then
  cp .dev.vars.example .dev.vars
  echo "→ .dev.vars aus .dev.vars.example angelegt (nur lokal, nicht committen)"
fi
TOKEN="$(grep -E '^API_TOKEN=' .dev.vars | cut -d= -f2-)"

echo "→ Migrations (lokal)"
npx wrangler d1 migrations apply nextbike --local >/dev/null

EXTRA=()
FPID=""
if [[ "${FIXTURE:-0}" == "1" ]]; then
  FPORT=$((PORT + 1))
  node -e "
    const fs=require('fs'),http=require('http');
    const body=fs.readFileSync('test/fixtures/feed-luzern.json');
    http.createServer((q,r)=>{r.writeHead(200,{'content-type':'application/json'});r.end(body)}).listen($FPORT,'127.0.0.1');
  " &
  FPID=$!
  EXTRA=(--var "FEED_URL:http://127.0.0.1:$FPORT/feed.json")
  echo "→ Fixture-Feed auf Port $FPORT"
fi

echo "→ starte wrangler dev auf Port $PORT"
LOG="$(mktemp)"
# eigene Prozessgruppe, damit beim Beenden auch workerd mitgestoppt wird
setsid npx wrangler dev --test-scheduled --port "$PORT" "${EXTRA[@]}" >"$LOG" 2>&1 &
PID=$!
cleanup() {
  kill -- -"$PID" 2>/dev/null || true
  [[ -n "$FPID" ]] && kill "$FPID" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if grep -q "Ready on" "$LOG"; then break; fi
  if grep -q "ERROR" "$LOG"; then cat "$LOG"; exit 1; fi
  sleep 1
done

for i in $(seq 1 "$RUNS"); do
  echo "→ Cron-Lauf $i/$RUNS"
  curl -s "http://127.0.0.1:$PORT/__scheduled?cron=*/5+*+*+*+*" >/dev/null
  [[ $i -lt $RUNS ]] && sleep 2
done
sleep 2; [[ -n "${DEBUG:-}" ]] && cat "$LOG"
echo "--- Worker-Log ---"
grep -E "cron|Feed|Wartung|Station|scheduled|ERROR" "$LOG" || true

echo "--- lokale D1 ---"
npx wrangler d1 execute nextbike --local --command \
  "SELECT (SELECT count(*) FROM obs) AS obs, (SELECT count(*) FROM snap) AS snaps, (SELECT count(*) FROM stations) AS stations, (SELECT count(*) FROM agg) AS agg" \
  2>/dev/null | grep -A20 '"results"' | head -12 || true

echo "--- GET /now ---"
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/now?nocache=$RANDOM"
echo
echo "--- GET /now ohne Token (erwartet 401) ---"
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:$PORT/now?nocache=$RANDOM"
echo "--- GET /plan?at=07:45 ---"
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/plan?at=07:45&nocache=$RANDOM" | head -c 600
echo
echo "--- GET / (HTML, Token per Query) ---"
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" "http://127.0.0.1:$PORT/?token=$TOKEN&nocache=$RANDOM"
