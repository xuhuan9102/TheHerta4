from ..utils.ssmt_error_utils import SSMTErrorUtils

from dataclasses import dataclass, field

'''
TODO

这里的设计有问题
例如对于WWMI来说，整个DrawIB级别的内容可以合并在一起计算
得到一个总的IB和CategoryBuffer，然后直接写出就行了
但是此时仍然需要得到SubMesh级别的偏移和索引

对于GIMI来说，CategoryBuffer可以是DrawIB级别的
但是IB可以是DrawIB级别的简单一个buffer，也可以是SubMesh级别的多个buffer来突破顶点索引限制
所以理想的方法是先在SubMeshModel层级计算好IB和CategoryBuffer，然后在DrawIBModel层级进行合并，最后写出

对于EFMI来说，IB和CategoryBuffer都是SubMesh级别的

所以说最终的IndexBuffer和CategoryBuffer，应该是在SubMeshModel层级计算好，然后在DrawIBModel层级进行合并
这样可以灵活应用，解决所有层面的问题
所以不应该在ObjDataModel层级直接计算，而是在SubMesh级别上进行临时obj拼接，拼接后进行计算
使用这个架构还能把WWMI、GIMI、EFMI等流程全部使用一套架构来完成，算是解决了之前遗留的架构设计问题。

所以ObjDataModel它只能代表BluePrintModel解析出来的结果，然后交给下一层SubMesh来进行计算IB、VB等
最后交由各个游戏的控制逻辑决定是否再提升到DrawIB层级

ObjDataModel => SubMeshModel => DrawIBModel
SubMeshModel必选，因为一个SubMeshModel里可以有1到多个ObjDataModel
DrawIBModel可选，由各个游戏生成Mod逻辑控制

SubMeshModel中进行临时obj的生成，以及计算每个obj的drawindexed

但是在WWMI中，直接进行了DrawIB层级的obj合并？
那么那种情况下，SubMeshModel还有存在的意义吗？
也许SubMeshModel层级的属性计算也可以作为可选项
如果是WWMI调用，就不计算SubMeshModel层级的IBVB，直接在DrawIBModel层级进行合并计算IBVB
如果是GIMI调用，就先计算SubMeshModel层级的IBVB，再在DrawIBModel层级进行合并计算IBVB
如果是EFMI调用，就只计算SubMeshModel层级的IBVB，直接生成Mod

计算IB和VB不是必要的，SubMeshModel只起到一个把mesh聚集在一起的作用，真到了用到的时候再去计算
也就是它只要维护一个ObjDataModel列表即可，其它都是可选项

为了架构实现起来更加简单易懂，直接改为SubMeshModel层级进行IBVB的计算
DrawIBModel层级只进行SubMeshModel的合并，最后写出

此外M_DrawIndexed和M_DrawIndexedInstanced也可以直接放在ObjDataModel里
他们的属性都是固定的，在合并obj并计算时，就可以得到，
直接放在ObjDataModel中会让调用更加简单便捷，只需要提供get_drawindexed_str()和get_drawindexedinstanced_str()方法就行了

这种情况下，每个Obj其实都是一个DrawCall
所以ObjDataModel改名为DrawCallModel更合适

每个SubMesh可以有多个DrawCall
每个DrawIB可以有多个SubMesh
对整个绘制流程进行了标准的抽象，在此基础上统一了所有游戏的绘制调用流程
再由每个游戏的具体逻辑去处理具体的情况，比如要不要合并IB，VB到DrawIB层级等等
'''

from .m_key import M_Key


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
    draw_ib:str = field(init=False,repr=False,default="")
    index_count:str = field(init=False,repr=False,default="")
    first_index:str = field(init=False,repr=False,default="")
    obj_alias_name:str = field(init=False,repr=False,default="")
    display_name:str = field(init=False,repr=False,default="")

    # 生效条件，在BlueprintModel解析的时候得到
    condition:M_Condition = field(init=False,repr=False,default_factory=M_Condition)


    # 这些是后续计算手动赋值进来的
    # TODO 当然也可以直接来一个函数来计算得到，具体看最终设计
    ib:list = field(init=False,repr=False,default_factory=list)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    # 仅用于WWMI的索引顶点ID字典，key是顶点索引，value是顶点ID，默认可以为None
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict) 

    # 最终计算得到的偏移，才能得到DrawIndexed
    drawindexed_obj:M_DrawIndexed = field(init=False,repr=False,default_factory=M_DrawIndexed)

    def __post_init__(self):
        obj_rule_name = ObjRuleName(self.obj_name)

        self.draw_ib = obj_rule_name.draw_ib
        self.index_count = obj_rule_name.index_count
        self.first_index = obj_rule_name.first_index
        self.obj_alias_name = obj_rule_name.obj_alias_name
        self.display_name = self.obj_name
        
       
       