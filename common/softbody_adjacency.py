"""M1：软体邻接网烘焙（纯 Python，无 bpy / 无 IO）。

软体物理要有"皮肤感"，第一步不是着色器，而是**邻接网**：每个顶点认识自己的邻居，
并且知道每条边的静止长度。没有它，顶点之间没有任何联系 —— 那正是现在拖拽系统
"一堆各自独立的弹簧点"的本质（`rzmi_jiggle_interaction.hlsl` 自述
"single-buffer Null-spring jiggle"）。

产出（每个 DrawIB 两个 buffer，全部小端 uint32，与 VGMap 同一条导出通道）：

    sb_adj_<ib>.buf     每顶点 1 条 uint4 = (邻居数, 边表起始下标, 0, 0)
    sb_edges_<ib>.buf   每条边 1 条 uint4 = (邻居顶点号, asuint(静止长度), 0, 0)

**为什么这样排**：求解器每个顶点只读 1 次 adj 记录 + count 次 edge 记录，读写次数
最少；邻居号保持整数、静止长度用 `asfloat()` 按位读，避免"索引转 float"的精度损失
（超过 16777216 就会开始丢）。

边的定义：三角形三条边去重（`a<b` 规范化），**严格无向** —— A 的邻居表里有 B，
就一定有 B 的邻居表里有 A。这条对称性是后面互相拉扯、自碰撞的正确性前提，
本模块自带校验并把它写进报告里。
"""

import struct


class SoftBodyAdjacencyError(ValueError):
    """邻接网构建失败（顶层可捕获后中止导出）。"""


def _normalize_triangles(triangles):
    """三角形迭代 → 三元组列表；顺带丢掉退化三角形（有重复顶点号）。"""
    cleaned = []
    for tri in triangles:
        if tri is None or len(tri) != 3:
            raise SoftBodyAdjacencyError(f"三角形必须是三元组，收到 {tri!r}")
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        if a == b or b == c or a == c:
            continue
        cleaned.append((a, b, c))
    return cleaned


def build_adjacency(positions, triangles, *, neighbor_cap: int = 0):
    """从顶点坐标 + 三角形构建邻接网。

    参数
    ----
    positions : 每顶点 (x, y, z)
    triangles : 每面 (i0, i1, i2)
    neighbor_cap : >0 时对邻居数做上限裁剪（保留**最短**的若干条边）。
        默认 0 = 不裁剪（保留全部）。裁剪会破坏"边是共享的"这一前提，
        因此裁剪时被丢掉的边会对**两端都丢**，对称性仍然成立。
        ⚠️ 注意：裁剪是"每个顶点各自保留自己最短的 N 条"，但某条边只要被
        **任一端**保留就还在，所以**裁剪后的度数可能超过 N**。这是刻意的取舍：
        对称性（互相拉扯/自碰撞的正确性）优先于严格度数上限；求解器按
        `report['max_neighbors']` 决定它的循环上界与带宽预算。

    返回 ``(adj_records, edges, report)``：
    - adj_records : [(neighbor_count, edge_start), ...] 长度 = 顶点数
    - edges       : [(neighbor_index, rest_length), ...]（按顶点分组、组内升序）
    - report      : 校验报告（见 `validate_adjacency`）
    """
    pos = [(float(p[0]), float(p[1]), float(p[2])) for p in positions]
    vertex_count = len(pos)
    tris = _normalize_triangles(triangles)

    for tri in tris:
        for idx in tri:
            if idx < 0 or idx >= vertex_count:
                raise SoftBodyAdjacencyError(
                    f"三角形引用了越界顶点 {idx}（顶点数 {vertex_count}）"
                )

    # 1) 收集无向边（a<b 规范化 + 去重）
    edge_set = set()
    for a, b, c in tris:
        for u, v in ((a, b), (b, c), (c, a)):
            edge_set.add((u, v) if u < v else (v, u))

    # 2) 可选：按边长裁剪邻居（两端同时丢，保持对称）
    if neighbor_cap and neighbor_cap > 0:
        candidates = {}
        for u, v in edge_set:
            du = _distance(pos[u], pos[v])
            candidates.setdefault(u, []).append((du, v))
            candidates.setdefault(v, []).append((du, u))
        kept = set()
        for vertex, items in candidates.items():
            items.sort(key=lambda item: (item[0], item[1]))
            for _dist, other in items[:neighbor_cap]:
                kept.add((vertex, other) if vertex < other else (other, vertex))
        edge_set = kept

    # 3) 每个顶点的邻居表（升序 → 输出可复现）
    neighbors = {v: [] for v in range(vertex_count)}
    for u, v in edge_set:
        neighbors[u].append(v)
        neighbors[v].append(u)
    for vertex in neighbors:
        neighbors[vertex].sort()

    edges = []
    adj_records = []
    for vertex in range(vertex_count):
        start = len(edges)
        for other in neighbors[vertex]:
            edges.append((other, _distance(pos[vertex], pos[other])))
        adj_records.append((len(neighbors[vertex]), start))

    report = validate_adjacency(adj_records, edges, vertex_count)
    return adj_records, edges, report


