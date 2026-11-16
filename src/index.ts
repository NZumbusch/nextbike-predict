/**
 * nextbike-predict – Cloudflare Worker
 *   scheduled: alle 5 Min Feed → D1 (nicht über HTTP erreichbar)
 *   fetch:     /, /now, /predict, /plan, /export – alle mit Token
 */
import { handleNow, handlePlan, handlePredict, HttpError, json, statusData } from "./api";
import { isAuthorized } from "./auth";
import { cached } from "./cache";
import { CACHE_TTL_S } from "./config";
import { runCron } from "./cron";
import type { Env } from "./db";
import { handleExport } from "./export";
import { renderStatus } from "./html";

const unauthorized = () =>
  new Response(JSON.stringify({ error: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json", "www-authenticate": 'Bearer realm="nextbike-predict"' },
  });

export async function handleFetch(req: Request, env: Env, ctx?: ExecutionContext): Promise<Response> {
  const url = new URL(req.url);
  // 1) Auth zuerst – vor jedem D1-Zugriff. ?token= nur für die HTML-Seite und deren Download-Links.
  const allowQuery = url.pathname === "/" || url.pathname === "/export";
  if (!(await isAuthorized(req, url, env.API_TOKEN, allowQuery))) return unauthorized();

  if (req.method !== "GET" && req.method !== "HEAD") return json({ error: "method not allowed" }, 405);
  const now = Date.now();
  try {
    switch (url.pathname) {
      case "/":
        return await cached(url, CACHE_TTL_S, ctx, async () =>
          new Response(renderStatus(await statusData(env, now)), {
            headers: { "content-type": "text/html; charset=utf-8", "referrer-policy": "no-referrer", "x-robots-tag": "noindex" },
          }),
        );
      case "/now":
        return await cached(url, CACHE_TTL_S, ctx, () => handleNow(env, now));
      case "/predict":
        return await cached(url, CACHE_TTL_S, ctx, () => handlePredict(env, url, now));
      case "/plan":
        return await cached(url, CACHE_TTL_S, ctx, () => handlePlan(env, url, now));
      case "/export":
        return await handleExport(env, url);
      default:
        return json({ error: "not found", endpoints: ["/", "/now", "/predict", "/plan", "/export"] }, 404);
    }
  } catch (e) {
    if (e instanceof HttpError) return json({ error: e.message }, e.status);
    console.error("HTTP-Fehler", e);
    return json({ error: "internal error" }, 500);
  }
}

export default {
  fetch: (req, env, ctx) => handleFetch(req, env, ctx),
  async scheduled(_event, env, _ctx) {
    const r = await runCron(env, Date.now());
    if (!r.ok) console.error("Cron-Lauf fehlgeschlagen", r);
  },
} satisfies ExportedHandler<Env>;
