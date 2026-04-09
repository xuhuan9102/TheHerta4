import bpy


class BMTP_OT_ClearMaterials(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clear_materials"
    bl_label = "清理选中物体材质"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        for obj in context.selected_objects:
            if hasattr(obj.data, 'materials'):
                obj.data.materials.clear()
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        self.report({'INFO'}, "已清除选中物体的所有材质及孤立数据")
        return {'FINISHED'}


class BMTP_OT_CleanEmptyMaterialSlots(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clean_empty_material_slots"
    bl_label = "清理空材质槽"
    bl_description = "清理所选物体中未指定材质的材质槽"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        total_removed_slots = 0
        total_processed_objects = 0
        
        original_active = context.view_layer.objects.active
        
        for obj in context.selected_objects:
            if not hasattr(obj.data, 'materials'):
                continue
            
            context.view_layer.objects.active = obj
            
            materials_to_remove = []
            if hasattr(obj, 'material_slots'):
                for i, slot in enumerate(obj.material_slots):
                    if slot.material is None:
                        materials_to_remove.append(i)
                
                for slot_index in reversed(materials_to_remove):
                    obj.active_material_index = slot_index
                    bpy.ops.object.material_slot_remove()
                    total_removed_slots += 1
                
                if materials_to_remove:
                    total_processed_objects += 1
            else:
                for i, mat in enumerate(obj.data.materials):
                    if mat is None:
                        materials_to_remove.append(i)
                
                for mat_index in reversed(materials_to_remove):
                    obj.data.materials.pop(index=mat_index)
                    total_removed_slots += 1
                
                if materials_to_remove:
                    total_processed_objects += 1
        
        if original_active:
            context.view_layer.objects.active = original_active
        
        if total_processed_objects > 0:
            self.report({'INFO'}, f"已从 {total_processed_objects} 个物体中清理了 {total_removed_slots} 个空材质槽")
        else:
            self.report({'INFO'}, "没有找到需要清理的空材质槽")
            
        return {'FINISHED'}


class BMTP_OT_CleanDuplicateMaterials(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clean_duplicate_materials"
    bl_label = "清理重复材质"
    bl_description = "清理所选物体上相同材质名称的重复材质槽，保留第一个"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        total_removed_slots = 0
        total_processed_objects = 0
        
        original_active = context.view_layer.objects.active
        
        for obj in context.selected_objects:
            if not hasattr(obj.data, 'materials'):
                continue
            
            context.view_layer.objects.active = obj
            
            material_first_occurrence = {}
            slots_to_remove = []
            
            if hasattr(obj, 'material_slots'):
                for i, slot in enumerate(obj.material_slots):
                    if slot.material is None:
                        continue
                    
                    material_name = slot.material.name
                    if material_name not in material_first_occurrence:
                        material_first_occurrence[material_name] = i
                    else:
                        slots_to_remove.append(i)
                
                for slot_index in reversed(slots_to_remove):
                    obj.active_material_index = slot_index
                    bpy.ops.object.material_slot_remove()
                    total_removed_slots += 1
                
                if slots_to_remove:
                    total_processed_objects += 1
            else:
                for i, mat in enumerate(obj.data.materials):
                    if mat is None:
                        continue
                    
                    material_name = mat.name
                    if material_name not in material_first_occurrence:
                        material_first_occurrence[material_name] = i
                    else:
                        slots_to_remove.append(i)
                
                for mat_index in reversed(slots_to_remove):
                    obj.data.materials.pop(index=mat_index)
                    total_removed_slots += 1
                
                if slots_to_remove:
                    total_processed_objects += 1
        
        if original_active:
            context.view_layer.objects.active = original_active
        
        if total_processed_objects > 0:
            self.report({'INFO'}, f"已从 {total_processed_objects} 个物体中清理了 {total_removed_slots} 个重复材质槽")
        else:
            self.report({'INFO'}, "没有找到需要清理的重复材质槽")
            
        return {'FINISHED'}


bmtp_material_tools_list = (
    BMTP_OT_ClearMaterials,
    BMTP_OT_CleanEmptyMaterialSlots,
    BMTP_OT_CleanDuplicateMaterials,
)
