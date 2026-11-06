import bpy
import copy
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from datetime import datetime

from ..utils.log_utils import LOG

from ..common.m_key import M_Key
from ..common.draw_call_model import DrawCallModel
from ..common.object_prefix_helper import ObjectPrefixHelper
from .export_helper import BlueprintExportHelper

_NODE_TYPE_OBJECT_INFO = 'SSMTNode_Object_Info'
_NODE_TYPE_OBJECT_GROUP = 'SSMTNode_Object_Group'
_NODE_TYPE_RESULT_OUTPUT = 'SSMTNode_Result_Output'
_NODE_TYPE_RESULT_OUTPUT_NTMI_MODIMP = 'SSMTNode_Result_Output_NTMIModImp'
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
_NODE_TYPE_SHADER_REPLACE = 'SSMTNode_ShaderReplace'
_NODE_TYPE_MULTI_FILE_EXPORT = 'SSMTNode_MultiFile_Export'
_NODE_TYPE_POSTPROCESS_DRAG_INTERACTION = 'SSMTNode_PostProcess_DragInteraction'

_SINGLE_INSTANCE_POSTPROCESS_LABELS = {
    _NODE_TYPE_POSTPROCESS_DRAG_INTERACTION: "拖拽交互",
    "SSMTNode_PostProcess_AnimDriver": "动画驱动蓝图",
    "SSMTNode_PostProcess_BufferCleanup": "缓冲区清理",
    "SSMTNode_PostProcess_Glow": "RabbitFX 贴图后处理",
    "SSMTNode_PostProcess_HealthDetection": "血量检测",
    "SSMTNode_PostProcess_Material": "材质转资源",
    "SSMTNode_PostProcess_CustomMaterialAssign": "材质转资源pro",
    "SSMTNode_PostProcess_MultiFile": "多文件配置",
    "SSMTNode_PostProcess_PSBinding": "PS绑定",
    "SSMTNode_PostProcess_ResourceMerge": "资源合并",
    "SSMTNode_PostProcess_SeamWeld": "接缝焊接",
    "SSMTNode_PostProcess_ShapeKey": "形态键配置",
    "SSMTNode_PostProcess_ShapeKeyExt": "形态键扩展配置",
    "SSMTNode_PostProcess_SliderPanel": "滑块面板",
    "SSMTNode_PostProcess_CombinedPanel": "切换＆滑块面板",
    "SSMTNode_PostProcess_TextAppend": "文本追加",
    "SSMTNode_PostProcess_UVOffset": "UV偏移",
    # 流光描边按 <tag> 生成全局资源/段名，同一个 tag 在同一导出链里只能有一份。
    "SSMTNode_PostProcess_FlowLight": "模型流光描边",
}

_KNOWN_NODE_TYPES = {
    _NODE_TYPE_OBJECT_INFO,
    _NODE_TYPE_OBJECT_GROUP,
    _NODE_TYPE_RESULT_OUTPUT,
    _NODE_TYPE_RESULT_OUTPUT_NTMI_MODIMP,
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
    _NODE_TYPE_SHADER_REPLACE,
    _NODE_TYPE_MULTI_FILE_EXPORT,
}


def _get_node_unique_key(node: bpy.types.Node) -> str:
    """获取节点在全局的唯一标识键"""
    tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
    return f"{tree_name}::{node.name}"


def _get_shader_replace_config_key(info) -> tuple:
    """构建着色器替换配置的合并键。

    配置完全相同（名称前缀、快捷键、组件、着色器列表顺序与内容）的不同节点
    会合并为同一份导出配置，避免同前缀生成重复的 INI 段名和全局变量。
    前缀大小写不敏感，与全局前缀唯一性校验（_validate_shader_replace_info_list）一致。
    """
    shaders = tuple(
        (
            str(shader.get('variant_name', '') or '').strip().casefold(),
            str(shader.get('shader_file_path', '') or '').strip(),
            str(shader.get('shader_hash', '') or '').strip().casefold(),
        )
        for shader in (info.get('shaders') or [])
    )
    return (
        str(info.get('name_prefix', '') or '').strip().casefold(),
        str(info.get('toggle_key', '') or '').strip(),
        info.get('component_index', 0),
        shaders,
    )


def _is_postprocess_node(bl_idname: str) -> bool:
    """判断是否为后处理节点类型"""
    return bl_idname.startswith('SSMTNode_PostProcess_')


def _is_known_node_type(bl_idname: str) -> bool:
    """判断是否为已知的蓝图节点类型"""
    return bl_idname in _KNOWN_NODE_TYPES or _is_postprocess_node(bl_idname)


def _is_postprocess_chain_member(node) -> bool:
    """判断节点是否允许出现在后处理链中。

    后处理链只能由后处理节点和重定向节点组成；也允许“混合”节点，即自身带有
    SSMTSocketPostProcess 输入/输出 socket 的节点。
    """
    bl_idname = getattr(node, "bl_idname", "")
    if bl_idname == "NodeReroute" or _is_postprocess_node(bl_idname):
        return True
    for socket in getattr(node, "inputs", None) or []:
        if getattr(socket, "bl_idname", "") == "SSMTSocketPostProcess":
            return True
    for socket in getattr(node, "outputs", None) or []:
        if getattr(socket, "bl_idname", "") == "SSMTSocketPostProcess":
            return True
    return False


def _will_emit_slider_panel(node) -> bool:
    """节点是否会真的写出形态键滑块面板（供附加模式的顺序校验使用）。

    只勾了 ``use_slider_panel`` 不等于会产出面板：没有可建滑块的形态键参数时
    ``node_postprocess_shapekey_ext`` 会直接跳过。判定优先问节点自己的
    ``will_emit_slider_panel()``（与导出代码同源），拿不到时退回开关判定。
    """
    predicate = getattr(node, "will_emit_slider_panel", None)
    if callable(predicate):
        try:
            return bool(predicate())
        except Exception:
            pass
    return bool(getattr(node, "use_slider_panel", False))


