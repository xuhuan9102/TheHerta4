import bpy
import os
import subprocess
import shutil

TOOLSET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'Toolset')

DDS_DEFAULT_RULES = [
    {"pattern": r"(?i)(diffuse|albedo|color|base)", "format": "bc7_unorm_srgb", "enabled": True},
    {"pattern": r"(?i)(normal|nrm|norm)", "format": "r8g8b8a8_unorm", "enabled": True},
    {"pattern": r"(?i)(roughness|rough|rm)", "format": "bc7_unorm", "enabled": True},
    {"pattern": r"(?i)(metallic|metal)", "format": "bc7_unorm", "enabled": True},
    {"pattern": r"(?i)(emissive|emit|glow)", "format": "bc7_unorm_srgb", "enabled": True},
    {"pattern": r"(?i)(ao|ambient|occlusion)", "format": "bc7_unorm", "enabled": True},
]


def find_texconv():
    props = bpy.context.scene.texture_tools_props
    
    local_path = os.path.join(TOOLSET_PATH, 'texconv.exe')
    if os.path.exists(local_path):
        return local_path
    
    if props and props.texconv_path and os.path.exists(props.texconv_path):
        return props.texconv_path
    
    system_path = shutil.which("texconv")
    if system_path:
        return system_path
    
    return None


class TT_OT_convert_to_dds(bpy.types.Operator):
    bl_idname = "toolkit.tt_convert_to_dds"
    bl_label = "批量转换为 .dds"
    bl_description = "使用texconv.exe将输出目录的图片转换为指定DDS格式，并更新项目引用"
    bl_options = {'REGISTER', 'UNDO'}
    
    def _get_dds_format(self, filename, props):
        import re
        
        if props.dds_use_custom_rules:
            for rule in props.dds_rules:
                if not rule.enabled:
                    continue
                try:
                    if re.search(rule.pattern, filename):
                        return rule.format
                except:
                    continue
        
        for rule in DDS_DEFAULT_RULES:
            try:
                if re.search(rule["pattern"], filename):
                    return rule["format"]
            except:
                continue
        
        return "bc7_unorm"
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        if not props.output_dir:
            self.report({'ERROR'}, "请先设置输出目录")
            return {'CANCELLED'}
        
        output_dir_abs = os.path.normpath(bpy.path.abspath(props.output_dir))
        if not os.path.isdir(output_dir_abs):
            self.report({'ERROR'}, f"输出目录不存在: {output_dir_abs}")
            return {'CANCELLED'}
        
        texconv_executable = find_texconv()
        if not texconv_executable:
            self.report({'ERROR'}, "未找到 texconv.exe。请将其放入插件目录的 'Toolset' 子文件夹，或手动指定路径。")
            return {'CANCELLED'}
        
        supported_extensions = {'.png', '.jpg', '.jpeg', '.tga', '.bmp'}
        conversion_map = {}
        converted_files_count = 0
        
        for root, _, files in os.walk(output_dir_abs):
            for filename in files:
                name_no_ext, ext = os.path.splitext(filename)
                if ext.lower() not in supported_extensions:
                    continue
                
                old_path = os.path.normpath(os.path.join(root, filename))
                
                if not old_path.startswith(output_dir_abs):
                    self.report({'WARNING'}, f"跳过输出目录外的文件: {filename}")
                    continue
                
                if os.path.normpath(old_path).startswith(os.path.normpath(bpy.path.abspath("//"))):
                    blend_dir = os.path.normpath(bpy.path.abspath("//"))
                    if old_path.startswith(blend_dir) and not old_path.startswith(output_dir_abs):
                        self.report({'WARNING'}, f"跳过工程目录内的源文件: {filename}")
                        continue
                
                new_path = os.path.normpath(os.path.join(root, f"{name_no_ext}.dds"))
                
                dds_format = self._get_dds_format(filename, props)
                
                command = [texconv_executable, "-f", dds_format, "-o", root, "-y", old_path]
                if "_srgb" in dds_format:
                    command.append("-srgb")
                
                try:
                    process = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore')
                    if process.returncode == 0:
                        conversion_map[old_path] = new_path
                        converted_files_count += 1
                        if props.dds_delete_originals:
                            try:
                                os.remove(old_path)
                            except:
                                pass
                    else:
                        self.report({'WARNING'}, f"转换文件 {filename} 失败: {process.stderr}")
                except Exception as e:
                    self.report({'WARNING'}, f"处理文件 {filename} 时出错: {e}")
                    continue
        
        if converted_files_count == 0:
            self.report({'INFO'}, "在输出目录中未找到支持的图片文件进行转换。")
            return {'CANCELLED'}
        
        updated_images_count = 0
        for image in bpy.data.images:
            if image.source == 'FILE' and image.filepath:
                try:
                    abs_filepath = os.path.normpath(bpy.path.abspath(image.filepath_raw))
                    if abs_filepath in conversion_map:
                        image.filepath = conversion_map[abs_filepath]
                        image.reload()
                        updated_images_count += 1
                except Exception as e:
                    self.report({'WARNING'}, f"更新图片 '{image.name}' 的路径时出错: {e}")
        
        self.report({'INFO'}, f"成功将 {converted_files_count} 个图片文件转换为 .dds 格式。更新了 {updated_images_count} 个图片引用。")
        return {'FINISHED'}


