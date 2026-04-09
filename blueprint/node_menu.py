import bpy
from bpy.types import NodeTree, Node, NodeSocket

from ..common.global_config import GlobalConfig

from .node_base import SSMTBlueprintTree, SSMTNodeBase

# 检查物体切换节点是否可用
try:
    from .node_swap import SSMTNode_ObjectSwap
    HAS_OBJECT_SWAP = True
except ImportError:
    HAS_OBJECT_SWAP = False

# 检查重命名节点是否可用
try:
    from .node_rename import SSMTNode_Object_Rename
    HAS_OBJECT_RENAME = True
except ImportError:
    HAS_OBJECT_RENAME = False


class SSMT_OT_CreateGroupFromSelection(bpy.types.Operator):
    '''Create nodes from selected objects and group them under a new Group node'''
    bl_idname = "ssmt.create_group_from_selection"
    bl_label = "将所选物体新建到组节点"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objects = context.selected_objects
        if not selected_objects:
            self.report({'WARNING'}, "没有选择任何物体")
            return {'CANCELLED'}

        node_tree = None
        
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        
        if not node_tree:
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'NODE_EDITOR':
                        for space in area.spaces:
                            if space.type == 'NODE_EDITOR':
                                tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
                                if tree and tree.bl_idname == 'SSMTBlueprintTreeType':
                                    node_tree = tree
                                    break
                        if node_tree:
                            break
                if node_tree:
                    break
        
        if not node_tree:
            GlobalConfig.read_from_main_json_ssmt4()
            workspace_name = f"{GlobalConfig.workspacename}" if GlobalConfig.workspacename else "SSMT_Mod_Logic"
            node_tree = bpy.data.node_groups.get(workspace_name)
        
        if not node_tree or node_tree.bl_idname != 'SSMTBlueprintTreeType':
            self.report({'WARNING'}, "未找到有效的蓝图树，请先打开蓝图编辑器")
            return {'CANCELLED'}

        base_x = 0
        base_y = 0
        if node_tree.nodes:
             pass

        for node in node_tree.nodes:
            node.select = False

        group_node = node_tree.nodes.new(type='SSMTNode_Object_Group')
        group_node.location = (base_x + 400, base_y)
        group_node.select = True
        
        for i, obj in enumerate(selected_objects):
            obj_node = node_tree.nodes.new(type='SSMTNode_Object_Info')
            obj_node.location = (base_x, base_y - i * 150)
            obj_node.select = True
            
            obj_node.object_name = obj.name
            
            target_socket = None
            if len(group_node.inputs) > 0:
                 target_socket = group_node.inputs[-1]
            
            if target_socket:
                node_tree.links.new(obj_node.outputs[0], target_socket)
                group_node.update()

        return {'FINISHED'}


class SSMT_OT_CreateInternalSwitch(bpy.types.Operator):
    '''Create Object Info nodes from selected objects and connect them to a Group node'''
    bl_idname = "ssmt.create_internal_switch"
    bl_label = "创建内部切换"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objects = context.selected_objects
        if not selected_objects:
            self.report({'WARNING'}, "没有选择任何物体")
            return {'CANCELLED'}
        
        node_tree = None
        
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        
        if not node_tree:
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'NODE_EDITOR':
                        for space in area.spaces:
                            if space.type == 'NODE_EDITOR':
                                tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
                                if tree and tree.bl_idname == 'SSMTBlueprintTreeType':
                                    node_tree = tree
                                    break
                        if node_tree:
                            break
                if node_tree:
                    break
        
        if not node_tree:
            GlobalConfig.read_from_main_json_ssmt4()
            workspace_name = f"{GlobalConfig.workspacename}" if GlobalConfig.workspacename else "SSMT_Mod_Logic"
            node_tree = bpy.data.node_groups.get(workspace_name)
        
        if not node_tree or node_tree.bl_idname != 'SSMTBlueprintTreeType':
            self.report({'WARNING'}, "未找到有效的蓝图树，请先打开蓝图编辑器")
            return {'CANCELLED'}
        
        nodes = node_tree.nodes
        links = node_tree.links
        
        base_x = 0
        base_y = 0
        if nodes:
            max_x = max([node.location.x + node.width for node in nodes])
            base_x = max_x + 200
        
        for node in nodes:
            node.select = False
        
        group_node = nodes.new(type='SSMTNode_Object_Group')
        group_node.location = (base_x + 600, base_y)
        
        obj_nodes = []
        for i, obj in enumerate(selected_objects):
            obj_node = nodes.new(type='SSMTNode_Object_Info')
            obj_node.location = (base_x, base_y - i * 150)
            obj_node.object_name = obj.name
            obj_node.select = True
            obj_nodes.append(obj_node)
            
            if i < len(group_node.inputs):
                links.new(obj_node.outputs[0], group_node.inputs[i])
        
        group_node.select = True
        
        self.report({'INFO'}, f"已创建 {len(obj_nodes)} 个物体节点并连接到组节点")
        return {'FINISHED'}


