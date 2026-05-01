'''
基础信息面板。
'''
import bpy
import os

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..blueprint.export_helper import BlueprintExportHelper

from ..utils.translate_utils import TR

from .ui_func_import_ssmt import SSMT4ImportAllFromCurrentWorkSpaceBlueprint, SSMT4ImportRaw
from . import ui_prefix_quick_ops
from .ui_func_export import SSMTGenerateModBlueprint, SSMTQuickExportSelected

from ..blueprint.preprocess_cache import PreProcessCache
from ..blueprint.preprocess_parallel import ParallelPreprocessCoordinator


class SSMT_OT_ClearPreprocessCache(bpy.types.Operator):
    bl_idname = "ssmt.clear_preprocess_cache"
    bl_label = "清空前处理缓存"
    bl_description = "清空所有前处理缓存文件"

    def execute(self, context):
        cleared_count = PreProcessCache.clear_cache()
        self.report({'INFO'}, f"已清空 {cleared_count} 个缓存文件")
        return {'FINISHED'}


class SSMT4RefreshWorkspaceList(bpy.types.Operator):
    bl_idname = "ssmt4.refresh_workspace_list"
    bl_label = "刷新工作空间列表"
    bl_description = "刷新当前游戏配置下的工作空间列表"

    def execute(self, context):
        GlobalConfig.read_from_main_json_ssmt4()

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        self.report({'INFO'}, "已刷新工作空间列表")
        return {'FINISHED'}


