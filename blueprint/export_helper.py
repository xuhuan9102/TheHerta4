import bpy
from bpy.types import NodeTree, Node, NodeSocket

from ..common.global_config import GlobalConfig
from ..common.m_key import M_Key


def _get_node_unique_key(node) -> str:
    tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
    return f"{tree_name}::{node.name}"


class BlueprintExportHelper:

    current_export_index = 1

    max_export_count = 1

    runtime_blueprint_tree_name = ""

    multi_file_export_nodes = []

    current_buffer_folder_name = "Meshes"

    MAX_EXPORT_COUNT_LIMIT = 1000

    @staticmethod
    def _is_valid_blueprint_tree(tree):
        return tree is not None and getattr(tree, "bl_idname", "") == 'SSMTBlueprintTreeType'

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

        tree_name = GlobalConfig.workspacename
        if not tree_name:
            return None

        tree = bpy.data.node_groups.get(tree_name)
        if BlueprintExportHelper._is_valid_blueprint_tree(tree):
            BlueprintExportHelper.set_runtime_blueprint_tree(tree)
            return tree

        return None

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
        for node in tree.nodes:
            if node.bl_idname == node_type:
                return node
        return None

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

            output_node = None
            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_Result_Output':
                    output_node = node
                    break

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

            output_node = BlueprintExportHelper.get_node_from_bl_idname(current_tree, 'SSMTNode_Result_Output')
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
                if getattr(output_socket, 'bl_idname', '') != 'SSMTSocketPostProcess':
                    continue
                if not output_socket.is_linked:
                    continue
                for link in output_socket.links:
                    target = link.to_node
                    if target.bl_idname.startswith('SSMTNode_PostProcess_'):
                        follow_forward(target)

        def follow_backward(node):
            node_key = _get_node_unique_key(node)
            if node_key in visited:
                return
            visited.add(node_key)

            for input_socket in node.inputs:
                if getattr(input_socket, 'bl_idname', '') != 'SSMTSocketPostProcess':
                    continue
                if not input_socket.is_linked:
                    continue
                for link in input_socket.links:
                    source = link.from_node
                    if source.bl_idname.startswith('SSMTNode_PostProcess_'):
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
        
        output_node = BlueprintExportHelper.get_node_from_bl_idname(tree, 'SSMTNode_Result_Output')
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
    def collect_shapekey_objects(tree) -> list:
        BlueprintExportHelper.shapekey_objects = []
        if not tree:
            return []

        visited_trees = set()

        def collect_from_tree(current_tree):
            if current_tree.name in visited_trees:
                return
            visited_trees.add(current_tree.name)

            output_node = BlueprintExportHelper.get_node_from_bl_idname(current_tree, 'SSMTNode_Result_Output')

            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_Object_Info' and not node.mute:
                    if output_node and not BlueprintExportHelper._is_node_connected_to_output(current_tree, node):
                        continue
                    obj_name = getattr(node, 'object_name', '')
                    if obj_name and obj_name not in BlueprintExportHelper.shapekey_objects:
                        BlueprintExportHelper.shapekey_objects.append(obj_name)

                elif node.bl_idname == 'SSMTNode_MultiFile_Export' and not node.mute:
                    if output_node and not BlueprintExportHelper._is_node_connected_to_output(current_tree, node):
                        continue
                    obj_list = getattr(node, 'object_list', [])
                    if obj_list:
                        item = obj_list[0]
                        obj_name = getattr(item, 'object_name', '')
                        if obj_name and obj_name not in BlueprintExportHelper.shapekey_objects:
                            BlueprintExportHelper.shapekey_objects.append(obj_name)

                elif node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    nested_tree_name = getattr(node, 'blueprint_name', '')
                    if nested_tree_name and nested_tree_name != 'NONE':
                        nested_tree = bpy.data.node_groups.get(nested_tree_name)
                        if nested_tree:
                            collect_from_tree(nested_tree)

        collect_from_tree(tree)

        print(f"[ShapeKeyExport] 收集到 {len(BlueprintExportHelper.shapekey_objects)} 个物体")
        return BlueprintExportHelper.shapekey_objects

    @staticmethod
    def calculate_max_shapekey_slot_count(tree) -> int:
        objects = BlueprintExportHelper.collect_shapekey_objects(tree)
        
        max_slot = 0
        slot_info = []
        
        for obj_name in objects:
            obj = bpy.data.objects.get(obj_name)
            if not obj or not obj.data:
                continue
            if not hasattr(obj.data, 'shape_keys') or not obj.data.shape_keys:
                continue
            
            key_blocks = obj.data.shape_keys.key_blocks
            num_keys = len(key_blocks)
            if num_keys > 1:
                slot_count = num_keys - 1
                slot_info.append(f"'{obj_name}':{slot_count}")
                if slot_count > max_slot:
                    max_slot = slot_count
        
        BlueprintExportHelper.max_shapekey_slot_count = max_slot
        
        if slot_info:
            print(f"[ShapeKeyExport] 形态键槽位统计: [{', '.join(slot_info)}] → 最大值 {max_slot}")
        else:
            print(f"[ShapeKeyExport] 未检测到形态键")
        
        return max_slot

    @staticmethod
    def set_all_shapekey_values(value: int, slot_index: int = None):
        print(f"[ShapeKeyExport] set_all_shapekey_values 调用: value={value}, slot_index={slot_index}")
        print(f"[ShapeKeyExport] shapekey_objects 列表: {BlueprintExportHelper.shapekey_objects}")
        
        for obj_name in BlueprintExportHelper.shapekey_objects:
            obj = bpy.data.objects.get(obj_name)
            if not obj or not obj.data:
                print(f"[ShapeKeyExport]   物体不存在或无数据: {obj_name}")
                continue
            if not hasattr(obj.data, 'shape_keys') or not obj.data.shape_keys:
                print(f"[ShapeKeyExport]   物体无形态键: {obj_name}")
                continue
            
            key_blocks = obj.data.shape_keys.key_blocks
            print(f"[ShapeKeyExport]   物体 {obj_name} 有 {len(key_blocks)} 个形态键")
            
            for i, kb in enumerate(key_blocks):
                if i == 0:
                    continue
                
                if slot_index is not None:
                    if i == slot_index:
                        try:
                            kb.value = 1.0
                            print(f"[ShapeKeyExport]     设置 {kb.name} = 1.0")
                        except Exception as e:
                            print(f"[ShapeKeyExport]     设置失败 {kb.name}: {e}")
                    else:
                        try:
                            kb.value = 0.0
                        except Exception:
                            pass
                else:
                    try:
                        kb.value = float(value)
                    except Exception:
                        pass
        
        if slot_index is not None:
            print(f"[ShapeKeyExport] 设置槽位 {slot_index} 形态键=1，其他=0")
        else:
            print(f"[ShapeKeyExport] 设置所有形态键值={value}")

    @staticmethod
    def generate_shapekey_classification_report(blueprint_model=None):
        from collections import defaultdict
        import re
        
        def extract_original_name(name):
            if not name:
                return name
            patterns = [
                r'_copy$',
                r'_chain\d+_copy$',
                r'_dup\d+_copy$',
                r'_chain\d+_dup\d+_copy$',
            ]
            result = name
            for pattern in patterns:
                result = re.sub(pattern, '', result)
            return result
        
        classification_data = defaultdict(lambda: defaultdict(list))
        original_to_derived = defaultdict(set)
        original_to_shapekeys = {}
        
        if blueprint_model:
            print(f"[ShapeKeyExport] 开始生成分类报告，处理链数: {len(blueprint_model.processing_chains)}")
            
            for chain in blueprint_model.processing_chains:
                if not chain.is_valid or not chain.reached_output:
                    continue
                
                current_obj_name = chain.object_name
                original_obj_name = chain.original_object_name or chain.object_name
                true_original_name = extract_original_name(original_obj_name)
                
                original_to_derived[true_original_name].add(current_obj_name)
                original_to_derived[true_original_name].add(original_obj_name)
                if current_obj_name != original_obj_name:
                    original_to_derived[true_original_name].add(current_obj_name)
            
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
                
                if obj and obj.data and not obj.hide_get():
                    if hasattr(obj.data, 'shape_keys') and obj.data.shape_keys:
                        key_blocks = obj.data.shape_keys.key_blocks
                        shapekey_info = []
                        for i, kb in enumerate(key_blocks):
                            if i == 0:
                                continue
                            shapekey_info.append((i, kb.name))
                        
                        if shapekey_info:
                            original_to_shapekeys[original_obj_name] = shapekey_info
                            print(f"[ShapeKeyExport]   从原始物体获取形态键: {shapekey_info}")
                            continue
                
                for derived_name in original_to_derived[original_obj_name]:
                    if derived_name == original_obj_name:
                        continue
                    derived_obj = bpy.data.objects.get(derived_name)
                    if derived_obj and derived_obj.data and not derived_obj.hide_get():
                        if hasattr(derived_obj.data, 'shape_keys') and derived_obj.data.shape_keys:
                            key_blocks = derived_obj.data.shape_keys.key_blocks
                            shapekey_info = []
                            for i, kb in enumerate(key_blocks):
                                if i == 0:
                                    continue
                                shapekey_info.append((i, kb.name))
                            
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
                if obj.hide_viewport or obj.hide_render or obj.hide_get():
                    continue
                if not hasattr(obj.data, 'shape_keys') or not obj.data.shape_keys:
                    continue
                
                key_blocks = obj.data.shape_keys.key_blocks
                for i, kb in enumerate(key_blocks):
                    if i == 0:
                        continue
                    slot_index = i
                    shape_key_name = kb.name
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


def register():
    pass


def unregister():
    pass
