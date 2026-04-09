import bpy


from .common import global_properties


# UI界面
from .ui import ui_panel_basic
from .ui import ui_panel_sword
from .ui import ui_func_import
from .ui import ui_func_import_ssmt

from .blueprint import node_obj as blueprint_node_obj
from .blueprint import node_base as blueprint_node_base
from .blueprint import node_menu as blueprint_node_menu
from .blueprint import node_shapekey as blueprint_node_shapekey
from .blueprint import node_rename as blueprint_node_rename
from .blueprint import sync as blueprint_sync

from .ui import ui_func_export

# Toolkit 工具集
from . import toolkit

# 自动更新功能
from . import addon_updater_ops

# 开发时确保同时自动更新所有模块
import importlib
importlib.reload(addon_updater_ops)
importlib.reload(global_properties)
importlib.reload(blueprint_node_base)
importlib.reload(blueprint_node_obj)
importlib.reload(blueprint_node_menu)
importlib.reload(blueprint_node_shapekey)
importlib.reload(blueprint_sync)

bl_info = {
    "name": "TheHerta4",
    "description": "Blender Plugin of SSMT4",
    "blender": (4, 5, 0),
    "version": (4, 0, 5),
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
    bl_context = "objectmode"
    bl_category = "TheHerta4"
    bl_order = 99
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return not getattr(context.scene, 'herta_show_toolkit', False)

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
    global_properties.register()
    
    # 注册 toolkit 切换属性
    bpy.types.Scene.herta_show_toolkit = bpy.props.BoolProperty(
        name="显示工具集",
        description="切换显示工具集面板",
        default=False
    )
    
    # 注册切换操作符
    bpy.utils.register_class(HERTT_OT_SwitchToMainPanel)
    bpy.utils.register_class(HERTT_OT_SwitchToToolkit)
    
    # 2. Addon Updater (local classes)
    addon_updater_ops.register(bl_info)
    bpy.utils.register_class(UpdaterPanel)
    bpy.utils.register_class(HertaUpdatePreference)

    # 3. UI Panels & Logic
    blueprint_node_base.register()
    ui_panel_basic.register()
    ui_panel_sword.register()
    ui_func_import_ssmt.register()
    ui_func_import.register()

    # 蓝图系统
    blueprint_node_obj.register()
    ui_func_export.register()
    blueprint_node_menu.register()
    blueprint_node_shapekey.register()
    blueprint_node_rename.register()
    blueprint_sync.register()
    
    # Toolkit 工具集
    toolkit.register()



def unregister():
    # Toolkit 工具集
    toolkit.unregister()
    
    # 蓝图系统
    blueprint_sync.unregister()
    blueprint_node_rename.unregister()
    blueprint_node_shapekey.unregister()
    blueprint_node_menu.unregister()
    ui_func_export.unregister()
    blueprint_node_obj.unregister()
    blueprint_node_base.unregister()

    ui_func_import.unregister()
    ui_func_import_ssmt.unregister()
    ui_panel_sword.unregister()
    ui_panel_basic.unregister()

    # 2. Addon Updater (local classes)
    bpy.utils.unregister_class(HertaUpdatePreference)
    bpy.utils.unregister_class(UpdaterPanel)
    addon_updater_ops.unregister()
    
    # 注销切换操作符
    bpy.utils.unregister_class(HERTT_OT_SwitchToToolkit)
    bpy.utils.unregister_class(HERTT_OT_SwitchToMainPanel)
    
    # 删除 toolkit 切换属性
    del bpy.types.Scene.herta_show_toolkit

    # 1. Configs
    global_properties.unregister()




