import hashlib

import bpy

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.m_key import M_Key
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..utils.shapekey_utils import ShapeKeyUtils


def _get_node_unique_key(node) -> str:
    tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
    return f"{tree_name}::{node.name}"


class BlueprintExportHelper:
    """蓝图导出辅助类，提供蓝图树查找、形状键管理、前/后处理协调等静态方法"""

    DEFAULT_RESULT_OUTPUT_NODE_TYPE = 'SSMTNode_Result_Output'

    current_export_index = 1

    max_export_count = 1

    runtime_blueprint_tree_name = ""
    runtime_result_output_node_type = ""
    multi_file_export_nodes = []

    current_buffer_folder_name = "Meshes"

    # 这些运行时字段由标准导出、并行导出和直出共同读写，用来传递形态键上下文。
    runtime_shapekey_buffer_names = []
    runtime_shapekey_buffer_name_map = {}
    preserve_current_shapekey_mix_for_export = False
    suppress_shapekey_resource_export = False
    capture_direct_shapekey_positions = False
    direct_shapekey_position_records = {}

    MAX_EXPORT_COUNT_LIMIT = 1000
    BLUEPRINT_NONE_IDENTIFIER = "__NONE__"
    BLUEPRINT_NONE_ENUM_NUMBER = 0

    @staticmethod
    def should_ignore_muted_shape_keys() -> bool:
        try:
            return bool(GlobalProterties.ignore_muted_shape_keys())
        except Exception:
            return False

    @staticmethod
    def get_disabled_shape_key_export_names() -> set:
        """收集当前蓝图树中形态键配置节点里被取消勾选的形态键名；这些形态键不参与导出。
        无有效树或节点时返回空集（不过滤）。"""
        disabled_names = set()
        try:
            tree = BlueprintExportHelper.get_current_blueprint_tree()
        except Exception:
            tree = None
        if not tree:
            return disabled_names
        try:
            shapekey_nodes = BlueprintExportHelper.collect_shapekey_postprocess_nodes(tree)
        except Exception:
            return disabled_names
        for node in shapekey_nodes:
            for item in getattr(node, "shapekey_variable_items", None) or []:
                if getattr(item, "export_enabled", True):
                    continue
                name = str(getattr(item, "shape_key_name", "") or "").strip()
                if name:
                    disabled_names.add(name)
        return disabled_names

    @staticmethod
    def get_exportable_shape_key_infos(obj, slot_limit: int | None = None) -> list[tuple[int, str, object]]:
        if obj is None or not getattr(obj, "data", None):
            return []

        ignore_muted = BlueprintExportHelper.should_ignore_muted_shape_keys()
        disabled_names = BlueprintExportHelper.get_disabled_shape_key_export_names()
        slot_infos = []
        slot_index = 0
        for key_block in ShapeKeyUtils.iter_exportable_shape_keys(obj) or ():
            if ignore_muted and getattr(key_block, "mute", False):
                continue
            if disabled_names and str(getattr(key_block, "name", "") or "").strip() in disabled_names:
                continue
            slot_index += 1
            if slot_limit is not None and slot_index > slot_limit:
                break
            slot_infos.append((slot_index, str(getattr(key_block, "name", "") or ""), key_block))
        return slot_infos

    @staticmethod
    def get_exportable_shape_key_by_slot(obj, slot_index: int):
        if slot_index is None or slot_index <= 0:
            return None
        slot_limit = BlueprintExportHelper.max_shapekey_slot_count or None
        for dense_slot_index, _shape_key_name, key_block in BlueprintExportHelper.get_exportable_shape_key_infos(
            obj,
            slot_limit=slot_limit,
        ):
            if dense_slot_index == slot_index:
                return key_block
        return None

    @staticmethod
    def _is_valid_blueprint_tree(tree):
        return tree is not None and getattr(tree, "bl_idname", "") == 'SSMTBlueprintTreeType'

    @staticmethod
    def set_runtime_result_output_node_type(node_type: str = ""):
        BlueprintExportHelper.runtime_result_output_node_type = str(node_type or "").strip()

    @staticmethod
    def clear_runtime_result_output_node_type():
        BlueprintExportHelper.runtime_result_output_node_type = ""

    @staticmethod
    def iter_result_output_node_types():
        preferred = BlueprintExportHelper.runtime_result_output_node_type
        if preferred:
            yield preferred
        if preferred != BlueprintExportHelper.DEFAULT_RESULT_OUTPUT_NODE_TYPE:
            yield BlueprintExportHelper.DEFAULT_RESULT_OUTPUT_NODE_TYPE

    @staticmethod
    def is_result_output_node(node) -> bool:
        return getattr(node, "bl_idname", "") in set(BlueprintExportHelper.iter_result_output_node_types())

    @staticmethod
    def get_all_blueprint_trees():
        blueprint_trees = [
            node_group for node_group in bpy.data.node_groups
            if BlueprintExportHelper._is_valid_blueprint_tree(node_group)
        ]
        blueprint_trees.sort(key=lambda tree: tree.name.casefold())
        return blueprint_trees

    @staticmethod
    def get_blueprint_tree_by_name(tree_name):
        if not tree_name:
            return None

        tree = bpy.data.node_groups.get(tree_name)
        if BlueprintExportHelper._is_valid_blueprint_tree(tree):
            return tree

        return None

    @staticmethod
    def get_preferred_blueprint_name(selected_name="", context=None):
        selected_tree = BlueprintExportHelper.get_blueprint_tree_by_name(selected_name)
        if selected_tree:
            return selected_tree.name

        current_tree = BlueprintExportHelper._get_blueprint_tree_from_context(context)
        if BlueprintExportHelper._is_valid_blueprint_tree(current_tree):
            return current_tree.name

        runtime_tree = BlueprintExportHelper.get_blueprint_tree_by_name(
            BlueprintExportHelper.runtime_blueprint_tree_name,
        )
        if runtime_tree:
            return runtime_tree.name

        workspace_tree = BlueprintExportHelper.get_blueprint_tree_by_name(GlobalConfig.get_workspace_name())
        if workspace_tree:
            return workspace_tree.name

        all_blueprints = BlueprintExportHelper.get_all_blueprint_trees()
        if all_blueprints:
            return all_blueprints[0].name

        return ""

    @staticmethod
    def _stable_blueprint_enum_number(identifier: str, used_numbers: set[int]) -> int:
        if identifier == BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER:
            used_numbers.add(BlueprintExportHelper.BLUEPRINT_NONE_ENUM_NUMBER)
            return BlueprintExportHelper.BLUEPRINT_NONE_ENUM_NUMBER

        digest = hashlib.blake2s(str(identifier).encode("utf-8"), digest_size=4).digest()
        number = int.from_bytes(digest, "little") & 0x7FFFFFFF
        if number == BlueprintExportHelper.BLUEPRINT_NONE_ENUM_NUMBER:
            number = 1
        while number in used_numbers:
            number += 1
            if number > 0x7FFFFFFF:
                number = 1
        used_numbers.add(number)
        return number

    @staticmethod
    def _enum_item_number(item, fallback_index: int) -> int:
        try:
            if len(item) >= 5:
                return int(item[4])
        except Exception:
            pass
        return int(fallback_index)

    @staticmethod
    def ensure_valid_selected_blueprint_name(context=None) -> str:
        scene = getattr(context, "scene", None) if context else getattr(bpy.context, "scene", None)
        global_properties = getattr(scene, "global_properties", None)
        if global_properties is None:
            return ""

        items = BlueprintExportHelper.get_blueprint_enum_items(context=context)
        identifiers = [item[0] for item in items]
        identifier_set = set(identifiers)
        number_to_identifier = {
            BlueprintExportHelper._enum_item_number(item, index): item[0]
            for index, item in enumerate(items)
        }

        raw_value = None
        raw_value_available = False
        try:
            raw_value = global_properties.get("selected_blueprint_name")
            raw_value_available = True
        except Exception:
            pass

        if isinstance(raw_value, str) and raw_value in identifier_set:
            return raw_value

        try:
            raw_number = int(raw_value)
            if raw_number in number_to_identifier:
                selected_identifier = number_to_identifier[raw_number]
                global_properties.selected_blueprint_name = selected_identifier
                return selected_identifier
        except Exception:
            pass

        if not raw_value_available:
            try:
                current_identifier = str(getattr(global_properties, "selected_blueprint_name", "") or "")
                if current_identifier in identifier_set:
                    return current_identifier
            except Exception:
                pass

        preferred_name = BlueprintExportHelper.get_preferred_blueprint_name(context=context)
        if preferred_name not in identifier_set:
            preferred_name = identifiers[0] if identifiers else BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER

        try:
            global_properties.selected_blueprint_name = preferred_name
        except Exception:
            pass
        return preferred_name

    @staticmethod
    def set_runtime_shapekey_buffer_names(shapekey_names):
        BlueprintExportHelper.runtime_shapekey_buffer_names = list(dict.fromkeys(
            name for name in (shapekey_names or []) if name
        ))

    @staticmethod
    def set_runtime_shapekey_buffer_name_map(shapekey_name_map):
        normalized_map = {}
        for key, shapekey_names in (shapekey_name_map or {}).items():
            if not key:
                continue
            normalized_names = list(dict.fromkeys(
                name for name in (shapekey_names or []) if name
            ))
            if normalized_names:
                normalized_map[key] = normalized_names
        BlueprintExportHelper.runtime_shapekey_buffer_name_map = normalized_map

    @staticmethod
    def get_runtime_shapekey_buffer_names(buffer_key=None):
        names = list(BlueprintExportHelper.runtime_shapekey_buffer_names)
        if buffer_key:
            key = str(buffer_key)
            if key.startswith("TEMP_SUBMESH_MERGED_"):
                key = key[len("TEMP_SUBMESH_MERGED_"):]

            key_prefix = key.split("-", 1)[0] if "-" in key else key
            for candidate_key in (buffer_key, key, key_prefix):
                names.extend(BlueprintExportHelper.runtime_shapekey_buffer_name_map.get(candidate_key, []))

        return list(dict.fromkeys(name for name in names if name))

    @staticmethod
    def clear_runtime_shapekey_buffer_names():
        BlueprintExportHelper.runtime_shapekey_buffer_names = []
        BlueprintExportHelper.runtime_shapekey_buffer_name_map = {}

    @staticmethod
    def reset_direct_export_runtime_state(clear_postprocess_caches: bool = False):
        BlueprintExportHelper.clear_runtime_shapekey_buffer_names()
        BlueprintExportHelper.set_preserve_current_shapekey_mix_for_export(False)
        BlueprintExportHelper.set_suppress_shapekey_resource_export(False)
        BlueprintExportHelper.set_capture_direct_shapekey_positions(False)
        BlueprintExportHelper.clear_direct_shapekey_position_records()
        if clear_postprocess_caches:
            BlueprintExportHelper.clear_postprocess_caches()

    @staticmethod
    def set_preserve_current_shapekey_mix_for_export(enabled: bool):
        BlueprintExportHelper.preserve_current_shapekey_mix_for_export = bool(enabled)

    @staticmethod
    def should_preserve_current_shapekey_mix_for_export() -> bool:
        return bool(BlueprintExportHelper.preserve_current_shapekey_mix_for_export)

    @staticmethod
    def set_suppress_shapekey_resource_export(enabled: bool):
        BlueprintExportHelper.suppress_shapekey_resource_export = bool(enabled)

    @staticmethod
    def should_suppress_shapekey_resource_export() -> bool:
        return bool(BlueprintExportHelper.suppress_shapekey_resource_export)

    @staticmethod
    def set_capture_direct_shapekey_positions(enabled: bool):
        BlueprintExportHelper.capture_direct_shapekey_positions = bool(enabled)

    @staticmethod
    def should_capture_direct_shapekey_positions() -> bool:
        return bool(BlueprintExportHelper.capture_direct_shapekey_positions)

    @staticmethod
    def clear_direct_shapekey_position_records():
        BlueprintExportHelper.direct_shapekey_position_records = {}

    @staticmethod
    def register_direct_shapekey_position_record(
        object_aliases,
        shapekey_name,
        coords,
        loop_vertex_indices,
        basis_coords=None,
    ):
        if not shapekey_name or coords is None or loop_vertex_indices is None:
            return

        for alias in object_aliases or []:
            if not alias:
                continue
            record = BlueprintExportHelper.direct_shapekey_position_records.setdefault(
                alias,
                {
                    "loop_vertex_indices": loop_vertex_indices,
                    "shape_keys": {},
                },
            )
            record["loop_vertex_indices"] = loop_vertex_indices
            if basis_coords is not None:
                record["basis_coords"] = basis_coords
            record.setdefault("shape_keys", {})[shapekey_name] = coords

    @staticmethod
    def update_direct_shapekey_record_loop_indices(object_aliases, loop_vertex_indices):
        if loop_vertex_indices is None:
            return

        for alias in object_aliases or []:
            record = BlueprintExportHelper.direct_shapekey_position_records.get(alias)
            if record:
                record["loop_vertex_indices"] = loop_vertex_indices

    @staticmethod
    def merge_direct_shapekey_position_records(records):
        for alias, record in (records or {}).items():
            if not alias or not record:
                continue
            target = BlueprintExportHelper.direct_shapekey_position_records.setdefault(
                alias,
                {
                    "loop_vertex_indices": record.get("loop_vertex_indices"),
                    "shape_keys": {},
                },
            )
            if record.get("loop_vertex_indices") is not None:
                target["loop_vertex_indices"] = record.get("loop_vertex_indices")
            if record.get("basis_coords") is not None:
                target["basis_coords"] = record.get("basis_coords")
            target.setdefault("shape_keys", {}).update(record.get("shape_keys", {}) or {})

    @staticmethod
    def get_direct_shapekey_position_records():
        return BlueprintExportHelper.direct_shapekey_position_records

    @staticmethod
    def get_blueprint_enum_items(context=None):
        items = []
        preferred_name = BlueprintExportHelper.get_preferred_blueprint_name(context=context)
        used_numbers = set()

        for tree in BlueprintExportHelper.get_all_blueprint_trees():
            description = "当前默认蓝图" if tree.name == preferred_name else "选择该蓝图进行打开或生成 Mod"
            enum_number = BlueprintExportHelper._stable_blueprint_enum_number(tree.name, used_numbers)
            items.append((tree.name, tree.name, description, 0, enum_number))

        if not items:
            items.append((
                BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER,
                "当前没有蓝图",
                "当前没有可选蓝图，请先打开蓝图界面或执行一键导入",
                0,
                BlueprintExportHelper.BLUEPRINT_NONE_ENUM_NUMBER,
            ))

        return items

    @staticmethod
    def set_runtime_blueprint_tree(tree):
        if BlueprintExportHelper._is_valid_blueprint_tree(tree):
            BlueprintExportHelper.runtime_blueprint_tree_name = tree.name

    @staticmethod
    def _get_blueprint_tree_from_context(context):
        if not context:
            return None

        space_data = getattr(context, "space_data", None)
        if space_data and getattr(space_data, "type", None) == 'NODE_EDITOR':
            node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if BlueprintExportHelper._is_valid_blueprint_tree(node_tree):
                return node_tree

        window_manager = getattr(context, "window_manager", None)
        if not window_manager:
            return None

        for window in window_manager.windows:
            for area in window.screen.areas:
                if area.type != 'NODE_EDITOR':
                    continue
                for space in area.spaces:
                    if space.type != 'NODE_EDITOR':
                        continue
                    node_tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
                    if BlueprintExportHelper._is_valid_blueprint_tree(node_tree):
                        return node_tree

        return None

    @staticmethod
    def get_current_blueprint_tree(context=None):
        tree = BlueprintExportHelper._get_blueprint_tree_from_context(context)
        if BlueprintExportHelper._is_valid_blueprint_tree(tree):
            BlueprintExportHelper.set_runtime_blueprint_tree(tree)
            return tree

        runtime_tree_name = BlueprintExportHelper.runtime_blueprint_tree_name
        if runtime_tree_name:
            tree = bpy.data.node_groups.get(runtime_tree_name)
            if BlueprintExportHelper._is_valid_blueprint_tree(tree):
                return tree

        tree_name = GlobalConfig.get_workspace_name()
        if not tree_name:
            return None

        tree = bpy.data.node_groups.get(tree_name)
        if BlueprintExportHelper._is_valid_blueprint_tree(tree):
            BlueprintExportHelper.set_runtime_blueprint_tree(tree)
            return tree

        return None

    @staticmethod
    def get_selected_blueprint_tree(selected_name="", context=None):
        requested_name = str(selected_name or "").strip()
        if requested_name == BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER:
            return None
        if requested_name:
            return BlueprintExportHelper.get_blueprint_tree_by_name(requested_name)

        preferred_name = BlueprintExportHelper.get_preferred_blueprint_name(
            selected_name=selected_name,
            context=context,
        )
        return BlueprintExportHelper.get_blueprint_tree_by_name(preferred_name)

    @staticmethod
    def _iter_ordered_object_input_sources(node):
        if not node:
            return

        for input_socket in getattr(node, "inputs", []):
            if getattr(input_socket, "bl_idname", "") != 'SSMTSocketObject':
                continue
            if not input_socket.is_linked:
                continue
            for link in input_socket.links:
                source_node = getattr(link, "from_node", None)
                if source_node and source_node != node:
                    yield source_node

    @staticmethod
    def _collect_ordered_start_nodes_from_tree(tree, ordered_nodes, emitted_node_keys, visited_trees):
        if not BlueprintExportHelper._is_valid_blueprint_tree(tree):
            return
        if tree.name in visited_trees:
            return
        visited_trees.add(tree.name)

        output_node = BlueprintExportHelper.get_result_output_node(tree)
        if not output_node:
            return

        visiting_node_keys = set()

        def append_start_node(start_node):
            node_key = _get_node_unique_key(start_node)
            if node_key in emitted_node_keys:
                return
            emitted_node_keys.add(node_key)
            ordered_nodes.append(start_node)

        def walk_node(current_node):
            if not current_node:
                return

            node_key = _get_node_unique_key(current_node)
            if node_key in visiting_node_keys:
                return

            visiting_node_keys.add(node_key)
            try:
                node_type = getattr(current_node, "bl_idname", "")

                if node_type == 'SSMTNode_Blueprint_Nest':
                    if getattr(current_node, "mute", False):
                        return

                    nested_tree_name = str(getattr(current_node, "blueprint_name", "") or "").strip()
                    if not nested_tree_name or nested_tree_name == 'NONE':
                        return

                    nested_tree = bpy.data.node_groups.get(nested_tree_name)
                    BlueprintExportHelper._collect_ordered_start_nodes_from_tree(
                        nested_tree,
                        ordered_nodes=ordered_nodes,
                        emitted_node_keys=emitted_node_keys,
                        visited_trees=visited_trees,
                    )
                    return

                if node_type in {'SSMTNode_Object_Info', 'SSMTNode_MultiFile_Export'}:
                    if not getattr(current_node, "mute", False):
                        append_start_node(current_node)
                    return

                for source_node in BlueprintExportHelper._iter_ordered_object_input_sources(current_node):
                    walk_node(source_node)

                # 对于转接点(Reroute)等使用虚拟插槽(NodeSocketVirtual)的透传节点，
                # 它们没有 SSMTSocketObject 类型的输入，需要遍历所有输入插槽才能继续回溯。
                # 只有当前节点没有任何 SSMTSocketObject 输入时才走此回退路径，
                # 避免与已有 Object 输入的正常节点重复遍历。
                if not any(
                    inp.is_linked and getattr(inp, "bl_idname", "") == 'SSMTSocketObject'
                    for inp in getattr(current_node, "inputs", [])
                ):
                    for input_socket in getattr(current_node, "inputs", []):
                        if input_socket.is_linked:
                            for link in input_socket.links:
                                source_node = getattr(link, "from_node", None)
                                if source_node and source_node != current_node:
                                    walk_node(source_node)
            finally:
                visiting_node_keys.remove(node_key)

        walk_node(output_node)

    @staticmethod
    def collect_connected_start_nodes(tree):
        ordered_nodes = []
        BlueprintExportHelper._collect_ordered_start_nodes_from_tree(
            tree,
            ordered_nodes=ordered_nodes,
            emitted_node_keys=set(),
            visited_trees=set(),
        )
        return ordered_nodes

    @staticmethod
    def collect_connected_object_names(tree) -> list[str]:
        if not tree:
            return []

        # 这里只收集真正连到输出的物体，避免断开的节点或历史副本混入前处理。
        object_names = []
        seen_names = set()

        def append_name(candidate_name: str):
            if not candidate_name or candidate_name in seen_names:
                return
            seen_names.add(candidate_name)
            object_names.append(candidate_name)

        for node in BlueprintExportHelper.collect_connected_start_nodes(tree):
            if node.bl_idname == 'SSMTNode_Object_Info':
                obj_name = ObjectPrefixHelper.build_virtual_object_name_for_node(node, strict=True)
                if not obj_name:
                    obj_name = getattr(node, 'object_name', '')
                append_name(obj_name)
                continue

            if node.bl_idname == 'SSMTNode_MultiFile_Export':
                for item in getattr(node, 'object_list', []):
                    append_name(getattr(item, 'object_name', ''))

        return object_names

    @staticmethod
    def _find_object_by_pointer(object_id: str):
        pointer_value = str(object_id or "").strip()
        if not pointer_value:
            return None

        for obj in bpy.data.objects:
            if obj is None:
                continue
            try:
                if str(obj.as_pointer()) == pointer_value:
                    return obj
            except (AttributeError, ReferenceError):
                continue

        return None

    @staticmethod
    def _resolve_preprocess_object_name(candidate_name: str, object_id: str = "", original_object_name: str = "") -> str:
        for candidate_id in (object_id,):
            obj = BlueprintExportHelper._find_object_by_pointer(candidate_id)
            if obj is not None:
                return obj.name

        for raw_name in (candidate_name, original_object_name):
            clean_name = str(raw_name or "").strip()
            if not clean_name:
                continue

            obj = bpy.data.objects.get(clean_name)
            if obj is not None:
                return obj.name

            try:
                resolved_name = ObjectPrefixHelper.resolve_source_object_name(clean_name)
            except Exception:
                resolved_name = ""
            if resolved_name:
                obj = bpy.data.objects.get(resolved_name)
                if obj is not None:
                    return obj.name

        return str(candidate_name or "").strip()

    @staticmethod
    def iter_preprocess_lookup_names(
        candidate_name: str,
        object_id: str = "",
        original_object_name: str = "",
        virtual_object_name: str = "",
    ) -> list[str]:
        candidate_names: list[str] = []
        seen_names: set[str] = set()

        def append_name(raw_name: str):
            clean_name = str(raw_name or "").strip()
            if not clean_name or clean_name in seen_names:
                return
            seen_names.add(clean_name)
            candidate_names.append(clean_name)

        append_name(
            BlueprintExportHelper._resolve_preprocess_object_name(
                candidate_name,
                object_id=object_id,
                original_object_name=original_object_name,
            )
        )
        append_name(virtual_object_name)
        append_name(candidate_name)
        append_name(original_object_name)

        for raw_name in (candidate_name, original_object_name, virtual_object_name):
            clean_name = str(raw_name or "").strip()
            if not clean_name:
                continue
            try:
                resolved_name = ObjectPrefixHelper.resolve_source_object_name(clean_name)
            except Exception:
                resolved_name = ""
            append_name(resolved_name)

        return candidate_names

    @staticmethod
    def find_preprocess_copy_name(
        original_to_copy_map: dict,
        candidate_name: str,
        object_id: str = "",
        original_object_name: str = "",
        virtual_object_name: str = "",
    ) -> tuple[str, str]:
        for lookup_name in BlueprintExportHelper.iter_preprocess_lookup_names(
            candidate_name,
            object_id=object_id,
            original_object_name=original_object_name,
            virtual_object_name=virtual_object_name,
        ):
            copy_name = str(original_to_copy_map.get(lookup_name, "") or "").strip()
            if copy_name:
                return lookup_name, copy_name

        return "", ""

    @staticmethod
    def collect_connected_preprocess_object_names(tree) -> list[str]:
        if not tree:
            return []

        object_names = []
        seen_names = set()

        def append_name(candidate_name: str):
            clean_name = str(candidate_name or "").strip()
            if not clean_name or clean_name in seen_names:
                return
            seen_names.add(clean_name)
            object_names.append(clean_name)

        for node in BlueprintExportHelper.collect_connected_start_nodes(tree):
            if node.bl_idname == 'SSMTNode_Object_Info':
                append_name(
                    BlueprintExportHelper._resolve_preprocess_object_name(
                        getattr(node, 'object_name', ''),
                        object_id=getattr(node, 'object_id', ''),
                        original_object_name=getattr(node, 'original_object_name', ''),
                    )
                )
                continue

            if node.bl_idname == 'SSMTNode_MultiFile_Export':
                for item in getattr(node, 'object_list', []):
                    append_name(
                        BlueprintExportHelper._resolve_preprocess_object_name(
                            getattr(item, 'object_name', ''),
                            object_id=getattr(item, 'object_id', ''),
                            original_object_name=getattr(item, 'original_object_name', ''),
                        )
                    )

        return object_names

    @staticmethod
    def get_active_mod_panel_nodes(context=None, tree=None):
        current_tree = tree or BlueprintExportHelper.get_current_blueprint_tree(context=context)
        if not current_tree:
            return []

        panel_nodes = BlueprintExportHelper.get_nodes_from_bl_idname(current_tree, 'SSMTNode_ModPanel')
        return [node for node in panel_nodes if not getattr(node, "mute", False)]

    @staticmethod
    def has_mod_panel_node(context=None, tree=None):
        return len(BlueprintExportHelper.get_active_mod_panel_nodes(context=context, tree=tree)) > 0

    @staticmethod
    def is_mod_panel_flow_effect_enabled(context=None, tree=None):
        panel_nodes = BlueprintExportHelper.get_active_mod_panel_nodes(context=context, tree=tree)
        if not panel_nodes:
            return False
        return any(getattr(node, "enable_flow_effect", True) for node in panel_nodes)

    @staticmethod
    def find_node_in_all_blueprints(node_name):
        for node_group in bpy.data.node_groups:
            if node_group.bl_idname == 'SSMTBlueprintTreeType':
                node = node_group.nodes.get(node_name)
                if node:
                    return node
        return None

    @staticmethod
    def get_node_from_bl_idname(tree, node_type:str):
        if not tree:
            return None
        if node_type == BlueprintExportHelper.DEFAULT_RESULT_OUTPUT_NODE_TYPE:
            for result_node_type in BlueprintExportHelper.iter_result_output_node_types():
                for node in tree.nodes:
                    if node.bl_idname == result_node_type:
                        return node
            for node in tree.nodes:
                if str(getattr(node, "bl_idname", "") or "").startswith("SSMTNode_Result_Output"):
                    return node
            return None
        for node in tree.nodes:
            if node.bl_idname == node_type:
                return node
        return None

    @staticmethod
    def get_result_output_node(tree):
        return BlueprintExportHelper.get_node_from_bl_idname(
            tree,
            BlueprintExportHelper.DEFAULT_RESULT_OUTPUT_NODE_TYPE,
        )

    @staticmethod
    def get_nodes_from_bl_idname(tree, node_type:str):
        if not tree:
            return []
        nodes = []
        for node in tree.nodes:
            if node.bl_idname == node_type:
                nodes.append(node)
        return nodes

    @staticmethod
    def get_connected_groups(output_node):
        connected_groups = []
        if not output_node:
            return connected_groups

        for socket in output_node.inputs:
            if socket.is_linked:
                for link in socket.links:
                    source_node = link.from_node
                    if source_node.bl_idname == 'SSMTNode_Object_Group':
                         connected_groups.append(source_node)

        return connected_groups

    @staticmethod
    def get_connected_nodes(current_node):
        connected_groups = []
        if not current_node:
            return connected_groups

        for socket in current_node.inputs:
            if socket.is_linked:
                for link in socket.links:
                    source_node = link.from_node
                    connected_groups.append(source_node)

        return connected_groups

    @staticmethod
    def get_objects_from_group(group_node):
        objects_info = []
        if not group_node:
            return objects_info

        for socket in group_node.inputs:
            if socket.is_linked:
                for link in socket.links:
                    source_node = link.from_node
                    if source_node.bl_idname == 'SSMTNode_Object_Info':
                        info = {
                            "object_name": source_node.object_name,
                            "draw_ib": source_node.draw_ib,
                            "component": source_node.component,
                            "node": source_node
                        }
                        objects_info.append(info)
        return objects_info


    @staticmethod
    def get_current_shapekeyname_mkey_dict(context=None):
        tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)
        if not tree:
            return {}

        shapekey_name_mkey_dict = {}
        visited_blueprints = set()
        key_index = 0

        def collect_shapekey_nodes(current_tree):
            nonlocal key_index

            if current_tree.name in visited_blueprints:
                return
            visited_blueprints.add(current_tree.name)

            shapekey_output_node = None
            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_ShapeKey_Output':
                    shapekey_output_node = node
                    break

            if shapekey_output_node:
                shapekey_nodes = BlueprintExportHelper.get_connected_nodes(shapekey_output_node)

                for shapekey_node in shapekey_nodes:
                    if shapekey_node.mute:
                        continue
                    if shapekey_node.bl_idname != 'SSMTNode_ShapeKey':
                        continue

                    shapekey_name = shapekey_node.shapekey_name
                    key = shapekey_node.key
                    comment = getattr(shapekey_node, 'comment', '')

                    m_key = M_Key()
                    m_key.key_name = "$shapekey" + str(key_index)
                    m_key.initialize_value = 0
                    m_key.initialize_vk_str = key
                    m_key.comment = comment

                    shapekey_name_mkey_dict[shapekey_name] = m_key
                    key_index += 1

            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name and bp_name != 'NONE':
                        nested_tree = bpy.data.node_groups.get(bp_name)
                        if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                            collect_shapekey_nodes(nested_tree)



        collect_shapekey_nodes(tree)

        connected_object_names = BlueprintExportHelper.collect_shapekey_objects(tree)
        for obj_name in connected_object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj or not obj.data:
                continue
            if not hasattr(obj.data, "shape_keys") or not obj.data.shape_keys:
                continue

            for key_block in obj.data.shape_keys.key_blocks:
                if key_block == obj.data.shape_keys.key_blocks[0]:
                    continue
                if key_block.name in shapekey_name_mkey_dict:
                    continue

                m_key = M_Key()
                m_key.key_name = "$shapekey" + str(key_index)
                m_key.initialize_value = 0
                m_key.initialize_vk_str = ""
                m_key.comment = key_block.name

                shapekey_name_mkey_dict[key_block.name] = m_key
                key_index += 1

        return shapekey_name_mkey_dict

    @staticmethod
    def get_datatype_node_info(context=None):
        tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)
        if not tree:
            return None

        visited_blueprints = set()
        datatype_nodes = []

        def collect_datatype_nodes(current_tree):
            if current_tree.name in visited_blueprints:
                return
            visited_blueprints.add(current_tree.name)

            output_node = BlueprintExportHelper.get_result_output_node(current_tree)

            if output_node:
                nodes = BlueprintExportHelper._find_datatype_nodes_connected_to_output(output_node)
                datatype_nodes.extend(nodes)

            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name and bp_name != 'NONE':
                        nested_tree = bpy.data.node_groups.get(bp_name)
                        if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                            collect_datatype_nodes(nested_tree)


        collect_datatype_nodes(tree)

        if not datatype_nodes:
            return None

        node_info_list = []
        for node in datatype_nodes:
            node_info_list.append({
                "draw_ib_match": node.draw_ib_match,
                "tmp_json_path": node.tmp_json_path,
                "loaded_data": getattr(node, 'loaded_data', {}),
                "node": node
            })

        return node_info_list

    @staticmethod
    def _find_datatype_nodes_connected_to_output(node, visited=None):
        if visited is None:
            visited = set()

        node_key = _get_node_unique_key(node)
        if node_key in visited:
            return []

        if node.mute:
            return []

        visited.add(node_key)
        datatype_nodes = []

        if node.bl_idname == 'SSMTNode_DataType':
            datatype_nodes.append(node)

        connected_nodes = BlueprintExportHelper.get_connected_nodes(node)
        for connected_node in connected_nodes:
            datatype_nodes.extend(BlueprintExportHelper._find_datatype_nodes_connected_to_output(connected_node, visited))

        return datatype_nodes

    @staticmethod
    def _collect_postprocess_nodes(tree):
        if not tree:
            return []

        ordered_chain = []
        visited = set()
        visited_trees = set()

        def collect_from_tree(current_tree):
            if current_tree.name in visited_trees:
                return
            visited_trees.add(current_tree.name)

            output_node = BlueprintExportHelper.get_result_output_node(current_tree)
            if output_node:
                follow_forward(output_node)

                found_in_tree = any(
                    n.bl_idname.startswith('SSMTNode_PostProcess_') and n.id_data.name == current_tree.name
                    for n in ordered_chain
                )
                if not found_in_tree:
                    for input_socket in output_node.inputs:
                        if not input_socket.is_linked:
                            continue
                        for link in input_socket.links:
                            source = link.from_node
                            if source.bl_idname.startswith('SSMTNode_PostProcess_'):
                                follow_backward(source)

            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name and bp_name != 'NONE':
                        nested_tree = bpy.data.node_groups.get(bp_name)
                        if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                            collect_from_tree(nested_tree)

        def follow_forward(node):
            node_key = _get_node_unique_key(node)
            if node_key in visited:
                return
            visited.add(node_key)

            if node.bl_idname.startswith('SSMTNode_PostProcess_') and node not in ordered_chain:
                if not node.mute:
                    ordered_chain.append(node)

            for output_socket in node.outputs:
                if node.bl_idname != 'NodeReroute' and getattr(output_socket, 'bl_idname', '') != 'SSMTSocketPostProcess':
                    continue
                if not output_socket.is_linked:
                    continue
                for link in output_socket.links:
                    target = link.to_node
                    if target.bl_idname == 'NodeReroute':
                        follow_forward(target)
                    elif target.bl_idname.startswith('SSMTNode_PostProcess_'):
                        follow_forward(target)

        def follow_backward(node):
            node_key = _get_node_unique_key(node)
            if node_key in visited:
                return
            visited.add(node_key)

            for input_socket in node.inputs:
                if node.bl_idname != 'NodeReroute' and getattr(input_socket, 'bl_idname', '') != 'SSMTSocketPostProcess':
                    continue
                if not input_socket.is_linked:
                    continue
                for link in input_socket.links:
                    source = link.from_node
                    if source.bl_idname == 'NodeReroute':
                        follow_backward(source)
                    elif source.bl_idname.startswith('SSMTNode_PostProcess_'):
                        follow_backward(source)

            if node.bl_idname.startswith('SSMTNode_PostProcess_') and node not in ordered_chain:
                if not node.mute:
                    ordered_chain.append(node)

        collect_from_tree(tree)

        return ordered_chain

    @staticmethod
    def _is_node_connected_to_output(tree, node) -> bool:
        """检查节点是否连接到输出节点（包括通过后处理链连接）"""
        if not tree or not node:
            return False
        
        output_node = BlueprintExportHelper.get_result_output_node(tree)
        if not output_node:
            return False
        
        visited = set()
        
        def check_reverse_connection(current_node, target_node):
            node_key = _get_node_unique_key(current_node)
            if node_key in visited:
                return False
            visited.add(node_key)
            
            if current_node == target_node:
                return True
            
            for input_socket in current_node.inputs:
                if not input_socket.is_linked:
                    continue
                for link in input_socket.links:
                    if check_reverse_connection(link.from_node, target_node):
                        return True
            
            return False
        
        return check_reverse_connection(output_node, node)

    @staticmethod
    def execute_postprocess_nodes(mod_export_path):
        tree = BlueprintExportHelper.get_current_blueprint_tree()
        if not tree:
            return

        postprocess_nodes = BlueprintExportHelper._collect_postprocess_nodes(tree)

        if not postprocess_nodes:
            return

        for node in postprocess_nodes:
            if not hasattr(node, 'execute_postprocess') or not callable(node.execute_postprocess):
                print(f"Warning: Postprocess node type '{node.bl_idname}' is not registered, skipping")
                continue

            try:
                node.execute_postprocess(mod_export_path)
            except Exception as e:
                print(f"Error executing postprocess node '{node.name}': {e}")
                import traceback
                traceback.print_exc()

    @staticmethod
    def clear_postprocess_caches():
        # 工作空间格式缓存（common.submesh_metadata 模块级，含 None 负缓存）：
        # 每次导出前清理，避免同会话内「重新导入工作空间 / 编辑数据类型覆盖节点」
        # 后旧的 Position/Texcoord 布局继续生效。无蓝图树时也必须执行，故置于
        # 下方 tree 提前返回之前。
        try:
            from ..common.submesh_metadata import clear_workspace_game_type_cache
            clear_workspace_game_type_cache()
        except Exception:
            pass

        tree = BlueprintExportHelper.get_current_blueprint_tree()
        if not tree:
            return

        postprocess_nodes = BlueprintExportHelper._collect_postprocess_nodes(tree)
        cleared_types = set()

        for node in postprocess_nodes:
            node_class = type(node)
            if node_class in cleared_types:
                continue
            cleared_types.add(node_class)

            clear_cache = getattr(node_class, 'clear_cache', None)
            if clear_cache and callable(clear_cache):
                try:
                    clear_cache()
                except Exception as e:
                    print(f"Warning: Failed to clear cache for {node.bl_idname}: {e}")


    @staticmethod
    def collect_multi_file_export_nodes(tree):
        BlueprintExportHelper.multi_file_export_nodes = []
        if not tree:
            return []

        visited_trees = set()

        def collect_from_tree(current_tree):
            if current_tree.name in visited_trees:
                return
            visited_trees.add(current_tree.name)

            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_MultiFile_Export' and not node.mute:
                    if BlueprintExportHelper._is_node_connected_to_output(current_tree, node):
                        BlueprintExportHelper.multi_file_export_nodes.append(node)
                        print(f"[MultiFileExport] 节点 '{node.name}' 已连接到输出")
                    else:
                        print(f"[MultiFileExport] 节点 '{node.name}' 未连接到输出，跳过")

                elif node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    if not BlueprintExportHelper._is_node_connected_to_output(current_tree, node):
                        print(f"[MultiFileExport] 嵌套蓝图节点 '{node.name}' 未连接到输出，跳过")
                        continue
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name and bp_name != 'NONE':
                        nested_tree = bpy.data.node_groups.get(bp_name)
                        if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                            collect_from_tree(nested_tree)

        collect_from_tree(tree)

        return BlueprintExportHelper.multi_file_export_nodes

    @staticmethod
    def calculate_max_export_count(tree) -> int:
        multi_file_nodes = BlueprintExportHelper.collect_multi_file_export_nodes(tree)
        
        if not multi_file_nodes:
            BlueprintExportHelper.max_export_count = 1
            return 1
        
        max_count = 0
        node_counts = []
        for node in multi_file_nodes:
            obj_list = getattr(node, 'object_list', [])
            list_count = len(obj_list)
            node_counts.append(f"'{node.name}':{list_count}")
            if list_count > max_count:
                max_count = list_count
        
        max_count = min(max_count, BlueprintExportHelper.MAX_EXPORT_COUNT_LIMIT)
        BlueprintExportHelper.max_export_count = max_count
        
        print(f"[MultiFileExport] 计算导出次数: 节点列表 [{', '.join(node_counts)}] → 最大值 {max_count}")
        
        return max_count

    @staticmethod
    def get_multi_file_export_object_info(export_index: int) -> dict:
        result = {}
        
        for node in BlueprintExportHelper.multi_file_export_nodes:
            node_name = node.name
            obj_info = node.get_current_object_info(export_index)
            if obj_info:
                result[node_name] = obj_info
                print(f"[MultiFileExport] 节点 '{node_name}' 索引 {export_index} → 物体 '{obj_info.get('object_name', 'N/A')}'")
        
        return result

    @staticmethod
    def set_current_export_index(index: int):
        BlueprintExportHelper.current_export_index = index
        print(f"[MultiFileExport] 设置当前导出索引: {index}")

    @staticmethod
    def set_current_buffer_folder_name(folder_name: str):
        BlueprintExportHelper.current_buffer_folder_name = folder_name
        print(f"[MultiFileExport] 设置 Buffer 文件夹名称: {folder_name}")

    @staticmethod
    def get_current_buffer_folder_name() -> str:
        return BlueprintExportHelper.current_buffer_folder_name

    @staticmethod
    def get_all_objects_from_multi_file_nodes() -> list:
        all_objects = []
        
        for node in BlueprintExportHelper.multi_file_export_nodes:
            for item in node.object_list:
                obj_name = getattr(item, 'object_name', '')
                if obj_name:
                    if obj_name.endswith('_copy'):
                        obj_name = obj_name[:-5]
                    if obj_name and obj_name not in all_objects:
                        all_objects.append(obj_name)
        
        if all_objects:
            print(f"[MultiFileExport] 收集所有物体: {len(all_objects)} 个")
        
        return all_objects

    @staticmethod
    def has_multi_file_export_nodes() -> bool:
        return len(BlueprintExportHelper.multi_file_export_nodes) > 0

    shapekey_postprocess_nodes = []
    shapekey_objects = []
    max_shapekey_slot_count = 0
    configured_shapekey_slot_count = 0

    @staticmethod
    def has_shapekey_postprocess_node(tree) -> bool:
        if not tree:
            return False

        connected_postprocess_nodes = BlueprintExportHelper._collect_postprocess_nodes(tree)

        for node in connected_postprocess_nodes:
            if node.bl_idname == 'SSMTNode_PostProcess_ShapeKey':
                print(f"[ShapeKeyExport] 检测到形态键配置节点 '{node.name}' 已连接到输出")
                return True

        visited_trees = set()

        def check_unconnected(current_tree):
            if current_tree.name in visited_trees:
                return
            visited_trees.add(current_tree.name)

            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_PostProcess_ShapeKey' and not node.mute:
                    print(f"[ShapeKeyExport] 形态键配置节点 '{node.name}' 未连接到输出，跳过")
                elif node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name and bp_name != 'NONE':
                        nested_tree = bpy.data.node_groups.get(bp_name)
                        if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                            check_unconnected(nested_tree)

        check_unconnected(tree)

        return False

    @staticmethod
    def collect_shapekey_postprocess_nodes(tree):
        BlueprintExportHelper.shapekey_postprocess_nodes = []
        if not tree:
            return []

        connected_postprocess_nodes = BlueprintExportHelper._collect_postprocess_nodes(tree)

        for node in connected_postprocess_nodes:
            if node.bl_idname == 'SSMTNode_PostProcess_ShapeKey':
                BlueprintExportHelper.shapekey_postprocess_nodes.append(node)

        return BlueprintExportHelper.shapekey_postprocess_nodes

    @staticmethod
    def _resolve_shapekey_source_object_name(candidate_name: str) -> str:
        if not candidate_name:
            return ""

        candidate_names = []
        seen_names = set()

        def append_name(name: str):
            if not name or name in seen_names:
                return
            seen_names.add(name)
            candidate_names.append(name)

        append_name(candidate_name)

        try:
            resolved_name = ObjectPrefixHelper.resolve_source_object_name(candidate_name)
        except Exception:
            resolved_name = ""
        append_name(resolved_name)

        if candidate_name.endswith("_copy"):
            append_name(candidate_name[:-5])
        if resolved_name.endswith("_copy"):
            append_name(resolved_name[:-5])

        for resolved_candidate_name in candidate_names:
            obj = bpy.data.objects.get(resolved_candidate_name)
            if obj is not None:
                return obj.name

        return resolved_name or candidate_name

    @staticmethod
    def collect_shapekey_objects(tree) -> list:
        BlueprintExportHelper.shapekey_objects = []
        resolved_count = 0
        for candidate_name in BlueprintExportHelper.collect_connected_object_names(tree):
            resolved_object = BlueprintExportHelper._resolve_shapekey_object_in_scene(candidate_name)
            resolved_name = (
                resolved_object.name
                if resolved_object is not None
                else BlueprintExportHelper._resolve_shapekey_source_object_name(candidate_name)
            )
            if not resolved_name or resolved_name in BlueprintExportHelper.shapekey_objects:
                continue
            BlueprintExportHelper.shapekey_objects.append(resolved_name)
            if resolved_name != candidate_name:
                resolved_count += 1
        print(
            f"[ShapeKeyExport] collected {len(BlueprintExportHelper.shapekey_objects)} shape key source objects"
            f" (resolved {resolved_count} virtual/export names)"
        )
        return BlueprintExportHelper.shapekey_objects

    @staticmethod
    def calculate_configured_shapekey_slot_count(tree) -> int:
        if not tree:
            BlueprintExportHelper.configured_shapekey_slot_count = 0
            return 0

        visited_trees = set()
        max_slot_index = 0

        def collect_from_tree(current_tree):
            nonlocal max_slot_index
            if not BlueprintExportHelper._is_valid_blueprint_tree(current_tree):
                return
            if current_tree.name in visited_trees:
                return
            visited_trees.add(current_tree.name)

            for node in current_tree.nodes:
                if getattr(node, "mute", False):
                    continue

                if node.bl_idname == 'SSMTNode_ShapeKey_Output':
                    linked_slot_indices = [
                        socket_index
                        for socket_index, socket in enumerate(getattr(node, "inputs", []), start=1)
                        if getattr(socket, "is_linked", False)
                    ]
                    if linked_slot_indices:
                        max_slot_index = max(max_slot_index, max(linked_slot_indices))
                    continue

                if node.bl_idname == 'SSMTNode_Blueprint_Nest':
                    nested_tree_name = getattr(node, 'blueprint_name', '')
                    if not nested_tree_name or nested_tree_name == 'NONE':
                        continue
                    nested_tree = bpy.data.node_groups.get(nested_tree_name)
                    collect_from_tree(nested_tree)

        collect_from_tree(tree)
        BlueprintExportHelper.configured_shapekey_slot_count = max_slot_index
        return max_slot_index

    @staticmethod
    def calculate_max_shapekey_slot_count(tree) -> int:
        objects = BlueprintExportHelper.collect_shapekey_objects(tree)
        configured_slot_count = BlueprintExportHelper.calculate_configured_shapekey_slot_count(tree)

        max_slot = 0
        slot_info = []

        for obj_name in objects:
            obj = bpy.data.objects.get(obj_name)
            if not obj or not obj.data:
                continue
            slot_count = len(BlueprintExportHelper.get_exportable_shape_key_infos(
                obj,
                slot_limit=configured_slot_count or None,
            ))
            if slot_count > 0:
                slot_info.append(f"'{obj_name}':{slot_count}")
                if slot_count > max_slot:
                    max_slot = slot_count

        effective_slot_count = configured_slot_count or max_slot
        BlueprintExportHelper.max_shapekey_slot_count = effective_slot_count

        if slot_info:
            print(
                f"[ShapeKeyExport] 形态键槽位统计: [{', '.join(slot_info)}] "
                f"→ 对象最大值 {max_slot}, 配置槽位数 {configured_slot_count}, 生效值 {effective_slot_count}"
            )
        else:
            print(f"[ShapeKeyExport] 未检测到形态键")

        return effective_slot_count

    @staticmethod
    def _resolve_shapekey_object_in_scene(obj_name: str):
        """在场景中查找形态键物体，支持带 _copy/_chainN/_dupN 后缀的反查。"""
        if not obj_name:
            return None
        obj = bpy.data.objects.get(obj_name)
        if obj:
            return obj
        resolved = BlueprintExportHelper._resolve_shapekey_source_object_name(obj_name)
        if resolved and resolved != obj_name:
            obj = bpy.data.objects.get(resolved)
            if obj:
                return obj
        import re as _re
        for initial_name in dict.fromkeys((obj_name, resolved)):
            current = initial_name
            for _ in range(20):
                changed = False
                for pattern in (r'_copy$', r'_chain\d+$', r'_dup\d+$'):
                    new_name = _re.sub(pattern, '', current)
                    if new_name != current:
                        obj = bpy.data.objects.get(new_name)
                        if obj:
                            return obj
                        current = new_name
                        changed = True
                        break
                if not changed:
                    break
        return None

    @staticmethod
    def set_all_shapekey_values(value: int, slot_index: int = None):
        total_objects = len(BlueprintExportHelper.shapekey_objects)
        objects_with_keys = 0
        missing_objects = 0
        objects_without_keys = 0
        changed_keys = 0
        failed_keys = 0

        for obj_name in BlueprintExportHelper.shapekey_objects:
            obj = BlueprintExportHelper._resolve_shapekey_object_in_scene(obj_name)
            if not obj or not obj.data:
                missing_objects += 1
                continue
            if not hasattr(obj.data, 'shape_keys') or not obj.data.shape_keys:
                objects_without_keys += 1
                continue
            
            key_blocks = obj.data.shape_keys.key_blocks
            objects_with_keys += 1
            target_key_block = (
                BlueprintExportHelper.get_exportable_shape_key_by_slot(obj, slot_index)
                if slot_index is not None
                else None
            )
            
            for key_index, kb in enumerate(key_blocks):
                if key_index == 0:
                    continue
                
                if slot_index is not None:
                    if kb == target_key_block:
                        try:
                            kb.value = 1.0
                            changed_keys += 1
                        except Exception:
                            failed_keys += 1
                    else:
                        try:
                            kb.value = 0.0
                            changed_keys += 1
                        except Exception:
                            failed_keys += 1
                else:
                    try:
                        kb.value = float(value)
                        changed_keys += 1
                    except Exception:
                        failed_keys += 1
        
        if slot_index is not None:
            print(
                f"[ShapeKeyExport] 设置槽位 {slot_index} 形态键=1，其他=0 "
                f"(物体 {objects_with_keys}/{total_objects}, 键 {changed_keys}, "
                f"缺失 {missing_objects}, 无形态键 {objects_without_keys}, 失败 {failed_keys})"
            )
        else:
            print(
                f"[ShapeKeyExport] 设置所有形态键值={value} "
                f"(物体 {objects_with_keys}/{total_objects}, 键 {changed_keys}, "
                f"缺失 {missing_objects}, 无形态键 {objects_without_keys}, 失败 {failed_keys})"
            )

    @staticmethod
    def generate_shapekey_classification_report(blueprint_model=None):
        from collections import defaultdict
        import re
        
        def extract_original_name(name):
            if not name:
                return name
            patterns = [
                r'_vgtest_unassigned_copy$',
                r'_vgtest_copy$',
                r'_copy$',
                r'_chain\d+$',
                r'_dup\d+$',
                r'_chain\d+_vgtest_unassigned_copy$',
                r'_chain\d+_vgtest_copy$',
                r'_chain\d+_copy$',
                r'_dup\d+_vgtest_unassigned_copy$',
                r'_dup\d+_vgtest_copy$',
                r'_dup\d+_copy$',
                r'_chain\d+_dup\d+$',
                r'_chain\d+_dup\d+_vgtest_unassigned_copy$',
                r'_chain\d+_dup\d+_vgtest_copy$',
                r'_chain\d+_dup\d+_copy$',
            ]
            result = name
            for pattern in patterns:
                result = re.sub(pattern, '', result)
            return result

        def iter_name_variants(name):
            if not name:
                return

            seen = set()
            candidate_names = [name, extract_original_name(name)]
            if name.endswith("_copy"):
                candidate_names.append(name[:-5])

            for candidate_name in candidate_names:
                if candidate_name and candidate_name not in seen:
                    seen.add(candidate_name)
                    yield candidate_name

        def canonical_name(name):
            if not name:
                return ""
            return extract_original_name(name)

        def iter_chain_aliases(chain):
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
                for candidate_name in iter_name_variants(raw_name):
                    if candidate_name and candidate_name not in seen:
                        seen.add(candidate_name)
                        yield candidate_name

        def get_shapekey_info(obj_name):
            obj = bpy.data.objects.get(obj_name)
            if not obj or not obj.data:
                return None
            # Export participation comes from the blueprint chain, not viewport visibility.
            slot_limit = BlueprintExportHelper.max_shapekey_slot_count or None
            shapekey_info = [
                (slot_index, shape_key_name)
                for slot_index, shape_key_name, _key_block
                in BlueprintExportHelper.get_exportable_shape_key_infos(obj, slot_limit=slot_limit)
            ]
            return shapekey_info or None

        def resolve_chain_output_names(chain):
            output_names = []
            seen = set()

            preferred_name = ""
            get_export_object_name = getattr(chain, "get_export_object_name", None)
            if callable(get_export_object_name):
                try:
                    preferred_name = get_export_object_name() or ""
                except Exception:
                    preferred_name = ""

            for candidate_name in (
                preferred_name,
                getattr(chain, "virtual_object_name", "") or "",
                getattr(chain, "export_object_name_override", "") or "",
                getattr(chain, "object_name", "") or "",
            ):
                candidate_name = canonical_name(candidate_name)
                if not candidate_name or candidate_name in seen:
                    continue
                seen.add(candidate_name)
                output_names.append(candidate_name)

            if not output_names:
                for candidate_name in iter_chain_aliases(chain):
                    candidate_name = canonical_name(candidate_name)
                    if candidate_name and candidate_name not in seen:
                        output_names.append(candidate_name)
                        break

            return output_names

        def resolve_chain_shapekey_source(chain):
            source_candidates = []

            get_export_object_name = getattr(chain, "get_export_object_name", None)
            if callable(get_export_object_name):
                try:
                    source_candidates.append(get_export_object_name() or "")
                except Exception:
                    pass

            source_candidates.extend([
                getattr(chain, "object_name", "") or "",
                getattr(chain, "virtual_object_name", "") or "",
                getattr(chain, "original_object_name", "") or "",
                getattr(chain, "export_object_name_override", "") or "",
            ])

            for rename_record in reversed(getattr(chain, "rename_history", []) or []):
                source_candidates.append(rename_record.get("new_name", "") or "")
                source_candidates.append(rename_record.get("old_name", "") or "")

            seen = set()
            for candidate_name in source_candidates:
                for variant_name in iter_name_variants(candidate_name):
                    variant_name = canonical_name(variant_name)
                    if not variant_name or variant_name in seen:
                        continue
                    seen.add(variant_name)
                    shapekey_info = get_shapekey_info(variant_name)
                    if shapekey_info:
                        return variant_name, shapekey_info

            return "", None

        if blueprint_model:
            classification_data = defaultdict(lambda: defaultdict(list))
            output_groups = defaultdict(set)
            output_group_shapekeys = {}

            valid_chains = [
                chain
                for chain in blueprint_model.processing_chains
                if chain.is_valid and chain.reached_output
            ]

            print(f"[ShapeKeyExport] 开始生成分类报告，处理链数: {len(blueprint_model.processing_chains)}")
            print(f"[ShapeKeyExport] 有效输出链路数: {len(valid_chains)}")

            for chain in valid_chains:
                output_names = resolve_chain_output_names(chain)
                if not output_names:
                    continue

                source_name, shapekey_info = resolve_chain_shapekey_source(chain)
                if not shapekey_info:
                    continue

                output_group_shapekeys[source_name] = shapekey_info
                output_groups[source_name].update(output_names)

                for slot_index, shape_key_name in shapekey_info:
                    for output_name in output_names:
                        classification_data[slot_index][shape_key_name].append(output_name)

            if not classification_data:
                print("[ShapeKeyExport] 未找到任何形态键，跳过分类报告生成")
                return False

            import time
            output_lines = ["# 自动化形态键导出 - 分类报告", time.ctime(), "=" * 40, ""]

            if output_groups:
                output_lines.append("# 原始物体与衍生物体映射:")
                for source_name in sorted(output_groups.keys()):
                    derived_sorted = sorted(output_groups.get(source_name, set()))
                    if derived_sorted:
                        output_lines.append(f"#   {source_name} -> {', '.join(derived_sorted)}")
                output_lines.append("")

            sorted_slots = sorted(classification_data.keys())
            for slot in sorted_slots:
                output_lines.append(f"槽位 {slot}:")
                sk_data = classification_data[slot]
                sorted_sk_names = sorted(sk_data.keys())

                for sk_name in sorted_sk_names:
                    output_lines.append(f"  - 名称: {sk_name}")
                    object_list = sk_data[sk_name]
                    for obj_name in sorted(set(object_list)):
                        output_lines.append(f"    - 物体: {obj_name}")
                output_lines.append("")

            final_text = "\n".join(output_lines)
            text_block_name = "Shape_Key_Classification"
            if text_block_name in bpy.data.texts:
                txt = bpy.data.texts[text_block_name]
                txt.clear()
            else:
                txt = bpy.data.texts.new(name=text_block_name)
            txt.write(final_text)

            print(f"[ShapeKeyExport] 形态键分类报告已生成: '{text_block_name}'")
            print(f"[ShapeKeyExport]   - 原始物体数: {len(output_group_shapekeys)}")
            print(f"[ShapeKeyExport]   - 衍生物体映射数: {len(output_groups)}")
            return True
        
        classification_data = defaultdict(lambda: defaultdict(list))
        original_to_derived = defaultdict(set)
        original_to_shapekeys = {}
        
        if blueprint_model:
            print(f"[ShapeKeyExport] 开始生成分类报告，处理链数: {len(blueprint_model.processing_chains)}")
            
            for chain in blueprint_model.processing_chains:
                if not chain.is_valid or not chain.reached_output:
                    continue
                
                chain_aliases = list(iter_chain_aliases(chain))
                if not chain_aliases:
                    continue

                current_obj_name = chain.object_name
                original_obj_name = chain.original_object_name or chain.object_name
                true_original_name = ""

                source_candidates = [
                    original_obj_name,
                    current_obj_name,
                    getattr(chain, "virtual_object_name", "") or "",
                    getattr(chain, "export_object_name_override", "") or "",
                ]
                for rename_record in getattr(chain, "rename_history", []) or []:
                    source_candidates.append(rename_record.get("old_name", "") or "")
                    source_candidates.append(rename_record.get("new_name", "") or "")

                get_export_object_name = getattr(chain, "get_export_object_name", None)
                if callable(get_export_object_name):
                    try:
                        source_candidates.append(get_export_object_name() or "")
                    except Exception:
                        pass

                for candidate_name in source_candidates:
                    for variant_name in iter_name_variants(candidate_name):
                        if get_shapekey_info(variant_name):
                            true_original_name = variant_name
                            break
                    if true_original_name:
                        break

                if not true_original_name:
                    true_original_name = extract_original_name(original_obj_name) or original_obj_name or current_obj_name
                true_original_name = canonical_name(true_original_name)

                derived_names = []
                for candidate_name in (
                    current_obj_name,
                    original_obj_name,
                    getattr(chain, "virtual_object_name", "") or "",
                    getattr(chain, "export_object_name_override", "") or "",
                ):
                    canonical_candidate = canonical_name(candidate_name)
                    if canonical_candidate and canonical_candidate not in derived_names:
                        derived_names.append(canonical_candidate)

                original_to_derived[true_original_name].update(derived_names)
            
            print(f"[ShapeKeyExport] 收集到 {len(original_to_derived)} 个原始物体组")
            for orig_name, derived_set in original_to_derived.items():
                print(f"[ShapeKeyExport]   '{orig_name}' -> {sorted(derived_set)}")
            
            for original_obj_name in original_to_derived:
                obj = bpy.data.objects.get(original_obj_name)
                obj_status = "存在" if obj else "不存在"
                hide_status = f"hide_get={obj.hide_get()}" if obj else ""
                shapekey_status = ""
                if obj and obj.data:
                    if hasattr(obj.data, 'shape_keys') and obj.data.shape_keys:
                        shapekey_status = f"形态键数={len(obj.data.shape_keys.key_blocks)}"
                    else:
                        shapekey_status = "无形态键"
                print(f"[ShapeKeyExport] 检查原始物体 '{original_obj_name}': {obj_status}, {hide_status}, {shapekey_status}")
                
                shapekey_info = get_shapekey_info(original_obj_name)
                if shapekey_info:
                    original_to_shapekeys[original_obj_name] = shapekey_info
                    print(f"[ShapeKeyExport]   从原始物体获取形态键: {shapekey_info}")
                    continue
                
                for derived_name in original_to_derived[original_obj_name]:
                    if derived_name == original_obj_name:
                        continue
                    shapekey_info = get_shapekey_info(derived_name)
                    if shapekey_info:
                        original_to_shapekeys[original_obj_name] = shapekey_info
                        print(f"[ShapeKeyExport] 从衍生物体 '{derived_name}' 获取形态键信息")
                        break
            
            for original_obj_name, shapekey_info in original_to_shapekeys.items():
                derived_objects = original_to_derived.get(original_obj_name, set())
                
                for slot_index, shape_key_name in shapekey_info:
                    for derived_obj_name in derived_objects:
                        classification_data[slot_index][shape_key_name].append(derived_obj_name)
        else:
            for obj_name in BlueprintExportHelper.shapekey_objects:
                obj = bpy.data.objects.get(obj_name)
                if not obj or not obj.data:
                    continue
                slot_limit = BlueprintExportHelper.max_shapekey_slot_count or None
                for slot_index, shape_key_name, _key_block in BlueprintExportHelper.get_exportable_shape_key_infos(obj, slot_limit=slot_limit):
                    classification_data[slot_index][shape_key_name].append(obj_name)
        
        if not classification_data:
            print("[ShapeKeyExport] 未找到任何形态键，跳过分类报告生成")
            return False
        
        import time
        output_lines = ["# 自动化形态键导出 - 分类报告", time.ctime(), "=" * 40, ""]
        
        if original_to_derived:
            output_lines.append("# 原始物体与衍生物体映射:")
            for orig_name in sorted(original_to_derived.keys()):
                derived = original_to_derived.get(orig_name, set())
                if len(derived) > 1 or (orig_name in derived and len(derived) == 1):
                    derived_sorted = sorted(derived)
                    output_lines.append(f"#   {orig_name} -> {', '.join(derived_sorted)}")
            output_lines.append("")
        
        sorted_slots = sorted(classification_data.keys())
        
        for slot in sorted_slots:
            output_lines.append(f"槽位 {slot}:")
            sk_data = classification_data[slot]
            sorted_sk_names = sorted(sk_data.keys())
            
            for sk_name in sorted_sk_names:
                output_lines.append(f"  - 名称: {sk_name}")
                object_list = sk_data[sk_name]
                for obj_name in sorted(set(object_list)):
                    output_lines.append(f"    - 物体: {obj_name}")
            output_lines.append("")
        
        final_text = "\n".join(output_lines)
        text_block_name = "Shape_Key_Classification"
        if text_block_name in bpy.data.texts:
            txt = bpy.data.texts[text_block_name]
            txt.clear()
        else:
            txt = bpy.data.texts.new(name=text_block_name)
        txt.write(final_text)
        
        print(f"[ShapeKeyExport] 形态键分类报告已生成: '{text_block_name}'")
        print(f"[ShapeKeyExport]   - 原始物体数: {len(original_to_shapekeys)}")
        print(f"[ShapeKeyExport]   - 衍生物体映射数: {len(original_to_derived)}")
        return True


