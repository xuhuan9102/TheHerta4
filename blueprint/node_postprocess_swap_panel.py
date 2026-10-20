"""
物体切换面板 后处理节点
================================
读取蓝图中的物体切换节点、贴图切换后处理节点，以及 mod 的 ini 中的切换配置
([KeySwap_*] 段落)，生成一个类似「滑块面板」的图形面板。

面板中为每个物体切换生成一个按钮（没有滑块），点击按钮的行为与按下该切换对应的
按键（KeySwap）完全一致：循环切换 $swapkeyX 的值。

当蓝图中存在形态键滑块面板（SSMTNode_PostProcess_ShapeKeyExt 且启用了滑块面板）时，
本节点不再生成独立的浮动面板，而是把切换按钮附加到滑块面板的左侧，复用滑块面板的
坐标系与交互状态（位置/缩放/显隐/拖拽/鼠标点击）。若不存在滑块面板，则回退原有的
独立面板机制。
"""

import math
import os
import glob
import re
import shutil
import uuid
from collections import OrderedDict

import bpy

from .node_postprocess_base import SSMTNode_PostProcess_Base

try:
    from .node_swap_ini import SwapKeyINIGenerator
    SWAP_GENERATOR_AVAILABLE = True
except ImportError:
    SWAP_GENERATOR_AVAILABLE = False

try:
    from .variable_registry import get_node_variable_name
    VARIABLE_REGISTRY_AVAILABLE = True
except ImportError:
    VARIABLE_REGISTRY_AVAILABLE = False

try:
    from .node_postprocess_diffuse_switch import (
        diffuse_group_hotkey,
        diffuse_group_option_count,
        diffuse_group_variable_name,
    )
    DIFFUSE_SWITCH_AVAILABLE = True
except ImportError:
    DIFFUSE_SWITCH_AVAILABLE = False

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw as PILDraw
    from PIL import ImageFont as PILImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class SSMT_SwapPanelEntry(bpy.types.PropertyGroup):
    """面板中一个物体切换按钮的条目信息（仅用于节点 UI 预览）。"""
    variable_name: bpy.props.StringProperty(name="变量名", default="")   # 如 $swapkey6
    variables: bpy.props.StringProperty(name="变量列表", default="")      # 如 $swapkey3|$swapkey4
    comment: bpy.props.StringProperty(name="备注", default="")
    option_count: bpy.props.IntProperty(name="选项数", default=2, min=1, max=1024)
    hotkey: bpy.props.StringProperty(name="按键", default="")
    node_name: bpy.props.StringProperty(name="节点", default="")


class SSMT_SwapPanelButtonEntry(bpy.types.PropertyGroup):
    """One generated control button and its optional custom image."""
    label: bpy.props.StringProperty(name="功能", default="")
    hotkey: bpy.props.StringProperty(name="按键", default="", options={'HIDDEN'})
    variables: bpy.props.StringProperty(name="变量", default="", options={'HIDDEN'})
    image_path: bpy.props.StringProperty(name="按钮图片", subtype='FILE_PATH', default="")


class SSMT_OT_SwapPanel_ParseObject(bpy.types.Operator):
    bl_idname = "ssmt.swap_panel_parse_object"
    bl_label = "解析物体"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_SwapPanel':
                    node._update_from_object()
                    self.report({'INFO'}, f"已解析物体 '{node.target_object}' -> 哈希: {node.detect_hash}, IndexCount: {node.detect_index_count}")
                    return {'FINISHED'}
        self.report({'WARNING'}, "无法找到物体切换面板节点")
        return {'CANCELLED'}


class SSMT_OT_SwapPanel_Scan(bpy.types.Operator):
    bl_idname = "ssmt.swap_panel_scan"
    bl_label = "刷新物体切换列表"
    bl_description = "从蓝图收集物体切换与贴图切换节点，并读取 INI 中的 [KeySwap_*] 配置"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_SwapPanel':
                    count = node._refresh_entries()
                    self.report({'INFO'}, f"已刷新物体切换列表，共 {count} 项")
                    return {'FINISHED'}
        self.report({'WARNING'}, "无法找到物体切换面板节点")
        return {'CANCELLED'}


class SSMT_OT_SwapPanel_Refresh(bpy.types.Operator):
    bl_idname = "ssmt.swap_panel_refresh"
    bl_label = "应用设置到Mod"
    bl_description = "按节点当前设置，原地更新 mod ini 中物体切换面板的配置与图片（无需重新导出整个 mod）"
    bl_options = {'REGISTER', 'INTERNAL'}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_SwapPanel':
                    ini_path = (node.ini_file_path or "").strip() or node.last_mod_ini_path
                    if not ini_path or not os.path.isfile(ini_path):
                        self.report({'WARNING'}, "未找到目标 ini：请先导出一次 mod，或在节点「INI文件」中指定")
                        return {'CANCELLED'}
                    ok = node.execute_postprocess(os.path.dirname(ini_path), _in_place=True, _ini_path=ini_path)
                    if ok:
                        self.report({'INFO'}, f"已原地刷新物体切换面板: {os.path.basename(ini_path)}")
                        return {'FINISHED'}
                    self.report({'ERROR'}, "刷新失败，请查看控制台日志（旧格式可能需要先重新导出一次）")
                    return {'CANCELLED'}
        self.report({'WARNING'}, "无法找到物体切换面板节点")
        return {'CANCELLED'}


