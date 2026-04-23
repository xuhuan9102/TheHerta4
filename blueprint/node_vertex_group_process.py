# -*- coding: utf-8 -*-
import bpy
import re
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Any, Optional
import threading

from .node_base import SSMTNodeBase


class SSMTNode_VertexGroupProcess(SSMTNodeBase):
    '''顶点组处理节点：在前处理流程中自动执行顶点组重命名、合并、清理、填充和排序'''
    bl_idname = 'SSMTNode_VertexGroupProcess'
    bl_label = '顶点组处理'
    bl_description = '在前处理流程中自动执行顶点组重命名、合并、清理、填充和排序'
    bl_icon = 'GROUP'
    bl_width_min = 300

    _mapping_cache: Dict[str, Dict[str, str]] = {}
    _cache_lock = threading.Lock()

    fill_missing_groups: bpy.props.BoolProperty(
        name='填充缺失组',
        description='是否自动补齐 0 到最大编号之间缺失的数字顶点组',
        default=True,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "物体")
        self.inputs.new('SSMTSocketObject', "映射表 1")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 300

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text="顶点组处理", icon='GROUP')

        connected_count = self._get_connected_mapping_count()
        box.label(text=f"已连接映射表: {connected_count}", icon='TEXT')

        box.separator()
        box.label(text="映射表配置:", icon='SETTINGS')
        box.prop(self, 'fill_missing_groups')

        for i, socket in enumerate(self.inputs):
            if i > 0:
                row = box.row(align=True)
                row.label(text=f"映射表 {i}:")

                if socket.is_linked:
                    for link in socket.links:
                        node_name = link.from_node.name if link.from_node else "未知"
                        target_hash = getattr(link.from_node, 'target_hash', '') if link.from_node else ''

                        if target_hash:
                            row.label(text=f"→ {node_name[:15]} (哈希: {target_hash})", icon='LINKED')
                        else:
                            row.label(text=f"→ {node_name[:15]} (全局)", icon='LINKED')
                else:
                    row.label(text="(未连接)")

    def update(self):
        if self.inputs and len(self.inputs) >= 2:
            if self.inputs[-1].is_linked:
                self.inputs.new('SSMTSocketObject', f"映射表 {len(self.inputs)}")

            while len(self.inputs) > 2 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
                self.inputs.remove(self.inputs[-1])

    def _get_connected_mapping_count(self):
        count = 0
        for i, socket in enumerate(self.inputs):
            if i > 0 and socket.is_linked:
                count += 1
        return count

    def get_connected_mapping_nodes(self):
        mapping_nodes = []

        for i, socket in enumerate(self.inputs):
            if i > 0 and socket.is_linked:
                for link in socket.links:
                    from_node = link.from_node
                    target_hash = getattr(from_node, 'target_hash', '')

                    if from_node.bl_idname == 'SSMTNode_VertexGroupMatch':
                        if hasattr(from_node, 'source_object') and hasattr(from_node, 'target_object'):
                            mapping_nodes.append({
                                'node': from_node,
                                'target_hash': target_hash,
                                'index': i,
                                'type': 'match'
                            })
                    elif from_node.bl_idname == 'SSMTNode_VertexGroupMappingInput':
                        mapping_nodes.append({
                            'node': from_node,
                            'target_hash': target_hash,
                            'index': i,
                            'type': 'input'
                        })

        return mapping_nodes

    def get_merged_mapping_for_object(self, obj_name, mapping_nodes):
        merged_mapping = {}
        exact_match_found = False

        def get_node_priority(node_info):
            node = node_info['node']
            exact_match = getattr(node, 'exact_hash_match', False)
            return (0 if exact_match else 1, node_info['index'])

        sorted_nodes = sorted(mapping_nodes, key=get_node_priority)

        for node_info in sorted_nodes:
            node = node_info['node']
            target_hash = node_info['target_hash']
            node_type = node_info.get('type', 'match')
            exact_match = getattr(node, 'exact_hash_match', False)

            if exact_match_found and not exact_match:
                continue

            if target_hash and not obj_name.startswith(target_hash):
                continue

            if node_type == 'input':
                if hasattr(node, 'get_mapping_dict'):
                    mapping = node.get_mapping_dict()
                    merged_mapping.update(mapping)
            else:
                mapping_text_name = getattr(node, 'mapping_text_name', '')

                if mapping_text_name and mapping_text_name in bpy.data.texts:
                    text = bpy.data.texts[mapping_text_name]
                    mapping = self.parse_mapping_text(text)
                    merged_mapping.update(mapping)
                else:
                    target_obj_name = getattr(node, 'target_object', '')

                    base_text_name = f"VG_Match_{target_obj_name}"

                    if len(base_text_name) > 63:
                        import hashlib
                        hash_suffix = hashlib.md5(target_obj_name.encode()).hexdigest()[:8]
                        base_text_name = f"VG_Match_{hash_suffix}"

                    text = bpy.data.texts.get(base_text_name)

                    if not text:
                        suffix = 1
                        while True:
                            text_name = f"{base_text_name}_{suffix:03d}"
                            text = bpy.data.texts.get(text_name)
                            if text:
                                break
                            suffix += 1

                    if text:
                        mapping = self.parse_mapping_text(text)
                        merged_mapping.update(mapping)

            if exact_match and target_hash and obj_name.startswith(target_hash):
                exact_match_found = True

        return merged_mapping

    def parse_mapping_text(self, text):
        mapping = {}
        for line in text.lines:
            clean_line = re.sub(r'[#//].*', '', line.body).strip()
            if '=' in clean_line:
                parts = clean_line.split('=', 1)
                if len(parts) == 2:
                    left = parts[0].strip()
                    right = parts[1].strip()
                    if left and right:
                        mapping[left] = right
        return mapping

    def parse_mapping_text_threadsafe(self, text_lines: List[Tuple[int, str]]) -> Dict[str, str]:
        mapping = {}
        for _, line_content in text_lines:
            clean_line = re.sub(r'[#//].*', '', line_content).strip()
            if '=' in clean_line:
                parts = clean_line.split('=', 1)
                if len(parts) == 2:
                    left = parts[0].strip()
                    right = parts[1].strip()
                    if left and right:
                        mapping[left] = right
        return mapping

    def prepare_mapping_data_threadsafe(self, mapping_nodes: List[Dict]) -> Dict[str, Any]:
        result = {
            'nodes': [],
            'global_mappings': {}
        }

        for node_info in mapping_nodes:
            node = node_info['node']
            target_hash = node_info['target_hash']
            node_type = node_info.get('type', 'match')
            exact_match = getattr(node, 'exact_hash_match', False)

            node_data = {
                'target_hash': target_hash,
                'index': node_info['index'],
                'type': node_type,
                'exact_match': exact_match,
                'mapping': {}
            }

            if node_type == 'input':
                if hasattr(node, 'get_mapping_dict'):
                    node_data['mapping'] = dict(node.get_mapping_dict())
            else:
                mapping_text_name = getattr(node, 'mapping_text_name', '')
                target_obj_name = getattr(node, 'target_object', '')
                node_data['mapping_text_name'] = mapping_text_name
                node_data['target_obj_name'] = target_obj_name

            result['nodes'].append(node_data)

        return result

    def compute_mapping_for_object_threadsafe(
        self,
        obj_name: str,
        prepared_data: Dict[str, Any],
        text_cache: Dict[str, Dict[str, str]]
    ) -> Dict[str, str]:
        merged_mapping = {}
        exact_match_found = False

        def get_node_priority(node_data):
            return (0 if node_data['exact_match'] else 1, node_data['index'])

        sorted_nodes = sorted(prepared_data['nodes'], key=get_node_priority)

        for node_data in sorted_nodes:
            target_hash = node_data['target_hash']
            exact_match = node_data['exact_match']
            node_type = node_data['type']

            if exact_match_found and not exact_match:
                continue

            if target_hash and not obj_name.startswith(target_hash):
                continue

            mapping = {}

            if node_type == 'input':
                mapping = dict(node_data.get('mapping', {}))
            else:
                mapping_text_name = node_data.get('mapping_text_name', '')
                target_obj_name = node_data.get('target_obj_name', '')

                cache_key = mapping_text_name or target_obj_name
                if cache_key and cache_key in text_cache:
                    mapping = dict(text_cache[cache_key])

            merged_mapping.update(mapping)

            if exact_match and target_hash and obj_name.startswith(target_hash):
                exact_match_found = True

        return merged_mapping

    def prepare_text_cache(self, mapping_nodes: List[Dict]) -> Dict[str, Dict[str, str]]:
        cache = {}

        for node_info in mapping_nodes:
            node = node_info['node']
            node_type = node_info.get('type', 'match')

            if node_type != 'input':
                mapping_text_name = getattr(node, 'mapping_text_name', '')
                target_obj_name = getattr(node, 'target_object', '')

                cache_key = mapping_text_name or target_obj_name
                if not cache_key or cache_key in cache:
                    continue

                if mapping_text_name and mapping_text_name in bpy.data.texts:
                    text = bpy.data.texts[mapping_text_name]
                    text_lines = [(i, line.body) for i, line in enumerate(text.lines)]
                    cache[cache_key] = self.parse_mapping_text_threadsafe(text_lines)
                elif target_obj_name:
                    base_text_name = f"VG_Match_{target_obj_name}"

                    if len(base_text_name) > 63:
                        import hashlib
                        hash_suffix = hashlib.md5(target_obj_name.encode()).hexdigest()[:8]
                        base_text_name = f"VG_Match_{hash_suffix}"

                    text = bpy.data.texts.get(base_text_name)
                    if not text:
                        suffix = 1
                        while True:
                            text_name = f"{base_text_name}_{suffix:03d}"
                            text = bpy.data.texts.get(text_name)
                            if text:
                                break
                            suffix += 1
                            if suffix > 100:
                                break

                    if text:
                        text_lines = [(i, line.body) for i, line in enumerate(text.lines)]
                        cache[cache_key] = self.parse_mapping_text_threadsafe(text_lines)

        return cache

    def batch_compute_mappings_threadsafe(
        self,
        obj_names: List[str],
        mapping_nodes: List[Dict],
        max_workers: int = 4
    ) -> Dict[str, Dict[str, str]]:
        prepared_data = self.prepare_mapping_data_threadsafe(mapping_nodes)
        text_cache = self.prepare_text_cache(mapping_nodes)

        results = {}

        def compute_single(obj_name: str) -> Tuple[str, Dict[str, str]]:
            mapping = self.compute_mapping_for_object_threadsafe(obj_name, prepared_data, text_cache)
            return obj_name, mapping

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(compute_single, name): name for name in obj_names}

            for future in as_completed(futures):
                try:
                    obj_name, mapping = future.result()
                    results[obj_name] = mapping
                except Exception as e:
                    obj_name = futures[future]
                    results[obj_name] = {}

        return results

    def process_objects_batch(self, objects: List[bpy.types.Object], max_workers: int = 4) -> Dict[str, Dict[str, int]]:
        mesh_objects = [obj for obj in objects if obj and obj.type == 'MESH']

        if not mesh_objects:
            return {}

        all_stats = {}

        mapping_nodes = self.get_connected_mapping_nodes()

        if mapping_nodes:
            obj_names = [obj.name for obj in mesh_objects]
            mappings = self.batch_compute_mappings_threadsafe(obj_names, mapping_nodes, max_workers)
        else:
            mappings = {}

        for obj in mesh_objects:
            obj_stats = {"renamed": 0, "merged": 0, "cleaned": 0, "filled": 0}

            try:
                obj_mapping = mappings.get(obj.name, {})

                if obj_mapping:
                    obj_stats["renamed"] = self._rename_vertex_groups(obj, obj_mapping)

                obj_stats["merged"] = self._merge_vertex_groups_by_prefix(obj)
                obj_stats["cleaned"] = self._remove_non_numeric_vertex_groups(obj)
                obj_stats["filled"] = self._fill_vertex_group_gaps(obj)
                self._sort_vertex_groups(obj)

            except Exception as e:
                print(f"[VGProcess] 处理物体 {obj.name} 时发生错误: {e}")

            all_stats[obj.name] = obj_stats

        return all_stats

    @staticmethod
    def generate_debug_detail(chain) -> list:
        """为处理链生成顶点组处理节点的调试信息

        Args:
            chain: ProcessingChain 实例

        Returns:
            list[str]: 调试文本行列表
        """
        lines = []
        if chain.vertex_group_process_nodes:
            lines.append(f"🔧 顶点组处理节点 ({len(chain.vertex_group_process_nodes)}个):")
            for vg_node in chain.vertex_group_process_nodes:
                process_mode = getattr(vg_node, 'process_mode', 'N/A')
                lines.append(f"   - {vg_node.name} (模式: {process_mode})")
        if chain.vertex_group_mapping_nodes:
            lines.append(f"🗺️ 顶点组映射节点 ({len(chain.vertex_group_mapping_nodes)}个):")
            for map_node in chain.vertex_group_mapping_nodes:
                node_type = map_node.bl_idname.replace('SSMTNode_', '')
                if node_type == 'VertexGroupMappingInput':
                    mapping_text = getattr(map_node, 'mapping_text', '')
                    lines.append(f"   - {map_node.name} (输入映射, 文本长度: {len(mapping_text)})")
                else:
                    target_hash = getattr(map_node, 'target_hash', '')
                    exact_match = getattr(map_node, 'exact_hash_match', False)
                    match_type = "全匹配" if exact_match else "前缀匹配"
                    lines.append(f"   - {map_node.name} ({match_type}: {target_hash})")
        return lines

    @staticmethod
    def generate_debug_summary(processing_chains: list) -> str:
        """生成全局顶点组处理统计摘要

        Args:
            processing_chains: 所有处理链列表

        Returns:
            str: 统计摘要文本行
        """
        chains_with_vg = sum(1 for c in processing_chains if c.vertex_group_process_nodes)
        total_vg_nodes = sum(len(c.vertex_group_process_nodes) for c in processing_chains)
        return f"顶点组处理: {total_vg_nodes} 个节点 (影响 {chains_with_vg} 个物体)"

    @staticmethod
    def execute_batch_from_chains(valid_chains: list) -> dict:
        """
        从处理链批量执行顶点组处理
        
        Args:
            valid_chains: 有效的处理链列表
            
        Returns:
            dict: {'processed_count': int, 'total_stats': dict}
        """
        from ..utils.log_utils import LOG
        import time
        
        start_time = time.time()
        
        processed_objects = set()
        total_stats = {"renamed": 0, "merged": 0, "cleaned": 0, "filled": 0}
        
        node_to_objects = {}
        node_to_mapping_names = {}

        for chain in valid_chains:
            if chain.object_name in processed_objects:
                continue

            if not chain.vertex_group_process_nodes:
                continue

            obj = bpy.data.objects.get(chain.object_name)
            if not obj and chain.original_object_name:
                obj = bpy.data.objects.get(chain.original_object_name)
            if not obj or obj.type != 'MESH':
                continue

            for node in chain.node_path:
                if node.bl_idname == 'SSMTNode_VertexGroupProcess':
                    if node not in node_to_objects:
                        node_to_objects[node] = []
                        mapping_names = []
                        mapping_nodes = node.get_connected_mapping_nodes()
                        for mn in mapping_nodes:
                            mn_node = mn['node']
                            mn_type = mn.get('type', 'match')
                            if mn_type == 'input':
                                mapping_names.append(f"输入:{mn_node.name}")
                            else:
                                target_hash = mn.get('target_hash', '')
                                exact_match = getattr(mn_node, 'exact_hash_match', False)
                                prefix = "全匹配:" if exact_match else "匹配:"
                                mapping_names.append(f"{prefix}{target_hash}")
                        node_to_mapping_names[node] = mapping_names
                    node_to_objects[node].append((chain.object_name, obj))

            processed_objects.add(chain.object_name)

        if not node_to_objects:
            return {'processed_count': 0, 'total_stats': total_stats}

        LOG.info("🔧 顶点组处理节点开始执行")

        for node, objects_list in node_to_objects.items():
            mapping_names = node_to_mapping_names[node]
            objects = [obj for _, obj in objects_list]
            
            stats = node.process_objects_batch(objects)
            
            for obj_name, obj in objects_list:
                if obj.name in stats:
                    obj_stats = stats[obj.name]
                    total_stats["renamed"] += obj_stats.get("renamed", 0)
                    total_stats["merged"] += obj_stats.get("merged", 0)
                    total_stats["cleaned"] += obj_stats.get("cleaned", 0)
                    total_stats["filled"] += obj_stats.get("filled", 0)
            
            if objects_list:
                    LOG.info(f"   [{', '.join(mapping_names)}] {len(objects_list)} 个物体")

        elapsed = time.time() - start_time
        LOG.info(f"   ✅ 顶点组处理节点执行完成: {len(processed_objects)} 个物体, "
                f"重命名={total_stats['renamed']}, 合并={total_stats['merged']}, "
                f"清理={total_stats['cleaned']}, 填充={total_stats['filled']}")
        LOG.info(f"   ⏱️ 总耗时: {elapsed:.2f}s")

        return {'processed_count': len(processed_objects), 'total_stats': total_stats}

    def generate_unique_name(self, base_name, collection):
        if base_name not in collection:
            return base_name
        suffix = 1
        while f"{base_name}.{suffix:03d}" in collection:
            suffix += 1
        return f"{base_name}.{suffix:03d}"

    def process_object(self, obj, mapping_cache: Dict[str, Dict[str, str]] = None):
        if not obj or obj.type != 'MESH':
            return {"renamed": 0, "merged": 0, "cleaned": 0, "filled": 0}

        stats = {"renamed": 0, "merged": 0, "cleaned": 0, "filled": 0}

        try:
            mapping_nodes = self.get_connected_mapping_nodes()

            if mapping_nodes:
                if mapping_cache is not None and obj.name in mapping_cache:
                    merged_mapping = mapping_cache[obj.name]
                else:
                    merged_mapping = self.get_merged_mapping_for_object(obj.name, mapping_nodes)

                if merged_mapping:
                    stats["renamed"] = self._rename_vertex_groups(obj, merged_mapping)

            stats["merged"] = self._merge_vertex_groups_by_prefix(obj)
            stats["cleaned"] = self._remove_non_numeric_vertex_groups(obj)
            if self.fill_missing_groups:
                stats["filled"] = self._fill_vertex_group_gaps(obj)
            self._sort_vertex_groups(obj)
        except Exception as e:
            print(f"[VGProcess] 处理物体 {obj.name} 时发生错误: {e}")
            import traceback
            traceback.print_exc()

        return stats

    def _rename_vertex_groups(self, obj, mapping):
        temp_prefix = f"__temp_{uuid.uuid4().hex[:8]}_"

        vg_dict = {vg.name: vg for vg in obj.vertex_groups}
        rename_pairs = []
        for vg_name, vg in vg_dict.items():
            if vg_name in mapping:
                new_name = mapping[vg_name]
                if vg_name != new_name:
                    rename_pairs.append((vg_name, new_name))

        if not rename_pairs:
            return 0

        target_names = {new_name for _, new_name in rename_pairs}
        source_names = {old_name for old_name, _ in rename_pairs}

        conflict_names = [name for name in target_names if name in vg_dict and name not in source_names]

        for conflict_name in conflict_names:
            vg = vg_dict.get(conflict_name)
            if vg:
                temp_name = f"{temp_prefix}conflict_{conflict_name}"
                vg.name = temp_name
                vg_dict[temp_name] = vg
                del vg_dict[conflict_name]

        for old_name, new_name in rename_pairs:
            vg = vg_dict.get(old_name)
            if vg:
                temp_name = f"{temp_prefix}{old_name}"
                vg.name = temp_name
                vg_dict[temp_name] = vg
                if old_name in vg_dict:
                    del vg_dict[old_name]

        renamed_count = 0
        for old_name, new_name in rename_pairs:
            temp_name = f"{temp_prefix}{old_name}"
            vg = vg_dict.get(temp_name)
            if vg:
                vg.name = new_name
                renamed_count += 1

        for vg in obj.vertex_groups:
            if vg.name.startswith(temp_prefix):
                new_name = vg.name[len(temp_prefix):]
                if new_name.startswith("conflict_"):
                    new_name = new_name[len("conflict_"):]
                vg.name = new_name

        return renamed_count

    def _merge_vertex_groups_by_prefix(self, obj):
        prefix_map = defaultdict(list)
        vg_by_name = {}

        for vg in obj.vertex_groups:
            vg_by_name[vg.name] = vg
            match = re.match(r'^(\d+)', vg.name)
            if match:
                prefix_map[match.group(1)].append(vg)

        merge_needed = {}
        for prefix, source_groups in prefix_map.items():
            if len(source_groups) > 1 or (len(source_groups) == 1 and source_groups[0].name != prefix):
                merge_needed[prefix] = source_groups

        if not merge_needed:
            return 0

        source_index_to_target = {}
        target_vg_indices = set()
        vg_index_to_name = {vg.index: vg.name for vg in obj.vertex_groups}
        for prefix, source_groups in merge_needed.items():
            target_vg = vg_by_name.get(prefix)
            if not target_vg:
                target_vg = obj.vertex_groups.new(name=prefix)
                vg_by_name[prefix] = target_vg
                vg_index_to_name[target_vg.index] = target_vg.name
            target_vg_indices.add(target_vg.index)
            for src_vg in source_groups:
                if src_vg != target_vg:
                    source_index_to_target[src_vg.index] = prefix

        if not source_index_to_target:
            return 0

        target_vg_map = {prefix: vg_by_name[prefix] for prefix in merge_needed}

        target_weights = defaultdict(dict)

        for vert in obj.data.vertices:
            for g in vert.groups:
                if g.weight <= 0:
                    continue
                target_prefix = source_index_to_target.get(g.group)
                if target_prefix:
                    existing = target_weights[target_prefix].get(vert.index, 0.0)
                    target_weights[target_prefix][vert.index] = existing + g.weight
                elif g.group in target_vg_indices:
                    vg_name = vg_index_to_name.get(g.group, "")
                    match = re.match(r'^(\d+)', vg_name)
                    if match:
                        prefix = match.group(1)
                        if prefix in target_vg_map:
                            existing = target_weights[prefix].get(vert.index, 0.0)
                            target_weights[prefix][vert.index] = existing + g.weight

        for prefix, w_dict in target_weights.items():
            target_vg = target_vg_map.get(prefix)
            if not target_vg:
                continue
            weight_to_verts = defaultdict(list)
            for vert_idx, weight in w_dict.items():
                clamped = min(1.0, weight)
                weight_to_verts[clamped].append(vert_idx)
            for weight, vert_indices in weight_to_verts.items():
                target_vg.add(vert_indices, weight, 'REPLACE')

        groups_to_delete = set()
        for prefix, source_groups in merge_needed.items():
            for g in source_groups:
                if not g.name.isdigit():
                    groups_to_delete.add(g)

        for vg in groups_to_delete:
            try:
                if vg.name in obj.vertex_groups:
                    obj.vertex_groups.remove(vg)
            except Exception as e:
                print(f"[VGProcess] 删除顶点组 {vg.name} 失败: {e}")

        return len(merge_needed)

    def _remove_non_numeric_vertex_groups(self, obj):
        groups_to_remove = [vg for vg in obj.vertex_groups if not vg.name.isdigit()]
        for vg in reversed(groups_to_remove):
            try:
                obj.vertex_groups.remove(vg)
            except Exception:
                pass
        return len(groups_to_remove)

    def _fill_vertex_group_gaps(self, obj):
        numeric_names = set()
        for vg in obj.vertex_groups:
            if vg.name.isdigit():
                numeric_names.add(vg.name)

        if not numeric_names:
            return 0

        try:
            max_num = max(int(name) for name in numeric_names)
        except (ValueError, TypeError):
            return 0

        if max_num > 1000:
            return 0

        filled_count = 0
        for num in range(max_num + 1):
            name = str(num)
            if name not in numeric_names:
                try:
                    obj.vertex_groups.new(name=name)
                    filled_count += 1
                except Exception:
                    pass

        return filled_count

    def _sort_vertex_groups(self, obj):
        if not obj.vertex_groups:
            return

        if len(obj.vertex_groups) <= 1:
            return

        try:
            vg_index_to_name = {vg.index: vg.name for vg in obj.vertex_groups}

            vg_weights = {vg.name: defaultdict(list) for vg in obj.vertex_groups}

            for vert in obj.data.vertices:
                for g in vert.groups:
                    vg_name = vg_index_to_name.get(g.group)
                    if vg_name and g.weight > 0:
                        vg_weights[vg_name][g.weight].append(vert.index)

            def sort_key(name):
                if name.isdigit():
                    return (0, int(name))
                else:
                    return (1, name)

            sorted_names = sorted(vg_weights.keys(), key=sort_key)

            for vg in list(obj.vertex_groups):
                try:
                    obj.vertex_groups.remove(vg)
                except Exception:
                    pass

            for name in sorted_names:
                new_vg = obj.vertex_groups.new(name=name)
                weight_dict = vg_weights[name]
                for weight, vert_indices in weight_dict.items():
                    new_vg.add(vert_indices, weight, 'REPLACE')
        except Exception as e:
            print(f"[VGProcess] 数据层面排序顶点组失败: {e}")
            import traceback
            traceback.print_exc()


classes = (
    SSMTNode_VertexGroupProcess,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
