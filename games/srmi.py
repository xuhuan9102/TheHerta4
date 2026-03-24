from ..common.export.blueprint_model import BluePrintModel
from ..common.export.draw_call_model import DrawCallModel, M_DrawIndexedInstanced
from ..common.export.submesh_model import SubMeshModel
from dataclasses import dataclass,field
from ..base.config.main_config import GlobalConfig

from ..helper.buffer_export_helper import BufferExportHelper
from ..common.migoto.m_ini_builder import M_IniBuilder,M_IniSection, M_SectionType

import os

@dataclass
class ExportSRMI:

    blueprint_model:BluePrintModel

    submesh_model_list:list[SubMeshModel] = field(default_factory=list,init=False)


        
            
        
