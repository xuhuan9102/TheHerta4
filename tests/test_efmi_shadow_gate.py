# -*- coding: utf-8 -*-
"""EFMI 深度/阴影 pass 门控（rt_width 口径、全 IB 挂载）的回归测试。

机制（2026-09-15 实机验证后定型；历史与三次实机事故见 efmi_shadow_gate 模块文档）：
- 深度/阴影 pass 不绑颜色 RT（抓帧里 OMSetRenderTargets(NumViews:0)），可见 pass 都有
  → command list 内建变量 rt_width 在无 RT pass 读为 0；
- 门控行 ``if rt_width != 0``：仅存在颜色输出目标的 pass 才回画模组网格；
- 所有 EntryPoint 一律挂载，不再按 LOD0/LOD1 配对挑选，也不再注册任何
  ShaderOverride/filter_index 哈希标签——零哈希、与角色/画质档无关。

被测对象：
- ui/universal/efmi_shadow_gate.py（纯行构造，无 bpy 依赖）；
- ui/universal/efmi.py 的发射侧（源码级回归：无条件发射、无哈希注册、无抓帧推导）。

断言要点：
  1. 门控行 = ``if rt_width != 0``，且门控输出里不得再出现 ps 标签 / 99001 /
     filter_index（实机事故守卫：v4.4.45 借用 RabbitFX 1718.2 致可见外壳消失、
     044b75548e7c9fd7 引擎级共享着色器被误注册、哈希集按角色漂移）；
  2. 发射侧对合并骨架 EntryPoint 无条件发射门控（全 IB 挂载），不再有
     efmi_shadow_gate_needed / efmi_shadow_part_keys 挑选逻辑；
  3. 不再有 ShadowOverride 注册段与抓帧推导链（efmi_derive_shadow_ps_hashes /
     _efmi_shadow_pass_ps_hashes / _efmi_shadow_log_paths）。
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "ui", "universal", "efmi_shadow_gate.py")
EFMI_PY_PATH = os.path.join(REPO_ROOT, "ui", "universal", "efmi.py")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load_module("efmi_shadow_gate_under_test", MODULE_PATH)


class GateOpenLinesTests(unittest.TestCase):
    """门控行内容：rt_width 口径，配套 endif 由发射侧追加。"""

    def test_condition_is_rt_width(self):
        lines = GATE.efmi_shadow_gate_open_lines()
        self.assertEqual(GATE.EFMI_SHADOW_GATE_CONDITION, "rt_width != 0")
        self.assertIn("if rt_width != 0", lines)
        self.assertEqual(lines[-1], "if " + GATE.EFMI_SHADOW_GATE_CONDITION)

    def test_comment_documents_rt_width_semantics(self):
        lines = GATE.efmi_shadow_gate_open_lines()
        self.assertIn("shadow-gate", lines[0])
        self.assertIn("rt_width", lines[0])

    def test_no_hash_tagging_in_emitted_lines(self):
        """门控输出不得再出现 ps 标签 / 99001 / filter_index（三次实机事故守卫）。"""
        text = "\n".join(GATE.efmi_shadow_gate_open_lines())
        self.assertNotIn("99001", text)
        self.assertNotIn("filter_index", text)
        self.assertNotIn("if ps", text)
        self.assertNotIn("ps !=", text)


class ModuleHygieneTests(unittest.TestCase):
    """哈希时代的机制必须彻底移除（防回退；文档注释不引用这些符号名）。"""

    @classmethod
    def setUpClass(cls):
        with open(MODULE_PATH, "r", encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_removed_symbols_gone(self):
        for removed in (
            "EFMI_SHADOW_PS_FILTER_INDEX",
            "EFMI_SHADOW_PASS_PS_HASH",
            "EFMI_SHADOW_FALLBACK_PS_HASHES",
            "EFMI_SHADOW_KEEP_LOD",
            "EFMI_SHADOW_GATE_LOD",
            "EFMI_SHADOW_SOURCE_",
            "efmi_shadow_gate_needed",
            "efmi_shadow_part_keys",
            "efmi_part_key",
            "efmi_bare_part_key",
            "efmi_lod_index",
            "efmi_normalize_obj_name",
            "efmi_scan_shadow_ps_hashes",
            "efmi_derive_shadow_ps_hashes",
            "efmi_shadow_override_lines",
            "efmi_shadow_override_section_name",
            "efmi_shadow_filter_index_text",
        ):
            self.assertNotIn(removed, self.source, removed)

    def test_no_regex_or_os_dependencies(self):
        """纯行构造模块：不再需要日志扫描用的 re/os/hashlib。"""
        self.assertNotIn("import re", self.source)
        self.assertNotIn("import os", self.source)
        self.assertNotIn("import hashlib", self.source)


class EmitSideSourceTests(unittest.TestCase):
    """efmi.py 发射侧的源码级回归。"""

    @classmethod
    def setUpClass(cls):
        with open(EFMI_PY_PATH, "r", encoding="utf-8") as handle:
            cls.code = handle.read()

    def test_gate_emitted_for_every_entry_point(self):
        """全 IB 挂载：不再有按 LOD 配对/挑选的判定调用，门控无条件发射。"""
        self.assertIn("efmi_shadow_gate_open_lines()", self.code)
        self.assertNotIn("efmi_shadow_gate_needed(", self.code)
        self.assertNotIn("efmi_shadow_part_keys(", self.code)
        self.assertNotIn("efmi_lod_index(", self.code)
        # 门控必须仍以 endif 关闭
        self.assertIn('texture_override_ib_section.append("endif")', self.code)

    def test_no_shaderoverride_hash_registration(self):
        """不再注册**阴影 PS** 的 ShaderOverride 段。

        注意：``M_SectionType.ShaderOverride`` 段类型本身不禁——efmi_pass_mirror
        （多 pass 贴图镜像）会按推导注册 G-buffer/第二层角色标签（99002/99003），
        与阴影门控无关，属合法存在。本测试只锁阴影专属符号。
        """
        self.assertNotIn("shadow_override_section", self.code)
        self.assertNotIn("efmi_shadow_override_lines(", self.code)
        self.assertNotIn("efmi_shadow_override_section_name(", self.code)
        self.assertNotIn("99001", self.code)
        # 阴影标签绝不得以别名复活（PassMirror 的 99002/99003 是另一套）
        self.assertNotIn("ShadowPS", self.code)

    def test_no_frame_analysis_derivation(self):
        """抓帧推导整条链移除（含模块方法与局部变量）。"""
        self.assertNotIn("_efmi_shadow_pass_ps_hashes", self.code)
        self.assertNotIn("_efmi_shadow_log_paths", self.code)
        self.assertNotIn("efmi_derive_shadow_ps_hashes(", self.code)
        self.assertNotIn("shadow_ps_hashes", self.code)

    def test_gate_emission_order_in_merged_branch(self):
        """门控行在 component_id 之前、endif 在其后（与实机验证过的 ini 形态一致）。"""
        gate_pos = self.code.index("efmi_shadow_gate_open_lines()")
        component_pos = self.code.index("component_id = {merged_component_id}")
        endif_pos = self.code.index('texture_override_ib_section.append("endif")')
        self.assertLess(gate_pos, component_pos)
        self.assertLess(component_pos, endif_pos)


if __name__ == "__main__":
    unittest.main()
