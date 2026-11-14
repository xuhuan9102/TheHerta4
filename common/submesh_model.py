from dataclasses import dataclass, field
import numpy

from .draw_call_model import DrawCallModel
from .geometry_hash import extract_object_hash_data, prepare_object_hashes

from ..utils.export_utils import ExportUtils
from ..utils.obj_utils import ObjUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.timer_utils import TimerUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from .logic_name import LogicName
from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .d3d11_gametype import D3D11GameType
from .submesh_metadata import SubmeshMetadataResolver
from ..blueprint.export_helper import BlueprintExportHelper


import bpy
import math
import json
import os
import re
from time import perf_counter


def _strip_duplicate_suffix_name(text: str) -> str:
    """剥掉 Blender 给重名顶点组追加的 ``.001`` 后缀。

    复制物体时顶点组名一起被去重（``12`` → ``12.001``）；它仍然是数字骨骼组，
    判「非数字组」之前必须先还原，否则这条骨会被静默删掉。
    """
    value = str(text or "").strip()
    head, separator, tail = value.rpartition(".")
    if separator and head and len(tail) == 3 and tail.isdigit():
        return head
    return value


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
    match_cs:str = field(init=False, default="")
    match_uav_bytes:int = field(init=False, default=0)
    unique_str:str = field(init=False, default="")
    workspace_unique_str:str = field(init=False, default="")

    # 调用组合obj并计算ib和vb得到这些属性
    vertex_count:int = field(init=False, default=0)
    index_count:int = field(init=False, default=0)

    # EFMI 骨骼合并升宽标记：BLENDINDICES 已从 8 位升到 16 位（导出 INI 需输出 ElementFormat 行）
    blendindices_widened:bool = field(init=False, default=False)
    # EFMI 骨骼合并元数据（由 SubmeshMetadata 透传）
    vg_offset:int = field(init=False, default=0)
    vg_count:int = field(init=False, default=0)
    vg_map_algorithm_version:int = field(init=False, default=0)
    merged_skeleton_metadata_valid:bool = field(init=False, default=True)
    # EFMI 跨 LOD 对应账本（v9 投影写回；非基准 LOD 子网格记录与基准部件的关系）
    efmi_lod_reference_component:str = field(init=False, default="")
    efmi_lod_correspondence:dict = field(init=False, default_factory=dict)
    efmi_lod_projection:bool = field(init=False, default=False)
    efmi_lod_layout_version:int = field(init=False, default=0)
    # ZZMI 骨架分组号（渲染 cb1 对象变换配对；缺省 0 = 单骨架旧语义）
    skeleton_group:int = field(init=False, default=0)
    # ZZMI 逐部件 VGMap（局部骨骼 id -> 全局槽位；attach CS 按此写合并骨架）
    vg_map:dict = field(init=False, default_factory=dict)
    # ZZMI 导出侧守卫元数据（反查写回；缺省 0）：
    # deform pass draw 序号（合并网格时序校验）+ 原部件顶点数（渲染 vb1 换绑判定）
    deform_draw_index:int = field(init=False, default=0)
    original_vertex_count:int = field(init=False, default=0)

    # 读取工作空间中的 Import.json 选择数据类型目录，再从对应的 SubmeshJson 获取 d3d11GameType
    d3d11_game_type:D3D11GameType = field(init=False,repr=False,default=None)

    ib:list = field(init=False,repr=False,default_factory=list)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict) 
    shape_key_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    unique_first_loop_indices:numpy.ndarray = field(init=False,repr=False,default=None)
    object_export_context_map:dict = field(init=False,repr=False,default_factory=dict)

    # 接缝焊接：每个 DrawCall 在 join 后 mesh 中的 loop 区间 / 索引区间（按它把顶点归回物体）
    drawcall_loop_ranges:list = field(init=False, default_factory=list)
    # 接缝焊接配置（导出器从「接缝焊接」后处理节点读取后注入；None = 不焊接）
    # 必须是 init 字段：导出器在构造时按关键字注入（ui/universal/export_helper.py:61），
    # 写成 init=False 会让 dataclass 生成的 __init__ 不认这个关键字，所有导出直接 TypeError。
    seam_weld_config:dict = field(default=None, repr=False)
    # 焊接：焊后 mesh 顶点 -> 原物体 id / 导出顶点池行归属
    weld_vertex_owner_map:numpy.ndarray = field(init=False, default=None, repr=False)
    weld_pool_owner_rows:dict = field(init=False, default_factory=dict, repr=False)
    weld_bucket_by_cache_key:dict = field(init=False, default_factory=dict, repr=False)
    seam_weld_index_recount_applied:bool = field(init=False, default=False)

    def __post_init__(self):

        # 因为列表里的每个DrawCallModel的draw_ib,first_index,index_count都是一样的，所以直接取第一个就行了
        if len(self.drawcall_model_list) > 0:
            self.match_draw_ib = self.drawcall_model_list[0].match_draw_ib
            # 这三个索引字段一律按**字符串**继承，不在这里转 int：它们的出处是对象名
            # `<draw_ib>-<index_count>-<first_index>`，名字末段可能带 Blender 去重后缀
            # （复制物体/重跑导出产生 `.001`~`.999`）。需要整数时用
            # `common/object_prefix_helper.coerce_index(值, field=…, context=…)`——
            # 那是全仓唯一允许的转换入口，别在这个字段上再写一次 int()。
            self.match_first_index = self.drawcall_model_list[0].match_first_index
            self.match_index_count = self.drawcall_model_list[0].match_index_count
            self.workspace_unique_str = self.drawcall_model_list[0].get_workspace_unique_str()
            self.unique_str = self.workspace_unique_str or self.drawcall_model_list[0].get_unique_str()

        self.calc_buffer()
    

    def calc_buffer(self):
        folder_name = self.workspace_unique_str or self.unique_str

        submesh_metadata = SubmeshMetadataResolver.resolve(folder_name)
        self.d3d11_game_type = submesh_metadata.d3d11_game_type
        self.match_cs = getattr(submesh_metadata, "match_cs", "") or ""
        self.match_uav_bytes = int(getattr(submesh_metadata, "match_uav_bytes", 0) or 0)
        # EFMI 骨骼合并元数据（反查写回；无则 0/空，合并段输出时据此校验）
        self.vg_offset = int(getattr(submesh_metadata, "vg_offset", 0) or 0)
        self.vg_count = int(getattr(submesh_metadata, "vg_count", 0) or 0)
        self.vg_map_algorithm_version = int(
            getattr(submesh_metadata, "vg_map_algorithm_version", 0) or 0
        )
        # t75 通道计划（唯一判定口径）：导入期算好并缓存，导出侧直接消费。
        self.channel_plan_version = int(
            getattr(submesh_metadata, "channel_plan_version", 0) or 0
        )
        self.channel_plan = dict(getattr(submesh_metadata, "channel_plan", {}) or {})
        self.channel_plan_digest = str(
            getattr(submesh_metadata, "channel_plan_digest", "") or ""
        )
        self.channel_plan_slot_weights = dict(
            getattr(submesh_metadata, "channel_plan_slot_weights", {}) or {}
        )
        self.merged_skeleton_metadata_valid = bool(
            getattr(submesh_metadata, "merged_skeleton_metadata_valid", True)
        )
        # EFMI 跨 LOD 对应账本（v9 投影写回；导出侧据此把 LOD 版本合并进同一逻辑部件）
        self.efmi_lod_reference_component = str(
            getattr(submesh_metadata, "efmi_lod_reference_component", "") or ""
        )
        self.efmi_lod_correspondence = dict(
            getattr(submesh_metadata, "efmi_lod_correspondence", {}) or {}
        )
        self.efmi_lod_projection = bool(
            getattr(submesh_metadata, "efmi_lod_projection", False)
        )
        self.efmi_lod_layout_version = int(
            getattr(submesh_metadata, "efmi_lod_layout_version", 0) or 0
        )
        # ZZMI 骨架分组号（渲染 cb1 对象变换配对；缺省 0 = 单骨架旧语义）
        self.skeleton_group = int(getattr(submesh_metadata, "skeleton_group", 0) or 0)
        # ZZMI 逐部件 VGMap（局部骨骼 id -> 全局槽位；attach CS 按此写合并骨架）
        self.vg_map = dict(getattr(submesh_metadata, "vg_map", {}) or {})
        # ZZMI 导出侧守卫元数据（deform draw 序号 / 原部件顶点数）
        self.deform_draw_index = int(getattr(submesh_metadata, "deform_draw_index", 0) or 0)
        self.original_vertex_count = int(getattr(submesh_metadata, "original_vertex_count", 0) or 0)

        # EFMI 骨骼合并：只有 GPU-PreSkinning 子网格才有统一骨架和
        # BLENDINDICES。CPU-PreSkinning 子网格在 ensure_skeleton_data 阶段
        # 会被明确跳过，必须继续走普通局部组导出，不能因为同一批次开启
        # 复选框就强制要求它存在 BLENDINDICES。
        self.blendindices_widened = False
        if (
            self.d3d11_game_type is not None
            and GlobalConfig.logic_name == LogicName.EFMI
            and GlobalProterties.import_merged_vgmap()
            and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
        ):
            self.blendindices_widened = self.d3d11_game_type.widen_blendindices()
            blend_layouts = self.d3d11_game_type.get_blendindices_layouts()
            if not blend_layouts:
                raise RuntimeError(
                    f"[EFMI骨骼合并] {self.unique_str} 的 GameType 不含 BLENDINDICES"
                )
            narrow_layouts = [
                (semantic_index, fmt)
                for semantic_index, fmt, _extract_slot in blend_layouts
                if str(fmt).upper().startswith("R8")
            ]
            if narrow_layouts:
                raise RuntimeError(
                    f"[EFMI骨骼合并] {self.unique_str} 的 BLENDINDICES 升宽未完成: "
                    f"{narrow_layouts}"
                )

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
        need_rotate = ExportUtils.requires_export_space_transform(GlobalConfig.logic_name)

        for i, draw_call_model in enumerate(self.drawcall_model_list):
            blender_obj_name = draw_call_model.get_blender_obj_name()
            source_obj = source_obj_list[i]

            obj_hash = object_hashes[i]
            if obj_hash is None:
                obj_hash = f"FALLBACK_{blender_obj_name}"
            original_name = original_names[i]
            cache_key = (obj_hash, original_name)
            cached_result = None if preserve_distinct_export_contexts else data_hash_cache.get(cache_key)

            # 【t81-F3 纵深防御】命中后**校验指纹**：几何不同（顶点数/索引数不同）⇒ 视为**未命中**。
            # 即使将来键仍然不足（例如又出现别的"同名同哈希不同几何"），也不会静默把别人的几何写进来。
            # 指纹取 `source_obj` 的真实尺寸（与 `:289-290` 写入缓存时的口径一致）。
            fingerprint_ok = True
            fingerprint_note = ""
            if cached_result is not None and source_obj is not None:
                try:
                    own_vertex_count = len(source_obj.data.vertices)
                    own_index_count = len(source_obj.data.polygons) * 3
                except Exception:  # noqa: BLE001 - 拿不到尺寸就不校验（保守起见按命中处理，但会打日志）
                    own_vertex_count = own_index_count = None
                if own_vertex_count is not None:
                    cached_offset_probe, cached_v_probe, cached_i_probe = cached_result
                    if own_vertex_count != cached_v_probe or own_index_count != cached_i_probe:
                        fingerprint_ok = False
                        fingerprint_note = (
                            f"指纹不符（自身 {own_vertex_count} 顶点/{own_index_count} 索引，"
                            f"缓存 {cached_v_probe}/{cached_i_probe}）⇒ 视为未命中"
                        )

            # 【t81 导出期日志】零假设钉死：命中/未命中 + 键 + 指纹 + 缓存账目（只打日志，不改行为）。
            # **只在 HIMI 打开**：这是 HIMI 合并导出的排查日志，其它逻辑保持改动前：不打。
            if GlobalConfig.logic_name == getattr(LogicName, "HIMI", "HIMI"):
                print(
                    f"[SubMeshModel][t81] draw_call={i} blender_obj='{blender_obj_name}' "
                    f"original='{original_name}' hash={str(obj_hash)[:16]} key={cache_key!r} "
                    f"preserve_distinct={preserve_distinct_export_contexts} "
                    f"hit={'yes' if cached_result is not None else 'no'} "
                    f"fingerprint_ok={fingerprint_ok} "
                    f"cached={cached_result if cached_result is not None else None} "
                    f"{fingerprint_note}"
                )

            if cached_result is not None and fingerprint_ok:
                cached_offset, cached_vertex_count, cached_index_count = cached_result
                draw_call_model.vertex_count = cached_vertex_count
                draw_call_model.index_count = cached_index_count
                draw_call_model.index_offset = cached_offset
                cache_key_to_candidate_names.setdefault(cache_key, set()).update(
                    self._build_drawcall_candidate_names(draw_call_model)
                )
                # 接缝焊接：复用几何的 DrawCall 指向首次记录的同一段 loop 区间
                cached_geometry_record = cache_key_to_geometry_record.get(cache_key) or {}
                self.drawcall_loop_ranges.append(
                    {
                        "draw_call": draw_call_model,
                        "loop_start": int(cached_geometry_record.get("loop_start", 0) or 0),
                        "loop_count": int(cached_geometry_record.get("loop_count", 0) or 0),
                        "index_offset": int(cached_offset),
                        "cache_key": cache_key,
                        "reused": True,
                    }
                )
                reuse_count += 1
                print(f"  ♻️复用: '{blender_obj_name}' (原始: {original_name}) → offset={cached_offset}, vertices={cached_vertex_count}, indices={cached_index_count}")
                continue
            if cached_result is not None and not fingerprint_ok:
                print(f"  ⚠️拒绝复用（t81-F3 指纹不符）: '{blender_obj_name}' (原始: {original_name}) "
                      f"→ 改用自己写几何；{fingerprint_note}")

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
            self.drawcall_loop_ranges.append(
                {
                    "draw_call": draw_call_model,
                    "loop_start": int(loop_offset),
                    "loop_count": int(loop_count),
                    "index_offset": int(draw_call_model.index_offset),
                    "cache_key": cache_key,
                    "reused": False,
                }
            )

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

        # ------------------------------------------------------------------ #
        # 【t83-A3】位置流插桩（**只打日志、不改行为**）—— **只在 HIMI 打开**：
        # 零假设钉死"行数多出来/位置写错"，是 HIMI 合并导出排查用的一次性诊断；
        # 留在通用路径上会让每个游戏的每次导出都刷屏。其它逻辑保持改动前：不打。
        # ------------------------------------------------------------------ #
        # 三态判据（配合 t82 的"产物 1321 行 = 源 1298 + 23"）：
        #   ① `draw_call_rows_sum != merged_verts` ⇒ **组装期就多/少行了**（位置流的行映射有问题）；
        #   ② 两者相等但产物仍错 ⇒ 错在**落盘之后**（插桩往后挪一层：`drawib_export_base` 写盘处）；
        #   ③ 每行的 `src_v` 与 IB 多数决不一致 ⇒ 写入点就是位置流（再看 `src_v` 是谁提供的）。
        if GlobalConfig.logic_name == getattr(LogicName, "HIMI", "HIMI"):
            try:
                _dc_rows = sum(int(getattr(d, "vertex_count", 0) or 0) for d in self.drawcall_model_list)
                _dc_index = sum(int(getattr(d, "index_count", 0) or 0) for d in self.drawcall_model_list)
                _merged_verts = None
                if submesh_temp_obj_list:
                    try:
                        _merged_verts = len(submesh_temp_obj_list[0].data.vertices)
                    except Exception:  # noqa: BLE001
                        _merged_verts = None
                print(
                    f"[t82-POS] part={self.unique_str} draw_calls={len(self.drawcall_model_list)} "
                    f"draw_call_rows_sum={_dc_rows} draw_call_index_sum={_dc_index} "
                    f"merged_verts={_merged_verts} "
                    f"rows_delta={None if _merged_verts is None else _dc_rows - _merged_verts} "
                    f"reuse={reuse_count} direct_reuse={direct_source_reuse_count} "
                    f"duplicated={duplicated_temp_count} "
                    f"offsets={[int(getattr(d, 'index_offset', -1) or -1) for d in self.drawcall_model_list]}"
                )
                if submesh_temp_obj_list:
                    _obj = submesh_temp_obj_list[0]
                    print(
                        f"[t82-POS]   merged_obj={getattr(_obj, 'name', '?')} "
                        f"verts={len(_obj.data.vertices)} polys={len(_obj.data.polygons)} "
                        f"loops={len(_obj.data.loops)} "
                        f"vgroups={len(getattr(_obj, 'vertex_groups', []) or [])}"
                    )
            except Exception as _exc:  # noqa: BLE001 - 插桩绝不许弄坏导出
                print(f"[t82-POS] 插桩异常（已忽略）：{_exc}")

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

        # 接缝焊接（「接缝焊接」节点有分组时才执行）：紧接在 join 之后、任何
        # normalize / 骨骼预处理 / 缓冲构建之前完成，等价于用户手动流程
        # 「导出前先把物体合并焊好，再点导出」——后续数据都基于焊好的 mesh。
        self._weld_group_seams_if_enabled(submesh_merged_obj, cache_key_to_geometry_record)

        if need_normalize:
            normalize_start = perf_counter()
            self._normalize_temp_obj_for_export(submesh_merged_obj)
            normalize_duration += perf_counter() - normalize_start

        if need_rotate:
            rotate_start = perf_counter()
            self._apply_export_rotation_for_logic(submesh_merged_obj)
            rotate_duration += perf_counter() - rotate_start

        TimerUtils.end_stage("对象处理与合并")

        # 骨骼合并导出前顶点组预处理（对齐参考插件 ObjectMerger 纪律）：
        # 使导出 blend 索引 = 全局骨骼 id（排序后 index == 数字组名）。
        # EFMI 无条件（复选框开即启用）；ZZMI 额外要求反查已写回（vg_count > 0），
        # 复选框关闭或无反查数据时完全保持旧逻辑。
        run_merged_skeleton_preprocess = False
        if GlobalProterties.import_merged_vgmap():
            if (
                GlobalConfig.logic_name == LogicName.EFMI
                and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
            ):
                run_merged_skeleton_preprocess = True
            elif GlobalConfig.logic_name == LogicName.ZZMI and self.vg_count > 0:
                run_merged_skeleton_preprocess = True

        # 双套顶点组导出转换（t9 方案 A：合并与非合并路径统一身份化）：
        # EFMI 去重对象（json 带 VGMap 合并元数据）在任一 EFMI 导出路径（合并骨架
        # 模式或非合并路径）下，先把合并槽更名回「源组中权重强度最强者」的非去重
        # 身份（Z 组更名），使导出 BLENDINDICES 落在原初身份命名空间——运行时全链
        # 本以身份（VGOffset+local）为键（t9 §2.1），VB 直写身份消除「引用 canonical
        # 槽」的跨组件间接层（F1 根治 + F4 族减轻）。
        # 更名只改名不排序；补缺/排序/断言 index==name 由下方各分支统一执行一次。
        dualset_eligible = (
            GlobalConfig.logic_name == LogicName.EFMI
            and self.vg_map_algorithm_version > 0
            and self.merged_skeleton_metadata_valid
            and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
        )
        if run_merged_skeleton_preprocess:
            if GlobalConfig.logic_name == LogicName.ZZMI:
                self._prepare_zzmi_merged_skeleton_vertex_groups(submesh_merged_obj)
            else:
                # EFMI 合并骨架模式：更名回非去重身份后再走既有预处理链。
                if dualset_eligible:
                    self._apply_dualset_export_rename(submesh_merged_obj)
                # 打包级幂等升宽：SubMeshModel 构造时可能因导入/导出状态差异未
                # 执行 widen，但**打包 dtype 必须与 INI 声明的 R16 全池布局一致**
                # ——否则顶点组读回的是全局骨骼 id（>255，如 596/372），被 uint8
                # 打包静默截断（596->84），运行时按 16 位读取 = 索引错位 = 权重爆炸。
                # widen_blendindices 幂等：已 R16 时直接返回 False，不重复修改。
                if (
                    GlobalProterties.import_merged_vgmap()
                    and bool(getattr(self.d3d11_game_type, "GPU_PreSkinning", False))
                ):
                    self.d3d11_game_type.widen_blendindices()
                # EFMI 合并骨架的 BLENDINDICES 已统一归一化为 R16 系
                # （widen_blendindices），全局骨骼池必须落在 uint16 可承载范围，
                # 否则导出时 astype(uint16) 会把越界骨骼 id 截断错位。
                # 多 LOD 分段平移（2026-08-27）后，LOD1 部件的组名 = 全局骨骼 id
                # （基准段之后起算），可能远超其自身顶点组数量——上限必须按
                # "最大组名（全局骨骼 id）"判定，不能按组数量判定（本地组数
                # 自洽但全局 id 已越界时，旧检查会漏放并静默截断）。
                numeric_group_ids = [
                    int(vg.name)
                    for vg in submesh_merged_obj.vertex_groups
                    if re.fullmatch(r"[0-9]+", str(vg.name))
                ]
                max_global_bone_id = max(numeric_group_ids, default=-1)
                if max_global_bone_id > 65535:
                    raise RuntimeError(
                        f"[EFMI骨骼合并] {self.unique_str} 全局骨骼 id "
                        f"{max_global_bone_id} 超过 uint16 上限 "
                        "65535，BLENDINDICES 无法以 R16 承载，请拆分骨架或减小骨骼池"
                    )
                self._prepare_merged_skeleton_vertex_groups(submesh_merged_obj)
        elif dualset_eligible:
            # EFMI 去重对象走非合并/其它逻辑导出（原 D2-B 路径保持）：更名回
            # 非去重身份 + 复用预处理链一次成型。更名失败即中止（fail-closed）。
            self._apply_dualset_export_rename(submesh_merged_obj)
            # S2（t20）/ B1 真落地（t22）：非合并路径补 widen——更名后身份
            # ∈ [0, Σvg_count) 可能 >255，而本工作区 Blend 原生 R8G8B8A8_UINT；
            # 不升宽则 _parse_blendindices 的 R8 范围检查抛 Fatal
            # （obj_buffer_helper.py:430-465）→ 导出中断。
            # t22 移除 import_merged_vgmap()/GPU_PreSkinning 门控：elif 可达条件
            # （dualset_eligible 含 GPU_PreSkinning）使二者恒真/恒假（t21 §1.1：
            # elif ⟺ run_merged_skeleton_preprocess=False ⟹ checkbox False——
            # 门控挂复选框 = 死代码，widen 永不触发）。widen 幂等（已 R16 返
            # False），无条件调用多调无害；合并分支行为不变。
            self.d3d11_game_type.widen_blendindices()
            self._prepare_merged_skeleton_vertex_groups(submesh_merged_obj)

        obj_buffer_result = ExportUtils.build_unity_obj_buffer_result(
            obj=submesh_merged_obj,
            d3d11_game_type=self.d3d11_game_type,
        )
        self.ib = obj_buffer_result.ib
        self.category_buffer_dict = obj_buffer_result.category_buffer_dict
        self.index_vertex_id_dict = obj_buffer_result.index_loop_id_dict
        self.unique_first_loop_indices = obj_buffer_result.unique_first_loop_indices
        self.shape_key_buffer_dict = obj_buffer_result.shape_key_buffer_dict
        # 接缝焊接：用「焊后每个顶点上的原物体 id」算本物体在导出顶点池里的行集合
        if self.weld_vertex_owner_map is not None:
            try:
                self.weld_pool_owner_rows = self._build_weld_pool_owner_rows(
                    submesh_merged_obj
                )
            except Exception as exc:  # noqa: BLE001 - 不允许让导出失败
                self.weld_pool_owner_rows = {}
                self._warn_weld(f"焊后顶点池归属计算失败，形态键范围退回导出上下文: {exc}")
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

    def _apply_dualset_export_rename(self, obj):
        """双套顶点组导出转换（M1 挂载点，t3 规格 §2/§6.1）：合并槽更名回运行时身份。

        两边分离契约（t3 设计 §2/§3；身份映射权威实现对照任务书
        ``get_dualset_slot_identity_map`` 别名 = ``build_per_mesh_identity_map``）：
        BL 侧顶点组名 = 合并槽位号（去重池命名空间，**永不直接写盘**）；导出侧
        更名回运行时身份 = 本组件成员身份（``VGOffset + local`` 语义，per-mesh
        覆盖层，含未更名槽），随后写盘 BLENDINDICES 落在自己 attach 必写的
        自属声明段内。

        非合并导出路径（F1 主场景根治）：EFMI 去重对象（json 带 VGMap 合并元数据）
        在本次导出未走合并骨架预处理链时，把临时导出对象上的合并槽顶点组名
        （去重全局 id）更名回本组件成员身份（per-mesh 身份选择主判 = 顶点数
        vertex_count，2026-08-29 用户裁决「顶点组多的那一边」，见
        efmi_skeleton.select_dualset_export_identity），随后复用
        _prepare_merged_skeleton_vertex_groups 的「剔非数字 -> 补缺 -> 排序 ->
        断言 index==name」纪律一次成型。

        - 单源槽恒等（e(s)==s），无实际操作；
        - 合并槽且本组件成员 ≠ 原槽：更名（如槽 371 -> 身份 596）；
        - 身份唯一性定理（t3 §1.3）保证改名不会撞名：e(s)≠s 时 e(s) 必不在
          槽位集合内，也不与其它槽的导出身份重复；
        - 失败即中止导出（fail-closed，A1-A5 + FC-1：槽无本组件成员时
          build_per_mesh_identity_map 直接 RuntimeError，不静默回退槽位直写；
          写盘路径另有 FC-2 产物域断言兜底）；
        - 本方法只做「槽位名→身份名」更名；补缺/排序/断言 index==name 由调用方
          在各自分支统一执行一次（合并/非合并共用同一预处理纪律，防双重执行）。
        """
        if obj is None or obj.type != 'MESH':
            return
        if not obj.vertex_groups:
            return
        # 仅 EFMI 去重对象适用：有 VGMap 合并元数据（算法版本号 > 0）。
        if self.vg_map_algorithm_version <= 0 or not self.merged_skeleton_metadata_valid:
            return
        workspace_root = str(GlobalConfig.path_workspace_folder() or "").strip()
        if not workspace_root:
            return
        from .efmi_skeleton import EFMIBoneMapBuilder

        # S1（t20 容错对称化）：build_dualset_export_table 的表级 fail-closed
        # 断言（A1 VGMap 键集 / A3 identity 单射 / A4 强度可得 / B10 跨 LOD 段）
        # 抛 RuntimeError 时，本调用点保留 fail-closed 语义（不静默回退槽位直写，
        # 与 rekey 侧一致地捕获），但把错误信息包装为带「子网格 / 断言 / 恢复
        # 指引」的可操作文本——避免构造期裸 abort 留下空目录且无指引。
        try:
            component_unique_str = str(
                self.workspace_unique_str or self.unique_str or ""
            ).strip()
            # t25 方向 A（v3）：per-mesh 身份映射——导出身份 = 本组件成员身份，
            # 全量覆盖（含未更名槽）。根治 t24 型「跨组件 canonical 单写者时序
            # 塌陷」：网格引用落回自己 attach 必写的身份域。
            rename_map = EFMIBoneMapBuilder.build_per_mesh_identity_map(
                workspace_root, component_unique_str
            )
        except RuntimeError as exc:
            raise RuntimeError(
                f"[EFMI双套导出] {obj.name}（{self.unique_str}）导出转换表构建失败，"
                f"中止导出（fail-closed，不静默回退槽位直写）。\n"
                f"原因: {exc}\n"
                "恢复指引: 请在工作空间执行「骨骼合并反查/重新导入」确保 VGMap "
                "元数据完整后重试；若反复失败请检查是否存在外部进程反复清除工作区 "
                "json（Config/Tabs 或 TYPE_ 目录）"
            ) from exc
        if not rename_map:
            return

        # t31/佩丽卡场景A硬化（fail-closed，不放宽 FC-2；失败点前移 + 可操作指引）：
        # per-mesh 更名表键域 = 本组件当前 json VGMap 引用槽。带权重的数字组名
        # 必须 ⊆ 该域 ∪ 折叠基准段（与 FC-2 的「自属段 ∪ 折叠目标」同口径：
        # same-IB 折叠的 TEMP 合并拷贝会携带基准 LOD0 部件的槽位，属合法折叠
        # 目标）——旧导入/旧缓存场景对象的数字顶点组（如佩丽卡重建前的旧合并
        # 槽 371..647）不在该域内：静默跳过会让其原样直写、到 FC-2 才在产物级
        # 被拦（消息难定位「373..393 从哪来」）。这里在 M1 更名点前置拒绝：
        # 报「场景对象顶点组与工作区骨骼元数据不同期，请重新导入该角色」并
        # 列出具体槽位（不再静默直写）。
        self._assert_dualset_weighted_groups_in_identity_domain(
            obj,
            set(rename_map.keys()),
            workspace_root,
            str(self.unique_str or ""),
        )

        renamed_count = 0
        for vg in list(obj.vertex_groups):
            name = str(vg.name)
            if re.fullmatch(r"[0-9]+", name) is None:
                continue
            slot = int(name)
            target = rename_map.get(slot)
            if target is None or target == slot:
                continue
            existing = None
            try:
                existing = obj.vertex_groups.get(str(target))
            except AttributeError:
                # 测试桩/退化对象：vertex_groups 可能是普通列表
                existing = None
            if existing is None:
                for candidate in obj.vertex_groups:
                    if str(getattr(candidate, "name", "")) == str(target):
                        existing = candidate
                        break
            if existing is None or existing == vg:
                vg.name = str(target)
            else:
                # 目标名已被导入侧的全骨架占位组占用。直接改名会触发 Blender
                # 自动后缀（"54.001"），随后被 _prepare_merged_skeleton_vertex_groups
                # 当作非数字组删除 → 权重陪葬（33b5aa11 单骨组件权重归零 → 导出
                # 报 "Cannot find any valid BLENDINDICES"；多骨组件静默丢权重）。
                # 二段重命名：先把占位组挪到临时非数字名（空组，随后被非数字组
                # 清理移除，无害），再把带权重的源组改名为正式身份名——只动名字
                # 不动顶点分配，权重按 index 跟随组对象。
                if any(
                    g.weight > 0.0 and g.group == existing.index
                    for v in getattr(obj.data, "vertices", ()) or ()
                    for g in getattr(v, "groups", ()) or ()
                ):
                    raise RuntimeError(
                        f"[EFMI双套导出] {obj.name}: 槽 {slot} -> 身份 {target} "
                        "与现有带权重顶点组名冲突（重名两边都有本地权重），无法"
                        "安全重命名，中止导出（fail-closed，不静默覆盖权重）。"
                        "请重新导入该角色"
                    )
                existing.name = f"__TMP_DUALSET_{target}_{slot}"
                vg.name = str(target)
            renamed_count += 1
        if renamed_count == 0:
            return
        print(
            f"[EFMI双套导出] {obj.name}: {renamed_count} 个合并槽更名回非去重身份，"
            "(补缺/排序/断言由调用方预处理链统一执行)"
        )

    @staticmethod
    def _dualset_registered_slots_union(workspace_root: str) -> set[int]:
        """全工作区所有组件 json VGMap 引用槽并集（t16 C-ii，统一骨架/单池语义）。

        骨骼合并后全场共用一副骨架：任一组件引用另一组件的**已注册槽**时，
        被引用组件生成占位符保持该槽注册——跨组件引用是设计内合法状态。故
        放行域 = 当前 json 全组件（LOD0/LOD1…）VGMap 引用槽并集（动态读；
        未注册/旧代槽不在并集内 → 照旧 fail-closed）。json 元数据缺失/被清
        （t15 场景）时并集为空 → 调用方按「元数据缺失」分支报错（与
        「未注册槽」原因区分）。
        """
        slots: set[int] = set()
        ws = str(workspace_root or "")
        if not ws or not os.path.isdir(ws):
            return slots
        try:
            for lod_name in os.listdir(ws):
                lod_root = os.path.join(ws, lod_name)
                if not os.path.isdir(lod_root) or not lod_name.upper().startswith("LOD"):
                    continue
                for bare in os.listdir(lod_root):
                    folder = os.path.join(lod_root, bare)
                    if not os.path.isdir(folder) or bare.startswith("DedupedTextures"):
                        continue
                    for entry in os.listdir(folder):
                        tdir = os.path.join(folder, entry)
                        if not os.path.isdir(tdir) or not entry.startswith("TYPE_"):
                            continue
                        jp = os.path.join(tdir, bare + ".json")
                        if not os.path.isfile(jp):
                            continue
                        try:
                            import json as _json
                            with open(jp, encoding="utf-8") as f:
                                payload = _json.load(f)
                            vg_map = payload.get("VGMap")
                            if isinstance(vg_map, dict):
                                for raw_value in vg_map.values():
                                    # 注意：槽位 0 是合法注册槽，不能用
                                    # `raw_value or ""`（0 会被当假值吞掉）。
                                    text_value = str(raw_value).strip()
                                    if text_value.isdigit():
                                        slots.add(int(text_value))
                        except Exception:
                            continue
                        break
        except OSError:
            pass
        return slots

    @staticmethod
    def _assert_dualset_weighted_groups_in_identity_domain(
        obj, domain: set[int], workspace_root: str = "", component_unique_str: str = ""
    ) -> None:
        """域前置断言：带权重数字组名 ⊆ 本组件身份域 ∪ 全工作区已注册槽并集。

        语义依据（t16 C-ii，用户领域裁决）：骨骼合并后全场共用一副骨架，不同
        前缀物体的顶点组设计上可以互相引用——A 引用 B 的槽时，B 生成占位符
        保持该槽注册（占位符机制的存在理由）。故放行域 = 本组件 VGMap 引用槽
        ∪ **全工作区所有组件已注册槽并集**（动态读当前 json；不再限于 L0 段）。

        fail-closed 牙齿保留：**未注册/旧代槽照旧拦截**（不在并集内，如佩丽卡
        旧时代 373..393）；json 元数据缺失/被清（t15 场景，并集为空）时报
        「元数据缺失」明确消息，与「未注册槽」原因区分。与 FC-2 同口径：
        只查**带权重（weight>0）** 数字组名——无权重组不写盘（0 哨兵/空组
        惰性），skip_empty=False 全量空组工作流不受影响。

        对象无网格数据（单测桩/退化对象）时无法读取权重，退化为检查全部数字
        组名（保守）。
        """
        slot_by_index: dict[int, int] = {}
        for idx, vg in enumerate(obj.vertex_groups):
            name = str(vg.name)
            if re.fullmatch(r"[0-9]+", name) is None:
                continue
            slot_by_index[getattr(vg, "index", idx)] = int(name)

        weighted_slots: set[int] | None = None
        data = getattr(obj, "data", None)
        vertices = getattr(data, "vertices", None) if data is not None else None
        if vertices is not None:
            weighted_slots = set()
            for vertex in vertices:
                for group in getattr(vertex, "groups", ()) or ():
                    if float(getattr(group, "weight", 0.0) or 0.0) > 0.0:
                        slot = slot_by_index.get(getattr(group, "group", -1))
                        if slot is not None:
                            weighted_slots.add(slot)
        if weighted_slots is None:
            weighted_slots = set(slot_by_index.values())
        if not weighted_slots:
            return

        registered_union = (
            SubMeshModel._dualset_registered_slots_union(workspace_root)
            if workspace_root else set()
        )
        allowed = set(domain) | registered_union
        out_of_domain = sorted(weighted_slots - allowed)
        if not out_of_domain:
            return
        preview = out_of_domain[:20]
        suffix = "…" if len(out_of_domain) > 20 else ""
        if not registered_union:
            # 元数据缺失/被清（t15 场景）：全组件 json 均无已注册槽 → 与
            # 「未注册槽」原因区分，给出检查工作区 json / 重新导入指引。
            raise RuntimeError(
                f"[EFMI双套导出/域前置] {getattr(obj, 'name', '?')} 的数字顶点组 "
                f"{preview}{suffix}（共 {len(out_of_domain)} 个）无法判定槽位归属："
                "当前工作区没有任何组件 json 携带 VGMap 已注册槽（骨骼合并元数据"
                "缺失/被清除，或 json 被外部进程改写）。请检查工作区 json 或按当前"
                "工作区 json 重新导入该角色（含合并顶点组开关）后重试"
            )
        raise RuntimeError(
            f"[EFMI双套导出/域前置] {getattr(obj, 'name', '?')} 的数字顶点组 "
            f"{preview}{suffix}（共 {len(out_of_domain)} 个）不在当前工作区已注册"
            "槽集合（本组件 VGMap 引用槽 ∪ 全工作区所有组件已注册槽）内：未注册/"
            "旧代槽（旧导入/旧缓存/外部进程改写 json 遗留）。产物将引用运行时未注册"
            "槽位，中止导出（fail-closed，不静默直写）。请按当前工作区 json 重新导入"
            "该角色后重试"
        )

    @staticmethod
    def _fill_vertex_group_gaps(obj):
        """按数字名补缺顶点组（参考 EFMI-Tools fill_gaps_in_vertex_groups）。

        把 [0, 3, 4, 6] 这样的名字补齐为连续数字名（新增 1、2、5），
        不依赖 active_object，直接操作给定对象。
        """
        existing = set()
        for vg in obj.vertex_groups:
            name = str(vg.name)
            if name.isdigit():
                existing.add(int(name))
        if not existing:
            return
        for i in range(max(existing) + 1):
            if i not in existing:
                obj.vertex_groups.new(name=str(i))
        # 排序由后续紧凑化重命名前的 index 语义处理；此处仅补缺。

    def _prepare_merged_skeleton_vertex_groups(self, obj):
        """骨骼合并导出前顶点组预处理（对齐参考插件 ObjectMerger.finalize_temp_objects_data）。

        EFMI/ZZMI 合并骨架模式共用：组名 = 全局骨骼 id。非数字组不属于骨骼命名
        空间，在临时导出对象上先移除；数字组无条件补缺、排序并验证
        ``vertex_group.index == int(vertex_group.name)``。任一步失败都中止导出，
        禁止退回原样导出后静默重编号全局骨骼。
        """
        if obj is None or obj.type != 'MESH':
            return
        if not obj.vertex_groups:
            raise RuntimeError(f"[骨骼合并] {obj.name} 没有可导出的数字骨骼顶点组")

        non_numeric_groups = [
            vg for vg in obj.vertex_groups
            if re.fullmatch(r"[0-9]+", str(vg.name)) is None
        ]
        # 复制物体时 Blender 会把顶点组名也加去重后缀（``12`` → ``12.001``）。这种组
        # **是**数字骨骼组，先剥后缀再判，否则会被当成「非数字」静默删掉 ⇒ 少一根骨。
        recovered_groups = []
        for vg in list(non_numeric_groups):
            cleaned = _strip_duplicate_suffix_name(str(vg.name))
            if re.fullmatch(r"[0-9]+", cleaned) is None:
                continue
            try:
                vg.name = cleaned
            except Exception:  # noqa: BLE001 - 重名冲突时保留原样，走下面的移除路径
                continue
            if str(vg.name) != cleaned:
                continue
            non_numeric_groups.remove(vg)
            recovered_groups.append(cleaned)
        if recovered_groups:
            print(
                f"[骨骼合并] {obj.name}: {len(recovered_groups)} 个顶点组从 Blender "
                f"去重后缀还原为数字组 {recovered_groups}"
            )
        for vg in non_numeric_groups:
            obj.vertex_groups.remove(vg)
        if non_numeric_groups:
            print(
                f"[骨骼合并] {obj.name}: 已从临时导出对象移除 "
                f"{len(non_numeric_groups)} 个非数字顶点组"
            )
        if not obj.vertex_groups:
            raise RuntimeError(f"[骨骼合并] {obj.name} 移除非数字组后没有骨骼顶点组")

        numeric_ids = [int(vg.name) for vg in obj.vertex_groups]
        if len(numeric_ids) != len(set(numeric_ids)):
            raise RuntimeError(
                f"[骨骼合并] {obj.name} 存在等价的重复数字顶点组名: {numeric_ids}"
            )

        # 连续全局 id 是合并骨架正确性前提，不受普通导出补组开关控制。
        self._fill_vertex_group_gaps(obj)

        prev_active = bpy.context.view_layer.objects.active
        try:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.vertex_group_sort()
        finally:
            bpy.context.view_layer.objects.active = prev_active

        for vg in obj.vertex_groups:
            if int(vg.name) != int(vg.index):
                raise RuntimeError(
                    f"[骨骼合并] {obj.name} 顶点组排序不变量失败: "
                    f"name={vg.name}, index={vg.index}"
                )
            vg.name = str(vg.index)

    def _prepare_zzmi_merged_skeleton_vertex_groups(self, obj):
        """ZZMI 合并骨架导出前顶点组预处理（组名 = 全局骨骼 id，连续性是关键不变量）。

        与 EFMI 版的差异（修复"删空组→后续部件集体错位消失"）：
        1. **无条件补缺**到最大数字名（不受 export_add_missing_vertex_groups 门控——
           ZZMI 合并模式下组号连续是正确性前提，不是可选项）；
        2. **不剔除任何组**（ignore 组也不删：删任意中间组都会让后续组号 -1 移位，
           全局骨骼 id 全部错位；需要隐藏部件请用其它方式）；
        3. 按名排序 + 重命名 str(index)（补缺+排序后此为安全恒等操作）；
        4. 自检：非数字组名只警告；空组正常（无顶点引用的骨骼，无害）。
        """
        self._prepare_merged_skeleton_vertex_groups(obj)

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
            if name.endswith("_vgtest_unassigned_copy"):
                normalized_names.add(name[:-23])
            if name.endswith("_vgtest_copy"):
                normalized_names.add(name[:-12])
            if name.endswith("_copy"):
                normalized_names.add(name[:-5])

        return {name for name in normalized_names if name}

    def _normalize_source_name(self, name: str) -> str:
        if not name:
            return name

        result = name
        for pattern in (
            r"_chain\d+_dup\d+_vgtest_unassigned_copy$",
            r"_chain\d+_vgtest_unassigned_copy$",
            r"_dup\d+_vgtest_unassigned_copy$",
            r"_vgtest_unassigned_copy$",
            r"_chain\d+_dup\d+_vgtest_copy$",
            r"_chain\d+_vgtest_copy$",
            r"_dup\d+_vgtest_copy$",
            r"_vgtest_copy$",
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

            # 接缝焊接：本物体在导出顶点池里的行集合（供直出形态键取顶点范围）
            weld_rows = (self.weld_pool_owner_rows or {}).get(cache_key)
            if weld_rows is not None and len(weld_rows):
                context["weld_vertex_rows"] = numpy.asarray(weld_rows, dtype=numpy.int64)

            for candidate_name in cache_key_to_candidate_names.get(cache_key, set()):
                if candidate_name:
                    object_export_context_map.setdefault(candidate_name, context)

        return object_export_context_map

    def _weld_group_seams_if_enabled(self, merged_obj, cache_key_to_geometry_record=None):
        """按「接缝焊接」节点的分组，在 join 后的临时 mesh 上焊接**缝合边**顶点。

        用户 2026-10-22 最终拍板：焊接只认用户手动标记的缝合边（seam edge），
        位置合并交给 Blender 原生 ``bmesh.ops.remove_doubles``；没有缝合边标记就不焊
        （warning 提示去编辑模式标记缝合边）。

        只在 ``seam_weld_config`` 非空（导出器已确认开关开启且节点有分组）时执行；
        否则立即返回，mesh 与导出产物与改动前完全一致。

        焊接会让三角形退化成线 / 与其它面重复，``bmesh`` 会直接删掉这些面（用户
        2026-10-22 拍板：允许删，不为了保面数而跳过焊接）。因此焊接成功后必须按 mesh
        实际面数重算每个 draw 段的 ``index_count`` / ``index_offset``，并同步每个物体的
        焊后 loop 区间（``cache_key_to_geometry_record`` 供形态键帧表使用）。
        """
        config = self.seam_weld_config
        if not config or not (config.get("groups") or []):
            return None
        try:
            from .group_weld_mesh import build_mesh_vertex_ranges, weld_submesh_mesh
            from .group_weld import DEFAULT_WELD_THRESHOLD
        except Exception as exc:  # 导入失败不允许让导出失败
            self._warn_weld(f"焊接模块导入失败，跳过焊接: {exc}")
            return None

        mesh = getattr(merged_obj, "data", None)
        if mesh is None:
            self._warn_weld("合并物体没有 mesh 数据，跳过焊接")
            return None
        try:
            ranges = build_mesh_vertex_ranges(self.drawcall_loop_ranges, mesh)
        except Exception as exc:
            self._warn_weld(f"物体顶点区间解析失败，跳过焊接: {exc}")
            return None
        if len(ranges) < 2:
            self._warn_weld("本子网格不足 2 个导出物体，跳过焊接")
            return None

        try:
            threshold = float(config.get("threshold", DEFAULT_WELD_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DEFAULT_WELD_THRESHOLD

        # 只用于状态栏文本 / 计时，不进 build_step_plan 的清单（不改 done/total）。
        TimerUtils.start_stage("接缝顶点焊接")
        try:
            stats = weld_submesh_mesh(
                merged_obj,
                ranges,
                config.get("groups") or [],
                threshold=threshold,
            )
        except Exception as exc:  # 焊接任何异常都不允许让导出失败
            stats = {"applied": False, "reason": f"焊接异常: {exc}"}
        finally:
            TimerUtils.end_stage("接缝顶点焊接")

        if stats.get("applied"):
            # Blender 侧几何数同步为焊后顶点数
            self.vertex_count = int(stats.get("after_vertex_count", self.vertex_count))
            self._apply_weld_index_segments(
                stats, ranges, cache_key_to_geometry_record
            )
            try:
                from ..utils.log_utils import LOG

                LOG.info(
                    f"[接缝焊接] 子网格 {self.unique_str}: {stats.get('message', '')}"
                )
            except Exception:
                pass
        else:
            self._warn_weld(f"跳过焊接（{stats.get('reason', '未知原因')}）")
        return stats

    def _apply_weld_index_segments(self, stats, ranges, cache_key_to_geometry_record=None):
        """把焊后重算的索引区间写回 drawcall / 子网格 / 形态键 loop 区间。

        - 每个原物体仍占自己的 draw 段（段数不变）；某物体面被全部删光时段宽设为 0；
        - ``index_offset`` 按原物体顺序累计；
        - ``self.index_count`` = 各段之和 = 焊后 ``ib`` 长度；
        - 每个物体的焊后 loop 区间同步进 ``drawcall_loop_ranges`` 与
          ``cache_key_to_geometry_record``（形态键帧表 / 导出上下文按它切物体范围）。
        """
        segments = list(stats.get("index_segments") or [])
        if not segments:
            return False
        entry_by_bucket = {
            int(entry.get("bucket", -1)): entry for entry in (ranges or [])
        }
        draw_call_bucket: dict = {}
        total_index_count = 0
        for segment in segments:
            bucket = int(segment.get("bucket", -1))
            index_count = int(segment.get("index_count", 0) or 0)
            index_offset = int(segment.get("index_offset", 0) or 0)
            total_index_count += index_count
            entry = entry_by_bucket.get(bucket) or {}
            for draw_call in entry.get("draw_calls") or []:
                draw_call.index_count = index_count
                draw_call.index_offset = index_offset
                draw_call_bucket[id(draw_call)] = bucket

        # 记录 cache_key → 原物体 bucket（形态键按焊后顶点归属算池行时用）
        self.weld_bucket_by_cache_key = {}
        for record in self.drawcall_loop_ranges:
            bucket = draw_call_bucket.get(id(record.get("draw_call")))
            cache_key = record.get("cache_key")
            if bucket is not None and cache_key is not None:
                self.weld_bucket_by_cache_key[cache_key] = int(bucket)

        vertex_owner_map = stats.get("vertex_owner_map")
        self.weld_vertex_owner_map = (
            numpy.asarray(vertex_owner_map, dtype=numpy.int64)
            if vertex_owner_map is not None
            else None
        )

        segment_by_bucket = {
            int(segment.get("bucket", -1)): segment for segment in segments
        }
        for record in self.drawcall_loop_ranges:
            bucket = draw_call_bucket.get(id(record.get("draw_call")))
            segment = segment_by_bucket.get(bucket) if bucket is not None else None
            if not segment:
                continue
            loop_start = int(segment.get("loop_start", 0) or 0)
            loop_count = int(segment.get("loop_count", 0) or 0)
            record["loop_start"] = loop_start
            record["loop_count"] = loop_count
            record["index_offset"] = int(segment.get("index_offset", 0) or 0)
            cache_key = record.get("cache_key")
            if cache_key_to_geometry_record is not None and cache_key in cache_key_to_geometry_record:
                geometry_record = cache_key_to_geometry_record.get(cache_key) or {}
                geometry_record["loop_start"] = loop_start
                geometry_record["loop_count"] = loop_count

        self.index_count = int(total_index_count)
        self.seam_weld_index_recount_applied = True
        return True

    def _build_weld_pool_owner_rows(self, merged_obj) -> dict:
        """焊后顶点池行号 → 原物体 bucket（用焊后 mesh 顶点上的原物体 id）。

        每行导出顶点池行对应 mesh 的一个 loop（``unique_first_loop_indices``）；该 loop
        的顶点在焊后带「原物体 id」，据此把每一行归到某个原物体。返回
        ``{cache_key: numpy.ndarray(行号升序)}``，只包含本子网格里匹配到的 cache_key。
        """
        owner_map = self.weld_vertex_owner_map
        if owner_map is None or self.unique_first_loop_indices is None:
            return {}
        mesh = getattr(merged_obj, "data", None)
        loops = getattr(mesh, "loops", None) if mesh is not None else None
        if loops is None:
            return {}
        loop_count = int(len(loops))
        if loop_count <= 0:
            return {}
        loop_vertex = numpy.empty(loop_count, dtype=numpy.int32)
        try:
            loops.foreach_get("vertex_index", loop_vertex)
        except (AttributeError, TypeError, RuntimeError, ValueError):
            loop_vertex = numpy.asarray(
                [int(loop.vertex_index) for loop in loops], dtype=numpy.int32
            )
        first_loops = numpy.asarray(self.unique_first_loop_indices, dtype=numpy.int64)
        first_loops = first_loops[(first_loops >= 0) & (first_loops < loop_count)]
        if first_loops.size == 0:
            return {}
        row_vertex = loop_vertex[first_loops].astype(numpy.int64)
        valid = (row_vertex >= 0) & (row_vertex < int(owner_map.size))
        row_owner = numpy.full(int(first_loops.size), -1, dtype=numpy.int64)
        row_owner[valid] = owner_map[row_vertex[valid]]

        rows_by_bucket: dict = {}
        for cache_key, bucket in (self.weld_bucket_by_cache_key or {}).items():
            rows = numpy.flatnonzero(row_owner == int(bucket)).astype(numpy.int64)
            if rows.size:
                rows_by_bucket[cache_key] = rows
        return rows_by_bucket

    def _warn_weld(self, message: str) -> None:
        """接缝焊接的 warning（不中断导出）。"""
        try:
            from ..utils.log_utils import LOG

            LOG.warning(f"⚠️ [接缝焊接] 子网格 {self.unique_str}: {message}")
        except Exception:
            print(f"[接缝焊接] 子网格 {self.unique_str}: {message}")

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
        #
        # 【t81-F1】HIMI 合并/join 导出：**禁用几何复用**（`preserve_distinct_export_contexts` 的既有语义）。
        #   为什么：复用键只有 `(obj_hash, original_name)`，而合并/join 会把同一源对象用在不同 draw call 上
        #   （去重 + 紧凑重编号后行序不同）⇒ **键相同、几何不同** ⇒ 命中方不写自己的几何、直接拿
        #   首写方的 offset/count ⇒ 位置被跨行复制（t80 实测 46 行 / 产物段长边 190）。
        #   维护者已有同类先例：直出 ShapeKey 场景就是靠这个开关禁复用（`:227-228` / `:247`）。
        # 【t83-A1】去掉 `import_merged_vgmap()` 这半条件：t82 已确认本缺陷与缓存无关（12/12 `hit=no`），
        #   而这个门本来就不该"只在合并开关打开时"才禁复用 —— **HIMI 的 join 路径两种模式都会走**，
        #   所以普通导出同样禁用；日志里同时打 `logic_name` 与开关实际取值，让"没生效"可判。
        if GlobalConfig.logic_name == LogicName.HIMI:
            print(
                f"[SubMeshModel][t83-A1] HIMI 禁用几何复用 "
                f"(logic_name={GlobalConfig.logic_name!r}, "
                f"import_merged_vgmap={bool(GlobalProterties.import_merged_vgmap())}) "
                f"unique_str={self.unique_str}"
            )
            return True
        return False

    def _deduplicate_draw_calls(self):
        if len(self.drawcall_model_list) <= 1:
            return

        seen_keys = set()
        deduped = []
        for dcm in self.drawcall_model_list:
            condition_str = dcm.get_condition_str()
            draw_key = (dcm.index_offset, dcm.index_count, condition_str)
            transparency_info = getattr(dcm, 'transparency_info', None)
            if transparency_info is not None:
                draw_key += (tuple(sorted(transparency_info.items())),)
            if condition_str or draw_key not in seen_keys:
                seen_keys.add(draw_key)
                deduped.append(dcm)

        removed = len(self.drawcall_model_list) - len(deduped)
        if removed > 0:
            print(f"[SubMeshModel] 绘制去重: {self.unique_str} 移除 {removed} 个重复绘制 (原始 {len(self.drawcall_model_list)} → 保留 {len(deduped)})")
            self.drawcall_model_list = deduped

    def _precompute_object_hashes(self) -> tuple:
        """主线程批量提取，原生权重打包/哈希并行；同一源对象只提取一次。"""
        hashes, source_obj_list, profile = prepare_object_hashes(
            self.drawcall_model_list, ObjUtils.get_obj_by_name,
            extractor=self._extract_object_raw_data,
        )
        print('[GeometryHashProfile] ' + json.dumps(profile, ensure_ascii=False), flush=True)
        return hashes, source_obj_list

    @staticmethod
    def _extract_object_raw_data(obj) -> list:
        """保持原哈希字节顺序，权重使用连续列数组代替大量混合 Python 对象。"""
        return extract_object_hash_data(obj)

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
        ExportUtils.apply_export_space_transform_to_object(temp_obj, logic_name=GlobalConfig.logic_name)
