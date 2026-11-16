"""Konsistente SQLite-Backups (Online-Backup-API, läuft auch während des Sammelns)."""
from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .timeutil import TZ


def make_backup(db_path: Path, backup_dir: Path, keep: int, now: float, name: str | None = None) -> Path:
    """Erstellt backups/nextbike-YYYY-MM-DD.sqlite.gz und behält die neuesten `keep` Dateien."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d")
    target = backup_dir / (name or f"nextbike-{stamp}.sqlite.gz")
    tmp_db = backup_dir / f".{target.stem}.tmp"
    src = sqlite3.connect(str(db_path), timeout=60)
    dst = sqlite3.connect(str(tmp_db))
    try:
        src.backup(dst, pages=4096)  # in Häppchen ⇒ Sammler wird nicht lange blockiert
    finally:
        dst.close()
        src.close()
    tmp_gz = target.with_suffix(target.suffix + ".part")
    with open(tmp_db, "rb") as fi, gzip.open(tmp_gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    tmp_db.unlink()
    tmp_gz.chmod(0o644)
    tmp_gz.replace(target)  # atomar: rclone sieht nie eine halbe Datei
    if keep > 0 and name is None:
        olds = sorted(backup_dir.glob("nextbike-*.sqlite.gz"))
        for old in olds[:-keep]:
            old.unlink()
    return target


def snapshot_db(db_path: Path, target: Path) -> Path:
    """Unkomprimierte, konsistente Kopie (für den Download in der Web-GUI)."""
    src = sqlite3.connect(str(db_path), timeout=60)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def list_backups(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("nextbike-*.sqlite.gz"), reverse=True)


def upload_status(backup_dir: Path) -> str | None:
    """Status-Zeile, die der rclone-Container nach jedem Upload-Versuch schreibt."""
    p = backup_dir / "upload-status.txt"
    return p.read_text("utf-8").strip() if p.exists() else None
