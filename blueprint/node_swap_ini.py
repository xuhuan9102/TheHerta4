from typing import List, Dict, Optional
import bpy

from ..common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ..utils.log_utils import LOG
from .node_swap import SSMTNode_ObjectSwap, SwapKeyConfig
from .node_swap_processor import SwapKeyRegistry, ObjectSwapChainProcessor, _get_node_unique_key


class SwapKeyINIGenerator:

    @staticmethod
    def collect_all_swap_nodes_from_blueprint(tree: bpy.types.NodeTree) -> List[bpy.types.Node]:
        swap_nodes = []
        visited = set()

        def is_connected_to_output(current_tree, node):
            output_node = None
            for n in current_tree.nodes:
                if n.bl_idname == 'SSMTNode_Result_Output':
                    output_node = n
                    break
            if not output_node:
                return False

            visited_check = set()

            def check_reverse(current):
                node_key = _get_node_unique_key(current)
                if node_key in visited_check:
                    return False
                visited_check.add(node_key)
                if current == node:
                    return True
                for input_socket in current.inputs:
                    if not input_socket.is_linked:
                        continue
                    for link in input_socket.links:
                        if check_reverse(link.from_node):
                            return True
                return False

            return check_reverse(output_node)

        def collect_from_tree(current_tree):
            if current_tree.name in visited:
                return
            visited.add(current_tree.name)

            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_ObjectSwap' and not node.mute:
                    if is_connected_to_output(current_tree, node):
                        swap_nodes.append(node)
                elif node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    bp_name = getattr(node, 'blueprint_name', '')
                    if bp_name and bp_name != 'NONE':
                        nested_tree = bpy.data.node_groups.get(bp_name)
                        if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                            collect_from_tree(nested_tree)

        collect_from_tree(tree)
        swap_nodes.sort(key=lambda n: _get_node_unique_key(n))

        return swap_nodes

    @staticmethod
    def _get_node_index(node: bpy.types.Node, registry: Optional[SwapKeyRegistry] = None, fallback_index: int = 0) -> int:
        if registry is not None:
            node_key = _get_node_unique_key(node)
            return registry.node_swapkey_map.get(node_key, fallback_index)
        return fallback_index

    @staticmethod
    def generate_key_swap_sections(
        swap_nodes: List[bpy.types.Node],
        registry: Optional[SwapKeyRegistry] = None,
    ) -> M_IniSection:
        section = M_IniSection(M_SectionType.Key)

        for fallback_idx, node in enumerate(swap_nodes):
            if node.bl_idname != 'SSMTNode_ObjectSwap':
                continue

            idx = SwapKeyINIGenerator._get_node_index(node, registry, fallback_idx)

            config = SwapKeyConfig(
                node_id=node.name,
                index=idx,
                hotkey=getattr(node, 'hotkey', 'No_Modifiers Numpad3'),
                swap_type=getattr(node, 'swap_type', 'cycle'),
                option_count=getattr(node, 'input_slot_count', 2),
                comment=getattr(node, 'comment', ''),
                custom_var_name=getattr(node, 'custom_var_name', ''),
            )

            if fallback_idx > 0:
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
        registry: Optional[SwapKeyRegistry] = None,
    ) -> M_IniSection:
        if existing_constants is None:
            section = M_IniSection(M_SectionType.Constants)
            section.SectionName = "Constants"
        else:
            section = existing_constants

        for fallback_idx, node in enumerate(swap_nodes):
            if node.bl_idname != 'SSMTNode_ObjectSwap':
                continue

            idx = SwapKeyINIGenerator._get_node_index(node, registry, fallback_idx)

            config = SwapKeyConfig(
                index=idx,
                custom_var_name=getattr(node, 'custom_var_name', ''),
            )

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
        if existing_present is None:
            section = M_IniSection(M_SectionType.Present)
            section.SectionName = "Present"
        else:
            section = existing_present

        if not section.empty():
            LOG.info(f"✓ 在 [Present] 中已处理参数初始化")

        return section

    @staticmethod
    def inject_activation_to_texture_override(
        ini_builder: M_IniBuilder,
        swap_nodes: List[bpy.types.Node],
    ):
        if not swap_nodes:
            return

        LOG.info(f"✓ 物体切换激活参数将由导出脚本在 TextureOverride 块中处理")


