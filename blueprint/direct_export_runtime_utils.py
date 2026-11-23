import os

import numpy as np


def iter_drawib_models(exporter):
    # 直出导出器在不同游戏实现里可能暴露 list 或 dict，两种入口都兼容。
    drawib_model_list = getattr(exporter, "drawib_model_list", None)
    if drawib_model_list is not None:
        return list(drawib_model_list)

    drawib_drawibmodel_dict = getattr(exporter, "drawib_drawibmodel_dict", None)
    if drawib_drawibmodel_dict is not None:
        return list(drawib_drawibmodel_dict.values())

    return []


def get_model_vertex_count(drawib_model) -> int:
    if hasattr(drawib_model, "draw_number"):
        return int(drawib_model.draw_number)
    if hasattr(drawib_model, "vertex_count"):
        return int(drawib_model.vertex_count)
    if hasattr(drawib_model, "mesh_vertex_count"):
        return int(drawib_model.mesh_vertex_count)
    return 0


def normalize_runtime_name(name: str) -> str:
    if not name:
        return ""
    if name.endswith("_copy"):
        return name[:-5]
    return name


def extract_position_bytes_by_indices(base_bytes: bytes, position_stride: int, export_indices: np.ndarray) -> bytes:
    # 用 numpy 直接做行切片，避免 Python 循环逐顶点拷贝。
    if export_indices.size == 0 or position_stride <= 0 or not base_bytes:
        return b""

    base_array = np.frombuffer(base_bytes, dtype=np.uint8)
    if base_array.size % position_stride != 0:
        raise ValueError(
            f"Position 缓冲区大小与步长不匹配: size={base_array.size}, stride={position_stride}"
        )

    row_view = base_array.reshape(-1, position_stride)
    normalized_indices = np.asarray(export_indices, dtype=np.int64)
    return row_view[normalized_indices].tobytes()


def apply_position_override_in_place(
    state_bytes: bytearray,
    position_bytes: bytes,
    export_indices: np.ndarray,
    position_stride: int,
):
    # 直接把目标 Position 行覆盖回状态缓冲，避免局部 Python 切片循环。
    expected_bytes = int(export_indices.size) * position_stride
    if len(position_bytes) != expected_bytes:
        raise ValueError(
            f"Position 覆盖大小不匹配: 期望={expected_bytes}, 实际={len(position_bytes)}"
        )

    if export_indices.size == 0 or position_stride <= 0:
        return

    state_array = np.frombuffer(state_bytes, dtype=np.uint8)
    if state_array.size % position_stride != 0:
        raise ValueError(
            f"目标 Position 缓冲区大小与步长不匹配: size={state_array.size}, stride={position_stride}"
        )

    source_array = np.frombuffer(position_bytes, dtype=np.uint8)
    if source_array.size % position_stride != 0:
        raise ValueError(
            f"源 Position 缓冲区大小与步长不匹配: size={source_array.size}, stride={position_stride}"
        )

    state_rows = state_array.reshape(-1, position_stride)
    source_rows = source_array.reshape(-1, position_stride)
    normalized_indices = np.asarray(export_indices, dtype=np.int64)
    state_rows[normalized_indices] = source_rows


def apply_full_vertex_override_in_place(
    state_bytes: bytearray,
    vertex_bytes: bytes,
    export_indices: np.ndarray,
    vertex_stride: int,
):
    expected_bytes = int(export_indices.size) * vertex_stride
    if len(vertex_bytes) != expected_bytes:
        raise ValueError(
            f"完整顶点覆盖大小不匹配: 期望={expected_bytes}, 实际={len(vertex_bytes)}"
        )
    if export_indices.size == 0 or vertex_stride <= 0:
        return
    state_array = np.frombuffer(state_bytes, dtype=np.uint8)
    source_array = np.frombuffer(vertex_bytes, dtype=np.uint8)
    if state_array.size % vertex_stride != 0 or source_array.size % vertex_stride != 0:
        raise ValueError(
            f"完整顶点缓冲区步长不匹配: state={state_array.size}, source={source_array.size}, stride={vertex_stride}"
        )
    state_rows = state_array.reshape(-1, vertex_stride)
    source_rows = source_array.reshape(-1, vertex_stride)
    normalized_indices = np.asarray(export_indices, dtype=np.int64)
    state_rows[normalized_indices] = source_rows


def _submesh_position_stride(submesh_model) -> int:
    game_type = getattr(submesh_model, "d3d11_game_type", None)
    stride_dict = getattr(game_type, "CategoryStrideDict", {}) or {}
    try:
        return int(stride_dict.get("Position", 0) or 0)
    except (TypeError, ValueError):
        return 0


def assemble_drawib_position_bytes(folder_path: str, submesh_models) -> tuple[bytes, int]:
    """按 DrawIB 的子网格顺序拼接各子网格的 `<unique_str>-Position.buf`。

    为什么需要：`DrawIBModel._assemble_object_export_context_map` 给出的 `export_indices`
    是 **DrawIB 级**（每个子网格都加上了前面子网格的顶点数偏移，与
    `_assemble_category_buffers` 拼接出来的 DrawIB 缓冲一致）。但逐子网格写盘的游戏
    （EFMI 多 LOD：`LOD0.xxx-Position.buf` / `LOD1.xxx-Position.buf`）在磁盘上**没有**
    DrawIB 级 Position 文件，按哈希前缀解析只会命中其中一个子网格的文件 —— 用 DrawIB 级
    索引去采样它就会越界（实测：`index 2179 is out of bounds for axis 0 with size 2179`，
    2179 正好是第二个子网格的起点）。

    返回 (拼接字节, 子网格数)；无法安全拼接时返回 (b"", 0)，由调用方回退到
    「按哈希解析单个文件」的旧路径：
    - 子网格数 < 2（单个子网格本来就是一个文件，索引空间天然一致）；
    - 任一子网格缺少 `<unique_str>-Position.buf`（合并 IB 的游戏按 DrawIB 命名，走回退）；
    - 各子网格 Position 步长不一致（混合步长的缓冲无法用单一 stride 描述行）。
    """
    normalized_folder = str(folder_path or "").strip()
    if not normalized_folder or not os.path.isdir(normalized_folder):
        return b"", 0

    paths: list[str] = []
    strides: set[int] = set()
    for submesh_model in submesh_models or ():
        unique_str = str(getattr(submesh_model, "unique_str", "") or "").strip()
        if not unique_str:
            return b"", 0
        candidate = os.path.join(normalized_folder, unique_str + "-Position.buf")
        if not os.path.isfile(candidate):
            return b"", 0
        stride = _submesh_position_stride(submesh_model)
        if stride <= 0 or os.path.getsize(candidate) % stride != 0:
            return b"", 0
        strides.add(stride)
        paths.append(candidate)

    if len(paths) < 2 or len(strides) != 1:
        return b"", 0

    chunks = []
    for path in paths:
        with open(path, "rb") as file_obj:
            chunks.append(file_obj.read())
    return b"".join(chunks), len(paths)
