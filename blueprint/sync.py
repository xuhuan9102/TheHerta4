"""
节点-物体双向关联同步模块
实现：
1. 节点选择时自动选择对应物体（节点→物体）- 支持多选
2. 物体选择时自动选择对应的节点（物体→节点）- 支持多选
3. 防循环选择机制
4. 物体名称变化时自动更新节点引用（支持撤销/重做）
"""
import bpy
import time
from bpy.app.handlers import persistent

_SYNC_NODE_TYPES = {'SSMTNode_Object_Info', 'SSMTNode_MultiFile_Export'}

_sync_enabled = True
_is_syncing = False
_last_object_snapshot = None
_last_node_snapshot = None
_pending_object_snapshot = None
_pending_node_snapshot = None
_last_active_object_id = ""
_last_active_node_id = ""
_msgbus_owner = object()

_object_id_to_name = {}
_timer_handle = None
_check_counter = 0
_heartbeat_counter = 0

_SYNC_DEBUG = False


def _log(msg):
    if _SYNC_DEBUG:
        print(f"[Sync] {msg}")


def _is_sync_node(node):
    try:
        return getattr(node, 'bl_idname', '') in _SYNC_NODE_TYPES
    except (AttributeError, ReferenceError):
        return False


def _iter_sync_nodes(tree):
    if not tree:
        return []
    return [node for node in tree.nodes if _is_sync_node(node)]


def _node_has_object_id(node, object_id):
    try:
        if node.bl_idname == 'SSMTNode_Object_Info':
            return getattr(node, 'object_id', '') == object_id
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                if getattr(item, 'object_id', '') == object_id:
                        return True
    except (AttributeError, ReferenceError):
        pass
    return False


def _resolve_node_objects(node):
    objects = []
    try:
        if node.bl_idname == 'SSMTNode_Object_Info':
            object_id = getattr(node, 'object_id', '')
            obj = find_object_by_id(object_id) if object_id else None
            if obj:
                objects.append(obj)
            else:
                _log(f"_resolve_node_objects: 节点 '{getattr(node, 'name', '?')}' 未找到物体 (object_id='{object_id[:8] if object_id else ''}...')")
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                object_id = getattr(item, 'object_id', '')
                obj = find_object_by_id(object_id) if object_id else None
                if obj:
                    objects.append(obj)
    except (AttributeError, ReferenceError):
        pass
    return objects


def _resolve_object_nodes(tree, obj):
    if not tree or not obj:
        return []

    object_id = str(obj.as_pointer())
    nodes = find_nodes_by_object_id(tree, object_id)
    if nodes:
        return nodes

    if refresh_stale_node_ids(tree) > 0:
        nodes = find_nodes_by_object_id(tree, object_id)
    return nodes


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
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                item_name = getattr(item, 'object_name', '')
                item_id = getattr(item, 'object_id', '')
                item_original = getattr(item, 'original_object_name', '')
                if object_id and item_id == object_id and item_name != new_name:
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
    try:
        wm = context.window_manager
    except (AttributeError, ReferenceError):
        wm = bpy.context.window_manager

    if not wm:
        return None, None, None

    try:
        for window in wm.windows:
            if not window.screen:
                continue
            for area in window.screen.areas:
                if area.type == 'NODE_EDITOR':
                    for space in area.spaces:
                        if space.type == 'NODE_EDITOR':
                            tree = getattr(space, 'edit_tree', None) or getattr(space, 'node_tree', None)
                            if tree and tree.bl_idname == 'SSMTBlueprintTreeType':
                                return tree, window, area
    except (AttributeError, ReferenceError):
        pass
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


