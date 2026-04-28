"""
节点-物体双向关联同步模块
实现：
1. 节点选择时自动选择对应物体（节点→物体）- 支持多选
2. 物体选择时自动选择对应的节点（物体→节点）- 支持多选
3. 防循环选择机制
4. 物体名称变化时自动更新节点引用（支持撤销/重做）
5. 物体视图显示禁用时自动禁用对应节点（双向同步）
"""
import time

import bpy
from bpy.app.handlers import persistent

from ..common.global_config import GlobalConfig

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
_object_hide_state_cache = {}
_msgbus_subscribed = False
_timer_handle = None
_next_maintenance_run_at = 0.0

_SELECTION_SYNC_ACTIVE_INTERVAL = 0.2
_SELECTION_SYNC_IDLE_INTERVAL = 1.0
_MAINTENANCE_ACTIVE_INTERVAL = 2.0
_MAINTENANCE_IDLE_INTERVAL = 5.0
_VISIBILITY_AUTO_MUTE_KEY = "ssmt_auto_muted_by_visibility"

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


def _iter_node_object_references(node):
    try:
        if node.bl_idname == 'SSMTNode_Object_Info':
            return (node,)
        if node.bl_idname == 'SSMTNode_MultiFile_Export':
            return tuple(getattr(node, 'object_list', []))
    except (AttributeError, ReferenceError):
        pass
    return tuple()


def _resolve_reference_state(reference):
    object_id = getattr(reference, 'object_id', '')
    object_name = getattr(reference, 'object_name', '')
    original_object_name = getattr(reference, 'original_object_name', '')

    if object_id:
        cached_name = _object_id_to_name.get(object_id)
        if cached_name:
            return object_id, cached_name, _object_hide_state_cache.get(object_id, False), True

    for candidate_name in (object_name, original_object_name):
        if not candidate_name:
            continue
        obj = bpy.data.objects.get(candidate_name)
        if not obj:
            continue

        current_id = str(obj.as_pointer())
        current_name = obj.name
        is_hidden = bool(obj.hide_viewport or obj.hide_get())
        _object_id_to_name[current_id] = current_name
        _object_hide_state_cache[current_id] = is_hidden
        return current_id, current_name, is_hidden, True

    return object_id, object_name, False, False


def _apply_reference_state(reference, object_id, object_name):
    changed = False

    try:
        previous_name = getattr(reference, 'object_name', '')
        if object_name and previous_name != object_name:
            reference.object_name = object_name
            changed = True

            if previous_name and previous_name != object_name and not getattr(reference, 'original_object_name', ''):
                reference.original_object_name = previous_name

        if object_id and getattr(reference, 'object_id', '') != object_id:
            reference.object_id = object_id
            changed = True
        elif not object_name and getattr(reference, 'object_id', ''):
            reference.object_id = ""
            changed = True
    except (AttributeError, ReferenceError):
        return False

    return changed


def _resolve_reference_object(reference):
    object_id, object_name, _, exists = _resolve_reference_state(reference)
    if not exists:
        return None

    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj:
        _apply_reference_state(reference, str(obj.as_pointer()), obj.name)
        return obj

    return find_object_by_id(object_id) if object_id else None


def _set_node_visibility_state(node, any_hidden):
    try:
        auto_muted = bool(node.get(_VISIBILITY_AUTO_MUTE_KEY, False))

        if any_hidden:
            if not node.mute:
                node.mute = True
                node[_VISIBILITY_AUTO_MUTE_KEY] = True
                print(f"[Sync] 物体视图显示被禁用，自动禁用节点: {node.name}")
            return

        if auto_muted:
            if node.mute:
                node.mute = False
                print(f"[Sync] 物体视图显示已恢复，自动启用节点: {node.name}")
            if _VISIBILITY_AUTO_MUTE_KEY in node:
                del node[_VISIBILITY_AUTO_MUTE_KEY]
    except (AttributeError, ReferenceError):
        return


