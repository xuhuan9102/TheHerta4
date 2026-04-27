from dataclasses import dataclass, field
import numpy

from .draw_call_model import DrawCallModel

from ..utils.export_utils import ExportUtils
from ..utils.obj_utils import ObjUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.json_utils import JsonUtils
from ..utils.timer_utils import TimerUtils
from .logic_name import LogicName
from .global_config import GlobalConfig
from .d3d11_gametype import D3D11GameType
from .obj_buffer_helper import ObjBufferHelper
from .submesh_metadata import SubmeshMetadataResolver


import bpy
import math
import os
import array
import hashlib
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter


@dataclass
class SubMeshModel:
    # 初始化时需要填入此属性
    drawcall_model_list:list[DrawCallModel] = field(default_factory=list)
    source_obj_unique_str_count:dict[str, int] = field(default_factory=dict, repr=False)
    has_multi_file_export_nodes:bool = False

    # post_init中计算得到这些属性
    match_draw_ib:str = field(init=False, default="")
    match_first_index:int = field(init=False, default=-1)
    match_index_count:int = field(init=False, default=-1)
    unique_str:str = field(init=False, default="")

    # 调用组合obj并计算ib和vb得到这些属性
    vertex_count:int = field(init=False, default=0)
    index_count:int = field(init=False, default=0)

    # 读取工作空间中的 Import.json 选择数据类型目录，再从对应的 SubmeshJson 获取 d3d11GameType
    d3d11_game_type:D3D11GameType = field(init=False,repr=False,default=None)

    ib:list = field(init=False,repr=False,default_factory=list)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict) 
    shape_key_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)

    def __post_init__(self):

        # 因为列表里的每个DrawCallModel的draw_ib,first_index,index_count都是一样的，所以直接取第一个就行了
        if len(self.drawcall_model_list) > 0:
            self.match_draw_ib = self.drawcall_model_list[0].match_draw_ib
            self.match_first_index = self.drawcall_model_list[0].match_first_index
            self.match_index_count = self.drawcall_model_list[0].match_index_count
            self.unique_str = self.drawcall_model_list[0].get_unique_str()
        
        self.calc_buffer()
    

    def calc_buffer(self):
        folder_name = self.unique_str

        submesh_metadata = SubmeshMetadataResolver.resolve(folder_name)
        self.d3d11_game_type = submesh_metadata.d3d11_game_type

        TimerUtils.start_stage("数据哈希预计算")
        object_hashes, source_obj_list = self._precompute_object_hashes()
        TimerUtils.end_stage("数据哈希预计算")
        
        # 获取每个对象的原始名称（用于判断是否来自同一个物体）
        # 只有来自同一个原始物体的分裂物体才能复用
        original_names = []
        for draw_call_model in self.drawcall_model_list:
            source_name = draw_call_model.source_obj_name
            if source_name:
                if source_name.endswith('_copy'):
                    source_name = source_name[:-5]
                source_name = re.sub(r'_chain\d+$', '', source_name)
            original_names.append(source_name if source_name else draw_call_model.get_blender_obj_name())

        # 调试输出：打印所有对象的哈希值和原始名称
        print(f"[SubMeshModel] 数据哈希预计算完成: {len(self.drawcall_model_list)} 个对象")
        hash_groups = {}
        none_hash_names = []
        for i, h in enumerate(object_hashes):
            obj_name = self.drawcall_model_list[i].get_blender_obj_name()
            orig_name = original_names[i]
            if h is None:
                none_hash_names.append(obj_name)
                continue
            # 使用 (hash, original_name) 作为分组 key
            group_key = (h, orig_name)
            if group_key not in hash_groups:
                hash_groups[group_key] = []
            hash_groups[group_key].append(obj_name)
        if none_hash_names:
            print(f"  ⚠️ 无法计算哈希（对象未找到）: {', '.join(none_hash_names)}")
        for (h, orig_name), names in hash_groups.items():
            if len(names) > 1:
                print(f"  📋 哈希 {h[:16]}... (原始: {orig_name}) → {len(names)} 个对象可复用: {', '.join(names)}")
            else:
                print(f"  📋 哈希 {h[:16]}... (原始: {orig_name}) → 独立对象: {names[0]}")

        index_offset = 0
        submesh_temp_obj_list = []
        data_hash_cache = {}

        reuse_count = 0
        direct_source_reuse_count = 0
        duplicated_temp_count = 0
        merged_obj_uses_preprocessed_copy = False
        copy_duration = 0.0
        join_duration = 0.0
        normalize_duration = 0.0
        rotate_duration = 0.0

        temp_collection = CollectionUtils.create_new_collection("TEMP_SUBMESH_COLLECTION_" + self.unique_str)
        bpy.context.scene.collection.children.link(temp_collection)

        TimerUtils.start_stage("对象处理与合并")

        need_normalize = self.d3d11_game_type is not None and "Blend" in self.d3d11_game_type.OrderedCategoryNameList
        need_rotate = (GlobalConfig.logic_name == LogicName.SRMI
            or GlobalConfig.logic_name == LogicName.GIMI
            or GlobalConfig.logic_name == LogicName.HIMI
            or GlobalConfig.logic_name == LogicName.YYSLS
            or GlobalConfig.logic_name == LogicName.IdentityV
            or GlobalConfig.logic_name == LogicName.EFMI)

        for i, draw_call_model in enumerate(self.drawcall_model_list):
            blender_obj_name = draw_call_model.get_blender_obj_name()
            source_obj = source_obj_list[i]

            obj_hash = object_hashes[i]
            if obj_hash is None:
                obj_hash = f"FALLBACK_{blender_obj_name}"
            original_name = original_names[i]
            cache_key = (obj_hash, original_name)
            cached_result = data_hash_cache.get(cache_key)

            if cached_result is not None:
                cached_offset, cached_vertex_count, cached_index_count = cached_result
                draw_call_model.vertex_count = cached_vertex_count
                draw_call_model.index_count = cached_index_count
                draw_call_model.index_offset = cached_offset
                reuse_count += 1
                print(f"  ♻️复用: '{blender_obj_name}' (原始: {original_name}) → offset={cached_offset}, vertices={cached_vertex_count}, indices={cached_index_count}")
                continue

            if source_obj is None:
                from ..utils.ssmt_error_utils import SSMTErrorUtils
                tried_names = [blender_obj_name]
                if draw_call_model.source_obj_name and draw_call_model.source_obj_name != blender_obj_name:
                    tried_names.append(draw_call_model.source_obj_name)
                if draw_call_model.obj_name and draw_call_model.obj_name not in tried_names:
                    tried_names.append(draw_call_model.obj_name)
                SSMTErrorUtils.raise_fatal(
                    f"找不到 Blender 对象: '{blender_obj_name}'"
                    f" (已尝试: {', '.join(tried_names)})"
                    f" — 请检查重命名节点是否正确配置"
                )

            print(f"  🆕 处理: '{blender_obj_name}' (原始: {original_name}, hash={obj_hash[:16]}...)")

            if self._should_duplicate_source_for_merge(source_obj):
                copy_start = perf_counter()
                new_obj = source_obj.copy()
                new_obj.data = source_obj.data.copy()
                new_obj.name = source_obj.name + "_temp"
                temp_collection.objects.link(new_obj)
                temp_obj = new_obj
                duplicated_temp_count += 1
                copy_duration += perf_counter() - copy_start
            else:
                temp_obj = source_obj
                direct_source_reuse_count += 1

            draw_call_model.vertex_count = len(temp_obj.data.vertices)
            draw_call_model.index_count = len(temp_obj.data.polygons) * 3
            draw_call_model.index_offset = index_offset

            data_hash_cache[cache_key] = (index_offset, draw_call_model.vertex_count, draw_call_model.index_count)

            index_offset += draw_call_model.index_count
            self.vertex_count += draw_call_model.vertex_count
            self.index_count += draw_call_model.index_count

            submesh_temp_obj_list.append(temp_obj)

        total = len(self.drawcall_model_list)
        if reuse_count > 0:
            print(f"[SubMeshModel] 数据复用统计: {reuse_count}/{total} 个对象复用, {total - reuse_count}/{total} 个对象独立处理")
        else:
            print(f"[SubMeshModel] 数据复用统计: 无复用, 全部 {total} 个对象独立处理")
        print(f"[SubMeshModel] 合并输入统计: 直接复用前处理副本 {direct_source_reuse_count} 个, 额外复制临时物体 {duplicated_temp_count} 个")

        join_start = perf_counter()
        if submesh_temp_obj_list:
            valid_temp_obj_list = []
            for temp_obj in submesh_temp_obj_list:
                try:
                    if temp_obj is not None and temp_obj.name in bpy.data.objects:
                        valid_temp_obj_list.append(temp_obj)
                except ReferenceError:
                    continue
            if valid_temp_obj_list:
                ObjUtils.join_objects_fast(valid_temp_obj_list[0], valid_temp_obj_list[1:])
            submesh_temp_obj_list = valid_temp_obj_list
        join_duration += perf_counter() - join_start

        if not submesh_temp_obj_list:
            from ..utils.ssmt_error_utils import SSMTErrorUtils
            SSMTErrorUtils.raise_fatal(f"SubMesh {self.unique_str} 没有有效的对象可供合并导出")

        submesh_merged_obj = submesh_temp_obj_list[0]
        merged_obj_uses_preprocessed_copy = submesh_merged_obj.name.endswith('_copy')
        if not merged_obj_uses_preprocessed_copy:
            merged_obj_name = "TEMP_SUBMESH_MERGED_" + self.unique_str
            ObjUtils.rename_object(submesh_merged_obj, merged_obj_name)

        if need_normalize:
            normalize_start = perf_counter()
            self._normalize_temp_obj_for_export(submesh_merged_obj)
            normalize_duration += perf_counter() - normalize_start

        if need_rotate:
            rotate_start = perf_counter()
            self._apply_export_rotation_for_logic(submesh_merged_obj)
            rotate_duration += perf_counter() - rotate_start

        TimerUtils.end_stage("对象处理与合并")

        # 检查并校验是否有缺少的元素
        ObjBufferHelper.check_and_verify_attributes(obj=submesh_merged_obj, d3d11_game_type=self.d3d11_game_type)

        obj_buffer_result = ExportUtils.build_unity_obj_buffer_result(
            obj=submesh_merged_obj,
            d3d11_game_type=self.d3d11_game_type,
        )
        self.ib = obj_buffer_result.ib
        self.category_buffer_dict = obj_buffer_result.category_buffer_dict
        self.index_vertex_id_dict = obj_buffer_result.index_loop_id_dict
        self.shape_key_buffer_dict = obj_buffer_result.shape_key_buffer_dict

        # 计算完成后，删除临时对象
        if not merged_obj_uses_preprocessed_copy:
            bpy.data.objects.remove(submesh_merged_obj, do_unlink=True)

        if temp_collection.name in bpy.data.collections:
            if temp_collection.name in bpy.context.scene.collection.children:
                bpy.context.scene.collection.children.unlink(temp_collection)
            bpy.data.collections.remove(temp_collection)

        if merged_obj_uses_preprocessed_copy:
            print("SubMeshModel: " + self.unique_str + " 计算完成，复用的前处理副本保留到轮次清理阶段")
        else:
            print("SubMeshModel: " + self.unique_str + " 计算完成，临时对象已删除")
        print(
            f"[SubMeshModel] 阶段细分: copy={copy_duration:.3f}s, join={join_duration:.3f}s, "
            f"normalize={normalize_duration:.3f}s, rotate={rotate_duration:.3f}s"
        )

        self._deduplicate_draw_calls()

    def _should_duplicate_source_for_merge(self, source_obj: bpy.types.Object) -> bool:
        if source_obj is None:
            return True

        if len(self.drawcall_model_list) != 1:
            return True

        if self.has_multi_file_export_nodes:
            return True

        if not source_obj.name.endswith('_copy'):
            return True

        unique_str_count = self.source_obj_unique_str_count.get(source_obj.name, 0)
        return unique_str_count > 1

    def _deduplicate_draw_calls(self):
        if len(self.drawcall_model_list) <= 1:
            return

        seen_keys = set()
        deduped = []
        for dcm in self.drawcall_model_list:
            condition_str = dcm.get_condition_str()
            draw_key = (dcm.index_offset, dcm.index_count, condition_str)
            if condition_str or draw_key not in seen_keys:
                seen_keys.add(draw_key)
                deduped.append(dcm)

        removed = len(self.drawcall_model_list) - len(deduped)
        if removed > 0:
            print(f"[SubMeshModel] 绘制去重: {self.unique_str} 移除 {removed} 个重复绘制 (原始 {len(self.drawcall_model_list)} → 保留 {len(deduped)})")
            self.drawcall_model_list = deduped

    def _precompute_object_hashes(self) -> tuple:
        """预计算所有源对象的数据哈希，使用多线程并行计算
        
        流程：
        1. 主线程中从 Blender 对象提取原始数据（Blender API 非线程安全）
        2. 多线程并行计算哈希值（纯 Python 计算，线程安全）
        
        Returns:
            (hashes, source_obj_list): 哈希列表和源对象引用列表
        """
        raw_data_list = []
        source_obj_list = []
        for draw_call_model in self.drawcall_model_list:
            blender_obj_name = draw_call_model.get_blender_obj_name()
            source_obj = ObjUtils.get_obj_by_name(blender_obj_name)
            source_obj_list.append(source_obj)

            if source_obj is None:
                raw_data_list.append(None)
                continue

            raw_data_list.append(self._extract_object_raw_data(source_obj))

        # 第二步：使用多线程并行计算哈希
        def compute_hash(raw_data):
            if raw_data is None:
                return None
            h = hashlib.md5()
            for item in raw_data:
                if isinstance(item, bytes):
                    h.update(item)
                elif isinstance(item, str):
                    h.update(item.encode('utf-8'))
                elif isinstance(item, int):
                    h.update(struct.pack('i', item))
                elif isinstance(item, float):
                    h.update(struct.pack('f', item))
            return h.hexdigest()

        if len(raw_data_list) > 8:
            try:
                with ThreadPoolExecutor() as executor:
                    hashes = list(executor.map(compute_hash, raw_data_list))
            except Exception:
                hashes = [compute_hash(raw_data) for raw_data in raw_data_list]
        else:
            hashes = [compute_hash(raw_data) for raw_data in raw_data_list]

        return hashes, source_obj_list

    @staticmethod
    def _extract_object_raw_data(obj) -> list:
        """从 Blender 对象提取用于哈希计算的原始数据
        
        包含：顶点位置、顶点组名称和权重、UV 数据
        使用 foreach_get 批量读取，性能远优于逐顶点迭代
        """
        mesh = obj.data
        raw_data = []

        # 1. 顶点位置数据
        vert_count = len(mesh.vertices)
        raw_data.append(vert_count)
        if vert_count > 0:
            coords = array.array('f', [0.0] * (vert_count * 3))
            mesh.vertices.foreach_get('co', coords)
            raw_data.append(coords.tobytes())

        # 2. 顶点组名称（顶点组存储在 Object 层面，不同对象可以不同）
        vg_count = len(obj.vertex_groups)
        raw_data.append(vg_count)
        for vg in obj.vertex_groups:
            raw_data.append(vg.name)

        # 3. 顶点组权重数据
        if vert_count > 0 and vg_count > 0:
            for vi, vert in enumerate(mesh.vertices):
                for group in vert.groups:
                    raw_data.append(vi)
                    raw_data.append(group.group)
                    raw_data.append(group.weight)

        # 4. UV 数据
        uv_layer_count = len(mesh.uv_layers)
        raw_data.append(uv_layer_count)
        for uv_layer in mesh.uv_layers:
            uv_count = len(uv_layer.data)
            raw_data.append(uv_count)
            if uv_count > 0:
                uvs = array.array('f', [0.0] * (uv_count * 2))
                uv_layer.data.foreach_get('uv', uvs)
                raw_data.append(uvs.tobytes())

        return raw_data

    def _normalize_temp_obj_for_export(self, temp_obj: bpy.types.Object):
        if self.d3d11_game_type is None:
            return

        if "Blend" not in self.d3d11_game_type.OrderedCategoryNameList:
            return

        if ObjUtils.is_all_vertex_groups_locked(temp_obj):
            return

        self._normalize_vertex_groups_numpy(temp_obj)

    @staticmethod
    def _normalize_vertex_groups_numpy(obj: bpy.types.Object):
        mesh = obj.data
        vgroups = obj.vertex_groups

        if len(mesh.vertices) == 0 or len(vgroups) == 0:
            return

        for vg in vgroups:
            if vg.lock_weight:
                vg.lock_weight = False

        import bmesh

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            deform_layer = bm.verts.layers.deform.verify()

            changed_count = 0
            for vert in bm.verts:
                deform_data = vert[deform_layer]
                if not deform_data:
                    continue

                total_weight = sum(deform_data.values())
                if total_weight <= 0.0 or abs(total_weight - 1.0) <= 1e-7:
                    continue

                inv_total = 1.0 / total_weight
                for group_index in list(deform_data.keys()):
                    deform_data[group_index] *= inv_total
                changed_count += 1

            if changed_count == 0:
                return

            bm.to_mesh(mesh)
            mesh.update()
        finally:
            bm.free()

        if not obj.data.vertices:
            return

    def _apply_export_rotation_for_logic(self, temp_obj: bpy.types.Object):
        if (GlobalConfig.logic_name == LogicName.SRMI
            or GlobalConfig.logic_name == LogicName.GIMI
            or GlobalConfig.logic_name == LogicName.HIMI
            or GlobalConfig.logic_name == LogicName.YYSLS
            or GlobalConfig.logic_name == LogicName.IdentityV):
            ObjUtils.select_obj(temp_obj)
            temp_obj.rotation_euler[0] = math.radians(-90)
            temp_obj.rotation_euler[1] = 0
            temp_obj.rotation_euler[2] = 0
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        elif GlobalConfig.logic_name == LogicName.EFMI:
            ObjUtils.select_obj(temp_obj)
            temp_obj.rotation_euler[0] = 0
            temp_obj.rotation_euler[1] = 0
            temp_obj.rotation_euler[2] = 0
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)