# -*- coding: utf-8 -*-
"""贴图切换热键的角色门控（回归：材质转资源pro / 贴图切换 V5.1 的 KeySwap 段缺门控）。

实机产物取证
------------
``K:\\...\\3Dmigoto\\ZZZ\\Mods\\新建文件夹\\克拉蕾\\克拉蕾.ini``（22821 行）：

- 全文件 39 段 KeySwap，除下面两段外**全部**是 ``condition = $active0 == 1``；
- 只有材质转资源pro 写的 ``[KeySwap_Diffuse_swapkey150]`` / ``[KeySwap_Diffuse_swapkey151]``
  （L6417/L6424）是 ``condition = $swapkey150 == 0 || $swapkey150 < 2`` —— 没有角色门控；
- 同一 ini 的门控三件套齐全：``[Constants] global $active0``（L1313）、各部件
  ``$active0 = 1``（L2420/2470/2508/2564）、``[Present] post $active0 = 0``（L1951）。

后果：同一个热键不只切本角色，也会在别的角色被绘制时把 ``$swapkeyN`` 改掉（键条件
与 ``$active0`` 无关），而贴图切换块本身在部件段内 —— 于是"按一次键，别人也换"。

门控口径
--------
**只在 ini 具备门控机制时加门控**：整套机制（``[Constants] global $active0`` +
部件 ``$active0 = 1`` + ``[Present] post $active0 = 0``）由导出器在「蓝图存在切换
按键」时整体发射；没有这套机制的 ini 加 ``condition = $active0 == 1`` 只会让热键
恒假（未声明变量按 0 读），比不加门控更糟。判据与 ``anim_driver_base._get_activation_flag``
（NTEMI 用 ``$ntmi_active0``）、``node_swap_ini`` 同源。

**判据必须同时认「声明」与「置位」**：段字典按段名去重，而实测产物（克拉蕾.ini）
有 12 个 ``[Constants]``，``global $active0`` 所在的段会被后面的 ``[Constants]`` 顶掉
—— 只看声明会漏判（这正是第一次复算时的实际失败），而置位行在唯一的
``[TextureOverride_VB_*]`` 段里，字典里一定还在。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path


PKG = "_texture_switch_activation_gate_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.toolkit"):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package


class _FakeOperator:
    def report(self, levels, message):  # pragma: no cover - 仅为类体可定义
        pass


_fake_bpy = types.ModuleType("bpy")
_fake_bpy.types = types.SimpleNamespace(
    PropertyGroup=object,
    Operator=_FakeOperator,
    Object=object,
)
_fake_bpy.props = types.SimpleNamespace(
    StringProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
    PointerProperty=lambda **_kwargs: None,
)
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups=[])
sys.modules["bpy"] = _fake_bpy

# 贴图切换的 copy_texture 会调用 toolkit 的 DDS 转换；本测试只关心 KeySwap 门控，
# 桩成「转换成功」即可（真正转换在 test_dds_conversion 里单独覆盖）。
_toolkit_stub = types.ModuleType(f"{PKG}.toolkit.tt_dds_conversion")
_toolkit_stub.convert_texture_to_dds = lambda *_args, **_kwargs: (True, "")
_toolkit_stub.resolve_texture_dds_format = lambda *_args, **_kwargs: "bc7_unorm"
_toolkit_stub.existing_dds_matches_format = lambda *_args, **_kwargs: True
sys.modules[_toolkit_stub.__name__] = _toolkit_stub

# 材质转资源pro 的基类来自 node_postprocess_material（此处用桩）。
_material_stub = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_stub.MATERIAL_DETECT_PRESETS = []
_material_stub.SSMTNode_PostProcess_MaterialBase = type(
    "_StubMaterialBase",
    (object,),
    {"define_swapkeys_in_sections": lambda self, sections, keys_to_define: None},
)
sys.modules[_material_stub.__name__] = _material_stub

# 贴图切换 V5.1 的基类来自 node_postprocess_base（此处用桩）。
_base_stub = types.ModuleType(f"{PKG}.blueprint.node_postprocess_base")
_base_stub.SSMTNode_PostProcess_Base = type("_StubPostProcessBase", (object,), {})
sys.modules[_base_stub.__name__] = _base_stub


def _load(module_stem):
    module_path = Path(__file__).resolve().parents[1] / "blueprint" / f"{module_stem}.py"
    spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.{module_stem}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pro = _load("node_postprocess_custom_material_assign")
diffuse = _load("node_postprocess_diffuse_switch")


def _load_patch_tool():
    """tools/ 下的既有模组补丁脚本（纯标准库，可在 Blender 之外导入）。"""
    module_path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "patch_diffuse_keyswap_character_gate.py"
    )
    spec = importlib.util.spec_from_file_location(f"{PKG}.patch_diffuse_gate", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


patch_tool = _load_patch_tool()


LEGACY_150 = "condition = $swapkey150 == 0 || $swapkey150 < 2"
GATED_150 = "condition = $active0 == 1 && ($swapkey150 == 0 || $swapkey150 < 2)"


def _write_pro_section(sections):
    node = pro.SSMTNode_PostProcess_CustomMaterialAssign.__new__(
        pro.SSMTNode_PostProcess_CustomMaterialAssign
    )
    node._write_keyswap_section(
        sections,
        {
            "variable": "$swapkey150",
            "state_count": 2,
            "key": "No_Modifiers Numpad9",
            "comment": "身体涂鸦",
        },
    )
    return sections["[KeySwap_Diffuse_swapkey150]"]


class MaterialProActivationGateTests(unittest.TestCase):
    """材质转资源pro：``[KeySwap_Diffuse_*]`` 段必须带角色门控。"""

    def test_gates_when_ini_declares_active0(self):
        lines = _write_pro_section({"[Constants]": ["global $active0"]})

        self.assertIn(GATED_150, lines)

    def test_legacy_form_without_activation_flag(self):
        """没有门控机制时保持原样 —— 加门控会让热键恒假。"""
        lines = _write_pro_section({})

        self.assertIn(LEGACY_150, lines)
        self.assertNotIn("$active0", "\n".join(lines))

    def test_uses_ntmi_flag_when_that_is_the_declared_one(self):
        lines = _write_pro_section({"[Constants]": ["global persist $ntmi_active0 = 0"]})

        self.assertIn(
            "condition = $ntmi_active0 == 1 && ($swapkey150 == 0 || $swapkey150 < 2)",
            lines,
        )

    def test_active0_wins_when_both_flags_are_declared(self):
        lines = _write_pro_section(
            {"[Constants]": ["global $active0", "global $ntmi_active0 = 0"]}
        )

        self.assertIn(GATED_150, lines)

    def test_commented_out_declaration_does_not_gate(self):
        lines = _write_pro_section({"[Constants]": ["; global $active0"]})

        self.assertIn(LEGACY_150, lines)

    def test_setter_alone_is_enough(self):
        """实机产物形态：``global $active0`` 被后面的重复 [Constants] 顶掉，
        字典里只剩部件段的 ``$active0 = 1`` —— 仍必须加门控。"""
        sections = {
            "[TextureOverride_VB_480eeade_480eeade_Position]": [
                "vb2 = Resource480eeadeBlend",
                "$active0 = 1",
            ],
            "[Constants]": ["global persist $swapkey150 = 0"],
        }

        lines = _write_pro_section(sections)

        self.assertIn(GATED_150, lines)

    def test_gate_condition_lines_are_not_mistaken_for_setters(self):
        """``if $active0 == 1`` / ``post $active0 = 0`` / ``$swapkey150 = 0,1``
        都不是置位，不得因此加门控（否则门控恒假或自引用）。"""
        sections = {
            "[Present]": ["post $active0 = 0", "if $active0 == 1"],
            "[KeySwap_7]": ["condition = $active0 == 1", "$swapkey150 = 0,1"],
        }

        lines = _write_pro_section(sections)

        self.assertIn(LEGACY_150, lines)

    def test_declaration_outside_constants_is_still_found(self):
        """多 ini / 手改产物里声明可能不在 [Constants]，也要认。"""
        lines = _write_pro_section(
            {"[Present]": ["post $ui_active = 0"], "[Other]": ["global $active0"]}
        )

        self.assertIn(GATED_150, lines)

    def test_non_line_values_are_tolerated(self):
        """段字典里可能混入簿记键（非行列表），探测不得抛异常。"""
        lines = _write_pro_section({"_config_path": "E:/tmp/mod", "[Constants]": ["global $active0"]})

        self.assertIn(GATED_150, lines)

    def test_other_lines_are_unchanged(self):
        lines = _write_pro_section({"[Constants]": ["global $active0"]})

        self.assertEqual(lines[0], "; 身体涂鸦")
        self.assertIn("key = No_Modifiers Numpad9", lines)
        self.assertIn("type = cycle", lines)
        self.assertIn("$swapkey150 = 0,1", lines)


class DiffuseSwitchV51ActivationGateTests(unittest.TestCase):
    """贴图切换 V5.1：同款缺陷、同一门控口径。"""

    BASE = "[Constants]\nglobal $active0\n"

    def test_gates_when_flag_declared(self):
        out = diffuse.ensure_keyswap(
            self.BASE, "No_Modifiers Numpad9", 2, "身体涂鸦", var="DiffuseSwap_Group_ab12"
        )

        self.assertIn("[KeySwap_Diffuse_DiffuseSwap_Group_ab12]", out)
        self.assertIn(
            "condition = $active0 == 1"
            " && ($DiffuseSwap_Group_ab12 == 0 || $DiffuseSwap_Group_ab12 < 2)",
            out,
        )

    def test_legacy_form_without_flag(self):
        out = diffuse.ensure_keyswap("[Constants]\n$other = 0\n", "N", 2, "", var="V")

        self.assertIn("condition = $V == 0 || $V < 2", out)
        self.assertNotIn("$active0", out)

    def test_gui_guard_stays_inside_the_activation_gate(self):
        out = diffuse.ensure_keyswap(
            self.BASE, "N", 2, "", gui_guard="$dts_x_gui_only == 0", var="V"
        )

        self.assertIn(
            "condition = $active0 == 1 && (($V == 0 || $V < 2) && $dts_x_gui_only == 0)",
            out,
        )

    def test_rewrite_is_idempotent_with_gate(self):
        first = diffuse.ensure_keyswap(self.BASE, "N", 3, "c", var="V")
        second = diffuse.ensure_keyswap(first, "N", 3, "c", var="V")

        self.assertEqual(first, second)


class ActivationFlagParityTests(unittest.TestCase):
    """两个生成器必须同一口径：一处改了另一处忘改 = 门控只在一半产物上生效。"""

    TEXTS = (
        "global $active0\n",
        "global persist $active0 = 0\n",
        "global persist $ntmi_active0 = 0\n",
        "global $active0\nglobal $ntmi_active0 = 0\n",
        "; global $active0\n",
        "global $ui_active = 0\n",
        "global  $active0   = 0\n",
        "global\t$active0\n",
        "$active0 = 1\n",
        "$ntmi_active0 = 1\n",
        "post $active0 = 0\n",
        "if $active0 == 1\n",
        "condition = $active0 == 1\n",
        "$swapkey150 = 0,1\n",
        "",
    )

    def test_both_modules_agree_on_every_sample(self):
        for text in self.TEXTS:
            with self.subTest(text=text):
                self.assertEqual(
                    diffuse.find_activation_flag_in_text(text),
                    pro.find_activation_flag_in_sections({"[Constants]": text.splitlines()}),
                )
                self.assertEqual(
                    patch_tool.find_activation_flag(text),
                    diffuse.find_activation_flag_in_text(text),
                )

    def test_expected_verdicts(self):
        check = pro.find_activation_flag_in_sections
        self.assertEqual(check({"[Constants]": ["global $active0"]}), "$active0")
        self.assertEqual(
            check({"[Constants]": ["global persist $ntmi_active0 = 0"]}), "$ntmi_active0"
        )
        self.assertEqual(check({}), "")
        self.assertEqual(diffuse.find_activation_flag_in_text("; global $active0"), "")
        # 置位（实机产物形态，声明段可能已被重复 [Constants] 顶掉）
        self.assertEqual(
            check({"[TextureOverride_VB_x_x_Position]": ["$active0 = 1"]}), "$active0"
        )
        # 非置位形态
        self.assertEqual(check({"[Present]": ["post $active0 = 0"]}), "")
        self.assertEqual(check({"[KeySwap_1]": ["condition = $active0 == 1"]}), "")
        self.assertEqual(check({"[KeySwap_1]": ["$swapkey150 = 0,1"]}), "")


class ExistingModPatchToolTests(unittest.TestCase):
    """既有产物补丁脚本：只动 KeySwap_Diffuse_* 段、幂等、缺机制不动。"""

    PRODUCT = (
        "[Constants]\n"
        "global persist $swapkey150 = 0\n"
        "\n"
        "[TextureOverride_VB_480eeade_480eeade_Position]\n"
        "$active0 = 1\n"
        "\n"
        "[KeySwap_7]\n"
        "condition = $swapkey150 == 0 || $swapkey150 < 2\n"
        "key = No_Modifiers Numpad1\n"
        "$swapkey150 = 0,1\n"
        "\n"
        "[KeySwap_Diffuse_swapkey150]\n"
        "; 身体涂鸦\n"
        "condition = $swapkey150 == 0 || $swapkey150 < 2\n"
        "key = No_Modifiers Numpad9\n"
        "type = cycle\n"
        "$swapkey150 = 0,1\n"
    )

    def test_patches_only_the_diffuse_section(self):
        patched, changed, status = patch_tool.patch_text(self.PRODUCT)

        self.assertEqual((changed, status), (1, "patched"))
        self.assertEqual(patched.count(GATED_150), 1)
        # 物体切换段的同名 condition 不能被误改
        self.assertIn(
            "[KeySwap_7]\ncondition = $swapkey150 == 0 || $swapkey150 < 2\n", patched
        )

    def test_second_run_is_idempotent(self):
        patched, _, _ = patch_tool.patch_text(self.PRODUCT)
        again, changed, status = patch_tool.patch_text(patched)

        self.assertEqual((changed, status), (0, "already-gated"))
        self.assertEqual(again, patched)

    def test_skips_when_no_activation_machinery(self):
        text = self.PRODUCT.replace("$active0 = 1\n", "")

        patched, changed, status = patch_tool.patch_text(text)

        self.assertEqual((changed, status), (0, "no-activation-flag"))
        self.assertEqual(patched, text)


if __name__ == "__main__":
    unittest.main()
