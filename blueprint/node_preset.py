import bpy
import os
import json
from bpy.types import Operator, Menu
from bpy.props import StringProperty
import mathutils

from .node_base import SSMTNodeBase


def _make_json_serializable(value):
    if value is None:
        return None
    elif isinstance(value, (str, int, float, bool)):
        return value
    elif isinstance(value, (mathutils.Vector, mathutils.Color)):
        return tuple(value)
    elif isinstance(value, mathutils.Matrix):
        return [tuple(row) for row in value]
    elif isinstance(value, (list, tuple)):
        return [_make_json_serializable(item) for item in value]
    elif isinstance(value, dict):
        return {k: _make_json_serializable(v) for k, v in value.items()}
    elif hasattr(value, '__iter__'):
        try:
            return [_make_json_serializable(item) for item in value]
        except:
            return str(value)
    else:
        return str(value)


class NodePresetManager:
    PRESET_FILE_NAME = "node_presets.json"
    
    @classmethod
    def get_preset_file_path(cls):
        from ..common.global_config import GlobalConfig
        config_folder = GlobalConfig.path_ssmt4_global_configs_folder()
        if not os.path.exists(config_folder):
            os.makedirs(config_folder)
        return os.path.join(config_folder, cls.PRESET_FILE_NAME)
    
    @classmethod
    def load_presets(cls):
        preset_path = cls.get_preset_file_path()
        if os.path.exists(preset_path):
            try:
                with open(preset_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"加载预设文件失败: {e}")
                return {}
        return {}
    
    @classmethod
    def save_presets(cls, presets):
        preset_path = cls.get_preset_file_path()
        try:
            with open(preset_path, 'w', encoding='utf-8') as f:
                json.dump(presets, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存预设文件失败: {e}")
            return False
    
    @classmethod
    def add_preset(cls, name, preset_data):
        presets = cls.load_presets()
        presets[name] = preset_data
        return cls.save_presets(presets)
    
    @classmethod
    def remove_preset(cls, name):
        presets = cls.load_presets()
        if name in presets:
            del presets[name]
            return cls.save_presets(presets)
        return False
    
    @classmethod
    def get_preset(cls, name):
        presets = cls.load_presets()
        return presets.get(name)


class SSMT_OT_SaveNodePreset(Operator):
    bl_idname = "ssmt.save_node_preset"
    bl_label = "保存节点预设"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: StringProperty(
        name="预设名称",
        description="输入预设的名称",
        default=""
    )
    
    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            return False
        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not node_tree or node_tree.bl_idname != 'SSMTBlueprintTreeType':
            return False
        selected_nodes = [node for node in node_tree.nodes if node.select]
        return len(selected_nodes) > 0
    
    def execute(self, context):
        if not self.preset_name or not self.preset_name.strip():
            self.report({'WARNING'}, "请输入有效的预设名称")
            return {'CANCELLED'}
        
        space_data = context.space_data
        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        
        selected_nodes = [node for node in node_tree.nodes if node.select]
        if not selected_nodes:
            self.report({'WARNING'}, "没有选中的节点")
            return {'CANCELLED'}
        
        preset_data = self._extract_preset_data(selected_nodes, node_tree)
        
        if NodePresetManager.add_preset(self.preset_name.strip(), preset_data):
            self.report({'INFO'}, f"预设 '{self.preset_name}' 保存成功")
        else:
            self.report({'ERROR'}, "保存预设失败")
            return {'CANCELLED'}
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "preset_name")
    
    def _extract_preset_data(self, nodes, node_tree):
        min_x = min(node.location.x for node in nodes)
        min_y = min(node.location.y for node in nodes)
        
        node_data_list = []
        node_name_mapping = {}
        
        for i, node in enumerate(nodes):
            input_count = len(node.inputs)
            output_count = len(node.outputs)
            
            node_data = {
                'index': i,
                'bl_idname': node.bl_idname,
                'relative_location': [
                    node.location.x - min_x,
                    node.location.y - min_y
                ],
                'width': node.width,
                'label': node.label if node.label else "",
                'input_count': input_count,
                'output_count': output_count,
                'properties': self._extract_node_properties(node)
            }
            node_data_list.append(node_data)
            node_name_mapping[node.name] = i
        
        connections = []
        for link in node_tree.links:
            if link.from_node.name in node_name_mapping and link.to_node.name in node_name_mapping:
                from_index = node_name_mapping[link.from_node.name]
                to_index = node_name_mapping[link.to_node.name]
                
                from_socket_index = self._get_socket_index(link.from_node.outputs, link.from_socket)
                to_socket_index = self._get_socket_index(link.to_node.inputs, link.to_socket)
                
                if from_socket_index is not None and to_socket_index is not None:
                    connections.append({
                        'from_node': from_index,
                        'from_socket': from_socket_index,
                        'to_node': to_index,
                        'to_socket': to_socket_index
                    })
        
        return {
            'nodes': node_data_list,
            'connections': connections,
            'version': 1
        }
    
    def _extract_node_properties(self, node):
        properties = {}
        
        property_blacklist = {
            'name', 'label', 'location', 'width', 'height', 'dimensions',
            'select', 'show_options', 'show_preview', 'hide', 'mute',
            'parent', 'use_custom_color', 'color', 'type'
        }
        
        for prop_name in node.bl_rna.properties.keys():
            if prop_name in property_blacklist:
                continue
            
            prop = node.bl_rna.properties[prop_name]
            if prop.is_readonly:
                continue
            
            if prop.type in {'STRING', 'INT', 'FLOAT', 'BOOLEAN'}:
                properties[prop_name] = _make_json_serializable(getattr(node, prop_name, None))
            elif prop.type == 'ENUM':
                properties[prop_name] = _make_json_serializable(getattr(node, prop_name, None))
            elif prop.type == 'COLLECTION':
                collection = getattr(node, prop_name, None)
                if collection:
                    collection_data = []
                    for item in collection:
                        item_data = {}
                        for item_prop_name in item.bl_rna.properties.keys():
                            item_prop = item.bl_rna.properties[item_prop_name]
                            if not item_prop.is_readonly and item_prop.type in {'STRING', 'INT', 'FLOAT', 'BOOLEAN', 'ENUM'}:
                                item_data[item_prop_name] = _make_json_serializable(getattr(item, item_prop_name, None))
                        if item_data:
                            collection_data.append(item_data)
                    if collection_data:
                        properties[prop_name] = collection_data
        
        return properties
    
    def _get_socket_index(self, sockets, target_socket):
        for i, socket in enumerate(sockets):
            if socket == target_socket:
                return i
        return None


