import bpy
import json
from . import bmtp_utils


class BMTP_OT_ShowUsedBones(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_show_used_bones"
    bl_label = "显示使用的骨骼"
    bl_description = "隐藏未使用的骨骼，只显示当前选中物体实际使用的骨骼"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'WARNING'}, "请先选择至少一个网格物体")
            return {'CANCELLED'}
        
        armature_obj = None
        for obj in selected_objects:
            for modifier in obj.modifiers:
                if modifier.type == 'ARMATURE':
                    armature_obj = modifier.object
                    break
            if armature_obj:
                break
        
        if not armature_obj:
            self.report({'WARNING'}, "未找到骨架修改器，请确保选中的物体有骨架修改器")
            return {'CANCELLED'}
        
        all_used_bones = set()
        for obj in selected_objects:
            used_bones = bmtp_utils.get_object_used_bones(obj, armature_obj)
            all_used_bones.update(used_bones)
        
        if not all_used_bones:
            self.report({'WARNING'}, "当前选中物体没有使用任何骨骼")
            return {'CANCELLED'}
        
        bpy.ops.object.select_all(action='DESELECT')
        armature_obj.select_set(True)
        context.view_layer.objects.active = armature_obj
        
        bmtp_utils.hide_unused_bones(armature_obj, all_used_bones)
        
        self.report({'INFO'}, f"已显示 {len(all_used_bones)} 个使用的骨骼")
        return {'FINISHED'}


