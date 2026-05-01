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
    _main_settings_cache = {}
    _game_settings_cache = {}
    _main_json_mtime = None
    _game_config_json_mtime = None
    _game_config_json_path = ""

    @classmethod
    def _safe_getmtime(cls, file_path: str):
        try:
            return os.path.getmtime(file_path)
        except OSError:
            return None

    @classmethod
    def _read_json_file(cls, file_path: str):
        with open(file_path, encoding="utf-8") as json_file:
            return json.load(json_file)

    @classmethod
    def _clear_main_settings(cls):
        cls._main_settings_cache = {}
        cls._main_json_mtime = None
        cls.workspacename = ""
        cls.gamename = ""
        cls.ssmtlocation = ""

    @classmethod
    def _clear_game_settings(cls, game_config_json_path: str = ""):
        cls._game_settings_cache = {}
        cls._game_config_json_mtime = None
        cls._game_config_json_path = game_config_json_path
        cls.current_game_migoto_folder = ""
        cls.logic_name = ""

    @classmethod
    def read_from_main_json_ssmt4(cls) :
        try:
            main_json_path = cls.path_main_json_ssmt4()
            main_json_mtime = cls._safe_getmtime(main_json_path)

            if main_json_mtime is None:
                cls._clear_main_settings()
                cls._clear_game_settings()
                return

            if cls._main_json_mtime != main_json_mtime:
                main_setting_json = cls._read_json_file(main_json_path)
                cls._main_settings_cache = main_setting_json
                cls._main_json_mtime = main_json_mtime
                cls.workspacename = main_setting_json.get("CurrentWorkSpace", "")
                cls.gamename = main_setting_json.get("CurrentGameName", "")

                base_folder = (
                    main_setting_json.get("SSMTWorkFolder")
                    or main_setting_json.get("DBMTWorkFolder", "")
                )
                cls.ssmtlocation = base_folder + "\\" if base_folder else ""

            game_config_json_path = os.path.join(
                cls.path_ssmt4_global_configs_folder(),
                "Games\\" + cls.gamename + "\\Config.json",
            )

            if not cls.gamename:
                cls._clear_game_settings(game_config_json_path)
                return

            game_config_json_mtime = cls._safe_getmtime(game_config_json_path)
            if game_config_json_mtime is None:
                cls._clear_game_settings(game_config_json_path)
                return

            if (
                cls._game_config_json_path != game_config_json_path
                or cls._game_config_json_mtime != game_config_json_mtime
            ):
                game_config_json = cls._read_json_file(game_config_json_path)
                cls._game_settings_cache = game_config_json
                cls._game_config_json_path = game_config_json_path
                cls._game_config_json_mtime = game_config_json_mtime
                cls.current_game_migoto_folder = game_config_json.get("installDir", "")
                cls.logic_name = game_config_json.get("gamePreset", "")
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
        cls.read_from_main_json_ssmt4()
        reverse_output_folder = cls._main_settings_cache.get("ReverseOutputFolder", "")
        return reverse_output_folder + "\\" if reverse_output_folder else ""

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
    def _normalize_workspace_folder_path(cls, folder_path: str) -> str:
        normalized = str(folder_path or "").strip()
        if not normalized:
            return ""

        normalized = os.path.normpath(normalized)
        if not normalized.endswith("\\"):
            normalized = normalized + "\\"
        return normalized

    @classmethod
    def get_workspace_name(cls):
        try:
            workspace_source_mode = GlobalProterties.workspace_source_mode()

            if workspace_source_mode == "SPECIFIC":
                specified_workspace_name = GlobalProterties.specific_workspace_name()
                if specified_workspace_name:
                    return specified_workspace_name

            if workspace_source_mode == "CUSTOM":
                custom_workspace_folder_path = cls._normalize_workspace_folder_path(
                    GlobalProterties.custom_workspace_folder_path()
                )
                if custom_workspace_folder_path:
                    return os.path.basename(custom_workspace_folder_path.rstrip("\\/"))
        except Exception:
            pass

        return cls.workspacename
    
    @classmethod
    def path_workspace_folder(cls):
        try:
            if GlobalProterties.workspace_source_mode() == "CUSTOM":
                custom_workspace_folder_path = cls._normalize_workspace_folder_path(
                    GlobalProterties.custom_workspace_folder_path()
                )
                if custom_workspace_folder_path:
                    return custom_workspace_folder_path
                return ""
        except Exception:
            pass

        return os.path.join(GlobalConfig.path_current_game_total_workspace_folder(), cls.get_workspace_name() + "\\")
    
    @classmethod
    def path_generate_mod_folder(cls):
        # 如果用户勾选了使用指定文件夹，那就返回指定文件夹位置，否则返回我们的默认位置。
        # 但是这里有个问题就是SkipIB和VSCheck不会生成在指定位置。
        if GlobalProterties.use_specific_generate_mod_folder_path():
            return GlobalProterties.generate_mod_folder_path()
        else:
            # 确保用的时候直接拿到的就是已经存在的目录
            ssmt_generated_mod_folder_path = os.path.join(GlobalConfig.path_mods_folder(),"SSMTGeneratedMod\\")
            generate_mod_folder_path = os.path.join(ssmt_generated_mod_folder_path, cls.get_workspace_name() + "\\")
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
    



    
