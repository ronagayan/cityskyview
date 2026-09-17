"""One binary STL per component, into a folder."""

from __future__ import annotations

from pathlib import Path

from ..log import logger
from ..model import slug


def write_stl_folder(folder, components, stem: str = "model") -> list:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for c in components:
        if c.mesh is None or len(c.mesh.faces) < 4:
            continue
        p = folder / f"{slug(stem)}__{slug(c.name)}.stl"
        c.mesh.export(p, file_type="stl")
        written.append(p)
    logger.info("STL folder written: %s (%d files)", folder, len(written))
    return written
