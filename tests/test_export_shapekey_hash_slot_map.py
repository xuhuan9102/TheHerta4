"""直出形态键 hash/slot 筛选：新实现与旧实现逐项等价（含顺序）+ 解析缓存次数。

新实现把「每个 hash 都对全部对象名重新解析一遍」改成「每个对象名只解析一次，
再按前缀分组」。本测试内联旧实现作为参考，构造多哈希/多槽/多对象数据做逐项对比。
"""

import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_export_shapekey_hash_slot_map_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
)
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    get_model_vertex_count=lambda *_a, **_k: 0,
    iter_drawib_models=lambda *_a, **_k: iter(()),
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=type("_ShapeKeyDirectExportError", (RuntimeError,), {}),
)

_module_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_runtime_mixin.py"
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_runtime_mixin",
    _module_path,
)
runtime_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = runtime_module
_spec.loader.exec_module(runtime_module)


# ---------------------------------------------------------------------------
# 与真实节点同形的假解析逻辑：8 位十六进制哈希，可选 "-xxxx" 段；前缀取 '-' 前。
# ---------------------------------------------------------------------------
def _fake_extract_hash_from_name(obj_name):
    if not isinstance(obj_name, str):
        return None
    match = re.match(r"^([0-9a-f]{8}(?:-[0-9a-f]{2,4})?)", obj_name)
    return match.group(1) if match else None


def _fake_extract_hash_prefix(hash_val):
    if not hash_val:
        return None
    return str(hash_val).split("-")[0]


class _FakeNode:
    """可记录调用次数的假节点。"""

    def __init__(self):
        self.hash_from_name_calls = []
        self.hash_prefix_calls = []

    def _extract_hash_from_name(self, obj_name):
        self.hash_from_name_calls.append(obj_name)
        return _fake_extract_hash_from_name(obj_name)

    def _extract_hash_prefix(self, hash_val):
        self.hash_prefix_calls.append(hash_val)
        return _fake_extract_hash_prefix(hash_val)


class _Harness(runtime_module.DirectShapeKeyRuntimeMixin):
    def __init__(self, node):
        self.node = node


def _old_build_hash_slot_data_map(node, unique_hashes, slot_to_name_to_objects):
    """旧实现原样内联（优化前 direct_export_shapekey_runtime_mixin.py 的版本）。"""
    hash_slot_data_map = {}
    for logical_hash in unique_hashes:
        logical_prefix = node._extract_hash_prefix(logical_hash)
        hash_slot_data = {}
        for slot, name_data in slot_to_name_to_objects.items():
            slot_entries = {}
            for name, objects in name_data.items():
                matched_objects = []
                for obj_name in objects:
                    obj_hash = node._extract_hash_from_name(obj_name)
                    obj_prefix = node._extract_hash_prefix(obj_hash) if obj_hash else None
                    if obj_prefix == logical_prefix:
                        matched_objects.append(obj_name)
                if matched_objects:
                    slot_entries[name] = matched_objects
            if slot_entries:
                hash_slot_data[slot] = slot_entries
        hash_slot_data_map[logical_hash] = hash_slot_data
    return hash_slot_data_map


def _make_slot_data():
    """多哈希 / 多槽 / 多对象，覆盖：前缀不匹配、无哈希名、空槽、空 name、混合前缀分组、重复对象。"""
    return {
        0: {
            "Alpha": [
                "aaaaaaaa-body_x0",
                "bbbbbbbb-body_x0",          # 前缀不匹配（对 aaaaaaaa 而言）
                "NoHashObject",              # 对象名无哈希
                "cccccccc-body_x0",
                "aaaaaaaa-body_x0",          # 同一对象重复出现，顺序必须保留
            ],
            "Mixed": [                       # 同一 name 下混合多个前缀
                "bbbbbbbb-mix_a",
                "aaaaaaaa-mix_b",
                "cccccccc-mix_c",
                "aaaaaaaa-mix_d",
            ],
            "Empty": [],                     # 空对象列表 -> 不写入
        },
        1: {
            "Beta": ["bbbbbbbb-part02_x0", "aaaa-zz"],   # "aaaa-zz" 不足 8 位十六进制 -> 无哈希
            "NoMatch": ["eeeeeeee-part04_x0"],            # 前缀不在 unique_hashes 中
        },
        2: {},                                            # 空槽 -> 不写入
        3: {
            "Gamma": ["dddddddd-part03_x0"],
            "NoHashTwo": ["AnotherNoHash"],
        },
    }


def _make_unique_hashes():
    # 顺序刻意与对象出现顺序不同；包含同前缀的两个 hash、无匹配的 hash、
    # 以及空前缀（_extract_hash_prefix 返回 None，旧实现会匹配所有无哈希对象）。
    return [
        "dddddddd-part03",
        "aaaaaaaa",
        "bbbbbbbb-aaaa",
        "aaaaaaaa-part01",   # 与 "aaaaaaaa" 同前缀
        "ffffffff-aa",       # 无任何匹配对象 -> 空 dict 但键必须存在
        "",
    ]


