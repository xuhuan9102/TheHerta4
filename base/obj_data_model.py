from .m_condition import M_Condition
from .m_draw_indexed import M_DrawIndexed
from .obj_rule_name import ObjRuleName

from dataclasses import dataclass, field

@dataclass
class ObjDataModel:
    obj_name:str

    # 传入obj_name后，根据ObjRuleName解析出这些属性，方便后续使用
    draw_ib:str = field(init=False,repr=False,default="")
    index_count:str = field(init=False,repr=False,default="")
    first_index:str = field(init=False,repr=False,default="")
    obj_alias_name:str = field(init=False,repr=False,default="")
    display_name:str = field(init=False,repr=False,default="")

    ib:list = field(init=False,repr=False,default_factory=list)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)

    # 仅用于WWMI的索引顶点ID字典，key是顶点索引，value是顶点ID，默认可以为None
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict) 

    condition:M_Condition = field(init=False,repr=False,default_factory=M_Condition)
    drawindexed_obj:M_DrawIndexed = field(init=False,repr=False,default_factory=M_DrawIndexed)

    def __post_init__(self):
        obj_rule_name = ObjRuleName(self.obj_name)

        self.draw_ib = obj_rule_name.draw_ib
        self.index_count = obj_rule_name.index_count
        self.first_index = obj_rule_name.first_index
        self.obj_alias_name = obj_rule_name.obj_alias_name
        self.display_name = self.obj_name
        
       
       