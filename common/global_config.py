import bpy
import os
import json


from .global_properties import GlobalProterties


# 全局配置类，使用字段默认为全局可访问的唯一静态变量的特性，来实现全局变量
class GlobalConfig:
    # 全局静态变量,任何地方访问到的值都是唯一的
    gamename = ""
    workspacename = ""
    ssmtlocation = ""
    current_game_migoto_folder = ""
    logic_name = ""

    @classmethod
    def read_from_main_json_ssmt4(cls) :
        try:
            main_json_path = GlobalConfig.path_main_json_ssmt4()
            # 先从main_json_path里读取ssmt位置，也就是ssmt总工作空间的位置
            # 在新架构中，总工作空间位置已不会再发生改变，所以用户只需要选择一次就可以了
            if os.path.exists(main_json_path):
                main_setting_file = open(main_json_path)
                main_setting_json = json.load(main_setting_file)
                main_setting_file.close()
                cls.workspacename = main_setting_json.get("CurrentWorkSpace","")
                cls.gamename = main_setting_json.get("CurrentGameName","")
                cls.ssmtlocation = (
                    main_setting_json.get("SSMTWorkFolder")
                    or main_setting_json.get("DBMTWorkFolder", "")
                ) + "\\" # 理论上应该绞杀所有旧时代孑遗, 然考虑到兼容性, 不得不保留 fallback.
            else:
                print("Can't find: " + main_json_path)
            
            game_config_json_path = os.path.join(GlobalConfig.path_ssmt4_global_configs_folder(),"Games\\" + cls.gamename + "\\Config.json")
            if os.path.exists(game_config_json_path):
                game_config_json_file = open(game_config_json_path)
                game_config_json = json.load(game_config_json_file)
                game_config_json_file.close()

                cls.current_game_migoto_folder = game_config_json.get("installDir","")
                cls.logic_name = game_config_json.get("gamePreset","")
        except Exception as e:
            print(e)
            
    @classmethod
    def base_path(cls):
        return cls.ssmtlocation
    
    @classmethod
    def path_drawib_config_json_path(cls):
        '''
        当前工作空间目录下的Config.json
        存储了所有的DrawIB和别名
        '''
        game_config_json_path = os.path.join(GlobalConfig.path_workspace_folder(),"Config.json")
        return game_config_json_path
    
    @classmethod
    def path_configs_folder(cls):
        return os.path.join(GlobalConfig.base_path(),"Configs\\")
    
    @classmethod
    def path_reverse_output_folder(cls):
        # 先从main_json_path里读取ssmt位置，也就是ssmt总工作空间的位置
        # 在新架构中，总工作空间位置已不会再发生改变，所以用户只需要选择一次就可以了
        if os.path.exists(cls.path_main_json_ssmt4()):
            main_setting_file = open(cls.path_main_json_ssmt4())
            main_setting_json = json.load(main_setting_file)
            main_setting_file.close()
            reverse_output_folder = main_setting_json.get("ReverseOutputFolder","") + "\\"
            
            print(reverse_output_folder)
            return reverse_output_folder
        else:
            return ""

    @classmethod
    def path_mods_folder(cls):
        return os.path.join(cls.current_game_migoto_folder,"Mods\\") 

    @classmethod
    def path_total_workspace_folder(cls):
        return os.path.join(GlobalConfig.base_path(),"WorkSpace\\") 
    
    @classmethod
    def path_current_game_total_workspace_folder(cls):
        return os.path.join(GlobalConfig.path_total_workspace_folder(),GlobalConfig.gamename + "\\") 
    
    @classmethod
    def path_workspace_folder(cls):
        return os.path.join(GlobalConfig.path_current_game_total_workspace_folder(), GlobalConfig.workspacename + "\\")
    
    @classmethod
    def path_generate_mod_folder(cls):
        # 如果用户勾选了使用指定文件夹，那就返回指定文件夹位置，否则返回我们的默认位置。
        # 但是这里有个问题就是SkipIB和VSCheck不会生成在指定位置。
        if GlobalProterties.use_specific_generate_mod_folder_path():
            return GlobalProterties.generate_mod_folder_path()
        else:
            # 确保用的时候直接拿到的就是已经存在的目录
            ssmt_generated_mod_folder_path = os.path.join(GlobalConfig.path_mods_folder(),"SSMTGeneratedMod\\")
            generate_mod_folder_path = os.path.join(ssmt_generated_mod_folder_path, GlobalConfig.workspacename + "\\")
            if not os.path.exists(generate_mod_folder_path):
                os.makedirs(generate_mod_folder_path)
            return generate_mod_folder_path
    
    @classmethod
    def path_extract_gametype_folder(cls,draw_ib:str,gametype_name:str):
        return os.path.join(GlobalConfig.path_workspace_folder(), draw_ib + "\\TYPE_" + gametype_name + "\\")
    
    @classmethod
    def path_generatemod_buffer_folder(cls):
        from ..blueprint.export_helper import BlueprintExportHelper
        buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
        buffer_path = os.path.join(GlobalConfig.path_generate_mod_folder(), buffer_folder_name + "\\")
        if not os.path.exists(buffer_path):
            os.makedirs(buffer_path)
        return buffer_path
    
    @classmethod
    def path_generatemod_texture_folder(cls,draw_ib:str):

        texture_path = os.path.join(GlobalConfig.path_generate_mod_folder(),"Textures\\")
        if not os.path.exists(texture_path):
            os.makedirs(texture_path)
            print("GlobalConfig: 已创建贴图输出目录: " + texture_path + " (DrawIB: " + str(draw_ib) + ")")
        else:
            print("GlobalConfig: 使用已有贴图输出目录: " + texture_path + " (DrawIB: " + str(draw_ib) + ")")
        return texture_path
    
    @classmethod
    def path_appdata_local(cls):
        return os.path.join(os.environ['LOCALAPPDATA'])
    
    @classmethod
    def path_ssmt4_global_configs_folder(cls):
        return os.path.join(GlobalConfig.path_appdata_local(),"SSMT4GlobalConfigs\\")

    # 定义基础的Json文件路径
    @classmethod
    def path_main_json_ssmt4(cls):
        return os.path.join(GlobalConfig.path_ssmt4_global_configs_folder(), "settings.json")
    



    
