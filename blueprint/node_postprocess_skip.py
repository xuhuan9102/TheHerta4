# -*- coding: utf-8 -*-
"""跳过部件后处理节点。

工作方式（与「条件判断」节点共用同一套结构化圈定机制）
------------------------------------------------------
1. 用 ``BlueprintExportHelper.get_current_blueprint_model()`` 拿到**本次导出**的
   蓝图模型；
2. 交给 ``node_postprocess_condition._collect_target_section_names`` 圈定
   「选中部件 -> draw_ib -> 导出期登记的段名」；
3. 把这些段从导出 ini 里**整段删除**（资源声明段 ``Resource*`` 与检测段
   ``TextureOverrideCheckHash*`` 已在圈定层排除，保持不动）；
4. 按 draw_ib 追加本节点自己的跳过段，并登记进导出段登记
   （``common/export_section_registry``）—— 因此链上后面的「条件判断」节点
   能圈到新段，把 ``handling = skip`` 包进 ``if``：条件不通过时跳过失效，
   效果等同删掉跳过段。

跳过段形状（每个 draw_ib 一段，hash 统一小写）::

    ; --- AUTO-APPENDED SKIP BLOCK ---
    ; skip <hash>
    [TextureOverride_IB_<hash>]
    hash = <hash>
    handling = skip

幂等：块由 ``; --- AUTO-APPENDED SKIP BLOCK ---`` 标记。执行时**先剥掉旧块再
删段**，避免第二次执行把旧跳过段当成目标段重复统计；处理完统一重新追加。
``node_postprocess_base.AUTO_APPENDED_SECTION_MARKER_PREFIXES`` 已登记该前缀，
其它后处理节点重写配置表时会把它当尾部内容原样保留。

为什么不再扫描 ini 关键词
--------------------------
段名里的 hash 可能是 VB hash / 贴图 hash（见 ``common/export_section_registry``
模块说明），按关键词反推必然漏段；只读导出期结构化登记才能「准确无漏」地
圈定某个 IB 的全部导出段。
"""

import bpy
import os
import re

from .node_postprocess_base import SSMTNode_PostProcess_Base


NODE_IDNAME = "SSMTNode_PostProcess_Skip"
NODE_LABEL = "跳过"

#: 本节点追加块的标记行；``node_postprocess_base`` 用它的前缀识别「自动追加尾段」。
SKIP_BLOCK_MARKER = "; --- AUTO-APPENDED SKIP BLOCK ---"
SKIP_BLOCK_MARKER_PREFIX = "; --- AUTO-APPENDED SKIP "

#: 段头解析口径（``[SectionName]``）。**条件判断节点直接复用本符号**，名字与
#: 语义不能变：它必须只匹配「整行就是一个段头」的行。
_SECTION_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s*$")

#: 导出时的运行时副本后缀（可重复、可带数字）。
_RUNTIME_SUFFIX_RE = re.compile(r"(_(?:copy|dup\d*|chain\d*|bpe\d*))+$", re.IGNORECASE)

#: 其它节点在配置表尾部追加块的通用标记前缀（用于把本节点块与它们分开）。
_AUTO_APPENDED_OTHER_PREFIX = "; --- AUTO-APPENDED "


# --------------------------------------------------------------------------- #
# 纯函数（不依赖 bpy，便于 headless 测试）
# --------------------------------------------------------------------------- #

def _strip_runtime_suffixes(name):
    """反复剥掉 ``_copy`` / ``_dup1`` / ``_chain2`` / ``_BPE`` 这类运行时后缀。"""
    current = str(name or "").strip()
    for _ in range(16):
        stripped = _RUNTIME_SUFFIX_RE.sub("", current)
        if stripped == current:
            break
        current = stripped
    return current


