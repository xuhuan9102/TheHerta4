'''
存放一些构建 SSMT 蓝图架构的基础节点。
'''
import bpy
from bpy.types import NodeTree, Node, NodeSocket

from ..utils.translate_utils import TR
from ..common.global_config import GlobalConfig
from ..common.text_width_utils import (
    DEFAULT_MIN_NODE_WIDTH,
    DEFAULT_NODE_PADDING,
    DEFAULT_TEXT_WIDTH_SAFETY_FACTOR,
    estimate_text_width,
    get_effective_min_width,
)

try:
    import blf
except ImportError:
    blf = None


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


def _on_use_specific_folder_changed(self, context):
    """「指定文件夹」与「蓝图名文件夹」互斥：勾一个就取消另一个。"""
    if self.use_specific_generate_mod_folder_path and self.use_blueprint_name_generate_mod_folder:
        self.use_blueprint_name_generate_mod_folder = False


def _on_use_blueprint_name_changed(self, context):
    """「蓝图名文件夹」与「指定文件夹」互斥：勾一个就取消另一个。"""
    if self.use_blueprint_name_generate_mod_folder and self.use_specific_generate_mod_folder_path:
        self.use_specific_generate_mod_folder_path = False


class SSMTBlueprintTree(NodeTree):
    '''SSMT Mod Logic Blueprint'''
    bl_idname = 'SSMTBlueprintTreeType'
    bl_label = 'SSMT BluePrint'
    bl_icon = 'NODETREE'

    # 每个蓝图各自独立的生成Mod输出目录设置（不再共用场景全局设置）
    use_specific_generate_mod_folder_path: bpy.props.BoolProperty(
        name="生成Mod到指定的文件夹中",
        description="勾选后将生成Mod到你指定的文件夹中（仅对当前蓝图生效）",
        default=False,
        update=_on_use_specific_folder_changed,
    ) # type: ignore

    generate_mod_folder_path: bpy.props.StringProperty(
        name="生成Mod文件夹路径",
        description="选择的生成Mod的文件夹路径（仅对当前蓝图生效）",
        default="",
        subtype='DIR_PATH',
    ) # type: ignore

    use_blueprint_name_generate_mod_folder: bpy.props.BoolProperty(
        name="生成Mod到 [蓝图名] 文件夹中",
        description=(
            "勾选后输出目录和主 INI 文件名都用当前蓝图名代替工作空间名，即 "
            "SSMTGeneratedMod/[蓝图名]/[蓝图名].ini"
            "（默认用工作空间名）；"
            "与「生成Mod到指定的文件夹中」互斥（仅对当前蓝图生效）"
        ),
        default=False,
        update=_on_use_blueprint_name_changed,
    ) # type: ignore

    # 前处理副本上的修改器开关：勾选后导出前把临时副本的修改器全部关闭（不应用），
    # 等价于手动关掉修改器再导出；原物体不受影响（仅对当前蓝图生效）。
    disable_copy_modifiers_on_export: bpy.props.BoolProperty(
        name="导出前关闭副本修改器",
        description=(
            "勾选后，导出前处理会把临时副本物体（xxx_copy 等）上的修改器全部关闭，"
            "不再应用修改器。等价于手动把修改器的显示开关关掉再导出："
            "修改器效果不会进入导出结果，含大量形态键的物体可省下大量时间。"
            "只影响导出过程创建的临时副本，不会改动原物体上的修改器；仅对当前蓝图生效"
        ),
        default=False,
    ) # type: ignore


_NODE_COLOR_INPUT_SOURCE = (0.38, 0.39, 0.40)
_NODE_COLOR_GROUP = (0.15, 0.16, 0.17)
_NODE_COLOR_SWITCH = (0.19, 0.55, 0.29)
_NODE_COLOR_VERTEX_GROUP = (0.45, 0.65, 0.81)
_NODE_COLOR_POSTPROCESS = (0.54, 0.36, 0.20)
_NODE_COLOR_OUTPUT = (0.57, 0.28, 0.28)
_NODE_COLOR_SHAPEKEY = (0.22, 0.42, 0.40)
_NODE_COLOR_BLUEPRINT = (0.47, 0.33, 0.65)
_NODE_COLOR_SPECIALIZED = (0.57, 0.33, 0.56)
_NODE_COLOR_HIGHLIGHT = (0.70, 0.60, 0.26)
_NODE_COLOR_ANIM_DRIVER = (0.17, 0.60, 0.51)


