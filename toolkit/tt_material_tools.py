import bpy

class TT_OT_assign_material_to_selected(bpy.types.Operator):
    bl_idname = "toolkit.tt_assign_material_to_selected"
    bl_label = "赋予材质到选中物体"
    bl_description = "将指定的材质追加到所有选中物体的材质槽"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        mat = props.material_to_assign
        
        if not mat:
            self.report({'ERROR'}, "请先选择一个材质")
            return {'CANCELLED'}
        
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        for obj in selected_objects:
            obj.data.materials.append(mat)
        
        self.report({'INFO'}, f"已将材质 '{mat.name}' 追加到 {len(selected_objects)} 个物体")
        return {'FINISHED'}


class TT_OT_delete_material_from_selected(bpy.types.Operator):
    bl_idname = "toolkit.tt_delete_material_from_selected"
    bl_label = "从选中物体删除材质"
    bl_description = "从所有选中的物体中删除指定的材质"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        mat = props.material_to_assign
        
        if not mat:
            self.report({'ERROR'}, "请先选择一个材质")
            return {'CANCELLED'}
        
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        removed_count = 0
        
        for obj in selected_objects:
            for i in range(len(obj.data.materials) - 1, -1, -1):
                if obj.data.materials[i] == mat:
                    obj.data.materials.pop(index=i)
                    removed_count += 1
        
        self.report({'INFO'}, f"已从物体中移除 {removed_count} 个材质槽")
        return {'FINISHED'}


class TT_OT_merge_duplicate_materials(bpy.types.Operator):
    bl_idname = "toolkit.tt_merge_duplicate_materials"
    bl_label = "合并重复材质"
    bl_description = "合并项目中所有使用相同贴图的材质"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        materials_by_texture = {}
        
        for mat in bpy.data.materials:
            if not mat.use_nodes:
                continue
            
            texture_images = []
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    texture_images.append(node.image.name)
            
            if texture_images:
                key = tuple(sorted(texture_images))
                if key not in materials_by_texture:
                    materials_by_texture[key] = []
                materials_by_texture[key].append(mat)
        
        merged_count = 0
        
        for key, mats in materials_by_texture.items():
            if len(mats) <= 1:
                continue
            
            base_mat = mats[0]
            
            for mat in mats[1:]:
                for obj in bpy.data.objects:
                    if obj.type == 'MESH':
                        for i, slot in enumerate(obj.material_slots):
                            if slot.material == mat:
                                slot.material = base_mat
                
                if mat.users == 0:
                    bpy.data.materials.remove(mat)
                    merged_count += 1
        
        self.report({'INFO'}, f"已合并 {merged_count} 个重复材质")
        return {'FINISHED'}


tt_material_tools_list = (
    TT_OT_assign_material_to_selected,
    TT_OT_delete_material_from_selected,
    TT_OT_merge_duplicate_materials,
)
