"""
City Map Wall Art Generator
---------------------------
Fetches OpenStreetMap data (via the Overpass API) for a bounding box, builds a
layered 3D relief -- buildings, roads, rail, water, green space -- on a base
plate, optionally scales it to a physical print size / aspect ratio, and
exports STL / 3MF (with colour) / per-group STL.

Two ways to use it:

  * library -- ``from city_map_generator import run_pipeline, PipelineConfig``
    Progress + errors go through the ``city_map_generator`` logger, wired to
    the console and a rotating file (``city_map_generator.log``) by
    :func:`setup_logging`.

  * script  -- ``python city_map_generator.py`` runs headless against the
    constants below.

For the GUI (embedded non-closing log, map picker, highlights) run
``city_map_gui.py``.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import logging.handlers
import math
import os
import random
import re
import time
import zipfile
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Sequence

import numpy as np
import requests
import trimesh
from shapely import affinity as _affinity
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("city_map_generator")
LOG_FILENAME = "city_map_generator.log"

SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")


def log_success(msg, *args):
    logger.log(SUCCESS, msg, *args)


def setup_logging(logfile: str = LOG_FILENAME,
                  level: int = logging.INFO,
                  console: bool = True) -> logging.Logger:
    """Attach a rotating file handler (+ optional console handler) to the
    package logger. Idempotent."""
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    if not any(isinstance(h, logging.handlers.RotatingFileHandler)
               for h in logger.handlers):
        try:
            fh = logging.handlers.RotatingFileHandler(
                logfile, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.setLevel(logging.DEBUG)
            logger.addHandler(fh)
        except OSError as exc:  # pragma: no cover
            print(f"WARNING: could not open log file {logfile!r}: {exc}")

    if console and not any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in logger.handlers):
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(level)
        logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# Constants (defaults for the standalone script)
# ---------------------------------------------------------------------------

BBOX = (32.0870, 34.8090, 32.0920, 34.8160)  # a chunk of Tel Aviv

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

HEADERS = {
    "User-Agent": ("city-map-generator/1.2 (personal hobby project; "
                   "https://www.openstreetmap.org/)"),
    "Accept": "application/json",
}

BASE_THICKNESS_M = 3.0
MIN_FOOTPRINT_AREA_M2 = 4.0
HEIGHT_SCALE = 1.0
DEFAULT_LEVEL_HEIGHT_M = 3.0
FALLBACK_HEIGHT_BUCKETS = [(80.0, 6.0), (400.0, 12.0), (float("inf"), 20.0)]
OUTPUT_STL = "city_map.stl"

BASE_COLOR = (205, 205, 205)  # 3MF colour for everything that is not highlighted


class OverpassError(RuntimeError):
    """Raised when every Overpass mirror / retry attempt has been exhausted."""


# ---------------------------------------------------------------------------
# Detail levels -- how much geometry a landmark gets
# ---------------------------------------------------------------------------

DETAIL_LEVELS = (
    (1, "blocks",
     "flat extruded footprints -- fastest, smallest files"),
    (2, "full outlines",
     "+ landmarks keep every vertex and every tiny building:part"),
    (3, "roof shapes",
     "+ real roof geometry from roof:shape (pyramid, gable, hip, dome, cone)"),
    (4, "tapered tiers",
     "+ landmark tiers lofted into a continuous taper instead of steps"),
    (5, "custom model",
     "+ an imported STL replaces the landmark when one is given"),
)
DETAIL_LEVEL_LABELS = tuple(f"{n} - {nm}" for n, nm, _d in DETAIL_LEVELS)
DEFAULT_DETAIL_LEVEL = 4


def detail_level_of(value) -> int:
    """Accept 3, '3' or '3 - roof shapes' and give back a clamped int."""
    try:
        n = int(str(value).strip().split(" ")[0])
    except (TypeError, ValueError):
        n = DEFAULT_DETAIL_LEVEL
    return max(1, min(len(DETAIL_LEVELS), n))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class LayerStyle:
    """How one map layer is turned into geometry."""
    enabled: bool = True
    height_m: float = 1.0            # extrusion height, real-world metres (pre-scale)
    mode: str = "raised"            # "raised" (on top) | "engraved" (cut into base)
    width_m: float = 6.0            # line layers: default buffer width
    color: tuple = None             # (r,g,b) -> own coloured object in 3MF / split
                                    # STL; None -> merged into the grey base


# Land-cover / zone layers, in addition to buildings / roads / rail. By default
# every zone shares the same look (height 0.7, no colour) so the model reads as
# one surface; give a zone a colour in the GUI to split it out.
_ZONE_LAYERS = ("parks", "forest", "farmland", "urban", "water")


def _default_layers() -> dict:
    d = {
        "buildings": LayerStyle(True, 0.0, "raised"),   # height resolved per building
        "roads":     LayerStyle(True, 1.0, "raised", width_m=6.0),
        "rail":      LayerStyle(True, 1.2, "raised", width_m=3.0),
    }
    for z in _ZONE_LAYERS:
        d[z] = LayerStyle(True, 0.7, "raised")
    return d


@dataclass
class Highlight:
    """A group of features drawn taller / at a set height and, in 3MF, coloured."""
    name: str
    color: tuple = (224, 40, 40)          # (r, g, b) 0-255
    osm_ids: frozenset = frozenset()      # {"way/123", "relation/456"}
    polygons: tuple = ()                  # areas: tuple of tuple of (lat, lon)
    height_mode: str = "delta"           # "delta" (add) | "absolute" (set)
    height_m: float = 6.0
    raise_mm: float = 0.0                # lift whole group this many mm above base
    # detail level 5: use this mesh instead of the OSM geometry for the group
    model_path: str = ""                 # STL / OBJ / 3MF on disk
    model_fit: str = "height"            # height | footprint | stretch
    model_scale: float = 0.0             # multiplier on the fit; 0/1 -> as fitted
    model_rotate_deg: float = 0.0        # spin about Z after standing it up
    model_upright: str = "auto"          # which axis of the file points up;
                                         # "auto" works it out from the shape

    def as_shapely(self):
        out = []
        for ring in self.polygons:
            if len(ring) >= 3:
                try:
                    p = Polygon([(lo, la) for la, lo in ring])  # x=lon, y=lat
                    if not p.is_valid:
                        p = p.buffer(0)
                    if not p.is_empty:
                        out.append(p)
                except Exception:  # noqa: BLE001
                    pass
        return out


@dataclass
class PipelineConfig:
    bbox: Sequence[float]                              # (south, west, north, east)

    # base plate
    base_thickness_m: float = BASE_THICKNESS_M
    # ... but never thinner than this once it is scaled to print size. At
    # country scale a 3 m plate becomes 0.02 mm, which is not a plate at all.
    base_min_mm: float = 0.0

    # buildings
    min_footprint_area_m2: float = MIN_FOOTPRINT_AREA_M2
    height_scale: float = HEIGHT_SCALE
    default_level_height_m: float = DEFAULT_LEVEL_HEIGHT_M
    fallback_height_buckets: Sequence = tuple(FALLBACK_HEIGHT_BUCKETS)

    # layers
    layers: dict = field(default_factory=_default_layers)

    # physical sizing. Any of the three that is > 0 is hit exactly; setting both
    # width and depth also decides the aspect ratio, so nothing is stretched.
    target_width_mm: float = 0.0        # X. 0 -> keep 1 unit == 1 real metre
    target_depth_mm: float = 0.0        # Y. 0 -> follow the width / aspect ratio
    # "fill": W and D are exact, and the area is cropped to that shape.
    # "fit":  the model is scaled to fit *inside* W x D, keeping its own shape
    #         and cropping nothing -- what a whole country needs to reach a bed.
    size_mode: str = "fill"
    target_height_mm: float = 0.0       # total Z of the finished model, base included
    aspect_ratio: str = "free"          # "free" | "1:1" | "16:9" | "4:3" | "3:2" | "W:H"
    vertical_exaggeration: float = 1.0
    simplify_tolerance_m: float = 0.0   # >0 simplifies outlines (smaller files)

    # free-form plate outline: tuple of (lat, lon). When set, the base plate is
    # this shape (not the bbox rectangle) and everything is clipped to it;
    # aspect_ratio is ignored. cfg.bbox must still be the polygon's bounds.
    area_polygon: tuple = ()

    # buildings: use the real OSM height / building:levels tags, or one flat
    # height for every plain building (building:part detail is kept either way)
    use_osm_heights: bool = True
    uniform_building_height_m: float = 12.0

    # how much geometry landmarks get -- see DETAIL_LEVELS
    detail_level: int = DEFAULT_DETAIL_LEVEL

    # real terrain relief, from keyless AWS Terrarium tiles. Works on any plate
    # outline -- a captured rectangle or a country's own coastline.
    terrain_enabled: bool = False
    terrain_exaggeration: float = 1.5
    # ... or say how tall the highest ground should stand on the finished print
    # and the exaggeration is worked out for you. 0 = use terrain_exaggeration.
    terrain_relief_mm: float = 0.0
    # Everything below this is flattened to it. The elevation tiles carry ocean
    # bathymetry, and a country's official boundary usually reaches out over
    # its territorial waters -- without this the sea floor 1800 m down sets the
    # baseline and the land itself is left almost flat. 0 = ordinary sea level;
    # set it to -450 to keep the Dead Sea (or below every low point to keep
    # real depths).
    terrain_sea_level_m: float = 0.0
    terrain_samples: int = 96

    # a path joining the landmarks, in the order the ids are listed here
    path_enabled: bool = False
    path_osm_ids: tuple = ()
    path_width_m: float = 8.0
    path_height_m: float = 1.2
    path_height_mm: float = 0.0        # >0 wins: how tall it stands on the print
    path_color: tuple = (240, 168, 64)
    path_closed: bool = False          # join the last stop back to the first
    path_style: str = "straight"       # straight | curved

    # highlights
    highlights: tuple = ()

    # output
    output_stl: str = OUTPUT_STL
    export_stl: bool = True
    export_3mf: bool = False
    export_split: bool = False          # one STL per highlight group + base

    # network
    overpass_urls: Sequence[str] = tuple(OVERPASS_URLS)
    request_timeout_s: float = 90.0
    max_attempts_per_mirror: int = 3
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 20.0
    overpass_query_timeout_s: int = 90


@dataclass
class PipelineResult:
    output_path: str                 # primary file (the STL, or first written)
    output_paths: list               # everything written
    building_count: int
    skipped_count: int
    raw_building_count: int
    is_watertight: bool
    bounds: list
    origin: tuple
    size_mm: tuple = (0.0, 0.0, 0.0)
    layer_counts: dict = field(default_factory=dict)
    highlight_counts: dict = field(default_factory=dict)


def _module_default_config() -> PipelineConfig:
    return PipelineConfig(
        bbox=tuple(BBOX),
        base_thickness_m=BASE_THICKNESS_M,
        min_footprint_area_m2=MIN_FOOTPRINT_AREA_M2,
        height_scale=HEIGHT_SCALE,
        default_level_height_m=DEFAULT_LEVEL_HEIGHT_M,
        fallback_height_buckets=tuple(FALLBACK_HEIGHT_BUCKETS),
        output_stl=OUTPUT_STL,
        overpass_urls=tuple(OVERPASS_URLS),
    )


# ---------------------------------------------------------------------------
# Overpass fetch (retry + backoff + mirrors)
# ---------------------------------------------------------------------------

_LAYER_QUERY = {
    "buildings": ['way["building"]({b});', 'relation["building"]({b});',
                  'way["building:part"]({b});', 'relation["building:part"]({b});'],
    "roads":     ['way["highway"]({b});'],
    "rail":      ['way["railway"~"^(rail|light_rail|subway|tram|narrow_gauge|'
                  'monorail|funicular)$"]({b});'],
    "water":     ['way["natural"="water"]({b});',
                  'relation["natural"="water"]({b});',
                  'way["waterway"~"^(riverbank)$"]({b});',
                  'way["waterway"~"^(river|canal|stream)$"]({b});'],
    "parks":     ['way["leisure"~"^(park|garden|pitch|playground|golf_course|'
                  'recreation_ground)$"]({b});',
                  'way["landuse"~"^(grass|recreation_ground|village_green|'
                  'cemetery)$"]({b});',
                  'way["natural"~"^(scrub|grassland|heath)$"]({b});'],
    "forest":    ['way["natural"="wood"]({b});',
                  'way["landuse"="forest"]({b});',
                  'relation["natural"="wood"]({b});',
                  'relation["landuse"="forest"]({b});'],
    "farmland":  ['way["landuse"~"^(farmland|meadow|orchard|vineyard|'
                  'allotments|greenhouse_horticulture)$"]({b});'],
    "urban":     ['way["landuse"~"^(residential|commercial|retail|industrial|'
                  'railway|construction)$"]({b});',
                  'relation["landuse"~"^(residential|commercial|retail|'
                  'industrial)$"]({b});'],
}


def build_overpass_query(bbox, cfg: PipelineConfig) -> str:
    south, west, north, east = bbox
    b = f"{south},{west},{north},{east}"
    frags = []
    for name, style in cfg.layers.items():
        if style.enabled and name in _LAYER_QUERY:
            frags += [f.replace("{b}", b) for f in _LAYER_QUERY[name]]
    if not frags:  # never send an empty query
        frags = [f.replace("{b}", b) for f in _LAYER_QUERY["buildings"]]
    body = "\n".join("  " + f for f in frags)
    return (f"[out:json][timeout:{int(cfg.overpass_query_timeout_s)}];\n"
            f"(\n{body}\n);\nout body;\n>;\nout skel qt;\n")


def _http_status_hint(code: int) -> str:
    return {
        400: "bad query syntax",
        403: "forbidden -- this mirror may be blocking the client",
        406: "not acceptable -- User-Agent header likely rejected",
        429: "rate limited -- too many requests to this mirror",
        502: "bad gateway -- mirror is having trouble",
        503: "service unavailable -- mirror overloaded",
        504: "gateway timeout -- Overpass overloaded or query too heavy",
    }.get(code, "")


def _sleep_with_backoff(attempt: int, cfg: PipelineConfig,
                        retry_after: Optional[float] = None) -> None:
    if retry_after is not None:
        delay = min(float(retry_after), cfg.backoff_cap_s)
    else:
        delay = min(cfg.backoff_cap_s, cfg.backoff_base_s * (2 ** (attempt - 1)))
    delay += random.uniform(0.0, 0.5)
    logger.info("    waiting %.1fs before next attempt ...", delay)
    time.sleep(delay)


def _overpass_request(query: str, cfg: PipelineConfig) -> dict:
    """POST ``query`` to each mirror in turn, retrying with exponential backoff.
    Handles every failure mode explicitly and logs it; raises
    :class:`OverpassError` with a full summary only when nothing worked."""
    attempts_log = []
    urls = list(cfg.overpass_urls)

    for url_i, url in enumerate(urls):
        for attempt in range(1, cfg.max_attempts_per_mirror + 1):
            label = f"{url}  (attempt {attempt}/{cfg.max_attempts_per_mirror})"
            logger.info("  Overpass POST -> %s", label)
            t0 = time.monotonic()
            is_final = (url_i == len(urls) - 1
                        and attempt == cfg.max_attempts_per_mirror)

            def backoff(retry_after=None):
                if not is_final:
                    _sleep_with_backoff(attempt, cfg, retry_after=retry_after)

            try:
                resp = requests.post(url, data={"data": query}, headers=HEADERS,
                                     timeout=cfg.request_timeout_s)
            except requests.exceptions.Timeout:
                msg = f"timed out after {cfg.request_timeout_s:.0f}s"
                logger.warning("  %s -> %s", label, msg)
                attempts_log.append(f"{label}: {msg}")
                backoff()
                continue
            except requests.exceptions.ConnectionError as exc:
                msg = f"connection error ({exc.__class__.__name__})"
                logger.warning("  %s -> %s: %s", label, msg, exc)
                attempts_log.append(f"{label}: {msg}")
                backoff()
                continue
            except requests.exceptions.RequestException as exc:
                msg = f"request failed ({exc.__class__.__name__}: {exc})"
                logger.warning("  %s -> %s", label, msg)
                attempts_log.append(f"{label}: {msg}")
                backoff()
                continue

            elapsed = time.monotonic() - t0
            logger.info("  %s -> HTTP %s, %d bytes, %.1fs",
                        label, resp.status_code, len(resp.content), elapsed)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    snippet = resp.text[:300].replace("\n", " ").strip()
                    msg = f"200 OK but body is not valid JSON ({exc}); starts: {snippet!r}"
                    logger.warning("  %s -> %s", label, msg)
                    attempts_log.append(f"{label}: {msg}")
                    backoff()
                    continue
                if not isinstance(data, dict) or "elements" not in data:
                    keys = list(data)[:6] if isinstance(data, dict) else type(data)
                    msg = f"JSON has no 'elements' list (got: {keys})"
                    logger.warning("  %s -> %s", label, msg)
                    attempts_log.append(f"{label}: {msg}")
                    backoff()
                    continue
                remark = str(data.get("remark", ""))
                if remark and ("error" in remark.lower()
                               or "timed out" in remark.lower()):
                    msg = f"Overpass error remark: {remark!r}"
                    logger.warning("  %s -> %s", label, msg)
                    attempts_log.append(f"{label}: {msg}")
                    backoff()
                    continue
                if remark:
                    logger.info("  Overpass remark: %s", remark)
                logger.info("  %s -> OK, %d OSM elements received",
                            label, len(data["elements"]))
                return data

            snippet = resp.text[:300].replace("\n", " ").strip()
            hint = _http_status_hint(resp.status_code)
            msg = (f"HTTP {resp.status_code}"
                   f"{' -- ' + hint if hint else ''}; body: {snippet!r}")
            logger.warning("  %s -> %s", label, msg)
            attempts_log.append(f"{label}: {msg}")
            retry_after = None
            if resp.status_code in (429, 503, 504):
                ra = resp.headers.get("Retry-After", "")
                if ra.strip().isdigit():
                    retry_after = float(ra.strip())
            backoff(retry_after=retry_after)
            continue

    summary = "; ".join(attempts_log) if attempts_log else "no attempts made"
    raise OverpassError("All Overpass mirrors and retries failed.\n  "
                        + summary.replace("; ", ";\n  "))


def fetch_map_data(bbox, config: Optional[PipelineConfig] = None) -> dict:
    """Query Overpass for every enabled layer in ``config`` within ``bbox``."""
    cfg = config or _module_default_config()
    if bbox is not None:
        cfg = replace(cfg, bbox=tuple(bbox))
    query = build_overpass_query(cfg.bbox, cfg)
    logger.info("Overpass query: %d line(s), bbox S=%s W=%s N=%s E=%s",
                query.count("\n  "), *cfg.bbox)
    return _overpass_request(query, cfg)


def fetch_buildings(bbox, config: Optional[PipelineConfig] = None) -> dict:
    """Backwards-compatible alias for :func:`fetch_map_data` (kept so existing
    callers and tests that monkeypatch this name keep working)."""
    return fetch_map_data(bbox, config)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

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


_RAIL_MATCH = re.compile(
    r"^(rail|light_rail|subway|tram|narrow_gauge|monorail|funicular)$")
_WATERWAY_MATCH = re.compile(r"^(river|canal|stream|riverbank)$")


def _zone_of(tags):
    """Which land-cover zone a feature's tags belong to (or None)."""
    lu, lz, nat = tags.get("landuse", ""), tags.get("leisure", ""), tags.get("natural", "")
    if nat == "water" or tags.get("waterway") == "riverbank":
        return "water"
    if nat == "wood" or lu == "forest":
        return "forest"
    if lu in ("farmland", "meadow", "orchard", "vineyard", "allotments",
              "greenhouse_horticulture"):
        return "farmland"
    if lu in ("residential", "commercial", "retail", "industrial", "railway",
              "construction"):
        return "urban"
    if (lz in ("park", "garden", "pitch", "playground", "golf_course",
               "recreation_ground")
            or lu in ("grass", "recreation_ground", "village_green", "cemetery")
            or nat in ("scrub", "grassland", "heath")):
        return "parks"
    return None

