'''
存放一些构建SSMT蓝图架构的基础节点
每种节点放在单独的py文件中
方便阅读理解
'''
import bpy
from bpy.types import NodeTree, Node, NodeSocket

from ..utils.translate_utils import TR
from ..common.global_config import GlobalConfig



class SSMTSocketObject(NodeSocket):
    '''Custom Socket for Object Data'''
    bl_idname = 'SSMTSocketObject'
    bl_label = 'Object Socket'

    def draw_color(self, context, node):
        return (0.0, 0.8, 0.8, 1.0)

    def draw(self, context, layout, node, text):
        layout.label(text=text)

class SSMTSocketPostProcess(NodeSocket):
    '''Custom Socket for Post Process Path'''
    bl_idname = 'SSMTSocketPostProcess'
    bl_label = 'Post Process Socket'

    def draw_color(self, context, node):
        return (1.0, 0.5, 0.0, 1.0)

    def draw(self, context, layout, node, text):
        layout.label(text=text)

class SSMTBlueprintTree(NodeTree):
    '''SSMT Mod Logic Blueprint'''
    bl_idname = 'SSMTBlueprintTreeType'
    bl_label = 'SSMT BluePrint'
    bl_icon = 'NODETREE'


class SSMTNodeBase(Node):
    @classmethod
    def poll(cls, ntree):
        return ntree.bl_idname == 'SSMTBlueprintTreeType'
    
    def calculate_text_width(self, text, padding=40):
        """计算文本所需的宽度（估算值）"""
        if not text:
            return 200
        
        char_count = 0
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                char_count += 2
            else:
                char_count += 1
        
        width = char_count * 8 + padding
        
        return max(200, width)
    
    def update_node_width(self, texts):
        """根据文本内容更新节点宽度"""
        if not texts:
            return
        
        max_width = 200
        for text in texts:
            width = self.calculate_text_width(text)
            if width > max_width:
                max_width = width
        
        self.width = max_width
    

class THEHERTA3_OT_OpenPersistentBlueprint(bpy.types.Operator):
    bl_idname = "theherta3.open_persistent_blueprint"
    bl_label = TR.translate("打开蓝图界面")
    bl_description = TR.translate("打开一个独立的蓝图窗口，用于配置Mod逻辑")
    
    def execute(self, context):
        GlobalConfig.read_from_main_json_ssmt4()
        tree_name = GlobalConfig.workspacename
        
        tree = bpy.data.node_groups.get(tree_name)
        if not tree:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
            tree.use_fake_user = True
        
        target_window = None
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'NODE_EDITOR':
                    for space in area.spaces:
                        if space.type == 'NODE_EDITOR' and space.node_tree == tree:
                            target_window = window
                            break
                if target_window: break
            if target_window: break
            
        if target_window:
            if len(context.window_manager.windows) > 1:
                try:
                    if hasattr(context, 'temp_override'):
                        with context.temp_override(window=target_window):
                            bpy.ops.wm.window_close()
                    else:
                        override = context.copy()
                        override['window'] = target_window
                        override['screen'] = target_window.screen
                        bpy.ops.wm.window_close(override)
                except Exception as e:
                    print(f"SSMT: Failed to close existing window, creating new one anyway. Error: {e}")

        old_windows = set(context.window_manager.windows)
        
        bpy.ops.wm.window_new()
        
        new_windows = set(context.window_manager.windows)
        created_window = (new_windows - old_windows).pop() if (new_windows - old_windows) else None
        
        if created_window:
            screen = created_window.screen
            
            target_area = max(screen.areas, key=lambda a: a.width * a.height)
            
            if target_area:
                target_area.ui_type = 'SSMTBlueprintTreeType'
                target_area.type = 'NODE_EDITOR'
                
                for space in target_area.spaces:
                    if space.type == 'NODE_EDITOR':
                        space.tree_type = 'SSMTBlueprintTreeType'
                        space.node_tree = tree
                        space.pin = True
                        
        return {'FINISHED'}
    
def register():
    bpy.utils.register_class(SSMTBlueprintTree)
    bpy.utils.register_class(SSMTSocketObject)
    bpy.utils.register_class(SSMTSocketPostProcess)
    bpy.utils.register_class(THEHERTA3_OT_OpenPersistentBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMTSocketPostProcess)
    bpy.utils.unregister_class(SSMTSocketObject)
    bpy.utils.unregister_class(THEHERTA3_OT_OpenPersistentBlueprint)
    bpy.utils.unregister_class(SSMTBlueprintTree)
