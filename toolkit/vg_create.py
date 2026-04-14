import bpy
import re
from collections import defaultdict


class CreateVGsAndUV(bpy.types.Operator):
    bl_idname = "toolkit.create_vgs_and_uv"
    bl_label = "执行创建"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.vg_props
        if not props.vg_create_full_name and not props.vg_create_empty_name:
            self.report({'WARNING'}, "未指定任何要创建的顶点组名称")
            return {'CANCELLED'}

        for obj in context.selected_objects:
            if obj.type != 'MESH': continue

            if props.vg_create_delete_existing:
                obj.vertex_groups.clear()

            if props.vg_create_full_name and props.vg_create_full_name not in obj.vertex_groups:
                full_group = obj.vertex_groups.new(name=props.vg_create_full_name)
                all_verts = [v.index for v in obj.data.vertices]
                if all_verts:
                    full_group.add(all_verts, 1.0, 'REPLACE')

            if props.vg_create_empty_name and props.vg_create_empty_name not in obj.vertex_groups:
                obj.vertex_groups.new(name=props.vg_create_empty_name)

            if not obj.data.uv_layers:
                obj.data.uv_layers.new()

        self.report({'INFO'}, "顶点组和UV创建完成")
        return {'FINISHED'}


class CleanVertexGroups(bpy.types.Operator):
    bl_idname = "toolkit.clean_vertex_groups"
    bl_label = "按指定名称和零权重清理"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.vg_props
        names_to_remove = {name.strip() for name in props.vg_cleanup_names.split(',') if name.strip()}
        total_removed = 0

        for obj in context.selected_objects:
            if obj.type != 'MESH' or not obj.vertex_groups: continue

            if props.vg_cleanup_remove_zero:
                vgs_to_remove = set()

                for vg in obj.vertex_groups:
                    if vg.name in names_to_remove:
                        vgs_to_remove.add(vg)
                    
                    total_weight = sum(g.weight for v in obj.data.vertices for g in v.groups if g.group == vg.index)
                    if total_weight < 1e-6:
                        vgs_to_remove.add(vg)
                
                if vgs_to_remove:
                    for vg in [v for v in vgs_to_remove if v.name in obj.vertex_groups]:
                        obj.vertex_groups.remove(vg)
                        total_removed += 1
            else:
                total_removed += len(obj.vertex_groups)
                obj.vertex_groups.clear()

        self.report({'INFO'}, f"总计删除了 {total_removed} 个顶点组")
        return {'FINISHED'}


class BatchDeleteVG(bpy.types.Operator):
    bl_idname = "toolkit.batch_delete_vg"
    bl_label = "批量删除指定顶点组"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        props = context.scene.vg_props
        names_to_delete = {name.strip() for name in props.vg_cleanup_names.split(',') if name.strip()}

        if not names_to_delete:
            self.report({'WARNING'}, "请输入要删除的顶点组名称，用逗号分隔")
            return {'CANCELLED'}

        selected_objects = context.selected_objects
        total_deleted = 0
        processed_objects = 0

        for obj in selected_objects:
            if obj.type == 'MESH':
                processed_objects += 1
                if not obj.vertex_groups:
                    continue
                
                deleted_count = 0
                for vg in list(obj.vertex_groups):
                    if vg.name in names_to_delete:
                        obj.vertex_groups.remove(vg)
                        deleted_count += 1
                        total_deleted += 1
        
        self.report({'INFO'}, f"操作完成！处理了 {processed_objects} 个物体，删除了 {total_deleted} 个顶点组。")
        return {'FINISHED'}


class BatchRenameVG(bpy.types.Operator):
    bl_idname = "toolkit.batch_rename_vg"
    bl_label = "执行批量重命名"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        props = context.scene.vg_props
        old_name = props.vg_rename_old_name
        new_name = props.vg_rename_new_name

        if not old_name:
            self.report({'ERROR'}, "旧名称不能为空")
            return {'CANCELLED'}
        
        if not new_name:
            self.report({'ERROR'}, "新名称不能为空")
            return {'CANCELLED'}

        if old_name == new_name:
            self.report({'WARNING'}, "新旧名称相同，无需操作")
            return {'CANCELLED'}

        selected_objects = context.selected_objects
        renamed_count = 0
        processed_objects = 0

        for obj in selected_objects:
            if obj.type == 'MESH':
                processed_objects += 1
                if not obj.vertex_groups:
                    continue
                
                if new_name in obj.vertex_groups and old_name in obj.vertex_groups:
                     self.report({'WARNING'}, f"物体 '{obj.name}' 中已同时存在 '{old_name}' 和 '{new_name}'，为避免冲突已跳过")
                     continue

                vg_to_rename = obj.vertex_groups.get(old_name)
                if vg_to_rename:
                    vg_to_rename.name = new_name
                    renamed_count += 1
        
        self.report({'INFO'}, f"操作完成！处理了 {processed_objects} 个物体，重命名了 {renamed_count} 个顶点组。")
        return {'FINISHED'}


