# -*- coding: utf-8 -*-
"""条件判断后处理节点。

用途：节点上选定若干 Blender 物体并填一个哈希；导出 ini 后，当该哈希在
游戏画面中出现时，这些物体所属 IB 的全部 ini 代码才生效；哈希不出现时
整段代码不执行（效果等同删掉这些代码，游戏恢复原生绘制）。

目标段的来源（结构化登记，**不扫描 ini 文本**）
----------------------------------------------
目标段不是从导出后的 ini 文本里按段名 / hash 关键词搜出来的，而是：

1. **物体 -> draw_ib**：从本次导出的 ``BluePrintModel``
   （``ordered_draw_obj_data_model_list`` 里的 ``DrawCallModel``）反查选定
   物体所属的 draw_ib。**合并骨骼（MergedSkeleton）导出**下，被合并的部件
   没有独立的导出对象（几何已并入别的 IB，原对象只剩占位小三角面），
   DrawCallModel 里按名字查不到，于是再用物体名开头的 8 位 IB hash 兜底补漏
   （见 ``_collect_draw_ibs_for_objects``）。VB 段、贴图修改段的段名 /
   ``hash =`` 是 **VB hash / 贴图 hash**，与物体 IB hash 不同，按关键词匹配
   必然漏段；
2. **draw_ib -> 段名**：读导出期 ``common/export_section_registry`` 登记的
   「这个 draw_ib 产出了哪些 ini 段」，再排除 ``Resource*``（资源声明段）
   与 ``TextureOverrideCheckHash*``（本节点自己的检测段）。

   列表里的物体**不在节点链上**时也能门控：「材质转资源pro」的**纯贴图模式**
   （目标物体不在导出链、只生成贴图替换段）会把它凭空生成的
   ``[TextureOverride_MaterialPureTexture_<hash>]`` 段登记进同一张登记表
   （段类型 ``TextureOverrideMaterialPureTexture``），本节点据此把它纳入目标段。
   这类段自带顶层的 ``Resource`` 槽位别名 ``= null`` 兜底行（纯贴图模式负责写），
   门控关闭时才回落原生贴图，而不是残留同帧别的部件的 mod 贴图。

「哈希出现」的检测方式（两种，优先第一种）
------------------------------------------

检测 hash 在本 mod 里被接管时（导出期段登记里有该 hash 的
``TextureOverride*`` 段），**不再**另建独立检测段，而是把置位块

.. code-block:: ini

    ; @@COND:<ns>:detect@@
    $cond_<ns>_cond_frame = 1

插进该 hash 自己的每个 ``TextureOverride*`` 段的**段体最前面**（段头 + 段根
匹配键之后、任何门控 ``if`` 之前，且必须在顶层）。原因（实机日志证据）：

- 独立检测段 ``[TextureOverrideCheckHash_cond_<ns>]`` 的 ``hash = <检测hash>``
  与同 hash 的 ``_IB_<hash>`` / ``_LOD0.<hash>_*`` 段撞车，3Dmigoto 报
  ``Possible Mod Conflict: Duplicate TextureOverride hash=...``，
  执行顺序不确定 -> 条件变量在 0/1 间摆动或根本不执行 -> 模型在 mod / 原生
  之间跳变 -> 崩坏；
- 把置位写进已被本 mod 接管的段（它本来就按 hash 命中），既没有重复 hash，
  也不依赖执行顺序。

检测 hash 未被本 mod 接管（登记里没有它的段）时，**回退**到原来的独立检测段
``[TextureOverrideCheckHash_cond_<ns>]`` —— 此时没有同 hash 竞争，安全。

机制（3Dmigoto ini 语义）：

- 检测方式二选一：段内置位（见上）或独立检测段 ``[TextureOverrideCheckHash_cond_<ns>]``
  （用 ``hash = <哈希>``，可选 ``match_index_count``）匹配画面中的绘制，
  命中时把「本帧检测标志」``$cond_<ns>_cond_frame`` 置 1；
- 每个目标段（导出期登记里属于这些 draw_ib 的段）的**可门控行 / 块**
  分别包进 ``if $cond_<ns>_cond_active == 1``（一个段可以产生多对门控块）；
  ``run =`` / ``vs-t0 =`` / ``$变量`` 赋值 / ``ResourceZZPalette*`` 捕获
  与未识别行**一律留在门控 if 之外**，因为本 3DMigoto fork 的加载期优化
  会让 if 体内的 ``run`` 不执行、把只在 if 内赋值的 ``$变量`` 按初值静态
  折叠（合并骨架 attach 不执行 -> 骨架为空 -> 模型崩坏，实测踩坑）；
  匹配键行（``hash`` / ``match_first_index`` / ``match_index_count`` /
  ``match_priority`` / ``allow_duplicate_hash`` / ``filter_index``）必须留在
  段根，不能放进 ``if`` 里（3Dmigoto 会忽略包住它们的 if，但保持段根更安全）；
- ``[Constants]`` 里 ``global`` 声明两个变量，``[Present]`` 每帧末先锁存再清零::

    post $cond_<ns>_cond_active = $cond_<ns>_cond_frame
    post $cond_<ns>_cond_frame = 0

  即 ``cond_active`` 的语义 = **上一帧是否命中**。门控只用 ``cond_active``
  （整帧恒定的锁存值）。不能直接把「本帧标志」用于跨段门控：检测 hash 的段
  与目标段的 draw 在帧内顺序不固定，同一帧里 IB 段（``handling = skip``）与
  LOD0 段（``drawindexed``）可能看到不同的值 -> 跳过了原绘制却没执行自定义
  绘制、或原绘制与自定义绘制同时执行 -> 模型错乱（2026-10-22 实机踩坑，
  「翅膀好了一下子又不行」）；
- 段内没有 draw 命令时游戏原 draw 照常执行，所以「整段门控」就等于
  「这段代码不存在」。

幂等：本节点插入的每一行都带 ``; @@COND:<ns>:`` 标记。重复执行时先按标记
剥掉本节点上一次插入的行（包裹块保留内容、只删标记与 ``if``/``endif``
两行；检测段整块删除后重建；段内置位块的 ``detect`` 标记与紧随的置位行
一起删），再按当前设置改写，因此不会无限嵌套。

命名空间 ``<ns>`` 由节点名 md5 前 8 位派生；节点改名等于换命名空间
（旧标记不再被本节点剥离，见报告中的已知限制）。

与 ``node_postprocess_skip`` 的分工：skip 节点**追加** IBSkip 段（让部件
不显示），本节点**包裹**已有段（让整段代码有条件地不执行）。
"""

import bpy
import hashlib
import os
import re

from .node_postprocess_base import SSMTNode_PostProcess_Base

# 复用 skip 节点的段头解析口径（``[SectionName]``），保持两个节点的分节理解一致。
# skip 只 import base，不存在循环依赖。
from .node_postprocess_skip import (
    _SECTION_HEADER_RE,
    _find_sibling_ini_sections,
    _strip_runtime_suffixes,
)


NODE_IDNAME = "SSMTNode_PostProcess_Condition"
NODE_LABEL = "条件判断"

#: 本节点插入行的统一标记前缀（后面接 ``<ns>:begin@@`` 等）。
COND_MARKER_PREFIX = "; @@COND:"

#: 段根匹配键：这些行必须留在 ``if`` 之外。
_MATCH_KEY_RE = re.compile(
    r"^(hash|match_first_index|match_index_count|match_priority|allow_duplicate_hash|filter_index)\s*=",
    re.IGNORECASE,
)

#: 物体名开头的 ``LOD0.`` / ``LOD1.`` 前缀。
_OBJECT_LOD_PREFIX_RE = re.compile(r"^LOD\d+[.]", re.IGNORECASE)
#: 物体名开头的 8 位 hash（``LOD0.`` 剥掉之后取）。
_OBJECT_HASH_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{8}")
#: 兜底：物体名中夹着的 8 位 hash，要求前后是分隔符（``-`` / ``_`` / ``.`` / 行尾）。
_OBJECT_HASH_FALLBACK_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{8})(?=[-_.]|$)")

#: 变量条件：变量名合法性（可带一个 ``$`` 前缀；``\`` 用作命名空间分隔）。
_VAR_NAME_RE = re.compile(r"^\$?[A-Za-z_\\][A-Za-z0-9_\\]*$")
#: 节点自己的变量命名空间（``cond_<8位hex>_``）：用户变量用这个前缀会被每帧复位。
_RESERVED_VAR_RE = re.compile(r"^cond_[0-9a-fA-F]{8}_")
#: 本节点标记里的命名空间：用于识别「其它 ns」的残留门控块。
_COND_NS_RE = re.compile(r";\s*@@COND:([0-9a-fA-F]{8}):")


class ConditionApplyError(RuntimeError):
    """配置表结构不允许安全改写时抛出：调用方必须报错并**拒绝写入**。

    典型场景：目标 ini 没有 [Present] 段，而唯一能插入新段的位置落在「自动追加
    尾块」里 —— 那样插进去的复位行下次导出会被整块重写掉，等于门控变量永不
    复位、部件效果永久消失。宁可拒绝也不静默放过。
    """


class ConditionValidationError(ValueError):
    """条件列表里有非法项（变量名 / 数值 / 算子）：调用方必须报错并拒绝写入。"""


def _log_info(message):
    """走 utils/log_utils 的 LOG；导入不可用（独立加载 / 测试）时退回 print。"""
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


def _log_error(message):
    """error 级别版本：用于本节点放弃改写（返回 False）的位置。"""
    try:
        from ..utils.log_utils import LOG
    except Exception:
        print(message)
        return
    LOG.error(message)


# --------------------------------------------------------------------------- #
# 纯函数（不依赖 bpy，便于 headless 测试）
# --------------------------------------------------------------------------- #

def _ensure_newline(line, newline):
    """保证一行以换行结尾（文件末行可能没有换行）。

    ``\\r`` 也算换行：孤立 CR 文件（老 Mac 风格）里不能再补一个 ``\\r\\n``，
    否则会出现 ``\\r\\r\\n``。
    """
    text = str(line or "")
    return text if text.endswith(("\n", "\r")) else text + newline


def _parse_hash_from_object_name(name):
    """从物体名解析 8 位 IB hash；解析不出返回 ""。

    样例：``LOD0.9004a39a-20472-0.触手身体翅膀`` -> ``9004a39a``。
    先剥 ``LODx.`` 前缀再取开头的 8 位十六进制；失败时兜底搜索名中
    前后带分隔符的 8 位十六进制（避免把 ``Body_copy`` 这类名字误判）。
    """
    value = str(name or "").strip()
    if not value:
        return ""
    value = _OBJECT_LOD_PREFIX_RE.sub("", value)
    match = _OBJECT_HASH_PREFIX_RE.match(value)
    if match:
        return match.group(0).lower()
    fallback = _OBJECT_HASH_FALLBACK_RE.search(value)
    return fallback.group(1).lower() if fallback else ""


def _safe_index_count(value):
    """把 IndexCount 文本转成正整数；空 / 非法返回 ``None``。"""
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def _get_section_registry():
    """软导入「导出段登记表」；不可用时返回 ``None``。

    放在函数内导入：正常包里 ``blueprint`` 是子包，``from ..common import`` 可用；
    但独立加载本文件的测试环境可能没有完整包层级，不能让导入失败拖垮节点注册。
    """
    try:
        from ..common import export_section_registry
    except Exception:
        return None
    return export_section_registry


def _name_aliases(name, mapping):
    """返回一个物体名的等价名集合（本身 + 名称映射的正向 / 反向结果）。

    ``BluePrintModel._object_name_mapping`` 记的是「原物体名 -> 运行时重命名后的
    名字」；节点列表里存的可能是改名前的 Blender 名，导出名可能是改名后的，
    所以比较前把两边都展开成等价名集合，避免改名导致物体匹配不上。
    """
    text = str(name or "").strip()
    if not text:
        return set()
    aliases = {text}
    mapped = str((mapping or {}).get(text) or "").strip()
    if mapped:
        aliases.add(mapped)
    for key, value in (mapping or {}).items():
        if str(value or "").strip() == text:
            key_text = str(key or "").strip()
            if key_text:
                aliases.add(key_text)
    return aliases


def _normalize_object_name(name):
    """物体名归一化：去首尾空白 + 剥运行时副本后缀 + casefold。

    导出期 ``DrawCallModel`` 里的名字常带 ``_copy`` / ``_dup1`` / ``_chain2``
    等运行时后缀（例如 ``LOD0.9004a39a-20472-0.触手身体翅膀_copy``），而节点上
    存的是用户在 Blender 里选中的原始名；比较前必须统一剥掉这些后缀。
    """
    return _strip_runtime_suffixes(str(name or "").strip()).casefold()


