import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shapely
from shapely.ops import unary_union
from citymodel.settings import Area, ModelSettings
from citymodel import pipeline
from citymodel.geo.frame import make_frame
from citymodel.mesh.surface import make_surface
from citymodel.mesh.buildings import build_buildings
from citymodel.data import elevation
area = Area(bbox=(32.805, 34.980, 32.818, 34.998)); s = ModelSettings(green_enabled=True)
data = pipeline.fetch_data(area, s); frame = make_frame(area, s.size_mm)
surface,_ = make_surface(frame, data.dem.dem, "DSM", 30, s)
solids,_ = build_buildings(data.features, frame, surface, s)
fps = [b.footprint for b in solids]
def T(label, fn):
    t=time.time(); r=fn(); print(f"{label:45s} {time.time()-t:6.2f}s", flush=True); return r
T("unary_union(footprints)", lambda: unary_union(fps))
T("union_all(footprints, grid 0.001)", lambda: shapely.union_all(fps, grid_size=0.001))
T("coverage-ish: union of buffered(0.02)", lambda: unary_union([f.buffer(0.02) for f in fps]))
T("union of UNSNAPPED? (buffer(0))", lambda: unary_union([f.buffer(0) for f in fps]))
