"""按蓝图连接路径配置 ZZMI 的 TTL 透明绘制。"""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty

from .node_base import SSMTNodeBase


class SSMTNode_Transparency(SSMTNodeBase):
    bl_idname = 'SSMTNode_Transparency'
    bl_label = '透明贴图'
    bl_icon = 'MATERIAL'
    bl_description = '仅将本节点连接的物体绘制替换为 TTL 透明绘制，支持切换与嵌套蓝图；存在本节点时「材质转资源」节点不再生成 TTL 段'
    bl_width_min = 260

    mode: EnumProperty(
        name="透明来源",
        items=(
            ('TEXTURE', "所属 IB 的 Diffuse 贴图", "使用每个物体所属 IB 的 Diffuse 资源作为透明遮罩"),
            ('ALPHA', "全局 Alpha", "使用下方 Alpha 数值作为透明度，不引用透明贴图"),
        ),
        default='TEXTURE',
    )
    mask_channel: EnumProperty(
        name="读取通道",
        items=(
            ('0', "R（0）", "读取红色通道"),
            ('1', "G（1）", "读取绿色通道"),
            ('2', "B（2）", "读取蓝色通道"),
            ('3', "A（3）", "读取 Alpha 通道"),
        ),
        default='3',
    )
    mask_invert: BoolProperty(name="反转通道", default=True)
    alpha: FloatProperty(name="Alpha", default=0.5, min=0.0, max=1.0)
    brightness: FloatProperty(name="亮度", default=1.5, min=0.0, soft_max=5.0)

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input 1")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 280

    def update(self):
        # 与图内包装节点使用相同的动态插口约定，始终留一个空输入。
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Input {len(self.inputs) + 1}")
        while (len(self.inputs) > 1 and not self.inputs[-1].is_linked
               and not self.inputs[-2].is_linked):
            self.inputs.remove(self.inputs[-1])

    def draw_buttons(self, context, layout):
        layout.prop(self, "mode", text="")
        if self.mode == 'TEXTURE':
            layout.prop(self, "mask_channel")
            layout.prop(self, "mask_invert")
            layout.label(text="自动引用各物体所属 IB 的 Diffuse", icon='TEXTURE')
        else:
            layout.prop(self, "alpha", slider=True)
        layout.prop(self, "brightness")
        layout.label(text="仅在 ZZMI 模式生成 TTL 绘制", icon='INFO')

    def get_transparency_info(self):
        """每条处理链拿独立快照，避免多节点、分支与重复导出串参数。"""
        return {
            'mode': self.mode,
            'mask_channel': int(self.mask_channel),
            'mask_invert': bool(self.mask_invert),
            'alpha': float(self.alpha),
            'brightness': float(self.brightness),
        }


def register():
    bpy.utils.register_class(SSMTNode_Transparency)


def unregister():
    bpy.utils.unregister_class(SSMTNode_Transparency)
