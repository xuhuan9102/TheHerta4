"""
物体切换节点的 INI 生成器 - 处理 KeySwap 段落和相关配置的生成

该模块负责将物体切换节点转换为 3DMigoto/XXMI 格式的 INI 配置。
生成的配置包括：
- KeySwap 段落：定义快捷键和切换行为
- Constants 段落：声明 swapkey 变量
- Present 段落：初始化参数
"""

from typing import List, Dict, Optional
import bpy

from ..common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ..utils.log_utils import LOG
from .node_swap import SSMTNode_ObjectSwap, SwapKeyConfig
from .node_swap_processor import SwapKeyRegistry, ObjectSwapChainProcessor


class SwapKeyINIGenerator:
    """物体切换节点的 INI 生成器
    
    负责将物体切换节点转换为 INI 格式的配置段落。
    支持生成 KeySwap、Constants、Present 等段落。
    """
    
    @staticmethod
    def collect_all_swap_nodes_from_blueprint(tree: bpy.types.NodeTree) -> List[bpy.types.Node]:
        """从蓝图树中收集所有物体切换节点
        
        遍历蓝图树中的所有节点，筛选出物体切换节点并按名称排序。
        
        Args:
            tree: 蓝图节点树
            
        Returns:
            List[Node]: 所有物体切换节点列表（按 name 排序以保证顺序一致）
        """
        swap_nodes = []
        
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_ObjectSwap':
                swap_nodes.append(node)
        
        # 按名称排序以保证一致的索引分配
        swap_nodes.sort(key=lambda n: n.name)
        
        return swap_nodes
    
    @staticmethod
    def generate_key_swap_sections(
        swap_nodes: List[bpy.types.Node]
    ) -> M_IniSection:
        """生成 [KeySwap_N] 段落
        
        为每个物体切换节点生成一个 KeySwap 段落，包含：
        - 段落标题：[KeySwap_N]
        - 备注：以注释形式显示
        - condition：激活条件
        - key：快捷键
        - type：切换类型
        - $swapkeyN：选项值序列
        
        Args:
            swap_nodes: 所有物体切换节点列表
            
        Returns:
            M_IniSection: 包含所有 KeySwap 段落的 INI 段
        """
        section = M_IniSection(M_SectionType.Key)
        
        for idx, node in enumerate(swap_nodes):
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
            
            if idx > 0:
                section.new_line()
            
            section.append(f"[{config.get_key_swap_section_name()}]")
            
            if config.comment:
                section.append(f"; {config.comment}")
            
            section.append(f"condition = $active0 == 1")
            section.append(f"key = {config.hotkey}")
            section.append(f"type = {config.swap_type}")
            
            option_sequence = ','.join(str(i) for i in range(config.option_count))
            section.append(f"{config.get_swap_key_name()} = {option_sequence},")
        
        if not section.empty():
            LOG.info(f"✓ 生成了 {len(swap_nodes)} 个 KeySwap 段落配置")
        
        return section
    
    @staticmethod
    def generate_constants_section(
        swap_nodes: List[bpy.types.Node],
        existing_constants: Optional[M_IniSection] = None,
    ) -> M_IniSection:
        """生成或更新 [Constants] 段落中的 swapkey 变量声明
        
        在 Constants 段落中添加 swapkey 变量的声明，格式为：
        global persist $swapkeyN = 0
        
        Args:
            swap_nodes: 所有物体切换节点列表
            existing_constants: 现有的 Constants 段落（若有），会在此基础上追加
            
        Returns:
            M_IniSection: 包含 swapkey 变量的 Constants 段落
        """
        if existing_constants is None:
            section = M_IniSection(M_SectionType.Constants)
            section.SectionName = "Constants"
        else:
            section = existing_constants
        
        for idx, node in enumerate(swap_nodes):
            if node.bl_idname != 'SSMTNode_ObjectSwap':
                continue
            
            config = SwapKeyConfig(index=idx)
            
            var_line = f"global persist {config.get_swap_key_name()} = 0"
            already_exists = any(var_line in line for line in section.SectionLineList)
            
            if not already_exists:
                section.append(var_line)
        
        if not section.empty():
            LOG.info(f"✓ 在 [Constants] 中添加了 {len(swap_nodes)} 个 swapkey 变量（global persist）")
        
        return section
    
    @staticmethod
    def generate_present_section(
        swap_nodes: List[bpy.types.Node],
        existing_present: Optional[M_IniSection] = None,
    ) -> M_IniSection:
        """生成或更新 [Present] 段落中的参数初始化
        
        NOTE: $active 参数的初始化应该由导出脚本根据 IB 数量处理，
              而不是在这里根据节点数量处理。
              因此本方法返回空段落。
        
        Args:
            swap_nodes: 所有物体切换节点列表
            existing_present: 现有的 Present 段落（若有）
            
        Returns:
            M_IniSection: 空的 Present 段落
        """
        if existing_present is None:
            section = M_IniSection(M_SectionType.Present)
            section.SectionName = "Present"
        else:
            section = existing_present
        
        # 不在此处生成 $active 初始化
        # $active 应该在具体的 TextureOverride 块中基于 IB 索引设置
        # 由导出脚本（efmi.py 等）根据 DrawIB 数量和索引来处理
        
        if not section.empty():
            LOG.info(f"✓ 在 [Present] 中已处理参数初始化")
        
        return section
    
    @staticmethod
    def inject_activation_to_texture_override(
        ini_builder: M_IniBuilder,
        swap_nodes: List[bpy.types.Node],
    ):
        """在 TextureOverride_* 段落中注入激活参数设定
        
        NOTE: $active 参数的注入应该由导出脚本处理，
              因为 $active 的索引取决于 DrawIB 的索引（由导出脚本确定）。
        
        Args:
            ini_builder: INI 构建器
            swap_nodes: 所有物体切换节点列表
        """
        if not swap_nodes:
            return
        
        # 注意：$active 参数的实际注入应该在导出脚本中完成
        # 因为 $active 的索引与 DrawIB 的索引相关（draw_ib_active_index_dict）
        # 导出脚本中已经处理了这个逻辑：
        #   if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
        #       texture_override_ib_section.append("$active" + str(active_index) + " = 1")
        # 
        # 所以此处不需要额外处理
        
        LOG.info(f"✓ 物体切换激活参数将由导出脚本在 TextureOverride 块中处理")


