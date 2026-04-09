"""
物体切换节点 - 用于在 INI 中添加条件判定，实现物体的动态切换
支持无限嵌套和自定义快捷键
"""

import bpy
from dataclasses import dataclass
from .node_base import SSMTNodeBase, SSMTSocketObject
from ..utils.log_utils import LOG
from ..common.m_key import M_Key


@dataclass
class SwapKeyConfig:
    """单个物体切换节点的快捷键配置
    
    Attributes:
        node_id: 节点唯一标识
        index: 节点序号（用于生成 swapkey0, swapkey1 等）
        hotkey: 快捷键，格式为 "Modifier KeyName"
        swap_type: 切换类型 (cycle/toggle/hold)
        option_count: 选项数量（会生成 0, 1, 2... 序列）
        comment: 备注信息（会生成在 KeySwap 段落中作为注释）
    """
    
    node_id: str = ""  # 节点唯一标识
    index: int = 0  # 节点序号（用于生成 swapkey0, swapkey1 等）
    
    hotkey: str = "No_Modifiers Numpad3"  # 快捷键
    swap_type: str = "cycle"  # 切换类型: cycle, toggle, hold
    option_count: int = 2  # 选项数量（会生成 0, 1, 2... 序列）
    comment: str = ""  # 备注信息（会生成在 KeySwap 段落中作为注释）
    
    def get_swap_key_name(self) -> str:
        """生成对应的 $swapkey 变量名
        
        Returns:
            str: 格式为 "$swapkey{index}" 的变量名
        """
        return f"$swapkey{self.index}"
    
    def get_key_swap_section_name(self) -> str:
        """生成 [KeySwap_N] 段落名称
        
        Returns:
            str: 格式为 "KeySwap_{index}" 的段落名
        """
        return f"KeySwap_{self.index}"
    
    def get_active_param_name(self) -> str:
        """生成对应的 $active 参数名
        
        Returns:
            str: 格式为 "$active{index}" 的参数名
        """
        return f"$active{self.index}"
    
    def get_condition_value(self, option_index: int) -> str:
        """生成条件值，用于 drawindexed 中的 if 判定
        
        Args:
            option_index: 选项索引
            
        Returns:
            str: 选项索引的字符串形式
        """
        return str(option_index)
    
    def get_condition_str(self, value: int = 1) -> str:
        """生成完整的条件字符串
        
        Args:
            value: 比较的目标值，默认为1
            
        Returns:
            str: 格式为 "$swapkey{index} == {value}" 的条件字符串
        """
        return f"{self.get_swap_key_name()} == {value}"