def _assert_same_structure(test_case, actual, expected, path="root"):
    if isinstance(expected, dict):
        test_case.assertIsInstance(actual, dict, f"{path}: 类型应为 dict")
        test_case.assertEqual(
            list(actual.keys()), list(expected.keys()), f"{path}: 键集合/插入顺序不一致"
        )
        for key in expected:
            _assert_same_structure(test_case, actual[key], expected[key], f"{path}[{key!r}]")
        return
    if isinstance(expected, list):
        test_case.assertIsInstance(actual, list, f"{path}: 类型应为 list")
        test_case.assertEqual(actual, expected, f"{path}: 列表内容/顺序不一致")
        return
    test_case.assertEqual(actual, expected, path)


class ExportShapeKeyHashSlotMapTests(unittest.TestCase):
    def test_new_map_matches_old_implementation_exactly_with_order(self):
        slot_to_name_to_objects = _make_slot_data()
        unique_hashes = _make_unique_hashes()

        old_node = _FakeNode()
        old_map = _old_build_hash_slot_data_map(old_node, unique_hashes, slot_to_name_to_objects)

        new_node = _FakeNode()
        new_map = _Harness(new_node)._build_hash_slot_data_map(unique_hashes, slot_to_name_to_objects)

        # dict 深度相等（内容）
        self.assertEqual(new_map, old_map)
        # 逐项 + 顺序一致（dict 插入顺序、对象列表顺序）
        _assert_same_structure(self, new_map, old_map)

        # 每个 hash 都出现，即使没有匹配对象
        self.assertEqual(list(new_map.keys()), list(unique_hashes))
        self.assertEqual(new_map["ffffffff-aa"], {})

        # 具体语义抽查
        self.assertEqual(
            new_map["aaaaaaaa"][0]["Alpha"],
            ["aaaaaaaa-body_x0", "aaaaaaaa-body_x0"],
        )
        self.assertEqual(new_map["aaaaaaaa"][0]["Mixed"], ["aaaaaaaa-mix_b", "aaaaaaaa-mix_d"])
        self.assertNotIn("Empty", new_map["aaaaaaaa"][0])
        self.assertNotIn(2, new_map["bbbbbbbb-aaaa"])
        self.assertEqual(new_map["bbbbbbbb-aaaa"][1]["Beta"], ["bbbbbbbb-part02_x0"])
        # 空前缀 hash 只匹配无哈希对象（与旧实现 == 语义一致）
        self.assertEqual(new_map[""][0]["Alpha"], ["NoHashObject"])
        self.assertEqual(new_map[""][1]["Beta"], ["aaaa-zz"])
        self.assertEqual(new_map[""][3]["NoHashTwo"], ["AnotherNoHash"])

    def test_each_object_name_is_parsed_only_once(self):
        slot_to_name_to_objects = _make_slot_data()
        unique_hashes = _make_unique_hashes()
        node = _FakeNode()

        _Harness(node)._build_hash_slot_data_map(unique_hashes, slot_to_name_to_objects)

        all_names = [
            obj_name
            for name_data in slot_to_name_to_objects.values()
            for objects in name_data.values()
            for obj_name in objects
        ]
        self.assertEqual(sorted(node.hash_from_name_calls), sorted(set(all_names)))
        self.assertEqual(len(node.hash_from_name_calls), len(set(all_names)))
        # 前缀解析 = 每个有哈希的对象一次 + 每个 logical_hash 一次
        expected_prefix_calls = sum(
            1 for obj_name in set(all_names) if _fake_extract_hash_from_name(obj_name)
        ) + len(unique_hashes)
        self.assertEqual(len(node.hash_prefix_calls), expected_prefix_calls)

    def test_same_prefix_hashes_do_not_share_mutable_containers(self):
        slot_to_name_to_objects = {0: {"Alpha": ["aaaaaaaa-body_x0"]}}
        unique_hashes = ["aaaaaaaa", "aaaaaaaa-part01"]

        result = _Harness(_FakeNode())._build_hash_slot_data_map(unique_hashes, slot_to_name_to_objects)

        self.assertEqual(result["aaaaaaaa"], result["aaaaaaaa-part01"])
        self.assertIsNot(result["aaaaaaaa"], result["aaaaaaaa-part01"])
        self.assertIsNot(result["aaaaaaaa"][0], result["aaaaaaaa-part01"][0])
        self.assertIsNot(result["aaaaaaaa"][0]["Alpha"], result["aaaaaaaa-part01"][0]["Alpha"])

        result["aaaaaaaa"][0]["Alpha"].append("mutated")
        self.assertEqual(result["aaaaaaaa-part01"][0]["Alpha"], ["aaaaaaaa-body_x0"])

    def test_empty_inputs(self):
        node = _FakeNode()
        self.assertEqual(_Harness(node)._build_hash_slot_data_map([], {}), {})
        self.assertEqual(_Harness(node)._build_hash_slot_data_map(["aaaaaaaa"], {}), {"aaaaaaaa": {}})


if __name__ == "__main__":
    unittest.main()
