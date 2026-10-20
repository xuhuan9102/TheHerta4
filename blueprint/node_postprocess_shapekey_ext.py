import bpy
import os
import glob
import re
import math
import shutil
import uuid
from collections import OrderedDict

from .node_postprocess_base import SSMTNode_PostProcess_Base

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class ShapeKeyPlayGroupItem(bpy.types.PropertyGroup):
    shape_key_name: bpy.props.StringProperty(
        name="形态键名称",
        description="来自 Blender 模型的形态键名称。用于在 mod 配置里定位对应的变量",
    )
    object_name: bpy.props.StringProperty(
        name="物体名称",
        description="该形态键所属的 Blender 物体名称",
    )
    group_index: bpy.props.IntProperty(
        name="分组编号",
        description="该形态键所属的播放分组编号。0 表示未分组（未分组的形态键在 mod 中不生效）",
        default=0,
        min=0,
    )


class ShapeKeySpeedIntervalItem(bpy.types.PropertyGroup):
    start: bpy.props.FloatProperty(
        name="起点",
        description="变速区间的起始位置（0-100，表示一个完整动画周期里的进度百分比）",
        default=0.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE',
    )
    end: bpy.props.FloatProperty(
        name="终点",
        description="变速区间的结束位置（0-100，表示一个完整动画周期里的进度百分比）",
        default=50.0,
        min=0.0,
        max=100.0,
        subtype='PERCENTAGE',
    )
    base_step: bpy.props.IntProperty(
        name="区间速度",
        description=(
            "该区间内动画的推进速度。\n"
            "分组勾选「区间数值按速度百分比」时：相对单位速度的百分比\n"
            "  100% = 使用单位速度（100% 全局速度下每帧推进的细分数）\n"
            "  10%  = 单位速度的十分之一，播放更慢\n"
            "未勾选（升级前的老工程）时：旧的「每帧步进数」，行为与升级前一致。\n"
            "单位速度由「总细分」「总时长」「假设帧率」自动计算"
        ),
        default=100,
        min=1,
        max=10000,
    )


def _on_group_use_remark_as_icon_changed(self, context):
    """勾选「用备注生成图标」时自动取消「以图片作为图标」，实现互斥。"""
    if self.use_remark_as_icon and self.use_image_as_icon:
        self.use_image_as_icon = False


def _on_group_use_image_as_icon_changed(self, context):
    """勾选「以图片作为图标」时自动取消「用备注生成图标」，实现互斥。"""
    if self.use_image_as_icon and self.use_remark_as_icon:
        self.use_remark_as_icon = False


class ShapeKeyPlayGroupSettings(bpy.types.PropertyGroup):
    group_index: bpy.props.IntProperty(
        name="分组编号",
        description="分组的唯一编号，用于区分不同分组。移动分组时编号会自动重排",
        default=1,
        min=1,
    )
    group_mode: bpy.props.EnumProperty(
        name="分组模式",
        description=(
            "组内形态键的控制方式：\n"
            "  同步 = 所有形态键同时受同一滑块控制，一起变化\n"
            "  序列 = 形态键依次受滑块控制，滑到底时形成连续动画"
        ),
        items=[
            ('SYNC', "同步", "组内所有形态键同时受滑块控制（默认）"),
            ('SEQUENCE', "序列", "组内形态键依次受滑块控制，形成连续动画路径"),
        ],
        default='SYNC',
    )
    enable_auto_playback: bpy.props.BoolProperty(
        name="启用自动播放",
        description="开启后该分组的形态键会自动循环播放，不需要玩家手动拖滑块。游戏内的滑块此时用来控制速度",
        default=False,
    )
    auto_playback_frame_count: bpy.props.IntProperty(
        name="总细分",
        description=(
            "一个完整动画周期被分成多少份（所有形态键的细分数总和）。\n"
            "份数越多动画越细腻，但生成代码越多、文件越大。\n"
            "与「总时长」「假设帧率」一起决定单位速度"
        ),
        default=30,
        min=2,
        max=5000,
    )
    auto_playback_step_frames: bpy.props.IntProperty(
        name="每帧初始步进次数",
        description="保留兼容用的旧参数，现在由「总细分 / 总时长 / 假设帧率」自动计算单位速度，修改此值一般不影响效果",
        default=5,
        min=1,
        max=5000,
    )
    auto_playback_cycle_mode: bpy.props.EnumProperty(
        name="循环模式",
        description=(
            "自动播放到达终点后的循环方向：\n"
            "  正向 = 0 到 1 循环（默认）\n"
            "  反向 = 1 到 0 循环\n"
            "  往返 = 0→1→0→1 来回播放"
        ),
        items=[
            ('FORWARD', "正向", "从0到1循环"),
            ('REVERSE', "反向", "从1到0循环"),
            ('PINGPONG', "往返", "0→1→0→1往返循环"),
        ],
        default='FORWARD',
    )
    auto_playback_duration: bpy.props.FloatProperty(
        name="总时长（秒）",
        description=(
            "一次完整动画循环播放所需的时间（秒）。\n"
            "配合「总细分」和「假设帧率」自动计算单位速度：\n"
            "  单位速度 = round(总细分 / (总时长 × 假设帧率))"
        ),
        default=2.0,
        min=0.1,
        max=300.0,
        precision=2,
    )
    playback_fps: bpy.props.IntProperty(
        name="假设帧率（FPS）",
        description=(
            "用于计算单位速度时假设的游戏帧率。\n"
            "游戏帧率越高，3dmigoto 每帧推进越快；\n"
            "建议填写目标游戏的实际帧率（60 / 120 / 144 等）"
        ),
        default=60,
        min=1,
        max=360,
    )
    speed_percent_min: bpy.props.IntProperty(
        name="速度百分比最小值",
        description="游戏内速度滑块拉到最左边时的全局速度倍率。100 = 正常速度",
        default=100,
        min=1,
        max=10000,
    )
    speed_percent_max: bpy.props.IntProperty(
        name="速度百分比最大值",
        description="游戏内速度滑块拉到最右边时的全局速度倍率。100 = 正常速度。数值越大，最快播放越快",
        default=1000,
        min=1,
        max=10000,
    )
    speed_percent: bpy.props.IntProperty(
        name="当前速度百分比",
        description=(
            "全局速度倍率（100% = 正常速度）。\n"
            "启用自动播放后，游戏内的滑块会实时修改这个值。\n"
            "实际每帧推进 = 单位速度 × 区间速度% × 全局速度%"
        ),
        default=100,
        min=1,
        max=10000,
    )
    speed_intervals: bpy.props.CollectionProperty(
        type=ShapeKeySpeedIntervalItem,
        description="按动画进度分段设置不同的推进速度（例如开头慢、中段快、结尾慢）",
    )
    active_interval_index: bpy.props.IntProperty(default=0)
    speed_percent_mode: bpy.props.BoolProperty(
        name="区间数值按速度百分比",
        description=(
            "勾选后，变速区间里的数值表示「相对单位速度的百分比」（100 = 单位速度）。\n"
            "升级前保存的老工程默认不勾选：数值仍按旧的「每帧步进数」解释，"
            "播放速度与升级前完全一致；想改用新语义时勾选本项即可"
        ),
        default=False,
    )
    max_step_unroll: bpy.props.IntProperty(
        name="最大步进展开次数",
        description="（已废弃）以前需要手动设置的参数。现在会根据变速区间和速度自动计算，无需再管",
        default=100,
        min=1,
        max=500,
    )
    remark: bpy.props.StringProperty(
        name="备注",
        description="分组备注，会显示在分组标题和游戏内按钮上。输入 / 符号可以强制换行",
    )
    button_icon_image: bpy.props.StringProperty(
        name="按钮图标图片",
        description="自定义游戏内按钮图标图片（勾选「以图片作为图标」时使用）",
        subtype='FILE_PATH',
        default="",
    )
    use_remark_as_icon: bpy.props.BoolProperty(
        name="用备注生成图标",
        description="勾选后会用备注文字自动生成按钮图标（需要安装 Pillow 库）。与「以图片作为图标」互斥",
        default=True,
        update=_on_group_use_remark_as_icon_changed,
    )
    use_image_as_icon: bpy.props.BoolProperty(
        name="以图片作为图标",
        description="勾选后使用自定义图片作为按钮图标。与「用备注生成图标」互斥；两个都不勾选时使用内置默认图标",
        default=False,
        update=_on_group_use_image_as_icon_changed,
    )

    expanded: bpy.props.BoolProperty(
        name="展开",
        description="是否在节点面板里展开该分组的形态键列表",
        default=True,
    )

    remark_font_family: bpy.props.EnumProperty(
        name="字体",
        description="备注文字使用的字体",
        items=[
            ('msyh.ttc', "微软雅黑", "Windows 标准中文字体"),
            ('simsun.ttc', "宋体", "Windows 经典衬线字体"),
            ('simhei.ttf', "黑体", "Windows 经典无衬线字体"),
            ('arial.ttf', "Arial", "标准英文字体"),
        ],
        default='msyh.ttc',
    )
    remark_font_size: bpy.props.IntProperty(
        name="字号大小",
        description="备注文字的字号（像素）。越大，生成的按钮图标越大",
        default=36,
        min=10,
        max=300,
    )
    remark_text_color: bpy.props.FloatVectorProperty(
        name="文字颜色",
        description="备注文字的颜色",
        subtype='COLOR',
        size=3,
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )
    remark_stroke_width: bpy.props.IntProperty(
        name="描边宽度",
        description="备注文字的描边宽度，用于在复杂背景上增强可读性。0 = 不描边",
        default=4,
        min=0,
        max=20,
    )
    remark_stroke_color: bpy.props.FloatVectorProperty(
        name="描边颜色",
        description="备注文字的描边颜色",
        subtype='COLOR',
        size=3,
        default=(0.0, 0.0, 0.0),
        min=0.0,
        max=1.0,
    )


class SSMT_OT_SpeedIntervalAdd(bpy.types.Operator):
    bl_idname = "ssmt.speed_interval_add"
    bl_label = "添加区间"
    bl_options = {'REGISTER', 'INTERNAL'}
    group_index: bpy.props.IntProperty(default=1)
    node_tree_name: bpy.props.StringProperty()
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree_name)
        if not tree: return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node: return {'CANCELLED'}
        settings = node._find_group_setting(self.group_index)
        if settings:
            interval = settings.speed_intervals.add()
            last = settings.speed_intervals[-2] if len(settings.speed_intervals) > 1 else None
            if last:
                interval.start = last.end
                interval.end = min(100.0, last.end + 50.0)
            else:
                interval.start = 0.0
                interval.end = 50.0
            # 新语义分组默认 100%（= 单位速度）；老语义分组默认沿用旧的步进默认值 10
            interval.base_step = 100 if getattr(settings, "speed_percent_mode", False) else 10
        return {'FINISHED'}


class SSMT_OT_SpeedIntervalRemove(bpy.types.Operator):
    bl_idname = "ssmt.speed_interval_remove"
    bl_label = "删除区间"
    bl_options = {'REGISTER', 'INTERNAL'}
    group_index: bpy.props.IntProperty(default=1)
    interval_index: bpy.props.IntProperty(default=0)
    node_tree_name: bpy.props.StringProperty()
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree_name)
        if not tree: return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node: return {'CANCELLED'}
        settings = node._find_group_setting(self.group_index)
        if settings and self.interval_index < len(settings.speed_intervals):
            settings.speed_intervals.remove(self.interval_index)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyExtSetGroup(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_ext_set_group"
    bl_label = "设置播放分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    shape_key_name: bpy.props.StringProperty()
    group_index: bpy.props.IntProperty(name="分组编号", min=0, default=1)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "group_index", text="播放分组")

    def execute(self, context):
        node = self._get_node(context)
        if not node: return {'CANCELLED'}
        found = False
        for entry in node.play_group_entries:
            if entry.shape_key_name == self.shape_key_name:
                entry.group_index = self.group_index
                found = True
                break
        if not found:
            entry = node.play_group_entries.add()
            entry.shape_key_name = self.shape_key_name
            entry.group_index = self.group_index
        self.report({'INFO'}, f"已设置 {self.shape_key_name} -> 播放分组 {self.group_index}")
        return {'FINISHED'}

    def _get_node(self, context):
        space = getattr(context, "space_data", None)
        if space and space.type == 'NODE_EDITOR':
            tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
            if tree:
                return tree.nodes.get(self.node_name)
        return None


class SSMT_OT_ScanShapeKeyExt(bpy.types.Operator):
    bl_idname = "ssmt.scan_shapekey_ext"
    bl_label = "扫描形态键"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        names = node._scan_shapekey_names_from_variable_items()
        if not names:
            names = node._scan_shapekey_names_from_classification()
        if not names:
            self.report({'WARNING'}, "未扫描到任何形态键")
            return {'CANCELLED'}
        current_names = set(names)
        stale_indices = [i for i, entry in enumerate(node.play_group_entries) if entry.shape_key_name not in current_names]
        for i in reversed(stale_indices): node.play_group_entries.remove(i)
        count = 0
        for name in names:
            if not any(entry.shape_key_name == name for entry in node.play_group_entries):
                entry = node.play_group_entries.add()
                entry.shape_key_name = name
                entry.group_index = 0
                count += 1
        self.report({'INFO'}, f"扫描到 {len(names)} 个形态键，清除 {len(stale_indices)} 个失效条目，新增 {count} 个")
        return {'FINISHED'}


