import contextlib
import os
import re
import shutil

from .m_ini_builder import *
from .m_key import M_Key
from .draw_call_model import DrawCallModel
from .drawib_model import DrawIBModel
from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .logic_name import LogicName

from .global_key_count_helper import GlobalKeyCountHelper
from .workspace_helper import WorkSpaceHelper
from ..blueprint.export_helper import BlueprintExportHelper
from ..utils.log_utils import LOG

# 导出段登记表（纯 Python、无 bpy 依赖）：在 draw_ib 上下文里构造的 M_IniSection
# 会在构造时快照所属 draw_ib，写盘时整批登记，供导出后的「条件判断」后处理节点反查。
try:
    from . import export_section_registry as _section_registry
except Exception:  # 独立加载本文件时兜底：退化为无登记，不影响导出
    _section_registry = None

class M_IniHelper:
    """INI 辅助工具类
    
    提供生成 INI 配置的各种辅助方法，包括：
    - drawindexed 命令生成
    - Hash 风格贴图配置
    - Slot 风格贴图复制
    - 形态键配置生成
    - 分支按键配置生成
    """
    
    @classmethod
    def _count_marked_textures(cls, draw_ib_model: DrawIBModel, mark_type: str | None = None) -> int:
        """统计标记的贴图数量
        
        Args:
            draw_ib_model: DrawIB 模型实例
            mark_type: 贴图标记类型（Slot 或 Hash），为 None 时统计所有类型
            
        Returns:
            int: 标记的贴图数量
        """
        count = 0
        for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
            texture_info_list = draw_ib_model.get_submesh_texture_markup_info_list(submesh_model)
            for texture_info in texture_info_list:
                if mark_type is not None and getattr(texture_info, "mark_type", "") != mark_type:
                    continue
                count += 1
        return count

    @classmethod
    def is_slot_binding_mark_type(cls, mark_type: str) -> bool:
        return mark_type in {"Slot", "SharedSlot"}

    @classmethod
    def is_shared_slot_mark_type(cls, mark_type: str) -> bool:
        return mark_type == "SharedSlot"

    @classmethod
    def _get_extract_gametype_folder_path(cls, draw_ib_model: DrawIBModel) -> str:
        primary_submesh_metadata = getattr(draw_ib_model, "primary_submesh_metadata", None)
        if primary_submesh_metadata is not None:
            extract_gametype_folder_path = getattr(primary_submesh_metadata, "extract_gametype_folder_path", "")
            if extract_gametype_folder_path:
                return extract_gametype_folder_path

        submesh_model_list = getattr(draw_ib_model, "submesh_model_list", [])
        if submesh_model_list:
            first_submesh_model = submesh_model_list[0]
            unique_str = getattr(first_submesh_model, "workspace_unique_str", "") or getattr(first_submesh_model, "unique_str", "")
            d3d11_game_type = getattr(first_submesh_model, "d3d11_game_type", None)
            if unique_str and d3d11_game_type is not None:
                return os.path.join(
                    WorkSpaceHelper.get_submesh_folder_path(unique_str),
                    "TYPE_" + d3d11_game_type.GameTypeName,
                    "",
                )

        d3d11_game_type = getattr(draw_ib_model, "d3d11_game_type", getattr(draw_ib_model, "d3d11GameType", None))
        if d3d11_game_type is None:
            return ""

        return GlobalConfig.path_extract_gametype_folder(
            draw_ib=draw_ib_model.draw_ib,
            gametype_name=d3d11_game_type.GameTypeName,
        )

    @classmethod
    def _get_part_extract_gametype_folder_path(cls, draw_ib_model: DrawIBModel, part_name: str) -> str:
        part_name_submesh_dict = getattr(draw_ib_model, "part_name_submesh_dict", {})
        submesh_model = part_name_submesh_dict.get(part_name)
        if submesh_model is None:
            return ""

        d3d11_game_type = getattr(submesh_model, "d3d11_game_type", None)
        if d3d11_game_type is None:
            d3d11_game_type = getattr(draw_ib_model, "d3d11_game_type", getattr(draw_ib_model, "d3d11GameType", None))
        unique_str = getattr(submesh_model, "workspace_unique_str", "") or getattr(submesh_model, "unique_str", "")
        if d3d11_game_type is None or unique_str == "":
            return ""

        return os.path.join(
            WorkSpaceHelper.get_submesh_folder_path(unique_str),
            "TYPE_" + d3d11_game_type.GameTypeName,
            "",
        )

    @classmethod
    def _get_slot_texture_source_path(cls, draw_ib_model: DrawIBModel, part_name: str, texture_markup_info) -> str:
        extract_gametype_folder_path = cls._get_part_extract_gametype_folder_path(draw_ib_model, part_name)
        if extract_gametype_folder_path:
            source_path = extract_gametype_folder_path + texture_markup_info.mark_filename
            print("M_IniHelper: 检查 Slot 贴图源路径: " + source_path)
            if os.path.exists(source_path):
                print("M_IniHelper: 命中 Slot 贴图源路径: " + source_path)
                return source_path

        for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
            d3d11_game_type = getattr(submesh_model, "d3d11_game_type", None)
            if d3d11_game_type is None:
                d3d11_game_type = getattr(draw_ib_model, "d3d11_game_type", getattr(draw_ib_model, "d3d11GameType", None))
            unique_str = getattr(submesh_model, "workspace_unique_str", "") or getattr(submesh_model, "unique_str", "")
            if d3d11_game_type is None or unique_str == "":
                continue

            candidate_source_path = os.path.join(
                WorkSpaceHelper.get_submesh_folder_path(unique_str),
                "TYPE_" + d3d11_game_type.GameTypeName,
                texture_markup_info.mark_filename,
            )
            print("M_IniHelper: 检查备用 Slot 贴图源路径: " + candidate_source_path)
            if os.path.exists(candidate_source_path):
                print("M_IniHelper: 命中备用 Slot 贴图源路径: " + candidate_source_path)
                return candidate_source_path

        print(
            "M_IniHelper: 未找到 Slot 贴图源文件，DrawIB: "
            + draw_ib_model.draw_ib
            + "，Part: "
            + str(part_name)
            + "，文件: "
            + texture_markup_info.mark_filename
        )
        return ""

    @classmethod
    def _get_hash_texture_source_path(cls, draw_ib_model: DrawIBModel, part_name: str, texture_markup_info) -> str:
        print(
            "M_IniHelper: 开始解析 Hash 贴图源路径，DrawIB: "
            + draw_ib_model.draw_ib
            + "，Part: "
            + str(part_name)
            + "，文件: "
            + texture_markup_info.mark_filename
        )
        return cls._get_slot_texture_source_path(draw_ib_model, part_name, texture_markup_info)

    @classmethod
    def _get_part_submesh_folder_name(cls, draw_ib_model: DrawIBModel, part_name: str) -> str:
        part_name_submesh_dict = getattr(draw_ib_model, "part_name_submesh_dict", {})
        submesh_model = part_name_submesh_dict.get(part_name)
        if submesh_model is None:
            print("M_IniHelper: part_name 未匹配到 submesh，DrawIB: " + draw_ib_model.draw_ib + "，Part: " + str(part_name))
            return ""

        submesh_folder_name = getattr(submesh_model, "workspace_unique_str", "") or getattr(submesh_model, "unique_str", "")
        print("M_IniHelper: Part " + str(part_name) + " 对应 unique_str: " + submesh_folder_name)
        return submesh_folder_name

    @classmethod
    def _get_hash_deduped_texture_info(cls, draw_ib_model: DrawIBModel, mark_hash: str):
        for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
            submesh_folder_name = getattr(submesh_model, "workspace_unique_str", "") or getattr(submesh_model, "unique_str", "")
            if not submesh_folder_name:
                continue

            hash_deduped_texture_info_dict = WorkSpaceHelper.get_hash_deduped_texture_info_dict(submesh_folder_name=submesh_folder_name)
            deduped_texture_info = hash_deduped_texture_info_dict.get(mark_hash, None)
            if deduped_texture_info is not None:
                print(
                    "M_IniHelper: 在 unique_str "
                    + submesh_folder_name
                    + " 中找到 Hash 去重信息，Hash: "
                    + mark_hash
                )
                return deduped_texture_info

        print("M_IniHelper: 当前 DrawIB 的所有 unique_str 中都未找到 Hash 去重信息，Hash: " + mark_hash)
        return None

    @classmethod
    def get_drawindexed_str_list(
        cls,
        ordered_draw_obj_model_list: list[DrawCallModel],
        obj_name_draw_offset_dict: dict[str, int] | None = None,
        base_vertex: int = 0,
    ) -> list[str]:
        """获取 drawindexed 命令字符串列表
        
        根据 DrawCallModel 列表生成 drawindexed 命令，支持条件判断。
        会按 condition_str 把相邻且条件相同的 obj_model 合并进同一个 if 块，
        并严格保持 ordered_draw_obj_model_list 的顺序：同条件但不相邻的物体
        各自成块，绝不会被挪到该条件首次出现的位置。
        
        Args:
            ordered_draw_obj_model_list: DrawCallModel 列表，按绘制顺序排列
            obj_name_draw_offset_dict: 对象名称到绘制偏移的映射字典
            base_vertex: drawindexed 的 base_vertex，合并网格重定向时使用
            
        Returns:
            list[str]: drawindexed 命令字符串列表
        """
        # 传统的使用DrawIndexed方式调用这个
        # 只有「相邻且条件相同」的 obj_model 才合并进同一个 if 块。
        # 旧实现用 dict 按 condition_str 聚合，会把同条件但不相邻的物体
        # （尤其是「无任何物体切换门控」的物体）整体挪到该条件首次出现的位置，
        # 使 ini 里的 drawindexed 行序与蓝图收集顺序（= 顶点/索引区分配顺序）脱钩。
        drawindexed_str_list:list[str] = []

        def _emit_body(obj_model_list: list[DrawCallModel], indent: str) -> None:
            for obj_model in obj_model_list:
                display_name = str(getattr(obj_model, 'obj_name', '') or getattr(obj_model, 'display_name', '') or '')
                drawindexed_str_list.append(indent + "; [mesh:" + display_name + "] [vertex_count:" + str(obj_model.vertex_count) + "]" )
                drawindexed = (
                    obj_model.get_drawindexed_str(obj_name_draw_offset_dict)
                    if base_vertex == 0
                    else obj_model.get_drawindexed_str(
                        obj_name_draw_offset_dict,
                        base_vertex=base_vertex,
                    )
                )
                drawindexed_str_list.append(indent + drawindexed)

        def _flush_run(condition_str: str, obj_model_list: list[DrawCallModel]) -> None:
            if condition_str != "":
                drawindexed_str_list.append("if " + condition_str)
                _emit_body(obj_model_list, "  ")
                drawindexed_str_list.append("endif")
            else:
                _emit_body(obj_model_list, "")
            drawindexed_str_list.append("")

        run_condition_str = ""
        run_obj_model_list: list[DrawCallModel] = []
        for obj_model in ordered_draw_obj_model_list:
            condition_str = obj_model.get_condition_str()
            if run_obj_model_list and condition_str != run_condition_str:
                _flush_run(run_condition_str, run_obj_model_list)
                run_obj_model_list = []
            run_condition_str = condition_str
            run_obj_model_list.append(obj_model)
        if run_obj_model_list:
            _flush_run(run_condition_str, run_obj_model_list)

        return drawindexed_str_list
    
    @classmethod
    def get_drawindexed_instanced_str_list(
        cls,
        ordered_draw_obj_model_list: list[DrawCallModel],
        obj_name_draw_offset_dict: dict[str, int] | None = None,
    ) -> list[str]:
        # 使用DrawIndexedInstanced方式调用这个
        # 分组规则同 get_drawindexed_str_list：只合并相邻同条件，严格保持传入顺序。
        drawindexed_str_list:list[str] = []

        def _emit_body(obj_model_list: list[DrawCallModel], comment_indent: str, command_indent: str) -> None:
            for obj_model in obj_model_list:
                display_name = str(getattr(obj_model, 'obj_name', '') or getattr(obj_model, 'display_name', '') or '')
                drawindexed_str_list.append(comment_indent + "; [mesh:" + display_name + "] [vertex_count:" + str(obj_model.vertex_count) + "]" )
                drawindexed_str_list.append(command_indent + obj_model.get_drawindexed_instanced_str(obj_name_draw_offset_dict))

        def _flush_run(condition_str: str, obj_model_list: list[DrawCallModel]) -> None:
            if condition_str != "":
                drawindexed_str_list.append("if " + condition_str)
                _emit_body(obj_model_list, "  ", "  ")
                drawindexed_str_list.append("endif")
            else:
                _emit_body(obj_model_list, "", "  ")
            drawindexed_str_list.append("")

        run_condition_str = ""
        run_obj_model_list: list[DrawCallModel] = []
        for obj_model in ordered_draw_obj_model_list:
            condition_str = obj_model.get_condition_str()
            if run_obj_model_list and condition_str != run_condition_str:
                _flush_run(run_condition_str, run_obj_model_list)
                run_obj_model_list = []
            run_condition_str = condition_str
            run_obj_model_list.append(obj_model)
        if run_obj_model_list:
            _flush_run(run_condition_str, run_obj_model_list)

        return drawindexed_str_list

    @classmethod
    def generate_hash_style_texture_ini(cls,ini_builder:M_IniBuilder,drawib_drawibmodel_dict:dict[str,DrawIBModel]):
        '''
        Hash风格贴图
        '''

        if GlobalProterties.forbid_auto_texture_ini():
            return

        # 先统计当前标记的具有Slot风格的Hash值，后续Render里搞图片的时候跳过这些
        slot_style_texture_hash_list = []
        for draw_ib_model in drawib_drawibmodel_dict.values():
            for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
                for texture_markup_info in draw_ib_model.get_submesh_texture_markup_info_list(submesh_model):
                    if texture_markup_info.mark_type == "Slot":
                        slot_style_texture_hash_list.append(texture_markup_info.mark_hash)
        
        print("slot_style_texture_hash_list:" + str(slot_style_texture_hash_list))
        print("M_IniHelper: 开始生成 Hash 风格贴图配置，DrawIB 数量: " + str(len(drawib_drawibmodel_dict)))
                    
        repeat_hash_list = []
        # 遍历当前drawib的Render文件夹
        for draw_ib,draw_ib_model in drawib_drawibmodel_dict.items():
            marked_hash_count = cls._count_marked_textures(draw_ib_model, mark_type="Hash")
            print("M_IniHelper: DrawIB " + draw_ib + " 的 Hash 标记数量: " + str(marked_hash_count))

            # 添加标记的Hash风格贴图
            for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
                texture_markup_info_list = draw_ib_model.get_submesh_texture_markup_info_list(submesh_model)
                if not texture_markup_info_list:
                    continue

                part_name = draw_ib_model.get_submesh_part_name(submesh_model)
                submesh_folder_name = getattr(submesh_model, "workspace_unique_str", "") or getattr(submesh_model, "unique_str", "")
                if not submesh_folder_name:
                    print("M_IniHelper: 跳过 Hash 贴图处理，未找到 unique_str，Part: " + str(part_name))
                    continue

                hash_deduped_texture_info_dict = WorkSpaceHelper.get_hash_deduped_texture_info_dict(submesh_folder_name=submesh_folder_name)
                print(
                    "M_IniHelper: 已读取 Hash 去重信息，unique_str: "
                    + submesh_folder_name
                    + "，记录数: "
                    + str(len(hash_deduped_texture_info_dict))
                )

                for texture_markup_info in texture_markup_info_list:
                    if texture_markup_info.mark_type != "Hash":
                        print("Skipping non-Hash style texture: " + texture_markup_info.mark_filename)
                        continue

                    texture_output_folder = GlobalConfig.path_generatemod_texture_folder(draw_ib=draw_ib)
                    print("M_IniHelper: Hash 贴图输出目录: " + texture_output_folder)

                    if texture_markup_info.mark_hash in repeat_hash_list:
                        print("Skipping repeated Hash style texture: " + texture_markup_info.mark_filename)
                        continue
                    else:
                        repeat_hash_list.append(texture_markup_info.mark_hash)

                    d3d11_game_type = getattr(draw_ib_model, "d3d11_game_type", getattr(draw_ib_model, "d3d11GameType", None))
                    if d3d11_game_type is None:
                        continue

                    original_texture_file_path = cls._get_hash_texture_source_path(
                        draw_ib_model=draw_ib_model,
                        part_name=part_name,
                        texture_markup_info=texture_markup_info,
                    )
                    print("M_IniHelper: Hash 贴图源路径解析结果: " + original_texture_file_path)
                    if not os.path.exists(original_texture_file_path):
                        print("Skipping missing texture file: " + original_texture_file_path)
                        continue

                    hash_style_texture_filename = ""
                    hash_style_texture_filename = hash_style_texture_filename + draw_ib + "_" + draw_ib_model.draw_ib_alias + "_"

                    deduped_texture_info = hash_deduped_texture_info_dict.get(texture_markup_info.mark_hash,None)
                    if deduped_texture_info is None:
                        deduped_texture_info = cls._get_hash_deduped_texture_info(
                            draw_ib_model=draw_ib_model,
                            mark_hash=texture_markup_info.mark_hash,
                        )

                    if deduped_texture_info is None:
                        print(
                            "M_IniHelper: 未找到 Hash 去重信息，降级使用原始标记文件名继续导出。DrawIB: "
                            + draw_ib
                            + "，文件名: "
                            + texture_markup_info.mark_filename
                            + "，Hash: "
                            + texture_markup_info.mark_hash
                        )
                        hash_style_texture_filename = texture_markup_info.mark_filename
                    else:
                        component_count_list_str = deduped_texture_info.componet_count_list_str
                        hash_style_texture_filename = hash_style_texture_filename + "_" + component_count_list_str + "_"
                        hash_style_texture_filename = hash_style_texture_filename + deduped_texture_info.original_hash + "_" + deduped_texture_info.render_hash + "_" + deduped_texture_info.format + "_" + texture_markup_info.mark_name
                        hash_style_texture_filename = hash_style_texture_filename + "." + texture_markup_info.mark_filename.split(".")[1]
                    print(texture_markup_info.mark_filename)
                    print(texture_markup_info.get_hash_style_filename())




                    target_texture_file_path = GlobalConfig.path_generatemod_texture_folder(draw_ib=draw_ib) + hash_style_texture_filename
                    print("M_IniHelper: Hash 贴图目标路径: " + target_texture_file_path)
                    
                    # 该贴图段（[Resource_Texture_<贴图hash>] + [TextureOverride_<贴图hash>]）属于当前 draw_ib：
                    # 构造期间进入 draw_ib 上下文，供「条件判断」后处理节点按 draw_ib 反查。
                    _draw_ib_ctx = _section_registry.draw_ib_context(draw_ib) if _section_registry is not None else contextlib.nullcontext()
                    with _draw_ib_ctx:
                        resource_and_textureoverride_texture_section = M_IniSection(M_SectionType.ResourceAndTextureOverride_Texture)
                        resource_and_textureoverride_texture_section.append("[Resource_Texture_" + texture_markup_info.mark_hash + "]")
                        resource_and_textureoverride_texture_section.append("filename = Textures/" + hash_style_texture_filename)
                        resource_and_textureoverride_texture_section.new_line()

                        resource_and_textureoverride_texture_section.append("[TextureOverride_" + texture_markup_info.mark_hash + "]")
                        resource_and_textureoverride_texture_section.append("; " + texture_markup_info.mark_filename)
                        resource_and_textureoverride_texture_section.append("hash = " + texture_markup_info.mark_hash)
                        resource_and_textureoverride_texture_section.append("match_priority = 0")
                        resource_and_textureoverride_texture_section.append("this = Resource_Texture_" + texture_markup_info.mark_hash)
                        resource_and_textureoverride_texture_section.new_line()

                        ini_builder.append_section(resource_and_textureoverride_texture_section)

                    # copy only if target not exists avoid overwrite texture manually replaced by mod author.
                    if not os.path.exists(target_texture_file_path):
                        print("M_IniHelper: 开始复制 Hash 贴图文件: " + original_texture_file_path + " -> " + target_texture_file_path)
                        shutil.copy2(original_texture_file_path,target_texture_file_path)
                        print("M_IniHelper: 已复制 Hash 贴图文件: " + target_texture_file_path)
                    else:
                        print("M_IniHelper: Hash 贴图目标已存在，跳过复制: " + target_texture_file_path)

            # NTEMI 复用 WWMI 导出链路，这里同样需要保留全局 Hash 贴图风格。
            if GlobalConfig.logic_name not in (LogicName.WWMI, LogicName.NTEMI):
                continue


            

        # if len(repeat_hash_list) != 0:
        #     texture_ini_builder.save_to_file(MainConfig.path_generate_mod_folder() + MainConfig.workspacename + "_Texture.ini")

    @classmethod
    def generate_shared_slot_style_texture_ini(cls, ini_builder:M_IniBuilder, drawib_drawibmodel_dict:dict[str,DrawIBModel]):
        """
        SharedSlot:
        - 文件去重和命名按 Hash 风格
        - INI 绑定按 Slot 风格，仅生成 Resource 段
        """
        if GlobalProterties.forbid_auto_texture_ini():
            return

        appended_resource_names:set[str] = set()
        hash_filename_cache:dict[str, str] = {}

        for draw_ib, draw_ib_model in drawib_drawibmodel_dict.items():
            # 该 draw_ib 的共享槽位资源段（[Resource_...]）属于当前 draw_ib：构造期间进入 draw_ib 上下文。
            # 归属在 M_IniSection 构造时就已快照，段内容随后统一 append、写盘时整批登记。
            _draw_ib_ctx = _section_registry.draw_ib_context(draw_ib) if _section_registry is not None else contextlib.nullcontext()
            with _draw_ib_ctx:
                shared_slot_resource_section = M_IniSection(M_SectionType.ResourceTexture)
            has_shared_slot = False

            for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
                texture_markup_info_list = draw_ib_model.get_submesh_texture_markup_info_list(submesh_model)
                if not texture_markup_info_list:
                    continue

                part_name = draw_ib_model.get_submesh_part_name(submesh_model)
                submesh_folder_name = getattr(submesh_model, "workspace_unique_str", "") or getattr(submesh_model, "unique_str", "")
                if not submesh_folder_name:
                    continue

                hash_deduped_texture_info_dict = WorkSpaceHelper.get_hash_deduped_texture_info_dict(
                    submesh_folder_name=submesh_folder_name
                )

                for texture_markup_info in texture_markup_info_list:
                    if getattr(texture_markup_info, "mark_type", "") != "SharedSlot":
                        continue

                    hash_style_texture_filename = hash_filename_cache.get(texture_markup_info.mark_hash)
                    if hash_style_texture_filename is None:
                        original_texture_file_path = cls._get_slot_texture_source_path(
                            draw_ib_model=draw_ib_model,
                            part_name=part_name,
                            texture_markup_info=texture_markup_info,
                        )
                        if not original_texture_file_path or not os.path.exists(original_texture_file_path):
                            continue

                        hash_style_texture_filename = draw_ib + "_" + draw_ib_model.draw_ib_alias + "_"

                        deduped_texture_info = hash_deduped_texture_info_dict.get(texture_markup_info.mark_hash, None)
                        if deduped_texture_info is None:
                            deduped_texture_info = cls._get_hash_deduped_texture_info(
                                draw_ib_model=draw_ib_model,
                                mark_hash=texture_markup_info.mark_hash,
                            )

                        if deduped_texture_info is None:
                            hash_style_texture_filename = texture_markup_info.mark_filename
                        else:
                            component_count_list_str = deduped_texture_info.componet_count_list_str
                            hash_style_texture_filename = hash_style_texture_filename + "_" + component_count_list_str + "_"
                            hash_style_texture_filename = (
                                hash_style_texture_filename
                                + deduped_texture_info.original_hash
                                + "_"
                                + deduped_texture_info.render_hash
                                + "_"
                                + deduped_texture_info.format
                                + "_"
                                + texture_markup_info.mark_name
                            )
                            hash_style_texture_filename = hash_style_texture_filename + "." + texture_markup_info.mark_filename.split(".")[1]

                        target_texture_file_path = GlobalConfig.path_generatemod_texture_folder(draw_ib=draw_ib) + hash_style_texture_filename
                        if not os.path.exists(target_texture_file_path):
                            shutil.copy2(original_texture_file_path, target_texture_file_path)

                        hash_filename_cache[texture_markup_info.mark_hash] = hash_style_texture_filename

                    has_shared_slot = True
                    resource_name = texture_markup_info.get_resource_name()
                    if resource_name in appended_resource_names:
                        continue

                    appended_resource_names.add(resource_name)
                    shared_slot_resource_section.append("[" + resource_name + "]")
                    shared_slot_resource_section.append("filename = Textures/" + hash_style_texture_filename)
                    shared_slot_resource_section.new_line()

            if has_shared_slot:
                ini_builder.append_section(shared_slot_resource_section)

    @classmethod
    def move_slot_style_textures(cls,draw_ib_model:DrawIBModel):
        '''
        Move all textures from extracted game type folder to generate mod Texture folder.
        Only works in default slot style texture.
        '''
        if GlobalProterties.forbid_auto_texture_ini():
            return

        marked_slot_count = cls._count_marked_textures(draw_ib_model, mark_type="Slot")
        print("M_IniHelper: 开始复制 Slot 贴图，DrawIB: " + draw_ib_model.draw_ib + "，标记数量: " + str(marked_slot_count))

        for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
            texture_markup_info_list = draw_ib_model.get_submesh_texture_markup_info_list(submesh_model)
            if not texture_markup_info_list:
                continue

            part_name = draw_ib_model.get_submesh_part_name(submesh_model) or submesh_model.unique_str
            for texture_markup_info in texture_markup_info_list:
                # 只有槽位风格会移动到目标位置
                if texture_markup_info.mark_type != "Slot":
                    continue

                texture_output_folder = GlobalConfig.path_generatemod_texture_folder(draw_ib=draw_ib_model.draw_ib)
                print("M_IniHelper: Slot 贴图输出目录: " + texture_output_folder)

                target_path = GlobalConfig.path_generatemod_texture_folder(draw_ib=draw_ib_model.draw_ib) + texture_markup_info.mark_filename
                source_path = cls._get_slot_texture_source_path(draw_ib_model, part_name, texture_markup_info)
                print(
                    "M_IniHelper: Slot 贴图复制计划，Part: "
                    + str(part_name)
                    + "，源: "
                    + source_path
                    + "，目标: "
                    + target_path
                )
                
                # only overwrite when there is no texture file exists.
                if not os.path.exists(target_path):
                    if source_path == "":
                        print("Skip missing texture file: " + texture_markup_info.mark_filename)
                        continue
                    print("Move Texture File: " + texture_markup_info.mark_filename)
                    shutil.copy2(source_path,target_path)
                    print("M_IniHelper: 已复制 Slot 贴图文件: " + target_path)
                else:
                    print("M_IniHelper: Slot 贴图目标已存在，跳过复制: " + target_path)
    
    @classmethod
    def _report_standard_route_baked_shapekeys(cls, ini_builder:M_IniBuilder) -> list[str]:
        """R-A 诊断：标准（非直出）路线把形态键烘焙掉时明确报警，不再静默 return。

        背景（review-reports/t82-shapekey-drag-retest.md §3.3 R-A）：标准单轮导出的前处理
        `blueprint/preprocess.py::_apply_shape_keys` 会先把副本上的非 Basis 键块烘焙进网格
        （`bake_current_shape_key_mix_to_mesh` + `remove_non_basis_shape_keys`），于是经典
        发射器拿到的 `shapekeyname_mkey_dict` 为空、载荷也为空 —— 旧实现在 :572-573 直接
        `return`：产物里既没有形态键段，也没有任何告警。本方法把这条路径变成**可见**：
        同时落 stdout/日志 sink（`LOG.warning`）与产物注释（ini `[Present]` 里的 `;` 行）。
        """
        try:
            baked_object_names = BlueprintExportHelper.detect_standard_route_baked_shapekey_objects()
        except Exception as exc:  # 诊断本身绝不能中断导出
            LOG.warning(f"形态键丢弃检测失败（不影响导出结果）: {exc}")
            return []

        if not baked_object_names:
            return []

        shown_names = "、".join(baked_object_names[:5])
        if len(baked_object_names) > 5:
            shown_names += f" 等 {len(baked_object_names)} 个"

        LOG.warning(
            "[ShapeKeyExport] 标准（非直出）路线不导出形态键："
            f"{len(baked_object_names)} 个物体的形态键已在标准前处理中被烘焙进网格"
            f"（{shown_names}），本轮产物不会包含形态键段与形态键缓冲。"
            "合并骨架下的形态键导出需要『直出模式』：请在形态键配置节点"
            "（SSMTNode_PostProcess_ShapeKey）勾选 direct_export_mode（新节点默认已勾选）。"
            "若确实不需要导出形态键，本告警可忽略；要消除它请删除物体上非 Basis 的形态键。"
        )

        notice_section = M_IniSection(M_SectionType.Present)
        notice_section.SectionName = "Present"
        notice_section.append("; --- SSMT 形态键导出诊断（标准/非直出路线）---")
        notice_section.append(
            "; 标准（非直出）路线不导出形态键："
            + str(len(baked_object_names))
            + " 个物体的形态键已在标准前处理中被烘焙进网格，本产物不含形态键段与形态键缓冲。"
        )
        notice_section.append(
            "; 需要形态键导出请启用直出模式"
            "（SSMTNode_PostProcess_ShapeKey.direct_export_mode，新节点默认已勾选）。"
        )
        notice_section.append("; --- SSMT 形态键导出诊断 END ---")
        ini_builder.append_section(notice_section)
        return baked_object_names

    @classmethod
    def add_shapekey_ini_sections(cls, ini_builder:M_IniBuilder,drawib_drawibmodel_dict:dict[str,DrawIBModel]):
        """发射经典（非直出）形态键 INI 段。

        .. legacy::
            本发射器是**老路线**（`[Constants] $shapekey*` + `CustomShaderComputeShapes`）的
            唯一实现，在 ZZMI/ZZMIDX12 的实际路径上**从不产出**（见 t82：4 份产物
            `CustomShaderComputeShapes` 恒为 0 —— 直出路线在此前主动
            `set_suppress_shapekey_resource_export(True)` 压制它，标准路线的前处理则先把键块
            烘焙掉）。它仍是 unity×2 / srmi / gimi / identityv / yysls / snowbreak / zzmidx12
            的活性实现（全仓 9 处调用点，8 处在 ZZMI 之外），且是本轮 R-A 诊断的落点，故
            **保留**；形态键导出请走
            `blueprint/direct_export.py`（`direct_export_mode`，形态键节点新节点默认勾选）。
        """
        if BlueprintExportHelper.should_suppress_shapekey_resource_export():
            return

        # 放在空判定之前：无论「段没发出去」还是「段发出去了但载荷被烘焙没了」，都要能看见。
        cls._report_standard_route_baked_shapekeys(ini_builder)

        shapekeyname_mkey_dict = BlueprintExportHelper.get_current_shapekeyname_mkey_dict()
        if len(shapekeyname_mkey_dict.keys()) == 0:
            return

        # 生成形态键 INI 时需要确保 Shapes.hlsl 等资源已复制到 res/ 目录
        from .m_ini_helper_gui import M_IniHelperGUI
        M_IniHelperGUI.copy_res_to_mod_folder()

        # [Constants]
        constants_section = M_IniSection(M_SectionType.Constants)
        constants_section.SectionName = "Constants"
        constants_section.append("global persist $shapekey_first_run = 1")

        for shapekey_name, m_key in shapekeyname_mkey_dict.items():
            constants_section.append("; ShapeKey: " + shapekey_name)
            constants_section.append("global persist " + m_key.key_name + " = " + str(m_key.initialize_value))
            constants_section.new_line()

        ini_builder.append_section(constants_section)

        # [Present]
        present_section = M_IniSection(M_SectionType.Present)
        present_section.SectionName = "Present"
        present_section.append("if $shapekey_first_run")

        ib_number = 1
        for drawib, drawib_model in drawib_drawibmodel_dict.items():
            shapekey_buffer_dict = getattr(drawib_model, "shapekey_name_bytelist_dict", {})

            # 如果当前DrawIB没有生成形态键数据，则跳过不处理
            if not shapekey_buffer_dict:
                continue

            original_position_buffer_resource_name ="Resource" + drawib + "Position"     
            duplicated_position_buffer_resource_name = "Resource" + drawib + "Position.1"

            present_section.append("  " + original_position_buffer_resource_name + " = copy " + duplicated_position_buffer_resource_name)
            present_section.append("  run = CustomShaderComputeShapes" + str(ib_number))

            ib_number += 1
        
        present_section.append("  $shapekey_first_run = 0")
        present_section.append("endif")

        ib_number = 1
        for drawib, drawib_model in drawib_drawibmodel_dict.items():
            shapekey_buffer_dict = getattr(drawib_model, "shapekey_name_bytelist_dict", {})

            # 如果当前DrawIB没有生成形态键数据，则跳过不处理
            if not shapekey_buffer_dict:
                continue

            present_section.append("  run = CustomShaderComputeShapes" + str(ib_number))
            ib_number += 1

        ini_builder.append_section(present_section)
        
        # [CustomShaderComputeShapes]
        customshader_section = M_IniSection(M_SectionType.CommandList)

        ib_number = 1
        for drawib, drawib_model in drawib_drawibmodel_dict.items():
            shapekey_buffer_dict = getattr(drawib_model, "shapekey_name_bytelist_dict", {})
            d3d11_game_type = getattr(drawib_model, "d3d11_game_type", getattr(drawib_model, "d3d11GameType", None))
            draw_number = getattr(drawib_model, "draw_number", getattr(drawib_model, "vertex_count", 0))

            # 如果当前DrawIB没有生成形态键数据，则跳过不处理
            if not shapekey_buffer_dict or d3d11_game_type is None:
                continue

            customshader_section.append("[CustomShaderComputeShapes" + str(ib_number) + "]")
            customshader_section.append("cs = ./res/Shapes.hlsl")
            customshader_section.append("cs-u5 = copy " + "Resource" + drawib + "Position.1")
            customshader_section.new_line()

            # 对于每个形态键buffer都进行计算
            for shapekey_name, m_key in shapekeyname_mkey_dict.items():
                # 这里很显然有问题，如果一个DrawIB有这个形态键，另一个DrawIB没有这个形态键呢？
                # 那这里就会导致游戏内没有这个形态键的模型出现异常
                # 所以如果这个DrawIB内没有这个形态键的话，就不需要生成它的计算代码
                if shapekey_buffer_dict.get(shapekey_name, None) is None:
                    continue

                customshader_section.append("x88 = " + m_key.key_name)
                customshader_section.append("cs-t50 = copy " + "Resource" + drawib + "Position.1")
                customshader_section.append("cs-t51 = copy " + "Resource" + drawib + "Position." + shapekey_name)
                customshader_section.append("Resource" + drawib + "Position = ref cs-u5")
                customshader_section.append("Dispatch = " + str(draw_number) + " ,1 ,1")
                customshader_section.new_line()

            ib_number += 1

            customshader_section.append("cs-u5 = null")
            customshader_section.append("cs-t50 = null")
            customshader_section.append("cs-t51 = null")

        ini_builder.append_section(customshader_section)

        # [Resources]
        resource_section = M_IniSection(M_SectionType.ResourceBuffer)


        ib_number = 1
        for drawib, drawib_model in drawib_drawibmodel_dict.items():
            shapekey_buffer_dict = getattr(drawib_model, "shapekey_name_bytelist_dict", {})
            d3d11_game_type = getattr(drawib_model, "d3d11_game_type", getattr(drawib_model, "d3d11GameType", None))

            # 如果当前DrawIB没有生成形态键数据，则跳过不处理
            if not shapekey_buffer_dict or d3d11_game_type is None:
                continue

            buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()

            # 原本的Buffer
            resource_section.append("[Resource" + drawib + "Position.1]")
            resource_section.append("type = buffer")
            resource_section.append("stride = " + str(d3d11_game_type.CategoryStrideDict["Position"]))
            resource_section.append("filename = " + buffer_folder_name + "\\" + drawib + "-" + "Position.buf")
            resource_section.new_line()

            # 各个形态键的Buffer
            for shapekey_name, m_key in shapekeyname_mkey_dict.items():
                # 这里很显然有问题，如果一个DrawIB有这个形态键，另一个DrawIB没有这个形态键呢？
                # 那这里就会导致游戏内没有这个形态键的模型出现异常
                # 所以如果这个DrawIB内没有这个形态键的话，就不需要生成它的计算代码
                if shapekey_buffer_dict.get(shapekey_name, None) is None:
                    continue
                
                resource_section.append("[Resource" + drawib + "Position." + shapekey_name + "]")
                resource_section.append("type = buffer")
                resource_section.append("stride = " + str(d3d11_game_type.CategoryStrideDict["Position"]))
                resource_section.append("filename = " + buffer_folder_name + "\\" + drawib + "-" + "Position." + shapekey_name + ".buf")
                resource_section.new_line()

            ib_number += 1
        
        ini_builder.append_section(resource_section)

        # [Key]
        # 用于按下测试的Key，也可以作为在没有面板时的按键切换形态键快捷键
        key_section = M_IniSection(M_SectionType.Key)
        for shapekey_name, m_key in shapekeyname_mkey_dict.items():
            if m_key.initialize_vk_str != "":
                key_section.append("[Key_ShapeKey_" +shapekey_name + "]")
                
                # 添加备注信息
                comment = getattr(m_key, 'comment', '')
                if comment:
                    key_section.append("; " + comment)
                
                key_section.append("key = " + m_key.initialize_vk_str)
                key_section.append("type = cycle")
                key_section.append(m_key.key_name + " = 0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1")
                key_section.new_line()

        ini_builder.append_section(key_section)



    @classmethod
    def add_branch_key_sections(cls, ini_builder:M_IniBuilder, key_name_mkey_dict:dict[str,M_Key]):
        """添加分支按键配置段落
        
        在 INI 中添加物体切换相关的配置，包括：
        - [Constants] 中声明 $active0 变量
        - [Present] 中初始化 $active0 = 0
        - [KeySwap_N] 段落用于按键切换
        
        注意：$swapkey 相关的配置由 node_swap_ini.py 模块单独处理，
        此方法只处理非 swapkey 的按键配置。
        
        Args:
            ini_builder: INI 构建器
            key_name_mkey_dict: 按键名称到 M_Key 的映射字典
        """
        if len(key_name_mkey_dict.keys()) != 0:
            constants_section = None
            for section in ini_builder.ini_section_list:
                if section.SectionType == M_SectionType.Constants:
                    constants_section = section
                    break
            
            if constants_section is None:
                constants_section = M_IniSection(M_SectionType.Constants)
                constants_section.SectionName = "Constants"
                ini_builder.append_section(constants_section)

            mod_count = GlobalKeyCountHelper.generated_mod_number
            for i in range(mod_count):
                active_line = f"global $active{i}"
                already_exists = any(active_line in line for line in constants_section.SectionLineList)
                if not already_exists:
                    constants_section.append(active_line)

            for mkey in key_name_mkey_dict.values():
                # 跳过 swapkey，它们由 node_swap_ini.py 单独处理
                if getattr(mkey, 'is_swapkey', False):
                    continue
                
                key_str = "global persist " + mkey.key_name + " = " + str(mkey.initialize_value)
                already_exists = any(key_str in line for line in constants_section.SectionLineList)
                if not already_exists:
                    constants_section.append(key_str)


        if len(key_name_mkey_dict.keys()) != 0:
            present_section = M_IniSection(M_SectionType.Present)
            present_section.SectionName = "Present"
            
            mod_count = GlobalKeyCountHelper.generated_mod_number
            for i in range(mod_count):
                present_section.append(f"post $active{i} = 0")
            ini_builder.append_section(present_section)
        
        key_number = 0
        if len(key_name_mkey_dict.keys()) != 0:

            for mkey in key_name_mkey_dict.values():
                # 跳过 swapkey，它们由 node_swap_ini.py 单独处理
                if getattr(mkey, 'is_swapkey', False):
                    continue
                
                key_section = M_IniSection(M_SectionType.Key)
                key_section.append("[KeySwap_" + str(key_number) + "]")
                
                comment = getattr(mkey, 'comment', '')
                if comment:
                    key_section.append("; " + comment)
                
                key_section.append("condition = $active0 == 1")

                if mkey.initialize_vk_str != "":
                    key_section.append("key = " + mkey.initialize_vk_str)
                else:
                    key_section.append("key = " + mkey.key_value)
                key_section.append("type = cycle")

                key_value_number = len(mkey.value_list)
                key_cycle_str = ""
                for i in range(key_value_number):
                    if i < key_value_number + 1:
                        key_cycle_str = key_cycle_str + str(i) + ","
                    else:
                        key_cycle_str = key_cycle_str + str(i)
                key_section.append(mkey.key_name + " = " + key_cycle_str)
                key_section.new_line()
                ini_builder.append_section(key_section)

                key_number = key_number + 1

    # ------------------------------------------------------------------
    # 着色器替换相关
    # ------------------------------------------------------------------

    @classmethod
    def _build_custom_shader_section_name(cls, prefix, ib_hash, first_index, component, index_count, index_offset, base_vertex, variant):
        """构建 CustomShader 段名。

        格式: CustomShader_{prefix}_{ib_hash}_{first_index}_{component}_{index_count}_{index_offset}_{base_vertex}_{variant}
        """
        return f"CustomShader_{prefix}_{ib_hash}_{first_index}_{component}_{index_count}_{index_offset}_{base_vertex}_{variant}"

    @staticmethod
    def _validate_shader_replace_info_list(shader_replace_info_list):
        """Validate names that become shared INI variables and section identifiers."""
        identifier_pattern = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
        shader_hash_pattern = re.compile(r'^[0-9A-Fa-f]+$')
        seen_prefixes = {}
        for info_index, info in enumerate(shader_replace_info_list or [], start=1):
            prefix = str(info.get('name_prefix', '') or '').strip()
            if not prefix:
                raise ValueError(f"着色器替换配置 {info_index} 的名称前缀不能为空")
            if not identifier_pattern.fullmatch(prefix):
                raise ValueError(
                    f"着色器替换配置 {info_index} 的名称前缀 '{prefix}' 非法；"
                    "仅允许英文字母、数字和下划线，且不能以数字开头。"
                )

            prefix_key = prefix.casefold()
            previous_index = seen_prefixes.get(prefix_key)
            if previous_index is not None:
                raise ValueError(
                    f"着色器替换名称前缀 '{prefix}' 重复：配置 {previous_index} 与 {info_index}。"
                    "配置完全相同的节点会被自动合并；参数不同的节点必须使用唯一前缀。"
                )
            seen_prefixes[prefix_key] = info_index

            seen_shader_hashes = {}
            for variant_index, shader in enumerate(info.get('shaders', []) or [], start=1):
                variant = str(shader.get('variant_name', '') or '').strip()
                if not variant:
                    raise ValueError(
                        f"着色器替换配置 {info_index} 的变体 {variant_index} 名称不能为空"
                    )
                if not identifier_pattern.fullmatch(variant):
                    raise ValueError(
                        f"着色器替换配置 {info_index} 的变体名称 '{variant}' 非法；"
                        "仅允许英文字母、数字和下划线，且不能以数字开头。"
                    )
                if variant.casefold() == "normal":
                    raise ValueError(
                        f"着色器替换配置 {info_index} 的变体名称 '{variant}' 为保留名称；"
                        "Normal 用于系统生成的原始着色器回退段。"
                    )
                shader_hash = str(shader.get('shader_hash', '') or '').strip()
                if shader_hash and not shader_hash_pattern.fullmatch(shader_hash):
                    raise ValueError(
                        f"着色器替换配置 {info_index} 的变体 '{variant}' 哈希 '{shader_hash}' 非法；"
                        "着色器哈希只能包含十六进制字符。"
                    )
                if shader_hash:
                    shader_hash_key = shader_hash.casefold()
                    previous_hash = seen_shader_hashes.get(shader_hash_key)
                    if previous_hash is not None and previous_hash[1] != variant.casefold():
                        raise ValueError(
                            f"着色器替换配置 {info_index} 的哈希 '{shader_hash}' 同时用于不同变体："
                            f"变体 {previous_hash[0]} 与 {variant_index}。"
                        )
                    seen_shader_hashes[shader_hash_key] = (variant_index, variant.casefold())

            toggle_key = str(info.get('toggle_key', '') or '').strip()
            if '\r' in toggle_key or '\n' in toggle_key:
                raise ValueError(
                    f"着色器替换配置 {info_index} 的快捷键包含非法换行符"
                )

    @staticmethod
    def _normalize_shader_replace_shaders(info):
        """Annotate variants with automatic groups while accepting older node data."""
        variant_occurrences = {}
        variant_values = {}
        normalized = []
        for shader in info.get('shaders', []) or []:
            entry = dict(shader)
            variant = str(entry.get('variant_name', '') or '').strip()
            variant_key = variant.casefold()
            inferred_group = variant_occurrences.get(variant_key, 0) + 1
            variant_occurrences[variant_key] = inferred_group
            if variant_key not in variant_values:
                try:
                    variant_values[variant_key] = int(entry.get('env_value', len(variant_values) + 1))
                except (TypeError, ValueError):
                    variant_values[variant_key] = len(variant_values) + 1

            try:
                group_index = max(1, int(entry.get('group_index', inferred_group)))
            except (TypeError, ValueError):
                group_index = inferred_group
            entry.update({
                'variant_name': variant,
                'group_index': group_index,
                'env_value': variant_values[variant_key],
                'variant_id': variant if group_index == 1 else f"{variant}_Group{group_index}",
            })
            normalized.append(entry)
        return normalized

    @staticmethod
    def get_draw_call_shader_replace_info_list(
        draw_call,
        shader_replace_object_names=None,
        shader_replace_object_info_map=None,
        shader_replace_info_list=None,
    ):
        """Resolve Shader Replace data without overriding an explicitly normal chain."""
        direct_infos = list(getattr(draw_call, "shader_replace_info_list", []) or [])
        if getattr(draw_call, "shader_replace_info_resolved", False):
            return direct_infos
        if direct_infos:
            return direct_infos

        obj_name = str(getattr(draw_call, "obj_name", "") or "")
        if obj_name not in (shader_replace_object_names or set()):
            return []

        mapped_infos = list((shader_replace_object_info_map or {}).get(obj_name, []) or [])
        if mapped_infos:
            return mapped_infos
        return list(shader_replace_info_list or [])

    @staticmethod
    def build_draw_call_offset_map(drawib_models):
        """Build final combined-IB offsets keyed by DrawCall object identity."""
        offset_map = {}
        for drawib_model in drawib_models or []:
            object_offsets = getattr(drawib_model, "obj_name_draw_offset", {}) or {}
            for submesh_model in getattr(drawib_model, "submesh_model_list", []) or []:
                for draw_call in getattr(submesh_model, "drawcall_model_list", []) or []:
                    offset_map[id(draw_call)] = object_offsets.get(
                        draw_call.obj_name,
                        draw_call.index_offset,
                    )
        return offset_map

    @classmethod
    def add_shader_replace_sections(
        cls,
        ini_builder,
        shader_replace_info_list,
        shader_replace_object_names,
        draw_call_models,
        mod_export_path,
        use_instanced_draw: bool = False,
        shader_replace_object_info_map: dict = None,
        draw_call_offset_map: dict = None,
        draw_call_base_vertex_map: dict = None,
    ):
        """生成着色器替换相关的 INI 段。

        生成内容:
        - CustomShader 段（按替换组区分的着色器变体及 Normal 回退）
        - KeyToggle 段（原始状态与替换组循环）
        - ShaderOverride 段（基于哈希的自动判定）

        Args:
            ini_builder: M_IniBuilder 实例
            shader_replace_info_list: 着色器替换配置列表
            shader_replace_object_names: 关联物体名集合
            draw_call_models: DrawCallModel 列表
            mod_export_path: 导出路径（用于复制着色器文件）
            use_instanced_draw: 是否生成 drawindexedinstanced 绘制命令
            draw_call_offset_map: DrawCall 对象身份到最终 IB 绘制偏移的映射
            draw_call_base_vertex_map: DrawCall 对象身份到重定向 base_vertex 的映射
        """
        if not shader_replace_info_list:
            return

        cls._validate_shader_replace_info_list(shader_replace_info_list)
        normalized_shaders = {
            id(info): cls._normalize_shader_replace_shaders(info)
            for info in shader_replace_info_list
        }

        def get_normalized_shaders(info):
            info_id = id(info)
            if info_id not in normalized_shaders:
                normalized_shaders[info_id] = cls._normalize_shader_replace_shaders(info)
            return normalized_shaders[info_id]

        # 复制着色器文件到导出目录
        shaders_dir = os.path.join(mod_export_path, "Shaders")
        os.makedirs(shaders_dir, exist_ok=True)
        shader_export_filenames = {}
        for info in shader_replace_info_list:
            prefix = str(info['name_prefix']).strip()
            for shader_index, shader in enumerate(get_normalized_shaders(info), start=1):
                src = str(shader.get('shader_file_path', '') or '').strip()
                if not src:
                    continue
                if not os.path.isfile(src):
                    raise RuntimeError(f"着色器替换文件不存在: {src}")

                variant = shader['variant_name']
                variant_id = shader['variant_id']
                shader_hash = str(shader.get('shader_hash', '') or '').strip()
                extension = os.path.splitext(src)[1] or ".txt"
                hash_or_index = shader_hash or str(shader_index)
                export_filename = f"{prefix}_{variant_id}_{hash_or_index}{extension}"
                dst = os.path.join(shaders_dir, export_filename)
                try:
                    if not (os.path.exists(dst) and os.path.samefile(src, dst)):
                        shutil.copy2(src, dst)
                except Exception as exc:
                    raise RuntimeError(f"复制着色器替换文件失败: {src} -> {dst}: {exc}") from exc
                shader_export_filenames[
                    (prefix.casefold(), shader['group_index'], variant.casefold())
                ] = export_filename

        # --- 在 [Constants] 中声明变量 ---
        constants_section = None
        for section in ini_builder.ini_section_list:
            if section.SectionType == M_SectionType.Constants:
                constants_section = section
                break

        is_new_constants = constants_section is None
        if is_new_constants:
            constants_section = M_IniSection(M_SectionType.Constants)
            constants_section.SectionName = "Constants"

        def has_global_declaration(variable_name):
            declaration_pattern = re.compile(
                rf'^global(?:\s+persist)?\s+{re.escape(variable_name)}\s*=',
                re.IGNORECASE,
            )
            return any(
                declaration_pattern.match(str(line or "").strip())
                for line in constants_section.SectionLineList
            )

        for info in shader_replace_info_list:
            prefix = str(info.get('name_prefix', '') or '').strip()
            ps_replace_var = f"${prefix}_ps_replace"
            env_a_var = f"${prefix}_env_a"
            ps_line = f"global persist {ps_replace_var} = 0"
            env_line = f"global persist {env_a_var} = 0"
            if not has_global_declaration(ps_replace_var):
                constants_section.append(ps_line)
            if not has_global_declaration(env_a_var):
                constants_section.append(env_line)
        constants_section.new_line()

        # 新建的 Constants 段在填充内容后再注册到 ini_builder
        if is_new_constants:
            ini_builder.append_section(constants_section)

        # 按 DrawCall 解析链级配置；同名普通链路不能回退到另一条链的配置。
        sr_draw_models = []
        draw_model_info_map = {}
        for draw_model in draw_call_models:
            resolved_infos = cls.get_draw_call_shader_replace_info_list(
                draw_model,
                shader_replace_object_names=shader_replace_object_names,
                shader_replace_object_info_map=shader_replace_object_info_map,
                shader_replace_info_list=shader_replace_info_list,
            )
            if not resolved_infos:
                continue
            sr_draw_models.append(draw_model)
            draw_model_info_map[id(draw_model)] = resolved_infos

        generated_keytoggle_names = set()
        generated_override_names = set()
        for info in shader_replace_info_list:
            prefix = str(info.get('name_prefix', '') or '').strip()
            toggle_key = str(info.get('toggle_key', '') or '').strip()
            shaders = get_normalized_shaders(info)
            ps_replace_var = f"${prefix}_ps_replace"
            env_a_var = f"${prefix}_env_a"
            group_count = max((shader['group_index'] for shader in shaders), default=1)

            # --- KeyToggle 段（在最上面，去重） ---
            keytoggle_name = f"KeyToggle_{prefix}"
            if toggle_key and keytoggle_name not in generated_keytoggle_names:
                generated_keytoggle_names.add(keytoggle_name)
                keytoggle_section = M_IniSection(M_SectionType.ShaderReplace)
                keytoggle_section.SectionName = keytoggle_name
                keytoggle_section.append(f"key = {toggle_key}")
                keytoggle_section.append("type = cycle")
                cycle_values = ",".join(str(value) for value in range(group_count + 1))
                keytoggle_section.append(f"{ps_replace_var} = {cycle_values},")
                keytoggle_section.new_line()
                ini_builder.append_section(keytoggle_section)

            # --- ShaderOverride 段（每个着色器哈希一个，去重） ---
            override_entries = {}
            for shader in shaders:
                shader_hash = str(shader.get('shader_hash', '') or '').strip()
                if not shader_hash:
                    continue
                override_entries.setdefault(shader_hash.casefold(), []).append(shader)

            for hash_entries in override_entries.values():
                shader = hash_entries[0]
                shader_hash = str(shader.get('shader_hash', '') or '').strip()
                variant_id = shader['variant_id']
                env_value = shader['env_value']
                active_groups = sorted({entry['group_index'] for entry in hash_entries})
                so_name = f"ShaderOverride_{prefix}EnvA_{variant_id}"
                if so_name in generated_override_names:
                    continue
                generated_override_names.add(so_name)
                so_section = M_IniSection(M_SectionType.ShaderReplace)
                so_section.SectionName = so_name
                so_section.append(f"hash = {shader_hash}")
                so_section.append("allow_duplicate_hash = overrule")
                condition = " || ".join(
                    f"{ps_replace_var} == {group_index}" for group_index in active_groups
                )
                so_section.append(f"if {condition}")
                so_section.append(f"    {env_a_var} = {env_value}")
                so_section.append("else")
                so_section.append(f"    {env_a_var} = 0")
                so_section.append("endif")
                so_section.new_line()
                ini_builder.append_section(so_section)

        # --- CustomShader 段（放在最下面，按物体关联的 info 生成，去重） ---
        generated_section_names = set()
        for dm in sr_draw_models:
            obj_infos = draw_model_info_map[id(dm)]

            for info in obj_infos:
                prefix = str(info.get('name_prefix', '') or '').strip()
                component = info.get('component_index', 0)
                shaders = get_normalized_shaders(info)
                ib_hash = dm.match_draw_ib or "0"
                first_index = dm.match_first_index if dm.match_first_index else "0"
                index_count = dm.index_count or 0
                index_offset = (draw_call_offset_map or {}).get(id(dm), dm.index_offset) or 0
                # 普通绘制为 0；ZZMI 合并网格重定向会把 carrier 的 draw
                # 放到 target SO，并在 run 逻辑中携带非零 base_vertex。这里
                # 必须使用同一身份映射生成 CustomShader 段，否则 run 会
                # 引用一个没有被创建的 section。
                base_vertex = int(
                    (draw_call_base_vertex_map or {}).get(id(dm), 0) or 0
                )
                if use_instanced_draw:
                    drawindexed_str = (
                        f"drawindexedinstanced = {index_count},INSTANCE_COUNT,"
                        f"{index_offset},{base_vertex},FIRST_INSTANCE"
                    )
                else:
                    drawindexed_str = f"drawindexed = {index_count},{index_offset},{base_vertex}"

                # 为每个变体生成 CustomShader 段
                for shader in shaders:
                    variant = shader['variant_name']
                    variant_id = shader['variant_id']
                    src = str(shader.get('shader_file_path', '') or '').strip()
                    section_name = cls._build_custom_shader_section_name(
                        prefix, ib_hash, first_index, component,
                        index_count, index_offset, base_vertex, variant_id
                    )
                    if section_name in generated_section_names:
                        continue
                    generated_section_names.add(section_name)
                    cs_section = M_IniSection(M_SectionType.ShaderReplace)
                    cs_section.SectionName = section_name
                    if src:
                        filename = shader_export_filenames.get(
                            (prefix.casefold(), shader['group_index'], variant.casefold())
                        )
                        if not filename:
                            raise RuntimeError(
                                f"着色器替换文件未准备完成: prefix={prefix}, variant={variant}"
                            )
                        cs_section.append(f"ps = ./Shaders/{filename}")
                    cs_section.append("handling = skip")
                    cs_section.append(drawindexed_str)
                    cs_section.new_line()
                    ini_builder.append_section(cs_section)

                # Normal 变体（无着色器文件）
                normal_name = cls._build_custom_shader_section_name(
                    prefix, ib_hash, first_index, component,
                    index_count, index_offset, base_vertex, "Normal"
                )
                if normal_name not in generated_section_names:
                    generated_section_names.add(normal_name)
                    normal_section = M_IniSection(M_SectionType.ShaderReplace)
                    normal_section.SectionName = normal_name
                    normal_section.append("handling = skip")
                    normal_section.append(drawindexed_str)
                    normal_section.new_line()
                    ini_builder.append_section(normal_section)

    @classmethod
    def get_shader_replace_run_logic(cls, info, ib_hash, first_index, component, index_count, index_offset, base_vertex=0):
        """生成替换 drawindexed 的条件运行逻辑行列表。

        返回 None 表示无需替换。
        """
        prefix = str(info.get('name_prefix', '') or '').strip()
        shaders = cls._normalize_shader_replace_shaders(info)
        ps_replace_var = f"${prefix}_ps_replace"
        env_a_var = f"${prefix}_env_a"

        lines = []
        if shaders:
            normal_name = cls._build_custom_shader_section_name(
                prefix, ib_hash, first_index, component,
                index_count, index_offset, base_vertex, "Normal"
            )
            groups = {}
            for shader in shaders:
                groups.setdefault(shader['group_index'], []).append(shader)

            for group_position, (group_index, group_shaders) in enumerate(sorted(groups.items())):
                outer_keyword = "if" if group_position == 0 else "else if"
                lines.append(f"{outer_keyword} {ps_replace_var} == {group_index}")
                for variant_position, shader in enumerate(group_shaders):
                    variant_id = shader['variant_id']
                    section_name = cls._build_custom_shader_section_name(
                        prefix, ib_hash, first_index, component,
                        index_count, index_offset, base_vertex, variant_id
                    )
                    inner_keyword = "if" if variant_position == 0 else "else if"
                    lines.append(f"    {inner_keyword} {env_a_var} == {shader['env_value']}")
                    lines.append(f"        run = {section_name}")
                lines.append("    else")
                lines.append(f"        run = {normal_name}")
                lines.append("    endif")
        else:
            normal_name = cls._build_custom_shader_section_name(
                prefix, ib_hash, first_index, component,
                index_count, index_offset, base_vertex, "Normal"
            )
            lines.append(f"if {ps_replace_var} == 1")
            lines.append(f"    run = {normal_name}")

        lines.append("else")
        lines.append(f"    run = {normal_name}")
        lines.append("endif")

        return lines
