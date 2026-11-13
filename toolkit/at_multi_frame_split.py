# -*- coding: utf-8 -*-

import bpy
import mathutils
from mathutils import Matrix, Vector
import numpy as np
import math
import ast
import os

try:
    from ..utils.shapekey_rebase_utils import rebase_shape_key_coordinates
except ImportError:
    from utils.shapekey_rebase_utils import rebase_shape_key_coordinates


# 用作 mesh 中保存稳定ID的自定义属性键，方便后续帧从快照中读出
_STABLE_ID_STORAGE_KEY = "atp_stable_id_attr"


def _candidate_stable_id_names(props):
    """读取 props.stable_id_attribute（逗号分隔）并去除空白"""
    raw = ""
    if props is not None:
        raw = getattr(props, "stable_id_attribute", "") or ""
    names = [token.strip() for token in raw.split(",") if token.strip()]
    if not names:
        names = ["stable_id"]
    return names


def _read_stable_ids_from_mesh(mesh, candidate_names):
    """从 mesh 的顶点域属性中读取稳定 ID。
    返回 (numpy int64 array, attr_name) 或 (None, None)。
    """
    if mesh is None or not candidate_names:
        return None, None
    attributes = getattr(mesh, "attributes", None)
    if attributes is None:
        return None, None
    vtx_count = len(mesh.vertices)
    for name in candidate_names:
        attr = attributes.get(name) if hasattr(attributes, "get") else None
        if attr is None:
            continue
        if getattr(attr, "domain", None) != 'POINT':
            continue
        data_type = getattr(attr, "data_type", None)
        try:
            if data_type in {'INT', 'INT32'}:
                buf = np.zeros(vtx_count, dtype=np.int32)
                attr.data.foreach_get("value", buf)
                return buf.astype(np.int64, copy=False), name
            if data_type == 'FLOAT':
                buf = np.zeros(vtx_count, dtype=np.float32)
                attr.data.foreach_get("value", buf)
                return np.rint(buf).astype(np.int64), name
        except Exception:
            continue
    return None, None


def _snapshot_stable_ids_to_mesh(mesh, ids, attr_name):
    """把读到的稳定 ID 缓存到 mesh.id_properties，避免后续帧因 GN 重排丢失"""
    if mesh is None or ids is None:
        return
    try:
        mesh[_STABLE_ID_STORAGE_KEY] = list(int(v) for v in ids)
        mesh[_STABLE_ID_STORAGE_KEY + "_name"] = str(attr_name or "")
    except Exception:
        pass


def _read_cached_stable_ids(mesh):
    """从 mesh.id_properties 读出已缓存的稳定 ID 数组"""
    if mesh is None:
        return None, None
    try:
        cached = mesh.get(_STABLE_ID_STORAGE_KEY)
    except Exception:
        cached = None
    if cached is None:
        return None, None
    try:
        ids = np.fromiter((int(v) for v in cached), dtype=np.int64)
    except Exception:
        return None, None
    name = mesh.get(_STABLE_ID_STORAGE_KEY + "_name", "")
    return ids, str(name) if name else ""


def _resolve_stable_ids_for_object(obj, candidate_names):
    """优先读取 mesh 上已缓存的稳定 ID，回退到当前属性"""
    if obj is None or obj.type != 'MESH':
        return None, None
    mesh = obj.data
    ids, name = _read_cached_stable_ids(mesh)
    if ids is not None and len(ids) == len(mesh.vertices):
        return ids, name or None
    return _read_stable_ids_from_mesh(mesh, candidate_names)


