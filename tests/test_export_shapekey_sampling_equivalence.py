"""直出形态键前处理记录采样：优化实现与旧实现逐项等价（产物 / 日志 / 异常）。

优化点（`blueprint/direct_export_shapekey_sampling_mixin.py`）：
- object_context / 索引映射 / 记录候选名按对象缓存（同一对象会在大量槽位重复出现）；
- identity 判断改为「只比较会被覆写的字节」，不再无条件组装整块 Position bytes；
- base 坐标提取与 Position 写入改向量化（异常布局回退原逐顶点路径）。

本测试内联优化前实现作为参考，用同一份输入逐项比对：
覆盖 identity / 有差异 / 缺记录 / 缺 context / 缺元素定义 / 空索引 / 多槽共享同一对象 /
多候选记录 / float16 / 4 分量 / 带偏移 stride / NaN 与 -0.0 / 异常检查顺序。
"""

import ast
import importlib.util
import re
import sys
import types
import unittest
from collections import OrderedDict, defaultdict
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_export_shapekey_sampling_equivalence_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_LOG_MESSAGES = []


def _log_info(message, *_args, **_kwargs):
    _LOG_MESSAGES.append(message)


sys.modules.setdefault(
    "bpy",
    types.SimpleNamespace(
        data=types.SimpleNamespace(objects={}),
        types=types.SimpleNamespace(Object=object),
    ),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(enable_non_mirror_workflow=lambda: False),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        SRMI="SRMI", GIMI="GIMI", HIMI="HIMI", YYSLS="YYSLS",
        IdentityV="IdentityV", SnowBreak="SnowBreak", EFMI="EFMI", HTMI="HTMI",
    ),
)
_install_module(f"{PKG}.utils.log_utils", LOG=types.SimpleNamespace(info=_log_info, warning=lambda *_a, **_k: None))
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace(select_obj=lambda *_a, **_k: None))
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(transform_apply_preserve_shape_keys=lambda *_a, **_k: None),
)
_install_module(f"{PKG}.utils.export_utils", ExportUtils=types.SimpleNamespace())


class _FakeBlueprintExportHelper:
    records = {}

    @staticmethod
    def get_direct_shapekey_position_records():
        return _FakeBlueprintExportHelper.records


_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=_FakeBlueprintExportHelper)
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace())


def _extract_position_bytes_by_indices(base_bytes, position_stride, export_indices):
    return np.frombuffer(base_bytes, dtype=np.uint8).reshape(-1, position_stride)[
        np.asarray(export_indices, dtype=np.int64)
    ].tobytes()


_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    extract_position_bytes_by_indices=_extract_position_bytes_by_indices,
    normalize_runtime_name=lambda name: name[:-5] if str(name).endswith("_copy") else str(name or ""),
)


class _ShapeKeyDirectExportError(RuntimeError):
    pass


_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=_ShapeKeyDirectExportError,
)

_module_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_sampling_mixin.py"
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_sampling_mixin",
    _module_path,
)
sampling_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sampling_module
_spec.loader.exec_module(sampling_module)


# ---------------------------------------------------------------------------
# 等价的假节点：8 位十六进制哈希，可选 "-xxxx" 段；前缀取 '-' 前。
# ---------------------------------------------------------------------------
def _fake_extract_hash_from_name(obj_name):
    if not isinstance(obj_name, str):
        return None
    match = re.match(r"^([0-9a-f]{8}(?:-[0-9a-f]{2,4})?)", obj_name)
    return match.group(1) if match else None


class _Node:
    @staticmethod
    def _extract_hash_from_name(obj_name):
        return _fake_extract_hash_from_name(obj_name)

    @staticmethod
    def _extract_hash_prefix(hash_val):
        if not hash_val:
            return None
        return str(hash_val).split("-")[0]


class _PositionElement:
    def __init__(self, fmt="R32G32B32_FLOAT", byte_width=12, offset=0):
        self.Format = fmt
        self.ByteWidth = byte_width
        self.AlignedByteOffset = offset
        self.Category = "Position"
        self.ElementName = "POSITION"


class _GameType:
    def __init__(self, position_element=None):
        self.D3D11ElementList = [position_element] if position_element is not None else []
        self.ElementNameD3D11ElementDict = {}


_EXPORT_MATRIX = np.asarray(
    (
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
        (0.0, -1.0, 0.0),
    ),
    dtype=np.float32,
)


class _Harness(sampling_module.DirectShapeKeySamplingMixin):
    """测试桩：对象上下文 / 候选名 / 坐标变换全部可控，并记录调用次数。"""

    def __init__(self, object_contexts, record_candidates=None):
        self.node = _Node()
        self.merged_name_members = {}
        self._object_contexts = object_contexts
        self._record_candidates = record_candidates or {}
        self.context_resolution_calls = []
        self.candidate_name_calls = []
        self.convert_coords_calls = 0
        self.convert_deltas_calls = 0

    def _resolve_object_export_context_with_merged_members(self, _runtime_info, obj_name):
        self.context_resolution_calls.append(obj_name)
        return self._object_contexts.get(obj_name)

    def _iter_record_candidate_names(self, obj_name, _object_context, _source_object_map):
        self.candidate_name_calls.append(obj_name)
        return list(self._record_candidates.get(obj_name, [obj_name]))

    def _convert_position_coords_for_export(self, sampled_coords):
        self.convert_coords_calls += 1
        coords = np.asarray(sampled_coords, dtype=np.float32)
        if coords.size == 0:
            return coords
        return coords @ _EXPORT_MATRIX.T

    def _convert_position_deltas_for_export(self, sampled_deltas):
        self.convert_deltas_calls += 1
        return self._convert_position_coords_for_export(sampled_deltas)