class TT_OT_add_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_add_dds_rule"
    bl_label = "添加DDS规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        rule = props.dds_rules.add()
        rule.pattern = ".*"
        rule.format = "bc7_unorm"
        rule.enabled = True
        return {'FINISHED'}


class TT_OT_remove_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_remove_dds_rule"
    bl_label = "移除DDS规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.remove(self.index)
        return {'FINISHED'}


class TT_OT_reset_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_reset_dds_rules"
    bl_label = "重置DDS规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.clear()
        
        for rule_data in DDS_DEFAULT_RULES:
            rule = props.dds_rules.add()
            rule.pattern = rule_data["pattern"]
            rule.format = rule_data["format"]
            rule.enabled = rule_data["enabled"]
        
        return {'FINISHED'}


class TT_OT_test_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_test_dds_rule"
    bl_label = "测试DDS规则"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        import re
        
        test_names = ["DiffuseMap_Body", "NormalMap_Face", "RoughnessMap_Hair", "EmissiveMap_Eye"]
        
        result_lines = ["DDS规则测试结果:"]
        
        for name in test_names:
            matched_format = "bc7_unorm"
            
            if props.dds_use_custom_rules:
                for rule in props.dds_rules:
                    if rule.enabled:
                        try:
                            if re.search(rule.pattern, name):
                                matched_format = rule.format
                                break
                        except:
                            pass
            else:
                for rule in DDS_DEFAULT_RULES:
                    try:
                        if re.search(rule["pattern"], name):
                            matched_format = rule["format"]
                            break
                    except:
                        pass
            
            result_lines.append(f"  {name} -> {matched_format}")
        
        self.report({'INFO'}, "\n".join(result_lines))
        return {'FINISHED'}


class TT_OT_save_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_save_dds_rules"
    bl_label = "保存DDS规则"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        import json
        
        if not props.dds_rules_file_path:
            self.report({'ERROR'}, "请先指定规则文件路径")
            return {'CANCELLED'}
        
        rules_data = []
        for rule in props.dds_rules:
            rules_data.append({
                "pattern": rule.pattern,
                "format": rule.format,
                "enabled": rule.enabled
            })
        
        try:
            with open(props.dds_rules_file_path, 'w', encoding='utf-8') as f:
                json.dump(rules_data, f, indent=2, ensure_ascii=False)
            self.report({'INFO'}, f"规则已保存到: {props.dds_rules_file_path}")
        except Exception as e:
            self.report({'ERROR'}, f"保存失败: {str(e)}")
            return {'CANCELLED'}
        
        return {'FINISHED'}


class TT_OT_load_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_load_dds_rules"
    bl_label = "加载DDS规则"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        import json
        
        if not props.dds_rules_file_path:
            self.report({'ERROR'}, "请先指定规则文件路径")
            return {'CANCELLED'}
        
        if not os.path.exists(props.dds_rules_file_path):
            self.report({'ERROR'}, "规则文件不存在")
            return {'CANCELLED'}
        
        try:
            with open(props.dds_rules_file_path, 'r', encoding='utf-8') as f:
                rules_data = json.load(f)
            
            props.dds_rules.clear()
            
            for rule_data in rules_data:
                rule = props.dds_rules.add()
                rule.pattern = rule_data.get("pattern", ".*")
                rule.format = rule_data.get("format", "bc7_unorm")
                rule.enabled = rule_data.get("enabled", True)
            
            self.report({'INFO'}, f"已加载 {len(rules_data)} 条规则")
        except Exception as e:
            self.report({'ERROR'}, f"加载失败: {str(e)}")
            return {'CANCELLED'}
        
        return {'FINISHED'}


tt_dds_conversion_list = (
    TT_OT_convert_to_dds,
    TT_OT_add_dds_rule,
    TT_OT_remove_dds_rule,
    TT_OT_reset_dds_rules,
    TT_OT_test_dds_rule,
    TT_OT_save_dds_rules,
    TT_OT_load_dds_rules,
)