class SSMTNode_PostProcess_SwapPanel(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_SwapPanel'
    bl_label = '物体切换面板'
    bl_description = (
        '读取蓝图物体切换节点与 mod 的 KeySwap 配置，生成带按钮的图形切换面板（点击按钮=按下切换按键）。'
        '若蓝图中存在「确实会产出」滑块面板的形态键扩展节点，则按钮附加到该滑块面板左侧，'
        '复用其坐标系与交互状态；此时该形态键扩展节点必须排在本节点之前连接（后处理链执行顺序），'
        '否则点击面板外的按钮会连带把滑块面板关闭（导出前会校验并报错）'
    )

    def init(self, context):
        super().init(context)
        self.width = 600  # 基类默认 300 的 2 倍（适配左右两列 UI）

    # ---- 配置行专有标识（生成时留在 ini 中，供「应用设置到Mod」精准定位与原地更新设置项）----
    PANEL_TAG = "SSMTSwapPanel"
    BLOCK_BEGIN = "; @@SSMTSwapPanel:BLOCK:BEGIN:{ns}@@"
    BLOCK_END = "; @@SSMTSwapPanel:BLOCK:END:{ns}@@"
    PRESENT_BEGIN = "; ========== SWAP PANEL LOGIC (appended) [{ns}] =========="
    PRESENT_END = "; ========== SWAP PANEL LOGIC END (appended) [{ns}] =========="
    DUP_GUARD = "SWAP PANEL LOGIC (appended)"
    GUI_GUARD_MARKER = "; @@SSMTSwapPanel:gui_guard:{ns}@@"

    # 附加模式专用标识（用于区分独立面板与附加到滑块面板的按钮）
    ATTACHED_FLAG = "; @@SSMTSwapPanel:attached_to_slider@@"
    ATTACHED_PRESENT_BEGIN = "; ========== SWAP PANEL ATTACHED LOGIC (appended) [{ns}] =========="
    ATTACHED_PRESENT_END = "; ========== SWAP PANEL ATTACHED LOGIC END (appended) [{ns}] =========="
    ATTACHED_PRESENT_MARKER = "; @@SSMTSwapPanel:attached_present:{ns}@@"

    # 滑块面板真正产出后才存在的标记与「无命名空间」共用变量
    # （定义见 node_postprocess_shapekey_ext._apply_slider_panel 的 [Constants] 段）
    SLIDER_PANEL_MARKER = "SLIDER PANEL CUSTOM LOGIC (appended)"
    SLIDER_PANEL_SHARED_VARS = (
        "$img0_x", "$img0_y", "$zoom0", "$help", "$ui_active",
        "$mouse_clicked", "$is_dragging", "$click_outside",
    )

    create_cumulative_backup: bpy.props.BoolProperty(name="创建累积备份", default=True)

    help_key: bpy.props.StringProperty(name="显示/隐藏面板", default="home")
    reset_key: bpy.props.StringProperty(name="重置位置", default="ctrl home")
    zoom_in_key: bpy.props.StringProperty(name="放大", default="up")
    zoom_out_key: bpy.props.StringProperty(name="缩小", default="down")
    drag_key: bpy.props.StringProperty(name="拖拽键", default="VK_LBUTTON")
    gui_only: bpy.props.BoolProperty(
        name="GUI全局模式",
        description="启用后屏蔽物体切换原始按键，只允许使用面板按钮",
        default=False,
    )

    button_height: bpy.props.FloatProperty(name="按钮高度", default=0.05, min=0.001, max=1.0, precision=4)
    button_width: bpy.props.FloatProperty(
        name="按钮默认宽度",
        description="按钮宽度；设置为 0 时按各按钮图片比例自动计算",
        default=0.0, min=0.0, max=1.0, precision=4,
    )
    buttons_per_row: bpy.props.IntProperty(name="每行按钮数", default=1, min=1, max=1024)
    button_column_spacing: bpy.props.FloatProperty(name="列间距", default=0.02, min=0.0, max=1.0, precision=4)
    button_row_spacing: bpy.props.FloatProperty(name="行间距", default=0.02, min=0.0, max=1.0, precision=4)
    button_top_padding: bpy.props.FloatProperty(name="顶部留白", default=0.03, min=0.0, max=1.0, precision=4)
    panel_min_height: bpy.props.FloatProperty(name="面板最小高度", default=0.75, min=0.01, max=1.0, precision=4)

    panel_default_scale: bpy.props.FloatProperty(
        name="面板默认缩放",
        description="面板默认显示的缩放比例（1.0 = 原始大小；可在游戏内用放大/缩小键再调节）",
        default=1.0, min=0.1, max=5.0, precision=2
    )

    def _update_target_object(self, context):
        self._update_from_object()

    target_object: bpy.props.StringProperty(name="目标物体", default="", update=_update_target_object)
    detect_hash: bpy.props.StringProperty(name="哈希值", default="")
    detect_index_count: bpy.props.StringProperty(name="IndexCount", default="")

    background_image: bpy.props.StringProperty(name="背景图片", subtype='FILE_PATH', default="")
    button_image: bpy.props.StringProperty(name="按钮图片（全局回退）", subtype='FILE_PATH', default="")
    button_border_image: bpy.props.StringProperty(name="按钮边框图片", subtype='FILE_PATH', default="")

    # ---- 备注文字图标（用切换备注自动生成按钮图标）----
    # 完整优先级：单按钮图片 → 全局回退图片 → 备注文字图标 → 纯色默认按钮图
    use_remark_as_icon: bpy.props.BoolProperty(
        name="用备注生成图标",
        description="勾选后，没有单独指定图片的按钮会用其备注文字自动生成图标（需要安装 Pillow）",
        default=True,
    )
    remark_font_family: bpy.props.EnumProperty(
        name="字体",
        description="备注文字图标使用的字体",
        items=[
            ('msyh.ttc', "微软雅黑", ""),
            ('simsun.ttc', "宋体", ""),
            ('simhei.ttf', "黑体", ""),
            ('arial.ttf', "Arial", ""),
        ],
        default='msyh.ttc',
    )
    remark_font_size: bpy.props.IntProperty(name="字号大小", default=36, min=10, max=300)
    remark_text_color: bpy.props.FloatVectorProperty(
        name="文字颜色", subtype='COLOR', default=(1.0, 1.0, 1.0), min=0.0, max=1.0, size=3
    )
    remark_stroke_color: bpy.props.FloatVectorProperty(
        name="描边颜色", subtype='COLOR', default=(0.0, 0.0, 0.0), min=0.0, max=1.0, size=3
    )
    remark_stroke_width: bpy.props.IntProperty(name="描边粗细", default=4, min=0, max=20)

    # ---- 按钮样式（文字图标底色/边框，以及没有图片时的默认按钮图）----
    button_bg_color: bpy.props.FloatVectorProperty(
        name="按钮背景色", subtype='COLOR', default=(0.16, 0.22, 0.32), min=0.0, max=1.0, size=3
    )
    button_border_color: bpy.props.FloatVectorProperty(
        name="按钮边框色", subtype='COLOR', default=(0.59, 0.75, 0.94), min=0.0, max=1.0, size=3
    )
    button_border_width: bpy.props.IntProperty(name="边框宽度", default=2, min=0, max=20)
    button_opacity: bpy.props.FloatProperty(
        name="按钮透明度", default=0.9, min=0.0, max=1.0, precision=2
    )
    button_align: bpy.props.EnumProperty(
        name="按钮对齐",
        description="按钮组在面板内的水平对齐方式（仅独立面板模式生效；附加模式下按钮总在滑块面板左侧）",
        items=[('LEFT', "左对齐", ""), ('CENTER', "居中", ""), ('RIGHT', "右对齐", "")],
        default='CENTER',
    )

    # ---- 面板背景样式（圆角 + 边框，对背景图应用；自定义背景同样生效）----
    background_corner_radius: bpy.props.IntProperty(name="背景圆角", default=5, min=0, max=100)
    background_border_color: bpy.props.FloatVectorProperty(
        name="背景边框色", subtype='COLOR', default=(0.59, 0.75, 0.94), min=0.0, max=1.0, size=3
    )
    background_border_width: bpy.props.IntProperty(name="背景边框宽度", default=3, min=0, max=20)
    background_opacity: bpy.props.FloatProperty(name="背景透明度", default=0.85, min=0.0, max=1.0, precision=2)

    check_hash: bpy.props.StringProperty(name="检测Hash值", default="")
    match_index_count: bpy.props.IntProperty(name="Match Index Count", default=0, min=0)

    # 可选：用于预览/导出的物体切换配置 INI 文件
    # 留空时导出会自动读取目标 mod 的 ini（其中的 [KeySwap_*] 段落）
    ini_file_path: bpy.props.StringProperty(name="INI文件", subtype='FILE_PATH', default="")

    # 面板实例唯一命名空间：用于与滑块面板等其它面板隔离（自动生成，复制节点时重置）
    namespace: bpy.props.StringProperty(
        name="命名空间",
        description="面板实例唯一标识，用于隔离不同面板的变量/段落（自动生成）",
        default="",
        options={'HIDDEN'},
    )

    last_mod_ini_path: bpy.props.StringProperty(
        name="上次导出INI",
        description="最近一次导出时生成的 mod ini 路径，供「应用设置到Mod」原地更新",
        default="",
        options={'HIDDEN'},
    )

    swap_panel_entries: bpy.props.CollectionProperty(type=SSMT_SwapPanelEntry)
    swap_panel_button_entries: bpy.props.CollectionProperty(type=SSMT_SwapPanelButtonEntry)

    def _ensure_namespace(self):
        """生成/返回面板实例唯一命名空间（稳定持久，保证位置/缩放跨导出保留）。"""
        if not self.namespace:
            self.namespace = "swp_" + uuid.uuid4().hex[:8]
        return self.namespace

    def copy(self, node):
        # 复制节点时重置命名空间，避免与源节点共用同一套面板变量/段落
        self.namespace = ""

    # ==========================================
    # 查找形态键滑块面板（附加模式）
    # ==========================================
    def _find_slider_panel_node(self):
        """在当前蓝图（含嵌套蓝图）中查找**确实会产出**滑块面板的形态键扩展节点。

        找到时，本节点的切换按钮会附加到该滑块面板的左侧，复用其坐标系与交互状态。
        未找到时返回 None，走原有独立面板机制。
        """
        tree = getattr(self, "id_data", None)
        if tree is None:
            return None
        return self._find_slider_panel_node_in_tree(tree, set())

    @staticmethod
    def _node_emits_slider_panel(node):
        """该形态键扩展节点是否**真的**会写出滑块面板。

        只勾了 ``use_slider_panel`` 还不够（详见 ``node_postprocess_shapekey_ext`` 的
        ``will_emit_slider_panel``）：没有可建滑块的形态键参数时它不产出面板，附加模式
        引用的 ``$img0_x`` / ``$zoom0`` 等变量在 ini 里根本不存在。判定直接问节点自己，
        与导出代码同源；节点类型没有该方法（旧版本 / 测试替身）时退回开关判定。
        """
        predicate = getattr(node, "will_emit_slider_panel", None)
        if callable(predicate):
            try:
                return bool(predicate())
            except Exception as error:
                print(f"[物体切换面板] 判断滑块面板产出前提失败，退回 use_slider_panel: {error}")
        return bool(getattr(node, "use_slider_panel", False))

    def _find_slider_panel_node_in_tree(self, tree, visited):
        tree_key = getattr(tree, "name", str(id(tree)))
        if tree_key in visited:
            return None
        visited.add(tree_key)
        for node in tree.nodes:
            if getattr(node, "mute", False):
                continue
            if node.bl_idname == 'SSMTNode_PostProcess_ShapeKeyExt':
                if self._node_emits_slider_panel(node):
                    return node
            elif node.bl_idname == 'SSMTNode_Blueprint_Nest':
                blueprint_name = str(getattr(node, "blueprint_name", "") or "")
                nested = bpy.data.node_groups.get(blueprint_name) if blueprint_name and blueprint_name != "NONE" else None
                if nested and getattr(nested, "bl_idname", "") == "SSMTBlueprintTreeType":
                    result = self._find_slider_panel_node_in_tree(nested, visited)
                    if result is not None:
                        return result
        return None

    @classmethod
    def _slider_panel_emitted(cls, ini_file_path):
        """目标 ini 里是否**真的**产出了形态键滑块面板。

        附加模式复用滑块面板的坐标系与交互状态（`$img0_x`/`$img0_y`/`$zoom0`/
        `$help`/`$ui_active`/`$mouse_clicked`/`$is_dragging`/`$click_outside` 都是
        **无命名空间**的全局变量）。这些变量只在滑块面板真的写出时才存在；节点勾了开关
        却没有产出（没有可建滑块的形态键参数、节点不在后处理链上等）时，附加按钮会引用
        未定义变量，3Dmigoto 侧静默取 0。因此在真正走附加模式之前用 ini 内容做一次证据校验。
        """
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as handle:
                text = handle.read()
        except Exception as error:
            print(f"[物体切换面板] 读取 ini 校验滑块面板产出失败: {error}")
            return False
        if cls.SLIDER_PANEL_MARKER not in text:
            return False
        for var in cls.SLIDER_PANEL_SHARED_VARS:
            pattern = rf'^\s*global(?:\s+persist)?\s+{re.escape(var)}\s*(?:=|$)'
            if not re.search(pattern, text, re.MULTILINE):
                return False
        return True

    # ==========================================
    # 原地更新：按专有标识移除本面板旧配置
    # ==========================================
    def _is_owned_section(self, sec_name, ns):
        """判断该段落是否属于本物体切换面板实例（按命名空间段落名识别）。"""
        s = sec_name.strip()
        fixed = [
            f"[KeyHelp_{ns}]", f"[KeyResetPosition_{ns}]", f"[KeyZoomIn_{ns}]",
            f"[KeyZoomOut_{ns}]", f"[KeyMouseDrag_{ns}]",
            f"[CommandListZoomIn_{ns}]", f"[CommandListZoomOut_{ns}]",
            f"[ResourceImageToRender0_{ns}]", f"[CustomShaderDraw_{ns}]",
            f"[TextureOverrideCheckHash_{ns}]",
        ]
        if s in fixed:
            return True
        suffix = f"_{ns}]"
        if s.startswith("[CommandListSwap") and s.endswith(suffix):
            return True
        if s.startswith("[ResourceSwapButton") and s.endswith(suffix):
            return True
        return False

    def _remove_owned_config(self, sections, ns):
        """按专有标识移除本面板在 ini 中的旧配置（Constants 块、自有段落、Present 块）。"""
        begin_marker = self.BLOCK_BEGIN.format(ns=ns)
        end_marker = self.BLOCK_END.format(ns=ns)
        if '[Constants]' in sections:
            new_lines = []
            skipping = False
            for line in sections['[Constants]']:
                if begin_marker in line:
                    skipping = True
                    continue
                if end_marker in line:
                    skipping = False
                    continue
                if not skipping:
                    new_lines.append(line)
            sections['[Constants]'] = new_lines

        for sec_name in list(sections.keys()):
            if self._is_owned_section(sec_name, ns):
                del sections[sec_name]

        if '[Present]' in sections:
            new_lines = []
            skipping = False
            present_begin = self.PRESENT_BEGIN.format(ns=ns)
            present_end = self.PRESENT_END.format(ns=ns)
            attached_begin = self.ATTACHED_PRESENT_BEGIN.format(ns=ns)
            attached_end = self.ATTACHED_PRESENT_END.format(ns=ns)
            for line in sections['[Present]']:
                if present_begin in line or attached_begin in line:
                    skipping = True
                    continue
                if present_end in line or attached_end in line:
                    skipping = False
                    continue
                if not skipping:
                    new_lines.append(line)
            sections['[Present]'] = new_lines

    def _remove_gui_only_guards(self, sections, ns):
        """Remove this panel's keyboard guards before regenerating settings."""
        guard_expr = f"&& ${ns}_gui_only == 0"
        marker = self.GUI_GUARD_MARKER.format(ns=ns)
        for sec_name, lines in sections.items():
            new_lines = []
            marker_pending = False
            for line in lines:
                if marker in line:
                    marker_pending = True
                    continue
                if marker_pending and line.strip().lower().startswith("condition ="):
                    marker_pending = False
                    continue
                marker_pending = False
                if line.strip().lower().startswith("condition =") and guard_expr in line:
                    line = line.replace(guard_expr, "").rstrip()
                    match = re.match(
                        r"^(\s*condition\s*=\s*)\((.*)\)\s*$",
                        line,
                        re.IGNORECASE | re.DOTALL,
                    )
                    if match:
                        inner = match.group(2)
                        depth = 0
                        balanced = True
                        for char in inner:
                            if char == "(":
                                depth += 1
                            elif char == ")":
                                depth -= 1
                                if depth < 0:
                                    balanced = False
                                    break
                        if balanced and depth == 0:
                            line = f"{match.group(1)}{inner}"
                new_lines.append(line)
            sections[sec_name] = new_lines

    def _apply_gui_only_guards(self, sections, ns, buttons):
        """Gate the original KeySwap sections while leaving panel commands free."""
        if not self.gui_only:
            return
        variables = set()
        for button in buttons:
            variables.update(button.get("var_names") or [button.get("var_name", "")])
        guard = f"${ns}_gui_only == 0"
        marker = self.GUI_GUARD_MARKER.format(ns=ns)
        for sec_name, lines in sections.items():
            # Velo/EFMI emits [KeySwapSwapkeyN], while WWMI and older
            # generators commonly emit [KeySwap_SwapkeyN]. Both are original
            # keyboard controls and must be gated in GUI-only mode.
            if not re.match(r"^\[KeySwap(?:_|[^\]])[^\]]*\]$", sec_name.strip(), re.IGNORECASE):
                continue
            if not any(
                re.match(rf"^\s*{re.escape(var)}\s*=", line, re.IGNORECASE)
                for var in variables
                for line in lines
            ):
                continue
            condition_index = next(
                (index for index, line in enumerate(lines)
                 if line.strip().lower().startswith("condition =")),
                None,
            )
            if condition_index is None:
                lines.insert(0, marker)
                lines.insert(1, f"condition = {guard}")
            elif guard not in lines[condition_index]:
                condition_match = re.match(
                    r"^(\s*condition\s*=\s*)(.*)$",
                    lines[condition_index],
                    re.IGNORECASE | re.DOTALL,
                )
                if condition_match:
                    lines[condition_index] = (
                        f"{condition_match.group(1)}({condition_match.group(2)}) && {guard}"
                    )

    # ==========================================
    # UI
    # ==========================================
    def draw_buttons(self, context, layout):
        layout.prop(self, "create_cumulative_backup")

        # 顶部：原地刷新（应用设置到Mod，无需重新导出）
        box_top = layout.box()
        box_top.operator("ssmt.swap_panel_refresh", text="应用设置到Mod（原地更新）", icon='FILE_TICK').node_name = self.name
        box_top.prop(self, "ini_file_path", text="INI文件（可选，留空用上次导出）")
        box_top.label(text="修改设置后点上方按钮即可原地更新，无需重新导出", icon='INFO')
        layout.separator()

        # 提示：是否检测到形态键滑块面板
        slider_node = self._find_slider_panel_node()
        # 附加模式：按钮附加到滑块面板左侧，复用其位置/缩放/显隐；
        # 下列由滑块面板统一负责的设置项在该模式下无效，自动隐藏。
        attached_mode = slider_node is not None
        info_box = layout.box()
        if slider_node is not None:
            info_box.label(text=f"已检测到形态键滑块面板（{slider_node.name}）", icon='CHECKMARK')
            info_box.label(text="切换按钮将附加到滑块面板左侧，复用其位置/缩放/显隐", icon='INFO')
        else:
            info_box.label(text="未检测到形态键滑块面板，使用独立浮动面板机制", icon='INFO')
        layout.separator()

        # 左右两列：左=基础设置/检测/列表，右=图片资源设置
        split = layout.split(factor=0.5)
        col_left = split.column()
        col_right = split.column()

        # ---- 左列 ----
        box = col_left.box()
        box.label(text="快捷键设置", icon='KEYINGSET')
        col = box.column(align=True)
        if not attached_mode:
            col.prop(self, "help_key", text="显示/隐藏面板")
            col.prop(self, "reset_key", text="重置位置")
            col.prop(self, "zoom_in_key", text="放大")
            col.prop(self, "zoom_out_key", text="缩小")
            col.prop(self, "drag_key", text="拖拽键")
        col.prop(self, "gui_only", text="GUI全局模式")

        box = col_left.box()
        box.label(text="UI尺寸设置", icon='PROPERTIES')
        col = box.column(align=True)
        if not attached_mode:
            col.prop(self, "panel_default_scale", text="面板默认缩放")
            col.separator()
        col.prop(self, "button_height", text="按钮高度")
        col.prop(self, "button_width", text="按钮默认宽度")
        col.prop(self, "buttons_per_row", text="每行按钮数")
        row = col.row(align=True)
        row.prop(self, "button_column_spacing", text="列间距")
        row.prop(self, "button_row_spacing", text="行间距")
        col.prop(self, "button_top_padding", text="顶部留白")
        if not attached_mode:
            col.prop(self, "panel_min_height", text="面板最小高度")
        col.label(text="按钮/面板背景尺寸按图片比例自动计算", icon='INFO')

        if not attached_mode:
            box = col_left.box()
            box.label(text="角色检测设置", icon='VIEWZOOM')
            row = box.row(align=True)
            row.prop_search(self, "target_object", bpy.data, "objects", text="物体", icon='OBJECT_DATA')
            row.operator("ssmt.swap_panel_parse_object", text="", icon='FILE_REFRESH').node_name = self.name
            box.prop(self, "detect_hash", text="哈希值")
            box.prop(self, "detect_index_count", text="IndexCount")

        box = col_left.box()
        box.label(text="物体/贴图切换按钮列表", icon='SHADERFX')
        row = box.row(align=True)
        row.operator("ssmt.swap_panel_scan", text="刷新列表", icon='FILE_REFRESH').node_name = self.name
        box.label(text="修改设置后点顶部「应用设置到Mod」即可原地更新", icon='INFO')
        box.label(text="留空则导出时自动读取目标 mod 的 ini", icon='INFO')
        if self.swap_panel_entries:
            for entry in self.swap_panel_entries:
                note = entry.comment or "(无备注)"
                vars_text = entry.variables.replace("|", " / ") or entry.variable_name
                box.label(text=f"  {vars_text}  |  {note}  |  {entry.option_count}项", icon='NONE')
        else:
            box.label(text="未检测到切换，点击「刷新列表」", icon='INFO')

        # ---- 右列（图片资源 + 备注文字图标设置）----
        box = col_right.box()
        box.label(text="面板图片资源（自定义）", icon='TEXTURE')
        if not attached_mode:
            box.prop(self, "background_image", text="背景（仅独立面板模式生效）")
            box.prop(self, "button_image", text="按钮全局回退")
        box.prop(self, "button_border_image", text="按钮边框")
        box.label(text="优先级：单按钮图片 → 全局回退 → 备注文字图标 → 默认按钮", icon='INFO')
        box.label(text="边框会缩放并叠加到所有按钮，建议使用透明PNG", icon='INFO')
        if self.swap_panel_button_entries:
            box.separator()
            box.label(text="各控制按钮图片", icon='IMAGE_DATA')
            for entry in self.swap_panel_button_entries:
                row = box.row(align=True)
                row.label(text=entry.label or "(无备注)", icon='NONE')
                row.prop(entry, "image_path", text="")

        # ---- 备注文字图标 + 按钮样式（合并为一个区域；未勾选备注图标或附加模式下按钮样式组隐藏）----
        box = col_right.box()
        box.label(text="备注文字图标（自动生成）", icon='FILE_FONT')

        # 按钮样式（背景色 / 边框色 / 边框宽度 / 透明度 / 对齐）只在附加模式下无意义：
        # 附加模式的按钮外观由滑块面板统一控制。未勾选「用备注生成图标」时默认纯色按钮图
        # 仍然读这些参数（见 _ensure_button_image），因此不能跟着 use_remark_as_icon 一起隐藏。
        show_button_style = not attached_mode
        if PIL_AVAILABLE:
            box.prop(self, "use_remark_as_icon", text="用备注生成图标")
            if self.use_remark_as_icon:
                remark_col = box.column(align=True)
                remark_col.prop(self, "remark_font_family", text="字体")
                remark_col.prop(self, "remark_font_size", text="字号")
                remark_row = remark_col.row(align=True)
                remark_row.prop(self, "remark_text_color", text="文字色")
                remark_row.prop(self, "remark_stroke_color", text="描边色")
                remark_col.prop(self, "remark_stroke_width", text="描边粗细")
                box.label(text="备注里输入 / 可强制换行；已单独设置图片的按钮不受影响", icon='INFO')
        else:
            box.label(text="未安装 Pillow，无法用备注生成图标", icon='ERROR')

        if show_button_style:
            if PIL_AVAILABLE:
                box.separator()
            box.label(text="按钮样式（文字图标/默认按钮）", icon='COLOR')
            style_col = box.column(align=True)
            style_row = style_col.row(align=True)
            style_row.prop(self, "button_bg_color", text="背景色")
            style_row.prop(self, "button_border_color", text="边框色")
            style_col.prop(self, "button_border_width", text="边框宽度")
            style_col.prop(self, "button_opacity", text="透明度")
            style_col.prop(self, "button_align", text="按钮对齐")

        if not attached_mode:
            box = col_right.box()
            box.label(text="面板背景样式（圆角/边框）", icon='MATERIAL')
            bg_col = box.column(align=True)
            bg_col.prop(self, "background_corner_radius", text="背景圆角")
            bg_col.prop(self, "background_border_color", text="边框色")
            bg_col.prop(self, "background_border_width", text="边框宽度")
            bg_col.prop(self, "background_opacity", text="透明度")
            box.label(text="对背景图应用圆角/边框（自定义背景同样生效）", icon='INFO')

    # ==========================================
    # 扫描 / 解析
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
                print(f"[SwapPanel] 无法从物体名称 '{obj_name}' 解析哈希和IndexCount")

    def _collect_swap_nodes(self):
        """从蓝图收集物体切换节点（含嵌套蓝图）。"""
        tree = getattr(self, "id_data", None)
        if tree is None or not SWAP_GENERATOR_AVAILABLE:
            return []
        try:
            return SwapKeyINIGenerator.collect_all_swap_nodes_from_blueprint(tree)
        except Exception:
            return []

    def _collect_diffuse_groups(self):
        """Collect enabled V5.1 diffuse groups from this and nested blueprints."""
        tree = getattr(self, "id_data", None)
        if tree is None or not DIFFUSE_SWITCH_AVAILABLE:
            return []
        collected = []
        visited = set()

        def collect(current_tree):
            tree_key = getattr(current_tree, "name", str(id(current_tree)))
            if tree_key in visited:
                return
            visited.add(tree_key)
            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_PostProcess_DiffuseSwitch' and not node.mute:
                    for group in node.groups:
                        collected.append((node, group))
                elif node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    blueprint_name = str(getattr(node, "blueprint_name", "") or "")
                    nested = bpy.data.node_groups.get(blueprint_name) if blueprint_name and blueprint_name != "NONE" else None
                    if nested and getattr(nested, "bl_idname", "") == "SSMTBlueprintTreeType":
                        collect(nested)

        collect(tree)
        return collected

    def _collect_custom_material_switch_groups(self):
        """Collect enabled switch groups from custom material-assign nodes."""
        tree = getattr(self, "id_data", None)
        if tree is None:
            return []
        collected = []
        visited = set()

        def collect(current_tree):
            tree_key = getattr(current_tree, "name", str(id(current_tree)))
            if tree_key in visited:
                return
            visited.add(tree_key)
            for node in current_tree.nodes:
                if node.bl_idname == 'SSMTNode_PostProcess_CustomMaterialAssign' and not node.mute:
                    if bool(getattr(node, "use_global_assign", False)):
                        for group in node.global_switch_groups:
                            if not bool(getattr(group, "enabled", True)):
                                continue
                            variable = str(getattr(group, "switch_variable", "") or "").strip()
                            if not variable:
                                continue
                            object_name = str(getattr(group, "object_name", "") or "")
                            target_object = bpy.data.objects.get(object_name) if object_name else None
                            collected.append((node, target_object, group))
                    else:
                        for item in node.target_items:
                            target_object = item.target_object
                            for group in item.switch_groups:
                                if not bool(getattr(group, "enabled", True)):
                                    continue
                                variable = str(getattr(group, "switch_variable", "") or "").strip()
                                if not variable:
                                    continue
                                collected.append((node, target_object, group))
                elif node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                    blueprint_name = str(getattr(node, "blueprint_name", "") or "")
                    nested = bpy.data.node_groups.get(blueprint_name) if blueprint_name and blueprint_name != "NONE" else None
                    if nested and getattr(nested, "bl_idname", "") == "SSMTBlueprintTreeType":
                        collect(nested)

        collect(tree)
        return collected

    @staticmethod
    def _entry_variable_names(entry):
        """返回条目记录的变量列表（``variables`` 为空时回退到 ``variable_name``）。"""
        names = [
            part.strip()
            for part in str(getattr(entry, "variables", "") or "").split("|")
            if part.strip()
        ]
        if not names:
            first = str(getattr(entry, "variable_name", "") or "").strip()
            if first:
                names = [first]
        return names

    @classmethod
    def _append_entry_variable(cls, entry, variable_name):
        """把一个变量并入已有条目（``|`` 连接并去重）。"""
        names = cls._entry_variable_names(entry)
        if variable_name and variable_name not in names:
            names.append(variable_name)
        entry.variables = "|".join(names)
        if not str(getattr(entry, "variable_name", "") or "").strip() and names:
            entry.variable_name = names[0]

    def _find_entry_index(self, variable_name, comment, allow_variable_match=False):
        """查找可合并的已有条目；找不到返回 -1。

        合并规则：备注非空时按备注合并（不同变量也合并）；备注为空时按变量名合并，
        且空备注条目只与空备注条目合并。贴图切换 / 自定义材质切换 / INI 分支额外保留
        原有「同变量合并」行为（``allow_variable_match=True``）。
        """
        comment = str(comment or "").strip()
        variable_name = str(variable_name or "").strip()
        if not variable_name:
            return -1
        for index, entry in enumerate(self.swap_panel_entries):
            entry_comment = str(getattr(entry, "comment", "") or "").strip()
            names = self._entry_variable_names(entry)
            if allow_variable_match and variable_name in names:
                return index
            if comment:
                if entry_comment == comment:
                    return index
            elif not entry_comment and variable_name in names:
                return index
        return -1

    def _refresh_entries(self):
        """刷新节点 UI 中的物体切换列表：蓝图节点 + 可选 INI 配置。"""
        self._ensure_namespace()
        self.swap_panel_entries.clear()

        # 1. 蓝图物体切换节点：备注相同即合并（变量名不同也合并）
        nodes = self._collect_swap_nodes()
        idx_map = {}
        for node in nodes:
            if not VARIABLE_REGISTRY_AVAILABLE:
                break
            var = str(get_node_variable_name(node) or "").strip()
            if not var:
                continue
            comment = str(getattr(node, "comment", "") or "")
            option_count = max(1, int(getattr(node, "input_slot_count", 2) or 2))
            hotkey = str(getattr(node, "hotkey", "") or "")
            # 备注相同（或备注为空且变量相同）的节点合并成一条，避免面板重复列出同一切换。
            existing = self._find_entry_index(var, comment)
            if existing >= 0:
                entry = self.swap_panel_entries[existing]
                self._append_entry_variable(entry, var)
                entry.option_count = max(entry.option_count, option_count)
                if not entry.hotkey:
                    entry.hotkey = hotkey
                idx_map[var] = existing
                continue
            entry = self.swap_panel_entries.add()
            entry.variable_name = var
            entry.variables = var
            entry.comment = comment
            entry.option_count = option_count
            entry.hotkey = hotkey
            entry.node_name = node.name
            idx_map[var] = len(self.swap_panel_entries) - 1

        # 2. 蓝图贴图切换节点：每个组对应一个多状态切换功能。
        for node, group in self._collect_diffuse_groups():
            var = str(diffuse_group_variable_name(group) or "").strip()
            if not var:
                continue
            comment = str(getattr(group, "comment", "") or getattr(group, "name", "") or "").strip()
            option_count = diffuse_group_option_count(group)
            hotkey = diffuse_group_hotkey(group)
            existing = self._find_entry_index(var, comment, allow_variable_match=True)
            if existing >= 0:
                entry = self.swap_panel_entries[existing]
                self._append_entry_variable(entry, var)
                if comment and comment not in entry.comment.split(" / "):
                    entry.comment = " / ".join(part for part in (entry.comment, comment) if part)
                entry.option_count = max(entry.option_count, option_count)
                if not entry.hotkey:
                    entry.hotkey = hotkey
                idx_map[var] = existing
                continue
            entry = self.swap_panel_entries.add()
            entry.variable_name = var
            entry.variables = var
            entry.comment = comment
            entry.option_count = option_count
            entry.hotkey = hotkey
            entry.node_name = f"{node.name}:{getattr(group, 'name', '')}"
            idx_map[var] = len(self.swap_panel_entries) - 1

        # 2b. 自定义材质指定节点的贴图切换组
        for node, target_object, group in self._collect_custom_material_switch_groups():
            var = str(getattr(group, "switch_variable", "") or "").strip()
            if not var:
                continue
            comment = str(getattr(group, "comment", "") or "").strip()
            option_count = max(2, int(getattr(group, "state_count", 2) or 2))
            hotkey = str(getattr(group, "key", "") or "").strip()
            existing = self._find_entry_index(var, comment, allow_variable_match=True)
            if existing >= 0:
                entry = self.swap_panel_entries[existing]
                self._append_entry_variable(entry, var)
                if comment and comment not in entry.comment.split(" / "):
                    entry.comment = " / ".join(part for part in (entry.comment, comment) if part)
                entry.option_count = max(entry.option_count, option_count)
                if not entry.hotkey:
                    entry.hotkey = hotkey
                idx_map[var] = existing
                continue
            entry = self.swap_panel_entries.add()
            entry.variable_name = var
            entry.variables = var
            entry.comment = comment
            entry.option_count = option_count
            entry.hotkey = hotkey
            part_name = getattr(target_object, "name", "") if target_object is not None else ""
            entry.node_name = f"{node.name}:{part_name or 'part'}:{var}"
            idx_map[var] = len(self.swap_panel_entries) - 1

        # 3. 可选：从 INI 读取 [KeySwap_*] 配置并合并（覆盖备注/选项数/按键）
        ini_path = (self.ini_file_path or "").strip()
        if ini_path and os.path.isfile(ini_path):
            ini_swaps = self._parse_ini_key_swaps(ini_path)
            for s in ini_swaps:
                var = str(s["var_name"] or "").strip()
                if not var:
                    continue
                comment = str(s["comment"] or "").strip()
                existing = self._find_entry_index(var, comment, allow_variable_match=True)
                if existing >= 0:
                    entry = self.swap_panel_entries[existing]
                    self._append_entry_variable(entry, var)
                    if comment:
                        entry.comment = s["comment"]
                    entry.option_count = s["option_count"]
                    if s["key"]:
                        entry.hotkey = s["key"]
                    idx_map[var] = existing
                else:
                    entry = self.swap_panel_entries.add()
                    entry.variable_name = var
                    entry.variables = var
                    entry.comment = s["comment"]
                    entry.option_count = s["option_count"]
                    entry.hotkey = s["key"]
                    entry.node_name = ""
                    idx_map[var] = len(self.swap_panel_entries) - 1
        self._refresh_button_image_entries()
        return len(self.swap_panel_entries)

    def _refresh_button_image_entries(self):
        """Build one image input per generated control button.

        Controls with the same non-empty hotkey share one image input, just as
        they share one runtime button. Existing paths are retained on refresh.
        """
        old_images = {}
        for entry in self.swap_panel_button_entries:
            key = self._normalize_hotkey(entry.hotkey)
            if key:
                old_images[key] = entry.image_path
            elif entry.variables:
                for var_name in entry.variables.split("|"):
                    old_images[f"var:{var_name}"] = entry.image_path

        groups = OrderedDict()
        for index, entry in enumerate(self.swap_panel_entries):
            hotkey = self._normalize_hotkey(entry.hotkey)
            group_key = hotkey if hotkey else f"__unbound_{index}"
            group = groups.get(group_key)
            if group is None:
                group = {
                    "hotkey": entry.hotkey,
                    "variables": [],
                    "labels": [],
                }
                groups[group_key] = group
            var_name = (entry.variable_name or "").strip()
            if var_name:
                group["variables"].append(var_name)
            label = (entry.comment or "").strip() or var_name or "(无备注)"
            if label not in group["labels"]:
                group["labels"].append(label)

        self.swap_panel_button_entries.clear()
        for group_key, group in groups.items():
            button_entry = self.swap_panel_button_entries.add()
            button_entry.hotkey = group["hotkey"]
            button_entry.variables = "|".join(group["variables"])
            button_entry.label = " / ".join(group["labels"])
            if group_key.startswith("__unbound_"):
                button_entry.image_path = next(
                    (old_images.get(f"var:{var_name}", "") for var_name in group["variables"] if old_images.get(f"var:{var_name}", "")),
                    "",
                )
            else:
                button_entry.image_path = old_images.get(group_key, "")

    def _button_image_for_source(self, hotkey, var_name):
        """Return the custom image configured for a generated control."""
        normalized = self._normalize_hotkey(hotkey)
        for entry in self.swap_panel_button_entries:
            if normalized and self._normalize_hotkey(entry.hotkey) == normalized:
                return (entry.image_path or "").strip()
            if not normalized and var_name in (entry.variables or "").split("|"):
                return (entry.image_path or "").strip()
        return ""

    # ==========================================
    # INI 解析
    # ==========================================
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
                stripped_line = line.strip()
                if stripped_line.startswith('[') and stripped_line.endswith(']') and len(stripped_line) > 2:
                    current_section = stripped_line
                    # EFMI 会追加同名 Constants/Present 段，按原顺序合并保留。
                    current_section = next(
                        (key for key in sections if key.casefold() == current_section.casefold()),
                        current_section,
                    )
                    sections.setdefault(current_section, [])
                elif current_section is not None:
                    sections[current_section].append(line.rstrip())
        except FileNotFoundError:
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
                    if section_name.startswith(';;'):
                        f.write(section_name + '\n')
                    else:
                        f.write(section_name + '\n')
                    for line in lines:
                        f.write(line + '\n')
                    f.write('\n')
                if preserved_tail_content:
                    f.write('\n' + preserved_tail_content)
        except Exception as e:
            print(f"写入INI文件失败: {e}")

    def _parse_ini_key_swaps(self, ini_path):
        """Parse numeric object swaps and named V5.1 diffuse KeySwap sections."""
        result = self._read_ini_to_ordered_dict(ini_path)
        if result is None or not result[0]:
            return []
        sections, _, _ = result

        swaps = []
        for order, (section_name, lines) in enumerate(sections.items()):
            numeric_match = re.match(r'^\[KeySwap_(\d+)\]$', section_name.strip(), re.IGNORECASE)
            named_match = re.match(r'^\[KeySwap[^\]]+\]$', section_name.strip(), re.IGNORECASE)
            if not named_match:
                continue
            index = int(numeric_match.group(1)) if numeric_match else 1000000 + order
            entry = {"index": index, "var_name": "", "comment": "", "key": "", "option_count": 2}
            for line in lines:
                s = line.strip()
                if not s:
                    continue
                if s.startswith(';'):
                    if not entry["comment"]:
                        entry["comment"] = s.lstrip(';').strip()
                    continue
                m2 = re.match(r'key\s*=\s*(.+)', s, re.IGNORECASE)
                if m2:
                    entry["key"] = m2.group(1).strip()
                    continue
                m3 = re.match(r'^(\$[A-Za-z0-9_]+)\s*=\s*(.*)$', s)
                if m3:
                    entry["var_name"] = m3.group(1)
                    vals = [v for v in m3.group(2).split(',') if v.strip() != '']
                    if vals:
                        entry["option_count"] = len(vals)
            if entry["var_name"]:
                swaps.append(entry)

        swaps.sort(key=lambda e: e["index"])
        return swaps

    # ==========================================
    # 导出
    # ==========================================
    def _build_button_list(self, target_ini_file):
        """构建按钮数据列表。

        INI configuration takes priority, then blueprint nodes fill in remarks
        and any switch functions that have not been written yet.
        """
        buttons = []
        by_variable = {}

        def button_var_names(button):
            names = [
                str(name).strip()
                for name in (button.get("var_names") or [])
                if str(name).strip()
            ]
            if not names:
                first = str(button.get("var_name") or "").strip()
                if first:
                    names = [first]
            return names

        def append_button_variable(button, var_name, option_count):
            """把变量与选项数并入按钮（变量去重，两个列表保持一一对应）。"""
            names = button_var_names(button)
            counts = [max(1, int(count or 2)) for count in (button.get("option_counts") or [])]
            while len(counts) < len(names):
                counts.append(max(1, int(button.get("option_count", 2) or 2)))
            if var_name in names:
                index = names.index(var_name)
                counts[index] = max(counts[index], option_count)
            else:
                names.append(var_name)
                counts.append(option_count)
            button["var_names"] = names
            button["option_counts"] = counts
            button["var_name"] = names[0]
            button["option_count"] = max(
                int(button.get("option_count", 2) or 2), option_count
            )
            for name in names:
                by_variable[name] = button

        def add_button(item):
            var_name = str(item.get("var_name") or "").strip()
            if not var_name:
                return
            comment = str(item.get("comment") or "").strip()
            option_count = max(1, int(item.get("option_count", 2) or 2))
            # 1) 该变量已属于某个按钮：合并备注 / 按键 / 选项数。
            existing = by_variable.get(var_name)
            if existing is not None:
                old_comment = str(existing.get("comment") or "").strip()
                if comment and comment not in old_comment.split(" / "):
                    existing["comment"] = " / ".join(part for part in (old_comment, comment) if part)
                if not existing.get("hotkey") and item.get("hotkey"):
                    existing["hotkey"] = item["hotkey"]
                append_button_variable(existing, var_name, option_count)
                return
            # 2) 备注非空且已有同备注按钮：把变量追加进那个按钮（一个按钮切换全部同名变量）。
            if comment:
                for button in buttons:
                    old_comment = str(button.get("comment") or "").strip()
                    if comment in [part.strip() for part in old_comment.split(" / ")]:
                        if not button.get("hotkey") and item.get("hotkey"):
                            button["hotkey"] = item["hotkey"]
                        append_button_variable(button, var_name, option_count)
                        return
            # 3) 新建按钮。
            new_button = dict(item)
            names = button_var_names(item) or [var_name]
            counts = [max(1, int(count or 2)) for count in (item.get("option_counts") or [])]
            while len(counts) < len(names):
                counts.append(option_count)
            new_button["var_names"] = names
            new_button["option_counts"] = counts[:len(names)]
            buttons.append(new_button)
            by_variable[var_name] = new_button

        # 1. Explicit INI takes priority; otherwise inspect the generated INI.
        explicit = (self.ini_file_path or "").strip()
        ini_swaps = []
        if explicit and os.path.isfile(explicit):
            ini_swaps = self._parse_ini_key_swaps(explicit)
        if not ini_swaps:
            ini_swaps = self._parse_ini_key_swaps(target_ini_file)
        for swap in ini_swaps:
            add_button({
                "var_name": swap["var_name"],
                "comment": swap["comment"],
                "option_count": swap["option_count"],
                "hotkey": swap["key"],
                "image_path": self._button_image_for_source(swap["key"], swap["var_name"]),
            })

        # 2. Fill from blueprint object switches.
        for node in self._collect_swap_nodes():
            if not VARIABLE_REGISTRY_AVAILABLE:
                continue
            var = get_node_variable_name(node)
            if not var:
                continue
            add_button({
                "var_name": var,
                "comment": str(getattr(node, "comment", "") or ""),
                "option_count": max(1, int(getattr(node, "input_slot_count", 2) or 2)),
                "hotkey": str(getattr(node, "hotkey", "") or ""),
                "image_path": self._button_image_for_source(
                    str(getattr(node, "hotkey", "") or ""), var
                ),
            })

        # 3. Fill from every V5.1 diffuse group in this blueprint.
        for _node, group in self._collect_diffuse_groups():
            var = diffuse_group_variable_name(group)
            hotkey = diffuse_group_hotkey(group)
            add_button({
                "var_name": var,
                "comment": str(getattr(group, "comment", "") or getattr(group, "name", "") or ""),
                "option_count": diffuse_group_option_count(group),
                "hotkey": hotkey,
                "image_path": self._button_image_for_source(hotkey, var),
            })
        # 4. Fill from every custom material-assign switch group in this blueprint.
        for _node, _target_object, group in self._collect_custom_material_switch_groups():
            var = str(getattr(group, "switch_variable", "") or "").strip()
            if not var:
                continue
            hotkey = str(getattr(group, "key", "") or "").strip()
            add_button({
                "var_name": var,
                "comment": str(getattr(group, "comment", "") or "").strip(),
                "option_count": max(2, int(getattr(group, "state_count", 2) or 2)),
                "hotkey": hotkey,
                "image_path": self._button_image_for_source(hotkey, var),
            })
        return self._merge_buttons_by_hotkey(buttons)

    @staticmethod
    def _normalize_hotkey(value):
        """Return a stable comparison key for an INI key binding."""
        tokens = str(value or "").strip().lower().split()
        if tokens and tokens[0] == "no_modifiers":
            tokens = tokens[1:]
        return " ".join(token[3:] if token.startswith("vk_") else token for token in tokens)

    @classmethod
    def _merge_buttons_by_hotkey(cls, buttons):
        """Merge controls sharing the same non-empty hotkey.

        A merged button keeps all variables and option counts so its command
        list can cycle every KeySwap that the original key would trigger.
        Empty bindings stay separate because they are not an actual shared key.
        """
        merged = []
        by_hotkey = {}
        for source_index, button in enumerate(buttons):
            # 尊重已合并的多变量按钮：有 var_names 就不再退化成单个 var_name。
            var_names = [
                str(name).strip()
                for name in (button.get("var_names") or [])
                if str(name).strip()
            ]
            if not var_names:
                first = str(button.get("var_name") or "").strip()
                if not first:
                    continue
                var_names = [first]
            default_count = max(1, int(button.get("option_count", 2) or 2))
            option_counts = [
                max(1, int(count or 2))
                for count in (button.get("option_counts") or [])
            ]
            while len(option_counts) < len(var_names):
                option_counts.append(default_count)
            option_counts = option_counts[:len(var_names)]
            hotkey = cls._normalize_hotkey(button.get("hotkey"))
            group_key = hotkey if hotkey else f"__unbound_{source_index}"
            group_index = by_hotkey.get(group_key)
            if group_index is None:
                item = dict(button)
                item["var_names"] = list(var_names)
                item["option_counts"] = list(option_counts)
                item["image_paths"] = [str(button.get("image_path") or "").strip()]
                item["comments"] = []
                if str(button.get("comment") or "").strip():
                    item["comments"].append(str(button["comment"]).strip())
                merged.append(item)
                if hotkey:
                    by_hotkey[group_key] = len(merged) - 1
                continue

            item = merged[group_index]
            for var_name, option_count in zip(var_names, option_counts):
                if var_name in item["var_names"]:
                    index = item["var_names"].index(var_name)
                    item["option_counts"][index] = max(item["option_counts"][index], option_count)
                    continue
                item["var_names"].append(var_name)
                item["option_counts"].append(option_count)
            item["image_paths"].append(str(button.get("image_path") or "").strip())
            comment = str(button.get("comment") or "").strip()
            if comment and comment not in item["comments"]:
                item["comments"].append(comment)
            item["comment"] = " / ".join(item["comments"])
        for item in merged:
            item["comment"] = " / ".join(item.pop("comments", []))
            item["image_path"] = next((path for path in item.pop("image_paths", []) if path), "")
        return merged

    @staticmethod
    def _cycle_command_lines(var, option_count):
        """生成循环切换 $var 的 CommandList 行（等价于按下 KeySwap 的 cycle 按键）。"""
        n = max(2, int(option_count or 2))
        lines = []
        for v in range(n - 1):
            prefix = "if" if v == 0 else "else if"
            lines.append(f"{prefix} {var} == {v}")
            lines.append(f"    {var} = {v + 1}")
        lines.append("else")
        lines.append(f"    {var} = 0")
        lines.append("endif")
        return lines

    def _copy_default_image(self, std_name, dest_res_dir, source_asset_dir):
        default_path = os.path.join(source_asset_dir, std_name)
        dest_path = os.path.join(dest_res_dir, std_name)
        if os.path.exists(default_path):
            if not os.path.exists(dest_path):
                shutil.copy2(default_path, dest_path)
                print(f"已复制默认图片: {std_name}")
        else:
            print(f"警告: 默认图片不存在 {default_path}")

    def _apply_button_border_image(self, dest_path):
        """Alpha-composite the configured border over one final button image."""
        border_path = (self.button_border_image or "").strip()
        if not border_path or not os.path.isfile(border_path):
            return dest_path
        if not PIL_AVAILABLE or not dest_path or not os.path.isfile(dest_path):
            print("[物体切换面板] 无法叠加按钮边框：Pillow不可用或按钮图片不存在")
            return dest_path
        try:
            with PILImage.open(dest_path) as base_source:
                base = base_source.convert('RGBA')
            with PILImage.open(border_path) as border_source:
                border = border_source.convert('RGBA')
            if border.size != base.size:
                resampling = getattr(PILImage, "Resampling", PILImage)
                border = border.resize(base.size, resampling.LANCZOS)
            PILImage.alpha_composite(base, border).save(dest_path)
        except Exception as e:
            print(f"[物体切换面板] 叠加按钮边框失败: {e}")
        return dest_path

    def _generate_text_icon(self, text, dest_path, font_size=36, font_family="msyh.ttc",
                            text_color=(1.0, 1.0, 1.0), stroke_width=2, stroke_color=(0.0, 0.0, 0.0),
                            bg_color=(0.16, 0.22, 0.32), border_color=(0.59, 0.75, 0.94),
                            border_width=2, opacity=0.9):
        """根据备注文本生成按钮图标（圆角按钮背景 + 居中文字）。"""
        try:
            if not PIL_AVAILABLE:
                return None
            text = text.replace('/', '\n').strip()
            if not text:
                return None

            def float_to_int_rgb(vals):
                # 用 round 而不是 int：0.16/0.22/0.32 才能还原成旧硬编码的 (41, 56, 82)
                return tuple(int(round(val * 255)) for val in vals)

            text_rgb = float_to_int_rgb(text_color)
            stroke_rgb = float_to_int_rgb(stroke_color)

            font = None
            try:
                font = PILImageFont.truetype(font_family, font_size)
            except Exception:
                for f in ["msyh.ttc", "simsun.ttc", "simhei.ttf", "arial.ttf"]:
                    try:
                        font = PILImageFont.truetype(f, font_size)
                        break
                    except Exception:
                        continue
            if font is None:
                font = PILImageFont.load_default()

            temp_img = PILImage.new('RGBA', (1, 1), (0, 0, 0, 0))
            temp_draw = PILDraw.Draw(temp_img)
            bbox = temp_draw.multiline_textbbox((0, 0), text, font=font, align='center', spacing=4)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            pad_x = 18 + (stroke_width * 2)
            pad_y = 10 + (stroke_width * 2)
            img_w = math.ceil(text_w + pad_x * 2)
            img_h = math.ceil(text_h + pad_y * 2)

            bg_rgb = float_to_int_rgb(bg_color)
            bd_rgb = float_to_int_rgb(border_color)
            btn_alpha = int(255 * max(0.0, min(1.0, opacity)))

            img = PILImage.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
            draw = PILDraw.Draw(img)
            radius = min(14, img_h // 3)
            try:
                draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius,
                                       fill=bg_rgb + (btn_alpha,),
                                       outline=bd_rgb + (btn_alpha,), width=border_width)
            except Exception:
                draw.rectangle([0, 0, img_w - 1, img_h - 1], fill=bg_rgb + (btn_alpha,),
                               outline=bd_rgb + (btn_alpha,))

            x = (img_w - text_w) / 2 - bbox[0]
            y = (img_h - text_h) / 2 - bbox[1]
            draw.multiline_text((x, y), text, font=font, fill=text_rgb + (255,),
                                align='center', spacing=4,
                                stroke_width=stroke_width, stroke_fill=stroke_rgb + (255,))
            img.save(dest_path)
            return dest_path
        except Exception as e:
            print(f"[物体切换面板] 生成文字图标失败: {e}")
            return None

    def _ensure_button_image(self, dest_res_dir, ns, i, source_asset_dir, button, style_src=None):
        """生成第 i 个按钮的图标图片，返回路径。

        优先级：
          1. 该按钮单独设置的图片（按钮列表里的 image_path）
          2. 全局回退图片（button_image）
          3. 用备注文字自动生成的图标（use_remark_as_icon 且备注非空，需要 Pillow）
          4. 纯色默认按钮图（仍会叠加按钮边框图片）

        style_src：按钮回退图片与文字按钮样式的来源对象。附加模式下传入形态键滑块面板
        节点，让附加按钮与该面板保持一致（本节点对应设置项在附加模式下已隐藏）；
        缺省（None）时使用本节点自身的设置。
        """
        src = style_src if style_src is not None else self

        def _style(name):
            return getattr(src, name, getattr(self, name))

        dest_name = f"swpbtn_{ns}_{i}.png"
        dest_path = os.path.join(dest_res_dir, dest_name)

        custom = (button.get("image_path") or "").strip()
        if not custom:
            custom = (_style("button_image") or "").strip()
        if custom and os.path.isfile(custom):
            shutil.copy2(custom, dest_path)
            return self._apply_button_border_image(dest_path)

        # 备注文字图标（未单独指定图片时按备注自动生成）
        comment = str(button.get("comment") or "").strip()
        if self.use_remark_as_icon and comment:
            generated = self._generate_text_icon(
                comment,
                dest_path,
                font_size=self.remark_font_size,
                font_family=self.remark_font_family,
                text_color=self.remark_text_color,
                stroke_width=self.remark_stroke_width,
                stroke_color=self.remark_stroke_color,
                bg_color=_style("button_bg_color"),
                border_color=_style("button_border_color"),
                border_width=_style("button_border_width"),
                opacity=_style("button_opacity"),
            )
            if generated:
                return self._apply_button_border_image(generated)

        # 用按钮样式生成默认按钮图（没有图片、也没有备注图标时）
        if PIL_AVAILABLE:
            try:
                def _to_rgb(vals):
                    return tuple(int(round(val * 255)) for val in vals)

                bg_rgb = _to_rgb(_style("button_bg_color"))
                bd_rgb = _to_rgb(_style("button_border_color"))
                btn_alpha = int(255 * max(0.0, min(1.0, _style("button_opacity"))))
                img = PILImage.new('RGBA', (384, 64), (0, 0, 0, 0))
                draw = PILDraw.Draw(img)
                try:
                    draw.rounded_rectangle([0, 0, 383, 63], radius=12,
                                           fill=bg_rgb + (btn_alpha,),
                                           outline=bd_rgb + (btn_alpha,),
                                           width=_style("button_border_width"))
                except Exception:
                    draw.rectangle([0, 0, 383, 63], fill=bg_rgb + (btn_alpha,),
                                   outline=bd_rgb + (btn_alpha,))
                img.save(dest_path)
                return self._apply_button_border_image(dest_path)
            except Exception as e:
                print(f"生成默认按钮图片失败: {e}")

        fallback = os.path.join(source_asset_dir, "0.png")
        if os.path.exists(fallback):
            shutil.copy2(fallback, dest_path)
            return self._apply_button_border_image(dest_path)
        return None

    def _generate_background_image(self, dest_path, panel_w, panel_h, use_existing=False):
        """生成/处理面板背景图（圆角 + 边框）。panel_w/panel_h 为面板在屏幕单位的宽高。

        use_existing=False：生成纯色圆角背景（默认 512 高）。
        use_existing=True：基于 dest_path 现有图（自定义背景）应用圆角遮罩 + 边框。
        """
        try:
            if not PIL_AVAILABLE:
                return None

            def float_to_int_rgb(vals):
                return tuple(int(round(val * 255)) for val in vals)

            if use_existing and os.path.exists(dest_path):
                # 基于自定义背景图应用圆角/边框
                with PILImage.open(dest_path) as src:
                    img = src.convert('RGBA')
            else:
                # 生成纯色底图（按面板在 16:9 屏幕上的显示比例，保证圆角不变形）
                img_h = 512
                pixel_ratio = (panel_w / panel_h) * (1920.0 / 1080.0) if panel_h > 0 else 1.0
                img_w = max(64, int(round(img_h * pixel_ratio)))
                fill_rgb = float_to_int_rgb((0.05, 0.08, 0.12))
                img = PILImage.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
                d = PILDraw.Draw(img)
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
                img = PILImage.merge('RGBA', (r, g, b, a))

            # 圆角 alpha 遮罩
            mask = PILImage.new('L', (img_w, img_h), 0)
            md = PILDraw.Draw(mask)
            if radius_px > 0:
                md.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=radius_px, fill=255)
            else:
                md.rectangle([0, 0, img_w - 1, img_h - 1], fill=255)
            r, g, b, a = img.split()
            a = PILImage.composite(a, PILImage.new('L', (img_w, img_h), 0), mask)
            img = PILImage.merge('RGBA', (r, g, b, a))

            # 边框（在圆角区域内）
            if border_px > 0:
                draw = PILDraw.Draw(img)
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
            print(f"[物体切换面板] 生成背景图失败: {e}")
            return None

    @staticmethod
    def _compute_grid_left(panel_bg_width, grid_width, side_padding, align):
        """按钮组在面板内的水平起点。

        - LEFT  ：左对齐，留 side_padding 边距
        - CENTER：居中（与旧版硬编码的居中公式完全一致）
        - RIGHT ：右对齐，留 side_padding 边距
        """
        if str(align or '').upper() == 'RIGHT':
            return panel_bg_width - side_padding - grid_width
        if str(align or '').upper() == 'LEFT':
            return side_padding
        return (panel_bg_width - grid_width) * 0.5

    def _is_velo_efmi_export(self, _in_place=False):
        """本次后处理是否运行在 Velo / EFMI-Tools 后端上。

        Velo 桥接是独立的输出节点，导出期间由它显式写入运行时标记；这里只读那个标记，
        不做 ini 文本嗅探——TheHerta4 自身的 EFMI 导出同样会写 ``\\EFMIv1\\`` 命名空间，
        用文本特征判断会把原生 EFMI 导出误判成 Velo 导出（进而跳过本面板自己的检测段）。

        「原地刷新」没有导出上下文，回落到蓝图树 / 桥接节点上的持久标记。
        """
        try:
            from .export_helper import BlueprintExportHelper
        except Exception:
            return False
        try:
            game = BlueprintExportHelper.get_velo_bridge_game(
                tree=getattr(self, "id_data", None),
                use_blueprint_marker=bool(_in_place),
            )
        except Exception:
            return False
        return game == 'ENDFIELD'

    # ==========================================
    # 附加模式：把切换按钮附加到形态键滑块面板左侧
    # ==========================================
    def _generate_attached_panel(self, mod_export_path, target_ini_file, buttons, _in_place=False):
        """附加模式：不生成独立浮动面板，把切换按钮附加到滑块面板的左侧。

        - 复用滑块面板的坐标系与交互状态：`$img0_x`/`$img0_y`/`$zoom0`/`$help`/
          `$ui_active`/`$mouse_clicked`/`$is_dragging`/`$click_outside`。
        - 复用滑块面板的 `[CustomShaderDraw]`（无命名空间），无需单独准备 shader。
        - 不生成：背景图、独立键位段落、独立检测段、独立 shader 段。
        - 保留：按钮资源段落、循环切换 CommandList、按钮点击逻辑、GUI 全局模式守卫。

        若原有独立面板配置存在（从独立模式切换过来），会先按本节点命名空间清理掉。
        """
        # 附加模式下，按钮的回退图片与文字按钮样式跟随形态键滑块面板节点
        # （本节点里这几项设置已在 UI 上隐藏，避免出现「隐藏但生成时仍读本节点值」的无效隐藏）
        slider_style_src = self._find_slider_panel_node()

        ns = self._ensure_namespace()
        num_buttons = len(buttons)
        btn_h = self.button_height
        SCREEN_RATIO_CORRECTION = 0.5625

        # ---- 1. 准备按钮资源（附加模式不需要独立背景图）----
        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            source_asset_dir = os.path.join(addon_dir, "Toolset")
            dest_res_dir = os.path.join(mod_export_path, "res")
            os.makedirs(dest_res_dir, exist_ok=True)

            # 确保 draw_2d.hlsl 存在（滑块面板也会复制，两处都做去重即可）
            shader_src = os.path.join(source_asset_dir, "draw_2d.hlsl")
            shader_dst = os.path.join(dest_res_dir, "draw_2d.hlsl")
            if os.path.exists(shader_src) and not os.path.exists(shader_dst):
                shutil.copy2(shader_src, shader_dst)

            btn_w_list = []
            for i, b in enumerate(buttons, start=1):
                img_path = self._ensure_button_image(
                    dest_res_dir, ns, i, source_asset_dir, b, style_src=slider_style_src
                )
                btn_w_i = float(self.button_width or 0.0)
                if btn_w_i <= 0.0:
                    btn_w_i = btn_h * 3.0 * SCREEN_RATIO_CORRECTION  # 缺省按 3:1 比例
                    if img_path and PIL_AVAILABLE:
                        try:
                            with PILImage.open(img_path) as img:
                                w, h = img.size
                                if h > 0:
                                    btn_w_i = btn_h * (w / h) * SCREEN_RATIO_CORRECTION
                        except Exception:
                            pass
                btn_w_list.append(btn_w_i)
        except Exception as e:
            print(f"[物体切换面板] 附加模式准备按钮资源出错: {e}")
            return False

        # ---- 2. 读取目标 ini ----
        result = self._read_ini_to_ordered_dict(target_ini_file)
        if result is None or not result[0]:
            return False
        sections, preserved_tail_content, preserved_driver_content = result

        # ---- 3. 附加模式：总是清理本节点旧配置 ----
        # 这样无论之前是独立模式还是附加模式，都能被安全覆盖，不会留下孤立段落。
        self._remove_owned_config(sections, ns)
        self._remove_gui_only_guards(sections, ns)

        # ---- 4. Constants ----
        constants_additions = [self.BLOCK_BEGIN.format(ns=ns)]
        constants_additions.append("; --- Attached Swap Buttons (drawn to the left of the slider panel) ---")
        constants_additions.append(self.ATTACHED_FLAG)
        for i in range(1, num_buttons + 1):
            constants_additions.extend([
                f"global ${ns}_btn_w{i} = {btn_w_list[i-1]:.4f}",
                f"global ${ns}_btn_h{i} = {btn_h:.4f}",
                f"global ${ns}_btn_render_w{i}",
                f"global ${ns}_btn_render_h{i}",
                f"global ${ns}_btn_x{i}",
                f"global ${ns}_btn_y{i}",
                f"global ${ns}_btn_pressed{i} = 0",
            ])
        constants_additions.append(f"; @@{self.PANEL_TAG}:gui_only@@")
        constants_additions.append(f"global ${ns}_gui_only = {1 if self.gui_only else 0}")
        for b in buttons:
            for var_name in (b.get("var_names") or [b["var_name"]]):
                var_line = f"global persist {var_name} = 0"
                if var_line not in constants_additions:
                    constants_additions.append(var_line)
        constants_additions.append(self.BLOCK_END.format(ns=ns))

        # ---- 5. 资源段落 / 循环切换 CommandList ----
        other_sections = OrderedDict()
        for i in range(1, num_buttons + 1):
            other_sections[f"[ResourceSwapButton{i}_{ns}]"] = [f"filename = ./res/swpbtn_{ns}_{i}.png"]

        for i, b in enumerate(buttons, start=1):
            command_lines = []
            var_names = b.get("var_names") or [b["var_name"]]
            option_counts = b.get("option_counts") or [b["option_count"]]
            for sub_index, (var_name, option_count) in enumerate(zip(var_names, option_counts), start=1):
                sub_name = f"CommandListSwap{i}_{sub_index}_{ns}"
                other_sections[f"[{sub_name}]"] = self._cycle_command_lines(var_name, option_count)
                command_lines.append(f"run = {sub_name}")
            other_sections[f"[CommandListSwap{i}_{ns}]"] = command_lines

        # 复用滑块面板的 [CustomShaderDraw]（无命名空间）；若滑块面板尚未写入，则先建好
        if '[CustomShaderDraw]' not in sections:
            sections['[CustomShaderDraw]'] = []
            shader_def = [
                "hs = null", "ds = null", "gs = null", "cs = null",
                "vs = ./res/draw_2d.hlsl", "ps = ./res/draw_2d.hlsl",
                "blend = ADD SRC_ALPHA INV_SRC_ALPHA", "cull = none",
                "topology = triangle_strip", "o0 = set_viewport bb", "Draw = 4,0", "clear = ps-t100"
            ]
            for line in shader_def:
                sections['[CustomShaderDraw]'].append(line)

        # ---- 6. Present 逻辑：复用滑块面板的可见性/位置/缩放/点击状态 ----
        present_additions = []
        present_additions.append(self.ATTACHED_PRESENT_MARKER)
        present_additions.append("")
        present_additions.append("; --- Attached swap buttons (rendered alongside the slider panel) ---")
        present_additions.append("; 复用滑块面板状态：$help / $ui_active / $img0_x / $img0_y / $zoom0 / $mouse_clicked / $is_dragging")
        present_additions.append("if $help == 1 && $ui_active == 1")

        gap = 0.02  # 按钮组与滑块面板之间的水平间隙

        # 位置计算：按钮从左往右排列在滑块面板左侧
        for i, _b in enumerate(buttons, start=1):
            # 每个按钮相对面板顶部的 y 偏移（屏幕比例；与滑块面板同缩放）
            y_offset_screen_units = self.button_top_padding + (i - 1) * (btn_h + self.button_row_spacing)
            present_additions.append(f"    ${ns}_btn_render_w{i} = ${ns}_btn_w{i} * $zoom0")
            present_additions.append(f"    ${ns}_btn_render_h{i} = ${ns}_btn_h{i} * $zoom0")
            # 按钮右边缘紧贴滑块面板左边缘（面板 x = $img0_x）
            present_additions.append(f"    ${ns}_btn_x{i} = $img0_x - {gap:.4f} - ${ns}_btn_render_w{i}")
            present_additions.append(f"    ${ns}_btn_y{i} = $img0_y + {y_offset_screen_units:.4f} * $zoom0")

        # 按钮按下/弹起检测：复用滑块面板的鼠标状态
        # 注意：滑块面板自身的 Present 块先执行时会因为鼠标落在面板外而设置 $click_outside = 1，
        # 这里在按钮命中时把它重置为 0，避免点击附加按钮导致面板被关闭。
        present_additions.append("    ; 附加按钮：按下检测（同时清掉滑块面板设置的 $click_outside）")
        present_additions.append("    if $mouse_clicked && $is_dragging == 0")
        for i in range(1, num_buttons + 1):
            present_additions.append(
                f"        if cursor_x > ${ns}_btn_x{i} && cursor_x < ${ns}_btn_x{i} + ${ns}_btn_render_w{i} "
                f"&& cursor_y > ${ns}_btn_y{i} && cursor_y < ${ns}_btn_y{i} + ${ns}_btn_render_h{i}"
            )
            present_additions.append(f"            ${ns}_btn_pressed{i} = 1")
            present_additions.append("            $click_outside = 0")
            present_additions.append("        endif")
        present_additions.append("    else")
        present_additions.append("        if $is_dragging == 0")
        for i in range(1, num_buttons + 1):
            present_additions.append(f"            if ${ns}_btn_pressed{i} == 1")
            present_additions.append(
                f"                if cursor_x > ${ns}_btn_x{i} && cursor_x < ${ns}_btn_x{i} + ${ns}_btn_render_w{i} "
                f"&& cursor_y > ${ns}_btn_y{i} && cursor_y < ${ns}_btn_y{i} + ${ns}_btn_render_h{i}"
            )
            present_additions.append(f"                    run = CommandListSwap{i}_{ns}")
            present_additions.append("                endif")
            present_additions.append(f"                ${ns}_btn_pressed{i} = 0")
            present_additions.append("            endif")
        present_additions.append("        endif")
        present_additions.append("    endif")

        # 渲染按钮
        for i, b in enumerate(buttons, start=1):
            present_additions.append(f"\n    ; 渲染附加切换按钮{i} ({b.get('var_name', '')})")
            present_additions.append(f"    ps-t100 = ResourceSwapButton{i}_{ns}")
            present_additions.append(f"    x87 = ${ns}_btn_render_w{i}")
            present_additions.append(f"    y87 = ${ns}_btn_render_h{i}")
            present_additions.append(f"    z87 = ${ns}_btn_x{i}")
            present_additions.append(f"    if ${ns}_btn_pressed{i} == 1")
            present_additions.append(f"        w87 = ${ns}_btn_y{i} + 0.002")
            active_vars = b.get("var_names") or [b["var_name"]]
            active_condition = " || ".join(f"{var_name} != 0" for var_name in active_vars)
            present_additions.append(f"    else if {active_condition}")
            present_additions.append(f"        w87 = ${ns}_btn_y{i} + 0.001")
            present_additions.append("    else")
            present_additions.append(f"        w87 = ${ns}_btn_y{i}")
            present_additions.append("    endif")
            present_additions.append("    run = CustomShaderDraw")

        present_additions.append("endif")

        # GUI 全局模式守卫（屏蔽原始 KeySwap 段落）
        self._apply_gui_only_guards(sections, ns, buttons)

        # ---- 7. 合并写入 ----
        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        for line in constants_additions:
            if line not in sections['[Constants]']:
                sections['[Constants]'].append(line)

        for sec_name, lines in other_sections.items():
            if sec_name not in sections:
                sections[sec_name] = []
            for line in lines:
                if line not in sections[sec_name]:
                    sections[sec_name].append(line)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        sections['[Present]'].append("")
        sections['[Present]'].append(self.ATTACHED_PRESENT_BEGIN.format(ns=ns))
        sections['[Present]'].extend(present_additions)
        sections['[Present]'].append(self.ATTACHED_PRESENT_END.format(ns=ns))

        try:
            with open(target_ini_file, 'w', encoding='utf-8') as f:
                f.write("")
            self._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
            print(f"物体切换面板(附加到形态键滑块面板左侧)已{'原地更新' if _in_place else '合并到'}: {os.path.basename(target_ini_file)}")
            print(f"共生成 {num_buttons} 个附加切换按钮")
            return True
        except Exception as e:
            print(f"写入INI文件失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def execute_postprocess(self, mod_export_path, _in_place=False, _ini_path=None):
        """生成 / 原地刷新物体切换面板配置。

        - 若蓝图中存在形态键滑块面板（启用 use_slider_panel），进入附加模式：把切换按钮
          附加到滑块面板左侧，复用其坐标系与交互状态，不生成独立浮动面板。
        - 否则走原有独立面板机制。
        - 导出时（_in_place=False）：在 mod 目录 ini 中追加面板配置（含专有标识注释）。
        - 刷新时（_in_place=True）：按专有标识移除本面板旧配置后原地重新生成，无需重新导出整个 mod。
        """
        if _ini_path:
            target_ini_file = _ini_path
        else:
            ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
            if not ini_files:
                return False
            target_ini_file = ini_files[0]

        print(f"物体切换面板{'原地刷新' if _in_place else '开始执行'}: {target_ini_file}")

        # 1. 收集按钮数据
        buttons = self._build_button_list(target_ini_file)
        if not buttons:
            print("未检测到任何物体切换节点 / [KeySwap_*] 配置，跳过面板生成")
            return False

        # 2. 检测形态键滑块面板：存在**且确实产出**时走附加模式（不生成独立浮动面板）
        slider_node = self._find_slider_panel_node()
        if slider_node is not None:
            # 附加模式复用的是滑块面板的「无命名空间」全局变量（$img0_x / $zoom0 / $help …），
            # 它们只在滑块面板真的写出时才有定义：节点勾了开关但没有可建滑块的形态键参数、
            # 或节点没被连进后处理链时，附加块会引用不存在的变量（3Dmigoto 侧读到 0）。
            if self._slider_panel_emitted(target_ini_file):
                print(f"[物体切换面板] 检测到形态键滑块面板节点 '{slider_node.name}'，切换按钮将附加到其左侧")
                # 附加模式总是替换旧配置，允许从独立模式平滑切换过来（不残留旧的独立面板段落）
                return self._generate_attached_panel(
                    mod_export_path, target_ini_file, buttons, _in_place=True
                )
            print(
                f"[物体切换面板] 节点 '{slider_node.name}' 未在本 ini 中产出滑块面板"
                f"（缺少可建滑块的形态键参数或节点未接入后处理链），改用独立浮动面板"
            )

        # ---- 以下为原有独立面板机制 ----
        # 防重复：仅导出时检查（刷新时按标识替换旧配置）
        if not _in_place:
            try:
                with open(target_ini_file, 'r', encoding='utf-8') as f:
                    if self.DUP_GUARD in f.read():
                        print("物体切换面板配置已存在于文件中。请手动删除后再生成，或用节点上的「应用设置到Mod」原地更新。")
                        return False
            except Exception:
                pass

        try:
            if self.create_cumulative_backup:
                self._create_cumulative_backup(target_ini_file, mod_export_path)
        except Exception as e:
            print(f"创建备份时出错: {e}")
            return False

        ns = self._ensure_namespace()
        self.last_mod_ini_path = target_ini_file

        num_buttons = len(buttons)
        buttons_per_row = max(1, min(int(self.buttons_per_row or 1), num_buttons))
        num_rows = max(1, int(math.ceil(num_buttons / float(buttons_per_row))))

        # 3. 复制资源（shader + 背景图 + 按钮图）
        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            source_asset_dir = os.path.join(addon_dir, "Toolset")
            dest_res_dir = os.path.join(mod_export_path, "res")
            os.makedirs(dest_res_dir, exist_ok=True)

            shader_src = os.path.join(source_asset_dir, "draw_2d.hlsl")
            shader_dst = os.path.join(dest_res_dir, "draw_2d.hlsl")
            if os.path.exists(shader_src) and not os.path.exists(shader_dst):
                shutil.copy2(shader_src, shader_dst)

            btn_h = self.button_height
            SCREEN_RATIO_CORRECTION = 0.5625  # 固定 16:9 宽屏修正系数

            bg_custom = (self.background_image or "").strip()
            bg_dest_path = os.path.join(dest_res_dir, f"swpbg_{ns}.png")
            if bg_custom and os.path.isfile(bg_custom):
                shutil.copy2(bg_custom, bg_dest_path)
            else:
                default_bg_src = os.path.join(source_asset_dir, "0.png")
                if os.path.exists(default_bg_src) and not os.path.exists(bg_dest_path):
                    shutil.copy2(default_bg_src, bg_dest_path)

            # 读取实际使用的背景图宽高（用户选择的背景图片优先；未选择时为内置默认图）
            # → 面板背景尺寸按「面板高度 × 所选背景图宽高比」自动计算
            bg_img_w = None
            bg_img_h = None
            if os.path.exists(bg_dest_path) and PIL_AVAILABLE:
                try:
                    with PILImage.open(bg_dest_path) as img:
                        _w, _h = img.size
                        if _w > 0 and _h > 0:
                            bg_img_w, bg_img_h = _w, _h
                except Exception:
                    pass
            bg_aspect = (bg_img_w / bg_img_h) if (bg_img_w and bg_img_h) else None

            # 生成每个按钮图标；宽度为 0 时按图片比例自动计算，否则使用统一宽度。
            btn_w_list = []
            max_btn_w = 0.0
            for i, b in enumerate(buttons, start=1):
                img_path = self._ensure_button_image(dest_res_dir, ns, i, source_asset_dir, b)
                btn_w_i = float(self.button_width or 0.0)
                if btn_w_i <= 0.0:
                    btn_w_i = btn_h * 3.0 * SCREEN_RATIO_CORRECTION  # 缺省按 3:1 比例
                    if img_path and PIL_AVAILABLE:
                        try:
                            with PILImage.open(img_path) as img:
                                w, h = img.size
                                if h > 0:
                                    btn_w_i = btn_h * (w / h) * SCREEN_RATIO_CORRECTION
                        except Exception:
                            pass
                btn_w_list.append(btn_w_i)
                if btn_w_i > max_btn_w:
                    max_btn_w = btn_w_i
            column_widths = [0.0 for _ in range(buttons_per_row)]
            for i, btn_w_i in enumerate(btn_w_list):
                column_widths[i % buttons_per_row] = max(column_widths[i % buttons_per_row], btn_w_i)
            grid_width = sum(column_widths) + max(0, (buttons_per_row - 1) * self.button_column_spacing)
        except Exception as e:
            print(f"准备和复制资源文件时出错: {e}")
            return False

        # 4. 读取目标 ini
        result = self._read_ini_to_ordered_dict(target_ini_file)
        if result is None or not result[0]:
            return False
        sections, preserved_tail_content, preserved_driver_content = result

        is_efmi = self._is_velo_efmi_export(_in_place)

        # 刷新模式：先按专有标识移除本面板旧配置，再原地重新生成
        if _in_place:
            self._remove_owned_config(sections, ns)
            if '[Present]' in sections and any(self.DUP_GUARD in l for l in sections['[Present]']):
                print("检测到旧格式的物体切换面板配置（无标识标记）。请先重新导出一次 mod 以生成带标识的新配置。")
                return False
        self._remove_gui_only_guards(sections, ns)

        # 5. 生成面板配置（面板背景宽度按「面板高度 × 背景图片宽高比」推导，保持背景比例）
        btn_h = self.button_height

        top_padding = self.button_top_padding
        bottom_padding = 0.03
        side_padding = 0.02
        total_button_height = num_rows * btn_h
        total_spacing_height = max(0, (num_rows - 1) * self.button_row_spacing)
        parent_height = total_button_height + total_spacing_height + top_padding + bottom_padding
        parent_height = max(parent_height, self.panel_min_height)

        if bg_aspect:
            derived_bg_width = parent_height * bg_aspect * SCREEN_RATIO_CORRECTION
            adjusted_panel_bg_width = max(derived_bg_width, grid_width + (side_padding * 2))
        else:
            adjusted_panel_bg_width = grid_width + (side_padding * 2)

        # 生成/处理面板背景（圆角 + 边框）：无论是否自定义背景图都生效
        bg_custom = (self.background_image or "").strip()
        self._generate_background_image(os.path.join(dest_res_dir, f"swpbg_{ns}.png"),
                                        adjusted_panel_bg_width, parent_height,
                                        use_existing=bool(bg_custom))

        help_key = self.help_key.strip() or "home"
        reset_key = self.reset_key.strip() or "ctrl home"
        zoom_in_key = self.zoom_in_key.strip() or "up"
        zoom_out_key = self.zoom_out_key.strip() or "down"
        drag_key = self.drag_key.strip() or "VK_LBUTTON"
        detect_hash = self.detect_hash.strip() or self.check_hash.strip()
        detect_index_count_val = self.detect_index_count.strip()
        if not detect_index_count_val and self.match_index_count > 0:
            detect_index_count_val = str(self.match_index_count)

        # 每个按钮固定行位置（相对父级高度）
        fixed_rel_y = []
        fixed_rel_x = []
        grid_left = self._compute_grid_left(
            adjusted_panel_bg_width, grid_width, side_padding, self.button_align
        )

        for i in range(num_buttons):
            row_index = i // buttons_per_row
            col_index = i % buttons_per_row
            offset_y = top_padding + row_index * (btn_h + self.button_row_spacing) + (btn_h / 2)
            fixed_rel_y.append(offset_y / parent_height)
            offset_x = grid_left + sum(column_widths[:col_index]) + (col_index * self.button_column_spacing) + ((column_widths[col_index] - btn_w_list[i]) / 2.0)
            fixed_rel_x.append(offset_x / adjusted_panel_bg_width)

        constants_additions = []
        present_additions = []
        other_sections = OrderedDict()

        constants_additions.extend([
            self.BLOCK_BEGIN.format(ns=ns),
            "; --- UI 几何与位置配置 (由物体切换面板生成) ---",
            f"; @@{self.PANEL_TAG}:panel_width@@",
            f"global ${ns}_base_width0 = {adjusted_panel_bg_width:.4f}",
            f"; @@{self.PANEL_TAG}:panel_height@@",
            f"global ${ns}_base_height0 = {parent_height:.4f}",
            f"global ${ns}_set_x0 = 0.5", f"global ${ns}_set_y0 = 0.5",
        ])
        for i in range(1, num_buttons + 1):
            constants_additions.extend([
                f"; @@{self.PANEL_TAG}:btn_width_{i}@@",
                f"global ${ns}_btn_width{i} = {btn_w_list[i-1]:.4f}",
                f"; @@{self.PANEL_TAG}:btn_height_{i}@@",
                f"global ${ns}_btn_height{i} = {btn_h:.4f}",
                f"global ${ns}_btn_render_width{i}",
                f"global ${ns}_btn_render_height{i}",
                f"; @@{self.PANEL_TAG}:fixed_rel_y_{i}@@",
                f"global ${ns}_fixed_rel_y{i} = {fixed_rel_y[i-1]:.4f}",
                f"; @@{self.PANEL_TAG}:fixed_rel_x_{i}@@",
                f"global ${ns}_fixed_rel_x{i} = {fixed_rel_x[i-1]:.4f}",
                f"global ${ns}_rel_y{i}",
                f"global ${ns}_btn_x{i}", f"global ${ns}_btn_y{i}", f"global ${ns}_btn_pressed{i} = 0",
            ])

        constants_additions.extend([
            f"global ${ns}_ui_active", f"global ${ns}_help",
            f"global ${ns}_max_zoom = 5.0", f"global ${ns}_min_zoom = 0.1",
            f"global ${ns}_mouse_clicked = 0",
            f"global ${ns}_click_outside = 0", f"global ${ns}_is_dragging = 0",
            f"global ${ns}_drag_x = 0", f"global ${ns}_drag_y = 0",
            f"global persist ${ns}_img0_x = 0", f"global persist ${ns}_img0_y = 0",
            f"; @@{self.PANEL_TAG}:zoom0@@",
            f"global persist ${ns}_zoom0 = {self.panel_default_scale:.2f}",
            f"global ${ns}_norm_width0", f"global ${ns}_norm_height0",
            f"global ${ns}_btn_click_processed = 0",
            f"; @@{self.PANEL_TAG}:gui_only@@",
            f"global ${ns}_gui_only = {1 if self.gui_only else 0}",
        ])
        # 确保每个切换变量已声明（若 ini 中已存在会自动去重）
        for b in buttons:
            for var_name in (b.get("var_names") or [b["var_name"]]):
                var_line = f"global persist {var_name} = 0"
                if var_line not in constants_additions:
                    constants_additions.append(var_line)
        constants_additions.append(self.BLOCK_END.format(ns=ns))

        detect_lines = []
        has_any_check = False
        if detect_hash:
            detect_lines.append(f"hash = {detect_hash}")
            has_any_check = True
        if detect_index_count_val:
            detect_lines.append(f"match_index_count = {detect_index_count_val}")
            has_any_check = True
        detect_lines.append(f"${ns}_ui_active = 1")
        if has_any_check and not is_efmi:
            other_sections[f"[TextureOverrideCheckHash_{ns}]"] = detect_lines

        other_sections[f"[ResourceImageToRender0_{ns}]"] = [f"filename = ./res/swpbg_{ns}.png"]
        for i in range(1, num_buttons + 1):
            other_sections[f"[ResourceSwapButton{i}_{ns}]"] = [f"filename = ./res/swpbtn_{ns}_{i}.png"]

        reset_lines = [f"${ns}_img0_x = 0", f"${ns}_img0_y = 0",
                       f"; @@{self.PANEL_TAG}:reset_zoom0@@",
                       f"${ns}_zoom0 = {self.panel_default_scale:.2f}"]
        zoom_in_lines = [f"${ns}_zoom0 = ${ns}_zoom0 + 0.05"]
        zoom_out_lines = [f"${ns}_zoom0 = ${ns}_zoom0 - 0.05"]

        other_sections[f"[KeyHelp_{ns}]"] = [
            f"condition = ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:help_key@@", f"key = {help_key}",
            "type = cycle", f"${ns}_help = 0,1"
        ]
        other_sections[f"[KeyResetPosition_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:reset_key@@", f"key = {reset_key}",
            "type = cycle",
        ] + reset_lines
        other_sections[f"[KeyZoomIn_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:zoom_in_key@@", f"key = {zoom_in_key}",
            "type = press", f"run = CommandListZoomIn_{ns}"
        ]
        other_sections[f"[KeyZoomOut_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:zoom_out_key@@", f"key = {zoom_out_key}",
            "type = press", f"run = CommandListZoomOut_{ns}"
        ]
        other_sections[f"[KeyMouseDrag_{ns}]"] = [
            f"condition = ${ns}_help == 1 && ${ns}_ui_active == 1",
            f"; @@{self.PANEL_TAG}:drag_key@@", f"key = {drag_key}",
            "type = hold", f"${ns}_mouse_clicked = 1"
        ]

        other_sections[f"[CommandListZoomIn_{ns}]"] = zoom_in_lines
        other_sections[f"[CommandListZoomOut_{ns}]"] = zoom_out_lines

        # 每个按钮的循环切换 CommandList（等价于按下对应 KeySwap 按键）
        for i, b in enumerate(buttons, start=1):
            command_lines = []
            var_names = b.get("var_names") or [b["var_name"]]
            option_counts = b.get("option_counts") or [b["option_count"]]
            for sub_index, (var_name, option_count) in enumerate(zip(var_names, option_counts), start=1):
                sub_name = f"CommandListSwap{i}_{sub_index}_{ns}"
                other_sections[f"[{sub_name}]"] = self._cycle_command_lines(var_name, option_count)
                command_lines.append(f"run = {sub_name}")
            other_sections[f"[CommandListSwap{i}_{ns}]"] = command_lines

        # ---- Present 逻辑（全部使用命名空间变量，与其它面板隔离）----
        if is_efmi:
            # EFMI 已负责对象检测；复用其状态，避免第二个 hash override 竞争入口。
            present_additions.append(f"${ns}_ui_active = $object_detected && $mod_enabled")
        present_additions.append(f"post ${ns}_ui_active = 0")
        present_additions.append(f"if ${ns}_help == 1 && ${ns}_ui_active == 1")
        present_additions.append("    ; --- 1. 尺寸计算 ---")
        present_additions.append(f"    ${ns}_norm_width0 = ${ns}_base_width0 * ${ns}_zoom0")
        present_additions.append(f"    ${ns}_norm_height0 = ${ns}_base_height0 * ${ns}_zoom0")
        for i in range(1, num_buttons + 1):
            # Keep each button's ratio to the panel constant at every zoom level.
            present_additions.append(f"    ${ns}_btn_render_width{i} = ${ns}_btn_width{i} * ${ns}_zoom0")
            present_additions.append(f"    ${ns}_btn_render_height{i} = ${ns}_btn_height{i} * ${ns}_zoom0")

        present_additions.append("\n    ; --- 2. 位置初始化 ---")
        present_additions.append(f"    if ${ns}_img0_x == 0 && ${ns}_img0_y == 0")
        present_additions.append(f"        ${ns}_img0_x = ${ns}_set_x0 * (1 - ${ns}_norm_width0)")
        present_additions.append(f"        ${ns}_img0_y = ${ns}_set_y0 * (1 - ${ns}_norm_height0)")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 3. 计算按钮位置 ---")
        for i in range(1, num_buttons + 1):
            present_additions.append(f"    ${ns}_rel_y{i} = (${ns}_fixed_rel_y{i} * ${ns}_norm_height0) - (${ns}_btn_render_height{i} / 2)")
            present_additions.append(f"    ; @@{self.PANEL_TAG}:btn_align_{i}@@")
            present_additions.append(f"    ${ns}_btn_x{i} = ${ns}_img0_x + (${ns}_fixed_rel_x{i} * ${ns}_norm_width0)")
            present_additions.append(f"    ${ns}_btn_y{i} = ${ns}_img0_y + ${ns}_rel_y{i}")

        present_additions.append("\n    ; --- 4. 按钮按下/弹起检测 ---")
        present_additions.append(f"    ${ns}_btn_click_processed = 0")
        present_additions.append(f"    if ${ns}_mouse_clicked && ${ns}_is_dragging == 0")
        for i in range(1, num_buttons + 1):
            present_additions.append(f"        if cursor_x > ${ns}_btn_x{i} && cursor_x < ${ns}_btn_x{i} + ${ns}_btn_render_width{i} && cursor_y > ${ns}_btn_y{i} && cursor_y < ${ns}_btn_y{i} + ${ns}_btn_render_height{i}")
            present_additions.append(f"            ${ns}_btn_pressed{i} = 1")
            present_additions.append(f"            ${ns}_btn_click_processed = 1")
            present_additions.append(f"        endif")
        present_additions.append("    else")
        present_additions.append(f"        if ${ns}_is_dragging == 0")
        for i in range(1, num_buttons + 1):
            present_additions.append(f"            if ${ns}_btn_pressed{i} == 1")
            present_additions.append(f"                if cursor_x > ${ns}_btn_x{i} && cursor_x < ${ns}_btn_x{i} + ${ns}_btn_render_width{i} && cursor_y > ${ns}_btn_y{i} && cursor_y < ${ns}_btn_y{i} + ${ns}_btn_render_height{i}")
            present_additions.append(f"                    run = CommandListSwap{i}_{ns}")
            present_additions.append(f"                endif")
            present_additions.append(f"                ${ns}_btn_pressed{i} = 0")
            present_additions.append(f"            endif")
        present_additions.append("        endif")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 5. 面板拖拽逻辑 ---")
        present_additions.append(f"    if ${ns}_mouse_clicked")
        present_additions.append(f"        if ${ns}_is_dragging == 0")
        present_additions.append(f"            if cursor_x > ${ns}_img0_x && cursor_x < ${ns}_img0_x + ${ns}_norm_width0 && cursor_y > ${ns}_img0_y && cursor_y < ${ns}_img0_y + ${ns}_norm_height0")
        present_additions.append(f"                ${ns}_is_dragging = 1")
        present_additions.append(f"                ${ns}_drag_x = cursor_x - ${ns}_img0_x")
        present_additions.append(f"                ${ns}_drag_y = cursor_y - ${ns}_img0_y")
        present_additions.append("            else")
        present_additions.append(f"                if ${ns}_btn_click_processed == 0")
        present_additions.append(f"                    ${ns}_click_outside = 1")
        present_additions.append("                endif")
        present_additions.append("            endif")
        present_additions.append("        endif")
        present_additions.append("    else")
        present_additions.append(f"        ${ns}_is_dragging = 0")
        present_additions.append("    endif")
        present_additions.append(f"    if ${ns}_click_outside == 1 && ${ns}_mouse_clicked == 0")
        present_additions.append(f"        ${ns}_help = 0")
        present_additions.append(f"        ${ns}_click_outside = 0")
        present_additions.append("    endif")
        present_additions.append(f"    if ${ns}_is_dragging == 1")
        present_additions.append(f"        ${ns}_img0_x = cursor_x - ${ns}_drag_x")
        present_additions.append(f"        ${ns}_img0_y = cursor_y - ${ns}_drag_y")
        present_additions.append("    endif")

        present_additions.append("\n    ; --- 6. 渲染 ---")
        present_additions.append("    ; 渲染面板背景 (最底层)")
        present_additions.append(f"    ps-t100 = ResourceImageToRender0_{ns}")
        present_additions.append(f"    x87 = ${ns}_norm_width0")
        present_additions.append(f"    y87 = ${ns}_norm_height0")
        present_additions.append(f"    z87 = ${ns}_img0_x")
        present_additions.append(f"    w87 = ${ns}_img0_y")
        present_additions.append(f"    run = CustomShaderDraw_{ns}")
        for i, b in enumerate(buttons, start=1):
            present_additions.append(f"\n    ; 渲染切换按钮{i} ({b['var_name']})")
            present_additions.append(f"    ps-t100 = ResourceSwapButton{i}_{ns}")
            present_additions.append(f"    x87 = ${ns}_btn_render_width{i}")
            present_additions.append(f"    y87 = ${ns}_btn_render_height{i}")
            present_additions.append(f"    z87 = ${ns}_btn_x{i}")
            present_additions.append(f"    if ${ns}_btn_pressed{i} == 1")
            present_additions.append(f"        w87 = ${ns}_btn_y{i} + 0.002")
            active_vars = b.get("var_names") or [b["var_name"]]
            active_condition = " || ".join(f"{var_name} != 0" for var_name in active_vars)
            present_additions.append(f"    else if {active_condition}")
            present_additions.append(f"        w87 = ${ns}_btn_y{i} + 0.001")
            present_additions.append(f"    else")
            present_additions.append(f"        w87 = ${ns}_btn_y{i}")
            present_additions.append(f"    endif")
            present_additions.append(f"    run = CustomShaderDraw_{ns}")
        present_additions.append("endif")

        self._apply_gui_only_guards(sections, ns, buttons)

        shader_def = [
            "hs = null", "ds = null", "gs = null", "cs = null",
            "vs = ./res/draw_2d.hlsl", "ps = ./res/draw_2d.hlsl",
            "blend = ADD SRC_ALPHA INV_SRC_ALPHA", "cull = none",
            "topology = triangle_strip", "o0 = set_viewport bb", "Draw = 4,0", "clear = ps-t100"
        ]

        # 5. 合并写入 ini
        if '[Constants]' not in sections:
            sections['[Constants]'] = []
        for line in constants_additions:
            if line not in sections['[Constants]']:
                sections['[Constants]'].append(line)

        for sec_name, lines in other_sections.items():
            if sec_name not in sections:
                sections[sec_name] = []
            for line in lines:
                if line not in sections[sec_name]:
                    sections[sec_name].append(line)

        custom_shader_section = f"[CustomShaderDraw_{ns}]"
        if custom_shader_section not in sections:
            sections[custom_shader_section] = []
        for line in shader_def:
            if line not in sections[custom_shader_section]:
                sections[custom_shader_section].append(line)

        if '[Present]' not in sections:
            sections['[Present]'] = []
        has_existing_logic = any(self.DUP_GUARD in l for l in sections['[Present]'])
        if not has_existing_logic or _in_place:
            sections['[Present]'].append("")
            sections['[Present]'].append(self.PRESENT_BEGIN.format(ns=ns))
            sections['[Present]'].extend(present_additions)
            sections['[Present]'].append(self.PRESENT_END.format(ns=ns))

        try:
            with open(target_ini_file, 'w', encoding='utf-8') as f:
                f.write("")
            self._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
            print(f"物体切换面板配置已{'原地更新' if _in_place else '合并到'}: {os.path.basename(target_ini_file)}")
            print(f"共生成 {num_buttons} 个切换按钮")
            return True
        except Exception as e:
            print(f"写入INI文件失败: {e}")
            import traceback
            traceback.print_exc()
            return False


classes = (
    SSMT_SwapPanelEntry,
    SSMT_SwapPanelButtonEntry,
    SSMT_OT_SwapPanel_ParseObject,
    SSMT_OT_SwapPanel_Scan,
    SSMT_OT_SwapPanel_Refresh,
    SSMTNode_PostProcess_SwapPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)