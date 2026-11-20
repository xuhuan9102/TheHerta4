from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import bpy

from ..common.m_key import M_Key
from ..utils.log_utils import LOG
from .export_helper import BlueprintExportHelper
from .node_swap import SwapKeyConfig
from .variable_registry import (
    ensure_object_swap_variable_name,
    get_node_variable_name,
    validate_unique_object_swap_variable_names,
)


def _get_node_unique_key(node: bpy.types.Node) -> str:
    tree_name = node.id_data.name if hasattr(node, "id_data") and node.id_data else ""
    return f"{tree_name}::{node.name}"


@dataclass
class SwapKeyRegistry:
    next_index: int = 0
    node_swapkey_map: Dict[str, int] = None
    swapkey_nodes: List[bpy.types.Node] = None

    def __post_init__(self):
        if self.node_swapkey_map is None:
            self.node_swapkey_map = {}
        if self.swapkey_nodes is None:
            self.swapkey_nodes = []

    def register_node(self, node: bpy.types.Node) -> int:
        ensure_object_swap_variable_name(node)
        node_key = _get_node_unique_key(node)
        if node_key in self.node_swapkey_map:
            return self.node_swapkey_map[node_key]

        index = self.next_index
        self.node_swapkey_map[node_key] = index
        self.swapkey_nodes.append(node)
        self.next_index += 1
        return index

    def get_total_swap_keys(self) -> int:
        return self.next_index


class ObjectSwapChainProcessor:
    @staticmethod
    def collect_swap_nodes_from_chain(
        node_path: List[bpy.types.Node],
    ) -> List[Tuple[int, bpy.types.Node]]:
        swap_nodes = []
        for i, node in enumerate(node_path):
            if node.bl_idname == "SSMTNode_ObjectSwap":
                swap_nodes.append((i, node))
        return swap_nodes

    @staticmethod
    def build_swap_conditions_for_chain(
        node_path: List[bpy.types.Node],
        registry: SwapKeyRegistry,
        swap_node_option_values: Optional[Dict[str, int]] = None,
        node_index: Optional[int] = None,
    ) -> List[M_Key]:
        swap_keys = []
        swap_node_option_values = swap_node_option_values or {}

        for i, node in enumerate(node_path):
            if node.bl_idname != "SSMTNode_ObjectSwap":
                continue
            if node_index is not None and i != node_index:
                continue

            swap_index = registry.register_node(node)
            option_value = swap_node_option_values.get(_get_node_unique_key(node), 0)
            config = SwapKeyConfig(
                index=swap_index,
                custom_var_name=str(getattr(node, "custom_var_name", "") or "").strip().lstrip("$"),
                assigned_variable_name=str(ensure_object_swap_variable_name(node)),
            )

            m_key = M_Key()
            m_key.key_name = config.get_swap_key_name()
            m_key.initialize_vk_str = getattr(node, "hotkey", "")
            m_key.comment = getattr(node, "comment", f"物体切换_{swap_index}")
            m_key.tmp_value = option_value
            m_key.condition_operator = getattr(node, "condition_operator", "&&")
            m_key.is_swapkey = True
            swap_keys.append(m_key)

        return swap_keys

    @staticmethod
    def generate_swap_key_ini_sections(
        registry: SwapKeyRegistry,
        nodes_list: List[bpy.types.Node],
    ) -> Dict[str, list]:
        result = {"KeySwap": [], "Constants": [], "Present": []}

        for idx, node in enumerate(nodes_list):
            if node.bl_idname != "SSMTNode_ObjectSwap":
                continue

            config = SwapKeyConfig(
                node_id=node.name,
                index=idx,
                hotkey=str(getattr(node, "hotkey", "") or "").strip(),
                swap_type=getattr(node, "swap_type", "cycle"),
                option_count=getattr(node, "input_slot_count", 2),
                comment=getattr(node, "comment", ""),
                custom_var_name=str(getattr(node, "custom_var_name", "") or "").strip().lstrip("$"),
                assigned_variable_name=str(ensure_object_swap_variable_name(node)),
            )

            option_sequence = ",".join(str(i) for i in range(config.option_count))
            if config.hotkey:
                result["KeySwap"].extend(
                    [
                        f"[{config.get_key_swap_section_name()}]",
                        *([f"; {config.comment}"] if config.comment else []),
                        "condition = $active0 == 1",
                        f"key = {config.hotkey}",
                        f"type = {config.swap_type}",
                        f"{config.get_swap_key_name()} = {option_sequence},",
                        "",
                    ]
                )
            result["Constants"].append(f"{config.get_swap_key_name()} = 0")
            result["Present"].append("post $active0 = 0")

        return result

    @staticmethod
    def add_swap_activation_to_texture_override(
        swap_nodes: List[bpy.types.Node],
        texture_override_lines: List[str],
    ) -> List[str]:
        if not swap_nodes or not texture_override_lines:
            return texture_override_lines

        result = []
        for line in texture_override_lines:
            result.append(line)
            if line.strip().startswith("[TextureOverride_"):
                for idx, node in enumerate(swap_nodes):
                    if node.bl_idname == "SSMTNode_ObjectSwap":
                        result.append(f"$active{idx} = 1")
        return result


