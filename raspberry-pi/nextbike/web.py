"""Web-GUI + JSON-API (im WLAN erreichbar).

Start:  python -m nextbike.web   (waitress, Port WEB_PORT, Standard 8080)
Optionaler Schutz: WEB_TOKEN setzen ⇒ Bearer-Header, ?token=… (setzt Cookie) oder Cookie nötig.
"""
from __future__ import annotations

import csv
import hmac
import io
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from flask import Flask, Response, abort, after_this_request, g, jsonify, redirect, render_template, request, send_file

from . import db, svg
from .backup import list_backups, snapshot_db, upload_status
from .config import Config, load_config
from .predict import predict, recommend
from .timeutil import BadInput, fmt_dt, fmt_hm, iso_local, iso_utc, local, local_midnight, neighbor_slots, parse_at, week_slot


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config["NB"] = cfg
    app.jinja_env.filters.update(
        hm=fmt_hm,
        dt=fmt_dt,
        pct=lambda p: "–" if p is None else f"{round(p * 100)} %",
        mb=lambda b: f"{b / 1e6:.1f} MB",
        hue=lambda p: 0 if p is None else round(p * 120),
        dur=lambda s: f"{s // 60} min" if s < 3 * 3600 else f"{s / 3600:.1f} h",
    )

    # ------------------------------------------------------------ Infrastruktur
    def conn() -> sqlite3.Connection:
        if "db" not in g:
            if not cfg.db_path.exists():
                db.connect(cfg.db_path).close()  # Schema anlegen, falls Sammler noch nie lief
            g.db = db.connect(cfg.db_path, readonly=True)
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    @app.before_request
    def _auth():
        if not cfg.web_token:
            return None
        given = None
        h = request.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            given = h[7:].strip()
        given = given or request.args.get("token") or request.cookies.get("nb_token")
        if not given or not hmac.compare_digest(given.encode(), cfg.web_token.encode()):
            return Response("Nicht autorisiert – ?token=… anhängen oder Authorization: Bearer …", 401, {"WWW-Authenticate": "Bearer"})
        if request.args.get("token"):
            @after_this_request
            def _cookie(resp):
                resp.set_cookie("nb_token", cfg.web_token, max_age=365 * 86400, httponly=True, samesite="Lax")
                return resp
        return None

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    # ------------------------------------------------------------ Daten-Helfer
    def names() -> dict[int, dict]:
        return {r["uid"]: dict(r) for r in conn().execute("SELECT uid, name, lat, lng, number FROM stations")}

    def current():
        tracker, extra = db.load_tracker(conn())
        return tracker, extra

    def my_now(tracker) -> list[dict]:
        out = []
        for s in cfg.stations:
            t = tracker.st.get(s.id)
            out.append({**s.__dict__, "bikes": t[0] if t else None, "ebikes": t[1] if t else None, "active": bool(t[3]) if t else None})
        return out

    def forecast(uids: list[int], now: float, hours: int = 4, step_min: int = 30):
        start = (int(now) // 900 + 1) * 900
        cols = [(t, week_slot(t)) for t in range(start, start + hours * 3600 + 1, step_min * 60)]
        stats = db.load_stats(conn(), uids, [x for _, ws in cols for x in neighbor_slots(ws)])
        return cols, {u: [predict(stats.get(u), ws, cfg.min_obs) for _, ws in cols] for u in uids}

    def since_days(default: int) -> int:
        try:
            return max(0, int(request.args.get("days", default)))
        except ValueError:
            return default

    # ------------------------------------------------------------ Seiten
    @app.get("/")
    def index():
        now = time.time()
        tracker, extra = current()
        c = conn()
        cols, fc = forecast([s.id for s in cfg.stations], now)
        mid = local_midnight(now)
        nm = names()
        trips_today = c.execute("SELECT count(*) n, sum(direct) d FROM trip WHERE end_ts >= ?", (mid,)).fetchone()
        top_start = c.execute(
            "SELECT start_uid u, count(*) n FROM trip WHERE start_ts >= ? AND start_uid IS NOT NULL AND direct = 0 GROUP BY u ORDER BY n DESC LIMIT 6", (mid,)
        ).fetchall()
        top_end = c.execute(
            "SELECT end_uid u, count(*) n FROM trip WHERE end_ts >= ? AND end_uid IS NOT NULL AND direct = 0 GROUP BY u ORDER BY n DESC LIMIT 6", (mid,)
        ).fetchall()
        polls = c.execute("SELECT count(*) n, sum(ok) ok FROM poll WHERE ts >= ?", (int(now) - 86400,)).fetchone()
        last_err = c.execute("SELECT ts, error FROM poll WHERE ok = 0 ORDER BY ts DESC LIMIT 1").fetchone()
        ff = sum(1 for v in tracker.bk.values() if isinstance(v, tuple))
        lbl = lambda u: nm.get(u, {}).get("name", str(u))  # noqa: E731
        return render_template(
            "index.html",
            cfg=cfg,
            ts=tracker.ts,
            age_min=None if tracker.ts is None else round((now - tracker.ts) / 60),
            mine=my_now(tracker),
            cols=cols,
            fc=fc,
            net={
                "stations": len(tracker.st),
                "at_stations": sum(t[0] for t in tracker.st.values()),
                "free": ff,
                "visible": len(tracker.bk),
                "away": len(tracker.away),
                "trips": trips_today["n"] or 0,
                "direct": trips_today["d"] or 0,
            },
            top_start=svg.bar_list([(lbl(r["u"]), r["n"], f"/station/{r['u']}") for r in top_start]),
            top_end=svg.bar_list([(lbl(r["u"]), r["n"], f"/station/{r['u']}") for r in top_end]),
            sys={
                "db": db.db_size_bytes(cfg.db_path),
                "polls": polls["n"] or 0,
                "polls_ok": polls["ok"] or 0,
                "last_err": last_err,
                "backup": db.get_meta(c, "last_backup"),
                "upload": upload_status(cfg.backup_dir),
            },
        )

    @app.get("/stations")
    def stations():
        tracker, _ = current()
        mid = local_midnight(time.time())
        c = conn()
        starts = dict(c.execute("SELECT start_uid, count(*) FROM trip WHERE start_ts >= ? AND direct = 0 GROUP BY start_uid", (mid,)).fetchall())
        ends = dict(c.execute("SELECT end_uid, count(*) FROM trip WHERE end_ts >= ? AND direct = 0 GROUP BY end_uid", (mid,)).fetchall())
        mine = {s.id for s in cfg.stations}
        rows = []
        for uid, n in names().items():
            t = tracker.st.get(uid)
            rows.append({"uid": uid, "name": n["name"], "number": n["number"], "bikes": t[0] if t else None, "ebikes": t[1] if t else None,
                         "starts": starts.get(uid, 0), "ends": ends.get(uid, 0), "mine": uid in mine})
        sort = request.args.get("sort", "name")
        key = {"bikes": lambda r: -(r["bikes"] or 0), "starts": lambda r: -r["starts"], "ends": lambda r: -r["ends"]}.get(sort, lambda r: r["name"].lower())
        return render_template("stations.html", rows=sorted(rows, key=key), sort=sort)

    @app.get("/station/<int:uid>")
    def station(uid: int):
        c = conn()
        st = c.execute("SELECT * FROM stations WHERE uid = ?", (uid,)).fetchone()
        if not st:
            abort(404)
        now = int(time.time())
        hours = min(max(int(request.args.get("h", 48)), 6), 24 * 14)
        t0 = now - hours * 3600
        before = c.execute("SELECT ts, bikes FROM station_change WHERE uid = ? AND ts < ? ORDER BY ts DESC LIMIT 1", (uid, t0)).fetchone()
        pts = ([(t0, before["bikes"] or 0)] if before else []) + [
            (r["ts"], r["bikes"] or 0) for r in c.execute("SELECT ts, bikes FROM station_change WHERE uid = ? AND ts >= ? ORDER BY ts", (uid, t0))
        ]
        cells = {}
        for r in c.execute("SELECT wslot / 4 AS hw, count(*) n, sum(bikes > 0) a FROM obs WHERE uid = ? GROUP BY hw", (uid,)):
            d, hr = divmod(r["hw"], 24)
            cells[(d, hr)] = (r["a"] / r["n"] if r["n"] >= cfg.min_obs else None, r["n"])
        cols, fc = forecast([uid], now)
        nm = names()
        ev = c.execute(
            "SELECT * FROM bike_event WHERE (to_uid = ? OR from_uid = ?) ORDER BY ts DESC LIMIT 40", (uid, uid)
        ).fetchall()
        tracker, _ = current()
        t = tracker.st.get(uid)
        bikes_here = sorted(n for n, l in tracker.bk.items() if l == uid)
        types = dict(c.execute(f"SELECT number, type FROM bikes WHERE number IN ({','.join('?' * len(bikes_here))})", bikes_here).fetchall()) if bikes_here else {}
        return render_template(
            "station.html", st=st, now_t=t, hours=hours, chart=svg.step_chart(pts, t0, now), heat=svg.week_heatmap(cells, st["name"]),
            cols=cols, preds=fc[uid], events=ev, nm=nm, bikes_here=[(b, types.get(b)) for b in bikes_here],
        )

    @app.get("/map")
    def map_page():
        tracker, _ = current()
        mine = {s.id for s in cfg.stations}
        sts = [{"uid": u, "name": n["name"], "lat": n["lat"], "lng": n["lng"], "bikes": tracker.st.get(u, (0,))[0], "mine": u in mine}
               for u, n in names().items() if u in tracker.st]
        free = [v for v in tracker.bk.values() if isinstance(v, tuple)]
        return render_template("map.html", svgmap=svg.station_map(sts, free), ts=tracker.ts)

    @app.get("/trips")
    def trips():
        c = conn()
        where, args = ["1=1"], []
        if b := request.args.get("bike"):
            where.append("bike = ?"); args.append(b)  # noqa: E702
        if s := request.args.get("station", type=int):
            where.append("(start_uid = ? OR end_uid = ?)"); args += [s, s]  # noqa: E702
        if request.args.get("nodirect"):
            where.append("direct = 0")
        rows = c.execute(f"SELECT * FROM trip WHERE {' AND '.join(where)} ORDER BY end_ts DESC LIMIT 300", args).fetchall()
        week = int(time.time()) - 7 * 86400
        od = c.execute(
            """SELECT start_uid a, end_uid b, count(*) n, avg(duration_s) d FROM trip
               WHERE end_ts >= ? AND direct = 0 AND start_uid IS NOT NULL AND end_uid IS NOT NULL AND start_uid != end_uid
               GROUP BY a, b ORDER BY n DESC LIMIT 20""", (week,)
        ).fetchall()
        rebal = c.execute(
            """SELECT end_ts, end_uid, count(*) n FROM trip WHERE end_ts >= ? AND arrivals_same_poll >= 3
               GROUP BY end_ts, end_uid ORDER BY end_ts DESC LIMIT 30""", (week,)
        ).fetchall()
        return render_template("trips.html", rows=rows, od=od, rebal=rebal, nm=names(), args=request.args)

    @app.get("/bike/<number>")
    def bike(number: str):
        c = conn()
        b = c.execute("SELECT * FROM bikes WHERE number = ?", (number,)).fetchone()
        if not b:
            abort(404)
        tracker, _ = current()
        loc = tracker.bk.get(number)
        trips = c.execute("SELECT * FROM trip WHERE bike = ? ORDER BY end_ts DESC LIMIT 200", (number,)).fetchall()
        stats = c.execute("SELECT count(*) n, sum(distance_m) m, sum(duration_s) s FROM trip WHERE bike = ? AND direct = 0", (number,)).fetchone()
        away = tracker.away.get(number)
        return render_template("bike.html", b=b, loc=loc, away=away, trips=trips, stats=stats, nm=names())

    @app.get("/export")
    def export_page():
        c = conn()
        counts = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in db.EXPORT_TABLES}
        return render_template("export.html", counts=counts, backups=list_backups(cfg.backup_dir), upload=upload_status(cfg.backup_dir),
                               dbsize=db.db_size_bytes(cfg.db_path))

    @app.get("/export/<table>.csv")
    def export_csv(table: str):
        if table not in db.EXPORT_TABLES:
            abort(404)
        days = since_days(0)
        since = int(time.time()) - days * 86400 if days else 0
        sql = db.EXPORT_TABLES[table]
        params = (since,) if "?" in sql else ()

        def gen():
            c2 = db.connect(cfg.db_path, readonly=True)
            try:
                cur = c2.execute(sql, params)
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow([d[0] for d in cur.description])
                while rows := cur.fetchmany(5000):
                    w.writerows(rows)
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
                yield buf.getvalue()
            finally:
                c2.close()

        fn = f"{table}{'-' + str(days) + 'd' if days else ''}.csv"
        return Response(gen(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{fn}"'})

    @app.get("/export/nextbike.sqlite")
    def export_sqlite():
        tmpdir = Path(tempfile.mkdtemp(dir=cfg.data_dir))
        target = snapshot_db(cfg.db_path, tmpdir / "nextbike.sqlite")

        @after_this_request
        def _cleanup(resp):
            resp.call_on_close(lambda: (target.unlink(missing_ok=True), tmpdir.rmdir()))
            return resp

        return send_file(target, as_attachment=True, download_name=f"nextbike-{time.strftime('%Y%m%d-%H%M')}.sqlite")

    @app.get("/backups/<name>")
    def backup_file(name: str):
        p = (cfg.backup_dir / name).resolve()
        if p.parent != cfg.backup_dir.resolve() or not p.name.startswith("nextbike-") or not p.exists():
            abort(404)
        return send_file(p, as_attachment=True)

    # ------------------------------------------------------------ JSON-API (kompatibel zur Cloudflare-Version)
    def station_info(s):
        d = {"id": s.id, "alias": s.alias, "name": s.name, "group": s.group, "primary": s.primary}
        if s.distance_m:
            d["distance_m"] = s.distance_m
        return d

    def bad(msg: str, code: int = 400):
        return jsonify({"error": msg}), code

    @app.get("/api/now")
    def api_now():
        tracker, _ = current()
        now = time.time()
        age = None if tracker.ts is None else round(now - tracker.ts)
        return jsonify({
            "as_of": iso_local(tracker.ts) if tracker.ts else None,
            "age_s": age,
            "stale": age is None or age > 15 * 60,
            "network": {"stations": len(tracker.st), "bikes_at_stations": sum(t[0] for t in tracker.st.values()),
                        "free_floating": sum(1 for v in tracker.bk.values() if isinstance(v, tuple)), "away": len(tracker.away)},
            "stations": [{**station_info(s), "bikes": m["bikes"], "ebikes": m["ebikes"]} for s, m in zip(cfg.stations, my_now(tracker))],
        })

    @app.get("/api/predict")
    def api_predict():
        key = request.args.get("station")
        if not key:
            return bad("Parameter 'station' fehlt (ID oder Alias)")
        s = cfg.find_station(key)
        uid = s.id if s else (int(key) if key.isdigit() else None)
        if uid is None or (not s and not conn().execute("SELECT 1 FROM stations WHERE uid = ?", (uid,)).fetchone()):
            return bad(f"Unbekannte Station '{key}'", 404)
        try:
            dt, ws = parse_at(request.args.get("at"), time.time())
        except BadInput as e:
            return bad(str(e))
        p = predict(db.load_stats(conn(), [uid], neighbor_slots(ws)).get(uid), ws, cfg.min_obs)
        info = station_info(s) if s else {"id": uid, "name": names().get(uid, {}).get("name")}
        return jsonify({"station": info, "at": {"local": dt.isoformat(timespec="minutes"), "utc": iso_utc(dt.timestamp())}, **p})

    @app.get("/api/plan")
    def api_plan():
        group = request.args.get("dir", "start").lower()
        if group not in ("start", "ziel"):
            return bad("dir muss 'start' oder 'ziel' sein")
        try:
            dt, ws = parse_at(request.args.get("at"), time.time())
        except BadInput as e:
            return bad(str(e))
        members = sorted([s for s in cfg.stations if s.group == group], key=lambda s: not s.primary)
        stats = db.load_stats(conn(), [s.id for s in members], neighbor_slots(ws))
        opts = [(s, predict(stats.get(s.id), ws, cfg.min_obs)) for s in members]
        tracker, _ = current()
        return jsonify({
            "at": {"local": dt.isoformat(timespec="minutes"), "utc": iso_utc(dt.timestamp())},
            "group": group,
            "threshold": cfg.plan_threshold,
            "recommendation": recommend(opts, cfg.plan_threshold),
            "options": [{"station": station_info(s), "prediction": p,
                         "now": {"bikes": tracker.st.get(s.id, (None,))[0], "ebikes": tracker.st.get(s.id, (None, None))[1]}} for s, p in opts],
        })

    @app.get("/api/stations")
    def api_stations():
        tracker, _ = current()
        return jsonify([{**n, "bikes": tracker.st.get(u, (None,))[0], "ebikes": tracker.st.get(u, (None, None))[1]} for u, n in names().items()])

    @app.get("/healthz")
    def healthz():
        hb = cfg.data_dir / "heartbeat"
        age = time.time() - hb.stat().st_mtime if hb.exists() else None
        ok = age is not None and age < 600
        return jsonify({"collector_ok": ok, "heartbeat_age_s": None if age is None else round(age)}), (200 if ok else 503)

    # Kurzformen wie bei Cloudflare
    for path in ("now", "predict", "plan"):
        app.add_url_rule(f"/{path}", f"short_{path}", (lambda p: lambda: redirect(f"/api/{p}?{request.query_string.decode()}", 307))(path))

    return app


def main() -> None:
    from waitress import serve

    app = create_app()
    port = int(os.environ.get("WEB_PORT", "8080"))
    print(f"Web-GUI auf http://0.0.0.0:{port}")
    serve(app, host="0.0.0.0", port=port, threads=4)


if __name__ == "__main__":
    main()