def _build_position_nearest_mapping(base_coords, target_coords):
    """使用纯 numpy 的最近邻贪心匹配构建 target -> base 的顶点映射。"""
    base_coords = np.asarray(base_coords, dtype=np.float32)
    target_coords = np.asarray(target_coords, dtype=np.float32)

    base_count = len(base_coords)
    target_count = len(target_coords)
    mapping = np.empty(target_count, dtype=np.int64)
    used = np.zeros(base_count, dtype=bool)
    collisions = 0

    for t_idx, target_co in enumerate(target_coords):
        distances = np.linalg.norm(base_coords - target_co, axis=1)
        candidate_indices = np.argsort(distances)

        chosen_idx = -1
        for candidate_idx in candidate_indices.tolist():
            if not used[candidate_idx]:
                chosen_idx = int(candidate_idx)
                break

        if chosen_idx < 0:
            chosen_idx = int(candidate_indices[0])
            collisions += 1

        used[chosen_idx] = True
        mapping[t_idx] = chosen_idx

    return mapping, collisions


# 稳定ID采样校验：默认关闭，只有 ATP_STABLE_ID_DEBUG=1 才启用。
# 判据是「映射正确」——target 顶点 i 的 ID 必须等于 base 顶点 mapping[i] 的 ID；
# 不能拿「属性等于 0..N-1」当判据，因为用户可以通过几何节点写入任意稳定ID。
_STABLE_ID_DEBUG_ENV = "ATP_STABLE_ID_DEBUG"
_STABLE_ID_DEBUG_SAMPLES = 16


def _stable_id_debug_enabled():
    raw = os.environ.get(_STABLE_ID_DEBUG_ENV, "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _verify_stable_id_mapping_sampled(mapping, base_ids, target_ids,
                                      sample_limit=_STABLE_ID_DEBUG_SAMPLES):
    """debug 模式下抽样校验 target→base 映射与两侧稳定ID是否一致。

    默认零成本：ATP_STABLE_ID_DEBUG 未开启时第一行就返回。
    抽样点用 np.linspace 等距取（含首尾），样本固定、可复现；
    V < 16 时全抽。失败抛 RuntimeError，消息带采样下标、mapping 值、两侧 ID。
    """
    if not _stable_id_debug_enabled():
        return

    target_count = int(len(target_ids))
    base_count = int(len(base_ids))
    if target_count == 0 or base_count == 0:
        return
    if len(mapping) != target_count:
        raise RuntimeError(
            f"稳定ID采样校验失败：mapping 长度 {len(mapping)} 与 target 顶点数 {target_count} 不一致"
        )

    sample_count = min(int(sample_limit), target_count)
    if sample_count >= target_count:
        sample_indices = range(target_count)
    else:
        sample_indices = np.linspace(0, target_count - 1, sample_count).astype(np.int64)

    for raw_index in sample_indices:
        index = int(raw_index)
        base_index = int(mapping[index])
        target_id = int(target_ids[index])
        if base_index < 0 or base_index >= base_count:
            raise RuntimeError(
                f"稳定ID采样校验失败：target[{index}] 的 ID {target_id} 映射到非法 base 索引 "
                f"{base_index}（mapping[{index}]={base_index}）"
            )
        base_id = int(base_ids[base_index])
        if base_id != target_id:
            raise RuntimeError(
                f"稳定ID采样校验失败：target[{index}].id={target_id} != "
                f"base[{base_index}].id={base_id}（mapping[{index}]={base_index}）"
            )