class SSMTNodeBase(Node):
    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._wrap_color_lifecycle_hook("init")
        cls._wrap_color_lifecycle_hook("copy")

    @classmethod
    def _wrap_color_lifecycle_hook(cls, method_name):
        original_method = cls.__dict__.get(method_name)
        if original_method is None or getattr(original_method, "_ssmt_node_color_wrapped", False):
            return

        if method_name == "init":
            def wrapped(self, context):
                original_method(self, context)
                type(self).apply_default_node_color(self)
        else:
            def wrapped(self, node):
                original_method(self, node)
                type(self).apply_default_node_color(self)

        wrapped._ssmt_node_color_wrapped = True
        setattr(cls, method_name, wrapped)

    @classmethod
    def poll(cls, ntree):
        if ntree.bl_idname != 'SSMTBlueprintTreeType':
            return False
        if ntree.get("is_animation_driver"):
            return False
        return True

    @classmethod
    def get_default_node_color(cls, node_or_bl_idname):
        if isinstance(node_or_bl_idname, str):
            bl_idname = node_or_bl_idname
        else:
            bl_idname = getattr(node_or_bl_idname, "bl_idname", "")

        if bl_idname in {
            'SSMTNode_Object_Info',
            'SSMTNode_Object_Info_Pro',
            'SSMTNode_MultiFile_Export',
        }:
            return _NODE_COLOR_INPUT_SOURCE
        if bl_idname == 'SSMTNode_Object_Group':
            return _NODE_COLOR_GROUP
        if bl_idname == 'SSMTNode_ObjectSwap':
            return _NODE_COLOR_SWITCH
        if bl_idname in {'SSMTNode_VertexGroupMappingInput', 'SSMTNode_VertexGroupMatch', 'SSMTNode_VertexGroupProcess'}:
            return _NODE_COLOR_VERTEX_GROUP
        if bl_idname in {'SSMTNode_ShapeKey', 'SSMTNode_ShapeKey_Output'}:
            return _NODE_COLOR_SHAPEKEY
        if bl_idname in {'SSMTNode_Blueprint_Nest', 'SSMTNode_ModPanel'}:
            return _NODE_COLOR_BLUEPRINT
        if bl_idname == 'SSMTNode_ShaderReplace':
            return _NODE_COLOR_SPECIALIZED
        if bl_idname in {'SSMTNode_Result_Output', 'SSMTNode_Result_Output_NTMIModImp'}:
            return _NODE_COLOR_OUTPUT
        if bl_idname.startswith('SSMTNode_PostProcess_'):
            return _NODE_COLOR_POSTPROCESS
        if bl_idname.startswith('SSMTNode_AnimDriver_'):
            return _NODE_COLOR_ANIM_DRIVER
        if bl_idname.startswith('SSMTNode_'):
            return _NODE_COLOR_SPECIALIZED
        return None

    @classmethod
    def apply_default_node_color(cls, node):
        color = cls.get_default_node_color(node)
        if color is None:
            return False

        node.use_custom_color = True
        node.color = color
        return True

    @classmethod
    def get_highlight_color(cls):
        return _NODE_COLOR_HIGHLIGHT

    def _get_min_node_width(self):
        return get_effective_min_width(getattr(self, "bl_width_min", None), DEFAULT_MIN_NODE_WIDTH)

    def _measure_text_width_with_blf(self, text, padding=DEFAULT_NODE_PADDING):
        if blf is None or not text:
            return None

        try:
            view_preferences = getattr(getattr(bpy.context, "preferences", None), "view", None)
            ui_scale = float(getattr(view_preferences, "ui_scale", 1.0) or 1.0)
            font_size = max(12, int(round(12 * ui_scale)))

            blf.size(0, font_size)

            widest_line = 0.0
            lines = str(text).expandtabs(4).splitlines() or [str(text)]
            for line in lines:
                line_width, _ = blf.dimensions(0, line)
                widest_line = max(widest_line, line_width)

            adjusted_width = widest_line * float(DEFAULT_TEXT_WIDTH_SAFETY_FACTOR)
            return max(self._get_min_node_width(), adjusted_width + float(padding))
        except Exception:
            return None

    def calculate_text_width(self, text, padding=DEFAULT_NODE_PADDING):
        min_width = self._get_min_node_width()
        if not text:
            return min_width

        measured_width = self._measure_text_width_with_blf(text, padding=padding)
        if measured_width is not None:
            return measured_width

        return estimate_text_width(text, padding=padding, min_width=min_width)

    def update_node_width(self, texts):
        min_width = self._get_min_node_width()
        if not texts:
            self.width = min_width
            return

        max_width = min_width
        for text in texts:
            width = self.calculate_text_width(text)
            if width > max_width:
                max_width = width

        self.width = max_width