def _section_ranges(lines):
    """把行列表切成段，返回 ``[(段名, 段头索引, 段体起始, 段体结束)]``。

    口径与 ``node_postprocess_condition._find_sections`` 一致：结束索引独占，
    等于下一个段头索引或文件末尾；第一个段头之前的前导内容不属于任何段。
    """
    lines = list(lines or [])
    headers = []
    for index, raw_line in enumerate(lines):
        match = _SECTION_HEADER_RE.match(str(raw_line or "").strip())
        if match:
            headers.append((match.group(1).strip(), index))

    ranges = []
    for position, (name, header_index) in enumerate(headers):
        body_start = header_index + 1
        body_end = headers[position + 1][1] if position + 1 < len(headers) else len(lines)
        ranges.append((name, header_index, body_start, body_end))
    return ranges


def _delete_sections(content, section_names):
    """删除 ``content`` 中段名属于 ``section_names`` 的**整个段**。

    返回 ``(new_content, deleted_section_names)``：

    - 段名比较**忽略大小写**、必须整段名精确相等（不做前缀 / 关键词匹配）；
    - 只删真实存在于文本里的段；不存在的段名不报错、不计入结果；
    - 删除时顺带吃掉该段尾部的纯空行，避免删除后留下多余双空行；
    - 一个段都没删时原样返回 ``content``（保证「无匹配不写文件」）。
    """
    text = str(content or "")
    if not text:
        return text, []

    wanted = set()
    for name in section_names or ():
        key = str(name or "").strip().casefold()
        if key:
            wanted.add(key)
    if not wanted:
        return text, []

    lines = text.splitlines(keepends=True)
    drop_indexes = set()
    deleted_names = []
    for name, header_index, body_start, body_end in _section_ranges(lines):
        if name.casefold() not in wanted:
            continue
        end = body_end
        while end > body_start and not lines[end - 1].strip():
            # 段体末尾的空行一并删掉：否则删段后会在段与段之间堆积双空行。
            end -= 1
        drop_indexes.update(range(header_index, end))
        deleted_names.append(name)

    if not deleted_names:
        return text, []

    kept = "".join(line for index, line in enumerate(lines) if index not in drop_indexes)
    if not kept.strip():
        return "", deleted_names
    if not kept.endswith(("\n", "\r")):
        kept += "\r\n" if "\r\n" in text else "\n"
    return kept, deleted_names


def _skip_entries(draw_ibs):
    """把 draw_ib 集合整理成 ``[(原样 draw_ib, 小写 hash)]``（按输入顺序去重）。

    - 段名 / ``hash =`` 统一用小写（导出期 hash 是小写，例如
      ``TextureOverride_IB_92cb56c9``）；
    - 原样字符串用于 ``export_section_registry`` 登记：条件判断节点按导出期的
      ``match_draw_ib`` 原样查询，登记值必须与它逐字一致。
    """
    entries = []
    seen = set()
    for value in draw_ibs or ():
        raw = str(value or "").strip()
        if not raw:
            continue
        lowered = raw.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        entries.append((raw, lowered))
    return entries


def _strip_skip_block(content):
    """剥掉本节点上一次追加的整块（含标记行），返回剩下的正文。

    本节点的块永远在文件末尾；块后面若还有别的「自动追加尾段」（文本追加 /
    UI 面板等），从它们自己的标记行开始原样保留，避免连带删掉别的节点的内容。
    块后只有空行（或什么也没有）时，全部视为本块的一部分。
    """
    text = str(content or "")
    if not text:
        return ""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"

    start_index = None
    end_index = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start_index is None:
            if stripped.startswith(SKIP_BLOCK_MARKER_PREFIX):
                start_index = index
            continue
        if stripped.startswith(_AUTO_APPENDED_OTHER_PREFIX) and not stripped.startswith(SKIP_BLOCK_MARKER_PREFIX):
            end_index = index
            break

    if start_index is None:
        return text
    if end_index is None:
        end_index = len(lines)

    body = "".join(lines[:start_index]).rstrip("\r\n")
    tail = "".join(lines[end_index:]).lstrip("\r\n")
    parts = [part for part in (body, tail) if part]
    if not parts:
        return ""
    return f"{newline}{newline}".join(parts) + newline


