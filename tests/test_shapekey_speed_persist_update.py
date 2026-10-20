# -*- coding: utf-8 -*-
"""形态键扩展节点：自动播放速度相关 persist 变量必须随导出参数更新。

回归背景（用户实测 bug）：
    分组「总细分 / 总时长 / 假设帧率」改大后（例如 4800 细分 / 80s），游戏里播放
    速度与 480 细分 / 8s 的分组完全一样。根因是导出时对这些变量用了
    「``if 变量名 not in const_content`` 才追加」的写法：ini 里已存在
    ``global persist $frameEnd_group1 = 480`` 时，改参数重新导出**不会更新**这一行，
    进度上限仍是 480；而每帧步进 ``$base_step_group{g}`` 是按新的单位速度算的
    （4800/80s/60 = 1，480/8s/60 = 1），于是两组都「每帧推进 1 细分」、都在 480 帧
    内跑完，速度完全一样。

本测试锁定两条修复：
    A. ``[Constants]`` 里同名的 persist 行改为「存在则替换、不存在才追加」，不留重复行；
    B. ``[Present]`` 自动播放块里每帧先做一次非 persist 赋值
       ``$frameEnd_group{g} = <总细分>``，压住 d3dx_user.ini 里旧的 persist 值。
"""
import importlib.util
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_test_shapekey_speed_persist_update_pkg"
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

MASK = module.SSMTNode_PostProcess_ShapeKeyExt


class _Interval:
    def __init__(self, start=0.0, end=100.0, base_step=100):
        self.start = start
        self.end = end
        self.base_step = base_step


def _make_node():
    node = MASK.__new__(MASK)
    node.namespace = "testns"
    node.play_group_entries = []
    return node


def _const_value(lines, var_name):
    """返回 Constants 里该变量的唯一有效声明值；重复声明会直接让测试失败。"""
    pattern = f"global persist {var_name} ="
    matched = [line.strip() for line in lines if line.strip().startswith(pattern)]
    assert len(matched) <= 1, f"{var_name} 出现重复声明: {matched}"
    if not matched:
        return None
    return matched[0].split("=")[-1].strip()


def _live_lines(lines):
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith(";")]


class PersistUpsertTests(unittest.TestCase):
    """修复 A：Constants 里的同名 persist 行必须被新值替换。"""

    def setUp(self):
        self.node = _make_node()

    def _generate(self, sections, frame_count=4800, group_id=1,
                  speed_min=100, speed_max=1000, speed_percent=100):
        self.node._add_auto_playback_logic_for_group(
            sections, group_id, f"$Freq_Group{group_id}", frame_count,
            "FORWARD", speed_min, speed_max,
            [_Interval(0.0, 100.0, 100)], 3, 1.0, True,
            speed_percent=speed_percent,
        )

    def test_stale_frame_end_is_updated_instead_of_skipped(self):
        """复现旧 bug：已有 480 时必须变成 4800，而不是继续保留 480。"""
        sections = OrderedDict()
        sections["[Constants]"] = [
            "global persist $frameEnd_group1 = 480",
            "global persist $speed_percent_group1 = 100",
        ]

        self._generate(sections, frame_count=4800)

        const_lines = sections["[Constants]"]
        self.assertEqual(_const_value(const_lines, "$frameEnd_group1"), "4800")
        self.assertNotIn("global persist $frameEnd_group1 = 480", const_lines)
        self.assertEqual(
            len([l for l in _live_lines(const_lines) if l.startswith("global persist $frameEnd_group1")]),
            1,
        )

    def test_existing_lines_keep_their_position(self):
        """替换而不是"删掉再追加"，避免 Constants 行序来回跳。"""
        sections = OrderedDict()
        sections["[Constants]"] = [
            "global persist $frameEnd_group1 = 480",
            "global persist $other_var = 1",
        ]

        self._generate(sections, frame_count=4800)

        self.assertEqual(sections["[Constants]"][0], "global persist $frameEnd_group1 = 4800")

    def test_speed_percent_min_max_and_enabled_are_upserted(self):
        sections = OrderedDict()
        sections["[Constants]"] = [
            "global persist $speed_percent_group1 = 50",
            "global persist $speed_percent_min_group1 = 100",
            "global persist $speed_percent_max_group1 = 1000",
            "global persist $auto_play_enabled_group1 = 1",
        ]

        self._generate(sections, frame_count=4800, speed_min=150, speed_max=800,
                       speed_percent=50)

        const_lines = sections["[Constants]"]
        self.assertEqual(_const_value(const_lines, "$speed_percent_min_group1"), "150")
        self.assertEqual(_const_value(const_lines, "$speed_percent_max_group1"), "800")
        # speed_percent 也按传入值保留（不能被打回硬编码的 100）
        self.assertEqual(_const_value(const_lines, "$speed_percent_group1"), "50")
        self.assertEqual(_const_value(const_lines, "$auto_play_enabled_group1"), "1")
        for var_name in ("$speed_percent_min_group1", "$speed_percent_max_group1",
                         "$auto_play_enabled_group1"):
            self.assertEqual(
                len([l for l in _live_lines(const_lines)
                     if l.startswith(f"global persist {var_name}")]),
                1,
                f"{var_name} 出现重复声明",
            )

    def test_other_groups_are_not_touched(self):
        sections = OrderedDict()
        sections["[Constants]"] = [
            "global persist $frameEnd_group1 = 480",
            "global persist $frameEnd_group2 = 2400",
            "global persist $speed_percent_min_group2 = 100",
        ]

        self._generate(sections, frame_count=4800, group_id=2, speed_min=200)

        const_lines = sections["[Constants]"]
        self.assertEqual(_const_value(const_lines, "$frameEnd_group1"), "480")
        self.assertEqual(_const_value(const_lines, "$frameEnd_group2"), "4800")
        self.assertEqual(_const_value(const_lines, "$speed_percent_min_group2"), "200")
        # 分组 1 不应被顺手创建出新的声明行
        self.assertIsNone(_const_value(const_lines, "$speed_percent_min_group1"))

    def test_missing_lines_are_still_appended_once(self):
        sections = OrderedDict()
        sections["[Constants]"] = []

        self._generate(sections, frame_count=4800)
        self._generate(sections, frame_count=4800)

        const_lines = sections["[Constants]"]
        self.assertEqual(_const_value(const_lines, "$frameEnd_group1"), "4800")
        self.assertEqual(
            len([l for l in _live_lines(const_lines) if l.startswith("global persist $frameEnd_group1")]),
            1,
        )


