import bpy
import os
import time

from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR
from ..utils.command_utils import CommandUtils
from ..utils.log_utils import LOG

from ..common.global_config import GlobalConfig
from ..common.global_key_count_helper import GlobalKeyCountHelper
from ..common.config_table_backup import (
    backup_config_tables,
    find_config_table_files,
    restore_config_tables,
)

from ..blueprint.model import BluePrintModel
from ..blueprint.direct_export import (
    execute_direct_export,
    has_direct_export_mode,
)
from ..blueprint.export_helper import BlueprintExportHelper
from ..blueprint.preprocess import PreProcessHelper
from ..blueprint.export_parallel import ExportRoundExecutor, ParallelExportCoordinator, ParallelExportError
try:  # 共享步骤计划：拿不到时（精简加载 / 测试桩）退回旧预算
    from ..blueprint.export_step_plan import build_step_plan
except Exception:  # pragma: no cover
    build_step_plan = None
from ..blueprint.export_background import (
    BACKGROUND_BUSY_MESSAGE,
    BackgroundExportSession,
    QUICK_EXPORT_ROUND_PLAN,
    build_quick_export_temp_tree,
    clear_active_session,
    clear_progress_header,
    format_percent_bar,
    get_active_session,
    push_progress_header,
    safe_object_name,
)
from ..blueprint.sync import refresh_blueprint_sync_state
from ..common.global_properties import GlobalProterties
from ..common.logic_name import LogicName


def _raise_for_unknown_logic_name() -> None:
    """当游戏预设未加载或不受支持时抛出异常"""
    raise ValueError(
        "当前游戏预设未加载或不受支持，未执行任何主导出逻辑。"
        f" 当前 logic_name='{GlobalConfig.logic_name}'，请先确认全局设置中的游戏预设已正确加载。"
    )


#: 后台导出时蓝图节点的进度配色（RGB；Blender 5.x 的 node.color 是 3 分量，
#: 旧版本是 4 分量，赋值时按节点现有维度补齐 —— 见 ``_node_color_for``）
_BG_NODE_PENDING_COLOR = (0.45, 0.52, 0.68)
_BG_NODE_RUNNING_COLOR = (0.95, 0.72, 0.1)
_BG_NODE_DONE_COLOR = (0.22, 0.78, 0.3)
#: 节点状态 → 高亮颜色：尚未开始=蓝灰、正在执行=黄、已完成=绿。
#: 只认这三种状态，其它状态（未来的新增值）一律忽略。
_BG_NODE_STATUS_COLORS = {
    "pending": _BG_NODE_PENDING_COLOR,
    "running": _BG_NODE_RUNNING_COLOR,
    "done": _BG_NODE_DONE_COLOR,
}
# 节点颜色高亮（用户要求恢复）：写 node.color 会触发节点编辑器重绘，这里只在
# 节点状态真的变化时写一次（见 _sync_node_highlight 的 records 状态判断）。
_BG_NODE_HIGHLIGHT_ENABLED = True
# 不限流（恢复 B 方案之前的节奏）；需要时可调大来限制节点编辑器重绘频率。
_BG_HIGHLIGHT_MIN_INTERVAL = 0.0
#: 阶段名仍停在这些值 = 参与导出的节点还没真正开始干活（读作 pending）。
#: worker 启动后主进程先把阶段标成「加载工程」，直到它写出第一个 stage.json。
_BG_PARTICIPANT_IDLE_STAGES = ("", "加载工程")
#: 视图导航事件（滚轮 / 触控板 / 旋转）：导出期间必须放行给界面，
#: 否则蓝图视图的缩放 / 平移会被 modal 吞掉。
_BG_VIEW_NAV_EVENT_TYPES = frozenset({
    'WHEELUPMOUSE',
    'WHEELDOWNMOUSE',
    'TRACKPADPAN',
    'TRACKPADZOOM',
    'MOUSEROTATE',
    'MOUSEPAN',
})
#: 中键按下 / 松开期间需要放行的鼠标移动事件（中键拖动平移）。
_BG_MOUSE_MOVE_EVENT_TYPES = frozenset({'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'})
#: 嵌套蓝图节点与 SSMT 蓝图树类型：导出流程会递归展开
#: ``SSMTNode_Blueprint_Nest.blueprint_name`` 指向的蓝图树，节点高亮也要跟进。
_BG_NEST_NODE_IDNAME = "SSMTNode_Blueprint_Nest"
_BG_SSMT_TREE_IDNAME = "SSMTBlueprintTreeType"
_BG_NEST_SKIP_NAMES = frozenset({"", "NONE"})


def _iter_nested_blueprint_trees(tree):
    """某个蓝图树里 ``SSMTNode_Blueprint_Nest`` 引用到的嵌套蓝图树。

    跳过静音节点、``NONE`` / 空名、不存在的树、非 SSMT 蓝图树（与 worker 的
    ``direct_export._iter_nested_blueprint_trees`` 同口径）；异常静默。
    """
    for node in getattr(tree, "nodes", None) or ():
        try:
            if str(getattr(node, "bl_idname", "") or "") != _BG_NEST_NODE_IDNAME:
                continue
            if bool(getattr(node, "mute", False)):
                continue
            blueprint_name = str(getattr(node, "blueprint_name", "") or "").strip()
            if blueprint_name in _BG_NEST_SKIP_NAMES:
                continue
            nested_tree = bpy.data.node_groups.get(blueprint_name)
            if nested_tree is None:
                continue
            if str(getattr(nested_tree, "bl_idname", "") or "") != _BG_SSMT_TREE_IDNAME:
                continue
            yield nested_tree
        except Exception:
            continue


def _iter_blueprint_trees(tree):
    """主蓝图树 + 递归展开的嵌套蓝图树（先主树，再按引用顺序广度优先）。

    ``visited`` 按对象 id 记已展开的树，循环引用（A 嵌 B、B 又嵌 A）不会死循环。
    """
    if tree is None:
        return

    visited = set()
    pending = [tree]
    while pending:
        current = pending.pop(0)
        if current is None or id(current) in visited:
            continue
        visited.add(id(current))
        yield current
        for nested_tree in _iter_nested_blueprint_trees(current):
            if id(nested_tree) not in visited:
                pending.append(nested_tree)


def _blueprint_node_key(tree, node) -> str:
    """节点进度键：``树名/节点名``；没有树名时退回纯节点名（旧格式兼容）。"""
    node_name = str(getattr(node, "name", "") or "").strip()
    if not node_name:
        return ""
    tree_name = str(getattr(tree, "name", "") or "").strip()
    if not tree_name:
        return node_name
    return f"{tree_name}/{node_name}"


#: 状态栏后台导出进度文本的固定前缀（由 ``session.progress_text()`` 生成）。
_BG_PROGRESS_PREFIX = "后台导出："


def compose_background_progress_text(text: str, progress_bar: str) -> str:
    """把文本进度条插到「后台导出：」前缀之后、步骤文本之前。

    ``session.progress_text()`` 产出 ``后台导出：13/32 前处理-恢复非镜像｜树/节点``，
    ``_update_progress_bar()`` 产出 ``[██████░░] 40%``。状态栏布局要求进度条在
    前缀之后、步骤文本之前：:

        后台导出：[██████░░] 40% 13/32 前处理-恢复非镜像｜树/节点

    进度条为空（准备 / 加载工程 / 完成等没有百分比的阶段）时原样返回 ``text``；
    ``text`` 不是标准进度文案（如「后台导出完成」）时也不插入，避免拼出怪文本。
    """
    if not progress_bar:
        return text
    text = str(text or "")
    if not text.startswith(_BG_PROGRESS_PREFIX):
        return text
    step_text = text[len(_BG_PROGRESS_PREFIX):].strip()
    if not step_text:
        return f"{_BG_PROGRESS_PREFIX}{progress_bar}"
    return f"{_BG_PROGRESS_PREFIX}{progress_bar} {step_text}"


