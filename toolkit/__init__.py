import bpy
from .model_operators import model_operators_list
from .ui_panel_toolkit import (
    ToolkitPanel, 
    VGToolsPanel,
    BMTP_MainPanel,
    BMTP_BoneControlPanel,
    BMTP_WeightControlPanel,
    BMTP_WeightOperationPanel,
    BMTP_WeightManagePanel,
    BMTP_ModelControlPanel,
    BMTP_MeshEditPanel,
    BMTP_UVToolsPanel,
    BMTP_SceneCleanPanel,
    BMTP_CollectionLinkerPanel,
    BMTP_ModifierToolsPanel,
    TT_MainPanel,
    TT_DDSConversionPanel,
    TT_ChannelCompositePanel,
    TT_ColorBakePanel,
    TT_AlphaExtractPanel,
    TT_MaterialToolsPanel,
    TT_LightmapPanel,
    TT_MaterialPreviewPanel,
)
from .vg_properties import vg_properties_list
from .vg_backup import vg_backup_operators
from .vg_create import vg_create_operators
from .vg_weight_adjust import vg_weight_adjust_operators

from .bmtp_properties import bmtp_properties_list
from .bmtp_bone_tools import bmtp_bone_tools_list
from .bmtp_weight_tools import bmtp_weight_tools_list
from .bmtp_clean_tools import bmtp_clean_tools_list
from .bmtp_material_tools import bmtp_material_tools_list
from .bmtp_collection_linker import bmtp_collection_linker_list
from .bmtp_mesh_tools import bmtp_mesh_tools_list
from .bmtp_modifier_tools import bmtp_modifier_tools_list
from .bmtp_uv_tools import bmtp_uv_tools_list

from .tt_properties import tt_properties_list
from .tt_dependency_check import tt_dependency_check_list
from .tt_dds_conversion import tt_dds_conversion_list
from .tt_normal_map import tt_normal_map_list
from .tt_color_bake import tt_color_bake_list
from .tt_alpha_extract import tt_alpha_extract_list
from .tt_lightmap import tt_lightmap_list
from .tt_material_tools import tt_material_tools_list
from .tt_material_preview import tt_material_preview_list

from .at_properties import at_properties_list
from .at_shape_key_control import at_shape_key_control_list
from .at_shape_key_operations import at_shape_key_operations_list
from .at_shape_key_creation import at_shape_key_creation_list
from .at_alembic_tools import at_alembic_tools_list
from .at_multi_frame_split import at_multi_frame_split_list
from .at_batch_export import at_batch_export_list
from .at_shape_key_export import at_shape_key_export_list
from .at_animation_export import at_animation_export_list
from .at_buffer_merge import at_buffer_merge_list
from .ui_panel_animation import (
    ui_panel_animation_list,
    ATP_PT_MainPanel,
    ATP_PT_ShapeKeyTools,
    ATP_PT_AlembicTools,
    ATP_PT_AnimationFrameSplit,
    ATP_PT_Automation,
    ATP_PT_ShapeKeyOperations,
    ATP_PT_ShapeKeyCreation,
    ATP_PT_ShapeKeyAnimationExport,
    ATP_PT_AutomationShapeKeyExport,
    ATP_PT_AutomationBufferMerge,
)

