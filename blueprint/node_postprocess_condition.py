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
from .node_postprocess_skip import _SECTION_HEADER_RE, _strip_runtime_suffixes


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


# --------------------------------------------------------------------------- #
# 纯函数（不依赖 bpy，便于 headless 测试）
# --------------------------------------------------------------------------- #

def _ensure_newline(line, newline):
    """保证一行以换行结尾（文件末行可能没有换行）。"""
    text = str(line or "")
    return text if text.endswith("\n") else text + newline


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
#: - ``Resource\ZZMI\* = null`` 是「不覆盖」的安全置空：条件不满足时不执行
#:   ``= ref``、但要保留这份置空，``SetTextures`` 才会按 unless_null 不覆盖槽位
#:   -> 原生贴图；把它门控掉会让槽位残留上一次的 mod 贴图。
_TOP_ONLY_PATTERNS = (
    (re.compile(r"^run\s*=", re.IGNORECASE), "run"),
    (re.compile(r"^\$[A-Za-z_\\]"), "$变量"),
    (re.compile(r"^resourcezzpalette", re.IGNORECASE), "Palette 捕获"),
    (re.compile(r"^resource\\zzmi\\[^=]*=\s*null\s*(?:;.*)?$", re.IGNORECASE), "贴图槽置空"),
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
    active_var = f"cond_{ns}_cond_active"
    frame_var = f"cond_{ns}_cond_frame"

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
                if stripped == if_line:
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
            if (
                skip_content_kind == "detect"
                and stripped.startswith("$")
                and re.sub(r"\s+", "", stripped)
                in (f"${active_var}=1", f"${frame_var}=1")
            ):
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
            continue
        result.append(raw_line)

    return "".join(result)


def _wrap_target_sections(lines, ns, target_names, newline):
    """按「逐行 / 逐块分类」把目标段内可门控的内容包进门控 if。

    每个目标段只处理自己的段体（段头与段根的 ``hash`` 等匹配键之后的全部
    行），按 :func:`_segment_body` 切出的片段生成门控块：

    ``; @@COND:<ns>:begin@@`` / ``    if $cond_<ns>_cond_active == 1`` /
    原行（缩进不变） / ``    endif`` / ``; @@COND:<ns>:end@@``

    必须保持顶层的行（``run`` / ``vs-t0`` / ``$变量`` / ``ResourceZZPalette``
    / 未识别行）原样留在门控 if 之外；一个段可以产生多对 begin/end。

    返回 ``(new_lines, wrapped, skipped)``：``wrapped`` 为真正产生门控块的
    段数，``skipped`` 为「没有可门控内容」的目标段数。
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

    begin_line = f"{COND_MARKER_PREFIX}{ns}:begin@@{newline}"
    if_line = f"    if $cond_{ns}_cond_active == 1{newline}"
    endif_line = f"    endif{newline}"
    end_line = f"{COND_MARKER_PREFIX}{ns}:end@@{newline}"

    for name, header_index, body_start, body_end in sections:
        if not _is_target_section(name, folded_targets):
            continue
        body = list(lines[body_start:body_end])
        segments = _segment_body(body)
        if not any(kind == "gate" for kind, _chunk in segments):
            skipped += 1
            continue

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

        replacements.append((body_start, body_end, new_body))
        wrapped += 1

    # 从后往前替换，避免前面的替换改变后面的索引。
    for body_start, body_end, new_body in reversed(replacements):
        lines[body_start:body_end] = new_body
    return lines, wrapped, skipped


def _ensure_constants_global(lines, ns, newline):
    """在 ``[Constants]`` 段首插入两个全局变量声明（锁存值 + 本帧标志）。

    没有 ``[Constants]`` 段时新建一个（放在第一个段头之前），否则游戏里
    该变量没有声明，检测段给未声明的全局变量赋值会出问题。
    返回 ``(new_lines, inserted)``。
    """
    marker_line = f"{COND_MARKER_PREFIX}{ns}:global@@{newline}"
    global_line = (
        f"global $cond_{ns}_cond_active{newline}"
        f"global $cond_{ns}_cond_frame{newline}"
    )

    sections = _find_sections(lines)
    for name, header_index, body_start, body_end in sections:
        if str(name or "").strip().casefold() == "constants":
            lines[header_index] = _ensure_newline(lines[header_index], newline)
            lines[body_start:body_start] = [marker_line, global_line]
            return lines, True

    insert_at = sections[0][1] if sections else len(lines)
    if insert_at > 0:
        lines[insert_at - 1] = _ensure_newline(lines[insert_at - 1], newline)
    block = [f"[Constants]{newline}", marker_line, global_line, newline]
    lines[insert_at:insert_at] = block
    return lines, True


def _ensure_check_section(lines, ns, detect_hash, index_count, newline):
    """插入（或保留）``[TextureOverrideCheckHash_cond_<ns>]`` 检测段。

    检测段在 ``[Constants]`` 段末尾之后；没有 Constants 时放在第一个段头
    之前。已存在同名段时不重复插入（它的内容在剥离阶段已整块删掉）。
    返回 ``(new_lines, inserted)``。
    """
    target_name = f"textureoverridecheckhash_cond_{ns}"
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

    block = [
        f"{COND_MARKER_PREFIX}{ns}:check-begin@@{newline}",
        f"[TextureOverrideCheckHash_cond_{ns}]{newline}",
        f"hash = {detect_hash}{newline}",
    ]
    if index_count:
        block.append(f"match_index_count = {index_count}{newline}")
    block.append(f"$cond_{ns}_cond_frame = 1{newline}")
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


def _insert_detect_blocks(lines, ns, detect_section_names, newline):
    """在「检测 hash 自己的段」段体最前面无条件置位条件变量（幂等）。

    实机日志证据：独立检测段 ``[TextureOverrideCheckHash_cond_<ns>]`` 的
    ``hash =`` 与同 hash 的 ``_IB_<hash>`` / ``_LOD0.<hash>_*`` 段重复，3Dmigoto
    报 ``Possible Mod Conflict: Duplicate TextureOverride hash=...``，执行顺序
    不确定 -> 条件变量在 0/1 间摆动或不执行 -> 模型在 mod / 原生之间跳变 ->
    崩坏。把置位写进这些本来就被本 mod 接管的段，就不存在重复 hash。

    插入内容（两行，带 ``detect`` 标记，剥离时整对删除）::

        ; @@COND:<ns>:detect@@
        $cond_<ns>_cond_frame = 1

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
    set_line = f"$cond_{ns}_cond_frame = 1{newline}"

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


def _ensure_present_reset(lines, ns, newline):
    """在 ``[Present]`` 段末尾插入「锁存 + 清零」两行（幂等）。

    每帧末先 ``post $cond_<ns>_cond_active = $cond_<ns>_cond_frame`` 把本帧
    检测结果锁存给下一帧使用，再 ``post $cond_<ns>_cond_frame = 0`` 清零。
    门控一律用 ``cond_active``（帧内恒定），避免检测段与目标段 draw 顺序不同
    造成的半生效；代价是失效滞后 1 帧，视觉不可感知。

    没有 Present 段时返回 ``(lines, False)``（由调用方在摘要里提示）。
    """
    marker_line = f"{COND_MARKER_PREFIX}{ns}:present@@{newline}"
    post_line = (
        f"post $cond_{ns}_cond_active = $cond_{ns}_cond_frame{newline}"
        f"post $cond_{ns}_cond_frame = 0{newline}"
    )

    for name, header_index, body_start, body_end in _find_sections(lines):
        if str(name or "").strip().casefold() != "present":
            continue
        last = _last_non_empty_index(lines, body_start, body_end)
        if last is None:
            insert_at = body_start
        else:
            insert_at = last + 1
            lines[last] = _ensure_newline(lines[last], newline)
        lines[header_index] = _ensure_newline(lines[header_index], newline)
        lines[insert_at:insert_at] = [marker_line, post_line]
        return lines, True
    return lines, False


def _apply_condition(content, ns, target_names, detect_hash, index_count="", detect_section_names=None):
    """对配置表文本应用一次条件判断改写（幂等）。

    ``target_names`` 为导出期段登记反查出的目标段名集合（精确匹配）；
    ``detect_section_names`` 为「检测 hash 自己对应的段名」集合：非空时置位块
    写进这些段（段内置位，避免重复 hash 冲突），为空 / 在文本里一个都没找到
    时回退独立检测段。

    返回 ``(updated_text, stats)``，``stats`` 含 ``wrapped`` / ``skipped`` /
    ``check`` / ``detect`` / ``global_var`` / ``present`` 六个字段
    （``check`` 表示生成了独立检测段，``detect`` 表示段内置位的段数）。
    """
    text = str(content or "")
    newline = "\r\n" if "\r\n" in text else "\n"
    stripped_text = _strip_condition_blocks(text, ns)

    stats = {
        "wrapped": 0,
        "skipped": 0,
        "check": False,
        "detect": 0,
        "global_var": False,
        "present": False,
    }
    names = []
    for name in target_names or ():
        clean_name = str(name or "").strip()
        if clean_name:
            names.append(clean_name)
    if not names:
        return stripped_text, stats

    lines = stripped_text.splitlines(keepends=True)
    lines, wrapped, skipped = _wrap_target_sections(lines, ns, names, newline)
    stats["wrapped"] = wrapped
    stats["skipped"] = skipped
    if wrapped <= 0:
        # 没有真正门控的段：不插入检测段 / 变量 / 复位行，只保留剥离结果。
        return "".join(lines), stats

    lines, stats["global_var"] = _ensure_constants_global(lines, ns, newline)
    # 检测方式：检测 hash 已被本 mod 接管 -> 在它自己的段里无条件置位；
    # 否则（登记里没有它的段）才回退独立检测段（此时没有同 hash 竞争）。
    lines, stats["detect"] = _insert_detect_blocks(lines, ns, detect_section_names, newline)
    if stats["detect"] > 0:
        stats["check"] = False
    else:
        lines, stats["check"] = _ensure_check_section(lines, ns, detect_hash, index_count, newline)
    lines, stats["present"] = _ensure_present_reset(lines, ns, newline)
    return "".join(lines), stats


# --------------------------------------------------------------------------- #
# 节点数据
# --------------------------------------------------------------------------- #

class SSMT_ConditionObjectItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")  # type: ignore


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
        "只在指定哈希出现于游戏画面时生效；哈希不出现时整段不执行（游戏恢复原生绘制）"
    )

    condition_objects: bpy.props.CollectionProperty(type=SSMT_ConditionObjectItem)  # type: ignore
    active_condition_object: bpy.props.IntProperty(default=0)  # type: ignore

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
        layout.label(text="哈希出现时，选定物体相关的导出段才生效", icon='CON_TRANSLIKE')
        layout.label(text="目标段来自导出期结构化登记（含 VB / 贴图段）", icon='BOOKMARKS')
        layout.label(text="哈希不出现时整段不执行（等同删除代码）", icon='INFO')

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
        op_remove = row.operator("ssmt.condition_parts_remove", text="移除", icon='REMOVE')
        op_remove.node_name = self.name
        op_remove.index = int(self.active_condition_object)
        row.operator("ssmt.condition_parts_clear", text="清空", icon='TRASH').node_name = self.name

        hash_box = layout.box()
        hash_box.label(text="检测哈希（在游戏画面中出现时才生效）", icon='FILTER')
        hash_box.prop(self, "condition_hash")
        hash_box.prop(self, "check_hash")
        hash_box.prop(self, "condition_index_count")
        hash_box.operator(
            "ssmt.condition_resolve_hash", text="从物体解析哈希", icon='VIEWZOOM'
        ).node_name = self.name

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def _namespace(self):
        """由节点名派生稳定的 8 位十六进制命名空间。"""
        digest = hashlib.md5(str(self.name or "").encode("utf-8")).hexdigest()
        return digest[:8]

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
            print(f"条件判断：{error_message or '未找到目标配置表'}")
            return False

        object_names = self._selected_object_names()
        if not object_names:
            print("条件判断：节点未选择任何物体，跳过")
            return True

        detect_hash = str(self.condition_hash or "").strip() or str(self.check_hash or "").strip()
        if not detect_hash:
            print("条件判断：未填写哈希值（「哈希值」与「备用哈希值」都为空），跳过")
            return True

        raw_index_count = str(self.condition_index_count or "").strip()
        index_count = _safe_index_count(raw_index_count)
        if raw_index_count and index_count is None:
            print(f"条件判断：IndexCount「{raw_index_count}」不是正整数，已忽略")

        ns = self._namespace()
        try:
            with open(target_ini, "rb") as handle:
                raw_bytes = handle.read()
        except OSError as exc:
            print(f"条件判断：读取配置表失败 {target_ini}: {exc}")
            return False

        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        try:
            content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            print(f"条件判断：配置表不是 UTF-8，跳过 {target_ini}: {exc}")
            return False

        # ---- 结构化定位层：本次导出的蓝图模型 -> draw_ib -> 导出期登记的段名 ----
        model = None
        try:
            from .export_helper import BlueprintExportHelper

            get_blueprint_model = getattr(BlueprintExportHelper, "get_current_blueprint_model", None)
            model = get_blueprint_model() if callable(get_blueprint_model) else None
        except Exception as exc:
            print(f"条件判断：读取本次导出的蓝图模型失败（{exc}），跳过")
            return True

        if model is None:
            print("条件判断：未获取到本次导出的蓝图模型（导出期段登记不可用），跳过")
            return True

        draw_ibs, target_names = _collect_target_section_names(model, object_names, target_ini)
        if not draw_ibs:
            print("条件判断：选定的物体未匹配到任何 DrawIB（物体名与导出对象不一致），跳过")
            return True
        if not target_names:
            print(
                f"条件判断：导出期段登记里没有这些 DrawIB 的段（IB: {sorted(draw_ibs)}），跳过"
            )
            return True

        updated, stats = _apply_condition(
            content,
            ns,
            target_names,
            detect_hash,
            index_count,
            _collect_detect_section_names(detect_hash, target_ini),
        )
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
                print(f"条件判断：写入配置表失败 {target_ini}: {exc}")
                return False

        if stats.get("wrapped", 0) <= 0:
            print(
                f"条件判断：目标段没有可门控的内容（IB: {', '.join(sorted(draw_ibs))}；"
                f"登记段 {len(target_names)} 个）"
                f"{'；已清除本节点旧标记' if written else ''} -> {target_ini}"
            )
            return True

        if not stats.get("present", False):
            print("条件判断：配置表没有 [Present] 段，无法每帧复位（变量会保持上一次的值）")

        if stats.get("detect", 0) > 0:
            detect_text = (
                f"段内置位 {stats['detect']} 处"
                f"（检测 hash {detect_hash} 已被本 mod 接管，无重复 hash 冲突）"
            )
        else:
            detect_text = f"独立检测段（检测 hash {detect_hash} 未被本 mod 接管）"

        print(
            f"条件判断：门控 {stats['wrapped']} 个段（跳过 {stats['skipped']} 个无内容段），"
            f"检测方式：{detect_text}；"
            f"IB {len(draw_ibs)} 个（{', '.join(sorted(draw_ibs))}）/ "
            f"登记段 {len(target_names)} 个 / 物体 {len(object_names)} 个"
            f"{'' if written else '（配置表已是最新，未改写）'} -> {target_ini}"
        )
        try:
            from . import export_progress
            export_progress.report_stage(
                f"条件判断：{len(draw_ibs)} 个IB / {stats['wrapped']} 个段 / "
                f"{len(object_names)} 个物体 / "
                f"{'段内置位 ' + str(stats['detect']) + ' 处' if stats.get('detect', 0) > 0 else '独立检测段'}"
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
    bl_description = "从选中物体（或列表里的物体）名称解析 8 位哈希，填入「哈希值」"
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

        for name in candidates:
            hash_val = _parse_hash_from_object_name(name)
            if not hash_val:
                continue
            node.condition_hash = hash_val
            self.report({'INFO'}, f"已解析哈希: {hash_val}（来自「{name}」）")
            return {'FINISHED'}

        self.report({'WARNING'}, "无法从物体名称解析出 8 位哈希，请手动填写")
        return {'CANCELLED'}


classes = (
    SSMT_ConditionObjectItem,
    SSMT_UL_CONDITION_OBJECTS,
    SSMTNode_PostProcess_Condition,
    SSMT_OT_PostProcess_AddConditionObject,
    SSMT_OT_PostProcess_RemoveConditionObject,
    SSMT_OT_PostProcess_ClearConditionObjects,
    SSMT_OT_PostProcess_ResolveConditionHash,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
