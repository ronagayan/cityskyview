"""Mesh pipeline: watertightness, scale, placement on terrain, components."""

from __future__ import annotations

import numpy as np
import pytest
import trimesh

from citymodel import pipeline
from citymodel.mesh import validate
from citymodel.mesh.primitives import conforming_triangulation, prism, solid_between
from citymodel.mesh.terrain import TerrainSurface, build_terrain_solid
from citymodel.model import assemble_components
from citymodel.settings import Area, ModelSettings
from conftest import BBOX, make_data
from shapely.geometry import Polygon, box


def assert_clean(mesh, name=""):
    rep = validate.inspect(mesh, name)
    assert rep.ok, f"{name}: {rep.problems()}"


# ---------------------------------------------------------------- primitives

def test_prism_with_hole_is_watertight_and_outward():
    ring = Polygon([(0, 0), (10, 0), (10, 8), (0, 8)], [[(3, 3), (7, 3), (7, 5), (3, 5)]])
    m = prism(ring, 1.0, 5.0)
    assert_clean(m, "prism")
    assert m.volume == pytest.approx((80 - 8) * 4.0, rel=1e-6)


def test_conforming_triangulation_has_no_t_junctions():
    poly = Polygon([(0, 0), (9.3, 0.4), (11, 6), (5, 9.7), (-1, 5)])
    v, f = conforming_triangulation(poly, tile=1.5)
    m = solid_between(v, f, np.full(len(v), 2.0), np.zeros(len(v)))
    assert_clean(m, "tiled prism")
    assert m.volume == pytest.approx(poly.area * 2.0, rel=1e-6)
    edge = np.linalg.norm(m.vertices[m.edges_unique[:, 0]] - m.vertices[m.edges_unique[:, 1]], axis=1)
    assert edge[np.abs(m.vertices[m.edges_unique[:, 0], 2]
                       - m.vertices[m.edges_unique[:, 1], 2]) < 1e-9].max() < 1.5 * 2 ** 0.5 + 1e-6


def test_terrain_solid_rectangle_and_circle():
    z = 2.0 + np.fromfunction(lambda j, i: np.sin(i / 5.0) + np.cos(j / 7.0), (40, 60))
    surf = TerrainSurface(0.0, 0.0, 1.0, z + 1.5)
    rect = build_terrain_solid(surf)
    assert_clean(rect, "rect terrain")
    assert rect.bounds[0][2] == 0.0                      # flat bottom on the bed
    from shapely.geometry import Point
    circ = build_terrain_solid(surf, Point(30, 20).buffer(15), rectangular=False)
    assert_clean(circ, "circle terrain")
    assert circ.bounds[1][0] - circ.bounds[0][0] == pytest.approx(30.0, abs=0.05)


def test_surface_evaluates_the_mesh_itself():
    rng = np.random.default_rng(1)
    z = 3.0 + rng.random((12, 15))
    surf = TerrainSurface(0.0, 0.0, 0.8, z, cell_y=0.9)
    mesh = build_terrain_solid(surf)
    # every top-facing triangle of the solid must lie exactly on the surface
    tri = mesh.triangles[mesh.face_normals[:, 2] > 1e-6]
    inner = tri.mean(axis=1)
    np.testing.assert_allclose(surf.heights(inner[:, :2]), inner[:, 2], atol=1e-9)
    off = tri[:, 0] * 0.6 + tri[:, 1] * 0.3 + tri[:, 2] * 0.1
    np.testing.assert_allclose(surf.heights(off[:, :2]), off[:, 2], atol=1e-9)


# --------------------------------------------------------------------- model

def test_scale_and_extent(model, settings):
    w, h, _z = model.size_mm
    assert max(w, h) == pytest.approx(settings.size_mm, abs=1e-6)
    real_w, real_h = model.frame.size_m
    assert 640 < real_w < 680 and 540 < real_h < 570          # metres, from the bbox
    assert w / real_w == pytest.approx(h / real_h, rel=1e-9)  # no anisotropic stretch
    assert model.terrain.bounds[0][2] == 0.0
    assert model.surface.z.min() >= settings.base_thickness_mm - 1e-9


def test_terrain_and_every_building_is_a_clean_solid(model):
    assert_clean(model.terrain, "terrain")
    assert len(model.buildings) >= 12
    for b in model.buildings:
        assert_clean(b.mesh, b.osm_id)


def test_no_building_floats_or_is_buried(model, settings):
    for b in model.buildings:
        if b.tags.get("min_height"):
            continue                                    # raised tiers sit on other parts
        ring = np.asarray(b.footprint.exterior.coords)[:, :2]
        ground = model.surface.heights(ring)
        bottom, top = b.mesh.bounds[0][2], b.mesh.bounds[1][2]
        assert bottom < ground.min() - 1e-6, f"{b.osm_id} floats above the ground"
        assert top >= ground.max() + settings.min_building_height_mm - 1e-6
        assert bottom > 0.0


