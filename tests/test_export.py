"""3MF / STL export: components stay separate and survive a round trip."""

from __future__ import annotations

import zipfile
from dataclasses import replace

import numpy as np
import pytest
import trimesh

from citymodel import pipeline
from citymodel.export.threemf import read_3mf_summary


@pytest.fixture()
def exported(model, settings, osm_json, tmp_path):
    ids = osm_json["ids"]
    s = replace(settings, export_stl_folder=True)
    res = pipeline.export_model(model, [ids["levels"], ids["tower"]], s, tmp_path, "test city")
    return res, tmp_path


def test_3mf_has_one_named_object_per_component(exported):
    res, _ = exported
    path = next(p for p in res.files if p.suffix == ".3mf")
    summary = read_3mf_summary(path)
    names = [o["name"] for o in summary["objects"]]
    assert names == [c.name for c in res.components]
    assert {"terrain", "buildings", "Levels_House", "Tier_Tower", "water", "roads", "green"} <= set(names)
    assert summary["unit"] == "millimeter"
    # "parts" layout: a single build item, an assembly referencing every mesh
    assert len(summary["build_items"]) == 1
    assert sorted(summary["assembly"]) == sorted(o["id"] for o in summary["objects"])
    for o, c in zip(summary["objects"], res.components):
        assert o["triangles"] == len(c.mesh.faces)
        assert o["color"] is not None


def test_3mf_is_a_valid_package_and_loads_in_trimesh(exported):
    res, _ = exported
    path = next(p for p in res.files if p.suffix == ".3mf")
    with zipfile.ZipFile(path) as z:
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= set(z.namelist())
    scene = trimesh.load(path)
    geoms = list(scene.geometry.values())
    assert len(geoms) == len(res.components)
    for g in geoms:
        assert g.is_watertight
    total = sum(len(g.faces) for g in geoms)
    assert total == sum(len(c.mesh.faces) for c in res.components)
    # geometry survives at 0.1 micron precision
    size = np.ptp(np.vstack([g.vertices for g in geoms]), axis=0)
    assert max(size[:2]) == pytest.approx(150.0, abs=1e-3)


def test_objects_layout(model, settings, tmp_path):
    s = replace(settings, threemf_layout="objects")
    res = pipeline.export_model(model, [], s, tmp_path, "objs")
    summary = read_3mf_summary(res.files[0])
    assert summary["assembly"] == []
    assert len(summary["build_items"]) == len(summary["objects"]) == len(res.components)


def test_stl_folder_has_one_watertight_file_per_component(exported):
    res, tmp = exported
    stls = sorted((tmp / "test_city_stl").glob("*.stl"))
    assert len(stls) == len(res.components)
    for p in stls:
        m = trimesh.load(p, force="mesh")
        assert m.is_watertight, p.name


def test_unicode_building_names_survive(model, settings, tmp_path, osm_json):
    for b in model.buildings:
        if b.osm_id == osm_json["ids"]["big"]:
            b.name = "מגדל & <Tower>"
    res = pipeline.export_model(model, [osm_json["ids"]["big"]], settings, tmp_path, "uni")
    names = [o["name"] for o in read_3mf_summary(res.files[0])["objects"]]
    assert any("מגדל" in n for n in names)
