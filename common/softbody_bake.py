"""M1-2：把导出侧的顶点/索引字节直接烘成软体邻接 buffer。

这一层只做"字节 → 数据 → 字节"，**不碰 bpy、不碰文件**，所以可以拿真实导出的
`*-Position.buf` / `*-Index.buf` 离线跑，也能被单测完整覆盖。

约定（与 TheHerta4 导出的 Position 类别缓冲一致）：

* Position 缓冲是交错布局，**前 12 字节 = xyz (float32×3)**，后面还有 normal /
  tangent 等；stride 由 d3d11_game_type 给出（绝区零一般是 40）。
* 索引缓冲由调用方给出格式（`u32` / `u16`），导出的 `LOD0.*-Index.buf` 是 u32。

产物与 `softbody_adjacency` 一致：

    sb_adj_<ib>.buf     每顶点 uint4 = (邻居数, 边起始, 0, 0)
    sb_edges_<ib>.buf   每条边 uint4 = (邻居号, asuint(静止长度), 0, 0)
"""

import struct

try:  # 正常包内导入
    from .softbody_adjacency import (
        SoftBodyAdjacencyError,
        build_adjacency,
        format_report,
        serialize_adjacency,
    )
except ImportError:  # pragma: no cover - 单测按路径加载时的兜底
    import importlib.util
    from pathlib import Path

    _spec = importlib.util.spec_from_file_location(
        "softbody_adjacency_fallback",
        Path(__file__).resolve().parent / "softbody_adjacency.py",
    )
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    SoftBodyAdjacencyError = _mod.SoftBodyAdjacencyError
    build_adjacency = _mod.build_adjacency
    format_report = _mod.format_report
    serialize_adjacency = _mod.serialize_adjacency


class SoftBodyBakeError(ValueError):
    """烘焙输入不合法（顶层捕获后可中止导出或跳过该部件）。"""


def parse_positions(payload: bytes, stride: int, offset: int = 0):
    """从交错顶点缓冲里取出 xyz 列表。"""
    if stride <= 0:
        raise SoftBodyBakeError(f"顶点 stride 非法: {stride}")
    if offset < 0 or offset + 12 > stride:
        raise SoftBodyBakeError(
            f"位置偏移 {offset} 超出 stride {stride}（至少要放下 12 字节 xyz）"
        )
    count = len(payload) // stride
    if count <= 0:
        raise SoftBodyBakeError("顶点缓冲为空")
    return [
        struct.unpack_from("<3f", payload, index * stride + offset)
        for index in range(count)
    ]


def parse_indices(payload: bytes, index_format: str = "u32"):
    """解析索引缓冲；长度不是整元素倍数时直接报错（宁可失败也不静默截断）。"""
    fmt = (index_format or "u32").lower()
    if fmt in ("u32", "r32_uint", "dxgi_format_r32_uint"):
        size, code = 4, "I"
    elif fmt in ("u16", "r16_uint", "dxgi_format_r16_uint"):
        size, code = 2, "H"
    else:
        raise SoftBodyBakeError(f"不支持的索引格式: {index_format!r}")

    if len(payload) % size != 0:
        raise SoftBodyBakeError(
            f"索引缓冲长度 {len(payload)} 不是 {size} 的整数倍"
        )
    count = len(payload) // size
    if count == 0:
        raise SoftBodyBakeError("索引缓冲为空")
    return list(struct.unpack_from("<%d%s" % (count, code), payload, 0))


def bake_softbody_adjacency(
    position_payload: bytes,
    position_stride: int,
    index_payload: bytes,
    index_format: str = "u32",
    *,
    position_offset: int = 0,
    neighbor_cap: int = 0,
):
    """烘焙一个部件的软体邻接网。

    返回 ``(adj_bytes, edge_bytes, report)``；`report["ok"]` 为假时调用方应当
    拒绝该部件（并打印 `format_report` 的内容），不要产出半成品 buffer。
    """
    positions = parse_positions(position_payload, position_stride, position_offset)
    indices = parse_indices(index_payload, index_format)

    if len(indices) % 3 != 0:
        raise SoftBodyBakeError(
            f"索引数 {len(indices)} 不是 3 的倍数，无法组成三角形"
        )
    triangles = [
        (indices[i], indices[i + 1], indices[i + 2])
        for i in range(0, len(indices), 3)
    ]

    adj_records, edges, report = build_adjacency(
        positions, triangles, neighbor_cap=neighbor_cap
    )
    adj_bytes, edge_bytes = serialize_adjacency(adj_records, edges)
    return adj_bytes, edge_bytes, report
