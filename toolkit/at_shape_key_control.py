# -*- coding: utf-8 -*-

import bpy
import math
import mathutils
import time
import numpy as np


class ATP_OT_RefreshShapeKeys(bpy.types.Operator):
    bl_idname = "atp.refresh_shape_keys"
    bl_label = "刷新形态键列表"
    bl_description = "根据当前选中的物体，扫描并生成一个统一的同名形态键控制列表"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects

    def execute(self, context):
        props = context.scene.atp_props
        props.shape_key_list.clear()
        
        found_keys = set()
        
        for obj in context.selected_objects:
            if obj.type == 'MESH' and obj.data.shape_keys:
                reference_key = obj.data.shape_keys.reference_key
                for key_block in obj.data.shape_keys.key_blocks:
                    if key_block != reference_key:
                        found_keys.add(key_block.name)
        
        if not found_keys:
            self.report({'INFO'}, "在选中的物体中未找到任何可调节的形态键。")
            return {'CANCELLED'}

        for key_name in sorted(list(found_keys)):
            item = props.shape_key_list.add()
            item.name = key_name
            for obj in context.selected_objects:
                 if obj.type == 'MESH' and obj.data.shape_keys and key_name in obj.data.shape_keys.key_blocks:
                     item.value = obj.data.shape_keys.key_blocks[key_name].value
                     break
                     
        self.report({'INFO'}, f"找到 {len(found_keys)} 个独特的形态键。")
        return {'FINISHED'}


class ATP_OT_CopyShapeKeys(bpy.types.Operator):
    """将活动物体的形态键的相对位移复制到其他选中的、顶点数相同的物体上"""
    bl_idname = "atp.copy_shape_keys"
    bl_label = "复制形态键到选中项"
    bl_description = "将活动物体的所有形态键的相对位移复制到其他顶点数相同的选中物体上"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        return (
            active_obj is not None and
            active_obj.type == 'MESH' and
            active_obj.data.shape_keys is not None and
            len(context.selected_objects) > 1
        )

    def execute(self, context):
        source_obj = context.active_object
        all_targets = [obj for obj in context.selected_objects if obj != source_obj]

        if not source_obj.data.shape_keys or not source_obj.data.shape_keys.key_blocks:
            self.report({'ERROR'}, "源物体没有任何形态键可供复制。")
            return {'CANCELLED'}

        source_mesh = source_obj.data
        source_vtx_count = len(source_mesh.vertices)
        
        valid_targets = [
            target for target in all_targets 
            if target.type == 'MESH' and len(target.data.vertices) == source_vtx_count
        ]
        
        skipped_count = len(all_targets) - len(valid_targets)

        if not valid_targets:
            self.report({'ERROR'}, f"未找到顶点数({source_vtx_count})匹配的目标。")
            return {'CANCELLED'}

        start_time = time.time()
        
        source_keys = source_mesh.shape_keys
        if not source_keys.reference_key:
            self.report({'ERROR'}, f"源物体 '{source_obj.name}' 缺少基础形态键 (Basis Key)。")
            return {'CANCELLED'}
        
        basis_key = source_keys.reference_key
        
        source_basis_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
        basis_key.data.foreach_get("co", source_basis_coords)
        source_basis_coords = source_basis_coords.reshape(-1, 3)
        
        keys_deltas = {}
        for kb in source_keys.key_blocks:
            if kb == basis_key:
                continue

            key_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            kb.data.foreach_get("co", key_coords)
            key_coords = key_coords.reshape(-1, 3)
            
            delta = key_coords - source_basis_coords
            
            keys_deltas[kb.name] = {
                'delta': delta,
                'value': kb.value,
                'slider_min': kb.slider_min,
                'slider_max': kb.slider_max,
                'mute': kb.mute
            }

        for target in valid_targets:
            target_mesh = target.data
            
            if not target_mesh.shape_keys:
                target.shape_key_add(name="Basis")
            
            target_keys = target_mesh.shape_keys
            target_basis_key = target_keys.reference_key
            
            if not target_basis_key:
                 self.report({'WARNING'}, f"跳过目标 '{target.name}'，因为它缺少基础形态键。")
                 continue

            target_basis_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            target_basis_key.data.foreach_get("co", target_basis_coords)
            target_basis_coords = target_basis_coords.reshape(-1, 3)

            props = context.scene.atp_props

            if props.copy_sk_use_manual_rotation:
                rot_x = math.radians(props.copy_sk_rotation_x)
                rot_y = math.radians(props.copy_sk_rotation_y)
                rot_z = math.radians(props.copy_sk_rotation_z)
                
                rot_matrix_x = mathutils.Matrix.Rotation(rot_x, 4, 'X').to_3x3()
                rot_matrix_y = mathutils.Matrix.Rotation(rot_y, 4, 'Y').to_3x3()
                rot_matrix_z = mathutils.Matrix.Rotation(rot_z, 4, 'Z').to_3x3()
                
                transform_matrix = rot_matrix_z @ rot_matrix_y @ rot_matrix_x
            else:
                source_matrix = source_obj.matrix_world
                target_matrix_inv = target.matrix_world.inverted()
                transform_matrix = target_matrix_inv @ source_matrix
                transform_matrix = transform_matrix.to_3x3()

            for key_name, key_info in keys_deltas.items():
                
                target_kb = target_keys.key_blocks.get(key_name)
                if not target_kb:
                    target_kb = target.shape_key_add(name=key_name, from_mix=False)

                transformed_delta = key_info['delta'] @ transform_matrix.transposed()
                
                new_coords = target_basis_coords + transformed_delta
                new_coords = new_coords.reshape(-1)
                
                target_kb.data.foreach_set("co", new_coords)
                
                target_kb.value = key_info['value']
                target_kb.slider_min = key_info['slider_min']
                target_kb.slider_max = key_info['slider_max']
                target_kb.mute = key_info['mute']

            target_mesh.update()

        end_time = time.time()
        self.report({'INFO'}, f"成功将 {len(keys_deltas)} 个形态键复制到 {len(valid_targets)} 个物体 (耗时: {end_time - start_time:.3f}s)。")
        if skipped_count > 0:
            self.report({'WARNING'}, f"跳过了 {skipped_count} 个顶点数不匹配的物体。")
            
        return {'FINISHED'}


at_shape_key_control_list = (
    ATP_OT_RefreshShapeKeys,
    ATP_OT_CopyShapeKeys,
)
