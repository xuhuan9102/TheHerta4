# -*- coding: utf-8 -*-
"""切换面板按钮图标的优先级与备注文字图标生成。

优先级：单按钮图片 → 全局回退图片 → 备注文字图标（use_remark_as_icon）→ 默认按钮图。
备注文字图标曾在重构中被整段删除，这里把优先级链锁住，避免再次被回退逻辑挤掉。
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PKG = "_test_swap_panel_button_icon"
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
DEFAULT_BUTTON_SIZE = (384, 64)

# 顶层先把 PIL 导入 sys.modules：_load_panel() 用 mock.patch.dict 替换 sys.modules，
# 若 PIL 是在该 patch 内首次导入，patch 退出时会把它逐出 sys.modules；此后测试内的
# 再导入会生成第二个 PIL.Image 实例，而被测模块仍绑定第一个实例——它的 EXTENSION
# 永远拿不到 PngImagePlugin 注册（插件模块已在 sys.modules 中，不会再次执行），
# 于是 save("*.png") 会以 unknown file extension 失败。先导入可避免这种假失败。
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
        package.__name__: package,
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


class SwapPanelButtonIconTests(unittest.TestCase):
    """按钮图标优先级：单按钮图片 → 全局回退 → 备注文字图标 → 默认按钮图。"""

    DEFAULTS = {
        "use_remark_as_icon": True,
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
        "background_opacity": 1.0,
        "background_corner_radius": 24,
        "background_border_color": (0.59, 0.75, 0.94),
        "background_border_width": 3,
    }

    def setUp(self):
        self.module = _load_panel()
        self.panel = self.module.SSMTNode_PostProcess_SwapPanel()
        for name, value in self.DEFAULTS.items():
            setattr(self.panel, name, value)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.res = self.root / "res"
        self.res.mkdir()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.custom = self.root / "custom.png"
        self._write_png(self.custom, (200, 50))
        self._write_png(self.assets / "0.png", (64, 64))

    @staticmethod
    def _write_png(path, size):
        from PIL import Image

        Image.new("RGBA", size, (255, 0, 0, 255)).save(path)

    @staticmethod
    def _button(**kwargs):
        button = {
            "var_name": "$swapkey0",
            "comment": "",
            "option_count": 2,
            "hotkey": "1",
            "image_path": "",
        }
        button.update(kwargs)
        return button

    def _generate(self, index, button):
        return self.panel._ensure_button_image(
            str(self.res), "ns1", index, str(self.assets), button
        )

    def _size(self, path):
        from PIL import Image

        with Image.open(path) as image:
            return image.size

    def _assert_copied(self, generated, source):
        self.assertTrue(generated and Path(generated).is_file())
        self.assertEqual(Path(generated).read_bytes(), Path(source).read_bytes())

    # ---------------------------------------------------------------- 优先级
    def test_per_button_image_wins_over_everything(self):
        self.panel.button_image = str(self.custom)
        generated = self._generate(1, self._button(image_path=str(self.custom), comment="武器切换"))
        self._assert_copied(generated, self.custom)

    def test_global_fallback_used_when_button_has_no_image(self):
        self.panel.button_image = str(self.custom)
        generated = self._generate(2, self._button(comment="武器切换"))
        self._assert_copied(generated, self.custom)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_remark_text_icon_used_when_no_images_configured(self):
        generated = self._generate(3, self._button(comment="武器切换"))
        self.assertTrue(generated and Path(generated).is_file())
        self.assertNotEqual(self._size(generated), DEFAULT_BUTTON_SIZE)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_remark_icon_honours_forced_line_break(self):
        single = self._generate(4, self._button(comment="武器切换"))
        wrapped = self._generate(5, self._button(comment="武器/切换"))
        self.assertGreater(self._size(wrapped)[1], self._size(single)[1])

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_missing_remark_icon_file_falls_back_to_default_button(self):
        generated = self._generate(6, self._button(comment=""))
        self.assertEqual(self._size(generated), DEFAULT_BUTTON_SIZE)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_remark_icon_can_be_disabled(self):
        self.panel.use_remark_as_icon = False
        generated = self._generate(7, self._button(comment="武器切换"))
        self.assertEqual(self._size(generated), DEFAULT_BUTTON_SIZE)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_invalid_per_button_image_falls_back_to_remark_icon(self):
        missing = str(self.root / "not_here.png")
        generated = self._generate(8, self._button(image_path=missing, comment="武器切换"))
        self.assertTrue(generated and Path(generated).is_file())
        self.assertNotEqual(self._size(generated), DEFAULT_BUTTON_SIZE)

    # ------------------------------------------------- 按钮样式（默认按钮图）
    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成按钮图")
    def test_default_button_follows_button_style(self):
        plain = self._generate(10, self._button())
        self.panel.button_bg_color = (1.0, 0.0, 0.0)
        self.panel.button_border_width = 8
        styled = self._generate(11, self._button())
        self.assertEqual(self._size(plain), DEFAULT_BUTTON_SIZE)
        self.assertEqual(self._size(styled), DEFAULT_BUTTON_SIZE)
        self.assertNotEqual(Path(plain).read_bytes(), Path(styled).read_bytes())

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成按钮图")
    def test_default_button_follows_opacity(self):
        opaque = self._generate(12, self._button())
        self.panel.button_opacity = 0.2
        faded = self._generate(13, self._button())
        self.assertLess(self._max_alpha(faded), self._max_alpha(opaque))

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成文字图标")
    def test_text_icon_follows_button_style(self):
        first = self._generate(14, self._button(comment="武器切换"))
        self.panel.button_bg_color = (0.9, 0.1, 0.1)
        self.panel.button_border_color = (0.1, 0.9, 0.1)
        second = self._generate(15, self._button(comment="武器切换"))
        self.assertNotEqual(Path(first).read_bytes(), Path(second).read_bytes())

    # ------------------------------------------------- 面板背景样式
    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成背景图")
    def test_background_corner_radius_cuts_corners(self):
        square = self._background(20, corner_radius=0, border_width=0)
        rounded = self._background(21, corner_radius=50, border_width=0)
        self.assertGreater(self._corner_alpha(square), 0)
        self.assertEqual(self._corner_alpha(rounded), 0)

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成背景图")
    def test_background_border_width_changes_output(self):
        none = self._background(22, corner_radius=24, border_width=0)
        bordered = self._background(23, corner_radius=24, border_width=8)
        self.assertNotEqual(Path(none).read_bytes(), Path(bordered).read_bytes())

    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成背景图")
    def test_background_opacity_scales_alpha(self):
        solid = self._background(24, corner_radius=0, border_width=0, opacity=1.0)
        faded = self._background(25, corner_radius=0, border_width=0, opacity=0.4)
        self.assertGreater(self._corner_alpha(solid), self._corner_alpha(faded))

    def _background(self, index, corner_radius, border_width, opacity=None):
        self.panel.background_corner_radius = corner_radius
        self.panel.background_border_width = border_width
        if opacity is not None:
            self.panel.background_opacity = opacity
        dest = str(self.res / f"swpbg_{index}.png")
        generated = self.panel._generate_background_image(dest, 0.6, 0.75)
        self.assertTrue(generated and Path(generated).is_file())
        return generated

    @staticmethod
    def _corner_alpha(path):
        from PIL import Image

        with Image.open(path) as image:
            return image.convert("RGBA").getpixel((0, 0))[3]

    @staticmethod
    def _max_alpha(path):
        from PIL import Image

        with Image.open(path) as image:
            return max(pixel[3] for pixel in image.convert("RGBA").getdata())

    # ------------------------------------------------- 按钮对齐
    def test_button_align_center_matches_legacy_hardcoded_formula(self):
        """默认 CENTER 必须与旧版硬编码居中完全等价（不影响既有 mod 几何）。"""
        panel_w, grid_w, pad = 1.2, 0.6, 0.02

        for align in ("CENTER", "center", "", None):
            self.assertAlmostEqual(
                self.panel._compute_grid_left(panel_w, grid_w, pad, align),
                (panel_w - grid_w) * 0.5,
                places=9,
            )

    def test_button_align_left_and_right_use_side_padding(self):
        panel_w, grid_w, pad = 1.2, 0.6, 0.02

        self.assertAlmostEqual(
            self.panel._compute_grid_left(panel_w, grid_w, pad, "LEFT"), pad, places=9
        )
        self.assertAlmostEqual(
            self.panel._compute_grid_left(panel_w, grid_w, pad, "RIGHT"),
            panel_w - pad - grid_w,
            places=9,
        )

    def test_button_align_falls_back_to_center_for_unknown_value(self):
        self.assertAlmostEqual(
            self.panel._compute_grid_left(1.0, 0.4, 0.02, "SOMETHING"), 0.3, places=9
        )

    # ------------------------------------------------- 颜色量化
    @unittest.skipUnless(PIL_AVAILABLE, "需要 Pillow 才能生成按钮图")
    def test_default_colors_match_the_pre_refactor_hardcoded_values(self):
        """0.16/0.22/0.32 必须量化成旧硬编码的 (41, 56, 82)。"""
        from PIL import Image

        generated = self._generate(30, self._button())
        with Image.open(generated) as image:
            # 取按钮中心（避开 2px 边框与圆角区域）
            self.assertEqual(image.convert("RGBA").getpixel((192, 32))[:3], (41, 56, 82))


if __name__ == "__main__":
    unittest.main()