class _CountingHarness(_Harness):
    def __init__(self, object_contexts, record_candidates=None):
        super().__init__(object_contexts, record_candidates)
        self.compose_calls = 0

    def _compose_position_bytes_with_base_slice(self, *args, **kwargs):
        self.compose_calls += 1
        return super()._compose_position_bytes_with_base_slice(*args, **kwargs)


# ---------------------------------------------------------------------------
# 优化前实现（逐字内联），作为对照参考。
# ---------------------------------------------------------------------------
def _old_extract_position_coords_from_base_slice(harness, base_slice, position_element, position_stride=12):
    position_stride = int(position_stride)
    if position_stride <= 0 or len(base_slice) % position_stride != 0:
        raise _ShapeKeyDirectExportError(
            f"POSITION 模板大小不匹配: stride={position_stride}, actual={len(base_slice)}"
        )

    vertex_count = int(len(base_slice) / position_stride)
    offset, byte_width, dtype, _item_size, component_count = harness._position_element_layout(
        position_element,
        position_stride,
    )
    coords = np.zeros((vertex_count, 3), dtype=np.float32)
    for vertex_index in range(vertex_count):
        src_start = vertex_index * position_stride + offset
        src_end = src_start + byte_width
        values = np.frombuffer(base_slice[src_start:src_end], dtype=dtype, count=component_count)
        coords[vertex_index] = values[:3].astype(np.float32)
    return coords


def _old_compose_position_bytes_with_base_slice(
    harness,
    sampled_coords,
    base_slice,
    position_element,
    position_stride=12,
    coords_are_export_space=False,
):
    sampled_coords = np.asarray(sampled_coords, dtype=np.float32)
    if sampled_coords.size == 0:
        return b""
    if not coords_are_export_space:
        sampled_coords = harness._convert_position_coords_for_export(sampled_coords)

    vertex_count = int(sampled_coords.shape[0])
    expected_size = vertex_count * int(position_stride)
    if len(base_slice) != expected_size:
        raise _ShapeKeyDirectExportError(
            f"POSITION 模板大小不匹配: expected={expected_size}, actual={len(base_slice)}"
        )

    offset, byte_width, dtype, item_size, component_count = harness._position_element_layout(
        position_element,
        int(position_stride),
    )
    if dtype == np.float16:
        component_count = max(4, component_count)
        encoded_values = np.zeros((vertex_count, component_count), dtype=np.float16)
        encoded_values[:, :3] = sampled_coords.astype(np.float16)
        if component_count > 3:
            encoded_values[:, 3] = 1.0
        encoded_bytes = encoded_values.tobytes()
        encoded_stride = component_count * item_size
    else:
        component_count = max(3, component_count)
        encoded_values = np.zeros((vertex_count, component_count), dtype=np.float32)
        encoded_values[:, :3] = sampled_coords
        if component_count > 3:
            encoded_values[:, 3] = 1.0
        encoded_bytes = encoded_values.tobytes()
        encoded_stride = component_count * item_size

    output = bytearray(base_slice)
    for vertex_index in range(vertex_count):
        src_start = vertex_index * encoded_stride
        src_end = src_start + byte_width
        dst_start = vertex_index * position_stride + offset
        dst_end = dst_start + byte_width
        output[dst_start:dst_end] = encoded_bytes[src_start:src_end]
    return bytes(output)


def _old_compose_position_bytes_from_recorded_coords(
    harness,
    sampled_coords,
    sampled_basis_coords,
    base_slice,
    position_element,
    position_stride=12,
):
    sampled_coords = np.asarray(sampled_coords, dtype=np.float32)
    if sampled_basis_coords is None:
        return _old_compose_position_bytes_with_base_slice(
            harness,
            sampled_coords,
            base_slice,
            position_element,
            position_stride=position_stride,
        )

    sampled_basis_coords = np.asarray(sampled_basis_coords, dtype=np.float32)
    if sampled_basis_coords.shape != sampled_coords.shape:
        raise _ShapeKeyDirectExportError(
            f"Basis 记录大小不匹配: basis={sampled_basis_coords.shape}, shape={sampled_coords.shape}"
        )

    sampled_deltas = sampled_coords - sampled_basis_coords
    export_deltas = harness._convert_position_deltas_for_export(sampled_deltas)
    base_coords = _old_extract_position_coords_from_base_slice(
        harness,
        base_slice,
        position_element,
        position_stride=position_stride,
    )
    target_coords = base_coords + np.asarray(export_deltas, dtype=np.float32)
    return _old_compose_position_bytes_with_base_slice(
        harness,
        target_coords,
        base_slice,
        position_element,
        position_stride=position_stride,
        coords_are_export_space=True,
    )


def _old_resolve_recorded_shape_key_data(harness, obj_name, shapekey_name, object_context, source_object_map):
    records = _FakeBlueprintExportHelper.get_direct_shapekey_position_records()
    if not records:
        return None

    for candidate_name in harness._iter_record_candidate_names(obj_name, object_context, source_object_map):
        record = records.get(candidate_name)
        if not record:
            continue
        coords = (record.get("shape_keys", {}) or {}).get(shapekey_name)
        basis_coords = record.get("basis_coords")
        loop_vertex_indices = record.get("loop_vertex_indices")
        if coords is not None and loop_vertex_indices is not None:
            return coords, basis_coords, loop_vertex_indices, candidate_name
    return None


