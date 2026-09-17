"""The embedded Leaflet map: draw / adjust the area, click buildings."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from ..log import logger

ASSETS = Path(__file__).parent / "assets"
MAX_FOOTPRINTS = 12_000


class _Bridge(QObject):
    ready = Signal()
    areaChanged = Signal(object)          # dict | None
    buildingClicked = Signal(str)
    viewChanged = Signal(dict)

    @Slot()
    def onReady(self):
        self.ready.emit()

    @Slot(str)
    def onAreaChanged(self, payload: str):
        try:
            self.areaChanged.emit(json.loads(payload))
        except ValueError:
            pass

    @Slot(str)
    def onBuildingClicked(self, osm_id: str):
        self.buildingClicked.emit(osm_id)

    @Slot(str)
    def onViewChanged(self, payload: str):
        try:
            self.viewChanged.emit(json.loads(payload))
        except ValueError:
            pass


class _Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            logger.debug("map js: %s (line %s)", message, line)


def _qwebchannel_js() -> str:
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        return ""
    try:
        return bytes(f.readAll()).decode("utf-8")
    finally:
        f.close()


class MapWidget(QWebEngineView):
    ready = Signal()
    areaChanged = Signal(object)
    buildingClicked = Signal(str)
    viewChanged = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ready = False
        self._pending: list = []
        self.bridge = _Bridge(self)
        self.bridge.ready.connect(self._on_ready)
        self.bridge.areaChanged.connect(self.areaChanged)
        self.bridge.buildingClicked.connect(self.buildingClicked)
        self.bridge.viewChanged.connect(self.viewChanged)

        page = _Page(self)
        self.setPage(page)
        page.setBackgroundColor("#14171c")
        st = page.settings()
        st.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.channel = QWebChannel(page)
        self.channel.registerObject("bridge", self.bridge)
        page.setWebChannel(self.channel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        # The VTK preview is a native OpenGL child window. With one of those in
        # the same top-level window, a (non-native) web view composes to black;
        # giving the web view its own native window fixes that.
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.reload_map()

    def reload_map(self):
        self._ready = False
        html = (ASSETS / "map.html").read_text("utf-8").replace(
            "/*QWEBCHANNEL_JS*/", _qwebchannel_js())
        # an https base URL so tile servers get a normal Referer header
        self.page().setHtml(html, QUrl("https://citymodel.local/"))

    def _on_ready(self):
        self._ready = True
        for js in self._pending:
            self.page().runJavaScript(js)
        self._pending.clear()
        self.ready.emit()

    def _js(self, code: str):
        if self._ready:
            self.page().runJavaScript(code)
        else:
            self._pending.append(code)

    # ------------------------------------------------------------ Python -> JS
    def set_area(self, area_dict, fit: bool = False):
        self._js(f"setArea({json.dumps(area_dict)}, {str(bool(fit)).lower()});")

    def area_from_view(self, fraction: float = 0.7):
        self._js(f"areaAroundView({fraction});")

    def set_footprints(self, geojson: dict | None):
        self._js(f"setFootprints({json.dumps(geojson, separators=(',', ':'))});")

    def set_selection(self, ids):
        self._js(f"setSelection({json.dumps(list(ids))});")

    def fly_to(self, lat, lon, zoom=16, bbox=None):
        self._js(f"flyTo({lat}, {lon}, {zoom}, {json.dumps(list(bbox) if bbox else None)});")

    def restore_view(self, view: dict):
        self._js(f"restoreView({json.dumps(view or {})});")

    def focus_building(self, osm_id: str):
        self._js(f"focusBuilding({json.dumps(osm_id)});")


def footprints_geojson(features: dict, bbox=None) -> dict:
    """Buildings -> a compact GeoJSON FeatureCollection for the map. building:part
    outlines are left out: selection works on whole buildings."""
    from ..mesh.heights import tagged_height  # noqa: PLC0415
    out = []
    for f in features.get("buildings", []):
        ring = f.coords
        if bbox is not None:
            s, w, n, e = bbox
            if not any(s <= la <= n and w <= lo <= e for la, lo in ring):
                continue
        coords = [[[round(lo, 6), round(la, 6)] for la, lo in ring]]
        coords += [[[round(lo, 6), round(la, 6)] for la, lo in h] for h in f.holes]
        h = tagged_height(f.tags)
        kind = str(f.tags.get("building", "yes")).replace("_", " ")
        info = (f"{h.metres:.0f} m ({'height tag' if h.source == 'tag' else 'from levels'})"
                if h else "height estimated")
        out.append({"type": "Feature",
                    "properties": {"id": f.osm_id, "name": f.name,
                                   "info": f"{kind if kind != 'yes' else 'building'} &middot; {info}"},
                    "geometry": {"type": "Polygon", "coordinates": coords}})
        if len(out) >= MAX_FOOTPRINTS:
            logger.warning("map: showing the first %d building outlines only", MAX_FOOTPRINTS)
            break
    return {"type": "FeatureCollection", "features": out}
