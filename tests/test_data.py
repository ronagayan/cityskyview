"""Data layer pieces that can be tested offline."""

from __future__ import annotations

import time

import numpy as np
import pytest

from citymodel.data import overpass
from citymodel.data.cache import DiskCache
from citymodel.data.geocode import parse_coordinates
from citymodel.geo.dem import DemRaster, smooth, suppress_bumps
from citymodel.geo.projection import LocalProjection
from citymodel.mesh.heights import (NeighbourHeights, estimate_height, parse_length_m,
                                    tagged_height)


def test_cache_roundtrip_ttl_and_atomicity(tmp_path):
    c = DiskCache("t", ttl_s=0.2, root=tmp_path)
    assert c.get_json("k") is None
    c.put_json("k", {"a": 1})
    assert c.get_json("k") == {"a": 1}
    assert not list(tmp_path.rglob("*.tmp"))
    time.sleep(0.3)
    assert c.get_json("k") is None                 # expired ...
    assert c.get_stale_json("k") == {"a": 1}       # ... but usable when offline


def test_overpass_uses_cache_and_never_hits_network(tmp_path, monkeypatch, osm_json):
    cache = DiskCache("overpass", root=tmp_path)
    bbox = (32.0871, 34.8091, 32.0919, 34.8159)
    snapped = overpass.snap_bbox(bbox)
    assert snapped[0] <= bbox[0] and snapped[2] >= bbox[2]
    assert overpass.snap_bbox((32.08715, 34.80915, 32.09185, 34.81585)) == snapped   # nudged
    cache.put_json("v2|" + ",".join(f"{v:.6f}" for v in snapped), {"elements": osm_json["elements"]})
    monkeypatch.setattr(overpass, "post", lambda *a, **k: pytest.fail("network used"))
    data = overpass.fetch_osm(bbox, cache=cache)
    assert len(data["elements"]) == len(osm_json["elements"])


def test_overpass_refuses_absurd_areas():
    with pytest.raises(overpass.OverpassError, match="too large"):
        overpass.fetch_osm((31.0, 34.0, 33.0, 36.0))


def test_projection_is_metric_and_round_trips():
    p = LocalProjection(32.09, 34.81)
    x, y = p.to_xy(np.array([32.09, 32.09, 32.10]), np.array([34.81, 34.82, 34.81]))
    assert abs(x[0]) < 1e-6 and abs(y[0]) < 1e-6
    assert x[1] == pytest.approx(943.6, abs=1.5)        # 0.01 deg of longitude at 32 N
    assert y[2] == pytest.approx(1108.9, abs=1.5)       # 0.01 deg of latitude
    lat, lon = p.to_latlon(x, y)
    np.testing.assert_allclose(lat, [32.09, 32.09, 32.10], atol=1e-9)
    np.testing.assert_allclose(lon, [34.81, 34.82, 34.81], atol=1e-9)


def test_dem_sampling_is_smooth_not_stepped():
    # a coarse 30 m raster of a plane: bicubic sampling must reproduce the
    # plane between pixels instead of terraces
    lat0, lon0, d = 32.10, 34.80, 1 / 3600
    rows, cols = np.mgrid[0:40, 0:40]
    raster = DemRaster(100.0 + 2.0 * cols - 1.0 * rows, "geographic", lon0, lat0, d, -d)
    lat = lat0 - d * np.linspace(5, 30, 300)
    lon = lon0 + d * np.linspace(5, 30, 300)
    z = raster.sample(lat, lon)
    expect = 100.0 + 2.0 * np.linspace(5, 30, 300) - 1.0 * np.linspace(5, 30, 300)
    np.testing.assert_allclose(z, expect, atol=1e-3)     # spline edge effects only
    assert np.all(np.diff(z) > 0)


def test_bump_suppression_only_touches_masked_ground():
    y, x = np.mgrid[0:120, 0:120]
    ground = 50.0 + 0.2 * x
    dsm = ground.copy()
    dsm[40:60, 40:60] += 25.0                       # a city block seen by radar
    mask = np.zeros_like(dsm, bool)
    mask[38:62, 38:62] = True
    fixed = suppress_bumps(dsm, mask, cell_m=5.0)
    assert np.abs(fixed[45:55, 45:55] - ground[45:55, 45:55]).max() < 3.0
    np.testing.assert_allclose(fixed[:, 100:], dsm[:, 100:], atol=1e-6)
    assert smooth(dsm, 0.0) is dsm


def test_height_parsing_and_fallback():
    assert parse_length_m("18 m") == 18.0
    assert parse_length_m("60 ft") == pytest.approx(18.288)
    assert parse_length_m("5'11\"") == pytest.approx(1.8034)
    assert parse_length_m("tall") is None
    assert tagged_height({"building:levels": "5"}).metres == pytest.approx(15.5)
    assert tagged_height({}) is None
    assert estimate_height({"building": "garage"}, 30, 25.0).metres < 4      # stays small
    lone = estimate_height({"building": "yes"}, 400, None).metres
    downtown = estimate_height({"building": "yes"}, 400, 40.0).metres
    assert downtown > lone * 2                     # pulled towards tall neighbours
    n = NeighbourHeights(np.array([[0, 0], [10, 0], [0, 10], [900, 900]]), np.array([10, 20, 30, 99]))
    assert n.median_near(1, 1) == 20.0
    assert n.median_near(5000, 5000) is None


@pytest.mark.parametrize("text,expect", [
    ("32.0853, 34.7818", (32.0853, 34.7818)),
    ("40.7128 N 74.0060 W", (40.7128, -74.0060)),
    ("S 33.8688 E 151.2093", (-33.8688, 151.2093)),
    ("-33.8688 151.2093", (-33.8688, 151.2093)),
    ("Eiffel Tower", None), ("Tel Aviv 12", None), ("95.0, 10.0", None)])
def test_coordinate_parsing(text, expect):
    assert parse_coordinates(text) == expect
