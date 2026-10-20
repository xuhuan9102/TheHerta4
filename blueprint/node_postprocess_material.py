import bpy
import os
import glob
import hashlib
import re
from collections import OrderedDict
import shutil

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..toolkit.tt_dds_conversion import (
    convert_texture_to_dds,
    existing_dds_matches_format,
    resolve_texture_dds_format,
)
from . import fx_namespace
from .node_postprocess_base import SSMTNode_PostProcess_Base

_name_mapping_cache = {}
_reverse_name_mapping_cache = {}
# 资源缓存按“材质内容签名”去重，避免同一套贴图被重复复制/重复生成 Resource。
_material_resource_cache = {}
_TTL_MASK_INVERT_PREFIX = "${}TTL{}mask_invert".format(chr(92), chr(92)).casefold()
# TTL 合成亮度默认值（TTLib 的 $\TTL\V，即 HSV 的 Value）。
# 必须写在 if 条件块之外：TTLib 的 CommandListFrameReset 每帧把 $V 重置为 1.0，
# 只有绘制前重新赋值才生效；不需要提亮的段落手动删掉这一行即可。
_TTL_DEFAULT_BRIGHTNESS = "1.5"
# 拖拽物体显隐 flag 行（注入在绘制分支内；TTL 块重建必须原样保留，否则隐藏判定失效）
_DRAG_OBJVIS_LINE_RE = re.compile(r'^\s*\$ssmtdrag_objvis_[\w]*\s*=\s*1\s*$')

# 各 FX 命名空间的资源前缀（小写，用于识别 ini 里已有的发光/裁切绑定）。
# 识别一律按「所有已知命名空间」做，而不是只看当前逻辑：作者可能在自己的配置表里
# 手写另一套命名空间的绑定，材质转资源应该照样把它指向新复制出来的贴图资源。
_FX_RESOURCE_PREFIXES = tuple(
    profile.resource_prefix for profile in fx_namespace.ALL_PROFILES
)
_FX_PROFILE_BY_PREFIX = {
    profile.resource_prefix.lower(): profile for profile in fx_namespace.ALL_PROFILES
}
# 绘制后复位块的引用行 / 亮度参数行（跨命名空间识别，避免透明度段重建时把复位行
# 当成用户内容搬走）。
_FX_RESET_REF_LINES = tuple(
    f"{profile.glow_ref} = ref null" for profile in fx_namespace.ALL_PROFILES
) + tuple(
    f"{profile.fxmap_ref} = ref null" for profile in fx_namespace.ALL_PROFILES
) + tuple(
    # TTLMap 只有声明了它的命名空间才有（HI3FX）；空字符串要跳过，否则会多出一条
    # 谁也匹配不上的 " = ref null"。
    f"{profile.ttlmap_ref} = ref null"
    for profile in fx_namespace.ALL_PROFILES
    if profile.ttlmap_ref
)
_FX_RESET_PARAM_LINES = tuple(
    f"{profile.param('brightness')} = 0" for profile in fx_namespace.ALL_PROFILES
)
_FX_RUN_LINES = tuple(profile.run_line for profile in fx_namespace.ALL_PROFILES)


def _fx_profile_for_param(param_name):
    """参数名属于哪个 FX 命名空间；不属于任何一套时返回 None。"""
    lowered = str(param_name or "").lower()
    for prefix, profile in _FX_PROFILE_BY_PREFIX.items():
        if lowered.startswith(prefix):
            return profile
    return None


def _starts_with_fx_resource_prefix(param_name):
    return _fx_profile_for_param(param_name) is not None


def _load_gimi_orfix_adapter():
    """加载原神 ORFix 适配模块；模块缺失时返回 None，不影响其它游戏导出。"""
    try:
        from . import gimi_orfix_adapter
    except ImportError:
        return None
    return gimi_orfix_adapter


def clear_name_mapping_cache():
    global _name_mapping_cache, _reverse_name_mapping_cache, _material_resource_cache
    _name_mapping_cache.clear()
    _reverse_name_mapping_cache.clear()
    _material_resource_cache.clear()


class MaterialPrefixItem(bpy.types.PropertyGroup):
    prefix: bpy.props.StringProperty(
        name="前缀",
        description="材质名称前缀，用于筛选检测的材质",
        default=""
    )


class DetectedMaterialItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")
    missing_prefix: bpy.props.StringProperty(name="缺失前缀", default="")


MATERIAL_DETECT_PRESETS = [
    "DiffuseMap",
    "NormalMap",
    "LightMap",
    "MaterialMap",
    "RampMap",
    "HighLightMap",
    "WengineFX",
    "WengineFx",
    "ZglowMap",
]


