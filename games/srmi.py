from ..common.export.blueprint_model import BluePrintModel
from dataclasses import dataclass,field
from ..base.config.main_config import GlobalConfig
from ..base.config.global_properties import GlobalProterties

from ..helper.buffer_export_helper import BufferExportHelper
from ..helper.global_key_count_helper import GlobalKeyCountHelper
from ..helper.m_ini_helper import M_IniHelper
from ..helper.m_ini_helper_gui import M_IniHelperGUI
from ..common.migoto.m_ini_builder import M_IniBuilder,M_IniSection, M_SectionType
from .export_helper import ExportHelper
from ..common.export.drawib_model import DrawIBModel

import os

@dataclass
class ExportSRMI:

    blueprint_model:BluePrintModel

    drawib_model_list:list[DrawIBModel] = field(default_factory=list,init=False)

    def __post_init__(self):
        self.drawib_model_list = ExportHelper.parse_drawib_model_list_from_blueprint_model(blueprint_model=self.blueprint_model,combine_ib=False)

    def generate_buffer_files(self):
        buf_output_folder = GlobalConfig.path_generatemod_buffer_folder()

        for drawib_model in self.drawib_model_list:
            draw_ib = drawib_model.draw_ib
            
            if drawib_model.combine_ib:
                ib_filename = draw_ib + "-Index.buf"
                ib_filepath = os.path.join(buf_output_folder, ib_filename)
                BufferExportHelper.write_buf_ib_r32_uint(drawib_model.ib, ib_filepath)
            else:
                for submesh_model in drawib_model.submesh_model_list:
                    ib = drawib_model.submesh_ib_dict.get(submesh_model.unique_str, [])
                    ib_filename = submesh_model.unique_str + "-Index.buf"
                    ib_filepath = os.path.join(buf_output_folder, ib_filename)
                    BufferExportHelper.write_buf_ib_r32_uint(ib, ib_filepath)
            
            # 生成VB文件
            for category, category_buf in drawib_model.category_buffer_dict.items():
                category_buf_filename = draw_ib + "-" + category + ".buf"
                category_buf_filepath = os.path.join(buf_output_folder, category_buf_filename)
                with open(category_buf_filepath, 'wb') as f:
                    category_buf.tofile(f)

            for shapekey_name, shapekey_buf in drawib_model.shapekey_name_bytelist_dict.items():
                shapekey_buf_filename = draw_ib + "-Position." + shapekey_name + ".buf"
                shapekey_buf_filepath = os.path.join(buf_output_folder, shapekey_buf_filename)
                with open(shapekey_buf_filepath, 'wb') as f:
                    shapekey_buf.tofile(f)
            
    def generate_ini_file(self):
        ini_builder = M_IniBuilder()
        drawib_drawibmodel_dict = {
            drawib_model.draw_ib: drawib_model
            for drawib_model in self.drawib_model_list
        }
        draw_ib_active_index_dict = {
            drawib_model.draw_ib: index
            for index, drawib_model in enumerate(self.drawib_model_list)
        }

        M_IniHelper.generate_hash_style_texture_ini(
            ini_builder=ini_builder,
            drawib_drawibmodel_dict=drawib_drawibmodel_dict,
        )

        for drawib_model in self.drawib_model_list:
            draw_ib = drawib_model.draw_ib
            draw_ib_alias = drawib_model.draw_ib_alias
            d3d11_game_type = drawib_model.d3d11_game_type
            active_index = draw_ib_active_index_dict.get(draw_ib, 0)

            part_name_to_submesh_dict = {}
            for submesh_model in drawib_model.submesh_model_list:
                part_name = None
                if submesh_model.match_first_index in drawib_model.match_first_index_list:
                    part_index = drawib_model.match_first_index_list.index(submesh_model.match_first_index)
                    if part_index < len(drawib_model.part_name_list):
                        part_name = drawib_model.part_name_list[part_index]
                if part_name is None:
                    part_name = str(len(part_name_to_submesh_dict))
                part_name_to_submesh_dict[part_name] = submesh_model

            if d3d11_game_type.GPU_PreSkinning and drawib_model.vertex_limit_hash:
                vertexlimit_section = M_IniSection(M_SectionType.TextureOverrideVertexLimitRaise)
                vertexlimit_section.append("[TextureOverride_" + draw_ib + "_" + draw_ib_alias + "_Draw]")
                vertexlimit_section.append("hash = " + drawib_model.vertex_limit_hash)
                if drawib_model.vertex_count > drawib_model.original_vertex_count:
                    vertexlimit_section.append("override_byte_stride = " + str(d3d11_game_type.CategoryStrideDict.get("Position", 0)))
                    vertexlimit_section.append("override_vertex_count = " + str(drawib_model.vertex_count))
                    vertexlimit_section.append("uav_byte_stride = 4")
                    vertexlimit_section.new_line()
                ini_builder.append_section(vertexlimit_section)

            if not GlobalProterties.forbid_auto_texture_ini() and drawib_model.partname_texturemarkinfolist_dict:
                resource_texture_section = M_IniSection(M_SectionType.ResourceTexture)
                for part_name, texture_markup_info_list in drawib_model.partname_texturemarkinfolist_dict.items():
                    for texture_markup_info in texture_markup_info_list:
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        resource_texture_section.append("[" + texture_markup_info.get_resource_name() + "]")
                        resource_texture_section.append("filename = Texture/" + texture_markup_info.mark_filename)
                        resource_texture_section.new_line()
                ini_builder.append_section(resource_texture_section)

            if d3d11_game_type.GPU_PreSkinning:
                texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
                texture_override_vb_section.append("; " + draw_ib)
                for category_name in d3d11_game_type.OrderedCategoryNameList:
                    category_hash = drawib_model.category_hash_dict.get(category_name, "")
                    texture_override_vb_namesuffix = "VB_" + draw_ib + "_" + draw_ib_alias + "_" + category_name

                    if category_name != "Position":
                        texture_override_vb_section.append("[TextureOverride_" + texture_override_vb_namesuffix + "]")
                        if category_hash:
                            texture_override_vb_section.append("hash = " + category_hash)
                        if category_name != "Texcoord":
                            texture_override_vb_section.append("handling = skip")

                    for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                        if category_name != draw_category_name:
                            continue

                        if original_category_name == "Position":
                            continue
                        if original_category_name == "Blend":
                            texture_override_vb_section.append("vb2 = Resource" + draw_ib + "Blend")
                            texture_override_vb_section.append("if DRAW_TYPE == 1")
                            texture_override_vb_section.append("  vb0 = Resource" + draw_ib + "Position")
                            texture_override_vb_section.append("draw = " + str(drawib_model.vertex_count) + ", 0")
                            texture_override_vb_section.append("endif")
                            texture_override_vb_section.append("if DRAW_TYPE == 8")
                            texture_override_vb_section.append("  Resource\\SRMI\\PositionBuffer = ref Resource" + draw_ib + "PositionCS")
                            texture_override_vb_section.append("  Resource\\SRMI\\BlendBuffer = ref Resource" + draw_ib + "BlendCS")
                            texture_override_vb_section.append("  $\\SRMI\\vertex_count = " + str(drawib_model.vertex_count))
                            texture_override_vb_section.append("endif")
                        else:
                            category_original_slot = d3d11_game_type.CategoryExtractSlotDict.get(original_category_name, "")
                            if category_original_slot:
                                texture_override_vb_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

                    if category_name == d3d11_game_type.CategoryDrawCategoryDict.get("Position"):
                        if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                            texture_override_vb_section.append("$active" + str(active_index) + " = 1")
                            if GlobalProterties.generate_branch_mod_gui():
                                texture_override_vb_section.append("$ActiveCharacter = 1")

                    texture_override_vb_section.new_line()

                ini_builder.append_section(texture_override_vb_section)

            texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
            for count_i, part_name in enumerate(drawib_model.part_name_list):
                match_first_index = ""
                if count_i < len(drawib_model.match_first_index_list):
                    match_first_index = str(drawib_model.match_first_index_list[count_i])
                style_part_name = "Component" + part_name
                ib_resource_name = "Resource_" + draw_ib + "_" + style_part_name
                texture_override_ib_namesuffix = "IB_" + draw_ib + "_" + draw_ib_alias + "_" + style_part_name
                texture_override_ib_section.append("[TextureOverride_" + texture_override_ib_namesuffix + "]")
                texture_override_ib_section.append("hash = " + draw_ib)
                texture_override_ib_section.append("match_first_index = " + match_first_index)
                texture_override_ib_section.append("handling = skip")

                submesh_model = part_name_to_submesh_dict.get(part_name)
                ib_buf = drawib_model.submesh_ib_dict.get(submesh_model.unique_str, []) if submesh_model is not None else []
                if not ib_buf:
                    texture_override_ib_section.new_line()
                    continue

                texture_override_ib_section.append("ib = " + ib_resource_name)

                if not GlobalProterties.forbid_auto_texture_ini():
                    texture_markup_info_list = drawib_model.partname_texturemarkinfolist_dict.get(part_name, [])
                    for texture_markup_info in texture_markup_info_list:
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

                if not d3d11_game_type.GPU_PreSkinning:
                    for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                        if original_category_name != draw_category_name:
                            continue
                        category_original_slot = d3d11_game_type.CategoryExtractSlotDict.get(original_category_name, "")
                        if category_original_slot:
                            texture_override_ib_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

                if submesh_model is not None:
                    for draw_line in M_IniHelper.get_drawindexed_str_list(
                        submesh_model.drawcall_model_list,
                        obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
                    ):
                        texture_override_ib_section.append(draw_line)

                texture_override_ib_section.new_line()

            ini_builder.append_section(texture_override_ib_section)

            resource_buffer_section = M_IniSection(M_SectionType.ResourceBuffer)
            for category_name in d3d11_game_type.OrderedCategoryNameList:
                resource_buffer_section.append("[Resource" + draw_ib + category_name + "]")
                resource_buffer_section.append("type = Buffer")
                resource_buffer_section.append("stride = " + str(d3d11_game_type.CategoryStrideDict.get(category_name, 0)))
                resource_buffer_section.append("filename = Meshes\\" + draw_ib + "-" + category_name + ".buf")
                resource_buffer_section.new_line()

            for category_name in d3d11_game_type.OrderedCategoryNameList:
                if category_name == "Position" or category_name == "Blend":
                    resource_buffer_section.append("[Resource" + draw_ib + category_name + "CS]")
                    resource_buffer_section.append("type = StructuredBuffer")
                    resource_buffer_section.append("stride = " + str(d3d11_game_type.CategoryStrideDict.get(category_name, 0)))
                    resource_buffer_section.append("filename = Meshes\\" + draw_ib + "-" + category_name + ".buf")
                    resource_buffer_section.new_line()

            for part_name in drawib_model.part_name_list:
                style_part_name = "Component" + part_name
                submesh_model = part_name_to_submesh_dict.get(part_name)
                if submesh_model is None:
                    continue
                ib_resource_name = "Resource_" + draw_ib + "_" + style_part_name
                resource_buffer_section.append("[" + ib_resource_name + "]")
                resource_buffer_section.append("type = Buffer")
                resource_buffer_section.append("format = DXGI_FORMAT_R32_UINT")
                resource_buffer_section.append("filename = Meshes\\" + submesh_model.unique_str + "-Index.buf")
                resource_buffer_section.new_line()

            ini_builder.append_section(resource_buffer_section)

            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)

        GlobalKeyCountHelper.generated_mod_number = len(self.drawib_model_list)
        M_IniHelper.add_branch_key_sections(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )
        M_IniHelper.add_shapekey_ini_sections(
            ini_builder=ini_builder,
            drawib_drawibmodel_dict=drawib_drawibmodel_dict,
        )
        M_IniHelperGUI.add_branch_mod_gui_section(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )

        
        ini_filepath = os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.workspacename + ".ini")
        ini_builder.save_to_file(ini_filepath)
                


        

    def export(self):
        self.generate_buffer_files()
        self.generate_ini_file()

        
            
        

