import bpy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..common.global_properties import GlobalProterties
from ..utils.log_utils import LOG
from .preprocess import PreProcessHelper
from .preprocess_cache import PreProcessCache


class ParallelPreprocessError(RuntimeError):
    pass


class ParallelPreprocessCoordinator:
    SESSION_VERSION = 1

    @classmethod
    def execute_preprocess(cls, object_names: list[str]) -> dict[str, str]:
        PreProcessHelper.reset_runtime_state()

        unique_objects = PreProcessHelper.collect_target_object_names(object_names)
        if not unique_objects:
            LOG.info("🔧 前处理完成: 0 个物体")
            return {}

        cache_enabled = GlobalProterties.enable_preprocess_cache()
        hash_map, cached_objects, uncached_objects = cls._classify_objects(unique_objects, cache_enabled)

        failed_cached_objects = cls._load_cached_objects(cached_objects)
        uncached_objects.extend(failed_cached_objects)

        if not uncached_objects:
            LOG.info(f"🔧 前处理完成: {len(unique_objects)} 个物体")
            return dict(PreProcessHelper.original_to_copy_map)

        config = cls._build_runtime_config(uncached_objects)
        session_dir = cls._prepare_session_directory()

        try:
            snapshot_path = cls._create_snapshot(session_dir)
            jobs = cls._build_jobs(session_dir, snapshot_path, uncached_objects, config)
            cls._run_jobs(jobs, config)
            cls._integrate_results(jobs, hash_map, cache_enabled)
        except Exception:
            if not config["keep_temp"]:
                shutil.rmtree(session_dir, ignore_errors=True)
            raise

        if config["keep_temp"]:
            LOG.info(f"📁 并行前处理临时目录已保留: {session_dir}")
        else:
            shutil.rmtree(session_dir, ignore_errors=True)

        LOG.info(f"🔧 前处理完成: {len(unique_objects)} 个物体")
        return dict(PreProcessHelper.original_to_copy_map)

    @classmethod
    def validate_runtime_requirements(cls) -> list[str]:
        issues = []

        if not bpy.data.filepath:
            issues.append("当前工程尚未保存，无法创建子工程快照")

        blender_path = cls.get_effective_blender_executable()
        if not blender_path:
            issues.append("未配置 Blender 可执行文件路径")
        elif not os.path.isfile(blender_path):
            issues.append("Blender 可执行文件路径无效")
        elif not blender_path.lower().endswith(".exe"):
            issues.append("Blender 可执行文件必须是 .exe")

        instances = GlobalProterties.parallel_preprocess_instances()
        if instances < 1:
            issues.append("并行实例数必须大于 0")

        timeout_seconds = GlobalProterties.parallel_preprocess_timeout_seconds()
        if timeout_seconds < 30:
            issues.append("单任务超时时间不能小于 30 秒")

        return issues

    @classmethod
    def get_effective_blender_executable(cls) -> str:
        custom_path = GlobalProterties.parallel_blender_executable().strip()
        if custom_path:
            return bpy.path.abspath(custom_path)
        return bpy.app.binary_path or ""

    @classmethod
    def get_validation_summary(cls) -> tuple[bool, str]:
        issues = cls.validate_runtime_requirements()
        if issues:
            return False, "；".join(issues)
        return True, "配置有效"

    @classmethod
    def _classify_objects(cls, object_names: list[str], cache_enabled: bool) -> tuple[dict[str, str], dict[str, str], list[str]]:
        hash_map: dict[str, str] = {}
        cached_objects: dict[str, str] = {}
        uncached_objects: list[str] = []

        if cache_enabled:
            LOG.info("🔐 计算物体哈希值...")
        else:
            LOG.info("🔐 已关闭前处理缓存，所有物体进入并行前处理")

        for obj_name in object_names:
            hash_value = PreProcessCache.compute_object_hash(obj_name) if cache_enabled else ""
            hash_map[obj_name] = hash_value
            if cache_enabled and hash_value and PreProcessCache.has_cache(hash_value):
                cached_objects[obj_name] = hash_value
            else:
                uncached_objects.append(obj_name)

        if cached_objects:
            LOG.info(f"📦 缓存命中: {len(cached_objects)} 个物体")
        if uncached_objects:
            LOG.info(f"🔄 缓存未命中: {len(uncached_objects)} 个物体, 进入并行前处理")

        return hash_map, cached_objects, uncached_objects

    @classmethod
    def _load_cached_objects(cls, cached_objects: dict[str, str]) -> list[str]:
        failed_objects = []
        for obj_name, hash_value in cached_objects.items():
            copy_name = f"{obj_name}_copy"
            success = PreProcessCache.load_from_cache(obj_name, hash_value)
            if success:
                PreProcessHelper.register_copy_result(obj_name, copy_name)
            else:
                LOG.warning(f"   ⚠️ 缓存加载失败 {obj_name}, 将重新进入并行前处理")
                failed_objects.append(obj_name)
        return failed_objects

    @classmethod
    def _build_runtime_config(cls, uncached_objects: list[str]) -> dict:
        issues = cls.validate_runtime_requirements()
        if issues:
            raise ParallelPreprocessError("；".join(issues))

        blender_path = cls.get_effective_blender_executable()
        instance_count = max(1, min(GlobalProterties.parallel_preprocess_instances(), len(uncached_objects)))
        timeout_seconds = GlobalProterties.parallel_preprocess_timeout_seconds()

        LOG.info("⚙️ 并行前处理配置")
        LOG.info(f"   Blender 路径: {blender_path}")
        LOG.info(f"   并行实例数: {instance_count}")
        LOG.info(f"   单任务超时: {timeout_seconds} 秒")
        LOG.info("   启动参数: --factory-startup --addons 当前插件")

        return {
            "blender_path": blender_path,
            "instance_count": instance_count,
            "timeout_seconds": timeout_seconds,
            "keep_temp": GlobalProterties.parallel_preprocess_keep_temp_files(),
        }

    @classmethod
    def _prepare_session_directory(cls) -> str:
        root_dir = tempfile.mkdtemp(prefix="ssmt_parallel_preprocess_")
        LOG.info(f"📁 创建并行前处理会话目录: {root_dir}")
        return root_dir

    @classmethod
    def _create_snapshot(cls, session_dir: str) -> str:
        snapshot_path = os.path.join(session_dir, "snapshot.blend")
        LOG.info("💾 正在创建主工程快照...")
        bpy.ops.wm.save_as_mainfile(filepath=snapshot_path, copy=True, check_existing=False)
        return snapshot_path

    @classmethod
    def _build_jobs(cls, session_dir: str, snapshot_path: str, object_names: list[str], config: dict) -> list[dict]:
        groups = cls._partition_objects(object_names, config["instance_count"])
        jobs = []

        for index, group in enumerate(groups, start=1):
            job_dir = os.path.join(session_dir, f"job_{index:03d}")
            os.makedirs(job_dir, exist_ok=True)

            manifest_path = os.path.join(job_dir, "manifest.json")
            result_path = os.path.join(job_dir, "result.json")
            result_blend_path = os.path.join(job_dir, "result.blend")
            log_path = os.path.join(job_dir, "worker.log")

            manifest = {
                "version": cls.SESSION_VERSION,
                "mode": "preprocess-only",
                "snapshot_path": snapshot_path,
                "job_index": index,
                "object_names": group,
                "result_path": result_path,
                "result_blend_path": result_blend_path,
                "log_path": log_path,
            }

            with open(manifest_path, "w", encoding="utf-8") as file:
                json.dump(manifest, file, ensure_ascii=False, indent=2)

            total_weight = sum(cls._estimate_object_weight(obj_name) for obj_name in group)
            jobs.append({
                "index": index,
                "object_names": group,
                "weight": total_weight,
                "job_dir": job_dir,
                "snapshot_path": snapshot_path,
                "manifest_path": manifest_path,
                "result_path": result_path,
                "result_blend_path": result_blend_path,
                "log_path": log_path,
            })

        LOG.info(f"📦 并行任务分组: {len(jobs)} 组")
        for job in jobs:
            LOG.info(f"   Job {job['index']:03d}: {len(job['object_names'])} 个物体, 权重 {job['weight']}")

        return jobs

    @classmethod
    def _partition_objects(cls, object_names: list[str], group_count: int) -> list[list[str]]:
        buckets = [{"weight": 0, "objects": []} for _ in range(group_count)]

        weighted_objects = []
        for obj_name in object_names:
            weighted_objects.append((obj_name, cls._estimate_object_weight(obj_name)))
        weighted_objects.sort(key=lambda item: item[1], reverse=True)

        for obj_name, weight in weighted_objects:
            bucket = min(buckets, key=lambda item: item["weight"])
            bucket["objects"].append(obj_name)
            bucket["weight"] += weight

        return [bucket["objects"] for bucket in buckets if bucket["objects"]]

    @classmethod
    def _estimate_object_weight(cls, obj_name: str) -> int:
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            return 1

        weight = 100
        weight += len(obj.constraints) * 500
        weight += len(obj.modifiers) * 1500
        weight += len(obj.vertex_groups) * 50

        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            weight += len(mesh.vertices)
            weight += len(mesh.loops) // 4
            if mesh.shape_keys:
                weight += len(mesh.shape_keys.key_blocks) * 4000

        return max(weight, 1)

    @classmethod
    def _run_jobs(cls, jobs: list[dict], config: dict):
        LOG.info("🚀 启动 Blender 子进程池...")

        with ThreadPoolExecutor(max_workers=config["instance_count"]) as executor:
            future_map = {
                executor.submit(cls._run_single_job, job, config): job
                for job in jobs
            }

            for future in as_completed(future_map):
                job = future_map[future]
                future.result()
                LOG.info(f"   ✅ Job {job['index']:03d} 已完成")

    @classmethod
    def _run_single_job(cls, job: dict, config: dict):
        addon_name = __package__.split('.')[0]
        command = [
            config["blender_path"],
            "--factory-startup",
            "--background",
            job["snapshot_path"],
            "--addons",
            addon_name,
            "--python-expr",
            f"import {addon_name}.blueprint.preprocess_parallel as pp; pp.run_worker_cli()",
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
                timeout=config["timeout_seconds"],
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            cls._write_text(job["log_path"], (exc.stdout or "") + "\n" + (exc.stderr or ""))
            raise ParallelPreprocessError(f"Job {job['index']:03d} 超时") from exc

        cls._write_text(job["log_path"], (result.stdout or "") + "\n" + (result.stderr or ""))

        if result.returncode != 0:
            raise ParallelPreprocessError(f"Job {job['index']:03d} 启动失败，退出码 {result.returncode}")

        if not os.path.exists(job["result_path"]):
            raise ParallelPreprocessError(f"Job {job['index']:03d} 未生成结果清单")

        with open(job["result_path"], "r", encoding="utf-8") as file:
            result_data = json.load(file)

        if result_data.get("status") != "success":
            raise ParallelPreprocessError(f"Job {job['index']:03d} 执行失败: {result_data.get('error', '未知错误')}")

    @classmethod
    def _integrate_results(cls, jobs: list[dict], hash_map: dict[str, str], cache_enabled: bool):
        LOG.info("📥 正在整合子进程结果...")

        for job in sorted(jobs, key=lambda item: item["index"]):
            with open(job["result_path"], "r", encoding="utf-8") as file:
                result_data = json.load(file)

            copy_map = result_data.get("original_to_copy_map", {})
            if not copy_map:
                continue

            if not os.path.exists(job["result_blend_path"]):
                raise ParallelPreprocessError(f"Job {job['index']:03d} 缺少结果工程")

            with bpy.data.libraries.load(job["result_blend_path"], link=False) as (data_from, data_to):
                data_to.objects = [name for name in data_from.objects if name in copy_map.values()]

            for loaded_obj in data_to.objects:
                if not loaded_obj:
                    continue
                if loaded_obj.name not in copy_map.values():
                    continue
                if bpy.data.objects.get(loaded_obj.name):
                    continue
                bpy.context.scene.collection.objects.link(loaded_obj)

            for original_name, copy_name in copy_map.items():
                if not bpy.data.objects.get(copy_name):
                    raise ParallelPreprocessError(f"整合失败，缺少副本物体: {copy_name}")
                PreProcessHelper.register_copy_result(original_name, copy_name)
                if cache_enabled:
                    hash_value = hash_map.get(original_name, "")
                    if hash_value:
                        PreProcessCache.save_to_cache(original_name, copy_name, hash_value)
                    else:
                        LOG.warning(f"⚠️ 缓存保存跳过: {original_name} 哈希值为空")

    @staticmethod
    def _write_text(file_path: str, content: str):
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)


