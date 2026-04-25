import bpy
from bpy.types import Node, NodeSocket
from bpy.props import StringProperty, EnumProperty, PointerProperty

from .node_base import SSMTNodeBase


class SSMTNode_Blueprint_Nest(SSMTNodeBase):
    '''Blueprint Nest Node - 嵌套外部蓝图到当前蓝图'''
    bl_idname = 'SSMTNode_Blueprint_Nest'
    bl_label = 'Blueprint Nest'
    bl_icon = 'NODETREE'
    bl_width_min = 300

    _NODE_TYPE_LABELS = {
        'SSMTNode_Object_Info': '物体信息',
        'SSMTNode_Object_Group': '物体组',
        'SSMTNode_Result_Output': '输出',
        'SSMTNode_ShapeKey': '形态键',
        'SSMTNode_ShapeKey_Output': '形态键输出',
        'SSMTNode_Object_Rename': '重命名',
        'SSMTNode_ObjectSwap': '物体切换',
        'SSMTNode_DataType': '数据类型',
        'SSMTNode_VertexGroupMatch': '顶点组匹配',
        'SSMTNode_VertexGroupProcess': '顶点组处理',
        'SSMTNode_VertexGroupMappingInput': '映射表输入',
        'SSMTNode_Blueprint_Nest': '嵌套蓝图',
        'SSMTNode_CrossIB': '跨IB',
        'SSMTNode_MultiFile_Export': '多文件导出',
        'SSMTNode_PostProcess_VertexAttrs': '顶点属性',
        'SSMTNode_PostProcess_ShapeKey': '形态键配置',
        'SSMTNode_PostProcess_Material': '材质转资源',
        'SSMTNode_PostProcess_HealthDetection': '血量检测',
        'SSMTNode_PostProcess_SliderPanel': '滑块面板',
        'SSMTNode_PostProcess_WebPanel': '网页面板',
        'SSMTNode_PostProcess_ResourceMerge': '资源合并',
        'SSMTNode_PostProcess_BufferCleanup': '缓冲区清理',
        'SSMTNode_PostProcess_MultiFile': '多文件配置',
    }

    def update_blueprint_name(self, context):
        if self.blueprint_name and self.blueprint_name != 'NONE':
            self.label = f"嵌套: {self.blueprint_name}"
        else:
            self.label = "Blueprint Nest"
        self.update_node_width([self.blueprint_name])

    blueprint_name: bpy.props.StringProperty(
        name="Blueprint Name",
        description="选择要嵌套的蓝图",
        default="",
        update=update_blueprint_name
    )

    def init(self, context):
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 300

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.prop_search(self, "blueprint_name", bpy.data, "node_groups", text="", icon='NODETREE')

        op = row.operator("ssmt.create_blueprint_from_nest", text="", icon='ADD')
        op.node_name = self.name

        if self.blueprint_name and self.blueprint_name != 'NONE':
            blueprint = bpy.data.node_groups.get(self.blueprint_name)
            if blueprint and blueprint.bl_idname == 'SSMTBlueprintTreeType':
                box = layout.box()
                box.label(text=f"节点数: {len(blueprint.nodes)}", icon='NODE')
                box.label(text=f"连接数: {len(blueprint.links)}", icon='LINKED')

                output_nodes = [n for n in blueprint.nodes if n.bl_idname == 'SSMTNode_Result_Output']
                if output_nodes:
                    box.label(text=f"输出节点: {len(output_nodes)}", icon='FILE_TICK')
                else:
                    box.label(text="警告: 无输出节点", icon='ERROR')

                node_type_counts = {}
                for n in blueprint.nodes:
                    if n.bl_idname == 'NodeFrame':
                        continue
                    label = self._NODE_TYPE_LABELS.get(n.bl_idname, n.bl_idname)
                    node_type_counts[label] = node_type_counts.get(label, 0) + 1

                if node_type_counts:
                    box.separator()
                    box.label(text="节点类型:", icon='OUTLINER')
                    for type_label, count in sorted(node_type_counts.items()):
                        box.label(text=f"  {type_label}: {count}")

                box.separator()
                row = box.row(align=True)
                row.operator("ssmt.blueprint_nest_navigate", text="进入嵌套蓝图", icon='FORWARD')
            elif blueprint:
                layout.label(text="警告: 选中的不是SSMT蓝图", icon='ERROR')
            else:
                layout.label(text="警告: 蓝图不存在", icon='ERROR')


classes = (
    SSMTNode_Blueprint_Nest,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
