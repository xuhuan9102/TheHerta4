import os
import shutil

from .m_ini_builder import *
from .m_key import M_Key
from .draw_call_model import DrawCallModel
from .drawib_model import DrawIBModel
from ..utils.json_utils import JsonUtils
from ..utils.format_utils import Fatal
from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .logic_name import LogicName

from .global_key_count_helper import GlobalKeyCountHelper
from .workspace_helper import WorkSpaceHelper
from ..blueprint.export_helper import BlueprintExportHelper
from .texture_metadata_helper import TextureMetadataResolver, TextureMarkUpInfo

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
    def _get_extract_gametype_folder_path(cls, draw_ib_model: DrawIBModel) -> str:
        primary_submesh_metadata = getattr(draw_ib_model, "primary_submesh_metadata", None)
        if primary_submesh_metadata is not None:
            extract_gametype_folder_path = getattr(primary_submesh_metadata, "extract_gametype_folder_path", "")
            if extract_gametype_folder_path:
                return extract_gametype_folder_path

        submesh_model_list = getattr(draw_ib_model, "submesh_model_list", [])
        if submesh_model_list:
            first_submesh_model = submesh_model_list[0]
            unique_str = getattr(first_submesh_model, "unique_str", "")
            d3d11_game_type = getattr(first_submesh_model, "d3d11_game_type", None)
            if unique_str and d3d11_game_type is not None:
                return os.path.join(
                    GlobalConfig.path_workspace_folder(),
                    unique_str,
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
        unique_str = getattr(submesh_model, "unique_str", "")
        if d3d11_game_type is None or unique_str == "":
            return ""

        return os.path.join(
            GlobalConfig.path_workspace_folder(),
            unique_str,
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
            unique_str = getattr(submesh_model, "unique_str", "")
            if d3d11_game_type is None or unique_str == "":
                continue

            candidate_source_path = os.path.join(
                GlobalConfig.path_workspace_folder(),
                unique_str,
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

        submesh_folder_name = getattr(submesh_model, "unique_str", "")
        print("M_IniHelper: Part " + str(part_name) + " 对应 unique_str: " + submesh_folder_name)
        return submesh_folder_name

    @classmethod
    def _get_hash_deduped_texture_info(cls, draw_ib_model: DrawIBModel, mark_hash: str):
        for submesh_model in getattr(draw_ib_model, "submesh_model_list", []):
            submesh_folder_name = getattr(submesh_model, "unique_str", "")
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
    ) -> list[str]:
        """获取 drawindexed 命令字符串列表
        
        根据 DrawCallModel 列表生成 drawindexed 命令，支持条件判断。
        会根据 condition_str 对 obj_model 进行分组，相同条件的放在同一个 if 块中。
        
        Args:
            ordered_draw_obj_model_list: DrawCallModel 列表，按绘制顺序排列
            obj_name_draw_offset_dict: 对象名称到绘制偏移的映射字典
            
        Returns:
            list[str]: drawindexed 命令字符串列表
        """
        # 传统的使用DrawIndexed方式调用这个
        # 在输出之前，我们需要根据condition对obj_model进行分组
        condition_str_obj_model_list_dict:dict[str,list[DrawCallModel]] = {}
        for obj_model in ordered_draw_obj_model_list:
            condition_str = obj_model.get_condition_str()

            obj_model_list = condition_str_obj_model_list_dict.get(condition_str,[])
            
            obj_model_list.append(obj_model)
            condition_str_obj_model_list_dict[condition_str] = obj_model_list
        
        drawindexed_str_list:list[str] = []
        for condition_str, obj_model_list in condition_str_obj_model_list_dict.items():
            if condition_str != "":
                drawindexed_str_list.append("if " + condition_str)
                for obj_model in obj_model_list:
                    display_name = getattr(obj_model, 'display_name', obj_model.obj_name)
                    drawindexed_str_list.append("  ; [mesh:" + display_name + "] [vertex_count:" + str(obj_model.vertex_count) + "]" )
                    drawindexed_str_list.append("  " + obj_model.get_drawindexed_str(obj_name_draw_offset_dict))
                drawindexed_str_list.append("endif")
            else:
                for obj_model in obj_model_list:
                    display_name = getattr(obj_model, 'display_name', obj_model.obj_name)
                    drawindexed_str_list.append("; [mesh:" + display_name + "] [vertex_count:" + str(obj_model.vertex_count) + "]" )
                    drawindexed_str_list.append(obj_model.get_drawindexed_str(obj_name_draw_offset_dict))
            drawindexed_str_list.append("")

        return drawindexed_str_list
    
    @classmethod
    def get_drawindexed_instanced_str_list(
        cls,
        ordered_draw_obj_model_list: list[DrawCallModel],
        obj_name_draw_offset_dict: dict[str, int] | None = None,
    ) -> list[str]:
        # 使用DrawIndexedInstanced方式调用这个
        # 在输出之前，我们需要根据condition对obj_model进行分组
        condition_str_obj_model_list_dict:dict[str,list[DrawCallModel]] = {}
        for obj_model in ordered_draw_obj_model_list:
            condition_str = obj_model.get_condition_str()

            obj_model_list = condition_str_obj_model_list_dict.get(condition_str,[])
            
            obj_model_list.append(obj_model)
            condition_str_obj_model_list_dict[condition_str] = obj_model_list
        
        drawindexed_str_list:list[str] = []
        for condition_str, obj_model_list in condition_str_obj_model_list_dict.items():
            if condition_str != "":
                drawindexed_str_list.append("if " + condition_str)
                for obj_model in obj_model_list:
                    display_name = getattr(obj_model, 'display_name', obj_model.obj_name)
                    drawindexed_str_list.append("  ; [mesh:" + display_name + "] [vertex_count:" + str(obj_model.vertex_count) + "]" )
                    drawindexed_str_list.append("  " + obj_model.get_drawindexed_instanced_str(obj_name_draw_offset_dict))
                drawindexed_str_list.append("endif")
            else:
                for obj_model in obj_model_list:
                    display_name = getattr(obj_model, 'display_name', obj_model.obj_name)
                    drawindexed_str_list.append("; [mesh:" + display_name + "] [vertex_count:" + str(obj_model.vertex_count) + "]" )
                    drawindexed_str_list.append("  " + obj_model.get_drawindexed_instanced_str(obj_name_draw_offset_dict))
            drawindexed_str_list.append("")

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
                submesh_folder_name = getattr(submesh_model, "unique_str", "")
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

            # 现在除了WWMI外都不使用全局Hash贴图风格，而是上面的标记的Hash风格贴图
            if GlobalConfig.logic_name != LogicName.WWMI:
                continue


            

        # if len(repeat_hash_list) != 0:
        #     texture_ini_builder.save_to_file(MainConfig.path_generate_mod_folder() + MainConfig.workspacename + "_Texture.ini")

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
    def add_shapekey_ini_sections(cls, ini_builder:M_IniBuilder,drawib_drawibmodel_dict:dict[str,DrawIBModel]):
        shapekeyname_mkey_dict = BlueprintExportHelper.get_current_shapekeyname_mkey_dict()
        if len(shapekeyname_mkey_dict.keys()) == 0:
            return

        # [Constants]
        constants_section = M_IniSection(M_SectionType.Constants)
        constants_section.append("[Constants]")
        constants_section.append("global persist $shapekey_first_run = 1")

        for shapekey_name, m_key in shapekeyname_mkey_dict.items():
            constants_section.append("; ShapeKey: " + shapekey_name)
            constants_section.append("global persist " + m_key.key_name + " = " + str(m_key.initialize_value))
            constants_section.new_line()

        ini_builder.append_section(constants_section)

        # [Present]
        present_section = M_IniSection(M_SectionType.Present)
        present_section.append("[Present]")
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

            # 原本的Buffer
            resource_section.append("[Resource" + drawib + "Position.1]")
            resource_section.append("type = buffer")
            resource_section.append("stride = " + str(d3d11_game_type.CategoryStrideDict["Position"]))
            resource_section.append("filename = Meshes\\" + drawib + "-" + "Position.buf")
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
                resource_section.append("filename = Meshes\\" + drawib + "-" + "Position." + shapekey_name + ".buf")
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

            # 声明 $active0 变量，用于物体切换功能的激活控制
            active_line = "global $active0"
            already_exists = any(active_line in line for line in constants_section.SectionLineList)
            if not already_exists:
                constants_section.append(active_line)

            for mkey in key_name_mkey_dict.values():
                # 跳过 swapkey，它们由 node_swap_ini.py 单独处理
                if getattr(mkey, 'key_name', '').startswith('$swapkey'):
                    continue
                
                key_str = "global persist " + mkey.key_name + " = " + str(mkey.initialize_value)
                already_exists = any(key_str in line for line in constants_section.SectionLineList)
                if not already_exists:
                    constants_section.append(key_str)


        if len(key_name_mkey_dict.keys()) != 0:
            present_section = M_IniSection(M_SectionType.Present)
            present_section.SectionName = "Present"
            
            # 在 Present 中重置 $active0，确保每次帧开始时为 0
            present_section.append("post $active0 = 0")
            ini_builder.append_section(present_section)
        
        key_number = 0
        if len(key_name_mkey_dict.keys()) != 0:

            for mkey in key_name_mkey_dict.values():
                # 跳过 swapkey，它们由 node_swap_ini.py 单独处理
                if getattr(mkey, 'key_name', '').startswith('$swapkey'):
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