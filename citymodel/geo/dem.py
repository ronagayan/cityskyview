"""Rasters of elevation and how to sample them smoothly.

Every provider hands back a :class:`DemRaster` (or several, as a
:class:`DemMosaic`). Sampling is bicubic on the provider's native grid, at
arbitrary lat/lon points -- so the model grid never has to line up with the
source pixels, and there is no nearest-neighbour stair-stepping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage


def fill_nodata(a: np.ndarray) -> np.ndarray:
    """Replace NaNs with the nearest valid value."""
    bad = ~np.isfinite(a)
    if not bad.any():
        return a
    if bad.all():
        return np.zeros_like(a)
    idx = ndimage.distance_transform_edt(bad, return_distances=False,
                                         return_indices=True)
    return a[tuple(idx)]


@dataclass
class DemRaster:
    """A north-up raster. ``crs`` is "geographic" (pixels linear in lon/lat)
    or "webmercator" (pixels linear in Web-Mercator tile space).

    x0 / y0: coordinate of the *centre* of pixel [0, 0]; dx / dy: pixel step
    (dy is negative for a north-up geographic raster). For webmercator the
    coordinates are global pixel coordinates at ``zoom``.
    """
    data: np.ndarray
    crs: str
    x0: float
    y0: float
    dx: float
    dy: float
    zoom: int = 0

    def __post_init__(self):
        self.data = fill_nodata(np.asarray(self.data, dtype=np.float64))

    def pixel_coords(self, lat, lon):
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        if self.crs == "geographic":
            return (lat - self.y0) / self.dy, (lon - self.x0) / self.dx
        n = 256.0 * 2 ** self.zoom
        gx = (lon + 180.0) / 360.0 * n
        gy = (1.0 - np.arcsinh(np.tan(np.radians(lat))) / math.pi) / 2.0 * n
        return (gy - self.y0) / self.dy, (gx - self.x0) / self.dx

    def contains(self, lat, lon):
        r, c = self.pixel_coords(lat, lon)
        h, w = self.data.shape
        return (r >= -0.5) & (r <= h - 0.5) & (c >= -0.5) & (c <= w - 0.5)

    def sample(self, lat, lon, order: int = 3) -> np.ndarray:
        r, c = self.pixel_coords(lat, lon)
        shape = np.shape(r)
        out = ndimage.map_coordinates(self.data, [np.ravel(r), np.ravel(c)],
                                      order=order, mode="nearest")
        return out.reshape(shape)

    def native_res_m(self, lat: float) -> float:
        if self.crs == "geographic":
            return abs(self.dy) * 111_320.0
        return 156_543.034 * math.cos(math.radians(lat)) / 2 ** self.zoom * abs(self.dx)


class DemMosaic:
    """Several rasters that tile an area (e.g. two GLO-30 cells either side of
    a degree line). Each point is sampled from the raster that contains it."""

    def __init__(self, rasters):
        self.rasters = list(rasters)

    def sample(self, lat, lon, order: int = 3) -> np.ndarray:
        lat, lon = np.asarray(lat, float), np.asarray(lon, float)
        out = np.full(lat.shape, np.nan)
        for r in self.rasters:
            m = np.isnan(out) & r.contains(lat, lon)
            if m.any():
                out[m] = r.sample(lat[m], lon[m], order)
        if np.isnan(out).any():            # outside every raster: open sea
            out = np.where(np.isnan(out), 0.0, out)
        return out

    def native_res_m(self, lat: float) -> float:
        return min(r.native_res_m(lat) for r in self.rasters)


def smooth(grid: np.ndarray, sigma_cells: float) -> np.ndarray:
    if sigma_cells <= 0.05:
        return grid
    return ndimage.gaussian_filter(grid, sigma=sigma_cells, mode="nearest")


def suppress_bumps(grid: np.ndarray, mask: np.ndarray, cell_m: float,
                   window_m: float = 120.0) -> np.ndarray:
    """Surface models (SRTM, Copernicus) include the buildings themselves, so
    a city block shows up as a hill. Under ``mask`` (the building footprints,
    grown a little) replace the surface with its lower envelope -- a grey
    opening wide enough to span a block, then smoothed -- and feather the join.
    Ground away from buildings is left untouched."""
    if not mask.any():
        return grid
    k = max(3, int(round(window_m / max(cell_m, 1e-6))) | 1)
    opened = ndimage.grey_opening(grid, size=(k, k), mode="nearest")
    opened = ndimage.gaussian_filter(opened, sigma=k / 4.0, mode="nearest")
    opened = np.minimum(opened, grid)
    weight = ndimage.gaussian_filter(mask.astype(np.float64),
                                     sigma=max(k / 6.0, 1.0), mode="nearest")
    weight = np.clip(weight * 1.6, 0.0, 1.0)
    return grid * (1.0 - weight) + opened * weight
