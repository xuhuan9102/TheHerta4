import os
import re
from collections import OrderedDict

import numpy as np

from ..common.mod_path_compat import collect_base_position_resource_map
from ..common.mod_path_compat import derive_shapekey_base_resource_name
from ..common.mod_path_compat import derive_shapekey_frame_table_resource_name
from ..common.mod_path_compat import derive_shapekey_freq_resource_name
from ..common.mod_path_compat import derive_shapekey_group_map_resource_name
from ..common.mod_path_compat import derive_shapekey_merged_data_resource_name
from ..common.mod_path_compat import derive_shapekey_merged_map_resource_name
from ..common.mod_path_compat import derive_shapekey_slot_map_resource_name
from ..common.mod_path_compat import derive_shapekey_slot_resource_name
from ..common.mod_path_compat import derive_shapekey_vertex_entry_freq_resource_name
from ..common.mod_path_compat import derive_shapekey_vertex_entry_packed_resource_name
from ..common.mod_path_compat import derive_shapekey_vertex_entry_start_resource_name
from ..common.mod_path_compat import derive_shapekey_weight_resource_name
from ..common.mod_path_compat import ensure_resource_alias_section
from ..common.safe_write import write_text_if_changed
from ..utils.log_utils import LOG
from .direct_export_runtime_utils import apply_position_override_in_place
from .direct_export_runtime_utils import assemble_drawib_position_bytes
from .direct_export_runtime_utils import extract_position_bytes_by_indices as _extract_position_bytes_by_indices
from .direct_export_runtime_utils import iter_drawib_models as _iter_drawib_models
from .direct_export_shapekey_shared import ShapeKeyDirectExportError, _buffer_to_bytes, resolve_use_delta
try:
    from .shapekey_sparse_index import (
        NO_FREQ_INDEX,
        SPARSE_FREQ_REGISTER,
        SPARSE_FREQ_SUFFIX,
        SPARSE_PACKED_REGISTER,
        SPARSE_PACKED_SUFFIX,
        SPARSE_START_REGISTER,
        SPARSE_START_SUFFIX,
        write_sparse_vertex_index,
    )
except ImportError:  # 测试 stub 包无 __path__ 时退化为绝对导入
    from blueprint.shapekey_sparse_index import (
        NO_FREQ_INDEX,
        SPARSE_FREQ_REGISTER,
        SPARSE_FREQ_SUFFIX,
        SPARSE_PACKED_REGISTER,
        SPARSE_PACKED_SUFFIX,
        SPARSE_START_REGISTER,
        SPARSE_START_SUFFIX,
        write_sparse_vertex_index,
    )

# 「条件判断」后处理节点靠 common/export_section_registry 的登记表反查
# 「某 DrawIB 的 ini 段」。直出形态键生成器直接改写 ini 文本、不经 M_IniBuilder，
# 所以这里要手动登记本次生成的形态键段；模块不可用时退化为 None，绝不影响导出。
try:
    from ..common import export_section_registry as _section_registry
except Exception:
    _section_registry = None


# 形态键权重搬运的跨模块约定：必须与 node_postprocess_shapekey.py 的
# WEIGHT_SYNC_SHADER_NAME / SSMTNode_PostProcess_ShapeKey.WEIGHT_BUFFER_REGISTER
# 一致——两条生成路径写的是同一份 shader、同一张共享表、同一批槽位。
WEIGHT_SYNC_SHADER_NAME = "shapekey_weight_sync.hlsl"
WEIGHT_SYNC_SHADER_REL = "./res/" + WEIGHT_SYNC_SHADER_NAME


