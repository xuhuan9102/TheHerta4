import os

from ..base.global_config import GlobalConfig
from ..base.global_properties import GlobalProterties
from ..helper.global_key_count_helper import GlobalKeyCountHelper
from ..helper.m_ini_helper import M_IniHelper
from ..helper.m_ini_helper_gui import M_IniHelperGUI
from ..common.migoto.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from .unity import ExportUnity


class GIMITextureMarkName:
    DiffuseMap = "DiffuseMap"
    NormalMap = "NormalMap"
    LightMap = "LightMap"


class ExportGIMI(ExportUnity):
    def add_unity_vs_texture_override_vlr_section(self, ini_builder: M_IniBuilder, drawib_model, include_uav_byte_stride: bool = False):
        super().add_unity_vs_texture_override_vlr_section(
            ini_builder=ini_builder,
            drawib_model=drawib_model,
            include_uav_byte_stride=include_uav_byte_stride,
        )

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
        draw_ib = drawib_model.draw_ib

        texture_override_ib_section.append("[TextureOverride_IB_" + draw_ib + "]")
        texture_override_ib_section.append("hash = " + draw_ib)
        texture_override_ib_section.append("handling = skip")
        texture_override_ib_section.new_line()

        for submesh_model in drawib_model.submesh_model_list:
            texture_override_name_suffix = drawib_model.get_submesh_texture_override_suffix(submesh_model)
            ib_resource_name = drawib_model.get_submesh_ib_resource_name(submesh_model)

            texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
            texture_override_ib_section.append("hash = " + draw_ib)
            texture_override_ib_section.append("match_first_index = " + str(submesh_model.match_first_index))

            ib_buf = drawib_model.submesh_ib_dict.get(submesh_model.unique_str, None)
            if ib_buf is None or len(ib_buf) == 0:
                texture_override_ib_section.append("ib = null")
                texture_override_ib_section.new_line()
                continue

            texture_override_ib_section.append("ib = " + ib_resource_name)

            if not GlobalProterties.forbid_auto_texture_ini():
                texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
                normal_exists = False

                if GlobalProterties.gimi_use_orfix() and texture_markup_info_list:
                    for texture_markup_info in texture_markup_info_list:
                        if texture_markup_info.mark_name == GIMITextureMarkName.NormalMap:
                            normal_exists = True

                    altered_texture_markup_info_list = []
                    if normal_exists:
                        for texture_markup_info in texture_markup_info_list:
                            if texture_markup_info.mark_name == GIMITextureMarkName.NormalMap:
                                texture_markup_info.mark_slot = "ps-t0"
                            elif texture_markup_info.mark_name == GIMITextureMarkName.DiffuseMap:
                                texture_markup_info.mark_slot = "ps-t1"
                            elif texture_markup_info.mark_name == GIMITextureMarkName.LightMap:
                                texture_markup_info.mark_slot = "ps-t2"
                            altered_texture_markup_info_list.append(texture_markup_info)
                    else:
                        for texture_markup_info in texture_markup_info_list:
                            if texture_markup_info.mark_name == GIMITextureMarkName.DiffuseMap:
                                texture_markup_info.mark_slot = "ps-t0"
                            elif texture_markup_info.mark_name == GIMITextureMarkName.LightMap:
                                texture_markup_info.mark_slot = "ps-t1"
                            altered_texture_markup_info_list.append(texture_markup_info)
                    texture_markup_info_list = altered_texture_markup_info_list

                slot_replace_exists = False
                if texture_markup_info_list:
                    for texture_markup_info in texture_markup_info_list:
                        if texture_markup_info.mark_type == "Slot":
                            slot_replace_exists = True
                            texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

                if GlobalProterties.gimi_use_orfix() and slot_replace_exists:
                    if normal_exists:
                        texture_override_ib_section.append("run = CommandList\\global\\ORFix\\ORFix")
                    else:
                        texture_override_ib_section.append("run = CommandList\\global\\ORFix\\NNFix")

            for drawindexed_str in M_IniHelper.get_drawindexed_str_list(
                submesh_model.drawcall_model_list,
                obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
            ):
                texture_override_ib_section.append(drawindexed_str)

        ini_builder.append_section(texture_override_ib_section)

    def export(self):
        self.generate_buffer_files(GlobalConfig.path_generatemod_buffer_folder())
        ini_builder = M_IniBuilder()
        drawib_drawibmodel_dict = {drawib_model.draw_ib: drawib_model for drawib_model in self.drawib_model_list}

        M_IniHelper.generate_hash_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        for drawib_model in self.drawib_model_list:
            self.add_unity_vs_texture_override_vlr_section(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_unity_vs_texture_override_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_unity_vs_texture_override_ib_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_unity_vs_resource_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_resource_texture_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)
            GlobalKeyCountHelper.generated_mod_number = GlobalKeyCountHelper.generated_mod_number + 1

        M_IniHelper.add_branch_key_sections(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        M_IniHelper.add_shapekey_ini_sections(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelperGUI.add_branch_mod_gui_section(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        ini_builder.save_to_file(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.workspacename + ".ini"))