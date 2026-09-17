"""
City Map Generator -- GUI
------------------------
Desktop app around the pipeline in ``city_map_generator.py``.

How it works:

  1. Open the map, frame what you want in the pink viewfinder, hit CAPTURE.
     The captured view is both the printable area *and* the reference image --
     no separate coordinate entry. Open the map again later and it comes back
     locked on that exact rectangle.
  2. Optionally click the landmarks you want in full detail, and choose how
     much geometry they get (flat blocks ... your own imported model).
  3. Generate -> STL / coloured 3MF / split parts, then a 3D preview.

Everything else (layer heights, land-cover colours, terrain relief, network
settings) lives behind the "Advanced" button so the main window stays simple.

Run:  python city_map_gui.py     (or double-click launch_gui.bat)
"""

from __future__ import annotations

import concurrent.futures as cf
import glob as _glob
import http.server
import io
import json
import logging
import math
import os
import queue
import re
import threading
import time
import traceback
import unicodedata
import webbrowser
from urllib.parse import parse_qs, urlparse

import tkinter as tk
from tkinter import colorchooser, filedialog, font as tkfont, messagebox
from tkinter import scrolledtext, simpledialog, ttk

import numpy as np
import requests
from shapely.geometry import Point, Polygon
from shapely.ops import polygonize, unary_union

import city_map_generator as gen

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk
    HAVE_PIL = True
except Exception:  # noqa: BLE001
    HAVE_PIL = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Theme: rose pink + white, with a warm plum-slate as the third colour
# ---------------------------------------------------------------------------

PINK = "#D9548A"
PINK_DEEP = "#C0446F"
PINK_SOFT = "#FCEFF4"
PINK_LINE = "#F0D6E1"
PLUM = "#7A5AA6"
BG = "#F4EFF2"          # the page behind the cards
SURFACE = "#FFFFFF"     # cards, fields, notebook pages
FIELD = "#FBF7F9"       # inset slots (thumbnail, list boxes)
SLATE = "#332E3B"
MUTED = "#8C8296"
LOG_BG = "#2B2733"


# ---------------------------------------------------------------------------
# Logo: a little printed city on a plate, drawn at run time so there is no
# binary asset to ship. Cached next to the script as a .png and a .ico.
# ---------------------------------------------------------------------------

LOGO_PNG = os.path.join(SCRIPT_DIR, "city_map_icon.png")
LOGO_ICO = os.path.join(SCRIPT_DIR, "city_map_icon.ico")


def draw_logo(size=256):
    """The app mark: a rose plate with a river, a row of blocks and a tapered
    tower -- the four things this app actually makes."""
    S = 4 * size                      # draw big, shrink down = free antialiasing
    u = S / 100.0                     # one unit = 1% of the icon

    def pts(*p):
        return [(x * u, y * u) for x, y in p]

    def box(x0, y0, x1, y1):
        return [x0 * u, y0 * u, x1 * u, y1 * u]

    # rounded plate, filled with a vertical pink -> plum wash
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box(5, 5, 95, 95), radius=22 * u, fill=255)
    top = np.array([226, 96, 148], dtype=float)
    bottom = np.array([110, 78, 156], dtype=float)
    t = np.linspace(0.0, 1.0, S)[:, None, None]
    wash = (top + (bottom - top) * t) * np.ones((1, S, 1))
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(Image.fromarray(wash.astype("uint8"), "RGB"), (0, 0), mask)
    d = ImageDraw.Draw(img)

    # a river winding across, and two roads
    d.line(pts((5, 68), (22, 63), (38, 70), (54, 71), (72, 64), (95, 68)),
           fill=(150, 205, 245, 235), width=int(7.5 * u), joint="curve")
    for a, b in (((17, 5), (17, 95)), ((5, 35), (95, 27))):
        d.line(pts(a, b), fill=(255, 255, 255, 70), width=int(3.2 * u))

    white, shade = (255, 255, 255, 240), (255, 255, 255, 150)
    for x0, x1, y0 in ((24, 34, 47), (37, 45, 39), (68, 77, 51), (80, 89, 44)):
        d.rectangle(box(x0, y0, x1, 63), fill=white)
        d.rectangle(box(x1 - 2.2, y0, x1, 63), fill=shade)     # a lit-from-left edge

    # the tower: the tapered silhouette this app is good at
    d.polygon(pts((49.6, 17), (53.6, 17), (58.8, 79), (44.4, 79)), fill=white)
    d.polygon(pts((53.6, 17), (58.8, 79), (53.4, 79)), fill=shade)
    d.polygon(pts((51.3, 7), (52.2, 7), (53.6, 18), (49.6, 18)), fill=white)
    band = (236, 214, 232, 255)                # platforms, so the tiers read
    d.polygon(pts((46.2, 60), (57.2, 60), (57.8, 65), (45.6, 65)), fill=band)
    d.polygon(pts((48.2, 40), (55.1, 40), (55.5, 44), (47.8, 44)), fill=band)

    return img.resize((size, size), Image.LANCZOS)


def logo_images():
    """(icon image, small header image), or (None, None) without Pillow."""
    if not HAVE_PIL:
        return None, None
    try:
        big = draw_logo(256)
    except Exception:  # noqa: BLE001
        return None, None
    try:
        if not os.path.isfile(LOGO_PNG):
            big.save(LOGO_PNG)
        if not os.path.isfile(LOGO_ICO):
            big.save(LOGO_ICO, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                                      (128, 128), (256, 256)])
    except Exception:  # noqa: BLE001
        pass                          # a read-only folder just means no cache
    return big, big.resize((40, 40), Image.LANCZOS)


def _ui_font(root):
    """The nicest system UI face actually installed, so the app stops looking
    like a 1998 dialog on machines that have something better."""
    try:
        have = set(tkfont.families(root))
    except Exception:  # noqa: BLE001
        have = set()
    for name in ("Segoe UI Variable Text", "Segoe UI", "Inter", "SF Pro Text",
                 "Helvetica Neue", "DejaVu Sans"):
        if name in have:
            return name
    return "TkDefaultFont"


UI = "Segoe UI"         # replaced with the real pick on the first apply_theme

LEVEL_TAGS = {
    "DEBUG":    {"foreground": "#9A90A5"},
    "INFO":     {"foreground": "#E7E1EC"},
    "SUCCESS":  {"foreground": "#8BD9A8", "font": ("Consolas", 9, "bold")},
    "WARNING":  {"foreground": "#F2C97D"},
    "ERROR":    {"foreground": "#FF9BB0", "font": ("Consolas", 9, "bold")},
    "CRITICAL": {"foreground": "#FFFFFF", "background": PINK_DEEP},
}

ASPECT_PRESETS = ["free", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"]

# which axis of the imported file points up (Blender / CAD exports vary)
# "Auto" reads the model's proportions against the landmark's own footprint
# and height, which is right far more often than a guess at the file format
UPRIGHT_CHOICES = ["Auto", "Z up", "Y up", "X up"]
MODEL_FIT_HINTS = {
    "height": "Scaled so it reaches the same height the landmark reaches in the "
              "map -- in scale with the city around it.",
    "footprint": "Scaled to fill the landmark's OSM outline. That outline is often "
                 "the whole plaza, so the model can end up much taller than the map.",
    "stretch": "Squashed to fill the landmark's box exactly. Fits, but distorts "
               "the model.",
}

# ---- size presets ---------------------------------------------------------
# width / depth / height in mm; 0 means "don't force this one". Your own
# presets are appended to this file next to the script.
PRESET_FILE = os.path.join(SCRIPT_DIR, "city_map_presets.json")
BUILTIN_PRESETS = {
    "Free (no resize)":       {"width": 0, "depth": 0, "height": 0, "aspect": "free"},
    "180 x 180 mm square":    {"width": 180, "depth": 180, "height": 0, "aspect": "1:1"},
    "200 x 150 mm":           {"width": 200, "depth": 150, "height": 0, "aspect": "free"},
    "150 x 150 x 30 mm":      {"width": 150, "depth": 150, "height": 30, "aspect": "1:1"},
    "100 x 100 mm coaster":   {"width": 100, "depth": 100, "height": 12, "aspect": "1:1"},
    "A5 landscape 210 x 148": {"width": 210, "depth": 148, "height": 0, "aspect": "free"},
}


THEME_FILE = os.path.join(SCRIPT_DIR, "city_map_themes.json")
BUILTIN_THEMES = {
    "Paper (default)": None,          # filled in below, once the palette exists
    "Night": {"paper": (38, 36, 42), "urban": (52, 49, 57),
              "farmland": (58, 54, 46), "forest": (44, 58, 46),
              "parks": (48, 60, 50), "water": (40, 64, 92),
              "road_edge": (74, 70, 80), "roads": (150, 145, 156),
              "rail": (92, 88, 98), "buildings": (232, 226, 214),
              "building_edge": (120, 116, 110)},
    "Blueprint": {"paper": (22, 48, 88), "urban": (30, 60, 104),
                  "farmland": (32, 64, 110), "forest": (28, 70, 104),
                  "parks": (30, 74, 110), "water": (18, 40, 76),
                  "road_edge": (70, 118, 170), "roads": (198, 222, 250),
                  "rail": (110, 152, 200), "buildings": (150, 196, 240),
                  "building_edge": (86, 132, 186)},
    "Sepia": {"paper": (246, 238, 222), "urban": (236, 224, 202),
              "farmland": (238, 226, 196), "forest": (206, 208, 174),
              "parks": (224, 222, 190), "water": (198, 210, 206),
              "road_edge": (214, 196, 166), "roads": (255, 250, 238),
              "rail": (190, 172, 146), "buildings": (222, 206, 178),
              "building_edge": (188, 168, 140)},
}


def load_themes() -> dict:
    """Built-in colour themes plus whatever you saved. Never raises."""
    out = {k: (dict(v) if v else dict(BACKDROP_PALETTE))
           for k, v in BUILTIN_THEMES.items()}
    try:
        with open(THEME_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        for name, pal in (saved.get("themes") or {}).items():
            clean = {}
            for key in BACKDROP_PALETTE:
                v = pal.get(key)
                if isinstance(v, (list, tuple)) and len(v) == 3:
                    clean[key] = tuple(int(max(0, min(255, c))) for c in v)
            if clean:
                merged = dict(BACKDROP_PALETTE)
                merged.update(clean)
                out[str(name)] = merged
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return out


def save_themes(themes: dict):
    """Only your own are written, so the built-ins can still be improved."""
    mine = {k: {kk: list(vv) for kk, vv in v.items()}
            for k, v in themes.items() if k not in BUILTIN_THEMES}
    with open(THEME_FILE, "w", encoding="utf-8") as fh:
        json.dump({"themes": mine}, fh, indent=2)


def load_presets() -> dict:
    """Built-ins plus whatever the user saved. Never raises -- a broken preset
    file just means you get the built-ins back."""
    out = dict(BUILTIN_PRESETS)
    try:
        with open(PRESET_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
        for name, p in (saved.get("presets") or {}).items():
            out[str(name)] = {"width": float(p.get("width", 0) or 0),
                              "depth": float(p.get("depth", 0) or 0),
                              "height": float(p.get("height", 0) or 0),
                              "aspect": str(p.get("aspect", "free") or "free")}
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return out


def save_presets(presets: dict):
    """Write back only what is not a built-in, so upgrades keep working."""
    mine = {k: v for k, v in presets.items() if k not in BUILTIN_PRESETS}
    with open(PRESET_FILE, "w", encoding="utf-8") as fh:
        json.dump({"presets": mine}, fh, indent=2)


# ---- automatic file naming ------------------------------------------------

def name_part(text) -> str:
    """One piece of a file name: keep letters (any alphabet), swap runs of
    anything else for a single underscore, drop what Windows forbids."""
    t = unicodedata.normalize("NFKC", str(text or "")).strip()
    t = re.sub(r'[\\/:*?"<>|]+', " ", t)
    t = re.sub(r"[^\w]+", "_", t, flags=re.UNICODE)
    return t.strip("._").lower()


def _mm(v) -> str:
    """120.0 -> '120', 148.5 -> '148.5', 0 -> '0'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "0"
    return str(int(f)) if abs(f - int(f)) < 1e-9 else f"{f:g}"


def build_name(city, landmark, when=None) -> str:
    """city_firstlandmark_date, skipping whatever we don't know."""
    bits = [b for b in (name_part(city), name_part(landmark)) if b]
    bits.append(time.strftime("%Y%m%d", when or time.localtime()))
    return "_".join(bits)


def reverse_place(lat, lon) -> str:
    """The town/city a point sits in, via Nominatim. "" when it can't say."""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"lat": lat, "lon": lon, "format": "jsonv2",
                                 "zoom": 12, "addressdetails": 1},
                         headers=gen.HEADERS, timeout=15)
        a = (r.json() or {}).get("address", {})
    except Exception:  # noqa: BLE001
        return ""
    for key in ("city", "town", "village", "municipality", "borough",
                "city_district", "suburb", "county", "state", "country"):
        if a.get(key):
            return str(a[key])
    return ""

LAYER_ROWS = [
    ("buildings", "Buildings", False),
    ("roads",     "Roads",     True),
    ("rail",      "Rail",      True),
]
# suggested colour when a layer is split out of the grey base into its own object
LAYER_SUGGESTED = {
    "buildings": (232, 226, 214), "roads": (110, 105, 118),
    "rail": (80, 76, 88),
}
ZONE_ROWS = [
    ("water",    "Water (lakes / rivers)"),
    ("parks",    "Parks / grass"),
    ("forest",   "Forest / wood"),
    ("farmland", "Farmland / meadow"),
    ("urban",    "Urban area fill"),
]
ZONE_SUGGESTED = {
    "water": (45, 115, 215), "parks": (70, 165, 70), "forest": (40, 110, 45),
    "farmland": (210, 195, 120), "urban": (170, 165, 155),
}


def apply_theme(root):
    """Configure ttk so the whole app is rose pink on white cards over a warm
    grey page. Default widget background is SURFACE because almost everything
    lives inside a card; page-level widgets ask for the Page.* styles."""
    global UI
    UI = _ui_font(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(background=BG)
    base = (UI, 10)
    style.configure(".", background=SURFACE, foreground=SLATE,
                    fieldbackground=SURFACE, bordercolor=PINK_LINE,
                    lightcolor=SURFACE, darkcolor=SURFACE, focuscolor=PINK,
                    font=base)

    style.configure("TFrame", background=SURFACE)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("Page.TFrame", background=BG)
    style.configure("TLabel", background=SURFACE, foreground=SLATE, font=base)
    style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED,
                    font=(UI, 9))
    style.configure("Page.TLabel", background=BG, foreground=SLATE, font=base)
    style.configure("PageMuted.TLabel", background=BG, foreground=MUTED,
                    font=(UI, 9))
    style.configure("Head.TLabel", background=BG, foreground=SLATE,
                    font=(UI, 17, "bold"))
    style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(UI, 10))
    style.configure("CardTitle.TLabel", background=SURFACE, foreground=SLATE,
                    font=(UI, 11, "bold"))
    style.configure("Step.TLabel", background=SURFACE, foreground=PINK,
                    font=(UI, 9, "bold"))
    style.configure("Field.TLabel", background=SURFACE, foreground=MUTED,
                    font=(UI, 9))
    style.configure("Ok.TLabel", background=SURFACE, foreground="#2F8F5B")

    style.configure("TLabelframe", background=SURFACE, bordercolor=PINK_LINE,
                    relief="solid", borderwidth=1, padding=8)
    style.configure("TLabelframe.Label", background=SURFACE, foreground=PINK,
                    font=(UI, 9, "bold"))

    style.configure("TButton", background=PINK_SOFT, foreground=SLATE,
                    bordercolor=PINK_LINE, relief="flat", padding=(12, 7),
                    font=(UI, 9))
    style.map("TButton",
              background=[("active", "#F6DFE9"), ("pressed", PINK_LINE),
                          ("disabled", "#F3EFF1")],
              foreground=[("disabled", "#BDB4C2")])
    style.configure("Accent.TButton", background=PINK, foreground="#FFFFFF",
                    bordercolor=PINK, relief="flat", padding=(16, 9),
                    font=(UI, 10, "bold"))
    style.map("Accent.TButton",
              background=[("active", PINK_DEEP), ("pressed", PINK_DEEP),
                          ("disabled", "#EAC6D6")],
              foreground=[("disabled", "#FFF0F6")])
    style.configure("Ghost.TButton", background=SURFACE, foreground=MUTED,
                    bordercolor=PINK_LINE, relief="flat", padding=(10, 6),
                    font=(UI, 9))
    style.map("Ghost.TButton",
              background=[("active", PINK_SOFT), ("pressed", PINK_LINE)],
              foreground=[("active", SLATE)])
    style.configure("Tiny.TButton", background=PINK_SOFT, foreground=SLATE,
                    bordercolor=PINK_LINE, relief="flat", padding=(6, 4),
                    font=(UI, 9))
    style.map("Tiny.TButton", background=[("active", "#F6DFE9")])

    style.configure("TEntry", fieldbackground=SURFACE, foreground=SLATE,
                    bordercolor=PINK_LINE, insertcolor=PINK, padding=6)
    style.map("TEntry", bordercolor=[("focus", PINK)])
    style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE,
                    foreground=SLATE, bordercolor=PINK_LINE, arrowcolor=PINK,
                    padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)],
              background=[("readonly", SURFACE)], bordercolor=[("focus", PINK)])
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", SLATE)
    root.option_add("*TCombobox*Listbox.selectBackground", PINK)
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

    # clam's tick boxes: pink fill with a white mark when on, not a grey X
    for cb in ("TCheckbutton", "TRadiobutton"):
        style.configure(cb, background=SURFACE, foreground=SLATE, padding=3,
                        indicatorbackground=SURFACE, indicatorforeground=PINK,
                        upperbordercolor=PINK_LINE, lowerbordercolor=PINK_LINE,
                        indicatorsize=12, indicatormargin=(0, 0, 8, 0))
        style.map(cb,
                  background=[("active", SURFACE)],
                  indicatorbackground=[("selected", PINK), ("active", PINK_SOFT),
                                       ("disabled", "#F1EDF0")],
                  indicatorforeground=[("selected", "#FFFFFF")],
                  upperbordercolor=[("selected", PINK), ("active", PINK)],
                  lowerbordercolor=[("selected", PINK), ("active", PINK)])

    style.configure("TNotebook", background=BG, bordercolor=PINK_LINE,
                    tabmargins=(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                    padding=(16, 8), bordercolor=PINK_LINE, font=(UI, 9, "bold"))
    style.map("TNotebook.Tab", background=[("selected", SURFACE)],
              foreground=[("selected", PINK)],
              expand=[("selected", (0, 0, 0, 1))])
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=SLATE, bordercolor=PINK_LINE, rowheight=24)
    style.map("Treeview", background=[("selected", PINK)],
              foreground=[("selected", "#FFFFFF")])
    for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(sb, background=PINK_LINE, troughcolor=BG, relief="flat",
                        arrowcolor=PINK, bordercolor=BG, arrowsize=12)
        style.map(sb, background=[("active", PINK)])
    style.configure("TSeparator", background=PINK_LINE)
    style.configure("TPanedwindow", background=BG)
    return style


def make_card(parent, step, title, hint=""):
    """A white panel with a hairline border, a pink step chip and a title.
    Returns the frame to pack the card's contents into."""
    shell = tk.Frame(parent, background=SURFACE, highlightthickness=1,
                     highlightbackground=PINK_LINE, highlightcolor=PINK_LINE)
    shell.pack(fill="x", pady=(0, 12))
    body = ttk.Frame(shell, style="Card.TFrame", padding=(15, 13, 15, 15))
    body.pack(fill="both", expand=True)
    head = ttk.Frame(body, style="Card.TFrame")
    head.pack(fill="x", pady=(0, 11))
    if step:
        tk.Label(head, text=f"  {step}  ", background=PINK, foreground="#FFFFFF",
                 font=(UI, 8, "bold")).pack(side="left", pady=1)
    ttk.Label(head, text=title, style="CardTitle.TLabel").pack(
        side="left", padx=(8 if step else 0, 0))
    if hint:
        ttk.Label(head, text=hint, style="Muted.TLabel").pack(side="right")
    return body


# ---------------------------------------------------------------------------
# logging -> queue bridge
# ---------------------------------------------------------------------------

class QueueHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover
            msg = record.getMessage()
        self.q.put((record.levelname, msg))


# ---------------------------------------------------------------------------
# Leaflet map page: a viewfinder you CAPTURE, plus optional draw / pick tools
# ---------------------------------------------------------------------------