class SSMTNode_ObjectSwap(SSMTNodeBase):
    """物体切换节点 - 在 INI 中为物体添加条件判定"""
    
    bl_idname = 'SSMTNode_ObjectSwap'
    bl_label = '物体切换'
    bl_icon = 'SHADERFX'
    
    # ============= 节点属性 =============
    
    def update_all_properties(self, context):
        """所有属性变化时更新节点宽度"""
        self.update_node_width([self.comment, self.hotkey])

    comment: bpy.props.StringProperty(
        name="备注",
        description="节点的备注信息，会生成在 KeySwap 段落中作为注释",
        default="",
        update=update_all_properties
    )
    
    # 快捷键配置
    hotkey: bpy.props.StringProperty(
        name="快捷键",
        description="按键的虚拟键码组合，格式: Modifier KeyName，例如 No_Modifiers Numpad3",
        default="No_Modifiers Numpad3",
        update=update_all_properties
    )
    
    def update_swap_type(self, context):
        """切换类型变化时触发更新"""
        pass
    
    swap_type: bpy.props.EnumProperty(
        name="切换类型",
        description="切换的类型",
        items=[
            ('cycle', '循环切换 (cycle)', '循环切换所有选项'),
            ('toggle', '开关切换 (toggle)', '在两个选项间切换'),
            ('hold', '按住 (hold)', '按住时激活'),
        ],
        default='cycle',
        update=update_swap_type
    )
    
    def update_input_slot_count(self, context):
        """输入口数量变化时触发更新"""
        self._update_input_sockets()
    
    # 输入口数量（支持动态增减）
    input_slot_count: bpy.props.IntProperty(
        name="输入口数量",
        description="物体切换节点的输入口数量（每个输入口对应一个选项）",
        min=1,
        max=1024,
        default=2,
        update=update_input_slot_count
    )
    
    def update_condition_operator(self, context):
        """条件运算符变化时触发更新"""
        pass
    
    # 条件运算符：用于连接多个 swapkey 条件
    condition_operator: bpy.props.EnumProperty(
        name="条件运算符",
        description="多个 swapkey 条件之间的逻辑运算符（&& 表示所有条件都满足，|| 表示至少一个条件满足）",
        items=[
            ('&&', 'AND (&&)', '所有条件都满足时才执行'),
            ('||', 'OR (||)', '至少一个条件满足时就执行'),
        ],
        default='&&',
        update=update_condition_operator
    )
    
    # ============= 调试和说明相关 =============
    
    description_expanded: bpy.props.BoolProperty(
        name="展开说明",
        description="展开节点说明菜单",
        default=False
    )
    
    def init(self, context):
        """初始化节点"""
        # 添加输出口
        self.outputs.new('SSMTSocketObject', "Output")
        
        # 添加初始输入口
        self._update_input_sockets()
        
        self.width = 300
    
    def _update_input_sockets(self):
        """根据 input_slot_count 更新输入口
        
        输入槽编号从 0 开始，与 KeySwap 的选项值对应：
        - 选项_0: 对应 $swapkey == 0
        - 选项_1: 对应 $swapkey == 1
        - 以此类推
        """
        current_count = len(self.inputs)
        target_count = self.input_slot_count
        
        # 移除多余的输入口
        while len(self.inputs) > target_count:
            self.inputs.remove(self.inputs[-1])
        
        # 添加不足的输入口，编号从 0 开始
        while len(self.inputs) < target_count:
            idx = len(self.inputs)  # 从 0 开始
            self.inputs.new('SSMTSocketObject', f"选项_{idx}")
        
        # 重命名所有输入口，确保名称正确
        for idx, inp in enumerate(self.inputs):
            inp.name = f"选项_{idx}"
    
    def update(self):
        """当属性变化时更新"""
        self._update_input_sockets()
    
    def draw_buttons(self, context, layout):
        """绘制节点 UI"""
        
        layout.prop(self, "comment", text="备注")
        layout.prop(self, "hotkey", text="按键")
        layout.prop(self, "swap_type", text="类型")
        
        layout.separator()
        layout.prop(self, "condition_operator", text="逻辑运算符")
        
        layout.separator()
        layout.label(text="选项数量:")
        row = layout.row(align=True)
        row.prop(self, "input_slot_count", text="")
        
        if self.input_slot_count >= 2:
            row.operator("ssmt.add_swap_option", text="", icon='ADD').node_name = self.name
        if self.input_slot_count > 1:
            row.operator("ssmt.remove_swap_option", text="", icon='REMOVE').node_name = self.name
        
        layout.separator()
        row = layout.row()
        icon = 'TRIA_DOWN' if self.description_expanded else 'TRIA_RIGHT'
        row.prop(self, "description_expanded", text="节点说明", icon=icon, emboss=True)
        
        if self.description_expanded:
            col = layout.column()
            col.scale_y = 0.8
            
            self._draw_node_description(col)
    
    def _draw_node_description(self, layout):
        """动态生成节点说明信息"""
        
        layout.label(text="本节点会添加的配置段落:", icon='INFO')
        layout.label(text="  • [KeySwap_*]        快捷键配置", icon='NONE')
        layout.label(text="  • [Constants]         变量声明", icon='NONE')
        layout.label(text="  • [Present]           参数初始化", icon='NONE')
        layout.label(text="  • [TextureOverride_*] 激活参数设定", icon='NONE')
        
        layout.separator()
        layout.label(text="当前节点参数:", icon='FILE_TEXT')
        
        layout.label(text=f"  备注: {self.comment if self.comment else '(未设置)'}", icon='NONE')
        layout.label(text=f"  快捷键: {self.hotkey}", icon='NONE')
        layout.label(text=f"  类型: {self.swap_type}", icon='NONE')
        layout.label(text=f"  逻辑运算符: {self.condition_operator}", icon='NONE')
        
        layout.separator()
        layout.label(text="选项配置:", icon='TRACKING')
        option_seq = ', '.join(str(i) for i in range(self.input_slot_count))
        layout.label(text=f"  选项值: {option_seq}", icon='NONE')
        layout.label(text=f"  条件格式: $swapkeyN == 选项值", icon='NONE')
        
        layout.separator()
        layout.label(text="生成示例:", icon='INFO')
        layout.label(text=f"  [KeySwap_*]", icon='NONE')
        if self.comment:
            layout.label(text=f"  ; {self.comment}", icon='NONE')
        layout.label(text=f"  condition = $active0 == 1", icon='NONE')
        layout.label(text=f"  key = {self.hotkey}", icon='NONE')
        layout.label(text=f"  type = {self.swap_type}", icon='NONE')
        layout.label(text=f"  $swapkeyN = {option_seq},", icon='NONE')


