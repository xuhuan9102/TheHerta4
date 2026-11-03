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
