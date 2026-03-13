

class M_GlobalKeyCounter:
    '''
    在新版的生成Mod架构中用于统计一个Mod中全局的按键索引
    以及当前生成Mod的数量，每个DrawIB都是一个Mod。
    使用全局变量来避免过于复杂的变量传递。
    '''

    global_key_index:int = 0
    generated_mod_number:int = 0

    @classmethod
    def initialize(cls):        
        cls.global_key_index = 0
        cls.generated_mod_number = 0