# -*- coding: utf-8 -*-
"""材质指定节点「物体切换 if 块拆块」行为测试。

需求：IB 哈希代码块内，不在部件列表中的物体（非目标）的贴图引用与绘制命令
必须排在所有目标部件之前。当非目标 A 与目标 B 同处一个物体切换 if 块时，旧实现
无法拆分，A、B 被同一条件包裹而无法各自移动。本测试覆盖：

1. 同条件块内 A(非目标)+B(目标) → 拆成两个同条件块，A 块在前、B 块在段尾；
2. 块内 B 在 A 之前 → 拆分后仍 A 块在前、B 块在后；
3. 顶层非目标 mesh 位于目标之后 → 目标重排到段尾；
4. 目标 mesh 后的材质切换嵌套 if 随目标块整体移动，不被拆散；
5. 纯目标块 / 纯非目标块不产生拆分（且幂等）；
6. 含 else 的块保守跳过，if/endif 结构不变。
"""
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_custom_material_assign_if_split_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


class _FakePropertyBase:
    pass


def _fake_prop(**_kwargs):
    return None


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(
        PropertyGroup=_FakePropertyBase,
        Operator=_FakePropertyBase,
        Object=object,
        Node=_FakePropertyBase,
    ),
    props=types.SimpleNamespace(
        StringProperty=_fake_prop,
        BoolProperty=_fake_prop,
        IntProperty=_fake_prop,
        FloatProperty=_fake_prop,
        EnumProperty=_fake_prop,
        PointerProperty=_fake_prop,
        CollectionProperty=_fake_prop,
    ),
    utils=types.SimpleNamespace(
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    ),
)
sys.modules["bpy"] = _fake_bpy


class _FakeMaterialBase:
    """只提供本测试所需的接口形态。"""

    def extract_mesh_name(self, line):
        match = re.search(r'\[mesh:([^\]]+)\]', str(line or ""))
        return match.group(1) if match else None

    @staticmethod
    def _is_generated_material_line(line):
        """与生产实现同口径的最小子集：含 ps-t 赋值/材质切换标记即视为生成行。"""
        stripped = str(line or "").strip()
        if not stripped:
            return False
        return (
            stripped.startswith("ps-t")
            or "$\\RabbitFX\\brightness" in stripped
            or "$\\HI3FX\\brightness" in stripped
            or "材质切换" in stripped
            or stripped in (
                "run = CommandList\\RabbitFX\\SetTextures",
                "run = CommandList\\RabbitFx\\SetTextures",
                "run = CommandList\\ZZMI\\SetTextures",
            )
        )


_material_module = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_module.SSMTNode_PostProcess_MaterialBase = _FakeMaterialBase
_material_module.MATERIAL_DETECT_PRESETS = {}