def _resolve_node_objects(node):
    objects = []
    try:
        for reference in _iter_node_object_references(node):
            obj = _resolve_reference_object(reference)
            if obj and obj not in objects:
                objects.append(obj)
    except (AttributeError, ReferenceError):
        pass

    if not objects and getattr(node, 'bl_idname', '') == 'SSMTNode_Object_Info':
        object_id = getattr(node, 'object_id', '')
        object_name = getattr(node, 'object_name', '')
        if object_id or object_name:
            _log(
                f"_resolve_node_objects: 节点 '{getattr(node, 'name', '?')}' 未找到物体 "
                f"(object_id='{object_id[:8] if object_id else ''}...', object_name='{object_name}')"
            )

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

            matched = (object_id and node_object_id == object_id) or node_object_name == old_name
            if matched:
                if node_object_name != new_name:
                    node.object_name = new_name
                if object_id and node_object_id != object_id:
                    node.object_id = object_id
                if old_name and old_name != new_name and not node_original_name:
                    node.original_object_name = old_name
        elif node.bl_idname == 'SSMTNode_MultiFile_Export':
            any_changed = False
            for item in getattr(node, 'object_list', []):
                item_name = getattr(item, 'object_name', '')
                item_id = getattr(item, 'object_id', '')
                item_original = getattr(item, 'original_object_name', '')
                matched = (object_id and item_id == object_id) or item_name == old_name
                if not matched:
                    continue

                if item_name != new_name:
                    item.object_name = new_name
                    any_changed = True
                if object_id and item_id != object_id:
                    item.object_id = object_id
                    any_changed = True
                if old_name and old_name != new_name and not item_original:
                    item.original_object_name = old_name
                    any_changed = True

            if any_changed:
                node.update_node_width([item.object_name for item in getattr(node, 'object_list', [])])
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
        node_changed = False
        for reference in _iter_node_object_references(node):
            object_id, object_name, _, exists = _resolve_reference_state(reference)
            if exists and _apply_reference_state(reference, object_id, object_name):
                refreshed += 1
                node_changed = True
            elif not object_name and getattr(reference, 'object_id', ''):
                if _apply_reference_state(reference, "", ""):
                    refreshed += 1
                    node_changed = True

        if node_changed and node.bl_idname == 'SSMTNode_MultiFile_Export':
            node.update_node_width([item.object_name for item in getattr(node, 'object_list', [])])
    return refreshed


def find_object_by_id(object_id):
    """通过物体ID查找物体"""
    if not object_id:
        return None

    cached_name = _object_id_to_name.get(object_id)
    if cached_name:
        obj = bpy.data.objects.get(cached_name)
        if obj and str(obj.as_pointer()) == object_id:
            return obj

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


def _rebuild_object_name_cache():
    global _object_id_to_name

    _object_id_to_name = {}
    try:
        for obj in bpy.data.objects:
            _object_id_to_name[str(obj.as_pointer())] = obj.name
    except (AttributeError, ReferenceError):
        pass


def _rebuild_object_hide_state_cache():
    global _object_hide_state_cache

    _object_hide_state_cache = {}
    try:
        for obj in bpy.data.objects:
            try:
                _object_hide_state_cache[str(obj.as_pointer())] = bool(obj.hide_viewport or obj.hide_get())
            except (AttributeError, ReferenceError):
                continue
    except (AttributeError, ReferenceError):
        pass


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
    global _last_node_snapshot, _pending_node_snapshot, _last_active_node_id

    if _is_syncing or not _sync_enabled:
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


def sync_object_visibility_for_object_ids(object_ids, previous_hide_state=None):
    """根据发生变化的物体，局部同步关联节点的静音状态。"""
    if _is_syncing or not object_ids:
        return

    changed_ids = set(object_ids)

    try:
        for tree in bpy.data.node_groups:
            if tree.bl_idname != 'SSMTBlueprintTreeType':
                continue

            for node in tree.nodes:
                if not _is_sync_node(node):
                    continue

                objects = _resolve_node_objects(node)
                if not objects:
                    continue

                obj_ids = [str(obj.as_pointer()) for obj in objects]
                if not changed_ids.intersection(obj_ids):
                    continue

                any_hidden = any(_object_hide_state_cache.get(obj_id, False) for obj_id in obj_ids)
                _set_node_visibility_state(node, any_hidden)
    except Exception as exc:
        print(f"[Sync] sync_object_visibility_for_object_ids 异常: {exc}")


