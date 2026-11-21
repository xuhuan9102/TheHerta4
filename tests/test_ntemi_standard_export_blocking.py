import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_ntemi_standard_export_blocking_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeOperatorBase:
    pass


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=_FakeOperatorBase),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(node_groups=types.SimpleNamespace(get=lambda _name: None)),
)
_install_module("bpy", **_fake_bpy.__dict__)

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
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    ),
)

_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        logic_name="NTEMI",
        read_from_main_json_ssmt4=lambda: None,
        path_generate_mod_folder=lambda: "X:/Mods/Out",
        # PR #18：导出入口用 set/get/restore 给「输出目录蓝图」指针限定作用域
        # （invoke/execute 的 finally 要能取快照与还原，缺一个就 AttributeError）
        set_output_blueprint_tree=lambda _tree: None,
        get_output_blueprint_tree_name=lambda: "",
        restore_output_blueprint_tree=lambda _tree_name: None,
    ),
)
_install_module(f"{PKG}.common.global_key_count_helper", GlobalKeyCountHelper=types.SimpleNamespace(initialize=lambda: None))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace(
))
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)
_install_module(
    f"{PKG}.common.config_table_backup",
    backup_config_tables=lambda *_args, **_kwargs: [],
    find_config_table_files=lambda *_args, **_kwargs: [],
    restore_config_tables=lambda *_args, **_kwargs: None,
)

_install_module(f"{PKG}.blueprint.model", BluePrintModel=types.SimpleNamespace(clear_object_name_mapping=lambda: None))
_install_module(
    f"{PKG}.blueprint.direct_export",
    execute_direct_export=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("direct export should not run")),
    has_direct_export_mode=lambda _tree: False,
    collect_direct_shapekey_nodes=lambda _tree: [],
    collect_direct_multifile_nodes=lambda _tree: [],
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        get_selected_blueprint_tree=lambda **_kwargs: None,
        get_current_blueprint_tree=lambda **_kwargs: None,
        validate_object_info_references=lambda _tree, _output_node=None: None,
        set_runtime_blueprint_tree=lambda *_args, **_kwargs: None,
        reset_direct_export_runtime_state=lambda *_args, **_kwargs: None,
        has_shapekey_postprocess_node=lambda *_args, **_kwargs: False,
        calculate_max_shapekey_slot_count=lambda *_args, **_kwargs: 0,
        calculate_max_export_count=lambda *_args, **_kwargs: 1,
        has_multi_file_export_nodes=lambda *_args, **_kwargs: False,
        collect_shapekey_objects=lambda *_args, **_kwargs: None,
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
    ExportRoundExecutor=types.SimpleNamespace(execute_round=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("standard export should not run"))),
    ParallelExportCoordinator=types.SimpleNamespace(),
    ParallelExportError=RuntimeError,
)
# 会话启动在用例中打桩；这里验证前台的目录作用域与配置表确认。
_install_module(
    f"{PKG}.blueprint.export_background",
    BACKGROUND_BUSY_MESSAGE="已有后台导出在运行，请等它结束或按 ESC 取消",
    BackgroundExportSession=types.SimpleNamespace(),
    NTMI_MODIMP_STAGE_STEPS=(),
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
    format_percent_bar=lambda *_args, **_kwargs: "",
    get_active_session=lambda: None,
    push_progress_header=lambda _text: None,
    safe_object_name=lambda obj: str(getattr(obj, "name", "") or ""),
)
_install_module(f"{PKG}.blueprint.sync", refresh_blueprint_sync_state=lambda **_kwargs: {"tree_count": 0, "updated_count": 0})


module_path = Path(__file__).resolve().parents[1] / "ui" / "ui_func_export.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ui_func_export", module_path)
ui_func_export = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ui_func_export
spec.loader.exec_module(ui_func_export)


class _FakeNTMIOutputNode:
    bl_idname = "SSMTNode_Result_Output_NTMIModImp"


class _FakeTree:
    def __init__(self, include_ntmi_output: bool):
        self.name = "NTEMI_Blueprint"
        self.nodes = [_FakeNTMIOutputNode()] if include_ntmi_output else []


class _BaseFakeOperator:
    def __init__(self):
        self.reports = []
        self.blueprint_name = ""

    def report(self, level, message):
        self.reports.append((set(level), message))


class NtemiStandardExportBlockingTests(unittest.TestCase):
    def test_generate_mod_starts_modimp_in_background(self):
        ui_func_export.GlobalConfig.logic_name = "NTEMI"
        tree = _FakeTree(include_ntmi_output=True)
        with mock.patch.object(ui_func_export.BlueprintExportHelper, "get_current_blueprint_tree", lambda **kwargs: tree):
            operator = ui_func_export.SSMTGenerateModBlueprint()
            started = []
            operator._start_background_export = lambda context, target: started.append(target) or {'RUNNING_MODAL'}
            self.assertEqual(operator.execute(None), {'RUNNING_MODAL'})
            self.assertEqual(started, [tree])

    def test_quick_export_blocks_ntemi_before_building_temp_tree(self):
        operator = ui_func_export.SSMTQuickExportSelected()
        operator.reports = []
        operator.report = types.MethodType(_BaseFakeOperator.report, operator)

        fake_mesh = types.SimpleNamespace(
            type="MESH",
            name="Mesh",
            as_pointer=lambda: 1,
        )

        result = operator.execute(types.SimpleNamespace(selected_objects=[fake_mesh]))

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(operator.reports)
        self.assertIn("NTMI ModImp", operator.reports[-1][1])