class BMTP_OT_ShowAllBones(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_show_all_bones"
    bl_label = "显示所有骨骼"
    bl_description = "显示骨架中的所有骨骼"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        armature_obj = context.active_object
        bmtp_utils.show_all_bones(armature_obj)
        
        self.report({'INFO'}, f"已显示 '{armature_obj.name}' 的所有骨骼")
        return {'FINISHED'}


class BMTP_OT_CleanUnusedBones(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clean_unused_bones"
    bl_label = "清理无效骨骼"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        armature_obj = context.active_object
        mesh_objects = [o for o in bpy.data.objects if
                        o.type == 'MESH' and any(m.type == 'ARMATURE' and m.object == armature_obj for m in o.modifiers)]
        if not mesh_objects:
            self.report({'WARNING'}, "未找到绑定到此骨架的网格对象")
            return {'CANCELLED'}
        used_bone_names = {vg.name for mesh_ob in mesh_objects for vg in mesh_ob.vertex_groups}
        bones_to_remove = [bone.name for bone in armature_obj.data.bones if bone.name not in used_bone_names]
        if not bones_to_remove:
            self.report({'INFO'}, "未找到需要删除的未使用骨骼")
            return {'FINISHED'}
        bpy.ops.object.mode_set(mode='EDIT')
        for bone_name in bones_to_remove:
            edit_bone = armature_obj.data.edit_bones.get(bone_name)
            if edit_bone:
                armature_obj.data.edit_bones.remove(edit_bone)
        bpy.ops.object.mode_set(mode='OBJECT')
        self.report({'INFO'}, f"已删除 {len(bones_to_remove)} 个未使用的骨骼")
        return {'FINISHED'}


class BMTP_OT_AlignBones(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_align_bones"
    bl_label = "局部轴向对齐"
    bl_description = "让每根骨骼的主轴(Y)朝向其自身的某个局部坐标轴，同时保持长度不变"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        armature = context.active_object
        props = context.scene.bmtp_props
        align_axis_str = props.align_axis
        
        bpy.ops.object.mode_set(mode='EDIT')
        
        for b in armature.data.edit_bones:
            original_length = b.length
            if original_length < 0.0001:
                original_length = 0.01

            if align_axis_str == 'POSITIVE_X': direction_vector = b.x_axis
            elif align_axis_str == 'NEGATIVE_X': direction_vector = -b.x_axis
            elif align_axis_str == 'POSITIVE_Y': direction_vector = b.y_axis
            elif align_axis_str == 'NEGATIVE_Y': direction_vector = -b.y_axis
            elif align_axis_str == 'POSITIVE_Z': direction_vector = b.z_axis
            elif align_axis_str == 'NEGATIVE_Z': direction_vector = -b.z_axis
            else: direction_vector = b.y_axis

            b.tail = b.head + direction_vector.normalized() * original_length
            b.roll = 0.0
            
        bpy.ops.object.mode_set(mode='OBJECT')
        self.report({'INFO'}, f"已将 '{armature.name}' 的所有骨骼沿其局部坐标轴 {props.align_axis} 重新对齐")
        return {'FINISHED'}


class BMTP_OT_RemapBones(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_remap_bones_to_indices"
    bl_label = "重命名为索引"
    bl_description = "将骨骼重命名为数字索引，并将映射关系保存到内置文本编辑器"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        armature = context.active_object
        if bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bone_names = sorted([b.name for b in armature.data.bones])
        old_to_new_map = {name: str(i) for i, name in enumerate(bone_names)}
        new_to_old_map = {str(i): name for i, name in enumerate(bone_names)}
        bpy.ops.object.mode_set(mode='EDIT')
        for old_name, new_name in old_to_new_map.items():
            bone = armature.data.edit_bones.get(old_name)
            if bone:
                bone.name = new_name
        bpy.ops.object.mode_set(mode='OBJECT')
        for obj in bpy.data.objects:
            if obj.type == 'MESH':
                for old_name, new_name in old_to_new_map.items():
                    vg = obj.vertex_groups.get(old_name)
                    if vg:
                        vg.name = new_name
        text_block_name = f"{armature.name}_remap.json"
        json_string = json.dumps(new_to_old_map, ensure_ascii=False, indent=2)
        text_block = bpy.data.texts.get(text_block_name) or bpy.data.texts.new(name=text_block_name)
        text_block.clear()
        text_block.write(json_string)
        self.report({'INFO'}, f"重命名了 {len(bone_names)} 个骨骼。映射表已保存至文本块: '{text_block_name}'")
        return {'FINISHED'}


class BMTP_OT_RestoreBoneNames(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_restore_bone_names"
    bl_label = "从映射表恢复"
    bl_description = "使用选定的映射表，将索引化的骨骼名称恢复为原始名称"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'ARMATURE'

    def execute(self, context):
        armature = context.active_object
        props = context.scene.bmtp_props
        
        if not props.restore_map_text:
            self.report({'ERROR'}, "请在UI面板中选择一个映射表文本块")
            return {'CANCELLED'}
        
        try:
            remap_dict = json.loads(props.restore_map_text.as_string())
        except json.JSONDecodeError:
            self.report({'ERROR'}, f"文本块 '{props.restore_map_text.name}' 不是有效的JSON格式")
            return {'CANCELLED'}

        bpy.ops.object.mode_set(mode='EDIT')
        restored_count = 0
        for bone in armature.data.edit_bones:
            if bone.name in remap_dict:
                bone.name = remap_dict[bone.name]
                restored_count += 1
        bpy.ops.object.mode_set(mode='OBJECT')

        mesh_objects_to_process = []
        for obj in bpy.data.objects:
            if obj.type == 'MESH':
                for modifier in obj.modifiers:
                    if modifier.type == 'ARMATURE' and modifier.object == armature:
                        mesh_objects_to_process.append(obj)
                        break
        
        for obj in mesh_objects_to_process:
            for index_name, original_name in remap_dict.items():
                vg = obj.vertex_groups.get(index_name)
                if vg:
                    vg.name = original_name

        self.report({'INFO'}, f"从 '{props.restore_map_text.name}' 中恢复了 {restored_count} 个骨骼名称，并更新了关联物体的顶点组。")
        return {'FINISHED'}


bmtp_bone_tools_list = (
    BMTP_OT_ShowUsedBones,
    BMTP_OT_ShowAllBones,
    BMTP_OT_CleanUnusedBones,
    BMTP_OT_AlignBones,
    BMTP_OT_RemapBones,
    BMTP_OT_RestoreBoneNames,
)