def _patched_generate_shapekey_classification_report(blueprint_model=None):
    from collections import defaultdict
    import re
    import time

    def extract_original_name(name):
        if not name:
            return name
        patterns = [
            r'_vgtest_unassigned_copy$',
            r'_vgtest_copy$',
            r'_copy$',
            r'_chain\d+$',
            r'_dup\d+$',
            r'_chain\d+_vgtest_unassigned_copy$',
            r'_chain\d+_vgtest_copy$',
            r'_chain\d+_copy$',
            r'_dup\d+_vgtest_unassigned_copy$',
            r'_dup\d+_vgtest_copy$',
            r'_dup\d+_copy$',
            r'_chain\d+_dup\d+$',
            r'_chain\d+_dup\d+_vgtest_unassigned_copy$',
            r'_chain\d+_dup\d+_vgtest_copy$',
            r'_chain\d+_dup\d+_copy$',
        ]
        result = name
        for pattern in patterns:
            result = re.sub(pattern, '', result)
        return result

    def iter_name_variants(name):
        if not name:
            return

        seen = set()
        candidate_names = [name, extract_original_name(name)]
        if name.endswith("_copy"):
            candidate_names.append(name[:-5])

        try:
            resolved_name = ObjectPrefixHelper.resolve_source_object_name(name)
        except Exception:
            resolved_name = ""
        if resolved_name:
            candidate_names.append(resolved_name)

        for candidate_name in candidate_names:
            clean_name = str(candidate_name or "").strip()
            if clean_name and clean_name not in seen:
                seen.add(clean_name)
                yield clean_name

    def append_unique(target_list, seen_names, candidate_name):
        clean_name = str(candidate_name or "").strip()
        if not clean_name or clean_name in seen_names:
            return
        seen_names.add(clean_name)
        target_list.append(clean_name)

    def iter_chain_aliases(chain):
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
            for candidate_name in iter_name_variants(raw_name):
                if candidate_name and candidate_name not in seen:
                    seen.add(candidate_name)
                    yield candidate_name

    def get_shapekey_info(obj_name):
        obj = bpy.data.objects.get(obj_name)
        if not obj or not obj.data:
            return None
        slot_limit = BlueprintExportHelper.max_shapekey_slot_count or None
        shapekey_info = [
            (slot_index, shape_key_name)
            for slot_index, shape_key_name, _key_block
            in BlueprintExportHelper.get_exportable_shape_key_infos(obj, slot_limit=slot_limit)
        ]
        return shapekey_info or None

    def resolve_chain_output_names(chain):
        output_names = []
        seen = set()

        preferred_name = ""
        get_export_object_name = getattr(chain, "get_export_object_name", None)
        if callable(get_export_object_name):
            try:
                preferred_name = get_export_object_name() or ""
            except Exception:
                preferred_name = ""

        for candidate_name in (
            preferred_name,
            getattr(chain, "virtual_object_name", "") or "",
            getattr(chain, "export_object_name_override", "") or "",
            getattr(chain, "object_name", "") or "",
        ):
            append_unique(output_names, seen, candidate_name)

        if not output_names:
            for candidate_name in iter_chain_aliases(chain):
                append_unique(output_names, seen, candidate_name)
                if output_names:
                    break

        return output_names

    def resolve_chain_shapekey_source(chain):
        source_candidates = []

        get_export_object_name = getattr(chain, "get_export_object_name", None)
        if callable(get_export_object_name):
            try:
                source_candidates.append(get_export_object_name() or "")
            except Exception:
                pass

        source_candidates.extend([
            getattr(chain, "object_name", "") or "",
            getattr(chain, "virtual_object_name", "") or "",
            getattr(chain, "original_object_name", "") or "",
            getattr(chain, "export_object_name_override", "") or "",
        ])

        for rename_record in reversed(getattr(chain, "rename_history", []) or []):
            source_candidates.append(rename_record.get("new_name", "") or "")
            source_candidates.append(rename_record.get("old_name", "") or "")

        seen = set()
        for candidate_name in source_candidates:
            for variant_name in iter_name_variants(candidate_name):
                if not variant_name or variant_name in seen:
                    continue
                seen.add(variant_name)
                shapekey_info = get_shapekey_info(variant_name)
                if shapekey_info:
                    return variant_name, shapekey_info

        return "", None

    classification_data = defaultdict(lambda: defaultdict(list))
    output_groups = defaultdict(set)
    output_group_shapekeys = {}

    if blueprint_model:
        processing_chains = list(getattr(blueprint_model, "processing_chains", []) or [])
        valid_chains = [
            chain
            for chain in processing_chains
            if getattr(chain, "is_valid", False) and getattr(chain, "reached_output", False)
        ]

        print(f"[ShapeKeyExport] 开始生成分类报告，处理链数: {len(processing_chains)}")
        print(f"[ShapeKeyExport] 有效输出链路数: {len(valid_chains)}")

        for chain in valid_chains:
            output_names = resolve_chain_output_names(chain)
            if not output_names:
                print(f"[ShapeKeyExport] 跳过链路: 未解析到输出名 object='{getattr(chain, 'object_name', '')}'")
                continue

            source_name, shapekey_info = resolve_chain_shapekey_source(chain)
            if not shapekey_info:
                print(
                    f"[ShapeKeyExport] 跳过链路: 未找到 ShapeKey 源 object='{getattr(chain, 'object_name', '')}', "
                    f"original='{getattr(chain, 'original_object_name', '')}', "
                    f"virtual='{getattr(chain, 'virtual_object_name', '')}'"
                )
                continue

            output_group_shapekeys[source_name] = shapekey_info
            output_groups[source_name].update(output_names)
            print(
                f"[ShapeKeyExport] 源对象 '{source_name}' -> 输出对象 {output_names}, "
                f"ShapeKeys={shapekey_info}"
            )

            for slot_index, shape_key_name in shapekey_info:
                for output_name in output_names:
                    classification_data[slot_index][shape_key_name].append(output_name)
    else:
        for obj_name in BlueprintExportHelper.shapekey_objects:
            shapekey_info = get_shapekey_info(obj_name)
            if not shapekey_info:
                continue
            output_groups[obj_name].add(obj_name)
            output_group_shapekeys[obj_name] = shapekey_info
            for slot_index, shape_key_name in shapekey_info:
                classification_data[slot_index][shape_key_name].append(obj_name)

    if not classification_data:
        print("[ShapeKeyExport] 未找到任何形态键，跳过分类报告生成")
        return False

    output_lines = ["# 自动化形态键导出 - 分类报告", time.ctime(), "=" * 40, ""]

    if output_groups:
        output_lines.append("# 原始物体与衍生物体映射:")
        for source_name in sorted(output_groups.keys()):
            derived_sorted = sorted(output_groups.get(source_name, set()))
            if derived_sorted:
                output_lines.append(f"#   {source_name} -> {', '.join(derived_sorted)}")
        output_lines.append("")

    for slot in sorted(classification_data.keys()):
        output_lines.append(f"槽位 {slot}:")
        sk_data = classification_data[slot]
        for sk_name in sorted(sk_data.keys()):
            output_lines.append(f"  - 名称: {sk_name}")
            for obj_name in sorted(set(sk_data[sk_name])):
                output_lines.append(f"    - 物体: {obj_name}")
        output_lines.append("")

    final_text = "\n".join(output_lines)
    text_block_name = "Shape_Key_Classification"
    if text_block_name in bpy.data.texts:
        txt = bpy.data.texts[text_block_name]
        txt.clear()
    else:
        txt = bpy.data.texts.new(name=text_block_name)
    txt.write(final_text)

    print(f"[ShapeKeyExport] 形态键分类报告已生成: '{text_block_name}'")
    print(f"[ShapeKeyExport]   - 原始物体数: {len(output_group_shapekeys)}")
    print(f"[ShapeKeyExport]   - 衍生物体映射数: {len(output_groups)}")
    for slot, sk_data in sorted(classification_data.items()):
        summary = {shape_key_name: sorted(set(obj_names)) for shape_key_name, obj_names in sk_data.items()}
        print(f"[ShapeKeyExport]   - 槽位 {slot}: {summary}")
    return True


BlueprintExportHelper.generate_shapekey_classification_report = staticmethod(
    _patched_generate_shapekey_classification_report
)


def register():
    pass


def unregister():
    pass