MAP_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>City Map -- capture area</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css"/>
<style>
 html,body{margin:0;height:100%;font:13px "Segoe UI",system-ui,sans-serif;color:#3A3440}
 #bar{padding:8px 12px;background:#FFFFFF;border-bottom:2px solid #EBC7D6;
      display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 #bar b{color:#D9548A;font-size:14px}
 #map{position:absolute;top:52px;bottom:0;left:0;right:0}
 #status{margin-left:auto;color:#8A8091;max-width:38%}
 label{cursor:pointer;user-select:none}
 select,input{font:13px "Segoe UI",sans-serif;padding:3px 5px;border:1px solid #EBC7D6;
              border-radius:4px;background:#fff;color:#3A3440}
 button{font:13px "Segoe UI",sans-serif;padding:4px 10px;cursor:pointer;
        border:1px solid #EBC7D6;border-radius:4px;background:#F7E4EC;color:#3A3440}
 button:hover{background:#F2D3E0}
 #cap{background:#D9548A;color:#fff;border-color:#D9548A;font-weight:600;padding:6px 16px}
 #cap:hover{background:#C6467A}
 #back{background:#EFE7F7;border-color:#CDBBE4;color:#5B4A78;font-weight:600}
 #back:hover{background:#E4D7F2}
 .hint{color:#8A8091}
</style></head><body>
<div id="bar">
  <b>City Map</b>
  <input id="q" placeholder="search a place" size="18">
  <button id="go">find</button>
  <span class="hint">aspect</span><select id="ratio"></select>
  <button id="cap">CAPTURE THIS VIEW</button>
  <button id="back" style="display:none">back to captured area</button>
  <label><input type="checkbox" id="bld"> pick detailed building</label>
  <button id="clear">clear</button>
  <span id="status">Pan / zoom so the pink frame holds what you want, then CAPTURE. Purple dashes = the area the app already has.</span>
</div>
<div id="map"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
<script>
const qs=new URLSearchParams(location.search);
const LAT=parseFloat(qs.get('lat')||'0'), LON=parseFloat(qs.get('lon')||'0');
const RATIO0=qs.get('ratio')||'free';
const RATIOS=['free','1:1','3:2','2:3','4:3','3:4','16:9','9:16'];
const rsel=document.getElementById('ratio');
RATIOS.forEach(r=>{const o=document.createElement('option');o.value=o.text=r;rsel.appendChild(o);});
if(RATIOS.includes(RATIO0)) rsel.value=RATIO0;

const map=L.map('map',{zoomSnap:0.2,zoomDelta:0.4,wheelPxPerZoomLevel:100}).setView([LAT,LON],15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
const drawn=new L.FeatureGroup().addTo(map);
map.addControl(new L.Control.Draw({
  draw:{polyline:false,circle:false,circlemarker:false,marker:false,
        rectangle:{shapeOptions:{color:'#D9548A',weight:2}},
        polygon:{allowIntersection:false,shapeOptions:{color:'#7A5AA6',weight:2}}},
  edit:{featureGroup:drawn,edit:false}}));

function setStatus(s){document.getElementById('status').textContent=s;}
function ratioVal(){const r=rsel.value;if(r==='free')return 0;
  const p=r.split(':');return parseFloat(p[0])/parseFloat(p[1]);}
function send(o){fetch('/pick',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(o)}).then(r=>r.json()).then(()=>setStatus('sent: '+o.type))
  .catch(e=>setStatus('send failed: '+e));}
function rectBbox(b){return [b.getSouth(),b.getWest(),b.getNorth(),b.getEast()];}
function polyPts(l){return l.getLatLngs()[0].map(p=>[p.lat,p.lng]);}
function polyBounds(p){const la=p.map(x=>x[0]),lo=p.map(x=>x[1]);
  return [Math.min.apply(0,la),Math.min.apply(0,lo),Math.max.apply(0,la),Math.max.apply(0,lo)];}

let finder=null, saved=null, savedPoly=null, savedLayer=null;
let locked=false, prog=false;
function finderBounds(){
  const b=map.getBounds(), c=b.getCenter(), r=ratioVal();
  const latR=c.lat*Math.PI/180, mLat=111320, mLon=111320*Math.cos(latR);
  let wM=(b.getEast()-b.getWest())*mLon*0.86;
  let hM=(b.getNorth()-b.getSouth())*mLat*0.86;
  if(r){ if(wM/hM > r) wM = hM*r; else hM = wM/r; }
  const dLat=(hM/2)/mLat, dLon=(wM/2)/mLon;
  return L.latLngBounds([c.lat-dLat,c.lng-dLon],[c.lat+dLat,c.lng+dLon]);
}
function drawFinder(){
  const b=(locked&&saved)?saved:finderBounds();
  if(finder) map.removeLayer(finder);
  finder=L.rectangle(b,{color:'#D9548A',weight:3,dashArray:'8 6',fill:false,
                        interactive:false}).addTo(map);
  return b;
}
map.on('moveend zoomend', drawFinder);

// ---- the area the app already captured: shown, and one click away --------
function drawSaved(){
  if(savedLayer) map.removeLayer(savedLayer);
  if(!saved) return;
  savedLayer=(savedPoly?L.polygon(savedPoly,{color:'#7A5AA6',weight:2,dashArray:'4 4',
                                             fill:false,interactive:false})
                      :L.rectangle(saved,{color:'#7A5AA6',weight:2,dashArray:'4 4',
                                          fill:false,interactive:false})).addTo(map);
}
function restore(){
  if(!saved) return;
  locked=true; prog=true;
  map.fitBounds(saved,{animate:false,padding:[24,24]});
  prog=false;
  drawSaved(); drawFinder();
  setStatus('locked to the area you captured -- CAPTURE sends back exactly that');
}
function unlock(){
  if(!locked) return;
  locked=false; drawFinder();
  setStatus('free -- press "back to captured area" to return to your capture');
}
map.on('dragstart',unlock);
map.on('zoomstart',()=>{ if(!prog) unlock(); });
document.getElementById('back').onclick=restore;
fetch('/state').then(r=>r.json()).then(j=>{
  if(!j||!j.bbox||j.bbox.length!==4) return;
  saved=L.latLngBounds([j.bbox[0],j.bbox[1]],[j.bbox[2],j.bbox[3]]);
  if(j.polygon&&j.polygon.length>=3) savedPoly=j.polygon;
  document.getElementById('back').style.display='';
  restore();
}).catch(()=>{});
rsel.onchange=()=>{drawFinder();setStatus('aspect '+rsel.value);};
drawFinder();

document.getElementById('cap').onclick=()=>{
  if(locked&&saved&&savedPoly){
    send({type:'area_poly',latlngs:savedPoly,bbox:rectBbox(saved)});
    setStatus('sent your captured shape back, unchanged');
    return;
  }
  const b=drawFinder();
  drawn.clearLayers();
  send({type:'capture',bbox:rectBbox(b),zoom:map.getZoom()});
  saved=b; savedPoly=null; locked=true;
  document.getElementById('back').style.display='';
  drawSaved();
  setStatus('captured -- switch back to the app');
};

function makeDraggable(layer,onEnd){
  let drag=false,last=null;
  layer.on('mousedown',ev=>{drag=true;last=ev.latlng;map.dragging.disable();L.DomEvent.stop(ev);});
  map.on('mousemove',ev=>{ if(!drag)return;
    const dLat=ev.latlng.lat-last.lat,dLng=ev.latlng.lng-last.lng; last=ev.latlng;
    if(layer instanceof L.Rectangle){const b=layer.getBounds();
      layer.setBounds([[b.getSouth()+dLat,b.getWest()+dLng],[b.getNorth()+dLat,b.getEast()+dLng]]);}
    else{layer.setLatLngs(layer.getLatLngs()[0].map(p=>[p.lat+dLat,p.lng+dLng]));}
  });
  map.on('mouseup',()=>{ if(drag){drag=false;map.dragging.enable();onEnd(layer);} });
}
function snap(b){const r=ratioVal();if(!r)return b;
  const c=b.getCenter(),latR=c.lat*Math.PI/180,mLat=111320,mLon=111320*Math.cos(latR);
  const wM=(b.getEast()-b.getWest())*mLon,hM=wM/r;
  const dLat=(hM/2)/mLat,dLon=(wM/2)/mLon;
  return L.latLngBounds([c.lat-dLat,c.lng-dLon],[c.lat+dLat,c.lng+dLon]);}
map.on(L.Draw.Event.CREATED,e=>{
  drawn.clearLayers();
  if(e.layerType==='rectangle'){
    let b=snap(e.layer.getBounds());
    unlock(); saved=b; savedPoly=null; drawSaved();
    document.getElementById('back').style.display='';
    const rect=L.rectangle(b,{color:'#D9548A',weight:2}).addTo(drawn);
    send({type:'capture',bbox:rectBbox(b)});
    makeDraggable(rect,l=>{const bb=snap(l.getBounds());l.setBounds(bb);
      send({type:'capture',bbox:rectBbox(bb)});});
  } else if(e.layerType==='polygon'){
    e.layer.addTo(drawn);
    const p=polyPts(e.layer);
    unlock(); savedPoly=p;
    const pb=polyBounds(p);
    saved=L.latLngBounds([pb[0],pb[1]],[pb[2],pb[3]]);
    document.getElementById('back').style.display='';
    send({type:'area_poly',latlngs:p,bbox:pb});
    makeDraggable(e.layer,l=>{const q2=polyPts(l);
      send({type:'area_poly',latlngs:q2,bbox:polyBounds(q2)});});
  }
});
map.on('click',e=>{
  if(!document.getElementById('bld').checked)return;
  L.circleMarker(e.latlng,{radius:6,color:'#7A5AA6'}).addTo(drawn);
  send({type:'building',lat:e.latlng.lat,lon:e.latlng.lng});
});
document.getElementById('clear').onclick=()=>{drawn.clearLayers();setStatus('cleared');};
function doSearch(){const t=document.getElementById('q').value.trim();if(!t)return;
  setStatus('searching...');
  fetch('/geocode?q='+encodeURIComponent(t)).then(r=>r.json()).then(j=>{
    if(j&&j.lat){map.setView([j.lat,j.lon],16);drawFinder();setStatus('found: '+(j.name||t));}
    else setStatus('not found: '+t);
  }).catch(e=>setStatus('search error: '+e));}
document.getElementById('go').onclick=doSearch;
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch();});
</script></body></html>
"""


class LeafletPicker:
    """Serves the map page on 127.0.0.1 and funnels /pick POSTs into a queue."""

    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.thread = None
        self.port = None

    def open(self, lat, lon, ratio):
        if self.httpd is None:
            self._start()
        url = f"http://127.0.0.1:{self.port}/?lat={lat:.6f}&lon={lon:.6f}&ratio={ratio}"
        webbrowser.open(url)
        return url

    def _start(self):
        picker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                b = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if b:
                    self.wfile.write(b)

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send(200, MAP_HTML)
                elif path == "/state":
                    self._send(200, json.dumps(picker.app.saved_area()),
                               "application/json")
                elif path == "/geocode":
                    q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                    self._send(200, json.dumps(_geocode(q)), "application/json")
                else:
                    self._send(204, b"")

            def do_POST(self):
                if self.path != "/pick":
                    self._send(404, b"")
                    return
                n = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    data = None
                if isinstance(data, dict):
                    picker.app.map_pick_q.put(data)
                self._send(200, b'{"ok":true}', "application/json")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self.httpd = None


def _geocode(q: str) -> dict:
    q = (q or "").strip()
    if not q:
        return {}
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "json", "limit": 1},
                         headers=gen.HEADERS, timeout=15)
        js = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if not js:
        return {}
    return {"lat": float(js[0]["lat"]), "lon": float(js[0]["lon"]),
            "name": js[0].get("display_name", "")}


def _element_polygon(el):
    """Shapely polygon (lon/lat) from an Overpass ``out geom`` element."""
    try:
        if el.get("type") == "way":
            pts = [(g["lon"], g["lat"]) for g in el.get("geometry", []) if g]
            if len(pts) < 3:
                return None
            p = Polygon(pts)
            return p if p.is_valid else p.buffer(0)
        if el.get("type") == "relation":
            lines = []
            for m in el.get("members", []):
                if m.get("type") != "way" or m.get("role") not in ("outer", "", None):
                    continue
                g = [(q["lon"], q["lat"]) for q in m.get("geometry", []) if q]
                if len(g) >= 2:
                    lines.append(g)
            if not lines:
                return None
            polys = list(polygonize(lines))
            if polys:
                u = unary_union(polys)
                return u if not u.is_empty else None
            closed = [Polygon(g) for g in lines if len(g) >= 4 and g[0] == g[-1]]
            if closed:
                u = unary_union([c.buffer(0) for c in closed])
                return u if not u.is_empty else None
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# The "screenshot": stitch OSM raster tiles for the captured bbox
# ---------------------------------------------------------------------------

OSM_TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def _lonlat_to_tile(lat, lon, z):
    n = 2 ** z
    xt = (lon + 180.0) / 360.0 * n
    yt = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return xt, yt


def fetch_map_image(bbox, max_tiles=25, timeout=20):
    """Stitch OpenStreetMap tiles covering ``bbox`` and crop to it.
    Returns a PIL image, or None if it could not be built."""
    if not HAVE_PIL:
        return None
    south, west, north, east = bbox
    z = 12
    for zz in range(19, 5, -1):
        x0, y0 = _lonlat_to_tile(north, west, zz)
        x1, y1 = _lonlat_to_tile(south, east, zz)
        if abs(x1 - x0) <= 4.0 and abs(y1 - y0) <= 4.0:
            z = zz
            break
    x0f, y0f = _lonlat_to_tile(north, west, z)
    x1f, y1f = _lonlat_to_tile(south, east, z)
    tx0, tx1 = int(math.floor(min(x0f, x1f))), int(math.floor(max(x0f, x1f)))
    ty0, ty1 = int(math.floor(min(y0f, y1f))), int(math.floor(max(y0f, y1f)))
    cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
    if cols * rows > max_tiles:
        return None
    canvas = Image.new("RGB", (cols * 256, rows * 256), (245, 243, 244))
    ua = {"User-Agent": gen.HEADERS["User-Agent"]}
    for j in range(rows):
        for i in range(cols):
            url = OSM_TILE.format(z=z, x=tx0 + i, y=ty0 + j)
            try:
                r = requests.get(url, headers=ua, timeout=timeout)
                r.raise_for_status()
                canvas.paste(Image.open(io.BytesIO(r.content)).convert("RGB"),
                             (i * 256, j * 256))
            except Exception:  # noqa: BLE001
                continue
    left = (min(x0f, x1f) - tx0) * 256.0
    right = (max(x0f, x1f) - tx0) * 256.0
    top = (min(y0f, y1f) - ty0) * 256.0
    bottom = (max(y0f, y1f) - ty0) * 256.0
    box = (max(int(left), 0), max(int(top), 0),
           min(int(math.ceil(right)), canvas.width),
           min(int(math.ceil(bottom)), canvas.height))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return canvas
    return canvas.crop(box)


def parse_bbox_text(text):
    """Pull a bounding box out of pasted text. Understands

        S=41.887898 W=12.489375 N=41.893417 E=12.495456
        41.887898 N, 12.489375 E, 41.893417 N, 12.495456 E
        41.887898, 12.489375, 41.893417, 12.495456      (south, west, north, east)

    and returns (south, west, north, east) with the corners sorted, or None.
    A bare "lat, lon" pair is a point, not a box -- see :func:`parse_point_text`.
    """
    t = (text or "").replace("−", "-").replace("°", " ")
    named = {}
    for pat in (r"(?<![A-Za-z])([NSEW])\s*[=:]\s*(-?\d+(?:\.\d+)?)",
                r"(?<![A-Za-z])([NSEW])\s+(-?\d+(?:\.\d+)?)",
                r"(-?\d+(?:\.\d+)?)\s*([NSEW])(?![A-Za-z])"):
        for m in re.finditer(pat, t, re.I):
            a, b = m.group(1), m.group(2)
            key, val = (a, b) if a.isalpha() else (b, a)
            named.setdefault(key.lower(), float(val))
        if len(named) >= 4:
            break
    if {"s", "w", "n", "e"} <= set(named):
        vals = (named["s"], named["w"], named["n"], named["e"])
    else:
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", t)]
        if len(nums) < 4:
            return None
        vals = tuple(nums[:4])
    south, west, north, east = vals
    south, north = min(south, north), max(south, north)
    west, east = min(west, east), max(west, east)
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        return None
    return (south, west, north, east)


def parse_point_text(text):
    """A single "lat, lon" out of pasted text (what Google Maps copies), or
    None. Only accepts exactly two numbers so it can't eat a bounding box."""
    nums = [float(x) for x in
            re.findall(r"-?\d+(?:\.\d+)?", (text or "").replace("−", "-"))]
    if len(nums) != 2:
        return None
    lat, lon = nums
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def bbox_around(lat, lon, width_m, height_m):
    """A bounding box of the given real size centred on a point."""
    dlat = (height_m / 2.0) / 111320.0
    dlon = (width_m / 2.0) / max(111320.0 * math.cos(math.radians(lat)), 1e-6)
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def bbox_size_m(bbox):
    south, west, north, east = bbox
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians((south + north) / 2.0))
    return (east - west) * mlon, (north - south) * mlat


# ---------------------------------------------------------------------------
# 3D preview (pure PIL isometric renderer -- no extra dependencies)
# ---------------------------------------------------------------------------

def render_iso(parts, w=780, h=560, az_deg=35.0, el_deg=28.0, bg=(46, 42, 53),
               zoom=1.0, min_px=0.02, max_area_px=700.0):
    """parts: list of (vertices (N,3), faces (M,3), (r,g,b)) -> PIL image.

    A painter's-algorithm renderer. Two things keep it quick enough to drag:
    back faces are dropped (they can never be seen on a closed solid) and so is
    anything that lands on less than ``min_px`` pixels. Raise ``min_px`` while
    the user is moving the model and drop it again when they stop -- a whole
    country's relief has hundreds of thousands of triangles barely a pixel
    across, and culling those for the final picture puts holes in it.
    """
    az, el = math.radians(az_deg), math.radians(el_deg)
    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)

    def rot(v):
        x, y, z = v[:, 0], v[:, 1], v[:, 2]
        x1 = ca * x - sa * y
        y1 = sa * x + ca * y
        y2 = ce * y1 - se * z
        z2 = se * y1 + ce * z
        return np.column_stack([x1, y2, z2])

    rotated = [(rot(np.asarray(vv, dtype=float)), np.asarray(ff), rgb)
               for vv, ff, rgb in parts if len(vv) and len(ff)]
    if not rotated:
        return Image.new("RGB", (w, h), bg)
    allv = np.vstack([rv for rv, _f, _c in rotated])
    mnx, mny = allv[:, 0].min(), allv[:, 1].min()
    mxx, mxy = allv[:, 0].max(), allv[:, 1].max()
    cx, cy = (mnx + mxx) / 2.0, (mny + mxy) / 2.0
    s = min((w - 40) / max(mxx - mnx, 1e-6),
            (h - 40) / max(mxy - mny, 1e-6)) * max(zoom, 0.05)
    light = np.array([0.35, 0.45, 0.82])
    light /= np.linalg.norm(light)

    def sxy(P):
        return w / 2.0 + (P[..., 0] - cx) * s, h / 2.0 - (P[..., 1] - cy) * s

    chunks = []                                   # (A, B, C, colour) per part
    for rv, ff, rgb in rotated:
        A, B, C = rv[ff[:, 0]], rv[ff[:, 1]], rv[ff[:, 2]]
        n = np.cross(B - A, C - A)
        front = n[:, 2] > 0
        # a mesh whose winding is inside-out would vanish completely, so only
        # cull when a sane share of its faces actually points at us
        if front.sum() >= 0.05 * len(front):
            A, B, C = A[front], B[front], C[front]
        # the base plate is two enormous triangles; the depth sort works per
        # triangle, so anything covering a lot of screen has to be split up
        stack, keep = [(A, B, C, 0)], []
        while stack:
            A, B, C, lvl = stack.pop()
            if len(A) == 0:
                continue
            ax, ay = sxy(A)
            bx, by = sxy(B)
            cx_, cy_ = sxy(C)
            area = np.abs((bx - ax) * (cy_ - ay) - (cx_ - ax) * (by - ay)) * 0.5
            big = (area > max_area_px) if lvl < 5 else np.zeros(len(A), bool)
            if big.any():
                i = np.nonzero(big)[0]
                ab, bc, caa = (A[i] + B[i]) / 2, (B[i] + C[i]) / 2, (C[i] + A[i]) / 2
                stack += [(A[i], ab, caa, lvl + 1), (ab, B[i], bc, lvl + 1),
                          (caa, bc, C[i], lvl + 1), (ab, bc, caa, lvl + 1)]
                k = np.nonzero(~big)[0]
                if len(k) == 0:
                    continue
                A, B, C, area = A[k], B[k], C[k], area[k]
            vis = area >= min_px                  # too small to see -> skip it
            if vis.any():
                keep.append((A[vis], B[vis], C[vis]))
        if keep:
            chunks.append(([np.vstack([k[i] for k in keep]) for i in range(3)],
                           np.array(rgb, dtype=float)))

    img = Image.new("RGB", (w, h), bg)
    if not chunks:
        return img
    A = np.vstack([c[0][0] for c in chunks])
    B = np.vstack([c[0][1] for c in chunks])
    C = np.vstack([c[0][2] for c in chunks])
    base = np.vstack([np.repeat(c[1][None, :], len(c[0][0]), axis=0) for c in chunks])

    n = np.cross(B - A, C - A)
    nl = np.linalg.norm(n, axis=1)
    nl[nl == 0] = 1.0
    shade = 0.32 + 0.68 * np.clip(np.abs((n / nl[:, None]) @ light), 0, 1)
    cols = np.clip(shade[:, None] * base, 0, 255).astype(np.uint8)
    ax, ay = sxy(A)
    bx, by = sxy(B)
    cx_, cy_ = sxy(C)
    xy = np.column_stack([ax, ay, bx, by, cx_, cy_])
    order = np.argsort((A[:, 2] + B[:, 2] + C[:, 2]))     # far to near

    dr = ImageDraw.Draw(img)
    poly = dr.polygon
    xy_l, col_l = xy.tolist(), cols.tolist()
    for i in order.tolist():
        poly(xy_l[i], fill=tuple(col_l[i]))
    return img


def load_preview_parts(paths, main_path, colmap):
    """Turn exported files into render_iso parts. Prefers the split objects so
    each group keeps its colour; falls back to the combined mesh."""
    import trimesh
    grey = (198, 196, 202)
    parts = []
    paths = [p for p in (paths or []) if p]
    base_stl = next((p for p in paths
                     if p.lower().endswith("_base.stl") and os.path.isfile(p)), None)
    if base_stl:
        m = trimesh.load(base_stl, force="mesh")
        parts.append((np.asarray(m.vertices), np.asarray(m.faces), grey))
        for p in paths:
            low = p.lower()
            if not low.endswith(".stl") or low.endswith("_base.stl"):
                continue
            if main_path and os.path.abspath(p) == os.path.abspath(main_path):
                continue
            if not os.path.isfile(p):
                continue
            key = os.path.splitext(os.path.basename(p))[0].rsplit("_", 1)[-1].lower()
            mm = trimesh.load(p, force="mesh")
            if len(mm.vertices):
                parts.append((np.asarray(mm.vertices), np.asarray(mm.faces),
                              colmap.get(key, (216, 140, 80))))
        if parts:
            return parts
    for cand in ([main_path] if main_path else []) + paths:
        if cand and os.path.isfile(cand) and cand.lower().endswith((".stl", ".3mf")):
            try:
                m = trimesh.load(cand, force="mesh")
            except Exception:  # noqa: BLE001
                continue
            if hasattr(m, "vertices") and len(m.vertices):
                return [(np.asarray(m.vertices), np.asarray(m.faces), grey)]
    return parts


# ---------------------------------------------------------------------------
# Printed backdrop: the same map, wider, on paper, with a hole where the 3D
# print goes. Printed at the model's own scale the streets carry straight on
# off the plastic and onto the page.
# ---------------------------------------------------------------------------

PAPERS = {"A4": (210.0, 297.0), "A3": (297.0, 420.0), "A5": (148.0, 210.0),
          "Letter": (215.9, 279.4), "Square 200": (200.0, 200.0)}
POSTER_DPI = (150, 200, 300)

ESRI_IMAGERY = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery/MapServer/tile/{z}/{y}/{x}")
OSM_CREDIT = "Map data (c) OpenStreetMap contributors"

ESRI_CREDIT = "Imagery (c) Esri, Maxar, Earthstar Geographics"
# style -> (with labels, without labels). A None url means "draw it here from
# the OSM data" -- see render_vector_backdrop.
POSTER_STYLES = {
    "drawn": ((OSM_TILE, OSM_CREDIT), (None, OSM_CREDIT)),
    "realistic": ((ESRI_IMAGERY, ESRI_CREDIT), (ESRI_IMAGERY, ESRI_CREDIT)),
}
CUT_MODES = ("blank", "outline", "keep")


def _merc(lat):
    return math.asinh(math.tan(math.radians(lat)))


def latlon_to_px(bbox, w, h, lat, lon):
    """Where a coordinate lands in an image cropped exactly to ``bbox``."""
    south, west, north, east = bbox
    x = (lon - west) / max(east - west, 1e-12) * w
    y = ((_merc(north) - _merc(lat))
         / max(_merc(north) - _merc(south), 1e-12) * h)
    return x, y


def _pick_zoom(bbox, want_px, max_tiles):
    """Smallest zoom that gives ``want_px`` across, without blowing the tile
    budget. Falls back to the most detailed zoom that does fit."""
    south, west, north, east = bbox
    fallback = 3
    for z in range(3, 20):
        x0, y0 = _lonlat_to_tile(north, west, z)
        x1, y1 = _lonlat_to_tile(south, east, z)
        cols = int(math.floor(max(x0, x1))) - int(math.floor(min(x0, x1))) + 1
        rows = int(math.floor(max(y0, y1))) - int(math.floor(min(y0, y1))) + 1
        if cols * rows > max_tiles:
            break
        fallback = z
        if abs(x1 - x0) * 256 >= want_px:
            return z
    return fallback


def stitch_tiles(bbox, url_tpl, want_px=1600, max_tiles=240, timeout=25,
                 progress=None, workers=5):
    """Mosaic web-mercator tiles over ``bbox`` and crop to it. Returns
    (image, zoom, tiles_missing)."""
    if not HAVE_PIL:
        return None, 0, 0
    south, west, north, east = bbox
    z = _pick_zoom(bbox, want_px, max_tiles)
    x0f, y0f = _lonlat_to_tile(north, west, z)
    x1f, y1f = _lonlat_to_tile(south, east, z)
    tx0, tx1 = int(math.floor(min(x0f, x1f))), int(math.floor(max(x0f, x1f)))
    ty0, ty1 = int(math.floor(min(y0f, y1f))), int(math.floor(max(y0f, y1f)))
    cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
    canvas = Image.new("RGB", (cols * 256, rows * 256), (250, 249, 250))
    ua = {"User-Agent": gen.HEADERS["User-Agent"]}
    jobs = [(i, j) for j in range(rows) for i in range(cols)]
    if progress:
        progress(f"zoom {z}: {cols} x {rows} = {len(jobs)} tiles")

    missing = 0
    done = [0]

    def grab(ij):
        i, j = ij
        url = url_tpl.format(z=z, x=tx0 + i, y=ty0 + j)
        try:
            r = requests.get(url, headers=ua, timeout=timeout)
            r.raise_for_status()
            return i, j, Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:  # noqa: BLE001
            return i, j, None

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for i, j, tile in pool.map(grab, jobs):
            done[0] += 1
            if tile is None:
                missing += 1
            else:
                canvas.paste(tile, (i * 256, j * 256))
            if progress and done[0] % 25 == 0:
                progress(f"  {done[0]}/{len(jobs)} tiles")

    left, right = (min(x0f, x1f) - tx0) * 256.0, (max(x0f, x1f) - tx0) * 256.0
    top, bottom = (min(y0f, y1f) - ty0) * 256.0, (max(y0f, y1f) - ty0) * 256.0
    box = (max(int(round(left)), 0), max(int(round(top)), 0),
           min(int(round(right)), canvas.width),
           min(int(round(bottom)), canvas.height))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return canvas, z, missing
    return canvas.crop(box), z, missing


# The label-free "drawn" backdrop is rendered here from the same OpenStreetMap
# data the model is built from, rather than fetched as picture tiles. Every
# keyless label-free tile service now wants an API key, and drawing it locally
# is better anyway: the paper ends up in the same palette as the plate, so the
# streets really do carry on off the print.

BACKDROP_PALETTE = {
    "paper": (253, 252, 253), "urban": (244, 241, 243),
    "farmland": (243, 239, 226), "forest": (211, 228, 210),
    "parks": (224, 236, 221), "water": (205, 223, 240),
    "road_edge": (228, 223, 229), "roads": (255, 255, 255),
    "rail": (206, 201, 209), "buildings": (228, 222, 228),
    "building_edge": (209, 201, 210),
}
# desaturating the colour palette turns the page almost black, which is no
# use on paper -- black and white gets its own set of light greys instead
BACKDROP_MONO = {
    "paper": (255, 255, 255), "urban": (243, 243, 243), "farmland": (240, 240, 240),
    "forest": (226, 226, 226), "parks": (235, 235, 235), "water": (222, 222, 222),
    "road_edge": (206, 206, 206), "roads": (255, 255, 255), "rail": (198, 198, 198),
    "buildings": (228, 228, 228), "building_edge": (196, 196, 196),
}
BACKDROP_LAYERS = ("urban", "farmland", "forest", "parks", "water")


BACKDROP_SWATCHES = (
    ("paper", "Background"), ("urban", "Built-up"), ("parks", "Parks"),
    ("forest", "Forest"), ("farmland", "Farmland"), ("water", "Water"),
    ("roads", "Roads"), ("road_edge", "Road edge"), ("rail", "Rail"),
    ("buildings", "Buildings"), ("building_edge", "Building edge"),
)


def shade(rgb, f):
    """Darken (f < 1) or lighten (f > 1) a colour, staying in range."""
    return tuple(max(0, min(255, int(round(c * f)))) for c in rgb)


def fade_to_white(img, amount):
    """Blend a map toward the page so the model stays the loud thing on it."""
    amount = max(0.0, min(float(amount), 0.95))
    if amount <= 0:
        return img
    return Image.blend(img, Image.new("RGB", img.size, (255, 255, 255)), amount)


def render_vector_backdrop(bbox, w_px, h_px, *, mono=False, palette=None,
                           fetch=None, progress=None, config=None):
    """Draw ``bbox`` as a flat map with no writing on it at all, straight from
    Overpass. Returns a PIL image exactly ``w_px`` x ``h_px``."""
    if not HAVE_PIL:
        raise RuntimeError("the backdrop needs Pillow (pip install pillow)")
    # black-and-white means black-and-white: a colour theme handed in alongside
    # it would otherwise put the colour straight back
    pal = dict(BACKDROP_MONO) if mono else dict(BACKDROP_PALETTE, **(palette or {}))
    cfg = config or gen.PipelineConfig(bbox=tuple(bbox))
    if progress:
        progress("fetching map data for the backdrop ...")
    osm = (fetch or gen.fetch_map_data)(bbox, cfg)
    feats = gen.parse_features(osm, cfg)
    if progress:
        progress("  " + ", ".join(f"{k}={len(v)}" for k, v in feats.items() if v))

    ss = 2 if w_px * h_px <= 6_000_000 else 1        # supersample for clean edges
    W, H = max(int(w_px * ss), 1), max(int(h_px * ss), 1)
    img = Image.new("RGB", (W, H), pal["paper"])
    dr = ImageDraw.Draw(img)
    width_m, _height_m = bbox_size_m(bbox)
    px_per_m = W / max(width_m, 1e-6)

    def to_px(coords):
        return [latlon_to_px(bbox, W, H, la, lo) for la, lo in coords]

    def area(feat, fill, outline=None, wid=1):
        pts = to_px(feat["coords"])
        if len(pts) < 3:
            return
        dr.polygon(pts, fill=fill, outline=outline, width=wid)
        for ring in feat.get("holes", []) or []:     # courtyards, islands
            hp = to_px(ring)
            if len(hp) >= 3:
                dr.polygon(hp, fill=pal["paper"])

    for name in BACKDROP_LAYERS:
        for feat in feats.get(name, []):
            area(feat, pal.get(name, pal["urban"]))

    for feat in feats.get("rail", []):
        pts = to_px(feat["coords"])
        if len(pts) >= 2:
            dr.line(pts, fill=pal["rail"],
                    width=max(int(2.5 * px_per_m), 1), joint="curve")

    roads = feats.get("roads", [])
    for pass_no, (colour, extra) in enumerate(((pal["road_edge"], 1.9),
                                               (pal["roads"], 0.0))):
        for feat in roads:                            # cased: edge, then fill
            pts = to_px(feat["coords"])
            if len(pts) < 2:
                continue
            wm = float(feat.get("width_m", 6.0))
            dr.line(pts, fill=colour,
                    width=max(int((wm + extra) * px_per_m), 1 + (pass_no == 0)),
                    joint="curve")

    for feat in feats.get("buildings", []):
        area(feat, pal["buildings"], outline=pal["building_edge"],
             wid=max(int(0.6 * px_per_m), 1))

    if ss != 1:
        img = img.resize((int(w_px), int(h_px)), Image.LANCZOS)
    return img

def poster_bbox(bbox, model_w_mm, sheet_w_mm, sheet_h_mm, wider=1.0):
    """The area a sheet covers when it is printed at the model's own scale --
    so what is on the paper lines up with what is on the plate."""
    cw_m, _ch_m = bbox_size_m(bbox)
    m_per_mm = (cw_m / max(model_w_mm, 1e-6)) * max(wider, 0.05)
    lat = (bbox[0] + bbox[2]) / 2.0
    lon = (bbox[1] + bbox[3]) / 2.0
    return (bbox_around(lat, lon, sheet_w_mm * m_per_mm, sheet_h_mm * m_per_mm),
            m_per_mm)


# ---------------------------------------------------------------------------
# The marker in one corner of the backdrop. Point a phone at it and the
# model can be shown standing on the paper.
#
# Two kinds, and they work in completely different ways. An ArUco square
# (the default) is a fiducial: a detector finds the black square, reads the
# bits inside it and works out exactly how it is lying, so nothing has to be
# trained and 15-20 mm of paper is enough. The other styles are image
# targets for MindAR, which matches *natural features* instead -- a good one
# is busy, high in contrast and never repeats itself, and it has to be
# compiled into a .mind file before it will track.
# ---------------------------------------------------------------------------

MARKER_CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")
MARKER_STYLES = ("aruco", "legend", "compass", "pattern", "map area")
ARUCO_DEFAULT_MM = 20.0        # an ArUco square this small reads fine
OPPOSITE_CORNER = {"bottom-right": "top-left", "top-left": "bottom-right",
                   "bottom-left": "top-right", "top-right": "bottom-left"}
# Not every one of the 64 markers is worth printing small. These were picked by
# rendering all of them at 11-24 px with tilt and noise and keeping the ones the
# detector always read, then thinning that down so no two are closer than 6 of
# their 16 bits in any rotation -- so an anchor is never mistaken for its helper
# even when both are a smudge. Anchor i pairs with the one half a list away.
ROBUST_ARUCO_IDS = [3, 6, 12, 13, 18, 19, 20, 21, 22, 24, 31, 49]
ARUCO_PAIR_STEP = 6


def second_aruco_id(first_id):
    """The id printed in the opposite corner. Always a different marker, and
    always one that is hard to confuse with the first."""
    lst = ROBUST_ARUCO_IDS
    if int(first_id) in lst:
        i = lst.index(int(first_id))
        return lst[(i + ARUCO_PAIR_STEP) % len(lst)]
    return (int(first_id) + ARUCO_PAIR_STEP) % len(ARUCO_4X4_BYTES)


def pick_aruco_id(seed):
    """The anchor square for a sheet.

    Only the first half of the list may be an anchor; the second half are
    helpers. That way the id alone says which corner a square belongs in, so a
    scanner that can see only one of the two still knows how the sheet is laid
    out instead of having to guess -- guessing puts the picture in the wrong
    corner half the time.
    """
    return ROBUST_ARUCO_IDS[int(seed) % ARUCO_PAIR_STEP]

# ---------------------------------------------------------------------------
# ArUco marker: a small black square with a 4x4 binary grid inside it.
#
# Nothing has to be trained or compiled -- a detector finds the square, reads
# the bits and knows both which marker it is and exactly how it is lying, so a
# 20 mm square is enough. The codes below are OpenCV's own DICT_4X4_1000 table,
# so any standard ArUco reader (and the scanner in web/) agrees with what we
# print.
# ---------------------------------------------------------------------------

# OpenCV DICT_4X4_1000, first 64 ids, as (byte0, byte1) pairs.
# Source: js-aruco2's copy of the OpenCV dictionary, 3-clause BSD,
# Copyright (C) 2013 OpenCV Foundation. Kept here so the printed marker and
# the browser's detector cannot drift apart.
ARUCO_4X4_BYTES = (
    (181, 50), (15, 154), (51, 45), (153, 70), (84, 158), (121, 205), (158, 46), (196, 242),
    (254, 218), (207, 86), (249, 145), (17, 167), (14, 183), (42, 15), (36, 177), (38, 62),
    (70, 101), (102, 0), (108, 94), (118, 175), (134, 139), (176, 43), (204, 213), (221, 130),
    (254, 71), (148, 113), (172, 228), (165, 84), (33, 35), (52, 111), (68, 21), (87, 178),
    (158, 207), (240, 203), (8, 174), (9, 41), (24, 117), (4, 255), (13, 246), (28, 90),
    (23, 24), (42, 40), (50, 140), (56, 178), (36, 232), (46, 235), (45, 63), (75, 100),
    (80, 46), (80, 19), (81, 148), (85, 104), (93, 65), (95, 151), (104, 1), (104, 103),
    (97, 36), (97, 233), (107, 18), (111, 229), (103, 223), (126, 27), (128, 160), (131, 68),
)
ARUCO_BITS = 4                       # 4 x 4 payload
ARUCO_CELLS = ARUCO_BITS + 2         # plus the black quiet border the spec needs


def aruco_bit_grid(marker_id):
    """The 4x4 payload of a marker as rows of 0/1, 1 meaning white.

    js-aruco2 flattens the grid row by row and compares it against the
    dictionary string, so this is the order that has to match."""
    idx = int(marker_id) % len(ARUCO_4X4_BYTES)
    b0, b1 = ARUCO_4X4_BYTES[idx]
    bits = f"{b0:08b}{b1:08b}"
    return [[int(bits[r * ARUCO_BITS + c]) for c in range(ARUCO_BITS)]
            for r in range(ARUCO_BITS)]


def make_aruco_image(marker_id, size_px=480, quiet=1):
    """The marker as a black-and-white image, ``quiet`` extra white cells all
    round so it never touches the map."""
    if not HAVE_PIL:
        raise RuntimeError("the marker needs Pillow (pip install pillow)")
    grid = aruco_bit_grid(marker_id)
    cells = ARUCO_CELLS + 2 * int(quiet)
    scale = max(int(size_px) // cells, 1)
    W = cells * scale
    im = Image.new("L", (W, W), 255)
    d = ImageDraw.Draw(im)
    off = int(quiet)
    # the black border is part of the marker, not decoration
    d.rectangle((off * scale, off * scale,
                 (off + ARUCO_CELLS) * scale - 1,
                 (off + ARUCO_CELLS) * scale - 1), fill=0)
    for r in range(ARUCO_BITS):
        for c in range(ARUCO_BITS):
            if grid[r][c]:
                x = (off + 1 + c) * scale
                y = (off + 1 + r) * scale
                d.rectangle((x, y, x + scale - 1, y + scale - 1), fill=255)
    if W != int(size_px):
        im = im.resize((int(size_px), int(size_px)), Image.NEAREST)
    return im.convert("RGB")


def read_aruco_image(img, quiet_hint=None):
    """Decode a square, axis-aligned marker image the way js-aruco2 does:
    reject it unless the border is dark, sample the inner grid, then try all
    four rotations against the dictionary. Returns (id, rotation) or None.

    This is what lets the printed marker be checked without a camera."""
    g = np.asarray(img.convert("L"), dtype=float)
    n = min(g.shape)
    g = g[:n, :n]
    # find the marker inside whatever quiet zone surrounds it
    dark = g < 128
    rows = np.nonzero(dark.any(axis=1))[0]
    cols = np.nonzero(dark.any(axis=0))[0]
    if not len(rows) or not len(cols):
        return None
    g = g[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    step = min(g.shape) / ARUCO_CELLS

    def cell(r, c):
        y0, y1 = int(r * step), int((r + 1) * step)
        x0, x1 = int(c * step), int((c + 1) * step)
        patch = g[y0 + 1:max(y1 - 1, y0 + 2), x0 + 1:max(x1 - 1, x0 + 2)]
        return 1 if patch.mean() > 127 else 0

    for r in range(ARUCO_CELLS):                 # the border must be black
        for c in range(ARUCO_CELLS):
            if (r in (0, ARUCO_CELLS - 1) or c in (0, ARUCO_CELLS - 1)) \
                    and cell(r, c):
                return None
    bits = [[cell(r + 1, c + 1) for c in range(ARUCO_BITS)]
            for r in range(ARUCO_BITS)]

    def rot(m):                                  # js-aruco2's rotate()
        return [[m[len(m[i]) - j - 1][i] for j in range(len(m[i]))]
                for i in range(len(m))]

    table = {"".join(f"{b0:08b}{b1:08b}"): i
             for i, (b0, b1) in enumerate(ARUCO_4X4_BYTES)}
    for turn in range(4):
        key = "".join(str(b) for row in bits for b in row)
        if key in table:
            return table[key], turn
        bits = rot(bits)
    return None

MARKER_INK_DARK = (34, 30, 38)
MARKER_GREY = (120, 116, 126)
# a printed target below this many features per megapixel is asking to be
# unreliable; the loud pattern sits around 1500 for comparison
MARKER_WEAK_BELOW = 300.0


def _mk_font(px, bold=True):
    names = (("arialbd.ttf", "seguisb.ttf", "DejaVuSans-Bold.ttf") if bold
             else ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"))
    for n in names:
        try:
            return ImageFont.truetype(n, max(int(px), 6))
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _box_mean(a, r):
    """Mean over a (2r+1) window, from an integral image -- no scipy."""
    p = np.pad(a, r + 1, mode="edge")
    s = p.cumsum(0).cumsum(1)
    n = 2 * r + 1
    h, w = a.shape
    y, x = np.arange(h) + n, np.arange(w) + n
    y0, x0 = y - n, x - n
    return (s[np.ix_(y, x)] - s[np.ix_(y0, x)] - s[np.ix_(y, x0)]
            + s[np.ix_(y0, x0)]) / float(n * n)


def marker_strength(img, cell=16, keep_frac=0.02):
    """Roughly how many trackable features an image offers, per megapixel.

    MindAR matches corners, so this counts them the way a detector would --
    Shi-Tomasi response, one candidate per cell so a single busy blob cannot
    masquerade as detail spread over the whole target."""
    g = np.asarray(img.convert("L"), dtype=np.float64) / 255.0
    if min(g.shape) < 32:
        return 0.0
    iy, ix = np.gradient(g)
    sxx, syy, sxy = (_box_mean(ix * ix, 2), _box_mean(iy * iy, 2),
                     _box_mean(ix * iy, 2))
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    lam = tr / 2.0 - np.sqrt(np.maximum(tr * tr / 4.0 - det, 0.0))
    if lam.max() <= 0:
        return 0.0
    h, w = lam.shape
    ch, cw = h // cell, w // cell
    if ch < 1 or cw < 1:
        return 0.0
    best = lam[:ch * cell, :cw * cell].reshape(ch, cell, cw, cell).max(axis=(1, 3))
    return float((best > lam.max() * keep_frac).sum()) / max(h * w / 1e6, 1e-9)


def _nice_bar(max_m):
    """The longest tidy round distance that fits, and how to write it."""
    for step in (5000, 2500, 2000, 1000, 500, 250, 200, 100, 50, 25, 20, 10, 5):
        if step <= max_m:
            return step, (f"{step / 1000:g} km" if step >= 1000 else f"{step:g} m")
    return max(max_m, 1.0), f"{max(max_m, 1.0):.0f} m"


def _marker_legend(W, label, sub, scale_txt, m_per_mm, marker_mm):
    """Map furniture: a title block, a scale bar, a north arrow and a key. It
    reads as part of the map, and the lettering gives the tracker its corners."""
    im = Image.new("RGB", (W, W), (255, 255, 255))
    d = ImageDraw.Draw(im)
    u = W / 100.0
    d.rectangle((0, 0, W - 1, W - 1), outline=MARKER_INK_DARK,
                width=max(int(u * 0.9), 2))
    x, y = int(u * 8), int(u * 10)
    d.text((x, y), label or "City map", fill=MARKER_INK_DARK, font=_mk_font(u * 13))
    y += int(u * 16)
    if sub:
        d.text((x, y), sub, fill=MARKER_GREY, font=_mk_font(u * 7.5, False))
    y += int(u * 11)
    if scale_txt:
        d.text((x, y), scale_txt, fill=MARKER_GREY, font=_mk_font(u * 7.5, False))
    y += int(u * 13)
    d.line((x, y, W - x, y), fill=MARKER_INK_DARK, width=max(int(u * 0.5), 1))

    y += int(u * 7)
    full = W - 2 * x
    bh = int(u * 5)
    metres, txt = _nice_bar((full / (W / max(marker_mm, 1e-6))) * max(m_per_mm, 0)
                            if m_per_mm else 200.0)
    span = full
    if m_per_mm:                       # draw the bar its true length
        span = min(full, metres / m_per_mm * (W / max(marker_mm, 1e-6)))
    seg = span / 8.0
    for i in range(8):
        d.rectangle((x + i * seg, y, x + (i + 1) * seg, y + bh),
                    fill=MARKER_INK_DARK if i % 2 == 0 else (255, 255, 255),
                    outline=MARKER_INK_DARK, width=max(int(u * 0.35), 1))
    f = _mk_font(u * 6, False)
    d.text((x, y + bh + int(u * 1.5)), "0", fill=MARKER_INK_DARK, font=f)
    d.text((x + span - d.textlength(txt, font=f), y + bh + int(u * 1.5)), txt,
           fill=MARKER_INK_DARK, font=f)

    y += int(u * 16)
    cx = int(u * 16)
    d.polygon([(cx, y), (cx - u * 5, y + u * 13), (cx, y + u * 9.5),
               (cx + u * 5, y + u * 13)], fill=MARKER_INK_DARK)
    d.text((cx - u * 3, y + u * 14), "N", fill=MARKER_INK_DARK, font=_mk_font(u * 7))

    lx, ly = int(u * 34), y + int(u * 2)
    for i, (col, name) in enumerate(((PINK, "landmark"), ((150, 146, 156), "streets"),
                                     ((120, 170, 210), "water"))):
        yy = ly + i * int(u * 8)
        d.rectangle((lx, yy, lx + u * 7, yy + u * 5), fill=col,
                    outline=MARKER_INK_DARK, width=max(int(u * 0.3), 1))
        d.text((lx + u * 10, yy - u * 0.5), name, fill=MARKER_INK_DARK,
               font=_mk_font(u * 6, False))
    return im


def _marker_compass(W, label, sub):
    """A compass rose. The tick ring alone is dozens of clean corners."""
    im = Image.new("RGB", (W, W), (255, 255, 255))
    d = ImageDraw.Draw(im)
    u = W / 100.0
    d.rectangle((0, 0, W - 1, W - 1), outline=MARKER_INK_DARK,
                width=max(int(u * 0.9), 2))
    cx = cy = W / 2.0
    R = u * 33
    for rr, wd in ((R, 1.1), (R * 0.86, 0.5), (R * 0.5, 0.5)):
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=MARKER_INK_DARK,
                  width=max(int(u * wd), 1))
    for i in range(72):
        a = math.radians(i * 5)
        r0 = R * (0.86 if i % 6 else 0.72)
        d.line((cx + r0 * math.sin(a), cy - r0 * math.cos(a),
                cx + R * math.sin(a), cy - R * math.cos(a)),
               fill=MARKER_INK_DARK,
               width=max(int(u * (0.55 if i % 6 == 0 else 0.3)), 1))
    for i, ch in enumerate("NESW"):
        a = math.radians(i * 90)
        rr = R * 0.62
        d.text((cx + rr * math.sin(a) - u * 2.5, cy - rr * math.cos(a) - u * 4),
               ch, fill=PINK if i == 0 else MARKER_INK_DARK, font=_mk_font(u * 8))
    for i in range(4):
        a = math.radians(i * 90)
        b = a + math.radians(45)
        d.polygon([(cx + R * 0.46 * math.sin(a), cy - R * 0.46 * math.cos(a)),
                   (cx + R * 0.16 * math.sin(b), cy - R * 0.16 * math.cos(b)),
                   (cx, cy),
                   (cx + R * 0.16 * math.sin(b - math.radians(90)),
                    cy - R * 0.16 * math.cos(b - math.radians(90)))],
                  fill=PINK if i == 0 else MARKER_INK_DARK)
    if label:
        d.text((u * 8, W - u * 19), label, fill=MARKER_INK_DARK,
               font=_mk_font(u * 10))
    if sub:
        d.text((u * 8, W - u * 9), sub, fill=MARKER_GREY,
               font=_mk_font(u * 6.5, False))
    return im

MARKER_INK = ((26, 24, 30), (196, 68, 111), (122, 90, 166), (32, 92, 156),
              (214, 148, 48), (44, 122, 96))


def _marker_seed(text):
    """A stable seed: hash() moves between runs, this does not."""
    import hashlib  # noqa: PLC0415
    return int(hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:12], 16)


def make_marker_image(seed_text, size_px=1024, label="", sub="", shapes=130,
                     style="pattern", scale_txt="", m_per_mm=0.0,
                     marker_mm=45.0):
    """A square target for MindAR.

    ``legend`` and ``compass`` look like map furniture rather than a scanning
    code, and still carry enough corners to track; ``pattern`` is the loud one,
    which tracks best of all. See :func:`marker_strength` for the numbers."""
    if not HAVE_PIL:
        raise RuntimeError("the marker needs Pillow (pip install pillow)")
    style = str(style or "pattern").lower()
    S = int(size_px)
    if style in ("legend", "compass"):
        ss = 3 if S <= 900 else 1                   # supersample for clean type
        W = S * ss
        im = (_marker_legend(W, label, sub, scale_txt, m_per_mm, marker_mm)
              if style == "legend" else _marker_compass(W, label, sub))
        return im.resize((S, S), Image.LANCZOS) if ss != 1 else im

    import random  # noqa: PLC0415
    rnd = random.Random(_marker_seed(seed_text))
    ss = 2 if S <= 1400 else 1                      # supersample for clean edges
    W = S * ss
    img = Image.new("RGB", (W, W), (255, 255, 255))
    d = ImageDraw.Draw(img)
    pad = int(W * 0.055)
    cap = int(W * 0.145) if (label or sub) else 0   # caption band at the foot
    field = (pad, pad, W - pad, W - pad - cap)

    # a heavy frame gives the tracker strong corners and helps you line it up
    d.rectangle((0, 0, W - 1, W - 1), outline=(26, 24, 30), width=int(W * 0.035))
    # one thick L in a single corner, so the orientation is never ambiguous
    b = int(W * 0.05)
    t = int(W * 0.028)
    d.rectangle((b, b, b + int(W * 0.20), b + t), fill=(196, 68, 111))
    d.rectangle((b, b, b + t, b + int(W * 0.20)), fill=(196, 68, 111))

    fx0, fy0, fx1, fy1 = field
    for _ in range(int(shapes)):
        col = MARKER_INK[rnd.randrange(len(MARKER_INK))]
        r = rnd.uniform(W * 0.018, W * 0.075)
        cx = rnd.uniform(fx0 + r, fx1 - r)
        cy = rnd.uniform(fy0 + r, fy1 - r)
        kind = rnd.random()
        if kind < 0.34:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
        elif kind < 0.58:
            a = rnd.uniform(0, 6.283)
            pts = [(cx + r * math.cos(a + i * 2.094),
                    cy + r * math.sin(a + i * 2.094)) for i in range(3)]
            d.polygon(pts, fill=col)
        elif kind < 0.78:
            d.rectangle((cx - r, cy - r * rnd.uniform(0.35, 1.0),
                         cx + r, cy + r * rnd.uniform(0.35, 1.0)), fill=col)
        elif kind < 0.90:
            s0 = rnd.uniform(0, 360)
            d.arc((cx - r, cy - r, cx + r, cy + r), s0, s0 + rnd.uniform(90, 260),
                  fill=col, width=max(int(W * 0.010), 2))
        else:
            d.line((cx - r, cy - r, cx + r, cy + r), fill=col,
                   width=max(int(W * 0.009), 2))

    if cap:
        y0 = W - pad - cap
        d.rectangle((pad, y0, W - pad, W - pad), fill=(255, 255, 255))
        d.line((pad, y0, W - pad, y0), fill=(26, 24, 30), width=max(int(W * 0.006), 2))
        big = _marker_font(int(cap * 0.42))
        small = _marker_font(int(cap * 0.26))
        if label:
            d.text((pad + int(W * 0.02), y0 + int(cap * 0.12)), str(label),
                   fill=(26, 24, 30), font=big)
        if sub:
            d.text((pad + int(W * 0.02), y0 + int(cap * 0.60)), str(sub),
                   fill=(120, 116, 126), font=small)
    return img.resize((S, S), Image.LANCZOS) if ss != 1 else img


def _marker_font(size):
    for name in ("arialbd.ttf", "seguisb.ttf", "arial.ttf",
                 "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, max(int(size), 8))
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def marker_box_mm(corner, marker_mm, pad_mm, pw, ph):
    """Where the marker sits on the sheet, in millimetres: (x0, y0, x1, y1)
    with y measured down from the top edge."""
    corner = str(corner or MARKER_CORNERS[0]).lower()
    x0 = pad_mm if "left" in corner else pw - pad_mm - marker_mm
    y0 = pad_mm if "top" in corner else ph - pad_mm - marker_mm
    return (x0, y0, x0 + marker_mm, y0 + marker_mm)

def build_poster(bbox, model_w_mm, *, paper="A4", landscape=False, dpi=200,
                 style="drawn", labels=False, mono=False, cut="blank",
                 cut_bbox=None, polygon=(), gap_mm=3.0, wider=1.0,
                 credit=True, max_tiles=240, progress=None, palette=None,
                 fetch=None, fade=0.0, marker=False, marker_corner="bottom-right",
                 marker_mm=45.0, marker_pad_mm=6.0, marker_seed="",
                 marker_label="", marker_sub="", marker_style="legend",
                 marker_aruco_id=0, marker_second=True, rotate180=False):
    """The printable sheet: map right out to the paper edge, with a hole where
    the model stands. ``gap_mm`` is breathing room *around that hole*, not a
    border at the edge of the page. Returns (image, info dict)."""
    if not HAVE_PIL:
        raise RuntimeError("the backdrop needs Pillow (pip install pillow)")
    pw, ph = PAPERS.get(paper, PAPERS["A4"])
    if landscape:
        pw, ph = ph, pw
    gap_mm = float(gap_mm)                # may be negative: tuck under the print
    pbox, m_per_mm = poster_bbox(bbox, model_w_mm, pw, ph, wider)

    def px(mm):
        return max(int(round(mm / 25.4 * dpi)), 1)

    def px_f(mm):                         # signed, for distances that can go in
        return float(mm) / 25.4 * dpi

    sw, sh = px(pw), px(ph)
    url, cred = POSTER_STYLES.get(style, POSTER_STYLES["drawn"])[0 if labels else 1]
    if url is None:                       # drawn with no writing on it at all
        zoom, missing = 0, 0
        img = render_vector_backdrop(pbox, sw, sh, mono=mono, palette=palette,
                                     fetch=fetch, progress=progress)
    else:
        img, zoom, missing = stitch_tiles(pbox, url, want_px=sw,
                                          max_tiles=max_tiles, progress=progress)
        if img is None:
            raise RuntimeError("no tiles could be fetched")
        img = img.resize((sw, sh), Image.LANCZOS)
        if mono:
            img = ImageOps.grayscale(img).convert("RGB")

    sheet = fade_to_white(img, fade)      # edge to edge, no border on the page

    # Punch out where the print will sit -- no point inking under the plastic.
    # A positive gap clears a little extra so the plate need not land exactly;
    # a negative one runs the map in under the plate edge, which hides any
    # white sliver if it lands a hair off.
    if cut in ("blank", "outline"):
        pts = [(la, lo) for la, lo in (polygon or [])]
        if len(pts) < 3:
            s_, w_, n_, e_ = tuple(cut_bbox or bbox)
            pts = [(s_, w_), (s_, e_), (n_, e_), (n_, w_)]
        plate = [latlon_to_px(pbox, sw, sh, la, lo) for la, lo in pts]
        hole = plate
        if gap_mm:
            try:
                moved = Polygon(plate).buffer(px_f(gap_mm), join_style=2)
                if moved.geom_type == "MultiPolygon":
                    moved = max(moved.geoms, key=lambda g: g.area)
                if moved.geom_type == "Polygon" and not moved.is_empty:
                    hole = list(moved.exterior.coords)
                elif progress:
                    progress(f"gap {gap_mm:+.1f} mm would close the hole "
                             f"completely -- using the plate outline instead")
            except Exception:  # noqa: BLE001
                pass
        dr = ImageDraw.Draw(sheet)
        dr.polygon(hole, fill=(255, 255, 255))
        if cut == "outline":
            # the guide line always marks the true plate edge, so it stays
            # useful for placing the model whichever way the gap goes
            dr.line(list(plate) + [plate[0]], fill=(170, 170, 175),
                    width=max(px(0.3), 1))

    # Turn the map itself, not the sheet: the squares are printed after this,
    # so they stay in the corners the AR page expects while the map underneath
    # comes out the other way up. That is what a model standing the wrong way
    # round on the plate needs.
    if rotate180:
        sheet = sheet.rotate(180)
        if progress:
            progress("map turned 180 degrees; the squares stay in their corners")

    # ---- the tracking marker in one corner -------------------------------
    marker_img = None
    marker_info = None
    if marker:
        mm = max(float(marker_mm), 8.0)
        mpad = max(float(marker_pad_mm), 0.0)
        mm = min(mm, min(pw, ph) - 2 * mpad)              # never off the sheet
        second_info = None
        try:
            x0, y0, x1, y1 = marker_box_mm(marker_corner, mm, mpad, pw, ph)
            box = (px_f(x0), px_f(y0))
            side = px(mm)
            style = str(marker_style or "legend").lower()
            if style == "aruco":
                # quiet=0: the black square fills the box, so "15 mm" really is
                # a 15 mm square on the paper. The white surround it needs is
                # the rectangle drawn below, which sits outside the box.
                marker_img = make_aruco_image(marker_aruco_id, max(side, 240),
                                              quiet=0)
                quiet = px(max(mm * 0.25, 2.5))
                ImageDraw.Draw(sheet).rectangle(
                    (box[0] - quiet, box[1] - quiet,
                     box[0] + side + quiet, box[1] + side + quiet),
                    fill=(255, 255, 255))
                sheet.paste(marker_img.resize((side, side), Image.NEAREST),
                            (int(round(box[0])), int(round(box[1]))))
                if marker_second:
                    cnr2 = OPPOSITE_CORNER.get(marker_corner, "top-left")
                    id2 = second_aruco_id(marker_aruco_id)
                    a2, b2, c2, d2 = marker_box_mm(cnr2, mm, mpad, pw, ph)
                    box2 = (px_f(a2), px_f(b2))
                    img2 = make_aruco_image(id2, max(side, 240), quiet=0)
                    ImageDraw.Draw(sheet).rectangle(
                        (box2[0] - quiet, box2[1] - quiet,
                         box2[0] + side + quiet, box2[1] + side + quiet),
                        fill=(255, 255, 255))
                    sheet.paste(img2.resize((side, side), Image.NEAREST),
                                (int(round(box2[0])), int(round(box2[1]))))
                    second_info = {"aruco_id": id2, "corner": cnr2,
                                   "box_mm": (a2, b2, c2, d2),
                                   "centre_mm": ((a2 + c2) / 2.0,
                                                 (b2 + d2) / 2.0)}
            elif style.startswith("map"):
                # nothing is drawn at all -- a patch of the printed map is the
                # target, so the sheet carries no mark of any kind
                marker_img = sheet.crop(
                    (int(round(box[0])), int(round(box[1])),
                     int(round(box[0])) + side, int(round(box[1])) + side)).copy()
            else:
                marker_img = make_marker_image(
                    marker_seed or f"{bbox}", size_px=max(side, 512),
                    label=marker_label, sub=marker_sub, style=style,
                    scale_txt=f"1:{int(round(m_per_mm * 1000)):,}",
                    m_per_mm=m_per_mm, marker_mm=mm)
                # a white card under it: MindAR wants the target clean, and the
                # map showing through the frame would confuse the feature match
                quiet = px(max(mm * 0.06, 1.5))
                ImageDraw.Draw(sheet).rectangle(
                    (box[0] - quiet, box[1] - quiet,
                     box[0] + side + quiet, box[1] + side + quiet),
                    fill=(255, 255, 255))
                sheet.paste(marker_img.resize((side, side), Image.LANCZOS),
                            (int(round(box[0])), int(round(box[1]))))
            strength = marker_strength(marker_img)
            marker_info = {"corner": marker_corner, "mm": mm, "pad_mm": mpad,
                           # what a pose solver measures: the black square. It
                           # is the box for an ArUco, the whole tile otherwise.
                           "square_mm": mm,
                           "style": style, "strength": strength,
                           "aruco_id": (int(marker_aruco_id) % len(ARUCO_4X4_BYTES)
                                        if style == "aruco" else None),
                           "dictionary": "ARUCO_4X4_1000" if style == "aruco" else None,
                           "box_mm": (x0, y0, x1, y1),
                           "centre_mm": ((x0 + x1) / 2.0, (y0 + y1) / 2.0),
                           "second": second_info}
            if progress:
                progress(f"marker: {style}, {mm:.0f} mm in the {marker_corner}, "
                         f"{strength:.0f} features per megapixel")
                if style == "aruco":
                    lever = max(pw, ph) / max(mm, 1e-6)
                    progress(f"  the black square prints {mm:.1f} mm; the sheet "
                             f"is {lever:.0f} squares across")
                    if second_info:
                        progress(f"  a second square, id {second_info['aruco_id']}, "
                                 f"in the {second_info['corner']} -- the two "
                                 "together fix the sheet far more steadily than "
                                 "one can")
                    elif lever > 14:
                        progress("  that is a long reach for one small square: "
                                 "turn on the second square, or print 25-30 mm")
                if style == "aruco":
                    pass            # a fiducial is read by its shape, not by
                                    # counting features, so the score is moot
                elif strength < MARKER_WEAK_BELOW:
                    tip = ("try the 'legend' or 'pattern' style, or a bigger "
                           "marker")
                    if style.startswith("map"):
                        # the map is the target here, so say whether any other
                        # corner of this sheet would actually have worked
                        ranked = []
                        for cnr in MARKER_CORNERS:
                            a, b, c2, d2 = marker_box_mm(cnr, mm, mpad, pw, ph)
                            patch = sheet.crop((int(px_f(a)), int(px_f(b)),
                                                int(px_f(c2)), int(px_f(d2))))
                            ranked.append((marker_strength(patch), cnr))
                        ranked.sort(reverse=True)
                        if ranked[0][0] >= MARKER_WEAK_BELOW:
                            tip = ("the %s corner is the busiest here (%.0f) -- "
                                   "use that" % (ranked[0][1], ranked[0][0]))
                        else:
                            tip = ("no corner of this sheet is busy enough. "
                                   "Switch the map to satellite or turn labels "
                                   "on, or pick a drawn marker style")
                    if style != "aruco":
                        progress("marker: that is thin for tracking -- " + tip)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("city_map_generator").warning(
                "marker could not be drawn (%s)", exc)
            marker_img, marker_info = None, None

    if credit:
        dr = ImageDraw.Draw(sheet)
        line = cred + f"   .   1:{int(round(m_per_mm * 1000)):,}"
        try:
            font = ImageFont.truetype("arial.ttf", max(px(2.2), 8))
        except Exception:  # noqa: BLE001
            font = ImageFont.load_default()
        pad = px(2.0)
        box = dr.textbbox((0, 0), line, font=font)
        strip = (0, sh - (box[3] - box[1]) - 2 * pad, sw, sh)
        dr.rectangle(strip, fill=(255, 255, 255))     # a clean strip to read on
        dr.text((pad, strip[1] + pad), line, fill=(140, 136, 146), font=font)

    info = {"paper": f"{pw:.0f} x {ph:.0f} mm", "dpi": dpi, "zoom": zoom,
            "fade": fade, "gap_mm": gap_mm,
            "scale": f"1:{int(round(m_per_mm * 1000)):,}",
            "covers_m": (pw * m_per_mm, ph * m_per_mm),
            "missing_tiles": missing, "credit": cred,
            "paper_mm": (pw, ph), "m_per_mm": m_per_mm,
            "rotated180": bool(rotate180),
            "marker": marker_info, "marker_image": marker_img,
            "pixels": (sheet.width, sheet.height)}
    return sheet, info

ARUCO_SCANNER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>__TITLE__</title>
<style>
 *{box-sizing:border-box}
 html,body{margin:0;height:100%;background:#0a0a0a;color:#fff;
   font:15px/1.4 Rubik,system-ui,-apple-system,sans-serif;overflow:hidden}
 #stage{position:fixed;inset:0}
 #cam,#gl{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
 #cam{z-index:1}#gl{z-index:2}
 #bar{position:fixed;left:0;right:0;bottom:0;z-index:5;padding:14px 16px
   calc(14px + env(safe-area-inset-bottom));background:rgba(10,10,10,.82);
   backdrop-filter:blur(8px);display:flex;gap:12px;align-items:center}
 #bar b{color:#D9548A}
 #msg{flex:1;min-width:0}
 button{font:inherit;padding:10px 16px;border:0;border-radius:999px;
   background:#D9548A;color:#fff;font-weight:600}
 button[disabled]{opacity:.45}
 #dot{width:10px;height:10px;border-radius:50%;background:#666;flex:none}
 #dot.on{background:#39d98a}
 #err{position:fixed;inset:0;z-index:9;display:none;place-items:center;
   padding:28px;text-align:center;background:#0a0a0a}
</style>
</head>
<body>
<div id="stage">
  <video id="cam" playsinline muted></video>
  <canvas id="gl"></canvas>
</div>
<div id="bar">
  <span id="dot"></span>
  <span id="msg">Point the camera at the <b>small square</b> on the sheet</span>
  <button id="go">Start</button>
</div>
<div id="err"><div><h2>Camera unavailable</h2><p id="errtext"></p></div></div>

<script src="https://cdn.jsdelivr.net/npm/js-aruco2@2.0.0/src/cv.js"></script>
<script src="https://cdn.jsdelivr.net/npm/js-aruco2@2.0.0/src/aruco.js"></script>
<script src="https://cdn.jsdelivr.net/npm/js-aruco2@2.0.0/src/svd.js"></script>
<script src="https://cdn.jsdelivr.net/npm/js-aruco2@2.0.0/src/posit1.js"></script>
<script src="https://cdn.jsdelivr.net/npm/js-aruco2@2.0.0/src/dictionaries/aruco_4x4_1000.js"></script>
<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
            "three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';

// ---- what this sheet is -------------------------------------------------
const CFG = {
  markerId:   __MARKER_ID__,          // which ArUco id is printed
  markerMm:   __SQUARE_MM__,          // the black square, in mm on paper
  offsetXmm:  __OFFSET_X__,           // marker centre -> model centre, on the page
  offsetYmm:  __OFFSET_Y__,
  modelUrl:   './model.glb',
  dictionary: 'ARUCO_4X4_1000'
};

const video = document.getElementById('cam');
const glc   = document.getElementById('gl');
const msg   = document.getElementById('msg');
const dot   = document.getElementById('dot');
const go    = document.getElementById('go');

const work = document.createElement('canvas');   // detection runs small & fast
const wctx = work.getContext('2d', {willReadFrequently: true});
const detector = new AR.Detector({dictionaryName: CFG.dictionary});
let posit = null, lastSeen = 0;

const renderer = new THREE.WebGLRenderer({canvas: glc, alpha: true, antialias: true});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 1, 10000);
scene.add(new THREE.HemisphereLight(0xffffff, 0x556070, 2.2));
const key = new THREE.DirectionalLight(0xffffff, 1.6);
key.position.set(0.4, 0.9, 1.0);
scene.add(key);

// everything is hung off this: POSIT gives us the marker's pose in millimetres
const anchor = new THREE.Object3D();
anchor.matrixAutoUpdate = false;
scene.add(anchor);
const holder = new THREE.Object3D();            // the model, offset to the hole
holder.position.set(CFG.offsetXmm, CFG.offsetYmm, 0);
anchor.add(holder);

new GLTFLoader().load(CFG.modelUrl, g => {
  const m = g.scene;
  const box = new THREE.Box3().setFromObject(m);
  const c = box.getCenter(new THREE.Vector3());
  m.position.set(-c.x, -c.y, -box.min.z);        // stand it on the paper
  holder.add(m);
  say('Model ready. Point at the square.');
}, undefined, () => say('model.glb did not load -- the square will still track.'));

function say(t){ msg.innerHTML = t; }

function resize(){
  const w = innerWidth, h = innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
addEventListener('resize', resize);

go.onclick = async () => {
  go.disabled = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {facingMode: {ideal: 'environment'}, width: {ideal: 1280}}
    });
    video.srcObject = stream;
    await video.play();
    document.getElementById('bar').style.paddingBottom =
      'calc(14px + env(safe-area-inset-bottom))';
    go.style.display = 'none';
    resize();
    tick();
    say('Looking for the square ...');
  } catch (e) {
    document.getElementById('err').style.display = 'grid';
    document.getElementById('errtext').textContent =
      e.message + ' -- the page must be on https:// (or localhost) and you have '
      + 'to allow the camera.';
  }
};

