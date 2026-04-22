try:
    from .node_base import SSMTBlueprintTree, SSMTNodeBase, SSMTSocketObject, SSMTSocketPostProcess, THEHERTA3_OT_OpenPersistentBlueprint
except ImportError:
    pass
try:
    from .node_obj import SSMTNode_Object_Info, SSMTNode_Object_Group, SSMTNode_Result_Output
except ImportError:
    pass
try:
    from .node_shapekey import SSMTNode_ShapeKey, SSMTNode_ShapeKey_Output
except ImportError:
    pass
try:
    from .model import BluePrintModel
except ImportError:
    pass
try:
    from .preprocess_cache import PreProcessCache
except ImportError:
    pass
try:
    from .preprocess_parallel import ParallelPreprocessCoordinator
except ImportError:
    pass
try:
    from .export_parallel import ParallelExportCoordinator
except ImportError:
    pass
try:
    from .export_helper import BlueprintExportHelper
except ImportError:
    pass
try:
    from .sync import (
        SSMT_OT_ToggleSync,
        SSMT_OT_SyncSelectedNodeToObject,
        SSMT_OT_SyncSelectedObjectToNode,
        SSMT_OT_UpdateAllNodeReferences,
        SSMT_OT_SelectObjectFromNode,
        SSMT_OT_SelectNodeFromObject,
        SSMT_OT_SyncDebugStatus,
    )
except ImportError:
    pass

_MODULE_REGISTRY = [
    {"name": "node_base", "required": True},
    {"name": "node_obj", "required": True},
    {"name": "node_shapekey", "required": True},
    {"name": "model", "required": True},
    {"name": "export_helper", "required": True},
    {"name": "preprocess_cache", "required": True},
    {"name": "preprocess_parallel", "required": True},
    {"name": "export_parallel", "required": True},
    {"name": "sync", "required": True},
    {"name": "shader_quick_connect", "required": False},
    {"name": "node_menu", "required": True},
    {"name": "node_preset", "required": True},
    {"name": "node_datatype", "required": False},
    {"name": "node_swap", "required": False},
    {"name": "node_rename", "required": False},
    {"name": "node_swap_ini", "required": False},
    {"name": "node_swap_processor", "required": False},
    {"name": "node_vertex_group_match", "required": False},
    {"name": "node_vertex_group_process", "required": False},
    {"name": "node_vertex_group_mapping_input", "required": False},
    {"name": "nest_navigate", "required": False},
    {"name": "node_nest", "required": False},
    {"name": "node_cross_ib", "required": False},
    {"name": "node_multifile_export", "required": False},
    {"name": "node_bone_palette_export", "required": False},
    {"name": "node_postprocess_base", "required": False},
    {"name": "node_postprocess_vertex_attrs", "required": False},
    {"name": "node_postprocess_shapekey", "required": False},
    {"name": "node_postprocess_material", "required": False},
    {"name": "node_postprocess_health", "required": False},
    {"name": "node_postprocess_slider", "required": False},
    {"name": "node_postprocess_resource_merge", "required": False},
    {"name": "node_postprocess_buffer_cleanup", "required": False},
    {"name": "node_postprocess_multifile", "required": False},
]

_MODULE_AVAILABLE = {}
_registered_modules = []

for _entry in _MODULE_REGISTRY:
    _MODULE_AVAILABLE[_entry["name"]] = False

for _entry in _MODULE_REGISTRY:
    if not _entry["required"]:
        try:
            __import__(f"{__name__}.{_entry['name']}", fromlist=[_entry["name"]])
            _MODULE_AVAILABLE[_entry["name"]] = True
        except ImportError:
            pass

def register():
    global _registered_modules
    _registered_modules = []
    for entry in _MODULE_REGISTRY:
        mod_name = entry["name"]
        required = entry["required"]
        try:
            mod = __import__(f"{__name__}.{mod_name}", fromlist=[mod_name])
            mod.register()
            _registered_modules.append(mod_name)
        except ImportError:
            if required:
                raise
        except Exception as e:
            if required:
                raise
            print(f"Warning: optional module '{mod_name}' failed to register: {e}")

def unregister():
    for mod_name in reversed(_registered_modules):
        try:
            mod = __import__(f"{__name__}.{mod_name}", fromlist=[mod_name])
            mod.unregister()
        except Exception as e:
            print(f"Warning: module '{mod_name}' failed to unregister: {e}")