_ROAD_WIDTH_BY_CLASS = {
    "motorway": 14, "motorway_link": 8, "trunk": 12, "trunk_link": 7,
    "primary": 10, "primary_link": 6, "secondary": 8, "secondary_link": 5,
    "tertiary": 6, "tertiary_link": 4, "residential": 5, "unclassified": 5,
    "living_street": 4, "service": 3, "pedestrian": 4, "road": 5,
    "track": 0, "path": 0, "footway": 0, "cycleway": 0, "steps": 0,
    "bridleway": 0, "corridor": 0, "construction": 0, "proposed": 0,
    "raceway": 6, "busway": 6,
}


def _road_width(tags, cfg: PipelineConfig) -> float:
    hw = tags.get("highway", "")
    base = _ROAD_WIDTH_BY_CLASS.get(hw)
    if base is None:
        base = cfg.layers.get("roads", LayerStyle()).width_m or 5.0
    if base == 0:
        return 0.0
    if "width" in tags:
        try:
            return max(float(str(tags["width"]).split()[0]), 1.0)
        except ValueError:
            pass
    if "lanes" in tags:
        try:
            return max(float(tags["lanes"]) * 3.2, base)
        except ValueError:
            pass
    return float(base)


def parse_features(osm_json: dict, cfg: Optional[PipelineConfig] = None) -> dict:
    """Parse Overpass JSON into per-layer feature lists.

    Returns ``{"buildings": [...], "roads": [...], "rail": [...],
    "water": [...], "green": [...]}`` where each feature is a dict with:
      kind   -- "area" or "line"
      layer  -- layer name
      coords -- outer ring / polyline as [(lat, lon), ...]
      holes  -- inner rings (areas only)
      tags   -- OSM tags
      osm    -- "way/<id>" or "relation/<id>"
      width_m-- buffer width (line features)
    """
    cfg = cfg or _module_default_config()
    if not isinstance(osm_json, dict) or "elements" not in osm_json:
        raise ValueError("Overpass response has no 'elements' list to parse "
                         f"(got {type(osm_json).__name__}).")

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

    out = {k: [] for k in (("buildings", "building_parts", "roads", "rail")
                           + _ZONE_LAYERS)}

    def _is_part(t):
        return "building:part" in t and t.get("building:part") != "no"

    # ---- relations first (so we can skip their member ways) --------------
    rel_member_ways = set()
    for rel in relations:
        rt = rel.get("tags", {})
        if ("building" in rt or _is_part(rt) or _zone_of(rt)):
            for m in rel.get("members", []):
                if m.get("type") == "way":
                    rel_member_ways.add(m.get("ref"))

    def _emit_relation_area(rel, layer):
        tags = rel.get("tags", {})
        outer_w, inner_w = [], []
        for m in rel.get("members", []):
            if m.get("type") != "way" or m.get("ref") not in ways:
                continue
            seq = ways[m["ref"]].get("nodes", [])
            (inner_w if m.get("role") == "inner" else outer_w).append(seq)
        outers = [to_coords(r) for r in _assemble_rings(outer_w)]
        inners = [to_coords(r) for r in _assemble_rings(inner_w)]
        outers = [r for r in outers if len(r) >= 4]
        inners = [r for r in inners if len(r) >= 4]
        for o in outers:
            out[layer].append({
                "kind": "area", "layer": layer, "coords": o,
                "holes": inners if len(outers) == 1 else [],
                "tags": tags, "osm": f"relation/{rel['id']}"})

    for rel in relations:
        rt = rel.get("tags", {})
        if _is_part(rt):
            _emit_relation_area(rel, "building_parts")
        elif "building" in rt:
            _emit_relation_area(rel, "buildings")
        else:
            z = _zone_of(rt)
            if z and cfg.layers.get(z, LayerStyle()).enabled:
                _emit_relation_area(rel, z)

    # ---- ways ------------------------------------------------------------
    for wid, way in ways.items():
        tags = way.get("tags", {})
        if not tags:
            continue
        coords = to_coords(way.get("nodes", []))
        if len(coords) < 2:
            continue
        closed = len(coords) >= 4 and coords[0] == coords[-1]

        if _is_part(tags):
            if wid in rel_member_ways:
                continue
            ring = coords if closed else coords + [coords[0]]
            if len(ring) >= 4:
                out["building_parts"].append({
                    "kind": "area", "layer": "building_parts", "coords": ring,
                    "holes": [], "tags": tags, "osm": f"way/{wid}"})
            continue

        if "building" in tags:
            if wid in rel_member_ways:
                continue
            ring = coords if closed else coords + [coords[0]]
            if len(ring) >= 4:
                out["buildings"].append({
                    "kind": "area", "layer": "buildings", "coords": ring,
                    "holes": [], "tags": tags, "osm": f"way/{wid}"})
            continue

        if "highway" in tags and cfg.layers.get("roads", LayerStyle()).enabled:
            w = _road_width(tags, cfg)
            if w > 0:
                out["roads"].append({
                    "kind": "line", "layer": "roads", "coords": coords,
                    "holes": [], "tags": tags, "osm": f"way/{wid}", "width_m": w})
            continue

        if ("railway" in tags and _RAIL_MATCH.match(tags.get("railway", ""))
                and cfg.layers.get("rail", LayerStyle()).enabled):
            out["rail"].append({
                "kind": "line", "layer": "rail", "coords": coords, "holes": [],
                "tags": tags, "osm": f"way/{wid}",
                "width_m": cfg.layers["rail"].width_m})
            continue

        # --- land-cover zone areas (parks / forest / farmland / urban / water) ---
        zone = _zone_of(tags)
        if zone and cfg.layers.get(zone, LayerStyle()).enabled:
            if wid in rel_member_ways:
                continue
            if closed:
                out[zone].append({
                    "kind": "area", "layer": zone, "coords": coords,
                    "holes": [], "tags": tags, "osm": f"way/{wid}"})
                continue

        # --- waterway lines (river / canal / stream) ---
        ww = tags.get("waterway", "")
        if ww in ("river", "canal", "stream") and cfg.layers.get(
                "water", LayerStyle()).enabled:
            out["water"].append({
                "kind": "line", "layer": "water", "coords": coords,
                "holes": [], "tags": tags, "osm": f"way/{wid}",
                "width_m": max(cfg.layers["water"].width_m, 4.0)})
            continue

    logger.info("Parsed features: %s",
                ", ".join(f"{k}={len(v)}" for k, v in out.items() if v))
    return out


