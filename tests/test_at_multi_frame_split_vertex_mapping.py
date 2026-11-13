"""测试多帧拆分到形态键时，基于稳定ID/位置最近邻的顶点映射逻辑"""
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _FakeMatrix:
    @staticmethod
    def Identity(_size):
        return _FakeMatrix(((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0)))

    def __init__(self, rows=None):
        self.rows = rows or ()


class _FakeKDPoint:
    def __init__(self, co, idx, dist):
        self.co = co
        self.idx = idx
        self.dist = dist

    def __iter__(self):
        yield self.co
        yield self.idx
        yield self.dist


class _FakeKDTree:
    def __init__(self, size):
        self._items = []

    def insert(self, co, idx):
        self._items.append((tuple(co), idx))

    def balance(self):
        pass

    def find(self, co):
        co_arr = np.asarray(co, dtype=np.float64)
        best = None
        best_dist = float('inf')
        for item_co, item_idx in self._items:
            d = float(np.linalg.norm(np.asarray(item_co) - co_arr))
            if d < best_dist:
                best_dist = d
                best = (item_co, item_idx, d)
        return best

    def find_n(self, co, n):
        co_arr = np.asarray(co, dtype=np.float64)
        scored = []
        for item_co, item_idx in self._items:
            d = float(np.linalg.norm(np.asarray(item_co) - co_arr))
            scored.append((item_co, item_idx, d))
        scored.sort(key=lambda x: x[2])
        return scored[:n]


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=object),
    data=types.SimpleNamespace(objects={}, collections={}),
)
_install_module(
    "mathutils",
    Matrix=_FakeMatrix,
    Vector=tuple,
    kdtree=types.SimpleNamespace(KDTree=_FakeKDTree),
)


PKG = "_at_multi_frame_split_vmap_test_pkg"

# 注册 fake 包链，使 toolkit/at_multi_frame_split.py 顶部的相对导入
# `from ..utils.shapekey_rebase_utils import ...` 能在测试包内解析；
# 装配模式与 test_buffer_cleanup_mergeskeleton.py 一致（_install_module + __path__）。
for _pkg_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    _pkg = _install_module(_pkg_name)
    _pkg.__path__ = []

# 真实加载 utils/shapekey_rebase_utils.py（纯 numpy、无 bpy 依赖），
# 保证被测行为使用真实实现而非桩。
_rebase_spec = importlib.util.spec_from_file_location(
    f"{PKG}.utils.shapekey_rebase_utils",
    Path(__file__).resolve().parents[1] / "utils" / "shapekey_rebase_utils.py",
)
_rebase_module = importlib.util.module_from_spec(_rebase_spec)
sys.modules[_rebase_spec.name] = _rebase_module
_rebase_spec.loader.exec_module(_rebase_module)


module_path = Path(__file__).resolve().parents[1] / "toolkit" / "at_multi_frame_split.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.at_multi_frame_split", module_path)
split_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = split_module
spec.loader.exec_module(split_module)


class _FakeAttr:
    def __init__(self, name, domain, data_type, values):
        self.name = name
        self.domain = domain
        self.data_type = data_type
        self._values = np.asarray(values)
        self.read_indices = []

        class _Data:
            def __init__(inner_self, arr_ref):
                inner_self._owner = arr_ref
                inner_self._broken_read = False

            def foreach_get(inner_self, _attr, output):
                output[:] = inner_self._owner._values.reshape(-1)

            def foreach_set(inner_self, _attr, values):
                inner_self._owner._values = np.asarray(values)

            def __getitem__(inner_self, index):
                if inner_self._broken_read:
                    raise RuntimeError("unreadable attribute")
                inner_self._owner.read_indices.append(int(index))
                return types.SimpleNamespace(value=inner_self._owner._values[index])

            def __len__(inner_self):
                return int(inner_self._owner._values.size)

        self.data = _Data(self)


