"""Optional height supplement from Overture Maps.

Overture's building theme conflates OSM with the Microsoft, Google and Esri
ML footprints, and carries a ``height`` wherever any of them has one. We only
use it for what OSM lacks: a building with no ``height`` / ``building:levels``
tag borrows the height of the Overture footprint it overlaps.

Needs the ``overturemaps`` package (pulls in pyarrow). The first download for
an area takes a minute or two -- it scans cloud GeoParquet -- so it is off by
default and the result is cached.
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..log import logger
from ..progress import NULL_REPORTER, Reporter
from .cache import DAY, DiskCache
from .overpass import snap_bbox


def available() -> bool:
    try:
        import overturemaps  # noqa: F401, PLC0415
        return True
    except Exception:  # noqa: BLE001
        return False


def fetch_heights(bbox: Sequence[float], *, reporter: Reporter = NULL_REPORTER,
                  cache: Optional[DiskCache] = None) -> list:
    """``[{"lat", "lon", "height", "floors", "source"}, ...]`` -- one entry per
    Overture building that has a height or a floor count, located by an
    interior point. Returns [] if the package is missing or the download fails;
    this data is a bonus, never a reason to stop."""
    if not available():
        logger.info("Overture: 'overturemaps' is not installed -- skipping")
        return []
    cache = cache or DiskCache("overture", ttl_s=60 * DAY)
    s, w, n, e = snap_bbox(bbox)
    key = f"v1|{s:.6f},{w:.6f},{n:.6f},{e:.6f}"
    hit = cache.get_json(key)
    if hit is not None:
        logger.info("Overture: cached heights for this area (%d buildings)", len(hit))
        return hit
    reporter.step("Downloading Overture Maps building heights (slow the first "
                  "time, cached afterwards) ...")
    try:
        import shapely  # noqa: PLC0415
        from overturemaps import core  # noqa: PLC0415
        reader = core.record_batch_reader("building", bbox=(w, s, e, n))
        out = []
        for batch in reader:
            reporter.check()
            cols = batch.to_pydict()
            for geom, h, fl, srcs in zip(cols["geometry"], cols["height"],
                                         cols["num_floors"], cols["sources"]):
                if not h and not fl:
                    continue
                try:
                    pt = shapely.from_wkb(geom).representative_point()
                except Exception:  # noqa: BLE001
                    continue
                src = (srcs[0].get("dataset") if srcs else "") or ""
                out.append({"lat": pt.y, "lon": pt.x, "height": h or 0.0,
                            "floors": fl or 0, "source": src})
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "Cancelled":
            raise
        logger.warning("Overture download failed (%s) -- continuing without it", exc)
        return []
    cache.put_json(key, out)
    logger.info("Overture: %d buildings with a height or floor count", len(out))
    return out