def _collect_draw_ibs_for_objects(model, object_names):
    """从导出期 BluePrintModel 的 DrawCallModel 反查选中物体所属 draw_ib 集合。

    候选名集合 = ``{obj_name, source_obj_name, get_blender_obj_name()}`` 再叠加
    名称映射的正 / 反向结果；与选定物体名做**精确比较**（去首尾空白）。命中则
    收集该 DrawCallModel 的 ``match_draw_ib``（非空、去重）。

    **兜底（合并骨骼导出）**：``MergedSkeleton`` 下部分部件的几何被并进别的 IB、
    原对象只剩占位小三角面，``ordered_draw_obj_data_model_list`` 里没有它的
    ``DrawCallModel``（导出日志：``DrawIB xxx 没有对象 ... 全组件补占位小三角面``），
    按名字永远匹配不上。此时再从每个选中物体名（先经 :func:`_name_aliases` 展开）
    解析开头的 8 位 IB hash（:func:`_parse_hash_from_object_name`，样例
    ``LOD0.7fbbcf0d-1260-9402.脸+眼`` -> ``7fbbcf0d``），并入结果。这条路径只做
    **补漏**：已有命中原样保留、不覆盖；解析不出 hash 的名字（如 ``随便一个物体``）
    不会产生垃圾 hash。这里**不校验** registry 里有没有该 hash 的段 —— 调用方
    :func:`_collect_target_section_names` 随后会查 registry，没有段时自然返回空集，
    安全。

    ``model`` 为 ``None`` 时返回空集合（没有导出上下文）；物体名里也解析不出
    hash 时同样返回空集合。
    """
    if model is None:
        return set()

    mapping = getattr(model, "_object_name_mapping", None) or {}
    wanted = set()
    for name in object_names or []:
        for alias in _name_aliases(name, mapping):
            normalized = _normalize_object_name(alias)
            if normalized:
                wanted.add(normalized)
    if not wanted:
        return set()

    draw_ibs = set()
    for draw_call in getattr(model, "ordered_draw_obj_data_model_list", None) or []:
        base_names = []
        for attr in ("obj_name", "source_obj_name"):
            text = str(getattr(draw_call, attr, "") or "").strip()
            if text:
                base_names.append(text)
        get_blender_name = getattr(draw_call, "get_blender_obj_name", None)
        if callable(get_blender_name):
            try:
                text = str(get_blender_name() or "").strip()
            except Exception:
                text = ""
            if text:
                base_names.append(text)

        aliases = set()
        for name in base_names:
            for alias in _name_aliases(name, mapping):
                normalized = _normalize_object_name(alias)
                if normalized:
                    aliases.add(normalized)
        if not aliases or not (aliases & wanted):
            continue
        draw_ib = str(getattr(draw_call, "match_draw_ib", "") or "").strip()
        if draw_ib:
            draw_ibs.add(draw_ib)

    # 兜底：合并骨骼导出下这些部件没有独立导出对象（几何已被合并、原对象换成
    # 占位小三角面），DrawCallModel 按名字查不到，改用物体名前缀的 8 位 IB hash。
    seen = {str(value).casefold() for value in draw_ibs}
    for name in object_names or []:
        for alias in _name_aliases(name, mapping):
            hash_val = _parse_hash_from_object_name(alias)
            if hash_val and hash_val not in seen:
                draw_ibs.add(hash_val)
                seen.add(hash_val)
    return draw_ibs


#: 不参与门控的段名前缀（小写比较）：资源声明段、本节点自己的检测段。
_EXCLUDED_SECTION_PREFIXES = ("resource", "textureoverridecheckhash")


def _collect_target_section_names(model, object_names, ini_path):
    """反查「选定物体 -> draw_ib -> 导出期登记的段名」，返回 ``(draw_ibs, names)``。

    - ``draw_ibs``：物体所属 draw_ib 集合（空集表示物体与导出对象对不上）；
    - ``names``：这些 draw_ib 在导出期登记、且需要门控的段名集合（保留原始
      大小写）。排除 ``Resource*`` 资源声明段与 ``TextureOverrideCheckHash*``
      检测段。

    只读结构化登记，不扫描 ini 文本（段名里的 hash 可能是 VB hash / 贴图 hash）。
    """
    draw_ibs = _collect_draw_ibs_for_objects(model, object_names)
    if not draw_ibs:
        return set(), set()

    registry = _get_section_registry()
    if registry is None:
        return draw_ibs, set()

    try:
        ini_abs = os.path.abspath(ini_path) if ini_path else ""
    except (OSError, ValueError):
        ini_abs = str(ini_path or "")

    names = set()
    for entry in registry.get_sections_for_draw_ibs(draw_ibs, ini_abs) or []:
        name = str((entry or {}).get("name") or "").strip()
        if not name:
            continue
        if name.casefold().startswith(_EXCLUDED_SECTION_PREFIXES):
            continue
        names.add(name)
    return draw_ibs, names


def _detect_hash_candidates(detect_hash):
    """返回检测 hash 的等价写法（原样 / 小写 / 大写 / 去掉 ``0x`` 前缀）。

    用户填的哈希大小写、``0x`` 前缀都可能与导出期登记的 ``match_draw_ib``
    不一致；登记表按字符串精确匹配，所以先展开成候选再逐个查。
    """
    text = str(detect_hash or "").strip()
    if not text:
        return []
    candidates = []
    for value in (text, text.lower(), text.upper()):
        if value and value not in candidates:
            candidates.append(value)
    if text.lower().startswith("0x"):
        bare = text[2:].strip()
        for value in (bare, bare.lower(), bare.upper()):
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def _collect_detect_section_names(detect_hash, ini_path):
    """查出「检测 hash 自己对应的段名」，返回 ``TextureOverride*`` 段名集合。

    检测 hash 在本次导出里被本 mod 接管时（例如用户填的 ``92cb56c9`` 对应
    ``TextureOverride_IB_92cb56c9`` / ``TextureOverride_LOD0.92cb56c9_*``），
    条件变量就直接在这些段的段体里置位，不再生成独立检测段 —— 避免同 hash
    多个 ``TextureOverride`` 的加载顺序不确定（实机 ``Duplicate
    TextureOverride hash`` 冲突 -> 条件变量摆动 -> 模型崩坏）。

    只读导出期段登记（``common/export_section_registry``），不扫描 ini 文本。
    登记表不可用 / 该 hash 没有登记段时返回空集合（调用方回退独立检测段）。
    """
    registry = _get_section_registry()
    if registry is None:
        return set()

    try:
        ini_abs = os.path.abspath(ini_path) if ini_path else ""
    except (OSError, ValueError):
        ini_abs = str(ini_path or "")

    names = set()
    for candidate in _detect_hash_candidates(detect_hash):
        try:
            entries = registry.get_sections_for_draw_ib(candidate, ini_abs) or []
        except Exception:
            continue
        for entry in entries:
            name = str((entry or {}).get("name") or "").strip()
            if not name:
                continue
            if name.upper().startswith("TEXTUREOVERRIDE"):
                names.add(name)
    return names


def _is_target_section(section_name, target_names):
    """段名是否属于「要门控」的目标段：与导出期登记的段名精确匹配（忽略大小写）。

    目标段集合已由 :func:`_collect_target_section_names` 过滤过资源声明段与检测段；
    这里再兜底排除 ``[Constants]`` / ``[Present]``，避免把本节点要写入变量的段
    包进 ``if`` 里。

    ``target_names`` 允许传入已 casefold 的集合（结果等价）。
    """
    name = str(section_name or "").strip()
    if not name:
        return False
    lowered = name.casefold()
    if lowered in ("constants", "present"):
        return False
    for item in target_names or ():
        if str(item or "").strip().casefold() == lowered:
            return True
    return False


def _find_sections(lines):
    """把行列表切成段。

    返回 ``[(段名, 段头索引, 段体起始索引, 段体结束索引), ...]``，结束索引
    独占（等于下一个段头索引，或文件末尾）。第一个段头之前的前导内容
    不属于任何段。
    """
    headers = []
    for index, raw_line in enumerate(lines or []):
        header = _SECTION_HEADER_RE.match(str(raw_line or "").strip())
        if header:
            headers.append((header.group(1).strip(), index))
    sections = []
    for position, (name, header_index) in enumerate(headers):
        body_start = header_index + 1
        body_end = headers[position + 1][1] if position + 1 < len(headers) else len(lines or [])
        sections.append((name, header_index, body_start, body_end))
    return sections


#: 段体内**必须保持顶层**（绝不放进门控 ``if`` 体内）的行判定。
#:
#: 这几类是实测踩坑得到的硬约束，违反即模型崩坏：
#: - ``run =`` 在本 3DMigoto fork 的 if 体内不执行 -> 合并骨架 attach 不发生
#:   -> 骨架为空 -> 头发变块状 / 翅膀黑色碎片；
#: - 只在 if 体内赋值的 ``$变量`` 会被加载期优化器按初值静态折叠
#:   （``$zz_ms_seen_*`` / ``$zz_ms_occ_*`` 的 sticky 累加尤其致命）；
#: - ``ResourceZZPalette_*`` 是骨架状态捕获，必须每帧在顶层执行；
#: - 任意 ``Resource\<ns>\<槽位> = null`` 是「不覆盖」的安全置空（ZZMI /
#:   EFMI 的 SetTextures 都按 unless_null 处理）：条件不满足时不执行
#:   ``= ref``、但要保留这份置空，槽位才会回落原生贴图；把它门控掉会让槽位
#:   残留同帧上一次的 mod 贴图（「材质转资源pro」纯贴图模式段就靠这行兜底）。
_TOP_ONLY_PATTERNS = (
    (re.compile(r"^run\s*=", re.IGNORECASE), "run"),
    # 条件判断圈到「跳过部件」段时，段体里唯一可门控的行就是 ``handling = skip``：
    # 把它包进 if 才能实现「条件满足才跳过，否则恢复原生绘制」。
    # （``hash`` / ``match_first_index`` / ``match_index_count`` 由
    # ``_MATCH_KEY_RE`` 留在顶层，不走本表。）
    (re.compile(r"^\$[A-Za-z_\\]"), "$变量"),
    (re.compile(r"^resourcezzpalette", re.IGNORECASE), "Palette 捕获"),
    (re.compile(r"^resource\\[^=]*=\s*null\s*(?:;.*)?$", re.IGNORECASE), "贴图槽置空"),
)

#: 段体内**可以门控**（放进 ``if`` 体内）的行判定；逐个尝试，命中即可门控。
_GATEABLE_PATTERNS = (
    re.compile(r"^handling\s*=", re.IGNORECASE),
    re.compile(r"^vb\d+\s*=", re.IGNORECASE),
    re.compile(r"^ib\s*=", re.IGNORECASE),
    re.compile(r"^drawindexed\s*=", re.IGNORECASE),
    re.compile(r"^draw\s*=", re.IGNORECASE),
    # 贴图槽赋值：``Resource\ZZMI\Diffuse = ref ...`` / ``= null``
    re.compile(r"^resource\\zzmi\\[^=]*=", re.IGNORECASE),
    # 其余 Resource* 赋值（贴图资源 / ref 绑定）；Palette 已在顶层规则里拦掉
    re.compile(r"^resource[^=]*=", re.IGNORECASE),
    re.compile(r"^so\d+\s*=", re.IGNORECASE),
    # vs 槽绑定（含 vs-t0 骨架默认换绑）：条件不满足时不覆盖原生 deform draw
    # 的骨架绑定；条件满足时仍在绘制前执行，顺序不变。
    re.compile(r"^vs-t\d+\s*=", re.IGNORECASE),
    re.compile(r"^cs-t\d+\s*=", re.IGNORECASE),
    re.compile(r"^cs-u\d+\s*=", re.IGNORECASE),
    re.compile(r"^cs\s*=", re.IGNORECASE),
    re.compile(r"^dispatch\s*=", re.IGNORECASE),
    # shader 常量寄存器（WeightSync 段）
    re.compile(r"^[xyzw]\d+\s*=", re.IGNORECASE),
    # VertexLimitRaise 段
    re.compile(r"^(override_|uav_)", re.IGNORECASE),
)

_IF_START_RE = re.compile(r"^if\b", re.IGNORECASE)
_ENDIF_RE = re.compile(r"^endif\b", re.IGNORECASE)


