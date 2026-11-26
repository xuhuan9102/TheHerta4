"""回归：ini 里 drawindexed 的行序必须等于蓝图收集顺序。

被测（全部真实实现，只有依赖叶子件是 stub）：

    common/m_ini_helper.py::M_IniHelper.get_drawindexed_str_list
    common/m_ini_helper.py::M_IniHelper.get_drawindexed_instanced_str_list

根因（洛克茜模组实测，2026-10-08）：旧实现把 DrawCallModel 按 `get_condition_str()`
聚合进一个 dict，再按 dict（= 条件首次出现顺序）输出整组 —— 于是一批
「无任何物体切换门控」（`condition_str == ""`）的物体被整体挪到该条件首次出现的位置。
实测 K:\\SSMT-Package-master\\3Dmigoto\\ZZZ\\Mods\\SSMTGeneratedMod\\洛克茜\\洛克茜.ini 的
`[TextureOverride_LOD0.d5e1e7ff_61305_0]` 段（L456-605）：链序 5 的 `.001`、
链序 13 的 `Body 洛克茜.001`、链序 24 的 `Body 洛克茜` 被一起输出在第 4-6 位，
而索引偏移仍严格按收集顺序分配（10500 / 26682 / 76278）—— 用户看到的
「没接物体切换节点的物体跑到最上面」即由此而来。

可证伪性：`test_ungated_objects_keep_collected_position` 在修复前必红 —— 旧实现输出
`脸部, 脸部.001, 脸部.002, .001, Body.001, Body, 切断器, ...`，而期望是
`脸部, 脸部.001, 脸部.002, .001, 切断器, 切断器.001, Body.005, Body.001, ...`。
本文件同时内置旧算法的参考实现（`_legacy_grouped_order`），断言它复现那条错误顺序，
从而证明这组用例确实钉在回归点上、而不是恒真。
"""

import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PKG = "_drawindexed_condition_order_test_pkg"

M_INI_HELPER_PATH = REPO_ROOT / "common" / "m_ini_helper.py"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


# --- 依赖叶子件 stub（被测函数本身完全不碰它们） -------------------------------
_install_module(f"{PKG}.common.m_key", M_Key=type("M_Key", (), {}))
# 注解 `list[DrawCallModel]` 在 def 时求值，所以这里必须是个可调用对象。
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(get_workspace_name=lambda: ""),
)
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(),
)
_install_module(f"{PKG}.common.workspace_helper", WorkSpaceHelper=types.SimpleNamespace())
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        debug=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    ),
)