class PresentFrameEndOverrideTests(unittest.TestCase):
    """修复 B：Present 里每帧强制覆盖 frameEnd，旧 persist 值压不住。"""

    def setUp(self):
        self.node = _make_node()
        self.sections = OrderedDict()
        self.sections["[Constants]"] = ["global persist $frameEnd_group1 = 480"]
        self.sections["[Present]"] = ["$existing = 1"]
        self.node._add_auto_playback_logic_for_group(
            self.sections, 1, "$Freq_Group1", 4800, "FORWARD", 100, 1000,
            [_Interval(0.0, 100.0, 100)], 3, 1.0, True,
        )
        self.present = [line.strip() for line in self.sections["[Present]"]]

    def _index(self, text):
        for i, line in enumerate(self.present):
            if line == text:
                return i
        self.fail(f"Present 中找不到行: {text}\n实际内容:\n" + "\n".join(self.present))

    def test_frame_end_is_assigned_every_frame_before_norm_frame(self):
        frame_end_idx = self._index("$frameEnd_group1 = 4800")
        norm_idx = self._index("$norm_frame_group1 = $shapekey_frame_group1 / $frameEnd_group1")
        enabled_idx = self._index("if ($auto_play_enabled_group1 == 1)")

        self.assertLess(enabled_idx, frame_end_idx)
        self.assertLess(frame_end_idx, norm_idx)

    def test_override_line_is_not_persist_and_has_no_duplicate(self):
        assignments = [line for line in self.present if line.startswith("$frameEnd_group1 =")]
        self.assertEqual(assignments, ["$frameEnd_group1 = 4800"])
        self.assertNotIn("persist", assignments[0])

    def test_per_frame_state_vars_stay_initialized_once(self):
        """每帧状态变量（$shapekey_frame 等）不能被重复定义或变成 persist。"""
        const_lines = [line.strip() for line in self.sections["[Constants]"]]
        for var_name, expected in (
            ("$shapekey_frame_group1", "global $shapekey_frame_group1 = 0"),
            ("$step_accum_group1", "global $step_accum_group1 = 0"),
            ("$norm_frame_group1", "global $norm_frame_group1 = 0"),
        ):
            declarations = [line for line in const_lines if line.startswith(var_name + " =")]
            self.assertEqual(declarations, [], f"{var_name} 不应在 Constants 里重复定义")
            self.assertEqual(const_lines.count(expected), 1, f"{var_name} 定义缺失或重复")
        self.assertFalse(
            any(line.startswith("global persist $shapekey_frame_group1")
                for line in const_lines),
            "$shapekey_frame_group1 不应是 persist（每帧状态变量）",
        )

    def test_pingpong_group_keeps_single_direction_var(self):
        sections = OrderedDict()
        sections["[Constants]"] = ["global persist $frameEnd_group1 = 480"]
        sections["[Present]"] = []
        self.node._add_auto_playback_logic_for_group(
            sections, 1, "$Freq_Group1", 4800, "PINGPONG", 100, 1000,
            [_Interval(0.0, 100.0, 100)], 3, 1.0, True,
        )

        const_lines = sections["[Constants]"]
        self.assertEqual(_const_value(const_lines, "$frameEnd_group1"), "4800")
        self.assertEqual(
            len([l for l in _live_lines(const_lines)
                 if l.startswith("global $pingpong_dir_group1")]),
            1,
        )


