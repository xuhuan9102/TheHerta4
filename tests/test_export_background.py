"""后台导出（``blueprint/export_background.py``）单元测试 —— fake bpy，不启动真实 Blender。

覆盖：

1. manifest 写入/读取（含全部必需字段）；
2. stage.json 三种状态解析（running / success / error）与进度文本；
3. ``BackgroundExportSession`` 用 fake Popen 测 start / poll 状态机 / cancel / cleanup；
4. worker 侧 ``_run_worker`` 的轮次串行、后处理、result.json 成功与失败；
5. 移除模式开关，旧工程中的关闭值也不能走前台计算；
6. invoke / execute 两种入口统一启动后台会话。

装载方式沿用 ``tests/test_blueprint_output_folder.py`` 的 fake-package 范式：
被测的是**真实**的 ``export_background.py``、``ui_func_export.py``、``global_properties.py``，
只有 bpy / 日志 / 形态键工具 / 并行导出等外部依赖打桩。
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path


PKG = "_background_export_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.ui", f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


# ---------------------------------------------------------------------------
# fake bpy（含 area header 记录，用来断言「进度显示在状态栏」）
# ---------------------------------------------------------------------------


class _FakeArea:
    def __init__(self):
        self.header = "<unset>"
        self.redraws = 0

    def header_text_set(self, text):
        self.header = text

    def tag_redraw(self):
        self.redraws += 1


_FAKE_AREAS = [_FakeArea()]


class _FakeScreen:
    def __init__(self):
        self.areas = _FAKE_AREAS


class _FakeWindow:
    def __init__(self):
        self.screen = _FakeScreen()


class _FakeWindowManager:
    def __init__(self):
        self.windows = [_FakeWindow()]
        self.timers = []
        self.removed_timers = []
        self.modal_handlers = []
        # invoke_props_dialog 调用记录（旧配置表「覆盖 / 保留」确认框）
        self.dialogs = []
        # 原生进度条调用记录（后台导出的步骤进度条）
        self.progress_calls = []
        self.progress_fail = False

    def event_timer_add(self, interval, window=None):
        timer = object()
        self.timers.append({"interval": interval, "timer": timer, "window": window})
        return timer

    def event_timer_remove(self, timer):
        self.removed_timers.append(timer)

    def modal_handler_add(self, operator):
        self.modal_handlers.append(operator)

    def invoke_props_dialog(self, operator, width=0):
        self.dialogs.append((operator, width))
        return {'RUNNING_MODAL'}

    def progress_begin(self, min_value, max_value):
        if self.progress_fail:
            raise RuntimeError("progress API unavailable")
        self.progress_calls.append(("begin", min_value, max_value))

    def progress_update(self, value):
        if self.progress_fail:
            raise RuntimeError("progress API unavailable")
        self.progress_calls.append(("update", value))

    def progress_end(self):
        if self.progress_fail:
            raise RuntimeError("progress API unavailable")
        self.progress_calls.append(("end",))


_FAKE_WINDOW_MANAGER = _FakeWindowManager()


class _FakeOperatorBase:
    pass


def _fake_property(**kwargs):
    return dict(kwargs)


_FAKE_BPY = types.ModuleType("bpy")
_FAKE_BPY.types = types.SimpleNamespace(
    Operator=_FakeOperatorBase,
    PropertyGroup=object,
    # 真实 direct_export.py 的函数返回注解会求值 bpy.types.Node
    Node=object,
)
_FAKE_BPY.props = types.SimpleNamespace(
    StringProperty=_fake_property,
    EnumProperty=_fake_property,
    BoolProperty=_fake_property,
    IntProperty=_fake_property,
    FloatProperty=_fake_property,
    PointerProperty=_fake_property,
)
_FAKE_BPY.app = types.SimpleNamespace(
    binary_path="",
    handlers=types.SimpleNamespace(load_post=[]),
)
_FAKE_BPY.data = types.SimpleNamespace(
    node_groups=types.SimpleNamespace(get=lambda _name: None),
    objects=types.SimpleNamespace(get=lambda _name: None),
    scenes=[],
    texts={},
)
_FAKE_BPY.context = types.SimpleNamespace(
    window_manager=_FAKE_WINDOW_MANAGER,
    scene=types.SimpleNamespace(global_properties=types.SimpleNamespace()),
)
_FAKE_BPY.ops = types.SimpleNamespace(
    wm=types.SimpleNamespace(save_as_mainfile=lambda **_kwargs: None)
)
sys.modules["bpy"] = _FAKE_BPY


# ---------------------------------------------------------------------------
# 打桩的外部依赖
# ---------------------------------------------------------------------------


class _FakeLog:
    def __init__(self):
        self.messages = []
        self.collecting = False

    def start_collecting(self, *_args, **_kwargs):
        self.collecting = True
        self.messages = []

    def stop_collecting(self, *_args, **_kwargs):
        self.collecting = False

    def get_log_content(self, strip_ansi=False):
        return "\n".join(self.messages)

    def info(self, message):
        self.messages.append(str(message))

    def warning(self, message):
        self.messages.append("Warning: " + str(message))

    def exception(self, *_args, **_kwargs):
        pass

    def debug(self, message):
        self.messages.append(str(message))

    def print_summary(self, *_args, **_kwargs):
        pass


FAKE_LOG = _FakeLog()
_install_module(f"{PKG}.utils.log_utils", LOG=FAKE_LOG)


class _FakeShapeKeyUtils:
    save_calls = []

    @classmethod
    def save_as_mainfile_with_shape_key_recovery(cls, filepath, copy=False, check_existing=False):
        cls.save_calls.append(
            {"filepath": filepath, "copy": copy, "check_existing": check_existing}
        )
        with open(filepath, "w", encoding="utf-8") as file:
            file.write("fake blend payload")


_install_module(f"{PKG}.utils.shapekey_utils", ShapeKeyUtils=_FakeShapeKeyUtils)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_session=lambda *_args, **_kwargs: None,
        start_stage=lambda *_args, **_kwargs: None,
        end_stage=lambda *_args, **_kwargs: None,
        print_summary=lambda *_args, **_kwargs: None,
        _display_stage_name=lambda stage_name: {
            "Direct-Preprocess": "直出-执行前处理",
        }.get(stage_name, stage_name),
    ),
)
_install_module(f"{PKG}.utils.translate_utils", TR=types.SimpleNamespace(translate=lambda text: text))
_install_module(
    f"{PKG}.utils.command_utils",
    CommandUtils=types.SimpleNamespace(OpenGeneratedModFolder=lambda: None),
)


class _FakeGlobalConfig:
    logic_name = ""
    workspacename = ""
    output_dir = ""

    @classmethod
    def read_from_main_json_ssmt4(cls):
        pass

    @classmethod
    def get_workspace_name(cls):
        return cls.workspacename

    @classmethod
    def get_generate_mod_name(cls, _tree=None):
        return cls.get_workspace_name()

    @classmethod
    def path_generate_mod_folder(cls):
        return cls.output_dir

    @classmethod
    def set_output_blueprint_tree(cls, _tree):
        pass

    @classmethod
    def get_output_blueprint_tree_name(cls):
        return ""

    @classmethod
    def restore_output_blueprint_tree(cls, _name):
        pass


_ORIGINAL_PATH_GENERATE_MOD_FOLDER = _FakeGlobalConfig.__dict__["path_generate_mod_folder"]
_install_module(f"{PKG}.common.global_config", GlobalConfig=_FakeGlobalConfig)


class _Recorder:
    def __init__(self):
        self.calls = []

    def make(self, name, result=None):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return result

        return _record


_EXPORT_ROUND_RECORDER = _Recorder()


def _default_execute_round(**kwargs):
    _EXPORT_ROUND_RECORDER.calls.append(("execute_round", kwargs, {}))
    return {
        "blueprint_model": None,
        "buffer_size": 0,
        "buffer_folder_name": kwargs.get("round_plan", {}).get("buffer_folder_name", ""),
        "object_count": 0,
    }


class _FakeRoundExecutor:
    execute_round = staticmethod(_default_execute_round)


_ORIGINAL_EXECUTE_ROUND = _FakeRoundExecutor.execute_round


_install_module(
    f"{PKG}.blueprint.export_parallel",
    ExportRoundExecutor=_FakeRoundExecutor,
    ParallelExportCoordinator=types.SimpleNamespace(),
    ParallelExportError=RuntimeError,
)
_install_module(
    f"{PKG}.blueprint.preprocess_parallel",
    ParallelPreprocessCoordinator=types.SimpleNamespace(
        get_effective_blender_executable=lambda: ""
    ),
)
_install_module(f"{PKG}.blueprint.sync", refresh_blueprint_sync_state=lambda **_kwargs: {"tree_count": 1, "updated_count": 0})
_install_module(
    f"{PKG}.blueprint.model",
    BluePrintModel=types.SimpleNamespace(clear_object_name_mapping=lambda: None),
)
_install_module(
    f"{PKG}.blueprint.direct_export",
    execute_direct_export=lambda **_kwargs: None,
    has_direct_export_mode=lambda _tree: False,
    collect_direct_shapekey_nodes=lambda _tree: [],
    collect_direct_multifile_nodes=lambda _tree: [],
)
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace(cleanup_copies=lambda **_kwargs: None))
_install_module(f"{PKG}.common.global_key_count_helper", GlobalKeyCountHelper=types.SimpleNamespace(initialize=lambda: None))
_install_module(
    f"{PKG}.common.config_table_backup",
    backup_config_tables=lambda *_args, **_kwargs: [],
    find_config_table_files=lambda *_args, **_kwargs: [],
    restore_config_tables=lambda *_args, **_kwargs: None,
)
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace(NTEMI="NTEMI"))


class _FakeBlueprintExportHelper:
    BLUEPRINT_NONE_IDENTIFIER = "<none>"
    multi_file_export_nodes = []
    runtime_blueprint_tree_name = ""
    current_export_index = 1
    shapekey_objects = []
    #: 后台导出步骤预计算用的后处理节点（测试按需覆盖）
    postprocess_nodes = []

    @staticmethod
    def _collect_postprocess_nodes(*_args, **_kwargs):
        return list(_FakeBlueprintExportHelper.postprocess_nodes)

    @staticmethod
    def get_selected_blueprint_tree(**_kwargs):
        return None

    @staticmethod
    def get_current_blueprint_tree(**_kwargs):
        return None

    @staticmethod
    def set_runtime_blueprint_tree(*_args, **_kwargs):
        pass

    @staticmethod
    def reset_direct_export_runtime_state(*_args, **_kwargs):
        pass

    @staticmethod
    def has_shapekey_postprocess_node(*_args, **_kwargs):
        return False

    @staticmethod
    def calculate_max_shapekey_slot_count(*_args, **_kwargs):
        return 0

    @staticmethod
    def calculate_max_export_count(*_args, **_kwargs):
        return 1

    @staticmethod
    def has_multi_file_export_nodes(*_args, **_kwargs):
        return False

    @staticmethod
    def _is_node_connected_to_output(_tree, node):
        """参与导出节点判定（真实插件的私有静态方法）：测试里用属性控制。"""
        return bool(getattr(node, "connected_to_output", True))

    @staticmethod
    def collect_shapekey_objects(*_args, **_kwargs):
        return []

    @staticmethod
    def set_all_shapekey_values(*_args, **_kwargs):
        pass

    @staticmethod
    def get_current_buffer_folder_name():
        return ""

    @staticmethod
    def set_current_export_index(*_args, **_kwargs):
        pass

    @staticmethod
    def set_current_buffer_folder_name(*_args, **_kwargs):
        pass


_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=_FakeBlueprintExportHelper)


def _load_real_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# 真实模块：开关声明（默认 False）
global_properties_module = _load_real_module(
    f"{PKG}.common.global_properties", Path("common") / "global_properties.py"
)
# 真实模块：细粒度进度中心（worker 用，export_background 导入时会引用它）
export_progress = _load_real_module(
    f"{PKG}.blueprint.export_progress", Path("blueprint") / "export_progress.py"
)
# 真实模块：固定步骤计划（export_background / ui_func_export / direct_export 共用）
export_step_plan = _load_real_module(
    f"{PKG}.blueprint.export_step_plan", Path("blueprint") / "export_step_plan.py"
)
# 真实模块：后台导出
process_log = _load_real_module(
    f"{PKG}.utils.process_log", Path("utils") / "process_log.py"
)


export_background = _load_real_module(
    f"{PKG}.blueprint.export_background", Path("blueprint") / "export_background.py"
)
# 真实模块：导出算子（它会 import 上面两个真实模块）
ui_func_export = _load_real_module(
    f"{PKG}.ui.ui_func_export", Path("ui") / "ui_func_export.py"
)

# 真实模块：直出导出（只测阶段节点上报辅助函数）。
# direct_export.py 会 import 各游戏导出器与直出子模块，这里按需补最小桩。
for _package_name in (f"{PKG}.ui.universal", f"{PKG}.ui.wwmi"):
    _package = _install_module(_package_name)
    _package.__path__ = []
for _module_name, _class_name in (
    ("efmi", "ExportEFMI"),
    ("gimi", "ExportGIMI"),
    ("himi", "ExportHIMI"),
    ("identityv", "ExportIdentityV"),
    ("snowbreak", "ExportSnowBreak"),
    ("srmi", "ExportSRMI"),
    ("unity", "ExportUnity"),
    ("yysls", "ExportYYSLS"),
    ("zzmi", "ExportZZMI"),
):
    _install_module(
        f"{PKG}.ui.universal.{_module_name}",
        **{_class_name: type(_class_name, (), {})},
    )
_install_module(
    f"{PKG}.ui.wwmi.wwmi_export",
    ExportWWMI=type("ExportWWMI", (), {}),
)
_install_module(
    f"{PKG}.blueprint.direct_export_multifile",
    DirectMultiFileGenerator=type("DirectMultiFileGenerator", (), {}),
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey",
    DirectShapeKeyGenerator=type("DirectShapeKeyGenerator", (), {}),
)
direct_export_module = _load_real_module(
    f"{PKG}.blueprint.direct_export_node_progress",
    Path("blueprint") / "direct_export.py",
)


# ---------------------------------------------------------------------------
# fake Popen
# ---------------------------------------------------------------------------


class _FakeProcess:
    instances = []
    raise_on_start = None
    ignore_terminate = False

    def __init__(self, args, **kwargs):
        if _FakeProcess.raise_on_start is not None:
            raise _FakeProcess.raise_on_start
        self.args = list(args)
        self.kwargs = dict(kwargs)
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False
        _FakeProcess.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not _FakeProcess.ignore_terminate:
            self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


_ORIGINAL_SUBPROCESS = export_background.subprocess
_ORIGINAL_KILL_GRACE = export_background.KILL_GRACE_SECONDS
_ORIGINAL_TERMINATE_INTERVAL = export_background.TERMINATE_POLL_INTERVAL

_FAKE_SUBPROCESS = types.SimpleNamespace(
    Popen=_FakeProcess,
    STDOUT=_ORIGINAL_SUBPROCESS.STDOUT,
    DEVNULL=_ORIGINAL_SUBPROCESS.DEVNULL,
)

# 先把模块里的 subprocess 换成假的，避免污染真实 stdlib 模块
export_background.subprocess = _FAKE_SUBPROCESS


class _BackgroundTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="herta_bg_test_")
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.addCleanup(self._restore_globals)

        _FakeProcess.instances = []
        _FakeProcess.raise_on_start = None
        _FakeProcess.ignore_terminate = False
        export_background.subprocess = _FAKE_SUBPROCESS
        export_background.KILL_GRACE_SECONDS = 0.05
        export_background.TERMINATE_POLL_INTERVAL = 0.01

        _FakeShapeKeyUtils.save_calls = []
        _EXPORT_ROUND_RECORDER.calls = []
        _FAKE_AREAS[0].header = "<unset>"
        _FAKE_AREAS[0].redraws = 0
        _FAKE_WINDOW_MANAGER.timers = []
        _FAKE_WINDOW_MANAGER.removed_timers = []
        _FAKE_WINDOW_MANAGER.modal_handlers = []
        _FAKE_WINDOW_MANAGER.dialogs = []
        _FAKE_WINDOW_MANAGER.progress_calls = []
        _FAKE_WINDOW_MANAGER.progress_fail = False
        _FakeBlueprintExportHelper.postprocess_nodes = []

        # 后台导出防重入：每个用例都从"无活动会话"开始
        export_background.clear_active_session()
        self.addCleanup(export_background.clear_active_session)
        # 快速导出的 worker 分支会从 bpy.data.objects 取网格
        _FAKE_BPY.data.objects.get = lambda _name: None

        _FakeGlobalConfig.logic_name = ""
        _FakeGlobalConfig.workspacename = ""
        _FakeGlobalConfig.output_dir = ""

        fake_scene_props = types.SimpleNamespace(
            open_mod_folder_after_generate_mod=False,
        )
        _FAKE_BPY.context.scene = types.SimpleNamespace(global_properties=fake_scene_props)
        self.scene_properties = fake_scene_props

    def _restore_globals(self):
        export_background.subprocess = _ORIGINAL_SUBPROCESS
        export_background.KILL_GRACE_SECONDS = _ORIGINAL_KILL_GRACE
        export_background.TERMINATE_POLL_INTERVAL = _ORIGINAL_TERMINATE_INTERVAL
        setattr(
            _FakeGlobalConfig,
            "path_generate_mod_folder",
            _ORIGINAL_PATH_GENERATE_MOD_FOLDER,
        )
        _FAKE_BPY.data.node_groups.get = lambda _name: None
        _FakeRoundExecutor.execute_round = staticmethod(_ORIGINAL_EXECUTE_ROUND)

    def make_session(self, **overrides):
        params = {
            "tree_name": "测试蓝图",
            "output_dir": os.path.join(self.temp_dir, "out"),
            "round_plans": [
                {
                    "round_index": 1,
                    "phase": "single",
                    "description": "单次导出",
                    "buffer_folder_name": "Meshes",
                    "export_index": 1,
                    "generate_ini": True,
                    "generate_classification_report": False,
                    "shapekey_mode": "unchanged",
                    "shapekey_slot_index": None,
                }
            ],
            "logic_name": "EFMI",
            "workspace_name": "测试角色",
            "blender_executable": "C:/fake/blender.exe",
            "addon_name": "TheHerta4-main",
            "session_dir": os.path.join(self.temp_dir, "session"),
        }
        params.update(overrides)
        os.makedirs(params["session_dir"], exist_ok=True)
        return export_background.BackgroundExportSession(**params)


# ---------------------------------------------------------------------------
# 1. manifest / stage
# ---------------------------------------------------------------------------


class ManifestTests(_BackgroundTestBase):
    def test_build_manifest_writes_all_required_fields(self):
        session_dir = os.path.join(self.temp_dir, "manifest_session")
        os.makedirs(session_dir)
        round_plans = [{"round_index": 1, "buffer_folder_name": "Meshes"}]

        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="蓝图A",
            output_dir=os.path.join(self.temp_dir, "out"),
            round_plans=round_plans,
            logic_name="EFMI",
            workspace_name="角色A",
            has_shapekey_export=True,
            max_shapekey_slot=2,
            has_multi_file_export=True,
            max_multi_file_count=3,
        )

        required_keys = {
            "snapshot_path",
            "tree_name",
            "output_dir",
            "logic_name",
            "workspace_name",
            "round_plans",
            "has_shapekey_export",
            "max_shapekey_slot",
            "has_multi_file_export",
            "max_multi_file_count",
            "stage_path",
            "result_path",
            "log_path",
        }
        self.assertTrue(required_keys.issubset(manifest.keys()))

        manifest_path = os.path.join(session_dir, export_background.MANIFEST_FILE_NAME)
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path, "r", encoding="utf-8") as file:
            loaded = json.load(file)
        self.assertEqual(loaded, manifest)
        self.assertEqual(loaded["round_plans"], round_plans)
        self.assertEqual(loaded["manifest_path"], manifest_path)

    def test_build_manifest_records_quick_export_fields(self):
        """快速局部导出的清单要带 quick_export 与 object_names。"""
        session_dir = os.path.join(self.temp_dir, "manifest_quick")
        os.makedirs(session_dir)

        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="",
            output_dir=os.path.join(self.temp_dir, "out"),
            round_plans=[],
            quick_export=True,
            object_names=["M1", "", "M2"],
            total_steps=2,
        )

        self.assertTrue(manifest["quick_export"])
        self.assertEqual(manifest["object_names"], ["M1", "M2"])
        self.assertEqual(manifest["total_steps"], 2)

    def test_build_manifest_records_quick_export_modifier_states(self):
        """快速局部导出要冻结每个修改器在点击导出时的实际开关状态。"""
        session_dir = os.path.join(self.temp_dir, "manifest_quick_modifiers")
        os.makedirs(session_dir)

        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="",
            output_dir=os.path.join(self.temp_dir, "out"),
            round_plans=[],
            quick_export=True,
            quick_export_modifier_states={
                "M1": {
                    "关闭的修改器": False,
                    "打开的修改器": True,
                }
            },
            object_names=["M1"],
            total_steps=2,
        )

        self.assertEqual(
            manifest["quick_export_modifier_states"],
            {
                "M1": {
                    "关闭的修改器": False,
                    "打开的修改器": True,
                }
            },
        )

    def test_build_manifest_records_preprocess_cache_dir(self):
        """主进程算好的前处理缓存目录必须进清单，worker 才有得用。"""
        session_dir = os.path.join(self.temp_dir, "manifest_cache")
        os.makedirs(session_dir)
        cache_dir = os.path.join(self.temp_dir, ".ssmt_preprocess_cache")

        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="测试蓝图",
            output_dir=os.path.join(self.temp_dir, "out_cache"),
            round_plans=[{"round_index": 1, "buffer_folder_name": "Meshes"}],
            preprocess_cache_dir=cache_dir,
        )

        self.assertEqual(manifest["preprocess_cache_dir"], cache_dir)
        with open(manifest["manifest_path"], "r", encoding="utf-8") as file:
            self.assertEqual(json.load(file)["preprocess_cache_dir"], cache_dir)

    def test_build_manifest_defaults_preprocess_cache_dir_to_empty(self):
        session_dir = os.path.join(self.temp_dir, "manifest_cache_default")
        os.makedirs(session_dir)

        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="测试蓝图",
            output_dir=os.path.join(self.temp_dir, "out_cache_default"),
            round_plans=[{"round_index": 1, "buffer_folder_name": "Meshes"}],
        )

        self.assertEqual(manifest["preprocess_cache_dir"], "")

    def test_parse_manifest_argument(self):
        argv = ["blender", "--background", "--", "--manifest", "C:/tmp/m.json"]
        self.assertEqual(export_background._parse_manifest_argument(argv), "C:/tmp/m.json")
        self.assertEqual(export_background._parse_manifest_argument(["blender"]), "")

    def test_save_export_snapshot_uses_copy(self):
        session_dir = os.path.join(self.temp_dir, "snapshot_session")
        os.makedirs(session_dir)

        path = export_background.save_export_snapshot(session_dir)

        self.assertTrue(path.endswith(export_background.SNAPSHOT_FILE_NAME))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(len(_FakeShapeKeyUtils.save_calls), 1)
        self.assertTrue(_FakeShapeKeyUtils.save_calls[0]["copy"])
        self.assertFalse(_FakeShapeKeyUtils.save_calls[0]["check_existing"])

    def test_worker_command_handles_hyphenated_addon_name(self):
        manifest = {
            "snapshot_path": "C:/tmp/snapshot.blend",
            "manifest_path": "C:/tmp/manifest.json",
        }
        command = export_background.build_worker_command(
            manifest, blender_executable="C:/fake/blender.exe", addon_name="TheHerta4-main"
        )

        self.assertEqual(command[0], "C:/fake/blender.exe")
        self.assertEqual(command[1], "C:/tmp/snapshot.blend")
        self.assertIn("--background", command)
        self.assertIn("--factory-startup", command)
        self.assertEqual(command[command.index("--addons") + 1], "TheHerta4-main")
        python_expr = command[command.index("--python-expr") + 1]
        # 目录名带连字符，必须走 importlib（import 语句不合法）
        self.assertIn("importlib.import_module", python_expr)
        self.assertIn("TheHerta4-main.blueprint.export_background", python_expr)
        self.assertEqual(command[-2:], ["--manifest", "C:/tmp/manifest.json"])


# ---------------------------------------------------------------------------
# 2/3. 会话：start / poll / cancel / cleanup
# ---------------------------------------------------------------------------


class BackgroundSessionTests(_BackgroundTestBase):
    def test_worker_log_reaches_console_and_text_block_without_replaying(self):
        class TextBlock:
            content = "old export"

            def clear(self):
                self.content = ""

            @property
            def lines(self):
                return [types.SimpleNamespace(body=line) for line in self.content.split("\n")]

            def cursor_set(self, line, character=0):
                self.at_end = (line, character) == (len(self.lines) - 1, len(self.lines[-1].body))

            def write(self, text):
                assert self.at_end
                self.content += text

        block = TextBlock()
        session = self.make_session()
        self.assertTrue(session.start())
        with mock.patch.object(_FAKE_BPY.data, "texts", {"导出流程日志": block}), mock.patch("builtins.print") as output:
            data = "\x1b[33m中文警告\x1b[0m\n".encode("utf-8")
            with open(session.log_path, "ab") as log:
                log.write(data)
            self.assertEqual(session.poll(), "running")
            self.assertEqual(block.content, "中文警告\n")
            session.poll()
            self.assertEqual(output.call_count, 1)
            with open(session.log_path, "ab") as log:
                log.write("Traceback: 最后异常".encode("utf-8"))
            _FakeProcess.instances[0].returncode = 1
            self.assertEqual(session.poll(), "error")
            session.cleanup()
            self.assertEqual(block.content, "中文警告\nTraceback: 最后异常")
            self.assertEqual(output.call_count, 2)
            self.assertEqual(Path(session.log_path).read_bytes(), data + "Traceback: 最后异常".encode("utf-8"))

    def test_cancel_drains_output_not_yet_seen_by_poll(self):
        session = self.make_session()
        self.assertTrue(session.start())
        Path(session.log_path).write_bytes("取消前最后一条错误".encode("utf-8"))
        with mock.patch("builtins.print") as output:
            session.cancel()
            session.cleanup()
        output.assert_called_once_with("取消前最后一条错误", end="", flush=True)

    def test_worker_environment_enables_unbuffered_utf8_output(self):
        session = self.make_session()
        self.assertTrue(session.start())
        environment = _FakeProcess.instances[0].kwargs["env"]
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(environment["HERTA_WORKER_LOG_REDIRECTED"], "1")
        session.cleanup()

    def test_start_launches_detached_process_and_writes_manifest(self):
        session = self.make_session()

        self.assertTrue(session.start())
        self.assertEqual(session.status, "running")
        self.assertEqual(len(_FakeProcess.instances), 1)
        # 起进程后、worker 写出第一个 stage.json 之前：状态栏显示「加载工程」
        # （Blender 要先加载 480MB 快照，约 10 秒，这期间没有任何 stage 文件）
        self.assertIn("加载工程", session.progress_text())
        self.assertIn("加载工程", session.stage_message)

        process = _FakeProcess.instances[0]
        self.assertEqual(process.args[0], "C:/fake/blender.exe")
        self.assertIn("--background", process.args)
        self.assertIn("--factory-startup", process.args)
        # 子进程输出直接写日志文件，主进程不读管道（不会被管道阻塞）
        self.assertEqual(process.kwargs["stderr"], export_background.subprocess.STDOUT)
        self.assertTrue(os.path.isfile(session.log_path))
        self.assertTrue(os.path.isfile(session.manifest_path))
        self.assertTrue(os.path.isfile(session.snapshot_path))
        with open(session.manifest_path, "r", encoding="utf-8") as file:
            manifest = json.load(file)
        self.assertEqual(manifest["tree_name"], "测试蓝图")
        self.assertEqual(manifest["output_dir"], os.path.join(self.temp_dir, "out"))

        session.cleanup()

    def test_start_passes_preprocess_cache_dir_into_manifest(self):
        """主进程算好的缓存目录随清单传给 worker（否则 worker 用快照旁的空目录）。"""
        cache_dir = os.path.join(self.temp_dir, ".ssmt_preprocess_cache")
        original = export_background._default_preprocess_cache_dir
        export_background._default_preprocess_cache_dir = lambda: cache_dir
        self.addCleanup(setattr, export_background, "_default_preprocess_cache_dir", original)
        session = self.make_session()

        self.assertTrue(session.start())

        self.assertEqual(session.preprocess_cache_dir, cache_dir)
        self.assertEqual(session.manifest["preprocess_cache_dir"], cache_dir)

    def test_explicit_preprocess_cache_dir_wins_over_default(self):
        explicit = os.path.join(self.temp_dir, "explicit_cache")
        original = export_background._default_preprocess_cache_dir
        export_background._default_preprocess_cache_dir = lambda: (_ for _ in ()).throw(
            AssertionError("显式给了缓存目录就不该再推默认值")
        )
        self.addCleanup(setattr, export_background, "_default_preprocess_cache_dir", original)
        session = self.make_session(preprocess_cache_dir=explicit)

        self.assertTrue(session.start())

        self.assertEqual(session.manifest["preprocess_cache_dir"], explicit)

    def test_start_returns_false_when_popen_fails(self):
        _FakeProcess.raise_on_start = OSError("no blender")
        session = self.make_session()

        self.assertFalse(session.start())
        self.assertEqual(session.status, "error")
        self.assertIn("no blender", session.error)

    def test_poll_running_then_success(self):
        session = self.make_session()
        self.assertTrue(session.start())
        process = _FakeProcess.instances[0]

        with open(session.stage_path, "w", encoding="utf-8") as file:
            json.dump({"stage": "后处理", "done": 3, "total": 7, "message": "后处理节点"}, file, ensure_ascii=False)

        self.assertEqual(session.poll(), "running")
        self.assertEqual(session.stage, "后处理")
        self.assertEqual(session.stage_done, 3)
        self.assertEqual(session.stage_total, 7)
        self.assertEqual(session.progress_text(), "后台导出：3/7 后处理")

        with open(session.result_path, "w", encoding="utf-8") as file:
            json.dump({"status": "success", "error": "", "rounds": 2, "buffer_sizes": [10, 10]}, file)
        process.returncode = 0

        self.assertEqual(session.poll(), "success")
        self.assertEqual(session.progress_text(), "后台导出完成")
        self.assertIn("2 轮", session.success_summary())

        session.cleanup()

    def test_poll_error_reads_result_json(self):
        session = self.make_session()
        self.assertTrue(session.start())
        process = _FakeProcess.instances[0]

        with open(session.result_path, "w", encoding="utf-8") as file:
            json.dump({"status": "error", "error": "蓝图解析失败", "rounds": 0, "buffer_sizes": []}, file, ensure_ascii=False)
        process.returncode = 1

        self.assertEqual(session.poll(), "error")
        self.assertEqual(session.error, "蓝图解析失败")
        self.assertEqual(session.progress_text(), "后台导出失败")
        self.assertIn(session.log_path, session.error_summary())

        session.cleanup()

    def test_poll_error_without_result_reports_exit_code(self):
        session = self.make_session()
        self.assertTrue(session.start())
        _FakeProcess.instances[0].returncode = 1

        self.assertEqual(session.poll(), "error")
        self.assertIn("退出码 1", session.error)
        session.cleanup()

    def test_refresh_stage_parses_node_nodes_and_elapsed(self):
        """细粒度快照：解析当前节点 / 节点状态表 / 已耗时，并拼进进度文本。"""
        session = self.make_session()
        self.assertTrue(session.start())
        _FakeProcess.instances[0].returncode = None

        with open(session.stage_path, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "stage": "直出-形态键生成",
                    "done": 2,
                    "total": 3,
                    "message": "",
                    "node": "形态键节点",
                    "nodes": {"形态键节点": "running", "多文件节点": "done"},
                    "elapsed": 12.5,
                },
                file,
                ensure_ascii=False,
            )

        self.assertEqual(session.poll(), "running")
        self.assertEqual(session.stage_node, "形态键节点")
        self.assertEqual(
            session.stage_nodes,
            {"形态键节点": "running", "多文件节点": "done"},
        )
        self.assertAlmostEqual(session.stage_elapsed, 12.5)
        self.assertEqual(session.progress_text(), "后台导出：2/3 直出-形态键生成｜形态键节点")

        # 粗粒度快照（_write_stage 写的）没有子状态字段：保留上一次的节点状态
        with open(session.stage_path, "w", encoding="utf-8") as file:
            json.dump({"stage": "第 2 轮", "done": 1, "total": 2, "message": ""}, file, ensure_ascii=False)
        session._refresh_stage()

        self.assertEqual(session.stage, "第 2 轮")
        self.assertEqual(session.stage_node, "形态键节点")
        self.assertEqual(
            session.stage_nodes,
            {"形态键节点": "running", "多文件节点": "done"},
        )
        session.cleanup()

    def test_cancel_terminates_and_marks_cancelled(self):
        session = self.make_session()
        self.assertTrue(session.start())
        process = _FakeProcess.instances[0]

        session.cancel()

        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(session.status, "cancelled")
        self.assertEqual(session.progress_text(), "后台导出已取消")
        session.cleanup()

    def test_cancel_kills_process_that_ignores_terminate(self):
        _FakeProcess.ignore_terminate = True
        session = self.make_session()
        self.assertTrue(session.start())
        process = _FakeProcess.instances[0]

        session.cancel()

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(session.status, "cancelled")
        session.cleanup()

    def test_cleanup_removes_snapshot_and_manifest_but_keeps_log(self):
        session = self.make_session()
        self.assertTrue(session.start())
        snapshot_path = session.snapshot_path
        manifest_path = session.manifest_path
        log_path = session.log_path
        backup_path = snapshot_path + "1"  # Blender 的 .blend1 版本备份
        with open(backup_path, "w", encoding="utf-8") as file:
            file.write("backup")

        returned_log_path = session.cleanup()

        self.assertEqual(returned_log_path, log_path)
        self.assertFalse(os.path.exists(snapshot_path))
        self.assertFalse(os.path.exists(backup_path))
        self.assertFalse(os.path.exists(manifest_path))
        self.assertTrue(os.path.isfile(log_path))
        self.assertEqual(session.result.get("log_path"), log_path)
        self.assertTrue(session.cleaned)


# ---------------------------------------------------------------------------
# 3b. 总用时（start → 完成）
# ---------------------------------------------------------------------------


class ElapsedTimingTests(_BackgroundTestBase):
    """后台导出总用时：start() 起算、elapsed_seconds() 单调增长、收尾文案带用时。"""

    START = 100.0

    def _header(self, session):
        return ui_func_export.SSMTGenerateModBlueprint._completion_header(session)

    def test_elapsed_seconds_zero_before_start(self):
        """没有 start() 就不计时：秒数 0、用时文案为空，收尾退化为原文案。"""
        session = self.make_session()

        self.assertEqual(session.elapsed_seconds(), 0.0)
        self.assertEqual(session.elapsed_text(), "")

    def test_elapsed_seconds_positive_and_grows_after_start(self):
        """start() 记下开始时刻；之后重复调用返回当前时长（桩时间让它一直增长）。"""
        session = self.make_session()
        # start() 读一次（100.0），之后每次读都在往后走
        with mock.patch.object(
            export_background.time,
            "monotonic",
            side_effect=[self.START, 138.6, 140.0, 140.0, 140.0],
        ):
            self.assertTrue(session.start())
            first = session.elapsed_seconds()
            second = session.elapsed_seconds()
            third = session.elapsed_seconds()
            label = session.elapsed_text()

        self.assertGreater(first, 0.0)
        self.assertAlmostEqual(first, 38.6)
        self.assertGreater(second, first)
        self.assertAlmostEqual(third, second)
        self.assertEqual(label, "用时 40.0 秒")
        session.cleanup()

    def test_elapsed_text_keeps_one_decimal_and_seconds_over_60(self):
        """保留 1 位小数；超过 60 秒也照常显示秒数，不换算成分钟。"""
        session = self.make_session()
        session.started_monotonic = self.START

        with mock.patch.object(
            export_background.time, "monotonic", side_effect=[225.34, 225.34]
        ):
            self.assertEqual(session.elapsed_text(), "用时 125.3 秒")
            self.assertAlmostEqual(session.elapsed_seconds(), 125.34)

    def test_completion_header_with_steps_includes_elapsed(self):
        session = self.make_session()
        session.done_steps = 21
        session.total_steps = 21
        session.started_monotonic = self.START

        with mock.patch.object(export_background.time, "monotonic", return_value=138.6):
            header = self._header(session)

        self.assertEqual(header, "后台导出完成（21/21，用时 38.6 秒）")

    def test_completion_header_without_steps_includes_elapsed(self):
        session = self.make_session()
        session.done_steps = 0
        session.total_steps = 0
        session.started_monotonic = self.START

        with mock.patch.object(export_background.time, "monotonic", return_value=138.6):
            header = self._header(session)

        self.assertEqual(header, "后台导出完成（用时 38.6 秒）")

    def test_completion_header_is_silent_when_elapsed_raises(self):
        """计时相关异常不能影响收尾：文案退化为不带用时的原文案。"""
        session = self.make_session()
        session.done_steps = 2
        session.total_steps = 2

        def _boom():
            raise RuntimeError("计时坏了")

        session.elapsed_seconds = _boom
        session.elapsed_text = _boom

        self.assertEqual(self._header(session), "后台导出完成（2/2）")

    def test_success_summary_includes_elapsed_once(self):
        """成功摘要追加「用时 X 秒」；重复调用不会重复添加。"""
        session = self.make_session()
        session.result = {"status": "success", "rounds": 2, "warnings": ["缺失变量: $x"]}
        session.started_monotonic = self.START

        with mock.patch.object(export_background.time, "monotonic", return_value=138.6):
            first = session.success_summary()
            second = session.success_summary()

        self.assertIn("后台导出完成，共 2 轮", first)
        self.assertIn("自检警告", first)
        self.assertIn("用时 38.6 秒", first)
        self.assertEqual(first.count("用时"), 1)
        self.assertEqual(second, first)


# ---------------------------------------------------------------------------
# 4. worker 全链路（fake ExportRoundExecutor / fake 蓝图模型）
# ---------------------------------------------------------------------------


class _FakeBlueprintModel:
    def __init__(self):
        self.postprocess_paths = []

    def execute_postprocess_nodes(self, output_dir):
        self.postprocess_paths.append(output_dir)


class WorkerTests(_BackgroundTestBase):
    def test_ntmi_worker_uses_selected_output_node_and_foreground_resolved_directory(self):
        session_dir = os.path.join(self.temp_dir, "ntmi_worker")
        os.makedirs(session_dir)
        output_dir = os.path.join(self.temp_dir, "ntmi_output")
        manifest = self._write_manifest(session_dir, [], output_dir)
        manifest["ntmi_output_node_name"] = "指定输出"
        export_background._write_json(manifest["manifest_path"], manifest)
        node = types.SimpleNamespace(name="指定输出", bl_idname="SSMTNode_Result_Output_NTMIModImp",
                                     use_custom_export_dir=False, export_dir="//relative")
        tree = types.SimpleNamespace(name="测试蓝图", nodes={node.name: node})
        engine = types.ModuleType(f"{PKG}.blueprint.ntmi_export_modimp")
        engine.execute_ntmi_modimp_export = mock.Mock()
        with mock.patch.dict(sys.modules, {engine.__name__: engine}), mock.patch.object(_FAKE_BPY.data.node_groups, "get", return_value=tree):
            export_background._run_worker(manifest["manifest_path"])
        result = export_background._read_json(manifest["result_path"])
        self.assertEqual(result["status"], "success")
        engine.execute_ntmi_modimp_export.assert_called_once_with(context=_FAKE_BPY.context, tree=tree, node=node)
        self.assertTrue(node.use_custom_export_dir)
        self.assertEqual(node.export_dir, output_dir)
        stage = export_background._read_json(manifest["stage_path"])
        self.assertEqual(stage["done"], len(export_background.NTMI_MODIMP_STAGE_STEPS))
        self.assertEqual(stage["total"], stage["done"])

    def test_redirected_worker_preserves_native_output_instead_of_rewriting_log(self):
        session_dir = os.path.join(self.temp_dir, "native_output")
        os.makedirs(session_dir)
        manifest = self._write_manifest(session_dir, [], os.path.join(self.temp_dir, "out"))
        original = b"Blender startup\nnative engine output\n"
        Path(manifest["log_path"]).write_bytes(original)
        with mock.patch.dict(os.environ, {"HERTA_WORKER_LOG_REDIRECTED": "1"}), mock.patch.object(export_background.traceback, "print_exc"):
            export_background._run_worker(manifest["manifest_path"])
        self.assertEqual(Path(manifest["log_path"]).read_bytes(), original)
        self.assertTrue(Path(manifest["result_path"]).is_file())

    def _write_manifest(self, session_dir, round_plans, output_dir):
        return export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="测试蓝图",
            output_dir=output_dir,
            round_plans=round_plans,
            logic_name="EFMI",
            workspace_name="测试角色",
            has_multi_file_export=True,
            max_multi_file_count=len(round_plans),
        )

    def test_worker_runs_all_rounds_then_postprocess(self):
        session_dir = os.path.join(self.temp_dir, "worker_ok")
        os.makedirs(session_dir)
        output_dir = os.path.join(self.temp_dir, "out_ok")
        round_plans = [
            {"round_index": 1, "description": "形态键基态", "buffer_folder_name": "Meshes0000", "export_index": 1},
            {"round_index": 2, "description": "形态键槽位 1", "buffer_folder_name": "Meshes1001", "export_index": 1},
        ]
        manifest = self._write_manifest(session_dir, round_plans, output_dir)

        model = _FakeBlueprintModel()
        _EXPORT_ROUND_RECORDER.calls = []

        def _fake_execute_round(**kwargs):
            _EXPORT_ROUND_RECORDER.calls.append(("execute_round", kwargs, {}))
            return {
                "blueprint_model": model,
                "buffer_size": 2048,
                "buffer_folder_name": kwargs["round_plan"]["buffer_folder_name"],
                "object_count": 1,
            }

        _FakeRoundExecutor.execute_round = staticmethod(_fake_execute_round)

        _FAKE_BPY.data.node_groups.get = (
            lambda name: types.SimpleNamespace(name=name) if name == "测试蓝图" else None
        )

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["buffer_sizes"], [2048, 2048])
        self.assertEqual(model.postprocess_paths, [output_dir])
        self.assertEqual(len(_EXPORT_ROUND_RECORDER.calls), 2)
        for _name, kwargs, _ in _EXPORT_ROUND_RECORDER.calls:
            self.assertFalse(kwargs["allow_parallel_preprocess"])
        with open(manifest["stage_path"], "r", encoding="utf-8") as file:
            stage = json.load(file)
        self.assertEqual(stage["stage"], "完成")
        self.assertEqual(stage["done"], 3)
        self.assertEqual(stage["total"], 3)
        self.assertTrue(os.path.isfile(manifest["log_path"]))
        self.assertTrue(FAKE_LOG.messages)

    def test_worker_writes_error_result_when_round_fails(self):
        session_dir = os.path.join(self.temp_dir, "worker_error")
        os.makedirs(session_dir)
        manifest = self._write_manifest(
            session_dir,
            [{"round_index": 1, "description": "单次导出", "buffer_folder_name": "Meshes", "export_index": 1}],
            os.path.join(self.temp_dir, "out_error"),
        )

        def _explode(**_kwargs):
            raise RuntimeError("前处理错误：物体引用未正确更新为副本")

        _FakeRoundExecutor.execute_round = staticmethod(_explode)

        _FAKE_BPY.data.node_groups.get = lambda _name: types.SimpleNamespace(name="测试蓝图")

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "error")
        self.assertIn("物体引用未正确更新", result["error"])
        with open(manifest["stage_path"], "r", encoding="utf-8") as file:
            stage = json.load(file)
        self.assertEqual(stage["stage"], "失败")
        self.assertTrue(os.path.isfile(manifest["log_path"]))

    def test_worker_reports_missing_tree(self):
        session_dir = os.path.join(self.temp_dir, "worker_no_tree")
        os.makedirs(session_dir)
        manifest = self._write_manifest(
            session_dir,
            [{"round_index": 1, "description": "单次导出", "buffer_folder_name": "Meshes", "export_index": 1}],
            os.path.join(self.temp_dir, "out_no_tree"),
        )
        _FAKE_BPY.data.node_groups.get = lambda _name: None

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "error")
        self.assertIn("未找到蓝图树", result["error"])

    def test_worker_direct_export_builds_fixed_plan_total(self):
        """直出模式：worker 用计划函数算固定清单，total 全程不变，收尾补满。"""
        session_dir = os.path.join(self.temp_dir, "worker_direct")
        os.makedirs(session_dir)
        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="测试蓝图",
            output_dir=os.path.join(self.temp_dir, "out_direct"),
            round_plans=[],
            logic_name="EFMI",
            direct_export=True,
            total_steps=12,  # 旧预算：worker 以计划清单为准，不再用它
        )
        tree = types.SimpleNamespace(name="测试蓝图")
        _FAKE_BPY.data.node_groups.get = (
            lambda name: tree if name == "测试蓝图" else None
        )

        # worker 会为这棵树算出的计划清单（同一函数）
        expected_plan = export_step_plan.build_step_plan(
            tree,
            direct_export=True,
            has_shapekey_export=False,
            max_shapekey_slot=0,
            has_multi_file_export=False,
            round_plans=[],
            postprocess_nodes=[],
        )
        self.assertTrue(expected_plan)

        direct_module = sys.modules[f"{PKG}.blueprint.direct_export"]
        original_direct = direct_module.execute_direct_export
        observed = []

        def _fake_direct(**_kwargs):
            timer_utils = sys.modules[f"{PKG}.utils.timer_utils"].TimerUtils
            timer_utils.start_stage("Direct-CollectNodes")
            observed.append(export_progress.step_done())
            timer_utils.start_stage("Direct-SetupState")
            observed.append(export_progress.step_done())
            export_progress.report_node("形态键节点", "done")
            observed.append(export_progress.step_done())
            observed.append(export_progress.step_total())

        direct_module.execute_direct_export = _fake_direct
        self.addCleanup(setattr, direct_module, "execute_direct_export", original_direct)

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["rounds"], 1)
        # 测试桩 TimerUtils 只把 Direct-Preprocess 翻成中文，这两个英文阶段名都在
        # 计划清单之外 → 只更新文本、不计步；total 始终是清单长度
        self.assertEqual(observed[:3], [0, 0, 0])
        self.assertEqual(observed[3], len(expected_plan))

        with open(manifest["stage_path"], "r", encoding="utf-8") as file:
            stage = json.load(file)
        self.assertEqual(stage["stage"], "完成")
        self.assertEqual((stage["done"], stage["total"]), (len(expected_plan), len(expected_plan)))
        self.assertEqual(stage["plan_total"], len(expected_plan))
        self.assertEqual(stage["plan"], expected_plan)

    def test_worker_quick_export_rebuilds_tree_and_runs_single_round(self):
        """quick_export：按 object_names 取 MESH → 临时树 → 单轮导出 + 后处理。"""
        session_dir = os.path.join(self.temp_dir, "worker_quick")
        os.makedirs(session_dir)
        output_dir = os.path.join(self.temp_dir, "out_quick")
        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="",
            output_dir=output_dir,
            round_plans=[dict(export_background.QUICK_EXPORT_ROUND_PLAN)],
            logic_name="EFMI",
            quick_export=True,
            object_names=["M1", "M2"],
            total_steps=2,
        )

        meshes = {
            "M1": types.SimpleNamespace(name="M1", type="MESH", as_pointer=lambda: 1),
            "M2": types.SimpleNamespace(name="M2", type="MESH", as_pointer=lambda: 2),
        }
        _FAKE_BPY.data.objects.get = lambda name: meshes.get(name)

        fake_tree = types.SimpleNamespace(name="SSMT_QuickExport_1")
        built = []
        original_build = export_background.build_quick_export_temp_tree

        def _fake_build(objects):
            built.append(list(objects))
            return fake_tree

        export_background.build_quick_export_temp_tree = _fake_build
        self.addCleanup(
            setattr, export_background, "build_quick_export_temp_tree", original_build
        )

        model = _FakeBlueprintModel()
        observed_round_plans = []

        def _fake_execute_round(**kwargs):
            observed_round_plans.append(kwargs["round_plan"])
            self.assertFalse(kwargs["allow_parallel_preprocess"])
            return {
                "blueprint_model": model,
                "buffer_size": 128,
                "buffer_folder_name": kwargs["round_plan"]["buffer_folder_name"],
                "object_count": 2,
            }

        _FakeRoundExecutor.execute_round = staticmethod(_fake_execute_round)

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(built, [[meshes["M1"], meshes["M2"]]])
        self.assertEqual(len(observed_round_plans), 1)
        self.assertEqual(observed_round_plans[0]["description"], "快速局部导出")
        self.assertEqual(model.postprocess_paths, [output_dir])
        self.assertEqual(
            export_background.get_active_session(), None, "worker 不应登记主进程活动会话"
        )

        with open(manifest["stage_path"], "r", encoding="utf-8") as file:
            stage = json.load(file)
        self.assertEqual(stage["stage"], "完成")
        self.assertEqual((stage["done"], stage["total"]), (2, 2))

    def test_worker_quick_export_freezes_per_modifier_visibility(self):
        """关闭的修改器从后台对象移除，打开的修改器保留并继续应用。"""
        session_dir = os.path.join(self.temp_dir, "worker_quick_modifier_states")
        os.makedirs(session_dir)
        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="",
            output_dir=os.path.join(self.temp_dir, "out_quick_modifier_states"),
            round_plans=[dict(export_background.QUICK_EXPORT_ROUND_PLAN)],
            logic_name="EFMI",
            quick_export=True,
            quick_export_modifier_states={
                "M1": {
                    "关闭的修改器": False,
                    "打开的修改器": True,
                }
            },
            object_names=["M1"],
            total_steps=2,
        )

        class _ModifierCollection(list):
            def remove(self, modifier):
                super().remove(modifier)

        mesh = types.SimpleNamespace(
            name="M1",
            type="MESH",
            as_pointer=lambda: 1,
            modifiers=_ModifierCollection(
                [
                    types.SimpleNamespace(name="关闭的修改器", show_viewport=True),
                    types.SimpleNamespace(name="打开的修改器", show_viewport=True),
                ]
            ),
        )
        _FAKE_BPY.data.objects.get = lambda name: mesh if name == "M1" else None

        quick_tree = types.SimpleNamespace(
            name="SSMT_QuickExport_1",
            disable_copy_modifiers_on_export=False,
        )
        original_build = export_background.build_quick_export_temp_tree
        export_background.build_quick_export_temp_tree = (
            lambda _objects: quick_tree
        )
        self.addCleanup(
            setattr,
            export_background,
            "build_quick_export_temp_tree",
            original_build,
        )

        export_background._run_worker(manifest["manifest_path"])

        self.assertEqual(
            [modifier.name for modifier in mesh.modifiers],
            ["打开的修改器"],
        )

    def test_worker_quick_export_errors_when_no_selected_mesh(self):
        session_dir = os.path.join(self.temp_dir, "worker_quick_empty")
        os.makedirs(session_dir)
        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="",
            output_dir=os.path.join(self.temp_dir, "out_quick_empty"),
            round_plans=[],
            quick_export=True,
            object_names=["缺失物体"],
            total_steps=2,
        )
        _FAKE_BPY.data.objects.get = lambda _name: None

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "error")
        self.assertIn("未找到选中的网格物体", result["error"])

    def test_worker_skips_config_table_backup_and_records_ini_warnings(self):
        """普通模式：旧配置表备份已移到主进程启动前，worker 不再碰它，只记录 INI 告警。"""
        session_dir = os.path.join(self.temp_dir, "worker_backup")
        os.makedirs(session_dir)
        output_dir = os.path.join(self.temp_dir, "out_backup")
        os.makedirs(output_dir)
        manifest = self._write_manifest(
            session_dir,
            [{"round_index": 1, "description": "单次导出", "buffer_folder_name": "Meshes", "export_index": 1}],
            output_dir,
        )

        config_module = sys.modules[f"{PKG}.common.config_table_backup"]
        calls = []
        original_find = config_module.find_config_table_files
        original_backup = config_module.backup_config_tables
        config_module.find_config_table_files = (
            lambda path: calls.append(("find", path))
            or [os.path.join(path, "角色.ini")]
        )
        config_module.backup_config_tables = (
            lambda path, paths: calls.append(("backup", path, list(paths)))
            or [("/old", "/new")]
        )
        self.addCleanup(
            setattr, config_module, "find_config_table_files", original_find
        )
        self.addCleanup(
            setattr, config_module, "backup_config_tables", original_backup
        )

        original_warnings = export_background._collect_ini_variable_warnings
        export_background._collect_ini_variable_warnings = (
            lambda path: ["缺失变量: $x"] if path == output_dir else []
        )
        self.addCleanup(
            setattr,
            export_background,
            "_collect_ini_variable_warnings",
            original_warnings,
        )

        _FAKE_BPY.data.node_groups.get = lambda name: types.SimpleNamespace(name=name)

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["warnings"], ["缺失变量: $x"])
        # 备份职责已上移主进程（与同步导出一致）：worker 连查找都不该做，
        # 否则会二次搬动主进程刚备份好的旧配置表。
        self.assertEqual(calls, [])
        self.assertFalse(hasattr(export_background, "_backup_config_tables_in_worker"))

    def test_worker_direct_export_skips_config_backup_and_ini_check(self):
        """直出模式与同步直出一致：不搬配置表备份、不做 INI 自检。"""
        session_dir = os.path.join(self.temp_dir, "worker_direct_skip")
        os.makedirs(session_dir)
        output_dir = os.path.join(self.temp_dir, "out_direct_skip")
        manifest = export_background.build_manifest(
            session_dir=session_dir,
            snapshot_path=os.path.join(session_dir, "snapshot.blend"),
            tree_name="测试蓝图",
            output_dir=output_dir,
            round_plans=[],
            logic_name="EFMI",
            direct_export=True,
            total_steps=1,
        )
        _FAKE_BPY.data.node_groups.get = lambda name: types.SimpleNamespace(name=name)

        config_module = sys.modules[f"{PKG}.common.config_table_backup"]
        original_find = config_module.find_config_table_files
        original_backup = config_module.backup_config_tables
        calls = []
        config_module.find_config_table_files = lambda path: calls.append(("find", path)) or []
        config_module.backup_config_tables = lambda *_args, **_kwargs: calls.append(("backup",)) or []
        self.addCleanup(
            setattr, config_module, "find_config_table_files", original_find
        )
        self.addCleanup(
            setattr, config_module, "backup_config_tables", original_backup
        )

        original_warnings = export_background._collect_ini_variable_warnings
        export_background._collect_ini_variable_warnings = (
            lambda _path: calls.append(("ini",)) or []
        )
        self.addCleanup(
            setattr,
            export_background,
            "_collect_ini_variable_warnings",
            original_warnings,
        )

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "success")
        self.assertNotIn("warnings", result)
        self.assertEqual(calls, [])

    def test_apply_manifest_environment_overrides_output_dir(self):
        output_dir = os.path.join(self.temp_dir, "override_out")
        export_background._apply_manifest_environment(
            {
                "logic_name": "ZZMI",
                "workspace_name": "覆盖角色",
                "output_dir": output_dir,
            }
        )

        self.assertEqual(_FakeGlobalConfig.logic_name, "ZZMI")
        self.assertEqual(_FakeGlobalConfig.workspacename, "覆盖角色")
        self.assertEqual(_FakeGlobalConfig.path_generate_mod_folder(), output_dir)
        self.assertTrue(os.path.isdir(output_dir))

    def test_apply_manifest_environment_overrides_preprocess_cache_dir(self):
        """worker 的 bpy.data.filepath 指向临时快照，缓存目录必须按清单覆盖回主进程的值。"""
        cache_dir = os.path.join(self.temp_dir, ".ssmt_preprocess_cache")
        applied = []
        cache_module = _install_module(
            f"{PKG}.blueprint.preprocess_cache",
            PreProcessCache=types.SimpleNamespace(
                set_override_cache_dir=applied.append,
            ),
        )
        self.addCleanup(sys.modules.pop, cache_module.__name__, None)

        export_background._apply_manifest_environment(
            {
                "logic_name": "EFMI",
                "workspace_name": "覆盖角色",
                "output_dir": os.path.join(self.temp_dir, "override_out_cache"),
                "preprocess_cache_dir": cache_dir,
            }
        )

        self.assertEqual(applied, [cache_dir])

    def test_apply_manifest_environment_keeps_cache_dir_when_absent(self):
        applied = []
        cache_module = _install_module(
            f"{PKG}.blueprint.preprocess_cache",
            PreProcessCache=types.SimpleNamespace(
                set_override_cache_dir=applied.append,
            ),
        )
        self.addCleanup(sys.modules.pop, cache_module.__name__, None)

        export_background._apply_manifest_environment(
            {
                "logic_name": "EFMI",
                "workspace_name": "覆盖角色",
                "output_dir": os.path.join(self.temp_dir, "override_out_no_cache"),
            }
        )

        self.assertEqual(applied, [])


# ---------------------------------------------------------------------------
# 5/6. 开关默认值与算子分流
# ---------------------------------------------------------------------------


class BackgroundToggleTests(_BackgroundTestBase):
    def test_background_mode_has_no_setting_or_sync_export_method(self):
        self.assertNotIn("background_export_enabled", global_properties_module.GlobalProterties.__annotations__)
        self.assertFalse(hasattr(global_properties_module.GlobalProterties, "is_background_export_enabled"))
        self.assertFalse(hasattr(ui_func_export.SSMTGenerateModBlueprint, "_execute_export"))


class _RecordingSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.status = "running"
        self.error = ""
        self.snapshot_path = "C:/tmp/snapshot.blend"
        self.log_path = "C:/tmp/worker.log"
        self.output_dir = kwargs.get("output_dir", "")
        self.started = False
        self.cancelled = False
        self.cleaned = False
        self.poll_results = ["running"]
        # 收尾文本「后台导出完成（done/total）」用的步数（真实会话由 stage.json 提供）
        self.done_steps = 0
        self.total_steps = 2
        # 收尾「用时 X.X 秒」用的计时（真实会话由 start() 记 monotonic）
        self.elapsed_seconds_value = 38.6
        self.header_before_start = ""
        _RecordingSession.instances.append(self)

    def elapsed_seconds(self):
        return self.elapsed_seconds_value

    def elapsed_text(self):
        return f"用时 {self.elapsed_seconds_value:.1f} 秒"

    def start(self):
        self.started = True
        # 记录 start() 执行期间状态栏是什么文本：保存 480MB 快照（数秒）就发生在这里，
        # 之前没有文本正是"点了导出十几秒没反应"的根因。
        self.header_before_start = _FAKE_AREAS[0].header if _FAKE_AREAS else ""
        return True

    def poll(self):
        if len(self.poll_results) > 1:
            return self.poll_results.pop(0)
        return self.poll_results[0]

    def progress_text(self):
        return "后台导出：准备 0/2"

    def success_summary(self):
        return "后台导出完成，共 2 轮，用时 38.6 秒"

    def error_summary(self):
        return "后台导出失败（日志: C:/tmp/worker.log）"

    def cancel(self):
        self.cancelled = True
        return True

    def cleanup(self):
        self.cleaned = True
        return self.log_path


class _FakeExportTree:
    name = "测试蓝图"
    nodes = []


class OperatorInvokeTests(_BackgroundTestBase):
    def test_execute_default_starts_background_session(self):
        context = self._make_context()
        operator = self._make_operator(context)
        self.assertEqual(operator.execute(context), {'RUNNING_MODAL'})
        self.assertTrue(_RecordingSession.instances[0].started)
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [operator])

    def test_quick_execute_default_starts_background_session(self):
        context = self._make_context([self._make_mesh("M1")])
        operator = self._make_quick_operator()
        self.assertEqual(operator.execute(context), {'RUNNING_MODAL'})
        self.assertTrue(_RecordingSession.instances[0].kwargs["quick_export"])
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [operator])

    def test_execute_default_preserves_busy_guard(self):
        context = self._make_context()
        operator = self._make_operator(context)
        export_background.set_active_session(object())
        self.assertEqual(operator.execute(context), {'CANCELLED'})
        self.assertFalse(_RecordingSession.instances)

    def test_ntmi_session_keeps_selected_node_and_custom_directory(self):
        _FakeGlobalConfig.logic_name = "NTEMI"
        node = types.SimpleNamespace(name="自定义输出", bl_idname="SSMTNode_Result_Output_NTMIModImp")
        tree = types.SimpleNamespace(name="测试蓝图", nodes={node.name: node})
        operator = self._make_operator(self._make_context())
        operator.ntmi_output_node_name = node.name
        output_dir = os.path.join(self.temp_dir, "custom_ntmi_output")
        engine = types.ModuleType(f"{PKG}.blueprint.ntmi_export_modimp")
        engine.resolve_ntmi_modimp_output_dir = mock.Mock(return_value=output_dir)
        with mock.patch.dict(sys.modules, {engine.__name__: engine}):
            session = operator._create_background_session(tree)
        self.assertEqual(session.kwargs["output_dir"], output_dir)
        self.assertEqual(session.kwargs["ntmi_output_node_name"], node.name)
        self.assertEqual(session.kwargs["round_plans"], [])
        self.assertEqual(session.kwargs["total_steps"], len(export_background.NTMI_MODIMP_STAGE_STEPS))

    def setUp(self):
        super().setUp()
        _RecordingSession.instances = []
        self._original_session_cls = ui_func_export.BackgroundExportSession
        ui_func_export.BackgroundExportSession = _RecordingSession
        self.addCleanup(
            setattr, ui_func_export, "BackgroundExportSession", self._original_session_cls
        )

        self._original_has_direct = ui_func_export.has_direct_export_mode
        ui_func_export.has_direct_export_mode = lambda _tree: False
        self.addCleanup(setattr, ui_func_export, "has_direct_export_mode", self._original_has_direct)

        self._original_get_current = ui_func_export.BlueprintExportHelper.get_current_blueprint_tree
        self._original_get_selected = ui_func_export.BlueprintExportHelper.get_selected_blueprint_tree
        ui_func_export.BlueprintExportHelper.get_selected_blueprint_tree = (
            lambda **_kwargs: _FakeExportTree()
        )
        ui_func_export.BlueprintExportHelper.get_current_blueprint_tree = (
            lambda **_kwargs: _FakeExportTree()
        )
        self.addCleanup(
            setattr,
            ui_func_export.BlueprintExportHelper,
            "get_current_blueprint_tree",
            self._original_get_current,
        )
        self.addCleanup(
            setattr,
            ui_func_export.BlueprintExportHelper,
            "get_selected_blueprint_tree",
            self._original_get_selected,
        )

        _FakeGlobalConfig.logic_name = "EFMI"
        _FakeGlobalConfig.workspacename = "测试角色"
        _FakeGlobalConfig.output_dir = os.path.join(self.temp_dir, "out_operator")

    def _make_operator(self, context):
        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator.blueprint_name = ""
        operator.reports = []

        def _report(level, message):
            operator.reports.append((set(level), message))

        operator.report = _report
        return operator

    def _make_quick_operator(self):
        operator = ui_func_export.SSMTQuickExportSelected()
        operator.reports = []

        def _report(level, message):
            operator.reports.append((set(level), message))

        operator.report = _report
        return operator

    def _make_context(self, selected_objects=None):
        return types.SimpleNamespace(
            window_manager=_FAKE_WINDOW_MANAGER,
            window=object(),
            scene=types.SimpleNamespace(global_properties=self.scene_properties),
            selected_objects=list(selected_objects or []),
        )

    def _make_mesh(self, name, pointer=1, modifiers=None):
        return types.SimpleNamespace(
            type="MESH",
            name=name,
            as_pointer=lambda: pointer,
            modifiers=list(modifiers or []),
        )

    def _start_background_operator(self):
        """起一个真正走到 modal 的后台导出操作符（Generate 入口）。"""
        context = self._make_context()
        operator = self._make_operator(context)
        operator.execute = lambda _context: (_ for _ in ()).throw(
            AssertionError("后台导出不应走同步 execute")
        )
        self.assertEqual(operator.invoke(context, None), {'RUNNING_MODAL'})
        return context, operator

    def _patch_old_config_tables(self, ini_paths, backed_up):
        """让输出目录"看起来有旧配置表"，并记录备份调用（真实备份逻辑另有测试）。"""
        original_find = ui_func_export.find_config_table_files
        original_backup = ui_func_export.backup_config_tables
        original_has_postprocess = ui_func_export.SSMTGenerateModBlueprint._has_postprocess_nodes
        ui_func_export.find_config_table_files = lambda _path: list(ini_paths)
        ui_func_export.backup_config_tables = (
            lambda path, paths: backed_up.append((path, list(paths)))
            or [("/old.ini", "/new.ini")]
        )
        ui_func_export.SSMTGenerateModBlueprint._has_postprocess_nodes = (
            lambda self, _tree: True
        )
        self.addCleanup(setattr, ui_func_export, "find_config_table_files", original_find)
        self.addCleanup(setattr, ui_func_export, "backup_config_tables", original_backup)
        self.addCleanup(
            setattr,
            ui_func_export.SSMTGenerateModBlueprint,
            "_has_postprocess_nodes",
            original_has_postprocess,
        )

    def test_background_invoke_asks_before_overwriting_old_config_tables(self):
        """后台导出与同步导出一样先问「覆盖 / 保留」：确认前不备份、不起会话。"""
        context = self._make_context()
        operator = self._make_operator(context)
        output_dir = _FakeGlobalConfig.output_dir
        os.makedirs(output_dir, exist_ok=True)
        ini_path = os.path.join(output_dir, "角色.ini")
        backed_up = []
        self._patch_old_config_tables([ini_path], backed_up)

        result = operator.invoke(context, None)

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(len(_FAKE_WINDOW_MANAGER.dialogs), 1)
        self.assertEqual(_FAKE_WINDOW_MANAGER.dialogs[0][1], 460)
        self.assertEqual(backed_up, [])
        self.assertEqual(_RecordingSession.instances, [])

    def test_background_confirm_backs_up_then_starts_session(self):
        """弹窗点确定（execute）后先备份旧配置表，再起后台会话。"""
        context = self._make_context()
        operator = self._make_operator(context)
        output_dir = _FakeGlobalConfig.output_dir
        os.makedirs(output_dir, exist_ok=True)
        ini_path = os.path.join(output_dir, "角色.ini")
        backed_up = []
        self._patch_old_config_tables([ini_path], backed_up)
        operator.invoke(context, None)
        operator.overwrite_config = "YES"

        result = operator.execute(context)

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(backed_up, [(output_dir, [ini_path])])
        self.assertEqual(len(_RecordingSession.instances), 1)
        self.assertFalse(operator._restore_config_tables_after_export)

    def test_background_finish_restores_old_config_tables_when_user_keeps_them(self):
        """选了「否」：后台导出收尾把备份的旧配置表覆盖回原位，且只做一次。"""
        context = self._make_context()
        operator = self._make_operator(context)
        output_dir = _FakeGlobalConfig.output_dir
        os.makedirs(output_dir, exist_ok=True)
        ini_path = os.path.join(output_dir, "角色.ini")
        backed_up = []
        self._patch_old_config_tables([ini_path], backed_up)
        operator.invoke(context, None)
        operator.overwrite_config = "NO"
        operator.execute(context)
        self.assertTrue(operator._restore_config_tables_after_export)

        restored = []
        original_restore = ui_func_export.restore_config_tables
        ui_func_export.restore_config_tables = lambda entries: restored.append(list(entries))
        self.addCleanup(setattr, ui_func_export, "restore_config_tables", original_restore)
        session = _RecordingSession.instances[0]
        session.poll_results = ["success"]

        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='TIMER')), {'FINISHED'}
        )

        self.assertEqual(restored, [[("/old.ini", "/new.ini")]])
        self.assertFalse(operator._restore_config_tables_after_export)
        self.addCleanup(setattr, ui_func_export, "restore_config_tables", original_restore)

    def test_legacy_disabled_setting_cannot_enable_foreground_export(self):
        context = self._make_context()
        operator = self._make_operator(context)
        self.assertEqual(operator.invoke(context, None), {'RUNNING_MODAL'})
        self.assertTrue(_RecordingSession.instances[0].started)
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [operator])

    def test_quick_export_ignores_legacy_disabled_setting(self):
        context = self._make_context([self._make_mesh("M1")])
        operator = self._make_quick_operator()
        self.assertEqual(operator.invoke(context, None), {'RUNNING_MODAL'})
        self.assertTrue(_RecordingSession.instances[0].started)
        self.assertTrue(_RecordingSession.instances[0].kwargs["quick_export"])

    def test_quick_invoke_with_toggle_on_starts_background_session(self):
        context = self._make_context([self._make_mesh("M1")])
        operator = self._make_quick_operator()
        operator.execute = lambda _context: (_ for _ in ()).throw(
            AssertionError("后台导出不应走同步 execute")
        )

        result = operator.invoke(context, None)

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(len(_RecordingSession.instances), 1)
        session = _RecordingSession.instances[0]
        self.assertTrue(session.started)
        self.assertFalse(operator._bg_view_nav)
        self.assertTrue(session.kwargs["quick_export"])
        self.assertEqual(session.kwargs["object_names"], ["M1"])
        # 快速导出步骤口径：轮次 1 + 后处理节点 0 + 收尾 1
        self.assertEqual(session.kwargs["total_steps"], 2)
        self.assertEqual(session.kwargs["round_plans"][0]["buffer_folder_name"], "Meshes")
        self.assertEqual(_FAKE_WINDOW_MANAGER.timers[0]["interval"], 0.2)
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [operator])

    def test_quick_session_captures_per_modifier_visibility(self):
        mesh = self._make_mesh(
            "M1",
            modifiers=[
                types.SimpleNamespace(name="关闭的修改器", show_viewport=False),
                types.SimpleNamespace(name="打开的修改器", show_viewport=True),
            ],
        )
        context = self._make_context([mesh])
        operator = self._make_quick_operator()

        operator.invoke(context, None)

        self.assertEqual(
            _RecordingSession.instances[0].kwargs[
                "quick_export_modifier_states"
            ],
            {
                "M1": {
                    "关闭的修改器": False,
                    "打开的修改器": True,
                }
            },
        )

    def test_quick_modal_uses_same_navigation_dispatch(self):
        """快速局部导出与 Generate 共用同一套事件分派：普通移动吞掉、中键拖动放行。"""
        context = self._make_context([self._make_mesh("M1")])
        operator = self._make_quick_operator()
        operator.execute = lambda _context: (_ for _ in ()).throw(
            AssertionError("后台导出不应走同步 execute")
        )

        self.assertEqual(operator.invoke(context, None), {'RUNNING_MODAL'})
        self.assertFalse(operator._bg_view_nav)
        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='MOUSEMOVE', value='NOTHING')),
            {'RUNNING_MODAL'},
        )
        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='MIDDLEMOUSE', value='PRESS')),
            {'PASS_THROUGH'},
        )
        self.assertTrue(operator._bg_view_nav)
        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='MIDDLEMOUSE', value='RELEASE')),
            {'PASS_THROUGH'},
        )
        self.assertFalse(operator._bg_view_nav)

    def test_invoke_is_blocked_while_background_session_active(self):
        """防重入：已有活动会话时两个导出入口都返回 CANCELLED，且不新建会话。"""
        export_background.set_active_session(object())
        context = self._make_context([self._make_mesh("M1")])

        operator = self._make_operator(context)
        result = operator.invoke(context, None)

        self.assertEqual(result, {'CANCELLED'})
        self.assertEqual(operator.reports[-1][0], {'WARNING'})
        self.assertIn("已有后台导出在运行", operator.reports[-1][1])

        quick_operator = self._make_quick_operator()
        self.assertEqual(quick_operator.invoke(context, None), {'CANCELLED'})
        self.assertEqual(quick_operator.reports[-1][0], {'WARNING'})
        self.assertEqual(_RecordingSession.instances, [])
        self.assertEqual(_FAKE_WINDOW_MANAGER.timers, [])

    def test_success_summary_includes_worker_warnings(self):
        session = self.make_session()
        session.result = {
            "status": "success",
            "rounds": 2,
            "warnings": ["缺失变量: $x", "重复声明: $y"],
        }

        summary = session.success_summary()

        self.assertIn("共 2 轮", summary)
        self.assertIn("自检警告", summary)
        self.assertIn("缺失变量: $x", summary)

    def test_invoke_with_toggle_on_starts_session_and_runs_modal(self):
        context = self._make_context()
        operator = self._make_operator(context)
        operator.execute = lambda _context: (_ for _ in ()).throw(
            AssertionError("后台导出不应走同步 execute")
        )

        result = operator.invoke(context, None)

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(len(_RecordingSession.instances), 1)
        session = _RecordingSession.instances[0]
        self.assertTrue(session.started)
        self.assertFalse(operator._bg_view_nav)
        self.assertEqual(session.kwargs["tree_name"], "测试蓝图")
        self.assertEqual(session.kwargs["output_dir"], _FakeGlobalConfig.output_dir)
        self.assertEqual(session.kwargs["round_plans"][0]["buffer_folder_name"], "Meshes")
        self.assertEqual(len(_FAKE_WINDOW_MANAGER.timers), 1)
        self.assertEqual(_FAKE_WINDOW_MANAGER.timers[0]["interval"], 0.2)
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [operator])
        # 点导出立即有反馈（快照保存期间），启动完成后换成「加载工程」等待文本
        self.assertEqual(session.header_before_start, "后台导出：正在保存工程快照…")
        self.assertEqual(
            _FAKE_AREAS[0].header, "后台导出：后台实例正在加载工程（约 10 秒）…"
        )

        # 轮询：成功 → FINISHED，header 保留「完成」文本、停 timer、cleanup
        session.poll_results = ["running", "success"]
        self.assertEqual(operator.modal(context, types.SimpleNamespace(type='TIMER')), {'RUNNING_MODAL'})
        self.assertEqual(operator.modal(context, types.SimpleNamespace(type='TIMER')), {'FINISHED'})
        self.assertEqual(_FAKE_AREAS[0].header, "后台导出完成（0/2，用时 38.6 秒）")
        self.assertEqual(len(_FAKE_WINDOW_MANAGER.removed_timers), 1)
        self.assertTrue(session.cleaned)
        self.assertEqual(operator.reports[-1][0], {'INFO'})

    def test_success_report_includes_elapsed(self):
        """成功收尾的 INFO 摘要带用时，且不会重复添加。"""
        context, operator = self._start_background_operator()
        session = _RecordingSession.instances[0]
        session.poll_results = ["success"]

        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='TIMER')), {'FINISHED'}
        )

        message = operator.reports[-1][1]
        self.assertEqual(operator.reports[-1][0], {'INFO'})
        self.assertIn("用时 38.6 秒", message)
        self.assertEqual(message.count("用时"), 1)

    def test_modal_failure_updates_header_without_error_report(self):
        """失败收尾不再弹 Blender 原生 Report：header 留摘要，report 不出现 ERROR。"""
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.poll_results = ["error"]

        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(session.cleaned)
        self.assertFalse(any(level & {'ERROR'} for level, _message in operator.reports))
        self.assertIn("后台导出失败", _FAKE_AREAS[0].header)
        self.assertIn("worker.log", _FAKE_AREAS[0].header)

    def test_modal_escape_cancels_session(self):
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]

        result = operator.modal(context, types.SimpleNamespace(type='ESC'))

        self.assertEqual(result, {'CANCELLED'})
        self.assertTrue(session.cancelled)
        self.assertTrue(session.cleaned)
        self.assertEqual(_FAKE_AREAS[0].header, "后台导出已取消")
        self.assertFalse(
            any(level & {'WARNING', 'ERROR'} for level, _message in operator.reports)
        )

    def test_modal_mouse_move_without_middle_button_is_swallowed(self):
        """未按中键时 MOUSEMOVE 被吞掉：放行会让 UI 层重设光标（进度光标被覆盖的根因）。"""
        context, operator = self._start_background_operator()
        session = _RecordingSession.instances[0]
        polls = []
        session.poll = lambda: polls.append(1) or "running"
        header_after_start = _FAKE_AREAS[0].header

        for event_type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    operator.modal(
                        context, types.SimpleNamespace(type=event_type, value='NOTHING')
                    ),
                    {'RUNNING_MODAL'},
                )

        # 非 TIMER 事件不轮询 stage.json、也不写状态栏（进度仍按 0.2s TIMER 刷新）
        self.assertEqual(polls, [])
        self.assertEqual(_FAKE_AREAS[0].header, header_after_start)

    def test_modal_other_events_are_swallowed(self):
        """键盘 / 左右键等非导航事件一律 RUNNING_MODAL，免得界面重设光标。"""
        context, operator = self._start_background_operator()

        for event_type in ('LEFTMOUSE', 'RIGHTMOUSE', 'RET', 'A', 'SPACE'):
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    operator.modal(
                        context, types.SimpleNamespace(type=event_type, value='PRESS')
                    ),
                    {'RUNNING_MODAL'},
                )

    def test_modal_middle_mouse_drag_passes_through_and_tracks_state(self):
        """中键按下 → 拖动中的移动放行 → 松开清状态；松开后移动重新被吞掉。"""
        context, operator = self._start_background_operator()
        self.assertFalse(operator._bg_view_nav)

        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='MIDDLEMOUSE', value='PRESS')),
            {'PASS_THROUGH'},
        )
        self.assertTrue(operator._bg_view_nav)

        for event_type in ('MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'):
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    operator.modal(
                        context, types.SimpleNamespace(type=event_type, value='NOTHING')
                    ),
                    {'PASS_THROUGH'},
                )

        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='MIDDLEMOUSE', value='RELEASE')),
            {'PASS_THROUGH'},
        )
        self.assertFalse(operator._bg_view_nav)

        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='MOUSEMOVE', value='NOTHING')),
            {'RUNNING_MODAL'},
        )

    def test_modal_wheel_and_trackpad_events_pass_through(self):
        """滚轮 / 触控板 / 旋转事件直接放行，且不改变拖动状态。"""
        context, operator = self._start_background_operator()

        for event_type in (
            'WHEELUPMOUSE',
            'WHEELDOWNMOUSE',
            'TRACKPADPAN',
            'TRACKPADZOOM',
            'MOUSEROTATE',
            'MOUSEPAN',
        ):
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    operator.modal(
                        context, types.SimpleNamespace(type=event_type, value='PRESS')
                    ),
                    {'PASS_THROUGH'},
                )

        self.assertFalse(operator._bg_view_nav)

    def test_modal_timer_still_polls_after_navigation_events(self):
        """导航事件放行后，TIMER 仍走轮询刷新状态栏并返回 RUNNING_MODAL。"""
        context, operator = self._start_background_operator()
        session = _RecordingSession.instances[0]
        session.total_steps = 2
        polls = []
        original_poll = session.poll
        session.poll = lambda: polls.append(1) or original_poll()

        operator.modal(context, types.SimpleNamespace(type='WHEELUPMOUSE', value='PRESS'))
        operator.modal(context, types.SimpleNamespace(type='MOUSEMOVE', value='NOTHING'))

        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(polls, [1])
        # 进度条非空时插在「后台导出：」之后、步骤文本之前
        self.assertEqual(
            _FAKE_AREAS[0].header,
            "后台导出：▏────────────────────▕ 0% 准备 0/2",
        )

    def test_modal_escape_clears_view_nav_state(self):
        """收尾（含 ESC 取消）要清掉中键拖动状态，避免下一轮误放行移动事件。"""
        context, operator = self._start_background_operator()
        operator.modal(context, types.SimpleNamespace(type='MIDDLEMOUSE', value='PRESS'))
        self.assertTrue(operator._bg_view_nav)

        self.assertEqual(
            operator.modal(context, types.SimpleNamespace(type='ESC')), {'CANCELLED'}
        )

        self.assertFalse(operator._bg_view_nav)
        self.assertTrue(_RecordingSession.instances[0].cancelled)
        self.assertTrue(_RecordingSession.instances[0].cleaned)
        self.assertEqual(_FAKE_AREAS[0].header, "后台导出已取消")

    def test_finish_restores_node_colors_for_all_outcomes(self):
        """success / cancel / error 三个收尾分支都要恢复节点原色并清空状态。"""
        context = self._make_context()

        for outcome in ("success", "cancel", "error"):
            with self.subTest(outcome=outcome):
                node = _FakeProgressNode("节点A", color=(0.1, 0.2, 0.3))
                node.use_custom_color = True
                node.color = (0.95, 0.72, 0.1)

                operator = self._make_operator(context)
                operator._bg_node_colors = {
                    "节点A": {
                        "node": node,
                        "use_custom_color": False,
                        "color": (0.1, 0.2, 0.3),
                    }
                }
                session = _RecordingSession()

                operator._finish_background_export(context, session, outcome)

                self.assertFalse(node.use_custom_color)
                self.assertEqual(tuple(node.color), (0.1, 0.2, 0.3))
                self.assertIsNone(operator._bg_node_colors)

    # -- 总步骤预计算 -------------------------------------------------------

    def test_background_session_total_steps_common_mode(self):
        """普通模式：轮次数 + 后处理节点数 + 1(收尾)，与计划函数口径一致。"""
        nodes = [
            types.SimpleNamespace(name="节点A"),
            types.SimpleNamespace(name="节点B"),
        ]
        _FakeBlueprintExportHelper.postprocess_nodes = nodes
        operator = self._make_operator(None)

        session = operator._create_background_session(_FakeExportTree())

        expected = export_step_plan.build_step_plan(
            _FakeExportTree(),
            direct_export=False,
            round_plans=session.kwargs["round_plans"],
            postprocess_nodes=nodes,
        )
        self.assertFalse(session.kwargs["direct_export"])
        self.assertEqual(session.kwargs["total_steps"], len(expected))
        self.assertEqual(len(expected), 1 + 2 + 1)

    def test_background_session_total_steps_direct_mode(self):
        """直出模式：主进程用与 worker 同一个计划函数算 total。"""
        nodes = [
            types.SimpleNamespace(
                name="形态键配置",
                bl_idname="SSMTNode_PostProcess_ShapeKey",
                direct_export_mode=True,
            ),
            types.SimpleNamespace(
                name="多文件配置",
                bl_idname="SSMTNode_PostProcess_MultiFile",
                direct_export_mode=True,
            ),
        ]
        _FakeBlueprintExportHelper.postprocess_nodes = nodes
        ui_func_export.has_direct_export_mode = lambda _tree: True
        operator = self._make_operator(None)

        session = operator._create_background_session(_FakeExportTree())

        expected = export_step_plan.build_step_plan(
            _FakeExportTree(),
            direct_export=True,
            has_shapekey_export=False,
            max_shapekey_slot=0,
            has_multi_file_export=False,
            round_plans=[],
            postprocess_nodes=nodes,
        )
        self.assertTrue(session.kwargs["direct_export"])
        self.assertEqual(session.kwargs["total_steps"], len(expected))
        # 直出形态键生成 / 多文件生成各 1 步；节点直出，不需要传统附加轮次
        self.assertIn("直出-形态键生成", expected)
        self.assertIn("直出-生成多文件资源", expected)
        self.assertNotIn("直出-形态键附加轮次", expected)
        # 7 顶层 + 8 前处理 + 2 直出形态键前处理 + 8 基础导出 + 2 节点 + 2 生成 + 收尾
        self.assertEqual(len(expected), 7 + 8 + 2 + 8 + 2 + 2 + 1)

    def test_estimate_total_steps_includes_standard_shapekey_round_stage(self):
        """有未启用直出的形态键后处理节点时，直出还要补跑传统附加轮次（多 1 步）。"""
        nodes = [
            types.SimpleNamespace(
                name="形态键配置",
                bl_idname="SSMTNode_PostProcess_ShapeKey",
                direct_export_mode=False,
            )
        ]
        _FakeBlueprintExportHelper.postprocess_nodes = nodes
        operator = self._make_operator(None)

        total = operator._estimate_background_total_steps(_FakeExportTree(), [], True)

        expected = export_step_plan.build_step_plan(
            _FakeExportTree(),
            direct_export=True,
            postprocess_nodes=nodes,
        )
        self.assertEqual(total, len(expected))
        self.assertIn("直出-形态键附加轮次", expected)
        self.assertNotIn("直出-形态键生成", expected)

    # -- 步骤进度条（window_manager.progress_*） ---------------------------

    def test_modal_drives_native_progress_bar(self):
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.total_steps = 5
        session.done_steps = 1
        session.poll_results = ["running", "success"]

        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(
            _FAKE_WINDOW_MANAGER.progress_calls,
            [("begin", 0, 100), ("update", 20)],
        )

        session.done_steps = 5
        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(
            _FAKE_WINDOW_MANAGER.progress_calls,
            [("begin", 0, 100), ("update", 20), ("update", 100), ("end",)],
        )

    def test_modal_places_progress_bar_before_step_text(self):
        """状态栏布局：``后台导出：`` + 进度条 + 空格 + ``done/total 阶段｜节点``。"""
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.total_steps = 32
        session.done_steps = 13
        session.progress_text = lambda: (
            "后台导出：13/32 前处理-恢复非镜像｜蕾米埃尔-NF/物体信息.013"
        )
        session.poll_results = ["running"]

        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(
            _FAKE_AREAS[0].header,
            "后台导出：▏████████────────────▕ 40% 13/32 前处理-恢复非镜像｜"
            "蕾米埃尔-NF/物体信息.013",
        )

    def test_modal_keeps_text_when_progress_bar_empty(self):
        """进度条为空（准备 / 加载 / 完成等阶段）时状态栏文本保持原样。"""
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.progress_text = lambda: "后台导出完成（31/31，用时 27.7 秒）"
        operator._update_progress_bar = lambda _context, _session: ""
        session.poll_results = ["running"]

        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(_FAKE_AREAS[0].header, "后台导出完成（31/31，用时 27.7 秒）")

    def test_modal_progress_api_failure_falls_back_to_header_bar(self):
        """progress_* 抛异常时静默，并在 header 上退回文本进度条。"""
        _FAKE_WINDOW_MANAGER.progress_fail = True
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.total_steps = 4
        session.done_steps = 1
        session.poll_results = ["running"]

        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(_FAKE_WINDOW_MANAGER.progress_calls, [])
        self.assertIn("▏█████", _FAKE_AREAS[0].header)
        self.assertIn("25%", _FAKE_AREAS[0].header)

    def test_modal_progress_update_throttled_when_done_unchanged(self):
        """done 不变时不再调 progress_update（指针在「进度/繁忙」间高频跳动的根因）。"""
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.total_steps = 5
        session.done_steps = 2
        session.poll_results = ["running"]

        for _ in range(3):
            result = operator.modal(context, types.SimpleNamespace(type='TIMER'))
            self.assertEqual(result, {'RUNNING_MODAL'})
            # 文本进度条仍每 tick 刷新（节流只针对原生 progress_update）
            self.assertIn("▏████████────────────▕ 40%", _FAKE_AREAS[0].header)

        self.assertEqual(
            _FAKE_WINDOW_MANAGER.progress_calls,
            [("begin", 0, 100), ("update", 40)],
        )

    def test_modal_progress_update_fires_once_per_done_change(self):
        """done 每次变化只调一次 progress_update；重复的 done 被跳过。"""
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.total_steps = 5
        session.poll_results = ["running"]

        for done in (1, 1, 3, 3, 5, 5):
            session.done_steps = done
            result = operator.modal(context, types.SimpleNamespace(type='TIMER'))
            self.assertEqual(result, {'RUNNING_MODAL'})

        self.assertEqual(
            _FAKE_WINDOW_MANAGER.progress_calls,
            [("begin", 0, 100), ("update", 20), ("update", 60), ("update", 100)],
        )

    def test_modal_progress_begin_called_only_once(self):
        """progress_begin 只在第一次 tick 调一次，之后无论 done 怎么变都不再 begin。"""
        context = self._make_context()
        operator = self._make_operator(context)
        operator.invoke(context, None)
        session = _RecordingSession.instances[0]
        session.total_steps = 4
        session.poll_results = ["running"]

        for done in (0, 1, 2, 3, 4):
            session.done_steps = done
            result = operator.modal(context, types.SimpleNamespace(type='TIMER'))
            self.assertEqual(result, {'RUNNING_MODAL'})

        begins = [call for call in _FAKE_WINDOW_MANAGER.progress_calls if call[0] == "begin"]
        self.assertEqual(begins, [("begin", 0, 100)])

    def test_finish_background_export_ends_progress_bar(self):
        context = self._make_context()
        for outcome in ("success", "cancel", "error"):
            with self.subTest(outcome=outcome):
                _FAKE_WINDOW_MANAGER.progress_calls = []
                operator = self._make_operator(context)
                operator._bg_progress_started = True
                session = _RecordingSession()

                operator._finish_background_export(context, session, outcome)

                self.assertEqual(_FAKE_WINDOW_MANAGER.progress_calls, [("end",)])


# ---------------------------------------------------------------------------
# 7. 细粒度进度中心 / 节点高亮 / 进度桥
# ---------------------------------------------------------------------------


class _FakeProgressNode:
    def __init__(self, name, color=(0.5, 0.5, 0.5), use_custom_color=False):
        self.name = name
        self.color = color
        self.use_custom_color = use_custom_color


class _FakeNodeCollection:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def get(self, name):
        for node in self._nodes:
            if node.name == name:
                return node
        return None

    def __iter__(self):
        return iter(self._nodes)


class ProgressCenterTests(_BackgroundTestBase):
    """``blueprint/export_progress.py``：stage.json 字段与合并写。"""

    def _stage_path(self, name="progress_stage.json"):
        return os.path.join(self.temp_dir, name)

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def test_reports_stage_nodes_and_single_node(self):
        stage_path = self._stage_path()
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)

        export_progress.report_stage("直出-收集节点", 0, 3)
        export_progress.report_nodes(["节点A", "节点B"])

        stage = self._read(stage_path)
        self.assertEqual(stage["stage"], "直出-收集节点")
        self.assertEqual(stage["done"], 0)
        # report_stage 只更新显示文本，不再改 done/total（显式传入也被忽略）
        self.assertEqual(stage["total"], 0)
        self.assertEqual(stage["nodes"], {"节点A": "pending", "节点B": "pending"})
        self.assertEqual(stage["node"], "")
        self.assertGreaterEqual(stage["elapsed"], 0.0)

        export_progress.report_node("节点A", "running")
        stage = self._read(stage_path)
        self.assertEqual(stage["node"], "节点A")
        self.assertEqual(stage["nodes"]["节点A"], "running")

        export_progress.report_node("节点A", "done")
        stage = self._read(stage_path)
        self.assertEqual(stage["node"], "")
        self.assertEqual(stage["nodes"]["节点A"], "done")
        self.assertEqual(stage["nodes"]["节点B"], "pending")

    def test_report_stage_keeps_previous_fields(self):
        """合并写：粗粒度阶段的 done/total/message 不会被细粒度阶段冲掉。"""
        stage_path = self._stage_path("progress_merge.json")
        with open(stage_path, "w", encoding="utf-8") as file:
            json.dump(
                {"stage": "第 2 轮", "done": 1, "total": 4, "message": "完成 Meshes"},
                file,
                ensure_ascii=False,
            )

        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.report_stage("前处理-创建副本")

        stage = self._read(stage_path)
        self.assertEqual(stage["stage"], "前处理-创建副本")
        self.assertEqual(stage["done"], 1)
        self.assertEqual(stage["total"], 4)
        self.assertEqual(stage["message"], "完成 Meshes")

    def test_node_sub_stage_text_restored_after_node_done(self):
        """节点内的细分文本（<节点名>：…）在节点完成后还原为粗粒度阶段名。"""
        stage_path = self._stage_path("progress_substage.json")
        with open(stage_path, "w", encoding="utf-8") as file:
            json.dump(
                {"stage": "后处理", "done": 1, "total": 4, "message": "执行后处理节点"},
                file,
                ensure_ascii=False,
            )
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.begin_steps(4)

        export_progress.report_nodes(["材质转资源pro"])
        export_progress.report_node("材质转资源pro", "running")
        export_progress.report_stage("材质转资源pro：转换贴图 3/26")
        self.assertEqual(self._read(stage_path)["stage"], "材质转资源pro：转换贴图 3/26")

        export_progress.report_node("材质转资源pro", "done")
        stage = self._read(stage_path)
        # 细分级文本不漏到下一个节点：还原成节点开始前的阶段名
        self.assertEqual(stage["stage"], "后处理")
        self.assertEqual(stage["node"], "")

    def test_elapsed_counts_from_stage_file_setup(self):
        export_progress.set_stage_file(self._stage_path("progress_elapsed.json"))
        self.addCleanup(export_progress.clear_stage_file)
        time.sleep(0.02)
        self.assertGreater(export_progress.elapsed(), 0.0)
        self.assertLess(export_progress.elapsed(), 5.0)

    def test_write_failure_is_silent(self):
        export_progress.set_stage_file(
            os.path.join(self.temp_dir, "missing_dir", "nested", "stage.json")
        )
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.report_stage("无目录")
        export_progress.report_nodes(["节点A"])
        export_progress.report_node("节点A", "done")  # 不抛异常即通过


class _StageNode:
    """直出阶段节点上报测试用的最小节点。"""

    def __init__(
        self,
        name,
        bl_idname,
        mute=False,
        connected_to_output=True,
        blueprint_name="",
        id_data=None,
    ):
        self.name = name
        self.bl_idname = bl_idname
        self.mute = mute
        self.connected_to_output = connected_to_output
        self.blueprint_name = blueprint_name
        self.id_data = id_data


class _StageTree:
    def __init__(self, nodes, name="", bl_idname=""):
        self.nodes = list(nodes)
        self.name = name
        self.bl_idname = bl_idname


class DirectExportStageNodeProgressTests(_BackgroundTestBase):
    """``direct_export.py`` 的阶段节点上报：按 bl_idname 批量、跳静音/空名、静默。"""

    def _stage_path(self, name="direct_stage_nodes.json"):
        return os.path.join(self.temp_dir, name)

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def _set_stage(self):
        stage_path = self._stage_path()
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.begin_steps(5)
        return stage_path

    @staticmethod
    def _tree():
        return _StageTree(
            [
                _StageNode("物体信息A", "SSMTNode_Object_Info"),
                _StageNode("物体信息B", "SSMTNode_Object_Info"),
                _StageNode("顶点组处理", "SSMTNode_VertexGroupProcess"),
                _StageNode("静音物体信息", "SSMTNode_Object_Info", mute=True),
                _StageNode("", "SSMTNode_Object_Info"),
                _StageNode("无关节点", "SSMTNode_DataType"),
            ]
        )

    def test_reports_matching_nodes_by_bl_idname(self):
        stage_path = self._set_stage()
        direct_export_module._report_stage_nodes(
            self._tree(),
            ("SSMTNode_Object_Info", "SSMTNode_VertexGroupProcess"),
            export_progress.STATE_RUNNING,
        )

        stage = self._read(stage_path)
        self.assertEqual(
            stage["nodes"],
            {
                "物体信息A": "running",
                "物体信息B": "running",
                "顶点组处理": "running",
            },
        )
        self.assertEqual(stage["node"], "顶点组处理")

        direct_export_module._report_stage_nodes(
            self._tree(),
            ("SSMTNode_Object_Info", "SSMTNode_VertexGroupProcess"),
            export_progress.STATE_DONE,
        )
        stage = self._read(stage_path)
        self.assertEqual(
            stage["nodes"],
            {
                "物体信息A": "done",
                "物体信息B": "done",
                "顶点组处理": "done",
            },
        )
        # 阶段节点只驱动高亮，不占用主进程预计算的步骤数
        self.assertEqual((stage["done"], stage["total"]), (0, 5))

    def test_pending_batch_registers_only_matching_nodes(self):
        stage_path = self._set_stage()
        direct_export_module._report_stage_nodes(
            self._tree(),
            ("SSMTNode_Object_Info", "SSMTNode_Result_Output"),
            export_progress.STATE_PENDING,
        )

        stage = self._read(stage_path)
        self.assertEqual(
            stage["nodes"],
            {"物体信息A": "pending", "物体信息B": "pending"},
        )
        self.assertEqual(stage["node"], "")

    def test_participant_collection_matches_main_process_filter(self):
        tree = _StageTree(
            [
                _StageNode("物体信息A", "SSMTNode_Object_Info"),
                _StageNode("物体组", "SSMTNode_Object_Group"),
                _StageNode("后处理节点", "SSMTNode_PostProcess_ShapeKey"),
                _StageNode("输出节点", "SSMTNode_Result_Output"),
                _StageNode("中转", "NodeReroute"),
                _StageNode("静音物体信息", "SSMTNode_Object_Info", mute=True),
                _StageNode("断开物体组", "SSMTNode_Object_Group", connected_to_output=False),
                _StageNode("", "SSMTNode_Object_Info"),
            ]
        )

        self.assertEqual(
            direct_export_module._collect_participant_node_names(tree),
            ["物体信息A", "物体组"],
        )

    def test_report_failures_are_silent(self):
        direct_export_module._report_stage_nodes(
            _StageTree([_StageNode("物体信息A", "SSMTNode_Object_Info")]),
            ("SSMTNode_Object_Info",),
            export_progress.STATE_RUNNING,
        )

        class _ExplodingTree:
            @property
            def nodes(self):
                raise RuntimeError("nodes unavailable")

        direct_export_module._report_stage_nodes(
            _ExplodingTree(),
            ("SSMTNode_Object_Info",),
            export_progress.STATE_RUNNING,
        )

        with mock.patch.object(
            export_progress, "report_node", side_effect=RuntimeError("progress broken")
        ):
            direct_export_module._report_stage_nodes(
                _StageTree([_StageNode("物体信息A", "SSMTNode_Object_Info")]),
                ("SSMTNode_Object_Info",),
                export_progress.STATE_RUNNING,
            )


class DirectExportNestedTreeProgressTests(_BackgroundTestBase):
    """worker 侧节点键 ``树名/节点名`` 与嵌套蓝图递归收集。"""

    def _stage_path(self, name="direct_nested_stage.json"):
        return os.path.join(self.temp_dir, name)

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def _set_stage(self):
        stage_path = self._stage_path()
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.begin_steps(5)
        return stage_path

    def _install_trees(self, *trees):
        mapping = {tree.name: tree for tree in trees}
        original_get = _FAKE_BPY.data.node_groups.get
        _FAKE_BPY.data.node_groups.get = lambda name: mapping.get(name)
        self.addCleanup(setattr, _FAKE_BPY.data.node_groups, "get", original_get)

    def _nested_fixture(self):
        """主蓝图 + 子蓝图 + 循环引用 + NONE/缺失/非 SSMT 引用。"""
        nested_leaf = _StageNode("物体信息子", "SSMTNode_Object_Info")
        back_to_main = _StageNode(
            "回指主树", "SSMTNode_Blueprint_Nest", blueprint_name="主蓝图"
        )
        sub_tree = _StageTree(
            [nested_leaf, back_to_main],
            name="子蓝图",
            bl_idname="SSMTBlueprintTreeType",
        )
        main_leaf = _StageNode("物体信息主", "SSMTNode_Object_Info")
        nest_entry = _StageNode(
            "嵌套入口", "SSMTNode_Blueprint_Nest", blueprint_name="子蓝图"
        )
        none_ref = _StageNode(
            "空引用", "SSMTNode_Blueprint_Nest", blueprint_name="NONE"
        )
        missing_ref = _StageNode(
            "缺失引用", "SSMTNode_Blueprint_Nest", blueprint_name="不存在的蓝图"
        )
        non_ssmt_ref = _StageNode(
            "引用普通树", "SSMTNode_Blueprint_Nest", blueprint_name="普通节点树"
        )
        main_tree = _StageTree(
            [main_leaf, nest_entry, none_ref, missing_ref, non_ssmt_ref],
            name="主蓝图",
            bl_idname="SSMTBlueprintTreeType",
        )
        non_ssmt_tree = _StageTree(
            [_StageNode("非SSMT节点", "SSMTNode_Object_Info")],
            name="普通节点树",
            bl_idname="ShaderNodeTree",
        )
        self._install_trees(main_tree, sub_tree, non_ssmt_tree)
        return main_tree, sub_tree

    def test_stage_nodes_use_tree_prefixed_keys(self):
        """主树与嵌套树里同 bl_idname 的节点上报为 ``树名/节点名``。"""
        stage_path = self._set_stage()
        main_tree, _sub_tree = self._nested_fixture()

        direct_export_module._report_stage_nodes(
            main_tree,
            ("SSMTNode_Object_Info",),
            export_progress.STATE_RUNNING,
        )

        stage = self._read(stage_path)
        self.assertEqual(
            stage["nodes"],
            {"主蓝图/物体信息主": "running", "子蓝图/物体信息子": "running"},
        )

    def test_pending_batch_includes_nested_tree_nodes(self):
        stage_path = self._set_stage()
        main_tree, _sub_tree = self._nested_fixture()

        direct_export_module._report_stage_nodes(
            main_tree,
            ("SSMTNode_Object_Info",),
            export_progress.STATE_PENDING,
        )

        self.assertEqual(
            self._read(stage_path)["nodes"],
            {"主蓝图/物体信息主": "pending", "子蓝图/物体信息子": "pending"},
        )

    def test_participant_collection_walks_nested_trees_without_looping(self):
        """主树 + 嵌套树节点都被收集；循环引用不死循环；NONE/缺失/非 SSMT 跳过。"""
        main_tree, _sub_tree = self._nested_fixture()

        keys = direct_export_module._collect_participant_node_names(main_tree)

        # 主树在前，嵌套树随后；每个键只出现一次（循环引用不重复）
        self.assertEqual(keys[:4], [
            "主蓝图/物体信息主",
            "主蓝图/嵌套入口",
            "主蓝图/空引用",
            "主蓝图/缺失引用",
        ])
        self.assertIn("子蓝图/物体信息子", keys)
        self.assertIn("子蓝图/回指主树", keys)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("普通节点树/非SSMT节点", keys)

    def test_iter_stage_node_names_skips_none_and_missing_references(self):
        main_tree, _sub_tree = self._nested_fixture()

        self.assertEqual(
            direct_export_module._iter_stage_node_names(
                main_tree, ("SSMTNode_Blueprint_Nest",)
            ),
            [
                "主蓝图/嵌套入口",
                "主蓝图/空引用",
                "主蓝图/缺失引用",
                "主蓝图/引用普通树",
                "子蓝图/回指主树",
            ],
        )

    def test_postprocess_node_key_uses_owning_tree(self):
        stage_path = self._set_stage()
        _main_tree, sub_tree = self._nested_fixture()
        node = _StageNode(
            "嵌套后处理", "SSMTNode_PostProcess_ShapeKey", id_data=sub_tree
        )

        direct_export_module._report_postprocess_node(
            node, export_progress.STATE_RUNNING
        )

        stage = self._read(stage_path)
        self.assertEqual(stage["nodes"], {"子蓝图/嵌套后处理": "running"})
        self.assertEqual(stage["node"], "子蓝图/嵌套后处理")


class StepProgressTests(_BackgroundTestBase):
    """步骤计数：``begin_steps`` / ``complete_step`` / ``report_node(done)`` / 进度桥。"""

    def _stage_path(self, name="step_stage.json"):
        return os.path.join(self.temp_dir, name)

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def test_begin_complete_and_node_done_update_done_total(self):
        stage_path = self._stage_path()
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)

        export_progress.begin_steps(5)
        stage = self._read(stage_path)
        self.assertEqual((stage["done"], stage["total"]), (0, 5))
        self.assertEqual((export_progress.step_done(), export_progress.step_total()), (0, 5))

        export_progress.complete_step()
        self.assertEqual(self._read(stage_path)["done"], 1)

        export_progress.report_nodes(["节点A", "节点B"])
        export_progress.report_node("节点A", "running")
        self.assertEqual(self._read(stage_path)["done"], 1)  # running 不推进

        export_progress.report_node("节点A", "done")
        stage = self._read(stage_path)
        self.assertEqual(stage["done"], 2)
        self.assertEqual(stage["total"], 5)
        self.assertEqual(stage["nodes"]["节点A"], "done")
        self.assertEqual(stage["nodes"]["节点B"], "pending")

        # 同一节点重复上报 done 不重复计步
        export_progress.report_node("节点A", "done")
        self.assertEqual(self._read(stage_path)["done"], 2)

    def test_complete_step_keeps_total_fixed(self):
        """旧预算模式（没有计划清单）：complete_step 推 done，但 total 不再增长。"""
        stage_path = self._stage_path("step_cap.json")
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.begin_steps(2)
        export_progress.complete_step()
        export_progress.complete_step()
        export_progress.complete_step()  # 预算用满后不再推进
        self.assertEqual(export_progress.step_done(), 2)
        self.assertEqual(export_progress.step_total(), 2)

    def test_finish_steps_fills_total(self):
        stage_path = self._stage_path("step_finish.json")
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.begin_steps(10)
        export_progress.complete_step()
        self.assertEqual(export_progress.finish_steps(), 10)
        stage = self._read(stage_path)
        self.assertEqual((stage["done"], stage["total"]), (10, 10))

    def test_start_stage_advances_previous_stage_only(self):
        """第一个阶段不推进；第二个阶段推进上一个；同名阶段重入不推进。"""
        stage_path = self._stage_path("step_bridge.json")
        timer_utils = sys.modules[f"{PKG}.utils.timer_utils"].TimerUtils
        self.assertTrue(export_background.install_progress_bridge(stage_path))
        self.addCleanup(export_background.uninstall_progress_bridge)
        export_progress.begin_steps(3)

        timer_utils.start_stage("Direct-CollectNodes")
        self.assertEqual(export_progress.step_done(), 0)  # 第一个阶段不推进
        self.assertEqual(self._read(stage_path)["stage"], "Direct-CollectNodes")

        timer_utils.start_stage("Direct-SetupState")
        self.assertEqual(export_progress.step_done(), 1)  # 推进上一个阶段

        timer_utils.start_stage("Direct-SetupState")
        self.assertEqual(export_progress.step_done(), 1)  # 同名阶段重入不推进

        timer_utils.start_stage("Preprocess-CreateCopies")
        self.assertEqual(export_progress.step_done(), 2)  # 推进 Direct-SetupState
        self.assertEqual(self._read(stage_path)["stage"], "Preprocess-CreateCopies")

        # 每个不同名称的子阶段都算 1 步（Preprocess-* 也计步）
        timer_utils.start_stage("Direct-CollectObjects")
        self.assertEqual(export_progress.step_done(), 3)  # 推进 Preprocess-CreateCopies
        self.assertEqual(self._read(stage_path)["stage"], "Direct-CollectObjects")

    def test_progress_text_omits_score_and_pipe_when_absent(self):
        session = self.make_session()
        session.stage = "准备"
        session.stage_total = 0
        session.stage_node = ""
        self.assertEqual(session.progress_text(), "后台导出：准备")

        session.stage_node = "节点A"
        self.assertEqual(session.progress_text(), "后台导出：准备｜节点A")

    def test_refresh_stage_parses_step_progress(self):
        session = self.make_session(total_steps=5)
        self.assertTrue(session.start())
        _FakeProcess.instances[0].returncode = None
        with open(session.stage_path, "w", encoding="utf-8") as file:
            json.dump(
                {"stage": "直出-收集节点", "done": 2, "total": 5, "message": ""},
                file,
                ensure_ascii=False,
            )

        self.assertEqual(session.poll(), "running")
        self.assertEqual(session.done_steps, 2)
        self.assertEqual(session.total_steps, 5)
        self.assertEqual(session.progress_text(), "后台导出：2/5 直出-收集节点")
        session.cleanup()

    def test_format_progress_bar(self):
        self.assertEqual(
            export_background.format_progress_bar(0, 5), "▏────────────────────▕ 0%"
        )
        self.assertEqual(
            export_background.format_progress_bar(5, 5), "▏████████████████████▕ 100%"
        )
        self.assertEqual(export_background.format_progress_bar(3, 0), "")

    def test_format_percent_bar(self):
        self.assertEqual(export_background.format_percent_bar(0), "▏────────────────────▕ 0%")
        self.assertEqual(export_background.format_percent_bar(40), "▏████████────────────▕ 40%")
        self.assertEqual(export_background.format_percent_bar(100), "▏████████████████████▕ 100%")
        self.assertEqual(export_background.format_percent_bar(120), "▏████████████████████▕ 100%")

    def test_compose_background_progress_text_layout(self):
        """进度条插在「后台导出：」之后、步骤文本之前；为空时原样返回文本。"""
        compose = ui_func_export.compose_background_progress_text
        self.assertEqual(
            compose("后台导出：13/32 前处理-恢复非镜像｜树/节点", "▏████████────────────▕ 40%"),
            "后台导出：▏████████────────────▕ 40% 13/32 前处理-恢复非镜像｜树/节点",
        )
        self.assertEqual(compose("后台导出：准备 0/2", ""), "后台导出：准备 0/2")
        # 非标准进度文案（完成 / 取消 / 失败）不插入进度条，避免拼出重复前缀
        self.assertEqual(
            compose("后台导出完成（31/31，用时 27.7 秒）", "▏████████████████████▕ 100%"),
            "后台导出完成（31/31，用时 27.7 秒）",
        )


class FixedStepProgressTests(_BackgroundTestBase):
    """固定步数模型：**导出开始时一次算好清单，total 全程不变**。

    计步只认计划清单里、且整次导出没计过的名字；同名子阶段重复出现只计一次；
    清单之外的阶段只更新文本；贴图张数这类更深层内部进度也只显示文本。
    ``finish_steps`` 把 done 补满到 total（100%）。
    """

    #: 实测录制到的阶段序列（顺序即实际出现顺序）；用于模拟执行序列
    RECORDED_STAGES = (
        "加载工程",
        "前处理-计算缓存哈希",
        "前处理-清空形态键",
        "前处理-采样直出形态键",
        "前处理-应用形态键",
        "前处理-恢复非镜像",
        "直出-解析蓝图模型",
        "直出-构建导出器",
        "数据哈希预计算",
        "对象处理与合并",
        "Element数据解析",
        "索引去重与Buffer构建",
        "ShapeKey计算",
        "基础导出：写入资源…",
        "INI配置生成",
        "直出-形态键生成",
        "形态键扩展配置",
        "物体切换面板",
        "完成",
    )

    def _stage_path(self, name="dynamic_step.json"):
        return os.path.join(self.temp_dir, name)

    @staticmethod
    def _read(path):
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    def _begin(self, total=0, name="dynamic_step.json", plan=None):
        stage_path = self._stage_path(name)
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.begin_steps(total, plan=plan)
        return stage_path

    @staticmethod
    def _make_tree(nodes=()):
        """直出计划测试用的最小蓝图树 + 后处理节点。"""
        tree = types.SimpleNamespace(name="测试蓝图")
        for node in nodes:
            node.id_data = tree
        return tree

    @staticmethod
    def _make_node(name, bl_idname, direct_export_mode=True):
        return types.SimpleNamespace(
            name=name,
            bl_idname=bl_idname,
            direct_export_mode=direct_export_mode,
        )

    def _recorded_plan(self):
        """实测阶段的计划清单：直出 + 形态键 + 4 个后处理节点（共 31 步）。"""
        nodes = [
            self._make_node("材质转资源pro", "SSMTNode_PostProcess_CustomMaterialAssign"),
            self._make_node("形态键配置", "SSMTNode_PostProcess_ShapeKey"),
            self._make_node("形态键扩展配置", "SSMTNode_PostProcess_ShapeKeyExt"),
            self._make_node("物体切换面板", "SSMTNode_PostProcess_ObjectSwap"),
        ]
        tree = self._make_tree(nodes)
        return export_step_plan.build_step_plan(
            tree,
            direct_export=True,
            has_shapekey_export=True,
            max_shapekey_slot=1,
            has_multi_file_export=False,
            postprocess_nodes=nodes,
        )

    def test_direct_plan_lists_every_step_once_and_totals_31(self):
        """计划函数返回有序无重复清单：7 顶层 + 10 前处理 + 8 基础导出 + 4 节点 + 生成/收尾。"""
        plan = self._recorded_plan()

        self.assertEqual(len(plan), 31)
        self.assertEqual(len(set(plan)), len(plan))
        # 顶层 / 前处理 / 基础导出内部 / 节点 / 直出生成 / 收尾 都在清单里
        for name in (
            "直出-收集节点",
            "直出-执行前处理",
            "前处理-采样直出形态键",
            "ShapeKey计算",
            "缓冲文件生成",
            "INI配置生成",
            "测试蓝图/材质转资源pro",
            "测试蓝图/形态键配置",
            "直出-形态键生成",
            "收尾",
        ):
            self.assertIn(name, plan)
        # 每个后处理节点只算 1 步
        self.assertEqual(
            sum(1 for key in plan if key.startswith("测试蓝图/")), 4
        )

    def test_plan_matches_simulated_execution_and_finishes_at_100(self):
        """按实测阶段序列模拟执行：每步 done/total 单调不减，total 恒定，收尾 100%。"""
        plan = self._recorded_plan()
        stage_path = self._begin(0, plan=plan)

        events = list(self.RECORDED_STAGES)
        # 基础导出内部会反复切换同名子阶段：重复出现不能再计步
        events.insert(9, "数据哈希预计算")
        events.insert(10, "对象处理与合并")

        node_steps = [key for key in plan if key.startswith("测试蓝图/")]
        observed = []
        totals = []
        for name in events:
            export_progress.advance_stage(name)
            # 计划外阶段（录制里没有的）也不涨 total
            if name == "INI配置生成":
                export_progress.advance_stage("计划外阶段")
                before = (export_progress.step_done(), export_progress.step_total())
                export_progress.report_stage("材质转资源pro：转换贴图 26/60")
                self.assertEqual(
                    (export_progress.step_done(), export_progress.step_total()), before
                )
            observed.append(export_progress.step_done())
            totals.append(export_progress.step_total())

        # 后处理节点逐个完成（每个节点 1 步）
        for key in node_steps:
            export_progress.report_node(key, "running")
            export_progress.report_node(key, "done")
            observed.append(export_progress.step_done())
            totals.append(export_progress.step_total())

        self.assertTrue(all(total == len(plan) for total in totals), "total 必须全程不变")
        self.assertEqual(observed, sorted(observed), "done 不能回退")
        self.assertLessEqual(max(observed), len(plan))

        # 收尾补满：done == len(plan) == total，百分比 100
        export_progress.finish_steps()
        self.assertEqual(
            (export_progress.step_done(), export_progress.step_total()),
            (len(plan), len(plan)),
        )
        self.assertEqual(export_progress.step_percent(), 100)
        stage = self._read(stage_path)
        self.assertEqual((stage["done"], stage["total"]), (len(plan), len(plan)))
        self.assertEqual(stage["plan"], plan)

    def test_out_of_plan_stage_does_not_change_done_or_total(self):
        """清单之外的阶段：只更新文本，done / total 都不变。"""
        plan = ["阶段A", "阶段B", "收尾"]
        stage_path = self._begin(len(plan), plan=plan)

        export_progress.advance_stage("阶段A")
        self.assertEqual(export_progress.step_done(), 0)
        export_progress.advance_stage("清单外阶段")  # 完成阶段A
        self.assertEqual(export_progress.step_done(), 1)
        before = (export_progress.step_done(), export_progress.step_total())

        export_progress.advance_stage("另一个清单外阶段")
        self.assertEqual(
            (export_progress.step_done(), export_progress.step_total()), before
        )
        self.assertEqual(export_progress.step_total(), len(plan))
        self.assertEqual(self._read(stage_path)["stage"], "另一个清单外阶段")

    def test_worker_total_stays_fixed_across_advance_stage(self):
        """worker 的 total 在整次导出中保持不变（多次 advance_stage + 节点完成）。"""
        plan = ["阶段A", "阶段B", "节点X", "收尾"]
        self._begin(0, plan=plan)

        for name in ("阶段A", "阶段B", "阶段A", "阶段C", "阶段D"):
            export_progress.advance_stage(name)
            self.assertEqual(export_progress.step_total(), len(plan))
        export_progress.report_node("节点X", "running")
        self.assertEqual(export_progress.step_total(), len(plan))
        export_progress.report_node("节点X", "done")
        self.assertEqual(export_progress.step_total(), len(plan))

    def test_finish_steps_fills_plan_and_reaches_100(self):
        """收尾把 done 补到 total；total 保持不变，百分比到 100。"""
        plan = ["阶段A", "阶段B", "阶段C", "收尾"]
        self._begin(0, plan=plan)
        export_progress.advance_stage("阶段A")
        export_progress.advance_stage("阶段B")
        self.assertEqual(export_progress.step_done(), 1)
        self.assertLess(export_progress.step_percent(), 100)

        export_progress.finish_steps()
        self.assertEqual(
            (export_progress.step_done(), export_progress.step_total()),
            (len(plan), len(plan)),
        )
        self.assertEqual(export_progress.step_percent(), 100)

    def test_report_stage_text_only_does_not_change_steps(self):
        """贴图张数（report_stage）只更新文本，不推百分比；后续阶段仍正确计步。"""
        plan = ["缓冲文件生成", "INI配置生成", "收尾"]
        stage_path = self._begin(len(plan), plan=plan)
        export_progress.advance_stage("缓冲文件生成")
        before = (export_progress.step_done(), export_progress.step_total())

        export_progress.report_node("材质转资源pro", "running")
        for index in (1, 26, 60):
            export_progress.report_stage(f"材质转资源pro：转换贴图 {index}/60")
            self.assertEqual(
                (export_progress.step_done(), export_progress.step_total()), before
            )
        self.assertEqual(
            self._read(stage_path)["stage"], "材质转资源pro：转换贴图 60/60"
        )

        # 显示文本被改写成细分文本后，阶段切换仍按计步名判断（不把贴图文本当阶段）
        export_progress.advance_stage("INI配置生成")
        self.assertEqual(export_progress.step_done(), 1)
        self.assertEqual(export_progress.step_total(), len(plan))

    def test_same_stage_name_repeated_is_counted_once(self):
        """同名阶段重复出现只计 1 步（基础导出的子阶段会反复切换）。"""
        plan = ["数据哈希预计算", "对象处理与合并", "ShapeKey计算", "收尾"]
        self._begin(len(plan), plan=plan)
        for name in (
            "数据哈希预计算",
            "对象处理与合并",
            "数据哈希预计算",
            "对象处理与合并",
            "ShapeKey计算",
        ):
            export_progress.advance_stage(name)

        self.assertEqual(export_progress.step_done(), 2)  # 前两个唯一名各 1 步
        self.assertEqual(export_progress.step_total(), len(plan))  # total 固定
        self.assertEqual(export_progress.step_percent(), 50)

    def test_total_stays_fixed_when_new_stage_appears(self):
        """出现新阶段时 total 不变（旧实现会 total = max(total, done+1)）。"""
        plan = ["阶段A", "阶段B", "收尾"]
        self._begin(len(plan), plan=plan)
        export_progress.advance_stage("阶段A")
        self.assertEqual(
            (export_progress.step_done(), export_progress.step_total()), (0, len(plan))
        )

        for name in ("阶段B", "阶段C", "阶段D", "阶段E", "阶段F"):
            export_progress.advance_stage(name)
            self.assertEqual(export_progress.step_total(), len(plan))
        # 清单内只完成了 阶段A / 阶段B 两步
        self.assertEqual(export_progress.step_done(), 2)

        export_progress.finish_steps()
        self.assertEqual(
            (export_progress.step_done(), export_progress.step_total()),
            (len(plan), len(plan)),
        )
        self.assertEqual(export_progress.step_percent(), 100)

    def test_node_done_counts_stage_and_node_once(self):
        """节点内部启动的计划阶段与节点本身各算 1 步；重复上报 done 不再计步。"""
        plan = ["后处理", "直出-形态键生成", "形态键节点", "收尾"]
        self._begin(len(plan), plan=plan)
        export_progress.advance_stage("后处理")
        export_progress.report_nodes(["形态键节点"])
        export_progress.report_node("形态键节点", "running")
        export_progress.advance_stage("直出-形态键生成")
        self.assertEqual(export_progress.step_done(), 1)  # 完成"后处理"

        export_progress.report_node("形态键节点", "done")
        self.assertEqual(export_progress.step_done(), 3)  # 阶段 + 节点各 1 步
        self.assertEqual(export_progress.step_total(), len(plan))

        export_progress.report_node("形态键节点", "done")
        self.assertEqual(export_progress.step_done(), 3)  # 同名节点不重复计

        export_progress.finish_steps()
        self.assertEqual(export_progress.step_percent(), 100)

    def test_refresh_stage_percent_monotonic_when_total_grows(self):
        """主进程：total 动态变大时百分比不回退（取已显示最大值），最终 100%。"""
        session = self.make_session(total_steps=5)
        self.assertTrue(session.start())
        _FakeProcess.instances[0].returncode = None

        samples = (
            (2, 5),
            (3, 8),   # 原始百分比 37% < 40%：必须保持 40%
            (5, 9),
            (9, 9),
        )
        percents = []
        for done, total in samples:
            with open(session.stage_path, "w", encoding="utf-8") as file:
                json.dump(
                    {"stage": "基础导出", "done": done, "total": total, "message": ""},
                    file,
                    ensure_ascii=False,
                )
            self.assertEqual(session.poll(), "running")
            self.assertEqual(session.total_steps, total)  # total 以 stage.json 为准
            self.assertEqual(session.done_steps, done)
            percents.append(session.progress_percent)

        self.assertEqual(percents, [40, 40, 55, 100])
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(session.progress_text(), "后台导出：9/9 基础导出")
        session.cleanup()

    def test_update_progress_bar_uses_session_percent(self):
        """UI 用主进程的整数百分比驱动原生条（固定 0~100 刻度）与文本条。"""
        session = self.make_session(total_steps=5)
        self.assertTrue(session.start())
        _FakeProcess.instances[0].returncode = None
        with open(session.stage_path, "w", encoding="utf-8") as file:
            json.dump(
                {"stage": "基础导出", "done": 3, "total": 10, "message": ""},
                file,
                ensure_ascii=False,
            )
        self.assertEqual(session.poll(), "running")
        self.assertEqual(session.progress_percent, 30)

        operator = ui_func_export.SSMTGenerateModBlueprint()
        text = operator._update_progress_bar(
            types.SimpleNamespace(window_manager=_FAKE_WINDOW_MANAGER), session
        )

        self.assertEqual(
            _FAKE_WINDOW_MANAGER.progress_calls,
            [("begin", 0, 100), ("update", 30)],
        )
        self.assertIn("▏██████──────────────▕ 30%", text)
        session.cleanup()


class NodeHighlightTests(_BackgroundTestBase):
    """``SSMTGenerateModBlueprint`` 的节点高亮：应用 / 恢复 / 每个 tick 同步。"""

    TREE_NAME = "测试蓝图"

    def setUp(self):
        super().setUp()
        # 高亮默认关闭（打开节点编辑器时会拖慢状态栏），测试里显式打开验证逻辑
        self._highlight_enabled_backup = ui_func_export._BG_NODE_HIGHLIGHT_ENABLED
        ui_func_export._BG_NODE_HIGHLIGHT_ENABLED = True
        ui_func_export._BG_HIGHLIGHT_MIN_INTERVAL = 0.0
        self.addCleanup(
            setattr,
            ui_func_export,
            "_BG_NODE_HIGHLIGHT_ENABLED",
            self._highlight_enabled_backup,
        )

    def _install_tree(self, *nodes):
        tree = types.SimpleNamespace(name=self.TREE_NAME, nodes=_FakeNodeCollection(nodes))
        original_get = _FAKE_BPY.data.node_groups.get
        _FAKE_BPY.data.node_groups.get = (
            lambda name: tree if name == self.TREE_NAME else None
        )
        self.addCleanup(setattr, _FAKE_BPY.data.node_groups, "get", original_get)
        return tree

    @staticmethod
    def _operator():
        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator._bg_node_colors = {}
        return operator

    def test_applies_running_and_done_colors_then_restores(self):
        # Blender 5.x 的 node.color 是 3 分量（4 分量赋值会 ValueError，实机验证过）
        running = _FakeProgressNode("形态键节点", color=(0.11, 0.22, 0.33))
        done = _FakeProgressNode("多文件节点", color=(0.4, 0.5, 0.6), use_custom_color=True)
        self._install_tree(running, done)
        operator = self._operator()
        session = types.SimpleNamespace(
            tree_name=self.TREE_NAME,
            stage_nodes={"形态键节点": "running", "多文件节点": "done"},
        )

        operator._sync_node_highlight(session)

        self.assertTrue(running.use_custom_color)
        self.assertEqual(tuple(running.color), (0.95, 0.72, 0.1))
        self.assertTrue(done.use_custom_color)
        self.assertEqual(tuple(done.color), (0.22, 0.78, 0.3))

        operator._restore_node_colors()

        self.assertFalse(running.use_custom_color)
        self.assertEqual(tuple(running.color), (0.11, 0.22, 0.33))
        self.assertTrue(done.use_custom_color)
        self.assertEqual(tuple(done.color), (0.4, 0.5, 0.6))
        self.assertIsNone(operator._bg_node_colors)

    def test_color_dimension_follows_node(self):
        """旧版本 Blender 的 4 分量节点：高亮与恢复都按 4 分量赋值。"""
        node = _FakeProgressNode("四分量节点", color=(0.1, 0.2, 0.3, 1.0))
        self._install_tree(node)
        operator = self._operator()

        operator._sync_node_highlight(
            types.SimpleNamespace(tree_name=self.TREE_NAME, stage_nodes={"四分量节点": "done"})
        )
        self.assertEqual(tuple(node.color), (0.22, 0.78, 0.3, 1.0))

        operator._restore_node_colors()
        self.assertEqual(tuple(node.color), (0.1, 0.2, 0.3, 1.0))

    def test_unknown_node_and_unknown_status_are_ignored(self):
        node = _FakeProgressNode("已知节点", color=(0.3, 0.3, 0.3))
        self._install_tree(node)
        operator = self._operator()

        operator._sync_node_highlight(
            types.SimpleNamespace(
                tree_name=self.TREE_NAME,
                stage_nodes={"已知节点": "unknown-status", "不存在节点": "running"},
            )
        )

        self.assertFalse(node.use_custom_color)
        self.assertEqual(tuple(node.color), (0.3, 0.3, 0.3))

    def test_pending_nodes_highlighted_once_then_not_rewritten(self):
        """导出开始后所有 pending 节点一次性上蓝灰；后续 tick 不重复写同一批。"""
        nodes = [
            _FakeProgressNode("节点A", color=(0.11, 0.12, 0.13)),
            _FakeProgressNode("节点B", color=(0.21, 0.22, 0.23)),
            _FakeProgressNode("节点C", color=(0.31, 0.32, 0.33)),
        ]
        self._install_tree(*nodes)
        operator = self._operator()
        stage_nodes = {"节点A": "pending", "节点B": "pending", "节点C": "pending"}

        operator._sync_node_highlight(
            types.SimpleNamespace(tree_name=self.TREE_NAME, stage_nodes=dict(stage_nodes))
        )

        for node in nodes:
            self.assertTrue(node.use_custom_color)
            self.assertEqual(tuple(node.color), (0.45, 0.52, 0.68))

        # 状态没变 → 第二次 tick 不重写（把颜色改成哨兵值，重写会变回蓝灰）
        for node in nodes:
            node.color = (0.9, 0.9, 0.9)
        operator._sync_node_highlight(
            types.SimpleNamespace(tree_name=self.TREE_NAME, stage_nodes=dict(stage_nodes))
        )
        for node in nodes:
            self.assertEqual(tuple(node.color), (0.9, 0.9, 0.9))

        # pending 的原色也要由收尾恢复
        operator._restore_node_colors()
        self.assertFalse(nodes[0].use_custom_color)
        self.assertEqual(tuple(nodes[0].color), (0.11, 0.12, 0.13))
        self.assertEqual(tuple(nodes[1].color), (0.21, 0.22, 0.23))
        self.assertEqual(tuple(nodes[2].color), (0.31, 0.32, 0.33))

    def test_status_transition_pending_running_done(self):
        """pending → running → done：每次状态变化换色，原色仍是最开始那一份。"""
        node = _FakeProgressNode("状态节点", color=(0.1, 0.2, 0.3))
        self._install_tree(node)
        operator = self._operator()

        def _sync(status):
            operator._sync_node_highlight(
                types.SimpleNamespace(
                    tree_name=self.TREE_NAME, stage_nodes={"状态节点": status}
                )
            )

        _sync("pending")
        self.assertEqual(tuple(node.color), (0.45, 0.52, 0.68))
        _sync("running")
        self.assertEqual(tuple(node.color), (0.95, 0.72, 0.1))
        _sync("done")
        self.assertEqual(tuple(node.color), (0.22, 0.78, 0.3))

        operator._restore_node_colors()
        self.assertEqual(tuple(node.color), (0.1, 0.2, 0.3))

    def test_participants_highlighted_through_export_phases(self):
        """前处理 / 解析节点：加载=蓝灰 → 阶段开始=黄 → 后处理开始=绿。"""
        info = _FakeProgressNode("物体信息", color=(0.05, 0.1, 0.15))
        info.bl_idname = "SSMTNode_Object_Info"
        group = _FakeProgressNode("物体组", color=(0.2, 0.25, 0.3))
        group.bl_idname = "SSMTNode_Object_Group"
        post = _FakeProgressNode("后处理节点", color=(0.4, 0.4, 0.4))
        post.bl_idname = "SSMTNode_PostProcess_ShapeKey"
        output = _FakeProgressNode("Mod输出", color=(0.5, 0.5, 0.5))
        output.bl_idname = "SSMTNode_Result_Output"
        self._install_tree(info, group, post, output)
        operator = self._operator()

        def _sync(stage, stage_nodes):
            operator._sync_node_highlight(
                types.SimpleNamespace(
                    tree_name=self.TREE_NAME, stage=stage, stage_nodes=dict(stage_nodes)
                )
            )

        # 加载工程：参与导出的节点先上 pending 蓝灰；后处理节点等 worker 上报
        _sync("加载工程", {})
        self.assertEqual(tuple(info.color), (0.45, 0.52, 0.68))
        self.assertEqual(tuple(group.color), (0.45, 0.52, 0.68))
        self.assertFalse(post.use_custom_color)
        # 输出节点不算「参与导出的工作节点」
        self.assertFalse(output.use_custom_color)

        # 前处理 / 蓝图解析 / 导出轮次阶段：黄
        _sync("前处理", {})
        self.assertEqual(tuple(info.color), (0.95, 0.72, 0.1))
        self.assertEqual(tuple(group.color), (0.95, 0.72, 0.1))

        # worker 开始上报后处理节点 = 前处理等阶段已结束：绿
        _sync("后处理", {"后处理节点": "running"})
        self.assertEqual(tuple(info.color), (0.22, 0.78, 0.3))
        self.assertEqual(tuple(group.color), (0.22, 0.78, 0.3))
        self.assertEqual(tuple(post.color), (0.95, 0.72, 0.1))

        # 收尾恢复原色（参与导出节点也一起恢复）
        operator._restore_node_colors()
        self.assertEqual(tuple(info.color), (0.05, 0.1, 0.15))
        self.assertEqual(tuple(group.color), (0.2, 0.25, 0.3))
        self.assertEqual(tuple(post.color), (0.4, 0.4, 0.4))
        self.assertFalse(output.use_custom_color)

    def test_modal_syncs_highlight_on_every_tick(self):
        self._install_tree()
        operator = self._operator()
        calls = []
        operator._sync_node_highlight = lambda session: calls.append(session)
        session = types.SimpleNamespace(
            poll=lambda: "running",
            progress_text=lambda: "后台导出：准备 0/2",
            stage_nodes={},
            tree_name=self.TREE_NAME,
        )
        operator._bg_session = session
        _FAKE_AREAS[0].header = "<unset>"

        result = operator.modal(
            types.SimpleNamespace(window_manager=_FAKE_WINDOW_MANAGER),
            types.SimpleNamespace(type='TIMER'),
        )

        self.assertEqual(result, {'RUNNING_MODAL'})
        self.assertEqual(calls, [session])
        self.assertEqual(_FAKE_AREAS[0].header, "后台导出：准备 0/2")


class _FakeBlueprintTree:
    """多蓝图树高亮测试用的最小蓝图树（可被 ``bpy.data.node_groups.get`` 取到）。"""

    def __init__(self, name, nodes=(), bl_idname="SSMTBlueprintTreeType"):
        self.name = name
        self.bl_idname = bl_idname
        self.nodes = _FakeNodeCollection(nodes)


def _make_blueprint_node(name, bl_idname="", color=(0.5, 0.5, 0.5), **attrs):
    node = _FakeProgressNode(name, color=color)
    node.bl_idname = bl_idname
    for key, value in attrs.items():
        setattr(node, key, value)
    return node


class NestedBlueprintNodeHighlightTests(_BackgroundTestBase):
    """主进程节点高亮：``树名/节点名`` 键、嵌套蓝图递归、原色恢复。"""

    MAIN_TREE_NAME = "主蓝图"
    NESTED_TREE_NAME = "子蓝图"

    def setUp(self):
        super().setUp()
        self._highlight_enabled_backup = ui_func_export._BG_NODE_HIGHLIGHT_ENABLED
        self._highlight_interval_backup = ui_func_export._BG_HIGHLIGHT_MIN_INTERVAL
        ui_func_export._BG_NODE_HIGHLIGHT_ENABLED = True
        ui_func_export._BG_HIGHLIGHT_MIN_INTERVAL = 0.0
        self.addCleanup(
            setattr,
            ui_func_export,
            "_BG_NODE_HIGHLIGHT_ENABLED",
            self._highlight_enabled_backup,
        )
        self.addCleanup(
            setattr,
            ui_func_export,
            "_BG_HIGHLIGHT_MIN_INTERVAL",
            self._highlight_interval_backup,
        )

    @staticmethod
    def _operator():
        operator = ui_func_export.SSMTGenerateModBlueprint()
        operator._bg_node_colors = {}
        operator._bg_node_lookup = None
        return operator

    def _install_trees(self, *trees):
        mapping = {tree.name: tree for tree in trees}
        original_get = _FAKE_BPY.data.node_groups.get
        _FAKE_BPY.data.node_groups.get = lambda name: mapping.get(name)
        self.addCleanup(setattr, _FAKE_BPY.data.node_groups, "get", original_get)

    def _fixture(self):
        """主树 → 子蓝图 → 回指主树（循环）；另有 NONE / 缺失 / 非 SSMT 引用。"""
        self.nested_leaf = _make_blueprint_node(
            "嵌套物体信息", "SSMTNode_Object_Info", color=(0.11, 0.22, 0.33)
        )
        back_to_main = _make_blueprint_node(
            "回指主树",
            "SSMTNode_Blueprint_Nest",
            blueprint_name=self.MAIN_TREE_NAME,
        )
        self.nested_tree = _FakeBlueprintTree(
            self.NESTED_TREE_NAME, [self.nested_leaf, back_to_main]
        )

        self.main_leaf = _make_blueprint_node(
            "主树物体信息", "SSMTNode_Object_Info", color=(0.4, 0.5, 0.6)
        )
        nest_entry = _make_blueprint_node(
            "嵌套入口",
            "SSMTNode_Blueprint_Nest",
            blueprint_name=self.NESTED_TREE_NAME,
        )
        none_ref = _make_blueprint_node(
            "空引用", "SSMTNode_Blueprint_Nest", blueprint_name="NONE"
        )
        missing_ref = _make_blueprint_node(
            "缺失引用", "SSMTNode_Blueprint_Nest", blueprint_name="不存在的蓝图"
        )
        non_ssmt_ref = _make_blueprint_node(
            "引用普通树", "SSMTNode_Blueprint_Nest", blueprint_name="普通节点树"
        )
        self.main_tree = _FakeBlueprintTree(
            self.MAIN_TREE_NAME,
            [self.main_leaf, nest_entry, none_ref, missing_ref, non_ssmt_ref],
        )
        non_ssmt_tree = _FakeBlueprintTree(
            "普通节点树",
            [_make_blueprint_node("非SSMT节点", "SSMTNode_Object_Info")],
            bl_idname="ShaderNodeTree",
        )
        self._install_trees(self.main_tree, self.nested_tree, non_ssmt_tree)
        return self.main_tree

    def test_split_node_key_parses_tree_and_falls_back_to_main_tree(self):
        split = ui_func_export.SSMTGenerateModBlueprint._split_node_key
        self.assertEqual(split("树A/节点X", "主蓝图"), ("树A", "节点X"))
        # 旧 stage.json 的纯节点名：回退到主树
        self.assertEqual(split("节点X", "主蓝图"), ("主蓝图", "节点X"))
        self.assertEqual(split("", "主蓝图"), ("主蓝图", ""))

    def test_participant_collection_walks_nested_trees(self):
        self._fixture()
        keys = ui_func_export.SSMTGenerateModBlueprint._collect_participant_node_names(
            self.main_tree
        )

        self.assertIn("主蓝图/主树物体信息", keys)
        self.assertIn("子蓝图/嵌套物体信息", keys)
        self.assertIn("子蓝图/回指主树", keys)
        self.assertEqual(len(keys), len(set(keys)))  # 循环引用不重复
        self.assertNotIn("普通节点树/非SSMT节点", keys)

    def test_old_key_falls_back_to_main_tree(self):
        self._fixture()
        operator = self._operator()

        operator._sync_node_highlight(
            types.SimpleNamespace(
                tree_name=self.MAIN_TREE_NAME,
                stage="后处理",
                stage_nodes={"主树物体信息": "running"},
            )
        )

        self.assertEqual(tuple(self.main_leaf.color), (0.95, 0.72, 0.1))
        # 嵌套树里的同名键节点不会被旧键误伤（参与节点推断为 done 绿）
        self.assertEqual(tuple(self.nested_leaf.color), (0.22, 0.78, 0.3))

    def test_nested_node_highlighted_by_tree_prefixed_key_and_restored(self):
        self._fixture()
        operator = self._operator()

        operator._sync_node_highlight(
            types.SimpleNamespace(
                tree_name=self.MAIN_TREE_NAME,
                stage="后处理",
                stage_nodes={"子蓝图/嵌套物体信息": "running"},
            )
        )

        self.assertTrue(self.nested_leaf.use_custom_color)
        self.assertEqual(tuple(self.nested_leaf.color), (0.95, 0.72, 0.1))
        # 主树里没有这个节点，不会被错上成黄（参与节点推断为 done 绿）
        self.assertEqual(tuple(self.main_leaf.color), (0.22, 0.78, 0.3))

        operator._restore_node_colors()

        self.assertFalse(self.nested_leaf.use_custom_color)
        self.assertEqual(tuple(self.nested_leaf.color), (0.11, 0.22, 0.33))

    def test_nested_participants_follow_export_phases(self):
        """嵌套树里的参与节点同样 蓝灰 → 黄 → 绿；后处理节点由 worker 上报。"""
        self._fixture()
        post = _make_blueprint_node(
            "嵌套后处理", "SSMTNode_PostProcess_ShapeKey", color=(0.5, 0.5, 0.5)
        )
        self.nested_tree.nodes = _FakeNodeCollection([self.nested_leaf, post])
        operator = self._operator()

        def _sync(stage, stage_nodes):
            operator._sync_node_highlight(
                types.SimpleNamespace(
                    tree_name=self.MAIN_TREE_NAME,
                    stage=stage,
                    stage_nodes=dict(stage_nodes),
                )
            )

        _sync("加载工程", {})
        self.assertEqual(tuple(self.nested_leaf.color), (0.45, 0.52, 0.68))
        self.assertEqual(tuple(self.main_leaf.color), (0.45, 0.52, 0.68))
        self.assertFalse(post.use_custom_color)

        _sync("前处理", {})
        self.assertEqual(tuple(self.nested_leaf.color), (0.95, 0.72, 0.1))

        _sync("后处理", {"子蓝图/嵌套后处理": "running"})
        self.assertEqual(tuple(self.nested_leaf.color), (0.22, 0.78, 0.3))
        self.assertEqual(tuple(post.color), (0.95, 0.72, 0.1))

        operator._restore_node_colors()
        self.assertEqual(tuple(self.nested_leaf.color), (0.11, 0.22, 0.33))
        self.assertEqual(tuple(self.main_leaf.color), (0.4, 0.5, 0.6))
        self.assertEqual(tuple(post.color), (0.5, 0.5, 0.5))


class ProgressBridgeTests(_BackgroundTestBase):
    """``install_progress_bridge`` / ``uninstall_progress_bridge``。"""

    def test_timer_utils_stage_reported_and_restored(self):
        stage_path = os.path.join(self.temp_dir, "bridge_stage.json")
        timer_utils = sys.modules[f"{PKG}.utils.timer_utils"].TimerUtils
        original = timer_utils.start_stage

        self.assertTrue(export_background.install_progress_bridge(stage_path))
        try:
            timer_utils.start_stage("Direct-Preprocess")
            with open(stage_path, "r", encoding="utf-8") as file:
                stage = json.load(file)
            self.assertEqual(stage["stage"], "直出-执行前处理")
            self.assertIn("elapsed", stage)
        finally:
            export_background.uninstall_progress_bridge()

        self.assertIs(timer_utils.start_stage, original)

    def test_timer_utils_class_is_patched_as_classmethod(self):
        """真实 TimerUtils 是类（classmethod 描述符），patch 后仍按类方法调用。"""
        timer_module = sys.modules[f"{PKG}.utils.timer_utils"]
        original_timer_utils = timer_module.TimerUtils
        calls = []

        class _RealTimerUtils:
            @classmethod
            def start_stage(cls, stage_name):
                calls.append(stage_name)

            @classmethod
            def _display_stage_name(cls, stage_name):
                return {"Direct-Preprocess": "直出-执行前处理"}.get(stage_name, stage_name)

        timer_module.TimerUtils = _RealTimerUtils
        self.addCleanup(setattr, timer_module, "TimerUtils", original_timer_utils)

        stage_path = os.path.join(self.temp_dir, "bridge_class_stage.json")
        self.assertTrue(export_background.install_progress_bridge(stage_path))
        try:
            _RealTimerUtils.start_stage("Direct-Preprocess")
            with open(stage_path, "r", encoding="utf-8") as file:
                stage = json.load(file)
            self.assertEqual(stage["stage"], "直出-执行前处理")
        finally:
            export_background.uninstall_progress_bridge()

        self.assertEqual(calls, ["Direct-Preprocess"])
        # 恢复后原 classmethod 描述符仍在
        self.assertIsInstance(vars(_RealTimerUtils)["start_stage"], classmethod)
        _RealTimerUtils.start_stage("Direct-Preprocess")
        self.assertEqual(calls, ["Direct-Preprocess", "Direct-Preprocess"])

    def test_postprocess_nodes_reported_running_then_done_and_restored(self):
        stage_path = os.path.join(self.temp_dir, "bridge_model_stage.json")
        model_module = sys.modules[f"{PKG}.blueprint.model"]
        original_model = model_module.BluePrintModel
        model_module.BluePrintModel = _BridgeModel
        self.addCleanup(setattr, model_module, "BluePrintModel", original_model)
        self.addCleanup(export_background.uninstall_progress_bridge)

        executed = []

        class _Node:
            def __init__(self, name):
                self.name = name
                self.bl_idname = f"SSMTNode_PostProcess_{name}"

            def execute_postprocess(self, path):
                with open(stage_path, "r", encoding="utf-8") as file:
                    stage = json.load(file)
                executed.append(
                    (self.name, stage["node"], stage["nodes"].get(self.name), path)
                )

        _BridgeModel.postprocess_nodes = [_Node("节点A"), _Node("节点B")]
        _BridgeModel._object_name_mapping = {}
        original_method = _BridgeModel.__dict__["execute_postprocess_nodes"]
        model = _BridgeModel()

        self.assertTrue(export_background.install_progress_bridge(stage_path))
        model.execute_postprocess_nodes("out")
        export_background.uninstall_progress_bridge()

        self.assertEqual([item[0] for item in executed], ["节点A", "节点B"])
        for name, current_node, status, path in executed:
            self.assertEqual(current_node, name)  # 执行时该节点已标 running
            self.assertEqual(status, "running")
            self.assertEqual(path, "out")

        with open(stage_path, "r", encoding="utf-8") as file:
            stage = json.load(file)
        self.assertEqual(stage["nodes"], {"节点A": "done", "节点B": "done"})
        self.assertEqual(stage["node"], "")
        self.assertIs(_BridgeModel.__dict__["execute_postprocess_nodes"], original_method)


class _BridgeModel:
    """进度桥测试用的最小蓝图模型（有真实的逐节点后处理方法）。"""

    _object_name_mapping = {}
    postprocess_nodes = []

    def execute_postprocess_nodes(self, mod_export_path):
        for node in self.postprocess_nodes:
            node.execute_postprocess(mod_export_path)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 8. 后台 worker 禁用嵌套并行
# ---------------------------------------------------------------------------


class NestedParallelGuardTests(_BackgroundTestBase):
    """后台 worker 的嵌套并行策略：默认沿用面板；HERTA_BG_PARALLEL_INSTANCES 可限流/禁用。"""

    def test_default_keeps_panel_settings(self):
        props = self.scene_properties
        props.enable_parallel_preprocess = True
        props.enable_parallel_export_rounds = True
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERTA_BG_PARALLEL_INSTANCES", None)
            export_background._configure_worker_parallel()
        self.assertTrue(props.enable_parallel_preprocess)
        self.assertTrue(props.enable_parallel_export_rounds)

    def test_env_zero_disables_both(self):
        props = self.scene_properties
        props.enable_parallel_preprocess = True
        props.enable_parallel_export_rounds = True
        with mock.patch.dict(os.environ, {"HERTA_BG_PARALLEL_INSTANCES": "0"}):
            export_background._configure_worker_parallel()
        self.assertFalse(props.enable_parallel_preprocess)
        self.assertFalse(props.enable_parallel_export_rounds)

    def test_env_positive_limits_instances(self):
        props = self.scene_properties
        props.enable_parallel_preprocess = False
        props.enable_parallel_export_rounds = True
        props.parallel_preprocess_instances = 8
        with mock.patch.dict(os.environ, {"HERTA_BG_PARALLEL_INSTANCES": "2"}):
            export_background._configure_worker_parallel()
        self.assertTrue(props.enable_parallel_preprocess)
        self.assertEqual(2, props.parallel_preprocess_instances)
        self.assertFalse(props.enable_parallel_export_rounds)

    def test_configure_parallel_is_silent_without_scene(self):
        old_scene = _FAKE_BPY.context.scene
        _FAKE_BPY.context.scene = types.SimpleNamespace(global_properties=None)
        try:
            export_background._configure_worker_parallel()  # 不抛即通过
        finally:
            _FAKE_BPY.context.scene = old_scene