__all__ = [
    'ToolkitPanel',
    'VGToolsPanel',
    'BMTP_MainPanel',
    'BMTP_BoneControlPanel',
    'BMTP_WeightControlPanel',
    'BMTP_WeightOperationPanel',
    'BMTP_WeightManagePanel',
    'BMTP_ModelControlPanel',
    'BMTP_MeshEditPanel',
    'BMTP_UVToolsPanel',
    'BMTP_SceneCleanPanel',
    'BMTP_CollectionLinkerPanel',
    'BMTP_ModifierToolsPanel',
    'TT_MainPanel',
    'TT_DDSConversionPanel',
    'TT_ChannelCompositePanel',
    'TT_ColorBakePanel',
    'TT_AlphaExtractPanel',
    'TT_MaterialToolsPanel',
    'TT_LightmapPanel',
    'TT_MaterialPreviewPanel',
    'model_operators_list',
    'vg_properties_list',
    'vg_backup_operators',
    'vg_create_operators',
    'vg_weight_adjust_operators',
    'bmtp_properties_list',
    'bmtp_bone_tools_list',
    'bmtp_weight_tools_list',
    'bmtp_clean_tools_list',
    'bmtp_material_tools_list',
    'bmtp_collection_linker_list',
    'bmtp_mesh_tools_list',
    'bmtp_modifier_tools_list',
    'bmtp_uv_tools_list',
    'tt_properties_list',
    'tt_dependency_check_list',
    'tt_dds_conversion_list',
    'tt_normal_map_list',
    'tt_color_bake_list',
    'tt_alpha_extract_list',
    'tt_lightmap_list',
    'tt_material_tools_list',
    'tt_material_preview_list',
    'at_properties_list',
    'at_shape_key_control_list',
    'at_shape_key_operations_list',
    'at_shape_key_creation_list',
    'at_alembic_tools_list',
    'at_multi_frame_split_list',
    'at_batch_export_list',
    'at_shape_key_export_list',
    'at_animation_export_list',
    'at_buffer_merge_list',
    'ui_panel_animation_list',
    'register',
    'unregister',
]

