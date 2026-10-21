"""ZZMI DX12 模式导出器

绝区零 DX12 模式与 D3D11 模式的关键差异：
- Blend 类别的 VB 段使用 ``match_cs`` + ``match_uav_bytes`` 进行匹配，而不依赖 VertexLimitRaise。
- 不需要 ``add_unity_vs_texture_override_vlr_section``。
- $active 编号需要使用全局已生成 Mod 数（与上游 ``GlobalConfig.generated_mod_number`` 等价的本地 ``GlobalKeyCountHelper``）。

为避免与现有 D3D11 实现 (``ExportZZMI``) 互相干扰，本类不继承 ``ExportZZMI``，
而是继承 ``ExportUnity`` 并完全重写 VB / IB 生成逻辑，保留 D3D11 模式不变。
"""

import os

from ...common.global_config import GlobalConfig
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.global_properties import GlobalProterties
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...utils.timer_utils import TimerUtils
from .unity import ExportUnity
from contextlib import nullcontext

# 「条件判断」后处理节点需要按 draw_ib 反查 ini 段：导出期用 draw_ib 上下文包住
# 「构建该 draw_ib 的段」的代码，M_IniSection 构造时快照当前 draw_ib。
# 导入失败时导出照常，只是不登记段。
try:
    from ...common import export_section_registry as _section_registry
except Exception:  # pragma: no cover - 轻量 fake 宿主 / 旧插件环境
    _section_registry = None


def _section_registry_context(draw_ib):
    """返回 draw_ib 的段登记上下文；登记表不可用时退化为空上下文（不改变导出行为）。"""
    if _section_registry is None:
        return nullcontext()
    return _section_registry.draw_ib_context(draw_ib)


class ZZMIDX12TextureMarkName:
    """ZZMI DX12 模式下材质槽位标记常量。"""
    DiffuseMap = "DiffuseMap"
    NormalMap = "NormalMap"
    LightMap = "LightMap"
    MaterialMap = "MaterialMap"
    WengineFx = "WengineFx"
    WengineFX = "WengineFX"
    ZglowMap = "ZglowMap"


