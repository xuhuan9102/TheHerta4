import bpy

from ..base.utils.timer_utils import TimerUtils
from ..base.utils.translate_utils import TR
from ..base.utils.command_utils import CommandUtils

from ..base.config.main_config import GlobalConfig, LogicName

from ..games.efmi import ExportEFMI
from ..games.gimi import ExportGIMI
from ..games.himi import ExportHIMI
from ..games.identityv import ExportIdentityV
from ..games.snowbreak import ExportSnowBreak
from ..games.srmi import ExportSRMI
from ..games.unity import ExportUnity
from ..games.wwmi import ExportWWMI
from ..games.yysls import ExportYYSLS
from ..games.zzmi import ExportZZMI

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
        elif GlobalConfig.logic_name == LogicName.GIMI:
            export_gimi = ExportGIMI(blueprint_model=blueprint_model)
            export_gimi.export()
        elif GlobalConfig.logic_name == LogicName.HIMI:
            export_himi = ExportHIMI(blueprint_model=blueprint_model)
            export_himi.export()
        elif GlobalConfig.logic_name == LogicName.IdentityVNeoX2 or GlobalConfig.logic_name == LogicName.IdentityVNeoX3:
            export_identityv = ExportIdentityV(blueprint_model=blueprint_model)
            export_identityv.export()
        elif GlobalConfig.logic_name == LogicName.SRMI:
            export_srmi = ExportSRMI(blueprint_model=blueprint_model)
            export_srmi.export()
        elif GlobalConfig.logic_name == LogicName.ZZMI:
            export_zzmi = ExportZZMI(blueprint_model=blueprint_model)
            export_zzmi.export()
        elif GlobalConfig.logic_name == LogicName.WWMI:
            export_wwmi = ExportWWMI(blueprint_model=blueprint_model)
            export_wwmi.export()
        elif GlobalConfig.logic_name == LogicName.SnowBreak:
            export_snowbreak = ExportSnowBreak(blueprint_model=blueprint_model)
            export_snowbreak.export()
        elif GlobalConfig.logic_name == LogicName.YYSLS:
            export_yysls = ExportYYSLS(blueprint_model=blueprint_model)
            export_yysls.export()
        elif GlobalConfig.logic_name == LogicName.Naraka or GlobalConfig.logic_name == LogicName.NarakaM or GlobalConfig.logic_name == LogicName.GF2 or GlobalConfig.logic_name == LogicName.AILIMIT:
            export_unity = ExportUnity(blueprint_model=blueprint_model)
            export_unity.export()
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

