"""Speicherverbrauch und Prognose ("voll in ≈ X Tagen") aus dem gemessenen Wachstum."""
from __future__ import annotations

import shutil
from collections import defaultdict
from datetime import date
from pathlib import Path

from .config import Config


def _files(p: Path):
    return (f for f in p.rglob("*") if f.is_file()) if p.exists() else iter(())


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in _files(p))


def daily_growth(cfg: Config) -> dict[str, int]:
    """Bytes pro Kalendertag aus Archiv-Partitionen (…/date=JJJJ-MM-TT/) und Rohdaten-Ordnern (raw/JJJJ-MM-TT/)."""
    per_day: dict[str, int] = defaultdict(int)
    for f in _files(cfg.archive_dir):
        for part in f.parts:
            if part.startswith("date="):
                per_day[part[5:]] += f.stat().st_size
    if cfg.raw_dir.exists():
        for d in cfg.raw_dir.iterdir():
            if d.is_dir():
                per_day[d.name] += dir_size(d)
    return dict(per_day)


def usage(cfg: Config, today: date | None = None) -> dict:
    today = today or date.today()
    du = shutil.disk_usage(cfg.data_dir if cfg.data_dir.exists() else ".")
    growth = daily_growth(cfg)
    # nur abgeschlossene Tage (heute ist unvollständig), die letzten 7
    full = sorted((d, b) for d, b in growth.items() if d < today.isoformat())[-7:]
    per_day = int(sum(b for _, b in full) / len(full)) if full else None
    days_left = int(du.free / per_day) if per_day else None
    return {
        "hot": sum(f.stat().st_size for f in cfg.db_path.parent.glob("*") if f.is_file()) if cfg.db_path.parent.exists() else 0,
        "archive": dir_size(cfg.archive_dir),
        "raw": dir_size(cfg.raw_dir),
        "backups": dir_size(cfg.backup_dir),
        "disk_free": du.free,
        "disk_total": du.total,
        "free_ratio": du.free / du.total if du.total else None,
        "per_day": per_day,              # Wachstum Archiv + Rohdaten pro Tag (Ø der letzten bis zu 7 Tage)
        "days_measured": len(full),
        "days_left": days_left,
    }
