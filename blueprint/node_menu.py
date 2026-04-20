import re

import bpy
from bpy.types import NodeTree, Node, NodeSocket

from ..common.global_config import GlobalConfig
from ..common.object_prefix_helper import ObjectPrefixHelper

from .node_base import SSMTBlueprintTree, SSMTNodeBase


def _get_active_blueprint_tree(context):
    """获取当前活动的蓝图节点树
    
    使用多种检测方式确定用户当前正在操作的蓝图：
    1. 检查当前窗口中是否有节点编辑器区域（优先）
    2. 检查所有窗口中有激活区域的节点编辑器
    3. 检查任何窗口中正在编辑的节点树
    4. 回退到 GlobalConfig 配置的默认蓝图
    
    Returns:
        NodeTree or None: 当前活动的蓝图节点树
    """
    def is_valid_blueprint(tree):
        return tree and tree.bl_idname == 'SSMTBlueprintTreeType'
    
    def get_tree_from_space(space):
        if space.type != 'NODE_EDITOR':
            return None
        tree = getattr(space, "edit_tree", None)
        if is_valid_blueprint(tree):
            return tree
        tree = getattr(space, "node_tree", None)
        if is_valid_blueprint(tree):
            return tree
        return None
    
    current_window = getattr(context, 'window', None)
    if current_window:
        for area in current_window.screen.areas:
            if area.type == 'NODE_EDITOR':
                for space in area.spaces:
                    tree = get_tree_from_space(space)
                    if tree:
                        return tree
    
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                for space in area.spaces:
                    tree = get_tree_from_space(space)
                    if tree:
                        return tree
    
    GlobalConfig.read_from_main_json_ssmt4()
    workspace_name = f"{GlobalConfig.workspacename}" if GlobalConfig.workspacename else "SSMT_Mod_Logic"
    fallback_tree = bpy.data.node_groups.get(workspace_name)
    if is_valid_blueprint(fallback_tree):
        return fallback_tree
    
    return None


def _add_node_entry(layout, text, icon, node_type):
    try:
        layout.operator("node.add_node", text=text, icon=icon).type = node_type
    except Exception:
        pass


def _get_or_create_blueprint_tree(context):
    node_tree = _get_active_blueprint_tree(context)
    if node_tree:
        return node_tree

    GlobalConfig.read_from_main_json_ssmt4()
    tree_name = GlobalConfig.workspacename or "SSMT_Mod_Logic"
    tree = bpy.data.node_groups.get(tree_name)
    if tree and tree.bl_idname == 'SSMTBlueprintTreeType':
        return tree

    tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
    tree.use_fake_user = True
    return tree


def _get_new_node_location(node_tree, x_padding: float = 220.0, y_step: float = 180.0):
    if not node_tree.nodes:
        return (0.0, 0.0)

    max_x = max(node.location.x + getattr(node, "width", 200) for node in node_tree.nodes)
    min_y = min(node.location.y for node in node_tree.nodes)
    return (max_x + x_padding, min_y - y_step)


def _extract_target_hash_from_name(object_name: str) -> str:
    if not object_name:
        return ""

    match = re.match(r'^([a-f0-9]{8}-[a-f0-9]+(?:-[a-f0-9]+)?)', object_name)
    if match:
        return match.group(1)

    match = re.match(r'^([a-f0-9]{8})', object_name)
    if match:
        return match.group(1)

    return ""


def _append_rename_rule(node, search_str: str, replace_str: str) -> bool:
    for rule in getattr(node, 'rename_rules', []):
        if getattr(rule, 'search_str', '') == search_str and getattr(rule, 'replace_str', '') == replace_str:
            return False

    rule = node.rename_rules.add()
    rule.name = f"Rule_{len(node.rename_rules):03d}"
    rule.search_str = search_str
    rule.replace_str = replace_str
    node.active_rule_index = len(node.rename_rules) - 1
    return True