def parse_buildings(osm_json: dict) -> list:
    """Backwards-compatible: just the building features."""
    return parse_features(osm_json)["buildings"]


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _bbox_metres(bbox):
    """Real width and height of a bounding box, in metres."""
    south, west, north, east = bbox
    lat0 = math.radians((south + north) / 2.0)
    return ((east - west) * 111_320.0 * math.cos(lat0),
            (north - south) * 111_320.0)


def make_projector(bbox):
    south, west, north, east = bbox
    lat0 = (south + north) / 2.0
    lon0 = (west + east) / 2.0
    mpd_lat = 111_320.0
    mpd_lon = 111_320.0 * math.cos(math.radians(lat0))

    def project(lat, lon):
        return (lon - lon0) * mpd_lon, (lat - lat0) * mpd_lat

    return project, (lat0, lon0)


def _fit_bbox_to_ratio(bbox, ratio_str: str):
    """Expand ``bbox`` (keeping its centre) so width:height matches ``ratio_str``.
    Expands the smaller dimension so nothing selected is lost."""
    if not ratio_str or ratio_str.strip().lower() in ("free", ""):
        return tuple(bbox)
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*$", ratio_str)
    if not m:
        logger.warning("aspect ratio %r not understood -- using 'free'", ratio_str)
        return tuple(bbox)
    target = float(m.group(1)) / float(m.group(2))       # width / height
    south, west, north, east = bbox
    cy, cx = (south + north) / 2, (west + east) / 2
    mpd_lat = 111_320.0
    mpd_lon = 111_320.0 * math.cos(math.radians(cy))
    real_w = (east - west) * mpd_lon
    real_h = (north - south) * mpd_lat
    if real_w <= 0 or real_h <= 0:
        return tuple(bbox)
    if real_w / real_h > target:            # too wide -> grow height
        real_h = real_w / target
    else:                                   # too tall -> grow width
        real_w = real_h * target
    dlat = (real_h / mpd_lat) / 2
    dlon = (real_w / mpd_lon) / 2
    new = (cy - dlat, cx - dlon, cy + dlat, cx + dlon)
    logger.info("aspect %s -> bbox expanded to S=%.6f W=%.6f N=%.6f E=%.6f",
                ratio_str, *new)
    return new


# ---------------------------------------------------------------------------
# Height resolution (buildings)
# ---------------------------------------------------------------------------

def resolve_height(tags, footprint_area_m2, config: Optional[PipelineConfig] = None):
    cfg = config or _module_default_config()
    if not getattr(cfg, "use_osm_heights", True):
        # flat-block mode: ignore height / building:levels entirely
        return cfg.uniform_building_height_m * cfg.height_scale
    if "height" in tags:
        try:
            return float(str(tags["height"]).strip().split(" ")[0]) * cfg.height_scale
        except ValueError:
            pass
    if "building:levels" in tags:
        try:
            return (float(tags["building:levels"]) * cfg.default_level_height_m
                    * cfg.height_scale)
        except ValueError:
            pass
    for max_area, h in cfg.fallback_height_buckets:
        if footprint_area_m2 < max_area:
            return h * cfg.height_scale
    return cfg.fallback_height_buckets[-1][1] * cfg.height_scale


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _poly_from_feature(feat, project, simplify_m=0.0, clip=None):
    xy = [project(la, lo) for la, lo in feat["coords"]]
    holes = [[project(la, lo) for la, lo in ring] for ring in feat.get("holes", [])]
    try:
        poly = Polygon(xy, holes) if holes else Polygon(xy)
    except Exception:  # noqa: BLE001
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    if clip is not None:
        poly = poly.intersection(clip)
        if poly.is_empty:
            return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon":
        return None
    if simplify_m > 0:
        poly = poly.simplify(simplify_m, preserve_topology=True)
        if poly.is_empty or poly.geom_type != "Polygon":
            return None
    return poly


def _extrude(poly, height, z0):
    mesh = trimesh.creation.extrude_polygon(poly, height=max(height, 0.05))
    mesh.apply_translation([0, 0, z0])
    return mesh


# ---------------------------------------------------------------------------
# Roof shapes (detail level 3+)
# ---------------------------------------------------------------------------

# every roof:shape value we can build, mapped onto the profiles we know
_ROOF_ALIASES = {
    "pyramidal": "pyramidal", "pyramid": "pyramidal", "cone": "cone",
    "conical": "cone", "spire": "cone", "tented": "pyramidal",
    "dome": "dome", "sphere": "dome", "round": "dome", "onion": "onion",
    "gabled": "gabled", "gable": "gabled", "pitched": "gabled",
    "gambrel": "gabled", "saltbox": "gabled",
    "hipped": "hipped", "half-hipped": "hipped", "mansard": "hipped",
    "skillion": "skillion", "lean_to": "skillion", "shed": "skillion",
}


def _tag_float(tags, keys):
    for k in keys:
        if k in tags:
            try:
                return float(str(tags[k]).strip().split(" ")[0])
            except ValueError:
                pass
    return None


def _roof_shape(tags):
    s = str(tags.get("roof:shape") or tags.get("building:roof:shape") or "")
    return _ROOF_ALIASES.get(s.strip().lower())


def _obb_axes(poly):
    """(centre, long-axis unit vector, half-length, half-width) of the minimum
    area rotated rectangle around ``poly``."""
    try:
        rect = poly.minimum_rotated_rectangle
        pts = np.asarray(rect.exterior.coords)[:4]
    except Exception:  # noqa: BLE001
        c = np.asarray(poly.centroid.coords[0])
        return c, np.array([1.0, 0.0]), 1.0, 1.0
    e0, e1 = pts[1] - pts[0], pts[2] - pts[1]
    l0, l1 = float(np.linalg.norm(e0)), float(np.linalg.norm(e1))
    if l0 >= l1:
        v, ll, ww = e0 / max(l0, 1e-9), l0, l1
    else:
        v, ll, ww = e1 / max(l1, 1e-9), l1, l0
    return (np.asarray(rect.centroid.coords[0]), v,
            max(ll / 2.0, 1e-6), max(ww / 2.0, 1e-6))


def _roof_height(tags, poly, cfg, wall_h):
    """Metres of the building's height that the roof itself occupies."""
    h = _tag_float(tags, ("roof:height", "building:roof:height"))
    if h is None:
        lv = _tag_float(tags, ("roof:levels", "building:roof:levels"))
        if lv is not None:
            h = lv * cfg.default_level_height_m
    if h is None:
        _c, _v, _hl, half_w = _obb_axes(poly)
        h = min(max(0.7 * half_w, 1.2), 8.0)
    h = h * cfg.height_scale
    return max(min(h, wall_h * 0.75), 0.3)


def _roof_profile(shape, poly, tags=None):
    """f(xy) -> height 0..1 across the footprint, 0 at the eaves."""
    c, long_v, _half_l, half_w = _obb_axes(poly)
    perp = np.array([-long_v[1], long_v[0]])
    ring = poly.exterior

    def edge_dist(xy):
        return np.array([ring.distance(Point(float(p[0]), float(p[1])))
                         for p in xy])

    if shape in ("pyramidal", "cone", "dome", "onion"):
        def f_radial(xy, _s=shape):
            d = edge_dist(xy)
            dmax = float(d.max())
            t = d / dmax if dmax > 0 else d * 0.0
            if _s in ("pyramidal", "cone"):
                return t
            r = 1.0 - t                       # 0 at the apex, 1 at the eaves
            if _s == "dome":
                return np.sqrt(np.clip(1.0 - r * r, 0.0, 1.0))
            return np.clip(1.0 - r ** 1.6, 0.0, 1.0) ** 0.6      # onion
        return f_radial
    if shape == "hipped":
        def f_hip(xy):
            d = edge_dist(xy)
            # a hip reaches full height at the ridge; on an L-shaped plan the
            # widest inscribed distance is what defines that, not the OBB
            return np.clip(d / max(min(half_w, float(d.max())), 1e-6), 0.0, 1.0)
        return f_hip
    if shape == "skillion":
        u = long_v
        b = _tag_float(tags or {}, ("roof:direction", "roof:slope:direction"))
        if b is not None:                      # compass bearing it slopes down to
            br = math.radians(b)
            u = -np.array([math.sin(br), math.cos(br)])
        ring_xy = np.asarray(poly.exterior.coords)[:, :2]
        proj = (ring_xy - c) @ u
        lo, span = float(proj.min()), max(float(proj.max() - proj.min()), 1e-6)

        def f_skillion(xy):
            return np.clip(((xy - c) @ u - lo) / span, 0.0, 1.0)
        return f_skillion

    def f_gable(xy):                            # gabled and its look-alikes
        return np.clip(1.0 - np.abs((xy - c) @ perp) / half_w, 0.0, 1.0)
    return f_gable


def _triangulate(poly):
    try:
        return trimesh.creation.triangulate_polygon(poly, engine="earcut")
    except TypeError:
        return trimesh.creation.triangulate_polygon(poly)


def _oriented(mesh):
    """Flip a solid that came out inside-out. (trimesh's own fix_normals wants
    scipy, which this project does not depend on.)"""
    try:
        if mesh.volume < 0:
            mesh.invert()
    except Exception:  # noqa: BLE001
        pass
    return mesh


def _height_field_mesh(poly, zfun, h, seg_len, base_h=0.0, clip=True):
    """Watertight solid: flat bottom on ``poly`` at z=0, top surface following
    ``base_h + h * zfun(xy)``. Every roof shape is built through this -- walls
    and roof in one body, so the building stays a single closed solid.

    ``clip`` keeps zfun's output in 0..1 (roof profiles); pass False when zfun
    already returns an absolute height, as terrain relief does."""
    v2, f = _triangulate(poly)
    v3 = np.column_stack([np.asarray(v2, dtype=float),
                          np.zeros(len(v2), dtype=float)])
    if seg_len > 0:
        v3, f = trimesh.remesh.subdivide_to_size(v3, np.asarray(f),
                                                 max_edge=seg_len)
    f = np.asarray(f, dtype=np.int64)
    n = len(v3)
    top = v3.copy()
    zv = np.asarray(zfun(top[:, :2]), dtype=float)
    top[:, 2] = float(base_h) + float(h) * (np.clip(zv, 0.0, 1.0) if clip else zv)
    verts = np.vstack([top, v3])
    # the walls run along the edges only one triangle uses -- keep each edge's
    # own direction (the triangulation winds CCW, so the outside is on its right)
    directed = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    _u, inv, counts = np.unique(np.sort(directed, axis=1), axis=0,
                                return_inverse=True, return_counts=True)
    border = directed[counts[inv.ravel()] == 1]
    a, b = border[:, 0], border[:, 1]
    walls = np.vstack([np.column_stack([a, a + n, b + n]),
                       np.column_stack([a, b + n, b])])
    faces = np.vstack([f, f[:, ::-1] + n, walls])
    return _oriented(trimesh.Trimesh(vertices=verts, faces=faces, process=True))