class SSMT_OT_LoadNodePreset(Operator):
    bl_idname = "ssmt.load_node_preset"
    bl_label = "加载节点预设"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: StringProperty()
    mouse_x: bpy.props.IntProperty()
    mouse_y: bpy.props.IntProperty()
    
    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            return False
        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        return node_tree and node_tree.bl_idname == 'SSMTBlueprintTreeType'
    
    def invoke(self, context, event):
        self.mouse_x = event.mouse_region_x
        self.mouse_y = event.mouse_region_y
        return self.execute(context)
    
    def execute(self, context):
        preset_data = NodePresetManager.get_preset(self.preset_name)
        if not preset_data:
            self.report({'WARNING'}, f"预设 '{self.preset_name}' 不存在")
            return {'CANCELLED'}
        
        space_data = context.space_data
        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        
        for node in node_tree.nodes:
            node.select = False
        
        created_nodes = self._create_nodes(node_tree, preset_data, context)
        
        self._create_connections(node_tree, created_nodes, preset_data)
        
        for node in created_nodes.values():
            node.select = True
        
        self.report({'INFO'}, f"已加载预设 '{self.preset_name}'，创建了 {len(created_nodes)} 个节点")
        return {'FINISHED'}
    
    def _create_nodes(self, node_tree, preset_data, context):
        created_nodes = {}
        
        view2d = None
        try:
            for region in context.area.regions:
                if region.type == 'WINDOW':
                    view2d = region.view2d
                    break
        except:
            pass
        
        base_x = 0
        base_y = 0
        if view2d:
            try:
                base_x, base_y = view2d.region_to_view(self.mouse_x, self.mouse_y)
            except:
                pass
        
        for node_data in preset_data['nodes']:
            try:
                node = node_tree.nodes.new(type=node_data['bl_idname'])
                
                node.location = (
                    base_x + node_data['relative_location'][0],
                    base_y + node_data['relative_location'][1]
                )
                
                if node_data.get('width'):
                    node.width = node_data['width']
                
                if node_data.get('label'):
                    node.label = node_data['label']
                
                created_nodes[node_data['index']] = node
                
            except Exception as e:
                print(f"创建节点失败: {e}")
        
        return created_nodes
    
    def _create_connections(self, node_tree, created_nodes, preset_data):
        dynamic_classes = set()
        for node in created_nodes.values():
            if node.bl_idname in ('SSMTNode_Object_Group', 'SSMTNode_Result_Output'):
                cls = type(node)
                dynamic_classes.add(cls)
        
        saved_updates = {}
        for cls in dynamic_classes:
            if hasattr(cls, 'update'):
                saved_updates[cls] = cls.update
                cls.update = lambda self: None
        
        try:
            for node_data in preset_data['nodes']:
                node = created_nodes.get(node_data['index'])
                if not node:
                    continue
                
                required_inputs = node_data.get('input_count', 0)
                while len(node.inputs) < required_inputs:
                    try:
                        node.inputs.new('SSMTSocketObject', f"Input {len(node.inputs) + 1}")
                    except:
                        break
            
            sorted_connections = sorted(
                preset_data.get('connections', []),
                key=lambda c: (c['to_node'], c['to_socket'])
            )
            
            for conn in sorted_connections:
                from_node = created_nodes.get(conn['from_node'])
                to_node = created_nodes.get(conn['to_node'])
                
                if not from_node or not to_node:
                    continue
                
                from_socket_index = conn['from_socket']
                to_socket_index = conn['to_socket']
                
                if from_socket_index < len(from_node.outputs) and to_socket_index < len(to_node.inputs):
                    try:
                        node_tree.links.new(
                            from_node.outputs[from_socket_index],
                            to_node.inputs[to_socket_index]
                        )
                    except Exception as e:
                        print(f"创建连接失败: {e}")
        finally:
            for cls, original_update in saved_updates.items():
                cls.update = original_update
            
            for node in created_nodes.values():
                if node.bl_idname in ('SSMTNode_Object_Group', 'SSMTNode_Result_Output'):
                    try:
                        node.update()
                    except:
                        pass


