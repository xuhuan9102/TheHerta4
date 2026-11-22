
"""Prepare shared ZZMI indices and per-layout UV streams for native rendering."""

import os
import zlib

import numpy as np


FACE_SOURCE_ATTRIBUTE = "herta_zzmi_face_source"


def face_source_id(identity):
    return (zlib.crc32(identity.split(".", 1)[-1].encode("utf8")) & 0x7fffffff) or 1


def tag_face_sources(mesh, identity):
    ids = np.full(len(mesh.polygons), face_source_id(identity), dtype=np.int32)
    write_face_sources(mesh, ids)


def read_face_sources(mesh, index_count=None):
    attribute = mesh.attributes.get(FACE_SOURCE_ATTRIBUTE)
    if attribute is None or attribute.domain != "FACE" or attribute.data_type != "INT":
        return None
    count = len(mesh.polygons)
    if index_count is not None:
        if count * 3 != index_count:
            return None
        starts = np.empty(count, dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", starts)
        if not np.array_equal(starts, np.arange(count) * 3):
            return None
    ids = np.empty(count, dtype=np.int32)
    attribute.data.foreach_get("value", ids)
    return ids


def restore_native_slots(blend_bytes, indices, sources, face_sources):
    """Use the imported face source to assign index ranges and native bone slots."""
    if face_sources is None or len(face_sources) * 3 != len(indices):
        raise ValueError("导出网格缺少完整的原生面来源")
    by_tag = {face_source_id(bare): bare for bare in sources}
    if set(np.unique(face_sources)) != by_tag.keys():
        raise ValueError("导出网格的原生面来源与绘制计划不一致")

    triangles = np.asarray(indices, dtype="<u4").reshape(-1, 3)
    packed = np.frombuffer(blend_bytes, dtype="<u4").reshape(-1, 8).copy()
    positive = packed[:, :4].copy().view("<f4") > 0
    remapped_vertices = np.full(len(packed), -1, dtype=np.int32)
    result = []
    ranges = {}
    cursor = 0
    for tag, bare in by_tag.items():
        source_tris = triangles[face_sources == tag].reshape(-1)
        vertices = np.unique(source_tris)
        if (remapped_vertices[vertices] != -1).any():
            raise ValueError(f"{bare} 与其他部件共用导出顶点，无法保留各自蒙皮")
        remapped_vertices[vertices] = tag
        vertex_rows, columns = np.nonzero(positive[vertices])
        rows = vertices[vertex_rows]
        canonical = packed[rows, columns + 4]
        lookup = sources[bare]
        try:
            mapped = {int(slot): lookup[int(slot)] for slot in np.unique(canonical)}
        except KeyError as exc:
            raise ValueError(f"{bare} 缺少顶点使用的骨骼槽位 {exc.args[0]}") from exc
        for slot, native in mapped.items():
            keep = canonical == slot
            packed[rows[keep], columns[keep] + 4] = native
        result.append(source_tris)
        count = len(source_tris)
        ranges[bare] = (cursor, count)
        cursor += count
    return np.concatenate(result), ranges, packed.tobytes()


TEXCOORD_FORMATS = {
    "R32G32_FLOAT": ("<f4", 2),
    "R16G16_FLOAT": ("<f2", 2),
    "R32G32B32A32_FLOAT": ("<f4", 4),
    "R8G8B8A8_UNORM": ("u1", 4),
}


def union_texcoord_elements(elements):
    """Keep every native input and use the widest representation of each."""
    result = {}
    for element in elements:
        key = element["SemanticName"], int(element["SemanticIndex"])
        existing = result.setdefault(key, element)
        if element["Format"] == existing["Format"]:
            continue
        old_format = TEXCOORD_FORMATS.get(existing["Format"])
        new_format = TEXCOORD_FORMATS.get(element["Format"])
        if old_format is None or new_format is None or old_format[1] != new_format[1]:
            raise ValueError(f"原生顶点属性 {key} 的格式无法合并")
        if int(element["ByteWidth"]) > int(existing["ByteWidth"]):
            result[key] = element
    return [result[key] for key in sorted(result)]


def repack_texcoords(data, source, destination):
    source_elements = {(e["semantic"], e["index"]): e for e in source["elements"]}
    count = len(data) // source["stride"]
    output = bytearray(count * destination["stride"])
    for element in destination["elements"]:
        key = element["semantic"], element["index"]
        original = source_elements.get(key)
        dest_format = TEXCOORD_FORMATS.get(element["format"])
        if not original or not dest_format or original["format"] not in TEXCOORD_FORMATS:
            raise ValueError(f"无法匹配原生顶点属性 {key}")
        source_dtype, cols = TEXCOORD_FORMATS[original["format"]]
        dest_dtype, dest_cols = dest_format
        if cols != dest_cols:
            raise ValueError(f"顶点属性分量不一致 {key}")
        values = np.ndarray(
            (count, cols), dtype=source_dtype, buffer=data, offset=original["offset"],
            strides=(source["stride"], np.dtype(source_dtype).itemsize),
        )
        if dest_dtype == "u1" and source_dtype != "u1":
            values = np.clip(values, 0, 1)
            if key[0] == "COLOR":
                from ..utils.color_attribute_utils import linear_to_srgb

                values[:, :3] = linear_to_srgb(values[:, :3])
            values = np.rint(values * 255)
        elif source_dtype == "u1" and dest_dtype != "u1":
            values = values.astype(np.float32) / 255
        target = np.ndarray(
            (count, cols), dtype=dest_dtype, buffer=output, offset=element["offset"],
            strides=(destination["stride"], np.dtype(dest_dtype).itemsize),
        )
        target[:] = values
    return bytes(output)

def write_face_sources(mesh, ids):
    attribute = mesh.attributes.get(FACE_SOURCE_ATTRIBUTE)
    if attribute is None:
        attribute = mesh.attributes.new(name=FACE_SOURCE_ATTRIBUTE, type="INT", domain="FACE")
    attribute.data.foreach_set("value", ids)


def recover_join_face_sources(obj, paths):
    """Verify the source blocks left by Blender Join in an unedited legacy mesh."""
    mesh = obj.data
    if len(mesh.loops) != len(mesh.polygons) * 3:
        raise ValueError("旧网格包含修改过的面，请从工作空间重新导入后再合并")
    vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", vertices)
    vertices = vertices.reshape(-1, 3)
    triangles = np.empty(len(mesh.loops), dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", triangles)
    triangles = triangles.reshape(-1, 3)
    low = triangles.min(axis=1)
    high = triangles.max(axis=1)

    candidates = {}
    for bare, path in paths.items():
        if not path:
            continue
        stem = os.path.splitext(path)[0]
        points = np.fromfile(stem + "-Position.buf", dtype="<f4").reshape(-1, 10)[:, :3]
        source = np.fromfile(stem + ".ib", dtype="<u4").reshape(-1, 3)
        if not len(source) or len(source) * 3 != int(bare.split("-")[1]):
            continue
        used, compact = np.unique(source, return_inverse=True)
        if used[-1] >= len(points):
            continue
        for block, indices in ((points, source), (points[used], compact.reshape(-1, 3))):
            ordered = np.sort(indices, axis=1)
            keep = (ordered[:, 0] != ordered[:, 1]) & (ordered[:, 1] != ordered[:, 2])
            candidates.setdefault(bare, {})[len(block)] = (block, np.unique(ordered[keep], axis=0))

    result = np.zeros(len(triangles), dtype=np.int32)
    start = 0
    matched = set()
    while start < len(vertices):
        possibilities = []
        for bare, variants in candidates.items():
            if bare in matched:
                continue
            for size, (points, source) in variants.items():
                end = start + size
                block = vertices[start:end]
                if len(block) != size or not (
                    np.array_equal(block, points)
                    or np.array_equal(block, points * (-1, 1, 1))
                ):
                    continue
                mask = (low >= start) & (high < end)
                local = np.sort(triangles[mask] - start, axis=1)
                if len(local) == len(source) and np.array_equal(np.unique(local, axis=0), source):
                    possibilities.append((bare, end, mask))
        if len(possibilities) != 1:
            raise ValueError(f"顶点 {start} 的原生部件无法唯一确认，请重新导入源部件后再合并")
        bare, start, mask = possibilities[0]
        result[mask] = face_source_id(bare)
        matched.add(bare)
    if not result.all():
        raise ValueError("旧网格的部分面跨越了原生部件，请重新导入源部件后再合并")
    return result