def draw_objects_context_menu_add(self, context):
    layout = self.layout
    layout.separator()
    layout.menu("SSMT_MT_ObjectContextMenuSub", text="SSMT蓝图架构", icon='NODETREE')

class SSMT_MT_ObjectContextMenuSub(bpy.types.Menu):
    bl_label = "SSMT蓝图架构"
    
    def draw(self, context):
        layout = self.layout
        layout.operator("ssmt.create_group_from_selection", text="将所选物体新建到组节点", icon='GROUP')
        layout.operator("ssmt.create_internal_switch", text="创建内部切换", icon='ARROW_LEFTRIGHT')


class SSMT_MT_NodeMenu_Branch(bpy.types.Menu):
    bl_label = "分支"
    
    def draw(self, context):
        layout = self.layout
        layout.operator("node.add_node", text="Object Info", icon='OBJECT_DATAMODE').type = "SSMTNode_Object_Info"
        layout.operator("node.add_node", text="Group", icon='GROUP').type = "SSMTNode_Object_Group"
        
        # 物体切换节点 - 仅在模块可用时显示
        if HAS_OBJECT_SWAP:
            layout.separator()
            layout.operator("node.add_node", text="Object Swap", icon='SHADERFX').type = "SSMTNode_ObjectSwap"
        
        # 重命名节点 - 仅在模块可用时显示
        if HAS_OBJECT_RENAME:
            layout.separator()
            layout.operator("node.add_node", text="Rename Object", icon='OUTLINER').type = "SSMTNode_Object_Rename"
        
        layout.separator()
        layout.operator("node.add_node", text="Mod Output", icon='EXPORT').type = "SSMTNode_Result_Output"

class SSMT_MT_NodeMenu_ShapeKey(bpy.types.Menu):
    bl_label = "形态键"
    
    def draw(self, context):
        layout = self.layout
        layout.operator("node.add_node", text="Shape Key", icon='SHAPEKEY_DATA').type = "SSMTNode_ShapeKey"
        layout.operator("node.add_node", text="Generate ShapeKey Buffer", icon='EXPORT').type = "SSMTNode_ShapeKey_Output"


