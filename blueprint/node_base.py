'''
存放一些构建 SSMT 蓝图架构的基础节点。
'''
import bpy
from bpy.types import NodeTree, Node, NodeSocket

from ..utils.translate_utils import TR
from ..common.global_config import GlobalConfig


class SSMTSocketObject(NodeSocket):
    '''Custom Socket for Object Data'''
    bl_idname = 'SSMTSocketObject'
    bl_label = 'Object Socket'

    def draw_color(self, context, node):
        return (0.0, 0.8, 0.8, 1.0)

    def draw(self, context, layout, node, text):
        layout.label(text=text)


class SSMTSocketPostProcess(NodeSocket):
    '''Custom Socket for Post Process Path'''
    bl_idname = 'SSMTSocketPostProcess'
    bl_label = 'Post Process Socket'

    def draw_color(self, context, node):
        return (1.0, 0.5, 0.0, 1.0)

    def draw(self, context, layout, node, text):
        layout.label(text=text)


class SSMTBlueprintTree(NodeTree):
    '''SSMT Mod Logic Blueprint'''
    bl_idname = 'SSMTBlueprintTreeType'
    bl_label = 'SSMT BluePrint'
    bl_icon = 'NODETREE'


class SSMTNodeBase(Node):
    @classmethod
    def poll(cls, ntree):
        return ntree.bl_idname == 'SSMTBlueprintTreeType'

    def calculate_text_width(self, text, padding=40):
        if not text:
            return 200

        char_count = 0
        for char in text:
            code = ord(char)
            is_wide = False

            if 0x4E00 <= code <= 0x9FFF:
                is_wide = True
            elif 0x3400 <= code <= 0x4DBF:
                is_wide = True
            elif 0x20000 <= code <= 0x2A6DF:
                is_wide = True
            elif 0x2A700 <= code <= 0x2B73F:
                is_wide = True
            elif 0x2B740 <= code <= 0x2B81F:
                is_wide = True
            elif 0x2B820 <= code <= 0x2CEAF:
                is_wide = True
            elif 0x2CEB0 <= code <= 0x2EBEF:
                is_wide = True
            elif 0x30000 <= code <= 0x3134F:
                is_wide = True
            elif 0xF900 <= code <= 0xFAFF:
                is_wide = True
            elif 0x2E80 <= code <= 0x2EFF:
                is_wide = True
            elif 0x3040 <= code <= 0x309F:
                is_wide = True
            elif 0x30A0 <= code <= 0x30FF:
                is_wide = True
            elif 0xAC00 <= code <= 0xD7AF:
                is_wide = True
            elif 0x1100 <= code <= 0x11FF:
                is_wide = True
            elif 0x3130 <= code <= 0x318F:
                is_wide = True
            elif 0xFF00 <= code <= 0xFFEF:
                is_wide = True
            elif 0x3000 <= code <= 0x303F:
                is_wide = True
            elif 0x2190 <= code <= 0x21FF:
                is_wide = True
            elif 0x2200 <= code <= 0x22FF:
                is_wide = True
            elif 0x2500 <= code <= 0x257F:
                is_wide = True
            elif 0x25A0 <= code <= 0x25FF:
                is_wide = True
            elif 0x2600 <= code <= 0x26FF:
                is_wide = True
            elif 0x1F300 <= code <= 0x1F9FF:
                is_wide = True

            char_count += 2 if is_wide else 1

        width = char_count * 8 + padding
        return max(200, width)

    def update_node_width(self, texts):
        if not texts:
            return

        max_width = 200
        for text in texts:
            width = self.calculate_text_width(text)
            if width > max_width:
                max_width = width

        self.width = max_width