def _extrude_with_roof(poly, tags, h, z0, cfg):
    """Straight prism, or prism + roof when the feature carries a roof:shape we
    understand. Returns None if even the plain prism fails."""
    shape = _roof_shape(tags or {})
    if shape is not None and h > 0.6:
        try:
            rh = _roof_height(tags, poly, cfg, h)
            wall = max(h - rh, 0.05)
            _c, _v, _hl, half_w = _obb_axes(poly)
            # curved roofs need a finer grid than the piecewise-flat ones; at
            # print scale a roof is under a millimetre, so don't overdo it
            seg = max(half_w / (3.0 if shape in ("dome", "onion", "cone")
                                else 1.25), 0.6)
            m = _height_field_mesh(poly, _roof_profile(shape, poly, tags),
                                   rh, seg, base_h=wall)
            m.apply_translation([0.0, 0.0, z0])
            return m
        except Exception as exc:  # noqa: BLE001
            logger.debug("roof %s failed (%s) -- flat top", shape, exc)
    try:
        return _extrude(poly, h, z0)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Tapered landmark tiers (detail level 4+)
# ---------------------------------------------------------------------------

def _ring_samples(poly, n=72):
    """``n`` points around the outline, one per angle from the centroid, so two
    outlines can be lofted vertex to vertex. None when the shape is too concave
    for that (a ray would leave and re-enter it)."""
    c = poly.representative_point()
    cx, cy = float(c.x), float(c.y)
    minx, miny, maxx, maxy = poly.bounds
    reach = max(maxx - minx, maxy - miny) * 2.0 + 1.0
    ring = poly.exterior
    pts = []
    for k in range(n):
        a = 2.0 * math.pi * k / n
        ray = LineString([(cx, cy),
                          (cx + reach * math.cos(a), cy + reach * math.sin(a))])
        hit = ray.intersection(ring)
        if hit.is_empty:
            return None
        best, bestd = None, -1.0
        for g in getattr(hit, "geoms", [hit]):
            for x, y in (g.coords if hasattr(g, "coords") else []):
                d = (x - cx) ** 2 + (y - cy) ** 2
                if d > bestd:
                    best, bestd = (x, y), d
        if best is None:
            return None
        pts.append(best)
    arr = np.asarray(pts, dtype=float)
    try:
        if Polygon(arr).area < 0.82 * poly.area:      # too concave to loft
            return None
    except Exception:  # noqa: BLE001
        return None
    return arr


def _loft(ring_a, z_a, ring_b, z_b):
    """Closed solid between two matching rings at two heights."""
    n = len(ring_a)
    verts = np.vstack([np.column_stack([ring_a, np.full(n, float(z_a))]),
                       np.column_stack([ring_b, np.full(n, float(z_b))])])
    ca, cb = verts[:n].mean(axis=0), verts[n:].mean(axis=0)
    ia, ib = 2 * n, 2 * n + 1
    verts = np.vstack([verts, ca, cb])
    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces += [[i, j, j + n], [i, j + n, i + n],
                  [j, i, ia], [i + n, j + n, ib]]
    return _oriented(trimesh.Trimesh(
        vertices=verts, faces=np.asarray(faces, dtype=np.int64), process=True))


