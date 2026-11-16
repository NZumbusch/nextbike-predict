#!/usr/bin/env node
/**
 * Lädt alle gesammelten Daten seitenweise über GET /export herunter und schreibt CSVs.
 *
 *   NEXTBIKE_URL=https://nextbike-predict.<dein-subdomain>.workers.dev \
 *   NEXTBIKE_TOKEN=... node scripts/export.mjs [zielordner]
 *
 * Ergebnis (Standard: ./export/):
 *   stations.csv, bikes.csv, agg.csv, obs.csv   jedes Mal komplett neu
 *   log.csv               Änderungs-Log roh                          (inkrementell)
 *   station_changes.csv   jede Bestandsänderung jeder Station        (inkrementell)
 *   bike_events.csv       jede Bewegung jedes Velos                  (inkrementell)
 *   trips.csv             Fahrten Ausleihe → Rückgabe                (inkrementell)
 *   snap.csv / snap_long.csv   alte v1-Snapshots, falls vorhanden    (inkrementell)
 *   .reconstruct-state.json    Zwischenstand für den nächsten inkrementellen Lauf
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync, appendFileSync } from "node:fs";
import { join } from "node:path";
import { applyRow, newState, parseCsvLine, BIKE_EVENTS_HEADER, STATION_CHANGES_HEADER, TRIPS_HEADER } from "./reconstruct.mjs";

const base = (process.env.NEXTBIKE_URL ?? "").replace(/\/$/, "");
const token = process.env.NEXTBIKE_TOKEN;
if (!base || !token) {
  console.error("Bitte NEXTBIKE_URL und NEXTBIKE_TOKEN setzen.");
  process.exit(1);
}
const out = process.argv[2] ?? "export";
mkdirSync(out, { recursive: true });
const file = (n) => join(out, n);

async function page(table, cursor) {
  const u = new URL(`${base}/export`);
  u.searchParams.set("table", table);
  if (cursor) u.searchParams.set("cursor", cursor);
  for (let attempt = 1; ; attempt++) {
    const r = await fetch(u, { headers: { authorization: `Bearer ${token}` } });
    if (r.ok) return { text: await r.text(), next: r.headers.get("x-next-cursor") };
    if (attempt >= 4 || r.status === 401 || r.status === 400) throw new Error(`${table}: HTTP ${r.status} ${await r.text()}`);
    await new Promise((res) => setTimeout(res, 1000 * attempt));
  }
}

/** Ganze Tabelle; onPage(header, zeilen[]) pro Seite. */
async function each(table, cursor, onPage) {
  let n = 0;
  do {
    const { text, next } = await page(table, cursor);
    const lines = text.split("\n").filter(Boolean);
    const header = lines.shift();
    onPage(header, lines);
    n += lines.length;
    cursor = next;
    process.stdout.write(`\r${table}: ${n} Zeilen`);
  } while (cursor);
  process.stdout.write("\n");
  return n;
}

function lastTs(path) {
  if (!existsSync(path)) return null;
  const rows = readFileSync(path, "utf8").trimEnd().split("\n");
  const v = rows[rows.length - 1]?.split(",")[0];
  return v && /^\d+$/.test(v) ? v : null;
}

const csv = (rows) => rows.map((r) => r.map((v) => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : v)).join(",")).join("\n") + "\n";

// 1) Kleine Tabellen komplett
const coords = new Map();
const types = new Map();
for (const t of ["stations", "bikes", "agg", "obs"]) {
  let first = true;
  await each(t, null, (header, lines) => {
    if (first) writeFileSync(file(`${t}.csv`), header + "\n");
    first = false;
    if (!lines.length) return;
    appendFileSync(file(`${t}.csv`), lines.join("\n") + "\n");
    for (const l of lines) {
      const f = parseCsvLine(l);
      if (t === "stations" && f[3] !== "") coords.set(Number(f[0]), [Number(f[3]), Number(f[4])]);
      if (t === "bikes" && f[1]) types.set(f[0], f[1]);
    }
  });
}

// 2) Änderungs-Log inkrementell + Rekonstruktion
const stateFile = file(".reconstruct-state.json");
const since = lastTs(file("log.csv"));
let state = since && existsSync(stateFile) ? JSON.parse(readFileSync(stateFile, "utf8")) : null;
if (!state || !since) {
  state = newState();
  writeFileSync(file("station_changes.csv"), STATION_CHANGES_HEADER + "\n");
  writeFileSync(file("bike_events.csv"), BIKE_EVENTS_HEADER + "\n");
  writeFileSync(file("trips.csv"), TRIPS_HEADER + "\n");
}
let logHeader = !!(since && state.ts);
await each("log", logHeader ? since : null, (header, lines) => {
  if (!logHeader) writeFileSync(file("log.csv"), header + "\n");
  logHeader = true;
  if (!lines.length) return;
  appendFileSync(file("log.csv"), lines.join("\n") + "\n");
  const sc = [], ev = [], tr = [];
  for (const l of lines) {
    const f = parseCsvLine(l);
    const row = { ts: Number(f[0]), prev_ts: f[1] === "" ? null : Number(f[1]), kind: f[2], st: JSON.parse(f[5]), bk: JSON.parse(f[6]) };
    const o = applyRow(state, row, coords, types);
    sc.push(...o.stationChanges);
    ev.push(...o.events);
    tr.push(...o.trips);
  }
  if (sc.length) appendFileSync(file("station_changes.csv"), csv(sc));
  if (ev.length) appendFileSync(file("bike_events.csv"), csv(ev));
  if (tr.length) appendFileSync(file("trips.csv"), csv(tr));
  writeFileSync(stateFile, JSON.stringify(state));
});

// 3) Alte v1-Snapshots (falls vorhanden)
const snapSince = lastTs(file("snap.csv"));
let snapHeader = !!snapSince;
await each("snap", snapSince, (header, lines) => {
  if (!lines.length) return;
  if (!snapHeader) {
    writeFileSync(file("snap.csv"), header + "\n");
    writeFileSync(file("snap_long.csv"), "ts,zeit_utc,uid,bikes,ebikes,active\n");
    snapHeader = true;
  }
  appendFileSync(file("snap.csv"), lines.join("\n") + "\n");
  const long = [];
  for (const l of lines) {
    const f = parseCsvLine(l);
    const ts = Number(f[0]);
    const iso = new Date(ts * 1000).toISOString();
    for (const [uid, bikes, ebikes, active] of JSON.parse(f[6])) long.push(`${ts},${iso},${uid},${bikes},${ebikes},${active === 0 ? 0 : 1}`);
  }
  appendFileSync(file("snap_long.csv"), long.join("\n") + "\n");
});
console.log(`Fertig → ${out}/`);
