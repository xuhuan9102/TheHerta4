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


PKG = "_anim_driver_base_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []


_bpy_props = _install_module(
    "bpy.props",
    FloatProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    StringProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
)
_bpy_types = _install_module(
    "bpy.types",
    Menu=object,
    NodeTree=object,
    NodeSocket=object,
    Node=object,
    SpaceNodeEditor=object,
    PropertyGroup=object,
    UIList=object,
    Operator=object,
)
_install_module(
    "bpy",
    props=_bpy_props,
    types=_bpy_types,
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
    app=types.SimpleNamespace(
        handlers=types.SimpleNamespace(
            persistent=lambda func: func,
            load_post=[],
        )
    ),
    data=types.SimpleNamespace(objects={}, node_groups=[]),
)
_install_module(
    f"{PKG}.blueprint.node_base",
    SSMTBlueprintTree=object,
    SSMTNodeBase=object,
    refresh_blueprint_node_colors=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(workspacename="Test", read_from_main_json_ssmt4=lambda: None),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda _name: None,
        parse_prefix_parts=lambda _prefix: {},
    ),
)
def _stub_ensure_anim_driver_frame_variable_name(node, context=None):
    """运行时间节点的预分配帧变量（与生产分配器同契约：写回并返回名字）。"""
    name = str(getattr(node, "assigned_frame_variable_name", "") or "").strip()
    if not name:
        name = f"anim_frame{int(getattr(node, 'auto_index', 0) or 0)}"
        try:
            node.assigned_frame_variable_name = name
        except Exception:
            pass
    return name


_install_module(
    f"{PKG}.blueprint.variable_registry",
    ANIM_DRIVER_FRAME_PREFIX="anim_frame",
    allocate_continuous_shapekey_index_variable_name=lambda **_kwargs: "continuous_shapekey_frame1",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip().lstrip("$"),
    ensure_anim_driver_frame_variable_name=_stub_ensure_anim_driver_frame_variable_name,
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "anim_driver_base.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.anim_driver_base", module_path)
anim_driver_base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = anim_driver_base
spec.loader.exec_module(anim_driver_base)


def _load_blueprint_module(module_name):
    module_path = Path(__file__).resolve().parents[1] / "blueprint" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


forward_play_module = _load_blueprint_module("anim_driver_forward_play")
pingpong_module = _load_blueprint_module("anim_driver_pingpong")
runtime_module = _load_blueprint_module("anim_driver_runtime")
toggle_module = _load_blueprint_module("anim_driver_toggle")
trigger_module = _load_blueprint_module("anim_driver_trigger")
accumulative_trigger_module = _load_blueprint_module("anim_driver_accumulative_trigger")
cond_trigger_module = _load_blueprint_module("anim_driver_conditional_trigger")
shapekey_seq_module = _load_blueprint_module("anim_driver_shapekey_seq")
node_menu_module = _load_blueprint_module("node_menu")