def _taper_tiers(recs):
    """recs: tier dicts with poly / z0 / h / idx from one landmark. Returns
    {idx: mesh} for every tier whose top could be morphed into the footprint of
    the tier resting on it -- that is what turns a stack of boxes into the
    Eiffel Tower's curve. Tiers we cannot loft are left to the caller."""
    out = {}
    tiers = [r for r in recs if r["h"] > 0]
    for r in tiers:
        r["_top"] = r["z0"] + r["h"]
    for r in tiers:
        tol = max(1.0, 0.03 * r["h"])
        best, best_area = None, 0.0
        for q in tiers:
            if q is r or abs(q["z0"] - r["_top"]) > tol:
                continue
            try:
                inter = q["poly"].intersection(r["poly"])
            except Exception:  # noqa: BLE001
                continue
            if not inter.is_empty and inter.area > best_area:
                best, best_area = q, inter.area
        target = None
        if best is not None:
            if 0.05 <= best_area / max(r["poly"].area, 1e-9) <= 0.95:
                target = best["poly"].intersection(r["poly"])
        elif r["h"] > 2.0 * math.sqrt(max(r["poly"].area, 1e-9)):
            # a tall tier with nothing on top of it -> bring it to a point
            target = _affinity.scale(r["poly"], 0.5, 0.5,
                                     origin=r["poly"].centroid)
        if target is None or target.is_empty:
            continue
        if target.geom_type == "MultiPolygon":
            target = max(target.geoms, key=lambda g: g.area)
        if target.geom_type != "Polygon":
            continue
        a = _ring_samples(r["poly"])
        b = _ring_samples(target)
        if a is None or b is None:
            continue
        try:
            out[r["idx"]] = _loft(a, r["z0"], b, r["_top"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("taper failed (%s)", exc)
    return out


# ---------------------------------------------------------------------------
# Imported landmark model (detail level 5)
# ---------------------------------------------------------------------------

MODEL_FITS = ("height", "footprint", "stretch")
MODEL_UPRIGHT = ("z", "y", "x")


# Turning the file so that one of its axes points up. The rotation that makes
# +X point at +Z is a *minus* ninety about Y -- plus ninety puts it underground.
_UP_TURNS = {
    "z":  None,
    "-z": (180.0, (1.0, 0.0, 0.0)),
    "y":  (90.0, (1.0, 0.0, 0.0)),
    "-y": (-90.0, (1.0, 0.0, 0.0)),
    "x":  (-90.0, (0.0, 1.0, 0.0)),
    "-x": (90.0, (0.0, 1.0, 0.0)),
}
MODEL_UPRIGHTS = ("auto", "z", "y", "x")


def _shape_cost(ext, want):
    """How differently proportioned two boxes are, ignoring their sizes and
    which way round the two horizontal axes happen to be."""
    ex, ey, ez = (float(v) for v in ext)
    wx, wy, wz = (float(v) for v in want)
    if min(ex, ey, ez) <= 0 or min(wx, wy, wz) <= 0:
        return float("inf")
    a = sorted((ex, ey))
    b = sorted((wx, wy))
    return (abs(math.log((a[0] / ez) / (b[0] / wz)))
            + abs(math.log((a[1] / ez) / (b[1] / wz))))


def _standing_penalty(mesh):
    """A building carries its mass low. If the centre of the model sits in the
    top half of it, it is probably on its head."""
    lo, hi = mesh.bounds[0][2], mesh.bounds[1][2]
    h = hi - lo
    if h <= 0:
        return 0.0
    try:
        cz = float(mesh.centroid[2])
    except Exception:  # noqa: BLE001
        return 0.0
    return max(0.0, (cz - lo) / h - 0.5) * 1.6


def _auto_upright(mesh, want):
    """Which way up makes this model the shape of the landmark it replaces.
    Returns (name, turned mesh)."""
    best, best_cost, best_name = None, float("inf"), "z"
    for name, turn in _UP_TURNS.items():
        m = mesh.copy()
        if turn:
            m.apply_transform(trimesh.transformations.rotation_matrix(
                math.radians(turn[0]), list(turn[1]), m.centroid))
        cost = _shape_cost(m.extents, want) + _standing_penalty(m)
        if cost < best_cost:
            best, best_cost, best_name = m, cost, name
    return best_name, best


def _auto_yaw(mesh, want):
    """Lay the model's long side along the landmark's long side. Only worth
    doing when both are clearly longer one way than the other."""
    ex, ey = float(mesh.extents[0]), float(mesh.extents[1])
    wx, wy = float(want[0]), float(want[1])
    if min(ex, ey, wx, wy) <= 0:
        return 0.0, mesh
    if max(ex, ey) / min(ex, ey) < 1.15 or max(wx, wy) / min(wx, wy) < 1.15:
        return 0.0, mesh                      # near enough square either way
    if (ex >= ey) == (wx >= wy):
        return 0.0, mesh                      # already the right way round
    mesh.apply_transform(trimesh.transformations.rotation_matrix(
        math.radians(90.0), [0, 0, 1], mesh.centroid))
    return 90.0, mesh


def _fit_landmark_model(path, box, hl):
    """Load an STL / OBJ / 3MF and drop it into ``box`` -- the millimetre-space
    bounding box the landmark's own OSM geometry occupies, ((x0,y0,z0),(x1,y1,z1)).

    Fitting happens after the model has been scaled to print size, so what you
    ask for is what you measure. ``hl.model_fit`` picks how:

      height     uniform, Z matches the landmark's height in the model, so it
                 sits in scale with the city around it (the sane default)
      footprint  uniform, XY fills the landmark's OSM outline. Careful: that
                 outline is often the whole plaza, not the building's base, so
                 the model can come out far taller than the rest of the map
      stretch    non-uniform, fills the box exactly (distorts the model)
    """
    mesh = trimesh.load(path, force="mesh")
    if mesh is None or not len(getattr(mesh, "faces", [])):
        raise ValueError("no triangles in that file")
    mesh = mesh.copy()

    (x0, y0, z0), (x1, y1, z1) = box
    want = np.array([x1 - x0, y1 - y0, z1 - z0], dtype=float)
    if want.min() <= 0:
        raise ValueError("the landmark has no room on the plate")

    up = str(getattr(hl, "model_upright", "auto") or "auto").lower()
    chosen, auto_yaw = up, 0.0
    if up.startswith("a"):
        chosen, mesh = _auto_upright(mesh, want)
        logger.info("  model %s: standing it up by its proportions -> %s up "
                    "(the landmark is %.0f x %.0f x %.0f mm)",
                    os.path.basename(path), chosen.upper(), *want)
    else:
        turn = _UP_TURNS.get(up[:1])
        if turn:
            mesh.apply_transform(trimesh.transformations.rotation_matrix(
                math.radians(turn[0]), list(turn[1]), mesh.centroid))

    rot = float(getattr(hl, "model_rotate_deg", 0.0) or 0.0)
    if rot:
        mesh.apply_transform(trimesh.transformations.rotation_matrix(
            math.radians(rot), [0, 0, 1], mesh.centroid))
    elif up.startswith("a"):
        auto_yaw, mesh = _auto_yaw(mesh, want)
        if auto_yaw:
            logger.info("  model %s: turned %.0f deg so its long side runs the "
                        "same way as the landmark's", os.path.basename(path),
                        auto_yaw)

    ext = np.asarray(mesh.extents, dtype=float)
    if not np.all(np.isfinite(ext)) or float(ext.max()) <= 0:
        raise ValueError("that model has no size")

    # an axis this thin can't be divided by -- say which fit would work instead
    eps = float(ext.max()) * 1e-6
    fit = str(getattr(hl, "model_fit", "height") or "height").lower()
    if fit == "stretch":
        if float(ext.min()) <= eps:
            raise ValueError("that model is flat in one axis -- 'stretch' needs "
                             "all three")
        s = want / ext
    elif fit == "height":
        if ext[2] <= eps:
            raise ValueError("that model is flat in Z -- try the 'footprint' fit")
        s = np.repeat(want[2] / ext[2], 3)
    else:
        if min(ext[0], ext[1]) <= eps:
            raise ValueError("that model is flat in X or Y -- try the 'height' fit")
        s = np.repeat(min(want[0] / ext[0], want[1] / ext[1]), 3)
    pct = float(getattr(hl, "model_scale", 0.0) or 0.0)
    if pct > 0:                     # a multiplier on the fit, 1.0 == as fitted
        s = s * pct
    mesh.apply_scale(s.tolist())

    b = mesh.bounds                 # centre on the footprint, stand on the plate
    mesh.apply_translation([(x0 + x1) / 2.0 - (b[0][0] + b[1][0]) / 2.0,
                            (y0 + y1) / 2.0 - (b[0][1] + b[1][1]) / 2.0,
                            z0 - b[0][2]])
    return mesh


# ---------------------------------------------------------------------------
# A path joining the landmarks, in the order you picked them
# ---------------------------------------------------------------------------

PATH_STYLES = ("straight", "curved", "roads")


def _chaikin(pts, rounds=3):
    """Round the corners off a polyline (Chaikin). Keeps both end points."""
    pts = [tuple(p) for p in pts]
    for _ in range(max(int(rounds), 0)):
        if len(pts) < 3:
            break
        out = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            out.append((a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25))
            out.append((a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75))
        out.append(pts[-1])
        pts = out
    return pts


def path_waypoints(cfg, polys_by_osm):
    """Where the path calls, in the order the ids were given. Ids that are not
    on the plate are skipped -- with a note, since it changes the route."""
    pts, missing = [], []
    for osm in tuple(getattr(cfg, "path_osm_ids", ()) or ()):
        poly = polys_by_osm.get(osm)
        if poly is None or poly.is_empty:
            missing.append(osm)
            continue
        c = poly.representative_point()
        pts.append((float(c.x), float(c.y)))
    if missing:
        logger.warning("  path: %d landmark(s) are not on the plate (%s) -- "
                       "the route skips them", len(missing), ", ".join(missing))
    return pts


def _road_graph(feats, project, clip=None):
    """The street network as a graph: nodes are road vertices in projected
    metres, edges are the runs between them weighted by real length. Ways that
    meet at a junction share that node's exact coordinates in OSM, so snapping
    on position is enough to make the network connected."""
    index, nodes, adj = {}, [], []

    def node_id(p):
        key = (round(p[0], 2), round(p[1], 2))          # 1 cm is the same node
        i = index.get(key)
        if i is None:
            i = len(nodes)
            index[key] = i
            nodes.append(p)
            adj.append([])
        return i

    for feat in feats.get("roads", []) or []:
        prev = None
        for la, lo in feat["coords"]:
            p = project(la, lo)
            if clip is not None and not clip.contains(Point(p)):
                prev = None                             # off the plate: break here
                continue
            i = node_id(p)
            if prev is not None and prev != i:
                d = math.hypot(nodes[i][0] - nodes[prev][0],
                               nodes[i][1] - nodes[prev][1])
                adj[prev].append((i, d))
                adj[i].append((prev, d))
            prev = i
    return nodes, adj


def _dijkstra(adj, start, goal):
    """Cheapest run of edges from start to goal, or None if they are not
    connected (a road cut off by the plate edge, an island, a pedestrian-only
    block that was filtered out)."""
    import heapq  # noqa: PLC0415
    dist = {start: 0.0}
    prev = {}
    seen = set()
    pq = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == goal:
            break
        for v, w in adj[u]:
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if goal not in dist:
        return None
    out = [goal]
    while out[-1] != start:
        out.append(prev[out[-1]])
    return out[::-1]


def _kerbside(stop, poly, road_xy):
    """Where a landmark's spur should meet the street: the point on its own
    outline nearest that road, so the path stops at the door instead of
    striking out from the middle of the building."""
    if poly is None or poly.is_empty:
        return stop
    try:
        ring = poly.exterior
        return tuple(ring.interpolate(ring.project(Point(road_xy))).coords[0])
    except Exception:  # noqa: BLE001
        return stop


def route_along_roads(feats, project, stops, clip=None, stop_polys=None):
    """Join the stops by street rather than by straight line, so the path runs
    down roads instead of ploughing through buildings. Each stop is linked to
    the nearest road with a short spur -- the way a path meets a street.
    Returns the centre line, or None if there are no roads to follow."""
    nodes, adj = _road_graph(feats, project, clip)
    if len(nodes) < 2:
        logger.warning("  path: no roads on the plate to follow -- is the roads "
                       "layer switched on?")
        return None
    arr = np.asarray(nodes, dtype=float)
    polys = list(stop_polys or [None] * len(stops))
    polys += [None] * (len(stops) - len(polys))

    def nearest(p):
        return int(np.argmin((arr[:, 0] - p[0]) ** 2 + (arr[:, 1] - p[1]) ** 2))

    # each stop joins the network at its kerbside point, not its centre
    ends = []
    for stop, poly in zip(stops, polys):
        i = nearest(stop)
        ends.append((_kerbside(stop, poly, nodes[i]), i))

    line, detours = [ends[0][0]], 0
    for (_pa, ia), (pb, ib) in zip(ends, ends[1:]):
        seq = _dijkstra(adj, ia, ib)
        if seq is None:
            detours += 1
            line.append(pb)                             # straight for this leg
            continue
        line.extend(nodes[i] for i in seq)
        line.append(pb)
    if detours:
        logger.warning("  path: %d leg(s) had no road connecting them -- those "
                       "run straight", detours)
    out = [line[0]]
    for p in line[1:]:                                  # drop repeats
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return out if len(out) >= 2 else None


def path_polyline(cfg, stops, feats=None, project=None, clip=None,
                  stop_polys=None):
    """The centre line of the route through the stops, in projected metres."""
    if len(stops) < 2:
        return []
    stops = list(stops)
    polys = list(stop_polys or [None] * len(stops))
    polys += [None] * (len(stops) - len(polys))
    if getattr(cfg, "path_closed", False) and len(stops) > 2:
        stops.append(stops[0])
        polys.append(polys[0])
    style = str(getattr(cfg, "path_style", "straight")).lower()
    if style == "roads":
        if feats is None or project is None:
            logger.warning("  path: no map data to route with -- straight legs")
        else:
            line = route_along_roads(feats, project, stops, clip, polys)
            if line:
                return line
            logger.warning("  path: falling back to straight legs")
    if style == "curved":
        return _chaikin(stops, 3)
    return stops

def build_path_mesh(cfg, line, clip_poly, base_z, terrain_z=None):
    """The route as a printable ribbon, from a centre line that
    :func:`path_polyline` has already worked out. Returns a mesh, or None."""
    route = list(line or ())
    if len(route) < 2:
        return None
    width = max(float(getattr(cfg, "path_width_m", 8.0)), 0.1)
    try:
        strip = LineString(route).buffer(width / 2.0, cap_style=2, join_style=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("  path: could not build the ribbon (%s)", exc)
        return None
    if clip_poly is not None:
        strip = strip.intersection(clip_poly)
    if strip.is_empty:
        logger.warning("  path: the route falls outside the plate")
        return None
    height = max(float(getattr(cfg, "path_height_m", 1.2)), 0.05)

    parts = []
    for g in getattr(strip, "geoms", [strip]):
        if g.is_empty or g.geom_type != "Polygon":
            continue
        try:
            if terrain_z is None:
                parts.append(_extrude(g, height, base_z))
            else:
                # over relief the ribbon has to ride the ground, not float
                span = max(g.bounds[2] - g.bounds[0], g.bounds[3] - g.bounds[1])
                seg = max(span / 60.0, width / 2.0)

                def follow(xy, _h=height):
                    return np.array([terrain_z(float(x), float(y)) + _h
                                     for x, y in xy])

                parts.append(_height_field_mesh(g, follow, 1.0, seg, clip=False))
        except Exception as exc:  # noqa: BLE001
            logger.debug("path piece failed (%s)", exc)
    if not parts:
        return None
    mesh = trimesh.util.concatenate(parts) if len(parts) > 1 else parts[0]
    logger.info("  path: %.0f m long, %.1f m wide, %d point(s) -> %d triangles",
                LineString(route).length, width, len(route), len(mesh.faces))
    return mesh

def _match_highlight(feat, project, highlights, hl_shapes, hl_proj_shapes=None):
    """Return (index, Highlight) if the feature belongs to a highlight group --
    by OSM id, by lasso polygon (lat/lon), or by sitting inside a highlighted
    building's projected footprint (used so building:part tiers inherit the
    highlight of the landmark they belong to)."""
    for i, hl in enumerate(highlights):
        if feat["osm"] in hl.osm_ids:
            return i, hl
    la = sum(c[0] for c in feat["coords"]) / len(feat["coords"])
    lo = sum(c[1] for c in feat["coords"]) / len(feat["coords"])
    if hl_shapes:
        pt = Point(lo, la)
        for i, shapes in hl_shapes.items():
            for s in shapes:
                if s.contains(pt):
                    return i, highlights[i]
    if hl_proj_shapes:
        px, py = project(la, lo)
        ppt = Point(px, py)
        for i, shapes in hl_proj_shapes.items():
            for s in shapes:
                if s.contains(ppt):
                    return i, highlights[i]
    return None


# ---------------------------------------------------------------------------
# Terrain relief (optional, keyless AWS Terrarium elevation tiles)
# ---------------------------------------------------------------------------

_TERRAIN_TILE = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRAIN_MAX_TILES = 144          # how much elevation we are willing to download
TERRAIN_MAX_FACES = 260_000      # and how many triangles the relief may become


def _deg2tilexy(lat, lon, z):
    n = 2 ** z
    xt = (lon + 180.0) / 360.0 * n
    yt = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return xt, yt


def _terrain_zoom(bbox, want_x, want_y, max_tiles, zmax=14):
    """Coarsest zoom that gives at least ``want_x`` by ``want_y`` pixels, inside
    the tile budget. Both axes matter: a tall narrow country like Israel is
    wide enough at z7 but nowhere near detailed enough down its length."""
    south, west, north, east = bbox
    best = 0
    for z in range(0, zmax + 1):
        x0, y0 = _deg2tilexy(north, west, z)
        x1, y1 = _deg2tilexy(south, east, z)
        cols = int(math.floor(max(x0, x1))) - int(math.floor(min(x0, x1))) + 1
        rows = int(math.floor(max(y0, y1))) - int(math.floor(min(y0, y1))) + 1
        if cols * rows > max_tiles:
            break
        best = z
        if abs(x1 - x0) * 256 >= want_x and abs(y1 - y0) * 256 >= want_y:
            return z
    return best


def _fetch_terrain_grid(bbox, samples, timeout=30, max_tiles=TERRAIN_MAX_TILES,
                        workers=6):
    """Elevations in metres over ``bbox`` as a (rows, cols) array, row 0 south.
    The grid keeps the bbox's own aspect so a wide country is not squashed.
    Returns None if the tiles could not be fetched."""
    try:
        from PIL import Image  # noqa: PLC0415
        import io as _io       # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("terrain: Pillow not available (%s) -- skipping relief", exc)
        return None

    south, west, north, east = bbox
    S = max(8, int(samples))
    w_m, h_m = _bbox_metres(bbox)
    if w_m >= h_m:
        Sx, Sy = S, max(8, int(round(S * h_m / max(w_m, 1e-9))))
    else:
        Sy, Sx = S, max(8, int(round(S * w_m / max(h_m, 1e-9))))

    z = _terrain_zoom(bbox, Sx, Sy, max_tiles)
    x0f, y0f = _deg2tilexy(north, west, z)
    x1f, y1f = _deg2tilexy(south, east, z)
    tx0, tx1 = int(math.floor(min(x0f, x1f))), int(math.floor(max(x0f, x1f)))
    ty0, ty1 = int(math.floor(min(y0f, y1f))), int(math.floor(max(y0f, y1f)))
    cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
    logger.info("terrain: zoom %d, %d x %d tiles, sampling %d x %d",
                z, cols, rows, Sx, Sy)

    mosaic = np.full((rows * 256, cols * 256), np.nan, dtype=np.float64)
    ua = {"User-Agent": HEADERS["User-Agent"]}
    n_tiles = 2 ** z

    def grab(ij):
        i, j = ij
        # wrap in x so an area crossing the date line still works
        url = _TERRAIN_TILE.format(z=z, x=(tx0 + i) % n_tiles, y=ty0 + j)
        if not (0 <= ty0 + j < n_tiles):
            return i, j, None
        try:
            r = requests.get(url, headers=ua, timeout=timeout)
            r.raise_for_status()
            a = np.asarray(Image.open(_io.BytesIO(r.content)).convert("RGB"),
                           dtype=np.float64)
            return i, j, a[:, :, 0] * 256.0 + a[:, :, 1] + a[:, :, 2] / 256.0 - 32768.0
        except Exception:  # noqa: BLE001
            return i, j, None

    missing = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, j, tile in pool.map(grab, [(i, j) for j in range(rows)
                                          for i in range(cols)]):
            if tile is None:
                missing += 1
            else:
                mosaic[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = tile
    if missing:
        logger.warning("terrain: %d of %d tiles did not arrive", missing,
                       cols * rows)
        if missing == cols * rows:
            return None
        mosaic = np.nan_to_num(mosaic, nan=float(np.nanmean(mosaic)))

    # bilinear sample the mosaic on a lat/lon grid, all at once
    lats = np.linspace(south, north, Sy)
    lons = np.linspace(west, east, Sx)
    xt = (lons + 180.0) / 360.0 * n_tiles
    yt = (1.0 - np.arcsinh(np.tan(np.radians(lats))) / math.pi) / 2.0 * n_tiles
    px = np.clip((xt - tx0) * 256.0, 0, mosaic.shape[1] - 1.001)
    py = np.clip((yt - ty0) * 256.0, 0, mosaic.shape[0] - 1.001)
    xi, yi = px.astype(int), py.astype(int)
    fx, fy = px - xi, py - yi
    X0, X1 = mosaic[np.ix_(yi, xi)], mosaic[np.ix_(yi, xi + 1)]
    Y0, Y1 = mosaic[np.ix_(yi + 1, xi)], mosaic[np.ix_(yi + 1, xi + 1)]
    top = X0 * (1 - fx)[None, :] + X1 * fx[None, :]
    bot = Y0 * (1 - fx)[None, :] + Y1 * fx[None, :]
    out = top * (1 - fy)[:, None] + bot * fy[:, None]

    logger.info("terrain: elevation %.0f .. %.0f m across the area",
                float(out.min()), float(out.max()))
    return out


def print_scale(cfg, bbox):
    """Millimetres per real metre once the model is sized for the print."""
    w_m, h_m = _bbox_metres(bbox)
    s_x = cfg.target_width_mm / max(w_m, 1e-9) if cfg.target_width_mm else 0.0
    s_y = (getattr(cfg, "target_depth_mm", 0.0) or 0.0) / max(h_m, 1e-9)
    if str(getattr(cfg, "size_mode", "fill")).lower() == "fit" and s_x and s_y:
        return min(s_x, s_y)
    return s_x or s_y or 1.0


def terrain_exaggeration_for(cfg, bbox, relief_m):
    """The vertical exaggeration that makes the highest ground stand
    ``cfg.terrain_relief_mm`` above the plate on the finished print. Falls back
    to the plain multiplier when there is nothing to work from."""
    want = float(getattr(cfg, "terrain_relief_mm", 0.0) or 0.0)
    plain = max(float(cfg.terrain_exaggeration), 0.0)
    if want <= 0 or relief_m <= 0 or not cfg.target_width_mm:
        return plain
    s_xy = print_scale(cfg, bbox)
    s_z = s_xy * max(cfg.vertical_exaggeration, 0.01)
    exag = want / max(relief_m * s_z, 1e-12)
    logger.info("terrain: %.0f m of relief -> %.1f mm on the print "
                "(exaggeration x%.1f)", relief_m, want, exag)
    return exag


def terrain_range(elev, plate_poly, bbox, project):
    """Lowest and highest ground actually *inside* the plate outline.

    It matters: Israel's bounding box takes in the Mediterranean, whose floor
    is 1800 m down, and measuring from there leaves the country's own hills as
    a rounding error. Falls back to the whole grid if it cannot rasterise."""
    Sy, Sx = elev.shape
    try:
        from PIL import Image, ImageDraw  # noqa: PLC0415
        south, west, north, east = bbox
        x0, y0 = project(south, west)
        x1, y1 = project(north, east)
        xlo, ylo = min(x0, x1), min(y0, y1)
        sx = max(max(x0, x1) - xlo, 1e-6)
        sy = max(max(y0, y1) - ylo, 1e-6)
        img = Image.new("L", (Sx, Sy), 0)
        pts = [((x - xlo) / sx * (Sx - 1), (y - ylo) / sy * (Sy - 1))
               for x, y in plate_poly.exterior.coords]
        ImageDraw.Draw(img).polygon(pts, fill=255)
        inside = np.asarray(img) > 0          # row 0 is the south edge, as in elev
        if inside.sum() >= 8:
            vals = elev[inside]
            return float(np.nanmin(vals)), float(np.nanmax(vals))
    except Exception as exc:  # noqa: BLE001
        logger.debug("terrain: outline mask failed (%s)", exc)
    return float(np.nanmin(elev)), float(np.nanmax(elev))


def _make_terrain_solid(plate_poly, bbox, project, cfg, elev, exaggeration=None):
    """Relief over whatever the plate outline is -- a rectangle or a country's
    own coastline. Returns (mesh, terrain_z(x, y))."""
    south, west, north, east = bbox
    Sy, Sx = elev.shape
    lo, hi = terrain_range(elev, plate_poly, bbox, project)
    exag = (max(float(cfg.terrain_exaggeration), 0.0) if exaggeration is None
            else max(float(exaggeration), 0.0))
    # anything deeper than the lowest land inside the outline is sea: flatten
    # it to that level instead of letting it dig a trench under the plate
    rel = (np.clip(elev, lo, None) - lo) * exag
    x0, y0 = project(south, west)
    x1, y1 = project(north, east)
    xlo, xhi = min(x0, x1), max(x0, x1)
    ylo, yhi = min(y0, y1), max(y0, y1)
    span_x, span_y = max(xhi - xlo, 1e-6), max(yhi - ylo, 1e-6)

    def zfun(xy):
        xy = np.atleast_2d(np.asarray(xy, dtype=float))
        fx = np.clip((xy[:, 0] - xlo) / span_x * (Sx - 1), 0, Sx - 1.001)
        fy = np.clip((xy[:, 1] - ylo) / span_y * (Sy - 1), 0, Sy - 1.001)
        xi, yi = fx.astype(int), fy.astype(int)
        tx, ty = fx - xi, fy - yi
        v = ((rel[yi, xi] * (1 - tx) + rel[yi, xi + 1] * tx) * (1 - ty)
             + (rel[yi + 1, xi] * (1 - tx) + rel[yi + 1, xi + 1] * tx) * ty)
        return cfg.base_thickness_m + v

    # one triangle per elevation sample. Subdivision overshoots by a lot on
    # awkward outlines, so measure the result and coarsen once if it ran away.
    seg = span_x / max(Sx - 1, 1)
    mesh = _height_field_mesh(plate_poly, zfun, 1.0, seg, clip=False)
    if len(mesh.faces) > TERRAIN_MAX_FACES:
        seg *= math.sqrt(len(mesh.faces) / TERRAIN_MAX_FACES)
        logger.info("terrain: %d triangles is too many -- rebuilding at %.0f m "
                    "per step", len(mesh.faces), seg)
        mesh = _height_field_mesh(plate_poly, zfun, 1.0, seg, clip=False)
    logger.info("terrain: relief mesh %d triangles, %.1f m of relief",
                len(mesh.faces), float(rel.max()))
    return mesh, (lambda x, y: float(zfun([[x, y]])[0]))


# ---------------------------------------------------------------------------
# Base plate
# ---------------------------------------------------------------------------

def base_plate_polygon(bbox, project, config: Optional[PipelineConfig] = None):
    """Projected 2D outline of the plate: the free-form ``area_polygon`` if the
    config has one, otherwise the bbox rectangle."""
    cfg = config or _module_default_config()
    ap = getattr(cfg, "area_polygon", ()) or ()
    if len(ap) >= 3:
        poly = Polygon([project(la, lo) for la, lo in ap])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty and poly.geom_type == "Polygon":
            return poly
    south, west, north, east = bbox
    return Polygon([project(la, lo) for la, lo in
                    [(south, west), (south, east), (north, east), (north, west)]])


def build_base_plate(bbox, project, config: Optional[PipelineConfig] = None):
    cfg = config or _module_default_config()
    return trimesh.creation.extrude_polygon(
        base_plate_polygon(bbox, project, cfg), height=cfg.base_thickness_m)


# ---------------------------------------------------------------------------
# Back-compat wrapper used by older tests / scripts
# ---------------------------------------------------------------------------

def build_building_meshes(buildings, project, config: Optional[PipelineConfig] = None):
    cfg = config or _module_default_config()
    meshes, skipped = [], 0
    for b in buildings:
        poly = _poly_from_feature(b, project, cfg.simplify_tolerance_m)
        if poly is None or poly.area < cfg.min_footprint_area_m2:
            skipped += 1
            continue
        try:
            m = _extrude(poly, resolve_height(b["tags"], poly.area, cfg),
                         cfg.base_thickness_m)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        meshes.append(m)
    logger.info("Built %d building meshes, skipped %d.", len(meshes), skipped)
    return meshes


# ---------------------------------------------------------------------------
# 3MF writer (hand-rolled: trimesh's exporter drops per-object colour)
# ---------------------------------------------------------------------------

_CT_XML = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
           '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
           '</Types>')
_RELS_XML = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
             'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
             '</Relationships>')


def _write_3mf(path, named_meshes):
    """named_meshes: list of (name, trimesh.Trimesh, (r,g,b)). Writes a 3MF with
    one object + one base-material per entry so slicers see distinct colours."""
    mats = []
    for name, _m, rgb in named_meshes:
        r, g, b = (int(max(0, min(255, c))) for c in rgb)
        mats.append(f'<base name="{_xml_escape(name)}" '
                    f'displaycolor="#{r:02X}{g:02X}{b:02X}FF"/>')
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
        '<metadata name="Application">city-map-generator</metadata>',
        '<resources>',
        '<basematerials id="1">', *mats, '</basematerials>',
    ]
    oids = []
    for idx, (name, mesh, _rgb) in enumerate(named_meshes):
        v, f = mesh.vertices, mesh.faces
        if len(v) < 4 or len(f) < 4:
            logger.warning("3MF: skipping empty/degenerate object %r", name)
            continue
        oid = idx + 2
        oids.append(oid)
        parts.append(f'<object id="{oid}" name="{_xml_escape(name)}" '
                     f'type="model" pid="1" pindex="{idx}">')
        parts.append('<mesh><vertices>')
        parts.append("".join(
            f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v))
        parts.append('</vertices><triangles>')
        parts.append("".join(
            f'<triangle v1="{a}" v2="{b_}" v3="{c}"/>' for a, b_, c in f))
        parts.append('</triangles></mesh></object>')
    parts.append('</resources>')          # <-- was missing: made the file invalid
    parts.append('<build>')
    parts += [f'<item objectid="{o}"/>' for o in oids]
    parts.append('</build>')
    parts.append('</model>')
    model_xml = "".join(parts)

    # never ship a 3MF a slicer will refuse: parse it before zipping
    import xml.etree.ElementTree as _ET
    try:
        _ET.fromstring(model_xml)
    except _ET.ParseError as exc:
        raise ValueError(f"internal error: generated 3MF XML is invalid ({exc})")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT_XML)
        z.writestr("_rels/.rels", _RELS_XML)
        z.writestr("3D/3dmodel.model", model_xml)
    logger.info("  3MF: %d coloured object(s)", len(oids))


