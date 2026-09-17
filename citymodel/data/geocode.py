"""Find a place: by name / address (Nominatim) or by pasted coordinates."""

from __future__ import annotations

import re
from typing import Optional

from ..progress import NULL_REPORTER, Reporter
from .cache import DAY, DiskCache
from .http import get

NOMINATIM = "https://nominatim.openstreetmap.org/search"
_NUM = r"-?\d+(?:[.,]\d+)?"


def parse_coordinates(text: str) -> Optional[tuple]:
    """``(lat, lon)`` from "32.0853, 34.7818", "32.0853 N 34.7818 E",
    "40.7 -74.0", "N 48.8584 E 2.2945" ... or None if it is not coordinates."""
    t = (text or "").strip().replace("−", "-").replace("°", " ")
    if not t or re.search(r"[A-DF-MO-RT-VX-Za-df-mo-rt-vx-z]{2,}", t):
        return None                                   # words -> it's a place name
    hemi = re.findall(rf"([NSEWnsew])\s*({_NUM})|({_NUM})\s*([NSEWnsew])", t)
    if len(hemi) == 2:
        vals = {}
        for a, b, c, d in hemi:
            letter, num = (a, b) if a else (d, c)
            v = float(num.replace(",", "."))
            letter = letter.upper()
            vals["lat" if letter in "NS" else "lon"] = -abs(v) if letter in "SW" else abs(v)
        if {"lat", "lon"} <= set(vals):
            lat, lon = vals["lat"], vals["lon"]
            return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None
    nums = re.findall(r"-?\d+(?:\.\d+)?", t)
    if len(nums) == 2:
        lat, lon = float(nums[0]), float(nums[1])
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    return None


def search(query: str, limit: int = 6, reporter: Reporter = NULL_REPORTER) -> list:
    """``[{"name", "lat", "lon", "bbox": (s, w, n, e)}, ...]``. Coordinates are
    recognised locally and never sent anywhere."""
    query = (query or "").strip()
    if not query:
        return []
    pt = parse_coordinates(query)
    if pt is not None:
        lat, lon = pt
        return [{"name": f"{lat:.6f}, {lon:.6f}", "lat": lat, "lon": lon, "bbox": None}]
    cache = DiskCache("geocode", ttl_s=30 * DAY)
    key = f"v1|{limit}|{query.lower()}"
    hit = cache.get_json(key)
    if hit is not None:
        return hit
    resp = get(NOMINATIM, params={"q": query, "format": "jsonv2", "limit": limit,
                                  "addressdetails": 0},
               timeout=20, reporter=reporter, what="Place search (Nominatim)")
    out = []
    for r in resp.json():
        try:
            bb = [float(v) for v in r.get("boundingbox", [])]
            out.append({"name": r.get("display_name", query),
                        "lat": float(r["lat"]), "lon": float(r["lon"]),
                        "bbox": (bb[0], bb[2], bb[1], bb[3]) if len(bb) == 4 else None})
        except (KeyError, ValueError):
            continue
    cache.put_json(key, out)
    return out