class SSMT_OT_SelectPlayGroup(bpy.types.Operator):
    bl_idname = "ssmt.select_play_group"
    bl_label = "切换到此分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        node.active_group_index = self.target_group_index
        node._ensure_group_setting(self.target_group_index)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyGroupAdd(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_group_add"
    bl_label = "创建新分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        max_idx = 0
        for setting in node.play_group_settings:
            if setting.group_index > max_idx:
                max_idx = setting.group_index
        new_group = node.play_group_settings.add()
        new_group.group_index = max_idx + 1
        # 新建分组一律使用新语义（区间数值 = 相对单位速度的百分比）
        new_group.speed_percent_mode = True
        node.active_group_index = new_group.group_index
        self.report({'INFO'}, f"已创建并切换至分组 {new_group.group_index}")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyGroupDuplicate(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_group_duplicate"
    bl_label = "复制分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    # 需要原样克隆的分组标量字段（不含 group_index；形态键成员归属不复制）
    _CLONED_FIELDS = (
        "group_mode",
        "enable_auto_playback",
        "auto_playback_frame_count",
        "auto_playback_step_frames",
        "auto_playback_cycle_mode",
        "auto_playback_duration",
        "playback_fps",
        "speed_percent_min",
        "speed_percent_max",
        "speed_percent",
        "speed_percent_mode",
        "max_step_unroll",
        "remark",
        "button_icon_image",
        "use_remark_as_icon",
        "use_image_as_icon",
        "expanded",
        "remark_font_family",
        "remark_font_size",
        "remark_text_color",
        "remark_stroke_width",
        "remark_stroke_color",
        "active_interval_index",
    )
    # 向量型字段：先从 bpy_prop_array 转成 tuple 再赋值，避免类型不匹配
    _VECTOR_FIELDS = ("remark_text_color", "remark_stroke_color")

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}

        source = None
        max_idx = 0
        for setting in node.play_group_settings:
            if setting.group_index > max_idx:
                max_idx = setting.group_index
            if setting.group_index == self.target_group_index:
                source = setting
        if source is None:
            self.report({'WARNING'}, f"找不到要复制的分组 {self.target_group_index}")
            return {'CANCELLED'}

        new_group = node.play_group_settings.add()
        new_group.group_index = max_idx + 1

        for field in self._CLONED_FIELDS:
            value = getattr(source, field)
            if field in self._VECTOR_FIELDS:
                value = tuple(value)
            setattr(new_group, field, value)

        # 逐条复制变速区间（start / end / base_step）
        for interval in source.speed_intervals:
            new_interval = new_group.speed_intervals.add()
            new_interval.start = interval.start
            new_interval.end = interval.end
            new_interval.base_step = interval.base_step

        # 复制后展开，便于立刻看到新分组；备注原样保留，不加后缀
        new_group.expanded = True
        # 不复制 play_group_entries 的成员归属：原分组成员保持不动
        node.active_group_index = new_group.group_index
        self.report({'INFO'}, f"已复制分组 {self.target_group_index} → 新分组 {new_group.group_index}")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyGroupRemove(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_group_remove"
    bl_label = "删除分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        if len(node.play_group_settings) <= 1:
            self.report({'WARNING'}, "至少保留一个分组，不能删除最后一个分组")
            return {'CANCELLED'}

        for entry in node.play_group_entries:
            if entry.group_index == self.target_group_index:
                entry.group_index = 0

        idx_to_remove = -1
        for i, setting in enumerate(node.play_group_settings):
            if setting.group_index == self.target_group_index:
                idx_to_remove = i
                break
        if idx_to_remove != -1:
            node.play_group_settings.remove(idx_to_remove)

        if node.active_group_index == self.target_group_index:
            if len(node.play_group_settings) > 0:
                node.active_group_index = node.play_group_settings[0].group_index

        self.report({'INFO'}, f"已删除分组 {self.target_group_index}，相关形态键已移至未分组")
        return {'FINISHED'}


class SSMT_OT_ShapeKeyGroupMoveUpDown(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_group_move"
    bl_label = "移动分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    direction: bpy.props.EnumProperty(items=[('UP', "上移", ""), ('DOWN', "下移", "")])
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        settings = node.play_group_settings
        idx = -1
        for i, setting in enumerate(settings):
            if setting.group_index == self.target_group_index:
                idx = i
                break
        if idx == -1: return {'CANCELLED'}
        new_idx = idx + (1 if self.direction == 'DOWN' else -1)
        if new_idx < 0 or new_idx >= len(settings): return {'CANCELLED'}

        # 交换列表顺序（settings.move 只重排列表位置，不会改变各分组头的 group_index 值）
        settings.move(idx, new_idx)

        # 重新编号：直接以每个分组头自身移动前的旧编号作为身份标识建立映射。
        # 注意：不能使用 as_pointer() —— CollectionProperty.move() 之后元素指针
        # 不保证稳定，会导致映射错误、形态键不跟随分组移动。
        index_map = {}
        for i, setting in enumerate(settings):
            old_index = setting.group_index
            setting.group_index = i + 1
            index_map[old_index] = i + 1

        # 同步移动所有形态键条目，使其跟随各自的分组头一起移动
        for entry in node.play_group_entries:
            if entry.group_index in index_map:
                entry.group_index = index_map[entry.group_index]

        # 当前编辑分组若随移动而改变编号，同步更新
        if node.active_group_index in index_map:
            node.active_group_index = index_map[node.active_group_index]

        return {'FINISHED'}


class SSMT_OT_OpenGroupSettings(bpy.types.Operator):
    bl_idname = "ssmt.open_group_settings"
    bl_label = "分组详细设置"
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: bpy.props.StringProperty()
    node_tree_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    def invoke(self, context, event):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}

        self.node_tree_name = tree.name
        self.node_name = node.name

        setting = node._find_group_setting(self.target_group_index)
        if not setting:
            return {'CANCELLED'}

        # 不再把 setting 拷贝到 prop_* —— draw 里直接绑定 setting 字段，
        # 所有修改实时写回原数据，无需点确认。
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text=f"分组 {self.target_group_index} - 详细设置", icon='GROUP')

        # 直接定位 setting —— 所有 prop 都渲染 setting 自身的字段，
        # 任何修改都会实时写回原数据，不再依赖"点确认"。
        tree = bpy.data.node_groups.get(self.node_tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            box.label(text="无法定位形态键扩展节点", icon='ERROR')
            return
        setting = node._find_group_setting(self.target_group_index)
        if setting is None:
            box.label(text="无法定位分组设置", icon='ERROR')
            return

        box.prop(setting, "group_mode")
        box.prop(setting, "enable_auto_playback")

        if setting.enable_auto_playback:
            box.prop(setting, "auto_playback_frame_count")
            box.prop(setting, "auto_playback_duration")
            box.prop(setting, "playback_fps")
            box.prop(setting, "auto_playback_cycle_mode")
            col = box.column(align=True)
            col.prop(setting, "speed_percent_min")
            col.prop(setting, "speed_percent_max")
            col.prop(setting, "speed_percent")

            box.label(text="变速区间 (0-100%)", icon='IPO_EASE_IN_OUT')
            box.prop(setting, "speed_percent_mode")
            percent_mode = bool(getattr(setting, "speed_percent_mode", True))
            if not percent_mode:
                box.label(
                    text="当前按旧版「每帧步进数」解释区间数值（播放速度与升级前一致）",
                    icon='INFO',
                )
            interval_box = box.box()

            for idx, interval in enumerate(setting.speed_intervals):
                row_int = interval_box.row(align=True)
                row_int.prop(interval, "start", text="起点")
                row_int.prop(interval, "end", text="终点")
                row_int.prop(interval, "base_step", text="速度%" if percent_mode else "步进")

                op = row_int.operator("ssmt.speed_interval_remove", text="", icon='X')
                op.group_index = self.target_group_index
                op.interval_index = idx
                op.node_name = self.node_name
                op.node_tree_name = self.node_tree_name

            row_add = interval_box.row(align=True)
            op = row_add.operator("ssmt.speed_interval_add", text="添加区间", icon='ADD')
            op.group_index = self.target_group_index
            op.node_name = self.node_name
            op.node_tree_name = self.node_tree_name

            # 单位速度自动计算，只显示结果（其余信息已按需求隐藏）
            unit_speed_cur = node._calc_unit_speed(setting)
            info_box = box.box()
            info_box.label(
                text=f"单位速度（自动）= {unit_speed_cur:g} 细分/帧",
                icon='AUTO',
            )

        box.prop(setting, "remark")
        box.label(text="提示：输入 / 符号可强制换行", icon='INFO')

        # ---- 按钮图标来源：图片 / 备注生成 / 内置默认 三选一 ----
        icon_box = box.box()
        icon_box.label(text="按钮图标来源", icon='IMAGE_DATA')

        row_img = icon_box.row(align=True)
        row_img.prop(setting, "use_image_as_icon", text="以图片作为图标")
        if setting.use_image_as_icon:
            icon_box.prop(setting, "button_icon_image", text="图片文件")

        if PIL_AVAILABLE:
            row_rem = icon_box.row(align=True)
            row_rem.prop(setting, "use_remark_as_icon", text="用备注生成图标")
            if setting.use_remark_as_icon:
                box_style = icon_box.box()
                box_style.label(text="文字图标样式", icon='COLOR')
                box_style.prop(setting, "remark_font_family")
                box_style.prop(setting, "remark_font_size")
                row_c = box_style.row(align=True)
                row_c.prop(setting, "remark_text_color")
                row_c.prop(setting, "remark_stroke_color")
                box_style.prop(setting, "remark_stroke_width")
        else:
            icon_box.label(text="未安装 Pillow 库，无法使用「用备注生成图标」", icon='ERROR')

        if not setting.use_image_as_icon and not setting.use_remark_as_icon:
            icon_box.label(text="两个都不勾选时将使用内置默认图标", icon='INFO')

    def execute(self, context):
        # 所有设置已在 draw 中通过直接绑定 setting 实时写回，
        # 这里不再需要从 prop_* 拷贝，只需结束对话框。
        return {'FINISHED'}


class SSMT_OT_ShapeKeyMoveUpDown(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_move"
    bl_label = "移动形态键"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    direction: bpy.props.EnumProperty(items=[('UP', "上移", ""), ('DOWN', "下移", "")])
    shape_key_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        entries = node.play_group_entries
        idx = -1
        for i, e in enumerate(entries):
            if e.shape_key_name == self.shape_key_name:
                idx = i
                break
        if idx == -1: return {'CANCELLED'}
        if self.direction == 'UP' and idx > 0:
            entries.move(idx, idx - 1)
        elif self.direction == 'DOWN' and idx < len(entries) - 1:
            entries.move(idx, idx + 1)
        return {'FINISHED'}


class SSMT_OT_ShapeKeyAssignToCurrentGroup(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_assign_current_group"
    bl_label = "移入当前分组"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    shape_key_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        group = node.active_group_index
        for entry in node.play_group_entries:
            if entry.shape_key_name == self.shape_key_name:
                entry.group_index = group
                break
        return {'FINISHED'}


class SSMT_OT_ShapeKeyUnassignFromGroup(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_unassign_group"
    bl_label = "移出分组"
    bl_description = "把该形态键移至「未分组」，其在 mod 配置文件中的变量/定义/触发机制将被自动注释"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()
    shape_key_name: bpy.props.StringProperty()

    def execute(self, context):
        space = getattr(context, "space_data", None)
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None) if space and space.type == 'NODE_EDITOR' else None
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_ShapeKeyExt':
            return {'CANCELLED'}
        for entry in node.play_group_entries:
            if entry.shape_key_name == self.shape_key_name:
                entry.group_index = 0
                break
        return {'FINISHED'}


class SSMT_OT_ShapeKeyExt_ParseSliderObject(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_ext_parse_slider_object"
    bl_label = "解析物体"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_ShapeKeyExt':
                    node._update_from_object()
                    self.report({'INFO'}, f"已解析物体 '{node.target_object}' -> 哈希: {node.detect_hash}, IndexCount: {node.detect_index_count}")
                    return {'FINISHED'}
        self.report({'WARNING'}, "无法找到形态键扩展配置节点")
        return {'CANCELLED'}


class SSMT_OT_ShapeKeyExt_Refresh(bpy.types.Operator):
    bl_idname = "ssmt.shapekey_ext_refresh"
    bl_label = "应用设置到Mod"
    bl_description = "按节点当前设置，原地更新 mod ini 中形态键扩展（含滑块面板）的配置，无需重新导出整个 mod"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_ShapeKeyExt':
                    ini_path = (node.ini_file_path or "").strip() or node.last_mod_ini_path
                    if not ini_path or not os.path.isfile(ini_path):
                        self.report({'WARNING'}, "未找到目标 ini：请先导出一次 mod，或在节点「INI文件」中指定")
                        return {'CANCELLED'}
                    ok = node.execute_postprocess(os.path.dirname(ini_path), _in_place=True, _ini_path=ini_path)
                    if ok:
                        self.report({'INFO'}, f"已原地刷新形态键扩展配置: {os.path.basename(ini_path)}")
                        return {'FINISHED'}
                    self.report({'ERROR'}, "刷新失败，请查看控制台日志（旧格式可能需要先重新导出一次）")
                    return {'CANCELLED'}
        self.report({'WARNING'}, "无法找到形态键扩展配置节点")
        return {'CANCELLED'}


# ============================================================================
# 快捷键图形键盘选择器（任务3）
# ----------------------------------------------------------------------------
# 布局常量：每行 = ((显示名, 写回 token, 键宽), ...)，键宽单位是标准键盘的 1u。
# token 就是 3dmigoto 键名（普通键小写 / 虚拟键 VK_XXX 大写），与节点默认值风格一致
# （home / ctrl home / VK_LBUTTON）。左右同名修饰键共用 token（两个 Shift 都是 shift）。
# ============================================================================
KEY_PICKER_MODIFIERS = ("ctrl", "shift", "alt")

# 主键区：标准 104 键布局
# 行1 Esc+F1~F12 / 行2 数字行 / 行3 Tab 行 / 行4 Caps 行 / 行5 Shift 行 / 行6 底行
KEY_PICKER_MAIN_ROWS = (
    (("Esc", "esc", 1.0), ("F1", "f1", 1.0), ("F2", "f2", 1.0), ("F3", "f3", 1.0),
     ("F4", "f4", 1.0), ("F5", "f5", 1.0), ("F6", "f6", 1.0), ("F7", "f7", 1.0),
     ("F8", "f8", 1.0), ("F9", "f9", 1.0), ("F10", "f10", 1.0), ("F11", "f11", 1.0),
     ("F12", "f12", 1.0)),
    (("`", "VK_OEM_3", 1.0), ("1", "1", 1.0), ("2", "2", 1.0), ("3", "3", 1.0),
     ("4", "4", 1.0), ("5", "5", 1.0), ("6", "6", 1.0), ("7", "7", 1.0),
     ("8", "8", 1.0), ("9", "9", 1.0), ("0", "0", 1.0), ("-", "VK_OEM_MINUS", 1.0),
     ("=", "VK_OEM_PLUS", 1.0), ("Backspace", "backspace", 2.0)),
    (("Tab", "tab", 1.5), ("Q", "q", 1.0), ("W", "w", 1.0), ("E", "e", 1.0),
     ("R", "r", 1.0), ("T", "t", 1.0), ("Y", "y", 1.0), ("U", "u", 1.0),
     ("I", "i", 1.0), ("O", "o", 1.0), ("P", "p", 1.0), ("[", "VK_OEM_4", 1.0),
     ("]", "VK_OEM_6", 1.0), ("\\", "VK_OEM_5", 1.5)),
    (("CapsLock", "capslock", 1.75), ("A", "a", 1.0), ("S", "s", 1.0), ("D", "d", 1.0),
     ("F", "f", 1.0), ("G", "g", 1.0), ("H", "h", 1.0), ("J", "j", 1.0),
     ("K", "k", 1.0), ("L", "l", 1.0), (";", "VK_OEM_1", 1.0), ("'", "VK_OEM_7", 1.0),
     ("Enter", "enter", 2.25)),
    (("Shift", "shift", 2.25), ("Z", "z", 1.0), ("X", "x", 1.0), ("C", "c", 1.0),
     ("V", "v", 1.0), ("B", "b", 1.0), ("N", "n", 1.0), ("M", "m", 1.0),
     (",", "VK_OEM_COMMA", 1.0), (".", "VK_OEM_PERIOD", 1.0), ("/", "VK_OEM_2", 1.0),
     ("Shift", "shift", 2.25)),
    (("Ctrl", "ctrl", 1.25), ("Win", "VK_LWIN", 1.25), ("Alt", "alt", 1.25),
     ("Space", "space", 6.25), ("AltGr", "VK_RMENU", 1.25), ("Win", "VK_RWIN", 1.25),
     ("Menu", "VK_APPS", 1.25), ("Ctrl", "ctrl", 1.25)),
)

# 编辑键区两行 / 方向键区两行 / 小键盘区 / 鼠标行（弹窗里左列=编辑+方向，右列=小键盘）
KEY_PICKER_EDITING_ROWS = (
    (("Ins", "insert", 1.0), ("HM", "home", 1.0), ("PgUp", "pageup", 1.0)),
    (("Del", "delete", 1.0), ("End", "end", 1.0), ("PgDn", "pagedown", 1.0)),
)
# 方向键第一行左右留空占位，让 ↑ 居中在 ← ↓ → 上方
KEY_PICKER_ARROW_ROWS = (
    (("", "", 1.0), ("↑", "up", 1.0), ("", "", 1.0)),
    (("←", "left", 1.0), ("↓", "down", 1.0), ("→", "right", 1.0)),
)
KEY_PICKER_NUMPAD_ROWS = (
    (("PrtSc", "VK_SNAPSHOT", 1.0), ("ScrLk", "VK_SCROLL", 1.0), ("Pause", "VK_PAUSE", 1.0)),
    (("NumLock", "VK_NUMLOCK", 1.0),),
    (("÷", "VK_DIVIDE", 1.0), ("×", "VK_MULTIPLY", 1.0), ("−", "VK_SUBTRACT", 1.0)),
    (("7", "VK_NUMPAD7", 1.0), ("8", "VK_NUMPAD8", 1.0), ("9", "VK_NUMPAD9", 1.0),
     ("+", "VK_ADD", 1.0)),
    (("4", "VK_NUMPAD4", 1.0), ("5", "VK_NUMPAD5", 1.0), ("6", "VK_NUMPAD6", 1.0)),
    (("1", "VK_NUMPAD1", 1.0), ("2", "VK_NUMPAD2", 1.0), ("3", "VK_NUMPAD3", 1.0)),
    (("0", "VK_NUMPAD0", 2.0), (".", "VK_DECIMAL", 1.0)),
)
KEY_PICKER_MOUSE_ROW = (
    ("左键", "VK_LBUTTON", 1.25), ("右键", "VK_RBUTTON", 1.25), ("中键", "VK_MBUTTON", 1.25),
    ("第四键", "VK_XBUTTON1", 1.25), ("第五键", "VK_XBUTTON2", 1.25),
    ("滚轮↑", "MOUSE_WHEEL_UP", 1.25), ("滚轮↓", "MOUSE_WHEEL_DOWN", 1.25),
)

KEY_PICKER_EXTRA_ROWS = (
    KEY_PICKER_EDITING_ROWS + KEY_PICKER_ARROW_ROWS + KEY_PICKER_NUMPAD_ROWS
    + (KEY_PICKER_MOUSE_ROW,)
)


def key_picker_row_tokens(row_def):
    """一行里真正可写回的 token（跳过方向键区的空白占位键）。"""
    return [token for _label, token, _width in row_def if token]


def key_picker_all_tokens():
    """全部可点按键 token（测试用来校验无重复；修饰键左右各一个属正常重复）。"""
    tokens = []
    for row in KEY_PICKER_MAIN_ROWS + KEY_PICKER_EXTRA_ROWS:
        tokens.extend(key_picker_row_tokens(row))
    return tokens


def key_picker_is_modifier(token):
    return str(token or "").strip().lower() in KEY_PICKER_MODIFIERS


def key_picker_parse(text):
    """'ctrl shift home' / 'VK_LBUTTON' → (修饰键元组, 主键)。"""
    mods = []
    key = ""
    for raw in str(text or "").split():
        token = raw.strip()
        if not token:
            continue
        low = token.lower()
        if low in KEY_PICKER_MODIFIERS:
            if low not in mods:
                mods.append(low)
        else:
            key = token
    return tuple(m for m in KEY_PICKER_MODIFIERS if m in mods), key


def key_picker_format(mods, key):
    """组合键写回格式：修饰键固定 ctrl→shift→alt，普通键小写、VK 名保持大写。"""
    ordered = [m for m in KEY_PICKER_MODIFIERS if m in tuple(mods or ())]
    main = str(key or "").strip()
    if main:
        main = main.upper() if main.upper().startswith("VK_") else main.lower()
    return " ".join(ordered + ([main] if main else []))


def key_picker_toggle_modifier(mods, token):
    """点击修饰键：已选则取消，未选则加上（可多选），返回新元组。"""
    name = str(token or "").strip().lower()
    current = [m for m in KEY_PICKER_MODIFIERS if m in tuple(mods or ())]
    if name in current:
        current.remove(name)
    elif name in KEY_PICKER_MODIFIERS:
        current.append(name)
    return tuple(m for m in KEY_PICKER_MODIFIERS if m in current)


def key_picker_apply_click(mods, key, token):
    """点击按键：修饰键 = 多选切换；普通键 = 替换当前主键。"""
    if key_picker_is_modifier(token):
        return key_picker_toggle_modifier(mods, token), key
    return tuple(m for m in KEY_PICKER_MODIFIERS if m in tuple(mods or ())), str(token or "").strip()


def key_picker_display(mods, key):
    return key_picker_format(mods, key) or "（未设置）"


def _force_key_picker_redraw():
    """弹窗重绘兜底：Blender 5.x 的 invoke_props_dialog 弹窗是独立窗口（screen 名 temp），
    area.tag_redraw 刷不到它。延迟一拍后对每个窗口各做一次窗口级重绘，尽力让已打开
    弹窗的按键高亮与底部「当前组合键」原地更新；绝不重新 invoke 弹窗。"""
    wm = bpy.context.window_manager
    for window in list(getattr(wm, "windows", None) or []):
        try:
            with bpy.context.temp_override(window=window):
                bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        except Exception:
            continue
    return None


class SSMT_OT_KeyPickerPick(bpy.types.Operator):
    """图形键盘里的单个按键：点击后更新组合键状态并刷新弹窗。"""
    bl_idname = "ssmt.key_picker_pick"
    bl_label = "选择按键"
    bl_options = {'REGISTER', 'INTERNAL'}

    token: bpy.props.StringProperty()
    action: bpy.props.StringProperty(default="pick")
    node_name: bpy.props.StringProperty()
    node_tree_name: bpy.props.StringProperty()
    target_prop: bpy.props.StringProperty()

    def execute(self, context):
        wm = context.window_manager
        mods, key = key_picker_parse(getattr(wm, "ssmt_kp_combo", ""))
        if self.action == "clear":
            mods, key = (), ""
        else:
            mods, key = key_picker_apply_click(mods, key, self.token)
        wm.ssmt_kp_combo = key_picker_format(mods, key)
        wm.ssmt_kp_just_picked = True
        # 弹窗此刻仍然开着：只让已打开的弹窗原地重绘。
        # 绝不能再 invoke 一个新弹窗，否则每点一个键就叠加一个窗口。
        for area in self._redraw_areas(context):
            try:
                area.tag_redraw()
            except Exception:
                pass
            for region in list(getattr(area, "regions", None) or []):
                try:
                    region.tag_redraw()
                except Exception:
                    pass
        # 标签重绘只覆盖普通区域，弹窗本身需要一次窗口级重绘；延迟到本步结束后执行
        try:
            bpy.app.timers.register(_force_key_picker_redraw, first_interval=0.01)
        except Exception:
            pass
        return {'FINISHED'}

    @staticmethod
    def _redraw_areas(context):
        """收集需要重绘的区域：当前区域 + 当前窗口的所有区域（含承载弹窗的区域）。"""
        areas = []
        area = getattr(context, "area", None)
        if area is not None:
            areas.append(area)
        window = getattr(context, "window", None)
        screen = getattr(window, "screen", None) if window is not None else None
        for candidate in list(getattr(screen, "areas", None) or []):
            areas.append(candidate)
        return areas


class SSMT_OT_KeyPicker(bpy.types.Operator):
    bl_idname = "ssmt.key_picker"
    bl_label = "快捷键选择"
    bl_options = {'REGISTER', 'INTERNAL'}
    bl_description = "点选图形键盘设置快捷键；Ctrl/Shift/Alt 可多选组成组合键"

    node_name: bpy.props.StringProperty()
    node_tree_name: bpy.props.StringProperty()
    target_prop: bpy.props.StringProperty()

    @staticmethod
    def _find_node(tree_name, node_name):
        tree = None
        node_groups = getattr(bpy.data, "node_groups", None)
        getter = getattr(node_groups, "get", None)
        if getter and tree_name:
            tree = getter(tree_name)
        if tree is None and tree_name:
            for collection_name in ("scenes", "materials", "worlds"):
                for owner in getattr(bpy.data, collection_name, []) or []:
                    candidate = getattr(owner, "node_tree", None)
                    if candidate is not None and candidate.name == tree_name:
                        tree = candidate
                        break
                if tree is not None:
                    break
        nodes = getattr(tree, "nodes", None)
        getter = getattr(nodes, "get", None)
        return getter(node_name) if getter and node_name else None

    def invoke(self, context, event):
        wm = context.window_manager
        current = ""
        picked = bool(getattr(wm, "ssmt_kp_just_picked", False))
        if picked and getattr(wm, "ssmt_kp_prop", "") == self.target_prop:
            current = getattr(wm, "ssmt_kp_combo", "") or ""
        else:
            node = self._find_node(self.node_tree_name, self.node_name)
            if node is not None and self.target_prop:
                current = getattr(node, self.target_prop, "") or ""
        wm.ssmt_kp_just_picked = False
        wm.ssmt_kp_prop = self.target_prop
        wm.ssmt_kp_node = self.node_name
        wm.ssmt_kp_tree = self.node_tree_name
        wm.ssmt_kp_combo = key_picker_format(*key_picker_parse(current))
        return wm.invoke_props_dialog(self, width=1000)

    def execute(self, context):
        wm = context.window_manager
        node = self._find_node(self.node_tree_name, self.node_name)
        if node is None or not self.target_prop:
            self.report({'WARNING'}, "无法找到形态键扩展配置节点")
            return {'CANCELLED'}
        setattr(node, self.target_prop, getattr(wm, "ssmt_kp_combo", "") or "")
        wm.ssmt_kp_just_picked = False
        return {'FINISHED'}

    def draw(self, context):
        wm = context.window_manager
        mods, key = key_picker_parse(getattr(wm, "ssmt_kp_combo", ""))
        layout = self.layout
        column = layout.column(align=True)
        for row_def in KEY_PICKER_MAIN_ROWS:
            self._draw_key_row(column, row_def, mods, key)
        column.separator()
        # 编辑键区 + 方向键区（左列）与小键盘区（右列）并排，模拟真实键盘右侧分区
        split = column.split(factor=0.5)
        nav_column = split.column(align=True)
        numpad_column = split.column(align=True)
        for row_def in KEY_PICKER_EDITING_ROWS:
            self._draw_key_row(nav_column, row_def, mods, key)
        nav_column.separator()
        for row_def in KEY_PICKER_ARROW_ROWS:
            self._draw_key_row(nav_column, row_def, mods, key)
        for row_def in KEY_PICKER_NUMPAD_ROWS:
            self._draw_key_row(numpad_column, row_def, mods, key)
        column.separator()
        self._draw_key_row(column, KEY_PICKER_MOUSE_ROW, mods, key)
        column.separator()
        box = layout.box()
        box.label(text=f"当前组合键：{key_picker_display(mods, key)}", icon='KEYINGSET')
        row = box.row(align=True)
        clear_op = row.operator("ssmt.key_picker_pick", text="清除", icon='X')
        self._fill_pick_op(clear_op, "", "clear")

    def _fill_pick_op(self, op, token, action="pick"):
        op.token = token
        op.action = action
        op.node_name = self.node_name
        op.node_tree_name = self.node_tree_name
        op.target_prop = self.target_prop

    def _draw_key_row(self, layout, row_def, mods, key):
        # 按真实键盘键宽分配：逐键 split，factor = 本键宽 / 行内剩余宽度。
        # 最后一个键直接占满剩余空间（split 不能取 factor=1.0）。
        row = layout.row(align=True)
        total_width = sum(max(float(width), 0.01) for _label, _token, width in row_def)
        remaining = total_width
        last_index = len(row_def) - 1
        for index, (label, token, width) in enumerate(row_def):
            width = max(float(width), 0.01)
            if index >= last_index or remaining <= width:
                cell = row
            else:
                factor = min(max(width / remaining, 0.01), 0.99)
                split = row.split(factor=factor, align=True)
                cell = split.column(align=True)
                row = split.column(align=True)
                remaining = max(remaining - width, 0.01)
            if not token:
                cell.label(text="")  # 方向键区占位，只留空
                continue
            if key_picker_is_modifier(token):
                selected = token in mods
            else:
                selected = key.strip().lower() == token.lower()
            op = cell.operator("ssmt.key_picker_pick", text=f"[{label}]" if selected else label)
            self._fill_pick_op(op, token)


class SSMTNode_PostProcess_ShapeKeyExt(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_ShapeKeyExt'
    bl_label = '形态键扩展配置'
    bl_description = '为形态键配置节点添加自动播放和分组功能（前置需连接形态键配置节点）'

    # ---- 配置行专有标识（生成时留在 ini 中，供「应用设置到Mod」精准定位与原地更新）----
    PANEL_TAG = "ShapeKeyExt"
    BLOCK_BEGIN = "; @@ShapeKeyExt:BLOCK:BEGIN:{ns}@@"
    BLOCK_END = "; @@ShapeKeyExt:BLOCK:END:{ns}@@"
    SLIDERCONST_BEGIN = "; @@ShapeKeyExt:SLIDERCONST:BEGIN@@"
    SLIDERCONST_END = "; @@ShapeKeyExt:SLIDERCONST:END@@"
    SLIDER_PRESENT_BEGIN = "; ========== SLIDER PANEL CUSTOM LOGIC (appended) =========="
    SLIDER_PRESENT_END = "; ========== SLIDER PANEL CUSTOM LOGIC END (appended) =========="
    DUP_GUARD = "; @@ShapeKeyExt:BLOCK:BEGIN:"

    play_group_settings: bpy.props.CollectionProperty(type=ShapeKeyPlayGroupSettings)
    active_group_index: bpy.props.IntProperty(
        name="编辑分组",
        description="当前正在编辑的分组。点击分组标题左侧的复选框可切换",
        default=1,
        min=1,
        update=lambda self, ctx: self._on_active_group_changed(self.active_group_index)
    )
    play_group_entries: bpy.props.CollectionProperty(type=ShapeKeyPlayGroupItem)
    auto_play_toggle_key: bpy.props.StringProperty(
        name="自动播放快捷键",
        description="游戏内切换「自动播放开/关」的快捷键，默认空格",
        default="space",
    )
    auto_play_key_global: bpy.props.BoolProperty(
        name="全局生效",
        description="勾选后快捷键在任何时候都生效；不勾选则只在滑块面板显示时生效",
        default=False,
    )

    show_unassigned: bpy.props.BoolProperty(
        name="显示未分组",
        description="是否在面板底部展开「未分组形态键」列表。未分组的形态键在 mod 中不生效",
        default=True,
    )

    # ==========================================
    # 【已合并】滑块面板-自定义 设置
    # ==========================================
    use_slider_panel: bpy.props.BoolProperty(
        name="启用滑块面板",
        description="是否在 mod 中生成游戏内滑块面板 UI。关闭后只保留自动播放逻辑，不显示面板",
        default=True,
    )
    create_cumulative_backup: bpy.props.BoolProperty(
        name="创建累积备份",
        description="每次生成/刷新时是否自动备份 mod ini 文件（避免误操作丢失数据）",
        default=True,
    )
    help_key: bpy.props.StringProperty(
        name="显示/隐藏面板",
        description="游戏内显示或隐藏滑块面板的快捷键，默认 home",
        default="home",
    )
    reset_key: bpy.props.StringProperty(
        name="重置位置",
        description="游戏内把滑块面板位置、缩放恢复默认的快捷键，默认 Ctrl+Home",
        default="ctrl home",
    )
    zoom_in_key: bpy.props.StringProperty(
        name="放大",
        description="游戏内放大滑块面板的快捷键，默认上方向键",
        default="up",
    )
    zoom_out_key: bpy.props.StringProperty(
        name="缩小",
        description="游戏内缩小滑块面板的快捷键，默认下方向键",
        default="down",
    )
    drag_key: bpy.props.StringProperty(
        name="拖拽键",
        description="游戏内拖动滑块面板的鼠标按键，默认鼠标左键",
        default="VK_LBUTTON",
    )

    slider_height: bpy.props.FloatProperty(
        name="滑块高度",
        description="游戏内滑块的高度（屏幕高度比例）。0.042 约等于屏幕高度的 4.2%",
        default=0.042,
        min=0.001,
        max=1.0,
        precision=4,
    )
    button_height: bpy.props.FloatProperty(
        name="按钮高度",
        description="游戏内播放/暂停按钮的高度（屏幕高度比例）",
        default=0.045,
        min=0.001,
        max=1.0,
        precision=4,
    )
    panel_min_height: bpy.props.FloatProperty(
        name="面板最小高度",
        description="滑块面板的最小高度。滑块数量少时会用这个值撑开面板，避免面板太扁",
        default=0.75,
        min=0.01,
        max=1.0,
        precision=4,
    )

    def _update_target_object(self, context):
        self._update_from_object()

    target_object: bpy.props.StringProperty(
        name="目标物体",
        description="用于自动解析滑块面板生效的哈希值和 IndexCount（从场景中选一个物体）",
        default="",
        update=_update_target_object,
    )
    detect_hash: bpy.props.StringProperty(
        name="哈希值",
        description="滑块面板生效的哈希值，通常从目标物体名称自动解析",
        default="",
    )
    detect_index_count: bpy.props.StringProperty(
        name="IndexCount",
        description="滑块面板生效的 IndexCount，通常从目标物体名称自动解析",
        default="",
    )

    background_image: bpy.props.StringProperty(
        name="背景图片",
        description="自定义滑块面板的背景图（留空则用内置默认背景）",
        subtype='FILE_PATH',
        default="",
    )
    slider_handle_image: bpy.props.StringProperty(
        name="滑块图片",
        description="自定义滑块柄图片（留空则用内置默认滑块）",
        subtype='FILE_PATH',
        default="",
    )
    left_bar_image: bpy.props.StringProperty(
        name="左进度条图片",
        description="滑块左侧已填充部分的图片（留空则用内置默认）",
        subtype='FILE_PATH',
        default="",
    )
    right_bar_image: bpy.props.StringProperty(
        name="右进度条图片",
        description="滑块右侧未填充部分的图片（留空则用内置默认）",
        subtype='FILE_PATH',
        default="",
    )
    button_image: bpy.props.StringProperty(
        name="按钮图片",
        description="播放/暂停按钮的默认图片（分组没有单独设置图标时使用）",
        subtype='FILE_PATH',
        default="",
    )

    # ---- 文字按钮样式（播放/暂停按钮，用分组备注生成；背景/边框紧贴文字，内边距极小不发糊）----
    button_bg_color: bpy.props.FloatVectorProperty(
        name="按钮背景色",
        description="文字按钮的背景颜色",
        subtype='COLOR',
        default=(0.16, 0.22, 0.32),
        min=0.0,
        max=1.0,
        size=3,
    )
    button_border_color: bpy.props.FloatVectorProperty(
        name="按钮边框色",
        description="文字按钮的边框颜色",
        subtype='COLOR',
        default=(0.59, 0.75, 0.94),
        min=0.0,
        max=1.0,
        size=3,
    )
    button_border_width: bpy.props.IntProperty(
        name="按钮边框宽度",
        description="文字按钮的边框宽度（像素）。0 = 无边框",
        default=2,
        min=0,
        max=20,
    )
    button_opacity: bpy.props.FloatProperty(
        name="按钮透明度",
        description="文字按钮的整体不透明度。1.0 = 完全不透明，0.0 = 完全透明",
        default=0.9,
        min=0.0,
        max=1.0,
        precision=2,
    )
    # ---- 面板背景样式（圆角 + 边框，未自定义背景图时生效）----
    background_corner_radius: bpy.props.IntProperty(
        name="背景圆角",
        description="面板背景的圆角半径。0 = 直角，100 = 最大圆角（约半圆）",
        default=5,
        min=0,
        max=100,
    )
    background_border_color: bpy.props.FloatVectorProperty(
        name="背景边框色",
        description="面板背景的边框颜色",
        subtype='COLOR',
        default=(0.59, 0.75, 0.94),
        min=0.0,
        max=1.0,
        size=3,
    )
    background_border_width: bpy.props.IntProperty(
        name="背景边框宽度",
        description="面板背景的边框宽度（像素）。0 = 无边框",
        default=3,
        min=0,
        max=20,
    )
    background_opacity: bpy.props.FloatProperty(
        name="背景透明度",
        description="面板背景的整体不透明度。1.0 = 完全不透明，0.0 = 完全透明",
        default=0.85,
        min=0.0,
        max=1.0,
        precision=2,
    )

    panel_default_scale: bpy.props.FloatProperty(
        name="面板默认缩放",
        description="面板默认显示的缩放比例（1.0 = 原始大小；可在游戏内用放大/缩小键再调节）",
        default=1.0, min=0.1, max=5.0, precision=2
    )
    check_hash: bpy.props.StringProperty(
        name="检测Hash值",
        description="备用哈希值。当「哈希值」为空时使用这个值来判断面板是否生效",
        default="",
    )
    match_index_count: bpy.props.IntProperty(
        name="Match Index Count",
        description="备用 IndexCount。当「IndexCount」为空时使用这个值来判断面板是否生效",
        default=0,
        min=0,
    )

    # ---- 原地刷新 - 实例标识与上次导出路径 ----
    namespace: bpy.props.StringProperty(
        name="命名空间",
        description="节点实例的唯一标识（自动生成，不需要手动修改）",
        default="",
        options={'HIDDEN'},
    )
    last_mod_ini_path: bpy.props.StringProperty(
        name="上次导出INI",
        description="上次导出/刷新的 mod ini 文件路径（自动记录）",
        default="",
        options={'HIDDEN'},
    )
    ini_file_path: bpy.props.StringProperty(
        name="INI文件",
        description="手动指定要刷新的 mod ini 文件。留空则自动使用「上次导出」记录的文件",
        subtype='FILE_PATH',
        default="",
    )

    def _ensure_namespace(self):
        """生成/返回节点实例唯一命名空间（仅用于标识注释，不改动 mod 业务变量）。"""
        if not self.namespace:
            self.namespace = "skx_" + uuid.uuid4().hex[:8]
        return self.namespace

    def copy(self, node):
        # 复制节点时重置命名空间，避免共用同一套标识注释
        self.namespace = ""

    def init(self, context):
        super().init(context)
        self.width = 700
        if len(self.play_group_settings) == 0:
            s = self.play_group_settings.add()
            s.group_index = 1
            # 全新节点：使用新语义（区间数值 = 相对单位速度的百分比）
            s.speed_percent_mode = True

    def _find_group_setting(self, group_index):
        for s in self.play_group_settings:
            if s.group_index == group_index: return s
        return None

    def _on_active_group_changed(self, group_index):
        """active_group_index 变化时的回调。Blender 要求 property update 回调返回 None。"""
        self._ensure_group_setting(group_index)

    def _ensure_group_setting(self, group_index):
        s = self._find_group_setting(group_index)
        if s is None:
            s = self.play_group_settings.add()
            s.group_index = group_index
            # 新建分组使用新语义；升级前保存的老分组 speed_percent_mode 保持 False，
            # 由 _interval_step_value 按旧的「每帧步进数」解释，播放速度与升级前一致。
            s.speed_percent_mode = True
            if len(s.speed_intervals) == 0:
                interval = s.speed_intervals.add()
                interval.start = 0.0
                interval.end = 100.0
                interval.base_step = 100
        return s

    @classmethod
    def split_anim_driver_block_content(cls, content):
        """Extract a complete animation-driver block from the top of an INI file.

        Mirrors SSMTNode_PostProcess_Base.split_anim_driver_block_content on
        main so this branch works standalone (the method was added to the base
        class after this PR forked).
        """
        text = str(content or "")
        lines = text.splitlines(keepends=True)
        start_index = next(
            (index for index, line in enumerate(lines)
             if getattr(cls, "ANIM_DRIVER_SECTION_MARKER_START", "; --- ANIMATION DRIVER SECTION ---") in line),
            None,
        )
        if start_index is None:
            return "", text
        end_marker = getattr(cls, "ANIM_DRIVER_SECTION_MARKER_END", "; --- END ANIMATION DRIVER SECTION ---")
        end_index = next(
            (index for index in range(start_index + 1, len(lines)) if end_marker in lines[index]),
            None,
        )
        if end_index is None:
            return "", text
        driver_content = "".join(lines[start_index:end_index + 1])
        remaining_content = "".join(lines[end_index + 1:]).lstrip("\r\n")
        return driver_content, remaining_content

    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict()
        current_section = None
        preserved_tail_content = ""
        preserved_driver_content = ""
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            preserved_driver_content, content = self.split_anim_driver_block_content(content)
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped
                    if current_section not in sections: sections[current_section] = []
                    continue
                if not stripped: continue
                if current_section: sections[current_section].append(line)
        except Exception:
            return None
        return sections, preserved_tail_content, preserved_driver_content

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content="", preserved_driver_content=""):
        try:
            with open(ini_file_path, 'w', encoding='utf-8') as f:
                if preserved_driver_content:
                    f.write(preserved_driver_content)
                    if not preserved_driver_content.endswith('\n'):
                        f.write('\n')
                    f.write('\n')
                for section_name, lines in sections.items():
                    if section_name.startswith(';;'): f.write(section_name + '\n')
                    else: f.write(section_name + '\n')
                    for line in lines: f.write(line + '\n')
                    f.write('\n')
                if preserved_tail_content:
                    f.write('\n' + preserved_tail_content)
        except Exception as e:
            print(f"写入INI文件失败: {e}")

    def _scan_freq_vars_from_ini(self, sections):
        freq_vars = OrderedDict()
        if '[Constants]' not in sections: return freq_vars
        pattern = re.compile(r'^\s*global\s+persist\s+(\$Freq_[^\s=]+)\s*=\s*([\d.]+)')
        prev_line = ""
        print("[ShapeKeyExt] ===== 变量名 <-> 形态键标签 对照表 =====")
        for line in sections['[Constants]']:
            m = pattern.match(line)
            if m:
                var_name = m.group(1)
                default_val = m.group(2)
                name_match = re.search(r";\s*控制形态键\s+'([^']+)'\s*的强度", prev_line)
                label = name_match.group(1).strip() if name_match else var_name
                freq_vars[var_name] = {"default": default_val, "label": label}
                print(f"[ShapeKeyExt]   {var_name}  ->  '{label}'")
            prev_line = line
        print(f"[ShapeKeyExt] ===== 共 {len(freq_vars)} 个形态键变量 =====")
        return freq_vars

    def _scan_shapekey_names_from_classification(self):
        text_obj = next((t for t in bpy.data.texts if "Shape_Key_Classification" in t.name), None)
        if not text_obj: return []
        names = set()
        for line in text_obj.as_string().splitlines():
            m = re.search(r'名称:\s*(.+)', line)
            if m: names.add(m.group(1).strip())
        return sorted(names)

    def _scan_shapekey_names_from_variable_items(self):
        if not self.inputs[0].is_linked: return []
        upstream = self.inputs[0].links[0].from_node
        if upstream.bl_idname != 'SSMTNode_PostProcess_ShapeKey': return []
        items = getattr(upstream, "shapekey_variable_items", None)
        if not items: return []
        return [item.shape_key_name for item in items if item.shape_key_name.strip()]

    def _scan_shapekey_name_to_var_map(self):
        """从上游形态键配置节点读取 {形态键中文名: 变量名(不含$)} 映射。

        用于「未分配形态键屏蔽」：需要把中文形态键名解析到实际 INI 变量名，
        因为变量名可能经过 cjk_to_ascii（拼音/uXXXX）或用户手动改名，
        不能简单从中文名推导。
        """
        if not self.inputs[0].is_linked:
            return {}
        upstream = self.inputs[0].links[0].from_node
        if upstream.bl_idname != 'SSMTNode_PostProcess_ShapeKey':
            return {}
        items = getattr(upstream, "shapekey_variable_items", None)
        if not items:
            return {}
        result = {}
        for item in items:
            name = str(getattr(item, "shape_key_name", "") or "").strip()
            if not name:
                continue
            var = str(getattr(item, "custom_variable_name", "") or "").strip()
            if not var:
                var = str(getattr(item, "assigned_variable_name", "") or "").strip()
            if var.startswith("$"):
                var = var[1:]
            if var:
                result[name] = var
        return result

    # ---- 未分配形态键屏蔽标记（三层统一前缀，便于幂等恢复）----
    UNASSIGNED_MASK_TAG = "[未分配-已屏蔽]"
    # 旧格式（无变量名），仍能识别并还原，保证已生成过的 mod 不卡死
    UNASSIGNED_MASK_PREFIX = "; [未分配-已屏蔽] "
    # 新格式：把变量名写进标记，还原时按“身份”判定而不是无条件还原
    UNASSIGNED_MASK_PREFIX_FMT = "; [未分配-已屏蔽:{token}] "
    UNASSIGNED_MASK_HEAD = "; [未分配-已屏蔽"
    # 被屏蔽的 shader 槽位额外写一行**有效**归零行：IniParams 是粘滞的，同槽位可能还有
    # 拖拽交互 / UV 偏移等模块每帧写入，只注释原行并不能保证 Shader 读到 0。
    # 归零行自己带变量名标记，与它的屏蔽行同生共死（还原时按身份一起保留/一起删除）。
    UNASSIGNED_ZERO_TAG = "[未分配-已屏蔽-归零]"
    # 行内匹配用「不带右括号」的头：带 token 时右括号在 token 之后
    UNASSIGNED_ZERO_HEAD = "[未分配-已屏蔽-归零"
    UNASSIGNED_ZERO_LINE_FMT = "{indent}{index} = 0 ; [未分配-已屏蔽-归零:{token}]"

    @classmethod
    def _parse_masked_token(cls, line):
        """解析屏蔽行里的变量名。

        Returns:
            None: 不是屏蔽行；
            "":   旧格式屏蔽行（标记里没有变量名，无法判定身份）；
            其他: 屏蔽时记录的变量名。
        """
        if not line.startswith(cls.UNASSIGNED_MASK_HEAD):
            return None
        end = line.find("] ")
        if end == -1:
            return None
        inner = line[len("; ["):end]  # "未分配-已屏蔽" 或 "未分配-已屏蔽:token"
        _tag, _, token = inner.partition(":")
        return token.strip()

    @classmethod
    def _parse_zero_token(cls, line):
        """解析归零行里的变量名（旧实现没有 token 时返回 ""，非归零行返回 None）。"""
        if cls.UNASSIGNED_ZERO_HEAD not in line:
            return None
        inner = line.split(cls.UNASSIGNED_ZERO_HEAD, 1)[1]
        _sep, _, token = inner.partition(":")
        return token.split("]", 1)[0].strip()

    @classmethod
    def _strip_mask_prefix(cls, line):
        """去掉屏蔽标记，还原成原始行（保留原有缩进）。"""
        end = line.find("] ")
        if end == -1:
            return line
        return line[end + 2:]

    @staticmethod
    def _normalize_token(value):
        token = str(value or "").strip()
        return token[1:] if token.startswith("$") else token

    def _unmask_all_shapekeys(self, sections, keep_masked=None, active_vars=None):
        """按“身份”处理上一次的屏蔽标记。

        每次 execute_postprocess 生成时都先调用（无论导出/刷新）：

        - 带变量名的屏蔽行（新格式）：变量名仍在本次屏蔽集合 ``keep_masked`` 里 →
          保持注释；不在屏蔽集合、但仍是当前有效的形态键变量（``active_vars``）→
          还原为有效行；两者都不是（变量已改名/废弃）→ **保守保持屏蔽**，
          避免把旧名残行悄悄复活成有效行（这正是 PR 描述里“改名后屏蔽失效”的坑）。
        - 旧格式屏蔽行（标记里没有变量名）：无法判定身份，按旧行为还原。
        - 无任何上下文（上游节点未连线、解析不出变量名）：按旧行为还原，
          由调用方打印 fail-open 警告。

        采用「固定前缀整行还原」（而不是正则吃空白）以保持原始缩进；
        屏蔽时补写的归零行按行尾标记整行删除，不参与还原。

        Returns:
            (restored, kept): 还原的有效行数与保持屏蔽的行数（都不含归零行）。
        """
        keep = {self._normalize_token(t) for t in (keep_masked or ()) if self._normalize_token(t)}
        active = {self._normalize_token(t) for t in (active_vars or ()) if self._normalize_token(t)}
        # 有上下文才敢“保守保持屏蔽”；什么都没有时退回旧的 fail-open 行为
        has_context = bool(keep or active)
        restored_count = 0
        kept_count = 0
        for sec_name in list(sections.keys()):
            new_lines = []
            for line in sections[sec_name]:
                zero_token = self._parse_zero_token(line)
                if zero_token is not None:
                    # 归零行跟随它的屏蔽行：token 仍在屏蔽集合（或身份未知且保守）
                    # 就保留，否则删除。
                    if zero_token and (zero_token in keep or (has_context and zero_token not in active)):
                        new_lines.append(line)
                    continue
                token = self._parse_masked_token(line)
                if token is None:
                    new_lines.append(line)
                    continue
                if token and token in keep:
                    new_lines.append(line)
                    kept_count += 1
                    continue
                if token and token in active:
                    new_lines.append(self._strip_mask_prefix(line))
                    restored_count += 1
                    continue
                if token and has_context:
                    # 变量已改名/不再属于本节点：保持屏蔽，不让旧行复活
                    new_lines.append(line)
                    kept_count += 1
                    continue
                new_lines.append(self._strip_mask_prefix(line))
                restored_count += 1
            sections[sec_name] = new_lines
        return restored_count, kept_count

    @staticmethod
    def _extract_variable_token(match):
        """取正则捕获到的变量 token（形如 ``$Freq_xxx``）并去掉前导 ``$``。"""
        token = (match.group(1) or "").strip() if match else ""
        return token[1:] if token.startswith("$") else token

    def _mask_unassigned_shapekeys(self, sections, unassigned_vars):
        """把未分配形态键的引用注释掉：常量声明 / Present 赋值 / Shader 输入。

        - [Constants]: "global [persist] $var = ..." 整行加带变量名的屏蔽标记。
        - [Present] : 所有 "$var = ..." 形式的赋值行加屏蔽标记。
        - [CustomShader_*_Anim]: "x<N> = $var" 行加屏蔽标记，并紧接一行**有效**的
          "x<N> = 0 ; [归零标记]"——IniParams 是粘滞的、同槽位还有其它模块在写，
          只注释原行并不能保证 Shader 读到 0（注释行本身不写寄存器）。
          归零行在 _unmask_all_shapekeys 中按行尾标记删除，不污染可逆性。

        注意：变量名**不要求**以 ``Freq_`` 开头——「导出变量」是自由文本，
        用户可以填任意名字（中文名经 cjk_to_ascii 后也没有 Freq_ 前缀），
        所以这里按解析出的 token 匹配，而不是按前缀匹配。

        Returns:
            (newly_masked, already_masked): 本次新加标记的行数、以及上次已屏蔽且仍然
            生效（保持原样）的行数。调用方据此如实汇报，避免“匹配不到却照样打印已屏蔽”。
        """
        if not unassigned_vars:
            return 0, 0, []
        target_set = {self._normalize_token(name) for name in unassigned_vars}
        target_set.discard("")
        if not target_set:
            return 0, 0, []
        zero_line_fmt = self.UNASSIGNED_ZERO_LINE_FMT
        masked_lines = 0
        already_masked = 0
        matched_tokens = set()

        def _mask_prefix(token):
            return self.UNASSIGNED_MASK_PREFIX_FMT.format(token=token)

        def _mask_line(raw_line, token):
            return _mask_prefix(token) + raw_line

        # 1) [Constants]：注释 global [persist] $var = ...
        decl_pattern = re.compile(r'^\s*global(?:\s+persist)?\s+(\$\S+)\s*=')
        if '[Constants]' in sections:
            new_lines = []
            for line in sections['[Constants]']:
                token = self._extract_variable_token(decl_pattern.match(line))
                if token in target_set:
                    new_lines.append(_mask_line(line, token))
                    masked_lines += 1
                    matched_tokens.add(token)
                    continue
                kept_token = self._parse_masked_token(line)
                if kept_token and kept_token in target_set:
                    already_masked += 1
                    matched_tokens.add(kept_token)
                new_lines.append(line)
            sections['[Constants]'] = new_lines

        # 2) [Present]：注释 $var = ... 赋值
        assign_pattern = re.compile(r'^\s*(\$\S+)\s*=')
        if '[Present]' in sections:
            new_lines = []
            for line in sections['[Present]']:
                token = self._extract_variable_token(assign_pattern.match(line))
                if token in target_set:
                    new_lines.append(_mask_line(line, token))
                    masked_lines += 1
                    matched_tokens.add(token)
                    continue
                kept_token = self._parse_masked_token(line)
                if kept_token and kept_token in target_set:
                    already_masked += 1
                    matched_tokens.add(kept_token)
                new_lines.append(line)
            sections['[Present]'] = new_lines

        # 3) [CustomShader_*_Anim]：注释 x<N> = $var，并补一行**有效**归零
        shader_pattern = re.compile(r'^\s*(x\d+)\s*=\s*(\$\S+)')
        for sec_name in list(sections.keys()):
            if not (sec_name.startswith('[CustomShader_') and sec_name.endswith('_Anim]')):
                continue
            new_lines = []
            for line in sections[sec_name]:
                m = shader_pattern.match(line)
                token = ""
                if m:
                    token = m.group(2)[1:] if m.group(2).startswith("$") else m.group(2)
                if m and token in target_set:
                    indent = line[:len(line) - len(line.lstrip())]
                    new_lines.append(_mask_line(line, token))
                    new_lines.append(
                        zero_line_fmt.format(indent=indent, index=m.group(1), token=token)
                    )
                    masked_lines += 1
                    matched_tokens.add(token)
                    continue
                kept_token = self._parse_masked_token(line)
                if kept_token and kept_token in target_set:
                    # 上次已屏蔽：归零行由 _unmask_all_shapekeys 按身份保留，这里不重复添加
                    already_masked += 1
                    matched_tokens.add(kept_token)
                new_lines.append(line)
            sections[sec_name] = new_lines

        # 4) [Present] 的形态键强度签名：把未分配变量的项从表达式里删掉。
        # 上游形态键节点会把**所有**形态键（本节点判为未分配的也在内）塞进签名
        # `$ssmt_sk_sig_* = $Freq_a * 1 + $Freq_b * 3 + ...`，而这条签名正是
        # `run = CustomShader_*_Anim` 的触发条件。不删掉这些项，未分配形态键就还挂在
        # 分组动画（自动播放 / 滑块）的驱动链上——它们的 `global persist` 声明已被注释，
        # 参与签名只剩副作用，游戏内表现为未分组形态键跟着分组一起动。
        # 权重是互质数、求和本身与顺序无关，删掉恒为 0 的项不影响变化检测。
        sig_pattern = re.compile(r'^(\s*)(\$ssmt_sk_sig_[^\s=]+)\s*=\s*(.+)$')
        if '[Present]' in sections:
            new_lines = []
            for line in sections['[Present]']:
                m = sig_pattern.match(line)
                if not m:
                    new_lines.append(line)
                    continue
                indent, sig_var, expr = m.group(1), m.group(2), m.group(3)
                sig_token = sig_var.lstrip('$')
                terms = [t.strip() for t in expr.split(' + ') if t.strip()]
                kept = []
                dropped = 0
                for term in terms:
                    term_match = re.match(r'^(\$[^\s*]+)', term)
                    token = (term_match.group(1)[1:] if term_match else "")
                    if token and token in target_set:
                        matched_tokens.add(token)
                        dropped += 1
                        continue
                    kept.append(term)
                if dropped == 0:
                    new_lines.append(line)
                    continue
                if not kept:
                    # 续行（`$sig = $sig + ...`）没有剩余项：整行丢弃；
                    # 首行（`$sig = 项...`）则保底成 `$sig = 0`，别让签名变量失去定义。
                    if any(t.lstrip('$') == sig_token for t in terms):
                        continue
                    kept = ["0"]
                elif len(kept) == 1 and kept[0].lstrip('$') == sig_token:
                    # 只剩 `$sig = $sig` 的自赋值续行，同样没有意义，丢弃
                    continue
                new_lines.append(f"{indent}{sig_var} = " + " + ".join(kept))
            sections['[Present]'] = new_lines

        # 一个匹配行都没有的未分配形态键：多半是变量名刚被刷新、ini 还没重新导出
        unmatched = sorted(target_set - matched_tokens)
        return masked_lines, already_masked, unmatched

    @classmethod
    def _collect_live_references(cls, sections, tokens):
        """找出仍以“有效行”（非注释）方式引用给定变量的位置。

        用于屏蔽之后的不变量校验：屏蔽只注释了「变量作左值」的行，若还有别的模块
        （例如滑块面板）以右值/读取方式引用这些变量，这里会显式报出来，
        而不是让 ini 静默引用一个已被注释掉声明的变量。

        Returns:
            list[(section, 1-based 行号, 行内容)]
        """
        names = []
        for token in tokens or ():
            name = cls._normalize_token(token)
            if name:
                names.append(name)
        if not names:
            return []
        patterns = [
            re.compile(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])") for name in names
        ]
        hits = []
        for sec_name, lines in sections.items():
            for index, line in enumerate(lines, 1):
                stripped = line.lstrip()
                if not stripped or stripped.startswith(";"):
                    continue
                if any(pattern.search(line) for pattern in patterns):
                    hits.append((sec_name, index, stripped))
        return hits

    @classmethod
    def _exclude_disabled_freq_params(cls, freq_params, exclude_vars):
        """从滑块参数集合里剔除被屏蔽的变量。

        Returns:
            (kept: set, skipped: list) —— skipped 已排序，便于日志输出。
        """
        excluded = {cls._normalize_token(v) for v in (exclude_vars or ())}
        excluded.discard("")
        if not excluded:
            return set(freq_params or ()), []
        skipped = sorted(v for v in (freq_params or ()) if cls._normalize_token(v) in excluded)
        kept = {v for v in (freq_params or ()) if v not in skipped}
        return kept, skipped

    # 滑块参数的来源声明（与 _apply_slider_panel 此前内联的扫描口径完全一致）
    SLIDER_FREQ_PARAM_PATTERN = re.compile(r'^\s*global(?:\s+persist)?\s+(\$Freq_[^\s=]+)')

    def _collect_unassigned_masked_vars(self):
        """未分配（分组为 0）的形态键名，以及它们对应的 ini 变量名。

        返回 ``(unassigned_names, masked_vars)``；``masked_vars`` 不带 `$` 前缀，
        与 ``_exclude_disabled_freq_params`` 的入出口径一致。「滑块面板排除未分配
        形态键」与「未分配形态键屏蔽」共用这一份判定，避免两处口径漂移。
        """
        unassigned_names = {
            entry.shape_key_name
            for entry in (getattr(self, "play_group_entries", None) or [])
            if entry.group_index == 0
        }
        name_to_var = self._scan_shapekey_name_to_var_map() if unassigned_names else {}
        masked_vars = {name_to_var[name] for name in unassigned_names if name in name_to_var}
        return unassigned_names, masked_vars

    def _collect_slider_freq_params(self, sections=None, exclude_vars=None):
        """收集**会变成滑块**的形态键参数（`_apply_slider_panel` 的唯一产出前提）。

        - 有 ``sections``：按 `[Constants]` 里的 `global $Freq_*` 声明取，即导出 /
          原地刷新时的真实来源；
        - 无 ``sections``（纯 UI 场景，供别的节点判断滑块面板会不会出现）：退回本节点
          上游的形态键变量表，判据与导出路径同源。

        返回 ``(kept, skipped)``，语义与 ``_exclude_disabled_freq_params`` 一致。
        """
        if sections is not None:
            candidates = set()
            for line in sections.get('[Constants]', []) or []:
                match = self.SLIDER_FREQ_PARAM_PATTERN.match(line)
                if match:
                    candidates.add(match.group(1))
        else:
            candidates = {
                f"${var}" for var in self._scan_shapekey_name_to_var_map().values() if var
            }
        return self._exclude_disabled_freq_params(candidates, exclude_vars)

    def will_emit_slider_panel(self, sections=None) -> bool:
        """本节点本次导出是否**真的**会写出形态键滑块面板。

        只看 ``use_slider_panel`` 是不够的：``_apply_slider_panel`` 在排除未分配形态键
        之后没有任何 ``$Freq_*`` 参数时会直接返回 False，此时 ini 里既没有滑块面板段落，
        也没有 ``$img0_x`` / ``$zoom0`` 等共用变量。依赖这些变量的节点（物体切换面板的
        附加模式）必须按本方法的结论决定能不能复用它们，否则会引用到不存在的变量。
        """
        if not bool(getattr(self, "use_slider_panel", False)):
            return False
        try:
            _unassigned_names, masked_vars = self._collect_unassigned_masked_vars()
            kept, _skipped = self._collect_slider_freq_params(sections, masked_vars)
        except AttributeError:
            # 节点未完整初始化（上游未连线等）时无法判定：按「会产出」处理（与引入本方法
            # 之前的行为一致），真正的产出前提由导出期的 ini 证据校验兜底。
            return True
        return bool(kept)

    def _build_var_to_group_map(self, freq_vars, shapekey_names):
        """构建 变量 -> 分组编号 的映射，并返回实际使用的分组列表。

        修复点（相对旧版）：
        - 默认分组改为 0（未分组），而不是 1；避免未在 play_group_entries 里
          出现的形态键被误归到分组 1，从而生成错误的分组同步逻辑。
        - 返回的 all_groups 只包含 > 0 的分组，杜绝生成 "$Freq_Group0" 这类
          虚假变量（此前会把未分组的变量也纳入 all_groups，导致后续生成
          "global persist $Freq_Group0 = 0.0" 并污染同步块）。
        """
        name_to_group = {}
        for name in shapekey_names:
            group = 0
            for entry in self.play_group_entries:
                if entry.shape_key_name == name:
                    group = entry.group_index
                    break
            name_to_group[name] = group
        var_to_group = {}
        for var_name, info in freq_vars.items():
            label = info["label"]
            group = name_to_group.get(label, 0)
            var_to_group[var_name] = group
        # 只保留真正存在的分组（排除 0 = 未分组）
        all_groups = sorted({g for g in name_to_group.values() if g > 0})
        return var_to_group, all_groups

    @staticmethod
    def _calc_unit_speed(setting):
        """计算单位速度：100% 速度时每帧推进的细分数。

        公式：单位速度 = 总细分 / (总时长 × 假设帧率)

        保留小数、不向上钳到 1：INI 侧的步进是累加式的
        （``$step_accum += $base_step * 速度%``，累加值 > 0 才推进 1 个细分），
        因此 0.25 这类步进会正确地每 4 帧推进一次。若钳到 1，像
        「30 细分 / 2 秒 / 60fps」这种常见配置（数学值 0.25）会变成 4 倍速播放，
        与用户填写的「总时长」不符。

        用户举例验证：2400 细分 / (2 秒 × 120fps) = 10 细分/帧。
        """
        try:
            frame_count = int(getattr(setting, "auto_playback_frame_count", 30) or 30)
            duration = float(getattr(setting, "auto_playback_duration", 2.0) or 2.0)
            fps = int(getattr(setting, "playback_fps", 60) or 60)
        except Exception:
            return 1.0
        total_frames = duration * fps
        if total_frames <= 0 or frame_count <= 0:
            return 1.0
        unit_speed = frame_count / total_frames
        # 下限留一个极小值：0 会让动画完全停滞
        return max(0.01, round(unit_speed, 4))

    @staticmethod
    def _interval_step_value(interval, unit_speed, speed_percent_mode):
        """区间在 100% 全局速度下每帧推进的细分数。

        - speed_percent_mode=True（新语义）：base_step 是相对单位速度的百分比；
        - speed_percent_mode=False（升级前保存的老工程）：base_step 就是旧的
          「每帧步进数」，直接返回，从而与升级前的播放速度完全一致。
        """
        try:
            base = float(getattr(interval, "base_step", 0) or 0)
        except Exception:
            base = 0.0
        if speed_percent_mode:
            return unit_speed * base / 100.0
        return base

    @staticmethod
    def _no_interval_fallback_step(unit_speed, speed_percent_mode):
        """没有任何变速区间时的兜底步进。

        老语义下与升级前一致（固定 10），新语义下等于单位速度。
        """
        return unit_speed if speed_percent_mode else 10.0

    @staticmethod
    def _tail_step_value(unit_speed, speed_percent_mode):
        """最后一个区间之后的兜底步进（老语义固定 1，新语义等于单位速度）。"""
        return unit_speed if speed_percent_mode else 1.0

    @staticmethod
    def _calc_auto_max_step_unroll(setting):
        """根据分组的变速区间 / 速度设置，自动计算单帧最大展开次数。

        原理：
          单帧最大推进帧数 = 区间最大每帧步进 × 全局最大速度百分比
          展开次数 = ceil(单帧最大推进帧数 × 1.5 安全余量)

        最终夹在 [10, 500] 之间：下限保证默认场景可播放，上限避免 INI 膨胀。
        """
        # 1) 单位速度（100% 速度下每帧推进的细分数）
        unit_speed = SSMTNode_PostProcess_ShapeKeyExt._calc_unit_speed(setting)
        percent_mode = bool(getattr(setting, "speed_percent_mode", True))

        # 2) 区间里最大的每帧步进（无区间时用兜底步进）
        max_step = 0.0
        for interval in setting.speed_intervals:
            step_value = SSMTNode_PostProcess_ShapeKeyExt._interval_step_value(
                interval, unit_speed, percent_mode
            )
            if step_value > max_step:
                max_step = step_value
        if max_step <= 0:
            max_step = SSMTNode_PostProcess_ShapeKeyExt._no_interval_fallback_step(
                unit_speed, percent_mode
            )
        max_step = max(
            max_step,
            SSMTNode_PostProcess_ShapeKeyExt._tail_step_value(unit_speed, percent_mode),
        )

        # 3) 全局最大速度百分比（考虑当前值，取更保守的最大值，且不低于 100）
        try:
            max_speed = max(
                int(setting.speed_percent),
                int(setting.speed_percent_max),
                100,
            )
        except Exception:
            max_speed = 100

        # 4) 单帧最大推进帧数
        max_per_tick = max_step * (max_speed / 100.0)

        # 5) 加 1.5 安全余量后向上取整
        needed = math.ceil(max_per_tick * 1.5)

        # 6) 夹在 [10, 500]
        needed = max(10, needed)
        needed = min(500, needed)

        return needed

    @staticmethod
    def _const_var_decl_pattern(var_name):
        """匹配 Constants 里 ``global [persist] $var = ...`` 声明的正则。"""
        return re.compile(r'^\s*global(?:\s+persist)?\s+' + re.escape(var_name) + r'\s*=')

    @classmethod
    def _upsert_const_var_line(cls, const_lines, var_name, new_line):
        """把 Constants 里 var_name 的声明写成 new_line：存在则替换，不存在才追加。

        旧写法是「``if var_name not in const_content`` 才追加」，导致改参数后重新
        导出时 ini 里的默认值仍是上一次的（例如 ``$frameEnd_group1 = 480`` 永远
        不会变成 4800）。替换时保留原行位置，避免 Constants 行序来回跳；同一变量
        若已有重复声明（历史 bug 产物），只保留第一行并删掉其余的。
        """
        pattern = cls._const_var_decl_pattern(var_name)
        matched_indexes = [
            i for i, line in enumerate(const_lines)
            if not line.strip().startswith(';') and pattern.match(line)
        ]
        if not matched_indexes:
            const_lines.append(new_line)
            return 'appended'
        const_lines[matched_indexes[0]] = new_line
        for i in reversed(matched_indexes[1:]):
            del const_lines[i]
        return 'updated'

    def _add_auto_playback_logic_for_group(self, sections, group_id, intensity_var, frame_count, cycle_mode, speed_percent_min, speed_percent_max, speed_intervals, max_unroll, unit_speed=1, speed_percent_mode=True, speed_percent=100):
        if '[Constants]' not in sections: sections['[Constants]'] = []
        const_lines = sections['[Constants]']
        prefix = f"group{group_id}"
        auto_enabled_var = f"$auto_play_enabled_{prefix}"
        step_accum_var = f"$step_accum_{prefix}"
        frame_var = f"$shapekey_frame_{prefix}"
        frame_end_var = f"$frameEnd_{prefix}"
        speed_percent_var = f"$speed_percent_{prefix}"
        speed_percent_min_var = f"$speed_percent_min_{prefix}"
        speed_percent_max_var = f"$speed_percent_max_{prefix}"
        step_frames_var = f"$step_frames_{prefix}"
        base_step_var = f"$base_step_{prefix}"
        norm_frame_var = f"$norm_frame_{prefix}"
        pingpong_dir_var = f"$pingpong_dir_{prefix}" if cycle_mode == 'PINGPONG' else None
        try:
            frame_count_text = str(int(frame_count))
        except (TypeError, ValueError):
            frame_count_text = str(frame_count)
        auto_vars = [
            f"global persist {auto_enabled_var} = 1", f"global {step_accum_var} = 0",
            f"global {frame_var} = 0", f"global persist {frame_end_var} = {frame_count_text}",
            f"global persist {speed_percent_var} = {speed_percent}", f"global persist {speed_percent_min_var} = {speed_percent_min}",
            f"global persist {speed_percent_max_var} = {speed_percent_max}", f"global {step_frames_var} = 0",
            f"global {base_step_var} = 0", f"global {norm_frame_var} = 0"
        ]
        if cycle_mode == 'PINGPONG': auto_vars.append(f"global {pingpong_dir_var} = 1")
        for var_line in auto_vars:
            var_name_only = var_line.split('=')[0].strip().split()[-1]
            self._upsert_const_var_line(const_lines, var_name_only, var_line)

        def _fmt_step(v):
            """把浮点步数格式化成 INI 可读的字符串（整数省略小数点）。"""
            if abs(v - round(v)) < 1e-9:
                return str(int(round(v)))
            s = f"{v:.4f}".rstrip('0').rstrip('.')
            return s if s else "0"

        present_code = []
        present_code.append(f"; @@ShapeKeyExt:PLAY:{self._ensure_namespace()}@@")
        present_code.append(f"; ========== AUTO PLAYBACK GROUP {group_id} (Variable Speed) ==========")
        present_code.append(f"if ({auto_enabled_var} == 1)")
        # 每帧强制把进度上限刷成「当前导出配置」：persist 变量在游戏端会把旧值
        # 保存在 d3dx_user.ini，只更新 ini 默认值不足以压住它，必须用非 persist
        # 赋值每帧覆盖，否则改大「总细分」后播放速度仍按旧的 frameEnd 走。
        present_code.append(f"    {frame_end_var} = {frame_count_text}")
        present_code.append(f"    {norm_frame_var} = {frame_var} / {frame_end_var}")
        intervals = sorted(speed_intervals, key=lambda x: x.start)
        if intervals:
            for i, interval in enumerate(intervals):
                start = interval.start / 100.0
                end = interval.end / 100.0
                # 新语义：base_step 是相对单位速度的百分比；
                # 老语义（升级前的工程）：base_step 就是每帧步进数，行为保持不变。
                actual_step = self._interval_step_value(
                    interval, unit_speed, speed_percent_mode
                )
                step_str = _fmt_step(actual_step)
                if i == 0: cond = f"{norm_frame_var} < {end}"
                else: cond = f"({norm_frame_var} >= {start} && {norm_frame_var} < {end})"
                present_code.append(f"    if ({cond})")
                present_code.append(f"        {base_step_var} = {step_str}")
                if i < len(intervals) - 1: present_code.append(f"    else")
                else:
                    present_code.append(f"    else")
                    # 时间线末端的兜底
                    tail_step = self._tail_step_value(unit_speed, speed_percent_mode)
                    present_code.append(f"        {base_step_var} = {_fmt_step(tail_step)}")
        else:
            # 无区间定义时的兜底
            fallback_step = self._no_interval_fallback_step(unit_speed, speed_percent_mode)
            present_code.append(f"    {base_step_var} = {_fmt_step(fallback_step)}")
        for _ in range(len(intervals)): present_code.append(f"    endif")
        present_code.append(f"    {step_frames_var} = {base_step_var} * {speed_percent_var} / 100.0")
        present_code.append(f"    {step_accum_var} = {step_accum_var} + {step_frames_var}")
        for _ in range(max_unroll):
            present_code.append(f"    if ({step_accum_var} > 0)")
            if cycle_mode == 'PINGPONG':
                present_code.append(f"        {frame_var} = {frame_var} + {pingpong_dir_var}")
                present_code.append(f"        if ({frame_var} >= {frame_end_var})")
                present_code.append(f"            {frame_var} = {frame_end_var}")
                present_code.append(f"            {pingpong_dir_var} = -1")
                present_code.append(f"        endif")
                present_code.append(f"        if ({frame_var} <= 0)")
                present_code.append(f"            {frame_var} = 0")
                present_code.append(f"            {pingpong_dir_var} = 1")
                present_code.append(f"        endif")
            else:
                present_code.append(f"        {frame_var} = {frame_var} + 1")
                present_code.append(f"        if ({frame_var} > {frame_end_var})")
                present_code.append(f"            {frame_var} = 0")
                present_code.append(f"        endif")
            present_code.append(f"        {step_accum_var} = {step_accum_var} - 1")
            present_code.append(f"    endif")
        if cycle_mode == 'REVERSE':
            present_code.append(f"    {intensity_var} = ({frame_end_var} - {frame_var}) / {frame_end_var}")
        else:
            present_code.append(f"    {intensity_var} = {frame_var} / {frame_end_var}")
        present_code.append(f"endif")
        present_code.append(f"; ========== END AUTO PLAYBACK GROUP {group_id} ==========")
        if '[Present]' not in sections: sections['[Present]'] = []
        present_lines = sections['[Present]']
        insert_pos = 0
        for i, line in enumerate(present_lines):
            stripped = line.strip()
            if stripped and not stripped.startswith(';'):
                insert_pos = i
                break
            else:
                insert_pos = i + 1
        for code_line in reversed(present_code): present_lines.insert(insert_pos, code_line)

    def _generate_remark_icon(self, text, dest_path, font_size=36,
                              font_family="msyh.ttc",
                              text_color=(1.0, 1.0, 1.0),
                              stroke_width=2,
                              stroke_color=(0.0, 0.0, 0.0),
                              with_style=True):
        """根据分组备注生成文字按钮图标。

        with_style=True：绘制紧贴文字的圆角背景 + 边框（内边距小，图片接近 1:1 不发糊）。
        with_style=False：透明背景仅文字（无背景/边框），用于未启用自动播放的分组。
        """
        try:
            if not PIL_AVAILABLE: return None

            text = text.replace('/', '\n').strip()
            if not text: return None

            def float_to_int_rgb(vals):
                return tuple(int(val * 255) for val in vals)

            text_rgb = float_to_int_rgb(text_color)
            stroke_rgb = float_to_int_rgb(stroke_color)

            font = None
            try:
                font = ImageFont.truetype(font_family, font_size)
            except:
                for f in ["msyh.ttc", "simsun.ttc", "simhei.ttf", "arial.ttf"]:
                    try:
                        font = ImageFont.truetype(f, font_size)
                        break
                    except:
                        continue
            if font is None:
                font = ImageFont.load_default()

            temp_img = Image.new('RGBA', (1, 1), (0, 0, 0, 0))
            temp_draw = ImageDraw.Draw(temp_img)
            bbox = temp_draw.multiline_textbbox((0, 0), text, font=font, align='center', spacing=6)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            if with_style:
                # 紧凑内边距：文字到背景边缘 6px + 边框宽，边框紧挨文字，图片不过大
                border_w = max(0, int(self.button_border_width))
                gap = 6
                padding = gap + border_w
            else:
                # 无背景/边框：透明背景仅文字，紧凑内边距（旧版机制，最锐利）
                border_w = 0
                padding = 8 + (stroke_width * 2)

            img_w = math.ceil(text_w + padding * 2)
            img_h = math.ceil(text_h + padding * 2)
            img = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            if with_style:
                # 按钮背景：圆角矩形紧贴文字 + 边框
                bg_rgb = float_to_int_rgb(self.button_bg_color)
                bd_rgb = float_to_int_rgb(self.button_border_color)
                btn_alpha = int(255 * max(0.0, min(1.0, self.button_opacity)))
                radius = min(6, img_h // 4)
                try:
                    draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius,
                                           fill=bg_rgb + (btn_alpha,),
                                           outline=bd_rgb + (btn_alpha,), width=border_w)
                except Exception:
                    draw.rectangle([0, 0, img_w - 1, img_h - 1], fill=bg_rgb + (btn_alpha,),
                                   outline=bd_rgb + (btn_alpha,))
            x = (img_w - text_w) / 2 - bbox[0]
            y = (img_h - text_h) / 2 - bbox[1]
            draw.multiline_text((x, y), text, font=font, fill=text_rgb + (255,),
                                align='center', spacing=6,
                                stroke_width=stroke_width, stroke_fill=stroke_rgb + (255,))
            img.save(dest_path)
            return dest_path
        except Exception as e:
            print(f"[形态键扩展] 生成文字图标失败: {e}")
            return None

    def _measure_image_aspect(self, dest_res_dir, std_name, fallback=1.0):
        """读取已复制到 res 的图片宽高比（用于按高度×比例推导宽度）。"""
        path = os.path.join(dest_res_dir, std_name)
        if os.path.exists(path) and PIL_AVAILABLE:
            try:
                with Image.open(path) as img:
                    w, h = img.size
                    if h > 0:
                        return w / h
            except Exception:
                pass
        return fallback

    def _generate_background_image(self, dest_path, panel_w, panel_h, use_existing=False):
        """生成/处理面板背景图（圆角 + 边框）。panel_w/panel_h 为面板在屏幕单位的宽高。

        use_existing=False：生成纯色圆角背景（默认 512 高）。
        use_existing=True：基于 dest_path 现有图（自定义背景）应用圆角遮罩 + 边框。
        """
        try:
            if not PIL_AVAILABLE:
                return None

            def float_to_int_rgb(vals):
                return tuple(int(val * 255) for val in vals)

            if use_existing and os.path.exists(dest_path):
                # 基于自定义背景图应用圆角/边框
                with Image.open(dest_path) as src:
                    img = src.convert('RGBA')
            else:
                # 生成纯色底图（按面板在 16:9 屏幕上的显示比例，保证圆角不变形）
                img_h = 512
                pixel_ratio = (panel_w / panel_h) * (1920.0 / 1080.0) if panel_h > 0 else 1.0
                img_w = max(64, int(round(img_h * pixel_ratio)))
                fill_rgb = float_to_int_rgb((0.05, 0.08, 0.12))
                img = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                d.rectangle([0, 0, img_w - 1, img_h - 1], fill=fill_rgb + (255,))

            img_w, img_h = img.size
            bd_rgb = float_to_int_rgb(self.background_border_color)
            alpha = int(255 * max(0.0, min(1.0, self.background_opacity)))

            # 圆角半径按图片实际尺寸比例（自定义大图圆角视觉一致）
            radius_px = int(min(img_w, img_h) * max(0, min(100, self.background_corner_radius)) / 100.0)
            # 边框宽度按 512 参考高缩放（自定义大图也可见）
            border_scale = max(1.0, img_h / 512.0)
            border_px = max(0, int(round(self.background_border_width * border_scale)))

            # 应用整体透明度
            if alpha < 255:
                r, g, b, a = img.split()
                a = a.point(lambda v: int(v * alpha / 255))
                img = Image.merge('RGBA', (r, g, b, a))

            # 圆角 alpha 遮罩
            mask = Image.new('L', (img_w, img_h), 0)
            md = ImageDraw.Draw(mask)
            if radius_px > 0:
                md.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius_px, fill=255)
            else:
                md.rectangle([0, 0, img_w - 1, img_h - 1], fill=255)
            r, g, b, a = img.split()
            a = Image.composite(a, Image.new('L', (img_w, img_h), 0), mask)
            img = Image.merge('RGBA', (r, g, b, a))

            # 边框（在圆角区域内）
            if border_px > 0:
                draw = ImageDraw.Draw(img)
                inset = max(0, border_px // 2)
                try:
                    draw.rounded_rectangle([inset, inset, img_w - 1 - inset, img_h - 1 - inset],
                                           radius=max(0, radius_px - inset),
                                           outline=bd_rgb + (alpha,), width=border_px)
                except Exception:
                    draw.rectangle([inset, inset, img_w - 1 - inset, img_h - 1 - inset],
                                   outline=bd_rgb + (alpha,), width=border_px)
            img.save(dest_path)
            return dest_path
        except Exception as e:
            print(f"[滑块面板] 生成背景图失败: {e}")
            return None

    # ==========================================
    # 【修复】添加自然排序的辅助函数
    # ==========================================
    def _natural_sort_key(self, s):
        """
        自然排序算法，将字符串中的数字转为整数进行智能比较。
        例如：Motion_Key_2 会排在 Motion_Key_19 前面。
        """
        return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', s)]

    # ==========================================
    # 原地更新：按专有标识移除本节点旧配置
    # ==========================================
    def _is_owned_section(self, sec_name, ns):
        """判断段落是否由本节点生成（形态键扩展 + 合并的滑块面板）。"""
        s = sec_name.strip()
        fixed = [
            f"[KeyToggleAutoPlay_{ns}]", f"[CommandListToggleAutoPlay_{ns}]",
            "[KeyHelp]", "[KeyResetPosition]", "[KeyZoomIn]", "[KeyZoomOut]", "[KeyMouseDrag]",
            "[CommandListZoomIn]", "[CommandListZoomOut]",
            "[ResourceImageToRender0]", "[ResourceSliderHandle]", "[ResourceLeftBar]", "[ResourceRightBar]",
            "[CustomShaderDraw]", "[TextureOverrideCheckHash]",
        ]
        if s in fixed:
            return True
        if s.startswith("[CommandListToggleAutoPlayGroup") and s.endswith(f"_{ns}]"):
            return True
        if s.startswith("[ResourcePlayPauseButton") and s.endswith("]"):
            return True
        return False

    def _remove_owned_config(self, sections, ns):
        """按专有标识移除本节点在 ini 中的旧配置（Constants 块、Present 块、自有段落）。"""
        # Constants：形态键块 + 滑块常量块
        const_pairs = [
            (self.BLOCK_BEGIN.format(ns=ns), self.BLOCK_END.format(ns=ns)),
            (self.SLIDERCONST_BEGIN, self.SLIDERCONST_END),
        ]
        if '[Constants]' in sections:
            new_lines = []
            skip_until = None
            for line in sections['[Constants]']:
                if skip_until is None:
                    for begin, end in const_pairs:
                        if begin in line:
                            skip_until = end
                            break
                    if skip_until is not None:
                        continue
                    new_lines.append(line)
                else:
                    if skip_until in line:
                        skip_until = None
            sections['[Constants]'] = new_lines

        # Present：移除插入的形态键同步块、自动播放块与滑块面板块（按块标识注释定位）
        if '[Present]' in sections:
            new_lines = []
            skip_state = None  # None / 'SYNC' / 'PLAY' / 'SLIDER'
            sync_marker = f"; @@ShapeKeyExt:SYNC:{ns}@@"
            play_marker = f"; @@ShapeKeyExt:PLAY:{ns}@@"
            for line in sections['[Present]']:
                if skip_state is None:
                    if sync_marker in line:
                        skip_state = 'SYNC'
                        continue
                    if play_marker in line:
                        skip_state = 'PLAY'
                        continue
                    if self.SLIDER_PRESENT_BEGIN in line:
                        skip_state = 'SLIDER'
                        continue
                    new_lines.append(line)
                else:
                    if skip_state == 'SYNC' and "结束组内变量同步" in line:
                        skip_state = None
                    elif skip_state == 'PLAY' and "END AUTO PLAYBACK GROUP" in line:
                        skip_state = None
                    elif skip_state == 'SLIDER' and self.SLIDER_PRESENT_END in line:
                        skip_state = None
            sections['[Present]'] = new_lines

        # 自有段落（整体删除）
        for sec_name in list(sections.keys()):
            if self._is_owned_section(sec_name, ns):
                del sections[sec_name]

    # ==========================================
    # 【已合并】滑块面板-自定义逻辑
    # ==========================================
    def _update_from_object(self):
        obj = bpy.data.objects.get(self.target_object)
        if not obj:
            return
        obj_name = obj.name
        match = re.search(r'([a-f0-9]{8})-([0-9]+)(?:-([0-9]+))?', obj_name)
        if match:
            self.detect_hash = match.group(1)
            self.detect_index_count = match.group(2)
        else:
            match2 = re.search(r'([a-f0-9]{8})', obj_name)
            if match2:
                self.detect_hash = match2.group(1)
                self.detect_index_count = ""
            else:
                print(f"[滑块面板] 无法从物体名称 '{obj_name}' 解析哈希和IndexCount")

    def _copy_default_image(self, std_name, dest_res_dir, source_asset_dir):
        default_path = os.path.join(source_asset_dir, std_name)
        dest_path = os.path.join(dest_res_dir, std_name)
        if os.path.exists(default_path):
            if not os.path.exists(dest_path):
                shutil.copy2(default_path, dest_path)
        else:
            print(f"警告: 默认图片不存在 {default_path}")

    def _scan_slider_group_info(self, sections):
        group_ids = set()
        if '[Constants]' in sections:
            const_text = "\n".join(sections['[Constants]'])
            for m in re.finditer(r'^\s*global(?:\s+persist)?\s+\$auto_play_enabled_group(\d+)', const_text, re.MULTILINE):
                group_ids.add(int(m.group(1)))
        if group_ids:
            return sorted(group_ids), True
        return [], False

    def _scan_slot_icons(self, sections):
        slot_icon_map = {}
        icon_pattern = re.compile(r'^\s*;\s*SLOT_ICON_(\d+)\s*=\s*(.+)$')
        if '[Constants]' in sections:
            for line in sections['[Constants]']:
                m = icon_pattern.match(line)
                if m:
                    slot = int(m.group(1))
                    path = m.group(2).strip()
                    if os.path.isfile(path):
                        slot_icon_map[slot] = path
        return slot_icon_map

    def _build_slider_to_group(self, sorted_freq_params, multi_group_mode, group_ids):
        slider_to_group = {}
        if multi_group_mode and group_ids:
            has_exact = False
            for idx, var_name in enumerate(sorted_freq_params):
                m = re.match(r'\$Freq_group(\d+)', var_name)
                if m:
                    slider_to_group[idx] = int(m.group(1))
                    has_exact = True
            if not has_exact:
                for idx in range(len(sorted_freq_params)):
                    gid = group_ids[idx % len(group_ids)]
                    slider_to_group[idx] = gid
        return slider_to_group

    def _apply_slider_panel(self, mod_export_path, target_ini_file, sections, exclude_vars=None):
        """【已合并】生成滑块面板-自定义配置并合并到 sections（不写盘）。

        exclude_vars: 不给这些变量生成滑块。用于「未分配形态键」——它们的引用会被
        屏蔽注释，如果继续建滑块，面板里既会留下一个点了没反应的死滑块，还会生成
        ``$paramN = $被屏蔽变量`` 这类对已注释声明变量的读取行。
        """
        ns = self._ensure_namespace()
        if '[Present]' in sections and any("SLIDER PANEL CUSTOM LOGIC (appended)" in l for l in sections['[Present]']):
            print("滑块面板配置已存在于文件中。请手动删除后再生成。")
            return False

        freq_params_temp, skipped = self._collect_slider_freq_params(sections, exclude_vars)
        if skipped:
            print(
                f"[形态键扩展] 滑块面板跳过 {len(skipped)} 个未分配形态键的滑块: {skipped}"
            )

        group_pattern = re.compile(r'^\$Freq_Group(\d+)$')
        group_var_matches = {}
        for v in freq_params_temp:
            m = group_pattern.match(v)
            if m:
                group_var_matches[int(m.group(1))] = v

        if group_var_matches:
            multi_group_mode = True
            group_ids = sorted(group_var_matches.keys())
            sorted_freq_params = [group_var_matches[g] for g in group_ids]
            slider_to_group = {i: gid for i, gid in enumerate(group_ids)}
            num_sliders = len(sorted_freq_params)
        else:
            sorted_freq_params = sorted(list(freq_params_temp))
            num_sliders = len(sorted_freq_params)
            group_ids, multi_group_mode = self._scan_slider_group_info(sections)
            slider_to_group = self._build_slider_to_group(sorted_freq_params, multi_group_mode, group_ids)

        if num_sliders == 0:
            return False

        slot_icon_map = self._scan_slot_icons(sections)

        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            source_asset_dir = os.path.join(addon_dir, "Toolset")
            dest_res_dir = os.path.join(mod_export_path, "res")
            os.makedirs(dest_res_dir, exist_ok=True)

            shader_src = os.path.join(source_asset_dir, "draw_2d.hlsl")
            shader_dst = os.path.join(dest_res_dir, "draw_2d.hlsl")
            if os.path.exists(shader_src) and not os.path.exists(shader_dst):
                shutil.copy2(shader_src, shader_dst)

            image_mappings = [("0.png", self.background_image), ("1.png", self.slider_handle_image),
                              ("2.png", self.left_bar_image), ("3.png", self.right_bar_image)]
            for std_name, custom_path in image_mappings:
                dest_path = os.path.join(dest_res_dir, std_name)
                if custom_path and os.path.isfile(custom_path):
                    shutil.copy2(custom_path, dest_path)
                else:
                    self._copy_default_image(std_name, dest_res_dir, source_asset_dir)

            btn_w_list = []
            max_btn_w = 0.0
            btn_h_fixed = self.button_height
            SCREEN_RATIO_CORRECTION = 0.5625

            for i in range(1, num_sliders + 1):
                idx = i - 1
                group_id = slider_to_group.get(idx) if multi_group_mode else None
                custom_icon = None
                if group_id is not None and group_id in slot_icon_map:
                    custom_icon = slot_icon_map[group_id]
                if not custom_icon and self.button_image and os.path.isfile(self.button_image):
                    custom_icon = self.button_image
                if custom_icon and os.path.isfile(custom_icon):
                    src_btn = custom_icon
                else:
                    src_btn = os.path.join(source_asset_dir, "4.png")

                if os.path.exists(src_btn):
                    dest_name = f"4_{i}.png"
                    dest_path = os.path.join(dest_res_dir, dest_name)
                    if os.path.normpath(src_btn) != os.path.normpath(dest_path):
                        shutil.copy2(src_btn, dest_path)

                    btn_w_i = btn_h_fixed * 3.0 * SCREEN_RATIO_CORRECTION  # 缺省按 3:1
                    if PIL_AVAILABLE:
                        try:
                            with Image.open(dest_path) as img:
                                w, h = img.size
                                if h > 0:
                                    btn_w_i = btn_h_fixed * (w / h) * SCREEN_RATIO_CORRECTION
                        except Exception:
                            pass
                    btn_w_list.append(btn_w_i)
                    if btn_w_i > max_btn_w:
                        max_btn_w = btn_w_i
                else:
                    btn_w_list.append(btn_h_fixed * 3.0 * SCREEN_RATIO_CORRECTION)
        except Exception as e:
            print(f"准备和复制资源文件时出错: {e}")
            return False

        slider_h = self.slider_height
        # 滑块宽度 = 滑块高度 × 滑块柄图片宽高比（与物体切换面板按钮一致，保持图片比例）
        slider_w = self._measure_image_aspect(dest_res_dir, "1.png") * slider_h * SCREEN_RATIO_CORRECTION

        child_height = slider_h
        top_bottom_padding = 0.03
        spacing = 0.02
        total_slider_height = num_sliders * child_height
        total_spacing_height = max(0, (num_sliders - 1) * spacing)
        parent_height = total_slider_height + total_spacing_height + (top_bottom_padding * 2)
        # 面板最小高度：自动计算高度过小时使用设置值
        parent_height = max(parent_height, self.panel_min_height)

        # 背景尺寸：宽 = 自动计算高度 × 背景图片宽高比 × 屏幕校正（保持图片比例）
        bg_aspect = self._measure_image_aspect(dest_res_dir, "0.png")
        panel_bg_width_auto = parent_height * bg_aspect * SCREEN_RATIO_CORRECTION

        # 生成/处理面板背景（圆角+边框）：无论是否自定义背景图都生效
        bg_custom = (self.background_image or "").strip()
        self._generate_background_image(os.path.join(dest_res_dir, "0.png"),
                                        panel_bg_width_auto, parent_height,
                                        use_existing=bool(bg_custom))

        help_key = self.help_key.strip() or "home"
        reset_key = self.reset_key.strip() or "ctrl home"
        zoom_in_key = self.zoom_in_key.strip() or "up"
        zoom_out_key = self.zoom_out_key.strip() or "down"
        drag_key = self.drag_key.strip() or "VK_LBUTTON"
        auto_play_key = self.auto_play_toggle_key.strip() or "space"
        detect_hash = self.detect_hash.strip() or self.check_hash.strip()
        detect_index_count_val = self.detect_index_count.strip()
        if not detect_index_count_val and self.match_index_count > 0:
            detect_index_count_val = str(self.match_index_count)

        constants_additions = []
        present_additions = []
        other_sections = OrderedDict()

        constants_additions.extend([
            "; --- UI 几何与位置配置 (由滑块面板-自定义生成) ---",
            f"global $base_width0 = {panel_bg_width_auto:.4f}",
            f"global $base_height0 = {parent_height:.4f}",
            "global $set_x0 = 0.5", "global $set_y0 = 0.5",
        ])
        for i in range(1, num_sliders + 1):
            current_y_offset = top_bottom_padding + (i - 1) * (child_height + spacing) + (child_height / 2)
            relative_y = current_y_offset / parent_height
            btn_w_i = btn_w_list[i-1]
            constants_additions.extend([
                f"global $base_width{i} = {slider_w:.4f}",
                f"global $base_height{i} = {slider_h:.4f}",
                f"global $set_rel_x{i} = 0.5",
                f"global $fixed_rel_y{i} = {relative_y:.4f}",
                f"global $btn_width{i} = {btn_w_i:.4f}",
                f"global $btn_height{i} = {btn_h_fixed:.4f}",
                f"global $btn_x{i}", f"global $btn_y{i}", f"global $btn_pressed{i} = 0",
            ])

        constants_additions.extend([
            "global $ui_active", "global $help",
            "global $max_zoom = 5.0", "global $min_zoom = 0.1",
            "global $dragged_slider = 0", "global $mouse_clicked = 0",
            "global $click_outside = 0", "global $is_dragging = 0",
            "global $drag_x = 0", "global $drag_y = 0",
            "global persist $img0_x = 0", "global persist $img0_y = 0",
            f"global persist $zoom0 = {self.panel_default_scale:.2f}",
            "global $norm_width0", "global $norm_height0",
            "global $btn_click_processed = 0",
        ])
        if not multi_group_mode:
            constants_additions.extend([
                "global persist $auto_play_enabled = 0", "global persist $frameEnd = 30",
                "global persist $step_accum = 0", "global persist $step_frames = 5",
                "global persist $step_frames_min = 1", "global persist $step_frames_max = 30",
            ])
        for i in range(1, num_sliders + 1):
            constants_additions.extend([
                f"global persist $rel_x{i} = 0", f"global persist $zoom{i} = 1.0",
                f"global $norm_width{i}", f"global $norm_height{i}",
                f"global $img{i}_x", f"global $img{i}_y", f"global $rel_y{i}",
                f"global $param{i}",
                f"global $left_bar{i}_x", f"global $left_bar{i}_y",
                f"global $left_bar{i}_width", f"global $left_bar{i}_height",
                f"global $right_bar{i}_x", f"global $right_bar{i}_y",
                f"global $right_bar{i}_width", f"global $right_bar{i}_height",
                f"global $min_rel_x{i}", f"global $max_rel_x{i}",
                f"global $range_x{i}", f"global $slider{i}_center_x",
            ])

        detect_lines = []
        has_any_check = False
        if detect_hash:
            detect_lines.append(f"hash = {detect_hash}")
            has_any_check = True
        if detect_index_count_val:
            detect_lines.append(f"match_index_count = {detect_index_count_val}")
            has_any_check = True
        detect_lines.append("$ui_active = 1")
        if has_any_check:
            other_sections["[TextureOverrideCheckHash]"] = detect_lines

        other_sections["[ResourceImageToRender0]"] = ["filename = ./res/0.png"]
        other_sections["[ResourceSliderHandle]"] = ["filename = ./res/1.png"]
        other_sections["[ResourceLeftBar]"] = ["filename = ./res/2.png"]
        other_sections["[ResourceRightBar]"] = ["filename = ./res/3.png"]
        for i in range(1, num_sliders + 1):
            other_sections[f"[ResourcePlayPauseButton{i}]"] = [f"filename = ./res/4_{i}.png"]

        reset_lines = ["$img0_x = 0", "$img0_y = 0", f"$zoom0 = {self.panel_default_scale:.2f}"]
        for i in range(1, num_sliders + 1):
            reset_lines.extend([f"$rel_x{i} = 0", f"$zoom{i} = 1.0"])
        zoom_in_lines = ["$zoom0 = $zoom0 + 0.05"] + [f"$zoom{i} = $zoom{i} + 0.05" for i in range(1, num_sliders + 1)]
        zoom_out_lines = ["$zoom0 = $zoom0 - 0.05"] + [f"$zoom{i} = $zoom{i} - 0.05" for i in range(1, num_sliders + 1)]

        other_sections["[KeyHelp]"] = [
            f"condition = $ui_active == 1", f"key = {help_key}", "type = cycle", "$help = 0,1"
        ]
        other_sections["[KeyResetPosition]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {reset_key}", "type = cycle",
        ] + reset_lines
        other_sections["[KeyZoomIn]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {zoom_in_key}",
            "type = press", "run = CommandListZoomIn"
        ]
        other_sections["[KeyZoomOut]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {zoom_out_key}",
            "type = press", "run = CommandListZoomOut"
        ]
        other_sections["[KeyMouseDrag]"] = [
            f"condition = $help == 1 && $ui_active == 1", f"key = {drag_key}",
            "type = hold", "$mouse_clicked = 1"
        ]

        auto_play_key_lines = []
        if not self.auto_play_key_global:
            auto_play_key_lines.append("condition = $help == 1 && $ui_active == 1")
        auto_play_key_lines.extend([f"key = {auto_play_key}", "type = press", f"run = CommandListToggleAutoPlay_{ns}"])
        other_sections[f"[KeyToggleAutoPlay_{ns}]"] = auto_play_key_lines

        other_sections["[CommandListZoomIn]"] = zoom_in_lines
        other_sections["[CommandListZoomOut]"] = zoom_out_lines

        if multi_group_mode:
            toggle_lines = [f"$auto_play_enabled_group{gid} = 1 - $auto_play_enabled_group{gid}" for gid in group_ids]
            other_sections[f"[CommandListToggleAutoPlay_{ns}]"] = toggle_lines
            for gid in group_ids:
                other_sections[f"[CommandListToggleAutoPlayGroup{gid}_{ns}]"] = [f"$auto_play_enabled_group{gid} = 1 - $auto_play_enabled_group{gid}"]
        else:
            other_sections[f"[CommandListToggleAutoPlay_{ns}]"] = ["$auto_play_enabled = 1 - $auto_play_enabled"]

        present_additions.append("post $ui_active = 0")
        present_additions.append("if $help == 1 && $ui_active == 1")
        present_additions.append("    ; --- 1. 尺寸计算 ---")
        present_additions.append("    $norm_width0 = $base_width0 * $zoom0")
        present_additions.append("    $norm_height0 = $base_height0 * $zoom0")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $norm_width{i} = $base_width{i} * $zoom{i}")
            present_additions.append(f"    $norm_height{i} = $base_height{i} * $zoom{i}")

        present_additions.append("\n    ; --- 2. 计算子级的拖拽边界 ---")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $min_rel_x{i} = $btn_width{i} + 0.02")
            present_additions.append(f"    $max_rel_x{i} = ($norm_width0 * 0.95) - $norm_width{i}")
            present_additions.append(f"    $range_x{i} = $max_rel_x{i} - $min_rel_x{i}")

        present_additions.append("\n    ; --- 3. 位置初始化 ---")
        present_additions.append("    if $img0_x == 0 && $img0_y == 0")
        present_additions.append("        $img0_x = $set_x0 * (1 - $norm_width0)")
        present_additions.append("        $img0_y = $set_y0 * (1 - $norm_height0)")
        present_additions.append("    endif")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    if $rel_x{i} == 0")
            if multi_group_mode:
                gid = slider_to_group.get(i - 1)
                if gid is not None and gid in group_ids:
                    present_additions.append(f"        if ($auto_play_enabled_group{gid} == 1)")
                    present_additions.append(f"            $param{i} = ($speed_percent_group{gid} - $speed_percent_min_group{gid}) / ($speed_percent_max_group{gid} - $speed_percent_min_group{gid})")
                    present_additions.append(f"        else")
                    present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                    present_additions.append(f"        endif")
                else:
                    present_additions.append(f"        $param{i} = {sorted_freq_params[i-1]}")
            else:
                present_additions.append(f"        if ($auto_play_enabled == 1)")
                present_additions.append(f"            $param{i} = ($step_frames - $step_frames_min) / ($step_frames_max - $step_frames_min)")
                present_additions.append(f"        else")
                present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                present_additions.append(f"        endif")
            present_additions.append(f"        $rel_x{i} = $min_rel_x{i} + $param{i} * $range_x{i}")
            present_additions.append("    endif")

        present_additions.append("\n    ; --- 4. 计算滑块和按钮位置 ---")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $rel_y{i} = ($fixed_rel_y{i} * $norm_height0) - ($norm_height{i} / 2)")
            present_additions.append(f"    $img{i}_x = $img0_x + $rel_x{i}")
            present_additions.append(f"    $img{i}_y = $img0_y + $rel_y{i}")
            present_additions.append(f"    $btn_x{i} = $img0_x + $min_rel_x{i} - $btn_width{i} - 0.01")
            present_additions.append(f"    $btn_y{i} = $img{i}_y + ($norm_height{i} - $btn_height{i}) * 0.5")

        present_additions.append("\n    ; --- 5. 按钮按下/弹起检测（仅自动播放分组可点击）---")
        present_additions.append("    $btn_click_processed = 0")
        present_additions.append("    if $mouse_clicked && $is_dragging == 0")
        for i in range(1, num_sliders + 1):
            gid = slider_to_group.get(i - 1) if multi_group_mode else None
            # 仅启用自动播放的分组按钮可点击（切换播放/暂停）
            setting = self._find_group_setting(gid) if (multi_group_mode and gid is not None) else None
            if setting is not None:
                auto_playable = bool(setting.enable_auto_playback)
            else:
                auto_playable = any(s.enable_auto_playback for s in self.play_group_settings)
            if not auto_playable:
                continue
            present_additions.append(f"        if cursor_x > $btn_x{i} && cursor_x < $btn_x{i} + $btn_width{i} && cursor_y > $btn_y{i} && cursor_y < $btn_y{i} + $btn_height{i}")
            present_additions.append(f"            $btn_pressed{i} = 1")
            present_additions.append(f"            $btn_click_processed = 1")
            present_additions.append(f"        endif")
        present_additions.append("    else")
        present_additions.append("        if $is_dragging == 0")
        for i in range(1, num_sliders + 1):
            gid = slider_to_group.get(i - 1) if multi_group_mode else None
            setting = self._find_group_setting(gid) if (multi_group_mode and gid is not None) else None
            if setting is not None:
                auto_playable = bool(setting.enable_auto_playback)
            else:
                auto_playable = any(s.enable_auto_playback for s in self.play_group_settings)
            if not auto_playable:
                continue
            present_additions.append(f"            if $btn_pressed{i} == 1")
            present_additions.append(f"                if cursor_x > $btn_x{i} && cursor_x < $btn_x{i} + $btn_width{i} && cursor_y > $btn_y{i} && cursor_y < $btn_y{i} + $btn_height{i}")
            if multi_group_mode and gid is not None and gid in group_ids:
                present_additions.append(f"                    run = CommandListToggleAutoPlayGroup{gid}_{ns}")
            else:
                present_additions.append(f"                    run = CommandListToggleAutoPlay_{ns}")
            present_additions.append(f"                endif")
            present_additions.append(f"                $btn_pressed{i} = 0")
            present_additions.append(f"            endif")
        present_additions.append("        endif")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 6. 拖拽逻辑与位置更新 ---")
        present_additions.append("    if $mouse_clicked")
        present_additions.append("        if $is_dragging == 0")
        for i in range(num_sliders, 0, -1):
            prefix = "if" if i == num_sliders else "            else if"
            present_additions.append(f"            {prefix} cursor_x > $img{i}_x && cursor_x < $img{i}_x + $norm_width{i} && cursor_y > $img{i}_y && cursor_y < $img{i}_y + $norm_height{i}")
            present_additions.append(f"                $is_dragging = {i + 1}")
            present_additions.append(f"                $drag_x = cursor_x - $img{i}_x")
        present_additions.append("            else if cursor_x > $img0_x && cursor_x < $img0_x + $norm_width0 && cursor_y > $img0_y && cursor_y < $img0_y + $norm_height0")
        present_additions.append("                $is_dragging = 1")
        present_additions.append("                $drag_x = cursor_x - $img0_x")
        present_additions.append("                $drag_y = cursor_y - $img0_y")
        present_additions.append("            else")
        present_additions.append("                if $btn_click_processed == 0")
        present_additions.append("                    $click_outside = 1")
        present_additions.append("                endif")
        present_additions.append("            endif")
        present_additions.append("        endif")
        present_additions.append("    else")
        present_additions.append("        $is_dragging = 0")
        present_additions.append("    endif")
        present_additions.append("    if $click_outside == 1 && $mouse_clicked == 0")
        present_additions.append("        $help = 0")
        present_additions.append("        $click_outside = 0")
        present_additions.append("    endif")
        present_additions.append("    if $is_dragging == 1")
        present_additions.append("        $img0_x = cursor_x - $drag_x")
        present_additions.append("        $img0_y = cursor_y - $drag_y")
        for i in range(2, num_sliders + 2):
            present_additions.append(f"    else if $is_dragging == {i}")
            present_additions.append(f"        $rel_x{i-1} = (cursor_x - $drag_x) - $img0_x")
            present_additions.append(f"        if $rel_x{i-1} < $min_rel_x{i-1}")
            present_additions.append(f"            $rel_x{i-1} = $min_rel_x{i-1}")
            present_additions.append(f"        endif")
            present_additions.append(f"        if $rel_x{i-1} > $max_rel_x{i-1}")
            present_additions.append(f"            $rel_x{i-1} = $max_rel_x{i-1}")
            present_additions.append(f"        endif")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 7. 计算进度条的几何信息 ---")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"    $slider{i}_center_x = $img{i}_x + ($norm_width{i} * 0.5)")
            present_additions.append(f"    $left_bar{i}_height = $norm_height{i} * 0.5")
            present_additions.append(f"    $left_bar{i}_y = $img{i}_y + ($norm_height{i} * 0.25)")
            present_additions.append(f"    $left_bar{i}_x = $img0_x + $min_rel_x{i}")
            present_additions.append(f"    $left_bar{i}_width = $slider{i}_center_x - $left_bar{i}_x")
            present_additions.append(f"    $right_bar{i}_height = $left_bar{i}_height")
            present_additions.append(f"    $right_bar{i}_y = $left_bar{i}_y")
            present_additions.append(f"    $right_bar{i}_x = $slider{i}_center_x")
            present_additions.append(f"    $right_bar{i}_width = ($img0_x + $norm_width0 * 0.95) - $right_bar{i}_x")

        present_additions.append("\n    ; --- 8. 计算映射参数并链接到形态键强度或播放速度百分比 ---")
        present_additions.append("    $dragged_slider = 0")
        present_additions.append("    if $is_dragging >= 2")
        present_additions.append("        $dragged_slider = $is_dragging - 1")
        present_additions.append("    endif")

        if multi_group_mode:
            for i in range(1, num_sliders + 1):
                gid = slider_to_group.get(i - 1)
                present_additions.append(f"    if ($dragged_slider == {i})")
                present_additions.append(f"        $param{i} = ($rel_x{i} - $min_rel_x{i}) / $range_x{i}")
                if gid is not None and gid in group_ids:
                    present_additions.append(f"        if ($auto_play_enabled_group{gid} == 1)")
                    present_additions.append(f"            $speed_percent_group{gid} = $speed_percent_min_group{gid} + $param{i} * ($speed_percent_max_group{gid} - $speed_percent_min_group{gid})")
                    present_additions.append(f"        else")
                    present_additions.append(f"            {sorted_freq_params[i-1]} = $param{i}")
                    present_additions.append(f"        endif")
                else:
                    present_additions.append(f"        {sorted_freq_params[i-1]} = $param{i}")
                present_additions.append(f"    else")
                if gid is not None and gid in group_ids:
                    present_additions.append(f"        if ($auto_play_enabled_group{gid} == 1)")
                    present_additions.append(f"            $param{i} = ($speed_percent_group{gid} - $speed_percent_min_group{gid}) / ($speed_percent_max_group{gid} - $speed_percent_min_group{gid})")
                    present_additions.append(f"        else")
                    present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                    present_additions.append(f"        endif")
                else:
                    present_additions.append(f"        $param{i} = {sorted_freq_params[i-1]}")
                present_additions.append(f"        $rel_x{i} = $min_rel_x{i} + $param{i} * $range_x{i}")
                present_additions.append(f"    endif")
        else:
            for i in range(1, num_sliders + 1):
                present_additions.append(f"    if ($dragged_slider == {i})")
                present_additions.append(f"        $param{i} = ($rel_x{i} - $min_rel_x{i}) / $range_x{i}")
                present_additions.append(f"        if ($auto_play_enabled == 1)")
                present_additions.append(f"            $step_frames = $step_frames_min + $param{i} * ($step_frames_max - $step_frames_min)")
                present_additions.append(f"        else")
                present_additions.append(f"            {sorted_freq_params[i-1]} = $param{i}")
                present_additions.append(f"        endif")
                present_additions.append(f"    else")
                present_additions.append(f"        if ($auto_play_enabled == 1)")
                present_additions.append(f"            $param{i} = ($step_frames - $step_frames_min) / ($step_frames_max - $step_frames_min)")
                present_additions.append(f"        else")
                present_additions.append(f"            $param{i} = {sorted_freq_params[i-1]}")
                present_additions.append(f"        endif")
                present_additions.append(f"        $rel_x{i} = $min_rel_x{i} + $param{i} * $range_x{i}")
                present_additions.append(f"    endif")

        present_additions.append("\n    ; --- 9. 执行渲染 (按层级) ---")
        present_additions.append("    ; 渲染父级 (最底层)")
        present_additions.append("    ps-t100 = ResourceImageToRender0")
        present_additions.append("    x87 = $norm_width0")
        present_additions.append("    y87 = $norm_height0")
        present_additions.append("    z87 = $img0_x")
        present_additions.append("    w87 = $img0_y")
        present_additions.append("    run = CustomShaderDraw")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"\n    ; 渲染进度条{i}")
            present_additions.append(f"    ps-t100 = ResourceLeftBar")
            present_additions.append(f"    x87 = $left_bar{i}_width")
            present_additions.append(f"    y87 = $left_bar{i}_height")
            present_additions.append(f"    z87 = $left_bar{i}_x")
            present_additions.append(f"    w87 = $left_bar{i}_y")
            present_additions.append(f"    run = CustomShaderDraw")
            present_additions.append(f"    ps-t100 = ResourceRightBar")
            present_additions.append(f"    x87 = $right_bar{i}_width")
            present_additions.append(f"    y87 = $right_bar{i}_height")
            present_additions.append(f"    z87 = $right_bar{i}_x")
            present_additions.append(f"    w87 = $right_bar{i}_y")
            present_additions.append(f"    run = CustomShaderDraw")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"\n    ; 渲染播放/暂停按钮{i}")
            present_additions.append(f"    ps-t100 = ResourcePlayPauseButton{i}")
            present_additions.append(f"    x87 = $btn_width{i}")
            present_additions.append(f"    y87 = $btn_height{i}")
            present_additions.append(f"    z87 = $btn_x{i}")
            present_additions.append(f"    if $btn_pressed{i} == 1")
            present_additions.append(f"        w87 = $btn_y{i} + 0.002")
            present_additions.append(f"    else")
            present_additions.append(f"        w87 = $btn_y{i}")
            present_additions.append(f"    endif")
            present_additions.append(f"    run = CustomShaderDraw")
        for i in range(1, num_sliders + 1):
            present_additions.append(f"\n    ; 渲染滑块{i} (最顶层)")
            present_additions.append(f"    ps-t100 = ResourceSliderHandle")
            present_additions.append(f"    x87 = $norm_width{i}")
            present_additions.append(f"    y87 = $norm_height{i}")
            present_additions.append(f"    z87 = $img{i}_x")
            present_additions.append(f"    w87 = $img{i}_y")
            present_additions.append(f"    run = CustomShaderDraw")
        present_additions.append("endif")

        shader_def = [
            "hs = null", "ds = null", "gs = null", "cs = null",
            "vs = ./res/draw_2d.hlsl", "ps = ./res/draw_2d.hlsl",
            "blend = ADD SRC_ALPHA INV_SRC_ALPHA", "cull = none",
            "topology = triangle_strip", "o0 = set_viewport bb", "Draw = 4,0", "clear = ps-t100"
        ]

        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        constants_additions.insert(0, self.SLIDERCONST_BEGIN)
        constants_additions.append(self.SLIDERCONST_END)
        for line in constants_additions:
            if line not in sections['[Constants]']:
                sections['[Constants]'].append(line)

        for sec_name, lines in other_sections.items():
            if sec_name not in sections:
                sections[sec_name] = []
            for line in lines:
                if line not in sections[sec_name]:
                    sections[sec_name].append(line)

        if '[CustomShaderDraw]' not in sections:
            sections['[CustomShaderDraw]'] = []
        for line in shader_def:
            if line not in sections['[CustomShaderDraw]']:
                sections['[CustomShaderDraw]'].append(line)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        has_existing_logic = any(self.SLIDER_PRESENT_BEGIN in l for l in sections['[Present]'])
        if not has_existing_logic:
            sections['[Present]'].append("")
            sections['[Present]'].append(self.SLIDER_PRESENT_BEGIN)
            sections['[Present]'].extend(present_additions)
            sections['[Present]'].append(self.SLIDER_PRESENT_END)

        print(f"滑块面板-自定义配置已合并（共 {num_sliders} 个滑块）")
        return True

    def draw_buttons(self, context, layout):
        # 顶部：原地刷新（应用设置到Mod，无需重新导出）
        box_top = layout.box()
        box_top.operator("ssmt.shapekey_ext_refresh", text="应用设置到Mod（原地更新）", icon='FILE_TICK').node_name = self.name
        box_top.prop(self, "ini_file_path", text="INI文件（可选，留空用上次导出）")
        box_top.label(text="修改设置后点上方按钮即可原地更新，无需重新导出", icon='INFO')
        layout.separator()
        # 左右两列布局：左=形态键扩展配置，右=滑块面板-自定义
        split = layout.split(factor=0.5)
        col_left = split.column()
        col_right = split.column()
        self._draw_shapekey_ui(col_left)
        self._draw_slider_ui(col_right)

    def _draw_shapekey_ui(self, layout):
        # --- 顶部：主容器 ---
        box = layout.box()
        box.label(text="形态键与分组管理", icon='SHAPEKEY_DATA')
        row = box.row(align=True)
        row.operator("ssmt.scan_shapekey_ext", text="刷新形态键列表", icon='FILE_REFRESH').node_name = self.name

        row = box.row(align=True)
        row.operator("ssmt.shapekey_group_add", text="创建新分组", icon='ADD').node_name = self.name

        for setting in self.play_group_settings:
            group_container = box.box()

            # 1. 分组头行
            row = group_container.row(align=True)
            # 折叠切换
            row.prop(setting, "expanded", text="", icon='TRIA_DOWN' if setting.expanded else 'TRIA_RIGHT', emboss=False)

            # 高亮当前分组标识：勾选框样式（勾选=当前编辑分组，点击可切换）
            is_active = (setting.group_index == self.active_group_index)
            act_op = row.operator("ssmt.select_play_group", text="", icon='CHECKBOX_HLT' if is_active else 'CHECKBOX_DEHLT', emboss=False)
            act_op.node_name = self.name
            act_op.target_group_index = setting.group_index

            sw_op = row.operator("ssmt.select_play_group", text=f"分组 {setting.group_index} - {setting.remark}", icon='GROUP')
            sw_op.node_name = self.name
            sw_op.target_group_index = setting.group_index

            set_op = row.operator("ssmt.open_group_settings", text="", icon='PREFERENCES')
            set_op.node_name = self.name
            set_op.target_group_index = setting.group_index

            dup_op = row.operator("ssmt.shapekey_group_duplicate", text="", icon='DUPLICATE')
            dup_op.node_name = self.name
            dup_op.target_group_index = setting.group_index

            row.separator()
            op_up = row.operator("ssmt.shapekey_group_move", text="", icon='TRIA_UP')
            op_up.node_name = self.name
            op_up.direction = 'UP'
            op_up.target_group_index = setting.group_index

            op_down = row.operator("ssmt.shapekey_group_move", text="", icon='TRIA_DOWN')
            op_down.node_name = self.name
            op_down.direction = 'DOWN'
            op_down.target_group_index = setting.group_index

            op_del = row.operator("ssmt.shapekey_group_remove", text="", icon='X')
            op_del.node_name = self.name
            op_del.target_group_index = setting.group_index

            # 2. 分组形态键列表
            if setting.expanded:
                child_box = group_container.box()
                has_items = False

                # 【修复】使用自然排序 (Natural Sort) 替换原有排序
                sorted_entries = sorted(self.play_group_entries, key=lambda e: self._natural_sort_key(e.shape_key_name))

                for entry in sorted_entries:
                    if entry.group_index == setting.group_index:
                        has_items = True
                        item_row = child_box.row(align=True)
                        item_row.label(text=entry.shape_key_name, icon='SHAPEKEY_DATA')

                        op_add = item_row.operator("ssmt.shapekey_assign_current_group", text="移入当前分组", icon='ADD')
                        op_add.node_name = self.name
                        op_add.shape_key_name = entry.shape_key_name

                        op_rm = item_row.operator("ssmt.shapekey_unassign_group", text="移出分组", icon='REMOVE')
                        op_rm.node_name = self.name
                        op_rm.shape_key_name = entry.shape_key_name
                if not has_items:
                    child_box.label(text="     (暂无形态键)", icon='INFO')
        # --- 底部：未分组区域 ---
        box = layout.box()
        row = box.row(align=True)
        row.prop(self, "show_unassigned", text="", icon='TRIA_DOWN' if self.show_unassigned else 'TRIA_RIGHT', emboss=False)
        row.label(text="未分组形态键", icon='PARTICLES')

        if self.show_unassigned:
            sub_box = box.box()
            has_unassigned = False

            # 【修复】未分组区域同样使用自然排序
            unassigned_entries = sorted([e for e in self.play_group_entries if e.group_index == 0], key=lambda e: self._natural_sort_key(e.shape_key_name))

            for entry in unassigned_entries:
                has_unassigned = True
                item_row = sub_box.row(align=True)
                item_row.label(text=entry.shape_key_name, icon='SHAPEKEY_DATA')

                op_add = item_row.operator("ssmt.shapekey_assign_current_group", text="移入当前分组", icon='ADD')
                op_add.node_name = self.name
                op_add.shape_key_name = entry.shape_key_name
            if not has_unassigned:
                sub_box.label(text="     (暂无未分配的形态键)", icon='INFO')
        # --- 底部：全局快捷键设置 ---
        box = layout.box()
        box.label(text="快捷键设置", icon='KEYINGSET')
        row = box.row(align=True)
        op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
        op.node_name = self.name
        op.node_tree_name = self.id_data.name
        op.target_prop = "auto_play_toggle_key"
        row.prop(self, "auto_play_toggle_key", text="自动播放")
        box.prop(self, "auto_play_key_global", text="全局生效")

    def _draw_slider_ui(self, layout):
        layout.prop(self, "use_slider_panel", text="启用滑块面板")
        if not self.use_slider_panel:
            return

        box = layout.box()
        box.label(text="滑块面板-自定义", icon='GRIP')
        box.prop(self, "create_cumulative_backup")

        box = layout.box()
        box.label(text="快捷键设置", icon='KEYINGSET')
        col = box.column(align=True)
        row = col.row(align=True)
        op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
        op.node_name = self.name
        op.node_tree_name = self.id_data.name
        op.target_prop = "help_key"
        row.prop(self, "help_key", text="显隐面板")
        row = col.row(align=True)
        op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
        op.node_name = self.name
        op.node_tree_name = self.id_data.name
        op.target_prop = "reset_key"
        row.prop(self, "reset_key", text="重置位置")
        row = col.row(align=True)
        op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
        op.node_name = self.name
        op.node_tree_name = self.id_data.name
        op.target_prop = "zoom_in_key"
        row.prop(self, "zoom_in_key", text="放大")
        row = col.row(align=True)
        op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
        op.node_name = self.name
        op.node_tree_name = self.id_data.name
        op.target_prop = "zoom_out_key"
        row.prop(self, "zoom_out_key", text="缩小")
        row = col.row(align=True)
        op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
        op.node_name = self.name
        op.node_tree_name = self.id_data.name
        op.target_prop = "drag_key"
        row.prop(self, "drag_key", text="拖拽键")

        box = layout.box()
        box.label(text="UI尺寸设置", icon='PROPERTIES')
        col = box.column(align=True)
        col.prop(self, "panel_default_scale", text="面板默认缩放")
        col.separator()
        col.prop(self, "slider_height", text="滑块高度")
        col.prop(self, "button_height", text="按钮高度")
        col.prop(self, "panel_min_height", text="面板最小高度")
        col.label(text="背景/滑块/按钮宽度按高度×图片比例自动计算", icon='INFO')

        box = layout.box()
        box.label(text="角色检测设置", icon='VIEWZOOM')
        row = box.row(align=True)
        row.prop_search(self, "target_object", bpy.data, "objects", text="物体", icon='OBJECT_DATA')
        row.operator("ssmt.shapekey_ext_parse_slider_object", text="", icon='FILE_REFRESH').node_name = self.name
        box.prop(self, "detect_hash", text="哈希值")
        box.prop(self, "detect_index_count", text="IndexCount")

        box = layout.box()
        box.label(text="滑块图片资源（自定义）", icon='TEXTURE')
        box.prop(self, "background_image", text="背景")
        box.prop(self, "slider_handle_image", text="滑块")
        box.prop(self, "left_bar_image", text="左进度条")
        box.prop(self, "right_bar_image", text="右进度条")
        box.prop(self, "button_image", text="按钮")
        box.label(text="留空则使用插件内置默认图片", icon='INFO')

        box = layout.box()
        box.label(text="文字按钮样式", icon='COLOR')
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(self, "button_bg_color", text="背景色")
        row.prop(self, "button_border_color", text="边框色")
        col.prop(self, "button_border_width", text="边框宽度")
        col.prop(self, "button_opacity", text="透明度")
        col.label(text="背景/边框紧贴文字，内边距极小不发糊", icon='INFO')

        box = layout.box()
        box.label(text="面板背景样式（圆角/边框）", icon='MATERIAL')
        col = box.column(align=True)
        col.prop(self, "background_corner_radius", text="背景圆角")
        col.prop(self, "background_border_color", text="边框色")
        col.prop(self, "background_border_width", text="边框宽度")
        col.prop(self, "background_opacity", text="透明度")
        col.label(text="未自定义背景图片时生效", icon='INFO')

    def execute_postprocess(self, mod_export_path, _in_place=False, _ini_path=None):
        """生成 / 原地刷新 形态键扩展配置（含合并的滑块面板）。

        - 导出（_in_place=False）：在 mod ini 中生成配置（带专有标识注释）。
        - 刷新（_in_place=True）：按标识移除本节点旧配置后原地重新生成，无需重新导出整个 mod。
        """
        if _ini_path:
            target_ini_file = _ini_path
        else:
            ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
            if not ini_files:
                return False
            target_ini_file = ini_files[0]

        print(f"[形态键扩展配置] {'原地刷新' if _in_place else '开始执行'}: {target_ini_file}")

        ns = self._ensure_namespace()
        self.last_mod_ini_path = target_ini_file

        # 防重复：仅导出时检查（刷新时按标识替换旧配置）
        if not _in_place:
            try:
                with open(target_ini_file, 'r', encoding='utf-8') as f:
                    if self.DUP_GUARD in f.read():
                        print("形态键扩展配置已存在于文件中。请手动删除后再生成，或用节点上的「应用设置到Mod」原地更新。")
                        return False
            except Exception:
                pass

        if self.create_cumulative_backup:
            self._create_cumulative_backup(target_ini_file, mod_export_path)

        result = self._read_ini_to_ordered_dict(target_ini_file)
        if result is None or not result[0]:
            return False
        sections, preserved_tail_content, preserved_driver_content = result

        if _in_place:
            self._remove_owned_config(sections, ns)
            # 旧格式（无标识）无法自动移除时提示
            if any("形态键扩展配置：组内变量同步" in l for l in sections.get('[Present]', [])):
                print("检测到旧格式的形态键扩展配置（无标识标记）。请先重新导出一次 mod 以生成带标识的新配置。")
                return False

        # 未分配形态键：先解析出变量名集合，供"按身份还原屏蔽"与"滑块面板排除"共用。
        unassigned_names, masked_vars = self._collect_unassigned_masked_vars()
        active_vars = set(self._scan_shapekey_name_to_var_map().values()) if unassigned_names else set()

        # 处理上次的未分配-已屏蔽标记：仍在屏蔽集合里的保持注释、移回分组的还原、
        # 变量已改名/废弃的保守保持屏蔽（不许旧行复活）。
        restored_mask_lines, kept_mask_lines = self._unmask_all_shapekeys(
            sections, keep_masked=masked_vars, active_vars=active_vars
        )
        if kept_mask_lines:
            print(f"[形态键扩展] 保持 {kept_mask_lines} 行未分配形态键的屏蔽注释")

        freq_vars = self._scan_freq_vars_from_ini(sections)
        if not freq_vars and not _in_place:
            return False

        shapekey_names = self._scan_shapekey_names_from_variable_items()
        if not shapekey_names: shapekey_names = self._scan_shapekey_names_from_classification()
        if not shapekey_names: shapekey_names = list(freq_vars.keys())
        var_to_group, all_groups = self._build_var_to_group_map(freq_vars, shapekey_names)
        if '[Constants]' not in sections: sections['[Constants]'] = []
        const_lines = sections['[Constants]']
        # 块标记开始（标识本节点在 Constants 中的配置，供刷新定位）
        const_lines.append(self.BLOCK_BEGIN.format(ns=ns))
        const_content = "\n".join(const_lines)
        for setting in self.play_group_settings:
            # 图标来源优先级：图片 > 备注生成 > 内置默认
            if setting.use_image_as_icon and setting.button_icon_image.strip():
                comment = f"; SLOT_ICON_{setting.group_index} = {setting.button_icon_image}"
                if comment not in const_lines: const_lines.append(comment)
                print(
                    f"[形态键扩展] 分组 {setting.group_index} 使用自定义图片作为图标: "
                    f"{setting.button_icon_image}"
                )
            elif PIL_AVAILABLE and setting.use_remark_as_icon and setting.remark.strip():
                res_dir = os.path.join(mod_export_path, "res")
                os.makedirs(res_dir, exist_ok=True)
                icon_name = f"4_{setting.group_index}.png"
                icon_path = os.path.join(res_dir, icon_name)
                # 仅启用自动播放的分组：带背景/边框；未启用：透明背景仅文字
                generated = self._generate_remark_icon(
                    text=setting.remark,
                    dest_path=icon_path,
                    font_size=setting.remark_font_size,
                    font_family=setting.remark_font_family,
                    text_color=setting.remark_text_color,
                    stroke_width=setting.remark_stroke_width,
                    stroke_color=setting.remark_stroke_color,
                    with_style=bool(setting.enable_auto_playback)
                )
                if generated:
                    comment = f"; SLOT_ICON_{setting.group_index} = {os.path.abspath(generated)}"
                    if comment not in const_lines: const_lines.append(comment)
            elif setting.use_remark_as_icon and not PIL_AVAILABLE:
                print(
                    f"[形态键扩展] 分组 {setting.group_index} 勾选了「用备注生成图标」但未安装 Pillow，"
                    f"将使用内置默认图标"
                )
            elif setting.use_image_as_icon and not setting.button_icon_image.strip():
                print(
                    f"[形态键扩展] 分组 {setting.group_index} 勾选了「以图片作为图标」但未指定图片，"
                    f"将使用内置默认图标"
                )
        group_strength_vars = {}
        for g in all_groups:
            group_var = f"$Freq_Group{g}"
            group_strength_vars[g] = group_var
            if group_var not in const_content:
                setting = self._find_group_setting(g)
                remark = f" ({setting.remark})" if (setting and setting.remark) else ""
                const_lines.append(f"; 控制分组{g}{remark}的形态键强度（组内统一）")
                const_lines.append(f"global persist {group_var} = 0.0")
        for g in all_groups:
            setting = self._find_group_setting(g)
            default_val = 1 if (setting and setting.enable_auto_playback) else 0
            # 这里所有变量都用 upsert：ini 里已存在同名行时必须换成当前配置的值，
            # 否则「总细分 / 速度区间」改大后重新导出仍沿用上次的旧默认值。
            self._upsert_const_var_line(
                const_lines, f"$auto_play_enabled_group{g}",
                f"global persist $auto_play_enabled_group{g} = {default_val}"
            )
            if setting and setting.enable_auto_playback:
                self._upsert_const_var_line(
                    const_lines, f"$speed_percent_group{g}",
                    f"global persist $speed_percent_group{g} = {setting.speed_percent}"
                )
                self._upsert_const_var_line(
                    const_lines, f"$speed_percent_min_group{g}",
                    f"global persist $speed_percent_min_group{g} = {setting.speed_percent_min}"
                )
                self._upsert_const_var_line(
                    const_lines, f"$speed_percent_max_group{g}",
                    f"global persist $speed_percent_max_group{g} = {setting.speed_percent_max}"
                )
                self._upsert_const_var_line(
                    const_lines, f"$frameEnd_group{g}",
                    f"global persist $frameEnd_group{g} = {setting.auto_playback_frame_count}"
                )
        # 块标记结束
        const_lines.append(self.BLOCK_END.format(ns=ns))

        if '[Present]' not in sections: sections['[Present]'] = []
        present_lines = sections['[Present]']
        first_code_pos = len(present_lines)
        for i, line in enumerate(present_lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(';') and self.UNASSIGNED_MASK_HEAD not in line:
                # 普通注释不参与定位；但「未分配-已屏蔽」标记行要参与——它是被注释掉的
                # 真实语句，若跳过它，[Present] 里只剩屏蔽行时插入点会从行首漂到行尾，
                # 同一个 mod 反复刷新就会出现“同步块位置来回跳”的伪变更。
                continue
            first_code_pos = i
            break
        # ---- 组内变量同步块（插入式，带标识注释）----
        # 修复：同步模式不再使用 `if sync_line not in present_lines` 检查。
        # 原来的检查语义错误 —— present_lines 是"已存在的行"，而 sync_lines 是
        # 本次要新插入的行；一旦 present_lines 中残留同名字符串（例如上次手改过的
        # ini、或其它来源的行），本应写入的同步赋值就会被跳过，导致同步模式失效。
        # 直接 append 即可：group_member_vars 是去重后的列表，每行只出现一次。
        sync_lines = [f"; @@ShapeKeyExt:SYNC:{ns}@@", ""]
        sync_lines.append("; ===== 形态键扩展配置：组内变量同步 ===== ")
        for g in all_groups:
            group_var = group_strength_vars[g]
            group_member_vars = [v for v, grp in var_to_group.items() if grp == g]
            if not group_member_vars: continue
            group_member_vars.sort(key=lambda v: self._natural_sort_key(v))
            num_members = len(group_member_vars)
            setting = self._find_group_setting(g)
            remark = f" ({setting.remark})" if (setting and setting.remark) else ""
            if setting and setting.group_mode == 'SEQUENCE':
                sync_lines.append(f"; 分组{g}{remark}：序列模式（{num_members} 个形态键依次控制）")
                for idx, member_var in enumerate(group_member_vars):
                    sync_lines.append(f"    {member_var} = {group_var} * {num_members} - {idx}")
                    sync_lines.append(f"    if ({member_var} > 1)")
                    sync_lines.append(f"        {member_var} = 1")
                    sync_lines.append(f"    endif")
                    sync_lines.append(f"    if ({member_var} < 0)")
                    sync_lines.append(f"        {member_var} = 0")
                    sync_lines.append(f"    endif")
            else:
                sync_lines.append(f"; 分组{g}{remark}：同步模式（{group_var} 控制 {num_members} 个形态键）")
                for member_var in group_member_vars:
                    sync_lines.append(f"    {member_var} = {group_var}")
        sync_lines.append("; ===== 结束组内变量同步 ===== ")
        sync_lines.append("")
        for code_line in reversed(sync_lines): present_lines.insert(first_code_pos, code_line)
        # ---- 自动播放块（插入式，每个启用组一个，带标识注释）----
        for g in all_groups:
            setting = self._find_group_setting(g)
            if not setting or not setting.enable_auto_playback: continue
            percent_mode = bool(getattr(setting, "speed_percent_mode", True))
            if len(setting.speed_intervals) == 0:
                interval = setting.speed_intervals.add()
                interval.start = 0.0
                interval.end = 100.0
                # 新语义分组默认 100%（= 单位速度）；老语义分组沿用旧的步进默认值 10
                interval.base_step = 100 if percent_mode else 10
            unit_speed = self._calc_unit_speed(setting)
            auto_unroll = self._calc_auto_max_step_unroll(setting)
            max_step = max(
                (
                    self._interval_step_value(iv, unit_speed, percent_mode)
                    for iv in setting.speed_intervals
                ),
                default=self._no_interval_fallback_step(unit_speed, percent_mode),
            )
            max_sp = max(int(setting.speed_percent), int(setting.speed_percent_max), 100)
            print(
                f"[形态键扩展] 分组 {g} 单位速度 = {unit_speed:g} 细分/帧 "
                f"(总细分 {setting.auto_playback_frame_count} / "
                f"(时长 {setting.auto_playback_duration:.2f}s × 帧率 {setting.playback_fps}))"
                f"{'' if percent_mode else '  [区间按旧版步进语义解释]'}"
            )
            print(
                f"[形态键扩展] 分组 {g} 自动展开次数 = {auto_unroll} "
                f"(区间最大步进 {max_step:g} × 全局最大 {max_sp}%)"
            )
            self._add_auto_playback_logic_for_group(
                sections, g, group_strength_vars[g],
                setting.auto_playback_frame_count, setting.auto_playback_cycle_mode,
                setting.speed_percent_min, setting.speed_percent_max,
                list(setting.speed_intervals), auto_unroll, unit_speed, percent_mode,
                speed_percent=setting.speed_percent
            )

        auto_play_key = self.auto_play_toggle_key.strip() or "space"
        key_section = f"[KeyToggleAutoPlay_{ns}]"
        if key_section not in sections:
            condition = ""
            if not self.auto_play_key_global: condition = "condition = $help == 1 && $ui_active == 1\n"
            sections[key_section] = []
            if condition: sections[key_section].append(condition.rstrip())
            sections[key_section].append(f"key = {auto_play_key}")
            sections[key_section].append("type = press")
            toggle_lines = [f"$auto_play_enabled_group{g} = 1 - $auto_play_enabled_group{g}" for g in all_groups]
            sections[key_section].append(f"run = CommandListToggleAutoPlay_{ns}")
            cmd_section = f"[CommandListToggleAutoPlay_{ns}]"
            if cmd_section not in sections: sections[cmd_section] = toggle_lines
            for g in all_groups:
                cmd_g = f"[CommandListToggleAutoPlayGroup{g}_{ns}]"
                if cmd_g not in sections: sections[cmd_g] = [f"$auto_play_enabled_group{g} = 1 - $auto_play_enabled_group{g}"]
        # 【已合并】滑块面板-自定义
        # 未分配（被屏蔽）的形态键不建滑块：否则会留下点了没反应的死滑块，
        # 并生成 `$paramN = $被屏蔽变量` 这类读取已注释声明变量的行。
        if self.use_slider_panel:
            self._apply_slider_panel(
                mod_export_path, target_ini_file, sections, exclude_vars=masked_vars
            )

        # ---- 未分配形态键屏蔽：把变量声明 / Present 赋值 / Shader 输入注释掉 ----
        if unassigned_names:
            if masked_vars:
                new_masked, already_masked, unmatched_vars = self._mask_unassigned_shapekeys(
                    sections, masked_vars
                )
                matched_vars = sorted(set(masked_vars) - set(unmatched_vars))
                print(
                    f"[形态键扩展] 未分配形态键屏蔽：本次新增 {new_masked} 行、"
                    f"沿用上次 {already_masked} 行；已处理 {len(matched_vars)} 个: {matched_vars}"
                )
                if unmatched_vars:
                    print(
                        f"[形态键扩展] 警告：以下未分配形态键在 ini 里找不到对应行"
                        f"（可能变量名已被刷新，ini 需重新导出一次），当前未产生屏蔽: "
                        f"{unmatched_vars}"
                    )
            else:
                print(
                    f"[形态键扩展] 检测到 {len(unassigned_names)} 个未分配形态键，"
                    f"但未能从上游节点解析出变量名映射（跳过屏蔽）"
                )
        if restored_mask_lines and not masked_vars:
            print(
                f"[形态键扩展] 警告：本次没有可屏蔽的未分配形态键，但已还原了上次的 "
                f"{restored_mask_lines} 行屏蔽注释（上游节点未连线/映射失败时会出现）。"
                f"若这些形态键本应保持失效，请检查形态键节点的连线与「导出变量」设置。"
            )

        # ---- 不变量校验：屏蔽之后，输出里不应再有对被屏蔽变量的有效引用 ----
        # （滑块面板之外若还有别的模块读/写这些变量，这里会显式报出来，而不是静默错下去）
        if masked_vars:
            leftovers = self._collect_live_references(sections, masked_vars)
            if leftovers:
                preview = "; ".join(f"{sec}#{idx}: {text}" for sec, idx, text in leftovers[:5])
                print(
                    f"[形态键扩展] 警告：屏蔽后仍有 {len(leftovers)} 处有效引用未处理：{preview}"
                    f"{' …' if len(leftovers) > 5 else ''}"
                )

        # ---- 未列入分组条目的形态键：升级后不再跟随分组 1 ----
        # 旧版本把“没有任何条目的形态键”默认归到分组 1，会跟着 $Freq_Group1 一起动；
        # 现在按未分组处理（保持 IniParams 默认值）。这里显式提示，避免静默变化。
        entry_names = {e.shape_key_name for e in self.play_group_entries}
        orphan_labels = sorted({
            info.get("label", "")
            for info in freq_vars.values()
            if info.get("label") and info.get("label") not in entry_names
        })
        if orphan_labels:
            print(
                f"[形态键扩展] 提示：{len(orphan_labels)} 个形态键没有分组条目，"
                f"按未分组处理（不再跟随分组 1）：{orphan_labels}"
            )

        self._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
        print(f"形态键扩展配置已{'原地更新' if _in_place else '合并到'}: {os.path.basename(target_ini_file)}")
        return True


classes = (
    ShapeKeyPlayGroupItem, ShapeKeySpeedIntervalItem, ShapeKeyPlayGroupSettings,
    SSMTNode_PostProcess_ShapeKeyExt, SSMT_OT_ShapeKeyExtSetGroup,
    SSMT_OT_ScanShapeKeyExt, SSMT_OT_SpeedIntervalAdd, SSMT_OT_SpeedIntervalRemove,
    SSMT_OT_SelectPlayGroup, SSMT_OT_OpenGroupSettings, SSMT_OT_ShapeKeyMoveUpDown,
    SSMT_OT_ShapeKeyAssignToCurrentGroup, SSMT_OT_ShapeKeyUnassignFromGroup,
    SSMT_OT_ShapeKeyGroupAdd, SSMT_OT_ShapeKeyGroupDuplicate, SSMT_OT_ShapeKeyGroupRemove,
    SSMT_OT_ShapeKeyGroupMoveUpDown,
    SSMT_OT_ShapeKeyExt_ParseSliderObject, SSMT_OT_ShapeKeyExt_Refresh,
    SSMT_OT_KeyPicker, SSMT_OT_KeyPickerPick,
)

# 快捷键选择器的弹窗状态：WindowManager 动态属性（唯一 ssmt_kp_ 前缀，不挂到节点上）
KEY_PICKER_WM_PROPS = (
    "ssmt_kp_combo", "ssmt_kp_prop", "ssmt_kp_node", "ssmt_kp_tree", "ssmt_kp_just_picked",
)


def register():
    for cls in classes: bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    if not hasattr(wm, "ssmt_kp_combo"):
        wm.ssmt_kp_combo = bpy.props.StringProperty(default="")
    if not hasattr(wm, "ssmt_kp_prop"):
        wm.ssmt_kp_prop = bpy.props.StringProperty(default="")
    if not hasattr(wm, "ssmt_kp_node"):
        wm.ssmt_kp_node = bpy.props.StringProperty(default="")
    if not hasattr(wm, "ssmt_kp_tree"):
        wm.ssmt_kp_tree = bpy.props.StringProperty(default="")
    if not hasattr(wm, "ssmt_kp_just_picked"):
        wm.ssmt_kp_just_picked = bpy.props.BoolProperty(default=False)


def unregister():
    wm = bpy.types.WindowManager
    for prop_name in KEY_PICKER_WM_PROPS:
        if hasattr(wm, prop_name):
            try:
                delattr(wm, prop_name)
            except Exception:
                pass
    for cls in reversed(classes): bpy.utils.unregister_class(cls)