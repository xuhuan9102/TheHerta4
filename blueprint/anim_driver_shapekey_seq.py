import bpy
from bpy.props import IntProperty, StringProperty, BoolProperty, CollectionProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)
from .variable_registry import normalize_variable_name


def _resolve_shapekey_variable(item):
    custom = getattr(item, 'custom_variable_name', '').strip()
    if custom:
        if custom.startswith('$'):
            custom = custom[1:]
        return f"${custom}"
    assigned = getattr(item, 'assigned_variable_name', '').strip()
    if assigned:
        if assigned.startswith('$'):
            assigned = assigned[1:]
        return f"${assigned}"
    return ""


class ShapeKeyAnimTargetObjectItem(bpy.types.PropertyGroup):
    name: StringProperty(
        name="物体名称",
        description="目标网格物体名称",
        default="",
    )


class SSMT_UL_ShapeKeyAnimTargetObjects(bpy.types.UIList):
    bl_idname = "SSMT_UL_SHAPEKEY_ANIM_TARGET_OBJECTS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'OBJECT_DATA' if item.name else 'ERROR'
            row.prop_search(item, "name", bpy.data, "objects", text="", icon=icon_val)


class ShapeKeyAnimItem(bpy.types.PropertyGroup):
    name: StringProperty(
        name="形态键名称",
        description="形态键的名称",
        default="",
    )

    variable: StringProperty(
        name="变量名",
        description="从形态键配置节点解析到的变量名（如 $Freq_Shape）",
        default="",
    )

    frame_values_csv: StringProperty(
        name="帧数值",
        description="各帧的数值，逗号分隔（用于显示）",
        default="",
    )


class SSMT_UL_ShapeKeyAnimItems(bpy.types.UIList):
    bl_idname = "SSMT_UL_SHAPEKEY_ANIM_ITEMS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'SHAPEKEY_DATA' if item.name else 'ERROR'
            row.prop(item, "name", text="", icon=icon_val, emboss=False)
            if item.variable:
                row.label(text=item.variable, icon='VIEWZOOM')
            if item.frame_values_csv:
                row.label(text=item.frame_values_csv, icon='INFO')