class ExecutePostprocessPersistUpdateTests(unittest.TestCase):
    """端到端口径：直接对一个已存在旧 persist 行的 ini 做导出，值必须被刷新。"""

    INI = """[Constants]
global persist $Freq_xianzhi = 0.0
global persist $frameEnd_group1 = 480
global persist $speed_percent_group1 = 100
global persist $speed_percent_min_group1 = 100
global persist $speed_percent_max_group1 = 1000
global persist $auto_play_enabled_group1 = 1
[Present]
$Freq_xianzhi = 0.5
"""

    def setUp(self):
        import tempfile
        from unittest import mock

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod_dir = Path(self._tmp.name)
        self.ini_path = self.mod_dir / "mod.ini"
        self.ini_path.write_text(self.INI, encoding="utf-8")

        self.node = _make_node()
        self.node.namespace = "itest"
        self.node.last_mod_ini_path = ""
        self.node.create_cumulative_backup = False
        self.node.use_slider_panel = False
        self.node.auto_play_toggle_key = "space"
        self.node.auto_play_key_global = False
        self.node.play_group_entries = [
            types.SimpleNamespace(shape_key_name="闲置", group_index=1)
        ]
        self.node._scan_shapekey_names_from_variable_items = lambda: ["闲置"]
        self.node._scan_shapekey_names_from_classification = lambda: []
        self.node._scan_shapekey_name_to_var_map = lambda: {"闲置": "Freq_xianzhi"}
        self.node._create_cumulative_backup = lambda *_a, **_k: None
        self.node._apply_slider_panel = lambda *_a, **_k: False

        setting = types.SimpleNamespace(
            group_index=1,
            enable_auto_playback=True,
            auto_playback_frame_count=4800,
            auto_playback_duration=80.0,
            playback_fps=60,
            auto_playback_cycle_mode="FORWARD",
            speed_percent=100,
            speed_percent_min=150,
            speed_percent_max=800,
            speed_percent_mode=True,
            speed_intervals=[_Interval(0.0, 100.0, 100)],
            use_image_as_icon=False,
            use_remark_as_icon=False,
            remark="",
            remark_font_size=36,
            remark_font_family="msyh.ttc",
            remark_text_color=(1.0, 1.0, 1.0),
            remark_stroke_width=2,
            remark_stroke_color=(0.0, 0.0, 0.0),
            button_icon_image="",
        )
        self.node.play_group_settings = [setting]

        self._print_patcher = mock.patch("builtins.print", side_effect=lambda *a, **k: None)
        self._print_patcher.start()
        self.addCleanup(self._print_patcher.stop)

    def test_stale_persist_defaults_are_refreshed(self):
        ok = self.node.execute_postprocess(
            str(self.mod_dir), _in_place=True, _ini_path=str(self.ini_path)
        )
        self.assertTrue(ok)
        text = self.ini_path.read_text(encoding="utf-8")
        const_text = text.split("[Present]")[0]

        self.assertIn("global persist $frameEnd_group1 = 4800", const_text)
        self.assertNotIn("global persist $frameEnd_group1 = 480\n", const_text)
        self.assertIn("global persist $speed_percent_min_group1 = 150", const_text)
        self.assertIn("global persist $speed_percent_max_group1 = 800", const_text)
        # Present 里必须有每帧覆盖，位置在 norm_frame 之前
        present = [line.strip() for line in text.split("[Present]")[1].splitlines()]
        self.assertIn("$frameEnd_group1 = 4800", present)
        self.assertLess(
            present.index("$frameEnd_group1 = 4800"),
            next(i for i, line in enumerate(present)
                 if line.startswith("$norm_frame_group1 = $shapekey_frame_group1")),
        )

    def test_refresh_twice_keeps_single_declaration(self):
        for _ in range(2):
            ok = self.node.execute_postprocess(
                str(self.mod_dir), _in_place=True, _ini_path=str(self.ini_path)
            )
            self.assertTrue(ok)
            text = self.ini_path.read_text(encoding="utf-8")
            self.assertEqual(text.count("global persist $frameEnd_group1"), 1)
            self.assertEqual(text.count("global persist $speed_percent_min_group1"), 1)
            self.assertEqual(text.count("global persist $speed_percent_max_group1"), 1)
            self.assertIn("global persist $frameEnd_group1 = 4800", text)
            self.assertIn("$frameEnd_group1 = 4800", text)


if __name__ == "__main__":
    unittest.main()