class _FakeAttrCollection:
    def __init__(self, attrs):
        self._attrs = {attr.name: attr for attr in attrs}
        self.new_calls = []

    def get(self, name):
        return self._attrs.get(name)

    def new(self, name, type, domain):
        # 创建一个新的属性，初始数据全为0；测试代码会再 foreach_set
        size = 0
        attr = _FakeAttr(name, domain, type, np.zeros(size, dtype=np.int32))
        # 替换为接受任意长度 foreach_set 的实现：通过先放置一个足够大的占位
        self._attrs[name] = attr
        self.new_calls.append(name)
        return attr

    def remove(self, attr):
        self._attrs.pop(attr.name, None)


class _FakeVertices:
    def __init__(self, coords):
        self._coords = np.asarray(coords, dtype=np.float32)

    def foreach_get(self, _attr, output):
        output[:] = self._coords.reshape(-1)

    def __len__(self):
        return len(self._coords)


class _FakeMesh:
    def __init__(self, coords, attrs=None, props=None):
        self.vertices = _FakeVertices(coords)
        self.attributes = _FakeAttrCollection(attrs or [])
        self._props = props or {}

    def update(self):
        pass

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value):
        self._props[key] = value


class _FakeObject:
    def __init__(self, name, mesh):
        self.name = name
        self.type = "MESH"
        self.data = mesh


class _FakeProps:
    def __init__(self, vertex_mapping_mode="AUTO", stable_id_attribute="stable_id"):
        self.vertex_mapping_mode = vertex_mapping_mode
        self.stable_id_attribute = stable_id_attribute


class StableIdMappingTests(unittest.TestCase):
    def test_stable_id_mapping_resolves_reordered_vertices(self):
        # base: ids [10, 20, 30], coords arbitrary
        base_coords = np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0]], dtype=np.float32)
        base_ids = [10, 20, 30]
        base_mesh = _FakeMesh(
            base_coords,
            attrs=[_FakeAttr("stable_id", "POINT", "INT", np.asarray(base_ids, dtype=np.int32))],
        )
        # target: same ids but reordered [30, 10, 20], coords moved
        target_coords = np.array([[2, 1, 0], [0, 1, 0], [1, 1, 0]], dtype=np.float32)
        target_ids = [30, 10, 20]
        target_mesh = _FakeMesh(
            target_coords,
            attrs=[_FakeAttr("stable_id", "POINT", "INT", np.asarray(target_ids, dtype=np.int32))],
        )

        base_obj = _FakeObject("Base", base_mesh)
        target_obj = _FakeObject("Target", target_mesh)
        props = _FakeProps()

        mapping, mode, msg = split_module._build_target_to_base_mapping(base_obj, target_obj, props)
        self.assertEqual(mode, "STABLE_ID")
        self.assertIsNotNone(mapping)

        # Verify remapping yields target coords aligned to base ID order:
        # base index 0 = id 10 -> target index 1 (id 10) -> coord [0, 1, 0]
        # base index 1 = id 20 -> target index 2 (id 20) -> coord [1, 1, 0]
        # base index 2 = id 30 -> target index 0 (id 30) -> coord [2, 1, 0]
        remapped = np.empty_like(base_coords)
        remapped[mapping] = target_coords
        np.testing.assert_allclose(
            remapped,
            np.array([[0, 1, 0], [1, 1, 0], [2, 1, 0]], dtype=np.float32),
        )

    def test_position_fallback_when_no_stable_id(self):
        base_coords = np.array([[0, 0, 0], [10, 0, 0], [20, 0, 0]], dtype=np.float32)
        # target reordered, slight position shift
        target_coords = np.array([[20.01, 0, 0], [0.01, 0, 0], [10.01, 0, 0]], dtype=np.float32)
        base_mesh = _FakeMesh(base_coords, attrs=[])
        target_mesh = _FakeMesh(target_coords, attrs=[])

        base_obj = _FakeObject("Base", base_mesh)
        target_obj = _FakeObject("Target", target_mesh)
        props = _FakeProps(vertex_mapping_mode="AUTO")

        mapping, mode, _msg = split_module._build_target_to_base_mapping(base_obj, target_obj, props)
        self.assertEqual(mode, "POSITION")
        # target[0] is near base[2]; target[1] near base[0]; target[2] near base[1]
        self.assertEqual(list(mapping), [2, 0, 1])

    def test_strict_stable_id_mode_falls_back_to_position_when_attribute_missing(self):
        # 严格模式下缺失稳定ID属性时，自动降级为KDTree位置最近邻匹配。
        base_coords = np.array([[0, 0, 0], [10, 0, 0]], dtype=np.float32)
        target_coords = np.array([[10.01, 0, 0], [0.01, 0, 0]], dtype=np.float32)
        base_mesh = _FakeMesh(base_coords, attrs=[])
        target_mesh = _FakeMesh(target_coords, attrs=[])

        base_obj = _FakeObject("Base", base_mesh)
        target_obj = _FakeObject("Target", target_mesh)
        props = _FakeProps(vertex_mapping_mode="STABLE_ID")

        mapping, mode, msg = split_module._build_target_to_base_mapping(base_obj, target_obj, props)
        self.assertEqual(mode, "POSITION")
        self.assertEqual(list(mapping), [1, 0])
        self.assertIn("降级", msg)

    def test_index_mode_returns_identity_mapping(self):
        base_coords = np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0]], dtype=np.float32)
        target_coords = np.array([[0, 1, 0], [1, 1, 0], [2, 1, 0]], dtype=np.float32)
        base_mesh = _FakeMesh(base_coords, attrs=[])
        target_mesh = _FakeMesh(target_coords, attrs=[])

        base_obj = _FakeObject("Base", base_mesh)
        target_obj = _FakeObject("Target", target_mesh)
        props = _FakeProps(vertex_mapping_mode="INDEX")

        mapping, mode, _msg = split_module._build_target_to_base_mapping(base_obj, target_obj, props)
        self.assertEqual(mode, "INDEX")
        self.assertEqual(list(mapping), [0, 1, 2])


