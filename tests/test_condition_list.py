# -*- coding: utf-8 -*-
"""条件列表（哈希 + 变量，多条件「与」）测试。

被测文件默认是同目录下的 ``node_postprocess_condition.py``（实现子体的 work
副本）；这个文件不存在时回退到仓库的 ``blueprint/node_postprocess_condition.py``。
也可以用环境变量 ``SSMT_CONDITION_MODULE`` 显式指定。

覆盖：
- 单哈希条件金样：旧参数路径 == 一条 HASH 条件的列表路径 == 冻结常量，且连跑
  两次字节不变（旧工程回归底线）；
- 变量条件：单条件生成一行 if，不生成 [Constants] global / 检测段 / [Present]
  复位，也不新建 [Present]；``!=`` 同理；变量名自动补 ``$``；
- 哈希 + 变量：合并成一条 ``&&`` 表达式；[Present] 只复位 cond_ 变量；
- 两条哈希：h1 槽位命名、两个检测段、两组 global / post、if 里两个 clause；
- 非法变量名 / 空数值：execute_postprocess 返回 False，配置表字节不变；
- 旧三属性迁移成一条 HASH 条件并清空旧值；
- 条件改动后的剥离：旧 ``&&`` 表达式 / 多哈希 detect 行必须被剥掉、endif 配平。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent


def _find_repo_root(start):
    for candidate in (start, *start.parents):
        if (candidate / "blueprint" / "node_postprocess_base.py").is_file():
            return candidate
    raise RuntimeError(f"找不到仓库根目录（从 {start} 往上没有 blueprint/node_postprocess_base.py）")


REPO_ROOT = _find_repo_root(TEST_DIR)


def _module_file():
    override = os.environ.get("SSMT_CONDITION_MODULE", "")
    if override and Path(override).is_file():
        return Path(override)
    sibling = TEST_DIR / "node_postprocess_condition.py"
    if sibling.is_file():
        return sibling
    return REPO_ROOT / "blueprint" / "node_postprocess_condition.py"


MODULE_FILE = _module_file()

PKG = "_condition_list_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


# ---------------------------------------------------------------------------
# 假 bpy（与 tests/test_node_postprocess_condition.py 同一套最小桩）
# ---------------------------------------------------------------------------

class _FakePropertyGroup:
    pass


class _FakeUIList:
    pass


class _FakeOperator:
    def report(self, _kind, _message):
        return None


def _build_fake_bpy():
    bpy_types = types.ModuleType("bpy.types")
    bpy_types.Operator = _FakeOperator
    bpy_types.Node = type("Node", (), {})
    bpy_types.NodeSocket = type("NodeSocket", (), {})
    bpy_types.PropertyGroup = _FakePropertyGroup
    bpy_types.UIList = _FakeUIList

    bpy_props = types.ModuleType("bpy.props")
    bpy_props.StringProperty = lambda **kwargs: kwargs.get("default", "")
    bpy_props.IntProperty = lambda **kwargs: kwargs.get("default", 0)
    bpy_props.BoolProperty = lambda **kwargs: kwargs.get("default", False)
    bpy_props.EnumProperty = lambda **kwargs: kwargs.get("default", "")
    bpy_props.CollectionProperty = lambda **kwargs: []

    bpy_utils = types.ModuleType("bpy.utils")
    bpy_utils.register_class = lambda _cls: None
    bpy_utils.unregister_class = lambda _cls: None

    bpy_module = types.ModuleType("bpy")
    bpy_module.types = bpy_types
    bpy_module.props = bpy_props
    bpy_module.utils = bpy_utils
    bpy_module.data = types.SimpleNamespace(texts=types.SimpleNamespace(get=lambda _name: None))
    bpy_module.ops = types.SimpleNamespace()
    bpy_module.context = types.SimpleNamespace(window_manager=None)
    return bpy_module, {
        "bpy.types": bpy_types,
        "bpy.props": bpy_props,
        "bpy.utils": bpy_utils,
    }


_fake_bpy, _fake_bpy_submodules = _build_fake_bpy()


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _RecordingLog:
    def __init__(self):
        self.records = []

    def _record(self, level, message):
        self.records.append((level, str(message)))

    def info(self, message, *_args, **_kwargs):
        self._record("info", message)

    def warning(self, message, *_args, **_kwargs):
        self._record("warning", message)

    def error(self, message, *_args, **_kwargs):
        self._record("error", message)

    def debug(self, message, *_args, **_kwargs):
        self._record("debug", message)

    def texts(self, level=None):
        return [text for lvl, text in self.records if level is None or lvl == level]


class _ExportState:
    target_ini = ""
    model = None
    stages = []


class _FakeCommentCleanup:
    @staticmethod
    def _find_target_ini_file(_mod_export_path):
        if _ExportState.target_ini:
            return _ExportState.target_ini, ""
        return "", "目录里没有 .ini 配置表"


class _FakeExportHelper:
    @staticmethod
    def get_current_blueprint_model():
        return _ExportState.model


class _FakeExportProgress:
    @staticmethod
    def report_stage(text):
        _ExportState.stages.append(str(text))


class _FakeRegistry:
    def __init__(self, entries):
        self.entries = [dict(entry) for entry in entries]

    def get_sections_for_draw_ib(self, draw_ib, ini_path=""):
        wanted = str(draw_ib or "").casefold()
        return [dict(e) for e in self.entries if str(e.get("draw_ib", "")).casefold() == wanted]

    def get_sections_for_draw_ibs(self, draw_ibs, ini_path=""):
        wanted = {str(value or "").casefold() for value in (draw_ibs or [])}
        return [dict(e) for e in self.entries if str(e.get("draw_ib", "")).casefold() in wanted]

    def get_all(self):
        return {}

    def get_ini_paths(self):
        return []


_SAVED_BPY_ENTRIES = {name: sys.modules.get(name) for name in _fake_bpy_submodules}
_SAVED_BPY = sys.modules.get("bpy")
sys.modules["bpy"] = _fake_bpy
sys.modules.update(_fake_bpy_submodules)

_log = _RecordingLog()

try:
    _install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)
    _install_module(f"{PKG}.utils.log_utils", LOG=_log)
    _install_module(f"{PKG}.utils.format_utils", Fatal=Exception)

    base_module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_base",
        REPO_ROOT / "blueprint" / "node_postprocess_base.py",
    )
    skip_module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_skip",
        REPO_ROOT / "blueprint" / "node_postprocess_skip.py",
    )
    _install_module(
        f"{PKG}.blueprint.node_postprocess_comment_cleanup",
        SSMTNode_PostProcess_CommentCleanup=_FakeCommentCleanup,
    )
    _install_module(
        f"{PKG}.blueprint.export_helper",
        BlueprintExportHelper=_FakeExportHelper,
    )
    _install_module(
        f"{PKG}.blueprint.export_progress",
        report_stage=_FakeExportProgress.report_stage,
    )
    module = _load_real_module(
        f"{PKG}.blueprint.node_postprocess_condition",
        MODULE_FILE,
    )
finally:
    for _name, _previous in _SAVED_BPY_ENTRIES.items():
        if _previous is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _previous
    if _SAVED_BPY is None:
        sys.modules.pop("bpy", None)
    else:
        sys.modules["bpy"] = _SAVED_BPY


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

OBJECT_NAME = "LOD0.7fbbcf0d-1260-9402.脸+眼"
TARGET_SECTION = "TextureOverride_IB_7fbbcf0d"
DRAW_IB = "7fbbcf0d"
NS = "abcd1234"
DETECT_HASH = "92cb56c9"

INI_WITH_PRESENT = (
    "[Constants]\n"
    "global persist $mod_enabled = 1\n"
    "\n"
    "[TextureOverride_IB_7fbbcf0d]\n"
    "hash = 7fbbcf0d\n"
    "drawindexed = 1260\n"
    "\n"
    "[Present]\n"
    "post $some_other = 1\n"
)

#: 07a：无 [Present] + 尾块带段头（`子体-解析/samples/07a_*.ini`）。
SAMPLE_07A_TAIL_SECTION = (
    "[Constants]\n"
    "global persist $x = 1\n"
    "\n"
    "[TextureOverride_IB_7fbbcf0d]\n"
    "hash = 7fbbcf0d\n"
    "drawindexed = 1260\n"
    "\n"
    "; --- AUTO-APPENDED CUSTOM TEXT 01 BEGIN ---\n"
    ";==============================================================================\n"
    "[TextureOverride_IB_abcdef12]\n"
    "hash = abcdef12\n"
    "drawindexed = 5\n"
    "; --- AUTO-APPENDED CUSTOM TEXT 01 END ---\n"
)

#: 07b：无 [Present] + 裸尾块（没有段头，`子体-解析/samples/07b_*.ini`）。
SAMPLE_07B_BARE_TAIL = (
    "[Constants]\n"
    "global persist $x = 1\n"
    "\n"
    "[TextureOverride_IB_7fbbcf0d]\n"
    "hash = 7fbbcf0d\n"
    "drawindexed = 1260\n"
    "\n"
    "; --- AUTO-APPENDED CUSTOM TEXT 02 BEGIN ---\n"
    "post $custom = 1\n"
    "; --- AUTO-APPENDED CUSTOM TEXT 02 END ---\n"
)

#: C7a：目标段在最前、唯一 [Constants] 在尾块里、无 [Present]
#: （`子体-幂等/test_idempotent.py:817-832`）。本节点必须在尾块之前另建
#: [Constants]，否则第二次执行时 [Present] 会把 [Constants] 段体劈开、
#: global/检测段落点漂移。
SAMPLE_C7A_CONSTANTS_IN_TAIL = (
    f"[{TARGET_SECTION}]\n"
    "hash = 7fbbcf0d\n"
    "drawindexed = 1260\n"
    "\n"
    "; --- AUTO-APPENDED CUSTOM TEXT 01 BEGIN ---\n"
    ";==============================================================================\n"
    "[Constants]\n"
    "global $panel_alpha = 1\n"
    "[TextureOverride_UI_Panel]\n"
    "hash = 11111111\n"
    "drawindexed = 4\n"
    ";==============================================================================\n"
    "; --- AUTO-APPENDED CUSTOM TEXT 01 END ---\n"
)

#: 用**改动前**的代码对 INI_WITH_PRESENT + 单哈希跑出来的输出，冻结成常量。
_GOLDEN_LINES = (
    "[Constants]",
    "; @@COND:abcd1234:global@@",
    "global $cond_abcd1234_cond_active",
    "global $cond_abcd1234_cond_frame",
    "global persist $mod_enabled = 1",
    "; @@COND:abcd1234:check-begin@@",
    "[TextureOverrideCheckHash_cond_abcd1234]",
    "hash = 92cb56c9",
    "$cond_abcd1234_cond_frame = 1",
    "; @@COND:abcd1234:check-end@@",
    "",
    "[TextureOverride_IB_7fbbcf0d]",
    "hash = 7fbbcf0d",
    "; @@COND:abcd1234:begin@@",
    "    if $cond_abcd1234_cond_active == 1",
    "drawindexed = 1260",
    "",
    "    endif",
    "; @@COND:abcd1234:end@@",
    "[Present]",
    "post $some_other = 1",
    "; @@COND:abcd1234:present@@",
    "post $cond_abcd1234_cond_active = $cond_abcd1234_cond_frame",
    "post $cond_abcd1234_cond_frame = 0",
)
GOLDEN_SINGLE_HASH = "\n".join(_GOLDEN_LINES) + "\n"


def _make_model(object_name=OBJECT_NAME, draw_ib=DRAW_IB):
    draw_call = types.SimpleNamespace(
        obj_name=object_name,
        source_obj_name="",
        match_draw_ib=draw_ib,
        get_blender_obj_name=lambda: "",
    )
    return types.SimpleNamespace(
        ordered_draw_obj_data_model_list=[draw_call],
        _object_name_mapping={},
    )


def _hash(hash_value=DETECT_HASH, check_hash="", index_count=""):
    return {
        "condition_type": "HASH",
        "hash_value": hash_value,
        "check_hash": check_hash,
        "index_count": index_count,
    }


def _var(name, op="==", value="1"):
    return {
        "condition_type": "VARIABLE",
        "variable_name": name,
        "comparison_op": op,
        "compare_value": value,
    }


class _FakeConditionCollection(list):
    """够算子用的假 CollectionProperty。"""

    def add(self):
        item = types.SimpleNamespace(
            condition_type="HASH",
            hash_value="",
            check_hash="",
            index_count="",
            variable_name="",
            comparison_op="==",
            compare_value="1",
        )
        self.append(item)
        return item

    def remove(self, index):
        del self[index]

    def move(self, from_index, to_index):
        item = self.pop(from_index)
        self.insert(to_index, item)


def _make_node(conditions=None, legacy_hash="", legacy_check="", legacy_index=""):
    node = module.SSMTNode_PostProcess_Condition()
    node.name = "条件判断"
    node.condition_objects = [types.SimpleNamespace(object_name=OBJECT_NAME)]
    node.condition_list = list(conditions or [])
    node.active_condition = 0
    node.active_condition_object = 0
    node.condition_hash = legacy_hash
    node.check_hash = legacy_check
    node.condition_index_count = legacy_index
    return node


def _stripped_lines(text):
    return [line.strip() for line in str(text).splitlines()]


class _RegistryPatch:
    def __init__(self, registry):
        self.registry = registry
        self.original = None

    def __enter__(self):
        self.original = module._get_section_registry
        module._get_section_registry = lambda: self.registry
        return self

    def __exit__(self, *_exc):
        module._get_section_registry = self.original
        return False


# ---------------------------------------------------------------------------
# 金样 / 旧路径回归
# ---------------------------------------------------------------------------

class GoldenRegressionTests(unittest.TestCase):
    def test_legacy_params_match_frozen_golden(self):
        updated, stats = module._apply_condition(
            INI_WITH_PRESENT, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(GOLDEN_SINGLE_HASH, updated)
        self.assertEqual(1, stats["wrapped"])
        self.assertEqual("existing", stats["present"])
        self.assertEqual(1, stats["hash_count"])
        self.assertEqual(0, stats["variable_count"])
        self.assertTrue(stats["global_var"])
        self.assertTrue(stats["check"])

    def test_one_hash_condition_list_matches_golden(self):
        conditions = [_hash()]
        first, stats = module._apply_condition(
            INI_WITH_PRESENT, NS, {TARGET_SECTION}, DETECT_HASH, conditions=conditions
        )
        self.assertEqual(GOLDEN_SINGLE_HASH, first)
        self.assertEqual(1, stats["hash_count"])
        second, _stats = module._apply_condition(
            first, NS, {TARGET_SECTION}, DETECT_HASH, conditions=conditions
        )
        self.assertEqual(GOLDEN_SINGLE_HASH, second)

    def test_list_path_equals_legacy_path(self):
        legacy, _ = module._apply_condition(
            INI_WITH_PRESENT, NS, {TARGET_SECTION}, DETECT_HASH
        )
        listed, _ = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            conditions=[_hash()],
        )
        self.assertEqual(legacy, listed)


# ---------------------------------------------------------------------------
# 变量条件
# ---------------------------------------------------------------------------

class VariableConditionTests(unittest.TestCase):
    def test_single_variable_condition_emits_no_hash_plumbing(self):
        updated, stats = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            "",
            conditions=[_var("$MyVar")],
        )
        self.assertIn("    if $MyVar == 1\n", updated)
        self.assertNotIn("$cond_", updated)
        self.assertNotIn("TextureOverrideCheckHash", updated)
        self.assertEqual(1, updated.count("[Present]"))
        self.assertNotIn(f"; @@COND:{NS}:present@@", updated)
        self.assertEqual(0, stats["hash_count"])
        self.assertEqual(1, stats["variable_count"])
        self.assertFalse(stats["global_var"])
        self.assertFalse(stats["check"])
        self.assertEqual("skipped", stats["present"])

    def test_not_equal_operator(self):
        updated, _stats = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            "",
            conditions=[_var("MyVar", "!=", "0")],
        )
        self.assertIn("    if $MyVar != 0\n", updated)
        self.assertNotIn("$cond_", updated)
        self.assertNotIn("TextureOverrideCheckHash", updated)

    def test_variable_name_gets_dollar_prefix(self):
        updated, _stats = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            "",
            conditions=[_var("MyVar")],
        )
        self.assertIn("    if $MyVar == 1\n", updated)


# ---------------------------------------------------------------------------
# 多条件（与）
# ---------------------------------------------------------------------------

class MultiConditionTests(unittest.TestCase):
    def test_hash_and_variable_share_one_if_line(self):
        updated, stats = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            conditions=[_hash(), _var("$MyVar", "!=", "0")],
        )
        lines = _stripped_lines(updated)
        self.assertIn(
            f"if $cond_{NS}_cond_active == 1 && $MyVar != 0",
            lines,
        )
        self.assertEqual(1, stats["hash_count"])
        self.assertEqual(1, stats["variable_count"])
        # [Present] 只复位节点自己的 cond_ 变量，不碰用户变量。
        self.assertIn(
            f"post $cond_{NS}_cond_active = $cond_{NS}_cond_frame",
            lines,
        )
        self.assertFalse(any("post $MyVar" in line for line in lines))
        self.assertEqual(1, updated.count(f"; @@COND:{NS}:begin@@"))

    def test_two_hash_conditions_get_separate_slots(self):
        updated, stats = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            conditions=[_hash("92cb56c9"), _hash("11112222")],
        )
        lines = _stripped_lines(updated)
        self.assertEqual(2, stats["hash_count"])
        self.assertIn(f"global $cond_{NS}_cond_active", lines)
        self.assertIn(f"global $cond_{NS}_h1_active", lines)
        self.assertIn(f"global $cond_{NS}_h1_frame", lines)
        self.assertIn(f"[TextureOverrideCheckHash_cond_{NS}]", lines)
        self.assertIn(f"[TextureOverrideCheckHash_cond_{NS}_h1]", lines)
        self.assertIn("hash = 92cb56c9", lines)
        self.assertIn("hash = 11112222", lines)
        self.assertIn(
            f"if $cond_{NS}_cond_active == 1 && $cond_{NS}_h1_active == 1",
            lines,
        )
        self.assertIn(f"post $cond_{NS}_h1_active = $cond_{NS}_h1_frame", lines)
        self.assertIn(f"post $cond_{NS}_h1_frame = 0", lines)
        # 每个检测段头都必须夹在自己的 check-begin / check-end 之间（多槽位插入
        # 顺序回归：上一版的第 2 个块会插进第 1 个块的 begin 与段头之间）。
        begin_positions = [
            i for i, line in enumerate(lines)
            if line == f"; @@COND:{NS}:check-begin@@"
        ]
        end_positions = [
            i for i, line in enumerate(lines)
            if line == f"; @@COND:{NS}:check-end@@"
        ]
        self.assertEqual(2, len(begin_positions))
        self.assertEqual(2, len(end_positions))
        for begin, end in zip(begin_positions, end_positions):
            self.assertLess(begin, end)
            self.assertTrue(
                any(
                    lines[i].startswith("[TextureOverrideCheckHash_cond_")
                    for i in range(begin, end)
                )
            )

    def test_duplicate_hash_conditions_are_merged(self):
        updated, stats = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            conditions=[_hash("92cb56c9"), _hash("92cb56c9")],
        )
        lines = _stripped_lines(updated)
        self.assertEqual(2, stats["hash_count"])
        self.assertEqual(1, sum(1 for line in lines if line == "hash = 92cb56c9"))
        self.assertIn(f"if $cond_{NS}_cond_active == 1", lines)
        self.assertNotIn(f"$cond_{NS}_h1_active", " ".join(lines))


# ---------------------------------------------------------------------------
# 剥离（改条件后旧块必须干净）
# ---------------------------------------------------------------------------

class StrippingTests(unittest.TestCase):
    def test_compound_expression_is_stripped_when_conditions_shrink(self):
        compound, _ = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            conditions=[_hash(), _var("$X", "==", "1")],
        )
        self.assertIn("&& $X == 1", compound)
        single, _ = module._apply_condition(
            compound, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(GOLDEN_SINGLE_HASH, single)
        self.assertNotIn("&&", single)
        self.assertEqual(1, single.count("    endif"))

    def test_second_hash_slot_lines_are_stripped(self):
        two_hashes, _ = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            conditions=[_hash("92cb56c9"), _hash("11112222")],
        )
        self.assertIn(f"$cond_{NS}_h1_frame", two_hashes)
        single, _ = module._apply_condition(
            two_hashes, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(GOLDEN_SINGLE_HASH, single)
        self.assertNotIn("_h1_", single)
        self.assertNotIn("11112222", single)


# ---------------------------------------------------------------------------
# handling = skip 是「跳过部件」段里唯一可门控的行
# ---------------------------------------------------------------------------

class HandlingSkipTests(unittest.TestCase):
    def test_handling_skip_is_classified_gate(self):
        # 条件满足才跳过、否则恢复原生绘制：skip 必须跟着 if 走。
        self.assertEqual("gate", module._classify_line("handling = skip"))
        self.assertEqual("gate", module._classify_line("handling=skip"))
        self.assertEqual("gate", module._classify_line("handling = auto"))

    def test_handling_skip_is_gated(self):
        text = (
            "[Constants]\n"
            "global persist $x = 1\n"
            "\n"
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "handling = skip\n"
            "drawindexed = 1260\n"
        )
        updated, stats = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        lines = [line.strip() for line in updated.splitlines()]
        begin = lines.index(f"; @@COND:{NS}:begin@@")
        end = lines.index(f"; @@COND:{NS}:end@@")
        handling = lines.index("handling = skip")
        draw = lines.index("drawindexed = 1260")
        self.assertLess(begin, handling)
        self.assertLess(handling, draw)
        self.assertLess(draw, end)
        self.assertEqual(1, stats["wrapped"])
        self.assertEqual(0, stats["skipped"])
        self.assertEqual(1, _if_depth_at(lines, handling))

    def test_skip_node_section_keeps_if_balanced(self):
        # skip 节点格式的段：hash + handling = skip + drawindexed。
        section = "TextureOverride_IB_7fbbcf0d"
        text = (
            f"[{section}]\n"
            "hash = 7fbbcf0d\n"
            "handling = skip\n"
            "drawindexed = 1260\n"
        )
        updated, _stats = module._apply_condition(
            text, NS, {section}, DETECT_HASH
        )
        lines = [line.strip() for line in updated.splitlines()]
        self.assertIn("handling = skip", lines)
        # handling 已在门控 if 体内：深度 1，且整体 if/endif 配平。
        self.assertEqual(1, _if_depth_at(lines, lines.index("handling = skip")))
        depth = 0
        for line in lines:
            if line.startswith("if "):
                depth += 1
            elif line == "endif":
                depth -= 1
        self.assertEqual(0, depth)


# ---------------------------------------------------------------------------
# 自动追加尾块（07a / 07b：无 [Present] + 尾块）
# ---------------------------------------------------------------------------

def _if_depth_at(lines, position):
    depth = 0
    for line in lines[:position]:
        stripped = line.strip()
        if stripped.startswith("if "):
            depth += 1
        elif stripped == "endif":
            depth -= 1
    return depth


class AutoAppendedTailTests(unittest.TestCase):
    def _apply_twice(self, text):
        first, stats_first = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        second, stats_second = module._apply_condition(
            first, NS, {TARGET_SECTION}, DETECT_HASH
        )
        return first, second, stats_first, stats_second

    def _assert_tail_safe(self, updated):
        raw_lines = updated.splitlines(keepends=True)
        lines = [line.strip() for line in raw_lines]
        tail = module._auto_appended_tail_index(raw_lines)
        self.assertIsNotNone(tail)

        present = lines.index("[Present]")
        self.assertLess(present, tail, "新 [Present] 不能落进自动追加尾块")
        self.assertEqual(0, _if_depth_at(lines, present), "[Present] 不能落在 if 体内")

        # 目标段必须在尾块标记之前闭合。
        end_index = lines.index(f"; @@COND:{NS}:end@@")
        self.assertLess(end_index, tail)
        self.assertLess(lines.index(f"; @@COND:{NS}:begin@@"), end_index)
        self.assertIn("endif", lines[end_index - 1])

        # [Constants] global 与独立检测段都不能写进尾块。
        self.assertLess(lines.index(f"global $cond_{NS}_cond_active"), tail)
        self.assertLess(lines.index(f"[TextureOverrideCheckHash_cond_{NS}]"), tail)
        self.assertLess(lines.index(f"; @@COND:{NS}:check-begin@@"), tail)
        self.assertLess(lines.index(f"; @@COND:{NS}:check-end@@"), tail)

        # 尾块标记本身必须留在顶层。
        self.assertEqual(0, _if_depth_at(lines, tail))
        self.assertEqual(1, updated.count(f"; @@COND:{NS}:present@@"))

    def test_auto_appended_marker_is_classified_top(self):
        self.assertEqual(
            "top",
            module._classify_line("; --- AUTO-APPENDED CUSTOM TEXT 01 BEGIN ---"),
        )
        self.assertEqual(
            "top",
            module._classify_line("; --- AUTO-APPENDED CUSTOM TEXT 01 END ---"),
        )

    def test_missing_present_with_tail_section_is_idempotent(self):
        first, second, stats_first, stats_second = self._apply_twice(
            SAMPLE_07A_TAIL_SECTION
        )
        self.assertEqual("created", stats_first["present"])
        self.assertEqual("existing", stats_second["present"])
        self.assertEqual(first, second)
        self._assert_tail_safe(first)

    def test_missing_present_with_bare_tail_is_idempotent(self):
        first, second, stats_first, stats_second = self._apply_twice(
            SAMPLE_07B_BARE_TAIL
        )
        self.assertEqual("created", stats_first["present"])
        self.assertEqual("existing", stats_second["present"])
        self.assertEqual(first, second)
        self._assert_tail_safe(first)

    def test_constants_only_inside_tail_keeps_stable_order(self):
        first, stats_first = module._apply_condition(
            SAMPLE_C7A_CONSTANTS_IN_TAIL, NS, {TARGET_SECTION}, DETECT_HASH
        )
        second, stats_second = module._apply_condition(
            first, NS, {TARGET_SECTION}, DETECT_HASH
        )
        third, _stats_third = module._apply_condition(
            second, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual("created", stats_first["present"])
        self.assertEqual("existing", stats_second["present"])
        self.assertEqual(first, second)
        self.assertEqual(second, third)

        raw_lines = first.splitlines(keepends=True)
        lines = [line.strip() for line in raw_lines]
        tail = module._auto_appended_tail_index(raw_lines)
        self.assertIsNotNone(tail)

        global_no = lines.index(f"; @@COND:{NS}:global@@")
        check_no = lines.index(f"; @@COND:{NS}:check-begin@@")
        present_no = lines.index("[Present]")
        # 插入物顺序固定：global -> check -> present，且全部在尾块之前。
        self.assertLess(global_no, check_no)
        self.assertLess(check_no, present_no)
        self.assertLess(present_no, tail)
        self.assertEqual(0, _if_depth_at(lines, present_no))
        self.assertLess(lines.index(f"; @@COND:{NS}:end@@"), tail)
        # 尾块里原来的 [Constants] 不被改写。
        self.assertLess(tail, lines.index("global $panel_alpha = 1"))

        # global 必须落在某个 [Constants] 段体里（幂等脚本 audit 的不变量）。
        cond_globals = 0
        for name, _header, body_start, body_end in module._find_sections(raw_lines):
            if str(name).strip().casefold() != "constants":
                continue
            for line in lines[body_start:body_end]:
                if line.casefold().startswith("global") and f"cond_{NS}_" in line:
                    cond_globals += 1
        self.assertEqual(2, cond_globals)


# ---------------------------------------------------------------------------
# 旧配置兼容 / 迁移
# ---------------------------------------------------------------------------

class LegacyCompatibilityTests(unittest.TestCase):
    def test_effective_conditions_synthesizes_hash_from_legacy_fields(self):
        node = _make_node(conditions=[], legacy_hash=DETECT_HASH, legacy_check="", legacy_index="")
        conditions = module._effective_conditions(node)
        self.assertEqual(1, len(conditions))
        self.assertEqual("HASH", conditions[0]["type"])
        self.assertEqual(DETECT_HASH, conditions[0]["hash_value"])
        # 不写回属性。
        self.assertEqual(DETECT_HASH, node.condition_hash)
        self.assertEqual([], list(node.condition_list))

    def test_legacy_fields_export_like_golden(self):
        node = _make_node(conditions=[], legacy_hash=DETECT_HASH)
        conditions = module._effective_conditions(node)
        updated, _stats = module._apply_condition(
            INI_WITH_PRESENT,
            NS,
            {TARGET_SECTION},
            DETECT_HASH,
            conditions=conditions,
        )
        self.assertEqual(GOLDEN_SINGLE_HASH, updated)

    def test_migrate_operator_moves_legacy_into_one_hash_item(self):
        node = _make_node(
            conditions=[],
            legacy_hash="92cb56c9",
            legacy_check="33334444",
            legacy_index="1260",
        )
        node.condition_list = _FakeConditionCollection()
        tree = types.SimpleNamespace(nodes={"条件判断": node})
        context = types.SimpleNamespace(
            space_data=types.SimpleNamespace(edit_tree=tree),
            selected_objects=[],
        )
        operator = module.SSMT_OT_PostProcess_MigrateLegacyConditions()
        operator.node_name = "条件判断"
        self.assertEqual({'FINISHED'}, operator.execute(context))

        self.assertEqual(1, len(node.condition_list))
        item = node.condition_list[0]
        self.assertEqual("HASH", item.condition_type)
        self.assertEqual("92cb56c9", item.hash_value)
        self.assertEqual("33334444", item.check_hash)
        self.assertEqual("1260", item.index_count)
        self.assertEqual("", node.condition_hash)
        self.assertEqual("", node.check_hash)
        self.assertEqual("", node.condition_index_count)


# ---------------------------------------------------------------------------
# 非法条件：拒绝导出且不写盘
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        _log.records.clear()
        _ExportState.target_ini = ""
        _ExportState.model = None
        _ExportState.stages = []

    def _prepare(self, directory, conditions):
        path = os.path.join(directory, "mod.ini")
        with open(path, "wb") as handle:
            handle.write(INI_WITH_PRESENT.encode("utf-8"))
        _ExportState.target_ini = path
        _ExportState.model = _make_model()
        return path, _make_node(conditions=conditions)

    def _assert_rejected(self, conditions, expected_fragment):
        registry = _FakeRegistry([{"name": TARGET_SECTION, "draw_ib": DRAW_IB}])
        with tempfile.TemporaryDirectory() as tmp:
            path, node = self._prepare(tmp, conditions)
            before = Path(path).read_bytes()
            with _RegistryPatch(registry):
                self.assertFalse(node.execute_postprocess(tmp))
            self.assertEqual(before, Path(path).read_bytes())
            self.assertFalse(os.path.isdir(os.path.join(tmp, "Backups")))
        errors = _log.texts("error")
        self.assertTrue(
            any("第 1 条条件" in text and expected_fragment in text for text in errors),
            errors,
        )

    def test_invalid_variable_name_is_rejected(self):
        self._assert_rejected([_var("$bad-name")], "非法")

    def test_empty_compare_value_is_rejected(self):
        self._assert_rejected([_var("$MyVar", "==", "")], "比较值为空")

    def test_reserved_variable_prefix_is_rejected(self):
        self._assert_rejected([_var("cond_abcd1234_x")], "保留前缀")

    def test_newline_in_compare_value_is_rejected(self):
        self._assert_rejected([_var("$MyVar", "==", "1\n2")], "换行")

    def test_no_gateable_target_with_stale_markers_warns_and_rewrites(self):
        registry = _FakeRegistry([{"name": TARGET_SECTION, "draw_ib": DRAW_IB}])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mod.ini")
            with open(path, "wb") as handle:
                handle.write(INI_WITH_PRESENT.encode("utf-8"))
            _ExportState.target_ini = path
            _ExportState.model = _make_model()
            node = _make_node(conditions=[], legacy_hash=DETECT_HASH)
            ns = node._namespace()

            with _RegistryPatch(registry):
                self.assertTrue(node.execute_postprocess(tmp))
                first = Path(path).read_text(encoding="utf-8")
                self.assertIn(f"; @@COND:{ns}:begin@@", first)

                # 目标段换成注释段，但保留上一次导出的门控标记（模拟现场结构变化）。
                changed_input = first.replace(
                    "drawindexed = 1260", "; 空段：已无可门控内容"
                )
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(changed_input)
                before = Path(path).read_bytes()
                _log.records.clear()
                _ExportState.stages.clear()

                self.assertTrue(node.execute_postprocess(tmp))
                after = Path(path).read_bytes()

            updated = after.decode("utf-8")
            self.assertNotEqual(before, after, "旧门控被剥掉后文件必须改写")
            self.assertNotIn(f"; @@COND:{ns}:begin@@", updated)
            self.assertIn("; 空段", updated)
            warnings = _log.texts("warning")
            self.assertTrue(
                any(
                    "原有的门控已被剥掉" in text
                    and "登记段" in text
                    and path in text
                    for text in warnings
                ),
                warnings,
            )
            self.assertTrue(
                any("没有包住任何段" in text for text in _ExportState.stages),
                _ExportState.stages,
            )

    def test_execute_writes_namespace_on_first_run(self):
        registry = _FakeRegistry([{"name": TARGET_SECTION, "draw_ib": DRAW_IB}])
        with tempfile.TemporaryDirectory() as tmp:
            _path, node = self._prepare(tmp, [])
            node.condition_hash = DETECT_HASH
            self.assertEqual("", getattr(node, "condition_namespace", ""))
            with _RegistryPatch(registry):
                self.assertTrue(node.execute_postprocess(tmp))
            self.assertEqual(8, len(node.condition_namespace))
            self.assertEqual(node._namespace(), node.condition_namespace)
            fixed = node.condition_namespace
            node.name = "改名后"
            self.assertEqual(fixed, node._namespace())

    def test_unclosed_if_execute_warns(self):
        registry = _FakeRegistry([{"name": TARGET_SECTION, "draw_ib": DRAW_IB}])
        text = (
            "[Constants]\n"
            "global persist $x = 1\n"
            "\n"
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "drawindexed = 1260\n"
            "if $some_flag == 1\n"
            "drawindexed = 5\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mod.ini")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            _ExportState.target_ini = path
            _ExportState.model = _make_model()
            node = _make_node(conditions=[], legacy_hash=DETECT_HASH)
            with _RegistryPatch(registry):
                self.assertTrue(node.execute_postprocess(tmp))
            warnings = _log.texts("warning")
            self.assertTrue(
                any("if 未闭合" in text and "自动补" in text for text in warnings),
                warnings,
            )
            updated = Path(path).read_text(encoding="utf-8")
            self.assertIn(f"; @@COND:{node._namespace()}:autoclose@@", updated)

    def test_skipped_section_autoclose_execute_warns(self):
        registry = _FakeRegistry([{"name": TARGET_SECTION, "draw_ib": DRAW_IB}])
        text = (
            "[Constants]\n"
            "global persist $x = 1\n"
            "\n"
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "run = CommandListX\n"
            "if $some_flag == 1\n"
            "run = CommandListY\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mod.ini")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            _ExportState.target_ini = path
            _ExportState.model = _make_model()
            node = _make_node(conditions=[], legacy_hash=DETECT_HASH)
            with _RegistryPatch(registry):
                self.assertTrue(node.execute_postprocess(tmp))
            warnings = _log.texts("warning")
            self.assertTrue(
                any(
                    "if 未闭合" in message and TARGET_SECTION in message
                    for message in warnings
                ),
                warnings,
            )
            updated = Path(path).read_text(encoding="utf-8")
            self.assertIn(f"; @@COND:{node._namespace()}:autoclose@@", updated)
            self.assertNotIn(f"; @@COND:{node._namespace()}:begin@@", updated)

    def test_stray_endif_execute_warns(self):
        registry = _FakeRegistry([{"name": TARGET_SECTION, "draw_ib": DRAW_IB}])
        text = (
            "[Constants]\n"
            "global persist $x = 1\n"
            "\n"
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "drawindexed = 1260\n"
            "endif\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mod.ini")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            _ExportState.target_ini = path
            _ExportState.model = _make_model()
            node = _make_node(conditions=[], legacy_hash=DETECT_HASH)
            with _RegistryPatch(registry):
                self.assertTrue(node.execute_postprocess(tmp))
            warnings = _log.texts("warning")
            self.assertTrue(
                any("游离 endif" in text for text in warnings),
                warnings,
            )


# ---------------------------------------------------------------------------
# 命名空间（改名不换 ns / 其它 ns 残留）
# ---------------------------------------------------------------------------

class NamespaceTests(unittest.TestCase):
    def test_namespace_is_fixed_after_rename(self):
        node = _make_node()
        node.condition_namespace = "deadbeef"
        node.name = "改名后"
        self.assertEqual("deadbeef", node._namespace())

    def test_ensure_namespace_writes_once(self):
        node = _make_node()
        node.condition_namespace = ""
        ns = node._ensure_namespace()
        self.assertEqual(8, len(ns))
        self.assertEqual(ns, node.condition_namespace)
        node.name = "改名后"
        self.assertEqual(ns, node._namespace())
        self.assertEqual(ns, node._ensure_namespace())

    def test_foreign_namespace_counting(self):
        content = (
            f"; @@COND:{NS}:begin@@\n"
            "; @@COND:deadbeef:begin@@\n"
            "; @@COND:deadbeef:end@@\n"
            ";@@COND:deadbeef:present@@\n"
            "; @@COND:aaaa000a:global@@\n"
        )
        counts = module._foreign_condition_namespaces(content, NS)
        self.assertEqual({"deadbeef": 3, "aaaa000a": 1}, counts)

    def test_strip_foreign_namespaces_keeps_current(self):
        text = (
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            f"; @@COND:{NS}:begin@@\n"
            f"    if $cond_{NS}_cond_active == 1\n"
            "drawindexed = 1260\n"
            "    endif\n"
            f"; @@COND:{NS}:end@@\n"
            "; @@COND:deadbeef:begin@@\n"
            "    if $cond_deadbeef_cond_active == 1\n"
            "drawindexed = 1260\n"
            "    endif\n"
            "; @@COND:deadbeef:end@@\n"
            "; @@COND:deadbeef:global@@\n"
            "global $cond_deadbeef_cond_active\n"
            "; @@COND:deadbeef:present@@\n"
            "post $cond_deadbeef_cond_active = $cond_deadbeef_cond_frame\n"
        )
        updated, removed = module._strip_foreign_namespaces(text, ["deadbeef"])
        self.assertNotIn("deadbeef", updated)
        self.assertEqual(4, removed["deadbeef"])
        self.assertIn(f"; @@COND:{NS}:begin@@", updated)
        self.assertIn(f"; @@COND:{NS}:end@@", updated)
        self.assertIn("drawindexed = 1260", updated)


# ---------------------------------------------------------------------------
# 未闭合 if / 游离 endif
# ---------------------------------------------------------------------------

class BlockBalanceTests(unittest.TestCase):
    def test_unclosed_if_gets_autoclose_and_is_idempotent(self):
        text = (
            "[Constants]\n"
            "global persist $x = 1\n"
            "\n"
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "drawindexed = 1260\n"
            "if $some_flag == 1\n"
            "drawindexed = 5\n"
        )
        first, stats = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual([TARGET_SECTION], stats["auto_closed"])
        self.assertEqual(1, stats["auto_close_count"])
        lines = [line.strip() for line in first.splitlines()]
        self.assertIn(f"; @@COND:{NS}:autoclose@@", lines)
        depth = 0
        for line in lines:
            if line.startswith("if "):
                depth += 1
            elif line == "endif":
                depth -= 1
        self.assertEqual(0, depth)
        second, _stats = module._apply_condition(
            first, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(first, second)

    def test_stray_endif_is_reported_not_removed(self):
        text = (
            "[Constants]\n"
            "global persist $x = 1\n"
            "\n"
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "drawindexed = 1260\n"
            "endif\n"
        )
        updated, stats = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual([TARGET_SECTION], stats["stray_endif"])
        self.assertEqual(0, stats["auto_close_count"])
        lines = [line.strip() for line in updated.splitlines()]
        # 生成的 1 个 endif + 原游离 1 个。
        self.assertEqual(2, lines.count("endif"))
        second, _stats = module._apply_condition(
            updated, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(updated, second)

    def test_no_gate_section_with_unclosed_if_is_autoclosed(self):
        # 段内全是 top（run / if），wrapped=0，但缺 endif 也要补。
        text = (
            "[Constants]\n"
            "global persist $x = 1\n"
            "\n"
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "run = CommandListX\n"
            "if $some_flag == 1\n"
            "run = CommandListY\n"
        )
        first, stats = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(0, stats["wrapped"])
        self.assertEqual(1, stats["skipped"])
        self.assertEqual([TARGET_SECTION], stats["auto_closed"])
        self.assertEqual(1, stats["auto_close_count"])
        lines = [line.strip() for line in first.splitlines()]
        self.assertIn(f"; @@COND:{NS}:autoclose@@", lines)
        self.assertNotIn(f"; @@COND:{NS}:begin@@", lines)
        depth = 0
        for line in lines:
            if line.startswith("if "):
                depth += 1
            elif line == "endif":
                depth -= 1
        self.assertEqual(0, depth)
        second, _stats = module._apply_condition(
            first, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(first, second)

    def test_skip_only_section_gates_handling(self):
        text = (
            f"[{TARGET_SECTION}]\n"
            "hash = 7fbbcf0d\n"
            "handling = skip\n"
        )
        updated, stats = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertNotEqual(text, updated)
        self.assertEqual(1, stats["wrapped"])
        self.assertEqual(0, stats["skipped"])
        self.assertEqual([], stats["auto_closed"])
        lines = [line.strip() for line in updated.splitlines()]
        begin = lines.index(f"; @@COND:{NS}:begin@@")
        end = lines.index(f"; @@COND:{NS}:end@@")
        self.assertLess(lines.index("hash = 7fbbcf0d"), begin)
        self.assertLess(begin, lines.index("handling = skip"))
        self.assertLess(lines.index("handling = skip"), end)
        second, _stats = module._apply_condition(
            updated, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(updated, second)


# ---------------------------------------------------------------------------
# 换行：孤立 \r / 混排
# ---------------------------------------------------------------------------

class NewlineTests(unittest.TestCase):
    def test_lone_cr_file_does_not_get_crlf(self):
        text = (
            "[Constants]\r"
            "global persist $x = 1\r"
            "\r"
            f"[{TARGET_SECTION}]\r"
            "hash = 7fbbcf0d\r"
            "drawindexed = 1260\r"
        )
        updated, _stats = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertNotIn("\n", updated)
        self.assertNotIn("\r\r\n", updated)
        lines = [line.strip() for line in updated.splitlines()]
        depth = 0
        for line in lines:
            if line.startswith("if "):
                depth += 1
            elif line == "endif":
                depth -= 1
        self.assertEqual(0, depth)
        second, _stats = module._apply_condition(
            updated, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(updated, second)

    def test_crlf_plus_lone_cr_stays_balanced(self):
        text = (
            "[Constants]\r\n"
            "global persist $x = 1\r\n"
            "\r\n"
            f"[{TARGET_SECTION}]\r\n"
            "hash = 7fbbcf0d\r\n"
            "drawindexed = 1260\r"
        )
        updated, _stats = module._apply_condition(
            text, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertNotIn("\r\r\n", updated)
        lines = [line.strip() for line in updated.splitlines()]
        depth = 0
        for line in lines:
            if line.startswith("if "):
                depth += 1
            elif line == "endif":
                depth -= 1
        self.assertEqual(0, depth)
        second, _stats = module._apply_condition(
            updated, NS, {TARGET_SECTION}, DETECT_HASH
        )
        self.assertEqual(updated, second)


# ---------------------------------------------------------------------------
# 回归：跳过部件的 4 个 skip 段必须被条件判断门控（真实导出形状）
# ---------------------------------------------------------------------------

class SkipNodeSectionsGatedTests(unittest.TestCase):
    """跳过节点先追加 4 个 skip 段，条件判断随后圈这 4 个段（单哈希条件）。"""

    SKIP_HASH = "d58b6350"
    SECTIONS = (
        ("TextureOverride_IB_d58b6350_2574_10752", "d58b6350", "10752", "2574"),
        ("TextureOverride_IB_d58b6350_3096_7656", "d58b6350", "7656", "3096"),
        ("TextureOverride_IB_d58b6350_7656_0", "d58b6350", "0", "7656"),
        ("TextureOverride_IB_dbbf0aee_180_0", "dbbf0aee", "0", "180"),
    )

    def _text(self):
        chunks = []
        for name, draw_hash, first_index, index_count in self.SECTIONS:
            chunks.append(
                f"[{name}]\n"
                f"hash = {draw_hash}\n"
                f"match_first_index = {first_index}\n"
                f"match_index_count = {index_count}\n"
                "handling = skip\n"
            )
        return "\n".join(chunks)

    def test_four_skip_sections_are_wrapped(self):
        text = self._text()
        names = {name for name, _hash, _first, _count in self.SECTIONS}
        updated, stats = module._apply_condition(
            text, NS, names, self.SKIP_HASH
        )
        self.assertEqual(4, stats["wrapped"])
        self.assertEqual(0, stats["skipped"])
        self.assertEqual([], stats["auto_closed"])
        self.assertEqual([], stats["stray_endif"])

        lines = [line.strip() for line in updated.splitlines()]
        self.assertEqual(4, lines.count(f"; @@COND:{NS}:begin@@"))
        self.assertEqual(4, lines.count(f"; @@COND:{NS}:end@@"))
        self.assertIn(f"$cond_{NS}_cond_active", updated)

        cursor = 0
        for name, draw_hash, first_index, index_count in self.SECTIONS:
            header = lines.index(f"[{name}]", cursor)
            self.assertEqual(0, _if_depth_at(lines, header))
            match_keys = (
                f"hash = {draw_hash}",
                f"match_first_index = {first_index}",
                f"match_index_count = {index_count}",
            )
            for position in (lines.index(key, header) for key in match_keys):
                self.assertEqual(0, _if_depth_at(lines, position))
            handling = lines.index("handling = skip", header)
            self.assertLess(header, handling)
            self.assertEqual(1, _if_depth_at(lines, handling))
            cursor = handling

        self.assertIn("[Constants]", lines)
        self.assertIn("[Present]", lines)
        self.assertIn(f"post $cond_{NS}_cond_frame = 0", lines)
        depth = 0
        for line in lines:
            if line.startswith("if "):
                depth += 1
            elif line == "endif":
                depth -= 1
        self.assertEqual(0, depth)

        second, stats_second = module._apply_condition(
            updated, NS, names, self.SKIP_HASH
        )
        self.assertEqual(updated, second)
        self.assertEqual(4, stats_second["wrapped"])


if __name__ == "__main__":
    unittest.main()
