"""后台导出（``blueprint/export_background.py``）单元测试 —— fake bpy，不启动真实 Blender。

覆盖：

1. manifest 写入/读取（含全部必需字段）；
2. stage.json 三种状态解析（running / success / error）与进度文本；
3. ``BackgroundExportSession`` 用 fake Popen 测 start / poll 状态机 / cancel / cleanup；
4. worker 侧 ``_run_worker`` 的轮次串行、后处理、result.json 成功与失败；
5. ``common/global_properties.py`` 的开关声明默认 False；
6. ``SSMTGenerateModBlueprint.invoke`` 开关关闭时走原 execute、开关开启时进 modal。

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
_FAKE_BPY.types = types.SimpleNamespace(Operator=_FakeOperatorBase, PropertyGroup=object)
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

    def save_to_text_editor(self, *_args, **_kwargs):
        return ""

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
# 真实模块：后台导出
export_background = _load_real_module(
    f"{PKG}.blueprint.export_background", Path("blueprint") / "export_background.py"
)
# 真实模块：导出算子（它会 import 上面两个真实模块）
ui_func_export = _load_real_module(
    f"{PKG}.ui.ui_func_export", Path("ui") / "ui_func_export.py"
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
            background_export_enabled=False,
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
    def test_start_launches_detached_process_and_writes_manifest(self):
        session = self.make_session()

        self.assertTrue(session.start())
        self.assertEqual(session.status, "running")
        self.assertEqual(len(_FakeProcess.instances), 1)

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
# 4. worker 全链路（fake ExportRoundExecutor / fake 蓝图模型）
# ---------------------------------------------------------------------------


class _FakeBlueprintModel:
    def __init__(self):
        self.postprocess_paths = []

    def execute_postprocess_nodes(self, output_dir):
        self.postprocess_paths.append(output_dir)


class WorkerTests(_BackgroundTestBase):
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

    def test_worker_direct_export_uses_manifest_total_steps(self):
        """直出模式：total_steps 以 manifest 为准，阶段/节点完成逐步推进，收尾补满。"""
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
            total_steps=12,
        )
        _FAKE_BPY.data.node_groups.get = (
            lambda name: types.SimpleNamespace(name=name) if name == "测试蓝图" else None
        )

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

        direct_module.execute_direct_export = _fake_direct
        self.addCleanup(setattr, direct_module, "execute_direct_export", original_direct)

        export_background._run_worker(manifest["manifest_path"])

        with open(manifest["result_path"], "r", encoding="utf-8") as file:
            result = json.load(file)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(observed, [0, 1, 2])

        with open(manifest["stage_path"], "r", encoding="utf-8") as file:
            stage = json.load(file)
        self.assertEqual(stage["stage"], "完成")
        self.assertEqual((stage["done"], stage["total"]), (12, 12))

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

    def test_worker_backs_up_config_tables_and_records_ini_warnings(self):
        """普通模式：导出前备份旧配置表，成功后把 INI 自检告警写进 result。"""
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
        self.assertEqual(
            calls,
            [
                ("find", output_dir),
                ("backup", output_dir, [os.path.join(output_dir, "角色.ini")]),
            ],
        )

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


# ---------------------------------------------------------------------------
# 5/6. 开关默认值与算子分流
# ---------------------------------------------------------------------------


class BackgroundToggleTests(_BackgroundTestBase):
    def test_property_defaults_to_true(self):
        """后台导出默认开启（用户要求：点导出就不阻塞界面；取消勾选才走同步导出）。"""
        declaration = global_properties_module.GlobalProterties.__annotations__[
            "background_export_enabled"
        ]
        self.assertIs(declaration["default"], True)
        self.assertEqual(declaration["name"], "后台导出（不阻塞界面）")

        # 读取入口按场景属性返回真假
        self.scene_properties.background_export_enabled = False
        self.assertFalse(global_properties_module.GlobalProterties.is_background_export_enabled())
        self.scene_properties.background_export_enabled = True
        self.assertTrue(global_properties_module.GlobalProterties.is_background_export_enabled())


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
        _RecordingSession.instances.append(self)

    def start(self):
        self.started = True
        return True

    def poll(self):
        if len(self.poll_results) > 1:
            return self.poll_results.pop(0)
        return self.poll_results[0]

    def progress_text(self):
        return "后台导出：准备 0/2"

    def success_summary(self):
        return "后台导出完成，共 2 轮"

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

    def _make_mesh(self, name, pointer=1):
        return types.SimpleNamespace(type="MESH", name=name, as_pointer=lambda: pointer)

    def test_invoke_with_toggle_off_runs_original_execute(self):
        self.scene_properties.background_export_enabled = False
        operator = self._make_operator(None)
        executed = []
        operator.execute = lambda context: executed.append(context) or {'FINISHED'}

        result = operator.invoke(None, None)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(executed, [None])
        self.assertEqual(_RecordingSession.instances, [])
        self.assertEqual(_FAKE_WINDOW_MANAGER.timers, [])
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [])

    def test_quick_invoke_with_toggle_off_runs_sync_execute(self):
        self.scene_properties.background_export_enabled = False
        operator = self._make_quick_operator()
        executed = []
        operator.execute = lambda context: executed.append(context) or {'FINISHED'}

        result = operator.invoke(None, None)

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(executed, [None])
        self.assertEqual(_RecordingSession.instances, [])
        self.assertEqual(_FAKE_WINDOW_MANAGER.timers, [])

    def test_quick_invoke_with_toggle_on_starts_background_session(self):
        self.scene_properties.background_export_enabled = True
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
        self.assertTrue(session.kwargs["quick_export"])
        self.assertEqual(session.kwargs["object_names"], ["M1"])
        # 快速导出步骤口径：轮次 1 + 后处理节点 0 + 收尾 1
        self.assertEqual(session.kwargs["total_steps"], 2)
        self.assertEqual(session.kwargs["round_plans"][0]["buffer_folder_name"], "Meshes")
        self.assertEqual(_FAKE_WINDOW_MANAGER.timers[0]["interval"], 0.5)
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [operator])

    def test_invoke_is_blocked_while_background_session_active(self):
        """防重入：已有活动会话时两个导出入口都返回 CANCELLED，且不新建会话。"""
        self.scene_properties.background_export_enabled = True
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
        self.scene_properties.background_export_enabled = True
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
        self.assertEqual(session.kwargs["tree_name"], "测试蓝图")
        self.assertEqual(session.kwargs["output_dir"], _FakeGlobalConfig.output_dir)
        self.assertEqual(session.kwargs["round_plans"][0]["buffer_folder_name"], "Meshes")
        self.assertEqual(len(_FAKE_WINDOW_MANAGER.timers), 1)
        self.assertEqual(_FAKE_WINDOW_MANAGER.timers[0]["interval"], 0.5)
        self.assertEqual(_FAKE_WINDOW_MANAGER.modal_handlers, [operator])
        self.assertEqual(_FAKE_AREAS[0].header, "后台导出：准备 0/2")

        # 轮询：成功 → FINISHED，清 header、停 timer、cleanup
        session.poll_results = ["running", "success"]
        self.assertEqual(operator.modal(context, types.SimpleNamespace(type='TIMER')), {'RUNNING_MODAL'})
        self.assertEqual(operator.modal(context, types.SimpleNamespace(type='TIMER')), {'FINISHED'})
        self.assertEqual("", _FAKE_AREAS[0].header)
        self.assertEqual(len(_FAKE_WINDOW_MANAGER.removed_timers), 1)
        self.assertTrue(session.cleaned)
        self.assertEqual(operator.reports[-1][0], {'INFO'})

    def test_modal_failure_updates_header_without_error_report(self):
        """失败收尾不再弹 Blender 原生 Report：header 留摘要，report 不出现 ERROR。"""
        self.scene_properties.background_export_enabled = True
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
        self.scene_properties.background_export_enabled = True
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
        """普通模式：轮次数 + 后处理节点数 + 1(收尾)。"""
        _FakeBlueprintExportHelper.postprocess_nodes = [object(), object()]
        operator = self._make_operator(None)

        session = operator._create_background_session(_FakeExportTree())

        self.assertFalse(session.kwargs["direct_export"])
        self.assertEqual(session.kwargs["total_steps"], 1 + 2 + 1)

    def test_background_session_total_steps_direct_mode(self):
        """直出模式：7 个基础阶段 + 形态键生成 + 后处理节点 + 收尾。"""
        ui_func_export.has_direct_export_mode = lambda _tree: True
        original_shapekey = ui_func_export.collect_direct_shapekey_nodes
        original_multifile = ui_func_export.collect_direct_multifile_nodes
        ui_func_export.collect_direct_shapekey_nodes = lambda _tree: [object()]
        ui_func_export.collect_direct_multifile_nodes = lambda _tree: []
        self.addCleanup(
            setattr, ui_func_export, "collect_direct_shapekey_nodes", original_shapekey
        )
        self.addCleanup(
            setattr, ui_func_export, "collect_direct_multifile_nodes", original_multifile
        )
        _FakeBlueprintExportHelper.postprocess_nodes = [
            types.SimpleNamespace(
                bl_idname="SSMTNode_PostProcess_ShapeKey", direct_export_mode=True
            ),
            types.SimpleNamespace(
                bl_idname="SSMTNode_PostProcess_MultiFile", direct_export_mode=True
            ),
        ]
        operator = self._make_operator(None)

        session = operator._create_background_session(_FakeExportTree())

        self.assertTrue(session.kwargs["direct_export"])
        # 7 基础 + 形态键生成 1 + 多文件生成 0 + 附加轮次 0 + 2 节点 + 收尾 1
        self.assertEqual(session.kwargs["total_steps"], 7 + 1 + 2 + 1)

    def test_estimate_total_steps_includes_standard_shapekey_round_stage(self):
        """有未启用直出的形态键后处理节点时，直出还要补跑传统附加轮次（多 1 步）。"""
        original_shapekey = ui_func_export.collect_direct_shapekey_nodes
        original_multifile = ui_func_export.collect_direct_multifile_nodes
        ui_func_export.collect_direct_shapekey_nodes = lambda _tree: [object()]
        ui_func_export.collect_direct_multifile_nodes = lambda _tree: []
        self.addCleanup(
            setattr, ui_func_export, "collect_direct_shapekey_nodes", original_shapekey
        )
        self.addCleanup(
            setattr, ui_func_export, "collect_direct_multifile_nodes", original_multifile
        )
        _FakeBlueprintExportHelper.postprocess_nodes = [
            types.SimpleNamespace(
                bl_idname="SSMTNode_PostProcess_ShapeKey", direct_export_mode=False
            )
        ]
        operator = self._make_operator(None)

        total = operator._estimate_background_total_steps(_FakeExportTree(), [], True)

        # 7 基础 + 形态键生成 1 + 附加轮次 1 + 节点 1 + 收尾 1
        self.assertEqual(total, 7 + 1 + 1 + 1 + 1)

    # -- 步骤进度条（window_manager.progress_*） ---------------------------

    def test_modal_drives_native_progress_bar(self):
        self.scene_properties.background_export_enabled = True
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
            [("begin", 0, 5), ("update", 1)],
        )

        session.done_steps = 5
        result = operator.modal(context, types.SimpleNamespace(type='TIMER'))

        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(
            _FAKE_WINDOW_MANAGER.progress_calls,
            [("begin", 0, 5), ("update", 1), ("update", 5), ("end",)],
        )

    def test_modal_progress_api_failure_falls_back_to_header_bar(self):
        """progress_* 抛异常时静默，并在 header 上退回文本进度条。"""
        self.scene_properties.background_export_enabled = True
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
        self.assertIn("[█████", _FAKE_AREAS[0].header)
        self.assertIn("25%", _FAKE_AREAS[0].header)

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
        self.assertEqual(stage["total"], 3)
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

    def test_complete_step_is_capped_at_total(self):
        stage_path = self._stage_path("step_cap.json")
        export_progress.set_stage_file(stage_path)
        self.addCleanup(export_progress.clear_stage_file)
        export_progress.begin_steps(2)
        export_progress.complete_step()
        export_progress.complete_step()
        export_progress.complete_step()
        self.assertEqual(export_progress.step_done(), 2)

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
        """第一个阶段不推进；第二个阶段推进上一个；同名/子阶段不推进。"""
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

        timer_utils.start_stage("Direct-CollectObjects")
        self.assertEqual(export_progress.step_done(), 2)  # 前一步是子阶段：不推进
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
            export_background.format_progress_bar(0, 5), "[░░░░░░░░░░░░░░░░░░░░] 0%"
        )
        self.assertEqual(
            export_background.format_progress_bar(5, 5), "[████████████████████] 100%"
        )
        self.assertEqual(export_background.format_progress_bar(3, 0), "")


class NodeHighlightTests(_BackgroundTestBase):
    """``SSMTGenerateModBlueprint`` 的节点高亮：应用 / 恢复 / 每个 tick 同步。"""

    TREE_NAME = "测试蓝图"

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

    def test_unknown_node_and_pending_status_are_ignored(self):
        node = _FakeProgressNode("已知节点", color=(0.3, 0.3, 0.3))
        self._install_tree(node)
        operator = self._operator()

        operator._sync_node_highlight(
            types.SimpleNamespace(
                tree_name=self.TREE_NAME,
                stage_nodes={"已知节点": "pending", "不存在节点": "running"},
            )
        )

        self.assertFalse(node.use_custom_color)
        self.assertEqual(tuple(node.color), (0.3, 0.3, 0.3))

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
    """worker 里不能再起并行前处理/并行轮次子进程（嵌套并行会争抢资源并失败）。"""

    def test_disable_nested_parallel_turns_off_both_switches(self):
        props = self.scene_properties
        props.enable_parallel_preprocess = True
        props.enable_parallel_export_rounds = True

        export_background._disable_nested_parallel()

        self.assertFalse(props.enable_parallel_preprocess)
        self.assertFalse(props.enable_parallel_export_rounds)

    def test_disable_nested_parallel_is_silent_without_scene(self):
        old_scene = _FAKE_BPY.context.scene
        _FAKE_BPY.context.scene = types.SimpleNamespace(global_properties=None)
        try:
            export_background._disable_nested_parallel()  # 不抛即通过
        finally:
            _FAKE_BPY.context.scene = old_scene