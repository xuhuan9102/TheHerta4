import bpy
import copy
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime

from ..utils.log_utils import LOG

from ..common.m_key import M_Key
from ..common.draw_call_model import DrawCallModel
from .export_helper import BlueprintExportHelper

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


@dataclass
class ProcessingChain:

    object_name: str = ""
    original_object_name: str = ""
    source_node: Optional[bpy.types.Node] = None

    node_path: List[bpy.types.Node] = field(default_factory=list)
    node_param_signatures: List[str] = field(default_factory=list)

    shapekey_params: List[M_Key] = field(default_factory=list)
    group_stack: List[str] = field(default_factory=list)

    condition_operator: str = " && "

    rename_history: List[dict] = field(default_factory=list)

    swap_node_option_values: Dict[str, int] = field(default_factory=dict)

    vertex_group_process_nodes: List[bpy.types.Node] = field(default_factory=list)
    vertex_group_mapping_nodes: List[bpy.types.Node] = field(default_factory=list)

    reached_output: bool = False
    is_valid: bool = True

    @staticmethod
    def extract_node_signature(node: bpy.types.Node) -> str:
        node_type = node.bl_idname.replace('SSMTNode_', '')

        if node_type == 'Object_Info':
            params = []

            obj_name = getattr(node, 'object_name', '')
            if obj_name:
                params.append(f"obj={obj_name}")

            return f"ObjectInfo[{','.join(params)}]" if params else "ObjectInfo[]"

        elif node_type == 'Object_Group':
            return "Group[]"

        elif node_type == 'ShapeKey':
            params = []

            shapekey_name = getattr(node, 'shapekey_name', '')
            if shapekey_name:
                params.append(f"name={shapekey_name}")

            key = getattr(node, 'key', '')
            if key:
                params.append(f"vk={key}")

            comment = getattr(node, 'comment', '')
            if comment:
                params.append(f"cmt={comment}")

            return f"ShapeKey[{','.join(params)}]" if params else "ShapeKey[]"

        elif node_type == 'Object_Rename':
            try:
                from .node_rename import SSMTNode_Object_Rename
                return SSMTNode_Object_Rename.generate_signature(
                    [{'search_str': r.search_str, 'replace_str': r.replace_str} for r in getattr(node, 'rename_rules', [])],
                    getattr(node, 'reverse_mapping', False)
                )
            except ImportError:
                return "Object_Rename[unavailable]"

        elif node_type == 'Result_Output':
            return "ResultOutput[unique]"

        elif node_type == 'VertexGroupProcess':
            params = []
            process_mode = getattr(node, 'process_mode', '')
            if process_mode:
                params.append(f"mode={process_mode}")
            return f"VertexGroupProcess[{','.join(params)}]" if params else "VertexGroupProcess[]"

        elif node_type == 'VertexGroupMatch':
            params = []
            match_mode = getattr(node, 'match_mode', '')
            if match_mode:
                params.append(f"mode={match_mode}")
            return f"VertexGroupMatch[{','.join(params)}]" if params else "VertexGroupMatch[]"

        elif node_type == 'VertexGroupMappingInput':
            params = []
            mapping_text = getattr(node, 'mapping_text', '')
            if mapping_text:
                params.append(f"text={mapping_text}")
            target_hash = getattr(node, 'target_hash', '')
            if target_hash:
                params.append(f"hash={target_hash}")
            return f"VertexGroupMappingInput[{','.join(params)}]" if params else "VertexGroupMappingInput[]"

        elif node_type == 'Blueprint_Nest':
            params = []
            blueprint_name = getattr(node, 'blueprint_name', '')
            if blueprint_name:
                params.append(f"bp={blueprint_name}")
            return f"BlueprintNest[{','.join(params)}]" if params else "BlueprintNest[]"

        elif node_type == 'CrossIB':
            params = []
            cross_ib_method = getattr(node, 'cross_ib_method', '')
            if cross_ib_method:
                params.append(f"method={cross_ib_method}")
            return f"CrossIB[{','.join(params)}]" if params else "CrossIB[]"

        elif node_type == 'MultiFile_Export':
            params = []
            obj_count = len(getattr(node, 'object_list', []))
            params.append(f"objs={obj_count}")
            return f"MultiFileExport[{','.join(params)}]"

        elif node_type == 'DataType':
            params = []
            draw_ib_match = getattr(node, 'draw_ib_match', '')
            if draw_ib_match:
                params.append(f"ib={draw_ib_match}")
            return f"DataType[{','.join(params)}]" if params else "DataType[]"

        else:
            return f"{node_type}[]"

    def get_chain_hash(self) -> str:
        if not self.node_path:
            return f"SINGLE:{self.object_name}"

        signature_parts = []
        for i, (node, sig) in enumerate(zip(self.node_path, self.node_param_signatures)):
            signature_parts.append(f"[{i}]{sig}")

        path_with_params = "|".join(signature_parts)
        return f"CHAIN:{path_with_params}"

    def get_simple_hash(self) -> str:
        if not self.node_path:
            return f"SINGLE_SIMPLE:{self.object_name}"

        path_types = [node.bl_idname.replace('SSMTNode_', '') for node in self.node_path]
        return f"CHAIN_SIMPLE:{'->'.join(path_types)}"

    def get_chain_description(self) -> str:
        parts = [f"📍 {self.object_name}"]

        if self.group_stack:
            parts.append(f"   └─ 通过分组: {' > '.join(self.group_stack)}")

        if self.shapekey_params:
            sk_details = []
            for sk in self.shapekey_params:
                detail = f"{sk.key_name}"
                if sk.initialize_vk_str:
                    detail += f"(VK:{sk.initialize_vk_str})"
                if sk.comment:
                    detail += f"[{sk.comment}]"
                sk_details.append(detail)
            parts.append(f"   └─ 形态键参数 ({len(self.shapekey_params)}个): {', '.join(sk_details)}")

        if self.node_param_signatures:
            parts.append(f"   └─ 节点参数详情:")
            for i, sig in enumerate(self.node_param_signatures, 1):
                parts.append(f"      {i:>2}. {sig}")

        if self.reached_output:
            parts.append(f"   ✅ 已到达输出节点")
        else:
            parts.append(f"   ⚠️ 未到达输出节点")

        parts.append(f"   🔑 完整哈希: {self.get_chain_hash()[:80]}{'...' if len(self.get_chain_hash()) > 80 else ''}")

        return "\n".join(parts)

    def __deepcopy__(self, memo):
        new_chain = ProcessingChain()
        new_chain.object_name = self.object_name
        new_chain.original_object_name = self.original_object_name
        new_chain.source_node = self.source_node
        new_chain.node_path = list(self.node_path)
        new_chain.node_param_signatures = list(self.node_param_signatures)
        new_chain.shapekey_params = copy.deepcopy(self.shapekey_params, memo)
        new_chain.group_stack = list(self.group_stack)
        new_chain.condition_operator = self.condition_operator
        new_chain.rename_history = copy.deepcopy(self.rename_history, memo)
        new_chain.swap_node_option_values = copy.deepcopy(self.swap_node_option_values, memo)
        new_chain.vertex_group_process_nodes = list(self.vertex_group_process_nodes)
        new_chain.vertex_group_mapping_nodes = list(self.vertex_group_mapping_nodes)
        new_chain.reached_output = self.reached_output
        new_chain.is_valid = self.is_valid
        return new_chain

    def to_draw_call_model(self) -> DrawCallModel:
        obj_model = DrawCallModel(obj_name=self.object_name)

        obj = bpy.data.objects.get(self.object_name)
        if obj:
            obj_model.source_obj_name = obj.name
        elif self.original_object_name:
            obj = bpy.data.objects.get(self.original_object_name)
            if obj:
                obj_model.source_obj_name = obj.name
            else:
                obj_model.source_obj_name = self.original_object_name

        obj_model.work_key_list = copy.deepcopy(self.shapekey_params)

        return obj_model


