"""Build a reference-position map in the primary texture coordinates of a draw."""

import re
from pathlib import Path

import numpy as np

from .drag_cloth import _field

MASK_SIZE = 1024
UV_MARKER = "; SSMT tear TEXCOORD0 = "
UV_FORMATS = {"R16G16_FLOAT": "<f2", "R32G32_FLOAT": "<f4"}


def uv_annotation(game_type):
    offset = 0
    for element in getattr(game_type, "D3D11ElementList", ()):
        if element.Category != "Texcoord":
            continue
        if element.ElementName == "TEXCOORD":
            if element.Format in UV_FORMATS:
                return f"{UV_MARKER}{offset}, {element.Format}"
            return None
        offset += element.ByteWidth
    return None


def read_uv(root, sections, comp, part, vertex_count):
    lines = sections[part["section"]]
    layout = next((line.strip()[len(UV_MARKER):] for line in lines if line.strip().startswith(UV_MARKER)), "")
    match = re.fullmatch(r"(\d+),\s*(\w+)", layout)
    if not match or match[2] not in UV_FORMATS:
        return None
    override = _field(lines, "vb1")
    name = override or f"Resource{comp['comp_name']}Texcoord"
    resource = sections.get(f"[{name}]", ())
    filename = _field(resource, "filename")
    if not filename:
        return None
    dtype = np.dtype(UV_FORMATS[match[2]])
    try:
        stride = int(_field(resource, "stride") or 0)
        data = Path(root, filename.replace("\\", "/")).read_bytes()
    except (OSError, ValueError):
        return None
    offset = int(match[1])
    if stride < offset + 2 * dtype.itemsize or len(data) % stride:
        return None
    rows = len(data) // stride
    base = int(part["vertex_base"])
    # A redirected vb1 carries the same prefix as the skinned vertex buffer.
    first = base if name.casefold().startswith("resourcezzredirecttexcoord_") else 0
    if rows < first + vertex_count:
        return None
    return np.ndarray((vertex_count, 2), dtype, buffer=data, offset=first * stride + offset,
                      strides=(stride, dtype.itemsize)).astype(np.float32)


def rasterize_reference(positions, uv, indices, size=MASK_SIZE):
    """Interpolate bind positions at texel centres, preserving the triangle's UV edges."""
    atlas = np.zeros((size, size, 4), dtype="<f2")
    for indices3 in indices:
        texcoord = uv[indices3]
        points = positions[indices3]
        if not np.isfinite(texcoord).all() or not np.isfinite(points).all():
            continue
        # Keep triangles crossing a wrap seam continuous before sampling tiles.
        start_tile = np.floor(texcoord.min(axis=0)).astype(int)
        end_tile = np.ceil(texcoord.max(axis=0)).astype(int)
        if np.any(end_tile - start_tile > 2):
            continue
        first, second = texcoord[1:] - texcoord[0]
        det = first[0] * second[1] - first[1] * second[0]
        if abs(det) < 1e-10:
            continue
        for tile_y in range(start_tile[1], end_tile[1]):
            for tile_x in range(start_tile[0], end_tile[0]):
                projected = (texcoord - (tile_x, tile_y)) * size - 0.5
                x0, y0 = np.maximum(np.ceil(projected.min(axis=0)).astype(int), 0)
                x1, y1 = np.minimum(np.floor(projected.max(axis=0)).astype(int) + 1, size)
                if x1 <= x0 or y1 <= y0:
                    continue
                x = (np.arange(x0, x1) - projected[0, 0])[None, :] / size
                y = (np.arange(y0, y1) - projected[0, 1])[:, None] / size
                u = (x * second[1] - y * second[0]) / det
                v = (y * first[0] - x * first[1]) / det
                inside = (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6)
                result = atlas[y0:y1, x0:x1]
                local = points[0] + u[inside, None] * (points[1] - points[0]) + v[inside, None] * (points[2] - points[0])
                result[inside, :3] = local
                result[inside, 3] = 1
    return atlas