def _build_skip_block(draw_ibs, newline="\n"):
    """把 draw_ib 集合组装成 SKIP 块文本（``draw_ibs`` 为空时返回 ""）。"""
    entries = _skip_entries(draw_ibs)
    if not entries:
        return ""

    lines = [SKIP_BLOCK_MARKER]
    for _raw, hash_val in entries:
        lines.append(f"; skip {hash_val}")
        lines.append(f"[TextureOverride_IB_{hash_val}]")
        lines.append(f"hash = {hash_val}")
        lines.append("handling = skip")
        lines.append("")  # 段之间的空行；块末尾同样以换行结束
    return newline.join(lines)


def _append_skip_block(content, draw_ibs):
    """把跳过块追加到文本末尾（**不剥旧块**；``draw_ibs`` 为空时原样返回）。"""
    text = str(content or "")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = _build_skip_block(draw_ibs, newline=newline)
    if not block:
        return text

    body = text.rstrip("\r\n")
    if not body:
        return block
    return f"{body}{newline}{newline}{block}"


def _apply_skip_block(content, draw_ibs):
    """剥掉旧块后把新块追加到末尾（幂等）；``draw_ibs`` 为空时只剥不追加。"""
    text = str(content or "")
    stripped = _strip_skip_block(text)
    if not _skip_entries(draw_ibs):
        return stripped
    return _append_skip_block(stripped, draw_ibs)


def _get_section_registry():
    """软导入「导出段登记」；不可用时返回 ``None``（不拖垮节点注册 / 导出）。"""
    try:
        from ..common import export_section_registry
    except Exception:
        return None
    return export_section_registry


# --------------------------------------------------------------------------- #
# 节点数据
# --------------------------------------------------------------------------- #

class SSMT_SkipObjectItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")  # type: ignore


class SSMT_UL_SKIP_OBJECTS(bpy.types.UIList):
    """已选跳过部件列表（行内 X 按钮按 index 删除对应项）。"""

    bl_idname = "SSMT_UL_SKIP_OBJECTS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=str(item.object_name or ""), icon='OBJECT_DATA')
        op_remove = row.operator("ssmt.skip_parts_remove", text="", icon='X')
        op_remove.node_name = getattr(data, "name", "")
        op_remove.index = index


