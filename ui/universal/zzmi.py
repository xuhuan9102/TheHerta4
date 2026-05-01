import os

from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from .unity import ExportUnity
from ...utils.timer_utils import TimerUtils


class ZZMITextureMarkName:
    DiffuseMap = "DiffuseMap"
    NormalMap = "NormalMap"
    LightMap = "LightMap"
    MaterialMap = "MaterialMap"
    StockingMap = "StockingMap"


class ExportZZMI(ExportUnity):
    SLOT_FIX_RESOURCE_NAME_DICT = {
        ZZMITextureMarkName.DiffuseMap: r"Resource\ZZMI\Diffuse",
        ZZMITextureMarkName.NormalMap: r"Resource\ZZMI\NormalMap",
        ZZMITextureMarkName.LightMap: r"Resource\ZZMI\LightMap",
        ZZMITextureMarkName.MaterialMap: r"Resource\ZZMI\MaterialMap",
        ZZMITextureMarkName.StockingMap: r"Resource\ZZMI\WengineFx",
    }

    def __init__(self, blueprint_model):
        super().__init__(blueprint_model)

        self.cross_ib_info_dict = blueprint_model.cross_ib_info_dict
        self.cross_ib_method_dict = blueprint_model.cross_ib_method_dict
        self.has_cross_ib = blueprint_model.has_cross_ib
        self.cross_ib_object_names = blueprint_model.cross_ib_object_names

        print(f"[CrossIB ZZMI] 初始化: has_cross_ib={self.has_cross_ib}")
        print(f"[CrossIB ZZMI] cross_ib_info_dict={self.cross_ib_info_dict}")
        print(f"[CrossIB ZZMI] cross_ib_object_names={self.cross_ib_object_names}")

    def _get_submesh_ib_key(self, submesh_model, draw_ib):
        return f"{draw_ib}_{submesh_model.match_first_index}"

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
        draw_ib = drawib_model.draw_ib

        print(f"[CrossIB ZZMI] 处理 draw_ib={draw_ib}, has_cross_ib={self.has_cross_ib}")

        texture_override_ib_section.append("[TextureOverride_IB_" + draw_ib + "]")
        texture_override_ib_section.append("hash = " + draw_ib)
        texture_override_ib_section.append("handling = skip")
        texture_override_ib_section.new_line()

        for submesh_model in drawib_model.submesh_model_list:
            texture_override_name_suffix = drawib_model.get_submesh_texture_override_suffix(submesh_model)
            ib_resource_name = drawib_model.get_submesh_ib_resource_name(submesh_model)

            current_ib_key = self._get_submesh_ib_key(submesh_model, draw_ib)
            is_cross_ib_source = current_ib_key in self.cross_ib_info_dict
            is_cross_ib_target = any(current_ib_key in targets for targets in self.cross_ib_info_dict.values())

            print(f"[CrossIB ZZMI] submesh={submesh_model.unique_str}, ib_key={current_ib_key}, is_source={is_cross_ib_source}, is_target={is_cross_ib_target}")

            source_ib_list_for_target = []
            if is_cross_ib_target:
                for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                    if current_ib_key in target_ib_list:
                        source_ib_list_for_target.append(source_ib)

            if is_cross_ib_source:
                texture_override_ib_section.append("[ResourceBodyVB_" + draw_ib + "_" + str(submesh_model.match_first_index) + "]")

            texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
            texture_override_ib_section.append("hash = " + draw_ib)
            texture_override_ib_section.append("match_first_index = " + str(submesh_model.match_first_index))

            if is_cross_ib_source:
                texture_override_ib_section.append("ResourceBodyVB_" + draw_ib + "_" + str(submesh_model.match_first_index) + " = copy vb0")

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
                    if texture_markup_info.mark_type != "Slot":
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

            if is_cross_ib_source:
                non_cross_ib_drawcalls = []
                for drawcall_model in submesh_model.drawcall_model_list:
                    obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)
                    if obj_name not in self.cross_ib_object_names:
                        non_cross_ib_drawcalls.append(drawcall_model)

                print(f"[CrossIB ZZMI] 源块绘制非跨IB物体: {len(non_cross_ib_drawcalls)} 个")

                for drawindexed_str in M_IniHelper.get_drawindexed_str_list(
                    non_cross_ib_drawcalls,
                    obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
                ):
                    texture_override_ib_section.append(drawindexed_str)
            else:
                print(f"[CrossIB ZZMI] 非源块绘制物体: {len(submesh_model.drawcall_model_list)} 个")

                for drawindexed_str in M_IniHelper.get_drawindexed_str_list(
                    submesh_model.drawcall_model_list,
                    obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
                ):
                    texture_override_ib_section.append(drawindexed_str)

            if is_cross_ib_target and source_ib_list_for_target:
                print(f"[CrossIB ZZMI] 目标块处理: source_ib_list={source_ib_list_for_target}")

                for source_ib_key in source_ib_list_for_target:
                    source_parts = source_ib_key.split("_")
                    source_hash = source_parts[0]
                    source_first_index = int(source_parts[1]) if len(source_parts) > 1 else 0

                    print(f"[CrossIB ZZMI] 查找源块: hash={source_hash}, first_index={source_first_index}")

                    source_drawib_model = None
                    for dib_model in self.drawib_model_list:
                        if dib_model.draw_ib == source_hash:
                            source_drawib_model = dib_model
                            print(f"[CrossIB ZZMI] 找到源 DrawIBModel: {dib_model.draw_ib}")
                            break

                    source_submesh = None
                    if source_drawib_model:
                        for sm in source_drawib_model.submesh_model_list:
                            if str(sm.match_first_index) == str(source_first_index):
                                source_submesh = sm
                                print(f"[CrossIB ZZMI] 找到源 submesh: {sm.unique_str}")
                                break

                    if source_submesh:
                        source_ib_resource_name = source_drawib_model.get_submesh_ib_resource_name(source_submesh)
                        texture_override_ib_section.append("ib = " + source_ib_resource_name)

                        texture_override_ib_section.append("vb0 = ResourceBodyVB_" + source_hash + "_" + str(source_first_index))
                        texture_override_ib_section.append("vb1 = Resource" + source_hash + "Texcoord")
                        texture_override_ib_section.append("vb2 = Resource" + source_hash + "Blend")
                        texture_override_ib_section.append("vb3 = ResourceBodyVB_" + source_hash + "_" + str(source_first_index))

                        cross_ib_drawcalls = []
                        for drawcall_model in source_submesh.drawcall_model_list:
                            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)
                            if obj_name in self.cross_ib_object_names:
                                cross_ib_drawcalls.append(drawcall_model)

                        print(f"[CrossIB ZZMI] 跨IB物体数量: {len(cross_ib_drawcalls)}")

                        if cross_ib_drawcalls:
                            for drawindexed_str in M_IniHelper.get_drawindexed_str_list(
                                cross_ib_drawcalls,
                                obj_name_draw_offset_dict=source_drawib_model.obj_name_draw_offset,
                            ):
                                texture_override_ib_section.append(drawindexed_str)
                    else:
                        print(f"[CrossIB ZZMI] 警告: 未找到源块 submesh for {source_ib_key}")

        ini_builder.append_section(texture_override_ib_section)

    def export(self):
        TimerUtils.start_stage("缓冲文件生成")
        self.generate_buffer_files(GlobalConfig.path_generatemod_buffer_folder())
        TimerUtils.end_stage("缓冲文件生成")

        if self.has_cross_ib:
            for node_name, cross_ib_method in self.cross_ib_method_dict.items():
                if cross_ib_method and cross_ib_method != 'VB_COPY':
                    print(f"[CrossIB] ❌ 错误: 节点 '{node_name}' 使用的跨 IB 方式 '{cross_ib_method}' 不适用于 ZZMI 模式")
                    print(f"[CrossIB] ZZMI 模式只支持 'VB_COPY' (VB 复制) 方式")
                    print(f"[CrossIB] 请在 Cross IB 节点中将跨 IB 方式改为 'VB_COPY'")
                    self.has_cross_ib = False
                    break

        print(f"[CrossIB ZZMI] export: has_cross_ib={self.has_cross_ib}")

        TimerUtils.start_stage("INI配置生成")
        ini_builder = M_IniBuilder()
        drawib_drawibmodel_dict = {drawib_model.draw_ib: drawib_model for drawib_model in self.drawib_model_list}

        M_IniHelper.generate_hash_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        self._integrate_object_swap_ini_hook(ini_builder)
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
        ini_builder.save_to_file(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + ".ini"))
        TimerUtils.end_stage("INI配置生成")


ModModelZZMI = ExportZZMI
