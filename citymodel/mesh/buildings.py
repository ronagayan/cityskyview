"""OSM footprints -> closed building solids standing in the terrain.

Placement rule (the fix for floating buildings): every solid starts *below*
the lowest ground anywhere under its footprint (by ``embed_mm``) and its top
is measured from that lowest ground point, which is how OSM defines
``height``. So on a slope the downhill wall is simply taller; nothing floats,
and no face is coplanar with the terrain.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
import trimesh
from shapely import affinity
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..geo.frame import Frame
from ..log import logger
from ..progress import NULL_REPORTER, Reporter
from ..settings import ModelSettings
from . import roofs
from .heights import (Height, NeighbourHeights, estimate_height, min_height_m,
                      tagged_height)
from .primitives import clean_polygon, heightfield_solid, polygons_of, prism
from .terrain import TerrainSurface


@dataclass
class BuildingSolid:
    bid: str                     # id used for selection (the parent building)
    osm_id: str                  # the feature this solid was built from
    name: str
    mesh: trimesh.Trimesh
    footprint: Polygon           # model mm
    height_m: float
    height_source: str
    is_part: bool = False
    tags: dict = field(default_factory=dict)


@dataclass
class _Rec:
    feat: object
    poly_m: Polygon
    is_part: bool
    bid: str = ""
    height: Height | None = None
    min_h: float = 0.0


def _feature_polygon_m(feat, frame: Frame):
    try:
        shell = frame.proj.ring_to_xy(feat.coords)
        holes = [frame.proj.ring_to_xy(h) for h in feat.holes if len(h) >= 4]
        poly = Polygon(shell, holes)
    except Exception:  # noqa: BLE001
        return None
    poly = clean_polygon(poly)
    if poly is None:
        return None
    if feat.holes:       # a relation's inner rings may belong to another outer
        poly = clean_polygon(poly)
    poly = poly.intersection(frame.outline_m)
    parts = [p for p in polygons_of(poly) if p.area > 0.5]
    if not parts:
        return None
    return max(parts, key=lambda g: g.area)


def _ground_range(poly_mm: Polygon, surface: TerrainSurface):
    """(lowest, highest) terrain under a footprint: outline densified to the
    grid pitch plus every grid node inside it."""
    ring = shapely.segmentize(poly_mm.exterior, max(surface.cell, 0.05))
    pts = [np.asarray(ring.coords)[:, :2]]
    minx, miny, maxx, maxy = poly_mm.bounds
    xs, ys = surface.node_xy()
    xs = xs[(xs > minx) & (xs < maxx)]
    ys = ys[(ys > miny) & (ys < maxy)]
    if len(xs) and len(ys) and len(xs) * len(ys) < 40_000:
        gx, gy = np.meshgrid(xs, ys)
        inside = shapely.contains_xy(poly_mm, gx.ravel(), gy.ravel())
        if inside.any():
            pts.append(np.column_stack([gx.ravel()[inside], gy.ravel()[inside]]))
    z = surface.heights(np.vstack(pts))
    return float(z.min()), float(z.max())


def build_buildings(features: dict, frame: Frame, surface: TerrainSurface,
                    settings: ModelSettings, overture_points: list | None = None,
                    reporter: Reporter = NULL_REPORTER):
    """Returns ``(solids, stats)``."""
    s = settings
    lvl_h = s.default_level_height_m
    stats = {"raw": len(features.get("buildings", [])), "built": 0,
             "too_small": 0, "failed": 0, "replaced_by_parts": 0,
             "height_sources": {"tag": 0, "levels": 0, "overture": 0, "estimate": 0}}

    recs: list[_Rec] = []
    for layer, is_part in (("buildings", False), ("building_parts", True)):
        for feat in features.get(layer, []):
            if feat.tags.get("location") == "underground" or feat.tags.get("layer", "0").startswith("-"):
                continue
            poly = _feature_polygon_m(feat, frame)
            if poly is not None:
                recs.append(_Rec(feat, poly, is_part, bid=feat.osm_id))
    stats["raw"] = sum(1 for r in recs if not r.is_part)      # footprints inside the area
    if not recs:
        return [], stats
    reporter.check()

    # ---- which building does each part belong to? ---------------------------
    b_recs = [r for r in recs if not r.is_part]
    p_recs = [r for r in recs if r.is_part]
    if b_recs and p_recs:
        tree = STRtree([r.poly_m for r in b_recs])
        covered = {}
        for pr in p_recs:
            pt = pr.poly_m.representative_point()
            for i in tree.query(pt, predicate="within"):
                parent = b_recs[int(i)]
                pr.bid = parent.bid
                covered.setdefault(parent.bid, []).append(pr.poly_m)
                break
        # a footprint whose parts describe it in full is replaced by them
        keep = []
        for br in b_recs:
            parts = covered.get(br.bid)
            if parts and unary_union(parts).intersection(br.poly_m).area >= 0.55 * br.poly_m.area:
                stats["replaced_by_parts"] += 1
                for pr in p_recs:                    # parts inherit what they lack
                    if pr.bid == br.bid:
                        pr.feat.tags = {**{k: v for k, v in br.feat.tags.items()
                                           if k in ("height", "building:levels", "name")},
                                        **pr.feat.tags}
                continue
            keep.append(br)
        b_recs = keep
    recs = b_recs + p_recs

    # ---- heights --------------------------------------------------------------
    for r in recs:
        r.height = tagged_height(r.feat.tags, lvl_h)
        r.min_h = min_height_m(r.feat.tags, lvl_h) if r.is_part else 0.0
    if overture_points:
        lat = np.array([p["lat"] for p in overture_points])
        lon = np.array([p["lon"] for p in overture_points])
        ox, oy = frame.proj.to_xy(lat, lon)
        need = [r for r in recs if r.height is None]
        if need:
            tree = STRtree([r.poly_m for r in need])
            pts = shapely.points(np.column_stack([ox, oy]))
            pi, ri = tree.query(pts, predicate="within")
            for p_idx, r_idx in zip(pi, ri):
                r = need[int(r_idx)]
                o = overture_points[int(p_idx)]
                h = o.get("height") or (o.get("floors") or 0) * lvl_h
                if r.height is None and h and 2.0 <= h <= 900.0:
                    r.height = Height(float(h), "overture")
    known = [r for r in recs if r.height is not None and not r.is_part]
    neigh = NeighbourHeights(
        np.array([[r.poly_m.centroid.x, r.poly_m.centroid.y] for r in known]).reshape(-1, 2),
        np.array([r.height.metres for r in known]))
    for r in recs:
        if r.height is None:
            c = r.poly_m.centroid
            r.height = estimate_height(r.feat.tags, r.poly_m.area,
                                       neigh.median_near(c.x, c.y))

    # ---- geometry ---------------------------------------------------------------
    k = frame.scale
    z_per_m = k * s.vertical_exaggeration * s.building_height_scale
    to_mm = [k, 0, 0, k, -frame.x_off * k, -frame.y_off * k]
    min_area = s.min_feature_mm ** 2
    ground_of_group: dict = {}
    prepared = []
    for r in recs:
        # a 1 micron grid: neighbours whose shared corners differ by float
        # noise end up with identical coordinates, so walls meet exactly
        poly = shapely.set_precision(affinity.affine_transform(r.poly_m, to_mm), 0.001)
        if s.min_feature_mm > 0:
            poly = poly.simplify(s.min_feature_mm / 6.0, preserve_topology=True)
            poly = clean_polygon(poly)
            if poly is not None and poly.geom_type != "Polygon":
                poly = max(polygons_of(poly), key=lambda g: g.area)
        if (poly is None or poly.area < min_area
                or (not r.is_part and poly.buffer(-s.min_feature_mm / 2.0).is_empty)):
            stats["too_small"] += 1
            continue
        lo, hi = _ground_range(poly, surface)
        g = ground_of_group.setdefault(r.bid, [lo, hi])
        g[0], g[1] = min(g[0], lo), max(g[1], hi)
        prepared.append((r, poly, lo, hi))

    solids = []
    for n, (r, poly, lo, hi) in enumerate(prepared):
        if n % 200 == 0:
            reporter.step(f"Buildings {n}/{len(prepared)} ...", n / max(len(prepared), 1),
                          log=False)
        g_lo, _g_hi = ground_of_group[r.bid]
        h_mm = r.height.metres * z_per_m
        z_top = g_lo + h_mm
        if r.min_h > 0 and r.min_h * z_per_m < h_mm - 0.05:
            z_bottom = g_lo + r.min_h * z_per_m           # a raised part (bridge, tier)
        else:
            z_bottom = max(lo - s.embed_mm, 0.05)
            z_top = max(z_top, hi + s.min_building_height_mm)   # never buried uphill
        mesh = None
        shape = roofs.roof_shape(r.feat.tags) if s.roof_shapes else None
        if shape is not None and z_top - max(hi, z_bottom) > 0.5:
            try:
                c, long_v, _hl, half_w = roofs.obb_axes(poly)
                rh = roofs.roof_height_m(r.feat.tags, half_w / k, r.height.metres, lvl_h) * z_per_m
                rh = min(rh, (z_top - max(hi, z_bottom)) * 0.75)
                if rh >= 0.15:
                    prof = roofs.roof_profile(shape, poly, r.feat.tags)
                    tile = max(half_w / (3.0 if shape in roofs.CURVED else 1.0), 0.25)
                    wall = z_top - rh
                    mesh = heightfield_solid(
                        poly, lambda xy, _p=prof, _w=wall, _r=rh: _w + _r * _p(xy),
                        lambda xy, _z=z_bottom: np.full(len(xy), _z), tile,
                        origin=(float(c[0]), float(c[1])),
                        rotate_deg=float(np.degrees(np.arctan2(long_v[1], long_v[0]))))
            except Exception as exc:  # noqa: BLE001
                logger.debug("roof %s on %s failed (%s) -- flat top", shape, r.feat.osm_id, exc)
                mesh = None
        if mesh is None:
            try:
                mesh = prism(poly, z_bottom, z_top)
            except Exception as exc:  # noqa: BLE001
                logger.debug("extrude %s failed (%s)", r.feat.osm_id, exc)
        if mesh is None or len(mesh.faces) < 4:
            stats["failed"] += 1
            continue
        stats["height_sources"][r.height.source] += 1
        solids.append(BuildingSolid(
            bid=r.bid, osm_id=r.feat.osm_id, name=r.feat.name, mesh=mesh,
            footprint=poly, height_m=r.height.metres, height_source=r.height.source,
            is_part=r.is_part, tags=r.feat.tags))
    stats["built"] = len(solids)
    logger.info("Buildings: %d solids from %d footprints (%d too small to print, "
                "%d replaced by building:part detail, %d failed); heights: %s",
                len(solids), stats["raw"], stats["too_small"],
                stats["replaced_by_parts"], stats["failed"], stats["height_sources"])
    return solids, stats