def _get_unique_collection_name(base_name: str) -> str:
    if base_name not in bpy.data.collections:
        return base_name

    suffix = 1
    while True:
        collection_name = f"{base_name}_{suffix:03d}"
        if collection_name not in bpy.data.collections:
            return collection_name
        suffix += 1


def _create_source_collection_for_vg_match(context, source_objects, target_obj):
    valid_sources = [obj for obj in source_objects if obj and obj.type == 'MESH' and obj.data]
    if len(valid_sources) == 1:
        return None, False

    if not valid_sources:
        return None, False

    base_name = f"VGMatchSources_{target_obj.name}" if target_obj else "VGMatchSources"
    if len(base_name) > 56:
        base_name = base_name[:56]

    collection_name = _get_unique_collection_name(base_name)
    source_collection = bpy.data.collections.new(collection_name)
    context.scene.collection.children.link(source_collection)
    source_collection["ssmt_vg_match_source_count"] = len(valid_sources)

    for source_obj in valid_sources:
        if source_collection not in source_obj.users_collection:
            source_collection.objects.link(source_obj)

    return source_collection, True


class SSMT_OT_CreateGroupFromSelection(bpy.types.Operator):
    bl_idname = "ssmt.create_group_from_selection"
    bl_label = "将所选物体新建到组节点"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objects = context.selected_objects
        if not selected_objects:
            self.report({'WARNING'}, "没有选择任何物体")
            return {'CANCELLED'}

        node_tree = _get_active_blueprint_tree(context)

        if not node_tree:
            self.report({'WARNING'}, "未找到当前活动的蓝图，请先在节点编辑器中打开蓝图")
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

        self.report({'INFO'}, f"已将 {len(selected_objects)} 个物体添加到蓝图 '{node_tree.name}'")
        return {'FINISHED'}


class SSMT_OT_CreateInternalSwitch(bpy.types.Operator):
    bl_idname = "ssmt.create_internal_switch"
    bl_label = "创建内部切换"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_objects = context.selected_objects
        if not selected_objects:
            self.report({'WARNING'}, "没有选择任何物体")
            return {'CANCELLED'}

        node_tree = _get_active_blueprint_tree(context)

        if not node_tree:
            self.report({'WARNING'}, "未找到当前活动的蓝图，请先在节点编辑器中打开蓝图")
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

        self.report({'INFO'}, f"已在蓝图 '{node_tree.name}' 中创建 {len(obj_nodes)} 个物体节点并连接到组节点")
        return {'FINISHED'}


class SSMT_OT_QuickAddRenameRule(bpy.types.Operator):
    bl_idname = "ssmt.quick_add_rename_rule"
    bl_label = "快速添加重命名规则"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active_obj = getattr(context, 'active_object', None)
        if not active_obj:
            return False
        selected_objects = getattr(context, 'selected_objects', [])
        return len(selected_objects) == 2 and active_obj in selected_objects

    def execute(self, context):
        target_obj = context.active_object
        source_candidates = [obj for obj in context.selected_objects if obj != target_obj]
        if len(source_candidates) != 1:
            self.report({'ERROR'}, "请选择两个物体，且活动物体作为目标物体")
            return {'CANCELLED'}

        source_obj = source_candidates[0]
        source_prefix_info = ObjectPrefixHelper.extract_prefix_info(source_obj.name)
        target_prefix_info = ObjectPrefixHelper.extract_prefix_info(target_obj.name)

        if not source_prefix_info or not target_prefix_info:
            self.report({'ERROR'}, "无法从所选物体名称中提取前缀，请确认名称包含可识别前缀")
            return {'CANCELLED'}

        search_str = source_prefix_info[0]
        replace_str = target_prefix_info[0]
        if not search_str or not replace_str:
            self.report({'ERROR'}, "提取到的前缀为空，无法生成重命名规则")
            return {'CANCELLED'}

        node_tree = _get_or_create_blueprint_tree(context)
        rename_nodes = [node for node in node_tree.nodes if node.bl_idname == 'SSMTNode_Object_Rename']
        created_node = None
        if not rename_nodes:
            new_location = _get_new_node_location(node_tree)
            created_node = node_tree.nodes.new(type='SSMTNode_Object_Rename')
            created_node.location = new_location
            rename_nodes = [created_node]

        added_count = 0
        duplicate_count = 0
        for node in rename_nodes:
            if _append_rename_rule(node, search_str, replace_str):
                added_count += 1
            else:
                duplicate_count += 1

        for node in node_tree.nodes:
            node.select = False
        active_node = created_node or rename_nodes[0]
        active_node.select = True
        node_tree.nodes.active = active_node

        mapping_str = f"{search_str} >>> {replace_str}"
        if added_count == 0:
            self.report({'INFO'}, f"重命名规则已存在: {mapping_str}")
        else:
            extra = f"，跳过重复 {duplicate_count} 个节点" if duplicate_count else ""
            self.report({'INFO'}, f"已添加重命名规则: {mapping_str}，影响 {added_count} 个节点{extra}")
        return {'FINISHED'}


