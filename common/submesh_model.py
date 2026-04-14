from dataclasses import dataclass, field
from .draw_call_model import DrawCallModel

from ..utils.export_utils import ExportUtils
from ..utils.obj_utils import ObjUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.json_utils import JsonUtils
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
import struct
from concurrent.futures import ThreadPoolExecutor


@dataclass
class SubMeshModel:
    # 初始化时需要填入此属性
    drawcall_model_list:list[DrawCallModel] = field(default_factory=list)

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

        # 读取 SubmeshJson 获取 d3d11GameType
        submesh_metadata = SubmeshMetadataResolver.resolve(folder_name)
        self.d3d11_game_type = submesh_metadata.d3d11_game_type

        # 预计算所有源对象的数据哈希，用于判断是否可以复用
        # 哈希包含顶点位置、顶点组名称/权重、UV 数据等
        # 使用多线程并行计算，避免产生大量性能开销
        object_hashes = self._precompute_object_hashes()
        
        # 获取每个对象的原始名称（用于判断是否来自同一个物体）
        # 只有来自同一个原始物体的分裂物体才能复用
        original_names = []
        for draw_call_model in self.drawcall_model_list:
            source_name = draw_call_model.source_obj_name
            if source_name:
                if source_name.endswith('_copy'):
                    source_name = source_name[:-5]
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
        # 临时对象列表，用于后续合并
        submesh_temp_obj_list = []
        # 临时集合列表，用于后续清理
        temp_collection_list = []
        # 数据哈希缓存：(hash, original_name) → (index_offset, vertex_count, index_count)
        # 只有来自同一个原始物体且哈希相同的对象才能复用
        data_hash_cache = {}

        reuse_count = 0

        for i, draw_call_model in enumerate(self.drawcall_model_list):
            blender_obj_name = draw_call_model.get_blender_obj_name()
            source_obj = ObjUtils.get_obj_by_name(blender_obj_name)

            # 检查对象是否存在，不存在则报错
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

            # 用 (数据哈希, 原始名称) 作为缓存 key
            # 只有来自同一个原始物体且数据完全相同的对象才能复用
            obj_hash = object_hashes[i]
            if obj_hash is None:
                obj_hash = f"FALLBACK_{blender_obj_name}"
            original_name = original_names[i]
            cache_key = (obj_hash, original_name)
            cached_result = data_hash_cache.get(cache_key)

            if cached_result is not None:
                # 缓存命中：直接复用已有的 index_offset、vertex_count、index_count
                # 不再重复创建临时对象、三角化、写入 buffer
                cached_offset, cached_vertex_count, cached_index_count = cached_result
                draw_call_model.vertex_count = cached_vertex_count
                draw_call_model.index_count = cached_index_count
                draw_call_model.index_offset = cached_offset
                reuse_count += 1
                print(f"  ♻️复用: '{blender_obj_name}' (原始: {original_name}) → offset={cached_offset}, vertices={cached_vertex_count}, indices={cached_index_count}")
                continue

            # 缓存未命中：创建临时集合和临时对象进行处理
            print(f"  🆕 处理: '{blender_obj_name}' (原始: {original_name}, hash={obj_hash[:16]}...)")
            temp_collection = CollectionUtils.create_new_collection("TEMP_SUBMESH_COLLECTION_" + self.unique_str)
            bpy.context.scene.collection.children.link(temp_collection)
            temp_collection_list.append(temp_collection)

            # 复制源对象为临时对象，不影响原始对象
            temp_obj = ObjUtils.copy_object(
                context=bpy.context,
                obj=source_obj,
                name=source_obj.name + "_temp",
                collection=temp_collection
            )

            # 归一化顶点组权重（Blend 类别需要）
            self._normalize_temp_obj_for_export(temp_obj)
            # 因为导入时根据 LogicName 进行了翻转，所以导出时对临时对象进行翻转才能得到游戏原本坐标系
            self._apply_export_rotation_for_logic(temp_obj)
            # 三角化对象，确保每个面都是三角形（后续 index_count = polygons * 3）
            ObjUtils.triangulate_object(bpy.context, temp_obj)

            # 计算顶点数和索引数
            draw_call_model.vertex_count = len(temp_obj.data.vertices)
            # 因为三角化了，所以每个面都是3个索引，所以 *3 就没问题
            draw_call_model.index_count = len(temp_obj.data.polygons) * 3
            draw_call_model.index_offset = index_offset

            # 存入缓存，后续来自同一原始物体且数据相同的对象可直接复用
            data_hash_cache[cache_key] = (index_offset, draw_call_model.vertex_count, draw_call_model.index_count)

            index_offset += draw_call_model.index_count
            # 这里赋值的意义在于，后续可能会合并到 DrawIB 级别，这里就可以直接复用了
            self.vertex_count += draw_call_model.vertex_count
            self.index_count += draw_call_model.index_count

            # 临时对象放到列表里，后续进行合并
            submesh_temp_obj_list.append(temp_obj)

        # 调试输出：复用统计
        total = len(self.drawcall_model_list)
        if reuse_count > 0:
            print(f"[SubMeshModel] 数据复用统计: {reuse_count}/{total} 个对象复用, {total - reuse_count}/{total} 个对象独立处理")
        else:
            print(f"[SubMeshModel] 数据复用统计: 无复用, 全部 {total} 个对象独立处理")

        # 接下来合并对象，合并的意义在于可以减少 IB 和 VB 的计算次数，在大批量导出时节省很多时间
        # 确保选中第一个，否则 join_objects 会报错
        if submesh_temp_obj_list:
            # 取消选中所有物体
            bpy.ops.object.select_all(action='DESELECT')
            # 选中第一个物体并设置为活动物体
            target_active = submesh_temp_obj_list[0]
            target_active.select_set(True)
            bpy.context.view_layer.objects.active = target_active

        # 执行物体合并
        ObjUtils.join_objects(bpy.context, submesh_temp_obj_list)

        # 因为合并到第一个对象上了，所以这里直接拿到这个对象
        submesh_merged_obj = submesh_temp_obj_list[0]
        # 重命名为指定名称，等待后续操作
        merged_obj_name = "TEMP_SUBMESH_MERGED_" + self.unique_str
        ObjUtils.rename_object(submesh_merged_obj, merged_obj_name)

        # 检查并校验是否有缺少的元素
        ObjBufferHelper.check_and_verify_attributes(obj=submesh_merged_obj, d3d11_game_type=self.d3d11_game_type)

        # 构建 Unity 导出所需的 buffer 结果
        obj_buffer_result = ExportUtils.build_unity_obj_buffer_result(
            obj=submesh_merged_obj,
            d3d11_game_type=self.d3d11_game_type,
        )
        self.ib = obj_buffer_result.ib
        self.category_buffer_dict = obj_buffer_result.category_buffer_dict
        self.index_vertex_id_dict = obj_buffer_result.index_loop_id_dict
        self.shape_key_buffer_dict = obj_buffer_result.shape_key_buffer_dict

        # 计算完成后，删除临时对象
        bpy.data.objects.remove(submesh_merged_obj, do_unlink=True)

        # 顺便把刚才创建的临时集合也删掉
        for temp_collection in temp_collection_list:
            if temp_collection.name in bpy.data.collections:
                if temp_collection.name in bpy.context.scene.collection.children:
                    bpy.context.scene.collection.children.unlink(temp_collection)
                bpy.data.collections.remove(temp_collection)

        print("SubMeshModel: " + self.unique_str + " 计算完成，临时对象已删除")

    def _precompute_object_hashes(self) -> list:
        """预计算所有源对象的数据哈希，使用多线程并行计算
        
        流程：
        1. 主线程中从 Blender 对象提取原始数据（Blender API 非线程安全）
        2. 多线程并行计算哈希值（纯 Python 计算，线程安全）
        """
        # 第一步：在主线程中提取所有对象的原始数据
        raw_data_list = []
        for draw_call_model in self.drawcall_model_list:
            blender_obj_name = draw_call_model.get_blender_obj_name()
            source_obj = ObjUtils.get_obj_by_name(blender_obj_name)

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

        hashes = [compute_hash(raw_data) for raw_data in raw_data_list]

        if len(raw_data_list) > 4:
            try:
                with ThreadPoolExecutor() as executor:
                    hashes = list(executor.map(compute_hash, raw_data_list))
            except Exception:
                pass

        return hashes

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

        # 3. 顶点组权重数据（存储在 Mesh 层面，但通过 Object 的顶点组索引引用）
        # 采样策略：每 step 个顶点取一个，平衡精度和性能
        if vert_count > 0 and vg_count > 0:
            step = max(1, vert_count // 500)
            for vi in range(0, vert_count, step):
                vert = mesh.vertices[vi]
                if vert.groups:
                    raw_data.append(vi)
                    for group in vert.groups:
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

        ObjUtils.normalize_all(temp_obj)

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