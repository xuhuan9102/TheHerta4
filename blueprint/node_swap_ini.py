from typing import List, Optional

import bpy

from ..common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ..utils.log_utils import LOG
from .node_swap import SwapKeyConfig
from .node_swap_processor import SwapKeyRegistry, _get_node_unique_key
from .variable_registry import ensure_object_swap_variable_name, get_node_variable_name


class SwapKeyINIGenerator:
    @staticmethod
    def collect_all_swap_nodes_from_blueprint(tree: bpy.types.NodeTree) -> List[bpy.types.Node]:
        swap_nodes = []
        visited = set()

        def is_connected_to_output(current_tree, node):
            output_node = None
            for candidate in current_tree.nodes:
                if candidate.bl_idname in {"SSMTNode_Result_Output", "SSMTNode_Result_Output_NTMIModImp", "SSMTNode_VeloExportBridge"}:
                    output_node = candidate
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
                if node.bl_idname == "SSMTNode_ObjectSwap" and not node.mute:
                    if is_connected_to_output(current_tree, node):
                        ensure_object_swap_variable_name(node)
                        swap_nodes.append(node)
                elif node.bl_idname == "SSMTNode_Blueprint_Nest" and not node.mute:
                    bp_name = getattr(node, "blueprint_name", "")
                    if bp_name and bp_name != "NONE":
                        nested_tree = bpy.data.node_groups.get(bp_name)
                        if nested_tree and getattr(nested_tree, "bl_idname", "") == "SSMTBlueprintTreeType":
                            collect_from_tree(nested_tree)

        collect_from_tree(tree)
        swap_nodes.sort(key=lambda item: _get_node_unique_key(item))
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
            if node.bl_idname != "SSMTNode_ObjectSwap":
                continue

            idx = SwapKeyINIGenerator._get_node_index(node, registry, fallback_idx)
            config = SwapKeyConfig(
                node_id=node.name,
                index=idx,
                hotkey=getattr(node, "hotkey", "No_Modifiers Numpad3"),
                swap_type=getattr(node, "swap_type", "cycle"),
                option_count=getattr(node, "input_slot_count", 2),
                comment=getattr(node, "comment", ""),
                custom_var_name=str(getattr(node, "custom_var_name", "") or "").strip().lstrip("$"),
                assigned_variable_name=str(ensure_object_swap_variable_name(node)),
            )

            if fallback_idx > 0:
                section.new_line()

            section.append(f"[{config.get_key_swap_section_name()}]")
            if config.comment:
                section.append(f"; {config.comment}")
            section.append("condition = $active0 == 1")
            section.append(f"key = {config.hotkey}")
            section.append(f"type = {config.swap_type}")
            section.append(f"{config.get_swap_key_name()} = {','.join(str(i) for i in range(config.option_count))},")

        if not section.empty():
            LOG.info(f"生成了 {len(swap_nodes)} 个 KeySwap 段落配置")

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
            if node.bl_idname != "SSMTNode_ObjectSwap":
                continue
            idx = SwapKeyINIGenerator._get_node_index(node, registry, fallback_idx)
            config = SwapKeyConfig(
                index=idx,
                custom_var_name=str(getattr(node, "custom_var_name", "") or "").strip().lstrip("$"),
                assigned_variable_name=str(ensure_object_swap_variable_name(node)),
            )
            var_line = f"global persist {config.get_swap_key_name()} = 0"
            if not any(var_line in line for line in section.SectionLineList):
                section.append(var_line)

        if not section.empty():
            LOG.info(f"在 [Constants] 中添加了 {len(swap_nodes)} 个切换变量")
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
            LOG.info("在 [Present] 中已处理参数初始化")
        return section

    @staticmethod
    def inject_activation_to_texture_override(
        ini_builder: M_IniBuilder,
        swap_nodes: List[bpy.types.Node],
    ):
        if swap_nodes:
            LOG.info("物体切换激活参数将由导出脚本在 TextureOverride 块中处理")


class SwapKeyINIIntegrator:
    @staticmethod
    def integrate_to_export(
        ini_builder: M_IniBuilder,
        tree: bpy.types.NodeTree,
        registry: Optional[SwapKeyRegistry] = None,
        swap_nodes: Optional[List[bpy.types.Node]] = None,
    ):
        # 允许调用方传入预过滤的 swap_nodes 列表（如 WWMI 按 draw_ib 拆分 INI 时），
        # 未提供时回退到原有的「全量收集」逻辑以保持向后兼容。
        if swap_nodes is None:
            swap_nodes = list(registry.swapkey_nodes) if registry is not None else SwapKeyINIGenerator.collect_all_swap_nodes_from_blueprint(tree)
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
        LOG.info(f"物体切换节点配置生成完成 ({len(swap_nodes)} 个节点)")


class SwapKeyDebugINIWriter:
    @staticmethod
    def generate_sample_ini_output(
        swap_nodes: List[bpy.types.Node],
        registry: Optional[SwapKeyRegistry] = None,
    ) -> str:
        lines = ["\n" + "=" * 80, "物体切换节点 INI 生成示例", "=" * 80, "\n; ========== KeySwap 段落 =========="]

        for fallback_idx, node in enumerate(swap_nodes):
            idx = SwapKeyINIGenerator._get_node_index(node, registry, fallback_idx)
            comment = getattr(node, "comment", "")
            var_name = get_node_variable_name(node)
            lines.append(f"\n[KeySwap_{idx}]")
            if comment:
                lines.append(f"; {comment}")
            lines.append("condition = $active0 == 1")
            lines.append(f"key = {getattr(node, 'hotkey', 'No_Modifiers Numpad3')}")
            lines.append(f"type = {getattr(node, 'swap_type', 'cycle')}")
            lines.append(f"{var_name} = {','.join(str(i) for i in range(getattr(node, 'input_slot_count', 2)))},")

        lines.extend(["\n\n; ========== [Constants] 中的声明 ==========", "[Constants]", "global $active0"])
        for node in swap_nodes:
            lines.append(f"global persist {get_node_variable_name(node)} = 0")

        lines.extend(
            [
                "\n\n; ========== [Present] 中的初始化 ==========",
                "[Present]",
                "post $active0 = 0",
                "\n\n; ========== [TextureOverride_XX] 中的激活参数 ==========",
                "[TextureOverride_4c11c155_288_7068]",
                "hash = 4c11c155",
                "$active0 = 1",
                "; ... 其他配置内容 ...",
                "\n\n; ========== drawindexed 条件示例 ==========",
            ]
        )

        first_var = get_node_variable_name(swap_nodes[0]) if swap_nodes else "$swapkey0"
        lines.extend([f"if {first_var} == 1", "  drawindexed = 7068,0,0", "endif"])

        if len(swap_nodes) > 1:
            lines.append("\n; 支持嵌套条件")
            first = get_node_variable_name(swap_nodes[0])
            second = get_node_variable_name(swap_nodes[1])
            op = getattr(swap_nodes[0], "condition_operator", "&&")
            lines.extend([f"if {first} == 1 {op} {second} == 1", "  drawindexed = 7068,0,0", "endif"])

        lines.append("\n" + "=" * 80 + "\n")
        return "\n".join(lines)


def register():
    pass


def unregister():
    pass