def _old_build_slot_position_overrides(harness, slot_to_name_to_objects, calculated_ranges, runtime_infos, source_object_map):
    slot_position_overrides = defaultdict(dict)
    prefix_to_runtime_info = {}
    base_slice_cache = {}
    skip_reason_counts = defaultdict(lambda: defaultdict(int))

    for logical_hash, runtime_info in runtime_infos.items():
        logical_prefix = harness.node._extract_hash_prefix(logical_hash)
        if logical_prefix:
            prefix_to_runtime_info[logical_prefix] = runtime_info

    for slot_index, names_data in slot_to_name_to_objects.items():
        for shapekey_name, objects in names_data.items():
            for obj_name in OrderedDict.fromkeys(objects).keys():
                if obj_name not in calculated_ranges:
                    continue

                obj_hash = harness.node._extract_hash_from_name(obj_name)
                obj_prefix = harness.node._extract_hash_prefix(obj_hash) if obj_hash else None
                runtime_info = prefix_to_runtime_info.get(obj_prefix)
                if runtime_info is None:
                    skip_reason_counts[obj_prefix]["missing_runtime_info"] += 1
                    continue

                object_context = harness._resolve_object_export_context_with_merged_members(runtime_info, obj_name)
                if object_context is None:
                    skip_reason_counts[obj_prefix]["missing_object_context"] += 1
                    continue

                recorded_data = _old_resolve_recorded_shape_key_data(
                    harness,
                    obj_name,
                    shapekey_name,
                    object_context,
                    source_object_map,
                )
                if recorded_data is None:
                    skip_reason_counts[obj_prefix]["missing_record"] += 1
                    continue

                coords, basis_coords, loop_vertex_indices, _record_name = recorded_data
                local_loop_indices = np.asarray(object_context.get("local_loop_indices", []), dtype=np.int32)
                export_indices = np.asarray(object_context.get("export_indices", []), dtype=np.int32)
                if local_loop_indices.size == 0 or export_indices.size == 0:
                    skip_reason_counts[obj_prefix]["empty_indices"] += 1
                    continue

                loop_vertex_indices = np.asarray(loop_vertex_indices, dtype=np.int32)
                if int(local_loop_indices.max()) >= len(loop_vertex_indices):
                    raise _ShapeKeyDirectExportError(
                        f"槽位 {slot_index}: 物体 '{obj_name}' 的形态键记录 loop 映射越界"
                    )
                sampled_vertex_indices = loop_vertex_indices[local_loop_indices]
                coords = np.asarray(coords, dtype=np.float32)
                if sampled_vertex_indices.size and int(sampled_vertex_indices.max()) >= len(coords):
                    raise _ShapeKeyDirectExportError(
                        f"槽位 {slot_index}: 物体 '{obj_name}' 的形态键记录 vertex 映射越界"
                    )
                sampled_basis_coords = None
                if basis_coords is not None:
                    basis_coords = np.asarray(basis_coords, dtype=np.float32)
                    if sampled_vertex_indices.size and int(sampled_vertex_indices.max()) >= len(basis_coords):
                        raise _ShapeKeyDirectExportError(
                            f"槽位 {slot_index}: 物体 '{obj_name}' 的 Basis 记录 vertex 映射越界"
                        )
                    sampled_basis_coords = basis_coords[sampled_vertex_indices]

                d3d11_game_type = object_context.get("d3d11_game_type")
                position_element = harness._get_position_element_from_game_type(d3d11_game_type)
                if position_element is None:
                    skip_reason_counts[obj_prefix]["missing_position_element"] += 1
                    continue

                export_indices_key = tuple(int(index) for index in export_indices.tolist())
                base_slice = base_slice_cache.get((runtime_info["logical_hash"], export_indices_key))
                if base_slice is None:
                    base_slice = _extract_position_bytes_by_indices(
                        runtime_info["base_bytes"],
                        runtime_info["position_stride"],
                        export_indices,
                    )
                    base_slice_cache[(runtime_info["logical_hash"], export_indices_key)] = base_slice

                position_bytes = _old_compose_position_bytes_from_recorded_coords(
                    harness,
                    coords[sampled_vertex_indices],
                    sampled_basis_coords,
                    base_slice,
                    position_element,
                    position_stride=runtime_info["position_stride"],
                )
                expected_bytes = export_indices.size * runtime_info["position_stride"]
                if len(position_bytes) != expected_bytes:
                    raise _ShapeKeyDirectExportError(
                        f"槽位 {slot_index}: 物体 '{obj_name}' 的记录 Position 大小不匹配，"
                        f"期望={expected_bytes}，实际={len(position_bytes)}"
                    )

                export_indices_key = tuple(int(index) for index in export_indices.tolist())
                base_slice = base_slice_cache.get((runtime_info["logical_hash"], export_indices_key))
                if base_slice is None:
                    base_slice = _extract_position_bytes_by_indices(
                        runtime_info["base_bytes"],
                        runtime_info["position_stride"],
                        export_indices,
                    )
                    base_slice_cache[(runtime_info["logical_hash"], export_indices_key)] = base_slice

                if position_bytes == base_slice:
                    slot_position_overrides[slot_index][obj_name] = {
                        "position_bytes": base_slice,
                        "export_indices": export_indices,
                        "is_identity": True,
                    }
                    skip_reason_counts[obj_prefix]["same_as_base"] += 1
                    continue

                slot_position_overrides[slot_index][obj_name] = {
                    "position_bytes": position_bytes,
                    "export_indices": export_indices,
                }
                skip_reason_counts[obj_prefix]["active_overrides"] += 1

    for hash_prefix, reason_counts in skip_reason_counts.items():
        _log_info(f"直出形态键: 前处理记录采样统计 {hash_prefix} -> {dict(reason_counts)}")

    return slot_position_overrides


# ---------------------------------------------------------------------------
# 数据构造工具
# ---------------------------------------------------------------------------
def _context(local_loop_indices, export_indices, position_element):
    return {
        "local_loop_indices": np.asarray(local_loop_indices, dtype=np.int32),
        "export_indices": np.asarray(export_indices, dtype=np.int32),
        "d3d11_game_type": _GameType(position_element),
    }


def _record(basis_coords, shape_keys, loop_vertex_indices):
    record = {
        "loop_vertex_indices": np.asarray(loop_vertex_indices, dtype=np.int32),
        "shape_keys": {
            name: np.asarray(coords, dtype=np.float32) for name, coords in shape_keys.items()
        },
    }
    if basis_coords is not None:
        record["basis_coords"] = np.asarray(basis_coords, dtype=np.float32)
    return record


