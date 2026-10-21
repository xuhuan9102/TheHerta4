"""RabbitFX 贴图后处理 Pro —— 可串联的 FX 参数注入节点。

**本节点不生成任何贴图配置**：发光贴图（``Glowmap_<亮度>_*``）与 FX 贴图
（``FXMap_*``）的材质判定、资源复制、``Resource\\RabbitFX\\Glowmap`` /
``Resource\\RabbitFX\\FXMap`` 绑定以及 ``run = CommandList\\RabbitFX\\Run``
全部由「材质转资源(pro)」完成。本节点只往这些已有绑定上**追加参数**，面板也
按这一点分成两块：

* **发光贴图参数**：``$\\RabbitFX\\H/S/V``、``brightness``、``interpolate``
  的静态值；它下面挂着子项**呼吸灯**，勾选后把这些变量交给三角波动态变换；
* **FX 贴图参数**：子项 **W-Engine 同步**（``$\\rabbitfx\\blendmode`` /
  ``SetFXBuffer`` / 提取源的 ``ERun``）与 **颜色偏移**
  （``run = CommandList\\RabbitFX\\ColorShift``，用 FX 贴图的 R 通道做遮罩）。

与旧的「RabbitFX贴图后处理」(:mod:`node_postprocess_rabbitfx`) 的差别：

* **不生成贴图资源**，因此没有「镂空 FXMap」这类开关——镂空由材质转资源生成，
  本节点管不了也不需要管；
* **可以多个串联**：同一条后处理链上允许存在任意多个本节点，每个节点自带
  物体列表与参数，只会改自己列表里物体所在的 mesh 块；
* **绘制后复位**：按本节点真正写过的参数生成复位行。RabbitFX 的 ``Run`` /
  ``ERun`` / ``ColorShift`` 命令列表末尾本身也会把
  ``$h/$s/$v/$brightness/$interpolate/$BLENDMODE`` 与资源别名清空
  （RabbitFX.ini ``[CommandListRun]`` 结尾），这里再显式写一遍作为双保险，
  同时覆盖「只绑定、不 Run」的异常路径。

**代码范围定位**：本节点不再按 ``[mesh:...]`` / ``drawindexed`` / ``endif`` 关键词
自己切块，而是复用 :mod:`blueprint.ini_scope` 的结构化圈定 —— 段 → ``if``/``endif``
深度配对 → 行分类 → 绘制单元。目标单元按「IB 键一致 + 部件名完全相等」判定
（同一个 IB 下的相邻部件不会被误当成目标）；参数块插在单元内 ``run`` / ``draw``
之前，复位块插在单元所在条件块最内层 ``endif`` 之后。

**命名空间随游戏走**：本节点注入的名字全部取自
:mod:`blueprint.fx_namespace` 的档案，不再写死 ``RabbitFX``。

* HIMI（崩坏 3）→ ``HI3FX``：变量是小写 ``$\\HI3FX\\h/s/v``，复位必须走
  ``run = CommandList\\HI3FX\\Reset``（HI3FX 的 ``Run`` 不复位变量，照搬
  RabbitFX 的「写 0 再 Run」会让后续绘制全黑）；
* HI3FX 没有 ``SetFXBuffer`` / ``ERun`` / ``ColorShift``，所以 HIMI 下
  **W-Engine 同步与颜色偏移自动跳过**（面板会标出来）。

INI 语法全部对照官方 mod 说明与随附 demo（v7.8）：
https://gamebanana.com/mods/531649
"""

import bpy
import glob
import json
import os
import re
import uuid
from collections import OrderedDict

from . import fx_namespace
from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_rabbitfx import (
    _extract_hash_from_object,
    _extract_resource_suffix,
)

#: 「段 → if/endif 块 → 行分类 → 绘制单元」的结构化圈定（纯函数，不依赖 bpy）。
#: 正常插件导入走相对导入；纯 Python 单测按文件路径装载本模块时包结构不完整
#: （``PKG.blueprint.__path__`` 为空），退化成按文件路径直接装载。
try:  # pragma: no cover - 分支由测试装载方式决定
    from . import ini_scope as ini_scope
except ImportError:  # pragma: no cover
    import importlib.util as _importlib_util

    _INI_SCOPE_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ini_scope.py"
    )
    _ini_scope_spec = _importlib_util.spec_from_file_location(
        "ssmt_ini_scope_fallback", _INI_SCOPE_PATH
    )
    ini_scope = _importlib_util.module_from_spec(_ini_scope_spec)
    _ini_scope_spec.loader.exec_module(ini_scope)

NODE_IDNAME = 'SSMTNode_PostProcess_RabbitFXPro'
MATERIAL_NODE_IDNAME = 'SSMTNode_PostProcess_CustomMaterialAssign'
LEGACY_MATERIAL_NODE_IDNAME = 'SSMTNode_PostProcess_Material'

# 下面这些常量只属于 RabbitFX 的 W-Engine 同步 / ColorShift 路径（HI3FX 没有对应
# 能力，相关分支在 fx_profile 不支持时直接跳过），命名空间相关的名字一律从
# blueprint/fx_namespace.py 的档案取。
SET_FX_BUFFER_REF = r"Resource\RabbitFX\SetFXBuffer"
COLOR_SHIFT_LINE = r"run = CommandList\RabbitFX\ColorShift"
E_RUN_LINE = r"run = CommandList\RabbitFX\ERun"
UPDATE_BUFFER_LINE = r"pre run = CommandList\RabbitFX\UpdateFXBuffer"

# 本节点写入块的起止标记。清理时按标记成对删除，保证重复导出幂等。
# 标记必须保持纯 ASCII：配置文件清理节点会把 INI 里的非 ASCII 文本改写成
# 确定性字母串，中文标记被改写后就再也匹配不上，重跑会累积重复块。
_PARAM_BEGIN = "; === RabbitFXPro Params ==="
_PARAM_END = "; === End RabbitFXPro Params ==="
_BREATH_BEGIN = "; === RabbitFXPro Breath ==="
_BREATH_END = "; === End RabbitFXPro Breath ==="
_CS_BEGIN = "; === RabbitFXPro ColorShift ==="
_CS_END = "; === End RabbitFXPro ColorShift ==="
_SYNC_BEGIN = "; === RabbitFXPro SyncSource ==="
_SYNC_END = "; === End RabbitFXPro SyncSource ==="
_RESET_BEGIN = "; === RabbitFXPro Reset ==="
_RESET_END = "; === End RabbitFXPro Reset ==="

# 兼容早期中文标记（结束 / End 两种写法都认）。
_MARKER_RE = re.compile(
    r"^;\s*===\s*(?:(?P<end>结束|End)\s+)?RabbitFXPro\b", re.IGNORECASE
)

# 呼吸灯计数变量统一前缀，清理上一轮生成时按前缀整块删除。
_VAR_PREFIX = "$rfxpro_"
_VAR_IF_RE = re.compile(r"^if\s+\$rfxpro_", re.IGNORECASE)

_MESH_LINE_RE = re.compile(r"^\s*\[mesh:(?P<name>.+?)\]\s*$", re.IGNORECASE)
_IF_LINE_RE = re.compile(r"^if\s+", re.IGNORECASE)

#: 名称里的 IB 编号段（``8位hex-数字-数字``）：部件名比对前必须剥掉，否则同一个
#: IB 下的所有 mesh 看起来都同名（实机 bug：``右手.001`` 被当成 ``右乳球.001``）。
_IB_SEGMENT_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}[._-]\d+[._-]\d+(?!\d)"
)
#: 名字开头的 ``LOD0.`` 前缀（同一部件在不同 LOD 里名字不同）。
_LOD_PREFIX_RE = re.compile(r"^LOD\d+[._-]", re.IGNORECASE)
#: 运行时附加在名字末尾的后缀（``_copy`` / ``.copy`` / ``-copy``，可重复出现）。
_RUNTIME_SUFFIX_RE = re.compile(r"(?:[._-]copy)+$", re.IGNORECASE)
#: 名字末尾的序号段（``.001`` / ``_001``）：序号本身必须完全一致才算同一个部件。
_TRAILING_INDEX_RE = re.compile(r"^(?P<base>.+?)[._-](?P<index>\d{1,4})$")


def _mesh_object_poll(_self, obj):
    return bool(getattr(obj, "type", "") == "MESH")


def _binding_namespaces(sections):
    """配置表里出现过的 FX 命名空间档案（用来告诉作者该表用的是哪一套名字）。"""
    return fx_namespace.detect_profiles(sections)


def _find_material_node_in_tree(node):
    """同一蓝图里的「材质转资源(pro)」节点（本节点的贴图绑定来源）。"""
    tree = getattr(node, "id_data", None)
    for candidate in getattr(tree, "nodes", []) or []:
        if getattr(candidate, "bl_idname", "") in (
            MATERIAL_NODE_IDNAME,
            LEGACY_MATERIAL_NODE_IDNAME,
        ):
            return candidate
    return None


def _normalize_suffix(suffix):
    return str(suffix or "").strip().replace("-", "_").lower()


