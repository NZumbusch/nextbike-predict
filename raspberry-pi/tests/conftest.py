import copy
import json
from pathlib import Path

import pytest

from nextbike.config import load_config

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "feed-luzern-full.json").read_text("utf-8"))
ROOT = Path(__file__).parent.parent


@pytest.fixture
def feed_json():
    return copy.deepcopy(FIXTURE)


def places(j):
    return j["countries"][0]["cities"][0]["places"]


def move_bike(j, bike, to_uid):
    """Velo im Feed-JSON verschieben (to_uid=None ⇒ ausgeliehen)."""
    for p in places(j):
        if bike in p["bike_numbers"]:
            p["bike_numbers"].remove(bike)
            p["bike_list"] = [b for b in p.get("bike_list", []) if b["number"] != bike]
            p["bikes"] -= 1
            p["bikes_available_to_rent"] -= 1
    if to_uid is not None:
        t = next(p for p in places(j) if p["uid"] == to_uid)
        t["bike_numbers"].append(bike)
        t["bikes"] += 1
        t["bikes_available_to_rent"] += 1
    return j


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("WEB_TOKEN", raising=False)
    return load_config(ROOT / "config.toml")
