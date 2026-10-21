"""后台导出：快照 + 干净后台 Blender 实例跑完整导出 + 主进程 modal 轮询进度。

照 BakeWrangler 的模式（`nodes/node_tree.py` 的 save_as_mainfile(copy=True) →
Popen(blender, 快照, --background, --factory-startup, ...) → event_timer 轮询）：

1. 主进程调用 ``bpy.ops.wm.save_as_mainfile(copy=True)`` 保存**副本工程**，
   不覆盖用户正在编辑的文件；
2. ``subprocess.Popen([blender, 快照, "--background", "--factory-startup",
   "--addons", 插件, "--python-expr", ...])``：``--factory-startup`` 屏蔽用户的其它
   插件，只加载本插件，避免别的插件在后台实例里做副作用；
3. 主进程用 modal 定时器（0.5s）轮询，跨进程通信只走会话目录里的普通文件
   （manifest / stage / result / log），主界面全程不阻塞。

worker 侧：在干净实例里打开快照，按 manifest 里主进程算好的轮次计划**串行**跑完整
导出（``ExportRoundExecutor.execute_round``，子进程里不再嵌套并行前处理），最后执行
后处理节点，结果写回 ``result.json``。

与 ``export_parallel.py`` 的区别：那边的子进程只跑「多轮导出里的中间轮」，本模块的
子进程跑**整条导出链路**（全部轮次 + 后处理），主进程只负责快照、清单与进度显示。
"""

import bpy
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback

from ..common.global_config import GlobalConfig
from ..utils.log_utils import LOG
from ..utils.shapekey_utils import ShapeKeyUtils
from . import export_progress
from .export_parallel import ExportRoundExecutor
from .export_step_plan import build_step_plan
from .preprocess_parallel import ParallelPreprocessCoordinator


SESSION_VERSION = 1

MANIFEST_FILE_NAME = "manifest.json"
STAGE_FILE_NAME = "stage.json"
RESULT_FILE_NAME = "result.json"
LOG_FILE_NAME = "worker.log"
SNAPSHOT_FILE_NAME = "snapshot.blend"

#: 子进程退出后主进程最多等这么久（秒），超时则 kill
KILL_GRACE_SECONDS = 2.0
#: cancel() 里轮询 terminate 是否生效的间隔（秒）
TERMINATE_POLL_INTERVAL = 0.05

#: 已有后台导出在运行时，各导出入口的统一提示
BACKGROUND_BUSY_MESSAGE = "已有后台导出在运行，请等它结束或按 ESC 取消"

#: 快速局部导出的单轮轮次计划（同步路径与后台 worker 共用，口径完全一致）
QUICK_EXPORT_ROUND_PLAN = {
    "round_index": 1,
    "phase": "single",
    "description": "快速局部导出",
    "buffer_folder_name": "Meshes",
    "export_index": 1,
    "generate_ini": True,
    "generate_classification_report": False,
    "shapekey_mode": "unchanged",
    "shapekey_slot_index": None,
}


def get_addon_name() -> str:
    """取插件包名（``TheHerta4-main.blueprint`` → ``TheHerta4-main``）。

    安装在 ``scripts/addons`` 下的传统插件目录名可能带连字符（本机就是
    ``TheHerta4-main``），所以 worker 侧导入必须用 ``importlib.import_module``，
    不能用 ``import xxx`` 语句（连字符名不是合法标识符）。
    """
    package = str(__package__ or "")
    return package.split(".")[0] or "TheHerta4"


# ---------------------------------------------------------------------------
# 文件通信（manifest / stage / result / log）
# ---------------------------------------------------------------------------


def _read_json(file_path: str):
    """读 JSON；文件不存在 / 正在被写 / 内容损坏时返回 None（不抛异常）。"""
    if not file_path:
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return None


