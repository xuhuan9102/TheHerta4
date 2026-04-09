import math
import bpy
import copy
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName

from ..utils.obj_utils import ObjUtils
from ..utils.log_utils import LOG
from ..utils.collection_utils import CollectionUtils, CollectionColor
from ..utils.tips_utils import TipUtils

from ..common.m_key import M_Key
from ..common.d3d11_gametype import D3D11GameType
from ..common.draw_call_model import DrawCallModel
from ..common.global_key_count_helper import GlobalKeyCountHelper
from .export_helper import BlueprintExportHelper

from .node_obj import SSMTNode_Object_Group, SSMTNode_Object_Info, SSMTNode_Result_Output


@dataclass
class ProcessingChain:
    """
    处理链 - 表示一个物体从源头到输出的完整路径
    
    正向解析的核心数据结构，记录物体经过的所有节点和处理参数
    """
    
    object_name: str = ""
    original_object_name: str = ""  # 记录原始名称（用于调试）
    source_node: Optional[bpy.types.Node] = None  # Object_Info 节点
    
    node_path: List[bpy.types.Node] = field(default_factory=list)  # 经过的节点序列（不含源节点）
    node_param_signatures: List[str] = field(default_factory=list)  # 每个节点的完整参数签名
    
    shapekey_params: List[M_Key] = field(default_factory=list)  # 收集的形态键参数
    group_stack: List[str] = field(default_factory=list)  # 经过的 Group 名称栈（用于调试显示）
    
    condition_operator: str = " && "  # 条件运算符（用于控制多个条件间的逻辑关系）
    
    rename_history: List[dict] = field(default_factory=list)  # 名称修改历史记录
    
    # 记录每个 ObjectSwap 节点的选项值（节点名称 -> 选项索引）
    swap_node_option_values: Dict[str, int] = field(default_factory=dict)
    
    reached_output: bool = False  # 是否成功到达输出节点
    is_valid: bool = True  # 链是否有效（未遇到静音/断开等）
    
    @staticmethod
    def extract_node_signature(node: bpy.types.Node) -> str:
        """
        提取节点的用户可自定义参数签名
        
        只包含用户在节点 UI 上可以手动设置的参数，
        用于精确判断两条处理链是否具有相同的用户配置
        
        注意: Result_Output 节点不参与签名计算，
        因为蓝图内只有一个 Output 节点，所有处理链共享相同配置。
        
        不包括:
        - 自动解析/生成的属性（如从 object_name 解析出的 draw_ib 等）
        - 内部使用的属性（如 object_id）
        - 纯标识性属性（如 name/label）
        - Result_Output 节点（全局唯一，不区分）
        
        Args:
            node: 蓝图节点
            
        Returns:
            str: 节点的用户参数签名，格式为 "NodeType[user_param1=val1,user_param2=val2,...]"
        """
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
        
        else:
            return f"{node_type}[]"
    
    def get_chain_hash(self) -> str:
        """
        生成处理链的唯一标识符（基于完整路径+参数）
        
        用于识别完全相同的处理链（路径和所有参数都一致），以便安全合并
        
        Returns:
            str: 基于节点路径和参数签名的唯一哈希字符串
        """
        if not self.node_path:
            return f"SINGLE:{self.object_name}"
        
        signature_parts = []
        for i, (node, sig) in enumerate(zip(self.node_path, self.node_param_signatures)):
            signature_parts.append(f"[{i}]{sig}")
        
        path_with_params = "|".join(signature_parts)
        return f"CHAIN:{path_with_params}"
    
    def get_simple_hash(self) -> str:
        """
        生成简化版哈希（仅基于路径，不含参数）
        
        用于调试显示和快速分组预览
        
        Returns:
            str: 仅包含节点类型的简化哈希
        """
        if not self.node_path:
            return f"SINGLE_SIMPLE:{self.object_name}"
        
        path_types = [node.bl_idname.replace('SSMTNode_', '') for node in self.node_path]
        return f"CHAIN_SIMPLE:{'->'.join(path_types)}"
    
    def get_chain_description(self) -> str:
        """
        生成人类可读的处理链描述（含详细参数信息）
        
        Returns:
            str: 格式化的处理链描述文本
        """
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
    
    def to_draw_call_model(self) -> DrawCallModel:
        """
        将处理链转换为 DrawCallModel（兼容现有导出流程）
        
        Returns:
            DrawCallModel: 包含所有收集信息的模型对象
        """
        obj_model = DrawCallModel(obj_name=self.object_name)
        
        # 如果对象被重命名过，设置原始名称用于获取 Blender 对象
        if self.original_object_name:
            obj_model.source_obj_name = self.original_object_name
        
        # 每个 M_Key 都有自己的 condition_operator，不再需要单独设置
        obj_model.work_key_list = copy.deepcopy(self.shapekey_params)
        
        return obj_model


