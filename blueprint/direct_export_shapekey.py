import glob
import os
import shutil
import copy
from collections import OrderedDict
from time import perf_counter

import bpy

from ..utils.log_utils import LOG
from ..utils.shapekey_utils import ShapeKeyUtils
from ..common.safe_write import write_text_if_changed
from .direct_export_runtime_utils import normalize_runtime_name as _normalize_runtime_name
from .direct_export_shapekey_output_mixin import DirectShapeKeyOutputMixin
from .direct_export_shapekey_output_mixin import WEIGHT_SYNC_SHADER_NAME
from .direct_export_shapekey_runtime_mixin import DirectShapeKeyRuntimeMixin
from .direct_export_shapekey_sampling_mixin import DirectShapeKeySamplingMixin
from .direct_export_shapekey_shared import ShapeKeyDirectExportError, resolve_use_delta
from .export_helper import BlueprintExportHelper


class DirectShapeKeyGenerator(
    DirectShapeKeyOutputMixin,
    DirectShapeKeySamplingMixin,
    DirectShapeKeyRuntimeMixin,
):
    def __init__(self, node, mod_export_path: str, blueprint_model, exporter):
        self.node = node
        self.mod_export_path = mod_export_path
        self.blueprint_model = blueprint_model
        self.exporter = exporter
        # 直出始终以 Meshes0000 作为基底，再派生各个形态键槽位资源。
        self.meshes_dir = os.path.join(mod_export_path, "Meshes0000")
        self.merged_name_members = {}
        self._processing_chain_alias_lookup = None

    def _iter_name_variants(self, name: str):
        if not name:
            return

        candidate_names = [
            name,
            _normalize_runtime_name(name),
            self._extract_original_name(name),
            self._extract_original_name(_normalize_runtime_name(name)),
        ]

        seen = set()
        for candidate_name in candidate_names:
            if candidate_name and candidate_name not in seen:
                seen.add(candidate_name)
                yield candidate_name

    def _iter_chain_aliases(self, chain):
        raw_names = [
            getattr(chain, "object_name", "") or "",
            getattr(chain, "original_object_name", "") or "",
            getattr(chain, "virtual_object_name", "") or "",
            getattr(chain, "export_object_name_override", "") or "",
        ]

        get_export_object_name = getattr(chain, "get_export_object_name", None)
        if callable(get_export_object_name):
            try:
                raw_names.append(get_export_object_name() or "")
            except Exception:
                pass

        for rename_record in getattr(chain, "rename_history", []) or []:
            raw_names.append(rename_record.get("old_name", "") or "")
            raw_names.append(rename_record.get("new_name", "") or "")

        seen = set()
        for raw_name in raw_names:
            for candidate_name in self._iter_name_variants(raw_name):
                if candidate_name and candidate_name not in seen:
                    seen.add(candidate_name)
                    yield candidate_name

    def _resolve_chain_source_name(self, chain) -> str:
        preferred_names = []
        preferred_names.append(getattr(chain, "original_object_name", "") or "")
        preferred_names.append(getattr(chain, "object_name", "") or "")
        preferred_names.append(getattr(chain, "virtual_object_name", "") or "")
        preferred_names.append(getattr(chain, "export_object_name_override", "") or "")

        get_export_object_name = getattr(chain, "get_export_object_name", None)
        if callable(get_export_object_name):
            try:
                preferred_names.append(get_export_object_name() or "")
            except Exception:
                pass

        for rename_record in getattr(chain, "rename_history", []) or []:
            preferred_names.append(rename_record.get("old_name", "") or "")
            preferred_names.append(rename_record.get("new_name", "") or "")

        fallback_name = ""
        for candidate_name in preferred_names:
            for variant_name in self._iter_name_variants(candidate_name):
                if not fallback_name:
                    fallback_name = variant_name

                source_obj = bpy.data.objects.get(variant_name)
                if source_obj is None:
                    continue

                if ShapeKeyUtils.has_exportable_shape_keys(source_obj):
                    return variant_name

        return fallback_name

    def _get_processing_chain_alias_lookup(self):
        if self._processing_chain_alias_lookup is not None:
            return self._processing_chain_alias_lookup

        alias_lookup = {}
        for chain in getattr(self.blueprint_model, "processing_chains", []) or []:
            if not getattr(chain, "is_valid", False) or not getattr(chain, "reached_output", False):
                continue

            alias_names = list(self._iter_chain_aliases(chain))
            if not alias_names:
                continue

            for alias_name in alias_names:
                related_names = alias_lookup.setdefault(alias_name, [])
                for related_name in alias_names:
                    if related_name not in related_names:
                        related_names.append(related_name)

        self._processing_chain_alias_lookup = alias_lookup
        return alias_lookup

    def _iter_related_runtime_names(self, obj_name: str):
        alias_lookup = self._get_processing_chain_alias_lookup()
        related_names = []
        seen = set()

        def append_name(candidate_name: str):
            if candidate_name and candidate_name not in seen:
                seen.add(candidate_name)
                related_names.append(candidate_name)

        for candidate_name in self._iter_name_variants(obj_name):
            append_name(candidate_name)

        for candidate_name in list(related_names):
            for related_name in alias_lookup.get(candidate_name, []):
                for variant_name in self._iter_name_variants(related_name):
                    append_name(variant_name)

        return related_names

    def generate(self):
        stage_start = perf_counter()
        classification_text_obj = None
        if self.blueprint_model is not None:
            BlueprintExportHelper.generate_shapekey_classification_report(self.blueprint_model)
        classification_text_obj = next((t for t in bpy.data.texts if "Shape_Key_Classification" in t.name), None)

        if not classification_text_obj:
            raise ShapeKeyDirectExportError("未找到 Shape_Key_Classification 文本，无法执行形态键直出。")

        ini_files = glob.glob(os.path.join(self.mod_export_path, "*.ini"))
        if not ini_files:
            raise ShapeKeyDirectExportError("路径中未找到任何 ini 文件，无法执行形态键直出。")

        target_ini_file = ini_files[0]
        self.node._create_cumulative_backup(target_ini_file, self.mod_export_path)
        sections, preserved_tail_content, preserved_driver_content = self.node._read_ini_to_ordered_dict(target_ini_file)

        slot_to_name_to_objects, unique_hashes, hash_to_objects, all_objects = self.node._parse_classification_text_final(
            classification_text_obj.as_string()
        )
        if not slot_to_name_to_objects:
            raise ShapeKeyDirectExportError("分类文本解析失败或为空，无法执行形态键直出。")

        slot_to_name_to_objects = copy.deepcopy(slot_to_name_to_objects)
        hash_to_objects = {hash_value: list(objects) for hash_value, objects in hash_to_objects.items()}
        all_objects = list(all_objects)
        LOG.info(
            "Direct ShapeKey: parsed classification "
            f"slots={sorted(slot_to_name_to_objects.keys())}, hashes={unique_hashes}, object_count={len(all_objects)}"
        )
        for slot_index, names_data in sorted(slot_to_name_to_objects.items()):
            LOG.info(f"Direct ShapeKey: slot {slot_index} classification -> {dict(names_data)}")

        shader_source_path = self.node._get_shader_source_path()
        if not shader_source_path or not os.path.exists(shader_source_path):
            raise ShapeKeyDirectExportError(f"着色器模板文件未找到: {shader_source_path}")

        source_object_map = self._build_source_object_map()
        static_copy_map = {}

        try:
            use_preprocess_records = bool(BlueprintExportHelper.get_direct_shapekey_position_records())
            if use_preprocess_records and bool(getattr(self.node, "store_all_vertex_channels", False)):
                # 前处理记录只存顶点坐标（preprocess.py 只 foreach_get("co")），
                # 合成槽位时也只覆写 POSITION 元素字节 —— 法线/切线会保持基础网格值，
                # 于是全通道增量的法线/切线部分是 0。这里必须显式说出来，
                # 否则用户在游戏里只会看到"开关没反应"。
                LOG.warning(
                    "直出形态键: 已开启「存储全部顶点属性增量」，但本次走的是前处理记录路径——"
                    "该路径只记录顶点坐标，法线/切线增量将为 0（描边/光照不会跟随形变）。"
                    "需要法线/切线增量时请改用 exporter 缓冲路径（关闭前处理记录采集）。"
                )
            if use_preprocess_records:
                runtime_infos = self._build_runtime_infos(unique_hashes)
            else:
                runtime_infos = self._build_runtime_infos_from_exporter_buffers(unique_hashes)
            LOG.info(f"Direct ShapeKey: runtime infos hashes -> {list(runtime_infos.keys())}")
            calculated_ranges = self._calculate_object_ranges(runtime_infos, all_objects, sections=sections)
            LOG.info(f"Direct ShapeKey: calculated ranges -> {calculated_ranges}")
            unique_hashes = self._apply_merged_ranges(
                calculated_ranges,
                slot_to_name_to_objects,
                hash_to_objects,
                all_objects,
            )
            LOG.info(
                "Direct ShapeKey: merged ranges "
                f"hashes={unique_hashes}, objects={all_objects}, merged_members={self.merged_name_members}"
            )
            if use_preprocess_records:
                slot_position_overrides = self._build_slot_position_overrides_from_preprocess_records(
                    slot_to_name_to_objects=slot_to_name_to_objects,
                    calculated_ranges=calculated_ranges,
                    runtime_infos=runtime_infos,
                    source_object_map=source_object_map,
                )
            else:
                slot_position_overrides = self._build_slot_position_overrides_from_exporter_buffers(
                    slot_to_name_to_objects=slot_to_name_to_objects,
                    calculated_ranges=calculated_ranges,
                    runtime_infos=runtime_infos,
                )
            LOG.info(
                "Direct ShapeKey: slot position overrides -> "
                f"{ {slot_index: sorted(slot_data.keys()) for slot_index, slot_data in slot_position_overrides.items()} }"
            )
            raw_hash_slot_data_map, hash_slot_data_map, dropped_slots = self._analyze_hash_slot_filters(
                unique_hashes=unique_hashes,
                slot_to_name_to_objects=slot_to_name_to_objects,
                slot_position_overrides=slot_position_overrides,
            )
            if dropped_slots and not use_preprocess_records:
                LOG.info(
                    "直出形态键: exporter ShapeKey 缓冲路径丢失槽位，"
                    f"改用静态副本补齐: {dropped_slots}"
                )
                slot_position_overrides = self._supplement_dropped_slots_from_static_sampling(
                    unique_hashes=unique_hashes,
                    slot_to_name_to_objects=slot_to_name_to_objects,
                    calculated_ranges=calculated_ranges,
                    source_object_map=source_object_map,
                    runtime_infos=runtime_infos,
                    slot_position_overrides=slot_position_overrides,
                )
                # 补齐槽位后 overrides 已变化，filtered 需按新 overrides 重算；
                # raw 与 overrides 无关，可直接复用上面 _analyze_hash_slot_filters 的结果。
                hash_slot_data_map = self._filter_hash_slot_data_map_by_overrides(
                    raw_hash_slot_data_map,
                    slot_position_overrides,
                )
            if dropped_slots and use_preprocess_records:
                LOG.warning(f"Direct ShapeKey: missing preprocess-record slots {dropped_slots}")
            LOG.info("Direct ShapeKey: using preprocess-record path" if use_preprocess_records else "直出形态键: 使用 exporter ShapeKey 缓冲路径")
        except ShapeKeyDirectExportError as exc:
            LOG.info(
                f"直出形态键: exporter ShapeKey 缓冲不可用，回退到静态副本采样路径 "
                f"({perf_counter() - stage_start:.3f}s) - {exc}"
            )
            runtime_infos = self._build_runtime_infos_from_exporter_memory(unique_hashes)
            calculated_ranges = self._calculate_object_ranges(runtime_infos, all_objects, sections=sections)
            unique_hashes = self._apply_merged_ranges(
                calculated_ranges,
                slot_to_name_to_objects,
                hash_to_objects,
                all_objects,
            )
            LOG.info(
                "Direct ShapeKey: merged ranges "
                f"hashes={unique_hashes}, objects={all_objects}, merged_members={self.merged_name_members}"
            )
            LOG.info(f"直出形态键: 构建 runtime infos + ranges {perf_counter() - stage_start:.3f}s")
            static_copy_map = self._create_static_shapekey_copies(slot_to_name_to_objects, source_object_map, runtime_infos)
            LOG.info(f"直出形态键: 创建静态副本 {perf_counter() - stage_start:.3f}s")
            slot_position_overrides = self._build_slot_position_overrides(
                slot_to_name_to_objects=slot_to_name_to_objects,
                calculated_ranges=calculated_ranges,
                runtime_infos=runtime_infos,
                source_object_map=source_object_map,
                static_copy_map=static_copy_map,
            )
            # 回退路径没有走过上面的 _analyze_hash_slot_filters，这里补算一次。
            raw_hash_slot_data_map = self._build_hash_slot_data_map(unique_hashes, slot_to_name_to_objects)
            hash_slot_data_map = self._filter_hash_slot_data_map_by_overrides(
                raw_hash_slot_data_map,
                slot_position_overrides,
            )
            LOG.info("直出形态键: 已回退到静态副本采样路径")
            LOG.info(f"直出形态键: 槽位 Position 采样完成 {perf_counter() - stage_start:.3f}s")

        for logical_hash in unique_hashes:
            raw_slot_data = raw_hash_slot_data_map.get(logical_hash, {})
            filtered_slot_data = hash_slot_data_map.get(logical_hash, {})
            raw_object_count = sum(
                len(objects)
                for slot_data in raw_slot_data.values()
                for objects in slot_data.values()
            )
            filtered_object_count = sum(
                len(objects)
                for slot_data in filtered_slot_data.values()
                for objects in slot_data.values()
            )
            LOG.info(
                f"直出形态键: 哈希 {logical_hash} 原始对象={raw_object_count}, "
                f"有效对象={filtered_object_count}, 原始槽位={sorted(raw_slot_data.keys())}, "
                f"有效槽位={sorted(filtered_slot_data.keys())}"
            )
        LOG.info(f"直出形态键: hash/slot 筛选完成 {perf_counter() - stage_start:.3f}s")
        hash_to_stride = {}
        hash_to_actual_file_hash = {}
        hash_to_vertex_count = {}
        hash_to_slot_maps = {}
        hash_to_merged_index_map = {}

        for logical_hash in unique_hashes:
            runtime_info = runtime_infos.get(logical_hash)
            hash_slot_data = hash_slot_data_map.get(logical_hash, {})
            if not runtime_info or not hash_slot_data:
                continue

            logical_prefix = self.node._extract_hash_prefix(logical_hash)
            actual_hash = runtime_info["actual_hash"]

            hash_to_actual_file_hash[logical_hash] = actual_hash
            if logical_prefix:
                hash_to_stride[logical_prefix] = runtime_info["position_stride"]
                hash_to_vertex_count[logical_prefix] = runtime_info["vertex_count"]

            if self.node._should_merge_slot_files(getattr(self.node, "use_packed_Meshess", False)):
                merged_index_map = self._write_merged_slot_files(
                    logical_hash=logical_hash,
                    runtime_info=runtime_info,
                    hash_slot_data=hash_slot_data,
                    slot_position_overrides=slot_position_overrides,
                )
                hash_to_merged_index_map[logical_hash] = merged_index_map
            else:
                slot_maps = self._write_slot_files(
                    logical_hash=logical_hash,
                    runtime_info=runtime_info,
                    hash_slot_data=hash_slot_data,
                    slot_position_overrides=slot_position_overrides,
                )
                hash_to_slot_maps[logical_hash] = slot_maps
        LOG.info(f"直出形态键: Position 缓冲写出完成 {perf_counter() - stage_start:.3f}s")

        processed_hashes = [logical_hash for logical_hash in unique_hashes if logical_hash in hash_to_actual_file_hash]
        LOG.info(f"Direct ShapeKey: processed hashes -> {processed_hashes}")
        hash_to_base_resources = self._parse_hash_to_base_resources(sections)
        all_unique_names = list(
            OrderedDict.fromkeys(name for slot_data in slot_to_name_to_objects.values() for name in slot_data.keys())
        )
        all_unique_objects = list(
            OrderedDict.fromkeys(
                obj
                for slot_data in slot_to_name_to_objects.values()
                for name_data in slot_data.values()
                for obj in name_data
            )
        )

        dest_res_dir = os.path.join(self.mod_export_path, "res")
        os.makedirs(dest_res_dir, exist_ok=True)

        use_packed = self.node.use_packed_Meshess
        use_delta = resolve_use_delta(self.node)
        use_optimized = self.node.use_optimized_lookup
        merge_slot_files = self.node._should_merge_slot_files(use_packed)
        drag_drive_enabled = bool(getattr(self.node, "drag_drive_enabled", False))
        drag_drive_resource = None
        if drag_drive_enabled:
            drag_drive_resource = self.node._drag_shapekey_drive_resource_name(target_ini_file)
            if not drag_drive_resource:
                LOG.warning(
                    "直出形态键: 已开启拖拽驱动但未找到启用了驱动输出的拖拽节点，"
                    "本次导出回退到强度变量"
                )
                drag_drive_enabled = False

        # 帧表模式准备（仅在勾选「帧表插值」时启用；不勾选时以下分支整体不执行，
        # 完全沿用原有槽位模型）。分组配置从「形态键扩展」节点直接读取——该节点运行
        # 在本节点**之后**，导出时 ini 里的 sync 块尚未写入，不能依赖解析 ini；
        # ini 解析仅作为旧版插件的兜底。
        frame_table_mode = bool(getattr(self.node, "use_frame_table", False))
        sequence_groups = {}
        group_totals = {}
        freq_params = {}
        frame_table_meta_map = {}
        frame_table_blocked = []
        if frame_table_mode:
            # 前置条件校验：帧表依赖「紧凑 + 合并槽位文件 + 优化查找」三件套产出的
            # 中间数据，且当前模板只处理位置通道。任一不满足都明确点名并回退，
            # 而不是让用户只看到一句笼统的「帧表构建失败」。
            if bool(getattr(self.node, "store_all_vertex_channels", False)):
                frame_table_blocked.append(
                    "「存储全部顶点属性增量」：帧表当前只建位置通道，法线/切线增量会被丢弃"
                    "（描边与光照不会跟随形变）"
                )
            if not use_packed:
                frame_table_blocked.append("「紧凑模式」：帧表需要合并后的逐槽位数据")
            if not merge_slot_files:
                frame_table_blocked.append("「合并槽位文件」：帧表需要 merged 数据产物")
            if not use_optimized:
                frame_table_blocked.append("「优化查找性能」：帧表需要 freq_indices 产物")
            if drag_drive_enabled:
                frame_table_blocked.append("「拖拽驱动形态键」：帧表模板尚未实现拖拽分支")
            if frame_table_blocked:
                # 勾选了帧表就应当明确成功或明确失败，不静默回退——否则用户无从
                # 分辨本次导出究竟用了哪套模型。
                raise ShapeKeyDirectExportError(
                    "帧表插值无法启用。请修正以下设置，或取消勾选「帧表插值（序列组加速）」后重试：\n"
                    + "\n".join("  - " + reason for reason in frame_table_blocked)
                )

        # 顶点命中索引（稀疏查找）准备：把「每顶点遍历全部槽位」换成「只遍历该顶点
        # 命中的槽位」。与帧表同样是「勾选了就明确成功或明确失败」，不静默回退。
        sparse_requested = bool(getattr(self.node, "use_sparse_vertex_index", False))
        use_sparse_vertex_index = self.node.effective_use_sparse_index(
            use_packed, use_delta, use_optimized, merge_slot_files
        )
        if sparse_requested:
            # 只要勾了就走这里：① 四项前置缺一不可；② 与帧表互斥——帧表即便前置
            # 齐全也必须拦下，否则稀疏资源段与 cs-t96/97/98 会被绑到帧表着色器上
            # （它没有这些寄存器），加载即出错。两种情况都明确失败，不静默回退。
            sparse_blockers = list(
                self.node._sparse_index_blockers(
                    use_packed, use_delta, use_optimized, merge_slot_files
                )
            )
            if frame_table_mode:
                sparse_blockers.append(
                    "「帧表插值（序列组加速）」：与顶点命中索引是互斥的两套渲染模型"
                )
            if sparse_blockers:
                raise ShapeKeyDirectExportError(
                    "「顶点命中索引（稀疏查找）」无法启用。请修正以下设置，或取消勾选后重试：\n"
                    + "\n".join("  - " + reason for reason in sparse_blockers)
                )
        if frame_table_mode:
            freq_params = {
                name: self.node.get_shape_key_export_variable_name(name) for name in all_unique_names
            }
            sequence_groups, group_totals = self._read_sequence_groups_from_ext_node(freq_params)
            if not sequence_groups:
                sequence_groups = self._parse_sequence_groups_from_ini(sections, ini_path=target_ini_file)
                group_totals = {}
            if not sequence_groups:
                raise ShapeKeyDirectExportError(
                    "帧表插值已启用，但未找到可用的序列分组。\n"
                    "请确认「形态键扩展」节点已启用、至少有一个「序列」模式的分组且该分组已分配到形态键；\n"
                    "或取消勾选「帧表插值（序列组加速）」后重试。"
                )

        hash_to_shader_paths = {}
        for logical_hash in processed_hashes:
            if logical_hash not in hash_slot_data_map or not hash_slot_data_map[logical_hash]:
                continue
            shader_dest_path = os.path.join(dest_res_dir, f"shapekey_anim_{logical_hash}.hlsl")
            # 只播种缺失的模板；已存在则保持原样，由 _update_shader_file 以模板为源
            # 重新注入。不能用 shutil.copy2：它每轮把目标 mtime 重置成模板的旧
            # mtime，而 3DMigoto 的 .bin 缓存按"注入后写入时刻"对齐 → 每轮必错配。
            if not os.path.exists(shader_dest_path):
                with open(shader_source_path, 'r', encoding='utf-8') as f:
                    write_text_if_changed(shader_dest_path, f.read())
            hash_to_shader_paths[logical_hash] = shader_dest_path

        # 权重同步 CS：形态键强度经 IniParams 打包中转搬进 mod 专属权重缓冲，
        # 由每个形态键 dispatch 段「run」它。内容固定（无 per-hash 注入），每份 mod
        # 只需一份。**缺了它，ini 里的 `cs = ./res/shapekey_weight_sync.hlsl` 会加载
        # 失败 → 权重恒为 0 → 形态键完全拖不动**（实机踩过：文件只被 node_postprocess
        # 路径复制，而本直出路径才是实际执行的入口）。
        weight_sync_source = os.path.join(os.path.dirname(shader_source_path), WEIGHT_SYNC_SHADER_NAME)
        if os.path.exists(weight_sync_source):
            with open(weight_sync_source, 'r', encoding='utf-8') as f:
                write_text_if_changed(os.path.join(dest_res_dir, WEIGHT_SYNC_SHADER_NAME), f.read())
        else:
            LOG.warning(f"直出形态键: 未找到权重同步着色器模板 {weight_sync_source}")

        for logical_hash in processed_hashes:
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
                )
            )

            if use_optimized:
                logical_prefix = self.node._extract_hash_prefix(logical_hash)
                vertex_count = hash_to_vertex_count.get(logical_prefix, 0)
                LOG.info(
                    f"Direct ShapeKey: write freq indices hash={logical_hash}, names={hash_unique_names}, objects={hash_unique_objects}, vertex_count={vertex_count}"
                )
                if use_sparse_vertex_index:
                    # 稀疏模式只写三份顶点命中索引，不写稠密 FREQ 表。
                    self._write_sparse_vertex_index(
                        logical_hash=logical_hash,
                        actual_hash=hash_to_actual_file_hash.get(logical_hash, logical_hash),
                        hash_slot_data=hash_slot_data,
                        unique_names=hash_unique_names,
                        vertex_count=vertex_count,
                        calculated_ranges=calculated_ranges,
                        merged_index_map=hash_to_merged_index_map.get(logical_hash),
                        slot_index_maps=hash_to_slot_maps.get(logical_hash, {}),
                    )
                else:
                    self._write_freq_indices(
                        logical_hash=logical_hash,
                        actual_hash=hash_to_actual_file_hash.get(logical_hash, logical_hash),
                        hash_slot_data=hash_slot_data,
                        unique_names=hash_unique_names,
                        vertex_count=vertex_count,
                        calculated_ranges=calculated_ranges,
                        merged_index_map=hash_to_merged_index_map.get(logical_hash),
                        slot_index_maps=hash_to_slot_maps.get(logical_hash, {}),
                    )

            frame_table_meta = None
            frame_vertex_count = hash_to_vertex_count.get(
                self.node._extract_hash_prefix(logical_hash), 0
            )
            if frame_table_mode:
                # 帧表模式下任何一步失败都**直接终止导出**：勾选了就该明确成功或明确
                # 失败，静默回退会让用户无法分辨本次究竟用了哪套模型。
                # 陈旧产物防护：merged 数据必须由**本次**导出写出。否则磁盘上残留的
                # 是上次的文件，直接拿来建表会得到静默错误的结果。
                _merged_ok = hash_to_merged_index_map.get(logical_hash) is not None
                if not _merged_ok:
                    raise ShapeKeyDirectExportError(
                        f"{logical_hash}: 本次导出未生成 merged 数据，帧表无法构建。"
                        "请确认已勾选「紧凑模式」与「合并槽位文件」。"
                    )
                try:
                    plan = self._build_frame_table_plan(
                        hash_unique_names, freq_params, sequence_groups, group_totals
                    )
                except Exception as exc:
                    raise ShapeKeyDirectExportError(
                        f"{logical_hash}: 帧表分组计划构建失败: {exc!r}"
                    ) from exc
                if not plan:
                    raise ShapeKeyDirectExportError(
                        f"{logical_hash}: 无法生成帧表分组计划。\n"
                        "常见原因：某分组只有部分形态键属于本网格（时间轴会错位）、"
                        "组内形态键缺少对应的强度变量、或分组数超过上限。"
                    )
                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                inputs = self._read_frame_table_inputs(actual_file_hash)
                if inputs is None:
                    raise ShapeKeyDirectExportError(
                        f"{logical_hash}: 读取帧表输入数据失败"
                        "（merged / freq_indices 缺失，或与当前顶点数不匹配）。"
                    )
                try:
                    frame_table_meta = self._write_frame_table_files(
                        actual_hash=actual_file_hash,
                        vertex_count=frame_vertex_count,
                        plan=plan,
                        inputs=inputs,
                    )
                except Exception as exc:
                    raise ShapeKeyDirectExportError(
                        f"{logical_hash}: 帧表数据写入失败: {exc!r}"
                    ) from exc
                if not frame_table_meta:
                    raise ShapeKeyDirectExportError(
                        f"{logical_hash}: 帧表数据为空（所有分组的顶点并集均为空）。"
                    )

            shader_path = hash_to_shader_paths.get(logical_hash)
            if shader_path and frame_table_meta:
                frame_table_template = self.node._get_frame_table_template_path()
                if not frame_table_template:
                    raise ShapeKeyDirectExportError(
                        "未找到帧表着色器模板 Toolset/shapekey_anim_frame_table.hlsl。"
                    )
                if not self._update_frame_table_shader(
                    shader_path,
                    frame_table_template,
                    key_count=len(hash_unique_names),
                    frame_meta=frame_table_meta["groups"],
                    hash_val=logical_hash,
                    vertex_count=frame_vertex_count,
                ):
                    raise ShapeKeyDirectExportError(
                        f"{logical_hash}: 帧表着色器注入失败。"
                    )
                frame_table_meta_map[logical_hash] = frame_table_meta
            elif shader_path:
                self.node._update_shader_file(
                    shader_path,
                    hash_slot_data,
                    use_packed,
                    use_delta,
                    hash_unique_names,
                    hash_unique_objects,
                    use_optimized=use_optimized,
                    merge_slot_files=merge_slot_files,
                    drag_drive_enabled=drag_drive_enabled,
                    drag_zone_ids=self.node._drag_drive_zone_ids(hash_unique_names) if drag_drive_enabled else None,
                    drag_click_stages=self.node._drag_drive_click_stages(hash_unique_names) if drag_drive_enabled else None,
                    drag_stage_count=self.node._drag_drive_stage_count(),
                    drag_dirs=self.node._drag_drive_dirs(hash_unique_names) if drag_drive_enabled else None,
                    hash_val=logical_hash,
                    source_path=shader_source_path,
                    use_sparse_vertex_index=use_sparse_vertex_index,
                )
        LOG.info(f"直出形态键: shader/freq 写出完成 {perf_counter() - stage_start:.3f}s")

        self._update_ini_sections(
            sections=sections,
            preserved_tail_content=preserved_tail_content,
            preserved_driver_content=preserved_driver_content,
            target_ini_file=target_ini_file,
            slot_to_name_to_objects=slot_to_name_to_objects,
            unique_hashes=processed_hashes,
            hash_to_objects=hash_to_objects,
            all_unique_names=all_unique_names,
            all_unique_objects=all_unique_objects,
            calculated_ranges=calculated_ranges,
            hash_to_stride=hash_to_stride,
            hash_to_actual_file_hash=hash_to_actual_file_hash,
            hash_to_vertex_count=hash_to_vertex_count,
            hash_slot_data_map=hash_slot_data_map,
            hash_to_base_resources=hash_to_base_resources,
            use_packed=use_packed,
            use_delta=use_delta,
            use_optimized=use_optimized,
            merge_slot_files=merge_slot_files,
            drag_drive_resource=drag_drive_resource,
            frame_table_meta_map=frame_table_meta_map,
            use_sparse_index=use_sparse_vertex_index,
        )
        LOG.info(f"直出形态键: ini 更新完成 {perf_counter() - stage_start:.3f}s")

        # 帧表模式收尾：删除本次已不再被 ini 引用的中间数据文件（约 190 MB）。
        # 必须放在 ini 写回**之后**——若中途失败，ini 仍引用旧文件、文件也还在，
        # mod 不会陷入「有声明无文件」的坏状态。
        if frame_table_meta_map:
            removed = 0
            for logical_hash in processed_hashes:
                if frame_table_meta_map.get(logical_hash) is None:
                    continue
                actual_file_hash = hash_to_actual_file_hash.get(logical_hash, logical_hash)
                for suffix in (
                    self.node._get_merged_data_file_suffix(use_delta),
                    "_merged_map",
                    "_freq_indices",
                ):
                    stale_path = os.path.join(
                        self.meshes_dir, f"{actual_file_hash}-Position{suffix}.buf"
                    )
                    if os.path.exists(stale_path):
                        try:
                            os.remove(stale_path)
                            removed += 1
                        except OSError as exc:
                            LOG.warning(f"直出形态键: 删除冗余数据文件失败 {stale_path}: {exc!r}")
            if removed:
                LOG.info(f"直出形态键: 帧表模式已清理 {removed} 个冗余数据文件")
