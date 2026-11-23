"""骨骼池换算 palette 的发射顺序回归（宿主 conv 必须在消费它的段里补算）。

根因（2026-10-13 实机）：整份骨骼池换算到「宿主部件空间」的 CS 只在宿主自己的
deform 段里发 run，但消费它的合并几何**重放**会出现在组内每个部件的 deform 段里
⇒ 只要宿主不是本帧最后一个提交的部件，落笔用的就是「池只更新了一半」的旧快照
（294/310 根骨落后一帧：运动时抖、静止时看不出）。修法 = 每个槽发完该槽的全部
attach 之后，把本段可能消费的换算补算一遍（_merged_deform_conv_component_ids）。

本文件固化的就是那条不变量：同一个 [TextureOverride_VB_*] 段内，每个
vs-t0 = ResourceZZPaletteConv_X_s<k> 之前必须已经出现过
run = CustomShaderZZMSPaletteConv_X_s<k>；且换算 run 必须与 attach run 同级
（不在任何 if/endif 之间）——本 fork 里 if 内的 run 不执行 ⇒ 骨架为空 ⇒ 模型消失。
"""

import re
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

def _import_ini_test_module():
    """取 ini 测试模块的**同一份**对象。

    pytest 按 prepend 模式把它导入成顶层 `test_zzmi_merged_skeleton_ini`；若这里写
    `from tests import ...` 就会**执行第二遍**（不同模块名 ⇒ 另一套 `{PKG}.*` 假包与
    真实模块），而那个文件里按 `sys.modules[...]` 打的补丁便落到第二份实例上——
    表现就是那些用例单跑全绿、与全量一起跑变红。
    """
    existing = sys.modules.get("test_zzmi_merged_skeleton_ini")
    if existing is not None:
        return existing
    try:
        # pytest 的 prepend 模式已把 tests/ 放进 sys.path，顶层名就是收集时的模块名。
        import test_zzmi_merged_skeleton_ini as module
        return module
    except ModuleNotFoundError:
        from tests import test_zzmi_merged_skeleton_ini as module
        return module


base = _import_ini_test_module()

PKG = base.PKG
ZZMI_MODULE = base._zzmi_module

# zzmi.py 在函数内按相对导入取 ...common.zzmi_cross_group_palette（真实实现、
# 只依赖 numpy）。不登记它 ⇒ 换算部件表为空，
# 骨骼池路线在测试里彻底失活。
_PALETTE = base._load_real_module(
    f"{PKG}.common.zzmi_cross_group_palette", "common/zzmi_cross_group_palette.py"
)

_CONV_RUN = re.compile(r"^run = CustomShaderZZMSPaletteConv_C(\d+)_s(\d+)$")
_CONV_BIND = re.compile(r"^vs-t0 = ResourceZZPaletteConv_C(\d+)_s(\d+)$")
_VB_SECTION = re.compile(r"^\[TextureOverride_VB_.+\]$")


