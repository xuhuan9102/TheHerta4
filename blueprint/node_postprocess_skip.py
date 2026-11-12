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
4. 从**选中的物体名**解析绘制条目 ``(hash, indexCount, firstIndex)``，按条目追加
   本节点自己的跳过段，并登记进导出段登记
   （``common/export_section_registry``）—— 因此链上后面的「条件判断」节点
   能圈到新段，把 ``handling = skip`` 包进 ``if``：条件不通过时跳过失效，
   效果等同删掉跳过段。
5. 节点上的**手动哈希**区（英文输入框 ``Hash`` / ``IndexCount`` / ``FirstIndex``
   + 「添加到跳过部件」按钮）不依赖物体列表：条目**直接加入同一个「跳过部件」
   列表**（与选中物体共用列表、移除/清空），同样按条目追加跳过段并登记。
   它主要用来跳过**没有导入 Blender** 的 IB（见下节），与选中部件可以同时使用；
   旧工程里单独的 ``manual_hash_items`` 列表会在首次绘制/导出时自动并入。

无导出段的 IB（游戏原生绘制）
--------------------------------
若某个 draw_ib 在本次导出里**没有登记段**（该 IB 是游戏原生绘制、不在本 mod 的
接管范围里），节点**不再早退**：直接追加跳过段（完整三元组形式；解析不出三段
时是 ``[TextureOverride_IB_<hash>]`` + ``hash`` + ``handling = skip``）让原生绘制
被跳过（没有段可删，删除步骤为 0 段）。有导出段的 IB 仍走「先删段、再追加」，
删除范围**仍按 draw_ib 集合**（不变）。

手动哈希（没有导入 Blender 的 IB）
----------------------------------
「手动哈希」区有三行输入框：``Hash`` / ``IndexCount`` / ``FirstIndex``。填好后点
「添加到跳过部件」把这一条**直接加入上方的「跳过部件」列表**（行内 X 可删除，
也可以选中后点「移除」/「清空」）。它不依赖物体列表，用来给
**没有导入 Blender** 的绘制创建跳过——这类 IB 在本次导出里没有段，删除步骤自然
为 0，只追加 ``handling = skip`` 段：

- 只填 ``Hash``（后两行留空）：只 hash 跳过段，匹配该 IB 的全部绘制；
- 三行都填：完整三段跳过段（``match_first_index = FirstIndex``、
  ``match_index_count = IndexCount``）。

``Hash`` 是 8 位十六进制（大小写不限，可带 ``0x`` 前缀）；``IndexCount`` /
``FirstIndex`` 必须是非负整数（``IndexCount`` 还要大于 0），且**要么都填、要么
都留空**——只填一项或填非数字时会提示原因并拒绝。添加时按
``(hash, indexCount, firstIndex)`` 去重，重复条目静默忽略。

列表条目与选中物体**可以同时使用**：两边的条目按
``(hash, indexCount, firstIndex)`` 去重后统一追加进同一个 SKIP 块。

手动哈希在导出段登记里查得到段时（用户填的哈希**恰好**也属于本次导出的某个
IB），这些段仍按节点语义先删除、再追加跳过段，避免同一 hash 上同时存在正常
绘制段与跳过段；查不到（正常情况：该 IB 没有导入 Blender）时删除步骤为 0。
手动哈希不依赖 ``BlueprintExportHelper.get_current_blueprint_model()``：导出模型
不可用时选中部件不处理，但手动哈希的跳过段仍会照常追加。

跳过段形状（**优先用完整三元组**，来自物体名前缀或手动哈希，hash 统一小写）::

    ; --- AUTO-APPENDED SKIP BLOCK ---
    ; skip <hash>_<indexCount>_<firstIndex>
    [TextureOverride_IB_<hash>_<indexCount>_<firstIndex>]
    hash = <hash>
    match_first_index = <firstIndex>
    match_index_count = <indexCount>
    handling = skip

**为什么要三段**：只写 ``hash`` 的跳过段在部分游戏里不生效（同一 IB hash 被
多次绘制、或需要按 index 区间区分绘制批次），必须同时匹配 ``match_first_index``
与 ``match_index_count`` 才能稳定跳过。三元组来自物体名前缀
（``LOD0.999bff94-6462-17946.前发`` -> ``999bff94`` / ``6462`` / ``17946``）或
手动哈希列表（``Hash = 999bff94`` / ``IndexCount = 6462`` / ``FirstIndex = 17946``），
解析实现见 :func:`_parse_part_draw` / :func:`_normalize_manual_hash_item`。

物体名解析不出三段时（例如老工程里的 ``Body_copy``）或手动条目只填了 ``Hash``
时**回退为只 hash 的旧形式**::

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

def _log_info(message):
    """走 ``utils/log_utils`` 的 LOG；导入不可用（独立加载 / 测试）时退回 print。"""
    try:
        from ..utils.log_utils import LOG
    except Exception:
        print(message)
        return
    LOG.info(message)


def _log_warning(message):
    """warn 级别版本（中文文案与语义与原来的 print 一致）。"""
    try:
        from ..utils.log_utils import LOG
    except Exception:
        print(message)
        return
    LOG.warning(message)


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

#: 物体名前缀里的「三段绘制信息」：``<hash>-<indexCount>-<firstIndex>``。
#: 例：``LOD0.999bff94-6462-17946.前发`` -> ``999bff94`` / ``6462`` / ``17946``。
#: 后面允许跟分隔符（``.`` / ``_`` / ``-``）或字符串结束。
_PART_DRAW_RE = re.compile(r"^([0-9a-fA-F]{8})-(\d+)-(\d+)(?:[._\-]|$)")

#: 物体名可选的 ``LODx.`` 前缀（``LOD0.`` / ``LOD1.`` ...）。
_PART_LOD_PREFIX_RE = re.compile(r"^LOD\d+[.]", re.IGNORECASE)

#: 只 hash 的兜底解析口径（与 ``node_postprocess_condition._parse_hash_from_object_name``
#: 同口径）：先看开头 8 位十六进制，再找前后带分隔符的 8 位十六进制。
_PART_HASH_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{8}")
_PART_HASH_FALLBACK_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{8})(?=[-_.]|$)")

#: 手动哈希输入框里的合法哈希值：8 位十六进制（大小写不限，调用方先剥 ``0x``）。
_MANUAL_HASH_VALUE_RE = re.compile(r"^[0-9a-fA-F]{8}$")

#: 不参与跳过删除的段名前缀（小写比较）：资源声明段与条件判断节点的检测段
#: （与 ``node_postprocess_condition._EXCLUDED_SECTION_PREFIXES`` 同口径，改动要同步）。
_EXCLUDED_SECTION_PREFIXES = ("resource", "textureoverridecheckhash")


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


