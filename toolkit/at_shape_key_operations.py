# -*- coding: utf-8 -*-

import bpy


class ATP_OT_BatchAddShapeKey(bpy.types.Operator):
    """为所有选中的物体批量添加一个新的形态键，并将其设为活动项"""
    bl_idname = "atp.batch_add_shape_key"
    bl_label = "批量添加形态键"
    bl_description = "为所有选中的网格物体添加一个指定名称的新形态键，并设为活动项"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects and context.scene.atp_props.sk_add_new_name

    def execute(self, context):
        props = context.scene.atp_props
        shape_key_name = props.sk_add_new_name
        selected_objects = context.selected_objects
        
        if not shape_key_name:
            self.report({'ERROR'}, "新形态键名称不能为空。")
            return {'CANCELLED'}

        processed_count = 0
        skipped_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH':
                if not obj.data.shape_keys:
                    obj.shape_key_add(name="Basis")

                if shape_key_name in obj.data.shape_keys.key_blocks:
                    skipped_count += 1
                    continue
                
                try:
                    new_key = obj.shape_key_add(name=shape_key_name)
                    obj.active_shape_key_index = len(obj.data.shape_keys.key_blocks) - 1
                    processed_count += 1
                except Exception as e:
                    self.report({'WARNING'}, f"为物体 '{obj.name}' 添加形态键时出错: {e}")
        
        report_message = f"成功为 {processed_count} 个物体添加了形态键 '{shape_key_name}'。"
        if skipped_count > 0:
            report_message += f" 跳过了 {skipped_count} 个已存在同名键的物体。"
        
        self.report({'INFO'}, report_message)
        bpy.ops.atp.refresh_shape_keys('INVOKE_DEFAULT')

        return {'FINISHED'}


class ATP_OT_BatchRemoveShapeKey(bpy.types.Operator):
    """为所有选中的物体批量删除一个指定名称的形态键"""
    bl_idname = "atp.batch_remove_shape_key"
    bl_label = "批量删除形态键"
    bl_description = "从所有选中的网格物体中，删除指定名称的形态键"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects and context.scene.atp_props.sk_add_new_name

    def execute(self, context):
        props = context.scene.atp_props
        shape_key_name_to_remove = props.sk_add_new_name
        selected_objects = context.selected_objects
        
        if not shape_key_name_to_remove:
            self.report({'ERROR'}, "要删除的形态键名称不能为空。")
            return {'CANCELLED'}
        
        if shape_key_name_to_remove.lower() == 'basis':
            self.report({'ERROR'}, "不能删除基础形态键 'Basis'。")
            return {'CANCELLED'}

        removed_count = 0
        processed_objects = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                key_block_to_remove = obj.data.shape_keys.key_blocks.get(shape_key_name_to_remove)
                
                if key_block_to_remove:
                    try:
                        obj.shape_key_remove(key_block_to_remove)
                        removed_count += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"为物体 '{obj.name}' 删除形态键时出错: {e}")

        if removed_count > 0:
            self.report({'INFO'}, f"成功移除了 {removed_count} 个名为 '{shape_key_name_to_remove}' 的形态键。")
        else:
            self.report({'INFO'}, f"在选中的物体中未找到名为 '{shape_key_name_to_remove}' 的形态键。")

        bpy.ops.atp.refresh_shape_keys('INVOKE_DEFAULT')

        return {'FINISHED'}


class ATP_OT_ResetAllShapeKeys(bpy.types.Operator):
    """将所有选中物体的所有形态键值归零"""
    bl_idname = "atp.reset_all_shape_keys"
    bl_label = "归零所有形态键"
    bl_description = "将所有选中物体的所有形态键值设置为0"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects

    def execute(self, context):
        selected_objects = context.selected_objects
        reset_count = 0
        skipped_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                for key_block in obj.data.shape_keys.key_blocks:
                    if key_block.name.lower() != 'basis':
                        key_block.value = 0.0
                        reset_count += 1
                obj.data.update_tag()
            else:
                skipped_count += 1
        
        if reset_count > 0:
            self.report({'INFO'}, f"成功归零了 {reset_count} 个形态键。")
            bpy.ops.atp.refresh_shape_keys('INVOKE_DEFAULT')
        else:
            self.report({'INFO'}, "在选中的物体中未找到任何形态键。")
            
        if skipped_count > 0:
            self.report({'WARNING'}, f"跳过了 {skipped_count} 个没有形态键的物体。")
            
        return {'FINISHED'}