function tick(){
  requestAnimationFrame(tick);
  if (video.readyState !== video.HAVE_ENOUGH_DATA) return;

  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw) return;
  const scale = Math.min(1, 640 / vw);
  const w = Math.round(vw * scale), h = Math.round(vh * scale);
  if (work.width !== w) { work.width = w; work.height = h; posit = null; }
  wctx.drawImage(video, 0, 0, w, h);

  // focal length in pixels for this frame, from the camera's field of view
  if (!posit) posit = new POS.Posit(CFG.markerMm, w);

  const markers = detector.detect(wctx.getImageData(0, 0, w, h));
  const hit = markers.find(m => m.id === CFG.markerId);

  if (hit) {
    lastSeen = performance.now();
    dot.classList.add('on');
    const pts = hit.corners.map(c => ({x: c.x - w / 2, y: h / 2 - c.y}));
    const pose = posit.pose(pts);
    if (pose) {
      const R = pose.bestRotation, T = pose.bestTranslation;
      anchor.matrix.set(
         R[0][0],  R[0][1],  R[0][2], T[0],
         R[1][0],  R[1][1],  R[1][2], T[1],
        -R[2][0], -R[2][1], -R[2][2], -T[2],
              0,        0,        0,    1);
      anchor.visible = true;
      // match the render camera to the one POSIT assumed
      const fov = 2 * Math.atan(h / 2 / w) * 180 / Math.PI;
      if (Math.abs(camera.fov - fov) > 0.01) {
        camera.fov = fov; camera.updateProjectionMatrix();
      }
      say('Tracking marker <b>' + CFG.markerId + '</b>');
    }
  } else if (performance.now() - lastSeen > 400) {
    anchor.visible = false;
    dot.classList.remove('on');
    say('Point the camera at the <b>small square</b> on the sheet');
  }
  renderer.render(scene, camera);
}
resize();
</script>
</body>
</html>
"""

ARUCO_README = """__TITLE__ -- augmented reality sheet (ArUco)
=======================================================================

