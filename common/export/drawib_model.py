
from dataclasses import field, dataclass

from ..d3d11.d3d11_gametype import D3D11GameType

import numpy

from .submesh_model import SubMeshModel

@dataclass
class DrawIBModel:
    '''
    DrawIBModel是一个更高层次的模型，包含一个或多个SubMeshModel
    适用于米游、Unity等需要将多个SubMesh组合成一个DrawIB进行导出的游戏
    要使用DrawIBModle，必须确保每个SubMesh的数据类型是相同的，才有可能组合在一起
    '''
    submesh_model_list:list[SubMeshModel]
    combine_ib:bool = True

    draw_ib:str = field(init=False, default="")

    vertex_count:int = field(init=False, default=0)
    index_count:int = field(init=False, default=0)

    # 这里的d3d11_game_type有一个隐含条件
    # DrawIBModel一旦创建，就默认它里面的每个SubmeshModel的d3d11_game_type都是一样的
    # 否则不可能通过组合buffer数据来正确导出
    # 所以这里的d3d11_game_type可以直接取submesh_model_list中第一个SubMeshModel的d3d11_game_type
    d3d11_game_type:D3D11GameType = field(init=False,repr=False,default="")

    ib:list = field(init=False,repr=False,default_factory=list)
    submesh_ib_dict:dict = field(init=False,repr=False,default_factory=dict)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict)
    obj_name_draw_offset:dict = field(init=False,repr=False,default_factory=dict)


    def __post_init__(self):
        # 因为初始化时传入的SubMeshModel列表中的每个SubMeshModel的match_draw_ib都是一样的，所以直接取第一个就行了
        self.draw_ib = self.submesh_model_list[0].match_draw_ib if len(self.submesh_model_list) > 0 else ""
        self.d3d11_game_type = self.submesh_model_list[0].d3d11_game_type if len(self.submesh_model_list) > 0 else None

        category_buffer_dict, submesh_vertex_base_dict, index_vertex_id_dict, vertex_count = self._assemble_category_buffers()

        self.vertex_count = vertex_count
        self.category_buffer_dict = category_buffer_dict
        self.index_vertex_id_dict = index_vertex_id_dict

        if self.combine_ib:
            total_ib, obj_name_draw_offset = self._assemble_combined_ib_and_draw_offset(submesh_vertex_base_dict)
            self.ib = total_ib
            self.submesh_ib_dict = {}
            self.obj_name_draw_offset = obj_name_draw_offset
            self.index_count = len(total_ib)
        else:
            submesh_ib_dict, obj_name_draw_offset, total_index_count = self._assemble_split_ib_and_draw_offset(submesh_vertex_base_dict)
            self.ib = []
            self.submesh_ib_dict = submesh_ib_dict
            self.obj_name_draw_offset = obj_name_draw_offset
            self.index_count = total_index_count

    def _assemble_category_buffers(self) -> tuple[dict, dict, dict, int]:
        total_category_buffer_chunks = {}
        total_index_vertex_id_dict = {}
        submesh_vertex_base_dict = {}
        vertex_offset = 0

        for submesh_model in self.submesh_model_list:
            submesh_vertex_base_dict[submesh_model.unique_str] = vertex_offset

            for category, category_buf in submesh_model.category_buffer_dict.items():
                if category not in total_category_buffer_chunks:
                    total_category_buffer_chunks[category] = []
                total_category_buffer_chunks[category].append(category_buf)

            if submesh_model.index_vertex_id_dict:
                for local_index, vertex_id in submesh_model.index_vertex_id_dict.items():
                    total_index_vertex_id_dict[local_index + vertex_offset] = vertex_id

            vertex_offset += self._get_exported_vertex_count(submesh_model)

        total_category_buffer_dict = {
            category: numpy.concatenate(category_chunks)
            for category, category_chunks in total_category_buffer_chunks.items()
            if category_chunks
        }
        return total_category_buffer_dict, submesh_vertex_base_dict, total_index_vertex_id_dict, vertex_offset

    def _assemble_combined_ib_and_draw_offset(self, submesh_vertex_base_dict: dict) -> tuple[list, dict]:
        total_ib = []
        obj_name_draw_offset = {}
        submesh_index_offset = 0

        for submesh_model in self.submesh_model_list:
            vertex_base = submesh_vertex_base_dict.get(submesh_model.unique_str, 0)
            total_ib.extend(index + vertex_base for index in submesh_model.ib)

            for draw_call_model in submesh_model.drawcall_model_list:
                obj_name_draw_offset[draw_call_model.obj_name] = submesh_index_offset + draw_call_model.index_offset

            submesh_index_offset += len(submesh_model.ib)

        return total_ib, obj_name_draw_offset

    def _assemble_split_ib_and_draw_offset(self, submesh_vertex_base_dict: dict) -> tuple[dict, dict, int]:
        submesh_ib_dict = {}
        obj_name_draw_offset = {}
        total_index_count = 0

        for submesh_model in self.submesh_model_list:
            vertex_base = submesh_vertex_base_dict.get(submesh_model.unique_str, 0)
            remapped_ib = [index + vertex_base for index in submesh_model.ib]
            submesh_ib_dict[submesh_model.unique_str] = remapped_ib
            total_index_count += len(remapped_ib)

            for draw_call_model in submesh_model.drawcall_model_list:
                obj_name_draw_offset[draw_call_model.obj_name] = draw_call_model.index_offset

        return submesh_ib_dict, obj_name_draw_offset, total_index_count

    def _get_exported_vertex_count(self, submesh_model: SubMeshModel) -> int:
        if submesh_model.index_vertex_id_dict:
            return len(submesh_model.index_vertex_id_dict)

        position_buffer = submesh_model.category_buffer_dict.get("Position")
        if position_buffer is None or self.d3d11_game_type is None:
            return 0

        position_stride = self.d3d11_game_type.CategoryStrideDict.get("Position", 0)
        if position_stride <= 0:
            return 0

        return int(len(position_buffer) / position_stride)

        