def _part_name(name):
    """从物体名 / mesh 注释名里取出「部件名」。

    剥掉 ``LOD0.`` 前缀、IB 编号段（``8位hex-数字-数字``）以及运行时附加的
    ``_copy`` 尾巴，剩下的就是部件名（``右手.001`` / ``右乳球.001`` / ``双乳钩``）。

    导出 ini 的 mesh 注释与 Blender 物体名两边都按这个口径归一：既能让同一个
    部件的多个 mesh 单元都对齐到目标物体，又不会把同一 IB 下的相邻部件误当成
    目标（旧口径 ``_extract_resource_suffix`` 只取到 IB 编号，后缀互相包含即匹配，
    于是 ``右手.001`` 命中了 ``右乳球.001`` 的单元）。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    text = _LOD_PREFIX_RE.sub("", text)
    text = _IB_SEGMENT_RE.sub("", text)
    text = _RUNTIME_SUFFIX_RE.sub("", text)
    return text.strip(" \t._-")


def _part_names_equal(left, right):
    """两个部件名是否相等（大小写不敏感；序号段必须完全一致）。

    ``.001`` / ``_001`` 这类序号只允许分隔符写法不同，序号本身必须相同 ——
    ``右手.001`` 与 ``右手.002``、``右手`` 与 ``右手.001`` 都不算同一个部件。
    """
    if not left or not right:
        return False
    if left.casefold() == right.casefold():
        return True
    left_match = _TRAILING_INDEX_RE.match(left)
    right_match = _TRAILING_INDEX_RE.match(right)
    if not left_match or not right_match:
        return False
    if left_match.group("index") != right_match.group("index"):
        return False
    return left_match.group("base").casefold() == right_match.group("base").casefold()


def _unit_matches_target(mesh_name, target_scope, target_part, section_scope=None):
    """绘制单元的 mesh 名是否就是这个目标物体（IB 键 + 部件名双重校验）。

    ``target_scope`` 是目标物体名的 IB 键（:func:`ini_scope.mesh_ib_key` 口径，
    与材质转资源 pro 的 ``_ib_key_from_name`` 一致）；``section_scope`` 是段名的
    IB 键，用来排除同名部件落在别的 IB 段里的情况。
    """
    mesh_scope = ini_scope.mesh_ib_key(mesh_name)
    if section_scope is not None and not ini_scope.ib_key_matches(section_scope, mesh_scope):
        return False
    if not ini_scope.ib_key_matches(target_scope, mesh_scope):
        return False
    if not target_part:
        # 物体名里取不到部件名（只有 IB 键）时无法再细分，只能按 IB 判定。
        return True
    return _part_names_equal(target_part, _part_name(mesh_name))


def _marker_kind(line):
    """``start`` / ``end`` / 空串 —— 识别本节点写入块的起止标记。"""
    match = _MARKER_RE.match(str(line or "").strip())
    if match is None:
        return ""
    return "end" if match.group("end") else "start"


def _strip_previous_blocks(lines):
    """删除本节点上一轮写入的标记块与呼吸灯变量块（重跑幂等）。"""
    kept = []
    index = 0
    total = len(lines)
    while index < total:
        stripped = str(lines[index]).strip()
        kind = _marker_kind(stripped)
        if kind == "start":
            # 只有配对成功才删除：孤立标记（截断文件）原样保留，避免吞掉后续内容。
            end_index = next(
                (
                    offset for offset in range(index + 1, total)
                    if _marker_kind(str(lines[offset]).strip()) == "end"
                ),
                None,
            )
            if end_index is None:
                kept.append(lines[index])
                index += 1
                continue
            index = end_index + 1
            continue
        if kind == "end":
            index += 1
            continue
        if stripped.startswith(_VAR_PREFIX):
            index += 1
            continue
        if _VAR_IF_RE.match(stripped):
            depth = 0
            while index < total:
                current = str(lines[index]).strip()
                if _IF_LINE_RE.match(current):
                    depth += 1
                elif current.casefold() == "endif":
                    depth -= 1
                    if depth <= 0:
                        index += 1
                        break
                index += 1
            continue
        kept.append(lines[index])
        index += 1
    return kept


def _leading_whitespace(line):
    """行首缩进（插进去的块跟锚点行保持同级，结构才合法）。"""
    text = str(line or "")
    stripped = text.lstrip()
    return text[: len(text) - len(stripped)]


def _trailing_indent(lines):
    """块末尾非空行的缩进（复位块跟着它写，不破坏缩进）。"""
    for line in reversed(list(lines or [])):
        if str(line).strip():
            return _leading_whitespace(line)
    return ""


def _profile_run_between(lines, start, end, profile):
    """``[start, end)`` 内第一条本命名空间 ``Run`` 行的索引；没有返回 ``None``。"""
    for index in range(max(0, start), min(end, len(lines))):
        if profile.run_line_re.match(str(lines[index]).strip()):
            return index
    return None


def _unit_anchor(lines, start, end, profile):
    """参数块的插入锚点 ``(锚点行, 第一个 draw 行)``；没有可用锚点时为 ``-1``。

    规则（实机修正）：参数必须在本单元的 ``Run`` 之前、``draw`` 之前生效，所以

    1. 取单元内**第一个 draw**；
    2. 若单元内还有 ``run =`` 行排在它之前，取更早的那条 —— 否则外部/其它节点
       写在 draw 之后的 ``run`` 会把参数块顶到 draw 后面（对本次绘制无效）；
    3. 单元里没有 draw（着色器替换把绘制搬进 ``run =`` 引用的段）时，退回
       「本命名空间的 Run 行，没有则最后一条 ``run``」。

    返回 ``(锚点行, draw 行)``；单元里既没有 draw 也没有 run 时锚点为 ``-1``。
    """
    first_draw = -1
    first_run = -1
    last_run = -1
    profile_run = -1
    for index in range(start, end):
        stripped = str(lines[index]).strip()
        kind = ini_scope.classify_line(stripped)
        if kind == "draw":
            if first_draw < 0:
                first_draw = index
        elif kind == "run":
            if first_run < 0:
                first_run = index
            last_run = index
            if profile_run < 0 and profile.run_line_re.match(stripped):
                profile_run = index
    if first_draw >= 0:
        anchor = first_draw
        if 0 <= first_run < first_draw:
            anchor = first_run
        return anchor, first_draw
    anchor = profile_run if profile_run >= 0 else last_run
    return anchor, -1


def _absorb_reset_block(lines, position):
    """把紧跟在 ``position`` 之后的本节点复位块并入可写范围（重跑幂等）。

    复位块写在单元末尾（单元在条件块内时 = 最内层 ``endif`` 之后），位置落在
    :func:`ini_scope.iter_draw_units` 给的单元区间之外；重跑时必须把它一起剥掉，
    否则会一轮一轮累积。只吸收紧邻的复位标记块，不吞别的目标/节点的内容。
    """
    total = len(lines)
    scan = position
    while scan < total and not str(lines[scan]).strip():
        scan += 1
    if scan >= total:
        return position
    if str(lines[scan]).strip().casefold() != _RESET_BEGIN.casefold():
        return position
    for offset in range(scan + 1, total):
        if str(lines[offset]).strip().casefold() == _RESET_END.casefold():
            return offset + 1
    return position


def _else_branch_endif(lines, else_index):
    """``else`` 行所属 ``if`` 块的 ``endif`` 行号；找不到返回 ``None``。"""
    depth = 0
    for index in range(else_index - 1, -1, -1):
        kind = ini_scope.classify_line(str(lines[index]).strip())
        if kind == "endif":
            depth += 1
        elif kind == "if":
            if depth == 0:
                return ini_scope.find_block_end(lines, index)
            depth -= 1
    return None


#: 资源别名清空行（材质转资源写的 ``Resource\RabbitFX\Glowmap = ref null`` / ``= null``）。
_RESET_SLOT_NULL_RE = re.compile(
    r"^resource\\[^=\s]+\\[^=\s]+\s*=\s*(?:ref\s+)?null\s*$", re.IGNORECASE
)
#: 变量归零行（``$\RabbitFX\H = 0`` / ``brightness = 0`` / ...）。
_RESET_PARAM_ZERO_RE = re.compile(
    r"^\$\\[^=\s]+\\(?:h|s|v|brightness|interpolate)\s*=\s*0(?:\.0+)?\s*$",
    re.IGNORECASE,
)
#: 命令列表调用行（``Run`` / ``Reset``）。
_RESET_RUN_RE = re.compile(
    r"^run\s*=\s*commandlist\\[^\\\s]+\\(?:run|reset)\s*$", re.IGNORECASE
)


def _reset_line_key(line):
    """复位行的归一化比较键（去重用）。

    ``= ref null`` 与 ``= null``、``= 0`` 与 ``= 0.0`` 是同一件事，实机里材质转资源
    这两种写法都有；不归一化会漏掉去重、写出等价但重复的复位行。
    """
    text = " ".join(str(line or "").strip().split())
    text = re.sub(r"=\s*ref\s+null\s*$", "= null", text, flags=re.IGNORECASE)
    text = re.sub(r"=\s*0(?:\.0+)?\s*$", "= 0", text)
    return text.casefold()


def _is_reset_like_line(line):
    """是不是一条复位行（见 :func:`_collect_trailing_reset_lines`）。"""
    text = str(line or "").strip()
    if not text:
        return False
    return bool(
        _RESET_SLOT_NULL_RE.match(text)
        or _RESET_PARAM_ZERO_RE.match(text)
        or _RESET_RUN_RE.match(text)
    )


def _collect_trailing_reset_lines(lines, position):
    """收集 ``position`` 之后紧邻的**块外**复位行（材质转资源写下的）。

    实机 bug：chain 内最后一个兄弟单元的 draw 之后、``endif`` 之外还有材质转资源
    写的 ``Glowmap = ref null`` / ``brightness = 0`` / ``Run``；旧去重只看单元区间，
    于是本节点又写了一份等价复位 —— 块内块外两套。把这些紧邻的复位行一并交给
    去重集合，重复行就不会再写进 Reset 块。
    """
    collected = []
    total = len(lines)
    index = position
    while index < total:
        stripped = str(lines[index]).strip()
        if not stripped:
            index += 1
            continue
        if not _is_reset_like_line(stripped):
            break
        collected.append(stripped)
        index += 1
    return collected


def _unit_edit_scope(lines, unit):
    """单元的**可写**行范围 ``(起, 止)``（止独占）。

    止 = 单元内容末尾；单元是 chain 内**最后一个**兄弟（末尾正好是 ``endif``）时
    包含该 ``endif`` —— 复位块插在它**之前**，留在条件块内（实机要求：不要写到
    ``endif`` 之外造成块外重复）。中间的兄弟单元止于下一个 ``; [mesh:...]``，
    区间互不重叠。因 ``else`` 截断时扩到所属 ``if`` 的 ``endif``。

    止之后紧邻的本节点旧复位块会被吸收进范围，保证重跑幂等。
    """
    start = int(unit["start"])
    end = int(unit["end"])
    chain = tuple(unit.get("chain") or ())
    end_line = str(lines[end]).strip() if 0 <= end < len(lines) else ""
    if chain:
        endif_index = chain[-1][1]
        if 0 <= endif_index < len(lines) and (
            end == endif_index or ini_scope.is_else_line(end_line)
        ):
            return start, _absorb_reset_block(lines, endif_index + 1)
    elif end < len(lines) and ini_scope.is_else_line(end_line):
        endif_index = _else_branch_endif(lines, end)
        if endif_index is not None:
            return start, _absorb_reset_block(lines, endif_index + 1)
    return start, _absorb_reset_block(lines, end)


def _reset_insert_position(unit, block):
    """复位块在 ``block`` 内的插入位置（``block`` 坐标）。

    统一口径：draw 之后、下一个兄弟 mesh / 条件块 ``endif`` 之前。最后一个兄弟
    单元的 ``block`` 末尾是 chain 的 ``endif``（见 :func:`_unit_edit_scope`），
    复位块插在它前面；其它单元插在块末尾（下一个 mesh 注释之前）。
    """
    chain = tuple(unit.get("chain") or ())
    if chain and int(unit["end"]) == chain[-1][1]:
        for index in range(len(block) - 1, -1, -1):
            if ini_scope.classify_line(str(block[index]).strip()) == "endif":
                return index
    return len(block)


def _section_fallback_unit(lines):
    """整段回退用的伪单元（段里没有 mesh 注释，只能按 hash + 段名定位整段）。"""
    total = len(lines)
    draw_lines = [
        index
        for index in range(total)
        if ini_scope.classify_line(str(lines[index]).strip()) == "draw"
    ]
    return {
        "start": 0,
        "end": total,
        "mesh": "",
        "chain": (),
        "material_lines": [],
        "draw_lines": draw_lines,
        "is_target": True,
    }


def _collect_existing_refs(lines, start, end, profile):
    """收集块内已有的发光/FX 别名绑定与 ``Run`` 行位置（按命名空间档案匹配）。

    匹配名字取自档案，所以 HIMI（``Resource\\HI3FX\\GlowMap`` /
    ``run = CommandList\\HI3FX\\Run``）与 RabbitFX 走的是同一条逻辑。
    """
    glow_ref = ""
    fxmap_ref = ""
    run_index = -1
    for index in range(start, end):
        stripped = str(lines[index]).strip()
        if run_index < 0 and profile.run_line_re.match(stripped):
            run_index = index
        if not glow_ref:
            match = profile.glow_ref_re.match(stripped)
            if match and match.group("name").strip().casefold() != "null":
                glow_ref = match.group("name").strip()
        if not fxmap_ref:
            match = profile.fxmap_ref_re.match(stripped)
            if match and match.group("name").strip().casefold() != "null":
                fxmap_ref = match.group("name").strip()
    return glow_ref, fxmap_ref, run_index


def _format_number(value):
    """整数写整数、浮点保留有效位，用于呼吸灯里的算式。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number == int(number):
        return str(int(number))
    return repr(round(number, 6))


