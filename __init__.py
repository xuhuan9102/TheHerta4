import bpy


from .common import global_properties
from .common.global_config import GlobalConfig


from .ui import ui_panel_basic
from .ui import ui_panel_sword
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

_global_config_timer_handle = None
_GLOBAL_CONFIG_REFRESH_INTERVAL = 1.0

def _global_config_refresh_timer_callback():
    global _global_config_timer_handle
    try:
        GlobalConfig.read_from_main_json_ssmt4()
    except Exception:
        pass
    return _GLOBAL_CONFIG_REFRESH_INTERVAL

bl_info = {
    "name": "TheHerta4",
    "description": "Blender Plugin of SSMT4",
    "blender": (4, 5, 0),
    "version": (4, 1, 2),
    "location": "View3D",
    "category": "Generic"
}


class HERTT_OT_SwitchToMainPanel(bpy.types.Operator):
    """切换回主面板"""
    bl_idname = "model.switch_to_main_panel"
    bl_label = "切换回主面板"
    
    def execute(self, context):
        context.scene.herta_show_toolkit = False
        return {'FINISHED'}


class HERTT_OT_SwitchToToolkit(bpy.types.Operator):
    """切换到工具集面板"""
    bl_idname = "model.switch_to_toolkit"
    bl_label = "切换到工具集面板"
    
    def execute(self, context):
        context.scene.herta_show_toolkit = True
        return {'FINISHED'}


class UpdaterPanel(bpy.types.Panel):
    """Update Panel"""
    bl_label = "检查版本更新"
    bl_idname = "HERTA_PT_UpdaterPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "TheHerta4"
    bl_order = 99
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        if not hasattr(context.scene, 'herta_show_toolkit'):
            return True
        return not context.scene.herta_show_toolkit

    def draw(self, context):
        layout = self.layout
        
        # Call to check for update in background.
        # Note: built-in checks ensure it runs at most once, and will run in
        # the background thread, not blocking or hanging blender.
        # Internally also checks to see if auto-check enabled and if the time
        # interval has passed.
        # addon_updater_ops.check_for_update_background()
        col = layout.column()
        col.scale_y = 0.7
        # Could also use your own custom drawing based on shared variables.
        if addon_updater_ops.updater.update_ready:
            layout.label(text="存在可用更新！", icon="INFO")

        # Call built-in function with draw code/checks.
        # addon_updater_ops.update_notice_box_ui(self, context)
        addon_updater_ops.update_settings_ui(self, context)


class HertaUpdatePreference(bpy.types.AddonPreferences):
    # Addon updater preferences.
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
        layout = self.layout
        layout.prop(self, "自动检查更新")
        addon_updater_ops.update_settings_ui(self, context)

def register():
    global _global_config_timer_handle
    
    global_properties.register()
    
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
    ui_prefix_quick_ops.register()
    ui_panel_basic.register()
    ui_panel_sword.register()
    ui_func_import_ssmt.register()
    ui_func_import.register()
    ui_func_export.register()
    
    toolkit.register()
    
    _global_config_timer_handle = bpy.app.timers.register(
        _global_config_refresh_timer_callback, 
        persistent=True
    )



def unregister():
    global _global_config_timer_handle
    
    if _global_config_timer_handle and bpy.app.timers.is_registered(_global_config_timer_handle):
        bpy.app.timers.unregister(_global_config_timer_handle)
    
    toolkit.unregister()
    
    ui_func_export.unregister()
    ui_func_import.unregister()
    ui_func_import_ssmt.unregister()
    ui_panel_sword.unregister()
    ui_panel_basic.unregister()
    ui_prefix_quick_ops.unregister()
    blueprint.unregister()

    bpy.utils.unregister_class(HertaUpdatePreference)
    bpy.utils.unregister_class(UpdaterPanel)
    addon_updater_ops.unregister()
    
    bpy.utils.unregister_class(HERTT_OT_SwitchToToolkit)
    bpy.utils.unregister_class(HERTT_OT_SwitchToMainPanel)
    
    del bpy.types.Scene.herta_show_toolkit

    global_properties.unregister()




