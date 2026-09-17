import sys, time
sys_path_fix = __import__("sys").path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
__import__("sys").stdout.reconfigure(encoding="utf-8", errors="replace")
from citymodel.log import setup_logging; setup_logging()
from citymodel.settings import Area, ModelSettings
from citymodel import pipeline
name, bbox = sys.argv[1], tuple(float(v) for v in sys.argv[2:6])
area = Area(bbox=bbox); s = ModelSettings(green_enabled=True)
t=time.time(); data = pipeline.fetch_data(area, s); print("fetch", round(time.time()-t,1))
t=time.time(); model = pipeline.build_model(area, s, data); print("build", round(time.time()-t,1), model.size_mm)
sel = model.building_ids()[:3]
t=time.time(); res = pipeline.export_model(model, sel, s, "output", name); print("export", round(time.time()-t,1))
print(res.report_text()); print(res.summary_3mf["assembly"], [ (o["name"],o["triangles"]) for o in res.summary_3mf["objects"]])
from citymodel.render.scene import snapshot
t=time.time(); snapshot(model, s, f"output/{name}.png", sel); print("snapshot", round(time.time()-t,1))
