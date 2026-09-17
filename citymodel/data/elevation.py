"""Free elevation sources, picked automatically by location, cached on disk.

==================  =========  ====  ==========================  ==========
provider            best res.  kind  coverage                    API key
==================  =========  ====  ==========================  ==========
usgs3dep            1 m        DTM   USA (lidar; else 10 m)      no
terrarium (AWS)     2-10 m     DTM   UK, Austria, Norway, NZ     no
glo30 (Copernicus)  30 m       DSM   global                      no
terrarium (AWS)     30 m       DSM   global (SRTM)               no
opentopography      30 m       DSM   global                      free key
==================  =========  ====  ==========================  ==========

DTM = bare earth. DSM = the surface the radar saw, roofs and tree tops
included, which is why the pipeline flattens DSM data under building
footprints (see geo.dem.suppress_bumps).

Order of preference in "auto": the finest bare-earth source that covers the
area, then Copernicus GLO-30 (newer and cleaner than SRTM), then the AWS
Terrarium tiles, which cover everything and need nothing but HTTP.
"""

from __future__ import annotations

import concurrent.futures as cf
import io
import math
from dataclasses import dataclass, replace
from typing import Sequence

import numpy as np

from ..geo.dem import DemMosaic, DemRaster
from ..log import logger
from ..progress import NULL_REPORTER, Reporter
from .cache import DAY, DiskCache
from .http import NetworkError, get


class ElevationUnavailable(RuntimeError):
    """This provider has nothing (usable) here -- try the next one."""


@dataclass(frozen=True)
class SourceInfo:
    key: str
    title: str
    resolution: str
    kind: str                 # "DTM" | "DSM"
    license: str
    attribution: str


@dataclass
class DemResult:
    dem: object               # DemRaster | DemMosaic
    source: SourceInfo
    native_res_m: float
    notes: str = ""


def _pad(bbox, frac=0.06, min_deg=0.0015):
    s, w, n, e = bbox
    dy = max((n - s) * frac, min_deg)
    dx = max((e - w) * frac, min_deg)
    return (s - dy, w - dx, n + dy, e + dx)


def _in(bbox, box) -> bool:
    s, w, n, e = bbox
    return box[0] <= s and n <= box[2] and box[1] <= w and e <= box[3]


# ---------------------------------------------------------------------------
# USGS 3DEP -- dynamic image service, best available bare-earth data in the US
# ---------------------------------------------------------------------------

class Usgs3dep:
    info = SourceInfo(
        "usgs3dep", "USGS 3DEP", "1 m where lidar exists, else 10 m", "DTM",
        "US public domain", "Elevation: U.S. Geological Survey 3DEP")
    URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
           "3DEPElevation/ImageServer/exportImage")
    REGIONS = ((24.3, -125.1, 49.5, -66.8),      # contiguous states
               (51.0, -180.0, 71.6, -129.0),     # Alaska
               (18.8, -160.5, 22.4, -154.6),     # Hawaii
               (17.6, -67.5, 18.6, -64.4))       # Puerto Rico / USVI
    MAX_PX = 3600

    def covers(self, bbox) -> bool:
        return any(_in(bbox, r) for r in self.REGIONS)

    def fetch(self, bbox, target_res_m, reporter=NULL_REPORTER) -> DemResult:
        import rasterio  # noqa: PLC0415
        s, w, n, e = _pad(bbox)
        lat0 = (s + n) / 2.0
        res = max(float(target_res_m), 1.0)
        wpx = int(min(self.MAX_PX, max(64, (e - w) * 111_320 * math.cos(math.radians(lat0)) / res)))
        hpx = int(min(self.MAX_PX, max(64, (n - s) * 111_320 / res)))
        key = f"3dep|{s:.5f},{w:.5f},{n:.5f},{e:.5f}|{wpx}x{hpx}"
        cache = DiskCache("usgs3dep", ttl_s=365 * DAY)
        raw = cache.get_bytes(key, ".tif")
        if raw is None:
            reporter.step("Downloading USGS 3DEP elevation ...")
            resp = get(self.URL, params=dict(
                bbox=f"{w},{s},{e},{n}", bboxSR=4326, imageSR=4326,
                size=f"{wpx},{hpx}", format="tiff", pixelType="F32",
                interpolation="RSP_BilinearInterpolation", f="image"),
                timeout=90, reporter=reporter, what="USGS 3DEP")
            raw = resp.content
            if not resp.headers.get("content-type", "").startswith("image/tiff"):
                raise ElevationUnavailable("USGS 3DEP did not return an image")
        with rasterio.MemoryFile(raw) as mf, mf.open() as ds:
            a = ds.read(1).astype(np.float64)
            t = ds.transform
            nodata = ds.nodata
        a[(a < -1000) | (a > 9000)] = np.nan
        if nodata is not None:
            a[a == nodata] = np.nan
        if np.isnan(a).mean() > 0.4:
            raise ElevationUnavailable("USGS 3DEP has no data for most of this area")
        cache.put_bytes(key, raw, ".tif")
        dem = DemRaster(a, "geographic", t.c + t.a / 2, t.f + t.e / 2, t.a, t.e)
        return DemResult(dem, self.info, dem.native_res_m(lat0))


