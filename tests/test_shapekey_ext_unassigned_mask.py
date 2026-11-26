# -*- coding: utf-8 -*-
"""形态键扩展节点：未分配屏蔽 / 单位速度 / 变速区间语义 的回归测试。

覆盖三类曾经出问题、且很容易再次被改坏的行为：

1. 屏蔽与还原必须**按身份配对**、且逐行可逆（含缩进）。变量名会被刷新
   （装 pypinyin 后 uXXXX → 拼音、用户手改「导出变量」），此时旧名的屏蔽行
   必须继续屏蔽，不能被无条件还原成有效行。
2. 被屏蔽的 Shader 槽位要补一行**有效**的显式归零：IniParams 是粘滞的，
   注释掉原行并不等于 Shader 读到 0；同槽位还有拖拽交互 / UV 偏移在写。
3. 单位速度必须兑现「总时长」（允许小数步进，不能钳成 ≥1）；升级前保存的
   老工程（speed_percent_mode=False）必须与升级前的步进值完全一致。
"""
import importlib.util
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_test_shapekey_ext_unassigned_mask_pkg"
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
            # 集成测试要真的读写 ini：_read_ini_to_ordered_dict 依赖这两个切分方法
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
        },
    ),
)

_module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey_ext.py"


def _load_real_common_module(alias):
    """把仓库里真实的 common/<name>.py 挂到假包前缀下（节点模块用相对导入取它）。"""
    file_path = Path(__file__).resolve().parents[1] / "common" / f"{alias.rsplit('.', 1)[-1]}.py"
    spec = importlib.util.spec_from_file_location(alias, file_path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[alias] = loaded
    spec.loader.exec_module(loaded)
    return loaded


_load_real_common_module(f"{PKG}.common.shapekey_weight_range")
_install_module(
    f"{PKG}.common.key_type",
    one_shot_key_type=lambda: "press",
)

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


class _Setting:
    """ShapeKeyPlayGroupSettings 的最小替身。"""

    def __init__(self, **kwargs):
        self.auto_playback_frame_count = kwargs.get("auto_playback_frame_count", 30)
        self.auto_playback_duration = kwargs.get("auto_playback_duration", 2.0)
        self.playback_fps = kwargs.get("playback_fps", 60)
        self.speed_percent = kwargs.get("speed_percent", 100)
        self.speed_percent_max = kwargs.get("speed_percent_max", 1000)
        self.speed_percent_min = kwargs.get("speed_percent_min", 100)
        self.speed_percent_mode = kwargs.get("speed_percent_mode", True)
        self.speed_intervals = kwargs.get("speed_intervals", [])
        self.enable_auto_playback = kwargs.get("enable_auto_playback", True)


def _make_node():
    node = module.SSMTNode_PostProcess_ShapeKeyExt.__new__(
        module.SSMTNode_PostProcess_ShapeKeyExt
    )
    node.namespace = "testns"
    node.play_group_entries = []
    return node


class UnassignedMaskTests(unittest.TestCase):
    """未分配形态键的屏蔽与按身份还原。"""

    def setUp(self):
        self.node = _make_node()

    @staticmethod
    def _sections():
        sections = OrderedDict()
        sections["[Constants]"] = [
            "; 控制形态键 '摇摆' 的强度",
            "global persist $Freq_yaobai_001 = 0.0",
            "global persist $MyShape = 1.0",
            "global persist $Freq_Group1 = 0.0",
        ]
        sections["[Present]"] = [
            "    $Freq_yaobai_001 = $Freq_Group1",
            "if ($ui_active == 1)",
            "    $Freq_yaobai_001 = $param0",
            "    $MyShape = $param1",
            "endif",
        ]
        sections["[CustomShader_ab12cd34_Anim]"] = [
            "x100 = $Freq_yaobai_001",
            "x101 = $MyShape",
            "x102 = $Freq_Group1",
        ]
        return sections

    def test_mask_comments_all_three_layers_and_unmask_is_line_exact(self):
        sections = self._sections()
        original = {name: list(lines) for name, lines in sections.items()}

        newly_masked, already_masked, unmatched = self.node._mask_unassigned_shapekeys(
            sections, {"Freq_yaobai_001"}
        )

        self.assertEqual((newly_masked, already_masked, unmatched), (4, 0, []))  # Constants 1 + Present 2 + Shader 1
        self.assertIn("; [未分配-已屏蔽:Freq_yaobai_001] global persist $Freq_yaobai_001 = 0.0",
                      sections["[Constants]"])
        # 未被屏蔽的变量必须保持原样
        self.assertIn("global persist $MyShape = 1.0", sections["[Constants]"])
        self.assertIn("; [未分配-已屏蔽:Freq_yaobai_001]     $Freq_yaobai_001 = $param0",
                      sections["[Present]"])
        self.assertIn("; [未分配-已屏蔽:Freq_yaobai_001] x100 = $Freq_yaobai_001",
                      sections["[CustomShader_ab12cd34_Anim]"])

        restored, kept = self.node._unmask_all_shapekeys(
            sections, keep_masked=set(), active_vars={"Freq_yaobai_001"}
        )

        self.assertEqual((restored, kept), (4, 0))
        for name, lines in original.items():
            self.assertEqual(sections[name], lines, f"{name} 未逐行还原")

    def test_mask_matches_custom_variable_name_without_freq_prefix(self):
        """「导出变量」是自由文本：不带 Freq_ 前缀的名字也必须被屏蔽。"""
        sections = self._sections()

        newly_masked, already_masked, unmatched = self.node._mask_unassigned_shapekeys(
            sections, {"MyShape"}
        )

        self.assertEqual((newly_masked, already_masked, unmatched), (3, 0, []))  # Constants 1 + Present 1 + Shader 1
        self.assertIn("; [未分配-已屏蔽:MyShape] global persist $MyShape = 1.0",
                      sections["[Constants]"])
        self.assertIn("; [未分配-已屏蔽:MyShape]     $MyShape = $param1", sections["[Present]"])
        self.assertIn("; [未分配-已屏蔽:MyShape] x101 = $MyShape",
                      sections["[CustomShader_ab12cd34_Anim]"])
        # 不相干的形态键不受影响
        self.assertIn("global persist $Freq_yaobai_001 = 0.0", sections["[Constants]"])

    def test_accepts_names_with_dollar_prefix(self):
        sections = self._sections()

        self.node._mask_unassigned_shapekeys(sections, {"$MyShape"})

        self.assertIn("; [未分配-已屏蔽:MyShape] global persist $MyShape = 1.0",
                      sections["[Constants]"])

    def test_shader_layer_writes_live_zero_line_and_unmask_removes_it(self):
        """注释行不写寄存器：归零必须是**有效行**，标记放在行尾供还原时删除。"""
        sections = self._sections()

        self.node._mask_unassigned_shapekeys(sections, {"MyShape"})

        shader_lines = sections["[CustomShader_ab12cd34_Anim]"]
        self.assertEqual(
            shader_lines,
            [
                "x100 = $Freq_yaobai_001",
                "; [未分配-已屏蔽:MyShape] x101 = $MyShape",
                "x101 = 0 ; [未分配-已屏蔽-归零:MyShape]",
                "x102 = $Freq_Group1",
            ],
        )
        # 归零行不是注释行（否则等于没写）
        self.assertFalse(shader_lines[2].lstrip().startswith(";"))

        self.node._unmask_all_shapekeys(
            sections, keep_masked=set(), active_vars={"MyShape"}
        )

        self.assertEqual(
            sections["[CustomShader_ab12cd34_Anim]"],
            ["x100 = $Freq_yaobai_001", "x101 = $MyShape", "x102 = $Freq_Group1"],
        )

    def test_renamed_variable_keeps_old_lines_masked(self):
        """变量改名后（装 pypinyin / 手改导出变量）：旧名残行必须继续屏蔽。"""
        sections = self._sections()
        self.node._mask_unassigned_shapekeys(sections, {"Freq_yaobai_001"})
        snapshot = {name: list(lines) for name, lines in sections.items()}

        restored, kept = self.node._unmask_all_shapekeys(
            sections, keep_masked={"Freq_xianzhi"}, active_vars={"Freq_xianzhi"}
        )

        self.assertEqual(restored, 0)
        self.assertEqual(kept, 4)
        for name, lines in snapshot.items():
            self.assertEqual(sections[name], lines, f"{name} 的屏蔽行被错误还原")

    def test_moved_back_into_group_restores_lines(self):
        sections = self._sections()
        self.node._mask_unassigned_shapekeys(sections, {"MyShape"})

        restored, kept = self.node._unmask_all_shapekeys(
            sections, keep_masked=set(), active_vars={"MyShape", "Freq_yaobai_001"}
        )

        self.assertEqual(restored, 3)
        self.assertEqual(kept, 0)
        self.assertIn("global persist $MyShape = 1.0", sections["[Constants]"])

    def test_legacy_untagged_marker_is_restored(self):
        """兼容已生成过的 mod：旧格式（标记里没有变量名）按旧行为还原。"""
        sections = OrderedDict()
        sections["[Constants]"] = ["; [未分配-已屏蔽] global persist $Freq_old = 0.0"]

        restored, kept = self.node._unmask_all_shapekeys(
            sections, keep_masked={"Freq_other"}, active_vars={"Freq_other"}
        )

        self.assertEqual((restored, kept), (1, 0))
        self.assertEqual(sections["[Constants]"], ["global persist $Freq_old = 0.0"])

    def test_unmask_without_context_falls_back_to_restore(self):
        """上游节点未连线（解析不到任何变量）时保持旧的 fail-open 行为。"""
        sections = self._sections()
        self.node._mask_unassigned_shapekeys(sections, {"MyShape"})

        restored, kept = self.node._unmask_all_shapekeys(
            sections, keep_masked=set(), active_vars=set()
        )

        self.assertEqual(restored, 3)
        self.assertEqual(kept, 0)

    def test_mask_is_idempotent_across_repeated_refresh(self):
        sections = self._sections()
        first_pass = None
        for _ in range(3):
            self.node._unmask_all_shapekeys(
                sections, keep_masked={"MyShape"}, active_vars={"MyShape"}
            )
            self.node._mask_unassigned_shapekeys(sections, {"MyShape"})
            if first_pass is None:
                first_pass = {name: list(lines) for name, lines in sections.items()}
            else:
                for name, lines in first_pass.items():
                    self.assertEqual(sections[name], lines, f"{name} 反复刷新后不稳定")

    def test_empty_target_does_not_touch_sections(self):
        sections = self._sections()
        original = {name: list(lines) for name, lines in sections.items()}

        self.assertEqual(
            self.node._mask_unassigned_shapekeys(sections, set()), (0, 0, [])
        )
        for name, lines in original.items():
            self.assertEqual(sections[name], lines)


class LiveReferenceTests(unittest.TestCase):
    """屏蔽之后的不变量校验：不允许残留对已屏蔽变量的有效引用。"""

    def setUp(self):
        self.node = _make_node()

    def test_flags_live_reads_and_ignores_comments_and_lookalike_names(self):
        sections = OrderedDict()
        sections["[Present]"] = [
            "$Freq_xianzhi = 0.5",
            "; [未分配-已屏蔽:Freq_xianzhi] $Freq_xianzhi = $param1",
            "            $param1 = $Freq_xianzhi",
            "$Freq_xianzhi_extra = 1",
            "; $param2 = $Freq_xianzhi",
        ]

        hits = self.node._collect_live_references(sections, {"Freq_xianzhi"})

        self.assertEqual(hits, [("[Present]", 1, "$Freq_xianzhi = 0.5"),
                                ("[Present]", 3, "$param1 = $Freq_xianzhi")])

    def test_no_hits_when_everything_is_commented(self):
        sections = OrderedDict()
        sections["[Present]"] = ["; $param1 = $Freq_xianzhi"]
        sections["[Constants]"] = ["; [未分配-已屏蔽:Freq_xianzhi] global persist $Freq_xianzhi = 0.0"]

        self.assertEqual(self.node._collect_live_references(sections, {"Freq_xianzhi"}), [])

    def test_empty_tokens_returns_empty(self):
        sections = OrderedDict()
        sections["[Present]"] = ["$param1 = $Freq_xianzhi"]

        self.assertEqual(self.node._collect_live_references(sections, set()), [])


class SliderPanelExclusionTests(unittest.TestCase):
    """被屏蔽的形态键不再生成滑块（否则会读已注释的变量、并留下死滑块）。"""

    def test_excludes_disabled_vars_and_keeps_group_vars(self):
        freq_params = {"$Freq_Group1", "$Freq_xianzhi", "$Freq_xianzhi_2"}

        kept, skipped = MASK._exclude_disabled_freq_params(freq_params, {"Freq_xianzhi"})

        self.assertEqual(kept, {"$Freq_Group1", "$Freq_xianzhi_2"})
        self.assertEqual(skipped, ["$Freq_xianzhi"])

    def test_accepts_dollar_prefixed_exclusions_and_empty_input(self):
        kept, skipped = MASK._exclude_disabled_freq_params({"$Freq_a"}, {"$Freq_a"})
        self.assertEqual((kept, skipped), (set(), ["$Freq_a"]))

        kept, skipped = MASK._exclude_disabled_freq_params({"$Freq_a"}, set())
        self.assertEqual(kept, {"$Freq_a"})
        self.assertEqual(skipped, [])


class UnitSpeedTests(unittest.TestCase):
    """单位速度：兑现「总时长」，允许小数步进。"""

    def setUp(self):
        self.node = _make_node()

    def test_unit_speed_keeps_fraction_for_small_subdivision_counts(self):
        setting = _Setting(auto_playback_frame_count=30, auto_playback_duration=2.0, playback_fps=60)

        # 30 / (2 × 60) = 0.25 —— 之前被钳成 1，导致 4 倍速播放
        self.assertAlmostEqual(self.node._calc_unit_speed(setting), 0.25, places=4)

    def test_unit_speed_matches_documented_example(self):
        setting = _Setting(auto_playback_frame_count=2400, auto_playback_duration=2.0, playback_fps=120)

        self.assertAlmostEqual(self.node._calc_unit_speed(setting), 10.0, places=4)

    def test_unit_speed_never_reaches_zero(self):
        setting = _Setting(auto_playback_frame_count=2, auto_playback_duration=300.0, playback_fps=360)

        self.assertGreater(self.node._calc_unit_speed(setting), 0.0)

    def test_generated_step_lines_use_fractional_unit_speed(self):
        sections = OrderedDict()
        self.node._add_auto_playback_logic_for_group(
            sections, 1, "$Freq_Group1", 30, "FORWARD", 100, 1000,
            [_Interval(0.0, 100.0, 100)], 3, 0.25, True,
        )

        steps = [line.strip() for line in sections["[Present]"] if "base_step_group1 =" in line]
        self.assertEqual(steps[0], "$base_step_group1 = 0.25")


class IntervalSemanticsTests(unittest.TestCase):
    """变速区间：新语义按百分比，老工程按旧的每帧步进数（行为不变）。"""

    def setUp(self):
        self.node = _make_node()

    def _step_line(self, base_step, unit_speed, percent_mode):
        sections = OrderedDict()
        self.node._add_auto_playback_logic_for_group(
            sections, 1, "$Freq_Group1", 30, "FORWARD", 100, 1000,
            [_Interval(0.0, 100.0, base_step)], 3, unit_speed, percent_mode,
        )
        return [line.strip() for line in sections["[Present]"] if "base_step_group1 =" in line]

    def test_legacy_interval_keeps_old_step_values(self):
        # 升级前保存的工程：base_step=10 就是每帧 10 个细分，与单位速度无关
        self.assertEqual(self._step_line(10, 0.25, False)[0], "$base_step_group1 = 10")

    def test_percent_interval_scales_with_unit_speed(self):
        self.assertEqual(self._step_line(100, 0.25, True)[0], "$base_step_group1 = 0.25")
        self.assertEqual(self._step_line(10, 0.25, True)[0], "$base_step_group1 = 0.025")

    def test_legacy_tail_and_fallback_match_old_generator(self):
        self.assertEqual(self.node._tail_step_value(0.25, False), 1.0)
        self.assertEqual(self.node._no_interval_fallback_step(0.25, False), 10.0)
        self.assertEqual(self.node._tail_step_value(0.25, True), 0.25)
        self.assertEqual(self.node._no_interval_fallback_step(0.25, True), 0.25)

    def test_auto_unroll_covers_both_semantics(self):
        percent = _Setting(speed_percent_mode=True, speed_intervals=[_Interval(base_step=100)])
        legacy = _Setting(speed_percent_mode=False, speed_intervals=[_Interval(base_step=10)])

        # 新语义：0.25 × 100% × 1000% × 1.5 → 4 → 下限 10
        self.assertEqual(self.node._calc_auto_max_step_unroll(percent), 10)
        # 老语义：10 × 1000% × 1.5 = 150
        self.assertEqual(self.node._calc_auto_max_step_unroll(legacy), 150)


class VarToGroupMapTests(unittest.TestCase):
    """没有分组条目的形态键按未分组处理（不再被并进分组 1）。"""

    def setUp(self):
        self.node = _make_node()
        self.node.play_group_entries = [
            types.SimpleNamespace(shape_key_name="摇摆", group_index=1),
            types.SimpleNamespace(shape_key_name="闲置", group_index=0),
        ]

    def test_orphan_and_unassigned_variables_are_group_zero(self):
        freq_vars = OrderedDict([
            ("$Freq_yaobai", {"default": "0.0", "label": "摇摆"}),
            ("$Freq_xianzhi", {"default": "0.0", "label": "闲置"}),
            ("$Freq_orphan", {"default": "0.0", "label": "没有条目的形态键"}),
        ])

        var_to_group, all_groups = self.node._build_var_to_group_map(
            freq_vars, ["摇摆", "闲置", "没有条目的形态键"]
        )

        self.assertEqual(var_to_group["$Freq_yaobai"], 1)
        self.assertEqual(var_to_group["$Freq_xianzhi"], 0)
        self.assertEqual(var_to_group["$Freq_orphan"], 0)
        self.assertEqual(all_groups, [1])


class _LogCapture:
    def __init__(self):
        self.lines = []

    def __call__(self, *args, **_kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self):
        return "\n".join(self.lines)


class ExecutePostprocessMaskIntegrationTests(unittest.TestCase):
    """端到端：真的跑 execute_postprocess 原地刷新，验证屏蔽/归零/改名/移回分组。"""

    INI = """[Constants]
; 控制形态键 '闲置' 的强度
global persist $Freq_xianzhi = 0.0
[Present]
$Freq_xianzhi = 0.5
[CustomShader_ab12cd34]
[CustomShader_ab12cd34_Anim]
x100 = $Freq_xianzhi
"""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod_dir = Path(self._tmp.name)
        self.ini_path = self.mod_dir / "mod.ini"
        self.ini_path.write_text(self.INI, encoding="utf-8")
        self.node = self._make_node(entry_group=0, var_name="Freq_xianzhi")
        self._log = _LogCapture()
        self._print_patcher = mock.patch("builtins.print", side_effect=self._log)
        self._print_patcher.start()
        self.addCleanup(self._print_patcher.stop)

    def _make_node(self, entry_group, var_name):
        node = _make_node()
        node.namespace = "itest"
        node.last_mod_ini_path = ""
        node.create_cumulative_backup = False
        node.use_slider_panel = False
        node.auto_play_toggle_key = "space"
        node.auto_play_key_global = False
        node.play_group_settings = []
        node.play_group_entries = [
            types.SimpleNamespace(shape_key_name="闲置", group_index=entry_group)
        ]
        node._scan_shapekey_names_from_variable_items = lambda: ["闲置"]
        node._scan_shapekey_names_from_classification = lambda: []
        node._scan_shapekey_name_to_var_map = lambda: {"闲置": var_name}
        node._create_cumulative_backup = lambda *_a, **_k: None
        node._apply_slider_panel = lambda *_a, **_k: False
        return node

    def _refresh(self):
        ok = self.node.execute_postprocess(
            str(self.mod_dir), _in_place=True, _ini_path=str(self.ini_path)
        )
        self.assertTrue(ok)
        return self.ini_path.read_text(encoding="utf-8")

    @staticmethod
    def _live_text(text):
        return "\n".join(
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(";")
        )

    def test_first_refresh_masks_declaration_assignment_and_shader_slot(self):
        text = self._refresh()

        self.assertIn("; [未分配-已屏蔽:Freq_xianzhi] global persist $Freq_xianzhi = 0.0", text)
        self.assertIn("; [未分配-已屏蔽:Freq_xianzhi] $Freq_xianzhi = 0.5", text)
        self.assertIn("; [未分配-已屏蔽:Freq_xianzhi] x100 = $Freq_xianzhi", text)
        # 归零行必须是有效行（能被 ini 解析），且带身份标记
        self.assertIn("x100 = 0 ; [未分配-已屏蔽-归零:Freq_xianzhi]", text)
        self.assertNotIn("$Freq_xianzhi", self._live_text(text))
        self.assertIn("本次新增 3 行", self._log.text)
        self.assertIn("已处理 1 个: ['Freq_xianzhi']", self._log.text)

    def test_apmi_refresh_uses_activate_key_type(self):
        key_type_module = sys.modules[f"{PKG}.common.key_type"]
        with mock.patch.object(
            key_type_module,
            "one_shot_key_type",
            return_value="activate",
        ):
            text = self._refresh()

        self.assertIn("[KeyToggleAutoPlay_itest]", text)
        self.assertIn("type = activate", text)
        self.assertNotIn("type = press", text)

    def test_repeated_refresh_is_idempotent_and_keeps_zero_line(self):
        first = self._refresh()
        second = self._refresh()

        self.assertEqual(first, second)
        self.assertEqual(second.count("; [未分配-已屏蔽-归零:Freq_xianzhi]"), 1)
        self.assertIn("沿用上次 3 行", self._log.text)

    def test_renamed_variable_keeps_old_lines_masked(self):
        first = self._refresh()

        # 模拟「装了 pypinyin / 改了导出变量」：变量名被刷新成新名
        self.node._scan_shapekey_name_to_var_map = lambda: {"闲置": "Freq_xinming"}
        second = self._refresh()

        self.assertEqual(first.count("; [未分配-已屏蔽:Freq_xianzhi]"), 3)
        # 旧名的三层引用必须继续屏蔽（不能被还原成有效行）
        self.assertIn("; [未分配-已屏蔽:Freq_xianzhi] global persist $Freq_xianzhi = 0.0", second)
        self.assertIn("; [未分配-已屏蔽:Freq_xianzhi] x100 = $Freq_xianzhi", second)
        self.assertIn("x100 = 0 ; [未分配-已屏蔽-归零:Freq_xianzhi]", second)
        self.assertNotIn("$Freq_xianzhi", self._live_text(second))
        self.assertIn("找不到对应行", self._log.text)
        self.assertIn("['Freq_xinming']", self._log.text)

    def test_mixed_matched_and_unmatched_variables_warns_for_the_unmatched_one(self):
        """一个键能匹配、另一个键已改名时，必须单独为未匹配的那个报警。"""
        self.ini_path.write_text(
            self.INI.replace(
                "global persist $Freq_xianzhi = 0.0",
                "global persist $Freq_yaobai = 0.0\nglobal persist $Freq_xianzhi = 0.0",
            ).replace(
                "x100 = $Freq_xianzhi",
                "x100 = $Freq_yaobai\nx101 = $Freq_xianzhi",
            ),
            encoding="utf-8",
        )
        self.node.play_group_entries = [
            types.SimpleNamespace(shape_key_name="摇摆", group_index=1),
            types.SimpleNamespace(shape_key_name="闲置", group_index=0),
            types.SimpleNamespace(shape_key_name="闭眼", group_index=0),
        ]
        self.node._scan_shapekey_name_to_var_map = lambda: {
            "摇摆": "Freq_yaobai",
            "闲置": "Freq_xinming",   # 改名了，ini 里还没有新名
            "闭眼": "Freq_xianzhi",   # ini 里有，能正常屏蔽
        }

        text = self._refresh()

        self.assertIn("; [未分配-已屏蔽:Freq_xianzhi] x101 = $Freq_xianzhi", text)
        self.assertIn("已处理 1 个: ['Freq_xianzhi']", self._log.text)
        self.assertIn("找不到对应行", self._log.text)
        self.assertIn("['Freq_xinming']", self._log.text)

    def test_moving_back_into_group_restores_every_line(self):
        self._refresh()

        # 形态键被移回分组 1（不再是未分配）→ 三层引用全部还原，归零行删除
        self.node = self._make_node(entry_group=1, var_name="Freq_xianzhi")
        text = self._refresh()

        self.assertIn("global persist $Freq_xianzhi = 0.0", text)
        self.assertIn("$Freq_xianzhi = 0.5", text)
        self.assertIn("x100 = $Freq_xianzhi", text)
        self.assertNotIn("未分配-已屏蔽", text)


if __name__ == "__main__":
    unittest.main()
