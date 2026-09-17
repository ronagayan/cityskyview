"""Roof shapes from ``roof:shape`` -- ported from the original generator.

A roof is described by a profile f(xy) -> 0..1 over the footprint (0 at the
eaves, 1 at the ridge / apex); the building is then one height-field solid,
walls and roof together, so it stays a single closed body.
"""

from __future__ import annotations

import math

import numpy as np
import shapely
from shapely.geometry import Polygon

ROOF_ALIASES = {
    "pyramidal": "pyramidal", "pyramid": "pyramidal", "tented": "pyramidal",
    "cone": "cone", "conical": "cone", "spire": "cone",
    "dome": "dome", "sphere": "dome", "round": "dome", "onion": "onion",
    "gabled": "gabled", "gable": "gabled", "pitched": "gabled",
    "gambrel": "gabled", "saltbox": "gabled",
    "hipped": "hipped", "half-hipped": "hipped", "mansard": "hipped",
    "skillion": "skillion", "lean_to": "skillion", "shed": "skillion",
}
CURVED = ("dome", "onion", "cone")


def roof_shape(tags: dict) -> str | None:
    s = str(tags.get("roof:shape") or tags.get("building:roof:shape") or "")
    return ROOF_ALIASES.get(s.strip().lower())


def obb_axes(poly: Polygon):
    """(centre, long-axis unit vector, half-length, half-width) of the minimum
    rotated rectangle around ``poly``."""
    try:
        rect = poly.minimum_rotated_rectangle
        pts = np.asarray(rect.exterior.coords)[:4]
        e0, e1 = pts[1] - pts[0], pts[2] - pts[1]
        l0, l1 = float(np.linalg.norm(e0)), float(np.linalg.norm(e1))
        if l0 >= l1:
            v, ll, ww = e0 / max(l0, 1e-9), l0, l1
        else:
            v, ll, ww = e1 / max(l1, 1e-9), l1, l0
        return (np.asarray(rect.centroid.coords[0]), v,
                max(ll / 2.0, 1e-6), max(ww / 2.0, 1e-6))
    except Exception:  # noqa: BLE001
        c = np.asarray(poly.centroid.coords[0])
        return c, np.array([1.0, 0.0]), 1.0, 1.0


def roof_height_m(tags: dict, half_width_m: float, total_h_m: float,
                  level_height_m: float = 3.0) -> float:
    from .heights import parse_length_m  # noqa: PLC0415
    h = parse_length_m(tags.get("roof:height") or tags.get("building:roof:height"))
    if h is None:
        try:
            h = float(str(tags.get("roof:levels", "")).split()[0]) * level_height_m
        except (ValueError, IndexError):
            h = None
    if h is None or h <= 0:
        h = min(max(0.7 * half_width_m, 1.2), 8.0)
    return max(min(h, total_h_m * 0.75), 0.3)


def roof_profile(shape: str, poly: Polygon, tags: dict | None = None):
    """f(xy (N,2)) -> 0..1."""
    c, long_v, _half_l, half_w = obb_axes(poly)
    perp = np.array([-long_v[1], long_v[0]])
    ring = poly.exterior

    def edge_dist(xy):
        return shapely.distance(ring, shapely.points(np.asarray(xy, dtype=float)))

    if shape in ("pyramidal", "cone", "dome", "onion"):
        def f_radial(xy, _s=shape):
            d = edge_dist(xy)
            dmax = float(d.max()) if len(d) else 0.0
            t = d / dmax if dmax > 0 else d * 0.0
            if _s in ("pyramidal", "cone"):
                return t
            r = 1.0 - t
            if _s == "dome":
                return np.sqrt(np.clip(1.0 - r * r, 0.0, 1.0))
            return np.clip(1.0 - r ** 1.6, 0.0, 1.0) ** 0.6
        return f_radial
    if shape == "hipped":
        def f_hip(xy):
            d = edge_dist(xy)
            return np.clip(d / max(min(half_w, float(d.max())), 1e-6), 0.0, 1.0)
        return f_hip
    if shape == "skillion":
        u = long_v
        try:
            br = math.radians(float(str((tags or {}).get("roof:direction", "")).split()[0]))
            u = -np.array([math.sin(br), math.cos(br)])
        except (ValueError, IndexError):
            pass
        ring_xy = np.asarray(ring.coords)[:, :2]
        proj = (ring_xy - c) @ u
        lo, span = float(proj.min()), max(float(proj.max() - proj.min()), 1e-6)
        return lambda xy: np.clip(((np.asarray(xy) - c) @ u - lo) / span, 0.0, 1.0)

    # gabled and its look-alikes; roof:orientation=across turns the ridge
    across = str((tags or {}).get("roof:orientation", "")).lower() == "across"
    axis, half = (long_v, _half_l) if across else (perp, half_w)
    return lambda xy: np.clip(1.0 - np.abs((np.asarray(xy) - c) @ axis) / half, 0.0, 1.0)
