import bpy
import json
import time
from collections import defaultdict


class VGBackupListUI(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row()
            row.prop(item, "name", text="", emboss=False, icon_value=icon)
            ts = item.timestamp
            if ts > 0:
                time_str = time.strftime("%m-%d %H:%M", time.localtime(ts))
                row.label(text=time_str)
            else:
                row.label(text="--:--")


class BackupVGWeights(bpy.types.Operator):
    bl_idname = "toolkit.backup_vg_weights"
    bl_label = "备份顶点组权重"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.vertex_groups

    def execute(self, context):
        obj = context.active_object
        
        group_map = {vg.index: vg.name for vg in obj.vertex_groups}
        vertex_weights = defaultdict(list)

        for v in obj.data.vertices:
            for g in v.groups:
                if g.weight > 1e-6:
                    vertex_weights[v.index].append([g.group, g.weight])
        
        backup_data = {
            "group_map": group_map,
            "vertex_weights": vertex_weights
        }
        
        json_data_str = json.dumps(backup_data)
        
        new_item = obj.vg_backups.add()
        new_item.name = f"备份 {time.strftime('%Y-%m-%d %H:%M:%S')}"
        new_item.timestamp = time.time()
        new_item.data = json_data_str.encode('utf-8')
        
        obj.vg_backups_index = len(obj.vg_backups) - 1
        
        self.report({'INFO'}, f"已为 '{obj.name}' 创建新的权重备份。")
        return {'FINISHED'}


class RestoreVGWeights(bpy.types.Operator):
    bl_idname = "toolkit.restore_vg_weights"
    bl_label = "恢复顶点组权重"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.vg_backups and len(obj.vg_backups) > obj.vg_backups_index

    def execute(self, context):
        obj = context.active_object
        backup_item = obj.vg_backups[obj.vg_backups_index]

        try:
            json_data_str = backup_item.data.decode('utf-8')
            backup_data = json.loads(json_data_str)
            backup_group_map = backup_data['group_map']
            vertex_weights = backup_data['vertex_weights']
        except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
            self.report({'ERROR'}, f"备份数据无效或已损坏: {e}")
            return {'CANCELLED'}

        vg_names_to_remove = [vg.name for vg in obj.vertex_groups]
        for vg_name in vg_names_to_remove:
            obj.vertex_groups.remove(obj.vertex_groups[vg_name])
        
        backup_group_names = set(backup_group_map.values())
        for name in backup_group_names:
            obj.vertex_groups.new(name=name)
        
        current_vg_map = {vg.name: vg for vg in obj.vertex_groups}

        for vert_idx_str, weights in vertex_weights.items():
            vert_idx = int(vert_idx_str)
            if vert_idx >= len(obj.data.vertices): continue

            for group_idx_str, weight in weights:
                backup_group_name = backup_group_map.get(str(group_idx_str))
                if backup_group_name and backup_group_name in current_vg_map:
                    vg = current_vg_map[backup_group_name]
                    vg.add([vert_idx], weight, 'ADD')

        self.report({'INFO'}, f"已从备份 '{backup_item.name}' 恢复权重。")
        return {'FINISHED'}


class RemoveVGBackup(bpy.types.Operator):
    bl_idname = "toolkit.remove_vg_backup"
    bl_label = "删除顶点组权重备份"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and obj.vg_backups

    def execute(self, context):
        obj = context.active_object
        index = obj.vg_backups_index
        
        if len(obj.vg_backups) > 0 and index < len(obj.vg_backups):
            obj.vg_backups.remove(index)
            obj.vg_backups_index = min(max(0, index - 1), len(obj.vg_backups) - 1)
            self.report({'INFO'}, "已删除选中的备份。")
        else:
            self.report({'WARNING'}, "没有可删除的备份。")

        return {'FINISHED'}


class BatchBackupVGWeights(bpy.types.Operator):
    bl_idname = "toolkit.batch_backup_vg_weights"
    bl_label = "批量备份选中物体权重"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(obj.type == 'MESH' for obj in context.selected_objects)

    def execute(self, context):
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'WARNING'}, "没有选中的网格物体")
            return {'CANCELLED'}
        
        success_count = 0
        failed_objects = []
        
        for obj in selected_meshes:
            try:
                if not obj.vertex_groups:
                    failed_objects.append(f"{obj.name} (无顶点组)")
                    continue
                
                group_map = {vg.index: vg.name for vg in obj.vertex_groups}
                vertex_weights = defaultdict(list)

                for v in obj.data.vertices:
                    for g in v.groups:
                        if g.weight > 1e-6:
                            vertex_weights[v.index].append([g.group, g.weight])
                
                backup_data = {
                    "group_map": group_map,
                    "vertex_weights": vertex_weights
                }
                
                json_data_str = json.dumps(backup_data)
                
                new_item = obj.vg_backups.add()
                new_item.name = f"批量备份 {time.strftime('%Y-%m-%d %H:%M:%S')}"
                new_item.timestamp = time.time()
                new_item.data = json_data_str.encode('utf-8')
                
                success_count += 1
            except Exception as e:
                failed_objects.append(f"{obj.name} (错误: {str(e)})")
        
        if success_count > 0:
            self.report({'INFO'}, f"已成功为 {success_count} 个物体创建权重备份")
        if failed_objects:
            self.report({'WARNING'}, f"以下物体备份失败: {', '.join(failed_objects)}")
        
        return {'FINISHED'}


