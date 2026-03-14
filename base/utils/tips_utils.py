from .format_utils import Fatal

class TipUtils:

    @staticmethod
    def raise_collection_name_parse_error(collection_name:str):

        raise Fatal("\n无法正确解析" + collection_name + "集合的名称，请确认您的集合名称符合分支架构命名规范\n分支架构规定的集合命名规范为: 指定的按键__按键初始值__集合名称\n注意这里是用两个下划线分隔开的\n例如 CTRL 1__0__头发 代表这个集合指定使用CTRL + 1按键组合来控制，初始值为0，集合名称为头发")