# -*- coding: utf-8 -*-
"""物体切换面板列表去重（按备注名合并控件）。

锁住 ``_refresh_entries`` 的合并规则：blueprint 里多个 ObjectSwap 节点的
``comment`` 相同时，即使 ``custom_var_name`` 不同也合并成一条（``variables``
用 ``|`` 记录全部变量）；备注为空时只按变量名合并，空备注条目互不合并；
备注不同则各自保留。合并时 option_count 取最大值、hotkey 取第一个非空值、
node_name 保留第一条。

同时锁住 INI 分支（同一变量重复只保留一条、可按备注并入已有条目）和导出用的
``_build_button_list``（同备注多变量只生成一个按钮，``var_names``/``option_counts``
一一对应；不同备注各自一个按钮；同快捷键合并后不截断多变量）。
"""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PKG = "_test_swap_panel_entries_dedupe"
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None

# 与 test_swap_panel_attach_mode.py 同样的理由：顶层先把 PIL 导入 sys.modules，
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
    stubs = {
        "bpy": fake_bpy,
        "bpy.props": fake_bpy.props,
        PKG + ".blueprint": package,
        base.__name__: base,
    }
    with mock.patch.dict(sys.modules, stubs):
        return _load_module(
            PKG + ".blueprint.node_postprocess_swap_panel",
            ROOT / "blueprint" / "node_postprocess_swap_panel.py",
        )


class _EntryCollection(list):
    """模拟 bpy CollectionProperty：``add()`` 追加一个条目对象并返回它。"""

    def add(self):
        entry = types.SimpleNamespace(
            variable_name="", variables="", comment="", option_count=2,
            hotkey="", node_name="",
        )
        self.append(entry)
        return entry


def _swap_node(var="swapkey3", comment="口罩", slots=2, hotkey="", name="ObjectSwap"):
    return types.SimpleNamespace(
        custom_var_name=var,
        comment=comment,
        input_slot_count=slots,
        hotkey=hotkey,
        name=name,
    )


class EntriesDedupeTestCase(unittest.TestCase):
    def setUp(self):
        self.module = _load_panel()
        self.panel = self.module.SSMTNode_PostProcess_SwapPanel()
        self.panel.namespace = "ns_dedupe"
        self.panel.ini_file_path = ""
        self.panel.swap_panel_entries = _EntryCollection()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    # ------------------------------------------------------------------ 工具
    def _refresh(self, nodes, ini=None):
        """驱动 _refresh_entries；贴图/INI/按钮图为空，只留下物体切换分支。"""
        ini_path = ""
        if ini:
            path = self.root / "Mod.ini"
            path.write_text("\n".join(ini) + "\n", encoding="utf-8")
            ini_path = str(path)
        self.panel.ini_file_path = ini_path
        with mock.patch.object(
            self.module, "VARIABLE_REGISTRY_AVAILABLE", True
        ), mock.patch.object(
            self.module, "get_node_variable_name",
            side_effect=lambda node: node.custom_var_name,
            create=True,
        ), mock.patch.object(
            self.panel, "_collect_swap_nodes", return_value=list(nodes)
        ), mock.patch.object(
            self.panel, "_collect_diffuse_groups", return_value=[]
        ), mock.patch.object(
            self.panel, "_collect_custom_material_switch_groups", return_value=[]
        ), mock.patch.object(
            self.panel, "_refresh_button_image_entries"
        ):
            return self.panel._refresh_entries()

    def _build_buttons(self, nodes, ini_swaps=None):
        """驱动 _build_button_list；INI / 贴图 / 材质 / 图片均为空。"""
        self.panel.ini_file_path = ""
        with mock.patch.object(
            self.module, "VARIABLE_REGISTRY_AVAILABLE", True
        ), mock.patch.object(
            self.module, "get_node_variable_name",
            side_effect=lambda node: node.custom_var_name,
            create=True,
        ), mock.patch.object(
            self.panel, "_collect_swap_nodes", return_value=list(nodes)
        ), mock.patch.object(
            self.panel, "_collect_diffuse_groups", return_value=[]
        ), mock.patch.object(
            self.panel, "_collect_custom_material_switch_groups", return_value=[]
        ), mock.patch.object(
            self.panel, "_parse_ini_key_swaps", return_value=list(ini_swaps or [])
        ), mock.patch.object(
            self.panel, "_button_image_for_source", return_value=""
        ):
            return self.panel._build_button_list("")

    def _ini_swaps(self, *swaps):
        return list(swaps)

    def _parse_ini(self, *swaps):
        return mock.patch.object(
            self.panel, "_parse_ini_key_swaps", return_value=self._ini_swaps(*swaps)
        )


