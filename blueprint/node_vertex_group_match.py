# -*- coding: utf-8 -*-
"""
优化版顶点组匹配节点
结合 KD-Tree 快速查询和 Chamfer 距离精确匹配
"""
import bpy
import bmesh
import json
import re
import time
import uuid
import numpy as np
from collections import defaultdict
from bpy.types import PropertyGroup
from mathutils import Vector, kdtree
from typing import Dict, List, Tuple, Optional

from .node_base import SSMTNodeBase

_keymaps = []


class VertexGroupMatcherOptimized:
    """优化版顶点组匹配器：结合 KD-Tree 预筛选和 Chamfer 距离精确匹配"""
    
    def __init__(self, candidates_count: int = 3, chunk_size: int = 256):
        self.candidates_count = candidates_count
        self.chunk_size = chunk_size
    
    def get_vertex_positions(self, obj, use_shape_key: bool = False, context=None) -> np.ndarray:
        """获取物体所有顶点的世界坐标"""
        if use_shape_key and context:
            depsgraph = context.evaluated_depsgraph_get()
            eval_obj = obj.evaluated_get(depsgraph)
            positions = np.array([
                (eval_obj.matrix_world @ v.co)[:]
                for v in eval_obj.data.vertices
            ], dtype=np.float32)
        else:
            positions = np.array([
                (obj.matrix_world @ v.co)[:]
                for v in obj.data.vertices
            ], dtype=np.float32)
        return positions
    
    def get_vg_point_clouds(self, obj, positions: np.ndarray) -> Dict[str, np.ndarray]:
        """获取每个顶点组影响的所有顶点点云"""
        vg_points = {}
        vg_weights = {}
        
        for vg in obj.vertex_groups:
            vg_points[vg.name] = []
            vg_weights[vg.name] = []
        
        for vert_idx, vert in enumerate(obj.data.vertices):
            for vgroup in vert.groups:
                try:
                    vg = obj.vertex_groups[vgroup.group]
                    weight = vgroup.weight
                    if weight > 0:
                        vg_points[vg.name].append(positions[vert_idx])
                        vg_weights[vg.name].append(weight)
                except (IndexError, RuntimeError):
                    continue
        
        result = {}
        for name, points in vg_points.items():
            if points:
                result[name] = {
                    'points': np.array(points, dtype=np.float32),
                    'weights': np.array(vg_weights[name], dtype=np.float32),
                    'centroid': np.mean(points, axis=0)
                }
        return result
    
    def calculate_chamfer_distance(self, points_a: np.ndarray, points_b: np.ndarray) -> float:
        """计算双向 Chamfer 距离（分块计算避免内存溢出）"""
        def min_distances(pa, pb):
            chunks = []
            for start in range(0, len(pa), self.chunk_size):
                end = min(start + self.chunk_size, len(pa))
                diff = pa[start:end, None, :] - pb[None, :, :]
                dist = np.min(np.linalg.norm(diff, axis=2), axis=1)
                chunks.append(dist)
            return np.concatenate(chunks)
        
        if len(points_a) == 0 or len(points_b) == 0:
            return float('inf')
        
        dist_ab = min_distances(points_a, points_b)
        dist_ba = min_distances(points_b, points_a)
        
        return float(np.mean(dist_ab) + np.mean(dist_ba))
    
    def build_kdtree(self, vg_data: Dict) -> Tuple[kdtree.KDTree, List[str]]:
        """构建顶点组质心的 KD-Tree"""
        names = list(vg_data.keys())
        size = len(names)
        kd = kdtree.KDTree(size)
        
        for i, name in enumerate(names):
            centroid = vg_data[name]['centroid']
            kd.insert(centroid, i)
        
        kd.balance()
        return kd, names
    
    def match(self, source_vg: Dict, target_vg: Dict, threshold: float = 0.1) -> Dict[str, Tuple[str, float]]:
        """
        匹配源物体和目标物体的顶点组
        
        Args:
            source_vg: 源物体顶点组数据 {name: {'points': ndarray, 'weights': ndarray, 'centroid': ndarray}}
            target_vg: 目标物体顶点组数据
            threshold: Chamfer 距离阈值
            
        Returns:
            Dict[str, Tuple[str, float]]: {源顶点组名: (目标顶点组名, Chamfer距离)}
        """
        kd_tree, target_names = self.build_kdtree(target_vg)
        mapping = {}
        
        for src_name, src_data in source_vg.items():
            src_centroid = Vector(src_data['centroid'])
            src_points = src_data['points']
            
            found = kd_tree.find_range(src_centroid, threshold * 10)
            
            if not found:
                continue
            
            candidates = sorted(found, key=lambda x: x[2])[:self.candidates_count]
            
            best_match = None
            best_distance = float('inf')
            
            for _, idx, _ in candidates:
                tgt_name = target_names[idx]
                tgt_points = target_vg[tgt_name]['points']
                
                chamfer_dist = self.calculate_chamfer_distance(src_points, tgt_points)
                
                if chamfer_dist < best_distance and chamfer_dist < threshold:
                    best_distance = chamfer_dist
                    best_match = tgt_name
            
            if best_match:
                mapping[src_name] = (best_match, best_distance)
        
        return mapping


