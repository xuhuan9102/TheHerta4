"""导出期接缝焊接：在 ``SubMeshModel`` join 之后的**临时 mesh** 上按缝合边统一接缝顶点属性。

用户最终拍板的方案（2026-10-22，方案 A：只统一属性，不删顶点）：
    用户已在 Blender 里**手动把需要合并的顶点标记为缝合边（seam edge）**。
    导出器不再按位置自动猜接缝，只按这个标记处理：

1. 导出器本来就会把同一子网格的所有物体 join 成一个临时 mesh（现有代码）；
2. join 后给每个顶点 / 每个面打 bmesh int 层「原物体 id」（按 join 顺序的顶点区间、
   loop 区间），供后续 draw 段与形态键使用；
3. **只在每个分组内**：把该分组内物体的、属于缝合边、且彼此距离在「距离阈值」以内的
   顶点聚成一处（``cluster_positions``：空间哈希 + 真实距离判定），再把它们的
   坐标 / 顶点组权重 / 形态键统一到同一个代表顶点。代表优先取该组的常驻物体
   （出现在多个组里的物体，通常是身体）的顶点，否则取组内最小号顶点；
4. 顶点行、面、loop、draw 段编号一律不变——所以物体切换与形态键帧表都不受影响，
   导出器读到的只是一份「接缝两侧属性完全一致」的 mesh；
5. 各分组之间不参与彼此的顶点集合，所以不会跨组统一；
6. 接缝两侧的 per-loop 法线也统一到代表顶点的 corner 法线，否则反光会在接缝处
   留下一条折线；法线统一失败只 warning，不中断导出。

无 seam 边 = 不焊接（warning「没有缝合边标记」）；任何异常整体回退（备份 mesh），
warning 后继续导出，不中断。

本模块顶层不导入 bpy（便于纯 numpy 单测）；真正焊接触及 bpy/bmesh 时才在函数内导入。
"""

from __future__ import annotations

import math
import time

import numpy

from .group_weld import DEFAULT_WELD_THRESHOLD
from .group_weld_apply import _drawcall_names, _log, _match_units_for_name, _warn

__all__ = [
    "build_mesh_vertex_ranges",
    "build_vertex_owner_array",
    "collect_weld_groups",
    "assign_face_buckets",
    "compute_index_segments",
    "polygon_counts_per_range",
    "weld_submesh_mesh",
]


def _empty_stats(reason: str) -> dict:
    return {
        "applied": False,
        "reason": str(reason),
        "before_vertex_count": 0,
        "after_vertex_count": 0,
        "removed_vertex_count": 0,
        "polygon_count_before": 0,
        "polygon_count_after": 0,
        "loop_count_before": 0,
        "loop_count_after": 0,
        "degenerate_face_count": 0,
        "index_recount_applied": False,
        "index_segments": [],
        "total_index_count": 0,
        "seam_edge_count": 0,
        "seam_vertex_count": 0,
        "unified_vertex_count": 0,
        "group_count": 0,
        "matched_group_count": 0,
        "group_stats": [],
        "threshold": 0.0,
        "vertex_owner_map": None,
        "elapsed_sec": 0.0,
        "message": "",
    }


# ---------------------------------------------------------------------------
# 1. join 后 mesh 里的物体顶点 / loop 区间
# ---------------------------------------------------------------------------


def _read_loop_vertex_indices(mesh) -> numpy.ndarray | None:
    """整份 mesh 的 loop → vertex 映射（拿不到返回 None）。"""
    loops = getattr(mesh, "loops", None)
    if loops is None:
        return None
    try:
        count = int(len(loops))
    except TypeError:
        return None
    if count <= 0:
        return numpy.empty(0, dtype=numpy.int64)
    buffer = numpy.empty(count, dtype=numpy.int32)
    try:
        loops.foreach_get("vertex_index", buffer)
    except (AttributeError, TypeError, RuntimeError, ValueError):
        try:
            buffer = numpy.asarray(
                [int(loop.vertex_index) for loop in loops], dtype=numpy.int32
            )
        except Exception:
            return None
    return buffer.astype(numpy.int64, copy=False)


def _read_polygon_spans(mesh) -> tuple | None:
    """每个多边形的 ``(loop_start, loop_total)``；拿不到拓扑时返回 None。"""
    polygons = getattr(mesh, "polygons", None)
    if polygons is None:
        return None
    try:
        count = int(len(polygons))
    except TypeError:
        return None
    if count <= 0:
        return (numpy.empty(0, dtype=numpy.int64), numpy.empty(0, dtype=numpy.int64))
    starts = numpy.empty(count, dtype=numpy.int32)
    totals = numpy.empty(count, dtype=numpy.int32)
    try:
        polygons.foreach_get("loop_start", starts)
        polygons.foreach_get("loop_total", totals)
    except (AttributeError, TypeError, RuntimeError, ValueError):
        try:
            starts = numpy.asarray(
                [int(polygon.loop_start) for polygon in polygons], dtype=numpy.int32
            )
            totals = numpy.asarray(
                [int(polygon.loop_total) for polygon in polygons], dtype=numpy.int32
            )
        except Exception:
            return None
    return starts.astype(numpy.int64), totals.astype(numpy.int64)


def _read_loop_count(mesh) -> int:
    """整份 mesh 的 loop 总数（拿不到返回 -1）。"""
    loops = getattr(mesh, "loops", None)
    if loops is None:
        return -1
    try:
        return int(len(loops))
    except TypeError:
        return -1


