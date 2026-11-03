# -*- coding: utf-8 -*-
"""流体探针节点里两条「要靠 bpy 才跑得到」的契约。

本机没有 Blender，所以这里装一个最小 ``bpy`` 桩（与 ``tests`` 里其它 node 测试
同一套写法）：只实现 ``node_postprocess_fluid`` 在 import 期与这两条路径上真正
碰到的属性。桩缺东西会当场 ``AttributeError``，不会假装通过。

覆盖：
1. **发射器上限 8**：已经 8 个时 ``ssmt.fluid_emitter_add`` 返回 ``CANCELLED``，
   report 里带「上限 8」（第 9 个没有槽位也没有 ``$fluid_emit_8`` 可拨）；
2. **导出键位**：第 1 个发射器留空 → 产物自动出一条
   ``[KeyFluidEmit0] key = no_alt no_ctrl no_shift VK_NUMPAD1``；
   第 2 个起留空 → 不出 ``[KeyFluidEmit1]``（不绑）。

纯文本层面的键位表（``GLOBAL_KEY_SECTIONS`` / ``GLOBAL_KEY_DEFAULTS``）在
``test_fluid_probe_ini.py`` 里，那份不 import bpy。
"""

import importlib
import sys
import types
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO / "Toolset" / "fluid" / "Probe.ini"
_PKG = "_fluid_probe_node_keys_pkg"