def _parse_part_draw(name):
    """从物体名解析 ``(hash, index_count, first_index)``；解析不出三段返回 ``None``。

    物体名格式（SSMT 导出期的部件名前缀）::

        [LODx.]<hash>-<indexCount>-<firstIndex>[.<部件名>[.<序号>]]

    - 去首尾空白；``LOD\\d+.`` 前缀可缺省；
    - 必须**三段齐全**（hash 8 位十六进制 + 两个非负整数）才算完整，否则 ``None``
      —— 缺段的条目由调用方回退成只 hash 的旧形式；
    - hash 统一小写，``index_count`` / ``first_index`` 转 ``int``。

    样例::

        >>> _parse_part_draw("LOD0.999bff94-6462-17946.前发")
        ('999bff94', 6462, 17946)
        >>> _parse_part_draw("LOD0.38b3bd13-4086-0.麻花辫.001")
        ('38b3bd13', 4086, 0)
        >>> _parse_part_draw("随便一个物体") is None
        True
    """
    value = str(name or "").strip()
    if not value:
        return None
    value = _PART_LOD_PREFIX_RE.sub("", value)
    match = _PART_DRAW_RE.match(value)
    if not match:
        return None
    try:
        return (match.group(1).lower(), int(match.group(2)), int(match.group(3)))
    except (TypeError, ValueError):
        return None


def _parse_part_hash(name):
    """只解析物体名里的 8 位 IB hash（三段解析失败时的兜底）；失败返回 ""。"""
    value = str(name or "").strip()
    if not value:
        return ""
    value = _PART_LOD_PREFIX_RE.sub("", value)
    match = _PART_HASH_PREFIX_RE.match(value)
    if match:
        return match.group(0).lower()
    fallback = _PART_HASH_FALLBACK_RE.search(value)
    return fallback.group(1).lower() if fallback else ""


def _normalize_manual_hash_item(hash_text, index_count_text="", first_index_text=""):
    """校验一条手动哈希输入，返回 ``(entry, error)``。

    成功时 ``entry`` 是 ``(hash, index_count|None, first_index|None)``（hash 统一
    小写；``index_count`` / ``first_index`` 为 ``None`` 表示只 hash 条目），``error``
    为空串；失败时 ``entry`` 为 ``None``，``error`` 是给用户看的中文原因。

    规则（与节点三行输入框一一对应）::

        Hash       8 位十六进制，大小写不限，可带 ``0x`` / ``0X`` 前缀
        IndexCount 可选；填了就必须是大于 0 的十进制整数
        FirstIndex 可选；填了就必须是非负十进制整数
        IndexCount / FirstIndex 要么都填、要么都留空（只填一项算无效条目）

    样例::

        >>> _normalize_manual_hash_item("0X7FBBCF0D")
        (('7fbbcf0d', None, None), '')
        >>> _normalize_manual_hash_item("999bff94", "1260", "9402")
        (('999bff94', 1260, 9402), '')
        >>> _normalize_manual_hash_item("999bff94", "1260")
        (None, 'IndexCount 与 FirstIndex 要么都填、要么都留空')
    """
    raw_hash = str(hash_text or "").strip()
    if not raw_hash:
        return None, "Hash 不能为空"
    if raw_hash[:2].lower() == "0x":
        raw_hash = raw_hash[2:].strip()
    if not _MANUAL_HASH_VALUE_RE.match(raw_hash):
        return None, "Hash 必须是 8 位十六进制"

    count_text = str(index_count_text or "").strip()
    first_text = str(first_index_text or "").strip()
    if not count_text and not first_text:
        return (raw_hash.lower(), None, None), ""
    if not count_text or not first_text:
        return None, "IndexCount 与 FirstIndex 要么都填、要么都留空"
    if not count_text.isdigit() or not first_text.isdigit():
        return None, "IndexCount 与 FirstIndex 必须是非负十进制整数"
    index_count = int(count_text)
    first_index = int(first_text)
    if index_count <= 0:
        return None, "IndexCount 必须大于 0"
    return (raw_hash.lower(), index_count, first_index), ""


def _collect_manual_hash_entries(items):
    """把节点上的手动哈希列表整理成跳过条目，返回 ``(entries, invalid)``。

    ``entries`` 为去重后的 ``[(hash, index_count|None, first_index|None)]``（保序）；
    ``invalid`` 为 ``[(位置, 原始哈希文本, 原因)]``（位置从 1 开始），由调用方提示
    用户——不静默吞掉，也不制造垃圾段。
    """
    entries = []
    invalid = []
    seen = set()
    for position, item in enumerate(items or (), start=1):
        entry, error = _normalize_manual_hash_item(
            getattr(item, "hash", ""),
            getattr(item, "index_count", ""),
            getattr(item, "first_index", ""),
        )
        if entry is None:
            invalid.append(
                (position, str(getattr(item, "hash", "") or "").strip(), error)
            )
            continue
        if entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    return entries, invalid


def _manual_hash_item_label(item):
    """列表行的显示文本：``hash`` / ``hash_count_first``；无效条目带原因。"""
    entry, error = _normalize_manual_hash_item(
        getattr(item, "hash", ""),
        getattr(item, "index_count", ""),
        getattr(item, "first_index", ""),
    )
    if entry is None:
        raw_hash = str(getattr(item, "hash", "") or "").strip()
        return f"{raw_hash or '<空>'}（无效：{error}）"
    return _format_skip_entry(entry)


def _add_manual_hash_item(node, hash_text, index_count_text="", first_index_text=""):
    """把一条手动哈希输入追加到节点的 ``manual_hash_items`` 列表。

    返回 ``(ok, message)``：``ok`` 为真时 ``message`` 是格式化后的条目写法；否则
    是拒绝原因（输入不合法 / 与已有条目重复）。列表非法 / 重复时保持不变。
    """
    entry, error = _normalize_manual_hash_item(
        hash_text, index_count_text, first_index_text
    )
    if entry is None:
        return False, error

    existing_entries, _invalid = _collect_manual_hash_entries(
        getattr(node, "manual_hash_items", None)
    )
    if entry in existing_entries:
        return False, f"条目已存在（{_format_skip_entry(entry)}）"

    item = node.manual_hash_items.add()
    item.hash = entry[0]
    item.index_count = "" if entry[1] is None else str(entry[1])
    item.first_index = "" if entry[2] is None else str(entry[2])
    return True, _format_skip_entry(entry)


def _is_manual_skip_item(item):
    """统一的「跳过部件」列表项是否是手动哈希条目。"""
    return bool(getattr(item, "is_manual", False)) or bool(
        str(getattr(item, "manual_hash", "") or "").strip()
    )


def _normalize_manual_skip_item(item):
    """校验统一列表里的手动哈希条目，返回 ``(entry, error)``（与旧列表同口径）。"""
    return _normalize_manual_hash_item(
        getattr(item, "manual_hash", ""),
        getattr(item, "manual_index_count", ""),
        getattr(item, "manual_first_index", ""),
    )


