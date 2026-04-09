import bpy
import os
import numpy as np
import tempfile
import random
import string

BAKE_RESOLUTION_DEFAULT_RULES = [
    {"pattern": r"(?i)(face|head)", "resolution": 4096, "enabled": True},
    {"pattern": r"(?i)(body|torso)", "resolution": 2048, "enabled": True},
    {"pattern": r"(?i)(hair)", "resolution": 2048, "enabled": True},
    {"pattern": r"(?i)(eye)", "resolution": 1024, "enabled": True},
]


class TT_OT_add_bake_resolution_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_add_bake_resolution_rule"
    bl_label = "添加烘焙分辨率规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        rule = props.bake_resolution_rules.add()
        rule.pattern = ".*"
        rule.resolution = 2048
        rule.enabled = True
        return {'FINISHED'}


class TT_OT_remove_bake_resolution_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_remove_bake_resolution_rule"
    bl_label = "移除烘焙分辨率规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        props.bake_resolution_rules.remove(self.index)
        return {'FINISHED'}


class TT_OT_reset_bake_resolution_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_reset_bake_resolution_rules"
    bl_label = "重置烘焙分辨率规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        props.bake_resolution_rules.clear()
        
        for rule_data in BAKE_RESOLUTION_DEFAULT_RULES:
            rule = props.bake_resolution_rules.add()
            rule.pattern = rule_data["pattern"]
            rule.resolution = rule_data["resolution"]
            rule.enabled = rule_data["enabled"]
        
        return {'FINISHED'}


class TT_OT_bake_color_maps(bpy.types.Operator):
    bl_idname = "toolkit.tt_bake_color_maps"
    bl_label = "烘焙颜色贴图"
    bl_description = "将选中物体的材质颜色烘焙为贴图"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        output_dir = props.output_dir
        if not output_dir:
            output_dir = tempfile.gettempdir()
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        bake_size = props.color_bake_size
        preview_type = props.color_bake_preview_type
        unfold_by_uv = props.color_bake_unfold_by_uv
        import_to_material = props.color_bake_import_to_material
        node_types = props.color_bake_node_types
        
        original_active = context.active_object
        original_mode = original_active.mode if original_active else 'OBJECT'
        
        processed_count = 0
        
        for obj in selected_objects:
            if not obj.data.materials:
                continue
            
            for mat_idx, mat in enumerate(obj.data.materials):
                if not mat or not mat.use_nodes:
                    continue
                
                mat_name = mat.name
                output_path = os.path.join(output_dir, f"{mat_name}.png")
                
                if preview_type == 'FLAT':
                    bpy.ops.mesh.primitive_plane_add(size=2)
                elif preview_type == 'SPHERE':
                    bpy.ops.mesh.primitive_uv_sphere_add(radius=1)
                elif preview_type == 'CUBE':
                    bpy.ops.mesh.primitive_cube_add(size=2)
                elif preview_type == 'MONKEY':
                    bpy.ops.mesh.primitive_monkey_add(size=2)
                
                preview_obj = context.active_object
                preview_obj.name = f"_BakePreview_{mat_name}"
                
                if not preview_obj.data.materials:
                    preview_obj.data.materials.append(mat)
                else:
                    preview_obj.data.materials[0] = mat
                
                if unfold_by_uv:
                    self._unfold_by_uv(preview_obj)
                
                self._bake_material(preview_obj, mat, output_path, bake_size)
                
                if import_to_material:
                    self._import_baked_to_material(mat, output_path)
                
                bpy.data.objects.remove(preview_obj, do_unlink=True)
                processed_count += 1
        
        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active
            if context.object.mode != original_mode:
                bpy.ops.object.mode_set(mode=original_mode)
        
        self.report({'INFO'}, f"已烘焙 {processed_count} 个材质")
        return {'FINISHED'}
    
    def _unfold_by_uv(self, obj):
        mesh = obj.data
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="UVMap")
        
        uv_layer = mesh.uv_layers.active.data
        
        for poly in mesh.polygons:
            for loop_idx in poly.loop_indices:
                vert_idx = mesh.loops[loop_idx].vertex_index
                vert = mesh.vertices[vert_idx]
                uv = uv_layer[loop_idx].uv
                vert.co.x = uv.x * 2 - 1
                vert.co.y = uv.y * 2 - 1
                vert.co.z = 0
    
    def _bake_material(self, obj, mat, output_path, bake_size):
        scene = bpy.context.scene
        scene.render.engine = 'CYCLES'
        scene.cycles.bake_type = 'EMIT'
        scene.render.bake.use_clear = True
        
        img = bpy.data.images.new(name=f"_BakeTemp_{mat.name}", width=bake_size, height=bake_size)
        img.filepath = output_path
        img.file_format = 'PNG'
        
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image and node.image.name == img.name:
                mat.node_tree.nodes.remove(node)
                break
        
        tex_node = mat.node_tree.nodes.new('ShaderNodeTexImage')
        tex_node.image = img
        tex_node.select = True
        mat.node_tree.nodes.active = tex_node
        
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.bake(type='EMIT')
        
        img.save()
        bpy.data.images.remove(img)
    
    def _import_baked_to_material(self, mat, output_path):
        if not os.path.exists(output_path):
            return
        
        img = bpy.data.images.load(output_path, check_existing=True)
        
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        for node in nodes:
            if node.type == 'TEX_IMAGE':
                node.image = img
                return
        
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.image = img
        tex_node.location = (-400, 0)
        
        bsdf = nodes.get("Principled BSDF")
        if bsdf:
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])


tt_color_bake_list = (
    TT_OT_add_bake_resolution_rule,
    TT_OT_remove_bake_resolution_rule,
    TT_OT_reset_bake_resolution_rules,
    TT_OT_bake_color_maps,
)
