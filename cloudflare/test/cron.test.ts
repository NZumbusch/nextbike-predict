import { describe, expect, it, vi } from "vitest";
import { createD1 } from "./d1-shim";
import { intervalMin, maintenanceDue, pollDue, runCron } from "../src/cron";
import { loadStats, maintenance, readCurrent, readState } from "../src/db";
import { MIN_OBS } from "../src/config";
import { predict } from "../src/predict";
import { neighborSlots, weekSlot, weekSlotOf, zurichToUtc } from "../src/time";
import { clone, fetchOf, ids, moveBike, okFetch, places, seedObs } from "./helpers";

const count = (db: ReturnType<typeof createD1>, t: string) => (db.raw.prepare(`SELECT count(*) c FROM ${t}`).get() as { c: number }).c;
// Mo 16.11.2026 07:47:12 Zürich
const T0 = zurichToUtc(2026, 11, 16, 7, 47) + 12_000;

describe("Takt", () => {
  it("tagsüber 1 Min, nachts (01–05) 5 Min", () => {
    expect(intervalMin(zurichToUtc(2026, 11, 16, 7, 0))).toBe(1);
    expect(intervalMin(zurichToUtc(2026, 11, 16, 0, 59))).toBe(1);
    expect(intervalMin(zurichToUtc(2026, 11, 16, 1, 0))).toBe(5);
    expect(intervalMin(zurichToUtc(2026, 11, 16, 4, 59))).toBe(5);
    expect(intervalMin(zurichToUtc(2026, 6, 16, 3, 0))).toBe(5); // Sommerzeit
  });
  it("pollDue mit Toleranz für Cron-Jitter", () => {
    const night = zurichToUtc(2026, 11, 16, 2, 0);
    expect(pollDue(night, null)).toBe(true);
    expect(pollDue(night, night / 1000 - 60)).toBe(false);
    expect(pollDue(night, night / 1000 - 285)).toBe(true); // 4:45 nach letztem Abruf, Cron leicht früh
    expect(pollDue(T0, T0 / 1000 - 50)).toBe(true);
    expect(pollDue(T0, T0 / 1000 - 30)).toBe(false); // Doppelauslösung
  });
});

describe("runCron v2", () => {
  it("erster Lauf: Keyframe + obs + Zustand + Stammdaten", async () => {
    const db = createD1();
    const r = await runCron({ DB: db }, T0, okFetch);
    expect(r).toMatchObject({ ok: true, log: "K", obs: 5, maintenance: true });
    expect(count(db, "log")).toBe(1);
    expect(count(db, "obs")).toBe(5);
    expect(count(db, "stations")).toBe(25);
    expect(count(db, "bikes")).toBeGreaterThan(50);
    const s = (await readState(db))!;
    expect(s.ts).toBe(Math.floor(T0 / 60000) * 60);
    expect(s.last_maint).toBe(s.ts);
    expect(s.db_bytes).toBeGreaterThan(0);
    const c = (await readCurrent(db))!;
    expect(c.byId.get(70900)).toEqual({ bikes: 4, ebikes: 4, active: true });
    expect(c.free_floating).toBe(2);
    const obs = db.raw.prepare("SELECT * FROM obs").all() as Record<string, number>[];
    expect(obs.every((o) => o.wslot === weekSlot(0, 7, 45) && o.ts % 300 === 0)).toBe(true);
  });

  it("Folgelauf ohne Änderung: keine Log-Zeile; mit Änderung: Delta mit Bewegung", async () => {
    const db = createD1();
    await runCron({ DB: db }, T0, okFetch);
    const r1 = await runCron({ DB: db }, T0 + 60_000, okFetch);
    expect(r1).toMatchObject({ ok: true, log: null, obs: 0 });
    expect(count(db, "log")).toBe(1);

    const r2 = await runCron({ DB: db }, T0 + 120_000, fetchOf(moveBike(clone(), "401197", null)));
    expect(r2).toMatchObject({ log: "D", moves: 1 });
    const row = db.raw.prepare("SELECT * FROM log WHERE kind='D'").get() as Record<string, unknown>;
    expect(JSON.parse(row.bk as string)).toEqual([["401197", 3024956, 0]]);
    expect(row.prev_ts).toBe(Math.floor((T0 + 60_000) / 60000) * 60);

    // 5-Min-Grenze überschritten (07:47 → 07:50) ⇒ neue obs
    const r3 = await runCron({ DB: db }, T0 + 180_000, okFetch);
    expect(r3.obs).toBe(5);
    expect(count(db, "obs")).toBe(10);
  });

  it("zu früher Doppelaufruf wird übersprungen (kein Feed-Abruf)", async () => {
    const db = createD1();
    await runCron({ DB: db }, T0, okFetch);
    const f = vi.fn(okFetch);
    const r = await runCron({ DB: db }, T0 + 10_000, f as unknown as typeof fetch);
    expect(r.skipped).toBe(true);
    expect(f).not.toHaveBeenCalled();
  });

  it("Keyframe alle 6 h", async () => {
    const db = createD1();
    await runCron({ DB: db }, T0, okFetch);
    expect((await runCron({ DB: db }, T0 + 5.9 * 3600_000, okFetch)).log).toBe(null);
    expect((await runCron({ DB: db }, T0 + 6 * 3600_000, okFetch)).log).toBe("K");
  });

  it("Feed-Ausfall: nichts schreiben, Zustand bleibt, Fehler gemerkt; danach Delta über die Lücke", async () => {
    vi.useFakeTimers();
    const db = createD1();
    await runCron({ DB: db }, T0, okFetch);
    const p = runCron({ DB: db }, T0 + 60_000, (async () => new Response("x", { status: 502 })) as unknown as typeof fetch);
    await vi.runAllTimersAsync();
    const r = await p;
    vi.useRealTimers();
    expect(r.ok).toBe(false);
    expect(count(db, "log")).toBe(1);
    expect((db.raw.prepare("SELECT v FROM meta WHERE k='last_error'").get() as { v: string }).v).toContain("HTTP 502");
    const r2 = await runCron({ DB: db }, T0 + 180_000, fetchOf(moveBike(clone(), "401197", 70900)));
    expect(r2.log).toBe("D");
    const row = db.raw.prepare("SELECT prev_ts, ts FROM log WHERE kind='D'").get() as { prev_ts: number; ts: number };
    expect(row.ts - row.prev_ts).toBe(180); // Lücke sichtbar
  });

  it("fehlende Station im Feed ⇒ keine obs-Zeile dafür", async () => {
    const j = clone();
    j.countries[0].cities[0].places = places(j).filter((p) => p.uid !== 70900);
    const db = createD1();
    expect((await runCron({ DB: db }, T0, fetchOf(j))).obs).toBe(4);
  });

  it("Wartungslauf nutzt Vollabruf (mit Velo-Liste), sonst schlanken Feed", async () => {
    const db = createD1();
    const urls: string[] = [];
    const f = (async (u: string) => (urls.push(String(u)), Response.json(clone()))) as unknown as typeof fetch;
    await runCron({ DB: db }, T0, f);
    await runCron({ DB: db }, T0 + 60_000, f);
    expect(urls[0]).not.toContain("bikes=false");
    expect(urls[1]).toContain("bikes=false");
  });
});