def _classify_line(stripped):
    """把段体内一行分类成 ``"top"`` / ``"gate"`` / ``"neutral"``。

    - ``top``：必须留在门控 if 之外（``run`` / ``vs-t0`` / ``$变量`` /
      ``ResourceZZPalette*`` / 未识别行 —— 保守处理，避免误伤状态机）；
    - ``gate``：可以放进 ``if`` 体内；
    - ``neutral``：空行 / 纯注释行，不参与判定，跟随相邻片段。
    """
    text = str(stripped or "").strip()
    if not text:
        return "neutral"
    # 自动追加尾块标记行必须留在最外层：它是别的节点整块重写的内容，一旦被
    # 并进门控片段，endif 会落到标记之后、[Present] 会被插进 if 体内。
    if SSMTNode_PostProcess_Base.is_known_auto_appended_marker(text):
        return "top"
    if text.startswith(";"):
        # ``;MARK:...`` 是导出期段标记，必须留在最外层
        if text[:5].upper() == ";MARK":
            return "top"
        return "neutral"
    if text.startswith("//"):
        return "neutral"
    if _MATCH_KEY_RE.match(text):
        # 段根匹配键：必须留在最外层
        return "top"
    for pattern, _label in _TOP_ONLY_PATTERNS:
        if pattern.match(text):
            return "top"
    for pattern in _GATEABLE_PATTERNS:
        if pattern.match(text):
            return "gate"
    # 其它未识别行：保守保持顶层
    return "top"


def _find_block_end(body_lines, start):
    """返回 ``start`` 处 ``if`` 配对的 ``endif`` 行索引；没闭合返回 ``None``。"""
    depth = 0
    for index in range(start, len(body_lines)):
        stripped = str(body_lines[index] or "").strip()
        if _IF_START_RE.match(stripped):
            depth += 1
        elif _ENDIF_RE.match(stripped):
            depth -= 1
            if depth == 0:
                return index
    return None


def _if_depth_of_lines(lines):
    """统计一组行的 ``if`` / ``endif`` 净深度（正数=未闭合 if 数）。"""
    depth = 0
    for raw in lines or ():
        stripped = str(raw or "").strip()
        if _IF_START_RE.match(stripped):
            depth += 1
        elif _ENDIF_RE.match(stripped):
            depth -= 1
    return depth


def _segment_raw(body_lines):
    """按行分类切出片段 ``[(kind, [行...]), ...]``（kind 为 top/gate/neutral）。

    顶层 ``if ... endif`` 块的处理：

    - 块内**只有**可门控行（+ 空行 / 注释）-> 整块标成 ``gate``（保留块内原有
      if/endif，形成合法的嵌套）；
    - 块内混有 ``run`` / ``vs-t0`` / ``$变量`` / ``Palette`` / 未识别行 ->
      块本身保持顶层（``if`` / ``endif`` 两行原样），**递归**切分块内内容，
      只把其中可门控的连续片段门控（这样 if 块内的 ``run`` 仍在门控 if 之外）。
    """
    segments = []
    index = 0
    total = len(body_lines)
    while index < total:
        stripped = str(body_lines[index] or "").strip()
        if _IF_START_RE.match(stripped):
            end = _find_block_end(body_lines, index)
            if end is None:
                # 没闭合的 if：保守，剩余内容全部保持顶层
                segments.append(("top", list(body_lines[index:])))
                break
            interior = _segment_raw(list(body_lines[index + 1:end]))
            kinds = {kind for kind, _chunk in interior}
            if "gate" in kinds and "top" not in kinds:
                segments.append(("gate", list(body_lines[index:end + 1])))
            else:
                segments.append(("top", [body_lines[index]]))
                segments.extend(interior)
                segments.append(("top", [body_lines[end]]))
            index = end + 1
            continue

        kind = _classify_line(stripped)
        segments.append((kind, [body_lines[index]]))
        index += 1
    return segments


def _segment_body(body_lines):
    """把段体切成合并后的 ``[(kind, [行...]), ...]``，``kind`` 只有 top/gate。

    合并规则：空行 / 纯注释行不切断可门控片段 —— 紧跟在门控片段之后的归入
    该片段，紧挨着门控片段之前的归入后一个片段，两侧都不是门控片段时留在顶层。
    """
    merged = []
    pending = []
    for kind, chunk in _segment_raw(body_lines):
        if kind == "neutral":
            if merged and merged[-1][0] == "gate":
                merged[-1][1].extend(chunk)
            else:
                pending.extend(chunk)
            continue
        if kind == "gate":
            chunk = list(pending) + list(chunk)
            pending = []
            if merged and merged[-1][0] == "gate":
                merged[-1][1].extend(chunk)
            else:
                merged.append(["gate", chunk])
            continue
        # top
        if pending:
            merged.append(["top", list(pending)])
            pending = []
        if merged and merged[-1][0] == "top":
            merged[-1][1].extend(chunk)
        else:
            merged.append(["top", list(chunk)])
    if pending:
        if merged and merged[-1][0] == "gate":
            merged[-1][1].extend(pending)
        else:
            merged.append(["top", list(pending)])
    return merged


def _last_non_empty_index(lines, start, end):
    """返回 ``[start, end)`` 内最后一个非空行的索引；全空返回 ``None``。"""
    found = None
    for index in range(start, min(end, len(lines or []))):
        if str(lines[index] or "").strip():
            found = index
    return found


def _strip_condition_blocks(content, ns):
    """剥掉本节点（``ns``）上一次插入的所有行。

    三种块的剥离方式：

    - ``begin`` / ``end`` 包裹块：标记行与紧随的 ``if`` 行删掉，中间内容
      原样保留，末尾的 ``endif`` 与 ``end`` 标记删掉；
    - ``check-begin`` / ``check-end`` 检测段：整块删除（整块都是本节点
      生成的，删掉后按当前设置重建最干净）；
    - 单行标记 ``global`` / ``present`` / ``detect``：标记行 + 紧跟的对应
      ``global`` / ``post`` / ``$cond_... = 1`` 行一起删掉（避免残留导致变量
      重复声明或无条件置位失效）。

    只剥 ``ns`` 自己的标记，别的条件节点（别的 ns）的块保持原样。
    """
    text = str(content or "")
    if not text:
        return ""

    lines = text.splitlines(keepends=True)
    marker_prefix = f"{COND_MARKER_PREFIX}{ns}:"
    begin_marker = f"{COND_MARKER_PREFIX}{ns}:begin@@"
    end_marker = f"{COND_MARKER_PREFIX}{ns}:end@@"
    check_begin_marker = f"{COND_MARKER_PREFIX}{ns}:check-begin@@"
    check_end_marker = f"{COND_MARKER_PREFIX}{ns}:check-end@@"
    if_line = f"if $cond_{ns}_cond_active == 1"
    ns_var_prefix = f"cond_{ns}_"

    result = []
    in_wrap = False
    in_check = False
    wrap_first_line = False
    skip_content_kind = None  # "global" / "post"

    for raw_line in lines:
        stripped = str(raw_line or "").strip()

        if in_check:
            if stripped == check_end_marker:
                in_check = False
            continue

        if in_wrap:
            if stripped == end_marker:
                if result and str(result[-1]).strip() == "endif":
                    result.pop()
                in_wrap = False
                continue
            if wrap_first_line:
                wrap_first_line = False
                # 条件列表允许改条件（单哈希 -> 哈希+变量 / 反过来），旧表达式
                # 不再与新生成的 if 行逐字相同；begin 标记后的第一行只要是 if
                # 就删。保留完全相等判断做兜底。否则旧 if 残留、末尾 endif 被
                # 删掉 -> if/endif 不配对，结构损坏。
                if stripped == if_line or stripped.startswith("if "):
                    continue
            result.append(raw_line)
            continue

        if skip_content_kind is not None:
            lowered = stripped.casefold()
            # 标记行后面可能跟着「两行 global」或「两行 post」（锁存 + 清零），
            # 连续跳过所有属于本 ns 的同类行，直到出现别的行再收手。
            if (
                skip_content_kind == "global"
                and lowered.startswith("global ")
                and f"cond_{ns}_" in stripped
            ):
                continue
            if (
                skip_content_kind == "post"
                and lowered.startswith("post ")
                and f"cond_{ns}_" in stripped
            ):
                continue
            if skip_content_kind == "detect" and stripped.startswith("$"):
                compact = re.sub(r"\s+", "", stripped)
                # 多哈希条件：第 2 条起用 $cond_<ns>_h1_frame 等；按 ns 前缀
                # 统一识别，否则后几个槽位的置位行剥不掉。
                if (
                    compact.startswith(f"${ns_var_prefix}")
                    and compact.endswith("=1")
                ):
                    continue
            if skip_content_kind == "autoclose" and stripped == "endif":
                # 自动补齐的 endif 紧随 autoclose 标记；下一轮重新统计后再补。
                continue
            skip_content_kind = None

        if stripped == check_begin_marker:
            in_check = True
            continue
        if stripped == begin_marker:
            in_wrap = True
            wrap_first_line = True
            continue
        if stripped.startswith(marker_prefix):
            suffix = stripped[len(marker_prefix):]
            if suffix == "global@@":
                skip_content_kind = "global"
            elif suffix == "present@@":
                skip_content_kind = "post"
            elif suffix == "detect@@":
                skip_content_kind = "detect"
            elif suffix == "autoclose@@":
                skip_content_kind = "autoclose"
            continue
        result.append(raw_line)

    return "".join(result)


def _foreign_condition_namespaces(content, ns):
    """收集「不属于当前 ns」的 ``; @@COND:<8hex>:`` 标记计数。

    节点改名后旧 ns 的块再也剥不掉，这里只负责发现并提示；不自动删除。
    返回 ``{ns小写: 出现次数}``（当前 ns 不计）。
    """
    counts = {}
    current = str(ns or "").strip().lower()
    for match in _COND_NS_RE.finditer(str(content or "")):
        value = match.group(1).lower()
        if value == current:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _strip_foreign_namespaces(content, namespaces):
    """按 ns 循环调用 :func:`_strip_condition_blocks`，只删指定 ns 的块。

    返回 ``(updated, removed)``；``removed[ns]`` 为该 ns 被删掉的标记行数。
    当前 ns 不在这里处理。
    """
    text = str(content or "")
    removed = {}
    normalized = sorted(
        {
            str(value or "").strip().lower()
            for value in (namespaces or ())
            if str(value or "").strip()
        }
    )
    for value in normalized:
        marker = f"{COND_MARKER_PREFIX}{value}:"
        count = text.count(marker)
        if count <= 0:
            continue
        updated = _strip_condition_blocks(text, value)
        if updated != text:
            removed[value] = count
            text = updated
    return text, removed


def _read_ini_text(path):
    """读 ini，返回 ``(text, has_bom)``（utf-8-sig 解码，BOM 原样记下）。"""
    with open(path, "rb") as handle:
        raw = handle.read()
    return raw.decode("utf-8-sig"), raw.startswith(b"\xef\xbb\xbf")


def _write_ini_text(path, text, has_bom):
    """按原 BOM 状态写回 ini。"""
    with open(
        path,
        "w",
        encoding="utf-8-sig" if has_bom else "utf-8",
        newline="",
    ) as handle:
        handle.write(text)

# --------------------------------------------------------------------------- #
# 条件列表：归一化 / 校验 / 表达式
# --------------------------------------------------------------------------- #

def _condition_field(item, name, default=""):
    """从 dict 或 PropertyGroup item 取字段（缺失返回默认值）。"""
    if isinstance(item, dict):
        value = item.get(name, default)
    else:
        value = getattr(item, name, default)
    return default if value is None else value


def _normalize_condition(item):
    """把一条条件归一化成 dict（不校验合法性，见 :func:`_validate_conditions`）。"""
    if isinstance(item, dict):
        kind = item.get("condition_type", item.get("type", "HASH"))
    else:
        kind = getattr(item, "condition_type", "HASH")
    kind = str(kind or "HASH").strip().upper()
    if kind not in ("HASH", "VARIABLE"):
        kind = "HASH"

    hash_value = _condition_field(item, "hash_value", "")
    if not hash_value and isinstance(item, dict):
        # 兼容直接传 {"hash": ...} 的调用方。
        hash_value = item.get("hash", "")

    return {
        "type": kind,
        "hash_value": str(hash_value or "").strip(),
        "check_hash": str(_condition_field(item, "check_hash", "") or "").strip(),
        "index_count": str(_condition_field(item, "index_count", "") or "").strip(),
        "variable_name": str(_condition_field(item, "variable_name", "") or "").strip(),
        "comparison_op": str(_condition_field(item, "comparison_op", "==")).strip(),
        "compare_value": str(_condition_field(item, "compare_value", "1") or ""),
    }


