import bpy
import os
import glob
import re
from collections import OrderedDict
import shutil

from .node_postprocess_base import SSMTNode_PostProcess_Base

_name_mapping_cache = {}
_reverse_name_mapping_cache = {}


def clear_name_mapping_cache():
    global _name_mapping_cache, _reverse_name_mapping_cache
    _name_mapping_cache.clear()
    _reverse_name_mapping_cache.clear()


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
    "StockingMap",
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
    bl_label = "检测材质"
    bl_description = "检测节点树中连接的物体是否缺少指定前缀的材质"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def _find_result_output(self, node, visited=None):
        if visited is None:
            visited = set()
        tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
        node_key = f"{tree_name}::{node.name}"
        if node_key in visited:
            return None
        visited.add(node_key)

        if node.bl_idname == 'SSMTNode_Result_Output':
            return node

        for input_socket in node.inputs:
            if input_socket.bl_idname == 'SSMTSocketPostProcess' and input_socket.is_linked:
                for link in input_socket.links:
                    result = self._find_result_output(link.from_node, visited)
                    if result:
                        return result
        return None

    def _collect_object_info_nodes(self, node, visited=None):
        if visited is None:
            visited = set()
        tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
        node_key = f"{tree_name}::{node.name}"
        if node_key in visited:
            return []
        visited.add(node_key)

        obj_info_nodes = []
        if node.bl_idname == 'SSMTNode_Object_Info':
            obj_info_nodes.append(node)

        for input_socket in node.inputs:
            if input_socket.is_linked:
                for link in input_socket.links:
                    obj_info_nodes.extend(self._collect_object_info_nodes(link.from_node, visited))
        return obj_info_nodes

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            self.report({'WARNING'}, "无法获取节点树")
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != 'SSMTNode_PostProcess_Material':
            return {'CANCELLED'}

        prefixes = [item.prefix.strip() for item in node.material_detect_prefixes if item.prefix.strip()]
        if not prefixes:
            self.report({'WARNING'}, "请先添加至少一个检测前缀")
            return {'CANCELLED'}

        result_output = self._find_result_output(node)
        if not result_output:
            self.report({'WARNING'}, "未找到连接的 Result_Output 节点")
            return {'CANCELLED'}

        obj_info_nodes = self._collect_object_info_nodes(result_output)
        if not obj_info_nodes:
            self.report({'WARNING'}, "未找到连接的物体节点")
            return {'CANCELLED'}

        node.detected_materials.clear()

        missing_count = 0
        for oi_node in obj_info_nodes:
            obj_name = getattr(oi_node, 'object_name', '')
            if not obj_name:
                continue
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

                if not has_prefix_material:
                    item = node.detected_materials.add()
                    item.object_name = obj_name
                    item.missing_prefix = prefix
                    missing_count += 1

        node.detect_all_ok = (missing_count == 0)

        if missing_count > 0:
            self.report({'WARNING'}, f"检测完成: {missing_count} 个缺失 (来自 {len(obj_info_nodes)} 个物体)")
        else:
            self.report({'INFO'}, f"检测完成: 全部正确 ({len(obj_info_nodes)} 个物体)")
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


