import os 
import json
import bpy
import json
import math
import bmesh
import os


from typing import List, Dict, Union, Optional
from dataclasses import dataclass, field, asdict

from ..utils.json_utils import JsonUtils
from ..utils.format_utils import Fatal

from .global_config import GlobalConfig

from .d3d11_gametype import D3D11GameType



def check_and_get_import_json_path(unique_str: str) -> tuple[bool, str, Optional[str]]:
    '''
    检查 unique_str 对应的 import.json 是否存在
    返回: (是否存在, 错误信息, 找到的import.json路径)
    '''
    workspace_folder = GlobalConfig.path_workspace_folder()
    unique_str_folder = os.path.join(workspace_folder, unique_str)
    if not os.path.exists(unique_str_folder):
        return False, f"unique_str '{unique_str}' 没有找到对应的提取数据。\n请确保已从游戏中提取模型并执行「一键导入当前工作空间内容」操作。", None

    workspace_import_json_path = os.path.join(workspace_folder, "Import.json")
    workspace_import_json = JsonUtils.LoadFromFile(workspace_import_json_path) if os.path.exists(workspace_import_json_path) else {}
    gametypename = workspace_import_json.get(unique_str, "")

    if gametypename:
        import_json_path = os.path.join(unique_str_folder, "TYPE_" + gametypename, "import.json")
        if os.path.exists(import_json_path):
            return True, "", import_json_path

    found_type_paths = []
    found_types = []
    for dirname in os.listdir(unique_str_folder):
        if not dirname.startswith("TYPE_"):
            continue

        import_json_path = os.path.join(unique_str_folder, dirname, "import.json")
        if os.path.exists(import_json_path):
            found_type_paths.append(import_json_path)
            found_types.append(dirname.replace("TYPE_", ""))

    if len(found_type_paths) == 1:
        return True, "", found_type_paths[0]

    if len(found_type_paths) > 1:
        return False, f"unique_str '{unique_str}' 找到以下数据类型但没有在 Import.json 中记录: {', '.join(found_types)}\n请尝试重新执行「一键导入当前工作空间内容」操作。", None

    return False, f"unique_str '{unique_str}' 没有找到对应的 import.json。\n请确保已从游戏中提取模型并执行「一键导入当前工作空间内容」操作。", None

@dataclass
class TextureMarkUpInfo:
    mark_name:str = field(default="",init=False)
    mark_type:str = field(default="",init=False)
    mark_hash:str = field(default="",init=False)
    mark_slot:str = field(default="",init=False)
    mark_filename:str = field(default="",init=False)
    
    def get_resource_name(self):
        return "Resource-" + self.mark_filename.split(".")[0]
    
    def get_hash_style_filename(self):
        return self.mark_hash + "-" + self.mark_name + "." + self.mark_filename.split(".")[1]

@dataclass
class ImportConfig:
    '''
    在一键导入工作空间时，Import.json会记录导入的GameType，在生成Mod时需要用到
    所以这里我们读取Import.json来确定要从哪个提取出来的数据类型文件夹中读取
    然后读取tmp.json来初始化D3D11GameType
    '''
    unique_str: str

    draw_ib: str = field(init=False, default="")
    extract_gametype_folder_path: str = field(init=False, default="")
    d3d11GameType: D3D11GameType = field(init=False, repr=False)
    part_name: str = field(init=False, default="")

    def __post_init__(self):
        self.draw_ib = self.unique_str.split("-")[0] if "-" in self.unique_str else self.unique_str

        exists, error_msg, import_json_path = check_and_get_import_json_path(self.unique_str)
        if not exists:
            raise Fatal(error_msg)
        self.extract_gametype_folder_path = os.path.join(os.path.dirname(import_json_path), "")
        
        from .blueprint_export_helper import BlueprintExportHelper
        datatype_node_info_list = BlueprintExportHelper.get_datatype_node_info()
        
        matched_datatype_node_info = None
        if datatype_node_info_list:
            for node_info in datatype_node_info_list:
                node = node_info["node"]
                if node.is_draw_ib_matched(self.draw_ib):
                    matched_datatype_node_info = node_info
                    print(f"找到匹配的数据类型节点，DrawIB: {self.draw_ib}, 节点: {node.name}")
                    break

        if matched_datatype_node_info and matched_datatype_node_info.get("tmp_json_path") and os.path.exists(matched_datatype_node_info["tmp_json_path"]):
            with open(import_json_path, 'r', encoding='utf-8') as f:
                base_tmp_json_dict = json.load(f)
            with open(matched_datatype_node_info["tmp_json_path"], 'r', encoding='utf-8') as f:
                datatype_tmp_json_dict = json.load(f)
            
            if "D3D11ElementList" in datatype_tmp_json_dict:
                base_tmp_json_dict["D3D11ElementList"] = datatype_tmp_json_dict["D3D11ElementList"]
                print(f"使用数据类型节点的 D3D11ElementList 覆盖原始配置")
            
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
                json.dump(base_tmp_json_dict, f, indent=2, ensure_ascii=False)
                merged_tmp_json_path = f.name
            
            self.d3d11GameType:D3D11GameType = D3D11GameType(merged_tmp_json_path)
            tmp_json_dict = base_tmp_json_dict
            
            try:
                os.unlink(merged_tmp_json_path)
            except:
                pass
        else:
            self.d3d11GameType:D3D11GameType = D3D11GameType(import_json_path)
            tmp_json_dict = JsonUtils.LoadFromFile(import_json_path)
        
        '''
        读取 import.json 中的内容，后续会用于生成Mod的 ini 文件
        需要在确定了D3D11GameType之后再执行
        注意：这里使用已经确定的 tmp_json_dict
        '''
        raw_part_name_list = tmp_json_dict["PartNameList"]
        self.part_name = str(raw_part_name_list[0]) if raw_part_name_list else ""

        print("读取配置: " + import_json_path)





                


                