class BatchRestoreVGWeights(bpy.types.Operator):
    bl_idname = "toolkit.batch_restore_vg_weights"
    bl_label = "批量恢复选中物体权重"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        return any(obj.vg_backups and len(obj.vg_backups) > 0 for obj in selected_meshes)

    def execute(self, context):
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_meshes:
            self.report({'WARNING'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        success_count = 0
        failed_objects = []
        
        for obj in selected_meshes:
            try:
                if not obj.vg_backups or len(obj.vg_backups) == 0:
                    failed_objects.append(f"{obj.name} (无备份)")
                    continue
                
                backup_item = obj.vg_backups[0]
                
                json_data_str = backup_item.data.decode('utf-8')
                backup_data = json.loads(json_data_str)
                backup_group_map = backup_data['group_map']
                vertex_weights = backup_data['vertex_weights']
                
                vg_names_to_remove = [vg.name for vg in obj.vertex_groups]
                for vg_name in vg_names_to_remove:
                    obj.vertex_groups.remove(obj.vertex_groups[vg_name])
                
                backup_group_names = set(backup_group_map.values())
                for name in backup_group_names:
                    obj.vertex_groups.new(name=name)
                
                current_vg_map = {vg.name: vg for vg in obj.vertex_groups}

                for vert_idx_str, weights in vertex_weights.items():
                    vert_idx = int(vert_idx_str)
                    if vert_idx >= len(obj.data.vertices): continue

                    for group_idx_str, weight in weights:
                        backup_group_name = backup_group_map.get(str(group_idx_str))
                        if backup_group_name and backup_group_name in current_vg_map:
                            vg = current_vg_map[backup_group_name]
                            vg.add([vert_idx], weight, 'ADD')
                
                success_count += 1
            except Exception as e:
                failed_objects.append(f"{obj.name} (错误: {str(e)})")
        
        if success_count > 0:
            self.report({'INFO'}, f"已成功为 {success_count} 个物体恢复权重")
        if failed_objects:
            self.report({'WARNING'}, f"以下物体恢复失败: {', '.join(failed_objects)}")
        
        return {'FINISHED'}


vg_backup_operators = [
    VGBackupListUI,
    BackupVGWeights,
    RestoreVGWeights,
    RemoveVGBackup,
    BatchBackupVGWeights,
    BatchRestoreVGWeights,
]
