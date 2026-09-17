"""Offline fixtures: a hand-built Overpass response and an analytic hill, so
the whole mesh pipeline is tested without touching the network."""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from citymodel import pipeline  # noqa: E402
from citymodel.data import elevation  # noqa: E402
from citymodel.data.osm_parse import parse_features  # noqa: E402
from citymodel.settings import Area, ModelSettings  # noqa: E402

BBOX = (32.0870, 34.8090, 32.0920, 34.8160)          # ~660 x 555 m


class HillDem:
    """A smooth 60 m hill with a constant 8 % slope underneath it."""

    def sample(self, lat, lon, order=3):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        y = (lat - BBOX[0]) / (BBOX[2] - BBOX[0])
        x = (lon - BBOX[1]) / (BBOX[3] - BBOX[1])
        return 20.0 + 45.0 * x + 60.0 * np.exp(-(((x - 0.5) / 0.25) ** 2 + ((y - 0.5) / 0.25) ** 2))

    def native_res_m(self, lat):
        return 10.0


class _Builder:
    def __init__(self):
        self.elements, self._nid, self._wid = [], 0, 100

    def ring(self, lat, lon, dlat, dlon, tags, closed=True):
        ids = []
        for la, lo in ((lat, lon), (lat, lon + dlon), (lat + dlat, lon + dlon), (lat + dlat, lon)):
            self._nid += 1
            self.elements.append({"type": "node", "id": self._nid, "lat": la, "lon": lo})
            ids.append(self._nid)
        self._wid += 1
        self.elements.append({"type": "way", "id": self._wid,
                              "nodes": ids + ([ids[0]] if closed else []), "tags": tags})
        return self._wid

    def line(self, pts, tags):
        ids = []
        for la, lo in pts:
            self._nid += 1
            self.elements.append({"type": "node", "id": self._nid, "lat": la, "lon": lo})
            ids.append(self._nid)
        self._wid += 1
        self.elements.append({"type": "way", "id": self._wid, "nodes": ids, "tags": tags})
        return self._wid


@pytest.fixture(scope="session")
def osm_json():
    b = _Builder()
    ids = {}
    ids["tagged"] = b.ring(32.0880, 34.8100, 0.0003, 0.0004, {"building": "yes", "height": "18 m"})
    ids["levels"] = b.ring(32.0885, 34.8110, 0.0003, 0.0004,
                           {"building": "residential", "building:levels": "5", "name": "Levels House"})
    ids["plain"] = b.ring(32.0890, 34.8120, 0.0002, 0.0003, {"building": "house"})
    ids["big"] = b.ring(32.0895, 34.8130, 0.0005, 0.0010, {"building": "commercial"})
    ids["sliver"] = b.ring(32.0905, 34.8150, 0.000004, 0.000004, {"building": "yes"})
    ids["gabled"] = b.ring(32.0900, 34.8100, 0.0002, 0.0005,
                           {"building": "house", "roof:shape": "gabled", "height": "9"})
    ids["dome"] = b.ring(32.0908, 34.8112, 0.0003, 0.00035,
                         {"building": "mosque", "roof:shape": "dome", "height": "14"})
    # two buildings sharing a wall, and two that overlap outright
    ids["row_a"] = b.ring(32.0875, 34.8130, 0.0002, 0.0003, {"building": "terrace", "height": "9"})
    ids["row_b"] = b.ring(32.0875, 34.8133, 0.0002, 0.0003, {"building": "terrace", "height": "12"})
    ids["over_a"] = b.ring(32.0912, 34.8130, 0.0003, 0.0004, {"building": "yes", "height": "10"})
    ids["over_b"] = b.ring(32.09135, 34.8132, 0.0003, 0.0004, {"building": "yes", "height": "15"})
    # a tower described by building:part tiers
    ids["tower"] = b.ring(32.0880, 34.8145, 0.0004, 0.0005, {"building": "office", "name": "Tier Tower"})
    b.ring(32.0880, 34.8145, 0.0004, 0.0005, {"building:part": "yes", "height": "20"})
    b.ring(32.0881, 34.8146, 0.0002, 0.0003,
           {"building:part": "yes", "height": "45", "min_height": "20"})
    b.line([(32.0872, 34.8092), (32.0895, 34.8125), (32.0918, 34.8158)],
           {"highway": "primary"})
    b.line([(32.0918, 34.8092), (32.0872, 34.8158)], {"highway": "residential"})
    b.ring(32.0902, 34.8140, 0.0006, 0.0008, {"natural": "water"})
    b.ring(32.0873, 34.8095, 0.0005, 0.0006, {"leisure": "park"})
    return {"elements": b.elements, "ids": {k: f"way/{v}" for k, v in ids.items()}}


@pytest.fixture(scope="session")
def features(osm_json):
    return parse_features(osm_json)


@pytest.fixture(scope="session")
def area():
    return Area(shape="rectangle", bbox=BBOX)


@pytest.fixture(scope="session")
def settings():
    return ModelSettings(size_mm=150.0, green_enabled=True, terrain_quality="normal",
                         min_feature_mm=0.5)


def make_data(features, dem=None):
    info = elevation.SourceInfo("test", "synthetic hill", "10 m", "DTM", "-", "")
    return pipeline.SourceData(
        bbox=BBOX, features=features,
        dem=elevation.DemResult(dem or HillDem(), info, 10.0))


@pytest.fixture(scope="session")
def model(area, settings, features):
    return pipeline.build_model(area, settings, make_data(features))


def metres(lat0=BBOX[0]):
    return 111_320.0 * math.cos(math.radians(lat0))
