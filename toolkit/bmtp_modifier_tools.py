import bpy
from . import bmtp_shape_key_utils


class BMTP_OT_ArmatureToShapekey(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_armature_to_shapekey"
    bl_label = "骨架修改器 -> 形态键 (批量)"
    bl_description = "将所有选中网格物体上的骨架修改器转换为形态键"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        selected_objects = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "请至少选择一个网格物体")
            return {'CANCELLED'}

        original_active = context.view_layer.objects.active
        success_count = 0
        failed_objects = []

        for obj in selected_objects:
            context.view_layer.objects.active = obj
            
            armature_mod = next((mod for mod in obj.modifiers if mod.type == 'ARMATURE'), None)
            if not armature_mod:
                failed_objects.append(obj.name)
                continue

            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

            if not obj.data.shape_keys:
                obj.shape_key_add(from_mix=False).name = "Basis"
            
            try:
                bpy.ops.object.modifier_apply_as_shapekey(modifier=armature_mod.name)
                new_shape_key = obj.data.shape_keys.key_blocks[-1]
                new_shape_key.value = 1.0
                armature_mod.show_viewport = False
                success_count += 1
            except RuntimeError as e:
                self.report({'WARNING'}, f"无法为 '{obj.name}' 应用修改器: {e}")
                failed_objects.append(obj.name)

        if original_active and original_active.name in bpy.data.objects:
             context.view_layer.objects.active = original_active

        if success_count > 0:
            self.report({'INFO'}, f"成功为 {success_count} 个物体创建了形态键")
        if failed_objects:
            self.report({'WARNING'}, f"未能为以下物体创建形态键 (未找到骨架修改器或应用失败): {', '.join(failed_objects)}")

        return {'FINISHED'}