# ---------------------------------------------------------------------------
# Copernicus GLO-30 -- public AWS bucket, one GeoTIFF per 1x1 degree cell
# ---------------------------------------------------------------------------

class CopernicusGlo30:
    info = SourceInfo(
        "glo30", "Copernicus GLO-30", "30 m", "DSM",
        "free, attribution required (Copernicus DEM licence)",
        "Elevation: Copernicus DEM GLO-30 (c) DLR e.V. 2010-2014 and (c) Airbus "
        "Defence and Space GmbH 2014-2018, provided under COPERNICUS by the "
        "European Union and ESA")
    URL = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"

    def covers(self, bbox) -> bool:
        return True            # global; the few withheld cells raise on fetch

    @staticmethod
    def _cell_name(lat: int, lon: int) -> str:
        ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
        return (f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_"
                f"{ew}{abs(lon):03d}_00_DEM")

    def _cell_file(self, lat, lon, reporter):
        """Local path of the cell's GeoTIFF (downloading it once), or None if
        the cell does not exist -- open sea has no file."""
        name = self._cell_name(lat, lon)
        cache = DiskCache("glo30")
        if cache.has(name, ".tif"):
            return cache.path(name, ".tif")
        if cache.has(name, ".missing"):
            return None
        reporter.step(f"Downloading Copernicus GLO-30 cell {name[22:-4]} "
                      "(once; cached afterwards) ...")
        try:
            resp = get(self.URL.format(name=name), timeout=180, reporter=reporter,
                       what="Copernicus GLO-30", ok_statuses=(200, 404),
                       give_up_statuses=(403,))
        except NetworkError as exc:
            raise ElevationUnavailable(str(exc)) from exc
        if resp.status_code == 404:
            cache.put_bytes(name, b"", ".missing")
            return None
        return cache.put_bytes(name, resp.content, ".tif")

    def fetch(self, bbox, target_res_m, reporter=NULL_REPORTER) -> DemResult:
        import rasterio  # noqa: PLC0415
        from rasterio.windows import Window  # noqa: PLC0415
        s, w, n, e = _pad(bbox)
        rasters, missing = [], 0
        for lat in range(math.floor(s), math.floor(n) + 1):
            for lon in range(math.floor(w), math.floor(e) + 1):
                path = self._cell_file(lat, lon, reporter)
                if path is None:
                    missing += 1
                    continue
                with rasterio.open(path) as ds:
                    t = ds.transform
                    c0 = max(int(math.floor((w - t.c) / t.a)) - 4, 0)
                    c1 = min(int(math.ceil((e - t.c) / t.a)) + 4, ds.width)
                    r0 = max(int(math.floor((n - t.f) / t.e)) - 4, 0)
                    r1 = min(int(math.ceil((s - t.f) / t.e)) + 4, ds.height)
                    if c1 <= c0 or r1 <= r0:
                        continue
                    a = ds.read(1, window=Window(c0, r0, c1 - c0, r1 - r0))
                rasters.append(DemRaster(
                    a.astype(np.float64), "geographic",
                    t.c + (c0 + 0.5) * t.a, t.f + (r0 + 0.5) * t.e, t.a, t.e))
        if not rasters:
            raise ElevationUnavailable("Copernicus GLO-30 has no cells here "
                                       "(open sea, or a withheld region)")
        dem = rasters[0] if len(rasters) == 1 and not missing else DemMosaic(rasters)
        return DemResult(dem, self.info, 30.0)


# ---------------------------------------------------------------------------
# AWS Terrain Tiles ("Terrarium") -- keyless PNG tiles, global
# ---------------------------------------------------------------------------