class SSMT_OT_AlignNodes(bpy.types.Operator):
    '''将选中的节点按照矩阵对齐'''
    bl_idname = "ssmt.align_nodes"
    bl_label = "矩阵对齐节点"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            self.report({'ERROR'}, "请在节点编辑器中使用此功能")
            return {'CANCELLED'}

        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not node_tree:
            self.report({'ERROR'}, "未找到节点树")
            return {'CANCELLED'}

        selected_nodes = [node for node in node_tree.nodes if node.select]
        if len(selected_nodes) < 2:
            self.report({'WARNING'}, "请至少选择2个节点")
            return {'CANCELLED'}

        columns = self.group_nodes_by_columns(selected_nodes)
        
        for column in columns:
            self.align_column_vertically(column)
        
        self.align_columns_horizontally(columns)
        
        self.adjust_node_order_by_connections(selected_nodes, node_tree)
        
        self.report({'INFO'}, f"已将 {len(selected_nodes)} 个节点结构化对齐，分为 {len(columns)} 列")
        return {'FINISHED'}
    
    def group_nodes_by_columns(self, nodes):
        if not nodes:
            return []
        
        avg_width = sum(node.width for node in nodes) / len(nodes)
        column_threshold = avg_width * 1.1
        
        sorted_nodes = sorted(nodes, key=lambda n: n.location.x)
        
        columns = []
        current_column = [sorted_nodes[0]]
        current_x = sorted_nodes[0].location.x
        
        for node in sorted_nodes[1:]:
            if abs(node.location.x - current_x) > column_threshold:
                columns.append(current_column)
                current_column = [node]
                current_x = node.location.x
            else:
                current_column.append(node)
        
        if current_column:
            columns.append(current_column)
        
        return columns
    
    def align_column_vertically(self, column):
        if len(column) <= 1:
            return
        
        column.sort(key=lambda n: -n.location.y)
        
        start_x = column[0].location.x
        start_y = column[0].location.y
        
        vertical_spacing = 80.0
        
        current_y = start_y
        for node in column:
            node.location = (start_x, current_y)
            current_y -= (node.height + vertical_spacing)
    
    def align_columns_horizontally(self, columns):
        if len(columns) <= 1:
            return
        
        all_nodes = [node for column in columns for node in column]
        avg_width = sum(node.width for node in all_nodes) / len(all_nodes)
        column_spacing = avg_width * 0.3
        
        column_bounds = []
        for i, column in enumerate(columns):
            if not column:
                continue
            
            x_min = min(node.location.x for node in column)
            x_max = max(node.location.x + node.width for node in column)
            
            center_x = (x_min + x_max) / 2
            
            width = x_max - x_min + 10
            
            column_bounds.append({
                'index': i,
                'column': column,
                'center_x': center_x,
                'width': width,
                'x_min': x_min,
                'x_max': x_max
            })
        
        column_bounds.sort(key=lambda b: b['center_x'])
        
        current_x = column_bounds[0]['x_min']
        for bound in column_bounds:
            offset_x = current_x - bound['x_min']
            for node in bound['column']:
                node.location.x += offset_x
            
            current_x += bound['width'] + column_spacing
    
    def adjust_node_order_by_connections(self, nodes, node_tree):
        if len(nodes) < 2:
            return
        
        connection_graph = {}
        for node in nodes:
            connection_graph[node] = {'inputs': [], 'outputs': []}
        
        for link in node_tree.links:
            from_node = link.from_node
            to_node = link.to_node
            
            if from_node in connection_graph and to_node in connection_graph:
                connection_graph[from_node]['outputs'].append(to_node)
                connection_graph[to_node]['inputs'].append(from_node)
        
        for column in self.group_nodes_by_columns(nodes):
            if len(column) <= 1:
                continue
            
            column.sort(key=lambda n: (
                len(connection_graph[n]['inputs']),
                -n.location.y
            ))


