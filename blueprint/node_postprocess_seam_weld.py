"""接缝焊接后处理节点（纯配置，导出期在构造 DrawIBModel 前生效）。

用户最终语义（2026-10-22 拍板）：
- 焊接只认**用户在 Blender 编辑模式手动标记的缝合边（seam edge）**：导出时在每个
  分组内，把「本组物体的、且属于缝合边的顶点」交给 Blender 原生「按距离合并」
  （``bmesh.ops.remove_doubles``，阈值 = 本节点的距离阈值）——位置合并与代表顶点
  完全由 Blender 决定，导出器不做任何自动配对 / 优先规则；
- 只有本节点存在时导出才焊接；没有节点 = 完全保持原导出行为；
- 只焊「同一个组内」的缝合边顶点（组内可以是多个物体），不同组之间绝不焊接；
- 同一物体内部的重合顶点：只有被标记为缝合边的才可能参与合并。

本节点不写任何 INI，只提供分组与距离阈值，供
``common/group_weld_apply.collect_seam_weld_config`` 在导出期读取。
"""

import bpy

from .node_postprocess_base import SSMTNode_PostProcess_Base

NODE_IDNAME = "SSMTNode_PostProcess_SeamWeld"
NODE_LABEL = "接缝焊接"


class SSMTSeamWeldObject(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体", default="")  # type: ignore


class SSMTSeamWeldGroup(bpy.types.PropertyGroup):
    group_name: bpy.props.StringProperty(name="组名", default="组")  # type: ignore
    objects: bpy.props.CollectionProperty(type=SSMTSeamWeldObject)  # type: ignore
    objects_index: bpy.props.IntProperty(default=0)  # type: ignore


def _clamp_index(value, count):
    if count <= 0:
        return -1
    return max(0, min(int(value), count - 1))


class SSMTNode_PostProcess_SeamWeld(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = NODE_LABEL
    bl_description = (
        "只焊接你在 Blender 编辑模式里手动标记的缝合边（seam）顶点："
        "导出时在每个分组内按距离阈值合并这些缝合边顶点，合并由 Blender 原生完成；"
        "没有标记缝合边就不焊接。需要先在编辑模式选中接缝边并「标记缝合边」"
    )
    bl_width_min = 320

    weld_threshold: bpy.props.FloatProperty(  # type: ignore
        name="距离阈值",
        default=0.0001,
        min=0.0,
        precision=6,
    )
    groups: bpy.props.CollectionProperty(type=SSMTSeamWeldGroup)  # type: ignore
    groups_index: bpy.props.IntProperty(default=0)  # type: ignore

    def draw_buttons(self, context, layout):
        layout.label(text="只合并已标记的缝合边(seam)顶点", icon='AUTOMERGE_ON')
        layout.label(text="需先在编辑模式标记缝合边；不同组之间不焊接", icon='INFO')
        layout.prop(self, "weld_threshold")

        box = layout.box()
        box.label(text=f"分组 ({len(self.groups)})", icon='GROUP')
        if self.groups:
            active = _clamp_index(self.groups_index, len(self.groups))
            for index, group in enumerate(self.groups):
                row = box.row(align=True)
                op = row.operator(
                    "ssmt.seam_weld_set_active_group",
                    text=f"组{index + 1}: {group.group_name or '未命名'}",
                    depress=(index == active),
                )
                op.node_name = self.name
                op.index = index
            row = box.row(align=True)
            row.prop(self.groups[active], "group_name", text="组名")
        else:
            box.label(text="还没有分组：先添加组，再加入物体", icon='ERROR')

        row = box.row(align=True)
        row.operator("ssmt.seam_weld_add_group", text="添加组", icon='ADD').node_name = self.name
        op = row.operator("ssmt.seam_weld_remove_group", text="删除当前组", icon='REMOVE')
        op.node_name = self.name
        op.index = int(self.groups_index)

        obj_box = layout.box()
        active = _clamp_index(self.groups_index, len(self.groups))
        if active < 0:
            obj_box.label(text="当前组物体：无", icon='OBJECT_DATA')
            return
        group = self.groups[active]
        obj_box.label(text=f"当前组物体 ({len(group.objects)})", icon='OBJECT_DATA')
        if group.objects:
            for index, item in enumerate(group.objects):
                row = obj_box.row(align=True)
                row.label(text=f"{index + 1}. {item.object_name or '(空)'}", icon='MESH_DATA')
                up_button = row.row(align=True)
                up_button.enabled = index > 0
                up = up_button.operator(
                    "ssmt.seam_weld_move_object", text="", icon='TRIA_UP'
                )
                up.node_name = self.name
                up.group_index = active
                up.object_index = index
                up.direction = -1
                down_button = row.row(align=True)
                down_button.enabled = index + 1 < len(group.objects)
                down = down_button.operator(
                    "ssmt.seam_weld_move_object", text="", icon='TRIA_DOWN'
                )
                down.node_name = self.name
                down.group_index = active
                down.object_index = index
                down.direction = 1
                op = row.operator("ssmt.seam_weld_remove_object", text="", icon='X')
                op.node_name = self.name
                op.group_index = active
                op.object_index = index
        else:
            obj_box.label(text="列表为空：选中物体后点下面的按钮", icon='ERROR')

        row = obj_box.row(align=True)
        row.operator(
            "ssmt.seam_weld_add_selected", text="添加选中物体", icon='ADD'
        ).node_name = self.name
        row.operator(
            "ssmt.seam_weld_create_group_from_selection",
            text="按选中物体建组",
            icon='DUPLICATE',
        ).node_name = self.name

    def execute_postprocess(self, mod_export_path):
        """纯配置节点：不写 INI，只打印一行摘要（导出前已由导出器读取配置）。"""
        pairs = []
        for index, group in enumerate(self.groups):
            names = [
                str(item.object_name or "").strip()
                for item in group.objects
                if str(item.object_name or "").strip()
            ]
            pairs.append(f"组{index + 1}[{', '.join(names) or '空'}]")
        print(
            f"[接缝焊接] 配置节点：{len(self.groups)} 组，阈值 {float(self.weld_threshold):.6g}；"
            "只合并已标记缝合边(seam)的顶点（未标记则不焊接）；"
            + ("；".join(pairs) if pairs else "未配置任何组（导出时不焊接）")
        )
        return True


# --------------------------------------------------------------------------- #
# 算子
# --------------------------------------------------------------------------- #

def _poll_node_editor(context) -> bool:
    space = getattr(context, "space_data", None)
    if space is None or getattr(space, "type", "") != 'NODE_EDITOR':
        return False
    tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    return bool(tree and getattr(tree, "bl_idname", "") == 'SSMTBlueprintTreeType')


def _resolve_weld_node(context, node_name):
    space_data = getattr(context, "space_data", None)
    tree = None
    if space_data is not None:
        tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
    node = tree.nodes.get(node_name) if tree and node_name else None
    if node is None:
        node = getattr(context, "active_node", None)
    if node is None or getattr(node, "bl_idname", "") != NODE_IDNAME:
        return None
    return node


def _active_group(node):
    index = _clamp_index(getattr(node, "groups_index", 0), len(node.groups))
    if index < 0:
        return None
    return node.groups[index]


def _selected_mesh_names(context):
    objects = [
        obj for obj in getattr(context, "selected_objects", [])
        if getattr(obj, "type", "") == 'MESH'
    ]
    if not objects and getattr(context, "active_object", None) is not None:
        active = context.active_object
        if getattr(active, "type", "") == 'MESH':
            objects = [active]
    return [str(getattr(obj, "name", "") or "").strip() for obj in objects]


class SSMT_OT_SeamWeldAddGroup(bpy.types.Operator):
    bl_idname = "ssmt.seam_weld_add_group"
    bl_label = "添加组"
    bl_description = "在接缝焊接节点里新增一个空组"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    @classmethod
    def poll(cls, context):
        return _poll_node_editor(context)

    def execute(self, context):
        node = _resolve_weld_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择接缝焊接节点")
            return {'CANCELLED'}
        group = node.groups.add()
        group.group_name = f"组{len(node.groups)}"
        node.groups_index = len(node.groups) - 1
        return {'FINISHED'}


class SSMT_OT_SeamWeldRemoveGroup(bpy.types.Operator):
    bl_idname = "ssmt.seam_weld_remove_group"
    bl_label = "删除当前组"
    bl_description = "删除接缝焊接节点里的当前组"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    @classmethod
    def poll(cls, context):
        return _poll_node_editor(context)

    def execute(self, context):
        node = _resolve_weld_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择接缝焊接节点")
            return {'CANCELLED'}
        index = int(self.index)
        if index < 0:
            index = _clamp_index(getattr(node, "groups_index", 0), len(node.groups))
        if not (0 <= index < len(node.groups)):
            self.report({'WARNING'}, "没有可删除的组")
            return {'CANCELLED'}
        node.groups.remove(index)
        node.groups_index = _clamp_index(index, len(node.groups))
        return {'FINISHED'}


class SSMT_OT_SeamWeldSetActiveGroup(bpy.types.Operator):
    bl_idname = "ssmt.seam_weld_set_active_group"
    bl_label = "选择组"
    bl_description = "把该组切换为当前组"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=0)  # type: ignore

    @classmethod
    def poll(cls, context):
        return _poll_node_editor(context)

    def execute(self, context):
        node = _resolve_weld_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择接缝焊接节点")
            return {'CANCELLED'}
        node.groups_index = _clamp_index(self.index, len(node.groups))
        return {'FINISHED'}


class SSMT_OT_SeamWeldAddSelected(bpy.types.Operator):
    bl_idname = "ssmt.seam_weld_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前选中的网格物体加入当前组（按物体名去重）"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    @classmethod
    def poll(cls, context):
        return _poll_node_editor(context)

    def execute(self, context):
        node = _resolve_weld_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择接缝焊接节点")
            return {'CANCELLED'}
        group = _active_group(node)
        if group is None:
            self.report({'WARNING'}, "请先添加一个组")
            return {'CANCELLED'}
        names = [name for name in _selected_mesh_names(context) if name]
        if not names:
            self.report({'WARNING'}, "请先选中要添加的网格物体")
            return {'CANCELLED'}
        existing = {str(item.object_name or "") for item in group.objects}
        added = 0
        for name in names:
            if name in existing:
                continue
            item = group.objects.add()
            item.object_name = name
            existing.add(name)
            added += 1
        self.report({'INFO'}, f"已添加 {added} 个物体")
        return {'FINISHED'}


class SSMT_OT_SeamWeldRemoveObject(bpy.types.Operator):
    bl_idname = "ssmt.seam_weld_remove_object"
    bl_label = "移除物体"
    bl_description = "从当前组里移除指定物体"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    group_index: bpy.props.IntProperty(default=-1)  # type: ignore
    object_index: bpy.props.IntProperty(default=-1)  # type: ignore

    @classmethod
    def poll(cls, context):
        return _poll_node_editor(context)

    def execute(self, context):
        node = _resolve_weld_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择接缝焊接节点")
            return {'CANCELLED'}
        group_index = int(self.group_index)
        if not (0 <= group_index < len(node.groups)):
            group_index = _clamp_index(getattr(node, "groups_index", 0), len(node.groups))
        if not (0 <= group_index < len(node.groups)):
            self.report({'WARNING'}, "没有当前组")
            return {'CANCELLED'}
        group = node.groups[group_index]
        object_index = int(self.object_index)
        if not (0 <= object_index < len(group.objects)):
            self.report({'WARNING'}, "该组里没有这一项")
            return {'CANCELLED'}
        group.objects.remove(object_index)
        group.objects_index = _clamp_index(object_index, len(group.objects))
        return {'FINISHED'}


class SSMT_OT_SeamWeldMoveObject(bpy.types.Operator):
    bl_idname = "ssmt.seam_weld_move_object"
    bl_label = "调整物体顺序"
    bl_description = "调整当前组物体的上下顺序；导出时按该顺序链式传递接缝数据"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    group_index: bpy.props.IntProperty(default=-1)  # type: ignore
    object_index: bpy.props.IntProperty(default=-1)  # type: ignore
    direction: bpy.props.IntProperty(default=0)  # type: ignore

    @classmethod
    def poll(cls, context):
        return _poll_node_editor(context)

    def execute(self, context):
        node = _resolve_weld_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择接缝焊接节点")
            return {'CANCELLED'}
        group_index = int(self.group_index)
        if not (0 <= group_index < len(node.groups)):
            self.report({'WARNING'}, "没有当前组")
            return {'CANCELLED'}
        group = node.groups[group_index]
        object_index = int(self.object_index)
        target_index = object_index + int(self.direction)
        if not (0 <= object_index < len(group.objects)):
            self.report({'WARNING'}, "该组里没有这一项")
            return {'CANCELLED'}
        if not (0 <= target_index < len(group.objects)):
            return {'CANCELLED'}
        group.objects.move(object_index, target_index)
        group.objects_index = target_index
        return {'FINISHED'}


class SSMT_OT_SeamWeldCreateGroupFromSelection(bpy.types.Operator):
    bl_idname = "ssmt.seam_weld_create_group_from_selection"
    bl_label = "按选中物体建组"
    bl_description = "新建一个组，并把当前选中的网格物体全部加入该组"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    @classmethod
    def poll(cls, context):
        return _poll_node_editor(context)

    def execute(self, context):
        node = _resolve_weld_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择接缝焊接节点")
            return {'CANCELLED'}
        names = [name for name in _selected_mesh_names(context) if name]
        if not names:
            self.report({'WARNING'}, "请先选中要建组的网格物体")
            return {'CANCELLED'}
        group = node.groups.add()
        group.group_name = f"组{len(node.groups)}"
        for name in names:
            item = group.objects.add()
            item.object_name = name
        node.groups_index = len(node.groups) - 1
        self.report({'INFO'}, f"已建组并加入 {len(names)} 个物体")
        return {'FINISHED'}


classes = (
    SSMTSeamWeldObject,
    SSMTSeamWeldGroup,
    SSMTNode_PostProcess_SeamWeld,
    SSMT_OT_SeamWeldAddGroup,
    SSMT_OT_SeamWeldRemoveGroup,
    SSMT_OT_SeamWeldSetActiveGroup,
    SSMT_OT_SeamWeldAddSelected,
    SSMT_OT_SeamWeldRemoveObject,
    SSMT_OT_SeamWeldMoveObject,
    SSMT_OT_SeamWeldCreateGroupFromSelection,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