def _write_json(file_path: str, payload) -> None:
    """原子写 JSON：先写 ``.tmp`` 再 os.replace，避免主进程读到半截文件。"""
    if not file_path:
        return

    temp_path = file_path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, file_path)
        return
    except OSError:
        # 原子替换失败（杀软 / 文件短暂被占）时退回直接写：进度文件不值得报错
        pass

    try:
        with open(file_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
    except OSError:
        pass
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def _write_stage(stage_path: str, stage: str, done: int, total: int, message: str = "") -> None:
    """写当前阶段快照：``{"stage": ..., "done": n, "total": N, "message": ...}``。

    已有的固定步骤清单（``plan`` / ``plan_total``，由 ``export_progress`` 写入）
    原样保留 —— 粗粒度阶段快照不能把调试用的清单冲掉。
    """
    payload = {
        "stage": str(stage),
        "done": int(done),
        "total": int(total),
        "message": str(message or ""),
    }
    try:
        existing = _read_json(stage_path)
        if isinstance(existing, dict) and existing.get("plan"):
            payload["plan"] = list(existing.get("plan") or [])
            payload["plan_total"] = int(
                existing.get("plan_total") or len(payload["plan"])
            )
    except Exception:
        pass
    _write_json(stage_path, payload)


# ---------------------------------------------------------------------------
# worker 进度桥（节点级进度）
# ---------------------------------------------------------------------------

#: 进度桥状态：保存被替换的原引用，卸载时原样恢复
_PROGRESS_BRIDGE = {
    "installed": False,
    "timer_utils": None,
    "timer_had_own": False,
    "timer_original_attr": None,
    "model_cls": None,
    "model_had_own": False,
    "model_original_attr": None,
}


def _progress_display_stage_name(timer_utils, stage_name: str) -> str:
    """把 ``TimerUtils`` 的英文阶段名翻成界面用的中文名；没有映射就原样返回。"""
    try:
        display = getattr(timer_utils, "_display_stage_name", None)
        if callable(display):
            return str(display(stage_name) or stage_name)
    except Exception:
        pass
    return str(stage_name or "")


def install_progress_bridge(stage_path: str) -> bool:
    """给 worker 进程装节点级进度上报。

    替换两个统一入口（原引用全部保存，``uninstall_progress_bridge`` 恢复）：

    - ``TimerUtils.start_stage``：所有导出阶段（直出 / 前处理 / 形态键生成…）
      的统一入口 → 先调原方法，再把中文阶段名写进 stage.json；
    - ``BluePrintModel.execute_postprocess_nodes``：普通模式的逐节点后处理 →
      每个节点执行前上报 running、执行后上报 done，其余行为（clear_cache /
      名称映射 / 异常包装）与原来完全一致。

    直出模式的节点循环不走这个方法，由 ``direct_export.py`` 自己上报。
    """
    if _PROGRESS_BRIDGE.get("installed"):
        return True

    try:
        from ..utils.log_utils import LOG as bridge_log
        from ..utils.timer_utils import TimerUtils
        from .model import BluePrintModel
    except Exception:
        return False

    export_progress.set_stage_file(stage_path)

    timer_utils = TimerUtils
    original_start_stage = getattr(timer_utils, "start_stage", None)
    if callable(original_start_stage):
        def _start_stage_and_report(stage_name, _original=original_start_stage):
            result = _original(stage_name)
            try:
                # advance_stage：推进上一个阶段 + 记录新阶段；Preprocess-* 子阶段只更新显示名
                export_progress.advance_stage(
                    _progress_display_stage_name(timer_utils, stage_name)
                )
            except Exception:
                pass
            return result

        # TimerUtils 是类（classmethod）；测试里的打桩对象是命名空间实例（普通函数）
        if isinstance(timer_utils, type):
            def _start_stage(cls, stage_name, _report=_start_stage_and_report):
                return _report(stage_name)

            patched_start_stage = classmethod(_start_stage)
        else:
            def _start_stage(stage_name, _report=_start_stage_and_report):
                return _report(stage_name)

            patched_start_stage = _start_stage

        _PROGRESS_BRIDGE["timer_utils"] = timer_utils
        _PROGRESS_BRIDGE["timer_had_own"] = "start_stage" in vars(timer_utils)
        _PROGRESS_BRIDGE["timer_original_attr"] = vars(timer_utils).get("start_stage")
        try:
            setattr(timer_utils, "start_stage", patched_start_stage)
        except Exception:
            pass

    original_postprocess = getattr(BluePrintModel, "execute_postprocess_nodes", None)
    if callable(original_postprocess):
        def _execute_postprocess_nodes(self, mod_export_path, _model_cls=BluePrintModel, _log=bridge_log):
            # 与 model.py::execute_postprocess_nodes 原逻辑一致，只在节点循环里加上报
            postprocess_nodes = list(getattr(self, "postprocess_nodes", None) or [])
            if not postprocess_nodes:
                return

            for pp_node in postprocess_nodes:
                node_class = type(pp_node)
                clear_cache = getattr(node_class, 'clear_cache', None)
                if clear_cache and callable(clear_cache):
                    try:
                        clear_cache()
                    except Exception:
                        pass

            name_mapping = dict(getattr(_model_cls, "_object_name_mapping", None) or {})
            if name_mapping:
                _log.info(f"📋 传递名称映射到后处理节点: {len(name_mapping)} 条规则")
                for pp_node in postprocess_nodes:
                    if hasattr(pp_node, 'apply_name_mapping'):
                        try:
                            pp_node.apply_name_mapping(name_mapping)
                        except Exception as e:
                            _log.warning(f"   ⚠️ 后处理节点 '{pp_node.name}' 应用名称映射失败: {e}")

            _log.info(f"🔧 后处理节点开始执行: {len(postprocess_nodes)} 个节点")

            export_progress.report_nodes(
                [str(getattr(node, "name", "") or "") for node in postprocess_nodes]
            )
            for pp_node in postprocess_nodes:
                node_name = str(getattr(pp_node, "name", "") or "")
                try:
                    export_progress.report_node(node_name, export_progress.STATE_RUNNING)
                except Exception:
                    pass

                try:
                    if hasattr(pp_node, 'execute_postprocess'):
                        pp_node.execute_postprocess(mod_export_path)
                    else:
                        _log.warning(f"   ⚠️ 后处理节点缺少 execute_postprocess 方法: {pp_node.bl_idname}")
                except Exception as e:
                    # LOG.error 会立刻抛出 Fatal，导致下面带节点上下文的异常包装不可达。
                    _log.warning(f"   ❌ 后处理节点执行失败: {pp_node.bl_idname} ({pp_node.name}): {e}")
                    raise RuntimeError(
                        f"后处理节点 '{pp_node.name}' 执行失败: {e}"
                    ) from e
                else:
                    try:
                        export_progress.report_node(node_name, export_progress.STATE_DONE)
                    except Exception:
                        pass

            _log.info(f"   ✅ 后处理节点执行完成: {len(postprocess_nodes)} 个节点")

        _PROGRESS_BRIDGE["model_cls"] = BluePrintModel
        _PROGRESS_BRIDGE["model_had_own"] = "execute_postprocess_nodes" in vars(BluePrintModel)
        _PROGRESS_BRIDGE["model_original_attr"] = vars(BluePrintModel).get("execute_postprocess_nodes")
        try:
            setattr(BluePrintModel, "execute_postprocess_nodes", _execute_postprocess_nodes)
        except Exception:
            pass

    _PROGRESS_BRIDGE["installed"] = True
    return True


def uninstall_progress_bridge() -> None:
    """恢复被替换的导出入口；重复调用安全。"""
    timer_utils = _PROGRESS_BRIDGE.get("timer_utils")
    if timer_utils is not None:
        try:
            if _PROGRESS_BRIDGE.get("timer_had_own"):
                setattr(
                    timer_utils,
                    "start_stage",
                    _PROGRESS_BRIDGE.get("timer_original_attr"),
                )
            else:
                delattr(timer_utils, "start_stage")
        except Exception:
            pass

    model_cls = _PROGRESS_BRIDGE.get("model_cls")
    if model_cls is not None:
        try:
            if _PROGRESS_BRIDGE.get("model_had_own"):
                setattr(
                    model_cls,
                    "execute_postprocess_nodes",
                    _PROGRESS_BRIDGE.get("model_original_attr"),
                )
            else:
                delattr(model_cls, "execute_postprocess_nodes")
        except Exception:
            pass

    _PROGRESS_BRIDGE.clear()
    _PROGRESS_BRIDGE["installed"] = False
    try:
        export_progress.clear_stage_file()
    except Exception:
        pass


def save_export_snapshot(session_dir: str) -> str:
    """把当前工程另存为副本（不覆盖用户文件），返回快照路径。

    与 ``ParallelExportCoordinator`` 用同一个保存入口：形态键指针损坏时 Blender
    抛的自动修复警告由 ``ShapeKeyUtils`` 兜住，不会让后台导出直接失败。
    """
    snapshot_path = os.path.join(session_dir, SNAPSHOT_FILE_NAME)
    ShapeKeyUtils.save_as_mainfile_with_shape_key_recovery(
        filepath=snapshot_path,
        copy=True,
        check_existing=False,
    )
    return snapshot_path


# ---------------------------------------------------------------------------
# 快速局部导出：临时蓝图树（同步路径与后台 worker 共用）
# ---------------------------------------------------------------------------


def safe_object_name(obj) -> str:
    """取物体名；失效引用 / 非物体对象返回空串（不抛异常）。"""
    if obj is None:
        return ""
    try:
        return str(obj.name or "")
    except (AttributeError, ReferenceError):
        return ""


def build_quick_export_temp_tree(objects):
    """按物体列表建一棵临时蓝图树（Object Info → Object Group → Output）。

    同步快速导出与后台 worker 共用同一实现：worker 在快照工程里按
    ``manifest["object_names"]`` 找回物体后调用它，节点连接方式与同步路径
    完全一致。调用方负责在同步路径结束后删除这棵树（worker 进程退出即回收）。
    """
    tree_name = f"SSMT_QuickExport_{len(bpy.data.node_groups) + 1}"
    tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')

    group_node = tree.nodes.new('SSMTNode_Object_Group')
    group_node.label = "Quick Export"
    group_node.location = (300, 0)

    output_node = tree.nodes.new('SSMTNode_Result_Output')
    output_node.label = "Quick Export"
    output_node.location = (650, 0)
    tree.links.new(group_node.outputs[0], output_node.inputs[0])

    current_y = 0
    object_count = 0
    for obj in objects:
        obj_name = safe_object_name(obj)
        if not obj_name:
            continue
        try:
            object_id = str(obj.as_pointer())
        except (AttributeError, ReferenceError):
            continue

        node = tree.nodes.new('SSMTNode_Object_Info')
        node.object_name = obj_name
        node.object_id = object_id
        node.label = obj_name
        node.location = (0, current_y)
        current_y -= 180
        object_count += 1

        if group_node.inputs[-1].is_linked:
            group_node.inputs.new('SSMTSocketObject', f"Input {len(group_node.inputs) + 1}")

        tree.links.new(node.outputs[0], group_node.inputs[-1])

    if object_count == 0:
        raise ValueError("快速局部导出未能建立有效的物体节点")

    group_node.location = (300, max(0, (object_count - 1) * -90))
    output_node.location = (650, group_node.location.y)
    return tree


def build_manifest(
    session_dir: str,
    snapshot_path: str,
    tree_name: str,
    output_dir: str,
    round_plans: list,
    logic_name: str = "",
    workspace_name: str = "",
    has_shapekey_export: bool = False,
    max_shapekey_slot: int = 0,
    has_multi_file_export: bool = False,
    max_multi_file_count: int = 0,
    direct_export: bool = False,
    total_steps: int = 0,
    quick_export: bool = False,
    object_names: list = None,
    preprocess_cache_dir: str = "",
) -> dict:
    """写 ``manifest.json`` 并返回清单内容。

    清单是主进程 → worker 的唯一任务输入：worker 不重新算轮次计划、不重新解析
    输出目录，一切都以主进程（能看到当前蓝图 / 全局设置）算好的值为准。
    ``direct_export`` 为真时 worker 走直出导出（``execute_direct_export``），
    不使用 ``round_plans``。
    ``quick_export`` 为真时 worker 按 ``object_names`` 在快照里重建临时蓝图树，
    走单轮导出 + 后处理（快速局部导出的后台分支）。
    ``total_steps`` 是进度条的**总步骤数**（阶段 + 后处理节点 + 收尾）。worker
    会优先用 ``export_step_plan.build_step_plan`` 在同一棵蓝图树上重算固定清单；
    只有清单算不出来时才用这个字段兜底（旧清单 / 拿不到树的场景）。
    """
    stage_path = os.path.join(session_dir, STAGE_FILE_NAME)
    result_path = os.path.join(session_dir, RESULT_FILE_NAME)
    log_path = os.path.join(session_dir, LOG_FILE_NAME)
    manifest_path = os.path.join(session_dir, MANIFEST_FILE_NAME)

    manifest = {
        "version": SESSION_VERSION,
        "snapshot_path": str(snapshot_path or ""),
        "tree_name": str(tree_name or ""),
        "output_dir": str(output_dir or ""),
        "logic_name": str(logic_name or ""),
        "workspace_name": str(workspace_name or ""),
        "round_plans": [dict(round_plan) for round_plan in (round_plans or [])],
        "has_shapekey_export": bool(has_shapekey_export),
        "max_shapekey_slot": int(max_shapekey_slot or 0),
        "has_multi_file_export": bool(has_multi_file_export),
        "max_multi_file_count": int(max_multi_file_count or 0),
        "direct_export": bool(direct_export),
        "total_steps": max(0, int(total_steps or 0)),
        "quick_export": bool(quick_export),
        "object_names": [str(name) for name in (object_names or []) if str(name)],
        "preprocess_cache_dir": str(preprocess_cache_dir or ""),
        "stage_path": stage_path,
        "result_path": result_path,
        "log_path": log_path,
        "manifest_path": manifest_path,
    }

    _write_json(manifest_path, manifest)
    return manifest


# ---------------------------------------------------------------------------
# 子进程命令
# ---------------------------------------------------------------------------


def resolve_blender_executable() -> str:
    """取子进程要用的 blender 可执行文件。

    优先复用「并行处理」里配置的路径（存在才用），否则用当前实例自己的路径。
    """
    try:
        configured = ParallelPreprocessCoordinator.get_effective_blender_executable()
    except Exception:
        configured = ""
    if configured and os.path.isfile(configured):
        return configured
    try:
        return str(bpy.app.binary_path or "")
    except Exception:
        return ""


def build_worker_command(manifest: dict, blender_executable: str = "", addon_name: str = "") -> list:
    """组装后台 worker 命令。

    参数顺序照 BakeWrangler：``blender <blend副本> --background --factory-startup
    --addons <插件> --python-expr <入口> -- --manifest <清单>``。
    """
    binary = str(blender_executable or resolve_blender_executable() or "")
    addon = str(addon_name or get_addon_name())
    module_name = f"{addon}.blueprint.export_background"

    # 目录名可能带连字符（本机是 TheHerta4-main），import 语句不合法，用 importlib
    python_expr = (
        "import importlib; "
        f"eb = importlib.import_module({module_name!r}); "
        "eb.run_worker_cli()"
    )

    return [
        binary,
        str(manifest.get("snapshot_path", "")),
        "--background",
        "--factory-startup",
        "--addons",
        addon,
        "--python-expr",
        python_expr,
        "--",
        "--manifest",
        str(manifest.get("manifest_path", "")),
    ]


# ---------------------------------------------------------------------------
# worker（在干净后台实例里执行）
# ---------------------------------------------------------------------------


def run_worker_cli() -> None:
    """子进程入口：解析 ``-- --manifest <path>``（与 export_parallel 同款）。"""
    manifest_path = _parse_manifest_argument(sys.argv)
    if not manifest_path:
        raise RuntimeError("未提供后台导出 manifest 路径")
    _run_worker(manifest_path)


def _parse_manifest_argument(argv: list) -> str:
    if "--" not in argv:
        return ""
    extra_args = argv[argv.index("--") + 1:]
    for index, value in enumerate(extra_args):
        if value == "--manifest" and index + 1 < len(extra_args):
            return extra_args[index + 1]
    return ""


def _apply_manifest_environment(manifest: dict) -> None:
    """把主进程算好的环境写进 worker 的全局配置。

    - ``logic_name`` / ``workspace_name``：快照里的设置本应与主进程一致，
      但从清单显式覆盖可以避免子进程读到「保存快照之后又被改过」的值；
    - ``output_dir``：主进程解析输出目录时依赖「本次导出蓝图」指针，子进程里
      没有这个指针，若不覆盖会退回场景全局设置，产物落到别的目录。
    """
    logic_name = str(manifest.get("logic_name") or "")
    if logic_name:
        GlobalConfig.logic_name = logic_name

    workspace_name = str(manifest.get("workspace_name") or "")
    if workspace_name:
        GlobalConfig.workspacename = workspace_name

    output_dir = str(manifest.get("output_dir") or "")
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        # 子进程里所有路径解析都以主进程算好的目录为准
        GlobalConfig.path_generate_mod_folder = classmethod(
            lambda cls, _output_dir=output_dir: _output_dir
        )

    # 前处理缓存目录只能由主进程算：worker 的 bpy.data.filepath 指向临时快照，
    # 用 PreProcessCache.get_cache_dir() 会落到快照旁边的空目录，每次会话 100% 冷启动。
    cache_dir = str(manifest.get("preprocess_cache_dir") or "")
    if cache_dir:
        try:
            from .preprocess_cache import PreProcessCache

            PreProcessCache.set_override_cache_dir(cache_dir)
        except Exception as error:
            LOG.warning(f"后台实例前处理缓存目录设置失败（继续导出）: {error}")


def _collect_ini_variable_warnings(output_dir: str) -> list:
    """校验产物 ini 的变量声明，返回告警行（失败返回空列表）。

    与主进程 ``_validate_generated_ini_variables`` 用同一套校验器；worker 把结果
    写进 ``result.json`` 的 ``warnings``，主进程在成功提示里展示摘要。
    直出不调用（与同步直出一致）。
    """
    if not output_dir:
        return []
    try:
        from ..common.ini_variable_validator import (
            find_primary_ini,
            format_findings,
            validate_ini_file,
        )

        workspace_name = ""
        try:
            workspace_name = str(GlobalConfig.get_workspace_name() or "")
        except Exception:
            workspace_name = ""

        ini_path = find_primary_ini(output_dir, workspace_name)
        if not ini_path:
            return []
        report = validate_ini_file(ini_path)
        if not report.get("undeclared") and not report.get("duplicate_globals"):
            return []
        return [str(line) for line in format_findings(report)]
    except Exception:
        return []


def _configure_worker_parallel() -> None:
    """后台 worker 里的「嵌套并行」策略。

    并行前处理不是线程池里跑 Python，而是**每个 job 起一个完整的 Blender 子进程**，
    各自加载整份工程快照做前处理。worker 本身已经是一个 Blender 进程，再叠加 N 个
    同规格子进程时，内存与磁盘 IO 会成倍上升（大工程每实例可占 1~3 GB）。

    默认在 worker 内关闭并行前处理 / 并行轮次；环境变量
    ``HERTA_BG_PARALLEL_INSTANCES`` 指定正整数时，保留并行前处理但把实例数限制到
    该值，并保留并行临时目录（便于排查子进程日志）。
    """
    limit_text = ""
    try:
        limit_text = str(os.environ.get("HERTA_BG_PARALLEL_INSTANCES", "") or "").strip()
    except Exception:
        limit_text = ""

    try:
        scene = getattr(bpy.context, "scene", None)
        if scene is None and len(bpy.data.scenes):
            scene = bpy.data.scenes[0]
        props = getattr(scene, "global_properties", None) if scene is not None else None
        if props is None:
            return

        if not limit_text:
            # 默认：沿用面板设置（并行前处理可用；两个子进程相关 bug 已修）
            try:
                LOG.info("后台实例：沿用面板的并行前处理设置")
            except Exception:
                pass
            return

        limit = int(limit_text) if limit_text.isdigit() else 0
        if limit <= 0:
            props.enable_parallel_preprocess = False
            props.enable_parallel_export_rounds = False
            try:
                LOG.info("后台实例：已禁用嵌套并行（并行前处理 / 并行轮次）")
            except Exception:
                pass
            return

        props.enable_parallel_preprocess = True
        props.parallel_preprocess_instances = limit
        try:
            props.parallel_preprocess_keep_temp_files = True
        except Exception:
            pass
        props.enable_parallel_export_rounds = False
        try:
            LOG.info(f"后台实例：并行前处理实例数限制为 {limit}（并行轮次关闭）")
        except Exception:
            pass
    except Exception as error:
        try:
            LOG.warning(f"后台实例配置嵌套并行失败（继续导出）: {error}")
        except Exception:
            pass


def _disable_nested_parallel() -> None:
    """兼容旧名：按默认策略配置 worker 内并行（默认沿用面板设置）。"""
    _configure_worker_parallel()


def _clear_export_section_registry() -> None:
    """worker 进程内清空 DrawIB→ini 段登记表（与主进程同步导出入口保持一致）。

    登记表由 ``common/export_section_registry.py`` 提供，可能尚未落地（精简加载 /
    测试桩），因此软导入：拿不到模块或清理失败都只留一条提示，绝不让后台导出失败。
    """
    try:
        from ..common import export_section_registry as _section_registry
    except Exception:
        return
    try:
        _section_registry.clear()
    except Exception as error:
        print(f"⚠️ 清理导出段登记表失败（已忽略）: {error}")


def _run_worker(manifest_path: str) -> None:
    """在干净实例里跑完整导出：全部轮次串行 → 后处理 → 写 result.json / log。

    进度条按**步骤数**显示：worker 在导出开始前用
    ``export_step_plan.build_step_plan`` 一次算出完整步骤清单（与主进程预估
    同一个函数），``total = len(清单)`` 整次导出固定不变；阶段切换 / 节点完成
    逐项推进 ``done``，收尾 ``finish_steps()`` 补满。
    """
    manifest = _read_json(manifest_path) or {}

    # worker 与主进程内存隔离：这里再清一次，保证后处理节点只看到本次导出的 ini 段。
    _clear_export_section_registry()

    stage_path = str(manifest.get("stage_path") or "")
    result_path = str(manifest.get("result_path") or "")
    log_path = str(manifest.get("log_path") or "")
    round_plans = list(manifest.get("round_plans") or [])
    quick_export = bool(manifest.get("quick_export"))
    object_names = [str(name) for name in (manifest.get("object_names") or []) if str(name)]
    # 步骤数以清单为准；清单算不出来时才退回 manifest 里的预算
    total_steps = max(0, int(manifest.get("total_steps") or 0))
    if total_steps <= 0 and round_plans:
        total_steps = len(round_plans) + 1

    result = {
        "status": "error",
        "error": "",
        "rounds": 0,
        "buffer_sizes": [],
    }
    buffer_sizes = []
    done_steps = 0

    try:
        LOG.start_collecting()
        GlobalConfig.read_from_main_json_ssmt4()
        _apply_manifest_environment(manifest)
        # 后台实例内配置嵌套并行（默认关闭；HERTA_BG_PARALLEL_INSTANCES 可限流开启）
        _configure_worker_parallel()
        output_dir = str(manifest.get("output_dir") or "") or GlobalConfig.path_generate_mod_folder()

        if quick_export:
            # 快速局部导出：按主进程记录的物体名在快照里找回网格，重建临时蓝图树
            objects = []
            for name in object_names:
                obj = bpy.data.objects.get(name)
                if obj is not None and getattr(obj, "type", "") == "MESH":
                    objects.append(obj)
            if not objects:
                raise RuntimeError("后台快速局部导出未找到选中的网格物体")
            tree = build_quick_export_temp_tree(objects)
        else:
            tree_name = str(manifest.get("tree_name") or "")
            tree = bpy.data.node_groups.get(tree_name)
            if tree is None:
                raise RuntimeError(f"未找到蓝图树: {tree_name}")

        # 步骤清单在**导出开始前一次算好**（与主进程预估同一个函数）：之后
        # total = len(清单) 固定不变，阶段/节点按清单推进，不再动态加步数。
        try:
            step_plan = build_step_plan(
                tree,
                direct_export=bool(manifest.get("direct_export")),
                has_shapekey_export=bool(manifest.get("has_shapekey_export")),
                max_shapekey_slot=int(manifest.get("max_shapekey_slot") or 0),
                has_multi_file_export=bool(manifest.get("has_multi_file_export")),
                round_plans=round_plans,
                quick_export=quick_export,
            )
        except Exception:
            step_plan = []
        if step_plan:
            total_steps = len(step_plan)

        # 进度桥：之后所有 TimerUtils 阶段与后处理节点都会写进 stage.json
        install_progress_bridge(stage_path)
        # 步骤计数必须在 install（set_stage_file 会重置状态）之后初始化
        export_progress.begin_steps(total_steps, plan=step_plan)

        _write_stage(
            stage_path,
            "准备",
            export_progress.step_done(),
            export_progress.step_total(),
            "已载入工程快照",
        )

        # 导出前同步：与主进程同步导出链路保持一致。失败只记警告，不阻断后台导出。
        # 快速局部导出走临时树（同步路径也不做这一步），跳过。
        if not quick_export:
            try:
                from .sync import refresh_blueprint_sync_state

                refresh_summary = refresh_blueprint_sync_state(
                    tree=tree,
                    context=bpy.context,
                    include_all_blueprints=True,
                )
                LOG.info(
                    f"🔄 后台实例导出前刷新同步状态: {refresh_summary['tree_count']} 个蓝图, "
                    f"{refresh_summary['updated_count']} 处节点引用更新"
                )
            except Exception as sync_error:
                LOG.warning(f"后台实例导出前同步失败（继续导出）: {sync_error}")

        blueprint_model = None
        if quick_export:
            # 快速局部导出后台分支：临时树单轮导出 + 后处理（与同步快速导出一致）
            round_plan = dict(round_plans[0]) if round_plans else dict(QUICK_EXPORT_ROUND_PLAN)
            export_progress.report_stage("快速局部导出")
            _write_stage(
                stage_path,
                "快速局部导出",
                export_progress.step_done(),
                export_progress.step_total(),
                f"导出 {len(object_names)} 个选中物体",
            )

            round_result = ExportRoundExecutor.execute_round(
                tree=tree,
                round_plan=round_plan,
                allow_parallel_preprocess=False,
            )
            blueprint_model = round_result.get("blueprint_model", blueprint_model)
            buffer_sizes.append(int(round_result.get("buffer_size") or 0))
            result["rounds"] = 1
            result["buffer_sizes"] = list(buffer_sizes)
            # 按计划清单推进（单轮 1 步）
            export_progress.complete_step("快速局部导出")

            _write_stage(
                stage_path,
                "后处理",
                export_progress.step_done(),
                export_progress.step_total(),
                "执行后处理节点",
            )
            if blueprint_model is not None:
                blueprint_model.execute_postprocess_nodes(output_dir)

            result["warnings"] = _collect_ini_variable_warnings(output_dir)
            done_steps = export_progress.finish_steps()  # 收尾算最后一步（并补满）
            result["status"] = "success"
            result["error"] = ""
            _write_stage(
                stage_path,
                "完成",
                done_steps,
                export_progress.step_total(),
                "后台快速局部导出完成",
            )
        elif bool(manifest.get("direct_export")):
            # 直出模式：与同步导出走同一个 execute_direct_export 入口（headless 可用，
            # 经验库 `theherta4-plugin-headless-direct-export` 已实证），它内部含后处理。
            export_progress.report_stage("直出导出")
            from .direct_export import execute_direct_export

            execute_direct_export(context=bpy.context, tree=tree)
            done_steps = export_progress.finish_steps()  # 收尾算最后一步（并补满）
            result["rounds"] = 1
            result["status"] = "success"
            result["error"] = ""
            _write_stage(
                stage_path,
                "完成",
                done_steps,
                export_progress.step_total(),
                "后台直出导出完成",
            )
        else:
            for index, round_plan in enumerate(round_plans, start=1):
                # 第 2 轮开始时，第 1 轮才算完成；每轮只推进一步（按计划清单名字）
                if index > 1:
                    export_progress.complete_step(f"第 {index - 1} 轮")

                _write_stage(
                    stage_path,
                    f"第 {index} 轮",
                    export_progress.step_done(),
                    export_progress.step_total(),
                    str(round_plan.get("description", "")),
                )

                round_result = ExportRoundExecutor.execute_round(
                    tree=tree,
                    round_plan=round_plan,
                    allow_parallel_preprocess=False,
                )

                blueprint_model = round_result.get("blueprint_model", blueprint_model)
                buffer_sizes.append(int(round_result.get("buffer_size") or 0))
                result["rounds"] = index
                result["buffer_sizes"] = list(buffer_sizes)

                _write_stage(
                    stage_path,
                    f"第 {index} 轮",
                    export_progress.step_done(),
                    export_progress.step_total(),
                    f"完成 {round_plan.get('buffer_folder_name', '')}",
                )

            if round_plans:
                # 最后一轮完成（按计划清单名字推进）
                export_progress.complete_step(f"第 {len(round_plans)} 轮")

            _write_stage(
                stage_path,
                "后处理",
                export_progress.step_done(),
                export_progress.step_total(),
                "执行后处理节点",
            )

            if blueprint_model is not None:
                blueprint_model.execute_postprocess_nodes(output_dir)

            result["warnings"] = _collect_ini_variable_warnings(output_dir)
            done_steps = export_progress.finish_steps()  # 收尾算最后一步（并补满）
            result["status"] = "success"
            result["error"] = ""
            _write_stage(
                stage_path,
                "完成",
                done_steps,
                export_progress.step_total(),
                "后台导出完成",
            )
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
        traceback.print_exc()
        _write_stage(
            stage_path,
            "失败",
            export_progress.step_done(),
            export_progress.step_total(),
            str(error)[:200],
        )
    finally:
        try:
            uninstall_progress_bridge()
        except Exception:
            pass

        try:
            log_content = LOG.get_log_content(strip_ansi=True)
            if log_path:
                with open(log_path, "w", encoding="utf-8") as file:
                    file.write(log_content)
        except Exception:
            pass

        _write_json(result_path, result)

        LOG.stop_collecting()


# ---------------------------------------------------------------------------
# 主进程：会话 + 进度
# ---------------------------------------------------------------------------

#: 当前正在运行的后台导出会话（对象或 id；None = 没有活动会话）
_ACTIVE_SESSION = None


def set_active_session(session) -> None:
    """登记当前活动的后台导出会话（start 成功后调用；存对象或 id 均可）。"""
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = session


def get_active_session():
    """取当前活动的后台导出会话；没有则返回 None（供各导出入口防重入）。"""
    return _ACTIVE_SESSION


def clear_active_session(session=None) -> None:
    """清除活动会话登记；传 session 时只清自己（避免误清后来的会话）。"""
    global _ACTIVE_SESSION
    if session is not None and _ACTIVE_SESSION is not session:
        return
    _ACTIVE_SESSION = None


def format_progress_bar(done, total, width: int = 20) -> str:
    """文本进度条：``[████████░░░░░░░░░░░░] 40%``；总数未知时返回空串。

    用作 ``window_manager.progress_begin/update`` 不可用（headless / 测试桩 /
    某些 timer 环境）时的兜底，保证 header 上「一定有进度条可看」。
    """
    try:
        total_value = int(total or 0)
    except (TypeError, ValueError):
        return ""
    if total_value <= 0:
        return ""

    try:
        done_value = int(done or 0)
    except (TypeError, ValueError):
        done_value = 0
    done_value = max(0, min(done_value, total_value))

    cells = max(1, int(width or 20))
    filled = max(0, min(cells, int(round(done_value / total_value * cells))))
    percent = int(done_value / total_value * 100)
    return f"[{'█' * filled}{'░' * (cells - filled)}] {percent}%"


def format_percent_bar(percent, width: int = 20) -> str:
    """文本进度条（百分比驱动）：``[████░░] 40%``。

    后台导出的总步数会随实际阶段动态增长，所以主进程只按**百分比**（整数
    0~100）驱动原生进度条与这条文本条；文本里的 ``done/total`` 由
    ``progress_text`` 单独显示。``percent`` 非法时返回空串。
    """
    try:
        value = int(percent or 0)
    except (TypeError, ValueError):
        return ""
    value = max(0, min(100, value))

    cells = max(1, int(width or 20))
    filled = max(0, min(cells, int(round(value / 100 * cells))))
    return f"[{'█' * filled}{'░' * (cells - filled)}] {value}%"


def _resolve_status_workspace(context=None):
    """取当前 workspace：优先用调用方传入的 context（modal 事件 context 最可靠）。

    modal 回调里全局 ``bpy.context.workspace`` 可能拿不到（窗口上下文被弱化），
    而 ``window_manager.progress_*`` 用事件 context 是好的 —— 这就是「原生百分比
    一直动、状态栏文本不更新」的原因。所以这里按 context → 全局 → window →
    任意 window 的顺序兜底。
    """
    sources = []
    if context is not None:
        sources.append(getattr(context, "workspace", None))
        sources.append(getattr(getattr(context, "window", None), "workspace", None))
        window_manager = getattr(context, "window_manager", None)
    else:
        window_manager = getattr(bpy.context, "window_manager", None)
        sources.append(getattr(bpy.context, "workspace", None))
        sources.append(getattr(getattr(bpy.context, "window", None), "workspace", None))
    for workspace in sources:
        if workspace is not None:
            return workspace
    for window in getattr(window_manager, "windows", None) or []:
        workspace = getattr(window, "workspace", None)
        if workspace is not None:
            return workspace
    return None


_STATUS_TEXT_STATE = {"last": None}


def _set_status_text(text, context=None) -> None:
    """把进度文本写到 Blender **唯一的提示位**：状态栏（``workspace.status_text_set``）。

    为什么不再逐 area 写 ``header_text_set``：那会让每个编辑器顶部都出现同一句话
    （2026-10-20 用户明确要求：用 Blender 自带的悬浮提示，不要每个窗口塞一条）。
    ``status_text_set`` 不可用（headless / 测试桩）时，兜底只写**一个** 3D 视图的
    header（没有就第一个 area），同样只写一处。异常全部静默。

    **关键（2026-10-21 实测定位）**：``Workspace.status_text_set`` 只给「**调用时
    context 所在窗口**」的状态栏区域打重绘标记（Blender 源码
    ``ED_workspace_status_text`` → ``WM_window_status_area_find(CTX_wm_window(C), ...)``），
    **不会标记其它窗口**。所以以前在蓝图独立窗口里点导出时，文本虽然写进了
    workspace，主窗口状态栏却收不到重绘标记 —— 用户看到的就是「主窗口进度随机
    卡几秒~十几秒、跳阶段」（鼠标旁的原生 ``wm.progress_*`` 是 GHOST 直接画的，
    与重绘无关，所以一直正常）。

    因此这里**逐个窗口用该窗口自己的 context 调一次** ``status_text_set``：
    每个窗口都会被精确标记重绘，主窗口和蓝图窗口的状态栏都能实时跟随。
    只标记「状态栏」这一个区域，不做全窗口重绘，开销可忽略。
    同一文本也不会重复设置（``_STATUS_TEXT_STATE`` 去重）。
    """
    try:
        value = "" if text is None else str(text)
        if value and _STATUS_TEXT_STATE.get("last") == value:
            return
        _STATUS_TEXT_STATE["last"] = value

        window_manager = None
        if context is not None:
            window_manager = getattr(context, "window_manager", None)
        if window_manager is None:
            window_manager = getattr(bpy.context, "window_manager", None)

        workspaces = []
        wrote = False
        for window in getattr(window_manager, "windows", None) or []:
            workspace = getattr(window, "workspace", None)
            if workspace is None:
                continue
            if workspace not in workspaces:
                workspaces.append(workspace)
            setter = getattr(workspace, "status_text_set", None)
            if not callable(setter):
                continue
            try:
                override = {"window": window}
                screen = getattr(window, "screen", None)
                if screen is not None:
                    override["screen"] = screen
                with bpy.context.temp_override(**override):
                    setter(value)
                wrote = True
            except Exception:
                continue

        # 没有窗口（headless / 测试桩）时退回「直接按 workspace 写」，保持旧行为。
        if not wrote:
            for workspace in workspaces or (getattr(bpy.data, "workspaces", None) or []):
                setter = getattr(workspace, "status_text_set", None)
                if not callable(setter):
                    continue
                try:
                    setter(value)
                    wrote = True
                except Exception:
                    continue
        if wrote:
            return
    except Exception:
        pass
    try:
        window_manager = getattr(context, "window_manager", None) if context is not None else getattr(bpy.context, "window_manager", None)
        text_value = "" if text is None else str(text)
        for window in getattr(window_manager, "windows", None) or []:
            screen = getattr(window, "screen", None)
            areas = list(getattr(screen, "areas", None) or [])
            # 先找 3D 视图；写不进去（桩/坏 area）就继续找下一个，写到成功为止
            for area in areas:
                if getattr(area, "type", "") != "VIEW_3D":
                    continue
                try:
                    area.header_text_set(text_value)
                    area.tag_redraw()
                    return
                except Exception:
                    continue
            for area in areas:
                try:
                    area.header_text_set(text_value)
                    area.tag_redraw()
                    return
                except Exception:
                    continue
    except Exception:
        pass


def push_progress_header(text: str, context=None) -> None:
    """把进度写到状态栏（图形进度条见 ``window_manager.progress_*``）；单处显示，绝不逐 area。

    ``context`` 传 modal 的事件 context（最可靠）；不传时退回全局 ``bpy.context``。
    """
    _set_status_text(text, context)


def clear_progress_header(context=None) -> None:
    """撤掉状态栏上的后台导出进度文本（原生图形进度条由 ``progress_end`` 收尾）。"""
    _set_status_text("", context)


def _default_preprocess_cache_dir() -> str:
    """主进程侧的前处理缓存目录：真实工程目录下的 .ssmt_preprocess_cache。

    worker 的 bpy.data.filepath 指向临时快照，必须由主进程算好再随清单传过去。
    """
    try:
        from .preprocess_cache import PreProcessCache

        return str(PreProcessCache.get_original_cache_dir() or "")
    except Exception as error:
        LOG.warning(f"解析前处理缓存目录失败（后台实例将用默认目录）: {error}")
        return ""


class BackgroundExportSession:
    """一次后台导出的主进程侧会话：快照 → 清单 → 子进程 → 轮询 → 清理。"""

    def __init__(
        self,
        tree_name: str,
        output_dir: str,
        round_plans: list,
        logic_name: str = "",
        workspace_name: str = "",
        has_shapekey_export: bool = False,
        max_shapekey_slot: int = 0,
        has_multi_file_export: bool = False,
        max_multi_file_count: int = 0,
        direct_export: bool = False,
        blender_executable: str = "",
        addon_name: str = "",
        session_dir: str = "",
        total_steps: int = 0,
        quick_export: bool = False,
        object_names: list = None,
        preprocess_cache_dir: str = "",
    ):
        self.tree_name = str(tree_name or "")
        self.output_dir = str(output_dir or "")
        self.round_plans = [dict(round_plan) for round_plan in (round_plans or [])]
        self.logic_name = str(logic_name or "")
        self.workspace_name = str(workspace_name or "")
        self.has_shapekey_export = bool(has_shapekey_export)
        self.max_shapekey_slot = int(max_shapekey_slot or 0)
        self.has_multi_file_export = bool(has_multi_file_export)
        self.max_multi_file_count = int(max_multi_file_count or 0)
        self.direct_export = bool(direct_export)
        self.blender_executable = str(blender_executable or "")
        self.addon_name = str(addon_name or "")
        self.session_dir = str(session_dir or "")
        #: 主进程预计算的总步骤数（与 worker 用同一个计划函数，导出期间固定）
        self.total_steps = max(0, int(total_steps or 0))
        #: 快速局部导出：worker 按 object_names 在快照里重定位网格并重建临时树
        self.quick_export = bool(quick_export)
        self.object_names = [str(name) for name in (object_names or []) if str(name)]
        #: 前处理缓存目录由主进程算，worker 不能自己推（快照路径会指向临时目录）
        self.preprocess_cache_dir = str(preprocess_cache_dir or "")

        self.snapshot_path = ""
        self.manifest_path = ""
        self.manifest = {}
        self.stage_path = ""
        self.result_path = ""
        self.log_path = ""

        self.process = None
        #: start() 成功启动后台实例时的 monotonic 时刻；None = 尚未启动（不计时）
        self.started_monotonic = None
        self.status = "idle"
        self.stage = ""
        self.stage_done = 0
        self.stage_total = 0
        #: 已完成步骤数（与 stage_done 同步；UI 进度条按 step 计数读它）
        self.done_steps = 0
        #: 已显示百分比（整数 0~100；total 在导出开始时已固定，单调保护留给旧 worker）
        self.progress_percent = 0
        #: 已经显示过的最大百分比（total 变大时防止百分比回退）
        self._max_percent = 0
        self.stage_message = ""
        self.stage_node = ""
        self.stage_nodes = {}
        self.stage_elapsed = 0.0
        self.result = {}
        self.error = ""
        self.cancelled = False
        self.cleaned = False

        self._log_handle = None

    # -- 启动 ---------------------------------------------------------------

    def start(self) -> bool:
        """建会话目录 → 存快照 → 写清单 → 起子进程；返回 False 表示启动失败。"""
        if self.process is not None:
            self.error = "后台导出已经在运行"
            return False

        try:
            if not self.session_dir:
                self.session_dir = tempfile.mkdtemp(prefix="herta_bg_export_")
            os.makedirs(self.session_dir, exist_ok=True)

            self.snapshot_path = save_export_snapshot(self.session_dir)
            if not self.preprocess_cache_dir:
                self.preprocess_cache_dir = _default_preprocess_cache_dir()
            self.manifest = build_manifest(
                session_dir=self.session_dir,
                snapshot_path=self.snapshot_path,
                tree_name=self.tree_name,
                output_dir=self.output_dir,
                round_plans=self.round_plans,
                logic_name=self.logic_name,
                workspace_name=self.workspace_name,
                has_shapekey_export=self.has_shapekey_export,
                max_shapekey_slot=self.max_shapekey_slot,
                has_multi_file_export=self.has_multi_file_export,
                max_multi_file_count=self.max_multi_file_count,
                direct_export=self.direct_export,
                total_steps=self.total_steps,
                quick_export=self.quick_export,
                object_names=self.object_names,
                preprocess_cache_dir=self.preprocess_cache_dir,
            )

            self.manifest_path = str(self.manifest.get("manifest_path") or "")
            self.stage_path = str(self.manifest.get("stage_path") or "")
            self.result_path = str(self.manifest.get("result_path") or "")
            self.log_path = str(self.manifest.get("log_path") or "")

            command = build_worker_command(
                self.manifest,
                blender_executable=self.blender_executable,
                addon_name=self.addon_name,
            )

            if not command[0]:
                raise RuntimeError("未找到 Blender 可执行文件路径")

            # 子进程起来后 Blender 先加载 480MB 工程快照（约 10 秒，期间不跑 python、
            # 写不出 stage.json），状态栏先显示这一段等待文本；worker 写出第一个
            # stage.json 后由 poll() 自然覆盖。
            self.stage = "加载工程"
            self.stage_done = 0
            self.done_steps = 0
            self.progress_percent = 0
            self._max_percent = 0
            # 步骤数以主进程预计算（同一计划函数）为准；未提供时退化成「轮次 + 收尾」
            self.stage_total = self.total_steps or (len(self.round_plans) + 1)
            self.stage_message = "后台实例正在加载工程（约 10 秒）…"

            # 子进程输出直接落到日志文件：主进程不读管道，poll 永远不会被管道阻塞
            self._log_handle = open(self.log_path, "ab")
            self.process = subprocess.Popen(
                command,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            self.status = "running"
            # 本次导出总用时从「后台实例真正启动」起算；计时异常绝不能影响导出
            try:
                self.started_monotonic = time.monotonic()
            except Exception:
                self.started_monotonic = None
            # 防重入：登记活动会话，清理时（cleanup）再注销
            set_active_session(self)
            return True
        except Exception as error:
            self.error = f"后台导出启动失败: {error}"
            self.status = "error"
            self._close_log_handle()
            return False

    # -- 轮询 ---------------------------------------------------------------

    def poll(self) -> str:
        """返回 ``"running"`` / ``"success"`` / ``"error"``（取消后固定为 error 侧）。"""
        if self.status in ("success", "error", "cancelled"):
            return self.status

        if self.process is None:
            self.status = "error"
            self.error = self.error or "后台导出尚未启动"
            return self.status

        self._refresh_stage()

        exit_code = self.process.poll()
        if exit_code is None:
            self.status = "running"
            return self.status

        self._close_log_handle()
        self.result = _read_json(self.result_path) or {}
        self._refresh_stage()

        if self.result.get("status") == "success":
            self.status = "success"
            self.error = ""
        else:
            self.status = "error"
            self.error = (
                str(self.result.get("error") or "").strip()
                or f"后台导出进程异常退出（退出码 {exit_code}）"
            )
        return self.status

    def _refresh_stage(self) -> None:
        stage = _read_json(self.stage_path)
        if not isinstance(stage, dict):
            return

        self.stage = str(stage.get("stage") or self.stage)
        try:
            self.stage_done = int(stage.get("done") or 0)
        except (TypeError, ValueError):
            self.stage_done = 0
        # total 在导出开始时由计划清单固定；这里仍读 stage.json 的 total，
        # 兼容旧 worker（会动态增长）与早期没有 total 的快照。
        try:
            stage_total = int(stage.get("total") or 0)
        except (TypeError, ValueError):
            stage_total = 0
        if stage_total > 0:
            self.stage_total = stage_total
        elif self.stage_total <= 0:
            self.stage_total = self.total_steps
        # 主进程的 total_steps 与 stage.json 的 total 同步（UI 文本 done/total）
        if self.stage_total > 0:
            self.total_steps = self.stage_total
        self.done_steps = self.stage_done
        self.stage_message = str(stage.get("message") or "")
        self._refresh_progress_percent()

        # 细粒度进度：``_write_stage`` 写的粗粒度快照没有子状态字段，
        # 这时保留上一次的值，避免节点高亮与"当前节点"在轮次切换时闪空。
        if "node" in stage:
            self.stage_node = str(stage.get("node") or "")

        nodes = stage.get("nodes")
        if isinstance(nodes, dict):
            self.stage_nodes = {
                str(name): str(status)
                for name, status in nodes.items()
                if str(name) and str(status)
            }

        try:
            self.stage_elapsed = max(0.0, float(stage.get("elapsed") or 0.0))
        except (TypeError, ValueError):
            pass

    def _refresh_progress_percent(self) -> None:
        """按 ``done / total`` 算整数百分比（0~100），并保证**单调不回退**。

        worker 的 total 在导出开始时已由计划清单固定，正常情况下刻度恒定；
        主进程仍记「已经显示过的最大百分比」兜底旧 worker / total 变化时的回跳
        （total<=0 时按 0 处理）。
        """
        try:
            total = max(0, int(self.stage_total or 0))
            done = max(0, int(self.stage_done or 0))
        except (TypeError, ValueError):
            return  # 进度显示失败绝不能影响导出轮询
        percent = int(done * 100 / total) if total > 0 else 0
        percent = max(0, min(100, percent))
        if percent < self._max_percent:
            percent = self._max_percent
        self._max_percent = percent
        self.progress_percent = percent

    # -- 展示 ---------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        """从 ``start()`` 成功启动后台实例到**现在**的秒数；重复调用返回当前时长。

        未启动、或计时出现任何异常时返回 ``0.0``：计时只用于文案，绝不能影响导出。
        """
        try:
            started = self.started_monotonic
            if started is None:
                return 0.0
            return max(0.0, float(time.monotonic() - started))
        except Exception:
            return 0.0

    def elapsed_text(self) -> str:
        """统一格式的用时文案 ``用时 38.6 秒``（保留 1 位小数，超 60 秒也照常显示秒）。

        计时不可用时返回空串，调用方据此退化为不带用时的原文案。
        """
        try:
            if self.started_monotonic is None:
                return ""
            return f"用时 {self.elapsed_seconds():.1f} 秒"
        except Exception:
            return ""

    def _append_elapsed(self, message: str) -> str:
        """把用时幂等追加到摘要末尾；``message`` 已含「用时」时不重复添加。"""
        try:
            if "用时" in message:
                return message
            label = self.elapsed_text()
            if not label:
                return message
            return f"{message}，{label}"
        except Exception:
            return message

    def progress_text(self) -> str:
        """状态栏短文本，例如 ``后台导出：3/7 后处理-创建副本｜形态键基态``。

        ``{done}/{total}`` 是**步骤数**（总步数由主进程预计算）；没有总步数时
        省略分数，没有当前节点时省略竖线。
        """
        if self.status == "success":
            return "后台导出完成"
        if self.status == "cancelled":
            return "后台导出已取消"
        if self.status == "error":
            return "后台导出失败"

        stage = self.stage or "准备"
        if self.stage_total > 0:
            done = max(0, min(self.stage_done, self.stage_total))
            text = f"后台导出：{done}/{self.stage_total} {stage}"
        else:
            text = f"后台导出：{stage}"
        if self.stage_node:
            text += f"｜{self.stage_node}"
        return text

    def success_summary(self) -> str:
        rounds = int(self.result.get("rounds") or len(self.round_plans))
        message = f"后台导出完成，共 {rounds} 轮"
        warnings = [str(item) for item in (self.result.get("warnings") or []) if str(item)]
        if warnings:
            head = " | ".join(warnings[:3])
            suffix = f"；另有 {len(warnings) - 3} 条" if len(warnings) > 3 else ""
            message += f"；自检警告: {head}{suffix}"
        # 成功摘要末尾追加本次总用时（原有文案保持不变）
        return self._append_elapsed(message)

    def error_summary(self) -> str:
        message = self.error or "后台导出失败"
        if self.log_path:
            message = f"{message}（日志: {self.log_path}）"
        return message

    # -- 取消 / 清理 --------------------------------------------------------

    def cancel(self) -> bool:
        """终止子进程：先 terminate，2 秒后仍在则 kill。"""
        self.cancelled = True

        process = self.process
        if process is None:
            self.status = "cancelled"
            return True

        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

            deadline = time.monotonic() + KILL_GRACE_SECONDS
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(TERMINATE_POLL_INTERVAL)
            else:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=KILL_GRACE_SECONDS)
                except Exception:
                    pass

        self._close_log_handle()
        self.status = "cancelled"
        return True

    def cleanup(self) -> str:
        """删快照与清单（保留 log 便于排查），返回日志路径。

        快照可能带 Blender 的 ``.blend1`` 版本备份，按文件名前缀一起清掉，
        避免每个会话都在临时目录里留一份大文件。
        """
        self._close_log_handle()

        targets = [self.manifest_path]
        if self.snapshot_path:
            targets.append(self.snapshot_path)
            directory = os.path.dirname(self.snapshot_path) or "."
            base_name = os.path.basename(self.snapshot_path)
            try:
                for entry in os.listdir(directory):
                    if entry.startswith(base_name):
                        targets.append(os.path.join(directory, entry))
            except OSError:
                pass

        for file_path in targets:
            if not file_path or not os.path.exists(file_path):
                continue
            try:
                os.remove(file_path)
            except OSError:
                pass

        self.result.setdefault("log_path", self.log_path)
        self.cleaned = True
        # 注销活动会话登记（只清自己，避免误清别的会话）
        clear_active_session(self)
        return self.log_path

    def _close_log_handle(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass


def register():
    pass


def unregister():
    pass