def _install_module(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


class _Prop(object):
    """``bpy.props.XxxProperty(...)`` 的占位：类体只需要它可赋值。"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _Operator(object):
    """``bpy.types.Operator`` 的桩：``report()`` 收进 ``_reports`` 供断言。"""

    def __init__(self, *args, **kwargs):
        self._reports = []

    def report(self, level, message):
        self._reports.append(message)


def _install_bpy():
    bpy = _install_module("bpy")
    base = type("_Base", (object,), {"__init__": lambda self, *a, **k: None})
    bpy.types = types.SimpleNamespace(
        PropertyGroup=base,
        Operator=_Operator,
        UIList=base,
        Panel=base,
        Menu=base,
        Node=base,
        NodeTree=base,
    )
    bpy.props = types.SimpleNamespace(**{
        name: _Prop for name in (
            "BoolProperty", "IntProperty", "FloatProperty", "StringProperty",
            "EnumProperty", "CollectionProperty", "PointerProperty",
            "FloatVectorProperty", "IntVectorProperty",
        )
    })
    bpy.app = types.SimpleNamespace(
        handlers=types.SimpleNamespace(
            depsgraph_update_post=[], load_post=[],
            persistent=lambda fn: fn,
        ),
        timers=types.SimpleNamespace(
            register=lambda *a, **k: None, unregister=lambda *a, **k: None,
            is_registered=lambda *a, **k: False),
        version=(4, 4, 0),
        driver_namespace={},
    )
    bpy.data = types.SimpleNamespace(
        collections=None, objects=None, meshes=None, materials=None,
        node_groups=None, scenes=[], filepath="",
    )
    bpy.context = types.SimpleNamespace(
        scene=None, window_manager=None, preferences=None, active_object=None)
    return bpy


def _install_package_stubs():
    """让 ``from .x`` / ``from ..y`` 解析到仓库里的真文件。

    真身是插件包 ``<addon>/blueprint/...``；这里用同名顶层包伪造同样的层级，
    只把 ``node_postprocess_base``（纯基类）换成空桩。
    """
    root = _install_module(_PKG)
    root.__path__ = [str(_REPO)]
    bp = _install_module(_PKG + ".blueprint")
    bp.__path__ = [str(_REPO / "blueprint")]
    common = _install_module(_PKG + ".common")
    common.__path__ = [str(_REPO / "common")]
    base = _install_module(_PKG + ".blueprint.node_postprocess_base")
    base.SSMTNode_PostProcess_Base = type("SSMTNode_PostProcess_Base", (object,), {})


_SAVED_BPY = sys.modules.get("bpy")
_install_bpy()
_install_package_stubs()
node = importlib.import_module(_PKG + ".blueprint.node_postprocess_fluid")
merge = node.fluid_ini_merge
# 模块已经拿到桩的引用（``node.bpy``），把 sys.modules 还回去，少污染别的测试文件。
if _SAVED_BPY is not None:
    sys.modules["bpy"] = _SAVED_BPY


def _emitter(key="", **extra):
    cfg = dict(key=key, name="emitter", object_name="Body", poly_index=0,
               force_fields=[])
    cfg.update(extra)
    return types.SimpleNamespace(**cfg)


def _bare_node(emitters):
    """不跑 ``__init__``（那要真 bpy），直接把导出路径需要的属性摆好。"""
    cls = node.SSMTNode_PostProcess_FluidProbe
    inst = cls.__new__(cls)
    for name, value in dict(
        remap_ini_params=False,
        gate_by_host_hash=False,
        include_shaders_cache=False,
        force_scale=1.0,
        particle_radius=0.0028,
        gravity_scale=1.0,
        viscosity=0.0,
        launch_speed=0.514,
        strength_level=3,
        depth_test=True,
        auto_pulse=False,
        tint_r=1.0, tint_g=1.0, tint_b=1.0,
        absorb_m=0.015,
        wet_darken=0.85,
        puddle_reflect=0.15,
        stream_reflect=1.6,
        scatter=0.35,
        refraction=0.02,
        emitters=emitters,
    ).items():
        setattr(inst, name, value)
    return inst


class EmitterCapacityTests(unittest.TestCase):
    def test_capacity_matches_the_slot_count(self):
        self.assertEqual(merge.EMITTER_CAPACITY, 8)
        self.assertEqual(merge.EMITTER_CAPACITY, merge.EMITTER_KEY_MAX)

    def test_ninth_emitter_is_rejected(self):
        inst = _bare_node([_emitter() for _ in range(8)])
        op = node.SSMT_OT_FluidEmitterAdd()
        result = op.execute(types.SimpleNamespace(node=inst))
        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(any("上限 8" in message for message in op._reports),
                        "report 里应写清上限 8，实际: %r" % (op._reports,))
        self.assertEqual(len(inst.emitters), 8)


def _force_field(**extra):
    cfg = dict(enabled=True, kind="DIRECTIONAL", offset_u=0.1, offset_v=0.2,
               offset_w=0.3, axis_u=30.0, axis_v=1.0, axis_w=2.0,
               radius=0.5, strength=20.0, falloff=2.0)
    cfg.update(extra)
    return types.SimpleNamespace(**cfg)


def _data_floats(text):
    """从生成的主 ini 里取出 ``[ResourceFluidForceField]`` 的 data 行并切成浮点。"""
    for line in text.splitlines():
        if line.strip().startswith("data = "):
            return [float(v) for v in line.split("=", 1)[1].split()]
    raise AssertionError("产物里没有 data 行：{}".format(text[:200]))


class MirrorWorkflowTests(unittest.TestCase):
    """U 轴镜像：只在导出层按 ``_game_frame[6]`` 取反，且只反 U。

    依据：``ResolveSurface.hlsl:63-64`` 的 ``right=up×forward`` 在 X 镜像下手性
    翻转 ⇒ ``right_game = -M(right_blender)``；``ForceField.hlsl:97/:105`` 只乘
    right，不再取反（两处都翻等于没翻）。
    """

    def test_mirrored_emitter_negates_u_in_the_exported_row(self):
        inst = _bare_node([_emitter(force_fields=[_force_field()])])
        inst._game_frame = lambda em: (0, 0, 0, 0, 0, 0, True)
        vals = _data_floats(inst._build_main_ini("FluidTest", []))
        # row0 = (offset, type)：U 反号，V/W 原样
        self.assertEqual(vals[0], -0.1)
        self.assertEqual(vals[1], 0.2)
        self.assertEqual(vals[2], 0.3)
        # row1 = (axis, radius)：U 反号，V/W 原样
        self.assertEqual(vals[4], -30.0)
        self.assertEqual(vals[5], 1.0)
        self.assertEqual(vals[6], 2.0)

    def test_non_mirrored_emitter_keeps_u(self):
        inst = _bare_node([_emitter(force_fields=[_force_field()])])
        inst._game_frame = lambda em: (0, 0, 0, 0, 0, 0, False)
        vals = _data_floats(inst._build_main_ini("FluidTest", []))
        self.assertEqual(vals[0], 0.1)
        self.assertEqual(vals[4], 30.0)

    def test_unresolvable_frame_is_treated_as_non_mirrored(self):
        """``_game_frame`` 返回 None（物体缺失/面号越界/手填 first_index）⇒ 不翻。

        这是**有意**的：判据一个都拿不到就不动 U（非镜像模型占多数，翻错会让
        水柱朝反方向）。镜像模型走到这条分支时先修面号/first_index 再导出。
        """
        inst = _bare_node([_emitter(force_fields=[_force_field()])])
        inst._game_frame = lambda em: None
        slots, warnings = inst._force_fields_by_slot()
        self.assertEqual(warnings, [])
        self.assertFalse(slots[0][0]["mirror_u"])
        vals = _data_floats(inst._build_main_ini("FluidTest", []))
        self.assertEqual(vals[0], 0.1)
        self.assertEqual(vals[4], 30.0)


class FamilyFlagTests(unittest.TestCase):
    """力场族标记 ``w41``：导出里必须正好一条声明，且缺省写 0（M3 防粘滞）。"""

    def test_family_flag_is_written_once_as_a_literal(self):
        text = _bare_node([_emitter()])._build_main_ini("FluidTest", [])
        self.assertEqual(
            text.count("w41 = "), 1,
            "族标记只能有一行；多余的行会互相覆盖")
        self.assertIn("    w41 = 0", text)
        self.assertNotIn("$fluid_force_fields_switchable", text,
                         "族标记写 literal，不再引出一个跨 mod 粘滞的 global")

    def test_disabled_field_turns_the_family_flag_on(self):
        emitter = _emitter(force_fields=[_force_field(enabled=False)])
        inst = _bare_node([emitter])
        inst._game_frame = lambda em: None   # 没有真 bpy，手性判据按非镜像走
        text = inst._build_main_ini("FluidTest", [])
        self.assertIn("    w41 = 1", text)


class EmitterKeyExportTests(unittest.TestCase):
    def test_first_emitter_defaults_to_numpad1(self):
        text = _bare_node([_emitter(), _emitter()])._build_main_ini("FluidTest", [])
        self.assertIn("[KeyFluidEmit0]", text)
        self.assertIn("key = no_alt no_ctrl no_shift VK_NUMPAD1", text)
        # 第 2 个留空 = 不绑，不许凭空多一段抢键
        self.assertNotIn("[KeyFluidEmit1]", text)
        # 模板里的 KeyToggle 已删，VK_NUMPAD1 只该出现这一次（发射器 0 的绑定）
        self.assertEqual(text.count("VK_NUMPAD1"), 1)

    def test_explicit_key_on_the_second_emitter_is_kept(self):
        text = _bare_node([_emitter(), _emitter(key="VK_F1")])._build_main_ini(
            "FluidTest", [])
        self.assertIn("[KeyFluidEmit1]", text)
        self.assertIn("key = no_alt no_ctrl no_shift VK_F1", text)

    def test_exported_template_has_no_manual_master_switch(self):
        text = _bare_node([_emitter()])._build_main_ini("FluidTest", [])
        for var in ("$fluid_enabled", "$fluid_effect_requested",
                    "$fluid_shutdown_remaining", "$fluid_last_effect_request"):
            self.assertNotIn(var, text, "产物里不该再有 %s" % var)


if __name__ == "__main__":
    unittest.main()
