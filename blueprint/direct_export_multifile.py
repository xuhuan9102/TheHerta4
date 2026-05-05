import glob
import os
import re
import shutil
from collections import OrderedDict, defaultdict

import bpy
import numpy as np

from ..utils.export_utils import ExportUtils
from .direct_export_runtime_utils import (
    apply_position_override_in_place,
    get_model_vertex_count as _get_model_vertex_count,
    iter_drawib_models as _iter_drawib_models,
    normalize_runtime_name as _normalize_runtime_name,
)


class MultiFileDirectExportError(RuntimeError):
    pass


class DirectMultiFileGenerator:
    def __init__(self, config_node, multi_file_nodes, mod_export_path: str, exporter):
        self.config_node = config_node
        self.multi_file_nodes = list(multi_file_nodes)
        self.mod_export_path = mod_export_path
        self.exporter = exporter
        self.meshes_dir = os.path.join(mod_export_path, "Meshes0000")
        self._processing_chain_alias_lookup = None

    def generate(self):
        ini_files = glob.glob(os.path.join(self.mod_export_path, "*.ini"))
        if not ini_files:
            raise MultiFileDirectExportError("路径中未找到任何 ini 文件，无法执行多文件直出。")

        target_ini_file = ini_files[0]
        self.config_node._create_cumulative_backup(target_ini_file, self.mod_export_path)
        sections, preserved_tail_content = self.config_node._read_ini_to_ordered_dict(target_ini_file)

        hash_filters = self.config_node._parse_hash_values(self.config_node.hash_values)
        if not hash_filters:
            raise MultiFileDirectExportError("请至少输入一个有效的哈希值。")

        runtime_infos = self._build_runtime_infos(hash_filters)
        if not runtime_infos:
            raise MultiFileDirectExportError("未找到匹配的基础 Position 缓冲区，无法执行多文件直出。")

        object_entry_map = self._build_object_entry_map(runtime_infos)
        max_export_count = 1
        for multi_file_node in self.multi_file_nodes:
            max_export_count = max(max_export_count, len(getattr(multi_file_node, "object_list", [])))

        generated_states = {}
        for actual_hash, runtime_info in runtime_infos.items():
            generated_states[actual_hash] = self._build_state_outputs(runtime_info, object_entry_map, max_export_count)

        self._update_ini_sections(
            sections=sections,
            preserved_tail_content=preserved_tail_content,
            target_ini_file=target_ini_file,
            runtime_infos=runtime_infos,
            generated_states=generated_states,
        )

    def _build_runtime_infos(self, hash_filters):
        # 每个哈希先绑定基础 Position 文件、步长和对象导出上下文，后续所有状态都从这份基底派生。
        runtime_infos = OrderedDict()
        for hash_filter in hash_filters:
            base_path, actual_hash = self._resolve_position_buffer_path(hash_filter)
            if not base_path:
                continue

            drawib_model = self._match_drawib_model(actual_hash, hash_filter)
            if drawib_model is None:
                raise MultiFileDirectExportError(f"无法为 {hash_filter} 匹配到基础 DrawIB 模型")

            with open(base_path, "rb") as file_obj:
                base_bytes = file_obj.read()

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                raise MultiFileDirectExportError(f"{actual_hash} 的基础 Position 顶点数无效")

            runtime_infos[actual_hash] = {
                "hash_filter": hash_filter,
                "actual_hash": actual_hash,
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "base_resource_name": self._find_base_resource_name(actual_hash),
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        return runtime_infos

    def _resolve_position_buffer_path(self, hash_filter):
        candidates = []
        for filename in os.listdir(self.meshes_dir):
            if not filename.endswith("-Position.buf"):
                continue
            if filename.startswith(hash_filter):
                candidates.append(filename)
        if not candidates:
            return None, None
        candidates.sort(key=str.casefold)
        filename = candidates[0]
        return os.path.join(self.meshes_dir, filename), filename.replace("-Position.buf", "")

    def _match_drawib_model(self, actual_hash: str, logical_hash: str):
        logical_prefix = logical_hash.split("-")[0] if logical_hash else ""
        for drawib_model in _iter_drawib_models(self.exporter):
            candidate_keys = {
                getattr(drawib_model, "draw_ib", ""),
                getattr(drawib_model, "draw_ib_alias", ""),
            }
            for submesh_model in getattr(drawib_model, "submesh_model_list", []) or []:
                candidate_keys.add(getattr(submesh_model, "unique_str", ""))
                candidate_keys.add(getattr(submesh_model, "match_draw_ib", ""))

            for candidate_key in candidate_keys:
                if not candidate_key:
                    continue
                if candidate_key == actual_hash or candidate_key == logical_hash:
                    return drawib_model
                if actual_hash.startswith(candidate_key + "-") or candidate_key.startswith(actual_hash + "-"):
                    return drawib_model
                if logical_prefix and (
                    candidate_key == logical_prefix
                    or candidate_key.startswith(logical_prefix + "-")
                    or logical_prefix.startswith(candidate_key + "-")
                ):
                    return drawib_model
        return None

    def _extract_original_name(self, name: str) -> str:
        if not name:
            return name

        patterns = [
            r"_x\d+$",
            r"_copy$",
            r"_chain\d+$",
            r"_dup\d+$",
            r"_chain\d+_copy$",
            r"_dup\d+_copy$",
            r"_chain\d+_dup\d+$",
            r"_chain\d+_dup\d+_copy$",
        ]
        result = name
        for pattern in patterns:
            result = re.sub(pattern, "", result)
        return result

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

    def _get_processing_chain_alias_lookup(self):
        if self._processing_chain_alias_lookup is not None:
            return self._processing_chain_alias_lookup

        alias_lookup = defaultdict(list)
        blueprint_model = getattr(self.exporter, "blueprint_model", None)
        for chain in getattr(blueprint_model, "processing_chains", []) or []:
            if not getattr(chain, "is_valid", False) or not getattr(chain, "reached_output", False):
                continue

            alias_names = list(OrderedDict.fromkeys(
                candidate_name
                for candidate_name in self._iter_chain_aliases(chain)
                if candidate_name
            ))
            if not alias_names:
                continue

            for candidate_name in alias_names:
                related_names = alias_lookup[candidate_name]
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

    def _build_drawib_object_context_lookup(self, drawib_model):
        lookup = {}
        context_map = getattr(drawib_model, "object_export_context_map", {}) or {}
        for candidate_name, context in context_map.items():
            if not candidate_name:
                continue
            for alias in self._iter_name_variants(candidate_name):
                if alias and alias not in lookup:
                    lookup[alias] = context
        return lookup

    def _infer_position_stride(self, drawib_model, base_bytes: bytes) -> int:
        d3d11_game_type = getattr(drawib_model, "d3d11GameType", None) or getattr(drawib_model, "d3d11_game_type", None)
        if d3d11_game_type is not None:
            stride = int(d3d11_game_type.CategoryStrideDict.get("Position", 0))
            if stride > 0:
                return stride

        vertex_count = _get_model_vertex_count(drawib_model)
        if vertex_count > 0 and len(base_bytes) % vertex_count == 0:
            return int(len(base_bytes) / vertex_count)

        if len(base_bytes) % 12 == 0:
            return 12

        raise MultiFileDirectExportError(f"无法推断 Position 步长: draw_ib={getattr(drawib_model, 'draw_ib', '')}")

    def _build_object_entry_map(self, runtime_infos):
        entry_map = defaultdict(list)
        for actual_hash, runtime_info in runtime_infos.items():
            drawib_model = runtime_info["drawib_model"]
            context_lookup = runtime_info.get("object_export_context_lookup", {}) or {}

            if context_lookup:
                for candidate_name, context in context_lookup.items():
                    export_indices = np.asarray(context.get("export_indices", []), dtype=np.int32)
                    if export_indices.size == 0:
                        continue

                    local_loop_indices = np.asarray(context.get("local_loop_indices", []), dtype=np.int32)
                    entry = {
                        "actual_hash": actual_hash,
                        "export_indices": export_indices,
                        "local_loop_indices": local_loop_indices,
                        "expected_bytes": int(export_indices.size) * runtime_info["position_stride"],
                        "d3d11_game_type": context.get("d3d11_game_type"),
                        "preferred_source_name": context.get("preferred_source_name", ""),
                    }
                    self._append_object_entry(entry_map, candidate_name, entry)
                continue

            split_ib_dict = getattr(drawib_model, "submesh_ib_dict", {}) or {}
            combined_ib = getattr(drawib_model, "ib", []) or []
            draw_offset_map = getattr(drawib_model, "obj_name_draw_offset", {}) or {}

            for submesh_model in getattr(drawib_model, "submesh_model_list", []) or []:
                submesh_ib = split_ib_dict.get(getattr(submesh_model, "unique_str", "")) or combined_ib
                d3d11_game_type = getattr(submesh_model, "d3d11_game_type", None)

                for draw_call_model in getattr(submesh_model, "drawcall_model_list", []) or []:
                    obj_name = getattr(draw_call_model, "obj_name", "")
                    index_offset = draw_offset_map.get(obj_name, getattr(draw_call_model, "index_offset", 0))
                    index_count = int(getattr(draw_call_model, "index_count", 0) or 0)
                    if index_count <= 0:
                        continue

                    indices = submesh_ib[index_offset:index_offset + index_count]
                    if not indices:
                        continue

                    start_v = int(min(indices))
                    end_v = int(max(indices))
                    export_indices = np.arange(start_v, end_v + 1, dtype=np.int32)
                    candidate_names = {
                        obj_name,
                        getattr(draw_call_model, "source_obj_name", ""),
                        draw_call_model.get_blender_obj_name(),
                        _normalize_runtime_name(draw_call_model.get_blender_obj_name()),
                    }
                    for candidate_name in candidate_names:
                        if not candidate_name:
                            continue
                        entry = {
                            "actual_hash": actual_hash,
                            "export_indices": export_indices,
                            "local_loop_indices": np.asarray([], dtype=np.int32),
                            "expected_bytes": int(export_indices.size) * runtime_info["position_stride"],
                            "d3d11_game_type": d3d11_game_type,
                        }
                        self._append_object_entry(entry_map, candidate_name, entry)

        return entry_map

    def _append_object_entry(self, entry_map, candidate_name, entry):
        if not candidate_name:
            return

        for alias in self._iter_related_runtime_names(candidate_name):
            existing_entries = entry_map[alias]
            for existing_entry in existing_entries:
                existing_indices = np.asarray(existing_entry.get("export_indices", []), dtype=np.int32)
                new_indices = np.asarray(entry.get("export_indices", []), dtype=np.int32)
                if (
                    existing_entry.get("actual_hash") == entry.get("actual_hash")
                    and existing_indices.shape == new_indices.shape
                    and np.array_equal(existing_indices, new_indices)
                ):
                    break
            else:
                existing_entries.append(entry)

    def _build_state_outputs(self, runtime_info, object_entry_map, max_export_count):
        generated_states = OrderedDict()
        base_bytes = runtime_info["base_bytes"]
        object_buffer_cache = {}

        for export_index in range(2, max_export_count + 1):
            # 每个状态都从基础 Position 拷贝一份，再按对象范围覆盖目标物体的顶点数据。
            state_bytes = bytearray(base_bytes)
            state_changed = False

            for multi_file_node in self.multi_file_nodes:
                object_list = getattr(multi_file_node, "object_list", [])
                if not object_list:
                    continue

                base_item = object_list[0]
                target_item = object_list[min(export_index - 1, len(object_list) - 1)]
                if not getattr(base_item, "object_name", "") or not getattr(target_item, "object_name", ""):
                    continue
                if base_item.object_name == target_item.object_name:
                    continue

                any_base_entry = self._find_object_entry(object_entry_map, base_item)
                if any_base_entry is None:
                    raise MultiFileDirectExportError(f"无法定位多文件基础物体范围: {base_item.object_name}")

                base_entry = self._find_object_entry(
                    object_entry_map,
                    base_item,
                    actual_hash=runtime_info["actual_hash"],
                )
                if base_entry is None:
                    continue

                target_obj = bpy.data.objects.get(target_item.object_name)
                if target_obj is None:
                    raise MultiFileDirectExportError(f"找不到多文件目标物体: {target_item.object_name}")

                target_pos_bytes = self._build_object_position_bytes(
                    target_obj,
                    object_entry=base_entry,
                    position_stride=runtime_info["position_stride"],
                    buffer_result_cache=object_buffer_cache,
                )
                if len(target_pos_bytes) != base_entry["expected_bytes"]:
                    raise MultiFileDirectExportError(
                        f"多文件状态 {export_index}: 物体 '{target_item.object_name}' 顶点数/步长不一致，"
                        f"基础字节数={base_entry['expected_bytes']}，当前字节数={len(target_pos_bytes)}"
                    )

                self._apply_position_override(
                    state_bytes=state_bytes,
                    position_bytes=target_pos_bytes,
                    export_indices=np.asarray(base_entry.get("export_indices", []), dtype=np.int32),
                    position_stride=runtime_info["position_stride"],
                )
                state_changed = True

            if not state_changed or bytes(state_bytes) == base_bytes:
                continue

            base_meshes = np.frombuffer(base_bytes, dtype=np.float32)
            target_meshes = np.frombuffer(bytes(state_bytes), dtype=np.float32)
            map_array, pos_deltas_array = self.config_node._create_packed_Meshess(base_meshes, target_meshes, True)

            data_filename = f"{runtime_info['actual_hash']}-Position{export_index:02d}_packed_pos_delta.buf"
            map_filename = f"{runtime_info['actual_hash']}-Position{export_index:02d}_map.buf"
            data_path = os.path.join(self.meshes_dir, data_filename)
            map_path = os.path.join(self.meshes_dir, map_filename)
            self.config_node._write_Meshes_file(pos_deltas_array, data_path)
            self.config_node._write_Meshes_file(map_array, map_path)

            generated_states[export_index] = {
                "data_filename": data_filename,
                "map_filename": map_filename,
            }

        return generated_states

    def _find_object_entry(self, object_entry_map, item, actual_hash: str | None = None):
        candidate_names = OrderedDict()
        for raw_name in (
            getattr(item, "object_name", ""),
            getattr(item, "original_object_name", ""),
        ):
            for candidate_name in self._iter_related_runtime_names(raw_name):
                if candidate_name:
                    candidate_names[candidate_name] = True

        for candidate_name in candidate_names:
            entries = object_entry_map.get(candidate_name, [])
            if actual_hash is None and entries:
                return entries[0]
            for entry in entries:
                if entry.get("actual_hash") == actual_hash:
                    return entry
        return None

    def _build_object_position_bytes(self, obj, object_entry, position_stride: int, buffer_result_cache: dict | None = None):
        d3d11_game_type = object_entry.get("d3d11_game_type")
        if d3d11_game_type is None:
            raise MultiFileDirectExportError(f"无法确定物体 '{obj.name}' 的数据类型")

        cache_key = (obj.name, id(d3d11_game_type))
        buffer_result = None
        if buffer_result_cache is not None:
            buffer_result = buffer_result_cache.get(cache_key)

        if buffer_result is None:
            buffer_result = ExportUtils.build_unity_obj_buffer_result(obj=obj, d3d11_game_type=d3d11_game_type)
            if buffer_result_cache is not None:
                buffer_result_cache[cache_key] = buffer_result

        position_buffer = buffer_result.category_buffer_dict.get("Position")
        if position_buffer is None:
            raise MultiFileDirectExportError(f"物体 '{obj.name}' 未生成 Position 缓冲区")

        if hasattr(position_buffer, "tobytes"):
            full_position_bytes = position_buffer.tobytes()
        else:
            full_position_bytes = bytes(position_buffer)

        export_indices = np.asarray(object_entry.get("export_indices", []), dtype=np.int32)
        if export_indices.size == 0:
            return b""

        local_loop_indices = np.asarray(object_entry.get("local_loop_indices", []), dtype=np.int32)
        if local_loop_indices.size == 0:
            return full_position_bytes

        unique_first_loop_indices = getattr(buffer_result, "unique_first_loop_indices", None)
        source_loop_indices = np.asarray(unique_first_loop_indices, dtype=np.int32) if unique_first_loop_indices is not None else np.asarray([], dtype=np.int32)
        return self._reorder_position_bytes_by_loop_indices(
            obj_name=obj.name,
            position_bytes=full_position_bytes,
            position_stride=position_stride,
            source_loop_indices=source_loop_indices,
            target_loop_indices=local_loop_indices,
        )

    def _reorder_position_bytes_by_loop_indices(self, obj_name: str, position_bytes: bytes, position_stride: int, source_loop_indices: np.ndarray, target_loop_indices: np.ndarray) -> bytes:
        if position_stride <= 0:
            raise MultiFileDirectExportError(f"物体 '{obj_name}' 的 Position 步长无效: {position_stride}")

        expected_bytes = target_loop_indices.size * position_stride
        if target_loop_indices.size == 0:
            return b""
        if len(position_bytes) % position_stride != 0:
            raise MultiFileDirectExportError(
                f"物体 '{obj_name}' 的 Position 缓冲区大小异常: bytes={len(position_bytes)}, stride={position_stride}"
            )

        if source_loop_indices.size == 0:
            if len(position_bytes) != expected_bytes:
                raise MultiFileDirectExportError(
                    f"物体 '{obj_name}' 缺少 loop 映射且 Position 大小不匹配: 期望={expected_bytes}, 实际={len(position_bytes)}"
                )
            return position_bytes

        if source_loop_indices.size == target_loop_indices.size and np.array_equal(source_loop_indices, target_loop_indices):
            if len(position_bytes) != expected_bytes:
                raise MultiFileDirectExportError(
                    f"物体 '{obj_name}' 的 Position 大小与导出映射不匹配: 期望={expected_bytes}, 实际={len(position_bytes)}"
                )
            return position_bytes

        loop_index_to_vertex_index = {
            int(loop_index): vertex_index
            for vertex_index, loop_index in enumerate(source_loop_indices.tolist())
        }

        reordered_bytes = bytearray(expected_bytes)
        for local_index, loop_index in enumerate(target_loop_indices.tolist()):
            source_vertex_index = loop_index_to_vertex_index.get(int(loop_index))
            if source_vertex_index is None:
                raise MultiFileDirectExportError(
                    f"物体 '{obj_name}' 缺少导出所需 loop 映射: loop_index={loop_index}"
                )

            src_start = source_vertex_index * position_stride
            src_end = src_start + position_stride
            dst_start = local_index * position_stride
            dst_end = dst_start + position_stride
            reordered_bytes[dst_start:dst_end] = position_bytes[src_start:src_end]

        return bytes(reordered_bytes)

    def _apply_position_override(self, state_bytes: bytearray, position_bytes: bytes, export_indices: np.ndarray, position_stride: int):
        try:
            apply_position_override_in_place(
                state_bytes=state_bytes,
                position_bytes=position_bytes,
                export_indices=export_indices,
                position_stride=position_stride,
            )
        except ValueError as exc:
            raise MultiFileDirectExportError(str(exc)) from exc

    def _find_base_resource_name(self, actual_hash):
        ini_files = glob.glob(os.path.join(self.mod_export_path, "*.ini"))
        if not ini_files:
            return f"Resource{actual_hash.replace('-', '_')}Position"

        sections, _ = self.config_node._read_ini_to_ordered_dict(ini_files[0])
        resource_pattern = re.compile(r'\[(Resource_?([a-f0-9]{8}(?:[_-][a-f0-9]+)*)_?Position)\]')
        for section_name in sections.keys():
            match = resource_pattern.match(section_name)
            if not match:
                continue
            full_name, hash_value = match.groups()
            if hash_value.replace("_", "-") == actual_hash or actual_hash.startswith(hash_value.replace("_", "-")):
                return full_name
        return f"Resource{actual_hash.replace('-', '_')}Position"

    def _update_ini_sections(self, sections, preserved_tail_content, target_ini_file, runtime_infos, generated_states):
        shader_source_path = self.config_node._get_shader_source_path()
        if not shader_source_path or not os.path.exists(shader_source_path):
            raise MultiFileDirectExportError(f"着色器模板文件未找到: {shader_source_path}")

        dest_res_dir = os.path.join(self.mod_export_path, "res")
        os.makedirs(dest_res_dir, exist_ok=True)
        shader_dest_path = os.path.join(dest_res_dir, "merge_anim_packed_delta.hlsl")
        shutil.copy2(shader_source_path, shader_dest_path)
        self.config_node._update_shader_file(shader_dest_path)

        constants_section = "[Constants]"
        constants_lines = sections.get(constants_section, [])
        constants_content = "".join(constants_lines)
        if self.config_node.animation_swapkey not in constants_content:
            constants_lines.append(f"global persist {self.config_node.animation_swapkey} = 0")
        if self.config_node.active_swapkey not in constants_content:
            constants_lines.append(f"global persist {self.config_node.active_swapkey} = 0")

        present_section = "[Present]"
        present_lines = sections.get(present_section, [])
        run_lines_to_add = []

        for actual_hash, runtime_info in runtime_infos.items():
            state_outputs = generated_states.get(actual_hash, {})
            if not state_outputs:
                continue

            resource_prefix = self.config_node._hash_to_resource_prefix(actual_hash)
            base_resource_name = runtime_info["base_resource_name"]

            base_section_name = f"[{base_resource_name}_1]"
            if base_section_name not in sections:
                original_section_name = f"[{base_resource_name}]"
                original_lines = list(sections.get(original_section_name, []))
                if original_lines:
                    sections[base_section_name] = original_lines

            for export_index, state_info in state_outputs.items():
                data_section = f"[Resource_{resource_prefix}_Position{export_index:02d}_packed_pos_delta]"
                sections[data_section] = [
                    "type = Buffer",
                    "stride = 12",
                    f"filename = Meshes0000/{state_info['data_filename']}",
                ]

                map_section = f"[Resource_{resource_prefix}_Position{export_index:02d}_Map]"
                sections[map_section] = [
                    "type = Buffer",
                    "stride = 4",
                    f"filename = Meshes0000/{state_info['map_filename']}",
                ]

            shader_section = f"[CustomShader_{actual_hash}_1Anim]"
            shader_lines = []
            if self.config_node.comment:
                shader_lines.append("; " + self.config_node.comment)
                shader_lines.append("")

            ordered_states = list(state_outputs.items())
            for state_index, (export_index, _) in enumerate(ordered_states, 1):
                shader_lines.append(f"if {self.config_node.animation_swapkey} == {state_index}")
                shader_lines.append(f"      cs-t51 = copy Resource_{resource_prefix}_Position{export_index:02d}_packed_pos_delta")
                shader_lines.append("endif")

            shader_lines.append("")
            for state_index, (export_index, _) in enumerate(ordered_states, 1):
                shader_lines.append(f"if {self.config_node.animation_swapkey} == {state_index}")
                shader_lines.append(f"      cs-t75 = copy Resource_{resource_prefix}_Position{export_index:02d}_Map")
                shader_lines.append("endif")

            shader_lines.append("")
            shader_lines.append("    cs = ./res/merge_anim_packed_delta.hlsl")
            shader_lines.append(f"    cs-u5 = copy {base_resource_name}_1")
            shader_lines.append(f"    {base_resource_name} = ref cs-u5")
            vertex_count = runtime_info["vertex_count"] or 100000
            shader_lines.append(f"    Dispatch = {vertex_count}, 1, 1")
            shader_lines.append("    cs-u5 = null")
            shader_lines.append("    cs-t51 = null")
            shader_lines.append("    cs-t75 = null")
            sections[shader_section] = shader_lines

            post_copy_line = f"post {base_resource_name} = copy_desc {base_resource_name}_1"
            post_run_line = f"post run = CustomShader_{actual_hash}_1Anim"
            if post_copy_line not in constants_lines:
                constants_lines.append(post_copy_line)
            if post_run_line not in constants_lines:
                constants_lines.append(post_run_line)

            run_line = f"    run = CustomShader_{actual_hash}_1Anim"
            if run_line not in run_lines_to_add:
                run_lines_to_add.append(run_line)

        self._ensure_present_run_lines(present_lines, run_lines_to_add)

        sections[constants_section] = constants_lines
        sections[present_section] = present_lines
        self.config_node._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content)

    def _ensure_present_run_lines(self, present_lines, run_lines):
        if not run_lines:
            return

        guard_line = f"if {self.config_node.active_swapkey} == {self.config_node.active_value}"
        active_block_start = -1
        active_block_end = -1
        nested_if_depth = 0

        for index, line in enumerate(present_lines):
            stripped_line = line.strip()
            if active_block_start < 0:
                if stripped_line == guard_line:
                    active_block_start = index
                    nested_if_depth = 1
                continue

            if stripped_line.startswith("if "):
                nested_if_depth += 1
            elif stripped_line == "endif":
                nested_if_depth -= 1
                if nested_if_depth == 0:
                    active_block_end = index
                    break

        if active_block_start >= 0 and active_block_end >= 0:
            existing_run_lines = {
                line.strip()
                for line in present_lines[active_block_start + 1:active_block_end]
            }
            insert_index = active_block_end
            for run_line in run_lines:
                if run_line.strip() in existing_run_lines:
                    continue
                present_lines.insert(insert_index, run_line)
                insert_index += 1
                existing_run_lines.add(run_line.strip())
            return

        if present_lines and present_lines[-1].strip():
            present_lines.append("")
        present_lines.append(guard_line)
        present_lines.extend(run_lines)
        present_lines.append("endif")
