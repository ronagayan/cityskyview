"""The terrain: a regular height grid turned into a printable solid.

Top surface from the DEM, vertical walls down to a flat bottom at z = 0, so
the part stands on the print bed. For a non-rectangular area the rectangular
solid is cut to the outline with a manifold boolean, which keeps it closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import trimesh
from shapely.geometry import Polygon

from ..log import logger
from .primitives import polygons_of

MAX_CELLS = 420_000          # ~0.85 M top triangles; plenty for a print


@dataclass
class TerrainSurface:
    """Heights (mm) on a regular grid in model space, and exact evaluation of
    the triangulated surface built from it -- ``z(x, y)`` returns the height
    of the mesh itself, not of some smoother interpolant, so things placed
    with it sit exactly on the terrain."""
    x0: float
    y0: float
    cell: float
    z: np.ndarray                         # (ny, nx), row 0 = south
    cell_y: float = 0.0                   # 0 -> same as ``cell``
    elev_min_m: float = 0.0
    z_per_m: float = 1.0                  # mm of model height per real metre
    base_mm: float = 0.0
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.cell_y:
            self.cell_y = self.cell

    @property
    def shape(self):
        return self.z.shape

    def node_xy(self):
        ny, nx = self.z.shape
        return (self.x0 + np.arange(nx) * self.cell,
                self.y0 + np.arange(ny) * self.cell_y)

    def heights(self, xy) -> np.ndarray:
        xy = np.atleast_2d(np.asarray(xy, dtype=np.float64))
        ny, nx = self.z.shape
        fx = np.clip((xy[:, 0] - self.x0) / self.cell, 0.0, nx - 1 - 1e-9)
        fy = np.clip((xy[:, 1] - self.y0) / self.cell_y, 0.0, ny - 1 - 1e-9)
        i, j = fx.astype(np.int64), fy.astype(np.int64)
        u, v = fx - i, fy - j
        za, zb = self.z[j, i], self.z[j, i + 1]
        zc, zd = self.z[j + 1, i + 1], self.z[j + 1, i]
        # each cell is split along a-c, exactly as build_terrain_solid does
        lower = za + u * (zb - za) + v * (zc - zb)      # triangle a, b, c  (u >= v)
        upper = za + u * (zc - zd) + v * (zd - za)      # triangle a, c, d
        return np.where(u >= v, lower, upper)

    def height(self, x: float, y: float) -> float:
        return float(self.heights([[x, y]])[0])

    def elevation_to_z(self, elev_m):
        return self.base_mm + (np.asarray(elev_m) - self.elev_min_m) * self.z_per_m


def grid_for(bounds, cell_mm: float):
    """(x0, y0, cell_x, cell_y, nx, ny) covering ``bounds`` with square cells no larger
    than ``cell_mm``, coarsened if that would exceed MAX_CELLS."""
    minx, miny, maxx, maxy = bounds
    w, h = maxx - minx, maxy - miny
    cell = max(cell_mm, 1e-3)
    if (w / cell) * (h / cell) > MAX_CELLS:
        cell = math.sqrt(w * h / MAX_CELLS)
        logger.info("terrain: grid coarsened to %.2f mm cells to stay under %d cells",
                    cell, MAX_CELLS)
    nx = max(int(math.ceil(w / cell - 1e-9)), 2) + 1
    ny = max(int(math.ceil(h / cell - 1e-9)), 2) + 1
    # stretch the pitch a hair so the grid ends exactly on the far edges
    return minx, miny, w / (nx - 1), h / (ny - 1), nx, ny


def _rect_solid(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> trimesh.Trimesh:
    ny, nx = z.shape
    gx, gy = np.meshgrid(x, y)
    top = np.column_stack([gx.ravel(), gy.ravel(), z.ravel()])
    idx = np.arange(ny * nx).reshape(ny, nx)
    a, b = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel()
    c, d = idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    top_faces = np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])])

    # perimeter, counter-clockwise seen from above
    ring = np.concatenate([idx[0, :-1], idx[:-1, -1], idx[-1, :0:-1], idx[:0:-1, 0]])
    p = len(ring)
    bottom = top[ring].copy()
    bottom[:, 2] = 0.0
    nb = len(top)                                   # first bottom vertex
    centre = nb + p
    verts = np.vstack([top, bottom,
                       [[(x[0] + x[-1]) / 2.0, (y[0] + y[-1]) / 2.0, 0.0]]])
    k = np.arange(p)
    k1 = (k + 1) % p
    walls = np.vstack([np.column_stack([ring[k], nb + k, nb + k1]),
                       np.column_stack([ring[k], nb + k1, ring[k1]])])
    floor = np.column_stack([np.full(p, centre), nb + k1, nb + k])
    return trimesh.Trimesh(vertices=verts, faces=np.vstack([top_faces, walls, floor]),
                           process=False)


def build_terrain_solid(surface: TerrainSurface, outline: Polygon | None = None,
                        rectangular: bool = True) -> trimesh.Trimesh:
    """Closed terrain solid. ``outline`` (model mm) cuts it to a polygon or
    circle when the area is not the full grid rectangle."""
    x, y = surface.node_xy()
    solid = _rect_solid(x, y, surface.z)
    if rectangular or outline is None:
        return solid
    top = float(surface.z.max()) + 5.0
    parts = []
    for poly in polygons_of(outline):
        cutter = trimesh.creation.extrude_polygon(poly, height=top + 5.0)
        cutter.apply_translation([0, 0, -5.0])
        parts.append(cutter)
    cutter = parts[0] if len(parts) == 1 else trimesh.boolean.union(parts, engine="manifold")
    cut = trimesh.boolean.intersection([solid, cutter], engine="manifold")
    if cut is None or len(cut.faces) == 0:
        raise ValueError("cutting the terrain to the area outline produced nothing")
    return cut