class SSMT_OT_QuickAddVertexGroupMatch(bpy.types.Operator):
    bl_idname = "ssmt.quick_add_vertex_group_match"
    bl_label = "快速添加顶点组匹配"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        active_obj = getattr(context, 'active_object', None)
        if not active_obj or active_obj.type != 'MESH':
            return False
        selected_objects = getattr(context, 'selected_objects', [])
        non_active = [obj for obj in selected_objects if obj != active_obj and obj.type == 'MESH']
        return len(non_active) >= 1

    def execute(self, context):
        target_obj = context.active_object
        source_objects = [obj for obj in context.selected_objects if obj != target_obj and obj.type == 'MESH']
        if not source_objects:
            self.report({'ERROR'}, "请至少选择一个非活动网格物体作为源物体")
            return {'CANCELLED'}

        source_collection, is_collection_mode = _create_source_collection_for_vg_match(context, source_objects, target_obj)

        node_tree = _get_or_create_blueprint_tree(context)
        new_location = _get_new_node_location(node_tree)
        match_node = node_tree.nodes.new(type='SSMTNode_VertexGroupMatch')
        match_node.location = new_location
        if is_collection_mode and source_collection:
            match_node.source_collection = source_collection.name
            match_node.source_object = ""
        else:
            match_node.source_object = source_objects[0].name
            match_node.source_collection = ""
        match_node.target_object = target_obj.name
        match_node.match_threshold = 0.06
        match_node.use_chamfer_matching = False
        match_node.use_shape_key = True
        match_node.create_debug_objects = True
        match_node.rename_format = True
        match_node.target_hash = _extract_target_hash_from_name(target_obj.name)

        for node in node_tree.nodes:
            node.select = False
        match_node.select = True
        node_tree.nodes.active = match_node

        rename_map, message = match_node.execute_match(context)
        if rename_map is None:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        source_suffix = f"，已创建源合集: {source_collection.name}" if is_collection_mode and source_collection else ""
        hash_suffix = f"，目标哈希: {match_node.target_hash}" if match_node.target_hash else ""
        self.report({'INFO'}, f"{message}{source_suffix}{hash_suffix}")
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
        layout.separator()
        layout.operator("ssmt.quick_add_rename_rule", text="快速添加重命名规则", icon='FONT_DATA')
        layout.operator("ssmt.quick_add_vertex_group_match", text="快速添加顶点组匹配", icon='GROUP_VERTEX')


class SSMT_MT_NodeMenu_Object(bpy.types.Menu):
    bl_label = "物体"

    def draw(self, context):
        layout = self.layout
        _add_node_entry(layout, "物体信息", 'OBJECT_DATAMODE', "SSMTNode_Object_Info")
        _add_node_entry(layout, "物体组", 'GROUP', "SSMTNode_Object_Group")
        _add_node_entry(layout, "Mod输出", 'EXPORT', "SSMTNode_Result_Output")
        _add_node_entry(layout, "重命名物体", 'FONT_DATA', "SSMTNode_Object_Rename")
        _add_node_entry(layout, "物体切换", 'ARROW_LEFTRIGHT', "SSMTNode_ObjectSwap")


