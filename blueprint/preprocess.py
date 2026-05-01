import bpy
import re
from typing import List, Dict

from ..utils.log_utils import LOG
from ..utils.shapekey_utils import ShapeKeyUtils
from .export_helper import BlueprintExportHelper
from .preprocess_cache import PreProcessCache
from ..common.global_properties import GlobalProterties
from ..common.non_mirror_workflow import NonMirrorWorkflowHelper
from ..common.object_prefix_helper import ObjectPrefixHelper


class PreProcessHelper:
    original_to_copy_map: Dict[str, str] = {}
    created_copies: List[str] = []
    modified_nodes: List[tuple] = []

    @classmethod
    def reset_runtime_state(cls):
        cls.original_to_copy_map.clear()
        cls.created_copies.clear()
        cls.modified_nodes.clear()

    @classmethod
    def register_copy_result(cls, original_name: str, copy_name: str):
        cls.original_to_copy_map[original_name] = copy_name
        if copy_name not in cls.created_copies:
            cls.created_copies.append(copy_name)

    @classmethod
    def collect_target_object_names(cls, object_names: List[str]) -> List[str]:
        unique_objects = list(set(object_names))

        multi_file_objects = BlueprintExportHelper.get_all_objects_from_multi_file_nodes()
        for obj_name in multi_file_objects:
            if obj_name not in unique_objects:
                unique_objects.append(obj_name)

        if multi_file_objects:
            LOG.info(f"📋 多文件导出节点物体: {len(multi_file_objects)} 个")

        return unique_objects

    @classmethod
    def execute_objects_without_cache(cls, object_names: List[str]) -> Dict[str, str]:
        cls.reset_runtime_state()
        cls._execute_preprocess_without_cache(object_names)
        return dict(cls.original_to_copy_map)

    @classmethod
    def execute_preprocess(cls, object_names: List[str]) -> Dict[str, str]:
        cls.reset_runtime_state()

        unique_objects = cls.collect_target_object_names(object_names)
        
        cache_enabled = GlobalProterties.enable_preprocess_cache()
        
        if cache_enabled:
            cls._execute_preprocess_with_cache(unique_objects)
        else:
            cls._execute_preprocess_without_cache(unique_objects)
        
        LOG.info(f"🔧 前处理完成: {len(unique_objects)} 个物体")
        
        return cls.original_to_copy_map

    @classmethod
    def _execute_preprocess_without_cache(cls, object_names: List[str]):
        cls._create_object_copies(object_names)
        
        copy_names = list(cls.original_to_copy_map.values())
        
        LOG.info(f"🔧 对所有副本物体应用约束...")
        cls._apply_constraints(copy_names)
        
        LOG.info(f"🔧 对所有副本物体应用修改器...")
        cls._apply_modifiers(copy_names)
        
        LOG.info(f"🔧 对所有物体执行三角化...")
        cls._triangulate_objects(copy_names)
        
        LOG.info(f"🔧 对所有物体执行应用变换...")
        cls._apply_transforms(copy_names)
        
        LOG.info(f"🔧 对所有副本物体应用形态键...")
        cls._apply_shape_keys(copy_names)

        LOG.info(f"🔧 对所有副本物体重命名活动UV为TEXCOORD.xy...")
        cls._rename_uv_layers(copy_names)

        if GlobalProterties.enable_non_mirror_workflow():
            LOG.info(f"🔧 对所有副本物体执行非镜像工作流恢复...")
            cls._restore_non_mirror_objects(copy_names)

    @classmethod
    def _execute_preprocess_with_cache(cls, object_names: List[str]):
        hash_map = {}
        cached_objects = {}
        uncached_objects = []
        
        LOG.info(f"🔐 计算物体哈希值...")
        for obj_name in object_names:
            hash_value = PreProcessCache.compute_object_hash(obj_name)
            hash_map[obj_name] = hash_value
            if hash_value and PreProcessCache.has_cache(hash_value):
                cached_objects[obj_name] = hash_value
            else:
                uncached_objects.append(obj_name)
        
        if cached_objects:
            LOG.info(f"📦 缓存命中: {len(cached_objects)} 个物体")
        if uncached_objects:
            LOG.info(f"🔄 缓存未命中: {len(uncached_objects)} 个物体, 需要重新前处理")
        
        for obj_name, hash_value in cached_objects.items():
            copy_name = f"{obj_name}_copy"
            success = PreProcessCache.load_from_cache(obj_name, hash_value)
            if success:
                cls.register_copy_result(obj_name, copy_name)
            else:
                LOG.warning(f"   ⚠️ 缓存加载失败 {obj_name}, 将重新前处理")
                uncached_objects.append(obj_name)
        
        if uncached_objects:
            cls._create_object_copies(uncached_objects, hash_map=hash_map)
            
            copy_names = [cls.original_to_copy_map[name] for name in uncached_objects if name in cls.original_to_copy_map]
            
            LOG.info(f"🔧 对未缓存副本物体应用约束...")
            cls._apply_constraints(copy_names)
            
            LOG.info(f"🔧 对未缓存副本物体应用修改器...")
            cls._apply_modifiers(copy_names)
            
            LOG.info(f"🔧 对未缓存物体执行三角化...")
            cls._triangulate_objects(copy_names)
            
            LOG.info(f"🔧 对未缓存物体执行应用变换...")
            cls._apply_transforms(copy_names)
            
            LOG.info(f"🔧 对未缓存副本物体应用形态键...")
            cls._apply_shape_keys(copy_names)

            LOG.info(f"🔧 对未缓存副本物体重命名活动UV为TEXCOORD.xy...")
            cls._rename_uv_layers(copy_names)

            if GlobalProterties.enable_non_mirror_workflow():
                LOG.info(f"🔧 对未缓存副本物体执行非镜像工作流恢复...")
                cls._restore_non_mirror_objects(copy_names)
            
            LOG.info(f"💾 保存前处理结果到缓存...")
            for obj_name in uncached_objects:
                if obj_name in cls.original_to_copy_map:
                    copy_name = cls.original_to_copy_map[obj_name]
                    hash_value = hash_map.get(obj_name, "")
                    if hash_value:
                        PreProcessCache.save_to_cache(obj_name, copy_name, hash_value)
                    else:
                        LOG.warning(f"⚠️ 缓存保存跳过: {obj_name} 哈希值为空")

    @classmethod
    def _create_object_copies(cls, object_names: List[str], hash_map: Dict[str, str] = None):
        created_count = 0
        existing_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj, source_obj_name = PreProcessCache.resolve_source_object(obj_name)
            if not obj:
                LOG.warning(f"   找不到源物体 {obj_name} (解析源名称: {source_obj_name})")
                failed_count += 1
                continue
            
            copy_name = f"{obj_name}_copy"
            expected_hash = hash_map.get(obj_name, "") if hash_map else ""
            
            existing_copy = bpy.data.objects.get(copy_name)
            if existing_copy:
                if PreProcessCache.runtime_copy_matches(existing_copy, obj_name, source_obj_name, expected_hash):
                    existing_count += 1
                    cls.register_copy_result(obj_name, copy_name)
                    continue

                PreProcessCache.remove_runtime_copy(copy_name)
            
            obj_copy = obj.copy()
            obj_copy.name = copy_name
            
            if obj.data:
                obj_copy.data = obj.data.copy()
            
            bpy.context.scene.collection.objects.link(obj_copy)
            PreProcessCache.tag_runtime_copy(obj_copy, obj_name, source_obj_name, expected_hash)
            
            original_shapekey_count = 0
            if obj.data.shape_keys:
                original_shapekey_count = len(obj.data.shape_keys.key_blocks)
            copy_shapekey_count = 0
            if obj_copy.data.shape_keys:
                copy_shapekey_count = len(obj_copy.data.shape_keys.key_blocks)
            
            if original_shapekey_count > 0:
                LOG.info(f"   副本 {copy_name}: 原始形态键 {original_shapekey_count} -> 副本形态键 {copy_shapekey_count}")
            
            cls.register_copy_result(obj_name, copy_name)
            created_count += 1
        
        LOG.info(f"📋 创建副本: 成功 {created_count} 个, 已存在 {existing_count} 个, 失败 {failed_count} 个")

    @classmethod
    def _apply_constraints(cls, object_names: List[str]):
        applied_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            if not obj.constraints:
                continue
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            constraint_names = [c.name for c in obj.constraints]
            for constraint_name in constraint_names:
                try:
                    bpy.ops.object.constraint_apply(constraint=constraint_name)
                    applied_count += 1
                except Exception as e:
                    LOG.warning(f"   应用约束失败 {obj_name}.{constraint_name}: {e}")
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        LOG.info(f"   ✅ 应用约束: {applied_count} 个")

    @classmethod
    def _apply_modifiers(cls, object_names: List[str]):
        applied_count = 0
        removed_disabled_count = 0
        failed_count = 0
        shapekey_count = 0
        no_modifier_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            if obj.type != 'MESH':
                LOG.info(f"   {obj_name}: 非网格物体，跳过")
                continue
            
            modifier_count = len(obj.modifiers)
            has_shape_keys = obj.data.shape_keys and len(obj.data.shape_keys.key_blocks) > 0
            shape_key_count_before = len(obj.data.shape_keys.key_blocks) if has_shape_keys else 0
            
            if modifier_count == 0:
                no_modifier_count += 1
                LOG.info(f"   {obj_name}: 无修改器 (形态键: {shape_key_count_before})")
                continue
            
            LOG.info(f"   {obj_name}: {modifier_count} 个修改器, {shape_key_count_before} 个形态键")
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            if has_shape_keys:
                modifier_names = [m.name for m in obj.modifiers if m.show_viewport]
                if modifier_names:
                    success, error = ShapeKeyUtils.apply_modifiers_for_object_with_shape_keys_optimized(
                        bpy.context, modifier_names, disable_armatures=False
                    )
                    if success:
                        applied_count += len(modifier_names)
                        shapekey_count += 1
                        shape_key_count_after = len(obj.data.shape_keys.key_blocks) if obj.data.shape_keys else 0
                        LOG.info(f"   ✅ {obj_name}: 修改器应用成功，形态键 {shape_key_count_before} -> {shape_key_count_after}")
                    else:
                        LOG.warning(f"   ❌ {obj_name}: 应用修改器失败 - {error}")
                        failed_count += 1
            else:
                modifier_names = [m.name for m in obj.modifiers if m.show_viewport]
                for modifier_name in modifier_names:
                    try:
                        bpy.ops.object.modifier_apply(modifier=modifier_name)
                        applied_count += 1
                    except Exception as e:
                        LOG.warning(f"   ❌ {obj_name}.{modifier_name}: 应用失败 - {e}")
                        failed_count += 1
            
            disabled_modifier_names = [m.name for m in obj.modifiers if not m.show_viewport]
            for mod_name in reversed(disabled_modifier_names):
                try:
                    mod = obj.modifiers.get(mod_name)
                    if mod is not None:
                        obj.modifiers.remove(mod)
                        removed_disabled_count += 1
                except Exception as e:
                    LOG.warning(f"   ⚠️ {obj_name}: 删除禁用修改器 {mod_name} 失败 - {e}")
            
            if disabled_modifier_names:
                LOG.info(f"   🗑️ {obj_name}: 已删除 {len(disabled_modifier_names)} 个禁用修改器")
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        if removed_disabled_count > 0:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个, 删除禁用修改器: {removed_disabled_count} 个")
        elif shapekey_count > 0:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个 (含 {shapekey_count} 个有形态键物体)")
        elif no_modifier_count > 0:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个 ({no_modifier_count} 个物体无修改器)")
        else:
            LOG.info(f"   ✅ 应用修改器: {applied_count} 个")

    @classmethod
    def _triangulate_objects(cls, object_names: List[str]):
        triangulated_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            if obj.type != 'MESH':
                continue
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
                bpy.ops.object.mode_set(mode='OBJECT')
                triangulated_count += 1
            except Exception as e:
                LOG.warning(f"   三角化失败 {obj_name}: {e}")
                failed_count += 1
                try:
                    bpy.ops.object.mode_set(mode='OBJECT')
                except:
                    pass
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        LOG.info(f"   ✅ 三角化: {triangulated_count} 个物体")

    @classmethod
    def _apply_transforms(cls, object_names: List[str]):
        applied_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            try:
                bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
                applied_count += 1
            except Exception as e:
                LOG.warning(f"   应用变换失败 {obj_name}: {e}")
                failed_count += 1
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        LOG.info(f"   ✅ 应用变换: {applied_count} 个物体")

    @classmethod
    def _apply_shape_keys(cls, object_names: List[str]):
        applied_count = 0
        no_shapekey_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            if obj.type != 'MESH':
                continue
            
            shapekey_count = 0
            if obj.data.shape_keys:
                shapekey_count = len(obj.data.shape_keys.key_blocks)
            
            if shapekey_count == 0:
                no_shapekey_count += 1
                LOG.info(f"   {obj_name}: 无形态键")
                continue
            
            LOG.info(f"   {obj_name}: 有 {shapekey_count} 个形态键，准备应用...")
            
            original_active = bpy.context.view_layer.objects.active
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            try:
                bpy.ops.object.shape_key_remove(all=True, apply_mix=True)
                applied_count += 1
                LOG.info(f"   ✅ {obj_name}: 形态键应用成功")
            except Exception as e:
                LOG.warning(f"   应用形态键失败 {obj_name}: {e}")
                failed_count += 1
            
            if original_active:
                bpy.context.view_layer.objects.active = original_active
        
        LOG.info(f"   ✅ 应用形态键: {applied_count} 个物体, {no_shapekey_count} 个无形态键")

    @classmethod
    def _rename_uv_layers(cls, object_names: List[str]):
        renamed_count = 0
        skipped_count = 0

        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj or obj.type != 'MESH':
                continue

            uv_layers = obj.data.uv_layers
            if not uv_layers:
                continue

            active_uv = uv_layers.active
            if not active_uv:
                continue

            if active_uv.name == "TEXCOORD.xy":
                skipped_count += 1
                continue

            if "TEXCOORD.xy" in uv_layers:
                uv_layers["TEXCOORD.xy"].name = "_TEXCOORD_xy_conflict"
                LOG.warning(f"   {obj_name}: 名称冲突，已有TEXCOORD.xy UV层，将其临时重命名")

            old_name = active_uv.name
            active_uv.name = "TEXCOORD.xy"
            renamed_count += 1
            LOG.info(f"   {obj_name}: 活动UV '{old_name}' -> 'TEXCOORD.xy'")

        if renamed_count > 0:
            LOG.info(f"   ✅ UV重命名: {renamed_count} 个物体, {skipped_count} 个无需修改")
        elif skipped_count > 0:
            LOG.info(f"   ✅ UV重命名: 所有 {skipped_count} 个物体活动UV已为TEXCOORD.xy，无需修改")

    @classmethod
    def _restore_non_mirror_objects(cls, object_names: List[str]):
        objects = []
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if obj:
                objects.append(obj)
        NonMirrorWorkflowHelper.restore_export_objects(objects)

    @classmethod
    def update_blueprint_node_references(cls, tree, nested_trees: List = None):
        if not cls.original_to_copy_map:
            return
        
        trees_to_update = [tree]
        if nested_trees:
            trees_to_update.extend(nested_trees)
        
        updated_count = 0
        multi_file_updated_count = 0
        multi_ref_objects = {}
        
        for current_tree in trees_to_update:
            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_Object_Info' and not node.mute:
                    original_name = ObjectPrefixHelper.build_virtual_object_name_for_node(node)
                    if original_name.endswith('_copy'):
                        continue
                    if original_name in cls.original_to_copy_map:
                        copy_name = cls.original_to_copy_map[original_name]
                        cls.modified_nodes.append((current_tree.name, node.name, getattr(node, 'object_name', ''), 'object_info'))
                        node.object_name = copy_name
                        updated_count += 1
                        
                        if original_name not in multi_ref_objects:
                            multi_ref_objects[original_name] = []
                        multi_ref_objects[original_name].append(f"Object_Info:{node.name}")
                
                elif node.bl_idname == 'SSMTNode_MultiFile_Export' and not node.mute:
                    object_list = getattr(node, 'object_list', [])
                    for item in object_list:
                        original_name = getattr(item, 'object_name', '')
                        if not original_name:
                            continue
                        if original_name.endswith('_copy'):
                            continue
                        if original_name in cls.original_to_copy_map:
                            copy_name = cls.original_to_copy_map[original_name]
                            cls.modified_nodes.append((current_tree.name, node.name, original_name, 'multi_file_export'))
                            item.object_name = copy_name
                            multi_file_updated_count += 1
                            
                            if original_name not in multi_ref_objects:
                                multi_ref_objects[original_name] = []
                            multi_ref_objects[original_name].append(f"MultiFile_Export:{node.name}")
        
        multi_ref_count = sum(1 for k, v in multi_ref_objects.items() if len(v) > 1)
        
        if multi_file_updated_count > 0:
            LOG.info(f"📋 更新节点引用: Object_Info {updated_count} 个, MultiFile_Export {multi_file_updated_count} 个")
        elif multi_ref_count > 0:
            LOG.info(f"📋 更新节点引用: {updated_count} 个节点 ({multi_ref_count} 个物体被多节点引用)")
        else:
            LOG.info(f"📋 更新节点引用: {updated_count} 个节点")

    @classmethod
    def restore_blueprint_node_references(cls):
        if not cls.modified_nodes:
            return
        
        restored_count = 0
        for tree_name, node_name, original_name, node_type in cls.modified_nodes:
            tree = bpy.data.node_groups.get(tree_name)
            if not tree:
                continue
            
            node = tree.nodes.get(node_name)
            if not node:
                continue
            
            if node_type == 'object_info':
                current_name = getattr(node, 'object_name', '')
                if current_name.endswith('_copy'):
                    node.object_name = original_name
                    restored_count += 1
            
            elif node_type == 'multi_file_export':
                object_list = getattr(node, 'object_list', [])
                for item in object_list:
                    current_name = getattr(item, 'object_name', '')
                    if current_name == f"{original_name}_copy":
                        item.object_name = original_name
                        restored_count += 1
        
        cls.modified_nodes.clear()
        
        if restored_count > 0:
            LOG.info(f"   ✅ 已恢复 {restored_count} 个节点引用")

    @classmethod
    def cleanup_copies(cls, silent=False):
        if not silent:
            LOG.info(f"🧹 开始清理物体副本")
        
        copies_to_remove = []
        for obj in bpy.data.objects:
            obj_name = obj.name
            if obj_name.endswith('_copy'):
                copies_to_remove.append(obj)
            elif '_chain' in obj_name:
                if re.search(r'_chain\d+$', obj_name) or re.search(r'_chain\d+_copy$', obj_name):
                    copies_to_remove.append(obj)
            elif '_dup' in obj_name:
                if re.search(r'_dup\d+$', obj_name) or re.search(r'_dup\d+_copy$', obj_name):
                    copies_to_remove.append(obj)
        
        if not copies_to_remove:
            cls.restore_blueprint_node_references()
            if not silent:
                LOG.info(f"   未找到副本物体")
            return
        
        if not silent:
            LOG.info(f"   找到 {len(copies_to_remove)} 个副本物体")
        
        cleaned_count = 0
        for obj in copies_to_remove:
            try:
                obj_name = obj.name
                
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                    cleaned_count += 1
                except ReferenceError:
                    pass
                    
            except ReferenceError:
                pass
        
        cls.created_copies.clear()
        cls.original_to_copy_map.clear()
        if not silent:
            LOG.info(f"   ✅ 清理完成，删除 {cleaned_count} 个副本")
        
        cls.restore_blueprint_node_references()
        
        cls._cleanup_orphan_data(silent)

    @classmethod
    def _cleanup_orphan_data(cls, silent=False):
        if not silent:
            LOG.info(f"   🧹 清理孤立数据...")
        
        orphan_meshes = [mesh for mesh in bpy.data.meshes if mesh.users == 0]
        orphan_materials = [mat for mat in bpy.data.materials if mat.users == 0]
        orphan_textures = [tex for tex in bpy.data.textures if tex.users == 0]
        orphan_images = [img for img in bpy.data.images if img.users == 0]
        
        mesh_count = len(orphan_meshes)
        mat_count = len(orphan_materials)
        tex_count = len(orphan_textures)
        img_count = len(orphan_images)
        
        for mesh in orphan_meshes:
            bpy.data.meshes.remove(mesh)
        for mat in orphan_materials:
            bpy.data.materials.remove(mat)
        for tex in orphan_textures:
            bpy.data.textures.remove(tex)
        for img in orphan_images:
            bpy.data.images.remove(img)
        
        if not silent:
            if mesh_count > 0 or mat_count > 0 or tex_count > 0 or img_count > 0:
                LOG.info(f"   ✅ 清理孤立数据: {mesh_count} 网格, {mat_count} 材质, {tex_count} 纹理, {img_count} 图像")
            else:
                LOG.info(f"   ✅ 无孤立数据需要清理")

    @classmethod
    def get_copy_name(cls, original_name: str) -> str:
        return cls.original_to_copy_map.get(original_name, original_name)

    @classmethod
    def has_copies(cls) -> bool:
        return len(cls.created_copies) > 0

    @classmethod
    def validate_copy_suffix(cls, object_name: str) -> bool:
        if not cls.has_copies():
            return True

        if object_name.endswith('_copy'):
            return True

        if re.search(r'_chain\d+_copy$', object_name) or re.search(r'_chain\d+$', object_name):
            return True

        if re.search(r'_dup\d+_copy$', object_name) or re.search(r'_dup\d+$', object_name):
            return True

        return False


def register():
    pass


def unregister():
    pass
