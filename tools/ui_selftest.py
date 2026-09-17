"""Drive the real app end to end without a human: set an area, load buildings,
select two, generate, export, and save screenshots of the window at each step.

    python tools/ui_selftest.py [out_dir]
"""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "output", "ui_selftest"))
os.makedirs(out, exist_ok=True)
os.environ["CITYMODEL_CONFIG"] = os.path.join(out, "config")
os.environ.setdefault("QT_API", "pyside6")

from PySide6.QtCore import QCoreApplication, Qt, QTimer
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa
from PySide6.QtWidgets import QApplication
from citymodel.log import setup_logging
from citymodel.settings import AppState, Area
from citymodel.ui import theme
from citymodel.ui.main_window import MainWindow

setup_logging(console=True)
app = QApplication(sys.argv); theme.apply(app)
state = AppState(); state.settings.output_dir = out; state.settings.model_name = "selftest"
state.settings.export_stl_folder = True
win = MainWindow(state); win.resize(1600, 950); win.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating); win.show()
BBOX = [40.7050, -74.0160, 40.7110, -74.0070]      # Lower Manhattan
steps, log = [], []

def shot(name):
    """A picture of THIS window only, never the desktop: Qt paints the widgets,
    and the two GPU surfaces (web map, VTK view) are read back and pasted in."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QImage, QPainter
    p = os.path.join(out, name + ".png")
    canvas = win.grab().toImage()
    painter = QPainter(canvas)
    painter.drawImage(win.map.mapTo(win, QPoint(0, 0)), win.map.grab().toImage())
    pl = win.preview.plotter
    arr = pl.screenshot(None, return_img=True)
    h, w, _ = arr.shape
    img = QImage(arr.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888).scaled(pl.width(), pl.height())
    painter.drawImage(pl.mapTo(win, QPoint(0, 0)), img)
    painter.end(); canvas.save(p); log.append(f"shot {p}")

def wait_for(cond, then, what, tries=600):
    def poll(n=[0]):
        n[0] += 1
        if cond(): then()
        elif n[0] > tries: fail(f"timeout waiting for {what}")
        else: QTimer.singleShot(250, poll)
    poll()

def fail(msg):
    print("SELFTEST FAILED:", msg); shot("99_failed"); app.exit(1)

def s1():
    win.map.set_area({"shape": "rectangle", "bbox": BBOX}, True)
    win._on_area({"shape": "rectangle", "bbox": BBOX})
    wait_for(lambda: win.features is not None and "osm" not in win._jobs, s2, "buildings")
def s2():
    ids = [f.osm_id for f in win.features["buildings"]
           if all(BBOX[0] < la < BBOX[2] and BBOX[1] < lo < BBOX[3] for la, lo in f.coords) and f.name][:2]
    print("selecting", ids); [win._toggle_building(i) for i in ids]
    QTimer.singleShot(2500, s3)
def s3():
    shot("01_map_with_footprints"); win._generate()
    wait_for(lambda: win.model is not None and "build" not in win._jobs, s4, "model")
def s4():
    QTimer.singleShot(3000, s5)
def s5():
    shot("02_model"); win._open_folder = lambda: None
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.exec = lambda self: 0                  # no modal dialogs in the self-test
    win._export()
    wait_for(lambda: "export" not in win._jobs and os.path.exists(os.path.join(out, "selftest.3mf")), s6, "export")
def s6():
    from citymodel.export.threemf import read_3mf_summary
    s = read_3mf_summary(os.path.join(out, "selftest.3mf"))
    print("3MF objects:", [(o["name"], o["triangles"]) for o in s["objects"]])
    print("STLs:", sorted(os.listdir(os.path.join(out, "selftest_stl"))))
    win.preview.top_view(); QTimer.singleShot(1500, s7)
def s7():
    shot("03_top_view"); print("SELFTEST OK"); win.close(); app.exit(0)

win.map.ready.connect(lambda: QTimer.singleShot(1500, s1))
QTimer.singleShot(240_000, lambda: fail("global timeout"))
sys.exit(app.exec())