def test_heights_follow_tags_and_scale(model, osm_json, settings):
    k = model.frame.scale * settings.vertical_exaggeration
    by_id = {b.osm_id: b for b in model.buildings}
    tagged = by_id[osm_json["ids"]["tagged"]]
    assert tagged.height_source == "tag" and tagged.height_m == 18.0
    lo = model.surface.heights(np.asarray(tagged.footprint.exterior.coords)[:, :2]).min()
    assert tagged.mesh.bounds[1][2] == pytest.approx(lo + 18.0 * k, abs=0.02)
    assert by_id[osm_json["ids"]["levels"]].height_source == "levels"
    assert by_id[osm_json["ids"]["plain"]].height_source == "estimate"
    assert osm_json["ids"]["sliver"] not in by_id                 # below min feature size
    assert osm_json["ids"]["tower"] not in by_id                  # replaced by its parts
    assert sum(1 for b in model.buildings if b.bid == osm_json["ids"]["tower"]) == 2


def test_roofs_change_the_shape(model, osm_json):
    by_id = {b.osm_id: b for b in model.buildings}
    gabled, dome = by_id[osm_json["ids"]["gabled"]], by_id[osm_json["ids"]["dome"]]
    for b in (gabled, dome):
        top = b.mesh.bounds[1][2]
        bottom = b.mesh.bounds[0][2]
        assert b.mesh.volume < b.footprint.area * (top - bottom) * 0.97   # not a box


def test_layers_are_disjoint_from_buildings(model):
    assert set(model.layers) == {"roads", "water", "green"}
    for mesh in model.layers.values():
        assert mesh.is_watertight and mesh.volume > 0


# ---------------------------------------------------------------- components

def test_components_union_into_manifold_bodies(model, settings, osm_json):
    ids = osm_json["ids"]
    comps = assemble_components(model, [ids["levels"], ids["tower"]], settings)
    names = [c.name for c in comps]
    assert names[0] == "terrain" and "buildings" in names
    assert "Levels_House" in names and "Tier_Tower" in names
    for c in comps:
        assert c.report.ok, f"{c.name}: {c.report.problems()}"
    general = next(c for c in comps if c.key == "buildings")
    # the two overlapping buildings and the shared-wall pair each became one body
    n_general = len({b.bid for b in model.buildings}) - 2
    assert general.report.bodies == n_general - 2
    tower = next(c for c in comps if c.name == "Tier_Tower")
    assert tower.report.bodies == 1                      # both tiers fused


def test_group_mode_and_sockets(model, settings, osm_json):
    from dataclasses import replace
    s = replace(settings, selected_mode="group", cut_sockets=True)
    comps = assemble_components(model, [osm_json["ids"]["levels"], osm_json["ids"]["big"]], s)
    assert [c.key for c in comps if c.kind == "selected"] == ["selected"]
    terrain = comps[0]
    assert terrain.report.ok
    assert terrain.mesh.volume < model.terrain.volume    # sockets were cut


def test_polygon_and_circle_areas(features, settings):
    ring = [(32.0872, 34.8095), (32.0875, 34.8155), (32.0916, 34.8150), (32.0905, 34.8100)]
    for area in (Area(shape="polygon", bbox=BBOX, ring=ring),
                 Area(shape="circle", bbox=BBOX, center=(32.0895, 34.8125), radius_m=200.0)):
        m = pipeline.build_model(area, settings, make_data(features))
        assert_clean(m.terrain, area.shape)
        assert max(m.size_mm[:2]) == pytest.approx(settings.size_mm, abs=0.05)
        for b in m.buildings:
            assert m.frame.outline_mm.buffer(0.01).contains(b.footprint)


def test_flat_model_without_terrain(features, area):
    s = ModelSettings(size_mm=100.0, terrain_enabled=False, roads_enabled=False,
                      water_enabled=False)
    data = pipeline.SourceData(bbox=BBOX, features=features, dem=None)
    m = pipeline.build_model(area, s, data)
    assert np.ptp(m.surface.z) == 0.0
    assert_clean(m.terrain, "flat")


def test_overture_heights_fill_only_what_osm_lacks(features, area, settings, osm_json):
    ids = osm_json["ids"]
    pts = [{"lat": 32.0891, "lon": 34.81215, "height": 23.0, "floors": 0, "source": "Microsoft"},   # "plain"
           {"lat": 32.08815, "lon": 34.8102, "height": 99.0, "floors": 0, "source": "Microsoft"}]   # "tagged"
    data = make_data(features)
    data.overture_points = pts
    m = pipeline.build_model(area, settings, data)
    by_id = {b.osm_id: b for b in m.buildings}
    assert by_id[ids["plain"]].height_source == "overture" and by_id[ids["plain"]].height_m == 23.0
    assert by_id[ids["tagged"]].height_m == 18.0            # an OSM tag always wins