class Terrarium:
    info = SourceInfo(
        "terrarium", "AWS Terrain Tiles (Mapzen)", "30 m SRTM globally; 2-10 m "
        "in the UK, Austria, Norway, New Zealand, USA", "mixed",
        "open data, attribution required",
        "Elevation: Mapzen / AWS Terrain Tiles -- SRTM, 3DEP, EU-DEM, data.gov.uk, "
        "data.gv.at, Kartverket, LINZ, ArcticDEM, GMTED, ETOPO1")
    URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
    # where these tiles carry a national bare-earth model finer than 30 m
    FINE_REGIONS = ((49.9, -5.8, 55.8, 1.8),       # England / Wales (2 m)
                    (46.35, 9.5, 49.05, 17.2),     # Austria (10 m)
                    (57.9, 4.4, 71.3, 31.2),       # Norway (10 m)
                    (-47.5, 166.0, -34.0, 179.0))  # New Zealand (8 m)
    MAX_TILES = 120
    ZMAX = 15

    def covers(self, bbox) -> bool:
        return bbox[0] > -85 and bbox[2] < 85

    def is_fine_here(self, bbox) -> bool:
        return any(_in(bbox, r) for r in self.FINE_REGIONS)

    @staticmethod
    def _tile_xy(lat, lon, z):
        n = 2 ** z
        return ((lon + 180.0) / 360.0 * n,
                (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)

    def _tile(self, z, x, y, cache, reporter):
        from PIL import Image  # noqa: PLC0415
        key = f"{z}/{x}/{y}"
        raw = cache.get_bytes(key, ".png")
        if raw is None:
            raw = get(self.URL.format(z=z, x=x, y=y), timeout=30, reporter=reporter,
                      what="AWS terrain tile").content
            cache.put_bytes(key, raw, ".png")
        a = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.float64)
        return a[:, :, 0] * 256.0 + a[:, :, 1] + a[:, :, 2] / 256.0 - 32768.0

    def fetch(self, bbox, target_res_m, reporter=NULL_REPORTER) -> DemResult:
        s, w, n, e = _pad(bbox)
        lat0 = (s + n) / 2.0
        ground = 156_543.034 * math.cos(math.radians(lat0))      # m / px at z0
        zmax = self.ZMAX if self.is_fine_here(bbox) else 14
        z = int(min(zmax, max(0, math.ceil(math.log2(ground / max(target_res_m, 0.5))))))
        while True:
            x0f, y0f = self._tile_xy(n, w, z)
            x1f, y1f = self._tile_xy(s, e, z)
            tx0, tx1 = int(math.floor(x0f)), int(math.floor(x1f))
            ty0, ty1 = int(math.floor(y0f)), int(math.floor(y1f))
            cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
            if cols * rows <= self.MAX_TILES or z == 0:
                break
            z -= 1
        cache = DiskCache("terrarium", ttl_s=365 * DAY)
        jobs = [(i, j) for j in range(rows) for i in range(cols)]
        mosaic = np.full((rows * 256, cols * 256), np.nan)
        ntiles, done, failed = 2 ** z, 0, 0

        def grab(ij):
            i, j = ij
            try:
                return i, j, self._tile(z, (tx0 + i) % ntiles, ty0 + j, cache, reporter)
            except NetworkError as exc:
                logger.warning("terrain tile %s/%s/%s: %s", z, tx0 + i, ty0 + j, exc)
                return i, j, None

        with cf.ThreadPoolExecutor(max_workers=8) as pool:
            for i, j, tile in pool.map(grab, jobs):
                done += 1
                if tile is None:
                    failed += 1
                else:
                    mosaic[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256] = tile
                reporter.step(f"Elevation tiles {done}/{len(jobs)} ...",
                              done / len(jobs), log=False)
        if failed > len(jobs) * 0.25:
            raise ElevationUnavailable(
                f"{failed} of {len(jobs)} AWS terrain tiles could not be downloaded")
        dem = DemRaster(mosaic, "webmercator", tx0 * 256 + 0.5, ty0 * 256 + 0.5,
                        1.0, 1.0, zoom=z)
        native = dem.native_res_m(lat0)
        bare_earth = self.is_fine_here(bbox) or Usgs3dep().covers(bbox)
        if not bare_earth:
            native = max(native, 30.0)          # SRTM underneath, whatever the zoom
        info = replace(self.info, kind="DTM" if bare_earth else "DSM")
        return DemResult(dem, info, native)


# ---------------------------------------------------------------------------
# OpenTopography -- optional, needs a (free) API key
# ---------------------------------------------------------------------------

