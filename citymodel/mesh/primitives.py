"""Building blocks for closed, consistently wound solids.

Everything here produces a mesh whose faces wind counter-clockwise seen from
outside, with shared (welded) vertices -- so each solid is watertight on its
own and the renderer gets correct normals without guessing.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np
import trimesh
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.validation import make_valid


def polygons_of(geom) -> list:
    """Every non-empty Polygon inside any shapely geometry."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if not g.is_empty]
    out = []
    for g in getattr(geom, "geoms", []):
        out += polygons_of(g)
    return out


def clean_polygon(poly):
    """A valid polygon (or multipolygon) or None."""
    if poly is None or poly.is_empty:
        return None
    if not poly.is_valid:
        poly = make_valid(poly)
        parts = polygons_of(poly)
        if not parts:
            return None
        poly = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    return None if poly.is_empty else poly


def triangulate(poly: Polygon):
    """(vertices (N,2), faces (M,3)) by ear clipping; faces wind CCW."""
    try:
        v, f = trimesh.creation.triangulate_polygon(poly, engine="earcut")
    except TypeError:
        v, f = trimesh.creation.triangulate_polygon(poly)
    v, f = np.asarray(v, dtype=np.float64), np.asarray(f, dtype=np.int64)
    if len(f) == 0:
        return v, f
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    f = f[np.abs(cross) > 1e-12]                   # drop zero-area ears
    cross = cross[np.abs(cross) > 1e-12]
    f[cross < 0] = f[cross < 0][:, ::-1]
    return v, f


def split_to_tiles(poly, tile: float, origin=(0.0, 0.0), _depth: int = 0) -> list:
    """Cut ``poly`` along a square grid of pitch ``tile`` (lines through
    ``origin``) and return the pieces. Bisecting recursively keeps this
    O(n log n) instead of clipping the whole shape once per tile. Pieces that
    share a grid line share its end points exactly, so triangulating each
    piece gives a conforming mesh once the vertices are welded."""
    out = []
    for p in polygons_of(poly):
        minx, miny, maxx, maxy = p.bounds
        w, h = maxx - minx, maxy - miny
        if (w <= tile * 1.0001 and h <= tile * 1.0001) or _depth > 40:
            out.append(p)
            continue
        ox, oy = origin
        if w >= h:
            k0 = math.floor((minx - ox) / tile) + 1
            k1 = math.ceil((maxx - ox) / tile) - 1
            if k1 < k0:
                out.append(p)
                continue
            cut = ox + ((k0 + k1) // 2) * tile
            halves = (box(minx - 1, miny - 1, cut, maxy + 1),
                      box(cut, miny - 1, maxx + 1, maxy + 1))
        else:
            k0 = math.floor((miny - oy) / tile) + 1
            k1 = math.ceil((maxy - oy) / tile) - 1
            if k1 < k0:
                out.append(p)
                continue
            cut = oy + ((k0 + k1) // 2) * tile
            halves = (box(minx - 1, miny - 1, maxx + 1, cut),
                      box(minx - 1, cut, maxx + 1, maxy + 1))
        for hb in halves:
            piece = p.intersection(hb)
            out += split_to_tiles(piece, tile, origin, _depth + 1)
    return [p for p in out if p.area > 1e-9]


def conforming_triangulation(poly, tile: float, origin=(0.0, 0.0)):
    """Triangulate ``poly`` so that no triangle is much larger than ``tile``:
    (vertices (N,2), faces (M,3)), welded, CCW."""
    vs, fs, n = [], [], 0
    for piece in split_to_tiles(poly, tile, origin):
        v, f = triangulate(piece)
        if len(f):
            vs.append(v)
            fs.append(f + n)
            n += len(v)
    if not vs:
        return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
    v, f = np.vstack(vs), np.vstack(fs)
    # weld: the same grid crossing computed for two neighbouring tiles
    key = np.round(v * 1e5).astype(np.int64)
    _u, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    v, f = v[first], inv.ravel()[f]
    ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    return v, f[ok]


def solid_between(v2: np.ndarray, faces: np.ndarray, z_top: np.ndarray,
                  z_bottom: np.ndarray) -> trimesh.Trimesh:
    """Closed solid from a 2D triangulation: a top sheet at ``z_top`` per
    vertex, a bottom sheet at ``z_bottom``, and walls along every boundary
    edge. The bottom must be strictly below the top everywhere."""
    n = len(v2)
    top = np.column_stack([v2, np.asarray(z_top, dtype=np.float64)])
    bot = np.column_stack([v2, np.asarray(z_bottom, dtype=np.float64)])
    directed = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    _u, inv, counts = np.unique(np.sort(directed, axis=1), axis=0,
                                return_inverse=True, return_counts=True)
    border = directed[counts[inv.ravel()] == 1]
    a, b = border[:, 0], border[:, 1]
    # the triangulation is CCW, so the outside lies to the right of a -> b
    walls = np.vstack([np.column_stack([a, a + n, b + n]),
                       np.column_stack([a, b + n, b])])
    all_faces = np.vstack([faces, faces[:, ::-1] + n, walls])
    mesh = trimesh.Trimesh(vertices=np.vstack([top, bot]), faces=all_faces,
                           process=False)
    mesh.merge_vertices()
    return mesh


def prism(poly: Polygon, z_bottom: float, z_top: float) -> trimesh.Trimesh | None:
    """Flat-topped extrusion of a footprint (holes respected)."""
    v, f = triangulate(poly)
    if len(f) == 0 or z_top - z_bottom <= 1e-6:
        return None
    return solid_between(v, f, np.full(len(v), z_top), np.full(len(v), z_bottom))


def heightfield_solid(poly, top_fn: Callable, bottom_fn: Callable, tile: float,
                      origin=(0.0, 0.0), rotate_deg: float = 0.0,
                      min_thickness: float = 0.05) -> trimesh.Trimesh | None:
    """Solid whose top follows ``top_fn(xy)`` and whose bottom follows
    ``bottom_fn(xy)``, meshed finely enough (``tile``) to show the shape.
    ``rotate_deg`` turns the meshing grid (about ``origin``) so a roof ridge
    can run along a grid line instead of across the triangles."""
    work = poly
    if rotate_deg:
        work = affinity.rotate(poly, -rotate_deg, origin=origin)
    v, f = conforming_triangulation(work, tile, origin)
    if len(f) == 0:
        return None
    if rotate_deg:
        a = math.radians(rotate_deg)
        c, s = math.cos(a), math.sin(a)
        d = v - np.asarray(origin)
        v = np.column_stack([c * d[:, 0] - s * d[:, 1],
                             s * d[:, 0] + c * d[:, 1]]) + np.asarray(origin)
    zt = np.asarray(top_fn(v), dtype=np.float64)
    zb = np.asarray(bottom_fn(v), dtype=np.float64)
    zt = np.maximum(zt, zb + min_thickness)
    return solid_between(v, f, zt, zb)