class SSMTNode_VertexGroupMatch(SSMTNodeBase):
    '''顶点组名称匹配节点：基于位置匹配源物体和目标物体的顶点组，生成映射表'''
    bl_idname = 'SSMTNode_VertexGroupMatch'
    bl_label = '顶点组匹配'
    bl_description = '基于位置匹配源物体和目标物体的顶点组，生成映射表保存到文本编辑器'
    bl_icon = 'GROUP'
    bl_width_min = 300

    source_object: bpy.props.StringProperty(
        name="源物体",
        description="源物体名称",
        default=""
    )

    source_collection: bpy.props.StringProperty(
        name="源合集",
        description="源合集名称，设置后会将合集下所有网格物体作为源物体参与匹配",
        default=""
    )

    runtime_source_object: bpy.props.StringProperty(
        name="临时源物体",
        description="合集模式下用于计算和权重预览的持久临时合并物体",
        default=""
    )

    target_object: bpy.props.StringProperty(
        name="目标物体",
        description="目标物体名称",
        default=""
    )

    match_threshold: bpy.props.FloatProperty(
        name="匹配阈值",
        description="顶点组中心位置的匹配距离阈值",
        default=0.01,
        min=0.001,
        max=1.0
    )

    use_shape_key: bpy.props.BoolProperty(
        name="使用形态键",
        description="计算顶点组中心时考虑形态键变形",
        default=False
    )

    chamfer_threshold: bpy.props.FloatProperty(
        name="Chamfer阈值",
        description="Chamfer距离阈值，用于精确匹配筛选",
        default=0.1,
        min=0.01,
        max=10.0
    )

    candidates_count: bpy.props.IntProperty(
        name="候选数量",
        description="KD-Tree预筛选后保留的候选数量",
        default=3,
        min=1,
        max=10
    )

    use_chamfer_matching: bpy.props.BoolProperty(
        name="使用Chamfer匹配",
        description="开启后使用Chamfer距离进行精确匹配，关闭则仅使用中心点距离",
        default=True
    )

    create_debug_objects: bpy.props.BoolProperty(
        name="创建调试物体",
        description="创建调试物体和连接线用于可视化匹配结果",
        default=True
    )

    rename_format: bpy.props.BoolProperty(
        name="重命名格式",
        description="使用 源名称=目标名称 格式",
        default=True
    )

    target_hash: bpy.props.StringProperty(
        name="目标哈希",
        description="应用此映射表的物体哈希标识（物体名称以'哈希-'开头时匹配），留空则应用于所有物体",
        default=""
    )

    exact_hash_match: bpy.props.BoolProperty(
        name="全匹配优先",
        description="开启后此映射表具有更高优先级，且被匹配的物体不会再被后续映射表处理",
        default=False
    )

    mapping_text_name: bpy.props.StringProperty(
        name="映射表名称",
        description="当前节点生成的映射表文本名称",
        default=""
    )

    source_mapping_applied: bpy.props.BoolProperty(
        name="已应用到原物体",
        description="当前映射表是否已直接应用到源物体顶点组名称",
        default=False
    )

    source_mapping_backup: bpy.props.StringProperty(
        name="原顶点组备份",
        description="用于撤回时恢复原始顶点组名称",
        default=""
    )

    source_mapping_backup_object: bpy.props.StringProperty(
        name="备份物体名",
        description="记录已应用映射的源物体名称",
        default=""
    )

    debug_link_id: bpy.props.StringProperty(
        name="调试链接ID",
        description="唯一标识，用于关联节点与调试物体",
        default=""
    )

    def init(self, context):
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 300

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text="顶点组匹配设置", icon='GROUP')

        if self.source_collection:
            box.label(text="当前使用源合集模式，优先于单物体", icon='OUTLINER_COLLECTION')
        
        row = box.row()
        row.prop_search(self, "source_collection", bpy.data, "collections", text="源合集")

        row = box.row()
        row.prop_search(self, "source_object", bpy.data, "objects", text="源物体")
        
        row = box.row()
        row.prop_search(self, "target_object", bpy.data, "objects", text="目标物体")
        
        row = box.row()
        row.prop(self, "match_threshold")
        
        row = box.row()
        row.prop(self, "use_chamfer_matching")
        
        if self.use_chamfer_matching:
            row = box.row()
            row.prop(self, "chamfer_threshold")
            
            row = box.row()
            row.prop(self, "candidates_count")
        
        row = box.row()
        row.prop(self, "use_shape_key")
        
        row = box.row()
        row.prop(self, "create_debug_objects")
        
        row = box.row()
        row.prop(self, "rename_format")
        
        row = box.row()
        row.prop(self, "target_hash")
        
        row = box.row()
        row.prop(self, "exact_hash_match")

        if self.mapping_text_name:
            mapping_box = layout.box()
            text = bpy.data.texts.get(self.mapping_text_name)
            if text:
                entry_count = 0
                for line in text.lines:
                    clean_line = re.sub(r'[#//].*', '', line.body).strip()
                    if '=' in clean_line and clean_line:
                        entry_count += 1
                mapping_box.label(text=f"映射表: {self.mapping_text_name}", icon='TEXT')
                mapping_box.label(text=f"条目数: {entry_count}", icon='INFO')
            else:
                mapping_box.label(text=f"映射表: {self.mapping_text_name} (文本已丢失)", icon='ERROR')
        
        layout.separator()
        row = layout.row(align=True)
        op = row.operator("ssmt.vertex_group_match_execute", text="执行匹配", icon='PLAY')
        op.node_name = self.name
        op = row.operator("ssmt.vertex_group_match_clear", text="清除映射", icon='X')
        op.node_name = self.name

        row = layout.row(align=True)
        apply_text = "撤回应用" if self.source_mapping_applied else "应用到原物体"
        apply_icon = 'LOOP_BACK' if self.source_mapping_applied else 'CHECKMARK'
        op = row.operator("ssmt.vertex_group_match_apply_to_source", text=apply_text, icon=apply_icon)
        op.node_name = self.name
        
        layout.separator()
        row = layout.row(align=True)
        op = row.operator("ssmt.vertex_group_match_toggle_debug", text="显示/隐藏调试", icon='HIDE_OFF')
        op.node_name = self.name
        
        layout.separator()
        row = layout.row(align=True)
        op = row.operator("ssmt.vertex_group_match_sync", text="同步选中", icon='LINKED')
        op.node_name = self.name
        op = row.operator("ssmt.vertex_group_match_delete_connection", text="删除连接", icon='X')
        op.node_name = self.name
        
        layout.separator()
        row = layout.row(align=True)
        op = row.operator("ssmt.vertex_group_match_detect_multi", text="检测多连接", icon='VIEWZOOM')
        op.node_name = self.name
        
        layout.separator()
        box = layout.box()
        box.label(text="快捷键:", icon='KEYINGSET')
        box.label(text="手动同步: Ctrl+Shift+Z")
        box.label(text="快速权重: Alt+W")

    def get_deformed_vertices(self, context, obj):
        """获取经过骨骼和形态键变换后的顶点世界坐标"""
        if not obj or obj.type != 'MESH':
            return None
        
        depsgraph = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        
        if not eval_obj or not eval_obj.data:
            return None
        
        vertices = []
        for vert in eval_obj.data.vertices:
            world_pos = eval_obj.matrix_world @ vert.co
            vertices.append(world_pos)
        
        return vertices

    def get_vertex_group_centers(self, context, obj, deformed_vertices=None):
        """获取物体所有顶点组的中心位置"""
        if not obj or obj.type != 'MESH':
            return {}

        mesh = obj.data
        group_centers = {}
        group_counts = {}

        for vg in obj.vertex_groups:
            original_name, _ = self.parse_vg_name(vg.name)
            group_centers[original_name] = Vector((0, 0, 0))
            group_counts[original_name] = 0

        for i, vert in enumerate(mesh.vertices):
            global_pos = deformed_vertices[i] if deformed_vertices else obj.matrix_world @ vert.co
            for vgroup in vert.groups:
                try:
                    vg = obj.vertex_groups[vgroup.group]
                    original_vg_name, _ = self.parse_vg_name(vg.name)
                    weight = vgroup.weight
                    if weight > 0 and original_vg_name in group_centers:
                        group_centers[original_vg_name] += global_pos * weight
                        group_counts[original_vg_name] += weight
                except (IndexError, RuntimeError):
                    continue

        result = {}
        for name, center in group_centers.items():
            if group_counts[name] > 0:
                result[name] = {'center': center / group_counts[name]}
        return result

    def parse_vg_name(self, name):
        """解析顶点组名称"""
        if self.rename_format and "=" in name:
            parts = name.split("=", 1)
            return parts[0].strip(), parts[1].strip()
        return name, name

    def get_source_scope_label(self):
        if self.source_collection:
            return f"源合集: {self.source_collection}"
        if self.source_object:
            return f"源物体: {self.source_object}"
        return "源物体未设置"

    def get_source_mesh_objects(self):
        """解析当前节点配置的源物体集合。设置源合集时优先使用合集。"""
        if self.source_collection:
            source_collection = bpy.data.collections.get(self.source_collection)
            if not source_collection:
                return [], f"未找到源合集: {self.source_collection}"

            source_objects = [
                obj for obj in source_collection.all_objects
                if obj.type == 'MESH' and obj.data
            ]
            if not source_objects:
                return [], f"源合集 '{self.source_collection}' 下没有可用的网格物体"
            return source_objects, ""

        source_obj = bpy.data.objects.get(self.source_object)
        if not source_obj:
            return [], "未找到源物体"
        if source_obj.type != 'MESH' or not source_obj.data:
            return [], "源物体必须是带网格数据的网格物体"
        return [source_obj], ""

    @staticmethod
    def _merge_source_objects_for_match(context, source_objects, temp_prefix="SSMT_VGMatchTemp", force_temp=False, object_name=""):
        valid_sources = [obj for obj in source_objects if obj and obj.type == 'MESH' and obj.data]
        if len(valid_sources) == 1 and not force_temp:
            return valid_sources[0], False

        if not valid_sources:
            return None, False

        depsgraph = context.evaluated_depsgraph_get()
        merged_name = object_name or f"{temp_prefix}_{uuid.uuid4().hex[:8]}"
        merged_mesh = bpy.data.meshes.new(f"{merged_name}_mesh")
        merged_obj = bpy.data.objects.new(merged_name, merged_mesh)
        merged_obj["ssmt_temp_vg_match"] = True
        merged_obj["ssmt_temp_source_names"] = ",".join(obj.name for obj in valid_sources)
        merged_obj.hide_render = True
        merged_obj.display_type = 'WIRE'

        vertices = []
        edges = []
        faces = []
        group_assignments = defaultdict(list)
        vertex_offset = 0

        for source_obj in valid_sources:
            evaluated_obj = source_obj.evaluated_get(depsgraph)
            evaluated_mesh = evaluated_obj.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
            try:
                if not evaluated_mesh or len(evaluated_mesh.vertices) == 0:
                    continue

                source_group_names = {vg.index: vg.name for vg in source_obj.vertex_groups}
                vertex_count = len(evaluated_mesh.vertices)

                for vert in evaluated_mesh.vertices:
                    vertices.append(tuple((evaluated_obj.matrix_world @ vert.co)[:]))

                for edge in evaluated_mesh.edges:
                    edges.append((
                        vertex_offset + edge.vertices[0],
                        vertex_offset + edge.vertices[1],
                    ))

                for polygon in evaluated_mesh.polygons:
                    faces.append([
                        vertex_offset + vertex_index
                        for vertex_index in polygon.vertices
                    ])

                source_vertices = source_obj.data.vertices
                if len(source_vertices) != vertex_count:
                    source_vertices = source_obj.data.vertices[:min(len(source_obj.data.vertices), vertex_count)]

                for vert_index in range(min(len(source_vertices), vertex_count)):
                    source_vert = source_vertices[vert_index]
                    merged_index = vertex_offset + vert_index
                    for group_elem in source_vert.groups:
                        group_name = source_group_names.get(group_elem.group)
                        if not group_name or group_elem.weight <= 0:
                            continue
                        group_assignments[group_name].append((merged_index, group_elem.weight))

                vertex_offset += vertex_count
            finally:
                evaluated_obj.to_mesh_clear()

        if not vertices:
            bpy.data.objects.remove(merged_obj, do_unlink=True)
            bpy.data.meshes.remove(merged_mesh, do_unlink=True)
            return None, False

        merged_mesh.from_pydata(vertices, edges, faces)
        merged_mesh.update()
        merged_mesh.validate(verbose=False)
        context.scene.collection.objects.link(merged_obj)

        for group_name, assignments in group_assignments.items():
            vertex_group = merged_obj.vertex_groups.new(name=group_name)
            for vert_index, weight in assignments:
                vertex_group.add([vert_index], weight, 'REPLACE')

        return merged_obj, True

    @staticmethod
    def _remove_temp_object(obj):
        if not obj:
            return

        mesh_data = obj.data if getattr(obj, 'type', None) == 'MESH' else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh_data and mesh_data.users == 0:
            bpy.data.meshes.remove(mesh_data, do_unlink=True)

    def _get_runtime_source_object_base_name(self):
        node_tree_name = self.id_data.name if getattr(self, 'id_data', None) else "Blueprint"
        safe_tree_name = re.sub(r'[^A-Za-z0-9_]+', '_', node_tree_name)[:24] or "Blueprint"
        safe_node_name = re.sub(r'[^A-Za-z0-9_]+', '_', self.name)[:24] or "VertexGroupMatch"
        return f"SSMT_VGMatchRuntime_{safe_tree_name}_{safe_node_name}"

    def get_runtime_source_object(self):
        if not self.runtime_source_object:
            return None
        return bpy.data.objects.get(self.runtime_source_object)

    def clear_runtime_source_object(self):
        runtime_obj = self.get_runtime_source_object()
        if runtime_obj:
            self._remove_temp_object(runtime_obj)
        self.runtime_source_object = ""

    def ensure_runtime_source_object(self, context, source_objects):
        runtime_name = self.runtime_source_object or self._get_runtime_source_object_base_name()
        existing_runtime = bpy.data.objects.get(runtime_name)
        if existing_runtime:
            self._remove_temp_object(existing_runtime)

        runtime_obj, is_temp_object = self._merge_source_objects_for_match(
            context,
            source_objects,
            temp_prefix=self._get_runtime_source_object_base_name(),
            force_temp=True,
            object_name=runtime_name,
        )
        if not runtime_obj:
            return None, False

        runtime_obj["ssmt_vg_match_persistent"] = True
        runtime_obj["ssmt_vg_match_node_name"] = self.name
        runtime_obj["ssmt_vg_match_node_tree"] = self.id_data.name if getattr(self, 'id_data', None) else ""
        runtime_obj["ssmt_vg_match_source_collection"] = self.source_collection
        runtime_obj.hide_render = True
        runtime_obj.display_type = 'WIRE'
        self.runtime_source_object = runtime_obj.name
        return runtime_obj, is_temp_object

    def build_runtime_source_object(self, context):
        source_objects, error_message = self.get_source_mesh_objects()
        if error_message:
            return None, False, error_message

        if self.source_collection:
            source_obj, is_temp_object = self.ensure_runtime_source_object(context, source_objects)
        else:
            source_obj, is_temp_object = self._merge_source_objects_for_match(context, source_objects)
            if self.runtime_source_object:
                self.clear_runtime_source_object()

        if not source_obj:
            return None, False, "源物体合并失败，无法执行顶点组匹配"

        return source_obj, is_temp_object, ""

    def get_mapping_dict(self):
        """从当前节点关联的映射文本读取映射字典。"""
        if not self.mapping_text_name:
            return {}

        text = bpy.data.texts.get(self.mapping_text_name)
        if not text:
            return {}

        mapping = {}
        for line in text.lines:
            clean_line = re.sub(r'[#//].*', '', line.body).strip()
            if '=' not in clean_line:
                continue

            left, right = clean_line.split('=', 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                mapping[left] = right

        return mapping

    @staticmethod
    def _build_temp_vertex_group_name(index):
        return f"__SSMT_VG_TMP_{index:04d}__"

    def _rename_vertex_groups_by_index(self, obj, target_names_by_index):
        """两阶段重命名，避免 A->B、B->C 这类冲突导致的自动重名。"""
        vertex_groups = obj.vertex_groups
        rename_indices = [
            index for index, target_name in target_names_by_index.items()
            if 0 <= index < len(vertex_groups) and vertex_groups[index].name != target_name
        ]

        if not rename_indices:
            return 0

        for index in rename_indices:
            vertex_groups[index].name = self._build_temp_vertex_group_name(index)

        for index in rename_indices:
            vertex_groups[index].name = target_names_by_index[index]

        return len(rename_indices)

    def _collect_vertex_group_weights(self, obj):
        index_to_name = {vg.index: vg.name for vg in obj.vertex_groups}
        weights_by_name = {vg.name: defaultdict(float) for vg in obj.vertex_groups}

        for vert in obj.data.vertices:
            for group_elem in vert.groups:
                group_name = index_to_name.get(group_elem.group)
                if not group_name or group_elem.weight <= 0:
                    continue
                weights_by_name[group_name][vert.index] += group_elem.weight

        return weights_by_name

    def _create_vertex_group_weight_snapshot(self, obj):
        group_map = {str(vg.index): vg.name for vg in obj.vertex_groups}
        vertex_weights = defaultdict(list)

        for vertex in obj.data.vertices:
            for group_elem in vertex.groups:
                if group_elem.weight > 1e-6:
                    vertex_weights[str(vertex.index)].append([group_elem.group, group_elem.weight])

        return {
            'group_map': group_map,
            'vertex_weights': dict(vertex_weights),
        }

    @staticmethod
    def _remove_all_vertex_groups(obj):
        for vertex_group in reversed(list(obj.vertex_groups)):
            try:
                obj.vertex_groups.remove(vertex_group)
            except Exception:
                pass

    @staticmethod
    def _apply_weight_maps_to_object(obj, ordered_group_names, weights_by_name):
        for group_name in ordered_group_names:
            vertex_group = obj.vertex_groups.new(name=group_name)
            weight_to_vertices = defaultdict(list)
            for vert_idx, weight in weights_by_name.get(group_name, {}).items():
                clamped_weight = min(1.0, weight)
                if clamped_weight > 0:
                    weight_to_vertices[clamped_weight].append(vert_idx)

            for weight, vert_indices in weight_to_vertices.items():
                vertex_group.add(vert_indices, weight, 'REPLACE')

    def _clean_non_numeric_vertex_groups(self, obj):
        """清理非数字名称的顶点组"""
        groups_to_remove = [vg for vg in obj.vertex_groups if not vg.name.isdigit()]
        for vg in reversed(groups_to_remove):
            try:
                obj.vertex_groups.remove(vg)
            except Exception:
                pass
        return len(groups_to_remove)

    def _fill_vertex_group_gaps(self, obj):
        """填充缺失的数字顶点组"""
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
        """按数字顺序排序顶点组"""
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
            print(f"[VertexGroupMatch] 排序顶点组失败: {e}")

    def _remove_backup_objects_from_payload(self, backup_payload):
        return

    def _apply_mapping_with_merge(self, source_obj, mapping):
        ordered_final_names = []
        final_name_by_original = {}

        for vertex_group in source_obj.vertex_groups:
            original_name, mapped_name = self.parse_vg_name(vertex_group.name)
            final_name = mapping.get(original_name) or mapping.get(mapped_name) or vertex_group.name
            final_name_by_original[vertex_group.name] = final_name
            if final_name not in ordered_final_names:
                ordered_final_names.append(final_name)

        if not ordered_final_names:
            return 0, 0

        original_weights = self._collect_vertex_group_weights(source_obj)
        merged_weights = {group_name: defaultdict(float) for group_name in ordered_final_names}
        changed_group_count = 0

        for original_name, weights in original_weights.items():
            final_name = final_name_by_original.get(original_name, original_name)
            if final_name != original_name:
                changed_group_count += 1
            for vert_idx, weight in weights.items():
                merged_weights[final_name][vert_idx] += weight

        merged_group_count = max(0, len(source_obj.vertex_groups) - len(ordered_final_names))

        self._remove_all_vertex_groups(source_obj)
        self._apply_weight_maps_to_object(source_obj, ordered_final_names, merged_weights)

        return changed_group_count, merged_group_count

    def _restore_vertex_groups_from_snapshot(self, source_obj, snapshot):
        if not isinstance(snapshot, dict):
            return False, f"源物体 '{source_obj.name}' 的权重备份格式无效"

        group_map = snapshot.get('group_map', {})
        vertex_weights = snapshot.get('vertex_weights', {})
        if not isinstance(group_map, dict) or not isinstance(vertex_weights, dict):
            return False, f"源物体 '{source_obj.name}' 的权重备份格式无效"

        self._remove_all_vertex_groups(source_obj)

        ordered_group_names = []
        for group_index in sorted(group_map.keys(), key=lambda value: int(value) if str(value).isdigit() else str(value)):
            group_name = group_map[group_index]
            if group_name not in ordered_group_names:
                ordered_group_names.append(group_name)
                source_obj.vertex_groups.new(name=group_name)

        current_vg_map = {vg.name: vg for vg in source_obj.vertex_groups}

        for vert_idx_str, weights in vertex_weights.items():
            try:
                vert_idx = int(vert_idx_str)
            except (TypeError, ValueError):
                continue

            if vert_idx < 0 or vert_idx >= len(source_obj.data.vertices):
                continue

            for group_idx, weight in weights:
                group_name = group_map.get(str(group_idx))
                if not group_name:
                    group_name = group_map.get(group_idx)
                if not group_name:
                    continue

                vertex_group = current_vg_map.get(group_name)
                if vertex_group and weight > 1e-6:
                    vertex_group.add([vert_idx], min(1.0, weight), 'ADD')

        return True, ""

    def apply_mapping_to_source_object(self):
        """将映射表直接应用到源物体或源合集下所有物体的顶点组名称。"""
        if self.source_mapping_applied:
            return False, "映射已经处于应用状态"

        source_objects, error_message = self.get_source_mesh_objects()
        if error_message:
            return False, error_message

        mapping = self.get_mapping_dict()
        if not mapping:
            return False, "未找到可用映射表，请先执行匹配"

        backup_payload = {
            'source_object': self.source_object,
            'source_collection': self.source_collection,
            'objects': {}
        }
        renamed_count = 0
        merged_count = 0
        cleaned_count = 0
        filled_count = 0
        renamed_objects = 0

        for source_obj in source_objects:
            will_change = False
            for vertex_group in source_obj.vertex_groups:
                original_name, mapped_name = self.parse_vg_name(vertex_group.name)
                if original_name in mapping or mapped_name in mapping:
                    will_change = True
                    break

            if not will_change:
                continue

            backup_payload['objects'][source_obj.name] = {
                'weight_snapshot': self._create_vertex_group_weight_snapshot(source_obj),
            }

            changed_group_count, merged_group_count = self._apply_mapping_with_merge(source_obj, mapping)
            renamed_count += changed_group_count
            merged_count += merged_group_count

            cleaned = self._clean_non_numeric_vertex_groups(source_obj)
            cleaned_count += cleaned

            filled = self._fill_vertex_group_gaps(source_obj)
            filled_count += filled

            self._sort_vertex_groups(source_obj)

            renamed_objects += 1

        if renamed_count == 0 and cleaned_count == 0 and filled_count == 0:
            self._remove_backup_objects_from_payload(backup_payload)
            return False, "源物体或源合集中没有顶点组命中当前映射表"

        self.source_mapping_backup = json.dumps(backup_payload, ensure_ascii=False)
        self.source_mapping_backup_object = self.source_collection or self.source_object
        self.source_mapping_applied = True
        scope_label = self.get_source_scope_label()

        message_parts = [f"已将映射表应用到{scope_label}，共处理 {renamed_objects} 个物体"]
        if renamed_count > 0:
            message_parts.append(f"重命名 {renamed_count} 个顶点组")
        if merged_count > 0:
            message_parts.append(f"合并 {merged_count} 组同名顶点组")
        if cleaned_count > 0:
            message_parts.append(f"清理 {cleaned_count} 个非数字组")
        if filled_count > 0:
            message_parts.append(f"填充 {filled_count} 个缺失组")

        return True, "，".join(message_parts)

    def revert_mapping_on_source_object(self):
        """撤回映射应用，恢复源物体原始顶点组名称。"""
        if not self.source_mapping_applied:
            return False, "当前没有可撤回的应用"

        try:
            backup_payload = json.loads(self.source_mapping_backup) if self.source_mapping_backup else {}
        except json.JSONDecodeError:
            return False, "原始顶点组备份已损坏，无法撤回"

        object_backups = backup_payload.get('objects', {}) if isinstance(backup_payload, dict) else {}
        if not object_backups:
            return False, "未找到可撤回的物体备份信息"

        restored_count = 0
        restored_objects = 0

        for object_name, object_info in object_backups.items():
            source_obj = bpy.data.objects.get(object_name)
            if not source_obj:
                return False, f"找不到已应用映射的源物体: {object_name}"
            if source_obj.type != 'MESH':
                return False, f"备份对应的源物体不是网格物体: {object_name}"

            snapshot = object_info.get('weight_snapshot') if isinstance(object_info, dict) else None
            if not snapshot:
                return False, f"找不到源物体 '{object_name}' 的顶点组权重备份，无法撤回"

            success, error_message = self._restore_vertex_groups_from_snapshot(source_obj, snapshot)
            if not success:
                return False, error_message

            group_map = snapshot.get('group_map', {}) if isinstance(snapshot, dict) else {}
            restored_count += len(set(group_map.values())) if isinstance(group_map, dict) else 0
            restored_objects += 1

        self._remove_backup_objects_from_payload(backup_payload)

        self.source_mapping_applied = False
        self.source_mapping_backup = ""
        self.source_mapping_backup_object = ""
        scope_label = self.get_source_scope_label()
        return True, f"已撤回{scope_label}上的映射应用，恢复 {restored_objects} 个物体的 {restored_count} 个顶点组名称"

    @staticmethod
    def get_or_create_debug_material(mat_name, color):
        mat = bpy.data.materials.get(mat_name)
        if not mat:
            mat = bpy.data.materials.new(name=mat_name)
        mat.diffuse_color = color
        mat.use_nodes = True
        principled = mat.node_tree.nodes.get("Principled BSDF")
        if not principled:
            principled = mat.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
            output = mat.node_tree.nodes.get("Material Output")
            if output:
                mat.node_tree.links.new(principled.outputs['BSDF'], output.inputs['Surface'])
        principled.inputs['Base Color'].default_value = color
        principled.inputs['Roughness'].default_value = 0.4
        return mat

    def create_debug_object(self, name, location, parent, is_connected=False, distance=None):
        """创建调试物体"""
        is_source = name.startswith("Source_")

        if is_source:
            mesh = bpy.data.meshes.new(name + "_mesh")
            bm = bmesh.new()
            
            if is_connected:
                bmesh.ops.create_cube(bm, size=0.005)
                mat_name, color = "VGTP_Debug_Blue", (0.0, 0.2, 1.0, 1.0)
            else:
                bmesh.ops.create_cube(bm, size=0.006)
                mat_name, color = "VGTP_Debug_Green", (0.0, 1.0, 0.2, 1.0)
                
            bm.to_mesh(mesh)
            bm.free()
        else:
            mesh = bpy.data.meshes.new(name + "_mesh")
            bm = bmesh.new()
            bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=(0.005 / 2))
            bm.to_mesh(mesh)
            bm.free()
            mat_name, color = "VGTP_Debug_Yellow", (1.0, 1.0, 0.0, 1.0)

        mat = self.get_or_create_debug_material(mat_name, color)

        debug_obj = bpy.data.objects.new(name, mesh)
        debug_obj.location = location
        debug_obj.data.materials.append(mat)
        debug_obj.parent = parent
        debug_obj["original_vg_name"] = name.split('_', 1)[1]
        debug_obj["is_connected"] = is_connected
        if distance is not None:
            debug_obj["chamfer_distance"] = distance
        debug_obj.show_name = True
        bpy.context.scene.collection.objects.link(debug_obj)
        return debug_obj

    def create_match_curves(self, source_centers, target_centers, rename_map, parent, 
                           one_to_many_connections=None, target_to_sources=None, distances=None):
        """创建匹配连接线"""
        if one_to_many_connections is None:
            one_to_many_connections = {}
        if target_to_sources is None:
            target_to_sources = {}
        if distances is None:
            distances = {}
        
        for src_name, src_data in source_centers.items():
            if src_name in rename_map:
                new_name = rename_map[src_name]
                target_name = new_name
                if self.rename_format and "=" in new_name:
                    target_name = new_name.split("=", 1)[1].strip()

                if target_name in target_centers:
                    src_center = src_data['center']
                    target_center = target_centers[target_name]['center']

                    curve_data = bpy.data.curves.new(f"Match_{src_name}_to_{target_name}", 'CURVE')
                    curve_data.dimensions = '3D'
                    curve_data.bevel_depth = 0.001
                    curve_data.bevel_resolution = 2

                    polyline = curve_data.splines.new('POLY')
                    polyline.points.add(1)
                    polyline.points[0].co = (*src_center, 1)
                    polyline.points[1].co = (*target_center, 1)

                    curve_obj = bpy.data.objects.new(f"Match_{src_name}_to_{target_name}", curve_data)
                    curve_obj.parent = parent
                    
                    is_one_to_many = src_name in one_to_many_connections and len(one_to_many_connections[src_name]) > 1
                    
                    if is_one_to_many:
                        mat_name = "VGTP_Debug_Red_Error"
                        mat = self.get_or_create_debug_material(mat_name, (1.0, 0.0, 0.0, 1.0))
                        curve_obj.data.materials.clear()
                        curve_obj.data.materials.append(mat)
                        curve_obj["is_one_to_many_connection"] = True
                        curve_obj["connected_targets"] = str(one_to_many_connections[src_name])
                    else:
                        mat_name = "VGTP_Debug_White_Normal"
                        mat = self.get_or_create_debug_material(mat_name, (1.0, 1.0, 1.0, 1.0))
                        curve_obj.data.materials.clear()
                        curve_obj.data.materials.append(mat)
                        curve_obj["is_one_to_many_connection"] = False
                    
                    if src_name in distances:
                        curve_obj["chamfer_distance"] = distances[src_name]
                    
                    bpy.context.scene.collection.objects.link(curve_obj)

    def optimize_matching(self, target_centers):
        """构建KD树优化匹配"""
        size = len(target_centers)
        kd = kdtree.KDTree(size)
        positions = []
        for i, (name, data) in enumerate(target_centers.items()):
            kd.insert(data['center'], i)
            positions.append((name, data['center']))
        kd.balance()
        return kd, positions

    def execute_match(self, context):
        """执行顶点组匹配"""
        source_obj, source_is_temp, source_error = self.build_runtime_source_object(context)
        target_obj = bpy.data.objects.get(self.target_object)

        if source_error:
            return None, source_error

        if not source_obj or not target_obj:
            return None, "请同时指定源物体/源合集和目标物体"
        
        if source_obj.type != 'MESH' or target_obj.type != 'MESH':
            return None, "源和目标都必须是网格物体"

        debug_parent = None
        source_scope_label = self.get_source_scope_label()

        if self.create_debug_objects:
            if not self.debug_link_id:
                self.debug_link_id = str(uuid.uuid4())
            parent_name = f"Debug_Match_{target_obj.name}_{int(time.time())}"
            debug_parent = bpy.data.objects.new(parent_name, None)
            context.scene.collection.objects.link(debug_parent)
            debug_parent["vgtp_debug_link_id"] = self.debug_link_id
            debug_parent["vgtp_node_name"] = self.name
            debug_parent["vgtp_node_tree"] = self.id_data.name if getattr(self, 'id_data', None) else ""
            debug_parent["vgtp_source_name"] = self.source_object
            debug_parent["vgtp_source_collection"] = self.source_collection
            debug_parent["vgtp_source_scope_label"] = source_scope_label
            debug_parent["vgtp_runtime_source_object"] = source_obj.name if source_is_temp else ""
            debug_parent["vgtp_target_name"] = target_obj.name

        source_deformed_data = self.get_deformed_vertices(context, source_obj) if self.use_shape_key else None
        target_deformed_data = self.get_deformed_vertices(context, target_obj) if self.use_shape_key else None
        
        source_centers = self.get_vertex_group_centers(context, source_obj, source_deformed_data)
        target_centers = self.get_vertex_group_centers(context, target_obj, target_deformed_data)

        if not source_centers or not target_centers:
            if debug_parent:
                bpy.data.objects.remove(debug_parent, do_unlink=True)
            return None, "物体缺少有效的顶点组"

        source_debug_objects = {}
        if self.create_debug_objects:
            for name, data in source_centers.items():
                debug_obj = self.create_debug_object(f"Source_{name}", data['center'], debug_parent, is_connected=False)
                source_debug_objects[name] = debug_obj
                
            for name, data in target_centers.items():
                self.create_debug_object(f"Target_{name}", data['center'], debug_parent, is_connected=False)

        rename_map = {}
        target_to_sources = {}
        matched_count = 0
        distances = {}

        if self.use_chamfer_matching:
            matcher = VertexGroupMatcherOptimized(
                candidates_count=self.candidates_count,
                chunk_size=256
            )
            
            source_positions = matcher.get_vertex_positions(source_obj, self.use_shape_key, context)
            target_positions = matcher.get_vertex_positions(target_obj, self.use_shape_key, context)
            
            source_vg_data = matcher.get_vg_point_clouds(source_obj, source_positions)
            target_vg_data = matcher.get_vg_point_clouds(target_obj, target_positions)
            
            chamfer_mapping = matcher.match(source_vg_data, target_vg_data, self.chamfer_threshold)
            
            for src_name, (tgt_name, chamfer_dist) in chamfer_mapping.items():
                new_name = f"{src_name}={tgt_name}" if self.rename_format else tgt_name
                rename_map[src_name] = new_name
                distances[src_name] = chamfer_dist
                matched_count += 1
                
                if tgt_name not in target_to_sources:
                    target_to_sources[tgt_name] = []
                target_to_sources[tgt_name].append(src_name)
                
                if self.create_debug_objects and src_name in source_debug_objects:
                    source_debug_objects[src_name]["is_connected"] = True
                    source_debug_objects[src_name]["chamfer_distance"] = chamfer_dist
                    mat_name = "VGTP_Debug_Blue"
                    mat = self.get_or_create_debug_material(mat_name, (0.0, 0.2, 1.0, 1.0))
                    source_debug_objects[src_name].data.materials.clear()
                    source_debug_objects[src_name].data.materials.append(mat)
        else:
            kd_tree, positions = self.optimize_matching(target_centers)
            
            for src_name, src_data in source_centers.items():
                src_center = src_data['center']
                found_targets = kd_tree.find_range(src_center, self.match_threshold)
                
                if found_targets:
                    _, index, _ = found_targets[0]
                    closest_name = positions[index][0]
                    new_name = f"{src_name}={closest_name}" if self.rename_format else closest_name
                    rename_map[src_name] = new_name
                    matched_count += 1
                    
                    if closest_name not in target_to_sources:
                        target_to_sources[closest_name] = []
                    target_to_sources[closest_name].append(src_name)
                    
                    if self.create_debug_objects and src_name in source_debug_objects:
                        source_debug_objects[src_name]["is_connected"] = True
                        mat_name = "VGTP_Debug_Blue"
                        mat = self.get_or_create_debug_material(mat_name, (0.0, 0.2, 1.0, 1.0))
                        source_debug_objects[src_name].data.materials.clear()
                        source_debug_objects[src_name].data.materials.append(mat)

        if self.create_debug_objects and matched_count > 0:
            one_to_many_connections = {}
            self.create_match_curves(source_centers, target_centers, rename_map, debug_parent, one_to_many_connections, target_to_sources, distances)
            
            many_to_one_count = 0
            for target_name, sources in target_to_sources.items():
                if len(sources) > 1:
                    many_to_one_count += 1

        base_text_name = f"VG_Match_{target_obj.name}"
        if len(base_text_name) > 63:
            import hashlib
            hash_suffix = hashlib.md5(target_obj.name.encode()).hexdigest()[:8]
            base_text_name = f"VG_Match_{hash_suffix}"
        
        text_name = self._get_unique_mapping_text_name(base_text_name)
        
        if text_name in bpy.data.texts:
            text = bpy.data.texts[text_name]
            text.clear()
        else:
            text = bpy.data.texts.new(text_name)

        text.write(f"# 顶点组名称映射表\n")
        text.write(f"# {source_scope_label}\n")
        text.write(f"# 用于计算的源物体: {source_obj.name}\n")
        text.write(f"# 目标物体: {target_obj.name}\n")
        text.write(f"# 匹配阈值: {self.match_threshold}\n")
        text.write(f"# 使用Chamfer匹配: {self.use_chamfer_matching}\n")
        if self.use_chamfer_matching:
            text.write(f"# Chamfer阈值: {self.chamfer_threshold}\n")
        text.write(f"# 匹配时间: {int(time.time())}\n")
        text.write(f"# 成功匹配: {matched_count}/{len(source_centers)}\n")
        text.write(f"# 格式: 源名称=目标名称\n")

        for src_name, tgt_name in rename_map.items():
            if self.rename_format and "=" in tgt_name:
                dist_info = f"  # Chamfer距离: {distances[src_name]:.6f}" if src_name in distances else ""
                text.write(f"{tgt_name}{dist_info}\n")
            else:
                dist_info = f"  # Chamfer距离: {distances[src_name]:.6f}" if src_name in distances else ""
                text.write(f"{src_name}={tgt_name}{dist_info}\n")

        for src_name in source_centers:
            if src_name not in rename_map:
                text.write(f"# 未匹配: {src_name}\n")

        if debug_parent:
            debug_parent["vgtp_mapping_text"] = text_name
            debug_parent["vgtp_match_threshold"] = self.match_threshold
            debug_parent["vgtp_match_time"] = int(time.time())
            debug_parent["vgtp_matched_count"] = matched_count
            debug_parent["vgtp_total_count"] = len(source_centers)
            debug_parent["vgtp_use_chamfer"] = self.use_chamfer_matching

        self.mapping_text_name = text_name

        return rename_map, f"匹配完成！成功匹配 {matched_count}/{len(source_centers)} 个顶点组，映射表已保存到: {text_name}"
    
    def _get_unique_mapping_text_name(self, base_name):
        """获取唯一的映射表文本名称，如果已存在则添加后缀"""
        if base_name not in bpy.data.texts:
            return base_name
        
        suffix = 1
        while True:
            text_name = f"{base_name}_{suffix:03d}"
            if text_name not in bpy.data.texts:
                return text_name
            suffix += 1


