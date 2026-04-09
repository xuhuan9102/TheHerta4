# -*- coding: utf-8 -*-

import bpy


class ATP_OT_ObjectToShapeKey(bpy.types.Operator):
    """将两个物体的形状差异转换为形态键"""
    bl_idname = "atp.object_to_shape_key"
    bl_label = "形状差异转形态键"
    bl_description = "将两个选中的物体的形状差异转换为形态键"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) == 2

    def execute(self, context):
        props = context.scene.atp_props
        selected_objects = context.selected_objects
        
        if len(selected_objects) != 2:
            self.report({'ERROR'}, "请选择两个物体（第一个为基础物体，第二个为目标形状）")
            return {'CANCELLED'}
        
        base_obj = selected_objects[0]
        target_obj = selected_objects[1]
        
        if base_obj.type != 'MESH' or target_obj.type != 'MESH':
            self.report({'ERROR'}, "两个物体都必须是网格物体")
            return {'CANCELLED'}
        
        if len(base_obj.data.vertices) != len(target_obj.data.vertices):
            self.report({'ERROR'}, "两个物体的顶点数不同，无法创建形态键")
            return {'CANCELLED'}
        
        if not base_obj.data.shape_keys:
            base_obj.shape_key_add(name="Basis")
        
        shape_key_name = props.shape_diff_key_name
        if not shape_key_name:
            shape_key_name = f"Shape_{target_obj.name}"
        
        base_positions = []
        for vert in base_obj.data.vertices:
            world_co = base_obj.matrix_world @ vert.co
            base_positions.append(world_co.copy())
        
        target_positions = []
        for vert in target_obj.data.vertices:
            world_co = target_obj.matrix_world @ vert.co
            target_positions.append(world_co.copy())
        
        existing_shape_key = None
        if base_obj.data.shape_keys:
            existing_shape_key = base_obj.data.shape_keys.key_blocks.get(shape_key_name)
        
        if existing_shape_key:
            self.report({'INFO'}, f"发现已存在的形态键 '{shape_key_name}'，正在更新...")
            shape_key = existing_shape_key
        else:
            shape_key = base_obj.shape_key_add(name=shape_key_name)
        
        world_to_local = base_obj.matrix_world.inverted()
        
        for i in range(len(base_obj.data.vertices)):
            local_target_pos = world_to_local @ target_positions[i]
            shape_key.data[i].co = local_target_pos
        
        self.report({'INFO'}, f"已将物体 '{target_obj.name}' 的形状转换为 '{base_obj.name}' 的形态键")
        return {'FINISHED'}


class ATP_OT_ShapeKeyAnimationExport(bpy.types.Operator):
    """导出所选物体的形态键动画序列到文本编辑器"""
    bl_idname = "atp.shape_key_animation_export"
    bl_label = "形态键动画序列导出"
    bl_description = "导出所选物体的形态键动画序列，生成播放表并输出到内置文本编辑器"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.selected_objects

    def execute(self, context):
        props = context.scene.atp_props
        start_frame = props.shape_anim_export_start_frame
        end_frame = props.shape_anim_export_end_frame
        
        if start_frame > end_frame:
            self.report({'ERROR'}, "起始帧不能大于结束帧")
            return {'CANCELLED'}
        
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        shape_key_names = set()
        for obj in selected_objects:
            if obj.data.shape_keys and obj.data.shape_keys.animation_data and obj.data.shape_keys.animation_data.action:
                action = obj.data.shape_keys.animation_data.action
                for fcurve in action.fcurves:
                    if fcurve.data_path.startswith('key_blocks["'):
                        key_block_name = fcurve.data_path.split('"')[1]
                        if key_block_name.lower() != 'basis':
                            shape_key_names.add(key_block_name)
        
        if not shape_key_names:
            self.report({'ERROR'}, "所选物体中未找到任何有关键帧的形态键")
            return {'CANCELLED'}
        
        current_frame = context.scene.frame_current
        original_values = {}
        
        for obj in selected_objects:
            if obj.data.shape_keys:
                original_values[obj.name] = {}
                for key_block in obj.data.shape_keys.key_blocks:
                    if key_block.name.lower() != 'basis':
                        original_values[obj.name][key_block.name] = key_block.value
        
        frame_count = end_frame - start_frame + 1
        shape_key_data = {name: [] for name in sorted(shape_key_names)}
        
        for frame in range(start_frame, end_frame + 1):
            context.scene.frame_set(frame)
            
            for obj in selected_objects:
                if obj.data.shape_keys:
                    for key_block in obj.data.shape_keys.key_blocks:
                        if key_block.name.lower() != 'basis':
                            if key_block.name in shape_key_data:
                                shape_key_data[key_block.name].append(key_block.value)
        
        for obj in selected_objects:
            if obj.data.shape_keys:
                for key_block in obj.data.shape_keys.key_blocks:
                    if key_block.name.lower() != 'basis':
                        if key_block.name in original_values[obj.name]:
                            key_block.value = original_values[obj.name][key_block.name]
        
        context.scene.frame_set(current_frame)
        
        output_lines = []
        output_lines.append(f"# 形态键动画序列播放表")
        output_lines.append(f"# 起始帧: {start_frame}, 结束帧: {end_frame}, 总帧数: {frame_count}")
        output_lines.append(f"# 导出物体: {', '.join([obj.name for obj in selected_objects])}")
        output_lines.append(f"# 格式: 形态键名称 = 每一帧的参数值")
        output_lines.append("")
        
        for shape_key_name in sorted(shape_key_data.keys()):
            values = shape_key_data[shape_key_name]
            
            object_count = len(selected_objects)
            values_per_frame = len(values) // frame_count
            
            if values_per_frame == 1:
                merged_values = values
            else:
                merged_values = []
                for i in range(frame_count):
                    frame_values = values[i * values_per_frame:(i + 1) * values_per_frame]
                    all_same = all(abs(v - frame_values[0]) < 0.0001 for v in frame_values)
                    if all_same:
                        merged_values.append(frame_values[0])
                    else:
                        merged_values.extend(frame_values)
            
            values_str = ",".join([f"{v:.2f}" for v in merged_values])
            output_lines.append(f"{shape_key_name} = {values_str}")
        
        output_text = "\n".join(output_lines)
        
        text_name = "形态键动画序列播放表"
        if text_name in bpy.data.texts:
            text_block = bpy.data.texts[text_name]
            text_block.clear()
        else:
            text_block = bpy.data.texts.new(text_name)
        
        text_block.write(output_text)
        
        for area in context.screen.areas:
            if area.type == 'TEXT_EDITOR':
                for space in area.spaces:
                    if space.type == 'TEXT_EDITOR':
                        space.text = text_block
                        break
        
        self.report({'INFO'}, f"已导出 {len(shape_key_names)} 个形态键的动画序列到文本编辑器")
        return {'FINISHED'}


