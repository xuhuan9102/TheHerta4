import bpy

class TR:
    '''
    中文为主要语言，这个类的作用是将中文翻译成其他语言。
    因为开发人员只有我自己，所以只添加英文翻译。
    '''
    
    # 翻译字典 - key为中文，value为英文翻译（只保留3个作为演示）
    _translations = {

        # SSMT基础面板
        "一键导入当前工作空间内容": "Import All From WorkSpace",
        "生成Mod": "Generate Mod", 
        "导入.fmt .ib .vb格式模型": "Import .fmt .ib .vb Model",


    }

    @classmethod
    def _get_blender_language(cls) -> str:
        """获取Blender当前使用的语言设置"""
        try:
            # 获取Blender的用户偏好设置中的语言
            return bpy.context.preferences.view.language
        except:
            # 如果获取失败，默认返回中文
            return "zh_CN"

    @classmethod
    def _is_chinese_language(cls) -> bool:
        """判断当前是否为中文语言环境"""
        current_lang = cls._get_blender_language()
        # 中文语言代码通常以'zh'开头（如zh_CN, zh_TW等）
        return current_lang.startswith('zh')

    @classmethod
    def translate(cls, text: str) -> str:
        """
        翻译文本
        如果是中文环境，返回原文；否则返回英文翻译
        """
        if cls._is_chinese_language():
            return text
        else:
            # 如果在翻译字典中找到对应的英文翻译，返回翻译后的文本
            # 如果没找到，返回原文（避免未翻译的文本显示为空）
            return cls._translations.get(text, text)


