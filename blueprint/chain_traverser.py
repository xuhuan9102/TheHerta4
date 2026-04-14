import bpy
import copy
from typing import List, Dict, Optional, Set, Tuple

from ..utils.log_utils import LOG
from ..common.m_key import M_Key
from .export_helper import BlueprintExportHelper
from .model import ProcessingChain

_NODE_TYPE_OBJECT_INFO = 'SSMTNode_Object_Info'
_NODE_TYPE_OBJECT_GROUP = 'SSMTNode_Object_Group'
_NODE_TYPE_RESULT_OUTPUT = 'SSMTNode_Result_Output'
_NODE_TYPE_SHAPEKEY = 'SSMTNode_ShapeKey'
_NODE_TYPE_SHAPEKEY_OUTPUT = 'SSMTNode_ShapeKey_Output'
_NODE_TYPE_OBJECT_RENAME = 'SSMTNode_Object_Rename'
_NODE_TYPE_OBJECT_SWAP = 'SSMTNode_ObjectSwap'
_NODE_TYPE_DATA_TYPE = 'SSMTNode_DataType'
_NODE_TYPE_VERTEX_GROUP_PROCESS = 'SSMTNode_VertexGroupProcess'
_NODE_TYPE_VERTEX_GROUP_MATCH = 'SSMTNode_VertexGroupMatch'
_NODE_TYPE_VERTEX_GROUP_MAPPING_INPUT = 'SSMTNode_VertexGroupMappingInput'
_NODE_TYPE_BLUEPRINT_NEST = 'SSMTNode_Blueprint_Nest'
_NODE_TYPE_CROSS_IB = 'SSMTNode_CrossIB'
_NODE_TYPE_MULTI_FILE_EXPORT = 'SSMTNode_MultiFile_Export'

_KNOWN_NODE_TYPES = {
    _NODE_TYPE_OBJECT_INFO,
    _NODE_TYPE_OBJECT_GROUP,
    _NODE_TYPE_RESULT_OUTPUT,
    _NODE_TYPE_SHAPEKEY,
    _NODE_TYPE_SHAPEKEY_OUTPUT,
    _NODE_TYPE_OBJECT_RENAME,
    _NODE_TYPE_OBJECT_SWAP,
    _NODE_TYPE_DATA_TYPE,
    _NODE_TYPE_VERTEX_GROUP_PROCESS,
    _NODE_TYPE_VERTEX_GROUP_MATCH,
    _NODE_TYPE_VERTEX_GROUP_MAPPING_INPUT,
    _NODE_TYPE_BLUEPRINT_NEST,
    _NODE_TYPE_CROSS_IB,
    _NODE_TYPE_MULTI_FILE_EXPORT,
}


def _is_postprocess_node(bl_idname: str) -> bool:
    return bl_idname.startswith('SSMTNode_PostProcess_')


def _is_known_node_type(bl_idname: str) -> bool:
    return bl_idname in _KNOWN_NODE_TYPES or _is_postprocess_node(bl_idname)


