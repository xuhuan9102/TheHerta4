from .fatal import Fatal

# 用于解析Obj名称的规则类，按照规则从Obj名称中提取DrawIB、IndexCount、FirstIndex和AliasName等信息
# 防止把解析写的到处都是，集中在一个地方，方便维护和修改规则
class ObjRuleName:
    def __init__(self, obj_name:str):
        self.obj_name = obj_name
        self.draw_ib = ""
        self.index_count = ""
        self.first_index = ""
        self.obj_alias_name = ""

        self.objname_parse_error_tips = "Obj名称规则为: DrawIB-IndexCount-FirstIndex.AliasName,例如[67f829fc-2653-0.头发]第一个.前面的内容要符合规则,后面出现的内容是可以自定义的"
        
        if "." in self.obj_name:
            obj_name_total_split = self.obj_name.split(".")
            obj_name_split = obj_name_total_split[0].split("-")
            
            if len(obj_name_total_split) < 2:
                raise Fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + self.objname_parse_error_tips)

            self.obj_alias_name = ".".join(obj_name_total_split[1:]) if len(obj_name_total_split) > 1 else ""

            if len(obj_name_split) < 3:
                raise Fatal("Obj名称解析错误: " + self.obj_name + "  '-'分隔符数量不足，至少需要2个\n" + self.objname_parse_error_tips)
            else:
                self.draw_ib = obj_name_split[0]
                self.index_count = obj_name_split[1]
                self.first_index = obj_name_split[2]
        else:
            raise Fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + self.objname_parse_error_tips)
