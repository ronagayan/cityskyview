"""Check every component before it is written, repair what can be repaired,
and say plainly what could not."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import trimesh

from ..log import logger

try:
    import manifold3d as m3
    HAVE_MANIFOLD = True
except Exception:  # noqa: BLE001
    HAVE_MANIFOLD = False


@dataclass
class MeshReport:
    name: str
    vertices: int = 0
    faces: int = 0
    watertight: bool = False
    winding_consistent: bool = False
    positive_volume: bool = False
    degenerate_faces: int = 0
    open_edges: int = 0
    nonmanifold_edges: int = 0
    bodies: int = 0
    repaired: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (self.watertight and self.winding_consistent and self.positive_volume
                and self.degenerate_faces == 0 and self.nonmanifold_edges == 0)

    def problems(self) -> list:
        p = []
        if self.open_edges:
            p.append(f"{self.open_edges} open edges (holes)")
        if self.nonmanifold_edges:
            p.append(f"{self.nonmanifold_edges} non-manifold edges")
        if self.degenerate_faces:
            p.append(f"{self.degenerate_faces} zero-area triangles")
        if not self.winding_consistent:
            p.append("inconsistent winding")
        if not self.positive_volume:
            p.append("inside-out (negative volume)")
        return p

    def line(self) -> str:
        state = "OK" if self.ok else "PROBLEMS: " + "; ".join(self.problems())
        fixed = f"  [repaired: {', '.join(self.repaired)}]" if self.repaired else ""
        return (f"{self.name}: {self.faces:,} triangles, {self.bodies} "
                f"bod{'y' if self.bodies == 1 else 'ies'} -- {state}{fixed}")


def inspect(mesh: trimesh.Trimesh, name: str = "") -> MeshReport:
    rep = MeshReport(name, len(mesh.vertices), len(mesh.faces))
    if len(mesh.faces) == 0:
        return rep
    edges = np.sort(mesh.edges, axis=1)
    _u, counts = np.unique(edges, axis=0, return_counts=True)
    rep.open_edges = int((counts == 1).sum())
    rep.nonmanifold_edges = int((counts > 2).sum())
    rep.degenerate_faces = int((mesh.area_faces < 1e-12).sum())
    rep.watertight = bool(mesh.is_watertight)
    rep.winding_consistent = bool(mesh.is_winding_consistent)
    try:
        rep.positive_volume = bool(mesh.volume > 0)
    except Exception:  # noqa: BLE001
        rep.positive_volume = False
    try:
        rep.bodies = int(mesh.body_count)
    except Exception:  # noqa: BLE001
        rep.bodies = 0
    return rep


# ---------------------------------------------------------------------------
# manifold3d bridge
# ---------------------------------------------------------------------------

def to_manifold(mesh: trimesh.Trimesh):
    return m3.Manifold(m3.Mesh64(
        # plain copies: nanobind rejects trimesh's TrackedArray subclass
        vert_properties=np.array(mesh.vertices, dtype=np.float64, order="C"),
        tri_verts=np.array(mesh.faces, dtype=np.uint64, order="C")))


def from_manifold(man) -> trimesh.Trimesh:
    out = man.to_mesh64()
    return trimesh.Trimesh(vertices=np.asarray(out.vert_properties)[:, :3],
                           faces=np.asarray(out.tri_verts, dtype=np.int64),
                           process=False)


SLIVER_TOL_MM = 0.002      # 2 microns: far below anything a printer resolves


def _desliver(man):
    """Two walls that are *almost* coincident (neighbouring buildings whose
    shared nodes differ in the 5th decimal) leave micron-wide slivers in a
    union. They are valid topology but collapse to zero-area triangles the
    moment the mesh is written as 32-bit STL. manifold3d can remove them
    while keeping the solid manifold."""
    try:
        out = man.simplify(SLIVER_TOL_MM)
        return out if out.num_tri() else man
    except Exception:  # noqa: BLE001
        return man


def union(meshes: list, label: str = "") -> trimesh.Trimesh | None:
    """Boolean union of closed solids -> one manifold mesh. Overlapping or
    touching buildings become a single clean shell instead of a pile of
    intersecting ones. Inputs manifold3d rejects are repaired or left out
    (and counted) rather than sinking the whole component."""
    meshes = [m for m in meshes if m is not None and len(m.faces) >= 4]
    if not meshes:
        return None
    if not HAVE_MANIFOLD:
        logger.warning("manifold3d missing -- %s exported as overlapping shells", label)
        return trimesh.util.concatenate(meshes)
    good, rejected = [], 0
    for m in meshes:
        man = to_manifold(m)
        if man.status() != m3.Error.NoError or man.num_tri() == 0:
            fixed = basic_repair(m.copy())
            man = to_manifold(fixed)
        if man.status() != m3.Error.NoError or man.num_tri() == 0:
            rejected += 1
            continue
        good.append(man)
    if rejected:
        logger.warning("%s: %d solid(s) were not valid volumes and were left out",
                       label or "union", rejected)
    if not good:
        return None
    result = good[0] if len(good) == 1 else m3.Manifold.batch_boolean(good, m3.OpType.Add)
    return from_manifold(_desliver(result))


def difference(a: trimesh.Trimesh, cutters: list) -> trimesh.Trimesh:
    if not HAVE_MANIFOLD or not cutters:
        return a
    cut = [to_manifold(c) for c in cutters if c is not None and len(c.faces) >= 4]
    cut = [c for c in cut if c.status() == m3.Error.NoError]
    if not cut:
        return a
    tool = cut[0] if len(cut) == 1 else m3.Manifold.batch_boolean(cut, m3.OpType.Add)
    res = to_manifold(a) - tool
    return from_manifold(_desliver(res)) if res.num_tri() else a


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------

def basic_repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    try:
        trimesh.repair.fix_normals(mesh, multibody=True)
    except Exception:  # noqa: BLE001
        pass
    if not mesh.is_watertight:
        try:
            trimesh.repair.fill_holes(mesh)
        except Exception:  # noqa: BLE001
            pass
    return mesh


def validate_and_repair(mesh: trimesh.Trimesh, name: str):
    """``(mesh, MeshReport)`` -- the mesh is repaired in place where needed."""
    rep = inspect(mesh, name)
    if rep.ok:
        return mesh, rep
    before = rep.problems()
    mesh = basic_repair(mesh)
    rep2 = inspect(mesh, name)
    rep2.repaired.append("weld / drop degenerate + duplicate faces / fix normals / fill holes")
    if not rep2.ok and HAVE_MANIFOLD:
        # last resort: let manifold3d rebuild each shell as a proper volume
        try:
            shells = mesh.split(only_watertight=False)
            rebuilt = union(list(shells), name)
            if rebuilt is not None and len(rebuilt.faces):
                rep3 = inspect(rebuilt, name)
                if rep3.ok or len(rep3.problems()) < len(rep2.problems()):
                    rep3.repaired = rep2.repaired + ["rebuilt as a volume with manifold3d"]
                    mesh, rep2 = rebuilt, rep3
        except Exception as exc:  # noqa: BLE001
            logger.debug("manifold rebuild of %s failed: %s", name, exc)
    if rep2.ok:
        logger.info("repaired %s (was: %s)", name, "; ".join(before))
    else:
        logger.warning("%s still has problems after repair: %s", name,
                       "; ".join(rep2.problems()))
    return mesh, rep2