class SwapKeyINIIntegrator:

    @staticmethod
    def integrate_to_export(
        ini_builder: M_IniBuilder,
        tree: bpy.types.NodeTree,
        registry: Optional[SwapKeyRegistry] = None,
    ):
        if registry is not None:
            swap_nodes = list(registry.swapkey_nodes)
        else:
            swap_nodes = SwapKeyINIGenerator.collect_all_swap_nodes_from_blueprint(tree)

        if not swap_nodes:
            LOG.info("未检测到物体切换节点")
            return

        LOG.info(f"检测到 {len(swap_nodes)} 个物体切换节点，开始生成配置...")

        key_swap_section = SwapKeyINIGenerator.generate_key_swap_sections(swap_nodes, registry)
        if not key_swap_section.empty():
            ini_builder.append_section(key_swap_section)

        existing_constants = None
        for section in ini_builder.ini_section_list:
            if section.SectionType == M_SectionType.Constants:
                existing_constants = section
                break

        constants_section = SwapKeyINIGenerator.generate_constants_section(
            swap_nodes,
            existing_constants=existing_constants,
            registry=registry,
        )

        if existing_constants is None and not constants_section.empty():
            ini_builder.append_section(constants_section)

        present_section = SwapKeyINIGenerator.generate_present_section(swap_nodes)
        if not present_section.empty():
            ini_builder.append_section(present_section)

        SwapKeyINIGenerator.inject_activation_to_texture_override(ini_builder, swap_nodes)

        LOG.info(f"✓ 物体切换节点配置生成完成 ({len(swap_nodes)} 个节点)")


class SwapKeyDebugINIWriter:

    @staticmethod
    def generate_sample_ini_output(
        swap_nodes: List[bpy.types.Node],
        registry: Optional[SwapKeyRegistry] = None,
    ) -> str:
        lines = []

        lines.append("\n" + "="*80)
        lines.append("物体切换节点 INI 生成示例")
        lines.append("="*80)

        lines.append("\n; ========== KeySwap 段落 ==========")
        for fallback_idx, node in enumerate(swap_nodes):
            idx = SwapKeyINIGenerator._get_node_index(node, registry, fallback_idx)
            comment = getattr(node, 'comment', '')
            custom_var_name = getattr(node, 'custom_var_name', '')
            var_name = f"${custom_var_name}" if custom_var_name else f"$swapkey{idx}"
            lines.append(f"\n[KeySwap_{idx}]")
            if comment:
                lines.append(f"; {comment}")
            lines.append(f"condition = $active0 == 1")
            lines.append(f"key = {getattr(node, 'hotkey', 'No_Modifiers Numpad3')}")
            lines.append(f"type = {getattr(node, 'swap_type', 'cycle')}")
            option_count = getattr(node, 'input_slot_count', 2)
            option_seq = ','.join(str(i) for i in range(option_count))
            lines.append(f"{var_name} = {option_seq},")

        lines.append("\n\n; ========== [Constants] 中的声明 ==========")
        lines.append("[Constants]")
        lines.append("global $active0")
        for fallback_idx, node in enumerate(swap_nodes):
            idx = SwapKeyINIGenerator._get_node_index(node, registry, fallback_idx)
            custom_var_name = getattr(node, 'custom_var_name', '')
            var_name = f"${custom_var_name}" if custom_var_name else f"$swapkey{idx}"
            lines.append(f"global persist {var_name} = 0")

        lines.append("\n\n; ========== [Present] 中的初始化 ==========")
        lines.append("[Present]")
        lines.append("post $active0 = 0")

        lines.append("\n\n; ========== [TextureOverride_XX] 中的激活参数 ==========")
        lines.append("[TextureOverride_4c11c155_288_7068]")
        lines.append("hash = 4c11c155")
        lines.append("$active0 = 1")
        lines.append("; ... 其他配置内容 ...")

        lines.append("\n\n; ========== drawindexed 条件示例 ==========")
        if swap_nodes:
            first_idx = SwapKeyINIGenerator._get_node_index(swap_nodes[0], registry, 0)
            first_custom = getattr(swap_nodes[0], 'custom_var_name', '')
            first_var = f"${first_custom}" if first_custom else f"$swapkey{first_idx}"
            lines.append(f"if {first_var} == 1")
        else:
            lines.append(f"if $swapkey0 == 1")
        lines.append(f"  drawindexed = 7068,0,0")
        lines.append(f"endif")

        if len(swap_nodes) > 1:
            lines.append(f"\n; 支持嵌套条件")
            condition_operator = getattr(swap_nodes[0], 'condition_operator', '&&') if swap_nodes else '&&'
            first_idx = SwapKeyINIGenerator._get_node_index(swap_nodes[0], registry, 0)
            first_custom = getattr(swap_nodes[0], 'custom_var_name', '')
            first_var = f"${first_custom}" if first_custom else f"$swapkey{first_idx}"
            second_idx = SwapKeyINIGenerator._get_node_index(swap_nodes[1], registry, 1)
            second_custom = getattr(swap_nodes[1], 'custom_var_name', '') if len(swap_nodes) > 1 else ''
            second_var = f"${second_custom}" if second_custom else f"$swapkey{second_idx}"
            lines.append(f"if {first_var} == 1 {condition_operator} {second_var} == 1")
            lines.append(f"  drawindexed = 7068,0,0")
            lines.append(f"endif")

        lines.append("\n" + "="*80 + "\n")

        return "\n".join(lines)


def register():
    pass


def unregister():
    pass
