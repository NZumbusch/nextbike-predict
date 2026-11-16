"""DuckDB-Verbindung mit allen Sichten auf das Archiv.

    import load
    con = load.connect("pfad/zu/data/archive")     # lokal oder von Google Drive synchronisiert
    con.sql("SELECT provider, count(*) FROM trip GROUP BY 1").show()
"""
from __future__ import annotations

from pathlib import Path

import duckdb


def connect(archive: str | Path = "data/archive", database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    archive = Path(archive).expanduser().resolve()
    if not (archive / "trip").exists():
        raise FileNotFoundError(f"Kein Archiv unter {archive} (erwartet: trip/, vehicle_spell/, …)")
    con = duckdb.connect(database)
    sql = (Path(__file__).parent / "views.sql").read_text("utf-8").replace("{ARCHIVE}", archive.as_posix())
    con.execute(sql)
    return con
