import os

from ...common.global_properties import GlobalProterties
from ...common.global_config import GlobalConfig
from .drawib_model_wwmi import DrawIBModelWWMI
from ...blueprint.model import BluePrintModel
from ...blueprint.export_helper import BlueprintExportHelper
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
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


def _get_component_index(draw_ib_model: DrawIBModelWWMI, component_tmp_obj_name: str, fallback_index: int) -> int:
    component_index = getattr(draw_ib_model, "component_object_index_dict", {}).get(component_tmp_obj_name)
    if component_index is not None:
        return int(component_index)
    return int(fallback_index)


def _build_swap_keys_by_draw_ib(blueprint_model: BluePrintModel) -> dict[str, set[str]]:
    """按 draw_ib 聚合当前处理链中使用到的物体切换节点 key（格式：tree::node_name）。

    WWMI 每个 draw_ib 单独输出一个 INI，因此只把当前 draw_ib 实际用到的
    物体切换节点写进对应的 INI，避免在无关 INI 中生成 [KeySwap_*] / 变量声明。
    """
    aggregated: dict[str, set[str]] = {}
    for chain in getattr(blueprint_model, "processing_chains", []) or []:
        swap_node_keys = getattr(chain, "swap_node_option_values", None)
        if not swap_node_keys:
            continue
        try:
            draw_call_model = chain.to_draw_call_model()
        except Exception:
            continue
        target_draw_ib = str(getattr(draw_call_model, "match_draw_ib", "") or "")
        if not target_draw_ib:
            continue
        aggregated.setdefault(target_draw_ib, set()).update(swap_node_keys.keys())
    return aggregated


def _collect_swap_nodes_for_draw_ib(
    blueprint_model: BluePrintModel,
    draw_ib: str,
    swap_keys_by_draw_ib: dict[str, set[str]],
):
    """根据预聚合的 swap_keys_by_draw_ib 过滤出当前 draw_ib 用到的物体切换节点。"""
    registry = getattr(blueprint_model, "_swap_key_registry", None)
    if registry is None:
        return None, []
    used_keys = swap_keys_by_draw_ib.get(draw_ib, set())
    if not used_keys:
        return registry, []
    filtered = [
        node
        for node in getattr(registry, "swapkey_nodes", [])
        if f"{node.id_data.name}::{node.name}" in used_keys
    ]
    return registry, filtered


def _iter_blend_remap_components(draw_ib_model: DrawIBModelWWMI):
    for fallback_index, (component_tmp_obj_name, use_remap) in enumerate(draw_ib_model.blend_remap_used.items()):
        component_count = _get_component_index(draw_ib_model, component_tmp_obj_name, fallback_index)
        yield component_tmp_obj_name, use_remap, component_count


def _get_component_model(draw_ib_model: DrawIBModelWWMI, component_index: int):
    for component_model in getattr(draw_ib_model, "component_model_list", []):
        if int(getattr(component_model, "component_index", -1)) == int(component_index):
            return component_model
    raise ValueError(
        "WWMI component model mapping missing: "
        f"component_index={component_index}, "
        f"known_component_indices={[getattr(model, 'component_index', None) for model in getattr(draw_ib_model, 'component_model_list', [])]}"
    )


