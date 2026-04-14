"""
节点-物体双向关联同步模块
实现：
1. 节点选择时自动选择对应物体（节点→物体）- 支持多选
2. 物体选择时自动选择对应的节点（物体→节点）- 支持多选
3. 防循环选择机制
4. 物体名称变化时自动更新节点引用（支持撤销/重做）
"""
import bpy
from bpy.app.handlers import persistent

_SYNC_NODE_TYPES = {'SSMTNode_Object_Info', 'SSMTNode_MultiFile_Export'}

_sync_enabled = True
_is_syncing = False
_last_synced_objects = set()
_last_synced_nodes = set()
_msgbus_owner = object()

_object_id_to_name = {}
_timer_handle = None
_check_counter = 0


def _is_sync_node(node):
    try:
        return getattr(node, 'bl_idname', '') in _SYNC_NODE_TYPES
    except (AttributeError, ReferenceError):
        return False


def _node_has_object_id(node, object_id):
    try:
        if node.bl_idname == 'SSMTNode_Object_Info':
            return getattr(node, 'object_id', '') == object_id
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                obj_name = getattr(item, 'object_name', '')
                if obj_name:
                    obj = bpy.data.objects.get(obj_name)
                    if obj and str(obj.as_pointer()) == object_id:
                        return True
    except (AttributeError, ReferenceError):
        pass
    return False


def _node_has_object_name(node, object_name):
    try:
        if node.bl_idname == 'SSMTNode_Object_Info':
            return getattr(node, 'object_name', '') == object_name
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                if getattr(item, 'object_name', '') == object_name:
                    return True
    except (AttributeError, ReferenceError):
        pass
    return False


def _resolve_node_objects(node):
    objects = []
    try:
        if node.bl_idname == 'SSMTNode_Object_Info':
            object_name = getattr(node, 'object_name', '')
            obj = bpy.data.objects.get(object_name) if object_name else None
            if not obj:
                object_id = getattr(node, 'object_id', '')
                if object_id:
                    obj = find_object_by_id(object_id)
                    if obj:
                        node.object_name = obj.name
            if obj:
                objects.append(obj)
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                obj_name = getattr(item, 'object_name', '')
                if obj_name:
                    obj = bpy.data.objects.get(obj_name)
                    if obj:
                        objects.append(obj)
    except (AttributeError, ReferenceError):
        pass
    return objects


def _update_object_name_in_node(node, old_name, new_name, object_id=None):
    try:
        if node.bl_idname == 'SSMTNode_Object_Info':
            node_object_name = getattr(node, 'object_name', '')
            node_object_id = getattr(node, 'object_id', '')
            node_original_name = getattr(node, 'original_object_name', '')

            if object_id and node_object_id == object_id and node_object_name != new_name:
                node.object_name = new_name
                if not node_original_name:
                    node.original_object_name = old_name
            elif node_object_name == old_name and not node_object_id:
                node.object_name = new_name
                if not node_original_name:
                    node.original_object_name = old_name
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                item_name = getattr(item, 'object_name', '')
                item_original = getattr(item, 'original_object_name', '')
                if item_name == old_name:
                    item.object_name = new_name
                    if not item_original:
                        item.original_object_name = old_name
    except (AttributeError, ReferenceError):
        pass


def is_blueprint_editor(context):
    """检查当前是否在蓝图编辑器中"""
    space_data = getattr(context, 'space_data', None)
    if space_data and space_data.type == 'NODE_EDITOR':
        tree = getattr(space_data, 'edit_tree', None) or getattr(space_data, 'node_tree', None)
        if tree and tree.bl_idname == 'SSMTBlueprintTreeType':
            return True
    return False


def get_active_blueprint_tree(context):
    """获取当前活动的蓝图树"""
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                for space in area.spaces:
                    if space.type == 'NODE_EDITOR':
                        tree = getattr(space, 'edit_tree', None) or getattr(space, 'node_tree', None)
                        if tree and tree.bl_idname == 'SSMTBlueprintTreeType':
                            return tree, window, area
    return None, None, None


