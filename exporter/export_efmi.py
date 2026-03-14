from ..common.export.blueprint_model import BluePrintModel
from ..common.export.draw_call_model import DrawCallModel
from ..common.export.submesh_model import SubMeshModel

from dataclasses import dataclass,field


@dataclass
class ExportEFMI:

    blueprint_model:BluePrintModel

    submesh_model_list:list[SubMeshModel] = field(default_factory=list,init=False)

    def __post_init__(self):
        self.initialize_submesh_model_list()
        
    def initialize_submesh_model_list(self):
        # 根据唯一标识符，把相同的DrawCallModel分在一起，形成SubMeshModel
        draw_call_model_dict:dict[str,list[DrawCallModel]] = {}

        # 拿到BlueprintModel后，开始解析SubMeshModel列表
        for draw_call_model in self.blueprint_model.ordered_draw_obj_data_model_list:
            # 获取独立标识
            unique_str = draw_call_model.get_unique_str()

            # 根据unique_str，加入到字典中，这样每个unique_str都对应一个DrawCallModel列表，用于初始化SubMeshModel
            draw_call_model_list = draw_call_model_dict.get(unique_str,[])
            draw_call_model_list.append(draw_call_model)
            draw_call_model_dict[unique_str] = draw_call_model_list

        # 根据draw_call_model_dict，初始化SubMeshModel列表
        for unique_str, draw_call_model_list in draw_call_model_dict.items():
            submesh_model = SubMeshModel(drawcall_model_list=draw_call_model_list)
            self.submesh_model_list.append(submesh_model)


    def export(self):
        pass
    