def _xml_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(s)).strip("_") or "group"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _validate_bbox(bbox) -> tuple:
    try:
        south, west, north, east = (float(v) for v in bbox)
    except (TypeError, ValueError):
        raise ValueError("bounding box must be four numbers "
                         f"(south, west, north, east); got {bbox!r}")
    if not (-90.0 <= south < north <= 90.0):
        raise ValueError(f"latitude problem: south ({south}) must be < north "
                         f"({north}), both within [-90, 90]")
    if not (-180.0 <= west < east <= 180.0):
        raise ValueError(f"longitude problem: west ({west}) must be < east "
                         f"({east}), both within [-180, 180]")
    return south, west, north, east


def _accepts_two_args(fn) -> bool:
    import inspect
    try:
        inspect.signature(fn).bind(None, None)
        return True
    except (TypeError, ValueError):
        return False


def _feat_min_height(tags) -> float:
    for key in ("min_height", "building:min_height"):
        if key in tags:
            try:
                return max(float(str(tags[key]).strip().split(" ")[0]), 0.0)
            except ValueError:
                pass
    return 0.0


def _build_area_layer(feats, layer, style, project, cfg, highlights, hl_shapes,
                      groups, engrave_polys, min_area, clip=None,
                      hl_proj_shapes=None, covered=None, base_group="__base__",
                      terrain_z=None, detail=DEFAULT_DETAIL_LEVEL,
                      model_keepout=None):
    made = skipped = 0
    inside_model = 0
    is_parts = layer == "building_parts"
    is_bldg = layer in ("buildings", "building_parts")
    # detail tiers keep their true heights even when OSM heights are switched off
    real_h_cfg = cfg if getattr(cfg, "use_osm_heights", True) else replace(
        cfg, use_osm_heights=True)

    # ---- pass 1: footprint + height for everything that survives filtering
    recs = []
    for feat in feats:
        match = _match_highlight(feat, project, highlights, hl_shapes,
                                 hl_proj_shapes if is_parts else None)
        # from level 2 a landmark keeps its full outline and its smallest parts
        landmark_detail = match is not None and detail >= 2
        poly = _poly_from_feature(
            feat, project,
            0.0 if landmark_detail else cfg.simplify_tolerance_m, clip)
        if poly is None or poly.area < (0.2 if landmark_detail else min_area):
            skipped += 1
            continue
        # An imported model stands here instead. Whatever else OSM has inside
        # that landmark -- its legs, its platforms, the plaza under it -- would
        # only poke through the model, so none of it is built.
        if model_keepout is not None and is_bldg and match is None:
            try:
                if poly.intersection(model_keepout).area >= 0.55 * poly.area:
                    skipped += 1
                    inside_model += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
        # a plain footprint that building:part detail already covers -> drop it
        if covered is not None:
            try:
                if poly.intersection(covered).area >= 0.55 * poly.area:
                    skipped += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
        if is_parts:
            h = resolve_height(feat["tags"], poly.area, real_h_cfg)
        elif is_bldg:
            # a building picked for Detail always keeps its true OSM height,
            # even when flat-block mode is on for everything else
            h = resolve_height(feat["tags"], poly.area,
                               real_h_cfg if match is not None else cfg)
        else:
            h = style.height_m
        z0 = cfg.base_thickness_m
        if terrain_z is not None:
            c = poly.representative_point()
            z0 = terrain_z(c.x, c.y)
        if is_parts:
            mh = _feat_min_height(feat["tags"])
            if mh and mh < h:
                z0 = z0 + mh
                h = h - mh
        if match is not None:
            hl = match[1]
            if hl.height_mode == "absolute":
                h = hl.height_m
            elif not is_parts:
                h = h + hl.height_m    # parts keep their real tier heights
        recs.append({"idx": len(recs), "feat": feat, "poly": poly,
                     "match": match, "z0": z0, "h": h})

    # ---- pass 2: landmark tiers become one continuous taper, not a stack
    tapered = {}
    if is_parts and detail >= 4 and highlights:
        by_hl = {}
        for r in recs:
            if r["match"] is not None:
                by_hl.setdefault(r["match"][0], []).append(r)
        for tiers in by_hl.values():
            tapered.update(_taper_tiers(tiers))
        if tapered:
            logger.info("  %-13s -> %d tier(s) tapered", layer, len(tapered))

    # ---- pass 3: geometry
    for r in recs:
        poly, match = r["poly"], r["match"]
        if match is None and style.mode == "engraved" and not is_bldg:
            engrave_polys.append((poly, style.height_m))
            made += 1
            continue
        m = tapered.get(r["idx"])
        if m is None and is_bldg and detail >= 3:
            m = _extrude_with_roof(poly, r["feat"]["tags"], r["h"], r["z0"], cfg)
        elif m is None:
            try:
                m = _extrude(poly, r["h"], r["z0"])
            except Exception:  # noqa: BLE001
                m = None
        if m is None:
            skipped += 1
            continue
        groups[match[1].name if match is not None else base_group].append(m)
        made += 1
    if inside_model:
        logger.info("  %-13s -> %d footprint(s) left out: an imported model "
                    "stands there", layer, inside_model)
    return made, skipped