def _sync_all_node_reference_states():
    updated_count = 0

    try:
        for tree in bpy.data.node_groups:
            if tree.bl_idname != 'SSMTBlueprintTreeType':
                continue

            for node in tree.nodes:
                if not _is_sync_node(node):
                    continue

                node_changed = False
                for reference in _iter_node_object_references(node):
                    object_id, object_name, _, exists = _resolve_reference_state(reference)
                    if exists and _apply_reference_state(reference, object_id, object_name):
                        updated_count += 1
                        node_changed = True

                if node_changed and node.bl_idname == 'SSMTNode_MultiFile_Export':
                    node.update_node_width([item.object_name for item in getattr(node, 'object_list', [])])
    except Exception as exc:
        _log(f"_sync_all_node_reference_states 异常: {exc}")

    return updated_count


def _sync_all_node_visibility_states():
    try:
        for tree in bpy.data.node_groups:
            if tree.bl_idname != 'SSMTBlueprintTreeType':
                continue

            for node in tree.nodes:
                if not _is_sync_node(node):
                    continue

                objects = _resolve_node_objects(node)
                if not objects:
                    continue

                any_hidden = any(
                    _object_hide_state_cache.get(str(obj.as_pointer()), bool(obj.hide_viewport or obj.hide_get()))
                    for obj in objects
                )
                _set_node_visibility_state(node, any_hidden)
    except Exception as exc:
        _log(f"_sync_all_node_visibility_states 异常: {exc}")


def _run_periodic_reference_and_visibility_maintenance(has_active_blueprint_tree):
    global _next_maintenance_run_at

    now = time.monotonic()
    interval = _MAINTENANCE_ACTIVE_INTERVAL if has_active_blueprint_tree else _MAINTENANCE_IDLE_INTERVAL
    if now < _next_maintenance_run_at:
        return

    _next_maintenance_run_at = now + interval
    _rebuild_object_name_cache()
    _rebuild_object_hide_state_cache()
    _sync_all_node_reference_states()
    _sync_all_node_visibility_states()


def on_object_renamed_by_id(object_id, old_name, new_name):
    """通过物体ID更新节点引用"""
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'SSMTBlueprintTreeType':
            for node in tree.nodes:
                if _is_sync_node(node):
                    _update_object_name_in_node(node, old_name, new_name, object_id)


def on_node_selection_changed():
    """节点选择变化时触发同步。"""
    if not _sync_enabled:
        return
    check_node_selection()


def on_object_selection_changed():
    """物体选择变化时触发同步。"""
    if not _sync_enabled:
        return
    check_object_selection()


def msgbus_callback_node_selection(*args):
    """msgbus节点选择回调"""
    on_node_selection_changed()


def msgbus_callback_object_selection(*args):
    """msgbus物体选择回调"""
    on_object_selection_changed()


@persistent
def depsgraph_update_handler(scene, depsgraph):
    """depsgraph更新处理器，用于检测物体名称和显示状态变化。"""
    global _object_id_to_name, _object_hide_state_cache

    if not _sync_enabled:
        return

    try:
        updated_object_ids = set()
        previous_hide_state = {}

        for update in depsgraph.updates:
            obj = update.id
            if not isinstance(obj, bpy.types.Object):
                continue

            obj_id = str(obj.as_pointer())
            obj_name = obj.name
            updated_object_ids.add(obj_id)

            if obj_id in _object_id_to_name:
                old_name = _object_id_to_name[obj_id]
                if old_name != obj_name:
                    on_object_renamed_by_id(obj_id, old_name, obj_name)

            _object_id_to_name[obj_id] = obj_name

            try:
                previous_hide_state[obj_id] = _object_hide_state_cache.get(obj_id, False)
                _object_hide_state_cache[obj_id] = bool(obj.hide_viewport or obj.hide_get())
            except (AttributeError, ReferenceError):
                pass

        if updated_object_ids:
            sync_object_visibility_for_object_ids(updated_object_ids, previous_hide_state)
    except (AttributeError, ReferenceError):
        pass


@persistent
def undo_post_handler(scene):
    """撤销/重做后重建缓存并修正节点引用。"""

    if not _sync_enabled:
        return

    try:
        old_name_cache = dict(_object_id_to_name)
        _rebuild_object_name_cache()
        _rebuild_object_hide_state_cache()

        for obj_id, obj_name in _object_id_to_name.items():
            old_name = old_name_cache.get(obj_id)
            if old_name and old_name != obj_name:
                on_object_renamed_by_id(obj_id, old_name, obj_name)

        _sync_all_node_reference_states()
        _sync_all_node_visibility_states()
    except (AttributeError, ReferenceError):
        pass