class SSMTNode_PostProcess_Skip(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = NODE_LABEL
    bl_description = (
        "圈定选中部件所属 IB 的全部导出段并从 ini 删除，再追加 handling = skip 跳过段"
        "（跳过段已登记，可被后面的条件判断节点加条件）"
    )

    skip_objects: bpy.props.CollectionProperty(type=SSMT_SkipObjectItem)  # type: ignore
    active_skip_object: bpy.props.IntProperty(default=0)  # type: ignore

    def draw_buttons(self, context, layout):
        layout.label(text="删除该部件所属 IB 的全部导出段，并追加跳过段", icon='HIDE_ON')
        layout.label(text="与条件判断节点同一套 IB 圈定机制（不扫描 ini 关键词）", icon='INFO')

        box = layout.box()
        box.label(text=f"跳过部件 ({len(self.skip_objects)})", icon='OBJECT_DATA')
        if self.skip_objects:
            box.template_list(
                "SSMT_UL_SKIP_OBJECTS", "",
                self, "skip_objects",
                self, "active_skip_object",
                rows=max(2, min(len(self.skip_objects), 6)),
            )
        else:
            box.label(text="列表为空：导出时不会修改 ini", icon='ERROR')

        row = box.row(align=True)
        row.operator("ssmt.skip_parts_add_selected", text="添加选中物体", icon='ADD').node_name = self.name
        op_remove = row.operator("ssmt.skip_parts_remove", text="移除", icon='REMOVE')
        op_remove.node_name = self.name
        op_remove.index = int(self.active_skip_object)
        row.operator("ssmt.skip_parts_clear", text="清空", icon='TRASH').node_name = self.name

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def _selected_part_names(self):
        """收集节点上选定的部件名（去空、去重、保持顺序）。"""
        names = []
        seen = set()
        for item in self.skip_objects:
            name = str(getattr(item, "object_name", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names

    def _collect_targets(self, selected_names, target_ini):
        """结构化圈定：返回 ``(model, draw_ibs, target_names)``。

        与条件判断节点共用 ``_collect_target_section_names``；延迟 import 避免
        模块加载期的循环依赖（condition 已经 import 了本模块的两个符号）。
        任何一步不可用时返回 ``(model_or_None, set(), set())``，由调用方打印原因。
        """
        model = None
        try:
            from .export_helper import BlueprintExportHelper

            get_blueprint_model = getattr(BlueprintExportHelper, "get_current_blueprint_model", None)
            model = get_blueprint_model() if callable(get_blueprint_model) else None
        except Exception as exc:
            print(f"跳过部件：读取本次导出的蓝图模型失败（{exc}），跳过")
            return None, set(), set()

        if model is None:
            return None, set(), set()

        try:
            from .node_postprocess_condition import _collect_target_section_names
        except Exception as exc:
            print(f"跳过部件：无法加载条件判断节点的圈定实现（{exc}），跳过")
            return model, set(), set()

        draw_ibs, target_names = _collect_target_section_names(model, selected_names, target_ini)
        return model, draw_ibs, target_names

    def execute_postprocess(self, mod_export_path):
        from .node_postprocess_comment_cleanup import SSMTNode_PostProcess_CommentCleanup

        target_ini, error_message = SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(mod_export_path)
        if not target_ini:
            print(f"跳过部件：{error_message or '未找到目标配置表'}")
            return False

        selected_names = self._selected_part_names()
        if not selected_names:
            print("跳过部件：节点未选择任何部件，跳过")
            return True

        # ---- 结构化圈定（与条件判断节点同一套机制） ----
        model, draw_ibs, target_names = self._collect_targets(selected_names, target_ini)
        if model is None:
            print("跳过部件：未获取到本次导出的蓝图模型（导出期段登记不可用），跳过")
            return True
        if not draw_ibs:
            print("跳过部件：选定的部件未匹配到任何 DrawIB（物体名与导出对象不一致），跳过")
            return True
        if not target_names:
            print(
                f"跳过部件：导出期段登记里没有这些 DrawIB 的段（IB: {sorted(draw_ibs)}），跳过"
            )
            return True

        try:
            with open(target_ini, "rb") as handle:
                raw_bytes = handle.read()
        except OSError as exc:
            print(f"跳过部件：读取配置表失败 {target_ini}: {exc}")
            return False

        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        try:
            content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            print(f"跳过部件：配置表不是 UTF-8，跳过 {target_ini}: {exc}")
            return False

        # ---- 删除 + 追加 ----
        # 先剥掉本节点上一次追加的块：否则第二次执行会把旧跳过段当成目标段重复删 / 计数。
        stripped_content = _strip_skip_block(content)
        updated, deleted_names = _delete_sections(stripped_content, target_names)
        updated = _append_skip_block(updated, draw_ibs)

        written = updated != content
        if written:
            self._create_cumulative_backup(target_ini, mod_export_path)
            try:
                with open(
                    target_ini,
                    "w",
                    encoding="utf-8-sig" if has_bom else "utf-8",
                    newline="",
                ) as handle:
                    handle.write(updated)
            except (OSError, UnicodeError) as exc:
                print(f"跳过部件：写入配置表失败 {target_ini}: {exc}")
                return False

        # ---- 登记新段：让链上后面的条件判断节点能圈到跳过段并给它加条件 ----
        skip_entries = _skip_entries(draw_ibs)
        registry = _get_section_registry()
        if registry is not None:
            for raw_draw_ib, hash_val in skip_entries:
                try:
                    registry.register_section(
                        f"TextureOverride_IB_{hash_val}",
                        section_type="TextureOverrideIB",
                        draw_ib=raw_draw_ib,
                        ini_path=target_ini,
                    )
                except Exception as exc:
                    print(f"跳过部件：登记导出段失败（{exc}）")

        print(
            f"跳过部件：选中 {len(selected_names)} 个部件、圈定 IB {len(draw_ibs)} 个"
            f"（{', '.join(sorted(str(value) for value in draw_ibs))}）；"
            f"删除段 {len(deleted_names)} 个"
            f"{'（' + '、'.join(deleted_names) + '）' if deleted_names else ''}；"
            f"新增跳过段 {len(skip_entries)} 个"
            f"{'' if written else '（配置表已是最新，未改写）'} -> {target_ini}"
        )
        try:
            from . import export_progress
            export_progress.report_stage(
                f"跳过部件：{len(draw_ibs)} 个IB / 删除 {len(deleted_names)} 段 / "
                f"新增 {len(skip_entries)} 段 / {len(selected_names)} 个部件"
            )
        except Exception:
            pass
        return True


# --------------------------------------------------------------------------- #
# 算子：添加 / 移除 / 清空
# --------------------------------------------------------------------------- #

def _resolve_skip_node(context, node_name):
    space_data = getattr(context, "space_data", None)
    tree = None
    if space_data is not None:
        tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
    node = tree.nodes.get(node_name) if tree else None
    if node is None or getattr(node, "bl_idname", "") != NODE_IDNAME:
        return None
    return node


class SSMT_OT_PostProcess_AddSkipObject(bpy.types.Operator):
    bl_idname = "ssmt.skip_parts_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前选中的网格物体加入跳过部件列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        objects = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == 'MESH']
        if not objects and getattr(context, "active_object", None) is not None:
            active = context.active_object
            if getattr(active, "type", "") == 'MESH':
                objects = [active]
        if not objects:
            self.report({'WARNING'}, "请先选中要添加的网格物体")
            return {'CANCELLED'}

        existing = {str(item.object_name or "") for item in node.skip_objects}
        added_count = 0
        for obj in objects:
            name = str(getattr(obj, "name", "") or "").strip()
            if not name or name in existing:
                continue
            item = node.skip_objects.add()
            item.object_name = name
            existing.add(name)
            added_count += 1

        self.report({'INFO'}, f"已添加 {added_count} 个部件")
        return {'FINISHED'}


class SSMT_OT_PostProcess_RemoveSkipObject(bpy.types.Operator):
    bl_idname = "ssmt.skip_parts_remove"
    bl_label = "移除部件"
    bl_description = "从跳过部件列表中移除指定项"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        if 0 <= self.index < len(node.skip_objects):
            node.skip_objects.remove(self.index)
            node.active_skip_object = max(0, min(int(node.active_skip_object), len(node.skip_objects) - 1))
            return {'FINISHED'}

        self.report({'WARNING'}, "列表中没有这一项")
        return {'CANCELLED'}


class SSMT_OT_PostProcess_ClearSkipObjects(bpy.types.Operator):
    bl_idname = "ssmt.skip_parts_clear"
    bl_label = "清空"
    bl_description = "清空跳过部件列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        while len(node.skip_objects) > 0:
            node.skip_objects.remove(len(node.skip_objects) - 1)
        node.active_skip_object = 0
        self.report({'INFO'}, "已清空跳过部件列表")
        return {'FINISHED'}


classes = (
    SSMT_SkipObjectItem,
    SSMT_UL_SKIP_OBJECTS,
    SSMTNode_PostProcess_Skip,
    SSMT_OT_PostProcess_AddSkipObject,
    SSMT_OT_PostProcess_RemoveSkipObject,
    SSMT_OT_PostProcess_ClearSkipObjects,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
