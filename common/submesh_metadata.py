import os
from dataclasses import dataclass, field

from ..utils.format_utils import Fatal
from ..utils.json_utils import JsonUtils
from .blueprint_export_helper import BlueprintExportHelper
from .d3d11_gametype import D3D11GameType
from .global_config import GlobalConfig
from .submesh_json import SubmeshJson


def check_and_get_submesh_json_path(unique_str: str) -> tuple[bool, str, str]:
    workspace_folder = GlobalConfig.path_workspace_folder()
    unique_str_folder = os.path.join(workspace_folder, unique_str)
    if not os.path.exists(unique_str_folder):
        return False, (
            f"unique_str '{unique_str}' 没有找到对应的提取数据。\n"
            + "请确保已从游戏中提取模型并执行「一键导入当前工作空间内容」操作。"
        ), ""

    workspace_import_json_path = os.path.join(workspace_folder, "Import.json")
    workspace_import_json = JsonUtils.LoadFromFile(workspace_import_json_path) if os.path.exists(workspace_import_json_path) else {}
    gametype_name = workspace_import_json.get(unique_str, "")

    if gametype_name:
        submesh_json_path = os.path.join(unique_str_folder, "TYPE_" + gametype_name, unique_str + ".json")
        if os.path.exists(submesh_json_path):
            return True, "", submesh_json_path

    found_type_paths = []
    found_types = []
    for dirname in os.listdir(unique_str_folder):
        if not dirname.startswith("TYPE_"):
            continue

        submesh_json_path = os.path.join(unique_str_folder, dirname, unique_str + ".json")
        if os.path.exists(submesh_json_path):
            found_type_paths.append(submesh_json_path)
            found_types.append(dirname.replace("TYPE_", ""))

    if len(found_type_paths) == 1:
        return True, "", found_type_paths[0]

    if len(found_type_paths) > 1:
        return False, (
            f"unique_str '{unique_str}' 找到以下数据类型但没有在 Import.json 中记录: {', '.join(found_types)}\n"
            + "请尝试重新执行「一键导入当前工作空间内容」操作。"
        ), ""

    return False, (
        f"unique_str '{unique_str}' 没有找到对应的 SubmeshJson。\n"
        + "请确保已从游戏中提取模型并执行「一键导入当前工作空间内容」操作。"
    ), ""


@dataclass
class SubmeshMetadata:
    unique_str: str

    submesh_json_path: str = field(init=False, default="")
    extract_gametype_folder_path: str = field(init=False, default="")
    submesh_json: SubmeshJson = field(init=False, repr=False)
    submesh_json_dict: dict = field(init=False, repr=False, default_factory=dict)
    d3d11_game_type: D3D11GameType = field(init=False, repr=False)
    work_game_type: str = field(init=False, default="")
    vertex_limit_hash: str = field(init=False, default="")
    category_hash_dict: dict = field(init=False, default_factory=dict)
    texture_markup_info_list: list = field(init=False, default_factory=list)
    part_name: str = field(init=False, default="")

    def __post_init__(self):
        exists, error_msg, submesh_json_path = check_and_get_submesh_json_path(self.unique_str)
        if not exists:
            raise Fatal(error_msg)

        self.submesh_json_path = submesh_json_path
        self.extract_gametype_folder_path = os.path.join(os.path.dirname(submesh_json_path), "")
        self.submesh_json = SubmeshJson(submesh_json_path)
        self.submesh_json_dict = self.submesh_json.JsonDict
        self.work_game_type = self.submesh_json.WorkGameType
        self.vertex_limit_hash = self.submesh_json.VertexLimitVB
        self.category_hash_dict = dict(self.submesh_json.CategoryHash)
        self.texture_markup_info_list = list(self.submesh_json.TextureMarkUpInfoList)
        self.part_name = str(
            self.submesh_json_dict.get("PartName")
            or self.submesh_json_dict.get("ComponentName")
            or self.unique_str
        )
        self.d3d11_game_type = self._build_d3d11_game_type()

    def _build_d3d11_game_type(self) -> D3D11GameType:
        draw_ib = self.unique_str.split("-")[0] if "-" in self.unique_str else self.unique_str
        datatype_node_info_list = BlueprintExportHelper.get_datatype_node_info()
        override_d3d11_element_list = None

        if datatype_node_info_list:
            for node_info in datatype_node_info_list:
                node = node_info["node"]
                if not node.is_draw_ib_matched(draw_ib):
                    continue

                tmp_json_path = node_info.get("tmp_json_path")
                if not tmp_json_path or not os.path.exists(tmp_json_path):
                    break

                datatype_tmp_json_dict = JsonUtils.LoadFromFile(tmp_json_path)
                override_d3d11_element_list = datatype_tmp_json_dict.get("D3D11ElementList")
                if override_d3d11_element_list:
                    print("使用数据类型节点的 D3D11ElementList 覆盖 SubmeshJson 配置")
                break

        return D3D11GameType.from_submesh_json_dict(
            submesh_json_dict=self.submesh_json_dict,
            file_path=self.submesh_json_path,
            override_d3d11_element_list=override_d3d11_element_list,
        )


class SubmeshMetadataResolver:
    @staticmethod
    def resolve(unique_str: str) -> SubmeshMetadata:
        return SubmeshMetadata(unique_str=unique_str)