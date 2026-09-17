"""How tall is a building? In order of trust:

1. ``height`` tag (metres; feet / inches understood)
2. ``building:levels`` (+ ``roof:levels``) x level height
3. Overture Maps height / floor count for the same footprint (optional)
4. an estimate: a typical height for the building type, pulled towards the
   median of the measured buildings around it -- a "yes" building in a
   district of 8-storey blocks is probably not a 6 m house.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

# typical eave-to-ground heights by building=* value, metres
TYPE_HEIGHT_M = {
    "house": 6.5, "detached": 6.5, "semidetached_house": 6.5, "bungalow": 4.0,
    "terrace": 7.5, "residential": 9.0, "apartments": 15.0, "dormitory": 12.0,
    "hotel": 18.0, "commercial": 12.0, "office": 20.0, "retail": 6.0,
    "supermarket": 6.0, "industrial": 8.0, "warehouse": 8.0, "hangar": 10.0,
    "garage": 2.8, "garages": 2.8, "carport": 2.5, "shed": 2.5, "hut": 2.5,
    "roof": 3.5, "kiosk": 3.0, "cabin": 3.5, "farm_auxiliary": 5.0, "barn": 7.0,
    "greenhouse": 3.5, "service": 3.0, "school": 9.0, "university": 14.0,
    "college": 12.0, "hospital": 18.0, "church": 16.0, "cathedral": 30.0,
    "chapel": 9.0, "mosque": 12.0, "synagogue": 12.0, "temple": 12.0,
    "civic": 12.0, "public": 12.0, "government": 15.0, "train_station": 10.0,
    "transportation": 8.0, "parking": 9.0, "stadium": 20.0, "grandstand": 12.0,
    "sports_hall": 10.0, "construction": 9.0, "ruins": 3.0, "bunker": 3.0,
}
GENERIC_BY_AREA = ((60.0, 5.0), (250.0, 8.0), (1200.0, 12.0), (float("inf"), 14.0))
SMALL_TYPES = {"garage", "garages", "carport", "shed", "hut", "roof", "kiosk",
               "cabin", "greenhouse", "service", "bunker", "ruins"}

_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def parse_length_m(value) -> float | None:
    """'18', '18 m', '18.5m', "60'", "60 ft", '5\\'11"' -> metres."""
    if value is None:
        return None
    t = str(value).strip().lower()
    nums = _NUM.findall(t)
    if not nums:
        return None
    v = float(nums[0].replace(",", "."))
    if "'" in t or "ft" in t or "feet" in t or "foot" in t:
        inches = float(nums[1].replace(",", ".")) if len(nums) > 1 else 0.0
        v = v * 0.3048 + inches * 0.0254
    return v if 0 < v < 1000 else None


def _levels(tags) -> float | None:
    for key in ("building:levels", "levels"):
        if key in tags:
            m = _NUM.search(str(tags[key]))
            if m:
                v = float(m.group(0).replace(",", "."))
                if 0 < v < 200:
                    return v
    return None


@dataclass
class Height:
    metres: float
    source: str            # "tag" | "levels" | "overture" | "estimate"


def tagged_height(tags: dict, level_height_m: float = 3.0) -> Height | None:
    h = parse_length_m(tags.get("height") or tags.get("building:height"))
    if h is not None:
        return Height(h, "tag")
    lv = _levels(tags)
    if lv is not None:
        roof_lv = 0.0
        m = _NUM.search(str(tags.get("roof:levels", "")))
        if m:
            roof_lv = min(float(m.group(0).replace(",", ".")), 4.0)
        return Height(lv * level_height_m + roof_lv * level_height_m * 0.7 + 0.5, "levels")
    return None


def min_height_m(tags: dict, level_height_m: float = 3.0) -> float:
    h = parse_length_m(tags.get("min_height") or tags.get("building:min_height"))
    if h is not None:
        return h
    m = _NUM.search(str(tags.get("building:min_level", "")))
    if m:
        return max(float(m.group(0).replace(",", ".")), 0.0) * level_height_m
    return 0.0


def estimate_height(tags: dict, area_m2: float, neighbour_median_m: float | None) -> Height:
    kind = str(tags.get("building") or tags.get("building:part") or "yes").lower()
    typed = TYPE_HEIGHT_M.get(kind)
    if typed is not None and (kind in SMALL_TYPES or neighbour_median_m is None):
        return Height(typed, "estimate")
    generic = next(h for a, h in GENERIC_BY_AREA if area_m2 < a)
    base = typed if typed is not None else generic
    if neighbour_median_m is None:
        return Height(base, "estimate")
    # small footprints in a tall district are usually still small buildings
    pull = 0.75 if area_m2 >= 120 else 0.35
    if typed is not None:
        pull *= 0.5
    return Height(base * (1 - pull) + neighbour_median_m * pull, "estimate")


class NeighbourHeights:
    """Median measured height within ``radius_m`` of a point."""

    def __init__(self, xy_m: np.ndarray, heights_m: np.ndarray, radius_m: float = 180.0):
        self.xy = np.asarray(xy_m, dtype=np.float64).reshape(-1, 2)
        self.h = np.asarray(heights_m, dtype=np.float64)
        self.r2 = radius_m ** 2

    def median_near(self, x: float, y: float, min_count: int = 3) -> float | None:
        if len(self.h) < min_count:
            return None
        d2 = (self.xy[:, 0] - x) ** 2 + (self.xy[:, 1] - y) ** 2
        near = self.h[d2 <= self.r2]
        if len(near) < min_count:
            return None
        return float(np.median(near))
