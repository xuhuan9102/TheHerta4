import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_postprocess_anim_driver_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []

sys.modules[f"{PKG}.blueprint"].__path__ = [str(Path(__file__).resolve().parents[1] / "blueprint")]


_bpy_props = _install_module(
    "bpy.props",
    StringProperty=lambda **_kwargs: None,
)
_bpy_types = _install_module(
    "bpy.types",
    Node=object,
    NodeSocket=object,
    PropertyGroup=object,
    UIList=object,
    Operator=object,
)
_fake_bpy = _install_module(
    "bpy",
    props=_bpy_props,
    types=_bpy_types,
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
    data=types.SimpleNamespace(node_groups={}),
)

_install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)
def _split_auto_appended_tail(content):
    markers = (
        "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
        "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
        "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---",
    )
    text = str(content or "")
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = str(line or "").strip()
        if stripped in markers or stripped.startswith("; --- AUTO-APPENDED UI PANEL "):
            return text[:offset], text[offset:]
        offset += len(line)
    return text, ""


_install_module(f"{PKG}.blueprint.node_postprocess_base")
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "SSMTNode_PostProcess_Base",
        (),
        {
            "split_auto_appended_tail_content": classmethod(lambda cls, content: _split_auto_appended_tail(content)),
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
        },
    ),
)
_install_module(
    f"{PKG}.blueprint.anim_driver_collector",
    AnimationDriverCollector=type("AnimationDriverCollector", (), {}),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        read_from_main_json_ssmt4=lambda: None,
        path_generate_mod_folder=lambda: "",
        get_workspace_name=lambda: "",
    ),
)


def _fake_activation_flag():
    """与生产实现同源：角色激活标志（NTEMI 用 $ntmi_active0）。"""
    config = sys.modules[f"{PKG}.common.global_config"].GlobalConfig
    return "$ntmi_active0" if getattr(config, "logic_name", "") == "NTEMI" else "$active0"


_install_module(
    f"{PKG}.blueprint.anim_driver_base",
    SSMTNode_AnimDriver_Base=type(
        "SSMTNode_AnimDriver_Base",
        (),
        {"_get_activation_flag": staticmethod(_fake_activation_flag)},
    ),
)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_module("blueprint.node_postprocess_anim_driver", "blueprint/node_postprocess_anim_driver.py")
SSMTNode_PostProcess_AnimDriver = module.SSMTNode_PostProcess_AnimDriver


def test_fluid_input_writer_marks_dirty_inside_the_driver_owned_block():
    node = SSMTNode_PostProcess_AnimDriver()
    text = node._build_ini_content([{'ini_content':
        '[Present]\n$\\Fluid_demo\\fluid_emit_0 = $input\n'}])
    assert '$\\Fluid_demo\\fluid_emit_0 = $input\n$\\Fluid_demo\\fluid_emitter_gate_dirty = 1' in text
    assert text.index('fluid_emitter_gate_dirty') < text.index('; --- END ANIMATION DRIVER SECTION ---')


class _FakeBlueprint:
    def __init__(self, name):
        self.name = name
        self.bl_idname = "SSMTBlueprintTreeType"
        self.nodes = []
        self.links = []
        self._props = {"is_animation_driver": True}

    def get(self, key, default=None):
        return self._props.get(key, default)


class NodePostprocessAnimDriverTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.node_groups = {}

    def test_refresh_exported_anim_driver_section_rewrites_existing_section_only(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint

        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: [{"ini_content": "[Constants]\nglobal $foo = 1"}]

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(
                "\n".join(
                    [
                        "; --- ANIMATION DRIVER SECTION ---",
                        "old driver section",
                        "; --- END ANIMATION DRIVER SECTION ---",
                        "",
                        "[TextureOverrideA]",
                        "hash = 123",
                    ]
                ),
                encoding="utf-8",
            )

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertTrue(success)
            self.assertIn("已刷新", message)
            updated = ini_path.read_text(encoding="utf-8")
            self.assertIn("; --- ANIMATION DRIVER SECTION ---", updated)
            self.assertIn("global $foo = 1", updated)
            self.assertNotIn("old driver section", updated)
            self.assertIn("[TextureOverrideA]", updated)
            self.assertIn("hash = 123", updated)

    def test_execute_postprocess_raises_when_refresh_fails(self):
        node = SSMTNode_PostProcess_AnimDriver()
        node.refresh_exported_anim_driver_section = lambda _path: (
            False,
            "动画驱动节点生成失败",
        )

        with self.assertRaisesRegex(RuntimeError, "动画驱动节点生成失败"):
            node.execute_postprocess("C:/Export")

    def test_refresh_exported_anim_driver_section_can_clear_stale_section(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint

        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: []

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(
                "\n".join(
                    [
                        "; --- ANIMATION DRIVER SECTION ---",
                        "stale driver section",
                        "; --- END ANIMATION DRIVER SECTION ---",
                        "",
                        "[TextureOverrideA]",
                        "hash = 123",
                    ]
                ),
                encoding="utf-8",
            )

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertTrue(success)
            self.assertIn("已清除", message)
            updated = ini_path.read_text(encoding="utf-8")
            self.assertNotIn("; --- ANIMATION DRIVER SECTION ---", updated)
            self.assertNotIn("stale driver section", updated)
            self.assertIn("[TextureOverrideA]", updated)
            self.assertIn("hash = 123", updated)

    def test_refresh_exported_anim_driver_section_skips_backup_when_content_is_unchanged(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint

        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: [{"ini_content": "[Constants]\nglobal $foo = 1"}]
        backup_calls = []
        node._create_cumulative_backup = lambda ini_file_path, mod_export_path: backup_calls.append((ini_file_path, mod_export_path))

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(
                node._build_ini_content(node._collect_ini_paragraphs(blueprint)),
                encoding="utf-8",
            )

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertTrue(success)
            self.assertIn("已是最新状态", message)
            self.assertEqual(backup_calls, [])

    def test_present_body_is_gated_on_character_presence(self):
        """角色不在场时动画驱动段整段不跑（每帧 466 条语句的常开开销）。"""
        node = SSMTNode_PostProcess_AnimDriver()
        paragraph = {
            "ini_content": (
                "[KeyToggle_Anim1]\ncondition = $active0 == 1\nkey = No_Ctrl Alt Numpad7\n"
                "[Constants]\nglobal persist $animation_paused1 = 0\n"
                "[Present]\nif $animation_paused1 == 1\n    $swapkey1 = $swapkey1 + 1\nendif"
            )
        }

        gated = node._gate_present_content(paragraph["ini_content"])
        lines = gated.split("\n")
        present_index = lines.index("[Present]")

        self.assertEqual(lines[present_index + 1], "if $active0 == 1")
        self.assertEqual(lines[-1], "endif")
        # [Present] 之前的段落（KeyToggle / Constants）不受影响：热键输入照常
        self.assertEqual(lines[:present_index + 1], paragraph["ini_content"].split("\n")[:present_index + 1])
        self.assertIn("    $swapkey1 = $swapkey1 + 1", lines)

    def test_gate_stops_at_next_section_header(self):
        """回归：段落里 [Present] 之后可能还有别的段（开关节点发的 [KeyToggle_*]，
        其段位置取决于节点排序）—— 段头绝不能被裹进 if 里。"""
        node = SSMTNode_PostProcess_AnimDriver()
        content = (
            "[Constants]\nglobal $a = 1\n"
            "[Present]\n$a = $a + 1\n"
            "[KeyToggle_Anim9]\ncondition = $active0 == 1\nkey = No_Modifiers K\n"
            "type = cycle\n$animation_paused9 = 0,1"
        )

        gated = node._gate_present_content(content)
        lines = gated.split("\n")
        present_index = lines.index("[Present]")

        self.assertEqual(lines[present_index + 1], "if $active0 == 1")
        self.assertEqual(lines[present_index + 3], "endif")
        # KeyToggle 段完整落在门控之外
        toggle_index = lines.index("[KeyToggle_Anim9]")
        self.assertGreater(toggle_index, present_index + 3)
        self.assertEqual(
            lines[toggle_index:],
            content.split("\n")[content.split("\n").index("[KeyToggle_Anim9]"):],
        )
        self.assertEqual(sum(1 for line in lines if line.strip() == "endif"), 1)

    def test_gate_uses_ntemi_activation_flag(self):
        node = SSMTNode_PostProcess_AnimDriver()
        config = sys.modules[f"{PKG}.common.global_config"].GlobalConfig
        config.logic_name = "NTEMI"
        try:
            gated = node._gate_present_content("[Present]\n$foo = 1")
        finally:
            config.logic_name = ""

        self.assertIn("if $ntmi_active0 == 1", gated)

    def test_gate_skips_content_without_present_section(self):
        node = SSMTNode_PostProcess_AnimDriver()
        constants_only = "[Constants]\nglobal $foo = 1"
        self.assertEqual(node._gate_present_content(constants_only), constants_only)

    def test_gate_skips_empty_present_body(self):
        node = SSMTNode_PostProcess_AnimDriver()
        self.assertEqual(node._gate_present_content("[Present]\n"), "[Present]\n")

    def test_build_ini_content_gates_every_paragraph(self):
        node = SSMTNode_PostProcess_AnimDriver()
        paragraphs = [
            {"ini_content": "[Constants]\nglobal $a = 1\n[Present]\n$a = 1"},
            {"ini_content": "[Constants]\nglobal $b = 1\n[Present]\n$b = 1"},
        ]

        content = node._build_ini_content(paragraphs)
        lines = content.split("\n")

        self.assertEqual(lines.count("[Present]"), 2)
        self.assertEqual(lines.count("if $active0 == 1"), 2)
        # 每个 [Present] 之后紧跟门控，且 endif 数量配平
        for index, line in enumerate(lines):
            if line == "[Present]":
                self.assertEqual(lines[index + 1], "if $active0 == 1")
        self.assertEqual(lines.count("endif"), 2)

    def test_find_target_ini_file_rejects_ambiguous_workspace_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "Workspace_A.ini").write_text("", encoding="utf-8")
            (Path(temp_dir) / "Workspace_B.ini").write_text("", encoding="utf-8")

            original_get_workspace_name = module.GlobalConfig.get_workspace_name
            module.GlobalConfig.get_workspace_name = staticmethod(lambda: "Workspace")
            try:
                ini_path, error_message = SSMTNode_PostProcess_AnimDriver._find_target_ini_file(temp_dir)
            finally:
                module.GlobalConfig.get_workspace_name = original_get_workspace_name

            self.assertEqual(ini_path, "")
            self.assertIn("多个匹配当前工作空间的 INI 文件", error_message)


    def test_compose_keeps_ui_tail_after_anim_driver_top_block(self):
        node = SSMTNode_PostProcess_AnimDriver()
        original = (
            "[TextureOverrideA]\n"
            "hash = 123\n\n"
            "; --- AUTO-APPENDED UI PANEL Main ---\n"
            "[CustomShaderDraw]\n"
            "vs = ./res/draw.hlsl\n"
        )
        ini_block = node._build_ini_content([{"ini_content": "[Constants]\nglobal $foo = 1"}])
        result = node._compose_updated_ini_content(original, ini_block)
        self.assertLess(
            result.index("; --- ANIMATION DRIVER SECTION ---"),
            result.index("[TextureOverrideA]"),
        )
        self.assertLess(result.index("global $foo = 1"), result.index("; --- AUTO-APPENDED UI PANEL Main ---"))
        self.assertEqual(result.count("; --- ANIMATION DRIVER SECTION ---"), 1)
        self.assertEqual(result.count("; --- AUTO-APPENDED UI PANEL Main ---"), 1)

    def test_refresh_removes_all_stale_anim_driver_blocks(self):
        node = SSMTNode_PostProcess_AnimDriver()
        original = (
            "; --- ANIMATION DRIVER SECTION ---\n"
            "stale one\n"
            "; --- END ANIMATION DRIVER SECTION ---\n"
            "[TextureOverrideA]\n"
            "hash = 123\n"
            "; --- ANIMATION DRIVER SECTION ---\n"
            "stale two\n"
            "; --- END ANIMATION DRIVER SECTION ---\n"
        )
        stripped, removed = node._strip_existing_anim_driver_section(original)
        self.assertTrue(removed)
        self.assertNotIn("stale one", stripped)
        self.assertNotIn("stale two", stripped)
        self.assertIn("[TextureOverrideA]", stripped)


    def test_refresh_preserves_main_constants_and_ui_tail(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint
        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: [
            {"ini_content": "[Constants]\nglobal $new = 1"}
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(
                "\n".join(
                    [
                        "; --- ANIMATION DRIVER SECTION ---",
                        "old driver content",
                        "; --- END ANIMATION DRIVER SECTION ---",
                        "[Constants]",
                        "global persist $main = 0",
                        "[Present]",
                        "run = CustomShaderMain",
                        "; --- AUTO-APPENDED UI PANEL UIPanel ---",
                        "[Constants]",
                        "global persist $ui = 1",
                        "[Present]",
                        "run = CustomShaderUI",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertTrue(success)
            updated = ini_path.read_text(encoding="utf-8")
            self.assertNotIn("old driver content", updated)
            self.assertIn("global $new = 1", updated)
            self.assertIn("global persist $main = 0", updated)
            self.assertIn("global persist $ui = 1", updated)
            self.assertIn("run = CustomShaderMain", updated)
            self.assertIn("run = CustomShaderUI", updated)
            self.assertLess(
                updated.index("; --- ANIMATION DRIVER SECTION ---"),
                updated.index("[Constants]"),
            )
            self.assertGreater(
                updated.index("global persist $ui = 1"),
                updated.index("; --- AUTO-APPENDED UI PANEL UIPanel ---"),
            )

    def test_incomplete_anim_driver_section_aborts_without_deleting_content(self):
        blueprint = _FakeBlueprint("AnimBlueprint")
        _fake_bpy.data.node_groups[blueprint.name] = blueprint
        node = SSMTNode_PostProcess_AnimDriver()
        node.blueprint_name = blueprint.name
        node._collect_ini_paragraphs = lambda _blueprint: [
            {"ini_content": "[Constants]\nglobal $new = 1"}
        ]

        original = (
            "; --- ANIMATION DRIVER SECTION ---\n"
            "old driver content without end marker\n"
            "[Constants]\n"
            "global persist $main = 0\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir) / "test.ini"
            ini_path.write_text(original, encoding="utf-8")

            success, message = node.refresh_exported_anim_driver_section(temp_dir)

            self.assertFalse(success)
            self.assertIn("缺少结束标记", message)
            self.assertEqual(ini_path.read_text(encoding="utf-8"), original)

    def test_strip_leaves_incomplete_anim_driver_section_untouched(self):
        node = SSMTNode_PostProcess_AnimDriver()
        original = (
            "; --- ANIMATION DRIVER SECTION ---\n"
            "stale content\n"
            "[Constants]\n"
            "global persist $main = 0\n"
        )
        stripped, removed = node._strip_existing_anim_driver_section(original)
        self.assertFalse(removed)
        self.assertEqual(stripped, original)


if __name__ == "__main__":
    unittest.main()
