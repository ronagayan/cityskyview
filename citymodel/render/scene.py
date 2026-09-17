"""The 3D scene, shared by the interactive preview and offscreen snapshots.

Why this fixes the old preview: VTK rasterises on the GPU with a real depth
buffer (no painter's-algorithm sorting), the camera clipping range is fitted
to the model every frame (good depth precision), buildings get split normals
at hard edges (flat, crisp faces) while the terrain is smooth-shaded, and
nothing in the scene is coplanar (buildings pass *through* the terrain
surface; draped layers stand proud of it).
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

from ..model import Model
from ..settings import ModelSettings

BACKGROUND_TOP = "#2b3340"
BACKGROUND_BOTTOM = "#14171c"
EDGE_COLOR = "#1b1e24"


def _polydata(vertices, faces) -> pv.PolyData:
    faces = np.asarray(faces, dtype=np.int64)
    cells = np.column_stack([np.full(len(faces), 3, dtype=np.int64), faces]).ravel()
    return pv.PolyData(np.asarray(vertices, dtype=np.float64), cells)


def _shade(color, factor):
    return tuple(int(max(0, min(255, c * factor))) for c in color)


class ModelScene:
    """Owns the actors for one Model inside a pyvista plotter."""

    def __init__(self, plotter, settings: ModelSettings):
        self.pl = plotter
        self.settings = settings
        self.model: Model | None = None
        self.building_mesh: pv.PolyData | None = None
        self._bid_of_index: list = []
        self._index_of_bid: dict = {}
        self._cell_building: np.ndarray | None = None
        self._actors: dict = {}
        self._setup()

    # ------------------------------------------------------------------ look
    def _setup(self):
        pl = self.pl
        pl.set_background(BACKGROUND_BOTTOM, top=BACKGROUND_TOP)
        pl.remove_all_lights()
        # key (sun, upper left), a cool fill from the other side, a soft rim
        for pos, color, intensity in (((-1.0, -1.4, 2.2), "#fff4e6", 0.95),
                                      ((1.6, 0.6, 1.0), "#cfe0ff", 0.38),
                                      ((0.2, 1.8, 0.7), "#ffffff", 0.22)):
            light = pv.Light(position=pos, focal_point=(0, 0, 0), color=color,
                             intensity=intensity, light_type="scene light")
            light.positional = False
            pl.add_light(light)
        try:
            pl.enable_anti_aliasing("msaa", multi_samples=8)
        except Exception:  # noqa: BLE001
            pass
        self._apply_effects()

    def _apply_effects(self):
        pl = self.pl
        try:
            pl.disable_ssao()
        except Exception:  # noqa: BLE001
            pass
        if self.settings.preview_ssao and self.model is not None:
            w, h, _z = self.model.size_mm
            try:
                # SSAO replaces the multisampled pass, so pair it with FXAA
                pl.enable_ssao(radius=max(w, h) * 0.035, bias=max(w, h) * 0.0015,
                               kernel_size=192, blur=True)
                pl.enable_anti_aliasing("fxaa")
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------------- build
    def clear(self):
        for a in list(self._actors.values()):
            try:
                self.pl.remove_actor(a, render=False)
            except Exception:  # noqa: BLE001
                pass
        self._actors.clear()
        self.model = None
        self.building_mesh = None

    def show(self, model: Model, selected_ids=(), reset_camera: bool = True):
        self.clear()
        self.model = model
        s = self.settings
        mat = dict(ambient=0.30, diffuse=0.78, specular=0.06, specular_power=12)

        terrain = _polydata(model.terrain.vertices, model.terrain.faces)
        terrain = terrain.compute_normals(cell_normals=False, point_normals=True,
                                          split_vertices=True, feature_angle=55.0,
                                          auto_orient_normals=False)
        self._actors["terrain"] = self.pl.add_mesh(
            terrain, color=s.color("terrain"), smooth_shading=True, render=False,
            name="terrain", **mat)

        for key, mesh in model.layers.items():
            pd = _polydata(mesh.vertices, mesh.faces).compute_normals(
                cell_normals=False, point_normals=True, split_vertices=True,
                feature_angle=50.0, auto_orient_normals=False)
            self._actors[key] = self.pl.add_mesh(
                pd, color=s.color(key), smooth_shading=True, render=False, name=key,
                ambient=0.34, diffuse=0.72, specular=0.10 if key == "water" else 0.03,
                specular_power=20)

        if model.buildings:
            self._build_buildings(model, mat)
            self.set_selection(selected_ids, render=False)
        self._apply_effects()
        if reset_camera:
            self.reset_camera(render=False)
        self.pl.render()

    def _build_buildings(self, model: Model, mat: dict):
        self._bid_of_index = model.building_ids()
        self._index_of_bid = {b: i for i, b in enumerate(self._bid_of_index)}
        verts, faces, owner, n = [], [], [], 0
        for b in model.buildings:
            verts.append(b.mesh.vertices)
            faces.append(b.mesh.faces + n)
            owner.append(np.full(len(b.mesh.faces), self._index_of_bid[b.bid], dtype=np.int32))
            n += len(b.mesh.vertices)
        pd = _polydata(np.vstack(verts), np.vstack(faces))
        pd.cell_data["building"] = np.concatenate(owner)
        pd.cell_data["rgb"] = np.tile(np.array(self.settings.color("buildings"), dtype=np.uint8),
                                      (pd.n_cells, 1))
        # split normals at anything sharper than 30 deg: flat walls and roofs,
        # while domes and cones stay smooth
        pd = pd.compute_normals(cell_normals=False, point_normals=True,
                                split_vertices=True, feature_angle=30.0,
                                auto_orient_normals=False)
        self.building_mesh = pd
        self._cell_building = np.asarray(pd.cell_data["building"])
        self._actors["buildings"] = self.pl.add_mesh(
            pd, scalars="rgb", rgb=True, smooth_shading=True, render=False,
            name="buildings", **mat)
        if self.settings.preview_edges:
            edges = pd.extract_feature_edges(feature_angle=35.0, boundary_edges=False,
                                             non_manifold_edges=False, manifold_edges=False)
            if edges.n_cells:
                self._actors["edges"] = self.pl.add_mesh(
                    edges, color=EDGE_COLOR, line_width=1.2, opacity=0.55,
                    lighting=False, render=False, name="edges", pickable=False)

    # ------------------------------------------------------------- selection
    def set_selection(self, selected_ids, render: bool = True):
        if self.building_mesh is None:
            return
        base = np.array(self.settings.color("buildings"), dtype=np.uint8)
        sel = np.array(self.settings.color("selected"), dtype=np.uint8)
        idx = [self._index_of_bid[b] for b in (selected_ids or []) if b in self._index_of_bid]
        mask = np.isin(self._cell_building, idx)
        rgb = np.where(mask[:, None], sel[None, :], base[None, :]).astype(np.uint8)
        self.building_mesh.cell_data["rgb"] = rgb
        self.building_mesh.Modified()
        if render:
            self.pl.render()

    def building_at_cell(self, cell_id: int) -> str | None:
        if self._cell_building is None or not (0 <= cell_id < len(self._cell_building)):
            return None
        return self._bid_of_index[int(self._cell_building[cell_id])]

    def recolor(self, selected_ids=()):
        s = self.settings
        for key in ("terrain", "water", "roads", "green"):
            actor = self._actors.get(key)
            if actor is not None:
                actor.prop.color = s.color(key)
        self.set_selection(selected_ids)

    # ---------------------------------------------------------------- camera
    def reset_camera(self, render: bool = True):
        if self.model is None:
            return
        w, h, z = self.model.size_mm
        centre = (w / 2.0, h / 2.0, z * 0.35)
        d = max(w, h) * 1.45
        self.pl.camera.focal_point = centre
        self.pl.camera.position = (centre[0] - d * 0.55, centre[1] - d * 0.95, d * 0.85)
        self.pl.camera.up = (0, 0, 1)
        self.pl.camera.view_angle = 28.0
        self.pl.reset_camera_clipping_range()
        if render:
            self.pl.render()

    def top_view(self):
        if self.model is None:
            return
        w, h, _z = self.model.size_mm
        self.pl.camera.focal_point = (w / 2, h / 2, 0)
        self.pl.camera.position = (w / 2, h / 2, max(w, h) * 2.4)
        self.pl.camera.up = (0, 1, 0)
        self.pl.reset_camera_clipping_range()
        self.pl.render()


def snapshot(model: Model, settings: ModelSettings, path, selected_ids=(),
             size=(1600, 1100)):
    """Render the model offscreen to a PNG (used by tests and the e2e script)."""
    pl = pv.Plotter(off_screen=True, window_size=list(size), lighting="none")
    try:
        scene = ModelScene(pl, settings)
        scene.show(model, selected_ids)
        pl.screenshot(str(path))
    finally:
        pl.close()
    return path