WHAT IS HERE
  index.html            the scanner -- open this on a phone
  model.glb             the 3D model, in millimetres
  __MARKER_PNG__        the marker exactly as printed, id __MARKER_ID__

NOTHING TO COMPILE
  An ArUco marker is read from its own geometry, so unlike an image target
  there is no training or .mind file. Print the sheet, open the page, done.

PUTTING IT ON A SITE
  The camera only works over https:// (or on localhost). Any static host will
  do. For a Vite project -- which is what shimushili.com is -- drop this whole
  folder into  public/  and it is served as-is:

      public/ar/index.html   ->   https://your-site/ar/

  No build changes, no routing: Vite copies public/ verbatim.
  To try it first:   python -m http.server 8000   then http://localhost:8000

DETAILS THAT MATTER
  marker id     __MARKER_ID__   from the ARUCO_4X4_1000 dictionary
  printed size  __MARKER_MM__ mm square, __CORNER__ corner
  offset        the model is drawn __OFFSET_X__ mm across and __OFFSET_Y__ mm
                up from the marker's centre, which is where the blank hole in
                the sheet is
  Print at 100% scale. If the sheet is printed at another size, change
  markerMm in index.html to whatever the square really measures -- everything
  else follows from it.

IF IT MISBEHAVES
  Nothing tracks        make sure the whole black square plus its white
                        surround is in frame, flat and evenly lit
  Model floats or sinks the GLB is in millimetres and is stood on the paper by
                        its own lowest point; check the model really is mm
  Model is beside it    offsetXmm / offsetYmm in index.html are in millimetres
                        on the page, +x right and +y up from the marker centre
  Wrong id              markerId in index.html must match the printed square

Area    : __AREA__
Model   : __MODEL_W__ x __MODEL_H__ mm on a __PAPER__ sheet at __SCALE_TXT__
Marker dictionary from OpenCV (3-clause BSD); detection by js-aruco2.
"""

AR_INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1,
      maximum-scale=1, user-scalable=no">
<title>__TITLE__</title>
<script src="https://aframe.io/releases/1.5.0/aframe.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mind-ar@1.2.5/dist/mindar-image-aframe.prod.js"></script>
<style>
 body{margin:0;font:14px system-ui,sans-serif}
 #hint{position:fixed;left:0;right:0;bottom:0;z-index:5;padding:10px 14px;
       background:rgba(255,255,255,.92);color:#332E3B;text-align:center}
 #hint b{color:#D9548A}
</style>
</head>
<body>
<div id="hint">Point the camera at the <b>marker</b> in the __CORNER__ of the sheet</div>
<a-scene mindar-image="imageTargetSrc: ./targets.mind; filterMinCF: 0.0001;
                       filterBeta: 0.001; missTolerance: 8; warmupTolerance: 3"
         color-space="sRGB" embedded renderer="colorManagement: true"
         vr-mode-ui="enabled: false" device-orientation-permission-ui="enabled: false">
  <a-assets>
    <a-asset-item id="cityModel" src="./model.glb"></a-asset-item>
  </a-assets>
  <a-camera position="0 0 0" look-controls="enabled: false"></a-camera>

  <a-entity mindar-image-target="targetIndex: 0">
    <!-- The marker is 1 unit wide in here, whatever its printed size, so the
         model is scaled by 1/__MARKER_MM__ to turn its millimetres into those
         units, and shifted to sit over the hole in the paper. -->
    <a-gltf-model src="#cityModel"
                  position="__POS_X__ __POS_Y__ 0"
                  rotation="__ROT__"
                  scale="__SCALE__ __SCALE__ __SCALE__"></a-gltf-model>
  </a-entity>
</a-scene>
</body>
</html>
"""

AR_README = """__TITLE__ -- augmented reality sheet
=======================================================================

WHAT IS HERE
  __MARKER_PNG__   the marker exactly as it is printed on the backdrop
  index.html       the viewer page
  model.glb        the 3D model, in millimetres
  targets.mind     YOU STILL HAVE TO MAKE THIS -- see step 1

THREE STEPS
  1. Compile the marker.
     Open  https://hiukim.github.io/mind-ar-js-doc/tools/compile
     Upload __MARKER_PNG__, press Start, download the .mind file,
     and save it in this folder as  targets.mind

  2. Serve this folder over HTTPS.
     Phone cameras only work on https:// or on localhost, never on file://.
     Anything static will do -- a local test is:
         python -m http.server 8000
     then open http://localhost:8000 on the same machine, or put the folder
     on any static host and open it on the phone.

  3. Print the backdrop at 100% scale, open the page, allow the camera and
     point it at the marker in the __CORNER__ corner.

WHAT IT SHOULD LOOK LIKE
  The model stands over the blank area in the middle of the sheet -- the hole
  the printed part normally occupies -- at the same scale as the paper map.

IF IT MISBEHAVES
  Model lies flat instead of standing, or stands on its side
      change   rotation="__ROT__"   in index.html. The three values are
      degrees about X Y Z; "-90 0 0" and "90 0 0" are the usual fixes.
  Model is the wrong size
      scale is 1/__MARKER_MM__ because the marker is __MARKER_MM__ mm wide on
      paper. If you print at a different size, change the scale to 1/newsize.
  Nothing is tracked
      the marker must be flat, evenly lit and not glossy; and targets.mind has
      to be compiled from __MARKER_PNG__, not from a photo of the print.
  Script errors
      the two libraries are pinned to aframe 1.5.0 and mind-ar 1.2.5 in
      index.html. Newer versions exist; bump them there if you need to.

Marker  : __STYLE__, __MARKER_MM__ mm square, __CORNER__ corner,
          __PAD_MM__ mm from the edge, __STRENGTH__ features per megapixel
          (the loud "pattern" style scores about 1500; below 300 is thin)
Model   : __MODEL_W__ x __MODEL_H__ mm on a __PAPER__ sheet at __SCALE_TXT__
Area    : __AREA__
"""


def export_glb(stl_paths, colmap, out_path, base_colour=(205, 205, 205)):
    """The model as one coloured GLB, so the AR page has something to show.
    Uses the split parts when they are there, so each keeps its own colour."""
    import trimesh  # noqa: PLC0415
    paths = [p for p in (stl_paths or []) if p and os.path.isfile(p)]
    splits = [p for p in paths if p.lower().endswith(".stl")
              and "_" in os.path.basename(p)]
    scene = trimesh.Scene()
    for p in (splits or paths):
        try:
            m = trimesh.load(p, force="mesh")
        except Exception:  # noqa: BLE001
            continue
        if not len(getattr(m, "faces", [])):
            continue
        key = os.path.splitext(os.path.basename(p))[0].rsplit("_", 1)[-1].lower()
        col = tuple(colmap.get(key, base_colour))
        # colour the vertices, not the faces: trimesh converts face colours to
        # vertex colours through scipy, which this project does not depend on
        m.visual = trimesh.visual.ColorVisuals(
            m, vertex_colors=np.tile(np.array([*col, 255], dtype=np.uint8),
                                     (len(m.vertices), 1)))
        scene.add_geometry(m, node_name=key, geom_name=key)
    if not scene.geometry:
        raise ValueError("no meshes to put in the GLB")
    scene.export(out_path)
    return out_path


def ar_placement(info, rotation="0 0 0"):
    """Where the model goes relative to the marker, in MindAR units (the marker
    is 1 unit wide). Returns the numbers index.html needs."""
    mk = (info or {}).get("marker")
    if not mk:
        raise ValueError("that sheet has no marker on it")
    pw, ph = info["paper_mm"]
    mcx, mcy = mk["centre_mm"]
    scale = 1.0 / float(mk["mm"])
    # the model sits in the middle of the sheet; y is flipped because paper
    # coordinates run down the page and A-Frame's run up
    return {"scale": scale, "rotation": rotation,
            "pos_x": (pw / 2.0 - mcx) * scale,
            "pos_y": -(ph / 2.0 - mcy) * scale,
            "marker_mm": float(mk["mm"]), "corner": mk["corner"]}


def export_ar_kit(folder, stem, info, *, stl_paths=(), colmap=None,
                  title="City map", area="", model_mm=(0, 0), rotation="0 0 0",
                  progress=None):
    """Write everything needed to view the model over the printed sheet."""
    os.makedirs(folder, exist_ok=True)
    mk = (info or {}).get("marker")
    img = (info or {}).get("marker_image")
    if not mk or img is None:
        raise ValueError("build the backdrop with the marker switched on first")
    place = ar_placement(info, rotation)
    marker_png = f"{stem}_marker.png"
    written = []
    is_aruco = str(mk.get("style", "")) == "aruco"

    img.save(os.path.join(folder, marker_png))
    written.append(marker_png)

    try:
        export_glb(stl_paths, dict(colmap or {}),
                   os.path.join(folder, "model.glb"))
        written.append("model.glb")
    except Exception as exc:  # noqa: BLE001
        if progress:
            progress(f"no model.glb ({exc}) -- the page will need one adding")

    pw_mm, ph_mm = info["paper_mm"]
    mcx, mcy = mk["centre_mm"]
    fields = {
        # POSIT works in the marker's own millimetres, so the offset to the
        # middle of the sheet is just millimetres; +y is up the page
        "__OFFSET_X__": f"{pw_mm / 2.0 - mcx:.2f}",
        "__OFFSET_Y__": f"{-(ph_mm / 2.0 - mcy):.2f}",
        "__MARKER_ID__": str(mk.get("aruco_id") if mk.get("aruco_id") is not None
                             else 0),
        # the square in the opposite corner, when the sheet carries one: the
        # two together are what let a scanner pin the paper down steadily
        "__MARKER_ID2__": str((mk.get("second") or {}).get("aruco_id", "")),
        "__SECOND_CORNER__": str((mk.get("second") or {}).get("corner", "")),
        "__TITLE__": title, "__CORNER__": place["corner"],
        "__MARKER_MM__": f"{place['marker_mm']:.0f}",
        # the solver is given the black square, which is what it can measure
        "__SQUARE_MM__": f"{float(mk.get('square_mm') or mk['mm']):.2f}",
        "__PAD_MM__": f"{mk['pad_mm']:.0f}",
        "__POS_X__": f"{place['pos_x']:.4f}",
        "__POS_Y__": f"{place['pos_y']:.4f}",
        "__SCALE__": f"{place['scale']:.6f}", "__ROT__": rotation,
        "__MARKER_PNG__": marker_png, "__PAPER__": info.get("paper", ""),
        "__STYLE__": str(mk.get("style", "pattern")),
        "__STRENGTH__": f"{mk.get('strength', 0):.0f}",
        "__SCALE_TXT__": info.get("scale", ""), "__AREA__": area or "",
        "__MODEL_W__": f"{model_mm[0]:.0f}", "__MODEL_H__": f"{model_mm[1]:.0f}",
    }

    def fill(text):
        for k, v in fields.items():
            text = text.replace(k, str(v))
        return text

    pages = (("index.html", ARUCO_SCANNER_HTML), ("README.txt", ARUCO_README)) \
        if is_aruco else (("index.html", AR_INDEX_HTML), ("README.txt", AR_README))
    for name, body in pages:
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write(fill(body))
        written.append(name)
    return written

# ---------------------------------------------------------------------------
# Capture history: every area you frame is remembered so you can go back to it
# ---------------------------------------------------------------------------

HISTORY_FILE = os.path.join(SCRIPT_DIR, "city_map_captures.json")
HISTORY_DIR = os.path.join(SCRIPT_DIR, "captures")
HISTORY_KEEP = 40


def load_history() -> list:
    """Newest first. A missing or broken file just means an empty history."""
    try:
        with open(HISTORY_FILE, encoding="utf-8") as fh:
            items = json.load(fh).get("captures") or []
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in items:
        b = it.get("bbox") or []
        if len(b) != 4:
            continue
        out.append({"bbox": [float(v) for v in b],
                    "polygon": [[float(a), float(c)] for a, c in
                                (it.get("polygon") or [])],
                    "place": str(it.get("place") or ""),
                    "when": str(it.get("when") or ""),
                    "thumb": str(it.get("thumb") or "")})
    return out


def save_history(items):
    with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
        json.dump({"captures": items[:HISTORY_KEEP]}, fh, indent=1)


def remember_capture(bbox, polygon, place, image):
    """Add a capture to the front of the history, de-duplicating the same box.
    The reference image is kept beside it as a small PNG."""
    items = [it for it in load_history()
             if not _same_bbox(it["bbox"], bbox)]
    thumb = ""
    if image is not None and HAVE_PIL:
        try:
            os.makedirs(HISTORY_DIR, exist_ok=True)
            thumb = os.path.join(HISTORY_DIR,
                                 time.strftime("cap_%Y%m%d-%H%M%S.png"))
            im = image.copy()
            im.thumbnail((480, 480))
            im.save(thumb)
        except Exception:  # noqa: BLE001
            thumb = ""
    items.insert(0, {"bbox": [float(v) for v in bbox],
                     "polygon": [[float(a), float(b)] for a, b in (polygon or [])],
                     "place": place or "",
                     "when": time.strftime("%Y-%m-%d %H:%M"),
                     "thumb": thumb})
    dropped = items[HISTORY_KEEP:]
    try:
        save_history(items)
    except OSError:
        return items[:HISTORY_KEEP]
    for it in dropped:                      # don't leave orphan thumbnails
        try:
            if it.get("thumb") and os.path.isfile(it["thumb"]):
                os.remove(it["thumb"])
        except OSError:
            pass
    return items[:HISTORY_KEEP]


def _same_bbox(a, b, tol=1e-6):
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