class SwapKeyINIIntegrator:
    """将物体切换 INI 集成到导出流程
    
    负责在导出过程中检测物体切换节点并生成相应的 INI 配置。
    """
    
    @staticmethod
    def integrate_to_export(
        ini_builder: M_IniBuilder,
        tree: bpy.types.NodeTree,
    ):
        """在导出前检查并添加物体切换相关的 INI 配置
        
        该方法会：
        1. 收集所有物体切换节点
        2. 生成 KeySwap 段落
        3. 更新 Constants 段落
        4. 更新 Present 段落
        5. 注入激活参数到 TextureOverride
        
        Args:
            ini_builder: INI 构建器
            tree: 蓝图节点树
        """
        
        # 收集所有物体切换节点
        swap_nodes = SwapKeyINIGenerator.collect_all_swap_nodes_from_blueprint(tree)
        
        if not swap_nodes:
            LOG.info("未检测到物体切换节点")
            return
        
        LOG.info(f"检测到 {len(swap_nodes)} 个物体切换节点，开始生成配置...")
        
        # 生成 KeySwap 段落
        key_swap_section = SwapKeyINIGenerator.generate_key_swap_sections(swap_nodes)
        if not key_swap_section.empty():
            ini_builder.append_section(key_swap_section)
        
        # 生成或更新 Constants 段落
        # 尝试找到现有的 Constants 段落
        existing_constants = None
        for section in ini_builder.ini_section_list:
            if section.SectionType == M_SectionType.Constants:
                existing_constants = section
                break
        
        constants_section = SwapKeyINIGenerator.generate_constants_section(
            swap_nodes, 
            existing_constants=existing_constants
        )
        
        # 只在没有现有 Constants 段落时添加新段落
        if existing_constants is None and not constants_section.empty():
            ini_builder.append_section(constants_section)
        
        # 生成或更新 Present 段落
        present_section = SwapKeyINIGenerator.generate_present_section(swap_nodes)
        if not present_section.empty():
            ini_builder.append_section(present_section)
        
        # 在 TextureOverride 中注入激活参数
        SwapKeyINIGenerator.inject_activation_to_texture_override(ini_builder, swap_nodes)
        
        LOG.info(f"✓ 物体切换节点配置生成完成 ({len(swap_nodes)} 个节点)")


class SwapKeyDebugINIWriter:
    """用于调试的 INI 内容输出
    
    生成示例 INI 输出，方便开发者了解生成的配置格式。
    """
    
    @staticmethod
    def generate_sample_ini_output(
        swap_nodes: List[bpy.types.Node]
    ) -> str:
        """生成示例 INI 输出（用于调试显示）
        
        生成一个完整的 INI 配置示例，展示物体切换节点的配置格式。
        
        Args:
            swap_nodes: 所有物体切换节点列表
            
        Returns:
            str: 格式化的 INI 样本字符串
        """
        lines = []
        
        lines.append("\n" + "="*80)
        lines.append("物体切换节点 INI 生成示例")
        lines.append("="*80)
        
        lines.append("\n; ========== KeySwap 段落 ==========")
        for idx, node in enumerate(swap_nodes):
            comment = getattr(node, 'comment', '')
            lines.append(f"\n[KeySwap_{idx}]")
            if comment:
                lines.append(f"; {comment}")
            lines.append(f"condition = $active0 == 1")
            lines.append(f"key = {getattr(node, 'hotkey', 'No_Modifiers Numpad3')}")
            lines.append(f"type = {getattr(node, 'swap_type', 'cycle')}")
            option_count = getattr(node, 'input_slot_count', 2)
            option_seq = ','.join(str(i) for i in range(option_count))
            lines.append(f"$swapkey{idx} = {option_seq},")
        
        lines.append("\n\n; ========== [Constants] 中的声明 ==========")
        lines.append("[Constants]")
        lines.append("global $active0")
        for idx in range(len(swap_nodes)):
            lines.append(f"global persist $swapkey{idx} = 0")
        
        lines.append("\n\n; ========== [Present] 中的初始化 ==========")
        lines.append("[Present]")
        lines.append("post $active0 = 0")
        
        lines.append("\n\n; ========== [TextureOverride_XX] 中的激活参数 ==========")
        lines.append("[TextureOverride_4c11c155_288_7068]")
        lines.append("hash = 4c11c155")
        lines.append("$active0 = 1")
        lines.append("; ... 其他配置内容 ...")
        
        lines.append("\n\n; ========== drawindexed 条件示例 ==========")
        lines.append(f"if $swapkey0 == 1")
        lines.append(f"  drawindexed = 7068,0,0")
        lines.append(f"endif")
        
        if len(swap_nodes) > 1:
            lines.append(f"\n; 支持嵌套条件")
            condition_operator = getattr(swap_nodes[0], 'condition_operator', '&&') if swap_nodes else '&&'
            lines.append(f"if $swapkey0 == 1 {condition_operator} $swapkey1 == 1")
            lines.append(f"  drawindexed = 7068,0,0")
            lines.append(f"endif")
        
        lines.append("\n" + "="*80 + "\n")
        
        return "\n".join(lines)


def register():
    pass


def unregister():
    pass