def refresh_stale_node_ids(tree):
    """刷新节点中过期的object_id（物体存在但pointer已变）"""
    refreshed = 0
    for node in tree.nodes:
        if not _is_sync_node(node):
            continue
        if node.bl_idname == 'SSMTNode_Object_Info':
            obj_name = getattr(node, 'object_name', '')
            obj_id = getattr(node, 'object_id', '')
            if obj_name:
                obj = bpy.data.objects.get(obj_name)
                if obj:
                    current_id = str(obj.as_pointer())
                    if obj_id != current_id:
                        _log(f"  刷新过期ID: 节点 '{node.name}' object_id '{obj_id[:8]}...' → '{current_id[:8]}...'")
                        node.object_id = current_id
                        refreshed += 1
                elif obj_id:
                    _log(f"  清除无效ID: 节点 '{node.name}' 物体 '{obj_name}' 不存在")
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            for item in getattr(node, 'object_list', []):
                obj_name = getattr(item, 'object_name', '')
                obj_id = getattr(item, 'object_id', '')
                if obj_name:
                    obj = bpy.data.objects.get(obj_name)
                    if obj:
                        current_id = str(obj.as_pointer())
                        if obj_id != current_id:
                            item.object_id = current_id
                            refreshed += 1
    return refreshed


def find_object_by_id(object_id):
    """通过物体ID查找物体"""
    if not object_id:
        return None

    for obj in bpy.data.objects:
        if str(obj.as_pointer()) == object_id:
            return obj
    return None


def _get_selected_objects(context):
    try:
        return list(context.selected_objects)
    except (AttributeError, ReferenceError):
        try:
            return list(bpy.context.selected_objects)
        except Exception:
            return []


def _try_select_object(obj, context):
    """尝试选中物体。失败时不抛出异常，也不影响源侧选择。"""
    if not obj:
        return False

    try:
        if getattr(obj, 'hide_select', False):
            return False
    except (AttributeError, ReferenceError):
        return False

    try:
        obj.select_set(True)
        return bool(obj.select_get())
    except Exception as exc:
        _log(f"_try_select_object: 物体 '{getattr(obj, 'name', '?')}' 选择失败: {exc}")
        return False


def _get_active_object(context):
    try:
        return context.view_layer.objects.active
    except (AttributeError, ReferenceError):
        try:
            return bpy.context.view_layer.objects.active
        except Exception:
            return None


def _get_active_sync_node(tree):
    try:
        active_node = tree.nodes.active
    except (AttributeError, ReferenceError):
        return None

    if active_node and _is_sync_node(active_node):
        return active_node
    return None


def _get_node_runtime_id(node):
    if not node:
        return ""

    try:
        return str(node.as_pointer())
    except Exception:
        return getattr(node, 'name', '')


def _build_object_snapshot(context):
    selected_objects = _get_selected_objects(context)
    active_object = _get_active_object(context)

    if active_object and active_object not in selected_objects:
        active_object = None

    selected_ids = tuple(sorted(str(obj.as_pointer()) for obj in selected_objects))
    active_id = str(active_object.as_pointer()) if active_object else ""
    return selected_ids, active_id


def _build_node_snapshot(tree):
    if not tree:
        return tuple(), ""

    try:
        selected_nodes = [node for node in tree.nodes if node.select and _is_sync_node(node)]
    except (AttributeError, ReferenceError):
        selected_nodes = []

    active_node = _get_active_sync_node(tree)
    if active_node and active_node not in selected_nodes:
        active_node = None

    selected_ids = tuple(sorted(_get_node_runtime_id(node) for node in selected_nodes))
    active_id = _get_node_runtime_id(active_node)
    return selected_ids, active_id


def _clear_object_selection(context):
    try:
        all_objects = list(context.view_layer.objects)
    except (AttributeError, ReferenceError):
        try:
            all_objects = list(bpy.context.view_layer.objects)
        except Exception:
            all_objects = []

    for obj in all_objects:
        try:
            if obj.select_get():
                obj.select_set(False)
        except (AttributeError, ReferenceError):
            continue


def _apply_object_selection(target_objects, active_object, context):
    selected_objects = []
    _clear_object_selection(context)

    if active_object and active_object in target_objects and _try_select_object(active_object, context):
        selected_objects.append(active_object)

    for obj in target_objects:
        if obj in selected_objects:
            continue
        if _try_select_object(obj, context):
            selected_objects.append(obj)

    chosen_active = None
    if active_object and active_object in selected_objects:
        chosen_active = active_object
    elif selected_objects:
        chosen_active = selected_objects[0]

    if chosen_active:
        try:
            context.view_layer.objects.active = chosen_active
        except (AttributeError, ReferenceError):
            try:
                bpy.context.view_layer.objects.active = chosen_active
            except Exception:
                pass

    return selected_objects, chosen_active