def _variable_name_error(value):
    """变量名非法时返回原因文本；合法返回 ""（面板与导出共用）。"""
    name = str(value or "").strip()
    if not name:
        return "变量名为空"
    if not _VAR_NAME_RE.match(name):
        return (
            f"变量名「{name}」含非法字符"
            "（只允许 ASCII 字母/数字/下划线/反斜杠，可带 $ 前缀）"
        )
    bare = name[1:] if name.startswith("$") else name
    if _RESERVED_VAR_RE.match(bare):
        return (
            f"变量名「{name}」占用了节点保留前缀 cond_<8位hex>_"
            "（该命名空间每帧会被复位）"
        )
    return ""


def _compare_value_error(value):
    """比较值非法时返回原因文本；合法返回 ""。"""
    text = str(value or "").strip()
    if not text:
        return "比较值为空"
    if "\n" in text or "\r" in text:
        return "比较值含换行"
    if ";" in text:
        return "比较值含分号（会截断 ini 行）"
    return ""


def _validate_conditions(items):
    """校验并归一化条件列表，返回 ``(normalized, error)``；非法时 error 非空。

    只做两种自动修正：去首尾空白、变量名补 ``$``；其余非法输入一律拒绝，
    不自动清洗（清洗会让用户看到「功能没生效」而不是「名字写错了」）。
    """
    normalized = [_normalize_condition(item) for item in (items or [])]
    for index, cond in enumerate(normalized, 1):
        if cond["type"] == "HASH":
            effective = cond["hash_value"] or cond["check_hash"]
            if not effective:
                return normalized, (
                    f"第 {index} 条条件：哈希值为空"
                    "（「哈希值」与「备用哈希值」都为空）"
                )
            if "\n" in effective or "\r" in effective or ";" in effective:
                return normalized, f"第 {index} 条条件：哈希值含换行或分号"
            continue

        name = cond["variable_name"]
        name_error = _variable_name_error(name)
        if name_error:
            return normalized, f"第 {index} 条条件：{name_error}"
        if cond["comparison_op"] not in ("==", "!="):
            return normalized, (
                f"第 {index} 条条件：算子「{cond['comparison_op']}」不支持"
                "（只支持 == / !=）"
            )
        value_error = _compare_value_error(cond["compare_value"])
        if value_error:
            return normalized, f"第 {index} 条条件：{value_error}"
        if not name.startswith("$"):
            cond["variable_name"] = f"${name}"
    return normalized, ""


def _effective_conditions(node):
    """读取节点当前生效的条件：列表优先；列表为空时兼容旧的三个哈希属性。

    列表为空且旧哈希非空 -> 临时合成一条 HASH 条件（**不写回**属性），
    这样旧 .blend 打开零改动、单哈希导出仍走完全相同的代码路径。
    """
    items = list(getattr(node, "condition_list", []) or [])
    if items:
        return [_normalize_condition(item) for item in items]

    legacy_hash = str(getattr(node, "condition_hash", "") or "").strip()
    legacy_check = str(getattr(node, "check_hash", "") or "").strip()
    if not (legacy_hash or legacy_check):
        return []
    return [
        {
            "type": "HASH",
            "hash_value": legacy_hash,
            "check_hash": legacy_check,
            "index_count": str(getattr(node, "condition_index_count", "") or "").strip(),
            "variable_name": "",
            "comparison_op": "==",
            "compare_value": "1",
        }
    ]


def _slot_prefix(slot):
    """第 ``slot`` 条哈希条件的变量前缀：第 1 条 ``cond``，之后 ``h1`` / ``h2``。"""
    return "cond" if slot == 0 else f"h{slot}"


def _plan_hash_slots(conditions):
    """给哈希条件分配 latch 槽位，返回 ``(slots, slot_of)``。

    ``(hash, index_count)`` 相同的重复条件合并成一条（重复检测段会在同段插
    两次置位，脏；日志点名第几条）。``slots`` 里每项含 ``slot`` / ``hash`` /
    ``index_count`` / ``condition_index``；``slot_of`` 是条件下标 -> 槽位。
    """
    slots = []
    slot_of = {}
    seen = {}
    for index, cond in enumerate(conditions):
        if cond["type"] != "HASH":
            continue
        hash_value = cond["hash_value"] or cond["check_hash"]
        raw_index = cond["index_count"]
        sanitized = _safe_index_count(raw_index)
        if raw_index and sanitized is None:
            _log_warning(
                f"条件判断：第 {index + 1} 条条件的 IndexCount「{raw_index}」"
                "不是正整数，已忽略"
            )
        index_text = str(sanitized) if sanitized else ""
        key = (hash_value.casefold(), index_text)
        if key in seen:
            slot_of[index] = seen[key]
            first = slots[seen[key]]["condition_index"] + 1
            _log_warning(
                f"条件判断：第 {index + 1} 条哈希条件与第 {first} 条重复"
                f"（hash = {hash_value}），已按一条导出"
            )
            continue
        slot = len(slots)
        slots.append(
            {
                "slot": slot,
                "hash": hash_value,
                "index_count": index_text,
                "condition_index": index,
            }
        )
        seen[key] = slot
        slot_of[index] = slot
    return slots, slot_of


def _condition_expression(ns, conditions, slot_of):
    """把条件列表拼成一条 if 表达式（``&&`` 连接，不嵌套、单条不加括号）。"""
    clauses = []
    emitted = set()
    for index, cond in enumerate(conditions):
        if cond["type"] == "HASH":
            slot = slot_of[index]
            if slot in emitted:
                # 重复哈希条件已在槽位规划时合并，只出现一次。
                continue
            emitted.add(slot)
            clauses.append(f"$cond_{ns}_{_slot_prefix(slot)}_active == 1")
            continue
        clauses.append(
            f"{cond['variable_name']} {cond['comparison_op']} {cond['compare_value']}"
        )
    return " && ".join(clauses)


def _detect_sets_for_slots(detect_section_names, slot_count):
    """把「检测段名」参数展开成每个哈希槽位一份集合。

    兼容三种写法：

    - ``None`` / 字符串集合（旧调用）：只作用于第 1 个槽位；
    - ``list[set]``（execute 按每个哈希条件查一次）：按槽位取，缺的补空集；
    - ``dict[slot] = set``。
    """
    if slot_count <= 0:
        return []
    if detect_section_names is None:
        return [set() for _ in range(slot_count)]
    if isinstance(detect_section_names, dict):
        return [
            {str(name).strip() for name in (detect_section_names.get(slot) or ()) if str(name).strip()}
            for slot in range(slot_count)
        ]
    if (
        isinstance(detect_section_names, (list, tuple))
        and detect_section_names
        and all(isinstance(value, (set, frozenset)) for value in detect_section_names)
    ):
        result = [
            {str(name).strip() for name in value if str(name).strip()}
            for value in detect_section_names
        ]
        while len(result) < slot_count:
            result.append(set())
        return result[:slot_count]
    base = {
        str(name).strip()
        for name in (detect_section_names or ())
        if str(name).strip()
    }
    return [set(base) if slot == 0 else set() for slot in range(slot_count)]


def _wrap_target_sections(lines, ns, target_names, newline, condition_expr=None):
    """按「逐行 / 逐块分类」把目标段内可门控的内容包进门控 if。

    每个目标段只处理自己的段体（段头与段根的 ``hash`` 等匹配键之后的全部
    行），按 :func:`_segment_body` 切出的片段生成门控块：

    ``; @@COND:<ns>:begin@@`` / ``    if <条件表达式>`` /
    原行（缩进不变） / ``    endif`` / ``; @@COND:<ns>:end@@``

    ``condition_expr`` 为 ``None`` 时生成与旧版逐字节相同的单哈希 if 行
    ``    if $cond_<ns>_cond_active == 1``；多条件时由
    :func:`_condition_expression` 传入 ``&&`` 连接的一行表达式。

    必须保持顶层的行（``run`` / ``vs-t0`` / ``$变量`` / ``ResourceZZPalette``
    / 未识别行）原样留在门控 if 之外；一个段可以产生多对 begin/end。

    返回 ``(new_lines, wrapped, skipped, auto_closed, stray_endif, auto_close_count)``：
    ``wrapped`` 为真正产生门控块的段数，``skipped`` 为「没有可门控内容」的
    目标段数；``auto_closed`` / ``stray_endif`` 为段名列表（段内 if 未闭合 /
    有多余 endif），``auto_close_count`` 为补上的 endif 总数。
    """
    sections = _find_sections(lines)
    folded_targets = {
        str(name or "").strip().casefold()
        for name in (target_names or ())
        if str(name or "").strip()
    }
    replacements = []
    wrapped = 0
    skipped = 0
    auto_closed = []
    stray_endif = []
    auto_close_count = 0

    begin_line = f"{COND_MARKER_PREFIX}{ns}:begin@@{newline}"
    if condition_expr is None:
        # 单哈希条件基线：与改动前完全相同的 f-string，保证导出字节不变。
        if_line = f"    if $cond_{ns}_cond_active == 1{newline}"
    else:
        if_line = f"    if {condition_expr}{newline}"
    endif_line = f"    endif{newline}"
    end_line = f"{COND_MARKER_PREFIX}{ns}:end@@{newline}"

    for name, header_index, body_start, body_end in sections:
        if not _is_target_section(name, folded_targets):
            continue
        body = list(lines[body_start:body_end])
        segments = _segment_body(body)
        has_gate = any(kind == "gate" for kind, _chunk in segments)
        if has_gate:
            new_body = []
            for kind, chunk in segments:
                if kind != "gate":
                    new_body.extend(chunk)
                    continue
                chunk = list(chunk)
                # 门控块的 if 行要另起一行，末行必须先有换行
                chunk[-1] = _ensure_newline(chunk[-1], newline)
                new_body.append(begin_line)
                new_body.append(if_line)
                new_body.extend(chunk)
                new_body.append(endif_line)
                new_body.append(end_line)
            wrapped += 1
        else:
            # 没有可门控内容也要做 if 深度检查：缺 endif 的段不能静默放过。
            new_body = list(body)
            skipped += 1

        # 段内原有 if 可能没闭合：在段体末尾补受控 endif，避免门控把后一个
        # 段头 / [Present] 吞进来。多出的 endif 只记 stray，不改写。
        depth = _if_depth_of_lines(new_body)
        if depth > 0 and new_body:
            new_body[-1] = _ensure_newline(new_body[-1], newline)
            new_body.append(f"{COND_MARKER_PREFIX}{ns}:autoclose@@{newline}")
            new_body.extend(f"    endif{newline}" for _ in range(depth))
            auto_closed.append(name)
            auto_close_count += depth
        elif depth < 0:
            stray_endif.append(name)

        if new_body != body:
            replacements.append((body_start, body_end, new_body))

    # 从后往前替换，避免前面的替换改变后面的索引。
    for body_start, body_end, new_body in reversed(replacements):
        lines[body_start:body_end] = new_body
    return lines, wrapped, skipped, auto_closed, stray_endif, auto_close_count


def _ensure_constants_global(lines, ns, newline, slots=1):
    """在 ``[Constants]`` 段首插入全局变量声明（每个哈希槽位一对）。

    第 1 个槽位沿用 ``cond_<ns>_cond_active/frame``（基线字节不变）；
    第 2 个起用 ``cond_<ns>_h1_active/frame``、``_h2_...``。

    没有 ``[Constants]`` 段时新建一个（放在第一个段头之前），否则游戏里
    该变量没有声明，检测段给未声明的全局变量赋值会出问题。
    返回 ``(new_lines, inserted)``。
    """
    marker_line = f"{COND_MARKER_PREFIX}{ns}:global@@{newline}"
    declarations = []
    for slot in range(max(1, int(slots))):
        prefix = _slot_prefix(slot)
        declarations.append(f"global $cond_{ns}_{prefix}_active{newline}")
        declarations.append(f"global $cond_{ns}_{prefix}_frame{newline}")
    global_line = "".join(declarations)

    sections = _find_sections(lines)
    tail_index = _auto_appended_tail_index(lines)
    for name, header_index, body_start, body_end in sections:
        if str(name or "").strip().casefold() == "constants":
            if tail_index is not None and header_index >= tail_index:
                # 唯一的 [Constants] 在自动追加尾块里：写进去下次会被尾块节点
                # 整块重写掉。跳过它，在尾块之前另建一个稳定的 [Constants]。
                continue
            lines[header_index] = _ensure_newline(lines[header_index], newline)
            insert_at = _clamp_insert_before_tail(lines, body_start)
            lines[insert_at:insert_at] = [marker_line, global_line]
            return lines, True

    if tail_index is not None and sections and sections[0][1] >= tail_index:
        # 第一个段头也在尾块里：唯一安全落点是尾块标记之前。
        insert_at = tail_index
    else:
        insert_at = sections[0][1] if sections else len(lines)
    insert_at = _clamp_insert_before_tail(lines, insert_at)
    if insert_at > 0:
        lines[insert_at - 1] = _ensure_newline(lines[insert_at - 1], newline)
    block = [f"[Constants]{newline}", marker_line, global_line, newline]
    lines[insert_at:insert_at] = block
    return lines, True