def register():
    print("[TheHerta4] 注册工具集...")
    
    for op_class in vg_properties_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册属性: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册属性失败: {op_class.__name__} - {e}")
    
    bpy.types.Scene.vg_props = bpy.props.PointerProperty(type=vg_properties_list[-1])
    bpy.types.Object.vg_backups = bpy.props.CollectionProperty(type=vg_properties_list[0])
    bpy.types.Object.vg_backups_index = bpy.props.IntProperty(name="备份列表索引")
    
    for op_class in model_operators_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册失败: {op_class.__name__} - {e}")
    
    for op_class in vg_backup_operators:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册失败: {op_class.__name__} - {e}")
    
    for op_class in vg_create_operators:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册失败: {op_class.__name__} - {e}")
    
    for op_class in vg_weight_adjust_operators:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_properties_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP属性: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP属性失败: {op_class.__name__} - {e}")
    
    bpy.types.Scene.bmtp_props = bpy.props.PointerProperty(type=bmtp_properties_list[2])
    
    for op_class in bmtp_bone_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP骨骼工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP骨骼工具失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_weight_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP权重工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP权重工具失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_clean_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP清理工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP清理工具失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_material_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP材质工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP材质工具失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_collection_linker_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP集合关联工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP集合关联工具失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_mesh_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP网格工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP网格工具失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_modifier_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP修改器工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP修改器工具失败: {op_class.__name__} - {e}")
    
    for op_class in bmtp_uv_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册BMTP UV工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册BMTP UV工具失败: {op_class.__name__} - {e}")
    
    for op_class in tt_properties_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT属性: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT属性失败: {op_class.__name__} - {e}")
    
    bpy.utils.register_class(tt_material_preview_list[0])
    print(f"[TheHerta4]   已注册TT材质预览项: {tt_material_preview_list[0].__name__}")
    
    bpy.types.Scene.texture_tools_props = bpy.props.PointerProperty(type=tt_properties_list[-1])
    bpy.types.Scene.material_preview_list = bpy.props.CollectionProperty(type=tt_material_preview_list[0])
    
    for op_class in tt_dependency_check_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT依赖检查: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT依赖检查失败: {op_class.__name__} - {e}")
    
    for op_class in tt_dds_conversion_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT DDS转换: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT DDS转换失败: {op_class.__name__} - {e}")
    
    for op_class in tt_normal_map_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT法线贴图: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT法线贴图失败: {op_class.__name__} - {e}")
    
    for op_class in tt_color_bake_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT颜色烘焙: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT颜色烘焙失败: {op_class.__name__} - {e}")
    
    for op_class in tt_alpha_extract_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT Alpha提取: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT Alpha提取失败: {op_class.__name__} - {e}")
    
    for op_class in tt_lightmap_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT光照贴图: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT光照贴图失败: {op_class.__name__} - {e}")
    
    for op_class in tt_material_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT材质工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT材质工具失败: {op_class.__name__} - {e}")
    
    for op_class in tt_material_preview_list[1:]:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册TT材质预览: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册TT材质预览失败: {op_class.__name__} - {e}")
    
    print("[TheHerta4] 注册动画处理工具...")
    for op_class in at_properties_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT属性: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT属性失败: {op_class.__name__} - {e}")
    
    bpy.types.Scene.atp_props = bpy.props.PointerProperty(type=at_properties_list[-1])
    
    for op_class in at_shape_key_control_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT形态键控制: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT形态键控制失败: {op_class.__name__} - {e}")
    
    for op_class in at_shape_key_operations_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT形态键操作: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT形态键操作失败: {op_class.__name__} - {e}")
    
    for op_class in at_shape_key_creation_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT形态键创建: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT形态键创建失败: {op_class.__name__} - {e}")
    
    for op_class in at_alembic_tools_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT Alembic工具: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT Alembic工具失败: {op_class.__name__} - {e}")
    
    for op_class in at_multi_frame_split_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT多帧拆分: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT多帧拆分失败: {op_class.__name__} - {e}")
    
    for op_class in at_batch_export_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT批量导出: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT批量导出失败: {op_class.__name__} - {e}")
    
    for op_class in at_shape_key_export_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT形态键导出: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT形态键导出失败: {op_class.__name__} - {e}")
    
    for op_class in at_animation_export_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT动画导出: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT动画导出失败: {op_class.__name__} - {e}")
    
    for op_class in at_buffer_merge_list:
        try:
            bpy.utils.register_class(op_class)
            print(f"[TheHerta4]   已注册AT缓冲合并: {op_class.__name__}")
        except Exception as e:
            print(f"[TheHerta4]   注册AT缓冲合并失败: {op_class.__name__} - {e}")
    
    bpy.utils.register_class(ToolkitPanel)
    bpy.utils.register_class(VGToolsPanel)
    bpy.utils.register_class(BMTP_MainPanel)
    bpy.utils.register_class(BMTP_BoneControlPanel)
    bpy.utils.register_class(BMTP_WeightControlPanel)
    bpy.utils.register_class(BMTP_WeightOperationPanel)
    bpy.utils.register_class(BMTP_WeightManagePanel)
    bpy.utils.register_class(BMTP_ModelControlPanel)
    bpy.utils.register_class(BMTP_MeshEditPanel)
    bpy.utils.register_class(BMTP_UVToolsPanel)
    bpy.utils.register_class(BMTP_SceneCleanPanel)
    bpy.utils.register_class(BMTP_CollectionLinkerPanel)
    bpy.utils.register_class(BMTP_ModifierToolsPanel)
    bpy.utils.register_class(TT_MainPanel)
    bpy.utils.register_class(TT_DDSConversionPanel)
    bpy.utils.register_class(TT_ChannelCompositePanel)
    bpy.utils.register_class(TT_ColorBakePanel)
    bpy.utils.register_class(TT_AlphaExtractPanel)
    bpy.utils.register_class(TT_MaterialToolsPanel)
    bpy.utils.register_class(TT_LightmapPanel)
    bpy.utils.register_class(TT_MaterialPreviewPanel)
    
    bpy.utils.register_class(ATP_PT_MainPanel)
    bpy.utils.register_class(ATP_PT_ShapeKeyTools)
    bpy.utils.register_class(ATP_PT_AlembicTools)
    bpy.utils.register_class(ATP_PT_AnimationFrameSplit)
    bpy.utils.register_class(ATP_PT_Automation)
    bpy.utils.register_class(ATP_PT_ShapeKeyOperations)
    bpy.utils.register_class(ATP_PT_ShapeKeyCreation)
    bpy.utils.register_class(ATP_PT_ShapeKeyAnimationExport)
    bpy.utils.register_class(ATP_PT_AutomationShapeKeyExport)
    bpy.utils.register_class(ATP_PT_AutomationBufferMerge)
    
    print("[TheHerta4] 工具集注册完成")

