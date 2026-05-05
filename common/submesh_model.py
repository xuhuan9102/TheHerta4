from dataclasses import dataclass, field
import numpy

from .draw_call_model import DrawCallModel

from ..utils.export_utils import ExportUtils
from ..utils.obj_utils import ObjUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.timer_utils import TimerUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from .logic_name import LogicName
from .global_config import GlobalConfig
from .d3d11_gametype import D3D11GameType
from .submesh_metadata import SubmeshMetadataResolver
from ..blueprint.export_helper import BlueprintExportHelper


import bpy
import math
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
    unique_first_loop_indices:numpy.ndarray = field(init=False,repr=False,default=None)
    object_export_context_map:dict = field(init=False,repr=False,default_factory=dict)

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
            normalized_source_name = self._normalize_source_name(source_name)
            if normalized_source_name:
                original_names.append(normalized_source_name)
                continue

            fallback_name = draw_call_model.get_blender_obj_name()
            original_names.append(self._normalize_source_name(fallback_name) or fallback_name)

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
        cache_key_to_geometry_record = {}
        cache_key_to_candidate_names = {}

        reuse_count = 0
        direct_source_reuse_count = 0
        duplicated_temp_count = 0
        merged_obj_uses_preprocessed_copy = False
        copy_duration = 0.0
        join_duration = 0.0
        normalize_duration = 0.0
        rotate_duration = 0.0
        loop_offset = 0
        preserve_distinct_export_contexts = self._should_preserve_distinct_export_contexts()

        if preserve_distinct_export_contexts:
            print(f"[SubMeshModel] 直出 ShapeKey 场景: 保留独立导出上下文，禁用几何复用 {self.unique_str}")

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
            cached_result = None if preserve_distinct_export_contexts else data_hash_cache.get(cache_key)

            if cached_result is not None:
                cached_offset, cached_vertex_count, cached_index_count = cached_result
                draw_call_model.vertex_count = cached_vertex_count
                draw_call_model.index_count = cached_index_count
                draw_call_model.index_offset = cached_offset
                cache_key_to_candidate_names.setdefault(cache_key, set()).update(
                    self._build_drawcall_candidate_names(draw_call_model)
                )
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
            loop_count = len(temp_obj.data.loops)

            data_hash_cache[cache_key] = (index_offset, draw_call_model.vertex_count, draw_call_model.index_count)
            cache_key_to_candidate_names.setdefault(cache_key, set()).update(
                self._build_drawcall_candidate_names(draw_call_model)
            )
            cache_key_to_geometry_record[cache_key] = {
                "loop_start": loop_offset,
                "loop_count": loop_count,
                "label": self.unique_str,
                "d3d11_game_type": self.d3d11_game_type,
                "preferred_source_name": self._resolve_preferred_source_name(source_obj, draw_call_model),
            }

            index_offset += draw_call_model.index_count
            self.vertex_count += draw_call_model.vertex_count
            self.index_count += draw_call_model.index_count
            loop_offset += loop_count

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
                if BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
                    self._ensure_target_shape_key_union(valid_temp_obj_list[0], valid_temp_obj_list[1:])
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

        obj_buffer_result = ExportUtils.build_unity_obj_buffer_result(
            obj=submesh_merged_obj,
            d3d11_game_type=self.d3d11_game_type,
        )
        self.ib = obj_buffer_result.ib
        self.category_buffer_dict = obj_buffer_result.category_buffer_dict
        self.index_vertex_id_dict = obj_buffer_result.index_loop_id_dict
        self.unique_first_loop_indices = obj_buffer_result.unique_first_loop_indices
        self.shape_key_buffer_dict = obj_buffer_result.shape_key_buffer_dict
        self.object_export_context_map = self._build_object_export_context_map(
            cache_key_to_geometry_record=cache_key_to_geometry_record,
            cache_key_to_candidate_names=cache_key_to_candidate_names,
        )

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

    def _ensure_target_shape_key_union(self, target_obj: bpy.types.Object, source_objs: list[bpy.types.Object]):
        objects = [
            obj for obj in [target_obj] + list(source_objs or [])
            if obj is not None and getattr(obj, "data", None)
        ]
        if not objects:
            return

        ordered_shape_key_names = []
        seen_names = set()
        for obj in objects:
            key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
            if not key_blocks:
                continue
            for key_index, key_block in enumerate(key_blocks):
                if key_index == 0:
                    continue
                key_name = getattr(key_block, "name", "")
                if key_name and key_name not in seen_names:
                    seen_names.add(key_name)
                    ordered_shape_key_names.append(key_name)

        if not ordered_shape_key_names:
            return

        total_added_count = 0
        for obj in objects:
            target_shape_keys = getattr(obj.data, "shape_keys", None)
            target_key_blocks = getattr(target_shape_keys, "key_blocks", None)
            if not target_key_blocks:
                obj.shape_key_add(name="Basis", from_mix=False)
                target_key_blocks = obj.data.shape_keys.key_blocks

            target_names = {key_block.name for key_block in target_key_blocks}
            added_count = 0
            for key_name in ordered_shape_key_names:
                if key_name in target_names:
                    continue
                obj.shape_key_add(name=key_name, from_mix=False)
                target_names.add(key_name)
                added_count += 1

            total_added_count += added_count

        if total_added_count:
            print(f"[SubMeshModel] ShapeKey 合并前补齐: {self.unique_str} 新增 {total_added_count} 个空形态键槽")

    def _build_drawcall_candidate_names(self, draw_call_model: DrawCallModel) -> set[str]:
        candidate_names = {
            getattr(draw_call_model, "obj_name", "") or "",
            getattr(draw_call_model, "source_obj_name", "") or "",
            draw_call_model.get_blender_obj_name() or "",
        }

        normalized_names = set()
        for name in candidate_names:
            if not name:
                continue
            normalized_names.add(name)
            normalized_name = self._normalize_source_name(name)
            if normalized_name:
                normalized_names.add(normalized_name)
            if name.endswith("_copy"):
                normalized_names.add(name[:-5])

        return {name for name in normalized_names if name}

    def _normalize_source_name(self, name: str) -> str:
        if not name:
            return name

        result = name
        for pattern in (
            r"_chain\d+_dup\d+_copy$",
            r"_chain\d+_copy$",
            r"_dup\d+_copy$",
            r"_copy$",
            r"_chain\d+_dup\d+$",
            r"_chain\d+$",
            r"_dup\d+$",
        ):
            result = re.sub(pattern, "", result)
        return result

    def _resolve_preferred_source_name(self, source_obj: bpy.types.Object, draw_call_model: DrawCallModel) -> str:
        for candidate_name in (
            getattr(source_obj, "name", "") if source_obj is not None else "",
            getattr(draw_call_model, "source_obj_name", "") or "",
            draw_call_model.get_blender_obj_name() or "",
            getattr(draw_call_model, "obj_name", "") or "",
        ):
            normalized_name = self._normalize_source_name(candidate_name)
            if normalized_name:
                return normalized_name
        return ""

    def _build_object_export_context_map(self, cache_key_to_geometry_record: dict, cache_key_to_candidate_names: dict) -> dict:
        if self.unique_first_loop_indices is None:
            return {}

        unique_first_loop_indices = numpy.asarray(self.unique_first_loop_indices, dtype=numpy.int32)
        object_export_context_map = {}

        for cache_key, geometry_record in cache_key_to_geometry_record.items():
            loop_start = int(geometry_record.get("loop_start", 0))
            loop_count = int(geometry_record.get("loop_count", 0))
            if loop_count <= 0:
                continue

            loop_end = loop_start + loop_count
            export_mask = (unique_first_loop_indices >= loop_start) & (unique_first_loop_indices < loop_end)
            export_indices = numpy.flatnonzero(export_mask).astype(numpy.int32)
            if export_indices.size == 0:
                continue

            local_loop_indices = (unique_first_loop_indices[export_mask] - loop_start).astype(numpy.int32)
            context = {
                "cache_key": cache_key,
                "export_indices": export_indices,
                "local_loop_indices": local_loop_indices,
                "vertex_count": int(export_indices.size),
                "label": geometry_record.get("label", self.unique_str),
                "d3d11_game_type": geometry_record.get("d3d11_game_type", self.d3d11_game_type),
                "preferred_source_name": geometry_record.get("preferred_source_name", ""),
            }

            for candidate_name in cache_key_to_candidate_names.get(cache_key, set()):
                if candidate_name:
                    object_export_context_map[candidate_name] = context

        return object_export_context_map

    def _should_duplicate_source_for_merge(self, source_obj: bpy.types.Object) -> bool:
        if source_obj is None:
            return True

        if len(self.drawcall_model_list) != 1:
            return True

        if self.has_multi_file_export_nodes:
            return True

        if BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
            return True

        if not source_obj.name.endswith('_copy'):
            return True

        unique_str_count = self.source_obj_unique_str_count.get(source_obj.name, 0)
        return unique_str_count > 1

    def _should_preserve_distinct_export_contexts(self) -> bool:
        # Keep the base exporter on the legacy geometry-reuse path.
        # Direct shape key export now supplements missing per-object data later,
        # and forcing distinct export contexts here inflates the base buffers.
        return False

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
            ShapeKeyUtils.transform_apply_preserve_shape_keys(temp_obj, location=False, rotation=True, scale=True)
        elif GlobalConfig.logic_name == LogicName.EFMI:
            temp_obj.rotation_euler[0] = 0
            temp_obj.rotation_euler[1] = 0
            temp_obj.rotation_euler[2] = 0
            if all(abs(axis - 1.0) <= 1e-7 for axis in temp_obj.scale):
                return
            ObjUtils.select_obj(temp_obj)
            ShapeKeyUtils.transform_apply_preserve_shape_keys(temp_obj, location=False, rotation=True, scale=True)
