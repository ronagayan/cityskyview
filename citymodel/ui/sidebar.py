"""The settings sidebar. Every widget is bound to a field of ModelSettings."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from ..data import overture
from ..settings import TERRAIN_QUALITY, ModelSettings

ELEVATION_LABELS = (
    ("auto", "Automatic (best for the location)"),
    ("usgs3dep", "USGS 3DEP  ·  1-10 m, USA"),
    ("glo30", "Copernicus GLO-30  ·  30 m, global"),
    ("terrarium", "AWS Terrain Tiles  ·  global"),
    ("opentopography", "OpenTopography  ·  needs a free key"),
)
QUALITY_LABELS = {"draft": "Draft  (1.2 mm grid)", "normal": "Normal  (0.7 mm grid)",
                  "fine": "Fine  (0.45 mm grid)", "ultra": "Ultra  (0.3 mm grid)"}
COLOR_LABELS = (("terrain", "Terrain"), ("buildings", "Buildings"), ("selected", "Selected"),
                ("water", "Water"), ("roads", "Roads"), ("green", "Green"))


class _Spin(QDoubleSpinBox):
    """The wheel scrolls the sidebar, not the number under the cursor."""

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _Combo(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


class Sidebar(QScrollArea):
    geometryChanged = Signal()            # a setting that changes the meshes
    lookChanged = Signal()                # colours / preview effects only
    searchRequested = Signal(str)
    resultChosen = Signal(dict)
    useViewRequested = Signal()
    clearAreaRequested = Signal()
    selectionRemoveRequested = Signal(list)
    selectionClearRequested = Signal()
    buildingFocusRequested = Signal(str)
    exportRequested = Signal()
    openFolderRequested = Signal()
    clearCacheRequested = Signal()

    def __init__(self, settings: ModelSettings, parent=None):
        super().__init__(parent)
        self.s = settings
        self._loading = False
        self.setObjectName("sidebar")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedWidth(384)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        inner = QWidget()
        inner.setObjectName("sidebarInner")
        self.setWidget(inner)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(12, 10, 10, 12)
        inner.setMaximumWidth(384 - 14)      # never wider than the viewport beside the scroll bar
        lay.setSpacing(6)

        title = QLabel("CityModel")
        title.setObjectName("appTitle")
        lay.addWidget(title)
        lay.addWidget(_hint("Map area  →  3D terrain + buildings  →  multi-part 3MF"))

        lay.addWidget(self._build_search())
        lay.addWidget(self._build_area())
        lay.addWidget(self._build_model())
        lay.addWidget(self._build_layers())
        lay.addWidget(self._build_selection())
        lay.addWidget(self._build_export())
        lay.addWidget(self._build_look())
        lay.addStretch(1)
        self.load_from_settings()

    # ------------------------------------------------------------- sections
    def _build_search(self) -> QGroupBox:
        box = QGroupBox("1 · Find a place")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("City, address, landmark  —  or  32.0853, 34.7818")
        self.search_edit.returnPressed.connect(self._emit_search)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._emit_search)
        row.addWidget(self.search_edit, 1)
        row.addWidget(self.search_btn)
        v.addLayout(row)
        self.results = QListWidget()
        self.results.setVisible(False)
        self.results.setMaximumHeight(130)
        self.results.itemClicked.connect(
            lambda it: self.resultChosen.emit(it.data(Qt.ItemDataRole.UserRole)))
        v.addWidget(self.results)
        return box

    def _build_area(self) -> QGroupBox:
        box = QGroupBox("2 · Area")
        v = QVBoxLayout(box)
        self.area_label = QLabel("No area yet")
        self.area_label.setWordWrap(True)
        v.addWidget(self.area_label)
        v.addWidget(_hint("Draw a rectangle, polygon or circle with the tools at the top-left "
                          "of the map, then drag its handles to adjust."))
        row = QHBoxLayout()
        b1 = QPushButton("Use current map view")
        b1.clicked.connect(self.useViewRequested)
        b2 = QPushButton("Clear")
        b2.clicked.connect(self.clearAreaRequested)
        row.addWidget(b1, 1)
        row.addWidget(b2)
        v.addLayout(row)
        return box

    def _spin(self, field, lo, hi, step, decimals=1, suffix="", tip=""):
        w = _Spin()
        w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setDecimals(decimals)
        w.setSuffix(suffix)
        w.setToolTip(tip)
        w.setKeyboardTracking(False)
        w.valueChanged.connect(lambda val, f=field: self._set(f, float(val), True))
        self._bound[field] = w
        return w

    def _check(self, field, text, geometry=True, tip=""):
        w = QCheckBox(text)
        w.setToolTip(tip)
        w.toggled.connect(lambda val, f=field, g=geometry: self._set(f, bool(val), g))
        self._bound[field] = w
        return w

    def _combo(self, field, items, geometry=True, tip=""):
        w = _Combo()
        for key, label in items:
            w.addItem(label, key)
        w.setToolTip(tip)
        w.currentIndexChanged.connect(
            lambda _i, f=field, c=w, g=geometry: self._set(f, c.currentData(), g))
        self._bound[field] = w
        return w

    def _build_model(self) -> QGroupBox:
        self._bound = getattr(self, "_bound", {})
        box = QGroupBox("3 · Physical model")
        f = QFormLayout(box)
        f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        f.addRow("Longest side", self._spin("size_mm", 30, 600, 10, 0, " mm",
                                            "Size of the finished print along its longer side."))
        f.addRow("Base thickness", self._spin("base_thickness_mm", 0.6, 30, 0.2, 1, " mm",
                                              "Solid material under the lowest ground."))
        f.addRow("Vertical exaggeration", self._spin(
            "vertical_exaggeration", 0.2, 10, 0.1, 2, " ×",
            "Stretches heights -- terrain and buildings alike. 1.0 is true to scale."))
        f.addRow("Building height ×", self._spin(
            "building_height_scale", 0.2, 10, 0.1, 2, " ×",
            "Extra multiplier on building heights only."))
        f.addRow("Terrain detail", self._combo(
            "terrain_quality", [(k, QUALITY_LABELS[k]) for k in TERRAIN_QUALITY],
            tip="Size of one terrain grid cell on the print. Finer = more triangles."))
        f.addRow("Min. building height", self._spin(
            "min_building_height_mm", 0.0, 10, 0.1, 1, " mm",
            "Every building stands at least this far out of the ground."))
        f.addRow("Min. feature size", self._spin(
            "min_feature_mm", 0.0, 5, 0.1, 1, " mm",
            "Footprints and road widths below this cannot be printed: buildings are "
            "dropped, lines are widened. Roughly your nozzle width."))
        self.scale_label = _hint("")
        f.addRow(self.scale_label)
        return box

    def _build_layers(self) -> QGroupBox:
        box = QGroupBox("4 · Data and layers")
        v = QVBoxLayout(box)
        g = QGridLayout()
        g.addWidget(self._check("terrain_enabled", "Terrain relief"), 0, 0)
        g.addWidget(self._check("buildings_enabled", "Buildings"), 0, 1)
        g.addWidget(self._check("roads_enabled", "Roads + rail"), 1, 0)
        g.addWidget(self._check("water_enabled", "Water"), 1, 1)
        g.addWidget(self._check("green_enabled", "Parks + forest"), 2, 0)
        g.addWidget(self._check("roof_shapes", "Roof shapes", tip="Gabled, hipped, domes ... "
                                "where OpenStreetMap has roof:shape."), 2, 1)
        v.addLayout(g)
        f = QFormLayout()
        f.addRow("Elevation", self._combo("elevation_source", ELEVATION_LABELS))
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("OpenTopography API key (free at opentopography.org)")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.key_edit.editingFinished.connect(
            lambda: self._set("opentopo_key", self.key_edit.text().strip(), True))
        f.addRow(self.key_edit)
        f.addRow("Terrain smoothing", self._spin(
            "terrain_smoothing", 0.0, 5, 0.25, 2, "",
            "0 = raw data. 1 removes sensor noise and the terracing of integer-metre data."))
        v.addLayout(f)
        v.addWidget(self._check(
            "flatten_under_buildings", "Flatten radar 'bumps' under buildings",
            tip="SRTM and Copernicus measure roof tops, not ground. This levels the terrain "
                "under building footprints. Not applied to bare-earth sources."))
        ov = self._check("use_overture_heights", "Fill missing heights from Overture Maps",
                         tip="Slow the first time (1-2 min), cached afterwards.")
        if not overture.available():
            ov.setEnabled(False)
            ov.setToolTip("Needs the optional package:  pip install overturemaps")
        v.addWidget(ov)
        self.source_label = _hint("")
        v.addWidget(self.source_label)
        return box

    def _build_selection(self) -> QGroupBox:
        box = QGroupBox("5 · Selected buildings")
        v = QVBoxLayout(box)
        v.addWidget(_hint("Click buildings on the map or in the 3D preview. Each becomes its "
                          "own part in the export."))
        self.sel_list = QListWidget()
        self.sel_list.setMaximumHeight(140)
        self.sel_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.sel_list.itemDoubleClicked.connect(
            lambda it: self.buildingFocusRequested.emit(it.data(Qt.ItemDataRole.UserRole)))
        v.addWidget(self.sel_list)
        row = QHBoxLayout()
        rm = QPushButton("Remove")
        rm.clicked.connect(lambda: self.selectionRemoveRequested.emit(
            [i.data(Qt.ItemDataRole.UserRole) for i in self.sel_list.selectedItems()]))
        cl = QPushButton("Clear all")
        cl.clicked.connect(self.selectionClearRequested)
        row.addWidget(rm)
        row.addWidget(cl)
        row.addStretch(1)
        v.addLayout(row)
        return box

    def _build_export(self) -> QGroupBox:
        box = QGroupBox("6 · Export")
        v = QVBoxLayout(box)
        f = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("file name (no extension)")
        self.name_edit.editingFinished.connect(
            lambda: self._set("model_name", self.name_edit.text().strip(), False, quiet=True))
        f.addRow("Name", self.name_edit)
        row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.editingFinished.connect(
            lambda: self._set("output_dir", self.dir_edit.text().strip(), False, quiet=True))
        browse = QPushButton("…")
        browse.setFixedWidth(34)
        browse.clicked.connect(self._browse)
        row.addWidget(self.dir_edit, 1)
        row.addWidget(browse)
        f.addRow("Folder", row)
        f.addRow("Selected buildings", self._combo(
            "selected_mode", [("separate", "Each one a separate part"),
                              ("group", "All together as one part")], geometry=False))
        f.addRow("3MF layout", self._combo(
            "threemf_layout", [("parts", "One object, many parts  (recommended)"),
                               ("objects", "Separate objects")], geometry=False,
            tip="'Parts' opens in Bambu Studio / OrcaSlicer / PrusaSlicer as one object whose "
                "parts stay aligned and each take their own filament."))
        v.addLayout(f)
        v.addWidget(self._check("export_3mf", "3MF  (all components in one file)", False))
        v.addWidget(self._check("export_stl_folder", "Folder of STLs  (one per component)", False))
        v.addWidget(self._check(
            "cut_sockets", "Cut building sockets into the terrain", False,
            tip="Subtracts the buildings from the terrain part, so parts never overlap and "
                "separately printed buildings slot into place."))
        row = QHBoxLayout()
        self.export_btn = QPushButton("Export")
        self.export_btn.setObjectName("primary")
        self.export_btn.clicked.connect(self.exportRequested)
        self.export_btn.setEnabled(False)
        folder = QPushButton("Open folder")
        folder.clicked.connect(self.openFolderRequested)
        row.addWidget(self.export_btn, 1)
        row.addWidget(folder)
        v.addLayout(row)
        return box

    def _build_look(self) -> QGroupBox:
        box = QGroupBox("Appearance")
        v = QVBoxLayout(box)
        g = QGridLayout()
        self.color_btns = {}
        for i, (key, label) in enumerate(COLOR_LABELS):
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, k=key: self._pick_color(k))
            self.color_btns[key] = b
            g.addWidget(b, i // 3, i % 3)
        v.addLayout(g)
        v.addWidget(_hint("Colours are also written into the 3MF as the parts' display colours."))
        row = QHBoxLayout()
        row.addWidget(self._check("preview_ssao", "Ambient occlusion", False))
        row.addWidget(self._check("preview_edges", "Edge outlines", False))
        v.addLayout(row)
        cache = QPushButton("Clear download cache …")
        cache.clicked.connect(self.clearCacheRequested)
        v.addWidget(cache)
        return box

    # --------------------------------------------------------------- binding
    def load_from_settings(self):
        self._loading = True
        try:
            for field, w in self._bound.items():
                val = getattr(self.s, field)
                if isinstance(w, QDoubleSpinBox):
                    w.setValue(float(val))
                elif isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
                elif isinstance(w, QComboBox):
                    i = w.findData(val)
                    w.setCurrentIndex(max(i, 0))
            self.key_edit.setText(self.s.opentopo_key)
            self.name_edit.setText(self.s.model_name)
            self.dir_edit.setText(str(self.s.resolved_output_dir()))
            self._refresh_colors()
            self._refresh_key_visibility()
        finally:
            self._loading = False

    def _set(self, field, value, geometry, quiet=False):
        if self._loading or getattr(self.s, field) == value:
            return
        setattr(self.s, field, value)
        if field == "elevation_source":
            self._refresh_key_visibility()
        if quiet:
            return
        if geometry:
            self.geometryChanged.emit()
        elif field in ("preview_ssao", "preview_edges"):
            self.lookChanged.emit()

    def _refresh_key_visibility(self):
        self.key_edit.setVisible(self.s.elevation_source == "opentopography")

    def _refresh_colors(self):
        for key, b in self.color_btns.items():
            r, g, bl = self.s.color(key)
            fg = "#101010" if (r * 0.299 + g * 0.587 + bl * 0.114) > 140 else "#f4f4f4"
            b.setStyleSheet(f"background: rgb({r},{g},{bl}); color: {fg}; border: 1px solid #00000055;")

    def _pick_color(self, key):
        c = QColorDialog.getColor(QColor(*self.s.color(key)), self, f"{key.title()} colour")
        if c.isValid():
            self.s.colors[key] = [c.red(), c.green(), c.blue()]
            self._refresh_colors()
            self.lookChanged.emit()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Export folder", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)
            self.s.output_dir = d

    def _emit_search(self):
        q = self.search_edit.text().strip()
        if q:
            self.searchRequested.emit(q)

    # ------------------------------------------------------------ feedback
    def show_results(self, results: list):
        self.results.clear()
        if not results:
            it = QListWidgetItem("Nothing found -- try a different spelling.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.results.addItem(it)
        for r in results:
            it = QListWidgetItem(r["name"])
            it.setData(Qt.ItemDataRole.UserRole, r)
            it.setToolTip(r["name"])
            self.results.addItem(it)
        self.results.setVisible(True)

    def set_area_text(self, text: str, scale_text: str = ""):
        self.area_label.setText(text)
        self.scale_label.setText(scale_text)

    def set_source_text(self, text: str):
        self.source_label.setText(text)

    def set_selection(self, items: list):
        """items: [(osm_id, label)]"""
        self.sel_list.clear()
        for osm_id, label in items:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, osm_id)
            self.sel_list.addItem(it)