def _unique_loop_vertices(loop_vertex, loop_start: int, loop_count: int) -> numpy.ndarray:
    """某个 DrawCall loop 区间引用的顶点集合（升序去重）。"""
    total = int(loop_vertex.size)
    start = max(0, min(int(loop_start), total))
    end = max(start, min(start + max(int(loop_count), 0), total))
    if end <= start:
        return numpy.empty(0, dtype=numpy.int64)
    return numpy.unique(loop_vertex[start:end])


def build_mesh_vertex_ranges(drawcall_loop_ranges, mesh=None) -> list:
    """把 ``SubMeshModel.drawcall_loop_ranges`` 转成 join 后 mesh 的物体区间。

    返回 ``list[dict]``，每项：
        ``{"bucket", "offset", "count", "vertices", "names", "draw_calls",
           "loop_start", "loop_count", "reused", "order_mismatch"}``

    规则：
    - ``offset`` / ``count`` 用 ``draw_call_model.vertex_count`` 按 join 顺序（非复用
      记录的顺序）前缀和得出，与导出器「物体级区间」口径一致；
    - **几何复用**（``reused=True``）的 DrawCall 不新增几何，与首次出现的记录共享同一
      bucket / 区间（候选名合并）；
    - 传了 ``mesh`` 时，用 loop→vertex 映射校验并给出真实顶点集合 ``vertices``：
      join 顺序与列表顺序不一致时 ``order_mismatch=True``，此时 ``vertices`` 才是权威
      （导出器本身也是按 loop 区间划分物体的）。
    """
    loop_vertex = _read_loop_vertex_indices(mesh) if mesh is not None else None
    buckets: dict = {}
    ordered: list = []
    offset = 0
    for record in drawcall_loop_ranges or []:
        if not record:
            continue
        draw_call = record.get("draw_call")
        loop_start = int(record.get("loop_start", 0) or 0)
        loop_count = int(record.get("loop_count", 0) or 0)
        reused = bool(record.get("reused"))
        if not reused:
            count = int(getattr(draw_call, "vertex_count", 0) or 0)
            entry = {
                "bucket": len(ordered),
                "offset": int(offset),
                "count": count,
                "vertices": None,
                "names": [],
                "draw_calls": [],
                "loop_start": loop_start,
                "loop_count": loop_count,
                "reused": False,
                "order_mismatch": False,
            }
            buckets[loop_start] = entry
            ordered.append(entry)
            offset += count
        else:
            entry = buckets.get(loop_start)
            if entry is None:
                # 复用记录找不到首次出现（理论上不会发生）→ 跳过，不影响其它对象
                continue
        for name in _drawcall_names(draw_call, ""):
            if name not in entry["names"]:
                entry["names"].append(name)
        entry["draw_calls"].append(draw_call)

    for entry in ordered:
        if loop_vertex is not None:
            vertices = _unique_loop_vertices(
                loop_vertex, entry["loop_start"], entry["loop_count"]
            )
            entry["vertices"] = vertices
            if vertices.size:
                low = int(vertices.min())
                high = int(vertices.max())
                span_end = int(entry["offset"]) + int(entry["count"])
                if low < int(entry["offset"]) or high >= span_end:
                    entry["order_mismatch"] = True
        else:
            entry["vertices"] = numpy.arange(
                int(entry["offset"]),
                int(entry["offset"]) + int(entry["count"]),
                dtype=numpy.int64,
            )
    if any(entry["order_mismatch"] for entry in ordered):
        _warn(
            "[接缝焊接] join 后的顶点顺序与 DrawCall 列表顺序不一致，"
            "已改用 loop 区间推导每个物体的真实顶点集合"
        )
    return ordered


def build_vertex_owner_array(ranges, vertex_count: int) -> numpy.ndarray:
    """焊前：每个 mesh 顶点属于哪个原物体（bucket）；未归属为 -1。

    顶点区间互相重叠时抛 ``ValueError``（无法安全判定归属，调用方整体跳过焊接）。
    """
    owners = numpy.full(int(vertex_count), -1, dtype=numpy.int64)
    for entry in ranges or []:
        vertices = numpy.asarray(entry.get("vertices", []), dtype=numpy.int64).reshape(-1)
        if vertices.size == 0:
            continue
        if int(vertices.min()) < 0 or int(vertices.max()) >= int(vertex_count):
            raise ValueError("物体顶点区间越界（mesh 与记录不一致）")
        previous = owners[vertices]
        if numpy.any(previous != -1):
            raise ValueError("物体顶点区间互相重叠，无法安全焊接")
        owners[vertices] = int(entry.get("bucket", 0))
    return owners


# ---------------------------------------------------------------------------
# 2. 分组：节点配置的物体名 → 本 mesh 内的原物体 bucket 集合
# ---------------------------------------------------------------------------


