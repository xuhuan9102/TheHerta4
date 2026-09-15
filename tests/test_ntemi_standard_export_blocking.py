import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
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
        save_to_text_editor=lambda *_args, **_kwargs: None,
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
    ),
)
_install_module(f"{PKG}.common.global_key_count_helper", GlobalKeyCountHelper=types.SimpleNamespace(initialize=lambda: None))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.logic_name",
    # 只放本文件用得到的成员；补充 EFMI/ZZMI（吸收自 TheHerta4Test_20260915）：
    # 导出入口新增了「合并骨架导出前自动迁移旧骨骼编号」的分支，会读这两个名字。
    LogicName=types.SimpleNamespace(NTEMI="NTEMI", EFMI="EFMI", ZZMI="ZZMI"),
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
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        get_selected_blueprint_tree=lambda **_kwargs: None,
        get_current_blueprint_tree=lambda **_kwargs: None,
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
    def test_generate_mod_blocks_ntemi_and_points_to_modimp_output(self):
        tree = _FakeTree(include_ntmi_output=True)
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = lambda **_kwargs: tree
        start_collecting_calls = []
        ui_func_export.LOG.start_collecting = lambda *_args, **_kwargs: start_collecting_calls.append(True)

        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator.reports = []
        operator.report = types.MethodType(_BaseFakeOperator.report, operator)

        result = operator.execute(types.SimpleNamespace(scene=types.SimpleNamespace(global_properties=types.SimpleNamespace(selected_blueprint_name=""))))

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(operator.reports)
        self.assertIn("NTMI ModImp Output", operator.reports[-1][1])
        self.assertEqual(start_collecting_calls, [])

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
_ORIGINAL_EXECUTE_ROUND = ui_func_export.ExportRoundExecutor.execute_round