def _build_line_layer(feats, layer, style, project, cfg, highlights, hl_shapes,
                      groups, engrave_polys, clip=None, base_group="__base__"):
    # bucket polylines by target group, union per group, then extrude
    buckets = {"__base__": [], "__engrave__": []}
    for hl in highlights:
        buckets[hl.name] = []
    made = 0
    for feat in feats:
        xy = [project(la, lo) for la, lo in feat["coords"]]
        if len(xy) < 2:
            continue
        w = max(float(feat.get("width_m", style.width_m)), 0.5)
        try:
            strip = LineString(xy).buffer(w / 2.0, cap_style=2, join_style=2)
        except Exception:  # noqa: BLE001
            continue
        if clip is not None:
            strip = strip.intersection(clip)
        if strip.is_empty or strip.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        match = _match_highlight(feat, project, highlights, hl_shapes)
        if match is not None:
            buckets[match[1].name].append((strip, match[1]))
        elif style.mode == "engraved":
            buckets["__engrave__"].append(strip)
        else:
            buckets["__base__"].append(strip)

    # base
    if buckets["__base__"]:
        merged = unary_union(buckets["__base__"])
        for g in getattr(merged, "geoms", [merged]):
            if g.is_empty:
                continue
            try:
                groups[base_group].append(_extrude(g, style.height_m,
                                                   cfg.base_thickness_m))
                made += 1
            except Exception:  # noqa: BLE001
                pass
    # engraved
    for strip in buckets["__engrave__"]:
        engrave_polys.append((strip, style.height_m))
        made += 1
    # highlighted
    for hl in highlights:
        items = buckets.get(hl.name) or []
        if not items:
            continue
        merged = unary_union([s for s, _ in items])
        h = hl.height_m if hl.height_mode == "absolute" else style.height_m + hl.height_m
        for g in getattr(merged, "geoms", [merged]):
            if g.is_empty:
                continue
            try:
                m = _extrude(g, h, cfg.base_thickness_m)
                groups[hl.name].append(m)
                made += 1
            except Exception:  # noqa: BLE001
                pass
    return made, 0


