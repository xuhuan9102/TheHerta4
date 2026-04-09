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
        """计算文本所需的宽度（估算值）
        
        根据字符类型估算宽度：
        - 宽字符（中文、日文、韩文、全角符号等）：宽度为 2
        - 窄字符（英文、数字、半角符号等）：宽度为 1
        
        Args:
            text: 要计算宽度的文本
            padding: 额外的边距宽度
            
        Returns:
            int: 估算的文本宽度（最小 200）
        """
        if not text:
            return 200
        
        char_count = 0
        for char in text:
            code = ord(char)
            
            # 宽字符判断
            is_wide = False
            
            # CJK 基本汉字: U+4E00 - U+9FFF
            if 0x4E00 <= code <= 0x9FFF:
                is_wide = True
            # CJK 扩展A: U+3400 - U+4DBF
            elif 0x3400 <= code <= 0x4DBF:
                is_wide = True
            # CJK 扩展B: U+20000 - U+2A6DF
            elif 0x20000 <= code <= 0x2A6DF:
                is_wide = True
            # CJK 扩展C: U+2A700 - U+2B73F
            elif 0x2A700 <= code <= 0x2B73F:
                is_wide = True
            # CJK 扩展D: U+2B740 - U+2B81F
            elif 0x2B740 <= code <= 0x2B81F:
                is_wide = True
            # CJK 扩展E: U+2B820 - U+2CEAF
            elif 0x2B820 <= code <= 0x2CEAF:
                is_wide = True
            # CJK 扩展F: U+2CEB0 - U+2EBEF
            elif 0x2CEB0 <= code <= 0x2EBEF:
                is_wide = True
            # CJK 扩展G: U+30000 - U+3134F
            elif 0x30000 <= code <= 0x3134F:
                is_wide = True
            # CJK 兼容汉字: U+F900 - U+FAFF
            elif 0xF900 <= code <= 0xFAFF:
                is_wide = True
            # CJK 统一表意文字扩展: U+2E80 - U+2EFF
            elif 0x2E80 <= code <= 0x2EFF:
                is_wide = True
            # 日文平假名: U+3040 - U+309F
            elif 0x3040 <= code <= 0x309F:
                is_wide = True
            # 日文片假名: U+30A0 - U+30FF
            elif 0x30A0 <= code <= 0x30FF:
                is_wide = True
            # 日文半角片假名: U+FF65 - U+FF9F (这些是半角，但通常显示为窄)
            # 韩文音节: U+AC00 - U+D7AF
            elif 0xAC00 <= code <= 0xD7AF:
                is_wide = True
            # 韩文字母 (Jamo): U+1100 - U+11FF
            elif 0x1100 <= code <= 0x11FF:
                is_wide = True
            # 韩文兼容字母: U+3130 - U+318F
            elif 0x3130 <= code <= 0x318F:
                is_wide = True
            # 全角符号 (Fullwidth): U+FF00 - U+FFEF
            elif 0xFF00 <= code <= 0xFFEF:
                is_wide = True
            # 中文标点符号: U+3000 - U+303F
            elif 0x3000 <= code <= 0x303F:
                is_wide = True
            # 箭头符号: U+2190 - U+21FF
            elif 0x2190 <= code <= 0x21FF:
                is_wide = True
            # 数学运算符: U+2200 - U+22FF
            elif 0x2200 <= code <= 0x22FF:
                is_wide = True
            # 制表符和特殊符号: U+2500 - U+257F
            elif 0x2500 <= code <= 0x257F:
                is_wide = True
            # 几何图形符号: U+25A0 - U+25FF
            elif 0x25A0 <= code <= 0x25FF:
                is_wide = True
            # 杂项符号: U+2600 - U+26FF
            elif 0x2600 <= code <= 0x26FF:
                is_wide = True
            # 表情符号范围 (部分常用): U+1F300 - U+1F9FF
            elif 0x1F300 <= code <= 0x1F9FF:
                is_wide = True
            
            char_count += 2 if is_wide else 1
        
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