def _manual_skip_item_label(item):
    """统一列表行的手动条目显示文本。"""
    entry, error = _normalize_manual_skip_item(item)
    if entry is None:
        raw_hash = str(getattr(item, "manual_hash", "") or "").strip()
        return f"手动哈希 {raw_hash or '<空>'}（无效：{error}）"
    return f"手动哈希 {_format_skip_entry(entry)}"


def _add_manual_skip_item(node, hash_text, index_count_text="", first_index_text=""):
    """把手动哈希条目加入节点统一的 ``skip_objects`` 列表（与选中部件同一列表）。"""
    entry, error = _normalize_manual_hash_item(
        hash_text, index_count_text, first_index_text
    )
    if entry is None:
        return False, error

    # 点击「添加」是安全上下文：先把旧列表并入统一列表，再去重/追加。
    try:
        _migrate_legacy_manual_hashes(node)
    except Exception:
        pass

    existing_entries, _invalid = _collect_node_manual_entries(node)
    if entry in existing_entries:
        # 重复点击不再弹警告（用户反馈：正常重复添加也应静默处理）。
        return True, f"条目已存在，未重复添加（{_format_skip_entry(entry)}）"

    collection = getattr(node, "skip_objects", None)
    add = getattr(collection, "add", None)
    if not callable(add):
        # 兼容测试用的假节点：退回旧列表。
        return _add_manual_hash_item(node, hash_text, index_count_text, first_index_text)

    item = collection.add()
    item.is_manual = True
    item.manual_hash = entry[0]
    item.manual_index_count = "" if entry[1] is None else str(entry[1])
    item.manual_first_index = "" if entry[2] is None else str(entry[2])
    return True, _format_skip_entry(entry)


def _migrate_legacy_manual_hashes(node):
    """把旧版 ``manual_hash_items`` 列表原地并入统一的 ``skip_objects`` 列表。

    兼容旧工程：新版 UI 只在统一列表里显示手动哈希，旧数据第一次绘制/导出时迁移。
    返回迁移条数。
    """
    legacy = getattr(node, "manual_hash_items", None)
    target = getattr(node, "skip_objects", None)
    if legacy is None or target is None:
        return 0
    if not hasattr(target, "add") or not hasattr(legacy, "remove"):
        return 0
    moved = 0
    while len(legacy) > 0:
        item = legacy[0]
        new_item = target.add()
        new_item.is_manual = True
        new_item.manual_hash = str(getattr(item, "hash", "") or "").strip()
        new_item.manual_index_count = str(getattr(item, "index_count", "") or "").strip()
        new_item.manual_first_index = str(getattr(item, "first_index", "") or "").strip()
        try:
            legacy.remove(0)  # bpy CollectionProperty：按索引删除
        except (TypeError, ValueError):
            del legacy[0]  # 测试用 list：按索引切片删除
        moved += 1
    return moved


def _collect_node_manual_entries(node):
    """收集节点上所有手动哈希条目（统一列表 + 未迁移的旧列表），去重保序。"""
    entries = []
    invalid = []
    seen = set()
    position = 0
    for item in getattr(node, "skip_objects", []) or []:
        if not _is_manual_skip_item(item):
            continue
        position += 1
        entry, error = _normalize_manual_skip_item(item)
        if entry is None:
            invalid.append(
                (position, str(getattr(item, "manual_hash", "") or "").strip(), error)
            )
            continue
        if entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)

    legacy_entries, legacy_invalid = _collect_manual_hash_entries(
        getattr(node, "manual_hash_items", None)
    )
    for entry in legacy_entries:
        if entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    invalid.extend(legacy_invalid)
    return entries, invalid


def _normalize_skip_entries(entries):
    """把条目整理成 ``[(hash, index_count|None, first_index|None)]``（去重、保序）。

    接受三种写法：``(hash, count, first)`` 三元组（完整段）、``(hash,)`` 单元素、
    以及裸字符串 / ``None``（``index_count`` / ``first_index`` 为 ``None`` 表示
    「只有 hash」的回退条目）。去重键 = ``(hash, count, first)``。
    """
    result = []
    seen = set()
    for item in entries or ():
        if item is None:
            continue
        if isinstance(item, str):
            hash_val, index_count, first_index = item, None, None
        else:
            try:
                values = tuple(item)
            except TypeError:
                continue
            if not values:
                continue
            hash_val = values[0]
            index_count = values[1] if len(values) > 1 else None
            first_index = values[2] if len(values) > 2 else None

        hash_text = str(hash_val or "").strip().lower()
        if not hash_text:
            continue
        try:
            count_value = int(index_count) if index_count is not None else None
            first_value = int(first_index) if first_index is not None else None
        except (TypeError, ValueError):
            count_value = None
            first_value = None
        if count_value is None or first_value is None:
            count_value = None
            first_value = None

        key = (hash_text, count_value, first_value)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _entries_from_names(names):
    """从选中物体名解析跳过条目；三段解析失败时回退为只 hash 条目。

    - 三段齐全的物体 -> ``(hash, index_count, first_index)`` 完整段；
    - 只解析得出 hash 的物体 -> ``(hash, None, None)`` 回退段；
    - 解析不出任何 hash 的名字**不产生条目**（不制造垃圾段）；
    - 若同一个 hash 已经有完整段（另一个物体解析出了三段），**丢弃该 hash 的
      回退段**：只 hash 的段会匹配该 IB 的全部绘制、把精确段的范围覆盖掉。

    顺序与去重：按输入顺序、以 ``(hash, index_count, first_index)`` 去重。
    """
    prepared = []  # [(entry_or_None, hash_or_None)]，保持输入顺序
    full_hashes = set()
    for name in names or ():
        parsed = _parse_part_draw(name)
        if parsed is not None:
            prepared.append(parsed)
            full_hashes.add(parsed[0])
        else:
            prepared.append(_parse_part_hash(name) or None)

    entries = []
    seen = set()
    for item in prepared:
        if item is None:
            continue
        if isinstance(item, str):
            if item in full_hashes:
                continue
            entry = (item, None, None)
        else:
            entry = item
        if entry in seen:
            continue
        seen.add(entry)
        entries.append(entry)
    return entries


def _hash_only_entries(values):
    """把一组 hash（通常是 draw_ib）整理成只 hash 的条目列表（去重、保序）。"""
    entries = []
    seen = set()
    for value in values or ():
        hash_val = str(value or "").strip().lower()
        if not hash_val or hash_val in seen:
            continue
        seen.add(hash_val)
        entries.append((hash_val, None, None))
    return entries


