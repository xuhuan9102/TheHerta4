import bpy
import re


class BMTP_OT_KeepActiveUV(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_keep_active_uv"
    bl_label = "仅保留活动UV"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        processed_meshes, deleted_count = set(), 0
        for obj in [o for o in context.selected_objects if o.type == 'MESH' and o.data and o.data not in processed_meshes]:
            if not obj.data.uv_layers: continue
            
            active_uv = obj.data.uv_layers.active
            if not active_uv:
                self.report({'WARNING'}, f"物体 '{obj.name}' 没有活动的UV贴图，已跳过。")
                continue
            
            uvs_to_remove_names = [uv.name for uv in obj.data.uv_layers if uv != active_uv]
            
            for uv_name in uvs_to_remove_names:
                uv_layer = obj.data.uv_layers.get(uv_name)
                if uv_layer:
                    obj.data.uv_layers.remove(uv_layer)
                    deleted_count += 1
            
            processed_meshes.add(obj.data)
            
        self.report({'INFO'}, f"操作完成，共删除了 {deleted_count} 个非活动UV贴图。")
        return {'FINISHED'}


class BMTP_OT_DeleteUVsByPattern(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_delete_uvs_by_pattern"
    bl_label = "按模式删除UV"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        try:
            pattern = re.compile(props.uv_delete_pattern)
        except re.error:
            self.report({'ERROR'}, "无效的正则表达式模式")
            return {'CANCELLED'}
        processed_meshes, deleted_count = set(), 0
        for obj in [o for o in context.selected_objects if o.type == 'MESH' and o.data and o.data not in processed_meshes]:
            if not obj.data.uv_layers: continue

            uvs_to_remove_names = [uv.name for uv in obj.data.uv_layers if pattern.match(uv.name)]
            
            for uv_name in uvs_to_remove_names:
                uv_layer = obj.data.uv_layers.get(uv_name)
                if uv_layer:
                    obj.data.uv_layers.remove(uv_layer)
                    deleted_count += 1
                    
            processed_meshes.add(obj.data)
            
        self.report({'INFO'}, f"共删除了 {deleted_count} 个匹配模式的UV贴图")
        return {'FINISHED'}


class BMTP_OT_RenameUVsByPattern(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_rename_uvs_by_pattern"
    bl_label = "按模式重命名UV"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        try:
            pattern = re.compile(props.uv_rename_old_pattern)
        except re.error:
            self.report({'ERROR'}, "无效的正则表达式模式")
            return {'CANCELLED'}
        
        processed_meshes, renamed_count = set(), 0
        for obj in [o for o in context.selected_objects if o.type == 'MESH' and o.data and o.data not in processed_meshes]:
            if not obj.data.uv_layers: continue

            matching_uvs = [(uv, idx) for idx, uv in enumerate(obj.data.uv_layers) if pattern.match(uv.name)]
            
            for uv_layer, index in matching_uvs:
                new_name = props.uv_rename_new_template.format(index=index)
                uv_layer.name = new_name
                renamed_count += 1
                    
            processed_meshes.add(obj.data)
            
        self.report({'INFO'}, f"共重命名了 {renamed_count} 个匹配模式的UV贴图")
        return {'FINISHED'}


class BMTP_OT_AddUVLayers(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_add_uv_layers"
    bl_label = "批量添加UV"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        processed_meshes, added_count = set(), 0
        
        for obj in [o for o in context.selected_objects if o.type == 'MESH' and o.data and o.data not in processed_meshes]:
            if not obj.data.uv_layers:
                obj.data.uv_layers.new(name="UVMap")
            
            current_count = len(obj.data.uv_layers)
            
            for i in range(props.uv_add_count):
                new_index = current_count + i
                new_name = props.uv_add_name_template.format(index=new_index)
                obj.data.uv_layers.new(name=new_name)
                added_count += 1
                    
            processed_meshes.add(obj.data)
            
        self.report({'INFO'}, f"共添加了 {added_count} 个UV贴图")
        return {'FINISHED'}


bmtp_uv_tools_list = (
    BMTP_OT_KeepActiveUV,
    BMTP_OT_DeleteUVsByPattern,
    BMTP_OT_RenameUVsByPattern,
    BMTP_OT_AddUVLayers,
)