class SSMT_OT_VertexGroupMatchExecute(bpy.types.Operator):
    '''执行顶点组匹配'''
    bl_idname = "ssmt.vertex_group_match_execute"
    bl_label = "执行顶点组匹配"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    node_name: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        return space_data and space_data.type == 'NODE_EDITOR'

    def execute(self, context):
        node = None
        if self.node_name:
            space_data = getattr(context, "space_data", None)
            if space_data and hasattr(space_data, 'node_tree') and space_data.node_tree:
                node = space_data.node_tree.nodes.get(self.node_name)
        
        if not node:
            selected_nodes = [n for n in context.selected_nodes if n.bl_idname == 'SSMTNode_VertexGroupMatch']
            if selected_nodes:
                node = selected_nodes[0]
        
        if not node or node.bl_idname != 'SSMTNode_VertexGroupMatch':
            self.report({'WARNING'}, "请选中顶点组匹配节点")
            return {'CANCELLED'}
        
        rename_map, message = node.execute_match(context)
        
        if rename_map is None:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        
        self.report({'INFO'}, message)
        return {'FINISHED'}


class SSMT_OT_VertexGroupMatchClear(bpy.types.Operator):
    '''清除顶点组映射表和调试物体'''
    bl_idname = "ssmt.vertex_group_match_clear"
    bl_label = "清除顶点组映射表"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    node_name: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        return space_data and space_data.type == 'NODE_EDITOR'

    def execute(self, context):
        node = None
        if self.node_name:
            space_data = getattr(context, "space_data", None)
            if space_data and hasattr(space_data, 'node_tree') and space_data.node_tree:
                node = space_data.node_tree.nodes.get(self.node_name)
        
        if not node:
            selected_nodes = [n for n in context.selected_nodes if n.bl_idname == 'SSMTNode_VertexGroupMatch']
            if selected_nodes:
                node = selected_nodes[0]
        
        if not node or node.bl_idname != 'SSMTNode_VertexGroupMatch':
            self.report({'WARNING'}, "请选中顶点组匹配节点")
            return {'CANCELLED'}
        
        debug_objects_to_remove = []
        mapping_texts_to_remove = set()
        
        for obj in find_debug_parents_for_node(node):
            debug_objects_to_remove.append(obj)
            obj_mapping_text = obj.get("vgtp_mapping_text", "")
            if obj_mapping_text:
                mapping_texts_to_remove.add(obj_mapping_text)
            
        for debug_parent in debug_objects_to_remove:
            children_to_remove = list(debug_parent.children)
            for child in children_to_remove:
                bpy.data.objects.remove(child, do_unlink=True)
            bpy.data.objects.remove(debug_parent, do_unlink=True)
        
        if node.mapping_text_name:
            mapping_texts_to_remove.add(node.mapping_text_name)
        
        for text_name in mapping_texts_to_remove:
            if text_name in bpy.data.texts:
                bpy.data.texts.remove(bpy.data.texts[text_name])

        node.clear_runtime_source_object()

        try:
            backup_payload = json.loads(node.source_mapping_backup) if node.source_mapping_backup else {}
        except json.JSONDecodeError:
            backup_payload = {}
        node._remove_backup_objects_from_payload(backup_payload)
        
        node.mapping_text_name = ""
        node.source_mapping_applied = False
        node.source_mapping_backup = ""
        node.source_mapping_backup_object = ""
        
        if mapping_texts_to_remove or debug_objects_to_remove:
            self.report({'INFO'}, f"已清除 {len(mapping_texts_to_remove)} 个映射表和 {len(debug_objects_to_remove)} 个调试物体，并移除临时源物体")
        else:
            self.report({'INFO'}, f"未找到需要清除的内容")
        
        return {'FINISHED'}


