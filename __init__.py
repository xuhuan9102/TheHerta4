import bpy


from .common import global_properties
from .common.global_config import GlobalConfig
from .utils import log_utils as _log_utils
from .utils import texture_auto_reload


from .ui import ui_panel_basic
from .ui import ui_func_import
from .ui import ui_func_import_ssmt
from .ui import ui_prefix_quick_ops

from . import blueprint

from .ui import ui_func_export

from . import toolkit

from . import addon_updater_ops

import importlib
importlib.reload(addon_updater_ops)
importlib.reload(global_properties)
importlib.reload(blueprint)
importlib.reload(ui_prefix_quick_ops)
importlib.reload(texture_auto_reload)

_ORIGINAL_NODE_SELECTED_COLOR = None


def _refresh_blueprint_node_colors_safe():
    """安全刷新所有蓝图节点的颜色（带异常保护）"""
    try:
        from .blueprint.node_base import refresh_all_blueprint_node_colors_and_redraw
        refresh_all_blueprint_node_colors_and_redraw()
    except Exception:
        pass


def _schedule_blueprint_node_color_refresh():
    """通过Blender定时器安排蓝图节点颜色刷新"""
    def _timer_callback():
        _refresh_blueprint_node_colors_safe()
        return None

    try:
        bpy.app.timers.register(_timer_callback, first_interval=0.1, persistent=False)
    except Exception:
        _refresh_blueprint_node_colors_safe()


def _set_node_selected_theme_color(color):
    """设置Blender节点编辑器中节点的选中状态主题色（橙色主题）"""
    global _ORIGINAL_NODE_SELECTED_COLOR

    try:
        theme = bpy.context.preferences.themes[0]
        node_editor = theme.node_editor
    except Exception:
        return

    if _ORIGINAL_NODE_SELECTED_COLOR is None:
        try:
            _ORIGINAL_NODE_SELECTED_COLOR = tuple(node_editor.node_selected)
        except Exception:
            _ORIGINAL_NODE_SELECTED_COLOR = None

    try:
        node_editor.node_selected = color
    except Exception:
        pass

bl_info = {
    "name": "TheHerta4",
    "description": "Blender Plugin of SSMT4",
    "blender": (4, 5, 0),
    "version": (4, 4, 41),
    "location": "View3D",
    "category": "Generic"
}


class HERTT_OT_SwitchToMainPanel(bpy.types.Operator):
    """切换回主面板"""
    bl_idname = "model.switch_to_main_panel"
    bl_label = "切换回主面板"

    def execute(self, context):
        """执行：将面板切换到主面板模式"""
        context.scene.herta_show_toolkit = False
        return {'FINISHED'}


class HERTT_OT_SwitchToToolkit(bpy.types.Operator):
    """切换到工具集面板"""
    bl_idname = "model.switch_to_toolkit"
    bl_label = "切换到工具集面板"
    
    def execute(self, context):
        """执行：将面板切换到工具集模式"""
        context.scene.herta_show_toolkit = True
        return {'FINISHED'}


class UpdaterPanel(bpy.types.Panel):
    """更新检查面板 - 在3D视口侧边栏显示版本更新信息"""
    bl_label = "检查版本更新"
    bl_idname = "HERTA_PT_UpdaterPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "TheHerta4"
    bl_order = 99
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        """轮询条件：仅在主面板模式下显示（非工具集模式）"""
        if not hasattr(context.scene, 'herta_show_toolkit'):
            return True
        return not context.scene.herta_show_toolkit

    def draw(self, context):
        """绘制更新面板UI"""
        layout = self.layout
        col = layout.column()
        col.scale_y = 0.7
        if addon_updater_ops.updater.update_ready:
            layout.label(text="存在可用更新！", icon="INFO")

        addon_updater_ops.update_settings_ui(self, context)