def _ensure_check_section(lines, ns, detect_hash, index_count, newline, slot=0):
    """插入（或保留）``[TextureOverrideCheckHash_cond_<ns>]`` 检测段。

    第 1 个槽位用旧段名 ``..._cond_<ns>``；第 2 个起加 ``_h1`` / ``_h2``
    后缀。检测段在 ``[Constants]`` 段末尾之后；没有 Constants 时放在第一个
    段头之前。已存在同名段时不重复插入（它的内容在剥离阶段已整块删掉）。
    返回 ``(new_lines, inserted)``。
    """
    suffix = "" if slot == 0 else f"_h{slot}"
    prefix = _slot_prefix(slot)
    target_name = f"textureoverridecheckhash_cond_{ns}{suffix}"
    sections = _find_sections(lines)
    for name, header_index, body_start, body_end in sections:
        if str(name or "").strip().casefold() == target_name:
            return lines, False

    insert_at = None
    for name, header_index, body_start, body_end in sections:
        if str(name or "").strip().casefold() == "constants":
            last = _last_non_empty_index(lines, body_start, body_end)
            insert_at = (last + 1) if last is not None else body_start
            break
    if insert_at is None:
        insert_at = sections[0][1] if sections else len(lines)

    # 已经有本节点的检测段块时，新块必须整块接在最后一个 check-end 之后。
    # 否则（多哈希时）新块会插进上一个块的 check-begin 与段头之间：剥离状态
    # 机在第一个 check-end 就收尾，后面的段头残留、begin marker 反被吃掉。
    check_end_marker = f"{COND_MARKER_PREFIX}{ns}:check-end@@"
    last_check_end = None
    for index, raw_line in enumerate(lines):
        if str(raw_line or "").strip() == check_end_marker:
            last_check_end = index
    if last_check_end is not None:
        insert_at = last_check_end + 1
    insert_at = _clamp_insert_before_tail(lines, insert_at)

    block = [
        f"{COND_MARKER_PREFIX}{ns}:check-begin@@{newline}",
        f"[TextureOverrideCheckHash_cond_{ns}{suffix}]{newline}",
        f"hash = {detect_hash}{newline}",
    ]
    if index_count:
        block.append(f"match_index_count = {index_count}{newline}")
    block.append(f"$cond_{ns}_{prefix}_frame = 1{newline}")
    block.append(f"{COND_MARKER_PREFIX}{ns}:check-end@@{newline}")

    if insert_at > 0:
        lines[insert_at - 1] = _ensure_newline(lines[insert_at - 1], newline)
    lines[insert_at:insert_at] = block
    return lines, True


def _section_body_insert_index(lines, body_start, body_end):
    """返回「段体最前面、段根匹配键之后」的插入位置。

    段体开头可能连续出现 ``hash =`` / ``match_*`` / ``filter_index`` 与空行 /
    注释行；置位块插在它们之后、任何门控 ``if`` 之前，保证既不动段根匹配键，
    又不会被后面的门控块包住（``$变量`` 只能在顶层赋值）。
    """
    index = body_start
    last_key = body_start
    has_key = False
    while index < body_end:
        stripped = str(lines[index] or "").strip()
        if _MATCH_KEY_RE.match(stripped):
            has_key = True
            index += 1
            last_key = index
            continue
        if not stripped or stripped.startswith(";") or stripped.startswith("//"):
            index += 1
            continue
        break
    return last_key if has_key else index


def _insert_detect_blocks(lines, ns, detect_section_names, newline, slot=0):
    """在「检测 hash 自己的段」段体最前面无条件置位条件变量（幂等）。

    实机日志证据：独立检测段 ``[TextureOverrideCheckHash_cond_<ns>]`` 的
    ``hash =`` 与同 hash 的 ``_IB_<hash>`` / ``_LOD0.<hash>_*`` 段重复，3Dmigoto
    报 ``Possible Mod Conflict: Duplicate TextureOverride hash=...``，执行顺序
    不确定 -> 条件变量在 0/1 间摆动或不执行 -> 模型在 mod / 原生之间跳变 ->
    崩坏。把置位写进这些本来就被本 mod 接管的段，就不存在重复 hash。

    插入内容（两行，带 ``detect`` 标记，剥离时整对删除）::

        ; @@COND:<ns>:detect@@
        $cond_<ns>_<slot>_frame = 1

    位置：段头 + 段根匹配键之后、段体里第一个门控 ``if`` 之前，并保证在顶层
    （``$变量`` 只能在顶层赋值，否则会被加载期优化器静态折叠）。

    返回 ``(new_lines, inserted_sections)``：``inserted_sections`` 为真正插入
    置位块的段数（0 表示登记里的段在当前 ini 文本里一个都没找到，调用方回退
    独立检测段）。
    """
    folded = {
        str(name or "").strip().casefold()
        for name in (detect_section_names or ())
        if str(name or "").strip()
    }
    if not folded:
        return lines, 0

    marker_line = f"{COND_MARKER_PREFIX}{ns}:detect@@{newline}"
    set_line = f"$cond_{ns}_{_slot_prefix(slot)}_frame = 1{newline}"

    replacements = []
    for name, header_index, body_start, body_end in _find_sections(lines):
        if str(name or "").strip().casefold() not in folded:
            continue
        insert_at = _section_body_insert_index(lines, body_start, body_end)
        if insert_at > 0:
            lines[insert_at - 1] = _ensure_newline(lines[insert_at - 1], newline)
        replacements.append((insert_at, [marker_line, set_line]))

    # 从后往前插入，避免前面的插入改变后面的索引。
    for insert_at, block in reversed(replacements):
        lines[insert_at:insert_at] = block
    return lines, len(replacements)


def _auto_appended_tail_index(lines):
    """返回「自动追加尾块」第一行的索引；没有尾块时返回 ``None``。

    尾块内容每次导出都会被生成它的节点整块剥离重写，因此**不能**把本节点的
    复位行插进尾块里（下次导出会被抹掉）。判定口径复用
    ``node_postprocess_base.SSMTNode_PostProcess_Base.is_known_auto_appended_marker``。
    """
    for index, line in enumerate(lines):
        if SSMTNode_PostProcess_Base.is_known_auto_appended_marker(str(line or "")):
            return index
    return None


def _clamp_insert_before_tail(lines, insert_at):
    """把插入点夹到自动追加尾块之前；尾块内容不归本节点改写。"""
    tail_index = _auto_appended_tail_index(lines)
    if tail_index is not None and insert_at > tail_index:
        return tail_index
    return insert_at


def _innermost_unclosed_if_index(lines, insert_at):
    """返回 ``insert_at`` 之前仍未闭合的最内层 ``if`` 行索引；没有返回 ``None``。

    插入点若落在未闭合的 ``if`` 体内，插进去的 ``[Present]`` / 复位行 / global /
    检测段会改变 if/endif 配对。调用方据此把插入点回退到该 ``if`` 行之前。
    """
    stack = []
    limit = max(0, min(int(insert_at or 0), len(lines or [])))
    for index in range(limit):
        stripped = str(lines[index] or "").strip()
        if _IF_START_RE.match(stripped):
            stack.append(index)
        elif _ENDIF_RE.match(stripped):
            if stack:
                stack.pop()
    return stack[-1] if stack else None


def _present_insert_index(lines):
    """给新建的 ``[Present]`` 段选插入点（返回行索引，永远落在尾块之外）。

    优先接在最后一个段之后；一旦越过自动追加尾块的起始行，就收敛到尾块之前 ——
    段头一旦落在尾块标记之后，新的 ``[Present]`` 会连带复位行一起被下次导出重写掉。
    落点若在未闭合的 ``if`` 体内，再回退到该 ``if`` 行之前（否则 ``[Present]``
    会把门控块劈开）。
    """
    sections = _find_sections(lines)
    candidate = sections[-1][3] if sections else 0
    candidate = _clamp_insert_before_tail(lines, candidate)
    unclosed = _innermost_unclosed_if_index(lines, candidate)
    if unclosed is not None:
        candidate = unclosed
    return candidate


def _ensure_present_reset(lines, ns, newline, slots=1):
    """在 ``[Present]`` 段末尾插入「锁存 + 清零」两行（每个哈希槽位一对，幂等）。

    每帧末先 ``post $cond_<ns>_<slot>_active = $cond_<ns>_<slot>_frame`` 把本帧
    检测结果锁存给下一帧使用，再 ``post ..._frame = 0`` 清零。
    门控一律用 ``active``（帧内恒定），避免检测段与目标段 draw 顺序不同
    造成的半生效；代价是失效滞后 1 帧，视觉不可感知。

    这两行是**必要条件**：没有它们门控变量永不复位，部件效果会永久消失。所以
    配置表没有 ``[Present]`` 段时就地新建一个（位置见 :func:`_present_insert_index`）；
    若 ``[Present]`` 本身落在自动追加尾块里（插进去的复位行下次导出必被抹掉），
    则不做任何改写并返回 ``tail_blocked``，由调用方报错并拒绝写入。

    返回 ``(new_lines, status)``，``status`` 取 ``existing``（复用已有段）／
    ``created``（新建段）／``tail_blocked``（无安全位置、未改写）。
    """
    marker_line = f"{COND_MARKER_PREFIX}{ns}:present@@{newline}"
    posts = []
    for slot in range(max(1, int(slots))):
        prefix = _slot_prefix(slot)
        posts.append(
            f"post $cond_{ns}_{prefix}_active = $cond_{ns}_{prefix}_frame{newline}"
        )
        posts.append(f"post $cond_{ns}_{prefix}_frame = 0{newline}")
    post_line = "".join(posts)

    tail_index = _auto_appended_tail_index(lines)
    for name, header_index, body_start, body_end in _find_sections(lines):
        if str(name or "").strip().casefold() != "present":
            continue
        if tail_index is not None and header_index >= tail_index:
            # 唯一的 [Present] 在自动追加尾块里：复位行写进去等于没写。
            return lines, "tail_blocked"
        last = _last_non_empty_index(lines, body_start, body_end)
        if last is None:
            insert_at = body_start
        else:
            insert_at = last + 1
            lines[last] = _ensure_newline(lines[last], newline)
        if tail_index is not None and insert_at > tail_index:
            # 段体末尾落在尾块之后时收敛到尾块之前，保证连跑两次落点一致。
            insert_at = tail_index
        unclosed = _innermost_unclosed_if_index(lines, insert_at)
        if unclosed is not None:
            # 落点在未闭合 if 体内：插进去会把 if/endif 劈开，回退到 if 行之前。
            insert_at = unclosed
        lines[header_index] = _ensure_newline(lines[header_index], newline)
        lines[insert_at:insert_at] = [marker_line, post_line]
        return lines, "existing"

    insert_at = _present_insert_index(lines)
    if insert_at > 0:
        lines[insert_at - 1] = _ensure_newline(lines[insert_at - 1], newline)
    lines[insert_at:insert_at] = [f"[Present]{newline}", marker_line, post_line]
    return lines, "created"