class PanelBasicInformation(bpy.types.Panel):
    bl_label = "基础信息"
    bl_idname = "VIEW3D_PT_SSMT4_Basic_Information"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_order = 0

    @classmethod
    def poll(cls, context):
        if not hasattr(context.scene, 'herta_show_toolkit'):
            return True
        return not context.scene.herta_show_toolkit

    def draw(self, context):
        layout = self.layout
        global_properties = context.scene.global_properties

        GlobalConfig.read_from_main_json_ssmt4()

        preferred_blueprint_name = BlueprintExportHelper.get_preferred_blueprint_name(
            selected_name=getattr(global_properties, "selected_blueprint_name", ""),
            context=context,
        )
        if preferred_blueprint_name and global_properties.selected_blueprint_name != preferred_blueprint_name:
            global_properties.selected_blueprint_name = preferred_blueprint_name
        elif not preferred_blueprint_name and global_properties.selected_blueprint_name != "__NONE__":
            global_properties.selected_blueprint_name = "__NONE__"

        layout.label(text="TheHerta4 v4.2.5", icon='INFO')
        layout.label(text=TR.translate("SSMT缓存文件夹路径: ") + GlobalConfig.ssmtlocation)
        layout.label(text=TR.translate("当前配置名称: ") + GlobalConfig.gamename)
        layout.label(text=TR.translate("当前游戏预设: ") + GlobalConfig.logic_name)
        layout.label(text=TR.translate("当前工作空间: ") + GlobalConfig.get_workspace_name())

        if len(context.selected_objects) != 0:
            obj = context.selected_objects[0]

            gametypename = obj.get("3DMigoto:GameTypeName", "")
            recalculate_tangent = obj.get("3DMigoto:RecalculateTANGENT", False)
            recalculate_color = obj.get("3DMigoto:RecalculateCOLOR", False)

            layout.label(text="GameType: " + gametypename)
            layout.label(text="RecalculateTANGENT: " + str(recalculate_tangent))
            layout.label(text="RecalculateCOLOR: " + str(recalculate_color))

        layout.prop(context.scene, "herta_show_toolkit", text="工具集模式", icon='TOOL_SETTINGS')
        if context.scene.herta_show_toolkit:
            layout.operator("model.switch_to_main_panel", text="返回主面板", icon='BACK')

        layout.prop(global_properties, "enable_non_mirror_workflow", text="非镜像工作流", toggle=True)

        workspace_box = layout.box()
        workspace_box.label(text="工作空间来源", icon='FILE_FOLDER')
        workspace_box.prop(global_properties, "workspace_source_mode")
        if global_properties.workspace_source_mode == "SPECIFIC":
            workspace_row = workspace_box.row(align=True)
            workspace_row.prop(global_properties, "specific_workspace_name", text="指定工作空间")
            workspace_row.operator(SSMT4RefreshWorkspaceList.bl_idname, text="", icon='FILE_REFRESH')
        elif global_properties.workspace_source_mode == "CUSTOM":
            workspace_box.prop(global_properties, "custom_workspace_folder_path", text="自定义目录")

        layout.separator()

        blueprint_box = layout.box()
        blueprint_box.label(text="蓝图", icon='NODETREE')

        blueprint_row = blueprint_box.row(align=True)
        blueprint_row.prop(global_properties, "selected_blueprint_name", text="SSMT蓝图")

        rename_operator = blueprint_row.operator(
            "theherta3.rename_persistent_blueprint",
            text="",
            icon='GREASEPENCIL',
        )
        rename_operator.blueprint_name = preferred_blueprint_name or global_properties.selected_blueprint_name

        delete_operator = blueprint_row.operator(
            "theherta3.delete_persistent_blueprint",
            text="",
            icon='TRASH',
        )
        delete_operator.blueprint_name = preferred_blueprint_name or global_properties.selected_blueprint_name

        open_operator = blueprint_row.operator(
            "theherta3.open_persistent_blueprint",
            text="",
            icon='NODETREE',
        )
        open_operator.blueprint_name = preferred_blueprint_name

        open_current = blueprint_box.operator(
            "theherta3.open_persistent_blueprint",
            text="打开蓝图界面",
            icon='NODETREE',
        )
        open_current.blueprint_name = preferred_blueprint_name

        generate_operator = blueprint_box.operator(
            SSMTGenerateModBlueprint.bl_idname,
            text="生成所选蓝图 Mod",
            icon='EXPORT',
        )
        generate_operator.blueprint_name = preferred_blueprint_name or global_properties.selected_blueprint_name

        layout.separator()

        layout.operator(SSMTQuickExportSelected.bl_idname, text="快速局部导出", icon='EXPORT')

        import_row = layout.row(align=True)
        import_row.operator(SSMT4ImportAllFromCurrentWorkSpaceBlueprint.bl_idname, text="一键导入SSMT工作空间内容", icon='IMPORT')
        import_row.prop(
            global_properties,
            "expand_import_quick_tools",
            text="",
            icon='TRIA_DOWN' if global_properties.expand_import_quick_tools else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )

        if global_properties.expand_import_quick_tools:
            import_box = layout.box()
            import_box.operator("import_mesh.migoto_raw_buffers_mmt", text="导入FMT格式模型", icon='IMPORT')
            import_box.operator(SSMT4ImportRaw.bl_idname, text="导入SSMT格式模型", icon='IMPORT')
            import_box.prop(global_properties, "use_normal_map", text="自动上贴图时使用法线贴图")

        ui_prefix_quick_ops.draw_prefix_quick_section(layout, context)

        layout.separator()

        cache_box = layout.box()
        cache_header = cache_box.row(align=True)
        cache_header.prop(
            global_properties,
            "expand_preprocess_cache",
            text="",
            icon='TRIA_DOWN' if global_properties.expand_preprocess_cache else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )
        cache_header.label(text="前处理缓存", icon='FILE_CACHE')

        if global_properties.expand_preprocess_cache:
            cache_box.prop(global_properties, "enable_preprocess_cache")

            cache_stats = PreProcessCache.get_cache_stats()
            file_count = cache_stats["file_count"]
            total_size = cache_stats["total_size"]
            size_str = PreProcessCache.format_size(total_size)
            cache_box.label(text=f"缓存文件: {file_count} 个 大小: {size_str}")

            row = cache_box.row()
            row.operator(SSMT_OT_ClearPreprocessCache.bl_idname, icon='TRASH')

        parallel_box = layout.box()
        parallel_header = parallel_box.row(align=True)
        parallel_header.prop(
            global_properties,
            "expand_parallel_processing",
            text="",
            icon='TRIA_DOWN' if global_properties.expand_parallel_processing else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )
        parallel_header.label(text="并行处理", icon='SYSTEM')

        if global_properties.expand_parallel_processing:
            parallel_box.prop(global_properties, "enable_parallel_preprocess")
            parallel_box.prop(global_properties, "enable_parallel_export_rounds")

            if global_properties.enable_parallel_preprocess or global_properties.enable_parallel_export_rounds:
                parallel_box.prop(global_properties, "parallel_blender_executable")
                parallel_box.prop(global_properties, "parallel_preprocess_instances")
                parallel_box.prop(global_properties, "parallel_preprocess_timeout_seconds")
                parallel_box.prop(global_properties, "parallel_preprocess_keep_temp_files")

                effective_path = ParallelPreprocessCoordinator.get_effective_blender_executable()
                display_path = os.path.basename(effective_path) if effective_path else "未设置"
                is_valid, message = ParallelPreprocessCoordinator.get_validation_summary()

                parallel_box.label(text=f"当前生效路径: {display_path}")
                parallel_box.label(text=message, icon='CHECKMARK' if is_valid else 'ERROR')

        if GlobalConfig.logic_name == LogicName.WWMI:
            layout.prop(global_properties, "import_merged_vgmap")

        if GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
            layout.prop(global_properties, "import_skip_empty_vertex_groups")


def register():
    bpy.utils.register_class(SSMT_OT_ClearPreprocessCache)
    bpy.utils.register_class(SSMT4RefreshWorkspaceList)
    bpy.utils.register_class(PanelBasicInformation)


def unregister():
    bpy.utils.unregister_class(PanelBasicInformation)
    bpy.utils.unregister_class(SSMT4RefreshWorkspaceList)
    bpy.utils.unregister_class(SSMT_OT_ClearPreprocessCache)