def validate_postprocess_node_constraints(nodes) -> None:
    """验证跨全部已收集 Blueprint 的后处理单实例约束。"""
    active_nodes = [node for node in (nodes or []) if not getattr(node, "mute", False)]
    nodes_by_type = {}
    for node in active_nodes:
        node_type = getattr(node, "bl_idname", "")
        if node_type == "SSMTNode_PostProcess_SwapPanel":
            node_type = "SSMTNode_PostProcess_CombinedPanel"
        nodes_by_type.setdefault(node_type, []).append(node)

    for node_type, label in _SINGLE_INSTANCE_POSTPROCESS_LABELS.items():
        duplicate_nodes = nodes_by_type.get(node_type, [])
        if len(duplicate_nodes) <= 1:
            continue
        names = ", ".join(
            str(getattr(node, "name", "未命名")) for node in duplicate_nodes
        )
        raise ValueError(
            f"同一导出后处理链中{label}节点只能存在一个，当前检测到: {names}"
        )

    panel_names = {}
    for node in nodes_by_type.get("SSMTNode_PostProcess_UIPanel", []):
        display_name = " ".join(
            str(getattr(node, "panel_name", "") or "").split()
        ) or "UIPanel"
        normalized_name = display_name.casefold()
        if normalized_name in panel_names:
            first_node = panel_names[normalized_name]
            raise ValueError(
                "同一导出后处理链中的 UI 面板名称不能重复："
                f"{display_name}（节点 {getattr(first_node, 'name', '未命名')}、"
                f"{getattr(node, 'name', '未命名')}）"
            )
        panel_names[normalized_name] = node

    # execute_postprocess_nodes 按 postprocess_nodes 列表顺序执行，
    # 因此“最后执行”等价于位于 active_nodes 末位。
    cleanup_nodes = [
        node for node in active_nodes
        if getattr(node, "bl_idname", "") == "SSMTNode_PostProcess_BufferCleanup"
    ]
    if cleanup_nodes and cleanup_nodes[0] is not active_nodes[-1]:
        raise ValueError("缓冲区清理节点必须是后处理链最后执行的节点，避免提前删除后续节点所需文件")

    # 文本追加节点没有输出口，结构上只能是链尾；这里再确认它确实是最后执行的
    # 节点——否则说明它被接在了链中间（后面的节点永远拿不到输出），必须拦下来。
    text_append_nodes = [
        node for node in active_nodes
        if getattr(node, "bl_idname", "") == "SSMTNode_PostProcess_TextAppend"
    ]
    if text_append_nodes and text_append_nodes[0] is not active_nodes[-1]:
        raise ValueError(
            "文本追加节点必须位于后处理链最后（它没有输出口，后面不能再接其它节点）"
        )

    # 物体切换面板的「附加模式」复用形态键滑块面板的坐标系与交互状态
    # （$img0_x / $img0_y / $zoom0 / $help / $ui_active / $mouse_clicked / $is_dragging /
    #  $click_outside）。滑块面板的 [Present] 段在「单击且光标在面板外」时把
    # $click_outside 置 1，同帧稍后再判定 $click_outside == 1 && $mouse_clicked == 0
    # 就隐藏面板；附加块靠 $click_outside = 0 抵消这条判定，只有在同一帧里先跑滑块面板、
    # 后跑附加块时才有效。因此启用了滑块面板的形态键扩展节点必须排在物体切换面板之前。
    slider_nodes = [
        node for node in active_nodes
        if getattr(node, "bl_idname", "")
        == "SSMTNode_PostProcess_ShapeKeyExt"
        and _will_emit_slider_panel(node)
    ]
    if slider_nodes:
        slider_node = slider_nodes[0]
        slider_index = active_nodes.index(slider_node)
        for node in active_nodes:
            if getattr(node, "bl_idname", "") not in {
                "SSMTNode_PostProcess_SwapPanel",
                "SSMTNode_PostProcess_CombinedPanel",
            }:
                continue
            if active_nodes.index(node) < slider_index:
                raise ValueError(
                    "切换＆滑块面板节点必须排在启用了滑块面板的形态键扩展节点之后："
                    f"当前「物体切换面板（{getattr(node, 'name', '未命名')}）」在"
                    f"「形态键扩展配置（{getattr(slider_node, 'name', '未命名')}）」之前，"
                    "否则点击滑块面板以外的按钮会连带关闭形态键滑块面板"
                )

    for node in active_nodes:
        validator = getattr(node, "validate_export_configuration", None)
        if callable(validator):
            validator()


