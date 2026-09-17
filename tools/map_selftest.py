"""Checks the map page's JS <-> Python round trip for every area shape."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication, Qt, QTimer
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa
from PySide6.QtWidgets import QApplication
from citymodel.ui.map_widget import MapWidget
app = QApplication(sys.argv)
m = MapWidget(); m.resize(800, 600); m.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating); m.show()
cases = [{"shape": "rectangle", "bbox": [32.08, 34.77, 32.09, 34.79]},
         {"shape": "circle", "center": [32.085, 34.78], "radius_m": 400.0},
         {"shape": "polygon", "bbox": [32.08, 34.77, 32.09, 34.79],
          "ring": [[32.08, 34.77], [32.08, 34.79], [32.09, 34.78]]}]
got = []
def on_area(d):
    got.append(d); print("area from JS:", d["shape"], {k: v for k, v in d.items() if k != "ring"}, flush=True)
    nxt()
def nxt():
    if len(got) == len(cases):
        ok = [g["shape"] for g in got] == [c["shape"] for c in cases] and abs(got[1]["radius_m"] - 400) < 1
        print("MAP SELFTEST", "OK" if ok else "FAILED"); app.exit(0 if ok else 1); return
    m.set_area(cases[len(got)], True); m._js("sendArea();")
m.areaChanged.connect(on_area)
m.ready.connect(nxt)
QTimer.singleShot(40000, lambda: (print("MAP SELFTEST FAILED: timeout"), app.exit(1)))
sys.exit(app.exec())