class ChainTraverser:

    def __init__(self, blueprint_model):
        self._model = blueprint_model
        self._tree = blueprint_model._tree
        self._duplicate_counter: Dict[str, int] = {}

    def traverse_all_chains(self, tree: bpy.types.NodeTree, output_node: bpy.types.Node) -> List[ProcessingChain]:
        object_info_nodes = BlueprintExportHelper.get_nodes_from_bl_idname(tree, _NODE_TYPE_OBJECT_INFO)

        nest_object_info_nodes = self._collect_nested_object_info_nodes()
        object_info_nodes.extend(nest_object_info_nodes)

        multi_file_export_nodes = BlueprintExportHelper.get_nodes_from_bl_idname(tree, _NODE_TYPE_MULTI_FILE_EXPORT)
        for node in multi_file_export_nodes:
            if not node.mute:
                object_info_nodes.append(node)

        nested_counts = {}
        for n_oi in nest_object_info_nodes:
            n_tree = n_oi.id_data
            tree_name = n_tree.name
            if tree_name not in nested_counts:
                nested_counts[tree_name] = 0
            nested_counts[tree_name] += 1

        nested_info = ", ".join([f"{name}({count}个)" for name, count in nested_counts.items()])
        multi_file_count = len(multi_file_export_nodes)
        if nested_info:
            LOG.info(f"🔄 正向解析: 主蓝图 {tree.name}({len(object_info_nodes) - len(nest_object_info_nodes) - multi_file_count}个Object_Info, {multi_file_count}个MultiFile_Export), 嵌套蓝图 {nested_info}")
        else:
            LOG.info(f"🔄 正向解析: 主蓝图 {tree.name}({len(object_info_nodes) - len(nest_object_info_nodes) - multi_file_count}个Object_Info, {multi_file_count}个MultiFile_Export)")

        visited_chains: Set[str] = set()
        all_chains: List[ProcessingChain] = []

        for obj_node in object_info_nodes:
            if obj_node.bl_idname == _NODE_TYPE_MULTI_FILE_EXPORT:
                obj_list = getattr(obj_node, 'object_list', [])
                if not obj_list:
                    continue
                
                current_export_index = BlueprintExportHelper.current_export_index - 1
                if current_export_index < 0:
                    current_export_index = 0
                if current_export_index >= len(obj_list):
                    current_export_index = len(obj_list) - 1
                
                item = obj_list[current_export_index]
                object_name = getattr(item, 'object_name', '')
                
                if not object_name:
                    continue
                
                chain = ProcessingChain(
                    object_name=object_name,
                    source_node=obj_node
                )
                
                LOG.info(f"   📋 MultiFile_Export '{obj_node.name}' 索引 {current_export_index} → 物体 '{object_name}'")
            else:
                if not getattr(obj_node, 'object_name', ''):
                    continue

                chain = ProcessingChain(
                    object_name=obj_node.object_name,
                    source_node=obj_node
                )

                LOG.info(f"   📋 Object_Info '{obj_node.object_name}' → 开始遍历")

            completed_chains = []
            self._traverse_forward(chain, obj_node, set(), completed_chains)

            node_display_name = object_name if obj_node.bl_idname == _NODE_TYPE_MULTI_FILE_EXPORT else obj_node.object_name
            node_type_name = "MultiFile_Export" if obj_node.bl_idname == _NODE_TYPE_MULTI_FILE_EXPORT else "Object_Info"
            LOG.info(f"   📋 {node_type_name} '{node_display_name}' → 产生 {len(completed_chains)} 条链路")
            for ci, fc in enumerate(completed_chains):
                LOG.info(f"      链路{ci}: object_name='{fc.object_name}', valid={fc.is_valid}, reached_output={fc.reached_output}, nodes={len(fc.node_path)}")

            for finished_chain in completed_chains:
                dedup_key = f"{finished_chain.object_name}@{finished_chain.get_chain_hash()}"
                if dedup_key in visited_chains:
                    LOG.info(f"      ⏭️ 去重跳过: dedup_key='{dedup_key[:60]}...'")
                    continue
                visited_chains.add(dedup_key)
                all_chains.append(finished_chain)

        LOG.info(f"   📋 去重前: {len(all_chains)} 条链路")
        for ci, c in enumerate(all_chains):
            LOG.info(f"      链路{ci}: object_name='{c.object_name}', original='{c.original_object_name}'")

        self._deduplicate_shared_objects(all_chains)

        LOG.info(f"   📋 去重后: {len(all_chains)} 条链路")
        for ci, c in enumerate(all_chains):
            if c.is_valid and c.reached_output:
                LOG.info(f"      链路{ci}: object_name='{c.object_name}', original='{c.original_object_name}'")

        valid_count = sum(1 for c in all_chains if c.is_valid and c.reached_output)
        LOG.info(f"   ✅ 正向解析完成: {valid_count} 条有效处理链 / {len(all_chains)} 条总处理链")

        return all_chains

    def _deduplicate_shared_objects(self, chains: List[ProcessingChain]):
        from .model import BluePrintModel

        obj_name_chains: Dict[str, List[ProcessingChain]] = {}
        for chain in chains:
            if not chain.is_valid or not chain.reached_output:
                continue
            obj_name = chain.object_name
            if obj_name not in obj_name_chains:
                obj_name_chains[obj_name] = []
            obj_name_chains[obj_name].append(chain)

        LOG.info(f"   📋 同名物体检测: {len(obj_name_chains)} 个不同名称")
        for obj_name, shared_chains in obj_name_chains.items():
            if len(shared_chains) > 1:
                LOG.info(f"      ⚠️ '{obj_name}' 被 {len(shared_chains)} 条链路引用，需要复制")
            else:
                LOG.debug(f"      ✓ '{obj_name}' 仅 1 条链路引用")

        for obj_name, shared_chains in obj_name_chains.items():
            if len(shared_chains) <= 1:
                continue

            obj = bpy.data.objects.get(obj_name)
            if not obj:
                mapped_name = BluePrintModel.get_mapped_object_name(obj_name)
                if mapped_name != obj_name:
                    obj = bpy.data.objects.get(mapped_name)
                    if obj:
                        LOG.info(f"      🔄 使用映射名称查找物体: '{obj_name}' → '{mapped_name}'")
            
            if not obj:
                LOG.warning(f"      ⚠️ 找不到 Blender 对象 '{obj_name}'，跳过复制")
                continue

            for i, chain in enumerate(shared_chains):
                if i == 0:
                    continue

                dedup_cache_key = f"{obj_name}_{i}"
                cached_dedup_name = BluePrintModel.get_mapped_object_name(dedup_cache_key)
                
                if cached_dedup_name != dedup_cache_key:
                    chain.object_name = cached_dedup_name
                    if not chain.original_object_name:
                        chain.original_object_name = obj_name
                    LOG.info(f"      ♻️ 复用副本: '{obj_name}' → '{cached_dedup_name}'")
                    continue

                new_obj = obj.copy()
                new_obj.data = obj.data.copy()

                base_name = obj_name
                if base_name.endswith("_copy"):
                    new_name = base_name[:-5] + f"_dup{i}" + "_copy"
                else:
                    new_name = base_name + f"_dup{i}"
                new_obj.name = new_name
                bpy.context.scene.collection.objects.link(new_obj)

                chain.object_name = new_obj.name
                if not chain.original_object_name:
                    chain.original_object_name = obj_name

                BluePrintModel._object_name_mapping[dedup_cache_key] = new_obj.name

                LOG.info(f"      🔀 同名复制: '{obj_name}' → '{new_obj.name}'")

            LOG.info(f"   📋 物体 '{obj_name}' 被 {len(shared_chains)} 条链路引用，已复制 {len(shared_chains) - 1} 份独立副本")

    def _collect_nested_object_info_nodes(self) -> List[bpy.types.Node]:
        nested_obj_info_nodes = []

        for nested_tree in self._model.nested_blueprint_trees:
            for node in nested_tree.nodes:
                if node.bl_idname == _NODE_TYPE_OBJECT_INFO and not node.mute:
                    obj_name = getattr(node, 'object_name', '')
                    if obj_name:
                        nested_obj_info_nodes.append(node)

        return nested_obj_info_nodes

    def _traverse_forward(
        self,
        chain: ProcessingChain,
        current_node: bpy.types.Node,
        visited_nodes: Set[str],
        completed_chains: list,
        current_group_name: str = ""
    ):
        node_tree = current_node.id_data
        tree_name = node_tree.name if node_tree else "Unknown"
        node_id = f"{tree_name}:{current_node.bl_idname}:{current_node.name}"

        if node_id in visited_nodes:
            chain.is_valid = False
            completed_chains.append(chain)
            return

        if current_node.mute:
            visited_nodes_copy = visited_nodes | {node_id}
            muted_connections = []
            for output_socket in current_node.outputs:
                if output_socket.is_linked:
                    for link in output_socket.links:
                        muted_connections.append(link.to_node)
            self._fork_and_traverse(chain, muted_connections, visited_nodes_copy, completed_chains, current_group_name)
            return

        visited_nodes_copy = visited_nodes | {node_id}

        node_type = current_node.bl_idname

        if node_type == _NODE_TYPE_OBJECT_INFO:
            pass

        elif node_type == _NODE_TYPE_MULTI_FILE_EXPORT:
            pass

        elif node_type == _NODE_TYPE_OBJECT_GROUP:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))
            chain.group_stack.append(current_node.name or current_node.label or "Group")

        elif node_type == _NODE_TYPE_SHAPEKEY:
            shapekey_param = self._extract_shapekey_params(current_node)
            if shapekey_param:
                chain.shapekey_params.append(shapekey_param)
                chain.node_path.append(current_node)
                chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))
            else:
                chain.node_path.append(current_node)
                chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_SHAPEKEY_OUTPUT:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_OBJECT_RENAME:
            try:
                from .node_rename import SSMTNode_Object_Rename

                new_name, was_modified, history, signature = SSMTNode_Object_Rename.apply_to_object_name(
                    chain.object_name,
                    current_node
                )

                if was_modified:
                    if not chain.original_object_name:
                        chain.original_object_name = chain.object_name

                    for record in history:
                        operation = {
                            'operation_index': len(chain.rename_history) + 1,
                            **record
                        }
                        chain.rename_history.append(operation)

                chain.node_path.append(current_node)
                chain.node_param_signatures.append(signature)
            except ImportError:
                chain.node_path.append(current_node)
                chain.node_param_signatures.append("Object_Rename[unavailable]")

        elif node_type == _NODE_TYPE_VERTEX_GROUP_PROCESS:
            chain.vertex_group_process_nodes.append(current_node)
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_VERTEX_GROUP_MATCH:
            chain.vertex_group_mapping_nodes.append(current_node)
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_VERTEX_GROUP_MAPPING_INPUT:
            chain.vertex_group_mapping_nodes.append(current_node)
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_BLUEPRINT_NEST:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_CROSS_IB:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_DATA_TYPE:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        elif node_type == _NODE_TYPE_RESULT_OUTPUT:
            node_tree_of_output = current_node.id_data
            parent_nest_node = self._find_parent_nest_node(node_tree_of_output)

            if parent_nest_node is not None:
                chain.node_path.append(current_node)
                chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

                chain.node_path.append(parent_nest_node)
                chain.node_param_signatures.append(ProcessingChain.extract_node_signature(parent_nest_node))

                parent_tree = parent_nest_node.id_data
                parent_tree_name = parent_tree.name if parent_tree else "Unknown"
                parent_node_id = f"{parent_tree_name}:{parent_nest_node.bl_idname}:{parent_nest_node.name}"
                visited_nodes_copy = visited_nodes | {node_id, parent_node_id}

                output_connections = self._get_forward_connections_with_socket_index(parent_nest_node)
                self._fork_and_traverse(chain, [n for n, _ in output_connections], visited_nodes_copy, completed_chains, current_group_name, output_connections)
                return
            else:
                chain.reached_output = True
                completed_chains.append(chain)
                return

        elif not _is_known_node_type(node_type):
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        else:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))

        output_connections = self._get_forward_connections_with_socket_index(current_node)

        if not output_connections:
            return

        self._fork_and_traverse(chain, [n for n, _ in output_connections], visited_nodes_copy, completed_chains, current_group_name, output_connections)

    def _fork_and_traverse(
        self,
        chain: ProcessingChain,
        next_nodes: list,
        visited_nodes: Set[str],
        completed_chains: list,
        current_group_name: str = "",
        output_connections: list = None
    ):
        if not next_nodes:
            return

        if len(next_nodes) == 1:
            next_node = next_nodes[0]
            if output_connections:
                socket_index = next((si for n, si in output_connections if n == next_node), 0)
                if next_node.bl_idname == _NODE_TYPE_OBJECT_SWAP:
                    chain.swap_node_option_values[next_node.name] = socket_index
            self._traverse_forward(chain, next_node, visited_nodes, completed_chains, current_group_name)
            return

        branch_chains = [chain] + [copy.deepcopy(chain) for _ in range(len(next_nodes) - 1)]

        LOG.debug(f"   🔀 链路分叉: {len(next_nodes)} 条分支, 对象 '{chain.object_name}'")

        for i, next_node in enumerate(next_nodes):
            branch_chain = branch_chains[i]
            if i > 0:
                self._duplicate_chain_object(branch_chain, i)
            if output_connections:
                socket_index = next((si for n, si in output_connections if n == next_node), 0)
                if next_node.bl_idname == _NODE_TYPE_OBJECT_SWAP:
                    branch_chain.swap_node_option_values[next_node.name] = socket_index
            self._traverse_forward(branch_chain, next_node, visited_nodes, completed_chains, current_group_name)

    def _duplicate_chain_object(self, chain: ProcessingChain, branch_index: int):
        obj_name = chain.object_name
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            LOG.warning(f"   ⚠️ 链路分叉: 找不到对象 '{obj_name}'，跳过复制")
            return

        new_obj = obj.copy()
        new_obj.data = obj.data.copy()

        base_key = obj_name
        if base_key not in self._duplicate_counter:
            self._duplicate_counter[base_key] = 0
        self._duplicate_counter[base_key] += 1
        dup_index = self._duplicate_counter[base_key]

        chain_suffix = f"_chain{dup_index}"
        if obj_name.endswith("_copy"):
            new_name = obj_name[:-5] + chain_suffix + "_copy"
        else:
            new_name = obj_name + chain_suffix
        new_obj.name = new_name
        bpy.context.scene.collection.objects.link(new_obj)

        chain.object_name = new_obj.name
        if not chain.original_object_name:
            chain.original_object_name = obj_name

        LOG.info(f"   🔀 链路分叉: '{obj_name}' → '{new_obj.name}'")

    def _find_parent_nest_node(self, nested_tree: bpy.types.NodeTree) -> Optional[bpy.types.Node]:
        tree_name = nested_tree.name

        main_tree = self._tree
        if main_tree:
            for node in main_tree.nodes:
                if node.bl_idname == _NODE_TYPE_BLUEPRINT_NEST and not node.mute:
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name == tree_name:
                        return node

        for node_group in bpy.data.node_groups:
            if node_group.bl_idname != 'SSMTBlueprintTreeType':
                continue
            if node_group.name == tree_name:
                continue
            if main_tree and node_group.name == main_tree.name:
                continue
            for node in node_group.nodes:
                if node.bl_idname == _NODE_TYPE_BLUEPRINT_NEST and not node.mute:
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name == tree_name:
                        return node

        return None

    def _get_forward_connections_with_socket_index(self, node: bpy.types.Node) -> List[Tuple[bpy.types.Node, int]]:
        connected_nodes = []

        for output_socket in node.outputs:
            if output_socket.is_linked:
                for link in output_socket.links:
                    to_node = link.to_node
                    to_socket = link.to_socket
                    if to_node and to_node != node:
                        socket_index = 0
                        for idx, inp in enumerate(to_node.inputs):
                            if inp == to_socket:
                                socket_index = idx
                                break
                        connected_nodes.append((to_node, socket_index))

        return connected_nodes

    def _extract_shapekey_params(self, shapekey_node: bpy.types.Node) -> Optional[M_Key]:
        return ChainTraverser.extract_shapekey_params(shapekey_node)

    @staticmethod
    def extract_shapekey_params(shapekey_node: bpy.types.Node) -> Optional[M_Key]:
        try:
            shapekey_name = getattr(shapekey_node, 'shapekey_name', '')
            key = getattr(shapekey_node, 'key', '')
            comment = getattr(shapekey_node, 'comment', '')

            if not shapekey_name:
                return None

            m_key = M_Key()
            m_key.key_name = f"$shapekey_{shapekey_name}"
            m_key.initialize_value = 0
            m_key.initialize_vk_str = key
            m_key.comment = comment

            return m_key

        except Exception as e:
            LOG.error(f"   ❌ 提取 ShapeKey 参数失败: {e}")
            return None
