import bpy
import numpy as np
import bmesh


class BMTP_OT_DynamicBridge(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_dynamic_bridge"
    bl_label = "动态桥接循环边"
    bl_description = "桥接两个顶点数不同的循环边，自动处理顶点数不匹配的情况"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH' and context.mode == 'EDIT_MESH'

    def execute(self, context):
        props = context.scene.bmtp_props
        
        obj = context.active_object
        
        bm = bmesh.from_edit_mesh(obj.data)
        
        selected_edges = [e for e in bm.edges if e.select]
        
        if len(selected_edges) < 2:
            self.report({'ERROR'}, "请至少选择两条边")
            return {'CANCELLED'}
            
        bpy.ops.mesh.select_all(action='DESELECT')
        for e in selected_edges:
            e.select = True
            
        try:
            bpy.ops.mesh.bridge_edge_loops(
                number_cuts=props.bridge_segments,
                interpolation='LINEAR',
                smoothness=props.bridge_smooth
            )
            self.report({'INFO'}, f"已动态桥接循环边，分段数: {props.bridge_segments}")
        except Exception as e:
            self.report({'ERROR'}, f"桥接失败: {str(e)}")
            return {'CANCELLED'}
        
        bmesh.update_edit_mesh(obj.data)
        
        return {'FINISHED'}


class BMTP_OT_SetVertexColor(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_set_vertex_color"
    bl_label = "应用顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        selected_objects = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        color_rgba = props.vc_color[:]
        for obj in selected_objects:
            mesh = obj.data
            color_attr = mesh.color_attributes.active_color or \
                         mesh.color_attributes.new(name="COLOR", type='BYTE_COLOR', domain='CORNER')
            num_loops = len(mesh.loops)
            color_data = np.zeros(num_loops * 4, dtype=np.float32)
            if props.vc_mode == 'ALPHA_ONLY':
                color_attr.data.foreach_get("color", color_data)
                color_data[3::4] = color_rgba[3]
            else:
                color_data = np.tile(color_rgba, num_loops)
            color_attr.data.foreach_set("color", color_data)
            mesh.update()
        self.report({'INFO'}, f"顶点色操作完成，处理了 {len(selected_objects)} 个对象")
        return {'FINISHED'}


class BMTP_OT_DeleteEmptyMeshes(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_delete_empty_meshes"
    bl_label = "删除选中物体的空网格"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        objects_to_delete = [obj for obj in context.selected_objects if obj.type == 'MESH' and (not obj.data or not obj.data.polygons)]
        
        if objects_to_delete:
            count = len(objects_to_delete)
            for obj in objects_to_delete:
                bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'INFO'}, f"删除了 {count} 个没有面的选中网格对象")
        else:
            self.report({'INFO'}, "选中的对象中没有找到没有面的网格对象")
        return {'FINISHED'}


class BMTP_OT_SyncDataNames(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_sync_data_names"
    bl_label = "同步选中物体数据块名称"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return True
    
    def execute(self, context):
        renamed_count = 0
        for obj in context.selected_objects:
            if obj.data and obj.data.name != obj.name:
                try:
                    obj.data.name = obj.name
                    renamed_count += 1
                except Exception: pass
        self.report({'INFO'}, f"操作完成，同步了 {renamed_count} 个选中物体的数据块名称。")
        return {'FINISHED'}


class BMTP_OT_CleanUselessShapeKeys(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clean_useless_shape_keys"
    bl_label = "清理选中物体的无效形态键"
    bl_description = "清理选中物体中没有效果的形态键（所有顶点位置与基础形态键相同的形态键）"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return True
    
    def execute(self, context):
        total_removed = 0
        processed_objects = 0
        
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
                
            if not obj.data.shape_keys:
                continue
                
            shape_keys = obj.data.shape_keys.key_blocks
            if len(shape_keys) <= 1:
                continue
                
            basis_key = shape_keys[0]
            keys_to_remove = []
            
            for i, shape_key in enumerate(shape_keys):
                if i == 0:
                    continue
                    
                has_effect = False
                threshold = 1e-6
                
                for j in range(len(shape_key.data)):
                    if (shape_key.data[j].co - basis_key.data[j].co).length > threshold:
                        has_effect = True
                        break
                
                if not has_effect:
                    keys_to_remove.append(shape_key)
            
            for shape_key in keys_to_remove:
                obj.shape_key_remove(shape_key)
                total_removed += 1
                
            if keys_to_remove:
                processed_objects += 1
        
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        
        if total_removed > 0:
            self.report({'INFO'}, f"已从 {processed_objects} 个选中物体中删除了 {total_removed} 个无效的形态键")
        else:
            self.report({'INFO'}, "选中的物体中未找到无效的形态键")
        return {'FINISHED'}


bmtp_mesh_tools_list = (
    BMTP_OT_DynamicBridge,
    BMTP_OT_SetVertexColor,
    BMTP_OT_DeleteEmptyMeshes,
    BMTP_OT_SyncDataNames,
    BMTP_OT_CleanUselessShapeKeys,
)
