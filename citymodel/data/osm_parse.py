"""Overpass JSON -> per-layer feature lists.

Moved over from the original ``city_map_generator.parse_features`` (ring
assembly for multipolygon relations, road widths by class) and trimmed to the
layers the new app models: buildings, building parts, roads, rail, water, green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..log import logger

LAYERS = ("buildings", "building_parts", "roads", "rail", "water", "green")

_RAIL = re.compile(r"^(rail|light_rail|subway|tram|narrow_gauge|monorail|funicular)$")

_ROAD_WIDTH_BY_CLASS = {
    "motorway": 14, "motorway_link": 8, "trunk": 12, "trunk_link": 7,
    "primary": 10, "primary_link": 6, "secondary": 8, "secondary_link": 5,
    "tertiary": 6, "tertiary_link": 4, "residential": 5, "unclassified": 5,
    "living_street": 4, "service": 3, "pedestrian": 4, "road": 5,
    "raceway": 6, "busway": 6,
    # not modelled: too thin to print and they clutter the plate
    "track": 0, "path": 0, "footway": 0, "cycleway": 0, "steps": 0,
    "bridleway": 0, "corridor": 0, "construction": 0, "proposed": 0,
    "platform": 0, "elevator": 0,
}


@dataclass
class Feature:
    kind: str                 # "area" | "line"
    layer: str
    coords: list              # outer ring / polyline as [(lat, lon), ...]
    osm_id: str               # "way/123" | "relation/456"
    tags: dict = field(default_factory=dict)
    holes: list = field(default_factory=list)
    width_m: float = 0.0      # line features

    @property
    def name(self) -> str:
        return self.tags.get("name") or self.tags.get("name:en") or ""


def _assemble_rings(way_node_lists):
    """Stitch node-id sequences into closed rings by shared endpoints."""
    segments = [list(w) for w in way_node_lists if len(w) >= 2]
    rings = []
    while segments:
        ring = segments.pop()
        progressed = True
        while ring[0] != ring[-1] and progressed:
            progressed = False
            for i, seg in enumerate(segments):
                if seg[0] == ring[-1]:
                    ring.extend(seg[1:])
                elif seg[-1] == ring[-1]:
                    ring.extend(reversed(seg[:-1]))
                elif seg[-1] == ring[0]:
                    ring[:0] = seg[:-1]
                elif seg[0] == ring[0]:
                    ring[:0] = list(reversed(seg[1:]))
                else:
                    continue
                segments.pop(i)
                progressed = True
                break
        if len(ring) >= 4 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def _zone_of(tags) -> str | None:
    lu, lz, nat = tags.get("landuse", ""), tags.get("leisure", ""), tags.get("natural", "")
    if nat == "water" or tags.get("waterway") in ("riverbank", "dock"):
        return "water"
    if (nat in ("wood", "scrub", "grassland", "heath")
            or lu in ("forest", "grass", "recreation_ground", "village_green",
                      "cemetery", "meadow")
            or lz in ("park", "garden", "pitch", "playground", "golf_course",
                      "recreation_ground")):
        return "green"
    return None


def _road_width(tags) -> float:
    base = _ROAD_WIDTH_BY_CLASS.get(tags.get("highway", ""), 5)
    if base == 0 or tags.get("tunnel") in ("yes", "building_passage"):
        return 0.0
    if tags.get("area") == "yes":
        return 0.0
    for key, mult in (("width", 1.0), ("lanes", 3.2)):
        if key in tags:
            try:
                v = float(str(tags[key]).replace(",", ".").split()[0]) * mult
                return max(v, 1.0 if key == "width" else base)
            except (ValueError, IndexError):
                pass
    return float(base)


def _is_part(tags) -> bool:
    return "building:part" in tags and tags.get("building:part") != "no"


def _is_building(tags) -> bool:
    return "building" in tags and tags.get("building") != "no"


def parse_features(osm_json: dict) -> dict:
    """``{layer: [Feature, ...]}`` for every layer in :data:`LAYERS`."""
    if not isinstance(osm_json, dict) or "elements" not in osm_json:
        raise ValueError("Overpass response has no 'elements' list to parse.")

    nodes, ways, relations = {}, {}, []
    for el in osm_json["elements"]:
        t = el.get("type")
        if t == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif t == "way":
            ways[el["id"]] = el
        elif t == "relation":
            relations.append(el)

    def to_coords(ids):
        return [nodes[i] for i in ids if i in nodes]

    out = {k: [] for k in LAYERS}

    # ---- relations first, so their member ways are not emitted twice -------
    rel_member_ways = set()

    def emit_relation(rel, layer):
        tags = rel.get("tags", {})
        outer_w, inner_w = [], []
        for m in rel.get("members", []):
            if m.get("type") != "way" or m.get("ref") not in ways:
                continue
            seq = ways[m["ref"]].get("nodes", [])
            (inner_w if m.get("role") == "inner" else outer_w).append(seq)
            # an inner ring that is a building in its own right (a chapel in a
            # courtyard) stays one; everything else is just this relation's outline
            if not (m.get("role") == "inner"
                    and _is_building(ways[m["ref"]].get("tags", {}))):
                rel_member_ways.add(m["ref"])
        outers = [r for r in (to_coords(r) for r in _assemble_rings(outer_w)) if len(r) >= 4]
        inners = [r for r in (to_coords(r) for r in _assemble_rings(inner_w)) if len(r) >= 4]
        for i, o in enumerate(outers):
            suffix = "" if len(outers) == 1 else f"#{i}"
            out[layer].append(Feature(
                "area", layer, o, f"relation/{rel['id']}{suffix}", tags,
                holes=inners))      # holes outside this outer are dropped on clip

    for rel in relations:
        rt = rel.get("tags", {})
        # type=building relations only group parts that are ways of their own
        if rt.get("type") not in (None, "multipolygon"):
            continue
        if _is_part(rt):
            emit_relation(rel, "building_parts")
        elif _is_building(rt):
            emit_relation(rel, "buildings")
        else:
            z = _zone_of(rt)
            if z:
                emit_relation(rel, z)

    # ---- ways ---------------------------------------------------------------
    for wid, way in ways.items():
        tags = way.get("tags") or {}
        if not tags:
            continue
        coords = to_coords(way.get("nodes", []))
        if len(coords) < 2:
            continue
        closed = len(coords) >= 4 and coords[0] == coords[-1]
        osm_id = f"way/{wid}"

        if _is_part(tags) or _is_building(tags):
            if wid in rel_member_ways or not closed:
                continue
            layer = "building_parts" if _is_part(tags) else "buildings"
            out[layer].append(Feature("area", layer, coords, osm_id, tags))
        elif "highway" in tags:
            w = _road_width(tags)
            if w > 0:
                out["roads"].append(Feature("line", "roads", coords, osm_id, tags,
                                            width_m=w))
        elif _RAIL.match(tags.get("railway", "")):
            if tags.get("tunnel") != "yes":
                out["rail"].append(Feature("line", "rail", coords, osm_id, tags,
                                           width_m=3.0))
        else:
            zone = _zone_of(tags)
            if zone and closed:
                if wid not in rel_member_ways:
                    out[zone].append(Feature("area", zone, coords, osm_id, tags))
            elif tags.get("waterway") in ("river", "canal", "stream"):
                if tags.get("tunnel") not in ("yes", "culvert"):
                    w = {"river": 14.0, "canal": 8.0, "stream": 3.0}[tags["waterway"]]
                    try:
                        w = max(float(str(tags.get("width", w)).split()[0]), 2.0)
                    except (ValueError, IndexError):
                        pass
                    out["water"].append(Feature("line", "water", coords, osm_id,
                                                tags, width_m=w))

    logger.info("Parsed OSM: %s",
                ", ".join(f"{k}={len(v)}" for k, v in out.items() if v) or "nothing")
    return out
