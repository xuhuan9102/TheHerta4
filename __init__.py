import bpy

# 全局配置
from .common.global_properties import register as register_global_properties, unregister as unregister_global_properties

# UI界面
from .ui import ui_panel_basic
from .ui import ui_panel_model
from .ui import ui_panel_sword
from .ui import ui_func_import
from .ui import ui_panel_fast_texture

from .common.blueprint_node_base import register as register_blueprint_node_base, unregister as unregister_blueprint_node_base
from .common.blueprint_node_menu import register as register_blueprint_node_menu, unregister as unregister_blueprint_node_menu
from .common.blueprint_node_obj import register as register_blueprint_node_obj, unregister as unregister_blueprint_node_obj
from .common.blueprint_node_shapekey import register as register_blueprint_node_shapekey, unregister as unregister_blueprint_node_shapekey

from .ui import ui_func_export

# 自动更新功能
from . import addon_updater_ops

# 开发时确保同时自动更新 addon_updater_ops
import importlib
importlib.reload(addon_updater_ops)

bl_info = {
    "name": "TheHerta4",
    "description": "Blender Plugin of SSMT4",
    "blender": (4, 5, 0),
    "version": (4, 0, 4),
    "location": "View3D",
    "category": "Generic"
}


class UpdaterPanel(bpy.types.Panel):
    """Update Panel"""
    bl_label = "检查版本更新"
    bl_idname = "HERTA_PT_UpdaterPanel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_context = "objectmode"
    bl_category = "TheHerta4"
    bl_order = 99
    bl_options = {'DEFAULT_CLOSED'}

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
    # 1. Configs
    register_global_properties()
    
    # 2. Addon Updater (local classes)
    addon_updater_ops.register(bl_info)
    bpy.utils.register_class(UpdaterPanel)
    bpy.utils.register_class(HertaUpdatePreference)

    # 3. UI Panels & Logic
    register_blueprint_node_base()
    ui_panel_basic.register()
    ui_panel_model.register()
    ui_panel_sword.register()
    ui_func_import.register()
    ui_panel_fast_texture.register()

    # 蓝图系统
    register_blueprint_node_obj()
    ui_func_export.register()
    register_blueprint_node_menu()
    register_blueprint_node_shapekey()



def unregister():
    # 蓝图系统
    unregister_blueprint_node_obj()
    ui_func_export.unregister()
    unregister_blueprint_node_menu()
    unregister_blueprint_node_shapekey()
    unregister_blueprint_node_base()

    ui_panel_fast_texture.unregister()
    ui_func_import.unregister()
    ui_panel_sword.unregister()
    ui_panel_model.unregister()
    ui_panel_basic.unregister()

    # 2. Addon Updater (local classes)
    bpy.utils.unregister_class(HertaUpdatePreference)
    bpy.utils.unregister_class(UpdaterPanel)
    addon_updater_ops.unregister()

    # 1. Configs
    unregister_global_properties()




