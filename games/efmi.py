from ..common.export.blueprint_model import BluePrintModel
from ..common.export.draw_call_model import DrawCallModel, M_DrawIndexedInstanced
from ..common.export.submesh_model import SubMeshModel
from dataclasses import dataclass,field
from ..base.config.main_config import GlobalConfig

from ..helper.buffer_export_helper import BufferExportHelper
from ..common.migoto.m_ini_builder import M_IniBuilder,M_IniSection, M_SectionType

import os

@dataclass
class ExportEFMI:

    blueprint_model:BluePrintModel

    submesh_model_list:list[SubMeshModel] = field(default_factory=list,init=False)

    def __post_init__(self):
        self.initialize_submesh_model_list()
        
    def initialize_submesh_model_list(self):
        # 根据唯一标识符，把相同的DrawCallModel分在一起，形成SubMeshModel
        draw_call_model_dict:dict[str,list[DrawCallModel]] = {}

        # 拿到BlueprintModel后，开始解析SubMeshModel列表
        for draw_call_model in self.blueprint_model.ordered_draw_obj_data_model_list:
            # 获取独立标识
            unique_str = draw_call_model.get_unique_str()
            print("ExportEFMI: 解析DrawCallModel，Obj名称: " + draw_call_model.obj_name + " Unique标识: " + unique_str)

            # 根据unique_str，加入到字典中，这样每个unique_str都对应一个DrawCallModel列表，用于初始化SubMeshModel
            draw_call_model_list = draw_call_model_dict.get(unique_str,[])
            draw_call_model_list.append(draw_call_model)
            draw_call_model_dict[unique_str] = draw_call_model_list

        # 根据draw_call_model_dict，初始化SubMeshModel列表
        for unique_str, draw_call_model_list in draw_call_model_dict.items():
            submesh_model = SubMeshModel(drawcall_model_list=draw_call_model_list)
            self.submesh_model_list.append(submesh_model)
        
        print("ExportEFMI: SubMeshModel列表初始化完成，共有 " + str(len(self.submesh_model_list)) + " 个SubMeshModel")

    def generate_buffer_files(self):
        buf_output_folder = GlobalConfig.path_generatemod_buffer_folder()

        # 新版EFMI只需要依次导出每个SubMeshModel的内容，甚至无需合并，非常简单
        for submesh_model in self.submesh_model_list:
            print("ExportEFMI: 导出SubMeshModel，Unique标识: " + submesh_model.unique_str)

            # 生成IndexBuffer
            ib_filename = submesh_model.unique_str + "-Index.buf"
            ib_filepath = os.path.join(buf_output_folder, ib_filename)
            BufferExportHelper.write_buf_ib_r32_uint(submesh_model.ib, ib_filepath)

            # 生成CategoryBuffer
            for category, category_buf in submesh_model.category_buffer_dict.items():
                category_buf_filename = submesh_model.unique_str + "-" + category + ".buf"
                category_buf_filepath = os.path.join(buf_output_folder, category_buf_filename)
                with open(category_buf_filepath, 'wb') as f:
                    category_buf.tofile(f)

    def generate_ini_file(self):
        ini_builder = M_IniBuilder()

        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)

        for submesh_model in self.submesh_model_list:
            
            texture_override_ib_section.append("[TextureOverride_" + submesh_model.unique_str.replace("-","_") + "]")
            texture_override_ib_section.append("hash = " + submesh_model.match_draw_ib)
            texture_override_ib_section.append("match_first_index = " + submesh_model.match_first_index)
            texture_override_ib_section.append("match_index_count = " + submesh_model.match_index_count)
            texture_override_ib_section.append("handling = skip")

            texture_override_ib_section.append("run = CommandList\\EFMIv1\\OverrideTextures")

            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            texture_override_ib_section.append("ib = " + ib_resource_name)

            for category in submesh_model.category_buffer_dict.keys():
                category_slot = submesh_model.d3d11_game_type.CategoryExtractSlotDict.get(category,"unknown_slot")
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                texture_override_ib_section.append(category_slot + " = " + category_resource_name)

            for drawcall_model in submesh_model.drawcall_model_list:
                drawindexed_instanced = M_DrawIndexedInstanced()
                drawindexed_instanced.IndexCountPerInstance = drawcall_model.index_count
                drawindexed_instanced.StartIndexLocation = drawcall_model.index_offset
                texture_override_ib_section.append("; " + drawcall_model.comment_alias_name)
                texture_override_ib_section.append(drawindexed_instanced.get_draw_str())
            
            texture_override_ib_section.new_line()

        ini_builder.append_section(texture_override_ib_section)

        # ResourceBuffer部分
        resource_buffer_section = M_IniSection(M_SectionType.ResourceBuffer)
        for submesh_model in self.submesh_model_list:
            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            resource_buffer_section.append("[" + ib_resource_name + "]")
            resource_buffer_section.append("type = Buffer")
            resource_buffer_section.append("format = DXGI_FORMAT_R32_UINT")
            resource_buffer_section.append("filename = Buffer\\" + submesh_model.unique_str + "-Index.buf")
            resource_buffer_section.new_line()
            
            for category in submesh_model.category_buffer_dict.keys():
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                stride = submesh_model.d3d11_game_type.CategoryStrideDict.get(category,0)
                resource_buffer_section.append("[" + category_resource_name + "]")
                resource_buffer_section.append("type = Buffer")
                resource_buffer_section.append("stride = " + str(stride))
                resource_buffer_section.append("filename = Buffer\\" + submesh_model.unique_str + "-" + category + ".buf")
                resource_buffer_section.new_line()
        ini_builder.append_section(resource_buffer_section)

        
        ini_filepath = os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.workspacename + ".ini")
        ini_builder.save_to_file(ini_filepath)
                


        

    def export(self):
        self.generate_buffer_files()
        self.generate_ini_file()

        
            
        
