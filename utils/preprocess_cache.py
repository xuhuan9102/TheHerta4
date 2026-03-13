"""
预处理缓存系统
用于缓存预处理结果，避免重复计算
"""
import bpy
import json
import hashlib
import os
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
import numpy as np


@dataclass
class ObjectFingerprint:
    """物体指纹，用于检测物体是否变更"""
    vertex_count: int
    vertex_hash: str
    edge_hash: str
    face_hash: str
    vertex_group_hash: str
    modifier_hash: str
    shape_key_hash: str
    transform_hash: str
    armature_pose_hash: str
    mirror_workflow: bool
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'ObjectFingerprint':
        return cls(**data)
    
    def __eq__(self, other):
        if not isinstance(other, ObjectFingerprint):
            return False
        return (
            self.vertex_count == other.vertex_count and
            self.vertex_hash == other.vertex_hash and
            self.edge_hash == other.edge_hash and
            self.face_hash == other.face_hash and
            self.vertex_group_hash == other.vertex_group_hash and
            self.modifier_hash == other.modifier_hash and
            self.shape_key_hash == other.shape_key_hash and
            self.transform_hash == other.transform_hash and
            self.armature_pose_hash == other.armature_pose_hash and
            self.mirror_workflow == other.mirror_workflow
        )


class FingerprintCalculator:
    """指纹计算器"""
    
    @staticmethod
    def calculate_vertex_hash(obj: bpy.types.Object) -> Tuple[int, str]:
        """计算顶点数据的哈希值"""
        if obj.type != 'MESH' or not obj.data:
            return 0, ""
        
        mesh = obj.data
        
        if len(mesh.vertices) == 0:
            return 0, ""
        
        vertices = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get('co', vertices)
        
        vertex_hash = hashlib.md5(vertices.tobytes()).hexdigest()
        
        return len(mesh.vertices), vertex_hash
    
    @staticmethod
    def calculate_edge_hash(obj: bpy.types.Object) -> str:
        """计算边缘数据的哈希值（包括锐边标记）"""
        if obj.type != 'MESH' or not obj.data:
            return ""
        
        mesh = obj.data
        
        if len(mesh.edges) == 0:
            return ""
        
        edge_data = []
        edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        mesh.edges.foreach_get('vertices', edges)
        edge_data.append(edges.tobytes())
        
        use_edge_sharp = np.empty(len(mesh.edges), dtype=np.bool_)
        mesh.edges.foreach_get('use_edge_sharp', use_edge_sharp)
        edge_data.append(use_edge_sharp.tobytes())
        
        use_edge_freestyle = np.empty(len(mesh.edges), dtype=np.bool_)
        mesh.edges.foreach_get('use_freestyle_mark', use_edge_freestyle)
        edge_data.append(use_edge_freestyle.tobytes())
        
        combined = b''.join(edge_data)
        return hashlib.md5(combined).hexdigest()
    
    @staticmethod
    def calculate_face_hash(obj: bpy.types.Object) -> str:
        """计算面数据的哈希值"""
        if obj.type != 'MESH' or not obj.data:
            return ""
        
        mesh = obj.data
        
        if len(mesh.polygons) == 0:
            return ""
        
        face_data = []
        
        for poly in mesh.polygons:
            face_data.append(list(poly.vertices))
            face_data.append(poly.use_smooth)
        
        face_json = json.dumps(face_data, sort_keys=True)
        return hashlib.md5(face_json.encode()).hexdigest()
    
    @staticmethod
    def calculate_vertex_group_hash(obj: bpy.types.Object) -> str:
        """计算顶点组数据的哈希值"""
        if obj.type != 'MESH' or not obj.data or not obj.vertex_groups:
            return ""
        
        mesh = obj.data
        vg_data = []
        
        for vg in obj.vertex_groups:
            vg_info = {
                'name': vg.name,
                'lock_weight': vg.lock_weight,
            }
            
            weights = []
            for i, vert in enumerate(mesh.vertices):
                for group in vert.groups:
                    if group.group == vg.index:
                        weights.append((i, group.weight))
                        break
            
            vg_info['weights_hash'] = hashlib.md5(
                json.dumps(weights, sort_keys=True).encode()
            ).hexdigest()
            vg_data.append(vg_info)
        
        vg_json = json.dumps(vg_data, sort_keys=True)
        return hashlib.md5(vg_json.encode()).hexdigest()
    
    @staticmethod
    def calculate_modifier_hash(obj: bpy.types.Object) -> str:
        """计算修改器状态的哈希值"""
        if not obj.modifiers:
            return ""
        
        modifier_data = []
        for idx, mod in enumerate(obj.modifiers):
            mod_info = {
                'index': idx,
                'name': mod.name,
                'type': mod.type,
                'show_viewport': mod.show_viewport,
                'show_render': mod.show_render,
            }
            
            if mod.type == 'ARMATURE':
                mod_info['object'] = mod.object.name if mod.object else ""
                mod_info['use_vertex_groups'] = mod.use_vertex_groups
                mod_info['use_deform_preserve_volume'] = mod.use_deform_preserve_volume
                mod_info['invert_vertex_group'] = mod.invert_vertex_group
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                if hasattr(mod, 'use_multi_modifier'):
                    mod_info['use_multi_modifier'] = mod.use_multi_modifier
            elif mod.type == 'MIRROR':
                mod_info['use_axis'] = list(mod.use_axis)
                mod_info['use_bisect_axis'] = list(mod.use_bisect_axis)
                mod_info['mirror_object'] = mod.mirror_object.name if mod.mirror_object else ""
                mod_info['use_clip'] = mod.use_clip if hasattr(mod, 'use_clip') else False
                mod_info['mirror_offset_u'] = mod.offset_u if hasattr(mod, 'offset_u') else 0.0
                mod_info['mirror_offset_v'] = mod.offset_v if hasattr(mod, 'offset_v') else 0.0
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
            elif mod.type == 'SUBSURF':
                mod_info['levels'] = mod.levels
                mod_info['render_levels'] = mod.render_levels
                mod_info['subdivision_type'] = mod.subdivision_type if hasattr(mod, 'subdivision_type') else 'CATMULL_CLARK'
            elif mod.type == 'SOLIDIFY':
                mod_info['thickness'] = mod.thickness
                mod_info['offset'] = mod.offset
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                mod_info['use_even_offset'] = mod.use_even_offset if hasattr(mod, 'use_even_offset') else False
            elif mod.type == 'BEVEL':
                mod_info['width'] = mod.width
                mod_info['segments'] = mod.segments
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                mod_info['limit_method'] = mod.limit_method if hasattr(mod, 'limit_method') else 'NONE'
            elif mod.type == 'DECIMATE':
                mod_info['ratio'] = mod.ratio
                mod_info['decimate_type'] = mod.decimate_type
            elif mod.type == 'TRIANGULATE':
                mod_info['quad_method'] = mod.quad_method
                mod_info['ngon_method'] = mod.ngon_method
                mod_info['min_vertices'] = mod.min_vertices if hasattr(mod, 'min_vertices') else 4
            elif mod.type == 'WELD':
                mod_info['merge_threshold'] = mod.merge_threshold
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
            elif mod.type == 'BOOLEAN':
                mod_info['operation'] = mod.operation if hasattr(mod, 'operation') else 'DIFFERENCE'
                mod_info['object'] = mod.object.name if mod.object else ""
                mod_info['solver'] = mod.solver if hasattr(mod, 'solver') else 'EXACT'
            elif mod.type == 'LATTICE':
                mod_info['object'] = mod.object.name if mod.object else ""
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                mod_info['strength'] = mod.strength if hasattr(mod, 'strength') else 1.0
            elif mod.type == 'HOOK':
                mod_info['object'] = mod.object.name if mod.object else ""
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                mod_info['strength'] = mod.strength if hasattr(mod, 'strength') else 1.0
                mod_info['falloff_type'] = mod.falloff_type if hasattr(mod, 'falloff_type') else 'NONE'
            elif mod.type == 'SHRINKWRAP':
                mod_info['target'] = mod.target.name if mod.target else ""
                mod_info['auxiliary_target'] = mod.auxiliary_target.name if hasattr(mod, 'auxiliary_target') and mod.auxiliary_target else ""
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                mod_info['shrinkwrap_mode'] = mod.wrap_mode if hasattr(mod, 'wrap_mode') else 'ON_SURFACE'
            elif mod.type == 'SIMPLE_DEFORM':
                mod_info['deform_mode'] = mod.deform_method if hasattr(mod, 'deform_method') else 'TWIST'
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                mod_info['factor'] = mod.factor if hasattr(mod, 'factor') else 0.0
                mod_info['limits'] = [mod.limits[0], mod.limits[1]] if hasattr(mod, 'limits') else [0.0, 1.0]
            elif mod.type == 'WAVE':
                mod_info['vertex_group'] = mod.vertex_group if hasattr(mod, 'vertex_group') else ""
                mod_info['time_offset'] = mod.time_offset if hasattr(mod, 'time_offset') else 0.0
                mod_info['speed'] = mod.speed if hasattr(mod, 'speed') else 1.0
            elif mod.type == 'ARMATURE':
                pass
            
            modifier_data.append(mod_info)
        
        modifier_json = json.dumps(modifier_data, sort_keys=True)
        return hashlib.md5(modifier_json.encode()).hexdigest()
    
    @staticmethod
    def calculate_shape_key_hash(obj: bpy.types.Object) -> str:
        """计算形态键状态的哈希值"""
        if not obj.data or not obj.data.shape_keys or not obj.data.shape_keys.key_blocks:
            return ""
        
        shape_key_data = []
        for kb in obj.data.shape_keys.key_blocks:
            kb_info = {
                'name': kb.name,
                'value': kb.value,
                'mute': kb.mute,
                'vertex_group': kb.vertex_group,
                'interpolation': kb.interpolation if hasattr(kb, 'interpolation') else 'KEY_LINEAR',
            }
            
            if kb.relative_key:
                kb_info['relative_key'] = kb.relative_key.name
            
            if len(kb.data) > 0:
                coords = np.empty(len(kb.data) * 3, dtype=np.float32)
                kb.data.foreach_get('co', coords)
                kb_info['data_hash'] = hashlib.md5(coords.tobytes()).hexdigest()
            
            shape_key_data.append(kb_info)
        
        shape_key_json = json.dumps(shape_key_data, sort_keys=True)
        return hashlib.md5(shape_key_json.encode()).hexdigest()
    
    @staticmethod
    def calculate_transform_hash(obj: bpy.types.Object) -> str:
        """计算物体变换的哈希值"""
        transform_data = {
            'location': list(obj.location),
            'rotation_mode': obj.rotation_mode,
            'rotation_euler': list(obj.rotation_euler) if obj.rotation_mode == 'EULER' else [],
            'rotation_quaternion': list(obj.rotation_quaternion) if obj.rotation_mode == 'QUATERNION' else [],
            'scale': list(obj.scale),
            'delta_location': list(obj.delta_location) if hasattr(obj, 'delta_location') else [],
            'delta_rotation_euler': list(obj.delta_rotation_euler) if hasattr(obj, 'delta_rotation_euler') else [],
            'delta_scale': list(obj.delta_scale) if hasattr(obj, 'delta_scale') else [],
        }
        
        transform_json = json.dumps(transform_data, sort_keys=True)
        return hashlib.md5(transform_json.encode()).hexdigest()
    
    @staticmethod
    def calculate_armature_pose_hash(obj: bpy.types.Object) -> str:
        """计算骨骼姿势的哈希值"""
        armature_modifiers = [mod for mod in obj.modifiers if mod.type == 'ARMATURE' and mod.object and mod.show_viewport]
        
        if not armature_modifiers:
            return ""
        
        pose_data = []
        for mod in armature_modifiers:
            armature = mod.object
            if armature and armature.pose:
                armature_info = {
                    'armature_name': armature.name,
                    'bones': []
                }
                
                for bone in armature.pose.bones:
                    bone_info = {
                        'name': bone.name,
                        'location': list(bone.location),
                        'rotation_mode': bone.rotation_mode,
                        'rotation_euler': list(bone.rotation_euler) if bone.rotation_mode == 'EULER' else [],
                        'rotation_quaternion': list(bone.rotation_quaternion) if bone.rotation_mode == 'QUATERNION' else [],
                        'scale': list(bone.scale),
                    }
                    armature_info['bones'].append(bone_info)
                
                pose_data.append(armature_info)
        
        if not pose_data:
            return ""
        
        pose_json = json.dumps(pose_data, sort_keys=True)
        return hashlib.md5(pose_json.encode()).hexdigest()
    
    @classmethod
    def calculate_fingerprint(cls, obj: bpy.types.Object, mirror_workflow: bool = False) -> ObjectFingerprint:
        """计算物体的完整指纹"""
        vertex_count, vertex_hash = cls.calculate_vertex_hash(obj)
        edge_hash = cls.calculate_edge_hash(obj)
        face_hash = cls.calculate_face_hash(obj)
        vertex_group_hash = cls.calculate_vertex_group_hash(obj)
        modifier_hash = cls.calculate_modifier_hash(obj)
        shape_key_hash = cls.calculate_shape_key_hash(obj)
        transform_hash = cls.calculate_transform_hash(obj)
        armature_pose_hash = cls.calculate_armature_pose_hash(obj)
        
        return ObjectFingerprint(
            vertex_count=vertex_count,
            vertex_hash=vertex_hash,
            edge_hash=edge_hash,
            face_hash=face_hash,
            vertex_group_hash=vertex_group_hash,
            modifier_hash=modifier_hash,
            shape_key_hash=shape_key_hash,
            transform_hash=transform_hash,
            armature_pose_hash=armature_pose_hash,
            mirror_workflow=mirror_workflow
        )