def _layout_base_bytes(coords, dtype, component_count, stride, offset, fourth=None):
    """按给定 D3D 布局把顶点坐标打包成基础 Position 缓冲（stride 可大于元素宽度）。"""
    coords = np.asarray(coords, dtype=dtype)
    vertex_count = coords.shape[0]
    values = np.zeros((vertex_count, component_count), dtype=dtype)
    values[:, :3] = coords
    if component_count > 3 and fourth is not None:
        values[:, 3] = fourth
    encoded = values.tobytes()
    row_bytes = component_count * np.dtype(dtype).itemsize
    output = bytearray()
    for vertex_index in range(vertex_count):
        row = bytearray(stride)
        row[offset:offset + row_bytes] = encoded[vertex_index * row_bytes:(vertex_index + 1) * row_bytes]
        output.extend(row)
    return bytes(output)


def _assert_override_entries_equal(test_case, new_overrides, old_overrides):
    test_case.assertEqual(list(new_overrides.keys()), list(old_overrides.keys()), "槽位键/顺序不一致")
    for slot_index, old_slot in old_overrides.items():
        new_slot = new_overrides[slot_index]
        test_case.assertEqual(list(new_slot.keys()), list(old_slot.keys()), f"槽位 {slot_index} 对象键/顺序不一致")
        for obj_name, old_entry in old_slot.items():
            new_entry = new_slot[obj_name]
            test_case.assertEqual(
                list(new_entry.keys()),
                list(old_entry.keys()),
                f"槽位 {slot_index} / {obj_name} 的 overrides 键不一致",
            )
            test_case.assertEqual(
                new_entry["position_bytes"],
                old_entry["position_bytes"],
                f"槽位 {slot_index} / {obj_name} 的 position_bytes 不一致",
            )
            test_case.assertEqual(
                bool(new_entry.get("is_identity", False)),
                bool(old_entry.get("is_identity", False)),
                f"槽位 {slot_index} / {obj_name} 的 is_identity 不一致",
            )
            np.testing.assert_array_equal(
                new_entry["export_indices"],
                old_entry["export_indices"],
                f"槽位 {slot_index} / {obj_name} 的 export_indices 不一致",
            )


def _parse_skip_logs(messages):
    parsed = {}
    for message in messages:
        match = re.match(r"直出形态键: 前处理记录采样统计 (.+) -> (\{.*\})$", message)
        if match:
            parsed[match.group(1)] = ast.literal_eval(match.group(2))
    return parsed


