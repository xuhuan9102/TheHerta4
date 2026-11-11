# -*- coding: utf-8 -*-
"""物体切换面板「附加模式」判定（附加到形态键滑块面板 · 回归测试）。

锁住三条不变量：

1. 蓝图里存在**确实会产出**滑块面板的形态键扩展节点 → 走附加模式，调用
   ``_generate_attached_panel`` 且 ``_in_place=True``。
2. 找不到这样的节点 → 走原有独立浮动面板机制，**绝不**调用附加生成。
3. 「勾了开关」不等于「会产出」：节点自己的 ``will_emit_slider_panel()`` 为假，
   或目标 ini 里没有真正写出滑块面板（缺标记 / 缺共享变量）时，都必须退回独立面板
   —— 附加块复用的是 ``$img0_x`` / ``$zoom0`` / ``$help`` 等**无命名空间**全局变量，
   在那种 ini 里根本不存在，3Dmigoto 侧会静默读到 0。

另锁一条 UI 回归：按钮样式（背景色 / 边框色 / 边框宽度 / 透明度 / 对齐）**只在附加模式**
隐藏，不能跟着「用备注生成图标」一起隐藏 —— 未勾选备注图标时默认纯色按钮图仍然读这些参数
（见 ``_ensure_button_image``）。
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PKG = "_test_swap_panel_attach_mode"
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None

# 与 test_swap_panel_button_icon.py 同样的理由：顶层先把 PIL 导入 sys.modules，
# 避免 _load_panel() 的 mock.patch.dict 退出时把 PIL 逐出、使被测模块绑定到另一个实例。
if PIL_AVAILABLE:
    from PIL import Image as PILImage  # noqa: F401


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fake_bpy():
    module = types.ModuleType("bpy")
    module.types = types.SimpleNamespace(Node=object, Operator=object, PropertyGroup=object)
    prop = lambda **_kwargs: None
    props = types.ModuleType("bpy.props")
    for name in (
        "StringProperty", "BoolProperty", "IntProperty", "FloatProperty",
        "EnumProperty", "CollectionProperty", "FloatVectorProperty",
    ):
        setattr(props, name, prop)
    module.props = props
    module.path = types.SimpleNamespace(abspath=lambda path: path)
    module.data = types.SimpleNamespace(node_groups=types.SimpleNamespace(get=lambda name: None))
    return module


def _load_panel():
    fake_bpy = _fake_bpy()
    package = types.ModuleType(PKG + ".blueprint")
    package.__path__ = []
    base = types.ModuleType(PKG + ".blueprint.node_postprocess_base")

    class Base:
        @classmethod
        def split_anim_driver_block_content(cls, content):
            return "", content

        @classmethod
        def split_auto_appended_tail_content(cls, content):
            return content, ""

    base.SSMTNode_PostProcess_Base = Base
    root_pkg = types.ModuleType(PKG)
    root_pkg.__path__ = []
    common_pkg = types.ModuleType(PKG + ".common")
    common_pkg.__path__ = [str(ROOT / "common")]

    stubs = {
        root_pkg.__name__: root_pkg,
        common_pkg.__name__: common_pkg,
        "bpy": fake_bpy,
        "bpy.props": fake_bpy.props,
        PKG + ".blueprint": package,
        base.__name__: base,
    }
    with mock.patch.dict(sys.modules, stubs):
        # 产品代码用相对导入 `from ..common.global_config import ...`；
        # 这里按**真实文件位置**把 common 包挂进假包，别复制副本（复制会随产品改动腐坏）。
        importlib.import_module(PKG + ".common.global_config")
        return _load_module(
            PKG + ".blueprint.node_postprocess_swap_panel",
            ROOT / "blueprint" / "node_postprocess_swap_panel.py",
        )


def _tree(name, nodes, **extra):
    tree = types.SimpleNamespace(name=name, nodes=list(nodes))
    for key, value in extra.items():
        setattr(tree, key, value)
    return tree


def _shapekey_node(emit=None, use_slider_panel=True, name="ShapeKeyExt", mute=False):
    """形态键扩展节点替身。

    ``emit`` 非 None 时挂上 ``will_emit_slider_panel``（可调用，也可传会抛异常的函数）。
    """
    node = types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_ShapeKeyExt",
        name=name,
        mute=mute,
        use_slider_panel=use_slider_panel,
    )
    if emit is not None:
        node.will_emit_slider_panel = emit
    return node


def _other_node(bl_idname="SSMTNode_PostProcess_BufferCleanup", name="Other"):
    return types.SimpleNamespace(bl_idname=bl_idname, name=name, mute=False)


class _RecordingUI:
    """吞掉 draw_buttons 的链式 UI 调用，记录 prop 属性名与 label 文本。"""

    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        if name in ("prop", "label"):
            def _record(*args, **kwargs):
                if name == "prop" and len(args) >= 2 and isinstance(args[1], str):
                    self.log["props"].append(args[1])
                elif name == "label":
                    text = kwargs.get("text", args[0] if args else "")
                    if text:
                        self.log["labels"].append(text)
                return self
            return _record

        def _passthrough(*args, **kwargs):
            return self
        return _passthrough


BTN_STYLE_PROPS = (
    "button_bg_color", "button_border_color", "button_border_width",
    "button_opacity", "button_align",
)


class AttachModeTestCase(unittest.TestCase):
    DEFAULTS = {
        "name": "SwapPanel",
        "create_cumulative_backup": False,
        "ini_file_path": "",
        "namespace": "ns_attach",
        "last_mod_ini_path": "",
        "use_remark_as_icon": False,
        "remark_font_family": "msyh.ttc",
        "remark_font_size": 36,
        "remark_text_color": (1.0, 1.0, 1.0),
        "remark_stroke_color": (0.0, 0.0, 0.0),
        "remark_stroke_width": 2,
        "button_bg_color": (0.16, 0.22, 0.32),
        "button_border_color": (0.59, 0.75, 0.94),
        "button_border_width": 2,
        "button_opacity": 0.9,
        "button_align": "CENTER",
        "button_image": "",
        "button_border_image": "",
        "background_image": "",
        "background_opacity": 1.0,
        "background_corner_radius": 5,
        "background_border_color": (0.59, 0.75, 0.94),
        "background_border_width": 3,
        "help_key": "home",
        "reset_key": "end",
        "zoom_in_key": "pageup",
        "zoom_out_key": "pagedown",
        "drag_key": "leftmouse",
        "gui_only": True,
        "panel_default_scale": 1.0,
        "panel_min_height": 100.0,
        "button_height": 64.0,
        "button_width": 384.0,
        "buttons_per_row": 1,
        "button_column_spacing": 4.0,
        "button_row_spacing": 4.0,
        "button_top_padding": 8.0,
        "target_object": "",
        "detect_hash": "",
        "detect_index_count": "",
        "swap_panel_entries": [],
        "swap_panel_button_entries": [],
    }

    def setUp(self):
        self.module = _load_panel()
        self.panel = self.module.SSMTNode_PostProcess_SwapPanel()
        for name, value in self.DEFAULTS.items():
            setattr(self.panel, name, value)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ini = self.root / "Mod.ini"

    # ------------------------------------------------------------------ 工具
    def _write_ini(self, body):
        self.ini.write_text(body, encoding="utf-8")
        return str(self.ini)

    def _slider_body(self, module, omit=(), with_marker=True):
        cls = module.SSMTNode_PostProcess_SwapPanel
        lines = []
        if with_marker:
            lines.append("; " + cls.SLIDER_PANEL_MARKER)
        for var in cls.SLIDER_PANEL_SHARED_VARS:
            if var in omit:
                continue
            lines.append(f"global persist {var} = 0")
        return "\n".join(lines) + "\n"

    def _attach_probe(self, tree, ini_body):
        """驱动 execute_postprocess，返回 (返回值, 附加生成 mock)。"""
        self.panel.id_data = tree
        self._write_ini(ini_body)
        with mock.patch.object(
            self.panel, "_build_button_list", return_value=[{"var_name": "$swapkey0"}]
        ), mock.patch.object(
            self.panel, "_generate_attached_panel", return_value=True
        ) as attached:
            result = self.panel.execute_postprocess(str(self.root), _ini_path=str(self.ini))
        return result, attached


class FindSliderPanelNodeTests(AttachModeTestCase):
    """_find_slider_panel_node：只有「确实会产出」的节点才算数。"""

    def test_emitting_node_is_found(self):
        node = _shapekey_node(emit=lambda: True)
        self.panel.id_data = _tree("Blueprint", [_other_node(), node])
        self.assertIs(self.panel._find_slider_panel_node(), node)

    def test_node_that_will_not_emit_is_skipped(self):
        node = _shapekey_node(emit=lambda: False)
        self.panel.id_data = _tree("Blueprint", [node])
        self.assertIsNone(self.panel._find_slider_panel_node())

    def test_predicate_wins_over_the_switch_flag(self):
        """判定优先问节点自己（与导出代码同源），退回开关只是它没提供判定时的兜底。"""
        node = _shapekey_node(emit=lambda: True, use_slider_panel=False)
        self.panel.id_data = _tree("Blueprint", [node])
        self.assertIs(self.panel._find_slider_panel_node(), node)

    def test_predicate_exception_falls_back_to_switch(self):
        def boom():
            raise AttributeError("no sections yet")

        node = _shapekey_node(emit=boom, use_slider_panel=True)
        self.panel.id_data = _tree("Blueprint", [node])
        self.assertIs(self.panel._find_slider_panel_node(), node)

    def test_node_without_predicate_falls_back_to_switch(self):
        node = _shapekey_node(emit=None, use_slider_panel=True)
        self.panel.id_data = _tree("Blueprint", [node])
        self.assertIs(self.panel._find_slider_panel_node(), node)

        off = _shapekey_node(emit=None, use_slider_panel=False, name="ShapeKeyExtOff")
        self.panel.id_data = _tree("Blueprint", [off])
        self.assertIsNone(self.panel._find_slider_panel_node())

    def test_first_emitting_node_wins_when_earlier_one_is_silent(self):
        silent = _shapekey_node(emit=lambda: False, name="Silent")
        emitting = _shapekey_node(emit=lambda: True, name="Emitting")
        self.panel.id_data = _tree("Blueprint", [silent, emitting])
        self.assertIs(self.panel._find_slider_panel_node(), emitting)

    def test_muted_node_is_skipped(self):
        node = _shapekey_node(emit=lambda: True, mute=True)
        self.panel.id_data = _tree("Blueprint", [node])
        self.assertIsNone(self.panel._find_slider_panel_node())

    def test_nested_blueprint_is_searched(self):
        node = _shapekey_node(emit=lambda: True, name="NestedShapeKeyExt")
        nested = _tree("NestedBlueprint", [node], bl_idname="SSMTBlueprintTreeType")
        nest = _other_node(bl_idname="SSMTNode_Blueprint_Nest", name="Nest")
        nest.blueprint_name = "NestedBlueprint"

        with mock.patch.object(self.module.bpy.data.node_groups, "get", return_value=nested):
            self.panel.id_data = _tree("Blueprint", [nest])
            self.assertIs(self.panel._find_slider_panel_node(), node)

    def test_no_tree_returns_none(self):
        self.panel.id_data = None
        self.assertIsNone(self.panel._find_slider_panel_node())


class SliderPanelEmittedTests(AttachModeTestCase):
    """_slider_panel_emitted：用 ini 内容做证据校验，而不是相信节点自己的开关。"""

    def test_complete_slider_ini_is_recognised(self):
        path = self._write_ini(self._slider_body(self.module))
        self.assertTrue(self.panel._slider_panel_emitted(path))

    def test_missing_marker_is_rejected(self):
        path = self._write_ini(self._slider_body(self.module, with_marker=False))
        self.assertFalse(self.panel._slider_panel_emitted(path))

    def test_missing_shared_variable_is_rejected(self):
        path = self._write_ini(self._slider_body(self.module, omit=("$zoom0",)))
        self.assertFalse(self.panel._slider_panel_emitted(path))

    def test_bare_global_declaration_also_accepted(self):
        body = [
            "; " + self.module.SSMTNode_PostProcess_SwapPanel.SLIDER_PANEL_MARKER,
        ]
        body += [f"global {var} = 0" for var
                 in self.module.SSMTNode_PostProcess_SwapPanel.SLIDER_PANEL_SHARED_VARS]
        path = self._write_ini("\n".join(body) + "\n")
        self.assertTrue(self.panel._slider_panel_emitted(path))

    def test_unreadable_ini_is_rejected(self):
        self.assertFalse(self.panel._slider_panel_emitted(str(self.root / "missing.ini")))


class AttachedModeDecisionTests(AttachModeTestCase):
    """execute_postprocess 的两条分支：附加模式 与 独立浮动面板。"""

    def test_attached_used_when_node_emits_and_ini_has_panel(self):
        node = _shapekey_node(emit=lambda: True)
        result, attached = self._attach_probe(
            _tree("Blueprint", [node]), self._slider_body(self.module)
        )
        self.assertTrue(result)
        attached.assert_called_once()
        args, kwargs = attached.call_args
        self.assertEqual(args[1], str(self.ini))
        self.assertTrue(kwargs.get("_in_place"), "附加模式必须就地替换旧配置")

    def test_independent_panel_used_when_ini_lacks_slider_panel(self):
        """问题 2 的回归：节点自认会产出、但 ini 里并无滑块面板 → 退回独立面板。"""
        node = _shapekey_node(emit=lambda: True)
        body = self._slider_body(self.module, with_marker=False)
        # DUP_GUARD 让独立面板分支在写盘前短路，避免测试真的去生成整块面板配置。
        body += self.panel.DUP_GUARD + "\n"
        result, attached = self._attach_probe(_tree("Blueprint", [node]), body)
        self.assertFalse(result)
        attached.assert_not_called()

    def test_independent_panel_used_when_no_slider_node(self):
        body = self._slider_body(self.module) + self.panel.DUP_GUARD + "\n"
        result, attached = self._attach_probe(_tree("Blueprint", [_other_node()]), body)
        self.assertFalse(result)
        attached.assert_not_called()

    def test_independent_panel_used_when_node_does_not_emit(self):
        node = _shapekey_node(emit=lambda: False)
        body = self._slider_body(self.module) + self.panel.DUP_GUARD + "\n"
        result, attached = self._attach_probe(_tree("Blueprint", [node]), body)
        self.assertFalse(result)
        attached.assert_not_called()


class ButtonStyleVisibilityTests(AttachModeTestCase):
    """按钮样式控件只在附加模式隐藏（问题 3 的回归）。"""

    def _draw(self, tree):
        self.panel.id_data = tree
        log = {"props": [], "labels": []}
        self.panel.draw_buttons(None, _RecordingUI(log))
        return log

    @unittest.skipUnless(PIL_AVAILABLE, "未安装 Pillow 时该分支不渲染备注图标设置")
    def test_style_controls_remain_without_remark_icon_in_independent_mode(self):
        self.panel.use_remark_as_icon = False
        log = self._draw(_tree("Blueprint", [_other_node()]))
        for name in BTN_STYLE_PROPS:
            self.assertIn(name, log["props"], f"独立面板模式不应隐藏按钮样式项 {name}")
        self.assertIn("按钮样式（文字图标/默认按钮）", log["labels"])

    @unittest.skipUnless(PIL_AVAILABLE, "未安装 Pillow 时该分支不渲染备注图标设置")
    def test_style_controls_hidden_in_attached_mode(self):
        node = _shapekey_node(emit=lambda: True)
        self.panel.use_remark_as_icon = False
        log = self._draw(_tree("Blueprint", [node]))
        for name in BTN_STYLE_PROPS:
            self.assertNotIn(name, log["props"], f"附加模式应隐藏按钮样式项 {name}")

    @unittest.skipUnless(PIL_AVAILABLE, "未安装 Pillow 时该分支不渲染备注图标设置")
    def test_remark_icon_settings_still_rendered(self):
        self.panel.use_remark_as_icon = True
        log = self._draw(_tree("Blueprint", [_other_node()]))
        for name in ("remark_font_family", "remark_font_size", "remark_stroke_width"):
            self.assertIn(name, log["props"])
        for name in BTN_STYLE_PROPS:
            self.assertIn(name, log["props"])
        self.assertIn("未检测到形态键滑块面板，使用独立浮动面板机制", log["labels"])

    @unittest.skipUnless(PIL_AVAILABLE, "未安装 Pillow 时该分支不渲染备注图标设置")
    def test_attached_banner_names_the_slider_node(self):
        node = _shapekey_node(emit=lambda: True, name="MySliderPanel")
        log = self._draw(_tree("Blueprint", [node]))
        self.assertIn("已检测到形态键滑块面板（MySliderPanel）", log["labels"])


if __name__ == "__main__":
    unittest.main()