class SSMT_OT_MaterialDetectAddPrefix(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_add_prefix"
    bl_label = "添加前缀"
    bl_description = "按预设顺序添加下一个材质检测前缀"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != 'SSMTNode_PostProcess_Material':
            return {'CANCELLED'}

        existing = {item.prefix for item in node.material_detect_prefixes}
        for preset in MATERIAL_DETECT_PRESETS:
            if preset not in existing:
                new_item = node.material_detect_prefixes.add()
                new_item.prefix = preset
                return {'FINISHED'}

        self.report({'WARNING'}, "所有预设前缀已添加")
        return {'CANCELLED'}


class SSMT_OT_MaterialDetectRemovePrefix(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_remove_prefix"
    bl_label = "移除前缀"
    bl_description = "移除指定索引的材质检测前缀"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == 'SSMTNode_PostProcess_Material':
            if 0 <= self.item_index < len(node.material_detect_prefixes):
                node.material_detect_prefixes.remove(self.item_index)
        return {'FINISHED'}


class SSMT_OT_MaterialDetectAddCustomPrefix(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_add_custom_prefix"
    bl_label = "添加自定义前缀"
    bl_description = "添加手动输入的材质检测前缀"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != 'SSMTNode_PostProcess_Material':
            return {'CANCELLED'}

        custom = node.temp_prefix_input.strip()
        if not custom:
            self.report({'WARNING'}, "请输入前缀")
            return {'CANCELLED'}

        existing = {item.prefix for item in node.material_detect_prefixes}
        if custom in existing:
            self.report({'WARNING'}, f"前缀 '{custom}' 已存在")
            return {'CANCELLED'}

        new_item = node.material_detect_prefixes.add()
        new_item.prefix = custom
        node.temp_prefix_input = ""
        return {'FINISHED'}


class SSMT_OT_MaterialDetect(bpy.types.Operator):
    bl_idname = "ssmt.material_detect"
    bl_label = "Material Detect"
    bl_description = "Detect missing prefixed materials from connected objects, including nested blueprints"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    @staticmethod
    def _is_result_output_node(node) -> bool:
        return getattr(node, "bl_idname", "") in {
            "SSMTNode_Result_Output",
            "SSMTNode_Result_Output_NTMIModImp", "SSMTNode_VeloExportBridge",
        }

    def _find_result_output(self, node, visited=None):
        if visited is None:
            visited = set()
        tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
        node_key = f"{tree_name}::{node.name}"
        if node_key in visited:
            return None
        visited.add(node_key)

        if self._is_result_output_node(node):
            return node

        for input_socket in node.inputs:
            if input_socket.bl_idname == 'SSMTSocketPostProcess' and input_socket.is_linked:
                for link in input_socket.links:
                    result = self._find_result_output(link.from_node, visited)
                    if result:
                        return result
        return None

    def _collect_object_info_nodes(self, node, visited=None, visited_trees=None):
        if visited is None:
            visited = set()
        if visited_trees is None:
            visited_trees = set()

        tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
        node_key = f"{tree_name}::{node.name}"
        if node_key in visited:
            return []
        visited.add(node_key)

        detect_nodes = []
        if node.bl_idname in {'SSMTNode_Object_Info', 'SSMTNode_MultiFile_Export'}:
            detect_nodes.append(node)

        if node.bl_idname == 'SSMTNode_Blueprint_Nest':
            nested_tree_name = str(getattr(node, 'blueprint_name', '') or '').strip()
            if nested_tree_name and nested_tree_name != 'NONE' and nested_tree_name not in visited_trees:
                nested_tree = bpy.data.node_groups.get(nested_tree_name)
                if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                    visited_trees.add(nested_tree_name)
                    for nested_node in nested_tree.nodes:
                        if self._is_result_output_node(nested_node):
                            detect_nodes.extend(
                                self._collect_object_info_nodes(
                                    nested_node,
                                    visited=visited,
                                    visited_trees=visited_trees,
                                )
                            )

        for input_socket in node.inputs:
            if input_socket.is_linked:
                for link in input_socket.links:
                    detect_nodes.extend(
                        self._collect_object_info_nodes(
                            link.from_node,
                            visited=visited,
                            visited_trees=visited_trees,
                        )
                    )
        return detect_nodes

    def _iter_detect_object_names(self, detect_nodes):
        for detect_node in detect_nodes:
            node_type = getattr(detect_node, 'bl_idname', '')
            if node_type == 'SSMTNode_Object_Info':
                obj_name = getattr(detect_node, 'object_name', '')
                if obj_name:
                    yield obj_name
            elif node_type == 'SSMTNode_MultiFile_Export':
                for item in getattr(detect_node, 'object_list', []):
                    obj_name = getattr(item, 'object_name', '')
                    if obj_name:
                        yield obj_name

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            self.report({'WARNING'}, 'No active blueprint tree')
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != 'SSMTNode_PostProcess_Material':
            return {'CANCELLED'}

        prefixes = [item.prefix.strip() for item in node.material_detect_prefixes if item.prefix.strip()]
        if not prefixes:
            self.report({'WARNING'}, 'Add at least one material prefix first')
            return {'CANCELLED'}

        result_output = self._find_result_output(node)
        if not result_output:
            self.report({'WARNING'}, 'No connected Result_Output node found')
            return {'CANCELLED'}

        detect_nodes = self._collect_object_info_nodes(result_output)
        if not detect_nodes:
            self.report({'WARNING'}, 'No connected object nodes found')
            return {'CANCELLED'}

        node.detected_materials.clear()

        unique_object_names = []
        seen_object_names = set()
        for obj_name in self._iter_detect_object_names(detect_nodes):
            if obj_name in seen_object_names:
                continue
            seen_object_names.add(obj_name)
            unique_object_names.append(obj_name)

        missing_count = 0
        for obj_name in unique_object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                continue

            for prefix in prefixes:
                has_prefix_material = False
                for material_slot in obj.material_slots:
                    material = material_slot.material
                    if material and material.name.startswith(prefix):
                        has_prefix_material = True
                        break

                if has_prefix_material:
                    continue

                item = node.detected_materials.add()
                item.object_name = obj_name
                item.missing_prefix = prefix
                missing_count += 1

        node.detect_all_ok = (missing_count == 0)

        if missing_count > 0:
            self.report({'WARNING'}, f'Detection finished: {missing_count} missing entries across {len(unique_object_names)} objects')
        else:
            self.report({'INFO'}, f'Detection finished: all prefixes found across {len(unique_object_names)} objects')
        return {'FINISHED'}


class SSMT_OT_MaterialDetectClear(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_clear"
    bl_label = "清除结果"
    bl_description = "清除检测结果"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == 'SSMTNode_PostProcess_Material':
            node.detected_materials.clear()
            node.detect_all_ok = False
        return {'FINISHED'}


class SSMTNode_PostProcess_MaterialBase(SSMTNode_PostProcess_Base):
    """材质转资源节点的共享实现基类（未注册的纯 Python 基类）。

    注意：Blender 不允许注册「继承自已注册自定义节点」的子类——注册子类会
    破坏父节点类型（复现：注册 SSMTNode_PostProcess_CustomMaterialAssign 后，
    SSMTNode_PostProcess_Material 无法再创建，并报 unable to get Python class
    for RNA struct 警告）。因此具体节点类都继承本基类、各自独立注册。
    本基类由「材质转资源pro」（完整功能）与「材质转资源」弃用壳（旧文件兼容）
    共用；原版功能已由 pro 完全覆盖（逐方法对比见 docs/PR7审查修复记录.md）。
    """
    TRANSPARENCY_SECTION_MARKER = ";MARK:CustomShaderTransparency----------------------------------------------------------"

    @staticmethod
    def _parse_ini_content(content):
        preamble_lines = []
        sections = OrderedDict()
        current_section = None
        for line in str(content or "").splitlines():
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                current_section = stripped
                sections.setdefault(current_section, [])
            elif current_section is None:
                preamble_lines.append(line)
            elif stripped:
                sections[current_section].append(line)
        return preamble_lines, sections

    @classmethod
    def _serialize_ini_content(
        cls,
        preamble_lines,
        sections,
        transparency_sections=None,
        preserved_tail_content="",
        preserved_driver_content="",
    ):
        new_content = []
        if preserved_driver_content:
            new_content.append(preserved_driver_content.rstrip())
            new_content.append('')
        new_content.extend(preamble_lines or [])
        if new_content and sections and new_content[-1].strip():
            new_content.append('')

        for section_name, lines in sections.items():
            new_content.append(section_name)
            new_content.extend(lines)
            new_content.append('')

        if transparency_sections:
            new_content.append(f'\n{cls.TRANSPARENCY_SECTION_MARKER}')
            for shader_name, lines in transparency_sections.items():
                new_content.append(f"[{shader_name}]")
                new_content.extend(lines)
                new_content.append('')

        if preserved_tail_content:
            new_content.append('')
            new_content.append(preserved_tail_content)

        return "\n".join(new_content)

    @classmethod
    def _strip_previous_transparency_sections(cls, content):
        content = str(content or "")
        marker_index = content.find(cls.TRANSPARENCY_SECTION_MARKER)
        if marker_index < 0:
            return content

        preserved_tail_index = None
        for marker in cls.AUTO_APPENDED_SECTION_MARKERS:
            candidate_index = content.find(marker, marker_index + len(cls.TRANSPARENCY_SECTION_MARKER))
            if candidate_index < 0:
                continue
            if preserved_tail_index is None or candidate_index < preserved_tail_index:
                preserved_tail_index = candidate_index

        generated_tail_end = preserved_tail_index if preserved_tail_index is not None else len(content)
        generated_tail = content[
            marker_index + len(cls.TRANSPARENCY_SECTION_MARKER):generated_tail_end
        ]
        transparency_bodies = {}
        current_section_name = ""
        current_section_lines = []

        def store_current_section():
            if not current_section_name.startswith("CustomShaderTransparencyCloth"):
                return
            body_start = next(
                (
                    index + 1
                    for index, line in enumerate(current_section_lines)
                    if line.strip() == "; --- Start of Overridden Mesh Content ---"
                ),
                None,
            )
            if body_start is not None:
                transparency_bodies[current_section_name.casefold()] = current_section_lines[body_start:]

        for line in generated_tail.splitlines():
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                store_current_section()
                current_section_name = stripped[1:-1].strip()
                current_section_lines = []
            elif current_section_name:
                current_section_lines.append(line)
        store_current_section()

        restored_prefix_lines = []
        for line in content[:marker_index].rstrip().splitlines():
            run_match = re.match(r'^\s*run\s*=\s*([^;]+?)\s*$', line, re.IGNORECASE)
            run_target = run_match.group(1).strip().casefold() if run_match else ""
            body_lines = transparency_bodies.get(run_target)
            if body_lines is None:
                restored_prefix_lines.append(line)
            else:
                restored_prefix_lines.extend(body_lines)

        prefix = "\n".join(restored_prefix_lines).rstrip()
        if preserved_tail_index is None:
            return prefix
        return prefix + "\n\n" + content[preserved_tail_index:]

    @staticmethod
    def clear_cache():
        clear_name_mapping_cache()

    material_to_resource_override: bpy.props.BoolProperty(
        name="覆盖现有资源",
        description="如果资源已存在，则覆盖它",
        default=False
    )
    debug_disable_fx_ttl: bpy.props.BoolProperty(
        name="禁用 FX/TTL (调试)",
        description="调试用：开启后暂时不生成 FX（FXMap→RabbitFX/NTEMIFX）与 TTL（TTLMap）段落",
        default=False
    )
    material_switch_var: bpy.props.StringProperty(
        name="材质切换变量",
        description="用于材质切换的起始变量名(会自动递增)",
        default="$swapkey150",
        update=lambda self, context: self.update_node_width([self.material_switch_var])
    )
    material_detect_prefixes: bpy.props.CollectionProperty(type=MaterialPrefixItem)
    temp_prefix_input: bpy.props.StringProperty(name="自定义前缀", default="")
    detected_materials: bpy.props.CollectionProperty(type=DetectedMaterialItem)
    detect_all_ok: bpy.props.BoolProperty(name="全部正确", default=False)
    show_detect_panel: bpy.props.BoolProperty(
        name="材质检测",
        description="展开/收起材质检测面板",
        default=False
    )

    _ntmi_modimp_extra_ps_t2_diffuse_map = False

    def apply_name_mapping(self, mapping):
        global _name_mapping_cache, _reverse_name_mapping_cache

        node_key = self.name
        _name_mapping_cache[node_key] = mapping.copy()
        _reverse_name_mapping_cache[node_key] = {}

        for original_name, new_name in mapping.items():
            _reverse_name_mapping_cache[node_key][new_name] = original_name

        print(f"[MaterialToResource] 已应用名称映射: {len(mapping)} 条规则")
        for original_name, new_name in mapping.items():
            print(f"  映射: '{original_name}' -> '{new_name}'")

    def _get_name_mapping(self):
        global _name_mapping_cache
        return _name_mapping_cache.get(self.name, {})

    def _get_reverse_name_mapping(self):
        global _reverse_name_mapping_cache
        return _reverse_name_mapping_cache.get(self.name, {})

    def draw_buttons(self, context, layout):
        layout.prop(self, "material_to_resource_override")
        layout.prop(self, "debug_disable_fx_ttl")
        layout.prop(self, "material_switch_var")

        name_mapping = self._get_name_mapping()
        if name_mapping:
            box = layout.box()
            box.label(text=f"已应用 {len(name_mapping)} 条名称映射", icon='INFO')

        layout.separator()
        header_row = layout.row(align=True)
        header_row.prop(self, "show_detect_panel", icon='TRIA_DOWN' if self.show_detect_panel else 'TRIA_RIGHT', text="材质检测", emboss=False)

        if self.show_detect_panel:
            box = layout.box()

            prefix_row = box.row(align=True)
            prefix_row.label(text="检测前缀:", icon='FILTER')
            op = prefix_row.operator("ssmt.material_detect_add_prefix", text="", icon='ADD')
            op.node_name = self.name

            for i, item in enumerate(self.material_detect_prefixes):
                row = box.row(align=True)
                row.label(text=item.prefix, icon='MATERIAL')
                op = row.operator("ssmt.material_detect_remove_prefix", text="", icon='X')
                op.node_name = self.name
                op.item_index = i

            input_row = box.row(align=True)
            input_row.prop(self, "temp_prefix_input", text="", icon='CONSOLE')
            op = input_row.operator("ssmt.material_detect_add_custom_prefix", text="", icon='ADD')
            op.node_name = self.name

            btn_row = box.row(align=True)
            op = btn_row.operator("ssmt.material_detect", text="检测材质", icon='VIEWZOOM')
            op.node_name = self.name
            op = btn_row.operator("ssmt.material_detect_clear", text="清除", icon='X')
            op.node_name = self.name

            if self.detected_materials:
                result_box = box.box()
                result_box.label(text=f"缺失材质 ({len(self.detected_materials)} 个)", icon='ERROR')
                for item in self.detected_materials:
                    row = result_box.row(align=True)
                    row.label(text=item.object_name, icon='OBJECT_DATA')
                    row.label(text=f"缺少: {item.missing_prefix}", icon='ERROR')
            elif self.detect_all_ok:
                result_box = box.box()
                result_box.label(text="全部正确", icon='CHECKMARK')

    def extract_mesh_name(self, line):
        match = re.search(r'\[mesh:([^\]]+)\]', line)
        return match.group(1) if match else None

    def _strip_all_suffixes(self, name):
        stripped_names = []
        current = name
        max_iterations = 20
        iteration = 0
        
        suffix_patterns = [
            r'_copy$',
            r'_chain\d+$',
            r'_dup\d+$',
        ]
        
        while iteration < max_iterations:
            changed = False
            for pattern in suffix_patterns:
                new_name = re.sub(pattern, '', current)
                if new_name != current:
                    if new_name not in stripped_names:
                        stripped_names.append(new_name)
                    current = new_name
                    changed = True
                    break
            if not changed:
                break
            iteration += 1
        
        return stripped_names

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        from ..utils.log_utils import LOG as _LOG
        _LOG.debug(f"[find_object_by_mesh_name] 输入 mesh_name: '{mesh_name}'")
        
        reverse_mapping = self._get_reverse_name_mapping()
        name_mapping = self._get_name_mapping()
        _LOG.debug(f"  reverse_mapping 条目数: {len(reverse_mapping)}")
        _LOG.debug(f"  name_mapping 条目数: {len(name_mapping)}")

        potential_names = []

        all_stripped = self._strip_all_suffixes(mesh_name)
        for stripped in all_stripped:
            if stripped not in potential_names:
                potential_names.append(stripped)
                _LOG.debug(f"  移除后缀生成: '{stripped}'")

        potential_names.append(mesh_name)
        _LOG.debug(f"  初始 potential_names: {potential_names}")

        if reverse_mapping:
            for new_name, original_name in reverse_mapping.items():
                if new_name == mesh_name:
                    if original_name not in potential_names:
                        potential_names.append(original_name)
                        _LOG.debug(f"  反向映射精确匹配: '{new_name}' -> '{original_name}'")
                    for stripped in self._strip_all_suffixes(original_name):
                        if stripped not in potential_names:
                            potential_names.append(stripped)
                            _LOG.debug(f"  反向映射+后缀: '{original_name}' -> '{stripped}'")
                elif new_name in mesh_name:
                    original_mesh_name = mesh_name.replace(new_name, original_name)
                    if original_mesh_name not in potential_names:
                        potential_names.append(original_mesh_name)
                        _LOG.debug(f"  反向映射部分匹配: '{new_name}' in '{mesh_name}' -> '{original_mesh_name}'")
                    for stripped in self._strip_all_suffixes(original_mesh_name):
                        if stripped not in potential_names:
                            potential_names.append(stripped)
                            _LOG.debug(f"  反向映射部分+后缀: '{original_mesh_name}' -> '{stripped}'")

        if name_mapping:
            for original_name, new_name in name_mapping.items():
                if original_name in mesh_name:
                    renamed_mesh_name = mesh_name.replace(original_name, new_name)
                    if renamed_mesh_name not in potential_names:
                        potential_names.append(renamed_mesh_name)
                        _LOG.debug(f"  正向映射: '{original_name}' -> '{new_name}' in '{mesh_name}' -> '{renamed_mesh_name}'")

        _LOG.debug(f"  第一阶段 potential_names: {potential_names}")
        for name in potential_names:
            obj = bpy.data.objects.get(name)
            if obj:
                if object_filter is not None and not object_filter(obj):
                    _LOG.debug(f"  ⚠️ 找到物体但材质不匹配，继续查找: '{name}'")
                    continue
                _LOG.debug(f"  ✅ 找到物体: '{name}'")
                return obj
            else:
                _LOG.debug(f"  ❌ 未找到物体: '{name}'")

        clean_name = re.sub(r'^[a-f0-9]+-[\d]+-', '', mesh_name)
        _LOG.debug(f"  清理前缀后的名称: '{clean_name}' (原: '{mesh_name}')")
        if clean_name != mesh_name:
            potential_clean_names = []

            for stripped in self._strip_all_suffixes(clean_name):
                if stripped not in potential_clean_names:
                    potential_clean_names.append(stripped)
                    _LOG.debug(f"    清理后移除后缀生成: '{stripped}'")

            potential_clean_names.append(clean_name)
            _LOG.debug(f"    初始 potential_clean_names: {potential_clean_names}")

            if name_mapping:
                for original_name, new_name in name_mapping.items():
                    if original_name in clean_name:
                        renamed_clean_name = clean_name.replace(original_name, new_name)
                        if renamed_clean_name not in potential_clean_names:
                            potential_clean_names.append(renamed_clean_name)
                            _LOG.debug(f"    清理后正向映射: '{original_name}' -> '{new_name}' -> '{renamed_clean_name}'")
                        for stripped in self._strip_all_suffixes(renamed_clean_name):
                            if stripped not in potential_clean_names:
                                potential_clean_names.append(stripped)
                                _LOG.debug(f"    清理后正向映射+后缀: '{renamed_clean_name}' -> '{stripped}'")

            if reverse_mapping:
                for new_name, original_name in reverse_mapping.items():
                    original_clean = re.sub(r'^[a-f0-9]+-[\d]+-', '', original_name)
                    if original_clean and original_clean not in potential_clean_names:
                        potential_clean_names.append(original_clean)
                        _LOG.debug(f"    反向映射原始名清理前缀: '{original_name}' -> '{original_clean}'")
                        for stripped in self._strip_all_suffixes(original_clean):
                            if stripped not in potential_clean_names:
                                potential_clean_names.append(stripped)
                                _LOG.debug(f"    反向映射原始名清理+后缀: '{original_clean}' -> '{stripped}'")
                    if new_name in clean_name:
                        original_clean_name = clean_name.replace(new_name, original_name)
                        if original_clean_name not in potential_clean_names:
                            potential_clean_names.append(original_clean_name)
                            _LOG.debug(f"    清理后反向映射部分: '{new_name}' -> '{original_clean_name}'")
                    elif new_name == clean_name:
                        if original_name not in potential_clean_names:
                            potential_clean_names.append(original_name)
                            _LOG.debug(f"    清理后反向映射精确: '{new_name}' -> '{original_name}'")

            _LOG.debug(f"  第二阶段 potential_clean_names: {potential_clean_names}")
            for name in potential_clean_names:
                obj = bpy.data.objects.get(name)
                if obj:
                    if object_filter is not None and not object_filter(obj):
                        _LOG.debug(f"  ⚠️ 找到物体但材质不匹配，继续查找: '{name}'")
                        continue
                    _LOG.debug(f"  ✅ 找到物体: '{name}'")
                    return obj
                else:
                    _LOG.debug(f"  ❌ 未找到物体: '{name}'")

        _LOG.debug(f"  ⚠️ 最终未找到匹配物体: '{mesh_name}'")
        return None

    def extract_transparency_info_from_mesh_name(self, mesh_name):
        match = re.search(r'(.+)_透明(\d+(\.\d+)?)', mesh_name)
        if match:
            base_name = match.group(1)
            transparency_value = match.group(2)
            shader_name = f"CustomShaderTransparencyCloth{base_name.replace('-', '_').replace('.', '_')}_透明{transparency_value}"
            shader_name = SSMTNode_PostProcess_MaterialBase._replace_non_ascii_runs(shader_name)
            return shader_name, transparency_value
        return None, None

    def extract_texture_type_from_resource(self, resource_name):
        match = re.search(r'_Slot_([^_]+)$', resource_name)
        if match:
            return match.group(1)

        match = re.search(r'ResourceTexture_[^=]+_(T\d+)(?:_\d+)?$', resource_name, re.IGNORECASE)
        if match:
            return match.group(1)

        match = re.search(r'Resource-.*-([^_-]+)$', resource_name)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _parse_ps_texture_material_slot(material_name):
        clean_name = str(material_name or "").strip()
        if not clean_name:
            return None
        match = re.match(r'^(?:ps[-_ ]*)?t(\d+)(?:[_\-. ].*)?$', clean_name, re.IGNORECASE)
        if not match:
            return None

        slot_number = str(int(match.group(1)))
        return f"ps-t{slot_number}", f"T{slot_number}"

    @staticmethod
    def _resource_token(value):
        token = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "").strip())
        token = token.strip("_")
        return token or "part"

    @staticmethod
    def _resource_name_token(value):
        token = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "").strip())
        token = token.strip("_.-")
        return token or "Texture"

    @staticmethod
    def _latin_token_for_text(text):
        # 非 ASCII 文本（如中文）映射为确定性的“随机”英文串：由内容哈希派生，
        # 同一文本永远得到同一串字母，引用行与资源定义段才能始终对应。
        digest = hashlib.md5(str(text).encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], "big")
        letters = []
        for _ in range(10):
            letters.append(chr(ord("a") + (value % 26)))
            value //= 26
        return "".join(letters).capitalize()

    @staticmethod
    def _replace_non_ascii_runs(text):
        return re.sub(
            r"[^\x00-\x7f]+",
            lambda match: SSMTNode_PostProcess_MaterialBase._latin_token_for_text(match.group(0)),
            str(text or ""),
        )

    @staticmethod
    def _material_resource_stem(material):
        material_name = str(getattr(material, "name", "") or "").strip()
        if not material_name:
            material_name = "Texture"
        stem = re.sub(r'[\r\n\[\]=<>:"/\\|?*\s]+', "_", material_name).strip()
        stem = SSMTNode_PostProcess_MaterialBase._replace_non_ascii_runs(stem)
        return stem.strip("._") or "Texture"

    @staticmethod
    def _normalize_ps_texture_slot_label(value):
        clean_value = str(value or "").strip()
        if not clean_value:
            return None
        match = re.match(r'^(?:ps[-_ ]*)?t(\d+)$', clean_value, re.IGNORECASE)
        if not match:
            return None
        slot_number = str(int(match.group(1)))
        return f"ps-t{slot_number}", f"T{slot_number}"

    @classmethod
    def _infer_ps_texture_slot_label(cls, *values):
        for value in values:
            normalized_slot = cls._normalize_ps_texture_slot_label(value)
            if normalized_slot:
                return normalized_slot

            basename = os.path.basename(str(value or ""))
            match = re.search(r'(?:^|[-_])t(\d+)(?:[-_.]|$)', basename, re.IGNORECASE)
            if match:
                slot_number = str(int(match.group(1)))
                return f"ps-t{slot_number}", f"T{slot_number}"

        return None

    def _object_texture_resource_identity(self, obj):
        try:
            from ..common.object_prefix_helper import ObjectPrefixHelper
        except Exception:
            ObjectPrefixHelper = None

        candidate_names = []

        def append_candidate_name(name):
            name = str(name or "").strip()
            if name and name not in candidate_names:
                candidate_names.append(name)

        if obj:
            append_candidate_name(getattr(obj, "name", ""))
            append_candidate_name(getattr(obj, "original_object_name", ""))
            for stripped_name in self._strip_all_suffixes(getattr(obj, "name", "")):
                append_candidate_name(stripped_name)

            if ObjectPrefixHelper is not None:
                try:
                    append_candidate_name(ObjectPrefixHelper.resolve_source_object_name(obj.name))
                except Exception:
                    pass

        if ObjectPrefixHelper is not None:
            for name in candidate_names:
                try:
                    prefix_info = ObjectPrefixHelper.extract_prefix_info(name)
                except Exception:
                    prefix_info = None
                if not prefix_info:
                    continue
                prefix, _ = prefix_info
                parts = ObjectPrefixHelper.parse_prefix_parts(prefix)
                draw_ib = str(parts.get("draw_ib", "") or "").strip()
                index_count = str(parts.get("index_count", "") or "").strip()
                first_index = str(parts.get("first_index", "") or "").strip()
                if draw_ib and index_count and first_index:
                    return f"{draw_ib}-{index_count}-{first_index}"
                if draw_ib:
                    return draw_ib

        for name in candidate_names:
            clean_name = str(name or "").strip()
            if not clean_name:
                continue
            clean_name = re.sub(r"^LOD\d+\.", "", clean_name, flags=re.IGNORECASE)
            prefix_candidate = clean_name.split(".", 1)[0]
            match = re.match(r'^([A-Za-z0-9]{6,})[-_](\d+)[-_](\d+)', prefix_candidate)
            if match:
                return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
            match = re.match(r'^([A-Za-z0-9]{6,})', prefix_candidate)
            if match:
                return match.group(1)

        return self._resource_name_token(getattr(obj, "name", "") if obj else "")

    def _object_texture_resource_token(self, obj):
        return self._resource_token(self._object_texture_resource_identity(obj))

    def _ps_texture_resource_name(self, obj, slot_label):
        slot_token = str(slot_label or "").replace("ps-", "").replace("-", "_").upper()
        return f"ResourceTexture_{self._object_texture_resource_token(obj)}_{slot_token}"

    @staticmethod
    def _ps_texture_material_resource_name(material):
        return f"ResourceTexture_{SSMTNode_PostProcess_MaterialBase._material_resource_stem(material)}"

    def _collect_ntmi_cached_texture_slots_raw(self, obj):
        if obj is None:
            return ""
        try:
            from ..ui.ntmi_modimp.prefix_property_cache import get_prefix_record_props, has_prefix_record
        except Exception:
            return ""

        try:
            cached_props = get_prefix_record_props(getattr(obj, "name", ""))
            has_cached_record = has_prefix_record(getattr(obj, "name", ""))
        except Exception:
            return ""
        if not has_cached_record:
            return None
        if not isinstance(cached_props, dict):
            return ""

        workspace_unique_str = str(cached_props.get("modimp_workspace_unique_str", "") or "").strip()
        profile_id = str(cached_props.get("modimp_profile_id", "") or "").strip().lower()
        if not workspace_unique_str and profile_id not in {"yihuan", "ntemi"}:
            return ""

        return str(cached_props.get("modimp_texture_slots", "") or "").strip()

    def _collect_modimp_texture_slots(self, obj):
        import json as _json

        result = OrderedDict()
        raw = self._collect_ntmi_cached_texture_slots_raw(obj)
        if raw is None:
            raw = str(obj.get("modimp_texture_slots", "") or "").strip()
        if not raw:
            return result
        try:
            slots = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return result
        if not isinstance(slots, dict):
            return result

        for slot_label, binding in slots.items():
            if not isinstance(binding, dict):
                continue
            mark_name = str(binding.get("mark_name", "") or "").strip()
            source_path = str(binding.get("source_path", "") or "").strip()
            normalized_slot = self._infer_ps_texture_slot_label(
                slot_label,
                binding.get("mark_slot", ""),
                binding.get("slot", ""),
                source_path,
                binding.get("mark_filename", ""),
            )
            if not normalized_slot:
                continue
            if not mark_name:
                continue
            param_name, slot_token = normalized_slot
            slot_info = dict(binding)
            slot_info["param_name"] = param_name
            slot_info["slot_token"] = slot_token
            slot_info["mark_name"] = mark_name
            slot_info["source_path"] = source_path
            result[param_name] = slot_info
        return result

    def _workspace_material_output_filename(self, material, source_path=None):
        source_path = str(source_path or "").strip()
        source_extension = os.path.splitext(source_path)[1] or ".dds"
        return f"{self._material_resource_stem(material)}{source_extension}"

    def _workspace_material_resource_name(self, material):
        return f"Resource-{self._material_resource_stem(material)}"

    @staticmethod
    def _clone_generated_param_lines(lines, source_param, target_param):
        if not lines:
            return []

        source_param = str(source_param or "").strip()
        target_param = str(target_param or "").strip()
        if not source_param or not target_param or source_param == target_param:
            return []

        assignment_re = re.compile(
            rf"^(?P<indent>\s*){re.escape(source_param)}\s*=\s*(?:(?P<ref>ref)\s+)?(?P<resource>.+?)\s*$",
            re.IGNORECASE,
        )
        alias_lines = []
        target_is_resource_ref = target_param.lower().startswith("resource\\")

        for line in lines:
            match = assignment_re.match(str(line or ""))
            if not match:
                continue

            indent = match.group("indent") or ""
            resource_name = str(match.group("resource") or "").strip()
            if target_is_resource_ref:
                alias_lines.append(f"{indent}{target_param} = ref {resource_name}")
            else:
                alias_lines.append(f"{indent}{target_param} = {resource_name}")

        return alias_lines

    @staticmethod
    def _find_first_slot_by_mark_name(slot_map, mark_name):
        target_mark_name = str(mark_name or "").strip().lower()
        if not target_mark_name:
            return ""

        for param_name, slot_info in (slot_map or {}).items():
            current_mark_name = str(getattr(slot_info, "get", lambda *_args, **_kwargs: "")("mark_name", "") or "").strip().lower()
            if current_mark_name == target_mark_name:
                return str(param_name or "").strip().lower()
        return ""

    @staticmethod
    def _ensure_alias_assignment_in_lines(lines, source_param, target_param):
        source_param = str(source_param or "").strip()
        target_param = str(target_param or "").strip()
        if not source_param or not target_param or source_param == target_param:
            return False

        source_re = re.compile(
            rf"^(?P<indent>\s*){re.escape(source_param)}\s*=\s*(?:(?P<ref>ref)\s+)?(?P<resource>.+?)\s*$",
            re.IGNORECASE,
        )
        target_re = re.compile(
            rf"^\s*{re.escape(target_param)}\s*=",
            re.IGNORECASE,
        )

        if any(target_re.match(str(line or "")) for line in lines):
            return False

        for index, line in enumerate(lines):
            match = source_re.match(str(line or ""))
            if not match:
                continue
            indent = match.group("indent") or ""
            resource_name = str(match.group("resource") or "").strip()
            lines.insert(index, f"{indent}{target_param} = {resource_name}")
            return True

        return False

    def _find_workspace_slot_materials(self, obj, slot_info):
        mark_name = str(slot_info.get("mark_name", "") or "").strip()
        if not mark_name:
            return []

        texture_type_lower = mark_name.lower()
        
        def find_materials_in_object(target_obj):
            matching_materials = OrderedDict()
            for material_slot in getattr(target_obj, "material_slots", []):
                material = material_slot.material
                if not material:
                    continue
                material_name = str(getattr(material, "name", "") or "")
                if texture_type_lower == "fxmap" and not material_name.lower().startswith("fxmap_"):
                    continue
                if not material_name.lower().startswith(texture_type_lower):
                    continue
                material_first_word = material_name.split('_')[0].lower()
                if material_first_word != texture_type_lower:
                    continue
                signature = self._build_material_signature(material)
                if signature not in matching_materials:
                    matching_materials[signature] = material
            return list(matching_materials.values())
        
        if obj:
            obj_materials = find_materials_in_object(obj)
            if obj_materials:
                return obj_materials

        return []

    def _create_workspace_texture_resource_entry(self, obj, slot_info, texture_folder, all_sections, texture_ini_folder="Textures"):
        material = next(iter(self._find_workspace_slot_materials(obj, slot_info)), None)
        if not material:
            return None, None
        texture_image = self.get_texture_from_material(material)
        if not texture_image:
            return None, None
        source_path = bpy.path.abspath(getattr(texture_image, "filepath", "") or "")
        if not source_path or not os.path.exists(source_path):
            return None, None

        os.makedirs(texture_folder, exist_ok=True)
        output_filename = self._workspace_material_output_filename(material, source_path=source_path)
        target_path = os.path.join(texture_folder, output_filename)

        try:
            source_abs = os.path.abspath(source_path)
            target_abs = os.path.abspath(target_path)
            if source_abs != target_abs:
                if self.material_to_resource_override or not os.path.exists(target_path):
                    shutil.copy2(source_path, target_path)
        except Exception as e:
            print(f"复制工作空间贴图失败: {e}")
            return None, None

        resource_name = self._workspace_material_resource_name(material)
        resource_section_name = f"[{resource_name}]"
        texture_ini_folder = str(texture_ini_folder or "Textures").strip().strip("\\/")
        desired_line = f"filename = {texture_ini_folder}/{output_filename}".replace("\\", "/")
        existing_lines = all_sections.get(resource_section_name, [])
        existing_normalized = {
            line.strip().replace("\\", "/")
            for line in existing_lines
        }
        if self.material_to_resource_override or desired_line not in existing_normalized:
            all_sections[resource_section_name] = [desired_line]

        param_name = slot_info.get("param_name", "")
        return f"{param_name} = {resource_name}", resource_name

    def _has_strict_fxmap_material(self, obj):
        if not obj:
            return False
        for material_slot in getattr(obj, "material_slots", []):
            material = material_slot.material
            material_name = str(getattr(material, "name", "") or "")
            if material_name.lower().startswith("fxmap_"):
                return True
        return False

    def _collect_ntemifx_texture_slots(self, obj, workspace_resource_by_slot=None) -> dict[str, str]:
        result: dict[str, str] = {}
        if not self._has_strict_fxmap_material(obj):
            return result

        if workspace_resource_by_slot:
            for slot_label, slot_info in workspace_resource_by_slot.items():
                mark_name = str(slot_info.get("mark_name", "") or "").strip()
                resource_name = str(slot_info.get("resource_name", "") or "").strip()
                if mark_name == "FXMap" and resource_name:
                    result[slot_label] = resource_name
            return result

        for slot_label, slot_info in self._collect_modimp_texture_slots(obj).items():
            mark_name = str(slot_info.get("mark_name", "") or "").strip()
            if mark_name != "FXMap":
                continue
            material = next(iter(self._find_workspace_slot_materials(obj, slot_info)), None)
            if material:
                result[slot_label] = self._workspace_material_resource_name(material)
        return result

    def _collect_ps_texture_slot_materials(self, obj):
        slot_to_materials = OrderedDict()
        if not obj:
            return slot_to_materials

        def collect_from_object(target_obj):
            result = OrderedDict()
            for material_slot in getattr(target_obj, "material_slots", []):
                material = material_slot.material
                if not material:
                    continue
                parsed_slot = self._parse_ps_texture_material_slot(material.name)
                if not parsed_slot:
                    continue
                param_name, texture_type = parsed_slot
                slot_info = result.setdefault(
                    param_name,
                    {
                        "texture_type": texture_type,
                        "materials": OrderedDict(),
                    },
                )
                signature = self._build_material_signature(material)
                if signature not in slot_info["materials"]:
                    slot_info["materials"][signature] = material
            return result
        
        obj_slots = collect_from_object(obj)
        if obj_slots:
            return obj_slots

        return slot_to_materials

    def build_mapping_for_section(self, lines):
        mapping = OrderedDict()
        line_pattern = re.compile(r'^(ps-t\d+|Resource\\[^\s=]+)\s*=\s*(?:ref\s+)?(.*)$')
        for line in lines:
            resource_match = line_pattern.match(line.strip())
            if resource_match:
                param_name = resource_match.group(1).strip()
                resource_name = resource_match.group(2).strip()
                texture_type = self.extract_texture_type_from_resource(resource_name)
                if texture_type:
                    mapping[param_name] = texture_type
        return mapping

    def find_matching_materials(self, obj, texture_type):
        texture_type_lower = texture_type.lower()

        def find_in_object(target_obj):
            result = OrderedDict()
            for material_slot in getattr(target_obj, "material_slots", []):
                material = material_slot.material
                if not material:
                    continue
                if texture_type_lower == "fxmap" and not material.name.lower().startswith("fxmap_"):
                    continue
                if not material.name.lower().startswith(texture_type_lower):
                    continue
                material_first_word = material.name.split('_')[0].lower()
                if material_first_word == texture_type_lower:
                    signature = self._build_material_signature(material)
                    if signature not in result:
                        result[signature] = material
            return list(result.values())
        
        if obj:
            obj_materials = find_in_object(obj)
            if obj_materials:
                return obj_materials

        return []

    def _object_has_matching_materials(self, obj, ini_mapping):
        for slot_info in self._collect_modimp_texture_slots(obj).values():
            texture_type = str(slot_info.get("mark_name", "") or "").strip()
            if texture_type and texture_type != "FXMap" and self._find_workspace_slot_materials(obj, slot_info):
                return True

        for param_name, texture_type in ini_mapping.items():
            param_lower = str(param_name or "").lower()
            is_supported_param = (
                param_lower.startswith("ps-t")
                or param_lower.startswith("resource\\zzmi\\")
                or _starts_with_fx_resource_prefix(param_lower)
            )
            if not is_supported_param:
                continue
            if texture_type == "FXMap" and param_lower.startswith("ps-t"):
                continue
            if self.find_matching_materials(obj, texture_type):
                return True

        if self._collect_ps_texture_slot_materials(obj):
            return True

        # 贴图类型 → 材质前缀兜底。TTLMap 只在当前命名空间声明了它（HI3FX）时才算：
        # 其它运行时没有这个通道，把 TTLMap_ 材质当候选会改变它们既有的物体匹配结果。
        fallback_texture_types = ["Glowmap", "FXMap"]
        if fx_namespace.current_profile().ttlmap_ref:
            fallback_texture_types.append("TTLMap")
        return any(
            self.find_matching_materials(obj, texture_type)
            for texture_type in fallback_texture_types
        )

    @staticmethod
    def _build_material_signature(material):
        if not material:
            return ("",)

        material_name = str(getattr(material, "name", "") or "")
        type_prefix = material_name.split('_')[0].lower()
        
        main_texture_path = None
        if getattr(material, "use_nodes", False) and getattr(material, "node_tree", None):
            try:
                output_node = next(n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output)
                surface_input = output_node.inputs.get('Surface')
                if surface_input and surface_input.is_linked:
                    def find_texture_node_recursively(node):
                        if node.type == 'TEX_IMAGE' and node.image:
                            image = node.image
                            return bpy.path.abspath(getattr(image, "filepath", "") or "") or image.name
                        for input_socket in node.inputs:
                            if input_socket.is_linked:
                                found = find_texture_node_recursively(input_socket.links[0].from_node)
                                if found: return found
                        return None
                    main_texture_path = find_texture_node_recursively(surface_input.links[0].from_node)
            except (StopIteration, AttributeError):
                pass
            
            if not main_texture_path:
                for node in material.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        image = node.image
                        main_texture_path = bpy.path.abspath(getattr(image, "filepath", "") or "") or image.name
                        break

        if main_texture_path:
            return (type_prefix, main_texture_path)
        return (type_prefix, material_name)

    def get_texture_from_material(self, material):
        if not material or not material.use_nodes:
            return None
        try:
            output_node = next(n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output)
            surface_input = output_node.inputs.get('Surface')
            if surface_input and surface_input.is_linked:
                def find_texture_node_recursively(node):
                    if node.type == 'TEX_IMAGE' and node.image:
                        return node.image
                    for input_socket in node.inputs:
                        if input_socket.is_linked:
                            found_image = find_texture_node_recursively(input_socket.links[0].from_node)
                            if found_image: return found_image
                    return None
                linked_image = find_texture_node_recursively(surface_input.links[0].from_node)
                if linked_image: return linked_image
        except (StopIteration, AttributeError):
            pass
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                return node.image
        return None

    def copy_texture_file(self, texture_image, target_folder, material, forced_filename=None):
        if not texture_image or not texture_image.filepath:
            return None
        try:
            source_path = bpy.path.abspath(texture_image.filepath)
            if not os.path.exists(source_path): return None
            os.makedirs(target_folder, exist_ok=True)

            stem = SSMTNode_PostProcess_MaterialBase._material_resource_stem(material)
            _, file_extension = os.path.splitext(os.path.basename(source_path))

            # 导出 DDS 优先：编辑期贴图常是无损 PNG（避免反复存 DDS 时被过度压缩），但游戏
            # 只认 DDS，所以导出时按现有 DDS 规则转一份放进 Mod，INI 的 filename 靠本函数
            # 的返回值生成，自然跟着指向 .dds。开关在「材质转资源pro」节点上；旧版弃用壳没有
            # 该属性，getattr 兜底为 False，行为与改动前一致。
            if file_extension.lower() != ".dds" and getattr(self, "export_textures_as_dds", False):
                converted_name = f"{stem}.dds"
                converted_path = os.path.join(target_folder, converted_name)
                # 导出侧已知贴图类型（材质名本身就是 DiffuseMap_xxx 这类命名），
                # 拿它当兜底提示：编辑期源图名可能只有 Diffuse 而没有 DiffuseMap
                # （实测 VivianGemADiffuse.png），只按源文件名匹配会落到线性默认规则。
                name_hints = [getattr(material, "name", ""), stem]
                # 材质名前缀就是贴图类型（DiffuseMap / MaterialMap / ...），导出侧
                # 必须用它决定 DDS 格式，不能再让源文件名带偏。
                texture_type = str(getattr(material, "name", "") or "").strip().split("_")[0]
                # 「已存在 DDS」不能盲信：必须与当前规则期望的格式一致才跳过，
                # 否则规则改对后旧线性 DDS 会被永远沿用。
                if os.path.exists(converted_path):
                    expected_format = resolve_texture_dds_format(
                        source_path,
                        bpy.context.scene.texture_tools_props,
                        texture_type=texture_type,
                        name_hints=name_hints,
                    )
                    if existing_dds_matches_format(converted_path, expected_format) and (
                        not self.material_to_resource_override or forced_filename == converted_name
                    ):
                        return converted_name
                # 同一轮导出内这张图已经转过：forced_filename 是本次导出里同一材质
                # （同一 stem）上一次调用返回的 .dds 名，且文件确实存在。此时再调一次
                # texconv 重转同一张图，结果与上一次完全相同，纯属重复劳动 ——
                # 实测「身体」那套 5 张贴图各被重转 14 次，一次导出 82 次转换里 70 次
                # 是这种重复（占整次导出 ~70% 的墙钟时间）。
                # 「覆盖现有资源」的语义不受影响：本轮第一次该覆盖的仍然覆盖了。
                ok, reason = convert_texture_to_dds(
                    source_path,
                    converted_path,
                    texture_type=texture_type,
                    name_hints=name_hints,
                )
                if ok:
                    return converted_name
                # 转换失败不中断导出：退回原样复制。文件名也必须用回源扩展名 ——
                # forced_filename 可能来自上一轮成功缓存的 .dds，此刻已无对应文件。
                print(f"贴图转 DDS 失败，回退为原样复制: {source_path} - {reason}")

            if forced_filename and os.path.splitext(forced_filename)[1].lower() == file_extension.lower():
                new_filename = forced_filename
            else:
                # 缓存名与本轮源扩展名不一致时不能沿用：forced_filename 可能是上一轮
                # 成功转出的 .dds，此刻已无对应文件（例如本次转换失败、或编辑期贴图刚被
                # 重链成 PNG），照抄会把 PNG 字节写成 .dds，游戏读不出来。
                new_filename = f"{stem}{file_extension}"

            target_path = os.path.join(target_folder, new_filename)
            if os.path.exists(target_path) and not self.material_to_resource_override:
                return new_filename
            shutil.copy2(source_path, target_path)
            return new_filename
        except Exception as e:
            print(f"复制纹理文件失败: {e}")
            return None

    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict()
        current_section = None
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    stripped_line = line.strip()
                    if stripped_line.startswith('[') and stripped_line.endswith(']'):
                        current_section = stripped_line
                        sections[current_section] = []
                    elif not stripped_line:
                        continue
                    elif current_section is not None:
                        sections[current_section].append(line.rstrip())
        except FileNotFoundError:
            return None
        return sections

    def _write_ordered_dict_to_ini(self, sections, ini_file_path):
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            for section_name, lines in sections.items():
                f.write(f"{section_name}\n")
                for line in lines:
                    f.write(f"{line}\n")
                f.write("\n")

    def define_swapkeys_in_sections(self, sections, keys_to_define):
        if not keys_to_define: return
        if '[Constants]' not in sections:
            new_sections = OrderedDict([('[Constants]', [])])
            new_sections.update(sections)
            sections.clear()
            sections.update(new_sections)
        constants_lines = sections['[Constants]']
        existing_definitions = "".join(constants_lines)
        for key in sorted(list(keys_to_define)):
            if key not in existing_definitions:
                definition = f"global persist {key} = 0"
                constants_lines.append(definition)

    def generate_material_lines(self, matching_materials, param_name, texture_type, obj,
                                texture_folder, all_sections,
                                object_to_diffuse_swapkey, material_group_to_swapkey,
                                swap_key_prefix, next_swap_key_num, used_swap_keys,
                                resource_name_provider=None):
        generated_lines = []
        # 发光亮度写在当前逻辑对应的 FX 命名空间变量上：
        # RabbitFX/NTEMIFX 是大写 $…\brightness 同名字段，HI3FX 同样叫 brightness
        # （但它是 IniParams row230.w，语义一致）。
        brightness_param_name = fx_namespace.current_profile().param("brightness")

        if len(matching_materials) == 1:
            material = matching_materials[0]
            if texture_type == 'Glowmap':
                match = re.search(r'^Glowmap_(\d+)_', material.name, re.IGNORECASE)
                if match:
                    generated_lines.append(f"{brightness_param_name} = {match.group(1)}")

            resource_name = resource_name_provider(material, 0) if resource_name_provider else None
            new_line = self.create_resource_entry(
                material,
                param_name,
                texture_folder,
                all_sections,
                resource_name_override=resource_name,
            )
            if new_line:
                generated_lines.append(new_line)

        elif len(matching_materials) > 1:
            current_swap_variable = None
            if texture_type != 'DiffuseMap' and obj.name in object_to_diffuse_swapkey:
                current_swap_variable = object_to_diffuse_swapkey[obj.name]
            else:
                mat_names_tuple = tuple(sorted([mat.name for mat in matching_materials]))
                if mat_names_tuple not in material_group_to_swapkey:
                    new_swap_key = f"{swap_key_prefix}{next_swap_key_num}"
                    material_group_to_swapkey[mat_names_tuple] = new_swap_key
                    next_swap_key_num += 1
                current_swap_variable = material_group_to_swapkey[mat_names_tuple]

                if texture_type == 'DiffuseMap':
                    object_to_diffuse_swapkey[obj.name] = current_swap_variable

            used_swap_keys.add(current_swap_variable)

            generated_lines.append(f"; {texture_type} 材质切换 (组: {current_swap_variable})")
            for index, material in enumerate(matching_materials):
                resource_name = resource_name_provider(material, index) if resource_name_provider else None
                new_line = self.create_resource_entry(
                    material,
                    param_name,
                    texture_folder,
                    all_sections,
                    resource_name_override=resource_name,
                )
                if new_line:
                    generated_lines.append(f"if {current_swap_variable} == {index}")
                    if texture_type == 'Glowmap':
                        match = re.search(r'^Glowmap_(\d+)_', material.name, re.IGNORECASE)
                        if match:
                            generated_lines.append(f"    {brightness_param_name} = {match.group(1)}")
                    generated_lines.append(f"    {new_line}")
                    generated_lines.append("endif")
            generated_lines.append("")

        return generated_lines, next_swap_key_num

    def create_resource_entry(self, material, param_name, texture_folder, all_sections, resource_name_override=None):
        global _material_resource_cache

        texture_image = self.get_texture_from_material(material)
        if not texture_image: return None

        # 同内容材质复用同一个 Resource 名称，避免材质后处理把同图资源反复写出。
        material_signature = self._build_material_signature(material)
        resource_name_override = str(resource_name_override or "").strip()
        cache_key = material_signature if not resource_name_override else (material_signature, resource_name_override)
        cached_entry = _material_resource_cache.get(cache_key)

        if cached_entry:
            resource_name = cached_entry.get(
                "resource_name",
                resource_name_override or f"Resource_{self._material_resource_stem(material)}",
            )
            copied_filename = self.copy_texture_file(
                texture_image,
                texture_folder,
                material,
                forced_filename=cached_entry.get("filename"),
            )
        else:
            copied_filename = self.copy_texture_file(texture_image, texture_folder, material)
            resource_name = resource_name_override or f"Resource_{self._material_resource_stem(material)}"
            if copied_filename:
                _material_resource_cache[cache_key] = {
                    "resource_name": resource_name,
                    "filename": copied_filename,
                }

        if not copied_filename: return None
        resource_section_name = f"[{resource_name}]"
        desired_line = f"filename = Textures/{copied_filename}".replace("\\", "/")
        existing_lines = all_sections.get(resource_section_name, [])
        existing_normalized = {
            line.strip().replace("\\", "/")
            for line in existing_lines
        }
        if self.material_to_resource_override or desired_line not in existing_normalized:
            all_sections[resource_section_name] = [desired_line]
        return "{} = {}".format(param_name, resource_name) if not param_name.lower().startswith("resource\\") else "{} = ref {}".format(param_name, resource_name)

    @staticmethod
    def _is_generated_material_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False

        generated_prefixes = (
            "Resource\\RabbitFX\\",
            "Resource\\RabbitFx\\",
            "Resource\\ZZMI\\",
            "Resource\\NTEMIFX\\",
            "Resource\\HI3FX\\",
            "ps-t",
            "$\\RabbitFX\\brightness",
            "$\\HI3FX\\brightness",
        )
        generated_exact = {
            "run = CommandList\\RabbitFX\\SetTextures",
            "run = CommandList\\RabbitFx\\SetTextures",
            "run = CommandList\\ZZMI\\SetTextures",
            "run = CommandList\\RabbitFX\\Run",
            "run = CommandList\\NTEMIFX\\Run",
            "run = CommandList\\HI3FX\\Run",
            "run = CommandList\\HI3FX\\Reset",
        }
        return (
            stripped in generated_exact
            or stripped.startswith(generated_prefixes)
            or "材质切换" in stripped
        )

    def _is_material_switch_if_line(self, line: str) -> bool:
        base_var = str(getattr(self, "material_switch_var", "") or "").strip()
        if not base_var:
            return False

        match = re.match(r"^if\s+(\$\w+?)(\d+)\s*==", line.strip())
        base_match = re.match(r"^(\$\w+?)(\d+)$", base_var)
        if match and base_match:
            var_prefix, var_index = match.groups()
            base_prefix, base_index = base_match.groups()
            return var_prefix == base_prefix and int(var_index) >= int(base_index)

        return line.strip().startswith(f"if {base_var} ")

    @staticmethod
    def _should_emit_fx_reset(fx_kinds, next_mesh_fx_kinds) -> bool:
        """当前 mesh 是否需要发射 FX 复位行。

        **恒为 True（2026-09 用户口径）**：每一次绘制都必须自带 FX 复位，因为作者会
        自行调整绘制顺序；"下一个绘制必然重设同名 FX"这个前提在重排后不再成立，
        一旦依赖它就会让上一个 mesh 的 FX 绑定泄漏到不该有的部件上。

        历史上这里做过"下一个 mesh 会重设同名 FX 就跳过复位"的优化（省下每模组
        ~1380 条/帧的 `CommandList\\…\\Run`），已按上述理由撤销；`next_mesh_fx_kinds`
        参数保留以兼容调用点与既有测试。
        """
        return True

    def _find_mesh_block_reset_insert_index(self, lines, mesh_start_index: int) -> int:
        search_end_idx = len(lines)
        for i in range(mesh_start_index + 1, len(lines)):
            if '[mesh:' in lines[i]:
                search_end_idx = i
                break

        draw_idx = -1
        for i in range(mesh_start_index + 1, search_end_idx):
            if 'drawindexed' in lines[i]:
                draw_idx = i
                break

        # 着色器替换模式下 drawindexed 被移到了 run = 引用的段里，
        # 此时用 run = 行作为锚点来确定插入位置。
        if draw_idx == -1:
            run_idx = -1
            for i in range(mesh_start_index + 1, search_end_idx):
                stripped = lines[i].strip()
                if stripped.startswith("run = ") or stripped.startswith("run="):
                    run_idx = i
            if run_idx == -1:
                return -1
            draw_idx = run_idx

        block_depth = 0
        last_endif_idx = -1
        for i in range(mesh_start_index + 1, search_end_idx):
            stripped = lines[i].strip()
            if stripped.startswith("if "):
                block_depth += 1
            elif stripped == "endif":
                if block_depth > 0:
                    block_depth -= 1
                last_endif_idx = i

        if last_endif_idx > draw_idx:
            return last_endif_idx + 1
        return draw_idx + 1

    def _strip_generated_material_lines(self, lines, preserved_ps_slots=None):
        cleaned_lines = []
        inside_mesh_block = False
        skipping_material_switch_block = False
        preserved_ps_slots = {
            str(slot or "").strip().lower()
            for slot in (preserved_ps_slots or [])
            if str(slot or "").strip()
        }

        for line in lines:
            if self.extract_mesh_name(line):
                inside_mesh_block = True
                skipping_material_switch_block = False
                cleaned_lines.append(line)
                continue

            stripped = line.strip()
            if inside_mesh_block and self._is_material_switch_if_line(stripped):
                skipping_material_switch_block = True
                continue

            if skipping_material_switch_block:
                if stripped == "endif":
                    skipping_material_switch_block = False
                continue

            if inside_mesh_block and self._is_generated_material_line(stripped):
                ps_match = re.match(r"^(ps-t\d+)\s*=", stripped, re.IGNORECASE)
                if ps_match and ps_match.group(1).strip().lower() in preserved_ps_slots:
                    cleaned_lines.append(line)
                    continue
                continue

            cleaned_lines.append(line)

        return cleaned_lines

    @staticmethod
    def _extract_transparency_value_from_mesh_name(mesh_name):
        match = re.search(r'_透明(\d+(\.\d+)?)', str(mesh_name or ""))
        return match.group(1) if match else None

    @staticmethod
    def _ttl_parse_drawindexed(line):
        match = re.match(
            r'^\s*drawindexed(?:instanced)?\s*=\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)',
            str(line or "").strip(),
            re.IGNORECASE,
        )
        if match:
            # TTLib 的调用约定与 drawindexed 一致：
            # _1=index count，_2=first index，_3=first vertex/base vertex。
            # ZZMI RedirectSO 会在正文前保留 3 个 stub 顶点，因此第三项
            # 不能丢弃，否则 TTL 二次绘制会从 base_vertex=0 错读 SO。
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
        return None

    def _ttl_first_drawindexed(self, block_lines):
        for line in block_lines:
            draw = self._ttl_parse_drawindexed(line)
            if draw is not None:
                return draw
        return None

    @classmethod
    def _ttl_find_block_start(cls, lines, anchor):
        start = anchor
        i = anchor - 1
        while i >= 0:
            if re.search(r'\[mesh:([^\]]+)\]', str(lines[i] or "")) is not None:
                break
            stripped = str(lines[i]).strip()
            if re.match(r'^(?:if\s+)', stripped, re.IGNORECASE) or stripped.casefold() == 'else':
                start = i
                i -= 1
                continue
            break
        return start

    @staticmethod
    def _strip_drag_hook_blocks(lines):
        result = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if "DRAG HOOK BEGIN" in line:
                result.append(line)
                i += 1
                while i < n:
                    current = lines[i]
                    cs = current.strip()
                    if "DRAG HOOK END" in current:
                        result.append(current)
                        i += 1
                        break
                    if cs.startswith("if "):
                        block = []
                        depth = 0
                        while i < n:
                            b = lines[i]
                            bs = b.strip()
                            block.append(b)
                            if bs.startswith("if "):
                                depth += 1
                            elif bs == "endif":
                                depth -= 1
                                if depth == 0:
                                    i += 1
                                    break
                            i += 1
                        block_text = "\n".join(block)
                        if "CustomShaderDragBake" in block_text or "CustomShaderDragDetect" in block_text:
                            continue
                        result.extend(block)
                        continue
                    result.append(current)
                    i += 1
                continue
            result.append(line)
            i += 1
        return result

    @staticmethod
    def _ttl_extract_drag_vb0(lines):
        # 拖拽 Jiggle 影子缓冲当前形态为 TempVB0（R9 ShadowVB 直写链实机回归已回退）；
        # 正则同时匹配两家族，兼容 ShadowVB 时代的旧生成物。
        for line in lines:
            match = re.search(r'\b(ResourceDragJiggle(?:ShadowVB|TempVB0)_[A-Za-z0-9_]+)\b', str(line or ""))
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _ttl_find_drag_vb0_global(all_sections):
        """全表扫描 jiggle 换绑行，返回 (drag_vb0, 所在段 lines)，均无则 (None, None)。

        排除 [CommandListSSMTTTLDraw_*] 段（那里只是引用，且可能是陈旧残留）。
        优先当前形态 TempVB0：ShadowVB 资源段在重导出时会被拖拽侧清理，
        命令表若引用它将悬空。
        """
        for family in ("TempVB0", "ShadowVB"):
            pattern = r'\b(ResourceDragJiggle' + family + r'_[A-Za-z0-9_]+)\b'
            for sec_name, sec_lines in all_sections.items():
                if not isinstance(sec_lines, list):
                    continue
                if str(sec_name).startswith("[CommandListSSMTTTLDraw_"):
                    continue
                for line in sec_lines:
                    match = re.search(pattern, str(line or ""))
                    if match:
                        return match.group(1), sec_lines
        return None, None

    @staticmethod
    def _ttl_extract_ib(lines):
        for line in lines:
            stripped = str(line or "").strip()
            match = re.match(r'^ib\s*=\s*([A-Za-z0-9_.]+)', stripped, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _ttl_drawib_token(name):
        """取名字里形如 8 位十六进制的 DrawIB 片段（小写）；取不到返回 None。"""
        match = re.search(r'(?<![0-9A-Za-z])([0-9a-fA-F]{8})(?![0-9A-Za-z])', str(name or ""))
        return match.group(1).lower() if match else None

    @classmethod
    def _ttl_drag_vb0_matches_section(cls, drag_vb0, header_lines, ib_resource):
        """jiggle 影子 VB0 是否属于本 TTL 段所在的 DrawIB。

        jiggle 影子缓冲是**按 DrawIB** 生成的（ResourceDragJiggleTempVB0_<drawib>_<实例>），
        而命令列表会把它绑成 vb0 —— 只有同一 DrawIB 的 TTL 段才该接这根钩子。
        判据优先级：段头 `hash = <drawib>`（权威）→ 段头 `ib = <资源名>` 里的 DrawIB
        片段；两者都取不到（命名不是 8 位十六进制）时按「匹配」处理，不改变原有行为。
        """
        drag_token = cls._ttl_drawib_token(drag_vb0)
        if not drag_token:
            return True
        for line in header_lines:
            match = re.match(r'^hash\s*=\s*([0-9A-Za-z_.]+)', str(line or "").strip(), re.IGNORECASE)
            if match:
                section_token = cls._ttl_drawib_token(match.group(1))
                return drag_token == section_token if section_token else True
        ib_token = cls._ttl_drawib_token(ib_resource)
        if ib_token:
            return drag_token == ib_token
        return True

    @staticmethod
    def _ttl_extract_drag_condition(lines, drag_vb0):
        for index, line in enumerate(lines):
            if drag_vb0 not in str(line or ""):
                continue
            depth = 0
            cursor = index - 1
            while cursor >= 0:
                stripped = str(lines[cursor] or "").strip()
                if stripped.casefold() == "endif":
                    depth += 1
                elif re.match(r'^if\s+', stripped, re.IGNORECASE):
                    if depth == 0:
                        return re.sub(r'^if\s+', '', stripped, flags=re.IGNORECASE).strip()
                    depth -= 1
                cursor -= 1
            break
        return None

    def _build_ttl_draw_lines(self, block_lines):
        ttl_lines = []
        found = False
        pending_flags = []
        i = 0
        n = len(block_lines)
        while i < n:
            stripped = str(block_lines[i]).strip()
            if _DRAG_OBJVIS_LINE_RE.match(stripped):
                # 拖拽显隐 flag 行先收集，在紧随的绘制转换输出前冲刷。
                # TTL 块边界从 mesh 注释行开始（可能落在 if 块内部），此时
                # flag 与 drawindexed 都走「非 if 路径」——必须保留 flag，
                # 否则隐藏物体仍可被命中。
                if not pending_flags or str(pending_flags[-1]).strip() != stripped:
                    pending_flags.append(block_lines[i])
                i += 1
                continue
            if re.match(r'^if\s+', stripped, re.IGNORECASE):
                if pending_flags:
                    ttl_lines.extend(pending_flags)
                    pending_flags = []
                depth = 0
                j = i
                else_index = -1
                while j < n:
                    current = str(block_lines[j]).strip()
                    if re.match(r'^if\s+', current, re.IGNORECASE):
                        depth += 1
                    elif current.casefold() == 'endif':
                        depth -= 1
                        if depth == 0:
                            break
                    elif current.casefold() == 'else' and depth == 1 and else_index == -1:
                        else_index = j
                    j += 1
                if j >= n or depth != 0:
                    break
                head = block_lines[i]
                tail = block_lines[j]
                branch1_end = else_index if else_index != -1 else j
                branch1 = block_lines[i + 1:branch1_end]
                branch2 = block_lines[else_index + 1:j] if else_index != -1 else []
                draw1 = self._ttl_first_drawindexed(branch1)
                draw2 = self._ttl_first_drawindexed(branch2) if else_index != -1 else None
                if draw1 is None and draw2 is None:
                    i = j + 1
                    continue
                found = True
                ttl_lines.append(head)
                if draw1 is not None:
                    # 保留拖拽显隐 flag 行（重建不得丢弃，否则隐藏物体仍可被命中）
                    for bl in branch1:
                        if _DRAG_OBJVIS_LINE_RE.match(str(bl).strip()):
                            ttl_lines.append(bl)
                    ttl_lines.append("    ${}TTL{}_1 = {}".format(chr(92), chr(92), draw1[0]))
                    ttl_lines.append("    ${}TTL{}_2 = {}".format(chr(92), chr(92), draw1[1]))
                    ttl_lines.append("    ${}TTL{}_3 = {}".format(chr(92), chr(92), draw1[2]))
                    ttl_lines.append("    run = CommandList{}TTL{}Draw".format(chr(92), chr(92)))
                if else_index != -1:
                    ttl_lines.append(block_lines[else_index])
                    if draw2 is not None:
                        for bl in branch2:
                            if _DRAG_OBJVIS_LINE_RE.match(str(bl).strip()):
                                ttl_lines.append(bl)
                        ttl_lines.append("    ${}TTL{}_1 = {}".format(chr(92), chr(92), draw2[0]))
                        ttl_lines.append("    ${}TTL{}_2 = {}".format(chr(92), chr(92), draw2[1]))
                        ttl_lines.append("    ${}TTL{}_3 = {}".format(chr(92), chr(92), draw2[2]))
                        ttl_lines.append("    run = CommandList{}TTL{}Draw".format(chr(92), chr(92)))
                ttl_lines.append(tail)
                i = j + 1
            else:
                draw = self._ttl_parse_drawindexed(stripped)
                if draw is not None:
                    found = True
                    ttl_lines.extend(pending_flags)
                    pending_flags = []
                    ttl_lines.append("${}TTL{}_1 = {}".format(chr(92), chr(92), draw[0]))
                    ttl_lines.append("${}TTL{}_2 = {}".format(chr(92), chr(92), draw[1]))
                    ttl_lines.append("${}TTL{}_3 = {}".format(chr(92), chr(92), draw[2]))
                    ttl_lines.append("run = CommandList{}TTL{}Draw".format(chr(92), chr(92)))
                i += 1
        return ttl_lines, found

    def _ttl_section_name(self, mesh_name, all_sections, generated_names):
        cleaned = self._replace_non_ascii_runs(str(mesh_name or "").strip())
        cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", cleaned).strip("_")
        base = f"TextureOverride{cleaned}" if cleaned else "TextureOverride"
        existing = {str(name or "").strip().strip('[]').casefold() for name in all_sections}
        candidate = base
        suffix = 2
        while candidate.casefold() in existing or candidate.casefold() in generated_names:
            candidate = f"{base}_{suffix}"
            suffix += 1
        generated_names.add(candidate.casefold())
        return candidate

    def _process_ttl_sections(self, section_name, lines, all_sections, material_group_to_swapkey,
                              swap_key_prefix, next_swap_key_num, used_swap_keys, texture_folder):
        from ..utils.log_utils import LOG as _LOG
        mesh_lines_info = [(i, self.extract_mesh_name(line)) for i, line in enumerate(lines) if self.extract_mesh_name(line)]
        if not mesh_lines_info:
            return next_swap_key_num

        first_mesh_index = mesh_lines_info[0][0]
        first_block_start = self._ttl_find_block_start(lines, first_mesh_index)
        header_lines = self._strip_drag_hook_blocks(lines[:first_block_start])
        drag_vb0 = self._ttl_extract_drag_vb0(header_lines)
        drag_vb0_lines = header_lines
        if drag_vb0 is None:
            # 回退：拖拽钩子只注入单一主绘制段（_inject_draw_hooks 只落 part.section），
            # TTL copy 段头部无钩子块时全表扫描兄弟段；否则陈旧
            # [CommandListSSMTTTLDraw_*] 段会被原样保留、引用已失效的 jiggle 资源
            # → TTL 断链（2026-08-30 实机回归：TTL 层吃不到形态键/拖拽交互）。
            drag_vb0, drag_vb0_lines = self._ttl_find_drag_vb0_global(all_sections)
        ib_resource = self._ttl_extract_ib(header_lines)
        # 全表回退（54c1a91）本意是给「同一 DrawIB 的兄弟 copy 段」补钩子，却把别的
        # DrawIB 的 TTL 段也接了进来：那些段在拖拽激活时会绑上别人的 jiggle VB0，
        # 而命令列表里的 ib 又会被最后写入者固化（2026-10-07 实机：洛克茜模组
        # 身体/头发共用一个命令列表 → 头发爆炸、按住 ALT 更明显）。跨 DrawIB 一律
        # 不接拖拽钩子，退回原生 run = CommandList\TTL\Draw，索引缓冲由段内自己的
        # ib 行决定。
        if drag_vb0 and not self._ttl_drag_vb0_matches_section(drag_vb0, header_lines, ib_resource):
            drag_vb0 = None
            drag_vb0_lines = header_lines

        generated_section_names = set()
        ttl_sections_to_add = OrderedDict()
        ttl_draw_command_lists = OrderedDict()

        if_stack = []
        if_endif_map = {}
        mesh_cond = {}
        for i, line in enumerate(lines):
            stripped = str(line).strip()
            if re.match(r'^if\s+', stripped, re.IGNORECASE):
                if_stack.append((i, line))
            elif stripped.casefold() == 'endif' and if_stack:
                if_idx, _if_line = if_stack.pop()
                if_endif_map[if_idx] = (i, line)
            mesh_name = self.extract_mesh_name(line)
            if mesh_name:
                mesh_cond[i] = [if_idx for if_idx, _ in if_stack]

        for mesh_index, mesh_name in reversed(mesh_lines_info):
            transparency_value = self._extract_transparency_value_from_mesh_name(mesh_name)
            obj = self.find_object_by_mesh_name(mesh_name)
            fx_materials = self.find_matching_materials(obj, "FXMap") if obj is not None else []
            ttl_materials = self.find_matching_materials(obj, "TTLMap") if obj is not None else []
            if fx_materials and ttl_materials:
                _LOG.warning(f"      TTL: 跳过 '{mesh_name}'（同一物体不能同时启用 FX 与 TTL）")
                continue
            if not ttl_materials:
                continue

            cond_if_indexes = mesh_cond.get(mesh_index, [])
            innermost_endif = None
            if cond_if_indexes:
                innermost_endif = if_endif_map.get(cond_if_indexes[-1], (None, None))[0]
            block_start = mesh_index
            block_end = len(lines)
            for i in range(mesh_index + 1, len(lines)):
                if self.extract_mesh_name(lines[i]):
                    block_end = i
                    break
            if innermost_endif is not None and innermost_endif < block_end:
                block_end = innermost_endif
            block_lines = lines[block_start:block_end]
            ttl_draw_lines, draw_found = self._build_ttl_draw_lines(block_lines)
            if not draw_found:
                _LOG.info(f"      TTL: 跳过 '{mesh_name}'（块内无 drawindexed）")
                continue

            if drag_vb0:
                # 资源名家族前缀长度不等（TempVB0/ShadowVB 两代命名），按匹配结果取后缀
                token = re.sub(r'^ResourceDragJiggle(?:ShadowVB|TempVB0)_', '', drag_vb0)
                command_list_name = f"CommandListSSMTTTLDraw_{token}"
                if command_list_name not in ttl_draw_command_lists:
                    drag_condition = self._ttl_extract_drag_condition(drag_vb0_lines, drag_vb0)
                    command_list_lines = []
                    if drag_condition:
                        # 仅在拖拽激活时绑定 jiggle 临时 VB0;其余情况不覆盖 vb0,
                        # 让 TTL 二次绘制继承游戏当前已蒙皮/形态键的 SO 输出。
                        # 严禁绑定 Position 基础输入(会丢失骨骼与蒙皮)。
                        command_list_lines.append(f"if {drag_condition}")
                        command_list_lines.append(f"    vb0 = {drag_vb0}")
                        command_list_lines.append("endif")
                    # 不写死 ib：每个 TTL copy 段段头本来就带自己的 ib 行（段头整段拷贝，
                    # 见下方 header_lines 回写），而命令列表按 token 共用 —— 写死只会让
                    # 最后写入者（文件靠后的 DrawIB/子网格）的 ib 覆盖其它调用段，
                    # 索引缓冲与顶点缓冲错配 → 乱三角形（2026-10-07 实机：洛克茜模组
                    # 身体/头发共用命令列表，头发爆炸、按住 ALT 更明显）。
                    command_list_lines.append("run = CommandList{}TTL{}Draw".format(chr(92), chr(92)))
                    ttl_draw_command_lists[command_list_name] = command_list_lines
                ttl_draw_lines = [
                    str(line).replace(
                        "run = CommandList{}TTL{}Draw".format(chr(92), chr(92)),
                        "run = CommandListSSMTTTLDraw_{}".format(token),
                    )
                    for line in ttl_draw_lines
                ]

            new_lines = []
            object_to_diffuse_swapkey = {}
            for header_line in header_lines:
                stripped = str(header_line).strip()
                if not stripped:
                    continue
                if stripped.casefold().startswith(_TTL_MASK_INVERT_PREFIX):
                    continue
                resource_match = re.match(
                    r'^(ps-t\d+|Resource\\[^\s=]+)\s*=\s*(?:ref\s+)?(.*)$',
                    stripped,
                    re.IGNORECASE,
                )
                if resource_match:
                    param_name = resource_match.group(1).strip()
                    texture_type = self.extract_texture_type_from_resource(resource_match.group(2).strip())
                    if texture_type:
                        matching_materials = self.find_matching_materials(obj, texture_type) if obj is not None else []
                        if matching_materials:
                            generated_lines, next_swap_key_num = self.generate_material_lines(
                                matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                                object_to_diffuse_swapkey, material_group_to_swapkey,
                                swap_key_prefix, next_swap_key_num, used_swap_keys)
                            new_lines.extend(generated_lines)
                            continue
                new_lines.append(header_line)

            anchor_line = str(lines[mesh_index])
            if anchor_line.lstrip().startswith(';'):
                new_lines.append(anchor_line)
            else:
                new_lines.append(f"; {mesh_name}")

            if ttl_materials:
                ttl_lines_ref, next_swap_key_num = self.generate_material_lines(
                    ttl_materials, r"Resource\TTL\TransparencyTex", "TTLMap", obj, texture_folder, all_sections,
                    {}, material_group_to_swapkey, swap_key_prefix, next_swap_key_num, used_swap_keys)
                new_lines.extend(ttl_lines_ref)
                new_lines.append("${}TTL{}mask_channel = 3".format(chr(92), chr(92)))

            if ttl_materials:
                new_lines.append("${}TTL{}mask_invert = 1".format(chr(92), chr(92)))

            alpha_value = transparency_value if transparency_value else "1.0"
            alpha_var = self._ttl_ensure_alpha_variable(alpha_value, all_sections)
            new_lines.append("${}TTL{}alpha = {}".format(chr(92), chr(92), alpha_var))
            # 提亮默认写在条件块之外（此时 if 块尚未拼接），否则进 if 内会失效/爆炸。
            new_lines.append("${}TTL{}V = {}".format(chr(92), chr(92), _TTL_DEFAULT_BRIGHTNESS))

            if cond_if_indexes:
                cond_block = []
                for if_idx in cond_if_indexes:
                    cond_block.append(lines[if_idx])
                indent = "    " * len(cond_if_indexes)
                for ttl_line in ttl_draw_lines:
                    if str(ttl_line).strip():
                        cond_block.append(indent + str(ttl_line))
                    else:
                        cond_block.append(ttl_line)
                for if_idx in reversed(cond_if_indexes):
                    _end_idx, end_line = if_endif_map.get(if_idx, (None, None))
                    if end_line is not None:
                        cond_block.append(end_line)
                new_lines.extend(cond_block)
            else:
                new_lines.extend(ttl_draw_lines)

            new_section_name = self._ttl_section_name(mesh_name, all_sections, generated_section_names)
            ttl_sections_to_add[new_section_name] = new_lines
            del lines[block_start:block_end]

        self._cleanup_empty_if_blocks(lines)

        for command_list_name, command_list_lines in ttl_draw_command_lists.items():
            all_sections[f"[{command_list_name}]"] = command_list_lines

        for new_section_name, new_section_lines in ttl_sections_to_add.items():
            all_sections[f"[{new_section_name}]"] = new_section_lines

        if not any(self.extract_mesh_name(line) for line in lines) and not any('drawindexed' in line for line in lines):
            all_sections.pop(section_name, None)

        return next_swap_key_num

    @staticmethod
    def _ttl_normalize_alpha(alpha_value):
        try:
            return str(float(str(alpha_value)))
        except (TypeError, ValueError):
            return str(alpha_value)

    @classmethod
    def _ttl_alpha_var_name(cls, alpha_value):
        normalized = cls._ttl_normalize_alpha(alpha_value)
        token = normalized.replace(".", "_")
        return f"$TTLAlpha{token}", normalized

    def _ttl_ensure_alpha_variable(self, alpha_value, all_sections):
        var_name, normalized = self._ttl_alpha_var_name(alpha_value)
        constants_key = '[Constants]'
        if constants_key not in all_sections:
            all_sections[constants_key] = []
        definition = f"global {var_name} = {normalized}"
        existing = {str(line).strip() for line in all_sections[constants_key]}
        if definition not in existing:
            all_sections[constants_key].append(definition)
        return var_name

    @staticmethod
    def _cleanup_empty_if_blocks(lines):
        changed = True
        while changed:
            changed = False
            i = 0
            while i < len(lines):
                stripped = str(lines[i]).strip()
                if re.match(r'^if\s+', stripped, re.IGNORECASE):
                    depth = 0
                    j = i
                    empty = True
                    while j < len(lines):
                        cur = str(lines[j]).strip()
                        if re.match(r'^if\s+', cur, re.IGNORECASE):
                            depth += 1
                        elif cur.casefold() == 'endif':
                            depth -= 1
                            if depth == 0:
                                break
                        elif cur and not cur.startswith(';'):
                            empty = False
                        j += 1
                    if depth == 0 and empty:
                        del lines[i:j + 1]
                        changed = True
                        continue
                i += 1

    def process_texture_override_section(self, section_name, all_sections,
                                          material_group_to_swapkey, swap_key_prefix=None, next_swap_key_num=None,
                                          used_swap_keys=None, transparency_sections_to_add=None):
        from ..utils.log_utils import LOG as _LOG
        debug_disable_fx_ttl = bool(getattr(self, "debug_disable_fx_ttl", False))
        # 本次导出用哪一套 FX 命名空间：HIMI → HI3FX，NTEMI → NTEMIFX，其余 → RabbitFX。
        fx_profile = fx_namespace.profile_for_logic(getattr(GlobalConfig, "logic_name", ""))
        if material_group_to_swapkey is None or material_group_to_swapkey is Ellipsis:
            material_group_to_swapkey = {}
        if used_swap_keys is None or used_swap_keys is Ellipsis:
            used_swap_keys = set()
        if transparency_sections_to_add is None or transparency_sections_to_add is Ellipsis:
            transparency_sections_to_add = OrderedDict()
        if swap_key_prefix is Ellipsis:
            swap_key_prefix = None
        if next_swap_key_num is Ellipsis:
            next_swap_key_num = None
        if swap_key_prefix is None or next_swap_key_num is None:
            base_swap_var = getattr(self, "material_switch_var", "") or "$swapkey0"
            match = re.match(r'(\$\w+)(\d+)', str(base_swap_var))
            swap_key_prefix = match.group(1) if match else str(base_swap_var)
            next_swap_key_num = int(match.group(2)) if match else 0

        lines = all_sections[section_name]
        if any("CommandList\\TTL\\Draw" in str(line) for line in lines):
            return next_swap_key_num
        mesh_line = next((line for line in lines if self.extract_mesh_name(line)), "")
        section_mesh_name = self.extract_mesh_name(mesh_line) or ""
        section_obj = self.find_object_by_mesh_name(section_mesh_name) if section_mesh_name else None

        if section_mesh_name and section_obj is None:
            return next_swap_key_num

        ini_mapping = self.build_mapping_for_section(lines)
        material_candidate_filter = lambda candidate: self._object_has_matching_materials(candidate, ini_mapping)
        preserved_ps_slots = set()
        workspace_slots = OrderedDict()
        if getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False):
            if section_obj is not None:
                workspace_slots = self._collect_modimp_texture_slots(section_obj)
        diffuse_workspace_slot = self._find_first_slot_by_mark_name(workspace_slots, "DiffuseMap")
        if getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False) and diffuse_workspace_slot and diffuse_workspace_slot != "ps-t2":
            _self_alias_inserted = self._ensure_alias_assignment_in_lines(lines, diffuse_workspace_slot, "ps-t2")
            if _self_alias_inserted:
                ini_mapping["ps-t2"] = ini_mapping.get(diffuse_workspace_slot, "DiffuseMap")
                preserved_ps_slots.add("ps-t2")
        lines[:] = self._strip_generated_material_lines(lines, preserved_ps_slots=preserved_ps_slots)
        config_path = os.path.normpath(all_sections.get('_config_path', ''))
        workspace_texture_ini_folder = "Textures"
        workspace_texture_folder = os.path.join(config_path, workspace_texture_ini_folder)
        texture_folder = os.path.join(config_path, "Textures")
        object_to_diffuse_swapkey = {}

        mesh_lines_info_phase1 = [(i, self.extract_mesh_name(line)) for i, line in enumerate(lines) if self.extract_mesh_name(line)]

        for insert_index, mesh_name in reversed(mesh_lines_info_phase1):
            obj = self.find_object_by_mesh_name(mesh_name, object_filter=material_candidate_filter)
            if not obj:
                _LOG.info(f"      找到 '{mesh_name}', 所有候选均未匹配到材质")
                continue
            if not obj.material_slots:
                continue
            
            matched_types = []
            
            new_lines_for_this_mesh = []
            generated_zzmi_style, generated_glowmap, generated_fxmap = False, False, False
            generated_ttlmap = False
            generated_ps_slots = set()

            workspace_resource_by_slot = OrderedDict()
            diffuse_workspace_slot = ""
            for param_name, slot_info in self._collect_modimp_texture_slots(obj).items():
                texture_type = str(slot_info.get("mark_name", "") or "").strip()
                if not texture_type or texture_type == "FXMap":
                    continue
                if not diffuse_workspace_slot and texture_type == "DiffuseMap":
                    diffuse_workspace_slot = str(param_name or "").strip().lower()
                matching_materials = self._find_workspace_slot_materials(obj, slot_info)
                if not matching_materials:
                    continue
                matched_types.append(texture_type)
                generated_lines, next_swap_key_num = self.generate_material_lines(
                    matching_materials, param_name, texture_type, obj, workspace_texture_folder, all_sections,
                    object_to_diffuse_swapkey, material_group_to_swapkey,
                    swap_key_prefix, next_swap_key_num, used_swap_keys)
                if generated_lines:
                    new_lines_for_this_mesh.extend(generated_lines)
                    generated_ps_slots.add(param_name.lower())
                    workspace_resource_by_slot[param_name] = {
                        **dict(slot_info),
                        "resource_name": self._workspace_material_resource_name(matching_materials[0]),
                    }

            if (
                getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False)
                and diffuse_workspace_slot
                and diffuse_workspace_slot in generated_ps_slots
                and "ps-t2" not in generated_ps_slots
            ):
                diffuse_alias_lines = self._clone_generated_param_lines(
                    new_lines_for_this_mesh,
                    diffuse_workspace_slot,
                    "ps-t2",
                )
                if diffuse_alias_lines:
                    new_lines_for_this_mesh.extend(diffuse_alias_lines)
                    generated_ps_slots.add("ps-t2")
                    matched_types.append("DiffuseMap->ps-t2")

            is_pst_style = any(k.lower().startswith("ps-t") for k in ini_mapping.keys())
            is_zzmi_style = any(k.lower().startswith("resource\\zzmi\\") for k in ini_mapping.keys())
            is_fx_style = any(_starts_with_fx_resource_prefix(k) for k in ini_mapping.keys())
            # 段里真正出现过哪几套 FX 命名空间（决定要不要补 SetTextures，
            # 以及补哪一套的——各运行时的 SetTextures 不是同一个命令列表）。
            fx_profiles_in_section = []
            if is_pst_style or is_zzmi_style or is_fx_style:
                for param_name, texture_type in ini_mapping.items():
                    is_zzmi_param = param_name.lower().startswith("resource\\zzmi\\")
                    fx_param_profile = _fx_profile_for_param(param_name)
                    is_fx_param = fx_param_profile is not None

                    if not is_zzmi_param and not is_fx_param and not param_name.lower().startswith("ps-t"):
                        continue
                    if texture_type == "FXMap" and param_name.lower().startswith("ps-t"):
                        continue
                    if param_name.lower() in generated_ps_slots:
                        continue
                    matching_materials = self.find_matching_materials(obj, texture_type)
                    if matching_materials:
                        matched_types.append(texture_type)
                        if is_zzmi_param:
                            generated_zzmi_style = True
                        elif is_fx_param:
                            if fx_param_profile not in fx_profiles_in_section:
                                fx_profiles_in_section.append(fx_param_profile)
                        resource_name_provider = None
                        if param_name.lower().startswith("ps-t"):
                            resource_name_provider = lambda material, index: self._ps_texture_material_resource_name(material)
                        generated_lines, next_swap_key_num = self.generate_material_lines(
                            matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                            object_to_diffuse_swapkey, material_group_to_swapkey,
                            swap_key_prefix, next_swap_key_num, used_swap_keys,
                            resource_name_provider=resource_name_provider)
                        new_lines_for_this_mesh.extend(generated_lines)
                        if param_name.lower().startswith("ps-t"):
                            generated_ps_slots.add(param_name.lower())

            slot_materials = self._collect_ps_texture_slot_materials(obj)
            for param_name, slot_info in slot_materials.items():
                if param_name.lower() in generated_ps_slots:
                    continue
                texture_type = slot_info["texture_type"]
                matching_materials = list(slot_info["materials"].values())
                if not matching_materials:
                    continue
                matched_types.append(texture_type)
                resource_name_provider = lambda material, index: self._ps_texture_material_resource_name(material)
                generated_lines, next_swap_key_num = self.generate_material_lines(
                    matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                    object_to_diffuse_swapkey, material_group_to_swapkey,
                    swap_key_prefix, next_swap_key_num, used_swap_keys,
                    resource_name_provider=resource_name_provider)
                new_lines_for_this_mesh.extend(generated_lines)

            if (
                getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False)
                and diffuse_workspace_slot
                and "ps-t2" not in generated_ps_slots
            ):
                diffuse_alias_lines = self._clone_generated_param_lines(
                    new_lines_for_this_mesh,
                    diffuse_workspace_slot,
                    "ps-t2",
                )
                if diffuse_alias_lines:
                    new_lines_for_this_mesh.extend(diffuse_alias_lines)
                    generated_ps_slots.add("ps-t2")
                    matched_types.append("DiffuseMap->ps-t2")

            fxmap_lines = []
            # 贴图类型 → 资源别名。TTLMap 是「抖动半透明」专用的第二个遮罩通道，
            # 只有当前命名空间的档案声明了它（目前只有 HI3FX）才会产出：其它运行时
            # 没有这个概念，产出这条引用只会跑一个不存在的语义。
            fxmap_texture_types = [
                ('Glowmap', fx_profile.glow_ref),
                ('FXMap', fx_profile.fxmap_ref),
            ]
            if fx_profile.ttlmap_ref:
                fxmap_texture_types.append(('TTLMap', fx_profile.ttlmap_ref))
            for texture_type, param_name in fxmap_texture_types:
                if debug_disable_fx_ttl and texture_type in ('FXMap', 'TTLMap'):
                    continue
                matching_materials = self.find_matching_materials(obj, texture_type)
                if matching_materials:
                    matched_types.append(texture_type)
                    # 命名空间按当前执行逻辑取档案：RabbitFX / NTEMIFX / HI3FX。
                    # HIMI（崩坏 3）走 HI3FX：资源别名是 Resource\HI3FX\GlowMap /
                    # FXMap / TTLMap，变量名与 Run 命令列表都跟着换
                    # （见 blueprint/fx_namespace.py）。
                    if texture_type == 'Glowmap': generated_glowmap = True
                    if texture_type == 'FXMap': generated_fxmap = True
                    if texture_type == 'TTLMap': generated_ttlmap = True
                    generated_lines, next_swap_key_num = self.generate_material_lines(
                        matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                        object_to_diffuse_swapkey, material_group_to_swapkey,
                        swap_key_prefix, next_swap_key_num, used_swap_keys)
                    fxmap_lines.extend(generated_lines)
                    # Run 行对三种贴图类型是同一条命令列表：调用/使用方法不变，
                    # 只是引用行多了一种贴图类型。
                    fxmap_lines.append(fx_profile.run_line)

            ntemifx_lines = []
            ntemifx_texture_slots = {} if debug_disable_fx_ttl else self._collect_ntemifx_texture_slots(obj, workspace_resource_by_slot)
            for slot_label, resource_name in ntemifx_texture_slots.items():
                ntemifx_lines.append(f"Resource\\NTEMIFX\\FXMap = ref {resource_name}")
                ntemifx_lines.append("run = CommandList\\NTEMIFX\\Run")
                matched_types.append(f"NTEMIFX/{slot_label}")
            if ntemifx_texture_slots:
                reset_after_draw = []
                reset_after_draw.append("Resource\\NTEMIFX\\FXMap = ref null")
                reset_after_draw.append("run = CommandList\\NTEMIFX\\Run")
                reset_insert_idx = self._find_mesh_block_reset_insert_index(lines, insert_index)
                if reset_insert_idx != -1:
                    lines[reset_insert_idx:reset_insert_idx] = reset_after_draw
            
            if matched_types:
                _LOG.info(f"      找到 '{mesh_name}', 匹配材质: {', '.join(matched_types)}")
            else:
                _LOG.info(f"      找到 '{mesh_name}', 未匹配到材质")
            
            if generated_zzmi_style: new_lines_for_this_mesh.append("run = CommandList\\ZZMI\\SetTextures")
            # SetTextures 是「语义贴图重映射」，各运行时未必都提供（HI3FX 把它放在默认
            # 不加载的 HI3FX.Remap.ini 里），所以只补段里实际出现过、且档案声明了它的
            # 那一套命名空间。
            for fx_param_profile in fx_profiles_in_section:
                if fx_param_profile.set_textures_line:
                    new_lines_for_this_mesh.append(fx_param_profile.set_textures_line)
            new_lines_for_this_mesh.extend(fxmap_lines)
            new_lines_for_this_mesh.extend(ntemifx_lines)
            lines[insert_index + 1:insert_index + 1] = new_lines_for_this_mesh
            reset_lines = []
            if generated_glowmap:
                reset_lines.append(f"{fx_profile.glow_ref} = ref null")
                if fx_profile.reset_neutralises_variables:
                    # RabbitFX/NTEMIFX 的 Run 结尾会把 $h/$s/$v/$brightness 复位，
                    # 所以「写 0 + 再 Run 一次」就是复位。
                    reset_lines.append(f"{fx_profile.param('brightness')} = 0")
            if generated_fxmap:
                reset_lines.append(f"{fx_profile.fxmap_ref} = ref null")
            if generated_ttlmap:
                reset_lines.append(f"{fx_profile.ttlmap_ref} = ref null")
            if reset_lines:
                # HI3FX 的 Run 不复位变量，必须用 Reset（它自己会 null 别名 + Clean），
                # 否则 $brightness 会一直停在 0，后续绘制全黑。
                reset_lines.append(fx_profile.reset_line or fx_profile.run_line)
                reset_insert_idx = self._find_mesh_block_reset_insert_index(lines, insert_index)
                if reset_insert_idx != -1:
                    lines[reset_insert_idx:reset_insert_idx] = reset_lines
        # TTL 是 ZZMI 专属的绘制重建协议；EFMI 等其它逻辑仅执行普通材质转资源
        # 与 FX/Glowmap，不得因为某种 drawindexed 参数恰好能被正则解析就误入 TTL。
        # 别和 HI3FX 的 TTLMap 混淆：那是**第二个遮罩通道**（抖动半透明），属于
        # 普通材质转资源，由上面的 FX 段落产出，不涉及这里的绘制重建。
        ttl_supported = GlobalConfig.logic_name == LogicName.ZZMI
        if not debug_disable_fx_ttl and ttl_supported:
            next_swap_key_num = self._process_ttl_sections(
                section_name, lines, all_sections, material_group_to_swapkey,
                swap_key_prefix, next_swap_key_num, used_swap_keys, texture_folder)
        elif debug_disable_fx_ttl:
            _LOG.info(f"      调试开关已开启，跳过 {section_name} 的 FX/TTL 生成")

        mesh_lines_info_phase2 = [(i, self.extract_mesh_name(line)) for i, line in enumerate(lines) if self.extract_mesh_name(line)]
        for mesh_index, mesh_name in reversed(mesh_lines_info_phase2):
            transparency_shader_name, transparency_value = self.extract_transparency_info_from_mesh_name(mesh_name)
            if transparency_shader_name:
                base_shader_name = transparency_shader_name
                suffix = 2
                existing_section_names = {
                    str(existing_name or "").strip().strip('[]').casefold()
                    for existing_name in all_sections
                }
                generated_section_names = {
                    str(existing_name or "").casefold()
                    for existing_name in transparency_sections_to_add
                }
                while (
                    transparency_shader_name.casefold() in generated_section_names
                    or transparency_shader_name.casefold() in existing_section_names
                ):
                    transparency_shader_name = f"{base_shader_name}_{suffix}"
                    suffix += 1
                transparency_sections_to_add[transparency_shader_name] = [
                    "blend = ADD BLEND_FACTOR INV_BLEND_FACTOR",
                    f"blend_factor[0] = {transparency_value}", f"blend_factor[1] = {transparency_value}",
                    f"blend_factor[2] = {transparency_value}", "blend_factor[3] = 1",
                    "handling = skip",
                    "; --- Start of Overridden Mesh Content ---"
                ]
                lines.insert(mesh_index + 1, f"run = {transparency_shader_name}")
                start_move_idx = mesh_index + 2
                end_move_idx = len(lines)
                conditional_depth = 0
                draw_seen = False
                for i in range(start_move_idx, len(lines)):
                    stripped = lines[i].strip()
                    if self.extract_mesh_name(lines[i]) or (
                        stripped.startswith('[') and not stripped.startswith('[mesh:')
                    ):
                        end_move_idx = i
                        break
                    if re.match(r'^if\s+', stripped, re.IGNORECASE):
                        conditional_depth += 1
                    elif stripped.casefold() == 'endif':
                        conditional_depth = max(0, conditional_depth - 1)

                    if re.match(r'^drawindexed(?:instanced)?\s*=', stripped, re.IGNORECASE):
                        draw_seen = True

                    if draw_seen and conditional_depth == 0:
                        end_move_idx = i + 1
                        break
                if start_move_idx < end_move_idx:
                    block_to_move = lines[start_move_idx:end_move_idx]
                    reset_keywords = _FX_RESET_REF_LINES + _FX_RESET_PARAM_LINES
                    filtered_block_to_move = [
                        line for line in block_to_move
                        if not any(keyword in line for keyword in reset_keywords)
                    ]
                    final_block = []
                    for line in filtered_block_to_move:
                        if any(run_line in line for run_line in _FX_RUN_LINES):
                            # Run 只有在前面已经出现过资源别名时才搬过去：整段没有
                            # 别名绑定（只剩一条孤立的 Run）时它是空转，丢掉。
                            has_resource_before = any(
                                prefix in prev_line for prefix in _FX_RESOURCE_PREFIXES
                            )
                            if has_resource_before:
                                final_block.append(line)
                        else:
                            final_block.append(line)
                    transparency_sections_to_add[transparency_shader_name].extend(final_block)
                    del lines[start_move_idx:end_move_idx]
        return next_swap_key_num

    @staticmethod
    def _material_target_section_names(sections):
        """返回需要执行材质转换的 INI 段，包含 EFMI 合并骨骼绘制回调。

        普通导出把 ``[mesh:*]`` 与 draw 放在 ``TextureOverride`` 段内；EFMI
        合并骨骼则只在 EntryPoint 中挂载 ``Callback_Component_DrawCustom``，实际
        内容位于被引用的 ``CommandList_Draw_*``。这里只跟随该显式引用，避免把
        用户自定义或其它后处理生成的同名前缀 CommandList 误当成绘制段。
        """
        target_names = []
        seen_names = set()

        texture_override_names = [
            section_name
            for section_name in sections
            if section_name.startswith('[TextureOverride_')
        ]
        for section_name in texture_override_names:
            seen_names.add(section_name)
            target_names.append(section_name)

        section_lookup = {
            str(section_name).strip().strip('[]').casefold(): section_name
            for section_name in sections
            if str(section_name).strip().startswith('[')
            and str(section_name).strip().endswith(']')
        }
        callback_pattern = re.compile(
            r'^\s*CommandList\\EFMIv1\\Callback_Component_DrawCustom\s*=\s*'
            r'ref\s+([^;\s]+)',
            re.IGNORECASE,
        )
        for entrypoint_name in texture_override_names:
            for line in sections.get(entrypoint_name, []):
                callback_match = callback_pattern.match(str(line))
                if not callback_match:
                    continue
                referenced_name = callback_match.group(1).strip().strip('[]')
                section_name = section_lookup.get(referenced_name.casefold())
                if section_name is None or section_name in seen_names:
                    continue
                seen_names.add(section_name)
                target_names.append(section_name)

        return target_names

    def _collect_section_zzmi_slot_alias_map(self, lines):
        """汇总一个 TextureOverride 段内各部件槽位对应的 ZZMI 别名。

        SSMT/工作空间的槽位标记（mark_name）才是发光语义的权威来源：
        WengineFx / WengineFX 表示该 ps-t 槽应通过
        Resource\\ZZMI\\WengineFx 别名绑定；GlowMap 表示应通过
        Resource\\ZZMI\\GlowMap 绑定；ZglowMap 表示该贴图专用于 ZZMI 发光，
        同样通过 Resource\\ZZMI\\GlowMap 绑定，且不应再被当作 RabbitFX 发光
        重复处理。这里按 mesh 行找到对应物体并读取其工作空间槽位元数据，
        返回 {ps-tN(小写): 别名} 映射。
        """
        if GlobalConfig.logic_name != LogicName.ZZMI:
            return {}
        result = {}
        seen_params = set()
        for line in lines:
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            obj = self.find_object_by_mesh_name(mesh_name)
            if obj is None:
                continue
            try:
                slots = self._collect_modimp_texture_slots(obj)
            except Exception:
                slots = {}
            for param_name, slot_info in (slots or {}).items():
                key = str(param_name or "").strip().lower()
                if not key.startswith("ps-t") or key in seen_params:
                    continue
                mark_name = str(slot_info.get("mark_name", "") or "").strip().lower()
                if mark_name == "wenginefx":
                    result[key] = r"Resource\ZZMI\WengineFx"
                    seen_params.add(key)
                elif mark_name in ("glowmap", "glow", "zglowmap", "zglow"):
                    result[key] = r"Resource\ZZMI\GlowMap"
                    seen_params.add(key)
        return result

    def _normalize_zzmi_slot_lines(self, lines, slot_alias_map=None):
        """ZZMI 专用：把直接绑定 ps-tN 的发光槽位改写为资源别名。

        直接写 ps-tN = Resource... 会绕过 SlotFix 按 shader 的槽位分配。
        最终写盘前按 SSMT 标记决定别名：标记为 WengineFx / WengineFX 的槽位
        写成 Resource\\ZZMI\\WengineFx，标记为 GlowMap / ZglowMap（或资源名含
        GlowMap / ZglowMap）的槽位写成 Resource\\ZZMI\\GlowMap。
        """
        if GlobalConfig.logic_name != LogicName.ZZMI:
            return lines
        slot_alias_map = dict(slot_alias_map or {})
        converted = []
        for line in lines:
            stripped = str(line or "")
            match = re.match(
                r"^(\s*)ps-t\d+\s*=\s*(?:ref\s+)?(Resource[^\s=;]+?)\s*$",
                stripped,
                re.IGNORECASE,
            )
            if match:
                indent = match.group(1)
                resource_name = match.group(2)
                param_key = stripped.strip().split("=", 1)[0].strip().lower()
                alias = slot_alias_map.get(param_key)
                resource_lower = resource_name.lower()
                if alias is None:
                    if "wenginefx" in resource_lower:
                        alias = r"Resource\ZZMI\WengineFx"
                    elif "glowmap" in resource_lower:
                        alias = r"Resource\ZZMI\GlowMap"
                if alias is not None:
                    line = f"{indent}{alias} = ref {resource_name}"
            converted.append(line)
        return converted

    def execute_postprocess(self, mod_export_path, exporter=None):
        from ..utils.log_utils import LOG as _LOG

        self._ntmi_modimp_extra_ps_t2_diffuse_map = bool(
            getattr(exporter, "extra_ps_t2_diffuse_map", False)
        )

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            return

        _LOG.info("🔧 材质转资源节点开始执行")

        total_found = 0
        total_matched = 0

        for ini_file in ini_files:
            self._create_cumulative_backup(ini_file, mod_export_path)

            with open(ini_file, 'r', encoding='utf-8') as f:
                content = f.read()

            preserved_driver_content, content = self.split_anim_driver_block_content(content)
            content = self._strip_previous_transparency_sections(content)
            preserved_tail_content = ""
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)

            preamble_lines, sections = self._parse_ini_content(content)

            sections['_config_path'] = mod_export_path

            transparency_sections_to_add = OrderedDict()
            used_swap_keys = set()
            material_group_to_swapkey = {}
            base_swap_var = self.material_switch_var
            match = re.match(r'(\$\w+)(\d+)', base_swap_var)
            swap_key_prefix = match.group(1) if match else base_swap_var
            next_swap_key_num = int(match.group(2)) if match else 0

            for section_name in self._material_target_section_names(sections):
                next_swap_key_num = self.process_texture_override_section(
                    section_name, sections,
                    material_group_to_swapkey, swap_key_prefix, next_swap_key_num,
                    used_swap_keys, transparency_sections_to_add
                )

            del sections['_config_path']

            # 原神 GIMI ORFix 适配：只有检测到 ORFix 写法的段才会被重排，
            # 绝区零(ZZZ)/崩铁/NTEMI 等其它游戏的段落原样通过。
            _orfix_adapter = _load_gimi_orfix_adapter()
            if _orfix_adapter is not None:
                for _section_name in list(sections.keys()):
                    if not str(_section_name or "").lower().startswith("[textureoverride"):
                        continue
                    _section_lines = sections[_section_name]
                    if _orfix_adapter.uses_orfix(_section_lines):
                        sections[_section_name] = _orfix_adapter.place_orfix_runs(
                            _section_lines
                        )

            for _section_name in list(sections.keys()):
                if not str(_section_name or "").lower().startswith("[textureoverride"):
                    continue
                _section_lines = sections[_section_name]
                _slot_alias_map = self._collect_section_zzmi_slot_alias_map(_section_lines)
                sections[_section_name] = self._normalize_zzmi_slot_lines(
                    _section_lines,
                    slot_alias_map=_slot_alias_map,
                )

            self.define_swapkeys_in_sections(sections, used_swap_keys)

            new_content = self._serialize_ini_content(
                preamble_lines,
                sections,
                transparency_sections=transparency_sections_to_add,
                preserved_tail_content=preserved_tail_content,
                preserved_driver_content=preserved_driver_content,
            )

            with open(ini_file, 'w', encoding='utf-8', newline='\n') as f:
                f.write(new_content)

        _LOG.info(f"   ✅ 材质转资源节点执行完成")