class SSMT_OT_VertexGroupMatchApplyToSource(bpy.types.Operator):
    '''将映射表直接应用到源物体，或再次执行以撤回'''
    bl_idname = "ssmt.vertex_group_match_apply_to_source"
    bl_label = "应用映射到原物体"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        return space_data and space_data.type == 'NODE_EDITOR'

    def execute(self, context):
        node = None
        if self.node_name:
            space_data = getattr(context, "space_data", None)
            if space_data and hasattr(space_data, 'node_tree') and space_data.node_tree:
                node = space_data.node_tree.nodes.get(self.node_name)

        if not node:
            selected_nodes = [n for n in context.selected_nodes if n.bl_idname == 'SSMTNode_VertexGroupMatch']
            if selected_nodes:
                node = selected_nodes[0]

        if not node or node.bl_idname != 'SSMTNode_VertexGroupMatch':
            self.report({'WARNING'}, "请选中顶点组匹配节点")
            return {'CANCELLED'}

        if node.source_mapping_applied:
            success, message = node.revert_mapping_on_source_object()
        else:
            success, message = node.apply_mapping_to_source_object()

        if not success:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        self.report({'INFO'}, message)
        return {'FINISHED'}


class SSMT_OT_VertexGroupMatchToggleDebug(bpy.types.Operator):
    '''显示/隐藏调试物体'''
    bl_idname = "ssmt.vertex_group_match_toggle_debug"
    bl_label = "显示/隐藏调试物体"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    node_name: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        return space_data and space_data.type == 'NODE_EDITOR'

    def execute(self, context):
        node = None
        if self.node_name:
            space_data = getattr(context, "space_data", None)
            if space_data and hasattr(space_data, 'node_tree') and space_data.node_tree:
                node = space_data.node_tree.nodes.get(self.node_name)
        
        if not node:
            selected_nodes = [n for n in context.selected_nodes if n.bl_idname == 'SSMTNode_VertexGroupMatch']
            if selected_nodes:
                node = selected_nodes[0]
        
        if not node or node.bl_idname != 'SSMTNode_VertexGroupMatch':
            self.report({'WARNING'}, "请选中顶点组匹配节点")
            return {'CANCELLED'}
        
        target_obj = bpy.data.objects.get(node.target_object)
        
        if not target_obj:
            self.report({'WARNING'}, "请先设置目标物体")
            return {'CANCELLED'}
        
        debug_objects = find_debug_parents_for_node(node)
        
        if not debug_objects:
            self.report({'INFO'}, "未找到关联的调试物体，请先执行匹配")
            return {'CANCELLED'}
        
        first_debug = debug_objects[0]
        is_hidden = first_debug.hide_get()
        
        for debug_parent in debug_objects:
            debug_parent.hide_set(not is_hidden)
            for child in debug_parent.children:
                child.hide_set(not is_hidden)
        
        action = "显示" if is_hidden else "隐藏"
        self.report({'INFO'}, f"已{action} {len(debug_objects)} 个调试物体组")
        return {'FINISHED'}


