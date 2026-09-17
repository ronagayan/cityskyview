"""CityModel -- pick an area on a map, get a printable 3D terrain + city model.

Package layout
--------------
    data/     downloading + caching (Overpass, elevation providers, Overture)
    geo/      projection and DEM resampling
    mesh/     terrain, buildings, draped layers, validation / repair
    export/   3MF (multi-part) and per-component STL
    render/   PyVista scene used by the preview (and by offscreen snapshots)
    ui/       the PySide6 application
    pipeline  fetch -> build -> export orchestration, usable headless
"""

__version__ = "2.0.0"
APP_NAME = "CityModel"
USER_AGENT = (f"citymodel/{__version__} (personal hobby 3D-print project; "
              "https://www.openstreetmap.org/)")
