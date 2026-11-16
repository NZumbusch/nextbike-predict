import { beforeEach, describe, expect, it, vi } from "vitest";
import { createD1 } from "./d1-shim";
import { handleFetch } from "../src/index";
import { runCron } from "../src/cron";
import { _clearMemCache } from "../src/cache";
import { okFetch, seedObs } from "./helpers";
import { constantTimeEqual } from "../src/auth";
import { zurichToUtc } from "../src/time";

const TOKEN = "s3cret-token";

/** DB, die bei jedem Zugriff fehlschlägt – beweist "401 vor D1". */
const explodingDb = new Proxy({}, { get: () => { throw new Error("D1 darf nicht angefasst werden"); } }) as D1Database;

const get = (path: string, env: object, token?: string | null) =>
  handleFetch(new Request(`https://w.example${path}`, { headers: token ? { authorization: `Bearer ${token}` } : {} }), env as never);

async function seededDb() {
  const db = createD1();
  // 6 Montage, 07:30–08:15 lokal alle 5 Min
  for (let w = 0; w < 6; w++) {
    const d = new Date(Date.UTC(2026, 9, 5 + 7 * w));
    for (let mi = 30; mi <= 75; mi += 5) {
      const ms = zurichToUtc(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate(), 7, 0) + mi * 60_000;
      await seedObs(db, Math.floor(ms / 300000) * 300);
    }
  }
  return db;
}

beforeEach(() => _clearMemCache());

describe("Auth", () => {
  it("401 ohne/mit falschem Token, ohne D1 zu lesen", async () => {
    const env = { DB: explodingDb, API_TOKEN: TOKEN };
    for (const p of ["/", "/now", "/predict?station=home", "/plan?at=07:45", "/export?table=obs", "/__scheduled", "/x"]) {
      expect((await get(p, env)).status, p).toBe(401);
      expect((await get(p, env, "falsch")).status, p).toBe(401);
    }
  });
  it("?token= nur für HTML-Seite und Export-Downloads", async () => {
    const env = { DB: explodingDb, API_TOKEN: TOKEN };
    for (const p of ["/now", "/predict?station=home", "/plan"]) expect((await get(`${p}${p.includes("?") ? "&" : "?"}token=${TOKEN}`, env)).status, p).toBe(401);
    expect((await get(`/export?table=obs&token=falsch`, env)).status).toBe(401);
    const db = createD1();
    expect((await get(`/export?table=obs&token=${TOKEN}`, { DB: db, API_TOKEN: TOKEN })).status).toBe(200);
  });
  it("ohne gesetztes Secret wird alles abgelehnt", async () => {
    expect((await get("/now", { DB: explodingDb }, "")).status).toBe(401);
    expect((await get("/now", { DB: explodingDb, API_TOKEN: "" }, "x")).status).toBe(401);
  });
  it("constantTimeEqual", async () => {
    expect(await constantTimeEqual("abc", "abc")).toBe(true);
    expect(await constantTimeEqual("abc", "abd")).toBe(false);
    expect(await constantTimeEqual("abc", "abcd")).toBe(false);
  });
});

