from ...blueprint.model import BluePrintModel
from ...common.submesh_model import SubMeshModel
from ...common.drawib_model import DrawIBModel
from dataclasses import dataclass,field
from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...blueprint.export_helper import BlueprintExportHelper

from ...common.buffer_export_helper import BufferExportHelper
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...common.m_ini_builder import M_IniBuilder,M_IniSection, M_SectionType
from .export_helper import ExportHelper
from ...utils.timer_utils import TimerUtils

import os
import re
import shutil

@dataclass
class ExportEFMI:

    blueprint_model:BluePrintModel

    submesh_model_list:list[SubMeshModel] = field(default_factory=list,init=False)
    drawib_model_list:list[DrawIBModel] = field(default_factory=list,init=False)

    def __post_init__(self):
        self.submesh_model_list = ExportHelper.parse_submesh_model_list_from_blueprint_model(self.blueprint_model)
        # EFMI 直接复用已经解析好的 SubMeshModel，避免同一轮导出把几何解析做两遍。
        self.drawib_model_list = ExportHelper.parse_drawib_model_list_from_submesh_model_list(
            submesh_model_list=self.submesh_model_list,
            combine_ib=False,
        )
        print("SubMeshModel列表初始化完成，共有 " + str(len(self.submesh_model_list)) + " 个SubMeshModel")

        self.cross_ib_info_dict = self.blueprint_model.cross_ib_info_dict
        self.cross_ib_method_dict = self.blueprint_model.cross_ib_method_dict
        self.has_cross_ib = self.blueprint_model.has_cross_ib
        self.cross_ib_mapping_objects = self.blueprint_model.cross_ib_mapping_objects
        self.cross_ib_vb_condition_mapping = self.blueprint_model.cross_ib_vb_condition_mapping
        self.cross_ib_source_to_target_dict = self.blueprint_model.cross_ib_source_to_target_dict
        self.cross_ib_object_vb_condition = self.blueprint_model.cross_ib_object_vb_condition
        self.cross_ib_target_info = self.blueprint_model.cross_ib_target_info
        self.cross_ib_match_mode = self.blueprint_model.cross_ib_match_mode
        self.cross_ib_object_names = self.blueprint_model.cross_ib_object_names

        print(f"[CrossIB EFMI] 初始化: has_cross_ib={self.has_cross_ib}")
        print(f"[CrossIB EFMI] cross_ib_info_dict={self.cross_ib_info_dict}")
        print(f"[CrossIB EFMI] cross_ib_object_names={self.cross_ib_object_names}")
        print(f"[CrossIB EFMI] cross_ib_mapping_objects={self.cross_ib_mapping_objects}")

    def generate_buffer_files(self):
        buf_output_folder = GlobalConfig.path_generatemod_buffer_folder()

        for submesh_model in self.submesh_model_list:
            print("ExportEFMI: 导出SubMeshModel，Unique标识: " + submesh_model.unique_str)

            ib_filename = submesh_model.unique_str + "-Index.buf"
            ib_filepath = os.path.join(buf_output_folder, ib_filename)
            BufferExportHelper.write_buf_ib_r32_uint(submesh_model.ib, ib_filepath)

            for category, category_buf in submesh_model.category_buffer_dict.items():
                category_buf_filename = submesh_model.unique_str + "-" + category + ".buf"
                category_buf_filepath = os.path.join(buf_output_folder, category_buf_filename)
                with open(category_buf_filepath, 'wb') as f:
                    category_buf.tofile(f)

    def _get_submesh_ib_key(self, submesh_model):
        if self.cross_ib_match_mode == 'INDEX_COUNT':
            return f"indexcount_{submesh_model.match_index_count}"
        else:
            return f"{submesh_model.match_draw_ib}_{submesh_model.match_first_index}"

    def _get_all_cross_ib_identifiers(self):
        all_identifiers = set()

        if self.cross_ib_match_mode == 'INDEX_COUNT':
            for source_key, target_key_list in self.cross_ib_info_dict.items():
                if source_key.startswith('indexcount_'):
                    index_count = source_key.replace('indexcount_', '')
                    all_identifiers.add(index_count)
                for target_key in target_key_list:
                    if target_key.startswith('indexcount_'):
                        index_count = target_key.replace('indexcount_', '')
                        all_identifiers.add(index_count)

            for submesh_model in self.submesh_model_list:
                if submesh_model.match_index_count:
                    all_identifiers.add(submesh_model.match_index_count)
        else:
            for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                source_hash = source_ib.split("_")[0]
                all_identifiers.add(source_hash)
                for target_ib in target_ib_list:
                    target_hash = target_ib.split("_")[0]
                    all_identifiers.add(target_hash)

            for drawib_model in self.drawib_model_list:
                all_identifiers.add(drawib_model.draw_ib)

        return all_identifiers

    def _get_vb_condition_for_mapping(self, source_ib_key, target_ib_key, condition_type='source'):
        mapping_key = (source_ib_key, target_ib_key)
        condition_info = self.cross_ib_vb_condition_mapping.get(mapping_key, {})
        if condition_type == 'source':
            return condition_info.get('source', "if vs == 200 || vs == 201 || vs == 204")
        else:
            return condition_info.get('target', "if vs == 202 || vs == 203")

    def _get_vb_condition_for_object(self, obj_name, source_ib_key, target_ib_key, condition_type='source'):
        object_mapping_key = (obj_name, source_ib_key, target_ib_key)
        condition_info = self.cross_ib_object_vb_condition.get(object_mapping_key, {})
        if condition_type == 'source':
            return condition_info.get('source', "if vs == 200 || vs == 201 || vs == 204")
        else:
            return condition_info.get('target', "if vs == 202 || vs == 203")

    def _split_drawcalls_by_cross_ib(self, drawcall_model_list, source_ib_key=None, target_ib_key=None):
        cross_ib_drawcalls = []
        non_cross_ib_drawcalls = []

        cross_ib_mapping_objects = self.cross_ib_mapping_objects

        for drawcall_model in drawcall_model_list:
            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)

            is_cross_ib = False
            if source_ib_key:
                if target_ib_key:
                    mapping_key = (source_ib_key, target_ib_key)
                    if mapping_key in cross_ib_mapping_objects:
                        if obj_name in cross_ib_mapping_objects[mapping_key]:
                            is_cross_ib = True
                else:
                    for (src_key, tgt_key), obj_names in cross_ib_mapping_objects.items():
                        if src_key == source_ib_key and obj_name in obj_names:
                            is_cross_ib = True
                            break
            else:
                if obj_name in self.cross_ib_object_names:
                    is_cross_ib = True

            if is_cross_ib:
                cross_ib_drawcalls.append(drawcall_model)
            else:
                non_cross_ib_drawcalls.append(drawcall_model)

        return cross_ib_drawcalls, non_cross_ib_drawcalls

    def _group_drawcalls_by_cross_ib_target(self, drawcall_model_list, source_ib_key, target_ib_keys):
        grouped = {}
        cross_ib_mapping_objects = self.cross_ib_mapping_objects

        for drawcall_model in drawcall_model_list:
            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)

            for target_ib_key in target_ib_keys:
                mapping_key = (source_ib_key, target_ib_key)
                if mapping_key in cross_ib_mapping_objects:
                    if obj_name in cross_ib_mapping_objects[mapping_key]:
                        vb_condition = self._get_vb_condition_for_object(obj_name, source_ib_key, target_ib_key, 'source')
                        group_key = (target_ib_key, vb_condition)
                        if group_key not in grouped:
                            grouped[group_key] = []
                        grouped[group_key].append(drawcall_model)
                        break

        return grouped

    def _generate_cross_ib_block_for_source(self, source_identifier, drawcall_model_list, source_ib_key=None, target_ib_key=None):
        lines = []

        cross_ib_drawcalls, non_cross_ib_drawcalls = self._split_drawcalls_by_cross_ib(
            drawcall_model_list,
            source_ib_key=source_ib_key
        )

        target_ib_keys = self.cross_ib_source_to_target_dict.get(source_ib_key, [])
        if target_ib_key and target_ib_key not in target_ib_keys:
            target_ib_keys.append(target_ib_key)

        grouped_drawcalls = self._group_drawcalls_by_cross_ib_target(cross_ib_drawcalls, source_ib_key, target_ib_keys)

        for (tgt_ib_key, vb_condition), objects in grouped_drawcalls.items():
            if not objects:
                continue

            lines.append(";跨 iB 区域")
            lines.append(vb_condition)
            lines.append("    run = CustomShader_ExtractCB1")
            lines.append(f"    cs-t2 = ResourceID_{source_identifier}")
            lines.append(f"    run = CustomShader_RecordBones_{source_identifier}")
            lines.append(f"    run = CustomShader_RedirectCB1_{source_identifier}")
            lines.append(f"    vs-t0 = ResourceFakeT0_SRV_{source_identifier}")
            lines.append(f"    vs-cb1 = ResourceFakeCB1_{source_identifier}")
            lines.append(";所有需要跨 Ib 的物体引用")

            drawindexed_str_list = M_IniHelper.get_drawindexed_instanced_str_list(objects)
            for drawindexed_str in drawindexed_str_list:
                if drawindexed_str.strip():
                    lines.append(drawindexed_str)

            lines.append("endif")

        lines.append(";不需要跨 Ib 的物体引用")

        if non_cross_ib_drawcalls:
            drawindexed_str_list = M_IniHelper.get_drawindexed_instanced_str_list(non_cross_ib_drawcalls)
            for drawindexed_str in drawindexed_str_list:
                if drawindexed_str.strip():
                    lines.append(drawindexed_str)

        lines.append("")
        lines.append(f"post vs-cb1 = null")
        lines.append(f"post vs-t0 = null")
        lines.append(f"post cs-t2 = null")

        return lines

    def _add_cross_ib_present_section(self, ini_builder):
        if not self.has_cross_ib:
            return

        present_section = M_IniSection(M_SectionType.CrossIBPresent)
        present_section.append(";特殊追加固定区域")
        present_section.append("[Present]")
        present_section.append("ResourcePrev_SRV = ResourceFakeT0_SRV")
        present_section.new_line()

        present_section.append("[ResourceDumpedCB1_UAV]")
        present_section.append("type = RWStructuredBuffer")
        present_section.append("stride = 16")
        present_section.append("array = 4096")
        present_section.new_line()

        present_section.append("[ResourceDumpedCB1_SRV]")
        present_section.append("type = Buffer")
        present_section.append("stride = 16")
        present_section.append("array = 4096")
        present_section.new_line()

        all_identifiers = self._get_all_cross_ib_identifiers()

        for identifier in sorted(all_identifiers):
            present_section.append(f"[ResourceFakeCB1_UAV_{identifier}]")
            present_section.append("type = RWStructuredBuffer")
            present_section.append("stride = 16")
            present_section.append("array = 4096")
            present_section.new_line()

            present_section.append(f"[ResourceFakeCB1_{identifier}]")
            present_section.append("type = Buffer")
            present_section.append("stride = 16")
            present_section.append("format = R32G32B32A32_UINT")
            present_section.append("array = 4096")
            present_section.new_line()

            present_section.append(f"[ResourceFakeT0_UAV_{identifier}]")
            present_section.append("type = RWStructuredBuffer")
            present_section.append("stride = 16")
            present_section.append("array = 200000")
            present_section.new_line()

            present_section.append(f"[ResourceFakeT0_SRV_{identifier}]")
            present_section.append("type = StructuredBuffer")
            present_section.append("stride = 16")
            present_section.append("array = 200000")
            present_section.new_line()

        present_section.append("[ResourceFakeT0_UAV]")
        present_section.append("type = RWStructuredBuffer")
        present_section.append("stride = 16")
        present_section.append("array = 200000")
        present_section.new_line()

        present_section.append("[ResourceFakeT0_SRV]")
        present_section.append("type = StructuredBuffer")
        present_section.append("stride = 16")
        present_section.append("array = 200000")
        present_section.new_line()

        present_section.append("[ResourcePrev_SRV]")
        present_section.append("type = StructuredBuffer")
        present_section.append("stride = 16")
        present_section.append("array = 200000")
        present_section.new_line()

        present_section.append("[CustomShader_ExtractCB1]")
        present_section.append("vs = ./res/extract_cb1_vs.hlsl")
        present_section.append("ps = ./res/extract_cb1_ps.hlsl")
        present_section.append("ps-u7 = ResourceDumpedCB1_UAV")
        present_section.append("depth_enable = false")
        present_section.append("blend = ADD SRC_ALPHA INV_SRC_ALPHA")
        present_section.append("cull = none")
        present_section.append("topology = point_list")
        present_section.append("draw = 4096, 0")
        present_section.append("ps-u7 = null")
        present_section.append("ResourceDumpedCB1_SRV = copy ResourceDumpedCB1_UAV")
        present_section.new_line()

        for identifier in sorted(all_identifiers):
            present_section.append(f"[CustomShader_RecordBones_{identifier}]")
            present_section.append("cs = ./res/record_bones_cs.hlsl")
            present_section.append("cs-t0 = vs-t0")
            present_section.append("cs-t1 = ResourceDumpedCB1_SRV")
            present_section.append(f"cs-u1 = ResourceFakeT0_UAV_{identifier}")
            present_section.append("dispatch = 12, 1, 1")
            present_section.append("cs-u1 = null")
            present_section.append("cs-t0 = null")
            present_section.append("cs-t1 = null")
            present_section.append(f"ResourceFakeT0_SRV_{identifier} = copy ResourceFakeT0_UAV_{identifier}")
            present_section.new_line()

            present_section.append(f"[CustomShader_RedirectCB1_{identifier}]")
            present_section.append("cs = ./res/redirect_cb1_cs.hlsl")
            present_section.append("cs-t0 = ResourceDumpedCB1_SRV")
            present_section.append(f"ResourceFakeCB1_UAV_{identifier} = copy ResourceDumpedCB1_SRV")
            present_section.append(f"cs-u0 = ResourceFakeCB1_UAV_{identifier}")
            present_section.append("dispatch = 4, 1, 1")
            present_section.append("cs-u0 = null")
            present_section.append("cs-t0 = null")
            present_section.append(f"ResourceFakeCB1_{identifier} = copy ResourceFakeCB1_UAV_{identifier}")
            present_section.new_line()

        shader_overrides = [
            ("ShaderOverridevs1000", "241383a9d64b4978", "200"),
            ("ShaderOverridevs1001", "6733250da4e23fd6", "200"),
            ("ShaderOverridevs1002", "d66e2204be43808b", "200"),
            ("ShaderOverridevs1003", "9bac7486f7930a24", "201"),
            ("ShaderOverridevs1004", "f2c6f6a1e116c2bf", "201"),
            ("ShaderOverridevs1005", "a33eb5546f729a5d", "201"),
            ("ShaderOverridevs1006", "1bd133ecc1915893", "201"),
            ("ShaderOverridevs1007", "b30cc5ad521e0700", "202"),
            ("ShaderOverridevs1008", "5eb5517c9d2c7e6c", "202"),
            ("ShaderOverridevs1009", "4921f64a7c74226d", "203"),
            ("ShaderOverridevs1010", "1b835d0e8dbbfb8f", "203"),
            ("ShaderOverridevs1011", "06c94dd56f447210", "204"),
            ("ShaderOverridevs1012", "f47b1f797f5831d0", "204"),
            ("ShaderOverridevs1013", "906a3976f3e33cfb", "204"),
        ]

        for name, hash_val, filter_idx in shader_overrides:
            present_section.append(f"[{name}]")
            present_section.append(f"hash = {hash_val}")
            present_section.append(f"filter_index = {filter_idx}")
            present_section.append("allow_duplicate_hash = overrule")
            present_section.new_line()

        ini_builder.append_section(present_section)

    def _add_cross_ib_resource_id_sections(self, ini_builder):
        if not self.has_cross_ib:
            return

        resource_id_section = M_IniSection(M_SectionType.ResourceID)
        resource_id_section.append(";特殊追加身份证区域")

        all_identifiers = set()

        if self.cross_ib_match_mode == 'INDEX_COUNT':
            for source_key, target_key_list in self.cross_ib_info_dict.items():
                if source_key.startswith('indexcount_'):
                    index_count = source_key.replace('indexcount_', '')
                    all_identifiers.add(index_count)
                for target_key in target_key_list:
                    if target_key.startswith('indexcount_'):
                        index_count = target_key.replace('indexcount_', '')
                        all_identifiers.add(index_count)

            for submesh_model in self.submesh_model_list:
                if submesh_model.match_index_count:
                    all_identifiers.add(submesh_model.match_index_count)
        else:
            for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                source_hash = source_ib.split("_")[0]
                all_identifiers.add(source_hash)
                for target_ib in target_ib_list:
                    target_hash = target_ib.split("_")[0]
                    all_identifiers.add(target_hash)

            for drawib_model in self.drawib_model_list:
                all_identifiers.add(drawib_model.draw_ib)

        sorted_identifiers = sorted(list(all_identifiers))

        for idx, identifier in enumerate(sorted_identifiers):
            resource_id_section.append(f"[ResourceID_{identifier}]")
            resource_id_section.append("type = Buffer")
            resource_id_section.append("format = R32_FLOAT")
            resource_id_section.append(f"data = {idx * 1000}.0")
            resource_id_section.new_line()

        ini_builder.append_section(resource_id_section)

    def _find_source_submesh_by_ib_key(self, source_ib_key):
        for submesh_model in self.submesh_model_list:
            submesh_ib_key = self._get_submesh_ib_key(submesh_model)
            if submesh_ib_key == source_ib_key:
                return submesh_model
        return None

    def _find_source_drawib_by_ib_key(self, source_ib_key):
        if self.cross_ib_match_mode == 'INDEX_COUNT':
            index_count = source_ib_key.replace('indexcount_', '') if source_ib_key.startswith('indexcount_') else None
            if index_count:
                for drawib_model in self.drawib_model_list:
                    for submesh in drawib_model.submesh_model_list:
                        if submesh.match_index_count == index_count:
                            return drawib_model
            return None
        else:
            source_hash = source_ib_key.split("_")[0]
            for drawib_model in self.drawib_model_list:
                if drawib_model.draw_ib == source_hash:
                    return drawib_model
            return None

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

        if self.has_cross_ib:
            for node_name, cross_ib_method in self.cross_ib_method_dict.items():
                if cross_ib_method != 'END_FIELD':
                    print(f"[CrossIB] 警告: 节点 {node_name} 使用的跨 IB 方式 '{cross_ib_method}' 不适用于 EFMI 模式")
                    self.has_cross_ib = False
                    break

        if self.has_cross_ib:
            self._add_cross_ib_present_section(ini_builder)
            self._add_cross_ib_resource_id_sections(ini_builder)

        M_IniHelper.generate_hash_style_texture_ini(
            ini_builder=ini_builder,
            drawib_drawibmodel_dict=drawib_drawibmodel_dict,
        )

        self._integrate_object_swap_ini_hook(ini_builder)

        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)

        for submesh_model in self.submesh_model_list:
            drawib_model = drawib_drawibmodel_dict.get(submesh_model.match_draw_ib)
            active_index = draw_ib_active_index_dict.get(submesh_model.match_draw_ib, 0)

            current_ib_key = self._get_submesh_ib_key(submesh_model)

            is_source_ib = current_ib_key in self.cross_ib_info_dict
            source_ib_list_for_target = self.cross_ib_target_info.get(current_ib_key, [])
            is_target_ib = len(source_ib_list_for_target) > 0

            if self.cross_ib_match_mode == 'INDEX_COUNT':
                current_identifier = submesh_model.match_index_count
            else:
                current_identifier = submesh_model.match_draw_ib

            texture_override_ib_section.append("[TextureOverride_" + submesh_model.unique_str.replace("-","_") + "]")
            texture_override_ib_section.append("hash = " + submesh_model.match_draw_ib)
            texture_override_ib_section.append("match_first_index = " + submesh_model.match_first_index)
            texture_override_ib_section.append("match_index_count = " + submesh_model.match_index_count)
            texture_override_ib_section.append("handling = skip")

            if is_target_ib:
                texture_override_ib_section.append("analyse_options = deferred_ctx_immediate dump_rt dump_cb dump_vb dump_ib buf txt dds dump_tex dds symlink")

            texture_override_ib_section.append("run = CommandList\\EFMIv1\\OverrideTextures")

            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            texture_override_ib_section.append("ib = " + ib_resource_name)

            for category in submesh_model.category_buffer_dict.keys():
                category_slot = submesh_model.d3d11_game_type.CategoryExtractSlotDict.get(category,"unknown_slot")
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                texture_override_ib_section.append(category_slot + " = " + category_resource_name)

            unique_str = submesh_model.unique_str
            texture_override_ib_section.append("vb3 = Resource_" + unique_str.replace('-', '_') + "_Position")

            if not GlobalProterties.forbid_auto_texture_ini() and drawib_model is not None:
                texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
                if GlobalProterties.use_rabbitfx_slot():
                    for texture_markup_info in texture_markup_info_list:
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        if texture_markup_info.mark_name == "DiffuseMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\Diffuse = ref " + texture_markup_info.get_resource_name())
                        elif texture_markup_info.mark_name == "LightMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\LightMap = ref " + texture_markup_info.get_resource_name())
                        elif texture_markup_info.mark_name == "NormalMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\NormalMap = ref " + texture_markup_info.get_resource_name())
                    
                    texture_override_ib_section.append("run = CommandList\\RabbitFx\\SetTextures")
                    
                    for texture_markup_info in texture_markup_info_list:
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        if texture_markup_info.mark_name in ["DiffuseMap", "LightMap", "NormalMap"]:
                            pass
                        else:
                            slot = texture_markup_info.mark_slot
                            if slot and not slot.lower().startswith("ps-t"):
                                num_match = re.search(r'\d+', slot)
                                if num_match:
                                    slot = "ps-t" + num_match.group()
                                else:
                                    slot = "ps-t" + slot
                            texture_override_ib_section.append(slot + " = " + texture_markup_info.get_resource_name())
                else:
                    for texture_markup_info in texture_markup_info_list:
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

            is_both_source_and_target = is_source_ib and is_target_ib and self.has_cross_ib

            if is_both_source_and_target:
                cross_ib_drawcalls, non_cross_ib_drawcalls = self._split_drawcalls_by_cross_ib(
                    submesh_model.drawcall_model_list,
                    source_ib_key=current_ib_key
                )

                target_ib_keys = self.cross_ib_source_to_target_dict.get(current_ib_key, [])
                grouped_source_drawcalls = self._group_drawcalls_by_cross_ib_target(
                    cross_ib_drawcalls, current_ib_key, target_ib_keys
                )

                for (target_ib_key, vb_condition), objects in grouped_source_drawcalls.items():
                    if not objects:
                        continue

                    texture_override_ib_section.append(";跨 iB 区域")
                    texture_override_ib_section.append(vb_condition)
                    texture_override_ib_section.append("    run = CustomShader_ExtractCB1")
                    texture_override_ib_section.append(f"    cs-t2 = ResourceID_{current_identifier}")
                    texture_override_ib_section.append(f"    run = CustomShader_RecordBones_{current_identifier}")
                    texture_override_ib_section.append(f"    run = CustomShader_RedirectCB1_{current_identifier}")
                    texture_override_ib_section.append(f"    vs-t0 = ResourceFakeT0_SRV_{current_identifier}")
                    texture_override_ib_section.append(f"    vs-cb1 = ResourceFakeCB1_{current_identifier}")
                    texture_override_ib_section.append(";所有需要跨 Ib 的物体引用")

                    drawindexed_str_list = M_IniHelper.get_drawindexed_instanced_str_list(objects)
                    for drawindexed_str in drawindexed_str_list:
                        if drawindexed_str.strip():
                            texture_override_ib_section.append(drawindexed_str)

                    texture_override_ib_section.append("endif")

                texture_override_ib_section.append(";不需要跨 Ib 的物体引用")

                if non_cross_ib_drawcalls:
                    drawindexed_str_list = M_IniHelper.get_drawindexed_instanced_str_list(non_cross_ib_drawcalls)
                    for drawindexed_str in drawindexed_str_list:
                        if drawindexed_str.strip():
                            texture_override_ib_section.append(drawindexed_str)

                if is_target_ib and source_ib_list_for_target:
                    self._append_target_cross_ib_blocks(
                        texture_override_ib_section, source_ib_list_for_target, current_ib_key
                    )

                texture_override_ib_section.append("")
                texture_override_ib_section.append("post vs-cb1 = null")
                texture_override_ib_section.append("post vs-t0 = null")
                texture_override_ib_section.append("post cs-t2 = null")

            elif is_source_ib and self.has_cross_ib:
                target_ib_keys = self.cross_ib_source_to_target_dict.get(current_ib_key, [])
                target_ib_key = target_ib_keys[0] if target_ib_keys else None
                cross_ib_lines = self._generate_cross_ib_block_for_source(
                    current_identifier, submesh_model.drawcall_model_list,
                    source_ib_key=current_ib_key, target_ib_key=target_ib_key
                )
                for line in cross_ib_lines:
                    texture_override_ib_section.append(line)

            elif is_target_ib and self.has_cross_ib and source_ib_list_for_target:
                all_target_drawcalls = submesh_model.drawcall_model_list
                if all_target_drawcalls:
                    drawindexed_str_list = M_IniHelper.get_drawindexed_instanced_str_list(all_target_drawcalls)
                    for drawindexed_str in drawindexed_str_list:
                        if drawindexed_str.strip():
                            texture_override_ib_section.append(drawindexed_str)

                self._append_target_cross_ib_blocks(
                    texture_override_ib_section, source_ib_list_for_target, current_ib_key
                )

                texture_override_ib_section.append("")
                texture_override_ib_section.append("post vs-cb1 = null")
                texture_override_ib_section.append("post vs-t0 = null")
                texture_override_ib_section.append("post cs-t2 = null")

            else:
                for draw_line in M_IniHelper.get_drawindexed_instanced_str_list(submesh_model.drawcall_model_list):
                    texture_override_ib_section.append(draw_line)

            if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                texture_override_ib_section.append("$active" + str(active_index) + " = 1")
                if GlobalProterties.generate_branch_mod_gui():
                    texture_override_ib_section.append("$ActiveCharacter = 1")

            texture_override_ib_section.new_line()

        ini_builder.append_section(texture_override_ib_section)

        resource_buffer_section = M_IniSection(M_SectionType.ResourceBuffer)
        buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
        for submesh_model in self.submesh_model_list:
            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            resource_buffer_section.append("[" + ib_resource_name + "]")
            resource_buffer_section.append("type = Buffer")
            resource_buffer_section.append("format = DXGI_FORMAT_R32_UINT")
            resource_buffer_section.append("filename = " + buffer_folder_name + "\\" + submesh_model.unique_str + "-Index.buf")
            resource_buffer_section.new_line()

            for category in submesh_model.category_buffer_dict.keys():
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                stride = submesh_model.d3d11_game_type.CategoryStrideDict.get(category,0)
                resource_buffer_section.append("[" + category_resource_name + "]")
                resource_buffer_section.append("type = Buffer")
                resource_buffer_section.append("stride = " + str(stride))
                resource_buffer_section.append("filename = " + buffer_folder_name + "\\" + submesh_model.unique_str + "-" + category + ".buf")
                resource_buffer_section.new_line()

        if not GlobalProterties.forbid_auto_texture_ini():
            resource_texture_section = M_IniSection(M_SectionType.ResourceTexture)
            appended_resource_names = set()
            for drawib_model in self.drawib_model_list:
                for submesh_model in drawib_model.submesh_model_list:
                    for texture_markup_info in drawib_model.get_submesh_texture_markup_info_list(submesh_model):
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        resource_name = texture_markup_info.get_resource_name()
                        if resource_name in appended_resource_names:
                            continue
                        appended_resource_names.add(resource_name)
                        resource_texture_section.append("[" + texture_markup_info.get_resource_name() + "]")
                        resource_texture_section.append("filename = Textures/" + texture_markup_info.mark_filename)
                        resource_texture_section.new_line()
            ini_builder.append_section(resource_texture_section)

        ini_builder.append_section(resource_buffer_section)

        for drawib_model in self.drawib_model_list:
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)

        GlobalKeyCountHelper.generated_mod_number = len(self.drawib_model_list)
        M_IniHelper.add_branch_key_sections(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )
        M_IniHelperGUI.add_branch_mod_gui_section(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )

        ini_filepath = os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + ".ini")
        ini_builder.save_to_file(ini_filepath)

        if self.has_cross_ib:
            self._copy_cross_ib_hlsl_files()

    def _append_target_cross_ib_blocks(self, section, source_ib_list_for_target, current_ib_key):
        for source_ib_key in source_ib_list_for_target:
            if self.cross_ib_match_mode == 'INDEX_COUNT':
                source_identifier = source_ib_key.replace('indexcount_', '') if source_ib_key.startswith('indexcount_') else source_ib_key.split("_")[0]
            else:
                source_hash = source_ib_key.split("_")[0]
                source_identifier = source_hash

            source_submesh = self._find_source_submesh_by_ib_key(source_ib_key)
            source_drawib_model = self._find_source_drawib_by_ib_key(source_ib_key)

            if not source_submesh or not source_drawib_model:
                continue

            cross_drawcalls, _ = self._split_drawcalls_by_cross_ib(
                source_submesh.drawcall_model_list,
                source_ib_key=source_ib_key,
                target_ib_key=current_ib_key
            )

            if not cross_drawcalls:
                continue

            grouped_cross_drawcalls = {}
            for drawcall_model in cross_drawcalls:
                obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)
                vb_condition_target = self._get_vb_condition_for_object(obj_name, source_ib_key, current_ib_key, 'target')
                if vb_condition_target not in grouped_cross_drawcalls:
                    grouped_cross_drawcalls[vb_condition_target] = []
                grouped_cross_drawcalls[vb_condition_target].append(drawcall_model)

            for vb_condition_target, objects in grouped_cross_drawcalls.items():
                if not objects:
                    continue

                section.append(f";跨 IB 身份块,绘制 {source_identifier} 需要跨 Ib 的物体引用")
                if vb_condition_target:
                    section.append(vb_condition_target)
                section.append(f"    cs-t2 = ResourceID_{source_identifier}")
                section.append(f"    run = CustomShader_RedirectCB1_{source_identifier}")
                section.append(f"    vs-t0 = ResourceFakeT0_SRV_{source_identifier}")
                section.append(f"    vs-cb1 = ResourceFakeCB1_{source_identifier}")
                section.append("    ;跨 IB 块数据区域")

                source_unique_str = source_submesh.unique_str
                section.append(f"    vb0 = Resource_{source_unique_str.replace('-', '_')}_Position")
                section.append(f"    vb1 = Resource_{source_unique_str.replace('-', '_')}_Texcoord")
                section.append(f"    vb2 = Resource_{source_unique_str.replace('-', '_')}_Blend")
                section.append(f"    vb3 = Resource_{source_unique_str.replace('-', '_')}_Position")
                src_ib_resource_name = "Resource_" + source_unique_str.replace('-', '_') + "_Index"
                section.append(f"    ib = {src_ib_resource_name}")

                section.append(";所有需要跨 Ib 的物体引用")

                drawindexed_str_list = M_IniHelper.get_drawindexed_instanced_str_list(objects)
                for drawindexed_str in drawindexed_str_list:
                    if drawindexed_str.strip():
                        section.append(drawindexed_str)

                section.append("endif")

    def _copy_cross_ib_hlsl_files(self):
        addon_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
        source_dir = os.path.join(addon_dir, "Toolset", "old")

        if not os.path.exists(source_dir):
            print(f"[CrossIB] 警告: Toolset目录不存在: {source_dir}")
            return

        hlsl_files = [
            'extract_cb1_ps.hlsl',
            'extract_cb1_vs.hlsl',
            'record_bones_cs.hlsl',
            'redirect_cb1_cs.hlsl'
        ]

        mod_export_path = GlobalConfig.path_generate_mod_folder()
        res_dir = os.path.join(mod_export_path, "res")
        os.makedirs(res_dir, exist_ok=True)

        copied_count = 0
        for hlsl_file in hlsl_files:
            source_file = os.path.join(source_dir, hlsl_file)
            target_file = os.path.join(res_dir, hlsl_file)

            if os.path.exists(source_file):
                if not os.path.exists(target_file):
                    shutil.copy2(source_file, target_file)
                    print(f"[CrossIB] 已复制: {hlsl_file}")
                    copied_count += 1
                else:
                    print(f"[CrossIB] 文件已存在，跳过: {hlsl_file}")
            else:
                print(f"[CrossIB] 警告: 源文件不存在: {source_file}")

        print(f"[CrossIB] 共复制 {copied_count} 个HLSL文件到 {res_dir}")

    def _integrate_object_swap_ini_hook(self, ini_builder: M_IniBuilder):
        try:
            from ...blueprint.node_swap_ini import SwapKeyINIIntegrator
            from ...blueprint.export_helper import BlueprintExportHelper

            blueprint_tree = BlueprintExportHelper.get_current_blueprint_tree()
            if not blueprint_tree:
                return

            registry = getattr(self.blueprint_model, '_swap_key_registry', None)

            SwapKeyINIIntegrator.integrate_to_export(ini_builder, blueprint_tree, registry=registry)

        except ImportError:
            pass
        except Exception as e:
            from ...utils.log_utils import LOG
            LOG.warning(f"⚠️ 物体切换节点 INI 集成钩子执行失败: {e}")

    def export(self):
        TimerUtils.start_stage("缓冲文件生成")
        self.generate_buffer_files()
        TimerUtils.end_stage("缓冲文件生成")

        TimerUtils.start_stage("INI配置生成")
        self.generate_ini_file()
        TimerUtils.end_stage("INI配置生成")

    def export_buffers_only(self):
        """只导出 Buffer 文件，不生成 INI 配置"""
        TimerUtils.start_stage("缓冲文件生成")
        self.generate_buffer_files()
        TimerUtils.end_stage("缓冲文件生成")