@dataclass
class ProcessingChain:

    object_name: str = ""
    original_object_name: str = ""
    virtual_object_name: str = ""
    source_node: Optional[bpy.types.Node] = None

    node_path: List[bpy.types.Node] = field(default_factory=list)
    node_param_signatures: List[str] = field(default_factory=list)

    shapekey_params: List[M_Key] = field(default_factory=list)
    group_stack: List[str] = field(default_factory=list)

    condition_operator: str = " && "

    rename_history: List[dict] = field(default_factory=list)

    swap_node_option_values: Dict[str, int] = field(default_factory=dict)
    multi_file_source_node_key: str = ""
    multi_file_source_node_name: str = ""
    multi_file_option_index: Optional[int] = None
    multi_file_option_count: int = 0

    vertex_group_process_nodes: List[bpy.types.Node] = field(default_factory=list)
    vertex_group_mapping_nodes: List[bpy.types.Node] = field(default_factory=list)
    shader_replace_info_list: List[dict] = field(default_factory=list)
    export_object_name_override: str = ""

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

            prefix_info = ObjectPrefixHelper.get_node_prefix_info(node)
            if prefix_info:
                params.append(f"prefix={prefix_info[0]}")

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
                    getattr(node, 'reverse_mapping', False),
                    getattr(node, 'defer_until_after_vertex_group_process', False),
                    getattr(node, 'filter_objects', False),
                )
            except ImportError:
                return "Object_Rename[unavailable]"

        elif node_type == 'Result_Output':
            return "ResultOutput[unique]"

        elif node_type == 'Result_Output_NTMIModImp':
            return "ResultOutputNTMIModImp[unique]"

        elif node_type == 'VertexGroupProcess':
            params = []
            process_mode = getattr(node, 'process_mode', '')
            if process_mode:
                params.append(f"mode={process_mode}")
            params.append(f"fill={'on' if getattr(node, 'fill_missing_groups', True) else 'off'}")
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

        elif node_type == 'ShaderReplace':
            params = []
            name_prefix = getattr(node, 'name_prefix', '')
            if name_prefix:
                params.append(f"prefix={name_prefix}")
            shader_count = len(getattr(node, 'shader_list', []))
            params.append(f"shaders={shader_count}")
            return f"ShaderReplace[{','.join(params)}]"

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

        elif node_type == 'ObjectSwap':
            params = []
            tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
            if tree_name:
                params.append(f"tree={tree_name}")
            custom_var_name = getattr(node, 'custom_var_name', '')
            if custom_var_name:
                params.append(f"var={custom_var_name}")
            assigned_variable_name = getattr(node, 'assigned_variable_name', '')
            if assigned_variable_name:
                params.append(f"assigned={assigned_variable_name}")
            swap_type = getattr(node, 'swap_type', '')
            if swap_type:
                params.append(f"type={swap_type}")
            input_slot_count = getattr(node, 'input_slot_count', 0)
            params.append(f"slots={input_slot_count}")
            condition_operator = getattr(node, 'condition_operator', '&&')
            params.append(f"op={condition_operator}")
            return f"ObjectSwap[{','.join(params)}]"

        else:
            return f"{node_type}[]"

    def get_chain_hash(self) -> str:
        if not self.node_path:
            suffix = ""
            if self.multi_file_source_node_key:
                suffix = f"|MULTIFILE:{self.multi_file_source_node_key}:{self.multi_file_option_index}"
            return f"SINGLE:{self.object_name}{suffix}"

        signature_parts = []
        for i, (node, sig) in enumerate(zip(self.node_path, self.node_param_signatures)):
            signature_parts.append(f"[{i}]{sig}")

        path_with_params = "|".join(signature_parts)

        if self.swap_node_option_values:
            swap_parts = []
            for swap_name in sorted(self.swap_node_option_values.keys()):
                swap_parts.append(f"{swap_name}={self.swap_node_option_values[swap_name]}")
            path_with_params += "|SWAP:" + ",".join(swap_parts)

        if self.multi_file_source_node_key:
            path_with_params += f"|MULTIFILE:{self.multi_file_source_node_key}:{self.multi_file_option_index}"

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

        if self.multi_file_source_node_key:
            option_label = ""
            if self.multi_file_option_index is not None:
                option_label = f"{self.multi_file_option_index + 1}/{self.multi_file_option_count}"
            parts.append(f"   -> MultiFile: {self.multi_file_source_node_name} {option_label}")

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
        new_chain.virtual_object_name = self.virtual_object_name
        new_chain.source_node = self.source_node
        new_chain.node_path = list(self.node_path)
        new_chain.node_param_signatures = list(self.node_param_signatures)
        new_chain.shapekey_params = copy.deepcopy(self.shapekey_params, memo)
        new_chain.group_stack = list(self.group_stack)
        new_chain.condition_operator = self.condition_operator
        new_chain.rename_history = copy.deepcopy(self.rename_history, memo)
        new_chain.swap_node_option_values = copy.deepcopy(self.swap_node_option_values, memo)
        new_chain.multi_file_source_node_key = self.multi_file_source_node_key
        new_chain.multi_file_source_node_name = self.multi_file_source_node_name
        new_chain.multi_file_option_index = self.multi_file_option_index
        new_chain.multi_file_option_count = self.multi_file_option_count
        new_chain.vertex_group_process_nodes = list(self.vertex_group_process_nodes)
        new_chain.vertex_group_mapping_nodes = list(self.vertex_group_mapping_nodes)
        new_chain.shader_replace_info_list = list(self.shader_replace_info_list)
        new_chain.export_object_name_override = self.export_object_name_override
        new_chain.reached_output = self.reached_output
        new_chain.is_valid = self.is_valid
        return new_chain

    def to_draw_call_model(self) -> DrawCallModel:
        export_object_name = self.get_export_object_name()

        obj_model = DrawCallModel(obj_name=export_object_name)

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
        obj_model.shader_replace_info_list = list(self.shader_replace_info_list)
        obj_model.shader_replace_info_resolved = True

        return obj_model

    def get_export_object_name(self) -> str:
        if self.export_object_name_override:
            return self.export_object_name_override
        export_object_name = self.object_name
        if self.source_node and getattr(self.source_node, 'bl_idname', '') == _NODE_TYPE_OBJECT_INFO:
            if not self.rename_history:
                export_object_name = ObjectPrefixHelper.build_virtual_object_name_for_node(self.source_node, strict=True)
        return export_object_name


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
        self.cross_ib_mapping_method: Dict[tuple, str] = {}
        self.cross_ib_mapping_objects: Dict[tuple, set] = {}
        self.cross_ib_vb_condition_mapping: Dict[tuple, dict] = {}
        self.cross_ib_source_to_target_dict: Dict[str, list] = {}
        self.cross_ib_object_vb_condition: Dict[tuple, dict] = {}
        self.cross_ib_target_info: Dict[str, list] = {}
        self.cross_ib_match_mode: str = 'IB_HASH'
        self.cross_ib_object_names: Set[str] = set()
        self.has_cross_ib: bool = False

        # 着色器替换节点数据
        self.shader_replace_nodes: List[bpy.types.Node] = []
        self.shader_replace_info_list: List[dict] = []
        self.shader_replace_object_names: Set[str] = set()
        self.shader_replace_object_info_map: Dict[str, list] = {}
        self.has_shader_replace: bool = False

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
        self._collect_nested_postprocess_nodes()
        validate_postprocess_node_constraints(self.postprocess_nodes)

        if self.FORWARD_PARSE_MODE:
            self._forward_parse_blueprint(tree, output_node)
        else:
            self._backward_parse_legacy(output_node)

    def _collect_postprocess_nodes(self, output_node: bpy.types.Node):
        self.postprocess_nodes = []
        visited = set()

        for output_socket in output_node.outputs:
            if output_socket.bl_idname == 'SSMTSocketPostProcess' and output_socket.is_linked:
                for link in output_socket.links:
                    self._traverse_postprocess_chain(link.to_node, visited=visited)

        if self.postprocess_nodes:
            LOG.info(f"   🔧 收集到 {len(self.postprocess_nodes)} 个后处理节点")
            for pp_node in self.postprocess_nodes:
                LOG.debug(f"      - {pp_node.bl_idname}: {pp_node.name}")

    def _collect_nested_postprocess_nodes(self):
        if not self.nested_blueprint_trees:
            return
        existing_pp_keys = {_get_node_unique_key(n) for n in self.postprocess_nodes}
        nested_pp_count = 0
        visited = set()

        for nested_tree in self.nested_blueprint_trees:
            nested_output = BlueprintExportHelper.get_node_from_bl_idname(nested_tree, _NODE_TYPE_RESULT_OUTPUT)
            if not nested_output:
                continue

            previous_postprocess_count = len(self.postprocess_nodes)

            for output_socket in nested_output.outputs:
                if output_socket.bl_idname == 'SSMTSocketPostProcess' and output_socket.is_linked:
                    for link in output_socket.links:
                        self._traverse_postprocess_chain(
                            link.to_node,
                            visited=visited,
                            existing_keys=existing_pp_keys,
                        )

            if not any(_get_node_unique_key(n) in existing_pp_keys for n in nested_tree.nodes if _is_postprocess_node(n.bl_idname)):
                for input_socket in nested_output.inputs:
                    if not input_socket.is_linked:
                        continue
                    for link in input_socket.links:
                        source = link.from_node
                        if _is_postprocess_node(source.bl_idname):
                            self._traverse_postprocess_chain(
                                source,
                                visited=visited,
                                existing_keys=existing_pp_keys,
                            )

            nested_in_tree = len(self.postprocess_nodes) - previous_postprocess_count
            existing_pp_keys.update(_get_node_unique_key(n) for n in self.postprocess_nodes)
            nested_pp_count += nested_in_tree

        if nested_pp_count > 0:
            LOG.info(f"   🔧 从嵌套蓝图收集到 {nested_pp_count} 个后处理节点")

    def _traverse_postprocess_chain(self, node: bpy.types.Node, visited: Optional[Set[str]] = None, existing_keys: Optional[Set[str]] = None):
        if visited is None:
            visited = set()

        node_key = _get_node_unique_key(node)
        if node_key in visited:
            return
        visited.add(node_key)

        if not _is_postprocess_chain_member(node):
            raise ValueError(
                f"后处理链中不允许接入非后处理节点 '{getattr(node, 'name', '未命名')}' "
                f"({getattr(node, 'bl_idname', '')})；后处理链只能由后处理节点（及重定向/混合节点）组成"
            )

        if _is_postprocess_node(node.bl_idname):
            if not node.mute:
                if existing_keys is None or node_key not in existing_keys:
                    self.postprocess_nodes.append(node)
                    if existing_keys is not None:
                        existing_keys.add(node_key)
                    LOG.debug(f"   🔧 发现后处理节点: {node.bl_idname} ({node.name})")
            else:
                LOG.debug(f"   ⏭️ 跳过禁用的后处理节点: {node.bl_idname} ({node.name})")

        for output_socket in node.outputs:
            if output_socket.is_linked:
                for link in output_socket.links:
                    self._traverse_postprocess_chain(link.to_node, visited, existing_keys)

    def _collect_special_nodes(self, tree: bpy.types.NodeTree):
        self.vertex_group_process_nodes = []
        self.multi_file_export_nodes = []
        self.nested_blueprint_trees = []
        self.cross_ib_nodes = []
        self.shader_replace_nodes = []

        output_node = BlueprintExportHelper.get_node_from_bl_idname(tree, _NODE_TYPE_RESULT_OUTPUT)

        for node in tree.nodes:
            if node.mute:
                continue

            if node.bl_idname == _NODE_TYPE_VERTEX_GROUP_PROCESS:
                if output_node and BlueprintExportHelper._is_node_connected_to_output(tree, node):
                    self.vertex_group_process_nodes.append(node)
                    LOG.debug(f"   🔧 发现顶点组处理节点: {node.name}")

            elif node.bl_idname == _NODE_TYPE_MULTI_FILE_EXPORT:
                if output_node and BlueprintExportHelper._is_node_connected_to_output(tree, node):
                    self.multi_file_export_nodes.append(node)
                    LOG.debug(f"   🔧 发现多文件导出节点: {node.name}")

            elif node.bl_idname == _NODE_TYPE_BLUEPRINT_NEST:
                if output_node and BlueprintExportHelper._is_node_connected_to_output(tree, node):
                    self._resolve_nested_blueprint_collect(node)

            elif node.bl_idname == _NODE_TYPE_CROSS_IB:
                if output_node and BlueprintExportHelper._is_node_connected_to_output(tree, node):
                    self.cross_ib_nodes.append(node)
                    LOG.debug(f"   🔧 发现跨IB节点: {node.name}")

            elif node.bl_idname == _NODE_TYPE_SHADER_REPLACE:
                if output_node and BlueprintExportHelper._is_node_connected_to_output(tree, node):
                    self.shader_replace_nodes.append(node)
                    LOG.debug(f"   🔧 发现着色器替换节点: {node.name}")

        self._collect_special_nodes_from_nested_trees()

        if self.vertex_group_process_nodes:
            LOG.info(f"   🔧 收集到 {len(self.vertex_group_process_nodes)} 个顶点组处理节点")
        if self.multi_file_export_nodes:
            LOG.info(f"   🔧 收集到 {len(self.multi_file_export_nodes)} 个多文件导出节点")
        if self.nested_blueprint_trees:
            LOG.info(f"   🔧 收集到 {len(self.nested_blueprint_trees)} 个嵌套蓝图")
        if self.cross_ib_nodes:
            LOG.info(f"   🔧 收集到 {len(self.cross_ib_nodes)} 个跨IB节点")
        if self.shader_replace_nodes:
            LOG.info(f"   🔧 收集到 {len(self.shader_replace_nodes)} 个着色器替换节点")

    def _collect_special_nodes_from_nested_trees(self):
        existing_vg_keys = {_get_node_unique_key(n) for n in self.vertex_group_process_nodes}
        existing_mf_keys = {_get_node_unique_key(n) for n in self.multi_file_export_nodes}
        existing_cross_keys = {_get_node_unique_key(n) for n in self.cross_ib_nodes}
        existing_sr_keys = {_get_node_unique_key(n) for n in self.shader_replace_nodes}

        for nested_tree in self.nested_blueprint_trees:
            nested_output = BlueprintExportHelper.get_node_from_bl_idname(nested_tree, _NODE_TYPE_RESULT_OUTPUT)

            for node in nested_tree.nodes:
                if node.mute:
                    continue

                if nested_output and not BlueprintExportHelper._is_node_connected_to_output(nested_tree, node):
                    continue

                node_key = _get_node_unique_key(node)

                if node.bl_idname == _NODE_TYPE_VERTEX_GROUP_PROCESS:
                    if node_key not in existing_vg_keys:
                        self.vertex_group_process_nodes.append(node)
                        existing_vg_keys.add(node_key)
                        LOG.debug(f"   🔧 发现嵌套蓝图顶点组处理节点: {node.name} (蓝图: {nested_tree.name})")

                elif node.bl_idname == _NODE_TYPE_MULTI_FILE_EXPORT:
                    if node_key not in existing_mf_keys:
                        self.multi_file_export_nodes.append(node)
                        existing_mf_keys.add(node_key)
                        LOG.debug(f"   🔧 发现嵌套蓝图多文件导出节点: {node.name} (蓝图: {nested_tree.name})")

                elif node.bl_idname == _NODE_TYPE_CROSS_IB:
                    if node_key not in existing_cross_keys:
                        self.cross_ib_nodes.append(node)
                        existing_cross_keys.add(node_key)
                        LOG.debug(f"   🔧 发现嵌套蓝图跨IB节点: {node.name} (蓝图: {nested_tree.name})")

                elif node.bl_idname == _NODE_TYPE_SHADER_REPLACE:
                    if node_key not in existing_sr_keys:
                        self.shader_replace_nodes.append(node)
                        existing_sr_keys.add(node_key)
                        LOG.debug(f"   🔧 发现嵌套蓝图着色器替换节点: {node.name} (蓝图: {nested_tree.name})")

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

        # 嵌套蓝图也只遍历真正连到输出端的节点，避免把断开的试验链路带进导出结果。
        nested_output = BlueprintExportHelper.get_node_from_bl_idname(nested_tree, _NODE_TYPE_RESULT_OUTPUT)
        for inner_node in nested_tree.nodes:
            if inner_node.bl_idname == _NODE_TYPE_BLUEPRINT_NEST and not inner_node.mute:
                if nested_output and not BlueprintExportHelper._is_node_connected_to_output(nested_tree, inner_node):
                    continue
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

        self._execute_chain_nodes_sequentially()

        self._detect_and_apply_cross_ib_rename_mapping()

        self._process_cross_ib_nodes()

        self._process_shader_replace_nodes()

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
        for chain in valid_chains:
            draw_call_model = chain.to_draw_call_model()
            self.ordered_draw_obj_data_model_list.append(draw_call_model)
            LOG.info(
                f"[VG-DRAWCALL] runtime='{chain.object_name}' export='{draw_call_model.obj_name}' "
                f"source='{draw_call_model.source_obj_name}'"
            )

        LOG.info(f"🏗️ DrawCallModel 构建: {len(valid_chains)} 个有效 / {invalid_count} 个无效")

    def _integrate_object_swap_nodes(self):
        try:
            from .node_swap_processor import integrate_object_swap_to_blueprint_model
        except ImportError:
            LOG.debug("⊘ 物体切换节点模块未找到（可选功能）")
            return

        try:
            integrate_object_swap_to_blueprint_model(self)
        except Exception as e:
            raise RuntimeError(f"物体切换节点集成失败：{e}") from e
        LOG.info("✓ 物体切换节点集成完成")

    def _integrate_vertex_group_nodes(self):
        try:
            from .node_vertex_group_process import SSMTNode_VertexGroupProcess

            valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
            if not valid_chains:
                LOG.warning("   ⚠️ 没有有效的处理链，跳过顶点组处理")
                return

            has_vg_in_chains = any(c.vertex_group_process_nodes or c.vertex_group_mapping_nodes for c in valid_chains)
            if not self.vertex_group_process_nodes and not has_vg_in_chains:
                return

            SSMTNode_VertexGroupProcess.execute_batch_from_chains(valid_chains)

        except ImportError:
            LOG.debug("⊘ 顶点组处理节点模块未找到（可选功能）")
        except Exception as e:
            import traceback
            LOG.warning(f"⚠️ 顶点组处理节点集成遇到错误: {e}")
            traceback.print_exc()

    def _execute_chain_nodes_sequentially(self):
        from .node_rename import SSMTNode_Object_Rename

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]

        has_rename_chains = any(
            any(getattr(node, "bl_idname", "") == _NODE_TYPE_OBJECT_RENAME for node in getattr(c, "node_path", []) or [])
            for c in valid_chains
        )
        has_vg_chains = any(c.vertex_group_process_nodes for c in valid_chains)

        if BluePrintModel._has_executed_rename:
            for chain in valid_chains:
                original_obj_name = chain.object_name
                mapped_name = BluePrintModel.get_mapped_object_name(original_obj_name)
                if mapped_name != original_obj_name:
                    chain.object_name = mapped_name
                    LOG.debug(f"   Use mapped object name: '{original_obj_name}' -> '{mapped_name}'")
            if has_vg_chains:
                self._integrate_vertex_group_nodes()
            return
        if has_rename_chains and not has_vg_chains:
            self._execute_object_rename_nodes()
            return
        if not has_rename_chains:
            if has_vg_chains:
                self._integrate_vertex_group_nodes()
            return
        for chain in valid_chains:
            if has_rename_chains:
                chain.rename_history = []

        rename_count = 0
        vg_count = 0
        deferred_chain_count = 0
        next_processing_chains = []

        def _append_runtime_rename_history(target_chain, rename_node, history):
            for record in history or []:
                target_chain.rename_history.append(
                    {
                        'operation_index': len(target_chain.rename_history) + 1,
                        'node_name': getattr(rename_node, 'name', ''),
                        'node_key': _get_node_unique_key(rename_node),
                        **record,
                    }
                )

        for chain in valid_chains:
            obj_name = chain.object_name
            obj = bpy.data.objects.get(obj_name)
            if not obj and chain.original_object_name:
                obj = bpy.data.objects.get(chain.original_object_name)
            if not obj:
                LOG.warning(f"[VG-CHAIN] Skip chain because object is missing: '{obj_name}'")
                continue

            chain_defer_rename = any(
                getattr(node, 'bl_idname', '') == _NODE_TYPE_OBJECT_RENAME
                and getattr(node, 'defer_until_after_vertex_group_process', False)
                for node in getattr(chain, 'node_path', []) or []
            )
            if chain_defer_rename:
                deferred_chain_count += 1

            states = [
                {
                    "chain": chain,
                    "obj": obj,
                    "current_name": obj.name,
                    "original_obj_name": chain.object_name,
                    "chain_had_rename": False,
                    "deferred_rename_nodes": [],
                }
            ]

            for node in chain.node_path:
                if not states:
                    break

                next_states = []
                for state in states:
                    state_chain = state["chain"]
                    state_obj = state["obj"]
                    current_name = state["current_name"]

                    if node.bl_idname == _NODE_TYPE_VERTEX_GROUP_PROCESS:
                        if getattr(state_obj, "type", "") == 'MESH':
                            stats = node.process_object(state_obj)
                            vg_count += 1
                            LOG.info(
                                f"[VG-CHAIN] VertexGroupProcess node '{node.name}' on '{current_name}' "
                                f"(renamed={stats.get('renamed', 0)}, merged={stats.get('merged', 0)}, "
                                f"cleaned={stats.get('cleaned', 0)}, filled={stats.get('filled', 0)})"
                            )
                        next_states.append(state)
                        continue

                    if node.bl_idname == _NODE_TYPE_OBJECT_RENAME:
                        if chain_defer_rename:
                            state["deferred_rename_nodes"].append(node)
                            next_states.append(state)
                            continue

                        new_name, was_modified, history, _signature = SSMTNode_Object_Rename.apply_to_object_name(
                            current_name, node
                        )
                        if was_modified:
                            old_name = current_name
                            state_obj.name = new_name
                            current_name = state_obj.name
                            state["current_name"] = current_name
                            state["chain_had_rename"] = True
                            state_chain.object_name = current_name
                            state_chain.virtual_object_name = current_name
                            if state_chain.export_object_name_override:
                                state_chain.export_object_name_override = current_name
                            _append_runtime_rename_history(state_chain, node, history)
                            rename_count += 1
                            LOG.info(f"[VG-CHAIN] Rename '{old_name}' -> '{current_name}'")
                        next_states.append(state)
                        continue

                    next_states.append(state)

                states = next_states

            for state in states:
                state_chain = state["chain"]
                state_obj = state["obj"]
                current_name = state["current_name"]

                for deferred_node in state["deferred_rename_nodes"]:
                    new_name, was_modified, history, _signature = SSMTNode_Object_Rename.apply_to_object_name(
                        current_name, deferred_node
                    )
                    if not was_modified:
                        continue
                    old_name = current_name
                    state_obj.name = new_name
                    current_name = state_obj.name
                    state["current_name"] = current_name
                    state["chain_had_rename"] = True
                    state_chain.object_name = current_name
                    state_chain.virtual_object_name = current_name
                    if state_chain.export_object_name_override:
                        state_chain.export_object_name_override = current_name
                    _append_runtime_rename_history(state_chain, deferred_node, history)
                    rename_count += 1
                    LOG.info(f"[VG-CHAIN] Deferred rename '{old_name}' -> '{current_name}'")

                if state["chain_had_rename"] and state_obj.name != state["original_obj_name"]:
                    BluePrintModel._object_name_mapping[state["original_obj_name"]] = state_obj.name
                next_processing_chains.append(state_chain)

        BluePrintModel._has_executed_rename = True
        self.processing_chains = next_processing_chains
        self._merge_processing_chains()

        rename_chains = [c for c in self.processing_chains if c.rename_history]
        SSMTNode_Object_Rename.log_rename_summary(rename_chains)

        LOG.info(
            f"[VG-CHAIN] Sequential chain execution complete: rename={rename_count}, "
            f"vg_process={vg_count}, deferred_rename_chains={deferred_chain_count}, "
            f"final_chains={len(self.processing_chains)}"
        )

    @staticmethod
    def _append_cross_ib_mapping_from_name_pair(
        old_name: str,
        new_name: str,
        indexcount_mapping: Dict[str, List[str]],
        ibhash_mapping: Dict[str, List[str]],
    ):
        from ..common.object_prefix_helper import ObjectPrefixHelper

        if not old_name or not new_name or old_name == new_name:
            return
        old_prefix_info = ObjectPrefixHelper.extract_prefix_info(old_name)
        new_prefix_info = ObjectPrefixHelper.extract_prefix_info(new_name)

        if not old_prefix_info or not new_prefix_info:
            return
        old_prefix = old_prefix_info[0]
        new_prefix = new_prefix_info[0]

        old_parts = ObjectPrefixHelper.parse_prefix_parts(old_prefix)
        new_parts = ObjectPrefixHelper.parse_prefix_parts(new_prefix)

        old_indexcount = old_parts.get('index_count', '')
        new_indexcount = new_parts.get('index_count', '')
        if old_indexcount and new_indexcount and old_indexcount != new_indexcount:
            if old_indexcount not in indexcount_mapping:
                indexcount_mapping[old_indexcount] = []
            if new_indexcount not in indexcount_mapping[old_indexcount]:
                indexcount_mapping[old_indexcount].append(new_indexcount)

        old_ib_hash = old_parts.get('draw_ib', '')
        new_ib_hash = new_parts.get('draw_ib', '')
        old_first_index = old_parts.get('first_index', '')
        new_first_index = new_parts.get('first_index', '')
        if old_ib_hash and new_ib_hash and (old_ib_hash != new_ib_hash or old_first_index != new_first_index):
            old_key = f"{old_ib_hash}_{old_first_index}"
            new_key = f"{new_ib_hash}_{new_first_index}"
            if old_key not in ibhash_mapping:
                ibhash_mapping[old_key] = []
            if new_key not in ibhash_mapping[old_key]:
                ibhash_mapping[old_key].append(new_key)

    def _build_cross_ib_rename_mappings_for_chain(self, chain: ProcessingChain) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        indexcount_mapping: Dict[str, List[str]] = {}
        ibhash_mapping: Dict[str, List[str]] = {}

        for record in chain.rename_history:
            self._append_cross_ib_mapping_from_name_pair(
                record.get('old_name', ''),
                record.get('new_name', ''),
                indexcount_mapping,
                ibhash_mapping,
            )

        return indexcount_mapping, ibhash_mapping

    def _build_cross_ib_rename_mappings_from_records(self, rename_records: List[dict]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        temp_chain = ProcessingChain()
        temp_chain.rename_history = list(rename_records)
        return self._build_cross_ib_rename_mappings_for_chain(temp_chain)

    def _build_cross_ib_rename_mappings_from_node(self, rename_node: bpy.types.Node) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        indexcount_mapping: Dict[str, List[str]] = {}
        ibhash_mapping: Dict[str, List[str]] = {}

        rename_rules = list(getattr(rename_node, 'rename_rules', []))
        for rule in rename_rules:
            search_str = getattr(rule, 'search_str', '')
            replace_str = getattr(rule, 'replace_str', '')
            self._append_cross_ib_mapping_from_name_pair(
                search_str,
                replace_str,
                indexcount_mapping,
                ibhash_mapping,
            )

        if getattr(rename_node, 'reverse_mapping', False):
            for rule in reversed(rename_rules):
                search_str = getattr(rule, 'search_str', '')
                replace_str = getattr(rule, 'replace_str', '')
                self._append_cross_ib_mapping_from_name_pair(
                    replace_str,
                    search_str,
                    indexcount_mapping,
                    ibhash_mapping,
                )

        return indexcount_mapping, ibhash_mapping

    @staticmethod
    def _merge_cross_ib_rename_mappings(
        base_mapping: Dict[str, List[str]],
        extra_mapping: Dict[str, List[str]],
    ) -> Dict[str, List[str]]:
        merged_mapping: Dict[str, List[str]] = {}

        for source_key, target_keys in base_mapping.items():
            merged_mapping[source_key] = list(target_keys)

        for source_key, target_keys in extra_mapping.items():
            if source_key not in merged_mapping:
                merged_mapping[source_key] = []
            for target_key in target_keys:
                if target_key not in merged_mapping[source_key]:
                    merged_mapping[source_key].append(target_key)

        return merged_mapping

    def _detect_and_apply_cross_ib_rename_mapping(self):
        from .node_cross_ib import CrossIBMatchMode

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]

        for cross_ib_node in self.cross_ib_nodes:
            cross_ib_node.save_original_params()

        has_cross_ib_rename = False
        for cross_ib_node in self.cross_ib_nodes:
            rename_records_by_node: Dict[str, List[dict]] = {}
            rename_nodes_by_key: Dict[str, bpy.types.Node] = {}

            for chain in valid_chains:
                if cross_ib_node not in chain.node_path or not chain.rename_history:
                    pass
                else:
                    for record in chain.rename_history:
                        rename_node_key = (
                            record.get('node_key', '')
                            or record.get('node_name', '')
                            or '__unknown_rename_node__'
                        )
                        rename_records_by_node.setdefault(rename_node_key, []).append(record)

                if cross_ib_node not in chain.node_path:
                    continue

                for node in chain.node_path:
                    if node.bl_idname != _NODE_TYPE_OBJECT_RENAME:
                        continue
                    rename_nodes_by_key[_get_node_unique_key(node)] = node

            rename_node_keys = set(rename_records_by_node.keys()) | set(rename_nodes_by_key.keys())
            sorted_rename_nodes = sorted(
                rename_node_keys,
                key=lambda item: min(
                    (
                        record.get('operation_index', 10**9)
                        for record in rename_records_by_node.get(item, [])
                    ),
                    default=10**9 if item not in rename_records_by_node else 10**8,
                ),
            )

            for rename_node_key in sorted_rename_nodes:
                rename_records = rename_records_by_node.get(rename_node_key, [])
                rename_node = rename_nodes_by_key.get(rename_node_key)
                rename_node_name = (
                    rename_records[0].get('node_name', '')
                    if rename_records
                    else getattr(rename_node, 'name', rename_node_key)
                ) or rename_node_key

                indexcount_mapping, ibhash_mapping = self._build_cross_ib_rename_mappings_from_records(rename_records)
                if rename_node is not None:
                    node_indexcount_mapping, node_ibhash_mapping = self._build_cross_ib_rename_mappings_from_node(rename_node)
                    indexcount_mapping = self._merge_cross_ib_rename_mappings(indexcount_mapping, node_indexcount_mapping)
                    ibhash_mapping = self._merge_cross_ib_rename_mappings(ibhash_mapping, node_ibhash_mapping)

                if not indexcount_mapping and not ibhash_mapping:
                    continue

                has_cross_ib_rename = True
                match_mode = getattr(cross_ib_node, 'match_mode', CrossIBMatchMode.INDEX_COUNT)

                if match_mode == CrossIBMatchMode.INDEX_COUNT and indexcount_mapping:
                    cross_ib_node.apply_indexcount_mapping(indexcount_mapping)
                    total_pairs = sum(len(v) for v in indexcount_mapping.values())
                    LOG.info(
                        f"🔗 Rename节点 '{rename_node_name}' 向节点 '{cross_ib_node.name}' 追加IndexCount衍生映射 {len(indexcount_mapping)} 个源 → {total_pairs} 个目标"
                    )
                elif match_mode == CrossIBMatchMode.IB_HASH and ibhash_mapping:
                    cross_ib_node.apply_ibhash_mapping(ibhash_mapping)
                    total_pairs = sum(len(v) for v in ibhash_mapping.values())
                    LOG.info(
                        f"🔗 Rename节点 '{rename_node_name}' 向节点 '{cross_ib_node.name}' 追加IBHash衍生映射 {len(ibhash_mapping)} 个源 → {total_pairs} 个目标"
                    )

        if not has_cross_ib_rename:
            LOG.info("🔗 未检测到同时包含跨IB节点和重命名节点的链路")
            return
    def _process_cross_ib_nodes(self):
        LOG.info(f"🔗 开始处理跨IB节点，共 {len(self.cross_ib_nodes)} 个节点")

        if not self.cross_ib_nodes:
            self.has_cross_ib = False
            LOG.info("🔗 没有找到跨IB节点，跳过处理")
            return
        from .node_cross_ib import CrossIBMatchMode

        self.cross_ib_info_dict.clear()
        self.cross_ib_method_dict.clear()
        self.cross_ib_mapping_method.clear()
        self.cross_ib_mapping_objects.clear()
        self.cross_ib_vb_condition_mapping.clear()
        self.cross_ib_source_to_target_dict.clear()
        self.cross_ib_object_vb_condition.clear()
        self.cross_ib_target_info.clear()
        self.cross_ib_object_names.clear()
        self.cross_ib_match_mode = 'IB_HASH'
        self.has_cross_ib = False

        for cross_ib_node in self.cross_ib_nodes:
            if hasattr(cross_ib_node, '_update_cross_ib_method'):
                try:
                    cross_ib_node._update_cross_ib_method()
                except Exception as exc:
                    LOG.warning(f"跳过 Cross IB 节点 '{cross_ib_node.name}'：{exc}")
                    continue

            if getattr(cross_ib_node, 'unsupported_reason', ''):
                LOG.warning(f"跳过 Cross IB 节点 '{cross_ib_node.name}'：{cross_ib_node.unsupported_reason}")
                continue

            LOG.info(f"🔗 跨IB节点 '{cross_ib_node.name}':")
            LOG.info(f"🔗   cross_ib_list 长度: {len(cross_ib_node.cross_ib_list)}")

            for i, item in enumerate(cross_ib_node.cross_ib_list):
                LOG.info(f"🔗   条目 {i}: source_ib='{item.source_ib}', target_ib='{item.target_ib}'")
                LOG.info(f"🔗   条目 {i}: source_index_count='{item.source_index_count}', target_index_count='{item.target_index_count}'")

            node_ib_mapping = cross_ib_node.get_ib_mapping_dict()
            node_method = getattr(cross_ib_node, 'cross_ib_method', '')
            node_match_mode = getattr(cross_ib_node, 'match_mode', CrossIBMatchMode.INDEX_COUNT)

            LOG.info(f"🔗   method={node_method}, match_mode={node_match_mode}")
            LOG.info(f"🔗   映射内容: {self._format_cross_ib_mapping(node_ib_mapping)}")

            self.cross_ib_method_dict[cross_ib_node.name] = node_method

            if not self.cross_ib_match_mode or self.cross_ib_match_mode == 'IB_HASH':
                self.cross_ib_match_mode = node_match_mode

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
        LOG.info(f"🔗 有效处理链数量: {len(valid_chains)}")

        cross_ib_chain_count = 0
        for chain in valid_chains:
            cross_ib_nodes_in_chain = [n for n in chain.node_path if n.bl_idname == _NODE_TYPE_CROSS_IB]
            if not cross_ib_nodes_in_chain:
                continue

            cross_ib_chain_count += 1
            obj_name = chain.object_name
            export_obj_name = chain.get_export_object_name()

            for cross_ib_node in cross_ib_nodes_in_chain:
                node_ib_mapping = cross_ib_node.get_ib_mapping_dict()
                node_method = getattr(cross_ib_node, 'cross_ib_method', '')
                vb_condition_source = cross_ib_node.get_vb_condition_source()
                vb_condition_target = cross_ib_node.get_vb_condition_target()

                LOG.info(
                    f"🔗   链路 '{obj_name}' (导出名 '{export_obj_name}') 在节点 '{cross_ib_node.name}' 上的节点映射: "
                    f"{self._format_cross_ib_mapping(node_ib_mapping)}"
                )

                for source_key, target_keys in node_ib_mapping.items():
                    if source_key not in self.cross_ib_info_dict:
                        self.cross_ib_info_dict[source_key] = []
                    if source_key not in self.cross_ib_source_to_target_dict:
                        self.cross_ib_source_to_target_dict[source_key] = []

                    for target_key in target_keys:
                        if target_key not in self.cross_ib_info_dict[source_key]:
                            self.cross_ib_info_dict[source_key].append(target_key)
                        if target_key not in self.cross_ib_source_to_target_dict[source_key]:
                            self.cross_ib_source_to_target_dict[source_key].append(target_key)

                        mapping_key = (source_key, target_key)
                        if mapping_key not in self.cross_ib_mapping_method:
                            self.cross_ib_mapping_method[mapping_key] = node_method
                        if mapping_key not in self.cross_ib_vb_condition_mapping:
                            self.cross_ib_vb_condition_mapping[mapping_key] = {
                                'source': vb_condition_source,
                                'target': vb_condition_target
                            }
                            LOG.info(f"🔗   VS条件映射 {mapping_key}: source={vb_condition_source}, target={vb_condition_target}")

                        if target_key not in self.cross_ib_target_info:
                            self.cross_ib_target_info[target_key] = []
                        if source_key not in self.cross_ib_target_info[target_key]:
                            self.cross_ib_target_info[target_key].append(source_key)

                obj_ib_keys = self._get_object_ib_keys(obj_name)
                LOG.info(f"🔗 物体 '{obj_name}' 的 IB keys: {self._format_sorted_string_list(obj_ib_keys)}")

                matched_source_key = None
                for key in obj_ib_keys:
                    if key in node_ib_mapping:
                        matched_source_key = key
                        break

                if matched_source_key:
                    self.cross_ib_object_names.add(export_obj_name)
                    LOG.info(f"🔗   物体 '{obj_name}' 被标记为跨IB物体，导出名 '{export_obj_name}'，匹配源: {matched_source_key}")

                    for target_key in node_ib_mapping[matched_source_key]:
                        mapping_key = (matched_source_key, target_key)
                        if mapping_key not in self.cross_ib_mapping_objects:
                            self.cross_ib_mapping_objects[mapping_key] = set()
                        self.cross_ib_mapping_objects[mapping_key].add(export_obj_name)

                        object_mapping_key = (export_obj_name, matched_source_key, target_key)
                        if object_mapping_key not in self.cross_ib_object_vb_condition:
                            self.cross_ib_object_vb_condition[object_mapping_key] = {
                                'source': vb_condition_source,
                                'target': vb_condition_target
                            }

        LOG.info(f"🔗 经过跨IB节点的处理链数量: {cross_ib_chain_count}")

        self.has_cross_ib = len(self.cross_ib_info_dict) > 0

        if self.has_cross_ib:
            LOG.info(f"🔗 跨IB处理完成: {len(self.cross_ib_info_dict)} 个源映射, {len(self.cross_ib_object_names)} 个跨IB物体")
            for source_key, target_keys in sorted(self.cross_ib_info_dict.items(), key=lambda item: str(item[0])):
                LOG.info(f"🔗   源 {source_key} -> 目标 {self._format_sorted_string_list(target_keys)}")
            for mapping_key, obj_names in sorted(
                self.cross_ib_mapping_objects.items(),
                key=lambda item: tuple(str(part) for part in item[0]),
            ):
                LOG.info(f"🔗   映射 {mapping_key}: {self._format_sorted_string_list(obj_names)}")
        else:
            LOG.info("🔗 跨IB处理完成: 没有有效的跨IB映射")

        for cross_ib_node in self.cross_ib_nodes:
            if getattr(cross_ib_node, 'original_cross_ib_data', ''):
                cross_ib_node.restore_original_params()

    def _process_shader_replace_nodes(self):
        """处理着色器替换节点：收集配置和关联物体。"""
        LOG.info(f"🎨 开始处理着色器替换节点，共 {len(self.shader_replace_nodes)} 个节点")

        self.shader_replace_info_list.clear()
        self.shader_replace_object_names.clear()
        self.shader_replace_object_info_map.clear()
        self.has_shader_replace = False

        if not self.shader_replace_nodes:
            LOG.info("🎨 没有找到着色器替换节点，跳过处理")
            return

        valid_chains = [c for c in self.processing_chains if c.is_valid and c.reached_output]
        for chain in self.processing_chains:
            chain.shader_replace_info_list = []

        # 仅解析实际被有效链选中的最近节点。仅连接到输出但没有对象输入的节点，
        # 以及同一链上被最近节点遮蔽的配置，都不应参与文件复制和全局前缀校验。
        node_info_map = {}
        info_by_config_key = {}
        sr_chain_count = 0
        for chain in valid_chains:
            sr_nodes_in_chain = [n for n in chain.node_path if n.bl_idname == _NODE_TYPE_SHADER_REPLACE]
            if not sr_nodes_in_chain:
                continue

            sr_chain_count += 1
            obj_name = chain.object_name
            export_obj_name = chain.get_export_object_name()

            self.shader_replace_object_names.add(export_obj_name)
            # 只取链路中最近的一个着色器替换节点（最靠近物体端的）
            nearest_sr_node = sr_nodes_in_chain[0]
            node_key = _get_node_unique_key(nearest_sr_node)
            info = node_info_map.get(node_key)
            if info is None:
                info = nearest_sr_node.get_shader_replace_info()
                node_info_map[node_key] = info
                config_key = _get_shader_replace_config_key(info)
                merged_info = info_by_config_key.get(config_key)
                if merged_info is None:
                    info_by_config_key[config_key] = info
                    self.shader_replace_info_list.append(info)
                    LOG.info(
                        f"🎨 着色器替换节点 '{nearest_sr_node.name}': "
                        f"prefix={info['name_prefix']}, shaders={len(info['shaders'])}"
                    )
                else:
                    # 不同链路上的节点配置完全相同：合并为同一份配置，
                    # 所有关联物体共享同一前缀、快捷键和着色器。
                    info = merged_info
                    node_info_map[node_key] = merged_info
                    LOG.info(
                        f"🎨 着色器替换节点 '{nearest_sr_node.name}' 与已有节点配置完全相同，"
                        f"已合并为同一份配置 (prefix={info['name_prefix']})"
                    )
            if info:
                chain.shader_replace_info_list = [info]
                object_infos = self.shader_replace_object_info_map.setdefault(export_obj_name, [])
                if info not in object_infos:
                    object_infos.append(info)
            LOG.info(f"🎨   物体 '{obj_name}' (导出名 '{export_obj_name}') 关联着色器替换节点 '{nearest_sr_node.name}'")

        self.has_shader_replace = len(self.shader_replace_info_list) > 0

        if self.has_shader_replace:
            LOG.info(f"🎨 着色器替换处理完成: {len(self.shader_replace_info_list)} 个节点, {len(self.shader_replace_object_names)} 个关联物体")
        else:
            LOG.info("🎨 着色器替换处理完成: 没有有效的配置")

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

        return sorted(dict.fromkeys(keys))

    @staticmethod
    def _format_sorted_string_list(values) -> list[str]:
        return sorted(str(value) for value in (values or []))

    @classmethod
    def _format_cross_ib_mapping(cls, mapping) -> dict[str, list[str]]:
        ordered = {}
        for source_key in sorted((mapping or {}).keys(), key=str):
            ordered[str(source_key)] = cls._format_sorted_string_list((mapping or {}).get(source_key))
        return ordered

    def execute_postprocess_nodes(self, mod_export_path: str):
        if not self.postprocess_nodes:
            return
        for pp_node in self.postprocess_nodes:
            node_class = type(pp_node)
            clear_cache = getattr(node_class, 'clear_cache', None)
            if clear_cache and callable(clear_cache):
                try:
                    clear_cache()
                except Exception:
                    pass

        name_mapping = dict(BluePrintModel._object_name_mapping)
        if name_mapping:
            LOG.info(f"📋 传递名称映射到后处理节点: {len(name_mapping)} 条规则")
            for pp_node in self.postprocess_nodes:
                if hasattr(pp_node, 'apply_name_mapping'):
                    try:
                        pp_node.apply_name_mapping(name_mapping)
                    except Exception as e:
                        LOG.warning(f"   ⚠️ 后处理节点 '{pp_node.name}' 应用名称映射失败: {e}")

        LOG.info(f"🔧 后处理节点开始执行: {len(self.postprocess_nodes)} 个节点")

        for pp_node in self.postprocess_nodes:
            try:
                if hasattr(pp_node, 'execute_postprocess'):
                    pp_node.execute_postprocess(mod_export_path)
                else:
                    LOG.warning(f"   ⚠️ 后处理节点缺少 execute_postprocess 方法: {pp_node.bl_idname}")
            except Exception as e:
                # LOG.error 会立刻抛出 Fatal，导致下面带节点上下文的异常包装不可达。
                LOG.warning(f"   ❌ 后处理节点执行失败: {pp_node.bl_idname} ({pp_node.name}): {e}")
                raise RuntimeError(
                    f"后处理节点 '{pp_node.name}' 执行失败: {e}"
                ) from e

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
            virtual_object_name = ObjectPrefixHelper.build_virtual_object_name_for_node(unknown_node, strict=True)
            obj = bpy.data.objects.get(virtual_object_name)
            if obj is None or obj.type != 'MESH' or obj.data is None or len(obj.data.vertices) == 0:
                LOG.info("BluePrintModel: 跳过空网格或无效对象: " + str(virtual_object_name))
                return

            obj_model = DrawCallModel(obj_name=virtual_object_name)

            if hasattr(unknown_node, 'original_object_name') and unknown_node.original_object_name:
                obj_model.display_name = unknown_node.original_object_name

            obj_model.work_key_list = copy.deepcopy(chain_key_list)

            self.ordered_draw_obj_data_model_list.append(obj_model)

        elif unknown_node.bl_idname == 'SSMTNode_Object_Info_Pro':
            # 物体信息 Pro：逐条目展开（一个节点 = 多个物体信息节点）
            from .node_obj import build_object_info_entry_views

            for entry_view in build_object_info_entry_views(unknown_node):
                virtual_object_name = ObjectPrefixHelper.build_virtual_object_name_for_node(entry_view, strict=True)
                obj = bpy.data.objects.get(virtual_object_name)
                if obj is None or obj.type != 'MESH' or obj.data is None or len(obj.data.vertices) == 0:
                    LOG.info("BluePrintModel: 跳过空网格或无效对象: " + str(virtual_object_name))
                    continue

                obj_model = DrawCallModel(obj_name=virtual_object_name)
                original_name = str(getattr(entry_view, 'original_object_name', '') or '')
                if original_name:
                    obj_model.display_name = original_name
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