def _apply_node_selection(tree, target_nodes, active_node):
    for node in _iter_sync_nodes(tree):
        try:
            node.select = False
        except (AttributeError, ReferenceError):
            continue

    selected_nodes = []
    if active_node and active_node in target_nodes:
        try:
            active_node.select = True
            selected_nodes.append(active_node)
        except Exception as exc:
            _log(f"_apply_node_selection: 节点 '{getattr(active_node, 'name', '?')}' 选择失败: {exc}")

    for node in target_nodes:
        if node in selected_nodes:
            continue
        try:
            node.select = True
            selected_nodes.append(node)
        except Exception as exc:
            _log(f"_apply_node_selection: 节点 '{getattr(node, 'name', '?')}' 选择失败: {exc}")

    chosen_active = None
    if active_node and active_node in selected_nodes:
        chosen_active = active_node
    elif selected_nodes:
        chosen_active = selected_nodes[0]

    if chosen_active:
        try:
            tree.nodes.active = chosen_active
        except Exception:
            pass

    return selected_nodes, chosen_active


def _mark_pending_object_snapshot(context):
    global _pending_object_snapshot, _last_object_snapshot, _last_active_object_id

    snapshot = _build_object_snapshot(context)
    _pending_object_snapshot = snapshot
    _last_object_snapshot = snapshot
    _last_active_object_id = snapshot[1]


def _mark_pending_node_snapshot(tree):
    global _pending_node_snapshot, _last_node_snapshot, _last_active_node_id

    snapshot = _build_node_snapshot(tree)
    _pending_node_snapshot = snapshot
    _last_node_snapshot = snapshot
    _last_active_node_id = snapshot[1]


def _collect_objects_from_nodes(nodes):
    ordered_objects = []
    for node in nodes:
        if not _is_sync_node(node):
            continue
        for obj in _resolve_node_objects(node):
            if obj not in ordered_objects:
                ordered_objects.append(obj)
    return ordered_objects


def _collect_nodes_from_objects(tree, objects):
    ordered_nodes = []
    for obj in objects:
        for node in _resolve_object_nodes(tree, obj):
            if node not in ordered_nodes:
                ordered_nodes.append(node)
    return ordered_nodes


def _sync_object_selection(target_objects, context):
    """将物体选择状态同步到目标集合，避免历史同步选择不断累积。"""
    target_set = set(target_objects)

    try:
        current_objects = list(context.view_layer.objects)
    except (AttributeError, ReferenceError):
        current_objects = list(bpy.context.view_layer.objects)

    for obj in current_objects:
        try:
            if obj.select_get() and obj not in target_set:
                obj.select_set(False)
        except (AttributeError, ReferenceError):
            continue

    selected_objects = []
    for obj in target_objects:
        if _try_select_object(obj, context):
            selected_objects.append(obj)

    return selected_objects


def _sync_node_selection(tree, target_nodes):
    """将节点选择状态同步到目标集合，避免历史同步选择不断累积。"""
    target_set = set(target_nodes)

    for node in tree.nodes:
        if not _is_sync_node(node):
            continue
        try:
            node.select = node in target_set
        except (AttributeError, ReferenceError):
            continue

    selected_nodes = []
    for node in target_nodes:
        try:
            node.select = True
            selected_nodes.append(node)
        except Exception as exc:
            _log(f"_sync_node_selection: 节点 '{getattr(node, 'name', '?')}' 选择失败: {exc}")

    return selected_nodes


def sync_nodes_to_objects(nodes, context):
    """将当前节点选择完整映射到物体选择。"""
    global _is_syncing

    if _is_syncing or not _sync_enabled:
        return

    tree, _, _ = get_active_blueprint_tree(context)
    active_node = None
    if tree:
        active_node = _get_active_sync_node(tree)
        if active_node not in nodes:
            active_node = None

    objects_to_select = _collect_objects_from_nodes(nodes)

    if not objects_to_select:
        return

    _is_syncing = True
    try:
        active_object = None
        if active_node:
            resolved_objects = _resolve_node_objects(active_node)
            if resolved_objects:
                active_object = resolved_objects[0]

        _apply_object_selection(objects_to_select, active_object, context)
        _mark_pending_object_snapshot(context)
    finally:
        _is_syncing = False