class BlueprintMergeTests(EntriesDedupeTestCase):
    def test_same_comment_different_variables_merged_into_one(self):
        """用户现场：3 个 ObjectSwap 备注都是「口罩」，变量 swapkey3/4/5 → 1 条。"""
        nodes = [
            _swap_node(var="swapkey3", slots=2, name="SwapA"),
            _swap_node(var="swapkey4", slots=3, name="SwapB"),
            _swap_node(var="swapkey5", slots=4, name="SwapC"),
        ]
        count = self._refresh(nodes)
        self.assertEqual(count, 1)
        self.assertEqual(len(self.panel.swap_panel_entries), 1)
        entry = self.panel.swap_panel_entries[0]
        self.assertEqual(entry.variables, "swapkey3|swapkey4|swapkey5")
        self.assertEqual(entry.variable_name, "swapkey3", "variable_name 保留第一个变量")
        self.assertEqual(entry.comment, "口罩")
        self.assertEqual(entry.option_count, 4, "option_count 取最大值")
        self.assertEqual(entry.node_name, "SwapA", "node_name 必须保留第一个")

    def test_same_variable_and_comment_merged_into_one(self):
        nodes = [
            _swap_node(name="SwapA"),
            _swap_node(name="SwapB"),
            _swap_node(name="SwapC"),
        ]
        count = self._refresh(nodes)
        self.assertEqual(count, 1)
        self.assertEqual(len(self.panel.swap_panel_entries), 1)
        entry = self.panel.swap_panel_entries[0]
        self.assertEqual(entry.variable_name, "swapkey3")
        self.assertEqual(entry.variables, "swapkey3")
        self.assertEqual(entry.comment, "口罩")
        self.assertEqual(entry.node_name, "SwapA", "node_name 必须保留第一个")

    def test_empty_comment_different_variables_not_merged(self):
        nodes = [
            _swap_node(var="swapkey3", comment=""),
            _swap_node(var="swapkey4", comment=""),
        ]
        self._refresh(nodes)
        self.assertEqual(len(self.panel.swap_panel_entries), 2)
        self.assertEqual(
            [e.variables for e in self.panel.swap_panel_entries],
            ["swapkey3", "swapkey4"],
        )

    def test_empty_comment_same_variable_merged(self):
        nodes = [
            _swap_node(var="swapkey3", comment="", slots=2, name="First"),
            _swap_node(var="swapkey3", comment="", slots=3, name="Second"),
        ]
        self._refresh(nodes)
        self.assertEqual(len(self.panel.swap_panel_entries), 1)
        entry = self.panel.swap_panel_entries[0]
        self.assertEqual(entry.variables, "swapkey3")
        self.assertEqual(entry.option_count, 3)

    def test_different_comment_not_merged(self):
        nodes = [
            _swap_node(var="swapkey3", comment="口罩"),
            _swap_node(var="swapkey4", comment="帽子"),
        ]
        self._refresh(nodes)
        self.assertEqual(len(self.panel.swap_panel_entries), 2)
        self.assertEqual(
            [e.comment for e in self.panel.swap_panel_entries], ["口罩", "帽子"]
        )

    def test_same_variable_different_comment_kept_separately(self):
        nodes = [_swap_node(comment="口罩"), _swap_node(comment="帽子")]
        self._refresh(nodes)
        self.assertEqual(len(self.panel.swap_panel_entries), 2)
        self.assertEqual(
            [e.comment for e in self.panel.swap_panel_entries], ["口罩", "帽子"]
        )

    def test_merge_takes_max_option_count_and_first_non_empty_hotkey(self):
        nodes = [
            _swap_node(slots=2, hotkey="", name="First"),
            _swap_node(slots=3, hotkey="home", name="Second"),
            _swap_node(slots=5, hotkey="end", name="Third"),
        ]
        self._refresh(nodes)
        entry = self.panel.swap_panel_entries[0]
        self.assertEqual(entry.option_count, 5, "option_count 取各组最大值")
        self.assertEqual(entry.hotkey, "home", "hotkey 取第一个非空值")
        self.assertEqual(entry.node_name, "First")
        self.assertEqual(len(self.panel.swap_panel_entries), 1)


