"""Everything the user can set, in one dataclass that round-trips to JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from .log import logger
from .paths import default_output_dir, settings_file

TERRAIN_QUALITY = {          # name -> grid cell size on the print, mm
    "draft": 1.2,
    "normal": 0.7,
    "fine": 0.45,
    "ultra": 0.3,
}

COLORS = {                   # default component colours (r, g, b)
    "terrain": (196, 188, 172),
    "buildings": (232, 230, 225),
    "selected": (226, 74, 58),
    "water": (84, 148, 214),
    "roads": (92, 96, 104),
    "green": (122, 170, 96),
}


@dataclass
class Area:
    """The piece of the world to model. ``shape`` is "rectangle", "polygon"
    or "circle"; ``ring`` is its outline as [(lat, lon), ...] (a circle is
    stored as centre + radius and turned into a ring on demand)."""
    shape: str = "rectangle"
    bbox: tuple = (40.7540, -73.9900, 40.7600, -73.9800)     # S, W, N, E
    ring: list = field(default_factory=list)
    center: tuple = ()
    radius_m: float = 0.0

    def is_valid(self) -> bool:
        s, w, n, e = self.bbox
        return -90 <= s < n <= 90 and -180 <= w < e <= 180


@dataclass
class ModelSettings:
    # ---- physical model
    size_mm: float = 180.0               # longest side of the plate
    base_thickness_mm: float = 2.0       # solid base under the lowest ground
    vertical_exaggeration: float = 1.0   # terrain AND buildings
    building_height_scale: float = 1.0   # extra multiplier, buildings only
    min_building_height_mm: float = 0.8
    min_feature_mm: float = 0.6          # footprints smaller than this are dropped
    embed_mm: float = 0.6                # how far buildings sink into the terrain
    terrain_quality: str = "normal"      # see TERRAIN_QUALITY

    # ---- terrain
    terrain_enabled: bool = True
    elevation_source: str = "auto"       # see data.elevation.PROVIDER_CHOICES
    opentopo_key: str = ""
    terrain_smoothing: float = 1.0       # 0 = raw; 1 = remove source noise
    flatten_under_buildings: bool = True  # only applied to DSM sources
    clamp_sea: bool = True               # flatten sea-floor bathymetry to 0 m

    # ---- layers
    buildings_enabled: bool = True
    roads_enabled: bool = True
    water_enabled: bool = True
    green_enabled: bool = False
    roof_shapes: bool = True
    layer_raise_mm: float = 0.4          # how far roads / water / green stand proud
    use_overture_heights: bool = False
    default_level_height_m: float = 3.0

    # ---- export
    selected_mode: str = "separate"      # "separate" | "group"
    threemf_layout: str = "parts"        # "parts" (one object, many parts) | "objects"
    export_3mf: bool = True
    export_stl_folder: bool = False
    cut_sockets: bool = False            # subtract buildings from the terrain part
    output_dir: str = ""
    model_name: str = ""

    # ---- look
    colors: dict = field(default_factory=lambda: {k: list(v) for k, v in COLORS.items()})
    preview_ssao: bool = True
    preview_edges: bool = True

    def cell_mm(self) -> float:
        return TERRAIN_QUALITY.get(self.terrain_quality, TERRAIN_QUALITY["normal"])

    def color(self, key: str) -> tuple:
        c = self.colors.get(key) or COLORS.get(key) or (200, 200, 200)
        return tuple(int(v) for v in c[:3])

    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir) if self.output_dir else default_output_dir()


@dataclass
class AppState:
    """What is persisted between sessions."""
    settings: ModelSettings = field(default_factory=ModelSettings)
    area: Area = field(default_factory=Area)
    selected_ids: list = field(default_factory=list)
    map_view: dict = field(default_factory=dict)       # lat, lon, zoom, basemap
    window: dict = field(default_factory=dict)


def _from_dict(cls, d: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (d or {}).items() if k in known})


def load_state(path: Optional[Path] = None) -> AppState:
    p = Path(path or settings_file())
    try:
        raw = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return AppState()
    try:
        st = AppState(
            settings=_from_dict(ModelSettings, raw.get("settings")),
            area=_from_dict(Area, raw.get("area")),
            selected_ids=list(raw.get("selected_ids") or []),
            map_view=dict(raw.get("map_view") or {}),
            window=dict(raw.get("window") or {}))
        st.area.bbox = tuple(st.area.bbox)
        st.area.ring = [tuple(p) for p in st.area.ring]
        st.area.center = tuple(st.area.center)
        merged = {k: list(v) for k, v in COLORS.items()}
        merged.update(st.settings.colors or {})
        st.settings.colors = merged
        if not st.area.is_valid():
            st.area = Area()
        return st
    except (TypeError, ValueError) as exc:
        logger.warning("settings file unreadable (%s) -- using defaults", exc)
        return AppState()


def save_state(state: AppState, path: Optional[Path] = None):
    p = Path(path or settings_file())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=1), "utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("could not save settings: %s", exc)