def _assert_balanced_conditionals(test_case, ini_text):
    stack = []
    for lineno, raw_line in enumerate(ini_text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(";") or (line.startswith("[") and line.endswith("]")):
            continue
        if line.startswith("if "):
            stack.append((lineno, line))
        elif line == "else":
            test_case.assertTrue(stack, f"Line {lineno} has else without matching if")
        elif line == "endif":
            test_case.assertTrue(stack, f"Line {lineno} has endif without matching if")
            stack.pop()

    test_case.assertEqual(stack, [], f"Unclosed if blocks: {stack}")


class _FakeDynamicSocket:
    def __init__(self, name, linked=False):
        self.bl_idname = "SSMTSocketAnimDriver"
        self.name = name
        self.is_linked = linked


class _FakeDynamicSocketList:
    def __init__(self, sockets=None):
        self._sockets = list(sockets or [])
        self.created = []
        self.removed = []

    def __len__(self):
        return len(self._sockets)

    def __getitem__(self, index):
        return self._sockets[index]

    def __iter__(self):
        return iter(self._sockets)

    def new(self, socket_type, name):
        socket = _FakeDynamicSocket(name)
        self._sockets.append(socket)
        self.created.append((socket_type, name))
        return socket

    def remove(self, socket):
        self._sockets.remove(socket)
        self.removed.append(socket.name)


class AnimDriverBaseTests(unittest.TestCase):
    def test_default_play_state_migration_flips_legacy_flag_once(self):
        node = {}
        node_get = lambda key, default=None: dict.get(node, key, default)
        node["get"] = node_get
        legacy = types.SimpleNamespace(default_paused=True)
        legacy.get = lambda key, default=None: False
        legacy.__setitem__ = lambda key, value: node.__setitem__(key, value)
        try:
            anim_driver_base.SSMTNode_AnimDriver_Base.migrate_default_play_state_flag(legacy)
        except TypeError:
            setattr(legacy, anim_driver_base.SSMTNode_AnimDriver_Base.PLAY_STATE_MIGRATION_KEY, True)
            legacy.default_paused = not True

        self.assertFalse(legacy.default_paused)

    def test_activation_flag_defaults_to_active0(self):
        anim_driver_base.GlobalConfig.logic_name = ""
        self.assertEqual(anim_driver_base.SSMTNode_AnimDriver_Base._get_activation_flag(), "$active0")

    def test_activation_flag_uses_ntmi_active0_for_ntemi(self):
        anim_driver_base.GlobalConfig.logic_name = "NTEMI"
        try:
            self.assertEqual(anim_driver_base.SSMTNode_AnimDriver_Base._get_activation_flag(), "$ntmi_active0")
        finally:
            anim_driver_base.GlobalConfig.logic_name = ""

    def test_read_safe_index_uses_sorted_position_without_mutating_invalid_indices(self):
        base_cls = anim_driver_base.SSMTNode_AnimDriver_Base

        node_b = base_cls()
        node_b.name = "B"
        node_b.auto_index = 2

        node_a = base_cls()
        node_a.name = "A"
        node_a.auto_index = 2

        node_c = base_cls()
        node_c.name = "C"
        node_c.auto_index = 0

        tree = types.SimpleNamespace(nodes=[node_b, node_a, node_c])
        for node in (node_a, node_b, node_c):
            node.id_data = tree

        safe_index = node_b._read_safe_index()

        self.assertEqual(safe_index, 2)
        self.assertEqual(node_a.auto_index, 2)
        self.assertEqual(node_b.auto_index, 2)
        self.assertEqual(node_c.auto_index, 0)

    def test_forward_play_clamps_all_driven_vars_to_last_frame_after_overshoot(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 3.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.driven_variable_list = [
            types.SimpleNamespace(variable_name="$varA"),
            types.SimpleNamespace(variable_name="$varB"),
        ]
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("if $varA > $frameEnd1", ini)
        self.assertIn("$varA = $frameEnd1", ini)
        self.assertIn("$varB = $frameEnd1", ini)
        self.assertIn("global persist $paused = 1", ini)

    def test_pingpong_clamps_all_driven_vars_on_both_bounds_after_overshoot(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.name = "PingPong"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 3.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = False
        node.loop_playback = False
        node.driven_variable_list = [
            types.SimpleNamespace(variable_name="$varA"),
            types.SimpleNamespace(variable_name="$varB"),
        ]
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        self.assertIn("if $varA > $frameEnd1", ini)
        self.assertIn("$varA = $frameEnd1", ini)
        self.assertIn("$varB = $frameEnd1", ini)
        self.assertIn("if $varA < $frameStart1", ini)
        self.assertIn("$varA = $frameStart1", ini)
        self.assertIn("$varB = $frameStart1", ini)
        self.assertIn("global persist $paused = 1", ini)

    def test_pingpong_generated_ini_has_balanced_conditionals(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.name = "PingPong"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.frame_start = 0.0
        node.frame_end = 3.0
        node.play_total_duration = 0.1
        node.use_float_interval = True
        node.default_paused = True
        node.custom_paused_var = "$paused"
        node.reverse_playback = True
        node.loop_playback = True
        node.use_continuous_shapekey_mode = True
        node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Frame_001", variable_name="$Freq_Frame_001"),
            types.SimpleNamespace(shape_key_name="Frame_002", variable_name="$Freq_Frame_002"),
        ]
        node.driven_variable_list = []
        node.driven_variable = ""
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)
        node._get_next_node_in_chain = lambda: None

        ini = node.generate_ini_segment()

        _assert_balanced_conditionals(self, ini)

    def test_trigger_default_play_exports_persisted_play_state(self):
        node = trigger_module.SSMTNode_AnimDriver_Trigger()
        node.name = "Trigger"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.default_paused = True
        node.custom_paused_var = "$trigger_paused"
        node.target_list = [types.SimpleNamespace(variable_name="$target", trigger_value="1")]
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=2)

        ini = node.generate_ini_segment()

        self.assertIn("global persist $trigger_paused = 1", ini)

    def test_conditional_trigger_default_play_exports_persisted_play_state(self):
        node = cond_trigger_module.SSMTNode_AnimDriver_ConditionalTrigger()
        node.name = "CondTrigger"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.default_paused = True
        node.custom_paused_var = "$cond_paused"
        node.condition_list = []
        node.target_list = [types.SimpleNamespace(variable_name="$target", trigger_value="1")]
        node.else_target_list = []
        node.logic_operator = "AND"

        ini = node.generate_ini_segment()

        self.assertIn("global persist $cond_paused = 1", ini)
        self.assertIn("if $cond_paused == 1", ini)
        self.assertIn("    $target = 1", ini)
        self.assertNotIn("$cond_state", ini)

    def test_accumulative_trigger_stacks_each_matching_condition_dynamically(self):
        node = accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger()
        node.name = "AccumulativeTrigger"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.default_paused = True
        node.custom_paused_var = "$acc_paused"
        node.accumulator_variable = "$progress"
        node.condition_list = [
            types.SimpleNamespace(
                variable_name="$a", comparison_op="==", compare_value="1", increment_value="0.1"
            ),
            types.SimpleNamespace(
                variable_name="$b", comparison_op="==", compare_value="1", increment_value="0.1"
            ),
        ]
        node.target_list = [
            types.SimpleNamespace(threshold_value="3", variable_name="$d", trigger_value="1"),
            types.SimpleNamespace(threshold_value="1", variable_name="$c", trigger_value="1"),
        ]
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=2)

        ini = node.generate_ini_segment()

        self.assertIn("global persist $speed_auto1 = 2", ini)
        self.assertIn("if $anim_frame % $speed_auto1 == 0", ini)
        self.assertIn("if $a == 1\n            $progress = $progress + 0.1", ini)
        self.assertIn("if $b == 1\n            $progress = $progress + 0.1", ini)
        self.assertEqual(ini.count("$progress = $progress + 0.1"), 2)
        self.assertIn("if $progress >= 1\n                $c = 1", ini)
        self.assertIn("$accumulative_threshold_state1_1 = 1", ini)
        self.assertIn("if $progress >= 3\n            $d = 1\n            $progress = 0", ini)
        self.assertIn("$progress = 0\n            $accumulative_threshold_state1_1 = 0", ini)
        self.assertEqual(ini.count("$progress = 0"), 2)
        _assert_balanced_conditionals(self, ini)

    def test_new_accumulative_trigger_is_not_treated_as_legacy_on_reload(self):
        tree = types.SimpleNamespace(bl_idname='SSMTBlueprintTreeType', nodes=[])
        node = accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger()
        node.name = "AccumulativeTrigger"
        node.id_data = tree
        node.inputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        node.outputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        tree.nodes.append(node)

        node.init(context=None)

        migration_key = anim_driver_base.SSMTNode_AnimDriver_Base.PLAY_STATE_MIGRATION_KEY
        self.assertTrue(getattr(node, migration_key))

    def test_accumulative_trigger_keeps_non_finite_thresholds_in_user_order(self):
        targets = [
            types.SimpleNamespace(
                threshold_value="NaN", variable_name="$first", trigger_value="1"
            ),
            types.SimpleNamespace(
                threshold_value="1", variable_name="$second", trigger_value="1"
            ),
        ]

        groups = (
            accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger
            ._group_threshold_targets(targets)
        )

        self.assertEqual([group["threshold"] for group in groups], ["NaN", "1"])

    def test_conditional_trigger_and_mode_level_triggered_with_else(self):
        node = cond_trigger_module.SSMTNode_AnimDriver_ConditionalTrigger()
        node.name = "CondTrigger"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.default_paused = True
        node.custom_paused_var = "$cond_paused"
        node.logic_operator = "AND"
        node.condition_list = [
            types.SimpleNamespace(variable_name="$a", comparison_op="==", compare_value="1"),
            types.SimpleNamespace(variable_name="$b", comparison_op=">", compare_value="2"),
        ]
        node.target_list = [types.SimpleNamespace(variable_name="$target", trigger_value="1")]
        node.else_target_list = [types.SimpleNamespace(variable_name="$target", trigger_value="0")]

        ini = node.generate_ini_segment()

        self.assertIn("if ($a == 1) && ($b > 2)", ini)
        self.assertIn("\n    else\n", ini)
        self.assertIn("        $target = 0", ini)
        self.assertNotIn("$cond_state", ini)
        self.assertNotIn("$cond_flag", ini)
        _assert_balanced_conditionals(self, ini)

    def test_conditional_trigger_or_mode_combines_with_or(self):
        node = cond_trigger_module.SSMTNode_AnimDriver_ConditionalTrigger()
        node.name = "CondTrigger"
        node.auto_index = 2
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.default_paused = True
        node.custom_paused_var = "$cond_paused"
        node.logic_operator = "OR"
        node.condition_list = [
            types.SimpleNamespace(variable_name="$a", comparison_op="==", compare_value="1"),
            types.SimpleNamespace(variable_name="$b", comparison_op=">", compare_value="2"),
        ]
        node.target_list = [types.SimpleNamespace(variable_name="$target", trigger_value="1")]
        node.else_target_list = [types.SimpleNamespace(variable_name="$target", trigger_value="0")]

        ini = node.generate_ini_segment()

        self.assertIn("if ($a == 1) || ($b > 2)", ini)
        self.assertNotIn("$cond_flag", ini)
        self.assertNotIn("$cond_state", ini)
        _assert_balanced_conditionals(self, ini)

    def test_shapekey_sequence_default_play_exports_persisted_play_state(self):
        node = shapekey_seq_module.SSMTNode_AnimDriver_ShapeKeySequence()
        node.name = "ShapeKeySeq"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.default_paused = True
        node.custom_paused_var = "$seq_paused"
        node.driven_variable = "$seq_driver"
        node.shapekey_items = []
        node._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)

        ini = node.generate_ini_segment()

        self.assertIn("global persist $seq_paused = 1", ini)

    def test_shapekey_sequence_chain_handoff_and_balanced_conditionals(self):
        node1 = shapekey_seq_module.SSMTNode_AnimDriver_ShapeKeySequence()
        node1.name = "Seq1"
        node1.auto_index = 1
        node1.id_data = types.SimpleNamespace(nodes=[node1], links=[])
        node1.default_paused = True
        node1.custom_paused_var = "$seq_paused1"
        node1.driven_variable = "$seq_drv1"
        node1.loop_playback = True
        node1.shapekey_items = [
            types.SimpleNamespace(name="Key1", variable="$Freq_Key1", frame_values_csv="0.0,0.5,1.0"),
        ]
        node1._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)

        node2 = shapekey_seq_module.SSMTNode_AnimDriver_ShapeKeySequence()
        node2.name = "Seq2"
        node2.auto_index = 2
        node2.id_data = types.SimpleNamespace(nodes=[node2], links=[])
        node2.default_paused = False
        node2.custom_paused_var = "$seq_paused2"
        node2.driven_variable = "$seq_drv2"
        node2.loop_playback = True
        node2.pingpong_playback = True
        node2.shapekey_items = [
            types.SimpleNamespace(name="Key2", variable="$Freq_Key2", frame_values_csv="0.2,0.8,0.4"),
        ]
        node2._find_runtime_node = lambda: types.SimpleNamespace(fps=30, playback_rate=1)

        # 模拟链条：node1 -> node2
        node1._get_next_node_in_chain = lambda: node2
        node1._get_all_next_paused_vars = lambda: ["$seq_paused2"]
        node2._get_next_node_in_chain = lambda: None
        node2._get_all_next_paused_vars = lambda: []

        ini1 = node1.generate_ini_segment()
        # 下游有节点时：上游节点交接下游，暂停自己并激活下游
        self.assertIn("$seq_paused1 = 0", ini1)
        self.assertIn("$seq_paused2 = 1", ini1)
        _assert_balanced_conditionals(self, ini1)

        ini2 = node2.generate_ini_segment()
        # 下游无节点且开启往返：有方向变量并进行往返循环
        self.assertIn("global persist $direction2 = 1", ini2)
        self.assertIn("if $direction2 == 1", ini2)
        _assert_balanced_conditionals(self, ini2)

    def test_shapekey_sequence_target_objects_names_and_fallback(self):
        node = shapekey_seq_module.SSMTNode_AnimDriver_ShapeKeySequence()
        node.target_object = "LegacyMesh"
        node.target_objects = [
            types.SimpleNamespace(name="HeadMesh"),
            types.SimpleNamespace(name="BodyMesh"),
        ]
        names = node.get_target_object_names()
        self.assertEqual(names, ["HeadMesh", "BodyMesh", "LegacyMesh"])

    def test_draw_continuous_controls_does_not_allocate_or_write_scene_state(self):
        base_cls = anim_driver_base.SSMTNode_AnimDriver_Base
        node = base_cls()
        node.name = "Base"
        node.continuous_target_object = ""
        node.continuous_shape_key_prefix_filter = ""
        node.custom_continuous_index_variable_name = "continuous_shapekey_frame1"
        node.assigned_continuous_index_variable_name = "continuous_shapekey_frame1"
        node.continuous_shape_key_items = []
        node.continuous_shape_key_items_active = 0

        calls = []

        class _FakeOperator:
            node_name = ""
            object_name = ""

        class _FakeRow:
            def align(self):
                return self

            def prop_search(self, *_args, **_kwargs):
                calls.append("prop_search")

            def operator(self, *_args, **_kwargs):
                calls.append("operator")
                return _FakeOperator()

        class _FakeBox:
            def row(self, align=False):
                del align
                return _FakeRow()

            def prop(self, *_args, **_kwargs):
                calls.append("prop")

            def label(self, *_args, **_kwargs):
                calls.append("label")

            def template_list(self, *_args, **_kwargs):
                calls.append("template_list")

        node._ensure_continuous_index_variable_name = lambda context=None: (_ for _ in ()).throw(
            AssertionError("draw should not allocate variables")
        )

        node._draw_continuous_shape_key_controls(_FakeBox())

        self.assertIn("prop", calls)

    def test_update_backfills_continuous_index_variable_for_legacy_node(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Forward"
        node.use_continuous_shapekey_mode = True
        node.assigned_continuous_index_variable_name = ""
        node.custom_continuous_index_variable_name = ""
        node.continuous_index_var_initialized = False
        node._ensure_continuous_index_variable_name = lambda context=None: setattr(
            node, "assigned_continuous_index_variable_name", "continuous_shapekey_frame1"
        ) or "continuous_shapekey_frame1"

        node.update()

        self.assertEqual(node.custom_continuous_index_variable_name, "continuous_shapekey_frame1")
        self.assertTrue(node.continuous_index_var_initialized)

    def test_dynamic_socket_expansion_grows_when_last_socket_linked(self):
        sockets = _FakeDynamicSocketList([_FakeDynamicSocket("链输入", linked=True)])

        anim_driver_base.SSMTNode_AnimDriver_Base._apply_dynamic_socket_expansion(sockets, "链输入")

        self.assertEqual(len(sockets), 2)
        self.assertEqual(sockets.created, [("SSMTSocketAnimDriver", "链输入")])
        self.assertFalse(sockets[-1].is_linked)

    def test_dynamic_socket_expansion_shrinks_trailing_unlinked_sockets(self):
        sockets = _FakeDynamicSocketList([
            _FakeDynamicSocket("链输入", linked=True),
            _FakeDynamicSocket("链输入"),
            _FakeDynamicSocket("链输入"),
        ])

        anim_driver_base.SSMTNode_AnimDriver_Base._apply_dynamic_socket_expansion(sockets, "链输入")

        self.assertEqual(len(sockets), 2)
        self.assertEqual(sockets.removed, ["链输入"])

    def test_dynamic_socket_expansion_keeps_single_empty_socket(self):
        sockets = _FakeDynamicSocketList([_FakeDynamicSocket("链输入")])

        anim_driver_base.SSMTNode_AnimDriver_Base._apply_dynamic_socket_expansion(sockets, "链输入")

        self.assertEqual(len(sockets), 1)
        self.assertEqual(sockets.created, [])
        self.assertEqual(sockets.removed, [])

    def test_update_expands_linked_sockets_and_keeps_empty_output(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.use_continuous_shapekey_mode = False
        node.inputs = _FakeDynamicSocketList([_FakeDynamicSocket("链输入", linked=True)])
        node.outputs = _FakeDynamicSocketList([_FakeDynamicSocket("链输出")])

        node.update()

        self.assertEqual(len(node.inputs), 2)
        self.assertEqual(node.inputs.created, [("SSMTSocketAnimDriver", "链输入")])
        self.assertEqual(len(node.outputs), 1)
        self.assertEqual(node.outputs.created, [])

    def test_migrate_dynamic_sockets_renames_legacy_sockets_and_normalizes(self):
        node = types.SimpleNamespace(
            inputs=_FakeDynamicSocketList([
                _FakeDynamicSocket("链输入", linked=True),
                _FakeDynamicSocket("时间输入"),
                _FakeDynamicSocket("驱动输入"),
            ]),
            outputs=_FakeDynamicSocketList([
                _FakeDynamicSocket("链输出", linked=True),
                _FakeDynamicSocket("时间输出", linked=True),
            ]),
        )

        anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)

        self.assertEqual([socket.name for socket in node.inputs], ["链输入", "链输入"])
        self.assertEqual(node.inputs.removed, ["链输入"])
        self.assertEqual(
            [socket.name for socket in node.outputs],
            ["链输出", "链输出", "链输出"],
        )
        self.assertEqual(node.outputs.created, [("SSMTSocketAnimDriver", "链输出")])

    def test_migrate_dynamic_sockets_creates_socket_when_empty(self):
        node = types.SimpleNamespace(
            inputs=_FakeDynamicSocketList(),
            outputs=_FakeDynamicSocketList(),
        )

        anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)

        self.assertEqual(node.inputs.created, [("SSMTSocketAnimDriver", "链输入")])
        self.assertEqual(node.outputs.created, [("SSMTSocketAnimDriver", "链输出")])

    def test_find_runtime_node_traverses_anim_sockets_regardless_of_name(self):
        runtime_node = runtime_module.SSMTNode_AnimDriver_Runtime()
        runtime_node.name = "Runtime"
        runtime_node.fps = 30
        runtime_node.playback_rate = 1

        play_node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        play_node.name = "Forward"

        class _FakeSocket:
            def __init__(self, name):
                self.bl_idname = "SSMTSocketAnimDriver"
                self.name = name

        class _FakeLink:
            def __init__(self, from_node, to_node, from_name, to_name):
                self.from_node = from_node
                self.to_node = to_node
                self.from_socket = _FakeSocket(from_name)
                self.to_socket = _FakeSocket(to_name)

        node_group = types.SimpleNamespace(
            nodes=[runtime_node, play_node],
            links=[_FakeLink(runtime_node, play_node, "任意输出", "任意输入")],
        )
        runtime_node.id_data = node_group
        play_node.id_data = node_group

        self.assertIs(play_node._find_runtime_node(), runtime_node)

    def test_find_runtime_node_reaches_runtime_through_intermediate_node(self):
        runtime_node = runtime_module.SSMTNode_AnimDriver_Runtime()
        runtime_node.name = "Runtime"
        runtime_node.fps = 30
        runtime_node.playback_rate = 1

        toggle_node = toggle_module.SSMTNode_AnimDriver_Toggle()
        toggle_node.name = "Toggle"

        play_node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        play_node.name = "Forward"

        class _FakeSocket:
            def __init__(self, name):
                self.bl_idname = "SSMTSocketAnimDriver"
                self.name = name

        class _FakeLink:
            def __init__(self, from_node, to_node):
                self.from_node = from_node
                self.to_node = to_node
                self.from_socket = _FakeSocket("链输出")
                self.to_socket = _FakeSocket("链输入")

        node_group = types.SimpleNamespace(
            nodes=[runtime_node, toggle_node, play_node],
            links=[
                _FakeLink(runtime_node, toggle_node),
                _FakeLink(toggle_node, play_node),
            ],
        )
        runtime_node.id_data = node_group
        toggle_node.id_data = node_group
        play_node.id_data = node_group

        self.assertIs(play_node._find_runtime_node(), runtime_node)

    def test_find_runtime_node_is_stable_when_multiple_runtimes_are_equally_near(self):
        runtime_b = runtime_module.SSMTNode_AnimDriver_Runtime()
        runtime_b.name = "Runtime B"
        runtime_b.fps = 60
        runtime_b.playback_rate = 2

        runtime_a = runtime_module.SSMTNode_AnimDriver_Runtime()
        runtime_a.name = "Runtime A"
        runtime_a.fps = 30
        runtime_a.playback_rate = 1

        play_node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        play_node.name = "Forward"

        class _FakeSocket:
            bl_idname = "SSMTSocketAnimDriver"

        class _FakeLink:
            def __init__(self, from_node, to_node):
                self.from_node = from_node
                self.to_node = to_node
                self.from_socket = _FakeSocket()
                self.to_socket = _FakeSocket()

        links = [
            _FakeLink(runtime_b, play_node),
            _FakeLink(runtime_a, play_node),
        ]
        node_group = types.SimpleNamespace(name="Tree", links=links)
        runtime_a.id_data = node_group
        runtime_b.id_data = node_group
        play_node.id_data = node_group

        self.assertIs(play_node._find_runtime_node(), runtime_a)
        node_group.links = list(reversed(links))
        self.assertIs(play_node._find_runtime_node(), runtime_a)

    def test_get_chain_links_matches_links_by_socket_type_not_name(self):
        upstream_node = types.SimpleNamespace(name="Up")
        downstream_node = types.SimpleNamespace(name="Down")

        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.name = "Self"

        class _FakeSocket:
            def __init__(self, name):
                self.bl_idname = "SSMTSocketAnimDriver"
                self.name = name

        class _FakeLink:
            def __init__(self, from_node, to_node, from_name, to_name):
                self.from_node = from_node
                self.to_node = to_node
                self.from_socket = _FakeSocket(from_name)
                self.to_socket = _FakeSocket(to_name)

        node.id_data = types.SimpleNamespace(links=[
            _FakeLink(upstream_node, node, "任意输出", "旧时间输入"),
            _FakeLink(node, downstream_node, "旧时间输出", "任意输入"),
        ])

        upstream, downstream = node._get_chain_links()

        self.assertEqual(upstream, [upstream_node])
        self.assertEqual(downstream, [downstream_node])

    def test_forward_play_load_handler_backfills_continuous_index_variable_for_legacy_node(self):
        node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        node.bl_idname = 'SSMTNode_AnimDriver_ForwardPlay'
        node.id_data = None
        node.auto_index = 1
        node.custom_paused_var = ""
        node.use_continuous_shapekey_mode = True
        node.continuous_index_var_initialized = True
        node.driven_variable = ""
        node.driven_variable_list = []
        node.inputs = []
        node.outputs = []
        original_migrate = anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets
        anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets = staticmethod(lambda _node: None)
        node._ensure_initial_visible_continuous_index_variable_name = lambda: setattr(
            node, "custom_continuous_index_variable_name", "continuous_shapekey_frame1"
        ) or setattr(node, "continuous_index_var_initialized", True)

        forward_play_module.bpy.data.node_groups = [
            types.SimpleNamespace(
                bl_idname='SSMTBlueprintTreeType',
                nodes=[node],
            )
        ]

        try:
            forward_play_module._forward_play_load_handler(dummy=None)
        finally:
            anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets = original_migrate

        self.assertEqual(node.custom_paused_var, "$animation_paused1")
        self.assertEqual(node.custom_continuous_index_variable_name, "continuous_shapekey_frame1")
        self.assertTrue(node.continuous_index_var_initialized)

    def test_pingpong_load_handler_backfills_continuous_index_variable_for_legacy_node(self):
        node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        node.bl_idname = 'SSMTNode_AnimDriver_PingPong'
        node.id_data = None
        node.auto_index = 2
        node.custom_paused_var = ""
        node.use_continuous_shapekey_mode = True
        node.continuous_index_var_initialized = True
        node.driven_variable = ""
        node.driven_variable_list = []
        node.inputs = []
        node.outputs = []
        original_migrate = anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets
        anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets = staticmethod(lambda _node: None)
        node._ensure_initial_visible_continuous_index_variable_name = lambda: setattr(
            node, "custom_continuous_index_variable_name", "continuous_shapekey_frame2"
        ) or setattr(node, "continuous_index_var_initialized", True)

        pingpong_module.bpy.data.node_groups = [
            types.SimpleNamespace(
                bl_idname='SSMTBlueprintTreeType',
                nodes=[node],
            )
        ]

        try:
            pingpong_module._pingpong_load_handler(dummy=None)
        finally:
            anim_driver_base.SSMTNode_AnimDriver_Base._migrate_dynamic_sockets = original_migrate

        self.assertEqual(node.custom_paused_var, "$animation_paused2")
        self.assertEqual(node.custom_continuous_index_variable_name, "continuous_shapekey_frame2")
        self.assertTrue(node.continuous_index_var_initialized)

    def test_convert_anim_driver_node_preserves_properties_custom_state_and_links(self):
        class _FakeSocket:
            def __init__(self, node, name, identifier):
                self.node = node
                self.name = name
                self.identifier = identifier
                self.links = []

            @property
            def is_linked(self):
                return bool(self.links)

        class _FakeLink:
            def __init__(self, from_socket, to_socket):
                self.from_node = from_socket.node
                self.from_socket = from_socket
                self.to_node = to_socket.node
                self.to_socket = to_socket

        class _FakeLinks(list):
            def new(self, from_socket, to_socket):
                if getattr(to_socket, "links", None):
                    raise RuntimeError("target socket already linked")
                link = _FakeLink(from_socket, to_socket)
                self.append(link)
                from_socket.links.append(link)
                to_socket.links.append(link)
                return link

            def remove(self, link):
                if link in link.from_socket.links:
                    link.from_socket.links.remove(link)
                if link in link.to_socket.links:
                    link.to_socket.links.remove(link)
                super().remove(link)

            def remove_node(self, node):
                remaining = []
                for link in list(self):
                    if link.from_node == node or link.to_node == node:
                        if link in link.from_socket.links:
                            link.from_socket.links.remove(link)
                        if link in link.to_socket.links:
                            link.to_socket.links.remove(link)
                        continue
                    remaining.append(link)
                self[:] = remaining

        class _FakeNodeCollection(list):
            def __init__(self, tree):
                super().__init__()
                self._tree = tree
                self.active = None

            def new(self, type):
                node = self._tree._new_node_factory(type)
                node.id_data = self._tree
                self.append(node)
                return node

            def remove(self, node):
                self._tree.links.remove_node(node)
                super().remove(node)

        class _FakeLocation:
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def copy(self):
                return _FakeLocation(self.x, self.y)

        class _FakeAnimNode:
            def __init__(self, bl_idname, bl_label, name, input_defs, output_defs):
                self.bl_idname = bl_idname
                self.bl_label = bl_label
                self.name = name
                self.label = f"Label:{name}"
                self.location = _FakeLocation(12, 34)
                self.width = 320
                self.select = False
                self.inputs = [_FakeSocket(self, socket_name, identifier) for socket_name, identifier in input_defs]
                self.outputs = [_FakeSocket(self, socket_name, identifier) for socket_name, identifier in output_defs]
                self._idprops = {}
                self.update_count = 0

            def keys(self):
                return self._idprops.keys()

            def __getitem__(self, key):
                return self._idprops[key]

            def __setitem__(self, key, value):
                self._idprops[key] = value

            def update(self):
                self.update_count += 1

        def _copy_anim_driver_fields(source_node, target_node):
            scalar_fields = [
                "auto_index",
                "frame_start",
                "frame_end",
                "play_total_duration",
                "default_paused",
                "custom_paused_var",
                "reverse_playback",
                "loop_playback",
                "hold_end_value",
                "use_float_interval",
                "use_continuous_shapekey_mode",
                "continuous_target_object",
                "continuous_shape_key_prefix_filter",
                "driven_variable",
                "driven_variable_list_active",
                "continuous_shape_key_items_active",
                "assigned_continuous_index_variable_name",
                "custom_continuous_index_variable_name",
                "continuous_index_var_initialized",
            ]
            for field_name in scalar_fields:
                setattr(target_node, field_name, getattr(source_node, field_name))

            target_node.driven_variable_list = [
                types.SimpleNamespace(variable_name=item.variable_name)
                for item in getattr(source_node, "driven_variable_list", [])
            ]
            target_node.continuous_shape_key_items = [
                types.SimpleNamespace(
                    shape_key_name=item.shape_key_name,
                    variable_name=item.variable_name,
                )
                for item in getattr(source_node, "continuous_shape_key_items", [])
            ]

        upstream_a = _FakeAnimNode("Upstream", "Upstream", "UpstreamA", [], [("链输出", "up_out_0")])
        upstream_b = _FakeAnimNode("Upstream", "Upstream", "UpstreamB", [], [("驱动输出", "up_out_1")])
        downstream_a = _FakeAnimNode("Downstream", "Downstream", "DownstreamA", [("链输入", "down_in_0")], [])
        downstream_b = _FakeAnimNode("Downstream", "Downstream", "DownstreamB", [("时间输入", "down_in_1")], [])

        source_node = _FakeAnimNode(
            "SSMTNode_AnimDriver_ForwardPlay",
            "索引播放",
            "ForwardNode",
            [("链输入", "src_in_0"), ("时间输入", "src_in_1"), ("驱动输入", "src_in_2")],
            [("链输出", "src_out_0"), ("时间输出", "src_out_1")],
        )
        source_frame = object()
        source_node.parent = source_frame
        source_node.select = True
        source_node.auto_index = 7
        source_node.frame_start = 1.5
        source_node.frame_end = 9.5
        source_node.play_total_duration = 2.75
        source_node.default_paused = False
        source_node.custom_paused_var = "$pause_custom"
        source_node.reverse_playback = True
        source_node.loop_playback = True
        source_node.hold_end_value = True
        source_node.use_float_interval = False
        source_node.use_continuous_shapekey_mode = True
        source_node.continuous_target_object = "Body"
        source_node.continuous_shape_key_prefix_filter = "Talk_"
        source_node.driven_variable = "$legacy_var"
        source_node.driven_variable_list = [
            types.SimpleNamespace(variable_name="$var_a"),
            types.SimpleNamespace(variable_name="$var_b"),
        ]
        source_node.driven_variable_list_active = 1
        source_node.continuous_shape_key_items = [
            types.SimpleNamespace(shape_key_name="Talk_001", variable_name="$Freq_Talk_001"),
            types.SimpleNamespace(shape_key_name="Talk_002", variable_name="$Freq_Talk_002"),
        ]
        source_node.continuous_shape_key_items_active = 1
        source_node.assigned_continuous_index_variable_name = "continuous_shapekey_frame7"
        source_node.custom_continuous_index_variable_name = "custom_continuous_frame7"
        source_node.continuous_index_var_initialized = True
        source_node[anim_driver_base.SSMTNode_AnimDriver_Base.PLAY_STATE_MIGRATION_KEY] = True

        converted_node = _FakeAnimNode(
            "SSMTNode_AnimDriver_PingPong",
            "往返播放",
            "ConvertedNode",
            [("链输入", "dst_in_a"), ("时间输入", "dst_in_b"), ("驱动输入", "dst_in_c")],
            [("链输出", "dst_out_a"), ("时间输出", "dst_out_b")],
        )
        converted_node.driven_variable_list = []
        converted_node.continuous_shape_key_items = []

        class _FakeTree:
            def __init__(self):
                self.bl_idname = "SSMTBlueprintTreeType"
                self.links = _FakeLinks()
                self.nodes = _FakeNodeCollection(self)

            def _new_node_factory(self, type_name):
                self.last_new_type = type_name
                return converted_node

        tree = _FakeTree()
        for node in (upstream_a, upstream_b, source_node, downstream_a, downstream_b):
            node.id_data = tree
            tree.nodes.append(node)

        tree.links.new(upstream_a.outputs[0], source_node.inputs[0])
        tree.links.new(upstream_b.outputs[0], source_node.inputs[2])
        tree.links.new(source_node.outputs[0], downstream_a.inputs[0])
        tree.links.new(source_node.outputs[1], downstream_b.inputs[0])

        original_selector = node_menu_module._get_selected_anim_driver_convertible_node
        original_copy = node_menu_module._copy_scalar_and_collection_properties
        node_menu_module._get_selected_anim_driver_convertible_node = lambda _tree: source_node
        node_menu_module._copy_scalar_and_collection_properties = _copy_anim_driver_fields

        try:
            operator = node_menu_module.SSMT_OT_ConvertAnimDriverNode()
            operator.report = lambda *_args, **_kwargs: None
            context = types.SimpleNamespace(
                space_data=types.SimpleNamespace(
                    edit_tree=tree,
                    node_tree=None,
                )
            )

            result = operator.execute(context)
        finally:
            node_menu_module._get_selected_anim_driver_convertible_node = original_selector
            node_menu_module._copy_scalar_and_collection_properties = original_copy

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(tree.last_new_type, "SSMTNode_AnimDriver_PingPong")
        self.assertNotIn(source_node, tree.nodes)
        self.assertIn(converted_node, tree.nodes)
        self.assertEqual(converted_node.name, "往返播放")
        self.assertEqual(converted_node.label, "往返播放")
        self.assertIs(converted_node.parent, source_frame)
        self.assertTrue(converted_node.select)
        self.assertIs(tree.nodes.active, converted_node)
        self.assertEqual(converted_node.update_count, 1)

        self.assertEqual(converted_node.auto_index, 7)
        self.assertEqual(converted_node.frame_start, 1.5)
        self.assertEqual(converted_node.frame_end, 9.5)
        self.assertEqual(converted_node.play_total_duration, 2.75)
        self.assertFalse(converted_node.default_paused)
        self.assertEqual(converted_node.custom_paused_var, "$pause_custom")
        self.assertTrue(converted_node.reverse_playback)
        self.assertTrue(converted_node.loop_playback)
        self.assertTrue(converted_node.hold_end_value)
        self.assertFalse(converted_node.use_float_interval)
        self.assertTrue(converted_node.use_continuous_shapekey_mode)
        self.assertEqual(converted_node.continuous_target_object, "Body")
        self.assertEqual(converted_node.continuous_shape_key_prefix_filter, "Talk_")
        self.assertEqual(converted_node.driven_variable, "$legacy_var")
        self.assertEqual([item.variable_name for item in converted_node.driven_variable_list], ["$var_a", "$var_b"])
        self.assertEqual(converted_node.driven_variable_list_active, 1)
        self.assertEqual(
            [(item.shape_key_name, item.variable_name) for item in converted_node.continuous_shape_key_items],
            [("Talk_001", "$Freq_Talk_001"), ("Talk_002", "$Freq_Talk_002")],
        )
        self.assertEqual(converted_node.continuous_shape_key_items_active, 1)
        self.assertEqual(converted_node.assigned_continuous_index_variable_name, "continuous_shapekey_frame7")
        self.assertEqual(converted_node.custom_continuous_index_variable_name, "custom_continuous_frame7")
        self.assertTrue(converted_node.continuous_index_var_initialized)
        self.assertTrue(
            converted_node[anim_driver_base.SSMTNode_AnimDriver_Base.PLAY_STATE_MIGRATION_KEY]
        )

        self.assertEqual(len(tree.links), 4)
        self.assertIs(converted_node.inputs[0].links[0].from_socket, upstream_a.outputs[0])
        self.assertIs(converted_node.inputs[2].links[0].from_socket, upstream_b.outputs[0])
        self.assertIs(downstream_a.inputs[0].links[0].from_socket, converted_node.outputs[0])
        self.assertIs(downstream_b.inputs[0].links[0].from_socket, converted_node.outputs[1])

    def test_convert_anim_driver_node_rolls_back_when_link_migration_fails(self):
        class _FakeSocket:
            def __init__(self, node):
                self.node = node
                self.links = []

            @property
            def is_linked(self):
                return bool(self.links)

        class _FakeLink:
            def __init__(self, from_socket, to_socket):
                self.from_node = from_socket.node
                self.from_socket = from_socket
                self.to_node = to_socket.node
                self.to_socket = to_socket

        class _FakeLinks(list):
            def __init__(self, converted_node):
                super().__init__()
                self.converted_node = converted_node
                self.fail_migration = False

            def new(self, from_socket, to_socket):
                if self.fail_migration and (
                    from_socket.node is self.converted_node
                    or to_socket.node is self.converted_node
                ):
                    self.fail_migration = False
                    raise RuntimeError("injected link failure")
                link = _FakeLink(from_socket, to_socket)
                self.append(link)
                from_socket.links.append(link)
                to_socket.links.append(link)
                return link

            def remove(self, link):
                link.from_socket.links.remove(link)
                link.to_socket.links.remove(link)
                super().remove(link)

            def remove_node(self, node):
                for link in list(self):
                    if link.from_node is node or link.to_node is node:
                        self.remove(link)

        class _FakeNode:
            def __init__(self, bl_idname):
                self.bl_idname = bl_idname
                self.bl_label = bl_idname
                self.name = bl_idname
                self.label = ""
                self.location = (0, 0)
                self.width = 240
                self.select = False
                self.inputs = [_FakeSocket(self)]
                self.outputs = [_FakeSocket(self)]

            def keys(self):
                return []

        source_node = _FakeNode("SSMTNode_AnimDriver_ForwardPlay")
        converted_node = _FakeNode("SSMTNode_AnimDriver_PingPong")
        upstream_node = _FakeNode("Upstream")
        downstream_node = _FakeNode("Downstream")

        class _FakeNodes(list):
            def __init__(self, tree):
                super().__init__()
                self.tree = tree
                self.active = source_node

            def new(self, type):
                self.append(converted_node)
                return converted_node

            def remove(self, node):
                self.tree.links.remove_node(node)
                super().remove(node)

        tree = types.SimpleNamespace(bl_idname="SSMTBlueprintTreeType")
        tree.links = _FakeLinks(converted_node)
        tree.nodes = _FakeNodes(tree)
        tree.nodes.extend([upstream_node, source_node, downstream_node])
        tree.links.new(upstream_node.outputs[0], source_node.inputs[0])
        tree.links.new(source_node.outputs[0], downstream_node.inputs[0])
        tree.links.fail_migration = True

        original_selector = node_menu_module._get_selected_anim_driver_convertible_node
        original_copy = node_menu_module._copy_scalar_and_collection_properties
        original_sync = node_menu_module._sync_anim_driver_conversion_state
        node_menu_module._get_selected_anim_driver_convertible_node = lambda _tree: source_node
        node_menu_module._copy_scalar_and_collection_properties = lambda *_args: None
        node_menu_module._sync_anim_driver_conversion_state = lambda *_args: None

        try:
            operator = node_menu_module.SSMT_OT_ConvertAnimDriverNode()
            reports = []
            operator.report = lambda kinds, message: reports.append((kinds, message))
            context = types.SimpleNamespace(
                space_data=types.SimpleNamespace(edit_tree=tree, node_tree=None)
            )

            result = operator.execute(context)
        finally:
            node_menu_module._get_selected_anim_driver_convertible_node = original_selector
            node_menu_module._copy_scalar_and_collection_properties = original_copy
            node_menu_module._sync_anim_driver_conversion_state = original_sync

        self.assertEqual(result, {'CANCELLED'})
        self.assertIn(source_node, tree.nodes)
        self.assertNotIn(converted_node, tree.nodes)
        self.assertEqual(len(tree.links), 2)
        self.assertIs(source_node.inputs[0].links[0].from_socket, upstream_node.outputs[0])
        self.assertIs(downstream_node.inputs[0].links[0].from_socket, source_node.outputs[0])
        self.assertTrue(any("已回滚" in message for _kinds, message in reports))

    def test_convert_anim_driver_node_pingpong_to_forward_preserves_driven_variables(self):
        class _FakeSocket:
            def __init__(self, node, name, identifier):
                self.node = node
                self.name = name
                self.identifier = identifier
                self.links = []

            @property
            def is_linked(self):
                return bool(self.links)

        class _FakeLink:
            def __init__(self, from_socket, to_socket):
                self.from_node = from_socket.node
                self.from_socket = from_socket
                self.to_node = to_socket.node
                self.to_socket = to_socket

        class _FakeLinks(list):
            def new(self, from_socket, to_socket):
                if getattr(to_socket, "links", None):
                    raise RuntimeError("target socket already linked")
                link = _FakeLink(from_socket, to_socket)
                self.append(link)
                from_socket.links.append(link)
                to_socket.links.append(link)
                return link

            def remove(self, link):
                if link in link.from_socket.links:
                    link.from_socket.links.remove(link)
                if link in link.to_socket.links:
                    link.to_socket.links.remove(link)
                super().remove(link)

            def remove_node(self, node):
                remaining = []
                for link in list(self):
                    if link.from_node == node or link.to_node == node:
                        if link in link.from_socket.links:
                            link.from_socket.links.remove(link)
                        if link in link.to_socket.links:
                            link.to_socket.links.remove(link)
                        continue
                    remaining.append(link)
                self[:] = remaining

        class _FakeNodeCollection(list):
            def __init__(self, tree):
                super().__init__()
                self._tree = tree
                self.active = None

            def new(self, type):
                node = self._tree._new_node_factory(type)
                node.id_data = self._tree
                self.append(node)
                return node

            def remove(self, node):
                self._tree.links.remove_node(node)
                super().remove(node)

        class _FakeLocation:
            def __init__(self, x, y):
                self.x = x
                self.y = y

            def copy(self):
                return _FakeLocation(self.x, self.y)

        class _FakeAnimNode:
            def __init__(self, bl_idname, bl_label, name, input_defs, output_defs):
                self.bl_idname = bl_idname
                self.bl_label = bl_label
                self.name = name
                self.label = f"Label:{name}"
                self.location = _FakeLocation(40, 80)
                self.width = 300
                self.select = False
                self.inputs = [_FakeSocket(self, socket_name, identifier) for socket_name, identifier in input_defs]
                self.outputs = [_FakeSocket(self, socket_name, identifier) for socket_name, identifier in output_defs]
                self._idprops = {}
                self.update_count = 0

            def keys(self):
                return self._idprops.keys()

            def __getitem__(self, key):
                return self._idprops[key]

            def __setitem__(self, key, value):
                self._idprops[key] = value

            def update(self):
                self.update_count += 1

        def _copy_anim_driver_fields(source_node, target_node):
            scalar_fields = [
                "auto_index",
                "frame_start",
                "frame_end",
                "play_total_duration",
                "default_paused",
                "custom_paused_var",
                "reverse_playback",
                "loop_playback",
                "hold_end_value",
                "use_float_interval",
                "use_continuous_shapekey_mode",
                "continuous_target_object",
                "continuous_shape_key_prefix_filter",
                "driven_variable",
                "driven_variable_list_active",
                "continuous_shape_key_items_active",
                "assigned_continuous_index_variable_name",
                "custom_continuous_index_variable_name",
                "continuous_index_var_initialized",
            ]
            for field_name in scalar_fields:
                setattr(target_node, field_name, getattr(source_node, field_name))

            target_node.driven_variable_list = [
                types.SimpleNamespace(variable_name=item.variable_name)
                for item in getattr(source_node, "driven_variable_list", [])
            ]
            target_node.continuous_shape_key_items = [
                types.SimpleNamespace(
                    shape_key_name=item.shape_key_name,
                    variable_name=item.variable_name,
                )
                for item in getattr(source_node, "continuous_shape_key_items", [])
            ]

        source_node = _FakeAnimNode(
            "SSMTNode_AnimDriver_PingPong",
            "往返播放",
            "PingPongNode",
            [("链输入", "src_in_0"), ("时间输入", "src_in_1"), ("驱动输入", "src_in_2")],
            [("链输出", "src_out_0"), ("时间输出", "src_out_1")],
        )
        source_node.select = True
        source_node.auto_index = 3
        source_node.frame_start = 0.0
        source_node.frame_end = 12.0
        source_node.play_total_duration = 1.25
        source_node.default_paused = True
        source_node.custom_paused_var = "$paused_pp"
        source_node.reverse_playback = False
        source_node.loop_playback = False
        source_node.hold_end_value = True
        source_node.use_float_interval = True
        source_node.use_continuous_shapekey_mode = False
        source_node.continuous_target_object = ""
        source_node.continuous_shape_key_prefix_filter = ""
        source_node.driven_variable = "$legacy_pingpong"
        source_node.driven_variable_list = [
            types.SimpleNamespace(variable_name="$driver_1"),
            types.SimpleNamespace(variable_name="$driver_2"),
            types.SimpleNamespace(variable_name="$driver_3"),
        ]
        source_node.driven_variable_list_active = 2
        source_node.continuous_shape_key_items = []
        source_node.continuous_shape_key_items_active = 0
        source_node.assigned_continuous_index_variable_name = "continuous_shapekey_frame3"
        source_node.custom_continuous_index_variable_name = "continuous_shapekey_frame3"
        source_node.continuous_index_var_initialized = True

        converted_node = _FakeAnimNode(
            "SSMTNode_AnimDriver_ForwardPlay",
            "索引播放",
            "ConvertedForward",
            [("链输入", "dst_in_0"), ("时间输入", "dst_in_1"), ("驱动输入", "dst_in_2")],
            [("链输出", "dst_out_0"), ("时间输出", "dst_out_1")],
        )
        converted_node.driven_variable_list = []
        converted_node.continuous_shape_key_items = []

        class _FakeTree:
            def __init__(self):
                self.bl_idname = "SSMTBlueprintTreeType"
                self.links = _FakeLinks()
                self.nodes = _FakeNodeCollection(self)

            def _new_node_factory(self, type_name):
                self.last_new_type = type_name
                return converted_node

        tree = _FakeTree()
        source_node.id_data = tree
        tree.nodes.append(source_node)

        original_selector = node_menu_module._get_selected_anim_driver_convertible_node
        original_copy = node_menu_module._copy_scalar_and_collection_properties
        node_menu_module._get_selected_anim_driver_convertible_node = lambda _tree: source_node
        node_menu_module._copy_scalar_and_collection_properties = _copy_anim_driver_fields

        try:
            operator = node_menu_module.SSMT_OT_ConvertAnimDriverNode()
            operator.report = lambda *_args, **_kwargs: None
            context = types.SimpleNamespace(
                space_data=types.SimpleNamespace(
                    edit_tree=tree,
                    node_tree=None,
                )
            )

            result = operator.execute(context)
        finally:
            node_menu_module._get_selected_anim_driver_convertible_node = original_selector
            node_menu_module._copy_scalar_and_collection_properties = original_copy

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(tree.last_new_type, "SSMTNode_AnimDriver_ForwardPlay")
        self.assertEqual(converted_node.name, "索引播放")
        self.assertEqual(converted_node.label, "索引播放")
        self.assertEqual(converted_node.bl_idname, "SSMTNode_AnimDriver_ForwardPlay")
        self.assertEqual(converted_node.driven_variable, "$legacy_pingpong")
        self.assertEqual(
            [item.variable_name for item in converted_node.driven_variable_list],
            ["$driver_1", "$driver_2", "$driver_3"],
        )
        self.assertEqual(converted_node.driven_variable_list_active, 2)
        self.assertEqual(converted_node.frame_start, 0.0)
        self.assertEqual(converted_node.frame_end, 12.0)
        self.assertEqual(converted_node.play_total_duration, 1.25)
        self.assertTrue(converted_node.default_paused)
        self.assertEqual(converted_node.custom_paused_var, "$paused_pp")
        self.assertFalse(converted_node.loop_playback)
        self.assertTrue(converted_node.hold_end_value)
        self.assertEqual(converted_node.update_count, 1)

    def test_convertible_node_selection_prefers_active_node(self):
        active_node = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_PingPong",
            select=True,
        )
        other_selected_node = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ForwardPlay",
            select=True,
        )
        node_tree = types.SimpleNamespace(
            nodes=types.SimpleNamespace(
                active=active_node,
                __iter__=lambda self: iter([active_node, other_selected_node]),
            )
        )

        class _IterableNodes:
            def __init__(self, active, nodes):
                self.active = active
                self._nodes = nodes

            def __iter__(self):
                return iter(self._nodes)

        node_tree.nodes = _IterableNodes(active_node, [active_node, other_selected_node])

        result = node_menu_module._get_selected_anim_driver_convertible_node(node_tree)

        self.assertIs(result, active_node)

    def test_convertible_node_selection_ignores_deselected_active_node(self):
        stale_active_node = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_PingPong",
            select=False,
        )
        selected_node = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ForwardPlay",
            select=True,
        )

        class _IterableNodes:
            def __init__(self):
                self.active = stale_active_node

            def __iter__(self):
                return iter([stale_active_node, selected_node])

        node_tree = types.SimpleNamespace(nodes=_IterableNodes())

        result = node_menu_module._get_selected_anim_driver_convertible_node(node_tree)

        self.assertIs(result, selected_node)

    def test_runtime_and_pingpong_segments_merge_with_balanced_conditionals(self):
        runtime_node = runtime_module.SSMTNode_AnimDriver_Runtime()
        runtime_node.name = "Runtime"
        runtime_node.auto_index = 1
        runtime_node.fps = 30
        runtime_node.playback_rate = 1

        pingpong_node = pingpong_module.SSMTNode_AnimDriver_PingPong()
        pingpong_node.name = "PingPong"
        pingpong_node.auto_index = 2
        pingpong_node.frame_start = 0.0
        pingpong_node.frame_end = 3.0
        pingpong_node.play_total_duration = 0.1
        pingpong_node.use_float_interval = True
        pingpong_node.default_paused = True
        pingpong_node.custom_paused_var = "$paused"
        pingpong_node.reverse_playback = False
        pingpong_node.loop_playback = False
        pingpong_node.driven_variable_list = [types.SimpleNamespace(variable_name="$varA")]
        pingpong_node.driven_variable = ""

        class _FakeSocket:
            def __init__(self, name):
                self.bl_idname = "SSMTSocketAnimDriver"
                self.name = name

        class _FakeLink:
            def __init__(self, from_node, to_node, from_name, to_name):
                self.from_node = from_node
                self.to_node = to_node
                self.from_socket = _FakeSocket(from_name)
                self.to_socket = _FakeSocket(to_name)

        node_group = types.SimpleNamespace(
            nodes=[runtime_node, pingpong_node],
            links=[
                _FakeLink(runtime_node, pingpong_node, "链输出", "链输入"),
                _FakeLink(runtime_node, pingpong_node, "链输出", "时间输入"),
            ],
        )
        runtime_node.id_data = node_group
        pingpong_node.id_data = node_group

        collector_module = _load_blueprint_module("anim_driver_collector")
        merged = collector_module.AnimationDriverCollector(node_group).collect()[0]["ini_content"]

        _assert_balanced_conditionals(self, merged)
        # 运行时间节点只发自己的预分配帧变量（不再有共享的 $fps / $swapvar）
        self.assertIn("global persist $anim_frame1 = 0", merged)
        self.assertIn("$anim_frame1 = (time * 30) // 1", merged)
        self.assertNotIn("$swapvar", merged)
        self.assertNotIn("$fps", merged)

    def test_toggle_comment_is_emitted_into_ini(self):
        node = toggle_module.SSMTNode_AnimDriver_Toggle()
        node.name = "Toggle"
        node.auto_index = 2
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.key_binding = "no_modifiers k"
        node.switch_type = "cycle"
        node.toggle_values = "0,1"
        node.comment = "切换测试"
        node.pause_target_list = [types.SimpleNamespace(variable_name="$animation_paused2")]

        ini = node.generate_ini_segment()

        self.assertIn("; 切换测试", ini)
        self.assertIn("[KeyToggle_Anim2]", ini)
        self.assertIn("[KeyToggle_Anim2]\n; 切换测试\ncondition = $active0 == 1\nkey = no_modifiers k\ntype = cycle\n$animation_paused2 = 0,1", ini)
        self.assertIn("condition = $active0 == 1", ini)
        self.assertIn("key = no_modifiers k", ini)
        self.assertIn("type = cycle", ini)
        self.assertLess(ini.index("condition = $active0 == 1"), ini.index("key = no_modifiers k"))
        self.assertLess(ini.index("key = no_modifiers k"), ini.index("type = cycle"))
        self.assertIn("$animation_paused2 = 0,1", ini)

    def test_toggle_uses_ntmi_active_flag_for_ntemi_logic(self):
        anim_driver_base.GlobalConfig.logic_name = "NTEMI"
        try:
            node = toggle_module.SSMTNode_AnimDriver_Toggle()
            node.name = "Toggle"
            node.auto_index = 2
            node.id_data = types.SimpleNamespace(nodes=[node], links=[])
            node.key_binding = "no_modifiers k"
            node.switch_type = "cycle"
            node.toggle_values = "0,1"
            node.comment = ""
            node.pause_target_list = [types.SimpleNamespace(variable_name="$animation_paused2")]

            ini = node.generate_ini_segment()
        finally:
            anim_driver_base.GlobalConfig.logic_name = ""

        self.assertIn("condition = $ntmi_active0 == 1", ini)

    def test_anim_driver_default_variables_are_unique_within_same_tree(self):
        tree = types.SimpleNamespace(bl_idname='SSMTBlueprintTreeType', nodes=[])

        forward_node = forward_play_module.SSMTNode_AnimDriver_ForwardPlay()
        forward_node.name = "Forward"
        forward_node.id_data = tree
        forward_node.inputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        forward_node.outputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        tree.nodes.append(forward_node)
        forward_node.init(context=None)

        seq_node = shapekey_seq_module.SSMTNode_AnimDriver_ShapeKeySequence()
        seq_node.name = "Seq"
        seq_node.id_data = tree
        seq_node.inputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        seq_node.outputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        tree.nodes.append(seq_node)
        seq_node.init(context=None)

        self.assertNotEqual(forward_node.custom_paused_var, seq_node.custom_paused_var)
        self.assertNotEqual(seq_node.custom_paused_var, seq_node.driven_variable)

    def test_toggle_supports_multiple_hotkeys_separated_by_comma(self):
        node = toggle_module.SSMTNode_AnimDriver_Toggle()
        node.name = "Toggle"
        node.auto_index = 2
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.key_binding = "no_modifiers k, no_modifiers l"
        node.switch_type = "toggle"
        node.toggle_values = "0,1"
        node.comment = ""
        node.pause_target_list = [types.SimpleNamespace(variable_name="$animation_paused2")]

        ini = node.generate_ini_segment()

        self.assertEqual(ini.count("[KeyToggle_Anim2]"), 0)
        self.assertEqual(ini.count("[KeyToggle_Anim2_1]"), 1)
        self.assertEqual(ini.count("[KeyToggle_Anim2_2]"), 1)
        self.assertIn("key = no_modifiers k", ini)
        self.assertIn("key = no_modifiers l", ini)
        self.assertEqual(ini.count("key = "), 2)
        self.assertEqual(ini.count("type = toggle"), 2)
        self.assertEqual(ini.count("$animation_paused2 = 0,1"), 2)
        self.assertIn("[KeyToggle_Anim2_1]\ncondition = $active0 == 1\nkey = no_modifiers k\ntype = toggle\n$animation_paused2 = 0,1", ini)
        self.assertIn("[KeyToggle_Anim2_2]\ncondition = $active0 == 1\nkey = no_modifiers l\ntype = toggle\n$animation_paused2 = 0,1", ini)
        self.assertIn("type = toggle", ini)

    def test_toggle_normalizes_extra_whitespace_in_multiple_hotkeys(self):
        node = toggle_module.SSMTNode_AnimDriver_Toggle()
        node.name = "Toggle"
        node.auto_index = 3
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.key_binding = "  no_modifiers k  ,   no_modifiers l  , "
        node.switch_type = "hold"
        node.toggle_values = "1"
        node.comment = ""
        node.pause_target_list = [types.SimpleNamespace(variable_name="$animation_paused3")]

        ini = node.generate_ini_segment()

        self.assertEqual(ini.count("[KeyToggle_Anim3]"), 0)
        self.assertEqual(ini.count("[KeyToggle_Anim3_1]"), 1)
        self.assertEqual(ini.count("[KeyToggle_Anim3_2]"), 1)
        self.assertIn("key = no_modifiers k", ini)
        self.assertIn("key = no_modifiers l", ini)
        self.assertEqual(ini.count("key = "), 2)
        self.assertEqual(ini.count("type = hold"), 2)

    def test_draw_node_add_menu_uses_layout_for_animation_driver_tree(self):
        calls = []

        class _FakeOperator:
            def __init__(self):
                self.type = None

        class _FakeLayout:
            def operator(self, op_idname, text="", icon=""):
                op = _FakeOperator()
                calls.append((op_idname, text, icon, op))
                return op

            def menu(self, *args, **kwargs):
                calls.append(("menu", args, kwargs))

            def separator(self):
                calls.append(("separator",))

        fake_context = types.SimpleNamespace(
            space_data=types.SimpleNamespace(
                edit_tree=types.SimpleNamespace(
                    bl_idname='SSMTBlueprintTreeType',
                    get=lambda key, default=None: True if key == "is_animation_driver" else default,
                ),
                node_tree=None,
            )
        )
        fake_self = types.SimpleNamespace(layout=_FakeLayout())

        node_menu_module.draw_node_add_menu(fake_self, fake_context)

        self.assertTrue(any(call[1] == "运行时间" for call in calls if call[0] == "node.add_node"))
        self.assertTrue(any(call[1] == "累计触发" for call in calls if call[0] == "node.add_node"))
        self.assertTrue(any(call[1] == "动画驱动开关" for call in calls if call[0] == "node.add_node"))
        self.assertTrue(any(call[1] == "随机驱动" for call in calls if call[0] == "node.add_node"))
        random_entry = next(call for call in calls if call[0] == "node.add_node" and call[1] == "随机驱动")
        self.assertEqual(random_entry[2], "RNDCURVE")
        self.assertEqual(random_entry[3].type, "SSMTNode_AnimDriver_Random")