class BMTP_OT_ApplyArmatureModifier(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_apply_armature_modifier"
    bl_label = "应用骨架修改器 (批量)"
    bl_description = "将所有选中网格物体上的骨架修改器直接应用"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        selected_objects = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objects:
            self.report({'WARNING'}, "请至少选择一个网格物体")
            return {'CANCELLED'}

        original_active = context.view_layer.objects.active
        applied_count = 0
        skipped_count = 0
        shapekey_count = 0
        error_objects = []

        for obj in selected_objects:
            armature_mod = next((mod for mod in obj.modifiers if mod.type == 'ARMATURE'), None)
            
            if armature_mod:
                has_shape_keys = obj.data.shape_keys is not None and len(obj.data.shape_keys.key_blocks) > 0
                
                context.view_layer.objects.active = obj
                
                if has_shape_keys:
                    shapekey_count += 1
                    try:
                        success, error_info = bmtp_shape_key_utils.BMTP_ShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(
                            context, [armature_mod.name], disable_armatures=False
                        )
                        
                        if success:
                            applied_count += 1
                        else:
                            self.report({'WARNING'}, f"无法为 '{obj.name}' 应用骨架修改器: {error_info}")
                            skipped_count += 1
                            error_objects.append(obj.name)
                    except Exception as e:
                        self.report({'WARNING'}, f"处理 '{obj.name}' 的形态键时出错: {e}")
                        skipped_count += 1
                        error_objects.append(obj.name)
                else:
                    try:
                        bpy.ops.object.modifier_apply(modifier=armature_mod.name)
                        applied_count += 1
                    except RuntimeError as e:
                        self.report({'WARNING'}, f"无法为 '{obj.name}' 应用修改器: {e}")
                        skipped_count += 1
                        error_objects.append(obj.name)
            else:
                skipped_count += 1
        
        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active

        report_message = f"操作完成: 成功应用 {applied_count} 个骨架修改器。"
        if shapekey_count > 0:
            report_message += f" 其中处理了 {shapekey_count} 个包含形态键的物体。"
        if skipped_count > 0:
            report_message += f" {skipped_count} 个物体被跳过 (无骨架修改器或应用失败)。"
            if error_objects:
                report_message += f" 失败的物体: {', '.join(error_objects)}"
        self.report({'INFO'}, report_message)
        
        return {'FINISHED'}


class BMTP_OT_ApplyAllShapeKeys(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_apply_all_shape_keys"
    bl_label = "应用全部形态键 (批量)"
    bl_description = "将所有选中网格物体上的全部形态键应用到基础网格"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        selected_objects = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objects:
            self.report({'WARNING'}, "请至少选择一个网格物体")
            return {'CANCELLED'}

        original_active = context.view_layer.objects.active
        applied_count = 0
        skipped_count = 0
        error_objects = []

        for obj in selected_objects:
            if not obj.data.shape_keys or len(obj.data.shape_keys.key_blocks) <= 1:
                skipped_count += 1
                continue
            
            context.view_layer.objects.active = obj
            
            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            
            try:
                bpy.ops.object.shape_key_remove(all=True)
                applied_count += 1
            except RuntimeError as e:
                self.report({'WARNING'}, f"无法为 '{obj.name}' 应用形态键: {e}")
                error_objects.append(obj.name)
        
        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active

        report_message = f"操作完成: 成功应用 {applied_count} 个物体的形态键。"
        if skipped_count > 0:
            report_message += f" {skipped_count} 个物体被跳过 (无形态键或只有基础键)。"
        if error_objects:
            report_message += f" 失败的物体: {', '.join(error_objects)}"
        self.report({'INFO'}, report_message)
        
        return {'FINISHED'}


class BMTP_OT_LatticeToShapekey(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_lattice_to_shapekey"
    bl_label = "晶格修改器 -> 形态键 (批量)"
    bl_description = "将所有选中网格物体上的晶格修改器转换为形态键"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        selected_objects = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "请至少选择一个网格物体")
            return {'CANCELLED'}

        original_active = context.view_layer.objects.active
        success_count = 0
        failed_objects = []

        for obj in selected_objects:
            context.view_layer.objects.active = obj
            
            lattice_mod = next((mod for mod in obj.modifiers if mod.type == 'LATTICE'), None)
            if not lattice_mod:
                failed_objects.append(obj.name)
                continue

            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

            if not obj.data.shape_keys:
                obj.shape_key_add(from_mix=False).name = "Basis"
            
            try:
                bpy.ops.object.modifier_apply_as_shapekey(modifier=lattice_mod.name)
                new_shape_key = obj.data.shape_keys.key_blocks[-1]
                new_shape_key.value = 1.0
                lattice_mod.show_viewport = False
                success_count += 1
            except RuntimeError as e:
                self.report({'WARNING'}, f"无法为 '{obj.name}' 应用修改器: {e}")
                failed_objects.append(obj.name)

        if original_active and original_active.name in bpy.data.objects:
             context.view_layer.objects.active = original_active

        if success_count > 0:
            self.report({'INFO'}, f"成功为 {success_count} 个物体创建了形态键")
        if failed_objects:
            self.report({'WARNING'}, f"未能为以下物体创建形态键 (未找到晶格修改器或应用失败): {', '.join(failed_objects)}")

        return {'FINISHED'}


class BMTP_OT_DeleteModifiersByName(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_delete_modifiers_by_name"
    bl_label = "批量删除修改器"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        names_to_remove = {name.strip() for name in props.mod_delete_names.split(',') if name.strip()}
        if not names_to_remove:
            self.report({'WARNING'}, "未指定要删除的修改器名称")
            return {'CANCELLED'}
        deleted_count = 0
        for obj in context.selected_objects:
            if hasattr(obj, 'modifiers'):
                mods_to_remove = [mod for mod in obj.modifiers if mod.name in names_to_remove]
                for mod in mods_to_remove:
                    obj.modifiers.remove(mod)
                    deleted_count += 1
        self.report({'INFO'}, f"共删除了 {deleted_count} 个修改器")
        return {'FINISHED'}


class BMTP_OT_ApplyModifiersByName(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_apply_modifiers_by_name"
    bl_label = "批量应用修改器"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        names_to_apply = {name.strip() for name in props.mod_apply_names.split(',') if name.strip()}
        if not names_to_apply:
            self.report({'WARNING'}, "未指定要应用的修改器名称")
            return {'CANCELLED'}
        
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objects:
            self.report({'WARNING'}, "请至少选择一个网格物体")
            return {'CANCELLED'}
        
        original_active = context.view_layer.objects.active
        applied_count = 0
        failed_count = 0
        shapekey_count = 0
        error_objects = []
        
        for obj in selected_objects:
            if obj.mode != 'OBJECT':
                context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
            
            matching_mods = [mod for mod in obj.modifiers if mod.name in names_to_apply]
            
            if not matching_mods:
                continue
                
            has_shape_keys = obj.data.shape_keys is not None and len(obj.data.shape_keys.key_blocks) > 0
            
            if has_shape_keys:
                shapekey_count += 1
                try:
                    selected_modifiers = [mod.name for mod in matching_mods]
                    
                    problematic_modifiers = []
                    for mod_name in selected_modifiers:
                        mod = obj.modifiers.get(mod_name)
                        if mod and mod.type == 'MIRROR' and hasattr(mod, 'use_mirror_merge') and mod.use_mirror_merge:
                            problematic_modifiers.append(mod_name)
                    
                    if problematic_modifiers:
                        self.report({'WARNING'}, 
                            f"物体 '{obj.name}' 包含镜像修改器且启用了合并选项，这可能导致顶点数量不一致问题")
                    
                    context.view_layer.objects.active = obj
                    success, error_info = bmtp_shape_key_utils.BMTP_ShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(
                        context, selected_modifiers, disable_armatures=False
                    )
                    
                    if success:
                        applied_count += len(selected_modifiers)
                    else:
                        self.report({'WARNING'}, f"无法为 '{obj.name}' 应用修改器: {error_info}")
                        failed_count += len(selected_modifiers)
                        error_objects.append(obj.name)
                except Exception as e:
                    self.report({'WARNING'}, f"处理 '{obj.name}' 的形态键时出错: {e}")
                    failed_count += len(matching_mods)
                    error_objects.append(obj.name)
            else:
                for mod in matching_mods:
                    context.view_layer.objects.active = obj
                    try:
                        bpy.ops.object.modifier_apply(modifier=mod.name)
                        applied_count += 1
                    except RuntimeError as e:
                        self.report({'WARNING'}, f"无法为 '{obj.name}' 应用修改器 '{mod.name}': {e}")
                        failed_count += 1
                        error_objects.append(obj.name)
        
        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active
        
        report_message = f"操作完成: 成功应用 {applied_count} 个修改器。"
        if shapekey_count > 0:
            report_message += f" 其中处理了 {shapekey_count} 个包含形态键的物体。"
        if failed_count > 0:
            report_message += f" {failed_count} 个修改器应用失败。"
            if error_objects:
                report_message += f" 失败的物体: {', '.join(error_objects)}"
        self.report({'INFO'}, report_message)
        
        return {'FINISHED'}


bmtp_modifier_tools_list = (
    BMTP_OT_ArmatureToShapekey,
    BMTP_OT_ApplyArmatureModifier,
    BMTP_OT_ApplyAllShapeKeys,
    BMTP_OT_LatticeToShapekey,
    BMTP_OT_DeleteModifiersByName,
    BMTP_OT_ApplyModifiersByName,
)
