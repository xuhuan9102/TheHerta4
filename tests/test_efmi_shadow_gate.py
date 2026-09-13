# -*- coding: utf-8 -*-
"""EFMI 阴影 pass 单投射源门控的回归测试（实机定位 2026-09-13）。

被测对象：ui/universal/efmi_shadow_gate.py（纯判定，无 bpy 依赖）。
断言：
  1. LOD 序号 / 裸名解析；
  2. **逻辑部件键命中 LOD1 集合时**：LOD0 入口需要门控、LOD1 入口不需要；
  3. **键不在 LOD1 集合时**：不产生门控（修复前行为，无回归）；
  4. **配对键不能用名字**：实机身体的 LOD0/LOD1 是两个不同 IB，裸名不同
     （曾因按裸名配对导致门控永不触发 —— 回归守卫）；
  5. 门控开启行内容与 vs 过滤号（与 ShaderOverridevs1000 一致）。
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "ui", "universal", "efmi_shadow_gate.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("efmi_shadow_gate_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load_module()

# 实机模组（艾尔黛拉(1)）的部件命名：身体 LOD0 / LOD1 是两个**不同 IB** 的 component
BODY_LOD0 = "LOD0.d6128f13-14664-0"
BODY_LOD1 = "LOD1.2dca919f-7596-0"
HAIR_LOD0 = "LOD0.650a6c6b-53472-0"
HAIR_LOD1 = "LOD1.6bf5c79b-51789-0"

# 逻辑部件键 = 该部件各 drawcall 的源物体名集合（同一物体导出的各 LOD 共用同一个键）
BODY_KEY = frozenset({"skin_body"})
HAIR_KEY = frozenset({"hair"})


class BareKeyAndLodIndexTests(unittest.TestCase):
    def test_bare_key_strips_lod_prefix(self):
        self.assertEqual(GATE.efmi_bare_part_key(BODY_LOD0), "d6128f13-14664-0")
        self.assertEqual(GATE.efmi_bare_part_key("LOD2.abc-1-0"), "abc-1-0")
        self.assertEqual(GATE.efmi_bare_part_key("plain-1-0"), "plain-1-0")
        self.assertEqual(GATE.efmi_bare_part_key(None), "")

    def test_lod_index(self):
        self.assertEqual(GATE.efmi_lod_index(BODY_LOD0), 0)
        self.assertEqual(GATE.efmi_lod_index(BODY_LOD1), 1)
        self.assertIsNone(GATE.efmi_lod_index("plain-1-0"))
        self.assertIsNone(GATE.efmi_lod_index(""))


class ShadowGateDecisionTests(unittest.TestCase):
    def test_lod0_gated_when_keep_lod_sibling_key_present(self):
        """实机修复场景：身体 LOD0/LOD1 是两个独立 component → LOD0 必须被门控。"""
        self.assertTrue(
            GATE.efmi_shadow_gate_needed(0, BODY_KEY, {BODY_KEY, HAIR_KEY})
        )

    def test_lod1_itself_never_gated(self):
        """留 LOD1 作为唯一投射源：LOD1 入口不门控（否则阴影图里没有该部件）。"""
        self.assertFalse(
            GATE.efmi_shadow_gate_needed(1, BODY_KEY, {BODY_KEY, HAIR_KEY})
        )

    def test_no_gate_without_keep_lod_sibling(self):
        """没有 LOD1 兄弟的部件行为不变（无回归）。"""
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, HAIR_KEY, {BODY_KEY}))
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, BODY_KEY, set()))
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, BODY_KEY, None))

    def test_none_key_never_gated(self):
        """拿不到源物体键（无 drawcall 物体名）时不门控，宁可少改。"""
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, None, {BODY_KEY}))

    def test_pairing_key_must_not_be_the_part_name(self):
        """回归守卫：实机身体的 LOD0/LOD1 裸名不同（两个不同 IB）。

        若用名字配对，门控永不触发（修复失效）。此处钉住该事实，
        并要求判定只依赖显式传入的逻辑部件键。
        """
        self.assertNotEqual(
            GATE.efmi_bare_part_key(BODY_LOD0), GATE.efmi_bare_part_key(BODY_LOD1)
        )
        # 相同裸名的假设输入不会让不同键互相命中
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, BODY_KEY, {HAIR_KEY}))

    def test_gate_open_lines(self):
        lines = GATE.efmi_shadow_gate_open_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("shadow-gate", lines[0])
        self.assertEqual(lines[1], "if vs != 200")
        self.assertEqual(GATE.EFMI_SHADOW_PASS_VS_FILTER_INDEX, 200)
        self.assertEqual(GATE.EFMI_SHADOW_KEEP_LOD, 1)
        self.assertEqual(GATE.EFMI_SHADOW_GATE_LOD, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