class SSMT_OT_DeleteNodePreset(Operator):
    bl_idname = "ssmt.delete_node_preset"
    bl_label = "删除节点预设"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_name: StringProperty()
    
    def execute(self, context):
        if NodePresetManager.remove_preset(self.preset_name):
            self.report({'INFO'}, f"预设 '{self.preset_name}' 已删除")
        else:
            self.report({'WARNING'}, f"删除预设 '{self.preset_name}' 失败")
            return {'CANCELLED'}
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)


class SSMT_MT_NodePresetMenu(Menu):
    bl_label = "添加预设"
    bl_idname = "SSMT_MT_NodePresetMenu"
    
    def draw(self, context):
        layout = self.layout
        presets = NodePresetManager.load_presets()
        
        if not presets:
            layout.label(text="暂无保存的预设", icon='INFO')
            return
        
        for preset_name in sorted(presets.keys()):
            row = layout.row()
            row.operator("ssmt.load_node_preset", text=preset_name, icon='NODE').preset_name = preset_name
            row.operator("ssmt.delete_node_preset", text="", icon='TRASH').preset_name = preset_name


classes = (
    SSMT_OT_SaveNodePreset,
    SSMT_OT_LoadNodePreset,
    SSMT_OT_DeleteNodePreset,
    SSMT_MT_NodePresetMenu,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
