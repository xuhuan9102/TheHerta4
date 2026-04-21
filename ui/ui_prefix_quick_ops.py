import bpy

from ..common.object_prefix_helper import ObjectPrefixHelper

class SSMT_PrefixQuickItem(bpy.types.PropertyGroup):
    prefix: bpy.props.StringProperty(name="前缀", default="") # type: ignore
    separator: bpy.props.StringProperty(name="分隔符", default="-") # type: ignore
    display_name: bpy.props.StringProperty(name="显示名称", default="") # type: ignore


class PrefixQuickOpsHelper:
    @staticmethod
    def _build_display_name(prefix: str, object_name: str = "", alias_name: str = "") -> str:
        clean_prefix = ObjectPrefixHelper.normalize_prefix(prefix)
        if not clean_prefix:
            return object_name or alias_name or ""

        if object_name:
            parsed_prefix, _, base_name = ObjectPrefixHelper.split_name_and_prefix(object_name, clean_prefix, ".")
            if parsed_prefix == clean_prefix and base_name:
                return object_name

        clean_alias = (alias_name or "").strip()
        if clean_alias:
            return f"{clean_prefix}.{clean_alias}"

        return clean_prefix

    @staticmethod
    def _upsert_prefix_item(items, prefix: str, separator: str, display_name: str) -> bool:
        clean_prefix = ObjectPrefixHelper.normalize_prefix(prefix)
        if not clean_prefix:
            return False

        for item in items:
            if item.prefix != clean_prefix:
                continue

            item.separator = separator
            current_name = (getattr(item, "display_name", "") or "").strip()
            next_name = (display_name or "").strip()
            if next_name and (not current_name or current_name == clean_prefix):
                item.display_name = next_name
            return False

        item = items.add()
        item.prefix = clean_prefix
        item.separator = separator
        item.display_name = (display_name or clean_prefix).strip() or clean_prefix
        return True

    @staticmethod
    def _iter_object_info_nodes():
        for tree in bpy.data.node_groups:
            if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
                continue
            for node in tree.nodes:
                if getattr(node, "bl_idname", "") == 'SSMTNode_Object_Info':
                    yield node

    @classmethod
    def _find_object_info_nodes(cls, obj):
        object_id = str(obj.as_pointer())
        matched_nodes = []
        for node in cls._iter_object_info_nodes():
            node_object_name = getattr(node, "object_name", "")
            node_object_id = getattr(node, "object_id", "")
            if node_object_id == object_id or node_object_name == obj.name:
                matched_nodes.append(node)
        return matched_nodes

    @staticmethod
    def _apply_prefix_to_node(node, prefix: str, separator: str, object_name: str | None = None):
        if object_name is not None and getattr(node, "object_name", "") != object_name:
            node.object_name = object_name
        node.object_prefix = ObjectPrefixHelper.normalize_prefix(prefix)
        node.prefix_separator = separator or "."

    @classmethod
    def extract_prefix_info(cls, object_name: str):
        return ObjectPrefixHelper.extract_prefix_info(object_name)

    @staticmethod
    def clear_scene_prefixes(scene: bpy.types.Scene) -> None:
        scene.ssmt_prefix_quick_items.clear()

    @classmethod
    def merge_prefixes_from_objects(cls, context, objects) -> int:
        scene = context.scene
        items = scene.ssmt_prefix_quick_items
        added_count = 0

        for obj in objects:
            info = cls.extract_prefix_info(getattr(obj, "name", ""))
            if not info:
                continue

            prefix, separator = info
            display_name = cls._build_display_name(prefix, object_name=getattr(obj, "name", ""))
            if cls._upsert_prefix_item(items, prefix, separator, display_name):
                added_count += 1

        return added_count

    @classmethod
    def merge_prefixes_from_nodes(cls, context) -> int:
        scene = context.scene
        items = scene.ssmt_prefix_quick_items
        added_count = 0

        for node in cls._iter_object_info_nodes():
            info = ObjectPrefixHelper.get_node_prefix_info(node)
            if not info:
                continue

            prefix, separator = info
            display_name = cls._build_display_name(
                prefix,
                object_name=ObjectPrefixHelper.build_virtual_object_name_for_node(node),
                alias_name=getattr(node, "alias_name", ""),
            )
            if cls._upsert_prefix_item(items, prefix, separator, display_name):
                added_count += 1

        return added_count

    @classmethod
    def merge_prefixes_from_selected_nodes(cls, context, objects) -> int:
        scene = context.scene
        items = scene.ssmt_prefix_quick_items
        selected_names = {obj.name for obj in objects}
        selected_ids = {str(obj.as_pointer()) for obj in objects}
        added_count = 0

        for node in cls._iter_object_info_nodes():
            node_object_name = getattr(node, "object_name", "")
            node_object_id = getattr(node, "object_id", "")
            if node_object_name not in selected_names and node_object_id not in selected_ids:
                continue

            info = ObjectPrefixHelper.get_node_prefix_info(node)
            if not info:
                continue

            prefix, separator = info
            display_name = cls._build_display_name(
                prefix,
                object_name=ObjectPrefixHelper.build_virtual_object_name_for_node(node),
                alias_name=getattr(node, "alias_name", ""),
            )
            if cls._upsert_prefix_item(items, prefix, separator, display_name):
                added_count += 1

        return added_count

    @classmethod
    def rebuild_from_scene(cls, context) -> int:
        scene = context.scene
        cls.clear_scene_prefixes(scene)
        object_count = cls.merge_prefixes_from_objects(context, scene.objects)
        node_count = cls.merge_prefixes_from_nodes(context)
        return object_count + node_count

    @classmethod
    def rebuild_from_selection(cls, context) -> int:
        scene = context.scene
        selected_objects = list(context.selected_objects)
        cls.clear_scene_prefixes(scene)
        if not selected_objects:
            return 0

        object_count = cls.merge_prefixes_from_objects(context, selected_objects)
        node_count = cls.merge_prefixes_from_selected_nodes(context, selected_objects)
        return object_count + node_count

    @classmethod
    def resolve_display_name(cls, context, item) -> str:
        current_name = (getattr(item, "display_name", "") or "").strip()
        if current_name and current_name != item.prefix:
            return current_name

        for node in cls._iter_object_info_nodes():
            info = ObjectPrefixHelper.get_node_prefix_info(node)
            if not info:
                continue

            prefix, _ = info
            if prefix != item.prefix:
                continue

            return cls._build_display_name(
                prefix,
                object_name=ObjectPrefixHelper.build_virtual_object_name_for_node(node),
                alias_name=getattr(node, "alias_name", ""),
            )

        for obj in context.scene.objects:
            info = cls.extract_prefix_info(getattr(obj, "name", ""))
            if not info:
                continue

            prefix, _ = info
            if prefix == item.prefix:
                return cls._build_display_name(prefix, object_name=obj.name)

        return item.prefix


