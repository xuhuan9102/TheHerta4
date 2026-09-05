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
from .workspace_helper import WorkSpaceHelper


def check_and_get_submesh_json_path(unique_str: str) -> tuple[bool, str, str]:
    """检查并获取 submesh JSON 文件路径

    Args:
        unique_str: 唯一标识符

    Returns:
        (是否存在, 错误信息, JSON 文件路径)
    """
    workspace_folder = GlobalConfig.path_workspace_folder()
    _lod_name, bare_unique_str = WorkSpaceHelper.parse_lod_unique_str(unique_str)
    unique_str_folder = WorkSpaceHelper.get_submesh_folder_path(unique_str)
    if not os.path.exists(unique_str_folder):
        return False, (
            f"unique_str '{unique_str}' 没有找到对应的提取数据。\n"
            + "请确保已从游戏中提取模型并执行「一键导入当前工作空间内容」操作。"
        ), ""

    workspace_import_json_path = os.path.join(workspace_folder, "Import.json")
    workspace_import_json = JsonUtils.LoadFromFile(workspace_import_json_path) if os.path.exists(workspace_import_json_path) else {}
    gametype_name = workspace_import_json.get(unique_str, "")

    if gametype_name:
        submesh_json_path = os.path.join(unique_str_folder, "TYPE_" + gametype_name, bare_unique_str + ".json")
        if os.path.exists(submesh_json_path):
            return True, "", submesh_json_path

    found_type_paths = []
    found_types = []
    for dirname in os.listdir(unique_str_folder):
        if not dirname.startswith("TYPE_"):
            continue

        submesh_json_path = os.path.join(unique_str_folder, dirname, bare_unique_str + ".json")
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
    match_cs: str = field(init=False, default="")
    match_uav_bytes: int = field(init=False, default=0)
    category_hash_dict: dict = field(init=False, default_factory=dict)
    texture_markup_info_list: list = field(init=False, default_factory=list)
    part_name: str = field(init=False, default="")
    # EFMI 骨骼合并元数据（由反查写回 json；无则默认 0/空）
    vg_offset: int = field(init=False, default=0)
    vg_count: int = field(init=False, default=0)
    vg_map: dict = field(init=False, default_factory=dict)
    # ZZMI VGMap 缓存算法版本；导出侧拒绝陈旧缓存，避免旧分组/门控结果继续生效。
    vg_map_algorithm_version: int = field(init=False, default=0)
    merged_skeleton_metadata_valid: bool = field(init=False, default=True)
    # EFMI 跨 LOD 对应账本：不直接参与槽位编号；投影开启时用于 LOD1 分区
    # 约束、未匹配过滤及自动匹配节点的物体配对，关闭时保留为诊断元数据。
    # 顶点组映射仍由节点基于实际导入物体重新计算，不能直接从账本生成。
    efmi_lod_reference_lod: str = field(init=False, default="")
    efmi_lod_reference_component: str = field(init=False, default="")
    efmi_lod_correspondence: dict = field(init=False, default_factory=dict)
    efmi_lod_projection: bool = field(init=False, default=False)
    efmi_lod_layout_version: int = field(init=False, default=0)
    # ZZMI 导出侧守卫元数据（反查写回；缺省 0）
    deform_draw_index: int = field(init=False, default=0)
    original_vertex_count: int = field(init=False, default=0)

    def __post_init__(self):
        """初始化后处理"""
        exists, error_msg, submesh_json_path = check_and_get_submesh_json_path(self.unique_str)
        if not exists:
            raise Fatal(error_msg)

        self.submesh_json_path = submesh_json_path
        self.extract_gametype_folder_path = os.path.join(os.path.dirname(submesh_json_path), "")
        self.submesh_json = SubmeshJson(submesh_json_path)
        self.submesh_json_dict = self.submesh_json.JsonDict
        self.merged_skeleton_metadata_valid = bool(
            getattr(self.submesh_json, "MergedSkeletonMetadataValid", True)
        )
        self.work_game_type = self.submesh_json.WorkGameType
        self.vertex_limit_hash = self.submesh_json.VertexLimitVB
        self.match_cs = self.submesh_json.MatchCS
        self.match_uav_bytes = self.submesh_json.MatchUAVBytes
        self.category_hash_dict = dict(self.submesh_json.CategoryHash)
        self.texture_markup_info_list = list(self.submesh_json.TextureMarkUpInfoList)
        self.part_name = str(
            self.submesh_json_dict.get("PartName")
            or self.submesh_json_dict.get("ComponentName")
            or self.unique_str
        )
        # EFMI 骨骼合并元数据（反查写回的 VGOffset/VGCount/VGMap）
        self.vg_offset = int(self.submesh_json_dict.get("VGOffset", 0) or 0)
        self.vg_count = int(self.submesh_json_dict.get("VGCount", 0) or 0)
        self.vg_map = dict(self.submesh_json_dict.get("VGMap", {}) or {})
        self.vg_map_algorithm_version = int(
            self.submesh_json_dict.get("VGMapAlgorithmVersion", 0) or 0
        )
        # EFMI 跨 LOD 对应账本（v9 投影写回；行 = target_local -> {local_vg_id: ref_local, ...}）
        corr = self.submesh_json_dict.get("EFMILODCorrespondence", {}) or {}
        self.efmi_lod_correspondence = {
            str(k): dict(v) for k, v in corr.items()
        }
        self.efmi_lod_reference_lod = str(
            self.submesh_json_dict.get("EFMILODReference", "") or ""
        )
        ref_component = ""
        for row in self.efmi_lod_correspondence.values():
            ref_component = str(row.get("reference_component", "") or "")
            if ref_component:
                break
        self.efmi_lod_reference_component = ref_component
        self.efmi_lod_projection = bool(
            self.submesh_json_dict.get("EFMILODProjection", False)
        )
        self.efmi_lod_layout_version = int(
            self.submesh_json_dict.get("EFMILODLayoutVersion", 0) or 0
        )
        # ZZMI 骨架分组号（渲染 cb1 对象变换配对；缺省 0 = 单骨架旧语义）
        self.skeleton_group = int(self.submesh_json_dict.get("SkeletonGroup", 0) or 0)
        # ZZMI 导出侧守卫元数据：deform pass draw 序号 + 原部件顶点数
        self.deform_draw_index = int(self.submesh_json_dict.get("DeformDrawIndex", 0) or 0)
        self.original_vertex_count = int(self.submesh_json_dict.get("OriginalVertexCount", 0) or 0)
        self.d3d11_game_type = self._build_d3d11_game_type()

    def _build_d3d11_game_type(self) -> D3D11GameType:
        """构建 D3D11GameType 对象

        检查是否有数据类型节点需要覆盖，如果有则使用节点配置替换原始数据类型。

        Returns:
            D3D11GameType 对象
        """
        # 从 unique_str 中提取 draw_ib
        _lod_name, bare_unique_str = WorkSpaceHelper.parse_lod_unique_str(self.unique_str)
        draw_ib = bare_unique_str.split("-")[0] if "-" in bare_unique_str else bare_unique_str

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


