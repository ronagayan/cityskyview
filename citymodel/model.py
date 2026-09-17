"""The built model, and how it is split into export components."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import trimesh

from .geo.frame import Frame
from .log import logger
from .mesh import validate
from .mesh.buildings import BuildingSolid
from .mesh.terrain import TerrainSurface
from .progress import NULL_REPORTER, Reporter
from .settings import ModelSettings


@dataclass
class Component:
    key: str                  # "terrain" | "buildings" | "selected" | "building:way/1" | "water" ...
    name: str                 # what the slicer shows
    kind: str                 # terrain | buildings | selected | water | roads | green
    mesh: trimesh.Trimesh
    color: tuple
    report: validate.MeshReport | None = None


@dataclass
class Model:
    frame: Frame
    surface: TerrainSurface
    terrain: trimesh.Trimesh
    buildings: list = field(default_factory=list)        # [BuildingSolid]
    layers: dict = field(default_factory=dict)           # {"roads": mesh, ...}
    info: dict = field(default_factory=dict)

    def building_ids(self) -> list:
        seen, out = set(), []
        for b in self.buildings:
            if b.bid not in seen:
                seen.add(b.bid)
                out.append(b.bid)
        return out

    def building_name(self, bid: str) -> str:
        for b in self.buildings:
            if b.bid == bid and b.name and not b.is_part:
                return b.name
        for b in self.buildings:
            if b.bid == bid and b.name:
                return b.name
        return ""

    @property
    def size_mm(self):
        lo, hi = self.terrain.bounds
        top = max([hi[2]] + [float(b.mesh.bounds[1][2]) for b in self.buildings])
        return float(hi[0] - lo[0]), float(hi[1] - lo[1]), float(top)


def slug(text: str, fallback: str = "part") -> str:
    t = re.sub(r"[^\w\-]+", "_", str(text), flags=re.UNICODE).strip("_")
    return t[:60] or fallback


def assemble_components(model: Model, selected_ids, settings: ModelSettings,
                        reporter: Reporter = NULL_REPORTER) -> list:
    """Split the model into the objects that get exported: terrain, the general
    building mass, the selected buildings (each on its own, or as one group),
    and the draped layers. Every component is unioned into a single manifold
    body where its pieces overlap, then validated and repaired."""
    s = settings
    selected = [b for b in model.building_ids() if b in set(selected_ids or [])]
    sel_set = set(selected)
    comps: list[Component] = []

    general = [b.mesh for b in model.buildings if b.bid not in sel_set]
    reporter.step("Merging buildings into one clean solid ...", 0.05)
    if general:
        mesh = validate.union(general, "buildings")
        if mesh is not None:
            comps.append(Component("buildings", "buildings", "buildings", mesh,
                                   s.color("buildings")))

    if selected:
        reporter.step("Preparing the selected buildings ...", 0.45)
        if s.selected_mode == "group":
            mesh = validate.union([b.mesh for b in model.buildings if b.bid in sel_set],
                                  "selected buildings")
            if mesh is not None:
                comps.append(Component("selected", "selected_buildings", "selected",
                                       mesh, s.color("selected")))
        else:
            used = set()
            for bid in selected:
                mesh = validate.union([b.mesh for b in model.buildings if b.bid == bid], bid)
                if mesh is None:
                    continue
                base = slug(model.building_name(bid) or bid.replace("/", "_"), "building")
                name, n = base, 2
                while name in used:
                    name, n = f"{base}_{n}", n + 1
                used.add(name)
                comps.append(Component(f"building:{bid}", name, "selected", mesh,
                                       s.color("selected")))

    for key in ("water", "roads", "green"):
        mesh = model.layers.get(key)
        if mesh is not None and len(mesh.faces):
            comps.append(Component(key, key, key, mesh.copy(), s.color(key)))

    reporter.step("Preparing the terrain ...", 0.6)
    terrain = model.terrain
    if s.cut_sockets:
        cutters = [c.mesh for c in comps if c.kind in ("buildings", "selected")]
        terrain = validate.difference(terrain, cutters)
    comps.insert(0, Component("terrain", "terrain", "terrain", terrain, s.color("terrain")))

    for i, c in enumerate(comps):
        reporter.step(f"Checking {c.name} ...", 0.65 + 0.35 * i / max(len(comps), 1), log=False)
        c.mesh, c.report = validate.validate_and_repair(c.mesh, c.name)
        logger.info("  %s", c.report.line())
    return comps
