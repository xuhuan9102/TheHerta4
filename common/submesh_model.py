from dataclasses import dataclass, field
from typing import Dict
from ..base.obj_data_model import ObjDataModel
'''
一般DrawIB索引缓冲区是由多个SubMesh子网格构成的
每个Submesh分别具有不同的材质和内容
所以这里沿用术语Submesh

因为我们可以通过DrawIndexed多次来绘制一个Submesh
所以Submesh是由多个Blender中的obj组成的

也就是在初始化的时候，遍历BlueprintModel中所有的obj
按照first_index,index_count,draw_ib来组在一起变成一个个Submesh
每个Submesh都包含1到多个obj
最后BluePrintModel可以得到一个SubmeshModel列表

然后就是数据的组合和数据的导出了
IB、CategoryBuffer要先组合在一起

然后在SubmeshModel之上，部分游戏还需要进行DrawIB级别的组合。
EFMI这个游戏只需要SubmeshModel级别的组合就行了，然后直接生成Mod
但是像GIMI这种游戏还需要在SubmeshModel之上进行DrawIB级别的组合，最后生成Mod

所以基于这个架构才是比较清晰的，SubmeshModel只负责Submesh级别的组合和数据导出
DrawIBModel负责DrawIB级别的组合和数据导出

'''
@dataclass
class SubMeshModel:

    obj_data_model_list:list[] = field(default_factory=list)

    draw_ib:str = field(init=False, default="")
    first_index:int = field(init=False, default=-1)
    index_count:int = field(init=False, default=-1)

    def __post_init__(self):
        pass