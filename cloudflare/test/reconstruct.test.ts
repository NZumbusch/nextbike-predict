import { describe, expect, it } from "vitest";
import { createD1 } from "./d1-shim";
import { runCron } from "../src/cron";
import { handleFetch } from "../src/index";
import { zurichToUtc } from "../src/time";
// @ts-ignore – reines JS-Modul
import { applyRow, newState, parseCsvLine } from "../scripts/reconstruct.mjs";
import { clone, fetchOf, moveBike, okFetch } from "./helpers";

describe("parseCsvLine", () => {
  it("Felder mit Komma, Quotes und JSON", () => {
    expect(parseCsvLine('1,,K,"[[1,2]]","[[""a"",0,1]]"')).toEqual(["1", "", "K", "[[1,2]]", '[["a",0,1]]']);
    expect(parseCsvLine('5,"Bundesplatz / Moosstrasse, Luzern",x,')).toEqual(["5", "Bundesplatz / Moosstrasse, Luzern", "x", ""]);
  });
});

describe("Rekonstruktion End-to-End (Cron → /export → Fahrten)", () => {
  it("findet Ausleihe+Rückgabe, direkte Verschiebung und Fahrten über einen Keyframe hinweg", async () => {
    const db = createD1();
    const env = { DB: db, API_TOKEN: "t" };
    const T = zurichToUtc(2026, 11, 16, 7, 0);
    const m = (min: number) => T + min * 60_000;
    await runCron(env, m(0), okFetch); // Keyframe
    await runCron(env, m(1), fetchOf(moveBike(clone(), "401197", null))); // Ausleihe an Neustadtstrasse
    await runCron(env, m(2), fetchOf(moveBike(clone(), "401197", null)));
    await runCron(env, m(9), fetchOf(moveBike(clone(), "401197", 70900))); // Rückgabe Südpol nach 8 Min
    // Umverteilung: 3 Velos gleichzeitig direkt an die Messe
    let j = moveBike(clone(), "401197", 70900);
    for (const b of ["400608", "400293", "401084"]) j = moveBike(j, b, 3024979);
    await runCron(env, m(10), fetchOf(j));
    // Ausleihe kurz vor dem Keyframe, Rückgabe genau in der Keyframe-Minute
    const j2 = moveBike(JSON.parse(JSON.stringify(j)), "400767", null);
    await runCron(env, m(359), fetchOf(j2));
    await runCron(env, m(360), fetchOf(moveBike(JSON.parse(JSON.stringify(j)), "400767", 3024956))); // Keyframe-Minute

    const csv = await (await handleFetch(new Request("https://w/export?table=log", { headers: { authorization: "Bearer t" } }), env as never)).text();
    const rows = csv.trim().split("\n").slice(1).map((l) => {
      const f = parseCsvLine(l);
      return { ts: +f[0]!, prev_ts: f[1] ? +f[1] : null, kind: f[2], st: JSON.parse(f[5]!), bk: JSON.parse(f[6]!) };
    });
    expect(rows.map((r) => r.kind)).toEqual(["K", "D", "D", "D", "D", "K"]);

    const coords = new Map<number, [number, number]>([[3024956, [47.044187, 8.308512]], [70900, [47.033674, 8.297328]]]);
    const s = newState();
    const trips: unknown[][] = [];
    const events: unknown[][] = [];
    for (const r of rows) {
      const o = applyRow(s, r, coords, new Map([["401197", "150"]]));
      trips.push(...o.trips);
      events.push(...o.events);
    }
    // bike,type,start_ts,start_uid,…,end_ts,end_uid,…,duration_min,distance_m,direct,arrivals_same_minute
    const t1 = trips.find((t) => t[0] === "401197")!;
    expect(t1.slice(0, 4)).toEqual(["401197", "150", m(1) / 1000, 3024956]);
    expect(t1[6]).toBe(m(9) / 1000);
    expect(t1[7]).toBe(70900);
    expect(t1[10]).toBe(8);
    expect(t1[11]).toBeGreaterThan(1000); // ≈ 1.4 km Luftlinie
    expect(t1[12]).toBe(0);

    const rebal = trips.filter((t) => t[7] === 3024979);
    expect(rebal).toHaveLength(3);
    expect(rebal.every((t) => t[12] === 1 && t[13] === 3)).toBe(true);

    const kf = trips.find((t) => t[0] === "400767")!;
    expect(kf[3]).toBe(70900);
    expect(kf[7]).toBe(3024956);
    expect(kf[10]).toBe(1);
    // erster Keyframe erzeugt keine Ereignisse
    expect(events.every((e) => e[0] !== m(0) / 1000)).toBe(true);
  });
});
