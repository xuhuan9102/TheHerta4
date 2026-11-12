import bpy
import os
import re
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
    # 本次蓝图导出正在使用的蓝图名，仅用于解析输出目录；
    # 由导出入口（ui.SSMTGenerateModBlueprint）set，并在 finally 里还原，见 set_output_blueprint_tree
    _output_blueprint_tree_name = ""

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
                    from .global_properties import resolve_workspace_safe_identifier
                    return resolve_workspace_safe_identifier(specified_workspace_name)

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
    def _sanitize_generated_mod_name(cls, name: str) -> str:
        """清理将用作 Mod 目录名和 INI 文件名的蓝图名。"""
        return re.sub(r'[\\/:*?"<>|]+', "_", str(name or "")).strip()

    @classmethod
    def get_generate_mod_name(cls, tree=None):
        """返回本次导出的 Mod 名称，供输出目录和主 INI 文件名共用。"""
        output_tree = tree if tree is not None else cls._get_output_blueprint_tree()
        if (
            output_tree is not None
            and getattr(output_tree, "use_blueprint_name_generate_mod_folder", False)
        ):
            blueprint_name = cls._sanitize_generated_mod_name(
                getattr(output_tree, "name", "")
            )
            if blueprint_name:
                return blueprint_name
        return cls.get_workspace_name()

    @classmethod
    def path_generate_mod_ini(cls, suffix: str = ""):
        """返回本次导出的主 INI 路径；suffix 用于 WWMI 的 DrawIB 后缀。"""
        return os.path.join(
            cls.path_generate_mod_folder(),
            cls.get_generate_mod_name() + str(suffix or "") + ".ini",
        )
    
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
    def set_output_blueprint_tree(cls, tree):
        """记录本次导出使用的蓝图，仅用于解析输出目录，不影响导出目标蓝图

        这个指针的生命周期**只能**覆盖「本次蓝图导出」：约定的用法是在导出入口
        先 get_output_blueprint_tree_name() 取快照、在 finally 里
        restore_output_blueprint_tree(快照) 还原。若只在入口 set 而不还原，
        指针会一直留到下一次导出，非蓝图导出（快速局部导出、NTMI ModImp 等）
        就会命中上一个蓝图，输出目录串到旧蓝图。
        """
        cls._output_blueprint_tree_name = str(getattr(tree, "name", "") or "")

    @classmethod
    def clear_output_blueprint_tree(cls):
        """清空输出目录蓝图指针，等价于「当前没有蓝图导出在进行」"""
        cls._output_blueprint_tree_name = ""

    @classmethod
    def get_output_blueprint_tree_name(cls):
        """取输出目录蓝图指针（空串 = 当前不在蓝图导出流程内），供导出入口做快照"""
        return str(getattr(cls, "_output_blueprint_tree_name", "") or "")

    @classmethod
    def restore_output_blueprint_tree(cls, tree_name):
        """把输出目录蓝图指针还原为导出入口的快照值（导出入口的 finally 调它）

        快照为空串即代表进入导出前没有蓝图导出在进行，这时等价于清空。
        含异常与提前 return 的路径都必须走到这里，否则指针会泄漏到下一次导出。
        """
        tree_name = str(tree_name or "")
        if tree_name:
            cls._output_blueprint_tree_name = tree_name
        else:
            cls.clear_output_blueprint_tree()

    @classmethod
    def _get_output_blueprint_tree(cls):
        """取本次导出使用的蓝图树；不在蓝图导出流程内时返回 None"""
        tree_name = str(getattr(cls, "_output_blueprint_tree_name", "") or "")
        if not tree_name:
            return None
        tree = bpy.data.node_groups.get(tree_name)
        if tree is None or getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
            return None
        return tree

    @classmethod
    def _default_generated_mod_folder(cls, folder_name=""):
        """默认输出目录 SSMTGeneratedMod/<文件夹名>。

        folder_name 留空时用工作空间名；传蓝图名即蓝图勾选了「生成Mod到
        [蓝图名] 文件夹中」。确保用的时候直接拿到的就是已经存在的目录。
        """
        ssmt_generated_mod_folder_path = os.path.join(GlobalConfig.path_mods_folder(),"SSMTGeneratedMod\\")
        name = str(folder_name or "").strip() or cls.get_workspace_name()
        # 蓝图名可能含路径非法字符，替换掉，避免建出意外层级的目录
        name = cls._sanitize_generated_mod_name(name) or cls.get_workspace_name()
        generate_mod_folder_path = os.path.join(ssmt_generated_mod_folder_path, name + "\\")
        if not os.path.exists(generate_mod_folder_path):
            os.makedirs(generate_mod_folder_path)
        return generate_mod_folder_path

    @classmethod
    def path_generate_mod_folder(cls):
        """解析本次导出要写入的 Mod 输出目录。

        两条互斥的取值路径，取决于是否在「蓝图导出流程内」（即导出入口是否登记了
        输出目录蓝图指针，见 set_output_blueprint_tree）：

        - 蓝图导出流程内：只认当前蓝图自己的 output_tree 设置。蓝图勾选了
          「生成Mod到指定的文件夹中」且路径非空就用它；勾了「生成Mod到
          [蓝图名] 文件夹中」则用 SSMTGeneratedMod/<蓝图名>；否则用默认目录
          SSMTGeneratedMod/<工作空间>。**绝不回落到场景全局设置**，否则会沿用
          以前在别的蓝图里留下的路径（PR 描述里的「未设置则沿用原有全局逻辑」
          与实际实现不符，以本处实现为准）。
        - 非蓝图导出流程（快速局部导出、NTMI ModImp 等）：沿用场景全局设置。
        """
        # 蓝图导出：只认当前蓝图自己的输出目录设置，蓝图没设置就用默认目录。
        # 这里绝不回落到场景全局设置，否则会沿用以前在别的蓝图里留下的路径。
        output_tree = cls._get_output_blueprint_tree()
        if output_tree is not None:
            if getattr(output_tree, "use_specific_generate_mod_folder_path", False):
                tree_path = str(getattr(output_tree, "generate_mod_folder_path", "") or "").strip()
                if tree_path:
                    return tree_path
            if getattr(output_tree, "use_blueprint_name_generate_mod_folder", False):
                # 「生成Mod到 [蓝图名] 文件夹中」：默认目录改用蓝图名
                return cls._default_generated_mod_folder(getattr(output_tree, "name", ""))
            return cls._default_generated_mod_folder()

        # 非蓝图导出流程：沿用场景全局设置。
        # 但是这里有个问题就是SkipIB和VSCheck不会生成在指定位置。
        if GlobalProterties.use_specific_generate_mod_folder_path():
            global_path = str(GlobalProterties.generate_mod_folder_path() or "").strip()
            if global_path:
                return global_path
        return cls._default_generated_mod_folder()
    
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
    



    
