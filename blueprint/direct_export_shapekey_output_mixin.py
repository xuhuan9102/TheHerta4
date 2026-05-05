import os
import re
from collections import OrderedDict

import numpy as np

from ..utils.log_utils import LOG
from .direct_export_runtime_utils import apply_position_override_in_place
from .direct_export_runtime_utils import extract_position_bytes_by_indices as _extract_position_bytes_by_indices
from .direct_export_shapekey_shared import ShapeKeyDirectExportError, _buffer_to_bytes


class DirectShapeKeyOutputMixin:
    def _write_slot_files(self, logical_hash, runtime_info, hash_slot_data, slot_position_overrides):
        use_packed = self.node.use_packed_Meshess
        use_delta = self.node.store_deltas
        actual_hash = runtime_info["actual_hash"]
        base_bytes = runtime_info["base_bytes"]
        struct_definition = self.node._get_vertex_struct_definition()
        slot_maps = {}

        for slot_num, names_data in sorted(hash_slot_data.items()):
            target_bytes = self._compose_slot_bytes(
                logical_hash,
                runtime_info,
                slot_num,
                names_data,
                slot_position_overrides,
            )
            vertex_stride, num_floats_per_vertex, num_vertices = self.node._detect_vertex_format(
                base_bytes,
                target_bytes,
                struct_definition,
            )
            base_data = np.frombuffer(base_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))
            target_data = np.frombuffer(target_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))

            output_prefix = os.path.join(self.meshes_dir, f"{actual_hash}-Position1{slot_num:03d}")
            slot_maps[slot_num] = None

            if use_delta:
                data_to_write = target_data[:, :3] - base_data[:, :3]
                data_to_write[data_to_write == 0] = 0.0
                diff_mask = ~np.isclose(base_data[:, :3], target_data[:, :3], atol=1e-6).all(axis=1)
                if use_packed:
                    packed_data = data_to_write[diff_mask]
                    map_array = np.full(num_vertices, -1, dtype=np.int32)
                    map_array[diff_mask] = np.arange(np.count_nonzero(diff_mask), dtype=np.int32)
                    with open(f"{output_prefix}_packed_pos_delta.buf", "wb") as file_obj:
                        file_obj.write(packed_data.tobytes())
                    with open(f"{output_prefix}_map.buf", "wb") as file_obj:
                        file_obj.write(map_array.tobytes())
                    slot_maps[slot_num] = map_array
                else:
                    with open(f"{output_prefix}_pos_delta.buf", "wb") as file_obj:
                        file_obj.write(data_to_write.tobytes())
            elif use_packed:
                diff_mask = ~np.isclose(base_data, target_data, atol=1e-6).all(axis=1)
                packed_data = target_data[diff_mask]
                map_array = np.full(num_vertices, -1, dtype=np.int32)
                map_array[diff_mask] = np.arange(np.count_nonzero(diff_mask), dtype=np.int32)
                with open(f"{output_prefix}_packed.buf", "wb") as file_obj:
                    file_obj.write(packed_data.tobytes())
                with open(f"{output_prefix}_map.buf", "wb") as file_obj:
                    file_obj.write(map_array.tobytes())
                slot_maps[slot_num] = map_array
            else:
                with open(f"{output_prefix}.buf", "wb") as file_obj:
                    file_obj.write(target_data.astype(np.float32, copy=False).tobytes())

        return slot_maps

    def _write_merged_slot_files(self, logical_hash, runtime_info, hash_slot_data, slot_position_overrides):
        use_delta = self.node.store_deltas
        actual_hash = runtime_info["actual_hash"]
        base_bytes = runtime_info["base_bytes"]
        struct_definition = self.node._get_vertex_struct_definition()
        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0

        merged_index_map = None
        merged_data_parts = []
        next_global_index = 0
        base_data = None

        for slot_num, names_data in sorted(hash_slot_data.items()):
            target_bytes = self._compose_slot_bytes(
                logical_hash,
                runtime_info,
                slot_num,
                names_data,
                slot_position_overrides,
            )
            if base_data is None:
                vertex_stride, num_floats_per_vertex, num_vertices = self.node._detect_vertex_format(
                    base_bytes,
                    target_bytes,
                    struct_definition,
                )
                base_data = np.frombuffer(base_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))
                merged_index_map = np.full((num_vertices, num_slots), -1, dtype=np.int32)

            target_data = np.frombuffer(target_bytes, dtype=np.float32).reshape(base_data.shape)
            if use_delta:
                data_to_write = target_data[:, :3] - base_data[:, :3]
                data_to_write[data_to_write == 0] = 0.0
                diff_mask = ~np.isclose(base_data[:, :3], target_data[:, :3], atol=1e-6).all(axis=1)
            else:
                data_to_write = target_data
                diff_mask = ~np.isclose(base_data, target_data, atol=1e-6).all(axis=1)

            active_count = int(np.count_nonzero(diff_mask))
            if active_count > 0:
                packed_data = data_to_write[diff_mask]
                merged_data_parts.append(packed_data)

                slot_index_map = np.full(base_data.shape[0], -1, dtype=np.int32)
                slot_index_map[diff_mask] = np.arange(next_global_index, next_global_index + active_count, dtype=np.int32)
                merged_index_map[:, slot_num - 1] = slot_index_map
                next_global_index += active_count

        if merged_index_map is None:
            raise ShapeKeyDirectExportError(f"{logical_hash} 未生成任何可用的形态键槽位数据")

        if merged_data_parts:
            merged_data = np.concatenate(merged_data_parts, axis=0)
        else:
            merged_data = np.empty((0, 3 if use_delta else base_data.shape[1]), dtype=np.float32)

        data_suffix = "_merged_packed_pos_delta" if use_delta else "_merged_packed"
        data_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position{data_suffix}.buf")
        map_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_merged_map.buf")
        with open(data_path, "wb") as file_obj:
            file_obj.write(merged_data.tobytes())
        with open(map_path, "wb") as file_obj:
            file_obj.write(merged_index_map.reshape(-1).tobytes())

        return merged_index_map

    def _write_freq_indices(
        self,
        logical_hash,
        actual_hash,
        hash_slot_data,
        unique_names,
        vertex_count,
        calculated_ranges,
        merged_index_map=None,
        slot_index_maps=None,
    ):
        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0
        if num_slots <= 0 or vertex_count <= 0:
            return

        name_to_freq_index = {name: index for index, name in enumerate(unique_names)}
        freq_indices = np.full((vertex_count, num_slots), 255, dtype=np.uint32)
        slot_index_maps = slot_index_maps or {}

        for slot_num, names_data in hash_slot_data.items():
            slot_index = slot_num - 1
            index_map = merged_index_map[:, slot_index] if merged_index_map is not None else slot_index_maps.get(slot_num)

            for shapekey_name, objects in names_data.items():
                freq_idx = name_to_freq_index.get(shapekey_name, 255)
                if freq_idx == 255:
                    continue

                for obj_name in objects:
                    range_tuple = calculated_ranges.get(obj_name)
                    if not range_tuple:
                        continue

                    start_v, end_v = range_tuple[:2]
                    start_v = max(0, min(start_v, vertex_count - 1))
                    end_v = max(0, min(end_v, vertex_count - 1))

                    if index_map is not None:
                        valid_local = np.flatnonzero(index_map[start_v:end_v + 1] >= 0)
                        if valid_local.size > 0:
                            valid_vertices = valid_local + start_v
                            freq_indices[valid_vertices, slot_index] = freq_idx
                    else:
                        freq_indices[start_v:end_v + 1, slot_index] = freq_idx

        output_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_freq_indices.buf")
        with open(output_path, "wb") as file_obj:
            file_obj.write(freq_indices.reshape(-1).tobytes())

    def _compose_slot_bytes(self, logical_hash, runtime_info, slot_index, names_data, slot_position_overrides):
        base_bytes = runtime_info["base_bytes"]
        position_stride = runtime_info["position_stride"]
        slot_bytes = bytearray(base_bytes)

        slot_overrides = slot_position_overrides.get(slot_index, {})
        for objects in names_data.values():
            for obj_name in objects:
                override_entry = slot_overrides.get(obj_name)
                if not override_entry:
                    continue

                obj_hash = self.node._extract_hash_from_name(obj_name)
                obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                if obj_prefix != hash_prefix:
                    continue

                export_indices = np.asarray(override_entry.get("export_indices", []), dtype=np.int32)
                position_bytes = override_entry.get("position_bytes", b"")
                if export_indices.size == 0 or not position_bytes:
                    continue

                expected_bytes = export_indices.size * position_stride
                if len(position_bytes) != expected_bytes:
                    raise ShapeKeyDirectExportError(
                        f"物体 '{obj_name}' 的 Position 数据长度异常，期望={expected_bytes}，实际={len(position_bytes)}"
                    )

                try:
                    apply_position_override_in_place(
                        state_bytes=slot_bytes,
                        position_bytes=position_bytes,
                        export_indices=export_indices,
                        position_stride=position_stride,
                    )
                except ValueError as exc:
                    raise ShapeKeyDirectExportError(str(exc)) from exc

        return bytes(slot_bytes)

    def _build_runtime_infos(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            base_path, actual_hash = self.node._resolve_position_buffer_path(
                self.mod_export_path,
                "Meshes0000",
                logical_hash,
            )
            if not os.path.exists(base_path):
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 文件不存在 {base_path}")
                continue

            drawib_model = self._match_drawib_model(actual_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配基础 DrawIB 模型")
                continue

            with open(base_path, "rb") as file_obj:
                base_bytes = file_obj.read()

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 Position 文件")

        return runtime_infos

    def _build_runtime_infos_from_exporter_buffers(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            base_path, actual_hash = self.node._resolve_position_buffer_path(
                self.mod_export_path,
                "Meshes0000",
                logical_hash,
            )
            if not os.path.exists(base_path):
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 文件不存在 {base_path}")
                continue

            drawib_model = self._match_drawib_model(actual_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配基础 DrawIB 模型")
                continue

            shapekey_buffers = getattr(drawib_model, "shapekey_name_bytelist_dict", {}) or {}
            if not shapekey_buffers:
                continue

            with open(base_path, "rb") as file_obj:
                base_bytes = file_obj.read()

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "shapekey_buffers": shapekey_buffers,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 ShapeKey 数据")

        return runtime_infos

    def _parse_hash_to_base_resources(self, sections):
        hash_to_base_resources = {}
        resource_pattern = re.compile(r'\[(Resource_?([a-f0-9]{8}(?:[_-][a-f0-9]+)*)_?Position(\d*))\]')
        for section_name in sections.keys():
            match = resource_pattern.match(section_name)
            if not match:
                continue
            full_name, hash_value, number = match.groups()
            if number:
                continue
            hash_value_normalized = hash_value.replace("_", "-")
            hash_prefix = self.node._extract_hash_prefix(hash_value_normalized)
            if not hash_prefix:
                continue
            hash_to_base_resources.setdefault(hash_prefix, []).append((int(number) if number else 1, full_name))

        for hash_prefix in hash_to_base_resources:
            hash_to_base_resources[hash_prefix].sort()
            hash_to_base_resources[hash_prefix] = [name for _, name in hash_to_base_resources[hash_prefix]]
        return hash_to_base_resources

    def _update_ini_sections(
        self,
        sections,
        preserved_tail_content,
        target_ini_file,
        slot_to_name_to_objects,
        unique_hashes,
        hash_to_objects,
        all_unique_names,
        all_unique_objects,
        calculated_ranges,
        hash_to_stride,
        hash_to_actual_file_hash,
        hash_to_vertex_count,
        hash_slot_data_map,
        hash_to_base_resources,
        use_packed,
        use_delta,
        use_optimized,
        merge_slot_files,
    ):
        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        constants_lines = sections['[Constants]']
        constants_content = "".join(constants_lines)
        vars_to_define = set()

        existing_param_names = set()
        shapekey_freq_params = {}
        for name in all_unique_names:
            shapekey_freq_params[name] = self.node._create_safe_var_name(
                name,
                prefix="$Freq_",
                existing_names=existing_param_names,
            )

        intensity_lines = []
        for name, param in shapekey_freq_params.items():
            if param not in constants_content:
                intensity_lines.append(f"; 控制形态键 '{name}' 的强度")
                intensity_lines.append(f"global persist {param} = 0.0")

        if intensity_lines:
            constants_lines.append("\n; --- Auto-generated Shape Key Intensity Controls (Additive Blending) ---")
            constants_lines.extend(intensity_lines)

        existing_vertex_range_names = set()
        vertex_range_vars = {}
        vertex_range_lines = []
        for obj_name, range_tuple in calculated_ranges.items():
            start_v, end_v = range_tuple[:2]
            if start_v is None:
                continue
            safe_name = self.node._create_safe_var_name(
                obj_name.replace("-", "_"),
                existing_names=existing_vertex_range_names,
            )
            start_var = f"$SV_{safe_name}"
            end_var = f"$EV_{safe_name}"
            vertex_range_vars[obj_name] = (start_var, end_var)
            if start_var not in constants_content:
                vertex_range_lines.append(f"global {start_var} = {start_v}")
            if end_var not in constants_content:
                vertex_range_lines.append(f"global {end_var} = {end_v}")

        if vertex_range_lines:
            constants_lines.append("\n; --- Auto-generated Vertex Ranges for Shape Keys ---")
            constants_lines.extend(vertex_range_lines)

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            res_to_post = base_resources if base_resources else [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]
            for res_name in res_to_post:
                if f"post {res_name} = copy_desc" not in constants_content:
                    constants_lines.append(f"post {res_name} = copy_desc {res_name}_0")
            if len(base_resources) > 1:
                vars_to_define.add("$swapkey100")
            if f"post run = CustomShader_{logical_hash}_Anim" not in constants_content:
                constants_lines.append(f"post run = CustomShader_{logical_hash}_Anim")

        base_mesh_switch_lines = []
        for var in sorted(vars_to_define):
            if f"global persist {var}" not in constants_content and f"global {var}" not in constants_content:
                base_mesh_switch_lines.append(f"global persist {var} = 1")
        if base_mesh_switch_lines:
            constants_lines.append("\n; --- Auto-generated Base Mesh Switch Key ---")
            constants_lines.extend(base_mesh_switch_lines)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        present_lines = sections['[Present]']
        rebuilt_present_lines = []
        line_index = 0
        while line_index < len(present_lines):
            stripped_line = present_lines[line_index].strip()
            if stripped_line == 'if $active0 == 1':
                end_index = line_index + 1
                inner_lines = []
                while end_index < len(present_lines):
                    candidate_line = present_lines[end_index]
                    candidate_stripped = candidate_line.strip()
                    if candidate_stripped == 'endif':
                        break
                    inner_lines.append(candidate_stripped)
                    end_index += 1

                if (
                    end_index < len(present_lines)
                    and inner_lines
                    and all(
                        (not inner_line) or inner_line.startswith('run = CustomShader_')
                        for inner_line in inner_lines
                    )
                ):
                    line_index = end_index + 1
                    continue

            rebuilt_present_lines.append(present_lines[line_index])
            line_index += 1

        rebuilt_present_lines.extend([
            'if $active0 == 1',
            *[f"    run = CustomShader_{h}_Anim" for h in unique_hashes],
            'endif',
        ])
        sections['[Present]'] = rebuilt_present_lines

        compute_blocks_to_add = OrderedDict()
        for logical_hash in unique_hashes:
            hash_objects = hash_to_objects.get(logical_hash, [])
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
                    if obj in hash_objects
                )
            )

            block_name = f"[CustomShader_{logical_hash}_Anim]"
            block_lines = ["\n    ; --- Shared Intensity Controls (per Shape Key Name) ---"]
            for index, name in enumerate(hash_unique_names):
                freq_param = shapekey_freq_params.get(name)
                if freq_param:
                    block_lines.append(f"    x{self.node.INTENSITY_START_INDEX + index} = {freq_param} \n; {name}")
            block_lines.append("\n    ; --- Per-Object Vertex Range Controls ---")
            for index, obj_name in enumerate(hash_unique_objects):
                if obj_name not in calculated_ranges or calculated_ranges[obj_name][0] is None:
                    continue
                start_var, end_var = vertex_range_vars.get(obj_name, ("$SV_unknown", "$EV_unknown"))
                block_lines.append(f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2} = {start_var} \n; {obj_name} Start")
                block_lines.append(f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2 + 1} = {end_var} \n; {obj_name} End")

            t_registers_to_null = []
            slots_for_hash = sorted(hash_slot_data.keys())
            if not use_delta:
                block_lines.append(f"\n    cs-t50 = copy Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position0000")
                t_registers_to_null.append("cs-t50")

            mode_str = (
                f"紧凑:{'是' if use_packed else '否'}, "
                f"增量(仅位置):{'是' if use_delta else '否'}, "
                f"优化查找:{'是' if use_optimized else '否'}, "
                f"文件合并:{'是' if merge_slot_files else '否'}"
            )
            block_lines.append(f"\n    ; --- Binding Shape Key Meshess (Mode: {mode_str}) ---")
            if merge_slot_files:
                block_lines.append(f"    cs-t51 = copy {self.node._get_merged_data_resource_name(logical_hash, use_delta)}")
                block_lines.append(f"    cs-t52 = copy {self.node._get_merged_map_resource_name(logical_hash)}")
                t_registers_to_null.extend(["cs-t51", "cs-t52"])

                if use_optimized:
                    block_lines.append(f"    cs-t53 = copy Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position_FreqIndices")
                    t_registers_to_null.append("cs-t53")
            else:
                res_suffix = "_packed_pos_delta" if use_packed and use_delta else "_pos_delta" if use_delta else "_packed" if use_packed else ""
                for slot_num in slots_for_hash:
                    res_name = f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position1{slot_num:03d}{res_suffix}"
                    if not (use_packed or use_delta):
                        res_name = f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position1{slot_num:03d}"

                    t_reg = 51 + slot_num - 1
                    block_lines.append(f"    cs-t{t_reg} = copy {res_name}")
                    t_registers_to_null.append(f"cs-t{t_reg}")
                    if use_packed:
                        map_reg = 75 + slot_num - 1
                        block_lines.append(
                            f"    cs-t{map_reg} = copy Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position1{slot_num:03d}_Map"
                        )
                        t_registers_to_null.append(f"cs-t{map_reg}")

                if use_optimized:
                    block_lines.append(f"    cs-t99 = copy Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position_FreqIndices")
                    t_registers_to_null.append("cs-t99")

            block_lines.append(f"    cs = ./res/shapekey_anim_{logical_hash}.hlsl")

            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            res_to_bind = base_resources if base_resources else [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]
            if len(res_to_bind) > 1:
                block_lines.append(f"\n    ; --- Base Mesh Switching ---")
                for index, res_name in enumerate(res_to_bind, 1):
                    block_lines.extend([f"    if $swapkey100 == {index}", f"        cs-u5 = copy {res_name}_0", f"        {res_name} = ref cs-u5", "    endif"])
            else:
                res_name = res_to_bind[0]
                block_lines.extend([f"    cs-u5 = copy {res_name}_0", f"    {res_name} = ref cs-u5"])

            dispatch_count = hash_to_vertex_count.get(hash_prefix, 10000) or 10000
            block_lines.extend([f"    Dispatch = {dispatch_count}, 1, 1", "    cs-u5 = null", *[f"    {reg} = null" for reg in sorted(list(set(t_registers_to_null)))]] )
            compute_blocks_to_add[block_name] = block_lines

        new_resource_lines = []
        generated_section_names = set()

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
            section_name = f"[Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position0000]"
            if section_name not in sections and section_name not in generated_section_names:
                stride = hash_to_stride.get(hash_prefix, 40)
                new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = Meshes0000/{actual_file_hash}-Position.buf", ""])
                generated_section_names.add(section_name)

        if merge_slot_files:
            for logical_hash in unique_hashes:
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                if not hash_prefix:
                    continue

                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                base_stride = hash_to_stride.get(hash_prefix, 40)
                data_stride = 12 if use_delta else base_stride
                data_section = f"[{self.node._get_merged_data_resource_name(logical_hash, use_delta)}]"
                data_filename = f"Meshes0000/{actual_file_hash}-Position{self.node._get_merged_data_file_suffix(use_delta)}.buf"
                if data_section not in sections and data_section not in generated_section_names:
                    new_resource_lines.extend([data_section, "type = Buffer", f"stride = {data_stride}", f"filename = {data_filename}", ""])
                    generated_section_names.add(data_section)

                map_section = f"[{self.node._get_merged_map_resource_name(logical_hash)}]"
                map_filename = f"Meshes0000/{actual_file_hash}-Position_merged_map.buf"
                if map_section not in sections and map_section not in generated_section_names:
                    new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = {map_filename}", ""])
                    generated_section_names.add(map_section)
        else:
            for slot_num, name_data in slot_to_name_to_objects.items():
                for obj_name in [obj for _, objects in name_data.items() for obj in objects]:
                    logical_hash = self.node._extract_hash_from_name(obj_name)
                    hash_prefix = self.node._extract_hash_prefix(logical_hash) if logical_hash else None
                    if not hash_prefix:
                        continue
                    actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                    base_stride = hash_to_stride.get(hash_prefix, 40)
                    if use_delta:
                        res_suffix = "_packed_pos_delta" if use_packed else "_pos_delta"
                        stride = 12
                    elif use_packed:
                        res_suffix = "_packed"
                        stride = base_stride
                    else:
                        res_suffix = ""
                        stride = base_stride

                    if use_delta or use_packed:
                        section_name = f"[Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position1{slot_num:03d}{res_suffix}]"
                        filename = f"Meshes0000/{actual_file_hash}-Position1{slot_num:03d}{res_suffix}.buf"
                        if section_name not in sections and section_name not in generated_section_names:
                            new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = {filename}", ""])
                            generated_section_names.add(section_name)

                    if use_packed:
                        map_section = f"[Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position1{slot_num:03d}_Map]"
                        if map_section not in sections and map_section not in generated_section_names:
                            new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position1{slot_num:03d}_map.buf", ""])
                            generated_section_names.add(map_section)

        if use_optimized:
            for logical_hash in unique_hashes:
                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                freq_idx_section = f"[Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position_FreqIndices]"
                if freq_idx_section not in sections and freq_idx_section not in generated_section_names:
                    new_resource_lines.extend([freq_idx_section, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position_freq_indices.buf", ""])
                    generated_section_names.add(freq_idx_section)

        if new_resource_lines:
            sections[";; --- Generated Shape Key Meshess ---"] = new_resource_lines

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            for res_name in hash_to_base_resources.get(hash_prefix, [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]):
                if f"[{res_name}]" in sections and not any(f"[{res_name}_0]" in line for line in sections[f"[{res_name}]"]):
                    sections[f"[{res_name}]"].insert(0, f"[{res_name}_0]")

        sections.update(compute_blocks_to_add)
        self.node._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content)
