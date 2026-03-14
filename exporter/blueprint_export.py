import bpy

from ..base.utils.timer_utils import TimerUtils
from ..base.utils.translate_utils import TR
from ..base.utils.command_utils import CommandUtils

from ..base.config.main_config import GlobalConfig, LogicName

from .export_efmi import ExportEFMI

from ..common.export.blueprint_model import BluePrintModel


class SSMTGenerateModBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.generate_mod_blueprint"
    bl_label = TR.translate("生成Mod")
    bl_description = "根据当前工作空间对应的蓝图架构生成对应的Mod文件"
    bl_options = {'REGISTER','UNDO'}

    def execute(self, context):
        TimerUtils.Start("GenerateMod Mod")

        # 1.在这里直接解析蓝图，得到的蓝图对象用于初始化各个游戏对应的导出器
        blueprint_model = BluePrintModel()

        # 2.根据不同的逻辑进行不同的导出器初始化和调用
        if GlobalConfig.logic_name == LogicName.EFMI:
            export_efmi = ExportEFMI(blueprint_model=blueprint_model)
            export_efmi.export()
        else:
            self.report({'ERROR'},"当前游戏预设暂不支持生成Mod")
            return {'FINISHED'}
        
        TimerUtils.End("GenerateMod Mod")
        
        self.report({'INFO'},TR.translate("Generate Mod Success!"))
        CommandUtils.OpenGeneratedModFolder()
        
        return {'FINISHED'}
    
def register():
    bpy.utils.register_class(SSMTGenerateModBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMTGenerateModBlueprint)

