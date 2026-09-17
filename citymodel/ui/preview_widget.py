"""The interactive 3D preview (PyVista / VTK inside Qt)."""

from __future__ import annotations

import numpy as np
import vtk
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget
from pyvistaqt import QtInteractor

from ..log import logger
from ..model import Model
from ..render.scene import ModelScene
from ..settings import ModelSettings

CLICK_SLOP_PX = 4


class PreviewWidget(QWidget):
    buildingClicked = Signal(str)

    def __init__(self, settings: ModelSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        self.title = QLabel("3D preview")
        self.title.setObjectName("panelTitle")
        bar.addWidget(self.title)
        bar.addSpacing(14)
        bar.addStretch(1)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        bar.addWidget(self.info)
        for text, tip, fn in (("Fit", "Reset the camera", self.reset_camera),
                              ("Top", "Look straight down", self.top_view),
                              ("PNG", "Save a screenshot", self.save_screenshot),
                              ("Reinit", "Panel showing blank / wrong colours? Rebuild the 3D "
                               "view -- this recovers from a one-off GPU context failure on "
                               "startup (common on laptops with two graphics cards).",
                               self.reinitialize)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        outer.addLayout(bar)

        # The VTK widget is always the visible one. (Hiding it behind a stacked
        # placeholder means its native window does not exist yet when the first
        # model arrives, and VTK then fails to create its OpenGL context.)
        self._outer = outer
        self._picker = vtk.vtkCellPicker()
        self._picker.SetTolerance(0.0008)
        self.plotter = None
        self.scene = None
        self._model = None
        self._selected_ids = ()
        self._build_plotter()

    def _build_plotter(self):
        """(Re)create the VTK widget. Startup can occasionally lose the race for
        a hardware OpenGL context on a hybrid-GPU laptop (Intel + a discrete
        GPU) and end up with a blank panel; tearing it down and building a
        fresh one -- via the Reinit button, or automatically once here if the
        very first frame comes back suspiciously blank -- reliably recovers."""
        if self.plotter is not None:
            self._outer.removeWidget(self.plotter)
            try:
                self.plotter.close()
            except Exception:  # noqa: BLE001
                pass
            self.plotter.deleteLater()
        self.plotter = QtInteractor(self, auto_update=False)
        self._outer.addWidget(self.plotter, 1)
        self._placeholder = None
        self.scene = ModelScene(self.plotter, self.settings)
        self._press = None
        iren = self.plotter.iren.interactor
        iren.AddObserver("LeftButtonPressEvent", self._on_press, 1.0)
        iren.AddObserver("LeftButtonReleaseEvent", self._on_release, 1.0)
        self.plotter.enable_trackball_style()
        self._show_placeholder()
        if self._model is not None:
            self.scene.show(self._model, self._selected_ids, reset_camera=True)

    def reinitialize(self):
        logger.info("preview: rebuilding the 3D view")
        self._build_plotter()

    def check_first_frame(self):
        """Called once, shortly after the window is first shown: if the panel
        never painted anything but its own background (a failed GPU context
        rendering nothing at all), rebuild it automatically so a person never
        has to notice and click Reinit themselves."""
        try:
            img = self.plotter.screenshot(None, return_img=True)
        except Exception:  # noqa: BLE001
            return
        if img is not None and float(np.ptp(img)) < 1.0:     # a flat, single-colour frame
            logger.warning("preview: the 3D view rendered a blank frame on startup -- "
                           "rebuilding it")
            self._build_plotter()

    PLACEHOLDER = "\n".join((
        "No model yet", "",
        "Mark an area on the map, then press  Generate model.", "",
        "Left-drag: orbit      Wheel / right-drag: zoom      Shift+drag: pan",
        "Click a building to select it."))

    def _show_placeholder(self):
        if self._placeholder is None:
            self._placeholder = self.plotter.add_text(
                self.PLACEHOLDER, position="upper_left", font_size=10, color="#8a93a1",
                name="placeholder")

    def _hide_placeholder(self):
        if self._placeholder is not None:
            self.plotter.remove_actor("placeholder", render=False)
            self._placeholder = None

    # ------------------------------------------------------------------ model
    def show_model(self, model: Model, selected_ids=(), keep_camera: bool = False):
        self._model, self._selected_ids = model, tuple(selected_ids)
        self._hide_placeholder()
        self.scene.show(model, selected_ids, reset_camera=not keep_camera)
        w, h, z = model.size_mm
        n = len(model.terrain.faces) + sum(len(b.mesh.faces) for b in model.buildings)
        self.info.setText(f"{w:.0f} x {h:.0f} x {z:.0f} mm   ·   {model.info.get('scale', '')}"
                          f"   ·   {n:,} triangles   ")

    def clear(self):
        self.scene.clear()
        self.info.setText("")
        self._show_placeholder()
        self.plotter.render()

    def set_selection(self, ids):
        self._selected_ids = tuple(ids)
        self.scene.set_selection(ids)

    def apply_look(self, selected_ids=()):
        """Colours / SSAO / edges changed -- rebuild the actors, keep the camera."""
        if self.scene.model is not None:
            self.scene.show(self.scene.model, selected_ids, reset_camera=False)

    def reset_camera(self):
        self.scene.reset_camera()

    def top_view(self):
        self.scene.top_view()

    def save_screenshot(self):
        from PySide6.QtWidgets import QFileDialog  # noqa: PLC0415
        if self.scene.model is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save preview image", "preview.png",
                                              "PNG image (*.png)")
        if path:
            self.plotter.screenshot(path)
            logger.info("preview saved: %s", path)

    # ---------------------------------------------------------------- picking
    def _on_press(self, obj, _event):
        self._press = obj.GetEventPosition()

    def _on_release(self, obj, _event):
        if self._press is None or self.scene.model is None:
            return
        x, y = obj.GetEventPosition()
        px, py = self._press
        self._press = None
        if abs(x - px) > CLICK_SLOP_PX or abs(y - py) > CLICK_SLOP_PX:
            return                                   # that was a drag (orbit)
        actor = self.scene._actors.get("buildings")
        if actor is None:
            return
        self._picker.InitializePickList()
        self._picker.AddPickList(actor)
        self._picker.PickFromListOn()
        if self._picker.Pick(x, y, 0, self.plotter.renderer) and self._picker.GetCellId() >= 0:
            bid = self.scene.building_at_cell(self._picker.GetCellId())
            if bid:
                self.buildingClicked.emit(bid)

    def shutdown(self):
        try:
            self.plotter.close()
        except Exception:  # noqa: BLE001
            pass
