import os

from ..base.global_config import GlobalConfig
from ..base.config.global_properties import GlobalProterties
from ..helper.global_key_count_helper import GlobalKeyCountHelper
from ..helper.m_ini_helper import M_IniHelper
from ..helper.m_ini_helper_gui import M_IniHelperGUI
from ..common.migoto.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from .drawib_export_base import DrawIBExportBase


class ExportYYSLS(DrawIBExportBase):
    def __init__(self, blueprint_model):
        super().__init__(blueprint_model=blueprint_model, combine_ib=False)

    @staticmethod
    def _get_submesh_ib_resource_name(submesh_model) -> str:
        return "Resource_" + submesh_model.unique_str.replace("-", "_") + "_Index"

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
        draw_ib = drawib_model.draw_ib
        d3d11_game_type = drawib_model.d3d11GameType
        part_name_by_unique_str = {
            submesh_model.unique_str: part_name
            for part_name, submesh_model in drawib_model.part_name_submesh_dict.items()
        }

        for submesh_model in drawib_model.submesh_model_list:
            match_first_index = str(submesh_model.match_first_index)
            texture_override_name_suffix = submesh_model.unique_str.replace("-", "_")
            ib_resource_name = self._get_submesh_ib_resource_name(submesh_model)

            texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
            texture_override_ib_section.append("hash = " + draw_ib)
            texture_override_ib_section.append("match_first_index = " + match_first_index)
            texture_override_ib_section.append("match_index_count = " + str(submesh_model.match_index_count))
            texture_override_ib_section.append("handling = skip")
            
            ib_buf = drawib_model.submesh_ib_dict.get(submesh_model.unique_str, None)
            if ib_buf is None or len(ib_buf) == 0:
                texture_override_ib_section.append("ib = null")
                texture_override_ib_section.new_line()
                continue

            for original_category_name in d3d11_game_type.CategoryDrawCategoryDict.keys():
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_ib_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            texture_override_ib_section.append("ib = " + ib_resource_name)

            if not GlobalProterties.forbid_auto_texture_ini():
                texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
                if texture_markup_info_list:
                    for texture_markup_info in texture_markup_info_list:
                        if texture_markup_info.mark_type == "Slot":
                            texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

            if not d3d11_game_type.GPU_PreSkinning:
                for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                    if original_category_name == draw_category_name:
                        category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                        texture_override_ib_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            # TODO 这里注意，YYSLS大世界中使用的是DrawindexedInstancedIndirect
            # 第一个参数是一个专门的参数Buffer，但是我们目前还没发构造这个参数Buffer，所以要加上
            # 第二个参数是Buffer的偏移量，因为一般是一个巨大的Buffer包含了这一帧所有要绘制的内容
            # 但是角色外观界面，使用的是DrawIndexed
            # 所以这个能够兼容的方法仍然需要摸索，也许能够通过某种DRAW_TYPE来进行过滤？
            # emmmm，总之后面测试的时候在考虑，暂时记录在此
            for drawindexed_str in M_IniHelper.get_drawindexed_instanced_str_list(
                submesh_model.drawcall_model_list,
                obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
            ):
                texture_override_ib_section.append(drawindexed_str)

            if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                texture_override_ib_section.append("$active" + str(GlobalKeyCountHelper.generated_mod_number) + " = 1")
                if GlobalProterties.generate_branch_mod_gui():
                    texture_override_ib_section.append("$ActiveCharacter = 1")

        ini_builder.append_section(texture_override_ib_section)

    def add_unity_vs_resource_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        resource_vb_section = M_IniSection(M_SectionType.ResourceBuffer)
        buffer_folder_name = "Meshes"
        for category_name in drawib_model.d3d11GameType.OrderedCategoryNameList:
            resource_vb_section.append("[Resource" + drawib_model.draw_ib + category_name + "]")
            resource_vb_section.append("type = Buffer")
            resource_vb_section.append("stride = " + str(drawib_model.d3d11GameType.CategoryStrideDict[category_name]))
            resource_vb_section.append("filename = " + buffer_folder_name + "/" + drawib_model.draw_ib + "-" + category_name + ".buf")
            resource_vb_section.new_line()

        for submesh_model in drawib_model.submesh_model_list:
            ib_resource_name = self._get_submesh_ib_resource_name(submesh_model)
            resource_vb_section.append("[" + ib_resource_name + "]")
            resource_vb_section.append("type = Buffer")
            resource_vb_section.append("format = DXGI_FORMAT_R32_UINT")
            resource_vb_section.append("filename = " + buffer_folder_name + "/" + submesh_model.unique_str + "-Index.buf")
            resource_vb_section.new_line()
        ini_builder.append_section(resource_vb_section)

    def add_resource_texture_sections(self, ini_builder: M_IniBuilder, drawib_model):
        if GlobalProterties.forbid_auto_texture_ini():
            return
        resource_texture_section = M_IniSection(M_SectionType.ResourceTexture)
        appended_resource_names = set()
        for submesh_model in drawib_model.submesh_model_list:
            for texture_markup_info in drawib_model.get_submesh_texture_markup_info_list(submesh_model):
                if texture_markup_info.mark_type == "Slot":
                    resource_name = texture_markup_info.get_resource_name()
                    if resource_name in appended_resource_names:
                        continue
                    appended_resource_names.add(resource_name)
                    resource_texture_section.append("[" + texture_markup_info.get_resource_name() + "]")
                    resource_texture_section.append("filename = Textures/" + texture_markup_info.mark_filename)
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