#: 后台导出 modal 轮询的共用实现：Generate Mod 与快速局部导出共用同一套
#: 进度显示 / 节点高亮 / 三种收尾，避免两处复制。
class _BackgroundExportMixin:
    """后台导出 modal：轮询进度、节点高亮、原生进度条与收尾分支。"""

    def modal(self, context, event):
        """后台导出轮询：每 0.2s 读一次 stage.json / result.json 并刷新状态栏。

        事件分派（**只放行视图导航事件**）：

        * ``ESC`` → 取消导出；
        * ``TIMER`` → 轮询进度、写状态栏 / 进度条、同步节点高亮，返回
          ``RUNNING_MODAL``；
        * ``MIDDLEMOUSE`` / 滚轮 / 触控板 → ``PASS_THROUGH``（中键按下期间记
          ``_bg_view_nav``，松开时清掉），导出期间仍可平移 / 缩放节点编辑器；
        * ``MOUSEMOVE`` / ``INBETWEEN_MOUSEMOVE`` → 仅中键拖动中 ``PASS_THROUGH``；
        * 其余非 TIMER 事件一律 ``RUNNING_MODAL``（吞掉）。

        为什么不能把所有非 TIMER 事件都放行：Blender 的进度光标由
        ``wm_cursor_number_impl`` 设置后会把 ``win->cursor`` 清零，之后任何
        UI 层的 ``WM_cursor_set``（鼠标 hover 改光标等）都会把进度光标覆盖成
        普通 / 繁忙指针，下一次 ``progress_update`` 又设回进度 —— 每次鼠标事件
        切换一次，表现为指针在百分比与繁忙指针之间高频跳动。
        """
        session = getattr(self, "_bg_session", None)
        if session is None:
            self._bg_view_nav = False
            self._stop_background_timer(context)
            clear_progress_header(context)
            return {'CANCELLED'}

        if event.type == 'ESC':
            self._finish_background_export(context, session, "cancel")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return self._modal_navigation_result(event)

        status = session.poll()
        self._sync_node_highlight(session)
        text = session.progress_text()
        progress_bar = self._update_progress_bar(context, session)
        # 布局：后台导出：[██░░] 40% 13/32 阶段｜节点 —— 进度条插在前缀之后、步骤文本之前。
        push_progress_header(compose_background_progress_text(text, progress_bar), context)

        if status == "running":
            return {'RUNNING_MODAL'}

        if status == "success":
            self._finish_background_export(context, session, "success")
            return {'FINISHED'}

        if status == "cancelled":
            self._finish_background_export(context, session, "cancel")
            return {'CANCELLED'}

        self._finish_background_export(context, session, "error")
        return {'CANCELLED'}

    def _modal_navigation_result(self, event):
        """非 TIMER 事件分派：只放行视图导航，其余吞掉以保住进度光标。

        * ``MIDDLEMOUSE``：按下记 ``_bg_view_nav=True``、松开清掉，均 ``PASS_THROUGH``；
        * 滚轮 / 触控板 / 旋转：``PASS_THROUGH``；
        * ``MOUSEMOVE`` / ``INBETWEEN_MOUSEMOVE``：仅中键拖动中放行；
        * 其它（键盘、左右键、其它鼠标键）：``RUNNING_MODAL``，不让界面重设光标。
        """
        event_type = event.type

        if event_type == 'MIDDLEMOUSE':
            value = getattr(event, "value", "")
            if value == 'PRESS':
                self._bg_view_nav = True
            elif value == 'RELEASE':
                self._bg_view_nav = False
            return {'PASS_THROUGH'}

        if event_type in _BG_VIEW_NAV_EVENT_TYPES:
            return {'PASS_THROUGH'}

        if event_type in _BG_MOUSE_MOVE_EVENT_TYPES:
            if getattr(self, "_bg_view_nav", False):
                return {'PASS_THROUGH'}
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}

    def _finish_background_export(self, context, session, outcome):
        """收尾：恢复节点颜色、结束进度条、停 timer、按结果更新 header 并清理。

        失败 / 取消**不再**用 ``self.report({'ERROR'})`` / ``self.report({'WARNING'})``：
        Blender 原生 Report 弹窗会挡住界面，改为把摘要留在 header（保留到用户操作）
        并 ``print`` 到控制台（含 worker.log 路径）。成功仍用 INFO（INFO 不弹窗）。
        """
        self._bg_view_nav = False
        self._end_progress_bar(context)
        self._restore_node_colors()
        self._stop_background_timer(context)
        clear_active_session(session)
        # 只有 Generate Mod 装了配置表备份流程；快速导出的算子没有这套钩子
        restore_if_needed = getattr(self, "_restore_config_tables_if_needed", None)
        if restore_if_needed is not None:
            restore_if_needed()

        if outcome == "cancel":
            session.cancel()
            log_path = session.cleanup()
            push_progress_header("后台导出已取消", context)
            print(f"⚠️ 后台导出已取消（日志: {log_path}）")
            return

        if outcome == "success":
            log_path = session.cleanup()
            self._open_background_output_folder(session)
            # 成功时**保留**一句完成文本（原来这里 clear_progress_header，状态栏瞬间
            # 清空，用户看到"进度消失"以为没跑完）。文本留到用户下一次操作自然覆盖。
            push_progress_header(self._completion_header(session), context)
            self.report({'INFO'}, f"{session.success_summary()}（日志: {log_path}）")
            return

        message = session.error_summary()
        session.cleanup()
        clear_active_session(session)
        push_progress_header(f"后台导出失败：{message[:160]}", context)
        print(f"❌ {message}")

    @staticmethod
    def _session_elapsed_label(session):
        """成功收尾用的「用时 X.X 秒」；会话没有计时（或计时异常）时返回空串。

        优先用会话自带的 ``elapsed_text()``，没有就退回 ``elapsed_seconds()`` 自己格式化；
        任何异常都静默（计时失败不能让收尾文案崩掉）。
        """
        try:
            elapsed_text = getattr(session, "elapsed_text", None)
            if callable(elapsed_text):
                label = str(elapsed_text() or "").strip()
                if label:
                    return label
            elapsed_seconds = getattr(session, "elapsed_seconds", None)
            if callable(elapsed_seconds):
                return f"用时 {float(elapsed_seconds()):.1f} 秒"
        except Exception:
            return ""
        return ""

    @staticmethod
    def _completion_header(session):
        """成功收尾的状态栏文本：
        ``后台导出完成（N/N，用时 X.X 秒）``；没有步数时省略分数，
        没有计时（旧会话 / 计时失败）时省略用时，退化为原本文案。
        """
        try:
            done = int(getattr(session, "done_steps", 0) or 0)
            total = int(getattr(session, "total_steps", 0) or 0)
        except (TypeError, ValueError):
            done = 0
            total = 0
        elapsed_label = SSMTGenerateModBlueprint._session_elapsed_label(session)
        if total > 0:
            detail = f"{max(0, min(done, total))}/{total}"
            if elapsed_label:
                detail = f"{detail}，{elapsed_label}"
            return f"后台导出完成（{detail}）"
        if elapsed_label:
            return f"后台导出完成（{elapsed_label}）"
        return "后台导出完成"

    @staticmethod
    def _session_progress_percent(session):
        """取主进程算好的整数百分比（0~100）；取不到时回退 ``done/total``。

        worker 的 total 会随阶段动态增长，原生进度条与文本条都只吃**百分比**，
        刻度固定 0~100，避免 total 变化时刻度错位。没有 total 时返回 ``None``
        （不显示进度条，状态栏只留纯文本）。
        """
        try:
            value = getattr(session, "progress_percent", None)
            if value is not None:
                return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            pass
        try:
            total = int(getattr(session, "total_steps", 0) or 0)
            done = int(getattr(session, "done_steps", 0) or 0)
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None
        return max(0, min(100, int(done * 100 / total)))

    def _update_progress_bar(self, context, session):
        """把主进程的百分比同步到 ``window_manager`` 原生进度条，并返回**文本进度条**。

        原生条固定 ``progress_begin(0, 100)`` + ``progress_update(int(percent))``：
        总步数是 worker 动态上报的（20+ 个阶段），用固定 0~100 刻度才不会在
        total 变化时错位。文本条（``[████░░] 40%``）**总是**返回并拼到 header 上
        （弹出窗口遮挡状态栏时也能看到进度）；``done/total`` 步数由
        ``progress_text`` 单独显示。

        任何异常都静默：进度显示失败绝不能影响导出。
        """
        percent = self._session_progress_percent(session)
        if percent is None:
            return ""

        try:
            window_manager = getattr(context, "window_manager", None)
            if window_manager is None:
                return format_percent_bar(percent)

            if not getattr(self, "_bg_progress_started", False):
                begin = getattr(window_manager, "progress_begin", None)
                if not callable(begin):
                    return format_percent_bar(percent)
                begin(0, 100)
                self._bg_progress_started = True
                # 新一轮进度条：清掉上一轮的重绘节流记录。
                self._bg_last_progress_state = None

            # 变化节流（用户实测：导出期间鼠标指针在「进度」与「繁忙」之间高频跳动）：
            # window_manager.progress_update() 每次都会刷新 GHOST 光标进度状态
            # （WM_cursor_progress），modal 每 0.2s 调一次就会让光标每 0.2s 重画一次。
            # 这里只在百分比真的变化时才调用；文本进度条仍然每 tick 返回。
            if getattr(self, "_bg_last_progress_state", None) == percent:
                return format_percent_bar(percent)

            update = getattr(window_manager, "progress_update", None)
            if callable(update):
                update(percent)
                self._bg_last_progress_state = percent
            return format_percent_bar(percent)
        except Exception:
            self._bg_progress_started = False
            # 异常后下次成功的 tick 必须能重新 begin 并重新 update，
            # 所以节流记录一并清掉。
            self._bg_last_progress_state = None
            try:
                return format_percent_bar(percent)
            except Exception:
                return ""

    def _end_progress_bar(self, context):
        """结束原生进度条（success / cancel / error 三个收尾分支都会走到）；重复调用安全。"""
        started = bool(getattr(self, "_bg_progress_started", False))
        self._bg_progress_started = False
        self._bg_last_progress_state = None
        if not started:
            return
        try:
            window_manager = getattr(context, "window_manager", None)
            end = getattr(window_manager, "progress_end", None)
            if callable(end):
                end()
        except Exception:
            pass

    def _sync_node_highlight(self, session):
        """把后台导出的节点级进度画到蓝图上：未开始=蓝灰、执行中=黄、已完成=绿。

        节点以 ``树名/节点名`` 为键（worker 按所属蓝图树上报，嵌套蓝图里的
        节点也能上色）；不含 ``/`` 的旧键回退到 ``session.tree_name`` 主树 +
        节点名。第一次改色前记录原 ``use_custom_color`` / ``color``，收尾由
        ``_restore_node_colors`` 原样恢复。任何异常都静默（进度不能影响导出）。

        写入节流（用户实测：打开节点编辑器时状态栏随机卡几秒~十几秒）：
        节点颜色写入会触发节点编辑器整体重绘，节点树大时一次重绘很慢。这里
        只处理「状态真的变了」的节点，并且最多每 ``_BG_HIGHLIGHT_MIN_INTERVAL``
        秒同步一次（未到时间的变更留到下一次 0.2s 轮询再写）。
        ``pending`` 节点在导出刚开始时一次性上色（可能几十个），之后 records
        里已记为 ``pending``，不再重复写入。

        worker 只在后处理阶段把节点键写进 ``stage.json``；蓝图里参与导出的
        前处理 / 蓝图解析 / 导出轮次节点由 ``_collect_participant_node_names``
        在主进程补全（同样递归嵌套蓝图树），见 ``_participant_phase_status``。
        """
        if not _BG_NODE_HIGHLIGHT_ENABLED:
            return
        try:
            worker_nodes = dict(getattr(session, "stage_nodes", None) or {})

            tree_name = str(getattr(session, "tree_name", "") or "")
            lookup = getattr(self, "_bg_node_lookup", None)
            if lookup is None:
                lookup = {}
                self._bg_node_lookup = lookup
            tree = self._lookup_highlight_tree(lookup, tree_name)
            if tree is None:
                return

            records = getattr(self, "_bg_node_colors", None)
            if records is None:
                records = {}
                self._bg_node_colors = records

            # worker 的 stage.json 只登记后处理节点；参与导出的其它节点
            # （前处理 / 蓝图解析 / 导出轮次）由主进程按蓝图树补全状态，
            # 与后处理节点一起按 蓝灰 → 黄 → 绿 推进。
            nodes_state = worker_nodes
            participants = lookup.get("participants")
            if participants is None:
                participants = self._collect_participant_node_names(tree)
                lookup["participants"] = participants
            if participants:
                phase_status = self._participant_phase_status(session, worker_nodes)
                nodes_state = dict(worker_nodes)
                for key in participants:
                    nodes_state.setdefault(key, phase_status)

            if not nodes_state:
                return

            # 键统一解析成 ``(树名, 节点名)``：新键带树名，旧 stage.json 的纯
            # 节点名回退到主树。同一节点只留先出现的那份状态（worker 上报优先
            # 于主进程推断的参与节点状态），避免同一节点被两种键重复改色。
            changes = []
            seen_keys = set()
            for name, status in nodes_state.items():
                key = self._split_node_key(name, tree_name)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if not key[1] or status not in _BG_NODE_STATUS_COLORS:
                    continue
                if (records.get(key) or {}).get("status") == status:
                    continue
                changes.append((key, status))
            if not changes:
                return

            now = time.monotonic()
            last = float(getattr(self, "_bg_last_highlight_time", 0.0) or 0.0)
            if now - last < _BG_HIGHLIGHT_MIN_INTERVAL:
                return
            self._bg_last_highlight_time = now

            node_cache = lookup.setdefault("nodes", {})
            for key, status in changes:
                record = records.get(key)
                node = node_cache.get(key)
                if node is None:
                    node_tree = self._lookup_highlight_tree(lookup, key[0]) or tree
                    node = self._find_blueprint_node(node_tree, key[1])
                    if node is None:
                        continue
                    node_cache[key] = node

                if record is None:
                    record = {
                        "node": node,
                        "use_custom_color": bool(getattr(node, "use_custom_color", False)),
                        "color": self._node_color_tuple(node),
                        "status": "",
                    }
                    records[key] = record
                record["status"] = status

                color = _BG_NODE_STATUS_COLORS[status]
                node.use_custom_color = True
                node.color = self._node_color_for(node, color)
        except Exception:
            pass

    @staticmethod
    def _lookup_highlight_tree(lookup, tree_name):
        """按树名取蓝图树并缓存（主树 / 嵌套树共用）；取不到返回 None。"""
        name = str(tree_name or "")
        if not name:
            return None
        trees = lookup.setdefault("trees", {})
        if name in trees:
            return trees[name]
        try:
            tree = bpy.data.node_groups.get(name)
        except Exception:
            tree = None
        if tree is None:
            return None
        trees[name] = tree
        return tree

    @staticmethod
    def _split_node_key(name, default_tree_name=""):
        """解析 ``树名/节点名`` 进度键；不含 ``/`` 的旧键回退到主树。"""
        text = str(name or "")
        if "/" in text:
            tree_part, node_part = text.split("/", 1)
            if tree_part and node_part:
                return (tree_part, node_part)
        return (str(default_tree_name or ""), text)

    @staticmethod
    def _find_blueprint_node(tree, name):
        """在指定蓝图树里按名字取节点：优先 ``nodes.get``（O(1)），否则线性兜底。

        ``tree`` 由进度键解析而来（主树或嵌套蓝图树），调用方按
        ``(树名, 节点名)`` 缓存结果。
        """
        collection = getattr(tree, "nodes", None)
        if collection is None:
            return None

        getter = getattr(collection, "get", None)
        if callable(getter):
            node = getter(name)
            if node is not None:
                return node

        for candidate in collection or []:
            if str(getattr(candidate, "name", "") or "") == name:
                return candidate
        return None

    @staticmethod
    def _collect_participant_node_names(tree):
        """蓝图里「参与导出」但 worker 不上报状态的节点键（前处理 / 解析 / 轮次）。

        worker 的 ``stage.json`` 只登记后处理节点，所以导出期间前处理等节点
        一直没有颜色。这里在主进程按蓝图树补全：递归遍历主树 + 嵌套蓝图树
        （``SSMTNode_Blueprint_Nest`` 引用的树），所有连到输出节点、且不是
        后处理 / 输出 / 中转的节点都算参与导出；键格式 ``树名/节点名``，
        不同蓝图树里的同名节点不会冲突。循环引用由 ``_iter_blueprint_trees``
        的 visited 集合挡住。

        树结构在导出期间不变，结果由调用方缓存在 ``_bg_node_lookup`` 里只算一次。
        """
        keys = []
        seen_keys = set()
        try:
            checker = getattr(BlueprintExportHelper, "_is_node_connected_to_output", None)
        except Exception:
            checker = None
        for current_tree in _iter_blueprint_trees(tree):
            for node in getattr(current_tree, "nodes", None) or []:
                try:
                    if bool(getattr(node, "mute", False)):
                        continue
                    bl_idname = str(getattr(node, "bl_idname", "") or "")
                    if bl_idname.startswith("SSMTNode_PostProcess_"):
                        continue
                    if bl_idname.startswith("SSMTNode_Result_"):
                        continue
                    if bl_idname in ("NodeReroute", "NodeFrame"):
                        continue
                    if callable(checker):
                        try:
                            if not checker(current_tree, node):
                                continue
                        except Exception:
                            continue
                    key = _blueprint_node_key(current_tree, node)
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    keys.append(key)
                except Exception:
                    continue
        return keys

    @staticmethod
    def _participant_phase_status(session, worker_nodes):
        """参与导出节点（前处理 / 蓝图解析 / 导出轮次）的推算状态。

        worker 只在**后处理阶段**才开始往 ``stage.json`` 写节点状态，因此
        ``stage_nodes`` 非空即代表「前处理等阶段已结束」→ ``done``；
        阶段名还停在加载工程 / 为空时还没开工 → ``pending``；其余阶段 → ``running``。
        """
        if worker_nodes:
            return "done"
        stage = str(getattr(session, "stage", "") or "")
        if stage in _BG_PARTICIPANT_IDLE_STAGES:
            return "pending"
        return "running"

    @staticmethod
    def _node_color_tuple(node):
        """取节点原色（保留 Blender 自身的维度：5.x 是 3 分量，旧版是 4 分量）。"""
        try:
            color = tuple(getattr(node, "color", ()) or ())
        except Exception:
            color = ()
        try:
            return tuple(float(value) for value in color[:4])
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)

    @staticmethod
    def _node_color_for(node, rgb):
        """按节点当前 ``color`` 的维度生成赋值序列（兼容 3 / 4 分量）。"""
        try:
            current = tuple(getattr(node, "color", ()) or ())
        except Exception:
            current = ()
        values = tuple(float(value) for value in tuple(rgb)[:3])
        if len(current) >= 4:
            return values + (1.0,)
        return values

    def _restore_node_colors(self):
        """恢复后台导出期间改过的节点颜色；重复调用安全。"""
        records = getattr(self, "_bg_node_colors", None) or {}
        self._bg_node_colors = None
        for record in records.values():
            try:
                node = record.get("node")
                if node is None:
                    continue
                node.use_custom_color = bool(record.get("use_custom_color", False))
                node.color = record.get("color") or (0.0, 0.0, 0.0)
            except Exception:
                continue

    def _open_background_output_folder(self, session):
        """打开产物目录。

        主进程的「本次导出蓝图」指针在 invoke 的 finally 里已还原，这里直接用
        会话记录的输出目录（command_utils 走指针解析，会落到场景全局目录）。
        """
        try:
            if not GlobalProterties.open_mod_folder_after_generate_mod():
                return
        except Exception:
            return

        output_dir = getattr(session, "output_dir", "")
        try:
            if output_dir and os.path.isdir(output_dir):
                os.startfile(output_dir)
                return
        except Exception:
            pass
        CommandUtils.OpenGeneratedModFolder()

    def _stop_background_timer(self, context):
        self._end_progress_bar(context)
        timer = getattr(self, "_bg_timer", None)
        self._bg_timer = None
        self._bg_session = None
        self._restore_node_colors()
        if timer is None:
            return
        try:
            context.window_manager.event_timer_remove(timer)
        except Exception:
            pass