def unregister():
    bpy.utils.unregister_class(ATP_PT_AutomationBufferMerge)
    bpy.utils.unregister_class(ATP_PT_AutomationShapeKeyExport)
    bpy.utils.unregister_class(ATP_PT_ShapeKeyAnimationExport)
    bpy.utils.unregister_class(ATP_PT_ShapeKeyCreation)
    bpy.utils.unregister_class(ATP_PT_ShapeKeyOperations)
    bpy.utils.unregister_class(ATP_PT_Automation)
    bpy.utils.unregister_class(ATP_PT_AnimationFrameSplit)
    bpy.utils.unregister_class(ATP_PT_AlembicTools)
    bpy.utils.unregister_class(ATP_PT_ShapeKeyTools)
    bpy.utils.unregister_class(ATP_PT_MainPanel)
    
    bpy.utils.unregister_class(TT_MaterialPreviewPanel)
    bpy.utils.unregister_class(TT_LightmapPanel)
    bpy.utils.unregister_class(TT_MaterialToolsPanel)
    bpy.utils.unregister_class(TT_AlphaExtractPanel)
    bpy.utils.unregister_class(TT_ColorBakePanel)
    bpy.utils.unregister_class(TT_ChannelCompositePanel)
    bpy.utils.unregister_class(TT_DDSConversionPanel)
    bpy.utils.unregister_class(TT_MainPanel)
    bpy.utils.unregister_class(BMTP_ModifierToolsPanel)
    bpy.utils.unregister_class(BMTP_CollectionLinkerPanel)
    bpy.utils.unregister_class(BMTP_SceneCleanPanel)
    bpy.utils.unregister_class(BMTP_UVToolsPanel)
    bpy.utils.unregister_class(BMTP_MeshEditPanel)
    bpy.utils.unregister_class(BMTP_ModelControlPanel)
    bpy.utils.unregister_class(BMTP_WeightManagePanel)
    bpy.utils.unregister_class(BMTP_WeightOperationPanel)
    bpy.utils.unregister_class(BMTP_WeightControlPanel)
    bpy.utils.unregister_class(BMTP_BoneControlPanel)
    bpy.utils.unregister_class(BMTP_MainPanel)
    bpy.utils.unregister_class(VGToolsPanel)
    bpy.utils.unregister_class(ToolkitPanel)
    
    for op_class in reversed(at_buffer_merge_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_animation_export_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_shape_key_export_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_batch_export_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_multi_frame_split_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_alembic_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_shape_key_creation_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_shape_key_operations_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(at_shape_key_control_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    if hasattr(bpy.types.Scene, 'atp_props'):
        del bpy.types.Scene.atp_props
    
    for op_class in reversed(at_properties_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_material_preview_list[1:]):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_material_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_lightmap_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_alpha_extract_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_color_bake_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_normal_map_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_dds_conversion_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(tt_dependency_check_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    if hasattr(bpy.types.Scene, 'material_preview_list'):
        del bpy.types.Scene.material_preview_list
    if hasattr(bpy.types.Scene, 'texture_tools_props'):
        del bpy.types.Scene.texture_tools_props
    
    bpy.utils.unregister_class(tt_material_preview_list[0])
    
    for op_class in reversed(tt_properties_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_uv_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_modifier_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_mesh_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_collection_linker_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_material_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_clean_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_weight_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(bmtp_bone_tools_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    if hasattr(bpy.types.Scene, 'bmtp_props'):
        del bpy.types.Scene.bmtp_props
    
    for op_class in reversed(bmtp_properties_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(vg_weight_adjust_operators):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(vg_create_operators):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(vg_backup_operators):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    for op_class in reversed(model_operators_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
    
    if hasattr(bpy.types.Object, 'vg_backups_index'):
        del bpy.types.Object.vg_backups_index
    if hasattr(bpy.types.Object, 'vg_backups'):
        del bpy.types.Object.vg_backups
    if hasattr(bpy.types.Scene, 'vg_props'):
        del bpy.types.Scene.vg_props
    
    for op_class in reversed(vg_properties_list):
        try:
            bpy.utils.unregister_class(op_class)
        except Exception:
            pass
