from dataclasses import dataclass, field
from .draw_call_model import DrawCallModel
from ...base.utils.export_utils import ExportUtils
from ...base.utils.obj_utils import ObjUtils
from ...base.utils.collection_utils import CollectionUtils
from ...base.utils.json_utils import JsonUtils
from ..d3d11.d3d11_gametype import D3D11GameType
from ...helper.obj_buffer_helper import ObjBufferHelper

from ...base.config.main_config import GlobalConfig

import bpy
import os
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

TODO 
这里还有个问题，那就是在Blender中先组合出临时obj，再计算IB，VB，还是先计算IB，VB，再组合数据
这是个问题。

'''
@dataclass
class SubMeshModel:
    '''
    注意，所有的写出文件都是由具体的游戏逻辑负责的
    这里只负责获取ib,category_buffer等数据
    '''
    # 初始化时需要填入此属性
    drawcall_model_list:list[DrawCallModel] = field(default_factory=list)

    # post_init中计算得到这些属性
    match_draw_ib:str = field(init=False, default="")
    match_first_index:int = field(init=False, default=-1)
    match_index_count:int = field(init=False, default=-1)
    unique_str:str = field(init=False, default="")

    # 调用组合obj并计算ib和vb得到这些属性
    vertex_count:int = field(init=False, default=0)
    index_count:int = field(init=False, default=0)

    # 读取工作空间中的import.json来获取d3d11GameType
    d3d11_game_type:D3D11GameType = field(init=False,repr=False,default=None)

    ib:list = field(init=False,repr=False,default_factory=list)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict) 

    def __post_init__(self):

        # 因为列表里的每个DrawCallModel的draw_ib,first_index,index_count都是一样的，所以直接取第一个就行了
        if len(self.drawcall_model_list) > 0:
            self.match_draw_ib = self.drawcall_model_list[0].match_draw_ib
            self.match_first_index = self.drawcall_model_list[0].match_first_index
            self.match_index_count = self.drawcall_model_list[0].match_index_count
            self.unique_str = self.drawcall_model_list[0].get_unique_str()
        
        self.calc_buffer()
    

    def calc_buffer(self):
        # 对每个obj都创建一个临时对象进行处理，这样不影响原本的对象

        index_offset = 0
        submesh_temp_obj_list = []
        for draw_call_model in self.drawcall_model_list:
            # 获取到原本的obj
            source_obj = ObjUtils.get_obj_by_name(draw_call_model.obj_name)

            temp_collection = CollectionUtils.create_new_collection("TEMP_SUBMESH_COLLECTION_" + self.unique_str)
            bpy.context.scene.collection.children.link(temp_collection)


            # 创建一个新的obj
            temp_obj = ObjUtils.copy_object(
                context=bpy.context,
                obj=source_obj,
                name=source_obj.name + "_temp",
                collection= temp_collection
            )


            # 三角化obj
            ObjUtils.triangulate_object(bpy.context, temp_obj)

            # 计算其额外属性，因为for里拿到的是引用，所以原地修改即可
            draw_call_model.vertex_count = len(temp_obj.data.vertices)
            # 因为三角化了，所以每个面都是3个索引，所以 *3 就没问题
            # 所以上面那一步三角化是必须的，否则概率报错
            draw_call_model.index_count = len(temp_obj.data.polygons) * 3
            draw_call_model.index_offset = index_offset

            index_offset += draw_call_model.index_count

            # 这里赋值的意义在于，后续可能会合并到DrawIB级别
            # 这里就可以直接复用了
            self.vertex_count += draw_call_model.vertex_count
            self.index_count += draw_call_model.index_count

            # 临时对象放到列表里，后续进行合并
            submesh_temp_obj_list.append(temp_obj)

        # 接下来合并obj，合并的意义在于可以减少IB和VB的计算次数，在大批量导出时节省很多时间
        # 确保选中第一个，否则join_objects会报错
        if submesh_temp_obj_list:
            # 取消选中所有物体
            bpy.ops.object.select_all(action='DESELECT')

            # 选中第一个物体并设置为活动物体
            target_active = submesh_temp_obj_list[0]
            target_active.select_set(True)
            bpy.context.view_layer.objects.active = target_active

        # 执行物体合并
        ObjUtils.join_objects(bpy.context, submesh_temp_obj_list)
        
        # 因为合并到第一个obj上了，所以这里直接拿到这个obj
        submesh_merged_obj = submesh_temp_obj_list[0]

        # 重命名为指定名称，等待后续操作
        merged_obj_name = "TEMP_SUBMESH_MERGED_" + draw_call_model.get_unique_str()
        ObjUtils.rename_object(submesh_merged_obj, merged_obj_name)

        # 这里我们需要拿到当前这个Obj的数据类型
        # 此时需要去读取工作空间中对应import.json中的d3d11_element_list
        # 此时定位到工作空间的文件夹，需要用unique_str
        folder_name = draw_call_model.get_unique_str()

        # 还需要定位到具体导入时导入的是哪个数据类型
        # 这个在一键导入的时候记录到当前工作空间下的Import.json中了
        # 先读取Import.json拿到当前导入的是哪个数据类型文件夹名称
        import_json_path = os.path.join(GlobalConfig.path_workspace_folder(), "Import.json")
        import_json = JsonUtils.LoadFromFile(import_json_path)
        gametype_name = import_json.get(folder_name,"")
        gametype_foldername = "TYPE_" + gametype_name
        import_folder_path = os.path.join(GlobalConfig.path_workspace_folder(), folder_name)
        import_json_path = os.path.join(import_folder_path, gametype_foldername, "import.json")

        # 根据import.json中的d3d11_element_list来获取当前SubMeshModel的D3D11GameType
        self.d3d11_game_type = D3D11GameType(FilePath=import_json_path)

        # 检查并校验是否有缺少的元素
        ObjBufferHelper.check_and_verify_attributes(obj=submesh_merged_obj, d3d11_game_type=self.d3d11_game_type)

        obj_buffer_result = ExportUtils.build_unity_obj_buffer_result(
            obj=submesh_merged_obj,
            d3d11_game_type=self.d3d11_game_type,
        )
        self.ib = obj_buffer_result.ib
        self.category_buffer_dict = obj_buffer_result.category_buffer_dict
        self.index_vertex_id_dict = obj_buffer_result.index_loop_id_dict

        # 4.计算完成后，删除临时obj
        bpy.data.objects.remove(submesh_merged_obj, do_unlink=True)

        # 顺便把刚才创建的临时集合也删掉
        bpy.context.scene.collection.children.unlink(temp_collection)
        bpy.data.collections.remove(temp_collection)

        print("SubMeshModel: " + self.unique_str + " 计算完成，临时对象已删除")