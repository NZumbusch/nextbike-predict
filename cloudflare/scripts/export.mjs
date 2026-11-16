#!/usr/bin/env node
/**
 * Lädt alle gesammelten Daten seitenweise über GET /export herunter und schreibt CSVs.
 *
 *   NEXTBIKE_URL=https://nextbike-predict.<dein-subdomain>.workers.dev \
 *   NEXTBIKE_TOKEN=... node scripts/export.mjs [zielordner]
 *
 * Ergebnis (Standard: ./export/):
 *   obs.csv        Rohbeobachtungen eigener Stationen (letzte 60 Tage)
 *   agg.csv        verdichtete Statistik (Station × Wochen-Slot)
 *   stations.csv   Stammdaten aller Stationen (Name, Koordinaten)
 *   snap.csv       Netz-Snapshots roh (data = JSON) – wird inkrementell ergänzt
 *   snap_long.csv  Snapshots "lang": ts, zeit_utc, uid, bikes, ebikes, active – inkrementell
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync, appendFileSync } from "node:fs";
import { join } from "node:path";

const base = (process.env.NEXTBIKE_URL ?? "").replace(/\/$/, "");
const token = process.env.NEXTBIKE_TOKEN;
if (!base || !token) {
  console.error("Bitte NEXTBIKE_URL und NEXTBIKE_TOKEN setzen.");
  process.exit(1);
}
const out = process.argv[2] ?? "export";
mkdirSync(out, { recursive: true });

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

/** Ganze Tabelle; ruft onPage(header, zeilen[]) pro Seite auf. */
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

// Kleine Tabellen: komplett neu schreiben
for (const t of ["stations", "agg", "obs"]) {
  const file = join(out, `${t}.csv`);
  let first = true;
  await each(t, null, (header, lines) => {
    if (first) writeFileSync(file, header + "\n");
    first = false;
    if (lines.length) appendFileSync(file, lines.join("\n") + "\n");
  });
}

// Snapshots: inkrementell ab letztem vorhandenen ts
const snapFile = join(out, "snap.csv");
const longFile = join(out, "snap_long.csv");
let since = null;
if (existsSync(snapFile)) {
  const rows = readFileSync(snapFile, "utf8").trimEnd().split("\n");
  const last = rows[rows.length - 1]?.split(",")[0];
  if (last && /^\d+$/.test(last)) since = last;
}
if (!since) {
  writeFileSync(longFile, "ts,zeit_utc,uid,bikes,ebikes,active\n");
}
let wroteHeader = !!since;

/** CSV-Zeile mit genau einem gequoteten JSON-Feld am Ende parsen */
function parseSnapLine(line) {
  const i = line.indexOf(',"');
  const head = line.slice(0, i).split(",");
  const data = JSON.parse(line.slice(i + 2, -1).replace(/""/g, '"'));
  return { ts: Number(head[0]), data };
}

await each("snap", since, (header, lines) => {
  if (!wroteHeader) writeFileSync(snapFile, header + "\n");
  wroteHeader = true;
  if (!lines.length) return;
  appendFileSync(snapFile, lines.join("\n") + "\n");
  const long = [];
  for (const l of lines) {
    const { ts, data } = parseSnapLine(l);
    const iso = new Date(ts * 1000).toISOString();
    for (const [uid, bikes, ebikes, active] of data) long.push(`${ts},${iso},${uid},${bikes},${ebikes},${active === 0 ? 0 : 1}`);
  }
  appendFileSync(longFile, long.join("\n") + "\n");
});
console.log(`Fertig → ${out}/`);
