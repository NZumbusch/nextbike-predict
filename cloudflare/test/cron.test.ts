import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/feed-luzern.json";
import { createD1 } from "./d1-shim";
import { maintenanceDue, runCron } from "../src/cron";
import { latestSnap, loadStats, maintenance, writeRun } from "../src/db";
import { parseFeed } from "../src/feed";
import { CITY_ID, MIN_OBS, STATIONS } from "../src/config";
import { predict } from "../src/predict";
import { neighborSlots, weekSlot, weekSlotOf, zurichToUtc } from "../src/time";

const okFetch = (async () => Response.json(fixture)) as unknown as typeof fetch;
const feed = parseFeed(fixture, CITY_ID, 20);
const ids = STATIONS.map((s) => s.id);

describe("runCron", () => {
  it("schreibt obs für eigene Stationen + 1 Snapshot, idempotent", async () => {
    const db = createD1();
    const env = { DB: db };
    const now = Date.UTC(2026, 10, 16, 6, 47, 12); // Mo 07:47 lokal
    const r = await runCron(env, now, okFetch);
    expect(r).toMatchObject({ ok: true, obs: 5, maintenance: true, ts: Math.floor(now / 300000) * 300 });
    // zweiter Lauf im selben 5-Min-Fenster (z. B. Retry): keine Duplikate
    await runCron(env, now + 60_000, okFetch);
    const obs = db.raw.prepare("SELECT * FROM obs ORDER BY station_id").all() as Record<string, number>[];
    expect(obs).toHaveLength(5);
    expect(obs.every((o) => o.wslot === weekSlot(0, 7, 45))).toBe(true);
    const suedpol = obs.find((o) => o.station_id === 70900)!;
    expect(suedpol).toMatchObject({ bikes: 4, ebikes: 4, free_racks: null });
    expect(db.raw.prepare("SELECT count(*) c FROM snap").get()).toEqual({ c: 1 });
    expect((db.raw.prepare("SELECT count(*) c FROM stations").get() as { c: number }).c).toBe(25);
    const snap = await latestSnap(db);
    expect(snap!.byId.get(3024956)).toEqual({ bikes: 1, ebikes: 0, active: true });
    expect(snap!.free_floating).toBe(2);
  });

  it("Feed-Ausfall: nichts schreiben, Fehler merken", async () => {
    vi.useFakeTimers();
    const db = createD1();
    const p = runCron({ DB: db }, Date.UTC(2026, 10, 16, 6, 0), (async () => new Response("x", { status: 502 })) as unknown as typeof fetch);
    await vi.runAllTimersAsync();
    const r = await p;
    vi.useRealTimers();
    expect(r.ok).toBe(false);
    expect(db.raw.prepare("SELECT count(*) c FROM obs").get()).toEqual({ c: 0 });
    expect(db.raw.prepare("SELECT count(*) c FROM snap").get()).toEqual({ c: 0 });
    expect((db.raw.prepare("SELECT v FROM meta WHERE k='last_error'").get() as { v: string }).v).toContain("HTTP 502");
  });

  it("fehlende Station im Feed ⇒ Lücke, kein Müll", async () => {
    const j = JSON.parse(JSON.stringify(fixture));
    j.countries[0].cities[0].places = j.countries[0].cities[0].places.filter((p: { uid: number }) => p.uid !== 70900);
    const db = createD1();
    const r = await runCron({ DB: db }, Date.UTC(2026, 10, 16, 6, 0), (async () => Response.json(j)) as unknown as typeof fetch);
    expect(r.obs).toBe(4);
  });
});

describe("maintenanceDue", () => {
  it("erster Lauf sofort, danach 1× pro Tag ab 03:00 lokal (auch nachgeholt)", () => {
    const at = (d: number, h: number, mi = 0) => zurichToUtc(2026, 11, d, h, mi);
    expect(maintenanceDue(at(16, 1), null)).toBe(true);
    const last = at(16, 3, 5) / 1000;
    expect(maintenanceDue(at(16, 23), last)).toBe(false);
    expect(maintenanceDue(at(17, 2, 55), last)).toBe(false);
    expect(maintenanceDue(at(17, 3, 0), last)).toBe(true);
    expect(maintenanceDue(at(17, 14, 0), last)).toBe(true); // 03:00-Lauf verpasst → nachholen
  });
});

describe("Wartung: Rohdaten → Aggregat", () => {
  it("verdichtet alte obs in agg, Prognose bleibt gleich", async () => {
    const db = createD1();
    // 10 Wochen lang jeden Montag 07:40/07:45/07:50 lokal einen Lauf (über den DST-Wechsel hinweg)
    for (let w = 0; w < 10; w++) {
      const d = new Date(Date.UTC(2026, 8, 7 + 7 * w)); // Mo 07.09.2026 + w Wochen
      for (const mi of [40, 45, 50]) {
        const ms = zurichToUtc(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate(), 7, mi);
        expect(Math.floor(weekSlotOf(ms) / 96)).toBe(0);
        await writeRun(db, Math.floor(ms / 300000) * 300, feed, ids);
      }
    }
    const ws = weekSlot(0, 7, 45);
    const slots = neighborSlots(ws);
    const before = predict((await loadStats(db, ids, slots)).get(3024956), ws, MIN_OBS);
    expect(before.n).toBe(30);
    expect(before.probability).toBe(1);

    const now = Date.UTC(2026, 10, 16, 3, 0) / 1000;
    await maintenance(db, now, 60, 365, feed.stations);
    const nObs = (db.raw.prepare("SELECT count(*) c FROM obs").get() as { c: number }).c;
    const nAgg = (db.raw.prepare("SELECT sum(n) c FROM agg").get() as { c: number }).c;
    expect(nObs).toBeLessThan(150);
    expect(nObs + nAgg).toBe(150);
    const after = predict((await loadStats(db, ids, slots)).get(3024956), ws, MIN_OBS);
    expect(after).toEqual(before);

    // zweite Wartung am selben Datensatz: nichts doppelt zählen
    await maintenance(db, now, 60, 365, null);
    expect((db.raw.prepare("SELECT sum(n) c FROM agg").get() as { c: number }).c).toBe(nAgg);
  });

  it("löscht alte Snapshots nach Aufbewahrungsfrist", async () => {
    const db = createD1();
    const now = Date.UTC(2026, 10, 16) / 1000;
    await writeRun(db, now - 400 * 86400, feed, ids);
    await writeRun(db, now - 10 * 86400, feed, ids);
    await maintenance(db, now, 60, 365, null);
    expect(db.raw.prepare("SELECT count(*) c FROM snap").get()).toEqual({ c: 1 });
  });
});