def find_nodes_by_object_id(tree, object_id):
    """通过物体ID查找所有关联的节点"""
    nodes = []
    if not tree or not object_id:
        return nodes

    for node in tree.nodes:
        if _is_sync_node(node) and _node_has_object_id(node, object_id):
            nodes.append(node)
    return nodes


def find_nodes_by_object_name(tree, object_name):
    """通过物体名称查找所有关联的节点"""
    nodes = []
    if not tree or not object_name:
        return nodes

    for node in tree.nodes:
        if _is_sync_node(node) and _node_has_object_name(node, object_name):
            nodes.append(node)
    return nodes


def find_object_by_id(object_id):
    """通过物体ID查找物体"""
    if not object_id:
        return None

    for obj in bpy.data.objects:
        if str(obj.as_pointer()) == object_id:
            return obj
    return None


def sync_nodes_to_objects(nodes, context):
    """多个节点选择时同步选择对应的物体"""
    global _is_syncing, _last_synced_objects

    if _is_syncing or not _sync_enabled:
        return

    objects_to_select = []

    for node in nodes:
        if not _is_sync_node(node):
            continue

        for obj in _resolve_node_objects(node):
            if obj not in objects_to_select:
                objects_to_select.append(obj)

    if not objects_to_select:
        return

    _is_syncing = True
    try:
        for o in context.selected_objects:
            if o not in objects_to_select:
                o.select_set(False)

        for obj in objects_to_select:
            obj.select_set(True)

        if objects_to_select:
            context.view_layer.objects.active = objects_to_select[0]

        _last_synced_objects = set(objects_to_select)
    finally:
        _is_syncing = False


def sync_objects_to_nodes(objects, context):
    """多个物体选择时同步选择对应的节点"""
    global _is_syncing, _last_synced_nodes

    if _is_syncing or not _sync_enabled:
        return

    tree, window, area = get_active_blueprint_tree(context)
    if not tree:
        return

    nodes_to_select = []

    for obj in objects:
        object_id = str(obj.as_pointer())
        nodes = find_nodes_by_object_id(tree, object_id)

        if not nodes:
            nodes = find_nodes_by_object_name(tree, obj.name)

        for node in nodes:
            if node not in nodes_to_select:
                nodes_to_select.append(node)

    if not nodes_to_select:
        return

    _is_syncing = True
    try:
        for node in tree.nodes:
            if node not in nodes_to_select:
                node.select = False

        for node in nodes_to_select:
            node.select = True

        if nodes_to_select:
            tree.nodes.active = nodes_to_select[0]

        _last_synced_nodes = set(nodes_to_select)
    finally:
        _is_syncing = False


def check_node_selection():
    """检查节点选择状态变化"""
    global _last_synced_nodes, _check_counter

    if _is_syncing or not _sync_enabled:
        return

    _check_counter += 1
    if _check_counter % 3 != 0:
        return

    context = bpy.context

    tree, window, area = get_active_blueprint_tree(context)
    if not tree:
        return

    selected_nodes = [n for n in tree.nodes if n.select]

    if not selected_nodes:
        _last_synced_nodes = set()
        return

    current_selection = set(selected_nodes)
    if current_selection == _last_synced_nodes:
        return

    _last_synced_nodes = current_selection

    sync_nodes = [n for n in selected_nodes if _is_sync_node(n)]
    if sync_nodes:
        sync_nodes_to_objects(sync_nodes, context)


def check_object_selection():
    """检查物体选择状态变化"""
    global _last_synced_objects

    if _is_syncing or not _sync_enabled:
        return

    context = bpy.context
    selected_objects = set(context.selected_objects)

    if not selected_objects:
        _last_synced_objects = set()
        return

    if selected_objects == _last_synced_objects:
        return

    _last_synced_objects = selected_objects

    sync_objects_to_nodes(list(selected_objects), context)


