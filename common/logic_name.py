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
    IdentityV = "IdentityV" # 第五人格Neox3引擎，目前留着也只是为部分抽象二创视频作者提供服务
    AILIMIT = "AILIMIT" # 小厂小游戏，但是虹汐哥还在开设粉丝群，暂且给他的粉丝群留着
    DOAV = "DOAV" # 古董游戏，万恶之源，就算添加了又有什么用呢，留着只是致敬
    SnowBreak = "SnowBreak" # 尘白禁区已经有原生Mod方式了，但是呢，万一哪天失效了，3Dmigoto将成为备选
    YYSLS = "YYSLS" # 燕云十六声，花费巨大宣发经费，但玩的人还是很少
    Naraka = "Naraka" # 使用Mod会掉帧/封禁帐号30天/封禁永久
    NarakaM = "NarakaM" # 使用Mod会掉帧/封禁帐号30天/封禁永久
    
    NTEMI = "NTEMI" # 异环，仅测试
    
    # 预留位置
    APMI = "APMI" # 还在内测的蓝色星原，已在测试服中测试过，完美支持3Dmigoto，预计发布就会被XXMI收录
    NEMI = "NEMI" # 还在内测的异环，已在测试服中测试过，完美支持3Dmigoto，预计发布就会被XXMI收录
