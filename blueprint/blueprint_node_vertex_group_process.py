# -*- coding: utf-8 -*-
import bpy
import re
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Any, Optional
import threading

from .blueprint_node_base import SSMTNodeBase


class SSMTNode_VertexGroupProcess(SSMTNodeBase):
    '''顶点组处理节点：在前处理流程中自动执行顶点组重命名、合并、清理、填充和排序'''
    bl_idname = 'SSMTNode_VertexGroupProcess'
    bl_label = '顶点组处理'
    bl_description = '在前处理流程中自动执行顶点组重命名、合并、清理、填充和排序'
    bl_icon = 'GROUP'
    bl_width_min = 300
    
    _mapping_cache: Dict[str, Dict[str, str]] = {}
    _cache_lock = threading.Lock()

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
        print(f"[VGProcess] {self.name}: 开始获取连接的映射表节点")
        
        for i, socket in enumerate(self.inputs):
            if i > 0 and socket.is_linked:
                for link in socket.links:
                    from_node = link.from_node
                    target_hash = getattr(from_node, 'target_hash', '')
                    
                    print(f"[VGProcess] {self.name}: 输入 {i} 连接到节点 '{from_node.name}', 类型: {from_node.bl_idname}, 哈希: '{target_hash}'")
                    
                    if from_node.bl_idname == 'SSMTNode_VertexGroupMatch':
                        if hasattr(from_node, 'source_object') and hasattr(from_node, 'target_object'):
                            mapping_nodes.append({
                                'node': from_node,
                                'target_hash': target_hash,
                                'index': i,
                                'type': 'match'
                            })
                            print(f"[VGProcess] {self.name}: 添加匹配节点 '{from_node.name}'")
                    elif from_node.bl_idname == 'SSMTNode_VertexGroupMappingInput':
                        mapping_nodes.append({
                            'node': from_node,
                            'target_hash': target_hash,
                            'index': i,
                            'type': 'input'
                        })
                        print(f"[VGProcess] {self.name}: 添加输入节点 '{from_node.name}'")
            elif i > 0:
                print(f"[VGProcess] {self.name}: 输入 {i} 未连接")
        
        print(f"[VGProcess] {self.name}: 共找到 {len(mapping_nodes)} 个映射表节点")
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
            
            print(f"[VGProcess] 检查映射节点: {node.name}, 哈希: '{target_hash}', 全匹配: {exact_match}, 物体名: '{obj_name}'")
            
            if exact_match_found and not exact_match:
                print(f"[VGProcess] 已有全匹配映射表处理过此物体，跳过普通映射表")
                continue
            
            if target_hash and not obj_name.startswith(target_hash):
                print(f"[VGProcess] 哈希不匹配，跳过: '{target_hash}' vs '{obj_name}'")
                continue
            
            print(f"[VGProcess] 哈希匹配成功: '{target_hash}'")
            
            if node_type == 'input':
                if hasattr(node, 'get_mapping_dict'):
                    mapping = node.get_mapping_dict()
                    print(f"[VGProcess] 从映射输入节点获取到 {len(mapping)} 条映射")
                    merged_mapping.update(mapping)
            else:
                mapping_text_name = getattr(node, 'mapping_text_name', '')
                
                if mapping_text_name and mapping_text_name in bpy.data.texts:
                    text = bpy.data.texts[mapping_text_name]
                    mapping = self.parse_mapping_text(text)
                    print(f"[VGProcess] 从节点存储的映射表 '{mapping_text_name}' 解析到 {len(mapping)} 条映射")
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
                        print(f"[VGProcess] 从文本 '{text.name}' 解析到 {len(mapping)} 条映射")
                        merged_mapping.update(mapping)
                    else:
                        print(f"[VGProcess] 警告: 未找到映射文本 '{base_text_name}'")
            
            if exact_match and target_hash and obj_name.startswith(target_hash):
                exact_match_found = True
                print(f"[VGProcess] 全匹配映射表已处理，标记物体 '{obj_name}' 为已匹配")
        
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
        print(f"[VGProcess] 开始多线程计算 {len(obj_names)} 个物体的映射...")
        
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
                    print(f"[VGProcess] 计算物体 {obj_name} 映射时出错: {e}")
                    results[obj_name] = {}
        
        print(f"[VGProcess] 多线程映射计算完成")
        return results

    def process_objects_batch(self, objects: List[bpy.types.Object], max_workers: int = 4) -> Dict[str, Dict[str, int]]:
        import time
        start_time = time.time()
        
        mesh_objects = [obj for obj in objects if obj and obj.type == 'MESH']
        
        if not mesh_objects:
            return {}
        
        print(f"[VGProcess] 批量处理 {len(mesh_objects)} 个网格物体")
        
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
                import traceback
                traceback.print_exc()
            
            all_stats[obj.name] = obj_stats
        
        elapsed = time.time() - start_time
        print(f"[VGProcess] 批量处理完成，耗时: {elapsed:.2f}秒")
        
        return all_stats

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
        
        merged_count = 0
        groups_to_delete = []
        
        for prefix, source_groups in prefix_map.items():
            if len(source_groups) > 1 or (len(source_groups) == 1 and source_groups[0].name != prefix):
                try:
                    target_vg = vg_by_name.get(prefix)
                    if not target_vg:
                        target_vg = obj.vertex_groups.new(name=prefix)
                        vg_by_name[prefix] = target_vg
                    
                    source_indices = [vg.index for vg in source_groups]
                    vertices = obj.data.vertices
                    
                    vert_weights = defaultdict(float)
                    
                    for vert in vertices:
                        for src_vg in source_groups:
                            try:
                                vert_weights[vert.index] += src_vg.weight(vert.index)
                            except RuntimeError:
                                continue
                    
                    if vert_weights:
                        for vert_idx, weight in vert_weights.items():
                            if weight > 0:
                                target_vg.add([vert_idx], min(1.0, weight), 'REPLACE')
                    
                    groups_to_delete.extend(g for g in source_groups if not g.name.isdigit())
                    merged_count += 1
                except Exception as e:
                    print(f"[VGProcess] 合并顶点组前缀 {prefix} 失败: {e}")
        
        for vg in set(groups_to_delete):
            try:
                if vg.name in obj.vertex_groups:
                    obj.vertex_groups.remove(vg)
            except Exception as e:
                print(f"[VGProcess] 删除顶点组 {vg.name} 失败: {e}")
        
        return merged_count

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
            vg_data = []
            
            for vg in obj.vertex_groups:
                weights = {}
                for vert in obj.data.vertices:
                    try:
                        w = vg.weight(vert.index)
                        if w > 0:
                            weights[vert.index] = w
                    except RuntimeError:
                        continue
                
                vg_data.append((vg.name, weights))
            
            def sort_key(item):
                name = item[0]
                if name.isdigit():
                    return (0, int(name))
                else:
                    return (1, name)
            
            vg_data.sort(key=sort_key)
            
            for vg in list(obj.vertex_groups):
                try:
                    obj.vertex_groups.remove(vg)
                except Exception:
                    pass
            
            for name, weights in vg_data:
                new_vg = obj.vertex_groups.new(name=name)
                for vert_idx, weight in weights.items():
                    new_vg.add([vert_idx], weight, 'REPLACE')
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