class _FakePostProcessNode:
    bl_idname = "SSMTNode_PostProcess_Material"
    mute = False


class _FakeTreeWithPostprocess:
    """带后处理节点的蓝图树（invoke() 弹窗门控要求 _has_postprocess_nodes 为真）。"""

    def __init__(self):
        self.name = "NTEMI_Blueprint"
        self.nodes = [_FakePostProcessNode()]


_ORIGINAL_BACKUP_CONFIG_TABLES = ui_func_export.backup_config_tables
_ORIGINAL_RESTORE_CONFIG_TABLES = ui_func_export.restore_config_tables
_ORIGINAL_GET_CURRENT_TREE = ui_func_export.BlueprintExportHelper.get_current_blueprint_tree
_ORIGINAL_FIND_CONFIG_TABLES = ui_func_export.find_config_table_files
_ORIGINAL_PATH_GENERATE_MOD_FOLDER = ui_func_export.GlobalConfig.path_generate_mod_folder


class ConfigTableBackupDecisionTests(unittest.TestCase):
    """配置表确认仍先备份；恢复发生在后台任务完成后。"""

    def setUp(self):
        self.config_dir = tempfile.mkdtemp(prefix="background_cfg_")
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        self.ini_path = os.path.join(self.config_dir, "角色.ini")
        Path(self.ini_path).write_text("[old]", encoding="utf-8")
        self.operator = ui_func_export.SSMTGenerateModBlueprint()
        self.operator.overwrite_config = 'YES'
        self.events = []
        self.entries = [(self.ini_path, self.ini_path + '.bak')]
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ui_func_export.GlobalConfig, "logic_name", "OTHER").start()
        mock.patch.object(ui_func_export.GlobalConfig, "path_generate_mod_folder", lambda: self.config_dir).start()
        mock.patch.object(ui_func_export.BlueprintExportHelper, "get_current_blueprint_tree", lambda **kwargs: _FakeTreeWithPostprocess()).start()
        mock.patch.object(ui_func_export, "find_config_table_files", return_value=[self.ini_path]).start()
        mock.patch.object(ui_func_export, "backup_config_tables", self._backup).start()
        mock.patch.object(ui_func_export, "restore_config_tables", lambda entries: self.events.append(('restore', entries))).start()
        self.operator._start_background_export = lambda context, tree: self.events.append(('start', tree.name)) or {'RUNNING_MODAL'}

    def _backup(self, path, files):
        self.assertEqual((path, files), (self.config_dir, [self.ini_path]))
        self.events.append(('backup', files))
        return self.entries

    def _confirm(self, overwrite):
        self.operator._pending_background_export = True
        self.operator._backup_pending = True
        self.operator._export_path = self.config_dir
        self.operator._config_table_paths = [self.ini_path]
        self.operator.overwrite_config = overwrite
        self.assertEqual(self.operator.execute(None), {'RUNNING_MODAL'})
        self.assertEqual([event[0] for event in self.events], ['backup', 'start'])
        self.operator._restore_config_tables_if_needed()

    def test_overwrite_yes_keeps_new_config_and_skips_restore(self):
        self._confirm('YES')
        self.assertEqual([event[0] for event in self.events], ['backup', 'start'])

    def test_overwrite_no_restores_only_after_background_completion(self):
        self._confirm('NO')
        self.assertEqual([event[0] for event in self.events], ['backup', 'start', 'restore'])
        self.assertEqual(self.events[-1][1], self.entries)
        self.operator._restore_config_tables_if_needed()
        self.assertEqual(len(self.events), 3)

    def test_invoke_dialog_opens_before_background_start(self):
        dialog = mock.Mock(return_value={'RUNNING_MODAL'})
        context = types.SimpleNamespace(window_manager=types.SimpleNamespace(invoke_props_dialog=dialog))
        self.assertEqual(self.operator.invoke(context, None), {'RUNNING_MODAL'})
        dialog.assert_called_once_with(self.operator, width=460)
        self.assertTrue(self.operator._pending_background_export)
        self.assertEqual(self.events, [])

    def test_invoke_without_config_tables_starts_background_directly(self):
        with mock.patch.object(ui_func_export, "find_config_table_files", return_value=[]):
            self.assertEqual(self.operator.invoke(None, None), {'RUNNING_MODAL'})
        self.assertEqual([event[0] for event in self.events], ['start'])


_REAL_CONFIG_NS = "_ntemi_real_config_table_backup_pkg"


