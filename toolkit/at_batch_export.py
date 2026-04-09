# -*- coding: utf-8 -*-

import bpy
import os
import time
import mathutils
import traceback

from .at_utils import is_alembic_object, move_object_to_collection


class BE_FrameSplitter:
    """(自动化流程专用) 简化的帧拆分器 helper 类"""

    def __init__(self, props, context, objects_to_split, frame, target_collection):
        self.props = props
        self.context = context
        self.scene = self.context.scene
        self.original_objects = objects_to_split
        self.target_frame = frame
        self.target_collection = target_collection

    def prepare_scene(self, frame):
        try:
            if self.props.be_playback_type == "PRECISE":
                for f in range(1, frame + 1):
                    self.scene.frame_set(f)
            else:
                self.scene.frame_set(frame)
            self.context.view_layer.update()
        except Exception:
            traceback.print_exc()

    def create_snapshot_object(self, original_obj, frame):
        """创建物体在特定帧的快照"""
        if is_alembic_object(original_obj):
            new_obj = None
            original_active = self.context.view_layer.objects.active
            original_selection = self.context.selected_objects[:]
            was_hidden = original_obj.hide_get()
            if was_hidden: original_obj.hide_set(False)

            try:
                bpy.ops.object.select_all(action='DESELECT')
                original_obj.select_set(True)
                self.context.view_layer.objects.active = original_obj
                
                bpy.ops.object.duplicate()
                new_obj = self.context.active_object
                if new_obj:
                    new_obj.name = f"{self.props.be_object_prefix}{original_obj.name}_{frame:03d}"

                self.prepare_scene(frame)
                if new_obj:
                    cache_mod_name = next((mod.name for mod in new_obj.modifiers if mod.type == 'MESH_SEQUENCE_CACHE'), None)
                    if cache_mod_name:
                        bpy.ops.object.modifier_apply(modifier=cache_mod_name)
                return new_obj
            except Exception:
                traceback.print_exc()
                if new_obj: bpy.data.objects.remove(new_obj, do_unlink=True)
                return None
            finally:
                if was_hidden: original_obj.hide_set(True)
                bpy.ops.object.select_all(action='DESELECT')
                for o in original_selection:
                    if o.name in bpy.data.objects: o.select_set(True)
                if original_active and original_active.name in bpy.data.objects:
                    self.context.view_layer.objects.active = original_active

        try:
            depsgraph = self.context.evaluated_depsgraph_get()
            evaluated_obj = original_obj.evaluated_get(depsgraph)

            if original_obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
                mesh_data = bpy.data.meshes.new_from_object(evaluated_obj)
                matrix = evaluated_obj.matrix_world.copy()
                obj_name = f"{self.props.be_object_prefix}{original_obj.name}_{frame:03d}"
                new_obj = bpy.data.objects.new(obj_name, mesh_data)
                mesh_data.transform(matrix)
                new_obj.matrix_world = mathutils.Matrix.Identity(4)
                return new_obj
            else:
                new_obj = original_obj.copy()
                new_obj.animation_data_clear()
                if original_obj.data: new_obj.data = original_obj.data.copy()
                new_obj.name = f"{self.props.be_object_prefix}{original_obj.name}_{frame:03d}"
                return new_obj

        except Exception:
            traceback.print_exc()
            return None

    def split_animation_for_frame(self):
        """执行当前帧的拆分并放入目标集合的子集合中"""
        created_objects = []
        frame = self.target_frame
        print(f"  -- [Splitter] 开始拆分集合 '{self.target_collection.name}' 的第 {frame} 帧 --")

        sub_collection_name = f"{self.target_collection.name}_Frame_{frame:03d}"
        sub_collection = self.target_collection.children.get(sub_collection_name)
        if sub_collection:
            for obj in list(sub_collection.objects): bpy.data.objects.remove(obj, do_unlink=True)
        else:
            sub_collection = bpy.data.collections.new(sub_collection_name)
            self.target_collection.children.link(sub_collection)

        for obj in self.original_objects:
            if obj.type in {'CAMERA', 'LIGHT'}: continue

            self.prepare_scene(frame)
            snapshot_obj = self.create_snapshot_object(obj, frame)

            if snapshot_obj:
                move_object_to_collection(snapshot_obj, sub_collection)
                created_objects.append(snapshot_obj)
                print(f"    [Splitter] 创建快照: {snapshot_obj.name}")
        
        print(f"  -- [Splitter] 完成, 创建了 {len(created_objects)} 个对象 --")
        return created_objects


