"""Local metric projection.

A transverse Mercator centred on the model area (the same maths as UTM, but
with the central meridian through the middle of *this* area instead of the
middle of a 6-degree zone). Compared with UTM that means: scale error under
1 ppm across a city instead of up to 400 ppm, and -- more visibly -- no grid
convergence, so north is straight up on the print rather than rotated by up
to 3 degrees at a zone edge.

Falls back to a spherical equirectangular approximation if pyproj is missing.
"""

from __future__ import annotations

import math

import numpy as np

try:
    from pyproj import Transformer
    HAVE_PYPROJ = True
except Exception:  # noqa: BLE001
    HAVE_PYPROJ = False


class LocalProjection:
    """lat/lon (WGS84) <-> metres east/north of (lat0, lon0)."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = float(lat0), float(lon0)
        if HAVE_PYPROJ:
            crs = (f"+proj=tmerc +lat_0={self.lat0:.8f} +lon_0={self.lon0:.8f} "
                   "+k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
            self._fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            self._inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        else:
            self._fwd = self._inv = None
            self._mlat = 111_132.95
            self._mlon = 111_319.49 * math.cos(math.radians(self.lat0))

    @classmethod
    def for_bbox(cls, bbox) -> "LocalProjection":
        s, w, n, e = bbox
        return cls((s + n) / 2.0, (w + e) / 2.0)

    def to_xy(self, lat, lon):
        lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
        if self._fwd is not None:
            x, y = self._fwd.transform(lon, lat)
            return np.asarray(x), np.asarray(y)
        return (lon - self.lon0) * self._mlon, (lat - self.lat0) * self._mlat

    def to_latlon(self, x, y):
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        if self._inv is not None:
            lon, lat = self._inv.transform(x, y)
            return np.asarray(lat), np.asarray(lon)
        return self.lat0 + y / self._mlat, self.lon0 + x / self._mlon

    def ring_to_xy(self, coords):
        """[(lat, lon), ...] -> (N, 2) array of metres."""
        arr = np.asarray(coords, dtype=float)
        x, y = self.to_xy(arr[:, 0], arr[:, 1])
        return np.column_stack([x, y])