class SSMT_OT_AddSwapOption(bpy.types.Operator):
    """添加一个物体切换选项"""
    bl_idname = "ssmt.add_swap_option"
    bl_label = "添加选项"
    bl_options = {'REGISTER', 'UNDO'}
    
    node_name: bpy.props.StringProperty()
    
    @classmethod
    def poll(cls, context):
        return context.active_node is not None
    
    def execute(self, context):
        tree = context.space_data.edit_tree
        node = tree.nodes.get(self.node_name)
        
        if node and node.bl_idname == 'SSMTNode_ObjectSwap':
            if node.input_slot_count < 1024:
                node.input_slot_count += 1
        
        return {'FINISHED'}


class SSMT_OT_RemoveSwapOption(bpy.types.Operator):
    """移除一个物体切换选项"""
    bl_idname = "ssmt.remove_swap_option"
    bl_label = "移除选项"
    bl_options = {'REGISTER', 'UNDO'}
    
    node_name: bpy.props.StringProperty()
    
    @classmethod
    def poll(cls, context):
        return context.active_node is not None
    
    def execute(self, context):
        tree = context.space_data.edit_tree
        node = tree.nodes.get(self.node_name)
        
        if node and node.bl_idname == 'SSMTNode_ObjectSwap':
            if node.input_slot_count > 1:
                node.input_slot_count -= 1
        
        return {'FINISHED'}


# ============= 蓝图模型集成（处理链中的调试输出） =============

class ObjectSwapDebugger:
    """物体切换节点的调试输出生成器
    
    用于在日志中输出物体切换节点的详细信息，方便调试。
    """
    
    @staticmethod
    def generate_debug_detail(swap_node: bpy.types.Node, node_index: int, swap_key_index: int) -> list[str]:
        """为处理链生成该节点的调试信息
        
        Args:
            swap_node: 物体切换节点实例
            node_index: 节点在处理链中的序号
            swap_key_index: 该节点对应的全局 swapkey 索引
        
        Returns:
            list[str]: 调试文本行列表
        """
        lines = []
        lines.append("")
        lines.append(f"🔄 物体切换节点 #{node_index + 1}")
        lines.append(f"   备注: {getattr(swap_node, 'comment', '未设置')}")
        lines.append(f"   变量: $swapkey{swap_key_index}")
        lines.append(f"   快捷键: {getattr(swap_node, 'hotkey', 'N/A')}")
        lines.append(f"   切换类型: {getattr(swap_node, 'swap_type', 'N/A')}")
        lines.append(f"   选项数量: {getattr(swap_node, 'input_slot_count', 1)}")
        
        config = SwapKeyConfig(
            index=swap_key_index,
            comment=getattr(swap_node, 'comment', ''),
        )
        
        lines.append(f"")
        lines.append(f"   配置段落:")
        lines.append(f"   [{config.get_key_swap_section_name()}]")
        if config.comment:
            lines.append(f"   ; {config.comment}")
        lines.append(f"   condition = $active0 == 1")
        lines.append(f"   key = {getattr(swap_node, 'hotkey', 'N/A')}")
        lines.append(f"   type = {getattr(swap_node, 'swap_type', 'N/A')}")
        lines.append(f"   ${config.get_swap_key_name()} = 0,{','.join(str(i) for i in range(getattr(swap_node, 'input_slot_count', 1)))},")
        
        lines.append(f"")
        lines.append(f"   常量声明:")
        lines.append(f"   $swapkey{swap_key_index} = 0")
        
        lines.append(f"")
        lines.append(f"   初始化参数:")
        lines.append(f"   post $active0 = 0")
        
        return lines


# ============= 注册与卸载 =============

classes = (
    SSMTNode_ObjectSwap,
    SSMT_OT_AddSwapOption,
    SSMT_OT_RemoveSwapOption,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
