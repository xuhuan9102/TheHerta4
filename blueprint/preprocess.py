import bpy
from typing import List, Dict

from ..utils.log_utils import LOG
from .export_helper import BlueprintExportHelper


class PreProcessHelper:
    original_to_copy_map: Dict[str, str] = {}
    created_copies: List[str] = []
    modified_nodes: List[tuple] = []

    @classmethod
    def execute_preprocess(cls, object_names: List[str]) -> Dict[str, str]:
        cls.original_to_copy_map.clear()
        cls.created_copies.clear()
        cls.modified_nodes.clear()
        
        unique_objects = list(set(object_names))
        
        multi_file_objects = BlueprintExportHelper.get_all_objects_from_multi_file_nodes()
        for obj_name in multi_file_objects:
            if obj_name not in unique_objects:
                unique_objects.append(obj_name)
        
        if multi_file_objects:
            LOG.info(f"📋 多文件导出节点物体: {len(multi_file_objects)} 个")
        
        cls._create_object_copies(unique_objects)
        
        LOG.info(f"🔧 前处理完成: {len(unique_objects)} 个物体")
        
        return cls.original_to_copy_map

    @classmethod
    def _create_object_copies(cls, object_names: List[str]):
        created_count = 0
        existing_count = 0
        failed_count = 0
        
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                failed_count += 1
                continue
            
            copy_name = f"{obj_name}_copy"
            
            existing_copy = bpy.data.objects.get(copy_name)
            if existing_copy:
                existing_count += 1
                cls.original_to_copy_map[obj_name] = copy_name
                continue
            
            obj_copy = obj.copy()
            obj_copy.name = copy_name
            
            if obj.data:
                obj_copy.data = obj.data.copy()
            
            bpy.context.collection.objects.link(obj_copy)
            
            cls.original_to_copy_map[obj_name] = copy_name
            cls.created_copies.append(copy_name)
            created_count += 1
        
        LOG.info(f"📋 创建副本: 成功 {created_count} 个, 已存在 {existing_count} 个, 失败 {failed_count} 个")

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
                    original_name = getattr(node, 'object_name', '')
                    if original_name.endswith('_copy'):
                        continue
                    if original_name in cls.original_to_copy_map:
                        copy_name = cls.original_to_copy_map[original_name]
                        cls.modified_nodes.append((current_tree.name, node.name, original_name, 'object_info'))
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
    def cleanup_copies(cls):
        LOG.info(f"🧹 开始清理物体副本")
        
        copies_to_remove = []
        for obj in bpy.data.objects:
            if obj.name.endswith('_copy'):
                copies_to_remove.append(obj)
        
        if not copies_to_remove:
            cls.restore_blueprint_node_references()
            return
        
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
        LOG.info(f"   ✅ 清理完成，删除 {cleaned_count} 个副本")
        
        cls.restore_blueprint_node_references()

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
        
        return False


def register():
    pass


def unregister():
    pass
