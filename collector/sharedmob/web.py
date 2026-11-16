"""Status-Webseite im WLAN: Läuft alles? Wie gut sind die Daten? Wie viel Platz?

  python -m sharedmob.web     (waitress, Port WEB_PORT, Standard 8080)
Optionaler Schutz: WEB_TOKEN ⇒ Bearer-Header, ?token=… (setzt Cookie) oder Cookie.
"""
from __future__ import annotations

import hmac
import os
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, after_this_request, g, jsonify, render_template, request, send_file

from . import charts
from .archive import TZ, local_date
from .config import Config, load_config
from .store import connect
from .usage import usage


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.jinja_env.filters.update(
        dt=lambda ts: datetime.fromtimestamp(ts, TZ).strftime("%d.%m. %H:%M") if ts else "–",
        ago=lambda ts: "–" if not ts else (f"{int(time.time() - ts)} s" if time.time() - ts < 120 else f"{int((time.time() - ts) / 60)} min"),
        mb=lambda b: f"{(b or 0) / 1e6:,.1f} MB".replace(",", "'"),
        num=lambda n: f"{n or 0:,}".replace(",", "'"),
        pct=lambda x: "–" if x is None else f"{x * 100:.1f} %",
    )

    # Schema einmal sicherstellen (neue Tabellen nach einem Update, auch wenn der Sammler noch nicht lief)
    connect(cfg.db_path).close()

    def db():
        if "db" not in g:
            g.db = connect(cfg.db_path, readonly=True)
        return g.db

    @app.teardown_appcontext
    def _close(_e):
        c = g.pop("db", None)
        if c:
            c.close()

    @app.before_request
    def _auth():
        if not cfg.web_token:
            return None
        h = request.headers.get("Authorization", "")
        given = h[7:].strip() if h.lower().startswith("bearer ") else None
        given = given or request.args.get("token") or request.cookies.get("sm_token")
        if not given or not hmac.compare_digest(given.encode(), cfg.web_token.encode()):
            return Response("Nicht autorisiert – ?token=… anhängen", 401)
        if request.args.get("token"):
            @after_this_request
            def _set(resp):
                resp.set_cookie("sm_token", cfg.web_token, max_age=365 * 86400, httponly=True, samesite="Lax")
                return resp
        return None

    def status() -> dict:
        c = db()
        now = int(time.time())
        day = now - 86400
        sources = []
        for s in cfg.sources:
            last = c.execute("SELECT * FROM poll WHERE source = ? ORDER BY ts DESC LIMIT 1", (s.id,)).fetchone()
            agg = c.execute(
                """SELECT count(*) n, sum(ok) ok, sum(stale) stale, avg(ms) ms, sum(bytes_in) bytes, sum(v_changes) vc,
                          sum(s_changes) sc, sum(trips) trips FROM poll WHERE source = ? AND ts >= ?""", (s.id, day)).fetchone()
            err = c.execute("SELECT ts, error FROM poll WHERE source = ? AND ok = 0 ORDER BY ts DESC LIMIT 1", (s.id,)).fetchone()
            sources.append({"id": s.id, "kind": s.kind, "last": dict(last) if last else None, "agg": dict(agg),
                            "err": dict(err) if err else None,
                            "ok_rate": (agg["ok"] or 0) / agg["n"] if agg["n"] else None})
        providers = c.execute(
            """SELECT source, provider, max(id_mode) mode,
                      (SELECT n_vehicles FROM provider_stat q WHERE q.source = p.source AND q.provider = p.provider ORDER BY ts DESC LIMIT 1) n,
                      sum(n_opened) opened, sum(n_new_ids) new_ids, sum(n_polls) polls, max(CASE WHEN n_opened > 0 THEN ts END) last_change
               FROM provider_stat p WHERE ts >= ? GROUP BY source, provider ORDER BY n DESC""", (day,)).fetchall()
        trips = dict(c.execute("SELECT source || '|' || provider, count(*) FROM trip WHERE arr_ts >= ? GROUP BY 1", (day,)).fetchall())
        names = dict(c.execute("SELECT provider, name FROM provider_info").fetchall())
        rows = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("vehicle_spell", "station_spell", "trip", "poll")}
        meta = dict(c.execute("SELECT k, v FROM meta").fetchall())
        up = cfg.data_dir / "upload-status.txt"
        gaps = [dict(r) for r in c.execute("SELECT * FROM gap ORDER BY start_ts DESC LIMIT 200")]
        wd_state = cfg.data_dir / "watchdog-state.json"
        try:
            import json as _json

            alerts = {k: v for k, v in _json.loads(wd_state.read_text("utf-8")).items() if isinstance(v, dict) and v.get("active")}
        except (OSError, ValueError):
            alerts = None
        hb = cfg.data_dir / "heartbeat"
        return {
            "now": now,
            "heartbeat": int(hb.stat().st_mtime) if hb.exists() else None,
            "sources": sources,
            "providers": [{**dict(p), "name": names.get(p["provider"]), "trips": trips.get(f"{p['source']}|{p['provider']}", 0)} for p in providers],
            "rows": rows,
            "storage": usage(cfg, local_date(now)),
            "gaps": gaps,
            "gaps_30d": sum(g["duration_s"] for g in gaps if g["end_ts"] >= now - 30 * 86400),
            "alerts": alerts,
            "archived_until": meta.get("archived_until"),
            "last_maintenance": int(meta["last_maintenance"]) if meta.get("last_maintenance") else None,
            "maint_error": meta.get("maint_error"),
            "upload": up.read_text("utf-8").strip() if up.exists() else None,
            "days": sorted({p.name[5:] for p in (cfg.archive_dir / "trip").glob("date=*")}, reverse=True)[:30],
        }

    @app.get("/")
    def index():
        s = status()
        now = s["now"]
        firsts = dict(db().execute("SELECT source, min(ts) FROM poll GROUP BY source").fetchall())
        s["gap_timeline"] = {src.id: charts.timeline([g for g in s["gaps"] if g["source"] == src.id], now - 14 * 86400, now,
                                                     firsts.get(src.id)) for src in cfg.sources}
        return render_template("status.html", s=s, cfg=cfg)

    @app.get("/provider/<source>/<path:provider>")
    def provider_page(source: str, provider: str):
        c = db()
        now = int(time.time())
        days = min(max(request.args.get("d", 4, type=int), 1), cfg.hot_days)
        t0 = now - days * 86400
        rows = c.execute("SELECT * FROM provider_stat WHERE source = ? AND provider = ? AND ts >= ? ORDER BY ts",
                         (source, provider, t0)).fetchall()
        if not rows and not c.execute("SELECT 1 FROM provider_stat WHERE source = ? AND provider = ? LIMIT 1", (source, provider)).fetchone():
            abort(404)
        n = [(r["ts"], r["n_vehicles"]) for r in rows]
        act = [(r["ts"], (r["n_opened"] or 0) / max(1, r["n_polls"])) for r in rows]
        churn = [(r["ts"], (r["n_new_ids"] or 0) / max(1, r["n_polls"]) / max(1, r["n_vehicles"] or 1)) for r in rows]
        info = c.execute("SELECT * FROM provider_info WHERE provider = ?", (provider,)).fetchone()
        tr = c.execute("SELECT count(*) n, coalesce(sum(kind = 'ride'), 0) rides FROM trip WHERE source = ? AND provider = ? AND arr_ts >= ?",
                       (source, provider, t0)).fetchone()
        modes = sorted({r["id_mode"] for r in rows if r["id_mode"]})
        return render_template(
            "provider.html", cfg=cfg, source=source, provider=provider, info=info, days=days, modes=modes, trips=tr,
            last=rows[-1] if rows else None,
            ch_n=charts.line_chart(n, t0, now, "Fahrzeuge im Feed"),
            ch_act=charts.line_chart(act, t0, now, "Zustandswechsel pro Abruf (Aktivität)", bars=True),
            ch_churn=charts.line_chart(churn, t0, now, "Anteil neuer IDs pro Abruf", pct=True, ymax=1.0),
        )

    @app.get("/api/status")
    def api_status():
        return jsonify(status())

    @app.get("/healthz")
    def healthz():
        hb = cfg.data_dir / "heartbeat"
        age = time.time() - hb.stat().st_mtime if hb.exists() else None
        ok = age is not None and age < 600
        return jsonify({"collector_ok": ok, "heartbeat_age_s": None if age is None else round(age)}), 200 if ok else 503

    @app.get("/files/<path:rel>")
    def files(rel: str):
        base = cfg.data_dir.resolve()
        p = (base / rel).resolve()
        if not p.is_file() or p.parts[len(base.parts)] not in ("archive", "raw", "backups") or base not in p.parents:
            abort(404)
        return send_file(p, as_attachment=True, download_name=p.name if "date=" not in rel else rel.replace("/", "_"))

    return app


def main() -> None:
    from waitress import serve

    port = int(os.environ.get("WEB_PORT", "8080"))
    print(f"Status-Seite auf http://0.0.0.0:{port}")
    serve(create_app(), host="0.0.0.0", port=port, threads=4)


if __name__ == "__main__":
    main()
