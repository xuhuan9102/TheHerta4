import math
import os
import re
from collections import OrderedDict, defaultdict

import bpy
import numpy as np

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.logic_name import LogicName
from ..utils.log_utils import LOG
from ..utils.obj_utils import ObjUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from .direct_export_runtime_utils import extract_position_bytes_by_indices as _extract_position_bytes_by_indices
from .direct_export_runtime_utils import normalize_runtime_name as _normalize_runtime_name
from .direct_export_shapekey_shared import ShapeKeyDirectExportError
from .export_helper import BlueprintExportHelper
from .preprocess import PreProcessHelper


class DirectShapeKeySamplingMixin:
    def _supplement_dropped_slots_from_static_sampling(
        self,
        unique_hashes,
        slot_to_name_to_objects,
        calculated_ranges,
        source_object_map,
        runtime_infos,
        slot_position_overrides,
    ):
        _, _, dropped_slots = self._analyze_hash_slot_filters(
            unique_hashes=unique_hashes,
            slot_to_name_to_objects=slot_to_name_to_objects,
            slot_position_overrides=slot_position_overrides,
        )

        missing_slot_to_name_to_objects = {}
        missing_object_count = 0
        for slot_info in dropped_slots.values():
            for slot_num, names_data in slot_info.get("dropped_entries", {}).items():
                target_names = missing_slot_to_name_to_objects.setdefault(slot_num, {})
                for shapekey_name, objects in names_data.items():
                    deduped_objects = list(OrderedDict.fromkeys(objects))
                    if not deduped_objects:
                        continue
                    target_names[shapekey_name] = deduped_objects
                    missing_object_count += len(deduped_objects)

        if not missing_slot_to_name_to_objects:
            return slot_position_overrides

        static_copy_map = self._create_static_shapekey_copies(
            missing_slot_to_name_to_objects,
            source_object_map,
            runtime_infos,
        )
        try:
            static_slot_position_overrides = self._build_slot_position_overrides(
                slot_to_name_to_objects=missing_slot_to_name_to_objects,
                calculated_ranges=calculated_ranges,
                runtime_infos=runtime_infos,
                source_object_map=source_object_map,
                static_copy_map=static_copy_map,
            )
        finally:
            self._cleanup_static_copies(static_copy_map)

        LOG.info(
            "直出形态键: 静态补齐仅处理缺失对象 "
            f"slot_count={len(missing_slot_to_name_to_objects)}, object_count={missing_object_count}"
        )

        for logical_hash, slot_info in dropped_slots.items():
            missing_entries = slot_info.get("dropped_entries", {})
            for slot_num, names_data in missing_entries.items():
                static_slot_overrides = static_slot_position_overrides.get(slot_num, {})
                target_slot_overrides = slot_position_overrides.setdefault(slot_num, {})
                for objects in names_data.values():
                    for obj_name in objects:
                        static_override = static_slot_overrides.get(obj_name)
                        if static_override is not None:
                            target_slot_overrides[obj_name] = static_override

        return slot_position_overrides

    def _extract_original_name(self, name: str) -> str:
        if not name:
            return name
        patterns = [
            r'_x\d+$',
            r'_copy$',
            r'_chain\d+$',
            r'_dup\d+$',
            r'_chain\d+_copy$',
            r'_dup\d+_copy$',
            r'_chain\d+_dup\d+$',
            r'_chain\d+_dup\d+_copy$',
        ]
        result = name
        for pattern in patterns:
            result = re.sub(pattern, '', result)
        return result

    def _build_source_object_map(self):
        source_object_map = {}
        for chain in getattr(self.blueprint_model, "processing_chains", []) or []:
            if not getattr(chain, "is_valid", False) or not getattr(chain, "reached_output", False):
                continue

            resolved_name = self._resolve_chain_source_name(chain)
            if not resolved_name:
                continue

            for candidate in self._iter_chain_aliases(chain):
                if candidate:
                    source_object_map[candidate] = resolved_name
        return source_object_map

    def _cleanup_static_copies(self, static_copy_map):
        for copy_name in static_copy_map.values():
            copy_obj = bpy.data.objects.get(copy_name)
            if copy_obj is not None:
                bpy.data.objects.remove(copy_obj, do_unlink=True)
        try:
            PreProcessHelper._cleanup_orphan_data(silent=True)
        except Exception:
            pass

    def _matches_runtime_prefix(self, candidate_name: str, expected_prefix: str) -> bool:
        if not expected_prefix:
            return True
        candidate_hash = self.node._extract_hash_from_name(candidate_name)
        candidate_prefix = self.node._extract_hash_prefix(candidate_hash) if candidate_hash else None
        return candidate_prefix in (None, "", expected_prefix)

    def _resolve_object_export_context(self, runtime_info, obj_name):
        lookup = runtime_info.get("object_export_context_lookup", {}) or {}
        expected_prefix = self.node._extract_hash_prefix(runtime_info.get("logical_hash", ""))

        for candidate_name in self._iter_name_variants(obj_name):
            if not self._matches_runtime_prefix(candidate_name, expected_prefix):
                continue
            context = lookup.get(candidate_name)
            if context is not None:
                return context

        for candidate_name in self._iter_related_runtime_names(obj_name):
            if not self._matches_runtime_prefix(candidate_name, expected_prefix):
                continue
            context = lookup.get(candidate_name)
            if context is not None:
                return context
        return None

    def _resolve_object_export_context_with_merged_members(self, runtime_info, obj_name):
        context = self._resolve_object_export_context(runtime_info, obj_name)
        if context is not None:
            return context

        for member_name in self.merged_name_members.get(obj_name, []):
            context = self._resolve_object_export_context(runtime_info, member_name)
            if context is not None:
                return context

        return None

    def _get_position_element_from_game_type(self, d3d11_game_type):
        if d3d11_game_type is None:
            return None
        for d3d11_element in d3d11_game_type.D3D11ElementList:
            if d3d11_element.Category == "Position" and d3d11_element.ElementName == "POSITION":
                return d3d11_element
        return None

    def _iter_record_candidate_names(self, obj_name, object_context, source_object_map):
        candidate_names = []
        seen = set()

        def append_name(candidate_name):
            if not candidate_name or candidate_name in seen:
                return
            seen.add(candidate_name)
            candidate_names.append(candidate_name)

        for member_name in self._iter_merged_member_names(obj_name):
            for related_name in self._iter_related_runtime_names(member_name):
                append_name(related_name)
                append_name(self.node._extract_alias_from_name(related_name))
                append_name(self.node._strip_runtime_copy_suffix(self.node._extract_alias_from_name(related_name)))

                source_name = (
                    source_object_map.get(related_name)
                    or source_object_map.get(_normalize_runtime_name(related_name))
                    or self._extract_original_name(related_name)
                )
                append_name(source_name)
                append_name(f"{source_name}_copy" if source_name else "")

        preferred_source_name = (object_context or {}).get("preferred_source_name", "")
        append_name(preferred_source_name)
        append_name(f"{preferred_source_name}_copy" if preferred_source_name else "")

        return candidate_names

    def _resolve_recorded_shape_key_data(self, obj_name, shapekey_name, object_context, source_object_map):
        records = BlueprintExportHelper.get_direct_shapekey_position_records()
        if not records:
            return None

        for candidate_name in self._iter_record_candidate_names(obj_name, object_context, source_object_map):
            record = records.get(candidate_name)
            if not record:
                continue
            coords = (record.get("shape_keys", {}) or {}).get(shapekey_name)
            loop_vertex_indices = record.get("loop_vertex_indices")
            if coords is not None and loop_vertex_indices is not None:
                return coords, loop_vertex_indices, candidate_name
        return None

    def _build_slot_position_overrides_from_preprocess_records(self, slot_to_name_to_objects, calculated_ranges, runtime_infos, source_object_map):
        slot_position_overrides = defaultdict(dict)
        prefix_to_runtime_info = {}
        base_slice_cache = {}
        skip_reason_counts = defaultdict(lambda: defaultdict(int))

        for logical_hash, runtime_info in runtime_infos.items():
            logical_prefix = self.node._extract_hash_prefix(logical_hash)
            if logical_prefix:
                prefix_to_runtime_info[logical_prefix] = runtime_info

        for slot_index, names_data in slot_to_name_to_objects.items():
            for shapekey_name, objects in names_data.items():
                for obj_name in OrderedDict.fromkeys(objects).keys():
                    if obj_name not in calculated_ranges:
                        continue

                    obj_hash = self.node._extract_hash_from_name(obj_name)
                    obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                    runtime_info = prefix_to_runtime_info.get(obj_prefix)
                    if runtime_info is None:
                        skip_reason_counts[obj_prefix]["missing_runtime_info"] += 1
                        continue

                    object_context = self._resolve_object_export_context_with_merged_members(runtime_info, obj_name)
                    if object_context is None:
                        skip_reason_counts[obj_prefix]["missing_object_context"] += 1
                        continue

                    recorded_data = self._resolve_recorded_shape_key_data(
                        obj_name=obj_name,
                        shapekey_name=shapekey_name,
                        object_context=object_context,
                        source_object_map=source_object_map,
                    )
                    if recorded_data is None:
                        skip_reason_counts[obj_prefix]["missing_record"] += 1
                        continue

                    coords, loop_vertex_indices, _record_name = recorded_data
                    local_loop_indices = np.asarray(object_context.get("local_loop_indices", []), dtype=np.int32)
                    export_indices = np.asarray(object_context.get("export_indices", []), dtype=np.int32)
                    if local_loop_indices.size == 0 or export_indices.size == 0:
                        skip_reason_counts[obj_prefix]["empty_indices"] += 1
                        continue

                    loop_vertex_indices = np.asarray(loop_vertex_indices, dtype=np.int32)
                    if int(local_loop_indices.max()) >= len(loop_vertex_indices):
                        raise ShapeKeyDirectExportError(
                            f"槽位 {slot_index}: 物体 '{obj_name}' 的形态键记录 loop 映射越界"
                        )
                    sampled_vertex_indices = loop_vertex_indices[local_loop_indices]
                    coords = np.asarray(coords, dtype=np.float32)
                    if sampled_vertex_indices.size and int(sampled_vertex_indices.max()) >= len(coords):
                        raise ShapeKeyDirectExportError(
                            f"槽位 {slot_index}: 物体 '{obj_name}' 的形态键记录 vertex 映射越界"
                        )

                    d3d11_game_type = object_context.get("d3d11_game_type")
                    position_element = self._get_position_element_from_game_type(d3d11_game_type)
                    if position_element is None:
                        skip_reason_counts[obj_prefix]["missing_position_element"] += 1
                        continue

                    position_bytes = self._format_position_bytes_from_coords(
                        coords[sampled_vertex_indices],
                        position_element,
                        position_stride=runtime_info["position_stride"],
                    )
                    expected_bytes = export_indices.size * runtime_info["position_stride"]
                    if len(position_bytes) != expected_bytes:
                        raise ShapeKeyDirectExportError(
                            f"槽位 {slot_index}: 物体 '{obj_name}' 的记录 Position 大小不匹配，"
                            f"期望={expected_bytes}，实际={len(position_bytes)}"
                        )

                    export_indices_key = tuple(int(index) for index in export_indices.tolist())
                    base_slice = base_slice_cache.get((runtime_info["logical_hash"], export_indices_key))
                    if base_slice is None:
                        base_slice = _extract_position_bytes_by_indices(
                            runtime_info["base_bytes"],
                            runtime_info["position_stride"],
                            export_indices,
                        )
                        base_slice_cache[(runtime_info["logical_hash"], export_indices_key)] = base_slice

                    if position_bytes == base_slice:
                        skip_reason_counts[obj_prefix]["same_as_base"] += 1
                        continue

                    slot_position_overrides[slot_index][obj_name] = {
                        "position_bytes": position_bytes,
                        "export_indices": export_indices,
                    }
                    skip_reason_counts[obj_prefix]["active_overrides"] += 1

        for hash_prefix, reason_counts in skip_reason_counts.items():
            LOG.info(f"直出形态键: 前处理记录采样统计 {hash_prefix} -> {dict(reason_counts)}")

        return slot_position_overrides

    def _build_base_position_sampling_context(self, copy_obj, object_context):
        d3d11_game_type = object_context.get("d3d11_game_type")
        if d3d11_game_type is None:
            raise ShapeKeyDirectExportError(f"物体 '{copy_obj.name}' 缺少导出数据类型上下文")

        local_loop_indices = np.asarray(object_context.get("local_loop_indices", []), dtype=np.int32)
        export_indices = np.asarray(object_context.get("export_indices", []), dtype=np.int32)
        if local_loop_indices.size == 0 or export_indices.size == 0:
            raise ShapeKeyDirectExportError(f"物体 '{copy_obj.name}' 缺少导出顶点映射上下文")

        mesh = copy_obj.data
        all_loop_vertex_indices = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", all_loop_vertex_indices)
        max_loop_index = int(local_loop_indices.max()) if local_loop_indices.size > 0 else -1
        if max_loop_index >= len(all_loop_vertex_indices):
            raise ShapeKeyDirectExportError(
                f"物体 '{copy_obj.name}' 的导出顶点映射越界: max_loop_index={max_loop_index}, loop_count={len(all_loop_vertex_indices)}"
            )
        loop_vertex_indices = all_loop_vertex_indices[local_loop_indices]

        position_element = self._get_position_element_from_game_type(d3d11_game_type)
        if position_element is None:
            raise ShapeKeyDirectExportError(f"物体 '{copy_obj.name}' 缺少 POSITION 元素定义")

        return {
            "loop_vertex_indices": loop_vertex_indices,
            "position_element": position_element,
        }

    def _format_position_bytes_from_coords(self, sampled_coords, position_element, position_stride=12):
        if sampled_coords.size == 0:
            return b""

        position_format = getattr(position_element, "Format", "")
        if position_format == "R32G32B32A32_FLOAT":
            formatted = np.zeros((sampled_coords.shape[0], 4), dtype=np.float32)
            formatted[:, :3] = sampled_coords
            return formatted.tobytes()
        if position_format == "R16G16B16A16_FLOAT":
            formatted = np.zeros((sampled_coords.shape[0], 4), dtype=np.float16)
            formatted[:, :3] = sampled_coords.astype(np.float16)
            formatted[:, 3] = 1.0
            return formatted.tobytes()
        if position_stride == 16:
            formatted = np.zeros((sampled_coords.shape[0], 4), dtype=np.float32)
            formatted[:, :3] = sampled_coords
            formatted[:, 3] = 1.0
            return formatted.tobytes()
        if position_stride == 8:
            formatted = np.zeros((sampled_coords.shape[0], 4), dtype=np.float16)
            formatted[:, :3] = sampled_coords.astype(np.float16)
            formatted[:, 3] = 1.0
            return formatted.tobytes()
        return np.asarray(sampled_coords, dtype=np.float32).tobytes()

    def _sample_slot_position_bytes_from_restored_copy(self, copy_obj, slot_index: int, sample_context, shapekey_name: str | None = None):
        key_blocks = getattr(getattr(copy_obj.data, "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return None

        key_block = key_blocks.get(shapekey_name) if shapekey_name else None
        if key_block is None and len(key_blocks) > slot_index:
            key_block = key_blocks[slot_index]
        if key_block is None:
            return None

        key_index = next((idx for idx, kb in enumerate(key_blocks) if kb == key_block), -1)
        if key_index < 0:
            return None

        sampled_vertex_indices = sample_context["loop_vertex_indices"]
        sample_obj = copy_obj.copy()
        sample_obj.name = f"{copy_obj.name}_slot_sample"
        if copy_obj.data:
            sample_obj.data = copy_obj.data.copy()
        bpy.context.scene.collection.objects.link(sample_obj)

        sample_key_blocks = getattr(getattr(sample_obj.data, "shape_keys", None), "key_blocks", None)
        if not sample_key_blocks:
            bpy.data.objects.remove(sample_obj, do_unlink=True)
            return None

        sample_key_block = sample_key_blocks.get(shapekey_name) if shapekey_name else None
        if sample_key_block is None and len(sample_key_blocks) > slot_index:
            sample_key_block = sample_key_blocks[slot_index]
        if sample_key_block is None:
            sample_mesh = sample_obj.data
            bpy.data.objects.remove(sample_obj, do_unlink=True)
            if sample_mesh and sample_mesh.users == 0:
                bpy.data.meshes.remove(sample_mesh)
            return None

        evaluated_obj = None
        evaluated_mesh = None
        original_active = bpy.context.view_layer.objects.active
        try:
            for idx, kb in enumerate(sample_key_blocks):
                if idx == 0:
                    continue
                kb.value = 0.0
            sample_key_block.value = 1.0
            bpy.context.view_layer.update()

            bpy.ops.object.select_all(action='DESELECT')
            bpy.context.view_layer.objects.active = sample_obj
            sample_obj.select_set(True)
            bpy.ops.object.shape_key_remove(all=True, apply_mix=True)
            bpy.context.view_layer.update()

            depsgraph = bpy.context.evaluated_depsgraph_get()
            evaluated_obj = sample_obj.evaluated_get(depsgraph)
            evaluated_mesh = evaluated_obj.to_mesh()
            coords = np.empty((len(evaluated_mesh.vertices), 3), dtype=np.float32)
            evaluated_mesh.vertices.foreach_get("co", coords.ravel())
            sampled_coords = coords[sampled_vertex_indices]
        finally:
            if evaluated_obj is not None and evaluated_mesh is not None:
                evaluated_obj.to_mesh_clear()
            try:
                sample_obj.select_set(False)
            except Exception:
                pass
            if original_active is not None:
                bpy.context.view_layer.objects.active = original_active
            sample_mesh = sample_obj.data
            bpy.data.objects.remove(sample_obj, do_unlink=True)
            if sample_mesh and sample_mesh.users == 0:
                bpy.data.meshes.remove(sample_mesh)

        position_bytes = self._format_position_bytes_from_coords(
            sampled_coords,
            sample_context["position_element"],
            position_stride=sample_context.get("position_stride", 12),
        )
        if not position_bytes:
            raise ShapeKeyDirectExportError(f"物体 '{copy_obj.name}' 未生成 Position 缓冲区")
        return position_bytes

    def _build_slot_position_overrides(self, slot_to_name_to_objects, calculated_ranges, runtime_infos, source_object_map, static_copy_map):
        slot_position_overrides = defaultdict(dict)
        prefix_to_runtime_info = {}
        sample_cache = {}
        sample_context_cache = {}
        base_slice_cache = {}
        skip_reason_counts = defaultdict(lambda: defaultdict(int))

        for logical_hash, runtime_info in runtime_infos.items():
            logical_prefix = self.node._extract_hash_prefix(logical_hash)
            if logical_prefix:
                prefix_to_runtime_info[logical_prefix] = runtime_info

        for slot_index, names_data in slot_to_name_to_objects.items():
            for shapekey_name, objects in names_data.items():
                for obj_name in OrderedDict.fromkeys(objects).keys():
                    if obj_name not in calculated_ranges:
                        continue

                    obj_hash = self.node._extract_hash_from_name(obj_name)
                    obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                    runtime_info = prefix_to_runtime_info.get(obj_prefix)
                    if runtime_info is None:
                        skip_reason_counts[obj_prefix]["missing_runtime_info"] += 1
                        continue

                    source_name = self._resolve_source_name_for_runtime_object(obj_name, source_object_map)
                    copy_name = self._ensure_static_copy_for_source(source_name, static_copy_map)
                    copy_obj = bpy.data.objects.get(copy_name) if copy_name else None
                    if copy_obj is None:
                        skip_reason_counts[obj_prefix]["missing_copy_obj"] += 1
                        continue

                    object_context = self._resolve_object_export_context_with_merged_members(runtime_info, obj_name)
                    if object_context is None:
                        skip_reason_counts[obj_prefix]["missing_object_context"] += 1
                        raise ShapeKeyDirectExportError(f"无法解析物体 '{obj_name}' 的导出顶点映射上下文")

                    export_indices = np.asarray(object_context.get("export_indices", []), dtype=np.int32)
                    if export_indices.size == 0:
                        skip_reason_counts[obj_prefix]["empty_export_indices"] += 1
                        continue

                    expected_bytes = export_indices.size * runtime_info["position_stride"]
                    export_indices_key = tuple(int(index) for index in export_indices.tolist())
                    cache_key = (slot_index, shapekey_name, copy_name, export_indices_key)
                    context_key = (copy_name, export_indices_key)

                    sample_context = sample_context_cache.get(context_key)
                    if sample_context is None:
                        sample_context = self._build_base_position_sampling_context(copy_obj, object_context)
                        sample_context["position_stride"] = runtime_info["position_stride"]
                        sample_context_cache[context_key] = sample_context

                    position_bytes = sample_cache.get(cache_key)
                    if position_bytes is None:
                        position_bytes = self._sample_slot_position_bytes_from_restored_copy(
                            copy_obj=copy_obj,
                            slot_index=slot_index,
                            sample_context=sample_context,
                            shapekey_name=shapekey_name,
                        )
                        if position_bytes is None:
                            skip_reason_counts[obj_prefix]["missing_position_bytes"] += 1
                            continue
                        sample_cache[cache_key] = position_bytes

                    if len(position_bytes) != expected_bytes:
                        raise ShapeKeyDirectExportError(
                            f"槽位 {slot_index}: 物体 '{obj_name}' 采样后的 Position 大小不匹配，"
                            f"期望={expected_bytes}，实际={len(position_bytes)}"
                        )

                    base_slice = base_slice_cache.get((runtime_info["logical_hash"], export_indices_key))
                    if base_slice is None:
                        base_slice = _extract_position_bytes_by_indices(
                            runtime_info["base_bytes"],
                            runtime_info["position_stride"],
                            export_indices,
                        )
                        base_slice_cache[(runtime_info["logical_hash"], export_indices_key)] = base_slice
                    if position_bytes == base_slice:
                        skip_reason_counts[obj_prefix]["same_as_base"] += 1
                        continue

                    slot_position_overrides[slot_index][obj_name] = {
                        "position_bytes": position_bytes,
                        "export_indices": export_indices,
                    }
                    skip_reason_counts[obj_prefix]["active_overrides"] += 1

        for hash_prefix, reason_counts in skip_reason_counts.items():
            LOG.info(f"直出形态键: 采样统计 {hash_prefix} -> {dict(reason_counts)}")

        return slot_position_overrides

    def _build_slot_position_overrides_from_exporter_buffers(self, slot_to_name_to_objects, calculated_ranges, runtime_infos):
        slot_position_overrides = defaultdict(dict)
        prefix_to_runtime_info = {}
        full_buffer_cache = {}
        base_slice_cache = {}

        for logical_hash, runtime_info in runtime_infos.items():
            logical_prefix = self.node._extract_hash_prefix(logical_hash)
            if logical_prefix:
                prefix_to_runtime_info[logical_prefix] = runtime_info

        for slot_index, names_data in slot_to_name_to_objects.items():
            for shapekey_name, objects in names_data.items():
                for obj_name in objects:
                    if obj_name not in calculated_ranges:
                        continue

                    obj_hash = self.node._extract_hash_from_name(obj_name)
                    obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                    runtime_info = prefix_to_runtime_info.get(obj_prefix)
                    if runtime_info is None:
                        continue

                    object_context = self._resolve_object_export_context(runtime_info, obj_name)
                    if object_context is None:
                        continue

                    export_indices = np.asarray(object_context.get("export_indices", []), dtype=np.int32)
                    if export_indices.size == 0:
                        continue

                    shapekey_cache_key = (runtime_info["logical_hash"], shapekey_name)
                    full_buffer_bytes = full_buffer_cache.get(shapekey_cache_key)
                    if full_buffer_bytes is None:
                        shapekey_buffer = runtime_info.get("shapekey_buffers", {}).get(shapekey_name)
                        if shapekey_buffer is None:
                            continue
                        full_buffer_bytes = _buffer_to_bytes(shapekey_buffer)
                        full_buffer_cache[shapekey_cache_key] = full_buffer_bytes

                    position_stride = runtime_info["position_stride"]
                    position_bytes = _extract_position_bytes_by_indices(
                        full_buffer_bytes,
                        position_stride,
                        export_indices,
                    )

                    export_indices_key = tuple(int(index) for index in export_indices.tolist())
                    base_slice = base_slice_cache.get((runtime_info["logical_hash"], export_indices_key))
                    if base_slice is None:
                        base_slice = _extract_position_bytes_by_indices(
                            runtime_info["base_bytes"],
                            position_stride,
                            export_indices,
                        )
                        base_slice_cache[(runtime_info["logical_hash"], export_indices_key)] = base_slice

                    if position_bytes == base_slice:
                        slot_position_overrides[slot_index][obj_name] = {
                            "position_bytes": position_bytes,
                            "export_indices": export_indices,
                            "is_identity": True,
                        }
                        continue

                    slot_position_overrides[slot_index][obj_name] = {
                        "position_bytes": position_bytes,
                        "export_indices": export_indices,
                    }

        return slot_position_overrides

    def _resolve_static_source_object_name(self, source_name: str) -> str | None:
        candidate_names = list(
            OrderedDict.fromkeys(
                candidate
                for candidate in (
                    source_name,
                    _normalize_runtime_name(source_name),
                    self._extract_original_name(source_name),
                    self._extract_original_name(_normalize_runtime_name(source_name)),
                    re.sub(r"_chain\d+$", "", source_name or ""),
                    re.sub(r"_chain\d+$", "", _normalize_runtime_name(source_name or "")),
                )
                if candidate
            )
        )

        for candidate_name in candidate_names:
            if bpy.data.objects.get(candidate_name) is not None:
                return candidate_name

        source_alias = self.node._extract_alias_from_name(source_name or "")
        alias_candidates = {
            source_alias.casefold(),
            self.node._strip_runtime_copy_suffix(source_alias).casefold(),
            self.node._strip_object_suffix(source_alias).casefold(),
        }

        preferred_matches = []
        fallback_matches = []
        for obj in bpy.data.objects:
            obj_alias = self.node._extract_alias_from_name(obj.name or "")
            obj_alias_candidates = {
                obj_alias.casefold(),
                self.node._strip_runtime_copy_suffix(obj_alias).casefold(),
                self.node._strip_object_suffix(obj_alias).casefold(),
            }
            if alias_candidates.isdisjoint(obj_alias_candidates):
                continue

            if getattr(getattr(obj.data, "shape_keys", None), "key_blocks", None):
                preferred_matches.append(obj.name)
            else:
                fallback_matches.append(obj.name)

        if preferred_matches:
            return preferred_matches[0]
        if fallback_matches:
            return fallback_matches[0]
        return None

    def _resolve_source_name_for_runtime_object(self, obj_name: str, source_object_map: dict) -> str:
        source_candidates = []
        seen = set()
        for member_name in self._iter_merged_member_names(obj_name):
            if not member_name:
                continue
            for candidate_name in self._iter_related_runtime_names(member_name):
                if candidate_name and candidate_name not in seen:
                    seen.add(candidate_name)
                    source_candidates.append(candidate_name)

        for candidate_name in source_candidates:
            source_name = (
                source_object_map.get(candidate_name)
                or source_object_map.get(_normalize_runtime_name(candidate_name))
                or self._extract_original_name(candidate_name)
            )
            if source_name:
                return source_name

        return self._extract_original_name(obj_name)

    def _apply_export_space_transform_to_static_copy(self, copy_obj: bpy.types.Object):
        if copy_obj is None:
            return

        if (GlobalConfig.logic_name == LogicName.SRMI
            or GlobalConfig.logic_name == LogicName.GIMI
            or GlobalConfig.logic_name == LogicName.HIMI
            or GlobalConfig.logic_name == LogicName.YYSLS
            or GlobalConfig.logic_name == LogicName.IdentityV):
            ObjUtils.select_obj(copy_obj)
            copy_obj.rotation_euler[0] = math.radians(-90)
            copy_obj.rotation_euler[1] = 0
            copy_obj.rotation_euler[2] = 0
            ShapeKeyUtils.transform_apply_preserve_shape_keys(copy_obj, location=False, rotation=True, scale=True)
            return

        if GlobalConfig.logic_name == LogicName.EFMI:
            copy_obj.rotation_euler[0] = 0
            copy_obj.rotation_euler[1] = 0
            copy_obj.rotation_euler[2] = 0
            if all(abs(axis - 1.0) <= 1e-7 for axis in copy_obj.scale):
                return
            ObjUtils.select_obj(copy_obj)
            ShapeKeyUtils.transform_apply_preserve_shape_keys(copy_obj, location=False, rotation=True, scale=True)

    def _ensure_static_copy_for_source(self, source_name: str, static_copy_map: dict) -> str | None:
        resolved_source_name = self._resolve_static_source_object_name(source_name) or source_name
        copy_name = static_copy_map.get(resolved_source_name) or static_copy_map.get(source_name)
        if copy_name and bpy.data.objects.get(copy_name) is not None:
            return copy_name

        source_obj = bpy.data.objects.get(resolved_source_name)
        if source_obj is None and source_name:
            source_obj = bpy.data.objects.get(source_name)
            if source_obj is not None:
                resolved_source_name = source_name
        if source_obj is None:
            return None

        copy_name = f"{resolved_source_name}_directsk"
        existing_obj = bpy.data.objects.get(copy_name)
        if existing_obj is not None:
            bpy.data.objects.remove(existing_obj, do_unlink=True)

        obj_copy = source_obj.copy()
        obj_copy.name = copy_name
        if source_obj.data:
            obj_copy.data = source_obj.data.copy()
        bpy.context.scene.collection.objects.link(obj_copy)

        PreProcessHelper._apply_constraints([copy_name])
        PreProcessHelper._apply_modifiers([copy_name], fail_on_error=True)
        PreProcessHelper._triangulate_objects([copy_name])
        PreProcessHelper._apply_transforms([copy_name])
        if GlobalProterties.enable_non_mirror_workflow():
            PreProcessHelper._restore_non_mirror_objects([copy_name])
        self._apply_export_space_transform_to_static_copy(obj_copy)

        static_copy_map[resolved_source_name] = copy_name
        if source_name:
            static_copy_map[source_name] = copy_name
        return copy_name

    def _create_static_shapekey_copies(self, slot_to_name_to_objects, source_object_map, runtime_infos):
        required_source_names = []
        valid_hash_prefixes = {
            self.node._extract_hash_prefix(logical_hash)
            for logical_hash in runtime_infos.keys()
            if self.node._extract_hash_prefix(logical_hash)
        }
        prefix_to_runtime_info = {
            self.node._extract_hash_prefix(logical_hash): info
            for logical_hash, info in runtime_infos.items()
            if self.node._extract_hash_prefix(logical_hash)
        }
        for names_data in slot_to_name_to_objects.values():
            for objects in names_data.values():
                for obj_name in objects:
                    obj_hash = self.node._extract_hash_from_name(obj_name)
                    obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                    if obj_prefix not in valid_hash_prefixes:
                        continue
                    runtime_info = prefix_to_runtime_info.get(obj_prefix)
                    object_context = self._resolve_object_export_context_with_merged_members(runtime_info, obj_name) if runtime_info else None
                    source_name = (object_context or {}).get("preferred_source_name")
                    if not source_name:
                        source_candidates = list(OrderedDict.fromkeys(
                            member_name
                            for member_name in self._iter_merged_member_names(obj_name)
                            if member_name
                        ))
                        for candidate_name in source_candidates:
                            source_name = (
                                source_object_map.get(candidate_name)
                                or source_object_map.get(_normalize_runtime_name(candidate_name))
                                or self._extract_original_name(candidate_name)
                            )
                            if source_name:
                                break
                    if not source_name:
                        source_name = self._extract_original_name(obj_name)
                    resolved_source_name = self._resolve_static_source_object_name(source_name)
                    if resolved_source_name is None:
                        raise ShapeKeyDirectExportError(f"找不到形态键源物体: {source_name}")
                    if resolved_source_name not in required_source_names:
                        required_source_names.append(resolved_source_name)

        copy_map = {}
        copy_names = []
        for source_name in required_source_names:
            source_obj = bpy.data.objects.get(source_name)
            if source_obj is None:
                raise ShapeKeyDirectExportError(f"找不到形态键源物体: {source_name}")

            copy_name = f"{source_name}_directsk"
            existing_obj = bpy.data.objects.get(copy_name)
            if existing_obj is not None:
                bpy.data.objects.remove(existing_obj, do_unlink=True)

            obj_copy = source_obj.copy()
            obj_copy.name = copy_name
            if source_obj.data:
                obj_copy.data = source_obj.data.copy()
            bpy.context.scene.collection.objects.link(obj_copy)

            copy_map[source_name] = copy_name
            copy_names.append(copy_name)

        if copy_names:
            PreProcessHelper._apply_constraints(copy_names)
            PreProcessHelper._apply_modifiers(copy_names, fail_on_error=True)
            PreProcessHelper._triangulate_objects(copy_names)
            PreProcessHelper._apply_transforms(copy_names)
            if GlobalProterties.enable_non_mirror_workflow():
                PreProcessHelper._restore_non_mirror_objects(copy_names)
            for copy_name in copy_names:
                copy_obj = bpy.data.objects.get(copy_name)
                self._apply_export_space_transform_to_static_copy(copy_obj)

        return copy_map