def _apply_condition(content, ns, target_names, detect_hash, index_count="", detect_section_names=None,
                     conditions=None):
    """对配置表文本应用一次条件判断改写（幂等）。

    ``target_names`` 为导出期段登记反查出的目标段名集合（精确匹配）；
    ``detect_section_names`` 为「检测 hash 自己对应的段名」集合：非空时置位块
    写进这些段（段内置位，避免重复 hash 冲突），为空 / 在文本里一个都没找到
    时回退独立检测段。多哈希条件时也可以按槽位传 ``list[set]``。

    ``conditions`` 为归一化前的条件列表（dict 或 PropertyGroup item 均可）；
    为 ``None`` 时按旧参数 ``detect_hash`` / ``index_count`` 合成一条 HASH
    条件，保证旧调用路径行为与字节完全不变。

    组合语义固定为「与」：所有条件合并成一条 if，用 `` && `` 连接，不嵌套。
    只有至少一条哈希条件时才生成 ``[Constants]`` global、检测段 / 段内置位、
    ``[Present]`` 复位；只有变量条件时三者都不生成，也不新建 ``[Present]``。

    返回 ``(updated_text, stats)``，``stats`` 在原有 ``wrapped`` / ``skipped`` /
    ``check`` / ``detect`` / ``global_var`` / ``present`` 六键之外新增
    ``hash_count`` / ``variable_count`` / ``auto_closed`` / ``stray_endif`` /
    ``auto_close_count``。

    条件非法时抛 :class:`ConditionValidationError`；配置表结构不允许安全改写
    （唯一的 ``[Present]`` 落在自动追加尾块里）时抛 :class:`ConditionApplyError`：
    调用方必须报错并**放弃写入**，不许静默放过。
    """
    text = str(content or "")
    # 保留原文件的换行风格；孤立 \r（老 Mac）也识别，避免补成 \r\r\n。
    if "\r\n" in text:
        newline = "\r\n"
    elif "\n" in text:
        newline = "\n"
    elif "\r" in text:
        newline = "\r"
    else:
        newline = "\n"
    stripped_text = _strip_condition_blocks(text, ns)

    stats = {
        "wrapped": 0,
        "skipped": 0,
        "check": False,
        "detect": 0,
        "global_var": False,
        "present": False,
        "hash_count": 0,
        "variable_count": 0,
        "auto_closed": [],
        "stray_endif": [],
        "auto_close_count": 0,
    }
    names = []
    for name in target_names or ():
        clean_name = str(name or "").strip()
        if clean_name:
            names.append(clean_name)
    if not names:
        return stripped_text, stats

    if conditions is None:
        if not str(detect_hash or "").strip():
            # 没有任何条件：不门控，只保留剥离结果（旧行为：由调用方先跳过）。
            return stripped_text, stats
        conditions = [
            {
                "type": "HASH",
                "hash_value": str(detect_hash or "").strip(),
                "check_hash": "",
                "index_count": str(index_count or ""),
                "variable_name": "",
                "comparison_op": "==",
                "compare_value": "1",
            }
        ]

    normalized, condition_error = _validate_conditions(conditions)
    if condition_error:
        raise ConditionValidationError(condition_error)
    stats["hash_count"] = sum(1 for cond in normalized if cond["type"] == "HASH")
    stats["variable_count"] = len(normalized) - stats["hash_count"]
    if not normalized:
        return stripped_text, stats

    hash_slots, slot_of = _plan_hash_slots(normalized)
    if len(hash_slots) == 1 and stats["variable_count"] == 0:
        # 单哈希基线：走 _wrap_target_sections 里与旧版完全相同的 f-string。
        condition_expr = None
    else:
        condition_expr = _condition_expression(ns, normalized, slot_of)

    lines = stripped_text.splitlines(keepends=True)
    (
        lines,
        wrapped,
        skipped,
        auto_closed,
        stray_endif,
        auto_close_count,
    ) = _wrap_target_sections(lines, ns, names, newline, condition_expr)
    stats["wrapped"] = wrapped
    stats["skipped"] = skipped
    stats["auto_closed"] = auto_closed
    stats["stray_endif"] = stray_endif
    stats["auto_close_count"] = auto_close_count
    if wrapped <= 0:
        # 没有真正门控的段：不插入检测段 / 变量 / 复位行，只保留剥离结果。
        return "".join(lines), stats

    if not hash_slots:
        # 只有变量条件：没有哈希就没有检测对象，不生成 [Constants] global、
        # 检测段、[Present] 复位，也不新建 [Present]。
        stats["present"] = "skipped"
        return "".join(lines), stats

    lines, stats["global_var"] = _ensure_constants_global(
        lines, ns, newline, len(hash_slots)
    )
    # 检测方式：每个哈希条件各自判断「hash 是否已被本 mod 接管」。
    detect_sets = _detect_sets_for_slots(detect_section_names, len(hash_slots))
    detect_total = 0
    check_created = False
    for slot_info in hash_slots:
        slot = slot_info["slot"]
        lines, inserted = _insert_detect_blocks(
            lines, ns, detect_sets[slot], newline, slot
        )
        detect_total += inserted
        if inserted <= 0:
            lines, created = _ensure_check_section(
                lines,
                ns,
                slot_info["hash"],
                slot_info["index_count"],
                newline,
                slot,
            )
            check_created = check_created or bool(created)
    stats["detect"] = detect_total
    stats["check"] = check_created
    lines, present_status = _ensure_present_reset(
        lines, ns, newline, len(hash_slots)
    )
    if present_status == "tail_blocked":
        raise ConditionApplyError(
            "唯一的 [Present] 段落在「自动追加块」内部（标记行之后）：写进去的每帧"
            "复位行下次导出会被整块重写掉，等于门控变量永不复位、部件效果永久"
            "消失。拒绝改写，请先把 [Present] 段移到自动追加块之前再执行本节点。"
        )
    stats["present"] = present_status
    return "".join(lines), stats

# --------------------------------------------------------------------------- #
# 节点数据
# --------------------------------------------------------------------------- #

class SSMT_ConditionItem(bpy.types.PropertyGroup):
    """条件列表里的一条：哈希条件或变量条件（按 ``condition_type`` 区分）。"""

    condition_type: bpy.props.EnumProperty(  # type: ignore
        name="条件类型",
        description="哈希：按画面里是否出现该哈希门控；变量：按 ini 变量比较结果门控",
        items=[
            ('HASH', '哈希', '哈希在游戏画面中出现时生效'),
            ('VARIABLE', '变量', '变量比较成立时生效'),
        ],
        default='HASH',
    )
    hash_value: bpy.props.StringProperty(  # type: ignore
        name="哈希值",
        description="哈希条件：该哈希在游戏画面中出现时，选定物体的其余代码才生效",
        default="",
    )
    check_hash: bpy.props.StringProperty(  # type: ignore
        name="备用哈希值",
        description="哈希条件：哈希值为空时使用",
        default="",
    )
    index_count: bpy.props.StringProperty(  # type: ignore
        name="IndexCount",
        description="哈希条件：可选，限制检测到的绘制区间（留空则不限制）",
        default="",
    )
    variable_name: bpy.props.StringProperty(  # type: ignore
        name="变量名",
        description="变量条件：ini 变量名，可带 $（导出时自动补）",
        default="",
    )
    comparison_op: bpy.props.EnumProperty(  # type: ignore
        name="比较运算符",
        description="变量条件：只支持 == / !=",
        items=[
            ('==', '==', '等于'),
            ('!=', '!=', '不等于'),
        ],
        default='==',
    )
    compare_value: bpy.props.StringProperty(  # type: ignore
        name="比较值",
        description="变量条件：要比较的数值/标识符",
        default="1",
    )


class SSMT_ConditionObjectItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")  # type: ignore


class SSMT_UL_POSTPROCESS_CONDITIONS(bpy.types.UIList):
    """条件列表（哈希 / 变量混排；行内可改类型、上下移、删除）。"""

    bl_idname = "SSMT_UL_POSTPROCESS_CONDITIONS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            return
        row = layout.row(align=True)
        row.prop(item, "condition_type", text="")
        if item.condition_type == 'VARIABLE':
            name_icon = 'VIEWZOOM' if str(item.variable_name or "").strip() else 'ERROR'
            row.prop(item, "variable_name", text="", icon=name_icon)
            row.prop(item, "comparison_op", text="")
            row.prop(item, "compare_value", text="")
        else:
            hash_icon = 'VIEWZOOM' if (
                str(item.hash_value or "").strip() or str(item.check_hash or "").strip()
            ) else 'ERROR'
            row.prop(item, "hash_value", text="", icon=hash_icon)
            row.prop(item, "index_count", text="ic")

        node_name = getattr(data, "name", "")
        op_up = row.operator("ssmt.postprocess_condition_move_up", text="", icon='TRIA_UP')
        op_up.node_name = node_name
        op_up.index = index
        op_down = row.operator("ssmt.postprocess_condition_move_down", text="", icon='TRIA_DOWN')
        op_down.node_name = node_name
        op_down.index = index
        op_remove = row.operator("ssmt.postprocess_condition_remove", text="", icon='X')
        op_remove.node_name = node_name
        op_remove.index = index


class SSMT_UL_CONDITION_OBJECTS(bpy.types.UIList):
    """已选生效物体列表（行内 X 按钮按 index 删除对应项）。"""

    bl_idname = "SSMT_UL_CONDITION_OBJECTS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=str(item.object_name or ""), icon='OBJECT_DATA')
        op_remove = row.operator("ssmt.condition_parts_remove", text="", icon='X')
        op_remove.node_name = getattr(data, "name", "")
        op_remove.index = index