class ConfigTableBackupDecisionTests(unittest.TestCase):
    """弹窗「是/否」选择驱动的备份/恢复决策（回归：选择「是」不得恢复旧配置表）。

    走主导出流程（直出桩关闭，ExportRoundExecutor 桩必然失败）：
    - 弹窗确认（_backup_pending=True）后应先备份；
    - 仅当 overwrite_config == 'NO' 时才在导出完成后恢复旧配置表。

    C2 补强（原用例只数调用次数 → 备份/恢复出错也测不出来）：
    - 断言 backup 收到的**实参形状** `(mod_export_path, config_table_paths)`；
    - 断言 restore 收到的 entries **就是** backup 的返回值；
    - 用事件序列断言**先后顺序** `backup → 导出尝试 → restore`；
    - 覆盖 `ui_func_export.invoke()` 弹窗链（有配置表才弹窗并挂状态 / 无配置表直接 execute）。
    """

    def setUp(self):
        self.operator = ui_func_export.SSMTGenerateModBlueprint()
        self.operator.reports = []
        self.operator.report = types.MethodType(_BaseFakeOperator.report, self.operator)
        # 模拟 RNA 默认值（真实 Blender 中 EnumProperty 默认 'YES'）
        self.operator.overwrite_config = 'YES'
        # 绕过 NTEMI 阻断，进入实际导出流程
        ui_func_export.GlobalConfig.logic_name = "OTHER"

        # 真实存在的导出目录 + 一份"旧配置表"（invoke() 用 os.path.exists 门控）
        self.config_dir = tempfile.mkdtemp(prefix="ntemi_cfg_")
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        self.ini_path = os.path.join(self.config_dir, "角色.ini")
        Path(self.ini_path).write_text("[old]", encoding="utf-8")

        ui_func_export.GlobalConfig.path_generate_mod_folder = lambda: self.config_dir
        ui_func_export.find_config_table_files = lambda _path: [self.ini_path]
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = (
            lambda **_kwargs: _FakeTree(include_ntmi_output=False)
        )

        self.events = []
        self.backup_calls = []
        self.restore_calls = []
        self.backup_entries = [(
            self.ini_path,
            os.path.join(
                self.config_dir, ".config_table_backup", "20260101_000000_000000",
                "角色.ini.bak",
            ),
        )]

        def _fake_backup(*args, **kwargs):
            self.events.append("backup")
            self.backup_calls.append({"args": args, "kwargs": kwargs})
            return list(self.backup_entries)

        def _fake_restore(*args, **kwargs):
            self.events.append("restore")
            self.restore_calls.append({"args": args, "kwargs": kwargs})

        def _fake_execute_round(**kwargs):
            self.events.append("export_attempt")
            return _ORIGINAL_EXECUTE_ROUND(**kwargs)   # 原桩必然抛 AssertionError

        ui_func_export.backup_config_tables = _fake_backup
        ui_func_export.restore_config_tables = _fake_restore
        ui_func_export.ExportRoundExecutor.execute_round = _fake_execute_round

    def tearDown(self):
        ui_func_export.GlobalConfig.logic_name = "NTEMI"
        ui_func_export.backup_config_tables = _ORIGINAL_BACKUP_CONFIG_TABLES
        ui_func_export.restore_config_tables = _ORIGINAL_RESTORE_CONFIG_TABLES
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = _ORIGINAL_GET_CURRENT_TREE
        ui_func_export.find_config_table_files = _ORIGINAL_FIND_CONFIG_TABLES
        ui_func_export.GlobalConfig.path_generate_mod_folder = _ORIGINAL_PATH_GENERATE_MOD_FOLDER
        ui_func_export.ExportRoundExecutor.execute_round = _ORIGINAL_EXECUTE_ROUND

    def _context(self, with_dialog=False):
        window_manager = None
        self.dialog_calls = []
        if with_dialog:
            def _invoke_props_dialog(operator, **kwargs):
                self.dialog_calls.append({"operator": operator, "kwargs": kwargs})
                return {'RUNNING_MODAL'}

            window_manager = types.SimpleNamespace(invoke_props_dialog=_invoke_props_dialog)
        return types.SimpleNamespace(
            scene=types.SimpleNamespace(
                global_properties=types.SimpleNamespace(selected_blueprint_name="")
            ),
            window_manager=window_manager,
        )

    def _run_export(self, **operator_attrs):
        for key, value in operator_attrs.items():
            setattr(self.operator, key, value)
        return self.operator.execute(self._context())

    def _assert_backup_args(self):
        self.assertEqual(len(self.backup_calls), 1, "弹窗确认后应先备份旧配置表")
        call = self.backup_calls[0]
        self.assertEqual(
            call["args"], (self.config_dir, [self.ini_path]),
            "backup 实参必须是 (导出目录, 配置表路径列表)",
        )
        self.assertEqual(call["kwargs"], {}, "backup 不应收到额外 kwargs")

    def test_overwrite_yes_keeps_new_config_and_skips_restore(self):
        result = self._run_export(
            _backup_pending=True,
            _config_table_paths=[self.ini_path],
            overwrite_config='YES',
        )

        self.assertEqual(result, {'CANCELLED'})
        self._assert_backup_args()
        self.assertEqual(self.restore_calls, [], "选择「是」时不得把旧配置表覆盖回原位")
        self.assertEqual(
            self.events, ["backup", "export_attempt"],
            "顺序必须是「备份 → 导出尝试」，且不得恢复",
        )

    def test_overwrite_no_restores_old_config_after_export(self):
        result = self._run_export(
            _backup_pending=True,
            _config_table_paths=[self.ini_path],
            overwrite_config='NO',
        )

        self.assertEqual(result, {'CANCELLED'})
        self._assert_backup_args()
        self.assertEqual(len(self.restore_calls), 1, "选择「否」时导出完成后应恢复旧配置表")
        self.assertEqual(
            self.restore_calls[0]["args"], (self.backup_entries,),
            "restore 必须收到 backup 返回的 entries（原路径/备份路径对）",
        )
        self.assertEqual(self.restore_calls[0]["kwargs"], {}, "restore 不应收到额外 kwargs")
        self.assertEqual(
            self.events, ["backup", "export_attempt", "restore"],
            "restore 必须在导出尝试**之后**才发生",
        )

    def test_without_dialog_no_backup_no_restore(self):
        result = self._run_export()

        self.assertEqual(result, {'CANCELLED'})
        self.assertEqual(self.backup_calls, [], "未弹窗确认时不应备份")
        self.assertEqual(self.restore_calls, [], "未弹窗确认时不应恢复")
        self.assertEqual(self.events, ["export_attempt"])

    def test_invoke_dialog_opens_with_config_tables_and_sets_state(self):
        """invoke()：有后处理节点 + 导出目录已存在 *.ini → 弹窗并挂上备份状态。"""
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = (
            lambda **_kwargs: _FakeTreeWithPostprocess()
        )
        context = self._context(with_dialog=True)

        result = self.operator.invoke(context, types.SimpleNamespace())

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(len(self.dialog_calls), 1, "应且只应弹出一次确认对话框")
        self.assertIs(self.dialog_calls[0]["operator"], self.operator)
        self.assertEqual(self.dialog_calls[0]["kwargs"], {"width": 460})
        self.assertTrue(getattr(self.operator, "_backup_pending", False))
        self.assertEqual(self.operator._config_table_paths, [self.ini_path])
        self.assertEqual(self.operator._export_path, self.config_dir)

    def test_invoke_without_config_tables_executes_directly(self):
        """invoke()：目录无配置表 → 不弹窗、不置 _backup_pending，直接走 execute。"""
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = (
            lambda **_kwargs: _FakeTreeWithPostprocess()
        )
        ui_func_export.find_config_table_files = lambda _path: []
        context = self._context(with_dialog=True)

        result = self.operator.invoke(context, types.SimpleNamespace())

        self.assertEqual(result, {'CANCELLED'})
        self.assertEqual(self.dialog_calls, [], "无配置表时不得弹窗")
        self.assertFalse(getattr(self.operator, "_backup_pending", False))
        self.assertEqual(self.events, ["export_attempt"])
        self.assertEqual(self.backup_calls, [])


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
