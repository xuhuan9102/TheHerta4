import bpy


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


vg_create_operators = [
    CreateVGsAndUV,
    CleanVertexGroups,
    BatchDeleteVG,
    BatchRenameVG,
]