class HertaUpdatePreference(bpy.types.AddonPreferences):
    """插件更新器偏好设置"""
    bl_label = "TheHerta 更新器"
    bl_idname = __package__


    auto_check_update: bpy.props.BoolProperty(
        name="自动检查更新",
        description="如启用，按设定的时间间隔自动检查更新",
        default=True) # type: ignore

    updater_interval_months: bpy.props.IntProperty(
        name='月',
        description="自动检查更新间隔月数",
        default=0,
        min=0) # type: ignore

    updater_interval_days: bpy.props.IntProperty(
        name='天',
        description="自动检查更新间隔天数",
        default=1,
        min=0,
        max=31) # type: ignore

    updater_interval_hours: bpy.props.IntProperty(
        name='小时',
        description="自动检查更新间隔小时数",
        default=0,
        min=0,
        max=23) # type: ignore

    updater_interval_minutes: bpy.props.IntProperty(
        name='分钟',
        description="自动检查更新间隔分钟数",
        default=0,
        min=0,
        max=59) # type: ignore
    def draw(self, context):
        """绘制偏好设置面板UI"""
        layout = self.layout
        layout.prop(self, "自动检查更新")
        addon_updater_ops.update_settings_ui(self, context)

def register():
    """插件注册入口 - 注册所有属性、面板、操作符和蓝图系统"""
    global_properties.register()
    GlobalConfig.read_from_main_json_ssmt4()
    _set_node_selected_theme_color((0.78, 0.41, 0.10))
    
    bpy.types.Scene.herta_show_toolkit = bpy.props.BoolProperty(
        name="显示工具集",
        description="切换显示工具集面板",
        default=False
    )
    
    bpy.utils.register_class(HERTT_OT_SwitchToMainPanel)
    bpy.utils.register_class(HERTT_OT_SwitchToToolkit)
    
    addon_updater_ops.register(bl_info)
    bpy.utils.register_class(UpdaterPanel)
    bpy.utils.register_class(HertaUpdatePreference)

    blueprint.register()
    # Velo 桥接支持两种安装布局：作为 TheHerta4 包内子包，或独立复制到 addons 目录。
    try:
        import TheHerta4_Velo_Bridge
    except ImportError:
        from . import TheHerta4_Velo_Bridge
    TheHerta4_Velo_Bridge.register()
    _schedule_blueprint_node_color_refresh()
    ui_prefix_quick_ops.register()
    ui_panel_basic.register()
    ui_func_import_ssmt.register()
    ui_func_import.register()
    ui_func_export.register()
    
    toolkit.register()
    texture_auto_reload.register()

    try:
        from .blueprint.export_helper import BlueprintExportHelper
        BlueprintExportHelper.ensure_valid_selected_blueprint_name()
    except Exception:
        pass



def unregister():
    """插件注销入口 - 注销所有注册的属性、面板、操作符和蓝图系统"""
    global _ORIGINAL_NODE_SELECTED_COLOR

    if _ORIGINAL_NODE_SELECTED_COLOR is not None:
        _set_node_selected_theme_color(_ORIGINAL_NODE_SELECTED_COLOR)
        _ORIGINAL_NODE_SELECTED_COLOR = None

    texture_auto_reload.unregister()
    toolkit.unregister()
    
    ui_func_export.unregister()
    ui_func_import.unregister()
    ui_func_import_ssmt.unregister()
    ui_panel_basic.unregister()
    ui_prefix_quick_ops.unregister()
    try:
        import TheHerta4_Velo_Bridge
    except ImportError:
        from . import TheHerta4_Velo_Bridge
    TheHerta4_Velo_Bridge.unregister()
    blueprint.unregister()

    bpy.utils.unregister_class(HertaUpdatePreference)
    bpy.utils.unregister_class(UpdaterPanel)
    addon_updater_ops.unregister()
    
    bpy.utils.unregister_class(HERTT_OT_SwitchToToolkit)
    bpy.utils.unregister_class(HERTT_OT_SwitchToMainPanel)

    del bpy.types.Scene.herta_show_toolkit

    global_properties.unregister()
    _log_utils.LOG.uninstall_print_hook()
