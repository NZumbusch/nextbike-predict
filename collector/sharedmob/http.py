"""HTTP-Abruf: gzip, Timeout, Retry, Messung. Nur Standardbibliothek (urllib folgt Redirects,
respektiert HTTPS_PROXY)."""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass

USER_AGENT = "sharedmob-collector/1.0 (+private research data collection)"


class FetchError(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    body: bytes          # dekomprimiert
    wire_bytes: int      # übertragen (komprimiert)
    ms: int

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError as e:
            raise FetchError(f"{self.url}: Antwort ist kein JSON") from e


def fetch(url: str, headers: dict[str, str] | None = None, timeout: float = 30.0, retries: int = 2,
          backoff: float = 3.0, opener=urllib.request.urlopen, sleep=time.sleep) -> Response:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip, deflate"}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with opener(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower() if getattr(r, "headers", None) else ""
                status = getattr(r, "status", 200)
            body = gzip.decompress(raw) if enc == "gzip" else zlib.decompress(raw) if enc == "deflate" else raw
            if status != 200:
                raise FetchError(f"{url}: HTTP {status}")
            return Response(url, status, body, len(raw), int((time.monotonic() - t0) * 1000))
        except urllib.error.HTTPError as e:
            last = FetchError(f"{url}: HTTP {e.code}")
            if e.code in (400, 401, 403, 404):
                break  # nicht wiederholen
        except Exception as e:  # noqa: BLE001 – Netzwerkfehler aller Art
            last = e if isinstance(e, FetchError) else FetchError(f"{url}: {type(e).__name__}: {e}")
        if attempt < retries:
            sleep(backoff * (attempt + 1))
    raise last  # type: ignore[misc]