class CachedStableIdTests(unittest.TestCase):
    def test_cached_ids_are_used_when_attributes_are_lost(self):
        base_coords = np.array([[0, 0, 0], [1, 0, 0]], dtype=np.float32)
        # base mesh no longer has the attribute, but cached ids are present in id_properties
        base_mesh = _FakeMesh(
            base_coords,
            attrs=[],
            props={
                split_module._STABLE_ID_STORAGE_KEY: [10, 20],
                split_module._STABLE_ID_STORAGE_KEY + "_name": "stable_id",
            },
        )
        target_mesh = _FakeMesh(
            np.array([[5, 0, 0], [6, 0, 0]], dtype=np.float32),
            attrs=[],
            props={
                split_module._STABLE_ID_STORAGE_KEY: [20, 10],
                split_module._STABLE_ID_STORAGE_KEY + "_name": "stable_id",
            },
        )

        base_obj = _FakeObject("Base", base_mesh)
        target_obj = _FakeObject("Target", target_mesh)
        props = _FakeProps()

        mapping, mode, _msg = split_module._build_target_to_base_mapping(base_obj, target_obj, props)
        self.assertEqual(mode, "STABLE_ID")
        # target[0].id=20 -> base index 1; target[1].id=10 -> base index 0
        self.assertEqual(list(mapping), [1, 0])


