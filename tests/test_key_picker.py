# -*- coding: utf-8 -*-
"""形态键扩展「快捷键图形键盘选择器」（ssmt.key_picker）的回归测试。

弹窗本身（bpy.types.Operator.draw / invoke）只能在真机里验证，这里覆盖它的
纯逻辑部分：组合键解析 / 格式化、修饰键多选切换、点击主键替换，以及键盘
布局常量的完整性（否则某个按键会被静默漏掉或写错 VK 名）。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_test_key_picker_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_prop = lambda **_kwargs: None
_install_module(
    "bpy",
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    props=types.SimpleNamespace(
        StringProperty=_prop,
        BoolProperty=_prop,
        IntProperty=_prop,
        FloatProperty=_prop,
        EnumProperty=_prop,
        CollectionProperty=_prop,
        FloatVectorProperty=_prop,
    ),
    data=types.SimpleNamespace(objects={}, node_groups=[]),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
        },
    ),
)

_module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey_ext.py"
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_shapekey_ext", _module_path
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)


class ParseTests(unittest.TestCase):
    def test_parse_existing_defaults(self):
        self.assertEqual(module.key_picker_parse("ctrl home"), (("ctrl",), "home"))
        self.assertEqual(module.key_picker_parse("VK_LBUTTON"), ((), "VK_LBUTTON"))
        self.assertEqual(module.key_picker_parse("home"), ((), "home"))
        self.assertEqual(module.key_picker_parse("space"), ((), "space"))

    def test_parse_is_case_insensitive_and_order_independent(self):
        self.assertEqual(
            module.key_picker_parse("  Shift   CTRL  Home "), (("ctrl", "shift"), "Home")
        )

    def test_parse_empty_and_garbage(self):
        self.assertEqual(module.key_picker_parse(""), ((), ""))
        self.assertEqual(module.key_picker_parse(None), ((), ""))
        self.assertEqual(module.key_picker_parse("   "), ((), ""))


class FormatTests(unittest.TestCase):
    def test_combo_joins_modifiers_in_fixed_order(self):
        self.assertEqual(module.key_picker_format(("shift", "ctrl", "alt"), "home"),
                         "ctrl shift alt home")
        self.assertEqual(module.key_picker_format(("shift", "ctrl"), "home"), "ctrl shift home")
        self.assertEqual(module.key_picker_format(("ctrl",), "home"), "ctrl home")

    def test_plain_keys_are_lowercase(self):
        self.assertEqual(module.key_picker_format((), "UP"), "up")
        self.assertEqual(module.key_picker_format(("ctrl",), "Space"), "ctrl space")

    def test_vk_names_keep_uppercase(self):
        self.assertEqual(module.key_picker_format((), "VK_LBUTTON"), "VK_LBUTTON")
        self.assertEqual(module.key_picker_format(("ctrl",), "vk_xbutton2"), "ctrl VK_XBUTTON2")

    def test_roundtrip_of_existing_defaults(self):
        for raw in ("home", "ctrl home", "up", "space", "VK_LBUTTON"):
            mods, key = module.key_picker_parse(raw)
            self.assertEqual(module.key_picker_format(mods, key), raw)


class ModifierToggleTests(unittest.TestCase):
    def test_toggle_adds_and_removes(self):
        mods = ()
        mods = module.key_picker_toggle_modifier(mods, "ctrl")
        self.assertEqual(mods, ("ctrl",))
        mods = module.key_picker_toggle_modifier(mods, "shift")
        self.assertEqual(mods, ("ctrl", "shift"))
        mods = module.key_picker_toggle_modifier(mods, "ctrl")
        self.assertEqual(mods, ("shift",))
        mods = module.key_picker_toggle_modifier(mods, "shift")
        self.assertEqual(mods, ())

    def test_three_modifiers_multiselect_keep_fixed_order(self):
        mods = ()
        for token in ("alt", "shift", "ctrl"):
            mods, _key = module.key_picker_apply_click(mods, "", token)
        self.assertEqual(mods, ("ctrl", "shift", "alt"))

    def test_unknown_token_is_not_a_modifier(self):
        self.assertFalse(module.key_picker_is_modifier("win"))
        self.assertEqual(module.key_picker_toggle_modifier(("ctrl",), "win"), ("ctrl",))


class MainKeyTests(unittest.TestCase):
    def test_clicking_main_key_replaces_current_main_key(self):
        mods, key = module.key_picker_apply_click(("ctrl",), "home", "up")
        self.assertEqual((mods, key), (("ctrl",), "up"))
        mods, key = module.key_picker_apply_click(mods, key, "VK_LBUTTON")
        self.assertEqual(module.key_picker_format(mods, key), "ctrl VK_LBUTTON")

    def test_clicking_modifier_keeps_main_key(self):
        mods, key = module.key_picker_apply_click((), "home", "shift")
        self.assertEqual((mods, key), (("shift",), "home"))

    def test_clear_action_state(self):
        # 「清除」= 清空主键与修饰键，写回空字符串
        self.assertEqual(module.key_picker_format((), ""), "")
        self.assertEqual(module.key_picker_display((), ""), "（未设置）")


def _row_tokens(row_def):
    """一行里真正可点的 token（跳过方向键区的空白占位键）。"""
    return [token for _label, token, _width in row_def if token]


class KeyboardLayoutTests(unittest.TestCase):
    def test_every_row_is_non_empty(self):
        rows = list(module.KEY_PICKER_MAIN_ROWS) + list(module.KEY_PICKER_EXTRA_ROWS)
        for row in rows:
            self.assertTrue(row, "键盘布局存在空行")
            for item in row:
                self.assertEqual(len(item), 3, f"按键项应为 (显示名, token, 键宽): {item}")
                label, token, width = item
                self.assertGreater(width, 0, f"键宽必须为正: {item}")
                # 允许 (\"\", \"\") 占位键，但不允许只写一半
                self.assertEqual(bool(label), bool(token), f"按键项显示名与 token 不配对: {item}")

    def test_no_duplicate_tokens_except_modifiers(self):
        # 左右 Shift/Ctrl/Alt 共用同一 token 属正常重复，只对非修饰键查重
        tokens = [t for t in module.key_picker_all_tokens() if not module.key_picker_is_modifier(t)]
        duplicates = sorted({token for token in tokens if tokens.count(token) > 1})
        self.assertEqual(duplicates, [], f"重复按键: {duplicates}")
        modifier_tokens = [t for t in module.key_picker_all_tokens()
                           if module.key_picker_is_modifier(t)]
        for modifier in module.KEY_PICKER_MODIFIERS:
            self.assertGreaterEqual(
                modifier_tokens.count(modifier), 1, f"键盘布局缺少修饰键: {modifier}"
            )
        # 左右各一个：Ctrl/Shift 在布局里都出现两次；右侧 Alt 位置是 AltGr(VK_RMENU)，故 alt 只出现一次
        for modifier in ("ctrl", "shift"):
            self.assertEqual(modifier_tokens.count(modifier), 2, f"{modifier} 应左右各一个")
        self.assertEqual(modifier_tokens.count("alt"), 1, "底行左侧应有一个 Alt")

    def test_required_keys_are_present(self):
        tokens = set(module.key_picker_all_tokens())
        required = {
            "esc", "tab", "enter", "space", "backspace", "capslock",
            "ctrl", "shift", "alt", "up", "down", "left", "right",
            "home", "end", "pageup", "pagedown", "insert", "delete",
            "VK_OEM_3", "VK_OEM_MINUS", "VK_OEM_PLUS", "VK_OEM_4", "VK_OEM_6",
            "VK_OEM_5", "VK_OEM_1", "VK_OEM_7", "VK_OEM_COMMA", "VK_OEM_PERIOD",
            "VK_OEM_2", "VK_LBUTTON", "VK_RBUTTON", "VK_MBUTTON",
            "VK_XBUTTON1", "VK_XBUTTON2", "MOUSE_WHEEL_UP", "MOUSE_WHEEL_DOWN",
            "VK_LWIN", "VK_RWIN", "VK_RMENU", "VK_APPS",
            "VK_NUMLOCK", "VK_MULTIPLY", "VK_ADD", "VK_SUBTRACT", "VK_DIVIDE",
            "VK_DECIMAL", "VK_SNAPSHOT", "VK_SCROLL", "VK_PAUSE",
        }
        required |= {str(digit) for digit in range(10)}
        required |= {chr(code) for code in range(ord("a"), ord("z") + 1)}
        required |= {f"f{index}" for index in range(1, 13)}
        required |= {f"VK_NUMPAD{digit}" for digit in range(10)}
        self.assertEqual(sorted(required - tokens), [], "键盘布局缺少按键")

    def test_main_rows_follow_real_keyboard_order(self):
        rows = module.KEY_PICKER_MAIN_ROWS
        self.assertEqual(len(rows), 6, "主键区应为 6 行")
        function_row = _row_tokens(rows[0])
        self.assertEqual(function_row[0], "esc", "行1 首键应为 Esc")
        self.assertEqual(function_row[-1], "f12", "行1 末键应为 F12")
        for token in ("f1", "f12"):
            self.assertIn(token, function_row)

        number_row = _row_tokens(rows[1])
        self.assertEqual(number_row[0], "VK_OEM_3", "数字行首键应为 `")
        self.assertEqual(number_row[1], "1")
        for token in ("1", "0", "backspace"):
            self.assertIn(token, number_row)
        self.assertEqual(number_row[-1], "backspace", "数字行末键应为 Backspace")

        tab_row = _row_tokens(rows[2])
        self.assertEqual(tab_row[0], "tab")
        for token in ("q", "VK_OEM_5"):
            self.assertIn(token, tab_row)
        self.assertEqual(tab_row[-1], "VK_OEM_5", "Tab 行末键应为 \\")

        caps_row = _row_tokens(rows[3])
        self.assertEqual(caps_row[0], "capslock")
        for token in ("a", "enter"):
            self.assertIn(token, caps_row)
        self.assertEqual(caps_row[-1], "enter", "Caps 行末键应为 Enter")

        shift_row = _row_tokens(rows[4])
        self.assertEqual(shift_row[0], "shift", "Shift 行首键应为左 Shift")
        self.assertEqual(shift_row[-1], "shift", "Shift 行末键应为右 Shift")

        bottom_row = _row_tokens(rows[5])
        for token in ("ctrl", "VK_LWIN", "alt", "space", "VK_RMENU", "VK_RWIN", "VK_APPS"):
            self.assertIn(token, bottom_row)
        self.assertEqual(bottom_row[0], "ctrl")
        self.assertEqual(bottom_row[-1], "ctrl", "底行末键应为右 Ctrl")

    def test_key_widths_match_real_keyboard(self):
        rows = module.KEY_PICKER_MAIN_ROWS
        self.assertEqual(rows[1][-1][2], 2.0, "Backspace 应为 2u")
        self.assertEqual(rows[2][0][2], 1.5, "Tab 应为 1.5u")
        self.assertEqual(rows[2][-1][2], 1.5, "反斜杠应为 1.5u")
        self.assertEqual(rows[3][0][2], 1.75, "CapsLock 应为 1.75u")
        self.assertEqual(rows[3][-1][2], 2.25, "Enter 应为 2.25u")
        self.assertEqual(rows[4][0][2], 2.25, "左 Shift 应为 2.25u")
        self.assertEqual(rows[4][-1][2], 2.25, "右 Shift 应为 2.25u")
        space = [item for item in rows[5] if item[1] == "space"][0]
        self.assertEqual(space[2], 6.25, "Space 应为 6.25u")

    def test_editing_and_arrow_blocks(self):
        self.assertEqual(_row_tokens(module.KEY_PICKER_EDITING_ROWS[0]),
                         ["insert", "home", "pageup"])
        self.assertEqual(_row_tokens(module.KEY_PICKER_EDITING_ROWS[1]),
                         ["delete", "end", "pagedown"])
        arrows = _row_tokens(module.KEY_PICKER_ARROW_ROWS[0])
        arrows += _row_tokens(module.KEY_PICKER_ARROW_ROWS[1])
        self.assertEqual(arrows, ["up", "left", "down", "right"])

    def test_numpad_rows_contain_digits_and_decimal(self):
        rows = module.KEY_PICKER_NUMPAD_ROWS
        flat = [token for row in rows for token in _row_tokens(row)]
        for token in ("VK_NUMLOCK", "VK_MULTIPLY", "VK_ADD", "VK_SUBTRACT",
                      "VK_DIVIDE", "VK_SNAPSHOT", "VK_SCROLL", "VK_PAUSE"):
            self.assertIn(token, flat)
        for digit in range(10):
            self.assertIn(f"VK_NUMPAD{digit}", flat)
        self.assertIn("VK_DECIMAL", flat)
        # 0 与小数点必须在同一行（真实小键盘最后一排）
        zero_row = [row for row in rows if "VK_NUMPAD0" in _row_tokens(row)][0]
        self.assertIn("VK_DECIMAL", _row_tokens(zero_row))
        # 小键盘 0 应为 2u 宽
        zero = [item for item in zero_row if item[1] == "VK_NUMPAD0"][0]
        self.assertEqual(zero[2], 2.0)

    def test_mouse_row_tokens(self):
        tokens = _row_tokens(module.KEY_PICKER_MOUSE_ROW)
        for token in ("VK_LBUTTON", "VK_RBUTTON", "VK_MBUTTON", "VK_XBUTTON1",
                      "VK_XBUTTON2", "MOUSE_WHEEL_UP", "MOUSE_WHEEL_DOWN"):
            self.assertIn(token, tokens)

    def test_extra_rows_cover_every_block(self):
        flat = set(token for row in module.KEY_PICKER_EXTRA_ROWS for token in _row_tokens(row))
        blocks = (module.KEY_PICKER_EDITING_ROWS + module.KEY_PICKER_ARROW_ROWS
                  + module.KEY_PICKER_NUMPAD_ROWS + (module.KEY_PICKER_MOUSE_ROW,))
        for row in blocks:
            for token in _row_tokens(row):
                self.assertIn(token, flat, f"KEY_PICKER_EXTRA_ROWS 未包含 {token}")

    def test_every_layout_token_is_accepted_by_format(self):
        for token in module.key_picker_all_tokens():
            if module.key_picker_is_modifier(token):
                continue  # 修饰键不是主键，单独由 ModifierToggleTests 覆盖
            text = module.key_picker_format(("ctrl",), token)
            self.assertEqual(module.key_picker_parse(text)[1].lower(), token.lower())


if __name__ == "__main__":
    unittest.main()
