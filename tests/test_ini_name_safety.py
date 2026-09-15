"""INI 段名安全化（common/ini_name_safety.py）单测。

回归对象（2026-09-13 实测）：用户叶瞬光工程里两个部件叫 ``头饰[丝带]`` 与
``头饰[丝带1]``。别名被直接拼进段名后：

    [TextureOverride_VB_ae840e72_头饰[丝带]_Position]
    [TextureOverride_VB_ae840e72_头饰[丝带]_Texcoord]     ← 解析后与上一行同名

3DMigoto 在**第一个** ``]`` 处结束段名，同族三条段（Position/Texcoord/Blend）
于是塌成同一个名字：引擎报 ``Duplicate section found`` 并忽略后面的，
那两个部件的 VB 绑定和 draw 全部失效。实测导出文件里正好 4 条这类警告
（2 个部件 × 2 条重复）。

本文件锁定：清洗后段名里不允许再出现定界符，且两个原本会撞名的部件仍然互不
相同（不能靠"删掉不同部分"来消重名）。
"""

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PATH = REPO_ROOT / "common" / "ini_name_safety.py"
_SPEC = importlib.util.spec_from_file_location("ini_name_safety_under_test", _PATH)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

sanitize_section_name_part = _mod.sanitize_section_name_part
section_name_fragments_are_safe = _mod.section_name_fragments_are_safe


class SanitizeSectionNamePartTests(unittest.TestCase):
    def test_brackets_are_replaced(self):
        self.assertEqual(sanitize_section_name_part("头饰[丝带]"), "头饰_丝带_")
        self.assertEqual(sanitize_section_name_part("头饰[丝带1]"), "头饰_丝带1_")

    def test_two_colliding_names_stay_distinct_and_safe(self):
        a = sanitize_section_name_part("头饰[丝带]")
        b = sanitize_section_name_part("头饰[丝带1]")
        self.assertNotEqual(a, b)
        self.assertTrue(section_name_fragments_are_safe(a, b))
        # 关键：清洗结果里不能再有定界符，否则段名仍会在第一个 `]` 处被截断。
        self.assertNotIn("[", a + b)
        self.assertNotIn("]", a + b)

    def test_real_section_headers_are_single_bracket(self):
        """用真实命名规则拼段名，断言每个段头只有一个 `[...]` 对。"""
        draw_ib = "ae840e72"
        for alias in ("头饰[丝带]", "头饰[丝带1]", "正常名字", ""):
            for category in ("Position", "Texcoord", "Blend"):
                header = (
                    "[TextureOverride_VB_"
                    + draw_ib
                    + "_"
                    + sanitize_section_name_part(alias, draw_ib)
                    + "_"
                    + category
                    + "]"
                )
                self.assertTrue(header.startswith("["))
                self.assertTrue(header.endswith("]"))
                self.assertEqual(header.count("["), 1, header)
                self.assertEqual(header.count("]"), 1, header)

    def test_chinese_digits_and_common_symbols_are_preserved(self):
        kept = "刘海+后半段头发.LOD0-2286_9846"
        self.assertEqual(sanitize_section_name_part(kept), kept)

    def test_empty_and_none_fall_back(self):
        self.assertEqual(sanitize_section_name_part("", "fb"), "fb")
        self.assertEqual(sanitize_section_name_part(None, "fb"), "fb")
        self.assertEqual(sanitize_section_name_part("   ", "fb"), "fb")
        # 两个定界符各换一个下划线（保留"原本有两个字符"的信息，不合并）。
        self.assertEqual(sanitize_section_name_part("[]"), "__")

    def test_control_chars_and_semicolon_are_replaced(self):
        self.assertEqual(sanitize_section_name_part("a\nb"), "a_b")
        self.assertEqual(sanitize_section_name_part("a\tb"), "a_b")
        self.assertEqual(sanitize_section_name_part("a;b"), "a_b")

    def test_fragments_are_safe_predicate(self):
        self.assertTrue(section_name_fragments_are_safe("正常", None, ""))
        self.assertFalse(section_name_fragments_are_safe("坏[名字"))
        self.assertFalse(section_name_fragments_are_safe("坏;名字"))
        self.assertFalse(section_name_fragments_are_safe("坏\n名字"))


if __name__ == "__main__":
    unittest.main()
