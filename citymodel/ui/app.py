"""Application entry point."""

from __future__ import annotations

import os
import sys


def main(argv=None) -> int:
    # must be set before QApplication exists: VTK and the web view share GL
    os.environ.setdefault("QT_API", "pyside6")
    # Hybrid-GPU laptops (an Intel iGPU alongside a discrete NVIDIA/AMD GPU) can
    # have the WebEngine (Chromium/ANGLE) and VTK (native WGL) renderers land on
    # different adapters, or have Chromium's GPU process fall back to a software
    # rasterizer via its driver blocklist. Both show up as blank white panels
    # that fixed themselves on the next launch -- pinning ANGLE to D3D11 and
    # ignoring that blocklist removes most of that non-determinism. If GPU
    # trouble is still suspected, CITYMODEL_SOFTWARE_GL=1 forces both onto
    # software rendering (slower, but never blank).
    if os.environ.get("CITYMODEL_SOFTWARE_GL") == "1":
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--use-gl=swiftshader")
        os.environ.setdefault("MESA_GL_VERSION_OVERRIDE", "3.3")
    else:
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                              "--use-angle=d3d11 --ignore-gpu-blocklist")
    from PySide6.QtCore import QCoreApplication, Qt  # noqa: PLC0415
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
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