class PreprocessCacheManager:
    """预处理缓存管理器"""
    
    CACHE_VERSION = 1
    
    def __init__(self, cache_dir: Optional[str] = None):
        self._cache_dir_override = cache_dir
        self.cache_index: Dict[str, dict] = {}
        self._ensure_cache_dir()
        self._load_cache_index()
    
    @property
    def cache_dir(self) -> str:
        """动态获取缓存目录"""
        if self._cache_dir_override:
            return self._cache_dir_override
        
        blend_file = bpy.data.filepath
        if blend_file:
            blend_dir = os.path.dirname(blend_file)
            cache_dir = os.path.join(blend_dir, ".preprocess_cache")
        else:
            cache_dir = os.path.join(os.path.expanduser("~"), ".ssmt_preprocess_cache")
        
        return cache_dir
    
    @property
    def index_file(self) -> str:
        """动态获取索引文件路径"""
        return os.path.join(self.cache_dir, "cache_index.json")
    
    def _get_default_cache_dir(self) -> str:
        """获取默认缓存目录"""
        blend_file = bpy.data.filepath
        if blend_file:
            blend_dir = os.path.dirname(blend_file)
            cache_dir = os.path.join(blend_dir, ".preprocess_cache")
        else:
            cache_dir = os.path.join(os.path.expanduser("~"), ".ssmt_preprocess_cache")
        
        print(f"[PreprocessCache] blend_file={blend_file}, cache_dir={cache_dir}")
        return cache_dir
    
    def _ensure_cache_dir(self):
        """确保缓存目录存在"""
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
    
    def _load_cache_index(self):
        """加载缓存索引"""
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data.get('version') == self.CACHE_VERSION:
                        self.cache_index = data.get('entries', {})
            except Exception as e:
                print(f"[PreprocessCache] 加载缓存索引失败: {e}")
                self.cache_index = {}
    
    def _save_cache_index(self):
        """保存缓存索引"""
        try:
            data = {
                'version': self.CACHE_VERSION,
                'entries': self.cache_index
            }
            with open(self.index_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[PreprocessCache] 保存缓存索引失败: {e}")
    
    def _get_cache_key(self, obj_name: str, fingerprint: ObjectFingerprint) -> str:
        """生成缓存键"""
        fp_dict = fingerprint.to_dict()
        fp_json = json.dumps(fp_dict, sort_keys=True)
        fp_hash = hashlib.md5(fp_json.encode()).hexdigest()
        cache_key = f"{obj_name}_{fp_hash}"
        print(f"[PreprocessCache] 生成缓存键: {cache_key[:50]}...")
        return cache_key
    
    def _get_cache_file_path(self, cache_key: str) -> str:
        """获取缓存文件路径"""
        return os.path.join(self.cache_dir, f"{cache_key}.blend")
    
    def has_valid_cache(self, obj_name: str, fingerprint: ObjectFingerprint) -> bool:
        """检查是否有有效的缓存"""
        cache_key = self._get_cache_key(obj_name, fingerprint)
        
        if cache_key not in self.cache_index:
            print(f"[PreprocessCache] 缓存未命中: {obj_name} (键不在索引中)")
            return False
        
        cache_file = self._get_cache_file_path(cache_key)
        if not os.path.exists(cache_file):
            print(f"[PreprocessCache] 缓存未命中: {obj_name} (文件不存在)")
            return False
        
        print(f"[PreprocessCache] 缓存命中: {obj_name}")
        return True
    
    def get_cache(self, obj_name: str, fingerprint: ObjectFingerprint) -> Optional[str]:
        """获取缓存文件路径"""
        cache_key = self._get_cache_key(obj_name, fingerprint)
        
        if not self.has_valid_cache(obj_name, fingerprint):
            return None
        
        return self._get_cache_file_path(cache_key)
    
    def store_cache(self, obj_name: str, fingerprint: ObjectFingerprint, 
                    preprocessed_obj: bpy.types.Object) -> str:
        """存储预处理结果到缓存"""
        cache_key = self._get_cache_key(obj_name, fingerprint)
        cache_file = self._get_cache_file_path(cache_key)
        
        try:
            self._ensure_cache_dir()
            
            temp_obj = preprocessed_obj.copy()
            temp_obj.data = preprocessed_obj.data.copy()
            temp_obj.name = f"__cache_{obj_name}__"
            
            bpy.context.scene.collection.objects.link(temp_obj)
            
            data_blocks = set()
            data_blocks.add(temp_obj)
            data_blocks.add(temp_obj.data)
            
            bpy.data.libraries.write(cache_file, data_blocks, compress=True)
            
            mesh_data = temp_obj.data
            bpy.data.objects.remove(temp_obj, do_unlink=True)
            if mesh_data and mesh_data.name in bpy.data.meshes:
                bpy.data.meshes.remove(mesh_data, do_unlink=True)
            
            self.cache_index[cache_key] = {
                'obj_name': obj_name,
                'fingerprint': fingerprint.to_dict(),
                'file': cache_file
            }
            self._save_cache_index()
            
            print(f"[PreprocessCache] 已缓存: {obj_name}")
            return cache_file
            
        except Exception as e:
            print(f"[PreprocessCache] 存储缓存失败: {e}")
            import traceback
            traceback.print_exc()
            return ""
    
    def load_cache(self, obj_name: str, fingerprint: ObjectFingerprint, 
                   target_scene: bpy.types.Scene) -> Optional[bpy.types.Object]:
        """从缓存加载预处理结果"""
        cache_file = self.get_cache(obj_name, fingerprint)
        if not cache_file:
            return None
        
        try:
            with bpy.data.libraries.load(cache_file, link=False) as (data_from, data_to):
                data_to.objects = [name for name in data_from.objects]
            
            loaded_obj = None
            for obj in data_to.objects:
                if obj and obj.type == 'MESH':
                    target_scene.collection.objects.link(obj)
                    loaded_obj = obj
                    break
                elif obj:
                    bpy.data.objects.remove(obj, do_unlink=True)
            
            if loaded_obj:
                print(f"[PreprocessCache] 从缓存加载: {obj_name}")
                return loaded_obj
            
            return None
            
        except Exception as e:
            print(f"[PreprocessCache] 加载缓存失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def clear_cache(self, obj_name: Optional[str] = None):
        """清理缓存"""
        if obj_name:
            keys_to_remove = [k for k in self.cache_index if k.startswith(obj_name)]
            for key in keys_to_remove:
                cache_file = self._get_cache_file_path(key)
                if os.path.exists(cache_file):
                    try:
                        os.remove(cache_file)
                    except:
                        pass
                del self.cache_index[key]
        else:
            for key in self.cache_index:
                cache_file = self._get_cache_file_path(key)
                if os.path.exists(cache_file):
                    try:
                        os.remove(cache_file)
                    except:
                        pass
            self.cache_index.clear()
        
        self._save_cache_index()
        print(f"[PreprocessCache] 已清理缓存")
    
    def get_cache_stats(self) -> dict:
        """获取缓存统计信息"""
        total_size = 0
        for key in self.cache_index:
            cache_file = self._get_cache_file_path(key)
            if os.path.exists(cache_file):
                total_size += os.path.getsize(cache_file)
        
        return {
            'total_entries': len(self.cache_index),
            'total_size_bytes': total_size,
            'total_size_mb': total_size / (1024 * 1024)
        }


_global_cache_manager: Optional[PreprocessCacheManager] = None
_global_cache_blend_file: Optional[str] = None


def get_cache_manager(blend_file: Optional[str] = None) -> PreprocessCacheManager:
    """获取全局缓存管理器
    
    Args:
        blend_file: 可选的 blend 文件路径，用于确定缓存目录
    """
    global _global_cache_manager, _global_cache_blend_file
    
    current_blend = blend_file or bpy.data.filepath
    
    if current_blend:
        blend_dir = os.path.dirname(current_blend)
        expected_cache_dir = os.path.join(blend_dir, ".preprocess_cache")
    else:
        expected_cache_dir = os.path.join(os.path.expanduser("~"), ".ssmt_preprocess_cache")
    
    need_recreate = False
    
    if _global_cache_manager is None:
        need_recreate = True
    elif current_blend and _global_cache_blend_file != current_blend:
        print(f"[PreprocessCache] 检测到 blend 文件变更: {_global_cache_blend_file} -> {current_blend}")
        need_recreate = True
    elif _global_cache_manager.cache_dir != expected_cache_dir:
        print(f"[PreprocessCache] 缓存目录不匹配: {_global_cache_manager.cache_dir} != {expected_cache_dir}")
        need_recreate = True
    
    if need_recreate:
        if current_blend:
            blend_dir = os.path.dirname(current_blend)
            cache_dir = os.path.join(blend_dir, ".preprocess_cache")
            print(f"[PreprocessCache] 使用项目目录作为缓存目录: {cache_dir}")
            _global_cache_manager = PreprocessCacheManager(cache_dir)
        else:
            print(f"[PreprocessCache] 使用默认缓存目录: {expected_cache_dir}")
            _global_cache_manager = PreprocessCacheManager()
        
        _global_cache_blend_file = current_blend
    
    return _global_cache_manager


def reset_cache_manager():
    """重置缓存管理器"""
    global _global_cache_manager, _global_cache_blend_file
    _global_cache_manager = None
    _global_cache_blend_file = None
