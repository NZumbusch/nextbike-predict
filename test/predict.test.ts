import { describe, expect, it } from "vitest";
import { addStat, predict, recommend, type StatsByStation, type SlotStat } from "../src/predict";
import { weekSlot } from "../src/time";
import type { StationConfig } from "../src/config";

const st = (n: number, n_avail: number, sum_bikes: number, n_e_avail = 0, sum_e = 0): SlotStat => ({ n, n_avail, sum_bikes, n_e_avail, sum_e });

describe("predict", () => {
  const ws = weekSlot(0, 7, 45);

  it("glättet über ±1 Slot und rechnet Anteil/Durchschnitt", () => {
    const stats: StatsByStation = new Map();
    addStat(stats, 1, ws - 1, st(3, 3, 9, 1, 1));
    addStat(stats, 1, ws, st(3, 2, 4, 0, 0));
    addStat(stats, 1, ws + 1, st(4, 1, 1, 1, 2));
    addStat(stats, 1, ws + 2, st(100, 0, 0)); // ausserhalb ±1 → ignoriert
    const p = predict(stats.get(1), ws, 5);
    expect(p.status).toBe("ok");
    expect(p.n).toBe(10);
    expect(p.probability).toBe(0.6);
    expect(p.avg_bikes).toBe(1.4);
    expect(p.probability_ebike).toBe(0.2);
    expect(p.avg_ebikes).toBe(0.3);
    expect(p.slot).toBe("Mo 07:45");
    expect(p.slots_used).toEqual(["Mo 07:30", "Mo 07:45", "Mo 08:00"]);
  });

  it("addiert Roh- und Aggregatdaten im selben Slot", () => {
    const stats: StatsByStation = new Map();
    addStat(stats, 1, ws, st(2, 2, 4)); // obs
    addStat(stats, 1, ws, st(8, 0, 0)); // agg
    const p = predict(stats.get(1), ws, 5);
    expect(p.n).toBe(10);
    expect(p.probability).toBe(0.2);
  });

  it("meldet 'zu wenig Daten' statt zu raten", () => {
    const stats: StatsByStation = new Map();
    addStat(stats, 1, ws, st(4, 4, 8));
    const p = predict(stats.get(1), ws, 5);
    expect(p.status).toBe("zu_wenig_daten");
    expect(p.probability).toBeNull();
    expect(p.n).toBe(4);
    expect(predict(undefined, ws, 5).n).toBe(0);
  });

  it("Glättung über den Wochenumbruch (So 23:45 ↔ Mo 00:00)", () => {
    const stats: StatsByStation = new Map();
    addStat(stats, 1, 671, st(5, 5, 5));
    const p = predict(stats.get(1), 0, 5);
    expect(p.n).toBe(5);
    expect(p.slots_used).toEqual(["So 23:45", "Mo 00:00", "Mo 00:15"]);
  });
});

describe("recommend", () => {
  const home: StationConfig = { id: 1, alias: "home", name: "Home", group: "start", primary: true };
  const fb1: StationConfig = { id: 2, alias: "fb1", name: "FB1", group: "start", primary: false, distance_m: 150 };
  const fb2: StationConfig = { id: 3, alias: "fb2", name: "FB2", group: "start", primary: false, distance_m: 300 };
  const ok = (p: number) => ({ status: "ok" as const, slot: "", slots_used: [], n: 20, probability: p, avg_bikes: 1, probability_ebike: 0, avg_ebikes: 0 });
  const none = { status: "zu_wenig_daten" as const, slot: "", slots_used: [], n: 1, probability: null, avg_bikes: null, probability_ebike: null, avg_ebikes: null };

  it("Stammstation, wenn ≥ Schwelle", () => {
    const r = recommend([{ station: home, prediction: ok(0.85) }, { station: fb1, prediction: ok(0.99) }], 0.8);
    expect(r.alias).toBe("home");
  });
  it("beste Ausweichstation, wenn Stamm unsicher", () => {
    const r = recommend(
      [{ station: home, prediction: ok(0.4) }, { station: fb1, prediction: ok(0.7) }, { station: fb2, prediction: ok(0.9) }],
      0.8,
    );
    expect(r.alias).toBe("fb2");
    expect(r.text).toContain("300 m");
  });
  it("bleibt bei Stamm, wenn keine Ausweichstation besser", () => {
    const r = recommend([{ station: home, prediction: ok(0.5) }, { station: fb1, prediction: ok(0.3) }], 0.8);
    expect(r.alias).toBe("home");
  });
  it("Stamm ohne Daten → Ausweich mit Daten", () => {
    const r = recommend([{ station: home, prediction: none }, { station: fb1, prediction: ok(0.6) }], 0.8);
    expect(r.alias).toBe("fb1");
    expect(r.text).toContain("zu wenig Daten");
  });
  it("gar keine Daten", () => {
    const r = recommend([{ station: home, prediction: none }], 0.8);
    expect(r.station_id).toBeNull();
  });
});