class SSMTNode_PostProcess_Condition(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = NODE_LABEL
    bl_description = (
        "选定物体所属 IB 的全部导出段（段名来自导出期结构化登记，含 VB / 贴图段）"
        "只在条件列表全部满足时生效；条件不满足时整段不执行（游戏恢复原生绘制）"
    )

    condition_objects: bpy.props.CollectionProperty(type=SSMT_ConditionObjectItem)  # type: ignore
    active_condition_object: bpy.props.IntProperty(default=0)  # type: ignore

    #: 条件列表（哈希 + 变量，组合语义固定「与」）。
    condition_list: bpy.props.CollectionProperty(type=SSMT_ConditionItem)  # type: ignore
    active_condition: bpy.props.IntProperty(default=0)  # type: ignore
    #: 首次导出时自动写入的命名空间；固定后节点改名也不再换 ns（旧标记还能剥）。
    condition_namespace: bpy.props.StringProperty(  # type: ignore
        name="条件命名空间（自动）",
        description="首次导出时自动写入；固定后改名也不会换命名空间",
        default="",
    )

    # 旧配置：保留声明与存档，列表为空时兼容读取（不写回），可显式迁移。
    condition_hash: bpy.props.StringProperty(  # type: ignore
        name="哈希值",
        description="该哈希在游戏画面中出现时，选定物体的其余代码才生效",
        default="",
    )
    check_hash: bpy.props.StringProperty(  # type: ignore
        name="备用哈希值",
        description="哈希值为空时使用",
        default="",
    )
    condition_index_count: bpy.props.StringProperty(  # type: ignore
        name="IndexCount",
        description="可选：限制检测到的绘制区间（留空则不限制）",
        default="",
    )

    def draw_buttons(self, context, layout):
        layout.label(text="条件全部满足时，选定物体相关的导出段才生效", icon='CON_TRANSLIKE')
        layout.label(text="目标段来自导出期结构化登记（含 VB / 贴图段）", icon='BOOKMARKS')
        layout.label(text="条件不满足时整段不执行（等同删除代码）", icon='INFO')

        box = layout.box()
        box.label(text=f"生效物体 ({len(self.condition_objects)})", icon='OBJECT_DATA')
        if self.condition_objects:
            box.template_list(
                "SSMT_UL_CONDITION_OBJECTS", "",
                self, "condition_objects",
                self, "active_condition_object",
                rows=max(2, min(len(self.condition_objects), 6)),
            )
        else:
            box.label(text="列表为空：导出时不会修改 ini", icon='ERROR')

        row = box.row(align=True)
        row.operator(
            "ssmt.condition_parts_add_selected", text="添加选中物体", icon='ADD'
        ).node_name = self.name
        op_remove_object = row.operator("ssmt.condition_parts_remove", text="移除", icon='REMOVE')
        op_remove_object.node_name = self.name
        op_remove_object.index = int(self.active_condition_object)
        row.operator("ssmt.condition_parts_clear", text="清空", icon='TRASH').node_name = self.name

        condition_box = layout.box()
        condition_box.label(
            text=f"条件列表 ({len(self.condition_list)}) · 组合方式：与（全部满足）",
            icon='FILTER',
        )
        if self.condition_list:
            condition_box.template_list(
                "SSMT_UL_POSTPROCESS_CONDITIONS", "",
                self, "condition_list",
                self, "active_condition",
                rows=max(2, min(len(self.condition_list), 6)),
            )
        else:
            condition_box.label(text="条件列表为空", icon='INFO')

        buttons = condition_box.row(align=True)
        op_add_hash = buttons.operator(
            "ssmt.postprocess_condition_add", text="+ 哈希条件", icon='ADD'
        )
        op_add_hash.node_name = self.name
        op_add_hash.condition_type = 'HASH'
        op_add_var = buttons.operator(
            "ssmt.postprocess_condition_add", text="+ 变量条件", icon='ADD'
        )
        op_add_var.node_name = self.name
        op_add_var.condition_type = 'VARIABLE'
        op_remove_condition = buttons.operator(
            "ssmt.postprocess_condition_remove", text="删除", icon='REMOVE'
        )
        op_remove_condition.node_name = self.name
        op_remove_condition.index = -1
        op_move_up = buttons.operator(
            "ssmt.postprocess_condition_move_up", text="上移", icon='TRIA_UP'
        )
        op_move_up.node_name = self.name
        op_move_up.index = -1
        op_move_down = buttons.operator(
            "ssmt.postprocess_condition_move_down", text="下移", icon='TRIA_DOWN'
        )
        op_move_down.node_name = self.name
        op_move_down.index = -1
        condition_box.label(
            text=f"命名空间：{getattr(self, 'condition_namespace', '') or '（首次导出时自动生成）'}"
        )
        condition_box.operator(
            "ssmt.postprocess_condition_cleanup_stale",
            text="清理其它命名空间残留",
            icon='TRASH',
        ).node_name = self.name

        if self.condition_list:
            index = max(0, min(int(self.active_condition), len(self.condition_list) - 1))
            item = self.condition_list[index]
            detail = condition_box.box()
            detail.label(text=f"选中条件 #{index + 1}")
            detail.prop(item, "condition_type")
            if item.condition_type == 'VARIABLE':
                detail.prop(item, "variable_name")
                detail.prop(item, "comparison_op")
                detail.prop(item, "compare_value")
                error = _variable_name_error(item.variable_name) or _compare_value_error(
                    item.compare_value
                )
                if error:
                    detail.label(text=f"第 {index + 1} 行：{error}", icon='ERROR')
            else:
                detail.prop(item, "hash_value")
                detail.prop(item, "check_hash")
                detail.prop(item, "index_count")
                if not (
                    str(item.hash_value or "").strip()
                    or str(item.check_hash or "").strip()
                ):
                    detail.label(text=f"第 {index + 1} 行：哈希值为空", icon='ERROR')

        legacy_box = layout.box()
        legacy_box.label(text="旧配置（兼容，只读）", icon='LOCKED')
        legacy_box.label(text=f"哈希值：{self.condition_hash or '（空）'}")
        legacy_box.label(text=f"备用哈希值：{self.check_hash or '（空）'}")
        legacy_box.label(text=f"IndexCount：{self.condition_index_count or '（空）'}")
        legacy_has_value = bool(
            str(self.condition_hash or "").strip()
            or str(self.check_hash or "").strip()
        )
        if legacy_has_value and not self.condition_list:
            legacy_box.label(text="当前按 1 条哈希条件导出", icon='INFO')
        elif legacy_has_value and self.condition_list:
            legacy_box.label(text="条件列表非空：旧配置不参与导出", icon='INFO')
        if legacy_has_value:
            legacy_box.operator(
                "ssmt.postprocess_condition_migrate_legacy",
                text="迁移到条件列表",
                icon='FORWARD',
            ).node_name = self.name

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def _namespace(self):
        """返回稳定命名空间：已固定的 ``condition_namespace`` 优先，否则 md5(name)。"""
        stored = str(getattr(self, "condition_namespace", "") or "").strip()
        if stored:
            return stored
        digest = hashlib.md5(str(self.name or "").encode("utf-8")).hexdigest()
        return digest[:8]

    def _ensure_namespace(self):
        """execute 专用：首次导出时把派生 ns 写回节点（draw 里不写）。"""
        ns = self._namespace()
        if not str(getattr(self, "condition_namespace", "") or "").strip():
            self.condition_namespace = ns
        return ns

    def _selected_object_names(self):
        """收集节点上选定的物体名（去空、去重、保持顺序）。"""
        names = []
        seen = set()
        for item in self.condition_objects:
            name = str(getattr(item, "object_name", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names

    def execute_postprocess(self, mod_export_path):
        from .node_postprocess_comment_cleanup import SSMTNode_PostProcess_CommentCleanup

        target_ini, error_message = SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(mod_export_path)
        if not target_ini:
            _log_error(f"条件判断：{error_message or '未找到目标配置表'}")
            return False

        object_names = self._selected_object_names()
        if not object_names:
            _log_info("条件判断：节点未选择任何物体，跳过")
            return True

        # 条件列表优先；列表为空时兼容旧三属性（临时合成一条哈希条件，不写回）。
        conditions, condition_error = _validate_conditions(_effective_conditions(self))
        if condition_error:
            _log_error(f"条件判断：{condition_error} -> {target_ini}")
            return False
        if not conditions:
            # 老工程行为兼容：列表空且旧哈希也空时仍按改动前的 warning 口径，
            # 文案保留「哈希值」字样（B1202）。
            _log_warning(
                "条件判断：未填写哈希值（「哈希值」与「备用哈希值」都为空，"
                "条件列表也为空），跳过"
            )
            return True

        hash_count = sum(1 for cond in conditions if cond["type"] == "HASH")
        variable_count = len(conditions) - hash_count
        detect_hash = ""
        if hash_count:
            first_hash = next(cond for cond in conditions if cond["type"] == "HASH")
            detect_hash = first_hash["hash_value"] or first_hash["check_hash"]

        ns = self._ensure_namespace()
        try:
            with open(target_ini, "rb") as handle:
                raw_bytes = handle.read()
        except OSError as exc:
            _log_error(f"条件判断：读取配置表失败 {target_ini}: {exc}")
            return False

        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        try:
            content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            _log_error(f"条件判断：配置表不是 UTF-8，跳过 {target_ini}: {exc}")
            return False

        # 其它 ns 的残留块：只提示、不自动删（可能是树上另一个条件判断节点）。
        foreign_namespaces = _foreign_condition_namespaces(content, ns)
        if foreign_namespaces:
            detail = "、".join(
                f"{name}({count} 处)"
                for name, count in sorted(foreign_namespaces.items())
            )
            _log_warning(
                f"条件判断：发现其它条件命名空间的门控块：{detail}。"
                "如果是本节点改名前的残留，用面板「清理其它命名空间残留」；"
                "如果树上有另一个条件判断节点，忽略"
            )

        # 用于区分 wrapped<=0 的改写原因：剥掉了旧门控，还是只补了未闭合 if。
        had_condition_markers = f"{COND_MARKER_PREFIX}{ns}:" in content

        # ---- 结构化定位层：本次导出的蓝图模型 -> draw_ib -> 导出期登记的段名 ----
        model = None
        try:
            from .export_helper import BlueprintExportHelper

            get_blueprint_model = getattr(BlueprintExportHelper, "get_current_blueprint_model", None)
            model = get_blueprint_model() if callable(get_blueprint_model) else None
        except Exception as exc:
            _log_warning(f"条件判断：读取本次导出的蓝图模型失败（{exc}），跳过")
            return True

        if model is None:
            _log_warning("条件判断：未获取到本次导出的蓝图模型（导出期段登记不可用），跳过")
            return True

        draw_ibs, target_names = _collect_target_section_names(model, object_names, target_ini)
        if not draw_ibs:
            _log_warning("条件判断：选定的物体未匹配到任何 DrawIB（物体名与导出对象不一致），跳过")
            return True
        if not target_names:
            _log_warning(
                f"条件判断：导出期段登记里没有这些 DrawIB 的段（IB: {sorted(draw_ibs)}），跳过"
            )
            return True

        # 同目录的分文件（multifile）里也可能有同名段：本节点只处理目标 ini，
        # 分文件走自己的导出与后处理链，这里只提示不处理。
        for sibling_name, sibling_sections in _find_sibling_ini_sections(
            target_ini, target_names
        ):
            _log_info(
                f"条件判断：同目录的分文件 {sibling_name} 里也有同名段"
                f"（{'、'.join(sibling_sections)}）；本节点只处理 "
                f"{os.path.basename(target_ini)}，分文件未改动"
            )

        # 每个哈希条件各自查「检测 hash 是否已被本 mod 接管」；变量条件不查。
        detect_sections = [
            _collect_detect_section_names(
                cond["hash_value"] or cond["check_hash"], target_ini
            )
            for cond in conditions
            if cond["type"] == "HASH"
        ]
        try:
            updated, stats = _apply_condition(
                content,
                ns,
                target_names,
                detect_hash,
                "",
                detect_sections or None,
                conditions,
            )
        except (ConditionApplyError, ConditionValidationError) as exc:
            _log_error(f"条件判断：{exc} -> {target_ini}")
            return False
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
                _log_error(f"条件判断：写入配置表失败 {target_ini}: {exc}")
                return False

        # if 深度告警先于 wrapped<=0 早退：没有可门控内容的段也要提示。
        if stats.get("auto_closed"):
            sections_text = "、".join(stats["auto_closed"])
            _log_warning(
                f"条件判断：段 {sections_text} 内 if 未闭合，已自动补 "
                f"{stats.get('auto_close_count', 0)} 个 endif，请检查原 ini"
            )
        if stats.get("stray_endif"):
            sections_text = "、".join(stats["stray_endif"])
            _log_warning(
                f"条件判断：段 {sections_text} 内有游离 endif（未修改文本，请检查原 ini）"
            )

        if stats.get("wrapped", 0) <= 0:
            if written:
                # 文件确实被改写了，但这次判定一个段都没包住：不能只记 info，
                # 否则用户会以为门控还在。
                if had_condition_markers:
                    message = (
                        "条件判断：这次判定没有包住任何段，但文件里原有的门控已被剥掉"
                        f"（IB: {', '.join(sorted(draw_ibs))}；"
                        f"登记段 {len(target_names)} 个） -> {target_ini}"
                    )
                else:
                    message = (
                        "条件判断：这次判定没有包住任何段（文件已按当前设置改写，"
                        f"如补齐未闭合 if；IB: {', '.join(sorted(draw_ibs))}；"
                        f"登记段 {len(target_names)} 个） -> {target_ini}"
                    )
                _log_warning(message)
                try:
                    from . import export_progress
                    stage_text = (
                        "已剥离旧门控" if had_condition_markers else "已按当前设置改写"
                    )
                    export_progress.report_stage(
                        f"条件判断：没有包住任何段，{stage_text} -> "
                        f"{os.path.basename(target_ini)}"
                    )
                except Exception:
                    pass
            else:
                # 没改写文件但一个段也没包住：段体全是顶层行（run / $变量 /
                # 未识别行）。用户只看 info 会以为条件判断根本没跑，这里升成
                # warning 并列出目标段，方便从日志定位为什么没生成。
                preview_names = sorted(target_names)[:3]
                preview_text = "、".join(preview_names)
                if len(target_names) > len(preview_names):
                    preview_text += f" 等 {len(target_names)} 个"
                _log_warning(
                    "条件判断：目标段没有可门控的内容，未生成任何条件段"
                    f"（IB: {', '.join(sorted(draw_ibs))}；"
                    f"登记段 {len(target_names)} 个：{preview_text}） -> {target_ini}"
                )
                try:
                    from . import export_progress
                    export_progress.report_stage(
                        "条件判断：目标段没有可门控的内容，未生成任何条件段"
                        f"（登记段 {len(target_names)} 个） -> "
                        f"{os.path.basename(target_ini)}"
                    )
                except Exception:
                    pass
            return True

        present_text = ""
        if stats.get("present") == "created":
            present_text = "；配置表原本没有 [Present] 段，已新建并写入每帧复位"
            _log_info(
                f"条件判断：配置表没有 [Present] 段，已在 {target_ini} 里新建该段并写入"
                "锁存 + 清零行（缺这些行门控变量永不复位，部件效果会永久消失）"
            )
        elif stats.get("present") not in ("existing", "skipped"):
            present_text = "；未能写入每帧复位行"
            _log_warning(
                f"条件判断：未能写入每帧复位行（present={stats.get('present')}），"
                "门控变量不会自动复位，请检查配置表结构"
            )

        if stats.get("detect", 0) > 0 and stats.get("check"):
            detect_text = (
                f"段内置位 {stats['detect']} 处 + 独立检测段"
                f"（{stats.get('hash_count', 0)} 条哈希条件）"
            )
        elif stats.get("detect", 0) > 0:
            detect_text = (
                f"段内置位 {stats['detect']} 处"
                f"（检测 hash {detect_hash} 已被本 mod 接管，无重复 hash 冲突）"
            )
        elif stats.get("check"):
            detect_text = (
                f"独立检测段（{stats.get('hash_count', 0)} 条哈希条件，"
                f"首个哈希 {detect_hash} 未被本 mod 接管）"
            )
        else:
            detect_text = "无哈希条件（只按变量条件门控）"

        condition_text = (
            f"条件 {stats.get('hash_count', 0) + stats.get('variable_count', 0)} 条"
            f"（哈希 {stats.get('hash_count', 0)} / 变量 {stats.get('variable_count', 0)}）"
        )
        _log_info(
            f"条件判断：{condition_text}，门控 {stats['wrapped']} 个段"
            f"（跳过 {stats['skipped']} 个无内容段），"
            f"检测方式：{detect_text}；"
            f"IB {len(draw_ibs)} 个（{', '.join(sorted(draw_ibs))}）/ "
            f"登记段 {len(target_names)} 个 / 物体 {len(object_names)} 个"
            f"{present_text}"
            f"{'' if written else '（配置表已是最新，未改写）'} -> {target_ini}"
        )
        try:
            from . import export_progress
            export_progress.report_stage(
                f"条件判断：{condition_text} / {len(draw_ibs)} 个IB / "
                f"{stats['wrapped']} 个段 / {len(object_names)} 个物体"
            )
        except Exception:
            pass
        return True


# --------------------------------------------------------------------------- #
# 算子：添加 / 移除 / 清空 / 解析哈希
# --------------------------------------------------------------------------- #

def _resolve_condition_node(context, node_name):
    """按节点名找回本节点（节点树变了 / 名字对不上时返回 ``None``）。"""
    space_data = getattr(context, "space_data", None)
    tree = None
    if space_data is not None:
        tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
    node = tree.nodes.get(node_name) if tree else None
    if node is None or getattr(node, "bl_idname", "") != NODE_IDNAME:
        return None
    return node


class SSMT_OT_PostProcess_AddConditionObject(bpy.types.Operator):
    bl_idname = "ssmt.condition_parts_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前选中的网格物体加入生效物体列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}

        objects = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == 'MESH']
        if not objects and getattr(context, "active_object", None) is not None:
            active = context.active_object
            if getattr(active, "type", "") == 'MESH':
                objects = [active]
        if not objects:
            self.report({'WARNING'}, "请先选中要添加的网格物体")
            return {'CANCELLED'}

        existing = {str(item.object_name or "") for item in node.condition_objects}
        added_count = 0
        for obj in objects:
            name = str(getattr(obj, "name", "") or "").strip()
            if not name or name in existing:
                continue
            item = node.condition_objects.add()
            item.object_name = name
            existing.add(name)
            added_count += 1

        self.report({'INFO'}, f"已添加 {added_count} 个物体")
        return {'FINISHED'}


class SSMT_OT_PostProcess_RemoveConditionObject(bpy.types.Operator):
    bl_idname = "ssmt.condition_parts_remove"
    bl_label = "移除物体"
    bl_description = "从生效物体列表中移除指定项"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}

        if 0 <= self.index < len(node.condition_objects):
            node.condition_objects.remove(self.index)
            node.active_condition_object = max(
                0, min(int(node.active_condition_object), len(node.condition_objects) - 1)
            )
            return {'FINISHED'}

        self.report({'WARNING'}, "列表中没有这一项")
        return {'CANCELLED'}


class SSMT_OT_PostProcess_ClearConditionObjects(bpy.types.Operator):
    bl_idname = "ssmt.condition_parts_clear"
    bl_label = "清空"
    bl_description = "清空生效物体列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}

        while len(node.condition_objects) > 0:
            node.condition_objects.remove(len(node.condition_objects) - 1)
        node.active_condition_object = 0
        self.report({'INFO'}, "已清空生效物体列表")
        return {'FINISHED'}


class SSMT_OT_PostProcess_ResolveConditionHash(bpy.types.Operator):
    bl_idname = "ssmt.condition_resolve_hash"
    bl_label = "从物体解析哈希"
    bl_description = "从选中物体（或列表里的物体）名称解析 8 位哈希，填入当前哈希条件"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}

        candidates = []
        for obj in getattr(context, "selected_objects", []) or []:
            candidates.append(str(getattr(obj, "name", "") or ""))
        for item in node.condition_objects:
            candidates.append(str(getattr(item, "object_name", "") or ""))

        # 优先写进当前选中的哈希条件；没有条件列表时兼容写旧属性。
        target_item = None
        conditions = list(getattr(node, "condition_list", []) or [])
        active_index = int(getattr(node, "active_condition", 0) or 0)
        if 0 <= active_index < len(conditions):
            item = conditions[active_index]
            if str(getattr(item, "condition_type", "HASH") or "HASH") == 'HASH':
                target_item = item

        for name in candidates:
            hash_val = _parse_hash_from_object_name(name)
            if not hash_val:
                continue
            if target_item is not None:
                target_item.hash_value = hash_val
            else:
                node.condition_hash = hash_val
            self.report({'INFO'}, f"已解析哈希: {hash_val}（来自「{name}」）")
            return {'FINISHED'}

        self.report({'WARNING'}, "无法从物体名称解析出 8 位哈希，请手动填写")
        return {'CANCELLED'}


