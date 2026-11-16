/**
 * Kurzzeit-Cache für Antworten: zuerst Isolate-Speicher (kostenlos, wirkt auch auf
 * *.workers.dev), dann Cache API (wirkt nur auf eigener Domain/Zone; auf workers.dev No-Op).
 * Der Cache-Key enthält nie den Token.
 */
interface MemEntry {
  exp: number;
  body: string;
  headers: [string, string][];
  status: number;
}

const mem = new Map<string, MemEntry>();
const MAX_MEM = 64;

export function cacheKey(url: URL): string {
  const p = new URLSearchParams(url.searchParams);
  p.delete("token");
  p.sort();
  const q = p.toString();
  return `https://cache.nextbike-predict.internal${url.pathname}${q ? "?" + q : ""}`;
}

export async function cached(
  url: URL,
  ttlS: number,
  ctx: ExecutionContext | undefined,
  produce: () => Promise<Response>,
  nowMs = Date.now(),
): Promise<Response> {
  const key = cacheKey(url);
  const hit = mem.get(key);
  if (hit && hit.exp > nowMs) return withHeader(new Response(hit.body, { status: hit.status, headers: hit.headers }), "x-cache", "mem");

  const cache = typeof caches !== "undefined" ? (caches as unknown as { default?: Cache }).default : undefined;
  if (cache) {
    try {
      const c = await cache.match(key);
      if (c) return withHeader(new Response(c.body, c), "x-cache", "edge");
    } catch {
      /* Cache API nicht verfügbar */
    }
  }

  const res = await produce();
  if (res.status !== 200) return res;
  const body = await res.text();
  const headers: [string, string][] = [...res.headers.entries()];
  if (mem.size >= MAX_MEM) mem.delete(mem.keys().next().value!);
  mem.set(key, { exp: nowMs + ttlS * 1000, body, headers, status: 200 });
  if (cache) {
    const toStore = new Response(body, { status: 200, headers });
    toStore.headers.set("cache-control", `public, max-age=${ttlS}`);
    const p = cache.put(key, toStore).catch(() => {});
    if (ctx) ctx.waitUntil(p);
  }
  return withHeader(new Response(body, { status: 200, headers }), "x-cache", "miss");
}

function withHeader(r: Response, k: string, v: string): Response {
  const out = new Response(r.body, r);
  out.headers.set(k, v);
  out.headers.set("cache-control", "private, max-age=60");
  return out;
}

/** nur für Tests */
export function _clearMemCache() {
  mem.clear();
}
