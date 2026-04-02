from ..base.config.main_config import GlobalConfig
from ..utils.json_utils import JsonUtils
from ..utils.collection_utils import CollectionUtils, CollectionColor
import os
import bpy
from typing import List, Dict, Union
from dataclasses import dataclass, field, asdict

@dataclass
class DedupedTextureInfo:
    original_hash:str = field(default="",init=False)
    render_hash:str = field(default="",init=False)
    format:str = field(default="",init=False)
    componet_count_list_str:str = field(default="",init=False)


class WorkSpaceHelper:

    @staticmethod
    def get_ordered_gpu_cpu_import_folderpath_list(submesh_folderpath:str)-> List[str]:
        # 导入时，要按照先GPU类型，再CPU类型进行排序
        gpu_import_folder_path_list = []
        cpu_import_folder_path_list = []

        dirs = os.listdir(submesh_folderpath)
        for dirname in dirs:
            if not dirname.startswith("TYPE_"):
                continue
            final_import_folder_path = os.path.join(submesh_folderpath,dirname)
            if dirname.startswith("TYPE_GPU"):
                gpu_import_folder_path_list.append(final_import_folder_path)
            elif dirname.startswith("TYPE_CPU"):
                cpu_import_folder_path_list.append(final_import_folder_path)

        final_import_folder_path_list = []
        for gpu_path in gpu_import_folder_path_list:
            final_import_folder_path_list.append(gpu_path)
        for cpu_path in cpu_import_folder_path_list:
            final_import_folder_path_list.append(cpu_path)

        return final_import_folder_path_list

    @staticmethod
    def create_and_get_workspace_collection() -> bpy.types.Collection:
        # 这里先创建以当前工作空间为名称的集合，并且链接到scene，确保它存在
        workspace_collection = CollectionUtils.create_new_collection(collection_name=GlobalConfig.workspacename,color_tag=CollectionColor.Red)
        bpy.context.scene.collection.children.link(workspace_collection)
        return workspace_collection

    @staticmethod
    def get_submesh_folderpath_list() -> List[str]:
        '''
        获取当前工作空间文件夹下面的所有SubMesh文件夹（仅保留名字包含至少两个 '-' 的文件夹）
        导入时就已经确保它至少包含两个'-'，即符合命名规则
        '''
        submesh_folderpath_list = []
        for f in os.scandir(GlobalConfig.path_workspace_folder()):
            if not f.is_dir():
                continue
            name_splits = f.name.split('-')
            if len(name_splits) >= 3:
                submesh_folderpath_list.append(f.path)
            
        return submesh_folderpath_list

    @staticmethod
    def get_drawib_aliasname_dict() -> Dict[str,str]:
        '''
        从当前工作空间目录下的Config.json里读取DrawIB和别名的对应关系
        '''
        drawib_aliasname_dict = {}

        # 如果工作空间下存在Config.json就尝试获取其别名
        config_json_path = GlobalConfig.path_drawib_config_json_path()
        if os.path.exists(config_json_path):
            config_json = JsonUtils.LoadFromFile(config_json_path)
            # 读取每个DrawIB的别名到字典里，键是DrawIB名称，值是别名
            if isinstance(config_json, list):
                for item in config_json:
                    if not isinstance(item, dict):
                        continue
                    draw_ib = str(item.get("DrawIB", "")).strip()
                    alias_name = str(item.get("Alias", "")).strip()
                    if draw_ib:
                        drawib_aliasname_dict[draw_ib] = alias_name
        return drawib_aliasname_dict
    

    @staticmethod
    def get_hash_deduped_texture_info_dict(submesh_folder_name:str) -> Dict[str,DedupedTextureInfo]:

        draw_ib_folder_path = GlobalConfig.path_workspace_folder() + submesh_folder_name + "\\"
        # 接下来计算ComponentList，也就是当前DrawIB使用到这个贴图的所有Component的Count，从1开始
        component_name__drawcall_indexlist_json_path = os.path.join(draw_ib_folder_path,"ComponentName_DrawCallIndexList.json")
        trianglelist_deduped_filename_json_path = os.path.join(draw_ib_folder_path,"TrianglelistDedupedFileName.json")

        component_name__drawcall_indexlist_json_dict = JsonUtils.LoadFromFile(component_name__drawcall_indexlist_json_path)

        drawcall_component_count_dict = {}
        for component_name, drawcall_indexlist in component_name__drawcall_indexlist_json_dict.items():
            for drawcall_index in drawcall_indexlist:
                drawcall_component_count_dict[drawcall_index] = component_name.split(" ")[1]  # 取Component的Count部分

        trianglelist_deduped_filename_json_dict = JsonUtils.LoadFromFile(trianglelist_deduped_filename_json_path)


        deduped_filename_drawcall_index_list_dict = {}
        for trianglelist_deduped_filename,deduped_kv_dict in trianglelist_deduped_filename_json_dict.items():
            deduped_filename:str = deduped_kv_dict["FALogDedupedFileName"]
            draw_call_index:str = trianglelist_deduped_filename[0:6]

            drawcall_index_list = deduped_filename_drawcall_index_list_dict.get(deduped_filename,[])
            if draw_call_index not in drawcall_index_list:
                drawcall_index_list.append(draw_call_index)

            deduped_filename_drawcall_index_list_dict[deduped_filename] = drawcall_index_list

        hash_deduped_texture_info_dict = {}

        for deduped_filename, drawcall_index_list in deduped_filename_drawcall_index_list_dict.items():
            used_component_count_list = []

            original_hash = deduped_filename.split("_")[0]
            render_hash = deduped_filename.split("_")[1].split("-")[0]

            # 从类似于 "b7ff7a6e_03d46264-R8G8B8A8_UNORM_SRGB.dds" 的文件名中
            # 提取出 "R8G8B8A8_UNORM_SRGB" 部分：
            # - 去掉扩展名
            # - 找到第一个下划线 `_` 的位置
            # - 从该下划线之后查找第一个连字符 `-`，并取其后到文件名末尾的子串
            # - 如果找不到上述模式，则退回到以最后一个 `-` 分割并取最后一段的策略
            base_name = os.path.splitext(deduped_filename)[0]
            fmt = ""
            try:
                first_underscore = base_name.find("_")
                if first_underscore != -1:
                    dash_after_underscore = base_name.find("-", first_underscore + 1)
                    if dash_after_underscore != -1:
                        fmt = base_name[dash_after_underscore + 1:]
                # fallback: use last '-' part
                if not fmt:
                    if "-" in base_name:
                        fmt = base_name.rsplit("-", 1)[-1]
                    else:
                        # as ultimate fallback, if there is an underscore then maybe format is after the second underscore
                        parts = base_name.split("_")
                        if len(parts) > 2:
                            fmt = parts[-1]
                        else:
                            fmt = ""
                # strip any stray whitespace
                fmt = fmt.strip()
            except Exception:
                fmt = ""

            format = fmt

            print(format)

            for draw_call_index in drawcall_index_list:
                matched_component_count = drawcall_component_count_dict.get(draw_call_index,"")
                if matched_component_count != "":
                    if matched_component_count not in used_component_count_list:
                        used_component_count_list.append(matched_component_count)

            used_component_count_list.sort()
            # print(used_component_count_list)

            

            componet_count_list_str = ""
            for unique_component_count_str in used_component_count_list:
                componet_count_list_str = componet_count_list_str + unique_component_count_str + "."

            deduped_texture_info = DedupedTextureInfo()
            deduped_texture_info.original_hash = original_hash
            deduped_texture_info.render_hash = render_hash
            deduped_texture_info.format = format
            deduped_texture_info.componet_count_list_str = componet_count_list_str

            hash_deduped_texture_info_dict[original_hash] = deduped_texture_info
    
        return hash_deduped_texture_info_dict