import { describe, expect, it, vi } from "vitest";
import fixture from "./fixtures/feed-luzern.json";
import { fetchFeed, FeedError, parseFeed } from "../src/feed";
import { CITY_ID, STATIONS } from "../src/config";

const clone = () => JSON.parse(JSON.stringify(fixture));

describe("parseFeed (echter Feed-Ausschnitt Luzern)", () => {
  const f = parseFeed(fixture, CITY_ID, 20);

  it("findet alle konfigurierten Stationen", () => {
    for (const s of STATIONS) {
      expect(f.stations.find((x) => x.uid === s.id), s.name).toBeDefined();
    }
  });

  it("zählt frei abgestellte Velos separat", () => {
    expect(f.free_floating).toBe(2);
    expect(f.stations.every((s) => !s.name.startsWith("BIKE "))).toBe(true);
  });

  it("trennt E-Bikes über bike_types (183/371)", () => {
    const suedpol = f.stations.find((s) => s.uid === 70900)!;
    expect(suedpol.name).toBe("Südpol / Arsenalstrasse 28");
    expect(suedpol.bikes).toBe(4);
    expect(suedpol.ebikes).toBe(4); // bike_types {"183": 4}
    const home = f.stations.find((s) => s.uid === 3024956)!;
    expect(home.ebikes).toBe(0); // bike_types {"150": 1}
  });

  it("free_racks = null bei virtuellen Stationen (bike_racks = 0)", () => {
    for (const s of STATIONS) expect(f.stations.find((x) => x.uid === s.id)!.free_racks).toBeNull();
  });

  it("free_racks bei echten Ständern", () => {
    const j = clone();
    const p = j.countries[0].cities[0].places[0];
    p.bike_racks = 10;
    p.free_racks = 3;
    expect(parseFeed(j, CITY_ID, 20).stations[0]!.free_racks).toBe(3);
  });

  it("verwirft unplausible Werte statt Müll zu speichern", () => {
    const j = clone();
    const p = j.countries[0].cities[0].places[0];
    p.bikes_available_to_rent = -3;
    p.bikes = "viele";
    const r = parseFeed(j, CITY_ID, 20);
    expect(r.stations.find((s) => s.uid === p.uid)).toBeUndefined();
  });

  it("E-Bikes nie mehr als ausleihbare Velos", () => {
    const j = clone();
    const p = j.countries[0].cities[0].places.find((x: { uid: number }) => x.uid === 70900);
    p.bikes_available_to_rent = 2; // 2 von 4 reserviert
    expect(parseFeed(j, CITY_ID, 20).stations.find((s) => s.uid === 70900)!.ebikes).toBe(2);
  });

  it("markiert Wartung als inaktiv", () => {
    const j = clone();
    j.countries[0].cities[0].places[0].maintenance = true;
    expect(parseFeed(j, CITY_ID, 20).stations[0]!.active).toBe(false);
  });

  it("wirft bei kaputtem / falschem Feed", () => {
    expect(() => parseFeed({}, CITY_ID, 20)).toThrow(FeedError);
    expect(() => parseFeed(null, CITY_ID, 20)).toThrow(FeedError);
    expect(() => parseFeed(fixture, 999, 20)).toThrow(/city 999/);
    expect(() => parseFeed(fixture, CITY_ID, 100)).toThrow(/gestört/);
    const j = clone();
    j.countries[0].cities[0].places = [];
    expect(() => parseFeed(j, CITY_ID, 20)).toThrow(FeedError);
  });
});

describe("fetchFeed", () => {
  it("Retry nach Fehler, dann Erfolg", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response("down", { status: 503 }))
      .mockResolvedValueOnce(Response.json(fixture));
    const p = fetchFeed("https://x", CITY_ID, 20, fetcher as unknown as typeof fetch);
    await vi.runAllTimersAsync();
    const r = await p;
    vi.useRealTimers();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(r.stations.length).toBe(25);
  });

  it("wirft FeedError bei HTML statt JSON", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockImplementation(async () => new Response("<html>301</html>"));
    const p = fetchFeed("https://x", CITY_ID, 20, fetcher as unknown as typeof fetch);
    const assertion = expect(p).rejects.toThrow(FeedError);
    await vi.runAllTimersAsync();
    await assertion;
    vi.useRealTimers();
  });
});
