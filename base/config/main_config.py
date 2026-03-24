import bpy
import os
import json


from ..config.global_properties import GlobalProterties

'''
执行逻辑名称
在SSMT3系列中，任意游戏可以配置任意执行逻辑，根据执行逻辑决定具体游戏流程
在SSMT4系列中，为了简化流程，降低维护成本，每个游戏都对应一个LogicName
或者说LogicName字段本身就是和SSMT4端的GamePreset游戏预设字段一一对应

另外，不管在数据类型上理解有多么深刻，当大部分用户选择使用错误的数据类型，并形成习惯时
例如WWMI的COLOR1还是TEXCOORD问题，此时为了降低维护成本，应该尊重大众的选择
毕竟这个世界大部分都是普通人，普通人是不想思考的，越简单越好。
也可以理解为一个概念若是被大部分人误解了，那么这个概念的正确理解就不重要了，
重要的是这个概念被大部分人误解成什么样了。
SSMT的本质是什么不重要，重要的是在大部分人眼中SSMT是什么。
'''
class LogicName:
    # 高人气游戏，常驻维护
    GIMI = "GIMI"
    HIMI = "HIMI"
    SRMI = "SRMI"
    ZZMI = "ZZMI"
    WWMI = "WWMI"
    EFMI = "EFMI"

    # 小众游戏，使用人数极少，有用户反馈时进行维护即可
    # 注意，如果一个游戏有原生Mod方式，就不应该使用3Dmigoto来进行Mod制作
    # 与主流相悖的路线只会导致维护成本过高，最终被世人遗忘
    # 如果只给少数部分人提供服务的话，就必须要考虑维护成本问题
    GF2 = "GF2" # 少女前线2，或者CPU-PreSkinning类型游戏，使用3Dmigoto强行修改的代表性方法
    IdentityVNeoX3 = "IdentityV2" # 第五人格Neox3引擎，目前留着也只是为部分抽象二创视频作者提供服务
    AILIMIT = "AILIMIT" # 小厂小游戏，但是虹汐哥还在开设粉丝群，暂且给他的粉丝群留着
    DOAV = "DOAV" # 古董游戏，万恶之源，就算添加了又有什么用呢，留着只是致敬
    SnowBreak = "SnowBreak" # 尘白禁区已经有原生Mod方式了，但是呢，万一哪天失效了，3Dmigoto将成为备选
    Nioh2 = "Nioh2" # 这游戏玩的人比较少，快被淘汰了，只剩下可怜的几个为爱发电的作者，且IB数量巨大，制作难度极高，维护成本极高
    YYSLS = "YYSLS" # 燕云十六声，花费巨大宣发经费，但玩的人还是很少
    Naraka = "Naraka" # 使用Mod会掉帧/封禁帐号30天/封禁永久
    NarakaM = "NarakaM" # 使用Mod会掉帧/封禁帐号30天/封禁永久

    # 预留位置
    APMI = "APMI" # 还在内测的蓝色星原，已在测试服中测试过，完美支持3Dmigoto，预计发布就会被XXMI收录
    NEMI = "NEMI" # 还在内测的异环，已在测试服中测试过，完美支持3Dmigoto，预计发布就会被XXMI收录


# 全局配置类，使用字段默认为全局可访问的唯一静态变量的特性，来实现全局变量
# 可减少从Main.json中读取的IO消耗
class GlobalConfig:
    # 全局静态变量,任何地方访问到的值都是唯一的
    gamename = ""
    workspacename = ""
    dbmtlocation = ""
    current_game_migoto_folder = ""
    logic_name = ""

    @classmethod
    def read_from_main_json_ssmt4(cls) :
        try:
            main_json_path = GlobalConfig.path_main_json_ssmt4()
            print("Reading SSMT4 main json from: " + main_json_path)
            # 先从main_json_path里读取dbmt位置，也就是dbmt总工作空间的位置
            # 在新架构中，总工作空间位置已不会再发生改变，所以用户只需要选择一次就可以了
            if os.path.exists(main_json_path):
                main_setting_file = open(main_json_path)
                main_setting_json = json.load(main_setting_file)
                main_setting_file.close()
                cls.workspacename = main_setting_json.get("CurrentWorkSpace","")
                cls.gamename = main_setting_json.get("CurrentGameName","")
                cls.dbmtlocation = main_setting_json.get("DBMTWorkFolder","") + "\\"
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
        return cls.dbmtlocation
    
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
        # 先从main_json_path里读取dbmt位置，也就是dbmt总工作空间的位置
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
            ssmt_generated_mod_default_folder_path = os.path.join(ssmt_generated_mod_folder_path,"Default\\")

            generate_mod_folder_path = os.path.join(ssmt_generated_mod_default_folder_path,"Mod_" + GlobalConfig.workspacename + "\\")

            if not os.path.exists(generate_mod_folder_path):
                os.makedirs(generate_mod_folder_path)
            return generate_mod_folder_path
    
    @classmethod
    def path_extract_gametype_folder(cls,draw_ib:str,gametype_name:str):
        return os.path.join(GlobalConfig.path_workspace_folder(), draw_ib + "\\TYPE_" + gametype_name + "\\")
    
    @classmethod
    def path_generatemod_buffer_folder(cls):
       
        buffer_folder_name = "Buffer"
        
        buffer_path = os.path.join(GlobalConfig.path_generate_mod_folder(), buffer_folder_name + "\\")
        if not os.path.exists(buffer_path):
            os.makedirs(buffer_path)
        return buffer_path
    
    @classmethod
    def get_buffer_folder_name(cls):
        """获取当前Buffer文件夹名称（用于INI文件中的路径引用）"""
        buffer_folder_name = "Buffer"
        return buffer_folder_name
    
    @classmethod
    def path_generatemod_texture_folder(cls,draw_ib:str):

        texture_path = os.path.join(GlobalConfig.path_generate_mod_folder(),"Texture\\")
        if not os.path.exists(texture_path):
            os.makedirs(texture_path)
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
    



    