class PosterDialog(tk.Toplevel):
    """The paper the model stands on: the same area, wider, at the print's own
    scale, with a hole where the plastic goes."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Backdrop to print")
        self.geometry("1020x820")
        self.minsize(880, 620)
        self.configure(background=BG)
        self.transient(app)
        self._img = None
        self._info = {}
        self._q: queue.Queue = queue.Queue()
        self._busy = False

        self.paper = tk.StringVar(value="A4")
        self.landscape = tk.BooleanVar(value=False)
        self.dpi = tk.StringVar(value="200")
        self.style = tk.StringVar(value="drawn")
        self.labels = tk.BooleanVar(value=False)
        self.mono = tk.BooleanVar(value=False)
        self.cut = tk.StringVar(value="blank")
        self.rotate180 = tk.BooleanVar(value=False)
        self.wider = tk.StringVar(value="1.0")
        self.frame = tk.StringVar(value="0")
        self.gap = tk.StringVar(value="3")
        self.theme_name = tk.StringVar(value="Paper (default)")
        self.marker_on = tk.BooleanVar(value=False)
        self.marker_second = tk.BooleanVar(value=True)
        self.marker_corner = tk.StringVar(value=MARKER_CORNERS[0])
        self.marker_style = tk.StringVar(value=MARKER_STYLES[0])
        self.marker_mm = tk.StringVar(value="45")
        self.marker_pad = tk.StringVar(value="6")
        self.fade = tk.StringVar(value="0")
        self.credit = tk.BooleanVar(value=True)

        side = ttk.Frame(self, style="Page.TFrame", padding=(14, 14, 8, 14))
        side.pack(side="left", fill="y")
        right = ttk.Frame(self, style="Page.TFrame", padding=(6, 14, 14, 14))
        right.pack(side="left", fill="both", expand=True)

        act2 = ttk.Frame(side, style="Page.TFrame")
        act2.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(act2, text="Save PNG", command=lambda: self._save("png")).pack(
            side="left")
        ttk.Button(act2, text="Save PDF", command=lambda: self._save("pdf")).pack(
            side="left", padx=6)
        act = ttk.Frame(side, style="Page.TFrame")
        act.pack(side="bottom", fill="x", pady=(10, 0))
        self.go = ttk.Button(act, text="Build preview", style="Accent.TButton",
                             command=lambda: self._build(preview=True))
        self.go.pack(side="left")
        ttk.Button(act, text="Close", style="Ghost.TButton",
                   command=self.destroy).pack(side="right")
        holder = ttk.Frame(side, style="Page.TFrame")
        holder.pack(side="top", fill="both", expand=True)
        left = app._scrollable(holder, width=306)

        c = make_card(left, "", "Paper")
        r1 = ttk.Frame(c, style="Card.TFrame")
        r1.pack(fill="x")
        ttk.Label(r1, text="Size", style="Field.TLabel").pack(side="left")
        ttk.Combobox(r1, textvariable=self.paper, width=11, state="readonly",
                     values=list(PAPERS)).pack(side="left", padx=8)
        ttk.Checkbutton(r1, text="landscape", variable=self.landscape).pack(side="left")
        r2 = ttk.Frame(c, style="Card.TFrame")
        r2.pack(fill="x", pady=(10, 0))
        ttk.Label(r2, text="DPI", style="Field.TLabel").pack(side="left")
        ttk.Combobox(r2, textvariable=self.dpi, width=6, state="readonly",
                     values=[str(d) for d in POSTER_DPI]).pack(side="left", padx=8)
        ttk.Label(r2, text="Gap mm", style="Field.TLabel").pack(side="left",
                                                                padx=(10, 0))
        ttk.Entry(r2, textvariable=self.gap, width=5).pack(side="left", padx=8)
        r2b = ttk.Frame(c, style="Card.TFrame")
        r2b.pack(fill="x", pady=(10, 0))
        ttk.Label(r2b, text="Frame mm", style="Field.TLabel").pack(side="left")
        ttk.Entry(r2b, textvariable=self.frame, width=5).pack(side="left", padx=8)
        ttk.Label(r2b, text="map to show around the print; 0 = whatever is left",
                  style="Muted.TLabel").pack(side="left")
        ttk.Label(c, style="Muted.TLabel", wraplength=250, justify="left",
                  text="The map runs to the paper edge. Gap is clearance around "
                       "the hole the model sits in: positive leaves room so the "
                       "plate need not land exactly, negative (-1, -2 ...) runs "
                       "the map in under the plate edge so no white shows at "
                       "the join."
                  ).pack(anchor="w", pady=(8, 0))

        c = make_card(left, "", "Look")
        r3 = ttk.Frame(c, style="Card.TFrame")
        r3.pack(fill="x")
        ttk.Label(r3, text="Style", style="Field.TLabel").pack(side="left")
        sb = ttk.Combobox(r3, textvariable=self.style, width=11, state="readonly",
                          values=list(POSTER_STYLES))
        sb.pack(side="left", padx=8)
        sb.bind("<<ComboboxSelected>>", lambda e: self._sync())
        for _v in (self.paper, self.landscape, self.frame, self.wider, self.gap):
            _v.trace_add("write", self._describe)
        ttk.Checkbutton(c, text="black and white", variable=self.mono,
                        command=self._sync).pack(anchor="w", pady=(10, 0))
        self.lbl_chk = ttk.Checkbutton(c, text="street names and labels",
                                       variable=self.labels, command=self._sync)
        self.lbl_chk.pack(anchor="w")
        ttk.Checkbutton(c, text="credit line along the bottom",
                        variable=self.credit).pack(anchor="w")
        r4 = ttk.Frame(c, style="Card.TFrame")
        r4.pack(fill="x", pady=(10, 0))
        ttk.Label(r4, text="Fade %", style="Field.TLabel").pack(side="left")
        ttk.Entry(r4, textvariable=self.fade, width=5).pack(side="left", padx=8)
        ttk.Label(r4, text="Wider x", style="Field.TLabel").pack(side="left",
                                                                 padx=(10, 0))
        ttk.Entry(r4, textvariable=self.wider, width=5).pack(side="left", padx=8)

        self.pal_card = make_card(left, "", "Colours")
        c = self.pal_card
        grid = ttk.Frame(c, style="Card.TFrame")
        grid.pack(fill="x")
        self.swatches = {}
        for i, (key, label) in enumerate(BACKDROP_SWATCHES):
            r, col = divmod(i, 2)
            cell = ttk.Frame(grid, style="Card.TFrame")
            cell.grid(row=r, column=col, sticky="we", padx=(0 if col == 0 else 10, 0),
                      pady=2)
            sw = tk.Label(cell, width=3, cursor="hand2", relief="flat",
                          borderwidth=0,
                          background="#%02x%02x%02x"
                                     % tuple(self.app.backdrop_palette[key]))
            sw.pack(side="left")
            sw.bind("<Button-1>", lambda e, k=key: self._pick_colour(k))
            ttk.Label(cell, text=label, style="Field.TLabel").pack(side="left",
                                                                  padx=6)
            self.swatches[key] = sw
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        tr = ttk.Frame(c, style="Card.TFrame")
        tr.pack(fill="x", pady=(10, 0))
        ttk.Label(tr, text="Theme", style="Field.TLabel").pack(side="left")
        self.theme_box = ttk.Combobox(tr, textvariable=self.theme_name, width=14,
                                      state="readonly", values=list(self.app.themes))
        self.theme_box.pack(side="left", fill="x", expand=True, padx=8)
        self.theme_box.bind("<<ComboboxSelected>>", self._apply_theme)
        ttk.Button(tr, text="Save", style="Tiny.TButton",
                   command=self._theme_save).pack(side="left")
        ttk.Button(tr, text="Del", style="Tiny.TButton",
                   command=self._theme_delete).pack(side="left", padx=(4, 0))

        pr = ttk.Frame(c, style="Card.TFrame")
        pr.pack(fill="x", pady=(8, 0))
        ttk.Button(pr, text="Match the 3D print", style="Tiny.TButton",
                   command=self._match_print).pack(side="left")
        ttk.Button(pr, text="Reset", style="Tiny.TButton",
                   command=self._reset_palette).pack(side="left", padx=6)
        self.pal_hint = ttk.Label(c, style="Muted.TLabel", wraplength=250,
                                  justify="left", text="")
        self.pal_hint.pack(anchor="w", pady=(8, 0))

        c = make_card(left, "", "AR marker")
        ttk.Checkbutton(c, text="print a tracking marker on the sheet",
                        variable=self.marker_on,
                        command=self._marker_hint).pack(anchor="w")
        sr = ttk.Frame(c, style="Card.TFrame")
        sr.pack(fill="x", pady=(8, 0))
        ttk.Label(sr, text="Style", style="Field.TLabel").pack(side="left")
        ttk.Combobox(sr, textvariable=self.marker_style, width=12,
                     state="readonly", values=list(MARKER_STYLES)).pack(
            side="left", padx=8)
        ttk.Checkbutton(c, text="a second square in the opposite corner",
                        variable=self.marker_second,
                        command=self._marker_hint).pack(anchor="w", pady=(8, 0))
        mg = ttk.Frame(c, style="Card.TFrame")
        mg.pack(fill="x", pady=(8, 0))
        for col, cap in enumerate(("Corner", "Size mm", "Edge mm")):
            ttk.Label(mg, text=cap, style="Field.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0 if col == 0 else 8, 0))
        ttk.Combobox(mg, textvariable=self.marker_corner, width=12,
                     state="readonly", values=list(MARKER_CORNERS)).grid(
            row=1, column=0, sticky="we", pady=(3, 0))
        ttk.Entry(mg, textvariable=self.marker_mm, width=6).grid(
            row=1, column=1, sticky="we", padx=(8, 0), pady=(3, 0))
        ttk.Entry(mg, textvariable=self.marker_pad, width=6).grid(
            row=1, column=2, sticky="we", padx=(8, 0), pady=(3, 0))
        for col, wgt in enumerate((3, 2, 2)):
            mg.columnconfigure(col, weight=wgt)
        ttk.Button(c, text="Save AR kit", style="Tiny.TButton",
                   command=self._save_ar).pack(anchor="w", pady=(10, 0))
        self.marker_hint = ttk.Label(c, style="Muted.TLabel", wraplength=250,
                                     justify="left", text="")
        self.marker_hint.pack(anchor="w", pady=(8, 0))
        for _v in (self.marker_corner, self.marker_mm, self.marker_pad,
                   self.marker_style, self.marker_second):
            _v.trace_add("write", lambda *_a: self._marker_hint())
        self._marker_hint()

        c = make_card(left, "", "The printed part")
        ttk.Label(c, style="Muted.TLabel", wraplength=250, justify="left",
                  text="The model covers the middle of the page, so there is no "
                       "point printing what goes under it.").pack(anchor="w")
        for val, txt in (("blank", "leave it blank"),
                         ("outline", "blank, with a placement outline"),
                         ("keep", "print the map there too")):
            ttk.Radiobutton(c, text=txt, value=val,
                            variable=self.cut).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(c, text="turn the map 180 degrees",
                        variable=self.rotate180,
                        command=self._sync).pack(anchor="w", pady=(10, 0))
        ttk.Label(c, style="Muted.TLabel", wraplength=250, justify="left",
                  text="For when the model sits on the plate the other way "
                       "round. The squares stay in their corners, so the AR "
                       "sheet still works.").pack(anchor="w", pady=(2, 0))

        self.info = ttk.Label(right, style="PageMuted.TLabel", wraplength=560,
                              justify="left", text="")
        self.info.pack(anchor="w", pady=(0, 6))
        holder = tk.Frame(right, background=FIELD, highlightthickness=1,
                          highlightbackground=PINK_LINE)
        holder.pack(fill="both", expand=True)
        self.view = tk.Label(holder, background=FIELD, fg=MUTED,
                             text="\npress Build preview\n")
        self.view.pack(fill="both", expand=True)

        self._sync()
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(80, self._poll)

    # ------------------------------------------------------------------ bits
    def _sync(self, *_a):
        real = self.style.get() == "realistic"
        self.lbl_chk.configure(state="disabled" if real else "normal")
        if real:
            self.labels.set(False)
        if self.mono.get():
            why = "Black and white uses its own light greys -- untick it to use "                  "these colours."
        elif real or self.labels.get():
            why = "These colours apply to the drawn style with no labels; the "                  "other two arrive as ready-made pictures."
        else:
            why = "Click a swatch to change it."
        self.pal_hint.configure(text=why)
        self._refresh_swatches()
        self._describe()

    def _refresh_swatches(self):
        for key, sw in self.swatches.items():
            sw.configure(background="#%02x%02x%02x"
                                    % tuple(self.app.backdrop_palette[key]))

    def _pick_colour(self, key):
        cur = tuple(self.app.backdrop_palette[key])
        rgb, _hex = colorchooser.askcolor(color="#%02x%02x%02x" % cur,
                                          title="Backdrop colour", parent=self)
        if rgb:
            self.app.backdrop_palette[key] = tuple(int(c) for c in rgb)
            self._refresh_swatches()

    def _refresh_themes(self, select=None):
        self.theme_box.configure(values=list(self.app.themes))
        if select:
            self.theme_name.set(select)

    def _apply_theme(self, *_a):
        pal = self.app.themes.get(self.theme_name.get())
        if not pal:
            return
        self.app.backdrop_palette.update(pal)
        self.mono.set(False)
        self._refresh_swatches()
        self._sync()
        self.app._log("INFO", f"backdrop theme {self.theme_name.get()!r} applied")

    def _theme_save(self):
        name = simpledialog.askstring("Save colour theme", "Name this theme:",
                                      parent=self,
                                      initialvalue=self.theme_name.get())
        if not name or not name.strip():
            return
        name = name.strip()
        if name in BUILTIN_THEMES:
            messagebox.showinfo("Save colour theme",
                                f"{name!r} is a built-in -- pick another name.",
                                parent=self)
            return
        self.app.themes[name] = dict(self.app.backdrop_palette)
        try:
            save_themes(self.app.themes)
        except OSError as exc:
            messagebox.showerror("Save colour theme", str(exc), parent=self)
            return
        self._refresh_themes(select=name)
        self.app._log("SUCCESS", f"colour theme {name!r} saved to "
                                 f"{os.path.basename(THEME_FILE)}")

    def _theme_delete(self):
        name = self.theme_name.get()
        if name in BUILTIN_THEMES:
            messagebox.showinfo("Delete colour theme",
                                "Built-in themes stay put.", parent=self)
            return
        if name not in self.app.themes:
            return
        if not messagebox.askyesno("Delete colour theme", f"Delete {name!r}?",
                                   parent=self):
            return
        self.app.themes.pop(name, None)
        try:
            save_themes(self.app.themes)
        except OSError as exc:
            messagebox.showerror("Delete colour theme", str(exc), parent=self)
            return
        self._refresh_themes(select="Paper (default)")
        self.app._log("INFO", f"colour theme {name!r} deleted")

    def _match_print(self):
        self.app.backdrop_palette.update(self.app.print_palette())
        self.mono.set(False)
        self._refresh_swatches()
        self._sync()
        own = ([k for k, w in self.app.layer_widgets.items() if w["custom"].get()]
               + [z for z, v in self.app.zone_vars.items() if v["custom"].get()])
        self.app._log("INFO", "backdrop colours matched to the 3D print -- "
                      + (", ".join(own) + " print in their own filament; "
                         if own else "")
                      + "everything else uses the plate colour, so it does here too")

    def _reset_palette(self):
        self.app.backdrop_palette.update(BACKDROP_PALETTE)
        self._refresh_swatches()

    def _num(self, var, default):
        try:
            return float(var.get().strip())
        except ValueError:
            return default

    def _area_bbox(self):
        """The area to draw. A capture if there is one, otherwise whatever the
        coordinate fields say -- printing the paper never needs a built model."""
        if self.app.capture_bbox is not None:
            return tuple(self.app.capture_bbox)
        try:
            return tuple(float(self.app.vars[k].get())
                         for k in ("south", "west", "north", "east"))
        except (ValueError, KeyError):
            return None

    def _model_width_mm(self):
        w = self.app._num_or0("target_width_mm")
        if w <= 0:
            res = getattr(self.app, "_last_result", None)
            w = float(res.size_mm[0]) if res is not None else 0.0
        return w or 180.0            # a sane scale so the sheet is still useful

    def _cut_bbox(self):
        """The plate's real outline: the area cropped to whatever aspect the
        model is (or would be) built at."""
        bbox = self._area_bbox()
        if bbox is None:
            return None
        w = self.app._num_or0("target_width_mm")
        d = self.app._num_or0("target_depth_mm")
        ratio = (f"{w}:{d}" if w > 0 and d > 0
                 else self.app.vars["aspect_ratio"].get().strip() or "free")
        try:
            return gen._fit_bbox_to_ratio(bbox, ratio)
        except Exception:  # noqa: BLE001
            return bbox

    def _marker_hint(self):
        if not self.marker_on.get():
            self.marker_hint.configure(
                text="A square target in one corner. Scan it with a phone and "
                     "the model can be shown standing on the paper.")
            return
        mm = self._num(self.marker_mm, 45.0)
        style = self.marker_style.get()
        how = {
            "aruco": "A small black square with a 4x4 code in it. Read from its "
                     "own geometry, so 20 mm is plenty and there is nothing to "
                     "compile -- Save AR kit writes a scanner page you can host.",
            "legend": "A title block with a scale bar and a north arrow -- it "
                      "reads as map furniture, not a scanning code.",
            "compass": "A compass rose; its tick ring is what the tracker "
                       "locks on to.",
            "pattern": "The busiest and most reliable target, but it does look "
                       "like a sticker.",
            "map area": "Nothing is printed at all -- that patch of the map "
                        "itself is the target. Needs a busy map there: "
                        "satellite, or the drawn style with labels on.",
        }.get(style, "")
        got = (self._info or {}).get("marker") or {}
        measured = ""
        if got.get("style") == style and got.get("strength"):
            measured = ("   Last build measured %.0f features per megapixel%s"
                        % (got["strength"],
                           " -- thin, see the log."
                           if got["strength"] < MARKER_WEAK_BELOW else "."))
        self.marker_hint.configure(
            text="%.0f mm square in the %s.  %s%s" % (mm, self.marker_corner.get(),
                                                      how, measured))

    def _marker_kwargs(self):
        bbox = self._area_bbox() or (0, 0, 0, 0)
        place = (self.app.capture_place or "").strip()
        return dict(
            marker=bool(self.marker_on.get()),
            marker_corner=self.marker_corner.get(),
            marker_style=self.marker_style.get(),
            marker_aruco_id=pick_aruco_id(_marker_seed(
                "%.6f,%.6f,%.6f,%.6f" % tuple(bbox))),
            marker_second=bool(self.marker_second.get()),
            marker_mm=self._num(self.marker_mm, 45.0),
            marker_pad_mm=self._num(self.marker_pad, 6.0),
            marker_seed="%.6f,%.6f,%.6f,%.6f" % tuple(bbox),
            marker_label=place or "City map",
            marker_sub="%.4f, %.4f" % ((bbox[0] + bbox[2]) / 2.0,
                                       (bbox[1] + bbox[3]) / 2.0))

    def _save_ar(self):
        if not self.marker_on.get():
            messagebox.showinfo("AR kit", "Switch the marker on first -- the "
                                          "kit is built around it.", parent=self)
            return
        if self._img is None or not (self._info or {}).get("marker"):
            self.info.configure(text="building the sheet with its marker ...")
            self._build(preview=False)
            self.after(200, self._save_ar_when_ready)
            return
        self._write_ar()

    def _save_ar_when_ready(self, waited=0.0):
        if self._busy:
            if waited > 300:
                return
            self.after(200, lambda: self._save_ar_when_ready(waited + 0.2))
            return
        if self._img is not None and (self._info or {}).get("marker"):
            self._write_ar()

    def _write_ar(self):
        out = self.app.vars["output_stl"].get().strip()
        stem = os.path.splitext(os.path.basename(out or "city_map"))[0]
        base = os.path.dirname(out) or SCRIPT_DIR
        folder = filedialog.askdirectory(
            parent=self, title="Where to put the AR kit",
            initialdir=base if os.path.isdir(base) else SCRIPT_DIR)
        if not folder:
            return
        folder = os.path.join(folder, stem + "_ar")
        res = getattr(self.app, "_last_result", None)
        stls = list(getattr(res, "output_paths", ())) or sorted(
            _glob.glob(os.path.splitext(out)[0] + "_*.stl"))
        b = self._area_bbox() or (0, 0, 0, 0)
        try:
            names = export_ar_kit(
                folder, stem, self._info, stl_paths=stls,
                colmap=self.app._colmap(),
                title=(self.app.capture_place or "City map"),
                area="S=%.6f W=%.6f N=%.6f E=%.6f" % tuple(b),
                model_mm=self._plate_mm(), progress=self._say)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("AR kit", str(exc), parent=self)
            return
        for n in names:
            self.app._log("SUCCESS", f"   wrote {os.path.join(folder, n)}")
        # an ArUco square is read from its own geometry, so that kit is
        # finished as written; only an image target needs compiling
        if self.marker_style.get() == "aruco":
            tail = ("\n\nNothing to compile: an ArUco square is read from"
                    " its own geometry.\nPut this folder on any https:// site"
                    " and open it.\n\n")
        else:
            tail = ("\n\nOne step is left: compile " + stem +
                    "_marker.png at\n"
                    "https://hiukim.github.io/mind-ar-js-doc/tools/compile\n"
                    "and save the result in that folder as targets.mind.\n\n")
        messagebox.showinfo(
            "AR kit",
            "Written to\n\n" + folder + "\n\n"
            + "\n".join("  " + n for n in names)
            + tail + "README.txt in there repeats all of this.", parent=self)

    def _plate_mm(self):
        """How big the printed model actually is, in millimetres."""
        box = self._cut_bbox()
        mw = self._model_width_mm()
        if box is None or mw <= 0:
            return 0.0, 0.0
        w_m, h_m = bbox_size_m(box)
        scale = mw / max(w_m, 1e-9)
        return w_m * scale, h_m * scale

    def _paper_mm(self):
        pw, ph = PAPERS.get(self.paper.get(), PAPERS["A4"])
        return (ph, pw) if self.landscape.get() else (pw, ph)

    def _effective_wider(self):
        """The zoom-out the sheet needs. A frame width wins over 'wider x':
        the map has to shrink for there to be any paper left around the model,
        and that is the only lever, since the model's size is fixed."""
        want = self._num(self.frame, 0.0)
        plain = max(self._num(self.wider, 1.0), 0.05)
        mw, mh = self._plate_mm()
        if want <= 0 or mw <= 0:
            return plain
        pw, ph = self._paper_mm()
        need = max(mw / max(pw - 2 * want, 1e-6), mh / max(ph - 2 * want, 1e-6))
        return max(need, 1.0)          # never draw the map larger than the model

    def _true_scale_width(self, want):
        """The model width that would leave a ``want`` mm frame with the map
        still at the model's own scale."""
        mw, mh = self._plate_mm()
        if mw <= 0:
            return 0.0
        pw, ph = self._paper_mm()
        return min(pw - 2 * want, (ph - 2 * want) * mw / max(mh, 1e-9))

    def _describe(self, *_a):
        bbox = self._area_bbox()
        mw = self._model_width_mm()
        if bbox is None:
            self.info.configure(
                text="No area yet -- capture one, pick a recent one, or paste "
                     "coordinates in Advanced > Area.")
            return
        pw, ph = PAPERS.get(self.paper.get(), PAPERS["A4"])
        if self.landscape.get():
            pw, ph = ph, pw
        wider = self._effective_wider()
        _pb, mpm = poster_bbox(bbox, mw, pw, ph, wider)
        pmw, pmh = self._plate_mm()
        fx, fy = (pw - pmw / wider) / 2.0, (ph - pmh / wider) / 2.0
        bits = ["%s   .   model %.0f x %.0f mm   .   1:%s   .   sheet covers "
                "%.0f x %.0f m"
                % (self.paper.get() + (" landscape" if self.landscape.get() else ""),
                   pmw, pmh, f"{int(round(mpm * 1000)):,}", pw * mpm, ph * mpm)]
        bits.append("Frame left: %.0f mm at the sides, %.0f mm top and bottom."
                    % (max(fx, 0), max(fy, 0)))
        if wider > 1.001:
            bits.append("The map is drawn %.0f%% smaller than the model to make "
                        "that room, so the streets meet the plate edge only "
                        "roughly." % ((1 - 1 / wider) * 100))
        elif min(fx, fy) < 20:
            want = 20.0
            fit = self._true_scale_width(want)
            more = [k for k, (a, b) in PAPERS.items()
                    if max(a, b) >= max(pmh, pmw) + 2 * want
                    and min(a, b) >= min(pmh, pmw) + 2 * want]
            bits.append("That is a thin frame -- the sheet is printed at the "
                        "model's own scale, so the hole is the model's own size. "
                        "For a 20 mm frame at true scale, print the model at "
                        "%.0f mm%s, or set Frame mm above and accept a small "
                        "scale difference."
                        % (fit, " or use " + "/".join(sorted(more)) if more else ""))
        self.info.configure(text="   ".join(bits))

    # ----------------------------------------------------------------- build
    def _build(self, preview=True):
        if self._busy:
            return
        bbox = self._area_bbox()
        mw = self._model_width_mm()
        if bbox is None:
            messagebox.showinfo("Backdrop", "Pick an area first -- capture one, "
                                            "reopen a recent one, or paste "
                                            "coordinates in Advanced > Area.",
                                parent=self)
            return
        self._busy = True
        self.go.configure(state="disabled", text="building ...")
        dpi = int(self._num(self.dpi, 200)) if not preview else 96
        kw = dict(paper=self.paper.get(), landscape=bool(self.landscape.get()),
                  dpi=dpi, style=self.style.get(), labels=bool(self.labels.get()),
                  mono=bool(self.mono.get()), cut=self.cut.get(),
                  rotate180=bool(self.rotate180.get()),
                  cut_bbox=self._cut_bbox(), polygon=list(self.app._area_polygon),
                  gap_mm=self._num(self.gap, 3.0),
                  wider=self._effective_wider(),
                  palette=None if self.mono.get() else dict(self.app.backdrop_palette),
                  fade=max(min(self._num(self.fade, 0.0), 95.0), 0.0) / 100.0,
                  credit=bool(self.credit.get()), fetch=self.app._cached_fetch,
                  **self._marker_kwargs())
        threading.Thread(target=self._work, args=(bbox, mw, kw), daemon=True).start()

    def _work(self, bbox, mw, kw):
        try:
            img, info = build_poster(bbox, mw, progress=self._say, **kw)
            self._q.put(("ok", img, info))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("err", str(exc), {}))

    def _say(self, msg):
        self.app._log("INFO", "backdrop: " + str(msg))

    def _poll(self):
        try:
            kind, payload, info = self._q.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            self.go.configure(state="normal", text="Build preview")
            if kind == "ok":
                self._img, self._info = payload, info
                self._draw()
                self.app._log("SUCCESS", "backdrop ready: %s at %s dpi, %s"
                              % (info["paper"], info["dpi"], info["scale"]))
            else:
                messagebox.showerror("Backdrop", payload, parent=self)
                self.app._log("ERROR", f"backdrop failed: {payload}")
        try:
            self.after(80, self._poll)
        except tk.TclError:
            pass

    def _draw(self):
        if self._img is None:
            return
        box = (max(self.view.winfo_width() - 12, 200),
               max(self.view.winfo_height() - 12, 200))
        im = self._img.copy()
        im.thumbnail(box, Image.LANCZOS)
        self._tk = ImageTk.PhotoImage(im)
        self.view.configure(image=self._tk, text="")
        self._describe()

    # ------------------------------------------------------------------ save
    def _save(self, kind):
        """Save straight away. When all we have is a quick preview -- or
        nothing at all -- build the print version first. No model, no generate
        and no capture needed; an area is enough."""
        if self._img is not None and int(self._info.get("dpi", 0)) >= 120:
            self._write(kind)
            return
        if self._area_bbox() is None:
            messagebox.showinfo("Backdrop", "Pick an area first -- capture one, "
                                            "reopen a recent one, or paste "
                                            "coordinates in Advanced > Area.",
                                parent=self)
            return
        self.info.configure(text="building the print version, then saving ...")
        self._build(preview=False)
        self._save_when_ready(kind)

    def _save_when_ready(self, kind, waited=0.0):
        if self._busy:
            if waited > 300:                       # something is badly stuck
                return
            self.after(200, lambda: self._save_when_ready(kind, waited + 0.2))
            return
        if self._img is not None and int(self._info.get("dpi", 0)) >= 120:
            self._write(kind)

    def _write(self, kind):
        stem = os.path.splitext(os.path.basename(
            self.app.vars["output_stl"].get().strip() or "city_map"))[0]
        folder = os.path.dirname(self.app.vars["output_stl"].get().strip()) or SCRIPT_DIR
        p = filedialog.asksaveasfilename(
            parent=self, defaultextension="." + kind,
            filetypes=[(kind.upper(), "*." + kind)],
            initialdir=folder if os.path.isdir(folder) else SCRIPT_DIR,
            initialfile=f"{stem}_backdrop_{self.paper.get().replace(' ', '')}.{kind}")
        if not p:
            return
        dpi = int(self._info.get("dpi", 200))
        try:
            if kind == "pdf":
                self._img.convert("RGB").save(p, "PDF", resolution=float(dpi))
            else:
                self._img.save(p, dpi=(dpi, dpi))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Backdrop", str(exc), parent=self)
            return
        self.app._log("SUCCESS", f"backdrop saved to {p}")
        messagebox.showinfo("Backdrop",
                            "Saved\n\n" + p + "\n\nPrint it at 100% / actual size "
                            "-- any 'fit to page' scaling breaks the match with "
                            "the model.", parent=self)


# ---------------------------------------------------------------------------
# Countries and regions: look up a real boundary and print its shape + relief
# ---------------------------------------------------------------------------

# how closely the outline follows the real coastline, in degrees of tolerance
OUTLINE_DETAIL = {"coarse": 0.05, "medium": 0.01, "fine": 0.002}


def _rings_from_geojson(gj):
    """Outer rings of a (Multi)Polygon as [(lat, lon), ...], biggest first."""
    t = (gj or {}).get("type")
    if t == "Polygon":
        raw = [gj["coordinates"][0]]
    elif t == "MultiPolygon":
        raw = [p[0] for p in gj["coordinates"] if p]
    else:
        return []
    rings = []
    for ring in raw:
        pts = [(float(la), float(lo)) for lo, la in ring if
               -90 <= float(la) <= 90 and -180 <= float(lo) <= 180]
        if len(pts) >= 4:
            try:
                a = abs(Polygon([(lo, la) for la, lo in pts]).area)
            except Exception:  # noqa: BLE001
                a = 0.0
            rings.append((a, pts))
    rings.sort(key=lambda r: r[0], reverse=True)
    return [pts for _a, pts in rings]


def search_regions(query, detail="medium", limit=5, timeout=30):
    """Places matching ``query`` that have a real boundary, biggest first.
    Each result: name, bbox, rings, points, size_km."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "jsonv2", "limit": limit,
                                 "polygon_geojson": 1, "addressdetails": 0,
                                 "polygon_threshold":
                                     OUTLINE_DETAIL.get(detail, 0.01)},
                         headers=gen.HEADERS, timeout=timeout)
        items = r.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"place lookup failed: {exc}") from exc
    out = []
    for it in items or []:
        rings = _rings_from_geojson(it.get("geojson"))
        if not rings:
            continue
        bb = it.get("boundingbox") or []
        if len(bb) == 4:
            bbox = (float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3]))
        else:
            las = [p[0] for p in rings[0]]
            los = [p[1] for p in rings[0]]
            bbox = (min(las), min(los), max(las), max(los))
        w_m, h_m = bbox_size_m(bbox)
        out.append({"name": it.get("display_name") or q,
                    "kind": it.get("type") or it.get("class") or "",
                    "bbox": bbox, "rings": rings, "points": len(rings[0]),
                    "size_km": (w_m / 1000.0, h_m / 1000.0)})
    return out


class RegionDialog(tk.Toplevel):
    """Type a country (or a region, an island, a national park) and print its
    real outline with the mountains standing up out of it."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Country or region")
        self.geometry("720x620")
        self.configure(background=BG)
        self.transient(app)
        self.results = []
        self._busy = False
        self._q: queue.Queue = queue.Queue()

        head = ttk.Frame(self, style="Page.TFrame", padding=(14, 14, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text="Country or region", style="Page.TLabel",
                  font=(UI, 13, "bold")).pack(anchor="w")
        ttk.Label(head, style="PageMuted.TLabel", wraplength=660, justify="left",
                  text="Its real boundary becomes the plate, and the ground "
                       "inside it is raised to its true relief. Map layers are "
                       "switched off -- at this size there is nothing to draw "
                       "but the land itself."
                  ).pack(anchor="w", pady=(4, 0))

        row = ttk.Frame(self, style="Page.TFrame", padding=(14, 10, 14, 6))
        row.pack(fill="x")
        self.q = tk.StringVar(value="")
        e = ttk.Entry(row, textvariable=self.q)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self._search())
        ttk.Label(row, text="outline", style="Field.TLabel").pack(side="left",
                                                                  padx=(10, 4))
        self.detail = tk.StringVar(value="medium")
        ttk.Combobox(row, textvariable=self.detail, width=8, state="readonly",
                     values=list(OUTLINE_DETAIL)).pack(side="left")
        self.go = ttk.Button(row, text="Search", style="Accent.TButton",
                             command=self._search)
        self.go.pack(side="left", padx=(8, 0))

        body = ttk.Frame(self, style="Page.TFrame", padding=(14, 4, 14, 8))
        body.pack(fill="both", expand=True)
        self.list = tk.Listbox(body, height=6, activestyle="none",
                               background=SURFACE, foreground=SLATE,
                               selectbackground=PINK, selectforeground="#FFFFFF",
                               relief="flat", highlightthickness=1,
                               highlightbackground=PINK_LINE, font=(UI, 9),
                               borderwidth=0)
        self.list.pack(fill="x")
        self.list.bind("<<ListboxSelect>>", self._draw)
        self.list.bind("<Double-Button-1>", lambda e: self._use())
        self.canvas = tk.Canvas(body, background=FIELD, highlightthickness=1,
                                highlightbackground=PINK_LINE, height=300)
        self.canvas.pack(fill="both", expand=True, pady=(10, 0))
        self.note = ttk.Label(body, style="PageMuted.TLabel", wraplength=660,
                              justify="left", text="")
        self.note.pack(anchor="w", pady=(8, 0))

        foot = ttk.Frame(self, style="Page.TFrame", padding=(14, 0, 14, 14))
        foot.pack(fill="x")
        ttk.Button(foot, text="Use this outline", style="Accent.TButton",
                   command=self._use).pack(side="left")
        ttk.Button(foot, text="Close", style="Ghost.TButton",
                   command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(80, self._poll)
        e.focus_set()

    # ------------------------------------------------------------------ find
    def _search(self):
        if self._busy or not self.q.get().strip():
            return
        self._busy = True
        self.go.configure(state="disabled", text="...")
        self.note.configure(text="searching ...")
        threading.Thread(target=self._work,
                         args=(self.q.get(), self.detail.get()),
                         daemon=True).start()

    def _work(self, q, detail):
        try:
            self._q.put(("ok", search_regions(q, detail)))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("err", str(exc)))

    def _poll(self):
        try:
            kind, payload = self._q.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            self.go.configure(state="normal", text="Search")
            if kind == "err":
                self.note.configure(text=payload)
            else:
                self.results = payload
                self.list.delete(0, "end")
                for r in payload:
                    self.list.insert("end", "%s   [%s]   %.0f x %.0f km"
                                     % (r["name"][:70], r["kind"],
                                        r["size_km"][0], r["size_km"][1]))
                if payload:
                    self.list.selection_set(0)
                    self._draw()
                else:
                    self.note.configure(
                        text="Nothing with a boundary matched that. Try the "
                             "country's English name, or a region like "
                             "'Bavaria' or 'Isle of Skye'.")
                    self.canvas.delete("all")
        try:
            self.after(80, self._poll)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ show
    def _selected(self):
        sel = self.list.curselection()
        return self.results[sel[0]] if sel else None

    def _draw(self, *_a):
        self.canvas.delete("all")
        r = self._selected()
        if not r:
            return
        ring = r["rings"][0]
        W = max(self.canvas.winfo_width(), 300)
        H = max(self.canvas.winfo_height(), 200)
        las = [p[0] for p in ring]
        los = [p[1] for p in ring]
        s, n, w, e = min(las), max(las), min(los), max(los)
        pad = 14
        sc = min((W - 2 * pad) / max(e - w, 1e-9), (H - 2 * pad) / max(n - s, 1e-9))
        ox = (W - (e - w) * sc) / 2.0
        oy = (H - (n - s) * sc) / 2.0
        pts = []
        for la, lo in ring:
            pts += [ox + (lo - w) * sc, H - oy - (la - s) * sc]
        self.canvas.create_polygon(pts, fill=PINK_SOFT, outline=PINK, width=2)
        extra = len(r["rings"]) - 1
        self.note.configure(
            text="%s\n%d points on the main outline%s   .   %.0f x %.0f km"
                 % (r["name"], r["points"],
                    f"   .   {extra} smaller island(s) will be left off"
                    if extra else "", r["size_km"][0], r["size_km"][1]))

    def _use(self):
        r = self._selected()
        if not r:
            return
        self.app.use_region(r)
        self.destroy()