# ---------------------------------------------------------------------------
# 工作空间格式解析辅助（供形态键配置 / UV 偏移生成等按 IB 动态取格式）
#
# 形态键配置生成与 UV 偏移生成原本都要求用户手动提供顶点属性 / UV 属性
# 定义（字节布局），一旦多个 IB 的真实布局不一致（如 9 个 16 字节 + 1 个
# 40 字节的 -Position.buf），手填的单一格式就会把其余 IB 写坏。
# 这里统一从工作空间 SubmeshJson（CategoryBufferList 的 D3D11ElementList）
# 解析每个 IB 的真实格式 —— 与导出管线完全同源（含数据类型覆盖节点），
# 失败时静默返回 None，由调用方回退到旧的节点/默认推断。
# ---------------------------------------------------------------------------

_workspace_game_type_cache: dict = {}
_workspace_game_type_prefix_cache: dict = {}


def clear_workspace_game_type_cache() -> None:
    """工作空间切换 / 重新导入后调用，避免旧缓存继续生效。"""
    _workspace_game_type_cache.clear()
    _workspace_game_type_prefix_cache.clear()


def resolve_workspace_game_type(unique_str: str):
    """按 unique_str（可含 LOD 前缀）解析工作空间游戏类型；失败返回 None。

    与导出管线同源（SubmeshMetadataResolver），会并入数据类型覆盖节点的影响。
    """
    key = str(unique_str or "").strip()
    if not key:
        return None
    cached = _workspace_game_type_cache.get(key, _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached
    game_type = None
    try:
        game_type = SubmeshMetadataResolver.resolve(key).d3d11_game_type
    except Exception:
        game_type = None
    _workspace_game_type_cache[key] = game_type
    return game_type


def resolve_workspace_game_type_by_prefix(draw_ib_prefix: str):
    """按 8 位 IB 前缀解析工作空间游戏类型（自动覆盖 LOD 分区与分区工作空间）。

    同一前缀存在多个子网格时取第一个可解析的；同一 DrawIB 的各子网格布局一致
    （DrawIBModel 的合并前提），因此首个可解析结果即可代表该 IB。
    """
    prefix = str(draw_ib_prefix or "").strip()
    if not prefix:
        return None
    cache_key = "prefix:" + prefix.casefold()
    cached = _workspace_game_type_prefix_cache.get(cache_key, _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached
    game_type = None
    prefix_lower = prefix.casefold()
    try:
        for record in WorkSpaceHelper.get_submesh_folder_records():
            bare_name = str(record.get("bare_name", "") or "").strip()
            if not bare_name:
                continue
            bare_lower = bare_name.casefold()
            if bare_lower != prefix_lower and not bare_lower.startswith(prefix_lower + "-"):
                continue
            lod_name = str(record.get("lod_name", "") or "").strip()
            identity = bare_name if not lod_name else f"{lod_name}.{bare_name}"
            candidate = resolve_workspace_game_type(identity)
            if candidate is not None:
                game_type = candidate
                break
    except Exception:
        game_type = None
    _workspace_game_type_prefix_cache[cache_key] = game_type
    return game_type


def resolve_workspace_category_stride(unique_str: str, category: str) -> int:
    """按 unique_str 解析工作空间指定类别（Position/Texcoord/...）的字节步长。"""
    game_type = resolve_workspace_game_type(unique_str)
    if game_type is None:
        return 0
    return int((getattr(game_type, "CategoryStrideDict", {}) or {}).get(category, 0) or 0)


def resolve_workspace_category_elements(unique_str: str, category: str) -> list:
    """按 unique_str 解析工作空间指定类别的元素列表（顺序即流内顺序）。"""
    game_type = resolve_workspace_game_type(unique_str)
    if game_type is None:
        return []
    category_upper = str(category or "").upper()
    return [
        element
        for element in getattr(game_type, "D3D11ElementList", []) or []
        if str(getattr(element, "Category", "") or "").upper() == category_upper
    ]


class _Unresolved:
    __slots__ = ()


_UNRESOLVED = _Unresolved()