class SSMT_OT_PrefixQuickApply(bpy.types.Operator):
    bl_idname = "ssmt.prefix_quick_apply"
    bl_label = "应用前缀"
    bl_description = "给当前选中的物体名称添加指定前缀"
    bl_options = {'REGISTER', 'UNDO'}

    prefix: bpy.props.StringProperty(name="前缀", default="") # type: ignore
    separator: bpy.props.StringProperty(name="分隔符", default="-") # type: ignore

    def execute(self, context):
        selected_objects = list(context.selected_objects)
        if not selected_objects:
            self.report({'WARNING'}, "请先选择至少一个物体")
            return {'CANCELLED'}

        rename_object_name = context.scene.global_properties.prefix_quick_apply_to_object_name
        updated_count = 0
        skipped_count = 0
        missing_node_count = 0
        for obj in selected_objects:
            linked_nodes = PrefixQuickOpsHelper._find_object_info_nodes(obj)
            node_prefix_info = ObjectPrefixHelper.get_node_prefix_info(linked_nodes[0]) if linked_nodes else None
            old_prefix = node_prefix_info[0] if node_prefix_info else ""
            old_separator = node_prefix_info[1] if node_prefix_info else self.separator

            if rename_object_name:
                new_name = ObjectPrefixHelper.replace_prefix(
                    obj.name,
                    self.prefix,
                    self.separator,
                    old_prefix,
                    old_separator,
                )
                old_name = obj.name
                if new_name != old_name:
                    obj.name = new_name
                    if getattr(obj, "data", None) and getattr(obj.data, "users", 0) == 1 and obj.data.name == old_name:
                        obj.data.name = new_name
                elif old_prefix == self.prefix:
                    skipped_count += 1

                for node in linked_nodes:
                    PrefixQuickOpsHelper._apply_prefix_to_node(node, self.prefix, self.separator, obj.name)
                    node.object_id = str(obj.as_pointer())

                if new_name != old_name or linked_nodes:
                    updated_count += 1
            else:
                if not linked_nodes:
                    missing_node_count += 1
                    continue

                node_changed = False
                for node in linked_nodes:
                    before_prefix = getattr(node, "object_prefix", "")
                    before_separator = getattr(node, "prefix_separator", "")
                    PrefixQuickOpsHelper._apply_prefix_to_node(node, self.prefix, self.separator)
                    node.object_id = str(obj.as_pointer())
                    if before_prefix != self.prefix or before_separator != self.separator:
                        node_changed = True

                if node_changed:
                    updated_count += 1
                else:
                    skipped_count += 1

        PrefixQuickOpsHelper.merge_prefixes_from_nodes(context)
        if rename_object_name:
            self.report({'INFO'}, f"已更新 {updated_count} 个物体前缀，跳过 {skipped_count} 个")
        else:
            self.report({'INFO'}, f"已写入 {updated_count} 个物体信息节点前缀，跳过 {skipped_count} 个，未找到节点 {missing_node_count} 个")
        return {'FINISHED'}


