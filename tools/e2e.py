"""End-to-end check against the live services, no UI:

    python tools/e2e.py [out_dir]

For each location: download (or reuse the cache), build, select three named
buildings, export 3MF + STL folder, then RE-OPEN the 3MF independently and
verify every component is there, separate, watertight and the right size.
Also renders a preview PNG so the result can be eyeballed.
"""

from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import trimesh  # noqa: E402

from citymodel import pipeline  # noqa: E402
from citymodel.export.threemf import read_3mf_summary  # noqa: E402
from citymodel.log import setup_logging  # noqa: E402
from citymodel.mesh import validate  # noqa: E402
from citymodel.render.scene import snapshot  # noqa: E402
from citymodel.settings import Area, ModelSettings  # noqa: E402

LOCATIONS = {
    # dense city, excellent OSM height coverage, 1 m lidar terrain
    "manhattan_midtown": dict(
        area=Area(bbox=(40.7480, -73.9920, 40.7580, -73.9790)),
        settings=ModelSettings(size_mm=180, green_enabled=True)),
    # strong relief (Mt Carmel drops ~280 m to the port) + a real city on it
    "haifa_carmel": dict(
        area=Area(bbox=(32.8050, 34.9800, 32.8180, 34.9980)),
        settings=ModelSettings(size_mm=180, green_enabled=True)),
    # pure relief, circular plate, terrain only: Matterhorn
    "matterhorn_circle": dict(
        area=Area(shape="circle", center=(45.9766, 7.6586), radius_m=2500.0,
                  bbox=(45.95, 7.62, 46.0, 7.70)),
        settings=ModelSettings(size_mm=150, buildings_enabled=False, roads_enabled=False,
                               water_enabled=False, terrain_quality="fine")),
}


def check(name, spec, out_dir) -> dict:
    area, s = spec["area"], spec["settings"]
    s.export_stl_folder = True
    t0 = time.time()
    data = pipeline.fetch_data(area, s)
    t_fetch = time.time() - t0
    t0 = time.time()
    model = pipeline.build_model(area, s, data)
    t_build = time.time() - t0

    named = [b for b in model.building_ids() if model.building_name(b)]
    tallest = sorted({b.bid: b for b in model.buildings if b.bid in named}.values(),
                     key=lambda b: -b.height_m)[:3]
    selected = [b.bid for b in tallest]

    t0 = time.time()
    res = pipeline.export_model(model, selected, s, out_dir, name)
    t_export = time.time() - t0
    path = next(p for p in res.files if p.suffix == ".3mf")

    # ---- independent verification of the file on disk ----------------------
    problems = []
    summary = read_3mf_summary(path)
    names = [o["name"] for o in summary["objects"]]
    expect = ["terrain"] + (["buildings"] if model.buildings else [])
    for want in expect + [k for k in ("water", "roads", "green") if k in model.layers]:
        if want not in names:
            problems.append(f"component {want!r} missing from the 3MF")
    n_sel = sum(1 for c in res.components if c.kind == "selected")
    if n_sel != len(selected):
        problems.append(f"{len(selected)} buildings selected but {n_sel} selected components")
    if len(summary["build_items"]) != 1 or sorted(summary["assembly"]) != sorted(o["id"] for o in summary["objects"]):
        problems.append("the assembly does not reference every component exactly once")
    scene = trimesh.load(path)
    geoms = list(scene.geometry.values())
    if len(geoms) != len(names):
        problems.append(f"trimesh sees {len(geoms)} meshes, expected {len(names)}")
    touching = 0
    for g in geoms:
        rep = validate.inspect(g)
        touching += rep.touching_edges
        if not rep.ok:
            problems.append(f"a re-loaded mesh ({len(g.faces)} faces) is not clean: {rep.problems()}")
    allv = np.vstack([g.vertices for g in geoms])
    size = np.ptp(allv, axis=0)
    if abs(max(size[:2]) - s.size_mm) > 0.05:
        problems.append(f"model is {max(size[:2]):.2f} mm, asked for {s.size_mm}")
    if allv[:, 2].min() < -1e-6:
        problems.append("geometry below z=0")
    # nothing floats: every building bottom is under the terrain surface
    floating = 0
    for b in model.buildings:
        if b.tags.get("min_height") or b.tags.get("building:min_level"):
            continue
        ring = np.asarray(b.footprint.exterior.coords)[:, :2]
        if b.mesh.bounds[0][2] >= model.surface.heights(ring).min():
            floating += 1
    if floating:
        problems.append(f"{floating} buildings float above the terrain")
    stl_dir = out_dir / f"{name}_stl" if hasattr(out_dir, "joinpath") else None

    png = os.path.join(out_dir, f"{name}.png")
    snapshot(model, s, png, selected)
    info = model.info
    return {
        "location": name, "ok": not problems, "problems": problems,
        "size_mm": [round(float(v), 2) for v in size], "scale": info["scale"],
        "elevation": f"{info['elevation_source']} ({info['elevation_kind']})",
        "relief_m": [round(info["terrain"]["elev_min_m"]), round(info["terrain"]["elev_max_m"])],
        "buildings": info.get("buildings", {}),
        "selected": [model.building_name(b) or b for b in selected],
        "touching_edges": touching,
        "components": {o["name"]: o["triangles"] for o in summary["objects"]},
        "seconds": {"fetch": round(t_fetch, 1), "build": round(t_build, 1), "export": round(t_export, 1)},
        "file": str(path), "file_mb": round(os.path.getsize(path) / 1e6, 1), "preview": png,
    }


def main():
    from pathlib import Path
    out = Path(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "output", "e2e"))
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(console=False)
    only = sys.argv[2:] or list(LOCATIONS)
    results = []
    for name in only:
        print(f"=== {name}", flush=True)
        try:
            r = check(name, LOCATIONS[name], out)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r = {"location": name, "ok": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        results.append(r)
        print(json.dumps(r, indent=1, ensure_ascii=False), flush=True)
    (out / "e2e_report.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), "utf-8")
    bad = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)}/{len(results)} locations passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
