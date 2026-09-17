"""3MF writer: every component is its own mesh object.

Two layouts:

``parts``    (default) one build item -- an assembly object whose
             ``<components>`` reference the component meshes. Bambu Studio,
             OrcaSlicer and PrusaSlicer open this as ONE object with several
             parts that stay locked together, each assignable to its own
             filament. Cura opens it as a group.
``objects``  one build item per component. Slicers show separate objects
             (Bambu/Orca ask whether to treat them as parts of one object).

Only the 3MF core specification is used (plus base-material display colours),
so any compliant reader can open the file.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import numpy as np

from ..log import logger

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '</Types>')
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    '</Relationships>')


def _mesh_xml(out: io.StringIO, vertices: np.ndarray, faces: np.ndarray):
    out.write("<mesh><vertices>")
    out.write("".join('<vertex x="%.4f" y="%.4f" z="%.4f"/>' % tuple(v)
                      for v in np.asarray(vertices, dtype=np.float64).tolist()))
    out.write("</vertices><triangles>")
    out.write("".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(f)
                      for f in np.asarray(faces, dtype=np.int64).tolist()))
    out.write("</triangles></mesh>")


def write_3mf(path, components, layout: str = "parts", title: str = "CityModel",
              metadata: dict | None = None) -> Path:
    """``components``: objects with ``.name``, ``.mesh`` (trimesh) and
    ``.color`` (r, g, b). Returns the path written."""
    comps = [c for c in components if c.mesh is not None and len(c.mesh.faces) >= 4]
    if not comps:
        raise ValueError("nothing to export -- every component is empty")
    path = Path(path)
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write(f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}">')
    out.write(f'<metadata name="Title">{_esc(title)}</metadata>')
    out.write('<metadata name="Application">CityModel</metadata>')
    for k, v in (metadata or {}).items():
        out.write(f'<metadata name={quoteattr(str(k))}>{_esc(v)}</metadata>')
    out.write('<resources><basematerials id="1">')
    for c in comps:
        r, g, b = (int(max(0, min(255, v))) for v in c.color)
        out.write(f'<base name={quoteattr(c.name)} displaycolor="#{r:02X}{g:02X}{b:02X}FF"/>')
    out.write('</basematerials>')
    ids = []
    for i, c in enumerate(comps):
        oid = i + 2
        ids.append(oid)
        out.write(f'<object id="{oid}" name={quoteattr(c.name)} type="model" '
                  f'pid="1" pindex="{i}">')
        _mesh_xml(out, c.mesh.vertices, c.mesh.faces)
        out.write('</object>')
    if layout == "parts":
        assembly = len(comps) + 2
        out.write(f'<object id="{assembly}" name={quoteattr(title)} type="model"><components>')
        out.write("".join(f'<component objectid="{oid}"/>' for oid in ids))
        out.write('</components></object></resources>')
        out.write(f'<build><item objectid="{assembly}"/></build>')
    else:
        out.write('</resources><build>')
        out.write("".join(f'<item objectid="{oid}"/>' for oid in ids))
        out.write('</build>')
    out.write('</model>')

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".3mf.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", out.getvalue())
    tmp.replace(path)
    logger.info("3MF written: %s (%d components, layout=%s)", path, len(comps), layout)
    return path


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def read_3mf_summary(path) -> dict:
    """Re-open a 3MF and describe it -- used to verify exports.
    ``{"objects": [{"id", "name", "vertices", "triangles", "color"}],
       "build_items": [...], "assembly": [...component object ids...]}``"""
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    ns = {"m": CORE_NS}
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("3D/3dmodel.model"))
    colors = [b.get("displaycolor") for b in root.findall(".//m:basematerials/m:base", ns)]
    objects, assembly = [], []
    for o in root.findall("./m:resources/m:object", ns):
        comp = o.findall("./m:components/m:component", ns)
        if comp:
            assembly = [int(c.get("objectid")) for c in comp]
            continue
        pidx = o.get("pindex")
        objects.append({
            "id": int(o.get("id")), "name": o.get("name"),
            "vertices": len(o.findall("./m:mesh/m:vertices/m:vertex", ns)),
            "triangles": len(o.findall("./m:mesh/m:triangles/m:triangle", ns)),
            "color": colors[int(pidx)] if pidx is not None and int(pidx) < len(colors) else None})
    items = [int(i.get("objectid")) for i in root.findall("./m:build/m:item", ns)]
    return {"unit": root.get("unit"), "objects": objects, "build_items": items,
            "assembly": assembly}
