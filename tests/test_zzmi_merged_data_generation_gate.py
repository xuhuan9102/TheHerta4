# -*- coding: utf-8 -*-
"""ZZMI 骨骼合并「数据生成与开关无关」契约单测（只读源码做断言）。

用户要求（生成侧 / 消费侧分离）：

1. **生成侧**：首次导入、或「清除骨骼合并VGMap缓存」后重新导入时，无论
   融合编号和自动生成 Blender 骨架是否开启，都必须把提取文件里的骨骼合并数据
   完整反查并落盘到工作空间，之后打开任何开关都不再需要提取文件；
2. **消费侧**：复选框仍决定本次导入/导出是否使用这份数据——关闭时导入
   结果必须与旧版一致（走局部顶点组、不按 SkeletonGroup 归集）。

本文件钉住 ui/ui_func_import_ssmt.py 的门控形态；判据本身（元数据缺失）
的行为由 tests/test_zzmi_skeleton.py::ZZMIMergedMetadataGateTests 覆盖。
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IMPORT_UI = REPO_ROOT / "ui" / "ui_func_import_ssmt.py"
ZZMI_SKELETON = REPO_ROOT / "common" / "zzmi_skeleton.py"
GLOBAL_PROPERTIES = REPO_ROOT / "common" / "global_properties.py"
IMPORT_PANEL = REPO_ROOT / "ui" / "ui_panel_basic.py"

EXPERIMENTAL_SWITCHES = ("zzmi_merged_redirect_enabled", "cross_group_merged_vgmap_test")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class GenerationGateTests(unittest.TestCase):
    """预生成门控：复选框关闭时仍要因「元数据缺失」而反查。"""

    def setUp(self):
        self.source = _read(IMPORT_UI)

    def test_gate_is_consumption_or_missing_metadata(self):
        match = re.search(
            r"if (?P<cond>[^\n]*zzmi_merged_metadata_missing[^\n]*):\s*\n"
            r"\s+merged_vgmap_ready = False",
            self.source,
        )
        self.assertIsNotNone(
            match, "ZZMI 预生成门控必须包含 zzmi_merged_metadata_missing"
        )
        condition = match.group("cond")
        self.assertIn(
            "zzmi_merged_consumption",
            condition,
            "门控必须是「消费请求 or 元数据缺失」，不能只看复选框",
        )
        self.assertIn("or", condition)

    def test_gate_no_longer_only_checks_the_checkbox(self):
        self.assertNotIn(
            "is_zzmi_merged",
            self.source,
            "旧的 is_zzmi_merged（= logic_name == ZZMI and import_merged_vgmap()）"
            "作为唯一门控会让复选框关闭时完全不反查，已废弃",
        )

    def test_missing_metadata_probe_targets_zzmi_workspace(self):
        match = re.search(
            r"if GlobalConfig\.logic_name == LogicName\.ZZMI:\s*\n(?P<body>(?:.*\n){1,12}?)"
            r"    if zzmi_merged_consumption",
            self.source,
        )
        self.assertIsNotNone(match, "缺少 ZZMI 元数据缺失探测段")
        body = match.group("body")
        self.assertIn("missing_merged_metadata_exist", body)
        self.assertIn("path_workspace_folder", body)
        self.assertIn('target["import_key"]', body)

    def test_failure_fallback_only_when_checkbox_requested_consumption(self):
        match = re.search(
            r"if merged_vgmap_ready is False and \((?P<cond>.*?)\):",
            self.source,
            re.S,
        )
        self.assertIsNotNone(match, "缺少预生成失败回退块")
        condition = match.group("cond")
        self.assertIn("zzmi_merged_consumption", condition)
        self.assertIn("efmi_merged_consumption", condition)
        self.assertIn("himi_merged_consumption", condition)


class ConsumptionGateTests(unittest.TestCase):
    """消费侧：复选框关闭时导入结果与旧版一致。"""

    def setUp(self):
        self.source = _read(IMPORT_UI)

    def test_import_override_forced_none_when_not_consuming(self):
        override = self.source.split("merged_vgmap_import_override = merged_vgmap_ready", 1)[1]
        override = override.split("foldername_gametypename_dict", 1)[0]
        for logic_name, setting in (
            ("LogicName.EFMI", "efmi_merged_consumption"),
            ("LogicName.ZZMI", "zzmi_merged_consumption"),
            ("_himi_logic_name", "himi_merged_consumption"),
        ):
            with self.subTest(logic_name=logic_name):
                self.assertIn(
                    f"if GlobalConfig.logic_name == {logic_name} and not {setting}:\n"
                    "        merged_vgmap_import_override = None",
                    override,
                )

    def test_create_mesh_uses_override(self):
        self.assertIn("use_merged_vgmap=merged_vgmap_import_override", self.source)
        self.assertNotIn("use_merged_vgmap=merged_vgmap_ready", self.source)

    def test_skeleton_group_collection_gated_on_consumption(self):
        self.assertIn(
            "if zzmi_merged_consumption and merged_vgmap_ready is True:",
            self.source,
            "SkeletonGroup 分组合集是合并骨架导入体验的一部分，复选框关闭时"
            "不得把对象归入 SkeletonGroup_<N> 合集",
        )

    def test_reports_only_when_consuming(self):
        """复选框关闭时的修复性预生成只记 stdout，不弹警告打扰用户。"""
        self.assertGreaterEqual(
            self.source.count("if zzmi_merged_consumption:\n"),
            3,
            "INFO/WARNING 上报与自动关闭选项都必须以「本次确实请求消费」为前提",
        )

    def test_armature_creation_is_opt_in_below_the_merge_checkbox(self):
        props = _read(GLOBAL_PROPERTIES)
        panel = _read(IMPORT_PANEL)
        self.assertRegex(props, r"(?s)zzmi_generate_armature_on_import: bpy\.props\.BoolProperty\([^)]*default=False")
        self.assertRegex(
            panel,
            r'layout\.prop\(global_properties, "import_merged_vgmap"\)\n'
            r'\s+if GlobalConfig\.logic_name == LogicName\.ZZMI:\n'
            r'\s+row = layout\.row\(\)\n'
            r'\s+row\.enabled = global_properties\.import_merged_vgmap\n'
            r'\s+row\.prop\(global_properties, "zzmi_generate_armature_on_import"\)',
        )
        self.assertRegex(
            self.source,
            r"if \(\s*zzmi_merged_consumption\s*and merged_vgmap_ready is True"
            r"\s*and GlobalProterties\.zzmi_generate_armature_on_import\(\)\s*\):"
            r'\s*yield 93, "重建导入骨架"',
        )
        self.assertEqual(self.source.count("zzmi_generate_armature_on_import"), 1)


class SwitchIndependenceTests(unittest.TestCase):
    """两个实验开关不得参与数据生成决策。"""

    def test_experimental_switches_absent_from_import_ui(self):
        source = _read(IMPORT_UI)
        for switch in EXPERIMENTAL_SWITCHES:
            self.assertNotIn(
                switch,
                source,
                f"{switch} 只影响导出侧的消费方式，不得参与导入侧的生成决策",
            )

    def test_helper_exposes_missing_metadata_probe(self):
        source = _read(ZZMI_SKELETON)
        self.assertIn(
            "def missing_merged_metadata_exist(",
            source,
            "ZZMISkeletonMergeHelper 必须提供元数据缺失/不完整判据（导入侧自愈门控）",
        )
        self.assertIn(
            "_zzmi_cache_intact(",
            source,
            "自愈判据必须包含快路径完整性检查，否则旧算法版本/缺字段不会自愈",
        )
        self.assertIn(
            "expect_cross=expect_cross",
            source,
            "自愈判据必须同时校验编号域：开着跨组合并时，历史分组编号的 VGMap"
            "看起来齐全却是各部件局部槽位，不校验就会被当成合并成果跳过",
        )


if __name__ == "__main__":
    unittest.main()
