from ..utils.ssmt_error_utils import SSMTErrorUtils
from .m_key import M_Key

from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DrawCallModel:
    obj_name:str

    # 传入obj_name后，解析出这些属性，方便后续使用
    match_draw_ib:str = field(init=False,repr=False,default="") # 用于匹配的DrawIB
    match_index_count:str = field(init=False,repr=False,default="") # 用于匹配的IndexCount
    match_first_index:str = field(init=False,repr=False,default="") # 用于匹配的FirstIndex
    comment_alias_name:str = field(init=False,repr=False,default="") # 用于显示在注释中的自定义名称

    # 生效条件，在BlueprintModel解析的时候得到
    work_key_list:list[M_Key] = field(init=False,repr=False,default_factory=list)

    # 在SubMeshModel层级计算得到这些属性，用于ini写出
    index_count:int = field(init=False,repr=False,default=0)
    vertex_count:int = field(init=False,repr=False,default=0)
    index_offset:int = field(init=False,repr=False,default=0)


    def __post_init__(self) -> None:
        objname_parse_error_tips = "Obj名称规则为: DrawIB-IndexCount-FirstIndex.AliasName,例如[67f829fc-2653-0.头发]第一个.前面的内容要符合规则,后面出现的内容是可以自定义的"

        if "." not in self.obj_name:
            SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + objname_parse_error_tips)

        obj_name_total_split = self.obj_name.split(".")
        obj_name_split = obj_name_total_split[0].split("-")

        if len(obj_name_total_split) < 2:
            SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + objname_parse_error_tips)

        self.comment_alias_name = ".".join(obj_name_total_split[1:]) if len(obj_name_total_split) > 1 else ""

        if len(obj_name_split) < 3:
            SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  '-'分隔符数量不足，至少需要2个\n" + objname_parse_error_tips)

        self.match_draw_ib = obj_name_split[0]
        self.match_index_count = obj_name_split[1]
        self.match_first_index = obj_name_split[2]
    
    def get_unique_str(self) -> str:
        # 这个唯一标识符是根据DrawIB、FirstIndex和IndexCount组成的字符串，作为一个整体来标识一个DrawCall
        # 同时也和提取出来的工作空间目录下对应的目录名称一致
        return self.match_draw_ib + "-" + self.match_index_count+ "-" + self.match_first_index 

    def get_condition_str(self) -> str:
        if len(self.work_key_list) == 0:
            return ""

        condition_str_list = []
        for work_key in self.work_key_list:
            condition_str_list.append(work_key.key_name + " == " + str(work_key.tmp_value))

        return " && ".join(condition_str_list)

    def get_drawindexed_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(self.obj_name, self.index_offset)
        return f"drawindexed = {self.index_count},{draw_offset},0"

    def get_drawindexed_instanced_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(self.obj_name, self.index_offset)
        return f"drawindexedinstanced = {self.index_count},INSTANCE_COUNT,{draw_offset},0,FIRST_INSTANCE"
        
 