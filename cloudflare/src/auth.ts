/**
 * Bearer-Token-Prüfung in konstanter Zeit. Beide Seiten werden zuerst mit SHA-256
 * gehasht ⇒ gleiche Länge, Vergleich ohne frühen Abbruch, Token-Länge leakt nicht.
 */
const enc = new TextEncoder();

async function sha256(s: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

export async function constantTimeEqual(a: string, b: string): Promise<boolean> {
  const [ha, hb] = await Promise.all([sha256(a), sha256(b)]);
  let diff = 0;
  for (let i = 0; i < ha.length; i++) diff |= ha[i]! ^ hb[i]!;
  return diff === 0;
}

/**
 * Token aus `Authorization: Bearer …` oder – nur wenn allowQuery – aus `?token=`.
 * Fehlt das Secret API_TOKEN, wird immer abgelehnt.
 */
export async function isAuthorized(req: Request, url: URL, secret: string | undefined, allowQuery: boolean): Promise<boolean> {
  if (!secret) return false;
  let given: string | null = null;
  const h = req.headers.get("authorization");
  if (h && /^bearer\s+/i.test(h)) given = h.replace(/^bearer\s+/i, "").trim();
  else if (allowQuery) given = url.searchParams.get("token");
  if (!given) return false;
  return constantTimeEqual(given, secret);
}
