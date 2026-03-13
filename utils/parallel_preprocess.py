

import os
import sys
import json
import shutil
import tempfile
import subprocess
import multiprocessing
from pathlib import Path
from typing import List, Dict, Tuple, Optional, TYPE_CHECKING
from dataclasses import dataclass, asdict
from datetime import datetime

if TYPE_CHECKING:
    import bpy


@dataclass
class PreprocessTask:
    """预处理任务定义"""
    task_id: int
    blend_file: str
    object_names: List[str]
    output_blend: str
    mirror_workflow: bool
    vg_mapping_texts: Dict[str, str]


@dataclass
class PreprocessResult:
    """预处理结果"""
    task_id: int
    success: bool
    error_message: str
    processed_objects: List[str]
    output_blend: str
    processing_time: float


class ParallelPreprocessManager:
    """多进程并行预处理管理器"""
    
    def __init__(self, num_workers: Optional[int] = None):
        self.num_workers = num_workers or max(1, multiprocessing.cpu_count() - 1)
        self.temp_dir = None
        self.tasks: List[PreprocessTask] = []
        self.results: List[PreprocessResult] = []
    
    def preprocess_parallel(
        self,
        blend_file: str,
        object_names: List[str],
        mirror_workflow: bool = False,
        vg_mapping_texts: Dict[str, str] = None,
        progress_callback=None
    ) -> Dict[str, str]:
        """
        并行预处理入口函数
        
        Args:
            blend_file: 原始 .blend 文件路径
            object_names: 物体名称列表
            mirror_workflow: 是否启用非镜像工作流
            vg_mapping_texts: 顶点组映射表数据 {文本名称: 文本内容}
            progress_callback: 进度回调函数
        
        Returns:
            字典: {原始物体名: 预处理后的blend文件路径}
        """
        try:
            self.temp_dir = tempfile.mkdtemp(prefix="ssmt_preprocess_")
            print(f"[ParallelPreprocess] 创建临时目录: {self.temp_dir}")
            
            if len(object_names) <= 1:
                print("[ParallelPreprocess] 物体数量不足，使用单进程模式")
                return None
            
            subsets = self._split_objects(object_names)
            
            self._create_tasks(blend_file, subsets, mirror_workflow, vg_mapping_texts or {})
            
            self._run_workers(progress_callback)
            
            object_blend_map = self._collect_results()
            
            return object_blend_map
            
        except Exception as e:
            print(f"[ParallelPreprocess] 错误: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            pass
    
    def cleanup(self):
        """清理临时文件"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                print(f"[ParallelPreprocess] 清理临时目录: {self.temp_dir}")
            except Exception as e:
                print(f"[ParallelPreprocess] 清理失败: {e}")
    
    def _split_objects(self, object_names: List[str]) -> List[List[str]]:
        """将物体列表分割成多个子集"""
        num_objects = len(object_names)
        actual_workers = min(self.num_workers, num_objects)
        
        if actual_workers == 0:
            return []
        
        subset_size = num_objects // actual_workers
        remainder = num_objects % actual_workers
        
        subsets = []
        start = 0
        
        for i in range(actual_workers):
            size = subset_size + (1 if i < remainder else 0)
            if size > 0:
                subsets.append(object_names[start:start + size])
            start += size
        
        print(f"[ParallelPreprocess] 分割 {num_objects} 个物体到 {len(subsets)} 个子集")
        for i, subset in enumerate(subsets):
            print(f"[ParallelPreprocess] 子集 {i}: {len(subset)} 个物体")
        return subsets
    
    def _create_tasks(
        self,
        blend_file: str,
        subsets: List[List[str]],
        mirror_workflow: bool,
        vg_mapping_texts: Dict[str, str]
    ) -> None:
        """创建预处理任务"""
        for i, subset in enumerate(subsets):
            output_blend = os.path.join(self.temp_dir, f"preprocessed_{i}.blend")
            
            task = PreprocessTask(
                task_id=i,
                blend_file=blend_file,
                object_names=subset,
                output_blend=output_blend,
                mirror_workflow=mirror_workflow,
                vg_mapping_texts=vg_mapping_texts
            )
            self.tasks.append(task)
            
            task_json = os.path.join(self.temp_dir, f"task_{i}.json")
            with open(task_json, 'w', encoding='utf-8') as f:
                json.dump(asdict(task), f, ensure_ascii=False, indent=2)
    
    def _run_workers(self, progress_callback=None) -> None:
        """启动并运行所有工作进程"""
        import threading
        import queue
        
        print(f"[ParallelPreprocess] 准备查找 Blender 可执行文件...")
        blender_exe = self._find_blender_executable()
        print(f"[ParallelPreprocess] Blender 可执行文件: {blender_exe}")
        
        if not blender_exe or not os.path.exists(blender_exe):
            print(f"[ParallelPreprocess] 错误: Blender 可执行文件无效或不存在: {blender_exe}")
            return
        
        print(f"[ParallelPreprocess] 启动 {len(self.tasks)} 个工作进程...")
        
        result_queue = queue.Queue()
        threads = []
        semaphore = threading.Semaphore(self.num_workers)
        
        def worker_thread(task: PreprocessTask):
            with semaphore:
                try:
                    print(f"[ParallelPreprocess] 开始任务 {task.task_id}")
                    result = self._run_single_worker(blender_exe, task)
                    result_queue.put(result)
                    print(f"[ParallelPreprocess] 任务 {task.task_id} 完成")
                except Exception as e:
                    print(f"[ParallelPreprocess] 任务 {task.task_id} 异常: {e}")
                    import traceback
                    traceback.print_exc()
                    result_queue.put(PreprocessResult(
                        task_id=task.task_id,
                        success=False,
                        error_message=str(e),
                        processed_objects=[],
                        output_blend="",
                        processing_time=0
                    ))
        
        for task in self.tasks:
            print(f"[ParallelPreprocess] 创建线程 {task.task_id}")
            thread = threading.Thread(target=worker_thread, args=(task,))
            thread.start()
            threads.append(thread)
            print(f"[ParallelPreprocess] 线程 {task.task_id} 已启动")
        
        completed = 0
        total = len(threads)
        print(f"[ParallelPreprocess] 等待 {total} 个任务完成...")
        
        while completed < total:
            try:
                result = result_queue.get(timeout=1)
                self.results.append(result)
                completed += 1
                
                if progress_callback:
                    progress_callback(completed / total * 100)
                
                status = "成功" if result.success else "失败"
                print(f"[ParallelPreprocess] 任务 {result.task_id} {status}")
                if not result.success:
                    print(f"[ParallelPreprocess] 错误信息: {result.error_message}")
            except queue.Empty:
                continue
        
        print(f"[ParallelPreprocess] 所有任务完成，等待线程结束...")
        for thread in threads:
            thread.join(timeout=1)
        print(f"[ParallelPreprocess] 所有线程已结束")
    
    @staticmethod
    def _run_single_worker(blender_exe: str, task: PreprocessTask) -> PreprocessResult:
        """运行单个工作进程 - 执行预处理（优化版）"""
        start_time = datetime.now()
        
        try:
            addon_name = "ssmt_theherta_plugin"
            
            # 优化：预生成脚本，避免每次都生成
            script = f'''
import bpy
import sys
import json
import os
import traceback
import time

# 优化：控制日志级别，减少不必要的输出
VERBOSE = False

if VERBOSE:
    print("=" * 50)
    print(f"[Worker {task.task_id}] 脚本开始执行")
    print(f"[Worker {task.task_id}] Python 路径: {{sys.path[:3]}}")
    print("=" * 50)

# 任务参数
task_id = {task.task_id}
object_names = {json.dumps(task.object_names)}
mirror_workflow = {str(task.mirror_workflow)}
output_blend = r"{task.output_blend}"
vg_mapping_texts = {json.dumps(task.vg_mapping_texts or {})}

if VERBOSE:
    print(f"[Worker {{task_id}}] 物体数量: {{len(object_names)}}")
    print(f"[Worker {{task_id}}] 非镜像工作流: {{mirror_workflow}}")
    print(f"[Worker {{task_id}}] 输出文件: {{output_blend}}")
    print(f"[Worker {{task_id}}] 映射表数量: {{len(vg_mapping_texts)}}")

# 检查物体是否存在
if VERBOSE:
    for obj_name in object_names:
        obj = bpy.data.objects.get(obj_name)
        if obj:
            print(f"[Worker {{task_id}}] 找到物体: {{obj_name}} (类型: {{obj.type}})")
        else:
            print(f"[Worker {{task_id}}] 警告: 物体不存在 {{obj_name}}")


def reset_shapekey_values(obj):
    """重置所有形态键值为0"""
    if obj.data.shape_keys is None:
        return
    for kb in obj.data.shape_keys.key_blocks:
        kb.value = 0.0


def apply_modifiers_for_object_with_shape_keys_optimized(context, selected_modifiers, disable_armatures=False):
    """
    优化版：使用 numpy 直接处理形态键数据
    避免对每个形态键重复复制物体和应用修改器
    """
    import numpy
    
    if len(selected_modifiers) == 0:
        return (True, None)
    
    obj = context.object
    
    modifiers_that_transform_vertices = {{'ARMATURE', 'CURVE', 'LATTICE', 'SHRINKWRAP', 'SIMPLE_DEFORM', 'BEND', 'HOOK'}}
    has_transform_modifiers = False
    for modifier in obj.modifiers:
        if modifier.name in selected_modifiers and modifier.type in modifiers_that_transform_vertices and modifier.show_viewport:
            has_transform_modifiers = True
            break
    
    if has_transform_modifiers:
        print(f"[Worker {{task_id}}] ShapeKeyOptimized: 检测到会变换顶点的修改器，回退到原始算法")
        return apply_modifiers_for_object_with_shape_keys_legacy(context, selected_modifiers, disable_armatures)
    
    start_time = time.time()
    
    contains_mirror_with_merge = False
    for modifier in obj.modifiers:
        if modifier.name in selected_modifiers:
            if modifier.type == 'MIRROR' and modifier.use_mirror_merge == True:
                contains_mirror_with_merge = True
    
    disabled_armature_modifiers = []
    if disable_armatures:
        for modifier in obj.modifiers:
            if modifier.name not in selected_modifiers and modifier.type == 'ARMATURE' and modifier.show_viewport == True:
                disabled_armature_modifiers.append(modifier)
                modifier.show_viewport = False
    
    if not obj.data.shape_keys:
        for modifier_name in selected_modifiers:
            mod = obj.modifiers.get(modifier_name)
            if mod and mod.show_viewport:
                bpy.ops.object.modifier_apply(modifier=modifier_name)
        return (True, None)
    
    shapes_count = len(obj.data.shape_keys.key_blocks)
    
    if shapes_count == 0:
        for modifier_name in selected_modifiers:
            mod = obj.modifiers.get(modifier_name)
            if mod and mod.show_viewport:
                bpy.ops.object.modifier_apply(modifier=modifier_name)
        return (True, None)
    
    print(f"[Worker {{task_id}}] ShapeKeyOptimized: 开始处理 {{shapes_count}} 个形态键")
    
    properties_list = []
    properties = ["interpolation", "mute", "name", "relative_key", "slider_max", "slider_min", "value", "vertex_group"]
    
    for i in range(shapes_count):
        key_b = obj.data.shape_keys.key_blocks[i]
        props = {{p: None for p in properties}}
        props["name"] = key_b.name
        props["mute"] = key_b.mute
        props["interpolation"] = key_b.interpolation
        props["relative_key"] = key_b.relative_key.name
        props["slider_max"] = key_b.slider_max
        props["slider_min"] = key_b.slider_min
        props["value"] = key_b.value
        props["vertex_group"] = key_b.vertex_group
        properties_list.append(props)
    
    original_vert_count = len(obj.data.vertices)
    
    shape_key_coords = []
    for i in range(shapes_count):
        key_b = obj.data.shape_keys.key_blocks[i]
        coords = numpy.empty((original_vert_count, 3), dtype=numpy.float32)
        key_b.data.foreach_get('co', coords.ravel())
        shape_key_coords.append(coords)
    
    print(f"[Worker {{task_id}}] ShapeKeyOptimized: 已提取 {{shapes_count}} 个形态键坐标数据")
    
    bpy.ops.object.shape_key_remove(all=True)
    
    for modifier_name in selected_modifiers:
        bpy.ops.object.modifier_apply(modifier=modifier_name)
    
    new_vert_count = len(obj.data.vertices)
    
    if original_vert_count != new_vert_count:
        error_hint = ""
        if contains_mirror_with_merge:
            error_hint = "\\n提示: 镜像修改器启用了 'Merge' 选项可能导致问题。"
        error_info = (f"顶点数量变化: {{original_vert_count}} -> {{new_vert_count}}！\\n"
                     f"形态键要求修改器应用后顶点数量不变。{{error_hint}}")
        
        for modifier in disabled_armature_modifiers:
            modifier.show_viewport = True
        return (False, error_info)
    
    bpy.ops.object.shape_key_add(from_mix=False)
    
    for i in range(1, shapes_count):
        key_b = obj.shape_key_add(name=properties_list[i]["name"], from_mix=False)
        key_b.data.foreach_set('co', shape_key_coords[i].ravel())
    
    print(f"[Worker {{task_id}}] ShapeKeyOptimized: 已重新创建 {{shapes_count - 1}} 个形态键")
    
    for i in range(shapes_count):
        key_b = obj.data.shape_keys.key_blocks[i]
        key_b.name = properties_list[i]["name"]
        key_b.interpolation = properties_list[i]["interpolation"]
        key_b.mute = properties_list[i]["mute"]
        key_b.slider_max = properties_list[i]["slider_max"]
        key_b.slider_min = properties_list[i]["slider_min"]
        key_b.value = properties_list[i]["value"]
        key_b.vertex_group = properties_list[i]["vertex_group"]
        
        rel_key = properties_list[i]["relative_key"]
        for j in range(shapes_count):
            key_brel = obj.data.shape_keys.key_blocks[j]
            if rel_key == key_brel.name:
                key_b.relative_key = key_brel
                break
    
    for modifier in disabled_armature_modifiers:
        modifier.show_viewport = True
    
    elapsed = time.time() - start_time
    print(f"[Worker {{task_id}}] ShapeKeyOptimized: 完成，耗时: {{elapsed:.2f}}秒")
    
    return (True, None)


def apply_modifiers_for_object_with_shape_keys(context, selected_modifiers, disable_armatures=False):
    """兼容接口：调用优化版"""
    return apply_modifiers_for_object_with_shape_keys_optimized(context, selected_modifiers, disable_armatures)


def apply_modifiers_for_object_with_shape_keys_legacy(context, selected_modifiers, disable_armatures=False):
    """原始算法：用于处理会变换顶点的修改器"""
    if len(selected_modifiers) == 0:
        return (True, None)
    
    properties = ["interpolation", "mute", "name", "relative_key", "slider_max", "slider_min", "value", "vertex_group"]
    list_properties = []
    shapes_count = 0
    vert_count = -1
    start_time_inner = time.time()
    
    contains_mirror_with_merge = False
    for modifier in context.object.modifiers:
        if modifier.name in selected_modifiers:
            if modifier.type == 'MIRROR' and modifier.use_mirror_merge == True:
                contains_mirror_with_merge = True
    
    disabled_armature_modifiers = []
    if disable_armatures:
        for modifier in context.object.modifiers:
            if modifier.name not in selected_modifiers and modifier.type == 'ARMATURE' and modifier.show_viewport == True:
                disabled_armature_modifiers.append(modifier)
                modifier.show_viewport = False
    
    if context.object.data.shape_keys:
        shapes_count = len(context.object.data.shape_keys.key_blocks)
    
    if shapes_count == 0:
        for modifier_name in selected_modifiers:
            bpy.ops.object.modifier_apply(modifier=modifier_name)
        return (True, None)
    
    original_object = context.view_layer.objects.active
    bpy.ops.object.select_all(action='DESELECT')
    original_object.select_set(True)
    
    bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={{"linked":False, "mode":'TRANSLATION'}}, TRANSFORM_OT_translate={{"value":(0, 0, 0)}})
    copy_object = context.view_layer.objects.active
    copy_object.select_set(False)
    
    context.view_layer.objects.active = original_object
    original_object.select_set(True)
    
    for i in range(0, shapes_count):
        key_b = original_object.data.shape_keys.key_blocks[i]
        properties_object = {{p:None for p in properties}}
        properties_object["name"] = key_b.name
        properties_object["mute"] = key_b.mute
        properties_object["interpolation"] = key_b.interpolation
        properties_object["relative_key"] = key_b.relative_key.name
        properties_object["slider_max"] = key_b.slider_max
        properties_object["slider_min"] = key_b.slider_min
        properties_object["value"] = key_b.value
        properties_object["vertex_group"] = key_b.vertex_group
        list_properties.append(properties_object)
    
    print(f"[Worker {{task_id}}] Legacy: Applying base shape key")
    bpy.ops.object.shape_key_remove(all=True)
    for modifier_name in selected_modifiers:
        bpy.ops.object.modifier_apply(modifier=modifier_name)
    vert_count = len(original_object.data.vertices)
    bpy.ops.object.shape_key_add(from_mix=False)
    original_object.select_set(False)
    
    for i in range(1, shapes_count):
        curr_time = time.time()
        elapsed_time = curr_time - start_time_inner
        print(f"[Worker {{task_id}}] Legacy: Applying shape key " + str(i+1) + "/" + str(shapes_count) + " ('" + str(list_properties[i]['name']) + "', " + str(round(elapsed_time, 2)) + "s)")
        
        context.view_layer.objects.active = copy_object
        copy_object.select_set(True)
        
        bpy.ops.object.duplicate_move(OBJECT_OT_duplicate={{"linked":False, "mode":'TRANSLATION'}}, TRANSFORM_OT_translate={{"value":(0, 0, 0)}})
        tmp_object = context.view_layer.objects.active
        bpy.ops.object.shape_key_remove(all=True)
        copy_object.select_set(True)
        copy_object.active_shape_key_index = i
        
        bpy.ops.object.shape_key_transfer()
        context.object.active_shape_key_index = 0
        bpy.ops.object.shape_key_remove()
        bpy.ops.object.shape_key_remove(all=True)
        
        for modifier_name in selected_modifiers:
            bpy.ops.object.modifier_apply(modifier=modifier_name)
        
        if vert_count != len(tmp_object.data.vertices):
            error_info_hint = ""
            if contains_mirror_with_merge == True:
                error_info_hint = "There is mirror modifier with 'Merge' property enabled."
            if error_info_hint:
                error_info_hint = "\\nHint: " + error_info_hint
            error_info = ("Shape keys ended up with different number of vertices!\\n"
                        "All shape keys needs to have the same number of vertices after modifier is applied." + error_info_hint)
            return (False, error_info)
        
        copy_object.select_set(False)
        context.view_layer.objects.active = original_object
        original_object.select_set(True)
        bpy.ops.object.join_shapes()
        original_object.select_set(False)
        context.view_layer.objects.active = tmp_object
        
        tmp_mesh = tmp_object.data
        bpy.ops.object.delete(use_global=False)
        bpy.data.meshes.remove(tmp_mesh)
    
    context.view_layer.objects.active = original_object
    for i in range(0, shapes_count):
        key_b = context.view_layer.objects.active.data.shape_keys.key_blocks[i]
        key_b.name = list_properties[i]["name"]
    
    for i in range(0, shapes_count):
        key_b = context.view_layer.objects.active.data.shape_keys.key_blocks[i]
        key_b.interpolation = list_properties[i]["interpolation"]
        key_b.mute = list_properties[i]["mute"]
        key_b.slider_max = list_properties[i]["slider_max"]
        key_b.slider_min = list_properties[i]["slider_min"]
        key_b.value = list_properties[i]["value"]
        key_b.vertex_group = list_properties[i]["vertex_group"]
        rel_key = list_properties[i]["relative_key"]
        
        for j in range(0, shapes_count):
            key_brel = context.view_layer.objects.active.data.shape_keys.key_blocks[j]
            if rel_key == key_brel.name:
                key_b.relative_key = key_brel
                break
    
    copy_mesh = copy_object.data
    bpy.data.objects.remove(copy_object, do_unlink=True)
    bpy.data.meshes.remove(copy_mesh)
    
    for modifier in disabled_armature_modifiers:
        modifier.show_viewport = True
    
    return (True, None)


def apply_all_modifiers(obj):
    """应用物体上的所有修改器（优化版）
    
    优化：
    1. 先删除禁用的修改器（不应用）
    2. 只应用启用的修改器
    """
    if obj.type != 'MESH':
        return
    if not obj.modifiers:
        return
    
    disabled_modifiers = [mod for mod in obj.modifiers if not mod.show_viewport]
    for mod in reversed(disabled_modifiers):
        obj.modifiers.remove(mod)
    
    if not obj.modifiers:
        return
    
    has_shape_keys = obj.data.shape_keys is not None
    
    if has_shape_keys:
        modifier_names = [mod.name for mod in obj.modifiers]
        apply_modifiers_for_object_with_shape_keys(
            bpy.context, 
            modifier_names, 
            disable_armatures=False
        )
    else:
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        
        for modifier in obj.modifiers[:]:
            try:
                bpy.ops.object.modifier_apply(modifier=modifier.name)
            except Exception as e:
                print(f"[Worker {{task_id}}] 应用修改器失败 {{modifier.name}}: {{e}}")


def prepare_copy_for_mirror_workflow(copy_obj):
    """
    为非镜像工作流准备副本 - 与单进程模式完全一致
    
    优化：
    1. 只检查启用的骨骼修改器
    2. 禁用的修改器会在 apply_all_modifiers 中删除
    
    情况一：物体包含启用的骨骼绑定但无形态键 -> 应用所有修改器
    情况二：物体同时包含启用的骨骼绑定和形态键 -> 使用特殊方式处理
    情况三：物体没有启用的骨骼绑定 -> 跳过前处理
    """
    if copy_obj.type != 'MESH':
        return
    
    has_enabled_armature = any(
        mod.type == 'ARMATURE' and mod.show_viewport 
        for mod in copy_obj.modifiers
    )
    has_shape_keys = copy_obj.data.shape_keys is not None
    
    if not has_enabled_armature:
        return
    
    if has_shape_keys:
        shape_key_values = {{}}
        for kb in copy_obj.data.shape_keys.key_blocks:
            shape_key_values[kb.name] = kb.value
        
        reset_shapekey_values(copy_obj)
        
        disabled_modifiers = [mod for mod in copy_obj.modifiers if not mod.show_viewport]
        for mod in reversed(disabled_modifiers):
            copy_obj.modifiers.remove(mod)
        
        modifier_names = [mod.name for mod in copy_obj.modifiers]
        if modifier_names:
            bpy.context.view_layer.objects.active = copy_obj
            apply_modifiers_for_object_with_shape_keys(
                bpy.context,
                modifier_names,
                disable_armatures=False
            )
        
        if copy_obj.data.shape_keys:
            for kb in copy_obj.data.shape_keys.key_blocks:
                if kb.name in shape_key_values:
                    kb.value = shape_key_values[kb.name]
    else:
        apply_all_modifiers(copy_obj)


def clear_materials(obj):
    """清除物体的所有材质槽，减少文件体积"""
    if obj.type != 'MESH':
        return
    
    if obj.data.materials:
        obj.data.materials.clear()
    
    for slot in obj.material_slots[:]:
        obj.active_material_index = slot.slot_index
        bpy.ops.object.material_slot_remove()


def mesh_triangulate_beauty(obj):
    """使用 BEAUTY 算法进行三角化（布线优化）"""
    if obj.type != 'MESH':
        return
    
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
    bpy.ops.object.mode_set(mode='OBJECT')


def apply_mirror_transform(obj):
    """应用镜像变换：Scale X = -1"""
    if obj.type != 'MESH':
        return
    
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    
    obj.scale[0] = -obj.scale[0]
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)


def flip_face_normals(obj):
    """翻转面朝向"""
    if obj.type != 'MESH':
        return
    
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.flip_normals()
    bpy.ops.object.mode_set(mode='OBJECT')


def process_vertex_groups(obj, vg_mapping_texts):
    """处理顶点组：重命名、合并、清理、填充、排序（优化版）"""
    if obj.type != 'MESH':
        return {{"renamed": 0, "merged": 0, "cleaned": 0, "filled": 0}}
    
    stats = {{"renamed": 0, "merged": 0, "cleaned": 0, "filled": 0}}
    
    # 1. 重命名顶点组
    if vg_mapping_texts:
        mapping = {{}}
        for text_name, text_content in vg_mapping_texts.items():
            for line in text_content.split('\\n'):
                line = line.strip()
                if not line or '=' not in line:
                    continue
                parts = line.split('=', 1)
                if len(parts) == 2:
                    left = parts[0].strip()
                    right = parts[1].strip()
                    if left and right:
                        mapping[left] = right
        
        if mapping:
            for vg in obj.vertex_groups:
                if vg.name in mapping:
                    new_name = mapping[vg.name]
                    if vg.name != new_name:
                        if new_name in obj.vertex_groups:
                            existing_vg = obj.vertex_groups[new_name]
                            existing_vg.name = new_name + ".001"
                        vg.name = new_name
                        stats["renamed"] += 1
    
    # 2. 合并顶点组（按数字前缀）- 优化版
    from collections import defaultdict
    import re
    prefix_map = defaultdict(list)
    for vg in obj.vertex_groups:
        match = re.match(r'^(\d+)', vg.name)
        if match:
            prefix_map[match.group(1)].append(vg)
    
    for prefix, source_groups in prefix_map.items():
        if len(source_groups) > 1 or (len(source_groups) == 1 and source_groups[0].name != prefix):
            target_vg = obj.vertex_groups.get(prefix) or obj.vertex_groups.new(name=prefix)
            
            # 优化：批量收集顶点权重，减少API调用
            vertex_weights = []
            for vert in obj.data.vertices:
                total_weight = 0.0
                for source_vg in source_groups:
                    try:
                        total_weight += source_vg.weight(vert.index)
                    except RuntimeError:
                        continue
                
                if total_weight > 0:
                    vertex_weights.append((vert.index, min(1.0, total_weight)))
            
            # 批量添加权重
            if vertex_weights:
                for vert_idx, weight in vertex_weights:
                    target_vg.add([vert_idx], weight, 'REPLACE')
            
            for vg in source_groups:
                if vg.name in obj.vertex_groups and vg.name != prefix:
                    obj.vertex_groups.remove(vg)
            stats["merged"] += 1
    
    # 3. 清理非数字顶点组 - 优化版：使用集合操作
    groups_to_remove = [vg for vg in obj.vertex_groups if not vg.name.isdigit()]
    for vg in reversed(groups_to_remove):
        obj.vertex_groups.remove(vg)
    stats["cleaned"] = len(groups_to_remove)
    
    # 4. 填充顶点组间隙 - 优化版：使用集合差集
    numeric_names = set(vg.name for vg in obj.vertex_groups if vg.name.isdigit())
    if numeric_names:
        max_num = max(int(name) for name in numeric_names)
        # 使用集合差集快速找出缺失的数字
        existing_nums = set(int(name) for name in numeric_names)
        missing_nums = set(range(max_num + 1)) - existing_nums
        
        for num in sorted(missing_nums):
            obj.vertex_groups.new(name=str(num))
            stats["filled"] += 1
    
    # 5. 排序顶点组
    try:
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.vertex_group_sort(sort_type='NAME')
    except Exception:
        pass
    
    return stats


processed_objects = []

for obj_name in object_names:
    try:
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            print(f"[Worker {{task_id}}] 跳过不存在的物体: {{obj_name}}")
            continue
        
        if obj.type != 'MESH':
            continue
        
        # 1. 创建副本，使用标准命名规范
        copy_obj = obj.copy()
        copy_obj.data = obj.data.copy()
        if obj_name.endswith("-Original"):
            copy_obj.name = obj_name.replace("-Original", "-copy_Original")
        else:
            copy_obj.name = obj_name + "_copy"
        bpy.context.scene.collection.objects.link(copy_obj)
        
        # 优化：先删除禁用的修改器，减少后续处理开销
        disabled_modifiers = [mod for mod in copy_obj.modifiers if not mod.show_viewport]
        for mod in reversed(disabled_modifiers):
            copy_obj.modifiers.remove(mod)
        
        # 2. 应用修改器 - 与单进程模式完全一致
        # 优化：只检查启用的骨骼修改器
        has_enabled_armature = any(
            mod.type == 'ARMATURE' and mod.show_viewport 
            for mod in copy_obj.modifiers
        )
        if mirror_workflow:
            try:
                prepare_copy_for_mirror_workflow(copy_obj)
            except Exception as e:
                print(f"[Worker {{task_id}}] 前处理失败 {{copy_obj.name}}: {{e}}")
                traceback.print_exc()
        elif has_enabled_armature:
            try:
                apply_all_modifiers(copy_obj)
            except Exception as e:
                print(f"[Worker {{task_id}}] 应用修改器失败 {{copy_obj.name}}: {{e}}")
                traceback.print_exc()
        
        # 3. BEAUTY三角化
        try:
            mesh_triangulate_beauty(copy_obj)
        except Exception as e:
            print(f"[Worker {{task_id}}] 三角化失败 {{copy_obj.name}}: {{e}}")
            traceback.print_exc()
        
        # 4. 清除材质，减少文件体积
        try:
            clear_materials(copy_obj)
        except Exception as e:
            print(f"[Worker {{task_id}}] 清除材质失败 {{copy_obj.name}}: {{e}}")
        
        # 5. 非镜像工作流后处理 - 与单进程模式完全一致
        if mirror_workflow:
            try:
                apply_mirror_transform(copy_obj)
                flip_face_normals(copy_obj)
            except Exception as e:
                print(f"[Worker {{task_id}}] 后处理失败 {{copy_obj.name}}: {{e}}")
                traceback.print_exc()
        
        # 记录副本名称（用于加载时匹配）
        processed_objects.append(copy_obj.name)
        
    except Exception as e:
        print(f"[Worker {{task_id}}] 处理物体 {{obj_name}} 时出错: {{e}}")
        traceback.print_exc()

# 删除所有非副本物体，只保留预处理后的副本
copy_names_set = set(processed_objects)
objects_to_remove = [obj for obj in bpy.data.objects if obj.name not in copy_names_set]
for obj in objects_to_remove:
    bpy.data.objects.remove(obj, do_unlink=True)

# 清理未使用的材质和数据块，进一步减小体积
try:
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
except:
    pass

# 保存预处理结果
try:
    bpy.ops.wm.save_as_mainfile(filepath=output_blend, compress=True)
except Exception as e:
    print(f"[Worker {{task_id}}] 保存失败: {{e}}")
    traceback.print_exc()

# 写入结果文件
result_file = output_blend.replace('.blend', '_result.json')
result_data = {{
    "task_id": task_id,
    "success": True,
    "processed_objects": processed_objects,
    "output_blend": output_blend
}}
try:
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result_data, f, ensure_ascii=False)
except Exception as e:
    print(f"[Worker {{task_id}}] 写入结果失败: {{e}}")

print(f"[Worker {{task_id}}] 完成: {{len(processed_objects)}} 个物体")
'''
            
            script_file = os.path.join(
                os.path.dirname(task.output_blend),
                f"preprocess_script_{task.task_id}.py"
            )
            
            with open(script_file, 'w', encoding='utf-8') as f:
                f.write(script)
            
            print(f"[Worker {task.task_id}] 启动 Blender: {blender_exe}")
            print(f"[Worker {task.task_id}] 项目文件: {task.blend_file}")
            print(f"[Worker {task.task_id}] 脚本文件: {script_file}")
            print(f"[Worker {task.task_id}] 输出文件: {task.output_blend}")
            print(f"[Worker {task.task_id}] 物体数量: {len(task.object_names)}")
            
            cmd = [
                blender_exe,
                '-b',
                task.blend_file,
                '-P', script_file
            ]
            
            print(f"[Worker {task.task_id}] 命令: {' '.join(cmd)}")
            
            # 检查文件是否存在
            if not os.path.exists(blender_exe):
                print(f"[Worker {task.task_id}] 错误: Blender可执行文件不存在: {blender_exe}")
                return PreprocessResult(
                    task_id=task.task_id,
                    success=False,
                    error_message=f"Blender可执行文件不存在: {blender_exe}",
                    processed_objects=[],
                    output_blend="",
                    processing_time=0
                )
            
            if not os.path.exists(task.blend_file):
                print(f"[Worker {task.task_id}] 错误: 项目文件不存在: {task.blend_file}")
                return PreprocessResult(
                    task_id=task.task_id,
                    success=False,
                    error_message=f"项目文件不存在: {task.blend_file}",
                    processed_objects=[],
                    output_blend="",
                    processing_time=0
                )
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            
            stdout, _ = process.communicate(timeout=1800)
            
            log_file = os.path.join(
                os.path.dirname(task.output_blend),
                f"worker_{task.task_id}_log.txt"
            )
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(stdout)
            
            print(f"[Worker {task.task_id}] 日志文件: {log_file}")
            print(f"[Worker {task.task_id}] 返回码: {process.returncode}")
            
            lines = stdout.split('\n')
            for line in lines[-50:]:
                if line.strip():
                    print(f"[Worker {task.task_id}] {line}")
            
            processed_objects = []
            if os.path.exists(task.output_blend):
                processed_objects = task.object_names
            
            processing_time = (datetime.now() - start_time).total_seconds()
            
            return PreprocessResult(
                task_id=task.task_id,
                success=process.returncode == 0 and os.path.exists(task.output_blend),
                error_message=stdout if process.returncode != 0 else "",
                processed_objects=processed_objects,
                output_blend=task.output_blend,
                processing_time=processing_time
            )
            
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            return PreprocessResult(
                task_id=task.task_id,
                success=False,
                error_message="预处理超时",
                processed_objects=[],
                output_blend="",
                processing_time=600
            )
        except Exception as e:
            return PreprocessResult(
                task_id=task.task_id,
                success=False,
                error_message=str(e),
                processed_objects=[],
                output_blend="",
                processing_time=(datetime.now() - start_time).total_seconds()
            )
    
    def _collect_results(self) -> Dict[str, str]:
        """收集预处理结果，返回副本名到blend文件的映射"""
        object_blend_map = {}
        
        for result in self.results:
            if result.success and os.path.exists(result.output_blend):
                result_file = result.output_blend.replace('.blend', '_result.json')
                if os.path.exists(result_file):
                    try:
                        with open(result_file, 'r', encoding='utf-8') as f:
                            result_data = json.load(f)
                        for obj_name in result_data.get('processed_objects', []):
                            object_blend_map[obj_name] = result.output_blend
                            print(f"[ParallelPreprocess] 收集副本: {obj_name}")
                    except Exception as e:
                        print(f"[ParallelPreprocess] 读取结果文件失败: {e}")
                else:
                    print(f"[ParallelPreprocess] 结果文件不存在: {result_file}")
        
        print(f"[ParallelPreprocess] 收集结果: {len(object_blend_map)} 个副本")
        return object_blend_map
    
    @staticmethod
    def _find_blender_executable() -> str:
        """查找 Blender 可执行文件"""
        from ..config.properties_import_model import Properties_ImportModel
        
        user_path = Properties_ImportModel.get_blender_executable_path()
        if user_path:
            print(f"[ParallelPreprocess] 使用用户指定的 Blender: {user_path}")
            return user_path
        
        import bpy
        current_blender = bpy.app.binary_path
        if current_blender and os.path.exists(current_blender):
            print(f"[ParallelPreprocess] 使用当前 Blender: {current_blender}")
            return current_blender
        
        if sys.platform == 'win32':
            common_paths = [
                r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
                r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
                r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
                r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
                r"C:\Program Files\Blender Foundation\Blender 4.1\blender.exe",
                r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
                r"C:\Program Files\Blender Foundation\Blender 3.6\blender.exe",
            ]
            
            for path in common_paths:
                if os.path.exists(path):
                    return path
            
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\BlenderFoundation")
                install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                return os.path.join(install_path, "blender.exe")
            except:
                pass
        
        return "blender"


def load_preprocessed_objects(object_blend_map: Dict[str, str]):
    """
    从预处理文件加载物体到当前场景
    
    Args:
        object_blend_map: {副本名: blend文件路径}
    
    Returns:
        {副本名: 加载的物体对象}
    """
    import bpy
    
    loaded_objects = {}
    
    blend_files = set(object_blend_map.values())
    print(f"[LoadPreprocessed] 需要加载 {len(object_blend_map)} 个副本，来自 {len(blend_files)} 个文件")
    print(f"[LoadPreprocessed] 期望的副本名称: {list(object_blend_map.keys())}")
    
    for blend_file in blend_files:
        if not os.path.exists(blend_file):
            print(f"[LoadPreprocessed] 文件不存在: {blend_file}")
            continue
        
        print(f"[LoadPreprocessed] 加载文件: {blend_file}")
        
        with bpy.data.libraries.load(blend_file, link=False) as (data_from, data_to):
            print(f"[LoadPreprocessed] 文件中的物体列表: {data_from.objects}")
            data_to.objects = data_from.objects
        
        loaded_count = 0
        for obj in data_to.objects:
            if obj:
                print(f"[LoadPreprocessed] 加载的对象: {obj.name}, 类型: {obj.type}")
                if obj.type == 'MESH':
                    obj_name = obj.name
                    if obj_name in object_blend_map:
                        bpy.context.scene.collection.objects.link(obj)
                        loaded_objects[obj_name] = obj
                        loaded_count += 1
                        print(f"[LoadPreprocessed] 匹配成功: {obj_name}")
                    else:
                        print(f"[LoadPreprocessed] 未匹配: {obj_name}")
        
        print(f"[LoadPreprocessed] 从 {blend_file} 加载了 {loaded_count} 个副本")
    
    print(f"[LoadPreprocessed] 总共加载了 {len(loaded_objects)} 个副本")
    return loaded_objects
