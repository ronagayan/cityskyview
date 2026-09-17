"""OpenStreetMap data through the Overpass API, cached on disk.

The request bbox is snapped outward to a coarse grid before it is sent, so
nudging the rectangle on the map re-uses the download instead of hitting the
API again; the pipeline clips to the exact area afterwards.
"""

from __future__ import annotations

import json
import math
from typing import Optional, Sequence

from ..log import logger
from ..progress import NULL_REPORTER, Reporter
from .cache import DAY, DiskCache
from .http import NetworkError, post

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

SNAP_DEG = 0.004            # ~450 m: how coarse the cached request grid is
CACHE_TTL_S = 14 * DAY
MAX_AREA_KM2 = 60.0         # beyond this a full-detail query is unreasonable

_QUERY_PARTS = (
    'way["building"]({b});', 'relation["building"]({b});',
    'way["building:part"]({b});', 'relation["building:part"]({b});',
    'way["highway"]({b});',
    'way["railway"~"^(rail|light_rail|subway|tram|narrow_gauge|monorail|funicular)$"]({b});',
    'way["natural"="water"]({b});', 'relation["natural"="water"]({b});',
    'way["waterway"~"^(riverbank|river|canal|stream|dock)$"]({b});',
    'way["natural"="coastline"]({b});',
    'way["leisure"~"^(park|garden|pitch|playground|golf_course|recreation_ground)$"]({b});',
    'way["landuse"~"^(grass|recreation_ground|village_green|cemetery|forest|meadow)$"]({b});',
    'relation["leisure"="park"]({b});',
    'way["natural"~"^(wood|scrub|grassland|heath)$"]({b});',
    'relation["natural"="wood"]({b});', 'relation["landuse"="forest"]({b});',
)


class OverpassError(NetworkError):
    pass


def snap_bbox(bbox: Sequence[float], step: float = SNAP_DEG) -> tuple:
    """(south, west, north, east) grown outward onto a ``step`` degree grid."""
    s, w, n, e = bbox
    f = lambda v: round(math.floor(v / step) * step, 6)   # noqa: E731
    c = lambda v: round(math.ceil(v / step) * step, 6)    # noqa: E731
    return (f(s), f(w), c(n), c(e))


def bbox_area_km2(bbox: Sequence[float]) -> float:
    s, w, n, e = bbox
    lat0 = math.radians((s + n) / 2.0)
    return abs((e - w) * 111.32 * math.cos(lat0) * (n - s) * 111.32)


def build_query(bbox: Sequence[float], timeout_s: int = 90) -> str:
    b = ",".join(f"{v:.6f}" for v in bbox)
    body = "\n".join("  " + p.replace("{b}", b) for p in _QUERY_PARTS)
    return (f"[out:json][timeout:{int(timeout_s)}][maxsize:536870912];\n"
            f"(\n{body}\n);\nout body;\n>;\nout skel qt;\n")


def _validate(resp) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise OverpassError(f"reply is not JSON ({exc})") from exc
    if not isinstance(data, dict) or "elements" not in data:
        raise OverpassError("reply has no 'elements' list")
    remark = str(data.get("remark", ""))
    if remark and ("error" in remark.lower() or "timed out" in remark.lower()):
        raise OverpassError(f"server remark: {remark}")
    return data


def fetch_osm(bbox: Sequence[float], *, reporter: Reporter = NULL_REPORTER,
              urls: Sequence[str] = OVERPASS_URLS, use_cache: bool = True,
              timeout_s: float = 120.0, cache: Optional[DiskCache] = None) -> dict:
    """Overpass JSON for everything the app can model inside ``bbox``."""
    if bbox_area_km2(bbox) > MAX_AREA_KM2:
        raise OverpassError(
            f"That area is {bbox_area_km2(bbox):.0f} km2 -- too large to download "
            f"building-level OpenStreetMap data for (limit {MAX_AREA_KM2:.0f} km2). "
            "Draw a smaller area, or switch the building / road layers off for a "
            "terrain-only model.")
    cache = cache or DiskCache("overpass", ttl_s=CACHE_TTL_S)
    snapped = snap_bbox(bbox)
    key = "v2|" + ",".join(f"{v:.6f}" for v in snapped)
    if use_cache:
        hit = cache.get_json(key)
        if hit is not None:
            logger.info("OSM data: using the cached download for this area "
                        "(%d elements)", len(hit.get("elements", [])))
            return hit

    query = build_query(snapped)
    failures = []
    for i, url in enumerate(urls):
        reporter.step(f"Downloading OpenStreetMap data (server {i + 1} of {len(urls)}) ...")
        try:
            resp = post(url, data={"data": query}, timeout=timeout_s, attempts=2,
                        reporter=reporter, what=f"Overpass ({url.split('/')[2]})",
                        headers={"Accept": "application/json"})
            data = _validate(resp)
        except NetworkError as exc:
            failures.append(str(exc))
            continue
        data = {"elements": data["elements"], "bbox": list(snapped)}
        cache.put_json(key, data)
        logger.info("OSM data: %d elements downloaded", len(data["elements"]))
        return data

    stale = cache.get_stale_json(key)
    if stale is not None:
        logger.warning("Overpass is unreachable -- using an older cached copy of "
                       "this area instead")
        return stale
    raise OverpassError(
        "Could not download OpenStreetMap data -- every Overpass server failed. "
        "They are free public servers and are often busy; wait a minute and try "
        "again.\n  " + "\n  ".join(failures))


def dumps(data: dict) -> str:        # small helper for tests / debugging
    return json.dumps(data)
