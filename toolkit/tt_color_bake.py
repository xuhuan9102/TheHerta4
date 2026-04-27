import bpy
import os
import re
import traceback
import bmesh
from pathlib import Path

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
    
    def has_complex_nodes(self, material, node_types):
        if not material or not material.use_nodes:
            return False
        output_node = next((n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and getattr(n, 'is_active_output', False)), None)
        if not output_node:
            output_node = next((n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if not output_node:
            return False
        check_shader = 'MIX_SHADER' in node_types or 'COMPLEX' in node_types
        check_color = 'MIX_COLOR' in node_types or 'COMPLEX' in node_types
        nodes_to_visit = {link.from_node for inp in output_node.inputs if inp.is_linked for link in inp.links}
        visited_nodes = {output_node}
        while nodes_to_visit:
            node = nodes_to_visit.pop()
            if node in visited_nodes:
                continue
            visited_nodes.add(node)
            if check_shader and node.type == 'MIX_SHADER':
                return True
            if check_color and (node.type == 'MIX_RGB' or (node.type == 'MIX' and getattr(node, 'data_type', '') == 'RGBA')):
                return True
            for inp in node.inputs:
                if inp.is_linked:
                    nodes_to_visit.update(link.from_node for link in inp.links if link.from_node not in visited_nodes)
        return False
    
    def unfold_mesh_by_uv(self, obj):
        """按照UV坐标展开网格顶点位置"""
        if obj.type != 'MESH':
            return None, None, None
        
        mesh = obj.data
        if not mesh.uv_layers.active:
            return None, None, None
        
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.transform(obj.matrix_world)
        bm.faces.ensure_lookup_table()
        
        uv_layer = bm.loops.layers.uv.verify()
        
        original_positions = {}
        for vert in bm.verts:
            original_positions[vert.index] = vert.co.copy()
        
        scale = 10.0
        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                loop.vert.co = (uv[0] * scale, uv[1] * scale, 0.0)
        
        new_mesh = bpy.data.meshes.new(f"{obj.name}_unfolded")
        bm.to_mesh(new_mesh)
        bm.free()
        
        return new_mesh, original_positions, None
    
    def render_material_preview(self, material, output_path, preview_type, size, unfold_by_uv=False, source_obj=None):
        original_scene = bpy.context.window.scene
        original_engine = original_scene.render.engine
        before_data = {
            'scenes': set(bpy.data.scenes), 
            'objects': set(bpy.data.objects), 
            'meshes': set(bpy.data.meshes), 
            'cameras': set(bpy.data.cameras), 
            'lights': set(bpy.data.lights)
        }
        
        try:
            temp_scene = bpy.data.scenes.new("TempMaterialRender_Scene")
            bpy.context.window.scene = temp_scene
            temp_scene.world = original_scene.world 
            temp_scene.render.engine = 'BLENDER_EEVEE_NEXT'
            temp_scene.eevee.taa_render_samples = 64
            temp_scene.view_settings.view_transform = 'Standard'
            
            if source_obj and unfold_by_uv:
                new_mesh, _, _ = self.unfold_mesh_by_uv(source_obj)
                if new_mesh:
                    new_obj = bpy.data.objects.new(f"{source_obj.name}_temp", new_mesh)
                    temp_scene.collection.objects.link(new_obj)
                    new_obj.data.materials.append(material)
                    render_obj = new_obj
                    cam_loc = (5.0, 5.0, 10.0)
                    cam_rot = (0.0, 0.0, 0.0)
                else:
                    render_obj, cam_loc, cam_rot = self._create_preview_primitive(preview_type)
                    for coll in list(render_obj.users_collection):
                        coll.objects.unlink(render_obj)
                    temp_scene.collection.objects.link(render_obj)
                    render_obj.data.materials.append(material)
            else:
                render_obj, cam_loc, cam_rot = self._create_preview_primitive(preview_type)
                for coll in list(render_obj.users_collection):
                    coll.objects.unlink(render_obj)
                temp_scene.collection.objects.link(render_obj)
                render_obj.data.materials.append(material)
            
            bpy.ops.object.camera_add(location=cam_loc, rotation=cam_rot)
            camera = bpy.context.active_object
            camera.data.type = 'ORTHO'
            
            if source_obj and unfold_by_uv:
                camera.data.ortho_scale = 10.0
            else:
                camera.data.ortho_scale = 2.0
            
            temp_scene.camera = camera
            temp_scene.render.resolution_x = size
            temp_scene.render.resolution_y = size
            temp_scene.render.film_transparent = True
            temp_scene.render.image_settings.file_format = 'PNG'
            temp_scene.render.filepath = output_path
            bpy.ops.render.render(write_still=True)
            return True
        except Exception as e:
            self.report({'ERROR'}, f"渲染预览失败: {e}")
            traceback.print_exc()
            return False
        finally:
            bpy.context.window.scene = original_scene
            original_scene.render.engine = original_engine
            
            for data_type, old_items in before_data.items():
                current_items = set(getattr(bpy.data, data_type))
                new_items = current_items - old_items
                for item in new_items:
                    try:
                        getattr(bpy.data, data_type).remove(item, do_unlink=True)
                    except:
                        pass
    
    def _create_preview_primitive(self, preview_type):
        if preview_type == 'FLAT':
            bpy.ops.mesh.primitive_plane_add(size=2)
            cam_loc, cam_rot = (0, 0, 2), (0, 0, 0)
        elif preview_type == 'SPHERE':
            bpy.ops.mesh.primitive_uv_sphere_add(radius=1)
            bpy.ops.object.shade_smooth()
            cam_loc, cam_rot = (0, -3, 0), (1.5708, 0, 0)
        elif preview_type == 'CUBE':
            bpy.ops.mesh.primitive_cube_add(size=1.5)
            cam_loc, cam_rot = (0, -3, 0), (1.5708, 0, 0)
        else:
            bpy.ops.mesh.primitive_monkey_add(size=1.5)
            cam_loc, cam_rot = (0, -3, 0), (1.5708, 0, 0)
        
        render_obj = bpy.context.active_object
        return render_obj, cam_loc, cam_rot
    
    def import_preview_to_material(self, material, preview_path):
        if not material.use_nodes:
            material.use_nodes = True
        
        preview_image = bpy.data.images.load(preview_path, check_existing=True)
        node_tree = material.node_tree
        output_node = next((n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and getattr(n, 'is_active_output', False)), None)
        if not output_node:
            output_node = next((n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if not output_node:
            output_node = node_tree.nodes.new('ShaderNodeOutputMaterial')
            output_node.location = (300, 0)
        
        surface_input = output_node.inputs.get('Surface')
        if not surface_input:
            return
        
        for link in list(surface_input.links):
            node_tree.links.remove(link)
        
        tex_node = node_tree.nodes.new('ShaderNodeTexImage')
        tex_node.image = preview_image
        tex_node.location = (output_node.location.x - 250, output_node.location.y)
        node_tree.links.new(tex_node.outputs['Color'], surface_input)

    def execute(self, context):
        props = context.scene.texture_tools_props
        
        if not props.output_dir:
            self.report({'ERROR'}, "请先设置输出目录")
            return {'CANCELLED'}
        
        output_dir = bpy.path.abspath(props.output_dir)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "未找到任何选中的网格物体")
            return {'CANCELLED'}
        
        mat_to_objects = {}
        for obj in selected_objects:
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    if slot.material not in mat_to_objects:
                        mat_to_objects[slot.material] = []
                    mat_to_objects[slot.material].append(obj)
        
        if not mat_to_objects:
            self.report({'ERROR'}, "未找到任何使用节点的选定材质")
            return {'CANCELLED'}
        
        exported_count = 0
        processed_materials = set()
        
        for material, objects in mat_to_objects.items():
            if material.name in processed_materials:
                continue
            
            should_bake = (props.color_bake_node_types == 'ALL') or self.has_complex_nodes(material, props.color_bake_node_types)
            
            if should_bake:
                safe_mat_name = "".join(c for c in material.name if c.isalnum() or c in ('-', '_', '.')).rstrip()
                output_path = os.path.join(output_dir, f"{safe_mat_name}.png")
                
                if output_path in processed_materials:
                    continue
                
                processed_materials.add(material.name)
                processed_materials.add(output_path)
                
                bake_size = props.color_bake_size
                
                if props.bake_resolution_use_rules:
                    for rule in props.bake_resolution_rules:
                        if not rule.enabled:
                            continue
                        if re.match(rule.pattern, material.name):
                            bake_size = rule.resolution
                            break
                
                source_obj = objects[0]
                
                if self.render_material_preview(material, output_path, props.color_bake_preview_type, bake_size,
                                                  unfold_by_uv=props.color_bake_unfold_by_uv,
                                                  source_obj=source_obj):
                    exported_count += 1
                    
                    if props.color_bake_import_to_material:
                        self.import_preview_to_material(material, output_path)
        
        self.report({'INFO'}, f"成功导出 {exported_count}/{len(mat_to_objects)} 个材质的贴图。")
        return {'FINISHED'}


tt_color_bake_list = (
    TT_OT_add_bake_resolution_rule,
    TT_OT_remove_bake_resolution_rule,
    TT_OT_reset_bake_resolution_rules,
    TT_OT_bake_color_maps,
)