class SSMTNode_PostProcess_Material(SSMTNode_PostProcess_MaterialBase):
    """材质转资源（弃用壳）。

    仅保留 bl_idname 用于加载旧版蓝图文件，避免未知节点类型导致节点被
    Blender 静默丢弃。功能已完全被「材质转资源pro」覆盖；addon 注册/文件
    加载后会自动迁移为 pro 节点（见 node_postprocess_custom_material_assign
    的 _migrate_legacy_material_nodes）。
    """
    bl_idname = 'SSMTNode_PostProcess_Material'
    bl_label = '材质转资源（已弃用）'
    bl_description = '已弃用：功能已被「材质转资源pro」完全覆盖，打开/保存后会自动迁移为 pro 节点。'

    def draw_buttons(self, context, layout):
        layout.label(
            text='已弃用：请改用「材质转资源pro」（本节点将自动迁移并转换）',
            icon='ERROR',
        )


classes = (
    MaterialPrefixItem,
    DetectedMaterialItem,
    SSMT_OT_MaterialDetectAddPrefix,
    SSMT_OT_MaterialDetectAddCustomPrefix,
    SSMT_OT_MaterialDetectRemovePrefix,
    SSMT_OT_MaterialDetect,
    SSMT_OT_MaterialDetectClear,
    SSMTNode_PostProcess_Material,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