def _clear_export_section_registry() -> None:
    """导出开始前清空 DrawIB→ini 段登记表（条件后处理节点依赖它做门控）。

    登记表由 ``common/export_section_registry.py`` 提供，可能尚未落地（精简加载 /
    测试桩），因此用软导入：拿不到模块或清理失败都只留一条提示，绝不让导出失败。
    """
    try:
        from ..common import export_section_registry as _section_registry
    except Exception:
        return
    try:
        _section_registry.clear()
    except Exception as error:
        print(f"⚠️ 清理导出段登记表失败（已忽略）: {error}")


class SSMTGenerateModBlueprint(_BackgroundExportMixin, bpy.types.Operator):
    """根据蓝图架构生成 Mod 文件的主导出 Blender 算子"""
    bl_idname = "ssmt.generate_mod_blueprint"
    bl_label = TR.translate("生成Mod")
    bl_description = "根据当前工作空间对应的蓝图架构生成对应的Mod文件"
    bl_options = {'REGISTER','UNDO'}

    _POSTPROCESS_NODE_PREFIX = 'SSMTNode_PostProcess_'

    blueprint_name: bpy.props.StringProperty(
        name="Blueprint Name",
        default="",
        options={'SKIP_SAVE'},
    ) # type: ignore

    overwrite_config: bpy.props.EnumProperty(
        name="配置表覆盖",
        description="导出目录中已存在配置表（*.ini），选择是否覆盖",
        items=[
            ('YES', "是", "将旧配置表改名并移动至备份，然后正常导出并生成新的配置表"),
            ('NO', "否", "将旧配置表改名并移动至备份，导出完成后恢复旧配置表"),
        ],
        default='YES',
        options={'SKIP_SAVE'},
    ) # type: ignore

    def _resolve_target_tree(self, context):
        """解析目标蓝图节点树"""
        requested_tree_name = str(getattr(self, "blueprint_name", "") or "").strip()
        if requested_tree_name:
            tree = BlueprintExportHelper.get_selected_blueprint_tree(
                selected_name=requested_tree_name,
                context=context,
            )
        else:
            tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if tree and global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        # 记录本次导出使用的蓝图，仅用于解析输出目录（path_generate_mod_folder）。
        # 这里绝不能改 runtime_blueprint_tree_name：它决定导出目标蓝图，
        # 一旦被写入就会被后续的蓝图解析当作回落目标，导致一直串到旧蓝图。
        # 该指针由调用方限定作用域：invoke/execute 都会在 finally 里还原，
        # 见 _invoke_with_output_blueprint / _execute_export。
        if tree:
            GlobalConfig.set_output_blueprint_tree(tree)

        return tree

    def invoke(self, context, event):
        """入口分流：勾选「后台导出」时走后台实例 + modal 轮询，否则走原有同步路径

        _resolve_target_tree 会登记「本次导出使用的蓝图」（决定输出目录），
        而走到 invoke 不代表一定会进入 execute（用户可以取消弹窗；后台导出也会
        在 invoke 阶段就把输出目录算进任务清单），所以这里同样要用 try/finally
        把这个指针还原，不能让它停在弹窗 / 后台启动阶段。
        """
        previous_output_tree_name = GlobalConfig.get_output_blueprint_tree_name()
        self._pending_background_export = False
        try:
            if GlobalProterties.is_background_export_enabled():
                # 防重入：已有后台导出在跑时不再起第二个后台实例
                if get_active_session() is not None:
                    self.report({'WARNING'}, BACKGROUND_BUSY_MESSAGE)
                    return {'CANCELLED'}
                return self._invoke_background_export(context, event)
            return self._invoke_with_output_blueprint(context, event)
        finally:
            GlobalConfig.restore_output_blueprint_tree(previous_output_tree_name)

    def _invoke_background_export(self, context, event):
        """后台导出：保存工程快照 → 起干净后台实例跑完整导出 → modal 轮询进度。"""
        GlobalConfig.read_from_main_json_ssmt4()

        tree = self._resolve_target_tree(context)
        if not tree:
            self.report({'ERROR'}, "未找到当前蓝图，请在蓝图编辑器中点击 Generate Mod")
            return {'CANCELLED'}

        # 后台实例支持普通导出与直出导出。NTEMI 有独立导出器，仍回落到原有入口。
        if GlobalConfig.logic_name == LogicName.NTEMI:
            self.report({'WARNING'}, "NTEMI 导出不支持后台导出，已回落到同步导出")
            return self._invoke_with_output_blueprint(context, event)

        # 旧配置表：与同步导出同一套「覆盖 / 保留」确认弹窗。后台导出以前整段跳过，
        # 既不追问也不备份，旧配置表会被新产物直接盖掉。确认结果由 execute() 接着跑。
        dialog_result = self._invoke_config_table_dialog(context, tree)
        if dialog_result is not None:
            self._pending_background_export = True
            return dialog_result

        return self._start_background_export(context, tree)

    def _start_background_export(self, context, tree):
        """存快照 → 起后台实例 → 挂 modal 轮询；确认弹窗与直接启动共用这一段。"""
        # 立即反馈：会话启动里要先把工程存成 480MB 快照（数秒）再起子进程，
        # 这段时间主进程没有任何进度文本 —— 先写状态栏，避免"点了导出十几秒没反应"。
        push_progress_header("后台导出：正在保存工程快照…", context)
        # 后台导出入口：主进程侧先清掉上一次残留的 ini 段登记（worker 进程内另有一次清理）。
        _clear_export_section_registry()
        try:
            session = self._create_background_session(tree)
            started = session.start()
        except Exception as error:
            clear_progress_header(context)
            self.report({'ERROR'}, f"后台导出启动失败: {error}")
            return {'CANCELLED'}

        if not started:
            clear_progress_header(context)
            self.report({'ERROR'}, session.error or "后台导出启动失败")
            return {'CANCELLED'}

        self._bg_session = session
        self._bg_node_colors = {}
        # 新一次导出：高亮缓存（树 / 节点 / 参与节点）必须跟着会话重建
        self._bg_node_lookup = None
        self._bg_progress_started = False
        # 中键拖动平移状态：modal 只在中键按住期间放行鼠标移动。
        self._bg_view_nav = False
        self._bg_timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        # 快照已保存：接下来后台实例要先加载 480MB 工程（约 10 秒）才写出第一个
        # stage.json，期间 modal 轮询读到的是会话的「加载工程」阶段 —— 先把完整
        # 的等待文本写上，让用户明确知道在等什么。
        push_progress_header("后台导出：后台实例正在加载工程（约 10 秒）…", context)

        LOG.info("")
        LOG.info("📦 后台导出已启动（主界面不阻塞）")
        LOG.info(f"   工程快照: {session.snapshot_path}")
        LOG.info(f"   后台日志: {session.log_path}")
        return {'RUNNING_MODAL'}

    def _invoke_config_table_dialog(self, context, tree):
        """导出目录里有旧配置表时弹确认框；返回 None 表示不需要确认。

        同步导出与后台导出共用：两条链路都必须先问「覆盖 / 保留」再动旧配置表。
        """
        if not self._has_postprocess_nodes(tree):
            return None

        mod_export_path = GlobalConfig.path_generate_mod_folder()
        if not mod_export_path or not os.path.exists(mod_export_path):
            return None

        config_table_paths = find_config_table_files(mod_export_path)
        if not config_table_paths:
            return None

        self._export_path = mod_export_path
        self._config_table_paths = config_table_paths
        self._backup_pending = True
        return context.window_manager.invoke_props_dialog(self, width=460)

    def _backup_config_tables_before_export(self):
        """确认后把旧配置表改名并移入备份目录（同步 / 后台共用同一实现）。"""
        self._backup_entries = []
        if not getattr(self, "_backup_pending", False):
            return

        mod_export_path = GlobalConfig.path_generate_mod_folder()
        self._backup_entries = backup_config_tables(
            mod_export_path,
            getattr(self, "_config_table_paths", []) or [],
        )
        self._backup_pending = False
        if self._backup_entries:
            backup_root = os.path.dirname(self._backup_entries[0][1])
            if self.overwrite_config == 'YES':
                LOG.info(f"💾 旧配置表已备份至: {backup_root}，导出后将生成新的配置表")
            else:
                LOG.info(f"💾 旧配置表已备份至: {backup_root}，导出完成后将恢复旧配置表")

    def _restore_config_tables_if_needed(self):
        """后台导出收尾：用户选「否」时把备份的旧配置表覆盖回原位。"""
        if not getattr(self, "_restore_config_tables_after_export", False):
            return
        self._restore_config_tables_after_export = False
        try:
            restore_config_tables(getattr(self, "_backup_entries", []) or [])
            LOG.info("💾 已恢复导出前的旧配置表（后台导出选择「否」）")
        except Exception as error:
            LOG.warning(f"恢复旧配置表失败: {error}")

    def _create_background_session(self, tree):
        """按当前蓝图准备会话参数（与同步导出同一套轮次计划计算）。"""
        has_shapekey_export = BlueprintExportHelper.has_shapekey_postprocess_node(tree)
        max_shapekey_slot = 0
        if has_shapekey_export:
            max_shapekey_slot = BlueprintExportHelper.calculate_max_shapekey_slot_count(tree)
            if max_shapekey_slot == 0:
                has_shapekey_export = False

        max_multi_file_count = BlueprintExportHelper.calculate_max_export_count(tree)
        has_multi_file_export = BlueprintExportHelper.has_multi_file_export_nodes()

        export_plan = self._build_export_round_plan(
            has_shapekey_export=has_shapekey_export,
            max_shapekey_slot=max_shapekey_slot,
            has_multi_file_export=has_multi_file_export,
            max_multi_file_count=max_multi_file_count,
        )

        direct_export = has_direct_export_mode(tree)
        total_steps = self._estimate_background_total_steps(
            tree,
            export_plan,
            direct_export,
            has_shapekey_export=has_shapekey_export,
            max_shapekey_slot=max_shapekey_slot,
            has_multi_file_export=has_multi_file_export,
        )

        return BackgroundExportSession(
            tree_name=tree.name,
            output_dir=GlobalConfig.path_generate_mod_folder(),
            round_plans=export_plan,
            logic_name=GlobalConfig.logic_name,
            workspace_name=GlobalConfig.get_workspace_name(),
            has_shapekey_export=has_shapekey_export,
            max_shapekey_slot=max_shapekey_slot,
            has_multi_file_export=has_multi_file_export,
            max_multi_file_count=max_multi_file_count,
            direct_export=direct_export,
            total_steps=total_steps,
        )

    def _estimate_background_total_steps(
        self,
        tree,
        export_plan,
        direct_export,
        has_shapekey_export=None,
        max_shapekey_slot=None,
        has_multi_file_export=None,
    ) -> int:
        """预计算后台导出的总步骤数（进度条的分母）。

        与 worker 用**同一个** ``export_step_plan.build_step_plan``：清单在导出
        开始时一次算好，``total = len(清单)`` 全程固定不变，所以「加载工程」阶段
        显示的分母就是最终分母。

        ``has_shapekey_export`` / ``max_shapekey_slot`` / ``has_multi_file_export``
        由 ``_create_background_session`` 传入（与写进 manifest 的值完全一致）；
        单独调用时按蓝图树重新推断。

        任何异常都退化为旧口径预算，绝不让进度计算阻断导出启动。
        """
        try:
            postprocess_nodes = self._collect_background_postprocess_nodes(tree)
        except Exception:
            postprocess_nodes = []

        if has_shapekey_export is None:
            try:
                has_shapekey_export = bool(
                    BlueprintExportHelper.has_shapekey_postprocess_node(tree)
                )
            except Exception:
                has_shapekey_export = False
        if max_shapekey_slot is None:
            max_shapekey_slot = 0
            if has_shapekey_export:
                try:
                    max_shapekey_slot = int(
                        BlueprintExportHelper.calculate_max_shapekey_slot_count(tree) or 0
                    )
                except Exception:
                    max_shapekey_slot = 0
            if max_shapekey_slot <= 0:
                has_shapekey_export = False
        if has_multi_file_export is None:
            try:
                has_multi_file_export = bool(
                    BlueprintExportHelper.has_multi_file_export_nodes()
                )
            except Exception:
                has_multi_file_export = False

        try:
            if callable(build_step_plan):
                step_plan = build_step_plan(
                    tree,
                    direct_export=bool(direct_export),
                    has_shapekey_export=bool(has_shapekey_export),
                    max_shapekey_slot=int(max_shapekey_slot or 0),
                    has_multi_file_export=bool(has_multi_file_export),
                    round_plans=export_plan,
                    postprocess_nodes=postprocess_nodes,
                )
                if step_plan:
                    return len(step_plan)
        except Exception:
            pass
        return max(1, len(export_plan or []) + len(postprocess_nodes) + 1)

    @staticmethod
    def _collect_background_postprocess_nodes(tree) -> list:
        """取导出链路上真正会执行的后处理节点（与直出用的是同一个收集函数）。"""
        try:
            return list(BlueprintExportHelper._collect_postprocess_nodes(tree) or [])
        except Exception:
            return []

    def _invoke_with_output_blueprint(self, context, event):
        tree = self._resolve_target_tree(context)
        if not tree:
            return self.execute(context)

        dialog_result = self._invoke_config_table_dialog(context, tree)
        if dialog_result is not None:
            return dialog_result

        return self.execute(context)

    def draw(self, context):
        """绘制确认对话框"""
        layout = self.layout
        layout.label(text="⚠️ 导出目录中存在配置表！", icon='ERROR')
        layout.separator()
        layout.label(text=f"路径: {getattr(self, '_export_path', '未知')}")
        for config_path in getattr(self, '_config_table_paths', []) or []:
            layout.label(text=f"   • {os.path.basename(config_path)}")
        layout.separator()
        layout.label(text="是否覆盖配置表？")
        layout.prop(self, "overwrite_config", expand=True)
        if self.overwrite_config == 'YES':
            layout.label(text="旧配置表将改名并移动至备份，导出后生成新的配置表。", icon='INFO')
        else:
            layout.label(text="旧配置表将改名并移动至备份，导出完成后自动恢复旧配置表。", icon='INFO')

    def _has_postprocess_nodes(self, tree) -> bool:
        """检查蓝图中是否包含后处理节点"""
        for node in tree.nodes:
            if node.bl_idname.startswith(self._POSTPROCESS_NODE_PREFIX) and not node.mute:
                return True
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                blueprint_name = getattr(node, 'blueprint_name', '')
                if blueprint_name and blueprint_name != 'NONE':
                    nested_tree = bpy.data.node_groups.get(blueprint_name)
                    if nested_tree and self._has_postprocess_nodes(nested_tree):
                        return True
        return False

    @staticmethod
    def _find_ntmi_modimp_output_node(tree):
        if not tree:
            return None
        for node in getattr(tree, "nodes", []) or []:
            if getattr(node, "bl_idname", "") == "SSMTNode_Result_Output_NTMIModImp":
                return node
        return None

    def execute(self, context):
        """导出入口：把「输出目录蓝图」指针的作用域严格限定在本次导出内。

        以前只在 _resolve_target_tree 里 set、从不还原，指针会一直留到下一次导出：
        非蓝图导出（快速局部导出 / NTMI ModImp）只要调用 path_generate_mod_folder()
        就会命中上一个蓝图，输出目录串到旧蓝图（与「非蓝图导出流程不受影响」矛盾）。
        这里用 try/finally 覆盖所有路径（含异常与提前 return）。
        """
        previous_output_tree_name = GlobalConfig.get_output_blueprint_tree_name()
        try:
            if getattr(self, "_pending_background_export", False):
                self._pending_background_export = False
                return self._execute_background_export_after_dialog(context)
            return self._execute_export(context)
        finally:
            GlobalConfig.restore_output_blueprint_tree(previous_output_tree_name)

    def _execute_background_export_after_dialog(self, context):
        """确认弹窗点「OK」后接着启动后台实例：旧配置表在导出开始之前就先备份。"""
        tree = self._resolve_target_tree(context)
        if not tree:
            self.report({'ERROR'}, "未找到当前蓝图，请在蓝图编辑器中点击 Generate Mod")
            return {'CANCELLED'}

        self._backup_config_tables_before_export()
        self._restore_config_tables_after_export = bool(
            self._backup_entries and str(getattr(self, "overwrite_config", "YES")) == "NO"
        )
        return self._start_background_export(context, tree)

    def _execute_export(self, context):
        GlobalConfig.read_from_main_json_ssmt4()
        
        TimerUtils.start_session("Mod导出")

        TimerUtils.start_stage("蓝图验证")
        tree = self._resolve_target_tree(context)
        if not tree:
            self.report({'ERROR'}, "未找到当前蓝图，请在蓝图编辑器中点击 Generate Mod")
            return {'CANCELLED'}

        if GlobalConfig.logic_name == LogicName.NTEMI:
            ntmi_output_node = self._find_ntmi_modimp_output_node(tree)
            if ntmi_output_node is not None:
                self.report(
                    {'ERROR'},
                    "NTEMI 已停用普通导出链路，请改用蓝图中的“NTMI ModImp Output”节点按钮进行导出。"
                )
            else:
                self.report(
                    {'ERROR'},
                    "NTEMI 已停用普通导出链路。请先从工作空间重新生成或添加“NTMI ModImp Output”节点后再导出。"
                )
            return {'CANCELLED'}

        # 配置表备份：弹窗确认后，将旧配置表改名并移动至备份目录
        self._backup_config_tables_before_export()

        BlueprintExportHelper.set_runtime_blueprint_tree(tree)
        BlueprintExportHelper.reset_direct_export_runtime_state(clear_postprocess_caches=True)
        # 导出开始：清空上一次导出残留的 ini 段登记，条件后处理节点只应看到本次导出的段。
        _clear_export_section_registry()
        TimerUtils.end_stage("蓝图验证")

        LOG.start_collecting()
        BluePrintModel.clear_object_name_mapping()
        GlobalKeyCountHelper.initialize()

        LOG.info("=" * 60)
        LOG.info("🚀 开始生成Mod")
        LOG.info("=" * 60)

        # 直出模式复用原有按钮入口，避免用户在标准导出和直出之间来回切换。
        if has_direct_export_mode(tree):
            LOG.info("⚡ 检测到直出模式，切换到直出导出流程")

            export_success = False
            error_message = None

            try:
                execute_direct_export(context=context, tree=tree)
                self._validate_generated_ini_variables(GlobalConfig.path_generate_mod_folder())
                export_success = True
            except Exception as e:
                error_message = f"直出失败: {str(e)}"
                print(f"❌ 直出过程中发生错误: {e}")
                LOG.exception(e)
                PreProcessHelper.cleanup_copies(silent=True)

            # 选择「否」时：导出完成后把备份的旧配置表覆盖回原位；「是」则保留新配置表
            if self.overwrite_config == 'NO':
                restore_config_tables(self._backup_entries)

            LOG.info("")
            LOG.info("📍 清理阶段")
            LOG.info("-" * 40)
            PreProcessHelper.cleanup_copies()

            LOG.info("")
            LOG.info("=" * 60)
            if export_success:
                LOG.info("✅ 直出完成!")
                LOG.info("=" * 60)
                self.report({'INFO'}, TR.translate("Generate Mod Success!"))
                CommandUtils.OpenGeneratedModFolder()
            else:
                LOG.info(f"❌ Mod生成失败: {error_message}")
                LOG.info("=" * 60)
                self.report({'ERROR'}, error_message)

            TimerUtils.print_summary()

            LOG.stop_collecting()
            log_name = LOG.save_to_text_editor()
            if log_name:
                print(f"📄 导出日志已保存至文本编辑器: {log_name}")

            if export_success:
                return {'FINISHED'}
            return {'CANCELLED'}

        TimerUtils.start_stage("导出前同步")
        refresh_summary = refresh_blueprint_sync_state(
            tree=tree,
            context=context,
            include_all_blueprints=True,
        )
        TimerUtils.end_stage("导出前同步")
        LOG.info(
            f"🔄 导出前刷新同步状态: {refresh_summary['tree_count']} 个蓝图, "
            f"{refresh_summary['updated_count']} 处节点引用更新"
        )

        has_shapekey_export = BlueprintExportHelper.has_shapekey_postprocess_node(tree)
        max_shapekey_slot = 0
        if has_shapekey_export:
            max_shapekey_slot = BlueprintExportHelper.calculate_max_shapekey_slot_count(tree)
            if max_shapekey_slot == 0:
                has_shapekey_export = False
                LOG.info("   未检测到形态键，跳过形态键导出")

        max_multi_file_count = BlueprintExportHelper.calculate_max_export_count(tree)
        has_multi_file_export = BlueprintExportHelper.has_multi_file_export_nodes()

        if has_shapekey_export:
            LOG.info("")
            LOG.info("🎭 形态键导出模式")
            LOG.info("-" * 40)
            LOG.info(f"   检测到形态键配置后处理节点")
            LOG.info(f"   最大形态键槽位: {max_shapekey_slot}")
            LOG.info(f"   形态键导出次数: {max_shapekey_slot + 1} 次 (基态 + {max_shapekey_slot} 个槽位)")

        if has_multi_file_export:
            LOG.info("")
            LOG.info("📦 多文件导出模式")
            LOG.info("-" * 40)
            LOG.info(f"   检测到 {len(BlueprintExportHelper.multi_file_export_nodes)} 个多文件导出节点")
            LOG.info(f"   多文件导出次数: {max_multi_file_count} 次")
            for node in BlueprintExportHelper.multi_file_export_nodes:
                obj_list = getattr(node, 'object_list', [])
                LOG.info(f"   - 节点 '{node.name}': {len(obj_list)} 个物体")

        LOG.info("")
        LOG.info("📍 第一阶段：检测与准备")
        LOG.info("-" * 40)

        export_success = False
        error_message = None
        buffer_sizes = []
        blueprint_model = None
        export_plan = []

        try:
            export_plan = self._build_export_round_plan(
                has_shapekey_export=has_shapekey_export,
                max_shapekey_slot=max_shapekey_slot,
                has_multi_file_export=has_multi_file_export,
                max_multi_file_count=max_multi_file_count,
            )
            self._log_export_plan(export_plan)

            if has_shapekey_export:
                BlueprintExportHelper.collect_shapekey_objects(tree)
                LOG.info(f"   收集到 {len(BlueprintExportHelper.shapekey_objects)} 个形态键物体")

            if len(export_plan) == 1:
                LOG.info("")
                LOG.info("📍 第二阶段：单次导出")
                LOG.info("-" * 40)

                round_result = self._execute_main_round(tree, export_plan[0])
                blueprint_model = round_result["blueprint_model"]
                reference_size = round_result["buffer_size"]
                buffer_sizes.append(reference_size)

            else:
                first_round = export_plan[0]
                middle_rounds = export_plan[1:-1]
                last_round = export_plan[-1]

                LOG.info("")
                LOG.info("📍 第二阶段：主线程首轮导出")
                LOG.info("-" * 40)

                first_result = self._execute_main_round(tree, first_round)
                blueprint_model = first_result["blueprint_model"]
                reference_size = first_result["buffer_size"]
                buffer_sizes.append(reference_size)

                if middle_rounds:
                    if GlobalProterties.enable_parallel_export_rounds():
                        LOG.info("")
                        LOG.info("📍 第三阶段：并行中间轮次导出")
                        LOG.info("-" * 40)
                        middle_results = ParallelExportCoordinator.execute_middle_rounds(tree, middle_rounds)
                        middle_results.sort(key=lambda item: item.get("round_index", 0))
                        for result in middle_results:
                            self._validate_buffer_size(reference_size, result["buffer_size"], result["round_index"], result["buffer_folder_name"])
                            buffer_sizes.append(result["buffer_size"])
                    else:
                        LOG.info("")
                        LOG.info("📍 第三阶段：串行中间轮次导出")
                        LOG.info("-" * 40)
                        for round_plan in middle_rounds:
                            round_result = self._execute_main_round(tree, round_plan)
                            self._validate_buffer_size(reference_size, round_result["buffer_size"], round_plan["round_index"], round_plan["buffer_folder_name"])
                            buffer_sizes.append(round_result["buffer_size"])

                LOG.info("")
                LOG.info("📍 第四阶段：主线程尾轮导出")
                LOG.info("-" * 40)
                last_result = self._execute_main_round(tree, last_round)
                blueprint_model = last_result["blueprint_model"]
                self._validate_buffer_size(reference_size, last_result["buffer_size"], last_round["round_index"], last_round["buffer_folder_name"])
                buffer_sizes.append(last_result["buffer_size"])

            LOG.info("")
            LOG.info("📍 第五阶段：后处理")
            LOG.info("-" * 40)
            TimerUtils.start_stage("后处理节点")
            mod_export_path = GlobalConfig.path_generate_mod_folder()
            if GlobalConfig.logic_name == LogicName.NTEMI:
                from ..ui.ntmi_modimp.texture_slot_refresh import refresh_texture_slots_for_objects
                refresh_texture_slots_for_objects(
                    [
                        obj
                        for draw_call_model in getattr(blueprint_model, "ordered_draw_obj_data_model_list", []) or []
                        for obj in [bpy.data.objects.get(draw_call_model.get_blender_obj_name())]
                        if obj is not None and obj.type == "MESH"
                    ]
                )
            blueprint_model.execute_postprocess_nodes(mod_export_path)
            TimerUtils.end_stage("后处理节点")
            self._validate_generated_ini_variables(mod_export_path)
            export_success = True

        except Exception as e:
            error_message = f"导出失败: {str(e)}"
            print(f"❌ 导出过程中发生错误: {e}")
            LOG.exception(e)

            PreProcessHelper.cleanup_copies(silent=True)

        # 选择「否」时：导出完成后把备份的旧配置表覆盖回原位；「是」则保留新配置表
        if self.overwrite_config == 'NO':
            restore_config_tables(self._backup_entries)

        LOG.info("")
        LOG.info("📍 清理阶段")
        LOG.info("-" * 40)

        PreProcessHelper.cleanup_copies()

        if has_shapekey_export:
            BlueprintExportHelper.set_all_shapekey_values(0)
            LOG.info("   所有形态键已归零")

        LOG.info("")
        LOG.info("=" * 60)
        if export_success:
            total_rounds = len(export_plan) if export_plan else 1
            if has_shapekey_export and has_multi_file_export:
                LOG.info(f"✅ 形态键+多文件导出完成! 共导出 {total_rounds} 轮")
            elif has_shapekey_export:
                LOG.info(f"✅ 形态键导出完成! 共导出 {total_rounds} 轮")
            elif has_multi_file_export:
                LOG.info(f"✅ 多文件导出完成! 共导出 {total_rounds} 轮")
            else:
                LOG.info("✅ Mod生成完成!")
            LOG.info("=" * 60)
            self.report({'INFO'},TR.translate("Generate Mod Success!"))
            CommandUtils.OpenGeneratedModFolder()
        else:
            LOG.info(f"❌ Mod生成失败: {error_message}")
            LOG.info("=" * 60)
            self.report({'ERROR'}, error_message)

        TimerUtils.print_summary()

        LOG.stop_collecting()
        log_name = LOG.save_to_text_editor()
        if log_name:
            print(f"📄 导出日志已保存至文本编辑器: {log_name}")

        if export_success:
            return {'FINISHED'}
        return {'CANCELLED'}

    def _build_export_round_plan(self, has_shapekey_export: bool, max_shapekey_slot: int,
                                 has_multi_file_export: bool, max_multi_file_count: int) -> list:
        export_plan = []
        round_index = 0

        if has_shapekey_export:
            for slot_index in range(max_shapekey_slot + 1):
                round_index += 1
                if slot_index == 0:
                    export_plan.append({
                        "round_index": round_index,
                        "phase": "shapekey",
                        "description": "形态键基态",
                        "buffer_folder_name": "Meshes0000",
                        "export_index": 1,
                        "generate_ini": True,
                        "generate_classification_report": True,
                        "shapekey_mode": "all_zero",
                        "shapekey_slot_index": None,
                    })
                else:
                    export_plan.append({
                        "round_index": round_index,
                        "phase": "shapekey",
                        "description": f"形态键槽位 {slot_index}",
                        "buffer_folder_name": f"Meshes1{slot_index:03d}",
                        "export_index": 1,
                        "generate_ini": False,
                        "generate_classification_report": False,
                        "shapekey_mode": "slot",
                        "shapekey_slot_index": slot_index,
                    })

        if has_multi_file_export:
            for multi_index in range(1, max_multi_file_count + 1):
                round_index += 1
                export_plan.append({
                    "round_index": round_index,
                    "phase": "multifile",
                    "description": f"多文件索引 {multi_index}",
                    "buffer_folder_name": f"Meshes{multi_index:02d}",
                    "export_index": multi_index,
                    "generate_ini": (not has_shapekey_export and multi_index == 1),
                    "generate_classification_report": False,
                    "shapekey_mode": "all_zero" if has_shapekey_export else "unchanged",
                    "shapekey_slot_index": None,
                })

        if not export_plan:
            export_plan.append({
                "round_index": 1,
                "phase": "single",
                "description": "单次导出",
                "buffer_folder_name": "Meshes",
                "export_index": 1,
                "generate_ini": True,
                "generate_classification_report": False,
                "shapekey_mode": "unchanged",
                "shapekey_slot_index": None,
            })

        return export_plan

    def _validate_generated_ini_variables(self, mod_export_path):
        """导出收尾自检：产物 ini 里"被引用但未声明"的变量 + 重复全局声明。

        未声明的 ``$var`` 在 3DMigoto 里退化成段内局部变量（跨 run= 不传递），
        表现为功能静默失效；历史事故（viewport_valid、形态键 Freq_xxx 分叉）
        都属于这一类。这里只报告、不阻断导出。
        """
        try:
            workspace_name = str(GlobalConfig.get_workspace_name() or "")
        except Exception:
            workspace_name = ""
        # 延迟导入：自检是收尾增强，模块缺失（裁剪/测试桩环境）不得影响导出
        try:
            from ..common.ini_variable_validator import (
                find_primary_ini,
                format_findings,
                validate_ini_file,
            )
        except ImportError as exc:  # pragma: no cover - 仅裁剪/测试桩环境命中
            LOG.info(f"⚠️ [INI 校验] 跳过（校验模块不可用）: {exc}")
            return
        ini_path = find_primary_ini(mod_export_path, workspace_name)
        if not ini_path:
            return
        try:
            report = validate_ini_file(ini_path)
        except Exception as exc:
            LOG.info(f"⚠️ [INI 校验] 跳过（读取失败）: {exc}")
            return
        if report.get("missing"):
            return
        LOG.info("")
        LOG.info("🔎 产物变量声明自检")
        LOG.info("-" * 40)
        for line in format_findings(report):
            LOG.info(line)

    def _log_export_plan(self, export_plan: list):
        LOG.info("")
        LOG.info("🗂️ 导出轮次计划")
        LOG.info("-" * 40)
        for round_plan in export_plan:
            flags = []
            if round_plan.get("generate_ini"):
                flags.append("生成INI")
            if round_plan.get("generate_classification_report"):
                flags.append("分类报告")
            if not flags:
                flags.append("仅Buffer")
            LOG.info(
                f"   第 {round_plan['round_index']} 轮: {round_plan['description']} -> {round_plan['buffer_folder_name']} ({' / '.join(flags)})"
            )

    def _execute_main_round(self, tree, round_plan: dict) -> dict:
        LOG.info("")
        LOG.info(f"🔄 第 {round_plan['round_index']} 轮导出: {round_plan['description']}")
        LOG.info(f"   Buffer 文件夹: {round_plan['buffer_folder_name']}")

        result = ExportRoundExecutor.execute_round(
            tree=tree,
            round_plan=round_plan,
            allow_parallel_preprocess=True,
        )

        LOG.info(f"   Buffer 文件夹大小: {result['buffer_size']} 字节")
        return result

    def _validate_buffer_size(self, reference_size: int, current_size: int, round_index: int, buffer_folder_name: str):
        if reference_size != current_size:
            raise ParallelExportError(
                f"第 {round_index} 轮导出的 Buffer 文件夹 {buffer_folder_name} 大小 ({current_size} 字节) 与首轮 ({reference_size} 字节) 不一致"
            )