def collect_weld_groups(ranges, groups) -> list:
    """把节点配置的每个组映射成本 mesh 内的原物体 bucket 集合。

    返回 ``list[dict]``，每项 ``{"label", "names", "buckets", "bucket_order"}``：
    - 组内只保留**本 mesh 里找得到**的物体；找不到的名字 warning 后跳过；
    - 一个物体都没匹配到的组直接跳过；
    - ``bucket_order`` 保留节点列表的可见上下顺序，供代表数据逐级传递。
    """
    groups_out: list = []
    for group_index, names in enumerate(groups or []):
        buckets: set = set()
        bucket_order: list = []
        matched_names: list = []
        for name in names or []:
            hits = _match_units_for_name(list(ranges or []), name)
            if not hits:
                _warn(
                    f"[接缝焊接] 第 {group_index + 1} 组的物体 {name!r} 在本次子网格的"
                    "导出物体里找不到，已跳过该条目"
                )
                continue
            for unit in hits:
                bucket = int(unit.get("bucket", 0))
                buckets.add(bucket)
                if bucket not in bucket_order:
                    bucket_order.append(bucket)
                matched_names.append(str(name))
        if not buckets:
            continue
        groups_out.append(
            {
                "label": f"组{group_index + 1}",
                "names": matched_names,
                "buckets": sorted(buckets),
                "bucket_order": bucket_order,
            }
        )
    return groups_out


def build_ordered_transfer_pairs(members, owner_ids, bucket_order) -> list:
    """按节点列表顺序生成链式传递对：第一→第二，第二→第三，依此类推。"""
    order_index = {
        int(bucket): index for index, bucket in enumerate(bucket_order or [])
    }
    fallback = len(order_index)
    ordered = sorted(
        (int(member) for member in members),
        key=lambda member: (
            order_index.get(int(owner_ids[member]), fallback),
            member,
        ),
    )
    return list(zip(ordered, ordered[1:]))


# ---------------------------------------------------------------------------
# 3. 焊后索引区间重算（面归属 → 每个 draw 段的 index_offset / index_count）
# ---------------------------------------------------------------------------

VERT_OWNER_LAYER = "_ssmt_seam_weld_owner"
FACE_BUCKET_LAYER = "_ssmt_seam_weld_bucket"


def assign_face_buckets(mesh, ranges):
    """按物体 loop 区间给每个面标上它所属的 bucket（焊前快照）。

    返回 ``numpy.ndarray``（长度 = 多边形数，未知归属为 -1）；拿不到面拓扑返回 None。
    """
    spans = _read_polygon_spans(mesh)
    if spans is None:
        return None
    starts, _totals = spans
    buckets = numpy.full(int(starts.size), -1, dtype=numpy.int64)
    for entry in ranges or []:
        loop_start = int(entry.get("loop_start", 0) or 0)
        loop_count = int(entry.get("loop_count", 0) or 0)
        if loop_count <= 0:
            continue
        inside = (starts >= loop_start) & (starts < loop_start + loop_count)
        buckets[inside] = int(entry.get("bucket", -1))
    return buckets


def _bucket_order(ranges):
    """原物体（bucket）顺序（= draw 段顺序）。"""
    order: list = []
    for entry in ranges or []:
        bucket = int(entry.get("bucket", -1))
        if bucket not in order:
            order.append(bucket)
    return order


def compute_index_segments(face_buckets, mesh, ranges) -> list:
    """按焊后「每个面属于哪个原物体」重算每个 draw 段的索引 / loop 区间。

    硬性不变量（违反即抛 ``ValueError``，由调用方整体回退焊接）：
    - 面归属必须按原物体顺序连续分块（bmesh 删面不改存活面的相对次序）；
    - 每个物体焊后的 loop 区间连续、互不重叠、合计等于 mesh loop 数；
    - ``sum(index_count) == 焊后面数 × 3`` 且每段 ``index_count % 3 == 0``。

    某个物体的面被全部删光时该段 ``index_count=0``（保留段，不删 draw 段），由调用方
    写 warning。

    返回 ``list[dict]``（按原物体顺序），每项：``bucket`` / ``names`` / ``face_count`` /
    ``index_count`` / ``index_offset`` / ``polygon_start`` / ``loop_start`` / ``loop_count``。
    """
    spans = _read_polygon_spans(mesh)
    if spans is None:
        raise ValueError("焊后无法读取面拓扑")
    starts, totals = spans
    total_faces = int(starts.size)
    order = _bucket_order(ranges)
    names_by_bucket = {
        int(entry.get("bucket", -1)): list(entry.get("names") or [])
        for entry in ranges or []
    }
    position = {bucket: index for index, bucket in enumerate(order)}
    face_array = numpy.asarray(face_buckets, dtype=numpy.int64).reshape(-1)
    if int(face_array.size) != total_faces:
        raise ValueError(f"焊后面归属数量 {face_array.size} != 面数 {total_faces}")

    previous = -1
    for bucket in face_array.tolist():
        index = position.get(int(bucket))
        if index is None:
            raise ValueError(f"焊后出现未知物体归属的面: bucket={bucket}")
        if index < previous:
            raise ValueError("焊后不同物体的面交错，无法按物体顺序切分索引区间")
        previous = index

    segments: list = []
    index_offset = 0
    loop_cursor = 0
    for bucket in order:
        indices = numpy.flatnonzero(face_array == bucket)
        if indices.size:
            start = int(indices[0])
            end = int(indices[-1]) + 1
            if end - start != int(indices.size):
                raise ValueError(f"物体 bucket={bucket} 的面不连续")
            loop_start = int(starts[start])
            loop_count = int(totals[start:end].sum())
            loop_end = int(starts[end - 1] + totals[end - 1])
            if loop_start != loop_cursor or loop_start + loop_count != loop_end:
                raise ValueError(f"物体 bucket={bucket} 的 loop 区间不连续")
            polygon_start = start
        else:
            loop_start = loop_cursor
            loop_count = 0
            polygon_start = -1
        segments.append(
            {
                "bucket": int(bucket),
                "names": names_by_bucket.get(int(bucket), []),
                "face_count": int(indices.size),
                "index_count": int(indices.size) * 3,
                "index_offset": int(index_offset),
                "polygon_start": int(polygon_start),
                "loop_start": int(loop_start),
                "loop_count": int(loop_count),
            }
        )
        index_offset += int(indices.size) * 3
        loop_cursor += loop_count

    mesh_loop_count = _read_loop_count(mesh)
    if mesh_loop_count >= 0 and loop_cursor != mesh_loop_count:
        raise ValueError(
            f"焊后 loop 区间合计 {loop_cursor} != mesh loop 数 {mesh_loop_count}"
        )
    if index_offset != total_faces * 3:
        raise ValueError(f"索引合计 {index_offset} != 焊后面数×3 {total_faces * 3}")
    for segment in segments:
        if segment["index_count"] % 3 != 0:
            raise ValueError(f"draw 段索引数不是 3 的倍数: {segment}")
    return segments


