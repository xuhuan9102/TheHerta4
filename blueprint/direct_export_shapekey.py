import glob
import os
import shutil
import copy
from collections import OrderedDict
from time import perf_counter

import bpy

from ..utils.log_utils import LOG
from .direct_export_runtime_utils import normalize_runtime_name as _normalize_runtime_name
from .direct_export_shapekey_output_mixin import DirectShapeKeyOutputMixin
from .direct_export_shapekey_runtime_mixin import DirectShapeKeyRuntimeMixin
from .direct_export_shapekey_sampling_mixin import DirectShapeKeySamplingMixin
from .direct_export_shapekey_shared import ShapeKeyDirectExportError
from .export_helper import BlueprintExportHelper


class DirectShapeKeyGenerator(
    DirectShapeKeyOutputMixin,
    DirectShapeKeySamplingMixin,
    DirectShapeKeyRuntimeMixin,
):
    def __init__(self, node, mod_export_path: str, blueprint_model, exporter):
        self.node = node
        self.mod_export_path = mod_export_path
        self.blueprint_model = blueprint_model
        self.exporter = exporter
        # 直出始终以 Meshes0000 作为基底，再派生各个形态键槽位资源。
        self.meshes_dir = os.path.join(mod_export_path, "Meshes0000")
        self.merged_name_members = {}
        self._processing_chain_alias_lookup = None

    def _iter_name_variants(self, name: str):
        if not name:
            return

        candidate_names = [
            name,
            _normalize_runtime_name(name),
            self._extract_original_name(name),
            self._extract_original_name(_normalize_runtime_name(name)),
        ]

        seen = set()
        for candidate_name in candidate_names:
            if candidate_name and candidate_name not in seen:
                seen.add(candidate_name)
                yield candidate_name

    def _iter_chain_aliases(self, chain):
        raw_names = [
            getattr(chain, "object_name", "") or "",
            getattr(chain, "original_object_name", "") or "",
            getattr(chain, "virtual_object_name", "") or "",
            getattr(chain, "export_object_name_override", "") or "",
        ]

        get_export_object_name = getattr(chain, "get_export_object_name", None)
        if callable(get_export_object_name):
            try:
                raw_names.append(get_export_object_name() or "")
            except Exception:
                pass

        for rename_record in getattr(chain, "rename_history", []) or []:
            raw_names.append(rename_record.get("old_name", "") or "")
            raw_names.append(rename_record.get("new_name", "") or "")

        seen = set()
        for raw_name in raw_names:
            for candidate_name in self._iter_name_variants(raw_name):
                if candidate_name and candidate_name not in seen:
                    seen.add(candidate_name)
                    yield candidate_name

    def _resolve_chain_source_name(self, chain) -> str:
        preferred_names = []
        preferred_names.append(getattr(chain, "original_object_name", "") or "")
        preferred_names.append(getattr(chain, "object_name", "") or "")
        preferred_names.append(getattr(chain, "virtual_object_name", "") or "")
        preferred_names.append(getattr(chain, "export_object_name_override", "") or "")

        get_export_object_name = getattr(chain, "get_export_object_name", None)
        if callable(get_export_object_name):
            try:
                preferred_names.append(get_export_object_name() or "")
            except Exception:
                pass

        for rename_record in getattr(chain, "rename_history", []) or []:
            preferred_names.append(rename_record.get("old_name", "") or "")
            preferred_names.append(rename_record.get("new_name", "") or "")

        fallback_name = ""
        for candidate_name in preferred_names:
            for variant_name in self._iter_name_variants(candidate_name):
                if not fallback_name:
                    fallback_name = variant_name

                source_obj = bpy.data.objects.get(variant_name)
                if source_obj is None:
                    continue

                shape_keys = getattr(getattr(source_obj.data, "shape_keys", None), "key_blocks", None)
                if shape_keys and len(shape_keys) > 1:
                    return variant_name

        return fallback_name

    def _get_processing_chain_alias_lookup(self):
        if self._processing_chain_alias_lookup is not None:
            return self._processing_chain_alias_lookup

        alias_lookup = {}
        for chain in getattr(self.blueprint_model, "processing_chains", []) or []:
            if not getattr(chain, "is_valid", False) or not getattr(chain, "reached_output", False):
                continue

            alias_names = list(self._iter_chain_aliases(chain))
            if not alias_names:
                continue

            for alias_name in alias_names:
                related_names = alias_lookup.setdefault(alias_name, [])
                for related_name in alias_names:
                    if related_name not in related_names:
                        related_names.append(related_name)

        self._processing_chain_alias_lookup = alias_lookup
        return alias_lookup

    def _iter_related_runtime_names(self, obj_name: str):
        alias_lookup = self._get_processing_chain_alias_lookup()
        related_names = []
        seen = set()

        def append_name(candidate_name: str):
            if candidate_name and candidate_name not in seen:
                seen.add(candidate_name)
                related_names.append(candidate_name)

        for candidate_name in self._iter_name_variants(obj_name):
            append_name(candidate_name)

        for candidate_name in list(related_names):
            for related_name in alias_lookup.get(candidate_name, []):
                for variant_name in self._iter_name_variants(related_name):
                    append_name(variant_name)

        return related_names

    def generate(self):
        stage_start = perf_counter()
        classification_text_obj = None
        if self.blueprint_model is not None:
            BlueprintExportHelper.generate_shapekey_classification_report(self.blueprint_model)
        classification_text_obj = next((t for t in bpy.data.texts if "Shape_Key_Classification" in t.name), None)

        if not classification_text_obj:
            raise ShapeKeyDirectExportError("未找到 Shape_Key_Classification 文本，无法执行形态键直出。")

        ini_files = glob.glob(os.path.join(self.mod_export_path, "*.ini"))
        if not ini_files:
            raise ShapeKeyDirectExportError("路径中未找到任何 ini 文件，无法执行形态键直出。")

        target_ini_file = ini_files[0]
        self.node._create_cumulative_backup(target_ini_file, self.mod_export_path)
        sections, preserved_tail_content = self.node._read_ini_to_ordered_dict(target_ini_file)

        slot_to_name_to_objects, unique_hashes, hash_to_objects, all_objects = self.node._parse_classification_text_final(
            classification_text_obj.as_string()
        )
        if not slot_to_name_to_objects:
            raise ShapeKeyDirectExportError("分类文本解析失败或为空，无法执行形态键直出。")

        slot_to_name_to_objects = copy.deepcopy(slot_to_name_to_objects)
        hash_to_objects = {hash_value: list(objects) for hash_value, objects in hash_to_objects.items()}
        all_objects = list(all_objects)

        shader_source_path = self.node._get_shader_source_path()
        if not shader_source_path or not os.path.exists(shader_source_path):
            raise ShapeKeyDirectExportError(f"着色器模板文件未找到: {shader_source_path}")

        source_object_map = self._build_source_object_map()
        static_copy_map = {}

        try:
            use_preprocess_records = bool(BlueprintExportHelper.get_direct_shapekey_position_records())
            if use_preprocess_records:
                runtime_infos = self._build_runtime_infos(unique_hashes)
            else:
                runtime_infos = self._build_runtime_infos_from_exporter_buffers(unique_hashes)
            calculated_ranges = self._calculate_object_ranges(runtime_infos, all_objects, sections=sections)
            unique_hashes = self._apply_merged_ranges(
                calculated_ranges,
                slot_to_name_to_objects,
                hash_to_objects,
                all_objects,
            )
            if use_preprocess_records:
                slot_position_overrides = self._build_slot_position_overrides_from_preprocess_records(
                    slot_to_name_to_objects=slot_to_name_to_objects,
                    calculated_ranges=calculated_ranges,
                    runtime_infos=runtime_infos,
                    source_object_map=source_object_map,
                )
            else:
                slot_position_overrides = self._build_slot_position_overrides_from_exporter_buffers(
                    slot_to_name_to_objects=slot_to_name_to_objects,
                    calculated_ranges=calculated_ranges,
                    runtime_infos=runtime_infos,
                )
            _, _, dropped_slots = self._analyze_hash_slot_filters(
                unique_hashes=unique_hashes,
                slot_to_name_to_objects=slot_to_name_to_objects,
                slot_position_overrides=slot_position_overrides,
            )
            if dropped_slots and not use_preprocess_records:
                LOG.info(
                    "直出形态键: exporter ShapeKey 缓冲路径丢失槽位，"
                    f"改用静态副本补齐: {dropped_slots}"
                )
                slot_position_overrides = self._supplement_dropped_slots_from_static_sampling(
                    unique_hashes=unique_hashes,
                    slot_to_name_to_objects=slot_to_name_to_objects,
                    calculated_ranges=calculated_ranges,
                    source_object_map=source_object_map,
                    runtime_infos=runtime_infos,
                    slot_position_overrides=slot_position_overrides,
                )
            if dropped_slots and use_preprocess_records:
                LOG.warning(f"Direct ShapeKey: missing preprocess-record slots {dropped_slots}")
            LOG.info("Direct ShapeKey: using preprocess-record path" if use_preprocess_records else "直出形态键: 使用 exporter ShapeKey 缓冲路径")
        except ShapeKeyDirectExportError as exc:
            LOG.info(
                f"直出形态键: exporter ShapeKey 缓冲不可用，回退到静态副本采样路径 "
                f"({perf_counter() - stage_start:.3f}s) - {exc}"
            )
            runtime_infos = self._build_runtime_infos(unique_hashes)
            calculated_ranges = self._calculate_object_ranges(runtime_infos, all_objects, sections=sections)
            unique_hashes = self._apply_merged_ranges(
                calculated_ranges,
                slot_to_name_to_objects,
                hash_to_objects,
                all_objects,
            )
            LOG.info(f"直出形态键: 构建 runtime infos + ranges {perf_counter() - stage_start:.3f}s")
            static_copy_map = self._create_static_shapekey_copies(slot_to_name_to_objects, source_object_map, runtime_infos)
            LOG.info(f"直出形态键: 创建静态副本 {perf_counter() - stage_start:.3f}s")
            slot_position_overrides = self._build_slot_position_overrides(
                slot_to_name_to_objects=slot_to_name_to_objects,
                calculated_ranges=calculated_ranges,
                runtime_infos=runtime_infos,
                source_object_map=source_object_map,
                static_copy_map=static_copy_map,
            )
            LOG.info("直出形态键: 已回退到静态副本采样路径")
            LOG.info(f"直出形态键: 槽位 Position 采样完成 {perf_counter() - stage_start:.3f}s")

        raw_hash_slot_data_map = self._build_hash_slot_data_map(unique_hashes, slot_to_name_to_objects)
        hash_slot_data_map = self._filter_hash_slot_data_map_by_overrides(
            raw_hash_slot_data_map,
            slot_position_overrides,
        )
        for logical_hash in unique_hashes:
            raw_slot_data = raw_hash_slot_data_map.get(logical_hash, {})
            filtered_slot_data = hash_slot_data_map.get(logical_hash, {})
            raw_object_count = sum(
                len(objects)
                for slot_data in raw_slot_data.values()
                for objects in slot_data.values()
            )
            filtered_object_count = sum(
                len(objects)
                for slot_data in filtered_slot_data.values()
                for objects in slot_data.values()
            )
            LOG.info(
                f"直出形态键: 哈希 {logical_hash} 原始对象={raw_object_count}, "
                f"有效对象={filtered_object_count}, 原始槽位={sorted(raw_slot_data.keys())}, "
                f"有效槽位={sorted(filtered_slot_data.keys())}"
            )
        LOG.info(f"直出形态键: hash/slot 筛选完成 {perf_counter() - stage_start:.3f}s")
        hash_to_stride = {}
        hash_to_actual_file_hash = {}
        hash_to_vertex_count = {}
        hash_to_slot_maps = {}
        hash_to_merged_index_map = {}

        for logical_hash in unique_hashes:
            runtime_info = runtime_infos.get(logical_hash)
            hash_slot_data = hash_slot_data_map.get(logical_hash, {})
            if not runtime_info or not hash_slot_data:
                continue

            logical_prefix = self.node._extract_hash_prefix(logical_hash)
            actual_hash = runtime_info["actual_hash"]

            hash_to_actual_file_hash[logical_hash] = actual_hash
            if logical_prefix:
                hash_to_stride[logical_prefix] = runtime_info["position_stride"]
                hash_to_vertex_count[logical_prefix] = runtime_info["vertex_count"]

            if self.node._should_merge_slot_files(getattr(self.node, "use_packed_Meshess", False)):
                merged_index_map = self._write_merged_slot_files(
                    logical_hash=logical_hash,
                    runtime_info=runtime_info,
                    hash_slot_data=hash_slot_data,
                    slot_position_overrides=slot_position_overrides,
                )
                hash_to_merged_index_map[logical_hash] = merged_index_map
            else:
                slot_maps = self._write_slot_files(
                    logical_hash=logical_hash,
                    runtime_info=runtime_info,
                    hash_slot_data=hash_slot_data,
                    slot_position_overrides=slot_position_overrides,
                )
                hash_to_slot_maps[logical_hash] = slot_maps
        LOG.info(f"直出形态键: Position 缓冲写出完成 {perf_counter() - stage_start:.3f}s")

        processed_hashes = [logical_hash for logical_hash in unique_hashes if logical_hash in hash_to_actual_file_hash]
        hash_to_base_resources = self._parse_hash_to_base_resources(sections)
        all_unique_names = list(
            OrderedDict.fromkeys(name for slot_data in slot_to_name_to_objects.values() for name in slot_data.keys())
        )
        all_unique_objects = list(
            OrderedDict.fromkeys(
                obj
                for slot_data in slot_to_name_to_objects.values()
                for name_data in slot_data.values()
                for obj in name_data
            )
        )

        dest_res_dir = os.path.join(self.mod_export_path, "res")
        os.makedirs(dest_res_dir, exist_ok=True)

        use_packed = self.node.use_packed_Meshess
        use_delta = self.node.store_deltas
        use_optimized = self.node.use_optimized_lookup
        merge_slot_files = self.node._should_merge_slot_files(use_packed)

        hash_to_shader_paths = {}
        for logical_hash in processed_hashes:
            if logical_hash not in hash_slot_data_map or not hash_slot_data_map[logical_hash]:
                continue
            shader_dest_path = os.path.join(dest_res_dir, f"shapekey_anim_{logical_hash}.hlsl")
            shutil.copy2(shader_source_path, shader_dest_path)
            hash_to_shader_paths[logical_hash] = shader_dest_path

        for logical_hash in processed_hashes:
            hash_slot_data = hash_slot_data_map.get(logical_hash, {})
            if not hash_slot_data:
                continue

            hash_unique_names = list(
                OrderedDict.fromkeys(name for slot_data in hash_slot_data.values() for name in slot_data.keys())
            )
            hash_unique_objects = list(
                OrderedDict.fromkeys(
                    obj
                    for slot_data in hash_slot_data.values()
                    for name_data in slot_data.values()
                    for obj in name_data
                )
            )

            if use_optimized:
                logical_prefix = self.node._extract_hash_prefix(logical_hash)
                vertex_count = hash_to_vertex_count.get(logical_prefix, 0)
                self._write_freq_indices(
                    logical_hash=logical_hash,
                    actual_hash=hash_to_actual_file_hash.get(logical_hash, logical_hash),
                    hash_slot_data=hash_slot_data,
                    unique_names=hash_unique_names,
                    vertex_count=vertex_count,
                    calculated_ranges=calculated_ranges,
                    merged_index_map=hash_to_merged_index_map.get(logical_hash),
                    slot_index_maps=hash_to_slot_maps.get(logical_hash, {}),
                )

            shader_path = hash_to_shader_paths.get(logical_hash)
            if shader_path:
                self.node._update_shader_file(
                    shader_path,
                    hash_slot_data,
                    use_packed,
                    use_delta,
                    hash_unique_names,
                    hash_unique_objects,
                    use_optimized=use_optimized,
                    merge_slot_files=merge_slot_files,
                )
        LOG.info(f"直出形态键: shader/freq 写出完成 {perf_counter() - stage_start:.3f}s")

        self._update_ini_sections(
            sections=sections,
            preserved_tail_content=preserved_tail_content,
            target_ini_file=target_ini_file,
            slot_to_name_to_objects=slot_to_name_to_objects,
            unique_hashes=processed_hashes,
            hash_to_objects=hash_to_objects,
            all_unique_names=all_unique_names,
            all_unique_objects=all_unique_objects,
            calculated_ranges=calculated_ranges,
            hash_to_stride=hash_to_stride,
            hash_to_actual_file_hash=hash_to_actual_file_hash,
            hash_to_vertex_count=hash_to_vertex_count,
            hash_slot_data_map=hash_slot_data_map,
            hash_to_base_resources=hash_to_base_resources,
            use_packed=use_packed,
            use_delta=use_delta,
            use_optimized=use_optimized,
            merge_slot_files=merge_slot_files,
        )
        LOG.info(f"直出形态键: ini 更新完成 {perf_counter() - stage_start:.3f}s")