def refresh_blueprint_node_colors(tree):
    if not tree or getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
        return 0

    updated_count = 0
    for node in getattr(tree, "nodes", []):
        if SSMTNodeBase.apply_default_node_color(node):
            updated_count += 1
    return updated_count


def refresh_all_blueprint_node_colors():
    updated_count = 0
    for tree in bpy.data.node_groups:
        if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
            continue
        updated_count += refresh_blueprint_node_colors(tree)
    return updated_count


def tag_blueprint_editors_redraw():
    window_manager = getattr(bpy.context, "window_manager", None)
    if not window_manager:
        return

    for window in window_manager.windows:
        screen = getattr(window, "screen", None)
        if not screen:
            continue
        for area in screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()


def refresh_all_blueprint_node_colors_and_redraw():
    updated_count = refresh_all_blueprint_node_colors()
    tag_blueprint_editors_redraw()
    return updated_count


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

        # 不在这里写 selected_blueprint_name：下拉框的「选择」和「当前打开的蓝图」
        # 是两件事——用户要求从这里只打开选定的蓝图，但打开的蓝图不能反过来
        # 改变下拉框的选择。选择只由用户在面板下拉框里显式修改。

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

        # 面板传进来的可能是陈旧的枚举序号、__NONE__ 或空串（蓝图在插件算子之外
        # 被重命名/删除后就是这种值）。交给 helper 统一解析：解析不出来就回退到
        # 已校验的当前选择，而不是直接取消——否则用户点删除只会看到一句
        # 「当前没有蓝图可删除」，表现为「删不掉」。
        return BlueprintExportHelper.resolve_blueprint_target_tree(
            self.blueprint_name,
            context=context,
        )

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
        # 先记录删除前的下拉列表顺序：删除后按顺序选下一项，而不是跳回当前打开的蓝图。
        ordered_blueprint_names = [
            tree.name for tree in BlueprintExportHelper.get_all_blueprint_trees()
        ]
        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        bpy.data.node_groups.remove(target_tree)

        next_blueprint_name = BlueprintExportHelper.get_blueprint_name_after_delete(
            ordered_blueprint_names, deleted_blueprint_name
        )
        # 删除后原枚举序号必然失效：写回顺序上的下一项（一个都不剩时写 __NONE__）。
        # 不调用 get_preferred_blueprint_name：它优先返回当前打开的蓝图，会让下拉框
        # 在删掉其他选项后自动跳回当前蓝图（用户反馈的选择被“绑定”问题）。
        if global_properties:
            try:
                global_properties.selected_blueprint_name = (
                    next_blueprint_name or BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER
                )
            except Exception:
                BlueprintExportHelper.ensure_valid_selected_blueprint_name(context=context)

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

        # 同删除：允许面板传空值/陈旧值，解析不出来时回退到已校验的当前选择。
        return BlueprintExportHelper.resolve_blueprint_target_tree(
            self.blueprint_name,
            context=context,
        )

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