@persistent
def load_post_handler(scene):
    """文件加载后重建缓存并恢复消息订阅。"""
    _rebuild_object_name_cache()
    _rebuild_object_hide_state_cache()
    subscribe_msgbus()
    _sync_all_node_reference_states()
    _sync_all_node_visibility_states()


def subscribe_msgbus():
    """订阅选择变化，改为事件触发同步。"""
    global _msgbus_subscribed

    if _msgbus_subscribed:
        return

    subscriptions = (
        ((bpy.types.LayerObjects, "selected"), msgbus_callback_object_selection),
        ((bpy.types.LayerObjects, "active"), msgbus_callback_object_selection),
        ((bpy.types.Node, "select"), msgbus_callback_node_selection),
    )

    for key, callback in subscriptions:
        try:
            bpy.msgbus.subscribe_rna(
                key=key,
                owner=_msgbus_owner,
                args=(),
                notify=callback,
            )
        except Exception as exc:
            _log(f"订阅消息总线失败 {key}: {exc}")

    _msgbus_subscribed = True


def unsubscribe_msgbus():
    """取消订阅msgbus消息"""
    global _msgbus_subscribed

    bpy.msgbus.clear_by_owner(_msgbus_owner)
    _msgbus_subscribed = False


def selection_sync_timer_callback():
    """兼容性回退：低频检查选择联动，仅在蓝图编辑器存在时工作。"""
    has_active_blueprint_tree = False

    if not _sync_enabled:
        return _SELECTION_SYNC_IDLE_INTERVAL

    try:
        context = bpy.context
    except (AttributeError, ReferenceError):
        context = None

    tree = None
    if context is not None:
        tree, _, _ = get_active_blueprint_tree(context)
        has_active_blueprint_tree = tree is not None

    if tree is not None:
        try:
            check_node_selection()
        except Exception as exc:
            _log(f"check_node_selection 异常: {exc}")

        try:
            check_object_selection()
        except Exception as exc:
            _log(f"check_object_selection 异常: {exc}")

    try:
        _run_periodic_reference_and_visibility_maintenance(has_active_blueprint_tree)
    except Exception as exc:
        _log(f"_run_periodic_reference_and_visibility_maintenance 异常: {exc}")

    return _SELECTION_SYNC_ACTIVE_INTERVAL if has_active_blueprint_tree else _SELECTION_SYNC_IDLE_INTERVAL


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
        lines.append(f"消息总线订阅: {_msgbus_subscribed}")
        lines.append(f"兼容定时器运行: {_timer_handle is not None and bpy.app.timers.is_registered(_timer_handle) if _timer_handle else False}")
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

    GlobalConfig.read_from_main_json_ssmt4()

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
    """初始化同步所需缓存。"""
    _rebuild_object_name_cache()
    _rebuild_object_hide_state_cache()


def register():
    global _timer_handle, _next_maintenance_run_at, _is_view3d_menu_hooked, _is_node_header_hooked

    for cls in classes:
        bpy.utils.register_class(cls)

    subscribe_msgbus()

    if depsgraph_update_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_handler)
    if load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(load_post_handler)
    if undo_post_handler not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(undo_post_handler)
    if undo_post_handler not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(undo_post_handler)

    if not _timer_handle or not bpy.app.timers.is_registered(_timer_handle):
        _timer_handle = bpy.app.timers.register(
            selection_sync_timer_callback,
            first_interval=_SELECTION_SYNC_ACTIVE_INTERVAL,
            persistent=True,
        )

    _next_maintenance_run_at = 0.0
    _init_object_cache()

    if not _is_view3d_menu_hooked:
        bpy.types.VIEW3D_MT_object_context_menu.append(draw_view3d_sync_menu)
        _is_view3d_menu_hooked = True

    if not _is_node_header_hooked:
        bpy.types.NODE_HT_header.append(draw_node_header)
        _is_node_header_hooked = True


def unregister():
    global _timer_handle, _next_maintenance_run_at, _is_view3d_menu_hooked, _is_node_header_hooked

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
    _timer_handle = None
    _next_maintenance_run_at = 0.0

    unsubscribe_msgbus()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