def _format_float(value):
    """始终带小数点的写法，与官方示例（``brightness = 8.0``）保持一致。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    text = repr(round(number, 6))
    if "." not in text and "e" not in text.lower():
        text += ".0"
    return text


def _insert_block(lines, position, block_lines, indent=""):
    """在 ``position`` 处插入一段行；``indent`` 用于跟锚点行保持同级缩进。"""
    for offset, line in enumerate(block_lines):
        text = indent + line if indent and str(line).strip() else line
        lines.insert(position + offset, text)


# ────────────────────────── 材质侧探测 ──────────────────────────

class _MaterialProbe:
    """复用材质转资源的材质前缀判定，但不继承它的 Node 类。"""

    @staticmethod
    def _load():
        from .node_postprocess_material import SSMTNode_PostProcess_MaterialBase as base

        return base

    def find_matching_materials(self, obj, texture_type):
        return self._load().find_matching_materials(self, obj, texture_type)

    def _build_material_signature(self, material):
        return self._load()._build_material_signature(material)


_PROBE = _MaterialProbe()


def _probe_marked_slots(obj):
    """导入侧记录的槽位标记（modimp_texture_slots）里是否有 Glowmap / FXMap。"""
    raw = str(obj.get("modimp_texture_slots", "") or "").strip()
    if not raw:
        return False, False
    try:
        slots = json.loads(raw)
    except Exception:
        return False, False
    if not isinstance(slots, dict):
        return False, False
    has_glow = False
    has_fx = False
    for binding in slots.values():
        if not isinstance(binding, dict):
            continue
        mark_name = str(binding.get("mark_name", "") or "").strip().lower()
        if mark_name == "glowmap":
            has_glow = True
        elif mark_name == "fxmap":
            has_fx = True
    return has_glow, has_fx


def _probe_fx_textures(obj):
    """判断物体是否存在发光贴图 / FX 贴图（材质前缀 + 槽位标记）。"""
    has_glow = bool(_PROBE.find_matching_materials(obj, "Glowmap"))
    has_fx = bool(_PROBE.find_matching_materials(obj, "FXMap"))
    if not (has_glow and has_fx):
        slot_glow, slot_fx = _probe_marked_slots(obj)
        has_glow = has_glow or slot_glow
        has_fx = has_fx or slot_fx
    return has_glow, has_fx


class RabbitFXProTargetItem(bpy.types.PropertyGroup):
    """一个受本节点控制的物体（FX 贴图来自材质转资源）。"""

    target_object: bpy.props.PointerProperty(
        name="目标物体",
        description="注入 RabbitFX 参数的物体；其发光/裁切贴图由「材质转资源(pro)」提供",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    has_glow: bpy.props.BoolProperty(
        name="有发光贴图",
        description="扫描结果：该物体的材质里存在 Glowmap 前缀贴图（发光参数会被注入）",
        default=False,
    )
    has_fx: bpy.props.BoolProperty(
        name="有 FX 贴图",
        description="扫描结果：该物体的材质里存在 FXMap 前缀贴图（同步/颜色偏移会用它的绑定）",
        default=False,
    )


class SSMT_UL_RabbitFXProTargets(bpy.types.UIList):
    """目标物体列表（UIList：固定可见行数 + 自带滚动条）。"""

    bl_idname = "SSMT_UL_RABBITFX_PRO_TARGETS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "target_object", text="")
            if item.target_object is None:
                row.label(text="", icon='ERROR')
            if item.has_glow:
                row.label(text="", icon='LIGHT')
            if item.has_fx:
                row.label(text="", icon='TEXTURE')
            op = row.operator("ssmt.rabbitfx_pro_remove_at", text="", icon='X')
            op.node_name = getattr(data, "name", "")
            op.index = index
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon='OBJECT_DATA')


class SSMTNode_PostProcess_RabbitFXPro(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = 'RabbitFX贴图后处理pro'
    bl_description = (
        '复用材质转资源写好的 RabbitFX 发光/裁切贴图，注入静态发光(HSV/亮度/插值)、'
        '呼吸灯、W-Engine 同步与颜色偏移参数；可多个串联，每个节点只改自己列表里的物体'
    )

    # ── 目标物体 ──
    target_items: bpy.props.CollectionProperty(type=RabbitFXProTargetItem)
    active_target_index: bpy.props.IntProperty(
        name="当前物体", description="列表里高亮的行（右侧 − 与下方详情按它来）",
        default=0, min=0,
    )

    # ── 静态发光 ──
    enable_glow: bpy.props.BoolProperty(
        name="静态发光",
        description=(
            "写入当前游戏的 FX 变量：RabbitFX 是 $\\RabbitFX\\H/S/V/brightness/interpolate，"
            "HIMI（崩坏 3）是 $\\HI3FX\\h/s/v/brightness/interpolate"
        ),
        default=True,
    )
    glow_h: bpy.props.FloatProperty(name="H 色相", default=0.0, min=-360, max=360)
    glow_s: bpy.props.FloatProperty(name="S 饱和度", default=0.0, min=-100, max=100)
    glow_v: bpy.props.FloatProperty(name="V 明度", default=0.0, min=-100, max=100)
    glow_brightness: bpy.props.FloatProperty(
        name="发光强度/光晕范围",
        description="$\\RabbitFX\\brightness（HIMI 为 $\\HI3FX\\brightness），越大光晕越强",
        default=8.0,
        min=0,
        max=500,
    )
    glow_interpolate: bpy.props.FloatProperty(
        name="插值",
        description="$\\RabbitFX\\interpolate（HIMI 为 $\\HI3FX\\interpolate），0=原色 1=完全偏移",
        default=1.0,
        min=0,
        max=1,
    )

    # ── 呼吸灯 ──
    enable_breath: bpy.props.BoolProperty(
        name="启用呼吸灯",
        description="让发光贴图参数的 H/S/V/亮度/插值随时间变化",
        default=False,
    )
    breath_mode: bpy.props.EnumProperty(
        name="呼吸模式",
        items=[
            ('SINGLE', "单色呼吸", "固定颜色，发光强度周期性脉动"),
            ('RAINBOW', "多彩渐变", "Hue 全色环循环，发光强度固定"),
            ('COMBO', "渐变式呼吸", "Hue 全色环循环 + 发光强度同步脉动"),
        ],
        default='SINGLE',
    )
    breath_fps: bpy.props.IntProperty(
        name="周期(帧)", description="三角波一个来回的帧数", default=200, min=10, max=7200
    )
    breath_step: bpy.props.FloatProperty(
        name="速度", description="每帧给计数器加多少", default=0.3, min=0.01, max=100
    )
    breath_h: bpy.props.FloatProperty(name="H 色相", default=0.0, min=0, max=360)
    breath_s: bpy.props.FloatProperty(name="S 饱和度", default=0.0, min=0, max=100)
    breath_v: bpy.props.FloatProperty(name="V 明度", default=0.0, min=0, max=100)
    breath_brightness_min: bpy.props.FloatProperty(name="最小强度", default=1.0, min=0, max=500)
    breath_brightness_max: bpy.props.FloatProperty(name="最大强度", default=8.0, min=0, max=500)
    breath_interpolate: bpy.props.FloatProperty(name="插值", default=1.0, min=0, max=1)

    # ── FX 贴图参数 ──
    enable_sync: bpy.props.BoolProperty(
        name="W-Engine 同步", description="让发光颜色/亮度跟随 W-Engine 发光", default=False
    )
    sync_blendmode: bpy.props.IntProperty(
        name="混合模式",
        description="$\\rabbitfx\\blendmode；1 = 与引擎发光做比较（只显示更亮的部位）",
        default=1,
        min=0,
        max=1,
    )
    sync_brightness_only: bpy.props.BoolProperty(
        name="仅同步亮度", description="$\\rabbitfx\\syncbrightnessonly = 1", default=False
    )
    sync_source_object: bpy.props.PointerProperty(
        name="提取源物体",
        description="带 W-Engine 发光的部件（通常是头发），在其绘制前运行 ERun 提取引擎发光",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    buffer_mode: bpy.props.EnumProperty(
        name="缓冲写法",
        items=[
            ('COPY', "copy（官方推荐）", "Resource…FXBuffer = copy Resource\\RabbitFX\\FXBuffer，配合 SetFXBuffer"),
            ('RWBUFFER', "自带 RWBuffer（旧版）", "声明 RWBuffer 并用 ps-u4 直绑，兼容 4.5 以前的 RabbitFX"),
        ],
        default='COPY',
    )

    # ── 颜色偏移 ──
    enable_colorshift: bpy.props.BoolProperty(
        name="颜色偏移 ColorShift",
        description="用 FXMap 的 R 通道作遮罩偏移角色贴图颜色",
        default=False,
    )
    cs_h: bpy.props.FloatProperty(name="H 色相偏移", default=0.0, min=-360, max=360)
    cs_s: bpy.props.FloatProperty(name="S 饱和度偏移", default=0.0, min=-100, max=100)
    cs_v: bpy.props.FloatProperty(name="V 明度偏移", default=0.0, min=-100, max=100)

    # ── 复位 ──
    enable_reset: bpy.props.BoolProperty(
        name="绘制后复位",
        description="每个被修改的 mesh 绘制完成后清空资源别名与变量（RabbitFX 命令列表本身也会复位）",
        default=True,
    )

    # ── 内部 ──
    buffer_tag: bpy.props.StringProperty(
        name="缓冲标识",
        description="内部使用：W-Engine 缓冲资源名与呼吸灯变量的唯一后缀",
        default="",
        options={"HIDDEN"},
    )

    # ── UI 折叠 ──
    show_targets: bpy.props.BoolProperty(default=True)
    show_glow: bpy.props.BoolProperty(default=True)
    show_breath: bpy.props.BoolProperty(default=False)
    show_fx: bpy.props.BoolProperty(default=False)
    show_sync: bpy.props.BoolProperty(default=False)
    show_colorshift: bpy.props.BoolProperty(default=False)

    def _breathing(self):
        """呼吸灯是否生效（需勾选呼吸灯；发光贴图参数关掉时不会执行 Run）。"""
        return bool(self.enable_breath)

    @property
    def fx_profile(self):
        """当前导出逻辑对应的 FX 命名空间档案。

        HIMI（崩坏 3）→ ``HI3FX``；NTEMI → ``NTEMIFX``；其余 → ``RabbitFX``。
        每次取用都重新解析，切逻辑后不需要重开蓝图。
        """
        return fx_namespace.current_profile()

    @property
    def fx_profile_label(self):
        """面板上显示用的「命名空间 + 能力」说明。"""
        profile = self.fx_profile
        limits = []
        if not profile.supports_sync:
            limits.append("无 W-Engine 同步")
        if not profile.supports_colorshift:
            limits.append("无颜色偏移")
        suffix = f"（{'、'.join(limits)}）" if limits else ""
        return f"FX 命名空间：{profile.key}{suffix}"

    def init(self, context):
        super().init(context)
        if not self.buffer_tag:
            self.buffer_tag = uuid.uuid4().hex[:8]
        # 列表要放下「物体选择器 + 图标 + 移除」，默认 300 太窄。
        self.width = 380

    # ──────────────────────────── UI ────────────────────────────

    def draw_buttons(self, context, layout):
        self._draw_targets(layout)
        self._draw_glow_block(layout)   # 内部再画子项呼吸灯
        self._draw_fx_block(layout)     # 内部再画子项同步/颜色偏移

        layout.separator()
        layout.label(text=self.fx_profile_label, icon='INFO')
        layout.prop(self, "enable_reset")

    def _draw_glow_block(self, layout):
        """发光贴图参数（一级折叠）→ 组内第一行是「标题 + 启用勾」（二级）。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_glow", text="",
            icon='TRIA_DOWN' if self.show_glow else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="发光贴图参数", icon='LIGHT')

        if not self.show_glow:
            return

        box = layout.box()
        row = box.row(align=True)
        row.label(text="发光贴图参数", icon='LIGHT')
        row.prop(self, "enable_glow", text="")

        breathing = self._breathing()
        static = box.column(align=True)
        static.enabled = self.enable_glow and not breathing
        r = static.row(align=True)
        r.prop(self, "glow_h")
        r.prop(self, "glow_s")
        r.prop(self, "glow_v")
        static.prop(self, "glow_brightness")
        static.prop(self, "glow_interpolate", slider=True)
        if breathing:
            static.label(text="呼吸灯启用中，静态值被覆盖", icon='INFO')

        # 呼吸灯是发光贴图参数的子项，跟着父级一起展开/收起。
        self._draw_breath_block(box)

    def _draw_breath_block(self, layout):
        """呼吸灯（发光贴图参数的子级）：让上面的变量随时间变化。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_breath", text="",
            icon='TRIA_DOWN' if self.show_breath else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="呼吸灯", icon='ANIM')
        row.prop(self, "enable_breath", text="")

        if not self.show_breath:
            return

        col = layout.column(align=True)
        col.enabled = bool(self.enable_breath)
        col.prop(self, "breath_mode", text="模式")
        col.prop(self, "breath_fps")
        col.prop(self, "breath_step")
        if self.breath_mode == 'SINGLE':
            r = col.row(align=True)
            r.prop(self, "breath_h")
            r.prop(self, "breath_s")
            r.prop(self, "breath_v")
            col.prop(self, "breath_brightness_min")
            col.prop(self, "breath_brightness_max")
        elif self.breath_mode == 'RAINBOW':
            r = col.row(align=True)
            r.prop(self, "breath_s")
            r.prop(self, "breath_v")
            col.prop(self, "breath_brightness_max", text="发光强度")
        else:
            r = col.row(align=True)
            r.prop(self, "breath_s")
            r.prop(self, "breath_v")
            col.prop(self, "breath_brightness_min")
            col.prop(self, "breath_brightness_max")
        col.prop(self, "breath_interpolate", slider=True)

        if self._breathing() and not self.enable_glow:
            layout.label(text="需开启「发光贴图参数」才会执行 Run", icon='ERROR')

    def _draw_fx_block(self, layout):
        """FX 贴图参数（父级）：FX 贴图本身由材质转资源生成，这里只加同步与偏移。"""
        row = layout.row(align=True)
        row.prop(
            self, "show_fx", text="",
            icon='TRIA_DOWN' if self.show_fx else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="FX 贴图参数", icon='TEXTURE')

        if not self.show_fx:
            return

        box = layout.box()
        self._draw_sync_block(box)
        box.separator()
        self._draw_colorshift_block(box)

    def _draw_sync_block(self, layout):
        """W-Engine 同步（FX 贴图参数的子级）。"""
        profile = self.fx_profile
        row = layout.row(align=True)
        row.prop(
            self, "show_sync", text="",
            icon='TRIA_DOWN' if self.show_sync else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="W-Engine 同步", icon='DRIVER')
        row.prop(self, "enable_sync", text="")

        if not self.show_sync:
            return

        if not profile.supports_sync:
            layout.label(
                text=f"{profile.key} 没有 W-Engine 同步，本项不生成任何行",
                icon='INFO',
            )

        col = layout.column(align=True)
        col.enabled = bool(self.enable_sync) and profile.supports_sync
        col.prop(self, "sync_blendmode")
        col.prop(self, "sync_brightness_only")
        col.prop(self, "buffer_mode", text="缓冲写法")
        col.prop(self, "sync_source_object", text="提取源")
        if self.enable_sync and profile.supports_sync:
            layout.label(text="提取源 = 带引擎发光的部件（通常是头发）", icon='INFO')

    def _draw_colorshift_block(self, layout):
        """颜色偏移（FX 贴图参数的子级）。"""
        profile = self.fx_profile
        row = layout.row(align=True)
        row.prop(
            self, "show_colorshift", text="",
            icon='TRIA_DOWN' if self.show_colorshift else 'TRIA_RIGHT', emboss=False,
        )
        row.label(text="颜色偏移 ColorShift", icon='COLOR')
        row.prop(self, "enable_colorshift", text="")

        if not self.show_colorshift:
            return

        if not profile.supports_colorshift:
            layout.label(
                text=f"{profile.key} 没有 ColorShift，本项不生成任何行",
                icon='INFO',
            )

        col = layout.column(align=True)
        col.enabled = bool(self.enable_colorshift) and profile.supports_colorshift
        r = col.row(align=True)
        r.prop(self, "cs_h")
        r.prop(self, "cs_s")
        r.prop(self, "cs_v")
        if self.enable_colorshift and profile.supports_colorshift:
            layout.label(text="FX 贴图 R 通道作为偏移遮罩", icon='INFO')

    def _draw_targets(self, layout):
        row = layout.row(align=True)
        row.prop(
            self,
            "show_targets",
            text="",
            icon='TRIA_DOWN' if self.show_targets else 'TRIA_RIGHT',
            emboss=False,
        )
        row.label(text=f"目标物体列表 ({len(self.target_items)})", icon='OUTLINER_OB_GROUP_INSTANCE')
        row.operator("ssmt.rabbitfx_pro_scan", text="", icon='VIEWZOOM').node_name = self.name

        if not self.show_targets:
            return

        layout.operator(
            "ssmt.rabbitfx_pro_add_selected", icon='RESTRICT_SELECT_OFF'
        ).node_name = self.name

        if _find_material_node_in_tree(self) is None:
            layout.label(
                text="本蓝图里没有「材质转资源」节点：发光/FX 贴图绑定由它生成",
                icon='ERROR',
            )

        if not self.target_items:
            layout.label(text="点扫描按钮从材质转资源收集 FX 物体", icon='INFO')
            return

        list_row = layout.row(align=True)
        # 固定可见行数上限：一两百个物体时靠 UIList 自带的滚动条翻页，
        # 而不是把所有物体一次性铺开把节点撑爆。
        visible_rows = max(3, min(len(self.target_items), 6))
        list_row.template_list(
            "SSMT_UL_RABBITFX_PRO_TARGETS", "",
            self, "target_items",
            self, "active_target_index",
            rows=visible_rows, type='DEFAULT',
        )
        op_col = list_row.column(align=True)
        op_col.operator("ssmt.rabbitfx_pro_add", text="", icon='ADD').node_name = self.name
        op_col.operator("ssmt.rabbitfx_pro_remove_at", text="", icon='REMOVE').node_name = self.name

        obj = self.active_target_object()
        if obj is None:
            return
        layout.label(
            text=(
                f"哈希 {_extract_hash_from_object(obj.name) or '—'}"
                f"   标识 {_extract_resource_suffix(obj.name) or '—'}"
            ),
            icon='INFO',
        )
        duplicate = _objects_used_by_other_nodes(self, obj)
        if duplicate:
            layout.label(text=f"同一物体也被 {duplicate} 接管", icon='ERROR')

    def active_target_object(self):
        """列表里当前高亮行的物体（越界时返回 None）。"""
        items = self.target_items
        index = int(getattr(self, "active_target_index", 0) or 0)
        if 0 <= index < len(items):
            return items[index].target_object
        return None

    # ──────────────────────── 生成入口 ────────────────────────

    def execute_postprocess(self, mod_export_path):
        if not self.buffer_tag:
            self.buffer_tag = uuid.uuid4().hex[:8]

        profile = self.fx_profile
        targets = [item for item in self.target_items if item.target_object is not None]
        sync_source = self.sync_source_object if self.enable_sync else None
        if self.enable_sync and not profile.supports_sync:
            # HI3FX 没有 SetFXBuffer / ERun / UpdateFXBuffer，同步整块不可用。
            print(
                f"[RabbitFXPro] {profile.key} 没有 W-Engine 同步能力，"
                "本次导出跳过同步（包括提取源）"
            )
            sync_source = None
        elif self.enable_sync and sync_source is None:
            print(
                "[RabbitFXPro] 警告: 已开启 W-Engine 同步但未指定「提取源物体」，"
                "引擎发光色不会被提取"
            )
        if not targets and sync_source is None:
            print("[RabbitFXPro] 错误: 未指定任何目标物体")
            return

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            return

        for ini_file in ini_files:
            self._create_cumulative_backup(ini_file, mod_export_path)
            self._process_ini_file(ini_file, targets, sync_source)

    @staticmethod
    def _parse_ini(content):
        """切段。``[mesh:...]`` 是段内的 mesh 块标记，不能当成新段。"""
        sections = OrderedDict()
        preamble = []
        current = None
        for line in str(content or "").splitlines():
            stripped = line.strip()
            is_header = (
                stripped.startswith('[')
                and stripped.endswith(']')
                and not _MESH_LINE_RE.match(stripped)
            )
            if is_header:
                current = stripped
                sections[current] = []
            elif current is not None:
                sections[current].append(line.rstrip())
            else:
                preamble.append(line.rstrip())
        # 去掉段首尾的空行，保证重复导出不会一次多出一个空行。
        for section_lines in sections.values():
            while section_lines and not section_lines[0].strip():
                section_lines.pop(0)
            while section_lines and not section_lines[-1].strip():
                section_lines.pop()
        return sections, preamble

    def _process_ini_file(self, ini_file, targets, sync_source):
        with open(ini_file, 'r', encoding='utf-8') as handle:
            content = handle.read()

        preserved_driver, content = self.split_anim_driver_block_content(content)
        content, tail = self.split_auto_appended_tail_content(content)
        sections, preamble = self._parse_ini(content)

        buffer_name = self.buffer_resource_name()
        # 先一次性圈定所有目标（含提取源）的绘制单元，再统一注入：同一轮里
        # 后处理的目标不会把别的目标刚写进去的参数块当成上一轮残留剥掉。
        jobs, missing_targets, source_missing = self._plan_injections(
            sections, targets, sync_source
        )
        changed = self._apply_injections(sections, jobs, buffer_name)
        for obj, hash_val in missing_targets:
            self._log_missing_target(sections, obj, hash_val)
        if source_missing and sync_source is not None:
            print(f"[RabbitFXPro] W-Engine 源 {sync_source.name}: 未找到可注入的绘制段")
        if self.enable_sync and self.fx_profile.supports_sync and changed:
            changed |= self._ensure_buffer_resource(sections, buffer_name)
        if changed and self.enable_glow and self._breathing():
            changed |= self._ensure_breath_constants(sections)

        if not changed:
            return

        with open(ini_file, 'w', encoding='utf-8') as handle:
            if preserved_driver:
                handle.write(preserved_driver)
                if not preserved_driver.endswith('\n'):
                    handle.write('\n')
                handle.write('\n')
            for line in preamble:
                handle.write(line + '\n')
            for section_name, section_lines in sections.items():
                if section_name.startswith('['):
                    handle.write(f"{section_name}\n")
                for line in section_lines:
                    handle.write(f"{line}\n")
                handle.write("\n")
            if tail:
                handle.write(tail)
        print(f"[RabbitFXPro] 已写入 {os.path.basename(ini_file)}")

    # ──────────────────── 目标物体：参数注入 ────────────────────

    def _plan_injections(self, sections, targets, sync_source):
        """在当前（未改动）内容上一次性圈定所有目标与提取源的绘制单元。

        圈定与实际注入分离：先按未改动的内容把每个目标的单元全部圈好，再统一
        注入。这样同一轮里后处理的目标不会把别的目标刚写进去的参数块当成
        「上一轮残留」剥掉 —— 多个目标共享同一个 IB 时，一个目标只改属于它
        自己的单元（各自的 ``; [mesh:...]`` 块），互不剥离。
        """
        jobs = []
        seen_units = set()
        missing_targets = []

        def collect(obj, kind):
            hash_val = _extract_hash_from_object(obj.name).lower()
            if not hash_val:
                print(f"[RabbitFXPro] 跳过 {obj.name}: 无法解析哈希")
                # None = 名字解析失败，已经单独提示过，不再报「未找到绑定」。
                return hash_val, None
            suffix_pattern = _normalize_suffix(_extract_resource_suffix(obj.name))
            found = self._collect_target_units(sections, obj, hash_val, suffix_pattern)
            if not found:
                return hash_val, False
            for section_name, units in found:
                for unit in units:
                    key = (section_name, int(unit["start"]))
                    if key in seen_units:
                        # 同一单元只注入一次（多个目标落到同一个单元时）。
                        continue
                    seen_units.add(key)
                    jobs.append((section_name, unit, obj.name, kind))
            return hash_val, True

        for item in targets:
            obj = item.target_object
            hash_val, found = collect(obj, "target")
            if found is False:
                missing_targets.append((obj, hash_val))
        source_missing = False
        if sync_source is not None:
            _hash_val, found = collect(sync_source, "source")
            source_missing = found is False
        return jobs, missing_targets, source_missing

    def _apply_injections(self, sections, jobs, buffer_name):
        """按段分组、单元从后往前注入（插行不影响前面单元的索引）。"""
        changed = False
        by_section = OrderedDict()
        for section_name, unit, label, kind in jobs:
            by_section.setdefault(section_name, []).append((unit, label, kind))
        for section_name, entries in by_section.items():
            section_lines = sections.get(section_name)
            if section_lines is None:
                continue
            entries.sort(key=lambda entry: int(entry[0]["start"]), reverse=True)
            for unit, label, kind in entries:
                if kind == "source":
                    changed |= self._patch_sync_source_unit(section_lines, unit, buffer_name)
                else:
                    changed |= self._patch_mesh_unit(section_lines, unit, label, buffer_name)
        return changed

    def _log_missing_target(self, sections, obj, hash_val):
        """没找到贴图绑定时给出可读提示（口径与旧实现一致）。"""
        profile = self.fx_profile
        print(
            f"[RabbitFXPro] 未找到 {obj.name} 的 {profile.key} 贴图绑定，跳过"
            f"（哈希 {hash_val}；确认本节点接在材质转资源之后）"
        )
        if not profile.supports_pro_injection:
            print(
                f"[RabbitFXPro] 提示: 当前逻辑的 FX 命名空间是 {profile.key}，"
                "本节点只对 RabbitFX / HI3FX 注入参数"
            )
            return
        others = [
            found.key
            for found in _binding_namespaces(sections)
            if found.key != profile.key
        ]
        if others:
            print(
                f"[RabbitFXPro] 提示: 该配置表里的绑定用的是 "
                f"{'、'.join(others)} 命名空间，与当前逻辑的 {profile.key} 不一致，"
                "本节点只注入当前逻辑对应的命名空间"
            )

    def _collect_target_units(self, sections, obj, hash_val, suffix_pattern):
        """返回 [(段名, [绘制单元, ...]), ...]：只收属于这个物体自己的单元。

        圈定口径（全局 ``blueprint/ini_scope``）：段 → ``if``/``endif`` 深度配对
        → 行分类 → 绘制单元（``; [mesh:...]`` 起，到下一个兄弟 mesh 注释 / 所在
        条件块 ``endif`` / ``;MARK:`` 之前），单元边界由结构决定，不再按关键词猜。

        单元是否本目标按「IB 键一致 **且** 部件名完全相等」判定 —— 同一 IB 下的
        相邻部件（``右手.001`` 与 ``右乳球.001``）不会再被当成同一个目标。

        不限定 ``[TextureOverride_*]``：EFMI 合并骨骼把绘制内容放在
        ``[CommandList_*]`` 回调段里，ZZMI 的 TTL 重建也会另开段，材质转资源
        写下的 FX 绑定跟着 mesh 单元走，所以只要段里带这个物体的单元就一起处理。
        """
        profile = self.fx_profile
        target_scope = ini_scope.mesh_ib_key(obj.name)
        target_part = _part_name(obj.name)
        result = []
        for section_name, section_lines in sections.items():
            if not section_name.startswith('['):
                continue
            section_scope = ini_scope.section_ib_key(section_name, section_lines)
            units = []
            for unit in ini_scope.iter_draw_units(section_lines):
                mesh_name = unit["mesh"]
                if target_scope is None:
                    # 物体名里没有完整 IB 键时退回「8 位 hash 相同」口径。
                    if _extract_hash_from_object(mesh_name).lower() != hash_val:
                        continue
                if not _unit_matches_target(
                    mesh_name, target_scope, target_part, section_scope
                ):
                    continue
                # 单元内没有 draw / run 时无处可插（旧实现同样跳过这类块）。
                if _unit_anchor(section_lines, unit["start"], unit["end"], profile)[0] < 0:
                    continue
                units.append(unit)
            if units:
                result.append((section_name, units))
        if result:
            return result

        # 回退：段里**完全没有 mesh 注释**（名字被映射/合并过）时，按 hash + 段名
        # 匹配整段；整段内的定位同样走 ini_scope 的行分类（第一个 draw / run）。
        for section_name, section_lines in sections.items():
            if not section_name.startswith('[TextureOverride'):
                continue
            if ini_scope.section_mesh_names(section_lines):
                continue
            if not any(
                str(line).strip().lower().startswith('hash =')
                and str(line).split('=', 1)[1].strip().lower() == hash_val
                for line in section_lines
            ):
                continue
            if suffix_pattern and suffix_pattern not in section_name.replace('-', '_').lower():
                continue
            if _collect_existing_refs(section_lines, 0, len(section_lines), profile)[2] < 0:
                continue
            result.append((section_name, [_section_fallback_unit(section_lines)]))
        return result

    def _patch_mesh_unit(self, lines, unit, object_label, buffer_name):
        """在结构化圈定的一个单元内注入参数块与复位块。

        参数块插在本单元的 ``Run`` / ``draw`` 之前（取更早者，见
        :func:`_unit_anchor`）；复位块插在 draw 之后、下一个兄弟 mesh / 条件块
        ``endif`` 之前（见 :func:`_reset_insert_position`），不会写到 endif 之外。
        去重集合 = 单元内容 + 紧邻的块外复位行（材质转资源写的），已有的行不再写。
        缩进跟随锚点行 / 末尾行，条件块里嵌套的材质切换 ``if`` 不会被拆散。
        """
        profile = self.fx_profile
        start, scope_end = _unit_edit_scope(lines, unit)
        # 块外紧邻的复位行（材质转资源pro 写在 endif 之后）也要算「已存在」。
        trailing_reset_lines = _collect_trailing_reset_lines(lines, scope_end)
        block = _strip_previous_blocks(lines[start:scope_end])
        glow_ref, fxmap_ref, run_index = _collect_existing_refs(block, 0, len(block), profile)
        anchor, draw_index = _unit_anchor(block, 0, len(block), profile)
        if anchor < 0:
            return False
        if not glow_ref and not fxmap_ref and run_index < 0:
            return False

        param_lines, wrote_glow, wrote_sync = self._build_param_lines(
            glow_ref, fxmap_ref, object_label, buffer_name
        )
        cs_lines = self._build_colorshift_lines(fxmap_ref, object_label)
        wrote_colorshift = bool(cs_lines)
        # 只写复位、不写任何参数时不动文件（"只对列表里的物体生成配置"）。
        if not param_lines and not cs_lines:
            return False

        # 锚点到 draw 之间没有本命名空间的 Run 时补一条，保证参数在本次 draw
        # 前被执行（块内 run 排在 draw 之后就是实机 bug 的成因）。
        run_limit = draw_index if draw_index >= 0 else len(block)
        run_position = _profile_run_between(block, anchor, run_limit, profile)
        if param_lines and run_position is None:
            param_lines = [*param_lines, profile.run_line]
        # ColorShift 必须在 Run 之后（Run 结尾会把 ResourceFXMap 清空）。
        cs_position = (
            run_position + 1 if run_position is not None else anchor + len(param_lines)
        )

        anchor_indent = _leading_whitespace(block[anchor]) if block else ""
        insertions = []
        if cs_lines:
            insertions.append((cs_position, cs_lines, anchor_indent))
        if param_lines:
            insertions.append((anchor, param_lines, anchor_indent))
        if self.enable_reset:
            reset_lines = self._build_reset_lines(
                block,
                wrote_glow,
                wrote_sync,
                wrote_colorshift,
                trailing_reset_lines,
            )
            if reset_lines:
                insertions.append(
                    (
                        _reset_insert_position(unit, block),
                        reset_lines,
                        _trailing_indent(block),
                    )
                )

        # 先插靠后的位置，前面的索引才不会位移。
        for position, block_lines, indent in sorted(
            insertions, key=lambda item: item[0], reverse=True
        ):
            _insert_block(block, position, block_lines, indent)

        lines[start:scope_end] = block
        return True

    def _build_param_lines(self, glow_ref, fxmap_ref, object_label, buffer_name):
        """返回 (行, 是否写了发光参数, 是否写了 W-Engine 同步参数)。"""
        profile = self.fx_profile
        lines = []
        wrote_glow = False
        wrote_sync = False
        if self.enable_glow and glow_ref:
            breathing = self._breathing()
            lines.extend(
                self._build_breath_lines() if breathing else self._build_static_glow_lines()
            )
            wrote_glow = True
        elif self.enable_glow:
            print(f"[RabbitFXPro] {object_label}: 没有发光贴图绑定，跳过发光参数")
        if self.enable_sync and not profile.supports_sync:
            # HI3FX 没有 SetFXBuffer / ERun / blendmode，硬写只会产生无效指令。
            print(
                f"[RabbitFXPro] {object_label}: {profile.key} 没有 W-Engine 同步能力，"
                "已跳过同步参数"
            )
        elif self.enable_sync and (glow_ref or fxmap_ref):
            lines.extend(self._build_sync_lines(buffer_name))
            wrote_sync = True
        if not lines:
            return [], False, False
        return [_PARAM_BEGIN, *lines, _PARAM_END], wrote_glow, wrote_sync

    def _build_static_glow_lines(self):
        param = self.fx_profile.param
        return [
            f"{param('h')} = {_format_float(self.glow_h)}",
            f"{param('s')} = {_format_float(self.glow_s)}",
            f"{param('v')} = {_format_float(self.glow_v)}",
            f"{param('brightness')} = {_format_float(self.glow_brightness)}",
            f"{param('interpolate')} = {_format_float(self.glow_interpolate)}",
        ]

    def _build_breath_lines(self):
        tag = self._var_tag()
        frame = f"{_VAR_PREFIX}{tag}_frame"
        valve = f"{_VAR_PREFIX}{tag}_valve"
        glow = f"{_VAR_PREFIX}{tag}_glow"
        fps = int(self.breath_fps)
        step = _format_number(self.breath_step)
        lines = [
            _BREATH_BEGIN,
            f"{frame} = {frame} + {step}",
            f"if {frame} >= {fps}",
            f"    {frame} = {frame} - {fps}",
            f"    {valve} = 1 - {valve}",
            "endif",
            f"if {valve} == 0",
            f"    {glow} = {frame}",
            "else",
            f"    {glow} = {fps} - {frame}",
            "endif",
        ]
        bmin = _format_number(self.breath_brightness_min)
        bmax = _format_number(self.breath_brightness_max)
        param = self.fx_profile.param
        if self.breath_mode == 'SINGLE':
            lines.extend(
                [
                    f"{param('h')} = {_format_number(self.breath_h)}",
                    f"{param('s')} = {_format_number(self.breath_s)}",
                    f"{param('v')} = {_format_number(self.breath_v)}",
                    f"{param('brightness')} = {bmin} + ({bmax} - {bmin}) * {glow} / {fps}",
                ]
            )
        elif self.breath_mode == 'RAINBOW':
            lines.extend(
                [
                    f"{param('h')} = {glow} * {_format_number(360.0 / fps)}",
                    f"{param('s')} = {_format_number(self.breath_s)}",
                    f"{param('v')} = {_format_number(self.breath_v)}",
                    f"{param('brightness')} = {bmax}",
                ]
            )
        else:  # COMBO
            lines.extend(
                [
                    f"{param('h')} = {glow} * {_format_number(360.0 / fps)}",
                    f"{param('s')} = {_format_number(self.breath_s)}",
                    f"{param('v')} = {_format_number(self.breath_v)}",
                    f"{param('brightness')} = {glow} * {_format_number(float(self.breath_brightness_max) / fps)}",
                ]
            )
        lines.extend(
            [f"{param('interpolate')} = {_format_number(self.breath_interpolate)}", _BREATH_END]
        )
        return lines

    def _build_sync_lines(self, buffer_name):
        lines = [f"$\\rabbitfx\\blendmode = {int(self.sync_blendmode)}"]
        if self.sync_brightness_only:
            lines.append("$\\rabbitfx\\syncbrightnessonly = 1")
        if self.buffer_mode == 'RWBUFFER':
            lines.append(f"ps-u4 = {buffer_name}")
        else:
            lines.append(f"{SET_FX_BUFFER_REF} = ref {buffer_name}")
        return lines

    def _build_colorshift_lines(self, fxmap_ref, object_label):
        profile = self.fx_profile
        if not self.enable_colorshift:
            return []
        if not profile.supports_colorshift:
            # HI3FX 没有 ColorShift 命令列表，写出来只会是无效指令。
            print(
                f"[RabbitFXPro] {object_label}: {profile.key} 没有 ColorShift 能力，"
                "已跳过颜色偏移"
            )
            return []
        if not fxmap_ref:
            print(f"[RabbitFXPro] {object_label}: 没有 FXMap 绑定，跳过 ColorShift")
            return []
        param = profile.param
        return [
            _CS_BEGIN,
            f"{profile.fxmap_ref} = ref {fxmap_ref}",
            f"{param('h')} = {_format_float(self.cs_h)}",
            f"{param('s')} = {_format_float(self.cs_s)}",
            f"{param('v')} = {_format_float(self.cs_v)}",
            COLOR_SHIFT_LINE,
            _CS_END,
        ]

    def _build_reset_lines(
        self, block, wrote_glow, wrote_sync, wrote_colorshift, extra_lines=()
    ):
        """只复位本节点真正写过的东西；材质转资源已有的复位行不重复写。

        RabbitFX/NTEMIFX：运行时 ``Run`` 结尾自带变量复位，所以写 0 + 再 Run 一次即可。
        HI3FX：``Run`` **不**复位变量，写 0 会让后续绘制一直吃 ``brightness = 0``
        （模型变黑），所以改成调 ``CommandList\\HI3FX\\Reset``。

        ``extra_lines`` 是单元区间**之外**紧邻的复位行（材质转资源写在 ``endif``
        之后的那几行）：它们同样算「已存在」，否则会写出块内块外两套等价复位。
        比较用 :func:`_reset_line_key` 归一化（``= ref null`` ↔ ``= null``、
        ``0`` ↔ ``0.0``）。
        """
        profile = self.fx_profile
        candidates = []
        if wrote_glow:
            candidates.append(f"{profile.glow_ref} = ref null")
        if wrote_colorshift:
            candidates.append(f"{profile.fxmap_ref} = ref null")
        if wrote_sync:
            candidates.append(f"{SET_FX_BUFFER_REF} = ref null")
        if profile.reset_neutralises_variables:
            param = profile.param
            if wrote_glow or wrote_colorshift:
                candidates.extend(
                    [
                        f"{param('h')} = 0",
                        f"{param('s')} = 0",
                        f"{param('v')} = 0",
                    ]
                )
            if wrote_glow:
                candidates.extend(
                    [
                        f"{param('brightness')} = 0",
                        f"{param('interpolate')} = 0",
                    ]
                )
        elif wrote_glow or wrote_colorshift or wrote_sync:
            candidates.append(profile.reset_line)
        if wrote_sync:
            candidates.append("$\\rabbitfx\\blendmode = 0")
            if self.sync_brightness_only:
                candidates.append("$\\rabbitfx\\syncbrightnessonly = 0")
        existing = {
            _reset_line_key(line)
            for line in list(block) + list(extra_lines or ())
        }
        lines = [line for line in candidates if line and _reset_line_key(line) not in existing]
        if not lines:
            return []
        return [_RESET_BEGIN, *lines, _RESET_END]

    # ──────────────────── W-Engine 提取源 ────────────────────

    def _patch_sync_source_unit(self, lines, unit, buffer_name):
        """在提取源自己的绘制单元里插 ERun 块（单元圈定口径与目标相同）。"""
        profile = self.fx_profile
        start, scope_end = _unit_edit_scope(lines, unit)
        trailing_reset_lines = _collect_trailing_reset_lines(lines, scope_end)
        block = _strip_previous_blocks(lines[start:scope_end])
        anchor, _draw_index = _unit_anchor(block, 0, len(block), profile)
        if anchor < 0:
            return False

        block_lines = [_SYNC_BEGIN]
        if self.buffer_mode == 'RWBUFFER':
            # 旧版直绑：UpdateFXBuffer 会解绑 ps-u4，所以这条路径不用 pre run。
            block_lines.append(f"ps-u4 = {buffer_name}")
            block_lines.append(E_RUN_LINE)
        else:
            # 3DMigoto 资源拷贝：自定义资源先声明空段，拷贝语句写在绘制段里执行
            # （官方 Resource Copying 文档）。每帧先按 RabbitFX 自己的 FXBuffer
            # 重建一份，再交给 ERun 写入引擎发光色，目标部件只读同一份资源。
            block_lines.append(f"{buffer_name} = copy Resource\\RabbitFX\\FXBuffer")
            block_lines.append(f"{SET_FX_BUFFER_REF} = ref {buffer_name}")
            block_lines.append(UPDATE_BUFFER_LINE)
            block_lines.append(E_RUN_LINE)
        block_lines.append(_SYNC_END)
        _insert_block(block, anchor, block_lines, _leading_whitespace(block[anchor]))

        if self.enable_reset:
            reset_line = f"{SET_FX_BUFFER_REF} = ref null"
            existing = {
                _reset_line_key(line)
                for line in list(block) + list(trailing_reset_lines)
            }
            if _reset_line_key(reset_line) not in existing:
                _insert_block(
                    block,
                    _reset_insert_position(unit, block),
                    [_RESET_BEGIN, reset_line, _RESET_END],
                    _trailing_indent(block),
                )

        lines[start:scope_end] = block
        return True

    # ──────────────────── 资源与变量声明 ────────────────────

    def buffer_resource_name(self):
        return f"ResourceRabbitFXPro_{self._var_tag()}_FXBuffer"

    def _ensure_buffer_resource(self, sections, buffer_name):
        section_name = f"[{buffer_name}]"
        if section_name in sections:
            return False
        if self.buffer_mode == 'RWBUFFER':
            sections[section_name] = ["type = RWBuffer", "format = R32_FLOAT", "array = 100"]
        else:
            # 自定义资源以空段声明；拷贝语句在提取源/目标部件的绘制段里执行。
            sections[section_name] = []
        return True

    def _ensure_breath_constants(self, sections):
        tag = self._var_tag()
        wanted = [
            f"global persist {_VAR_PREFIX}{tag}_frame = 0",
            f"global {_VAR_PREFIX}{tag}_valve = 0",
            f"global {_VAR_PREFIX}{tag}_glow = 0",
        ]
        constants = sections.setdefault('[Constants]', [])
        existing = {str(line).strip() for line in constants}
        added = [line for line in wanted if line not in existing]
        if not added:
            return False
        constants.extend(added)
        return True

    def _var_tag(self):
        tag = re.sub(r"[^0-9a-zA-Z_]", "_", str(self.buffer_tag or "")).strip("_")
        return tag or "0"

    # ──────────────────── 校验 ────────────────────

    def validate_export_configuration(self):
        conflicts = []
        for item in self.target_items:
            obj = item.target_object
            if obj is None:
                continue
            owners = _objects_used_by_other_nodes(self, obj)
            if owners:
                conflicts.append(f"{obj.name}（也被 {owners} 接管）")
        if conflicts:
            raise ValueError(
                "RabbitFX贴图后处理pro：同一物体被多个同类节点重复接管，参数会互相覆盖："
                + "、".join(conflicts)
            )


def _objects_used_by_other_nodes(node, obj):
    """同一蓝图里其它 RabbitFXPro 节点是否也在管这个物体。"""
    tree = getattr(node, "id_data", None)
    if tree is None or obj is None:
        return ""
    owners = []
    for other in getattr(tree, "nodes", []) or []:
        if other == node or getattr(other, "bl_idname", "") != NODE_IDNAME:
            continue
        if any(item.target_object == obj for item in getattr(other, "target_items", []) or []):
            owners.append(other.name)
        elif getattr(other, "sync_source_object", None) == obj:
            owners.append(f"{other.name}(提取源)")
    return "、".join(owners)


# ──────────────────────── 运算符 ────────────────────────

def _active_pro_node(context):
    node = getattr(context, "active_node", None)
    if node is not None and getattr(node, "bl_idname", "") == NODE_IDNAME:
        return node
    return None


def _node_by_name(context, node_name):
    """按名字在节点树里取本节点。

    节点内部的按钮点下去时，``context.active_node`` 未必是画这个按钮的节点
    （按钮会吃掉鼠标事件，活动节点可能还是上一个点过的节点），所以按钮必须
    带上 ``node_name``，这里优先按名字查树，再退回活动节点。
    """
    space = getattr(context, "space_data", None)
    tree = None
    if space is not None:
        tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    if tree is None:
        tree = getattr(context, "edit_tree", None) or getattr(context, "node_tree", None)
    nodes = getattr(tree, "nodes", None) if tree is not None else None
    if nodes is not None and node_name:
        found = nodes.get(node_name)
        if found is not None and getattr(found, "bl_idname", "") == NODE_IDNAME:
            return found
    return _active_pro_node(context)


def _resolve_node(operator, context):
    """取本节点；取不到时报出可读原因，避免按钮点了没反应。"""
    node = _node_by_name(context, getattr(operator, "node_name", ""))
    if node is None:
        operator.report({"WARNING"}, "没找到 RabbitFX贴图后处理pro 节点（请直接点节点面板上的按钮）")
    return node


def _selected_mesh_objects(context):
    """当前选中的网格物体；context 里取不到时退回视图层与全局选择。"""
    selected = [
        obj for obj in (getattr(context, "selected_objects", None) or [])
        if getattr(obj, "type", "") == "MESH"
    ]
    if selected:
        return selected

    view_layer = getattr(context, "view_layer", None)
    if view_layer is None:
        view_layer = getattr(getattr(bpy, "context", None), "view_layer", None)
    try:
        layer_selected = list(getattr(getattr(view_layer, "objects", None), "selected", None) or [])
    except Exception:
        layer_selected = []
    selected = [obj for obj in layer_selected if getattr(obj, "type", "") == "MESH"]
    if selected:
        return selected

    try:
        objects = list(getattr(bpy.data, "objects", None) or [])
    except Exception:
        objects = []
    for obj in objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        select_get = getattr(obj, "select_get", None)
        if callable(select_get) and select_get():
            selected.append(obj)
    return selected


class SSMT_OT_RabbitFXProScan(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_scan"
    bl_label = "扫描 FX 贴图物体"
    bl_description = (
        "从「材质转资源(pro)」的部件范围里收集所有使用 Glowmap 发光贴图 / FXMap "
        "裁切贴图的物体，填充到本节点的物体列表"
    )
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _resolve_node(self, context)
        if node is None:
            return {"CANCELLED"}

        candidates, source_label = _collect_scan_candidates(node)
        if not candidates:
            self.report({"WARNING"}, f"未从{source_label}找到任何部件")
            return {"CANCELLED"}

        found = []
        for obj in candidates:
            has_glow, has_fx = _probe_fx_textures(obj)
            if has_glow or has_fx:
                found.append((obj, has_glow, has_fx))

        node.target_items.clear()
        for obj, has_glow, has_fx in found:
            item = node.target_items.add()
            item.target_object = obj
            item.has_glow = has_glow
            item.has_fx = has_fx
        node.active_target_index = 0

        glow_count = sum(1 for _o, has_glow, _f in found if has_glow)
        fx_count = sum(1 for _o, _g, has_fx in found if has_fx)
        self.report(
            {"INFO"},
            f"从{source_label}扫描到 {len(found)} 个 FX 物体"
            f"（发光贴图 {glow_count} / FX 贴图 {fx_count}）",
        )
        return {"FINISHED"}


def _collect_scan_candidates(node):
    """返回 (物体列表, 来源说明)。优先跟随「材质转资源(pro)」的部件范围。"""
    from .node_postprocess_custom_material_assign import _connected_blueprint_object_names

    tree = getattr(node, "id_data", None)
    material_node = None
    if tree is not None:
        for candidate in getattr(tree, "nodes", []) or []:
            if getattr(candidate, "bl_idname", "") in (
                MATERIAL_NODE_IDNAME,
                LEGACY_MATERIAL_NODE_IDNAME,
            ) and not getattr(candidate, "mute", False):
                material_node = candidate
                break

    objects = []
    seen = set()

    def add(obj):
        if obj is None or getattr(obj, "type", "") != "MESH" or obj.name in seen:
            return
        seen.add(obj.name)
        objects.append(obj)

    if material_node is not None:
        if bool(getattr(material_node, "use_global_assign", False)):
            for name in _connected_blueprint_object_names(tree):
                add(bpy.data.objects.get(name))
            return objects, "材质转资源(pro) 全局扫描范围"
        for item in getattr(material_node, "target_items", []) or []:
            add(getattr(item, "target_object", None))
        if objects:
            return objects, "材质转资源(pro) 目标列表"

    if tree is not None:
        for name in _connected_blueprint_object_names(tree):
            add(bpy.data.objects.get(name))
    return objects, "蓝图链路（未找到材质转资源节点）"


class SSMT_OT_RabbitFXProAdd(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_add"
    bl_label = "添加一行"
    bl_description = "在列表末尾加一个空行，再手动指定物体"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _resolve_node(self, context)
        if node is None:
            return {"CANCELLED"}
        node.target_items.add()
        node.active_target_index = len(node.target_items) - 1
        return {"FINISHED"}


class SSMT_OT_RabbitFXProRemoveAt(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_remove_at"
    bl_label = "移除该物体"
    bl_description = "移除这一行；不指定 index 时移除列表里高亮的那一行"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()
    index: bpy.props.IntProperty(default=-1, min=-1)

    def execute(self, context):
        node = _resolve_node(self, context)
        if node is None:
            return {"CANCELLED"}
        index = self.index if self.index >= 0 else int(getattr(node, "active_target_index", 0) or 0)
        if 0 <= index < len(node.target_items):
            node.target_items.remove(index)
            node.active_target_index = max(0, min(index, len(node.target_items) - 1))
        return {"FINISHED"}


class SSMT_OT_RabbitFXProAddSelected(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_pro_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前选中的所有网格物体一次性加入列表（已在列表中的跳过）"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _resolve_node(self, context)
        if node is None:
            return {"CANCELLED"}

        selected = _selected_mesh_objects(context)
        if not selected:
            self.report({"WARNING"}, "没有选中任何网格物体：请先在 3D 视口或大纲里选中物体")
            return {"CANCELLED"}

        existing = {item.target_object for item in node.target_items if item.target_object}
        added = 0
        for obj in selected:
            if obj in existing:
                continue
            item = node.target_items.add()
            item.target_object = obj
            item.has_glow, item.has_fx = _probe_fx_textures(obj)
            existing.add(obj)
            added += 1

        if added:
            node.active_target_index = len(node.target_items) - 1
            self.report({"INFO"}, f"已加入 {added} 个物体")
        else:
            self.report({"INFO"}, "选中的物体都已在列表中")
        return {"FINISHED"}


_classes = (
    RabbitFXProTargetItem,
    SSMT_UL_RabbitFXProTargets,
    SSMTNode_PostProcess_RabbitFXPro,
    SSMT_OT_RabbitFXProScan,
    SSMT_OT_RabbitFXProAdd,
    SSMT_OT_RabbitFXProRemoveAt,
    SSMT_OT_RabbitFXProAddSelected,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
