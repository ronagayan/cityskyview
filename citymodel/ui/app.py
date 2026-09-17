"""Application entry point."""

from __future__ import annotations

import os
import sys


def main(argv=None) -> int:
    # must be set before QApplication exists: VTK and the web view share GL
    os.environ.setdefault("QT_API", "pyside6")
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
