import bpy
import os
import json
import hashlib
import struct
import numpy
import tempfile
import shutil

from ..common.global_properties import GlobalProterties
from ..utils.log_utils import LOG
from ..common.object_prefix_helper import ObjectPrefixHelper


class PreProcessCache:
    CACHE_DIR_NAME = ".ssmt_preprocess_cache"
    CACHE_INDEX_FILENAME = "cache_index.json"
    CACHE_VERSION = 2
    COPY_HASH_PROP = "_ssmt_preprocess_hash"
    COPY_SOURCE_PROP = "_ssmt_preprocess_source"
    COPY_REQUESTED_PROP = "_ssmt_preprocess_requested"
    _override_cache_dir: str = ""
    _override_read_cache_dirs: list[str] = []
    _index_cache_by_path: dict[str, dict] = {}
    _index_cache_mtime_by_path: dict[str, float] = {}

    @classmethod
    def invalidate_index_cache(cls, cache_dir: str = ""):
        if cache_dir:
            index_path = cls.get_cache_index_path_for_dir(cache_dir)
            if index_path:
                cls._index_cache_by_path.pop(index_path, None)
                cls._index_cache_mtime_by_path.pop(index_path, None)
            return

        cls._index_cache_by_path.clear()
        cls._index_cache_mtime_by_path.clear()

    @classmethod
    def set_override_cache_dir(cls, cache_dir: str):
        cls._override_cache_dir = cache_dir
        cls._override_read_cache_dirs = []
        cls.invalidate_index_cache()

    @classmethod
    def set_override_cache_dirs(cls, write_cache_dir: str, read_cache_dirs: list[str] | None = None):
        cls._override_cache_dir = write_cache_dir or ""
        cls._override_read_cache_dirs = [cache_dir for cache_dir in (read_cache_dirs or []) if cache_dir]
        cls.invalidate_index_cache()

    @classmethod
    def clear_override_cache_dir(cls):
        cls._override_cache_dir = ""
        cls._override_read_cache_dirs = []
        cls.invalidate_index_cache()

    @classmethod
    def get_cache_index_path_for_dir(cls, cache_dir: str) -> str:
        if not cache_dir:
            return ""
        return os.path.join(cache_dir, cls.CACHE_INDEX_FILENAME)

    @classmethod
    def get_cache_search_dirs(cls) -> list[str]:
        cache_dirs = []

        write_dir = cls.get_cache_dir()
        if write_dir:
            cache_dirs.append(write_dir)

        for read_dir in cls._override_read_cache_dirs:
            if read_dir and read_dir not in cache_dirs:
                cache_dirs.append(read_dir)

        return cache_dirs

    @classmethod
    def get_cache_dir(cls) -> str:
        if cls._override_cache_dir:
            cache_dir = cls._override_cache_dir
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir)
            return cache_dir
        
        blend_filepath = bpy.data.filepath
        if not blend_filepath:
            return ""
        blend_dir = os.path.dirname(blend_filepath)
        if not blend_dir:
            return ""
        cache_dir = os.path.join(blend_dir, cls.CACHE_DIR_NAME)
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        return cache_dir

    @classmethod
    def get_original_cache_dir(cls) -> str:
        blend_filepath = bpy.data.filepath
        if not blend_filepath:
            return ""
        blend_dir = os.path.dirname(blend_filepath)
        if not blend_dir:
            return ""
        return os.path.join(blend_dir, cls.CACHE_DIR_NAME)

    @classmethod
    def get_cache_index_path(cls) -> str:
        return cls.get_cache_index_path_for_dir(cls.get_cache_dir())

    @classmethod
    def load_cache_index(cls, force_reload: bool = False, cache_dir: str = "") -> dict:
        if not cache_dir:
            cache_dir = cls.get_cache_dir()

        index_path = cls.get_cache_index_path_for_dir(cache_dir)
        if not index_path or not os.path.exists(index_path):
            cls.invalidate_index_cache(cache_dir)
            return {"version": cls.CACHE_VERSION, "entries": {}}
        
        try:
            current_mtime = os.path.getmtime(index_path)
            
            cached_index = cls._index_cache_by_path.get(index_path)
            cached_mtime = cls._index_cache_mtime_by_path.get(index_path)
            if not force_reload and cached_index is not None and cached_mtime == current_mtime:
                return cached_index
            
            with open(index_path, 'r', encoding='utf-8') as f:
                index = json.load(f)
            
            cls._index_cache_by_path[index_path] = index
            cls._index_cache_mtime_by_path[index_path] = current_mtime
            return index
        except json.JSONDecodeError as e:
            LOG.warning(f"⚠️ 缓存索引文件损坏: {e}")
            return {"version": cls.CACHE_VERSION, "entries": {}}
        except Exception as e:
            LOG.warning(f"⚠️ 缓存索引文件读取失败: {e}")
            return {"version": cls.CACHE_VERSION, "entries": {}}

    @classmethod
    def _backup_and_rebuild_corrupted_index(cls, index_path: str, new_entries: list[dict]):
        try:
            backup_path = index_path + ".corrupted"
            counter = 1
            while os.path.exists(backup_path):
                backup_path = f"{index_path}.corrupted.{counter}"
                counter += 1
            shutil.copy2(index_path, backup_path)
            LOG.info(f"📦 已备份损坏的索引文件到: {backup_path}")
            os.remove(index_path)
            LOG.info("🧹 已删除损坏的索引文件，将重建缓存索引")
            
            index = {"version": cls.CACHE_VERSION, "entries": {}}
            for entry in new_entries:
                index["entries"][entry["hash_value"]] = {
                    "object_name": entry["obj_name"],
                    "cache_file": entry["cache_filename"],
                    "timestamp": entry["timestamp"],
                    "file_size": entry["file_size"],
                }
            cls._atomic_write_json(index_path, index)
        except Exception as e:
            LOG.warning(f"⚠️ 备份损坏索引文件失败: {e}")

    @classmethod
    def save_cache_index(cls, index: dict):
        index_path = cls.get_cache_index_path()
        if not index_path:
            return
        try:
            cls._atomic_write_json(index_path, index)
        except Exception as e:
            LOG.warning(f"⚠️ 缓存索引文件保存失败: {e}")

    @classmethod
    def _atomic_write_json(cls, filepath: str, data: dict):
        dir_path = os.path.dirname(filepath)
        fd, temp_path = tempfile.mkstemp(suffix='.tmp', dir=dir_path)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            if os.path.exists(filepath):
                backup_path = filepath + '.bak'
                try:
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
                    os.rename(filepath, backup_path)
                except Exception:
                    os.remove(filepath)
            os.rename(temp_path, filepath)
            
            cls._index_cache_by_path[filepath] = data
            cls._index_cache_mtime_by_path[filepath] = os.path.getmtime(filepath)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    @classmethod
    def resolve_source_object(cls, obj_name: str):
        requested_name = obj_name or ""
        obj = bpy.data.objects.get(requested_name)
        if obj is not None:
            return obj, obj.name

        source_obj_name = ObjectPrefixHelper.resolve_source_object_name(requested_name)
        obj = bpy.data.objects.get(source_obj_name)
        if obj is not None:
            return obj, obj.name

        return None, source_obj_name

    @classmethod
    def tag_runtime_copy(cls, copy_obj, requested_name: str, source_name: str, hash_value: str = ""):
        if copy_obj is None:
            return

        try:
            copy_obj[cls.COPY_REQUESTED_PROP] = requested_name or ""
            copy_obj[cls.COPY_SOURCE_PROP] = source_name or ""
            copy_obj[cls.COPY_HASH_PROP] = hash_value or ""
        except Exception:
            pass

    @classmethod
    def runtime_copy_matches(cls, copy_obj, requested_name: str, source_name: str, hash_value: str = "") -> bool:
        if copy_obj is None:
            return False

        try:
            stored_requested = copy_obj.get(cls.COPY_REQUESTED_PROP, "")
            stored_source = copy_obj.get(cls.COPY_SOURCE_PROP, "")
            stored_hash = copy_obj.get(cls.COPY_HASH_PROP, "")
        except Exception:
            return False

        if requested_name and stored_requested != requested_name:
            return False
        if source_name and stored_source != source_name:
            return False
        if hash_value and stored_hash != hash_value:
            return False

        return bool(stored_requested or stored_source or stored_hash)

    @classmethod
    def remove_runtime_copy(cls, copy_name: str):
        if not copy_name:
            return

        existing_copy = bpy.data.objects.get(copy_name)
        if existing_copy is None:
            return

        try:
            bpy.data.objects.remove(existing_copy, do_unlink=True)
        except Exception:
            pass

    @classmethod
    def compute_object_hash(cls, obj_name: str) -> str:
        obj, source_obj_name = cls.resolve_source_object(obj_name)
        if obj is None:
            return ""

        hasher = hashlib.sha256()

        hasher.update(source_obj_name.encode('utf-8'))
        hasher.update(obj.type.encode('utf-8'))
        hasher.update(struct.pack('<?', GlobalProterties.enable_non_mirror_workflow()))

        loc = numpy.array(obj.location, dtype=numpy.float32)
        rot = numpy.array(obj.rotation_euler, dtype=numpy.float32)
        scl = numpy.array(obj.scale, dtype=numpy.float32)
        hasher.update(loc.tobytes())
        hasher.update(rot.tobytes())
        hasher.update(scl.tobytes())

        if obj.type == 'MESH' and obj.data:
            mesh = obj.data

            n_verts = len(mesh.vertices)
            if n_verts > 0:
                verts = numpy.empty(n_verts * 3, dtype=numpy.float32)
                mesh.vertices.foreach_get('co', verts)
                hasher.update(verts.tobytes())

            n_loops = len(mesh.loops)
            for uv_layer in mesh.uv_layers:
                hasher.update(uv_layer.name.encode('utf-8'))
                if n_loops > 0:
                    uv_data = numpy.empty(n_loops * 2, dtype=numpy.float32)
                    uv_layer.data.foreach_get('uv', uv_data)
                    hasher.update(uv_data.tobytes())

            if hasattr(mesh, 'color_attributes'):
                for color_attr in mesh.color_attributes:
                    hasher.update(color_attr.name.encode('utf-8'))
                    if n_loops > 0:
                        color_data = numpy.empty(n_loops * 4, dtype=numpy.float32)
                        color_attr.data.foreach_get('color', color_data)
                        hasher.update(color_data.tobytes())

        for modifier in obj.modifiers:
            hasher.update(modifier.type.encode('utf-8'))
            hasher.update(modifier.name.encode('utf-8'))
            hasher.update(struct.pack('<?', modifier.show_viewport))
            cls._hash_rna_properties(hasher, modifier)

        if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
            key_blocks = obj.data.shape_keys.key_blocks
            hasher.update(struct.pack('<I', len(key_blocks)))
            print(f"[HashDebug] 物体 {obj_name} (源物体 {source_obj_name}) 形态键哈希计算:")
            for kb in key_blocks:
                hasher.update(kb.name.encode('utf-8'))
                hasher.update(struct.pack('<d', kb.value))
                hasher.update(struct.pack('<?', kb.mute))
                print(f"[HashDebug]   {kb.name}: value={kb.value}, mute={kb.mute}")
                if kb.data:
                    n_sk_verts = len(kb.data)
                    if n_sk_verts > 0:
                        sk_verts = numpy.empty(n_sk_verts * 3, dtype=numpy.float32)
                        kb.data.foreach_get('co', sk_verts)
                        hasher.update(sk_verts.tobytes())

        for vg in obj.vertex_groups:
            hasher.update(vg.name.encode('utf-8'))

        if obj.type == 'MESH' and obj.data and obj.vertex_groups:
            mesh = obj.data
            weight_bytes = bytearray()
            for vert in mesh.vertices:
                for group_elem in vert.groups:
                    weight_bytes.extend(struct.pack('<If', group_elem.group, group_elem.weight))
            if weight_bytes:
                hasher.update(bytes(weight_bytes))

        for constraint in obj.constraints:
            hasher.update(constraint.type.encode('utf-8'))
            hasher.update(constraint.name.encode('utf-8'))
            cls._hash_rna_properties(hasher, constraint)

        return hasher.hexdigest()

    @classmethod
    def _hash_rna_properties(cls, hasher, rna_obj):
        try:
            for prop in rna_obj.bl_rna.properties:
                if prop.is_readonly:
                    continue
                try:
                    prop_identifier = prop.identifier
                    prop_value = getattr(rna_obj, prop_identifier, None)
                    if prop_value is None:
                        continue
                    hasher.update(prop_identifier.encode('utf-8'))
                    if isinstance(prop_value, (bool, int, float)):
                        hasher.update(struct.pack('<d', float(prop_value)))
                    elif isinstance(prop_value, str):
                        hasher.update(prop_value.encode('utf-8'))
                    elif hasattr(prop_value, 'name'):
                        hasher.update(prop_value.name.encode('utf-8'))
                    elif isinstance(prop_value, (tuple, list)):
                        for item in prop_value:
                            if isinstance(item, (bool, int, float)):
                                hasher.update(struct.pack('<d', float(item)))
                            elif isinstance(item, str):
                                hasher.update(item.encode('utf-8'))
                except Exception:
                    pass
        except Exception:
            pass

    @classmethod
    def has_cache(cls, hash_value: str) -> bool:
        cache_dir, entry = cls.resolve_cache_entry(hash_value)
        if not entry:
            return False

        cache_file = os.path.join(cache_dir, entry.get("cache_file", ""))
        return os.path.exists(cache_file)

    @classmethod
    def resolve_cache_entry(cls, hash_value: str) -> tuple[str, dict]:
        if not hash_value:
            return "", {}

        for cache_dir in cls.get_cache_search_dirs():
            index = cls.load_cache_index(cache_dir=cache_dir)
            entry = index.get("entries", {}).get(hash_value)
            if not entry:
                continue

            cache_file = os.path.join(cache_dir, entry.get("cache_file", ""))
            if os.path.exists(cache_file):
                return cache_dir, entry

        return "", {}

    @classmethod
    def save_to_cache(cls, obj_name: str, copy_name: str, hash_value: str):
        copy_obj = bpy.data.objects.get(copy_name)
        if not copy_obj:
            LOG.warning(f"⚠️ 缓存保存跳过: 找不到副本物体 {copy_name}")
            return
        if copy_obj.type != 'MESH':
            LOG.warning(f"⚠️ 缓存保存跳过: {copy_name} 不是网格类型 (类型: {copy_obj.type})")
            return
        if not copy_obj.data:
            LOG.warning(f"⚠️ 缓存保存跳过: {copy_name} 没有网格数据")
            return

        cache_dir = cls.get_cache_dir()
        if not cache_dir:
            LOG.warning(f"⚠️ 缓存保存跳过: 无法获取缓存目录 (blend文件可能未保存)")
            return

        cache_filename = f"{hash_value}.blend"
        cache_filepath = os.path.join(cache_dir, cache_filename)

        saved_materials = []
        for slot in copy_obj.material_slots:
            saved_materials.append(slot.material)
            slot.material = None

        try:
            datablocks = {copy_obj, copy_obj.data}
            bpy.data.libraries.write(cache_filepath, datablocks, compress=True, fake_user=True)
        except Exception as e:
            LOG.warning(f"⚠️ 缓存文件保存失败 {obj_name}: {e}")
            for i, mat in enumerate(saved_materials):
                if i < len(copy_obj.material_slots):
                    copy_obj.material_slots[i].material = mat
            return

        for i, mat in enumerate(saved_materials):
            if i < len(copy_obj.material_slots):
                copy_obj.material_slots[i].material = mat

        file_size = os.path.getsize(cache_filepath) if os.path.exists(cache_filepath) else 0
        timestamp = int(os.path.getmtime(cache_filepath)) if os.path.exists(cache_filepath) else 0

        cls._add_entry_to_cache_index(hash_value, obj_name, cache_filename, timestamp, file_size)
        LOG.info(f"   💾 缓存已保存: {obj_name} -> {cache_filename}")

    @classmethod
    def _add_entry_to_cache_index(cls, hash_value: str, obj_name: str, cache_filename: str, timestamp: int, file_size: int):
        index_path = cls.get_cache_index_path()
        if not index_path:
            return
        
        max_retries = 5
        retry_delay = 0.1
        
        for attempt in range(max_retries):
            try:
                index = cls.load_cache_index(force_reload=True)
                
                if "entries" not in index:
                    index["entries"] = {}
                
                index["entries"][hash_value] = {
                    "object_name": obj_name,
                    "cache_file": cache_filename,
                    "timestamp": timestamp,
                    "file_size": file_size,
                }
                
                cls._atomic_write_json(index_path, index)
                return
                
            except json.JSONDecodeError as e:
                LOG.warning(f"⚠️ 缓存索引文件损坏，将备份并重建: {e}")
                entry = {
                    "hash_value": hash_value,
                    "obj_name": obj_name,
                    "cache_filename": cache_filename,
                    "timestamp": timestamp,
                    "file_size": file_size,
                }
                cls._backup_and_rebuild_corrupted_index(index_path, [entry])
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    LOG.warning(f"⚠️ 缓存索引保存失败 (重试 {max_retries} 次后): {e}")

    @classmethod
    def batch_save_to_cache(cls, items: list[tuple[str, str, str]]) -> int:
        """
        批量保存缓存，减少索引文件IO次数
        items: [(obj_name, copy_name, hash_value), ...]
        返回成功保存的数量
        """
        if not items:
            return 0
        
        cache_dir = cls.get_cache_dir()
        if not cache_dir:
            LOG.warning("⚠️ 批量缓存保存跳过: 无法获取缓存目录")
            return 0
        
        cache_entries = []
        saved_count = 0
        
        for obj_name, copy_name, hash_value in items:
            copy_obj = bpy.data.objects.get(copy_name)
            if not copy_obj:
                LOG.warning(f"⚠️ 缓存保存跳过: 找不到副本物体 {copy_name}")
                continue
            if copy_obj.type != 'MESH':
                continue
            if not copy_obj.data:
                continue
            
            cache_filename = f"{hash_value}.blend"
            cache_filepath = os.path.join(cache_dir, cache_filename)
            
            saved_materials = []
            for slot in copy_obj.material_slots:
                saved_materials.append(slot.material)
                slot.material = None
            
            try:
                datablocks = {copy_obj, copy_obj.data}
                bpy.data.libraries.write(cache_filepath, datablocks, compress=True, fake_user=True)
            except Exception as e:
                LOG.warning(f"⚠️ 缓存文件保存失败 {obj_name}: {e}")
                for i, mat in enumerate(saved_materials):
                    if i < len(copy_obj.material_slots):
                        copy_obj.material_slots[i].material = mat
                continue
            
            for i, mat in enumerate(saved_materials):
                if i < len(copy_obj.material_slots):
                    copy_obj.material_slots[i].material = mat
            
            file_size = os.path.getsize(cache_filepath) if os.path.exists(cache_filepath) else 0
            timestamp = int(os.path.getmtime(cache_filepath)) if os.path.exists(cache_filepath) else 0
            
            cache_entries.append({
                "hash_value": hash_value,
                "obj_name": obj_name,
                "cache_filename": cache_filename,
                "timestamp": timestamp,
                "file_size": file_size,
            })
            saved_count += 1
        
        if cache_entries:
            cls._batch_add_entries_to_cache_index(cache_entries)
            LOG.info(f"   💾 批量缓存已保存: {saved_count} 个物体")
        
        return saved_count

    @classmethod
    def _batch_add_entries_to_cache_index(cls, entries: list[dict]):
        """批量添加条目到缓存索引，只读写一次文件"""
        if not entries:
            return
        
        index_path = cls.get_cache_index_path()
        if not index_path:
            return
        
        max_retries = 5
        retry_delay = 0.1
        
        for attempt in range(max_retries):
            try:
                index = cls.load_cache_index(force_reload=True)
                
                if "entries" not in index:
                    index["entries"] = {}
                
                for entry in entries:
                    index["entries"][entry["hash_value"]] = {
                        "object_name": entry["obj_name"],
                        "cache_file": entry["cache_filename"],
                        "timestamp": entry["timestamp"],
                        "file_size": entry["file_size"],
                    }
                
                cls._atomic_write_json(index_path, index)
                return
                
            except json.JSONDecodeError as e:
                LOG.warning(f"⚠️ 缓存索引文件损坏，将备份并重建: {e}")
                cls._backup_and_rebuild_corrupted_index(index_path, entries)
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    LOG.warning(f"⚠️ 批量缓存索引保存失败 (重试 {max_retries} 次后): {e}")

    @classmethod
    def merge_cache_dir(cls, source_cache_dir: str, target_cache_dir: str = "") -> int:
        if not source_cache_dir or not os.path.exists(source_cache_dir):
            return 0

        if not target_cache_dir:
            target_cache_dir = cls.get_original_cache_dir() or cls.get_cache_dir()
        if not target_cache_dir:
            return 0

        os.makedirs(target_cache_dir, exist_ok=True)

        source_index = cls.load_cache_index(force_reload=True, cache_dir=source_cache_dir)
        source_entries = source_index.get("entries", {})
        if not source_entries:
            return 0

        target_index = cls.load_cache_index(force_reload=True, cache_dir=target_cache_dir)
        if "entries" not in target_index:
            target_index["entries"] = {}

        merged_count = 0
        target_updated = False

        for hash_value, entry in source_entries.items():
            cache_filename = entry.get("cache_file", "")
            if not cache_filename:
                continue

            source_file = os.path.join(source_cache_dir, cache_filename)
            if not os.path.exists(source_file):
                continue

            target_file = os.path.join(target_cache_dir, cache_filename)
            if not os.path.exists(target_file):
                shutil.copy2(source_file, target_file)

            normalized_entry = {
                "object_name": entry.get("object_name", ""),
                "cache_file": cache_filename,
                "timestamp": entry.get("timestamp", 0),
                "file_size": entry.get("file_size", 0),
            }

            if target_index["entries"].get(hash_value) == normalized_entry and os.path.exists(target_file):
                continue

            target_index["entries"][hash_value] = normalized_entry
            target_updated = True
            merged_count += 1

        if target_updated:
            target_index_path = cls.get_cache_index_path_for_dir(target_cache_dir)
            cls._atomic_write_json(target_index_path, target_index)

        return merged_count

    @classmethod
    def load_from_cache(cls, obj_name: str, hash_value: str) -> bool:
        cache_dir, entry = cls.resolve_cache_entry(hash_value)
        if not entry:
            return False

        cache_filepath = os.path.join(cache_dir, entry["cache_file"])

        if not os.path.exists(cache_filepath):
            return False

        copy_name = f"{obj_name}_copy"
        _, source_obj_name = cls.resolve_source_object(obj_name)

        existing_copy = bpy.data.objects.get(copy_name)
        if existing_copy:
            if cls.runtime_copy_matches(existing_copy, obj_name, source_obj_name, hash_value):
                return True
            cls.remove_runtime_copy(copy_name)

        try:
            with bpy.data.libraries.load(cache_filepath) as (data_from, data_to):
                all_cached_names = list(data_from.objects)
                LOG.debug(f"   📦 缓存文件中的物体: {all_cached_names}")
                
                target_obj_name = None
                for name in data_from.objects:
                    if name == copy_name or name == obj_name or name == f"{obj_name}_copy":
                        target_obj_name = name
                        break
                
                if not target_obj_name:
                    for name in data_from.objects:
                        if name.endswith('_copy') and name[:-5] == obj_name:
                            target_obj_name = name
                            break
                
                if not target_obj_name and data_from.objects:
                    target_obj_name = data_from.objects[0]
                    LOG.warning(f"   ⚠️ 缓存文件中未找到精确匹配的物体，使用第一个: {target_obj_name}")
                
                if target_obj_name:
                    data_to.objects = [target_obj_name]
                else:
                    LOG.warning(f"⚠️ 缓存文件中未找到物体 {obj_name}")
                    return False

            if not data_to.objects or len(data_to.objects) == 0:
                LOG.warning(f"⚠️ 缓存文件中未找到物体 {obj_name}")
                return False

            loaded_obj = data_to.objects[0]

            loaded_obj.name = copy_name
            if loaded_obj.data:
                loaded_obj.data.name = f"{copy_name}_mesh"

            bpy.context.scene.collection.objects.link(loaded_obj)
            cls.tag_runtime_copy(loaded_obj, obj_name, source_obj_name, hash_value)

            loaded_obj.location = (0, 0, 0)
            loaded_obj.rotation_euler = (0, 0, 0)
            loaded_obj.scale = (1, 1, 1)

            LOG.info(f"   📦 缓存已加载: {obj_name} <- {entry['cache_file']}")
            return True

        except Exception as e:
            LOG.warning(f"⚠️ 缓存加载失败 {obj_name}: {e}")

            if copy_name in bpy.data.objects:
                try:
                    bpy.data.objects.remove(bpy.data.objects[copy_name], do_unlink=True)
                except Exception:
                    pass

            return False

    @classmethod
    def clear_cache(cls) -> int:
        cache_dir = cls.get_cache_dir()
        if not cache_dir or not os.path.exists(cache_dir):
            return 0

        cleared_count = 0
        for filename in os.listdir(cache_dir):
            filepath = os.path.join(cache_dir, filename)
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
                    cleared_count += 1
            except Exception as e:
                LOG.warning(f"⚠️ 删除缓存文件失败 {filename}: {e}")

        LOG.info(f"🧹 缓存已清空: 删除 {cleared_count} 个文件")
        return cleared_count

    @classmethod
    def get_cache_stats(cls) -> dict:
        cache_dir = cls.get_cache_dir()
        if not cache_dir or not os.path.exists(cache_dir):
            return {"file_count": 0, "total_size": 0}

        file_count = 0
        total_size = 0

        for filename in os.listdir(cache_dir):
            filepath = os.path.join(cache_dir, filename)
            if os.path.isfile(filepath):
                file_count += 1
                total_size += os.path.getsize(filepath)

        return {"file_count": file_count, "total_size": total_size}

    @classmethod
    def format_size(cls, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def register():
    pass


def unregister():
    pass