def _load_real_config_table_backup():
    """加载真实 `common/config_table_backup.py`（独立 stub 包 + LOG 桩）。

    C2 配套：决策测试把 backup/restore 整体打桩，覆盖不到本轮新增的
    `BACKUP_SETS_TO_KEEP` / `prune_old_backups()` 保留策略，故单独加载真实模块断言。
    """
    for name in (_REAL_CONFIG_NS, f"{_REAL_CONFIG_NS}.common", f"{_REAL_CONFIG_NS}.utils"):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = []
            sys.modules[name] = package
    _install_module(
        f"{_REAL_CONFIG_NS}.utils.log_utils",
        LOG=types.SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        ),
    )
    module_name = f"{_REAL_CONFIG_NS}.common.config_table_backup"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = Path(__file__).resolve().parents[1] / "common" / "config_table_backup.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ConfigTableBackupPruneTests(unittest.TestCase):
    """真实模块的保留策略契约（BACKUP_SETS_TO_KEEP / prune_old_backups）。"""

    def setUp(self):
        self.tool = _load_real_config_table_backup()
        self._orig_prune = self.tool.prune_old_backups
        self._orig_keep = self.tool.BACKUP_SETS_TO_KEEP
        self.prune_calls = []

        def _spy(*args, **kwargs):
            self.prune_calls.append({"args": args, "kwargs": kwargs})
            return []

        self.tool.prune_old_backups = _spy

    def tearDown(self):
        self.tool.prune_old_backups = self._orig_prune
        self.tool.BACKUP_SETS_TO_KEEP = self._orig_keep

    def _make_backup_set(self, export_dir, name):
        target = os.path.join(export_dir, self.tool.BACKUP_DIR_NAME, name)
        os.makedirs(target, exist_ok=True)
        return target

    def test_backup_prunes_first_with_keep_minus_one_and_protects_current_set(self):
        """backup 必须先 prune：keep = BACKUP_SETS_TO_KEEP-1，且保护本批目录名。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = os.path.join(temp_dir, "角色.ini")
            Path(ini_path).write_text("[old]", encoding="utf-8")

            entries = self.tool.backup_config_tables(temp_dir, [ini_path])

            self.assertEqual(len(entries), 1)
            self.assertEqual(len(self.prune_calls), 1, "backup 必须先调用 prune_old_backups")
            args = self.prune_calls[0]["args"]
            self.assertEqual(args[0], temp_dir)
            self.assertEqual(args[1], max(0, self._orig_keep - 1))
            current_set_name = os.path.basename(os.path.dirname(entries[0][1]))
            self.assertEqual(args[2], current_set_name, "本批备份目录必须被保护")
            self.assertTrue(os.path.isfile(entries[0][1]), "备份文件必须真实存在")
            self.assertIn(
                self.tool.BACKUP_SUFFIX, os.path.basename(entries[0][1]),
                "备份文件必须以 .bak 结尾",
            )

    def test_backup_keeps_at_most_limit_backup_sets(self):
        """超过上限时真实 prune 清理最旧备份集：最终时间戳集合数 ≤ BACKUP_SETS_TO_KEEP。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            self.tool.prune_old_backups = self._orig_prune   # 本用例跑真实 prune
            self.tool.BACKUP_SETS_TO_KEEP = 3
            for name in (
                "20260101_000000_000001",
                "20260101_000001_000002",
                "20260101_000002_000003",
                "20260101_000003_000004",
            ):
                self._make_backup_set(temp_dir, name)

            ini_path = os.path.join(temp_dir, "角色.ini")
            Path(ini_path).write_text("[old]", encoding="utf-8")
            entries = self.tool.backup_config_tables(temp_dir, [ini_path])
            self.assertEqual(len(entries), 1)

            backup_root = os.path.join(temp_dir, self.tool.BACKUP_DIR_NAME)
            sets = sorted(
                name for name in os.listdir(backup_root)
                if self.tool.BACKUP_SET_PATTERN.match(name)
            )
            self.assertLessEqual(
                len(sets), 3,
                f"保留集数不得超过 BACKUP_SETS_TO_KEEP: {sets}",
            )
            current_set_name = os.path.basename(os.path.dirname(entries[0][1]))
            self.assertIn(current_set_name, sets, "本批备份集必须保留")
            self.assertEqual(sets[-1], current_set_name, "最旧的被清理，本批名（最新）留存")

    def test_prune_disabled_when_keep_not_positive(self):
        """keep<=0 时 prune 不动作（等价旧行为：不清理）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            self._make_backup_set(temp_dir, "20260101_000000_000001")
            self.tool.prune_old_backups = self._orig_prune
            self.assertEqual(self.tool.prune_old_backups(temp_dir, 0, ""), [])
            sets = os.listdir(os.path.join(temp_dir, self.tool.BACKUP_DIR_NAME))
            self.assertEqual(sets, ["20260101_000000_000001"])


if __name__ == "__main__":
    unittest.main()