class ExportShapeKeySamplingEquivalenceTests(unittest.TestCase):
    def setUp(self):
        _FakeBlueprintExportHelper.records = {}
        _LOG_MESSAGES.clear()

    def tearDown(self):
        _FakeBlueprintExportHelper.records = {}
        _LOG_MESSAGES.clear()

    def _run_both(
        self,
        slot_to_name_to_objects,
        calculated_ranges,
        runtime_infos,
        contexts,
        records,
        record_candidates=None,
        harness_class=_Harness,
    ):
        _FakeBlueprintExportHelper.records = records

        new_harness = harness_class(contexts, record_candidates)
        _LOG_MESSAGES.clear()
        new_result = new_harness._build_slot_position_overrides_from_preprocess_records(
            slot_to_name_to_objects=slot_to_name_to_objects,
            calculated_ranges=calculated_ranges,
            runtime_infos=runtime_infos,
            source_object_map={},
        )
        new_logs = list(_LOG_MESSAGES)

        old_harness = _Harness(contexts, record_candidates)
        _LOG_MESSAGES.clear()
        old_result = _old_build_slot_position_overrides(
            old_harness,
            slot_to_name_to_objects,
            calculated_ranges,
            runtime_infos,
            {},
        )
        old_logs = list(_LOG_MESSAGES)
        return new_result, old_result, new_logs, old_logs, new_harness, old_harness

    # -- 1) identity / 有差异 / 多槽共享同一对象 ---------------------------------
    def test_identity_and_differential_match_old(self):
        base_vertices = np.asarray(
            [
                [0.5, -1.25, 3.0],
                [10.0, 2.0, -4.5],
                [-7.5, 8.25, 0.125],
                [1.0, 2.0, 3.0],
                [0.0, -0.0, 4.0],
            ],
            dtype=np.float32,
        )
        base_bytes = base_vertices.tobytes()
        element = _PositionElement()
        sampled_basis = base_vertices[:3]
        records = {
            "aaaaaaaa-Body_x0": _record(
                sampled_basis,
                {
                    "Smile": sampled_basis.copy(),
                    "Angry": sampled_basis + np.asarray([0.5, -1.0, 2.0], dtype=np.float32),
                },
                [0, 1, 2],
            )
        }
        contexts = {"aaaaaaaa-Body_x0": _context([0, 1, 2], [0, 1, 2], element)}
        runtime_infos = {
            "aaaaaaaa": {
                "logical_hash": "aaaaaaaa",
                "base_bytes": base_bytes,
                "position_stride": 12,
            }
        }
        slots = {
            0: {"Smile": ["aaaaaaaa-Body_x0"]},
            1: {"Angry": ["aaaaaaaa-Body_x0"]},
            2: {"Smile": ["aaaaaaaa-Body_x0", "aaaaaaaa-Body_x0"]},  # 重复对象去重
        }
        ranges = {"aaaaaaaa-Body_x0": (0, 2, "")}

        new_result, old_result, new_logs, old_logs, _, _ = self._run_both(
            slots, ranges, runtime_infos, contexts, records
        )
        _assert_override_entries_equal(self, new_result, old_result)
        self.assertEqual(new_logs, old_logs)

        self.assertTrue(new_result[0]["aaaaaaaa-Body_x0"].get("is_identity"))
        self.assertFalse(new_result[1]["aaaaaaaa-Body_x0"].get("is_identity"))
        self.assertEqual(
            new_result[0]["aaaaaaaa-Body_x0"]["position_bytes"],
            _extract_position_bytes_by_indices(base_bytes, 12, [0, 1, 2]),
        )
        self.assertEqual(new_result[0]["aaaaaaaa-Body_x0"]["position_bytes"],
                         new_result[2]["aaaaaaaa-Body_x0"]["position_bytes"])

    def test_identity_fast_path_does_not_compose_bytes(self):
        base_vertices = np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
        element = _PositionElement()
        contexts = {
            "aaaaaaaa-Identity_x0": _context([0, 1], [0, 1], element),
            "aaaaaaaa-Changed_x0": _context([0, 1], [0, 1], element),
        }
        records = {
            "aaaaaaaa-Identity_x0": _record(
                base_vertices,
                {"KeyA": base_vertices.copy()},
                [0, 1],
            ),
            "aaaaaaaa-Changed_x0": _record(
                base_vertices,
                {"KeyB": base_vertices + np.asarray([1.0, 0.0, 0.0], dtype=np.float32)},
                [0, 1],
            ),
        }
        runtime_infos = {
            "aaaaaaaa": {
                "logical_hash": "aaaaaaaa",
                "base_bytes": base_vertices.tobytes(),
                "position_stride": 12,
            }
        }
        slots = {
            0: {"KeyA": ["aaaaaaaa-Identity_x0"]},
            1: {"KeyB": ["aaaaaaaa-Changed_x0"]},
        }
        ranges = {"aaaaaaaa-Identity_x0": (0, 1, ""), "aaaaaaaa-Changed_x0": (0, 1, "")}

        harness = _CountingHarness(contexts)
        _FakeBlueprintExportHelper.records = records
        result = harness._build_slot_position_overrides_from_preprocess_records(
            slot_to_name_to_objects=slots,
            calculated_ranges=ranges,
            runtime_infos=runtime_infos,
            source_object_map={},
        )
        self.assertTrue(result[0]["aaaaaaaa-Identity_x0"].get("is_identity"))
        self.assertFalse(result[1]["aaaaaaaa-Changed_x0"].get("is_identity"))
        # identity 槽位不组装整块 bytes；只有确实有差异的对象才走组装。
        self.assertEqual(harness.compose_calls, 1)

    # -- 2) 缺 runtime / 缺 context / 缺记录 / 空索引 / 缺元素 / 不在范围 ---------
    def test_skip_reason_counts_and_order_match_old(self):
        base_vertices = np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
        element = _PositionElement()
        records = {
            "aaaaaaaa-context_x0": _record(base_vertices, {"Smile": base_vertices}, [0, 1]),
            "aaaaaaaa-nocontext_x0": _record(base_vertices, {"Smile": base_vertices}, [0, 1]),
            "aaaaaaaa-empty_x0": _record(base_vertices, {"Smile": base_vertices}, [0, 1]),
            "aaaaaaaa-noelement_x0": _record(base_vertices, {"Smile": base_vertices}, [0, 1]),
        }
        contexts = {
            "aaaaaaaa-context_x0": _context([0, 1], [0, 1], element),
            "aaaaaaaa-norecord_x0": _context([0, 1], [0, 1], element),
            "aaaaaaaa-empty_x0": _context([], [], element),
            "aaaaaaaa-noelement_x0": _context([0, 1], [0, 1], None),
            "aaaaaaaa-skipped_x0": _context([0, 1], [0, 1], element),
        }
        runtime_infos = {
            "aaaaaaaa": {
                "logical_hash": "aaaaaaaa",
                "base_bytes": base_vertices.tobytes(),
                "position_stride": 12,
            }
        }
        slots = {
            0: {
                "Smile": [
                    "aaaaaaaa-context_x0",      # 有记录 -> identity
                    "aaaaaaaa-nocontext_x0",    # 缺 context
                    "aaaaaaaa-norecord_x0",     # 缺记录
                    "aaaaaaaa-empty_x0",        # 空索引
                    "aaaaaaaa-noelement_x0",    # 缺 POSITION 元素
                    "eeeeeeee-Noruntime_x0",    # 缺 runtime info
                    "NoHashObject",             # 无哈希 -> 前缀 None
                    "aaaaaaaa-skipped_x0",      # 不在 calculated_ranges
                ]
            }
        }
        ranges = {
            "aaaaaaaa-context_x0": (0, 1, ""),
            "aaaaaaaa-nocontext_x0": (0, 1, ""),
            "aaaaaaaa-norecord_x0": (0, 1, ""),
            "aaaaaaaa-empty_x0": (0, 1, ""),
            "aaaaaaaa-noelement_x0": (0, 1, ""),
            "eeeeeeee-Noruntime_x0": (0, 1, ""),
            "NoHashObject": (0, 1, ""),
        }

        new_result, old_result, new_logs, old_logs, new_harness, _ = self._run_both(
            slots, ranges, runtime_infos, contexts, records
        )
        _assert_override_entries_equal(self, new_result, old_result)
        self.assertEqual(new_logs, old_logs)

        skip_stats = _parse_skip_logs(new_logs)
        self.assertEqual(
            skip_stats["aaaaaaaa"],
            {
                "missing_object_context": 1,
                "missing_record": 1,
                "empty_indices": 1,
                "missing_position_element": 1,
                "same_as_base": 1,
            },
        )
        self.assertEqual(skip_stats["eeeeeeee"], {"missing_runtime_info": 1})
        self.assertEqual(skip_stats["None"], {"missing_runtime_info": 1})
        self.assertNotIn("aaaaaaaa-skipped_x0", new_harness.context_resolution_calls)
        self.assertNotIn("aaaaaaaa-nocontext_x0", new_harness.candidate_name_calls)
        self.assertIn("aaaaaaaa-context_x0", new_harness.candidate_name_calls)

    # -- 3) 多候选记录：按形态键逐个候选回溯 -----------------------------------
    def test_multi_candidate_records_match_old(self):
        base_vertices = np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]], dtype=np.float32)
        element = _PositionElement()
        basis = base_vertices[:2]
        records = {
            "alias-A": _record(
                basis,
                {"Angry": basis + np.asarray([0.5, 0.5, 0.5], dtype=np.float32)},
                [0, 1],
            ),
            "alias-B": _record(
                basis,
                {"Smile": basis.copy()},
                [0, 1, 2],
            ),
        }
        contexts = {"aaaaaaaa-Alias_x0": _context([0, 1], [0, 1], element)}
        candidates = {"aaaaaaaa-Alias_x0": ["alias-A", "alias-B"]}
        runtime_infos = {
            "aaaaaaaa": {
                "logical_hash": "aaaaaaaa",
                "base_bytes": base_vertices.tobytes(),
                "position_stride": 12,
            }
        }
        slots = {0: {"Smile": ["aaaaaaaa-Alias_x0"]}, 1: {"Angry": ["aaaaaaaa-Alias_x0"]}}
        ranges = {"aaaaaaaa-Alias_x0": (0, 1, "")}

        new_result, old_result, new_logs, old_logs, _, _ = self._run_both(
            slots, ranges, runtime_infos, contexts, records, candidates
        )
        _assert_override_entries_equal(self, new_result, old_result)
        self.assertEqual(new_logs, old_logs)
        self.assertTrue(new_result[0]["aaaaaaaa-Alias_x0"].get("is_identity"))
        self.assertFalse(new_result[1]["aaaaaaaa-Alias_x0"].get("is_identity"))

    # -- 4) 布局变体：float16 / 4 分量 / 带偏移 stride / NaN / -0.0 -------------
    def test_layout_variants_match_old(self):
        layouts = (
            ("R32G32B32_FLOAT", 12, 0, 12, np.float32, 3, None),
            ("R32G32B32A32_FLOAT", 16, 0, 16, np.float32, 4, 0.5),
            ("R32G32B32A32_FLOAT", 16, 0, 16, np.float32, 4, 1.0),
            ("R32G32B32_FLOAT", 12, 4, 16, np.float32, 3, None),
            ("R16G16B16A16_FLOAT", 8, 0, 8, np.float16, 4, 1.0),
            ("R16G16B16A16_FLOAT", 8, 0, 8, np.float16, 4, 0.5),
        )
        coords = np.asarray(
            [
                [0.5, -1.25, 3.0],
                [10.0, 2.0, -4.5],
                [-7.5, 8.25, 0.125],
            ],
            dtype=np.float32,
        )
        for fmt, byte_width, offset, stride, dtype, component_count, fourth in layouts:
            with self.subTest(fmt=fmt, byte_width=byte_width, offset=offset, stride=stride, fourth=fourth):
                element = _PositionElement(fmt, byte_width, offset)
                base_bytes = _layout_base_bytes(coords, dtype, component_count, stride, offset, fourth)
                harness = _Harness({})

                new_extracted = harness._extract_position_coords_from_base_slice(base_bytes, element, stride)
                old_extracted = _old_extract_position_coords_from_base_slice(harness, base_bytes, element, stride)
                np.testing.assert_array_equal(
                    np.asarray(new_extracted, dtype=np.float32).view(np.uint32),
                    np.asarray(old_extracted, dtype=np.float32).view(np.uint32),
                )

                for delta in (np.zeros(3, dtype=np.float32), np.asarray([0.25, -0.5, 1.5], dtype=np.float32)):
                    target = coords + delta
                    new_bytes = harness._compose_position_bytes_with_base_slice(target, base_bytes, element, stride)
                    old_bytes = _old_compose_position_bytes_with_base_slice(harness, target, base_bytes, element, stride)
                    self.assertEqual(new_bytes, old_bytes)
                    # coords_are_export_space 分支
                    new_export = harness._compose_position_bytes_with_base_slice(
                        target, base_bytes, element, stride, coords_are_export_space=True
                    )
                    old_export = _old_compose_position_bytes_with_base_slice(
                        harness, target, base_bytes, element, stride, coords_are_export_space=True
                    )
                    self.assertEqual(new_export, old_export)

                # 记录路径：basis 存在 / 不存在
                basis = coords.copy()
                shape = coords + np.asarray([0.5, -1.0, 2.0], dtype=np.float32)
                new_recorded = harness._compose_position_bytes_from_recorded_coords(
                    shape, basis, base_bytes, element, stride
                )
                old_recorded = _old_compose_position_bytes_from_recorded_coords(
                    harness, shape, basis, base_bytes, element, stride
                )
                self.assertEqual(new_recorded, old_recorded)
                new_no_basis = harness._compose_position_bytes_from_recorded_coords(
                    shape, None, base_bytes, element, stride
                )
                old_no_basis = _old_compose_position_bytes_from_recorded_coords(
                    harness, shape, None, base_bytes, element, stride
                )
                self.assertEqual(new_no_basis, old_no_basis)

    def test_identity_judgement_per_layout_matches_old(self):
        coords = np.asarray(
            [
                [0.5, -1.25, 3.0],
                [10.0, 2.0, -4.5],
                [-7.5, 8.25, 0.125],
            ],
            dtype=np.float32,
        )
        layouts = (
            ("R32G32B32_FLOAT", 12, 0, 12, np.float32, 3, None, True),
            ("R32G32B32A32_FLOAT", 16, 0, 16, np.float32, 4, 0.5, False),
            ("R32G32B32A32_FLOAT", 16, 0, 16, np.float32, 4, 1.0, True),
            ("R32G32B32_FLOAT", 12, 4, 16, np.float32, 3, None, True),
            ("R16G16B16A16_FLOAT", 8, 0, 8, np.float16, 4, 1.0, True),
            ("R16G16B16A16_FLOAT", 8, 0, 8, np.float16, 4, 0.5, False),
        )
        for fmt, byte_width, offset, stride, dtype, component_count, fourth, expected_identity in layouts:
            with self.subTest(fmt=fmt, byte_width=byte_width, offset=offset, stride=stride, fourth=fourth):
                element = _PositionElement(fmt, byte_width, offset)
                base_bytes = _layout_base_bytes(coords, dtype, component_count, stride, offset, fourth)
                obj_name = "aaaaaaaa-Layout_x0"
                context = _context([0, 1, 2], [0, 1, 2], element)
                records = {obj_name: _record(coords, {"Smile": coords.copy()}, [0, 1, 2])}
                runtime_infos = {
                    "aaaaaaaa": {
                        "logical_hash": "aaaaaaaa",
                        "base_bytes": base_bytes,
                        "position_stride": stride,
                    }
                }
                slots = {0: {"Smile": [obj_name]}}
                ranges = {obj_name: (0, 1, "")}

                new_result, old_result, new_logs, old_logs, _, _ = self._run_both(
                    slots, ranges, runtime_infos, {obj_name: context}, records
                )
                _assert_override_entries_equal(self, new_result, old_result)
                self.assertEqual(new_logs, old_logs)
                self.assertEqual(
                    bool(new_result[0][obj_name].get("is_identity")),
                    expected_identity,
                    f"布局 {fmt} stride={stride} offset={offset} 的 identity 判断与预期不符",
                )

    def test_compose_basis_shape_mismatch_matches_old(self):
        base_vertices = np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
        element = _PositionElement()
        harness = _Harness({})
        with self.assertRaises(_ShapeKeyDirectExportError) as new_error:
            harness._compose_position_bytes_from_recorded_coords(
                base_vertices,
                np.zeros((1, 3), dtype=np.float32),
                base_vertices.tobytes(),
                element,
                12,
            )
        with self.assertRaises(_ShapeKeyDirectExportError) as old_error:
            _old_compose_position_bytes_from_recorded_coords(
                harness,
                base_vertices,
                np.zeros((1, 3), dtype=np.float32),
                base_vertices.tobytes(),
                element,
                12,
            )
        self.assertEqual(str(new_error.exception), str(old_error.exception))

    def test_nan_and_negative_zero_identity_judgement_matches_old(self):
        # base 含 -0.0 / NaN，形状键与 Basis 完全相同（delta = 0）：
        # -0.0 + 0.0 = +0.0，字节与 base 不同 —— 旧实现判为“有差异”，这里必须一致。
        coords = np.asarray(
            [
                [-0.0, 0.0, 1.0],
                [np.nan, 2.0, 3.0],
                [4.0, 5.0, 6.0],
            ],
            dtype=np.float32,
        )
        base_bytes = coords.tobytes()
        element = _PositionElement()
        records = {"aaaaaaaa-Special_x0": _record(coords.copy(), {"Smile": coords.copy()}, [0, 1, 2])}
        contexts = {"aaaaaaaa-Special_x0": _context([0, 1, 2], [0, 1, 2], element)}
        runtime_infos = {
            "aaaaaaaa": {
                "logical_hash": "aaaaaaaa",
                "base_bytes": base_bytes,
                "position_stride": 12,
            }
        }
        ranges = {"aaaaaaaa-Special_x0": (0, 1, "")}
        slots = {0: {"Smile": ["aaaaaaaa-Special_x0"]}}

        new_result, old_result, new_logs, old_logs, _, _ = self._run_both(
            slots, ranges, runtime_infos, contexts, records
        )
        _assert_override_entries_equal(self, new_result, old_result)
        self.assertEqual(new_logs, old_logs)
        self.assertFalse(new_result[0]["aaaaaaaa-Special_x0"].get("is_identity"))

    # -- 5) 异常检查顺序 / 文案 / 槽位号 ---------------------------------------
    def test_error_checks_match_old(self):
        base_vertices = np.asarray(
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]],
            dtype=np.float32,
        )
        element = _PositionElement()
        cases = {
            "loop_overflow": {
                "context": _context([0, 5], [0, 1], element),
                "record": _record(base_vertices, {"Smile": base_vertices}, [0, 1]),
            },
            "vertex_overflow": {
                "context": _context([0, 2], [0, 1], element),
                "record": _record(base_vertices[:1], {"Smile": base_vertices[:1]}, [0, 1, 2]),
            },
            "basis_overflow": {
                "context": _context([0, 2], [0, 1], element),
                "record": _record(
                    np.zeros((2, 3), dtype=np.float32),
                    {"Smile": base_vertices[:3]},
                    [0, 1, 2],
                ),
            },
            "template_mismatch": {
                "context": _context([0, 1], [0, 1, 2], element),
                "record": _record(None, {"Smile": base_vertices}, [0, 1, 2]),
            },
        }
        for case_name, case in cases.items():
            with self.subTest(case=case_name):
                obj_name = "aaaaaaaa-Err_x0"
                runtime_infos = {
                    "aaaaaaaa": {
                        "logical_hash": "aaaaaaaa",
                        "base_bytes": base_vertices.tobytes(),
                        "position_stride": 12,
                    }
                }
                slots = {1: {"Other": [obj_name]}, 5: {"Smile": [obj_name]}}
                ranges = {obj_name: (0, 1, "")}

                _FakeBlueprintExportHelper.records = {obj_name: case["record"]}
                new_harness = _Harness({obj_name: case["context"]})
                with self.assertRaises(_ShapeKeyDirectExportError) as new_error:
                    new_harness._build_slot_position_overrides_from_preprocess_records(
                        slot_to_name_to_objects=slots,
                        calculated_ranges=ranges,
                        runtime_infos=runtime_infos,
                        source_object_map={},
                    )

                _FakeBlueprintExportHelper.records = {obj_name: case["record"]}
                old_harness = _Harness({obj_name: case["context"]})
                with self.assertRaises(_ShapeKeyDirectExportError) as old_error:
                    _old_build_slot_position_overrides(
                        old_harness,
                        slot_to_name_to_objects=slots,
                        calculated_ranges=ranges,
                        runtime_infos=runtime_infos,
                        source_object_map={},
                    )

                self.assertEqual(str(new_error.exception), str(old_error.exception), f"{case_name} 报错文案不一致")

    # -- 7) 随机批次对照（覆盖多布局 / 多对象 / 多槽 / 重复对象 / 缺记录） -------
    def test_randomized_batches_match_old(self):
        rng = np.random.default_rng(20261021)
        for round_index in range(15):
            with self.subTest(round=round_index):
                self._run_random_batch(rng)

    def _run_random_batch(self, rng):
        layouts = (
            ("R32G32B32_FLOAT", 12, 0, 12, np.float32, 3, None),
            ("R32G32B32A32_FLOAT", 16, 0, 16, np.float32, 4, 0.5),
            ("R32G32B32A32_FLOAT", 16, 0, 16, np.float32, 4, 1.0),
            ("R32G32B32_FLOAT", 12, 4, 16, np.float32, 3, None),
            ("R16G16B16A16_FLOAT", 8, 0, 8, np.float16, 4, 1.0),
            ("R16G16B16A16_FLOAT", 8, 0, 8, np.float16, 4, 0.25),
        )
        fmt, byte_width, offset, stride, dtype, component_count, fourth = layouts[
            int(rng.integers(0, len(layouts)))
        ]
        element = _PositionElement(fmt, byte_width, offset)
        vertex_count = int(rng.integers(3, 9))
        coords = rng.normal(size=(vertex_count, 3)).astype(np.float32)
        if rng.random() < 0.5:
            coords[0, 0] = -0.0
        if rng.random() < 0.25:
            coords[1 % vertex_count, 1] = np.nan
        base_bytes = _layout_base_bytes(coords, dtype, component_count, stride, offset, fourth)
        runtime_infos = {
            "aaaaaaaa": {
                "logical_hash": "aaaaaaaa",
                "base_bytes": base_bytes,
                "position_stride": stride,
            }
        }

        object_count = int(rng.integers(1, 5))
        contexts = {}
        records = {}
        ranges = {}
        obj_names = []
        for index in range(object_count):
            obj_name = f"aaaaaaaa-Part{index}_x0"
            obj_names.append(obj_name)
            export_count = int(rng.integers(1, min(3, vertex_count) + 1))
            export_indices = sorted(
                int(value) for value in rng.choice(vertex_count, size=export_count, replace=False)
            )
            local_loop_indices = [
                int(value) for value in rng.integers(0, export_count, size=export_count)
            ]
            contexts[obj_name] = _context(local_loop_indices, export_indices, element)
            ranges[obj_name] = (0, vertex_count - 1, "")
            if rng.random() < 0.85:
                basis = coords[[int(value) for value in rng.integers(0, vertex_count, size=vertex_count)]]
                if rng.random() < 0.4:
                    shape = basis.copy()
                else:
                    shape = basis + rng.normal(size=(vertex_count, 3)).astype(np.float32)
                loop_length = export_count + int(rng.integers(0, 2))
                loop_vertex_indices = [
                    int(value) for value in rng.integers(0, vertex_count, size=loop_length)
                ]
                if rng.random() < 0.2:
                    records[obj_name] = _record(None, {"Key": shape}, loop_vertex_indices)
                else:
                    records[obj_name] = _record(basis, {"Key": shape}, loop_vertex_indices)

        slots = {}
        for slot_index in range(int(rng.integers(2, 5))):
            selected = rng.choice(obj_names, size=int(rng.integers(1, len(obj_names) + 1)), replace=True).tolist()
            slots[slot_index] = {"Key": [str(name) for name in selected]}

        new_result, old_result, new_logs, old_logs, _, _ = self._run_both(
            slots, ranges, runtime_infos, contexts, records
        )
        _assert_override_entries_equal(self, new_result, old_result)
        self.assertEqual(new_logs, old_logs)

    # -- 6) 对象级缓存实际生效 ------------------------------------------------
    def test_object_level_caches_hit_once_per_object(self):
        base_vertices = np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
        element = _PositionElement()
        obj_name = "aaaaaaaa-Body_x0"
        context = _context([0, 1], [0, 1], element)
        shape_keys = {f"Key{index}": base_vertices + np.asarray([index, 0.0, 0.0], dtype=np.float32) for index in range(6)}
        records = {obj_name: _record(base_vertices, shape_keys, [0, 1])}
        contexts = {obj_name: context}
        runtime_infos = {
            "aaaaaaaa": {
                "logical_hash": "aaaaaaaa",
                "base_bytes": base_vertices.tobytes(),
                "position_stride": 12,
            }
        }
        slots = {index: {name: [obj_name]} for index, name in enumerate(shape_keys)}
        ranges = {obj_name: (0, 1, "")}

        new_result, old_result, new_logs, old_logs, new_harness, old_harness = self._run_both(
            slots, ranges, runtime_infos, contexts, records
        )
        _assert_override_entries_equal(self, new_result, old_result)
        self.assertEqual(new_logs, old_logs)

        self.assertEqual(len(slots), 6)
        self.assertEqual(new_harness.context_resolution_calls, [obj_name])
        self.assertEqual(new_harness.candidate_name_calls, [obj_name])
        self.assertEqual(old_harness.context_resolution_calls.count(obj_name), len(slots))
        self.assertEqual(old_harness.candidate_name_calls.count(obj_name), len(slots))


if __name__ == "__main__":
    unittest.main()