class SSMT_MT_NodeMenu_ShapeKey(bpy.types.Menu):
    bl_label = "形态键"

    def draw(self, context):
        layout = self.layout
        _add_node_entry(layout, "形态键", 'SHAPEKEY_DATA', "SSMTNode_ShapeKey")
        _add_node_entry(layout, "形态键输出", 'FILE_SCRIPT', "SSMTNode_ShapeKey_Output")


class SSMT_MT_NodeMenu_DataType(bpy.types.Menu):
    bl_label = "数据类型"

    def draw(self, context):
        layout = self.layout
        _add_node_entry(layout, "数据类型替换", 'TEXT', "SSMTNode_DataType")


class SSMT_MT_NodeMenu_VertexGroup(bpy.types.Menu):
    bl_label = "顶点组"

    def draw(self, context):
        layout = self.layout
        _add_node_entry(layout, "顶点组匹配", 'GROUP', "SSMTNode_VertexGroupMatch")
        _add_node_entry(layout, "顶点组处理", 'MESH_DATA', "SSMTNode_VertexGroupProcess")
        _add_node_entry(layout, "映射表输入", 'TEXT', "SSMTNode_VertexGroupMappingInput")


class SSMT_MT_NodeMenu_Blueprint(bpy.types.Menu):
    bl_label = "蓝图"

    def draw(self, context):
        layout = self.layout
        _add_node_entry(layout, "蓝图嵌套", 'NODETREE', "SSMTNode_Blueprint_Nest")
        _add_node_entry(layout, "跨IB节点", 'ARROW_LEFTRIGHT', "SSMTNode_CrossIB")
        _add_node_entry(layout, "多文件导出", 'FILE', "SSMTNode_MultiFile_Export")


class SSMT_MT_NodeMenu_PostProcess(bpy.types.Menu):
    bl_label = "后处理"

    def draw(self, context):
        layout = self.layout
        _add_node_entry(layout, "顶点属性定义", 'PROPERTIES', "SSMTNode_PostProcess_VertexAttrs")
        _add_node_entry(layout, "形态键配置", 'SHAPEKEY_DATA', "SSMTNode_PostProcess_ShapeKey")
        _add_node_entry(layout, "材质转资源", 'MATERIAL', "SSMTNode_PostProcess_Material")
        _add_node_entry(layout, "血量检测", 'HEART', "SSMTNode_PostProcess_HealthDetection")
        _add_node_entry(layout, "滑块面板", 'GRIP', "SSMTNode_PostProcess_SliderPanel")
        _add_node_entry(layout, "贴图资源去重", 'PACKAGE', "SSMTNode_PostProcess_ResourceMerge")
        _add_node_entry(layout, "缓冲区清理", 'TRASH', "SSMTNode_PostProcess_BufferCleanup")
        _add_node_entry(layout, "多文件配置", 'FILE_FOLDER', "SSMTNode_PostProcess_MultiFile")


class SSMT_OT_AlignNodes(bpy.types.Operator):
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

    node_tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
    if not node_tree or node_tree.bl_idname != 'SSMTBlueprintTreeType':
        return

    layout = self.layout
    layout.menu("SSMT_MT_NodeMenu_Object", text="物体", icon='OBJECT_DATAMODE')
    layout.menu("SSMT_MT_NodeMenu_ShapeKey", text="形态键", icon='SHAPEKEY_DATA')
    layout.menu("SSMT_MT_NodeMenu_DataType", text="数据类型", icon='FILE_FOLDER')
    layout.menu("SSMT_MT_NodeMenu_VertexGroup", text="顶点组", icon='GROUP_VERTEX')
    layout.menu("SSMT_MT_NodeMenu_Blueprint", text="蓝图", icon='NODETREE')
    layout.menu("SSMT_MT_NodeMenu_PostProcess", text="后处理", icon='MODIFIER')
    layout.separator()
    layout.menu("SSMT_MT_NodePresetMenu", text="添加预设", icon='PRESET')
    layout.separator()
    layout.operator("node.add_node", text="框架", icon='FILE_PARENT').type = "NodeFrame"
    layout.separator()


