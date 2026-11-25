"""Prepare a private render-skinning input from exported ZZMI buffers."""

import numpy as np

from ..utils.format_utils import FormatUtils

POSITION_STRIDE = 40
BLEND_STRIDE = 32
BLEND_LAYOUT_MARKER = "; SSMT private blend layout "
NATIVE_SLOT_COUNT_MARKER = "; SSMT native bone slot count = "
_POSITION_LAYOUT = (
    ("POSITION", "R32G32B32_FLOAT", 0),
    ("NORMAL", "R32G32B32_FLOAT", 12),
    ("TANGENT", "R32G32B32A32_FLOAT", 24),
)


def compatible_position(layout: dict | None) -> bool:
    if not layout or layout["stride"] != POSITION_STRIDE:
        return False
    return tuple(
        (element["semantic"], element["format"], element["offset"])
        for element in layout["elements"]
    ) == _POSITION_LAYOUT


def normalized_blend(payload: bytes, layout: dict | None) -> bytes | None:
    """Return float4 weights and uint4 global bone slots per exported vertex."""
    if not layout or layout["stride"] <= 0 or len(payload) % layout["stride"]:
        return None
    rows = len(payload) // layout["stride"]
    weights_by_channel = {}
    indices_by_channel = {}
    for element in layout["elements"]:
        semantic = element["semantic"]
        if semantic not in {"BLENDINDICES", "BLENDWEIGHT", "BLENDWEIGHTS"}:
            continue
        raw_type = np.dtype(FormatUtils.get_nptype_from_format(element["format"]))
        channels = element["byte_width"] // raw_type.itemsize
        if channels <= 0 or element["offset"] + element["byte_width"] > layout["stride"]:
            return None
        values = np.ndarray(
            (rows, channels), dtype=raw_type, buffer=payload,
            offset=element["offset"], strides=(layout["stride"], raw_type.itemsize),
        )
        if semantic == "BLENDINDICES":
            if raw_type.kind not in "iu":
                return None
            indices_by_channel[element["index"]] = values
        else:
            weights_by_channel[element["index"]] = FormatUtils.apply_format_conversion(values, element["format"])
    if not indices_by_channel or weights_by_channel.keys() - indices_by_channel.keys():
        return None
    packed = np.zeros(rows, dtype=[("weights", "<f4", 4), ("indices", "<u4", 4)])
    offset = 0
    for index, ids in sorted(indices_by_channel.items()):
        weights = weights_by_channel.get(index)
        if weights is None and not weights_by_channel:
            weights = np.zeros(ids.shape, dtype=np.float32)
            weights[:, 0] = 1.0
        end = offset + ids.shape[1]
        if weights is None or weights.shape != ids.shape or end > 4:
            return None
        if not np.isfinite(weights).all() or ((weights > 0) & (ids < 0)).any():
            return None
        packed["weights"][:, offset:end] = weights
        packed["indices"][:, offset:end] = ids
        offset = end
    return packed.tobytes()


def weighted_slots(packed, minimum=0):
    rows = np.frombuffer(packed, dtype=[("weights", "<f4", 4), ("slots", "<u4", 4)])
    slots = rows["slots"][(rows["weights"] > 0) & (rows["slots"] >= minimum)]
    return {int(slot) for slot in np.unique(slots)}


def extra_weighted_slots(packed, native_capacity):
    return weighted_slots(packed, native_capacity)


def remap_blend_indices(payload, layout, native_capacity, fallback):
    """Keep the game's original blend format and weights for ordinary deformation."""
    result = bytearray(payload)
    stride = layout["stride"]
    for element in layout["elements"]:
        if element["semantic"] != "BLENDINDICES":
            continue
        dtype = np.dtype(FormatUtils.get_nptype_from_format(element["format"]))
        values = np.ndarray(
            (len(payload) // stride, element["byte_width"] // dtype.itemsize),
            dtype=dtype, buffer=result, offset=element["offset"], strides=(stride, dtype.itemsize),
        )
        mask = values >= native_capacity
        slots = values[mask].astype(np.int64) - native_capacity
        parents = np.zeros(slots.shape, dtype=dtype)
        valid = slots < len(fallback)
        resolved = np.asarray(fallback, dtype="<u4")[slots[valid]]
        parents[valid] = np.where(resolved < native_capacity, resolved, 0)
        values[mask] = parents
    return bytes(result)


def ground_support_vertices(positions, blends, anchor):
    """Sample low bind vertices and keep their original four skin influences."""
    vertices = np.frombuffer(positions, dtype="<f4").reshape(-1, POSITION_STRIDE // 4)[:, :3]
    influences = np.frombuffer(blends, dtype=[("weights", "<f4", 4), ("slots", "<u4", 4)])
    if not len(vertices) or len(vertices) != len(influences):
        return None
    if not np.isfinite(vertices).all() or not np.isfinite(influences["weights"]).all():
        return None
    weighted = (influences["weights"] > 0).any(axis=1)
    if not weighted.any():
        return None
    low, high = np.min(vertices[weighted, 2]), np.max(vertices[weighted, 2])
    height = float(high - low)
    if height <= 1e-6:
        return None
    candidates = np.flatnonzero(weighted & (vertices[:, 2] <= low + height * 0.06))
    # Keep the lowest vertex in each horizontal cell across the shoe soles.
    cells = np.floor(vertices[candidates, :2] / (height / 100)).astype(np.int64)
    ascending = np.argsort(vertices[candidates, 2], kind="stable")
    unique = np.unique(cells[ascending], axis=0, return_index=True)[1]
    selected = candidates[ascending[np.sort(unique)][:256]]
    samples = np.empty(len(selected), dtype=[
        ("position", "<f4", 4), ("weights", "<f4", 4), ("slots", "<u4", 4),
    ])
    samples["position"][:, :3] = vertices[selected]
    samples["position"][:, 3] = 1.0
    samples["weights"] = influences["weights"][selected]
    samples["slots"] = influences["slots"][selected]
    owns_anchor = bool(((influences["slots"] == anchor) & (influences["weights"] > 0)).any())
    return (owns_anchor, height, len(vertices)), samples.tobytes()