def _load_real(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# 真实 m_ini_builder（纯标准库，m_ini_helper 靠 `from .m_ini_builder import *` 取工具）
_load_real(f"{PKG}.common.m_ini_builder", REPO_ROOT / "common" / "m_ini_builder.py")
# 真实 m_ini_helper —— 被测本体
m_ini_helper = _load_real(f"{PKG}.common.m_ini_helper", M_INI_HELPER_PATH)

M_IniHelper = m_ini_helper.M_IniHelper


class DrawActivationTests(unittest.TestCase):
    def test_driver_can_request_activation_without_branch_keys(self):
        ini = m_ini_helper.M_IniBuilder()
        M_IniHelper.add_branch_key_sections(ini, {}, active_mod_count=1)
        self.assertEqual(
            [(section.SectionName, section.SectionLineList) for section in ini.ini_section_list],
            [("Constants", ["global $active0"]), ("Present", ["post $active0 = 0"])],
        )

    def test_export_without_keys_or_driver_adds_no_activation_sections(self):
        ini = m_ini_helper.M_IniBuilder()
        M_IniHelper.add_branch_key_sections(ini, {})
        self.assertEqual(ini.ini_section_list, [])


class FakeDrawCallModel:
    """只实现发射器用得到的那几个成员。"""

    def __init__(self, obj_name, condition_str="", index_count=100, index_offset=0, vertex_count=10):
        self.obj_name = obj_name
        self.condition_str = condition_str
        self.index_count = index_count
        self.index_offset = index_offset
        self.vertex_count = vertex_count

    def get_condition_str(self):
        return self.condition_str

    def get_drawindexed_str(self, obj_name_draw_offset_dict=None, base_vertex=0):
        offset = self.index_offset
        if obj_name_draw_offset_dict and self.obj_name in obj_name_draw_offset_dict:
            offset = obj_name_draw_offset_dict[self.obj_name]
        return f"drawindexed = {self.index_count},{offset},{base_vertex}"

    def get_drawindexed_instanced_str(self, obj_name_draw_offset_dict=None):
        return f"drawindexedinstanced = {self.index_count},1,0,0,0"


MESH_COMMENT_RE = re.compile(r"; \[mesh:(.*?)\] \[vertex_count:(\d+)\]")


def _mesh_order(lines):
    """按产物顺序取出 mesh 注释里的物体名。"""
    return [MESH_COMMENT_RE.search(line).group(1) for line in lines if MESH_COMMENT_RE.search(line)]


def _legacy_grouped_order(models):
    """旧算法（按 condition_str 聚合）的参考实现——只用于证明用例钉在回归点上。"""
    buckets = {}
    for model in models:
        buckets.setdefault(model.get_condition_str(), []).append(model)
    return [model.obj_name for group in buckets.values() for model in group]


def _locke_family():
    """洛克茜 `[TextureOverride_LOD0.d5e1e7ff_61305_0]` 段的缩略复刻。

    括号里是蓝图链序（= collect_connected_start_nodes / processing_chains 顺序），
    与 ini 实测行号一一对应。
    """
    return [
        FakeDrawCallModel("脸部", "$swapkey17 == 0"),                  # 链序 0  → ini L468
        FakeDrawCallModel("脸部.001", "$swapkey17 == 0"),              # 链序 1  → ini L473
        FakeDrawCallModel("脸部.002", "$swapkey17 == 0"),              # 链序 2  → ini L475
        FakeDrawCallModel(".001", ""),                                 # 链序 5  → ini L478
        FakeDrawCallModel("切断器", "$swapkey15 == 1"),                 # 链序 8  → ini L494
        FakeDrawCallModel("切断器.001", "$swapkey15 == 1"),             # 链序 9  → ini L499
        FakeDrawCallModel("Body 洛克茜.005", "$swapkey19 == 0"),        # 链序 12 → ini L518
        FakeDrawCallModel("Body 洛克茜.001", ""),                       # 链序 13 → ini L483
        FakeDrawCallModel("Body 洛克茜.003", "$swapkey20 == 0"),        # 链序 14 → ini L525
        FakeDrawCallModel("Body 洛克茜", ""),                           # 链序 24 → ini L488
        FakeDrawCallModel("Body 洛克茜.002", "$swapkey27 == 0"),        # 链序 25 → ini L583
    ]


class DrawIndexedConditionOrderTests(unittest.TestCase):
    # --- 核心回归 -----------------------------------------------------------
    def test_ungated_objects_keep_collected_position(self):
        """无门控物体必须留在被收集到的位置，不能被挪到最前。"""
        models = _locke_family()
        expected = [
            "脸部", "脸部.001", "脸部.002",
            ".001",
            "切断器", "切断器.001",
            "Body 洛克茜.005",
            "Body 洛克茜.001",
            "Body 洛克茜.003",
            "Body 洛克茜",
            "Body 洛克茜.002",
        ]

        lines = M_IniHelper.get_drawindexed_str_list(models)
        self.assertEqual(_mesh_order(lines), expected)

        # `Body 洛克茜.001`（无门控）恰好夹在 .005 与 .003 之间 —— 用户报告里
        # 「Body 洛克茜 应当在 .005 和 .003 中间」说的就是这个位置。
        order = _mesh_order(lines)
        self.assertEqual(order.index("Body 洛克茜.001"), order.index("Body 洛克茜.005") + 1)
        self.assertEqual(order.index("Body 洛克茜.001"), order.index("Body 洛克茜.003") - 1)

    def test_legacy_algorithm_reproduces_the_reported_symptom(self):
        """证明这条用例确实钉在回归点上：旧算法会把两个无门控物体抬到第 5/6 位。"""
        models = _locke_family()
        legacy = _legacy_grouped_order(models)
        self.assertEqual(
            legacy,
            [
                "脸部", "脸部.001", "脸部.002",
                ".001", "Body 洛克茜.001", "Body 洛克茜",
                "切断器", "切断器.001",
                "Body 洛克茜.005", "Body 洛克茜.003",
                "Body 洛克茜.002",
            ],
        )
        self.assertNotEqual(
            legacy,
            [model.obj_name for model in models],
            "旧算法必须与收集顺序不同，否则本文件的回归点是假的",
        )

    def test_line_order_always_equals_input_order(self):
        """任意序列下，产物里的 mesh 行序 == 传入顺序。"""
        cases = [
            [],
            [FakeDrawCallModel("only", "")],
            [FakeDrawCallModel("only", "$k == 1")],
            [FakeDrawCallModel("a", ""), FakeDrawCallModel("b", ""), FakeDrawCallModel("c", "")],
            [
                FakeDrawCallModel("a", "$k == 1"),
                FakeDrawCallModel("b", "$k == 2"),
                FakeDrawCallModel("c", "$k == 1"),
                FakeDrawCallModel("d", ""),
                FakeDrawCallModel("e", "$k == 2"),
            ],
            [FakeDrawCallModel(f"m{i}", "" if i % 3 else f"$k{i} == 1") for i in range(20)],
        ]
        for models in cases:
            with self.subTest(names=[model.obj_name for model in models]):
                lines = M_IniHelper.get_drawindexed_str_list(models)
                self.assertEqual(_mesh_order(lines), [model.obj_name for model in models])

    # --- 相邻合并 / 不相邻拆块 ----------------------------------------------
    def test_adjacent_same_condition_share_one_if_block(self):
        models = [
            FakeDrawCallModel("a", "$k == 1"),
            FakeDrawCallModel("b", "$k == 1"),
            FakeDrawCallModel("c", "$k == 1"),
        ]
        lines = M_IniHelper.get_drawindexed_str_list(models)
        self.assertEqual(lines.count("if $k == 1"), 1)
        self.assertEqual(lines.count("endif"), 1)
        self.assertEqual(lines.count(""), 1)
        self.assertEqual(
            lines,
            [
                "if $k == 1",
                "  ; [mesh:a] [vertex_count:10]",
                "  drawindexed = 100,0,0",
                "  ; [mesh:b] [vertex_count:10]",
                "  drawindexed = 100,0,0",
                "  ; [mesh:c] [vertex_count:10]",
                "  drawindexed = 100,0,0",
                "endif",
                "",
            ],
        )

    def test_non_adjacent_same_condition_get_their_own_blocks(self):
        models = [
            FakeDrawCallModel("a", "$k == 1"),
            FakeDrawCallModel("b", "$k == 2"),
            FakeDrawCallModel("c", "$k == 1"),
        ]
        lines = M_IniHelper.get_drawindexed_str_list(models)
        self.assertEqual(lines.count("if $k == 1"), 2)
        self.assertEqual(lines.count("if $k == 2"), 1)
        self.assertEqual(lines.count("endif"), 3)
        self.assertEqual(_mesh_order(lines), ["a", "b", "c"])

    def test_ungated_objects_emit_without_if_and_without_indent(self):
        models = [
            FakeDrawCallModel("gated", "$k == 1"),
            FakeDrawCallModel("free", ""),
        ]
        lines = M_IniHelper.get_drawindexed_str_list(models)
        self.assertEqual(
            lines,
            [
                "if $k == 1",
                "  ; [mesh:gated] [vertex_count:10]",
                "  drawindexed = 100,0,0",
                "endif",
                "",
                "; [mesh:free] [vertex_count:10]",
                "drawindexed = 100,0,0",
                "",
            ],
        )

    def test_instanced_variant_keeps_order_and_legacy_indent_for_ungated(self):
        models = [
            FakeDrawCallModel("gated", "$k == 1"),
            FakeDrawCallModel("free", ""),
            FakeDrawCallModel("gated2", "$k == 1"),
        ]
        lines = M_IniHelper.get_drawindexed_instanced_str_list(models)
        self.assertEqual(_mesh_order(lines), ["gated", "free", "gated2"])
        # 无门控物体的命令行在旧实现里就带 2 空格缩进，修复不动它。
        self.assertIn("  drawindexedinstanced = 100,1,0,0,0", lines)
        idx = lines.index("; [mesh:free] [vertex_count:10]")
        self.assertEqual(lines[idx + 1], "  drawindexedinstanced = 100,1,0,0,0")
        self.assertEqual(lines.count("if $k == 1"), 2)

    # --- 与旧行为等价的边界 ------------------------------------------------
    def test_empty_input_returns_empty_list(self):
        self.assertEqual(M_IniHelper.get_drawindexed_str_list([]), [])
        self.assertEqual(M_IniHelper.get_drawindexed_instanced_str_list([]), [])

    def test_single_gated_model_still_produces_if_block(self):
        """shader-replace 路径会传单元素列表，行为必须与旧实现一致。"""
        lines = M_IniHelper.get_drawindexed_str_list([FakeDrawCallModel("solo", "$k == 1")])
        self.assertEqual(
            lines,
            [
                "if $k == 1",
                "  ; [mesh:solo] [vertex_count:10]",
                "  drawindexed = 100,0,0",
                "endif",
                "",
            ],
        )

    def test_base_vertex_and_draw_offset_dict_are_forwarded(self):
        models = [FakeDrawCallModel("m", "$k == 1", index_count=42, index_offset=7)]
        lines = M_IniHelper.get_drawindexed_str_list(models, base_vertex=3)
        self.assertIn("  drawindexed = 42,7,3", lines)

        lines = M_IniHelper.get_drawindexed_str_list(models, {"m": 999})
        self.assertIn("  drawindexed = 42,999,0", lines)

        # 无条件物体同样要吃到 base_vertex / offset 重定向
        free = [FakeDrawCallModel("f", "", index_count=5, index_offset=1)]
        self.assertIn("drawindexed = 5,1,4", M_IniHelper.get_drawindexed_str_list(free, base_vertex=4))
        self.assertIn("drawindexed = 5,88,0", M_IniHelper.get_drawindexed_str_list(free, {"f": 88}))

    def test_if_endif_stay_balanced_for_every_sequence(self):
        models = [
            FakeDrawCallModel("a", "$k == 1"),
            FakeDrawCallModel("b", ""),
            FakeDrawCallModel("c", "$k == 2"),
            FakeDrawCallModel("d", "$k == 1"),
            FakeDrawCallModel("e", ""),
        ]
        for lines in (
            M_IniHelper.get_drawindexed_str_list(models),
            M_IniHelper.get_drawindexed_instanced_str_list(models),
        ):
            depth = 0
            for line in lines:
                if line.startswith("if "):
                    self.assertEqual(depth, 0, "if 不得嵌套")
                    depth += 1
                elif line == "endif":
                    self.assertEqual(depth, 1, "endif 不得悬空")
                    depth = 0
            self.assertEqual(depth, 0, "if/endif 必须成对")


class NoConditionGroupingRegressionTests(unittest.TestCase):
    """源码护栏：不许再把 condition 聚合回 dict 分组。"""

    def test_no_condition_bucket_dict_in_source(self):
        source = M_INI_HELPER_PATH.read_text(encoding="utf-8")
        self.assertNotIn(
            "condition_str_obj_model_list_dict",
            source,
            "M_IniHelper 不得再按 condition_str 聚合物体：那会让 ini 行序脱离蓝图收集顺序",
        )

    def test_both_emitters_preserve_order(self):
        """两个发射器都必须按传入顺序迭代（不再先分组）。"""
        source = M_INI_HELPER_PATH.read_text(encoding="utf-8")
        self.assertIn("run_obj_model_list", source)
        self.assertEqual(source.count("for obj_model in ordered_draw_obj_model_list:"), 2)


if __name__ == "__main__":
    unittest.main()
