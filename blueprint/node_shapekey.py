
import bpy

from .node_base import SSMTNodeBase


class SSMTNode_ShapeKey(SSMTNodeBase):
    '''ShapeKey Node'''
    bl_idname = 'SSMTNode_ShapeKey'
    bl_label = 'Shape Key'
    bl_icon = 'SHAPEKEY_DATA'

    def update_shapekey_name(self, context):
        self.update_node_width([self.shapekey_name, self.key, self.comment])
    
    def update_key(self, context):
        self.update_node_width([self.shapekey_name, self.key, self.comment])
    
    def update_comment(self, context):
        self.update_node_width([self.shapekey_name, self.key, self.comment])
    
    shapekey_name: bpy.props.StringProperty(name="ShapeKey Name", default="", update=update_shapekey_name)
    key: bpy.props.StringProperty(name="Key", default="", update=update_key)
    comment: bpy.props.StringProperty(name="备注", description="备注信息，会以注释形式生成到配置表中", default="", update=update_comment)
    
    def init(self, context):
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 200

    def draw_buttons(self, context, layout):
        layout.prop(self, "shapekey_name", text="Name")

        row = layout.row(align=True)
        row.prop(self, "key", text="Key")
        row.operator("wm.url_open", text="", icon='HELP').url = "https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes"

        layout.prop(self, "comment", text="备注")

    @staticmethod
    def generate_debug_detail(shapekey_params, keyname_mkey_dict):
        """
        生成形态键参数详情（供蓝图解析引擎调用）

        Args:
            shapekey_params: 处理链中收集的形态键参数列表
            keyname_mkey_dict: 已注册的形态键字典

        Returns:
            list[str]: 调试文本行列表
        """
        lines = []
        lines.append(f"\n形态键参数:")
        for j, sk in enumerate(shapekey_params, 1):
            lines.append(f"  [{j}] {sk.key_name}")
            lines.append(f"       快捷键: {sk.initialize_vk_str or '未设置'}")
            lines.append(f"       注释: {sk.comment or '无'}")

            if sk.key_name in keyname_mkey_dict:
                lines.append(f"       状态: ✓ 已注册")
            else:
                lines.append(f"       状态: ○ 待注册")
        return lines


class SSMTNode_ShapeKey_Output(SSMTNodeBase):
    '''ShapeKey Output Node'''
    bl_idname = 'SSMTNode_ShapeKey_Output'
    bl_label = 'Generate ShapeKey Buffer'
    bl_icon = 'EXPORT'

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Group 1")
        self.width = 200

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Group {len(self.inputs) + 1}")
        
        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
             self.inputs.remove(self.inputs[-1])


classes = (
    SSMTNode_ShapeKey,
    SSMTNode_ShapeKey_Output,
    
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
