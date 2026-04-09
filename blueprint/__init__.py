from .node_base import SSMTBlueprintTree, SSMTNodeBase, SSMTSocketObject, SSMTSocketPostProcess, THEHERTA3_OT_OpenPersistentBlueprint
from .node_obj import SSMTNode_Object_Info, SSMTNode_Object_Group, SSMTNode_Result_Output
from .node_shapekey import SSMTNode_ShapeKey, SSMTNode_ShapeKey_Output
from .model import BluePrintModel
from .export_helper import BlueprintExportHelper
from .sync import (
    SSMT_OT_ToggleSync,
    SSMT_OT_SyncSelectedNodeToObject,
    SSMT_OT_SyncSelectedObjectToNode,
    SSMT_OT_UpdateAllNodeReferences,
    SSMT_OT_SelectObjectFromNode,
    SSMT_OT_SelectNodeFromObject,
)

# 检查物体切换节点模块是否可用
try:
    from .node_swap import SSMTNode_ObjectSwap
    HAS_OBJECT_SWAP = True
except ImportError:
    HAS_OBJECT_SWAP = False

# 检查重命名节点模块是否可用
try:
    from .node_rename import SSMTNode_Object_Rename
    HAS_OBJECT_RENAME = True
except ImportError:
    HAS_OBJECT_RENAME = False

def register():
    from . import node_base
    from . import node_obj
    from . import node_menu
    from . import node_shapekey
    from . import sync
    
    node_base.register()
    node_obj.register()
    node_menu.register()
    node_shapekey.register()
    
    # 物体切换节点 - 可选模块
    if HAS_OBJECT_SWAP:
        from . import node_swap
        node_swap.register()
    
    # 重命名节点 - 可选模块
    if HAS_OBJECT_RENAME:
        from . import node_rename
        node_rename.register()
    
    sync.register()

def unregister():
    from . import sync
    
    # 重命名节点 - 可选模块
    if HAS_OBJECT_RENAME:
        from . import node_rename
        node_rename.unregister()
    
    # 物体切换节点 - 可选模块
    if HAS_OBJECT_SWAP:
        from . import node_swap
        node_swap.unregister()
    
    from . import node_shapekey
    from . import node_menu
    from . import node_obj
    from . import node_base
    
    sync.unregister()
    node_shapekey.unregister()
    node_menu.unregister()
    node_obj.unregister()
    node_base.unregister()