class SSMT_OT_ShapeKeyAnimTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_target_add"
    bl_label = "添加目标物体"
    bl_description = "添加一个目标网格物体槽位"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.target_objects.add()
        active = getattr(getattr(context, 'view_layer', None), 'objects', None)
        active_obj = getattr(active, 'active', None)
        if active_obj and getattr(active_obj, 'type', '') == 'MESH':
            item.name = active_obj.name
        else:
            item.name = ""
        node.target_objects_active = len(node.target_objects) - 1
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_target_remove"
    bl_label = "删除目标物体"
    bl_description = "删除当前选中的目标物体"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.target_objects_active
        if 0 <= idx < len(node.target_objects):
            removed = node.target_objects[idx].name.strip()
            if str(getattr(node, 'target_object', '') or '').strip() == removed:
                node.target_object = ''
            node.target_objects.remove(idx)
            node.target_objects_active = min(idx, len(node.target_objects) - 1)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimTargetAddSelected(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_target_add_selected"
    bl_label = "从视图添加选中物体"
    bl_description = "将当前选中的所有网格物体批量加入目标物体列表"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        selected_objs = [obj for obj in getattr(context, 'selected_objects', []) if getattr(obj, 'type', '') == 'MESH']
        if not selected_objs:
            self.report({'WARNING'}, "未在当前视口中选中任何网格物体")
            return {'CANCELLED'}
        existing = {item.name.strip() for item in node.target_objects if item.name.strip()}
        added = 0
        for obj in selected_objs:
            if obj.name not in existing:
                item = node.target_objects.add()
                item.name = obj.name
                existing.add(obj.name)
                added += 1
        if added > 0:
            node.target_objects_active = len(node.target_objects) - 1
            self.report({'INFO'}, f"已添加 {added} 个目标网格物体")
        else:
            self.report({'INFO'}, "选中的网格物体均已在目标列表中")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimFetchKeys(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_fetch_keys"
    bl_label = "从目标物体读取形态键"
    bl_description = "自动读取所有目标物体上的所有非 Basis 形态键，添加到列表中并尝试匹配上层蓝图变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        target_names = node.get_target_object_names()
        if not target_names:
            self.report({'WARNING'}, "请先添加至少一个目标物体")
            return {'CANCELLED'}

        # 获取父级形态键变量映射
        var_map = {}
        for tree_candidate in getattr(bpy.data, "node_groups", []):
            if getattr(tree_candidate, "bl_idname", "") != 'SSMTBlueprintTreeType':
                continue
            if tree_candidate.get("is_animation_driver"):
                continue
            has_ref = any(
                n.bl_idname == 'SSMTNode_PostProcess_AnimDriver' and getattr(n, 'blueprint_name', '') == tree.name
                for n in getattr(tree_candidate, "nodes", [])
            )
            if not has_ref:
                continue
            for n in getattr(tree_candidate, "nodes", []):
                if n.bl_idname == 'SSMTNode_PostProcess_ShapeKey':
                    for item in getattr(n, 'shapekey_variable_items', []):
                        name = getattr(item, 'shape_key_name', '').strip()
                        resolved = _resolve_shapekey_variable(item)
                        if name and resolved:
                            var_map[name] = resolved

        existing_names = {item.name.strip() for item in node.shapekey_items if item.name.strip()}
        added = 0
        for name in target_names:
            obj = bpy.data.objects.get(name)
            if not obj or getattr(obj, "type", "") != 'MESH' or not getattr(obj, "data", None) or not obj.data.shape_keys:
                continue
            for kb in obj.data.shape_keys.key_blocks:
                if kb.name.lower() == 'basis':
                    continue
                if kb.name not in existing_names:
                    item = node.shapekey_items.add()
                    item.name = kb.name
                    if kb.name in var_map:
                        item.variable = var_map[kb.name]
                    existing_names.add(kb.name)
                    added += 1

        if added > 0:
            node.shapekey_items_active = len(node.shapekey_items) - 1
            self.report({'INFO'}, f"已从目标物体读取并添加 {added} 个形态键")
        else:
            self.report({'INFO'}, "未发现新的形态键（可能已全部添加或目标物体无形态键）")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimAdd(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_add"
    bl_label = "添加形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.shapekey_items.add()
        item.name = ""
        node.shapekey_items_active = len(node.shapekey_items) - 1
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimRemove(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_remove"
    bl_label = "删除形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.shapekey_items_active
        if 0 <= idx < len(node.shapekey_items):
            node.shapekey_items.remove(idx)
            node.shapekey_items_active = min(idx, len(node.shapekey_items) - 1)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimRefresh(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_refresh"
    bl_label = "刷新形态键变量"
    bl_description = "从上层蓝图中的形态键配置节点读取变量名，自动填充到列表中已有的同名形态键"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def _find_parent_shapekey_map(self, current_tree) -> dict:
        result = {}
        for tree in bpy.data.node_groups:
            if tree.bl_idname != 'SSMTBlueprintTreeType':
                continue
            if tree.get("is_animation_driver"):
                continue
            has_ref = False
            for node in tree.nodes:
                if node.bl_idname == 'SSMTNode_PostProcess_AnimDriver':
                    if getattr(node, 'blueprint_name', '') == current_tree.name:
                        has_ref = True
                        break
            if not has_ref:
                continue
            for node in tree.nodes:
                if node.bl_idname == 'SSMTNode_PostProcess_ShapeKey':
                    if not hasattr(node, 'shapekey_variable_items'):
                        continue
                    for item in node.shapekey_variable_items:
                        name = getattr(item, 'shape_key_name', '').strip()
                        resolved = _resolve_shapekey_variable(item)
                        if name and resolved:
                            result[name] = resolved
        return result

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        var_map = self._find_parent_shapekey_map(tree)
        if not var_map:
            self.report({'INFO'}, "未在父级蓝图中找到形态键配置节点")
            return {'FINISHED'}
        matched = 0
        for item in node.shapekey_items:
            name = item.name.strip()
            if name in var_map:
                if item.variable != var_map[name]:
                    item.variable = var_map[name]
                    matched += 1
        self.report({'INFO'}, f"已刷新 {matched} 个形态键的变量名")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAnimSample(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_anim_sample"
    bl_label = "采样帧数值"
    bl_description = "对目标物体列表采样各形态键在每一帧的数值"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        target_names = node.get_target_object_names()
        if not target_names:
            self.report({'ERROR'}, "请先添加至少一个目标物体")
            return {'CANCELLED'}

        target_objs = []
        for name in target_names:
            obj = bpy.data.objects.get(name)
            if obj and getattr(obj, "type", "") == 'MESH' and getattr(obj, "data", None) and obj.data.shape_keys:
                target_objs.append(obj)
            elif not obj:
                self.report({'WARNING'}, f"目标物体 '{name}' 不存在，已跳过")
            elif getattr(obj, "type", "") != 'MESH':
                self.report({'WARNING'}, f"目标物体 '{name}' 不是网格物体，已跳过")
            elif not getattr(obj.data, "shape_keys", None):
                self.report({'WARNING'}, f"目标物体 '{name}' 没有形态键，已跳过")

        if not target_objs:
            self.report({'ERROR'}, "未找到任何包含形态键的有效目标网格物体")
            return {'CANCELLED'}

        start_frame = node.frame_start
        end_frame = node.frame_end
        if start_frame > end_frame:
            self.report({'ERROR'}, "起始帧不能大于结束帧")
            return {'CANCELLED'}
        shapekey_names = [item.name.strip() for item in node.shapekey_items if item.name.strip()]
        if not shapekey_names:
            self.report({'ERROR'}, "形态键列表为空，请先添加形态键名称")
            return {'CANCELLED'}

        current_frame = context.scene.frame_current
        current_subframe = getattr(context.scene, 'frame_subframe', 0.0)

        # 记录所有目标物体的原始形态键数值
        original_values = {}
        for obj in target_objs:
            original_values[obj] = {}
            for kb in obj.data.shape_keys.key_blocks:
                if kb.name.lower() != 'basis':
                    original_values[obj][kb.name] = kb.value

        items = list(node.shapekey_items)
        sampled = [[] for _ in items]
        try:
            for frame in range(start_frame, end_frame + 1):
                context.scene.frame_set(frame)
                for item, values in zip(items, sampled):
                    name = item.name.strip()
                    if not name:
                        continue
                    val_found = None
                    for obj in target_objs:
                        kb = obj.data.shape_keys.key_blocks.get(name)
                        if kb is not None and kb.name.lower() != 'basis':
                            val_found = kb.value
                            break
                    values.append(f"{val_found:.3f}" if val_found is not None else "0.000")
        finally:
            try:
                context.scene.frame_set(current_frame, subframe=current_subframe)
            finally:
                for obj, orig_dict in original_values.items():
                    for kb_name, orig_val in orig_dict.items():
                        kb = obj.data.shape_keys.key_blocks.get(kb_name)
                        if kb is not None:
                            kb.value = orig_val

        # Only publish complete samples after restoring the editing state.
        for item, values in zip(items, sampled):
            item.frame_values_csv = ','.join(values)
        frame_count = end_frame - start_frame + 1
        self.report({'INFO'}, f"已完成采样：从 {len(target_objs)} 个物体采样 {len(shapekey_names)} 个形态键 x {frame_count} 帧")
        return {'FINISHED'}


class SSMTNode_AnimDriver_ShapeKeySequence(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_ShapeKeySequence'
    bl_label = '形态键动画序列'
    bl_icon = 'SHAPEKEY_DATA'

    target_object: StringProperty(
        name="目标物体(旧)",
        description="要采样形态键值的物体名称（兼容旧字段）",
        default="",
    )

    target_objects: CollectionProperty(
        type=ShapeKeyAnimTargetObjectItem,
        name="目标物体列表",
    )

    target_objects_active: IntProperty(
        name="当前目标物体",
        default=0,
    )

    shapekey_items: CollectionProperty(
        type=ShapeKeyAnimItem,
        name="形态键列表",
    )

    shapekey_items_active: IntProperty(
        name="当前形态键",
        default=0,
    )

    frame_start: IntProperty(
        name="起始帧",
        description="采样的起始帧",
        default=1,
        min=0,
        max=9999,
    )

    frame_end: IntProperty(
        name="结束帧",
        description="采样的结束帧",
        default=30,
        min=0,
        max=9999,
    )

    driven_variable: StringProperty(
        name="驱动变量",
        description="此序列自动驱动的变量名，映射形态键帧数值到 $Freq_XXX 变量",
        default="",
    )

    default_paused: BoolProperty(
        name="默认播放",
        description="节点默认处于播放状态",
        default=True,
    )

    custom_paused_var: StringProperty(
        name="暂停变量",
        description="自定义暂停状态变量名（留空自动分配）",
        default="",
    )

    loop_playback: BoolProperty(
        name="循环播放",
        description="播放到最后一帧后自动从头开始循环（下游有节点时强制禁用）",
        default=True,
    )

    pingpong_playback: BoolProperty(
        name="往返播放",
        description="开启后往返播放（正向播完反向播放）",
        default=False,
    )

    hold_end_value: BoolProperty(
        name="保持结束值",
        description="播放完成后保持当前值不变，不重置回起始值（仅在未启用循环时生效）",
        default=False,
    )

    def get_target_object_names(self) -> list:
        names = []
        for item in getattr(self, "target_objects", []):
            n = getattr(item, "name", "").strip()
            if n and n not in names:
                names.append(n)
        fallback = getattr(self, "target_object", "").strip()
        if fallback and fallback not in names:
            names.append(fallback)
        return names

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 350
        self._assign_next_available_index()
        self._ensure_paused_variable_name("shapekey_seq_paused")
        self._ensure_sequence_driver_variable_name()
        self._mark_play_state_migrated()

    def copy(self, node):
        self._assign_next_available_index()
        self.custom_paused_var = ""
        self.driven_variable = ""
        self._ensure_paused_variable_name("shapekey_seq_paused")
        self._ensure_sequence_driver_variable_name()
        self._mark_play_state_migrated()

    def _ensure_sequence_driver_variable_name(self):
        current_value = str(getattr(self, "driven_variable", "") or "").strip()
        normalized = normalize_variable_name(current_value)
        if normalized:
            self.driven_variable = f"${normalized}"
            return self.driven_variable
        allocated = self._allocate_unique_anim_driver_variable_name("shapekey_seq")
        self.driven_variable = f"${allocated}"
        return self.driven_variable

    def draw_buttons(self, context, layout):
        safe_idx = self._read_safe_index()
        has_next_in_chain = self._get_next_node_in_chain() is not None

        box = layout.box()
        box.label(text=f"索引: {safe_idx}", icon='LINENUMBERS_ON')

        # 目标物体列表
        row = box.row(align=True)
        row.label(text="目标物体:", icon='OBJECT_DATA')
        op = row.operator("ssmt.shapekey_anim_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.shapekey_anim_target_remove", text="", icon='REMOVE')
        op.node_name = self.name
        op = row.operator("ssmt.shapekey_anim_target_add_selected", text="", icon='RESTRICT_SELECT_OFF')
        op.node_name = self.name

        if self.target_objects:
            box.template_list(
                "SSMT_UL_SHAPEKEY_ANIM_TARGET_OBJECTS", "",
                self, "target_objects",
                self, "target_objects_active",
                rows=max(2, min(len(self.target_objects), 4)),
            )
        else:
            box.prop(self, "target_object", text="目标物体")

        row = box.row(align=True)
        op = row.operator("ssmt.shapekey_anim_fetch_keys", text="从目标物体读取形态键", icon='IMPORT')
        op.node_name = self.name

        box.separator()

        row = box.row(align=True)
        row.label(text="形态键:", icon='SHAPEKEY_DATA')
        op = row.operator("ssmt.shapekey_anim_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.shapekey_anim_remove", text="", icon='REMOVE')
        op.node_name = self.name

        if self.shapekey_items:
            box.template_list(
                "SSMT_UL_SHAPEKEY_ANIM_ITEMS", "",
                self, "shapekey_items",
                self, "shapekey_items_active",
                rows=max(2, min(len(self.shapekey_items), 6)),
            )

        row = box.row(align=True)
        op = row.operator("ssmt.shapekey_anim_refresh", text="刷新变量", icon='FILE_REFRESH')
        op.node_name = self.name

        box.separator()
        box.label(text="采样设置:", icon='SETTINGS')
        row = box.row(align=True)
        row.prop(self, "frame_start")
        row.prop(self, "frame_end")
        op = row.operator("ssmt.shapekey_anim_sample", text="采样", icon='PLAY')
        op.node_name = self.name

        if any(item.frame_values_csv for item in self.shapekey_items):
            box.separator()
            box.label(text="帧数值一览:", icon='INFO')
            col = box.column(align=True)
            for item in self.shapekey_items:
                if item.name.strip() and item.frame_values_csv:
                    values = item.frame_values_csv.split(",")
                    display = ", ".join(v for v in values)
                    col.label(text=f"  {item.name}: {display}")

        box.separator()
        box.prop(self, "driven_variable")

        row = box.row(align=True)
        row.prop(self, "default_paused", text="默认播放")
        if self.custom_paused_var.strip():
            row.prop(self, "custom_paused_var", text="")
        else:
            row.label(text=f"$shapekey_seq_paused{safe_idx}")

        row = box.row(align=True)
        row.prop(self, "pingpong_playback", text="往返", icon='ARROW_LEFTRIGHT')
        if has_next_in_chain:
            row.label(text="循环已禁用（下游有节点）", icon='FILE_REFRESH')
        else:
            row.prop(self, "loop_playback", text="循环", icon='FILE_REFRESH')

        if not self.loop_playback or has_next_in_chain:
            box.prop(self, "hold_end_value", text="保持结束值", icon='KEYFRAME')
        else:
            box.label(text="保持结束值（禁用）", icon='KEYFRAME')

        box.separator()
        box.label(text="连接状态:", icon='INFO')
        if self._has_linked_input():
            box.label(text="  [链输入] 已连接", icon='KEYFRAME')
        else:
            box.label(text="  [链输入] 未连接", icon='SNAP_FACE')
        if self._find_runtime_node() is not None:
            box.label(text="  [运行时间] 已连接", icon='TIME')
        else:
            box.label(text="  [运行时间] 未连接", icon='ERROR')
        if self._has_linked_output():
            box.label(text="  [链输出] 已连接（传递到下一节点）", icon='FORWARD')

    def generate_ini_segment(self, connected_nodes=None) -> str:
        idx = self._read_safe_index()

        runtime = self._find_runtime_node()
        playback_rate = runtime.playback_rate if runtime else 1
        frame_var = self._frame_variable_of(runtime)

        paused_state = self._resolve_default_play_state(getattr(self, "default_paused", True))
        paused_var = str(getattr(self, "custom_paused_var", "") or "").strip()
        if not paused_var:
            paused_var = f"$shapekey_seq_paused{idx}"
        elif not paused_var.startswith('$'):
            paused_var = f"${paused_var}"

        drv = str(getattr(self, "driven_variable", "") or "").strip()
        if not drv:
            drv = f"$shapekey_seq{idx}"
        elif not drv.startswith('$'):
            drv = f"${drv}"

        has_next_in_chain = self._get_next_node_in_chain() is not None
        loop_playback = bool(getattr(self, "loop_playback", True))
        can_loop = not has_next_in_chain and loop_playback
        hold_end_value = bool(getattr(self, "hold_end_value", False))
        is_pingpong = bool(getattr(self, "pingpong_playback", False))

        items_with_data = [
            item for item in self.shapekey_items
            if item.name.strip() and item.variable.strip() and item.frame_values_csv.strip()
        ]

        if not items_with_data:
            lines = [
                "[Constants]",
                self._format_global_assignment(f"$shapekey_seq_frame_count{idx}", 0, persist=True),
                self._format_global_assignment(f"$speed_auto{idx}", playback_rate, persist=True),
                "; 切换速度",
                self._format_global_assignment(paused_var, paused_state, persist=True),
                "; 暂停状态",
            ]
            if is_pingpong:
                lines.extend([
                    self._format_global_assignment(f"$direction{idx}", 1, persist=True),
                    "; 播放方向（1=正向，-1=反向）",
                ])
            return "\n".join(lines)

        frame_values = []
        for item in items_with_data:
            parts = item.frame_values_csv.split(",")
            var = item.variable.strip()
            if not var.startswith('$'):
                var = f"${var}"
            frame_values.append((var, parts))

        frame_count = max(len(parts) for _, parts in frame_values)

        lines = [
            "[Constants]",
            self._format_global_assignment(f"$speed_auto{idx}", playback_rate, persist=True),
            "; 切换速度（由运行时间的播放速率控制）",
            self._format_global_assignment(paused_var, paused_state, persist=True),
            "; 暂停状态",
            self._format_global_assignment(drv, 0, persist=True),
            "; 当前动画帧索引（自驱动）",
            self._format_global_assignment(f"$shapekey_seq_frame_count{idx}", frame_count, persist=True),
            "; 总帧数",
        ]
        if is_pingpong:
            lines.extend([
                self._format_global_assignment(f"$direction{idx}", 1, persist=True),
                "; 播放方向（1=正向，-1=反向）",
            ])

        lines.extend([
            "[Present]",
            f"; 形态键动画序列 - 自驱动 {drv}",
            f"if {paused_var} == 1",
            f"    if {frame_var} % $speed_auto{idx} == 0",
        ])

        # 1. 将当前索引对应的形态键数值赋给各变量
        for f_idx in range(frame_count - 1):
            keyword = "else if" if f_idx > 0 else "if"
            lines.append(f"        {keyword} {drv} < {float(f_idx + 1)}")
            for var, parts in frame_values:
                val = parts[f_idx] if f_idx < len(parts) else "0.000"
                lines.append(f"            {var} = {val}")

        keyword = "else if" if frame_count > 1 else "if"
        lines.append(f"        {keyword} {drv} >= 0")
        for var, parts in frame_values:
            val = parts[-1] if parts else "0.000"
            lines.append(f"            {var} = {val}")
        lines.append("        endif")

        # 2. 帧推进及下游交接逻辑
        if not is_pingpong:
            # 顺序/线性播放
            lines.append(f"        if {drv} < {float(frame_count - 1)}")
            lines.append(f"            {drv} = {drv} + 1.0")
            lines.append("        else")
            if can_loop:
                lines.append(f"            {drv} = 0")
            elif has_next_in_chain:
                lines.append(f"            {paused_var} = 0")
                next_paused_vars = self._get_all_next_paused_vars()
                for next_paused in next_paused_vars:
                    lines.append(f"            {next_paused} = 1")
                if not hold_end_value:
                    lines.append(f"            {drv} = 0")
            else:
                lines.append(f"            {paused_var} = 0")
                if not hold_end_value:
                    lines.append(f"            {drv} = 0")
            lines.append("        endif")
        else:
            # 往返播放
            if frame_count <= 1:
                if can_loop:
                    pass
                elif has_next_in_chain:
                    lines.append(f"        {paused_var} = 0")
                    next_paused_vars = self._get_all_next_paused_vars()
                    for next_paused in next_paused_vars:
                        lines.append(f"        {next_paused} = 1")
                else:
                    lines.append(f"        {paused_var} = 0")
            else:
                lines.append(f"        if $direction{idx} == 1")
                lines.append(f"            if {drv} < {float(frame_count - 1)}")
                lines.append(f"                {drv} = {drv} + 1.0")
                lines.append("            else")
                lines.append(f"                $direction{idx} = -1")
                lines.append(f"                {drv} = {drv} - 1.0")
                lines.append("            endif")
                lines.append("        else")
                lines.append(f"            if {drv} > 0.0")
                lines.append(f"                {drv} = {drv} - 1.0")
                lines.append("            else")
                if can_loop:
                    lines.append(f"                $direction{idx} = 1")
                    lines.append(f"                {drv} = {drv} + 1.0")
                elif has_next_in_chain:
                    lines.append(f"                {paused_var} = 0")
                    next_paused_vars = self._get_all_next_paused_vars()
                    for next_paused in next_paused_vars:
                        lines.append(f"                {next_paused} = 1")
                    lines.append(f"                $direction{idx} = 1")
                    if not hold_end_value:
                        lines.append(f"                {drv} = 0")
                else:
                    lines.append(f"                {paused_var} = 0")
                    lines.append(f"                $direction{idx} = 1")
                    if not hold_end_value:
                        lines.append(f"                {drv} = 0")
                lines.append("            endif")
                lines.append("        endif")

        lines.extend([
            "    endif",
            "endif",
        ])

        return "\n".join(lines)


_load_handler_registered = False


def migrate_existing_shapekey_seq_nodes() -> int:
    """补齐已有形态键序列驱动节点（暂停变量 + 驱动变量 + 目标物体列表迁移）；返回被动过的节点数。"""
    migrated = 0
    for tree in getattr(bpy.data, "node_groups", []) or []:
        if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
            continue
        for node in getattr(tree, "nodes", []) or []:
            if getattr(node, "bl_idname", "") != 'SSMTNode_AnimDriver_ShapeKeySequence':
                continue
            try:
                SSMTNode_AnimDriver_Base.migrate_default_play_state_flag(node)
                SSMTNode_AnimDriver_Base._migrate_dynamic_sockets(node)
                touched = False
                if not getattr(node, "custom_paused_var", ""):
                    node._ensure_indexed_paused_variable_name("shapekey_seq_paused")
                    touched = True
                if not getattr(node, "driven_variable", ""):
                    node._ensure_sequence_driver_variable_name()
                    touched = True
                legacy_target = str(getattr(node, "target_object", "") or "").strip()
                target_objs = getattr(node, "target_objects", None)
                if legacy_target and target_objs is not None:
                    if legacy_target not in {item.name.strip() for item in target_objs}:
                        it = target_objs.add()
                        it.name = legacy_target
                    node.target_object = ''
                    touched = True
                if touched:
                    migrated += 1
                    print(
                        f"[AnimDriver] 形态键序列节点 '{node.name}' 变量与目标物体补填: "
                        f"{node.custom_paused_var} / {node.driven_variable}"
                    )
            except Exception as exc:
                print(
                    f"[AnimDriver][警告] 形态键序列节点 "
                    f"'{getattr(node, 'name', '?')}' 迁移失败: {exc}"
                )
    return migrated


@bpy.app.handlers.persistent
def _shapekey_seq_load_handler(dummy, *args):
    migrate_existing_shapekey_seq_nodes()


classes = (
    ShapeKeyAnimTargetObjectItem,
    SSMT_UL_ShapeKeyAnimTargetObjects,
    ShapeKeyAnimItem,
    SSMT_UL_ShapeKeyAnimItems,
    SSMT_OT_ShapeKeyAnimTargetAdd,
    SSMT_OT_ShapeKeyAnimTargetRemove,
    SSMT_OT_ShapeKeyAnimTargetAddSelected,
    SSMT_OT_ShapeKeyAnimFetchKeys,
    SSMT_OT_ShapeKeyAnimAdd,
    SSMT_OT_ShapeKeyAnimRemove,
    SSMT_OT_ShapeKeyAnimRefresh,
    SSMT_OT_ShapeKeyAnimSample,
    SSMTNode_AnimDriver_ShapeKeySequence,
)


def register():
    global _load_handler_registered
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _load_handler_registered:
        bpy.app.handlers.load_post.append(_shapekey_seq_load_handler)
        _load_handler_registered = True
    # 插件（重新）加载时也要迁移：Reload Scripts / 重新启用不会触发 load_post
    migrate_existing_shapekey_seq_nodes()


def unregister():
    global _load_handler_registered
    if _load_handler_registered:
        bpy.app.handlers.load_post.remove(_shapekey_seq_load_handler)
        _load_handler_registered = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