def find_debug_parents_for_node(node):
    """通过 debug_link_id 精确查找调试父级，兼容旧版按节点名+树名回退。"""
    link_id = getattr(node, 'debug_link_id', '')

    if link_id:
        primary_matches = []
        for obj in bpy.data.objects:
            if not obj.name.startswith("Debug_Match_"):
                continue
            if obj.get("vgtp_debug_link_id", "") == link_id:
                primary_matches.append(obj)
        if primary_matches:
            return primary_matches

    node_tree_name = node.id_data.name if getattr(node, 'id_data', None) else ""
    node_name = node.name

    fallback_matches = []
    for obj in bpy.data.objects:
        if not obj.name.startswith("Debug_Match_"):
            continue

        obj_node_name = obj.get("vgtp_node_name", "")
        obj_tree_name = obj.get("vgtp_node_tree", "")

        if obj_node_name == node_name and obj_tree_name == node_tree_name:
            fallback_matches.append(obj)
            continue

        if not obj.get("vgtp_debug_link_id", ""):
            source_name = obj.get("vgtp_source_name", "")
            target_name = obj.get("vgtp_target_name", "")
            if source_name == getattr(node, 'source_object', '') and target_name == getattr(node, 'target_object', ''):
                if obj_tree_name == node_tree_name:
                    fallback_matches.append(obj)

    return fallback_matches


