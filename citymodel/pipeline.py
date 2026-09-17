"""fetch -> build -> export. Usable headless; the UI calls the same functions
from worker threads."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Polygon

from .data import elevation, overpass, overture
from .data.osm_parse import parse_features
from .export.stl import write_stl_folder
from .export.threemf import read_3mf_summary, write_3mf
from .geo.frame import area_bbox, make_frame
from .log import logger
from .mesh.buildings import build_buildings
from .mesh.layers import drape, layer_shapes_mm
from .mesh.primitives import clean_polygon
from .mesh.surface import make_surface
from .mesh.terrain import build_terrain_solid
from .model import Model, assemble_components, slug
from .progress import NULL_REPORTER, Reporter
from .settings import Area, ModelSettings


@dataclass
class SourceData:
    bbox: tuple
    features: dict = field(default_factory=dict)       # parsed OSM layers
    dem: elevation.DemResult | None = None
    overture_points: list = field(default_factory=list)


@dataclass
class ExportResult:
    files: list
    components: list
    summary_3mf: dict | None = None

    def report_text(self) -> str:
        lines = [c.report.line() for c in self.components if c.report is not None]
        bad = [c for c in self.components if c.report is not None and not c.report.ok]
        lines.append("All components are watertight and manifold." if not bad else
                     f"{len(bad)} component(s) still have problems -- see above.")
        return "\n".join(lines)


def wants_osm(settings: ModelSettings) -> bool:
    s = settings
    return s.buildings_enabled or s.roads_enabled or s.water_enabled or s.green_enabled


def fetch_osm_features(area: Area, reporter: Reporter = NULL_REPORTER) -> dict:
    """Just the map data -- what the map needs to draw clickable footprints."""
    return parse_features(overpass.fetch_osm(area_bbox(area), reporter=reporter))


def fetch_data(area: Area, settings: ModelSettings, reporter: Reporter = NULL_REPORTER,
               features: dict | None = None) -> SourceData:
    s = settings
    bbox = area_bbox(area)
    data = SourceData(bbox=bbox)
    if wants_osm(s):
        data.features = features if features is not None else fetch_osm_features(
            area, reporter.span(0.0, 0.45))
    if s.terrain_enabled:
        frame = make_frame(area, s.size_mm)
        target_res = max(s.cell_mm() / frame.scale, 0.5)
        data.dem = elevation.fetch_dem(bbox, target_res, prefer=s.elevation_source,
                                       opentopo_key=s.opentopo_key,
                                       reporter=reporter.span(0.45, 0.9))
    if s.use_overture_heights and s.buildings_enabled:
        data.overture_points = overture.fetch_heights(bbox, reporter=reporter.span(0.9, 1.0))
    return data


def _raw_shapes_mm(features, layer, frame):
    out = []
    for f in features.get(layer, []):
        if f.kind != "area":
            continue
        try:
            p = clean_polygon(Polygon(frame.latlon_to_mm(f.coords)))
        except Exception:  # noqa: BLE001
            continue
        if p is not None:
            out.append(p)
    return out


def build_model(area: Area, settings: ModelSettings, data: SourceData,
                reporter: Reporter = NULL_REPORTER) -> Model:
    s = settings
    t0 = time.monotonic()
    frame = make_frame(area, s.size_mm)
    w_m, h_m = frame.size_m
    logger.info("Model: %.0f x %.0f m -> %.1f x %.1f mm (1:%.0f)", w_m, h_m,
                *frame.size_mm, 1000.0 / frame.scale)

    reporter.step("Shaping the terrain ...", 0.02)
    feats = data.features or {}
    if data.dem is not None and s.terrain_enabled:
        dem, kind, native = data.dem.dem, data.dem.source.kind, data.dem.native_res_m
        src = data.dem.source
    else:
        dem, kind, native, src = elevation.FlatDem(), "DTM", 1.0, elevation.FLAT_INFO
    surface, sea = make_surface(
        frame, dem, kind, native, s,
        building_shapes_mm=_raw_shapes_mm(feats, "buildings", frame) if s.buildings_enabled else None,
        lake_shapes_mm=_raw_shapes_mm(feats, "water", frame) if s.water_enabled else None)
    reporter.step("Building the terrain solid ...", 0.15)
    terrain = build_terrain_solid(surface, frame.outline_mm, frame.rectangular)

    solids, stats = [], {}
    if s.buildings_enabled and feats:
        reporter.step("Building the buildings ...", 0.25)
        solids, stats = build_buildings(feats, frame, surface, s, data.overture_points,
                                        reporter.span(0.25, 0.7))

    layers = {}
    if feats and (s.roads_enabled or s.water_enabled or s.green_enabled):
        reporter.step("Draping roads / water / green over the terrain ...", 0.72)
        shapes = layer_shapes_mm(feats, frame, s, [b.footprint for b in solids], sea)
        for i, (key, shape) in enumerate(shapes.items()):
            reporter.step(f"Draping {key} ...", 0.72 + 0.08 * i, log=False)
            # water sits lowest, green a little higher, roads on top
            lift = s.layer_raise_mm * {"water": 0.6, "green": 0.8, "roads": 1.0}[key]
            mesh = drape(shape, surface, lift)
            if mesh is not None:
                layers[key] = mesh

    model = Model(frame, surface, terrain, solids, layers, info={
        "elevation_source": src.title, "elevation_kind": src.kind,
        "elevation_attribution": src.attribution,
        "elevation_notes": getattr(data.dem, "notes", "") if data.dem else "",
        "scale": f"1:{1000.0 / frame.scale:.0f}",
        "size_m": (w_m, h_m), "terrain": surface.meta, "buildings": stats,
        "build_seconds": round(time.monotonic() - t0, 1)})
    reporter.step("Model ready.", 1.0, log=False)
    logger.info("Model built in %.1fs: terrain %d triangles, %d building solids, layers: %s",
                time.monotonic() - t0, len(terrain.faces), len(solids),
                ", ".join(layers) or "none")
    return model


def default_model_name(area: Area) -> str:
    s, w, n, e = area_bbox(area)
    return f"model_{(s + n) / 2:.4f}_{(w + e) / 2:.4f}".replace("-", "m")


def export_model(model: Model, selected_ids, settings: ModelSettings, out_dir=None,
                 name: str = "", reporter: Reporter = NULL_REPORTER) -> ExportResult:
    s = settings
    out_dir = Path(out_dir or s.resolved_output_dir())
    name = slug(name or s.model_name or "city_model", "city_model")
    comps = assemble_components(model, selected_ids, s, reporter.span(0.0, 0.7))
    files, summary = [], None
    if s.export_3mf:
        reporter.step("Writing the 3MF ...", 0.75)
        p = write_3mf(out_dir / f"{name}.3mf", comps, s.threemf_layout, title=name,
                      metadata={"Description": f"Scale {model.info.get('scale', '')}. "
                                f"{model.info.get('elevation_attribution', '')} "
                                "Map data (c) OpenStreetMap contributors (ODbL)."})
        files.append(p)
        summary = read_3mf_summary(p)
        want = sum(1 for c in comps if len(c.mesh.faces) >= 4)
        if len(summary["objects"]) != want:
            raise RuntimeError(f"3MF verification failed: wrote {want} components but "
                               f"read back {len(summary['objects'])}")
    if s.export_stl_folder:
        reporter.step("Writing the STL files ...", 0.9)
        files += write_stl_folder(out_dir / f"{name}_stl", comps, name)
    if not files:
        raise ValueError("No export format is selected -- tick 3MF and/or STL folder.")
    reporter.step("Export finished.", 1.0, log=False)
    return ExportResult(files, comps, summary)