def timer_callback():
    """定时器回调函数"""
    if _sync_enabled:
        check_node_selection()
        check_object_selection()
        update_node_references_check()
    return 0.05


def update_node_references_check():
    """定期检查并更新节点引用"""
    global _object_id_to_name

    try:
        current_objects = {}
        for obj in bpy.data.objects:
            obj_id = str(obj.as_pointer())
            current_objects[obj_id] = obj.name

        for obj_id, old_name in _object_id_to_name.items():
            if obj_id in current_objects:
                new_name = current_objects[obj_id]
                if old_name != new_name:
                    on_object_renamed_by_id(obj_id, old_name, new_name)

        _object_id_to_name = current_objects
    except (AttributeError, ReferenceError):
        pass


def on_object_renamed_by_id(object_id, old_name, new_name):
    """通过物体ID更新节点引用"""
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'SSMTBlueprintTreeType':
            for node in tree.nodes:
                if _is_sync_node(node):
                    _update_object_name_in_node(node, old_name, new_name, object_id)


def on_node_selection_changed():
    """节点选择变化回调（手动触发）"""
    global _last_synced_nodes

    if _is_syncing or not _sync_enabled:
        return

    context = bpy.context
    if not is_blueprint_editor(context):
        return

    space_data = context.space_data
    tree = getattr(space_data, 'edit_tree', None) or getattr(space_data, 'node_tree', None)
    if not tree:
        return

    selected_nodes = [n for n in tree.nodes if n.select]

    if not selected_nodes:
        _last_synced_nodes = set()
        return

    if set(selected_nodes) == _last_synced_nodes:
        return

    _last_synced_nodes = set(selected_nodes)

    sync_nodes = [n for n in selected_nodes if _is_sync_node(n)]
    if sync_nodes:
        sync_nodes_to_objects(sync_nodes, context)


def on_object_selection_changed():
    """物体选择变化回调（手动触发）"""
    global _last_synced_objects

    if _is_syncing or not _sync_enabled:
        return

    context = bpy.context
    selected_objects = set(context.selected_objects)

    if not selected_objects:
        _last_synced_objects = set()
        return

    if selected_objects == _last_synced_objects:
        return

    _last_synced_objects = selected_objects

    sync_objects_to_nodes(list(selected_objects), context)


def msgbus_callback_node_selection(*args):
    """msgbus节点选择回调"""
    on_node_selection_changed()


def msgbus_callback_object_selection(*args):
    """msgbus物体选择回调"""
    on_object_selection_changed()


@persistent
def depsgraph_update_handler(scene, depsgraph):
    """depsgraph更新处理器，用于检测物体名称变化"""
    global _object_id_to_name

    if not _sync_enabled:
        return

    try:
        for update in depsgraph.updates:
            obj = update.id
            if isinstance(obj, bpy.types.Object):
                obj_id = str(obj.as_pointer())
                obj_name = obj.name

                if obj_id in _object_id_to_name:
                    old_name = _object_id_to_name[obj_id]
                    if old_name != obj_name:
                        on_object_renamed_by_id(obj_id, old_name, obj_name)

                _object_id_to_name[obj_id] = obj_name
    except (AttributeError, ReferenceError):
        pass


@persistent
def undo_post_handler(scene):
    """撤销/重做后更新节点引用"""
    global _object_id_to_name

    if not _sync_enabled:
        return

    try:
        current_objects = {}
        for obj in bpy.data.objects:
            obj_id = str(obj.as_pointer())
            current_objects[obj_id] = obj.name

            if obj_id in _object_id_to_name:
                old_name = _object_id_to_name[obj_id]
                if old_name != obj.name:
                    on_object_renamed_by_id(obj_id, old_name, obj.name)

        _object_id_to_name = current_objects
    except (AttributeError, ReferenceError):
        pass