class DebugOutputGenerator:
    @staticmethod
    def generate_swap_chain_debug(processing_chains, registry: SwapKeyRegistry) -> List[str]:
        lines = ["\n" + "=" * 80, "物体切换节点处理链分析", "=" * 80]
        if registry.next_index == 0:
            lines.append("未检测到物体切换节点")
            return lines

        lines.append(f"\n总共分配了 {registry.next_index} 个切换变量\n")
        for node in registry.swapkey_nodes:
            var_name = get_node_variable_name(node)
            label = str(getattr(node, "custom_var_name", "") or "").strip().lstrip("$") or var_name.replace("$", "swapkey")
            lines.extend(
                [
                    f"[{label}] 对应节点:",
                    f"  节点名称: {node.name}",
                    f"  变量名: {var_name}",
                    f"  备注: {getattr(node, 'comment', 'N/A')}",
                    f"  快捷键: {getattr(node, 'hotkey', 'N/A')}",
                    f"  切换类型: {getattr(node, 'swap_type', 'N/A')}",
                    f"  选项数量: {getattr(node, 'input_slot_count', 1)}",
                    "",
                ]
            )

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

        lines.append("=" * 80 + "\n")
        return lines


def _collect_stable_swap_nodes(blueprint_model) -> List[bpy.types.Node]:
    unique_nodes: Dict[str, bpy.types.Node] = {}

    tree = getattr(blueprint_model, "_tree", None)
    if tree is not None:
        output_node = BlueprintExportHelper.get_node_from_bl_idname(tree, "SSMTNode_Result_Output")
        for node in tree.nodes:
            if node.bl_idname == "SSMTNode_ObjectSwap" and not node.mute:
                if output_node and BlueprintExportHelper._is_node_connected_to_output(tree, node):
                    unique_nodes.setdefault(_get_node_unique_key(node), node)

    for nested_tree in getattr(blueprint_model, "nested_blueprint_trees", []):
        nested_output = BlueprintExportHelper.get_node_from_bl_idname(nested_tree, "SSMTNode_Result_Output")
        for node in nested_tree.nodes:
            if node.bl_idname == "SSMTNode_ObjectSwap" and not node.mute:
                if nested_output and BlueprintExportHelper._is_node_connected_to_output(nested_tree, node):
                    unique_nodes.setdefault(_get_node_unique_key(node), node)

    for chain in blueprint_model.processing_chains:
        for _, node in ObjectSwapChainProcessor.collect_swap_nodes_from_chain(chain.node_path):
            unique_nodes.setdefault(_get_node_unique_key(node), node)

    return [unique_nodes[key] for key in sorted(unique_nodes)]


def integrate_object_swap_to_blueprint_model(blueprint_model):
    registry = SwapKeyRegistry()
    logged_conditions = set()
    chains_with_swap = 0

    stable_swap_nodes = _collect_stable_swap_nodes(blueprint_model)
    validate_unique_object_swap_variable_names(stable_swap_nodes)
    for node in stable_swap_nodes:
        registry.register_node(node)

    for chain in blueprint_model.processing_chains:
        swap_nodes = ObjectSwapChainProcessor.collect_swap_nodes_from_chain(chain.node_path)
        if not swap_nodes:
            continue

        chains_with_swap += 1
        swap_keys = ObjectSwapChainProcessor.build_swap_conditions_for_chain(
            chain.node_path,
            registry,
            swap_node_option_values=chain.swap_node_option_values,
        )
        if not swap_keys:
            continue

        if chain.shapekey_params is None:
            chain.shapekey_params = []
        chain.shapekey_params.extend(swap_keys)

        for item in swap_keys:
            condition_str = f"{item.key_name} == {item.tmp_value}"
            if condition_str not in logged_conditions:
                logged_conditions.add(condition_str)
            if item.key_name not in blueprint_model.keyname_mkey_dict:
                blueprint_model.keyname_mkey_dict[item.key_name] = item

    blueprint_model._swap_key_registry = registry

    if logged_conditions:
        LOG.info("物体切换节点开始执行")
        LOG.info(f"   条件: {', '.join([f'{cond} (&&)' for cond in logged_conditions])}")
        LOG.info(f"   物体切换节点执行完成: {chains_with_swap} 条处理链, {len(logged_conditions)} 个条件")


def register():
    pass


def unregister():
    pass