describe("maintenanceDue", () => {
  it("erster Lauf sofort, danach 1× pro Tag ab 03:00 lokal (auch nachgeholt)", () => {
    const at = (d: number, h: number, mi = 0) => zurichToUtc(2026, 11, d, h, mi);
    expect(maintenanceDue(at(16, 1), null)).toBe(true);
    expect(maintenanceDue(at(16, 1), 0)).toBe(true);
    const last = at(16, 3, 5) / 1000;
    expect(maintenanceDue(at(16, 23), last)).toBe(false);
    expect(maintenanceDue(at(17, 2, 55), last)).toBe(false);
    expect(maintenanceDue(at(17, 3, 0), last)).toBe(true);
    expect(maintenanceDue(at(17, 14, 0), last)).toBe(true);
  });
});

const opts = { rawDays: 60, logDays: 0, snapDays: 365, dbBytes: 0, dbLimitBytes: 450e6 };

describe("Wartung", () => {
  it("verdichtet alte obs in agg, Prognose bleibt gleich, keine Doppelzählung", async () => {
    const db = createD1();
    for (let w = 0; w < 10; w++) {
      const d = new Date(Date.UTC(2026, 8, 7 + 7 * w)); // Mo 07.09.2026 + w Wochen, über DST hinweg
      for (const mi of [40, 45, 50]) {
        const ms = zurichToUtc(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate(), 7, mi);
        expect(Math.floor(weekSlotOf(ms) / 96)).toBe(0);
        await seedObs(db, Math.floor(ms / 300000) * 300);
      }
    }
    const ws = weekSlot(0, 7, 45);
    const slots = neighborSlots(ws);
    const before = predict((await loadStats(db, ids, slots)).get(3024956), ws, MIN_OBS);
    expect(before.n).toBe(30);

    const now = Date.UTC(2026, 10, 16, 3, 0) / 1000;
    await maintenance(db, now, opts, null);
    const nAgg = (db.raw.prepare("SELECT sum(n) c FROM agg").get() as { c: number }).c;
    expect(count(db, "obs")).toBeLessThan(150);
    expect(count(db, "obs") + nAgg).toBe(150);
    expect(predict((await loadStats(db, ids, slots)).get(3024956), ws, MIN_OBS)).toEqual(before);
    await maintenance(db, now, opts, null);
    expect((db.raw.prepare("SELECT sum(n) c FROM agg").get() as { c: number }).c).toBe(nAgg);
  });

  it("Log-Aufbewahrung und Grössen-Schutz", async () => {
    const db = createD1();
    const now = Date.UTC(2026, 10, 16) / 1000;
    const ins = db.raw.prepare("INSERT INTO log (ts, prev_ts, kind, n_st, n_bk, st, bk) VALUES (?, NULL, 'D', 0, 0, '[]', '[]')");
    for (const days of [400, 380, 100, 10]) ins.run(now - days * 86400);
    await maintenance(db, now, { ...opts, logDays: 0 }, null);
    expect(count(db, "log")).toBe(4); // 0 = für immer
    await maintenance(db, now, { ...opts, dbBytes: 460e6 }, null);
    expect(count(db, "log")).toBe(2); // älteste 30 Tage (400 + 380) weg
    await maintenance(db, now, { ...opts, logDays: 30 }, null);
    expect(count(db, "log")).toBe(1);
  });
});