def _get_component_vg_count(draw_ib_model: DrawIBModelWWMI, component_tmp_obj_name: str, component_count: int) -> int:
    vg_count = getattr(draw_ib_model, "component_real_vg_count_dict", {}).get(component_count)
    if vg_count is None:
        raise ValueError(
            "WWMI BlendRemap 组件映射缺失: "
            f"component='{component_tmp_obj_name}', "
            f"component_index={component_count}, "
            f"known_components={getattr(draw_ib_model, 'component_object_index_dict', {})}, "
            f"known_vg_counts={getattr(draw_ib_model, 'component_real_vg_count_dict', {})}"
        )
    return int(vg_count)


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
        constants_section.SectionName = "Constants"
        constants_section.append("global $required_wwmi_version = 0.91")
        constants_section.append("global $object_guid = " + str(draw_ib_model.extracted_object.index_count))
        constants_section.append("global $mesh_vertex_count = " + str(draw_ib_model.mesh_vertex_count))
        constants_section.append("global $shapekey_vertex_count = " + str(len(draw_ib_model.obj_buffer_model_wwmi.shapekey_vertex_ids)))
        for batch_id, batch in enumerate(self.get_wwmi_shapekey_batches(draw_ib_model)):
            constants_section.append("global $shapekey_vertex_offset_batch" + str(batch_id) + " = " + str(batch["custom_vertex_offset"]))
            constants_section.append("global $shapekey_vertex_count_batch" + str(batch_id) + " = " + str(batch["custom_vertex_count"]))
        constants_section.append("global $mod_id = -1000")

        if GlobalProterties.import_merged_vgmap():
            constants_section.append("global $state_id = 0")

        constants_section.append("global $mod_enabled = 0")
        constants_section.append("global $object_detected = 0")
        constants_section.new_line()
        ini_builder.append_section(constants_section)

    @staticmethod
    def get_wwmi_shapekey_batches(draw_ib_model: DrawIBModelWWMI) -> list[dict]:
        shapekey_offsets = draw_ib_model.obj_buffer_model_wwmi.shapekey_offsets
        if not shapekey_offsets:
            return []

        batch_count = len(shapekey_offsets) // 128
        metadata_batches = list(getattr(draw_ib_model.extracted_object.shapekeys, "batches", []) or [])
        if not metadata_batches and getattr(draw_ib_model.extracted_object.shapekeys, "checksum", 0):
            metadata_batches = [{
                "vertex_offset": 0,
                "dispatch_y": draw_ib_model.extracted_object.shapekeys.dispatch_y,
                "checksum": draw_ib_model.extracted_object.shapekeys.checksum,
            }]
        if len(metadata_batches) < batch_count:
            return []

        batches = []
        custom_vertex_offset = 0
        for batch_id in range(batch_count):
            batch_offsets = shapekey_offsets[batch_id * 128:(batch_id + 1) * 128]
            custom_vertex_count = batch_offsets[-1] if batch_offsets else 0
            metadata = metadata_batches[batch_id]
            if int(metadata.get("checksum", 0)) == 0:
                return []
            batches.append({
                "checksum": int(metadata.get("checksum", 0)),
                "original_vertex_offset": int(metadata.get("vertex_offset", 0)),
                "dispatch_y": int(metadata.get("dispatch_y", 0)),
                "custom_vertex_offset": custom_vertex_offset,
                "custom_vertex_count": custom_vertex_count,
            })
            custom_vertex_offset += custom_vertex_count
        return batches

    def add_present_section(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        present_section = M_IniSection(M_SectionType.Present)
        present_section.SectionName = "Present"
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

            for component_tmp_obj_name, use_remap, component_count in _iter_blend_remap_components(draw_ib_model):
                if not use_remap:
                    continue
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
                for component_tmp_obj_name, use_remap, component_count in _iter_blend_remap_components(draw_ib_model):
                    if not use_remap:
                        continue
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
                for component_tmp_obj_name, use_remap, component_count in _iter_blend_remap_components(draw_ib_model):
                    if not use_remap:
                        continue

                    blend_remap_section.append("$\\WWMIv1\\blend_remap_id = " + str(blend_remap_id))
                    vg_count = _get_component_vg_count(draw_ib_model, component_tmp_obj_name, component_count)
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
                commandlist_section.append("if vs-cb4 == 3381.7777")
                commandlist_section.append("  vs-cb4 = ref ResourceMergedSkeleton")
                commandlist_section.append("  if vs-cb3 == 3381.7777")
                commandlist_section.append("    vs-cb3 = ref ResourceExtraMergedSkeleton")
                commandlist_section.append("  endif")
                commandlist_section.append("else if vs-cb3 == 3381.7777")
                commandlist_section.append("  vs-cb3 = ref ResourceMergedSkeleton")
                commandlist_section.append("endif")
                commandlist_section.append("else")
                commandlist_section.append("vb4 = ref ResourceBlendBufferOverride")
                commandlist_section.append("if vs-cb4 == 3381.7777")
                commandlist_section.append("  vs-cb4 = ref ResourceMergedSkeletonOverride")
                commandlist_section.append("  if vs-cb3 == 3381.7777")
                commandlist_section.append("    vs-cb3 = ref ResourceExtraMergedSkeletonOverride")
                commandlist_section.append("  endif")
                commandlist_section.append("else if vs-cb3 == 3381.7777")
                commandlist_section.append("  vs-cb3 = ref ResourceMergedSkeletonOverride")
                commandlist_section.append("endif")
                commandlist_section.append("endif")
            else:
                commandlist_section.append("if vs-cb4 == 3381.7777")
                commandlist_section.append("  vs-cb4 = ref ResourceMergedSkeleton")
                commandlist_section.append("  if vs-cb3 == 3381.7777")
                commandlist_section.append("    vs-cb3 = ref ResourceExtraMergedSkeleton")
                commandlist_section.append("  endif")
                commandlist_section.append("else if vs-cb3 == 3381.7777")
                commandlist_section.append("  vs-cb3 = ref ResourceMergedSkeleton")
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
        for fallback_index, (component_tmp_obj_name, component_blend_remap_used) in enumerate(draw_ib_model.blend_remap_used.items()):
            component_index = _get_component_index(draw_ib_model, component_tmp_obj_name, fallback_index)
            component_count_str = str(component_index)
            component_object = draw_ib_model.extracted_object.components[component_index]

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

                component_model = _get_component_model(draw_ib_model, component_index)
                drawindexed_str_list = M_IniHelper.get_drawindexed_str_list(component_model.final_ordered_draw_obj_model_list)

                if len(drawindexed_str_list) != 0:
                    if component_blend_remap_used:
                        texture_override_component.append("    ResourceBlendBufferOverride = ref ResourceRemappedBlendBufferComponent" + component_count_str)
                        texture_override_component.append("    ResourceMergedSkeletonOverride = ref ResourceRemappedSkeletonComponent" + component_count_str)
                        texture_override_component.append("    ResourceExtraMergedSkeletonOverride = ref ResourceExtraRemappedSkeletonComponent" + component_count_str)

                    texture_override_component.append("    run = CommandListTriggerResourceOverrides")
                    texture_override_component.append("    run = CommandListOverrideSharedResources")
                    texture_override_component.append("    ; Draw Component " + component_count_str)
                    for drawindexed_str in drawindexed_str_list:
                        texture_override_component.append(drawindexed_str)
                    texture_override_component.append("    run = CommandListCleanupSharedResources")
                texture_override_component.append("  endif")
            else:
                component_model = _get_component_model(draw_ib_model, component_index)
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

        ini_builder.append_section(texture_override_component)

    def add_texture_override_shapekeys(self, ini_builder: M_IniBuilder, draw_ib_model: DrawIBModelWWMI):
        # 直出基础轮次不写这部分，避免和后续直出生成的 ShapeKey 资源段重复。
        if BlueprintExportHelper.should_suppress_shapekey_resource_export():
            return

        shapekey_batches = self.get_wwmi_shapekey_batches(draw_ib_model)
        if not shapekey_batches:
            return

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

        texture_override_shapekeys_section.append("[CommandListSetupShapeKeysBatch]")
        for batch_id, batch in enumerate(shapekey_batches):
            texture_override_shapekeys_section.append("$\\WWMIv1\\shapekey_checksum_batch" + str(batch_id) + " = " + str(batch["checksum"]))
            texture_override_shapekeys_section.append("$\\WWMIv1\\shapekey_vertex_offset_original_batch" + str(batch_id) + " = " + str(batch["original_vertex_offset"]))
            texture_override_shapekeys_section.append("$\\WWMIv1\\shapekey_vertex_offset_custom_batch" + str(batch_id) + " = $shapekey_vertex_offset_batch" + str(batch_id))
        texture_override_shapekeys_section.append("cs-t33 = ResourceShapeKeyOffsetBuffer")
        texture_override_shapekeys_section.append("cs-u5 = ResourceCustomShapeKeyValuesRW")
        texture_override_shapekeys_section.append("cs-u6 = ResourceShapeKeyCBRW")
        texture_override_shapekeys_section.append("run = CustomShader\\WWMIv1\\ShapeKeyBatchOverrider")
        texture_override_shapekeys_section.new_line()

        texture_override_shapekeys_section.append("[CommandListLoadShapeKeysBatch]")
        for batch_id, batch in enumerate(shapekey_batches):
            texture_override_shapekeys_section.append("$\\WWMIv1\\shapekey_dispatch_size_y_original_batch" + str(batch_id) + " = " + str(batch["dispatch_y"]))
            texture_override_shapekeys_section.append("$\\WWMIv1\\shapekey_vertex_count_batch" + str(batch_id) + " = $shapekey_vertex_count_batch" + str(batch_id))
        texture_override_shapekeys_section.append("cs-t0 = ResourceShapeKeyVertexIdBuffer")
        texture_override_shapekeys_section.append("cs-t1 = ResourceShapeKeyVertexOffsetBuffer")
        texture_override_shapekeys_section.append("cs-u6 = ResourceShapeKeyCBRW")
        texture_override_shapekeys_section.append("run = CommandList\\WWMIv1\\LoadShapeKeysBatch")
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
            texture_override_shapekeys_section.append("    run = CommandListSetupShapeKeysBatch")
            texture_override_shapekeys_section.append("    run = CommandListLoadShapeKeysBatch")
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
        # ShapeKey 资源段和二进制缓冲都由同一个抑制开关控制，防止基础轮次输出半套资源。
        if BlueprintExportHelper.should_suppress_shapekey_resource_export():
            return

        shapekey_batches = self.get_wwmi_shapekey_batches(draw_ib_model)
        if not shapekey_batches:
            return

        resource_shapekeys_section = M_IniSection(M_SectionType.ResourceShapeKeysOverride)
        resource_shapekeys_section.append("; Resources: Shape Keys Override -------------------------")
        resource_shapekeys_section.append("[ResourceShapeKeyCBRW]")
        resource_shapekeys_section.append("type = RWBuffer")
        resource_shapekeys_section.append("format = R32G32B32A32_UINT")
        resource_shapekeys_section.append("array = 66")
        resource_shapekeys_section.append("[ResourceCustomShapeKeyValuesRW]")
        resource_shapekeys_section.append("type = RWBuffer")
        resource_shapekeys_section.append("format = R32G32B32A32_FLOAT")
        resource_shapekeys_section.append("array = " + str(32 * len(shapekey_batches)))
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
        buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()

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

        if not BlueprintExportHelper.should_suppress_shapekey_resource_export():
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

        # 预聚合每个 draw_ib 实际用到的物体切换节点 key，避免在无关 INI 中注入 [KeySwap_*]
        swap_keys_by_draw_ib = _build_swap_keys_by_draw_ib(self.blueprint_model)

        for draw_ib, draw_ib_model in self.drawib_drawibmodel_dict.items():
            self.add_constants_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_present_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_register_mod_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_update_merged_skeleton(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            # 下面这些段只属于当前 draw_ib（WWMI 一个 draw_ib 一个 INI），包进登记上下文；
            # Constants / Present / 共享 CommandList / 共享骨架 / ModInfo / Key / GUI 等全局段留在上下文外。
            self.add_blend_remap_sections(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_resource_mod_info_section_default(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            with _section_registry_context(draw_ib):
                self.add_texture_override_mark_bone_data_cb(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_merge_skeleton_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            self.add_commandlist_trigger_shared_cleanup_section(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            with _section_registry_context(draw_ib):
                self.add_texture_override_component(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
                self.add_texture_override_shapekeys(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
                self.add_resource_shapekeys(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)

            if GlobalProterties.import_merged_vgmap():
                # 共享骨架资源段：属于全局共享骨架，不登记到单个 draw_ib。
                self.add_resource_merged_skeleton(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)

            with _section_registry_context(draw_ib):
                self.add_resource_buffer(ini_builder=config_ini_builder, draw_ib_model=draw_ib_model)
            M_IniHelper.move_slot_style_textures(draw_ib_model=draw_ib_model)
            GlobalKeyCountHelper.generated_mod_number = GlobalKeyCountHelper.generated_mod_number + 1
            M_IniHelper.add_branch_key_sections(ini_builder=config_ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
            M_IniHelperGUI.add_branch_mod_gui_section(ini_builder=config_ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
            # 集成当前 draw_ib 涉及到的物体切换节点配置（[KeySwap_*] / [Constants] 中 swap 变量声明等）
            self._integrate_object_swap_for_draw_ib(
                config_ini_builder,
                draw_ib,
                swap_keys_by_draw_ib,
            )
            M_IniHelper.generate_hash_style_texture_ini(ini_builder=config_ini_builder, drawib_drawibmodel_dict=self.drawib_drawibmodel_dict)
            M_IniHelper.generate_shared_slot_style_texture_ini(ini_builder=config_ini_builder, drawib_drawibmodel_dict=self.drawib_drawibmodel_dict)
            config_ini_builder.save_to_file_not_reorder(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + "_" + draw_ib + ".ini"))
            config_ini_builder.clear()

    def _integrate_object_swap_for_draw_ib(
        self,
        ini_builder: M_IniBuilder,
        draw_ib: str,
        swap_keys_by_draw_ib: dict[str, set[str]],
    ):
        """将当前 draw_ib 用到的物体切换节点配置追加到对应 INI 中。

        WWMI 不会在 [TextureOverride*] 中自动设置 `$active0 = 1`，因此 [KeySwap_*]
        的 `condition = $active0 == 1` 仍依赖 add_branch_key_sections 中声明的
        `$active0` 全局变量与外部激活手段；本方法只补齐 [KeySwap_*] / [Constants] 中
        swap 变量声明，避免该节点在 WWMI 项目中完全静默失效。
        """
        try:
            from ...blueprint.export_helper import BlueprintExportHelper
            from ...blueprint.node_swap_ini import SwapKeyINIIntegrator
        except ImportError:
            return

        registry, filtered_swap_nodes = _collect_swap_nodes_for_draw_ib(
            self.blueprint_model,
            draw_ib,
            swap_keys_by_draw_ib,
        )
        if registry is None or not filtered_swap_nodes:
            return

        blueprint_tree = getattr(self.blueprint_model, "_tree", None) or BlueprintExportHelper.get_current_blueprint_tree()
        if blueprint_tree is None:
            return

        SwapKeyINIIntegrator.integrate_to_export(
            ini_builder,
            blueprint_tree,
            registry=registry,
            swap_nodes=filtered_swap_nodes,
        )

    def export(self):
        TimerUtils.start_stage("缓冲文件生成")
        for draw_ib_model in self.drawib_drawibmodel_dict.values():
            draw_ib_model.write_buffer_files()
        TimerUtils.end_stage("缓冲文件生成")

        TimerUtils.start_stage("INI配置生成")
        self.generate_unreal_vs_config_ini()
        TimerUtils.end_stage("INI配置生成")

    def export_buffers_only(self):
        """只导出 Buffer 文件，不生成 INI 配置"""
        TimerUtils.start_stage("缓冲文件生成")
        for draw_ib_model in self.drawib_drawibmodel_dict.values():
            draw_ib_model.write_buffer_files()
        TimerUtils.end_stage("缓冲文件生成")


ModModelWWMI = ExportWWMI
