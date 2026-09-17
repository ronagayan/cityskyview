"""Roads, water and green space as thin solids draped over the terrain.

Each layer is one closed body whose underside sits slightly *inside* the
terrain and whose top stands slightly proud of it, following the relief. The
2D shapes are made disjoint first (roads beat water beat green, and every
layer keeps clear of building footprints), so no two parts fight for the same
volume -- in the slicer or in the preview's depth buffer.
"""

from __future__ import annotations

import numpy as np
import trimesh
from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from ..geo.frame import Frame
from ..log import logger
from ..settings import ModelSettings
from .primitives import clean_polygon, heightfield_solid, polygons_of
from .terrain import TerrainSurface

LAYER_ORDER = ("roads", "water", "green")          # priority where they overlap
SINK_MM = 0.35                                     # how far a layer reaches into the ground


def _to_mm(geom, frame: Frame):
    k = frame.scale
    return affinity.affine_transform(
        geom, [k, 0, 0, k, -frame.x_off * k, -frame.y_off * k])


def layer_shapes_mm(features: dict, frame: Frame, settings: ModelSettings,
                    building_footprints=None, sea_mm=None) -> dict:
    """``{layer: shapely geometry in model mm}``, mutually disjoint."""
    s = settings
    k = frame.scale
    min_w_m = s.min_feature_mm / k                      # thinnest printable line
    wanted = {"roads": s.roads_enabled, "water": s.water_enabled, "green": s.green_enabled}
    raw = {}
    for layer in LAYER_ORDER:
        if not wanted[layer]:
            continue
        geoms = []
        names = ("roads", "rail") if layer == "roads" else (layer,)
        for name in names:
            for feat in features.get(name, []):
                try:
                    xy = frame.proj.ring_to_xy(feat.coords)
                    if feat.kind == "line":
                        g = LineString(xy).buffer(max(feat.width_m, min_w_m) / 2.0,
                                                  cap_style="flat", join_style="mitre",
                                                  mitre_limit=2.0)
                    else:
                        holes = [frame.proj.ring_to_xy(h) for h in feat.holes if len(h) >= 4]
                        g = clean_polygon(Polygon(xy, holes))
                except Exception:  # noqa: BLE001
                    continue
                if g is not None and not g.is_empty:
                    geoms.append(g)
        if geoms:
            merged = unary_union(geoms).intersection(frame.outline_m)
            raw[layer] = _to_mm(merged, frame)
    if sea_mm is not None and not sea_mm.is_empty and wanted["water"]:
        raw["water"] = unary_union([raw["water"], sea_mm]) if "water" in raw else sea_mm

    keepout = None
    if building_footprints:
        keepout = unary_union(building_footprints).buffer(0.02)
    out, taken = {}, keepout
    outline = frame.outline_mm.buffer(-0.01)            # stay inside the plate walls
    for layer in LAYER_ORDER:
        g = raw.get(layer)
        if g is None or g.is_empty:
            continue
        g = g.intersection(outline)
        if taken is not None:
            g = g.difference(taken)
        # drop specks and hairlines nobody could print
        g = g.buffer(-s.min_feature_mm / 4.0).buffer(s.min_feature_mm / 4.0)
        polys = [p for p in polygons_of(g) if p.area >= s.min_feature_mm ** 2]
        if not polys:
            continue
        g = unary_union(polys)
        out[layer] = g
        taken = g if taken is None else unary_union([taken, g.buffer(0.02)])
    return out


def drape(shape_mm, surface: TerrainSurface, raise_mm: float,
          sink_mm: float = SINK_MM) -> trimesh.Trimesh | None:
    """One closed solid per polygon, concatenated: top = terrain + raise,
    bottom = terrain - sink. Polygons are disjoint, so the result is a set of
    separate watertight shells."""
    tile = max(surface.cell, surface.cell_y) * 2.0
    meshes = []
    for poly in polygons_of(shape_mm):
        try:
            m = heightfield_solid(
                poly,
                lambda xy: surface.heights(xy) + raise_mm,
                lambda xy: np.maximum(surface.heights(xy) - sink_mm, 0.05),
                tile, origin=(surface.x0, surface.y0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("drape piece failed: %s", exc)
            m = None
        if m is not None and len(m.faces) >= 4:
            meshes.append(m)
    if not meshes:
        return None
    return meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
