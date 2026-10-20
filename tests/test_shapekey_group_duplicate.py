# -*- coding: utf-8 -*-
"""形态键扩展节点：分组「复制」功能测试。

覆盖复制分组的可判定行为：

1. 新分组编号 = 现有最大 group_index + 1；
2. 分组全部设置字段逐一复制（备注原样、不加后缀）；
3. speed_intervals 数量与每条 start / end / base_step 一致；
4. 不复制形态键成员归属：原分组成员保持不动，新分组没有成员；
5. active_group_index 切到新分组；
6. 目标分组不存在时取消并提示。

模块加载方式与 tests/test_shapekey_ext_unassigned_mask.py 一致（纯 Python fake bpy）。
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


PKG = "_test_shapekey_group_duplicate_pkg"
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
    SSMTNode_PostProcess_Base=type("_FakePostProcessBase", (object,), {}),
)

_module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey_ext.py"
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_shapekey_ext", _module_path
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)

DUPLICATE = module.SSMT_OT_ShapeKeyGroupDuplicate

# 与验收标准逐字对应的字段清单（故意独立于算子里的 _CLONED_FIELDS，防止漏复制）
REQUIRED_FIELDS = (
    "group_mode",
    "enable_auto_playback",
    "auto_playback_frame_count",
    "auto_playback_step_frames",
    "auto_playback_cycle_mode",
    "auto_playback_duration",
    "playback_fps",
    "speed_percent_min",
    "speed_percent_max",
    "speed_percent",
    "speed_percent_mode",
    "max_step_unroll",
    "remark",
    "button_icon_image",
    "use_remark_as_icon",
    "use_image_as_icon",
    "expanded",
    "remark_font_family",
    "remark_font_size",
    "remark_text_color",
    "remark_stroke_width",
    "remark_stroke_color",
    "active_interval_index",
)

DEFAULTS = {
    "group_mode": 'SYNC',
    "enable_auto_playback": False,
    "auto_playback_frame_count": 30,
    "auto_playback_step_frames": 5,
    "auto_playback_cycle_mode": 'FORWARD',
    "auto_playback_duration": 2.0,
    "playback_fps": 60,
    "speed_percent_min": 100,
    "speed_percent_max": 1000,
    "speed_percent": 100,
    "speed_percent_mode": False,
    "max_step_unroll": 100,
    "remark": "",
    "button_icon_image": "",
    "use_remark_as_icon": True,
    "use_image_as_icon": False,
    "expanded": True,
    "remark_font_family": 'msyh.ttc',
    "remark_font_size": 36,
    "remark_text_color": (1.0, 1.0, 1.0),
    "remark_stroke_width": 4,
    "remark_stroke_color": (0.0, 0.0, 0.0),
    "active_interval_index": 0,
}


class _Interval:
    def __init__(self, start=0.0, end=50.0, base_step=100):
        self.start = start
        self.end = end
        self.base_step = base_step


class _IntervalCollection(list):
    def add(self):
        item = _Interval()
        self.append(item)
        return item


class _Setting:
    def __init__(self, group_index=1, **kwargs):
        self.group_index = group_index
        for field, default in DEFAULTS.items():
            setattr(self, field, kwargs.get(field, default))
        self.speed_intervals = _IntervalCollection(kwargs.get("speed_intervals", []))


class _SettingsCollection(list):
    def add(self):
        item = _Setting()
        self.append(item)
        return item


class _Log:
    def __init__(self):
        self.messages = []

    def __call__(self, level, message):
        self.messages.append((set(level), message))

    @property
    def text(self):
        return "\n".join(message for _level, message in self.messages)


def _make_node(settings, entries, active_group_index=1):
    node = types.SimpleNamespace(
        bl_idname='SSMTNode_PostProcess_ShapeKeyExt',
        play_group_settings=_SettingsCollection(settings),
        play_group_entries=entries,
        active_group_index=active_group_index,
    )
    return node


def _make_context(node):
    return types.SimpleNamespace(
        space_data=types.SimpleNamespace(
            type='NODE_EDITOR',
            edit_tree=types.SimpleNamespace(nodes={"TestNode": node}),
        )
    )


def _run_operator(node, target_group_index):
    op = DUPLICATE.__new__(DUPLICATE)
    op.node_name = "TestNode"
    op.target_group_index = target_group_index
    log = _Log()
    op.report = log
    result = op.execute(_make_context(node))
    return result, log


def _source_setting():
    """构造一个每个字段都偏离默认值的源分组。"""
    intervals = [
        _Interval(start=0.0, end=30.0, base_step=50),
        _Interval(start=30.0, end=100.0, base_step=200),
    ]
    return _Setting(
        group_index=2,
        group_mode='SEQUENCE',
        enable_auto_playback=True,
        auto_playback_frame_count=240,
        auto_playback_step_frames=7,
        auto_playback_cycle_mode='PINGPONG',
        auto_playback_duration=3.5,
        playback_fps=144,
        speed_percent_min=25,
        speed_percent_max=400,
        speed_percent=150,
        speed_percent_mode=True,
        max_step_unroll=250,
        remark="摇摆/动作",
        button_icon_image="D:/icons/sway.png",
        use_remark_as_icon=False,
        use_image_as_icon=True,
        expanded=False,
        remark_font_family='simhei.ttf',
        remark_font_size=48,
        remark_text_color=(0.2, 0.4, 0.6),
        remark_stroke_width=8,
        remark_stroke_color=(0.1, 0.1, 0.1),
        active_interval_index=1,
        speed_intervals=intervals,
    )


class DuplicateGroupTests(unittest.TestCase):
    def setUp(self):
        self.source = _source_setting()
        self.other = _Setting(group_index=4, remark="另一个分组")
        self.entries = [
            types.SimpleNamespace(shape_key_name="摇摆", group_index=2),
            types.SimpleNamespace(shape_key_name="闲置", group_index=4),
        ]
        self.node = _make_node([self.source, self.other], self.entries, active_group_index=2)
        self.result, self.log = _run_operator(self.node, 2)

    def test_duplicate_finishes_and_uses_max_index_plus_one(self):
        self.assertEqual(self.result, {'FINISHED'})
        self.assertEqual(len(self.node.play_group_settings), 3)
        new_group = self.node.play_group_settings[-1]
        # 现有最大编号是 4（不是被复制的 2），新分组必须是 5
        self.assertEqual(new_group.group_index, 5)
        self.assertIn("已复制分组 2 → 新分组 5", self.log.text)
        self.assertTrue(any('INFO' in level for level, _msg in self.log.messages))

    def test_every_field_is_copied_verbatim(self):
        new_group = self.node.play_group_settings[-1]
        # expanded 按验收要求复制后统一置 True，其余字段必须原样相等
        for field in REQUIRED_FIELDS:
            if field == "expanded":
                continue
            self.assertEqual(
                getattr(new_group, field),
                getattr(self.source, field),
                f"字段 {field} 未被原样复制",
            )
        # 备注原样复制，不加后缀
        self.assertEqual(new_group.remark, "摇摆/动作")
        # 源分组本来是折叠的，复制后展开，便于看到新分组
        self.assertFalse(self.source.expanded)
        self.assertTrue(new_group.expanded)

    def test_speed_intervals_are_copied_one_by_one(self):
        new_group = self.node.play_group_settings[-1]
        self.assertEqual(len(new_group.speed_intervals), 2)
        for copied, original in zip(new_group.speed_intervals, self.source.speed_intervals):
            self.assertEqual(copied.start, original.start)
            self.assertEqual(copied.end, original.end)
            self.assertEqual(copied.base_step, original.base_step)

    def test_group_membership_is_not_copied(self):
        new_group = self.node.play_group_settings[-1]
        # 原分组成员保持不动
        self.assertEqual(self.entries[0].group_index, 2)
        self.assertEqual(self.entries[1].group_index, 4)
        # 新分组不带任何成员
        self.assertFalse(any(e.group_index == new_group.group_index for e in self.entries))

    def test_active_group_switches_to_new_group(self):
        self.assertEqual(self.node.active_group_index, 5)

    def test_unknown_source_group_cancels_with_warning(self):
        node = _make_node([_Setting(group_index=1)], [], active_group_index=1)
        result, log = _run_operator(node, 9)
        self.assertEqual(result, {'CANCELLED'})
        self.assertEqual(len(node.play_group_settings), 1)
        self.assertIn("找不到要复制的分组 9", log.text)


if __name__ == "__main__":
    unittest.main()
