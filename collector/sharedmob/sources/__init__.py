"""Datenquellen. Jede Quelle hat poll(now) -> Snapshot."""
from __future__ import annotations

from ..config import SourceConfig


def make_source(cfg: SourceConfig, fetch=None):
    from .gbfs import GbfsSource
    from .nextbike import NextbikeSource

    kinds = {"gbfs": GbfsSource, "nextbike": NextbikeSource}
    if cfg.kind not in kinds:
        raise ValueError(f"Unbekannte Quellenart '{cfg.kind}' (erlaubt: {', '.join(kinds)})")
    return kinds[cfg.kind](cfg, fetch=fetch)