def draw_node_context_menu(self, context):
    if not isinstance(context.space_data, bpy.types.SpaceNodeEditor):
        return

    node_tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
    if not node_tree or node_tree.bl_idname != 'SSMTBlueprintTreeType':
        return

    layout = self.layout
    layout.separator()
    layout.operator("ssmt.align_nodes", text="矩阵对齐节点", icon='GRID')
    layout.operator("ssmt.batch_connect_nodes", text="批量连接节点", icon='LINKED')
    layout.separator()
    layout.operator_context = 'INVOKE_DEFAULT'
    layout.operator("ssmt.save_node_preset", text="保存预设", icon='PRESET')
    layout.operator_context = 'EXEC_DEFAULT'
    layout.separator()
    layout.operator("ssmt.update_all_node_references", text="更新所有节点引用", icon='FILE_REFRESH')


def register():
    bpy.utils.register_class(SSMT_OT_CreateGroupFromSelection)
    bpy.utils.register_class(SSMT_OT_CreateInternalSwitch)
    bpy.utils.register_class(SSMT_OT_QuickAddRenameRule)
    bpy.utils.register_class(SSMT_OT_QuickAddVertexGroupMatch)
    bpy.utils.register_class(SSMT_OT_AlignNodes)
    bpy.utils.register_class(SSMT_OT_BatchConnectNodes)
    bpy.utils.register_class(SSMT_MT_ObjectContextMenuSub)
    bpy.utils.register_class(SSMT_MT_NodeMenu_Object)
    bpy.utils.register_class(SSMT_MT_NodeMenu_ShapeKey)
    bpy.utils.register_class(SSMT_MT_NodeMenu_DataType)
    bpy.utils.register_class(SSMT_MT_NodeMenu_VertexGroup)
    bpy.utils.register_class(SSMT_MT_NodeMenu_Blueprint)
    bpy.utils.register_class(SSMT_MT_NodeMenu_PostProcess)

    bpy.types.NODE_MT_add.prepend(draw_node_add_menu)
    bpy.types.VIEW3D_MT_object_context_menu.append(draw_objects_context_menu_add)
    bpy.types.NODE_MT_context_menu.append(draw_node_context_menu)

def unregister():
    bpy.types.NODE_MT_context_menu.remove(draw_node_context_menu)
    bpy.types.NODE_MT_add.remove(draw_node_add_menu)
    bpy.types.VIEW3D_MT_object_context_menu.remove(draw_objects_context_menu_add)

    bpy.utils.unregister_class(SSMT_MT_NodeMenu_PostProcess)
    bpy.utils.unregister_class(SSMT_MT_NodeMenu_Blueprint)
    bpy.utils.unregister_class(SSMT_MT_NodeMenu_VertexGroup)
    bpy.utils.unregister_class(SSMT_MT_NodeMenu_DataType)
    bpy.utils.unregister_class(SSMT_MT_NodeMenu_ShapeKey)
    bpy.utils.unregister_class(SSMT_MT_NodeMenu_Object)
    bpy.utils.unregister_class(SSMT_MT_ObjectContextMenuSub)
    bpy.utils.unregister_class(SSMT_OT_BatchConnectNodes)
    bpy.utils.unregister_class(SSMT_OT_AlignNodes)
    bpy.utils.unregister_class(SSMT_OT_QuickAddVertexGroupMatch)
    bpy.utils.unregister_class(SSMT_OT_QuickAddRenameRule)
    bpy.utils.unregister_class(SSMT_OT_CreateInternalSwitch)
    bpy.utils.unregister_class(SSMT_OT_CreateGroupFromSelection)
