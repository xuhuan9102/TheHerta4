from dataclasses import dataclass
import json
import os

import numpy


ATTRIBUTE_BUFFER_TYPE = "ShapeKeyVertexAttributeOffset"
ATTRIBUTE_CHANNEL_COUNT = 9


@dataclass(frozen=True)
class NativeShapeKeyAttributeDelta:
    vertex_ids: numpy.ndarray
    deltas: numpy.ndarray


@dataclass(frozen=True)
class NativeShapeKeyAttributes:
    by_name: dict
    has_non_position_deltas: bool


def _position_semantic_float_offsets(game_type):
    offsets = {}
    byte_offset = 0
    for element in getattr(game_type, "D3D11ElementList", []) or []:
        if str(getattr(element, "Category", "") or "").upper() != "POSITION":
            continue
        semantic = str(getattr(element, "SemanticName", "") or "").upper()
        byte_width = int(getattr(element, "ByteWidth", 0) or 0)
        if semantic in {"POSITION", "NORMAL", "TANGENT"} and byte_width >= 12:
            offsets[semantic] = byte_offset // 4
        byte_offset += byte_width
    return offsets


def apply_native_shapekey_delta(
    base_bytes,
    target_bytes,
    export_indices,
    vertex_stride,
    game_type,
    native_delta,
    source_vertex_ids=None,
):
    """Apply one sparse native morph delta to DrawIB rows in place.

    A merged ZZMI draw can contain several submeshes that all reference the same
    full vertex pool. Re-sampling each Blender copy independently can therefore
    apply the same sparse morph at shifted local vertex ids. The native record
    already uses DrawIB-global vertex ids, so it is the authoritative source for
    both Position and optional Normal/Tangent deltas.
    """
    if (
        native_delta is None
        or vertex_stride <= 0
        or vertex_stride % 4 != 0
        or len(base_bytes) % vertex_stride != 0
        or len(target_bytes) % vertex_stride != 0
    ):
        return target_bytes

    semantic_offsets = _position_semantic_float_offsets(game_type)
    if "POSITION" not in semantic_offsets:
        return target_bytes

    base_rows = numpy.frombuffer(base_bytes, dtype=numpy.uint8).reshape(-1, vertex_stride)
    target_rows = numpy.frombuffer(target_bytes, dtype=numpy.uint8).reshape(-1, vertex_stride).copy()
    base_float = base_rows.view(numpy.float32)
    target_float = target_rows.view(numpy.float32)
    target_vertex_ids = (
        numpy.asarray(source_vertex_ids, dtype=numpy.int64)
        if source_vertex_ids is not None
        else numpy.asarray(export_indices, dtype=numpy.int64)
    )
    vertex_count = len(base_bytes) // vertex_stride
    if target_vertex_ids.size != vertex_count:
        return target_bytes
    rows_by_vertex_id = {}
    for local_index, vertex_id in enumerate(target_vertex_ids.tolist()):
        rows_by_vertex_id.setdefault(int(vertex_id), []).append(local_index)
    channels = (
        (semantic_offsets.get("POSITION"), 0),
        (semantic_offsets.get("NORMAL"), 3),
        (semantic_offsets.get("TANGENT"), 6),
    )
    changed = False
    seen_vertex_ids = set()
    for vertex_id, delta in zip(native_delta.vertex_ids, native_delta.deltas):
        local_indices = rows_by_vertex_id.get(int(vertex_id))
        if not local_indices:
            continue
        is_first = int(vertex_id) not in seen_vertex_ids
        seen_vertex_ids.add(int(vertex_id))
        for local_index in local_indices:
            for channel_offset, values_offset in channels:
                if channel_offset is None or channel_offset + 3 > target_float.shape[1]:
                    continue
                values = delta[values_offset:values_offset + 3]
                if is_first:
                    target_float[local_index, channel_offset:channel_offset + 3] = (
                        base_float[local_index, channel_offset:channel_offset + 3] + values
                    )
                else:
                    target_float[local_index, channel_offset:channel_offset + 3] += values
                changed = True
    return target_rows.tobytes() if changed else target_bytes


def _find_buffer_file(category_buffers, buffer_type):
    for buffer in category_buffers or []:
        if str(buffer.get("Type", "") or "") == buffer_type:
            file_name = str(buffer.get("FileName", "") or "").strip()
            if file_name:
                return file_name
    return ""


def load_native_shapekey_attributes(json_path: str) -> NativeShapeKeyAttributes:
    if not json_path or not os.path.exists(json_path):
        return NativeShapeKeyAttributes({}, False)
    try:
        with open(json_path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except (OSError, ValueError, TypeError):
        return NativeShapeKeyAttributes({}, False)

    shape_keys_info = payload.get("ShapeKeysInfo", {}) or {}
    if int(shape_keys_info.get("vertex_attribute_channel_count", 0) or 0) != ATTRIBUTE_CHANNEL_COUNT:
        return NativeShapeKeyAttributes({}, False)

    buffers = payload.get("CategoryBufferList", []) or []
    folder = os.path.dirname(json_path)
    offsets_path = os.path.join(folder, _find_buffer_file(buffers, "ShapeKeyOffset"))
    ids_path = os.path.join(folder, _find_buffer_file(buffers, "ShapeKeyVertexId"))
    attributes_path = os.path.join(folder, _find_buffer_file(buffers, ATTRIBUTE_BUFFER_TYPE))
    if not all(
        path and os.path.exists(path)
        for path in (offsets_path, ids_path, attributes_path)
    ):
        return NativeShapeKeyAttributes({}, False)

    try:
        offsets = numpy.fromfile(offsets_path, dtype=numpy.uint32)
        vertex_ids = numpy.fromfile(ids_path, dtype=numpy.uint32)
        values = numpy.fromfile(attributes_path, dtype=numpy.float32).reshape(
            -1, ATTRIBUTE_CHANNEL_COUNT
        )
    except (OSError, ValueError):
        return NativeShapeKeyAttributes({}, False)
    if len(offsets) < 2 or len(vertex_ids) != len(values):
        return NativeShapeKeyAttributes({}, False)

    by_name = {}
    has_non_position = False
    for shape_key_index in range(len(offsets) - 1):
        start = int(offsets[shape_key_index])
        end = min(int(offsets[shape_key_index + 1]), len(vertex_ids), len(values))
        if start < 0 or end <= start:
            continue
        deltas = values[start:end]
        by_name[f"Deform {shape_key_index}"] = NativeShapeKeyAttributeDelta(
            vertex_ids[start:end].copy(),
            deltas.copy(),
        )
        if numpy.any(numpy.abs(deltas[:, 3:]) > 1e-8):
            has_non_position = True
    return NativeShapeKeyAttributes(by_name, has_non_position)