def sync_objects_to_nodes(objects, context):
    """将当前物体选择完整映射到节点选择。"""
    global _is_syncing

    if _is_syncing or not _sync_enabled:
        return

    tree, window, area = get_active_blueprint_tree(context)
    if not tree:
        return

    nodes_to_select = _collect_nodes_from_objects(tree, objects)

    if not nodes_to_select:
        return

    _is_syncing = True
    try:
        active_object = _get_active_object(context)
        active_node = None
        if active_object and active_object in objects:
            resolved_nodes = _resolve_object_nodes(tree, active_object)
            if resolved_nodes:
                active_node = resolved_nodes[0]

        _apply_node_selection(tree, nodes_to_select, active_node)
        _mark_pending_node_snapshot(tree)
    finally:
        _is_syncing = False


def check_node_selection():
    """检查节点选择变化并同步到物体。"""
    global _last_node_snapshot, _pending_node_snapshot, _check_counter, _last_active_node_id

    if _is_syncing or not _sync_enabled:
        return

    _check_counter += 1
    if _check_counter % 2 != 0:
        return

    try:
        context = bpy.context
    except (AttributeError, ReferenceError):
        return

    tree, window, area = get_active_blueprint_tree(context)
    if not tree:
        return

    snapshot = _build_node_snapshot(tree)
    if snapshot == _pending_node_snapshot:
        _pending_node_snapshot = None
        _last_node_snapshot = snapshot
        _last_active_node_id = snapshot[1]
        return

    if snapshot == _last_node_snapshot:
        return

    _last_node_snapshot = snapshot
    _last_active_node_id = snapshot[1]

    selected_nodes = [node for node in tree.nodes if node.select and _is_sync_node(node)]
    if selected_nodes:
        sync_nodes_to_objects(selected_nodes, context)


def check_object_selection():
    """检查物体选择变化并同步到节点。"""
    global _last_object_snapshot, _pending_object_snapshot, _last_active_object_id

    if _is_syncing or not _sync_enabled:
        return

    try:
        context = bpy.context
    except (AttributeError, ReferenceError):
        return

    snapshot = _build_object_snapshot(context)
    if snapshot == _pending_object_snapshot:
        _pending_object_snapshot = None
        _last_object_snapshot = snapshot
        _last_active_object_id = snapshot[1]
        return

    if snapshot == _last_object_snapshot:
        return

    _last_object_snapshot = snapshot
    _last_active_object_id = snapshot[1]

    selected_objects = _get_selected_objects(context)
    if selected_objects:
        sync_objects_to_nodes(selected_objects, context)


def timer_callback():
    """定时器回调函数"""
    global _heartbeat_counter

    _heartbeat_counter += 1
    if _heartbeat_counter > 100000:
        _heartbeat_counter = 1

    if _SYNC_DEBUG and _heartbeat_counter % 200 == 1:
        _log(f"心跳 #{_heartbeat_counter}: 定时器运行中 (enabled={_sync_enabled}, syncing={_is_syncing})")

    if _sync_enabled:
        try:
            check_node_selection()
        except Exception as e:
            _log(f"check_node_selection 异常: {e}")

        try:
            check_object_selection()
        except Exception as e:
            _log(f"check_object_selection 异常: {e}")

        try:
            update_node_references_check()
        except Exception as e:
            _log(f"update_node_references_check 异常: {e}")

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
    """保留兼容入口，实际同步由定时器统一处理。"""
    return


def on_object_selection_changed():
    """保留兼容入口，实际同步由定时器统一处理。"""
    return


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
    """保留接口，不再依赖 msgbus 触发同步。"""
    return


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
                                object_id = getattr(item, 'object_id', '')
                                if object_id:
                                    obj = find_object_by_id(object_id)
                                    if obj and obj.name != obj_name:
                                        item.object_name = obj.name
                                        updated_count += 1
                                elif obj_name:
                                    obj = bpy.data.objects.get(obj_name)
                                    if obj:
                                        item.object_id = str(obj.as_pointer())
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