class EnsureStableIdOnOriginalMeshTests(unittest.TestCase):
    def test_injects_attribute_when_missing(self):
        mesh = _FakeMesh(np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0]], dtype=np.float32), attrs=[])
        obj = _FakeObject("Source", mesh)
        attr_name, was_created = split_module._ensure_stable_id_on_original_mesh(obj, ["stable_id"])
        self.assertEqual(attr_name, "stable_id")
        self.assertTrue(was_created)
        stored = mesh.attributes.get("stable_id")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.domain, "POINT")
        self.assertIn(stored.data_type, {"INT", "INT32"})
        buf = np.zeros(3, dtype=np.int32)
        stored.data.foreach_get("value", buf)
        # 验证写入的是 0..N-1
        self.assertEqual(list(buf), [0, 1, 2])

    def test_reuses_existing_valid_attribute(self):
        existing = _FakeAttr("stable_id", "POINT", "INT", np.array([7, 8, 9], dtype=np.int32))
        mesh = _FakeMesh(np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0]], dtype=np.float32), attrs=[existing])
        obj = _FakeObject("Source", mesh)
        attr_name, was_created = split_module._ensure_stable_id_on_original_mesh(obj, ["stable_id"])
        self.assertEqual(attr_name, "stable_id")
        self.assertFalse(was_created)
        buf = np.zeros(3, dtype=np.int32)
        mesh.attributes.get("stable_id").data.foreach_get("value", buf)
        # 已存在的值未被覆盖
        self.assertEqual(list(buf), [7, 8, 9])

    def test_existing_attribute_is_reused_without_reading_all_values(self):
        # 零成本路径：只读 3 个点做探针，不再 foreach_get 全部 V 个 int。
        existing = _FakeAttr("stable_id", "POINT", "INT", np.arange(20, dtype=np.int32))
        mesh = _FakeMesh(np.zeros((20, 3), dtype=np.float32), attrs=[existing])
        obj = _FakeObject("Source", mesh)

        attr_name, was_created = split_module._ensure_stable_id_on_original_mesh(obj, ["stable_id"])

        self.assertEqual(attr_name, "stable_id")
        self.assertFalse(was_created)
        self.assertEqual(sorted(existing.read_indices), [0, 10, 19])

    def test_unreadable_existing_attribute_is_rebuilt(self):
        # 老语义：foreach_get 读失败时会删掉属性重建；探针读失败也必须走重建。
        existing = _FakeAttr("stable_id", "POINT", "INT", np.arange(3, dtype=np.int32))
        existing.data._broken_read = True
        mesh = _FakeMesh(np.zeros((3, 3), dtype=np.float32), attrs=[existing])
        obj = _FakeObject("Source", mesh)

        attr_name, was_created = split_module._ensure_stable_id_on_original_mesh(obj, ["stable_id"])

        self.assertEqual(attr_name, "stable_id")
        self.assertTrue(was_created)
        stored = mesh.attributes.get("stable_id")
        self.assertIsNotNone(stored)
        buf = np.zeros(3, dtype=np.int32)
        stored.data.foreach_get("value", buf)
        self.assertEqual(list(buf), [0, 1, 2])


