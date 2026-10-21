# -*- coding: utf-8 -*-
"""INI 代码范围圈定（纯函数，不 import bpy、不 import 节点模块）。

用途
----
「材质转资源pro（自定义材质指定）」要在导出后的 ini 段里找出「本节点指定部件」
的绘制代码范围，再决定是否拆块 / 置顶 / 补恢复块。本模块把这件事结构化：不再
按 ``drawindexed`` / ``;MARK:`` 关键词临时猜边界，而是复用「条件判断」节点的
圈定口径，逐层缩小范围：

1. **段**（``find_sections``）：``[SectionName]`` 切段，给出段头与段体区间；
2. **块**（``find_block_end`` / ``find_if_blocks``）：``if`` / ``endif`` 深度配对，
   得到每个条件块的完整区间；
3. **行**（``classify_line``）：把段体内一行分成绘制 / 贴图引用 / run / 绑定 /
   条件 / 结束 / 标记 / 注释 / 空行 / 其它；
4. **绘制单元**（``iter_draw_units``）：以 ``; [mesh:...]`` 注释为起点，给出该
   单元的行区间、包裹它的条件链、单元内的贴图引用行与绘制行。

与 ``node_postprocess_condition`` 的关系
----------------------------------------
段切分（``_find_sections``）与 if/endif 配对（``_find_block_end``）与条件节点
**同口径**实现。这里不 ``from .node_postprocess_condition import ...``，因为该
模块 import bpy，而本模块要保持纯函数、可被纯 Python 单测直接按文件路径装载
（条件节点也不依赖本模块，同口径实现不会产生循环依赖）。

术语
----
* **深度**：某一行之前尚未闭合的 ``if`` 数量；``endif`` 行按「闭合它自己的那个
  块」计深度（即先取深度、再出栈），这样「块内最后一个单元的终点 = 该块 endif
  行之前」可以直接用深度判断。
* **单元深度**：单元起点（``; [mesh:...]`` 行）的深度。单元内的嵌套 ``if``
  （例如材质切换条件块）属于单元内容，不被拆散。
"""
import re

#: 段头 ``[SectionName]``。与条件节点（继承自 skip 节点）的段头口径一致。
_SECTION_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*$")

_IF_START_RE = re.compile(r"^if\b", re.IGNORECASE)
_ENDIF_RE = re.compile(r"^endif\b", re.IGNORECASE)
#: ``else`` / ``else if`` / ``elseif`` / ``elif``：分支切换行，拆分时必须保守跳过。
_ELSE_RE = re.compile(r"^(?:else\b|elseif\b|elif\b)", re.IGNORECASE)

#: 部件锚点注释 ``; [mesh:物体名]``（导出期写进段体的绘制单元起点）。
_MESH_COMMENT_RE = re.compile(r"\[mesh:([^\]]+)\]", re.IGNORECASE)
#: 导出期段尾标记 ``;MARK:...``（必须留在段体最外层）。
_MARK_RE = re.compile(r"^;MARK", re.IGNORECASE)

#: 绘制命令（含 instanced 变体）。
_DRAW_RE = re.compile(r"^(?:drawindexed(?:instanced)?|draw(?:instanced)?)\s*=", re.IGNORECASE)
#: 命令列表调用（合并骨架 attach 等副作用，复制到多个块会重复执行）。
_RUN_RE = re.compile(r"^run\s*=", re.IGNORECASE)
#: 缓冲/着色器绑定行。
_BIND_RE = re.compile(r"^(?:ib|vb\d+|vs-t\d+|cs-t\d+)\s*=", re.IGNORECASE)
#: 贴图槽位绑定行：``ps-t0 = ...``（直接指向游戏槽位）。
_PS_T_RE = re.compile(r"^ps-t\d+\s*=", re.IGNORECASE)
#: ``this = ...``（个别导出器用它指向当前槽位）。
_THIS_RE = re.compile(r"^this\s*=", re.IGNORECASE)
#: 贴图别名绑定行：``Resource\ZZMI\Diffuse = ref ...`` / ``= null`` / ``= 资源名``。
_RESOURCE_SLOT_RE = re.compile(r"^resource\\[^=\s]+\\[^=\s]+\s*=", re.IGNORECASE)

