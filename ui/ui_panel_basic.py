'''
基础信息面板
'''
import bpy

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName

from ..utils.translate_utils import TR

from .ui_func_import_ssmt import SSMT4ImportAllFromCurrentWorkSpaceBlueprint, SSMT4ImportRaw

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


class PanelBasicInformation(bpy.types.Panel):
    '''
    基础信息面板
    '''
    bl_label = "基础信息"
    bl_idname = "VIEW3D_PT_SSMT4_Basic_Information"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'

    @classmethod
    def poll(cls, context):
        return not getattr(context.scene, 'herta_show_toolkit', False)

    def draw(self, context):
        layout = self.layout
        
        GlobalConfig.read_from_main_json_ssmt4()

        layout.label(text=TR.translate("SSMT缓存文件夹路径: ") + GlobalConfig.ssmtlocation)
        layout.label(text=TR.translate("当前配置名称: ") + GlobalConfig.gamename)
        layout.label(text=TR.translate("当前游戏预设: ") + GlobalConfig.logic_name)
        layout.label(text=TR.translate("当前工作空间: ") + GlobalConfig.workspacename)
        
        if len(context.selected_objects) != 0:
            obj = context.selected_objects[0]

            gametypename = obj.get("3DMigoto:GameTypeName", "")
            recalculate_tangent = obj.get("3DMigoto:RecalculateTANGENT", False)
            recalculate_color = obj.get("3DMigoto:RecalculateCOLOR", False)

            layout.label(text="GameType: " + gametypename)
            layout.label(text="RecalculateTANGENT: " + str(recalculate_tangent))
            layout.label(text="RecalculateCOLOR: " + str(recalculate_color))
            
        layout.operator("theherta3.open_persistent_blueprint", icon='NODETREE')
        
        layout.operator("import_mesh.migoto_raw_buffers_mmt", text="导入.fmt .ib .vb格式模型", icon='IMPORT')

        layout.operator(SSMT4ImportRaw.bl_idname, text="导入SSMT格式模型", icon='IMPORT')

        layout.separator()

        layout.operator(SSMT4ImportAllFromCurrentWorkSpaceBlueprint.bl_idname, text="一键导入SSMT工作空间内容", icon='IMPORT')

        layout.separator()

        cache_box = layout.box()
        cache_box.label(text="前处理缓存", icon='FILE_CACHE')
        cache_box.prop(context.scene.global_properties, "enable_preprocess_cache")

        cache_stats = PreProcessCache.get_cache_stats()
        file_count = cache_stats["file_count"]
        total_size = cache_stats["total_size"]
        size_str = PreProcessCache.format_size(total_size)
        cache_box.label(text=f"缓存文件: {file_count} 个, 大小: {size_str}")

        row = cache_box.row()
        row.operator(SSMT_OT_ClearPreprocessCache.bl_idname, icon='TRASH')

        parallel_box = layout.box()
        parallel_box.label(text="并行处理", icon='SYSTEM')
        parallel_box.prop(context.scene.global_properties, "enable_parallel_preprocess")
        parallel_box.prop(context.scene.global_properties, "enable_parallel_export_rounds")

        if context.scene.global_properties.enable_parallel_preprocess or context.scene.global_properties.enable_parallel_export_rounds:
            parallel_box.prop(context.scene.global_properties, "parallel_blender_executable")
            parallel_box.prop(context.scene.global_properties, "parallel_preprocess_instances")
            parallel_box.prop(context.scene.global_properties, "parallel_preprocess_timeout_seconds")
            parallel_box.prop(context.scene.global_properties, "parallel_preprocess_keep_temp_files")

            effective_path = ParallelPreprocessCoordinator.get_effective_blender_executable()
            is_valid, message = ParallelPreprocessCoordinator.get_validation_summary()

            parallel_box.label(text=f"当前生效路径: {effective_path or '未设置'}")
            parallel_box.label(text=message, icon='CHECKMARK' if is_valid else 'ERROR')
        
        layout.separator()
        
        layout.prop(context.scene, "herta_show_toolkit", text="工具集模式", icon='TOOL_SETTINGS')
        if context.scene.herta_show_toolkit:
            layout.operator("model.switch_to_main_panel", text="返回主面板", icon='BACK')

        if GlobalConfig.logic_name == LogicName.WWMI:
            layout.prop(context.scene.global_properties,"import_merged_vgmap")
            layout.prop(context.scene.global_properties,"import_skip_empty_vertex_groups")


def register():
    bpy.utils.register_class(SSMT_OT_ClearPreprocessCache)
    bpy.utils.register_class(PanelBasicInformation)

def unregister():
    bpy.utils.unregister_class(PanelBasicInformation)
    bpy.utils.unregister_class(SSMT_OT_ClearPreprocessCache)
