
from dataclasses import field, dataclass
import os

from ..d3d11.d3d11_gametype import D3D11GameType
from ...helper.import_config import TextureMarkUpInfo
from ...base.config.main_config import GlobalConfig
from ...base.utils.json_utils import JsonUtils

import numpy

from .submesh_model import SubMeshModel

@dataclass
class DrawIBModel:
    '''
    - DrawIBModel是一个更高层次的模型，包含一个或多个SubMeshModel
    - 适用于米游、Unity等需要将多个SubMesh组合成一个DrawIB进行导出的游戏
    - 要使用DrawIBModle，必须确保每个SubMesh的数据类型是相同的，才能组合在一起
    '''
    submesh_model_list:list[SubMeshModel]
    combine_ib:bool = True

    draw_ib:str = field(init=False, default="")
    draw_ib_alias:str = field(init=False, default="")

    vertex_count:int = field(init=False, default=0)
    index_count:int = field(init=False, default=0)

    # 这里的d3d11_game_type有一个隐含条件
    # DrawIBModel一旦创建，就默认它里面的每个SubmeshModel的d3d11_game_type都是一样的
    # 否则不可能通过组合buffer数据来正确导出
    # 所以这里的d3d11_game_type可以直接取submesh_model_list中第一个SubMeshModel的d3d11_game_type
    d3d11_game_type:D3D11GameType = field(init=False,repr=False,default=None)

    import_json_path:str = field(init=False,repr=False,default="")
    import_json_dict:dict = field(init=False,repr=False,default_factory=dict)
    import_config:object = field(init=False,repr=False,default=None)
    category_hash_dict:dict = field(init=False,repr=False,default_factory=dict)
    part_name_list:list = field(init=False,repr=False,default_factory=list)
    match_first_index_list:list = field(init=False,repr=False,default_factory=list)
    vshash_list:list = field(init=False,repr=False,default_factory=list)
    partname_texturemarkinfolist_dict:dict = field(init=False,repr=False,default_factory=dict)
    vertex_limit_hash:str = field(init=False,repr=False,default="")
    original_vertex_count:int = field(init=False,repr=False,default=0)

    ib:list = field(init=False,repr=False,default_factory=list)
    submesh_ib_dict:dict = field(init=False,repr=False,default_factory=dict)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict)
    obj_name_draw_offset:dict = field(init=False,repr=False,default_factory=dict)
    shapekey_name_bytelist_dict:dict = field(init=False,repr=False,default_factory=dict)


    def __post_init__(self):
        # 因为初始化时传入的SubMeshModel列表中的每个SubMeshModel的match_draw_ib都是一样的，所以直接取第一个就行了
        self.draw_ib = self.submesh_model_list[0].match_draw_ib if len(self.submesh_model_list) > 0 else ""
        self.draw_ib_alias = self.draw_ib
        self.d3d11_game_type = self.submesh_model_list[0].d3d11_game_type if len(self.submesh_model_list) > 0 else None
        self._load_import_metadata_from_first_submesh()

        category_buffer_dict, submesh_vertex_base_dict, index_vertex_id_dict, vertex_count = self._assemble_category_buffers()

        self.vertex_count = vertex_count
        self.category_buffer_dict = category_buffer_dict
        self.index_vertex_id_dict = index_vertex_id_dict
        self.shapekey_name_bytelist_dict = self._assemble_shape_key_buffers()

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

    def _load_import_metadata_from_first_submesh(self):
        if not self.submesh_model_list:
            print("DrawIBModel: submesh_model_list 为空，无法读取导入元数据")
            return

        first_submesh = self.submesh_model_list[0]
        folder_name = first_submesh.unique_str
        print("DrawIBModel: 开始读取导入元数据，DrawIB: " + self.draw_ib + "，unique_str: " + folder_name)
        workspace_import_json_path = os.path.join(GlobalConfig.path_workspace_folder(), "Import.json")
        workspace_import_json = JsonUtils.LoadFromFile(workspace_import_json_path) if os.path.exists(workspace_import_json_path) else {}
        gametype_name = workspace_import_json.get(folder_name, "")
        if gametype_name:
            print("DrawIBModel: 命中工作空间 Import.json，GameType: " + gametype_name)
        else:
            print("DrawIBModel: 工作空间 Import.json 未记录 unique_str 对应的 GameType: " + folder_name)

        if gametype_name:
            self.import_json_path = os.path.join(
                GlobalConfig.path_workspace_folder(),
                folder_name,
                "TYPE_" + gametype_name,
                "import.json",
            )
            if os.path.exists(self.import_json_path):
                self.import_json_dict = JsonUtils.LoadFromFile(self.import_json_path)
                print("DrawIBModel: 已读取新结构 import.json: " + self.import_json_path)
            else:
                print("DrawIBModel: 未找到新结构 import.json: " + self.import_json_path)

        if self.import_json_dict:
            self.category_hash_dict = dict(self.import_json_dict.get("CategoryHash", {}))
            self.part_name_list = list(self.import_json_dict.get("PartNameList", []))
            self.match_first_index_list = list(self.import_json_dict.get("MatchFirstIndex", []))
            self.vshash_list = list(self.import_json_dict.get("VSHashList", []))
            self.partname_texturemarkinfolist_dict = self._load_texture_markup_info_from_all_submeshes(workspace_import_json)
            self.vertex_limit_hash = self.import_json_dict.get("VertexLimitVB", "")
            self.original_vertex_count = self.import_json_dict.get("OriginalVertexCount", 0)
            print(
                "DrawIBModel: 已使用新结构元数据，Part数量: "
                + str(len(self.part_name_list))
                + "，贴图标记Part数量: "
                + str(len(self.partname_texturemarkinfolist_dict))
            )
            return

        self.import_config = None
        print("DrawIBModel: 未读取到新结构元数据，贴图标记信息为空，DrawIB: " + self.draw_ib)

    def _get_import_json_path_by_unique_str(self, workspace_import_json: dict, unique_str: str) -> str:
        gametype_name = workspace_import_json.get(unique_str, "")
        if not gametype_name:
            return ""

        return os.path.join(
            GlobalConfig.path_workspace_folder(),
            unique_str,
            "TYPE_" + gametype_name,
            "import.json",
        )

    def _get_part_name_for_submesh(self, submesh_model: SubMeshModel) -> str:
        if submesh_model.match_first_index in self.match_first_index_list:
            part_index = self.match_first_index_list.index(submesh_model.match_first_index)
            if part_index < len(self.part_name_list):
                return self.part_name_list[part_index]

        return ""

    def _load_texture_markup_info_from_all_submeshes(self, workspace_import_json: dict) -> dict:
        merged_texture_markup_info_dict = {}

        for submesh_model in self.submesh_model_list:
            unique_str = submesh_model.unique_str
            part_name = self._get_part_name_for_submesh(submesh_model)
            import_json_path = self._get_import_json_path_by_unique_str(workspace_import_json, unique_str)

            print(
                "DrawIBModel: 读取贴图标记，unique_str: "
                + unique_str
                + "，part_name: "
                + str(part_name)
                + "，import.json: "
                + import_json_path
            )

            if not import_json_path or not os.path.exists(import_json_path):
                print("DrawIBModel: 跳过贴图标记读取，import.json 不存在: " + import_json_path)
                continue

            submesh_import_json_dict = JsonUtils.LoadFromFile(import_json_path)
            raw_texture_markup_info_dict = submesh_import_json_dict.get("ComponentTextureMarkUpInfoListDict", {})
            normalized_texture_markup_info_dict = self._normalize_texture_markup_info_dict(raw_texture_markup_info_dict)

            if not normalized_texture_markup_info_dict:
                print("DrawIBModel: 当前 submesh 没有贴图标记: " + unique_str)
                continue

            if part_name:
                texture_markup_info_list = normalized_texture_markup_info_dict.get(part_name)
                if texture_markup_info_list is None:
                    texture_markup_info_list = normalized_texture_markup_info_dict.get(unique_str, [])

                if texture_markup_info_list:
                    merged_texture_markup_info_dict[part_name] = texture_markup_info_list
                    print(
                        "DrawIBModel: 已合并贴图标记到 Part "
                        + part_name
                        + "，数量: "
                        + str(len(texture_markup_info_list))
                    )
                else:
                    print(
                        "DrawIBModel: 当前 submesh 的贴图标记键未匹配到 Part，unique_str: "
                        + unique_str
                        + "，可用键: "
                        + str(list(normalized_texture_markup_info_dict.keys()))
                    )
            else:
                print("DrawIBModel: 当前 submesh 未匹配到 PartName，unique_str: " + unique_str)

        return merged_texture_markup_info_dict

    def _normalize_texture_markup_info_dict(self, raw_texture_info_dict: dict) -> dict:
        normalized_texture_info_dict = {}

        for part_name, texture_info_list in raw_texture_info_dict.items():
            normalized_texture_info_list = []
            for texture_info in texture_info_list:
                if isinstance(texture_info, TextureMarkUpInfo):
                    normalized_texture_info_list.append(texture_info)
                    continue

                if not isinstance(texture_info, dict):
                    continue

                markup_info = TextureMarkUpInfo()
                markup_info.mark_name = texture_info.get("MarkName", texture_info.get("mark_name", ""))
                markup_info.mark_type = texture_info.get("MarkType", texture_info.get("mark_type", ""))
                markup_info.mark_hash = texture_info.get("MarkHash", texture_info.get("mark_hash", ""))
                markup_info.mark_slot = texture_info.get("MarkSlot", texture_info.get("mark_slot", ""))
                markup_info.mark_filename = texture_info.get("MarkFileName", texture_info.get("mark_filename", ""))
                normalized_texture_info_list.append(markup_info)

            normalized_texture_info_dict[part_name] = normalized_texture_info_list

        return normalized_texture_info_dict

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

    def _assemble_shape_key_buffers(self) -> dict:
        if self.d3d11_game_type is None:
            return {}

        position_stride = self.d3d11_game_type.CategoryStrideDict.get("Position", 0)
        if position_stride <= 0:
            return {}

        position_offset = self._get_category_byte_offset("Position")
        ordered_shapekey_names = []
        seen_shapekey_names = set()

        for submesh_model in self.submesh_model_list:
            for shapekey_name in submesh_model.shape_key_buffer_dict.keys():
                if shapekey_name in seen_shapekey_names:
                    continue
                seen_shapekey_names.add(shapekey_name)
                ordered_shapekey_names.append(shapekey_name)

        if not ordered_shapekey_names:
            return {}

        shapekey_chunks = {shapekey_name: [] for shapekey_name in ordered_shapekey_names}

        for submesh_model in self.submesh_model_list:
            base_position_buffer = submesh_model.category_buffer_dict.get("Position")

            for shapekey_name in ordered_shapekey_names:
                shape_key_buffer_result = submesh_model.shape_key_buffer_dict.get(shapekey_name)

                if shape_key_buffer_result is None:
                    if base_position_buffer is not None:
                        shapekey_chunks[shapekey_name].append(base_position_buffer)
                    continue

                position_bytes = self._extract_position_bytes_from_shape_key(
                    shape_key_buffer_result.element_vertex_ndarray,
                    position_offset,
                    position_stride,
                )
                shapekey_chunks[shapekey_name].append(position_bytes)

        return {
            shapekey_name: numpy.concatenate(chunks)
            for shapekey_name, chunks in shapekey_chunks.items()
            if chunks
        }

    def _get_category_byte_offset(self, target_category: str) -> int:
        byte_offset = 0

        for category_name in self.d3d11_game_type.OrderedCategoryNameList:
            if category_name == target_category:
                return byte_offset
            byte_offset += self.d3d11_game_type.CategoryStrideDict.get(category_name, 0)

        return byte_offset

    def _extract_position_bytes_from_shape_key(
        self,
        element_vertex_ndarray: numpy.ndarray,
        position_offset: int,
        position_stride: int,
    ) -> numpy.ndarray:
        if len(element_vertex_ndarray) == 0:
            return numpy.array([], dtype=numpy.uint8)

        full_byte_view = element_vertex_ndarray.view(numpy.uint8).reshape(len(element_vertex_ndarray), -1)
        return full_byte_view[:, position_offset:position_offset + position_stride].reshape(-1)

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

    @property
    def draw_number(self) -> int:
        return self.vertex_count

    @property
    def d3d11GameType(self) -> D3D11GameType:
        return self.d3d11_game_type

    @property
    def part_name_submesh_dict(self) -> dict:
        mapping = {}

        for submesh_model in self.submesh_model_list:
            part_name = None
            if submesh_model.match_first_index in self.match_first_index_list:
                part_index = self.match_first_index_list.index(submesh_model.match_first_index)
                if part_index < len(self.part_name_list):
                    part_name = self.part_name_list[part_index]
            if part_name is None:
                continue
            mapping[part_name] = submesh_model

        return mapping

    @property
    def PartName_IBResourceName_Dict(self) -> dict:
        return {
            part_name: "Resource_" + self.draw_ib + "_Component" + part_name
            for part_name in self.part_name_list
        }

    @property
    def PartName_IBBufferFileName_Dict(self) -> dict:
        result = {}
        for part_name, submesh_model in self.part_name_submesh_dict.items():
            result[part_name] = submesh_model.unique_str + "-Index.buf"
        return result

    @property
    def componentname_ibbuf_dict(self) -> dict:
        result = {}
        for part_name, submesh_model in self.part_name_submesh_dict.items():
            result["Component " + part_name] = self.submesh_ib_dict.get(submesh_model.unique_str, [])
        return result

        