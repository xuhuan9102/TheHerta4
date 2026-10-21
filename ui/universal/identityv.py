import os

from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...common.workspace_helper import WorkSpaceHelper
from .drawib_export_base import DrawIBExportBase
from ...utils.timer_utils import TimerUtils
import contextlib

# 「条件判断」后处理节点需要按 draw_ib 反查 ini 段：导出期用 draw_ib 上下文包住
# 「构建该 draw_ib 的段」的代码，M_IniSection 构造时会快照当前 draw_ib。
# 导入失败时导出照常，只是不登记段（独立加载/裁剪环境）。
try:
    from ...common import export_section_registry as _section_registry
except Exception:  # 独立加载/裁剪环境：退化为不登记，导出行为不变
    _section_registry = None


def _section_registry_context(draw_ib):
    """返回 draw_ib 的段登记上下文；登记表不可用时退化为空上下文（不改变导出行为）。"""
    if _section_registry is None:
        return contextlib.nullcontext()
    return _section_registry.draw_ib_context(draw_ib)


class ExportIdentityV(DrawIBExportBase):
    def __init__(self, blueprint_model):
        super().__init__(blueprint_model=blueprint_model, combine_ib=False)

    def _get_drawib_submesh_entries(self, drawib_model):
        existing_submesh_dict = {}
        for submesh_model in drawib_model.submesh_model_list:
            try:
                match_first_index = int(submesh_model.match_first_index)
            except (TypeError, ValueError):
                continue

            unique_str = getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            existing_submesh_dict[match_first_index] = {
                "match_first_index": match_first_index,
                "unique_str": unique_str,
                "submesh_model": submesh_model,
                "is_missing": False,
            }

        candidate_unique_strs = []
        candidate_base_folder_list = [
            GlobalConfig.path_workspace_folder(),
            *WorkSpaceHelper.get_workspace_partition_folderpath_list(),
        ]
        for base_folder in candidate_base_folder_list:
            if not os.path.isdir(base_folder):
                continue

            for submesh_folder_path in WorkSpaceHelper._get_submesh_folderpath_list_from(base_folder):
                candidate_unique_strs.append(os.path.basename(submesh_folder_path))

            for lod_name, submesh_folderpath_list in WorkSpaceHelper.get_lod_submesh_folderpath_dict(base_folder).items():
                for submesh_folder_path in submesh_folderpath_list:
                    candidate_unique_strs.append(
                        WorkSpaceHelper._compose_lod_name(lod_name, os.path.basename(submesh_folder_path))
                    )

        for candidate_unique_str in candidate_unique_strs:
            _lod_name, bare_unique_str = WorkSpaceHelper.parse_lod_unique_str(candidate_unique_str)
            if not bare_unique_str.startswith(drawib_model.draw_ib + "-"):
                continue

            name_splits = bare_unique_str.split("-")
            if len(name_splits) < 3:
                continue

            try:
                match_first_index = int(name_splits[2])
            except ValueError:
                continue

            if match_first_index in existing_submesh_dict:
                continue

            existing_submesh_dict[match_first_index] = {
                "match_first_index": match_first_index,
                "unique_str": candidate_unique_str,
                "submesh_model": None,
                "is_missing": True,
            }

        return [
            existing_submesh_dict[match_first_index]
            for match_first_index in sorted(existing_submesh_dict.keys())
        ]

    def _append_missing_texture_override_ib_section(self, texture_override_ib_section, draw_ib, submesh_entry):
        texture_override_name_suffix = submesh_entry["unique_str"].replace("-", "_")
        texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
        texture_override_ib_section.append("hash = " + draw_ib)
        texture_override_ib_section.append("match_first_index = " + str(submesh_entry["match_first_index"]))
        texture_override_ib_section.append("handling = skip")
        texture_override_ib_section.new_line()

    def add_unity_vs_texture_override_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        d3d11_game_type = drawib_model.d3d11GameType
        if not d3d11_game_type.GPU_PreSkinning:
            return

        texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
        texture_override_vb_section.append("; " + drawib_model.draw_ib)
        for category_name in d3d11_game_type.OrderedCategoryNameList:
            category_hash = drawib_model.category_hash_dict.get(category_name, "")
            texture_override_vb_name_suffix = "VB_" + drawib_model.draw_ib + "_" + drawib_model.draw_ib_alias + "_" + category_name
            texture_override_vb_section.append("[TextureOverride_" + texture_override_vb_name_suffix + "]")
            texture_override_vb_section.append("hash = " + category_hash)

            if category_name == d3d11_game_type.CategoryDrawCategoryDict["Position"]:
                texture_override_vb_section.append("override_byte_stride = " + str(d3d11_game_type.CategoryStrideDict["Position"]))
                texture_override_vb_section.append("override_vertex_count = " + str(drawib_model.draw_number))
                texture_override_vb_section.append("uav_byte_stride = 4")

            for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                if category_name != draw_category_name:
                    continue
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_vb_section.append(category_original_slot + " = Resource" + drawib_model.draw_ib + original_category_name)

            if category_name == d3d11_game_type.CategoryDrawCategoryDict["Position"]:
                if len(self.blueprint_model.keyname_mkey_dict.values()) != 0:
                    texture_override_vb_section.append("$active" + str(GlobalKeyCountHelper.generated_mod_number) + " = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_vb_section.append("$ActiveCharacter = 1")

            texture_override_vb_section.new_line()

        ini_builder.append_section(texture_override_vb_section)

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
        draw_ib = drawib_model.draw_ib
        d3d11_game_type = drawib_model.d3d11GameType

        for submesh_entry in self._get_drawib_submesh_entries(drawib_model):
            if submesh_entry["is_missing"]:
                self._append_missing_texture_override_ib_section(
                    texture_override_ib_section=texture_override_ib_section,
                    draw_ib=draw_ib,
                    submesh_entry=submesh_entry,
                )
                continue

            submesh_model = submesh_entry["submesh_model"]
            texture_override_name_suffix = drawib_model.get_submesh_texture_override_suffix(submesh_model)
            ib_resource_name = drawib_model.get_submesh_ib_resource_name(submesh_model)
            backup_resource_name = "Resource_IB_" + drawib_model.get_submesh_texture_override_suffix(submesh_model) + "_Bak"

            texture_override_ib_section.append("[" + backup_resource_name + "]")
            texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
            texture_override_ib_section.append("hash = " + draw_ib)
            texture_override_ib_section.append("match_first_index = " + str(submesh_model.match_first_index))
            texture_override_ib_section.append("handling = skip")
            texture_override_ib_section.append(backup_resource_name + " = ref ib")
            texture_override_ib_section.append("checktextureoverride = vb0")

            if not GlobalProterties.forbid_auto_texture_ini():
                texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
                if texture_markup_info_list:
                    for texture_markup_info in texture_markup_info_list:
                        if texture_markup_info.mark_type == "Hash":
                            texture_override_ib_section.append("checktextureoverride = " + texture_markup_info.mark_slot)

            texture_override_ib_section.append("ib = " + ib_resource_name)

            if not GlobalProterties.forbid_auto_texture_ini():
                texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
                if texture_markup_info_list:
                    for texture_markup_info in texture_markup_info_list:
                        if M_IniHelper.is_slot_binding_mark_type(texture_markup_info.mark_type):
                            texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

            if not d3d11_game_type.GPU_PreSkinning:
                for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                    if original_category_name == draw_category_name:
                        category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                        texture_override_ib_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            for drawindexed_str in M_IniHelper.get_drawindexed_str_list(
                submesh_model.drawcall_model_list,
                obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
            ):
                texture_override_ib_section.append(drawindexed_str)

            if not d3d11_game_type.GPU_PreSkinning and len(self.blueprint_model.keyname_mkey_dict.values()) != 0:
                texture_override_ib_section.append("$active" + str(GlobalKeyCountHelper.generated_mod_number) + " = 1")
                if GlobalProterties.generate_branch_mod_gui():
                    texture_override_ib_section.append("$ActiveCharacter = 1")

            texture_override_ib_section.append("ib = " + backup_resource_name)
            texture_override_ib_section.new_line()

        ini_builder.append_section(texture_override_ib_section)

    def add_unity_vs_resource_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        from ...blueprint.export_helper import BlueprintExportHelper
        resource_vb_section = M_IniSection(M_SectionType.ResourceBuffer)
        buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
        for category_name in drawib_model.d3d11GameType.OrderedCategoryNameList:
            resource_vb_section.append("[Resource" + drawib_model.draw_ib + category_name + "]")
            resource_vb_section.append("type = Buffer")
            resource_vb_section.append("stride = " + str(drawib_model.d3d11GameType.CategoryStrideDict[category_name]))
            resource_vb_section.append("filename = " + buffer_folder_name + "/" + drawib_model.draw_ib + "-" + category_name + ".buf")
            resource_vb_section.new_line()

        for submesh_model in drawib_model.submesh_model_list:
            ib_resource_name = drawib_model.get_submesh_ib_resource_name(submesh_model)
            resource_vb_section.append("[" + ib_resource_name + "]")
            resource_vb_section.append("type = Buffer")
            resource_vb_section.append("format = DXGI_FORMAT_R32_UINT")
            ib_name = getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            resource_vb_section.append("filename = " + buffer_folder_name + "/" + ib_name + "-Index.buf")
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
        TimerUtils.start_stage("缓冲文件生成")
        self.generate_buffer_files(GlobalConfig.path_generatemod_buffer_folder())
        TimerUtils.end_stage("缓冲文件生成")

        TimerUtils.start_stage("INI配置生成")
        ini_builder = M_IniBuilder()
        drawib_drawibmodel_dict = {drawib_model.draw_ib: drawib_model for drawib_model in self.drawib_model_list}
        M_IniHelper.generate_hash_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelper.generate_shared_slot_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)

        self._integrate_object_swap_ini_hook(ini_builder)

        for drawib_model in self.drawib_model_list:
            # 只把这个 draw_ib 自己的 ini 段包进登记上下文；贴图搬移与全局计数留在上下文外。
            with _section_registry_context(drawib_model.draw_ib):
                self.add_unity_vs_texture_override_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
                self.add_unity_vs_texture_override_ib_sections(ini_builder=ini_builder, drawib_model=drawib_model)
                self.add_unity_vs_resource_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
                self.add_resource_texture_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)
            GlobalKeyCountHelper.generated_mod_number = GlobalKeyCountHelper.generated_mod_number + 1

        M_IniHelper.add_branch_key_sections(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        M_IniHelper.add_shapekey_ini_sections(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelperGUI.add_branch_mod_gui_section(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        ini_builder.save_to_file(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + ".ini"))
        TimerUtils.end_stage("INI配置生成")
