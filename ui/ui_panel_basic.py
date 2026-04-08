'''
基础信息面板
'''
import bpy

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName

from ..utils.translate_utils import TR

from .ui_func_import_ssmt import SSMT4ImportAllFromCurrentWorkSpaceBlueprint, SSMT4ImportRaw


class PanelBasicInformation(bpy.types.Panel):
    '''
    基础信息面板
    此面板实时刷新并读取全局配置文件中的路径
    '''
    bl_label = TR.translate("基础信息面板")
    bl_idname = "VIEW3D_PT_CATTER_Buttons_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'

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

        if GlobalConfig.logic_name == LogicName.WWMI:
            layout.prop(context.scene.global_properties,"import_merged_vgmap")
            layout.prop(context.scene.global_properties,"import_skip_empty_vertex_groups")


def register():
    bpy.utils.register_class(PanelBasicInformation)

def unregister():
    bpy.utils.unregister_class(PanelBasicInformation)