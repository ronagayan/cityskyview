"""Application entry point."""

from __future__ import annotations

import os
import sys


def main(argv=None) -> int:
    # must be set before QApplication exists: VTK and the web view share GL
    os.environ.setdefault("QT_API", "pyside6")
    # Hybrid-GPU laptops (an Intel iGPU alongside a discrete NVIDIA/AMD GPU) can
    # have the WebEngine (Chromium/ANGLE) and VTK (native WGL) renderers land on
    # different adapters, or race for a hardware context at startup, and one or
    # both panels come up blank -- or, forcing a specific ANGLE backend turned
    # out to trade that for visual corruption on at least one real machine, so
    # it is opt-in, not a default: CITYMODEL_GPU=angle-d3d11 to try that,
    # CITYMODEL_GPU=software to force both onto a software rasterizer (slow,
    # but never blank or corrupted -- useful to confirm it really is a GPU
    # driver issue). Left unset, Chromium picks its own backend as normal.
    gpu_mode = os.environ.get("CITYMODEL_GPU", "")
    if gpu_mode == "software":
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--use-gl=swiftshader")
        os.environ.setdefault("MESA_GL_VERSION_OVERRIDE", "3.3")
    elif gpu_mode == "angle-d3d11":
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                              "--use-angle=d3d11 --ignore-gpu-blocklist")
    from PySide6.QtCore import QCoreApplication, Qt  # noqa: PLC0415
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401, PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from ..log import setup_logging  # noqa: PLC0415
    from ..settings import load_state  # noqa: PLC0415
    from . import theme  # noqa: PLC0415

    setup_logging(console=sys.stderr is not None and sys.stderr.isatty() if sys.stderr else False)
    app = QApplication(list(argv or sys.argv))
    app.setApplicationName("CityModel")
    theme.apply(app)

    from .main_window import MainWindow  # noqa: PLC0415
    win = MainWindow(load_state())
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