class BE_AutomationPipeline:
    """批量导出主自动化流程控制器"""

    def __init__(self, props, context):
        self.props = props
        self.context = context
        self.scene = self.context.scene
        self.all_hidden_source_objects = []
        self.registered_source_objects = []

    def _wait_for_step_delay(self):
        """步骤间暂停"""
        if self.props.auto_step_delay_seconds > 0:
            time.sleep(self.props.auto_step_delay_seconds)

    def discover_and_register_source_objects(self, parent_collection):
        """查找所有带蓝色标签集合中的Mesh物体"""
        print("--- [Init] 注册源物体 ---")
        blue_collections = self.find_collections_by_color(parent_collection, 'COLOR_05')
        for coll in blue_collections:
            for obj in coll.objects:
                if obj.type == 'MESH':
                    if obj not in self.registered_source_objects:
                        self.registered_source_objects.append(obj)
        print(f"--- [Init] 已注册 {len(self.registered_source_objects)} 个源物体 ---")

    def find_collections_by_color(self, parent_collection, color_tag):
        """递归查找特定颜色的集合"""
        found_collections = []
        def recurse(collection):
            if collection.color_tag == color_tag:
                found_collections.append(collection)
            for child in collection.children:
                recurse(child)
        recurse(parent_collection)
        return found_collections

    def set_collection_color(self, collections, color_tag):
        """批量设置集合颜色"""
        for coll in collections: coll.color_tag = color_tag

    def select_collection_and_contents(self, collection):
        """选中指定集合及其包含的所有可见物体，并设置活动层集合"""
        try:
            bpy.ops.object.select_all(action='DESELECT')
            
            valid_objects = [o for o in collection.objects if o.name in self.context.view_layer.objects and o.visible_get()]
            if not valid_objects:
                print(f"  [Select] 警告: 集合 '{collection.name}' 中没有可见/可选中的物体。")
                return

            for obj in valid_objects:
                obj.select_set(True)
            self.context.view_layer.objects.active = valid_objects[0]
            print(f"  [Select] 已选中集合 '{collection.name}' 中的 {len(valid_objects)} 个物体。")

            def find_layer_collection(layer_coll, coll_name):
                if layer_coll.collection.name == coll_name: return layer_coll
                for child in layer_coll.children:
                    found = find_layer_collection(child, coll_name)
                    if found: return found
                return None

            view_layer_collection = self.context.view_layer.layer_collection
            target_layer_collection = find_layer_collection(view_layer_collection, collection.name)
            if target_layer_collection:
                self.context.view_layer.active_layer_collection = target_layer_collection
            else:
                print(f"  [Select] 错误: 无法在视图层中找到集合 '{collection.name}'")

        except Exception as e:
            print(f"  [Select] 错误: 选择集合内容时出错: {e}")

    def rename_exported_files(self, loop_index):
        """重命名导出生成的 Buffer 文件夹和 .ini 文件"""
        print(f"  -- [File Ops] 重命名 Frame {loop_index} 的导出文件 --")
        export_path = bpy.path.abspath(self.props.auto_export_base_path)
        if not os.path.isdir(export_path):
            print(f"    [Error] 导出目录 '{export_path}' 不存在！")
            return
            
        original_buffer_path = os.path.join(export_path, "Buffer")
        if os.path.exists(original_buffer_path) and os.path.isdir(original_buffer_path):
            i_rename = loop_index
            while True:
                new_buffer_name = f"Buffer{i_rename:02d}"
                new_buffer_path = os.path.join(export_path, new_buffer_name)
                if not os.path.exists(new_buffer_path):
                    os.rename(original_buffer_path, new_buffer_path)
                    print(f"    [File Ops] 'Buffer' -> '{new_buffer_name}'")
                    break
                i_rename += 1
        else:
            print(f"    [Warning] 未在 '{export_path}' 中找到 'Buffer' 文件夹。")

        ini_files = [f for f in os.listdir(export_path) if
                     f.lower().endswith('.ini') and not f[0].isdigit()]
        if ini_files:
            original_ini_path = os.path.join(export_path, ini_files[0])
            i_rename = loop_index
            while True:
                new_ini_name = f"{i_rename:02d}_{self.props.be_ini_file_prefix}.ini"
                new_ini_path = os.path.join(export_path, new_ini_name)
                if not os.path.exists(new_ini_path):
                    os.rename(original_ini_path, new_ini_path)
                    print(f"    [File Ops] '{ini_files[0]}' -> '{new_ini_name}'")
                    
                    self.modify_ini_content(new_ini_path, i_rename)
                    break
                i_rename += 1
        else:
            print(f"    [Warning] 未在 '{export_path}' 中找到新的 .ini 文件。")

    def modify_ini_content(self, ini_filepath, loop_index):
        """修改 .ini 文件中的 Buffer 路径引用"""
        print(f"  -- [File Ops] 修改 .ini 内容 --")
        if not os.path.exists(ini_filepath): return

        string_to_find = "Buffer/"
        string_to_replace = f"Buffer{loop_index:02d}/"
        try:
            with open(ini_filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if string_to_find in content:
                new_content = content.replace(string_to_find, string_to_replace)
                with open(ini_filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"    [File Ops] 更新ini路径: '{string_to_find}' -> '{string_to_replace}'")
            else:
                print(f"    [File Ops] ini文件中未找到需替换路径。")
        except Exception as e:
            print(f"    [Error] 修改 .ini 文件出错: {e}")

    def run(self):
        """执行主循环"""
        bpy.ops.wm.console_toggle()
        print("\n" + "="*40)
        print("   ATP 批量导出自动化流程启动   ")
        print("="*40)
        
        start_time = time.time()
        
        try:
            if self.props.auto_top_level_collection_name not in bpy.data.collections:
                raise RuntimeError(f"找不到顶层/导出集合 '{self.props.auto_top_level_collection_name}'")
            top_level_collection = bpy.data.collections[self.props.auto_top_level_collection_name]

            self.discover_and_register_source_objects(top_level_collection)
            if not self.registered_source_objects:
                raise RuntimeError("未在蓝色集合中找到任何可处理的源物体。")
            self._wait_for_step_delay()

            for i in range(self.props.be_start_frame, self.props.be_loop_count + 1):
                blue_collections_this_loop = []
                created_objects_this_loop = []
                current_frame = i
                loop_start_time = time.time()
                print(f"\n>>> 开始处理第 {current_frame} 帧 (循环 {i}/{self.props.be_loop_count}) <<<")

                try:
                    blue_collections_this_loop = self.find_collections_by_color(top_level_collection, 'COLOR_05')

                    print("  [Step 1] 清理场景，准备当前帧...")
                    for blue_coll in blue_collections_this_loop:
                        for obj in blue_coll.objects:
                            if obj in self.registered_source_objects:
                                if obj not in self.all_hidden_source_objects:
                                    self.all_hidden_source_objects.append(obj)
                                obj.hide_set(True)
                        
                        for child_coll in blue_coll.children:
                            if child_coll.name.startswith(f"{blue_coll.name}_Frame_"):
                                for obj in child_coll.objects:
                                    obj.hide_set(True)

                    print("  [Step 2] 生成或显示当前帧的物体...")
                    for blue_coll in blue_collections_this_loop:
                        target_sub_coll_name = f"{blue_coll.name}_Frame_{current_frame:03d}"
                        target_sub_coll = blue_coll.children.get(target_sub_coll_name)
                        
                        col_source_objs = [obj for obj in blue_coll.objects if obj in self.registered_source_objects]

                        if target_sub_coll:
                            print(f"    -> 引用已有集合: '{target_sub_coll_name}'")
                            for obj in target_sub_coll.objects:
                                obj.hide_set(False)
                                created_objects_this_loop.append(obj)
                        else:
                            if not col_source_objs: continue
                            print(f"    -> 执行新拆分: '{blue_coll.name}' -> Frame {current_frame}")
                            splitter = BE_FrameSplitter(self.props, self.context, col_source_objs, current_frame, blue_coll)
                            new_objs = splitter.split_animation_for_frame()
                            created_objects_this_loop.extend(new_objs)
                    self._wait_for_step_delay()

                    self.set_collection_color(blue_collections_this_loop, 'NONE')

                    print(f"  [Step 4] 选中导出集合 '{self.props.auto_top_level_collection_name}'...")
                    if self.props.auto_top_level_collection_name in bpy.data.collections:
                        red_collection = bpy.data.collections[self.props.auto_top_level_collection_name]
                        if red_collection.color_tag != 'COLOR_01':
                             print(f"    [Warning] 导出集合的标签不是红色，可能会影响SSMT识别。")
                        self.select_collection_and_contents(red_collection)
                    else:
                        raise RuntimeError(f"找不到导出集合 '{self.props.auto_top_level_collection_name}'")
                    self._wait_for_step_delay()

                    print("  [Step 5] 调用 SSMT 导出...")
                    if hasattr(bpy.ops.ssmt, 'generate_mod'):
                        bpy.ops.ssmt.generate_mod()
                    else:
                        raise RuntimeError("找不到 SSMT 导出命令 (bpy.ops.ssmt.generate_mod)")
                    
                    print("    导出命令执行完毕。")
                    self._wait_for_step_delay() 

                    self.rename_exported_files(current_frame)
                    self._wait_for_step_delay()

                    self.set_collection_color(blue_collections_this_loop, 'COLOR_05')
                    
                    print(f"<<< 第 {current_frame} 帧处理完成 (耗时: {time.time() - loop_start_time:.2f}s) >>>")

                except Exception as e:
                    print(f"\n!!! 循环 {i} 发生严重错误 !!!")
                    traceback.print_exc()
                    if blue_collections_this_loop:
                        self.set_collection_color(blue_collections_this_loop, 'COLOR_05')
                    raise RuntimeError(f"自动化在第 {i} 帧中断: {e}")

            print(f"\n{'='*40}")
            print(f"   自动化流程全部完成! 总耗时: {time.time() - start_time:.2f}s")
            print(f"{'='*40}")

        finally:
            print("\n--- [Final] 执行最终清理 ---")
            if self.all_hidden_source_objects:
                count = 0
                for obj in self.all_hidden_source_objects:
                    if obj and obj.name in bpy.data.objects:
                        obj.hide_set(False)
                        count += 1
                print(f"  已恢复 {count} 个源物体的可见性。")
            
            self.scene.frame_set(self.props.be_start_frame)
            print("--- [Final] 清理完成 ---")
            
            print(f"\n将在 {self.props.auto_final_delay_seconds} 秒后关闭控制台...")
            time.sleep(self.props.auto_final_delay_seconds)
            bpy.ops.wm.console_toggle()


class ATP_OT_BatchExport(bpy.types.Operator):
    bl_idname = "atp.batch_export"
    bl_label = "执行批量导出"
    bl_description = "执行自动化批量导出流程"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.atp_props
        pipeline = BE_AutomationPipeline(props, context)
        pipeline.run()
        return {'FINISHED'}


at_batch_export_list = (
    ATP_OT_BatchExport,
)
