import bpy

from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR
from ..utils.command_utils import CommandUtils

from ..config.main_config import GlobalConfig, LogicName



class SSMTGenerateModBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.generate_mod_blueprint"
    bl_label = TR.translate("生成Mod(蓝图架构)")
    bl_description = "根据当前工作空间对应的蓝图架构生成对应的Mod文件"
    bl_options = {'REGISTER','UNDO'}

    def execute(self, context):
        TimerUtils.Start("GenerateMod Mod")
        # 调用对应游戏的生成Mod逻辑
        if GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.WuWa:
            from ..games.wwmi import ModModelWWMI
            migoto_mod_model = ModModelWWMI()
            migoto_mod_model.generate_unreal_vs_config_ini()
        elif GlobalConfig.logic_name == LogicName.YYSLS:
            from ..games.yysls import ModModelYYSLS
            migoto_mod_model = ModModelYYSLS()
            migoto_mod_model.generate_unity_vs_config_ini()

        elif GlobalConfig.logic_name == LogicName.CTXMC or GlobalConfig.logic_name == LogicName.IdentityV2 or GlobalConfig.logic_name == LogicName.NierR:
            from ..games.identityv import ModModelIdentityV
            migoto_mod_model = ModModelIdentityV()

            migoto_mod_model.generate_unity_vs_config_ini()
        
        # 老米四件套
        elif GlobalConfig.logic_name == LogicName.HIMI:
            from ..games.himi import ModModelHIMI
            migoto_mod_model = ModModelHIMI()
            migoto_mod_model.generate_unity_vs_config_ini()
        elif GlobalConfig.logic_name == LogicName.GIMI:
            from ..games.gimi import ModModelGIMI
            migoto_mod_model = ModModelGIMI()
            migoto_mod_model.generate_unity_vs_config_ini()
        elif GlobalConfig.logic_name == LogicName.SRMI:
            from ..games.srmi import ModModelSRMI
            migoto_mod_model = ModModelSRMI()
            migoto_mod_model.generate_unity_cs_config_ini()
        elif GlobalConfig.logic_name == LogicName.ZZMI:
            from ..games.zzmi import ModModelZZMI
            migoto_mod_model = ModModelZZMI()
            migoto_mod_model.generate_unity_vs_config_ini()

        # 强兼支持
        elif GlobalConfig.logic_name == LogicName.EFMI:
            from ..games.efmi import ModModelEFMI
            migoto_mod_model = ModModelEFMI()
            migoto_mod_model.generate_unity_vs_config_ini()
        
        # UnityVS
        elif GlobalConfig.logic_name == LogicName.UnityVS:
            from ..games.unity import ModModelUnity
            migoto_mod_model = ModModelUnity()
            migoto_mod_model.generate_unity_vs_config_ini()

        # AILIMIT
        elif GlobalConfig.logic_name == LogicName.AILIMIT or GlobalConfig.logic_name == LogicName.UnityCS:
            from ..games.unity import ModModelUnity
            migoto_mod_model = ModModelUnity()
            migoto_mod_model.generate_unity_cs_config_ini()
        
        # UnityCPU 例如少女前线2、虚空之眼等等，绝大部分手游都是UnityCPU
        elif GlobalConfig.logic_name == LogicName.UnityCPU:
            from ..games.unity import ModModelUnity
            migoto_mod_model = ModModelUnity()
            migoto_mod_model.generate_unity_vs_config_ini()
        
        # UnityCSM
        elif GlobalConfig.logic_name == LogicName.UnityCSM:
            from ..games.unity import ModModelUnity
            migoto_mod_model = ModModelUnity()
            migoto_mod_model.generate_unity_cs_config_ini()

        # 尘白禁区、卡拉比丘
        elif GlobalConfig.logic_name == LogicName.SnowBreak:
            from ..games.snowbreak import ModModelSnowBreak
            migoto_mod_model = ModModelSnowBreak()
            migoto_mod_model.generate_ini()
        else:
            self.report({'ERROR'},"当前逻辑暂不支持生成Mod")
            return {'FINISHED'}
        
        self.report({'INFO'},TR.translate("Generate Mod Success!"))
        TimerUtils.End("GenerateMod Mod")

        CommandUtils.OpenGeneratedModFolder()
        
        return {'FINISHED'}
    
def register():
    bpy.utils.register_class(SSMTGenerateModBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMTGenerateModBlueprint)

