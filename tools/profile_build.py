import sys, os, cProfile, pstats, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from citymodel.settings import Area, ModelSettings
from citymodel import pipeline
area = Area(bbox=(32.805, 34.980, 32.818, 34.998)); s = ModelSettings(green_enabled=True)
data = pipeline.fetch_data(area, s)
pr = cProfile.Profile(); pr.enable(); m = pipeline.build_model(area, s, data); pr.disable()
out = io.StringIO(); pstats.Stats(pr, stream=out).sort_stats("cumulative").print_stats(28); print(out.getvalue()[:6000])
