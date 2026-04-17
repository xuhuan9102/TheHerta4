import bpy
import os
import json
import hashlib
import struct
import numpy

from ..utils.log_utils import LOG


class PreProcessCache:
    CACHE_DIR_NAME = ".ssmt_preprocess_cache"
    CACHE_INDEX_FILENAME = "cache_index.json"
    CACHE_VERSION = 2

    @classmethod
    def get_cache_dir(cls) -> str:
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
    def get_cache_index_path(cls) -> str:
        cache_dir = cls.get_cache_dir()
        if not cache_dir:
            return ""
        return os.path.join(cache_dir, cls.CACHE_INDEX_FILENAME)

    @classmethod
    def load_cache_index(cls) -> dict:
        index_path = cls.get_cache_index_path()
        if not index_path or not os.path.exists(index_path):
            return {"version": cls.CACHE_VERSION, "entries": {}}
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            LOG.warning(f"⚠️ 缓存索引文件读取失败: {e}")
            return {"version": cls.CACHE_VERSION, "entries": {}}

    @classmethod
    def save_cache_index(cls, index: dict):
        index_path = cls.get_cache_index_path()
        if not index_path:
            return
        try:
            with open(index_path, 'w', encoding='utf-8') as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
        except Exception as e:
            LOG.warning(f"⚠️ 缓存索引文件保存失败: {e}")

    @classmethod
    def compute_object_hash(cls, obj_name: str) -> str:
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            return ""

        hasher = hashlib.sha256()

        hasher.update(obj_name.encode('utf-8'))
        hasher.update(obj.type.encode('utf-8'))

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
            hasher.update(modifier.bl_idname.encode('utf-8'))
            hasher.update(modifier.name.encode('utf-8'))
            hasher.update(struct.pack('<?', modifier.show_viewport))
            cls._hash_rna_properties(hasher, modifier)

        if obj.type == 'MESH' and obj.data and obj.data.shape_keys:
            key_blocks = obj.data.shape_keys.key_blocks
            hasher.update(struct.pack('<I', len(key_blocks)))
            for kb in key_blocks:
                hasher.update(kb.name.encode('utf-8'))
                hasher.update(struct.pack('<d', kb.value))
                hasher.update(struct.pack('<?', kb.mute))
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
            hasher.update(constraint.bl_idname.encode('utf-8'))
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
        index = cls.load_cache_index()
        entry = index.get("entries", {}).get(hash_value)
        if not entry:
            return False
        cache_dir = cls.get_cache_dir()
        cache_file = os.path.join(cache_dir, entry.get("cache_file", ""))
        return os.path.exists(cache_file)

    @classmethod
    def save_to_cache(cls, obj_name: str, copy_name: str, hash_value: str):
        copy_obj = bpy.data.objects.get(copy_name)
        if not copy_obj or copy_obj.type != 'MESH' or not copy_obj.data:
            return

        cache_dir = cls.get_cache_dir()
        if not cache_dir:
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

        index = cls.load_cache_index()
        file_size = os.path.getsize(cache_filepath) if os.path.exists(cache_filepath) else 0

        index["entries"][hash_value] = {
            "object_name": obj_name,
            "cache_file": cache_filename,
            "timestamp": int(os.path.getmtime(cache_filepath)) if os.path.exists(cache_filepath) else 0,
            "file_size": file_size,
        }

        cls.save_cache_index(index)
        LOG.info(f"   💾 缓存已保存: {obj_name} -> {cache_filename}")

    @classmethod
    def load_from_cache(cls, obj_name: str, hash_value: str) -> bool:
        index = cls.load_cache_index()
        entry = index.get("entries", {}).get(hash_value)
        if not entry:
            return False

        cache_dir = cls.get_cache_dir()
        cache_filepath = os.path.join(cache_dir, entry["cache_file"])

        if not os.path.exists(cache_filepath):
            return False

        copy_name = f"{obj_name}_copy"

        existing_copy = bpy.data.objects.get(copy_name)
        if existing_copy:
            return True

        try:
            with bpy.data.libraries.load(cache_filepath) as (data_from, data_to):
                data_to.objects = data_from.objects

            if not data_to.objects or len(data_to.objects) == 0:
                LOG.warning(f"⚠️ 缓存文件中未找到物体 {obj_name}")
                return False

            loaded_obj = data_to.objects[0]

            loaded_obj.name = copy_name
            if loaded_obj.data:
                loaded_obj.data.name = f"{copy_name}_mesh"

            bpy.context.collection.objects.link(loaded_obj)

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