def _load_gimi_orfix_adapter():
    spec = importlib.util.spec_from_file_location(
        "gimi_orfix_adapter_probe_if_split", REPO_ROOT / "blueprint" / "gimi_orfix_adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_material_module._load_gimi_orfix_adapter = _load_gimi_orfix_adapter
sys.modules[f"{PKG}.blueprint.node_postprocess_material"] = _material_module

_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_custom_material_assign",
    REPO_ROOT / "blueprint" / "node_postprocess_custom_material_assign.py",
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

MATERIAL_SWITCH_COMMENT = "; 材质切换"


class _Node(module.SSMTNode_PostProcess_CustomMaterialAssign):
    """绕过 bpy 实例化：只装本测试需要的 mesh 判定与生成行判定。"""

    def __init__(self, custom_names):
        self.extract_mesh_name = _FakeMaterialBase().extract_mesh_name
        self._custom_names = set(custom_names)

    def _is_custom_target(self, obj):
        return obj is not None and obj in self._custom_names

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        if object_filter is None or mesh_name not in self._custom_names:
            return None
        return mesh_name if object_filter(mesh_name) else None


def _mesh(mesh_name):
    return "; [mesh:{}] [vertex_count:3]".format(mesh_name)


def _if_line(condition="$switch == 1"):
    return "if {}".format(condition)


def _count(lines, text):
    return sum(1 for line in lines if str(line or "").strip() == text)


def _mesh_block_indices(lines):
    return [index for index, line in enumerate(lines) if "[mesh:" in str(line or "")]


def _block_between_if_and_endif(lines, if_index):
    """返回 if_index 对应顶层块内部的各行（不含 if/endif 行）。"""
    depth = 0
    for index in range(if_index, len(lines)):
        stripped = str(lines[index] or "").strip()
        if re.match(r"^if\s+", stripped, re.IGNORECASE):
            depth += 1
            continue
        if stripped.casefold() == "endif":
            depth -= 1
            if depth == 0:
                return lines[if_index + 1:index]
    raise AssertionError("未配对 endif")


class IfSplitTests(unittest.TestCase):
    def test_mixed_block_split_and_target_moved_to_tail(self):
        """同条件块内 A(非目标)+B(目标)：拆两块，A 前、B 段尾。"""
        lines = [
            _mesh("C"), "ps-t0 = DefaultRes", "drawindexed = 1,0,0",
            _if_line(),
            _mesh("A"), "ps-t0 = ARes", "drawindexed = 2,0,0",
            _mesh("B"), "ps-t0 = BRes", "drawindexed = 3,0,0",
            "endif",
            _mesh("D"), "ps-t0 = DRes", "drawindexed = 4,0,0",
            ";MARK: tail",
        ]
        node = _Node({"B"})
        node._split_mixed_target_if_blocks(lines)

        # 两个同条件块，各自带 endif。
        self.assertEqual(_count(lines, _if_line()), 2)
        self.assertEqual(_count(lines, "endif"), 2)

        index_a = lines.index(_mesh("A"))
        index_b = lines.index(_mesh("B"))
        index_d = lines.index(_mesh("D"))
        self.assertLess(index_a, index_b, "非目标 A 必须排在目标 B 之前")
        self.assertLess(index_d, index_b, "目标块必须移动到段尾（非目标 D 之后）")
        self.assertEqual(lines[-1], ";MARK: tail")
        self.assertLess(index_b, lines.index(";MARK: tail"))

        # 每块内部只含自己的 mesh 单元，条件行原样复制。
        if_indices = [i for i, line in enumerate(lines) if str(line).strip() == _if_line()]
        block_a = _block_between_if_and_endif(lines, if_indices[0])
        block_b = _block_between_if_and_endif(lines, if_indices[1])
        self.assertIn(_mesh("A"), block_a)
        self.assertNotIn(_mesh("B"), block_a)
        self.assertIn(_mesh("B"), block_b)
        self.assertNotIn(_mesh("A"), block_b)

        # 幂等：再拆一次输出不变。
        snapshot = list(lines)
        node._split_mixed_target_if_blocks(lines)
        self.assertEqual(lines, snapshot)

    def test_target_before_non_target_inside_block_still_non_target_first(self):
        """块内 B(目标) 在 A(非目标) 之前：拆分后仍 A 块在前、B 块在后。"""
        lines = [
            _if_line(),
            _mesh("B"), "ps-t0 = BRes", "drawindexed = 2,0,0",
            _mesh("A"), "ps-t0 = ARes", "drawindexed = 3,0,0",
            "endif",
            ";MARK: tail",
        ]
        node = _Node({"B"})
        node._split_mixed_target_if_blocks(lines)

        self.assertEqual(_count(lines, _if_line()), 2)
        self.assertEqual(_count(lines, "endif"), 2)
        self.assertLess(lines.index(_mesh("A")), lines.index(_mesh("B")))
        if_indices = [i for i, line in enumerate(lines) if str(line).strip() == _if_line()]
        self.assertIn(_mesh("A"), _block_between_if_and_endif(lines, if_indices[0]))
        self.assertIn(_mesh("B"), _block_between_if_and_endif(lines, if_indices[1]))

    def test_top_level_non_target_after_target_is_reordered(self):
        """顶层目标 mesh 在非目标之前：目标重排到段尾（非目标在前）。"""
        lines = [
            _mesh("Target"), "ps-t0 = TargetRes", "drawindexed = 1,0,0",
            _mesh("Default"), "ps-t0 = DefaultRes", "drawindexed = 2,0,0",
            ";MARK: tail",
        ]
        node = _Node({"Target"})
        node._move_top_level_targets_to_end(lines)

        self.assertLess(lines.index(_mesh("Default")), lines.index(_mesh("Target")))
        self.assertEqual(lines[-1], ";MARK: tail")

    def test_nested_material_switch_if_follows_target_block(self):
        """目标 mesh 后紧跟材质切换嵌套 if：随目标块整体移动，不拆散。"""
        lines = [
            _mesh("Target"), _if_line("$material_switch == 1"),
            MATERIAL_SWITCH_COMMENT, "ps-t0 = TargetRes", "drawindexed = 1,0,0",
            "endif",
            _mesh("Default"), "ps-t0 = DefaultRes", "drawindexed = 2,0,0",
            ";MARK: tail",
        ]
        node = _Node({"Target"})
        node._move_top_level_targets_to_end(lines)

        index_target = lines.index(_mesh("Target"))
        self.assertLess(lines.index(_mesh("Default")), index_target)
        # 嵌套 if 与它的 endif 必须完整跟随目标 mesh（中间不被默认部件插入）。
        self.assertEqual(
            lines[index_target + 1:index_target + 6],
            [
                _if_line("$material_switch == 1"),
                MATERIAL_SWITCH_COMMENT,
                "ps-t0 = TargetRes",
                "drawindexed = 1,0,0",
                "endif",
            ],
        )
        self.assertEqual(_count(lines, "endif"), 1)
        self.assertEqual(lines[-1], ";MARK: tail")

    def test_pure_blocks_not_split(self):
        """纯目标块 / 纯非目标块：不拆分；纯目标块已在非目标之后则输出不变。"""
        lines = [
            _if_line("$switch == 2"),
            _mesh("Default"), "ps-t0 = DefaultRes", "drawindexed = 2,0,0",
            "endif",
            _if_line("$switch == 1"),
            _mesh("Target"), "ps-t0 = TargetRes", "drawindexed = 1,0,0",
            "endif",
            ";MARK: tail",
        ]
        node = _Node({"Target"})
        snapshot = list(lines)
        node._split_mixed_target_if_blocks(lines)
        self.assertEqual(lines, snapshot)

        node._split_mixed_target_if_blocks(lines)
        self.assertEqual(lines, snapshot)

    def test_pure_target_block_moved_after_later_non_target(self):
        """S3：纯目标 if 块后面还有顶层非目标 mesh → 目标块整体移到段尾。"""
        lines = [
            "ps-t0 = CustomRes",
            _if_line(),
            _mesh("B"), "drawindexed = 200,0,0",
            "endif",
            _mesh("A"), "drawindexed = 100,0,0",
            ";MARK: tail",
        ]
        node = _Node({"B"})
        node._split_mixed_target_if_blocks(lines)

        self.assertLess(lines.index(_mesh("A")), lines.index(_mesh("B")),
                        "非目标 A 必须排在目标块 B 之前")
        self.assertLess(lines.index(_mesh("B")), lines.index(";MARK: tail"),
                        "目标块必须位于段尾 ;MARK: 之前")
        self.assertEqual(_count(lines, _if_line()), 1)
        self.assertEqual(_count(lines, "endif"), 1)
        self.assertEqual(lines[-1], ";MARK: tail")

        def _chain():
            node._split_mixed_target_if_blocks(lines)
            node._move_targets_to_end_of_top_level_if_blocks(lines)
            node._move_top_level_targets_to_end(lines)

        # 与既有目标重排串起来跑两遍：顺序不变、幂等。
        _chain()
        snapshot = list(lines)
        _chain()
        self.assertEqual(lines, snapshot)
        self.assertLess(lines.index(_mesh("A")), lines.index(_mesh("B")))
        self.assertEqual(_count(lines, "endif"), 1)

    def test_top_level_target_and_pure_target_block_both_after_non_target(self):
        """目标顶层 mesh + 纯目标 if 块：最终都排在非目标之后、;MARK: 之前。"""
        lines = [
            _mesh("Target"), "ps-t0 = TargetRes", "drawindexed = 1,0,0",
            _if_line(),
            _mesh("B"), "drawindexed = 200,0,0",
            "endif",
            _mesh("A"), "drawindexed = 100,0,0",
            ";MARK: tail",
        ]
        node = _Node({"B", "Target"})
        node._split_mixed_target_if_blocks(lines)
        node._move_targets_to_end_of_top_level_if_blocks(lines)
        node._move_top_level_targets_to_end(lines)

        index_a = lines.index(_mesh("A"))
        index_target = lines.index(_mesh("Target"))
        index_b = lines.index(_mesh("B"))
        mark = lines.index(";MARK: tail")
        self.assertLess(index_a, index_target)
        self.assertLess(index_a, index_b)
        self.assertLess(index_target, mark)
        self.assertLess(index_b, mark)
        self.assertEqual(_count(lines, "endif"), 1)

        snapshot = list(lines)
        node._split_mixed_target_if_blocks(lines)
        node._move_targets_to_end_of_top_level_if_blocks(lines)
        node._move_top_level_targets_to_end(lines)
        self.assertEqual(lines, snapshot)

    def test_else_block_is_skipped(self):
        """含 else 的 if 块：保守跳过，不破坏 if/else/endif 结构。"""
        lines = [
            _if_line(),
            _mesh("A"), "ps-t0 = ARes", "drawindexed = 1,0,0",
            "else",
            _mesh("B"), "ps-t0 = BRes", "drawindexed = 2,0,0",
            "endif",
            ";MARK: tail",
        ]
        node = _Node({"B"})
        snapshot = list(lines)
        node._split_mixed_target_if_blocks(lines)

        self.assertEqual(lines, snapshot)
        self.assertEqual(_count(lines, "else"), 1)
        self.assertEqual(_count(lines, _if_line()), 1)
        self.assertEqual(_count(lines, "endif"), 1)


if __name__ == "__main__":
    unittest.main()