class FillVGNumberGaps(bpy.types.Operator):
    bl_idname = "toolkit.fill_vg_number_gaps"
    bl_label = "填充顶点组数字空隙"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        selected_objects = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objects:
            self.report({'INFO'}, "请选择至少一个网格物体")
            return {'CANCELLED'}

        original_active = context.active_object
        original_mode = 'OBJECT'
        if original_active and original_active.mode != 'OBJECT':
            original_mode = original_active.mode
            bpy.ops.object.mode_set(mode='OBJECT')

        total_filled_count = 0
        objects_to_sort = []

        for obj in selected_objects:
            if not obj.vertex_groups:
                continue

            numeric_names = {vg.name for vg in obj.vertex_groups if vg.name.isdigit()}
            if not numeric_names:
                continue

            max_num = max(int(name) for name in numeric_names)

            for num in range(max_num + 1):
                name = str(num)
                if name not in numeric_names:
                    obj.vertex_groups.new(name=name)
                    total_filled_count += 1

            objects_to_sort.append(obj)

        if objects_to_sort:
            for obj in objects_to_sort:
                context.view_layer.objects.active = obj
                bpy.ops.object.vertex_group_sort(sort_type='NAME')

        if original_active and original_active.name in bpy.data.objects:
            context.view_layer.objects.active = original_active
            if context.mode != original_mode:
                try:
                    bpy.ops.object.mode_set(mode=original_mode)
                except RuntimeError:
                    pass

        self.report({'INFO'}, f"操作完成，共填充了 {total_filled_count} 个顶点组空隙并排序")
        return {'FINISHED'}


class MergeVGByPrefix(bpy.types.Operator):
    bl_idname = "toolkit.merge_vg_by_prefix"
    bl_label = "按数字前缀合并顶点组"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def _find_armature(self, obj):
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object:
                return mod.object
        return None

    def _merge_bones(self, armature, prefix, source_bone_names):
        if not armature or len(source_bone_names) <= 1:
            return False

        bpy.context.view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode='EDIT')

        edit_bones = armature.data.edit_bones
        target_bone = edit_bones.get(prefix)

        source_bones = []
        for name in source_bone_names:
            if name != prefix:
                bone = edit_bones.get(name)
                if bone:
                    source_bones.append(bone)

        if not source_bones:
            bpy.ops.object.mode_set(mode='OBJECT')
            return False

        if not target_bone:
            target_bone = edit_bones.new(name=prefix)
            first_source = source_bones[0]
            target_bone.head = first_source.head.copy()
            target_bone.tail = first_source.tail.copy()
            target_bone.roll = first_source.roll
            if first_source.parent:
                target_bone.parent = first_source.parent

        children_to_reparent = []
        for src_bone in source_bones:
            for child in src_bone.children:
                if child != target_bone:
                    children_to_reparent.append(child)

        for child in children_to_reparent:
            child.parent = target_bone

        for src_bone in source_bones:
            edit_bones.remove(src_bone)

        bpy.ops.object.mode_set(mode='OBJECT')
        return True

    def _show_bone(self, armature, bone_name):
        if not armature:
            return
        bone = armature.data.bones.get(bone_name)
        if bone:
            bone.hide = False

    def execute(self, context):
        props = context.scene.vg_props
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        total_merged_prefixes = 0
        total_merged_bones = 0
        processed_armatures = set()

        for obj in selected_objects:
            prefix_map = defaultdict(list)
            for vg in obj.vertex_groups:
                match = re.match(r'^(\d+)', vg.name)
                if match:
                    prefix_map[match.group(1)].append(vg)

            groups_to_delete = []
            for prefix, source_groups in prefix_map.items():
                if len(source_groups) > 1 or (len(source_groups) == 1 and source_groups[0].name != prefix):
                    total_merged_prefixes += 1
                    target_vg = obj.vertex_groups.get(prefix) or obj.vertex_groups.new(name=prefix)

                    for vert in obj.data.vertices:
                        total_weight = 0.0
                        for source_vg in source_groups:
                            try:
                                total_weight += source_vg.weight(vert.index)
                            except RuntimeError:
                                continue

                        if total_weight > 0:
                            target_vg.add([vert.index], min(1.0, total_weight), 'REPLACE')

                    groups_to_delete.extend(g for g in source_groups if not g.name.isdigit())

                    if props.vg_merge_sync_bones:
                        armature = self._find_armature(obj)
                        if armature and armature not in processed_armatures:
                            source_bone_names = [vg.name for vg in source_groups]
                            if self._merge_bones(armature, prefix, source_bone_names):
                                total_merged_bones += 1
                                self._show_bone(armature, prefix)
                            processed_armatures.add(armature)

            for vg in set(groups_to_delete):
                if vg.name in obj.vertex_groups:
                    obj.vertex_groups.remove(vg)

        if props.vg_merge_sync_bones and total_merged_bones > 0:
            self.report({'INFO'}, f"操作完成，共合并了 {total_merged_prefixes} 个前缀的顶点组，{total_merged_bones} 组骨骼")
        else:
            self.report({'INFO'}, f"操作完成，共合并了 {total_merged_prefixes} 个前缀的顶点组")
        return {'FINISHED'}


class RemoveNonNumericVG(bpy.types.Operator):
    bl_idname = "toolkit.remove_non_numeric_vg"
    bl_label = "仅保留数字顶点组"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        total_removed_count = 0
        for obj in [o for o in context.selected_objects if o.type == 'MESH']:
            groups_to_remove = [vg for vg in obj.vertex_groups if not vg.name.isdigit()]
            for vg in reversed(groups_to_remove):
                obj.vertex_groups.remove(vg)
                total_removed_count += 1

        self.report({'INFO'}, f"操作完成，共移除了 {total_removed_count} 个非数字顶点组")
        return {'FINISHED'}


vg_create_operators = [
    CreateVGsAndUV,
    CleanVertexGroups,
    BatchDeleteVG,
    BatchRenameVG,
    FillVGNumberGaps,
    MergeVGByPrefix,
    RemoveNonNumericVG,
]
