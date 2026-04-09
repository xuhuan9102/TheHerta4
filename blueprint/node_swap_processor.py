"""
物体切换节点的处理链集成 - 处理蓝图模型中节点的解析和 INI 生成
"""

import bpy
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

from ..utils.log_utils import LOG
from ..common.m_key import M_Key
from .node_swap import SSMTNode_ObjectSwap, SwapKeyConfig


@dataclass
class SwapKeyRegistry:
    """跟踪已分配的 swapkey 索引和对应的节点
    
    用于在蓝图模型解析过程中统一管理所有物体切换节点的索引分配，
    确保每个节点获得唯一的 swapkey 索引。
    
    Attributes:
        next_index: 下一个待分配的索引
        node_swapkey_map: 节点 name -> swapkey 索引的映射字典
        swapkey_nodes: 按序号排列的节点列表
    """
    
    next_index: int = 0  # 下一个待分配的索引
    node_swapkey_map: Dict[str, int] = None  # 节点 name -> swapkey 索引的映射
    swapkey_nodes: List[bpy.types.Node] = None  # 按序号排列的节点列表
    
    def __post_init__(self):
        """初始化后处理，确保字典和列表被正确初始化"""
        if self.node_swapkey_map is None:
            self.node_swapkey_map = {}
        if self.swapkey_nodes is None:
            self.swapkey_nodes = []
    
    def register_node(self, node: bpy.types.Node) -> int:
        """为节点分配一个 swapkey 索引
        
        如果节点已经注册过，则返回之前分配的索引。
        否则分配新索引并记录映射关系。
        
        Args:
            node: 物体切换节点实例
            
        Returns:
            int: 分配的索引（从0开始递增）
        """
        if node.name in self.node_swapkey_map:
            return self.node_swapkey_map[node.name]
        
        index = self.next_index
        self.node_swapkey_map[node.name] = index
        self.swapkey_nodes.append(node)
        self.next_index += 1
        
        return index
    
    def get_total_swap_keys(self) -> int:
        """获取总的 swapkey 数量
        
        Returns:
            int: 已分配的 swapkey 总数
        """
        return self.next_index


