import bpy

from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR
from ..utils.command_utils import CommandUtils

from ..config.main_config import GlobalConfig, LogicName

from .export_efmi import ExportEFMI


class SSMTGenerateModBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.generate_mod_blueprint"
    bl_label = TR.translate("生成Mod")
    bl_description = "根据当前工作空间对应的蓝图架构生成对应的Mod文件"
    bl_options = {'REGISTER','UNDO'}

    def execute(self, context):
        TimerUtils.Start("GenerateMod Mod")
       
        if GlobalConfig.logic_name == LogicName.EFMI:
            export_efmi = ExportEFMI()
            export_efmi.export()
        else:
            self.report({'ERROR'},"当前逻辑暂不支持生成Mod")
            return {'FINISHED'}
        
        TimerUtils.End("GenerateMod Mod")
        
        self.report({'INFO'},TR.translate("Generate Mod Success!"))
        CommandUtils.OpenGeneratedModFolder()
        
        return {'FINISHED'}
    
def register():
    bpy.utils.register_class(SSMTGenerateModBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMTGenerateModBlueprint)

