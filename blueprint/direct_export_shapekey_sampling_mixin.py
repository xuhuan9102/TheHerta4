from __future__ import annotations

import math
import os
import re
from collections import OrderedDict, defaultdict
from types import SimpleNamespace

import bpy
import numpy as np

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.logic_name import LogicName
from ..utils.log_utils import LOG
from ..utils.obj_utils import ObjUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from ..utils.export_utils import ExportUtils
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

    def _iter_recorded_object_records(self, obj_name, object_context, source_object_map):
        """按候选名顺序取出该对象命中且非空的记录列表。

        命中顺序与原实现逐候选名查字典完全一致，只是把候选名生成与字典查找
        从「每个 (对象, 形态键)」降到「每个对象」一次。
        """
        records = BlueprintExportHelper.get_direct_shapekey_position_records()
        if not records:
            return []

        resolved_records = []
        for candidate_name in self._iter_record_candidate_names(obj_name, object_context, source_object_map):
            record = records.get(candidate_name)
            if not record:
                continue
            resolved_records.append((candidate_name, record))
        return resolved_records

    def _resolve_recorded_shape_key_data_from_records(self, resolved_records, shapekey_name):
        for candidate_name, record in resolved_records:
            coords = (record.get("shape_keys", {}) or {}).get(shapekey_name)
            basis_coords = record.get("basis_coords")
            loop_vertex_indices = record.get("loop_vertex_indices")
            if coords is not None and loop_vertex_indices is not None:
                return coords, basis_coords, loop_vertex_indices, candidate_name
        return None

    def _resolve_recorded_shape_key_data(self, obj_name, shapekey_name, object_context, source_object_map):
        resolved_records = self._iter_recorded_object_records(obj_name, object_context, source_object_map)
        return self._resolve_recorded_shape_key_data_from_records(resolved_records, shapekey_name)

    def _build_preprocess_record_object_state(self, object_context):
        return {
            "local_loop_indices": np.asarray(object_context.get("local_loop_indices", []), dtype=np.int32),
            "export_indices": np.asarray(object_context.get("export_indices", []), dtype=np.int32),
            "export_indices_key": None,
            "loop_state": None,
        }

    def _build_slot_position_overrides_from_preprocess_records(self, slot_to_name_to_objects, calculated_ranges, runtime_infos, source_object_map):
        slot_position_overrides = defaultdict(dict)
        prefix_to_runtime_info = {}
        base_slice_cache = {}
        base_coords_cache = {}
        skip_reason_counts = defaultdict(lambda: defaultdict(int))
        # 同一对象会在大量槽位里重复出现：下面这些缓存全部是本次调用的局部状态，
        # 只消除重复解析/重复取样，不跨调用保留。
        not_cached = object()
        object_context_cache = {}
        object_state_cache = {}
        object_prefix_cache = {}
        position_element_cache = {}
        recorded_records_cache = {}

        for logical_hash, runtime_info in runtime_infos.items():
            logical_prefix = self.node._extract_hash_prefix(logical_hash)
            if logical_prefix:
                prefix_to_runtime_info[logical_prefix] = runtime_info

        for slot_index, names_data in slot_to_name_to_objects.items():
            for shapekey_name, objects in names_data.items():
                for obj_name in OrderedDict.fromkeys(objects).keys():
                    if obj_name not in calculated_ranges:
                        continue

                    obj_prefix = object_prefix_cache.get(obj_name, not_cached)
                    if obj_prefix is not_cached:
                        obj_hash = self.node._extract_hash_from_name(obj_name)
                        obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                        object_prefix_cache[obj_name] = obj_prefix
                    runtime_info = prefix_to_runtime_info.get(obj_prefix)
                    if runtime_info is None:
                        skip_reason_counts[obj_prefix]["missing_runtime_info"] += 1
                        continue

                    object_context = object_context_cache.get(obj_name, not_cached)
                    if object_context is not_cached:
                        object_context = self._resolve_object_export_context_with_merged_members(runtime_info, obj_name)
                        object_context_cache[obj_name] = object_context
                    if object_context is None:
                        skip_reason_counts[obj_prefix]["missing_object_context"] += 1
                        continue

                    resolved_records = recorded_records_cache.get(obj_name, not_cached)
                    if resolved_records is not_cached:
                        resolved_records = self._iter_recorded_object_records(
                            obj_name,
                            object_context,
                            source_object_map,
                        )
                        recorded_records_cache[obj_name] = resolved_records
                    recorded_data = self._resolve_recorded_shape_key_data_from_records(resolved_records, shapekey_name)
                    if recorded_data is None:
                        skip_reason_counts[obj_prefix]["missing_record"] += 1
                        continue

                    object_state = object_state_cache.get(obj_name, not_cached)
                    if object_state is not_cached:
                        object_state = self._build_preprocess_record_object_state(object_context)
                        object_state_cache[obj_name] = object_state

                    coords, basis_coords, record_loop_vertex_indices, _record_name = recorded_data
                    local_loop_indices = object_state["local_loop_indices"]
                    export_indices = object_state["export_indices"]
                    if local_loop_indices.size == 0 or export_indices.size == 0:
                        skip_reason_counts[obj_prefix]["empty_indices"] += 1
                        continue

                    loop_state = object_state["loop_state"]
                    if loop_state is None or loop_state[0] is not record_loop_vertex_indices:
                        loop_vertex_indices = np.asarray(record_loop_vertex_indices, dtype=np.int32)
                        if int(local_loop_indices.max()) >= len(loop_vertex_indices):
                            raise ShapeKeyDirectExportError(
                                f"槽位 {slot_index}: 物体 '{obj_name}' 的形态键记录 loop 映射越界"
                            )
                        sampled_vertex_indices = loop_vertex_indices[local_loop_indices]
                        sampled_vertex_max = (
                            int(sampled_vertex_indices.max()) if sampled_vertex_indices.size else -1
                        )
                        loop_state = (
                            record_loop_vertex_indices,
                            loop_vertex_indices,
                            sampled_vertex_indices,
                            sampled_vertex_max,
                        )
                        object_state["loop_state"] = loop_state
                    else:
                        loop_vertex_indices = loop_state[1]
                        sampled_vertex_indices = loop_state[2]
                        sampled_vertex_max = loop_state[3]

                    coords = np.asarray(coords, dtype=np.float32)
                    if sampled_vertex_indices.size and sampled_vertex_max >= len(coords):
                        raise ShapeKeyDirectExportError(
                            f"槽位 {slot_index}: 物体 '{obj_name}' 的形态键记录 vertex 映射越界"
                        )
                    sampled_basis_coords = None
                    if basis_coords is not None:
                        basis_coords = np.asarray(basis_coords, dtype=np.float32)
                        if sampled_vertex_indices.size and sampled_vertex_max >= len(basis_coords):
                            raise ShapeKeyDirectExportError(
                                f"槽位 {slot_index}: 物体 '{obj_name}' 的 Basis 记录 vertex 映射越界"
                            )
                        sampled_basis_coords = basis_coords[sampled_vertex_indices]

                    position_element = position_element_cache.get(obj_name, not_cached)
                    if position_element is not_cached:
                        position_element = self._get_position_element_from_game_type(object_context.get("d3d11_game_type"))
                        position_element_cache[obj_name] = position_element
                    if position_element is None:
                        skip_reason_counts[obj_prefix]["missing_position_element"] += 1
                        continue

                    export_indices_key = object_state["export_indices_key"]
                    if export_indices_key is None:
                        export_indices_key = tuple(int(index) for index in export_indices.tolist())
                        object_state["export_indices_key"] = export_indices_key
                    base_slice_key = (runtime_info["logical_hash"], export_indices_key)
                    base_slice = base_slice_cache.get(base_slice_key)
                    if base_slice is None:
                        base_slice = _extract_position_bytes_by_indices(
                            runtime_info["base_bytes"],
                            runtime_info["position_stride"],
                            export_indices,
                        )
                        base_slice_cache[base_slice_key] = base_slice

                    position_stride = runtime_info["position_stride"]
                    base_coords = None
                    if sampled_basis_coords is not None:
                        # 元素对象由 position_element_cache 持有，id 在本次调用内稳定。
                        base_coords_key = (base_slice_key, id(position_element))
                        base_coords = base_coords_cache.get(base_coords_key, not_cached)
                        if base_coords is not_cached:
                            base_coords = self._extract_position_coords_from_base_slice(
                                base_slice,
                                position_element,
                                position_stride=position_stride,
                            )
                            base_coords_cache[base_coords_key] = base_coords

                    # 先只算目标坐标（numpy），用字节级比较判断是否与基础模板一致；
                    # 只有确实存在差异时才组装整块 Position bytes。
                    target_coords, _base_coords = self._build_recorded_position_target_coords(
                        coords[sampled_vertex_indices],
                        sampled_basis_coords,
                        base_slice,
                        position_element,
                        position_stride=position_stride,
                        base_coords=base_coords,
                    )
                    if target_coords.size == 0:
                        position_bytes = b""
                    elif self._recorded_position_coords_match_base_slice(
                        target_coords,
                        base_slice,
                        position_element,
                        position_stride,
                        base_coords=base_coords,
                    ):
                        position_bytes = base_slice
                    else:
                        position_bytes = self._compose_position_bytes_with_base_slice(
                            target_coords,
                            base_slice,
                            position_element,
                            position_stride=position_stride,
                            coords_are_export_space=True,
                        )

                    expected_bytes = export_indices.size * position_stride
                    if len(position_bytes) != expected_bytes:
                        raise ShapeKeyDirectExportError(
                            f"槽位 {slot_index}: 物体 '{obj_name}' 的记录 Position 大小不匹配，"
                            f"期望={expected_bytes}，实际={len(position_bytes)}"
                        )

                    if position_bytes == base_slice:
                        slot_position_overrides[slot_index][obj_name] = {
                            "position_bytes": base_slice,
                            "export_indices": export_indices,
                            "is_identity": True,
                        }
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
            "d3d11_game_type": d3d11_game_type,
            "local_loop_indices": local_loop_indices,
            "export_indices": export_indices,
        }

    def _format_position_bytes_from_coords(self, sampled_coords, position_element, position_stride=12):
        return ExportUtils.format_position_bytes_from_coords(
            sampled_coords,
            d3d11_game_type=self._wrap_position_element_as_game_type(position_element),
            position_stride=position_stride,
            logic_name=GlobalConfig.logic_name,
        )

    def _convert_position_coords_for_export(self, sampled_coords):
        return ExportUtils.convert_position_coords_for_export(sampled_coords, logic_name=GlobalConfig.logic_name)

    def _convert_position_deltas_for_export(self, sampled_deltas):
        return ExportUtils.convert_position_deltas_for_export(sampled_deltas, logic_name=GlobalConfig.logic_name)

    def _reorder_position_bytes_by_loop_indices(
        self,
        obj_name: str,
        position_bytes: bytes,
        position_stride: int,
        source_loop_indices: np.ndarray,
        target_loop_indices: np.ndarray,
        source_loop_vertex_indices: np.ndarray | None = None,
        target_loop_vertex_indices: np.ndarray | None = None,
    ) -> bytes:
        expected_bytes = target_loop_indices.size * position_stride
        if target_loop_indices.size == 0:
            return b""
        if len(position_bytes) % position_stride != 0:
            raise ShapeKeyDirectExportError(
                f"物体 '{obj_name}' 的 Position 缓冲区大小异常: bytes={len(position_bytes)}, stride={position_stride}"
            )

        if source_loop_indices.size == 0:
            if len(position_bytes) != expected_bytes:
                raise ShapeKeyDirectExportError(
                    f"物体 '{obj_name}' 缺少 loop 映射且 Position 大小不匹配: 期望={expected_bytes}, 实际={len(position_bytes)}"
                )
            return position_bytes

        if source_loop_indices.size == target_loop_indices.size and np.array_equal(source_loop_indices, target_loop_indices):
            if len(position_bytes) != expected_bytes:
                raise ShapeKeyDirectExportError(
                    f"物体 '{obj_name}' 的 Position 大小与导出映射不匹配: 期望={expected_bytes}, 实际={len(position_bytes)}"
                )
            return position_bytes

        loop_index_to_vertex_index = {
            int(loop_index): vertex_index
            for vertex_index, loop_index in enumerate(source_loop_indices.tolist())
        }
        vertex_index_to_source_vertex_index = {}
        if source_loop_vertex_indices is not None:
            for source_vertex_index, vertex_id in enumerate(np.asarray(source_loop_vertex_indices, dtype=np.int32).tolist()):
                vertex_index_to_source_vertex_index.setdefault(int(vertex_id), source_vertex_index)

        reordered_bytes = bytearray(expected_bytes)
        for local_index, loop_index in enumerate(target_loop_indices.tolist()):
            source_vertex_index = loop_index_to_vertex_index.get(int(loop_index))
            if source_vertex_index is None:
                target_vertex_index = None
                if target_loop_vertex_indices is not None and local_index < len(target_loop_vertex_indices):
                    target_vertex_index = int(target_loop_vertex_indices[local_index])
                    source_vertex_index = vertex_index_to_source_vertex_index.get(target_vertex_index)

                if source_vertex_index is None:
                    detail = f"loop_index={loop_index}"
                    if target_vertex_index is not None:
                        detail = f"{detail}, vertex_index={target_vertex_index}"
                    raise ShapeKeyDirectExportError(
                        f"物体 '{obj_name}' 缺少导出所需 loop 映射: {detail}"
                    )

            src_start = source_vertex_index * position_stride
            src_end = src_start + position_stride
            dst_start = local_index * position_stride
            dst_end = dst_start + position_stride
            reordered_bytes[dst_start:dst_end] = position_bytes[src_start:src_end]

        return bytes(reordered_bytes)

    def _build_position_bytes_for_sample_object(self, sample_obj, sample_context):
        d3d11_game_type = sample_context.get("d3d11_game_type")
        if d3d11_game_type is None:
            raise ShapeKeyDirectExportError(f"物体 '{sample_obj.name}' 缺少导出数据类型上下文")

        buffer_result = ExportUtils.build_unity_obj_buffer_result(obj=sample_obj, d3d11_game_type=d3d11_game_type)
        position_buffer = buffer_result.category_buffer_dict.get("Position")
        if position_buffer is None:
            raise ShapeKeyDirectExportError(f"物体 '{sample_obj.name}' 未生成 Position 缓冲区")

        full_position_bytes = position_buffer.tobytes() if hasattr(position_buffer, "tobytes") else bytes(position_buffer)
        local_loop_indices = np.asarray(sample_context.get("local_loop_indices", []), dtype=np.int32)
        if local_loop_indices.size == 0:
            return full_position_bytes

        mesh = getattr(sample_obj, "data", None)
        if mesh is None:
            raise ShapeKeyDirectExportError(f"物体 '{sample_obj.name}' 缺少网格数据")

        all_loop_vertex_indices = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", all_loop_vertex_indices)
        source_loop_indices = np.asarray(getattr(buffer_result, "unique_first_loop_indices", []), dtype=np.int32)
        source_loop_vertex_indices = (
            all_loop_vertex_indices[source_loop_indices]
            if source_loop_indices.size > 0
            else np.asarray([], dtype=np.int32)
        )
        target_loop_vertex_indices = all_loop_vertex_indices[local_loop_indices]

        return self._reorder_position_bytes_by_loop_indices(
            obj_name=sample_obj.name,
            position_bytes=full_position_bytes,
            position_stride=int(sample_context.get("position_stride", 12) or 12),
            source_loop_indices=source_loop_indices,
            target_loop_indices=local_loop_indices,
            source_loop_vertex_indices=source_loop_vertex_indices,
            target_loop_vertex_indices=target_loop_vertex_indices,
        )

    def _position_element_layout(self, position_element, position_stride: int):
        offset = int(getattr(position_element, "AlignedByteOffset", 0) or 0)
        byte_width = int(getattr(position_element, "ByteWidth", 0) or 0)
        if byte_width <= 0:
            byte_width = 12
        if offset < 0 or offset + byte_width > position_stride:
            raise ShapeKeyDirectExportError(
                f"POSITION 元素布局越界: offset={offset}, byte_width={byte_width}, stride={position_stride}"
            )

        position_format = str(getattr(position_element, "Format", "") or "").upper()
        if "16" in position_format and "FLOAT" in position_format:
            dtype = np.float16
            item_size = 2
        else:
            dtype = np.float32
            item_size = 4
        component_count = max(3, byte_width // item_size)
        return offset, byte_width, dtype, item_size, component_count

    def _extract_position_coords_from_base_slice(self, base_slice, position_element, position_stride=12):
        position_stride = int(position_stride)
        if position_stride <= 0 or len(base_slice) % position_stride != 0:
            raise ShapeKeyDirectExportError(
                f"POSITION 模板大小不匹配: stride={position_stride}, actual={len(base_slice)}"
            )

        vertex_count = int(len(base_slice) / position_stride)
        offset, byte_width, dtype, item_size, component_count = self._position_element_layout(
            position_element,
            position_stride,
        )
        # 常规布局用向量化读取（数值与逐顶点 frombuffer 完全一致）；异常布局保留原逐顶点路径。
        if byte_width % item_size == 0 and byte_width // item_size >= 3:
            base_rows = np.frombuffer(base_slice, dtype=np.uint8).reshape(vertex_count, position_stride)
            region = np.ascontiguousarray(base_rows[:, offset:offset + byte_width])
            values = region.view(dtype).reshape(vertex_count, byte_width // item_size)
            return np.asarray(values[:, :3], dtype=np.float32)

        coords = np.zeros((vertex_count, 3), dtype=np.float32)
        for vertex_index in range(vertex_count):
            src_start = vertex_index * position_stride + offset
            src_end = src_start + byte_width
            values = np.frombuffer(base_slice[src_start:src_end], dtype=dtype, count=component_count)
            coords[vertex_index] = values[:3].astype(np.float32)
        return coords

    def _build_recorded_position_target_coords(
        self,
        sampled_coords,
        sampled_basis_coords,
        base_slice,
        position_element,
        position_stride=12,
        base_coords=None,
    ):
        """只计算最终写入 POSITION 的目标坐标（已在导出空间），不组装整块 bytes。

        返回 (target_coords, base_coords)；无 Basis 记录时 base_coords 为 None。
        base_coords 可由调用方传入缓存值，避免同一对象在多个槽位重复提取。
        """
        sampled_coords = np.asarray(sampled_coords, dtype=np.float32)
        if sampled_basis_coords is None:
            if sampled_coords.size == 0:
                return sampled_coords, None
            target_coords = self._convert_position_coords_for_export(sampled_coords)
            return np.asarray(target_coords, dtype=np.float32), None

        sampled_basis_coords = np.asarray(sampled_basis_coords, dtype=np.float32)
        if sampled_basis_coords.shape != sampled_coords.shape:
            raise ShapeKeyDirectExportError(
                f"Basis 记录大小不匹配: basis={sampled_basis_coords.shape}, shape={sampled_coords.shape}"
            )

        sampled_deltas = sampled_coords - sampled_basis_coords
        export_deltas = self._convert_position_deltas_for_export(sampled_deltas)
        if base_coords is None:
            base_coords = self._extract_position_coords_from_base_slice(
                base_slice,
                position_element,
                position_stride=position_stride,
            )
        target_coords = base_coords + np.asarray(export_deltas, dtype=np.float32)
        return target_coords, base_coords

    def _recorded_position_coords_match_base_slice(
        self,
        target_coords,
        base_slice,
        position_element,
        position_stride=12,
        base_coords=None,
    ):
        """判断目标坐标写进 base_slice 后是否与 base_slice 逐字节相同（不组装整块 bytes）。

        直接比较「会被覆写的那段字节」，因此 float16 转换、第 4 分量补 1.0、
        -0.0 / NaN 等边界都与原实现（组装后整块比较）完全一致。
        base_coords 是 base_slice 已提取的坐标缓存；常规 12 字节 float32 布局可用它
        直接做按位比较，省掉一次编码。
        """
        position_stride = int(position_stride)
        target_coords = np.asarray(target_coords, dtype=np.float32)
        vertex_count = int(target_coords.shape[0])
        expected_size = vertex_count * position_stride
        if len(base_slice) != expected_size:
            raise ShapeKeyDirectExportError(
                f"POSITION 模板大小不匹配: expected={expected_size}, actual={len(base_slice)}"
            )

        offset, byte_width, dtype, item_size, component_count = self._position_element_layout(
            position_element,
            position_stride,
        )
        if (
            base_coords is not None
            and dtype == np.float32
            and byte_width == 12
            and component_count == 3
            and target_coords.shape == base_coords.shape
            and target_coords.flags.c_contiguous
            and base_coords.flags.c_contiguous
        ):
            # 常规 12 字节 float32 布局：写入的就是这 3 个 float32，按位比较即可。
            return bool(
                np.array_equal(
                    target_coords.view(np.uint32),
                    base_coords.view(np.uint32),
                )
            )

        if dtype == np.float16:
            component_count = max(4, component_count)
            encoded_values = np.zeros((vertex_count, component_count), dtype=np.float16)
            encoded_values[:, :3] = target_coords.astype(np.float16)
            if component_count > 3:
                encoded_values[:, 3] = 1.0
            encoded_stride = component_count * item_size
        else:
            component_count = max(3, component_count)
            encoded_values = np.zeros((vertex_count, component_count), dtype=np.float32)
            encoded_values[:, :3] = target_coords
            if component_count > 3:
                encoded_values[:, 3] = 1.0
            encoded_stride = component_count * item_size

        if byte_width > encoded_stride:
            # 元素宽度超过编码行宽的异常布局：交给原逐顶点写入路径，保持原行为。
            return False

        base_rows = np.frombuffer(base_slice, dtype=np.uint8).reshape(vertex_count, position_stride)
        encoded_rows = encoded_values.view(np.uint8).reshape(vertex_count, encoded_stride)
        return bool(
            np.array_equal(
                base_rows[:, offset:offset + byte_width],
                encoded_rows[:, :byte_width],
            )
        )

    def _compose_position_bytes_from_recorded_coords(
        self,
        sampled_coords,
        sampled_basis_coords,
        base_slice,
        position_element,
        position_stride=12,
    ):
        target_coords, _base_coords = self._build_recorded_position_target_coords(
            sampled_coords,
            sampled_basis_coords,
            base_slice,
            position_element,
            position_stride=position_stride,
        )
        if target_coords.size == 0:
            return b""
        return self._compose_position_bytes_with_base_slice(
            target_coords,
            base_slice,
            position_element,
            position_stride=position_stride,
            coords_are_export_space=True,
        )

    def _compose_position_bytes_with_base_slice(
        self,
        sampled_coords,
        base_slice,
        position_element,
        position_stride=12,
        coords_are_export_space=False,
    ):
        sampled_coords = np.asarray(sampled_coords, dtype=np.float32)
        if sampled_coords.size == 0:
            return b""
        if not coords_are_export_space:
            sampled_coords = self._convert_position_coords_for_export(sampled_coords)

        vertex_count = int(sampled_coords.shape[0])
        expected_size = vertex_count * int(position_stride)
        if len(base_slice) != expected_size:
            raise ShapeKeyDirectExportError(
                f"POSITION 模板大小不匹配: expected={expected_size}, actual={len(base_slice)}"
            )

        offset, byte_width, dtype, item_size, component_count = self._position_element_layout(
            position_element,
            int(position_stride),
        )
        if dtype == np.float16:
            component_count = max(4, component_count)
            encoded_values = np.zeros((vertex_count, component_count), dtype=np.float16)
            encoded_values[:, :3] = sampled_coords.astype(np.float16)
            if component_count > 3:
                encoded_values[:, 3] = 1.0
            encoded_bytes = encoded_values.tobytes()
            encoded_stride = component_count * item_size
        else:
            component_count = max(3, component_count)
            encoded_values = np.zeros((vertex_count, component_count), dtype=np.float32)
            encoded_values[:, :3] = sampled_coords
            if component_count > 3:
                encoded_values[:, 3] = 1.0
            encoded_bytes = encoded_values.tobytes()
            encoded_stride = component_count * item_size

        if byte_width <= encoded_stride:
            stride = int(position_stride)
            output_rows = np.frombuffer(base_slice, dtype=np.uint8).reshape(vertex_count, stride).copy()
            encoded_rows = np.frombuffer(encoded_bytes, dtype=np.uint8).reshape(vertex_count, encoded_stride)
            output_rows[:, offset:offset + byte_width] = encoded_rows[:, :byte_width]
            return output_rows.tobytes()

        output = bytearray(base_slice)
        for vertex_index in range(vertex_count):
            src_start = vertex_index * encoded_stride
            src_end = src_start + byte_width
            dst_start = vertex_index * position_stride + offset
            dst_end = dst_start + byte_width
            output[dst_start:dst_end] = encoded_bytes[src_start:src_end]
        return bytes(output)

    def _sample_slot_position_bytes_from_restored_copy(self, copy_obj, slot_index: int, sample_context, shapekey_name: str | None = None):
        key_blocks = getattr(getattr(copy_obj.data, "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return None

        key_block = key_blocks.get(shapekey_name) if shapekey_name else None
        if key_block is None:
            key_block = BlueprintExportHelper.get_exportable_shape_key_by_slot(copy_obj, slot_index)
        if key_block is None:
            return None

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
        if sample_key_block is None:
            sample_key_block = BlueprintExportHelper.get_exportable_shape_key_by_slot(sample_obj, slot_index)
        if sample_key_block is None:
            sample_mesh = sample_obj.data
            bpy.data.objects.remove(sample_obj, do_unlink=True)
            if sample_mesh and sample_mesh.users == 0:
                bpy.data.meshes.remove(sample_mesh)
            return None

        original_active = bpy.context.view_layer.objects.active
        try:
            for idx, kb in enumerate(sample_key_blocks):
                if idx == 0:
                    continue
                kb.value = 0.0
            sample_key_block.value = 1.0
            bpy.context.view_layer.update()

            ShapeKeyUtils.remove_shape_keys(sample_obj, all=True, apply_mix=True)
            bpy.context.view_layer.update()

            position_bytes = self._build_position_bytes_for_sample_object(sample_obj, sample_context)
        finally:
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
                        slot_position_overrides[slot_index][obj_name] = {
                            "position_bytes": base_slice,
                            "export_indices": export_indices,
                            "is_identity": True,
                        }
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
            if obj is None:
                continue
            try:
                obj_name = obj.name or ""
                obj_data = obj.data
            except (AttributeError, ReferenceError):
                continue

            obj_alias = self.node._extract_alias_from_name(obj_name)
            obj_alias_candidates = {
                obj_alias.casefold(),
                self.node._strip_runtime_copy_suffix(obj_alias).casefold(),
                self.node._strip_object_suffix(obj_alias).casefold(),
            }
            if alias_candidates.isdisjoint(obj_alias_candidates):
                continue

            if getattr(getattr(obj_data, "shape_keys", None), "key_blocks", None):
                preferred_matches.append(obj_name)
            else:
                fallback_matches.append(obj_name)

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
        ExportUtils.apply_export_space_transform_to_object(copy_obj, logic_name=GlobalConfig.logic_name)

    def _wrap_position_element_as_game_type(self, position_element):
        return SimpleNamespace(
            D3D11ElementList=[position_element] if position_element is not None else [],
            ElementNameD3D11ElementDict={"POSITION": position_element} if position_element is not None else {},
        )

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
