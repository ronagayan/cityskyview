"""
Verifies the geometry/export pipeline in city_map_generator.py using
hand-built sample data shaped exactly like real Overpass JSON output,
since this sandbox can't reach the live Overpass API.
"""

import os

import city_map_generator as gen

# Small synthetic bbox for the sample data below (few hundred meters).
gen.BBOX = (32.0870, 34.8090, 32.0920, 34.8160)

# Fake Overpass response: a few nodes + 4 building ways.
# One has an explicit height tag, one has building:levels, two have neither
# (to exercise the fallback heuristic), and one is a tiny sliver to test
# the min-area filter.
sample_osm_json = {
    "elements": [
        # --- Building A: explicit height tag ---
        {"type": "node", "id": 1, "lat": 32.0880, "lon": 34.8100},
        {"type": "node", "id": 2, "lat": 32.0880, "lon": 34.8103},
        {"type": "node", "id": 3, "lat": 32.0882, "lon": 34.8103},
        {"type": "node", "id": 4, "lat": 32.0882, "lon": 34.8100},
        {
            "type": "way", "id": 101,
            "nodes": [1, 2, 3, 4, 1],
            "tags": {"building": "yes", "height": "18 m"},
        },
        # --- Building B: building:levels tag ---
        {"type": "node", "id": 5, "lat": 32.0885, "lon": 34.8110},
        {"type": "node", "id": 6, "lat": 32.0885, "lon": 34.8113},
        {"type": "node", "id": 7, "lat": 32.0887, "lon": 34.8113},
        {"type": "node", "id": 8, "lat": 32.0887, "lon": 34.8110},
        {
            "type": "way", "id": 102,
            "nodes": [5, 6, 7, 8, 5],
            "tags": {"building": "residential", "building:levels": "5"},
        },
        # --- Building C: no height data -> small footprint fallback ---
        {"type": "node", "id": 9, "lat": 32.0890, "lon": 34.8120},
        {"type": "node", "id": 10, "lat": 32.0890, "lon": 34.81205},
        {"type": "node", "id": 11, "lat": 32.08905, "lon": 34.81205},
        {"type": "node", "id": 12, "lat": 32.08905, "lon": 34.8120},
        {
            "type": "way", "id": 103,
            "nodes": [9, 10, 11, 12, 9],
            "tags": {"building": "house"},
        },
        # --- Building D: no height data -> large footprint fallback ---
        {"type": "node", "id": 13, "lat": 32.0895, "lon": 34.8130},
        {"type": "node", "id": 14, "lat": 32.0895, "lon": 34.8140},
        {"type": "node", "id": 15, "lat": 32.0900, "lon": 34.8140},
        {"type": "node", "id": 16, "lat": 32.0900, "lon": 34.8130},
        {
            "type": "way", "id": 104,
            "nodes": [13, 14, 15, 16, 13],
            "tags": {"building": "commercial"},
        },
        # --- Building E: tiny sliver, should get filtered out ---
        {"type": "node", "id": 17, "lat": 32.0905, "lon": 34.8150},
        {"type": "node", "id": 18, "lat": 32.0905, "lon": 34.815001},
        {"type": "node", "id": 19, "lat": 32.090501, "lon": 34.815001},
        {"type": "node", "id": 20, "lat": 32.090501, "lon": 34.8150},
        {
            "type": "way", "id": 105,
            "nodes": [17, 18, 19, 20, 17],
            "tags": {"building": "yes"},
        },
    ]
}


def fake_fetch_buildings(bbox, config=None):
    return sample_osm_json


# Monkeypatch the network call so main() runs fully offline.
gen.fetch_buildings = fake_fetch_buildings
gen.OUTPUT_STL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "city_map_test.stl")

gen.main()

# --- Sanity checks on the resolve_height logic directly ---
print("\n--- resolve_height() checks ---")
h_a = gen.resolve_height({"height": "18 m"}, footprint_area_m2=50)
h_b = gen.resolve_height({"building:levels": "5"}, footprint_area_m2=50)
h_c = gen.resolve_height({}, footprint_area_m2=50)      # small -> 6m
h_d = gen.resolve_height({}, footprint_area_m2=1000)    # large -> 20m
print(f"explicit height tag (18 m)      -> {h_a} (expected 18.0)")
print(f"levels tag (5 * 3m)             -> {h_b} (expected 15.0)")
print(f"fallback, small footprint       -> {h_c} (expected 6.0)")
print(f"fallback, large footprint       -> {h_d} (expected 20.0)")

assert h_a == 18.0
assert h_b == 15.0
assert h_c == 6.0
assert h_d == 20.0
print("\nAll height-resolution checks passed.")
