import bpy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.logic_name import LogicName
from ..utils.log_utils import LOG
from ..utils.timer_utils import TimerUtils
from .model import BluePrintModel
from .export_helper import BlueprintExportHelper
from .preprocess import PreProcessHelper
from .preprocess_cache import PreProcessCache
from .preprocess_parallel import ParallelPreprocessCoordinator
from ..ui.universal.efmi import ExportEFMI
from ..ui.universal.gimi import ExportGIMI
from ..ui.universal.himi import ExportHIMI
from ..ui.universal.identityv import ExportIdentityV
from ..ui.universal.snowbreak import ExportSnowBreak
from ..ui.universal.srmi import ExportSRMI
from ..ui.universal.unity import ExportUnity
from ..ui.wwmi.wwmi_export import ExportWWMI
from ..ui.universal.yysls import ExportYYSLS
from ..ui.universal.zzmi import ExportZZMI


def _raise_for_unknown_logic_name() -> None:
    raise ParallelExportError(
        "当前游戏预设未加载或不受支持，未执行任何主导出逻辑。"
        f" 当前 logic_name='{GlobalConfig.logic_name}'，请先确认全局设置中的游戏预设已正确加载。"
    )


class ParallelExportError(RuntimeError):
    pass


class ExportRoundExecutor:
    @classmethod
    def execute_round(cls, tree, round_plan: dict, allow_parallel_preprocess: bool = True) -> dict:
        BluePrintModel.clear_object_name_mapping()
        BlueprintExportHelper.set_runtime_blueprint_tree(tree)
        BlueprintExportHelper.calculate_max_export_count(tree)
        nested_trees = cls.collect_nested_trees(tree)
        PreProcessHelper.recover_blueprint_node_references(tree, nested_trees)

        try:
            cls._prepare_round_state(tree, round_plan)

            object_names = cls.collect_object_names_from_tree(tree)

            TimerUtils.start_stage("前处理")
            if allow_parallel_preprocess and GlobalProterties.enable_parallel_preprocess():
                LOG.info("   ⚡ 已启用并行前处理")
                original_to_copy_map = ParallelPreprocessCoordinator.execute_preprocess(object_names)
            else:
                if not allow_parallel_preprocess and GlobalProterties.enable_parallel_preprocess():
                    LOG.info("   ℹ️ 当前轮次运行在并行导出子进程中，已禁用嵌套并行前处理，改为当前轮次独立串行前处理")
                original_to_copy_map = PreProcessHelper.execute_preprocess(object_names)

            if original_to_copy_map:
                PreProcessHelper.update_blueprint_node_references(tree, nested_trees)
            TimerUtils.end_stage("前处理")

            TimerUtils.start_stage("蓝图解析")
            blueprint_model = cls._build_blueprint_model(tree)
            TimerUtils.end_stage("蓝图解析")

            if round_plan.get("generate_ini"):
                cls.export_with_ini(blueprint_model)
            else:
                cls.export_buffers_only(blueprint_model)

            if round_plan.get("generate_classification_report"):
                BlueprintExportHelper.generate_shapekey_classification_report(blueprint_model)

            buffer_size = cls.get_buffer_folder_size(round_plan["buffer_folder_name"])

            return {
                "blueprint_model": blueprint_model,
                "buffer_size": buffer_size,
                "buffer_folder_name": round_plan["buffer_folder_name"],
                "object_count": len(blueprint_model.ordered_draw_obj_data_model_list),
            }
        finally:
            PreProcessHelper.cleanup_copies(silent=True)

    @classmethod
    def _prepare_round_state(cls, tree, round_plan: dict):
        shape_key_mode = round_plan.get("shapekey_mode", "unchanged")
        if shape_key_mode != "unchanged":
            BlueprintExportHelper.collect_shapekey_objects(tree)
            if shape_key_mode == "all_zero":
                LOG.info("   🎭 当前轮次形态键状态: 全部归零后再执行前处理")
                BlueprintExportHelper.set_all_shapekey_values(0)
            elif shape_key_mode == "slot":
                LOG.info(f"   🎭 当前轮次形态键状态: 槽位 {round_plan.get('shapekey_slot_index')} = 1，其余归零，然后执行前处理")
                BlueprintExportHelper.set_all_shapekey_values(0, round_plan.get("shapekey_slot_index"))

        BlueprintExportHelper.set_current_buffer_folder_name(round_plan["buffer_folder_name"])
        BlueprintExportHelper.set_current_export_index(round_plan["export_index"])

        if round_plan.get("phase") == "multifile":
            obj_info = BlueprintExportHelper.get_multi_file_export_object_info(round_plan["export_index"] - 1)
            for node_name, info in obj_info.items():
                LOG.info(f"   节点 '{node_name}' → 物体: {info.get('object_name', 'N/A')}")

    @classmethod
    def _build_blueprint_model(cls, tree):
        LOG.info("   正在解析蓝图...")
        try:
            blueprint_model = BluePrintModel(tree=tree, context=bpy.context)
        except ValueError as error:
            raise ParallelExportError(f"蓝图解析失败: {error}") from error

        cls.validate_copy_references(blueprint_model)
        LOG.info(f"📋 待导出物体: {len(blueprint_model.ordered_draw_obj_data_model_list)} 个")
        return blueprint_model

    @staticmethod
    def export_with_ini(blueprint_model):
        if GlobalConfig.logic_name == LogicName.EFMI:
            ExportEFMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.GIMI:
            ExportGIMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.HIMI:
            ExportHIMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.IdentityV:
            ExportIdentityV(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.SRMI:
            ExportSRMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.ZZMI:
            ExportZZMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name in (LogicName.WWMI, LogicName.NTEMI):
            ExportWWMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.SnowBreak:
            ExportSnowBreak(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.YYSLS:
            ExportYYSLS(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name in (LogicName.Naraka, LogicName.NarakaM, LogicName.GF2, LogicName.AILIMIT):
            ExportUnity(blueprint_model=blueprint_model).export()
        else:
            _raise_for_unknown_logic_name()

    @staticmethod
    def export_buffers_only(blueprint_model):
        if GlobalConfig.logic_name == LogicName.EFMI:
            ExportEFMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.GIMI:
            ExportGIMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.HIMI:
            ExportHIMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.IdentityV:
            ExportIdentityV(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.SRMI:
            ExportSRMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.ZZMI:
            ExportZZMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name in (LogicName.WWMI, LogicName.NTEMI):
            ExportWWMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.SnowBreak:
            ExportSnowBreak(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.YYSLS:
            ExportYYSLS(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name in (LogicName.Naraka, LogicName.NarakaM, LogicName.GF2, LogicName.AILIMIT):
            ExportUnity(blueprint_model=blueprint_model).export_buffers_only()
        else:
            _raise_for_unknown_logic_name()

    @staticmethod
    def collect_object_names_from_tree(tree) -> list:
        # 并行轮次和主线程轮次必须看到完全一致的连通物体集合。
        object_names = BlueprintExportHelper.collect_connected_object_names(tree)
        LOG.info(f"Collected preprocess objects from {tree.name}: {len(object_names)}")
        return object_names

    @staticmethod
    def collect_nested_trees(tree, visited=None) -> list:
        if visited is None:
            visited = set()

        if tree.name in visited:
            return []
        visited.add(tree.name)

        output_node = BlueprintExportHelper.get_node_from_bl_idname(tree, 'SSMTNode_Result_Output')

        nested_trees = []
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                if output_node and not BlueprintExportHelper._is_node_connected_to_output(tree, node):
                    continue
                blueprint_name = getattr(node, 'blueprint_name', '')
                if blueprint_name and blueprint_name != 'NONE':
                    nested_tree = bpy.data.node_groups.get(blueprint_name)
                    if nested_tree and nested_tree.name not in visited:
                        nested_trees.append(nested_tree)
                        nested_trees.extend(ExportRoundExecutor.collect_nested_trees(nested_tree, visited))

        return nested_trees

    @staticmethod
    def validate_copy_references(blueprint_model: BluePrintModel):
        if not PreProcessHelper.has_copies():
            return

        LOG.info("📋 验证物体引用是否为副本...")

        copy_names = set(PreProcessHelper.original_to_copy_map.values())
        reverse_rename = {v: k for k, v in BluePrintModel._object_name_mapping.items()}

        invalid_objects = []
        for chain in blueprint_model.processing_chains:
            if chain.is_valid:
                obj_name = chain.object_name
                if obj_name in copy_names:
                    continue
                pre_rename = reverse_rename.get(obj_name)
                if pre_rename and pre_rename in copy_names:
                    continue
                if PreProcessHelper.validate_copy_suffix(obj_name):
                    continue
                if chain.original_object_name and chain.original_object_name in copy_names:
                    continue
                invalid_objects.append(obj_name)

        if invalid_objects:
            raise ParallelExportError("前处理错误：物体引用未正确更新为副本")

        LOG.info("   ✅ 所有物体引用已正确更新为副本")

    @staticmethod
    def get_buffer_folder_size(buffer_folder_name: str) -> int:
        folder_path = os.path.join(GlobalConfig.path_generate_mod_folder(), buffer_folder_name)
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(folder_path):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if os.path.isfile(filepath):
                        total_size += os.path.getsize(filepath)
        except Exception as error:
            LOG.warning(f"计算文件夹大小失败: {error}")
        return total_size


class ParallelExportCoordinator:
    SESSION_VERSION = 1

    @classmethod
    def execute_middle_rounds(cls, tree, middle_rounds: list[dict]) -> list[dict]:
        if not middle_rounds:
            return []

        issues = ParallelPreprocessCoordinator.validate_runtime_requirements()
        if issues:
            raise ParallelExportError("；".join(issues))

        blender_path = ParallelPreprocessCoordinator.get_effective_blender_executable()
        instance_count = max(1, min(GlobalProterties.parallel_preprocess_instances(), len(middle_rounds)))
        timeout_seconds = GlobalProterties.parallel_preprocess_timeout_seconds()
        keep_temp = GlobalProterties.parallel_preprocess_keep_temp_files()

        session_dir = tempfile.mkdtemp(prefix="ssmt_parallel_export_")
        LOG.info(f"📁 创建并行导出会话目录: {session_dir}")

        try:
            snapshot_path = os.path.join(session_dir, "snapshot.blend")
            bpy.ops.wm.save_as_mainfile(filepath=snapshot_path, copy=True, check_existing=False)

            cache_dir = PreProcessCache.get_original_cache_dir()
            jobs = cls._build_jobs(session_dir, snapshot_path, tree.name, middle_rounds, cache_dir)
            cls._run_jobs(jobs, blender_path, instance_count, timeout_seconds)
            results = cls._load_results(jobs)
            cls._merge_job_caches(jobs, cache_dir)
        except Exception:
            if not keep_temp:
                shutil.rmtree(session_dir, ignore_errors=True)
            raise

        if keep_temp:
            LOG.info(f"📁 并行导出临时目录已保留: {session_dir}")
        else:
            shutil.rmtree(session_dir, ignore_errors=True)

        return results

    @classmethod
    def _build_jobs(cls, session_dir: str, snapshot_path: str, tree_name: str, middle_rounds: list[dict], cache_dir: str) -> list[dict]:
        jobs = []
        for index, round_plan in enumerate(middle_rounds, start=1):
            job_dir = os.path.join(session_dir, f"job_{index:03d}")
            os.makedirs(job_dir, exist_ok=True)
            job_cache_dir = os.path.join(job_dir, "preprocess_cache")

            manifest_path = os.path.join(job_dir, "manifest.json")
            result_path = os.path.join(job_dir, "result.json")
            log_path = os.path.join(job_dir, "worker.log")

            manifest = {
                "version": cls.SESSION_VERSION,
                "snapshot_path": snapshot_path,
                "tree_name": tree_name,
                "round_plan": round_plan,
                "result_path": result_path,
                "log_path": log_path,
                "cache_read_dir": cache_dir,
                "cache_write_dir": job_cache_dir,
            }

            with open(manifest_path, "w", encoding="utf-8") as file:
                json.dump(manifest, file, ensure_ascii=False, indent=2)

            jobs.append({
                "index": index,
                "manifest_path": manifest_path,
                "result_path": result_path,
                "log_path": log_path,
                "snapshot_path": snapshot_path,
                "round_plan": round_plan,
                "cache_write_dir": job_cache_dir,
            })

        LOG.info(f"⚡ 并行导出任务数: {len(jobs)}")
        for job in jobs:
            round_plan = job["round_plan"]
            LOG.info(f"   Job {job['index']:03d}: 第 {round_plan['round_index']} 轮 -> {round_plan['buffer_folder_name']}")
        return jobs

    @classmethod
    def _merge_job_caches(cls, jobs: list[dict], cache_dir: str):
        if not cache_dir:
            return

        merged_count = 0
        for job in jobs:
            merged_count += PreProcessCache.merge_cache_dir(job.get("cache_write_dir", ""), cache_dir)

        if merged_count > 0:
            LOG.info(f"[ParallelExportCache] merged staged cache entries: {merged_count}")

    @classmethod
    def _run_jobs(cls, jobs: list[dict], blender_path: str, instance_count: int, timeout_seconds: int):
        addon_name = __package__.split('.')[0]
        LOG.info(f"🚀 启动并行导出子进程: {instance_count} 个")

        with ThreadPoolExecutor(max_workers=instance_count) as executor:
            future_map = {
                executor.submit(cls._run_single_job, job, blender_path, timeout_seconds, addon_name): job
                for job in jobs
            }

            for future in as_completed(future_map):
                job = future_map[future]
                future.result()
                LOG.info(f"   ✅ Job {job['index']:03d} 完成")

    @classmethod
    def _run_single_job(cls, job: dict, blender_path: str, timeout_seconds: int, addon_name: str):
        command = [
            blender_path,
            "--factory-startup",
            "--background",
            job["snapshot_path"],
            "--addons",
            addon_name,
            "--python-expr",
            f"import {addon_name}.blueprint.export_parallel as ep; ep.run_worker_cli()",
            "--",
            "--manifest",
            job["manifest_path"],
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            cls._write_text(job["log_path"], (error.stdout or "") + "\n" + (error.stderr or ""))
            raise ParallelExportError(f"Job {job['index']:03d} 超时") from error

        cls._write_text(job["log_path"], (result.stdout or "") + "\n" + (result.stderr or ""))

        if result.returncode != 0:
            raise ParallelExportError(f"Job {job['index']:03d} 启动失败，退出码 {result.returncode}")

    @classmethod
    def _load_results(cls, jobs: list[dict]) -> list[dict]:
        results = []
        for job in jobs:
            if not os.path.exists(job["result_path"]):
                raise ParallelExportError(f"Job {job['index']:03d} 未生成结果清单")

            with open(job["result_path"], "r", encoding="utf-8") as file:
                result = json.load(file)

            if result.get("status") != "success":
                raise ParallelExportError(f"Job {job['index']:03d} 执行失败: {result.get('error', '未知错误')}")

            results.append(result)
        return results

    @staticmethod
    def _write_text(file_path: str, content: str):
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)


def run_worker_cli():
    manifest_path = _parse_manifest_argument(sys.argv)
    if not manifest_path:
        raise RuntimeError("未提供并行导出 manifest 路径")
    _run_worker(manifest_path)


def _parse_manifest_argument(argv: list[str]) -> str:
    if "--" not in argv:
        return ""
    extra_args = argv[argv.index("--") + 1:]
    for index, value in enumerate(extra_args):
        if value == "--manifest" and index + 1 < len(extra_args):
            return extra_args[index + 1]
    return ""


def _run_worker(manifest_path: str):
    with open(manifest_path, "r", encoding="utf-8") as file:
        manifest = json.load(file)

    result = {
        "status": "error",
        "round_index": manifest["round_plan"].get("round_index", 0),
        "buffer_folder_name": manifest["round_plan"].get("buffer_folder_name", ""),
        "buffer_size": 0,
        "error": "",
    }

    try:
        LOG.start_collecting()
        GlobalConfig.read_from_main_json_ssmt4()
        
        cache_read_dir = manifest.get("cache_read_dir", "")
        cache_write_dir = manifest.get("cache_write_dir", "")
        if cache_write_dir or cache_read_dir:
            read_dirs = [cache_read_dir] if cache_read_dir else []
            PreProcessCache.set_override_cache_dirs(cache_write_dir or cache_read_dir, read_dirs)

        tree = bpy.data.node_groups.get(manifest["tree_name"])
        if not tree:
            raise ParallelExportError(f"未找到蓝图树: {manifest['tree_name']}")

        round_result = ExportRoundExecutor.execute_round(
            tree=tree,
            round_plan=manifest["round_plan"],
            allow_parallel_preprocess=False,
        )

        result["status"] = "success"
        result["buffer_size"] = round_result["buffer_size"]
    except Exception as error:
        result["error"] = str(error)
        traceback.print_exc()
    finally:
        PreProcessCache.clear_override_cache_dir()
        
        with open(manifest["log_path"], "w", encoding="utf-8") as file:
            file.write(LOG.get_log_content())

        with open(manifest["result_path"], "w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)

        LOG.stop_collecting()


def register():
    pass


def unregister():
    pass