class IniMergeTests(EntriesDedupeTestCase):
    def test_duplicate_variable_in_ini_added_once(self):
        ini = ["[KeySwap_1]", "[KeySwap_2]"]
        swaps = [
            {"var_name": "swapkey3", "comment": "口罩", "option_count": 2, "key": "home"},
            {"var_name": "swapkey3", "comment": "口罩", "option_count": 4, "key": "end"},
        ]
        with self._parse_ini(*swaps):
            self._refresh([], ini=ini)
        self.assertEqual(len(self.panel.swap_panel_entries), 1)
        self.assertEqual(self.panel.swap_panel_entries[0].option_count, 4)

    def test_ini_merges_into_blueprint_entry_and_updates_idx_map(self):
        swaps = [
            {"var_name": "swapkey3", "comment": "口罩", "option_count": 3, "key": "home"},
        ]
        with self._parse_ini(*swaps):
            self._refresh([_swap_node()], ini=["[KeySwap_1]"])
        self.assertEqual(len(self.panel.swap_panel_entries), 1)
        entry = self.panel.swap_panel_entries[0]
        self.assertEqual(entry.option_count, 3)
        self.assertEqual(entry.hotkey, "home")

    def test_ini_same_comment_different_variable_merged(self):
        node = _swap_node(var="swapkey4", comment="口罩")
        swaps = [
            {"var_name": "swapkey3", "comment": "口罩", "option_count": 5, "key": "end"},
        ]
        with self._parse_ini(*swaps):
            self._refresh([node], ini=["[KeySwap_1]"])
        self.assertEqual(len(self.panel.swap_panel_entries), 1)
        entry = self.panel.swap_panel_entries[0]
        self.assertEqual(entry.variables, "swapkey4|swapkey3")
        self.assertEqual(entry.option_count, 5)


class ButtonListTests(EntriesDedupeTestCase):
    def test_same_comment_different_variables_one_button(self):
        nodes = [
            _swap_node(var="swapkey3", slots=2),
            _swap_node(var="swapkey4", slots=3),
            _swap_node(var="swapkey5", slots=4),
        ]
        buttons = self._build_buttons(nodes)
        self.assertEqual(len(buttons), 1)
        button = buttons[0]
        self.assertEqual(button["var_names"], ["swapkey3", "swapkey4", "swapkey5"])
        self.assertEqual(button["option_counts"], [2, 3, 4])
        self.assertEqual(button["comment"], "口罩")

    def test_different_comment_two_buttons(self):
        nodes = [
            _swap_node(var="swapkey3", comment="口罩"),
            _swap_node(var="swapkey4", comment="帽子"),
        ]
        buttons = self._build_buttons(nodes)
        self.assertEqual(len(buttons), 2)
        self.assertEqual(
            [button["var_names"] for button in buttons],
            [["swapkey3"], ["swapkey4"]],
        )

    def test_same_variable_same_comment_not_duplicated(self):
        nodes = [
            _swap_node(var="swapkey3", slots=2),
            _swap_node(var="swapkey3", slots=3),
        ]
        buttons = self._build_buttons(nodes)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["var_names"], ["swapkey3"])
        self.assertEqual(buttons[0]["option_counts"], [3], "同变量重复时选项数取大值")

    def test_same_hotkey_merge_keeps_all_variables(self):
        nodes = [
            _swap_node(var="swapkey3", comment="口罩", hotkey="home"),
            _swap_node(var="swapkey4", comment="帽子", hotkey="home"),
        ]
        buttons = self._build_buttons(nodes)
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["var_names"], ["swapkey3", "swapkey4"])
        self.assertEqual(buttons[0]["option_counts"], [2, 2])


if __name__ == "__main__":
    unittest.main()