class ATP_OT_AddFrameShapeKeyPair(bpy.types.Operator):
    """添加帧/形态键对"""
    bl_idname = "atp.add_frame_shape_key_pair"
    bl_label = "添加帧/形态键对"
    bl_description = "添加新的帧和形态键名称对"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.atp_props
        
        new_pair = props.frame_shape_key_pairs.add()
        
        if len(props.frame_shape_key_pairs) > 1:
            prev_pair = props.frame_shape_key_pairs[-2]
            new_pair.end_frame = prev_pair.end_frame + 5
            new_pair.shape_key_name = f"Motion_Key_{len(props.frame_shape_key_pairs)}"
        else:
            new_pair.end_frame = 10
            new_pair.shape_key_name = "Motion_Key_1"
        
        props.frame_shape_key_index = len(props.frame_shape_key_pairs) - 1
        
        self.report({'INFO'}, f"已添加帧/形态键对: {new_pair.end_frame} - {new_pair.shape_key_name}")
        return {'FINISHED'}


class ATP_OT_RemoveFrameShapeKeyPair(bpy.types.Operator):
    """删除帧/形态键对"""
    bl_idname = "atp.remove_frame_shape_key_pair"
    bl_label = "删除帧/形态键对"
    bl_description = "删除指定的帧/形态键对"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty(name="索引", default=0)
    
    def execute(self, context):
        props = context.scene.atp_props
        
        if self.index < 0 or self.index >= len(props.frame_shape_key_pairs):
            self.report({'ERROR'}, "无效的索引")
            return {'CANCELLED'}
        
        pair = props.frame_shape_key_pairs[self.index]
        info = f"{pair.end_frame} - {pair.shape_key_name}"
        
        props.frame_shape_key_pairs.remove(self.index)
        
        if props.frame_shape_key_index >= len(props.frame_shape_key_pairs):
            props.frame_shape_key_index = max(0, len(props.frame_shape_key_pairs) - 1)
        
        self.report({'INFO'}, f"已删除帧/形态键对: {info}")
        return {'FINISHED'}


class ATP_OT_ClearFrameShapeKeyPairs(bpy.types.Operator):
    """清空所有帧/形态键对"""
    bl_idname = "atp.clear_frame_shape_key_pairs"
    bl_label = "清空所有"
    bl_description = "清空所有帧/形态键对"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.atp_props
        
        count = len(props.frame_shape_key_pairs)
        props.frame_shape_key_pairs.clear()
        props.frame_shape_key_index = 0
        
        self.report({'INFO'}, f"已清空所有帧/形态键对 ({count} 个)")
        return {'FINISHED'}


class ATP_OT_StopMultiProcessing(bpy.types.Operator):
    """停止多物体处理"""
    bl_idname = "atp.stop_multi_processing"
    bl_label = "停止处理"
    bl_description = "停止正在运行的多物体处理"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        context.scene.atp_props.stop_processing_flag = True
        self.report({'INFO'}, "正在停止处理...")
        return {'FINISHED'}


class ATP_OT_AddDefaultFrameShapeKeyPairs(bpy.types.Operator):
    """添加默认帧/形态键对"""
    bl_idname = "atp.add_default_frame_shape_key_pairs"
    bl_label = "添加默认配置"
    bl_description = "添加一组默认的帧/形态键对"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.atp_props
        
        props.frame_shape_key_pairs.clear()
        
        default_pairs = [
            (5, "1"),
            (10, "2"),
            (15, "3"),
            (20, "4"),
            (25, "5")
        ]
        
        for end_frame, shape_key_name in default_pairs:
            pair = props.frame_shape_key_pairs.add()
            pair.end_frame = end_frame
            pair.shape_key_name = shape_key_name
        
        props.frame_shape_key_index = 0
        
        self.report({'INFO'}, f"已添加 {len(default_pairs)} 个默认帧/形态键对")
        return {'FINISHED'}


at_shape_key_creation_list = (
    ATP_OT_ObjectToShapeKey,
    ATP_OT_ShapeKeyAnimationExport,
    ATP_OT_AddFrameShapeKeyPair,
    ATP_OT_RemoveFrameShapeKeyPair,
    ATP_OT_ClearFrameShapeKeyPairs,
    ATP_OT_StopMultiProcessing,
    ATP_OT_AddDefaultFrameShapeKeyPairs,
)