def _build_target_to_base_mapping(base_obj, target_obj, props):
    """构建 target 顶点 index → base 顶点 index 的映射数组。

    返回 (mapping numpy int64 array, mode_used_str, message)
    若返回 (None, mode_used_str, message) 表示无法构建。
    AUTO 与 STABLE_ID 模式在稳定ID缺失/匹配失败时会自动降级为 KDTree 位置匹配，
    再失败时回退到顶点index对齐（仅在用户显式选择INDEX时静默）。
    """
    base_mesh = base_obj.data
    target_mesh = target_obj.data
    base_count = len(base_mesh.vertices)
    target_count = len(target_mesh.vertices)
    if base_count != target_count:
        return None, "NONE", f"顶点数不一致 ({base_count} vs {target_count})"

    mode = getattr(props, "vertex_mapping_mode", "AUTO") if props else "AUTO"
    if mode not in {"AUTO", "STABLE_ID", "POSITION", "INDEX"}:
        mode = "AUTO"
    candidates = _candidate_stable_id_names(props)

    stable_id_diag = ""

    if mode in {"AUTO", "STABLE_ID"}:
        base_ids, base_attr = _resolve_stable_ids_for_object(base_obj, candidates)
        target_ids, target_attr = _resolve_stable_ids_for_object(target_obj, candidates)
        if base_ids is not None and target_ids is not None and len(base_ids) == base_count and len(target_ids) == target_count:
            base_lookup = {}
            for idx, value in enumerate(base_ids):
                base_lookup.setdefault(int(value), idx)
            mapping = np.empty(target_count, dtype=np.int64)
            missing = 0
            for t_idx, value in enumerate(target_ids):
                base_idx = base_lookup.get(int(value))
                if base_idx is None:
                    missing += 1
                    mapping[t_idx] = -1
                else:
                    mapping[t_idx] = base_idx
            if missing == 0:
                attr_label = base_attr or target_attr or "stable_id"
                # 调试开关下的抽样自检：确认 mapping 把每个 target 顶点指向了
                # 同一个稳定 ID 的 base 顶点。默认关闭，不改变正常运行行为。
                _verify_stable_id_mapping_sampled(mapping, base_ids, target_ids)
                return mapping, "STABLE_ID", f"基于稳定ID属性 '{attr_label}' 建立顶点映射"
            stable_id_diag = f"稳定ID属性匹配失败：{missing}/{target_count} 个目标顶点未找到对应ID"
        else:
            tried = ",".join(candidates) if candidates else "stable_id"
            stable_id_diag = (
                f"未找到稳定ID属性 (尝试: {tried})。"
                f"建议在GN开头用 Store Named Attribute(Point域, INT) 把 Index 存为 '{candidates[0] if candidates else 'stable_id'}'"
            )

    # 进入位置最近邻分支（AUTO/STABLE_ID 失败时降级，或显式选择 POSITION）
    if mode in {"AUTO", "STABLE_ID", "POSITION"}:
        base_coords = np.zeros(base_count * 3, dtype=np.float32)
        base_mesh.vertices.foreach_get("co", base_coords)
        base_coords = base_coords.reshape(-1, 3)
        target_coords = np.zeros(target_count * 3, dtype=np.float32)
        target_mesh.vertices.foreach_get("co", target_coords)
        target_coords = target_coords.reshape(-1, 3)

        try:
            import mathutils  # noqa: WPS433
            kd = mathutils.kdtree.KDTree(base_count)
            for idx, co in enumerate(base_coords):
                kd.insert((float(co[0]), float(co[1]), float(co[2])), idx)
            kd.balance()

            mapping = np.empty(target_count, dtype=np.int64)
            used = np.zeros(base_count, dtype=bool)
            collisions = 0
            for t_idx, co in enumerate(target_coords):
                found_idx = -1
                for _co_found, idx_found, _dist in kd.find_n((float(co[0]), float(co[1]), float(co[2])), 4):
                    if not used[idx_found]:
                        found_idx = idx_found
                        break
                if found_idx < 0:
                    _co_found, idx_found, _dist = kd.find((float(co[0]), float(co[1]), float(co[2])))
                    found_idx = idx_found
                    collisions += 1
                used[found_idx] = True
                mapping[t_idx] = found_idx
            message = "基于位置最近邻(KDTree)建立顶点映射"
            if stable_id_diag:
                message = f"已降级为位置最近邻: {stable_id_diag}"
            if collisions:
                message += f"（{collisions} 处一对多冲突已就近回退）"
            return mapping, "POSITION", message
        except Exception as exc:
            try:
                mapping, collisions = _build_position_nearest_mapping(base_coords, target_coords)
                message = "基于位置最近邻(numpy)建立顶点映射"
                if stable_id_diag:
                    message = f"已降级为位置最近邻: {stable_id_diag}"
                if collisions:
                    message += f"（{collisions} 处一对多冲突已就近回退）"
                message += f"；KDTree不可用: {exc}"
                return mapping, "POSITION", message
            except Exception as fallback_exc:
                if mode == "POSITION":
                    return None, "POSITION", f"位置最近邻匹配失败: {fallback_exc}"
                stable_id_diag = (stable_id_diag + "；" if stable_id_diag else "") + f"KDTree匹配失败: {exc}；numpy回退失败: {fallback_exc}"

    mapping = np.arange(target_count, dtype=np.int64)
    final_message = "按顶点index一一对应（GN若改变顺序将得到错误结果）"
    if stable_id_diag:
        final_message = f"{stable_id_diag}；最终回退为按index对齐"
    return mapping, "INDEX", final_message



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


