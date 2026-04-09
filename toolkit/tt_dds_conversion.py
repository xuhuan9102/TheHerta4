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
    bl_label = "转换为DDS"
    bl_description = "将选中的贴图转换为DDS格式"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        texconv_path = find_texconv()
        if not texconv_path:
            self.report({'ERROR'}, "未找到 texconv.exe，请将其放置到 Toolset 文件夹或手动指定路径")
            return {'CANCELLED'}
        
        props = context.scene.texture_tools_props
        output_dir = props.output_dir if props.output_dir else "//"
        
        converted_count = 0
        
        for img in bpy.data.images:
            if not img.filepath:
                continue
            
            source_path = bpy.path.abspath(img.filepath)
            if not os.path.exists(source_path):
                continue
            
            dds_format = self._get_dds_format(img.name, props)
            
            output_folder = bpy.path.abspath(output_dir) if output_dir != "//" else os.path.dirname(source_path)
            
            try:
                cmd = [
                    texconv_path,
                    "-f", dds_format,
                    "-o", output_folder,
                    "-y",
                    source_path
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
                
                if result.returncode == 0:
                    dds_path = os.path.join(output_folder, os.path.splitext(os.path.basename(source_path))[0] + ".dds")
                    
                    if props.dds_delete_originals:
                        try:
                            os.remove(source_path)
                        except:
                            pass
                    
                    converted_count += 1
                else:
                    print(f"DDS转换失败: {img.name} - {result.stderr}")
            
            except Exception as e:
                print(f"DDS转换异常: {img.name} - {str(e)}")
        
        self.report({'INFO'}, f"已转换 {converted_count} 个贴图为DDS格式")
        return {'FINISHED'}
    
    def _get_dds_format(self, image_name, props):
        import re
        
        if props.dds_use_custom_rules:
            for rule in props.dds_rules:
                if rule.enabled:
                    try:
                        if re.search(rule.pattern, image_name):
                            return rule.format
                    except:
                        continue
        
        for rule in DDS_DEFAULT_RULES:
            try:
                if re.search(rule["pattern"], image_name):
                    return rule["format"]
            except:
                continue
        
        return "bc7_unorm"


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
