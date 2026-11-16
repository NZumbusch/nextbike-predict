"""Kleine serverseitige SVG-Diagramme (offline, ohne JS-Bibliotheken)."""
from __future__ import annotations

import math
from datetime import datetime
from html import escape

from markupsafe import Markup

from .archive import TZ

DOW = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def line_chart(series: list[tuple[int, float | None]], t0: int, t1: int, label: str, *, w: int = 720, h: int = 150,
               unit: str = "", bars: bool = False, ymax: float | None = None, pct: bool = False) -> Markup:
    pl, pr, pt, pb = 44, 8, 18, 22
    iw, ih = w - pl - pr, h - pt - pb
    vals = [v for _, v in series if v is not None]
    top = ymax if ymax is not None else (max(vals) if vals else 1)
    top = top or 1
    if not pct and ymax is None:  # "schöne" Obergrenze
        mag = 10 ** math.floor(math.log10(top)) if top > 0 else 1
        top = math.ceil(top / mag * 2) / 2 * mag

    def x(t):
        return pl + (min(max(t, t0), t1) - t0) / max(1, t1 - t0) * iw

    def y(v):
        return pt + ih - (v / top) * ih

    fmt = (lambda v: f"{v * 100:.0f}%") if pct else (lambda v: f"{v:,.0f}".replace(",", "'"))
    p = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{escape(label)}">',
         f'<text x="{pl}" y="12" class="lbl">{escape(label)}{escape(unit)}</text>']
    for i in range(5):
        v = top * i / 4
        p.append(f'<line x1="{pl}" x2="{w - pr}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                 f'<text x="{pl - 4}" y="{y(v) + 3:.1f}" class="ax" text-anchor="end">{fmt(v)}</text>')
    span_h = (t1 - t0) / 3600
    step = 6 if span_h <= 48 else 12 if span_h <= 120 else 24
    t = (t0 // 3600 + 1) * 3600
    while t < t1:
        d = datetime.fromtimestamp(t, TZ)
        if d.hour % step == 0:
            lab = f"{DOW[d.weekday()]} {d.day}." if d.hour == 0 else f"{d.hour:02d}h"
            p.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{pt}" y2="{pt + ih}" class="grid"/>'
                     f'<text x="{x(t):.1f}" y="{h - 6}" class="ax" text-anchor="middle">{lab}</text>')
        t += 3600
    pts = [(t, v) for t, v in series if v is not None and t0 <= t <= t1]
    if bars and pts:
        bw = max(1.0, iw / max(1, (t1 - t0) / 300) * 0.8)
        for t, v in pts:
            p.append(f'<rect x="{x(t) - bw / 2:.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{max(0.0, pt + ih - y(v)):.1f}" class="bar"/>')
    elif pts:
        d, prev = [], None
        for t, v in pts:
            gap = prev is not None and t - prev > 1800  # Lücke > 30 min nicht verbinden
            d.append(f"{'M' if prev is None or gap else 'L'}{x(t):.1f},{y(v):.1f}")
            prev = t
        p.append(f'<path d="{" ".join(d)}" class="line"/>')
    if not pts:
        p.append(f'<text x="{pl + iw / 2}" y="{pt + ih / 2}" class="ax" text-anchor="middle">keine Daten</text>')
    p.append("</svg>")
    return Markup("".join(p))


def timeline(gaps: list[dict], t0: int, t1: int, first_ts: int | None = None, w: int = 720, h: int = 40) -> Markup:
    """Zeitleiste: grün = Daten, rot = Lücke, grau = vor Beginn der Sammlung."""
    pl, pr = 44, 8
    iw = w - pl - pr

    def x(t):
        return pl + (min(max(t, t0), t1) - t0) / max(1, t1 - t0) * iw

    start = max(t0, first_ts) if first_ts else t1
    p = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Datenlücken">',
         f'<rect x="{pl}" y="6" width="{iw}" height="14" class="none"/>',
         f'<rect x="{x(start):.1f}" y="6" width="{max(0.0, x(t1) - x(start)):.1f}" height="14" class="ok"/>']
    for g in gaps:
        if g["end_ts"] < t0 or g["start_ts"] > t1:
            continue
        x0, x1 = x(g["start_ts"]), x(g["end_ts"])
        tip = f"{datetime.fromtimestamp(g['start_ts'], TZ):%d.%m. %H:%M} – {datetime.fromtimestamp(g['end_ts'], TZ):%H:%M}: {g['reason']}"
        p.append(f'<rect x="{x0:.1f}" y="4" width="{max(2.0, x1 - x0):.1f}" height="18" class="gap"><title>{escape(tip)}</title></rect>')
    t = (t0 // 86400 + 1) * 86400
    while t < t1:
        d = datetime.fromtimestamp(t, TZ)
        p.append(f'<text x="{x(t):.1f}" y="{h - 4}" class="ax" text-anchor="middle">{d.day}.{d.month}.</text>')
        t += 86400
    p.append("</svg>")
    return Markup("".join(p))