@persistent
def load_post_handler(scene):
    """文件加载后重建物体名称缓存"""
    global _object_id_to_name
    _object_id_to_name = {}

    try:
        for obj in bpy.data.objects:
            _object_id_to_name[str(obj.as_pointer())] = obj.name
    except (AttributeError, ReferenceError):
        pass


def subscribe_msgbus():
    """订阅msgbus消息"""
    subscribe_to = bpy.types.LayerObjects, "active"
    bpy.msgbus.subscribe_rna(
        key=subscribe_to,
        owner=_msgbus_owner,
        args=(),
        notify=msgbus_callback_object_selection,
        options={'PERSISTENT'}
    )

    bpy.msgbus.publish_rna(key=subscribe_to)


def unsubscribe_msgbus():
    """取消订阅msgbus消息"""
    bpy.msgbus.clear_by_owner(_msgbus_owner)


class SSMT_OT_ToggleSync(bpy.types.Operator):
    """切换节点-物体同步功能"""
    bl_idname = "ssmt.toggle_sync"
    bl_label = "切换节点-物体同步"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _sync_enabled
        _sync_enabled = not _sync_enabled
        status = "开启" if _sync_enabled else "关闭"
        self.report({'INFO'}, f"节点-物体同步已{status}")
        return {'FINISHED'}


class SSMT_OT_SyncSelectedNodeToObject(bpy.types.Operator):
    """手动同步选中节点到物体"""
    bl_idname = "ssmt.sync_node_to_object"
    bl_label = "同步节点到物体"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if not is_blueprint_editor(context):
            self.report({'WARNING'}, "请在蓝图编辑器中使用此功能")
            return {'CANCELLED'}

        space_data = context.space_data
        tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}

        selected_nodes = [n for n in tree.nodes if n.select and _is_sync_node(n)]
        if not selected_nodes:
            self.report({'WARNING'}, "请选择至少一个同步节点")
            return {'CANCELLED'}

        sync_nodes_to_objects(selected_nodes, context)
        self.report({'INFO'}, f"已同步 {len(selected_nodes)} 个节点到物体")
        return {'FINISHED'}


class SSMT_OT_SyncSelectedObjectToNode(bpy.types.Operator):
    """手动同步选中物体到节点"""
    bl_idname = "ssmt.sync_object_to_node"
    bl_label = "同步物体到节点"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if not context.selected_objects:
            self.report({'WARNING'}, "请选择至少一个物体")
            return {'CANCELLED'}

        sync_objects_to_nodes(context.selected_objects, context)
        self.report({'INFO'}, f"已同步 {len(context.selected_objects)} 个物体到节点")
        return {'FINISHED'}


class SSMT_OT_UpdateAllNodeReferences(bpy.types.Operator):
    """更新所有节点的物体引用"""
    bl_idname = "ssmt.update_all_node_references"
    bl_label = "更新所有节点引用"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        updated_count = 0

        for tree in bpy.data.node_groups:
            if tree.bl_idname == 'SSMTBlueprintTreeType':
                for node in tree.nodes:
                    if not _is_sync_node(node):
                        continue

                    try:
                        if node.bl_idname == 'SSMTNode_Object_Info':
                            object_id = getattr(node, 'object_id', '')
                            object_name = getattr(node, 'object_name', '')

                            if object_id:
                                obj = find_object_by_id(object_id)
                                if obj and obj.name != object_name:
                                    node.object_name = obj.name
                                    updated_count += 1
                            elif object_name:
                                obj = bpy.data.objects.get(object_name)
                                if obj:
                                    node.object_id = str(obj.as_pointer())

                        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
                            for item in getattr(node, 'object_list', []):
                                obj_name = getattr(item, 'object_name', '')
                                if obj_name:
                                    obj = bpy.data.objects.get(obj_name)
                                    if not obj:
                                        original_name = getattr(item, 'original_object_name', '')
                                        if original_name:
                                            obj = bpy.data.objects.get(original_name)
                                            if obj:
                                                item.object_name = obj.name
                                                updated_count += 1
                    except (AttributeError, ReferenceError):
                        pass

        if updated_count > 0:
            self.report({'INFO'}, f"已更新 {updated_count} 个节点引用")
        else:
            self.report({'INFO'}, "所有节点引用已是最新")

        return {'FINISHED'}


