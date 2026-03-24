import os

from ..base.config.main_config import GlobalConfig
from ..base.config.global_properties import GlobalProterties
from ..helper.global_key_count_helper import GlobalKeyCountHelper
from ..helper.m_ini_helper import M_IniHelper
from ..helper.m_ini_helper_gui import M_IniHelperGUI
from ..common.migoto.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from .drawib_export_base import DrawIBExportBase


class ExportSnowBreak(DrawIBExportBase):
    def __init__(self, blueprint_model):
        super().__init__(blueprint_model=blueprint_model, combine_ib=False)

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
        draw_ib = drawib_model.draw_ib
        d3d11_game_type = drawib_model.d3d11GameType

        for count_i, part_name in enumerate(drawib_model.part_name_list):
            match_first_index = str(drawib_model.match_first_index_list[count_i])
            style_part_name = "Component" + part_name
            texture_override_name_suffix = "IB_" + draw_ib + "_" + drawib_model.draw_ib_alias + "_" + style_part_name
            ib_resource_name = drawib_model.PartName_IBResourceName_Dict.get(part_name, "")

            texture_override_ib_section.append("[Resource_IB_Bak_" + str(count_i) + "]")
            texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
            texture_override_ib_section.append("hash = " + draw_ib)
            texture_override_ib_section.append("match_first_index = " + match_first_index)
            texture_override_ib_section.append("handling = skip")
            texture_override_ib_section.append("Resource_IB_Bak_" + str(count_i) + " = ref ib")
            texture_override_ib_section.append("checktextureoverride = vb0")

            if not GlobalProterties.forbid_auto_texture_ini():
                texture_markup_info_list = drawib_model.partname_texturemarkinfolist_dict.get(part_name, None)
                if texture_markup_info_list is not None:
                    for texture_markup_info in texture_markup_info_list:
                        if texture_markup_info.mark_type == "Hash":
                            texture_override_ib_section.append("checktextureoverride = " + texture_markup_info.mark_slot)

            texture_override_ib_section.append("ib = " + ib_resource_name)

            for original_category_name in d3d11_game_type.CategoryDrawCategoryDict.keys():
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_ib_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            if not GlobalProterties.forbid_auto_texture_ini():
                texture_markup_info_list = drawib_model.partname_texturemarkinfolist_dict.get(part_name, None)
                if texture_markup_info_list is not None:
                    for texture_markup_info in texture_markup_info_list:
                        if texture_markup_info.mark_type == "Slot":
                            texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

            if not d3d11_game_type.GPU_PreSkinning:
                for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                    if original_category_name == draw_category_name:
                        category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                        texture_override_ib_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            submesh_model = drawib_model.part_name_submesh_dict.get(part_name)
            if submesh_model is not None:
                for drawindexed_str in M_IniHelper.get_drawindexed_str_list(
                    submesh_model.drawcall_model_list,
                    obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
                ):
                    texture_override_ib_section.append(drawindexed_str)

            texture_override_ib_section.append("ib = Resource_IB_Bak_" + str(count_i))

        ini_builder.append_section(texture_override_ib_section)

    def add_unity_vs_resource_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        resource_vb_section = M_IniSection(M_SectionType.ResourceBuffer)
        buffer_folder_name = GlobalConfig.get_buffer_folder_name()
        for category_name in drawib_model.d3d11GameType.OrderedCategoryNameList:
            resource_vb_section.append("[Resource" + drawib_model.draw_ib + category_name + "]")
            resource_vb_section.append("type = Buffer")
            resource_vb_section.append("stride = " + str(drawib_model.d3d11GameType.CategoryStrideDict[category_name]))
            resource_vb_section.append("filename = " + buffer_folder_name + "/" + drawib_model.draw_ib + "-" + category_name + ".buf")
            resource_vb_section.new_line()

        for part_name, ib_filename in drawib_model.PartName_IBBufferFileName_Dict.items():
            ib_resource_name = drawib_model.PartName_IBResourceName_Dict.get(part_name, None)
            resource_vb_section.append("[" + ib_resource_name + "]")
            resource_vb_section.append("type = Buffer")
            resource_vb_section.append("format = DXGI_FORMAT_R32_UINT")
            resource_vb_section.append("filename = " + buffer_folder_name + "/" + ib_filename)
            resource_vb_section.new_line()
        ini_builder.append_section(resource_vb_section)

    def add_resource_texture_sections(self, ini_builder: M_IniBuilder, drawib_model):
        if GlobalProterties.forbid_auto_texture_ini():
            return
        resource_texture_section = M_IniSection(M_SectionType.ResourceTexture)
        for texture_markup_info_list in drawib_model.partname_texturemarkinfolist_dict.values():
            for texture_markup_info in texture_markup_info_list:
                if texture_markup_info.mark_type == "Slot":
                    resource_texture_section.append("[" + texture_markup_info.get_resource_name() + "]")
                    resource_texture_section.append("filename = Texture/" + texture_markup_info.mark_filename)
                    resource_texture_section.new_line()
        ini_builder.append_section(resource_texture_section)

    def export(self):
        self.generate_buffer_files(GlobalConfig.path_generatemod_buffer_folder())
        ini_builder = M_IniBuilder()
        drawib_drawibmodel_dict = {drawib_model.draw_ib: drawib_model for drawib_model in self.drawib_model_list}
        M_IniHelper.generate_hash_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        for drawib_model in self.drawib_model_list:
            self.add_unity_vs_texture_override_ib_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_unity_vs_resource_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            self.add_resource_texture_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)
            GlobalKeyCountHelper.generated_mod_number = GlobalKeyCountHelper.generated_mod_number + 1
        M_IniHelper.add_branch_key_sections(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        M_IniHelper.add_shapekey_ini_sections(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelperGUI.add_branch_mod_gui_section(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        ini_builder.save_to_file(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.workspacename + ".ini"))