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
