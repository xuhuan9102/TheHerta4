import re

import bpy
from bpy.props import IntProperty, StringProperty, CollectionProperty

from .anim_driver_base import (
    ANIM_DRIVER_INPUT_SOCKET_NAME,
    ANIM_DRIVER_OUTPUT_SOCKET_NAME,
    SSMTNode_AnimDriver_Base,
)
from .node_postprocess_draginteraction import (
    DEFAULT_MOD_NAMESPACE,
    MAX_ZONES,
    is_postprocess_node_on_export_chain,
)
from .variable_registry import normalize_variable_name


# 「开关值」列表长度上限（与循环档数钳制同口径：拖拽侧布局按 min(64, …) 收紧）
MAX_CLICK_VALUE_COUNT = 64


def _click_value_tokens(raw):
    """「开关值」文本 → token 列表（保序、未过滤）。

    分隔口径与动画驱动开关（KeyToggle 的 toggle_values）一致：空格或逗号。
    另支持连续数字写法：整串只给了一个全数字 token 时按位拆分
    （"001" → 0 0 1）——单项循环没有意义，而多位数开关值应写成分隔形式
    （如 "0 10"）。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    tokens = text.replace(",", " ").split()
    if len(tokens) == 1 and tokens[0].isdigit() and len(tokens[0]) > 1:
        tokens = list(tokens[0])
    return tokens


def parse_click_values(raw):
    """「开关值」→ 数值列表：丢弃非数值 token，最多 MAX_CLICK_VALUE_COUNT 项。"""
    values = []
    for token in _click_value_tokens(raw):
        try:
            float(token)
        except (TypeError, ValueError):
            continue
        values.append(token)
        if len(values) >= MAX_CLICK_VALUE_COUNT:
            break
    return values


def _drag_drive_feature_linked(candidate):
    """四开关兼容谓词：新拖拽节点读 _feature_var()（变量联动总开关，含 F4⇒F1
    降级与旧值迁移）；旧版本/测试桩无该方法时回退 enable_shapekey_drive。
    若谓词不同步，变量联动关闭后 ClickExport 仍会生成引用未发射 ClickCountF
    的 store 段（悬空引用），见 phase2/n1 §5.2 跨节点读取链。"""
    feature_var = getattr(candidate, "_feature_var", None)
    if feature_var is not None:
        return bool(feature_var())
    return bool(getattr(candidate, "enable_shapekey_drive", False))


def _seed_pending_var_declared(drag_node) -> bool:
    """拖拽节点是否真的会声明 ``$ssmtdrag_seed_pending_*``。

    只有存在冷启动播种条目时才声明（「开关值」模式的点击导出节点不提供播种）。
    不声明却在门控条件里引用它，会让它变成 3DMigoto 的局部变量——本段与拖拽
    节点不在同一段，读到的恒为 0（行为上"恰好"还能跑，但属于悬空引用，导出
    校验会报未声明变量）。谓词不同步（旧节点/测试桩）时保守返回 True，保持
    旧行为不变。
    """
    checker = getattr(drag_node, "_click_export_seed_variable_declared", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:
        return True


def _booted_gate(booted_var: str, seed_pending_var: str, drag_node) -> str:
    """构造导出段的门控条件：boot 完成 && 无待播种。"""
    parts = [f"{booted_var} == 1"]
    if _seed_pending_var_declared(drag_node):
        parts.append(f"{seed_pending_var} == 0")
    return " && ".join(parts)


def _interaction_gate(drag_node, ns, condition=""):
    """Only interactive mode owns GPU click counters; retain legacy owners."""
    getter = getattr(drag_node, "_runtime_variable_names", None)
    if not callable(getter):
        return condition or "1"
    mode = getter(ns)[0]
    return f"{mode} == 1" + (f" && ({condition})" if condition else "")


def _trigger_gate_vars(drag_node, ns) -> list:
    """取拖拽节点的"按住"变量（LMB/X）。取不到时返回空 = 不做回读门控。"""
    getter = getattr(drag_node, "_click_export_trigger_vars", None)
    if not callable(getter):
        return []
    try:
        return [str(v) for v in (getter(ns) or []) if v]
    except Exception:
        return []


class ClickExportTargetItem(bpy.types.PropertyGroup):
    variable_name: StringProperty(
        name="受控变量",
        description="要写入区域点击次数的变量名；与物体切换节点的变量名一致即可驱动该切换（可从切换节点复制其预分配变量名）",
        default="",
    )


class SSMT_UL_ClickExportTargets(bpy.types.UIList):
    bl_idname = "SSMT_UL_CLICK_EXPORT_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            icon_val = 'VIEWZOOM' if item.variable_name else 'ERROR'
            row.prop(item, "variable_name", text="", icon=icon_val)


class SSMT_OT_ClickExportTargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.click_export_target_add"
    bl_label = "添加受控变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        item = node.click_target_list.add()
        item.variable_name = ""
        node.click_target_active = len(node.click_target_list) - 1
        return {'FINISHED'}


class SSMT_OT_ClickExportTargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.click_export_target_remove"
    bl_label = "删除受控变量"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        idx = node.click_target_active
        if 0 <= idx < len(node.click_target_list):
            node.click_target_list.remove(idx)
            node.click_target_active = min(idx, len(node.click_target_list) - 1)
        return {'FINISHED'}


class SSMT_OT_ClickExportCycleRefresh(bpy.types.Operator):
    bl_idname = "ssmt.click_export_cycle_refresh"
    bl_label = "刷新循环档数"
    bl_description = "按受控变量列表查找对应物体切换节点，取其选项数的最大值作为循环档数"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    node_name: StringProperty(default="")

    def execute(self, context):
        tree = getattr(getattr(context, 'space_data', None), 'edit_tree', None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if not node:
            return {'CANCELLED'}
        best, matched = node._compute_cycle_from_swaps()
        node.cycle_length = best
        if node.click_value_sequence():
            self.report(
                {'INFO'},
                f"已配置开关值：循环档数由开关值列表长度（{len(node.click_value_sequence())}）决定，"
                f"本次仅更新备用值 {best}",
            )
            return {'FINISHED'}
        if matched:
            self.report({'INFO'}, f"已获取循环档数 {best}（匹配 {matched} 个物体切换节点）")
        else:
            self.report({'WARNING'}, "未匹配到物体切换节点，循环档数已置 0（跟随形态键推导）")
        return {'FINISHED'}


class SSMTNode_AnimDriver_ClickExport(SSMTNode_AnimDriver_Base):
    bl_idname = 'SSMTNode_AnimDriver_ClickExport'
    bl_label = '点击计数导出'
    bl_icon = 'DRIVER'

    click_zone_id: IntProperty(
        name="绑定区域编号",
        description="拖拽交互节点区域空物体列表中的稳定区域 ID；仅命中模式下按住左键/X 点击该区域递增计数",
        default=0, min=0, max=MAX_ZONES - 1,
    )

    cycle_length: IntProperty(
        name="循环档数",
        description="该区域点击次数的循环长度（0..档数-1）。0=跟随形态键点击档位推导；点右侧刷新按物体切换节点选项数自动获取（多个变量取最大值）。会同步改变该区域形态键点击档位的循环长度。已配置开关值时以开关值列表长度为准（本项作为备用值保留）",
        default=0, min=0, max=64,
    )

    click_values: StringProperty(
        name="开关值",
        description="每次点击依次写入受控变量的值（空格或逗号分隔，如 0 0 1，也可直接写 001）。"
                    "点完一轮回到第一项，未点击时写第一项；列表长度就是该区域的循环档数。"
                    "受控变量被外部改动（热键/驱动器）时以变量为准，点击从当前进度继续。"
                    "留空 = 直接写入原始点击计数（旧行为）",
        default="",
    )

    click_target_list: CollectionProperty(
        type=ClickExportTargetItem,
        name="受控变量列表",
    )

    click_target_active: IntProperty(
        name="当前受控变量",
        default=0,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_INPUT_SOCKET_NAME)
        self.outputs.new('SSMTSocketAnimDriver', ANIM_DRIVER_OUTPUT_SOCKET_NAME)
        self.width = 300

    # ------------------------------------------------------------------
    # 数据收集
    # ------------------------------------------------------------------

    def _iter_target_vars(self):
        """归一化受控变量列表：['$a', '$b']，过滤空项并保序去重。"""
        result = []
        seen = set()
        for item in self.click_target_list:
            name = normalize_variable_name(getattr(item, "variable_name", "") or "")
            if not name:
                continue
            var = f"${name}"
            if var in seen:
                continue
            seen.add(var)
            result.append(var)
        return result

    # ------------------------------------------------------------------
    # 开关值（跨节点只读契约：拖拽交互节点据此定循环档数与冷启动播种）
    # ------------------------------------------------------------------

    def click_value_sequence(self):
        """开关值列表（保序、去非数值、最多 64 项）。空 = 写原始点击计数（旧行为）。"""
        return parse_click_values(getattr(self, "click_values", ""))

    def effective_cycle_length(self):
        """有效循环档数：已配置开关值 → 列表长度（列表即循环）；否则 → 循环档数属性。"""
        sequence = self.click_value_sequence()
        if sequence:
            return len(sequence)
        try:
            return int(getattr(self, "cycle_length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _find_anim_owner_trees(self):
        """回溯引用本动画驱动树的主蓝图树列表。"""
        anim_tree = getattr(self, "id_data", None)
        if anim_tree is None:
            return []
        owners = []
        for tree in bpy.data.node_groups:
            if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
                continue
            if tree.get("is_animation_driver"):
                continue
            for node in getattr(tree, "nodes", None) or []:
                if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_AnimDriver":
                    continue
                if getattr(node, "mute", False):
                    continue
                if not is_postprocess_node_on_export_chain(tree, node):
                    continue
                if str(getattr(node, "blueprint_name", "") or "") != anim_tree.name:
                    continue
                owners.append(tree)
                break
        return owners

    def _find_drag_drive_nodes(self):
        """在关联主树中查找开启形态键驱动的拖拽交互节点。"""
        candidates = []
        for tree in self._find_anim_owner_trees():
            for candidate in getattr(tree, "nodes", None) or []:
                if getattr(candidate, "bl_idname", "") == "SSMTNode_PostProcess_DragInteraction" \
                        and not getattr(candidate, "mute", False) \
                        and is_postprocess_node_on_export_chain(tree, candidate) \
                        and _drag_drive_feature_linked(candidate):
                    candidates.append(candidate)
        return candidates

    def _find_drag_drive_node(self):
        candidates = self._find_drag_drive_nodes()
        return candidates[0] if len(candidates) == 1 else None

    def _compute_cycle_from_swaps(self):
        """按受控变量查找物体切换节点，返回 (最大选项数, 匹配到的节点数)。"""
        best = 0
        matched = 0
        for var in self._iter_target_vars():
            target = var.lstrip("$")
            for tree in bpy.data.node_groups:
                if getattr(tree, "bl_idname", "") != 'SSMTBlueprintTreeType':
                    continue
                for node in getattr(tree, "nodes", None) or []:
                    if getattr(node, "bl_idname", "") != "SSMTNode_ObjectSwap" or getattr(node, "mute", False):
                        continue
                    candidates = set()
                    custom = str(getattr(node, "custom_var_name", "") or "").strip().lstrip("$")
                    if custom:
                        candidates.add(custom)
                    assigned = str(getattr(node, "assigned_variable_name", "") or "").strip().lstrip("$")
                    if assigned:
                        candidates.add(assigned)
                    if target not in candidates:
                        continue
                    try:
                        count = int(getattr(node, "input_slot_count", 0) or 0)
                    except Exception:
                        count = 0
                    if count > 0:
                        matched += 1
                        best = max(best, count)
        return best, matched

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.prop(self, "click_zone_id")
        box.prop(self, "click_values")
        sequence = self.click_value_sequence()
        if sequence:
            box.label(
                text=f"开关值循环 {len(sequence)} 档: {' '.join(sequence)}",
                icon='DRIVER',
            )
            dropped = len(_click_value_tokens(getattr(self, "click_values", ""))) - len(sequence)
            if dropped > 0:
                box.label(
                    text=f"已忽略 {dropped} 项（非数值或超出 {MAX_CLICK_VALUE_COUNT} 项上限）",
                    icon='ERROR',
                )
        row = box.row(align=True)
        # 开关值即循环：此时循环档数由列表长度决定，属性仅作备用值
        row.enabled = not sequence
        row.prop(self, "cycle_length")
        op = row.operator("ssmt.click_export_cycle_refresh", text="", icon='FILE_REFRESH')
        op.node_name = self.name

        box.separator()
        row = box.row(align=True)
        row.label(text="受控变量:", icon='VIEWZOOM')
        op = row.operator("ssmt.click_export_target_add", text="", icon='ADD')
        op.node_name = self.name
        op = row.operator("ssmt.click_export_target_remove", text="", icon='REMOVE')
        op.node_name = self.name

        if self.click_target_list:
            box.template_list(
                "SSMT_UL_CLICK_EXPORT_TARGETS", "",
                self, "click_target_list",
                self, "click_target_active",
                rows=max(2, min(len(self.click_target_list), 6)),
            )
        else:
            box.label(text="添加受控变量（与物体切换节点变量名一致即联动）", icon='INFO')

        drag_nodes = self._find_drag_drive_nodes()
        if not drag_nodes:
            box.label(text="警告: 未找到同模组开启形态键驱动的拖拽交互节点", icon='ERROR')
        elif len(drag_nodes) > 1:
            box.label(text="警告: 找到多个拖拽所有者，请只保留一个", icon='ERROR')

    # ------------------------------------------------------------------
    # INI 段生成
    # ------------------------------------------------------------------

    def generate_ini_segment(self, connected_nodes=None) -> str:
        target_vars = self._iter_target_vars()
        if not target_vars:
            return ""
        drag_node = self._find_drag_drive_node()
        if drag_node is None:
            return ""
        try:
            ns = drag_node._resolve_namespace("")
        except Exception:
            ns = DEFAULT_MOD_NAMESPACE
        # EFMI 分支跨节点契约（研究② §3.2）：经拖拽节点 _click_export_names 取
        # 前缀资源/变量（EFMI 模式 → EFMI 前缀）；旧节点/测试桩回退 zzmi 前缀
        names_fn = getattr(drag_node, "_click_export_names", None)
        if callable(names_fn):
            click_f_resource, booted_var, seed_pending_var = names_fn(ns)
        else:
            click_f_resource = f"ResourceDragShapeKeyClickCountF_{ns}"
            booted_var = f"$ssmtdrag_booted_{ns}"
            seed_pending_var = f"$ssmtdrag_seed_pending_{ns}"
        try:
            zone = int(getattr(self, "click_zone_id", 0) or 0)
        except (TypeError, ValueError):
            return ""
        if not 0 <= zone < MAX_ZONES:
            return ""
        sequence = self.click_value_sequence()
        if sequence:
            return self._generate_click_value_segment(
                sequence, target_vars, click_f_resource, booted_var, seed_pending_var, zone, ns,
                drag_node,
            )
        # 值仲裁、变量为主（修复「快捷键切换被每帧点击计数回读顶掉」）：
        #   变量变化（热键/驱动器）→ 不回读，置 seed_pending 触发播种把变量值
        #   推回点击计数缓冲（播种 = 驱动 CS 的 seed 模式，天然就是
        #   「变量→缓冲」方向）；变量未变 → 拉取缓冲值（点击推进）。
        #   prev 辅助变量按受控变量名派生（同一变量天然共享、不同变量天然
        #   隔离，且多节点不会互相覆盖索引）。
        block_lines = [
            "[Present]",
            "; 点击计数导出（值仲裁、变量为主）：变量变化时经 seed_pending 触发播种",
            "; 把变量值推回点击计数缓冲（下一帧点击从新值继续推进）；变量未变且",
            "; 处于按住期间才拉取缓冲值（点击推进）；热键改动绝不被回读顶掉。",
            f"if {_booted_gate(booted_var, seed_pending_var, drag_node)}",
        ]
        # 回读最小化：store 只在「按住 / 上一帧还按着（松开沿，防丢最后一次点击）」
        # 时执行——点击计数只在按住期间推进，空闲帧零回读。
        trigger_vars = _trigger_gate_vars(drag_node, ns)
        held_var = f"$ssmtdrag_ckheld_{ns}_{zone}"
        held_prev_var = f"$ssmtdrag_ckheldprev_{ns}_{zone}"
        if trigger_vars:
            held_expr = " || ".join(f"{var} == 1" for var in trigger_vars)
            block_lines.extend([
                f"\t{held_var} = 0",
                f"\tif {_interaction_gate(drag_node, ns, held_expr)}",
                f"\t\t{held_var} = 1",
                "\tendif",
            ])
        prev_names = []
        for var in target_vars:
            stem = re.sub(r"[^0-9A-Za-z_]", "_", str(var).lstrip("$"))
            prev = f"$ssmtdrag_ckprev_{ns}_{stem}"
            prev_names.append(prev)
            block_lines.extend([
                f"\tif {var} != {prev}",
                f"\t\t{prev} = {var}",
                f"\t\t{seed_pending_var} = 1",
                "\telse",
            ])
            if trigger_vars:
                block_lines.extend([
                    f"\t\tif {_interaction_gate(drag_node, ns, f'{held_var} == 1 || {held_prev_var} == 1')}",
                    f"\t\t\tstore = {var}, {click_f_resource}, {zone}",
                    "\t\tendif",
                ])
            else:
                gate = _interaction_gate(drag_node, ns)
                block_lines.extend([f"\t\tif {gate}",
                                    f"\t\t\tstore = {var}, {click_f_resource}, {zone}", "\t\tendif"])
            block_lines.extend([
                f"\t\t{prev} = {var}",
                "\tendif",
            ])
        if trigger_vars:
            block_lines.append(f"\t{held_prev_var} = {held_var}")
        block_lines.append("endif")
        # 每绑定声明一个 prev 辅助变量（全局、跨节可见）
        globals_lines = ["[Constants]"]
        if trigger_vars:
            globals_lines.append(f"global {held_var} = 0")
            globals_lines.append(f"global {held_prev_var} = 0")
        for prev in prev_names:
            globals_lines.append(f"global {prev} = 0")
        return "\n".join(globals_lines) + "\n" + "\n".join(block_lines)

    def _generate_click_value_segment(self, sequence, target_vars, click_f_resource,
                                      booted_var, seed_pending_var, zone, ns, drag_node=None):
        """「开关值」模式段：点击 = 开关值列表的步进（对齐动画驱动开关的 cycle 语义）。

        语义（用户口径）：点第一下写列表第 1 项、第二下第 2 项……点完一轮回到第 1 项；
        未点击时写第 1 项。列表是**位置序**（允许重复项，如 0 0 1 的两项都是 0），
        所以用自维护下标推进，而不是从变量值反查位置。

        点击沿由点击计数缓冲的变化判定：计数每帧镜像进 ClickCountF，区域档位数
        与列表长度可能不等（布局对形态键档位取大），故只按「计数变了」认定一次
        点击，不按计数取值——列表长度因此与形态键档位解耦。首个读到的计数只作
        基线（同区域若有旧模式节点冷启动播种、或缓冲残留非零值，不会被误判成点击）。

        与旧模式的两点差异（有意）：
          * 冷启动播种不适用：列表值 → 点击计数不可逆（重复项），拖拽侧已把本节点
            排除在 `_click_export_seed_entries` 之外，重启后统一回到列表第 1 项；
          * 变量被外部改动（热键/驱动器）时以变量为准且不置 seed_pending，
            避免写回一个无法反解为计数的值。
        """
        values = list(sequence)
        count = len(values)
        tag = f"{re.sub(r'[^0-9A-Za-z_]', '_', str(getattr(self, 'name', '') or 'node'))}_{zone}"
        read_var = f"$ssmtdrag_ckread_{ns}_{tag}"
        last_var = f"$ssmtdrag_cklast_{ns}_{tag}"
        init_var = f"$ssmtdrag_ckinit_{ns}_{tag}"
        index_var = f"$ssmtdrag_ckidx_{ns}_{tag}"
        value_var = f"$ssmtdrag_ckval_{ns}_{tag}"
        held_var = f"$ssmtdrag_ckheld_{ns}_{tag}"
        held_prev_var = f"$ssmtdrag_ckheldprev_{ns}_{tag}"
        # 点击边沿标志：只有本帧**真的推进了一档**才置 1。
        # 受控变量只在边沿那一刻写一次 —— 不能每帧持续写：受控变量可能同时是
        # 动画驱动的播放状态/自动动画目标（例如 `$animation_pausedN`），每帧强写会
        # 把播放状态按回列表首项，表现为该动画只播一帧就被按停。
        edge_var = f"$ssmtdrag_ckedge_{ns}_{tag}"

        globals_lines = [
            "[Constants]",
            # 本帧读到的点击计数（store 目标）/ 上一帧读到的计数（点击沿检测）/
            # 基线已建立标志（首个读数只做基线）
            f"global {read_var} = 0",
            f"global {last_var} = 0",
            f"global {init_var} = 0",
            # 开关值下标：预置为末项，首次点击推进到第 0 项
            f"global {index_var} = {count - 1}",
            # 当前开关值：未点击时即列表第 1 项
            f"global {value_var} = {values[0]}",
            # 本帧是否发生了点击推进（受控变量只在此时写一次）
            f"global {edge_var} = 0",
        ]
        # ---- 回读最小化：点击计数只在按住 LMB/X 期间会变 ----
        # store 是 GPU→CPU 同步，旧实现每帧无条件回读一次。现在只在
        # 「按住 / 上一帧还按着（松开沿，防丢最后一次点击）/ 首帧建基线」时回读。
        trigger_vars = _trigger_gate_vars(drag_node, ns)
        if trigger_vars:
            globals_lines.extend([
                f"global {held_var} = 0",
                f"global {held_prev_var} = 0",
            ])
        block_lines = [
            "[Present]",
            "; 点击计数导出（开关值循环）：每次点击依次写入列表下一项，点完一轮回第一项；",
            "; 未点击时写列表首项；受控变量被外部改动（热键/驱动器）时以变量为准。",
            f"if {_booted_gate(booted_var, seed_pending_var, drag_node)}",
        ]
        gate = _interaction_gate(drag_node, ns)
        if gate != "1":
            reset = [f"{edge_var} = 0", f"if !({gate})", f"\t{init_var} = 0"]
            if trigger_vars:
                reset.extend([f"\t{held_var} = 0", f"\t{held_prev_var} = 0"])
            reset.append("endif")
            block_lines[-1:-1] = reset
            block_lines[-1] += f" && ({gate})"
        if trigger_vars:
            held_expr = " || ".join(f"{var} == 1" for var in trigger_vars)
            block_lines.extend([
                f"\t{held_var} = 0",
                f"\tif {held_expr}",
                f"\t\t{held_var} = 1",
                "\tendif",
                # 读 prev 要在更新 prev 之前（松开沿判定靠它）
                f"\tif {held_var} == 1 || {held_prev_var} == 1 || {init_var} == 0",
            ])
            indent = "\t\t"
        else:
            indent = "\t"
        block_lines.extend([
            f"{indent}store = {read_var}, {click_f_resource}, {zone}",
            f"{indent}{edge_var} = 0",
            f"{indent}if {init_var} == 0",
            f"{indent}\t{init_var} = 1",
            f"{indent}\t{last_var} = {read_var}",
            f"{indent}elif {read_var} != {last_var}",
            f"{indent}\t{last_var} = {read_var}",
            f"{indent}\t{index_var} = {index_var} + 1",
            f"{indent}\tif {index_var} >= {count}",
            f"{indent}\t\t{index_var} = 0",
            f"{indent}\tendif",
            # 点击推进沿：本次点击确定了下标，值随之更新
            f"{indent}\t{edge_var} = 1",
        ])
        for idx, value in enumerate(values):
            block_lines.append(f"{indent}\t{'if' if idx == 0 else 'elif'} {index_var} == {idx}")
            block_lines.append(f"{indent}\t\t{value_var} = {value}")
        block_lines.extend([
            f"{indent}\telse",
            f"{indent}\t\t{value_var} = {values[0]}",
            f"{indent}\tendif",
            f"{indent}endif",
        ])
        if trigger_vars:
            block_lines.append("\tendif")
            block_lines.append(f"\t{held_prev_var} = {held_var}")
        for var in target_vars:
            # prev 按受控变量名派生：同一变量天然共享、不同变量天然隔离
            stem = re.sub(r"[^0-9A-Za-z_]", "_", str(var).lstrip("$"))
            prev = f"$ssmtdrag_ckprev_{ns}_{stem}"
            globals_lines.append(f"global {prev} = 0")
            # 受控变量只在**两个时刻**写：
            #   ① 点击推进沿（把新的开关值写下去一次）；
            #   ② 变量被外部改动（热键/驱动器）时以变量为准、重建基线。
            # 不再每帧持续强写 —— 受控变量可能同时是动画驱动的播放状态
            # （`$animation_pausedN`）或自动动画目标，持续强写会把该动画按停。
            block_lines.extend([
                f"\tif {edge_var} == 1",
                f"\t\t{var} = {value_var}",
                f"\t\t{prev} = {var}",
                f"\telif {var} != {prev}",
                f"\t\t{prev} = {var}",
                "\tendif",
            ])
        block_lines.append("endif")
        return "\n".join(globals_lines) + "\n" + "\n".join(block_lines)


classes = (
    ClickExportTargetItem,
    SSMT_UL_ClickExportTargets,
    SSMT_OT_ClickExportTargetAdd,
    SSMT_OT_ClickExportTargetRemove,
    SSMT_OT_ClickExportCycleRefresh,
    SSMTNode_AnimDriver_ClickExport,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