@dataclass 
class ChainGroup:
    """
    处理链组 - 合并了相同路径的多条处理链
    
    当多个物体的处理链完全相同时，它们会被合并到一个 ChainGroup 中
    """
    
    chain_hash: str  # 组的唯一标识（来自 ProcessingChain.get_chain_hash()）
    
    chains: List[ProcessingChain] = field(default_factory=list)  # 组内的所有处理链
    
    representative_chain: Optional[ProcessingChain] = None  # 代表性处理链（用于显示）
    
    @property
    def object_count(self) -> int:
        """组内物体数量"""
        return len(self.chains)
    
    @property
    def object_names(self) -> List[str]:
        """组内所有物体名称"""
        return [chain.object_name for chain in self.chains]
    
    def get_group_description(self) -> str:
        """
        生成组的描述信息
        
        Returns:
            str: 格式化的组描述
        """
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

    FORWARD_PARSE_MODE = True  # 标识使用正向解析模式
    
    def __init__(self, tree=None, context=None):
        self.keyname_mkey_dict: Dict[str, M_Key] = {} 

        self.ordered_draw_obj_data_model_list: List[DrawCallModel] = [] 
        
        self.processing_chains: List[ProcessingChain] = []  # 所有处理链
        self.chain_groups: List[ChainGroup] = []  # 合并后的处理链组
        
        tree = tree or BlueprintExportHelper.get_current_blueprint_tree(context=context)
        if not tree:
            raise ValueError("未找到当前蓝图树，请先打开正确的蓝图编辑器")

        # 保存蓝图树引用，供后续 INI 生成使用
        self._tree = tree

        LOG.debug(f"   🌳 当前蓝图树: {tree.name if hasattr(tree, 'name') else '未命名'}")
        
        output_node = BlueprintExportHelper.get_node_from_bl_idname(tree, SSMTNode_Result_Output.bl_idname)
        if not output_node:
            raise ValueError("当前蓝图缺少 Generate Mod 输出节点")

        LOG.info("🔄 启动正向解析模式")
        
        if self.FORWARD_PARSE_MODE:
            self._forward_parse_blueprint(tree, output_node)
        else:
            self._backward_parse_legacy(output_node)

    def _forward_parse_blueprint(self, tree: bpy.types.NodeTree, output_node: bpy.types.Node):
        """
        正向解析蓝图（新算法）
        
        从 Object_Info 节点出发，沿着输出连接向前推进到 Result_Output
        为每个物体构建完整的处理链，然后合并相同路径的链
        
        Args:
            tree: 蓝图树对象
            output_node: 输出节点（Result_Output）
        """
        LOG.info("🔄 启动正向解析模式")
        
        object_info_nodes = BlueprintExportHelper.get_nodes_from_bl_idname(tree, SSMTNode_Object_Info.bl_idname)
        LOG.info(f"   找到 {len(object_info_nodes)} 个 Object_Info 节点")
        
        visited_chains: Set[str] = set()
        
        for obj_node in object_info_nodes:
            if not obj_node.object_name:
                LOG.warning(f"   ⚠️ Object_Info 节点 '{obj_node.name}' 缺少对象名称，跳过")
                continue
            
            chain = ProcessingChain(
                object_name=obj_node.object_name,
                source_node=obj_node
            )
            
            self._traverse_forward(chain, obj_node, set())
            
            chain_hash = chain.get_chain_hash()
            
            if chain_hash in visited_chains:
                LOG.debug(f"   📋 重复处理链检测到: {obj_node.object_name}")
            
            visited_chains.add(chain_hash)
            self.processing_chains.append(chain)
        
        LOG.info(f"   ✅ 正向解析完成，共构建 {len(self.processing_chains)} 条处理链")
        
        self._merge_processing_chains()
        
        # 先集成物体切换节点条件，然后再构建 DrawCallModel
        self._integrate_object_swap_nodes()
        
        self._build_draw_call_models_from_chains()
        
        self._output_debug_info_to_text_editor()

    def _traverse_forward(
        self, 
        chain: ProcessingChain, 
        current_node: bpy.types.Node, 
        visited_nodes: Set[str],
        current_group_name: str = ""
    ):
        """
        正向遍历 - 从当前节点向前推进
        
        沿着节点的输出连接向前遍历，收集经过的所有节点和参数
        
        Args:
            chain: 当前正在构建的处理链
            current_node: 当前遍历到的节点
            visited_nodes: 已访问节点集合（防止循环）
            current_group_name: 当前所在的 Group 名称
        """
        node_id = f"{current_node.bl_idname}:{current_node.name}"
        
        if node_id in visited_nodes:
            LOG.warning(f"   ⚠️ 检测到循环引用，节点: {current_node.name}")
            chain.is_valid = False
            return
        
        if current_node.mute:
            LOG.debug(f"   🔇 节点已静音，跳过: {current_node.name}")
            visited_nodes_copy = visited_nodes | {node_id}
            for output_socket in current_node.outputs:
                if output_socket.is_linked:
                    for link in output_socket.links:
                        self._traverse_forward(chain, link.to_node, visited_nodes_copy, current_group_name)
            return
        
        visited_nodes_copy = visited_nodes | {node_id}
        
        node_type = current_node.bl_idname
        
        if node_type == SSMTNode_Object_Info.bl_idname:
            pass
            
        elif node_type == SSMTNode_Object_Group.bl_idname:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))
            chain.group_stack.append(current_node.name or current_node.label or "Group")
            
            LOG.debug(f"   ➡️ 经过 Group 节点: {current_node.name or current_node.label}")
        
        elif hasattr(current_node, 'bl_idname') and 'ShapeKey' in current_node.bl_idname:
            shapekey_param = self._extract_shapekey_params(current_node)
            if shapekey_param:
                chain.shapekey_params.append(shapekey_param)
                chain.node_path.append(current_node)
                chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))
                
                LOG.debug(f"   🔑 收集形态键参数: {shapekey_param.key_name}")
        
        elif current_node.bl_idname == 'SSMTNode_Object_Rename':
            try:
                from .node_rename import SSMTNode_Object_Rename

                old_name = chain.object_name
                new_name, was_modified, history, signature = SSMTNode_Object_Rename.apply_to_object_name(
                    chain.object_name,
                    current_node
                )

                if was_modified:
                    if not chain.original_object_name:
                        chain.original_object_name = old_name

                    chain.object_name = new_name

                    for record in history:
                        operation = {
                            'operation_index': len(chain.rename_history) + 1,
                            **record
                        }
                        chain.rename_history.append(operation)

                    LOG.debug(f"   ✏️ 名称修改: '{old_name}' → '{new_name}' ({len(history)}条规则生效)")
                else:
                    LOG.debug(f"   ✏️ Rename 节点未修改名称: {old_name}")

                chain.node_path.append(current_node)
                chain.node_param_signatures.append(signature)
            except ImportError:
                LOG.warning(f"   ⚠️ Rename 节点模块不可用，跳过: {current_node.name}")
                chain.node_path.append(current_node)
                chain.node_param_signatures.append("Object_Rename[unavailable]")
        
        elif node_type == SSMTNode_Result_Output.bl_idname:
            chain.reached_output = True
            
            LOG.debug(f"   🎯 到达输出节点: {current_node.label}")
            return
        
        else:
            chain.node_path.append(current_node)
            chain.node_param_signatures.append(ProcessingChain.extract_node_signature(current_node))
            LOG.debug(f"   📍 经过其他节点: {current_node.bl_idname}")

        output_connections = self._get_forward_connections_with_socket_index(current_node)
        
        if not output_connections:
            if node_type != SSMTNode_Result_Output.bl_idname:
                LOG.debug(f"   🔚 节点无输出连接，终止遍历: {current_node.name}")
            return

        for next_node, socket_index in output_connections:
            # 检查下一个节点是否是 ObjectSwap，如果是，记录选项值
            if next_node.bl_idname == 'SSMTNode_ObjectSwap':
                chain.swap_node_option_values[next_node.name] = socket_index
                LOG.debug(f"   🔄 ObjectSwap 节点 '{next_node.name}' 选项值: {socket_index}")
            
            self._traverse_forward(chain, next_node, visited_nodes_copy, current_group_name)

    def _get_forward_connections_with_socket_index(self, node: bpy.types.Node) -> List[Tuple[bpy.types.Node, int]]:
        """获取节点的正向连接（输出方向），包含目标 socket 索引
        
        从当前节点的输出 socket 找到连接的下游节点，并返回目标 socket 的索引。
        对于 ObjectSwap 节点，socket 索引表示选项值。
        
        Args:
            node: 当前节点
            
        Returns:
            List[Tuple[Node, int]]: (下游节点, 目标 socket 索引) 的列表
        """
        connected_nodes = []
        
        for output_socket in node.outputs:
            if output_socket.is_linked:
                for link in output_socket.links:
                    to_node = link.to_node
                    to_socket = link.to_socket
                    if to_node and to_node != node:
                        # 获取目标 socket 的索引
                        socket_index = 0
                        for idx, inp in enumerate(to_node.inputs):
                            if inp == to_socket:
                                socket_index = idx
                                break
                        connected_nodes.append((to_node, socket_index))
        
        return connected_nodes

    def _extract_shapekey_params(self, shapekey_node: bpy.types.Node) -> Optional[M_Key]:
        """
        从 ShapeKey 节点提取参数
        
        Args:
            shapekey_node: ShapeKey 节点
            
        Returns:
            Optional[M_Key]: 提取的 M_Key 对象，如果节点无效则返回 None
        """
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

    def _merge_processing_chains(self):
        """
        合并相同路径的处理链
        
        将具有相同 chain_hash 的处理链合并到同一个 ChainGroup 中
        这样可以减少重复计算，优化导出性能
        """
        LOG.info("🔗 开始合并处理链...")
        
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
        
        LOG.info(f"   ✅ 合并完成: {total_objects} 个物体 → {len(self.chain_groups)} 个处理链组 (其中 {merged_count} 个组合并)")
        
        for i, group in enumerate(self.chain_groups):
            if group.object_count > 1:
                LOG.info(f"      组 {i+1}: {', '.join(group.object_names)} ({group.object_count}个物体共享同一路径)")

    def _build_draw_call_models_from_chains(self):
        """
        从处理链构建 DrawCallModel 列表
        
        将所有有效的处理链转换为 DrawCallModel，
        保持与现有导出流程的兼容性
        """
        LOG.info("🏗️ 构建 DrawCallModel 列表...")
        
        self.ordered_draw_obj_data_model_list.clear()
        
        valid_chain_count = 0
        invalid_chain_count = 0
        
        for chain in self.processing_chains:
            if not chain.is_valid:
                invalid_chain_count += 1
                LOG.warning(f"   ⚠️ 无效处理链跳过: {chain.object_name}")
                continue
            
            if not chain.reached_output:
                invalid_chain_count += 1
                LOG.warning(f"   ⚠️ 未到达输出的处理链跳过: {chain.object_name}")
                continue
            
            draw_call_model = chain.to_draw_call_model()
            self.ordered_draw_obj_data_model_list.append(draw_call_model)
            valid_chain_count += 1
        
        LOG.info(f"   ✅ 构建完成: {valid_chain_count} 个有效 / {invalid_chain_count} 个无效")

    def _integrate_object_swap_nodes(self):
        """
        集成物体切换节点到处理链中
        
        调用物体切换节点的处理器，将节点条件添加到处理链中
        """
        try:
            from .node_swap_processor import integrate_object_swap_to_blueprint_model
            
            integrate_object_swap_to_blueprint_model(self)
            LOG.info("✓ 物体切换节点集成完成")
        except ImportError:
            LOG.debug("⊘ 物体切换节点模块未找到（可选功能）")
        except Exception as e:
            LOG.warning(f"⚠️ 物体切换节点集成遇到错误: {e}")

    def _output_debug_info_to_text_editor(self):
        """
        输出调试信息到 Blender 内置文本编辑器
        
        在 Blender 中创建或更新名为 "物体处理链" 的文本块，
        显示每个物体的完整处理链信息
        """
        LOG.info("📝 输出处理链调试信息...")
        
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
        
        debug_lines.append("📊 统计摘要")
        debug_lines.append("-" * 40)
        debug_lines.append(f"总物体数: {len(self.processing_chains)}")
        debug_lines.append(f"有效处理链: {sum(1 for c in self.processing_chains if c.is_valid and c.reached_output)}")
        debug_lines.append(f"无效处理链: {sum(1 for c in self.processing_chains if not c.is_valid or not c.reached_output)}")
        debug_lines.append(f"处理链组数: {len(self.chain_groups)}")
        debug_lines.append(f"合并的组数: {sum(1 for g in self.chain_groups if g.object_count > 1)}")
        
        try:
            from .node_rename import SSMTNode_Object_Rename
            debug_lines.append(SSMTNode_Object_Rename.generate_debug_summary(self.processing_chains))
        except ImportError:
            pass
        debug_lines.append("")
        
        debug_lines.append("\n🔗 处理链详情（按物体列出）")
        debug_lines.append("=" * 80)
        debug_lines.append("")
        
        for i, chain in enumerate(self.processing_chains, 1):
            status_icon = "✅" if (chain.is_valid and chain.reached_output) else ("⚠️" if chain.is_valid else "❌")
            
            debug_lines.append(f"\n{'─' * 80}")
            debug_lines.append(f"[{i}/{len(self.processing_chains)}] {status_icon} 物体: {chain.object_name}")
            debug_lines.append(f"{'─' * 80}")
            
            debug_lines.append(f"源节点: {getattr(chain.source_node, 'name', '未知') if chain.source_node else '未知'}")
            debug_lines.append(f"原始名称: {chain.original_object_name or chain.object_name}")
            debug_lines.append(f"当前名称: {chain.object_name}")
            debug_lines.append(f"有效性: {'有效' if chain.is_valid else '无效'}")
            debug_lines.append(f"是否到达输出: {'是' if chain.reached_output else '否'}")
            debug_lines.append(f"经过节点数: {len(chain.node_path)}")
            
            if chain.group_stack:
                debug_lines.append(f"分组路径: {' > '.join(chain.group_stack)}")
            
            if chain.shapekey_params:
                from .node_shapekey import SSMTNode_ShapeKey
                debug_lines.extend(SSMTNode_ShapeKey.generate_debug_detail(chain.shapekey_params, self.keyname_mkey_dict))
            
            if chain.rename_history:
                try:
                    from .node_rename import SSMTNode_Object_Rename
                    debug_lines.extend(SSMTNode_Object_Rename.generate_debug_detail(chain.rename_history))
                except ImportError:
                    pass
            
            if chain.node_path:
                debug_lines.append(f"\n节点路径 (含完整参数):")
                for j, (node, sig) in enumerate(zip(chain.node_path, chain.node_param_signatures), 1):
                    node_type = node.bl_idname.replace('SSMTNode_', '')
                    node_label = node.label or node.name
                    mute_status = " [MUTED]" if node.mute else ""
                    debug_lines.append(f"  {j:>2}. [{node_type}] {node_label}{mute_status}")
                    debug_lines.append(f"      参数: {sig}")
            
            simple_hash = chain.get_simple_hash()
            full_hash = chain.get_chain_hash()
            
            debug_lines.append(f"\n🔑 哈希标识:")
            debug_lines.append(f"   简化哈希 (仅路径): {simple_hash[:80]}{'...' if len(simple_hash) > 80 else ''}")
            debug_lines.append(f"   完整哈希 (含参数): {full_hash[:80]}{'...' if len(full_hash) > 80 else ''}")
            
            chain_hash = chain.get_chain_hash()
            matching_groups = [g for g in self.chain_groups if g.chain_hash == chain_hash]
            if matching_groups:
                group = matching_groups[0]
                if group.object_count > 1:
                    debug_lines.append(f"\n📦 属于合并组 (共享此路径的 {group.object_count} 个物体):")
                    for other_obj in group.object_names:
                        if other_obj != chain.object_name:
                            debug_lines.append(f"     • {other_obj}")
        
        debug_lines.append("\n\n")
        debug_lines.append("=" * 80)
        debug_lines.append("📦 处理链组合并报告")
        debug_lines.append("=" * 80)
        debug_lines.append("")
        
        for i, group in enumerate(self.chain_groups, 1):
            debug_lines.append(f"\n[{i}/{len(self.chain_groups)}] {group.get_group_description()}")
        
        debug_lines.append("\n\n")
        
        simple_hash_groups = {}
        for chain in self.processing_chains:
            simple_hash = chain.get_simple_hash()
            if simple_hash not in simple_hash_groups:
                simple_hash_groups[simple_hash] = []
            simple_hash_groups[simple_hash].append(chain)
        
        potential_merges = [chains for chains in simple_hash_groups.values() if len(chains) > 1]
        
        if potential_merges:
            debug_lines.append("=" * 80)
            debug_lines.append("⚠️ 路径相同但参数不同的处理链（未合并）")
            debug_lines.append("=" * 80)
            debug_lines.append("")
            debug_lines.append("以下物体的节点路径相同，但由于某些节点的参数不同，无法安全合并:")
            debug_lines.append("这确保了每个物体都能获得正确的导出配置。\n")
            
            for chain_group in potential_merges:
                debug_lines.append(f"{'─' * 80}")
                debug_lines.append(f"路径类型: {chain_group[0].get_simple_hash()}")
                debug_lines.append(f"涉及物体 ({len(chain_group)}个): {', '.join([c.object_name for c in chain_group])}")
                
                unique_full_hashes = set(chain.get_chain_hash() for c in chain_group)
                
                if len(unique_full_hashes) == 1:
                    debug_lines.append("状态: ✅ 实际已合并（参数完全相同）")
                else:
                    debug_lines.append(f"状态: ❌ 未合并（存在 {len(unique_full_hashes)} 种不同的参数配置）")
                    
                    debug_lines.append("\n参数差异详情:")
                    for idx, chain in enumerate(chain_group):
                        debug_lines.append(f"\n  物体 [{idx+1}]: {chain.object_name}")
                        for j, sig in enumerate(chain.node_param_signatures, 1):
                            debug_lines.append(f"    节点{j}: {sig}")
                
                debug_lines.append("")
        
        debug_lines.append("\n\n")
        
        for i, group in enumerate(self.chain_groups, 1):
            debug_lines.append(f"\n[{i}/{len(self.chain_groups)}] {group.get_group_description()}")
        
        debug_lines.append("\n\n")
        debug_lines.append("=" * 80)
        debug_lines.append("💡 说明")
        debug_lines.append("=" * 80)
        debug_lines.append("""
• 处理链表示物体从 Object_Info 到 Result_Output 的完整路径
• 只有路径和用户自定义参数完全相同的处理链才会被合并，以确保导出配置的正确性
• 合并条件: 节点类型序列 + 每个节点的用户可设置参数都一致（不含 Result_Output）
• 用户可设置参数包括:
  - Object_Info: object_name (选择的对象名称)
  - ShapeKey: name (形态键名), key (快捷键), comment (注释)
  - Rename Object: 多规则列表 (每条规则含 search, replace) + 全局反转映射开关
    • search: 搜索字符串
    • replace: 替换字符串
    • reverse_mapping: 全局反转映射（所有规则执行完后按反向顺序再执行一遍）
  - Group: 无用户参数（纯路由节点）
  - Result_Output: 不参与哈希计算（蓝图内只有一个，所有链共享相同配置）
• 不包含自动生成的属性:
  - Object_Info 的 draw_ib, index_count, first_index, alias 等从名称自动解析的字段
  - 节点的 name/label 标识符
  - 内部使用的 object_id 等
• 无效链通常由以下原因导致:
  - 节点被静音 (mute=True)
  - 连接断开或不存在
  - 存在循环引用
• 使用 Ctrl+T 在 Blender 文本编辑器中查看此报告
""")
        
        final_text = "\n".join(debug_lines)
        text_block.write(final_text)
        
        LOG.info(f"   ✅ 调试信息已写入文本: '{text_name}' ({len(final_text)} 字符)")

    def _backward_parse_legacy(self, output_node: bpy.types.Node):
        """
        反向解析（旧算法）- 保留用于兼容性和回退
        
        从 Result_Output 回溯到 Object_Info 的传统解析方式
        
        Args:
            output_node: 输出节点
        """
        LOG.warning("⚠️ 使用旧版反向解析模式（不推荐）")
        
        LOG.debug(f"   📊 输出节点连接的节点数量: {len(BlueprintExportHelper.get_connected_nodes(output_node))}")
        self.parse_current_node(output_node, [])

    def parse_current_node(self, current_node:bpy.types.Node, chain_key_list:list[M_Key]):
        for unknown_node in BlueprintExportHelper.get_connected_nodes(current_node):
            self.parse_single_node(unknown_node, chain_key_list)

    def parse_single_node(self, unknown_node:bpy.types.Node, chain_key_list:list[M_Key]):
        '''
        这个是递归方法
        解析当前节点，获取其连接的所有节点的信息,分类进行解析
        '''
        
        if unknown_node.mute:
            return

        if unknown_node.bl_idname == SSMTNode_Object_Group.bl_idname:
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == SSMTNode_Object_Info.bl_idname:
            obj_model = DrawCallModel(obj_name=unknown_node.object_name)
            
            if hasattr(unknown_node, 'original_object_name') and unknown_node.original_object_name:
                obj_model.display_name = unknown_node.original_object_name

            obj_model.work_key_list = copy.deepcopy(chain_key_list)
            
            self.ordered_draw_obj_data_model_list.append(obj_model)
