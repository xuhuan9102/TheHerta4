import os
import bpy
from dataclasses import dataclass, field

from ..utils.format_utils import Fatal
from ..utils.json_utils import JsonUtils
from ..blueprint.export_helper import BlueprintExportHelper
from ..blueprint.node_datatype import (
    reset_datatype_override_log,
    build_override_element_list,
)
from .d3d11_gametype import D3D11GameType
from .global_config import GlobalConfig
from .submesh_json import SubmeshJson


def check_and_get_submesh_json_path(unique_str: str) -> tuple[bool, str, str]:
    """检查并获取 submesh JSON 文件路径

    Args:
        unique_str: 唯一标识符

    Returns:
        (是否存在, 错误信息, JSON 文件路径)
    """
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
    """Submesh 元数据类

    用于存储和管理 submesh 的元数据信息，包括 JSON 配置、数据类型等。
    """
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
        """初始化后处理"""
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
        """构建 D3D11GameType 对象

        检查是否有数据类型节点需要覆盖，如果有则使用节点配置替换原始数据类型。

        Returns:
            D3D11GameType 对象
        """
        # 从 unique_str 中提取 draw_ib
        draw_ib = self.unique_str.split("-")[0] if "-" in self.unique_str else self.unique_str

        # 获取数据类型节点信息
        datatype_node_info_list = BlueprintExportHelper.get_datatype_node_info()
        override_d3d11_element_list = None

        if datatype_node_info_list:
            for node_info in datatype_node_info_list:
                node = node_info["node"]

                # 检查节点是否匹配当前 draw_ib
                if not node.is_draw_ib_matched(draw_ib):
                    continue

                # 获取配置文件路径
                tmp_json_path = node_info.get("tmp_json_path")
                if not tmp_json_path:
                    break

                # 处理文件路径
                raw_path = tmp_json_path.strip()
                if os.path.isabs(raw_path):
                    abs_json_path = raw_path
                else:
                    abs_json_path = bpy.path.abspath(raw_path)

                if not os.path.exists(abs_json_path):
                    break

                # 获取加载的配置数据
                loaded_data = node_info.get("loaded_data", {})
                if not loaded_data:
                    break

                # 调用节点模块的函数构建覆盖后的 D3D11ElementList
                original_category_buffers = self.submesh_json_dict.get("CategoryBufferList", [])
                override_d3d11_element_list = build_override_element_list(
                    original_category_buffers,
                    loaded_data,
                    draw_ib
                )
                break

        return D3D11GameType.from_submesh_json_dict(
            submesh_json_dict=self.submesh_json_dict,
            file_path=self.submesh_json_path,
            override_d3d11_element_list=override_d3d11_element_list,
        )


class SubmeshMetadataResolver:
    """Submesh 元数据解析器"""

    @staticmethod
    def resolve(unique_str: str) -> SubmeshMetadata:
        """解析 submesh 元数据

        Args:
            unique_str: 唯一标识符

        Returns:
            SubmeshMetadata 对象
        """
        return SubmeshMetadata(unique_str=unique_str)
