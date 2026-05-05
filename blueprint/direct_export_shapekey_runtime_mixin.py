from collections import Counter, OrderedDict, defaultdict

import numpy as np

from ..utils.log_utils import LOG
from .direct_export_runtime_utils import get_model_vertex_count as _get_model_vertex_count
from .direct_export_runtime_utils import iter_drawib_models as _iter_drawib_models
from .direct_export_shapekey_shared import ShapeKeyDirectExportError


class DirectShapeKeyRuntimeMixin:
    def _match_drawib_model(self, actual_hash: str, logical_hash: str):
        logical_prefix = self.node._extract_hash_prefix(logical_hash)
        for drawib_model in _iter_drawib_models(self.exporter):
            candidate_keys = self._get_drawib_candidate_keys(drawib_model)
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

    def _get_drawib_candidate_keys(self, drawib_model):
        candidate_keys = {
            getattr(drawib_model, "draw_ib", ""),
            getattr(drawib_model, "draw_ib_alias", ""),
        }

        for submesh_model in getattr(drawib_model, "submesh_model_list", []) or []:
            candidate_keys.add(getattr(submesh_model, "unique_str", ""))
            candidate_keys.add(getattr(submesh_model, "match_draw_ib", ""))

        return {key for key in candidate_keys if key}

    def _get_configured_vertex_stride(self) -> int:
        get_struct_definition = getattr(self.node, "_get_vertex_struct_definition", None)
        parse_vertex_struct = getattr(self.node, "parse_vertex_struct", None)
        if not callable(get_struct_definition) or not callable(parse_vertex_struct):
            return 0

        try:
            struct_definition = get_struct_definition()
            parsed = parse_vertex_struct(struct_definition)
        except Exception:
            return 0

        if not parsed:
            return 0

        try:
            return int(parsed[0])
        except (TypeError, ValueError, IndexError):
            return 0

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
        configured_stride = self._get_configured_vertex_stride()
        if configured_stride > 0 and len(base_bytes) % configured_stride == 0:
            return configured_stride

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

        raise ShapeKeyDirectExportError(f"无法推断 Position 步长: draw_ib={getattr(drawib_model, 'draw_ib', '')}")

    def _calculate_object_ranges(self, runtime_infos, all_objects, sections=None):
        if sections:
            draw_info_map = self.node._parse_ini_for_draw_info(sections, self.mod_export_path)
            if draw_info_map:
                calculated_ranges = self._calculate_object_ranges_from_draw_info(draw_info_map, all_objects)
                if calculated_ranges:
                    return calculated_ranges

        mesh_name_to_range_and_label = {}
        mesh_candidates_by_prefix = defaultdict(list)
        mesh_runtime_alias_map = defaultdict(list)
        mesh_base_alias_map = defaultdict(list)

        for runtime_info in runtime_infos.values():
            drawib_model = runtime_info["drawib_model"]
            for mesh_name, start_v, end_v, label in self._iter_object_range_entries(drawib_model):
                candidate_names = list(OrderedDict.fromkeys(self._iter_name_variants(mesh_name)))
                if not candidate_names:
                    continue

                for candidate_name in candidate_names:
                    candidate = (candidate_name, start_v, end_v, label)
                    mesh_name_to_range_and_label.setdefault(candidate_name, (start_v, end_v, label))

                    mesh_hash = self.node._extract_hash_from_name(candidate_name)
                    mesh_prefix = self.node._extract_hash_prefix(mesh_hash) if mesh_hash else None
                    if not mesh_prefix:
                        continue

                    mesh_alias = self.node._extract_alias_from_name(candidate_name)
                    runtime_alias = self.node._strip_runtime_copy_suffix(mesh_alias).casefold()
                    base_alias = self.node._strip_object_suffix(mesh_alias).casefold()
                    mesh_candidates_by_prefix[mesh_prefix].append(candidate)
                    mesh_runtime_alias_map[(mesh_prefix, runtime_alias)].append(candidate)
                    mesh_base_alias_map[(mesh_prefix, base_alias)].append(candidate)

        calculated_ranges = {}
        for obj_name in all_objects:
            if obj_name in mesh_name_to_range_and_label:
                calculated_ranges[obj_name] = mesh_name_to_range_and_label[obj_name]
                continue

            obj_hash = self.node._extract_hash_from_name(obj_name)
            if not obj_hash:
                continue

            obj_prefix = self.node._extract_hash_prefix(obj_hash)
            obj_alias = self.node._extract_alias_from_name(obj_name)
            obj_runtime_alias = self.node._strip_runtime_copy_suffix(obj_alias).casefold()
            obj_base_alias = self.node._strip_object_suffix(obj_alias).casefold()

            runtime_matches = mesh_runtime_alias_map.get((obj_prefix, obj_runtime_alias), [])
            if len(runtime_matches) == 1:
                _, start_v, end_v, label = runtime_matches[0]
                calculated_ranges[obj_name] = (start_v, end_v, label)
                continue

            base_matches = mesh_base_alias_map.get((obj_prefix, obj_base_alias), [])
            if len(base_matches) == 1:
                _, start_v, end_v, label = base_matches[0]
                calculated_ranges[obj_name] = (start_v, end_v, label)
                continue

            prefix_candidates = mesh_candidates_by_prefix.get(obj_prefix, [])
            if len(prefix_candidates) == 1:
                _, start_v, end_v, label = prefix_candidates[0]
                calculated_ranges[obj_name] = (start_v, end_v, label)

        return calculated_ranges

    def _calculate_object_ranges_from_draw_info(self, draw_info_map, all_objects):
        mesh_name_to_range_and_label = {}
        mesh_candidates_by_prefix = defaultdict(list)
        mesh_runtime_alias_map = defaultdict(list)
        mesh_base_alias_map = defaultdict(list)

        for mesh_name, info_list in draw_info_map.items():
            all_ranges = []
            label = ""
            for info in info_list:
                start_v, end_v = self.node._calculate_vertex_range(info['ib_path'], info['draw_params'])
                if start_v is None or end_v is None:
                    continue
                all_ranges.append((start_v, end_v))
                if not label:
                    label = info['ib_path']

            if not all_ranges:
                continue

            min_start = min(item[0] for item in all_ranges)
            max_end = max(item[1] for item in all_ranges)
            mesh_name_to_range_and_label[mesh_name] = (min_start, max_end, label)

            mesh_hash = self.node._extract_hash_from_name(mesh_name)
            mesh_prefix = self.node._extract_hash_prefix(mesh_hash) if mesh_hash else None
            if not mesh_prefix:
                continue

            mesh_alias = self.node._extract_alias_from_name(mesh_name)
            runtime_alias = self.node._strip_runtime_copy_suffix(mesh_alias).casefold()
            base_alias = self.node._strip_object_suffix(mesh_alias).casefold()
            candidate = (mesh_name, min_start, max_end, label)
            mesh_candidates_by_prefix[mesh_prefix].append(candidate)
            mesh_runtime_alias_map[(mesh_prefix, runtime_alias)].append(candidate)
            mesh_base_alias_map[(mesh_prefix, base_alias)].append(candidate)

        calculated_ranges = {}
        for obj_name in all_objects:
            if obj_name in mesh_name_to_range_and_label:
                calculated_ranges[obj_name] = mesh_name_to_range_and_label[obj_name]
                continue

            obj_hash = self.node._extract_hash_from_name(obj_name)
            if not obj_hash:
                continue

            obj_prefix = self.node._extract_hash_prefix(obj_hash)
            obj_alias = self.node._extract_alias_from_name(obj_name)
            obj_runtime_alias = self.node._strip_runtime_copy_suffix(obj_alias).casefold()
            obj_base_alias = self.node._strip_object_suffix(obj_alias).casefold()

            runtime_matches = mesh_runtime_alias_map.get((obj_prefix, obj_runtime_alias), [])
            if len(runtime_matches) == 1:
                _, start_v, end_v, label = runtime_matches[0]
                calculated_ranges[obj_name] = (start_v, end_v, label)
                continue

            base_matches = mesh_base_alias_map.get((obj_prefix, obj_base_alias), [])
            if len(base_matches) == 1:
                _, start_v, end_v, label = base_matches[0]
                calculated_ranges[obj_name] = (start_v, end_v, label)
                continue

            prefix_candidates = mesh_candidates_by_prefix.get(obj_prefix, [])
            if len(prefix_candidates) == 1:
                _, start_v, end_v, label = prefix_candidates[0]
                calculated_ranges[obj_name] = (start_v, end_v, label)

        return calculated_ranges

    def _apply_merged_ranges(self, calculated_ranges, slot_to_name_to_objects, hash_to_objects, all_objects):
        range_to_objects = OrderedDict()
        range_label_map = {}

        for obj_name, range_tuple in calculated_ranges.items():
            start_v, end_v = range_tuple[:2]
            if start_v is None:
                continue
            obj_hash = self.node._extract_hash_from_name(obj_name)
            merge_identity = self.node._get_merge_identity_alias(obj_name)
            merge_key = (obj_hash or "", start_v, end_v, merge_identity)
            range_to_objects.setdefault(merge_key, []).append(obj_name)
            range_label_map.setdefault(merge_key, range_tuple[2] if len(range_tuple) > 2 else (obj_hash or ""))

        merged_ranges = OrderedDict()
        range_name_mapping = {}
        for (obj_hash, start_v, end_v, merge_identity), obj_names in range_to_objects.items():
            label = range_label_map.get((obj_hash, start_v, end_v, merge_identity), obj_hash)
            if len(obj_names) == 1:
                merged_name = obj_names[0]
            else:
                merge_aliases = [self.node._get_merge_identity_alias(name) for name in obj_names]
                base_name = Counter(merge_aliases).most_common(1)[0][0]
                merged_name = f"{obj_hash}.{base_name}_x{len(obj_names)}" if obj_hash else f"{base_name}_x{len(obj_names)}"
            merged_ranges[merged_name] = (start_v, end_v, label)
            for obj_name in obj_names:
                range_name_mapping[obj_name] = merged_name

        merged_name_members = defaultdict(list)
        for original_name, merged_name in range_name_mapping.items():
            if original_name != merged_name:
                merged_name_members[merged_name].append(original_name)
        self.merged_name_members = {name: members for name, members in merged_name_members.items()}
        if not range_name_mapping:
            return list(OrderedDict.fromkeys(
                h for obj in all_objects if (h := self.node._extract_hash_from_name(obj))
            ))

        calculated_ranges.clear()
        calculated_ranges.update(merged_ranges)

        new_all_objects = []
        seen_merged = set()
        for obj_name in all_objects:
            merged_name = range_name_mapping.get(obj_name, obj_name)
            if merged_name not in seen_merged:
                new_all_objects.append(merged_name)
                seen_merged.add(merged_name)
        all_objects[:] = new_all_objects

        for slot_index, name_data in slot_to_name_to_objects.items():
            for shapekey_name, objects in name_data.items():
                merged_objects = []
                seen_names = set()
                for obj_name in objects:
                    merged_name = range_name_mapping.get(obj_name, obj_name)
                    if merged_name not in seen_names:
                        merged_objects.append(merged_name)
                        seen_names.add(merged_name)
                slot_to_name_to_objects[slot_index][shapekey_name] = merged_objects

        for logical_hash, objects in list(hash_to_objects.items()):
            merged_objects = []
            seen_names = set()
            for obj_name in objects:
                merged_name = range_name_mapping.get(obj_name, obj_name)
                if merged_name not in seen_names:
                    merged_objects.append(merged_name)
                    seen_names.add(merged_name)
            if merged_objects:
                hash_to_objects[logical_hash] = merged_objects
            else:
                del hash_to_objects[logical_hash]

        return list(OrderedDict.fromkeys(
            h for obj in all_objects if (h := self.node._extract_hash_from_name(obj))
        ))

    def _iter_merged_member_names(self, obj_name: str):
        yield obj_name
        for member_name in self.merged_name_members.get(obj_name, []):
            yield member_name

    def _iter_object_range_entries(self, drawib_model):
        object_export_context_map = getattr(drawib_model, "object_export_context_map", {}) or {}
        if object_export_context_map:
            for candidate_name, context in object_export_context_map.items():
                export_indices = np.asarray(context.get("export_indices", []), dtype=np.int32)
                if export_indices.size == 0:
                    continue
                yield (
                    candidate_name,
                    int(export_indices.min()),
                    int(export_indices.max()),
                    context.get("label", getattr(drawib_model, "draw_ib", "")),
                )
            return

        submesh_model_list = getattr(drawib_model, "submesh_model_list", None) or []
        if not submesh_model_list:
            return

        split_ib_dict = getattr(drawib_model, "submesh_ib_dict", {}) or {}
        combined_ib = getattr(drawib_model, "ib", []) or []
        draw_offset_map = getattr(drawib_model, "obj_name_draw_offset", {}) or {}

        for submesh_model in submesh_model_list:
            submesh_key = getattr(submesh_model, "unique_str", "")
            submesh_ib = split_ib_dict.get(submesh_key)
            if submesh_ib is None:
                submesh_ib = combined_ib

            for draw_call_model in getattr(submesh_model, "drawcall_model_list", None) or []:
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
                label = submesh_key or getattr(drawib_model, "draw_ib", "")

                candidate_names = {
                    obj_name,
                    getattr(draw_call_model, "source_obj_name", ""),
                    draw_call_model.get_blender_obj_name(),
                    _normalize_runtime_name(draw_call_model.get_blender_obj_name()),
                }
                for candidate_name in candidate_names:
                    if candidate_name:
                        yield candidate_name, start_v, end_v, label

    def _build_hash_slot_data_map(self, unique_hashes, slot_to_name_to_objects):
        hash_slot_data_map = {}
        for logical_hash in unique_hashes:
            logical_prefix = self.node._extract_hash_prefix(logical_hash)
            hash_slot_data = {}
            for slot, name_data in slot_to_name_to_objects.items():
                slot_entries = {}
                for name, objects in name_data.items():
                    matched_objects = []
                    for obj_name in objects:
                        obj_hash = self.node._extract_hash_from_name(obj_name)
                        obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                        if obj_prefix == logical_prefix:
                            matched_objects.append(obj_name)
                    if matched_objects:
                        slot_entries[name] = matched_objects
                if slot_entries:
                    hash_slot_data[slot] = slot_entries
            hash_slot_data_map[logical_hash] = hash_slot_data
        return hash_slot_data_map

    def _filter_hash_slot_data_map_by_overrides(self, hash_slot_data_map, slot_position_overrides):
        filtered_map = {}
        for logical_hash, hash_slot_data in hash_slot_data_map.items():
            filtered_slot_data = {}
            for slot_num, names_data in hash_slot_data.items():
                slot_overrides = slot_position_overrides.get(slot_num, {})
                filtered_names_data = {}
                for shapekey_name, objects in names_data.items():
                    active_objects = [obj_name for obj_name in objects if obj_name in slot_overrides]
                    if active_objects:
                        filtered_names_data[shapekey_name] = active_objects
                if filtered_names_data:
                    filtered_slot_data[slot_num] = filtered_names_data
            filtered_map[logical_hash] = filtered_slot_data
        return filtered_map

    def _analyze_hash_slot_filters(self, unique_hashes, slot_to_name_to_objects, slot_position_overrides):
        raw_hash_slot_data_map = self._build_hash_slot_data_map(unique_hashes, slot_to_name_to_objects)
        filtered_hash_slot_data_map = self._filter_hash_slot_data_map_by_overrides(
            raw_hash_slot_data_map,
            slot_position_overrides,
        )

        dropped_slots = {}
        for logical_hash in unique_hashes:
            raw_slot_data = raw_hash_slot_data_map.get(logical_hash, {})
            filtered_slot_data = filtered_hash_slot_data_map.get(logical_hash, {})
            raw_slots = set(raw_slot_data.keys())
            filtered_slots = set(filtered_slot_data.keys())
            missing_slots = sorted(raw_slots - filtered_slots)

            missing_entries = {}
            for slot_num, names_data in raw_slot_data.items():
                filtered_names_data = filtered_slot_data.get(slot_num, {})
                for shapekey_name, objects in names_data.items():
                    filtered_objects = set(filtered_names_data.get(shapekey_name, []))
                    missing_objects = [obj_name for obj_name in objects if obj_name not in filtered_objects]
                    if missing_objects:
                        missing_entries.setdefault(slot_num, {})[shapekey_name] = missing_objects

            if missing_slots or missing_entries:
                dropped_slots[logical_hash] = {
                    "raw_slots": sorted(raw_slots),
                    "filtered_slots": sorted(filtered_slots),
                    "dropped_slots": missing_slots,
                    "dropped_entries": missing_entries,
                }

        return raw_hash_slot_data_map, filtered_hash_slot_data_map, dropped_slots