class DirectShapeKeyOutputMixin:
    _PRESENT_RUN_BEGIN = "; --- SSMT DIRECT SHAPEKEY PRESENT BEGIN ---"
    _PRESENT_RUN_END = "; --- SSMT DIRECT SHAPEKEY PRESENT END ---"

    #: 形态键强度签名：每帧把本帧所有强度/开关变量按固定权重求和，与上一帧比较，
    #: 没变化就整段跳过 dispatch。一次 dispatch 要读 ~36MB、写 ~9MB 顶点缓冲
    #: （14033 组 × 16 线程），静止（没有动画、没按 Alt 拖拽）时输入完全没变，
    #: 重算一遍是纯显存带宽浪费 —— 装大量同类模组时这是最大的 GPU 开销。
    #: 权重取互不相同的质数：任意单个强度变化都会改变加权和（避免两项抵消）。
    _SK_SIGNATURE_WEIGHTS = (
        1, 3, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53,
        59, 61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109,
        113, 127, 131, 137, 139, 149, 151, 157, 163, 167, 173,
        179, 181, 191, 193, 197, 199, 211, 223, 227, 229, 233,
    )
    #: 单条 INI 表达式里最多塞多少个强度项（3DMigoto 行缓冲有限，超长行会被截断）
    _SK_SIGNATURE_TERMS_PER_LINE = 16

    def _uses_active_guard(self):
        key_map = getattr(getattr(self, "blueprint_model", None), "keyname_mkey_dict", None)
        return bool(key_map)

    @staticmethod
    def _drag_mode_variable_for(drag_drive_resource):
        """从拖拽驱动资源名推出「拖拽系统本帧在臂动」的门控变量。

        资源名形如 ``ResourceDragShapeKeyDrive_{ns}``（拖拽节点命名），对应变量
        ``$ssmtdrag_mode_{ns}``（Alt 按住时为 1）。拖拽驱动缓冲每帧都在变，
        签名看不出来，必须用它强制 dispatch。拿不到就返回空串（不加这一项）。
        """
        text = str(drag_drive_resource or "").strip()
        prefix = "ResourceDragShapeKeyDrive_"
        if not text.startswith(prefix):
            return ""
        namespace = text[len(prefix):].strip()
        return f"$ssmtdrag_mode_{namespace}" if namespace else ""

    def _sk_signature_var_names(self, unique_hashes):
        """签名变量的名字（按 hash 前缀区分，多个形态键节点互不干扰）。"""
        suffix = ""
        for logical_hash in unique_hashes:
            suffix = str(self.node._extract_hash_prefix(logical_hash) or "")
            if suffix:
                break
        safe_suffix = re.sub(r"[^0-9A-Za-z_]", "_", suffix) or "0"
        return f"$ssmt_sk_sig_{safe_suffix}", f"$ssmt_sk_sig_prev_{safe_suffix}"

    def _build_signature_lines(self, signature_var, signature_vars):
        """把签名拆成若干条有界长度的赋值（超长表达式行可能被 3DMigoto 截断）。"""
        params = []
        for name in signature_vars or ():
            text = str(name or "").strip()
            if text and text not in params:
                params.append(text)
        if not params:
            return []

        step = max(1, int(self._SK_SIGNATURE_TERMS_PER_LINE))
        weights = self._SK_SIGNATURE_WEIGHTS
        lines = []
        for chunk_index in range(0, len(params), step):
            chunk = params[chunk_index:chunk_index + step]
            terms = " + ".join(
                f"{param} * {weights[(chunk_index + offset) % len(weights)]}"
                for offset, param in enumerate(chunk)
            )
            if chunk_index == 0:
                lines.append(f"{signature_var} = {terms}")
            else:
                lines.append(f"{signature_var} = {signature_var} + {terms}")
        return lines

    def _build_present_run_block(self, unique_hashes, signature_vars=None, drag_active_var=""):
        """[Present] 里的形态键 dispatch 块。

        带 ``signature_vars`` 时先算签名，只有签名变化（或有拖拽在驱动形态键）才
        真的 dispatch；没有签名变量（旧调用点）时保持原样无条件 dispatch。
        """
        signature_lines = []
        if signature_vars:
            signature_var, _prev_var = self._sk_signature_var_names(unique_hashes)
            signature_lines = self._build_signature_lines(signature_var, signature_vars)

        lines = [self._PRESENT_RUN_BEGIN]
        guard_open = ['if $active0 == 1'] if self._uses_active_guard() else []
        indent = "    " * len(guard_open)

        if signature_lines:
            signature_var, prev_var = self._sk_signature_var_names(unique_hashes)
            condition = f"{signature_var} != {prev_var}"
            if drag_active_var:
                condition = f"{condition} || {drag_active_var} == 1"
            lines.extend(guard_open)
            lines.extend(f"{indent}{line}" for line in signature_lines)
            lines.append(f"{indent}if {condition}")
            # 权重搬运必须先于形态键 CS：把打包窗口搬进 mod 专属权重缓冲并清零，
            # Anim CS 才能从缓冲读到本帧强度（见 shapekey_weight_sync.hlsl）。
            lines.extend(
                f"{indent}    run = CustomShaderShapeKeyWeightSync_{logical_hash}"
                for logical_hash in unique_hashes
            )
            lines.extend(
                f"{indent}    run = CustomShader_{logical_hash}_Anim"
                for logical_hash in unique_hashes
            )
            lines.append(f"{indent}    {prev_var} = {signature_var}")
            lines.append(f"{indent}endif")
            if guard_open:
                lines.append("endif")
        else:
            lines.extend(guard_open)
            lines.extend(
                f"{indent}run = CustomShaderShapeKeyWeightSync_{logical_hash}"
                for logical_hash in unique_hashes
            )
            lines.extend(
                f"{indent}run = CustomShader_{logical_hash}_Anim"
                for logical_hash in unique_hashes
            )
            if guard_open:
                lines.append("endif")
        lines.append(self._PRESENT_RUN_END)
        return lines

    def _delta_stride_for(self, logical_hash, vertex_stride, struct_definition=None):
        """增量资源在 INI 里声明的 stride。

        关闭「存储全部顶点属性增量」时恒为 12（仅位置）；开启时 = 4 × 通道 float 数
        （位置+法线+切线 xyz 即 36）。必须与着色器里的 ``ShapeKeyDelta`` 行宽一致。
        """
        return self.node._resolve_delta_stride(
            hash_val=logical_hash,
            vertex_stride=vertex_stride,
            struct_definition=struct_definition,
        )

    def _delta_channel_columns(self, runtime_info, num_floats_per_vertex, struct_definition=None):
        """增量通道列下标。

        关闭「存储全部顶点属性增量」时恒为 ``[0, 1, 2]``（仅位置），与旧版逐字节一致；
        开启时按顶点数据类型展开（位置 + 法线 + 切线 xyz）。结构体一律按 ``hash_val``
        逐哈希解析——必须与 ``_update_shader_file`` 注入着色器的那个是同一份，
        否则数据列与着色器结构体字段会错位。
        """
        plan = self.node._resolve_delta_channel_plan(
            hash_val=runtime_info.get("logical_hash") if runtime_info else None,
            struct_definition=struct_definition,
            num_floats_per_vertex=num_floats_per_vertex,
        )
        return self.node._channel_plan_columns(plan)

    def _write_slot_files(self, logical_hash, runtime_info, hash_slot_data, slot_position_overrides):
        use_packed = self.node.use_packed_Meshess
        use_delta = resolve_use_delta(self.node)
        actual_hash = runtime_info["actual_hash"]
        base_bytes = runtime_info["base_bytes"]
        struct_definition = self.node._get_vertex_struct_definition()
        slot_maps = {}

        for slot_num, names_data in sorted(hash_slot_data.items()):
            target_bytes = self._compose_slot_bytes(
                logical_hash,
                runtime_info,
                slot_num,
                names_data,
                slot_position_overrides,
            )
            vertex_stride, num_floats_per_vertex, num_vertices = self.node._detect_vertex_format(
                base_bytes,
                target_bytes,
                struct_definition,
                preferred_stride=runtime_info["position_stride"],
            )
            base_data = np.frombuffer(base_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))
            target_data = np.frombuffer(target_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))

            output_prefix = os.path.join(self.meshes_dir, f"{actual_hash}-Position1{slot_num:03d}")
            slot_maps[slot_num] = None

            if use_delta:
                channel_columns = self._delta_channel_columns(
                    runtime_info,
                    num_floats_per_vertex,
                )
                data_to_write = target_data[:, channel_columns] - base_data[:, channel_columns]
                data_to_write[data_to_write == 0] = 0.0
                diff_mask = ~np.isclose(
                    base_data[:, channel_columns],
                    target_data[:, channel_columns],
                    atol=1e-6,
                ).all(axis=1)
                if use_packed:
                    packed_data = data_to_write[diff_mask]
                    map_array = np.full(num_vertices, -1, dtype=np.int32)
                    map_array[diff_mask] = np.arange(np.count_nonzero(diff_mask), dtype=np.int32)
                    with open(f"{output_prefix}_packed_pos_delta.buf", "wb") as file_obj:
                        file_obj.write(packed_data.tobytes())
                    with open(f"{output_prefix}_map.buf", "wb") as file_obj:
                        file_obj.write(map_array.tobytes())
                    slot_maps[slot_num] = map_array
                else:
                    with open(f"{output_prefix}_pos_delta.buf", "wb") as file_obj:
                        file_obj.write(data_to_write.tobytes())
            elif use_packed:
                diff_mask = ~np.isclose(base_data, target_data, atol=1e-6).all(axis=1)
                packed_data = target_data[diff_mask]
                map_array = np.full(num_vertices, -1, dtype=np.int32)
                map_array[diff_mask] = np.arange(np.count_nonzero(diff_mask), dtype=np.int32)
                with open(f"{output_prefix}_packed.buf", "wb") as file_obj:
                    file_obj.write(packed_data.tobytes())
                with open(f"{output_prefix}_map.buf", "wb") as file_obj:
                    file_obj.write(map_array.tobytes())
                slot_maps[slot_num] = map_array
            else:
                with open(f"{output_prefix}.buf", "wb") as file_obj:
                    file_obj.write(target_data.astype(np.float32, copy=False).tobytes())

        return slot_maps

    def _write_merged_slot_files(self, logical_hash, runtime_info, hash_slot_data, slot_position_overrides):
        try:
            from ..common.shapekey_native import packed_slot_deltas
        except ImportError:
            packed_slot_deltas = None
        use_delta = resolve_use_delta(self.node)
        actual_hash = runtime_info["actual_hash"]
        base_bytes = runtime_info["base_bytes"]
        struct_definition = self.node._get_vertex_struct_definition()
        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0

        merged_index_map = None
        merged_data_parts = []
        next_global_index = 0
        base_data = None
        # 增量记录每条的 float 宽度：关闭「全部顶点属性增量」时恒为 3（仅位置）
        delta_channel_width = 3

        for slot_num, names_data in sorted(hash_slot_data.items()):
            target_bytes = self._compose_slot_bytes(
                logical_hash,
                runtime_info,
                slot_num,
                names_data,
                slot_position_overrides,
            )
            if base_data is None:
                vertex_stride, num_floats_per_vertex, num_vertices = self.node._detect_vertex_format(
                    base_bytes,
                    target_bytes,
                    struct_definition,
                    preferred_stride=runtime_info["position_stride"],
                )
                base_data = np.frombuffer(base_bytes, dtype=np.float32).reshape((num_vertices, num_floats_per_vertex))
                merged_index_map = np.full((num_vertices, num_slots), -1, dtype=np.int32)

            target_data = np.frombuffer(target_bytes, dtype=np.float32).reshape(base_data.shape)
            if use_delta:
                channel_columns = self._delta_channel_columns(
                    runtime_info,
                    base_data.shape[1],
                )
                delta_channel_width = len(channel_columns)
                packed_result = (
                    packed_slot_deltas(base_data, target_data, channel_columns,
                                       global_start=next_global_index)
                    if packed_slot_deltas is not None else None
                )
                if packed_result is not None:
                    packed_data, slot_index_map = packed_result
                    if len(packed_data):
                        merged_data_parts.append(packed_data)
                        merged_index_map[:, slot_num - 1] = slot_index_map
                        next_global_index += len(packed_data)
                    continue
                data_to_write = target_data[:, channel_columns] - base_data[:, channel_columns]
                data_to_write[data_to_write == 0] = 0.0
                diff_mask = ~np.isclose(
                    base_data[:, channel_columns],
                    target_data[:, channel_columns],
                    atol=1e-6,
                ).all(axis=1)
            else:
                data_to_write = target_data
                diff_mask = ~np.isclose(base_data, target_data, atol=1e-6).all(axis=1)

            active_count = int(np.count_nonzero(diff_mask))
            if active_count > 0:
                packed_data = data_to_write[diff_mask]
                merged_data_parts.append(packed_data)

                slot_index_map = np.full(base_data.shape[0], -1, dtype=np.int32)
                slot_index_map[diff_mask] = np.arange(next_global_index, next_global_index + active_count, dtype=np.int32)
                merged_index_map[:, slot_num - 1] = slot_index_map
                next_global_index += active_count

        if merged_index_map is None:
            raise ShapeKeyDirectExportError(f"{logical_hash} 未生成任何可用的形态键槽位数据")

        if merged_data_parts:
            merged_data = np.concatenate(merged_data_parts, axis=0)
        else:
            merged_data = np.empty(
                (0, delta_channel_width if use_delta else base_data.shape[1]),
                dtype=np.float32,
            )

        data_suffix = "_merged_packed_pos_delta" if use_delta else "_merged_packed"
        data_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position{data_suffix}.buf")
        map_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_merged_map.buf")
        with open(data_path, "wb") as file_obj:
            file_obj.write(memoryview(merged_data))
        with open(map_path, "wb") as file_obj:
            file_obj.write(memoryview(merged_index_map))

        return merged_index_map

    def _build_freq_index_table(
        self,
        actual_hash,
        hash_slot_data,
        unique_names,
        vertex_count,
        calculated_ranges,
        merged_index_map=None,
        slot_index_maps=None,
    ):
        """构建稠密 FREQ 表（顶点数 × 槽位数，未命中为 ``NO_FREQ_INDEX``）。

        稠密写出与「顶点命中索引（稀疏查找）」共用这一份结果：稀疏索引只是把
        同一张表的非哨兵条目按顶点压紧，不改变判定逻辑，避免两条路径走偏。
        """
        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0
        if num_slots <= 0 or vertex_count <= 0:
            return None, 0

        name_to_freq_index = {name: index for index, name in enumerate(unique_names)}
        freq_indices = np.full((vertex_count, num_slots), NO_FREQ_INDEX, dtype=np.uint32)
        slot_index_maps = slot_index_maps or {}

        for slot_num, names_data in hash_slot_data.items():
            slot_index = slot_num - 1
            index_map = merged_index_map[:, slot_index] if merged_index_map is not None else slot_index_maps.get(slot_num)

            for shapekey_name, objects in names_data.items():
                freq_idx = name_to_freq_index.get(shapekey_name, NO_FREQ_INDEX)
                if freq_idx == NO_FREQ_INDEX:
                    continue

                for obj_name in objects:
                    range_tuple = calculated_ranges.get(obj_name)
                    if not range_tuple:
                        continue

                    start_v, end_v = range_tuple[:2]
                    start_v = max(0, min(start_v, vertex_count - 1))
                    end_v = max(0, min(end_v, vertex_count - 1))

                    if index_map is not None:
                        valid_local = np.flatnonzero(index_map[start_v:end_v + 1] >= 0)
                        if valid_local.size > 0:
                            valid_vertices = valid_local + start_v
                            freq_indices[valid_vertices, slot_index] = freq_idx
                    else:
                        freq_indices[start_v:end_v + 1, slot_index] = freq_idx

        return freq_indices, num_slots

    def _write_freq_indices(
        self,
        logical_hash,
        actual_hash,
        hash_slot_data,
        unique_names,
        vertex_count,
        calculated_ranges,
        merged_index_map=None,
        slot_index_maps=None,
    ):
        freq_indices, _num_slots = self._build_freq_index_table(
            actual_hash,
            hash_slot_data,
            unique_names,
            vertex_count,
            calculated_ranges,
            merged_index_map=merged_index_map,
            slot_index_maps=slot_index_maps,
        )
        if freq_indices is None:
            return

        output_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_freq_indices.buf")
        with open(output_path, "wb") as file_obj:
            file_obj.write(memoryview(freq_indices))

    def _write_sparse_vertex_index(
        self,
        logical_hash,
        actual_hash,
        hash_slot_data,
        unique_names,
        vertex_count,
        calculated_ranges,
        merged_index_map=None,
        slot_index_maps=None,
    ):
        """顶点命中索引（稀疏查找）：写出三份「每顶点只列命中槽位」的缓冲。

        取代稠密 FREQ 表（顶点数 × 槽位数 × 4B）；着色器据此只遍历自己命中的
        形态键，而不是把全部槽位过一遍。
        """
        freq_indices, _num_slots = self._build_freq_index_table(
            actual_hash,
            hash_slot_data,
            unique_names,
            vertex_count,
            calculated_ranges,
            merged_index_map=merged_index_map,
            slot_index_maps=slot_index_maps,
        )
        if freq_indices is None:
            return None

        if merged_index_map is None:
            LOG.warning(
                f"直出形态键 {logical_hash}: 未读到合并映射文件，"
                "顶点命中索引的位移记录下标将全部写 -1（位移不会生效）"
            )

        return write_sparse_vertex_index(self.meshes_dir, actual_hash, freq_indices, merged_index_map)

    # ------------------------------------------------------------------
    # 帧表（序列组加速）：把「每顶点遍历全部槽位」编译成「逐帧位移表插值」
    # ------------------------------------------------------------------

    # 帧表模式下组映射表占用的 cs-t 寄存器起点（t51 留给帧表本身）。
    FRAME_TABLE_GROUP_MAP_REGISTER_BASE = 52
    # 组数上限：52 + 组数 - 1 必须小于 t100（拖拽驱动起点）。
    FRAME_TABLE_MAX_GROUPS = 45

    def _read_sequence_groups_from_ext_node(self, freq_params):
        """直接从「形态键扩展」节点读取序列分组配置。

        为什么不能依赖 ini 里的 ``@@ShapeKeyExt:SYNC@@`` 块：扩展节点运行在形态键
        节点**之后**，导出时 ini 里尚无该块（实机验证：导出前备份的 ini 中
        ``[Present]`` 段为空，只有段头）。节点上的配置才是权威来源。
        """
        try:
            import bpy
        except Exception:
            return {}, {}

        name_to_var = {}
        for name, param in (freq_params or {}).items():
            if param:
                name_to_var[name] = str(param).lstrip("$")

        # 搜索顺序：先本节点所属的节点树（多蓝图共存时最准确），找不到再退回全局
        # 扫描。必须保留全局兜底——形态键节点与扩展节点可能不在同一棵蓝图树里
        # （direct_export 存在跨蓝图取源的情况），只认本树会导致永远读不到分组。
        node_tree = getattr(self.node, "id_data", None)
        search_passes = []
        if node_tree is not None:
            search_passes.append([node_tree])
        search_passes.append(list(bpy.data.node_groups))

        for candidate_trees in search_passes:
            groups, group_totals = self._collect_sequence_groups(candidate_trees, name_to_var)
            if groups:
                return groups, group_totals
        return {}, {}

    @staticmethod
    def _collect_sequence_groups(candidate_trees, name_to_var):
        """在给定节点树集合里收集「序列模式」分组：``({组号: [变量]}, {组号: 完整成员数})``。"""
        groups = {}
        group_totals = {}
        for node_group in candidate_trees:
            for node in getattr(node_group, "nodes", []) or []:
                if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_ShapeKeyExt":
                    continue
                mode_of = {}
                for setting in getattr(node, "play_group_settings", []) or []:
                    mode_of[int(getattr(setting, "group_index", 0) or 0)] = str(
                        getattr(setting, "group_mode", "") or ""
                    )
                if "SEQUENCE" not in mode_of.values():
                    continue

                members = {}
                totals = {}
                for entry in getattr(node, "play_group_entries", []) or []:
                    group_index = int(getattr(entry, "group_index", 0) or 0)
                    key_name = str(getattr(entry, "shape_key_name", "") or "")
                    if group_index <= 0 or not key_name:
                        continue
                    if mode_of.get(group_index) != "SEQUENCE":
                        continue
                    var = name_to_var.get(key_name)
                    # 只统计**能映射到本次导出形态键变量**的成员：扩展节点里可能残留
                    # 历史条目（改名/删除/未导出的键），它们不在 ini 的时间轴 N 里。
                    # ini 的 N 正是「该组在本次导出变量里的成员数」。
                    if not var:
                        continue
                    members.setdefault(group_index, []).append(var)
                    totals[group_index] = totals.get(group_index, 0) + 1

                sort_key = getattr(node, "_natural_sort_key", None)
                for group_index, vars_in_group in members.items():
                    if not vars_in_group:
                        continue
                    if callable(sort_key):
                        vars_in_group.sort(key=sort_key)
                    else:
                        vars_in_group.sort()
                    groups[group_index] = vars_in_group
                group_totals.update(totals)
        return groups, group_totals

    def _parse_sequence_groups_from_ini(self, sections, ini_path=None):
        """从 ini 里解析「形态键扩展」的序列分组。

        返回 ``{组号: [(组内序号, $Freq_变量名), ...]}``（按序列顺序排序）。
        只收「序列模式」的成员——同步模式的赋值是 `$x = $Freq_GroupN`，不带
        `* N - idx`，自然不会被收进来。块不存在时返回空 dict（→ 回退旧模型）。

        ``ini_path`` 给定时直接扫全文，比依赖 ``sections['[Present]']`` 的结构更稳
        （后者受各 postprocess 节点的 tail/driver 切分逻辑影响）。
        """
        lines = None
        if ini_path:
            try:
                with open(ini_path, "r", encoding="utf-8", errors="ignore") as file_obj:
                    lines = file_obj.read().splitlines()
            except Exception:
                lines = None
        if lines is None:
            lines = sections.get('[Present]') or []

        collected = {}
        current = None
        in_sync = False
        for raw in lines:
            text = raw.strip()
            if not in_sync:
                if '形态键扩展配置：组内变量同步' in text:
                    in_sync = True
                continue
            if '结束组内变量同步' in text:
                break
            header = re.match(r';\s*分组(\d+)', text)
            if header:
                current = int(header.group(1))
                collected.setdefault(current, [])
                continue
            member = re.match(r'\$(\w+)\s*=\s*\$Freq_Group\d+\s*\*\s*\d+\s*-\s*(\d+)', text)
            if member and current is not None:
                collected[current].append((int(member.group(2)), member.group(1)))

        groups = {}
        for group_index, items in collected.items():
            if not items:
                continue
            items.sort(key=lambda pair: pair[0])
            groups[group_index] = [var for _order, var in items]
        return groups

    def _build_frame_table_plan(self, unique_names, freq_params, sequence_groups, group_totals=None):
        """把形态键索引编排成帧表分组计划。

        每个分组 = 一条独立的帧序列；未分组的键各自成为「单帧组」（进度就是该键
        自己的强度变量，等价于 N=1 的序列）。返回 None 表示信息不全，应回退旧模型。
        """
        # ini 的 sync 块里成员名不带 '$' 前缀（正则捕的是 \$ 之后的部分），
        # 而 freq_params 的值带 '$'。两边必须规范化后再比对，否则分组会全部落空、
        # 帧表退化成「每个键各自成组」，帧表路径等于整条失效。
        var_to_freq = {}
        for freq_index, name in enumerate(unique_names):
            param = freq_params.get(name)
            if param:
                var_to_freq[str(param).lstrip("$")] = freq_index

        groups = []
        assigned = set()

        for group_index in sorted(sequence_groups):
            members = []
            for var in sequence_groups[group_index]:
                freq_index = var_to_freq.get(var)
                if freq_index is None:
                    continue
                members.append(freq_index)
                assigned.add(freq_index)
            # 该组完全不属于本网格（例：尿道组只在上半身网格里）——跳过，不是错误。
            # 必须先判空再校验，否则 expected 非零而 members 为空会被误判成「跨网格」，
            # 导致整个帧表被放弃。
            if not members:
                continue
            # 时间轴一致性：ini 侧进度是 `$Freq_GroupN * N - idx`（N = 该组在本次导出
            # 变量里的完整成员数）。若本网格只命中其中一部分（分组跨网格、或组内有被
            # 屏蔽/未导出的键），`t * M` 便不再等价于 `t * N`，序列姿态会整体错位且无
            # 报错。这里直接放弃帧表、回退旧模型。
            expected = int((group_totals or {}).get(group_index, 0) or 0)
            if expected and len(members) != expected:
                return None
            groups.append({
                "group": group_index,
                "progress": f"$Freq_Group{group_index}",
                "members": members,
            })

        for freq_index, name in enumerate(unique_names):
            if freq_index in assigned:
                continue
            param = freq_params.get(name)
            if not param:
                return None
            groups.append({"group": None, "progress": param, "members": [freq_index]})

        if not groups:
            return None
        # t 寄存器上限：组映射表从 t52 起连续占用，而 t100/t101（拖拽驱动）与
        # t102（权重缓冲）是固定用途，组数过多会互相覆盖。
        if len(groups) > self.FRAME_TABLE_MAX_GROUPS:
            return None
        return {"groups": groups}

    def _read_frame_table_inputs(self, actual_hash):
        """读回刚写出的稀疏产物（帧表构建零侵入，不改动旧路径的产出）。"""
        data_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_merged_packed_pos_delta.buf")
        map_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_merged_map.buf")
        freq_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_freq_indices.buf")
        for path in (data_path, map_path, freq_path):
            if not os.path.exists(path):
                return None
        raw = np.fromfile(data_path, dtype=np.float32)
        merged_map = np.fromfile(map_path, dtype=np.int32)
        freq_indices = np.fromfile(freq_path, dtype=np.uint32)
        if merged_map.size == 0 or merged_map.size != freq_indices.size:
            return None
        active = int(merged_map.max()) + 1
        if active <= 0 or raw.size % active != 0:
            return None
        per_record = raw.size // active
        if per_record < 3:
            return None
        # 只取每条记录的前 3 个 float：形态键着色器只写回 position。
        return raw.reshape(active, per_record)[:, :3], merged_map, freq_indices

    def _pack_half_float3(self, values):
        """把 (N,3) float32 逐条半精度打包：2 条记录占 3 个 uint32。

        与 Toolset/shapekey_anim_frame_table.hlsl 的 ``frame_table_load`` 严格对齐：
        偶数记录落在 [w0.lo, w0.hi, w1.lo]，奇数记录落在 [w1.hi, w2.lo, w2.hi]。
        """
        try:
            from ..common.shapekey_native import pack_half_float3
        except ImportError:
            pack_half_float3 = None
        if pack_half_float3 is not None:
            packed = pack_half_float3(values)
            if packed is not None:
                return packed
        records = np.ascontiguousarray(values, dtype=np.float32)
        count = records.shape[0]
        if count == 0:
            return np.zeros(0, dtype=np.uint32)
        half_bits = records.astype(np.float16).view(np.uint16).reshape(-1).astype(np.uint32)
        words = np.zeros(((count + 1) // 2) * 3, dtype=np.uint32)

        even = np.arange(0, count, 2)
        words[(even // 2) * 3] = half_bits[even * 3] | (half_bits[even * 3 + 1] << 16)
        words[(even // 2) * 3 + 1] = half_bits[even * 3 + 2]

        odd = np.arange(1, count, 2)
        if odd.size:
            words[(odd // 2) * 3 + 1] |= half_bits[odd * 3] << 16
            words[(odd // 2) * 3 + 2] = half_bits[odd * 3 + 1] | (half_bits[odd * 3 + 2] << 16)
        return words

    def _write_frame_table_files(self, actual_hash, vertex_count, plan, inputs):
        """构建并写出帧表 + 每组顶点局部索引表。

        帧表第 m 条 = 组内前 m 个形态键的累积位移（relative to base，不含 base），
        第 0 条恒为 0——于是「第 K 帧姿态 − base」与累积位移逐位等价。
        """
        delta_data, merged_map, freq_indices = inputs
        slot_count = merged_map.size // vertex_count if vertex_count else 0
        # 必须整除：freq_indices/merged_map 都是 (顶点数 × 槽位数) 展平而来，
        # 不能整除说明读到的产物与当前顶点数不匹配（例如残留的旧文件）。
        if vertex_count <= 0 or merged_map.size % vertex_count != 0 or slot_count <= 0:
            return None
        merged_map = merged_map.reshape(vertex_count, slot_count)
        freq_indices = freq_indices.reshape(vertex_count, slot_count)

        # key_records[freq_idx] = [(顶点数组, 位移载荷), ...]
        # 同一形态键可跨多个槽位出现（各槽位覆盖的顶点集合通常不相交，
        # 相交时按槽位顺序累加；每份 picked 来自唯一递增的 rows，份内无重复顶点）。
        key_records = {}
        for slot_index in range(slot_count):
            key_column = freq_indices[:, slot_index]
            index_column = merged_map[:, slot_index]
            valid = (key_column != NO_FREQ_INDEX) & (index_column >= 0)
            if not valid.any():
                continue
            rows = np.flatnonzero(valid)
            keys = key_column[rows]
            # One stable grouping pass replaces a full-column boolean scan for
            # every key. Ties retain ascending vertex order exactly as before.
            order = np.argsort(keys, kind="stable")
            sorted_keys = keys[order]
            boundaries = np.r_[0, np.flatnonzero(sorted_keys[1:] != sorted_keys[:-1]) + 1, len(keys)]
            for begin, end in zip(boundaries[:-1], boundaries[1:]):
                freq_index = sorted_keys[begin]
                picked = rows[order[begin:end]]
                key_records.setdefault(int(freq_index), []).append(
                    (picked, delta_data[index_column[picked]])
                )

        table_parts = []
        group_meta = []
        offset = 0
        for ordinal, group in enumerate(plan["groups"]):
            members = group["members"]
            key_mask = np.zeros(vertex_count, dtype=bool)
            for freq_index in members:
                for vertices, _payload in key_records.get(int(freq_index), ()):
                    key_mask[vertices] = True
            union_rows = np.flatnonzero(key_mask)
            union_size = int(union_rows.size)
            if union_size == 0:
                continue

            local_index = np.full(vertex_count, -1, dtype=np.int32)
            local_index[union_rows] = np.arange(union_size, dtype=np.int32)

            frames = np.zeros((len(members) + 1, union_size, 3), dtype=np.float32)
            accumulator = np.zeros((union_size, 3), dtype=np.float32)
            for step, freq_index in enumerate(members, start=1):
                for vertices, payload in key_records.get(int(freq_index), ()):
                    # local_index is the already-built exact inverse of union_rows.
                    # Each vertices list is unique; += is equivalent to add.at here,
                    # including sequential accumulation across overlapping slots.
                    positions = local_index[vertices]
                    accumulator[positions] += payload
                frames[step] = accumulator

            # 注意：这里只收集原始记录，**不能**逐段打包。半精度打包把 2 条记录装进
            # 3 个 uint32（每条 1.5 word），逐段打包时奇数条记录会产生半条 padding；
            # 而 shader 是按「全局记录索引」推算 word 位置的（rec * 1.5），于是其后
            # 所有组整体错位 3 个 word —— 实机表现为大范围顶点乱飞。
            table_parts.append(frames.reshape(-1, 3))
            # 序号必须连续：并集为空的组会被跳过，沿用 plan 的原始序号会让
            # shader 里的 frame_group_map_N 与 map_register 错位。
            slot_ordinal = len(group_meta)
            group_meta.append({
                "group": group["group"],
                "progress": group["progress"],
                "ordinal": slot_ordinal,
                "key_count": len(members),
                "union_size": union_size,
                "offset": offset,
                "map_register": self.FRAME_TABLE_GROUP_MAP_REGISTER_BASE + slot_ordinal,
                "local_index": local_index,
            })
            offset += frames.shape[0] * union_size

        if not group_meta:
            return None

        # 全部组的记录拼成一个连续数组后**一次性**打包，保证
        # 「记录索引 → word 索引」的全局 1.5 倍关系处处成立。
        try:
            from ..common.shapekey_native import pack_half_float3_parts
        except ImportError:
            pack_half_float3_parts = None
        packed = pack_half_float3_parts(table_parts) if pack_half_float3_parts is not None else None
        if packed is None:
            all_records = (
                np.concatenate(table_parts, axis=0)
                if table_parts
                else np.zeros((0, 3), dtype=np.float32)
            )
            packed = self._pack_half_float3(all_records)
        table_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_frame_table.buf")
        with open(table_path, "wb") as file_obj:
            file_obj.write(memoryview(packed))

        for meta in group_meta:
            map_path = os.path.join(self.meshes_dir, f"{actual_hash}-Position_group_map_{meta['ordinal']}.buf")
            with open(map_path, "wb") as file_obj:
                file_obj.write(meta["local_index"].tobytes())
            meta["map_path"] = map_path

        return {"groups": group_meta, "slot_count": slot_count}

    # 帧表模板三个注入块的定位（与旧模板共用同一套标记）
    FRAME_SHADER_BLOCK_RE = re.compile(
        r"// --- \[PYTHON-MANAGED BLOCK START\] ---.*?// --- \[PYTHON-MANAGED BLOCK END\] ---",
        re.DOTALL,
    )
    FRAME_SHADER_LOGIC_RE = re.compile(
        r"// --- \[PYTHON-MANAGED LOGIC START\] ---.*?// --- \[PYTHON-MANAGED LOGIC END\] ---",
        re.DOTALL,
    )

    def _build_frame_table_defines(self, key_count, frame_meta, vertex_count=0):
        """帧表模式下的资源声明与分组常量。"""
        lines = [
            "// --- Shared Animation Intensity (per Shape Key Name) ---",
            f"Buffer<float> ShapeKeyWeight : register(t{self.node.WEIGHT_BUFFER_REGISTER});",
            f"#define SHAPEKEY_KEY_COUNT {key_count}u",
            f"#define FRAME_GROUP_COUNT {len(frame_meta)}u",
        ]
        if vertex_count > 0:
            # 用生成期已知的顶点数做边界，避免依赖 StructuredBuffer.Length
            # （本路径未在实机验证过 .Length 的可用性，常量更确定）。
            lines.append(f"#define FRAME_VERTEX_COUNT {int(vertex_count)}u")
        else:
            return []
        for ordinal, meta in enumerate(frame_meta):
            lines.append(
                f"StructuredBuffer<int> frame_group_map_{ordinal} : register(t{meta['map_register']});"
            )
        for ordinal, meta in enumerate(frame_meta):
            lines.append(f"#define GROUP{ordinal}_OFFSET {meta['offset']}u")
            lines.append(f"#define GROUP{ordinal}_UNION {meta['union_size']}u")
            lines.append(f"#define GROUP{ordinal}_KEYS {meta['key_count']}u")
            lines.append(
                f"#define GROUP{ordinal}_PROGRESS ShapeKeyWeight[{key_count + ordinal}] // {meta['progress']}"
            )
        return lines

    def _build_frame_table_logic(self, frame_meta):
        """每组的插值调用；组进度住在权重缓冲尾部，由权重同步 CS 每帧搬运。"""
        lines = ["    // 帧表插值：每组只读「当前帧 / 下一帧」两条顺序记录。"]
        for ordinal, meta in enumerate(frame_meta):
            lines.extend([
                "    {",
                f"        int frame_local_{ordinal} = "
                f"(i < FRAME_VERTEX_COUNT) ? frame_group_map_{ordinal}[i] : -1;",
                # 上界同样要判：映射表若越界（文件过期/组间错配），SRV 读回 0 会被
                # 当成合法的局部索引 0，从而静默串到该组第 0 条记录上。
                f"        if (frame_local_{ordinal} >= 0 && frame_local_{ordinal} < (int)GROUP{ordinal}_UNION)",
                "        {",
                f"            total_diff_position += frame_table_sample("
                f"GROUP{ordinal}_OFFSET, GROUP{ordinal}_UNION, GROUP{ordinal}_KEYS, "
                f"(uint)frame_local_{ordinal}, GROUP{ordinal}_PROGRESS);",
                "        }",
                "    }",
            ])
        return lines

    def _update_frame_table_shader(self, shader_path, template_path, key_count, frame_meta,
                                   hash_val=None, vertex_count=0):
        """把帧表配置注入模板并写到 shader_path。"""
        # vertex_count 为 0 时 FRAME_VERTEX_COUNT 不会被定义，而注入的逻辑会引用它
        # → shader 编译失败。这里直接拒绝，交由调用方回退旧模型。
        if int(vertex_count or 0) <= 0 or not template_path:
            return False
        with open(template_path, "r", encoding="utf-8") as file_obj:
            content = file_obj.read()

        vertex_struct = self.node._get_vertex_struct_definition(hash_val=hash_val)
        if vertex_struct:
            content = re.sub(
                r"struct VertexAttributes\s*\{[^}]*\};",
                vertex_struct,
                content,
                flags=re.DOTALL,
            )

        define_lines = self._build_frame_table_defines(key_count, frame_meta, vertex_count)
        logic_lines = self._build_frame_table_logic(frame_meta)

        content = self.FRAME_SHADER_BLOCK_RE.sub(
            lambda _match: (
                "// --- [PYTHON-MANAGED BLOCK START] ---\n"
                + "\n".join(define_lines)
                + "\n// --- [PYTHON-MANAGED BLOCK END] ---"
            ),
            content,
            count=1,
        )
        content = self.FRAME_SHADER_LOGIC_RE.sub(
            lambda _match: (
                "    // --- [PYTHON-MANAGED LOGIC START] ---\n"
                + "\n".join(logic_lines)
                + "\n    // --- [PYTHON-MANAGED LOGIC END] ---"
            ),
            content,
            count=1,
        )
        write_text_if_changed(shader_path, content)
        return True

    def _compose_slot_bytes(self, logical_hash, runtime_info, slot_index, names_data, slot_position_overrides):
        base_bytes = runtime_info["base_bytes"]
        position_stride = runtime_info["position_stride"]
        slot_bytes = bytearray(base_bytes)

        slot_overrides = slot_position_overrides.get(slot_index, {})
        for objects in names_data.values():
            for obj_name in objects:
                override_entry = slot_overrides.get(obj_name)
                if not override_entry:
                    continue

                obj_hash = self.node._extract_hash_from_name(obj_name)
                obj_prefix = self.node._extract_hash_prefix(obj_hash) if obj_hash else None
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                if obj_prefix != hash_prefix:
                    continue

                export_indices = np.asarray(override_entry.get("export_indices", []), dtype=np.int32)
                position_bytes = override_entry.get("position_bytes", b"")
                if export_indices.size == 0 or not position_bytes:
                    continue

                expected_bytes = export_indices.size * position_stride
                if len(position_bytes) != expected_bytes:
                    raise ShapeKeyDirectExportError(
                        f"物体 '{obj_name}' 的 Position 数据长度异常，期望={expected_bytes}，实际={len(position_bytes)}"
                    )

                try:
                    apply_position_override_in_place(
                        state_bytes=slot_bytes,
                        position_bytes=position_bytes,
                        export_indices=export_indices,
                        position_stride=position_stride,
                    )
                except ValueError as exc:
                    raise ShapeKeyDirectExportError(str(exc)) from exc

        return bytes(slot_bytes)

    def _resolve_drawib_base_position_bytes(
        self, drawib_model, base_path: str, logical_hash: str
    ) -> bytes:
        """取 DrawIB 级基础 Position 字节，与 DrawIB 级 export_indices 同一索引空间。

        优先按子网格顺序拼接 `<unique_str>-Position.buf`（逐子网格写盘的游戏，例如
        EFMI 多 LOD：同一 DrawIB 的 LOD0/LOD1 各有一个文件，而 `export_indices` 是
        DrawIB 级的、第二个子网格的索引从第一个子网格顶点数处开始）——不拼接就会用
        DrawIB 级索引去采样单子网格文件，直接 `IndexError: index N is out of bounds`。

        无法拼接（单个子网格 / 文件缺失 / 步长不一致 / 合并 IB 的游戏）时回退到
        「按哈希解析出的单个文件」，保持既有行为。
        """
        folder_path = os.path.dirname(str(base_path or ""))
        merged_bytes, submesh_count = assemble_drawib_position_bytes(
            folder_path,
            getattr(drawib_model, "submesh_model_list", []) or [],
        )
        if merged_bytes:
            LOG.info(
                f"直出形态键: DrawIB {logical_hash} 基础 Position 由 {submesh_count} 个子网格缓冲"
                f"拼接（{len(merged_bytes)} 字节），与 DrawIB 级顶点索引对齐"
            )
            return merged_bytes

        with open(base_path, "rb") as file_obj:
            return file_obj.read()

    def _build_runtime_infos(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            base_path, actual_hash = self.node._resolve_position_buffer_path(
                self.mod_export_path,
                "Meshes0000",
                logical_hash,
            )
            if not os.path.exists(base_path):
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 文件不存在 {base_path}")
                continue

            drawib_model = self._match_drawib_model(actual_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配基础 DrawIB 模型")
                continue

            base_bytes = self._resolve_drawib_base_position_bytes(
                drawib_model, base_path, logical_hash
            )

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 Position 文件")

        return runtime_infos

    def _build_runtime_infos_from_exporter_buffers(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            base_path, actual_hash = self.node._resolve_position_buffer_path(
                self.mod_export_path,
                "Meshes0000",
                logical_hash,
            )
            if not os.path.exists(base_path):
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 文件不存在 {base_path}")
                continue

            drawib_model = self._match_drawib_model(actual_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配基础 DrawIB 模型")
                continue

            shapekey_buffers = getattr(drawib_model, "shapekey_name_bytelist_dict", {}) or {}
            if not shapekey_buffers:
                continue

            base_bytes = self._resolve_drawib_base_position_bytes(
                drawib_model, base_path, logical_hash
            )

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "shapekey_buffers": shapekey_buffers,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 ShapeKey 数据")

        return runtime_infos

    def _build_runtime_infos_from_exporter_memory(self, unique_hashes):
        runtime_infos = {}
        for logical_hash in unique_hashes:
            drawib_model = self._match_drawib_model(logical_hash, logical_hash)
            if drawib_model is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 无法匹配内存中的 DrawIB 模型")
                continue

            category_buffer_dict = getattr(drawib_model, "category_buffer_dict", {}) or {}
            position_buffer = category_buffer_dict.get("Position")
            if position_buffer is None:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 内存中无 Position 缓冲")
                continue

            base_bytes = _buffer_to_bytes(position_buffer)

            position_stride = self._infer_position_stride(drawib_model, base_bytes)
            vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"直出形态键跳过哈希 {logical_hash}: 基础 Position 顶点数无效")
                continue

            draw_ib = getattr(drawib_model, "draw_ib", logical_hash)
            actual_hash = draw_ib if draw_ib else logical_hash

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": actual_hash,
                "base_path": "",
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": drawib_model,
                "object_export_context_lookup": self._build_drawib_object_context_lookup(drawib_model),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("直出形态键未找到任何可用的基础 Position 数据（内存回退）")

        return runtime_infos

    def _parse_hash_to_base_resources(self, sections):
        return collect_base_position_resource_map(sections, self.node._extract_hash_prefix)

    def _update_ini_sections(
        self,
        sections,
        preserved_tail_content,
        target_ini_file,
        slot_to_name_to_objects,
        unique_hashes,
        hash_to_objects,
        all_unique_names,
        all_unique_objects,
        calculated_ranges,
        hash_to_stride,
        hash_to_actual_file_hash,
        hash_to_vertex_count,
        hash_slot_data_map,
        hash_to_base_resources,
        use_packed,
        use_delta,
        use_optimized,
        merge_slot_files,
        preserved_driver_content="",
        drag_drive_resource=None,
        frame_table_meta_map=None,
        use_sparse_index=False,
    ):
        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        constants_lines = sections['[Constants]']
        constants_content = "".join(constants_lines)
        vars_to_define = set()

        shapekey_freq_params = {}
        for name in all_unique_names:
            shapekey_freq_params[name] = self.node.get_shape_key_export_variable_name(name)

        intensity_lines = []
        for name, param in shapekey_freq_params.items():
            if param not in constants_content:
                intensity_lines.append(f"; 控制形态键 '{name}' 的强度")
                intensity_lines.append(f"global persist {param} = 0.0")

        if intensity_lines:
            constants_lines.append("\n; --- Auto-generated Shape Key Intensity Controls (Additive Blending) ---")
            constants_lines.extend(intensity_lines)

        vertex_range_vars = {}
        if not use_optimized:
            existing_vertex_range_names = set()
            vertex_range_lines = []
            for obj_name, range_tuple in calculated_ranges.items():
                start_v, end_v = range_tuple[:2]
                if start_v is None:
                    continue
                safe_name = self.node._create_safe_var_name(
                    obj_name.replace("-", "_"),
                    existing_names=existing_vertex_range_names,
                )
                start_var = f"$SV_{safe_name}"
                end_var = f"$EV_{safe_name}"
                vertex_range_vars[obj_name] = (start_var, end_var)
                if start_var not in constants_content:
                    vertex_range_lines.append(f"global {start_var} = {start_v}")
                if end_var not in constants_content:
                    vertex_range_lines.append(f"global {end_var} = {end_v}")

            if vertex_range_lines:
                constants_lines.append("\n; --- Auto-generated Vertex Ranges for Shape Keys ---")
                constants_lines.extend(vertex_range_lines)

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            res_to_post = base_resources if base_resources else [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]
            for res_name in res_to_post:
                ensure_resource_alias_section(
                    sections,
                    res_name,
                    "_0",
                    source_candidates=[res_name],
                )
                if f"post {res_name} = copy_desc" not in constants_content:
                    constants_lines.append(f"post {res_name} = copy_desc {res_name}_0")
            if len(base_resources) > 1:
                # 内部基础网格选择器不可复用 MultiFile 的公开动画帧参数。
                vars_to_define.add("$ssmt_sk_base_mesh")
            if f"post run = CustomShader_{logical_hash}_Anim" not in constants_content:
                constants_lines.append(f"post run = CustomShader_{logical_hash}_Anim")

        base_mesh_switch_lines = []
        for var in sorted(vars_to_define):
            if f"global persist {var}" not in constants_content and f"global {var}" not in constants_content:
                base_mesh_switch_lines.append(f"global persist {var} = 1")
        if base_mesh_switch_lines:
            constants_lines.append("\n; --- Auto-generated Base Mesh Switch Key ---")
            constants_lines.extend(base_mesh_switch_lines)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        present_lines = sections['[Present]']
        rebuilt_present_lines = []
        generated_run_lines = {
            f"run = CustomShader_{logical_hash}_Anim"
            for logical_hash in unique_hashes
        } | {
            f"run = CustomShaderShapeKeyWeightSync_{logical_hash}"
            for logical_hash in unique_hashes
        }
        line_index = 0
        while line_index < len(present_lines):
            stripped_line = present_lines[line_index].strip()
            if stripped_line == self._PRESENT_RUN_BEGIN:
                end_index = line_index + 1
                while end_index < len(present_lines):
                    if present_lines[end_index].strip() == self._PRESENT_RUN_END:
                        break
                    end_index += 1
                if end_index < len(present_lines):
                    line_index = end_index + 1
                    continue
            if stripped_line == 'if $active0 == 1':
                end_index = line_index + 1
                inner_lines = []
                while end_index < len(present_lines):
                    candidate_line = present_lines[end_index]
                    candidate_stripped = candidate_line.strip()
                    if candidate_stripped == 'endif':
                        break
                    inner_lines.append(candidate_stripped)
                    end_index += 1

                if (
                    end_index < len(present_lines)
                    and inner_lines
                    and {line for line in inner_lines if line} == generated_run_lines
                ):
                    line_index = end_index + 1
                    continue

            if stripped_line in generated_run_lines:
                line_index += 1
                continue

            rebuilt_present_lines.append(present_lines[line_index])
            line_index += 1

        # 签名变量：本帧所有形态键强度（+ 基础网格切换开关）。没变化就不 dispatch，
        # 拖拽臂动（Alt）期间强制 dispatch —— 拖拽驱动缓冲每帧都在变，签名看不出来。
        signature_vars = sorted({
            str(param).strip() for param in shapekey_freq_params.values() if str(param).strip()
        })
        if "$ssmt_sk_base_mesh" in vars_to_define:
            signature_vars.append("$ssmt_sk_base_mesh")
        rebuilt_present_lines.extend(self._build_present_run_block(
            unique_hashes,
            signature_vars=signature_vars,
            drag_active_var=self._drag_mode_variable_for(drag_drive_resource),
        ))
        sections['[Present]'] = rebuilt_present_lines

        if signature_vars:
            signature_var, prev_var = self._sk_signature_var_names(unique_hashes)
            for declaration in (
                f"global {signature_var} = 0",
                # 初值 -1：第一帧必然与签名不同 → 至少 dispatch 一次
                f"global {prev_var} = -1",
            ):
                if declaration not in constants_lines:
                    constants_lines.append(declaration)

        compute_blocks_to_add = OrderedDict()
        # 每个 hash 的形态键数量：权重缓冲的 array 容量（供资源段使用）
        hash_weight_counts = {}
        for logical_hash in unique_hashes:
            hash_objects = hash_to_objects.get(logical_hash, [])
            hash_slot_data = hash_slot_data_map.get(logical_hash, {})
            if not hash_slot_data:
                continue

            hash_unique_names = list(
                OrderedDict.fromkeys(name for slot_data in hash_slot_data.values() for name in slot_data.keys())
            )
            hash_unique_objects = list(
                OrderedDict.fromkeys(
                    obj
                    for slot_data in hash_slot_data.values()
                    for name_data in slot_data.values()
                    for obj in name_data
                    if obj in hash_objects
                )
            )

            block_name = f"[CustomShader_{logical_hash}_Anim]"
            # 形态键强度不再直读共享表：写进打包中转窗口 → 权重同步 CS 搬进
            # mod 专属权重缓冲 → 窗口立即清零。与 node_postprocess 路径同格式。
            _weight_h_prefix = self.node._extract_hash_prefix(logical_hash)
            _weight_base_resources = hash_to_base_resources.get(_weight_h_prefix, [])
            _weight_primary_base = (
                _weight_base_resources[0]
                if _weight_base_resources
                else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
            )
            weight_resource = derive_shapekey_weight_resource_name(_weight_primary_base)
            # 帧表模式：组进度复用同一条打包窗口，紧跟在形态键强度之后，
            # 于是权重同步 CS 会连进度一起搬进专属缓冲（无需新开通道）。
            frame_meta = (frame_table_meta_map or {}).get(logical_hash)
            frame_groups = frame_meta["groups"] if frame_meta else []
            weight_slots = len(hash_unique_names) + len(frame_groups)
            sync_lines = ["\n    ; --- Shape Key Weight Transfer (packed 4 keys per float4) ---"]
            for index, name in enumerate(hash_unique_names):
                freq_param = shapekey_freq_params.get(name)
                if freq_param:
                    sync_lines.append(
                        f"    {'xyzw'[index % 4]}{self.node.INTENSITY_START_INDEX + index // 4} = {freq_param} \n; {name}"
                    )
            for ordinal, group in enumerate(frame_groups):
                slot = len(hash_unique_names) + ordinal
                sync_lines.append(
                    f"    {'xyzw'[slot % 4]}{self.node.INTENSITY_START_INDEX + slot // 4} = {group['progress']}"
                    f" \n; frame group ordinal {ordinal} (group {group['group']})"
                )
            sync_lines.extend([
                f"    cs = {WEIGHT_SYNC_SHADER_REL}",
                f"    cs-u0 = {weight_resource}",
                f"    Dispatch = {max(1, (weight_slots + 63) // 64)}, 1, 1",
                "    cs-u0 = null",
            ])
            # 搬运后立即清零：共享表里不留形态键值，别的 mod 即使也占用这些槽位，
            # 渲染期读到的也永远是 0。
            for index in range(weight_slots):
                sync_lines.append(
                    f"    {'xyzw'[index % 4]}{self.node.INTENSITY_START_INDEX + index // 4} = 0"
                )

            block_lines = []
            if not use_optimized:
                block_lines.append("\n    ; --- Per-Object Vertex Range Controls ---")
                for index, obj_name in enumerate(hash_unique_objects):
                    if obj_name not in calculated_ranges or calculated_ranges[obj_name][0] is None:
                        continue
                    start_var, end_var = vertex_range_vars.get(obj_name, ("$SV_unknown", "$EV_unknown"))
                    block_lines.append(f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2} = {start_var} \n; {obj_name} Start")
                    block_lines.append(f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2 + 1} = {end_var} \n; {obj_name} End")

            t_registers_to_null = []
            slots_for_hash = sorted(hash_slot_data.keys())
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
            if not use_delta:
                block_lines.append(f"\n    cs-t50 = copy {derive_shapekey_base_resource_name(primary_base_resource)}")
                t_registers_to_null.append("cs-t50")

            mode_str = (
                f"紧凑:{'是' if use_packed else '否'}, "
                f"增量:{self.node._describe_delta_scope(use_delta)}, "
                f"优化查找:{'是' if use_optimized else '否'}, "
                f"文件合并:{'是' if merge_slot_files else '否'}, "
                f"顶点命中索引:{'是' if use_sparse_index else '否'}"
            )
            block_lines.append(f"\n    ; --- Binding Shape Key Meshess (Mode: {mode_str}) ---")
            if frame_meta:
                # 帧表模式：t51 给帧表，t52+ 给各组顶点局部索引表；
                # 不再需要 merged delta / map / freq_indices（逐槽位遍历已被取代）。
                block_lines.append("\n    ; --- Frame Table (per-group frame interpolation) ---")
                block_lines.append(
                    f"    cs-t51 = copy {derive_shapekey_frame_table_resource_name(primary_base_resource)}"
                )
                t_registers_to_null.append("cs-t51")
                for ordinal, group in enumerate(frame_groups):
                    register = group["map_register"]
                    block_lines.append(
                        f"    cs-t{register} = copy "
                        f"{derive_shapekey_group_map_resource_name(primary_base_resource, ordinal)}"
                    )
                    t_registers_to_null.append(f"cs-t{register}")
            elif merge_slot_files:
                block_lines.append(f"    cs-t51 = copy {derive_shapekey_merged_data_resource_name(primary_base_resource, use_delta)}")
                block_lines.append(f"    cs-t52 = copy {derive_shapekey_merged_map_resource_name(primary_base_resource)}")
                t_registers_to_null.extend(["cs-t51", "cs-t52"])

                if use_sparse_index:
                    # 稀疏模式：三份顶点命中索引取代稠密 FREQ 表（t53 不再绑定）。
                    for register, resource_name in (
                        (SPARSE_START_REGISTER, derive_shapekey_vertex_entry_start_resource_name(primary_base_resource)),
                        (SPARSE_PACKED_REGISTER, derive_shapekey_vertex_entry_packed_resource_name(primary_base_resource)),
                        (SPARSE_FREQ_REGISTER, derive_shapekey_vertex_entry_freq_resource_name(primary_base_resource)),
                    ):
                        block_lines.append(f"    cs-t{register} = copy {resource_name}")
                        t_registers_to_null.append(f"cs-t{register}")
                elif use_optimized:
                    block_lines.append(f"    cs-t53 = copy {derive_shapekey_freq_resource_name(primary_base_resource)}")
                    t_registers_to_null.append("cs-t53")
            else:
                res_suffix = "_packed_pos_delta" if use_packed and use_delta else "_pos_delta" if use_delta else "_packed" if use_packed else ""
                for slot_num in slots_for_hash:
                    res_name = derive_shapekey_slot_resource_name(primary_base_resource, slot_num, res_suffix if (use_packed or use_delta) else "")

                    t_reg = 51 + slot_num - 1
                    block_lines.append(f"    cs-t{t_reg} = copy {res_name}")
                    t_registers_to_null.append(f"cs-t{t_reg}")
                    if use_packed:
                        map_reg = 75 + slot_num - 1
                        block_lines.append(
                            f"    cs-t{map_reg} = copy {derive_shapekey_slot_map_resource_name(primary_base_resource, slot_num)}"
                        )
                        t_registers_to_null.append(f"cs-t{map_reg}")

                if use_optimized:
                    block_lines.append(f"    cs-t99 = copy {derive_shapekey_freq_resource_name(primary_base_resource)}")
                    t_registers_to_null.append("cs-t99")

            if drag_drive_resource:
                block_lines.append("\n    ; --- Drag ShapeKey Drive ---")
                block_lines.append(f"    cs-t{self.node.DRAG_DRIVE_REGISTER} = {drag_drive_resource}")
                click_resource = self.node._drag_shapekey_click_count_resource_name(target_ini_file)
                if click_resource:
                    block_lines.append(f"    cs-t{self.node.DRAG_CLICK_COUNT_REGISTER} = {click_resource}")
                t_registers_to_null.append(f"cs-t{self.node.DRAG_DRIVE_REGISTER}")
                t_registers_to_null.append(f"cs-t{self.node.DRAG_CLICK_COUNT_REGISTER}")

            # 权重缓冲：mod 专属资源，由 CustomShaderShapeKeyWeightSync 每帧搬运
            block_lines.append("\n    ; --- Shape Key Weight Buffer ---")
            block_lines.append(f"    cs-t{self.node.WEIGHT_BUFFER_REGISTER} = {weight_resource}")
            t_registers_to_null.append(f"cs-t{self.node.WEIGHT_BUFFER_REGISTER}")

            block_lines.append(f"    cs = ./res/shapekey_anim_{logical_hash}.hlsl")
            res_to_bind = base_resources if base_resources else [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]
            if len(res_to_bind) > 1:
                block_lines.append(f"\n    ; --- Base Mesh Switching ---")
                for index, res_name in enumerate(res_to_bind, 1):
                    ensure_resource_alias_section(
                        sections,
                        res_name,
                        "_0",
                        source_candidates=[res_name],
                    )
                    block_lines.extend([f"    if $ssmt_sk_base_mesh == {index}", f"        cs-u5 = copy {res_name}_0", f"        {res_name} = ref cs-u5", "    endif"])
            else:
                res_name = res_to_bind[0]
                ensure_resource_alias_section(
                    sections,
                    res_name,
                    "_0",
                    source_candidates=[res_name],
                )
                block_lines.extend([f"    cs-u5 = copy {res_name}_0", f"    {res_name} = ref cs-u5"])

            dispatch_count = self.node._compute_dispatch_group_count(
                hash_to_vertex_count.get(hash_prefix, 0),
                threads_per_group=16,
            )
            block_lines.extend([f"    Dispatch = {dispatch_count}, 1, 1", "    cs-u5 = null", *[f"    {reg} = null" for reg in sorted(list(set(t_registers_to_null)))]] )
            compute_blocks_to_add[block_name] = block_lines
            # 权重搬运段：每帧「写打包窗口 → 搬运 → 清零」，必须排在 Anim 之前
            compute_blocks_to_add[f"[CustomShaderShapeKeyWeightSync_{logical_hash}]"] = sync_lines
            hash_weight_counts[logical_hash] = weight_slots

            # 登记本 hash 刚生成的两类段（裸段名，不含方括号），让「条件判断」
            # 后处理能按 draw_ib 把形态键段一起门控。登记表是追加语义且自动去重，
            # 不同分支重复调用安全；登记失败只静默跳过，绝不能影响导出结果。
            if _section_registry is not None:
                try:
                    for _register_name in (
                        f"CustomShader_{logical_hash}_Anim",
                        f"CustomShaderShapeKeyWeightSync_{logical_hash}",
                    ):
                        _section_registry.register_section(
                            _register_name,
                            draw_ib=logical_hash.split("-")[0],
                            ini_path=os.path.abspath(target_ini_file),
                        )
                except Exception:
                    pass

        new_resource_lines = []
        generated_section_names = set()

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
            base_resources = hash_to_base_resources.get(hash_prefix, [])
            primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
            section_name = f"[{derive_shapekey_base_resource_name(primary_base_resource)}]"
            if section_name not in sections and section_name not in generated_section_names:
                stride = hash_to_stride.get(hash_prefix, 40)
                new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = Meshes0000/{actual_file_hash}-Position.buf", ""])
                generated_section_names.add(section_name)

            # 形态键权重缓冲：mod 专属 RWBuffer（资源名即命名空间），由
            # CustomShaderShapeKeyWeightSync 每帧写入。不给 filename 即零初始化。
            # 容量 = 形态键数 + 组进度数（帧表模式下组进度住在尾部）。
            weight_section_name = f"[{derive_shapekey_weight_resource_name(primary_base_resource)}]"
            if weight_section_name not in sections and weight_section_name not in generated_section_names:
                weight_capacity = max(1, int(hash_weight_counts.get(logical_hash, 1)))
                new_resource_lines.extend([
                    weight_section_name,
                    "type = RWBuffer",
                    "format = R32_FLOAT",
                    f"array = {weight_capacity}",
                    "",
                ])
                generated_section_names.add(weight_section_name)

            # 帧表 + 各组顶点局部索引表（帧表模式专用）
            frame_meta = (frame_table_meta_map or {}).get(logical_hash)
            if frame_meta:
                table_section = f"[{derive_shapekey_frame_table_resource_name(primary_base_resource)}]"
                if table_section not in sections and table_section not in generated_section_names:
                    new_resource_lines.extend([
                        table_section,
                        "type = Buffer",
                        "stride = 4",
                        f"filename = Meshes0000/{actual_file_hash}-Position_frame_table.buf",
                        "",
                    ])
                    generated_section_names.add(table_section)
                for ordinal, _group in enumerate(frame_meta["groups"]):
                    map_section = f"[{derive_shapekey_group_map_resource_name(primary_base_resource, ordinal)}]"
                    if map_section not in sections and map_section not in generated_section_names:
                        new_resource_lines.extend([
                            map_section,
                            "type = Buffer",
                            "stride = 4",
                            f"filename = Meshes0000/{actual_file_hash}-Position_group_map_{ordinal}.buf",
                            "",
                        ])
                        generated_section_names.add(map_section)

        if merge_slot_files:
            for logical_hash in unique_hashes:
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                if not hash_prefix:
                    continue

                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                base_stride = hash_to_stride.get(hash_prefix, 40)
                data_stride = self._delta_stride_for(logical_hash, base_stride) if use_delta else base_stride
                base_resources = hash_to_base_resources.get(hash_prefix, [])
                primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                # 帧表模式：shader 只读帧表与组映射，不再读 merged delta/map。
                # 即使不绑定，光声明 [Resource] + filename 也会让 3DMigoto 把它加载进来，
                # 白白吃掉上百 MB 的磁盘与显存（实机：帧表模式下这两项约 190 MB 纯冗余）。
                if (frame_table_meta_map or {}).get(logical_hash) is None:
                    data_section = f"[{derive_shapekey_merged_data_resource_name(primary_base_resource, use_delta)}]"
                    data_filename = f"Meshes0000/{actual_file_hash}-Position{self.node._get_merged_data_file_suffix(use_delta)}.buf"
                    if data_section not in sections and data_section not in generated_section_names:
                        new_resource_lines.extend([data_section, "type = Buffer", f"stride = {data_stride}", f"filename = {data_filename}", ""])
                        generated_section_names.add(data_section)

                    map_section = f"[{derive_shapekey_merged_map_resource_name(primary_base_resource)}]"
                    map_filename = f"Meshes0000/{actual_file_hash}-Position_merged_map.buf"
                    if map_section not in sections and map_section not in generated_section_names:
                        new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = {map_filename}", ""])
                        generated_section_names.add(map_section)
        else:
            for slot_num, name_data in slot_to_name_to_objects.items():
                for obj_name in [obj for _, objects in name_data.items() for obj in objects]:
                    logical_hash = self.node._extract_hash_from_name(obj_name)
                    hash_prefix = self.node._extract_hash_prefix(logical_hash) if logical_hash else None
                    if not hash_prefix:
                        continue
                    actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                    base_stride = hash_to_stride.get(hash_prefix, 40)
                    base_resources = hash_to_base_resources.get(hash_prefix, [])
                    primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                    if use_delta:
                        res_suffix = "_packed_pos_delta" if use_packed else "_pos_delta"
                        stride = self._delta_stride_for(logical_hash, base_stride)
                    elif use_packed:
                        res_suffix = "_packed"
                        stride = base_stride
                    else:
                        res_suffix = ""
                        stride = base_stride

                    if use_delta or use_packed:
                        section_name = f"[{derive_shapekey_slot_resource_name(primary_base_resource, slot_num, res_suffix)}]"
                        filename = f"Meshes0000/{actual_file_hash}-Position1{slot_num:03d}{res_suffix}.buf"
                        if section_name not in sections and section_name not in generated_section_names:
                            new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = {filename}", ""])
                            generated_section_names.add(section_name)

                    if use_packed:
                        map_section = f"[{derive_shapekey_slot_map_resource_name(primary_base_resource, slot_num)}]"
                        if map_section not in sections and map_section not in generated_section_names:
                            new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position1{slot_num:03d}_map.buf", ""])
                            generated_section_names.add(map_section)

        if use_optimized:
            for logical_hash in unique_hashes:
                # 帧表模式不声明 freq_indices（同上：只声明也会被加载）。
                if (frame_table_meta_map or {}).get(logical_hash) is not None:
                    continue
                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                base_resources = hash_to_base_resources.get(hash_prefix, [])
                primary_base_resource = base_resources[0] if base_resources else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                if use_sparse_index:
                    # 稀疏模式：三份顶点命中索引取代稠密 FREQ 表。旧 ini 里的稠密段
                    # 必须显式移除（与帧表同理：sections 是从旧 ini 读来的，只「不新增」
                    # 会被原样写回，而 3DMigoto 见到 [Resource] + filename 就会加载）。
                    for resource_name, suffix in (
                        (derive_shapekey_vertex_entry_start_resource_name(primary_base_resource), SPARSE_START_SUFFIX),
                        (derive_shapekey_vertex_entry_packed_resource_name(primary_base_resource), SPARSE_PACKED_SUFFIX),
                        (derive_shapekey_vertex_entry_freq_resource_name(primary_base_resource), SPARSE_FREQ_SUFFIX),
                    ):
                        section_name = f"[{resource_name}]"
                        if section_name not in sections and section_name not in generated_section_names:
                            new_resource_lines.extend(
                                [section_name, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position{suffix}.buf", ""]
                            )
                            generated_section_names.add(section_name)
                    sections.pop(f"[{derive_shapekey_freq_resource_name(primary_base_resource)}]", None)
                    continue

                freq_idx_section = f"[{derive_shapekey_freq_resource_name(primary_base_resource)}]"
                if freq_idx_section not in sections and freq_idx_section not in generated_section_names:
                    new_resource_lines.extend([freq_idx_section, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position_freq_indices.buf", ""])
                    generated_section_names.add(freq_idx_section)

        # 未开启稀疏查找时：移除旧 ini 里残留的顶点命中索引段（同上，避免被加载）。
        if use_optimized and not use_sparse_index:
            for logical_hash in unique_hashes:
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                base_resources = hash_to_base_resources.get(hash_prefix, [])
                primary_base_resource = (
                    base_resources[0]
                    if base_resources
                    else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                )
                for stale_section in (
                    f"[{derive_shapekey_vertex_entry_start_resource_name(primary_base_resource)}]",
                    f"[{derive_shapekey_vertex_entry_packed_resource_name(primary_base_resource)}]",
                    f"[{derive_shapekey_vertex_entry_freq_resource_name(primary_base_resource)}]",
                ):
                    sections.pop(stale_section, None)

        # 帧表模式：显式移除旧 ini 里残留的 merged / freq_indices 资源段。
        # 只「不新增」不够 —— sections 是从旧 ini 读来的，旧段落会被原样写回，而
        # 3DMigoto 只要看到 [Resource] + filename 就会加载（实机约 190 MB 纯冗余）。
        if frame_table_meta_map:
            for logical_hash in unique_hashes:
                if frame_table_meta_map.get(logical_hash) is None:
                    continue
                hash_prefix = self.node._extract_hash_prefix(logical_hash)
                base_resources = hash_to_base_resources.get(hash_prefix, [])
                primary_base_resource = (
                    base_resources[0]
                    if base_resources
                    else f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"
                )
                for stale_section in (
                    f"[{derive_shapekey_merged_data_resource_name(primary_base_resource, use_delta)}]",
                    f"[{derive_shapekey_merged_map_resource_name(primary_base_resource)}]",
                    f"[{derive_shapekey_freq_resource_name(primary_base_resource)}]",
                ):
                    sections.pop(stale_section, None)

        if new_resource_lines:
            sections[";; --- Generated Shape Key Meshess ---"] = new_resource_lines

        for logical_hash in unique_hashes:
            hash_prefix = self.node._extract_hash_prefix(logical_hash)
            for res_name in hash_to_base_resources.get(hash_prefix, [f"Resource_{self.node._hash_to_resource_prefix(logical_hash)}_Position"]):
                ensure_resource_alias_section(
                    sections,
                    res_name,
                    "_0",
                    source_candidates=[res_name],
                )

        sections.update(compute_blocks_to_add)
        self.node._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
