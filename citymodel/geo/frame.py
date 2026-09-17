"""The model frame: lat/lon -> local metres -> millimetres on the print."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from shapely import affinity
from shapely.geometry import Point, Polygon, box

from ..settings import Area
from .projection import LocalProjection


@dataclass
class Frame:
    proj: LocalProjection
    outline_m: Polygon          # the area, local metres
    scale: float                # mm per metre
    rectangular: bool

    def __post_init__(self):
        self.x_off, self.y_off = self.outline_m.bounds[:2]
        self.outline_mm = affinity.affine_transform(
            self.outline_m, [self.scale, 0, 0, self.scale,
                             -self.x_off * self.scale, -self.y_off * self.scale])

    @property
    def size_mm(self):
        b = self.outline_mm.bounds
        return b[2] - b[0], b[3] - b[1]

    @property
    def size_m(self):
        b = self.outline_m.bounds
        return b[2] - b[0], b[3] - b[1]

    def latlon_to_mm(self, coords) -> np.ndarray:
        """[(lat, lon), ...] -> (N, 2) model millimetres."""
        xy = self.proj.ring_to_xy(coords)
        return (xy - [self.x_off, self.y_off]) * self.scale

    def mm_to_latlon(self, x_mm, y_mm):
        return self.proj.to_latlon(np.asarray(x_mm) / self.scale + self.x_off,
                                   np.asarray(y_mm) / self.scale + self.y_off)

    def mm_to_m(self, xy_mm) -> np.ndarray:
        return np.asarray(xy_mm) / self.scale + [self.x_off, self.y_off]


def area_outline_m(area: Area, proj: LocalProjection) -> tuple:
    """(polygon in local metres, is_rectangle)."""
    if area.shape == "circle" and area.center and area.radius_m > 0:
        cx, cy = proj.to_xy(area.center[0], area.center[1])
        return Point(float(cx), float(cy)).buffer(area.radius_m, quad_segs=24), False
    if area.shape == "polygon" and len(area.ring) >= 3:
        poly = Polygon(proj.ring_to_xy(area.ring))
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        return poly, False
    s, w, n, e = area.bbox
    # a lat/lon rectangle is very slightly trapezoidal in metres; use the
    # axis-aligned rectangle through the side mid-points
    xw, _ = proj.to_xy((s + n) / 2, w)
    xe, _ = proj.to_xy((s + n) / 2, e)
    _, ys = proj.to_xy(s, (w + e) / 2)
    _, yn = proj.to_xy(n, (w + e) / 2)
    return box(float(xw), float(ys), float(xe), float(yn)), True


def area_bbox(area: Area) -> tuple:
    """(south, west, north, east) enclosing the area, whatever its shape."""
    if area.shape == "circle" and area.center and area.radius_m > 0:
        proj = LocalProjection(area.center[0], area.center[1])
        r = area.radius_m
        lat, lon = proj.to_latlon(np.array([-r, r, 0, 0]), np.array([0, 0, -r, r]))
        return float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())
    if area.shape == "polygon" and len(area.ring) >= 3:
        la = [p[0] for p in area.ring]
        lo = [p[1] for p in area.ring]
        return min(la), min(lo), max(la), max(lo)
    return tuple(area.bbox)


def make_frame(area: Area, size_mm: float) -> Frame:
    bbox = area_bbox(area)
    proj = LocalProjection.for_bbox(bbox)
    outline, rect = area_outline_m(area, proj)
    minx, miny, maxx, maxy = outline.bounds
    longest = max(maxx - minx, maxy - miny)
    if longest <= 0:
        raise ValueError("The selected area has no size -- draw it again.")
    return Frame(proj, outline, float(size_mm) / longest, rect)