class THEHERTA3_OT_OpenPersistentBlueprint(bpy.types.Operator):
    bl_idname = "theherta3.open_persistent_blueprint"
    bl_label = TR.translate("打开蓝图界面")
    bl_description = TR.translate("打开一个独立的蓝图窗口，用于配置Mod逻辑")

    blueprint_name: bpy.props.StringProperty(
        name="Blueprint Name",
        default="",
        options={'SKIP_SAVE'},
    ) # type: ignore

    def execute(self, context):
        from .export_helper import BlueprintExportHelper

        GlobalConfig.read_from_main_json_ssmt4()
        requested_tree_name = str(self.blueprint_name or "").strip()
        tree_name = requested_tree_name or GlobalConfig.get_workspace_name()

        tree = bpy.data.node_groups.get(tree_name)
        if tree and getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
            tree = None

        if not tree and requested_tree_name:
            tree = BlueprintExportHelper.get_selected_blueprint_tree(requested_tree_name, context=context)

        if not tree:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
            tree.use_fake_user = True

        BlueprintExportHelper.set_runtime_blueprint_tree(tree)

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        target_window = None
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != 'NODE_EDITOR':
                    continue
                for space in area.spaces:
                    if space.type == 'NODE_EDITOR' and space.node_tree == tree:
                        target_window = window
                        break
                if target_window:
                    break
            if target_window:
                break

        if target_window and len(context.window_manager.windows) > 1:
            try:
                if hasattr(context, 'temp_override'):
                    with context.temp_override(window=target_window):
                        bpy.ops.wm.window_close()
                else:
                    override = context.copy()
                    override['window'] = target_window
                    override['screen'] = target_window.screen
                    bpy.ops.wm.window_close(override)
            except Exception as exc:
                print(f"SSMT: Failed to close existing window, creating new one anyway. Error: {exc}")

        old_windows = set(context.window_manager.windows)
        bpy.ops.wm.window_new()
        new_windows = set(context.window_manager.windows)
        created_window = (new_windows - old_windows).pop() if (new_windows - old_windows) else None

        if created_window:
            screen = created_window.screen
            target_area = max(screen.areas, key=lambda area: area.width * area.height)

            if target_area:
                target_area.ui_type = 'SSMTBlueprintTreeType'
                target_area.type = 'NODE_EDITOR'

                for space in target_area.spaces:
                    if space.type == 'NODE_EDITOR':
                        space.tree_type = 'SSMTBlueprintTreeType'
                        space.node_tree = tree
                        space.pin = True

        return {'FINISHED'}


class THEHERTA3_OT_DeletePersistentBlueprint(bpy.types.Operator):
    bl_idname = "theherta3.delete_persistent_blueprint"
    bl_label = "删除蓝图"
    bl_description = "删除当前选中的蓝图"
    bl_options = {'REGISTER', 'INTERNAL'}

    blueprint_name: bpy.props.StringProperty(
        name="Blueprint Name",
        default="",
        options={'SKIP_SAVE'},
    ) # type: ignore

    def _get_target_tree(self, context):
        from .export_helper import BlueprintExportHelper

        requested_tree_name = str(self.blueprint_name or "").strip()
        if requested_tree_name == "__NONE__":
            return None
        return BlueprintExportHelper.get_selected_blueprint_tree(requested_tree_name, context=context)

    def invoke(self, context, event):
        target_tree = self._get_target_tree(context)
        if not target_tree:
            self.report({'WARNING'}, "当前没有蓝图可删除")
            return {'CANCELLED'}

        self.blueprint_name = target_tree.name
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.label(text="确认删除当前选中的蓝图吗？", icon='TRASH')
        layout.label(text=self.blueprint_name)
        layout.label(text="删除后无法恢复，请确认不是误操作。", icon='ERROR')

    def execute(self, context):
        from .export_helper import BlueprintExportHelper

        target_tree = self._get_target_tree(context)
        if not target_tree:
            self.report({'WARNING'}, "当前没有蓝图可删除")
            return {'CANCELLED'}

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != 'NODE_EDITOR':
                    continue
                for space in area.spaces:
                    if space.type != 'NODE_EDITOR':
                        continue
                    if getattr(space, "node_tree", None) == target_tree:
                        space.node_tree = None

        if BlueprintExportHelper.runtime_blueprint_tree_name == target_tree.name:
            BlueprintExportHelper.runtime_blueprint_tree_name = ""

        deleted_blueprint_name = target_tree.name
        bpy.data.node_groups.remove(target_tree)

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        preferred_blueprint_name = BlueprintExportHelper.get_preferred_blueprint_name(context=context)
        if global_properties:
            global_properties.selected_blueprint_name = preferred_blueprint_name or "__NONE__"

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        self.report({'INFO'}, "已删除蓝图: " + deleted_blueprint_name)
        return {'FINISHED'}