class SSMT_OT_SyncDebugStatus(bpy.types.Operator):
    """检查同步状态并输出调试信息"""
    bl_idname = "ssmt.sync_debug_status"
    bl_label = "同步调试状态"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global _SYNC_DEBUG
        _SYNC_DEBUG = not _SYNC_DEBUG

        lines = []
        lines.append(f"同步开关: {'开启' if _sync_enabled else '关闭'}")
        lines.append(f"正在同步中: {_is_syncing}")
        lines.append(f"调试模式: {'开启' if _SYNC_DEBUG else '关闭'}")
        lines.append(f"定时器运行: {_timer_handle is not None and bpy.app.timers.is_registered(_timer_handle) if _timer_handle else False}")
        lines.append(f"当前物体快照: {_last_object_snapshot}")
        lines.append(f"当前节点快照: {_last_node_snapshot}")
        lines.append(f"待忽略物体快照: {_pending_object_snapshot}")
        lines.append(f"待忽略节点快照: {_pending_node_snapshot}")
        lines.append(f"物体缓存数: {len(_object_id_to_name)}")

        tree, window, area = get_active_blueprint_tree(context)
        if tree:
            lines.append(f"蓝图树: {tree.name}")
            sync_nodes = [n for n in tree.nodes if _is_sync_node(n)]
            lines.append(f"同步节点数: {len(sync_nodes)}")
            for node in sync_nodes:
                obj_name = getattr(node, 'object_name', '')
                obj_id = getattr(node, 'object_id', '')
                obj = bpy.data.objects.get(obj_name) if obj_name else None
                lines.append(f"  节点 '{node.name}': object_name='{obj_name}', object_id='{obj_id[:8]}...' , 物体存在={obj is not None}")
        else:
            lines.append("蓝图树: 未找到")

        msg = "\n".join(lines)
        print(f"[Sync Debug]\n{msg}")
        self.report({'INFO'}, f"调试模式已{'开启' if _SYNC_DEBUG else '关闭'}，详情见控制台")
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
    row = layout.row(align=True)
    row.operator("ssmt.generate_mod_blueprint", text="导出", icon='EXPORT')


classes = (
    SSMT_OT_ToggleSync,
    SSMT_OT_SyncSelectedNodeToObject,
    SSMT_OT_SyncSelectedObjectToNode,
    SSMT_OT_UpdateAllNodeReferences,
    SSMT_OT_SelectObjectFromNode,
    SSMT_OT_SelectNodeFromObject,
    SSMT_OT_SyncDebugStatus,
)

_is_view3d_menu_hooked = False
_is_node_header_hooked = False


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
    global _timer_handle, _is_view3d_menu_hooked, _is_node_header_hooked

    for cls in classes:
        bpy.utils.register_class(cls)

    subscribe_msgbus()

    _timer_handle = bpy.app.timers.register(timer_callback, persistent=True)

    bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_handler)
    bpy.app.handlers.load_post.append(load_post_handler)
    bpy.app.handlers.undo_post.append(undo_post_handler)
    bpy.app.handlers.redo_post.append(undo_post_handler)

    bpy.app.timers.register(_init_object_cache, first_interval=0.1)

    if not _is_view3d_menu_hooked:
        bpy.types.VIEW3D_MT_object_context_menu.append(draw_view3d_sync_menu)
        _is_view3d_menu_hooked = True

    if not _is_node_header_hooked:
        bpy.types.NODE_HT_header.append(draw_node_header)
        _is_node_header_hooked = True


def unregister():
    global _timer_handle, _is_view3d_menu_hooked, _is_node_header_hooked

    if _is_node_header_hooked:
        bpy.types.NODE_HT_header.remove(draw_node_header)
        _is_node_header_hooked = False

    if _is_view3d_menu_hooked:
        bpy.types.VIEW3D_MT_object_context_menu.remove(draw_view3d_sync_menu)
        _is_view3d_menu_hooked = False

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
