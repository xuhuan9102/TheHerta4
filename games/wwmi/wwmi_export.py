import os

from ...base.config.global_properties import GlobalProterties
from ...base.config.main_config import GlobalConfig, LogicName
from ...common.export.drawib_model_wwmi import DrawIBModelWWMI
from ...common.export.blueprint_model import BluePrintModel
from ...common.migoto.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...helper.global_key_count_helper import GlobalKeyCountHelper
from ...helper.m_ini_helper import M_IniHelper
from ...helper.m_ini_helper_gui import M_IniHelperGUI


class ExportWWMI:
    def __init__(self, blueprint_model: BluePrintModel):
        self.blueprint_model = blueprint_model
        self.drawib_drawibmodel_dict: dict[str, DrawIBModelWWMI] = {}
        self.parse_draw_ib_draw_ib_model_dict()

    def parse_draw_ib_draw_ib_model_dict(self):
        ordered_draw_ib_list = []
        for drawcall_model in self.blueprint_model.ordered_draw_obj_data_model_list:
            draw_ib = drawcall_model.match_draw_ib
            if draw_ib in ordered_draw_ib_list:
                continue
            ordered_draw_ib_list.append(draw_ib)

        for draw_ib in ordered_draw_ib_list:
            draw_ib_model = DrawIBModelWWMI(draw_ib=draw_ib, blueprint_model=self.blueprint_model)
            self.drawib_drawibmodel_dict[draw_ib] = draw_ib_model

    def add_constants_section(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        constants_section = M_IniSection(M_SectionType.Constants)
        constants_section.append("[Constants]")
        constants_section.append("global $required_wwmi_version = 0.91")
        constants_section.append("global $object_guid = " + str(draw_ib_model.extracted_object.index_count))
        constants_section.append("global $mesh_vertex_count = " + str(draw_ib_model.mesh_vertex_count))
        constants_section.append("global $shapekey_vertex_count = " + str(len(draw_ib_model.obj_buffer_model_wwmi.shapekey_vertex_ids)))
        constants_section.append("global $mod_id = -1000")

        if GlobalProterties.import_merged_vgmap():
            constants_section.append("global $state_id = 0")

        constants_section.append("global $mod_enabled = 0")
        constants_section.append("global $object_detected = 0")
        constants_section.new_line()
        ini_builder.append_section(constants_section)

    def add_present_section(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        present_section = M_IniSection(M_SectionType.Present)
        present_section.append("[Present]")
        present_section.append("if $object_detected")
        present_section.append("  if $mod_enabled")
        present_section.append("    post $object_detected = 0")

        if GlobalProterties.import_merged_vgmap():
            if draw_ib_model.blend_remap:
                present_section.append("    run = CommandListInitializeBlendRemaps")
            present_section.append("    run = CommandListUpdateMergedSkeleton")

        present_section.append("  else")
        present_section.append("    if $mod_id == -1000")
        present_section.append("      run = CommandListRegisterMod")
        present_section.append("    endif")
        present_section.append("  endif")
        present_section.append("endif")
        present_section.new_line()
        ini_builder.append_section(present_section)

    def add_commandlist_register_mod_section(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        commandlist_section = M_IniSection(M_SectionType.CommandList)
        commandlist_section.append("[CommandListRegisterMod]")
        commandlist_section.append("$\\WWMIv1\\required_wwmi_version = $required_wwmi_version")
        commandlist_section.append("$\\WWMIv1\\object_guid = $object_guid")
        commandlist_section.append("Resource\\WWMIv1\\ModName = ref ResourceModName")
        commandlist_section.append("Resource\\WWMIv1\\ModAuthor = ref ResourceModAuthor")
        commandlist_section.append("Resource\\WWMIv1\\ModDesc = ref ResourceModDesc")
        commandlist_section.append("Resource\\WWMIv1\\ModLink = ref ResourceModLink")
        commandlist_section.append("Resource\\WWMIv1\\ModLogo = ref ResourceModLogo")
        commandlist_section.append("run = CommandList\\WWMIv1\\RegisterMod")
        commandlist_section.append("$mod_id = $\\WWMIv1\\mod_id")
        commandlist_section.append("if $mod_id >= 0")
        commandlist_section.append("  $mod_enabled = 1")
        commandlist_section.append("endif")
        commandlist_section.new_line()
        ini_builder.append_section(commandlist_section)

    def add_commandlist_update_merged_skeleton(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        commandlist_section = M_IniSection(M_SectionType.CommandList)
        if GlobalProterties.import_merged_vgmap():
            commandlist_section.append("[CommandListUpdateMergedSkeleton]")
            commandlist_section.append("if $state_id")
            commandlist_section.append("  $state_id = 0")
            commandlist_section.append("else")
            commandlist_section.append("  $state_id = 1")
            commandlist_section.append("endif")
            commandlist_section.append("ResourceMergedSkeleton = copy ResourceMergedSkeletonRW")
            commandlist_section.append("ResourceExtraMergedSkeleton = copy ResourceExtraMergedSkeletonRW")
            if draw_ib_model.blend_remap:
                commandlist_section.append("run = CommandListRemapMergedSkeleton")
            commandlist_section.new_line()
        ini_builder.append_section(commandlist_section)

    def add_blend_remap_sections(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        blend_remap_section = M_IniSection(M_SectionType.CommandList)

        if GlobalProterties.import_merged_vgmap():
            blend_remap_section.append("[ResourceMergedSkeletonRemap]")
            blend_remap_section.append("[ResourceExtraMergedSkeletonRemap]")
            blend_remap_section.new_line()

            blend_remap_section.append("[ResourceBlendBufferOverride]")
            blend_remap_section.append("[ResourceExtraMergedSkeletonOverride]")
            blend_remap_section.append("[ResourceMergedSkeletonOverride]")
            blend_remap_section.new_line()

            blend_remap_section.append("[ResourceRemappedBlendBufferRW]")
            blend_remap_section.append("[ResourceRemappedSkeletonRW]")
            blend_remap_section.append("[ResourceExtraRemappedSkeletonRW]")
            blend_remap_section.new_line()

            for component_tmp_obj_name, use_remap in draw_ib_model.blend_remap_used.items():
                if not use_remap:
                    continue
                component_count = int(component_tmp_obj_name.split("-")[1]) - 1
                blend_remap_section.append("[ResourceRemappedBlendBufferComponent" + str(component_count) + "]")
                blend_remap_section.append("[ResourceRemappedSkeletonComponent" + str(component_count) + "]")
                blend_remap_section.append("[ResourceExtraRemappedSkeletonComponent" + str(component_count) + "]")
                blend_remap_section.new_line()

            if draw_ib_model.blend_remap:
                blend_remap_section.append("[CommandListInitializeBlendRemaps]")
                blend_remap_section.append("local $blend_remaps_initialized")
                blend_remap_section.append("if !$blend_remaps_initialized")
                blend_remap_section.append("  ResourceRemappedSkeletonRW = copy ResourceMergedSkeletonRW")
                blend_remap_section.append("  ResourceExtraRemappedSkeletonRW = copy ResourceExtraMergedSkeletonRW")
                blend_remap_section.new_line()
                blend_remap_section.append("  $\\WWMIv1\\custom_vertex_count = $mesh_vertex_count")
                weights_per_vertex_count = draw_ib_model.d3d11GameType.get_blendindices_count_wwmi()
                blend_remap_section.append("  $\\WWMIv1\\weights_per_vertex_count = " + str(weights_per_vertex_count))
                blend_remap_section.append("  cs-t34 = ref ResourceBlendRemapReverseBuffer")
                blend_remap_section.append("  cs-t35 = ref ResourceBlendRemapVertexVGBuffer")

                blend_remap_id = 0
                for component_tmp_obj_name, use_remap in draw_ib_model.blend_remap_used.items():
                    if not use_remap:
                        continue
                    component_count = int(component_tmp_obj_name.split("-")[1]) - 1
                    component_count_str = str(component_count)
                    blend_remap_section.append("    $\\WWMIv1\\blend_remap_id = " + str(blend_remap_id))
                    blend_remap_section.append("    ResourceRemappedBlendBufferRW = copy ResourceBlendBufferNoStride")
                    blend_remap_section.append("    cs-u4 = ref ResourceRemappedBlendBufferRW")
                    blend_remap_section.append("    run = CustomShader\\WWMIv1\\BlendRemapper")
                    blend_remap_section.append("    ResourceRemappedBlendBufferComponent" + component_count_str + " = copy ResourceRemappedBlendBufferRW")
                    blend_remap_section.append("    ResourceRemappedBlendBufferComponent" + component_count_str + " = copy_desc ResourceBlendBuffer")
                    blend_remap_section.new_line()
                    blend_remap_id = blend_remap_id + 1

                blend_remap_section.append("    $blend_remaps_initialized = 1")
                blend_remap_section.append("endif")
                blend_remap_section.new_line()

            blend_remap_section.append("[CommandListRemapMergedSkeleton]")
            blend_remap_section.append("ResourceMergedSkeletonRemap = copy ResourceMergedSkeletonRW")
            blend_remap_section.append("ResourceExtraMergedSkeletonRemap = copy ResourceExtraMergedSkeletonRW")
            blend_remap_section.new_line()
            if draw_ib_model.blend_remap:
                blend_remap_section.append("cs-t37 = ResourceBlendRemapForwardBuffer")
                blend_remap_section.new_line()

                blend_remap_id = 0
                for component_tmp_obj_name, use_remap in draw_ib_model.blend_remap_used.items():
                    if not use_remap:
                        continue

                    blend_remap_section.append("$\\WWMIv1\\blend_remap_id = " + str(blend_remap_id))
                    component_count = int(component_tmp_obj_name.split("-")[1]) - 1
                    vg_count = draw_ib_model.component_real_vg_count_dict[component_count]
                    blend_remap_section.append("$\\WWMIv1\\vg_count = " + str(vg_count))
                    blend_remap_section.append("cs-t38 = ResourceMergedSkeletonRemap")
                    blend_remap_section.append("cs-u5 = ResourceRemappedSkeletonRW")
                    blend_remap_section.append("run = CustomShader\\WWMIv1\\SkeletonRemapper")
                    blend_remap_section.append("ResourceRemappedSkeletonComponent" + str(component_count) + " = copy ResourceRemappedSkeletonRW")
                    blend_remap_section.append("cs-t38 = ResourceExtraMergedSkeletonRemap")
                    blend_remap_section.append("cs-u5 = ResourceExtraRemappedSkeletonRW")
                    blend_remap_section.append("run = CustomShader\\WWMIv1\\SkeletonRemapper")
                    blend_remap_section.append("ResourceExtraRemappedSkeletonComponent" + str(component_count) + " = copy ResourceExtraRemappedSkeletonRW")
                    blend_remap_section.new_line()
                    blend_remap_id = blend_remap_id + 1

        ini_builder.append_section(blend_remap_section)

    def add_commandlist_trigger_shared_cleanup_section(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        commandlist_section = M_IniSection(M_SectionType.CommandList)
        commandlist_section.append("[CommandListTriggerResourceOverrides]")
        commandlist_section.append("CheckTextureOverride = ps-t0")
        commandlist_section.append("CheckTextureOverride = ps-t1")
        commandlist_section.append("CheckTextureOverride = ps-t2")
        commandlist_section.append("CheckTextureOverride = ps-t3")
        commandlist_section.append("CheckTextureOverride = ps-t4")
        commandlist_section.append("CheckTextureOverride = ps-t5")
        commandlist_section.append("CheckTextureOverride = ps-t6")
        commandlist_section.append("CheckTextureOverride = ps-t7")
        if GlobalProterties.import_merged_vgmap():
            commandlist_section.append("CheckTextureOverride = vs-cb3")
            commandlist_section.append("CheckTextureOverride = vs-cb4")
        commandlist_section.new_line()

        commandlist_section.append("[ResourceBypassVB0]")
        commandlist_section.new_line()

        commandlist_section.append("[CommandListOverrideSharedResources]")
        commandlist_section.append("ResourceBypassVB0 = ref vb0")
        commandlist_section.append("ib = ResourceIndexBuffer")
        commandlist_section.append("vb0 = ResourcePositionBuffer")
        commandlist_section.append("vb1 = ResourceVectorBuffer")
        commandlist_section.append("vb2 = ResourceTexcoordBuffer")
        commandlist_section.append("vb3 = ResourceColorBuffer")

        if not draw_ib_model.blend_remap:
            commandlist_section.append("vb4 = ResourceBlendBuffer")

        if GlobalProterties.import_merged_vgmap():
            if draw_ib_model.blend_remap:
                commandlist_section.append("if ResourceBlendBufferOverride === null")
                commandlist_section.append("vb4 = ResourceBlendBuffer")
                commandlist_section.append("if vs-cb3 == 3381.7777")
                commandlist_section.append("  vs-cb3 = ResourceExtraMergedSkeleton")
                commandlist_section.append("endif")
                commandlist_section.append("if vs-cb4 == 3381.7777")
                commandlist_section.append("  vs-cb4 = ResourceMergedSkeleton")
                commandlist_section.append("endif")
                commandlist_section.append("else")
                commandlist_section.append("vb4 = ref ResourceBlendBufferOverride")
                commandlist_section.append("if vs-cb3 == 3381.7777")
                commandlist_section.append("  vs-cb3 = ResourceExtraMergedSkeletonOverride")
                commandlist_section.append("endif")
                commandlist_section.append("if vs-cb4 == 3381.7777")
                commandlist_section.append("  vs-cb4 = ResourceMergedSkeletonOverride")
                commandlist_section.append("  endif")
                commandlist_section.append("endif")
            else:
                commandlist_section.append("if vs-cb3 == 3381.7777")
                commandlist_section.append("  vs-cb3 = ResourceExtraMergedSkeleton")
                commandlist_section.append("endif")
                commandlist_section.append("if vs-cb4 == 3381.7777")
                commandlist_section.append("  vs-cb4 = ResourceMergedSkeleton")
                commandlist_section.append("endif")

        commandlist_section.new_line()
        commandlist_section.append("[CommandListCleanupSharedResources]")
        commandlist_section.append("vb0 = ref ResourceBypassVB0")

        if draw_ib_model.blend_remap:
            commandlist_section.append("if ResourceBlendBufferOverride !== null")
            commandlist_section.append("    ResourceBlendBufferOverride = null")
            commandlist_section.append("    ResourceMergedSkeletonOverride = null")
            commandlist_section.append("    ResourceExtraMergedSkeletonOverride = null")
            commandlist_section.append("endif")

        commandlist_section.new_line()
        ini_builder.append_section(commandlist_section)

    def add_commandlist_merge_skeleton_section(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        commandlist_section = M_IniSection(M_SectionType.CommandList)
        if GlobalProterties.import_merged_vgmap():
            commandlist_section.append("[CommandListMergeSkeleton]")
            commandlist_section.append("$\\WWMIv1\\custom_mesh_scale = 1.00")
            commandlist_section.append("cs-cb8 = ref vs-cb4")
            commandlist_section.append("cs-u6 = ResourceMergedSkeletonRW")
            commandlist_section.append("run = CustomShader\\WWMIv1\\SkeletonMerger")
            commandlist_section.append("cs-cb8 = ref vs-cb3")
            commandlist_section.append("cs-u6 = ResourceExtraMergedSkeletonRW")
            commandlist_section.append("run = CustomShader\\WWMIv1\\SkeletonMerger")
            commandlist_section.new_line()
        ini_builder.append_section(commandlist_section)

    def add_resource_mod_info_section_default(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        resource_mod_info_section = M_IniSection(M_SectionType.ResourceModInfo)
        resource_mod_info_section.append("[ResourceModName]")
        resource_mod_info_section.append("type = Buffer")
        resource_mod_info_section.append("data = \"Unnamed Mod\"")
        resource_mod_info_section.new_line()
        resource_mod_info_section.append("[ResourceModAuthor]")
        resource_mod_info_section.append("type = Buffer")
        resource_mod_info_section.append("data = \"Unknown Author\"")
        resource_mod_info_section.new_line()
        resource_mod_info_section.append("[ResourceModDesc]")
        resource_mod_info_section.append("; type = Buffer")
        resource_mod_info_section.append("; data = \"Empty Mod Description\"")
        resource_mod_info_section.new_line()
        resource_mod_info_section.append("[ResourceModLink]")
        resource_mod_info_section.append("; type = Buffer")
        resource_mod_info_section.append("; data = \"Empty Mod Link\"")
        resource_mod_info_section.new_line()
        resource_mod_info_section.append("[ResourceModLogo]")
        resource_mod_info_section.append("; filename = Textures/Logo.dds")
        resource_mod_info_section.new_line()
        ini_builder.append_section(resource_mod_info_section)

    def add_texture_override_mark_bone_data_cb(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        texture_override_mark_bonedatacb_section = M_IniSection(M_SectionType.TextureOverrideGeneral)
        texture_override_mark_bonedatacb_section.append("[TextureOverrideMarkBoneDataCB]")
        texture_override_mark_bonedatacb_section.append("hash = " + draw_ib_model.extracted_object.cb4_hash)
        texture_override_mark_bonedatacb_section.append("match_priority = 0")
        texture_override_mark_bonedatacb_section.append("filter_index = 3381.7777")
        texture_override_mark_bonedatacb_section.new_line()
        ini_builder.append_section(texture_override_mark_bonedatacb_section)

    def add_texture_override_component(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        texture_override_component = M_IniSection(M_SectionType.TextureOverrideIB)
        component_count = 0

        for component_tmp_obj_name, component_blend_remap_used in draw_ib_model.blend_remap_used.items():
            component_name = "Component " + str(component_count + 1)
            component_count_str = str(component_count)
            component_object = draw_ib_model.extracted_object.components[component_count]

            texture_override_component.append("[TextureOverrideComponent" + component_count_str + "]")
            texture_override_component.append("hash = " + draw_ib_model.extracted_object.vb0_hash)
            texture_override_component.append("match_first_index = " + str(component_object.index_offset))
            texture_override_component.append("match_index_count = " + str(component_object.index_count))
            texture_override_component.append("$object_detected = 1")

            if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                texture_override_component.append("$active" + str(GlobalKeyCountHelper.generated_mod_number) + " = 1")
                if GlobalProterties.generate_branch_mod_gui():
                    texture_override_component.append("$ActiveCharacter = 1")

            texture_override_component.append("if $mod_enabled")

            if GlobalProterties.import_merged_vgmap():
                state_id_var_str = "$state_id_" + component_count_str
                texture_override_component.append("  local " + state_id_var_str)
                texture_override_component.append("  if " + state_id_var_str + " != $state_id")
                texture_override_component.append("    " + state_id_var_str + " = $state_id")
                texture_override_component.append("    $\\WWMIv1\\vg_offset = " + str(component_object.vg_offset))
                texture_override_component.append("    $\\WWMIv1\\vg_count = " + str(component_object.vg_count))
                texture_override_component.append("    run = CommandListMergeSkeleton")
                texture_override_component.append("  endif")
                texture_override_component.append("  if ResourceMergedSkeleton !== null")
                texture_override_component.append("    handling = skip")

                component_model = draw_ib_model.component_name_component_model_dict[component_name]
                drawindexed_str_list = M_IniHelper.get_drawindexed_str_list(component_model.final_ordered_draw_obj_model_list)

                if len(drawindexed_str_list) != 0:
                    if component_blend_remap_used:
                        texture_override_component.append("    ResourceBlendBufferOverride = ref ResourceRemappedBlendBufferComponent" + str(component_count))
                        texture_override_component.append("    ResourceMergedSkeletonOverride = ref ResourceRemappedSkeletonComponent" + str(component_count))
                        texture_override_component.append("    ResourceExtraMergedSkeletonOverride = ref ResourceExtraRemappedSkeletonComponent" + str(component_count))

                    texture_override_component.append("    run = CommandListTriggerResourceOverrides")
                    texture_override_component.append("    run = CommandListOverrideSharedResources")
                    texture_override_component.append("    ; Draw Component " + component_count_str)
                    for drawindexed_str in drawindexed_str_list:
                        texture_override_component.append(drawindexed_str)
                    texture_override_component.append("    run = CommandListCleanupSharedResources")
                texture_override_component.append("  endif")
            else:
                component_model = draw_ib_model.component_name_component_model_dict[component_name]
                drawindexed_str_list = M_IniHelper.get_drawindexed_str_list(component_model.final_ordered_draw_obj_model_list)
                if len(drawindexed_str_list) != 0:
                    texture_override_component.append("  handling = skip")
                    texture_override_component.append("  run = CommandListTriggerResourceOverrides")
                    texture_override_component.append("  run = CommandListOverrideSharedResources")
                    texture_override_component.append("  ; Draw Component " + component_count_str)
                    for drawindexed_str in drawindexed_str_list:
                        texture_override_component.append(drawindexed_str)
                    texture_override_component.append("  run = CommandListCleanupSharedResources")

            texture_override_component.append("endif")
            texture_override_component.new_line()
            component_count = component_count + 1

        ini_builder.append_section(texture_override_component)

    def add_texture_override_shapekeys(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        texture_override_shapekeys_section = M_IniSection(M_SectionType.TextureOverrideShapeKeys)

        shapekey_offsets_hash = draw_ib_model.extracted_object.shapekeys.offsets_hash
        if shapekey_offsets_hash != "":
            texture_override_shapekeys_section.append("[TextureOverrideShapeKeyOffsets]")
            texture_override_shapekeys_section.append("hash = " + shapekey_offsets_hash)
            texture_override_shapekeys_section.append("match_priority = 0")
            texture_override_shapekeys_section.append("override_byte_stride = 24")
            texture_override_shapekeys_section.append("override_vertex_count = $mesh_vertex_count")
            texture_override_shapekeys_section.new_line()

        shapekey_scale_hash = draw_ib_model.extracted_object.shapekeys.scale_hash
        if shapekey_scale_hash != "":
            texture_override_shapekeys_section.append("[TextureOverrideShapeKeyScale]")
            texture_override_shapekeys_section.append("hash = " + draw_ib_model.extracted_object.shapekeys.scale_hash)
            texture_override_shapekeys_section.append("match_priority = 0")
            texture_override_shapekeys_section.append("override_byte_stride = 4")
            texture_override_shapekeys_section.append("override_vertex_count = $mesh_vertex_count")
            texture_override_shapekeys_section.new_line()

        texture_override_shapekeys_section.append("[CommandListSetupShapeKeys]")
        texture_override_shapekeys_section.append("$\\WWMIv1\\shapekey_checksum = " + str(draw_ib_model.extracted_object.shapekeys.checksum))
        texture_override_shapekeys_section.append("cs-t33 = ResourceShapeKeyOffsetBuffer")
        texture_override_shapekeys_section.append("cs-u5 = ResourceCustomShapeKeyValuesRW")
        texture_override_shapekeys_section.append("cs-u6 = ResourceShapeKeyCBRW")
        texture_override_shapekeys_section.append("run = CustomShader\\WWMIv1\\ShapeKeyOverrider")
        texture_override_shapekeys_section.new_line()

        texture_override_shapekeys_section.append("[CommandListLoadShapeKeys]")
        texture_override_shapekeys_section.append("$\\WWMIv1\\shapekey_vertex_count = $shapekey_vertex_count")
        texture_override_shapekeys_section.append("cs-t0 = ResourceShapeKeyVertexIdBuffer")
        texture_override_shapekeys_section.append("cs-t1 = ResourceShapeKeyVertexOffsetBuffer")
        texture_override_shapekeys_section.append("cs-u6 = ResourceShapeKeyCBRW")
        texture_override_shapekeys_section.append("run = CustomShader\\WWMIv1\\ShapeKeyLoader")
        texture_override_shapekeys_section.new_line()

        if shapekey_offsets_hash != "":
            texture_override_shapekeys_section.append("[TextureOverrideShapeKeyLoaderCallback]")
            texture_override_shapekeys_section.append("hash = " + draw_ib_model.extracted_object.shapekeys.offsets_hash)
            texture_override_shapekeys_section.append("match_priority = 0")
            texture_override_shapekeys_section.append("if $mod_enabled")
            if GlobalProterties.import_merged_vgmap():
                texture_override_shapekeys_section.append("  if cs == 3381.3333 && ResourceMergedSkeleton !== null")
            else:
                texture_override_shapekeys_section.append("  if cs == 3381.3333")
            texture_override_shapekeys_section.append("    handling = skip")
            texture_override_shapekeys_section.append("    run = CommandListSetupShapeKeys")
            texture_override_shapekeys_section.append("    run = CommandListLoadShapeKeys")
            texture_override_shapekeys_section.append("  endif")
            texture_override_shapekeys_section.append("endif")
            texture_override_shapekeys_section.new_line()

        texture_override_shapekeys_section.append("[CommandListMultiplyShapeKeys]")
        texture_override_shapekeys_section.append("$\\WWMIv1\\custom_vertex_count = $mesh_vertex_count")
        texture_override_shapekeys_section.append("run = CustomShader\\WWMIv1\\ShapeKeyMultiplier")
        texture_override_shapekeys_section.new_line()

        if shapekey_offsets_hash != "":
            texture_override_shapekeys_section.append("[TextureOverrideShapeKeyMultiplierCallback]")
            texture_override_shapekeys_section.append("hash = " + draw_ib_model.extracted_object.shapekeys.offsets_hash)
            texture_override_shapekeys_section.append("match_priority = 0")
            texture_override_shapekeys_section.append("if $mod_enabled")
            if GlobalProterties.import_merged_vgmap():
                texture_override_shapekeys_section.append("  if cs == 3381.4444 && ResourceMergedSkeleton !== null")
            else:
                texture_override_shapekeys_section.append("  if cs == 3381.4444")
            texture_override_shapekeys_section.append("    handling = skip")
            texture_override_shapekeys_section.append("    run = CommandListMultiplyShapeKeys")
            texture_override_shapekeys_section.append("  endif")
            texture_override_shapekeys_section.append("endif")
            texture_override_shapekeys_section.new_line()

        ini_builder.append_section(texture_override_shapekeys_section)

    def add_resource_shapekeys(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        resource_shapekeys_section = M_IniSection(M_SectionType.ResourceShapeKeysOverride)
        resource_shapekeys_section.append("; Resources: Shape Keys Override -------------------------")
        resource_shapekeys_section.append("[ResourceShapeKeyCBRW]")
        resource_shapekeys_section.append("type = RWBuffer")
        resource_shapekeys_section.append("format = R32G32B32A32_UINT")
        resource_shapekeys_section.append("array = 66")
        resource_shapekeys_section.append("[ResourceCustomShapeKeyValuesRW]")
        resource_shapekeys_section.append("type = RWBuffer")
        resource_shapekeys_section.append("format = R32G32B32A32_FLOAT")
        resource_shapekeys_section.append("array = 32")
        ini_builder.append_section(resource_shapekeys_section)

    def add_resource_merged_skeleton(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        resource_skeleton_section = M_IniSection(M_SectionType.ResourceSkeletonOverride)
        resource_skeleton_section.append("[ResourceMergedSkeleton]")
        resource_skeleton_section.new_line()
        resource_skeleton_section.append("[ResourceMergedSkeletonRW]")
        resource_skeleton_section.append("type = RWBuffer")
        resource_skeleton_section.append("format = R32G32B32A32_FLOAT")
        resource_skeleton_section.append("array = 1536" if draw_ib_model.blend_remap else "array = 768")
        resource_skeleton_section.new_line()
        resource_skeleton_section.append("[ResourceExtraMergedSkeleton]")
        resource_skeleton_section.new_line()
        resource_skeleton_section.append("[ResourceExtraMergedSkeletonRW]")
        resource_skeleton_section.append("type = RWBuffer")
        resource_skeleton_section.append("format = R32G32B32A32_FLOAT")
        resource_skeleton_section.append("array = 1536" if draw_ib_model.blend_remap else "array = 768")
        ini_builder.append_section(resource_skeleton_section)

    def add_resource_buffer(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        resource_buffer_section = M_IniSection(M_SectionType.ResourceBuffer)
        buffer_folder_name = "Meshes"

        resource_buffer_section.append("[ResourceIndexBuffer]")
        resource_buffer_section.append("type = Buffer")
        resource_buffer_section.append("format = DXGI_FORMAT_R32_UINT")
        resource_buffer_section.append("stride = 12")
        resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-Component1.buf")
        resource_buffer_section.new_line()

        for category_name, category_stride in draw_ib_model.d3d11GameType.CategoryStrideDict.items():
            resource_buffer_section.append("[Resource" + category_name + "Buffer]")
            resource_buffer_section.append("type = Buffer")
            if category_name == "Position":
                resource_buffer_section.append("format = DXGI_FORMAT_R32G32B32_FLOAT")
            elif category_name == "Blend":
                resource_buffer_section.append("format = DXGI_FORMAT_R8_UINT")
            elif category_name == "Vector":
                resource_buffer_section.append("format = DXGI_FORMAT_R8G8B8A8_SNORM")
            elif category_name == "Color":
                resource_buffer_section.append("format = DXGI_FORMAT_R8G8B8A8_UNORM")
            elif category_name == "Texcoord":
                resource_buffer_section.append("format = DXGI_FORMAT_R16G16_FLOAT")
            resource_buffer_section.append("stride = " + str(category_stride))
            resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-" + category_name + ".buf")
            resource_buffer_section.new_line()

            if category_name == "Blend" and draw_ib_model.blend_remap:
                resource_buffer_section.append("[ResourceBlendBufferNoStride]")
                resource_buffer_section.append("type = Buffer")
                resource_buffer_section.append("format = DXGI_FORMAT_R8_UINT")
                resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-" + category_name + ".buf")
                resource_buffer_section.new_line()

        if draw_ib_model.blend_remap:
            resource_buffer_section.append("[ResourceBlendRemapVertexVGBuffer]")
            resource_buffer_section.append("type = Buffer")
            resource_buffer_section.append("format = DXGI_FORMAT_R16_UINT")
            resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-BlendRemapVertexVG.buf")
            resource_buffer_section.new_line()

            resource_buffer_section.append("[ResourceBlendRemapForwardBuffer]")
            resource_buffer_section.append("type = Buffer")
            resource_buffer_section.append("format = DXGI_FORMAT_R16_UINT")
            resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-BlendRemapForward.buf")
            resource_buffer_section.new_line()

            resource_buffer_section.append("[ResourceBlendRemapReverseBuffer]")
            resource_buffer_section.append("type = Buffer")
            resource_buffer_section.append("format = DXGI_FORMAT_R16_UINT")
            resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-BlendRemapReverse.buf")
            resource_buffer_section.new_line()

        resource_buffer_section.append("[ResourceShapeKeyOffsetBuffer]")
        resource_buffer_section.append("type = Buffer")
        resource_buffer_section.append("format = DXGI_FORMAT_R32G32B32A32_UINT")
        resource_buffer_section.append("stride = 16")
        resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-ShapeKeyOffset.buf")
        resource_buffer_section.new_line()

        resource_buffer_section.append("[ResourceShapeKeyVertexIdBuffer]")
        resource_buffer_section.append("type = Buffer")
        resource_buffer_section.append("format = DXGI_FORMAT_R32_UINT")
        resource_buffer_section.append("stride = 4")
        resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-ShapeKeyVertexId.buf")
        resource_buffer_section.new_line()

        resource_buffer_section.append("[ResourceShapeKeyVertexOffsetBuffer]")
        resource_buffer_section.append("type = Buffer")
        resource_buffer_section.append("format = DXGI_FORMAT_R16_FLOAT")
        resource_buffer_section.append("stride = 2")
        resource_buffer_section.append("filename = " + buffer_folder_name + "/" + draw_ib_model.draw_ib + "-ShapeKeyVertexOffset.buf")
        resource_buffer_section.new_line()

        ini_builder.append_section(resource_buffer_section)

    def generate_unreal_vs_config_ini(self):
        config_ini_builder = M_IniBuilder()

        for draw_ib, draw_ib_model in self.drawib_drawibmodel_dict.items():
            self.add_constants_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_present_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_register_mod_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_update_merged_skeleton(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_blend_remap_sections(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_resource_mod_info_section_default(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_texture_override_mark_bone_data_cb(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_merge_skeleton_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_trigger_shared_cleanup_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_texture_override_component(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_texture_override_shapekeys(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_resource_shapekeys(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)

            if GlobalProterties.import_merged_vgmap():
                self.add_resource_merged_skeleton(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)

            self.add_resource_buffer(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            M_IniHelper.move_slot_style_textures(draw_ib_model=draw_ib_model)
            GlobalKeyCountHelper.generated_mod_number = GlobalKeyCountHelper.generated_mod_number + 1
            M_IniHelper.add_branch_key_sections(ini_builder=config_ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
            M_IniHelperGUI.add_branch_mod_gui_section(ini_builder=config_ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
            M_IniHelper.generate_hash_style_texture_ini(ini_builder=config_ini_builder, drawib_drawibmodel_dict=self.drawib_drawibmodel_dict)
            config_ini_builder.save_to_file_not_reorder(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.workspacename + "_" + draw_ib + ".ini"))
            config_ini_builder.clear()

    def export(self):
        for draw_ib_model in self.drawib_drawibmodel_dict.values():
            draw_ib_model.write_buffer_files()
        self.generate_unreal_vs_config_ini()


ModModelWWMI = ExportWWMI