class OpenTopography:
    URL = "https://portal.opentopography.org/API/globaldem"

    def __init__(self, api_key: str, demtype: str = "COP30"):
        self.api_key, self.demtype = api_key.strip(), demtype
        self.info = SourceInfo(
            "opentopography", f"OpenTopography ({demtype})", "30 m", "DSM",
            "per dataset; free API key required",
            f"Elevation: {demtype} via OpenTopography")

    def covers(self, bbox) -> bool:
        return bool(self.api_key)

    def fetch(self, bbox, target_res_m, reporter=NULL_REPORTER) -> DemResult:
        import rasterio  # noqa: PLC0415
        s, w, n, e = _pad(bbox, min_deg=0.003)
        key = f"{self.demtype}|{s:.4f},{w:.4f},{n:.4f},{e:.4f}"
        cache = DiskCache("opentopography", ttl_s=365 * DAY)
        raw = cache.get_bytes(key, ".tif")
        if raw is None:
            reporter.step("Downloading elevation from OpenTopography ...")
            try:
                raw = get(self.URL, params=dict(
                    demtype=self.demtype, south=s, north=n, west=w, east=e,
                    outputFormat="GTiff", API_Key=self.api_key),
                    timeout=120, reporter=reporter, what="OpenTopography",
                    give_up_statuses=(400, 401, 403, 404)).content
            except NetworkError as exc:
                raise ElevationUnavailable(
                    f"{exc} -- check the API key in Settings") from exc
        try:
            with rasterio.MemoryFile(raw) as mf, mf.open() as ds:
                a, t = ds.read(1).astype(np.float64), ds.transform
        except Exception as exc:  # noqa: BLE001
            raise ElevationUnavailable(f"OpenTopography reply unreadable: {exc}") from exc
        cache.put_bytes(key, raw, ".tif")
        a[a < -1000] = np.nan
        dem = DemRaster(a, "geographic", t.c + t.a / 2, t.f + t.e / 2, t.a, t.e)
        return DemResult(dem, self.info, 30.0)


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------

PROVIDER_CHOICES = ("auto", "usgs3dep", "glo30", "terrarium", "opentopography")


def provider_chain(bbox, prefer: str = "auto", opentopo_key: str = "") -> list:
    usgs, glo, terr = Usgs3dep(), CopernicusGlo30(), Terrarium()
    if terr.is_fine_here(bbox):
        auto = [terr, glo]
    else:
        auto = [glo, terr]
    if usgs.covers(bbox):
        auto.insert(0, usgs)
    named = {"usgs3dep": usgs, "glo30": glo, "terrarium": terr}
    if opentopo_key:
        named["opentopography"] = OpenTopography(opentopo_key)
    first = named.get(prefer)
    if first is None:
        return auto
    return [first] + [p for p in auto if p.info.key != first.info.key]


def fetch_dem(bbox: Sequence[float], target_res_m: float, *, prefer: str = "auto",
              opentopo_key: str = "", reporter: Reporter = NULL_REPORTER) -> DemResult:
    """The best elevation we can get for ``bbox``, falling back down the chain.
    Raises :class:`ElevationUnavailable` only when every provider failed."""
    problems = []
    for prov in provider_chain(bbox, prefer, opentopo_key):
        if not prov.covers(bbox):
            continue
        reporter.step(f"Elevation: trying {prov.info.title} ...")
        try:
            res = prov.fetch(bbox, target_res_m, reporter)
        except (ElevationUnavailable, NetworkError) as exc:
            logger.warning("elevation: %s failed -- %s", prov.info.title, exc)
            problems.append(f"{prov.info.title}: {exc}")
            continue
        except ImportError as exc:
            problems.append(f"{prov.info.title}: needs the 'rasterio' package ({exc})")
            continue
        if problems:
            res.notes = "fell back after: " + "; ".join(problems)
        logger.info("elevation: %s (%s, ~%.0f m native)", res.source.title,
                    res.source.kind, res.native_res_m)
        return res
    raise ElevationUnavailable(
        "No elevation source could be reached:\n  " + "\n  ".join(problems))


class FlatDem:
    """Stand-in when terrain is switched off (or for tests)."""

    def __init__(self, height: float = 0.0):
        self.height = height

    def sample(self, lat, lon, order: int = 3):
        return np.full(np.shape(lat), float(self.height))

    def native_res_m(self, lat):
        return 1.0


FLAT_INFO = SourceInfo("flat", "Flat (terrain off)", "-", "-", "-", "")