class SSMTQuickExportSelected(_BackgroundExportMixin, bpy.types.Operator):
    """快速局部导出选中的网格物体，无需复杂蓝图"""
    bl_idname = "ssmt.quick_export_selected"
    bl_label = TR.translate("快速局部导出")
    bl_description = "无视蓝图，直接导出当前选中的网格物体"
    bl_options = {'REGISTER', 'UNDO'}

    _NTEMI_QUICK_EXPORT_DISABLED_MESSAGE = (
        "NTEMI 已停用快速局部导出，请改用“NTMI ModImp Output”独立导出器。"
    )

    def invoke(self, context, event):
        """入口分流：勾选「后台导出」时起后台实例 + modal 轮询，否则走原有同步路径。"""
        if not GlobalProterties.is_background_export_enabled():
            return self.execute(context)

        # 防重入：已有后台导出在跑时不再起第二个后台实例
        if get_active_session() is not None:
            self.report({'WARNING'}, BACKGROUND_BUSY_MESSAGE)
            return {'CANCELLED'}

        selected_meshes, _skipped_count = self._collect_selected_meshes(context)
        if not selected_meshes:
            self.report({'WARNING'}, "请先选择至少一个网格物体")
            return {'CANCELLED'}

        GlobalConfig.read_from_main_json_ssmt4()
        # NTEMI 有独立导出器，快速导出不支持后台，回落到同步入口（与 Generate 一致）
        if GlobalConfig.logic_name == LogicName.NTEMI:
            return self.execute(context)

        # 立即反馈：会话启动里要先存工程快照（数秒）再起子进程，先把文本写上。
        push_progress_header("后台导出：正在保存工程快照…", context)
        try:
            session = self._create_quick_background_session(selected_meshes)
            started = session.start()
        except Exception as error:
            clear_progress_header(context)
            self.report({'ERROR'}, f"后台导出启动失败: {error}")
            return {'CANCELLED'}

        if not started:
            clear_progress_header(context)
            self.report({'ERROR'}, getattr(session, "error", "") or "后台导出启动失败")
            return {'CANCELLED'}

        self._bg_session = session
        self._bg_node_colors = {}
        # 新一次导出：高亮缓存（树 / 节点 / 参与节点）必须跟着会话重建
        self._bg_node_lookup = None
        self._bg_progress_started = False
        # 中键拖动平移状态：modal 只在中键按住期间放行鼠标移动。
        self._bg_view_nav = False
        self._bg_timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        # 快照已保存：后台实例要先加载工程快照（约 10 秒）才写出 stage.json。
        push_progress_header("后台导出：后台实例正在加载工程（约 10 秒）…", context)

        LOG.info("")
        LOG.info("📦 快速局部导出已在后台启动（主界面不阻塞）")
        LOG.info(f"   后台日志: {session.log_path}")
        return {'RUNNING_MODAL'}

    def _create_quick_background_session(self, selected_meshes):
        """按选中的网格准备后台会话（manifest 里带 quick_export / object_names）。"""
        object_names = [safe_object_name(obj) for obj in selected_meshes]
        object_names = [name for name in object_names if name]

        # 临时蓝图树只有 Object Info / Group / Output，不含后处理节点；
        # 步骤口径与 worker 用同一个计划函数：单轮 1 + 后处理节点 0 + 收尾 1。
        try:
            quick_plan = build_step_plan(
                None,
                direct_export=False,
                round_plans=[dict(QUICK_EXPORT_ROUND_PLAN)],
                quick_export=True,
            )
            total_steps = len(quick_plan) if quick_plan else 2
        except Exception:
            total_steps = 2

        return BackgroundExportSession(
            tree_name="",
            output_dir=GlobalConfig.path_generate_mod_folder(),
            round_plans=[dict(QUICK_EXPORT_ROUND_PLAN)],
            logic_name=GlobalConfig.logic_name,
            workspace_name=GlobalConfig.get_workspace_name(),
            direct_export=False,
            total_steps=total_steps,
            quick_export=True,
            object_names=object_names,
        )

    def execute(self, context):
        selected_meshes, skipped_count = self._collect_selected_meshes(context)
        if not selected_meshes:
            self.report({'WARNING'}, "请先选择至少一个网格物体")
            return {'CANCELLED'}

        GlobalConfig.read_from_main_json_ssmt4()
        if GlobalConfig.logic_name == LogicName.NTEMI:
            self.report({'ERROR'}, self._NTEMI_QUICK_EXPORT_DISABLED_MESSAGE)
            return {'CANCELLED'}

        temp_tree = None
        previous_runtime_tree_name = BlueprintExportHelper.runtime_blueprint_tree_name
        previous_export_index = BlueprintExportHelper.current_export_index
        previous_buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
        export_success = False

        try:
            LOG.start_collecting()
            TimerUtils.start_session("快速局部导出")
            GlobalKeyCountHelper.initialize()

            LOG.info("=" * 60)
            LOG.info("🚀 开始快速局部导出")
            LOG.info(f"📋 当前选择网格物体: {len(selected_meshes)} 个")
            if skipped_count:
                LOG.warning(f"   跳过 {skipped_count} 个已失效或非 Mesh 的选中对象")
            for obj in selected_meshes:
                LOG.info(f"   - {self._safe_object_name(obj)}")

            temp_tree = self._build_temp_blueprint_tree(selected_meshes)

            round_plan = dict(QUICK_EXPORT_ROUND_PLAN)

            result = ExportRoundExecutor.execute_round(
                tree=temp_tree,
                round_plan=round_plan,
                allow_parallel_preprocess=True,
            )

            export_success = True
            self.report({'INFO'}, f"快速局部导出完成，共导出 {result['object_count']} 个物体")
            CommandUtils.OpenGeneratedModFolder()
            return {'FINISHED'}
        except Exception as e:
            print(f"❌ 快速局部导出失败: {e}")
            LOG.exception(e)
            self.report({'ERROR'}, f"快速局部导出失败: {e}")
            return {'CANCELLED'}
        finally:
            BlueprintExportHelper.runtime_blueprint_tree_name = previous_runtime_tree_name
            BlueprintExportHelper.set_current_export_index(previous_export_index)
            BlueprintExportHelper.set_current_buffer_folder_name(previous_buffer_folder_name)

            if temp_tree:
                try:
                    temp_tree_name = temp_tree.name
                except (AttributeError, ReferenceError):
                    temp_tree_name = ""
                if temp_tree_name and bpy.data.node_groups.get(temp_tree_name):
                    bpy.data.node_groups.remove(temp_tree, do_unlink=True)

            TimerUtils.print_summary()

            LOG.stop_collecting()
            log_name = LOG.save_to_text_editor("快速局部导出日志")
            if log_name:
                print(f"📄 快速局部导出日志已保存至文本编辑器: {log_name}")

    @staticmethod
    def _safe_object_name(obj) -> str:
        return safe_object_name(obj)

    @classmethod
    def _collect_selected_meshes(cls, context):
        try:
            selected_objects = list(getattr(context, "selected_objects", []) or [])
        except (AttributeError, ReferenceError):
            selected_objects = []

        selected_meshes = []
        skipped_count = 0
        for obj in selected_objects:
            if obj is None:
                skipped_count += 1
                continue
            try:
                if obj.type != 'MESH':
                    skipped_count += 1
                    continue
                obj_name = obj.name
                obj.as_pointer()
            except (AttributeError, ReferenceError):
                skipped_count += 1
                continue
            if not obj_name:
                skipped_count += 1
                continue
            selected_meshes.append(obj)

        return selected_meshes, skipped_count

    def _build_temp_blueprint_tree(self, objects):
        """建临时蓝图树（实现抽到 ``export_background``，同步路径与后台 worker 共用）。"""
        return build_quick_export_temp_tree(objects)


def register():
    bpy.utils.register_class(SSMTQuickExportSelected)
    bpy.utils.register_class(SSMTGenerateModBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMTQuickExportSelected)
    bpy.utils.unregister_class(SSMTGenerateModBlueprint)
