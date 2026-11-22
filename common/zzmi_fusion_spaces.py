
"""Prepare shared ZZMI indices and per-layout UV streams for native rendering."""

import numpy as np


def count_imported_indices(path, declared_count):
    """Match mesh.validate's removal of repeated and degenerate triangles."""
    try:
        indices = np.fromfile(path, dtype="<u4")
    except OSError:
        return None
    if len(indices) != declared_count or len(indices) % 3:
        return None
    triangles = np.sort(indices.reshape(-1, 3), axis=1)
    nondegenerate = (triangles[:, 0] != triangles[:, 1]) & (triangles[:, 1] != triangles[:, 2])
    return len(np.unique(triangles[nondegenerate], axis=0)) * 3


def partition_triangles(blend_bytes, indices, bone_spaces):
    packed = np.frombuffer(blend_bytes, dtype="<u4").reshape(-1, 8)
    weights = packed[:, :4].copy().view("<f4")
    bones = packed[:, 4:]
    slot_space = np.full(max(bone_spaces) + 1, -1, dtype=np.int32)
    for slot, space in bone_spaces.items():
        slot_space[slot] = space

    positive = weights > 0
    if (bones[positive] >= len(slot_space)).any():
        raise ValueError("权重引用了骨骼池以外的槽位")
    influences = slot_space[np.where(positive, bones, 0)]
    if (influences[positive] < 0).any():
        raise ValueError("部分骨骼没有对应的绘制空间")

    triangles = np.asarray(indices, dtype="<u4").reshape(-1, 3)
    spaces = influences[triangles].reshape(-1, 12)
    used = positive[triangles].reshape(-1, 12)
    high = np.max(np.where(used, spaces, -1), axis=1)
    low = np.min(np.where(used, spaces, len(bone_spaces)), axis=1)
    if (low != high).any():
        raise ValueError("存在无权重或权重跨越原生绘制空间的三角面")

    order = np.argsort(high, kind="stable")
    cursor = 0
    ranges = {}
    for space, faces in zip(*np.unique(high, return_counts=True)):
        count = int(faces) * 3
        ranges[int(space)] = (cursor, count)
        cursor += count
    return triangles[order].reshape(-1), ranges


def restore_native_slots(position_bytes, blend_bytes, indices, sources):
    """Recover source draw ranges and private palette slots from the imported triangles."""
    def triangle_keys(positions, triangles):
        for corners in positions[triangles]:
            yield tuple(sorted(map(tuple, corners)))

    source_for_triangle = {}
    for section, (position_path, index_path, _) in sources.items():
        points = np.fromfile(position_path, dtype="<f4").reshape(-1, 10)[:, :3]
        triangles = np.fromfile(index_path, dtype="<u4").reshape(-1, 3)
        for key in triangle_keys(points, triangles):
            if source_for_triangle.setdefault(key, section) != section:
                source_for_triangle[key] = None

    positions = np.frombuffer(position_bytes, dtype="<f4").reshape(-1, 10)[:, :3]
    triangles = np.asarray(indices, dtype="<u4").reshape(-1, 3)
    packed = np.frombuffer(blend_bytes, dtype="<u4").reshape(-1, 8).copy()
    positive = packed[:, :4].copy().view("<f4") > 0
    by_section = {section: [] for section in sources}
    for index, key in enumerate(triangle_keys(positions, triangles)):
        section = source_for_triangle.get(key)
        if section is None:
            raise ValueError(f"三角面 {index} 无法唯一匹配原生部件")
        by_section[section].append(index)

    remapped_vertices = {}
    result = []
    ranges = {}
    cursor = 0
    for section, indices_in_section in by_section.items():
        if not indices_in_section:
            continue
        source_tris = triangles[indices_in_section].reshape(-1)
        lookup = sources[section][2]
        for vertex in np.unique(source_tris):
            mask = positive[vertex]
            canonical = packed[vertex, 4:][mask]
            previous = remapped_vertices.get(int(vertex))
            if previous is not None:
                if previous != section[0]:
                    raise ValueError(f"顶点 {vertex} 跨部件共用，无法保留各自蒙皮")
                continue
            try:
                packed[vertex, 4:][mask] = [lookup[int(slot)] for slot in canonical]
            except KeyError as exc:
                raise ValueError(f"{section} 缺少顶点使用的骨骼槽位 {exc.args[0]}") from exc
            remapped_vertices[int(vertex)] = section[0]
        result.append(source_tris)
        count = len(source_tris)
        ranges[section] = (cursor, count)
        cursor += count
    return np.concatenate(result), ranges, packed.tobytes()


def repack_texcoords(data, source, destination):
    formats = {
        "R32G32_FLOAT": ("<f4", 2),
        "R16G16_FLOAT": ("<f2", 2),
        "R32G32B32A32_FLOAT": ("<f4", 4),
        "R8G8B8A8_UNORM": ("u1", 4),
    }
    source_elements = {(e["semantic"], e["index"]): e for e in source["elements"]}
    count = len(data) // source["stride"]
    output = bytearray(count * destination["stride"])
    for element in destination["elements"]:
        key = element["semantic"], element["index"]
        original = source_elements.get(key)
        dest_format = formats.get(element["format"])
        if not original or not dest_format or original["format"] not in formats:
            raise ValueError(f"无法匹配原生顶点属性 {key}")
        source_dtype, cols = formats[original["format"]]
        dest_dtype, dest_cols = dest_format
        if cols != dest_cols:
            raise ValueError(f"顶点属性分量不一致 {key}")
        values = np.ndarray(
            (count, cols), dtype=source_dtype, buffer=data, offset=original["offset"],
            strides=(source["stride"], np.dtype(source_dtype).itemsize),
        )
        if dest_dtype == "u1" and source_dtype != "u1":
            values = np.rint(np.clip(values, 0, 1) * 255)
        elif source_dtype == "u1" and dest_dtype != "u1":
            values = values.astype(np.float32) / 255
        target = np.ndarray(
            (count, cols), dtype=dest_dtype, buffer=output, offset=element["offset"],
            strides=(destination["stride"], np.dtype(dest_dtype).itemsize),
        )
        target[:] = values
    return bytes(output)