class RuntimeFrameVariablePreallocationTests(unittest.TestCase):
    """运行时间节点的帧变量预分配（取代硬编码共享的 $swapvar）。"""

    def _make_runtime(self, name, auto_index, tree):
        node = runtime_module.SSMTNode_AnimDriver_Runtime()
        node.name = name
        node.auto_index = auto_index
        node.id_data = tree
        node.fps = 30
        node.playback_rate = 1
        node.assigned_frame_variable_name = ""
        node.custom_frame_variable_name = ""
        node.ensure_frame_variable_name()
        tree.nodes.append(node)
        return node

    def test_two_runtime_nodes_get_distinct_preallocated_variables(self):
        tree = types.SimpleNamespace(name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[], links=[])
        first = self._make_runtime("运行时间", 1, tree)
        second = self._make_runtime("运行时间.001", 2, tree)

        self.assertEqual(first.frame_variable_name(), "anim_frame1")
        self.assertEqual(second.frame_variable_name(), "anim_frame2")

    def test_custom_frame_variable_wins_over_preallocated(self):
        tree = types.SimpleNamespace(name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[], links=[])
        node = self._make_runtime("运行时间", 3, tree)

        node.custom_frame_variable_name = "$my_frame"

        self.assertEqual(node.frame_variable_name(), "my_frame")

    def test_each_runtime_node_only_emits_its_own_frame_variable(self):
        """不再有共享的 $swapvar / $fps 兼容别名：全部走 $anim_frame{序号}。"""
        tree = types.SimpleNamespace(name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[], links=[])
        owner = self._make_runtime("运行时间", 1, tree)
        other = self._make_runtime("运行时间.001", 2, tree)

        owner_segment = owner.generate_ini_segment()
        other_segment = other.generate_ini_segment()
        joined = owner_segment + other_segment

        # 各自声明自己的帧变量，互不重名
        self.assertIn("global persist $anim_frame1 = 0", owner_segment)
        self.assertIn("$anim_frame1 = (time * 30) // 1", owner_segment)
        self.assertIn("global persist $anim_frame2 = 0", other_segment)
        self.assertIn("$anim_frame2 = (time * 30) // 1", other_segment)
        # 旧的共享名彻底消失（既不是别名，也没有 $fps 字面量变量）
        self.assertNotIn("$swapvar", joined)
        self.assertNotIn("$fps", joined)

    def test_frame_variable_of_falls_back_to_unindexed_anim_frame(self):
        self.assertEqual(
            anim_driver_base.SSMTNode_AnimDriver_Base._frame_variable_of(None), "$anim_frame"
        )
        self.assertEqual(
            anim_driver_base.SSMTNode_AnimDriver_Base._frame_variable_of(
                types.SimpleNamespace(custom_frame_variable_name="", assigned_frame_variable_name="")
            ),
            "$anim_frame",
        )
        self.assertEqual(
            anim_driver_base.SSMTNode_AnimDriver_Base._frame_variable_of(
                types.SimpleNamespace(custom_frame_variable_name="", assigned_frame_variable_name="anim_frame2")
            ),
            "$anim_frame2",
        )

    def test_consumer_uses_upstream_runtime_preallocated_variable(self):
        node = accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger()
        node.name = "AccumulativeTrigger"
        node.auto_index = 1
        node.id_data = types.SimpleNamespace(nodes=[node], links=[])
        node.default_paused = True
        node.custom_paused_var = "$acc_paused"
        node.accumulator_variable = "$progress"
        node.condition_list = []
        node.target_list = []
        node._find_runtime_node = lambda: types.SimpleNamespace(
            fps=30,
            playback_rate=2,
            assigned_frame_variable_name="anim_frame2",
            custom_frame_variable_name="",
        )

        ini = node.generate_ini_segment()

        self.assertIn("if $anim_frame2 % $speed_auto1 == 0", ini)
        self.assertNotIn("if $swapvar % $speed_auto1 == 0", ini)


    def test_init_fills_preallocated_frame_variable_into_input_box(self):
        tree = types.SimpleNamespace(
            name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[], links=[]
        )
        node = runtime_module.SSMTNode_AnimDriver_Runtime()
        node.name = "运行时间"
        node.auto_index = 1
        node.id_data = tree
        node.fps = 30
        node.playback_rate = 1
        node.custom_frame_variable_name = ""
        node.assigned_frame_variable_name = ""
        node.frame_var_initialized = False
        node.inputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        node.outputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        tree.nodes.append(node)

        node.init(None)

        self.assertEqual(node.assigned_frame_variable_name, "anim_frame1")
        self.assertEqual(
            node.custom_frame_variable_name, "anim_frame1",
            "预分配帧变量名必须填入输入框（与暂停变量/连续索引变量同规则）",
        )
        self.assertEqual(node.frame_variable_name(), "anim_frame1")

    def test_copy_reallocates_frame_variable(self):
        tree = types.SimpleNamespace(
            name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[], links=[]
        )
        original = self._make_runtime("运行时间", 1, tree)
        duplicate = runtime_module.SSMTNode_AnimDriver_Runtime()
        duplicate.name = "运行时间.001"
        duplicate.id_data = tree
        duplicate.fps = 30
        duplicate.playback_rate = 1
        duplicate.custom_frame_variable_name = original.custom_frame_variable_name
        duplicate.assigned_frame_variable_name = original.assigned_frame_variable_name
        duplicate.frame_var_initialized = True
        tree.nodes.append(duplicate)

        duplicate.copy(original)

        self.assertNotEqual(
            duplicate.assigned_frame_variable_name, original.assigned_frame_variable_name,
            "复制节点必须重新预分配，否则两个节点声明同一个帧变量",
        )
        self.assertEqual(duplicate.custom_frame_variable_name, duplicate.assigned_frame_variable_name)

    def test_accumulative_trigger_init_fills_accumulator_variable(self):
        node = accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger()
        node.name = "AccumulativeTrigger"
        node.auto_index = 1
        tree = types.SimpleNamespace(
            name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[node], links=[]
        )
        node.id_data = tree
        node.custom_paused_var = ""
        node.accumulator_variable = ""
        node.inputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)
        node.outputs = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)

        node.init(None)

        self.assertEqual(
            node.accumulator_variable, "$accumulator1",
            "累计变量名必须填入输入框（此前只有回退 label，输入框始终为空）",
        )

    def test_accumulative_trigger_copy_reallocates_accumulator_variable(self):
        tree = types.SimpleNamespace(
            name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[], links=[]
        )
        original = accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger()
        original.name = "AccumulativeTrigger"
        original.auto_index = 1
        original.id_data = tree
        original.accumulator_variable = "$accumulator1"
        original.custom_paused_var = "$accumulative_trigger_paused1"
        tree.nodes.append(original)

        duplicate = accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger()
        duplicate.name = "AccumulativeTrigger.001"
        duplicate.id_data = tree
        duplicate.accumulator_variable = "$accumulator1"
        duplicate.custom_paused_var = "$accumulative_trigger_paused1"
        tree.nodes.append(duplicate)

        duplicate.copy(original)

        self.assertEqual(duplicate.accumulator_variable, "$accumulator2")
        self.assertEqual(duplicate.custom_paused_var, "$accumulative_trigger_paused2")


    def test_migrate_legacy_frame_variable_renames_old_prefix(self):
        node = runtime_module.SSMTNode_AnimDriver_Runtime()
        node.name = "运行时间"
        node.custom_frame_variable_name = "$swapvar3"
        node.assigned_frame_variable_name = "swapvar3"

        changed = runtime_module.migrate_legacy_frame_variable_name(node)

        self.assertTrue(changed)
        self.assertEqual(node.custom_frame_variable_name, "$anim_frame3")
        self.assertEqual(node.assigned_frame_variable_name, "anim_frame3")

    def test_migrate_legacy_frame_variable_keeps_user_names(self):
        node = runtime_module.SSMTNode_AnimDriver_Runtime()
        node.name = "运行时间"
        node.custom_frame_variable_name = "$swapvar_myframe"
        node.assigned_frame_variable_name = "my_frame"

        changed = runtime_module.migrate_legacy_frame_variable_name(node)

        self.assertFalse(changed, "手改过的名字（含 swapvar_xxx 这种自定名）不能动")
        self.assertEqual(node.custom_frame_variable_name, "$swapvar_myframe")
        self.assertEqual(node.assigned_frame_variable_name, "my_frame")

    def _with_runtime_tree(self, module, node):
        """把节点挂进假蓝图树并装到假 bpy 上，供迁移 sweep 测试使用。

        用被测模块**自己的** ``bpy`` 引用：其它测试模块会各自替换
        ``sys.modules["bpy"]``，读全局那个会与生产代码看到的不是同一个对象。
        """
        tree = types.SimpleNamespace(
            name="动画驱动蓝图", bl_idname='SSMTBlueprintTreeType', nodes=[node], links=[]
        )
        node.id_data = tree
        bpy_module = module.bpy
        previous = list(getattr(bpy_module.data, "node_groups", []) or [])
        bpy_module.data.node_groups = [tree]
        self.addCleanup(lambda: setattr(bpy_module.data, "node_groups", previous))

    def test_register_sweep_renames_legacy_name_and_fills_box(self):
        """回归：重载（load_post / 插件重载）必须把旧节点刷新到当前命名。"""
        node = runtime_module.SSMTNode_AnimDriver_Runtime()
        node.name = "运行时间"
        node.auto_index = 3
        node.fps = 30
        node.playback_rate = 1
        node.custom_frame_variable_name = ""          # 旧版本只写 assigned，没填输入框
        node.assigned_frame_variable_name = "swapvar3"  # 旧前缀
        node.frame_var_initialized = False
        self._with_runtime_tree(runtime_module, node)

        migrated = runtime_module.migrate_existing_runtime_nodes()

        self.assertEqual(migrated, 1)
        self.assertEqual(node.assigned_frame_variable_name, "anim_frame3")
        # 输入框里存的是归一化名（无 $，与连续索引变量同规则；消费侧自己补 $）
        self.assertEqual(node.custom_frame_variable_name, "anim_frame3")

    def test_register_sweep_respects_user_cleared_box(self):
        node = runtime_module.SSMTNode_AnimDriver_Runtime()
        node.name = "运行时间"
        node.auto_index = 3
        node.fps = 30
        node.playback_rate = 1
        node.custom_frame_variable_name = ""            # 用户手动清空过
        node.assigned_frame_variable_name = "anim_frame3"
        node.frame_var_initialized = True               # 守卫已置位 → 不再回填
        self._with_runtime_tree(runtime_module, node)

        migrated = runtime_module.migrate_existing_runtime_nodes()

        self.assertEqual(migrated, 0)
        self.assertEqual(node.custom_frame_variable_name, "")

    def test_register_sweep_fills_accumulator_variable(self):
        node = accumulative_trigger_module.SSMTNode_AnimDriver_AccumulativeTrigger()
        node.name = "AccumulativeTrigger"
        node.auto_index = 1
        node.custom_paused_var = "$accumulative_trigger_paused1"
        node.accumulator_variable = ""                  # 旧节点：输入框空
        self._with_runtime_tree(accumulative_trigger_module, node)

        migrated = accumulative_trigger_module.migrate_existing_accumulative_nodes()

        self.assertEqual(migrated, 1)
        self.assertEqual(node.accumulator_variable, "$accumulator1")


if __name__ == "__main__":
    unittest.main()