class ATP_OT_SetActiveShapeKey(bpy.types.Operator):
    """将指定名称的形态键设置为所有选中物体的活动形态键"""
    bl_idname = "atp.set_active_shape_key"
    bl_label = "设置活动形态键"
    bl_description = "将指定名称的形态键设置为所有选中物体的活动形态键"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.atp_props
        return context.selected_objects and props.sk_set_active_name

    def execute(self, context):
        props = context.scene.atp_props
        shape_key_name = props.sk_set_active_name
        selected_objects = context.selected_objects
        
        if not shape_key_name:
            self.report({'ERROR'}, "形态键名称不能为空。")
            return {'CANCELLED'}

        success_count = 0
        not_found_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                key_block = obj.data.shape_keys.key_blocks.get(shape_key_name)
                
                if key_block:
                    obj.active_shape_key_index = list(obj.data.shape_keys.key_blocks).index(key_block)
                    success_count += 1
                else:
                    not_found_count += 1
            else:
                not_found_count += 1
        
        if success_count > 0:
            self.report({'INFO'}, f"成功为 {success_count} 个物体设置了活动形态键 '{shape_key_name}'。")
        else:
            self.report({'WARNING'}, f"未在任何选中物体中找到名为 '{shape_key_name}' 的形态键。")
            
        if not_found_count > 0:
            self.report({'INFO'}, f"在 {not_found_count} 个物体中未找到指定形态键。")
            
        return {'FINISHED'}


class ATP_OT_BatchRenameShapeKey(bpy.types.Operator):
    """批量重命名选中物体中指定名称的形态键"""
    bl_idname = "atp.batch_rename_shape_key"
    bl_label = "批量重命名形态键"
    bl_description = "将选中物体中指定名称的形态键批量重命名为新名称"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.atp_props
        return context.selected_objects and props.sk_rename_old_name and props.sk_rename_new_name

    def execute(self, context):
        props = context.scene.atp_props
        old_name = props.sk_rename_old_name
        new_name = props.sk_rename_new_name
        selected_objects = context.selected_objects
        
        if not old_name or not new_name:
            self.report({'ERROR'}, "原名称和新名称不能为空。")
            return {'CANCELLED'}
            
        if old_name == new_name:
            self.report({'WARNING'}, "原名称和新名称相同，无需重命名。")
            return {'CANCELLED'}

        success_count = 0
        not_found_count = 0
        already_exists_count = 0
        
        for obj in selected_objects:
            if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
                key_block = obj.data.shape_keys.key_blocks.get(old_name)
                
                if key_block:
                    if obj.data.shape_keys.key_blocks.get(new_name):
                        already_exists_count += 1
                        continue
                        
                    key_block.name = new_name
                    success_count += 1
                else:
                    not_found_count += 1
            else:
                not_found_count += 1
        
        if success_count > 0:
            self.report({'INFO'}, f"成功在 {success_count} 个物体中将形态键 '{old_name}' 重命名为 '{new_name}'。")
        else:
            self.report({'WARNING'}, f"未在任何选中物体中找到名为 '{old_name}' 的形态键。")
            
        if not_found_count > 0:
            self.report({'INFO'}, f"在 {not_found_count} 个物体中未找到指定形态键。")
            
        if already_exists_count > 0:
            self.report({'WARNING'}, f"在 {already_exists_count} 个物体中已存在名为 '{new_name}' 的形态键，未进行重命名。")
            
        return {'FINISHED'}


at_shape_key_operations_list = (
    ATP_OT_BatchAddShapeKey,
    ATP_OT_BatchRemoveShapeKey,
    ATP_OT_ResetAllShapeKeys,
    ATP_OT_SetActiveShapeKey,
    ATP_OT_BatchRenameShapeKey,
)