class SSMT_OT_BatchConnectNodes(bpy.types.Operator):
    '''批量连接选中的节点：支持一对一或多对一连接'''
    bl_idname = "ssmt.batch_connect_nodes"
    bl_label = "批量连接节点"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            self.report({'ERROR'}, "请在节点编辑器中使用此功能")
            return {'CANCELLED'}

        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not node_tree:
            self.report({'ERROR'}, "未找到节点树")
            return {'CANCELLED'}

        selected_nodes = [node for node in node_tree.nodes if node.select]
        if len(selected_nodes) < 2:
            self.report({'WARNING'}, "请至少选择2个节点")
            return {'CANCELLED'}

        type_count_dict = {}
        for node in selected_nodes:
            node_type = node.bl_idname
            type_count_dict[node_type] = type_count_dict.get(node_type, 0) + 1

        if len(type_count_dict) > 2:
            self.report({'ERROR'}, f"所选节点类型过多（{len(type_count_dict)}种），请选择1-2种类型的节点")
            return {'CANCELLED'}

        if len(type_count_dict) == 1:
            self.report({'ERROR'}, "所选节点类型相同，无法连接")
            return {'CANCELLED'}
        else:
            type_items = list(type_count_dict.items())
            type_items.sort(key=lambda x: x[1], reverse=True)
            
            majority_type, majority_count = type_items[0]
            minority_type, minority_count = type_items[1]

            if majority_count == minority_count:
                return self.connect_one_to_one(selected_nodes, type_count_dict, node_tree)
            else:
                return self.connect_many_to_one(selected_nodes, type_count_dict, node_tree)

    def connect_one_to_one(self, selected_nodes, type_count_dict, node_tree):
        type_items = list(type_count_dict.items())
        type_a, count_a = type_items[0]
        type_b, count_b = type_items[1]

        nodes_a = [node for node in selected_nodes if node.bl_idname == type_a]
        nodes_b = [node for node in selected_nodes if node.bl_idname == type_b]

        a_has_output = len(nodes_a[0].outputs) > 0
        a_has_input = len(nodes_a[0].inputs) > 0
        b_has_output = len(nodes_b[0].outputs) > 0
        b_has_input = len(nodes_b[0].inputs) > 0

        if a_has_output and not a_has_input and b_has_input:
            source_nodes, target_nodes = nodes_a, nodes_b
        elif b_has_output and not b_has_input and a_has_input:
            source_nodes, target_nodes = nodes_b, nodes_a
        elif a_has_output and a_has_input and b_has_output and b_has_input:
            avg_x_a = sum(node.location.x for node in nodes_a) / len(nodes_a)
            avg_x_b = sum(node.location.x for node in nodes_b) / len(nodes_b)
            if avg_x_a < avg_x_b:
                source_nodes, target_nodes = nodes_a, nodes_b
            else:
                source_nodes, target_nodes = nodes_b, nodes_a
        elif a_has_output and not a_has_input and not b_has_output:
            source_nodes, target_nodes = nodes_a, nodes_b
        elif b_has_output and not b_has_input and not a_has_output:
            source_nodes, target_nodes = nodes_b, nodes_a
        elif a_has_output and not b_has_input:
            source_nodes, target_nodes = nodes_a, nodes_b
        elif b_has_output and not a_has_input:
            source_nodes, target_nodes = nodes_b, nodes_a
        else:
            self.report({'ERROR'}, "无法确定连接方向，请检查节点端口配置")
            return {'CANCELLED'}

        for node in target_nodes:
            if len(node.inputs) == 0:
                self.report({'ERROR'}, f"节点 '{node.name}' 没有输入端口")
                return {'CANCELLED'}

        for source_node in source_nodes:
            for output in source_node.outputs:
                for link in output.links:
                    if link.to_node in target_nodes:
                        node_tree.links.remove(link)

        source_nodes.sort(key=lambda n: (n.location.x, -n.location.y))
        target_nodes.sort(key=lambda n: (n.location.x, -n.location.y))

        connection_info = []
        for i in range(min(len(source_nodes), len(target_nodes))):
            source_node = source_nodes[i]
            target_node = target_nodes[i]

            available_input = None
            for input_socket in target_node.inputs:
                if not input_socket.is_linked:
                    available_input = input_socket
                    break

            if not available_input:
                try:
                    if hasattr(target_node, 'update'):
                        target_node.inputs.new('SSMTSocketObject', f"Input {len(target_node.inputs) + 1}")
                        available_input = target_node.inputs[-1]
                except:
                    self.report({'WARNING'}, f"节点 '{target_node.name}' 没有可用的输入端口")
                    continue

            if available_input and len(source_node.outputs) > 0:
                node_tree.links.new(source_node.outputs[0], available_input)
                connection_info.append(f"{source_node.name} -> {target_node.name}")

        for node in target_nodes:
            if hasattr(node, 'update'):
                node.update()

        total_connections = len(connection_info)
        self.report({'INFO'}, f"一对一连接：成功连接 {total_connections} 个节点对")
        print(f"一对一连接完成，共创建 {total_connections} 个连接:")
        for info in connection_info:
            print(f"  {info}")

        return {'FINISHED'}

    def connect_many_to_one(self, selected_nodes, type_count_dict, node_tree):
        type_items = list(type_count_dict.items())
        type_items.sort(key=lambda x: x[1], reverse=True)
        
        majority_type, majority_count = type_items[0]
        minority_type, minority_count = type_items[1]

        majority_nodes = [node for node in selected_nodes if node.bl_idname == majority_type]
        minority_nodes = [node for node in selected_nodes if node.bl_idname == minority_type]

        for node in majority_nodes:
            if len(node.outputs) == 0:
                self.report({'ERROR'}, f"多数节点 '{node.name}' 没有输出端口")
                return {'CANCELLED'}

        for node in minority_nodes:
            if len(node.inputs) == 0:
                self.report({'ERROR'}, f"少数节点 '{node.name}' 没有输入端口")
                return {'CANCELLED'}

        for node in majority_nodes:
            for output in node.outputs:
                for link in output.links:
                    if link.to_node in minority_nodes:
                        node_tree.links.remove(link)

        majority_nodes.sort(key=lambda n: -n.location.y)
        minority_nodes.sort(key=lambda n: -n.location.y)

        nodes_per_target = majority_count // minority_count
        remainder = majority_count % minority_count

        connection_info = []
        majority_index = 0

        for minority_index, minority_node in enumerate(minority_nodes):
            current_batch_size = nodes_per_target + (1 if minority_index < remainder else 0)

            for i in range(current_batch_size):
                if majority_index >= len(majority_nodes):
                    break

                majority_node = majority_nodes[majority_index]

                available_input = None
                for input_socket in minority_node.inputs:
                    if not input_socket.is_linked:
                        available_input = input_socket
                        break

                if not available_input:
                    try:
                        if hasattr(minority_node, 'update'):
                            minority_node.inputs.new('SSMTSocketObject', f"Input {len(minority_node.inputs) + 1}")
                            available_input = minority_node.inputs[-1]
                    except:
                        self.report({'WARNING'}, f"节点 '{minority_node.name}' 没有可用的输入端口")
                        majority_index += 1
                        continue

                if available_input and len(majority_node.outputs) > 0:
                    node_tree.links.new(majority_node.outputs[0], available_input)
                    connection_info.append(f"{majority_node.name} -> {minority_node.name}")

                majority_index += 1

        for node in minority_nodes:
            if hasattr(node, 'update'):
                node.update()

        total_connections = len(connection_info)
        self.report({'INFO'}, f"多对一连接：成功连接 {total_connections} 个节点对")
        print(f"多对一连接完成，共创建 {total_connections} 个连接:")
        for info in connection_info:
            print(f"  {info}")

        return {'FINISHED'}