def get_debug_source_objects(debug_parent):
    """从调试父级解析源物体列表，支持源合集。"""
    source_collection_name = debug_parent.get("vgtp_source_collection", "")
    if source_collection_name:
        source_collection = bpy.data.collections.get(source_collection_name)
        if source_collection:
            source_objects = [
                obj for obj in source_collection.all_objects
                if obj.type == 'MESH' and obj.data
            ]
            if source_objects:
                return source_objects

    source_obj_name = debug_parent.get("vgtp_source_name", "")
    source_obj = bpy.data.objects.get(source_obj_name) if source_obj_name else None
    if source_obj and source_obj.type == 'MESH' and source_obj.data:
        return [source_obj]

    return []


def get_debug_runtime_source_object(debug_parent):
    runtime_source_name = debug_parent.get("vgtp_runtime_source_object", "")
    if runtime_source_name:
        runtime_obj = bpy.data.objects.get(runtime_source_name)
        if runtime_obj and runtime_obj.type == 'MESH' and runtime_obj.data:
            return runtime_obj

    return None


def find_debug_info(source_empty):
    """从调试物体查找关联的源物体列表、目标物体和调试父级"""
    debug_parent = source_empty.parent
    if not debug_parent:
        return None, None, None

    source_objects = get_debug_source_objects(debug_parent)
    target_obj_name = debug_parent.get("vgtp_target_name")
    target_obj = bpy.data.objects.get(target_obj_name) if target_obj_name else None

    return source_objects, target_obj, debug_parent


class SSMT_OT_VertexGroupMatchSync(bpy.types.Operator):
    '''同步选中的顶点组关联到映射表'''
    bl_idname = "ssmt.vertex_group_match_sync"
    bl_label = "同步选中的顶点组关联"
    bl_options = {'REGISTER', 'UNDO'}
    
    node_name: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) >= 2

    def find_mapping_text_from_debug_parent(self, debug_parent):
        """从调试父级的自定义属性获取映射表文本"""
        mapping_text_name = debug_parent.get("vgtp_mapping_text", "")
        if mapping_text_name and mapping_text_name in bpy.data.texts:
            return bpy.data.texts[mapping_text_name]
        return None

    def update_mapping_text(self, text, source_vg_name, target_vg_name):
        """更新映射表中的条目 - 根据源名称找到对应行，替换等号后的目标名称"""
        lines = []
        found = False
        
        for line in text.lines:
            body = line.body.rstrip('\n\r')
            
            if not body.strip():
                continue
            
            if body.startswith('#'):
                if '未匹配:' in body and source_vg_name in body:
                    lines.append(f"{source_vg_name}={target_vg_name}")
                    found = True
                else:
                    lines.append(body)
                continue
            
            if '=' in body:
                parts = body.split('=', 1)
                src_name = parts[0].strip()
                if src_name == source_vg_name:
                    lines.append(f"{source_vg_name}={target_vg_name}")
                    found = True
                else:
                    lines.append(body)
            else:
                lines.append(body)
        
        if not found:
            lines.append(f"{source_vg_name}={target_vg_name}")
        
        text.clear()
        for line in lines:
            text.write(line + '\n')

    def execute(self, context):
        selected_objects = context.selected_objects

        source_empties = []
        target_empties = []
        for obj in selected_objects:
            if obj.name.startswith("Source_"):
                source_empties.append(obj)
            elif obj.name.startswith("Target_"):
                target_empties.append(obj)

        if not source_empties or not target_empties:
            self.report({'ERROR'}, "请选择至少一个源调试物体（绿色）和一个目标调试物体（黄色）")
            return {'CANCELLED'}

        target_empty = target_empties[0]
        target_vg_name = target_empty.get("original_vg_name")
        if not target_vg_name:
            self.report({'ERROR'}, "选择的目标调试物体缺少原始顶点组名称信息")
            return {'CANCELLED'}

        debug_parent = source_empties[0].parent
        if not debug_parent:
            self.report({'ERROR'}, "无法确定调试父级")
            return {'CANCELLED'}

        source_scope_label = debug_parent.get("vgtp_source_scope_label", "源物体")
        target_obj_name = debug_parent.get("vgtp_target_name")

        mapping_text = self.find_mapping_text_from_debug_parent(debug_parent)
        if not mapping_text:
            import hashlib
            base_name = f"VG_Match_{target_obj_name}"
            if len(base_name) > 63:
                hash_suffix = hashlib.md5(target_obj_name.encode()).hexdigest()[:8]
                base_name = f"VG_Match_{hash_suffix}"
            
            suffix = 1
            text_name = base_name
            while text_name in bpy.data.texts:
                text_name = f"{base_name}_{suffix:03d}"
                suffix += 1
            
            mapping_text = bpy.data.texts.new(text_name)
            mapping_text.write(f"# 顶点组名称映射表\n")
            mapping_text.write(f"# {source_scope_label}\n")
            mapping_text.write(f"# 目标物体: {target_obj_name}\n")
            mapping_text.write(f"# 格式: 源名称=目标名称\n")
            debug_parent["vgtp_mapping_text"] = text_name

        for src in source_empties:
            if src.parent != debug_parent:
                self.report({'ERROR'}, "所有源调试物体必须属于同一个匹配分块")
                return {'CANCELLED'}

        mat_name = "VGTP_Debug_Blue"
        mat = SSMTNode_VertexGroupMatch.get_or_create_debug_material(mat_name, (0.0, 0.2, 1.0, 1.0))

        success_count = 0
        for source_empty in source_empties:
            source_vg_name = source_empty.get("original_vg_name")
            if not source_vg_name:
                continue

            self.update_mapping_text(mapping_text, source_vg_name, target_vg_name)

            old_curve_name_prefix = f"Match_{source_vg_name}_"
            for child in debug_parent.children:
                if child.type == 'CURVE' and child.name.startswith(old_curve_name_prefix):
                    bpy.data.objects.remove(child, do_unlink=True)
                    break

            src_loc, tgt_loc = source_empty.location, target_empty.location
            curve_data = bpy.data.curves.new(f"Match_{source_vg_name}_to_{target_vg_name}", 'CURVE')
            curve_data.dimensions = '3D'
            curve_data.bevel_depth = 0.001
            curve_data.bevel_resolution = 2
            polyline = curve_data.splines.new('POLY')
            polyline.points.add(1)
            polyline.points[0].co = (*src_loc, 1)
            polyline.points[1].co = (*tgt_loc, 1)
            curve_obj = bpy.data.objects.new(f"Match_{source_vg_name}_to_{target_vg_name}", curve_data)
            curve_obj.parent = debug_parent
            context.scene.collection.objects.link(curve_obj)
            
            mat_white_name = "VGTP_Debug_White_Normal"
            mat_white = SSMTNode_VertexGroupMatch.get_or_create_debug_material(mat_white_name, (1.0, 1.0, 1.0, 1.0))
            curve_obj.data.materials.clear()
            curve_obj.data.materials.append(mat_white)

            source_empty["is_connected"] = True
            source_empty.data.materials.clear()
            source_empty.data.materials.append(mat)
            success_count += 1

        self.report({'INFO'}, f"成功同步 {success_count} 个顶点组关联到映射表: {mapping_text.name}")
        return {'FINISHED'}


