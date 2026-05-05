import bpy
import re
import numpy
from time import perf_counter
from typing import List, Dict, Optional

from ..utils.log_utils import LOG
from ..utils.timer_utils import TimerUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from .export_helper import BlueprintExportHelper
from .preprocess_cache import PreProcessCache
from ..common.global_properties import GlobalProterties
from ..common.non_mirror_workflow import NonMirrorWorkflowHelper
from ..common.object_prefix_helper import ObjectPrefixHelper


class PreProcessHelper:
    PRESERVE_SHAPEKEY_CACHE_PREFIX = "preserve_shapekeys_v4__"
    DIRECT_SHAPEKEY_CACHE_PREFIX = "direct_shapekeys_v1__"
    DIRECT_SHAPEKEY_RECORDS_SUFFIX = "direct_shapekey_records.pkl"
    _TRANSFORM_TOLERANCE = 1e-6
    _SHAPEKEY_VALUE_TOLERANCE = 1e-8
    _VG_MATCH_COPY_COLLECTION_PREFIX = "SSMT_VGMatchCopy"
    original_to_copy_map: Dict[str, str] = {}
    created_copies: List[str] = []
    modified_nodes: List[dict] = []

    @classmethod
    def reset_runtime_state(cls):
        cls.original_to_copy_map.clear()
        cls.created_copies.clear()
        cls.modified_nodes.clear()

    @classmethod
    def register_copy_result(cls, original_name: str, copy_name: str):
        cls.original_to_copy_map[original_name] = copy_name
        if copy_name not in cls.created_copies:
            cls.created_copies.append(copy_name)

    @staticmethod
    def _looks_like_runtime_object_name(object_name: str) -> bool:
        if not object_name:
            return False

        return (
            object_name.endswith('_temp')
            or object_name.endswith('_copy')
            or object_name.startswith('TEMP_SUBMESH_MERGED_')
            or re.search(r'_chain\d+$', object_name) is not None
            or re.search(r'_dup\d+$', object_name) is not None
        )

    @classmethod
    def _iter_runtime_name_candidates(cls, object_name: str):
        if not object_name:
            return

        queue = [object_name]
        seen = set()
        strip_patterns = (
            re.compile(r'_temp$'),
            re.compile(r'_copy$'),
            re.compile(r'_chain\d+$'),
            re.compile(r'_dup\d+$'),
        )

        while queue:
            current_name = queue.pop(0)
            if not current_name or current_name in seen:
                continue

            seen.add(current_name)
            yield current_name

            for pattern in strip_patterns:
                stripped_name = pattern.sub('', current_name)
                if stripped_name and stripped_name != current_name and stripped_name not in seen:
                    queue.append(stripped_name)

    @staticmethod
    def _resolve_object_id(object_name: str, fallback_object_id: str = "") -> str:
        if object_name:
            obj = bpy.data.objects.get(object_name)
            if obj:
                return str(obj.as_pointer())
        return fallback_object_id or ""

    @classmethod
    def _resolve_runtime_reference_target(cls, current_name: str, fallback_name: str = "") -> Optional[str]:
        for candidate_name in cls._iter_runtime_name_candidates(current_name):
            if candidate_name != current_name and bpy.data.objects.get(candidate_name):
                return candidate_name

        for candidate_name in cls._iter_runtime_name_candidates(fallback_name):
            if bpy.data.objects.get(candidate_name):
                return candidate_name

        return None

    @staticmethod
    def _sanitize_name_fragment(name: str, fallback: str) -> str:
        safe_name = re.sub(r'[^A-Za-z0-9_]+', '_', name or '').strip('_')
        return safe_name[:24] or fallback

    @classmethod
    def _build_vg_match_copy_collection_name(cls, tree_name: str, node_name: str) -> str:
        safe_tree = cls._sanitize_name_fragment(tree_name, "Tree")
        safe_node = cls._sanitize_name_fragment(node_name, "Node")
        return f"{cls._VG_MATCH_COPY_COLLECTION_PREFIX}_{safe_tree}_{safe_node}"

    @classmethod
    def _ensure_vg_match_copy_collection(
        cls,
        tree_name: str,
        node_name: str,
        copy_names: List[str],
    ) -> str:
        collection_name = cls._build_vg_match_copy_collection_name(tree_name, node_name)
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(collection)
        elif bpy.context.scene.collection.children.get(collection.name) is None:
            bpy.context.scene.collection.children.link(collection)

        for obj in list(collection.objects):
            collection.objects.unlink(obj)

        for copy_name in copy_names:
            copy_obj = bpy.data.objects.get(copy_name)
            if copy_obj and collection.objects.get(copy_obj.name) is None:
                collection.objects.link(copy_obj)

        return collection.name

    @classmethod
    def _remove_collection_if_exists(cls, collection_name: str):
        if not collection_name:
            return

        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            return

        for parent_collection in bpy.data.collections:
            if collection.name in parent_collection.children:
                parent_collection.children.unlink(collection)

        scene_children = bpy.context.scene.collection.children
        if collection.name in scene_children:
            scene_children.unlink(collection)

        bpy.data.collections.remove(collection)

    @classmethod
    def recover_blueprint_node_references(cls, tree, nested_trees: List = None):
        trees_to_update = [tree]
        if nested_trees:
            trees_to_update.extend(nested_trees)

        recovered_count = 0

        for current_tree in trees_to_update:
            for node in current_tree.nodes:
                if node.mute:
                    continue

                if node.bl_idname == 'SSMTNode_Object_Info':
                    current_name = getattr(node, 'object_name', '')
                    fallback_name = getattr(node, 'original_object_name', '')
                    current_obj = bpy.data.objects.get(current_name) if current_name else None

                    should_recover = current_obj is None or (
                        fallback_name and cls._looks_like_runtime_object_name(current_name)
                    )
                    if not should_recover:
                        continue

                    target_name = cls._resolve_runtime_reference_target(current_name, fallback_name)
                    if not target_name or target_name == current_name:
                        continue

                    node.object_name = target_name
                    node.object_id = cls._resolve_object_id(target_name, getattr(node, 'object_id', ''))
                    recovered_count += 1

                elif node.bl_idname == 'SSMTNode_MultiFile_Export':
                    node_changed = False
                    for item in getattr(node, 'object_list', []):
                        current_name = getattr(item, 'object_name', '')
                        fallback_name = getattr(item, 'original_object_name', '')
                        current_obj = bpy.data.objects.get(current_name) if current_name else None

                        should_recover = current_obj is None or (
                            fallback_name and cls._looks_like_runtime_object_name(current_name)
                        )
                        if not should_recover:
                            continue

                        target_name = cls._resolve_runtime_reference_target(current_name, fallback_name)
                        if not target_name or target_name == current_name:
                            continue

                        item.object_name = target_name
                        item.object_id = cls._resolve_object_id(target_name, getattr(item, 'object_id', ''))
                        recovered_count += 1
                        node_changed = True

                    if node_changed:
                        node.update_node_width([item.object_name for item in getattr(node, 'object_list', [])])

                elif node.bl_idname == 'SSMTNode_VertexGroupMatch':
                    source_object = getattr(node, 'source_object', '')
                    target_object = getattr(node, 'target_object', '')

                    resolved_source = cls._resolve_runtime_reference_target(source_object, source_object)
                    if resolved_source and resolved_source != source_object:
                        node.source_object = resolved_source
                        recovered_count += 1

                    resolved_target = cls._resolve_runtime_reference_target(target_object, target_object)
                    if resolved_target and resolved_target != target_object:
                        node.target_object = resolved_target
                        recovered_count += 1

        if recovered_count > 0:
            LOG.info(f"🔧 自动恢复失效节点引用: {recovered_count} 个")

    @classmethod
    def collect_target_object_names(cls, object_names: List[str]) -> List[str]:
        return cls.collect_target_object_names_strict(object_names)

    @classmethod
    def collect_target_object_names_strict(cls, object_names: List[str]) -> List[str]:
        # 这里严格保持“只处理已连到输出端的物体”，避免断开的多文件节点把无关物体带进前处理。
        unique_objects = []
        seen_objects = set()
        for obj_name in object_names:
            if not obj_name or obj_name in seen_objects:
                continue
            seen_objects.add(obj_name)
            unique_objects.append(obj_name)
        return unique_objects

    @classmethod
    def execute_objects_without_cache(cls, object_names: List[str]) -> Dict[str, str]:
        cls.reset_runtime_state()
        cls._execute_preprocess_without_cache(object_names)
        return dict(cls.original_to_copy_map)

    @classmethod
    def execute_objects_without_cache_preserve_shape_keys(cls, object_names: List[str]) -> Dict[str, str]:
        cls.reset_runtime_state()
        cls._execute_preprocess_preserve_shape_keys_without_cache(object_names)
        return dict(cls.original_to_copy_map)

    @classmethod
    def execute_preprocess(cls, object_names: List[str]) -> Dict[str, str]:
        cls.reset_runtime_state()

        unique_objects = cls.collect_target_object_names_strict(object_names)
        
        cache_enabled = GlobalProterties.enable_preprocess_cache()
        
        if cache_enabled:
            cls._execute_preprocess_with_cache(unique_objects)
        else:
            cls._execute_preprocess_without_cache(unique_objects)
        
        LOG.info(f"🔧 前处理完成: {len(unique_objects)} 个物体")
        
        return cls.original_to_copy_map

    @classmethod
    def execute_preprocess_preserve_shape_keys(cls, object_names: List[str]) -> Dict[str, str]:
        cls.reset_runtime_state()

        unique_objects = cls.collect_target_object_names_strict(object_names)
        if GlobalProterties.enable_preprocess_cache():
            cls._execute_preprocess_preserve_shape_keys_with_cache(unique_objects)
        else:
            cls._execute_preprocess_preserve_shape_keys_without_cache(unique_objects)
        LOG.info(f"保留形态键前处理完成: {len(unique_objects)} 个物体")
        return cls.original_to_copy_map

    @classmethod
    def execute_preprocess_capture_shape_keys(cls, object_names: List[str]) -> Dict[str, str]:
        cls.reset_runtime_state()
        BlueprintExportHelper.clear_direct_shapekey_position_records()

        unique_objects = cls.collect_target_object_names_strict(object_names)
        if GlobalProterties.enable_preprocess_cache():
            cls._execute_preprocess_capture_shape_keys_with_cache(unique_objects)
        else:
            cls._execute_preprocess_capture_shape_keys_without_cache(unique_objects)

        LOG.info(f"直出形态键前处理完成: {len(unique_objects)} 个物体")
        return dict(cls.original_to_copy_map)

    @classmethod
    def _build_preserve_shapekey_cache_hash(cls, object_name: str) -> str:
        base_hash = PreProcessCache.compute_object_hash(object_name)
        if not base_hash:
            return ""
        return f"{cls.PRESERVE_SHAPEKEY_CACHE_PREFIX}{base_hash}"

    @classmethod
    def _build_direct_shapekey_cache_hash(cls, object_name: str) -> str:
        base_hash = PreProcessCache.compute_object_hash(object_name)
        if not base_hash:
            return ""
        return f"{cls.DIRECT_SHAPEKEY_CACHE_PREFIX}{base_hash}"

    @classmethod
    def _has_direct_shapekey_cache_entry(cls, hash_value: str) -> bool:
        return bool(
            hash_value
            and PreProcessCache.has_cache(hash_value)
            and PreProcessCache.has_sidecar(hash_value, cls.DIRECT_SHAPEKEY_RECORDS_SUFFIX)
        )

    @classmethod
    def _collect_direct_shapekey_records_for_object(cls, original_name: str) -> dict:
        copy_name = cls.original_to_copy_map.get(original_name, "")
        if not copy_name:
            return {}

        aliases = cls._direct_shapekey_record_aliases(copy_name)
        records = BlueprintExportHelper.get_direct_shapekey_position_records()
        return {
            alias: records.get(alias)
            for alias in aliases
            if alias in records
        }

    @classmethod
    def _save_direct_shapekey_cache_records(cls, original_name: str, hash_value: str) -> bool:
        record_payload = cls._collect_direct_shapekey_records_for_object(original_name)
        return PreProcessCache.save_pickle_sidecar(
            hash_value,
            cls.DIRECT_SHAPEKEY_RECORDS_SUFFIX,
            record_payload,
        )

    @classmethod
    def _load_direct_shapekey_cache_records(cls, hash_value: str) -> bool:
        records = PreProcessCache.load_pickle_sidecar(hash_value, cls.DIRECT_SHAPEKEY_RECORDS_SUFFIX)
        if records is None:
            return False
        if records:
            BlueprintExportHelper.merge_direct_shapekey_position_records(records)
        return True

    @classmethod
    def _execute_preprocess_preserve_shape_keys_without_cache(cls, object_names: List[str]):
        copy_shape_key_state_map = {}
        TimerUtils.start_stage("Preprocess-CreateCopies")
        cls._create_object_copies(
            object_names,
            copy_shape_key_state_map=copy_shape_key_state_map,
        )
        TimerUtils.end_stage("Preprocess-CreateCopies")
        cls._run_preserve_shape_key_copy_pipeline(
            list(cls.original_to_copy_map.values()),
            copy_shape_key_state_map=copy_shape_key_state_map,
        )

    @classmethod
    def _execute_preprocess_preserve_shape_keys_with_cache(cls, object_names: List[str]):
        hash_map = {}
        cached_objects = {}
        uncached_objects = []

        TimerUtils.start_stage("Preprocess-CacheHash")
        LOG.info("正在计算保留形态键前处理缓存哈希...")
        for obj_name in object_names:
            hash_value = cls._build_preserve_shapekey_cache_hash(obj_name)
            hash_map[obj_name] = hash_value
            if hash_value and PreProcessCache.has_cache(hash_value):
                cached_objects[obj_name] = hash_value
            else:
                uncached_objects.append(obj_name)
        TimerUtils.end_stage("Preprocess-CacheHash")

        if cached_objects:
            LOG.info(f"保留形态键前处理缓存命中: {len(cached_objects)} 个物体")
        if uncached_objects:
            LOG.info(f"保留形态键前处理缓存未命中: {len(uncached_objects)} 个物体")

        failed_cached_objects = []
        loaded_from_single_cache = False
        TimerUtils.start_stage("Preprocess-LoadCache")
        bundle_loaded_objects = set()
        pending_cached_objects = dict(cached_objects)
        if PreProcessCache.ENABLE_CACHE_BUNDLES:
            bundle_loaded_objects, pending_cached_objects = PreProcessCache.load_batch_from_cache_bundle(cached_objects)
        for obj_name in bundle_loaded_objects:
            cls.register_copy_result(obj_name, f"{obj_name}_copy")

        for obj_name, hash_value in pending_cached_objects.items():
            success = PreProcessCache.load_from_cache(obj_name, hash_value)
            if success:
                cls.register_copy_result(obj_name, f"{obj_name}_copy")
                loaded_from_single_cache = True
            else:
                LOG.warning(f"保留形态键缓存加载失败 {obj_name}，将重新构建")
                failed_cached_objects.append(obj_name)
        TimerUtils.end_stage("Preprocess-LoadCache")

        if PreProcessCache.ENABLE_CACHE_BUNDLES and loaded_from_single_cache and not failed_cached_objects:
            PreProcessCache.save_cache_bundle(cached_objects)

        uncached_objects.extend(failed_cached_objects)
        if not uncached_objects:
            return

        TimerUtils.start_stage("Preprocess-CreateCopies")
        copy_shape_key_state_map = {}
        cls._create_object_copies(
            uncached_objects,
            hash_map=hash_map,
            copy_shape_key_state_map=copy_shape_key_state_map,
        )
        TimerUtils.end_stage("Preprocess-CreateCopies")

        copy_names = [
            cls.original_to_copy_map[name]
            for name in uncached_objects
            if name in cls.original_to_copy_map
        ]
        cls._run_preserve_shape_key_copy_pipeline(
            copy_names,
            copy_shape_key_state_map=copy_shape_key_state_map,
        )

        cache_items = [
            (obj_name, cls.original_to_copy_map[obj_name], hash_map.get(obj_name, ""))
            for obj_name in uncached_objects
            if obj_name in cls.original_to_copy_map and hash_map.get(obj_name, "")
        ]
        if cache_items:
            LOG.info("正在写入保留形态键前处理缓存...")
            PreProcessCache.batch_save_to_cache(cache_items)

        bundle_objects = {
            obj_name: hash_value
            for obj_name, hash_value in hash_map.items()
            if hash_value and obj_name in cls.original_to_copy_map
        }
        if PreProcessCache.ENABLE_CACHE_BUNDLES and len(bundle_objects) > 1:
            PreProcessCache.save_cache_bundle(bundle_objects)

    @classmethod
    def _run_preserve_shape_key_copy_pipeline(
        cls,
        copy_names: List[str],
        copy_shape_key_state_map: Optional[Dict[str, List[dict]]] = None,
    ):
        # 保留 ShapeKey 的前处理会先清空副本值，处理完几何后再恢复，避免副本烘焙时把当前混合态写死。
        if copy_shape_key_state_map:
            TimerUtils.start_stage("Preprocess-ClearShapeKeys")
            cls._clear_shape_key_values_on_copies(copy_shape_key_state_map)
            TimerUtils.end_stage("Preprocess-ClearShapeKeys")

        LOG.info("正在应用约束...")
        TimerUtils.start_stage("Preprocess-ApplyConstraints")
        cls._apply_constraints(copy_names)
        TimerUtils.end_stage("Preprocess-ApplyConstraints")

        LOG.info("正在应用修改器...")
        TimerUtils.start_stage("Preprocess-ApplyModifiers")
        cls._apply_modifiers(copy_names, fail_on_error=True)
        TimerUtils.end_stage("Preprocess-ApplyModifiers")

        LOG.info("正在三角化网格...")
        TimerUtils.start_stage("Preprocess-Triangulate")
        cls._triangulate_objects(copy_names)
        TimerUtils.end_stage("Preprocess-Triangulate")

        LOG.info("正在应用变换...")
        TimerUtils.start_stage("Preprocess-ApplyTransforms")
        cls._apply_transforms(copy_names)
        TimerUtils.end_stage("Preprocess-ApplyTransforms")

        if copy_shape_key_state_map:
            TimerUtils.start_stage("Preprocess-RestoreShapeKeys")
            cls._restore_shape_key_values_on_copies(copy_shape_key_state_map)
            TimerUtils.end_stage("Preprocess-RestoreShapeKeys")

        LOG.info("正在重命名活动 UV 到 TEXCOORD.xy...")
        TimerUtils.start_stage("Preprocess-RenameUV")
        cls._rename_uv_layers(copy_names)
        TimerUtils.end_stage("Preprocess-RenameUV")

        if GlobalProterties.enable_non_mirror_workflow():
            LOG.info("正在恢复非镜像工作流数据...")
            TimerUtils.start_stage("Preprocess-NonMirrorRestore")
            cls._restore_non_mirror_objects(copy_names)
            TimerUtils.end_stage("Preprocess-NonMirrorRestore")
        cls._refresh_direct_shape_key_loop_indices(copy_names)

    @classmethod
    def _execute_preprocess_capture_shape_keys_without_cache(cls, object_names: List[str]):
        BlueprintExportHelper.set_capture_direct_shapekey_positions(True)
        try:
            cls._execute_preprocess_without_cache(object_names)
        finally:
            BlueprintExportHelper.set_capture_direct_shapekey_positions(False)

    @classmethod
    def _execute_preprocess_capture_shape_keys_with_cache(cls, object_names: List[str]):
        hash_map = {}
        cached_objects = {}
        uncached_objects = []

        TimerUtils.start_stage("Preprocess-CacheHash")
        LOG.info("正在计算直出形态键前处理缓存哈希...")
        for obj_name in object_names:
            hash_value = cls._build_direct_shapekey_cache_hash(obj_name)
            hash_map[obj_name] = hash_value
            if cls._has_direct_shapekey_cache_entry(hash_value):
                cached_objects[obj_name] = hash_value
            else:
                uncached_objects.append(obj_name)
        TimerUtils.end_stage("Preprocess-CacheHash")

        if cached_objects:
            LOG.info(f"直出形态键前处理缓存命中: {len(cached_objects)} 个物体")
        if uncached_objects:
            LOG.info(f"直出形态键前处理缓存未命中: {len(uncached_objects)} 个物体")

        failed_cached_objects = []
        TimerUtils.start_stage("Preprocess-LoadCache")
        for obj_name, hash_value in cached_objects.items():
            copy_name = f"{obj_name}_copy"
            if not PreProcessCache.load_from_cache(obj_name, hash_value):
                failed_cached_objects.append(obj_name)
                continue

            if not cls._load_direct_shapekey_cache_records(hash_value):
                PreProcessCache.remove_runtime_copy(copy_name)
                failed_cached_objects.append(obj_name)
                continue

            cls.register_copy_result(obj_name, copy_name)
        TimerUtils.end_stage("Preprocess-LoadCache")

        uncached_objects.extend(failed_cached_objects)
        if not uncached_objects:
            return

        BlueprintExportHelper.set_capture_direct_shapekey_positions(True)
        try:
            TimerUtils.start_stage("Preprocess-CreateCopies")
            copy_shape_key_state_map = {}
            cls._create_object_copies(
                uncached_objects,
                hash_map=hash_map,
                copy_shape_key_state_map=copy_shape_key_state_map,
            )
            TimerUtils.end_stage("Preprocess-CreateCopies")

            copy_names = [
                cls.original_to_copy_map[name]
                for name in uncached_objects
                if name in cls.original_to_copy_map
            ]
            cls._run_standard_copy_pipeline(
                copy_names=copy_names,
                copy_shape_key_state_map=copy_shape_key_state_map,
                capture_shape_keys=True,
            )
        finally:
            BlueprintExportHelper.set_capture_direct_shapekey_positions(False)

        cache_items = [
            (obj_name, cls.original_to_copy_map[obj_name], hash_map.get(obj_name, ""))
            for obj_name in uncached_objects
            if obj_name in cls.original_to_copy_map and hash_map.get(obj_name, "")
        ]
        if cache_items:
            LOG.info("正在写入直出形态键前处理缓存...")
            PreProcessCache.batch_save_to_cache(cache_items)

        for obj_name in uncached_objects:
            hash_value = hash_map.get(obj_name, "")
            if not hash_value or obj_name not in cls.original_to_copy_map:
                continue
            if not cls._save_direct_shapekey_cache_records(obj_name, hash_value):
                LOG.warning(f"直出形态键采样记录缓存保存失败: {obj_name}")

    @classmethod
    def _run_standard_copy_pipeline(
        cls,
        copy_names: List[str],
        copy_shape_key_state_map: Optional[Dict[str, List[dict]]] = None,
        capture_shape_keys: bool = False,
    ):
        # 普通前处理、保留 ShapeKey 前处理、直出 ShapeKey 前处理都复用这一套副本加工流程。
        if copy_shape_key_state_map:
            TimerUtils.start_stage("Preprocess-ClearShapeKeys")
            cls._clear_shape_key_values_on_copies(copy_shape_key_state_map)
            TimerUtils.end_stage("Preprocess-ClearShapeKeys")

        LOG.info("正在应用约束...")
        cls._apply_constraints(copy_names)

        LOG.info("正在应用修改器...")
        cls._apply_modifiers(copy_names)

        LOG.info("正在三角化网格...")
        cls._triangulate_objects(copy_names)

        LOG.info("正在应用变换...")
        cls._apply_transforms(copy_names)

        if copy_shape_key_state_map:
            TimerUtils.start_stage("Preprocess-RestoreShapeKeys")
            cls._restore_shape_key_values_on_copies(copy_shape_key_state_map)
            TimerUtils.end_stage("Preprocess-RestoreShapeKeys")

        if capture_shape_keys:
            TimerUtils.start_stage("Preprocess-CaptureDirectShapeKeys")
            cls._capture_direct_shape_key_positions(copy_names)
            TimerUtils.end_stage("Preprocess-CaptureDirectShapeKeys")

        TimerUtils.start_stage("Preprocess-ApplyShapeKeys")
        cls._apply_shape_keys(copy_names)
        TimerUtils.end_stage("Preprocess-ApplyShapeKeys")

        TimerUtils.start_stage("Preprocess-RenameUV")
        cls._rename_uv_layers(copy_names)
        TimerUtils.end_stage("Preprocess-RenameUV")

        if GlobalProterties.enable_non_mirror_workflow():
            TimerUtils.start_stage("Preprocess-NonMirrorRestore")
            cls._restore_non_mirror_objects(copy_names)
            TimerUtils.end_stage("Preprocess-NonMirrorRestore")

        cls._refresh_direct_shape_key_loop_indices(copy_names)

    @classmethod
    def _execute_preprocess_without_cache(cls, object_names: List[str]):
        copy_shape_key_state_map = {}
        TimerUtils.start_stage("Preprocess-CreateCopies")
        cls._create_object_copies(
            object_names,
            copy_shape_key_state_map=copy_shape_key_state_map,
        )
        TimerUtils.end_stage("Preprocess-CreateCopies")

        copy_names = list(cls.original_to_copy_map.values())
        cls._run_standard_copy_pipeline(
            copy_names=copy_names,
            copy_shape_key_state_map=copy_shape_key_state_map,
            capture_shape_keys=BlueprintExportHelper.should_capture_direct_shapekey_positions(),
        )

    @classmethod
    def _execute_preprocess_with_cache(cls, object_names: List[str]):
        hash_map = {}
        cached_objects = {}
        uncached_objects = []
        TimerUtils.start_stage("Preprocess-CacheHash")

        LOG.info("正在计算前处理缓存哈希...")
        for obj_name in object_names:
            hash_value = PreProcessCache.compute_object_hash(obj_name)
            hash_map[obj_name] = hash_value
            if hash_value and PreProcessCache.has_cache(hash_value):
                cached_objects[obj_name] = hash_value
            else:
                uncached_objects.append(obj_name)
        TimerUtils.end_stage("Preprocess-CacheHash")

        if cached_objects:
            LOG.info(f"前处理缓存命中: {len(cached_objects)} 个物体")
        if uncached_objects:
            LOG.info(f"前处理缓存未命中: {len(uncached_objects)} 个物体，进入重新处理")

        load_cache_start = perf_counter()
        for obj_name, hash_value in cached_objects.items():
            copy_name = f"{obj_name}_copy"
            success = PreProcessCache.load_from_cache(obj_name, hash_value)
            if success:
                cls.register_copy_result(obj_name, copy_name)
            else:
                LOG.warning(f"缓存加载失败 {obj_name}，将重新处理")
                uncached_objects.append(obj_name)

        LOG.info(f"[CodexTiming] Preprocess-LoadCache {perf_counter() - load_cache_start:.3f}s")
        if uncached_objects:
            copy_shape_key_state_map = {}
            cls._create_object_copies(
                uncached_objects,
                hash_map=hash_map,
                copy_shape_key_state_map=copy_shape_key_state_map,
            )

            copy_names = [cls.original_to_copy_map[name] for name in uncached_objects if name in cls.original_to_copy_map]
            cls._run_standard_copy_pipeline(
                copy_names=copy_names,
                copy_shape_key_state_map=copy_shape_key_state_map,
                capture_shape_keys=BlueprintExportHelper.should_capture_direct_shapekey_positions(),
            )

            LOG.info("正在写入前处理缓存...")
            for obj_name in uncached_objects:
                if obj_name in cls.original_to_copy_map:
                    copy_name = cls.original_to_copy_map[obj_name]
                    hash_value = hash_map.get(obj_name, "")
                    if hash_value:
                        PreProcessCache.save_to_cache(obj_name, copy_name, hash_value)
                    else:
                        LOG.warning(f"缓存保存跳过: {obj_name} 的哈希值为空")

    @classmethod
    def _capture_shape_key_state(cls, obj) -> List[dict]:
        if not obj or obj.type != 'MESH' or not obj.data:
            return []

        shape_keys = getattr(obj.data, 'shape_keys', None)
        key_blocks = getattr(shape_keys, 'key_blocks', None)
        if not key_blocks:
            return []

        return [
            {
                "name": key_block.name,
                "value": float(getattr(key_block, 'value', 0.0)),
                "mute": bool(getattr(key_block, 'mute', False)),
            }
            for key_block in key_blocks
        ]

    @classmethod
    def _apply_shape_key_state(cls, obj, shape_key_state: List[dict]):
        if not obj or obj.type != 'MESH' or not obj.data:
            return

        shape_keys = getattr(obj.data, 'shape_keys', None)
        key_blocks = getattr(shape_keys, 'key_blocks', None)
        if not key_blocks:
            return

        state_by_name = {
            entry.get("name", ""): entry
            for entry in shape_key_state
            if entry.get("name", "")
        }

        for key_block in key_blocks:
            state_entry = state_by_name.get(key_block.name)
            if key_block.name != 'Basis':
                key_block.value = float(state_entry.get("value", 0.0)) if state_entry else 0.0
            if state_entry is not None and hasattr(key_block, 'mute'):
                key_block.mute = bool(state_entry.get("mute", False))

    @classmethod
    def _clear_shape_key_values(cls, obj):
        if not obj or obj.type != 'MESH' or not obj.data:
            return

        shape_keys = getattr(obj.data, 'shape_keys', None)
        key_blocks = getattr(shape_keys, 'key_blocks', None)
        if not key_blocks:
            return

        for key_block in key_blocks:
            if key_block.name != 'Basis':
                key_block.value = 0.0

    @classmethod
    def _iter_non_basis_shape_keys(cls, key_blocks):
        if not key_blocks:
            return

        for key_index, key_block in enumerate(key_blocks):
            if key_index == 0 or key_block.name == 'Basis':
                continue
            yield key_block

    @classmethod
    def _shape_key_values_are_neutral(cls, key_blocks) -> bool:
        # 只有当前可见混合结果仍然等于 Basis 时，才允许直接删除 ShapeKey 而不做 apply_mix。
        for key_block in cls._iter_non_basis_shape_keys(key_blocks):
            if bool(getattr(key_block, 'mute', False)):
                continue

            if abs(float(getattr(key_block, 'value', 0.0))) > cls._SHAPEKEY_VALUE_TOLERANCE:
                return False

        return True

    @classmethod
    def _remove_shape_keys_without_apply_mix(cls, obj):
        shape_keys = getattr(getattr(obj, 'data', None), 'shape_keys', None)
        key_blocks = getattr(shape_keys, 'key_blocks', None)
        basis_key = key_blocks[0] if key_blocks else None
        if basis_key is not None:
            # 快路径删除前先把 Basis 坐标写回网格，保证结果和 apply_mix=True 的基态行为一致。
            basis_coords = numpy.empty((len(basis_key.data), 3), dtype=numpy.float32)
            basis_key.data.foreach_get('co', basis_coords.ravel())
            obj.data.vertices.foreach_set('co', basis_coords.ravel())
            obj.data.update()

        clear_method = getattr(obj, 'shape_key_clear', None)
        if callable(clear_method):
            clear_method()
            remaining_key_blocks = getattr(getattr(getattr(obj, 'data', None), 'shape_keys', None), 'key_blocks', None)
            if not remaining_key_blocks:
                return

        bpy.ops.object.shape_key_remove(all=True, apply_mix=False)

    @classmethod
    def _direct_shapekey_record_aliases(cls, copy_name: str) -> List[str]:
        aliases = [copy_name]
        if copy_name.endswith("_copy"):
            aliases.append(copy_name[:-5])

        for original_name, mapped_copy_name in cls.original_to_copy_map.items():
            if mapped_copy_name == copy_name:
                aliases.append(original_name)
                aliases.append(mapped_copy_name)
                break

        return list(dict.fromkeys(alias for alias in aliases if alias))

    @classmethod
    def _capture_direct_shape_key_positions(cls, object_names: List[str]):
        # 这里采的是“前处理后、真正删除 ShapeKey 前”的最终顶点坐标，供直出资源构建复用。
        captured_objects = 0
        captured_keys = 0
        depsgraph = bpy.context.evaluated_depsgraph_get()

        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj or obj.type != 'MESH' or not obj.data:
                continue

            shape_keys = getattr(obj.data, 'shape_keys', None)
            key_blocks = getattr(shape_keys, 'key_blocks', None)
            if not key_blocks or len(key_blocks) <= 1:
                continue

            non_basis_key_blocks = list(cls._iter_non_basis_shape_keys(key_blocks))
            if not non_basis_key_blocks:
                continue

            all_loop_vertex_indices = numpy.empty(len(obj.data.loops), dtype=numpy.int32)
            obj.data.loops.foreach_get("vertex_index", all_loop_vertex_indices)
            original_values = [float(key_block.value) for key_block in key_blocks]
            aliases = cls._direct_shapekey_record_aliases(obj.name)
            object_key_count = 0
            active_key_block = None

            try:
                for key_block in non_basis_key_blocks:
                    key_block.value = 0.0
                bpy.context.view_layer.update()

                for target_key_block in non_basis_key_blocks:
                    if active_key_block is not None:
                        active_key_block.value = 0.0
                    target_key_block.value = 1.0
                    active_key_block = target_key_block

                    bpy.context.view_layer.update()
                    evaluated_obj = obj.evaluated_get(depsgraph)
                    evaluated_mesh = evaluated_obj.to_mesh()
                    try:
                        coords = numpy.empty((len(evaluated_mesh.vertices), 3), dtype=numpy.float32)
                        evaluated_mesh.vertices.foreach_get("co", coords.ravel())
                    finally:
                        evaluated_obj.to_mesh_clear()

                    if GlobalProterties.enable_non_mirror_workflow():
                        coords[:, 0] *= -1.0

                    BlueprintExportHelper.register_direct_shapekey_position_record(
                        aliases,
                        target_key_block.name,
                        coords,
                        all_loop_vertex_indices,
                    )
                    object_key_count += 1
            finally:
                for key_block, value in zip(key_blocks, original_values):
                    key_block.value = value
                bpy.context.view_layer.update()

            if object_key_count:
                captured_objects += 1
                captured_keys += object_key_count

        LOG.info(f"已采集直出形态键前处理顶点: {captured_objects} 个物体, {captured_keys} 个形态键")

    @classmethod
    def _refresh_direct_shape_key_loop_indices(cls, object_names: List[str]):
        if not BlueprintExportHelper.should_capture_direct_shapekey_positions():
            return

        refreshed_count = 0
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj or obj.type != 'MESH' or not obj.data:
                continue

            loop_vertex_indices = numpy.empty(len(obj.data.loops), dtype=numpy.int32)
            obj.data.loops.foreach_get("vertex_index", loop_vertex_indices)
            aliases = cls._direct_shapekey_record_aliases(obj.name)
            BlueprintExportHelper.update_direct_shapekey_record_loop_indices(aliases, loop_vertex_indices)
            refreshed_count += 1

        if refreshed_count:
            LOG.info(f"已刷新直出形态键最终 Loop 映射: {refreshed_count} 个物体")

    @classmethod
    def _clear_shape_key_values_on_copies(cls, copy_shape_key_state_map: Dict[str, List[dict]]):
        for original_name in copy_shape_key_state_map.keys():
            copy_name = cls.original_to_copy_map.get(original_name, "")
            if not copy_name:
                continue

            copy_obj = bpy.data.objects.get(copy_name)
            if copy_obj is None:
                continue

            cls._clear_shape_key_values(copy_obj)

    @classmethod
    def _restore_shape_key_values_on_copies(cls, copy_shape_key_state_map: Dict[str, List[dict]]):
        for original_name, shape_key_state in copy_shape_key_state_map.items():
            copy_name = cls.original_to_copy_map.get(original_name, "")
            if not copy_name:
                continue

            copy_obj = bpy.data.objects.get(copy_name)
            if copy_obj is None:
                continue

            cls._apply_shape_key_state(copy_obj, shape_key_state)

    @classmethod
    def _needs_triangulation(cls, obj) -> bool:
        if not obj or obj.type != 'MESH' or not obj.data:
            return False

        for polygon in obj.data.polygons:
            if polygon.loop_total != 3:
                return True

        return False

    @classmethod
    def _has_identity_transform(cls, obj) -> bool:
        if not obj:
            return True

        tolerance = cls._TRANSFORM_TOLERANCE

        basis_location, basis_rotation, basis_scale = obj.matrix_basis.decompose()
        if any(abs(float(component)) > tolerance for component in basis_location):
            return False
        if (
            abs(float(basis_rotation.w) - 1.0) > tolerance
            or abs(float(basis_rotation.x)) > tolerance
            or abs(float(basis_rotation.y)) > tolerance
            or abs(float(basis_rotation.z)) > tolerance
        ):
            return False
        if any(abs(float(component) - 1.0) > tolerance for component in basis_scale):
            return False

        if any(abs(float(component)) > tolerance for component in getattr(obj, "delta_location", ())):
            return False

        rotation_mode = getattr(obj, "rotation_mode", "")
        if rotation_mode == 'QUATERNION':
            delta_quaternion = getattr(obj, "delta_rotation_quaternion", None)
            if delta_quaternion and (
                abs(float(delta_quaternion.w) - 1.0) > tolerance
                or abs(float(delta_quaternion.x)) > tolerance
                or abs(float(delta_quaternion.y)) > tolerance
                or abs(float(delta_quaternion.z)) > tolerance
            ):
                return False
        elif rotation_mode == 'AXIS_ANGLE':
            delta_axis_angle = getattr(obj, "delta_rotation_axis_angle", None)
            if delta_axis_angle:
                angle = float(delta_axis_angle[0])
                axis_values = [float(component) for component in delta_axis_angle[1:]]
                if abs(angle) > tolerance or any(abs(component) > tolerance for component in axis_values):
                    return False
        else:
            if any(abs(float(component)) > tolerance for component in getattr(obj, "delta_rotation_euler", ())):
                return False

        delta_scale = getattr(obj, "delta_scale", None)
        if delta_scale and any(abs(float(component) - 1.0) > tolerance for component in delta_scale):
            return False

        return True

    @classmethod
    def _create_object_copies(
        cls,
        object_names: List[str],
        hash_map: Dict[str, str] = None,
        copy_shape_key_state_map: Optional[Dict[str, List[dict]]] = None,
    ):
        created_count = 0
        existing_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj, source_obj_name = PreProcessCache.resolve_source_object(obj_name)
            if not obj:
                LOG.warning(f"   找不到源物体 {obj_name} (解析源名称: {source_obj_name})")
                failed_count += 1
                continue

            source_shape_key_state = []
            shape_key_state_cleared = False
            try:
                if copy_shape_key_state_map is not None:
                    source_shape_key_state = cls._capture_shape_key_state(obj)
                    if source_shape_key_state:
                        copy_shape_key_state_map[obj_name] = source_shape_key_state
                        cls._clear_shape_key_values(obj)
                        shape_key_state_cleared = True

                copy_name = f"{obj_name}_copy"
                expected_hash = hash_map.get(obj_name, "") if hash_map else ""

                existing_copy = bpy.data.objects.get(copy_name)
                if existing_copy:
                    if PreProcessCache.runtime_copy_matches(existing_copy, obj_name, source_obj_name, expected_hash):
                        existing_count += 1
                        cls.register_copy_result(obj_name, copy_name)
                        continue

                    PreProcessCache.remove_runtime_copy(copy_name)

                obj_copy = obj.copy()
                obj_copy.name = copy_name

                if obj.data:
                    obj_copy.data = obj.data.copy()

                bpy.context.scene.collection.objects.link(obj_copy)
                PreProcessCache.tag_runtime_copy(obj_copy, obj_name, source_obj_name, expected_hash)

                original_shapekey_count = 0
                if obj.data.shape_keys:
                    original_shapekey_count = len(obj.data.shape_keys.key_blocks)
                copy_shapekey_count = 0
                if obj_copy.data.shape_keys:
                    copy_shapekey_count = len(obj_copy.data.shape_keys.key_blocks)

                if original_shapekey_count > 0:
                    LOG.info(f"   副本 {copy_name}: 原始形态键 {original_shapekey_count} -> 副本形态键 {copy_shapekey_count}")

                cls.register_copy_result(obj_name, copy_name)
                created_count += 1
            finally:
                if shape_key_state_cleared and source_shape_key_state:
                    cls._apply_shape_key_state(obj, source_shape_key_state)
        
        LOG.info(f"📋 创建副本: 成功 {created_count} 个, 已存在 {existing_count} 个, 失败 {failed_count} 个")

    @classmethod
    def _apply_constraints(cls, object_names: List[str]):
        applied_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            if not obj.constraints:
                continue
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            constraint_names = [c.name for c in obj.constraints]
            for constraint_name in constraint_names:
                try:
                    bpy.ops.object.constraint_apply(constraint=constraint_name)
                    applied_count += 1
                except Exception as e:
                    LOG.warning(f"   应用约束失败 {obj_name}.{constraint_name}: {e}")
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        LOG.info(f"   ✅ 应用约束: {applied_count} 个")

    @classmethod
    def _apply_modifiers(cls, object_names: List[str], fail_on_error: bool = False):
        applied_count = 0
        removed_disabled_count = 0
        failed_count = 0
        shapekey_count = 0
        no_modifier_count = 0
        failure_messages = []
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            if obj.type != 'MESH':
                LOG.info(f"   {obj_name}: 非网格物体，跳过")
                continue
            
            modifier_count = len(obj.modifiers)
            has_shape_keys = obj.data.shape_keys and len(obj.data.shape_keys.key_blocks) > 0
            shape_key_count_before = len(obj.data.shape_keys.key_blocks) if has_shape_keys else 0
            modifier_names = [m.name for m in obj.modifiers if m.show_viewport]
            disabled_modifier_names = [m.name for m in obj.modifiers if not m.show_viewport]
            
            if modifier_count == 0:
                no_modifier_count += 1
                LOG.info(f"   {obj_name}: 无修改器 (形态键: {shape_key_count_before})")
                continue

            if not modifier_names and not disabled_modifier_names:
                no_modifier_count += 1
                LOG.info(f"   {obj_name}: 修改器均不可用，跳过 (形态键: {shape_key_count_before})")
                continue
            
            LOG.info(f"   {obj_name}: {modifier_count} 个修改器, {shape_key_count_before} 个形态键")
            
            original_active = bpy.context.view_layer.objects.active
            did_select_object = False
            if modifier_names:
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                did_select_object = True
            
            if has_shape_keys:
                if modifier_names:
                    # Use the conservative path for shape-key meshes. The
                    # optimized path only validates vertex-count stability and
                    # has been producing semantically wrong geometry for several
                    # modifier types in real exports.
                    success, error = ShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(
                        bpy.context, modifier_names, disable_armatures=False
                    )
                    if success:
                        applied_count += len(modifier_names)
                        shapekey_count += 1
                        shape_key_count_after = len(obj.data.shape_keys.key_blocks) if obj.data.shape_keys else 0
                        LOG.info(f"   ✅ {obj_name}: 修改器应用成功，形态键 {shape_key_count_before} -> {shape_key_count_after}")
                    else:
                        failure_message = f"{obj_name}: shape-key modifier apply failed - {error}"
                        LOG.warning(f"   ❌ {failure_message}")
                        failure_messages.append(failure_message)
                        failed_count += 1
            else:
                for modifier_name in modifier_names:
                    try:
                        bpy.ops.object.modifier_apply(modifier=modifier_name)
                        applied_count += 1
                    except Exception as e:
                        failure_message = f"{obj_name}.{modifier_name}: modifier apply failed - {e}"
                        LOG.warning(f"   ❌ {failure_message}")
                        failure_messages.append(failure_message)
                        failed_count += 1
            
            for mod_name in reversed(disabled_modifier_names):
                try:
                    mod = obj.modifiers.get(mod_name)
                    if mod is not None:
                        obj.modifiers.remove(mod)
                        removed_disabled_count += 1
                except Exception as e:
                    LOG.warning(f"   ⚠️ {obj_name}: 删除禁用修改器 {mod_name} 失败 - {e}")
            
            if disabled_modifier_names:
                LOG.info(f"   🗑️ {obj_name}: 已删除 {len(disabled_modifier_names)} 个禁用修改器")
            
            if did_select_object and original_active:
                bpy.context.view_layer.objects.active = original_active
        
        if removed_disabled_count > 0:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个, 删除禁用修改器: {removed_disabled_count} 个")
        elif shapekey_count > 0:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个 (含 {shapekey_count} 个有形态键物体)")
        elif no_modifier_count > 0:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个 ({no_modifier_count} 个物体无修改器)")
        else:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个")

        if fail_on_error and failure_messages:
            details = "\n".join(failure_messages[:10])
            if len(failure_messages) > 10:
                details += f"\n... and {len(failure_messages) - 10} more"
            raise RuntimeError(f"Preprocess modifier application failed:\n{details}")

    @classmethod
    def _triangulate_objects(cls, object_names: List[str]):
        triangulated_count = 0
        failed_count = 0
        skipped_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            if obj.type != 'MESH':
                continue

            if not cls._needs_triangulation(obj):
                skipped_count += 1
                continue
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
                bpy.ops.object.mode_set(mode='OBJECT')
                triangulated_count += 1
            except Exception as e:
                LOG.warning(f"   三角化失败 {obj_name}: {e}")
                failed_count += 1
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except Exception:
                    pass
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        if skipped_count > 0:
            LOG.info(f"   ✅ 三角化: {triangulated_count} 个物体, 跳过已全三角物体 {skipped_count} 个")
        else:
            LOG.info(f"   ✅ 三角化: {triangulated_count} 个物体")

    @classmethod
    def _apply_transforms(cls, object_names: List[str]):
        applied_count = 0
        failed_count = 0
        skipped_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue

            if cls._has_identity_transform(obj):
                skipped_count += 1
                continue
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            try:
                ShapeKeyUtils.transform_apply_preserve_shape_keys(obj, location=True, rotation=True, scale=True)
                applied_count += 1
            except Exception as e:
                LOG.warning(f"   应用变换失败 {obj_name}: {e}")
                failed_count += 1
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        if skipped_count > 0:
            LOG.info(f"   ✅ 应用变换: {applied_count} 个物体, 跳过单位变换物体 {skipped_count} 个")
        else:
            LOG.info(f"   ✅ 应用变换: {applied_count} 个物体")

    @classmethod
    def _apply_shape_keys(cls, object_names: List[str]):
        applied_count = 0
        no_shapekey_count = 0
        failed_count = 0
        fast_clear_count = 0
        baked_mix_count = 0

        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue

            if obj.type != 'MESH':
                continue

            shapekey_count = 0
            if obj.data.shape_keys:
                shapekey_count = len(obj.data.shape_keys.key_blocks)

            if shapekey_count == 0:
                no_shapekey_count += 1
                LOG.info(f"   {obj_name}: 无形态键")
                continue

            LOG.info(f"   {obj_name}: 有 {shapekey_count} 个形态键，准备应用...")

            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)

            try:
                if cls._shape_key_values_are_neutral(obj.data.shape_keys.key_blocks):
                    cls._remove_shape_keys_without_apply_mix(obj)
                    fast_clear_count += 1
                else:
                    bpy.ops.object.shape_key_remove(all=True, apply_mix=True)
                    baked_mix_count += 1
                applied_count += 1
                LOG.info(f"   ✅ {obj_name}: 形态键应用成功")
            except Exception as e:
                LOG.warning(f"   应用形态键失败 {obj_name}: {e}")
                failed_count += 1

            if original_active:
                bpy.context.view_layer.objects.active = original_active

        LOG.info(
            f"   ✅ 应用形态键: {applied_count} 个物体, {no_shapekey_count} 个无形态键, "
            f"快速清理 {fast_clear_count} 个, 混合烘焙 {baked_mix_count} 个"
        )

    @classmethod
    def _rename_uv_layers(cls, object_names: List[str]):
        renamed_count = 0
        skipped_count = 0

        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj or obj.type != 'MESH':
                continue

            uv_layers = obj.data.uv_layers
            if not uv_layers:
                continue

            active_uv = uv_layers.active
            if not active_uv:
                continue

            if active_uv.name == "TEXCOORD.xy":
                skipped_count += 1
                continue

            if "TEXCOORD.xy" in uv_layers:
                uv_layers["TEXCOORD.xy"].name = "_TEXCOORD_xy_conflict"
                LOG.warning(f"   {obj_name}: 名称冲突，已有TEXCOORD.xy UV层，将其临时重命名")

            old_name = active_uv.name
            active_uv.name = "TEXCOORD.xy"
            renamed_count += 1
            LOG.info(f"   {obj_name}: 活动UV '{old_name}' -> 'TEXCOORD.xy'")

        if renamed_count > 0:
            LOG.info(f"   ✅ UV重命名: {renamed_count} 个物体, {skipped_count} 个无需修改")
        elif skipped_count > 0:
            LOG.info(f"   ✅ UV重命名: 所有 {skipped_count} 个物体活动UV已为TEXCOORD.xy，无需修改")

    @classmethod
    def _restore_non_mirror_objects(cls, object_names: List[str]):
        objects = []
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if obj:
                objects.append(obj)
        NonMirrorWorkflowHelper.restore_export_objects(objects)

    @classmethod
    def update_blueprint_node_references(cls, tree, nested_trees: List = None):
        if not cls.original_to_copy_map:
            return
        
        trees_to_update = [tree]
        if nested_trees:
            trees_to_update.extend(nested_trees)
        
        updated_count = 0
        multi_file_updated_count = 0
        multi_ref_objects = {}
        
        for current_tree in trees_to_update:
            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_Object_Info' and not node.mute:
                    original_name = ObjectPrefixHelper.build_virtual_object_name_for_node(node)
                    if original_name.endswith('_copy'):
                        continue
                    if original_name in cls.original_to_copy_map:
                        copy_name = cls.original_to_copy_map[original_name]
                        cls.modified_nodes.append({
                            "tree_name": current_tree.name,
                            "node_name": node.name,
                            "node_type": "object_info",
                            "item_index": None,
                            "original_name": getattr(node, 'object_name', ''),
                            "original_object_id": getattr(node, 'object_id', ''),
                            "original_original_name": getattr(node, 'original_object_name', ''),
                        })
                        node.object_name = copy_name
                        node.object_id = cls._resolve_object_id(copy_name, getattr(node, 'object_id', ''))
                        updated_count += 1
                        
                        if original_name not in multi_ref_objects:
                            multi_ref_objects[original_name] = []
                        multi_ref_objects[original_name].append(f"Object_Info:{node.name}")
                
                elif node.bl_idname == 'SSMTNode_MultiFile_Export' and not node.mute:
                    object_list = getattr(node, 'object_list', [])
                    for item_index, item in enumerate(object_list):
                        original_name = getattr(item, 'object_name', '')
                        if not original_name:
                            continue
                        if original_name.endswith('_copy'):
                            continue
                        if original_name in cls.original_to_copy_map:
                            copy_name = cls.original_to_copy_map[original_name]
                            cls.modified_nodes.append({
                                "tree_name": current_tree.name,
                                "node_name": node.name,
                                "node_type": "multi_file_export",
                                "item_index": item_index,
                                "original_name": original_name,
                                "original_object_id": getattr(item, 'object_id', ''),
                                "original_original_name": getattr(item, 'original_object_name', ''),
                            })
                            item.object_name = copy_name
                            item.object_id = cls._resolve_object_id(copy_name, getattr(item, 'object_id', ''))
                            multi_file_updated_count += 1
                            
                            if original_name not in multi_ref_objects:
                                multi_ref_objects[original_name] = []
                            multi_ref_objects[original_name].append(f"MultiFile_Export:{node.name}")

                elif node.bl_idname == 'SSMTNode_VertexGroupMatch' and not node.mute:
                    source_object = getattr(node, 'source_object', '')
                    target_object = getattr(node, 'target_object', '')
                    source_collection = getattr(node, 'source_collection', '')

                    updated_vg_match = False
                    original_state = {
                        "tree_name": current_tree.name,
                        "node_name": node.name,
                        "node_type": "vertex_group_match",
                        "item_index": None,
                        "original_source_object": source_object,
                        "original_target_object": target_object,
                        "original_source_collection": source_collection,
                        "temp_collection_name": "",
                    }

                    if source_object and not source_object.endswith('_copy') and source_object in cls.original_to_copy_map:
                        node.source_object = cls.original_to_copy_map[source_object]
                        updated_vg_match = True
                        multi_ref_objects.setdefault(source_object, []).append(f"VertexGroupMatch:{node.name}:source")

                    if target_object and not target_object.endswith('_copy') and target_object in cls.original_to_copy_map:
                        node.target_object = cls.original_to_copy_map[target_object]
                        updated_vg_match = True
                        multi_ref_objects.setdefault(target_object, []).append(f"VertexGroupMatch:{node.name}:target")

                    if source_collection:
                        source_collection_data = bpy.data.collections.get(source_collection)
                        copy_member_names = []
                        if source_collection_data:
                            for obj in source_collection_data.all_objects:
                                if not obj or getattr(obj, 'type', '') != 'MESH' or not getattr(obj, 'data', None):
                                    continue
                                copy_name = cls.original_to_copy_map.get(obj.name, "")
                                if copy_name:
                                    copy_member_names.append(copy_name)
                                    multi_ref_objects.setdefault(obj.name, []).append(f"VertexGroupMatch:{node.name}:collection")

                        if copy_member_names:
                            node.source_collection = cls._ensure_vg_match_copy_collection(
                                current_tree.name,
                                node.name,
                                copy_member_names,
                            )
                            original_state["temp_collection_name"] = node.source_collection
                            updated_vg_match = True

                    if updated_vg_match:
                        cls.modified_nodes.append(original_state)
                        updated_count += 1
        
        multi_ref_count = sum(1 for k, v in multi_ref_objects.items() if len(v) > 1)
        
        if multi_file_updated_count > 0:
            LOG.info(f"📋 更新节点引用: Object_Info {updated_count} 个, MultiFile_Export {multi_file_updated_count} 个")
        elif multi_ref_count > 0:
            LOG.info(f"📋 更新节点引用: {updated_count} 个节点 ({multi_ref_count} 个物体被多节点引用)")
        else:
            LOG.info(f"📋 更新节点引用: {updated_count} 个节点")

    @classmethod
    def restore_blueprint_node_references(cls):
        if not cls.modified_nodes:
            return
        
        restored_count = 0
        multi_file_nodes_to_refresh = set()

        for entry in cls.modified_nodes:
            tree_name = entry.get("tree_name", "")
            node_name = entry.get("node_name", "")
            node_type = entry.get("node_type", "")
            item_index = entry.get("item_index")
            original_name = entry.get("original_name", "")
            original_object_id = entry.get("original_object_id", "")
            original_original_name = entry.get("original_original_name", "")

            tree = bpy.data.node_groups.get(tree_name)
            if not tree:
                continue
            
            node = tree.nodes.get(node_name)
            if not node:
                continue
            
            if node_type == 'object_info':
                changed = (
                    getattr(node, 'object_name', '') != original_name
                    or getattr(node, 'object_id', '') != cls._resolve_object_id(original_name, original_object_id)
                    or getattr(node, 'original_object_name', '') != original_original_name
                )
                if changed:
                    node.object_name = original_name
                    node.object_id = cls._resolve_object_id(original_name, original_object_id)
                    node.original_object_name = original_original_name
                    restored_count += 1
            
            elif node_type == 'multi_file_export':
                object_list = getattr(node, 'object_list', [])
                if item_index is None or item_index < 0 or item_index >= len(object_list):
                    continue

                item = object_list[item_index]
                changed = (
                    getattr(item, 'object_name', '') != original_name
                    or getattr(item, 'object_id', '') != cls._resolve_object_id(original_name, original_object_id)
                    or getattr(item, 'original_object_name', '') != original_original_name
                )
                if changed:
                    item.object_name = original_name
                    item.object_id = cls._resolve_object_id(original_name, original_object_id)
                    item.original_object_name = original_original_name
                    restored_count += 1
                    multi_file_nodes_to_refresh.add((tree_name, node_name))

            elif node_type == 'vertex_group_match':
                original_source_object = entry.get("original_source_object", "")
                original_target_object = entry.get("original_target_object", "")
                original_source_collection = entry.get("original_source_collection", "")
                temp_collection_name = entry.get("temp_collection_name", "")

                changed = (
                    getattr(node, 'source_object', '') != original_source_object
                    or getattr(node, 'target_object', '') != original_target_object
                    or getattr(node, 'source_collection', '') != original_source_collection
                )
                if changed:
                    node.source_object = original_source_object
                    node.target_object = original_target_object
                    node.source_collection = original_source_collection
                    restored_count += 1

                if temp_collection_name:
                    cls._remove_collection_if_exists(temp_collection_name)
        
        cls.modified_nodes.clear()

        for tree_name, node_name in multi_file_nodes_to_refresh:
            tree = bpy.data.node_groups.get(tree_name)
            if not tree:
                continue
            node = tree.nodes.get(node_name)
            if node:
                node.update_node_width([item.object_name for item in getattr(node, 'object_list', [])])
        
        if restored_count > 0:
            LOG.info(f"   ✅ 已恢复 {restored_count} 个节点引用")

    @classmethod
    def cleanup_copies(cls, silent=False):
        if not silent:
            LOG.info(f"🧹 开始清理物体副本")
        
        copies_to_remove = []
        for obj in bpy.data.objects:
            obj_name = obj.name
            if obj_name.endswith('_copy_temp') or obj_name.startswith('TEMP_SUBMESH_MERGED_'):
                copies_to_remove.append(obj)
            elif obj_name.endswith('_copy'):
                copies_to_remove.append(obj)
            elif '_chain' in obj_name:
                if (
                    re.search(r'_chain\d+$', obj_name)
                    or re.search(r'_chain\d+_copy$', obj_name)
                    or re.search(r'_chain\d+_copy_temp$', obj_name)
                ):
                    copies_to_remove.append(obj)
            elif '_dup' in obj_name:
                if (
                    re.search(r'_dup\d+$', obj_name)
                    or re.search(r'_dup\d+_copy$', obj_name)
                    or re.search(r'_dup\d+_copy_temp$', obj_name)
                ):
                    copies_to_remove.append(obj)
        
        if not copies_to_remove:
            cls.restore_blueprint_node_references()
            if not silent:
                LOG.info(f"   未找到副本物体")
            return
        
        if not silent:
            LOG.info(f"   找到 {len(copies_to_remove)} 个副本物体")
        
        cleaned_count = 0
        for obj in copies_to_remove:
            try:
                obj_name = obj.name
                
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                    cleaned_count += 1
                except ReferenceError:
                    pass
                    
            except ReferenceError:
                pass
        
        cls.created_copies.clear()
        cls.original_to_copy_map.clear()
        if not silent:
            LOG.info(f"   ✅ 清理完成，删除 {cleaned_count} 个副本")
        
        cls.restore_blueprint_node_references()
        
        cls._cleanup_orphan_data(silent)

    @classmethod
    def _cleanup_orphan_data(cls, silent=False):
        if not silent:
            LOG.info(f"   🧹 清理孤立数据...")
        
        orphan_meshes = [mesh for mesh in bpy.data.meshes if mesh.users == 0]
        orphan_materials = [mat for mat in bpy.data.materials if mat.users == 0]
        orphan_textures = [tex for tex in bpy.data.textures if tex.users == 0]
        orphan_images = [img for img in bpy.data.images if img.users == 0]
        
        mesh_count = len(orphan_meshes)
        mat_count = len(orphan_materials)
        tex_count = len(orphan_textures)
        img_count = len(orphan_images)
        
        for mesh in orphan_meshes:
            bpy.data.meshes.remove(mesh)
        for mat in orphan_materials:
            bpy.data.materials.remove(mat)
        for tex in orphan_textures:
            bpy.data.textures.remove(tex)
        for img in orphan_images:
            bpy.data.images.remove(img)
        
        if not silent:
            if mesh_count > 0 or mat_count > 0 or tex_count > 0 or img_count > 0:
                LOG.info(f"   ✅ 清理孤立数据: {mesh_count} 网格, {mat_count} 材质, {tex_count} 纹理, {img_count} 图像")
            else:
                LOG.info(f"   ✅ 无孤立数据需要清理")

    @classmethod
    def get_copy_name(cls, original_name: str) -> str:
        return cls.original_to_copy_map.get(original_name, original_name)

    @classmethod
    def has_copies(cls) -> bool:
        return len(cls.created_copies) > 0

    @classmethod
    def validate_copy_suffix(cls, object_name: str) -> bool:
        if not cls.has_copies():
            return True

        if object_name.endswith('_copy'):
            return True

        if re.search(r'_chain\d+_copy$', object_name) or re.search(r'_chain\d+$', object_name):
            return True

        if re.search(r'_dup\d+_copy$', object_name) or re.search(r'_dup\d+$', object_name):
            return True

        return False


def register():
    pass


def unregister():
    pass