class SSMT_OT_PrefixQuickRefresh(bpy.types.Operator):
    bl_idname = "ssmt.prefix_quick_refresh"
    bl_label = "刷新前缀"
    bl_description = "重新扫描当前场景中的物体名称并刷新前缀按钮"

    def execute(self, context):
        if not context.selected_objects:
            self.report({'WARNING'}, "请先选择至少一个物体")
            return {'CANCELLED'}

        prefix_count = PrefixQuickOpsHelper.rebuild_from_selection(context)
        self.report({'INFO'}, f"已刷新 {prefix_count} 个前缀")
        return {'FINISHED'}


class SSMT_OT_PrefixQuickClear(bpy.types.Operator):
    bl_idname = "ssmt.prefix_quick_clear"
    bl_label = "清空前缀"
    bl_description = "清空当前记录的前缀按钮"

    def execute(self, context):
        PrefixQuickOpsHelper.clear_scene_prefixes(context.scene)
        self.report({'INFO'}, "已清空前缀按钮")
        return {'FINISHED'}


def draw_prefix_quick_section(layout, context):
    box = layout.box()
    header = box.row(align=True)
    expanded = context.scene.global_properties.expand_prefix_quick_ops
    header.prop(
        context.scene.global_properties,
        "expand_prefix_quick_ops",
        text="",
        icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
        icon_only=True,
        emboss=False,
    )
    header.label(text="前缀快捷操作", icon='BOOKMARKS')

    if not expanded:
        return

    box.prop(context.scene.global_properties, "prefix_quick_apply_to_object_name")

    toolbar = box.row(align=True)
    toolbar.operator(SSMT_OT_PrefixQuickRefresh.bl_idname, icon='FILE_REFRESH')
    toolbar.operator(SSMT_OT_PrefixQuickClear.bl_idname, icon='TRASH')

    items = context.scene.ssmt_prefix_quick_items
    if not items:
        box.label(text="导入带前缀名称的物体后，这里会自动生成按钮")
        return

    box.label(text=f"已记录前缀: {len(items)}")
    flow = box.column(align=True)
    for item in items:
        button_text = PrefixQuickOpsHelper.resolve_display_name(context, item)
        op = flow.operator(SSMT_OT_PrefixQuickApply.bl_idname, text=button_text)
        op.prefix = item.prefix
        op.separator = item.separator

    if context.selected_objects:
        box.label(text=f"当前选择物体: {len(context.selected_objects)}")
    else:
        box.label(text="先选择物体，再点击对应前缀按钮")


_CLASSES = (
    SSMT_PrefixQuickItem,
    SSMT_OT_PrefixQuickApply,
    SSMT_OT_PrefixQuickRefresh,
    SSMT_OT_PrefixQuickClear,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.ssmt_prefix_quick_items = bpy.props.CollectionProperty(type=SSMT_PrefixQuickItem)


def unregister():
    try:
        del bpy.types.Scene.ssmt_prefix_quick_items
    except Exception:
        pass

    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)