class SSMT_OT_VertexGroupMatchDeleteConnection(bpy.types.Operator):
    '''删除连接线并更新映射表'''
    bl_idname = "ssmt.vertex_group_match_delete_connection"
    bl_label = "删除连接线"
    bl_options = {'REGISTER', 'UNDO'}
    
    node_name: bpy.props.StringProperty(default="")

    @classmethod
    def poll(cls, context):
        selected_objects = context.selected_objects
        return any(obj.type == 'CURVE' and obj.name.startswith("Match_") for obj in selected_objects)

    def find_mapping_text_from_debug_parent(self, debug_parent):
        """从调试父级的自定义属性获取映射表文本"""
        mapping_text_name = debug_parent.get("vgtp_mapping_text", "")
        if mapping_text_name and mapping_text_name in bpy.data.texts:
            return bpy.data.texts[mapping_text_name]
        return None

    def remove_from_mapping_text(self, text, source_vg_name):
        """从映射表中移除条目"""
        lines = []
        
        for line in text.lines:
            body = line.body.rstrip('\n\r')
            
            if not body.strip():
                continue
            
            if body.startswith('#'):
                lines.append(body)
                continue
            
            if '=' in body:
                parts = body.split('=', 1)
                src_name = parts[0].strip()
                if src_name == source_vg_name:
                    lines.append(f"# 已删除: {source_vg_name}")
                else:
                    lines.append(body)
            else:
                lines.append(body)
        
        text.clear()
        for line in lines:
            text.write(line + '\n')

    def execute(self, context):
        selected_objects = context.selected_objects
        match_curves = [obj for obj in selected_objects if obj.type == 'CURVE' and obj.name.startswith("Match_")]
        
        if not match_curves:
            self.report({'ERROR'}, "请选择至少一个Match_开头的连接线")
            return {'CANCELLED'}

        deleted_count = 0
        
        for active_obj in match_curves:
            match_result = re.match(r"Match_(.+?)_to_(.+)", active_obj.name)
            if not match_result:
                continue

            source_vg_name = match_result.group(1)
            
            debug_parent = active_obj.parent
            
            source_empty_name = f"Source_{source_vg_name}"
            source_empty = None
            if debug_parent:
                for child in debug_parent.children:
                    if child.name == source_empty_name:
                        source_empty = child
                        break
            
            if source_empty:
                source_empty["is_connected"] = False
                mat_name = "VGTP_Debug_Green"
                mat = SSMTNode_VertexGroupMatch.get_or_create_debug_material(mat_name, (0.0, 1.0, 0.2, 1.0))
                source_empty.data.materials.clear()
                source_empty.data.materials.append(mat)
            
            if debug_parent:
                mapping_text = self.find_mapping_text_from_debug_parent(debug_parent)
                if mapping_text:
                    self.remove_from_mapping_text(mapping_text, source_vg_name)
            
            bpy.data.objects.remove(active_obj)
            deleted_count += 1
        
        self.report({'INFO'}, f"已删除 {deleted_count} 条连接线，映射表已更新")
        return {'FINISHED'}


class SSMT_OT_VertexGroupMatchDetectMulti(bpy.types.Operator):
    '''检测多分块连接'''
    bl_idname = "ssmt.vertex_group_match_detect_multi"
    bl_label = "检测多分块连接"
    bl_options = {'REGISTER', 'UNDO'}
    
    node_name: bpy.props.StringProperty(default="")

    def execute(self, context):
        selected_objects = context.selected_objects
        if not selected_objects:
            self.report({'WARNING'}, "请先选择Debug_Match_开头的匹配分块物体")
            return {'CANCELLED'}
            
        match_blocks = []
        for obj in selected_objects:
            if obj.name.startswith("Debug_Match_"):
                match_blocks.append(obj)
        
        if not match_blocks:
            self.report({'WARNING'}, "请选择名称以'Debug_Match_'开头的匹配分块物体")
            return {'CANCELLED'}
            
        all_match_curves = []
        for block in match_blocks:
            for child in block.children:
                if child.type == 'CURVE' and child.name.startswith("Match_"):
                    all_match_curves.append(child)
            
            if block.type == 'CURVE' and block.name.startswith("Match_"):
                all_match_curves.append(block)
        
        if not all_match_curves:
            self.report({'WARNING'}, "在选中的匹配分块中未找到Match_开头的连接线")
            return {'CANCELLED'}
            
        source_to_targets = {}
        
        for curve in all_match_curves:
            try:
                name_part = curve.name.replace("Match_", "")
                parts = name_part.split("_to_")
                if len(parts) == 2:
                    source_vg = parts[0]
                    target_vg = parts[1]
                    
                    source_vg_clean = source_vg
                    target_vg_clean = re.sub(r'\.\d+$', '', target_vg) if '.' in target_vg else target_vg
                    
                    if source_vg_clean not in source_to_targets:
                        source_to_targets[source_vg_clean] = []
                    if target_vg_clean not in source_to_targets[source_vg_clean]:
                        source_to_targets[source_vg_clean].append(target_vg_clean)
            except Exception as e:
                self.report({'WARNING'}, f"解析连接线名称失败: {curve.name}, 错误: {str(e)}")
                return {'CANCELLED'}
        
        multi_connections = {}
        for source_vg, target_vgs in source_to_targets.items():
            if len(target_vgs) > 1:
                multi_connections[source_vg] = target_vgs

        target_to_sources = {}
        for source_vg, target_vgs in source_to_targets.items():
            for target_vg in target_vgs:
                if target_vg not in target_to_sources:
                    target_to_sources[target_vg] = []
                target_to_sources[target_vg].append(source_vg)
        
        reverse_multi_connections = {}
        for target_vg, source_vgs in target_to_sources.items():
            if len(source_vgs) > 1:
                reverse_multi_connections[target_vg] = source_vgs
        
        white_mat_name = "VGTP_Debug_White_Normal"
        white_mat = SSMTNode_VertexGroupMatch.get_or_create_debug_material(white_mat_name, (1.0, 1.0, 1.0, 1.0))
            
        for curve in all_match_curves:
            curve.data.materials.clear()
            curve.data.materials.append(white_mat)
            if "is_multi_connection" in curve:
                del curve["is_multi_connection"]
        
        has_multi_connections = False
        for source_vg_clean, target_vgs_clean in multi_connections.items():
            for target_vg_clean in target_vgs_clean:
                for curve in all_match_curves:
                    try:
                        name_part = curve.name.replace("Match_", "")
                        parts = name_part.split("_to_")
                        if len(parts) == 2:
                            source_vg = parts[0]
                            target_vg = parts[1]
                            
                            source_vg_current = source_vg
                            target_vg_current = re.sub(r'\.\d+$', '', target_vg) if '.' in target_vg else target_vg
                            
                            if source_vg_current == source_vg_clean and target_vg_current == target_vg_clean:
                                mat_name = "VGTP_Debug_Red_Error"
                                mat = SSMTNode_VertexGroupMatch.get_or_create_debug_material(mat_name, (1.0, 0.0, 0.0, 1.0))
                                curve.data.materials.clear()
                                curve.data.materials.append(mat)
                                curve["is_multi_connection"] = True
                                has_multi_connections = True
                                break
                    except Exception:
                        continue
        
        message_parts = []
        
        if multi_connections:
            message_parts.append("检测到以下源顶点组连接多个目标顶点组（已标红）:")
            for src_name, targets in multi_connections.items():
                message_parts.append(f"- {src_name} 连接到: {', '.join(targets)}")
        
        if reverse_multi_connections:
            message_parts.append("检测到以下目标顶点组被多个源顶点组连接:")
            for target_name, sources in reverse_multi_connections.items():
                message_parts.append(f"- {target_name} 被 {', '.join(sources)} 连接")
        
        if not message_parts:
            self.report({'INFO'}, f"在选中的{len(match_blocks)}个匹配分块中的{len(all_match_curves)}条连接线中未检测到多分块连接情况")
        else:
            full_message = "\n".join(message_parts)
            self.report({'INFO'}, full_message)
            
        return {'FINISHED'}