# --------------------------------------------------------------------------- #
# 算子：条件列表增删 / 上下移 / 旧配置迁移
# --------------------------------------------------------------------------- #

class SSMT_OT_PostProcess_AddCondition(bpy.types.Operator):
    bl_idname = "ssmt.postprocess_condition_add"
    bl_label = "添加条件"
    bl_description = "往条件列表末尾添加一条哈希条件或变量条件"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    condition_type: bpy.props.StringProperty(default="HASH")  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}

        kind = str(self.condition_type or "HASH").strip().upper()
        if kind not in ("HASH", "VARIABLE"):
            kind = "HASH"
        item = node.condition_list.add()
        item.condition_type = kind
        item.hash_value = ""
        item.check_hash = ""
        item.index_count = ""
        item.variable_name = "$MyVar" if kind == "VARIABLE" else ""
        item.comparison_op = '=='
        item.compare_value = "1"
        node.active_condition = len(node.condition_list) - 1
        return {'FINISHED'}


class SSMT_OT_PostProcess_RemoveCondition(bpy.types.Operator):
    bl_idname = "ssmt.postprocess_condition_remove"
    bl_label = "删除条件"
    bl_description = "从条件列表中删除指定项（index < 0 时删当前选中项）"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}

        index = self.index
        if index < 0:
            index = int(getattr(node, "active_condition", 0) or 0)
        if not (0 <= index < len(node.condition_list)):
            self.report({'WARNING'}, "列表中没有这一项")
            return {'CANCELLED'}

        node.condition_list.remove(index)
        node.active_condition = max(
            0, min(index, len(node.condition_list) - 1)
        )
        return {'FINISHED'}


def _move_condition(node, direction, requested_index):
    """把条件上/下移一格；``requested_index < 0`` 时用当前选中项。"""
    count = len(node.condition_list)
    index = requested_index
    if index < 0:
        index = int(getattr(node, "active_condition", 0) or 0)
    target = index + direction
    if not (0 <= index < count and 0 <= target < count):
        return False
    move = getattr(node.condition_list, "move", None)
    if callable(move):
        move(index, target)
    else:
        item = node.condition_list[index]
        del node.condition_list[index]
        node.condition_list.insert(target, item)
    node.active_condition = target
    return True


class SSMT_OT_PostProcess_MoveConditionUp(bpy.types.Operator):
    bl_idname = "ssmt.postprocess_condition_move_up"
    bl_label = "上移条件"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}
        if not _move_condition(node, -1, self.index):
            return {'CANCELLED'}
        return {'FINISHED'}


class SSMT_OT_PostProcess_MoveConditionDown(bpy.types.Operator):
    bl_idname = "ssmt.postprocess_condition_move_down"
    bl_label = "下移条件"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}
        if not _move_condition(node, 1, self.index):
            return {'CANCELLED'}
        return {'FINISHED'}


class SSMT_OT_PostProcess_MigrateLegacyConditions(bpy.types.Operator):
    bl_idname = "ssmt.postprocess_condition_migrate_legacy"
    bl_label = "迁移到条件列表"
    bl_description = "把旧哈希三属性迁移成一条哈希条件，并清空旧属性"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}

        legacy_hash = str(getattr(node, "condition_hash", "") or "").strip()
        legacy_check = str(getattr(node, "check_hash", "") or "").strip()
        legacy_index = str(getattr(node, "condition_index_count", "") or "").strip()
        if not (legacy_hash or legacy_check):
            self.report({'WARNING'}, "旧配置为空，无需迁移")
            return {'CANCELLED'}

        item = node.condition_list.add()
        item.condition_type = 'HASH'
        item.hash_value = legacy_hash
        item.check_hash = legacy_check
        item.index_count = legacy_index
        item.variable_name = ""
        item.comparison_op = '=='
        item.compare_value = "1"
        node.condition_hash = ""
        node.check_hash = ""
        node.condition_index_count = ""
        node.active_condition = len(node.condition_list) - 1
        return {'FINISHED'}


class SSMT_OT_PostProcess_CleanupStaleConditions(bpy.types.Operator):
    bl_idname = "ssmt.postprocess_condition_cleanup_stale"
    bl_label = "清理其它命名空间残留"
    bl_description = (
        "删除配置表里其它条件命名空间（本节点改名前的旧 ns）的门控块；"
        "树上有其它条件判断节点时不要用"
    )
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    target_ini: bpy.props.StringProperty(default="")  # type: ignore
    namespace_list: bpy.props.StringProperty(default="")  # type: ignore

    def invoke(self, context, event):
        node = _resolve_condition_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择条件判断节点")
            return {'CANCELLED'}
        try:
            from .node_postprocess_comment_cleanup import (
                _read_mod_export_path,
                SSMTNode_PostProcess_CommentCleanup,
            )

            target_ini, error_message = (
                SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(
                    _read_mod_export_path()
                )
            )
        except Exception as exc:
            self.report({'ERROR'}, f"定位配置表失败: {exc}")
            return {'CANCELLED'}
        if not target_ini:
            self.report({'WARNING'}, error_message or "未找到目标配置表")
            return {'CANCELLED'}
        try:
            content, _has_bom = _read_ini_text(target_ini)
        except (OSError, UnicodeError) as exc:
            self.report({'ERROR'}, f"读取配置表失败: {exc}")
            return {'CANCELLED'}

        foreign = _foreign_condition_namespaces(content, node._namespace())
        if not foreign:
            self.report({'INFO'}, "没有发现其它命名空间的门控块")
            return {'CANCELLED'}
        self.target_ini = target_ini
        self.namespace_list = ",".join(sorted(foreign))
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.label(text="将删除以下命名空间的门控块：")
        layout.label(text=self.namespace_list)
        layout.label(text="树上有其它条件判断节点时不要用！", icon='ERROR')

    def execute(self, context):
        target_ini = str(self.target_ini or "").strip()
        namespaces = [
            value.strip()
            for value in str(self.namespace_list or "").split(",")
            if value.strip()
        ]
        if not target_ini or not os.path.isfile(target_ini):
            self.report({'ERROR'}, "目标配置表不存在，请重新执行")
            return {'CANCELLED'}
        if not namespaces:
            self.report({'INFO'}, "没有需要清理的命名空间")
            return {'CANCELLED'}
        try:
            content, has_bom = _read_ini_text(target_ini)
        except (OSError, UnicodeError) as exc:
            self.report({'ERROR'}, f"读取配置表失败: {exc}")
            return {'CANCELLED'}
        updated, removed = _strip_foreign_namespaces(content, namespaces)
        if updated == content:
            self.report({'INFO'}, "没有可清理的残留")
            return {'CANCELLED'}
        try:
            _write_ini_text(target_ini, updated, has_bom)
        except (OSError, UnicodeError) as exc:
            self.report({'ERROR'}, f"写入配置表失败: {exc}")
            return {'CANCELLED'}
        total = sum(removed.values())
        self.report({'INFO'}, f"已清理 {len(removed)} 个命名空间、{total} 处标记")
        return {'FINISHED'}


classes = (
    SSMT_ConditionItem,
    SSMT_ConditionObjectItem,
    SSMT_UL_POSTPROCESS_CONDITIONS,
    SSMT_UL_CONDITION_OBJECTS,
    SSMTNode_PostProcess_Condition,
    SSMT_OT_PostProcess_AddConditionObject,
    SSMT_OT_PostProcess_RemoveConditionObject,
    SSMT_OT_PostProcess_ClearConditionObjects,
    SSMT_OT_PostProcess_ResolveConditionHash,
    SSMT_OT_PostProcess_AddCondition,
    SSMT_OT_PostProcess_RemoveCondition,
    SSMT_OT_PostProcess_MoveConditionUp,
    SSMT_OT_PostProcess_MoveConditionDown,
    SSMT_OT_PostProcess_MigrateLegacyConditions,
    SSMT_OT_PostProcess_CleanupStaleConditions,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
