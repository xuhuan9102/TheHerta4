import bpy
import re
import random
import string

class TT_MaterialPreviewItem(bpy.types.PropertyGroup):
    material: bpy.props.PointerProperty(type=bpy.types.Material, name="材质")
    plane_object: bpy.props.StringProperty(name="平面物体名称")
    is_visible: bpy.props.BoolProperty(name="可见", default=True)
    source_objects: bpy.props.StringProperty(name="源物体列表")


class TT_OT_refresh_materials(bpy.types.Operator):
    bl_idname = "toolkit.tt_refresh_materials"
    bl_label = "刷新材质列表"
    bl_description = "根据正则表达式匹配选中物体的材质并创建预览平面"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        pattern = props.material_preview_pattern
        
        try:
            regex = re.compile(pattern)
        except re.error:
            self.report({'ERROR'}, "无效的正则表达式")
            return {'CANCELLED'}
        
        context.scene.material_preview_list.clear()
        
        selected_objects = context.selected_objects
        matched_materials = set()
        
        for obj in selected_objects:
            if obj.type != 'MESH':
                continue
            
            for mat_slot in obj.material_slots:
                mat = mat_slot.material
                if not mat:
                    continue
                
                if regex.search(mat.name):
                    matched_materials.add(mat)
        
        if not matched_materials:
            self.report({'WARNING'}, "未找到匹配的材质")
            return {'CANCELLED'}
        
        for mat in matched_materials:
            item = context.scene.material_preview_list.add()
            item.material = mat
            
            plane = self._create_preview_plane(mat)
            item.plane_object = plane.name
            
            source_obj_names = []
            for obj in selected_objects:
                if obj.type == 'MESH':
                    for slot in obj.material_slots:
                        if slot.material == mat:
                            source_obj_names.append(obj.name)
                            break
            
            item.source_objects = "|".join(source_obj_names)
        
        self.report({'INFO'}, f"已创建 {len(matched_materials)} 个材质预览")
        return {'FINISHED'}
    
    def _create_preview_plane(self, material):
        name = f"_Preview_{material.name}"
        
        if name in bpy.data.objects:
            bpy.data.objects.remove(bpy.data.objects[name], do_unlink=True)
        
        bpy.ops.mesh.primitive_plane_add(size=2)
        plane = bpy.context.active_object
        plane.name = name
        
        if not plane.data.materials:
            plane.data.materials.append(material)
        else:
            plane.data.materials[0] = material
        
        return plane


class TT_OT_clear_all_previews(bpy.types.Operator):
    bl_idname = "toolkit.tt_clear_all_previews"
    bl_label = "清除所有预览"
    bl_description = "清除所有材质预览平面"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        removed_count = 0
        
        for item in context.scene.material_preview_list:
            if item.plane_object:
                plane = bpy.data.objects.get(item.plane_object)
                if plane:
                    bpy.data.objects.remove(plane, do_unlink=True)
                    removed_count += 1
        
        context.scene.material_preview_list.clear()
        
        self.report({'INFO'}, f"已清除 {removed_count} 个预览平面")
        return {'FINISHED'}


class TT_OT_toggle_visibility(bpy.types.Operator):
    bl_idname = "toolkit.tt_toggle_visibility"
    bl_label = "切换可见性"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        items = context.scene.material_preview_list
        if self.index < 0 or self.index >= len(items):
            return {'CANCELLED'}
        
        item = items[self.index]
        item.is_visible = not item.is_visible
        
        if item.plane_object:
            plane = bpy.data.objects.get(item.plane_object)
            if plane:
                plane.hide_set(not item.is_visible)
        
        return {'FINISHED'}


class TT_OT_select_plane(bpy.types.Operator):
    bl_idname = "toolkit.tt_select_plane"
    bl_label = "选中平面"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        items = context.scene.material_preview_list
        if self.index < 0 or self.index >= len(items):
            return {'CANCELLED'}
        
        item = items[self.index]
        
        if item.plane_object:
            plane = bpy.data.objects.get(item.plane_object)
            if plane:
                bpy.ops.object.select_all(action='DESELECT')
                plane.select_set(True)
                context.view_layer.objects.active = plane
        
        return {'FINISHED'}


class TT_OT_update_from_planes(bpy.types.Operator):
    bl_idname = "toolkit.tt_update_from_planes"
    bl_label = "从平面更新UV"
    bl_description = "根据平面的实际位置和缩放更新UV"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        updated_count = 0
        
        for item in context.scene.material_preview_list:
            if not item.plane_object:
                continue
            
            plane = bpy.data.objects.get(item.plane_object)
            if not plane:
                continue
            
            mesh = plane.data
            
            if not mesh.uv_layers:
                mesh.uv_layers.new(name="UVMap")
            
            uv_layer = mesh.uv_layers.active.data
            
            scale_x = plane.scale.x
            scale_y = plane.scale.y
            offset_x = plane.location.x
            offset_y = plane.location.y
            
            for poly in mesh.polygons:
                for loop_idx in poly.loop_indices:
                    vert_idx = mesh.loops[loop_idx].vertex_index
                    vert = mesh.vertices[vert_idx]
                    
                    u = (vert.co.x + 1) * 0.5 * scale_x + offset_x
                    v = (vert.co.y + 1) * 0.5 * scale_y + offset_y
                    
                    uv_layer[loop_idx].uv = (u, v)
            
            updated_count += 1
        
        self.report({'INFO'}, f"已更新 {updated_count} 个平面的UV")
        return {'FINISHED'}


class TT_OT_bake_atlas(bpy.types.Operator):
    bl_idname = "toolkit.tt_bake_atlas"
    bl_label = "烘焙贴图集"
    bl_description = "将所有材质平面烘焙为一张贴图集"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        items = context.scene.material_preview_list
        
        if not items:
            self.report({'WARNING'}, "没有可烘焙的预览")
            return {'CANCELLED'}
        
        props = context.scene.texture_tools_props
        resolution = props.material_preview_base_resolution
        
        atlas_image = bpy.data.images.new(
            name="MaterialAtlas",
            width=resolution,
            height=resolution
        )
        
        atlas_image.pixels = [0.0, 0.0, 0.0, 1.0] * (resolution * resolution)
        
        self.report({'INFO'}, "贴图集烘焙功能需要更复杂的实现，此处为基础框架")
        return {'FINISHED'}


tt_material_preview_list = (
    TT_MaterialPreviewItem,
    TT_OT_refresh_materials,
    TT_OT_clear_all_previews,
    TT_OT_toggle_visibility,
    TT_OT_select_plane,
    TT_OT_update_from_planes,
    TT_OT_bake_atlas,
)