class SSMT_OT_SelectObjectFromNode(bpy.types.Operator):
    """从节点选择关联的物体"""
    bl_idname = "ssmt.select_object_from_node"
    bl_label = "选择关联物体"
    bl_options = {'REGISTER'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name)
        if not node:
            return {'CANCELLED'}

        sync_nodes_to_objects([node], context)
        return {'FINISHED'}


class SSMT_OT_SelectNodeFromObject(bpy.types.Operator):
    """从物体选择关联的节点"""
    bl_idname = "ssmt.select_node_from_object"
    bl_label = "选择关联节点"
    bl_options = {'REGISTER'}

    object_name: bpy.props.StringProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.object_name)
        if not obj:
            return {'CANCELLED'}

        sync_objects_to_nodes([obj], context)
        return {'FINISHED'}


def draw_view3d_sync_menu(self, context):
    """在3D视图物体右键菜单中添加同步选项"""
    if not context.selected_objects:
        return

    layout = self.layout
    layout.separator()
    layout.operator("ssmt.sync_object_to_node", text="选择关联节点", icon='NODE')


def draw_node_header(self, context):
    """在节点编辑器头部显示同步状态"""
    if not isinstance(context.space_data, bpy.types.SpaceNodeEditor):
        return
    if context.space_data.tree_type != 'SSMTBlueprintTreeType':
        return

    layout = self.layout
    sync_status = "同步: 开启" if _sync_enabled else "同步: 关闭"
    icon = 'CHECKMARK' if _sync_enabled else 'X'
    row = layout.row(align=True)
    row.operator("ssmt.toggle_sync", text=sync_status, icon=icon, emboss=False)


classes = (
    SSMT_OT_ToggleSync,
    SSMT_OT_SyncSelectedNodeToObject,
    SSMT_OT_SyncSelectedObjectToNode,
    SSMT_OT_UpdateAllNodeReferences,
    SSMT_OT_SelectObjectFromNode,
    SSMT_OT_SelectNodeFromObject,
)


def _init_object_cache():
    """初始化物体名称缓存"""
    global _object_id_to_name
    _object_id_to_name = {}
    try:
        for obj in bpy.data.objects:
            _object_id_to_name[str(obj.as_pointer())] = obj.name
    except (AttributeError, ReferenceError):
        pass


def register():
    global _timer_handle

    for cls in classes:
        bpy.utils.register_class(cls)

    subscribe_msgbus()

    _timer_handle = bpy.app.timers.register(timer_callback, persistent=True)

    bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_handler)
    bpy.app.handlers.load_post.append(load_post_handler)
    bpy.app.handlers.undo_post.append(undo_post_handler)
    bpy.app.handlers.redo_post.append(undo_post_handler)

    bpy.app.timers.register(_init_object_cache, first_interval=0.1)

    bpy.types.VIEW3D_MT_object_context_menu.append(draw_view3d_sync_menu)
    bpy.types.NODE_HT_header.append(draw_node_header)


def unregister():
    global _timer_handle

    bpy.types.NODE_HT_header.remove(draw_node_header)
    bpy.types.VIEW3D_MT_object_context_menu.remove(draw_view3d_sync_menu)

    if load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(load_post_handler)

    if depsgraph_update_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_update_handler)

    if undo_post_handler in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(undo_post_handler)

    if undo_post_handler in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(undo_post_handler)

    if _timer_handle and bpy.app.timers.is_registered(_timer_handle):
        bpy.app.timers.unregister(_timer_handle)

    unsubscribe_msgbus()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