class THEHERTA3_OT_RenamePersistentBlueprint(bpy.types.Operator):
    bl_idname = "theherta3.rename_persistent_blueprint"
    bl_label = "重命名蓝图"
    bl_description = "重命名当前选中的蓝图"
    bl_options = {'REGISTER', 'INTERNAL'}

    blueprint_name: bpy.props.StringProperty(
        name="Blueprint Name",
        default="",
        options={'SKIP_SAVE'},
    ) # type: ignore

    new_blueprint_name: bpy.props.StringProperty(
        name="新蓝图名称",
        default="",
    ) # type: ignore

    def _get_target_tree(self, context):
        from .export_helper import BlueprintExportHelper

        requested_tree_name = str(self.blueprint_name or "").strip()
        if requested_tree_name == "__NONE__":
            return None
        return BlueprintExportHelper.get_selected_blueprint_tree(requested_tree_name, context=context)

    def invoke(self, context, event):
        target_tree = self._get_target_tree(context)
        if not target_tree:
            self.report({'WARNING'}, "当前没有蓝图可重命名")
            return {'CANCELLED'}

        self.blueprint_name = target_tree.name
        self.new_blueprint_name = target_tree.name
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.label(text="请输入新的蓝图名称", icon='GREASEPENCIL')
        layout.prop(self, "new_blueprint_name", text="名称")

    def execute(self, context):
        from .export_helper import BlueprintExportHelper

        target_tree = self._get_target_tree(context)
        if not target_tree:
            self.report({'WARNING'}, "当前没有蓝图可重命名")
            return {'CANCELLED'}

        new_name = str(self.new_blueprint_name or "").strip()
        if not new_name:
            self.report({'ERROR'}, "蓝图名称不能为空")
            return {'CANCELLED'}

        if new_name == "__NONE__":
            self.report({'ERROR'}, "蓝图名称不能使用保留值 __NONE__")
            return {'CANCELLED'}

        if new_name == target_tree.name:
            self.report({'INFO'}, "蓝图名称未发生变化")
            return {'CANCELLED'}

        existing_tree = bpy.data.node_groups.get(new_name)
        if existing_tree and existing_tree != target_tree:
            self.report({'ERROR'}, "已存在同名蓝图，请使用其他名称")
            return {'CANCELLED'}

        old_name = target_tree.name
        target_tree.name = new_name

        if BlueprintExportHelper.runtime_blueprint_tree_name == old_name:
            BlueprintExportHelper.runtime_blueprint_tree_name = target_tree.name

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if global_properties:
            global_properties.selected_blueprint_name = target_tree.name

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        self.report({'INFO'}, "已将蓝图重命名为: " + target_tree.name)
        return {'FINISHED'}


def register():
    bpy.utils.register_class(SSMTBlueprintTree)
    bpy.utils.register_class(SSMTSocketObject)
    bpy.utils.register_class(SSMTSocketPostProcess)
    bpy.utils.register_class(THEHERTA3_OT_OpenPersistentBlueprint)
    bpy.utils.register_class(THEHERTA3_OT_DeletePersistentBlueprint)
    bpy.utils.register_class(THEHERTA3_OT_RenamePersistentBlueprint)


def unregister():
    bpy.utils.unregister_class(THEHERTA3_OT_RenamePersistentBlueprint)
    bpy.utils.unregister_class(THEHERTA3_OT_DeletePersistentBlueprint)
    bpy.utils.unregister_class(SSMTSocketPostProcess)
    bpy.utils.unregister_class(SSMTSocketObject)
    bpy.utils.unregister_class(THEHERTA3_OT_OpenPersistentBlueprint)
    bpy.utils.unregister_class(SSMTBlueprintTree)