class StableIdDebugVerificationTests(unittest.TestCase):
    """ATP_STABLE_ID_DEBUG=1 时的稳定ID采样校验（默认关闭时零成本）。"""

    @staticmethod
    def _debug_on():
        return mock.patch.dict(os.environ, {split_module._STABLE_ID_DEBUG_ENV: "1"})

    def test_debug_check_raises_when_mapping_point_is_wrong(self):
        base_ids = np.arange(20, dtype=np.int64) + 100
        target_ids = base_ids.copy()
        mapping = np.arange(20, dtype=np.int64)
        mapping[0] = 1  # target[0] 的 ID=100 应映射到 base[0]，故意错成 base[1]（ID=101）

        with self._debug_on():
            with self.assertRaises(RuntimeError) as ctx:
                split_module._verify_stable_id_mapping_sampled(mapping, base_ids, target_ids)

        message = str(ctx.exception)
        self.assertIn("target[0]", message)
        self.assertIn("mapping[0]=1", message)
        self.assertIn("100", message)  # target ID
        self.assertIn("101", message)  # 错误 base 顶点上的 ID

    def test_debug_check_is_noop_when_disabled(self):
        # 同一处故意改错，开关关闭时不读、不抛。
        base_ids = np.arange(4, dtype=np.int64)
        target_ids = base_ids.copy()
        mapping = np.array([1, 0, 2, 3], dtype=np.int64)

        with mock.patch.dict(os.environ, {}, clear=True):
            split_module._verify_stable_id_mapping_sampled(mapping, base_ids, target_ids)

    def test_debug_check_accepts_arbitrary_non_contiguous_ids(self):
        # 判据是映射正确，不是「属性等于 0..N-1」：用户经 GN 写入的 ID 可以是任意值。
        base_ids = np.array([1000, 7, 42, 999, 3], dtype=np.int64)
        target_ids = np.array([42, 1000, 3, 999, 7], dtype=np.int64)
        mapping = np.array([2, 0, 4, 3, 1], dtype=np.int64)

        with self._debug_on():
            split_module._verify_stable_id_mapping_sampled(mapping, base_ids, target_ids)

    def test_build_mapping_invokes_debug_check_after_stable_id_mapping(self):
        base_ids = np.array([10, 20, 30], dtype=np.int32)
        target_ids = np.array([30, 10, 20], dtype=np.int32)
        base_mesh = _FakeMesh(
            np.zeros((3, 3), dtype=np.float32),
            attrs=[_FakeAttr("stable_id", "POINT", "INT", base_ids)],
        )
        target_mesh = _FakeMesh(
            np.ones((3, 3), dtype=np.float32),
            attrs=[_FakeAttr("stable_id", "POINT", "INT", target_ids)],
        )
        calls = []

        def _record(mapping, checked_base_ids, checked_target_ids, sample_limit=16):
            del sample_limit
            calls.append((
                np.asarray(mapping).tolist(),
                np.asarray(checked_base_ids).tolist(),
                np.asarray(checked_target_ids).tolist(),
            ))

        with self._debug_on():
            with mock.patch.object(split_module, "_verify_stable_id_mapping_sampled", _record):
                mapping, mode, _msg = split_module._build_target_to_base_mapping(
                    _FakeObject("Base", base_mesh),
                    _FakeObject("Target", target_mesh),
                    _FakeProps(),
                )

        self.assertEqual(mode, "STABLE_ID")
        self.assertEqual(list(mapping), [2, 0, 1])
        self.assertEqual(calls, [([2, 0, 1], [10, 20, 30], [30, 10, 20])])


def _reference_stable_id_mapping(base_ids, target_ids):
    """原 :159-170 的 Python dict 循环，作为向量化实现的参考（逐元素等价判据）。"""
    base_lookup = {}
    for idx, value in enumerate(base_ids):
        base_lookup.setdefault(int(value), idx)
    mapping = np.empty(len(target_ids), dtype=np.int64)
    missing = 0
    for t_idx, value in enumerate(target_ids):
        base_idx = base_lookup.get(int(value))
        if base_idx is None:
            missing += 1
            mapping[t_idx] = -1
        else:
            mapping[t_idx] = base_idx
    return mapping, missing


def _make_id_mesh(ids, coords=None, attr_name="stable_id", domain="POINT", data_type="INT"):
    ids = np.asarray(ids, dtype=np.int64)
    if coords is None:
        coords = np.zeros((ids.size, 3), dtype=np.float32)
    return _FakeMesh(coords, attrs=[_FakeAttr(attr_name, domain, data_type, ids)])


