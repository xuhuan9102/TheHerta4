"""ZZMI 合并顶点组入口与跨组导出契约。"""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
PROPERTY_KEY = "cross_group_merged_vgmap_test"
GLOBAL_PROPERTIES = REPO_ROOT / "common" / "global_properties.py"
PANEL = REPO_ROOT / "ui" / "ui_panel_basic.py"
GENERATOR = REPO_ROOT / "ui" / "universal" / "zzmi.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class UnifiedSwitchTests(unittest.TestCase):
    def test_old_saved_options_do_not_override_the_master_switch(self):
        source = ast.parse(_read(GLOBAL_PROPERTIES))
        cls = next(
            node for node in source.body
            if isinstance(node, ast.ClassDef) and node.name == "GlobalProterties"
        )
        names = {"import_merged_vgmap", PROPERTY_KEY, "zzmi_merged_redirect_enabled"}
        declarations = {
            node.target.id for node in cls.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        self.assertEqual(declarations & names, {"import_merged_vgmap"})
        funcs = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
        namespace = {}
        module = ast.Module(body=[funcs[name] for name in sorted(names)], type_ignores=[])
        exec(compile(module, str(GLOBAL_PROPERTIES), "exec"), namespace)  # noqa: S102
        scene = SimpleNamespace()
        wrapper = type("GlobalProperties", (), {
            **{name: namespace[name] for name in names},
            "_instance": classmethod(lambda cls: scene),
        })
        for enabled in (True, False):
            scene.import_merged_vgmap = enabled
            scene.cross_group_merged_vgmap_test = not enabled
            scene.zzmi_merged_redirect_enabled = not enabled
            self.assertIs(wrapper.cross_group_merged_vgmap_test(), enabled)
            self.assertIs(wrapper.zzmi_merged_redirect_enabled(), enabled)

    def test_panel_only_draws_the_master_switch(self):
        panel = _read(PANEL)
        self.assertEqual(panel.count('layout.prop(global_properties, "import_merged_vgmap")'), 1)
        self.assertNotIn(f'layout.prop(global_properties, "{PROPERTY_KEY}")', panel)
        self.assertNotIn('layout.prop(global_properties, "zzmi_merged_redirect_enabled")', panel)


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
        body = self._block("def _merged_cross_group_enabled() -> bool:")
        body = body.split("\n    @staticmethod", 1)[0]
        self.assertIn(
            f'_zzmi_prop_flag("{PROPERTY_KEY}", True)',
            body,
            "开关必须按 True 缺省读取（与属性声明默认一致）",
        )

    def test_detection_short_circuits_when_disabled(self):
        body = self._block("def _merged_cross_group_component_ids(self, skeleton_group: int)")
        body = body.split("\n    def ", 1)[0]
        self.assertIn("if not self._merged_cross_group_enabled():", body)
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
        self.assertIn("cross_enabled = self._merged_cross_group_enabled()", body)
        self.assertIn("禁止跨组别骨骼合并", body, "关闭时仍要保留历史报警文案")