def run_pipeline(config: PipelineConfig,
                 *, fetch: Optional[Callable] = None) -> PipelineResult:
    """Run the full generate pipeline. Progress + errors via the logger.
    Returns :class:`PipelineResult`; raises OverpassError / ValueError / OSError
    on failure (callers catch and present; nothing here calls sys.exit)."""
    cfg = config
    # default via the fetch_buildings alias so callers/tests that monkeypatch
    # that name still intercept the network call
    fetch_fn = fetch or fetch_buildings

    logger.info("=" * 64)
    logger.info("Pipeline start")
    logger.info("  bbox (S,W,N,E)        : %s", tuple(cfg.bbox))
    logger.info("  layers enabled        : %s",
                [n for n, s in cfg.layers.items() if s.enabled])
    logger.info("  base_thickness_m      : %s", cfg.base_thickness_m)
    logger.info("  height_scale          : %s   vertical_exaggeration: %s",
                cfg.height_scale, cfg.vertical_exaggeration)
    logger.info("  target size (mm)      : W %s  D %s  H %s   aspect_ratio: %s",
                cfg.target_width_mm or "(1:1 real m)",
                getattr(cfg, "target_depth_mm", 0) or "(from aspect)",
                getattr(cfg, "target_height_mm", 0) or "(from exaggeration)",
                cfg.aspect_ratio)
    logger.info("  detail level          : %s",
                DETAIL_LEVEL_LABELS[detail_level_of(cfg.detail_level) - 1])
    logger.info("  highlights            : %d group(s)", len(cfg.highlights))
    logger.info("  exports               : stl=%s 3mf=%s split=%s",
                cfg.export_stl, cfg.export_3mf, cfg.export_split)
    logger.info("  overpass mirrors      : %s", list(cfg.overpass_urls))
    logger.info("  output                : %s", os.path.abspath(cfg.output_stl))

    bbox = _validate_bbox(cfg.bbox)
    # asking for an exact width *and* depth is itself an aspect ratio: crop the
    # area to it up front so the model is never stretched to reach the numbers
    ratio_str = cfg.aspect_ratio
    fit_inside = str(getattr(cfg, "size_mode", "fill")).lower() == "fit"
    if (cfg.target_width_mm > 0 and getattr(cfg, "target_depth_mm", 0) > 0
            and not fit_inside):
        ratio_str = f"{cfg.target_width_mm}:{cfg.target_depth_mm}"
        logger.info("  exact W x D given -> aspect ratio %s", ratio_str)
    free_area = tuple(getattr(cfg, "area_polygon", ()) or ())
    if len(free_area) >= 3:
        las = [p[0] for p in free_area]
        los = [p[1] for p in free_area]
        bbox = (min(las), min(los), max(las), max(los))
        logger.info("  free-form area polygon: %d points -> plate follows that "
                    "outline; aspect_ratio ignored", len(free_area))
    else:
        bbox = _fit_bbox_to_ratio(bbox, ratio_str)
    _validate_bbox(bbox)

    want_layers = [n for n, st in cfg.layers.items() if st.enabled]
    terrain_only = not want_layers and cfg.terrain_enabled
    if terrain_only:
        # a country's relief needs no buildings, and asking Overpass for them
        # over that area would be absurd -- so don't
        logger.info("Step 1/6: no map layers enabled -- terrain only, "
                    "skipping Overpass")
        feats = {}
    else:
        logger.info("Step 1/6: fetching map data from Overpass ...")
        if _accepts_two_args(fetch_fn):
            osm_json = fetch_fn(bbox, cfg)
        else:
            osm_json = fetch_fn(bbox)

        logger.info("Step 2/6: parsing OSM response ...")
        feats = parse_features(osm_json, cfg)
        if not any(feats.values()):
            raise ValueError("Nothing mappable found in that bounding box. Check "
                             "the coordinates / pick a denser area.")

    project, origin = make_projector(bbox)
    logger.info("Step 3/6: projection origin (lat, lon) = %s", origin)

    # a plate you could actually print: thicken it if the print scale would
    # otherwise leave it paper thin
    want_base = float(getattr(cfg, "base_min_mm", 0.0) or 0.0)
    if want_base > 0 and cfg.target_width_mm:
        s_z = print_scale(cfg, bbox) * max(cfg.vertical_exaggeration, 0.01)
        need_m = want_base / max(s_z, 1e-12)
        if need_m > cfg.base_thickness_m:
            logger.info("  plate would be %.3f mm thick -- thickening it to "
                        "%.1f mm (%.0f m at this scale)",
                        cfg.base_thickness_m * s_z, want_base, need_m)
            cfg = replace(cfg, base_thickness_m=need_m)

    logger.info("Step 4/6: base plate ...")
    plate_poly = base_plate_polygon(bbox, project, cfg)
    terrain_z = None
    base = None
    if cfg.terrain_enabled:
        elev = _fetch_terrain_grid(bbox, cfg.terrain_samples)
        if elev is not None:
            sea = float(getattr(cfg, "terrain_sea_level_m", 0.0) or 0.0)
            deep = float(np.nanmin(elev))
            if deep < sea:
                elev = np.maximum(elev, sea)
                logger.info("terrain: water flattened to %.0f m (the tiles go "
                            "down to %.0f m here)", sea, deep)
            t_lo, t_hi = terrain_range(elev, plate_poly, bbox, project)
            logger.info("terrain: ground inside the outline runs %.0f .. %.0f m",
                        t_lo, t_hi)
            exag = terrain_exaggeration_for(cfg, bbox, t_hi - t_lo)
            try:
                base, terrain_z = _make_terrain_solid(plate_poly, bbox, project,
                                                      cfg, elev, exag)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  terrain relief failed (%s) -- flat plate", exc)
                base, terrain_z = None, None
    if base is None:
        base = trimesh.creation.extrude_polygon(plate_poly, height=cfg.base_thickness_m)
    # everything is clipped to the plate outline so overhanging ways
    # (parks, long roads) don't blow up the model bounds / print size.
    clip_poly = plate_poly

    lvl = detail_level_of(cfg.detail_level)
    highlights = tuple(cfg.highlights)
    hl_shapes = {i: hl.as_shapely() for i, hl in enumerate(highlights)}
    groups = {"__base__": [base]}
    for hl in highlights:
        groups.setdefault(hl.name, [])
    # a layer with its own colour exports as its own object -> its own group
    layer_group = {}
    layer_color = {}
    for lname, lstyle in cfg.layers.items():
        col = getattr(lstyle, "color", None)
        if col:
            groups.setdefault(lname, [])
            layer_group[lname] = lname
            layer_color[lname] = tuple(col)
    engrave_polys = []
    layer_counts = {}

    # projected+clipped polygon per building OSM id -- used to (a) skip flat
    # footprints that building:part detail replaces, and (b) let a part inherit
    # the highlight of the building it sits inside.
    building_polys_by_osm = {}
    for bf in feats.get("buildings", []):
        p = _poly_from_feature(bf, project, 0.0, clip_poly)
        if p is not None:
            building_polys_by_osm.setdefault(bf["osm"], p)
    hl_proj_shapes = {}
    for i, hl in enumerate(highlights):
        shapes = [building_polys_by_osm[o] for o in hl.osm_ids
                  if o in building_polys_by_osm]
        if shapes:
            hl_proj_shapes[i] = shapes

    # detail 5: where an imported model replaces a landmark, keep everything
    # else out of its footprint. Only for a model that is actually there --
    # a missing file falls back to the OSM geometry, which must stay.
    model_keepout = None
    if detail_level_of(cfg.detail_level) >= 5:
        keep = []
        for i, hl in enumerate(highlights):
            path = (getattr(hl, "model_path", "") or "").strip()
            if path and os.path.isfile(path):
                keep += hl_proj_shapes.get(i, [])
        if keep:
            try:
                model_keepout = unary_union(keep)
                logger.info("  detail 5: %d landmark footprint(s) reserved for "
                            "imported models -- nothing else is built there",
                            len(keep))
            except Exception:  # noqa: BLE001
                model_keepout = None

    # union of all building:part footprints (projected) -> footprints they cover
    parts_union = None
    if cfg.layers.get("buildings", LayerStyle()).enabled and feats.get("building_parts"):
        pp = []
        for pf in feats["building_parts"]:
            p = _poly_from_feature(pf, project, 0.0, clip_poly)
            if p is not None:
                pp.append(p)
        if pp:
            try:
                parts_union = unary_union(pp)
            except Exception:  # noqa: BLE001
                parts_union = None

    logger.info("Step 5/6: building layers ...")
    order = ([("farmland", "area"), ("forest", "area"), ("parks", "area"),
              ("urban", "area"), ("water", "area"), ("roads", "line"),
              ("rail", "line"), ("building_parts", "area"), ("buildings", "area")])
    raw_buildings = len(feats.get("buildings", []))
    b_skipped = 0
    for name, kind in order:
        style = cfg.layers.get("buildings" if name == "building_parts" else name)
        if not style or not style.enabled or not feats.get(name):
            continue
        style_key = "buildings" if name == "building_parts" else name
        bg = layer_group.get(style_key, "__base__")
        if kind == "area":
            min_area = (cfg.min_footprint_area_m2 if name == "buildings"
                        else 6.0 if name == "building_parts" else 10.0)
            covered = parts_union if name == "buildings" else None
            made, sk = _build_area_layer(feats[name], name, style, project, cfg,
                                         highlights, hl_shapes, groups,
                                         engrave_polys, min_area, clip_poly,
                                         hl_proj_shapes, covered, bg, terrain_z,
                                         lvl, model_keepout)
        else:
            made, sk = _build_line_layer(feats[name], name, style, project, cfg,
                                         highlights, hl_shapes, groups,
                                         engrave_polys, clip_poly, bg)
        layer_counts[name] = made
        if name == "buildings":
            b_skipped = sk
        logger.info("  %-13s -> %d meshes (%d skipped)", name, made, sk)

    total_made = sum(layer_counts.values()) + sum(
        len(v) for k, v in groups.items() if k != "__base__")
    if total_made == 0 and not engrave_polys and not terrain_only:
        raise ValueError(
            "Nothing landed on the plate. Every feature was outside the "
            "bounding box, below the minimum size, or filtered out. Check the "
            "area (the map rectangle must sit over what you want) and lower "
            "'min footprint area' / enable more layers.")

    # ---- engraving (single boolean; fall back to raised) ------------------
    if engrave_polys:
        logger.info("  engraving %d groove polygons into the base ...",
                    len(engrave_polys))
        solids = []
        for poly, h in engrave_polys:
            try:
                s = trimesh.creation.extrude_polygon(poly, height=max(h, 0.05))
                s.apply_translation([0, 0, cfg.base_thickness_m - h])
                solids.append((s, h))
            except Exception:  # noqa: BLE001
                pass
        try:
            cutter = (trimesh.boolean.union([s for s, _ in solids])
                      if len(solids) > 1 else solids[0][0])
            newbase = trimesh.boolean.difference([base, cutter])
            if newbase.volume > base.volume * 0.3:
                groups["__base__"][0] = newbase
                logger.info("  engraving OK")
            else:
                raise RuntimeError("difference produced a degenerate base")
        except Exception as exc:  # noqa: BLE001
            logger.warning("  engraving failed (%s) -- adding those features as "
                           "raised ridges instead", exc)
            for s, h in solids:
                s.apply_translation([0, 0, h])  # move up onto the base top
                groups["__base__"].append(s)

    # the route joining the landmarks, in metre space like the rest
    if getattr(cfg, "path_enabled", False):
        way = path_waypoints(cfg, building_polys_by_osm)
        way_polys = [building_polys_by_osm.get(o)
                     for o in tuple(getattr(cfg, "path_osm_ids", ()) or ())
                     if building_polys_by_osm.get(o) is not None]
        p_cfg = cfg
        want_mm = float(getattr(cfg, "path_height_mm", 0.0) or 0.0)
        if want_mm > 0:
            s_z = print_scale(cfg, bbox) * max(cfg.vertical_exaggeration, 0.01)
            p_cfg = replace(cfg, path_height_m=want_mm / max(s_z, 1e-12))
        line = path_polyline(cfg, way, feats, project, clip_poly, way_polys)
        pm = build_path_mesh(p_cfg, line, clip_poly,
                             cfg.base_thickness_m, terrain_z)
        if pm is not None:
            groups.setdefault("path", []).append(pm)
            layer_color["path"] = tuple(cfg.path_color)

    # ---- physical scaling ------------------------------------------------
    real_w = base.bounds[1][0] - base.bounds[0][0]
    real_h = base.bounds[1][1] - base.bounds[0][1]
    tw = float(cfg.target_width_mm or 0.0)
    td = float(getattr(cfg, "target_depth_mm", 0.0) or 0.0)
    s_x = (tw / real_w) if (tw > 0 and real_w) else 0.0
    s_y = (td / real_h) if (td > 0 and real_h) else 0.0
    if not s_x and not s_y:
        s_x = s_y = 1.0
    elif fit_inside and s_x and s_y:
        # keep the shape and slip it inside the envelope, whichever way it is
        # the long way round
        s_x = s_y = min(s_x, s_y)
        logger.info("  fitting inside %.0f x %.0f mm -> %.1f x %.1f mm",
                    tw, td, real_w * s_x, real_h * s_y)
    else:                                   # one given -> the other follows it
        s_x = s_x or s_y
        s_y = s_y or s_x
    if abs(s_x - s_y) > 0.005 * max(s_x, s_y):
        logger.warning("  W x D asks for a %.1f%% stretch -- the plate outline "
                       "could not be cropped to that ratio (free-form area?)",
                       abs(s_x - s_y) / max(s_x, s_y) * 100.0)
    s_z = min(s_x, s_y) * max(cfg.vertical_exaggeration, 0.01)
    if (s_x, s_y, s_z) != (1.0, 1.0, 1.0):
        logger.info("  scaling: X x%.5f, Y x%.5f, Z x%.5f", s_x, s_y, s_z)
    for gname, meshes in groups.items():
        for m in meshes:
            m.apply_scale([s_x, s_y, s_z])
    for hl in highlights:
        if hl.raise_mm:
            for m in groups.get(hl.name, []):
                m.apply_translation([0, 0, hl.raise_mm])

    # an exact overall height wins over the exaggeration: squash / stretch Z
    # about the underside of the plate so the finished model measures up
    th = float(getattr(cfg, "target_height_mm", 0.0) or 0.0)
    every = [m for ms in groups.values() for m in ms]
    if th > 0 and every:
        zlo = min(float(m.bounds[0][2]) for m in every)
        span = max(float(m.bounds[1][2]) for m in every) - zlo
        if span > 1e-9:
            k = th / span
            for m in every:
                m.apply_translation([0, 0, -zlo])
                m.apply_scale([1.0, 1.0, k])
                m.apply_translation([0, 0, zlo])
            logger.info("  total height fitted to %.2f mm (Z x%.4f on top)", th, k)

    # ---- imported landmark models (detail level 5) -----------------------
    # Done last, in millimetre space: the model is fitted to the box the
    # landmark's own geometry ended up occupying, so it is never squashed by
    # the vertical exaggeration or the exact-height fit the way the map is.
    if lvl >= 5:
        for hl in highlights:
            path = (getattr(hl, "model_path", "") or "").strip()
            if not path:
                continue
            ms = groups.get(hl.name) or []
            if not ms:
                logger.warning("  custom model for %r: nothing of that landmark "
                               "is on the plate -- ignored", hl.name)
                continue
            box = trimesh.util.concatenate(ms).bounds
            try:
                m = _fit_landmark_model(path, box, hl)
            except Exception as exc:  # noqa: BLE001
                logger.warning("  custom model %s could not be used (%s) -- "
                               "keeping the OSM geometry", path, exc)
                continue
            was = box[1] - box[0]
            groups[hl.name] = [m]
            e = m.extents
            logger.info("  landmark %r <- %s: %d triangles, %.1f x %.1f x %.1f mm "
                        "(fit=%s, rotate=%s deg, up=%s); the OSM landmark was "
                        "%.1f x %.1f x %.1f mm", hl.name, os.path.basename(path),
                        len(m.faces), e[0], e[1], e[2],
                        getattr(hl, "model_fit", "height"),
                        getattr(hl, "model_rotate_deg", 0),
                        getattr(hl, "model_upright", "z"), *was)
            if e[2] > 2.5 * was[2]:
                logger.warning("  that model stands %.0f%% taller than the "
                               "landmark it replaces -- an OSM outline is often "
                               "the whole plaza, so try the 'height' fit or a "
                               "smaller scale", e[2] / max(was[2], 1e-9) * 100.0)

    # ---- assemble + export ---------------------------------------------
    logger.info("Step 6/6: assembling + exporting ...")
    base_combined = trimesh.util.concatenate(groups["__base__"])

    # every non-base group -> its own object: highlight groups first, then any
    # land-cover layer that was given a colour.
    extra = []  # (name, mesh, (r,g,b))
    for hl in highlights:
        ms = groups.get(hl.name) or []
        if ms:
            extra.append((hl.name, trimesh.util.concatenate(ms), tuple(hl.color)))
    for lname, col in layer_color.items():
        ms = groups.get(lname) or []
        if ms:
            extra.append((lname, trimesh.util.concatenate(ms), col))

    everything = trimesh.util.concatenate(
        [base_combined] + [m for _n, m, _c in extra])

    out_path = os.path.abspath(cfg.output_stl)
    stem, _ext = os.path.splitext(out_path)
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    written = []
    try:
        if cfg.export_stl:
            everything.export(out_path)
            written.append(out_path)
        if cfg.export_split:
            bp = stem + "_base.stl"
            base_combined.export(bp)
            written.append(bp)
            for name, m, _c in extra:
                p = f"{stem}_{_slug(name)}.stl"
                m.export(p)
                written.append(p)
        if cfg.export_3mf:
            named = [("base", base_combined, BASE_COLOR)] + list(extra)
            p = stem + ".3mf"
            _write_3mf(p, named)
            written.append(p)
    except OSError as exc:
        raise OSError(f"could not write output near {out_path!r}: {exc}") from exc

    if not written:
        raise ValueError("No export format selected -- enable STL, 3MF or split.")

    size = (everything.bounds[1] - everything.bounds[0]).tolist()
    result = PipelineResult(
        output_path=written[0],
        output_paths=written,
        building_count=layer_counts.get("buildings", 0),
        skipped_count=b_skipped,
        raw_building_count=raw_buildings,
        is_watertight=bool(everything.is_watertight),
        bounds=everything.bounds.tolist(),
        origin=origin,
        size_mm=tuple(round(v, 2) for v in size),
        layer_counts=layer_counts,
        highlight_counts={n: 1 for n, _m, _c in extra},
    )

    logger.info("Export complete.")
    for p in written:
        logger.info("  wrote  %s", p)
    logger.info("  model size (mm)     : %.1f x %.1f x %.1f", *result.size_mm)
    logger.info("  buildings in model  : %d (of %d, %d skipped)",
                result.building_count, result.raw_building_count,
                result.skipped_count)
    logger.info("  layer mesh counts   : %s", result.layer_counts)
    logger.info("  watertight          : %s", result.is_watertight)
    if not result.is_watertight:
        logger.warning("mesh is not watertight -- slicers usually repair this, "
                       "but check before printing.")
    log_success("Done -- saved %s", written[0])
    return result


def main():
    setup_logging()
    cfg = replace(_module_default_config(), bbox=tuple(BBOX), output_stl=OUTPUT_STL)
    try:
        run_pipeline(cfg)
    except OverpassError as exc:
        logger.error("Overpass fetch failed: %s", exc)
        raise SystemExit(1)
    except (ValueError, OSError) as exc:
        logger.error("Pipeline failed: %s", exc)
        raise SystemExit(1)
    except Exception:
        logger.exception("Pipeline failed with an unexpected error:")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
