/**
 * Minimale Statusseite ohne externe Abhängigkeiten. Enthält den Token NICHT
 * (Auto-Refresh lädt einfach dieselbe URL neu).
 */
import type { StatusData } from "./api";

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

function cell(p: StatusData["rows"][number]["preds"][number]): string {
  if (p.status !== "ok") return `<td class="na" title="${esc(p.message ?? "")}">–<small>n=${p.n}</small></td>`;
  const pr = p.probability!;
  const hue = Math.round(pr * 120); // 0 = rot, 120 = grün
  return `<td style="background:hsl(${hue} 70% 85%)" title="${esc(p.slot)} · Ø ${p.avg_bikes} Velos · E-Bike ${Math.round(p.probability_ebike! * 100)} % · n=${p.n}">${Math.round(pr * 100)}%<small>Ø${p.avg_bikes} n=${p.n}</small></td>`;
}

export function renderStatus(d: StatusData): string {
  const groups: [string, "start" | "ziel"][] = [["Start", "start"], ["Ziel", "ziel"]];
  const nowRows = d.now.stations
    .map(
      (s) =>
        `<tr><td>${esc(s.name)}${s.primary ? " ★" : ""}${"distance_m" in s ? ` <small>${s.distance_m} m</small>` : ""}</td>` +
        `<td class="big">${s.bikes ?? "?"}</td><td>${s.ebikes ?? "?"}</td></tr>`,
    )
    .join("");
  const head = d.columns.map((c) => `<th>${c.label}</th>`).join("");
  const progRows = groups
    .map(([label, g]) => {
      const rows = d.rows.filter((r) => r.station.group === g);
      return (
        `<tr class="grp"><th colspan="${d.columns.length + 1}">${label}</th></tr>` +
        rows.map((r) => `<tr><th class="st">${esc(r.station.name)}${r.station.primary ? " ★" : ""}</th>${r.preds.map(cell).join("")}</tr>`).join("")
      );
    })
    .join("");
  const stale = d.now.stale ? `<p class="warn">⚠ Daten veraltet oder noch keine vorhanden.</p>` : "";
  const err =
    d.lastError && Date.now() / 1000 - d.lastError.ts < 3600
      ? `<p class="warn">Letzter Feed-Fehler ${new Date(d.lastError.ts * 1000).toISOString().slice(11, 16)} UTC: ${esc(d.lastError.msg)}</p>`
      : "";
  return `<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="300">
<title>Velo Luzern</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{margin:0 auto;max-width:900px;padding:12px 16px;background:Canvas;color:CanvasText}
h1{font-size:1.2rem;margin:.2rem 0}h2{font-size:1rem;margin:1.2rem 0 .4rem}
table{border-collapse:collapse;width:100%}td,th{padding:4px 6px;border-bottom:1px solid #8884;text-align:center}
td:first-child,th.st{text-align:left;font-weight:normal}.big{font-size:1.4rem;font-weight:700}
small{display:block;font-size:.7rem;opacity:.7}td.na{opacity:.5}tr.grp th{text-align:left;background:#8882}
td[style]{color:#111}.scroll{overflow-x:auto}.warn{color:#c50}footer{margin-top:1rem;font-size:.75rem;opacity:.7}
</style></head><body>
<h1>🚲 nextbike Luzern</h1>
<p>Stand: ${d.now.as_of ? esc(d.now.as_of.slice(11, 16)) + ` (vor ${Math.round((d.now.age_s ?? 0) / 60)} Min)` : "–"}</p>
${stale}${err}
<h2>Jetzt</h2>
<table><tr><th style="text-align:left">Station</th><th>Velos</th><th>davon E</th></tr>${nowRows}</table>
<h2>Prognose: Chance auf ≥ 1 Velo</h2>
<div class="scroll"><table><tr><th></th>${head}</tr>${progRows}</table></div>
<h2>Daten herunterladen (CSV)</h2>
<p class="dl"><a data-t="obs">Rohdaten eigene Stationen</a> · <a data-t="agg">Aggregate</a> · <a data-t="stations">Stationen</a> · <a data-t="snap" data-day="1">Netz-Snapshots letzte 24 h</a></p>
<p><small>Vollständiger Export aller Seiten: <code>npm run export</code> (siehe README).</small></p>
<script>
(function(){var t=new URLSearchParams(location.search).get("token")||"";
document.querySelectorAll("a[data-t]").forEach(function(a){var q="table="+a.dataset.t;
if(a.dataset.day)q+="&cursor="+(Math.floor(Date.now()/1000)-86400);
a.href="/export?"+q+"&token="+encodeURIComponent(t);});})();
</script>
<footer>Anteil historischer Beobachtungen (gleicher Wochentag, ±15 Min) mit ≥ 1 Velo · Ø = durchschnittliche Anzahl · n = Beobachtungen · – = zu wenig Daten</footer>
</body></html>`;
}