class THEHERTA3_OT_CopyFrameProperties(bpy.types.Operator):
    bl_idname = "theherta3.copy_frame_properties"
    bl_label = "复制Frame属性到其他选中Frame"
    bl_description = "将活动 Frame 的标签、颜色和缩放属性复制到其他选中的 Frame"

    def execute(self, context):
        source = getattr(context, "active_node", None)
        if source is None or getattr(source, "bl_idname", "") != "NodeFrame":
            self.report({'WARNING'}, "当前活动节点不是 Frame")
            return {'CANCELLED'}

        copied = 0
        for node in getattr(context, "selected_nodes", []) or []:
            if node == source or getattr(node, "bl_idname", "") != "NodeFrame":
                continue
            node.label = source.label
            if hasattr(node, "label_size") and hasattr(source, "label_size"):
                node.label_size = source.label_size
            node.use_custom_color = source.use_custom_color
            if hasattr(node, "color") and hasattr(source, "color"):
                node.color = source.color[:]
            if hasattr(node, "shrink") and hasattr(source, "shrink"):
                node.shrink = source.shrink
            if hasattr(node, "width") and hasattr(source, "width"):
                node.width = source.width
            if hasattr(node, "height") and hasattr(source, "height"):
                node.height = source.height
            copied += 1

        self.report({'INFO'}, f"已复制 Frame 属性到 {copied} 个节点")
        return {'FINISHED'}


class SSMT_PT_FrameProperties(bpy.types.Panel):
    bl_label = "Frame 属性"
    bl_idname = "SSMT_PT_FrameProperties"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = '节点'

    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        node_tree = getattr(space_data, "node_tree", None)
        if node_tree is None or getattr(node_tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
            return False
        return any(getattr(node, "bl_idname", "") == "NodeFrame" for node in (getattr(context, "selected_nodes", []) or []))

    def draw(self, context):
        layout = self.layout
        selected_frames = [node for node in (getattr(context, "selected_nodes", []) or []) if getattr(node, "bl_idname", "") == "NodeFrame"]
        if not selected_frames:
            layout.label(text="未选中 Frame", icon='INFO')
            return

        frame = selected_frames[0]
        layout.label(text=f"已选中 {len(selected_frames)} 个 Frame", icon='FILE_PARENT')
        layout.prop(frame, "label", text="标签")
        if hasattr(frame, "label_size"):
            layout.prop(frame, "label_size", text="字体大小")
        layout.prop(frame, "use_custom_color", text="自定义颜色")
        if getattr(frame, "use_custom_color", False):
            layout.prop(frame, "color", text="颜色")
        if hasattr(frame, "shrink"):
            layout.prop(frame, "shrink", text="自动贴合")
        if hasattr(frame, "width"):
            layout.prop(frame, "width", text="宽度")
        if hasattr(frame, "height"):
            layout.prop(frame, "height", text="高度")
        if len(selected_frames) > 1:
            layout.operator("theherta3.copy_frame_properties", icon='DUPLICATE')


def register():
    bpy.utils.register_class(SSMTBlueprintTree)
    bpy.utils.register_class(SSMTSocketObject)
    bpy.utils.register_class(SSMTSocketPostProcess)
    bpy.utils.register_class(THEHERTA3_OT_OpenPersistentBlueprint)
    bpy.utils.register_class(THEHERTA3_OT_DeletePersistentBlueprint)
    bpy.utils.register_class(THEHERTA3_OT_RenamePersistentBlueprint)
    bpy.utils.register_class(THEHERTA3_OT_CopyFrameProperties)
    bpy.utils.register_class(SSMT_PT_FrameProperties)


def unregister():
    bpy.utils.unregister_class(SSMT_PT_FrameProperties)
    bpy.utils.unregister_class(THEHERTA3_OT_CopyFrameProperties)
    bpy.utils.unregister_class(THEHERTA3_OT_RenamePersistentBlueprint)
    bpy.utils.unregister_class(THEHERTA3_OT_DeletePersistentBlueprint)
    bpy.utils.unregister_class(SSMTSocketPostProcess)
    bpy.utils.unregister_class(SSMTSocketObject)
    bpy.utils.unregister_class(THEHERTA3_OT_OpenPersistentBlueprint)
    bpy.utils.unregister_class(SSMTBlueprintTree)