class ObjectSwapChainProcessor:
    """物体切换节点的处理链处理器
    
    负责从蓝图处理链中收集物体切换节点，并生成对应的条件参数。
    支持嵌套条件，多个条件之间使用逻辑运算符（&& 或 ||）连接。
    """
    
    @staticmethod
    def collect_swap_nodes_from_chain(
        node_path: List[bpy.types.Node]
    ) -> List[Tuple[int, bpy.types.Node]]:
        """从处理链路径中收集所有物体切换节点
        
        遍历处理链中的所有节点，筛选出物体切换节点并记录其位置。
        
        Args:
            node_path: 处理链中的节点列表
            
        Returns:
            List[Tuple[int, Node]]: (节点在路径中的索引, 节点) 的列表
        """
        swap_nodes = []
        
        for i, node in enumerate(node_path):
            if node.bl_idname == 'SSMTNode_ObjectSwap':
                swap_nodes.append((i, node))
        
        return swap_nodes
    
    @staticmethod
    def build_swap_conditions_for_chain(
        node_path: List[bpy.types.Node],
        registry: SwapKeyRegistry,
        swap_node_option_values: Optional[Dict[str, int]] = None,
        node_index: Optional[int] = None
    ) -> List[M_Key]:
        """为处理链中的物体切换节点生成 M_Key 条件
        
        支持嵌套，每个物体切换节点可以设置自己的逻辑运算符。
        每个物体切换节点会生成一个 M_Key 对象，包含：
        - key_name: $swapkey{index} 格式的变量名
        - tmp_value: 条件比较值（根据物体连接的选项位置决定）
        - comment: 备注信息
        - condition_operator: 该条件与前面条件之间的逻辑运算符
        
        Args:
            node_path: 处理链中的节点列表
            registry: swapkey 分配注册表
            swap_node_option_values: 节点名称 -> 选项索引的映射
            node_index: 如果指定，只处理该索引的节点（用于单个节点）
        
        Returns:
            List[M_Key]: 生成的条件 M_Key 列表
        """
        swap_keys = []
        swap_node_option_values = swap_node_option_values or {}
        
        for i, node in enumerate(node_path):
            if node.bl_idname == 'SSMTNode_ObjectSwap':
                if node_index is not None and i != node_index:
                    continue
                
                swap_index = registry.register_node(node)
                
                hotkey = getattr(node, 'hotkey', '')
                comment = getattr(node, 'comment', f'物体切换_{swap_index}')
                option_count = getattr(node, 'input_slot_count', 1)
                condition_operator = getattr(node, 'condition_operator', '&&')
                
                # 从 swap_node_option_values 获取选项值
                option_value = swap_node_option_values.get(node.name, 0)
                
                m_key = M_Key()
                m_key.key_name = f"$swapkey{swap_index}"
                m_key.initialize_vk_str = hotkey
                m_key.comment = comment
                m_key.tmp_value = option_value  # 使用选项值而不是固定的 1
                m_key.condition_operator = condition_operator
                
                swap_keys.append(m_key)
        
        return swap_keys
    
    @staticmethod
    def generate_swap_key_ini_sections(
        registry: SwapKeyRegistry,
        nodes_list: List[bpy.types.Node]
    ) -> Dict[str, list]:
        """生成物体切换节点所需的INI段落
        
        为每个物体切换节点生成：
        - KeySwap 段落：定义快捷键和切换行为
        - Constants 段落：声明 swapkey 变量
        - Present 段落：初始化参数
        
        Args:
            registry: swapkey 分配注册表
            nodes_list: 节点列表
            
        Returns:
            Dict[str, list]: 键为段落类型，值为行列表
            {
                'KeySwap': [...行...],
                'Constants': [...行...],
                'Present': [...行...],
            }
        """
        result = {
            'KeySwap': [],
            'Constants': [],
            'Present': [],
        }
        
        for idx, node in enumerate(nodes_list):
            if node.bl_idname != 'SSMTNode_ObjectSwap':
                continue
            
            config = SwapKeyConfig(
                node_id=node.name,
                index=idx,
                hotkey=getattr(node, 'hotkey', 'No_Modifiers Numpad3'),
                swap_type=getattr(node, 'swap_type', 'cycle'),
                option_count=getattr(node, 'input_slot_count', 2),
                comment=getattr(node, 'comment', ''),
            )
            
            key_swap_lines = []
            key_swap_lines.append(f"[{config.get_key_swap_section_name()}]")
            
            if config.comment:
                key_swap_lines.append(f"; {config.comment}")
            
            key_swap_lines.append(f"condition = $active0 == 1")
            key_swap_lines.append(f"key = {config.hotkey}")
            key_swap_lines.append(f"type = {config.swap_type}")
            
            option_sequence = ','.join(str(i) for i in range(config.option_count))
            key_swap_lines.append(f"{config.get_swap_key_name()} = {option_sequence},")
            
            result['KeySwap'].extend(key_swap_lines)
            result['KeySwap'].append("")
            
            result['Constants'].append(f"{config.get_swap_key_name()} = 0")
            
            result['Present'].append(f"post $active0 = 0")
        
        return result
    
    @staticmethod
    def add_swap_activation_to_texture_override(
        swap_nodes: List[bpy.types.Node],
        texture_override_lines: List[str]
    ) -> List[str]:
        """在 TextureOverride 块中添加激活参数设定
        
        在每个 [TextureOverride_*] 段落开始后添加 $active 参数设定，
        用于激活对应的物体切换功能。
        
        Args:
            swap_nodes: 所有物体切换节点列表
            texture_override_lines: TextureOverride 的行列表
            
        Returns:
            List[str]: 修改后的行列表，在 TextureOverride 块开始后添加激活参数设定
        """
        if not swap_nodes or not texture_override_lines:
            return texture_override_lines
        
        result = []
        
        # 查找 [TextureOverride_ 段落的开始位置
        for line in texture_override_lines:
            result.append(line)
            
            # 在段落标题下方添加激活参数
            if line.strip().startswith('[TextureOverride_'):
                for idx, node in enumerate(swap_nodes):
                    if node.bl_idname == 'SSMTNode_ObjectSwap':
                        result.append(f"$active{idx} = 1")
        
        return result