def draw_node_add_menu(self, context):
    if not isinstance(context.space_data, bpy.types.SpaceNodeEditor):
        return
    if context.space_data.tree_type != 'SSMTBlueprintTreeType':
        return
    
    layout = self.layout
    layout.menu("SSMT_MT_NodeMenu_Branch", text="分支", icon='RNA')
    layout.menu("SSMT_MT_NodeMenu_ShapeKey", text="形态键", icon='SHAPEKEY_DATA')
    layout.separator()

    layout.operator("node.add_node", text="Frame", icon='FILE_PARENT').type = "NodeFrame"
    layout.separator()



def draw_node_context_menu(self, context):
    if not isinstance(context.space_data, bpy.types.SpaceNodeEditor):
        return
    if context.space_data.tree_type != 'SSMTBlueprintTreeType':
        return
    
    layout = self.layout
    layout.separator()
    layout.operator("ssmt.align_nodes", text="矩阵对齐节点", icon='GRID')
    layout.operator("ssmt.batch_connect_nodes", text="批量连接节点", icon='LINKED')
    layout.separator()
    layout.operator("ssmt.update_all_node_references", text="更新所有节点引用", icon='FILE_REFRESH')


def register():
    bpy.utils.register_class(SSMT_OT_CreateGroupFromSelection)
    bpy.utils.register_class(SSMT_OT_CreateInternalSwitch)
    bpy.utils.register_class(SSMT_OT_AlignNodes)
    bpy.utils.register_class(SSMT_OT_BatchConnectNodes)
    bpy.utils.register_class(SSMT_MT_ObjectContextMenuSub)
    bpy.utils.register_class(SSMT_MT_NodeMenu_Branch)
    bpy.utils.register_class(SSMT_MT_NodeMenu_ShapeKey)

    bpy.types.NODE_MT_add.prepend(draw_node_add_menu)
    bpy.types.VIEW3D_MT_object_context_menu.append(draw_objects_context_menu_add)
    bpy.types.NODE_MT_context_menu.append(draw_node_context_menu)

def unregister():
    bpy.types.NODE_MT_context_menu.remove(draw_node_context_menu)
    bpy.types.NODE_MT_add.remove(draw_node_add_menu)
    bpy.types.VIEW3D_MT_object_context_menu.remove(draw_objects_context_menu_add)

    bpy.utils.unregister_class(SSMT_MT_NodeMenu_ShapeKey)
    bpy.utils.unregister_class(SSMT_MT_NodeMenu_Branch)
    bpy.utils.unregister_class(SSMT_MT_ObjectContextMenuSub)
    bpy.utils.unregister_class(SSMT_OT_BatchConnectNodes)
    bpy.utils.unregister_class(SSMT_OT_AlignNodes)
    bpy.utils.unregister_class(SSMT_OT_CreateInternalSwitch)
    bpy.utils.unregister_class(SSMT_OT_CreateGroupFromSelection)
