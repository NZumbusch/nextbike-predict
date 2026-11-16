import copy
import json
from pathlib import Path

import pytest

from sharedmob import http
from sharedmob.config import load_config

FX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


def fx(name):
    return json.loads((FX / name).read_text("utf-8"))


class FakeFetch:
    """Simuliert HTTP: URL-Ende → JSON (veränderbar), merkt sich Aufrufe."""

    def __init__(self):
        sm = {
            "gbfs.json": fx("sm_gbfs.json"),
            "free_bike_status.json": fx("sm_free_bike_status.json"),
            "station_status.json": fx("sm_station_status.json"),
            "station_information.json": fx("sm_station_information.json"),
            "providers.json": fx("sm_providers.json"),
        }
        self.docs = sm
        self.nextbike = fx("nextbike_live.json")
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self.ts = 1_800_000_000

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        key = "nextbike" if "nextbike-live" in url else url.rsplit("/", 1)[-1]
        if key in self.fail or any(f in url for f in self.fail):
            raise http.FetchError(f"{url}: HTTP 503")
        if key == "nextbike":
            doc = self.nextbike
        elif key not in self.docs:
            raise http.FetchError(f"{url}: HTTP 404")
        else:
            doc = copy.deepcopy(self.docs[key])
            if "last_updated" in doc:
                doc["last_updated"] = self.ts
        body = json.dumps(doc).encode()
        return http.Response(url, 200, body, len(body) // 8, 5)

    # Hilfen zum Verändern der Feeds
    def bikes(self):
        return self.docs["free_bike_status.json"]["data"]["bikes"]

    def places(self):
        return [p for c in self.nextbike["countries"] for ci in c["cities"] for p in ci["places"]]


@pytest.fixture
def fake():
    return FakeFetch()


@pytest.fixture
def cfg(tmp_path):
    return load_config(ROOT / "config.toml", env={"DATA_DIR": str(tmp_path)})
