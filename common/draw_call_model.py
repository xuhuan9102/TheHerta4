from ..utils.ssmt_error_utils import SSMTErrorUtils
from .m_condition import M_Condition
from .obj_rule_name import ObjRuleName

from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DrawCallModel:
    obj_name:str

    # 传入obj_name后，根据ObjRuleName解析出这些属性，方便后续使用
    match_draw_ib:str = field(init=False,repr=False,default="") # 用于匹配的DrawIB
    match_index_count:str = field(init=False,repr=False,default="") # 用于匹配的IndexCount
    match_first_index:str = field(init=False,repr=False,default="") # 用于匹配的FirstIndex
    comment_alias_name:str = field(init=False,repr=False,default="") # 用于显示在注释中的自定义名称

    # 生效条件，在BlueprintModel解析的时候得到
    condition:M_Condition = field(init=False,repr=False,default_factory=M_Condition)

    # 在SubMeshModel层级计算得到这些属性，用于ini写出
    index_count:int = field(init=False,repr=False,default=0)
    vertex_count:int = field(init=False,repr=False,default=0)
    index_offset:int = field(init=False,repr=False,default=0)


    def __post_init__(self) -> None:
        obj_rule_name = ObjRuleName(self.obj_name)
        self.match_draw_ib = obj_rule_name.draw_ib
        self.match_index_count = obj_rule_name.index_count
        self.match_first_index = obj_rule_name.first_index
        self.comment_alias_name = obj_rule_name.obj_alias_name
    
    def get_unique_str(self) -> str:
        # 这个唯一标识符是根据DrawIB、FirstIndex和IndexCount组成的字符串，作为一个整体来标识一个DrawCall
        # 同时也和提取出来的工作空间目录下对应的目录名称一致
        return self.match_draw_ib + "-" + self.match_index_count+ "-" + self.match_first_index 

    def get_drawindexed_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(self.obj_name, self.index_offset)
        return f"drawindexed = {self.index_count},{draw_offset},0"

    def get_drawindexed_instanced_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(self.obj_name, self.index_offset)
        return f"drawindexedinstanced = {self.index_count},INSTANCE_COUNT,{draw_offset},0,FIRST_INSTANCE"
        
       
# TODO 这里DrawCallModel应该负责生成最后的draw_str，
# 包含DrawIndexed和DrawIndexedInstanced两种情况，
# 后续根据需要再添加其他类型的DrawCall，例如DrawIndexedInstancedIndirect需要生成额外Buffer文件，等等