def _skip_section_name(entry):
    """条目对应的段名：三段齐全用完整名，否则退回 ``TextureOverride_IB_<hash>``。"""
    hash_val, index_count, first_index = entry
    if index_count is None or first_index is None:
        return f"TextureOverride_IB_{hash_val}"
    return f"TextureOverride_IB_{hash_val}_{index_count}_{first_index}"


def _format_skip_entry(entry):
    """条目的日志写法：``hash_count_first`` 或 ``hash``。"""
    hash_val, index_count, first_index = entry
    if index_count is None or first_index is None:
        return str(hash_val)
    return f"{hash_val}_{index_count}_{first_index}"


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


def _delete_sections(content, section_names, protected=None):
    """删除 ``content`` 中段名属于 ``section_names`` 的**整个段**。

    ``protected`` 是 :func:``_collect_cross_ib_shared_names`` 的结果：命中的段名
    **仍被其它 draw_ib 登记**（跨 IB 共用），一律保留不删，避免删掉别的 IB 依赖
    的 ``CrossIBPresent`` / ``CrossIBShaderOverride`` / ``CrossIBTextureOverride``。

    返回 ``(new_content, deleted_section_names, preserved_names)``：

    - 段名比较**忽略大小写**、必须整段名精确相等（不做前缀 / 关键词匹配）；
    - 只删真实存在于文本里的段；不存在的段名不报错、不计入结果；
    - 删除时顺带吃掉该段尾部的纯空行，避免删除后留下多余双空行；
    - 一个段都没删时原样返回 ``content``（保证「无匹配不写文件」）。
    """
    text = str(content or "")
    if not text:
        return text, [], []

    wanted = set()
    for name in section_names or ():
        key = str(name or "").strip().casefold()
        if key:
            wanted.add(key)
    if not wanted:
        return text, [], []

    shared = protected or {}
    lines = text.splitlines(keepends=True)
    drop_indexes = set()
    deleted_names = []
    preserved_names = []
    for name, header_index, body_start, body_end in _section_ranges(lines):
        key = name.casefold()
        if key not in wanted:
            continue
        if key in shared:
            # 同名段仍被删除范围之外的 draw_ib 登记（跨 IB 共用）：保留。
            if name not in preserved_names:
                preserved_names.append(name)
            continue
        end = body_end
        while end > body_start and not lines[end - 1].strip():
            # 段体末尾的空行一并删掉：否则删段后会在段与段之间堆积双空行。
            end -= 1
        drop_indexes.update(range(header_index, end))
        deleted_names.append(name)

    if not deleted_names:
        return text, [], preserved_names

    kept = "".join(line for index, line in enumerate(lines) if index not in drop_indexes)
    if not kept.strip():
        return "", deleted_names, preserved_names
    if not kept.endswith(("\n", "\r")):
        kept += "\r\n" if "\r\n" in text else "\n"
    return kept, deleted_names, preserved_names


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


def _build_skip_block(entries, newline="\n"):
    """把跳过条目组装成 SKIP 块文本（没有有效条目时返回 ""）。

    ``entries`` 为 ``[(hash, index_count, first_index)]``；``index_count`` /
    ``first_index`` 为 ``None`` 的条目是「只 hash」回退段，仍用旧形式。
    """
    normalized = _normalize_skip_entries(entries)
    if not normalized:
        return ""

    lines = [SKIP_BLOCK_MARKER]
    for hash_val, index_count, first_index in normalized:
        if index_count is None or first_index is None:
            lines.append(f"; skip {hash_val}")
            lines.append(f"[TextureOverride_IB_{hash_val}]")
            lines.append(f"hash = {hash_val}")
            lines.append("handling = skip")
        else:
            lines.append(f"; skip {hash_val}_{index_count}_{first_index}")
            lines.append(f"[TextureOverride_IB_{hash_val}_{index_count}_{first_index}]")
            lines.append(f"hash = {hash_val}")
            lines.append(f"match_first_index = {first_index}")
            lines.append(f"match_index_count = {index_count}")
            lines.append("handling = skip")
        lines.append("")  # 段之间的空行；块末尾同样以换行结束
    return newline.join(lines)


def _append_skip_block(content, entries):
    """把跳过块追加到文本末尾（**不剥旧块**；没有条目时原样返回）。"""
    text = str(content or "")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = _build_skip_block(entries, newline=newline)
    if not block:
        return text

    body = text.rstrip("\r\n")
    if not body:
        return block
    return f"{body}{newline}{newline}{block}"


def _apply_skip_block(content, entries):
    """剥掉旧块后把新块追加到末尾（幂等）；没有条目时只剥不追加。"""
    text = str(content or "")
    stripped = _strip_skip_block(text)
    if not _normalize_skip_entries(entries):
        return stripped
    return _append_skip_block(stripped, entries)


def _get_section_registry():
    """软导入「导出段登记」；不可用时返回 ``None``（不拖垮节点注册 / 导出）。"""
    try:
        from ..common import export_section_registry
    except Exception:
        return None
    return export_section_registry

#: 「跨 IB 共用」段类型（``common/m_ini_builder.M_SectionType`` 里同名字段的
#: 小写形式）：这几种段在设计上就是被多个 draw_ib 共享的，删同名段时必须先确认
#: 归属，否则删掉当前 IB 的那一份会连带毁掉别的 IB 的绘制。
_CROSS_IB_SECTION_TYPES = (
    "crossibpresent",
    "crossibshaderoverride",
    "crossibtextureoverride",
)

#: 登记表不可用时的退化说明（写进摘要，避免用户以为跨 IB 保护生效了）。
_REGISTRY_UNAVAILABLE_NOTE = (
    "导出段登记表不可用 -> 删除范围退化为「按段名全文删除」，跨 IB 共用段可能被误删"
)


def _collect_cross_ib_shared_names(section_names, keep_draw_ibs):
    """查出「同名段是否仍被本次删除范围之外的 draw_ib 登记」。

    返回 ``{段名casefold: 记录}``，记录形如
    ``{"name": 原始段名, "owners": [其它 draw_ib...], "unowned_cross_ib": bool}``：

    - ``owners`` 非空：这段仍被其它 draw_ib 登记（跨 IB 共用），保留；
    - ``unowned_cross_ib`` 为真：段类型是 ``CrossIB*`` 但登记里没有归属 IB，
      保守保留（宁可少删，不可误删）。

    登记表不可用时返回 ``None``（调用方据此退化成「按段名删除」的旧行为，
    并在摘要里说明这一退化）。``keep_draw_ibs`` 为本次选中的 draw_ib 集合。
    """
    wanted = {
        str(name or "").strip().casefold()
        for name in section_names or ()
        if str(name or "").strip()
    }
    if not wanted:
        return {}
    registry = _get_section_registry()
    if registry is None:
        return None
    try:
        tables = registry.get_all() or {}
    except Exception:
        return None
    if not isinstance(tables, dict):
        return None

    keep = {
        str(value or "").strip().casefold()
        for value in keep_draw_ibs or ()
        if str(value or "").strip()
    }
    shared = {}
    for entries in tables.values():
        for entry in entries or ():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            key = name.casefold()
            if not key or key not in wanted or key in shared:
                continue
            owner = str(entry.get("draw_ib") or "").strip()
            if owner and owner.casefold() in keep:
                # 属于本次要删的 IB：可以删（同名段如果还有别的登记，后面的
                # 条目会被这一轮的循环继续检查）。
                continue
            if owner:
                shared[key] = {
                    "name": name,
                    "owners": [owner],
                    "unowned_cross_ib": False,
                }
            elif str(entry.get("type") or "").strip().casefold() in _CROSS_IB_SECTION_TYPES:
                shared[key] = {"name": name, "owners": [], "unowned_cross_ib": True}
    return shared