#: 完整 IB 键（``8位hex-数字-数字``，如 ``6ef3d666-888-0``）。前后边界避免把更长
#: 的 hex 串或更长的数字当成 IB 键截断匹配。
_IB_KEY_FULL_RE = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{8})[._-](\d+)[._-](\d+)(?!\d)"
)
#: 名字里只有 8 位 IB hash 时的兜底（要求前后是分隔符或行尾）。
_IB_HEX_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{8})(?=[-_.]|$)")
#: 段根 ``hash = ...`` 行。
_HASH_LINE_RE = re.compile(r"^hash\s*=\s*(?P<value>.+)$", re.IGNORECASE)
#: 看起来是「缓冲 / 着色器资源」而非绘制 IB 的段名特征。这类段的 ``hash`` 是
#: VB/资源 hash，与 mesh 名里的 IB hash 不是一回事，不能用来做交叉校验。
_NON_IB_SECTION_HINT_RE = re.compile(r"(?:^|[_-])(?:vb|vs|ps|cs|buffer|buf)(?:[_-]|$)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 1. 段
# --------------------------------------------------------------------------- #

def find_sections(lines):
    """把行列表切成段。

    返回 ``[(段名, 段头索引, 段体起始索引, 段体结束索引), ...]``，结束索引
    独占（等于下一个段头索引，或文件末尾）。第一个段头之前的前导内容不属于
    任何段。段名不含方括号。
    """
    line_list = list(lines or [])
    headers = []
    for index, raw_line in enumerate(line_list):
        header = _SECTION_HEADER_RE.match(str(raw_line or "").strip())
        if header:
            headers.append((header.group(1).strip(), index))
    sections = []
    for position, (name, header_index) in enumerate(headers):
        body_start = header_index + 1
        body_end = headers[position + 1][1] if position + 1 < len(headers) else len(line_list)
        sections.append((name, header_index, body_start, body_end))
    return sections


# --------------------------------------------------------------------------- #
# 2. 块（if / endif 深度配对）
# --------------------------------------------------------------------------- #

def _scan_structure(lines):
    """预扫描结构：返回 ``(每行深度, 每行包裹的 if 起点元组, if起点->endif索引)``。

    深度口径见模块 docstring：``endif`` 行取「闭合它自己的那个块」的深度，
    使「块内最后一个单元的终点 = endif 行之前」可以直接成立。
    """
    line_list = list(lines or [])
    depths = []
    stacks = []
    block_ends = {}
    stack = []
    for index, raw_line in enumerate(line_list):
        stripped = str(raw_line or "").strip()
        depths.append(len(stack))
        stacks.append(tuple(stack))
        if _ENDIF_RE.match(stripped):
            if stack:
                block_ends[stack.pop()] = index
        elif _IF_START_RE.match(stripped):
            stack.append(index)
    return depths, stacks, block_ends


def line_depths(lines):
    """返回每行的 ``if`` 深度（口径同 :func:`_scan_structure`）。"""
    return _scan_structure(lines)[0]


def find_block_end(lines, start):
    """返回 ``start`` 处 ``if`` 配对的 ``endif`` 行索引；没闭合返回 ``None``。

    只认 ``if`` / ``endif`` 的深度配对，忽略其它行；``start`` 不是 ``if`` 行
    或超出范围时返回 ``None``。
    """
    line_list = list(lines or [])
    if start < 0 or start >= len(line_list):
        return None
    if not _IF_START_RE.match(str(line_list[start] or "").strip()):
        return None
    depth = 0
    for index in range(start, len(line_list)):
        stripped = str(line_list[index] or "").strip()
        if _IF_START_RE.match(stripped):
            depth += 1
        elif _ENDIF_RE.match(stripped):
            depth -= 1
            if depth == 0:
                return index
    return None


def find_if_blocks(lines):
    """返回段体内所有**顶层** ``if`` 块的 ``(起, 止)`` 行号（含 if 与 endif）。

    遇到没闭合的 ``if`` 时保守停止（后面的内容不再当作可重排的块）。
    """
    line_list = list(lines or [])
    blocks = []
    index = 0
    total = len(line_list)
    while index < total:
        if _IF_START_RE.match(str(line_list[index] or "").strip()):
            end = find_block_end(line_list, index)
            if end is None:
                break
            blocks.append((index, end))
            index = end + 1
            continue
        index += 1
    return blocks


# --------------------------------------------------------------------------- #
# 3. 行分类
# --------------------------------------------------------------------------- #

def is_else_line(stripped):
    """是否 ``else`` / ``else if`` / ``elseif`` / ``elif`` 分支切换行。"""
    return bool(_ELSE_RE.match(str(stripped or "").strip()))


def classify_line(stripped):
    """把段体内一行分类成下面的一种（返回小写字符串）。

    - ``"draw"``：绘制命令（``drawindexed`` / ``drawinstanced`` / ``draw`` …）；
    - ``"texture_ref"``：贴图引用行（``ps-tN = ...`` / ``this = ...`` /
      ``Resource\\<ns>\\<slot> = ref|null|资源名``）；
    - ``"run"``：``run = ...`` 命令列表调用；
    - ``"bind"``：缓冲 / 着色器绑定（``ib =`` / ``vbN =`` / ``vs-tN =`` / ``cs-tN =``）；
    - ``"if"`` / ``"endif"``：条件块边界；
    - ``"mark"``：``;MARK:`` 导出期段标记；
    - ``"comment"``：其它注释行（``;`` / ``//`` 开头）；
    - ``"neutral"``：空行；
    - ``"other"``：其余未识别行（含 ``else`` / ``elseif``，需要时用
      :func:`is_else_line` 判定）。
    """
    text = str(stripped or "").strip()
    if not text:
        return "neutral"
    if text.startswith(";"):
        # ``;MARK:...`` 是导出期段标记，语义上是「段尾到此为止」，单列一类。
        if text[:5].upper() == ";MARK":
            return "mark"
        return "comment"
    if text.startswith("//"):
        return "comment"
    if _IF_START_RE.match(text):
        return "if"
    if _ENDIF_RE.match(text):
        return "endif"
    if _DRAW_RE.match(text):
        return "draw"
    if _PS_T_RE.match(text) or _THIS_RE.match(text) or _RESOURCE_SLOT_RE.match(text):
        return "texture_ref"
    if _RUN_RE.match(text):
        return "run"
    if _BIND_RE.match(text):
        return "bind"
    return "other"


# --------------------------------------------------------------------------- #
# 4. 绘制单元
# --------------------------------------------------------------------------- #

def iter_draw_units(lines, is_target=None):
    """按出现顺序产出段内所有绘制单元 ``dict``。

    每个单元包含：

    - ``start`` / ``end``：行区间（``end`` 独占）；
    - ``mesh``：``; [mesh:...]`` 里的 mesh 名；
    - ``is_target``：``is_target`` 为 ``None`` 时留 ``None``；否则调用回调
      ``is_target(unit)`` 并把返回值转成 ``bool``（回调里可读 ``unit["mesh"]`` /
      ``unit["material_lines"]`` 等字段，也可自行按 ``start`` / ``end`` 取行）；
    - ``chain``：包裹该单元的条件链，外层 → 内层，元素为
      ``(if 行号, 配对 endif 行号, if 行原文)``；
    - ``material_lines``：单元内**贴图引用行**（``texture_ref``）的行号列表；
    - ``draw_lines``：单元内绘制命令（``draw``）的行号列表。

    单元边界（取最早满足的一项）：

    1. 同深度的下一个 ``; [mesh:...]`` 行（下一个兄弟绘制单元起点）；
    2. 包裹该单元的最内层 ``if`` 块的 ``endif`` 行之前；
    3. 同深度或更外层的 ``;MARK:`` 行之前（导出期段标记留在最外层）；
    4. 同深度或更外层的 ``else`` / ``elseif`` 行之前（分支切换不属于本单元）；
    5. **单元内已出现同深度绘制命令之后**，新开的同深度 ``if`` 行之前。这一条
       保留既有语义：目标绘制之后的独立条件块不应被跟着拖走（置顶 / 拆块时
       会把它当成新的代码范围起点）。

    单元内嵌套的材质切换 ``if``（在其绘制命令之前开启）整体属于该单元，不被
    拆散；嵌套 ``if`` 内部的 mesh 注释也会各自作为**带条件链**的单元产出，
    调用方可用 ``chain`` 区分层级。
    """
    line_list = list(lines or [])
    total = len(line_list)
    depths, stacks, block_ends = _scan_structure(line_list)

    for start in range(total):
        mesh_match = _MESH_COMMENT_RE.search(str(line_list[start] or ""))
        if not mesh_match:
            continue

        unit_depth = depths[start]
        chain = tuple(
            (
                block_start,
                block_ends.get(block_start, total),
                str(line_list[block_start] or "").strip(),
            )
            for block_start in stacks[start]
        )

        if chain:
            end = block_ends.get(chain[-1][0], total)
        else:
            end = total

        seen_same_depth_draw = False
        scan = start + 1
        while scan < end:
            stripped = str(line_list[scan] or "").strip()
            if not stripped:
                scan += 1
                continue
            scan_depth = depths[scan]
            if scan_depth == unit_depth and _MESH_COMMENT_RE.search(stripped):
                end = scan
                break
            if scan_depth <= unit_depth:
                if _MARK_RE.match(stripped):
                    end = scan
                    break
                if _ELSE_RE.match(stripped):
                    end = scan
                    break
                if _IF_START_RE.match(stripped) and seen_same_depth_draw:
                    end = scan
                    break
            if scan_depth == unit_depth and _DRAW_RE.match(stripped):
                seen_same_depth_draw = True
            scan += 1

        material_lines = []
        draw_lines = []
        for index in range(start, end):
            kind = classify_line(line_list[index])
            if kind == "texture_ref":
                material_lines.append(index)
            elif kind == "draw":
                draw_lines.append(index)

        unit = {
            "start": start,
            "end": end,
            "mesh": mesh_match.group(1),
            "is_target": None,
            "chain": chain,
            "material_lines": material_lines,
            "draw_lines": draw_lines,
        }
        if is_target is not None:
            unit["is_target"] = bool(is_target(unit))
        yield unit


def section_mesh_names(lines, unique=True):
    """段体内出现的 mesh 注释名称（按出现顺序；``unique`` 时去重）。

    只做文本层面的名字收集（含嵌套 ``if`` 内部的注释），不做目标判定 ——
    判定交给调用方，保持本模块不依赖 bpy。
    """
    names = []
    seen = set()
    for raw_line in lines or []:
        match = _MESH_COMMENT_RE.search(str(raw_line or ""))
        if not match:
            continue
        name = match.group(1)
        if unique:
            if name in seen:
                continue
            seen.add(name)
        names.append(name)
    return names


# --------------------------------------------------------------------------- #
# 5. IB 键交叉校验（段 → mesh）
# --------------------------------------------------------------------------- #

def _normalize_ib_key(match):
    """把正则匹配到的 ``(hex, 数字, 数字)`` 归一化成 ``{"hex", "full"}``。"""
    return {
        "hex": match.group(1).lower(),
        "full": "{}-{}-{}".format(
            match.group(1).lower(), match.group(2), match.group(3)
        ),
    }


def mesh_ib_key(mesh_name):
    """从 mesh 名取 IB 键；取不到返回 ``None``。

    返回 ``{"hex": 8位小写hex, "full": "hex-数字-数字" 或 None}``。导出 ini 的
    mesh 注释常只带 IB 键（``LOD0.6ef3d666-888-0``），而 Blender 物体名后面还
    挂着部件名（``LOD0.6ef3d666-888-0.武器翅膀花纹贴片``），两边靠这个键对齐。
    """
    text = str(mesh_name or "")
    match = _IB_KEY_FULL_RE.search(text)
    if match:
        return _normalize_ib_key(match)
    hex_match = _IB_HEX_RE.search(text)
    if hex_match:
        return {"hex": hex_match.group(1).lower(), "full": None}
    return None


def section_ib_key(section_name, lines):
    """从段名或段体 ``hash =`` 行取本段对应的 IB 键；取不到返回 ``None``。

    优先用段名里的**完整 IB 键**（``[TextureOverride_LOD0.c209c22b_45087_0]``
    -> ``c209c22b-45087-0``），其次用 ``hash =`` 行里的完整 IB 键。

    ``hash =`` 只有 8 位 hash 时，**仅在段名不像 VB/VS/PS/CS/Buffer 资源段**
    的情况下才采用（这类段的 ``hash`` 是缓冲 hash，跟 mesh 名里的 IB hash 不是
    一回事，拿来比较会把整段误判掉）。返回 ``{"hex": 8位小写hex, "full": ...}``。
    """
    name = str(section_name or "")
    match = _IB_KEY_FULL_RE.search(name)
    if match:
        return _normalize_ib_key(match)

    looks_like_resource_section = bool(_NON_IB_SECTION_HINT_RE.search(name))
    for raw_line in lines or []:
        stripped = str(raw_line or "").strip()
        hash_match = _HASH_LINE_RE.match(stripped)
        if not hash_match:
            continue
        value = hash_match.group("value")
        full_match = _IB_KEY_FULL_RE.search(value)
        if full_match:
            return _normalize_ib_key(full_match)
        hex_match = _IB_HEX_RE.search(value)
        if hex_match and not looks_like_resource_section:
            return {"hex": hex_match.group(1).lower(), "full": None}
    return None


def ib_key_matches(section_scope, mesh_scope):
    """段 IB 键与 mesh IB 键是否相容。

    两边都取不到（``None``）或只有一侧取到时一律返回 ``True``（不做交叉校验，
    保持既有「按名字 / 名称映射」判定）；完整键优先比较完整键，否则退化成
    比较 8 位 hex 前缀。
    """
    if not section_scope or not mesh_scope:
        return True
    section_full = section_scope.get("full")
    mesh_full = mesh_scope.get("full")
    if section_full and mesh_full:
        return section_full == mesh_full
    section_hex = section_scope.get("hex")
    mesh_hex = mesh_scope.get("hex")
    if section_hex and mesh_hex:
        return section_hex == mesh_hex
    return True


__all__ = [
    "classify_line",
    "find_block_end",
    "find_if_blocks",
    "find_sections",
    "ib_key_matches",
    "is_else_line",
    "iter_draw_units",
    "line_depths",
    "mesh_ib_key",
    "section_ib_key",
    "section_mesh_names",
]