def _distance(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    dz = a[2] - b[2]
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def validate_adjacency(adj_records, edges, vertex_count):
    """校验邻接网的三条硬性质，返回报告字典。

    1. **对称**：A→B 存在 ⟺ B→A 存在
    2. **无自环 / 无重复**
    3. **边界干净**：邻居号都在 [0, vertex_count)，边表下标连续且不越界
    另外统计孤立顶点（邻居数 0）与最大邻居数，供导出侧提示。
    """
    problems = []
    pair_set = set()
    directed_seen = set()
    duplicate_pairs = 0
    self_loops = 0

    for vertex, (count, start) in enumerate(adj_records):
        if count < 0 or start < 0:
            problems.append(f"顶点 {vertex}: count/start 为负 ({count}, {start})")
            continue
        if start + count > len(edges):
            problems.append(
                f"顶点 {vertex}: 边区间 [{start}, {start + count}) 越界（边数 {len(edges)}）"
            )
            continue
        for offset in range(start, start + count):
            other = edges[offset][0]
            if other < 0 or other >= vertex_count:
                problems.append(f"顶点 {vertex}: 邻居 {other} 越界")
                continue
            if other == vertex:
                self_loops += 1
                continue
            # 重复边的判据是「同一个有向项出现两次」——无向边本来就会在两个端点的
            # 邻居表里各出现一次，那是正确行为，不是重复。
            if (vertex, other) in directed_seen:
                duplicate_pairs += 1
            else:
                directed_seen.add((vertex, other))
            pair = (vertex, other) if vertex < other else (other, vertex)
            pair_set.add(pair)

    # 对称性：每个规范化 pair 必须两个方向都出现过
    directed = set()
    for vertex, (count, start) in enumerate(adj_records):
        for offset in range(start, start + count):
            directed.add((vertex, edges[offset][0]))
    missing_reverse = [pair for pair in pair_set if (pair[1], pair[0]) not in directed]

    isolated = [v for v, (count, _s) in enumerate(adj_records) if count == 0]
    max_neighbors = max((count for count, _s in adj_records), default=0)

    if self_loops:
        problems.append(f"发现 {self_loops} 条自环边")
    if duplicate_pairs:
        problems.append(f"发现 {duplicate_pairs} 条重复边")
    if missing_reverse:
        problems.append(
            f"发现 {len(missing_reverse)} 条边不对称，例如 {missing_reverse[:5]}"
        )

    return {
        "vertex_count": int(vertex_count),
        "edge_count": len(edges),
        "unique_edge_count": len(pair_set),
        "max_neighbors": int(max_neighbors),
        "isolated_vertices": isolated,
        "symmetric": not missing_reverse,
        "problems": problems,
        "ok": not problems,
    }


def serialize_adjacency(adj_records, edges):
    """按 GPU 布局打包：返回 ``(adj_bytes, edge_bytes)``（小端 uint32）。

    adj_bytes  : 每顶点 ``(count, start, 0, 0)``
    edge_bytes : 每边   ``(neighbor, asuint(rest_length), 0, 0)``
    """
    adj_bytes = bytearray()
    for count, start in adj_records:
        adj_bytes += struct.pack("<4I", int(count), int(start), 0, 0)

    edge_bytes = bytearray()
    for neighbor, rest_length in edges:
        packed_float = struct.unpack("<I", struct.pack("<f", float(rest_length)))[0]
        edge_bytes += struct.pack("<4I", int(neighbor), packed_float, 0, 0)
    return bytes(adj_bytes), bytes(edge_bytes)


def format_report(report, label: str = "") -> str:
    """一行为主的可读报告（导出时打进控制台）。"""
    head = f"[软体M1] {label}" if label else "[软体M1]"
    text = (
        f"{head} 顶点 {report['vertex_count']} 边 {report['edge_count']}"
        f"（去重后 {report['unique_edge_count']}）最大邻居 {report['max_neighbors']}"
        f" 孤立顶点 {len(report['isolated_vertices'])}"
    )
    if not report["ok"]:
        text += "  ❌ " + "；".join(report["problems"][:3])
    return text
