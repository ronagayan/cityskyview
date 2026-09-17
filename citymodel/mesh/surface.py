"""DEM -> the model's terrain grid (sampling, cleaning, smoothing, scaling)."""

from __future__ import annotations

import numpy as np
from shapely.geometry import shape as shapely_shape
from shapely.ops import unary_union

from ..geo import dem as demlib
from ..geo.frame import Frame
from ..log import logger
from ..settings import ModelSettings
from .primitives import polygons_of
from .terrain import TerrainSurface, grid_for


def rasterize(geoms, x0, y0, cx, cy, nx, ny) -> np.ndarray:
    """Boolean (ny, nx) mask of grid nodes covered by the shapes (model mm)."""
    from PIL import Image, ImageDraw  # noqa: PLC0415
    img = Image.new("L", (nx, ny), 0)
    draw = ImageDraw.Draw(img)
    for poly in (p for g in geoms for p in polygons_of(g)):
        ext = [((x - x0) / cx, (y - y0) / cy) for x, y in poly.exterior.coords]
        if len(ext) >= 3:
            draw.polygon(ext, fill=255)
        for ring in poly.interiors:
            pts = [((x - x0) / cx, (y - y0) / cy) for x, y in ring.coords]
            if len(pts) >= 3:
                draw.polygon(pts, fill=0)
    return np.asarray(img) > 0


def _sea_polygons(mask, x0, y0, cx, cy, min_area_mm2):
    try:
        from rasterio import features  # noqa: PLC0415
        from rasterio.transform import Affine  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    tr = Affine(cx, 0, x0 - cx / 2.0, 0, cy, y0 - cy / 2.0)
    polys = [shapely_shape(g) for g, v in
             features.shapes(mask.astype(np.uint8), mask=mask, transform=tr) if v == 1]
    polys = [p.buffer(0) for p in polys if p.area >= min_area_mm2]
    if not polys:
        return None
    sea = unary_union(polys)
    # the mask is a staircase of grid cells -- round it off
    return sea.buffer(max(cx, cy)).buffer(-max(cx, cy)).simplify(max(cx, cy) * 0.5)


def make_surface(frame: Frame, dem, source_kind: str, native_res_m: float,
                 settings: ModelSettings, building_shapes_mm=None,
                 lake_shapes_mm=None):
    """Returns ``(TerrainSurface, sea_polygon_mm | None)``."""
    s = settings
    x0, y0, cx, cy, nx, ny = grid_for(frame.outline_mm.bounds, s.cell_mm())
    xs, ys = x0 + np.arange(nx) * cx, y0 + np.arange(ny) * cy
    gx, gy = np.meshgrid(xs, ys)
    lat, lon = frame.mm_to_latlon(gx, gy)
    elev = np.asarray(dem.sample(lat, lon), dtype=np.float64)
    cell_m = cx / frame.scale

    sea = None
    if s.clamp_sea and np.median(elev) > 0.5:
        sea_mask = elev <= 0.01
        if sea_mask.mean() > 0.01:
            plate = frame.outline_mm.area
            sea = _sea_polygons(sea_mask, x0, y0, cx, cy, plate * 0.003)
            if sea is not None:
                sea = sea.intersection(frame.outline_mm)
        elev = np.maximum(elev, 0.0)

    if (s.flatten_under_buildings and source_kind != "DTM" and building_shapes_mm):
        grown = [g.buffer(max(cx, cy) * 1.5) for g in building_shapes_mm]
        mask = rasterize(grown, x0, y0, cx, cy, nx, ny)
        before = elev
        elev = demlib.suppress_bumps(elev, mask, cell_m)
        logger.info("terrain: surface model flattened under buildings "
                    "(largest correction %.1f m)", float((before - elev).max()))

    if lake_shapes_mm:
        for lake in lake_shapes_mm:
            m = rasterize([lake], x0, y0, cx, cy, nx, ny)
            if m.sum() >= 4:
                elev[m] = np.percentile(elev[m], 15)

    sigma = s.terrain_smoothing * min(max(0.6, 0.5 * native_res_m / max(cell_m, 1e-6)), 8.0)
    elev = demlib.smooth(elev, sigma)

    inside = rasterize([frame.outline_mm.buffer(max(cx, cy))], x0, y0, cx, cy, nx, ny)
    lo = float(elev[inside].min()) if inside.any() else float(elev.min())
    hi = float(elev[inside].max()) if inside.any() else float(elev.max())
    z_per_m = frame.scale * s.vertical_exaggeration
    z = s.base_thickness_mm + (np.maximum(elev, lo) - lo) * z_per_m
    surface = TerrainSurface(x0, y0, cx, z, cell_y=cy, elev_min_m=lo, z_per_m=z_per_m,
                             base_mm=s.base_thickness_mm,
                             meta={"elev_min_m": lo, "elev_max_m": hi, "cell_m": cell_m,
                                   "smoothing_sigma_cells": sigma})
    logger.info("terrain: %d x %d grid (%.2f mm = %.1f m per cell), ground %.0f..%.0f m "
                "-> %.1f mm of relief", nx, ny, cx, cell_m, lo, hi, (hi - lo) * z_per_m)
    return surface, sea