def _describe_cross_ib_shared(shared):
    """把跨 IB 共用段记录压成一行可读提示；没有记录时返回空串。"""
    if not shared:
        return ""
    parts = []
    for key in sorted(shared):
        info = shared[key] or {}
        name = str(info.get("name") or key)
        owners = [str(owner) for owner in (info.get("owners") or []) if str(owner)]
        if owners:
            parts.append(f"{name}（仍被 {'、'.join(owners)} 登记）")
        else:
            parts.append(f"{name}（跨 IB 共用段，登记里没有归属 IB）")
    return "、".join(parts)


def _collect_manual_target_section_names(manual_hashes, ini_path):
    """按手动哈希反查它们在本次导出里登记过的段名，返回 ``set`` 或 ``None``。

    手动哈希的用途是跳过**没有导入 Blender 的 IB**：这类 IB 在本次导出里没有段，
    查不到时返回空集，删除步骤自然为 0。若用户填的哈希**恰好**也属于本次导出
    （例如同一 IB 的其它部件没有加进跳过列表），查到的段仍按节点语义先删除再
    追加跳过段，避免同一 hash 上同时存在正常绘制段与跳过段。

    与条件判断节点同口径：只读导出期段登记，排除 ``Resource*`` 资源声明段与
    ``TextureOverrideCheckHash*`` 检测段。登记表不可用 / 查询异常时返回 ``None``
    （调用方按「无段可删」处理）。
    """
    hashes = []
    seen = set()
    for value in manual_hashes or ():
        hash_val = str(value or "").strip().lower()
        if not hash_val or hash_val in seen:
            continue
        seen.add(hash_val)
        hashes.append(hash_val)
    if not hashes:
        return set()

    registry = _get_section_registry()
    if registry is None:
        return None

    try:
        ini_abs = os.path.abspath(ini_path) if ini_path else ""
    except (OSError, ValueError):
        ini_abs = str(ini_path or "")

    try:
        entries = registry.get_sections_for_draw_ibs(hashes, ini_abs) or []
    except Exception as exc:  # noqa: BLE001 - 登记表查询失败只提示，不阻断导出
        _log_warning(f"跳过部件：按手动哈希查询导出段失败（{exc}）")
        return None

    names = set()
    for entry in entries:
        name = str((entry or {}).get("name") or "").strip()
        if not name:
            continue
        if name.casefold().startswith(_EXCLUDED_SECTION_PREFIXES):
            continue
        names.add(name)
    return names


def _find_sibling_ini_sections(target_ini, section_names):
    """在目标 ini **同目录的其它配置表**里找同名段，返回 ``[(文件名, [段名...])]``。

    只用于提示：multifile 分文件里有同名段时，本节点只会改写 ``target_ini``，
    分文件保持原样（它有自己的导出与后处理链），结果交给用户判断。
    """
    wanted = {
        str(name or "").strip().casefold()
        for name in section_names or ()
        if str(name or "").strip()
    }
    if not wanted or not target_ini:
        return []
    try:
        target_abs = os.path.abspath(target_ini)
        directory = os.path.dirname(target_abs)
        filenames = sorted(os.listdir(directory))
    except (OSError, ValueError):
        return []

    hints = []
    for filename in filenames:
        if not str(filename).casefold().endswith(".ini"):
            continue
        path = os.path.join(directory, filename)
        try:
            if os.path.abspath(path) == target_abs or not os.path.isfile(path):
                continue
            with open(path, "rb") as handle:
                text = handle.read().decode("utf-8-sig", errors="replace")
        except (OSError, ValueError):
            continue
        found = []
        for raw_line in text.splitlines():
            header = _SECTION_HEADER_RE.match(str(raw_line or "").strip())
            if not header:
                continue
            name = header.group(1).strip()
            if name.casefold() in wanted and name not in found:
                found.append(name)
        if found:
            hints.append((str(filename), found))
    return hints


# --------------------------------------------------------------------------- #
# 节点数据
# --------------------------------------------------------------------------- #

class SSMT_SkipObjectItem(bpy.types.PropertyGroup):
    """「跳过部件」列表的一条：选中物体，或手动输入的 IB 哈希。

    两种条目共用一个列表（按用户要求）：``is_manual`` 为真 / ``manual_hash``
    非空时按手动哈希条目处理，``object_name`` 条按选中物体处理。
    """

    object_name: bpy.props.StringProperty(name="物体名称", default="")  # type: ignore
    is_manual: bpy.props.BoolProperty(name="手动哈希条目", default=False)  # type: ignore
    manual_hash: bpy.props.StringProperty(name="手动 Hash", default="")  # type: ignore
    manual_index_count: bpy.props.StringProperty(name="手动 IndexCount", default="")  # type: ignore
    manual_first_index: bpy.props.StringProperty(name="手动 FirstIndex", default="")  # type: ignore


class SSMT_UL_SKIP_OBJECTS(bpy.types.UIList):
    """统一跳过列表：选中部件 + 手动哈希（行内 X 按钮按 index 删除对应项）。"""

    bl_idname = "SSMT_UL_SKIP_OBJECTS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        if _is_manual_skip_item(item):
            entry, _error = _normalize_manual_skip_item(item)
            # 注意：'HASH' 不是 Blender 5.2 的合法图标枚举（会导致列表绘制报错），
            # 手动条目统一用 'TEXT' / 'ERROR'。
            row.label(text=_manual_skip_item_label(item), icon='TEXT' if entry else 'ERROR')
        else:
            row.label(text=str(item.object_name or ""), icon='OBJECT_DATA')
        op_remove = row.operator("ssmt.skip_parts_remove", text="", icon='X')
        op_remove.node_name = getattr(data, "name", "")
        op_remove.index = index


