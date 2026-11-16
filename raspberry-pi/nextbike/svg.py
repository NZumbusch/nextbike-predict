"""Kleine serverseitige SVG-Diagramme (keine JS-/CDN-Abhängigkeiten, funktioniert offline im WLAN)."""
from __future__ import annotations

import math
from html import escape

from markupsafe import Markup

from .timeutil import DOW_DE, local


def _color(p: float | None) -> str:
    if p is None:
        return "var(--na)"
    return f"hsl({round(p * 120)} 65% 55%)"


def step_chart(points: list[tuple[int, int]], t0: int, t1: int, w: int = 640, h: int = 170, label: str = "Velos") -> Markup:
    """Treppenkurve Bestand über Zeit. points = [(ts, wert)], sortiert; erster Punkt darf vor t0 liegen."""
    pad_l, pad_b, pad_t, pad_r = 30, 22, 8, 8
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    ymax = max([v for _, v in points] + [3])
    ymax = int(math.ceil(ymax / 2) * 2)

    def x(t: float) -> float:
        return pad_l + (min(max(t, t0), t1) - t0) / max(t1 - t0, 1) * iw

    def y(v: float) -> float:
        return pad_t + ih - v / ymax * ih

    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{escape(label)} über Zeit">']
    for v in range(0, ymax + 1, max(1, ymax // 4)):
        parts.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 4}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v}</text>')
    # Zeitachse: alle 6 h
    first = (t0 // 3600 + 1) * 3600
    for t in range(first, t1, 3600):
        d = local(t)
        if d.hour % 6 == 0:
            lbl = f"{DOW_DE[d.weekday()]} {d.hour:02d}h" if d.hour == 0 else f"{d.hour:02d}h"
            parts.append(f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{pad_t}" y2="{pad_t + ih}" class="grid"/>')
            parts.append(f'<text x="{x(t):.1f}" y="{h - 6}" class="ax" text-anchor="middle">{lbl}</text>')
    if points:
        d = []
        prev_v = None
        for t, v in points:
            if prev_v is None:
                d.append(f"M{x(t):.1f},{y(v):.1f}")
            else:
                d.append(f"H{x(t):.1f}V{y(v):.1f}")
            prev_v = v
        d.append(f"H{x(t1):.1f}")
        parts.append(f'<path d="{"".join(d)}" class="line"/>')
    parts.append("</svg>")
    return Markup("".join(parts))


def week_heatmap(cells: dict[tuple[int, int], tuple[float | None, int]], title: str = "") -> Markup:
    """7 Tage × 24 Stunden; cells[(dow, hour)] = (Anteil ≥1 Velo, n)."""
    cw, ch, pad_l, pad_t = 22, 18, 26, 16
    w, h = pad_l + 24 * cw + 2, pad_t + 7 * ch + 2
    parts = [f'<svg viewBox="0 0 {w} {h}" class="heat" role="img" aria-label="{escape(title)}">']
    for hr in range(0, 24, 3):
        parts.append(f'<text x="{pad_l + hr * cw + cw / 2}" y="11" class="ax" text-anchor="middle">{hr}</text>')
    for d in range(7):
        parts.append(f'<text x="{pad_l - 4}" y="{pad_t + d * ch + 13}" class="ax" text-anchor="end">{DOW_DE[d]}</text>')
        for hr in range(24):
            p, n = cells.get((d, hr), (None, 0))
            tip = f"{DOW_DE[d]} {hr:02d}–{hr + 1:02d} Uhr: " + (f"{round(p * 100)} % (n={n})" if p is not None else f"zu wenig Daten (n={n})")
            parts.append(
                f'<rect x="{pad_l + hr * cw}" y="{pad_t + d * ch}" width="{cw - 2}" height="{ch - 2}" rx="2" '
                f'fill="{_color(p)}"><title>{escape(tip)}</title></rect>'
            )
    parts.append("</svg>")
    return Markup("".join(parts))


def station_map(stations: list[dict], free: list[tuple[float, float]], w: int = 640, h: int = 520) -> Markup:
    """Einfache Karte (Äquirektangular-Projektion): Kreisgrösse ~ Velos, Klick → Station."""
    pts = [(s["lat"], s["lng"]) for s in stations if s.get("lat")] + list(free)
    if not pts:
        return Markup("<p>Keine Koordinaten vorhanden.</p>")
    # Ausreisser (weit entfernte Gemeinden) beschneiden: 5–95 %-Quantil + Rand
    lats = sorted(p[0] for p in pts)
    lngs = sorted(p[1] for p in pts)
    q = lambda a, f: a[min(len(a) - 1, max(0, int(f * (len(a) - 1))))]  # noqa: E731
    la0, la1, lo0, lo1 = q(lats, 0.03), q(lats, 0.97), q(lngs, 0.03), q(lngs, 0.97)
    dla, dlo = max(la1 - la0, 0.005) * 0.1, max(lo1 - lo0, 0.005) * 0.1
    la0, la1, lo0, lo1 = la0 - dla, la1 + dla, lo0 - dlo, lo1 + dlo
    k = math.cos(math.radians((la0 + la1) / 2))
    sx, sy = w / ((lo1 - lo0) * k), h / (la1 - la0)
    s_ = min(sx, sy)
    ox, oy = (w - (lo1 - lo0) * k * s_) / 2, (h - (la1 - la0) * s_) / 2

    def xy(lat: float, lng: float) -> tuple[float, float] | None:
        if not (la0 <= lat <= la1 and lo0 <= lng <= lo1):
            return None
        return ox + (lng - lo0) * k * s_, oy + (la1 - lat) * s_

    parts = [f'<svg viewBox="0 0 {w} {h}" class="map" role="img" aria-label="Karte der Stationen">']
    for lat, lng in free:
        p = xy(lat, lng)
        if p:
            parts.append(f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="2.5" class="ff"><title>frei abgestellt</title></circle>')
    for s in sorted(stations, key=lambda s: -s["bikes"]):
        p = xy(s["lat"], s["lng"]) if s.get("lat") else None
        if not p:
            continue
        r = 3 + 2.2 * math.sqrt(s["bikes"])
        cls = "st mine" if s.get("mine") else ("st empty" if s["bikes"] == 0 else "st")
        parts.append(
            f'<a href="/station/{s["uid"]}"><circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="{r:.1f}" class="{cls}">'
            f'<title>{escape(s["name"])}: {s["bikes"]} Velos</title></circle></a>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))


def bar_list(items: list[tuple[str, int, str | None]], max_items: int = 10) -> Markup:
    """Horizontale Balken (Label, Wert, Link)."""
    items = items[:max_items]
    if not items:
        return Markup("<p class='muted'>Noch keine Daten.</p>")
    m = max(v for _, v, _ in items) or 1
    rows = []
    for label, v, href in items:
        lab = f'<a href="{escape(href)}">{escape(label)}</a>' if href else escape(label)
        rows.append(f'<div class="bar"><span class="bl">{lab}</span><span class="bv" style="width:{v / m * 100:.0f}%"></span><span class="bn">{v}</span></div>')
    return Markup("".join(rows))