def polygon_counts_per_range(mesh, ranges) -> dict | None:
    """按物体 loop 区间统计多边形数（用于焊前 / 焊后逐物体面数校验）。

    返回 ``{bucket: 多边形数}``；拿不到拓扑返回 None。
    """
    spans = _read_polygon_spans(mesh)
    if spans is None:
        return None
    starts = spans[0]
    counts: dict = {}
    for entry in ranges or []:
        bucket = int(entry.get("bucket", -1))
        loop_start = int(entry.get("loop_start", 0))
        loop_count = int(entry.get("loop_count", 0))
        if loop_count > 0:
            inside = (starts >= loop_start) & (starts < loop_start + loop_count)
            counts[bucket] = int(numpy.count_nonzero(inside))
        else:
            counts[bucket] = 0
    return counts


# ---------------------------------------------------------------------------
# 4. 执行焊接（bmesh：int 层标原物体 id → 读 seam 边 → 逐组统一接缝顶点属性）
# ---------------------------------------------------------------------------


def cluster_positions(points, threshold=DEFAULT_WELD_THRESHOLD) -> list:
    """把位置彼此相距 ``threshold`` 以内的点聚成一簇（空间哈希 + 真实距离判定）。

    只用来给「接缝两侧应该算同一处」的顶点分组；不移动、不删除任何点。

    参数：
        points: 可迭代的 ``(x, y, z)`` 坐标序列（顺序即返回的成员下标）。
        threshold: 距离上限（含等号）；非数值 / NaN / ``<= 0`` 时退化成
            「坐标完全相等才同簇」，与 Blender「按距离合并」填 0 的语义一致。

    返回：
        ``list[list[int]]``：每个成员数 ``>= 2`` 的簇的下标列表，按最小成员下标排序。
        单点簇（没有邻居）不返回——调用方只关心能统一的组。
    """
    coords = [(float(point[0]), float(point[1]), float(point[2])) for point in points]
    count = len(coords)
    if count < 2:
        return []
    try:
        limit = float(threshold)
    except (TypeError, ValueError):
        limit = 0.0
    if not numpy.isfinite(limit) or limit <= 0.0:
        buckets: dict = {}
        for index, point in enumerate(coords):
            buckets.setdefault(point, []).append(index)
        return sorted(
            (members for members in buckets.values() if len(members) >= 2),
            key=lambda members: members[0],
        )

    limit_sq = limit * limit
    inverse = 1.0 / limit
    cells: dict = {}
    for index, (x, y, z) in enumerate(coords):
        key = (math.floor(x * inverse), math.floor(y * inverse), math.floor(z * inverse))
        cells.setdefault(key, []).append(index)

    # 并查集：只用「半邻域」（含自身格 + 13 个正方向偏移），每对点恰好比较一次
    parent = list(range(count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    half_neighbourhood = [
        (0, 0, 0),
        (1, 0, 0), (1, 1, 0), (1, -1, 0), (1, 0, 1), (1, 0, -1),
        (1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1),
        (0, 1, 0), (0, 0, 1), (0, 1, 1), (0, 1, -1),
    ]
    for (cx, cy, cz), members in cells.items():
        for dx, dy, dz in half_neighbourhood:
            same_cell = (dx, dy, dz) == (0, 0, 0)
            candidates = members if same_cell else cells.get((cx + dx, cy + dy, cz + dz))
            if not candidates:
                continue
            for position, left in enumerate(members):
                left_x, left_y, left_z = coords[left]
                others = candidates[position + 1:] if same_cell else candidates
                for right in others:
                    right_x, right_y, right_z = coords[right]
                    delta_x = left_x - right_x
                    delta_y = left_y - right_y
                    delta_z = left_z - right_z
                    if delta_x * delta_x + delta_y * delta_y + delta_z * delta_z <= limit_sq:
                        union(left, right)

    clusters: dict = {}
    for index in range(count):
        clusters.setdefault(find(index), []).append(index)
    return sorted(
        (members for members in clusters.values() if len(members) >= 2),
        key=lambda members: members[0],
    )


def _remove_mesh_attribute(mesh, name: str) -> None:
    """删掉焊接用的临时 mesh 属性（避免污染导出 mesh 的属性集）。"""
    try:
        attributes = getattr(mesh, "attributes", None)
        if attributes is not None and name in attributes:
            attributes.remove(attributes[name])
    except Exception:
        pass


def _weld_with_bmesh(obj, owner_ids, face_buckets, weld_groups, threshold) -> dict:
    """真正的焊接执行器：只统一接缝顶点的属性，不增删顶点 / 面 / loop（方案 A）。

    步骤（严格按用户拍板的方案）：
    1. 建 bmesh，给每个顶点 / 每个面写 int 层「原物体 id」；
    2. 读 ``bm.edges`` 里 ``edge.seam == True`` 的边 → 缝合边顶点集合；
    3. ``cluster_positions`` 按 ``threshold`` 把 seam 顶点聚成「同一处」；
    4. 逐组、逐簇：严格按节点物体列表顺序链式传递坐标 / 顶点组权重 / 形态键 /
       顶点法线（第一传第二、第二传第三……）；
    5. 全部处理完 ``bm.to_mesh(mesh)``；
    6. 读回焊后每个顶点 / 每个面的原物体 id，并把接缝两侧的 per-loop 法线统一。
    """
    import bmesh

    mesh = obj.data
    owner_array = numpy.asarray(owner_ids, dtype=numpy.int64).reshape(-1)
    face_array = (
        numpy.asarray(face_buckets, dtype=numpy.int64).reshape(-1)
        if face_buckets is not None
        else None
    )

    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        vert_layer = bm.verts.layers.int.new(VERT_OWNER_LAYER)
        for index, vert in enumerate(bm.verts):
            vert[vert_layer] = (
                int(owner_array[index]) if index < owner_array.size else -1
            )
        face_layer = None
        if face_array is not None and len(bm.faces):
            face_layer = bm.faces.layers.int.new(FACE_BUCKET_LAYER)
            for index, face in enumerate(bm.faces):
                face[face_layer] = (
                    int(face_array[index]) if index < face_array.size else -1
                )

        # 2. 读 seam 边（用户手动标记的缝合边）；逐组焊接前还会重新读一遍，
        #    因为上一组焊掉的顶点已不在 bm 里。同一物体可以出现在多个组里
        #    （用户的 3 组都含身体），后续组用焊后仍存活的同一批顶点继续焊。
        def _collect_seam_verts() -> set:
            collected: set = set()
            for edge in bm.edges:
                if not getattr(edge, "seam", False):
                    continue
                collected.add(edge.verts[0])
                collected.add(edge.verts[1])
            return collected

        initial_seam_verts = _collect_seam_verts()
        seam_edge_count = sum(1 for edge in bm.edges if getattr(edge, "seam", False))

        # 方案 A（用户 2026-10-22 拍板）：**不合并顶点行**，只把「同位置、属于
        # 缝合边、跨不同物体」的顶点属性（坐标 / 顶点组权重 / 形态键）统一到
        # 同一个代表值。顶点数、索引、draw 段、形态键 buffer 行数全部保持不变，
        # 导出器拿到的是一份「接缝两侧属性一致」的 mesh，形态键不会被影响。
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        try:
            deform_layer = bm.verts.layers.deform.active
        except Exception:
            deform_layer = None
        try:
            shape_layers = [layer for _name, layer in bm.verts.layers.shape.items()]
        except Exception:
            shape_layers = []

        # 聚类键交给「距离阈值」：seam 顶点彼此相距 <= threshold 才算同一处接缝，
        # 不再用固定的 1e-6 量化网格（那样阈值填什么都不起作用）。
        seam_verts = sorted(initial_seam_verts, key=lambda vert: vert.index)
        cluster_members = cluster_positions(
            [tuple(vert.co) for vert in seam_verts], threshold
        )
        clusters: list = [
            [seam_verts[index] for index in members] for members in cluster_members
        ]

        group_stats: list = []
        for group in weld_groups or []:
            buckets = {int(value) for value in (group.get("buckets") or [])}
            group_stats.append(
                {
                    "label": str(group.get("label") or "组"),
                    "names": list(group.get("names") or []),
                    "seam_vertex_count": int(
                        sum(
                            1
                            for vert in seam_verts
                            if int(vert[vert_layer]) in buckets
                        )
                    ),
                    "merged_vertex_count": 0,
                }
            )

        before = len(bm.verts)
        unified_vertices = 0
        normal_targets: dict = {}
        for members in clusters:
            if len(members) < 2:
                continue
            vert_by_index = {int(vert.index): vert for vert in members}
            for group_index, group in enumerate(weld_groups or []):
                buckets = {int(value) for value in (group.get("buckets") or [])}
                group_members = [
                    vert for vert in members if int(vert[vert_layer]) in buckets
                ]
                if len(group_members) < 2:
                    continue
                pairs = build_ordered_transfer_pairs(
                    [int(vert.index) for vert in group_members],
                    owner_array,
                    group.get("bucket_order") or group.get("buckets") or [],
                )
                if not pairs:
                    continue
                first_source = vert_by_index[pairs[0][0]]
                first_normal = tuple(float(value) for value in first_source.normal)
                for source_index, target_index in pairs:
                    source = vert_by_index[source_index]
                    target = vert_by_index[target_index]
                    target.co = source.co.copy()
                    if deform_layer is not None:
                        weights = target[deform_layer]
                        weights.clear()
                        for vertex_group_index, weight in source[deform_layer].items():
                            weights[vertex_group_index] = weight
                    for shape_layer in shape_layers:
                        target[shape_layer] = source[shape_layer]
                    normal_targets[target_index] = first_normal
                    unified_vertices += 1
                    group_stats[group_index]["merged_vertex_count"] += 1
        after = len(bm.verts)
        for item in group_stats:
            item["before_vertex_count"] = int(before)
            item["after_vertex_count"] = int(after)
        # 汇报给调用方的是焊前的 seam 边 / 顶点数量（分组日志里另有每组数量）
        seam_vertex_count = len(initial_seam_verts)

        # 5. 焊后读回归属（顶点 / 面）
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        after_owner = numpy.fromiter(
            (int(vert[vert_layer]) for vert in bm.verts),
            dtype=numpy.int64,
            count=len(bm.verts),
        )
        after_faces = None
        if face_layer is not None:
            after_faces = numpy.fromiter(
                (int(face[face_layer]) for face in bm.faces),
                dtype=numpy.int64,
                count=len(bm.faces),
            )
        bm.to_mesh(mesh)
    finally:
        bm.free()

    mesh.update()
    # 法线统一：导出器读的是 per-loop 法线（mesh.loops.normal，obj_buffer_helper 同款）。
    # 方案 A 只统一属性，所以接缝两侧必须把 per-loop 法线统一到链首源顶点的顶点法线。
    if normal_targets:
        try:
            _unify_seam_loop_normals(mesh, normal_targets)
        except Exception as exc:
            _warn(f"[接缝焊接] 接缝法线统一失败（表面可能出现折线）: {exc}")
    _remove_mesh_attribute(mesh, VERT_OWNER_LAYER)
    _remove_mesh_attribute(mesh, FACE_BUCKET_LAYER)

    return {
        "vertex_owner": after_owner,
        "face_buckets": after_faces,
        "seam_edge_count": int(seam_edge_count),
        "seam_vertex_count": int(seam_vertex_count),
        "group_stats": group_stats,
        "unified_vertex_count": int(unified_vertices),
    }


def _read_loop_normals(mesh, loop_count: int):
    """读 per-loop 法线（corner normals），顺序与 ``mesh.loops`` 一致。"""
    normals = numpy.empty(loop_count * 3, dtype=numpy.float32)
    corner_normals = getattr(mesh, "corner_normals", None)
    if corner_normals is not None and len(corner_normals) == loop_count:
        corner_normals.foreach_get("vector", normals)
        return normals.reshape(-1, 3)
    # 老版本 bpy：先让 split normals 就位再逐 loop 读
    calc = getattr(mesh, "calc_normals_split", None)
    if callable(calc):
        calc()
    mesh.loops.foreach_get("normal", normals)
    return normals.reshape(-1, 3)


def _unify_seam_loop_normals(mesh, normal_targets: dict) -> None:
    """把被传递顶点的 per-loop 法线写成源顶点的顶点法线。"""
    loop_count = int(len(mesh.loops))
    vertex_count = int(len(mesh.vertices))
    if loop_count <= 0 or vertex_count <= 0:
        return
    loop_vertex = numpy.empty(loop_count, dtype=numpy.int32)
    mesh.loops.foreach_get("vertex_index", loop_vertex)
    loop_normals = _read_loop_normals(mesh, loop_count)

    target_normals = numpy.zeros((vertex_count, 3), dtype=numpy.float32)
    has_target = numpy.zeros(vertex_count, dtype=bool)
    assigned = 0
    for target_index, normal in normal_targets.items():
        target_index = int(target_index)
        if not 0 <= target_index < vertex_count:
            continue
        target_normals[target_index] = normal
        has_target[target_index] = True
        assigned += 1
    if not assigned:
        return
    mask = has_target[loop_vertex]
    if not bool(mask.any()):
        return
    loop_normals[mask] = target_normals[loop_vertex[mask]]
    mesh.normals_split_custom_set(loop_normals.tolist())
    mesh.update()


def _snapshot_mesh(obj):
    """焊接前的 mesh 副本（用于整体回退）；拿不到副本时返回 None。"""
    mesh = getattr(obj, "data", None)
    copy_method = getattr(mesh, "copy", None)
    if not callable(copy_method):
        return None
    try:
        return copy_method()
    except Exception:
        return None


def _restore_mesh(obj, backup) -> None:
    """把物体 data 指回焊接前的副本（失败回退）。"""
    if backup is None:
        _warn("[接缝焊接] 没有可用的 mesh 备份，无法回退（本次异常可能留下半焊的 mesh）")
        return
    current = getattr(obj, "data", None)
    try:
        obj.data = backup
    except Exception:
        return
    try:
        import bpy

        if current is not None and current is not backup and current.users == 0:
            bpy.data.meshes.remove(current)
    except Exception:
        pass


def _drop_snapshot(backup) -> None:
    """焊接成功后释放副本。"""
    if backup is None:
        return
    try:
        import bpy

        if backup.users == 0:
            bpy.data.meshes.remove(backup)
    except Exception:
        pass


def weld_submesh_mesh(
    merged_obj,
    mesh_object_vertex_ranges,
    groups,
    threshold=DEFAULT_WELD_THRESHOLD,
    weld_executor=None,
) -> dict:
    """在 join 后的临时 mesh 上按「接缝焊接」分组统一接缝顶点属性（方案 A）。

    参数：
        merged_obj: ``SubMeshModel`` join 出来的临时物体（`obj.data` 是合并 mesh）。
        mesh_object_vertex_ranges: ``build_mesh_vertex_ranges`` 的返回值。
        groups: 节点配置的组（``[[物体名, ...], ...]``）；空 = 不焊接。
        threshold: 距离阈值——seam 顶点彼此相距不超过它才算同一处接缝
            （默认 0.0001 = Blender「按距离合并」默认值）。
        weld_executor: 可选的注入执行器（单测用 fake）；默认 bmesh 方案 A 执行器。
            执行器签名 ``(obj, owner_ids, face_buckets, weld_groups, threshold)``，
            返回 ``{"vertex_owner", "face_buckets", "seam_edge_count",
            "seam_vertex_count", "group_stats", "unified_vertex_count"}``。

    返回：
        stats dict；``applied=True`` 表示**真的有顶点被统一**（``unified_vertex_count > 0``），
        顶点数 / 面数不变。无缝合边标记 / 没有可统一的接缝 / 任何异常 / 校验失败都会
        整体回退到焊接前的 mesh 副本并返回 ``applied=False``，由调用方写 warning 后
        继续导出。
    """
    started = time.perf_counter()
    stats = _empty_stats("没有可焊接的分组")
    try:
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        threshold_value = 0.0
    stats["threshold"] = threshold_value
    stats["group_count"] = int(len(groups or []))

    mesh = getattr(merged_obj, "data", None)
    if mesh is None:
        stats["reason"] = "物体没有 mesh 数据"
        return stats
    ranges = list(mesh_object_vertex_ranges or [])
    if not ranges:
        stats["reason"] = "没有物体顶点区间"
        return stats
    if not groups or not any(groups):
        stats["reason"] = "没有接缝焊接节点配置"
        return stats
    if not numpy.isfinite(threshold_value) or threshold_value <= 0.0:
        stats["reason"] = "焊接阈值无效"
        return stats

    try:
        before_vertex_count = int(len(mesh.vertices))
    except Exception:
        before_vertex_count = 0
    stats["before_vertex_count"] = before_vertex_count
    stats["after_vertex_count"] = before_vertex_count
    if before_vertex_count <= 0:
        stats["reason"] = "mesh 没有顶点"
        return stats

    # 2. 原物体 id：顶点区间（join 顺序前缀和）+ 面 loop 区间
    try:
        owner_ids = build_vertex_owner_array(ranges, before_vertex_count)
    except Exception as exc:
        stats["reason"] = f"原物体顶点归属解析失败: {exc}"
        _warn(f"[接缝焊接] {stats['reason']}，跳过焊接")
        return stats
    try:
        face_buckets_before = assign_face_buckets(mesh, ranges)
    except Exception as exc:
        stats["reason"] = f"焊前面归属读取失败: {exc}"
        _warn(f"[接缝焊接] {stats['reason']}，跳过焊接")
        return stats
    if face_buckets_before is None:
        stats["reason"] = "拿不到面拓扑，焊后无法重算索引区间"
        _warn(f"[接缝焊接] {stats['reason']}，跳过焊接")
        return stats
    if int(numpy.count_nonzero(numpy.asarray(face_buckets_before) < 0)):
        stats["reason"] = "存在不属于任何物体的面，跳过焊接"
        _warn(f"[接缝焊接] {stats['reason']}")
        return stats

    # 3. 分组：节点配置的物体名 → 本 mesh 的原物体 bucket
    weld_groups = collect_weld_groups(ranges, groups)
    stats["matched_group_count"] = int(len(weld_groups))
    if not weld_groups:
        stats["reason"] = "节点分组匹配不到本子网格内的导出物体"
        _warn(f"[接缝焊接] {stats['reason']}，跳过焊接")
        return stats

    try:
        polygon_count_before = int(len(mesh.polygons))
    except Exception:
        polygon_count_before = 0
    loop_count_before = _read_loop_count(mesh)
    stats["polygon_count_before"] = polygon_count_before
    stats["polygon_count_after"] = polygon_count_before
    stats["loop_count_before"] = loop_count_before
    stats["loop_count_after"] = loop_count_before

    executor = weld_executor or _weld_with_bmesh
    backup = _snapshot_mesh(merged_obj)
    try:
        result = executor(
            merged_obj, owner_ids, face_buckets_before, weld_groups, threshold_value
        )
        if not isinstance(result, dict):
            raise ValueError("焊接执行器没有返回结果字典")
        seam_edge_count = int(result.get("seam_edge_count", 0) or 0)
        stats["seam_edge_count"] = seam_edge_count
        stats["seam_vertex_count"] = int(result.get("seam_vertex_count", 0) or 0)
        group_stats = list(result.get("group_stats") or [])
        stats["group_stats"] = group_stats
        unified_vertex_count = int(result.get("unified_vertex_count", 0) or 0)
        if not unified_vertex_count:
            # 注入的 fake 执行器可能只回报分组统计，按成员统计兜底
            unified_vertex_count = sum(
                int(item.get("merged_vertex_count", 0) or 0) for item in group_stats
            )
        stats["unified_vertex_count"] = unified_vertex_count
        for item in group_stats:
            _log(
                f"[接缝焊接] {item.get('label')}"
                f"[{', '.join(item.get('names') or [])}]: "
                f"seam 顶点 {item.get('seam_vertex_count')}，"
                f"统一 {item.get('merged_vertex_count')} "
                f"(顶点 {item.get('before_vertex_count')} → "
                f"{item.get('after_vertex_count')})"
            )
        if seam_edge_count <= 0:
            raise _NoSeamError("没有缝合边标记")

        welded_mesh = getattr(merged_obj, "data", None)
        after_vertex_count = int(len(welded_mesh.vertices))
        after_polygon_count = int(len(welded_mesh.polygons))
        if after_vertex_count != before_vertex_count:
            raise ValueError(
                f"方案 A（属性统一）不改变顶点数，但焊后 {after_vertex_count} "
                f"!= 焊前 {before_vertex_count}"
            )
        removed_faces = int(polygon_count_before - after_polygon_count)
        if removed_faces < 0:
            raise ValueError(
                f"焊接后面数 {after_polygon_count} 多于焊前 {polygon_count_before}"
            )
        stats["degenerate_face_count"] = removed_faces
        stats["polygon_count_after"] = after_polygon_count
        after_loop_count = _read_loop_count(welded_mesh)
        stats["loop_count_after"] = after_loop_count
        if removed_faces:
            _warn(
                f"[接缝焊接] 焊接使 {removed_faces} 个三角形退化被 Blender 删除"
                f"（面数 {polygon_count_before} → {after_polygon_count}），"
                "index_count / index_offset 将按焊后 mesh 重算"
            )

        vertex_owner = result.get("vertex_owner")
        if vertex_owner is None:
            raise ValueError("焊接执行器没有回报焊后顶点归属")
        owner_array = numpy.asarray(vertex_owner, dtype=numpy.int64).reshape(-1)
        if int(owner_array.size) != after_vertex_count:
            raise ValueError(
                f"焊后顶点归属数量 {owner_array.size} != 顶点数 {after_vertex_count}"
            )
        known_buckets = {int(entry.get("bucket", -1)) for entry in ranges}
        unknown = numpy.setdiff1d(numpy.unique(owner_array), numpy.array(sorted(known_buckets | {-1})))
        if unknown.size:
            raise ValueError(f"焊后出现未知原物体 id: {unknown.tolist()}")
        stats["vertex_owner_map"] = owner_array

        after_face_buckets = result.get("face_buckets")
        if after_face_buckets is None:
            raise ValueError("焊后拿不到面归属，无法重算索引区间")
        segments = compute_index_segments(after_face_buckets, welded_mesh, ranges)
        stats["index_segments"] = segments
        stats["total_index_count"] = int(
            sum(int(segment["index_count"]) for segment in segments)
        )
        stats["index_recount_applied"] = True
        if stats["total_index_count"] != after_polygon_count * 3:
            raise ValueError(
                f"索引合计 {stats['total_index_count']} != 焊后面数×3 "
                f"{after_polygon_count * 3}"
            )
        for segment in segments:
            if int(segment["index_count"]) <= 0:
                _warn(
                    f"[接缝焊接] 物体 {segment.get('names') or segment['bucket']}"
                    " 的面被全部删除，该 draw 段索引数设为 0（不删段）"
                )
    except _NoSeamError as exc:
        _restore_mesh(merged_obj, backup)
        stats["applied"] = False
        stats["index_recount_applied"] = False
        stats["index_segments"] = []
        stats["total_index_count"] = 0
        stats["after_vertex_count"] = before_vertex_count
        stats["polygon_count_after"] = stats["polygon_count_before"]
        stats["reason"] = str(exc)
        stats["elapsed_sec"] = time.perf_counter() - started
        _warn(
            "[接缝焊接] 没有缝合边标记：请在 Blender 编辑模式下把需要按距离合并的"
            "接缝顶点标记为缝合边（Ctrl+E → 标记缝合边）后再导出；本次不焊接"
        )
        return stats
    except Exception as exc:
        _restore_mesh(merged_obj, backup)
        stats["applied"] = False
        stats["index_recount_applied"] = False
        stats["index_segments"] = []
        stats["total_index_count"] = 0
        stats["degenerate_face_count"] = 0
        stats["vertex_owner_map"] = None
        stats["reason"] = f"焊接执行失败，已回退: {exc}"
        stats["after_vertex_count"] = before_vertex_count
        stats["polygon_count_after"] = stats["polygon_count_before"]
        stats["elapsed_sec"] = time.perf_counter() - started
        _warn(f"[接缝焊接] {stats['reason']}")
        return stats

    _drop_snapshot(backup)
    # 方案 A 只统一属性：顶点数不变，所以「有没有真的焊到东西」只能看统一数
    unified_vertex_count = int(stats.get("unified_vertex_count", 0) or 0)
    stats["applied"] = bool(unified_vertex_count > 0)
    stats["reason"] = (
        ""
        if stats["applied"]
        else "接缝上没有可统一的顶点（分组没匹配上物体，或两侧顶点都属常驻物体被跳过）"
    )
    stats["removed_vertex_count"] = int(before_vertex_count - after_vertex_count)
    stats["after_vertex_count"] = int(after_vertex_count)
    stats["elapsed_sec"] = time.perf_counter() - started
    if not stats["applied"]:
        _warn(f"[接缝焊接] {stats['reason']}；本次不焊接")
    group_text = "；".join(
        f"{item.get('label')} seam {item.get('seam_vertex_count')} → 统一 "
        f"{item.get('merged_vertex_count')}"
        for item in stats["group_stats"]
    )
    stats["message"] = (
        f"接缝焊接(seam 驱动, 方案 A 属性统一): 统一 {unified_vertex_count} 个顶点"
        f"（顶点数 {before_vertex_count} → {after_vertex_count}，面数 "
        f"{polygon_count_before} → {after_polygon_count}，"
        f"seam 边 {seam_edge_count} / seam 顶点 {stats['seam_vertex_count']}），"
        f"{group_text or '无分组'}，"
        f"索引 {stats['total_index_count']}（{len(stats['index_segments'])} 段），"
        f"阈值 {threshold_value:.6g}，{stats['elapsed_sec']:.3f}s"
    )
    return stats


class _NoSeamError(RuntimeError):
    """mesh 上没有任何缝合边标记（不是失败，只是不焊接）。"""
