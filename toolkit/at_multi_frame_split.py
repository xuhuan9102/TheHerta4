# -*- coding: utf-8 -*-

import bpy
import mathutils
from mathutils import Matrix, Vector
import numpy as np
import math


def is_matrix_close(matrix1, matrix2, tolerance=1e-6):
    """检查两个矩阵是否在容差范围内相等"""
    if matrix1 is None or matrix2 is None:
        return False
    
    for i in range(4):
        for j in range(4):
            if abs(matrix1[i][j] - matrix2[i][j]) > tolerance:
                return False
    return True


def is_matrix_identity(matrix, tolerance=1e-6):
    """检查矩阵是否在容差范围内为单位矩阵"""
    identity = Matrix.Identity(4)
    return is_matrix_close(matrix, identity, tolerance)


class ATP_OT_SplitFramesToShapeKeyMulti(bpy.types.Operator):
    """增强版：支持多物体选择和可配置帧列表的形态键拆分"""
    bl_idname = "atp.split_frames_to_shape_key_multi"
    bl_label = "多物体拆分帧到形态键（增强版）"
    bl_description = "支持多物体选择和可配置帧列表的形态键拆分，每个物体独立处理"
    bl_options = {'REGISTER', 'UNDO'}
    
    _timer = None
    _current_object_index = 0
    _current_frame_index = 0
    _selected_objects = []
    _processing_status = ""
    _is_processing = False
    
    _state = None
    _temp_start_obj = None
    _temp_end_obj = None
    _temp_collection = None
    _base_frame_obj = None
    _continuous_base_obj = None
    _frames_to_process = None
    _current_split_frame_index = 0
    _created_objects = None
    _start_frame = None
    _end_frame = None
    _skip_start_frame = False
    _current_play_frame = 0
    _target_play_frame = 0
    _play_direction = 1
    
    STATE_INIT = 'INIT'
    STATE_FIND_BASE = 'FIND_BASE'
    STATE_PREPARE_SPLIT = 'PREPARE_SPLIT'
    STATE_PLAY_FRAMES = 'PLAY_FRAMES'
    STATE_CREATE_SNAPSHOT = 'CREATE_SNAPSHOT'
    STATE_SPLIT_COMPLETE = 'SPLIT_COMPLETE'
    STATE_CREATE_SHAPE_KEY = 'CREATE_SHAPE_KEY'
    STATE_CLEANUP = 'CLEANUP'
    STATE_NEXT_ITEM = 'NEXT_ITEM'
    STATE_FINISH = 'FINISH'
    STATE_COPY_SHAPE_KEYS = 'COPY_SHAPE_KEYS'
    STATE_COPY_SINGLE = 'COPY_SINGLE'
    STATE_FINAL_CLEANUP = 'FINAL_CLEANUP'
    
    _intermediate_objs = None
    _current_intermediate_index = 0
    
    @classmethod
    def poll(cls, context):
        return context.selected_objects and len(context.scene.atp_props.frame_shape_key_pairs) > 0
    
    def invoke(self, context, event):
        """初始化处理状态"""
        self._current_object_index = 0
        self._current_frame_index = 0
        self._selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        self._processing_status = "准备处理..."
        self._is_processing = False
        
        self._state = self.STATE_INIT
        self._temp_start_obj = None
        self._temp_end_obj = None
        self._temp_collection = None
        self._base_frame_obj = None
        self._continuous_base_obj = None
        self._frames_to_process = None
        self._current_split_frame_index = 0
        self._created_objects = None
        self._start_frame = None
        self._end_frame = None
        self._skip_start_frame = False
        self._current_play_frame = 0
        self._target_play_frame = 0
        self._play_direction = 1
        self._intermediate_objs = None
        self._current_intermediate_index = 0
        
        if not self._selected_objects:
            self.report({'ERROR'}, "未选择任何网格物体")
            return {'CANCELLED'}
        
        props = context.scene.atp_props
        if not props.frame_shape_key_pairs:
            self.report({'ERROR'}, "未配置任何帧/形态键对")
            return {'CANCELLED'}
        
        for pair in props.frame_shape_key_pairs:
            pair.is_processed = False
        
        props.current_processing_status = "准备处理..."
        props.current_processing_progress = 0.0
        props.stop_processing_flag = False
        
        context.window_manager.modal_handler_add(self)
        self._timer = context.window_manager.event_timer_add(0.001, window=context.window)
        self._is_processing = True
        
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        """模态处理函数 - 使用状态机实现细粒度异步处理"""
        props = context.scene.atp_props
        
        if props.stop_processing_flag:
            props.stop_processing_flag = False
            self.cancel_processing(context)
            return {'CANCELLED'}
        
        if event.type == 'ESC':
            self.cancel_processing(context)
            return {'CANCELLED'}
        
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        
        if not self._is_processing:
            self.finish_processing(context)
            return {'FINISHED'}
        
        try:
            result = self.process_state_machine(context)
            
            if result == 'FINISHED':
                self.finish_processing(context)
                return {'FINISHED'}
            elif result == 'CANCELLED':
                self.cancel_processing(context)
                return {'CANCELLED'}
            elif result == 'YIELD':
                return {'PASS_THROUGH'}
            else:
                return {'PASS_THROUGH'}
                
        except Exception as e:
            self.report({'ERROR'}, f"处理出错: {str(e)}")
            self.cancel_processing(context)
            return {'CANCELLED'}
    
    def process_state_machine(self, context):
        """状态机处理 - 每次只执行一个小步骤"""
        props = context.scene.atp_props
        
        if self._state == self.STATE_INIT:
            return self.state_init(context, props)
        elif self._state == self.STATE_FIND_BASE:
            return self.state_find_base(context, props)
        elif self._state == self.STATE_PREPARE_SPLIT:
            return self.state_prepare_split(context, props)
        elif self._state == self.STATE_PLAY_FRAMES:
            return self.state_play_frames(context, props)
        elif self._state == self.STATE_CREATE_SNAPSHOT:
            return self.state_create_snapshot(context, props)
        elif self._state == self.STATE_SPLIT_COMPLETE:
            return self.state_split_complete(context, props)
        elif self._state == self.STATE_CREATE_SHAPE_KEY:
            return self.state_create_shape_key(context, props)
        elif self._state == self.STATE_CLEANUP:
            return self.state_cleanup(context, props)
        elif self._state == self.STATE_NEXT_ITEM:
            return self.state_next_item(context, props)
        elif self._state == self.STATE_FINISH:
            return self.state_finish(context, props)
        elif self._state == self.STATE_COPY_SHAPE_KEYS:
            return self.state_copy_shape_keys(context, props)
        elif self._state == self.STATE_COPY_SINGLE:
            return self.state_copy_single(context, props)
        elif self._state == self.STATE_FINAL_CLEANUP:
            return self.state_final_cleanup(context, props)
        else:
            return 'FINISHED'
    
    def state_init(self, context, props):
        """状态: 初始化处理当前物体"""
        if self._current_object_index >= len(self._selected_objects):
            self._state = self.STATE_FINISH
            return 'YIELD'
        
        current_obj = self._selected_objects[self._current_object_index]
        self._processing_status = f"准备处理物体: {current_obj.name}"
        props.current_processing_status = self._processing_status
        
        self._temp_start_obj = None
        self._temp_end_obj = None
        self._temp_collection = None
        self._base_frame_obj = None
        self._continuous_base_obj = None
        
        self._state = self.STATE_FIND_BASE
        return 'YIELD'
    
    def state_find_base(self, context, props):
        """状态: 查找或创建基础帧物体"""
        current_obj = self._selected_objects[self._current_object_index]
        current_pair = props.frame_shape_key_pairs[self._current_frame_index]
        
        start_frame = props.multi_object_start_frame
        if props.use_continuous_mode and self._current_frame_index > 0:
            previous_pair = props.frame_shape_key_pairs[self._current_frame_index - 1]
            start_frame = previous_pair.end_frame
            
            continuous_base_obj_name = f"{current_obj.name}_{start_frame:03d}_Base"
            self._continuous_base_obj = bpy.data.objects.get(continuous_base_obj_name)
        
        self._base_frame_obj = self.find_or_create_base_frame_object(context, current_obj, props.multi_object_start_frame)
        
        self._processing_status = f"处理物体: {current_obj.name}, 帧: {current_pair.end_frame}"
        props.current_processing_status = self._processing_status
        
        self.update_progress(props)
        
        self._state = self.STATE_PREPARE_SPLIT
        return 'YIELD'
    
    def state_prepare_split(self, context, props):
        """状态: 准备分割帧 - 初始化变量"""
        current_obj = self._selected_objects[self._current_object_index]
        current_pair = props.frame_shape_key_pairs[self._current_frame_index]
        
        self._start_frame = props.multi_object_start_frame
        if props.use_continuous_mode and self._current_frame_index > 0:
            previous_pair = props.frame_shape_key_pairs[self._current_frame_index - 1]
            self._start_frame = previous_pair.end_frame
        
        self._end_frame = current_pair.end_frame
        self._skip_start_frame = self._base_frame_obj is not None
        
        if self._skip_start_frame:
            self._frames_to_process = [self._end_frame]
        else:
            self._frames_to_process = [self._start_frame, self._end_frame]
        
        self._current_split_frame_index = 0
        self._created_objects = []
        
        self._temp_collection = bpy.data.collections.new(f"Temp_Split_{current_obj.name}")
        context.scene.collection.children.link(self._temp_collection)
        
        sub_collection = bpy.data.collections.new(f"{self._temp_collection.name}_Frames")
        self._temp_collection.children.link(sub_collection)
        self._sub_collection = sub_collection
        
        self._original_frame = context.scene.frame_current
        
        self._state = self.STATE_PLAY_FRAMES
        return 'YIELD'
    
    def state_play_frames(self, context, props):
        """状态: 播放帧到目标帧（高精度模式下逐帧播放）"""
        if self._current_split_frame_index >= len(self._frames_to_process):
            self._state = self.STATE_SPLIT_COMPLETE
            return 'YIELD'
        
        target_frame = self._frames_to_process[self._current_split_frame_index]
        current_frame = context.scene.frame_current
        
        if props.use_precise_frame_mode:
            if current_frame < target_frame:
                context.scene.frame_set(current_frame + 1)
                context.view_layer.update()
                return 'YIELD'
            elif current_frame > target_frame:
                context.scene.frame_set(current_frame - 1)
                context.view_layer.update()
                return 'YIELD'
        
        context.scene.frame_set(target_frame)
        context.view_layer.update()
        
        self._state = self.STATE_CREATE_SNAPSHOT
        return 'YIELD'
    
    def state_create_snapshot(self, context, props):
        """状态: 创建当前帧的快照"""
        current_obj = self._selected_objects[self._current_object_index]
        frame = self._frames_to_process[self._current_split_frame_index]
        
        context.view_layer.depsgraph.update()
        
        depsgraph = context.evaluated_depsgraph_get()
        eval_obj = current_obj.evaluated_get(depsgraph)
        
        if eval_obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            self._current_split_frame_index += 1
            self._state = self.STATE_PLAY_FRAMES
            return 'YIELD'
        
        mesh_data = bpy.data.meshes.new_from_object(eval_obj)
        matrix = eval_obj.matrix_world.copy()
        mesh_data.transform(matrix)
        
        obj_name = f"{current_obj.name}_{frame:03d}"
        
        existing_obj = bpy.data.objects.get(obj_name)
        if existing_obj:
            old_mesh = existing_obj.data
            existing_obj.data = mesh_data
            existing_obj.matrix_world = Matrix.Identity(4)
            
            if old_mesh and old_mesh.users == 0:
                bpy.data.meshes.remove(old_mesh)
            
            new_obj = existing_obj
        else:
            new_obj = bpy.data.objects.new(obj_name, mesh_data)
            
            for slot in current_obj.material_slots:
                if slot.material:
                    new_obj.data.materials.append(slot.material)
            
            self._sub_collection.objects.link(new_obj)
        
        self._created_objects.append(new_obj)
        
        self._current_split_frame_index += 1
        self._state = self.STATE_PLAY_FRAMES
        return 'YIELD'
    
    def state_split_complete(self, context, props):
        """状态: 分割完成 - 设置起始和结束物体"""
        current_obj = self._selected_objects[self._current_object_index]
        
        context.scene.frame_set(self._original_frame)
        
        start_obj_name = f"{current_obj.name}_{self._start_frame:03d}"
        end_obj_name = f"{current_obj.name}_{self._end_frame:03d}"
        
        self._temp_start_obj = None
        self._temp_end_obj = None
        
        for created_obj in self._created_objects:
            if created_obj.name == start_obj_name:
                self._temp_start_obj = created_obj
            elif created_obj.name == end_obj_name:
                self._temp_end_obj = created_obj
        
        if self._skip_start_frame:
            self._temp_start_obj = self._base_frame_obj
        
        if not self._temp_start_obj or not self._temp_end_obj:
            self.report({'ERROR'}, f"无法创建起始或结束物体: {current_obj.name}")
            self._state = self.STATE_NEXT_ITEM
            return 'YIELD'
        
        self._state = self.STATE_CREATE_SHAPE_KEY
        return 'YIELD'
    
    def state_create_shape_key(self, context, props):
        """状态: 创建形态键"""
        current_obj = self._selected_objects[self._current_object_index]
        current_pair = props.frame_shape_key_pairs[self._current_frame_index]
        
        success = self.create_shape_key_from_objects(
            context, self._temp_start_obj, self._temp_end_obj, 
            current_pair.shape_key_name, self._continuous_base_obj)
        
        if success:
            if not self._temp_start_obj.get("atp_base_frame"):
                self._temp_start_obj["atp_base_frame"] = True
                self._temp_start_obj["atp_original_object"] = current_obj.name
                self._temp_start_obj["atp_frame_number"] = props.multi_object_start_frame
                expected_base_name = f"{current_obj.name}_{props.multi_object_start_frame:03d}_Base"
                if self._temp_start_obj.name != expected_base_name:
                    self._temp_start_obj.name = expected_base_name
            
            if props.use_continuous_mode:
                frame_base_obj_name = f"{current_obj.name}_{current_pair.end_frame:03d}_Base"
                frame_base_obj = bpy.data.objects.get(frame_base_obj_name)
                
                if not frame_base_obj:
                    frame_base_obj = self._temp_end_obj.copy()
                    frame_base_obj.data = self._temp_end_obj.data.copy()
                    frame_base_obj.name = frame_base_obj_name
                    frame_base_obj["atp_base_frame"] = True
                    frame_base_obj["atp_original_object"] = current_obj.name
                    frame_base_obj["atp_frame_number"] = current_pair.end_frame
                    
                    for collection in current_obj.users_collection:
                        collection.objects.link(frame_base_obj)
            
            current_pair.is_processed = True
            self.report({'INFO'}, f"成功处理: {current_obj.name} - {current_pair.shape_key_name}")
        else:
            self.report({'ERROR'}, f"创建形态键失败: {current_obj.name} - {current_pair.shape_key_name}")
        
        self._state = self.STATE_CLEANUP
        return 'YIELD'
    
    def state_cleanup(self, context, props):
        """状态: 清理临时物体"""
        current_obj = self._selected_objects[self._current_object_index]
        
        current_obj.hide_viewport = False
        current_obj.hide_render = False
        
        if self._temp_start_obj:
            self._temp_start_obj.hide_viewport = False
            self._temp_start_obj.hide_render = False
        
        if self._temp_end_obj:
            bpy.data.objects.remove(self._temp_end_obj, do_unlink=True)
            self._temp_end_obj = None
        
        if self._temp_collection and self._temp_collection.name in bpy.data.collections:
            self.cleanup_temp_collection(context, self._temp_collection, self._temp_start_obj, current_obj)
            self._temp_collection = None
        
        self._state = self.STATE_NEXT_ITEM
        return 'YIELD'
    
    def state_next_item(self, context, props):
        """状态: 移动到下一个处理项"""
        self._current_frame_index += 1
        
        if self._current_frame_index >= len(props.frame_shape_key_pairs):
            self._current_frame_index = 0
            self._current_object_index += 1
            
            if self._current_object_index >= len(self._selected_objects):
                self._state = self.STATE_FINISH
                return 'YIELD'
        
        self._state = self.STATE_INIT
        return 'YIELD'
    
    def state_finish(self, context, props):
        """状态: 完成主处理，开始复制形态键"""
        total_objects = len(self._selected_objects)
        total_pairs = len(props.frame_shape_key_pairs)
        processed_count = sum(1 for pair in props.frame_shape_key_pairs if pair.is_processed)
        
        self.report({'INFO'}, f"主处理完成！处理了 {total_objects} 个物体，{processed_count}/{total_pairs} 个帧/形态键对")
        
        props.current_processing_status = "复制形态键到原始物体..."
        
        intermediate_objects, original_to_intermediate = self.find_intermediate_objects(context)
        
        self._intermediate_objs = []
        for original_name, intermediate_objs in original_to_intermediate.items():
            original_obj = bpy.data.objects.get(original_name)
            if original_obj:
                for intermediate_obj in intermediate_objs:
                    self._intermediate_objs.append((intermediate_obj, original_obj))
        
        self._current_intermediate_index = 0
        
        if not self._intermediate_objs:
            props.current_processing_status = "处理完成"
            props.current_processing_progress = 1.0
            self._state = self.STATE_FINAL_CLEANUP
            return 'YIELD'
        
        self._state = self.STATE_COPY_SINGLE
        return 'YIELD'
    
    def state_copy_single(self, context, props):
        """状态: 复制单个中间物体的形态键"""
        if self._current_intermediate_index >= len(self._intermediate_objs):
            props.current_processing_status = "处理完成"
            props.current_processing_progress = 1.0
            self._state = self.STATE_FINAL_CLEANUP
            return 'YIELD'
        
        intermediate_obj, original_obj = self._intermediate_objs[self._current_intermediate_index]
        
        if not intermediate_obj.data.shape_keys:
            intermediate_obj_name = intermediate_obj.name
            try:
                bpy.data.objects.remove(intermediate_obj, do_unlink=True)
                self.report({'INFO'}, f"已删除中间物体 '{intermediate_obj_name}'")
            except Exception as e:
                self.report({'WARNING'}, f"删除中间物体 '{intermediate_obj_name}' 时出错: {e}")
        else:
            success = self.copy_shape_keys_between_objects(context, intermediate_obj, original_obj)
            
            if success:
                intermediate_obj_name = intermediate_obj.name
                self.report({'INFO'}, f"成功将 '{intermediate_obj_name}' 的形态键复制到 '{original_obj.name}'")
                
                try:
                    bpy.data.objects.remove(intermediate_obj, do_unlink=True)
                except Exception as e:
                    self.report({'WARNING'}, f"删除中间物体 '{intermediate_obj_name}' 时出错: {e}")
            else:
                self.report({'ERROR'}, f"复制形态键失败: '{intermediate_obj.name}' -> '{original_obj.name}'")
        
        self._current_intermediate_index += 1
        
        total = len(self._intermediate_objs)
        props.current_processing_progress = 0.9 + 0.1 * (self._current_intermediate_index / max(total, 1))
        
        return 'YIELD'
    
    def state_copy_shape_keys(self, context, props):
        """状态: 复制形态键到原始物体（已废弃，使用 STATE_COPY_SINGLE）"""
        self._state = self.STATE_FINAL_CLEANUP
        return 'YIELD'
    
    def state_final_cleanup(self, context, props):
        """状态: 最终清理"""
        bpy.ops.object.select_all(action='DESELECT')
        for obj in self._selected_objects:
            obj.select_set(True)
        
        self._is_processing = False
        return 'FINISHED'
    
    def update_progress(self, props):
        """更新处理进度"""
        total_items = len(self._selected_objects) * len(props.frame_shape_key_pairs)
        current_item = self._current_object_index * len(props.frame_shape_key_pairs) + self._current_frame_index
        props.current_processing_progress = current_item / max(total_items, 1)
    
    def cleanup_temp_collection(self, context, temp_collection, start_obj, original_obj):
        """清理临时集合但保留起始帧物体"""
        if start_obj and start_obj.name:
            original_collections = list(original_obj.users_collection)
            if not original_collections:
                original_collections = [context.scene.collection]
            
            for collection in original_collections:
                if start_obj.name not in collection.objects:
                    collection.objects.link(start_obj)
            
            for sub_coll in list(temp_collection.children):
                if start_obj.name in sub_coll.objects:
                    sub_coll.objects.unlink(start_obj)
            if start_obj.name in temp_collection.objects:
                temp_collection.objects.unlink(start_obj)
            
            start_obj.hide_viewport = False
            start_obj.hide_render = False
            start_obj.hide_set(False)
        
        for sub_coll in list(temp_collection.children):
            for temp_obj in list(sub_coll.objects):
                bpy.data.objects.remove(temp_obj, do_unlink=True)
            bpy.data.collections.remove(sub_coll)
        
        for temp_obj in list(temp_collection.objects):
            bpy.data.objects.remove(temp_obj, do_unlink=True)
        
        bpy.data.collections.remove(temp_collection)
    
    def finish_processing(self, context):
        """完成处理"""
        props = context.scene.atp_props
        
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        
        self.report({'INFO'}, "处理完成！")
    
    def find_intermediate_objects(self, context):
        """识别所有中间物体"""
        intermediate_objects = []
        original_to_intermediate = {}
        
        for obj in bpy.data.objects:
            if obj.type == 'MESH' and obj.get("atp_base_frame"):
                original_name = obj.get("atp_original_object")
                if original_name:
                    if original_name not in original_to_intermediate:
                        original_to_intermediate[original_name] = []
                    original_to_intermediate[original_name].append(obj)
                    intermediate_objects.append(obj)
        
        return intermediate_objects, original_to_intermediate
    
    def copy_shape_keys_from_intermediate_objects(self, context):
        """将中间物体的形态键复制到原始物体并删除中间物体"""
        intermediate_objects, original_to_intermediate = self.find_intermediate_objects(context)
        
        if not intermediate_objects:
            self.report({'INFO'}, "未找到任何中间物体需要处理")
            return
        
        print(f"[DEBUG] 找到 {len(intermediate_objects)} 个中间物体")
        self.report({'INFO'}, f"找到 {len(intermediate_objects)} 个中间物体，开始复制形态键...")
        
        processed_count = 0
        failed_count = 0
        
        for original_name, intermediate_objs in original_to_intermediate.items():
            original_obj = bpy.data.objects.get(original_name)
            
            if not original_obj:
                self.report({'WARNING'}, f"未找到原始物体 '{original_name}'，跳过处理")
                continue
            
            for i, intermediate_obj in enumerate(intermediate_objs):
                if not intermediate_obj.data.shape_keys:
                    intermediate_obj_name = intermediate_obj.name
                    try:
                        bpy.data.objects.remove(intermediate_obj, do_unlink=True)
                        self.report({'INFO'}, f"已删除中间物体 '{intermediate_obj_name}'")
                        processed_count += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"删除中间物体 '{intermediate_obj_name}' 时出错: {e}")
                        failed_count += 1
                    continue
                
                success = self.copy_shape_keys_between_objects(context, intermediate_obj, original_obj)
                
                if success:
                    intermediate_obj_name = intermediate_obj.name
                    self.report({'INFO'}, f"成功将 '{intermediate_obj_name}' 的形态键复制到 '{original_name}'")
                    
                    try:
                        bpy.data.objects.remove(intermediate_obj, do_unlink=True)
                        self.report({'INFO'}, f"已删除中间物体 '{intermediate_obj_name}'")
                        processed_count += 1
                    except Exception as e:
                        self.report({'WARNING'}, f"删除中间物体 '{intermediate_obj_name}' 时出错: {e}")
                        failed_count += 1
                else:
                    intermediate_obj_name = intermediate_obj.name
                    self.report({'ERROR'}, f"复制形态键失败: '{intermediate_obj_name}' -> '{original_name}'")
                    failed_count += 1
        
        print(f"[DEBUG] 处理完成: 成功 {processed_count}, 失败 {failed_count}")
    
    def copy_shape_keys_between_objects(self, context, source_obj, target_obj):
        """将源物体的所有形态键复制到目标物体"""
        if not source_obj.data.shape_keys or not source_obj.data.shape_keys.key_blocks:
            return False
        
        source_mesh = source_obj.data
        source_vtx_count = len(source_mesh.vertices)
        
        if target_obj.type != 'MESH':
            return False
        
        target_mesh = target_obj.data
        target_vtx_count = len(target_mesh.vertices)
        
        if target_vtx_count != source_vtx_count:
            return False
        
        print(f"[DEBUG] 复制形态键: '{source_obj.name}' -> '{target_obj.name}'")
        
        depsgraph = context.evaluated_depsgraph_get()
        eval_target = target_obj.evaluated_get(depsgraph)
        
        target_local_coords = np.zeros(target_vtx_count * 3, dtype=np.float32)
        eval_target.data.vertices.foreach_get('co', target_local_coords)
        target_local_coords = target_local_coords.reshape(-1, 3)
        
        source_keys = source_mesh.shape_keys
        if not source_keys.reference_key:
            return False
        
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

        print(f"[DEBUG] 需要复制的形态键数量: {len(keys_deltas)}")

        print(f"[DEBUG] 更新目标物体的 Basis 形态键...")
        depsgraph = context.evaluated_depsgraph_get()
        eval_target = target_obj.evaluated_get(depsgraph)
        
        target_local_coords = np.zeros(target_vtx_count * 3, dtype=np.float32)
        eval_target.data.vertices.foreach_get('co', target_local_coords)
        target_local_coords = target_local_coords.reshape(-1, 3)

        if not target_mesh.shape_keys:
            target_obj.shape_key_add(name="Basis")
        
        target_keys = target_mesh.shape_keys
        target_basis_key = target_keys.reference_key
        
        if not target_basis_key:
            print(f"[DEBUG] 错误: 目标物体缺少基础形态键")
            return False

        target_basis_key.data.foreach_set("co", target_local_coords.reshape(-1))
        
        print(f"[DEBUG] 复制中间物体的形态键...")
        
        source_keys = source_mesh.shape_keys
        if not source_keys.reference_key:
            print(f"[DEBUG] 错误: 源物体缺少基础形态键 (reference_key)")
            return False
        
        basis_key = source_keys.reference_key
        
        source_basis_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
        basis_key.data.foreach_get("co", source_basis_coords)
        source_basis_coords = source_basis_coords.reshape(-1, 3)
        
        keys_world_coords = {}
        for kb in source_keys.key_blocks:
            if kb == basis_key:
                continue

            key_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            kb.data.foreach_get("co", key_coords)
            key_coords = key_coords.reshape(-1, 3)
            
            keys_world_coords[kb.name] = key_coords

        print(f"[DEBUG] 复制 {len(keys_world_coords)} 个形态键...")

        for key_name, key_world_coords in keys_world_coords.items():
            target_kb = target_keys.key_blocks.get(key_name)
            if not target_kb:
                target_kb = target_obj.shape_key_add(name=key_name, from_mix=False)

            target_kb.data.foreach_set("co", key_world_coords.reshape(-1))
            
            target_kb.value = 0.0
            target_kb.slider_min = 0.0
            target_kb.slider_max = 1.0
            target_kb.mute = False

        target_mesh.update()
        
        armature = None
        for modifier in target_obj.modifiers:
            if modifier.type == 'ARMATURE':
                armature = modifier
                break
        
        if armature:
            armature.show_viewport = False
            armature.show_in_editmode = False
            armature.show_on_cage = False
        
        return True
    
    def cancel_processing(self, context):
        """取消处理"""
        props = context.scene.atp_props
        
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        
        self.report({'WARNING'}, "处理已取消")
        self._is_processing = False
        
        props.current_processing_status = "处理已取消"
        props.current_processing_progress = 0.0
    
    def split_frames(self, context, obj, start_frame, end_frame, props, skip_start_frame=False):
        """创建指定两个帧的物体快照"""
        print(f"\n[DEBUG] split_frames: '{obj.name}' 帧 {start_frame} -> {end_frame}")
        
        original_frame = context.scene.frame_current
        original_selection = context.selected_objects[:]
        original_active = context.view_layer.objects.active
        
        temp_collection = bpy.data.collections.new(f"Temp_Split_{obj.name}")
        context.scene.collection.children.link(temp_collection)
        
        sub_collection = bpy.data.collections.new(f"{temp_collection.name}_Frames")
        temp_collection.children.link(sub_collection)
        
        frames_to_process = [start_frame, end_frame] if not skip_start_frame else [end_frame]
        created_objects = []
        
        for frame in frames_to_process:
            current_frame = context.scene.frame_current
            
            if props.use_precise_frame_mode:
                if current_frame < frame:
                    for f in range(current_frame, frame + 1):
                        context.scene.frame_set(f)
                        context.view_layer.update()
                elif current_frame > frame:
                    for f in range(current_frame, frame - 1, -1):
                        context.scene.frame_set(f)
                        context.view_layer.update()
            else:
                context.scene.frame_set(frame)
                context.view_layer.update()
            
            context.view_layer.depsgraph.update()
            
            depsgraph = context.evaluated_depsgraph_get()
            eval_obj = obj.evaluated_get(depsgraph)
            
            if eval_obj.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
                continue
            
            mesh_data = bpy.data.meshes.new_from_object(eval_obj)
            matrix = eval_obj.matrix_world.copy()
            mesh_data.transform(matrix)
            
            obj_name = f"{obj.name}_{frame:03d}"
            
            existing_obj = bpy.data.objects.get(obj_name)
            if existing_obj:
                original_collections = list(existing_obj.users_collection)
                
                old_mesh = existing_obj.data
                existing_obj.data = mesh_data
                existing_obj.matrix_world = Matrix.Identity(4)
                
                if old_mesh and old_mesh.users == 0:
                    bpy.data.meshes.remove(old_mesh)
                
                new_obj = existing_obj
            else:
                new_obj = bpy.data.objects.new(obj_name, mesh_data)
                
                for slot in obj.material_slots:
                    if slot.material:
                        new_obj.data.materials.append(slot.material)
                
                sub_collection.objects.link(new_obj)
            
            created_objects.append(new_obj)
        
        start_obj = None
        end_obj = None
        
        start_obj_name = f"{obj.name}_{start_frame:03d}"
        end_obj_name = f"{obj.name}_{end_frame:03d}"
        
        for created_obj in created_objects:
            if created_obj.name == start_obj_name:
                start_obj = created_obj
            elif created_obj.name == end_obj_name:
                end_obj = created_obj
        
        print(f"\n[DEBUG] 起始物体: '{start_obj.name if start_obj else 'None'}'")
        print(f"[DEBUG] 结束物体: '{end_obj.name if end_obj else 'None'}'")
        
        if start_obj:
            print(f"[DEBUG] 起始物体变换矩阵:")
            print(f"[DEBUG]   {start_obj.matrix_world}")
        if end_obj:
            print(f"[DEBUG] 结束物体变换矩阵:")
            print(f"[DEBUG]   {end_obj.matrix_world}")
        
        context.scene.frame_set(original_frame)
        context.view_layer.objects.active = original_active
        for obj in original_selection:
            obj.select_set(True)
        
        print(f"\n[DEBUG] ====== split_frames 完成 ======\n")
        
        return start_obj, end_obj, temp_collection
    
    def find_or_create_base_frame_object(self, context, original_obj, frame):
        """查找或创建指定帧的基础物体"""
        base_obj_name = f"{original_obj.name}_{frame:03d}_Base"
        
        existing_base_obj = bpy.data.objects.get(base_obj_name)
        if existing_base_obj and existing_base_obj.type == 'MESH':
            self.report({'INFO'}, f"找到已存在的基础帧物体 '{base_obj_name}'")
            
            if not is_matrix_close(existing_base_obj.matrix_world, original_obj.matrix_world, 1e-6):
                self.report({'WARNING'}, f"基础帧物体 '{base_obj_name}' 的变换矩阵与原始物体不一致，可能导致形态键扭曲")
            
            return existing_base_obj
        
        standard_obj_name = f"{original_obj.name}_{frame:03d}"
        standard_obj = bpy.data.objects.get(standard_obj_name)
        if standard_obj and standard_obj.type == 'MESH':
            if standard_obj.data.shape_keys and len(standard_obj.data.shape_keys.key_blocks) > 1:
                standard_obj.name = base_obj_name
                standard_obj["atp_base_frame"] = True
                standard_obj["atp_original_object"] = original_obj.name
                standard_obj["atp_frame_number"] = frame
                
                if not is_matrix_close(standard_obj.matrix_world, original_obj.matrix_world, 1e-6):
                    self.report({'WARNING'}, f"基础帧物体 '{base_obj_name}' 的变换矩阵与原始物体不一致，可能导致形态键扭曲")
                
                self.report({'INFO'}, f"将现有物体 '{standard_obj_name}' 标记为基础帧物体（已有形态键）")
                return standard_obj
        
        return None
    
    def create_shape_key_from_objects(self, context, base_obj, target_obj, shape_key_name, continuous_base_obj=None):
        """基于两个物体创建形态键（形状差异转形态键）
        
        参数:
            context: Blender上下文
            base_obj: 基础物体（形态键将创建在此物体上）
            target_obj: 目标物体（形态键的目标形状）
            shape_key_name: 形态键名称
            continuous_base_obj: 连续模式下的基础物体（用于计算相对位移）
        """
        if base_obj.type != 'MESH' or target_obj.type != 'MESH':
            self.report({'ERROR'}, "两个物体都必须是网格物体")
            return False
        
        if len(base_obj.data.vertices) != len(target_obj.data.vertices):
            self.report({'ERROR'}, "两个物体的顶点数不同，无法创建形态键")
            return False
        
        if not base_obj.data.shape_keys:
            base_obj.shape_key_add(name="Basis")
        
        existing_shape_key = None
        if base_obj.data.shape_keys:
            existing_shape_key = base_obj.data.shape_keys.key_blocks.get(shape_key_name)
        
        if existing_shape_key:
            shape_key = existing_shape_key
        else:
            shape_key = base_obj.shape_key_add(name=shape_key_name)
        
        vtx_count = len(base_obj.data.vertices)
        
        basis_coords = np.zeros(vtx_count * 3, dtype=np.float32)
        base_obj.data.vertices.foreach_get('co', basis_coords)
        basis_coords = basis_coords.reshape(-1, 3)
        
        target_coords = np.zeros(vtx_count * 3, dtype=np.float32)
        target_obj.data.vertices.foreach_get('co', target_coords)
        target_coords = target_coords.reshape(-1, 3)
        
        if continuous_base_obj:
            continuous_base_coords = np.zeros(vtx_count * 3, dtype=np.float32)
            continuous_base_obj.data.vertices.foreach_get('co', continuous_base_coords)
            continuous_base_coords = continuous_base_coords.reshape(-1, 3)
            
            delta = target_coords - continuous_base_coords
            shape_key_coords = basis_coords + delta
        else:
            shape_key_coords = target_coords
        
        shape_key.data.foreach_set('co', shape_key_coords.reshape(-1))
        
        return True


at_multi_frame_split_list = (
    ATP_OT_SplitFramesToShapeKeyMulti,
)