def audit_palette_conv_order(text):
    """逐段核对「换算 run 先于它对应的 palette 绑定」。

    返回 (problems, stats, evidence)：problems 为空即不变量成立；evidence 是
    每条形如 "<行号>: <行文本>  [<段名>]" 的定位记录（报告用）。
    """
    problems = []
    evidence = []
    stats = {"sections": 0, "conv_runs": 0, "conv_bindings": 0, "runs_in_if": 0}
    conv_sections = set()
    section = ""
    seen = set()
    depth = 0
    for number, raw in enumerate(str(text).splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            seen = set()
            depth = 0
            if _VB_SECTION.match(section):
                stats["sections"] += 1
            continue
        if not _VB_SECTION.match(section):
            continue
        if stripped == "endif":
            depth = max(0, depth - 1)
            continue
        run = _CONV_RUN.match(stripped)
        bind = None if run else _CONV_BIND.match(stripped)
        if run:
            stats["conv_runs"] += 1
            conv_sections.add(section)
            evidence.append(f"{number}: {stripped}  [{section}]")
            key = (int(run.group(1)), int(run.group(2)))
            if depth > 0:
                stats["runs_in_if"] += 1
                problems.append(
                    f"{section}:{number} 换算 run 落在 if 里（if 内的 run 不执行）：{stripped}"
                )
            if raw[:1].isspace():
                problems.append(
                    f"{section}:{number} 换算 run 有缩进，必须与 attach run 同级：{raw!r}"
                )
            seen.add(key)
        elif bind:
            stats["conv_bindings"] += 1
            conv_sections.add(section)
            evidence.append(f"{number}: {stripped}  [{section}]")
            key = (int(bind.group(1)), int(bind.group(2)))
            if key not in seen:
                problems.append(
                    f"{section}:{number} 绑定了 ResourceZZPaletteConv_C{key[0]}_s{key[1]}，"
                    f"但本段此前没有 run = CustomShaderZZMSPaletteConv_C{key[0]}_s{key[1]}"
                )
        if stripped.startswith("if ") or stripped == "if":
            depth += 1
    stats["conv_sections"] = len(conv_sections)
    return problems, stats, evidence


def _palette_profile():
    """与直连路径夹具同构的骨骼池方案：C0 = 吸收宿主（根），C1/C2 挂在 C0 上。"""
    layout = (("a23aa8a3", 0, 105), ("b20f90ea", 105, 51), ("b30db54e", 156, 14))
    components = []
    for component_id, (ib_hash, vg_offset, vg_count) in enumerate(layout):
        components.append(
            {
                "component_id": component_id,
                "ib_hash": ib_hash,
                "role": _PALETTE.ROLE_MERGED,
                "vg_offset": vg_offset,
                "vg_count": vg_count,
                "vg_map": {str(local): vg_offset + local for local in range(vg_count)},
                "parent_component_id": 0,
                "anchor_parent_slot": 0,
                "anchor_self_slot": vg_offset,
            }
        )
    return {
        "mode": _PALETTE.PROFILE_MODE,
        "format_version": _PALETTE.PROFILE_FORMAT_VERSION,
        "components": components,
    }


def _enable_palette_route(case, profile=None):
    """在 `case` 上激活骨骼池路线（方案数据 + 开关 + 清换算缓存）。"""
    profile = _PALETTE.validate_profile(
        _palette_profile() if profile is None else profile
    )
    module_name = f"{PKG}.common.zzmi_skeleton"
    stub = types.ModuleType(module_name)
    stub.ZZMISkeletonMergeHelper = type(
        "_StubMergeHelper", (), {"cross_group_profile": profile}
    )
    # 同名模块可能已被 _real_modules 登记成真实实现（跨组路线要真跑）：只许临时顶替，
    # 清理时原样放回，否则会把登记表里的模块 pop 掉，同进程后续用例再也导入不到。
    previous = sys.modules.get(module_name)
    case.addCleanup(
        lambda: sys.modules.__setitem__(module_name, previous)
        if previous is not None
        else sys.modules.pop(module_name, None)
    )
    sys.modules[module_name] = stub
    patcher = mock.patch.object(
        ZZMI_MODULE,
        "_zzmi_prop_flag",
        lambda name, default: True
        if name == "cross_group_merged_vgmap_test"
        else bool(default),
    )
    patcher.start()
    case.addCleanup(patcher.stop)
    case.addCleanup(ZZMI_MODULE.__dict__.pop, "_MERGED_CONV_INDICES_CACHE", None)
    ZZMI_MODULE.__dict__.pop("_MERGED_CONV_INDICES_CACHE", None)
    return profile


class ZZMIPaletteConvOrderTests(unittest.TestCase):
    """骨骼池路线（cross_group_merged_vgmap_test + 方案数据）下的 deform 段时序。

    夹具直接借用直连路径回归的那一套（同一个宿主 + 两个兄弟部件），但**不继承**
    它：那一类的用例断言的是历史路线的骨架缓冲名，池路线下它们必然不同。
    """

    _FIXTURE = base.ZZSIMergedHostDirectPathTests
    _register_obj = _FIXTURE._register_obj
    _components = _FIXTURE._components
    _make_exporter = _FIXTURE._make_exporter
    _vb_text = _FIXTURE._vb_text

    def setUp(self):
        self._FIXTURE.setUp(self)
        self._enable_palette_route()

    def _enable_palette_route(self):
        return _enable_palette_route(self)

    def _section_texts(self):
        exporter, models = self._make_exporter()
        return exporter, {
            model.draw_ib: self._vb_text(exporter, model) for model in models
        }

    def test_fixture_runs_the_palette_route(self):
        """前置断言：骨骼池路线已激活，且本段换算集合 = 自身 ∪ 重放宿主。"""
        exporter, _models = self._make_exporter()
        self.assertTrue(exporter._merged_palette_enabled())
        self.assertEqual(
            ZZMI_MODULE._merged_palette_conv_indices_by_ib(),
            {"a23aa8a3": 0, "b20f90ea": 1, "b30db54e": 2},
        )
        absorbed_hosts = exporter._merged_group_absorbed_hosts(0)
        # 宿主段：只有自己（几何被吸收，重放的就是自己那份）
        self.assertEqual(
            exporter._merged_deform_conv_component_ids(
                "a23aa8a3", 0, 0, absorbed_hosts=absorbed_hosts
            ),
            [0],
        )
        # 兄弟段：自己 + 它要重放的宿主
        self.assertEqual(
            exporter._merged_deform_conv_component_ids(
                "b20f90ea", 1, 0, absorbed_hosts=absorbed_hosts
            ),
            [1, 0],
        )

    def test_every_conv_binding_has_prior_conv_run(self):
        """核心不变量：三段里每个 Conv palette 绑定之前都有对应的换算 run。"""
        _exporter, texts = self._section_texts()
        problems, stats, _evidence = audit_palette_conv_order("\n".join(texts.values()))
        self.assertEqual(problems, [])
        self.assertEqual(stats["sections"], 9)
        # 三个 deform 段（Position 通道）带换算；Texcoord/Blend 段没有 deform 块
        self.assertEqual(stats["conv_sections"], 3)
        self.assertEqual(stats["runs_in_if"], 0)
        # 宿主段每槽 1 条、两个兄弟段每槽 2 条（自己 + 宿主）
        self.assertEqual(stats["conv_runs"], 10)
        self.assertEqual(stats["conv_bindings"], 13)

    def test_sibling_sections_recompute_host_conv(self):
        """回归点：兄弟段必须补算宿主 conv（修复前只有它自己的那条）。"""
        _exporter, texts = self._section_texts()
        sibling = texts["b20f90ea"]
        self.assertIn("run = CustomShaderZZMSPaletteConv_C1_s1", sibling)
        self.assertIn("run = CustomShaderZZMSPaletteConv_C0_s1", sibling)
        self.assertIn("run = CustomShaderZZMSPaletteConv_C0_s2", sibling)
        # 宿主 conv 的 run 排在该槽全部 attach 之后、该槽第一次绑定之前
        attach_last = sibling.index("run = CustomShaderZZMIMergedSkeletonAttach_C2_s1")
        host_conv = sibling.index("run = CustomShaderZZMSPaletteConv_C0_s1")
        host_bind = sibling.index("vs-t0 = ResourceZZPaletteConv_C0_s1")
        self.assertLess(attach_last, host_conv)
        self.assertLess(host_conv, host_bind)
        # 宿主自己的段不重复发同一个换算
        host = texts["a23aa8a3"]
        self.assertEqual(host.count("run = CustomShaderZZMSPaletteConv_C0_s1"), 1)
        self.assertNotIn("run = CustomShaderZZMSPaletteConv_C1_s1", host)

    def test_conv_runs_are_top_level_next_to_attach_runs(self):
        """换算 run 与 attach run 同级同层：不在任何 if/endif 之间。"""
        _exporter, texts = self._section_texts()
        for draw_ib, text in texts.items():
            depth = 0
            for index, raw in enumerate(text.splitlines()):
                stripped = raw.strip()
                if stripped.startswith("if ") or stripped == "if":
                    depth += 1
                    continue
                if stripped == "endif":
                    depth = max(0, depth - 1)
                    continue
                if _CONV_RUN.match(stripped):
                    self.assertEqual(
                        depth,
                        0,
                        f"{draw_ib} 段第 {index + 1} 行的换算 run 落在 if 内：{raw!r}",
                    )
                    self.assertFalse(raw[:1].isspace(), f"换算 run 有缩进：{raw!r}")

class ZZMIPalettePartialProfileTests(unittest.TestCase):
    def _generate(self, profile, draw_ibs):
        _enable_palette_route(self, profile)
        models = []
        for component in _palette_profile()["components"]:
            draw_ib = component["ib_hash"]
            if draw_ib in draw_ibs:
                submesh = base._FakeSubmesh(
                    f"LOD0.{draw_ib}-3-0",
                    component["vg_offset"],
                    component["vg_count"],
                    exported_vertex_count=3,
                )
                models.append(base._FakeDrawIBModel(draw_ib, [submesh]))
        exporter = base._make_exporter(models)
        (
            exporter.merged_skeleton_components,
            exporter.merged_skeleton_component_id_dict,
        ) = exporter._collect_merged_skeleton_components()
        skeleton = base._FakeIniBuilder()
        with mock.patch.object(exporter, "_atomic_write_binary") as binary_writer:
            exporter.add_merged_skeleton_sections(skeleton)
        buffers = {
            Path(path).name: payload
            for (path, payload), _kwargs in binary_writer.call_args_list
        }

        texts = {}
        for model in models:
            vb = base._FakeIniBuilder()
            exporter.add_unity_vs_texture_override_vb_sections(vb, model)
            texts[model.draw_ib] = "\n".join(base._all_builder_lines(vb))
        return texts, "\n".join(base._all_builder_lines(skeleton)), buffers

    def test_part_outside_conversion_profile_reads_its_global_slots(self):
        profile = _palette_profile()
        profile["components"] = profile["components"][:1]
        profile["pool_bones"] = 170
        texts, skeleton, buffers = self._generate(profile, ("a23aa8a3", "b30db54e"))
        self.assertEqual(
            struct.unpack("<4I", buffers["zz_cgparams_C0.buf"]), (0, 1, 170, 0)
        )
        for slot in (1, 2):
            draw_block = (
                f"if $zz_ms_occ_1 == {slot}\n"
                f"    vs-t0 = ResourceZZMergedSkeleton_G0_s{slot}\n"
                "    draw = 3, 0"
            )
            self.assertIn(draw_block, texts["b30db54e"])
            self.assertIn(
                f"cs-t0 = ref ResourceZZPalette_b30db54e_s{slot}", skeleton
            )
            self.assertNotIn(f"ResourceZZPaletteConv_C1_s{slot}", skeleton)
            conv = skeleton.split(f"[ResourceZZPaletteConv_C0_s{slot}]", 1)[1]
            self.assertIn("array = 510", conv.split("\n[", 1)[0])

    def test_subset_matches_conversion_by_draw_ib(self):
        profile = _palette_profile()
        profile["components"] = profile["components"][:2]
        texts, skeleton, buffers = self._generate(profile, ("b20f90ea", "b30db54e"))
        self.assertEqual(
            struct.unpack("<4I", buffers["zz_cgparams_C1.buf"]), (1, 2, 170, 0)
        )
        for slot in (1, 2):
            self.assertIn(
                f"    vs-t0 = ResourceZZPaletteConv_C0_s{slot}", texts["b20f90ea"]
            )
            self.assertIn(
                f"    vs-t0 = ResourceZZMergedSkeleton_G0_s{slot}", texts["b30db54e"]
            )
            self.assertNotIn(f"CustomShaderZZMSPaletteConv_C1_s{slot}", texts["b30db54e"])
            conv = skeleton.split(f"[CustomShaderZZMSPaletteConv_C0_s{slot}]", 1)[1]
            self.assertIn("cs-t2 = ref ResourceZZCgParams_C1", conv.split("\n[", 1)[0])
            pool = skeleton.split(f"[ResourceZZMergedSkeleton_G0_s{slot}]", 1)[1]
            self.assertIn("array = 510", pool.split("\n[", 1)[0])


# 整条 VB 的重放块：绑换算 palette → 引 SO → 拿宿主（carrier）的原生 vb0/vb2 → 画全部行。
_REPLAY_BLOCK = re.compile(
    r"    vs-t0 = ResourceZZPaletteConv_C(\d+)_s(\d+)\n"
    r"    so0 = ref ResourceZZRedirectSO_G3_s\2\n"
    r"    vb2 = Resourceb20f90eaBlend\n"
    r"    vb0 = Resourceb20f90eaPosition\n"
    r"    draw = 18776, 0\n"
    r"    so0 = null"
)


class ZZMIPaletteConvSoOwnerSpaceTests(base._ZZMIGroup3RedirectFixture, unittest.TestCase):
    """target 只有占位几何（真几何在 carrier）时，重放/皮肤必须落在 so_owner 的空间。

    实机根因（用户确认，整份产物让角色插进地里）：组 3 的 target a23aa8a3 只有 3 行
    占位几何，真正的合并几何在 carrier b20f90ea（= so_owner）上，渲染用的是 so_owner
    的对象矩阵；但重放 draw 的 vs-t0 与皮肤 CS 的 cs-t2 都按 target 取了 Conv_C0。
    C0 在合并网格里零顶点 ⇒ 每槽输出的是同一份常量仿射 ⇒ 全部顶点被同一个矩阵左乘
    = 整体刚性下沉。本夹具构造的正是这个配置（默认 _group3_exporter()：target 3 行
    占位），断言生成物绑定的是 so_owner 的换算（C1），且每个 deform 段都先补算了它。
    """

    _REPLAY_ROWS = 18776

    def setUp(self):
        base._ZZMIGroup3RedirectFixture.setUp(self)
        _enable_palette_route(self)

    def _exporter(self, real_target=False):
        if real_target:
            exporter, models = self._group3_exporter(
                target_real_vertices=2000, target_registered=True
            )
        else:
            exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        return exporter, models

    def _all_sections_text(self, exporter, models):
        """跑齐 VB 段与骨架段（与 ZZMISkinPublishBlendLayoutGuardTests 同口径）。"""
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next((m for m in models if m.draw_ib == carrier_ib), None)
            if carrier_model is None:
                continue
            stride = int(
                (
                    getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {}
                ).get("Texcoord", 0)
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get(
                "Texcoord"
            ):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )
        vb_builder = base._FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        skeleton_builder = base._FakeIniBuilder()
        exporter.add_merged_skeleton_sections(skeleton_builder)
        return (
            "\n".join(base._all_builder_lines(vb_builder)),
            "\n".join(base._all_builder_lines(skeleton_builder)),
        )

    def _generate(self, real_target=False):
        exporter, models = self._exporter(real_target=real_target)
        vb_text, skeleton_text = self._all_sections_text(exporter, models)
        return exporter, vb_text, skeleton_text

    def _conv_runs_and_binds(self, text, draw_ib):
        """某部件 Position 段里「换算 run / 绑定」按出现顺序的 (行号, 类型, 名称)。"""
        section = ""
        table = []
        for number, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped
                continue
            if f"VB_{draw_ib}_{draw_ib}_Position" not in section:
                continue
            run = _CONV_RUN.match(stripped)
            bind = None if run else _CONV_BIND.match(stripped)
            if run:
                table.append((number, "run", (int(run.group(1)), int(run.group(2)))))
            elif bind:
                table.append((number, "bind", (int(bind.group(1)), int(bind.group(2)))))
        return table

    @staticmethod
    def _replay_binds(text):
        """守卫体内的整条 VB 重放绑定：缩进的 vs-t0 + 紧跟 so0 = ref ResourceZZRedirectSO_。

        返回 [(组件号, 槽位)]（字符串，顺序按出现位置）。只认守卫体内的那 6 处：
        段首那行不带缩进、且后面跟的是 \"handling = skip\"。
        """
        lines = text.splitlines()
        binds = []
        for index, raw in enumerate(lines):
            if not raw.startswith("    vs-t0 = "):
                continue
            match = _CONV_BIND.match(raw.strip())
            if not match:
                continue
            window = [line.strip() for line in lines[index + 1:index + 5]]
            if any(line.startswith("so0 = ref ResourceZZRedirectSO_") for line in window):
                binds.append((match.group(1), match.group(2)))
        return binds

    def test_fixture_really_puts_the_geometry_on_the_carrier(self):
        """前置断言：target 只有占位几何、so_owner 是 carrier ⇒ 本组用例才有意义。"""
        exporter, _models = self._exporter()
        plan = exporter._redirect_target_map["a23aa8a3"]
        self.assertFalse(plan["target_has_real_geometry"])
        self.assertEqual(plan["so_owner_ib"], "b20f90ea")
        self.assertEqual(
            exporter.merged_skeleton_component_id_dict,
            {"a23aa8a3": 0, "b20f90ea": 1, "b30db54e": 2},
        )
        self.assertEqual(
            exporter._merged_so_space_palette_ib(plan, "a23aa8a3"), "b20f90ea"
        )
        self.assertEqual(
            exporter._merged_deform_palette_name("b20f90ea", 3, 1),
            "ResourceZZPaletteConv_C1_s1",
        )

    def test_every_full_vb_replay_binds_the_so_owner_palette(self):
        """6 处整条 VB 重放（3 个部件 × 2 槽）全部绑 so_owner（C1）的换算。"""
        _exporter, vb_text, _skeleton = self._generate()
        self.assertEqual(
            self._replay_binds(vb_text),
            [("1", "1"), ("1", "2")] * 3,
            "三段 deform 的整条 VB 重放必须全部绑 ResourceZZPaletteConv_C1_s<k>",
        )
        # 这 6 处重放画的确实是 carrier 的全部行（不是 target 的 3 行占位几何）。
        self.assertEqual(vb_text.count(f"    draw = {self._REPLAY_ROWS}, 0"), 6)
        self.assertEqual(_REPLAY_BLOCK.findall(vb_text), [("1", "1"), ("1", "2")] * 3)
        # 反向断言：没有任何一处重放还绑着 target（C0）的换算。
        self.assertNotIn(("0", "1"), self._replay_binds(vb_text))
        self.assertNotIn(("0", "2"), self._replay_binds(vb_text))

    def test_redirect_skin_uses_full_output_in_the_carrier_space(self):
        exporter, _vb_text, skeleton = self._generate()
        carrier = next(m for m in exporter.drawib_model_list if m.draw_ib == "b20f90ea")
        builder = base._FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, carrier)
        render = "\n".join(base._all_builder_lines(builder))
        for slot in (1, 2):
            with self.subTest(slot=slot):
                out = f"ResourceZZSkinOut_G3_s{slot}"
                definition = skeleton.split(f"[{out}]", 1)[1].split("\n[", 1)[0]
                self.assertIn("array = 18779", definition)
                skin = skeleton.split(f"[CustomShaderZZMISkin_G3_s{slot}]", 1)[1]
                skin = skin.split("\n[", 1)[0]
                self.assertIn("cs-t0 = ref ResourceZZRedirectSkinPosition_G3_c0", skin)
                self.assertIn("cs-t1 = ref ResourceZZRedirectSkinBlend_G3_c0", skin)
                self.assertIn(f"cs-t2 = ref ResourceZZRedirectPalette_G3_s{slot}", skin)
                self.assertIn(f"ResourceZZRedirectSkinWrite_G3_s{slot} = ref {out}", skin)
                self.assertIn(f"@vb0 == @ResourceZZSpaceSO_b20f90ea_s{slot}", render)
                self.assertIn(f"cs-t2 = ref ResourceZZSpaceCB1_b20f90ea_s{slot}", skeleton)
                self.assertLess(
                    render.index(f"run = CustomShaderZZRebasePalette_a23aa8a3_s{slot}"),
                    render.index(f"run = CustomShaderZZMISkin_G3_s{slot}"),
                )
                self.assertLess(
                    render.index(f"run = CustomShaderZZMISkin_G3_s{slot}"),
                    render.index(f"vb0 = ref {out}"),
                )

    def test_every_deform_section_computes_the_palette_it_binds(self):
        """每个 deform 段先跑它要绑的那份换算（含 target 自己的段）。"""
        _exporter, vb_text, _skeleton = self._generate()
        for draw_ib, component_id in (
            ("b20f90ea", 1),
            ("a23aa8a3", 0),
            ("b30db54e", 2),
        ):
            section_text = "\n".join(
                block
                for block in vb_text.split("[TextureOverride_VB_")
                if block.startswith(f"{draw_ib}_{draw_ib}_Position]")
            )
            self.assertTrue(section_text, f"{draw_ib} 缺少 Position 段")
            for slot in (1, 2):
                self.assertIn(
                    f"run = CustomShaderZZMSPaletteConv_C1_s{slot}", section_text
                )
            table = self._conv_runs_and_binds(vb_text, draw_ib)
            self.assertTrue(table, f"{draw_ib} 段没有任何换算 run/绑定")
            computed = set()
            for number, kind, name in table:
                if kind == "run":
                    computed.add(name)
                else:
                    self.assertIn(
                        name,
                        computed,
                        f"{draw_ib} 段第 {number} 行绑了 {name} 但此前没有对应的 run",
                    )
            self.assertIn(
                f"run = CustomShaderZZMSPaletteConv_C{component_id}_s1", section_text
            )

    def test_conv_candidates_follow_the_so_owner(self):
        """验收点：候选集合按 so_owner 推导，否则 C0 段不会补算 C1 的重放换算。"""
        exporter, _models = self._exporter()
        self.assertEqual(
            exporter._merged_deform_conv_component_ids(
                "a23aa8a3", 0, 3, group_target="a23aa8a3"
            ),
            [0],
        )
        self.assertEqual(
            exporter._merged_deform_conv_component_ids(
                "a23aa8a3",
                0,
                3,
                group_target="a23aa8a3",
                palette_owner_ib="b20f90ea",
            ),
            [0, 1],
        )
        self.assertEqual(
            ZZMI_MODULE._merged_palette_conv_indices_by_ib(),
            {"a23aa8a3": 0, "b20f90ea": 1, "b30db54e": 2},
        )

    def test_all_conv_bindings_are_audited_clean(self):
        """整份 VB 产物过审计：换算 run 先于绑定、且不在 if 内。"""
        _exporter, vb_text, _skeleton = self._generate()
        problems, stats, _evidence = audit_palette_conv_order(vb_text)
        self.assertEqual(problems, [])
        self.assertEqual(stats["runs_in_if"], 0)
        self.assertEqual(stats["sections"], 9)
        self.assertEqual(stats["conv_sections"], 3)

    def test_real_parts_replay_in_the_visible_hosts_space(self):
        """多个可见部件走直连重放，合并几何使用自己的空间和原生 SO。"""
        exporter, vb_text, skeleton = self._generate(real_target=True)
        self.assertEqual(exporter._redirect_carrier_map, {})
        self.assertEqual(exporter._redirect_target_map, {})
        self.assertEqual(self._replay_binds(vb_text), [("1", "1"), ("1", "2")] * 3)
        self.assertNotIn("[CustomShaderZZMISkin_", skeleton)
        problems, _stats, _evidence = audit_palette_conv_order(vb_text)
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