def _snapshot_shape_key_coordinates(key_blocks):
    coordinates = {}
    for key_block in key_blocks:
        key_coords = np.empty(len(key_block.data) * 3, dtype=np.float32)
        key_block.data.foreach_get("co", key_coords)
        coordinates[key_block.name] = key_coords.reshape(-1, 3)
    return coordinates


def _restore_shape_key_coordinates(key_blocks, coordinates_by_name):
    for key_block in key_blocks:
        key_coords = coordinates_by_name.get(key_block.name)
        if key_coords is None:
            continue
        key_block.data.foreach_set("co", np.asarray(key_coords, dtype=np.float32).reshape(-1))


def _allocate_temp_shape_key_name(existing_names, base_name):
    candidate = base_name
    index = 1
    while candidate in existing_names:
        candidate = f"{base_name}_{index}"
        index += 1
    return candidate


def _rebase_target_shape_keys_to_baked_basis(target_keys, baked_basis_coords):
    basis_key = getattr(target_keys, "reference_key", None)
    if basis_key is None:
        raise ValueError("target object is missing a Basis shape key")

    baked_basis_coords = np.asarray(baked_basis_coords, dtype=np.float32)
    non_basis_keys = [key_block for key_block in target_keys.key_blocks if key_block != basis_key]
    if not non_basis_keys:
        basis_key.data.foreach_set("co", baked_basis_coords.reshape(-1))
        return

    coordinate_snapshot = _snapshot_shape_key_coordinates(target_keys.key_blocks)
    temp_basis_name = _allocate_temp_shape_key_name(
        set(coordinate_snapshot.keys()),
        "__atp_baked_basis__",
    )
    coordinate_snapshot[temp_basis_name] = np.array(baked_basis_coords, copy=True)

    rebased_coordinates = rebase_shape_key_coordinates(
        coordinates_by_name=coordinate_snapshot,
        basis_name=basis_key.name,
        new_basis_name=temp_basis_name,
        remove_new_basis_key=True,
    )
    _restore_shape_key_coordinates(target_keys.key_blocks, rebased_coordinates)