class HistoryDialog(tk.Toplevel):
    """The areas you have captured before, newest first. Pick one to reopen."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Recent areas")
        self.geometry("560x620")
        self.configure(background=BG)
        self.transient(app)
        self.items = load_history()

        head = ttk.Frame(self, style="Page.TFrame", padding=(14, 12, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text="Recent areas", style="Page.TLabel",
                  font=(UI, 13, "bold")).pack(side="left")
        ttk.Label(head, text=f"{len(self.items)} captured",
                  style="PageMuted.TLabel").pack(side="right")

        body = ttk.Frame(self, style="Page.TFrame", padding=(14, 0, 14, 8))
        body.pack(fill="both", expand=True)
        self.list = tk.Listbox(body, activestyle="none", background=SURFACE,
                               foreground=SLATE, selectbackground=PINK,
                               selectforeground="#FFFFFF", relief="flat",
                               highlightthickness=1, highlightbackground=PINK_LINE,
                               font=(UI, 9), borderwidth=0)
        self.list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.list.yview)
        sb.pack(side="left", fill="y")
        self.list.configure(yscrollcommand=sb.set)
        # a fixed slot, so the list cannot squeeze the preview into a sliver
        pane = tk.Frame(body, background=BG, width=232)
        pane.pack(side="left", fill="y", padx=(12, 0))
        pane.pack_propagate(False)
        self.thumb = tk.Label(pane, background=FIELD, fg=MUTED,
                              highlightthickness=1, highlightbackground=PINK_LINE,
                              text="\nno preview\n")
        self.thumb.pack(fill="both", expand=True)

        foot = ttk.Frame(self, style="Page.TFrame", padding=(14, 0, 14, 14))
        foot.pack(fill="x")
        ttk.Button(foot, text="Open this area", style="Accent.TButton",
                   command=self._use).pack(side="left")
        ttk.Button(foot, text="Forget", style="Ghost.TButton",
                   command=self._forget).pack(side="left", padx=8)
        ttk.Button(foot, text="Close", style="Ghost.TButton",
                   command=self.destroy).pack(side="right")

        for i, it in enumerate(self.items):
            w_m, h_m = bbox_size_m(it["bbox"])
            shape = f"  [{len(it['polygon'])}-pt shape]" if it["polygon"] else ""
            self.list.insert("end", "%s   %.0f x %.0f m   %s%s"
                             % (it["place"] or "(unknown place)", w_m, h_m,
                                it["when"], shape))
        self.list.bind("<<ListboxSelect>>", self._show_thumb)
        self.list.bind("<Double-Button-1>", lambda e: self._use())
        self.bind("<Escape>", lambda e: self.destroy())
        if self.items:
            self.list.selection_set(0)
            self._show_thumb()
        else:
            self.thumb.configure(text="\nnothing captured yet\n")

    def _selected(self):
        sel = self.list.curselection()
        return self.items[sel[0]] if sel else None

    def _show_thumb(self, *_a):
        it = self._selected()
        self._tk = None
        if not it or not it.get("thumb") or not os.path.isfile(it["thumb"]) \
                or not HAVE_PIL:
            self.thumb.configure(image="", text="\nno preview\n")
            return
        try:
            im = Image.open(it["thumb"])
            im.thumbnail((260, 260))
            self._tk = ImageTk.PhotoImage(im)
            self.thumb.configure(image=self._tk, text="")
        except Exception:  # noqa: BLE001
            self.thumb.configure(image="", text="\nno preview\n")

    def _use(self):
        it = self._selected()
        if not it:
            return
        img = None
        if it.get("thumb") and os.path.isfile(it["thumb"]) and HAVE_PIL:
            try:
                img = Image.open(it["thumb"]).convert("RGB")
            except Exception:  # noqa: BLE001
                img = None
        self.app.open_bbox(tuple(it["bbox"]),
                           polygon=[tuple(p) for p in it["polygon"]],
                           place=it["place"], image=img)
        self.destroy()

    def _forget(self):
        it = self._selected()
        if not it:
            return
        self.items = [x for x in self.items if x is not it]
        try:
            save_history(self.items)
            if it.get("thumb") and os.path.isfile(it["thumb"]):
                os.remove(it["thumb"])
        except OSError:
            pass
        idx = self.list.curselection()
        if idx:
            self.list.delete(idx[0])
        self._show_thumb()


class PreviewWindow(tk.Toplevel):
    """Drag to spin, wheel to zoom. A single worker thread renders the newest
    view and throws away anything the user has already moved past, so the
    picture keeps up instead of queueing frames nobody wants any more."""

    DRAG_SCALE = 0.55          # render smaller while the mouse is down
    DRAG_MIN_PX = 2.5          # ... and drop triangles too small to notice

    def __init__(self, app, parts, title="3D preview"):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.geometry("980x780")
        self.configure(background=BG)
        self.parts = parts
        self.az, self.el, self.zoom = 35.0, 28.0, 1.0

        bar = ttk.Frame(self, style="Page.TFrame", padding=(10, 8))
        bar.pack(fill="x")
        for txt, cmd in (("< spin", lambda: self._spin(-25)),
                         ("spin >", lambda: self._spin(25)),
                         ("tilt +", lambda: self._tilt(8)),
                         ("tilt -", lambda: self._tilt(-8))):
            ttk.Button(bar, text=txt, style="Ghost.TButton",
                       command=cmd).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="top", style="Ghost.TButton",
                   command=self._top_view).pack(side="left", padx=(8, 4))
        ttk.Button(bar, text="fit", style="Ghost.TButton",
                   command=self._fit).pack(side="left", padx=(0, 12))
        ttk.Button(bar, text="Save PNG", command=self._save).pack(side="left")
        self._status = ttk.Label(bar, text="rendering ...", style="PageMuted.TLabel")
        self._status.pack(side="right")

        # the hint claims its space first, so the canvas can never squeeze it out
        ttk.Label(self, style="PageMuted.TLabel",
                  text="drag to spin   .   wheel to zoom   .   double-click to reset"
                  ).pack(side="bottom", anchor="w", padx=12, pady=(4, 10))
        holder = tk.Frame(self, background=LOG_BG, highlightthickness=1,
                          highlightbackground=PINK_LINE)
        holder.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 2))
        self._lbl = tk.Label(holder, background=LOG_BG, anchor="center",
                             cursor="fleur")
        self._lbl.pack(fill="both", expand=True)

        self._img = None                 # last full-quality PIL image
        self._tk = None
        self._drag = None
        self._settle = None
        self._size = (900, 640)
        self._q: queue.Queue = queue.Queue()
        self._want = None
        self._closed = False
        self._cv = threading.Condition()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        for seq, fn in (("<ButtonPress-1>", self._press),
                        ("<B1-Motion>", self._motion),
                        ("<ButtonRelease-1>", self._release),
                        ("<Double-Button-1>", lambda e: self._fit()),
                        ("<MouseWheel>", self._wheel),
                        ("<Button-4>", self._wheel), ("<Button-5>", self._wheel),
                        ("<Configure>", self._resized)):
            self._lbl.bind(seq, fn)
        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(50, self._poll)
        self._request(fast=False)

    # ------------------------------------------------------------- rendering
    def _request(self, fast=False):
        w, h = self._size
        if fast:
            w, h = max(int(w * self.DRAG_SCALE), 60), max(int(h * self.DRAG_SCALE), 60)
        job = {"w": w, "h": h, "az_deg": self.az, "el_deg": self.el,
               "zoom": self.zoom, "min_px": self.DRAG_MIN_PX if fast else 0.02}
        with self._cv:
            self._want = (job, fast)
            self._cv.notify()

    def _worker_loop(self):
        while True:
            with self._cv:
                while self._want is None and not self._closed:
                    self._cv.wait()
                if self._closed:
                    return
                (job, fast), self._want = self._want, None
            try:
                self._q.put(("img", render_iso(self.parts, **job), fast))
            except Exception as exc:  # noqa: BLE001
                self._q.put(("err", str(exc), fast))

    def _poll(self):
        latest = None
        try:
            while True:
                latest = self._q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            kind, payload, fast = latest
            if kind == "img":
                self._show(payload, fast)
            else:
                self._status.configure(text=f"render failed: {payload}")
        try:
            self.after(33, self._poll)
        except tk.TclError:
            pass

    def _show(self, img, fast):
        if fast:                      # blow the small render back up to fit
            img = img.resize(self._size, Image.BILINEAR)
        else:
            self._img = img
        self._tk = ImageTk.PhotoImage(img)
        self._lbl.configure(image=self._tk)
        self._status.configure(
            text=f"spin {self.az:.0f}   tilt {self.el:.0f}   zoom {self.zoom:.2f}x")

    def _settle_soon(self, ms=180):
        """Follow a burst of cheap renders with one sharp one."""
        if self._settle is not None:
            try:
                self.after_cancel(self._settle)
            except tk.TclError:
                pass
        self._settle = self.after(ms, lambda: self._request(fast=False))

    # -------------------------------------------------------------- gestures
    def _press(self, ev):
        self._drag = (ev.x, ev.y)

    def _motion(self, ev):
        if self._drag is None:
            return
        dx, dy = ev.x - self._drag[0], ev.y - self._drag[1]
        self._drag = (ev.x, ev.y)
        self.az = (self.az + dx * 0.45) % 360.0
        self.el = max(0.0, min(88.0, self.el + dy * 0.35))
        self._request(fast=True)

    def _release(self, _ev):
        self._drag = None
        self._request(fast=False)

    def _wheel(self, ev):
        step = ev.delta / 120.0 if getattr(ev, "delta", 0) else (
            1.0 if getattr(ev, "num", 0) == 4 else -1.0)
        self.zoom = max(0.25, min(8.0, self.zoom * (1.12 ** step)))
        self._request(fast=True)
        self._settle_soon()
        return "break"

    def _resized(self, ev):
        size = (max(ev.width, 120), max(ev.height, 120))
        if size == self._size:
            return
        self._size = size
        self._request(fast=True)
        self._settle_soon(260)

    def _spin(self, d):
        self.az = (self.az + d) % 360.0
        self._request(fast=False)

    def _tilt(self, d):
        self.el = max(0.0, min(88.0, self.el + d))
        self._request(fast=False)

    def _top_view(self):
        # el is the angle away from straight down: 0 looks at the plate from
        # above, 90 looks at it edge-on
        self.az, self.el = 0.0, 1.0
        self._request(fast=False)

    def _fit(self):
        self.az, self.el, self.zoom = 35.0, 28.0, 1.0
        self._request(fast=False)

    def _save(self):
        if self._img is None:
            return
        p = filedialog.asksaveasfilename(
            parent=self, defaultextension=".png", filetypes=[("PNG", "*.png")],
            initialdir=os.path.dirname(self.app.vars["output_stl"].get()) or SCRIPT_DIR,
            initialfile=time.strftime("city_map_preview_%Y%m%d-%H%M%S.png"))
        if p:
            self._img.save(p)
            self.app._log("SUCCESS", f"preview saved to {p}")

    def destroy(self):
        with self._cv:
            self._closed = True
            self._cv.notify()
        super().destroy()


# ---------------------------------------------------------------------------
# Advanced settings dialog (all the knobs, out of the main window's way)
# ---------------------------------------------------------------------------

class AdvancedDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Advanced settings")
        self.geometry("580x640")
        self.configure(background=BG)
        self.transient(app)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self._tab_layers(nb)
        self._tab_landcover(nb)
        self._tab_model(nb)
        self._tab_area(nb)
        self._tab_network(nb)
        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        ttk.Button(bar, text="Reset to defaults", command=self._reset).pack(side="left")
        ttk.Button(bar, text="Done", style="Accent.TButton",
                   command=self._close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self):
        self.app.mirror_lines = [x.strip() for x in
                                 self.mirror_text.get("1.0", "end").splitlines()
                                 if x.strip()]
        self.destroy()

    def _reset(self):
        if messagebox.askyesno("Reset", "Reset all advanced settings to defaults?",
                               parent=self):
            self.app.init_settings_vars(reset=True)
            self.destroy()
            AdvancedDialog(self.app)

    def _row(self, parent, r, label, var, width=12, hint=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=var, width=width).grid(row=r, column=1, sticky="w")
        if hint:
            ttk.Label(parent, text=hint, style="Muted.TLabel").grid(
                row=r, column=2, sticky="w", padx=8)
        return r + 1

    def _tab_layers(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Layers")
        lf = ttk.LabelFrame(f, text="Buildings, roads, rail", padding=10)
        lf.pack(fill="x")
        for c, t in enumerate(("on", "layer", "height m", "mode", "width m",
                               "own", "colour")):
            ttk.Label(lf, text=t, style="Step.TLabel").grid(
                row=0, column=c, padx=5, pady=(0, 6), sticky="w")
        self.layer_swatches = {}
        for r, (key, label, is_line) in enumerate(LAYER_ROWS, start=1):
            s = self.app.layer_widgets[key]
            ttk.Checkbutton(lf, variable=s["enabled"]).grid(row=r, column=0, padx=5)
            ttk.Label(lf, text=label).grid(row=r, column=1, sticky="w", padx=5)
            ttk.Entry(lf, textvariable=s["height"], width=8,
                      state=("disabled" if key == "buildings" else "normal")).grid(
                row=r, column=2, padx=5)
            ttk.OptionMenu(lf, s["mode"], s["mode"].get(), "raised", "engraved").grid(
                row=r, column=3, padx=5, sticky="we")
            ttk.Entry(lf, textvariable=s["width"], width=8,
                      state=("normal" if is_line else "disabled")).grid(row=r, column=4, padx=5)
            ttk.Checkbutton(lf, variable=s["custom"]).grid(row=r, column=5, padx=5)
            sw = tk.Label(lf, width=4, relief="solid", borderwidth=1, cursor="hand2",
                          background="#%02x%02x%02x"
                                     % tuple(self.app.layer_state[key]["color"]))
            sw.grid(row=r, column=6, padx=(5, 2))
            self.layer_swatches[key] = sw
            sw.bind("<Button-1>",
                    lambda e, kk=key: self.app._layer_pick_color(
                        kk, self.layer_swatches[kk]))
            ttk.Button(lf, text="...", width=3, style="Ghost.TButton",
                       command=lambda kk=key: self.app._layer_pick_color(
                           kk, self.layer_swatches[kk])).grid(row=r, column=7, padx=(0, 4))
        hf = ttk.LabelFrame(f, text="Building heights", padding=10)
        hf.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(hf, text="Use real OSM heights (height / building:levels tags)",
                        variable=self.app.use_osm_heights).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(hf, text="Flat height when off (m):").grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        ttk.Entry(hf, textvariable=self.app.vars["uniform_building_height_m"],
                  width=8).grid(row=1, column=1, sticky="w")
        ttk.Label(f, wraplength=500, style="Muted.TLabel",
                  text="A layer with 'own' ticked leaves the grey base and becomes its "
                       "own coloured object in the 3MF plus its own split STL -- that is "
                       "how the buildings around your landmark get their own colour.\n\n"
                       "With OSM heights off every plain building becomes the same flat "
                       "block, but landmarks picked in Detail keep their real "
                       "building:part tiers. 'engraved' cuts roads into the base with a "
                       "boolean and falls back to raised ridges if that fails."
                  ).pack(anchor="w", pady=(10, 0))

    def _tab_landcover(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Land cover")
        top = ttk.LabelFrame(f, text="Shared look (zones without their own colour)",
                             padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Height (m):").grid(row=0, column=0, sticky="e", padx=(0, 6))
        ttk.Entry(top, textvariable=self.app.vars["zone_default_h"], width=8).grid(
            row=0, column=1, sticky="w")
        ttk.Label(top, text="Mode:").grid(row=0, column=2, sticky="e", padx=(14, 6))
        ttk.OptionMenu(top, self.app.zone_default_mode,
                       self.app.zone_default_mode.get(), "raised", "engraved").grid(
            row=0, column=3, sticky="w")

        zf = ttk.LabelFrame(f, text="Zones -- tick 'own' on as many as you like",
                            padding=10)
        zf.pack(fill="x", pady=(10, 0))
        for c, t in enumerate(("on", "zone", "own", "height m", "colour")):
            ttk.Label(zf, text=t, style="Step.TLabel").grid(
                row=0, column=c, padx=5, pady=(0, 6), sticky="w")
        self.zone_swatches = {}
        for r, (z, lbl) in enumerate(ZONE_ROWS, start=1):
            zv = self.app.zone_vars[z]
            ttk.Checkbutton(zf, variable=zv["enabled"]).grid(row=r, column=0, padx=5)
            ttk.Label(zf, text=lbl).grid(row=r, column=1, sticky="w", padx=5)
            ttk.Checkbutton(zf, variable=zv["custom"]).grid(row=r, column=2, padx=5)
            ttk.Entry(zf, textvariable=zv["height"], width=8).grid(row=r, column=3, padx=5)
            sw = tk.Label(zf, width=4, relief="solid", borderwidth=1, cursor="hand2",
                          background="#%02x%02x%02x"
                                     % tuple(self.app.zone_state[z]["color"]))
            sw.grid(row=r, column=4, padx=(5, 2))
            self.zone_swatches[z] = sw
            sw.bind("<Button-1>",
                    lambda e, zz=z: self.app._zone_pick_color(zz, self.zone_swatches[zz]))
            ttk.Button(zf, text="...", width=3, style="Ghost.TButton",
                       command=lambda zz=z: self.app._zone_pick_color(
                           zz, self.zone_swatches[zz])).grid(row=r, column=5, padx=(0, 4))
        ttk.Label(f, style="Muted.TLabel", wraplength=500,
                  text="A zone with 'own' ticked becomes its own coloured object in the "
                       "3MF and its own split STL. Untouched zones merge into the grey "
                       "base. Several zones can have their own colour at the same time."
                  ).pack(anchor="w", pady=(10, 0))

        tf = ttk.LabelFrame(f, text="Terrain", padding=10)
        tf.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(tf, text="Real elevation relief (open terrain tiles; "
                                 "rectangular areas only)",
                        variable=self.app.terrain_on).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(tf, text="Relief height (mm):").grid(row=1, column=0, sticky="e",
                                                       padx=(0, 6), pady=4)
        ttk.Entry(tf, textvariable=self.app.vars["terrain_relief_mm"], width=8).grid(
            row=1, column=1, sticky="w")
        ttk.Label(tf, text="0 = use the exaggeration below", style="Muted.TLabel").grid(
            row=1, column=2, sticky="w", padx=(8, 0))
        ttk.Label(tf, text="Exaggeration (x):").grid(row=2, column=0, sticky="e",
                                                     padx=(0, 6), pady=4)
        ttk.Entry(tf, textvariable=self.app.vars["terrain_exaggeration"], width=8).grid(
            row=2, column=1, sticky="w")
        ttk.Label(tf, text="Detail (samples):").grid(row=3, column=0, sticky="e",
                                                     padx=(0, 6), pady=4)
        ttk.Entry(tf, textvariable=self.app.vars["terrain_samples"], width=8).grid(
            row=3, column=1, sticky="w")
        ttk.Label(tf, text="Water level (m):").grid(row=4, column=0, sticky="e",
                                                    padx=(0, 6), pady=4)
        ttk.Entry(tf, textvariable=self.app.vars["terrain_sea_level_m"],
                  width=8).grid(row=4, column=1, sticky="w")
        ttk.Label(tf, text="anything lower is flattened to it",
                  style="Muted.TLabel").grid(row=4, column=2, sticky="w",
                                             padx=(8, 0))
        ttk.Label(tf, style="Muted.TLabel", wraplength=470, justify="left",
                  text="Relief height is the simple one: say how far the highest "
                       "ground should stand above the plate and the exaggeration "
                       "is worked out for you. More samples keep sharper ridges "
                       "and cost more triangles."
                  ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _tab_model(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Model")
        g = ttk.LabelFrame(f, text="Geometry", padding=10)
        g.pack(fill="x")
        r = 0
        r = self._row(g, r, "Base thickness (m):", self.app.vars["base_thickness_m"])
        r = self._row(g, r, "Plate at least (mm):", self.app.vars["base_min_mm"],
                      hint="0 = off; use ~2 for whole countries")
        r = self._row(g, r, "Height scale (x):", self.app.vars["height_scale"],
                      hint="scales real building heights")
        r = self._row(g, r, "Vertical exaggeration (x):",
                      self.app.vars["vertical_exaggeration"],
                      hint="extra Z gain after sizing")
        r = self._row(g, r, "Min footprint area (m2):",
                      self.app.vars["min_footprint_area_m2"],
                      hint="drops slivers")
        r = self._row(g, r, "Simplify tolerance (m):",
                      self.app.vars["simplify_tolerance_m"],
                      hint="0 = full fidelity")
        r = self._row(g, r, "Level height (m):",
                      self.app.vars["default_level_height_m"],
                      hint="per building:levels")

        ttk.Label(f, style="Muted.TLabel", wraplength=500,
                  text="Detail level lives in the main window, under Detailed "
                       "landmarks. Simplify tolerance above 0 trades outline "
                       "fidelity for smaller files -- landmarks ignore it from "
                       "detail level 2 up."
                  ).pack(anchor="w", pady=(10, 0))

    def _tab_area(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Area")
        g = ttk.LabelFrame(f, text="Captured bounding box (fine tuning)", padding=10)
        g.pack(fill="x")
        for i, key in enumerate(("south", "west", "north", "east")):
            rr, cc = divmod(i, 2)
            ttk.Label(g, text=key.capitalize() + ":").grid(
                row=rr, column=cc * 2, sticky="e", padx=(0, 6), pady=4)
            ttk.Entry(g, textvariable=self.app.vars[key], width=14).grid(
                row=rr, column=cc * 2 + 1, sticky="w")
        ttk.Label(f, style="Muted.TLabel", wraplength=500,
                  text="CAPTURE on the map normally fills these in; edit them here for "
                       "fine tuning."
                  ).pack(anchor="w", pady=(10, 0))

        pf = ttk.LabelFrame(f, text="Open coordinates", padding=10)
        pf.pack(fill="x", pady=(12, 0))
        self.paste_var = tk.StringVar(value="")
        row = ttk.Frame(pf)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.paste_var).pack(side="left", fill="x",
                                                         expand=True)
        ttk.Button(row, text="Open", style="Accent.TButton",
                   command=self._apply_pasted).pack(side="left", padx=(8, 0))
        self.paste_hint = ttk.Label(pf, style="Muted.TLabel", wraplength=480,
                                    justify="left",
                                    text="Paste a box  S=41.887898 W=12.489375 "
                                         "N=41.893417 E=12.495456  (or plain "
                                         "south, west, north, east), or a single "
                                         "lat, lon to centre on -- then press Open "
                                         "to fetch the reference image for it.")
        self.paste_hint.pack(anchor="w", pady=(8, 0))

        ttk.Button(f, text="Clear free-form shape",
                   command=self.app._clear_area_polygon).pack(anchor="w", pady=(12, 0))

    def _apply_pasted(self):
        txt = self.paste_var.get()
        bbox = parse_bbox_text(txt)
        if bbox is None:
            pt = parse_point_text(txt)
            if pt is None:
                self.paste_hint.configure(
                    text="Could not read coordinates out of that. Expected "
                         "something like  S=41.887898 W=12.489375 N=41.893417 "
                         "E=12.495456")
                return
            w_m, h_m = bbox_size_m(self.app.capture_bbox) if self.app.capture_bbox                 else (800.0, 800.0)
            bbox = bbox_around(pt[0], pt[1], w_m, h_m)
            self.paste_hint.configure(
                text="Centred a %.0f x %.0f m area on %.6f, %.6f."
                     % (w_m, h_m, pt[0], pt[1]))
        else:
            self.paste_hint.configure(text="Opened S=%.6f W=%.6f N=%.6f E=%.6f."
                                           % bbox)
        self.app.open_bbox(bbox)

    def _tab_network(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="Network")
        ttk.Label(f, text="Overpass mirrors (one URL per line):").pack(anchor="w")
        self.mirror_text = tk.Text(f, height=6, wrap="none", background=SURFACE,
                                   foreground=SLATE, insertbackground=SLATE,
                                   highlightthickness=1, highlightbackground=PINK_LINE,
                                   relief="flat", font=("Consolas", 9))
        self.mirror_text.pack(fill="x", pady=(4, 10))
        self.mirror_text.insert("1.0", "\n".join(self.app.mirror_lines))
        g = ttk.Frame(f)
        g.pack(fill="x")
        r = 0
        r = self._row(g, r, "Request timeout (s):", self.app.vars["request_timeout_s"])
        r = self._row(g, r, "Attempts per mirror:",
                      self.app.vars["max_attempts_per_mirror"])
        r = self._row(g, r, "Overpass query timeout (s):",
                      self.app.vars["overpass_query_timeout_s"])


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("City Map Generator")
        self.geometry("1300x860")
        self.minsize(1040, 680)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        apply_theme(self)

        self.log_queue: queue.Queue = queue.Queue()
        self.result_q: queue.Queue = queue.Queue()
        self.map_pick_q: queue.Queue = queue.Queue()
        self.preview_q: queue.Queue = queue.Queue()
        self.capture_q: queue.Queue = queue.Queue()

        self._worker = None
        self._picker = None
        self._area_polygon: list = []
        self._preview_parts = None
        self.capture_bbox = None
        self.capture_image = None
        self._thumb_tk = None
        self.detail_osm: dict = {}
        self.detail_color = (216, 84, 138)
        self.capture_place = ""          # town name for the automatic file name

        self.vars: dict = {}
        self.layer_widgets: dict = {}
        self.init_settings_vars()

        self._build_ui()
        self._wire_logging()
        self.after(100, self._pump)
        self._log("INFO", "Ready. Step 1: open the map and CAPTURE the area you want.")

    # ------------------------------------------------------------ settings
    def init_settings_vars(self, reset=False):
        """All advanced settings live here so the dialog can be opened/closed
        without losing anything."""
        def sv(key, val):
            if reset or key not in self.vars:
                self.vars[key] = tk.StringVar(value=str(val))
            return self.vars[key]

        for key in ("south", "west", "north", "east"):
            sv(key, gen.BBOX[("south", "west", "north", "east").index(key)])
        sv("target_width_mm", 180)
        sv("target_depth_mm", 0)
        sv("target_height_mm", 0)
        sv("aspect_ratio", "free")
        sv("size_preset", "Free (no resize)")
        sv("base_thickness_m", gen.BASE_THICKNESS_M)
        sv("base_min_mm", 0)
        sv("height_scale", gen.HEIGHT_SCALE)
        sv("vertical_exaggeration", 1.0)
        sv("min_footprint_area_m2", gen.MIN_FOOTPRINT_AREA_M2)
        sv("simplify_tolerance_m", 0.0)
        sv("default_level_height_m", gen.DEFAULT_LEVEL_HEIGHT_M)
        sv("terrain_exaggeration", 1.5)
        sv("terrain_relief_mm", 0)
        sv("terrain_sea_level_m", 0)
        sv("terrain_samples", 96)
        sv("request_timeout_s", 90)
        sv("max_attempts_per_mirror", 3)
        sv("overpass_query_timeout_s", 90)
        sv("zone_default_h", 0.7)
        sv("detail_extra_m", 0)
        sv("uniform_building_height_m", 12.0)
        sv("detail_level", gen.DETAIL_LEVEL_LABELS[gen.DEFAULT_DETAIL_LEVEL - 1])
        sv("path_width_m", 8)
        sv("path_height_mm", 1.2)
        sv("path_style", "straight")
        sv("model_fit", "height")
        sv("model_scale_pct", 100)
        sv("model_rotate_deg", 0)
        sv("model_upright", "Auto")
        sv("output_stl", os.path.join(SCRIPT_DIR, gen.OUTPUT_STL))

        if reset or not self.layer_widgets:
            self.layer_widgets = {}
            self.layer_state = {}
            dh = dict(buildings=0, roads=1.0, rail=1.2)
            dw = dict(roads=6.0, rail=3.0)
            for key, _lbl, _line in LAYER_ROWS:
                self.layer_widgets[key] = dict(
                    enabled=tk.BooleanVar(value=True),
                    height=tk.StringVar(value=str(dh[key])),
                    mode=tk.StringVar(value="raised"),
                    width=tk.StringVar(value=str(dw.get(key, 0.0))),
                    # buildings start split out: the whole point of the map is
                    # seeing them in their own colour, not merged into the plate
                    custom=tk.BooleanVar(value=(key == "buildings")))
                self.layer_state[key] = {"color": LAYER_SUGGESTED[key]}
        if reset or not hasattr(self, "zone_state"):
            self.zone_state = {z: {"color": ZONE_SUGGESTED[z]} for z, _ in ZONE_ROWS}
            self.zone_vars = {z: dict(enabled=tk.BooleanVar(value=True),
                                      custom=tk.BooleanVar(value=False),
                                      height=tk.StringVar(value="0.7"))
                              for z, _ in ZONE_ROWS}
            self.zone_default_mode = tk.StringVar(value="raised")
        if reset or not hasattr(self, "terrain_on"):
            self.terrain_on = tk.BooleanVar(value=False)
        if reset or not hasattr(self, "use_osm_heights"):
            self.use_osm_heights = tk.BooleanVar(value=True)
        if reset or not hasattr(self, "mirror_lines"):
            self.mirror_lines = list(gen.OVERPASS_URLS)
        if reset or not hasattr(self, "auto_name"):
            self.auto_name = tk.BooleanVar(value=True)
        if reset or not hasattr(self, "path_on"):
            self.path_on = tk.BooleanVar(value=False)
            self.path_closed = tk.BooleanVar(value=False)
            self.path_color = (240, 168, 64)
        if reset or not hasattr(self, "size_fit"):
            self.size_fit = tk.BooleanVar(value=False)
        if reset or not hasattr(self, "own_folder"):
            self.own_folder = tk.BooleanVar(value=True)
        if reset or not hasattr(self, "backdrop_palette"):
            self.backdrop_palette = dict(BACKDROP_PALETTE)
        if reset or not hasattr(self, "themes"):
            self.themes = load_themes()
        if reset or not hasattr(self, "presets"):
            self.presets = load_presets()
        if reset or not hasattr(self, "exp_stl"):
            self.exp_stl = tk.BooleanVar(value=True)
            self.exp_3mf = tk.BooleanVar(value=True)
            self.exp_split = tk.BooleanVar(value=True)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        page = ttk.Frame(self, style="Page.TFrame", padding=(16, 14, 16, 14))
        page.pack(fill="both", expand=True)

        # ---- title bar ---------------------------------------------------
        head = ttk.Frame(page, style="Page.TFrame")
        head.pack(fill="x", pady=(0, 14))
        icon, mark = logo_images()
        if mark is not None:
            self._mark_tk = ImageTk.PhotoImage(mark)
            tk.Label(head, image=self._mark_tk, background=BG).pack(
                side="left", padx=(0, 12))
        if icon is not None:
            try:
                self._icon_tk = ImageTk.PhotoImage(icon)
                self.iconphoto(True, self._icon_tk)
                if os.path.isfile(LOGO_ICO):
                    self.iconbitmap(default=LOGO_ICO)
            except Exception:  # noqa: BLE001
                pass
        titles = ttk.Frame(head, style="Page.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text="City Map Generator", style="Head.TLabel").pack(anchor="w")
        ttk.Label(titles, text="capture an area, pick your landmarks, print it",
                  style="Sub.TLabel").pack(anchor="w")
        self.status = tk.StringVar(value="Idle")
        self.status_chip = tk.Label(head, textvariable=self.status, background=PINK_SOFT,
                                    foreground=PINK_DEEP, font=(UI, 9, "bold"),
                                    padx=12, pady=6)
        self.status_chip.pack(side="right")

        panes = ttk.Panedwindow(page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, style="Page.TFrame", padding=(0, 0, 14, 0))
        right = ttk.Frame(panes, style="Page.TFrame")
        panes.add(left, weight=0)
        panes.add(right, weight=1)

        # ---- action strip, pinned to the bottom of the left column -------
        act2 = ttk.Frame(left, style="Page.TFrame")
        act2.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(act2, text="Save log", style="Ghost.TButton",
                   command=self._save_log_as).pack(side="left")
        ttk.Button(act2, text="Clear log", style="Ghost.TButton",
                   command=lambda: self.log_text.delete("1.0", "end")).pack(
            side="left", padx=6)
        ttk.Button(act2, text="Quit", style="Ghost.TButton",
                   command=self.on_close).pack(side="right")
        act = ttk.Frame(left, style="Page.TFrame")
        act.pack(side="bottom", fill="x", pady=(14, 0))
        self.gen_btn = ttk.Button(act, text="GENERATE", style="Accent.TButton",
                                  command=self._on_generate)
        self.gen_btn.pack(side="left")
        ttk.Button(act, text="Preview", command=self._show_preview).pack(
            side="left", padx=8)
        ttk.Button(act, text="Advanced", command=self._open_advanced).pack(side="left")

        holder = ttk.Frame(left, style="Page.TFrame")
        holder.pack(side="top", fill="both", expand=True)
        cards = self._scrollable(holder)

        # ---- 1. area -----------------------------------------------------
        a = make_card(cards, "1", "Area")
        ttk.Button(a, text="Open map   +   capture area", style="Accent.TButton",
                   command=self._open_map).pack(fill="x")
        arow = ttk.Frame(a, style="Card.TFrame")
        arow.pack(fill="x", pady=(8, 0))
        ttk.Button(arow, text="Recent areas", style="Ghost.TButton",
                   command=self._show_history).pack(side="left")
        ttk.Button(arow, text="Country", style="Ghost.TButton",
                   command=self._show_region).pack(side="left", padx=6)
        ttk.Button(arow, text="Backdrop for A4", style="Ghost.TButton",
                   command=self._open_poster).pack(side="right")
        self.thumb = tk.Label(a, background=FIELD, height=6, width=44,
                              highlightthickness=1, highlightbackground=PINK_LINE,
                              fg=MUTED, cursor="hand2", font=(UI, 9),
                              text="\nno area captured yet\n\nframe it on the map,"
                                   "\nthen press CAPTURE\n")
        self.thumb.pack(fill="x", pady=(10, 6))
        self.thumb.bind("<Button-1>", lambda e: self._show_capture_full())
        self.extent_var = tk.StringVar(value="")
        ttk.Label(a, textvariable=self.extent_var, style="Muted.TLabel",
                  wraplength=350).pack(anchor="w")

        # ---- 2. size -----------------------------------------------------
        z = make_card(cards, "2", "Size", "millimetres")
        pr = ttk.Frame(z, style="Card.TFrame")
        pr.pack(fill="x")
        ttk.Label(pr, text="Preset", style="Field.TLabel").pack(side="left")
        self.preset_box = ttk.Combobox(pr, textvariable=self.vars["size_preset"],
                                       state="readonly", width=20,
                                       values=list(self.presets))
        self.preset_box.pack(side="left", fill="x", expand=True, padx=8)
        self.preset_box.bind("<<ComboboxSelected>>", self._apply_preset)
        ttk.Button(pr, text="Save", style="Tiny.TButton",
                   command=self._preset_save).pack(side="left")
        ttk.Button(pr, text="Del", style="Tiny.TButton",
                   command=self._preset_delete).pack(side="left", padx=(4, 0))

        grid = ttk.Frame(z, style="Card.TFrame")
        grid.pack(fill="x", pady=(12, 0))
        for col, (cap, key) in enumerate((("Width X", "target_width_mm"),
                                          ("Depth Y", "target_depth_mm"),
                                          ("Height Z", "target_height_mm"))):
            ttk.Label(grid, text=cap, style="Field.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0 if col == 0 else 10, 0))
            e = ttk.Entry(grid, textvariable=self.vars[key], width=9)
            e.grid(row=1, column=col, sticky="we",
                   padx=(0 if col == 0 else 10, 0), pady=(3, 0))
            grid.columnconfigure(col, weight=1)
            self.vars[key].trace_add("write", self._size_summary)
        ttk.Label(grid, text="Aspect", style="Field.TLabel").grid(
            row=0, column=3, sticky="w", padx=(10, 0))
        ttk.Combobox(grid, textvariable=self.vars["aspect_ratio"], width=6,
                     state="readonly", values=ASPECT_PRESETS).grid(
            row=1, column=3, sticky="we", padx=(10, 0), pady=(3, 0))
        self.vars["aspect_ratio"].trace_add("write", self._size_summary)
        ttk.Checkbutton(z, text="fit inside W x D instead of filling it",
                        variable=self.size_fit,
                        command=self._size_summary).pack(anchor="w", pady=(9, 0))
        self.size_hint = ttk.Label(z, style="Muted.TLabel", wraplength=350,
                                   justify="left", text="")
        self.size_hint.pack(anchor="w", pady=(9, 0))
        self._size_summary()

        # ---- 3. landmarks ------------------------------------------------
        d = make_card(cards, "3", "Detailed landmarks", "optional")
        self.detail_list = tk.Listbox(d, height=3, activestyle="none",
                                      background=FIELD, foreground=SLATE,
                                      selectbackground=PINK, selectforeground="#FFFFFF",
                                      highlightthickness=1, highlightbackground=PINK_LINE,
                                      relief="flat", font=(UI, 9), borderwidth=0)
        self.detail_list.pack(fill="x")
        db = ttk.Frame(d, style="Card.TFrame")
        db.pack(fill="x", pady=(8, 0))
        ttk.Button(db, text="Pick on map", command=self._open_map).pack(side="left")
        ttk.Button(db, text="Remove", style="Ghost.TButton",
                   command=self._detail_remove).pack(side="left", padx=6)
        ttk.Button(db, text="Clear", style="Ghost.TButton",
                   command=self._detail_clear).pack(side="left")

        # anchored right so they always have room, whatever the card width
        ttk.Button(db, text="dn", style="Tiny.TButton", width=3,
                   command=lambda: self._detail_move(1)).pack(side="right")
        ttk.Button(db, text="up", style="Tiny.TButton", width=3,
                   command=lambda: self._detail_move(-1)).pack(side="right",
                                                               padx=(0, 4))

        db3 = ttk.Frame(d, style="Card.TFrame")
        db3.pack(fill="x", pady=(10, 0))
        ttk.Label(db3, text="Detail", style="Field.TLabel").pack(side="left")
        ttk.Combobox(db3, textvariable=self.vars["detail_level"], width=18,
                     state="readonly", values=list(gen.DETAIL_LEVEL_LABELS)).pack(
            side="left", padx=8)
        self.detail_swatch = tk.Label(db3, width=4, height=1, relief="flat",
                                      borderwidth=0, cursor="hand2",
                                      background="#%02x%02x%02x" % self.detail_color)
        self.detail_swatch.pack(side="right", pady=2)
        self.detail_swatch.bind("<Button-1>", lambda e: self._detail_pick_color())
        ttk.Label(db3, text="colour", style="Field.TLabel").pack(side="right", padx=6)
        self.detail_hint = ttk.Label(d, style="Muted.TLabel", wraplength=350, text="")
        self.detail_hint.pack(anchor="w", pady=(6, 0))
        self.vars["detail_level"].trace_add("write",
                                            lambda *_a: self._detail_level_hint())
        self._detail_level_hint()

        db4 = ttk.Frame(d, style="Card.TFrame")
        db4.pack(fill="x", pady=(10, 0))
        ttk.Label(db4, text="Own model", style="Field.TLabel").pack(side="left")
        self.model_var = tk.StringVar(value="")
        ttk.Entry(db4, textvariable=self.model_var).pack(side="left", fill="x",
                                                         expand=True, padx=8)
        ttk.Button(db4, text="...", style="Tiny.TButton",
                   command=self._browse_model).pack(side="left")
        ttk.Button(db4, text="x", style="Tiny.TButton",
                   command=lambda: self.model_var.set("")).pack(side="left", padx=(4, 0))

        db5 = ttk.Frame(d, style="Card.TFrame")
        db5.pack(fill="x", pady=(8, 0))
        for col, cap in enumerate(("Fit", "Scale %", "Turn deg", "Up")):
            ttk.Label(db5, text=cap, style="Field.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0 if col == 0 else 8, 0))
        ttk.Combobox(db5, textvariable=self.vars["model_fit"], width=10,
                     state="readonly", values=list(gen.MODEL_FITS)).grid(
            row=1, column=0, sticky="we", pady=(3, 0))
        ttk.Entry(db5, textvariable=self.vars["model_scale_pct"], width=6).grid(
            row=1, column=1, sticky="we", padx=(8, 0), pady=(3, 0))
        ttk.Entry(db5, textvariable=self.vars["model_rotate_deg"], width=6).grid(
            row=1, column=2, sticky="we", padx=(8, 0), pady=(3, 0))
        ttk.Combobox(db5, textvariable=self.vars["model_upright"], width=7,
                     state="readonly", values=UPRIGHT_CHOICES).grid(
            row=1, column=3, sticky="we", padx=(8, 0), pady=(3, 0))
        for col, wgt in enumerate((3, 2, 2, 2)):
            db5.columnconfigure(col, weight=wgt)
        self.model_hint = ttk.Label(d, style="Muted.TLabel", wraplength=350, text="")
        self.model_hint.pack(anchor="w", pady=(6, 0))
        self.vars["model_fit"].trace_add("write", lambda *_a: self._model_hint())
        self._model_hint()
        ttk.Label(d, style="Muted.TLabel", wraplength=350,
                  text="On the map tick 'pick detailed building' and click a roof. "
                       "An own model is only used at detail level 5, and replaces "
                       "that landmark's OSM geometry."
                  ).pack(anchor="w", pady=(6, 0))

        ttk.Separator(d, orient="horizontal").pack(fill="x", pady=(12, 10))
        pr = ttk.Frame(d, style="Card.TFrame")
        pr.pack(fill="x")
        ttk.Checkbutton(pr, text="join them with a path, in this order",
                        variable=self.path_on,
                        command=self._path_hint).pack(side="left")
        self.path_swatch = tk.Label(pr, width=4, height=1, relief="flat",
                                    borderwidth=0, cursor="hand2",
                                    background="#%02x%02x%02x" % self.path_color)
        self.path_swatch.pack(side="right", pady=2)
        self.path_swatch.bind("<Button-1>", lambda e: self._path_pick_color())
        pg = ttk.Frame(d, style="Card.TFrame")
        pg.pack(fill="x", pady=(8, 0))
        for col, cap in enumerate(("Width m", "Stands mm", "Shape")):
            ttk.Label(pg, text=cap, style="Field.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0 if col == 0 else 8, 0))
        ttk.Entry(pg, textvariable=self.vars["path_width_m"], width=7).grid(
            row=1, column=0, sticky="we", pady=(3, 0))
        ttk.Entry(pg, textvariable=self.vars["path_height_mm"], width=7).grid(
            row=1, column=1, sticky="we", padx=(8, 0), pady=(3, 0))
        ttk.Combobox(pg, textvariable=self.vars["path_style"], width=9,
                     state="readonly", values=list(gen.PATH_STYLES)).grid(
            row=1, column=2, sticky="we", padx=(8, 0), pady=(3, 0))
        for col in range(3):
            pg.columnconfigure(col, weight=1)
        ttk.Checkbutton(d, text="close the loop (last stop back to the first)",
                        variable=self.path_closed).pack(anchor="w", pady=(8, 0))
        self.path_hint = ttk.Label(d, style="Muted.TLabel", wraplength=350,
                                   justify="left", text="")
        self.path_hint.pack(anchor="w", pady=(6, 0))
        self.vars["path_style"].trace_add("write", lambda *_a: self._path_hint())
        self._path_hint()

        # ---- 4. output ---------------------------------------------------
        o = make_card(cards, "4", "Output")
        orow = ttk.Frame(o, style="Card.TFrame")
        orow.pack(fill="x")
        ttk.Entry(orow, textvariable=self.vars["output_stl"]).pack(
            side="left", fill="x", expand=True)
        ttk.Button(orow, text="...", style="Tiny.TButton",
                   command=self._browse_out).pack(side="left", padx=(8, 0))
        nrow = ttk.Frame(o, style="Card.TFrame")
        nrow.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(nrow, text="name files city_landmark_date",
                        variable=self.auto_name,
                        command=self.auto_rename).pack(side="left")
        ttk.Button(nrow, text="Rename now", style="Ghost.TButton",
                   command=self._rename_now).pack(side="right")
        frow = ttk.Frame(o, style="Card.TFrame")
        frow.pack(fill="x", pady=(2, 0))
        ttk.Checkbutton(frow, text="keep each model in its own folder",
                        variable=self.own_folder,
                        command=lambda: self.auto_rename(force=True)).pack(side="left")
        ttk.Button(frow, text="Open folder", style="Ghost.TButton",
                   command=self._open_output_folder).pack(side="right")
        fmt = ttk.Frame(o, style="Card.TFrame")
        fmt.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(fmt, text="STL", variable=self.exp_stl).pack(anchor="w")
        ttk.Checkbutton(fmt, text="Coloured 3MF", variable=self.exp_3mf).pack(anchor="w")
        ttk.Checkbutton(fmt, text="Split parts (base + each colour)",
                        variable=self.exp_split).pack(anchor="w")

        # ---- log ---------------------------------------------------------
        loghead = ttk.Frame(right, style="Page.TFrame")
        loghead.pack(fill="x", pady=(0, 6))
        ttk.Label(loghead, text="Log", style="Page.TLabel",
                  font=(UI, 11, "bold")).pack(side="left")
        ttk.Label(loghead, text=gen.LOG_FILENAME, style="PageMuted.TLabel").pack(
            side="right")
        logbox = tk.Frame(right, background=LOG_BG, highlightthickness=1,
                          highlightbackground=PINK_LINE)
        logbox.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(
            logbox, wrap="word", font=("Consolas", 9), height=40,
            background=LOG_BG, foreground="#E7E1EC", insertbackground="#E7E1EC",
            relief="flat", highlightthickness=0, borderwidth=0, padx=10, pady=8)
        self.log_text.pack(fill="both", expand=True)
        for level, cfg in LEVEL_TAGS.items():
            self.log_text.tag_configure(level, **cfg)
        self.log_text.bind("<Key>", self._readonly_guard)
        self._refresh_detail_list()

    def _scrollable(self, parent, width=396):
        """A vertically scrolling frame -- keeps a column of cards usable at any
        window size / DPI."""
        canvas = tk.Canvas(parent, background=BG, highlightthickness=0,
                           borderwidth=0, width=width)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Page.TFrame")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        for w in (canvas, inner):
            w.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
            w.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _open_advanced(self):
        AdvancedDialog(self)

    # ---------------------------------------------------------------- logging
    def _wire_logging(self):
        gen.setup_logging(logfile=os.path.join(SCRIPT_DIR, gen.LOG_FILENAME),
                          console=False)
        qh = QueueHandler(self.log_queue)
        qh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                                          datefmt="%H:%M:%S"))
        qh.setLevel(logging.DEBUG)
        gen.logger.addHandler(qh)

    def _readonly_guard(self, event):
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            return
        if event.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                            "Prior", "Next", "Shift_L", "Shift_R"):
            return
        return "break"

    def _log(self, level, msg):
        self.log_queue.put((level, msg))

    def _pump(self):
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                at_bottom = self.log_text.yview()[1] >= 0.999
                self.log_text.insert("end", msg + "\n",
                                     (level if level in LEVEL_TAGS else "INFO",))
                if at_bottom:
                    self.log_text.see("end")
        except queue.Empty:
            pass
        try:
            while True:
                self._apply_map_pick(self.map_pick_q.get_nowait())
        except queue.Empty:
            pass
        try:
            bbox, img, place = self.capture_q.get_nowait()
        except queue.Empty:
            pass
        else:
            self._show_capture(bbox, img, place)
        try:
            parts = self.preview_q.get_nowait()
        except queue.Empty:
            pass
        else:
            self._preview_parts = parts
            self._show_preview()
        try:
            ok, result = self.result_q.get_nowait()
        except queue.Empty:
            pass
        else:
            self._finish(ok, result)
        self.after(100, self._pump)

    # ---------------------------------------------------------------- area
    def saved_area(self):
        """What the map page reopens on: the rectangle this app last captured,
        plus the free-form outline when there is one. Empty before the first
        capture, so a fresh start still opens wherever you search."""
        return {"bbox": list(self.capture_bbox) if self.capture_bbox else None,
                "polygon": [[la, lo] for la, lo in (self._area_polygon or [])]}

    def apply_bbox(self, s, w, n, e):
        self.vars["south"].set(repr(round(float(s), 6)))
        self.vars["west"].set(repr(round(float(w), 6)))
        self.vars["north"].set(repr(round(float(n), 6)))
        self.vars["east"].set(repr(round(float(e), 6)))

    def _open_map(self):
        try:
            latc = (float(self.vars["south"].get()) + float(self.vars["north"].get())) / 2
            lonc = (float(self.vars["west"].get()) + float(self.vars["east"].get())) / 2
        except ValueError:
            latc = (gen.BBOX[0] + gen.BBOX[2]) / 2
            lonc = (gen.BBOX[1] + gen.BBOX[3]) / 2
        ratio = self.vars["aspect_ratio"].get().strip() or "free"
        if self._picker is None:
            self._picker = LeafletPicker(self)
        try:
            url = self._picker.open(latc, lonc, ratio)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Map", f"Could not start the local map:\n{exc}",
                                 parent=self)
            return
        self._log("INFO", f"map opened in your browser: {url}")
        if self.capture_bbox is not None:
            self._log("INFO", "  the map opens locked on the area you captured "
                              "(purple dashes) -- pan or zoom to break out of it, "
                              "'back to captured area' to return.")
        else:
            self._log("INFO", "  frame the area in the pink viewfinder and press "
                              "CAPTURE; tick 'pick detailed building' to click "
                              "landmarks.")

    def open_bbox(self, bbox, polygon=(), place="", image=None, note=""):
        """Make this the working area, wherever it came from -- the map, the
        history or pasted coordinates. Fetches the reference image unless one
        was handed over."""
        bbox = tuple(float(v) for v in bbox)
        self._set_area_polygon(list(polygon or []))
        self.apply_bbox(*bbox)
        self.capture_bbox = bbox
        if place:
            self.capture_place = place
        self._log("INFO", (note or "area opened")
                  + ": S=%.6f W=%.6f N=%.6f E=%.6f" % bbox)
        if image is not None:
            self._show_capture(bbox, image, place)
        else:
            self.extent_var.set("fetching reference image ...")
            threading.Thread(target=self._grab_capture_image, args=(bbox,),
                             daemon=True).start()

    def _show_history(self):
        HistoryDialog(self)

    def _show_region(self):
        RegionDialog(self)

    def use_region(self, region):
        """Make a country / region the plate: its own outline, its own relief,
        and none of the street-level layers that make no sense at that size."""
        ring = list(region["rings"][0])
        bbox = tuple(region["bbox"])
        name = str(region["name"]).split(",")[0].strip()
        for key, w in self.layer_widgets.items():
            w["enabled"].set(False)
        for z, v in self.zone_vars.items():
            v["enabled"].set(False)
        self.terrain_on.set(True)
        self.detail_osm.clear()
        self._refresh_detail_list()
        self.vars["aspect_ratio"].set("free")
        # a country is rarely square: fit it inside the bed rather than letting
        # the width alone decide and making it half a metre long
        self.size_fit.set(True)
        if self._num_or0("target_depth_mm") <= 0:
            self.vars["target_depth_mm"].set(
                self.vars["target_width_mm"].get() or "180")
        if self._num_or0("terrain_relief_mm") <= 0:
            self.vars["terrain_relief_mm"].set("12")
        if self._num_or0("base_min_mm") <= 0:
            self.vars["base_min_mm"].set("2")
        w_km, h_km = region["size_km"]
        # a bigger country wants a finer grid to keep its mountains
        self.vars["terrain_samples"].set(
            str(240 if max(w_km, h_km) > 300 else 160))
        self._set_area_polygon(ring)
        self.apply_bbox(*bbox)
        self.capture_bbox = bbox
        self.capture_place = name
        self._log("SUCCESS", f"{name}: {len(ring)}-point outline, "
                             f"%.0f x %.0f km" % (w_km, h_km))
        self._log("INFO", "  map layers switched off; terrain relief on. "
                          "Set the relief height in Advanced > Model.")
        self.extent_var.set("%s   %.0f x %.0f km   outline of %d points"
                            % (name, w_km, h_km, len(ring)))
        self.auto_rename()
        threading.Thread(target=self._grab_capture_image, args=(bbox,),
                         daemon=True).start()

    def print_palette(self) -> dict:
        """The backdrop palette that matches what actually comes off the
        printer: a layer that was never given its own colour is printed in the
        plate's filament, so that is the colour the paper should use too."""
        base = tuple(gen.BASE_COLOR)
        layer = {}
        for key, st in self.layer_state.items():
            layer[key] = (tuple(st["color"])
                          if self.layer_widgets[key]["custom"].get() else base)
        zone = {}
        for z, st in self.zone_state.items():
            zone[z] = (tuple(st["color"])
                       if self.zone_vars[z]["custom"].get() else base)
        return {
            "paper": base,
            "urban": zone.get("urban", base),
            "parks": zone.get("parks", base),
            "forest": zone.get("forest", base),
            "farmland": zone.get("farmland", base),
            "water": zone.get("water", base),
            "roads": layer.get("roads", base),
            "road_edge": shade(layer.get("roads", base), 0.88),
            "rail": layer.get("rail", base),
            "buildings": layer.get("buildings", base),
            "building_edge": shade(layer.get("buildings", base), 0.86),
        }

    def _open_poster(self):
        if not HAVE_PIL:
            messagebox.showinfo("Backdrop", "The backdrop needs Pillow "
                                            "(pip install pillow).", parent=self)
            return
        PosterDialog(self)

    def _apply_map_pick(self, data):
        t = data.get("type")
        if t == "capture":
            b = data.get("bbox") or []
            if len(b) == 4:
                self.open_bbox(tuple(float(x) for x in b), note="area captured")
        elif t == "area_poly":
            pts = [(float(la), float(lo)) for la, lo in data.get("latlngs", [])]
            bb = data.get("bbox") or []
            if len(pts) >= 3 and len(bb) == 4:
                self._log("INFO", f"free-form area ({len(pts)} pts); aspect ignored.")
                self.open_bbox(tuple(float(x) for x in bb), polygon=pts,
                               note="area captured")
        elif t == "building":
            threading.Thread(target=self._reverse_building,
                             args=(data.get("lat"), data.get("lon")),
                             daemon=True).start()
        elif t == "_addosm":
            self._detail_add(data["osm"], data.get("name", ""))

    def _grab_capture_image(self, bbox):
        img = None
        try:
            img = fetch_map_image(bbox)
        except Exception as exc:  # noqa: BLE001
            self._log("WARNING", f"reference image unavailable: {exc}")
        place = reverse_place((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
        self.capture_q.put((bbox, img, place))

    def _show_capture(self, bbox, img, place=""):
        # reference images arrive on a background thread; if the user has moved
        # to another area since, this one is stale and must not drag them back
        if self.capture_bbox is not None and not _same_bbox(self.capture_bbox, bbox):
            self._log("INFO", "ignoring a reference image for an area you left")
            return
        self.capture_bbox = bbox
        if place:
            self.capture_place = place
            self._log("INFO", f"area is in {place}")
        self.auto_rename()
        try:
            remember_capture(bbox, self._area_polygon, self.capture_place, img)
        except Exception as exc:  # noqa: BLE001
            self._log("WARNING", f"could not add this area to the history: {exc}")
        w_m, h_m = bbox_size_m(bbox)
        self.extent_var.set(
            "%.0f x %.0f m     S %.5f  W %.5f  N %.5f  E %.5f"
            % (w_m, h_m, bbox[0], bbox[1], bbox[2], bbox[3]))
        if img is not None and HAVE_PIL:
            self.capture_image = img
            th = img.copy()
            th.thumbnail((320, 158))
            self._thumb_tk = ImageTk.PhotoImage(th)
            self.thumb.configure(image=self._thumb_tk, text="", height=th.height)
            self._log("SUCCESS", "reference image captured (click it to enlarge)")
        else:
            self.thumb.configure(image="", text="\narea captured\n(no reference image)\n")

    def _show_capture_full(self):
        if self.capture_image is None or not HAVE_PIL:
            return
        win = tk.Toplevel(self)
        win.title("Captured area")
        win.configure(background=BG)
        img = self.capture_image.copy()
        img.thumbnail((1150, 820))
        win._img = ImageTk.PhotoImage(img)
        tk.Label(win, image=win._img, background=BG).pack(padx=8, pady=8)
        ttk.Label(win, style="Muted.TLabel", text=self.extent_var.get()).pack(pady=(0, 8))

    def _set_area_polygon(self, pts):
        self._area_polygon = list(pts)

    def _clear_area_polygon(self):
        self._set_area_polygon([])
        self._log("INFO", "free-form shape cleared -- back to the captured rectangle.")

    def _path_hint(self):
        if not self.path_on.get():
            self.path_hint.configure(text="")
            return
        n = len(self.detail_osm)
        if n < 2:
            self.path_hint.configure(
                text="Pick at least two landmarks -- the path runs between them.")
            return
        style = self.vars["path_style"].get()
        how = {"roads": "Follows the streets between",
               "curved": "Curved route through",
               "straight": "Straight route through"}.get(style, "Route through")
        extra = ("  Needs the Roads layer on -- it is what the route follows."
                 if style == "roads"
                 and not self.layer_widgets["roads"]["enabled"].get() else "")
        self.path_hint.configure(
            text="%s %d landmarks, in the order above. Use up / dn to change it; "
                 "it exports as its own coloured part.%s" % (how, n, extra))

    def _detail_level_hint(self):
        lvl = gen.detail_level_of(self.vars["detail_level"].get())
        self.detail_hint.configure(text=gen.DETAIL_LEVELS[lvl - 1][2])

    def _model_hint(self):
        self.model_hint.configure(text=MODEL_FIT_HINTS.get(
            self.vars["model_fit"].get(), ""))

    def _browse_model(self):
        p = filedialog.askopenfilename(
            parent=self, title="Model for the landmark",
            filetypes=[("Meshes", "*.stl *.obj *.3mf *.ply"), ("All files", "*.*")],
            initialdir=SCRIPT_DIR)
        if p:
            self.model_var.set(p)
            if gen.detail_level_of(self.vars["detail_level"].get()) < 5:
                self.vars["detail_level"].set(gen.DETAIL_LEVEL_LABELS[4])
                self._log("INFO", "detail level raised to 5 so the model is used")

    def _browse_out(self):
        p = filedialog.asksaveasfilename(
            parent=self, title="Output base / STL", defaultextension=".stl",
            filetypes=[("STL", "*.stl"), ("All files", "*.*")],
            initialfile=os.path.basename(self.vars["output_stl"].get()),
            initialdir=os.path.dirname(self.vars["output_stl"].get()) or SCRIPT_DIR)
        if p:
            self.vars["output_stl"].set(p)

    # ------------------------------------------------------- detailed picks
    _ODD = ('["man_made"~"^(tower|bridge|pier|gasometer|storage_tank|'
            'water_tower|chimney|works)$"]')

    def _reverse_building(self, lat, lon):
        r = 160
        q = (f"[out:json][timeout:25];("
             f'way(around:{r},{lat},{lon})["building"];'
             f'relation(around:{r},{lat},{lon})["building"];'
             f'way(around:{r},{lat},{lon})["building:part"];'
             f'relation(around:{r},{lat},{lon})["building:part"];'
             f'way(around:{r},{lat},{lon}){self._ODD};'
             f");out geom;")
        try:
            data = gen._overpass_request(q, gen._module_default_config())
        except Exception as exc:  # noqa: BLE001
            self._log("ERROR", f"building lookup failed: {exc}")
            return
        pt = Point(lon, lat)
        cands = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            poly = _element_polygon(el)
            if poly is None or poly.is_empty:
                continue
            rank = 0 if "building" in tags else (1 if "man_made" in tags else 2)
            cands.append((rank, poly.area, f"{el['type']}/{el['id']}",
                          tags.get("name") or tags.get("ref") or "", poly))
        if not cands:
            self._log("WARNING", "nothing building-like within 160 m of that click.")
            return
        inside = [c for c in cands if c[4].contains(pt) or c[4].distance(pt) < 1e-9]
        if inside:
            inside.sort(key=lambda c: (c[0], c[1]))
            pick = inside[0]
        else:
            nearest = min(cands, key=lambda c: c[4].distance(pt))
            if nearest[4].distance(pt) > 0.00035:
                self._log("WARNING", "no building under that click -- zoom in and "
                                     "click the roof.")
                return
            pick = nearest
        self.map_pick_q.put({"type": "_addosm", "osm": pick[2], "name": pick[3]})

    # ------------------------------------------------------- file naming
    def suggested_name(self) -> str:
        """city_firstlandmark_date, from the captured town and the first
        landmark in the Detail list. Missing pieces are simply left out."""
        landmark = ""
        for osm, nm in self.detail_osm.items():
            landmark = nm            # an unnamed pick adds nothing but noise
            break
        return build_name(self.capture_place, landmark)

    def auto_rename(self, force=False):
        """Point the output at <folder>/<suggested name>.stl. Runs itself after
        every capture / landmark change while 'auto' is ticked; the Rename
        button calls it with force."""
        if not (force or self.auto_name.get()):
            return
        name = self.suggested_name()
        if not name:
            return
        new = os.path.join(self.output_root(), name, name + ".stl")             if self.own_folder.get() else             os.path.join(self.output_root(), name + ".stl")
        cur = self.vars["output_stl"].get().strip()
        if os.path.abspath(new) == os.path.abspath(cur):
            return
        self.vars["output_stl"].set(new)
        self._log("INFO", "output set to " + (os.path.join(name, name + ".stl")
                                              if self.own_folder.get()
                                              else name + ".stl"))

    def output_root(self) -> str:
        """The folder the per-model folders are created in. Stays put when a
        model folder is already in the path, so renaming never nests."""
        cur = self.vars["output_stl"].get().strip()
        folder = os.path.dirname(cur) or SCRIPT_DIR
        stem = os.path.splitext(os.path.basename(cur))[0]
        if os.path.basename(folder) == stem and stem:
            return os.path.dirname(folder) or SCRIPT_DIR
        return folder

    def _rename_now(self):
        self.auto_rename(force=True)
        if not self.capture_place:
            self._log("WARNING", "no town known yet -- capture an area first and "
                                 "the name will fill in.")

    # ---------------------------------------------------------- size presets
    def _refresh_presets(self, select=None):
        names = list(self.presets)
        self.preset_box.configure(values=names)
        if select:
            self.vars["size_preset"].set(select)

    def _apply_preset(self, *_a):
        p = self.presets.get(self.vars["size_preset"].get())
        if not p:
            return
        self.vars["target_width_mm"].set(_mm(p["width"]))
        self.vars["target_depth_mm"].set(_mm(p["depth"]))
        self.vars["target_height_mm"].set(_mm(p["height"]))
        self.vars["aspect_ratio"].set(p.get("aspect") or "free")
        self._size_summary()

    def _preset_save(self):
        name = simpledialog.askstring(
            "Save size preset", "Name this size:", parent=self,
            initialvalue=self._auto_preset_name())
        if not name or not name.strip():
            return
        name = name.strip()
        if name in BUILTIN_PRESETS:
            messagebox.showinfo("Save size preset",
                                f"{name!r} is a built-in -- pick another name.",
                                parent=self)
            return
        self.presets[name] = {
            "width": self._num_or0("target_width_mm"),
            "depth": self._num_or0("target_depth_mm"),
            "height": self._num_or0("target_height_mm"),
            "aspect": self.vars["aspect_ratio"].get().strip() or "free"}
        try:
            save_presets(self.presets)
        except OSError as exc:
            messagebox.showerror("Save size preset", str(exc), parent=self)
            return
        self._refresh_presets(select=name)
        self._log("SUCCESS", f"size preset {name!r} saved to "
                             f"{os.path.basename(PRESET_FILE)}")

    def _preset_delete(self):
        name = self.vars["size_preset"].get()
        if name in BUILTIN_PRESETS:
            messagebox.showinfo("Delete size preset",
                                "Built-in presets stay put.", parent=self)
            return
        if name not in self.presets:
            return
        if not messagebox.askyesno("Delete size preset",
                                   f"Delete {name!r}?", parent=self):
            return
        self.presets.pop(name, None)
        try:
            save_presets(self.presets)
        except OSError as exc:
            messagebox.showerror("Delete size preset", str(exc), parent=self)
            return
        self._refresh_presets(select="Free (no resize)")
        self._log("INFO", f"size preset {name!r} deleted")

    def _auto_preset_name(self):
        w, d, h = (self._num_or0("target_width_mm"),
                   self._num_or0("target_depth_mm"),
                   self._num_or0("target_height_mm"))
        bits = [f"{_mm(w)} x {_mm(d)}" if d else f"{_mm(w)} mm wide"]
        if h:
            bits.append(f"x {_mm(h)}")
        return " ".join(bits) + (" mm" if d else "")

    def _num_or0(self, key):
        try:
            return max(float(self.vars[key].get().strip() or 0), 0.0)
        except ValueError:
            return 0.0

    def _size_summary(self, *_a):
        w, d = self._num_or0("target_width_mm"), self._num_or0("target_depth_mm")
        h = self._num_or0("target_height_mm")
        if not (w or d or h):
            txt = "No resize: 1 unit = 1 real metre. Set at least a width."
        else:
            x = f"{_mm(w)} mm" if w else "auto"
            y = (f"{_mm(d)} mm" if d else
                 f"auto (aspect {self.vars['aspect_ratio'].get()})")
            z = f"{_mm(h)} mm incl. base" if h else "from the height exaggeration"
            txt = f"X {x}   Y {y}   Z {z}"
            if w and d and self.size_fit.get():
                txt = (f"Fits inside {_mm(w)} x {_mm(d)} mm keeping its own "
                       f"shape -- one side comes out shorter.   Z {z}")
            elif w and d:
                txt += "\nThe area is cropped to that ratio, so nothing is stretched."
        self.size_hint.configure(text=txt)

    def _refresh_detail_list(self):
        keep = list(self.detail_list.curselection())
        self.detail_list.delete(0, "end")
        for i, (osm, label) in enumerate(self.detail_osm.items(), start=1):
            name = f"{label}   [{osm}]" if label else osm
            # numbered, because a path visits them in exactly this order
            self.detail_list.insert("end", f"{i}.  {name}")
        for i in keep:
            if i < self.detail_list.size():
                self.detail_list.selection_set(i)
        self.detail_swatch.configure(background="#%02x%02x%02x" % self.detail_color)

    def _detail_add(self, osm_id, name=""):
        if osm_id in self.detail_osm:
            self._log("INFO", f"{osm_id} is already in the detail list")
            return
        self.detail_osm[osm_id] = name
        self._refresh_detail_list()
        self.auto_rename()
        self._path_hint()
        self._log("SUCCESS", f"detail += {osm_id}" + (f"  ({name})" if name else ""))

    def _detail_remove(self):
        sel = list(self.detail_list.curselection())
        keys = list(self.detail_osm)
        for i in reversed(sel):
            if 0 <= i < len(keys):
                self.detail_osm.pop(keys[i], None)
        self._refresh_detail_list()
        self.auto_rename()
        self._path_hint()

    def _detail_clear(self):
        self.detail_osm.clear()
        self._refresh_detail_list()
        self.auto_rename()
        self._path_hint()

    def _detail_move(self, step):
        """Shuffle the selected landmark up or down. This is the order the path
        visits them in."""
        sel = list(self.detail_list.curselection())
        if len(sel) != 1:
            return
        i = sel[0]
        keys = list(self.detail_osm)
        j = i + step
        if not (0 <= i < len(keys) and 0 <= j < len(keys)):
            return
        keys[i], keys[j] = keys[j], keys[i]
        self.detail_osm = {k: self.detail_osm[k] for k in keys}
        self._refresh_detail_list()
        self.detail_list.selection_clear(0, "end")
        self.detail_list.selection_set(j)
        self.auto_rename()

    def _path_pick_color(self):
        rgb, _hex = colorchooser.askcolor(
            color="#%02x%02x%02x" % self.path_color, parent=self)
        if rgb:
            self.path_color = tuple(int(c) for c in rgb)
            self.path_swatch.configure(background="#%02x%02x%02x" % self.path_color)

    def _detail_pick_color(self):
        rgb, _hex = colorchooser.askcolor(
            color="#%02x%02x%02x" % self.detail_color, parent=self)
        if rgb:
            self.detail_color = tuple(int(c) for c in rgb)
            self._refresh_detail_list()

    def _layer_pick_color(self, key, swatch=None):
        cur = self.layer_state[key]["color"]
        rgb, _hex = colorchooser.askcolor(color="#%02x%02x%02x" % tuple(cur),
                                          parent=self)
        if rgb:
            self.layer_state[key]["color"] = tuple(int(c) for c in rgb)
            self.layer_widgets[key]["custom"].set(True)
            if swatch is not None:
                swatch.configure(background="#%02x%02x%02x" % tuple(rgb))

    # ------------------------------------------------------------- zones
    def _zone_pick_color(self, zone, swatch=None):
        cur = self.zone_state[zone]["color"]
        rgb, _hex = colorchooser.askcolor(color="#%02x%02x%02x" % tuple(cur),
                                          parent=self)
        if rgb:
            self.zone_state[zone]["color"] = tuple(int(c) for c in rgb)
            self.zone_vars[zone]["custom"].set(True)
            if swatch is not None:
                swatch.configure(background="#%02x%02x%02x" % tuple(rgb))

    # ---------------------------------------------------------------- config
    def _num(self, key, cast=float):
        raw = self.vars[key].get().strip()
        try:
            return cast(raw)
        except ValueError:
            raise ValueError(f"'{key}' must be a number (got {raw!r})")

    def _build_config(self) -> gen.PipelineConfig:
        def fl(v, d):
            try:
                return float(v.get())
            except ValueError:
                return d

        layers = {
            key: gen.LayerStyle(
                enabled=bool(s["enabled"].get()),
                height_m=fl(s["height"], 1.0),
                mode=s["mode"].get(),
                width_m=fl(s["width"], 6.0),
                color=(tuple(self.layer_state[key]["color"])
                       if s["custom"].get() else None))
            for key, s in self.layer_widgets.items()
        }
        zdh = fl(self.vars["zone_default_h"], 0.7)
        zdm = self.zone_default_mode.get()
        for zone, st in self.zone_state.items():
            zv = self.zone_vars[zone]
            custom = bool(zv["custom"].get())
            layers[zone] = gen.LayerStyle(
                enabled=bool(zv["enabled"].get()),
                height_m=(fl(zv["height"], zdh) if custom else zdh),
                mode=("raised" if custom else zdm),
                color=(tuple(st["color"]) if custom else None))

        mirrors = [x for x in self.mirror_lines if x.strip()]
        if not mirrors:
            raise ValueError("at least one Overpass mirror URL is required "
                             "(Advanced > Network)")

        tw = self._num("target_width_mm")
        att = int(self._num("max_attempts_per_mirror"))
        to = self._num("request_timeout_s")
        if tw < 0:
            raise ValueError("print width must be >= 0")
        if att < 1:
            raise ValueError("attempts per mirror must be >= 1")
        if to <= 0:
            raise ValueError("request timeout must be > 0")
        if not (self.exp_stl.get() or self.exp_3mf.get() or self.exp_split.get()):
            raise ValueError("tick at least one output format")

        path_kw = dict(
            path_enabled=bool(self.path_on.get()),
            path_osm_ids=tuple(self.detail_osm),
            path_width_m=max(fl(self.vars["path_width_m"], 8.0), 0.1),
            path_height_mm=max(fl(self.vars["path_height_mm"], 1.2), 0.0),
            path_color=tuple(self.path_color),
            path_closed=bool(self.path_closed.get()),
            path_style=self.vars["path_style"].get().strip() or "straight")

        detail_extra = fl(self.vars["detail_extra_m"], 0.0)
        if self.detail_osm:
            highlights = (gen.Highlight(
                name="detail", color=tuple(self.detail_color),
                osm_ids=frozenset(self.detail_osm),
                height_mode="delta", height_m=detail_extra, raise_mm=0.0,
                model_path=self.model_var.get().strip(),
                model_fit=self.vars["model_fit"].get().strip() or "height",
                model_scale=max(fl(self.vars["model_scale_pct"], 100.0), 1.0) / 100.0,
                model_rotate_deg=fl(self.vars["model_rotate_deg"], 0.0),
                model_upright=("auto" if self.vars["model_upright"].get().lower()
                               .startswith("a")
                               else self.vars["model_upright"].get()[:1].lower())),)
        else:
            highlights = ()

        return gen.PipelineConfig(
            bbox=(self._num("south"), self._num("west"),
                  self._num("north"), self._num("east")),
            base_thickness_m=self._num("base_thickness_m"),
            base_min_mm=max(self._num("base_min_mm"), 0.0),
            min_footprint_area_m2=self._num("min_footprint_area_m2"),
            height_scale=self._num("height_scale"),
            default_level_height_m=self._num("default_level_height_m"),
            layers=layers,
            target_width_mm=tw,
            target_depth_mm=max(self._num("target_depth_mm"), 0.0),
            size_mode="fit" if self.size_fit.get() else "fill",
            target_height_mm=max(self._num("target_height_mm"), 0.0),
            aspect_ratio=self.vars["aspect_ratio"].get().strip() or "free",
            vertical_exaggeration=self._num("vertical_exaggeration"),
            simplify_tolerance_m=self._num("simplify_tolerance_m"),
            area_polygon=tuple(tuple(p) for p in self._area_polygon),
            terrain_enabled=bool(self.terrain_on.get()),
            terrain_exaggeration=fl(self.vars["terrain_exaggeration"], 1.5),
            terrain_relief_mm=max(fl(self.vars["terrain_relief_mm"], 0.0), 0.0),
            terrain_sea_level_m=fl(self.vars["terrain_sea_level_m"], 0.0),
            terrain_samples=int(max(fl(self.vars["terrain_samples"], 96), 8)),
            use_osm_heights=bool(self.use_osm_heights.get()),
            uniform_building_height_m=fl(self.vars["uniform_building_height_m"], 12.0),
            detail_level=gen.detail_level_of(self.vars["detail_level"].get()),
            highlights=highlights,
            **path_kw,
            output_stl=self.vars["output_stl"].get().strip() or gen.OUTPUT_STL,
            export_stl=bool(self.exp_stl.get()),
            export_3mf=bool(self.exp_3mf.get()),
            export_split=bool(self.exp_split.get()),
            overpass_urls=tuple(mirrors),
            request_timeout_s=to,
            max_attempts_per_mirror=att,
            overpass_query_timeout_s=int(self._num("overpass_query_timeout_s")),
        )

    # --------------------------------------------------------------- generate
    def _on_generate(self):
        if self._worker and self._worker.is_alive():
            return
        self.auto_rename()
        if self.capture_bbox is None:
            if not messagebox.askyesno(
                    "No area captured",
                    "You have not captured an area yet -- the last known "
                    "coordinates will be used.\n\nGenerate anyway?", parent=self):
                return
        try:
            cfg = self._build_config()
        except ValueError as exc:
            messagebox.showerror("Check your settings", str(exc), parent=self)
            self._log("ERROR", f"Input error: {exc}")
            return
        self.gen_btn.configure(state="disabled")
        self._set_status("Running ...", "busy")
        self._log("INFO", "-" * 58)
        self._log("INFO", "Generate requested.")
        self._worker = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self._worker.start()

    def _cached_fetch(self, bbox, cfg=None):
        key = tuple(round(float(v), 6) for v in bbox)
        cached = getattr(self, "_osm_cache", None)
        if cached and cached[0] == key:
            gen.logger.info("reusing the map data already downloaded for this area")
            return cached[1]
        data = gen.fetch_buildings(bbox, cfg)
        self._osm_cache = (key, data)
        return data

    def _run(self, cfg):
        try:
            result = gen.run_pipeline(cfg, fetch=self._cached_fetch)
        except gen.OverpassError as exc:
            gen.logger.error("Overpass fetch failed after all retries:\n%s", exc)
            self.result_q.put((False, None))
        except (ValueError, OSError) as exc:
            gen.logger.error("Generation failed: %s", exc)
            self.result_q.put((False, None))
        except Exception:
            gen.logger.error("Generation failed with an unexpected error:\n%s",
                             traceback.format_exc())
            self.result_q.put((False, None))
        else:
            self.result_q.put((True, result))

    def _set_status(self, text, tone="idle"):
        self.status.set(text)
        bg, fg = {"idle": (PINK_SOFT, PINK_DEEP),
                  "busy": (PLUM, "#FFFFFF"),
                  "ok": ("#DFF3E7", "#2F8F5B"),
                  "err": ("#FFE1E8", "#C0446F")}.get(tone, (PINK_SOFT, PINK_DEEP))
        try:
            self.status_chip.configure(background=bg, foreground=fg)
        except tk.TclError:
            pass

    def _finish(self, ok, result):
        self.gen_btn.configure(state="normal")
        if ok and result is not None:
            self._set_status("Done -- preview opening", "ok")
            self._log("SUCCESS", f"Done -- saved {result.output_path}")
            for p in result.output_paths:
                self._log("SUCCESS", f"   wrote {p}")
            self._log("INFO", "model size: %.1f x %.1f x %.1f mm" % result.size_mm)
            messagebox.showinfo(
                "Generation complete",
                "Files written:\n" + "\n".join("  " + p for p in result.output_paths)
                + "\n\nModel size: %.1f x %.1f x %.1f mm" % result.size_mm
                + f"\nBuildings: {result.building_count} of {result.raw_building_count}"
                + f"\nWatertight: {result.is_watertight}",
                parent=self)
            self._last_result = result
            self._collect_run_files(result)
            if HAVE_PIL:
                threading.Thread(target=self._prepare_preview, args=(result,),
                                 daemon=True).start()
        else:
            self._set_status("Failed -- see the log", "err")
            self._log("ERROR", "GENERATION FAILED. Details above and in "
                      + os.path.join(SCRIPT_DIR, gen.LOG_FILENAME))

    def _open_output_folder(self):
        folder = os.path.dirname(self.vars["output_stl"].get().strip()) or SCRIPT_DIR
        if not os.path.isdir(folder):
            messagebox.showinfo("Open folder",
                                folder + "\n\nhas not been created yet -- it "
                                "appears when you generate.", parent=self)
            return
        try:
            os.startfile(folder)                       # noqa: S606  (Windows)
        except Exception as exc:                       # noqa: BLE001
            messagebox.showerror("Open folder", str(exc), parent=self)

    def _collect_run_files(self, result):
        """Drop everything that belongs to this model next to its meshes: the
        reference image, a readable summary and the run log."""
        folder = os.path.dirname(os.path.abspath(result.output_path))
        stem = os.path.splitext(os.path.basename(result.output_path))[0]
        wrote = []
        try:
            if self.capture_image is not None and HAVE_PIL:
                p = os.path.join(folder, stem + "_reference.png")
                self.capture_image.save(p)
                wrote.append(p)
        except Exception as exc:  # noqa: BLE001
            self._log("WARNING", f"could not save the reference image: {exc}")
        try:
            p = os.path.join(folder, stem + "_info.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(self._run_summary(result))
            wrote.append(p)
            p = os.path.join(folder, stem + ".log")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(self.log_text.get("1.0", "end"))
            wrote.append(p)
        except OSError as exc:
            self._log("WARNING", f"could not write the run notes: {exc}")
        for p in wrote:
            self._log("INFO", f"   wrote {p}")

    def _run_summary(self, result) -> str:
        landmarks = ", ".join(f"{nm or osm} [{osm}]"
                              for osm, nm in self.detail_osm.items()) or "(none)"
        b = self.capture_bbox or (0, 0, 0, 0)
        return os.linesep.join([
            f"City Map Generator -- {os.path.basename(result.output_path)}",
            time.strftime("generated %Y-%m-%d %H:%M"),
            "",
            f"place        : {self.capture_place or '(unknown)'}",
            "area S W N E : %.6f  %.6f  %.6f  %.6f" % tuple(b),
            f"extent       : {self.extent_var.get()}",
            f"landmarks    : {landmarks}",
            f"detail level : {self.vars['detail_level'].get()}",
            f"own model    : {self.model_var.get().strip() or '(none)'}",
            "size mm      : %.2f x %.2f x %.2f" % result.size_mm,
            f"buildings    : {result.building_count} of {result.raw_building_count}",
            f"watertight   : {result.is_watertight}",
            f"layer counts : {result.layer_counts}",
            "",
            "files:",
            *[f"  {os.path.basename(p)}" for p in result.output_paths],
            "",
        ])

    # ---------------------------------------------------------------- preview
    def _colmap(self):
        cm = {"detail": tuple(self.detail_color),
              "path": tuple(self.path_color)}
        for key, st in self.layer_state.items():
            cm[key] = tuple(st["color"])
        for z, st in self.zone_state.items():
            cm[z] = tuple(st["color"])
        return cm

    def _prepare_preview(self, result):
        try:
            parts = load_preview_parts(list(result.output_paths),
                                       result.output_path, self._colmap())
        except Exception as exc:  # noqa: BLE001
            self._log("WARNING", f"preview could not load the meshes: {exc}")
            return
        if not parts:
            self._log("WARNING", "preview: nothing loadable was exported.")
            return
        self.preview_q.put(parts)

    def _parts_from_disk(self):
        """Fallback so Preview works even before/without a fresh generate."""
        out = self.vars["output_stl"].get().strip()
        if not out:
            return []
        stem = os.path.splitext(out)[0]
        cands = sorted(set(_glob.glob(stem + "_*.stl")))
        for extra in (stem + ".stl", stem + ".3mf"):
            if os.path.isfile(extra):
                cands.append(extra)
        if not cands:
            return []
        try:
            return load_preview_parts(cands, stem + ".stl", self._colmap())
        except Exception as exc:  # noqa: BLE001
            self._log("WARNING", f"preview load failed: {exc}")
            return []

    def _show_preview(self):
        if not HAVE_PIL:
            messagebox.showinfo("Preview", "Preview needs Pillow (pip install pillow).",
                                parent=self)
            return
        if not self._preview_parts:
            self._preview_parts = self._parts_from_disk()
        if not self._preview_parts:
            messagebox.showinfo("Preview", "Nothing to preview yet -- press GENERATE "
                                           "first.", parent=self)
            return
        self._set_status("Done", "ok")
        old = getattr(self, "_preview_win", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._preview_win = PreviewWindow(self, self._preview_parts)

    # ---------------------------------------------------------------- logs
    def _save_log_as(self):
        p = filedialog.asksaveasfilename(
            parent=self, title="Save log as", defaultextension=".log",
            filetypes=[("Log", "*.log"), ("Text", "*.txt"), ("All files", "*.*")],
            initialfile=time.strftime("city_map_gui_%Y%m%d-%H%M%S.log"),
            initialdir=SCRIPT_DIR)
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(self.log_text.get("1.0", "end"))
            self._log("INFO", f"log saved to {p}")
        except OSError as exc:
            messagebox.showerror("Could not save log", str(exc), parent=self)

    def _autosave_log(self):
        p = os.path.join(SCRIPT_DIR, time.strftime("city_map_gui_%Y%m%d-%H%M%S.log"))
        try:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(self.log_text.get("1.0", "end"))
            return p
        except OSError:
            return None

    def on_close(self):
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno("Still running",
                                       "A generation is still running. Quit anyway?\n"
                                       "(The log is saved first.)", parent=self):
                return
        if self._picker is not None:
            self._picker.stop()
        saved = self._autosave_log()
        if saved:
            try:
                print(f"Log auto-saved to {saved}")
            except Exception:  # noqa: BLE001
                pass
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
