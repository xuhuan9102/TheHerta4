"""PR #18 回归：Mod 输出节点「每个蓝图独立的生成目录」。

覆盖三件事：

1. 蓝图设置了自己的目录 → ``GlobalConfig.path_generate_mod_folder()`` 返回该目录；
2. 蓝图勾选开关但路径为空（或开关没勾）→ 返回默认 ``SSMTGeneratedMod/<工作空间>``，
   **不回落**到场景全局设置；
3. 「输出目录蓝图」指针的作用域（问题 2 的回归）：导出结束——含异常路径与
   invoke 弹窗被取消的路径——必须把指针还原，否则「先蓝图导出、再非蓝图导出」
   （快速局部导出 / NTMI ModImp）会命中上一个蓝图，输出目录串到旧蓝图；
4. 「生成Mod到 [蓝图名] 文件夹中」（PR #20）：默认目录用蓝图名而不是工作空间名，
   蓝图名里的路径非法字符要替换掉，与「生成Mod到指定的文件夹中」互斥，
   老蓝图（没有该属性）按未勾选处理。

装载方式沿用 `tests/test_ntemi_standard_export_blocking.py` 的 fake-package 范式，
但这里加载的是**真实的** `common/global_config.py` 与 `ui/ui_func_export.py`：
要测的正是这两个模块之间的真实协作（指针的 set/restore 是否真的把目录串起来），
只有 bpy / GlobalProterties 等外部依赖打桩。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


PKG = "_blueprint_output_folder_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.ui", f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


# --- fake bpy ---------------------------------------------------------------
# 蓝图树注册表：GlobalConfig._get_output_blueprint_tree 走 bpy.data.node_groups.get(name)
_NODE_GROUPS = {}


class _FakeOperatorBase:
    pass


_install_module(
    "bpy",
    types=types.SimpleNamespace(Operator=_FakeOperatorBase),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(
        node_groups=types.SimpleNamespace(get=lambda name: _NODE_GROUPS.get(name)),
    ),
)


# --- 打桩的外部依赖 ---------------------------------------------------------
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_session=lambda *_args, **_kwargs: None,
        start_stage=lambda *_args, **_kwargs: None,
        end_stage=lambda *_args, **_kwargs: None,
        print_summary=lambda *_args, **_kwargs: None,
    ),
)
_install_module(f"{PKG}.utils.translate_utils", TR=types.SimpleNamespace(translate=lambda text: text))
_install_module(f"{PKG}.utils.command_utils", CommandUtils=types.SimpleNamespace(OpenGeneratedModFolder=lambda: None))
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        start_collecting=lambda *_args, **_kwargs: None,
        stop_collecting=lambda *_args, **_kwargs: None,
        save_to_text_editor=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    ),
)

# global_properties 是**可变**的打桩对象：各用例会在同一个对象上改返回值
_FAKE_GLOBAL_PROTERTIES = types.SimpleNamespace(
    workspace_source_mode=lambda: "",
    use_specific_generate_mod_folder_path=lambda: False,
    generate_mod_folder_path=lambda: "",
    # 后台导出开关：本文件只测同步链路，固定关闭
    is_background_export_enabled=lambda: False,
)
_install_module(f"{PKG}.common.global_properties", GlobalProterties=_FAKE_GLOBAL_PROTERTIES)
_install_module(f"{PKG}.common.global_key_count_helper", GlobalKeyCountHelper=types.SimpleNamespace(initialize=lambda: None))
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace(NTEMI="NTEMI"))
_install_module(
    f"{PKG}.common.config_table_backup",
    backup_config_tables=lambda *_args, **_kwargs: [],
    find_config_table_files=lambda *_args, **_kwargs: [],
    restore_config_tables=lambda *_args, **_kwargs: None,
)
_install_module(f"{PKG}.blueprint.model", BluePrintModel=types.SimpleNamespace(clear_object_name_mapping=lambda: None))
_install_module(
    f"{PKG}.blueprint.direct_export",
    execute_direct_export=lambda **_kwargs: None,
    has_direct_export_mode=lambda _tree: False,
    collect_direct_shapekey_nodes=lambda _tree: [],
    collect_direct_multifile_nodes=lambda _tree: [],
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        BLUEPRINT_NONE_IDENTIFIER="<none>",
        get_selected_blueprint_tree=lambda **_kwargs: None,
        get_current_blueprint_tree=lambda **_kwargs: None,
        set_runtime_blueprint_tree=lambda *_args, **_kwargs: None,
        reset_direct_export_runtime_state=lambda *_args, **_kwargs: None,
        has_shapekey_postprocess_node=lambda *_args, **_kwargs: False,
        calculate_max_shapekey_slot_count=lambda *_args, **_kwargs: 0,
        calculate_max_export_count=lambda *_args, **_kwargs: 1,
        has_multi_file_export_nodes=lambda *_args, **_kwargs: False,
        multi_file_export_nodes=[],
        runtime_blueprint_tree_name="",
        current_export_index=1,
        get_current_buffer_folder_name=lambda: "",
        set_current_export_index=lambda *_args, **_kwargs: None,
        set_current_buffer_folder_name=lambda *_args, **_kwargs: None,
    ),
)
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace(cleanup_copies=lambda **_kwargs: None))
_install_module(
    f"{PKG}.blueprint.export_parallel",
    ExportRoundExecutor=types.SimpleNamespace(),
    ParallelExportCoordinator=types.SimpleNamespace(),
    ParallelExportError=RuntimeError,
)
# 后台导出只用于 invoke 分流；本文件测同步链路，给空壳即可
_install_module(
    f"{PKG}.blueprint.export_background",
    BACKGROUND_BUSY_MESSAGE="已有后台导出在运行，请等它结束或按 ESC 取消",
    BackgroundExportSession=types.SimpleNamespace(),
    QUICK_EXPORT_ROUND_PLAN={
        "round_index": 1,
        "phase": "single",
        "description": "快速局部导出",
        "buffer_folder_name": "Meshes",
        "export_index": 1,
        "generate_ini": True,
        "generate_classification_report": False,
        "shapekey_mode": "unchanged",
        "shapekey_slot_index": None,
    },
    build_quick_export_temp_tree=lambda _objects: None,
    clear_active_session=lambda _session=None: None,
    clear_progress_header=lambda: None,
    format_progress_bar=lambda *_args, **_kwargs: "",
    get_active_session=lambda: None,
    push_progress_header=lambda _text: None,
    safe_object_name=lambda obj: str(getattr(obj, "name", "") or ""),
)
_install_module(f"{PKG}.blueprint.sync", refresh_blueprint_sync_state=lambda **_kwargs: {"tree_count": 0, "updated_count": 0})


def _load_real_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# 真实模块：common/global_config.py（它会 from .global_properties import GlobalProterties）
_global_config_module = _load_real_module(f"{PKG}.common.global_config", Path("common") / "global_config.py")
GlobalConfig = _global_config_module.GlobalConfig

# 真实模块：ui/ui_func_export.py（它 from ..common.global_config import GlobalConfig）
ui_func_export = _load_real_module(f"{PKG}.ui.ui_func_export", Path("ui") / "ui_func_export.py")

_ORIGINAL_READ_MAIN_JSON = GlobalConfig.read_from_main_json_ssmt4
_ORIGINAL_EXPORT_ROUND_EXECUTOR = ui_func_export.ExportRoundExecutor
_ORIGINAL_FIND_CONFIG_TABLES = ui_func_export.find_config_table_files
_ORIGINAL_GET_CURRENT_TREE = ui_func_export.BlueprintExportHelper.get_current_blueprint_tree


class _BlueprintTree:
    """最小蓝图树桩：GlobalConfig 只按 name / bl_idname / 三个输出目录属性读它。"""

    bl_idname = "SSMTBlueprintTreeType"

    def __init__(self, name, use_specific=False, folder_path="", nodes=None,
                 blueprint_name_folder=False):
        self.name = name
        self.use_specific_generate_mod_folder_path = use_specific
        self.generate_mod_folder_path = folder_path
        self.use_blueprint_name_generate_mod_folder = blueprint_name_folder
        self.nodes = list(nodes or [])


class _PostProcessNode:
    bl_idname = "SSMTNode_PostProcess_Material"
    mute = False


class _FakeBlueprintModel:
    def __init__(self, recorder):
        self._recorder = recorder

    def execute_postprocess_nodes(self, mod_export_path):
        self._recorder.append(("postprocess", mod_export_path))


class _BaseFakeOperator:
    def __init__(self):
        self.reports = []

    def report(self, level, message):
        self.reports.append((set(level), message))


class BlueprintOutputFolderTests(unittest.TestCase):
    def setUp(self):
        # 全局状态按用例重置（GlobalConfig 是类级静态变量）
        GlobalConfig.clear_output_blueprint_tree()
        GlobalConfig.logic_name = ""
        GlobalConfig.current_game_migoto_folder = tempfile.mkdtemp(prefix="bp18_migoto_")
        GlobalConfig.workspacename = "测试角色"
        GlobalConfig.gamename = "TESTGAME"
        GlobalConfig.ssmtlocation = tempfile.mkdtemp(prefix="bp18_ssmt_")
        self.addCleanup(self._cleanup_dirs, GlobalConfig.current_game_migoto_folder, GlobalConfig.ssmtlocation)

        # 不读用户真实的 SSMT settings.json（会改写 workspacename/logic_name，令用例不确定）
        GlobalConfig.read_from_main_json_ssmt4 = classmethod(lambda cls: None)

        self.global_folder = tempfile.mkdtemp(prefix="bp18_global_")
        self.blueprint_folder = tempfile.mkdtemp(prefix="bp18_blueprint_")
        self.addCleanup(self._cleanup_dirs, self.global_folder, self.blueprint_folder)

        # 场景全局设置：勾选 + 指向 global_folder（非蓝图流程应沿用它）
        _FAKE_GLOBAL_PROTERTIES.workspace_source_mode = lambda: ""
        _FAKE_GLOBAL_PROTERTIES.use_specific_generate_mod_folder_path = lambda: True
        _FAKE_GLOBAL_PROTERTIES.generate_mod_folder_path = lambda: self.global_folder

        _NODE_GROUPS.clear()

    def tearDown(self):
        GlobalConfig.read_from_main_json_ssmt4 = _ORIGINAL_READ_MAIN_JSON
        GlobalConfig.clear_output_blueprint_tree()
        ui_func_export.ExportRoundExecutor = _ORIGINAL_EXPORT_ROUND_EXECUTOR
        ui_func_export.find_config_table_files = _ORIGINAL_FIND_CONFIG_TABLES
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = _ORIGINAL_GET_CURRENT_TREE
        _NODE_GROUPS.clear()

    @staticmethod
    def _cleanup_dirs(*dirs):
        import shutil

        for directory in dirs:
            shutil.rmtree(directory, ignore_errors=True)

    def _default_folder(self, folder_name=""):
        name = folder_name or GlobalConfig.get_workspace_name()
        return os.path.join(GlobalConfig.path_mods_folder(), "SSMTGeneratedMod\\", name + "\\")

    def _register(self, tree):
        _NODE_GROUPS[tree.name] = tree
        return tree

    def _context(self, window_manager=None):
        return types.SimpleNamespace(
            scene=types.SimpleNamespace(
                global_properties=types.SimpleNamespace(selected_blueprint_name="")
            ),
            window_manager=window_manager,
        )

    # --- 用例 1/2：蓝图自己的目录与默认目录 --------------------------------

    def test_blueprint_with_its_own_folder_returns_that_folder(self):
        tree = self._register(_BlueprintTree("蓝图A", use_specific=True, folder_path=self.blueprint_folder))

        GlobalConfig.set_output_blueprint_tree(tree)

        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.blueprint_folder)
        self.assertNotEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder)

    def test_blueprint_switch_on_with_empty_path_falls_back_to_default(self):
        for empty_path in ("", "   "):
            with self.subTest(path=repr(empty_path)):
                tree = self._register(_BlueprintTree("蓝图空路径", use_specific=True, folder_path=empty_path))
                GlobalConfig.set_output_blueprint_tree(tree)

                result = GlobalConfig.path_generate_mod_folder()

                self.assertEqual(result, self._default_folder())
                self.assertNotEqual(result, self.global_folder)
                self.assertTrue(os.path.isdir(result), "默认目录应被创建")

    def test_blueprint_switch_off_ignores_its_path(self):
        tree = self._register(_BlueprintTree("蓝图未勾选", use_specific=False, folder_path=self.blueprint_folder))

        GlobalConfig.set_output_blueprint_tree(tree)

        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self._default_folder())

    def test_non_blueprint_flow_uses_scene_global_setting(self):
        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder)

    # --- 用例 3：指针作用域（问题 2 的回归） -------------------------------

    def test_pointer_restore_clears_blueprint_branch(self):
        """导出入口的 finally 把指针还原后，非蓝图导出不再命中蓝图分支。"""
        tree = self._register(_BlueprintTree("蓝图A", use_specific=True, folder_path=self.blueprint_folder))

        previous = GlobalConfig.get_output_blueprint_tree_name()
        GlobalConfig.set_output_blueprint_tree(tree)
        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.blueprint_folder)

        # 模拟导出入口的 finally
        GlobalConfig.restore_output_blueprint_tree(previous)

        self.assertEqual(GlobalConfig.get_output_blueprint_tree_name(), "")
        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder)

    def test_blueprint_export_then_quick_export_does_not_reuse_old_blueprint(self):
        """端到端：真实 SSMTGenerateModBlueprint.execute 之后指针必须还原。

        走真实的 execute（只有 ExportRoundExecutor 打桩），在导出流程内部记录
        `path_generate_mod_folder()` 的取值，确认：
        - 导出进行中 = 蓝图自己的目录；
        - execute 返回后（含异常路径）= 非蓝图流程的路径，不再串到旧蓝图。
        """
        tree = self._register(_BlueprintTree("蓝图A", use_specific=True, folder_path=self.blueprint_folder))
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = lambda **_kwargs: tree

        seen_paths = []

        def _fake_execute_round(**kwargs):
            seen_paths.append(GlobalConfig.path_generate_mod_folder())
            return {"blueprint_model": _FakeBlueprintModel(seen_paths), "buffer_size": 0}

        ui_func_export.ExportRoundExecutor = types.SimpleNamespace(execute_round=_fake_execute_round)

        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator.reports = []
        operator.report = types.MethodType(_BaseFakeOperator.report, operator)
        operator.overwrite_config = "YES"

        result = operator.execute(self._context())

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(
            seen_paths, [self.blueprint_folder, ("postprocess", self.blueprint_folder)],
            "导出流程内部必须记到蓝图自己的目录",
        )
        self.assertEqual(GlobalConfig.get_output_blueprint_tree_name(), "", "导出结束后指针必须还原为空")
        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder, "导出结束后不得再命中旧蓝图")

        # 「快速局部导出」用的就是同一个 path_generate_mod_folder()（export_parallel 里算 buffer 目录）
        self.assertNotEqual(
            os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes"),
            os.path.join(self.blueprint_folder, "Meshes"),
        )

    def test_blueprint_export_exception_still_restores_pointer(self):
        """导出过程中抛异常也必须还原指针（异常路径不得泄漏）。"""
        tree = self._register(_BlueprintTree("蓝图A", use_specific=True, folder_path=self.blueprint_folder))
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = lambda **_kwargs: tree

        def _explode(**_kwargs):
            raise RuntimeError("boom")

        ui_func_export.ExportRoundExecutor = types.SimpleNamespace(execute_round=_explode)

        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator.reports = []
        operator.report = types.MethodType(_BaseFakeOperator.report, operator)
        operator.overwrite_config = "YES"

        result = operator.execute(self._context())

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(GlobalConfig.get_output_blueprint_tree_name(), "")
        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder)

    def test_invoke_dialog_cancel_does_not_leak_blueprint_pointer(self):
        """invoke 弹窗（用户可能取消，之后不会进 execute）也必须还原指针。"""
        tree = self._register(
            _BlueprintTree(
                "蓝图A",
                use_specific=True,
                folder_path=self.blueprint_folder,
                nodes=[_PostProcessNode()],
            )
        )
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = lambda **_kwargs: tree
        ini_path = os.path.join(self.blueprint_folder, "角色.ini")
        Path(ini_path).write_text("[old]", encoding="utf-8")
        ui_func_export.find_config_table_files = lambda _path: [ini_path]

        dialog_calls = []

        def _invoke_props_dialog(operator, **kwargs):
            dialog_calls.append(kwargs)
            return {"RUNNING_MODAL"}

        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator.reports = []
        operator.report = types.MethodType(_BaseFakeOperator.report, operator)

        result = operator.invoke(
            self._context(window_manager=types.SimpleNamespace(invoke_props_dialog=_invoke_props_dialog)),
            types.SimpleNamespace(),
        )

        self.assertEqual(result, {"RUNNING_MODAL"})
        self.assertEqual(len(dialog_calls), 1, "应弹窗（弹窗里展示的目录就是蓝图的目录）")
        self.assertEqual(operator._export_path, self.blueprint_folder)
        self.assertEqual(GlobalConfig.get_output_blueprint_tree_name(), "", "invoke 返回后指针必须还原")
        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder)

    # --- 用例 4：PR #20「生成Mod到 [蓝图名] 文件夹中」 -----------------------

    def test_blueprint_name_option_uses_blueprint_name_folder(self):
        tree = self._register(_BlueprintTree("蓝图B", blueprint_name_folder=True))

        GlobalConfig.set_output_blueprint_tree(tree)

        result = GlobalConfig.path_generate_mod_folder()
        self.assertEqual(result, self._default_folder("蓝图B"))
        self.assertNotEqual(result, self._default_folder(), "不应再用工作空间名")
        self.assertNotEqual(result, self.global_folder)
        self.assertTrue(os.path.isdir(result), "蓝图名目录应被创建")

    def test_blueprint_name_option_replaces_illegal_path_characters(self):
        illegal = '角/色:蓝*图?<A>|B"'
        tree = self._register(_BlueprintTree(illegal, blueprint_name_folder=True))

        GlobalConfig.set_output_blueprint_tree(tree)

        # 连续的非法字符折叠成一个下划线（re.sub 的 `+`），且各段仍可辨认
        self.assertEqual(
            GlobalConfig.path_generate_mod_folder(),
            self._default_folder("角_色_蓝_图_A_B_"),
        )
        self.assertNotIn("/", os.path.basename(os.path.dirname(GlobalConfig.path_generate_mod_folder())))

    def test_blueprint_name_option_only_applies_inside_blueprint_flow(self):
        """非蓝图导出流程不受蓝图名开关影响（沿用场景全局设置）。"""
        self._register(_BlueprintTree("蓝图B", blueprint_name_folder=True))

        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder)
        self.assertFalse(os.path.isdir(self._default_folder("蓝图B")))

    def test_blueprint_name_no_longer_applies_after_pointer_restore(self):
        tree = self._register(_BlueprintTree("蓝图B", blueprint_name_folder=True))

        previous = GlobalConfig.get_output_blueprint_tree_name()
        GlobalConfig.set_output_blueprint_tree(tree)
        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self._default_folder("蓝图B"))

        GlobalConfig.restore_output_blueprint_tree(previous)

        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.global_folder)

    def test_specific_folder_takes_priority_over_blueprint_name(self):
        """两个开关同时为真（旧数据 / 脚本直接写属性）时，「指定文件夹」优先。"""
        tree = self._register(
            _BlueprintTree(
                "蓝图C",
                use_specific=True,
                folder_path=self.blueprint_folder,
                blueprint_name_folder=True,
            )
        )

        GlobalConfig.set_output_blueprint_tree(tree)

        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self.blueprint_folder)

    def test_specific_switch_with_empty_path_falls_through_to_blueprint_name(self):
        """指定文件夹勾了但路径为空 -> 落到蓝图名目录，而不是工作空间目录。"""
        tree = self._register(
            _BlueprintTree("蓝图D", use_specific=True, folder_path="   ", blueprint_name_folder=True)
        )

        GlobalConfig.set_output_blueprint_tree(tree)

        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self._default_folder("蓝图D"))

    def test_blueprint_name_option_absent_on_legacy_tree(self):
        """老蓝图（没有该属性）按未勾选处理 —— getattr 兜底，不得报错。"""

        class _LegacyTree:
            bl_idname = "SSMTBlueprintTreeType"
            name = "旧蓝图"
            use_specific_generate_mod_folder_path = False
            generate_mod_folder_path = ""
            nodes = ()

        tree = self._register(_LegacyTree())

        GlobalConfig.set_output_blueprint_tree(tree)

        self.assertEqual(GlobalConfig.path_generate_mod_folder(), self._default_folder())


def _load_node_base_callbacks():
    """从 blueprint/node_base.py 里取出两个互斥 update 回调。

    只编译那两个模块级函数，不导入整个 node_base（它需要完整的 bpy）。
    """
    import ast

    source = (Path(__file__).resolve().parents[1] / "blueprint" / "node_base.py").read_text(
        encoding="utf-8"
    )
    wanted = {"_on_use_specific_folder_changed", "_on_use_blueprint_name_changed"}
    picked = [
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    missing = wanted - {node.name for node in picked}
    if missing:
        raise AssertionError(f"blueprint/node_base.py 里缺少互斥回调：{sorted(missing)}")
    namespace = {}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "node_base.py", "exec"), namespace)
    return {name: namespace[name] for name in wanted}


class _TreeWithCallbacks:
    """复刻 Blender 的「赋值即触发 update 回调」语义，用于验证互斥不会来回弹跳。"""

    def __init__(self, callbacks, specific=False, name_folder=False):
        object.__setattr__(self, "_callbacks", dict(callbacks))
        object.__setattr__(self, "assignments", [])
        object.__setattr__(self, "_values", {
            "use_specific_generate_mod_folder_path": specific,
            "use_blueprint_name_generate_mod_folder": name_folder,
        })

    def __getattr__(self, item):
        values = object.__getattribute__(self, "_values")
        if item in values:
            return values[item]
        raise AttributeError(item)

    def __setattr__(self, key, value):
        values = object.__getattribute__(self, "_values")
        if key not in values:
            object.__setattr__(self, key, value)
            return
        if values[key] == value:
            return
        values[key] = value
        self.assignments.append((key, value))
        callback = object.__getattribute__(self, "_callbacks").get(key)
        if callback is not None:
            callback(self, None)


class BlueprintFolderOptionExclusionTests(unittest.TestCase):
    """PR #20：「生成Mod到指定的文件夹中」与「生成Mod到 [蓝图名] 文件夹中」互斥。"""

    @classmethod
    def setUpClass(cls):
        cls.callbacks = _load_node_base_callbacks()

    def _tree(self, **kwargs):
        return _TreeWithCallbacks(
            {
                "use_specific_generate_mod_folder_path": self.callbacks["_on_use_specific_folder_changed"],
                "use_blueprint_name_generate_mod_folder": self.callbacks["_on_use_blueprint_name_changed"],
            },
            **kwargs,
        )

    def test_checking_specific_folder_clears_blueprint_name(self):
        tree = self._tree(specific=False, name_folder=True)

        tree.use_specific_generate_mod_folder_path = True

        self.assertTrue(tree.use_specific_generate_mod_folder_path)
        self.assertFalse(tree.use_blueprint_name_generate_mod_folder)
        self.assertEqual(
            tree.assignments,
            [
                ("use_specific_generate_mod_folder_path", True),
                ("use_blueprint_name_generate_mod_folder", False),
            ],
            "只应发生两次赋值（勾 A、关 B），回调不得来回弹跳",
        )

    def test_checking_blueprint_name_clears_specific_folder(self):
        tree = self._tree(specific=True, name_folder=False)

        tree.use_blueprint_name_generate_mod_folder = True

        self.assertTrue(tree.use_blueprint_name_generate_mod_folder)
        self.assertFalse(tree.use_specific_generate_mod_folder_path)
        self.assertEqual(
            [key for key, _value in tree.assignments],
            [
                "use_blueprint_name_generate_mod_folder",
                "use_specific_generate_mod_folder_path",
            ],
        )

    def test_checking_when_other_is_already_off_is_a_no_op(self):
        tree = self._tree(specific=False, name_folder=False)

        tree.use_specific_generate_mod_folder_path = True

        self.assertEqual(tree.assignments, [("use_specific_generate_mod_folder_path", True)])

    def test_node_base_wires_the_callbacks_to_the_properties(self):
        """源码护栏：属性上必须挂着 update=，否则互斥会静默失效。"""
        source = (Path(__file__).resolve().parents[1] / "blueprint" / "node_base.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("update=_on_use_specific_folder_changed", source)
        self.assertIn("update=_on_use_blueprint_name_changed", source)
        self.assertIn("use_blueprint_name_generate_mod_folder: bpy.props.BoolProperty(", source)


if __name__ == "__main__":
    unittest.main()