class SSMT_ManualHashItem(bpy.types.PropertyGroup):
    """旧版手动哈希列表的一条（仅用于迁移到统一的「跳过部件」列表）。"""

    hash: bpy.props.StringProperty(name="Hash", default="")  # type: ignore
    index_count: bpy.props.StringProperty(name="IndexCount", default="")  # type: ignore
    first_index: bpy.props.StringProperty(name="FirstIndex", default="")  # type: ignore


class SSMT_UL_MANUAL_HASHES(bpy.types.UIList):
    """旧版手动哈希列表（保留注册以兼容旧数据，不再由节点 UI 使用）。"""

    bl_idname = "SSMT_UL_MANUAL_HASHES"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        entry, error = _normalize_manual_hash_item(
            getattr(item, "hash", ""),
            getattr(item, "index_count", ""),
            getattr(item, "first_index", ""),
        )
        row = layout.row(align=True)
        row.label(text=_manual_hash_item_label(item), icon='TEXT' if entry else 'ERROR')
        op_remove = row.operator("ssmt.skip_manual_hash_remove", text="", icon='X')
        op_remove.node_name = getattr(data, "name", "")
        op_remove.index = index


class SSMTNode_PostProcess_Skip(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = NODE_LABEL
    bl_description = (
        "圈定选中部件所属 IB 的全部导出段并从 ini 删除，再追加 handling = skip 跳过段"
        "（跳过段已登记，可被后面的条件判断节点加条件）；"
        "「手动哈希」可直接跳过没有导入 Blender 的 IB"
    )

    skip_objects: bpy.props.CollectionProperty(type=SSMT_SkipObjectItem)  # type: ignore
    active_skip_object: bpy.props.IntProperty(default=0)  # type: ignore

    manual_hash_items: bpy.props.CollectionProperty(type=SSMT_ManualHashItem)  # type: ignore
    active_manual_hash: bpy.props.IntProperty(default=0)  # type: ignore
    temp_manual_hash: bpy.props.StringProperty(  # type: ignore
        name="Hash",
        description="要跳过但没有导入 Blender 的 IB 哈希（8 位十六进制）",
        default="",
    )
    temp_index_count: bpy.props.StringProperty(  # type: ignore
        name="IndexCount",
        description="可选：绘制区间的索引数量；留空表示匹配该 IB 的全部绘制",
        default="",
    )
    temp_first_index: bpy.props.StringProperty(  # type: ignore
        name="FirstIndex",
        description="可选：绘制区间的起始索引；与 IndexCount 要么都填、要么都留空",
        default="",
    )

    def draw_buttons(self, context, layout):
        layout.label(text="删除 IB hash 所属的全部导出段，并追加跳过段", icon='HIDE_ON')

        # 注意：不能在 draw 阶段迁移旧列表（Blender 不允许在绘制时改数据）。
        # 这里只展示旧条目；真正的迁移在点击按钮 / 导出时做。
        legacy_items = list(getattr(self, "manual_hash_items", []) or [])
        total_count = len(self.skip_objects) + len(legacy_items)

        box = layout.box()
        box.label(text=f"跳过部件 ({total_count})", icon='OBJECT_DATA')
        if self.skip_objects:
            box.template_list(
                "SSMT_UL_SKIP_OBJECTS", "",
                self, "skip_objects",
                self, "active_skip_object",
                rows=max(2, min(len(self.skip_objects), 8)),
            )
        # 旧工程里未迁移的手动哈希：在同一个列表区域里显示，点击任意按钮后并入上面。
        for index, item in enumerate(legacy_items):
            entry, _error = _normalize_manual_hash_item(
                getattr(item, "hash", ""),
                getattr(item, "index_count", ""),
                getattr(item, "first_index", ""),
            )
            row = box.row(align=True)
            row.label(text=_manual_hash_item_label(item), icon='TEXT' if entry else 'ERROR')
            op_remove = row.operator("ssmt.skip_manual_hash_remove", text="", icon='X')
            op_remove.node_name = self.name
            op_remove.index = index

        row = box.row(align=True)
        row.operator("ssmt.skip_parts_add_selected", text="添加选中物体", icon='ADD').node_name = self.name
        op_remove = row.operator("ssmt.skip_parts_remove", text="移除", icon='REMOVE')
        op_remove.node_name = self.name
        op_remove.index = int(self.active_skip_object)
        row.operator("ssmt.skip_parts_clear", text="清空", icon='TRASH').node_name = self.name

        # 手动哈希不再单独成列表：填好后直接加入上面的「跳过部件」列表。
        box.separator()
        manual_col = box.column(align=True)
        manual_col.label(text="手动哈希（直接加入上方列表，跳过没导入 Blender 的 IB）", icon='TEXT')
        manual_col.prop(self, "temp_manual_hash", text="Hash")
        manual_col.prop(self, "temp_index_count", text="IndexCount")
        manual_col.prop(self, "temp_first_index", text="FirstIndex")
        box.operator(
            "ssmt.skip_manual_hash_add", text="添加到跳过部件", icon='ADD'
        ).node_name = self.name

        invalid_manual_items = _collect_node_manual_entries(self)[1]
        if invalid_manual_items:
            box.label(
                text=f"{len(invalid_manual_items)} 条手动哈希无效（导出时忽略）",
                icon='ERROR',
            )

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def _selected_part_names(self):
        """收集节点上选定的部件名（去空、去重、保持顺序；跳过手动哈希条目）。"""
        names = []
        seen = set()
        for item in self.skip_objects:
            if _is_manual_skip_item(item):
                continue
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
            _log_warning(f"跳过部件：读取本次导出的蓝图模型失败（{exc}），跳过")
            return None, set(), set()

        if model is None:
            return None, set(), set()

        try:
            from .node_postprocess_condition import _collect_target_section_names
        except Exception as exc:
            _log_warning(f"跳过部件：无法加载条件判断节点的圈定实现（{exc}），跳过")
            return model, set(), set()

        draw_ibs, target_names = _collect_target_section_names(model, selected_names, target_ini)
        return model, draw_ibs, target_names

    def execute_postprocess(self, mod_export_path):
        from .node_postprocess_comment_cleanup import SSMTNode_PostProcess_CommentCleanup

        target_ini, error_message = SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(mod_export_path)
        if not target_ini:
            _log_warning(f"跳过部件：{error_message or '未找到目标配置表'}")
            return False

        selected_names = self._selected_part_names()
        # 旧工程的手动哈希列表先并入统一列表（后台导出没有 UI 也会迁移一次）。
        try:
            _migrate_legacy_manual_hashes(self)
        except Exception:
            pass
        manual_entries, invalid_manual_items = _collect_node_manual_entries(self)
        if invalid_manual_items:
            details = "、".join(
                f"第 {position} 条「{hash_text or '空'}」{reason}"
                for position, hash_text, reason in invalid_manual_items[:5]
            )
            if len(invalid_manual_items) > 5:
                details += "…"
            _log_warning(
                f"跳过部件：手动哈希有 {len(invalid_manual_items)} 条无效、已忽略：{details}"
            )
        if not selected_names and not manual_entries:
            _log_info("跳过部件：节点未选择任何部件、也没有有效的手动哈希，跳过")
            return True

        # ---- 结构化圈定（与条件判断节点同一套机制） ----
        # 手动哈希不依赖选中物体 / 导出模型：它是给**没有导入 Blender** 的 IB 用的，
        # 这类 IB 在本次导出里没有段（删除步骤为 0），只追加跳过段即可。选中部件
        # 这条路仍要求导出模型可用，避免半边删除逻辑（旧行为：模型不可用就整节点跳过）。
        model = None
        draw_ibs = set()
        target_names = set()
        selected_entries = []
        if selected_names:
            model, draw_ibs, target_names = self._collect_targets(selected_names, target_ini)
            manual_note = "；仍会追加手动哈希的跳过段" if manual_entries else ""
            if model is None:
                _log_warning(
                    "跳过部件：未获取到本次导出的蓝图模型（导出期段登记不可用），"
                    f"跳过选中部件{manual_note}"
                )
            elif not draw_ibs:
                _log_warning(
                    "跳过部件：选定的部件未匹配到任何 DrawIB（物体名与导出对象不一致），"
                    f"跳过选中部件{manual_note}"
                )
            else:
                # 导出期段登记里没有这些 DrawIB 的段**不再早退**：这类 IB 是游戏原生绘制、
                # 不在本 mod 的接管范围里，没有段可删；用户既然把它加进跳过列表，就应当
                # 直接追加完整跳过段（hash + match_first_index + match_index_count +
                # handling = skip）让原生绘制被跳过（删除步骤自然 0 段）。有导出段的 IB
                # 仍走「先删段、再追加」。
                selected_entries = _entries_from_names(selected_names)
                if not selected_entries:
                    selected_entries = _hash_only_entries(draw_ibs)
                    if selected_entries:
                        _log_warning(
                            "跳过部件：物体名里没有可完整解析的 <hash>-<indexCount>-<firstIndex>，"
                            "回退为只 hash 的跳过段"
                        )
        if not selected_entries and not manual_entries:
            # 选中部件这条路不可用（模型缺失 / 对不上任何 DrawIB）且没有手动哈希：
            # 保持旧行为——本次不改写配置表。
            return True

        # 手动哈希在导出段登记里查得到段时（填的哈希恰好也属于本次导出），按节点
        # 语义先删除这些段再追加跳过段，避免同一 hash 上正常绘制段与跳过段并存。
        manual_hashes = [entry[0] for entry in manual_entries]
        if manual_hashes:
            manual_target_names = _collect_manual_target_section_names(manual_hashes, target_ini)
            if manual_target_names:
                target_names = set(target_names) | set(manual_target_names)
                _log_info(
                    f"跳过部件：手动哈希在本次导出登记里有 {len(manual_target_names)} 个段，"
                    "按节点语义先删除再追加跳过段"
                )

        # 追加条目 = 选中物体名前缀条目 + 手动哈希条目（去重、保序）
        entries = list(selected_entries)
        seen_entries = set(entries)
        for entry in manual_entries:
            if entry in seen_entries:
                continue
            seen_entries.add(entry)
            entries.append(entry)

        try:
            with open(target_ini, "rb") as handle:
                raw_bytes = handle.read()
        except OSError as exc:
            _log_warning(f"跳过部件：读取配置表失败 {target_ini}: {exc}")
            return False

        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        try:
            content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            _log_warning(f"跳过部件：配置表不是 UTF-8，跳过 {target_ini}: {exc}")
            return False

        # ---- 删除 + 追加 ----
        # entries 已在上面合并好：选中物体名前缀条目优先（完整三段），手动哈希条目
        # 追加在后。只写 hash 的跳过段在部分游戏里不生效，所以能解析出三段的都用
        # 完整段（match_first_index + match_index_count + handling = skip）。
        entry_list = ", ".join(_format_skip_entry(entry) for entry in entries)
        manual_entry_list = ", ".join(
            _format_skip_entry(entry) for entry in manual_entries
        )

        # 跨 IB 共用段保护：删同名段前先确认它是否仍被本次删除范围之外的 draw_ib 登记。
        # CrossIBPresent / CrossIBShaderOverride / CrossIBTextureOverride 这类段在设计上
        # 就由多个 IB 共享，删掉当前 IB 那一份会连带毁掉别的 IB 的绘制。
        # 本次要删的 draw_ib = 选中部件圈定的 + 手动哈希（查得到段时才必须删）。
        keep_draw_ibs = set(draw_ibs) | set(manual_hashes)
        shared_sections = _collect_cross_ib_shared_names(target_names, keep_draw_ibs)
        registry_note = "" if shared_sections is not None else _REGISTRY_UNAVAILABLE_NOTE

        # 先剥掉本节点上一次追加的块：否则第二次执行会把旧跳过段当成目标段重复删 / 计数。
        stripped_content = _strip_skip_block(content)
        updated, deleted_names, preserved_names = _delete_sections(
            stripped_content, target_names, shared_sections
        )
        updated = _append_skip_block(updated, entries)

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
                _log_warning(f"跳过部件：写入配置表失败 {target_ini}: {exc}")
                return False

        # ---- 登记新段：让链上后面的条件判断节点能圈到跳过段并给它加条件 ----
        registry = _get_section_registry()
        if registry is not None:
            for entry in entries:
                hash_val = entry[0]
                try:
                    registry.register_section(
                        _skip_section_name(entry),
                        section_type="TextureOverrideIB",
                        draw_ib=hash_val,
                        ini_path=target_ini,
                    )
                except Exception as exc:
                    _log_warning(f"跳过部件：登记导出段失败（{exc}）")

        # ---- 提示：跨 IB 共用段被保留 / 分文件里也有同名段 ----
        if shared_sections is None:
            _log_warning(f"跳过部件：{_REGISTRY_UNAVAILABLE_NOTE}")
        elif registry is None and not target_names:
            _log_warning(
                "跳过部件：导出段登记表不可用 -> 无法确认这些 IB 在配置表里有哪些段，"
                "本次只追加跳过段、不删除任何段（跨 IB 共用段不会被误删）"
            )
        if preserved_names:
            preserved_detail = _describe_cross_ib_shared(
                {
                    name.casefold(): shared_sections.get(name.casefold(), {})
                    for name in preserved_names
                }
            )
            _log_warning(
                f"跳过部件：{len(preserved_names)} 个同名段仍被其它 draw_ib 登记（跨 IB 共用），"
                f"已保留不删：{preserved_detail}"
            )
        for sibling_name, sibling_sections in _find_sibling_ini_sections(
            target_ini, target_names
        ):
            _log_info(
                f"跳过部件：同目录的分文件 {sibling_name} 里也有同名段"
                f"（{'、'.join(sibling_sections)}）；本节点只处理 "
                f"{os.path.basename(target_ini)}，分文件未改动"
            )

        # ---- 日志：区分「有导出段（删段+追加）」与「无导出段（只追加）」 ----
        ib_list = ", ".join(sorted(str(value) for value in draw_ibs))
        source_parts = []
        if selected_names:
            if selected_entries:
                source_parts.append(
                    f"选中 {len(selected_names)} 个部件、圈定 IB {len(draw_ibs)} 个"
                    f"（{ib_list}）"
                )
            else:
                source_parts.append(f"选中 {len(selected_names)} 个部件（未圈定到可用段）")
        if manual_entries:
            source_parts.append(
                f"手动哈希 {len(manual_entries)} 个（{manual_entry_list}）"
            )
        source_text = "；".join(source_parts)
        suffix = "" if written else "（配置表已是最新，未改写）"
        if deleted_names:
            delete_text = f"删除段 {len(deleted_names)} 个（{'、'.join(deleted_names)}）"
        elif preserved_names:
            delete_text = "同名段全部被跨 IB 共用保护拦下、未删除"
        elif registry is None:
            delete_text = "导出段登记表不可用，无法确认这些 IB 的段（无段可删）"
        elif manual_entries and not selected_entries:
            delete_text = "手动哈希不在本次导出中（无段可删）"
        else:
            delete_text = "这些 IB 不在本次导出中（无段可删）"
        message = (
            f"跳过部件：{source_text}；{delete_text}；"
            f"新增跳过段 {len(entries)} 个（{entry_list}）{suffix} -> {target_ini}"
        )
        notes = []
        if registry_note:
            notes.append(registry_note)
        if preserved_names:
            notes.append(
                f"跨 IB 共用保护保留 {len(preserved_names)} 段（{'、'.join(preserved_names)}）"
            )
        if notes:
            message = f"{message}；{'；'.join(notes)}"
        _log_info(message)
        try:
            from . import export_progress
            ib_count_text = f"{len(draw_ibs)} 个IB"
            selected_text = f"{len(selected_names)} 个部件"
            manual_text = f" / 手动哈希 {len(manual_entries)} 个" if manual_entries else ""
            if deleted_names:
                export_progress.report_stage(
                    f"跳过部件：{ib_count_text} / 删除 {len(deleted_names)} 段 / "
                    f"新增 {len(entries)} 段（{entry_list}）/ {selected_text}{manual_text}"
                )
            else:
                export_progress.report_stage(
                    f"跳过部件：{ib_count_text} / 无导出段可删 / "
                    f"新增 {len(entries)} 段（{entry_list}）/ {selected_text}{manual_text}"
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


def _sync_downstream_condition_nodes(node):
    try:
        from .node_postprocess_condition import _sync_downstream_condition_nodes as sync

        sync(node)
    except Exception as exc:
        print(f"[跳过部件] 同步下游条件判断失败: {exc}")


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

        try:
            _migrate_legacy_manual_hashes(node)
        except Exception:
            pass
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

        _sync_downstream_condition_nodes(node)
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

        try:
            _migrate_legacy_manual_hashes(node)
        except Exception:
            pass
        if 0 <= self.index < len(node.skip_objects):
            node.skip_objects.remove(self.index)
            node.active_skip_object = max(0, min(int(node.active_skip_object), len(node.skip_objects) - 1))
            _sync_downstream_condition_nodes(node)
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

        try:
            _migrate_legacy_manual_hashes(node)
        except Exception:
            pass
        while len(node.skip_objects) > 0:
            node.skip_objects.remove(len(node.skip_objects) - 1)
        # 迁移失败时也兜底清空旧列表
        legacy = getattr(node, "manual_hash_items", None)
        if legacy is not None and hasattr(legacy, "remove"):
            while len(legacy) > 0:
                try:
                    legacy.remove(0)
                except (TypeError, ValueError):
                    del legacy[0]
        node.active_skip_object = 0
        _sync_downstream_condition_nodes(node)
        self.report({'INFO'}, "已清空跳过部件列表")
        return {'FINISHED'}


class SSMT_OT_PostProcess_AddManualHash(bpy.types.Operator):
    bl_idname = "ssmt.skip_manual_hash_add"
    bl_label = "添加手动哈希"
    bl_description = "把 Hash / IndexCount / FirstIndex 输入框里的条目加入手动哈希列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        ok, message = _add_manual_skip_item(
            node,
            getattr(node, "temp_manual_hash", ""),
            getattr(node, "temp_index_count", ""),
            getattr(node, "temp_first_index", ""),
        )
        if not ok:
            self.report({'WARNING'}, message)
            return {'CANCELLED'}

        node.temp_manual_hash = ""
        node.temp_index_count = ""
        node.temp_first_index = ""
        try:
            node.active_skip_object = max(0, len(node.skip_objects) - 1)
        except Exception:
            pass
        self.report({'INFO'}, f"已加入跳过部件：{message}")
        return {'FINISHED'}


class SSMT_OT_PostProcess_RemoveManualHash(bpy.types.Operator):
    bl_idname = "ssmt.skip_manual_hash_remove"
    bl_label = "移除手动哈希"
    bl_description = "从手动哈希列表中移除指定项"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        if 0 <= self.index < len(node.manual_hash_items):
            node.manual_hash_items.remove(self.index)
            node.active_manual_hash = max(
                0, min(int(node.active_manual_hash), len(node.manual_hash_items) - 1)
            )
            return {'FINISHED'}

        self.report({'WARNING'}, "列表中没有这一项")
        return {'CANCELLED'}


class SSMT_OT_PostProcess_ClearManualHashes(bpy.types.Operator):
    bl_idname = "ssmt.skip_manual_hash_clear"
    bl_label = "清空手动哈希"
    bl_description = "清空手动哈希列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_skip_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择跳过节点")
            return {'CANCELLED'}

        while len(node.manual_hash_items) > 0:
            node.manual_hash_items.remove(len(node.manual_hash_items) - 1)
        node.active_manual_hash = 0
        self.report({'INFO'}, "已清空手动哈希列表")
        return {'FINISHED'}


classes = (
    SSMT_SkipObjectItem,
    SSMT_UL_SKIP_OBJECTS,
    SSMT_ManualHashItem,
    SSMT_UL_MANUAL_HASHES,
    SSMTNode_PostProcess_Skip,
    SSMT_OT_PostProcess_AddSkipObject,
    SSMT_OT_PostProcess_RemoveSkipObject,
    SSMT_OT_PostProcess_ClearSkipObjects,
    SSMT_OT_PostProcess_AddManualHash,
    SSMT_OT_PostProcess_RemoveManualHash,
    SSMT_OT_PostProcess_ClearManualHashes,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
