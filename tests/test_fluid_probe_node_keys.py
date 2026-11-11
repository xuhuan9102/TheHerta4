import os
import shutil
import tempfile
# -*- coding: utf-8 -*-
"""流体探针节点里两条「要靠 bpy 才跑得到」的契约。

本机没有 Blender，所以这里装一个最小 ``bpy`` 桩（与 ``tests`` 里其它 node 测试
同一套写法）：只实现 ``node_postprocess_fluid`` 在 import 期与这两条路径上真正
碰到的属性。桩缺东西会当场 ``AttributeError``，不会假装通过。

覆盖：
1. **发射器上限 8**：已经 8 个时 ``ssmt.fluid_emitter_add`` 返回 ``CANCELLED``，
   report 里带「上限 8」（第 9 个没有槽位也没有 ``$fluid_emit_8`` 可拨）；
2. **导出键位**：留空 = 不绑 —— 产物里**一条 `[KeyFluidEmitN]` 都没有**，
   也不再自动补 `VK_NUMPAD1`（2026-11-05 撤掉那条默认）；显式填了才出段。

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


class TTLOrderExportTests(unittest.TestCase):
    def test_export_keeps_the_shared_ttl_namespace_and_local_scene_target(self):
        for namespace in ("Fluid_A", "Fluid_B"):
            text = _bare_node([_emitter()])._build_main_ini(namespace, [])
            commands = [line.strip() for line in
                        merge.parse_sections(text)["CommandListDrawWorld"]]
            target = r"Resource\TTL\FluidOrderTarget = ref ResourceSceneColor"
            call = r"run = CommandList\TTL\ApplyBeforeFluid"
            self.assertIn(target, commands)
            self.assertIn(call, commands)
            self.assertLess(commands.index(target), commands.index(call))
            self.assertLess(commands.index(call), commands.index(
                "ResourceFluidLighting = copy ResourceSceneColor"))


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
    def test_empty_key_binds_nothing(self):
        """留空 = 不绑键：一个 ``[KeyFluidEmitN]`` 段都不许有，也不许自作主张给 NUMPAD1。"""
        text = _bare_node([_emitter(), _emitter()])._build_main_ini("FluidTest", [])
        self.assertNotIn("[KeyFluidEmit0]", text)
        self.assertNotIn("[KeyFluidEmit1]", text)
        self.assertNotIn("VK_NUMPAD1", text)
        self.assertNotIn("$fluid_emit_0 = 1,0", text)

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


class SplashEnableExportTests(unittest.TestCase):
    """次生总开关：默认开不写任何行；关掉写 x61=1 / y61=0，紧跟 x47 之后。"""

    def test_default_on_writes_nothing(self):
        # RED-BY: node 里把 splash_enable 的默认改成 False（或无条件写 x61/y61）
        text = _bare_node([_emitter()])._build_main_ini("FluidTest", [])
        self.assertNotIn("x61", text)
        self.assertNotIn("y61", text)

    def test_off_writes_valid_bit_and_value_after_x47(self):
        # RED-BY: 删掉 node 里写 x61/y61 的那段 re.sub
        inst = _bare_node([_emitter()])
        inst.splash_enable = False
        text = inst._build_main_ini("FluidTest", [])
        self.assertIn("x61 = 1", text)
        self.assertIn("y61 = 0", text)
        anchor = text.index("x47 = $fluid_gravity_scale")
        self.assertGreater(text.index("x61 = 1"), anchor)
        self.assertGreater(text.index("y61 = 0"), anchor)


class IconSafetyTests(unittest.TestCase):
    """图标安全：非法 icon 只影响那一行，绝不抛异常把面板带死。"""

    class _L(object):
        def __init__(self, valid=('INFO',)):
            self.calls = []
            self.valid = set(valid)

        def label(self, text='', icon=None):
            self.calls.append((text, icon))
            if icon is not None and icon not in self.valid:
                raise TypeError('enum "%s" not found' % icon)

    def test_illegal_icon_falls_back_to_text(self):
        # RED-BY: node 里 _safe_label 去掉 except TypeError 的第二次 label
        lay = self._L()
        node._safe_label(lay, 'hello', 'NOT_AN_ICON_ENUM')
        self.assertEqual(len(lay.calls), 2)
        self.assertIsNone(lay.calls[-1][1])
        self.assertIn('NOT_AN_ICON_ENUM', lay.calls[-1][0])

    def test_legal_icon_passes_once(self):
        # RED-BY: _safe_label 无条件去掉 icon（合法图标也会丢）
        lay = self._L()
        node._safe_label(lay, 'hi', 'INFO')
        self.assertEqual(lay.calls, [('hi', 'INFO')])

    def test_enum_icons_returns_none_without_bpy(self):
        # RED-BY: _enum_icons 的 except 改成 raise
        had = hasattr(node.bpy.types, 'UILayout')
        saved = getattr(node.bpy.types, 'UILayout', None)
        node.bpy.types.UILayout = types.SimpleNamespace()
        try:
            self.assertIsNone(node._enum_icons())
        finally:
            if had:
                node.bpy.types.UILayout = saved
            else:
                delattr(node.bpy.types, 'UILayout')

    def test_validate_icons_flags_unknown_and_falls_back(self):
        # RED-BY: validate_icons 不再把非法名字写进 _ICON_FALLBACK
        saved_icons = dict(node.ICONS)
        saved_dump = node._dump_icon_list
        had_layout = hasattr(node.bpy.types, 'UILayout')
        saved_layout = getattr(node.bpy.types, 'UILayout', None)
        node.ICONS['bogus'] = 'NOT_A_REAL_ICON'
        node._dump_icon_list = lambda valid: None      # 别在仓库里落盘
        node.bpy.types.UILayout = types.SimpleNamespace(
            bl_rna=types.SimpleNamespace(
                functions={'prop': types.SimpleNamespace(
                    parameters={'icon': types.SimpleNamespace(
                        enum_items=[types.SimpleNamespace(identifier='INFO')])})}))
        try:
            bad = node.validate_icons()
            self.assertIn('NOT_A_REAL_ICON', bad)
            self.assertEqual(node._ICON_FALLBACK.get('NOT_A_REAL_ICON'), 'INFO')
        finally:
            node.ICONS.clear()
            node.ICONS.update(saved_icons)
            node._dump_icon_list = saved_dump
            if had_layout:
                node.bpy.types.UILayout = saved_layout
            else:
                delattr(node.bpy.types, 'UILayout')
            node._ICON_FALLBACK.clear()
            node._ICON_WARNINGS.clear()


class EmitterDrawIsolationTests(unittest.TestCase):
    """单条坏发射器只影响它自己：`_draw_emitter_safe` 的三条分支。"""

    class _Box(object):
        def __init__(self):
            self.labels = []
            self.columns = 0

        def label(self, text='', icon='', **kw):
            self.labels.append((text, icon))

        def column(self, align=False):
            self.columns += 1
            return self

    @staticmethod
    def _good():
        return types.SimpleNamespace(name='e0', object_name='Body', poly_index=1,
                                     key='', enable=True, speed=0.0, lifetime=1.1,
                                     force_fields=[])

    def test_bad_emitter_never_calls_the_drawer(self):
        # RED-BY: _draw_emitter_safe 去掉 _emitter_state 判断、直接调 drawer
        box = self._Box()
        calls = []
        ok = node._draw_emitter_safe(
            box, types.SimpleNamespace(name='e'), 0, lambda sub, e, i: calls.append(i))
        self.assertFalse(ok)
        self.assertEqual(calls, [])
        self.assertTrue(any('无效' in t for t, _i in box.labels))

    def test_good_emitter_draws_once(self):
        # RED-BY: _draw_emitter_safe 里把 drawer 调两次（或漏调）
        box = self._Box()
        calls = []
        ok = node._draw_emitter_safe(box, self._good(), 3, lambda sub, e, i: calls.append(i))
        self.assertTrue(ok)
        self.assertEqual(calls, [3])

    def test_drawer_exception_is_contained(self):
        # RED-BY: _draw_emitter_safe 去掉 try/except
        box = self._Box()

        def boom(sub, e, i):
            raise RuntimeError('boom')

        ok = node._draw_emitter_safe(box, self._good(), 1, boom)
        self.assertFalse(ok)
        self.assertTrue(any('画不出来' in t for t, _i in box.labels))


class EmitterStateTests(unittest.TestCase):
    """面板发射器详情的状态判定（纯函数）：缺属性的老条目只警示、不抛。"""

    def test_legacy_emitter_missing_fields(self):
        # RED-BY: _emitter_state 去掉 missing 检查，直接返回 True
        ok, label = node._emitter_state(types.SimpleNamespace(name='e'))
        self.assertFalse(ok)
        self.assertIn('缺', label)

    def test_full_emitter_is_ok(self):
        # RED-BY: _emitter_state 恒返回 False
        e = types.SimpleNamespace(name='e0', object_name='Body', poly_index=1,
                                  key='', enable=True, speed=0.0, lifetime=1.1,
                                  force_fields=[types.SimpleNamespace(enabled=True)])
        ok, label = node._emitter_state(e)
        self.assertTrue(ok)
        self.assertIn('Body', label)
        self.assertIn('1', label)


class StageProbeExportTests(unittest.TestCase):
    """阶段染色探针：默认关不写 x63；开着写 x63 = 1，且在 x47 之后。"""

    def test_default_off_writes_nothing(self):
        # RED-BY: node 里无条件写 x63 = 1
        text = _bare_node([_emitter()])._build_main_ini("FluidTest", [])
        self.assertNotIn("x63", text)

    def test_on_writes_one_after_x47(self):
        # RED-BY: 删掉 node 里写 x63 的那段 re.sub
        inst = _bare_node([_emitter()])
        inst.stage_probe = True
        text = inst._build_main_ini("FluidTest", [])
        self.assertIn("x63 = 1", text)
        self.assertGreater(text.index("x63 = 1"),
                           text.index("x47 = $fluid_gravity_scale"))


class StrengthLevelTests(unittest.TestCase):
    """发射量档位是独立输入：填了用填的，勾「跟随速度」才反解。"""

    def _emu(self, **kw):
        cfg = dict(remap_ini_params=False, gate_by_host_hash=False, force_scale=1.0,
                   particle_radius=0.02, gravity_scale=1.0, viscosity=0.05,
                   launch_speed=1.5, enabled=True, depth_test=True, auto_pulse=False,
                   tint_r=0.9, tint_g=0.75, tint_b=0.03, absorb_m=0.015,
                   wet_darken=0.85, puddle_reflect=0.15, stream_reflect=1.6,
                   scatter=0.35, refraction=0.02, include_shaders_cache=False,
                   exclude_draw_hashes="", emitters=[])
        cfg.update(kw)
        cls = node.SSMTNode_PostProcess_FluidProbe
        inst = cls.__new__(cls)
        for k, v in cfg.items():
            setattr(inst, k, v)
        return inst

    def test_manual_level_is_written(self):
        """显式填 4.0029 ⇒ 导出写 4.0029（不反解）。"""
        inst = self._emu(strength_level=4.0029, auto_strength_level=False)
        text = inst._build_main_ini("FluidTest", [])
        self.assertIn("global $fluid_strength_level = 4.0029", text)

    def test_auto_level_follows_speed(self):
        """勾「跟随速度」⇒ 速度 1.5 反解成 7。"""
        inst = self._emu(strength_level=4.0029, auto_strength_level=True)
        text = inst._build_main_ini("FluidTest", [])
        self.assertIn("global $fluid_strength_level = 7", text)
        self.assertNotIn("global $fluid_strength_level = 4.0029", text)


class NamespaceGuardTests(unittest.TestCase):
    """命名空间自检：裸 Fluid_mod / 撞名 / 引用他人 ns 都要被抓出来。"""

    def _node(self):
        return node.SSMTNode_PostProcess_FluidProbe.__new__(node.SSMTNode_PostProcess_FluidProbe)

    def test_namespace_has_hash_suffix(self):
        ns = node._namespace_id(os.path.join("Mods", "蕾米埃尔·月夜密语"))
        self.assertRegex(ns, r"^Fluid_[A-Za-z0-9_]*_[0-9a-f]{8}$")

    def test_two_chinese_mods_get_different_namespaces(self):
        a = node._namespace_id(os.path.join("Mods", "蕾米埃尔·月夜密语"))
        b = node._namespace_id(os.path.join("Mods", "菲欧妮"))
        self.assertNotEqual(a, b)

    def test_verify_flags_bare_and_foreign(self):
        inst = self._node()
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "fluid_probe.ini"), "w", encoding="utf-8") as fh:
                fh.write("namespace = Fluid_mod\n")
            problems = inst._verify_generated_ns(tmp, "Fluid_x_32dc3d21")
            self.assertTrue(any("裸 Fluid_mod" in p for p in problems))
            with open(os.path.join(tmp, "fluid_probe.ini"), "w", encoding="utf-8") as fh:
                fh.write("run = CommandList\\Fluid_mod_deadbeef\\MarkPresent\n")
            problems = inst._verify_generated_ns(tmp, "Fluid_x_32dc3d21")
            self.assertTrue(any("别的 namespace" in p for p in problems))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_conflict_detection_finds_same_namespace(self):
        inst = self._node()
        tmp = tempfile.mkdtemp()
        try:
            mine = os.path.join(tmp, "mods", "A")
            other = os.path.join(tmp, "mods", "B")
            os.makedirs(mine)
            os.makedirs(other)
            for path in (mine, other):
                with open(os.path.join(path, "fluid_probe.ini"), "w", encoding="utf-8") as fh:
                    fh.write("namespace = Fluid_same_12345678\n")
            hits = inst._namespace_conflicts(mine, "Fluid_same_12345678")
            self.assertEqual(len(hits), 1)
            self.assertIn("B", hits[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NamespaceVarRefTests(unittest.TestCase):
    """变量层：引用了别的模组 ns 的变量必须被自检抓出来。"""

    def test_foreign_variable_reference_is_flagged(self):
        inst = node.SSMTNode_PostProcess_FluidProbe.__new__(node.SSMTNode_PostProcess_FluidProbe)
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "fluid_probe.ini"), "w", encoding="utf-8") as fh:
                fh.write("x = $\\Fluid_mod_deadbeef\\fluid_emit_0\n")
            problems = inst._verify_generated_ns(tmp, "Fluid_x_32dc3d21")
            self.assertTrue(any("别的模组的变量" in p for p in problems), problems)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
