/**
 * Datenexport als CSV, seitenweise (damit CPU-Zeit und D1-Reads pro Request klein bleiben).
 *   GET /export?table=obs|agg|snap|stations[&cursor=…][&limit=…]
 * Nächste Seite: Header `X-Next-Cursor` (fehlt ⇒ letzte Seite). Siehe scripts/export.mjs.
 */
import type { Env } from "./db";
import { HttpError } from "./api";

interface TableDef {
  cols: string[];
  /** Spalten, die den Cursor bilden (= Primärschlüssel-Reihenfolge) */
  key: string[];
  maxLimit: number;
  defLimit: number;
}

const TABLES: Record<string, TableDef> = {
  obs: { cols: ["station_id", "wslot", "ts", "bikes", "ebikes", "free_racks"], key: ["station_id", "wslot", "ts"], maxLimit: 5000, defLimit: 5000 },
  agg: {
    cols: ["station_id", "wslot", "n", "n_avail", "sum_bikes", "n_ebike_avail", "sum_ebikes", "first_ts", "last_ts"],
    key: ["station_id", "wslot"],
    maxLimit: 5000,
    defLimit: 5000,
  },
  // Ein Snapshot ≈ 2 KB ⇒ 288 Zeilen = 1 Tag ≈ 0.6 MB
  snap: { cols: ["ts", "n_stations", "total_bikes", "total_ebikes", "free_floating", "booked", "data"], key: ["ts"], maxLimit: 1000, defLimit: 288 },
  stations: { cols: ["uid", "number", "name", "lat", "lng", "place_type", "first_seen", "last_seen"], key: ["uid"], maxLimit: 5000, defLimit: 5000 },
};

function csvField(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export async function handleExport(env: Env, url: URL): Promise<Response> {
  const table = url.searchParams.get("table") ?? "";
  const def = TABLES[table];
  if (!def) throw new HttpError(400, `table muss eines von ${Object.keys(TABLES).join(", ")} sein`);
  const limRaw = Number(url.searchParams.get("limit") ?? def.defLimit);
  const limit = Number.isInteger(limRaw) && limRaw > 0 ? Math.min(limRaw, def.maxLimit) : def.defLimit;
  const cursorRaw = url.searchParams.get("cursor");
  let where = "";
  let binds: number[] = [];
  if (cursorRaw) {
    binds = cursorRaw.split(",").map(Number);
    if (binds.length !== def.key.length || binds.some((x) => !Number.isInteger(x))) throw new HttpError(400, "ungültiger cursor");
    const ph = def.key.map((_, i) => `?${i + 1}`).join(", ");
    where = def.key.length === 1 ? `WHERE ${def.key[0]} > ?1` : `WHERE (${def.key.join(", ")}) > (${ph})`;
  }
  const sql = `SELECT ${def.cols.join(", ")} FROM ${table} ${where} ORDER BY ${def.key.join(", ")} LIMIT ${limit}`;
  const { results } = await env.DB.prepare(sql)
    .bind(...binds)
    .all<Record<string, unknown>>();
  const lines = [def.cols.join(",")];
  for (const r of results) lines.push(def.cols.map((c) => csvField(r[c])).join(","));
  const headers = new Headers({
    "content-type": "text/csv; charset=utf-8",
    "content-disposition": `attachment; filename="${table}${cursorRaw ? "-" + cursorRaw.replace(/,/g, "_") : ""}.csv"`,
    "cache-control": "no-store",
    "x-rows": String(results.length),
  });
  if (results.length === limit) {
    const last = results[results.length - 1]!;
    const next = def.key.map((k) => String(last[k])).join(",");
    headers.set("x-next-cursor", next);
    const nu = new URL(url);
    nu.searchParams.set("cursor", next);
    nu.searchParams.delete("token");
    headers.set("link", `<${nu.pathname}${nu.search}>; rel="next"`);
  }
  return new Response(lines.join("\n") + "\n", { headers });
}
