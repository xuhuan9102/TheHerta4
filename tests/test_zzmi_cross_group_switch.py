# -*- coding: utf-8 -*-
"""ZZMI 跨组融合统一顶点组测试开关（cross_group_merged_vgmap_test）契约单测。

背景（**2026-08 初建时的口径，已被下方 2026-10-10 更新取代**）：跨 SkeletonGroup（对象变换不同）的合并需要一层逐帧对象空间换算，当时尚未实现。
本开关先把"实验入口"与**已验证可用的合并骨骼**（import_merged_vgmap，「使用融合统一
顶点组」）分离出来，供后续实验/测试使用：

1. 属性存在、标签精确为「跨组融合统一顶点组测试」、默认 True（关闭时零行为变化）；
2. 面板放置：位于「使用融合统一顶点组」**正下方**，且仅 ZZMI 逻辑名下渲染；
3. 分离契约（**2026-08 初建口径，已被下方 2026-10-10 更新取代**）：本开关在实现跨组
   功能前**不参与任何导入/导出决策** —— 除属性定义与面板渲染外，生产代码不得引用它。
   这条断言故意做成"脆"的：一旦开始接入跨组链路，必须在本文件里显式登记新的消费点，
   避免实验开关悄悄改变既有合并骨骼的行为（现已登记 `ui/universal/zzmi.py`，见
   `ALLOWED_REFERENCING_FILES`）。

2026-10-10 更新（F1 接入，实机复现件根因）：`ui/universal/zzmi.py` 已登记为本开关的
**消费点** —— `_merged_cross_group_enabled()` 读它决定是否把「被吸收进本组网格的别组
部件」的骨骼写进**宿主组**骨架缓冲（`CustomShaderZZMIMergedSkeletonAttachX_*`）。
关闭时逐字节保持历史行为（跨组件只写自己组 + 大声报警）。

测试纪律：无 bpy（只读源码做断言）。
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROPERTY_KEY = "cross_group_merged_vgmap_test"
PROPERTY_LABEL = "跨组融合统一顶点组测试"

GLOBAL_PROPERTIES = REPO_ROOT / "common" / "global_properties.py"
PANEL = REPO_ROOT / "ui" / "ui_panel_basic.py"
GENERATOR = REPO_ROOT / "ui" / "universal" / "zzmi.py"

# 允许引用本开关的文件（实现跨组链路时在此登记新消费点）
ALLOWED_REFERENCING_FILES = {
    "common/global_properties.py",
    "ui/ui_panel_basic.py",
    # 2026-10-10 F1：跨组骨骼合并的导出侧消费点（读开关决定是否写宿主组缓冲）
    "ui/universal/zzmi.py",
    # 2026-10-21 骨骼池路线（参考实现 loyaltools）：导入侧消费点 —— 开关开启且
    # 锚点链方案算得出来时，槽位由 `plan_from_parts` 定（整只角色一个骨骼池）；
    # 算不出来（跨组无共享骨可锚）则原样回落到历史分组编号。
    "common/zzmi_skeleton.py",
}

# 生产代码扫描根（排除 tests/临时产物/备份/工具产物）
PRODUCTION_ROOTS = ("common", "ui", "blueprint", "utils", "TheHerta4_Velo_Bridge")
EXCLUDED_DIR_PARTS = {
    "__pycache__", "node_modules", ".venv", ".dbg", ".review-out", ".codegraph",
    ".dsh-filess", ".agent-teams", ".codex_benchmarks", ".pr6head", "backup",
    "reports", "dist",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _referencing_files() -> set[str]:
    hits: set[str] = set()
    for root_name in PRODUCTION_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if any(part in EXCLUDED_DIR_PARTS for part in path.parts):
                continue
            if PROPERTY_KEY in _read(path):
                hits.add(path.relative_to(REPO_ROOT).as_posix())
    return hits


class PropertyDeclarationTests(unittest.TestCase):
    """属性声明：标签、默认值、访问器。"""

    def test_declared_as_bool_property_with_exact_label(self):
        source = _read(GLOBAL_PROPERTIES)
        block = re.search(
            rf"^    {PROPERTY_KEY}: bpy\.props\.BoolProperty\((.*?)^    \) # type: ignore",
            source,
            re.S | re.M,
        )
        self.assertIsNotNone(block, f"{PROPERTY_KEY} 必须声明为 bpy.props.BoolProperty")
        body = block.group(1)
        self.assertIn(f'name="{PROPERTY_LABEL}"', body)
        self.assertRegex(body, r"default=True")

    def test_accessor_defaults_true(self):
        source = _read(GLOBAL_PROPERTIES)
        self.assertRegex(
            source,
            rf"def {PROPERTY_KEY}\(cls\):",
            "必须提供类方法访问器（面板与后续链路统一从这里读）",
        )
        self.assertIn(f'_bool_attr("{PROPERTY_KEY}", True)', source)

    def test_independent_from_proven_merged_skeleton_switch(self):
        """两个开关互不代写：各自的 setter 只动自己的键。"""
        source = _read(GLOBAL_PROPERTIES)

        def _setter_body(method_name: str) -> str:
            match = re.search(
                rf"def {method_name}\(cls, value: bool\):\s*\n(?P<body>(?:        .*\n|\s*\n)+)",
                source,
            )
            self.assertIsNotNone(match, f"缺少 setter: {method_name}")
            return match.group("body")

        proven = _setter_body("set_import_merged_vgmap")
        experimental = _setter_body("set_cross_group_merged_vgmap_test")
        self.assertIn('"import_merged_vgmap"', proven)
        self.assertNotIn(PROPERTY_KEY, proven)
        self.assertIn(f'"{PROPERTY_KEY}"', experimental)
        self.assertNotIn('"import_merged_vgmap"', experimental)

    def test_getter_does_not_delegate_to_proven_switch(self):
        """实验开关的取值不得读已验证开关的值（避免耦合）。"""
        source = _read(GLOBAL_PROPERTIES)
        match = re.search(
            rf"def {PROPERTY_KEY}\(cls\):\s*\n(?P<body>(?:        .*\n|\s*\n)+?)\n    @classmethod",
            source,
        )
        self.assertIsNotNone(match)
        # 去掉文档字符串后再看代码体（文档里会提到已验证开关以示区别）
        body = re.sub(r'""".*?"""', "", match.group("body"), flags=re.S)
        self.assertNotIn("import_merged_vgmap", body)
        self.assertIn(f'"{PROPERTY_KEY}"', body)


class PanelPlacementTests(unittest.TestCase):
    """面板放置：紧跟「使用融合统一顶点组」之后，且仅 ZZMI 渲染。"""

    def setUp(self):
        self.source = _read(PANEL)
        self.lines = self.source.splitlines()

    def _line_index(self, needle: str) -> int:
        for index, line in enumerate(self.lines):
            if needle in line and line.strip().startswith("layout.prop("):
                return index
        self.fail(f"面板缺少 prop 渲染：{needle}")

    def test_rendered_for_zzmi_only(self):
        index = self._line_index(PROPERTY_KEY)
        guard = None
        for line in reversed(self.lines[:index]):
            if re.match(r"^\s*if GlobalConfig\.logic_name", line):
                guard = line
                break
        self.assertIsNotNone(guard, "渲染行必须处于 logic_name 门控内")
        self.assertIn("LogicName.ZZMI", guard)
        self.assertNotIn("LogicName.WWMI", guard)
        self.assertNotIn("LogicName.EFMI", guard)
        # 必须是相等判断（仅 ZZMI），不能是 membership 判断（会连带给其它逻辑名）
        self.assertIn("==", guard)

    def test_placed_directly_below_merged_vgmap_switch(self):
        merged_index = self._line_index("import_merged_vgmap")
        new_index = self._line_index(PROPERTY_KEY)
        self.assertGreater(
            new_index, merged_index,
            "新开关必须放在「使用融合统一顶点组」下方",
        )
        between = [
            line for line in self.lines[merged_index + 1:new_index]
            if line.strip().startswith("layout.prop(")
        ]
        self.assertEqual(
            between, [],
            f"新开关与既有开关之间不应插入其它 prop（实际: {between}）",
        )


class SeparationContractTests(unittest.TestCase):
    """分离契约：实现跨组链路前，本开关不得被生产代码消费。"""

    def test_not_consumed_outside_declaration_and_panel(self):
        referencing = _referencing_files()
        unexpected = referencing - ALLOWED_REFERENCING_FILES
        self.assertEqual(
            unexpected, set(),
            "本开关目前只是实验入口，不应参与导入/导出决策；"
            "若已开始接入跨组链路，请在此登记消费点并更新本测试。"
            f" 意外引用: {sorted(unexpected)}",
        )

    def test_allowlisted_files_actually_reference_it(self):
        """白名单不得变成空壳：两个文件确实各自引用了开关。"""
        referencing = _referencing_files()
        for expected in ALLOWED_REFERENCING_FILES:
            self.assertIn(expected, referencing)


class CrossGroupConsumerContractTests(unittest.TestCase):
    """F1 消费点契约（只读源码）：

    * 关闭 ⇒ 检测函数直接返回空 ⇒ 产物与历史**逐字节相同**；
    * 开启 ⇒ 跨组件骨骼写进**宿主组**骨架缓冲，调色板仍取跨组件自己的资源；
    * 守卫感知开关：关闭时保留历史「禁止跨组别骨骼合并」文案。
    """

    def setUp(self):
        self.source = _read(GENERATOR)

    def _block(self, header: str) -> str:
        self.assertIn(header, self.source, f"缺少 {header}")
        return self.source.split(header, 1)[1]

    def test_gate_reads_switch_with_true_default(self):
        body = self._block("def _merged_cross_group_enabled(self, skeleton_group: int) -> bool:")
        body = body.split("\n    @staticmethod", 1)[0]
        self.assertIn(
            f'_zzmi_prop_flag("{PROPERTY_KEY}", True)',
            body,
            "开关必须按 True 缺省读取（与属性声明默认一致）",
        )

    def test_detection_short_circuits_when_disabled(self):
        body = self._block("def _merged_cross_group_component_ids(self, skeleton_group: int)")
        body = body.split("\n    def ", 1)[0]
        self.assertIn("if not self._merged_cross_group_enabled(skeleton_group):", body)
        self.assertIn("return []", body)
        # 判据必须与「几何被吸收」同源：引用骨骼并集 − 本组合法槽位
        self.assertIn("offending = referenced - legal", body)

    def test_cross_attach_targets_host_group_buffer(self):
        body = self._block("def add_merged_skeleton_sections")
        body = body.split("_merged_cross_group_component_ids(skeleton_group)", 1)[1]
        chunk = body[:4000]
        self.assertIn("_merged_cross_attach_name", chunk, "必须发射跨组 attach 段")
        self.assertIn(
            "_merged_skeleton_name(int(skeleton_group), slot)", chunk,
            "写入目标必须是**宿主组**缓冲（skeleton_group 是宿主组）",
        )
        self.assertIn(
            'cross_component["draw_ib"]', chunk,
            "调色板/VgMap 必须取跨组件自己的资源",
        )

    def test_guard_is_switch_aware_but_keeps_legacy_text(self):
        body = self._block("def _warn_cross_group_bone_references(self):")
        self.assertIn("cross_enabled = self._merged_cross_group_enabled(skeleton_group)", body)
        self.assertIn("禁止跨组别骨骼合并", body, "关闭时仍要保留历史报警文案")


class MergedRedirectDefaultTests(unittest.TestCase):
    """「启用合并网格自动重定向（实验）」默认开启（用户 2026-10-07 要求）。"""

    PROPERTY_KEY = "zzmi_merged_redirect_enabled"

    def _block(self) -> str:
        source = _read(GLOBAL_PROPERTIES)
        block = re.search(
            rf"^    {self.PROPERTY_KEY}: bpy\.props\.BoolProperty\((.*?)^    \) # type: ignore",
            source,
            re.S | re.M,
        )
        self.assertIsNotNone(block, f"{self.PROPERTY_KEY} 必须声明为 bpy.props.BoolProperty")
        return block.group(1)

    def test_defaults_to_on(self):
        body = self._block()
        self.assertIn('name="启用合并网格自动重定向（实验）"', body)
        self.assertRegex(body, r"default=True")

    def test_description_keeps_the_geometry_loss_warning(self):
        """默认开启后仍要留住「部分帧序会丢几何」的告警与关闭方式。"""
        body = self._block()
        self.assertIn("丢失整块几何", body)
        self.assertIn("关掉本项", body)


if __name__ == "__main__":
    unittest.main()