@dataclass
class ChainGroup:

    chain_hash: str

    chains: List[ProcessingChain] = field(default_factory=list)

    representative_chain: Optional[ProcessingChain] = None

    @property
    def object_count(self) -> int:
        return len(self.chains)

    @property
    def object_names(self) -> List[str]:
        return [chain.object_name for chain in self.chains]

    def get_group_description(self) -> str:
        header = f"📦 处理链组 [{self.chain_hash[:50]}...]" if len(self.chain_hash) > 50 else f"📦 处理链组 [{self.chain_hash}]"

        lines = [header]
        lines.append(f"   物体数量: {self.object_count}")
        lines.append(f"   物体列表: {', '.join(self.object_names)}")

        if self.representative_chain:
            lines.append(f"\n   代表性处理链:")
            rep_desc = self.representative_chain.get_chain_description()
            for line in rep_desc.split("\n"):
                lines.append(f"      {line}")

        return "\n".join(lines)


class BluePrintModel:

    FORWARD_PARSE_MODE = True

    _object_name_mapping: Dict[str, str] = {}
    _has_executed_rename: bool = False

    @classmethod
    def clear_object_name_mapping(cls):
        cls._object_name_mapping = {}
        cls._has_executed_rename = False

    @classmethod
    def get_mapped_object_name(cls, original_name: str) -> str:
        return cls._object_name_mapping.get(original_name, original_name)

    def __init__(self, tree=None, context=None):
        self.keyname_mkey_dict: Dict[str, M_Key] = {}

        self.ordered_draw_obj_data_model_list: List[DrawCallModel] = []

        self.processing_chains: List[ProcessingChain] = []
        self.chain_groups: List[ChainGroup] = []

        self.postprocess_nodes: List[bpy.types.Node] = []
        self.nested_blueprint_trees: List[bpy.types.NodeTree] = []
        self.vertex_group_process_nodes: List[bpy.types.Node] = []
        self.multi_file_export_nodes: List[bpy.types.Node] = []
        self.cross_ib_nodes: List[bpy.types.Node] = []

        self.cross_ib_info_dict: Dict[str, list] = {}
        self.cross_ib_method_dict: Dict[str, str] = {}
        self.cross_ib_mapping_objects: Dict[tuple, set] = {}
        self.cross_ib_vb_condition_mapping: Dict[tuple, dict] = {}
        self.cross_ib_source_to_target_dict: Dict[str, list] = {}
        self.cross_ib_object_vb_condition: Dict[tuple, dict] = {}
        self.cross_ib_target_info: Dict[str, list] = {}
        self.cross_ib_match_mode: str = 'IB_HASH'
        self.cross_ib_object_names: Set[str] = set()
        self.has_cross_ib: bool = False

        tree = tree or BlueprintExportHelper.get_current_blueprint_tree(context=context)
        if not tree:
            raise ValueError("未找到当前蓝图树，请先打开正确的蓝图编辑器")

        self._tree = tree

        LOG.debug(f"   🌳 当前蓝图树: {tree.name if hasattr(tree, 'name') else '未命名'}")

        output_node = BlueprintExportHelper.get_node_from_bl_idname(tree, _NODE_TYPE_RESULT_OUTPUT)
        if not output_node:
            raise ValueError("当前蓝图缺少 Generate Mod 输出节点")

        LOG.info("🔄 启动正向解析模式")

        self._collect_postprocess_nodes(output_node)
        self._collect_special_nodes(tree)

        if self.FORWARD_PARSE_MODE:
            self._forward_parse_blueprint(tree, output_node)
        else:
            self._backward_parse_legacy(output_node)

    def _collect_postprocess_nodes(self, output_node: bpy.types.Node):
        self.postprocess_nodes = []

        for output_socket in output_node.outputs:
            if output_socket.bl_idname == 'SSMTSocketPostProcess' and output_socket.is_linked:
                for link in output_socket.links:
                    self._traverse_postprocess_chain(link.to_node)

        if self.postprocess_nodes:
            LOG.info(f"   🔧 收集到 {len(self.postprocess_nodes)} 个后处理节点")
            for pp_node in self.postprocess_nodes:
                LOG.debug(f"      - {pp_node.bl_idname}: {pp_node.name}")

    def _traverse_postprocess_chain(self, node: bpy.types.Node, visited: Optional[Set[str]] = None):
        if visited is None:
            visited = set()

        node_key = f"{node.bl_idname}:{node.name}"
        if node_key in visited:
            return
        visited.add(node_key)

        if _is_postprocess_node(node.bl_idname):
            if not node.mute:
                self.postprocess_nodes.append(node)
                LOG.debug(f"   🔧 发现后处理节点: {node.bl_idname} ({node.name})")
            else:
                LOG.debug(f"   ⏭️ 跳过禁用的后处理节点: {node.bl_idname} ({node.name})")

        for output_socket in node.outputs:
            if output_socket.is_linked:
                for link in output_socket.links:
                    self._traverse_postprocess_chain(link.to_node, visited)

    def _collect_special_nodes(self, tree: bpy.types.NodeTree):
        self.vertex_group_process_nodes = []
        self.multi_file_export_nodes = []
        self.nested_blueprint_trees = []
        self.cross_ib_nodes = []

        for node in tree.nodes:
            if node.mute:
                continue

            if node.bl_idname == _NODE_TYPE_VERTEX_GROUP_PROCESS:
                self.vertex_group_process_nodes.append(node)
                LOG.debug(f"   🔧 发现顶点组处理节点: {node.name}")

            elif node.bl_idname == _NODE_TYPE_MULTI_FILE_EXPORT:
                self.multi_file_export_nodes.append(node)
                LOG.debug(f"   🔧 发现多文件导出节点: {node.name}")

            elif node.bl_idname == _NODE_TYPE_BLUEPRINT_NEST:
                self._resolve_nested_blueprint_collect(node)

            elif node.bl_idname == _NODE_TYPE_CROSS_IB:
                self.cross_ib_nodes.append(node)
                LOG.debug(f"   🔧 发现跨IB节点: {node.name}")

        if self.vertex_group_process_nodes:
            LOG.info(f"   🔧 收集到 {len(self.vertex_group_process_nodes)} 个顶点组处理节点")
        if self.multi_file_export_nodes:
            LOG.info(f"   🔧 收集到 {len(self.multi_file_export_nodes)} 个多文件导出节点")
        if self.nested_blueprint_trees:
            LOG.info(f"   🔧 收集到 {len(self.nested_blueprint_trees)} 个嵌套蓝图")
        if self.cross_ib_nodes:
            LOG.info(f"   🔧 收集到 {len(self.cross_ib_nodes)} 个跨IB节点")

    def _resolve_nested_blueprint_collect(self, nest_node: bpy.types.Node, visited: Optional[Set[str]] = None):
        if visited is None:
            visited = set()

        blueprint_name = getattr(nest_node, 'blueprint_name', '')
        if not blueprint_name or blueprint_name == 'NONE':
            LOG.debug(f"   🔗 Blueprint_Nest 节点 '{nest_node.name}' 未指定蓝图，跳过")
            return

        if blueprint_name in visited:
            LOG.warning(f"   ⚠️ 检测到嵌套蓝图循环引用: {blueprint_name}，跳过")
            return

        nested_tree = bpy.data.node_groups.get(blueprint_name)
        if not nested_tree:
            LOG.warning(f"   ⚠️ Blueprint_Nest 节点 '{nest_node.name}' 引用的蓝图 '{blueprint_name}' 不存在")
            return

        if nested_tree.bl_idname != 'SSMTBlueprintTreeType':
            LOG.warning(f"   ⚠️ Blueprint_Nest 节点 '{nest_node.name}' 引用的 '{blueprint_name}' 不是 SSMT 蓝图树 (类型: {nested_tree.bl_idname})")
            return

        visited_copy = visited | {blueprint_name}
        self.nested_blueprint_trees.append(nested_tree)
        LOG.info(f"   🔗 解析嵌套蓝图: '{blueprint_name}' (节点数: {len(nested_tree.nodes)})")

        for inner_node in nested_tree.nodes:
            if inner_node.bl_idname == _NODE_TYPE_BLUEPRINT_NEST and not inner_node.mute:
                inner_bp_name = getattr(inner_node, 'blueprint_name', '')
                LOG.debug(f"   🔗 嵌套蓝图 '{blueprint_name}' 中发现子嵌套节点: '{inner_node.name}' → '{inner_bp_name}'")
                self._resolve_nested_blueprint_collect(inner_node, visited_copy)

    def _forward_parse_blueprint(self, tree: bpy.types.NodeTree, output_node: bpy.types.Node):
        from .chain_traverser import ChainTraverser

        max_export_count = BlueprintExportHelper.calculate_max_export_count(tree)
        if max_export_count > 1:
            LOG.info(f"   📦 多文件导出模式: 检测到 {len(self.multi_file_export_nodes)} 个多文件导出节点")
            LOG.info(f"   📦 导出次数: {max_export_count} 次")
            for node in self.multi_file_export_nodes:
                obj_list = getattr(node, 'object_list', [])
                LOG.info(f"      - 节点 '{node.name}': {len(obj_list)} 个物体")

        traverser = ChainTraverser(self)
        self.processing_chains = traverser.traverse_all_chains(tree, output_node)

        self._merge_processing_chains()

        self._integrate_object_swap_nodes()

        self._execute_object_rename_nodes()

        self._integrate_vertex_group_nodes()

        self._process_cross_ib_nodes()

        self._build_draw_call_models_from_chains()

        self._output_debug_info_to_text_editor()

    def _execute_object_rename_nodes(self):
        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]

        if BluePrintModel._has_executed_rename:
            for chain in valid_chains:
                original_obj_name = chain.object_name
                mapped_name = BluePrintModel.get_mapped_object_name(original_obj_name)
                if mapped_name != original_obj_name:
                    chain.object_name = mapped_name
                    LOG.debug(f"   🔄 使用映射名称: '{original_obj_name}' → '{mapped_name}'")
            return

        rename_chains = [c for c in valid_chains if c.rename_history]
        if not rename_chains:
            BluePrintModel._has_executed_rename = True
            return

        from .node_rename import SSMTNode_Object_Rename

        for chain in rename_chains:
            obj = bpy.data.objects.get(chain.object_name)
            if not obj:
                LOG.warning(f"   ⚠️ 重命名跳过: 找不到对象 '{chain.object_name}'")
                continue

            original_obj_name = chain.object_name
            current_name = obj.name
            for node in chain.node_path:
                if node.bl_idname == _NODE_TYPE_OBJECT_RENAME:
                    new_name, was_modified, _, _ = SSMTNode_Object_Rename.apply_to_object_name(
                        current_name, node
                    )
                    if was_modified:
                        current_name = new_name

            obj.name = current_name
            chain.object_name = obj.name

            BluePrintModel._object_name_mapping[original_obj_name] = obj.name

            LOG.info(f"   ✏️ 重命名: '{chain.original_object_name or original_obj_name}' → '{obj.name}'")

        BluePrintModel._has_executed_rename = True
        SSMTNode_Object_Rename.log_rename_summary(rename_chains)

    def _merge_processing_chains(self):
        LOG.info("🔗 开始合并处理链...")

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
        if valid_chains:
            longest_chain = max(valid_chains, key=lambda c: len(c.node_path))
            LOG.info(f"   📏 最长处理链参考: '{longest_chain.object_name}' (节点数: {len(longest_chain.node_path)})")

        chain_dict: Dict[str, ChainGroup] = {}

        for chain in self.processing_chains:
            chain_hash = chain.get_chain_hash()

            if chain_hash not in chain_dict:
                chain_dict[chain_hash] = ChainGroup(
                    chain_hash=chain_hash,
                    chains=[chain],
                    representative_chain=chain
                )
            else:
                chain_dict[chain_hash].chains.append(chain)

        self.chain_groups = list(chain_dict.values())

        merged_count = sum(1 for g in self.chain_groups if g.object_count > 1)
        total_objects = len(self.processing_chains)

        LOG.info(f"🔗 合并处理链: {total_objects} 个物体 → {len(self.chain_groups)} 个处理链组 (其中 {merged_count} 个组合并)")

    def _build_draw_call_models_from_chains(self):
        self.ordered_draw_obj_data_model_list.clear()

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
        invalid_count = len(self.processing_chains) - len(valid_chains)

        if not valid_chains:
            LOG.warning("   ⚠️ 没有有效的处理链")
            return

        for chain in valid_chains:
            draw_call_model = chain.to_draw_call_model()
            self.ordered_draw_obj_data_model_list.append(draw_call_model)

        LOG.info(f"🏗️ DrawCallModel 构建: {len(valid_chains)} 个有效 / {invalid_count} 个无效")

    def _integrate_object_swap_nodes(self):
        try:
            from .node_swap_processor import integrate_object_swap_to_blueprint_model

            integrate_object_swap_to_blueprint_model(self)
            LOG.info("✓ 物体切换节点集成完成")
        except ImportError:
            LOG.debug("⊘ 物体切换节点模块未找到（可选功能）")
        except Exception as e:
            LOG.warning(f"⚠️ 物体切换节点集成遇到错误: {e}")

    def _integrate_vertex_group_nodes(self):
        if not self.vertex_group_process_nodes:
            return

        try:
            from .node_vertex_group_process import SSMTNode_VertexGroupProcess

            valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
            if not valid_chains:
                LOG.warning("   ⚠️ 没有有效的处理链，跳过顶点组处理")
                return

            SSMTNode_VertexGroupProcess.execute_batch_from_chains(valid_chains)

        except ImportError:
            LOG.debug("⊘ 顶点组处理节点模块未找到（可选功能）")
        except Exception as e:
            import traceback
            LOG.warning(f"⚠️ 顶点组处理节点集成遇到错误: {e}")
            traceback.print_exc()

    def _process_cross_ib_nodes(self):
        LOG.info(f"🔗 开始处理跨IB节点，共 {len(self.cross_ib_nodes)} 个节点")

        if not self.cross_ib_nodes:
            self.has_cross_ib = False
            LOG.info("🔗 没有找到跨IB节点，跳过处理")
            return

        from .node_cross_ib import SSMTNode_CrossIB, CrossIBMatchMode

        for cross_ib_node in self.cross_ib_nodes:
            if hasattr(cross_ib_node, '_update_cross_ib_method'):
                cross_ib_node._update_cross_ib_method()

            LOG.info(f"🔗 跨IB节点 '{cross_ib_node.name}':")
            LOG.info(f"🔗   cross_ib_list 长度: {len(cross_ib_node.cross_ib_list)}")

            for i, item in enumerate(cross_ib_node.cross_ib_list):
                LOG.info(f"🔗   条目 {i}: source_ib='{item.source_ib}', target_ib='{item.target_ib}'")
                LOG.info(f"🔗   条目 {i}: source_index_count='{item.source_index_count}', target_index_count='{item.target_index_count}'")

            node_ib_mapping = cross_ib_node.get_ib_mapping_dict()
            node_method = getattr(cross_ib_node, 'cross_ib_method', '')
            node_match_mode = getattr(cross_ib_node, 'match_mode', CrossIBMatchMode.INDEX_COUNT)

            LOG.info(f"🔗   method={node_method}, match_mode={node_match_mode}")
            LOG.info(f"🔗   映射内容: {node_ib_mapping}")

            self.cross_ib_method_dict[cross_ib_node.name] = node_method

            if not self.cross_ib_match_mode or self.cross_ib_match_mode == 'IB_HASH':
                self.cross_ib_match_mode = node_match_mode

            for source_key, target_keys in node_ib_mapping.items():
                if source_key not in self.cross_ib_info_dict:
                    self.cross_ib_info_dict[source_key] = []
                for target_key in target_keys:
                    if target_key not in self.cross_ib_info_dict[source_key]:
                        self.cross_ib_info_dict[source_key].append(target_key)

                if source_key not in self.cross_ib_source_to_target_dict:
                    self.cross_ib_source_to_target_dict[source_key] = []
                for target_key in target_keys:
                    if target_key not in self.cross_ib_source_to_target_dict[source_key]:
                        self.cross_ib_source_to_target_dict[source_key].append(target_key)

            for source_key, target_keys in node_ib_mapping.items():
                for target_key in target_keys:
                    mapping_key = (source_key, target_key)
                    if mapping_key not in self.cross_ib_vb_condition_mapping:
                        vb_condition_source = cross_ib_node.get_vb_condition_source()
                        vb_condition_target = cross_ib_node.get_vb_condition_target()
                        self.cross_ib_vb_condition_mapping[mapping_key] = {
                            'source': vb_condition_source,
                            'target': vb_condition_target
                        }
                        LOG.info(f"🔗   VB条件映射 {mapping_key}: source={vb_condition_source}, target={vb_condition_target}")

            for source_key, target_keys in node_ib_mapping.items():
                for target_key in target_keys:
                    if target_key not in self.cross_ib_target_info:
                        self.cross_ib_target_info[target_key] = []
                    if source_key not in self.cross_ib_target_info[target_key]:
                        self.cross_ib_target_info[target_key].append(source_key)

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
        LOG.info(f"🔗 有效处理链数量: {len(valid_chains)}")

        cross_ib_chain_count = 0
        for chain in valid_chains:
            cross_ib_nodes_in_chain = [n for n in chain.node_path if n.bl_idname == _NODE_TYPE_CROSS_IB]
            if not cross_ib_nodes_in_chain:
                continue

            cross_ib_chain_count += 1
            obj_name = chain.object_name

            for cross_ib_node in cross_ib_nodes_in_chain:
                node_ib_mapping = cross_ib_node.get_ib_mapping_dict()
                node_match_mode = getattr(cross_ib_node, 'match_mode', CrossIBMatchMode.INDEX_COUNT)

                obj_ib_keys = self._get_object_ib_keys(obj_name)
                LOG.info(f"🔗 物体 '{obj_name}' 的 IB keys: {obj_ib_keys}")

                matched_source_key = None
                for key in obj_ib_keys:
                    if key in node_ib_mapping:
                        matched_source_key = key
                        break

                if matched_source_key:
                    self.cross_ib_object_names.add(obj_name)
                    LOG.info(f"🔗   物体 '{obj_name}' 被标记为跨IB物体，匹配源: {matched_source_key}")

                    for target_key in node_ib_mapping[matched_source_key]:
                        mapping_key = (matched_source_key, target_key)
                        if mapping_key not in self.cross_ib_mapping_objects:
                            self.cross_ib_mapping_objects[mapping_key] = set()
                        self.cross_ib_mapping_objects[mapping_key].add(obj_name)

                        vb_condition_source = cross_ib_node.get_vb_condition_source()
                        vb_condition_target = cross_ib_node.get_vb_condition_target()
                        object_mapping_key = (obj_name, matched_source_key, target_key)
                        if object_mapping_key not in self.cross_ib_object_vb_condition:
                            self.cross_ib_object_vb_condition[object_mapping_key] = {
                                'source': vb_condition_source,
                                'target': vb_condition_target
                            }

        LOG.info(f"🔗 经过跨IB节点的处理链数量: {cross_ib_chain_count}")

        self.has_cross_ib = len(self.cross_ib_info_dict) > 0

        if self.has_cross_ib:
            LOG.info(f"🔗 跨IB处理完成: {len(self.cross_ib_info_dict)} 个源映射, {len(self.cross_ib_object_names)} 个跨IB物体")
            for source_key, target_keys in self.cross_ib_info_dict.items():
                LOG.info(f"🔗   源 {source_key} → 目标 {target_keys}")
            for mapping_key, obj_names in self.cross_ib_mapping_objects.items():
                LOG.info(f"🔗   映射 {mapping_key}: {obj_names}")
        else:
            LOG.info("🔗 跨IB处理完成: 没有有效的跨IB映射")

    def _get_object_ib_key(self, obj_name: str, match_mode: str) -> Optional[str]:
        try:
            from .node_cross_ib import CrossIBMatchMode
            from ..common.draw_call_model import DrawCallModel
            temp_model = DrawCallModel(obj_name=obj_name)

            if match_mode == CrossIBMatchMode.INDEX_COUNT:
                return f"indexcount_{temp_model.match_index_count}" if temp_model.match_index_count else None
            else:
                return f"{temp_model.match_draw_ib}_{temp_model.match_first_index}"
        except Exception:
            return None

    def _get_object_ib_keys(self, obj_name: str) -> list:
        keys = []
        try:
            from ..common.draw_call_model import DrawCallModel
            temp_model = DrawCallModel(obj_name=obj_name)

            if temp_model.match_index_count:
                keys.append(f"indexcount_{temp_model.match_index_count}")

            if temp_model.match_draw_ib and temp_model.match_first_index is not None:
                keys.append(f"{temp_model.match_draw_ib}_{temp_model.match_first_index}")

            if temp_model.match_draw_ib:
                keys.append(f"{temp_model.match_draw_ib}_0")

        except Exception as e:
            LOG.debug(f"🔗 获取物体 '{obj_name}' 的 IB keys 失败: {e}")

        return keys

    def execute_postprocess_nodes(self, mod_export_path: str):
        if not self.postprocess_nodes:
            return

        LOG.info(f"🔧 后处理节点开始执行: {len(self.postprocess_nodes)} 个节点")

        for pp_node in self.postprocess_nodes:
            try:
                if hasattr(pp_node, 'execute_postprocess'):
                    pp_node.execute_postprocess(mod_export_path)
                else:
                    LOG.warning(f"   ⚠️ 后处理节点缺少 execute_postprocess 方法: {pp_node.bl_idname}")
            except Exception as e:
                LOG.error(f"   ❌ 后处理节点执行失败: {pp_node.bl_idname} ({pp_node.name}): {e}")

        LOG.info(f"   ✅ 后处理节点执行完成: {len(self.postprocess_nodes)} 个节点")

    def _output_debug_info_to_text_editor(self):
        LOG.info("📝 输出处理链调试信息...")

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
        if not valid_chains:
            LOG.warning("   ⚠️ 没有有效的处理链")
            return

        longest_chain = max(valid_chains, key=lambda c: len(c.node_path))
        LOG.info(f"   📏 最长处理链参考: '{longest_chain.object_name}' (节点数: {len(longest_chain.node_path)})")

        text_name = "物体处理链"
        text_block = bpy.data.texts.get(text_name)

        if text_block:
            text_block.clear()
        else:
            text_block = bpy.data.texts.new(text_name)

        debug_lines = []
        debug_lines.append("=" * 80)
        debug_lines.append("TheHerta4 蓝图处理链调试报告")
        debug_lines.append("生成时间: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        debug_lines.append("=" * 80)
        debug_lines.append("")

        self._append_summary_section(debug_lines, valid_chains, longest_chain)

        debug_lines.append("\n" + "=" * 80)
        debug_lines.append("📏 最长处理链参考（执行顺序）")
        debug_lines.append("=" * 80)
        debug_lines.append("")
        self._append_chain_execution_detail(debug_lines, longest_chain, is_reference=True)

        debug_lines.append("\n" + "=" * 80)
        debug_lines.append("🔗 所有处理链详情（按执行顺序）")
        debug_lines.append("=" * 80)
        debug_lines.append("")

        for i, chain in enumerate(self.processing_chains, 1):
            status_icon = "✅" if (chain.is_valid and chain.reached_output) else ("⚠️" if chain.is_valid else "❌")
            debug_lines.append(f"\n{'─' * 80}")
            debug_lines.append(f"[{i}/{len(self.processing_chains)}] {status_icon} 物体: {chain.object_name}")
            debug_lines.append(f"{'─' * 80}")

            self._append_chain_execution_detail(debug_lines, chain, is_reference=False)

        debug_lines.append("\n\n")
        debug_lines.append("=" * 80)
        debug_lines.append("📦 处理链组合并报告")
        debug_lines.append("=" * 80)
        debug_lines.append("")

        for i, group in enumerate(self.chain_groups, 1):
            debug_lines.append(f"\n[{i}/{len(self.chain_groups)}] {group.get_group_description()}")

        debug_lines.append("\n\n")
        debug_lines.append("=" * 80)
        debug_lines.append("💡 说明")
        debug_lines.append("=" * 80)
        debug_lines.append("""
• 处理链表示物体从 Object_Info 到 Result_Output 的完整路径
• 节点按照处理链顺序执行: Object_Info → 中间节点 → Result_Output
• 每个节点的执行顺序由其在处理链中的位置决定
• 只有路径和用户自定义参数完全相同的处理链才会被合并
• 使用 Ctrl+T 在 Blender 文本编辑器中查看此报告
""")

        final_text = "\n".join(debug_lines)
        text_block.write(final_text)

        LOG.info(f"   ✅ 调试信息已写入文本: '{text_name}' ({len(final_text)} 字符)")

    def _append_summary_section(self, debug_lines: List[str], valid_chains: list, longest_chain: ProcessingChain):
        debug_lines.append("📊 统计摘要")
        debug_lines.append("-" * 40)
        debug_lines.append(f"总物体数: {len(self.processing_chains)}")
        debug_lines.append(f"有效处理链: {len(valid_chains)}")
        debug_lines.append(f"无效处理链: {len(self.processing_chains) - len(valid_chains)}")
        debug_lines.append(f"处理链组数: {len(self.chain_groups)}")
        debug_lines.append(f"合并的组数: {sum(1 for g in self.chain_groups if g.object_count > 1)}")
        debug_lines.append(f"最长处理链: '{longest_chain.object_name}' (节点数: {len(longest_chain.node_path)})")
        debug_lines.append(f"后处理节点数: {len(self.postprocess_nodes)}")
        debug_lines.append(f"嵌套蓝图数: {len(self.nested_blueprint_trees)}")

        try:
            from .node_vertex_group_process import SSMTNode_VertexGroupProcess
            debug_lines.append(SSMTNode_VertexGroupProcess.generate_debug_summary(self.processing_chains))
        except ImportError:
            debug_lines.append(f"顶点组处理节点数: {len(self.vertex_group_process_nodes)}")

        debug_lines.append(f"多文件导出节点数: {len(self.multi_file_export_nodes)}")

        try:
            from .node_rename import SSMTNode_Object_Rename
            debug_lines.append(SSMTNode_Object_Rename.generate_debug_summary(self.processing_chains))
        except ImportError:
            pass

        try:
            from .node_swap_processor import DebugOutputGenerator
            registry = getattr(self, '_swap_key_registry', None)
            if registry is not None:
                swap_debug = DebugOutputGenerator.generate_swap_chain_debug(self.processing_chains, registry)
                debug_lines.extend(swap_debug)
        except ImportError:
            pass

        debug_lines.append("")

    def _append_chain_execution_detail(self, debug_lines: List[str], chain: ProcessingChain, is_reference: bool = False):
        debug_lines.append(f"物体名称: {chain.object_name}")
        if chain.original_object_name:
            debug_lines.append(f"原始名称: {chain.original_object_name}")
        debug_lines.append(f"有效性: {'有效' if chain.is_valid else '无效'}")
        debug_lines.append(f"到达输出: {'是' if chain.reached_output else '否'}")
        debug_lines.append(f"节点路径长度: {len(chain.node_path)}")
        debug_lines.append("")

        self._append_node_debug_details(debug_lines, chain)

        debug_lines.append("📋 节点执行顺序:")
        debug_lines.append("-" * 40)

        if chain.node_path:
            for j, (node, sig) in enumerate(zip(chain.node_path, chain.node_param_signatures), 1):
                node_type = node.bl_idname.replace('SSMTNode_', '')
                node_label = node.label or node.name
                node_tree = node.id_data
                tree_name = node_tree.name if node_tree else "未知"
                mute_status = " [已静音]" if node.mute else ""

                debug_lines.append(f"  步骤 {j:>2}: [{node_type}] {node_label}{mute_status}")
                debug_lines.append(f"          蓝图: {tree_name}")
                debug_lines.append(f"          参数: {sig}")

                if is_reference:
                    LOG.info(f"      [{j}] {node_type}: {node_label} (蓝图: {tree_name})")
        else:
            debug_lines.append("  (无节点路径)")

        debug_lines.append("")

    def _append_node_debug_details(self, debug_lines: List[str], chain: ProcessingChain):
        try:
            from .node_swap import ObjectSwapDebugger
            registry = getattr(self, '_swap_key_registry', None)
            swap_lines = ObjectSwapDebugger.generate_chain_detail(chain, registry)
            if swap_lines:
                debug_lines.extend(swap_lines)
                debug_lines.append("")
        except ImportError:
            if chain.swap_node_option_values:
                for swap_name, option_val in chain.swap_node_option_values.items():
                    debug_lines.append(f"🔄 物体切换: {swap_name} → 选项 {option_val + 1} (索引 {option_val})")
                debug_lines.append("")

        if chain.group_stack:
            debug_lines.append(f"📁 分组路径: {' > '.join(chain.group_stack)}")
            debug_lines.append("")

        try:
            from .node_shapekey import SSMTNode_ShapeKey
            if chain.shapekey_params:
                sk_detail = SSMTNode_ShapeKey.generate_debug_detail(chain.shapekey_params, self.keyname_mkey_dict)
                debug_lines.extend(sk_detail)
                debug_lines.append("")
        except ImportError:
            if chain.shapekey_params:
                debug_lines.append("🔑 形态键参数:")
                for sk in chain.shapekey_params:
                    detail = f"   - {sk.key_name}"
                    if sk.initialize_vk_str:
                        detail += f" (VK:{sk.initialize_vk_str})"
                    if sk.comment:
                        detail += f" [{sk.comment}]"
                    debug_lines.append(detail)
                debug_lines.append("")

        try:
            from .node_rename import SSMTNode_Object_Rename
            if chain.rename_history:
                rename_detail = SSMTNode_Object_Rename.generate_debug_detail(chain.rename_history)
                debug_lines.extend(rename_detail)
                debug_lines.append("")
        except ImportError:
            if chain.rename_history:
                debug_lines.append("✏️ 重命名历史:")
                for record in chain.rename_history:
                    debug_lines.append(f"   [{record.get('operation_index', '?')}] '{record.get('old_name', '')}' → '{record.get('new_name', '')}'")
                debug_lines.append("")

        try:
            from .node_vertex_group_process import SSMTNode_VertexGroupProcess
            if chain.vertex_group_process_nodes or chain.vertex_group_mapping_nodes:
                vg_detail = SSMTNode_VertexGroupProcess.generate_debug_detail(chain)
                if vg_detail:
                    debug_lines.extend(vg_detail)
                    debug_lines.append("")
        except ImportError:
            pass

    def _backward_parse_legacy(self, output_node: bpy.types.Node):
        LOG.warning("⚠️ 使用旧版反向解析模式（不推荐）")

        LOG.debug(f"   📊 输出节点连接的节点数量: {len(BlueprintExportHelper.get_connected_nodes(output_node))}")
        self.parse_current_node(output_node, [])

    def parse_current_node(self, current_node:bpy.types.Node, chain_key_list:list[M_Key]):
        for unknown_node in BlueprintExportHelper.get_connected_nodes(current_node):
            self.parse_single_node(unknown_node, chain_key_list)

    def parse_single_node(self, unknown_node:bpy.types.Node, chain_key_list:list[M_Key]):
        if unknown_node.mute:
            return

        if unknown_node.bl_idname == _NODE_TYPE_OBJECT_GROUP:
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_OBJECT_INFO:
            obj_model = DrawCallModel(obj_name=unknown_node.object_name)

            if hasattr(unknown_node, 'original_object_name') and unknown_node.original_object_name:
                obj_model.display_name = unknown_node.original_object_name

            obj_model.work_key_list = copy.deepcopy(chain_key_list)

            self.ordered_draw_obj_data_model_list.append(obj_model)

        elif unknown_node.bl_idname == _NODE_TYPE_SHAPEKEY:
            from .chain_traverser import ChainTraverser
            shapekey_param = ChainTraverser.extract_shapekey_params(unknown_node)
            if shapekey_param:
                chain_key_list.append(shapekey_param)
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_OBJECT_RENAME:
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_VERTEX_GROUP_PROCESS:
            self.vertex_group_process_nodes.append(unknown_node)
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_VERTEX_GROUP_MATCH:
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_VERTEX_GROUP_MAPPING_INPUT:
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_BLUEPRINT_NEST:
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_CROSS_IB:
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_MULTI_FILE_EXPORT:
            self.multi_file_export_nodes.append(unknown_node)
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == _NODE_TYPE_DATA_TYPE:
            self.parse_current_node(unknown_node, chain_key_list)

        elif _is_postprocess_node(unknown_node.bl_idname):
            self.postprocess_nodes.append(unknown_node)
            self.parse_current_node(unknown_node, chain_key_list)

        elif not _is_known_node_type(unknown_node.bl_idname):
            LOG.warning(f"   ⚠️ 未知节点类型（反向解析）: {unknown_node.bl_idname} ({unknown_node.name})，跳过")
            self.parse_current_node(unknown_node, chain_key_list)

        else:
            self.parse_current_node(unknown_node, chain_key_list)


def register():
    pass


def unregister():
    pass