class SSMT_OT_VertexGroupMatchQuickWeight(bpy.types.Operator):
    '''快速权重绘制 (切换) - 支持查看源物体、目标物体或合并权重'''
    bl_idname = "ssmt.vertex_group_match_quick_weight"
    bl_label = "快速权重绘制 (切换)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        if active_obj is None:
            return False
        if active_obj.type == 'CURVE' and active_obj.name.startswith("Match_"):
            return True
        if active_obj.name.startswith("Source_") or active_obj.name.startswith("Target_"):
            return True
        return True

    def execute(self, context):
        active_obj = context.active_object

        if active_obj and active_obj.mode == 'WEIGHT_PAINT':
            obj_to_exit = active_obj
            bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            
            if obj_to_exit.get("vgtp_is_temp_merge"):
                bpy.data.objects.remove(obj_to_exit, do_unlink=True)
            else:
                obj_to_exit.hide_set(True)
            return {'FINISHED'}

        if active_obj.type == 'CURVE' and active_obj.name.startswith("Match_"):
            return self._handle_curve_selection(context, active_obj)
        
        if active_obj.name.startswith("Source_") or active_obj.name.startswith("Target_"):
            return self._handle_debug_object_selection(context, active_obj)
        
        self.report({'WARNING'}, "请选择一条 Match_ 连接线或调试物体 (Source_/Target_)")
        return {'CANCELLED'}

    def _handle_curve_selection(self, context, active_curve):
        """处理连接线选择 - 创建合并网格显示双方权重"""
        match = re.match(r"Match_(.+?)_to_(.+)", active_curve.name)
        if not match:
            self.report({'ERROR'}, "连接线名称格式不正确")
            return {'CANCELLED'}

        source_vg_name = match.group(1)
        target_vg_name = match.group(2)
        temp_mesh_name = f"Temp_Merge_{source_vg_name}_{target_vg_name}"
        
        existing_temp_obj = bpy.data.objects.get(temp_mesh_name)
        if existing_temp_obj and existing_temp_obj.get("vgtp_is_temp_merge"):
            bpy.data.objects.remove(existing_temp_obj, do_unlink=True)
        
        debug_parent = active_curve.parent
        if not debug_parent:
            self.report({'ERROR'}, "无法找到连接线的父级调试组")
            return {'CANCELLED'}
        
        target_obj_name = debug_parent.get("vgtp_target_name")
        
        if not target_obj_name:
            self.report({'ERROR'}, "调试组缺少目标物体信息")
            return {'CANCELLED'}

        runtime_source_obj = get_debug_runtime_source_object(debug_parent)
        source_objects = [runtime_source_obj] if runtime_source_obj else get_debug_source_objects(debug_parent)
        target_obj = bpy.data.objects.get(target_obj_name)
        
        if not source_objects or not target_obj:
            self.report({'ERROR'}, "无法找到关联的源或目标物体")
            return {'CANCELLED'}

        source_obj = source_objects[0]
        source_is_temp = bool(runtime_source_obj and source_obj == runtime_source_obj)

        source_vg = None
        for vg in source_obj.vertex_groups:
            if "=" in vg.name:
                original_name = vg.name.split("=", 1)[0].strip()
            else:
                original_name = vg.name
            
            if original_name == source_vg_name:
                source_vg = vg
                break

        target_vg = target_obj.vertex_groups.get(target_vg_name)

        if not source_vg or not target_vg:
            self.report({'ERROR'}, "无法找到关联的顶点组")
            return {'CANCELLED'}
        
        temp_mesh = bpy.data.meshes.new(temp_mesh_name)
        
        source_mesh = source_obj.data
        target_mesh = target_obj.data
        
        source_matrix = source_obj.matrix_world
        target_matrix = target_obj.matrix_world
        
        source_vert_count = len(source_mesh.vertices)
        
        source_verts = [(source_matrix @ v.co)[:] for v in source_mesh.vertices]
        target_verts = [(target_matrix @ v.co)[:] for v in target_mesh.vertices]
        all_verts = source_verts + target_verts
        
        source_edges = [(e.vertices[0], e.vertices[1]) for e in source_mesh.edges]
        target_edges = [(e.vertices[0] + source_vert_count, e.vertices[1] + source_vert_count) for e in target_mesh.edges]
        all_edges = source_edges + target_edges
        
        source_faces = [list(p.vertices) for p in source_mesh.polygons]
        target_faces = [list(v + source_vert_count for v in p.vertices) for p in target_mesh.polygons]
        all_faces = source_faces + target_faces
        
        temp_mesh.from_pydata(all_verts, all_edges, all_faces)
        
        temp_obj = bpy.data.objects.new(temp_mesh_name, temp_mesh)
        context.scene.collection.objects.link(temp_obj)
        
        temp_obj["vgtp_is_temp_merge"] = True
        temp_obj["vgtp_source_obj"] = source_obj.name
        temp_obj["vgtp_source_vg"] = source_vg.name
        temp_obj["vgtp_target_obj"] = target_obj.name
        temp_obj["vgtp_target_vg"] = target_vg.name
        temp_obj["vgtp_source_vg_name"] = source_vg_name
        temp_obj["vgtp_target_vg_name"] = target_vg_name
        
        source_vg_index = source_obj.vertex_groups.find(source_vg.name)
        target_vg_index = target_obj.vertex_groups.find(target_vg.name)
        
        temp_vg_source = temp_obj.vertex_groups.new(name=f"Source_{source_vg_name}")
        temp_vg_target = temp_obj.vertex_groups.new(name=f"Target_{target_vg_name}")
        
        for i, v in enumerate(source_mesh.vertices):
            try:
                weight = source_vg.weight(i)
                if weight > 0:
                    temp_vg_source.add([i], weight, 'REPLACE')
            except RuntimeError:
                pass
        
        for i, v in enumerate(target_mesh.vertices):
            try:
                weight = target_vg.weight(i)
                if weight > 0:
                    temp_vg_target.add([source_vert_count + i], weight, 'REPLACE')
            except RuntimeError:
                pass
        
        merged_vg_name = f"Merged_{source_vg_name}_{target_vg_name}"
        merged_vg = temp_obj.vertex_groups.new(name=merged_vg_name)
        
        for vert_idx in range(len(temp_obj.data.vertices)):
            source_weight = 0.0
            target_weight = 0.0
            
            for group in temp_obj.data.vertices[vert_idx].groups:
                if group.group == temp_vg_source.index:
                    source_weight = group.weight
                elif group.group == temp_vg_target.index:
                    target_weight = group.weight
            
            if source_weight > 0 or target_weight > 0:
                merged_weight = min(1.0, source_weight + target_weight)
                merged_vg.add([vert_idx], merged_weight, 'REPLACE')
        
        bpy.ops.object.select_all(action='DESELECT')
        temp_obj.select_set(True)
        context.view_layer.objects.active = temp_obj
        temp_obj.vertex_groups.active = merged_vg
        bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
        
        self.report({'INFO'}, f"已创建临时合并物体，包含源、目标和合并权重")
        return {'FINISHED'}

    def _handle_debug_object_selection(self, context, debug_obj):
        """处理调试物体选择 - 直接进入原始物体的权重模式"""
        is_source = debug_obj.name.startswith("Source_")
        
        source_objects, target_obj, debug_parent = find_debug_info(debug_obj)
        runtime_source_obj = get_debug_runtime_source_object(debug_parent) if debug_parent else None
        if is_source and runtime_source_obj:
            mesh_candidates = [runtime_source_obj]
        else:
            mesh_candidates = source_objects if is_source else ([target_obj] if target_obj else [])
        
        if not mesh_candidates:
            self.report({'WARNING'}, "无法找到此调试物体关联的原始模型")
            return {'CANCELLED'}

        vg_name_from_empty = debug_obj.get("original_vg_name")
        if not vg_name_from_empty:
            self.report({'ERROR'}, "调试物体缺少顶点组名称信息")
            return {'CANCELLED'}

        match_candidates = []
        for mesh_obj in mesh_candidates:
            found_vg = None
            if is_source:
                for vg in mesh_obj.vertex_groups:
                    if "=" in vg.name:
                        original_name = vg.name.split("=", 1)[0].strip()
                    else:
                        original_name = vg.name

                    if original_name == vg_name_from_empty:
                        found_vg = vg
                        break
            else:
                found_vg = mesh_obj.vertex_groups.get(vg_name_from_empty)

            if found_vg:
                match_candidates.append((mesh_obj, found_vg))

        if not match_candidates:
            self.report({'WARNING'}, f"未找到关联顶点组: {vg_name_from_empty}")
            return {'CANCELLED'}

        mesh_obj, found_vg = match_candidates[0]

        if mesh_obj.hide_get():
            mesh_obj.hide_set(False)
        if mesh_obj.hide_select:
            mesh_obj.hide_select = False

        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = mesh_obj
        mesh_obj.select_set(True)
        mesh_obj.vertex_groups.active = found_vg
        bpy.ops.object.mode_set(mode='WEIGHT_PAINT')

        if is_source and runtime_source_obj:
            self.report({'INFO'}, f"已进入权重绘制模式: {mesh_obj.name} - {found_vg.name}（源合集预览临时物体）")
        elif is_source and len(match_candidates) > 1:
            self.report({'INFO'}, f"已进入权重绘制模式: {mesh_obj.name} - {found_vg.name}（源合集下命中 {len(match_candidates)} 个物体，已打开第一个）")
        else:
            self.report({'INFO'}, f"已进入权重绘制模式: {mesh_obj.name} - {found_vg.name}")
        return {'FINISHED'}


def register_keymaps():
    """注册快捷键"""
    wm = bpy.context.window_manager
    if not wm.keyconfigs.addon:
        return
    
    kc = wm.keyconfigs.addon
    
    km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
    
    kmi_sync = km.keymap_items.new(
        SSMT_OT_VertexGroupMatchSync.bl_idname, 
        'Z', 'PRESS', 
        ctrl=True, shift=True
    )
    _keymaps.append((km, kmi_sync))
    
    kmi_wp = km.keymap_items.new(
        SSMT_OT_VertexGroupMatchQuickWeight.bl_idname, 
        'W', 'PRESS', 
        alt=True
    )
    _keymaps.append((km, kmi_wp))
    
    print("[VertexGroupMatch] 已注册快捷键: Ctrl+Shift+Z (同步), Alt+W (快速权重)")


def unregister_keymaps():
    """注销快捷键"""
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _keymaps.clear()
    
    print("[VertexGroupMatch] 已注销快捷键")


classes = (
    SSMTNode_VertexGroupMatch,
    SSMT_OT_VertexGroupMatchExecute,
    SSMT_OT_VertexGroupMatchClear,
    SSMT_OT_VertexGroupMatchApplyToSource,
    SSMT_OT_VertexGroupMatchToggleDebug,
    SSMT_OT_VertexGroupMatchSync,
    SSMT_OT_VertexGroupMatchDeleteConnection,
    SSMT_OT_VertexGroupMatchDetectMulti,
    SSMT_OT_VertexGroupMatchQuickWeight,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    register_keymaps()


def unregister():
    unregister_keymaps()
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