def run_worker_cli():
    manifest_path = _parse_manifest_argument(sys.argv)
    if not manifest_path:
        raise RuntimeError("未提供并行前处理 manifest 路径")
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
        "job_index": manifest.get("job_index", 0),
        "original_to_copy_map": {},
        "error": "",
    }

    try:
        LOG.start_collecting()

        object_names = manifest.get("object_names", [])
        if not object_names:
            raise ParallelPreprocessError("任务中没有待处理物体")

        PreProcessHelper.reset_runtime_state()
        PreProcessHelper.execute_objects_without_cache(object_names)

        copy_map = dict(PreProcessHelper.original_to_copy_map)
        copy_names = set(copy_map.values())

        for obj in list(bpy.data.objects):
            if obj.name not in copy_names:
                bpy.data.objects.remove(obj, do_unlink=True)

        bpy.ops.wm.save_as_mainfile(filepath=manifest["result_blend_path"], check_existing=False)

        result["status"] = "success"
        result["original_to_copy_map"] = copy_map
    except Exception as exc:
        result["error"] = str(exc)
        traceback.print_exc()
    finally:
        log_content = LOG.get_log_content()
        with open(manifest["log_path"], "w", encoding="utf-8") as file:
            file.write(log_content)

        with open(manifest["result_path"], "w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)

        LOG.stop_collecting()


def register():
    pass


def unregister():
    pass