describe("Endpunkte", () => {
  it("/now zeigt letzten Snapshot", async () => {
    const db = createD1();
    await runCron({ DB: db }, Date.now(), okFetch);
    const r = await get("/now", { DB: db, API_TOKEN: TOKEN }, TOKEN);
    expect(r.status).toBe(200);
    const j = (await r.json()) as { stale: boolean; network: { free_floating: number }; stations: { alias: string; bikes: number; ebikes: number }[] };
    expect(j.stale).toBe(false);
    expect(j.network.free_floating).toBe(2);
    expect(j.stations.find((s) => s.alias === "suedpol")).toMatchObject({ bikes: 4, ebikes: 4 });
  });

  it("/predict mit Alias, ID, ISO und HH:MM", async () => {
    const env = { DB: await seededDb(), API_TOKEN: TOKEN };
    const r = await get("/predict?station=home&at=2026-11-16T07:45", env, TOKEN); // Montag
    const j = (await r.json()) as Record<string, unknown>;
    expect(j).toMatchObject({ status: "ok", probability: 1, avg_bikes: 1, slot: "Mo 07:45" });
    expect(j.n).toBe(6 * 9); // ±1 Slot = 07:30–08:14 → 9 Läufe pro Montag
    const r2 = await get("/predict?station=70900&at=2026-11-17T07:45", env, TOKEN); // Dienstag: keine Daten
    expect(await r2.json()).toMatchObject({ status: "zu_wenig_daten", probability: null, n: 0 });
    expect((await get("/predict?station=home&at=12:30", env, TOKEN)).status).toBe(200);
    expect((await get("/predict?station=nix", env, TOKEN)).status).toBe(404);
    expect((await get("/predict?station=home&at=quatsch", env, TOKEN)).status).toBe(400);
    expect((await get("/predict", env, TOKEN)).status).toBe(400);
  });

  it("/plan liefert Empfehlung + Optionen", async () => {
    const env = { DB: await seededDb(), API_TOKEN: TOKEN };
    const j = (await (await get("/plan?at=2026-11-16T07:45", env, TOKEN)).json()) as {
      recommendation: { alias: string; text: string };
      options: { station: { alias: string } }[];
    };
    // Fixture: home 1 Velo, Moosstrasse 5, Zentralstrasse 1 → alle 100 % ⇒ Stammstation
    expect(j.recommendation.alias).toBe("home");
    expect(j.options.map((o) => o.station.alias)).toEqual(["home", "moosstrasse", "zentralstrasse"]);
    const z = (await (await get("/plan?at=2026-11-16T07:45&dir=ziel", env, TOKEN)).json()) as { options: unknown[] };
    expect(z.options).toHaveLength(2);
    expect((await get("/plan?dir=xy", env, TOKEN)).status).toBe(400);
  });

  it("/ rendert HTML (Token per Query), ohne Token im HTML", async () => {
    const env = { DB: await seededDb(), API_TOKEN: TOKEN };
    const r = await get(`/?token=${TOKEN}`, env);
    expect(r.status).toBe(200);
    expect(r.headers.get("content-type")).toContain("text/html");
    const html = await r.text();
    expect(html).toContain("Neustadtstrasse 10");
    expect(html).not.toContain(TOKEN);
    expect(html).not.toMatch(/<script src|<link |https?:\/\//);
  });

  it("Cache: zweiter Aufruf liest D1 nicht erneut", async () => {
    const db = await seededDb();
    const spy = vi.spyOn(db, "prepare");
    const env = { DB: db, API_TOKEN: TOKEN };
    const r1 = await get("/now", env, TOKEN);
    const calls = spy.mock.calls.length;
    const r2 = await get("/now", env, TOKEN);
    expect(r1.headers.get("x-cache")).toBe("miss");
    expect(r2.headers.get("x-cache")).toBe("mem");
    expect(spy.mock.calls.length).toBe(calls);
    expect(await r2.text()).toBe(await r1.text());
  });

  it("/export blättert per Cursor durch alle Zeilen", async () => {
    const db = await seededDb();
    const env = { DB: db, API_TOKEN: TOKEN };
    const total = (db.raw.prepare("SELECT count(*) c FROM obs").get() as { c: number }).c;
    let cursor: string | null = null;
    let rows = 0;
    let pages = 0;
    do {
      const r = await get(`/export?table=obs&limit=70${cursor ? "&cursor=" + cursor : ""}`, env, TOKEN);
      expect(r.status).toBe(200);
      const lines = (await r.text()).trim().split("\n");
      expect(lines[0]).toBe("station_id,wslot,ts,bikes,ebikes,free_racks");
      rows += lines.length - 1;
      cursor = r.headers.get("x-next-cursor");
      pages++;
    } while (cursor && pages < 100);
    expect(rows).toBe(total);
    await runCron(env, Date.now(), okFetch);
    const s = await get("/export?table=log", env, TOKEN);
    const txt = await s.text();
    expect(txt.split("\n")[1]).toMatch(/^\d+,,K,25,\d+,"\[\[/);
    // Text-Cursor (Velonummern) blättert korrekt
    let cur: string | null = null, n = 0, guard = 0;
    do {
      const r = await get(`/export?table=bikes&limit=7${cur ? "&cursor=" + cur : ""}`, env, TOKEN);
      n += (await r.text()).trim().split("\n").length - 1;
      cur = r.headers.get("x-next-cursor");
    } while (cur && ++guard < 100);
    expect(n).toBe((db.raw.prepare("SELECT count(*) c FROM bikes").get() as { c: number }).c);
    expect((await get("/export?table=meta", env, TOKEN)).status).toBe(400);
  });
});