class ExportZZMIDX12(ExportUnity):
    """绝区零 DX12 模式导出器。

    与 ``ExportZZMI`` 不同，DX12 模式不依赖跨 IB 的复杂处理（暂不支持），
    专注于 ``match_cs`` / ``match_uav_bytes`` 匹配的 Blend 段处理。
    """

    SLOT_FIX_RESOURCE_NAME_DICT = {
        ZZMIDX12TextureMarkName.DiffuseMap: r"Resource\ZZMI\Diffuse",
        ZZMIDX12TextureMarkName.NormalMap: r"Resource\ZZMI\NormalMap",
        ZZMIDX12TextureMarkName.LightMap: r"Resource\ZZMI\LightMap",
        ZZMIDX12TextureMarkName.MaterialMap: r"Resource\ZZMI\MaterialMap",
        ZZMIDX12TextureMarkName.WengineFx: r"Resource\ZZMI\WengineFx",
        ZZMIDX12TextureMarkName.WengineFX: r"Resource\ZZMI\WengineFx",
        ZZMIDX12TextureMarkName.ZglowMap: r"Resource\ZZMI\GlowMap",
    }

    def get_blend_match_cs(self, drawib_model) -> str:
        """从 SubMesh 元数据中获取 Blend Category 的 ``match_cs`` 哈希。

        Args:
            drawib_model: 当前 DrawIB 模型。

        Returns:
            首个非空的 ``match_cs`` 字符串；若全部为空则返回空串。
        """
        for submesh_model in drawib_model.submesh_model_list:
            match_cs = str(getattr(submesh_model, "match_cs", "") or "").strip()
            if match_cs:
                return match_cs
        return ""

    def get_blend_match_uav_bytes(self, drawib_model) -> int:
        """从 SubMesh 元数据中获取 Blend Category 的 ``match_uav_bytes`` 字节数。

        Args:
            drawib_model: 当前 DrawIB 模型。

        Returns:
            首个 > 0 的 ``match_uav_bytes`` 整数；若全部不可用则返回 0。
        """
        for submesh_model in drawib_model.submesh_model_list:
            try:
                match_uav_bytes = int(getattr(submesh_model, "match_uav_bytes", 0) or 0)
            except (TypeError, ValueError):
                match_uav_bytes = 0
            if match_uav_bytes > 0:
                return match_uav_bytes
        return 0

    def add_unity_vs_texture_override_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """生成 DX12 模式下的 VB TextureOverride 段。

        与 D3D11 模式相比，DX12 模式在 Blend Category 上额外写入 ``match_cs``
        和 ``match_uav_bytes`` 用以精确匹配 DX12 的 dispatch 调用。
        """
        d3d11_game_type = drawib_model.d3d11GameType
        draw_ib = drawib_model.draw_ib

        texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
        texture_override_vb_section.append("; " + draw_ib)
        for category_name in d3d11_game_type.OrderedCategoryNameList:
            category_hash = drawib_model.category_hash_dict.get(category_name, "")
            texture_override_vb_name_suffix = "VB_" + draw_ib + "_" + drawib_model.draw_ib_alias + "_" + category_name
            texture_override_vb_section.append("[TextureOverride_" + texture_override_vb_name_suffix + "]")
            texture_override_vb_section.append("hash = " + category_hash)

            for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                if category_name != draw_category_name:
                    continue
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_vb_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            # DX12 关键差异：在 Blend Category 写入 match_cs / match_uav_bytes
            draw_category_name = d3d11_game_type.CategoryDrawCategoryDict.get("Blend", None)
            if draw_category_name is not None and category_name == draw_category_name:
                match_cs = self.get_blend_match_cs(drawib_model)
                match_uav_bytes = self.get_blend_match_uav_bytes(drawib_model)
                if match_cs:
                    texture_override_vb_section.append("match_cs = " + match_cs)
                if match_uav_bytes > 0:
                    texture_override_vb_section.append("match_uav_bytes = " + str(match_uav_bytes))
                texture_override_vb_section.append("handling = skip")
                texture_override_vb_section.append("draw = " + str(drawib_model.draw_number) + ", 0")

            if category_name == d3d11_game_type.CategoryDrawCategoryDict["Position"]:
                if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                    texture_override_vb_section.append("$active" + str(GlobalKeyCountHelper.generated_mod_number) + " = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_vb_section.append("$ActiveCharacter = 1")

            texture_override_vb_section.new_line()

        ini_builder.append_section(texture_override_vb_section)

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """生成 DX12 模式下的 IB TextureOverride 段。

        基本结构与 ZZMI D3D11 模式一致，但不处理跨 IB 复杂场景。
        """
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

            texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
            if not GlobalProterties.forbid_auto_texture_ini() and texture_markup_info_list:
                slot_fix_enabled = GlobalProterties.zzz_use_slot_fix()
                uses_slot_fix = False

                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(texture_markup_info.mark_type):
                        continue

                    slot_fix_resource_name = self.SLOT_FIX_RESOURCE_NAME_DICT.get(texture_markup_info.mark_name)
                    if slot_fix_enabled and slot_fix_resource_name is not None:
                        texture_override_ib_section.append(
                            slot_fix_resource_name + " = ref " + texture_markup_info.get_resource_name()
                        )
                        uses_slot_fix = True
                    else:
                        texture_override_ib_section.append(
                            texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name()
                        )

                if uses_slot_fix:
                    texture_override_ib_section.append(r"run = CommandList\ZZMI\SetTextures")

            if texture_markup_info_list:
                texture_override_ib_section.append("run = CommandListSkinTexture")

            for drawindexed_str in M_IniHelper.get_drawindexed_str_list(
                submesh_model.drawcall_model_list,
                obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
            ):
                texture_override_ib_section.append(drawindexed_str)

        ini_builder.append_section(texture_override_ib_section)

    def export(self):
        """主导出流程。

        DX12 模式不需要 VertexLimitRaise 段，因此跳过 ``add_unity_vs_texture_override_vlr_section``。
        其余流程与 ZZMI D3D11 模式保持一致。
        """
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
            # 只把这个 draw_ib 自己的 ini 段包进登记上下文（DX12 模式不需要
            # VertexLimitRaise）；贴图搬移与全局计数不是 ini 段，留在上下文外。
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


ModModelZZMIDX12 = ExportZZMIDX12