def _ensure_stable_id_on_original_mesh(obj, candidate_names):
    """在原始 mesh 上写入一个 INT 顶点属性作为稳定 ID。

    使用 candidate_names 中第一个名字（默认 'stable_id'）。
    如果同名属性已存在且类型正确，则直接复用；否则创建/重建。
    返回 (attr_name, was_created) 元组。
    """
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return None, False
    if not candidate_names:
        candidate_names = ["stable_id"]
    name = candidate_names[0]
    mesh = obj.data
    attributes = getattr(mesh, "attributes", None)
    if attributes is None:
        return None, False

    vtx_count = len(mesh.vertices)
    existing = attributes.get(name) if hasattr(attributes, "get") else None

    needs_rebuild = True
    if existing is not None:
        if (
            getattr(existing, "domain", None) == 'POINT'
            and getattr(existing, "data_type", None) in {'INT', 'INT32'}
            and len(existing.data) == vtx_count
        ):
            # 结构检查（域/类型/长度）通过就复用。属性内容可能是用户通过几何节点
            # 写入的任意 ID，不能假设 0..N-1；内容是否可用由映射阶段的
            # ATP_STABLE_ID_DEBUG 采样校验负责（默认零成本，见
            # _verify_stable_id_mapping_sampled）。
            # 这里只读 3 个点做“读得动吗”的探针，保留老代码 foreach_get 抛错时
            # 删掉属性重建的错误路径。注意：Attribute.data.foreach_get 要求目标
            # 数组长度恰好等于 V（Blender 5.0.1 实测短数组抛 RuntimeError），
            # 所以探针用逐元素索引读，而不是喂 3 个元素的 foreach_get。
            try:
                if vtx_count:
                    for probe_index in {0, vtx_count // 2, vtx_count - 1}:
                        _ = existing.data[probe_index].value
                needs_rebuild = False
            except Exception:
                needs_rebuild = True
        if needs_rebuild:
            try:
                attributes.remove(existing)
            except Exception:
                # 移除失败就别再尝试创建，避免命名冲突
                return name, False

    if needs_rebuild:
        try:
            attr = attributes.new(name=name, type='INT', domain='POINT')
        except Exception:
            return None, False
        ids = np.arange(vtx_count, dtype=np.int32)
        try:
            attr.data.foreach_set("value", ids)
        except Exception:
            return name, False
        mesh.update()
        return name, True

    return name, False



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
        self._baseline_mesh_names = set(bpy.data.meshes.keys())
        
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
        
        # 在原始 mesh 上注入稳定ID属性，确保经过几何节点重排后仍能反查回原顶点
        if getattr(props, "vertex_mapping_mode", "AUTO") in {"AUTO", "STABLE_ID"}:
            candidate_names = _candidate_stable_id_names(props)
            attr_name, was_created = _ensure_stable_id_on_original_mesh(current_obj, candidate_names)
            if was_created and attr_name:
                self.report({'INFO'}, f"已在 '{current_obj.name}' 上注入稳定ID属性 '{attr_name}'")
        
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
        
        candidate_names = _candidate_stable_id_names(props)
        ids, attr_name = _read_stable_ids_from_mesh(mesh_data, candidate_names)
        if ids is not None:
            _snapshot_stable_ids_to_mesh(mesh_data, ids, attr_name)
        
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
                flat = self._matrix_to_flat_list(current_obj.matrix_world)
                if flat is not None:
                    self._temp_start_obj["atp_original_matrix_world"] = str(flat)
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
                    flat = self._matrix_to_flat_list(current_obj.matrix_world)
                    if flat is not None:
                        frame_base_obj["atp_original_matrix_world"] = str(flat)
                    
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
            self._remove_temp_object_and_mesh(self._temp_end_obj)
            self._temp_end_obj = None
        
        if self._temp_collection and self._temp_collection.name in bpy.data.collections:
            preserve_start_obj = bool(self._temp_start_obj and self._temp_start_obj.get("atp_base_frame"))
            self.cleanup_temp_collection(
                context,
                self._temp_collection,
                self._temp_start_obj,
                current_obj,
                preserve_start_obj=preserve_start_obj,
            )
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
        
        if not props.copy_shape_keys_back:
            self._cleanup_intermediate_bases_keep_first(context)
            self._force_cleanup_all_temp_split_collections(context)
            self.report({'INFO'}, "已清理连续模式生成的中间_Base物体与Temp_Split合集，仅保留起始帧对应的_Base物体")
            props.current_processing_status = "处理完成（未回拷形态键）"
            props.current_processing_progress = 1.0
            self._state = self.STATE_FINAL_CLEANUP
            return 'YIELD'
        
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
                self._remove_temp_object_and_mesh(intermediate_obj)
                self.report({'INFO'}, f"已删除中间物体 '{intermediate_obj_name}'")
            except Exception as e:
                self.report({'WARNING'}, f"删除中间物体 '{intermediate_obj_name}' 时出错: {e}")
        else:
            success = self.copy_shape_keys_between_objects(context, intermediate_obj, original_obj)
            
            if success:
                intermediate_obj_name = intermediate_obj.name
                self.report({'INFO'}, f"成功将 '{intermediate_obj_name}' 的形态键复制到 '{original_obj.name}'")
                
                try:
                    self._remove_temp_object_and_mesh(intermediate_obj)
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
        context.view_layer.update()
        
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
    
    def cleanup_temp_collection(self, context, temp_collection, start_obj, original_obj, preserve_start_obj=False):
        """清理临时集合但保留起始帧物体"""
        if preserve_start_obj and start_obj and start_obj.name:
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
                if temp_obj and temp_obj.name in bpy.data.objects:
                    if preserve_start_obj and start_obj and temp_obj.name == start_obj.name:
                        try:
                            sub_coll.objects.unlink(temp_obj)
                        except Exception:
                            pass
                        continue
                    try:
                        self._remove_temp_object_and_mesh(temp_obj)
                    except Exception:
                        pass
            if sub_coll.name in bpy.data.collections:
                bpy.data.collections.remove(sub_coll)
        
        for temp_obj in list(temp_collection.objects):
            if temp_obj and temp_obj.name in bpy.data.objects:
                if preserve_start_obj and start_obj and temp_obj.name == start_obj.name:
                    try:
                        temp_collection.objects.unlink(temp_obj)
                    except Exception:
                        pass
                    continue
                try:
                    self._remove_temp_object_and_mesh(temp_obj)
                except Exception:
                    pass
        
        if temp_collection.name in bpy.data.collections:
            bpy.data.collections.remove(temp_collection)
    
    @staticmethod
    def _remove_temp_object_and_mesh(obj):
        """删除临时物体，并在其 mesh 无引用时一并释放。

        仅调用 bpy.data.objects.remove() 会把网格留在 bpy.data.meshes 成为孤儿
        （除非显式 meshes.remove 或 orphans purge），导致 Blender 内存随每次
        运行持续增长、只能重启后释放。
        """
        if obj is None:
            return
        data = getattr(obj, "data", None)
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            return
        try:
            if data is not None and data.users == 0 and data.name in bpy.data.meshes:
                bpy.data.meshes.remove(data)
        except Exception:
            pass

    def _purge_run_orphan_meshes(self):
        """兜底：释放本次运行期间新建、且已无任何引用（users==0）的 mesh 数据块。

        覆盖异常 / 中途取消等未走到逐物体清理的路径，防止孤儿 mesh 长期驻留内存。
        """
        baseline = getattr(self, "_baseline_mesh_names", None)
        if not baseline:
            return
        for name in list(bpy.data.meshes.keys()):
            if name in baseline:
                continue
            mesh = bpy.data.meshes.get(name)
            if mesh is None:
                continue
            try:
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
            except Exception:
                pass

    def finish_processing(self, context):
        """完成处理"""
        props = context.scene.atp_props
        
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        
        self._purge_run_orphan_meshes()
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

    def _cleanup_intermediate_bases_keep_first(self, context):
        """清理连续模式产生的中间 _Base 物体，仅保留起始帧对应的 _Base 物体"""
        props = context.scene.atp_props
        start_frame = props.multi_object_start_frame
        
        intermediate_objects, _ = self.find_intermediate_objects(context)
        
        removed_count = 0
        kept_count = 0
        
        for intermediate_obj in intermediate_objects:
            original_name = intermediate_obj.get("atp_original_object")
            frame_number = intermediate_obj.get("atp_frame_number")
            
            if original_name is None or frame_number is None:
                continue
            
            if frame_number == start_frame:
                kept_count += 1
                continue
            
            if frame_number > start_frame:
                obj_name = intermediate_obj.name
                try:
                    self._remove_temp_object_and_mesh(intermediate_obj)
                    removed_count += 1
                    print(f"[DEBUG] 清理中间_Base: '{obj_name}'")
                except Exception as e:
                    print(f"[DEBUG] 清理中间_Base '{obj_name}' 失败: {e}")
        
        self.report({'INFO'}, f"清理完成：保留 {kept_count} 个起始_Base，删除 {removed_count} 个中间_Base")

    def _force_cleanup_all_temp_split_collections(self, context):
        """强制清理所有 Temp_Split_* 合集（包括其子合集），仅保留起始帧 _Base 物体"""
        protected_obj_names = set()
        for obj in bpy.data.objects:
            if obj.type == 'MESH' and obj.get("atp_base_frame"):
                if obj.get("atp_frame_number") == context.scene.atp_props.multi_object_start_frame:
                    protected_obj_names.add(obj.name)
        
        collections_to_remove = []
        for coll in list(bpy.data.collections):
            if coll.name.startswith("Temp_Split_"):
                collections_to_remove.append(coll)
        
        removed_collections = 0
        for coll in collections_to_remove:
            try:
                for sub_coll in list(coll.children):
                    for temp_obj in list(sub_coll.objects):
                        if temp_obj and temp_obj.name in bpy.data.objects:
                            if temp_obj.name in protected_obj_names:
                                for c in list(temp_obj.users_collection):
                                    if c == sub_coll or c == coll:
                                        c.objects.unlink(temp_obj)
                                continue
                            try:
                                self._remove_temp_object_and_mesh(temp_obj)
                            except Exception:
                                pass
                    if sub_coll.name in bpy.data.collections:
                        try:
                            bpy.data.collections.remove(sub_coll)
                            removed_collections += 1
                        except Exception:
                            pass
                
                for temp_obj in list(coll.objects):
                    if temp_obj and temp_obj.name in bpy.data.objects:
                        if temp_obj.name in protected_obj_names:
                            for c in list(temp_obj.users_collection):
                                if c == coll:
                                    c.objects.unlink(temp_obj)
                            continue
                        try:
                            self._remove_temp_object_and_mesh(temp_obj)
                        except Exception:
                            pass
                
                if coll.name in bpy.data.collections:
                    try:
                        bpy.data.collections.remove(coll)
                        removed_collections += 1
                    except Exception as e:
                        print(f"[DEBUG] 移除合集 '{coll.name}' 失败: {e}")
            except Exception as e:
                print(f"[DEBUG] 处理合集 '{coll.name}' 时出错: {e}")
        
        self.report({'INFO'}, f"已强制清理 {removed_collections} 个 Temp_Split 合集")
    
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
                        self._remove_temp_object_and_mesh(intermediate_obj)
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
                        self._remove_temp_object_and_mesh(intermediate_obj)
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
        
        source_keys = source_mesh.shape_keys
        if not source_keys.reference_key:
            return False
        
        basis_key = source_keys.reference_key

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
            return False

        _rebase_target_shape_keys_to_baked_basis(target_keys, target_local_coords)

        source_to_target = target_obj.matrix_world.inverted() @ source_obj.matrix_world
        transform_linear = np.asarray(
            [[float(source_to_target[row][col]) for col in range(3)] for row in range(3)],
            dtype=np.float32,
        )
        transform_translation = np.asarray(
            [float(source_to_target[row][3]) for row in range(3)],
            dtype=np.float32,
        )

        for kb in source_keys.key_blocks:
            if kb == basis_key:
                continue

            key_coords = np.zeros(source_vtx_count * 3, dtype=np.float32)
            kb.data.foreach_get("co", key_coords)
            key_coords = key_coords.reshape(-1, 3)

            transformed_coords = key_coords @ transform_linear.T + transform_translation

            target_kb = target_keys.key_blocks.get(kb.name)
            if not target_kb:
                target_kb = target_obj.shape_key_add(name=kb.name, from_mix=False)

            target_kb.data.foreach_set("co", transformed_coords.reshape(-1))
            
            target_kb.value = kb.value
            target_kb.slider_min = kb.slider_min
            target_kb.slider_max = kb.slider_max
            target_kb.mute = kb.mute

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
        
        self._purge_run_orphan_meshes()
        self.report({'WARNING'}, "处理已取消")
        self._is_processing = False
        
        props.current_processing_status = "处理已取消"
        props.current_processing_progress = 0.0
    
    @staticmethod
    def _matrix_to_flat_list(matrix):
        """将 Matrix 序列化为扁平列表（4x4 = 16 个 float）"""
        try:
            result = []
            for i in range(4):
                for j in range(4):
                    result.append(float(matrix[i][j]))
            return result
        except Exception:
            return None

    @staticmethod
    def _flat_list_to_matrix(flat_list):
        """将扁平列表还原为 Matrix"""
        if not flat_list or len(flat_list) != 16:
            return None
        try:
            return Matrix((flat_list[0:4], flat_list[4:8], flat_list[8:12], flat_list[12:16]))
        except Exception:
            return None

    def find_or_create_base_frame_object(self, context, original_obj, frame):
        """查找或创建指定帧的基础物体"""
        base_obj_name = f"{original_obj.name}_{frame:03d}_Base"
        
        existing_base_obj = bpy.data.objects.get(base_obj_name)
        if existing_base_obj and existing_base_obj.type == 'MESH':
            self.report({'INFO'}, f"找到已存在的基础帧物体 '{base_obj_name}'")
            
            stored_matrix_str = existing_base_obj.get("atp_original_matrix_world")
            stored_matrix = None
            if stored_matrix_str and isinstance(stored_matrix_str, str):
                try:
                    parsed = ast.literal_eval(stored_matrix_str)
                    if isinstance(parsed, list) and len(parsed) == 16:
                        stored_matrix = self._flat_list_to_matrix(parsed)
                except (ValueError, SyntaxError):
                    stored_matrix = None
            
            if stored_matrix is not None:
                if not is_matrix_close(stored_matrix, original_obj.matrix_world, 1e-6):
                    self.report({'WARNING'}, f"基础帧物体 '{base_obj_name}' 的变换矩阵与原始物体不一致，可能导致形态键扭曲")
            else:
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
                
                flat = self._matrix_to_flat_list(original_obj.matrix_world)
                if flat is not None:
                    standard_obj["atp_original_matrix_world"] = str(flat)
                
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
        
        props = context.scene.atp_props if hasattr(context, "scene") else None
        mapping, mode_used, mapping_msg = _build_target_to_base_mapping(base_obj, target_obj, props)
        if mapping is None:
            self.report({'ERROR'}, f"无法建立顶点映射: {mapping_msg}")
            return False
        if mode_used == "STABLE_ID":
            self.report({'INFO'}, f"[{base_obj.name}→{target_obj.name}] {mapping_msg}")
        elif mode_used == "POSITION":
            self.report({'WARNING'}, f"[{base_obj.name}→{target_obj.name}] 未找到稳定ID属性，已降级为位置最近邻匹配。{mapping_msg}")
        elif mode_used == "INDEX":
            self.report({'WARNING'}, f"[{base_obj.name}→{target_obj.name}] {mapping_msg}")
        
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
        
        # 将目标帧的顶点按映射重排到 base 顶点顺序
        remapped_target_coords = np.empty_like(basis_coords)
        remapped_target_coords[mapping] = target_coords
        
        if continuous_base_obj:
            continuous_base_coords = np.zeros(vtx_count * 3, dtype=np.float32)
            continuous_base_obj.data.vertices.foreach_get('co', continuous_base_coords)
            continuous_base_coords = continuous_base_coords.reshape(-1, 3)
            
            # 同样把 continuous_base 重排到 base 顶点顺序（若 continuous_base 自带稳定ID）
            cb_mapping, _cb_mode, _cb_msg = _build_target_to_base_mapping(base_obj, continuous_base_obj, props)
            if cb_mapping is not None:
                remapped_cb_coords = np.empty_like(basis_coords)
                remapped_cb_coords[cb_mapping] = continuous_base_coords
            else:
                remapped_cb_coords = continuous_base_coords
            
            delta = remapped_target_coords - remapped_cb_coords
            shape_key_coords = basis_coords + delta
        else:
            shape_key_coords = remapped_target_coords
        
        shape_key.data.foreach_set('co', shape_key_coords.reshape(-1))
        
        return True


at_multi_frame_split_list = (
    ATP_OT_SplitFramesToShapeKeyMulti,
)