class DebugOutputGenerator:
    """生成调试输出
    
    用于在日志中输出物体切换节点的处理链分析信息，
    方便开发者了解节点的分配和处理情况。
    """
    
    @staticmethod
    def generate_swap_chain_debug(
        processing_chains,
        registry: SwapKeyRegistry,
    ) -> List[str]:
        """为所有处理链生成物体切换调试信息
        
        输出内容包括：
        - 总共分配的 swapkey 数量
        - 每个 swapkey 对应的节点信息
        - 处理链中的物体切换节点分布统计
        
        Args:
            processing_chains: 所有处理链
            registry: swapkey 注册表
            
        Returns:
            List[str]: 调试行列表
        """
        lines = []
        lines.append("\n" + "="*80)
        lines.append("物体切换节点处理链分析")
        lines.append("="*80)
        
        if registry.next_index == 0:
            lines.append("未检测到物体切换节点")
            return lines
        
        lines.append(f"\n总共分配了 {registry.next_index} 个 swapkey 变量\n")
        
        for idx, node in enumerate(registry.swapkey_nodes):
            lines.append(f"[swapkey{idx}] 对应节点:")
            lines.append(f"  节点名称: {node.name}")
            lines.append(f"  备注: {getattr(node, 'comment', 'N/A')}")
            lines.append(f"  快捷键: {getattr(node, 'hotkey', 'N/A')}")
            lines.append(f"  切换类型: {getattr(node, 'swap_type', 'N/A')}")
            lines.append(f"  选项数量: {getattr(node, 'input_slot_count', 1)}")
            lines.append("")
        
        swap_count_per_chain = {}
        
        for chain in processing_chains:
            swap_nodes = ObjectSwapChainProcessor.collect_swap_nodes_from_chain(chain.node_path)
            if swap_nodes:
                key = f"深度{len(swap_nodes)}"
                swap_count_per_chain[key] = swap_count_per_chain.get(key, 0) + 1
        
        if swap_count_per_chain:
            lines.append("处理链中的物体切换节点分布:")
            for key, count in sorted(swap_count_per_chain.items()):
                lines.append(f"  {key}: {count} 条链")
        else:
            lines.append("未在任何处理链中检测到物体切换节点")
        
        lines.append("="*80 + "\n")
        
        return lines


# ============= 集成到蓝图模型的接口函数 =============

def integrate_object_swap_to_blueprint_model(blueprint_model):
    """将物体切换节点集成到蓝图模型中
    
    在 BluePrintModel._forward_parse_blueprint() 执行后调用。
    
    该函数会：
    1. 遍历所有处理链，收集物体切换节点
    2. 为每个节点分配唯一的 swapkey 索引
    3. 生成 M_Key 条件参数并添加到处理链中
    4. 设置每个条件的逻辑运算符（&& 或 ||）
    5. 设置每个条件的选项值（根据物体连接的选项位置）
    6. 将所有 swapkey 注册到蓝图模型的字典中
    
    Args:
        blueprint_model: BluePrintModel 实例
    """
    
    registry = SwapKeyRegistry()
    
    for chain in blueprint_model.processing_chains:
        swap_nodes = ObjectSwapChainProcessor.collect_swap_nodes_from_chain(chain.node_path)
        
        if swap_nodes:
            swap_keys = ObjectSwapChainProcessor.build_swap_conditions_for_chain(
                chain.node_path,
                registry,
                swap_node_option_values=chain.swap_node_option_values
            )
            
            if swap_keys:
                if chain.shapekey_params is None:
                    chain.shapekey_params = []
                chain.shapekey_params.extend(swap_keys)
                
                LOG.debug(f"   🔗 添加了 {len(swap_keys)} 个条件")
                for k in swap_keys:
                    LOG.debug(f"      - {k.key_name} == {k.tmp_value} (运算符: {k.condition_operator})")
                
                for m_key in swap_keys:
                    if m_key.key_name not in blueprint_model.keyname_mkey_dict:
                        blueprint_model.keyname_mkey_dict[m_key.key_name] = m_key
    
    blueprint_model._swap_key_registry = registry
    
    if registry.next_index > 0:
        debug_lines = DebugOutputGenerator.generate_swap_chain_debug(
            blueprint_model.processing_chains,
            registry
        )
        LOG.info("\n".join(debug_lines))

