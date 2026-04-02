from ..utils.ssmt_error_utils import SSMTErrorUtils
from .migoto.m_key import M_Key

from dataclasses import dataclass, field


class M_DrawIndexed:
    def __init__(self) -> None:
        self.DrawNumber = ""

        # 绘制起始位置
        self.DrawOffsetIndex = "" 

        self.DrawStartIndex = "0"

        # 代表一个obj具体的draw_indexed
        self.AliasName = "" 

        # 代表这个obj的顶点数
        self.UniqueVertexCount = 0 
    
    def get_draw_str(self) ->str:
        return "drawindexed = " + self.DrawNumber + "," + self.DrawOffsetIndex +  "," + self.DrawStartIndex
  
@dataclass
class M_DrawIndexedInstanced:
    '''
    https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-drawindexedinstanced
    '''

    # Number of indices read from the index buffer for each instance.
    # 每个Instance绘制的索引数
    IndexCountPerInstance:int = field(init=False,repr=False,default=0)

    # Number of instances to draw.
    # 绘制几个Instance，一般用3Dmigoto默认的INSTANCE_COUNT即可，除非你需要绘制特定数量的Instance
    InstanceCount:int = field(init=False,repr=False,default=0)

    # The location of the first index read by the GPU from the index buffer.
    # 绘制起始索引偏移
    StartIndexLocation:int = field(init=False,repr=False,default=0)

    # A value added to each index before reading a vertex from the vertex buffer.
    # 一般情况下都为0
    BaseVertexLocation:int = field(init=False,repr=False,default=0)

    # 一般无需关注，用3Dmigoto默认的FIRST_INSTANCE即可，除非你需要从特定Instance开始绘制
    # A value added to each index before reading per-instance data from a vertex buffer.
    StartInstanceLocation:int = field(init=False,repr=False,default=0)

    def get_draw_str(self) ->str:

        draw_str = "drawindexedinstanced = "
        draw_str += str(self.IndexCountPerInstance) + ","

        # 一般使用默认值INSTANCE_COUNT
        if self.InstanceCount == 0:
            draw_str += "INSTANCE_COUNT,"
        else:
            draw_str += str(self.InstanceCount) + ","

        draw_str += str(self.StartIndexLocation) + ","      

        draw_str += str(self.BaseVertexLocation) + ","

        # 一般使用默认值FIRST_INSTANCE
        if self.StartInstanceLocation == 0:
            draw_str += "FIRST_INSTANCE"
        else:
            draw_str += str(self.StartInstanceLocation)
        return draw_str

class M_Condition:
    '''
    因为M_Condition只是DrawCallModel的一个属性
    所以在这里和DrawCallModel放在一起定义了
    '''
    def __init__(self,work_key_list:list[M_Key] = []):
        self.work_key_list = work_key_list

        # 计算出生效的ConditionStr
        condition_str = ""
        if len(self.work_key_list) != 0:
            for work_key in self.work_key_list:
                single_condition:str = work_key.key_name + " == " + str(work_key.tmp_value)
                condition_str = condition_str + single_condition + " && "
            # 移除结尾的最后四个字符 " && "
            condition_str = condition_str[:-4] 
        
        self.condition_str = condition_str

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
                SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + self.objname_parse_error_tips)

            self.obj_alias_name = ".".join(obj_name_total_split[1:]) if len(obj_name_total_split) > 1 else ""

            if len(obj_name_split) < 3:
                SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  '-'分隔符数量不足，至少需要2个\n" + self.objname_parse_error_tips)
            else:
                self.draw_ib = obj_name_split[0]
                self.index_count = obj_name_split[1]
                self.first_index = obj_name_split[2]
        else:
            SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + self.objname_parse_error_tips)

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


    def __post_init__(self):
        obj_rule_name = ObjRuleName(self.obj_name)
        self.match_draw_ib = obj_rule_name.draw_ib
        self.match_index_count = obj_rule_name.index_count
        self.match_first_index = obj_rule_name.first_index
        self.comment_alias_name = obj_rule_name.obj_alias_name
    
    def get_unique_str(self) -> str:
        # 这个唯一标识符是根据DrawIB、FirstIndex和IndexCount组成的字符串，作为一个整体来标识一个DrawCall
        # 同时也和提取出来的工作空间目录下对应的目录名称一致
        return self.match_draw_ib + "-" + self.match_index_count+ "-" + self.match_first_index 
        
       
       