class StableIdVectorizedMappingTests(unittest.TestCase):
    """MAPPING-SPEC §3 的 B1-B14：新旧实现逐元素等价 + 对外降级行为。"""

    CASES = [
        ("B1 ID 全重复", [7, 7, 7], [7, 7, 7], [0, 0, 0], 0),
        ("B2 部分缺失", [10, 20, 30], [10, 99, 30], [0, -1, 2], 1),
        ("B3 空数组", [], [], [], 0),
        ("B4a V=1 命中", [42], [42], [0], 0),
        ("B4b V=1 未命中", [42], [43], [-1], 1),
        ("B6 超出 int32", [3000000000], [3000000000], [0], 0),
        ("B8 非连续 ID 重排", [100, 200, 300], [300, 100, 200], [2, 0, 1], 0),
        ("B9 重复+缺失", [5, 5, 7], [5, 7, 9], [0, 2, -1], 1),
        ("B10 负 ID", [-1, -2], [-2, -1], [1, 0], 0),
        ("B12 base 空 target 非空", [], [1, 2], [-1, -1], 2),
        ("B13 全部缺失", [1, 2], [3, 4], [-1, -1], 2),
    ]

    def test_vectorized_matches_reference_for_all_cases(self):
        for name, base_ids, target_ids, expected, expected_missing in self.CASES:
            with self.subTest(case=name):
                old_mapping, old_missing = _reference_stable_id_mapping(base_ids, target_ids)
                new_mapping, new_missing = split_module._stable_id_mapping_vectorized(base_ids, target_ids)

                np.testing.assert_array_equal(new_mapping, old_mapping)
                np.testing.assert_array_equal(new_mapping, np.asarray(expected, dtype=np.int64))
                self.assertEqual(new_missing, old_missing)
                self.assertEqual(new_missing, expected_missing)
                self.assertEqual(new_mapping.dtype, np.int64)
                self.assertEqual(new_mapping.shape, (len(target_ids),))

    def test_stable_id_mode_for_b1_b8_b10(self):
        cases = [
            ("B1", [7, 7, 7], [7, 7, 7], [0, 0, 0]),
            ("B8", [100, 200, 300], [300, 100, 200], [2, 0, 1]),
            ("B10", [-1, -2], [-2, -1], [1, 0]),
        ]
        for label, base_ids, target_ids, expected in cases:
            with self.subTest(case=label):
                base = _make_id_mesh(base_ids, coords=np.arange(len(base_ids) * 3, dtype=np.float32).reshape(-1, 3))
                target = _make_id_mesh(target_ids, coords=np.arange(len(target_ids) * 3, dtype=np.float32).reshape(-1, 3))
                mapping, mode, _msg = split_module._build_target_to_base_mapping(
                    _FakeObject("Base", base), _FakeObject("Target", target), _FakeProps()
                )
                self.assertEqual(mode, "STABLE_ID")
                self.assertEqual(list(mapping), expected)

    def test_missing_ids_degrade_to_position(self):
        cases = [
            ("B2", [10, 20, 30], [10, 99, 30]),
            ("B9", [5, 5, 7], [5, 7, 9]),
            ("B13", [1, 2], [3, 4]),
        ]
        for label, base_ids, target_ids in cases:
            with self.subTest(case=label):
                base_coords = np.arange(len(base_ids) * 3, dtype=np.float32).reshape(-1, 3)
                target_coords = base_coords.copy()
                base = _make_id_mesh(base_ids, coords=base_coords)
                target = _make_id_mesh(target_ids, coords=target_coords)
                _mapping, mode, msg = split_module._build_target_to_base_mapping(
                    _FakeObject("Base", base), _FakeObject("Target", target), _FakeProps()
                )
                self.assertNotEqual(mode, "STABLE_ID")
                self.assertEqual(mode, "POSITION")
                self.assertIn("稳定ID属性匹配失败", msg)

    def test_b5_length_mismatch_returns_none_before_any_mode(self):
        base = _make_id_mesh([1, 2, 3])
        target = _make_id_mesh([1, 2])
        mapping, mode, msg = split_module._build_target_to_base_mapping(
            _FakeObject("Base", base), _FakeObject("Target", target), _FakeProps()
        )
        self.assertIsNone(mapping)
        self.assertEqual(mode, "NONE")
        self.assertIn("顶点数不一致", msg)

    def test_b3_zero_vertex_mesh_uses_stable_id(self):
        base = _make_id_mesh([])
        target = _make_id_mesh([])
        mapping, mode, _msg = split_module._build_target_to_base_mapping(
            _FakeObject("Base", base), _FakeObject("Target", target), _FakeProps()
        )
        self.assertEqual(mode, "STABLE_ID")
        self.assertEqual(mapping.dtype, np.int64)
        self.assertEqual(mapping.shape, (0,))

    def test_b7_non_int_attribute_filtering(self):
        float_mesh = _FakeMesh(
            np.zeros((2, 3), dtype=np.float32),
            attrs=[_FakeAttr("stable_id", "POINT", "FLOAT", np.array([10.4, 10.6], dtype=np.float32))],
        )
        ids, name = split_module._read_stable_ids_from_mesh(float_mesh, ["stable_id"])
        self.assertEqual(name, "stable_id")
        self.assertEqual(list(ids), [10, 11])

        vector_mesh = _FakeMesh(
            np.zeros((2, 3), dtype=np.float32),
            attrs=[_FakeAttr("stable_id", "POINT", "FLOAT_VECTOR", np.zeros((2, 3), dtype=np.float32))],
        )
        self.assertEqual(split_module._read_stable_ids_from_mesh(vector_mesh, ["stable_id"]), (None, None))

        corner_mesh = _FakeMesh(
            np.zeros((2, 3), dtype=np.float32),
            attrs=[_FakeAttr("stable_id", "CORNER", "INT", np.array([1, 2], dtype=np.int32))],
        )
        self.assertEqual(split_module._read_stable_ids_from_mesh(corner_mesh, ["stable_id"]), (None, None))

    def test_b11_cache_length_mismatch_falls_back_to_attribute(self):
        mesh = _FakeMesh(
            np.zeros((2, 3), dtype=np.float32),
            attrs=[_FakeAttr("stable_id", "POINT", "INT", np.array([7, 8], dtype=np.int32))],
            props={
                split_module._STABLE_ID_STORAGE_KEY: [1, 2, 3],
                split_module._STABLE_ID_STORAGE_KEY + "_name": "stable_id",
            },
        )
        ids, name = split_module._resolve_stable_ids_for_object(_FakeObject("Source", mesh), ["stable_id"])
        self.assertEqual(list(ids), [7, 8])
        self.assertEqual(name, "stable_id")

    def test_b14_invalid_mode_and_none_props_normalize_to_auto(self):
        base = _make_id_mesh([10, 20])
        target = _make_id_mesh([20, 10])
        for props in (_FakeProps(vertex_mapping_mode="FOO"), None):
            with self.subTest(props=props):
                mapping, mode, _msg = split_module._build_target_to_base_mapping(
                    _FakeObject("Base", base), _FakeObject("Target", target), props
                )
                self.assertEqual(mode, "STABLE_ID")
                self.assertEqual(list(mapping), [1, 0])