class SSMTNode_PostProcess_Material(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_Material'
    bl_label = '材质转资源'
    bl_description = '根据场景物体的材质和纹理创建资源引用'

    @staticmethod
    def clear_cache():
        clear_name_mapping_cache()

    material_to_resource_override: bpy.props.BoolProperty(
        name="覆盖现有资源",
        description="如果资源已存在，则覆盖它",
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

    def find_object_by_mesh_name(self, mesh_name):
        from ..utils.log_utils import LOG as _LOG
        _LOG.debug(f"[find_object_by_mesh_name] 输入 mesh_name: '{mesh_name}'")
        
        reverse_mapping = self._get_reverse_name_mapping()
        name_mapping = self._get_name_mapping()
        _LOG.debug(f"  reverse_mapping 条目数: {len(reverse_mapping)}")
        _LOG.debug(f"  name_mapping 条目数: {len(name_mapping)}")

        potential_names = []

        suffix_patterns = [r'_chain\d+_copy$', r'_chain\d+$', r'_copy$']
        for pattern in suffix_patterns:
            base_name = re.sub(pattern, '', mesh_name)
            if base_name != mesh_name and base_name not in potential_names:
                potential_names.append(base_name)
                _LOG.debug(f"  后缀模式 '{pattern}' 生成: '{base_name}'")

        potential_names.append(mesh_name)
        _LOG.debug(f"  初始 potential_names: {potential_names}")

        if reverse_mapping:
            for new_name, original_name in reverse_mapping.items():
                if new_name == mesh_name:
                    if original_name not in potential_names:
                        potential_names.append(original_name)
                        _LOG.debug(f"  反向映射精确匹配: '{new_name}' -> '{original_name}'")
                    for pattern in suffix_patterns:
                        original_base = re.sub(pattern, '', original_name)
                        if original_base != original_name and original_base not in potential_names:
                            potential_names.append(original_base)
                            _LOG.debug(f"  反向映射+后缀: '{original_name}' -> '{original_base}'")
                elif new_name in mesh_name:
                    original_mesh_name = mesh_name.replace(new_name, original_name)
                    if original_mesh_name not in potential_names:
                        potential_names.append(original_mesh_name)
                        _LOG.debug(f"  反向映射部分匹配: '{new_name}' in '{mesh_name}' -> '{original_mesh_name}'")
                    for pattern in suffix_patterns:
                        original_base = re.sub(pattern, '', original_mesh_name)
                        if original_base != original_mesh_name and original_base not in potential_names:
                            potential_names.append(original_base)
                            _LOG.debug(f"  反向映射部分+后缀: '{original_mesh_name}' -> '{original_base}'")

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
                _LOG.debug(f"  ✅ 找到物体: '{name}'")
                return obj
            else:
                _LOG.debug(f"  ❌ 未找到物体: '{name}'")

        clean_name = re.sub(r'^[a-f0-9]+-[\d]+-', '', mesh_name)
        _LOG.debug(f"  清理前缀后的名称: '{clean_name}' (原: '{mesh_name}')")
        if clean_name != mesh_name:
            potential_clean_names = []

            for pattern in suffix_patterns:
                base_clean_name = re.sub(pattern, '', clean_name)
                if base_clean_name != clean_name and base_clean_name not in potential_clean_names:
                    potential_clean_names.append(base_clean_name)
                    _LOG.debug(f"    清理后后缀模式 '{pattern}' 生成: '{base_clean_name}'")

            potential_clean_names.append(clean_name)
            _LOG.debug(f"    初始 potential_clean_names: {potential_clean_names}")

            if name_mapping:
                for original_name, new_name in name_mapping.items():
                    if original_name in clean_name:
                        renamed_clean_name = clean_name.replace(original_name, new_name)
                        if renamed_clean_name not in potential_clean_names:
                            potential_clean_names.append(renamed_clean_name)
                            _LOG.debug(f"    清理后正向映射: '{original_name}' -> '{new_name}' -> '{renamed_clean_name}'")
                        for pattern in suffix_patterns:
                            renamed_base = re.sub(pattern, '', renamed_clean_name)
                            if renamed_base != renamed_clean_name and renamed_base not in potential_clean_names:
                                potential_clean_names.append(renamed_base)
                                _LOG.debug(f"    清理后正向映射+后缀: '{renamed_clean_name}' -> '{renamed_base}'")

            if reverse_mapping:
                for new_name, original_name in reverse_mapping.items():
                    original_clean = re.sub(r'^[a-f0-9]+-[\d]+-', '', original_name)
                    if original_clean and original_clean not in potential_clean_names:
                        potential_clean_names.append(original_clean)
                        _LOG.debug(f"    反向映射原始名清理前缀: '{original_name}' -> '{original_clean}'")
                        for pattern in suffix_patterns:
                            original_clean_base = re.sub(pattern, '', original_clean)
                            if original_clean_base != original_clean and original_clean_base not in potential_clean_names:
                                potential_clean_names.append(original_clean_base)
                                _LOG.debug(f"    反向映射原始名清理+后缀: '{original_clean}' -> '{original_clean_base}'")
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
            shader_name = f"CustomShaderTransparencyCloth{base_name.replace('-', '_').replace('.', '_')}"
            return shader_name, transparency_value
        return None, None

    def extract_texture_type_from_resource(self, resource_name):
        match = re.search(r'_Slot_([^_]+)$', resource_name)
        if match:
            return match.group(1)

        match = re.search(r'Resource-.*-([^_-]+)$', resource_name)
        if match:
            return match.group(1)

        return None

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
        matching_materials = []
        texture_type_lower = texture_type.lower()
        for material_slot in obj.material_slots:
            material = material_slot.material
            if not material: continue
            material_first_word = material.name.split('_')[0].lower()
            if material_first_word == texture_type_lower:
                matching_materials.append(material)
        
        return matching_materials

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

    def copy_texture_file(self, texture_image, target_folder, material):
        if not texture_image or not texture_image.filepath:
            return None
        try:
            source_path = bpy.path.abspath(texture_image.filepath)
            if not os.path.exists(source_path): return None
            os.makedirs(target_folder, exist_ok=True)

            _, file_extension = os.path.splitext(os.path.basename(source_path))
            new_filename = f"{material.name}{file_extension}"

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
                                swap_key_prefix, next_swap_key_num, used_swap_keys):
        generated_lines = []
        brightness_param_name = r"$\RabbitFX\brightness"

        if len(matching_materials) == 1:
            material = matching_materials[0]
            if texture_type == 'Glowmap':
                match = re.search(r'^Glowmap_(\d+)_', material.name, re.IGNORECASE)
                if match:
                    generated_lines.append(f"{brightness_param_name} = {match.group(1)}")

            new_line = self.create_resource_entry(material, param_name, texture_folder, all_sections)
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
                new_line = self.create_resource_entry(material, param_name, texture_folder, all_sections)
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

    def create_resource_entry(self, material, param_name, texture_folder, all_sections):
        texture_image = self.get_texture_from_material(material)
        if not texture_image: return None
        copied_filename = self.copy_texture_file(texture_image, texture_folder, material)
        if not copied_filename: return None
        new_resource_name = f"Resource_{material.name}"
        resource_section_name = f"[{new_resource_name}]"
        if resource_section_name not in all_sections or self.material_to_resource_override:
            all_sections[resource_section_name] = [f"filename = Texture/{copied_filename}".replace("\\", "/")]
        return "{} = {}".format(param_name, new_resource_name) if not param_name.lower().startswith("resource\\") else "{} = ref {}".format(param_name, new_resource_name)

    def process_texture_override_section(self, section_name, all_sections,
                                          material_group_to_swapkey, swap_key_prefix, next_swap_key_num,
                                          used_swap_keys, transparency_sections_to_add):
        from ..utils.log_utils import LOG as _LOG
        lines = all_sections[section_name]
        ini_mapping = self.build_mapping_for_section(lines)
        texture_folder = os.path.join(os.path.dirname(all_sections.get('_config_path', '')), "Texture")
        object_to_diffuse_swapkey = {}

        mesh_lines_info_phase1 = [(i, self.extract_mesh_name(line)) for i, line in enumerate(lines) if self.extract_mesh_name(line)]
        
        for insert_index, mesh_name in reversed(mesh_lines_info_phase1):
            obj = self.find_object_by_mesh_name(mesh_name)
            if not obj:
                continue
            if not obj.material_slots:
                continue
            
            matched_types = []
            
            new_lines_for_this_mesh = []
            generated_zzmi_style, generated_rabbitfx_style, generated_glowmap, generated_fxmap = False, False, False, False
            is_pst_style = any(k.lower().startswith("ps-t") for k in ini_mapping.keys())
            is_zzmi_style = any(k.lower().startswith("resource\\zzmi\\") for k in ini_mapping.keys())
            is_rabbitfx_style = any(k.lower().startswith("resource\\rabbitfx\\") for k in ini_mapping.keys())
            if is_pst_style or is_zzmi_style or is_rabbitfx_style:
                for param_name, texture_type in ini_mapping.items():
                    is_zzmi_param = param_name.lower().startswith("resource\\zzmi\\")
                    is_rabbitfx_param = param_name.lower().startswith("resource\\rabbitfx\\")

                    if not is_zzmi_param and not is_rabbitfx_param and not param_name.lower().startswith("ps-t"):
                        continue
                    matching_materials = self.find_matching_materials(obj, texture_type)
                    if matching_materials:
                        matched_types.append(texture_type)
                        if is_zzmi_param:
                            generated_zzmi_style = True
                        elif is_rabbitfx_param:
                            generated_rabbitfx_style = True
                        generated_lines, next_swap_key_num = self.generate_material_lines(
                            matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                            object_to_diffuse_swapkey, material_group_to_swapkey,
                            swap_key_prefix, next_swap_key_num, used_swap_keys)
                        new_lines_for_this_mesh.extend(generated_lines)
            fxmap_lines = []
            for texture_type in ['Glowmap', 'FXMap']:
                matching_materials = self.find_matching_materials(obj, texture_type)
                if matching_materials:
                    matched_types.append(texture_type)
                    param_name = f"Resource\\RabbitFX\\{texture_type}"
                    if texture_type == 'Glowmap': generated_glowmap = True
                    if texture_type == 'FXMap': generated_fxmap = True
                    generated_lines, next_swap_key_num = self.generate_material_lines(
                        matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                        object_to_diffuse_swapkey, material_group_to_swapkey,
                        swap_key_prefix, next_swap_key_num, used_swap_keys)
                    fxmap_lines.extend(generated_lines)
                    fxmap_lines.append("run = CommandList\\RabbitFX\\Run")
            
            if matched_types:
                _LOG.info(f"      找到 '{mesh_name}', 匹配材质: {', '.join(matched_types)}")
            else:
                _LOG.info(f"      找到 '{mesh_name}', 未匹配到材质")
            
            if generated_zzmi_style: new_lines_for_this_mesh.append("run = CommandList\\ZZMI\\SetTextures")
            if generated_rabbitfx_style: new_lines_for_this_mesh.append("run = CommandList\\RabbitFX\\SetTextures")
            new_lines_for_this_mesh.extend(fxmap_lines)
            lines[insert_index + 1:insert_index + 1] = new_lines_for_this_mesh
            reset_lines = []
            if generated_glowmap: reset_lines.extend(["Resource\\RabbitFX\\Glowmap = ref null", r"$\RabbitFX\brightness = 0"])
            if generated_fxmap: reset_lines.append("Resource\\RabbitFX\\FXMap = ref null")
            if reset_lines:
                reset_lines.append("run = CommandList\\RabbitFX\\Run")
                draw_idx = -1
                search_end_idx = len(lines)
                for i in range(insert_index + 1, len(lines)):
                    if '[mesh:' in lines[i]:
                        search_end_idx = i
                        break
                for i in range(insert_index + 1, search_end_idx):
                    if 'drawindexed' in lines[i]:
                        draw_idx = i
                        break
                if draw_idx != -1:
                    lines[draw_idx + 1:draw_idx + 1] = reset_lines
        mesh_lines_info_phase2 = [(i, self.extract_mesh_name(line)) for i, line in enumerate(lines) if self.extract_mesh_name(line)]
        for mesh_index, mesh_name in reversed(mesh_lines_info_phase2):
            transparency_shader_name, transparency_value = self.extract_transparency_info_from_mesh_name(mesh_name)
            if transparency_shader_name:
                if transparency_shader_name not in transparency_sections_to_add:
                    transparency_sections_to_add[transparency_shader_name] = [
                        "blend = ADD BLEND_FACTOR INV_BLEND_FACTOR",
                        f"blend_factor[0] = {transparency_value}", f"blend_factor[1] = {transparency_value}",
                        f"blend_factor[2] = {transparency_value}", "blend_factor[3] = 1",
                        "handling = skip",
                        "; --- Start of Overridden Mesh Content ---"
                    ]
                lines.insert(mesh_index + 1, f"run = {transparency_shader_name}")
                start_move_idx = mesh_index + 2
                end_move_idx = -1
                for i in range(start_move_idx, len(lines)):
                    if 'drawindexed' in lines[i]:
                        end_move_idx = i + 1
                        break
                    if '[mesh:' in lines[i] or (lines[i].strip().startswith('[') and not lines[i].strip().startswith('[mesh:')):
                        end_move_idx = i
                        break
                if end_move_idx == -1: end_move_idx = len(lines)
                if start_move_idx < end_move_idx:
                    block_to_move = lines[start_move_idx:end_move_idx]
                    filtered_block_to_move = [
                        line for line in block_to_move
                        if not any(keyword in line for keyword in ["Resource\\RabbitFX\\Glowmap = ref null", r"$\RabbitFX\brightness = 0", "Resource\\RabbitFX\\FXMap = ref null"])
                    ]
                    final_block = []
                    for line in filtered_block_to_move:
                        if "run = CommandList\\RabbitFX\\Run" in line:
                            has_resource_before = any("Resource\\RabbitFX" in prev_line for prev_line in final_block)
                            if has_resource_before:
                                final_block.append(line)
                        else:
                            final_block.append(line)
                    transparency_sections_to_add[transparency_shader_name].extend(final_block)
                    del lines[start_move_idx:end_move_idx]
        return next_swap_key_num

    def execute_postprocess(self, mod_export_path):
        from ..utils.log_utils import LOG as _LOG

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

            preserved_tail_content = ""
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)

            sections = OrderedDict()
            current_section = None
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped
                    sections[current_section] = []
                elif current_section:
                    sections[current_section].append(line)

            sections['_config_path'] = mod_export_path

            transparency_sections_to_add = OrderedDict()
            used_swap_keys = set()
            material_group_to_swapkey = {}
            base_swap_var = self.material_switch_var
            match = re.match(r'(\$\w+)(\d+)', base_swap_var)
            swap_key_prefix = match.group(1) if match else base_swap_var
            next_swap_key_num = int(match.group(2)) if match else 0

            for section_name, lines in list(sections.items()):
                if section_name.startswith('[TextureOverride_') and section_name != '_config_path':
                    next_swap_key_num = self.process_texture_override_section(
                        section_name, sections,
                        material_group_to_swapkey, swap_key_prefix, next_swap_key_num,
                        used_swap_keys, transparency_sections_to_add
                    )

            del sections['_config_path']

            self.define_swapkeys_in_sections(sections, used_swap_keys)

            new_content = []
            for section_name, lines in sections.items():
                new_content.append(section_name)
                new_content.extend(lines)
                new_content.append('')

            if transparency_sections_to_add:
                new_content.append('\n;MARK:CustomShaderTransparency----------------------------------------------------------')
                for shader_name, lines in transparency_sections_to_add.items():
                    new_content.append(f"[{shader_name}]")
                    new_content.extend(lines)
                    new_content.append('')

            if preserved_tail_content:
                new_content.append('')
                new_content.append(preserved_tail_content)

            with open(ini_file, 'w', encoding='utf-8') as f:
                f.write("\n".join(new_content))

        _LOG.info(f"   ✅ 材质转资源节点执行完成")


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
