"""Main window: map | 3D preview, settings sidebar, progress + log."""

from __future__ import annotations

import os
from dataclasses import asdict

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
                               QToolButton, QVBoxLayout, QWidget)

from .. import pipeline
from ..data import cache as cachelib
from ..data import geocode
from ..data.overpass import MAX_AREA_KM2, bbox_area_km2
from ..geo.frame import area_bbox, make_frame
from ..log import logger
from ..settings import AppState, Area, save_state
from . import theme
from .map_widget import MapWidget, footprints_geojson
from .preview_widget import PreviewWidget
from .sidebar import Sidebar
from .workers import Job, QtLogHandler, start

AUTO_LOAD_KM2 = 6.0          # footprints load by themselves for areas up to this size


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.s = state.settings
        self.area: Area | None = state.area if state.window.get("has_area") else None
        self.selected: list = list(state.selected_ids)
        self.features: dict | None = None
        self.features_bbox = None
        self.model = None
        self.model_key = None
        self._jobs: dict = {}
        self._names: dict = {}

        self.setWindowTitle("CityModel — terrain + city models for 3D printing")
        self.resize(*state.window.get("size", (1500, 900)))
        self._build_ui()
        self._wire()

        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(900)
        self._load_timer.timeout.connect(self._auto_load_buildings)
        self._refresh_area_text()
        self._refresh_selection_list()
        self._update_buttons()

    # -------------------------------------------------------------------- UI
    def _panel(self, title: str, body: QWidget, extra: list | None = None) -> QFrame:
        frame = QFrame()
        frame.setObjectName("panel")
        v = QVBoxLayout(frame)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)
        if title:
            bar = QHBoxLayout()
            bar.setContentsMargins(10, 6, 8, 6)
            lab = QLabel(title)
            lab.setObjectName("panelTitle")
            bar.addWidget(lab)
            bar.addStretch(1)
            for w in extra or []:
                bar.addWidget(w)
            v.addLayout(bar)
        v.addWidget(body, 1)
        return frame

    def _build_ui(self):
        self.sidebar = Sidebar(self.s)
        self.map = MapWidget()
        self.preview = PreviewWidget(self.s)

        self.load_btn = QToolButton()
        self.load_btn.setText("Load buildings")
        self.load_btn.setToolTip("Download the building outlines for the area so you can "
                                 "click them on the map.")
        self.reload_btn = QToolButton()
        self.reload_btn.setText("Reload map")
        map_panel = self._panel("Map", self.map, [self.load_btn, self.reload_btn])
        prev_panel = QFrame()
        prev_panel.setObjectName("panel")
        pv = QVBoxLayout(prev_panel)
        pv.setContentsMargins(1, 1, 1, 1)
        pv.addWidget(self.preview)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(map_panel)
        self.split.addWidget(prev_panel)
        self.split.setChildrenCollapsible(False)
        self.split.setSizes([700, 700])

        # action row
        self.generate_btn = QPushButton("Generate model")
        self.generate_btn.setObjectName("primary")
        self.generate_btn.setMinimumWidth(170)
        self.stale = QLabel("")
        self.stale.setObjectName("stale")
        self.stale.setVisible(False)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(220)
        self.status = QLabel("Ready.")
        self.status.setObjectName("muted")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.log_btn = QToolButton()
        self.log_btn.setText("Log")
        self.log_btn.setCheckable(True)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(self.generate_btn)
        actions.addWidget(self.stale)
        actions.addSpacing(8)
        actions.addWidget(self.progress)
        actions.addWidget(self.status, 1)
        actions.addWidget(self.cancel_btn)
        actions.addWidget(self.log_btn)

        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setVisible(False)
        self.log.setFixedHeight(170)

        centre = QWidget()
        cv = QVBoxLayout(centre)
        cv.setContentsMargins(8, 8, 8, 8)
        cv.setSpacing(8)
        cv.addWidget(self.split, 1)
        cv.addLayout(actions)
        cv.addWidget(self.log)

        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self.sidebar)
        h.addWidget(centre, 1)
        self.setCentralWidget(root)

        self.log_handler = QtLogHandler()
        self.log_handler.emitter.record.connect(self._append_log)
        logger.addHandler(self.log_handler)

    def _wire(self):
        sb = self.sidebar
        sb.geometryChanged.connect(self._on_geometry_setting)
        sb.lookChanged.connect(lambda: self.preview.apply_look(self.selected))
        sb.searchRequested.connect(self._search)
        sb.resultChosen.connect(self._go_to_result)
        sb.useViewRequested.connect(lambda: self.map.area_from_view(0.7))
        sb.clearAreaRequested.connect(lambda: (self.map.set_area(None), self._on_area(None)))
        sb.selectionRemoveRequested.connect(self._remove_selected)
        sb.selectionClearRequested.connect(lambda: self._set_selection([]))
        sb.buildingFocusRequested.connect(self.map.focus_building)
        sb.exportRequested.connect(self._export)
        sb.openFolderRequested.connect(self._open_folder)
        sb.clearCacheRequested.connect(self._clear_cache)

        self.map.ready.connect(self._map_ready)
        self.map.areaChanged.connect(self._on_area)
        self.map.buildingClicked.connect(self._toggle_building)
        self.map.viewChanged.connect(lambda v: self.state.map_view.update(v))
        self.preview.buildingClicked.connect(self._toggle_building)

        self.load_btn.clicked.connect(lambda: self._load_buildings(force=True))
        self.reload_btn.clicked.connect(self.map.reload_map)
        self.generate_btn.clicked.connect(self._generate)
        self.cancel_btn.clicked.connect(self._cancel_jobs)
        self.log_btn.toggled.connect(self.log.setVisible)

    # ------------------------------------------------------------------- map
    def _map_ready(self):
        if self.state.map_view:
            self.map.restore_view(self.state.map_view)
        if self.area is not None:
            self.map.set_area(self._area_dict(self.area), fit=not self.state.map_view)
            self._load_timer.start()
        self.map.set_selection(self.selected)

    @staticmethod
    def _area_dict(a: Area) -> dict:
        return {"shape": a.shape, "bbox": list(a.bbox), "ring": [list(p) for p in a.ring],
                "center": list(a.center), "radius_m": a.radius_m}

    def _on_area(self, d):
        if not d:
            self.area = None
        else:
            a = Area(shape=d.get("shape", "rectangle"),
                     bbox=tuple(d.get("bbox") or (0, 0, 0, 0)),
                     ring=[tuple(p) for p in d.get("ring") or []],
                     center=tuple(d.get("center") or ()), radius_m=float(d.get("radius_m") or 0))
            a.bbox = tuple(area_bbox(a))
            self.area = a if a.is_valid() else None
        self._refresh_area_text()
        self._mark_stale()
        self._update_buttons()
        if self.area is not None:
            self._load_timer.start()

    def _refresh_area_text(self):
        if self.area is None:
            self.sidebar.set_area_text("No area yet — draw one on the map.")
            return
        try:
            frame = make_frame(self.area, self.s.size_mm)
        except ValueError:
            self.sidebar.set_area_text("That area has no size — draw it again.")
            return
        w, h = frame.size_m
        wm, hm = frame.size_mm
        km2 = bbox_area_km2(self.area.bbox)
        fmt = (lambda v: f"{v / 1000:.2f} km" if v >= 1000 else f"{v:.0f} m")
        warn = ""
        if km2 > MAX_AREA_KM2:
            warn = (f"\n⚠ Too large for building-level data (limit {MAX_AREA_KM2:.0f} km²). "
                    "Switch buildings / roads / water off for a terrain-only model.")
        self.sidebar.set_area_text(f"{self.area.shape.title()}  ·  {fmt(w)} × {fmt(h)}  ·  "
                                   f"{km2:.2f} km²{warn}",
                                   f"Print: {wm:.0f} × {hm:.0f} mm at 1:{1000 / frame.scale:,.0f}  ·  "
                                   f"a 10 m house is {10 * frame.scale:.1f} mm wide")

    # -------------------------------------------------------------- buildings
    def _auto_load_buildings(self):
        if (self.area is not None and self.s.buildings_enabled
                and bbox_area_km2(self.area.bbox) <= AUTO_LOAD_KM2):
            self._load_buildings(force=False)

    def _features_cover(self, bbox) -> bool:
        fb = self.features_bbox
        return (self.features is not None and fb is not None and fb[0] <= bbox[0]
                and fb[1] <= bbox[1] and fb[2] >= bbox[2] and fb[3] >= bbox[3])

    def _load_buildings(self, force: bool):
        if self.area is None:
            self._say("Draw an area on the map first.")
            return
        if "osm" in self._jobs:
            return
        bbox = tuple(self.area.bbox)
        if self._features_cover(bbox) and not force:
            self._show_footprints()
            return
        area = Area(**asdict(self.area))

        def work(rep):
            from ..data.overpass import snap_bbox  # noqa: PLC0415
            return pipeline.fetch_osm_features(area, rep), snap_bbox(area_bbox(area))

        def done(result):
            self.features, self.features_bbox = result
            self._show_footprints()
            n = len(self.features.get("buildings", []))
            self._say(f"{n:,} buildings loaded — click any of them to select it.")

        self._run("osm", work, done, "Loading buildings")

    def _show_footprints(self):
        if self.features is None or self.area is None:
            return
        gj = footprints_geojson(self.features, self.area.bbox)
        self._names = {f["properties"]["id"]: f["properties"]["name"] for f in gj["features"]}
        self.map.set_footprints(gj)
        self.map.set_selection(self.selected)
        self._refresh_selection_list()

    def _toggle_building(self, osm_id: str):
        sel = list(self.selected)
        if osm_id in sel:
            sel.remove(osm_id)
        else:
            sel.append(osm_id)
        self._set_selection(sel)

    def _remove_selected(self, ids):
        self._set_selection([b for b in self.selected if b not in set(ids)])

    def _set_selection(self, ids):
        self.selected = list(dict.fromkeys(ids))
        self.map.set_selection(self.selected)
        self.preview.set_selection(self.selected)
        self._refresh_selection_list()

    def _label_of(self, osm_id: str) -> str:
        name = self._names.get(osm_id) or (self.model.building_name(osm_id) if self.model else "")
        return f"{name}   ({osm_id})" if name else osm_id

    def _refresh_selection_list(self):
        self.sidebar.set_selection([(b, self._label_of(b)) for b in self.selected])

    # ---------------------------------------------------------------- search
    def _search(self, query: str):
        def done(results):
            self.sidebar.show_results(results)
            if len(results) == 1:
                self._go_to_result(results[0])
        self._run("search", lambda rep: geocode.search(query, reporter=rep), done, "Searching")

    def _go_to_result(self, r: dict):
        if not r:
            return
        self.map.fly_to(r["lat"], r["lon"], 16, r.get("bbox"))

    # -------------------------------------------------------------- generate
    def _settings_key(self):
        d = asdict(self.s)
        for k in ("colors", "preview_ssao", "preview_edges", "selected_mode", "threemf_layout",
                  "export_3mf", "export_stl_folder", "cut_sockets", "output_dir", "model_name"):
            d.pop(k, None)
        return repr(sorted(d.items())) + repr(asdict(self.area) if self.area else None)

    def _on_geometry_setting(self):
        self._refresh_area_text()
        self._mark_stale()

    def _mark_stale(self):
        stale = self.model is not None and self.model_key != self._settings_key()
        self.stale.setText("Settings changed — press Generate to update the model")
        self.stale.setVisible(stale)

    def _generate(self):
        if self.area is None:
            QMessageBox.information(self, "No area", "Draw an area on the map first "
                                    "(rectangle, polygon or circle tool, top-left of the map).")
            return
        if "build" in self._jobs:
            return
        area, s = Area(**asdict(self.area)), self.s
        import copy  # noqa: PLC0415
        s_copy = copy.deepcopy(s)
        feats = self.features if self._features_cover(tuple(area.bbox)) else None
        key = self._settings_key()

        def work(rep):
            data = pipeline.fetch_data(area, s_copy, rep.span(0.0, 0.45), features=feats)
            model = pipeline.build_model(area, s_copy, data, rep.span(0.45, 1.0))
            return data, model

        def done(result):
            data, model = result
            keep_cam = self.model is not None and self.model_key is not None \
                and asdict(self.area) == self._last_area
            self.model, self.model_key = model, key
            self._last_area = asdict(self.area)
            if data.features and not self._features_cover(tuple(area.bbox)):
                from ..data.overpass import snap_bbox  # noqa: PLC0415
                self.features, self.features_bbox = data.features, snap_bbox(area.bbox)
                self._show_footprints()
            known = set(model.building_ids())
            self.preview.show_model(model, [b for b in self.selected if b in known],
                                    keep_camera=keep_cam)
            info = model.info
            b = info.get("buildings") or {}
            hs = b.get("height_sources") or {}
            self.sidebar.set_source_text(
                f"Elevation: {info['elevation_source']} ({info['elevation_kind']})"
                + (f"\nBuildings: {b.get('built', 0):,} — heights from tags {hs.get('tag', 0):,}, "
                   f"levels {hs.get('levels', 0):,}, Overture {hs.get('overture', 0):,}, "
                   f"estimated {hs.get('estimate', 0):,}" if b else "")
                + (f"\n{info['elevation_notes']}" if info.get("elevation_notes") else ""))
            self._mark_stale()
            self._refresh_selection_list()
            self._update_buttons()
            w, h, z = model.size_mm
            self._say(f"Model ready: {w:.0f} × {h:.0f} × {z:.0f} mm, "
                      f"{len(model.buildings):,} buildings, built in {info['build_seconds']} s.")

        self._last_area = getattr(self, "_last_area", None)
        self._run("build", work, done, "Generating")

    # ---------------------------------------------------------------- export
    def _export(self):
        if self.model is None or "export" in self._jobs:
            return
        if self.stale.isVisible():
            r = QMessageBox.question(
                self, "Model is out of date",
                "Settings or the area changed since the model was generated.\n\n"
                "Export the model as it is shown now?")
            if r != QMessageBox.StandardButton.Yes:
                return
        import copy  # noqa: PLC0415
        model, s, sel = self.model, copy.deepcopy(self.s), list(self.selected)
        name = s.model_name or pipeline.default_model_name(self.area or Area())

        def work(rep):
            return pipeline.export_model(model, sel, s, None, name, rep)

        def done(res):
            files = "\n".join(f"  {p}" for p in res.files)
            bad = [c for c in res.components if c.report and not c.report.ok]
            box = QMessageBox(self)
            box.setWindowTitle("Export finished")
            box.setIcon(QMessageBox.Icon.Warning if bad else QMessageBox.Icon.Information)
            box.setText(f"{len(res.components)} components written.\n\n{files}")
            box.setInformativeText(res.report_text())
            open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                self._open_folder()
            self._say(f"Exported {len(res.components)} components to {res.files[0].parent}")

        self._run("export", work, done, "Exporting")

    def _open_folder(self):
        d = self.s.resolved_output_dir()
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _clear_cache(self):
        mb = cachelib.cache_size_bytes() / 1e6
        r = QMessageBox.question(self, "Clear download cache",
                                 f"The cache holds {mb:.0f} MB of map data and elevation tiles.\n"
                                 "Deleting it means areas you already used download again.\n\n"
                                 "Delete it?")
        if r == QMessageBox.StandardButton.Yes:
            freed = cachelib.clear_cache()
            self._say(f"Cache cleared ({freed / 1e6:.0f} MB).")

    # ------------------------------------------------------------------ jobs
    def _run(self, key, fn, on_done, label):
        job = Job(fn, label)
        self._jobs[key] = job
        job.signals.progress.connect(self._on_progress)
        job.signals.done.connect(on_done)
        job.signals.failed.connect(lambda msg, tb, lab=label: self._on_failed(lab, msg, tb))
        job.signals.cancelled.connect(lambda lab=label: self._say(f"{lab} cancelled."))
        job.signals.finished.connect(lambda k=key: self._job_finished(k))
        self._say(f"{label} …")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.cancel_btn.setVisible(True)
        self._update_buttons()
        start(job)

    def _job_finished(self, key):
        self._jobs.pop(key, None)
        if not self._jobs:
            self.progress.setVisible(False)
            self.cancel_btn.setVisible(False)
        self._update_buttons()

    def _cancel_jobs(self):
        for job in self._jobs.values():
            job.cancel()
        self._say("Cancelling …")

    def _on_progress(self, message, fraction):
        self.status.setText(message)
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(fraction * 1000))

    def _on_failed(self, label, message, tb):
        self._say(f"{label} failed.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(f"{label} failed")
        box.setText(message)
        if tb:
            box.setDetailedText(tb)
            box.setInformativeText("This looks like a bug rather than a network problem. "
                                   "The details are in the log.")
        box.exec()

    def _update_buttons(self):
        busy_build = "build" in self._jobs
        self.generate_btn.setEnabled(self.area is not None and not busy_build)
        self.generate_btn.setText("Generating …" if busy_build else "Generate model")
        self.load_btn.setEnabled(self.area is not None and "osm" not in self._jobs)
        self.sidebar.export_btn.setEnabled(self.model is not None and "export" not in self._jobs
                                           and not busy_build)

    def _say(self, text: str):
        self.status.setText(text)

    def _append_log(self, level: str, line: str):
        color = {"WARNING": theme.ACCENT, "ERROR": theme.DANGER, "CRITICAL": theme.DANGER}.get(level)
        esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        self.log.appendHtml(f'<span style="color:{color}">{esc}</span>' if color else esc)
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        if level in ("ERROR", "CRITICAL") and not self.log_btn.isChecked():
            self.log_btn.setChecked(True)

    # ----------------------------------------------------------------- close
    def closeEvent(self, event):
        for job in self._jobs.values():
            job.cancel()
        self.state.settings = self.s
        if self.area is not None:
            self.state.area = self.area
        self.state.selected_ids = list(self.selected)
        self.state.window = {"size": (self.width(), self.height()),
                             "has_area": self.area is not None}
        save_state(self.state)
        logger.removeHandler(self.log_handler)
        self.preview.shutdown()
        super().closeEvent(event)