class StableIdCacheStorageTests(unittest.TestCase):
    """MAPPING-SPEC §4：缓存从 IDProperty 换成 INT 顶点属性的兼容行为。"""

    def test_snapshot_writes_int_point_attribute_and_reads_back(self):
        mesh = _FakeMesh(np.zeros((3, 3), dtype=np.float32))

        split_module._snapshot_stable_ids_to_mesh(mesh, np.array([10, 20, 30], dtype=np.int64), "stable_id")

        attr = mesh.attributes.get(split_module._STABLE_ID_CACHE_ATTR)
        self.assertIsNotNone(attr)
        self.assertEqual(attr.domain, "POINT")
        self.assertEqual(attr.data_type, "INT")
        # 不再写旧 IDProperty 列表，只保留 _name 字符串元数据
        self.assertIsNone(mesh.get(split_module._STABLE_ID_STORAGE_KEY))
        ids, name = split_module._read_cached_stable_ids(mesh)
        self.assertEqual(ids.dtype, np.int64)
        self.assertEqual(list(ids), [10, 20, 30])
        self.assertEqual(name, "stable_id")

    def test_repeated_snapshot_reuses_single_attribute(self):
        mesh = _FakeMesh(np.zeros((3, 3), dtype=np.float32))

        for ids in ([1, 2, 3], [4, 5, 6], [7, 8, 9]):
            split_module._snapshot_stable_ids_to_mesh(mesh, ids, "stable_id")

        self.assertEqual(mesh.attributes.new_calls, [split_module._STABLE_ID_CACHE_ATTR])
        self.assertEqual(list(mesh.attributes._attrs), [split_module._STABLE_ID_CACHE_ATTR])
        ids, _name = split_module._read_cached_stable_ids(mesh)
        self.assertEqual(list(ids), [7, 8, 9])

    def test_wrong_domain_type_or_length_cache_attribute_is_replaced(self):
        wrong_type = _FakeAttr(split_module._STABLE_ID_CACHE_ATTR, "POINT", "FLOAT", np.zeros(3, dtype=np.float32))
        mesh = _FakeMesh(np.zeros((3, 3), dtype=np.float32), attrs=[wrong_type])

        split_module._snapshot_stable_ids_to_mesh(mesh, [1, 2, 3], "stable_id")

        attr = mesh.attributes.get(split_module._STABLE_ID_CACHE_ATTR)
        self.assertIsNot(attr, wrong_type)
        self.assertEqual(attr.data_type, "INT")
        self.assertEqual(mesh.attributes.new_calls, [split_module._STABLE_ID_CACHE_ATTR])
        ids, _name = split_module._read_cached_stable_ids(mesh)
        self.assertEqual(list(ids), [1, 2, 3])

        too_short = _FakeAttr(split_module._STABLE_ID_CACHE_ATTR, "POINT", "INT", np.zeros(2, dtype=np.int32))
        mesh2 = _FakeMesh(np.zeros((3, 3), dtype=np.float32), attrs=[too_short])
        split_module._snapshot_stable_ids_to_mesh(mesh2, [4, 5, 6], "stable_id")
        ids2, _name2 = split_module._read_cached_stable_ids(mesh2)
        self.assertEqual(list(ids2), [4, 5, 6])

    def test_out_of_int32_ids_skip_cache_without_error(self):
        # 旧版 IDProperty 写 3e9 会抛 OverflowError 并被吞掉 = 没有缓存；
        # 新版必须同样不写缓存、不崩，而不是抛 OverflowError 冒到状态机。
        mesh = _FakeMesh(np.zeros((2, 3), dtype=np.float32))

        split_module._snapshot_stable_ids_to_mesh(mesh, np.array([3000000000, 1], dtype=np.int64), "stable_id")

        self.assertIsNone(mesh.attributes.get(split_module._STABLE_ID_CACHE_ATTR))
        self.assertEqual(mesh.attributes.new_calls, [])
        self.assertIsNone(mesh.get(split_module._STABLE_ID_CACHE_NAME_KEY))
        self.assertEqual(split_module._read_cached_stable_ids(mesh), (None, None))

    def test_legacy_idproperty_cache_still_reads(self):
        mesh = _FakeMesh(
            np.zeros((2, 3), dtype=np.float32),
            props={
                split_module._STABLE_ID_STORAGE_KEY: [10, 20],
                split_module._STABLE_ID_STORAGE_KEY + "_name": "stable_id",
            },
        )
        ids, name = split_module._read_cached_stable_ids(mesh)
        self.assertEqual(list(ids), [10, 20])
        self.assertEqual(name, "stable_id")

    def test_cache_attribute_name_is_filtered_from_user_candidates(self):
        props = _FakeProps(stable_id_attribute=f"stable_id, {split_module._STABLE_ID_CACHE_ATTR}")
        self.assertEqual(split_module._candidate_stable_id_names(props), ["stable_id"])

        only_cache = _FakeProps(stable_id_attribute=split_module._STABLE_ID_CACHE_ATTR)
        self.assertEqual(split_module._candidate_stable_id_names(only_cache), ["stable_id"])


if __name__ == "__main__":
    unittest.main()
