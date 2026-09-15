# -*- coding: utf-8 -*-
"""EFMI 多 pass 贴图槽位镜像（pass-mirror）的推导与行构造（2026-09-15 实机定位）。

背景（终末地实机，FrameAnalysis-2026-09-15-160205）：同一部件一帧内被多个 pass
绘制，各 pass 的着色器读**不同**贴图槽位：

    深度/阴影   NumViews=0   ps=d7bb9dd5…           （rt_width 门控整段不回画，无需镜像）
    前向第一层  NumViews=2   ps=eeb2d6fb/de732586…   扩散图在部件标记槽（t13/t14/…）
    前向第二层  NumViews=2   ps=2f079737/d72788f1…   扩散图在 t13/t12（按族不同）
    G-buffer    NumViews=5   ps=78015be8/81690836…   扩散图在 t0、法线在 t1

模组的槽位绑定只覆盖第一层（提取时的标记槽）→ 其余可见 pass 仍读原始贴图 →
同一帧里同一部件被两种贴图上色 = 颜色偏差（2026-09-15 艾尔黛拉实机）。

三条硬约束（各有实机事故，勿回退）：

1. **镜像必须按 pass 的 PS 标签门控（``if ps == <tag>``），绝不直绑**：t0/t1 只在
   G-buffer pass 是贴图槽；在前向两层它们是 StructuredBuffer（光源簇位掩码表）
   ——直绑贴图 = 视图类型不匹配 = 游戏闪退（2026-09-15 实机）。第二层的扩散槽
   t12 在第一层又被别的贴图占用，同样必须门控。

2. **标签"只借不抢"**（99001 事故教训）：发射前先扫 Mods 目录既有 ini——已被
   别家（如 RabbitFX 1718.2）注册的哈希**接受其标签进条件、不再自注册**；
   未注册的才用本工具私号（99002 = G-buffer 族 / 99003 = 前向第二层族）。
   条件行同时接受两边：``if ps == 99002 || ps == 1718.2``——谁注册生效都不影响
   本门控成立。
   注意 RabbitFX 这类 ShaderRegex 是**按字节码特征**打标签的（段内无 hash 行，
   成对扫描看不到）：我们自注册的 ShaderOverride 优先级天然压过它——若被注册
   哈希恰好也被它命中，它对该 shader 失效。发射侧用 efmi_scan_regex_taggers
   发现并点名（打印提醒 + 手动借标签配方），但不自动把正则标签借进条件
   （家族归属不明时借标签 = 99001 类事故方向）。

3. **推导全部来自抓帧，零硬编码哈希**：各 pass 的 PS 哈希与原图槽位都从
   FrameAnalysis log.txt 按部件 IB 读出（实机验证：推导结果与 2026-09-15 手修
   艾尔黛拉.ini 的三条标签逐一吻合）。

本模块只放纯扫描/纯推导/纯行构造（无 bpy 依赖，可脱离 Blender 单测）；
发射侧接线见 ui/universal/efmi.py（_efmi_append_pass_mirrors）。
"""
import hashlib
import json
import os
import re
from collections import defaultdict

# pass 角色标签（本工具私号；改动前先全机 grep 确认没有别家在用）：
# 99002 = G-buffer 族（NumViews>=3；该 pass 的 t0=扩散、t1=法线是贴图槽）
# 99003 = 前向第二层族（NumViews==2 的非主层；扩散槽按族在 t12/t13…）
EFMI_PASSC_FILTER_INDEX = 99002
EFMI_LAYER2_FILTER_INDEX = 99003

# FrameAnalysis 日志行格式（与 common/efmi_skeleton.py 的 EFMILogParser 同源）：
#   000015 OMSetRenderTargets(NumViews:0, ...)
#   000015 DrawIndexedInstanced(IndexCountPerInstance:...)
#   000015 3DMigoto Dumping Buffer <dir>\000015-ib=<ib>-vs=<vs>-ps=<ps>.buf -> <deduped>
#   000015 3DMigoto Dumping Texture2D <dir>\000015-ps-t0=<texhash>-vs=<vs>-ps=<ps>.dds -> ...
_OM_NUMVIEWS_RE = re.compile(r"^(\d{6}) OMSetRenderTargets\(NumViews:(\d+)")
_DRAW_CALL_RE = re.compile(r"^(\d{6}) DrawIndexedInstanced\(")
_DUMP_RE = re.compile(r"^(\d{6}) 3DMigoto Dumping \S+ .*\\([^\\]+) -> ")
_IB_RE = re.compile(r"^(\d+)-ib=([0-9a-f]{8})")
_PS_IN_NAME_RE = re.compile(r"-ps=([0-9a-f]{16})")
_SLOT_RE = re.compile(r"^(\d+)-ps-t(\d+)=([0-9a-f]{8})")

# 别家 ini 的 ShaderOverride/ShaderRegex 段（抓 hash + filter_index 配对）
_FOREIGN_SECTION_RE = re.compile(
    r"(?ms)^\[(Shader(?:Override|Regex)[^\]]*)\]\s*\n(.*?)(?=^\[|\Z)"
)
_HASH_LINE_RE = re.compile(r"(?im)^\s*hash\s*=\s*([0-9a-f]{8,16})\s*$")
_FILTER_LINE_RE = re.compile(r"(?im)^\s*filter_index\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$")
# 本工具自己的段名前缀（防止扫到自家旧输出后"自我借标签"）
_OWN_SECTION_TOKENS = ("PassMirror", "ShadowPS")


def efmi_scan_pass_layouts(log_paths, part_ibs):
    """扫 FrameAnalysis log.txt → ``{ib: [layout]}``。

    layout = ``{"numviews": int|None, "ps": str, "slots": {tex_hash: "t12"}}``，
    按 (numviews, ps) 归并（同一 pass 可能分多笔 draw，槽位取并集——首见优先，
    同 pass 同着色器下同一贴图哈希不会换槽）。
    只收 ib 命中 part_ibs 的 draw；文件缺失/不可读跳过。
    """
    part_ibs = {str(v or "").strip().lower() for v in (part_ibs or ())} - {""}
    if not part_ibs:
        return {}
    result = {}
    for log_path in log_paths or ():
        if not log_path or not os.path.isfile(log_path):
            continue
        try:
            _scan_one_log(log_path, part_ibs, result)
        except OSError:
            continue
    return result


def _scan_one_log(log_path, part_ibs, result):
    draw_numviews = {}
    # (ib, nv, ps) -> slots 聚合；先按 draw 暂存再归并
    draw_ib = {}
    draw_ps = {}
    draw_slots = defaultdict(dict)
    current_numviews = None
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _OM_NUMVIEWS_RE.match(line)
            if match:
                current_numviews = int(match.group(2))
                continue
            match = _DRAW_CALL_RE.match(line)
            if match:
                draw_numviews[match.group(1)] = current_numviews
                continue
            match = _DUMP_RE.match(line)
            if not match:
                continue
            draw, name = match.group(1), match.group(2)
            ib_match = _IB_RE.match(name)
            if ib_match:
                ib = ib_match.group(2).lower()
                if ib in part_ibs:
                    draw_ib[draw] = ib
                    ps_match = _PS_IN_NAME_RE.search(name)
                    if ps_match:
                        draw_ps[draw] = ps_match.group(1).lower()
                continue
            slot_match = _SLOT_RE.match(name)
            if slot_match and slot_match.group(1) in draw_ib:
                tex_hash = slot_match.group(3).lower()
                draw_slots[slot_match.group(1)].setdefault(tex_hash, "t" + slot_match.group(2))
    for draw, ib in draw_ib.items():
        ps = draw_ps.get(draw)
        if not ps:
            continue
        nv = draw_numviews.get(draw)
        layouts = result.setdefault(ib, [])
        found = None
        for layout in layouts:
            if layout["numviews"] == nv and layout["ps"] == ps:
                found = layout
                break
        if found is None:
            found = {"numviews": nv, "ps": ps, "slots": {}}
            layouts.append(found)
        found["slots"].update(draw_slots.get(draw, {}))


def _normalize_slot(slot):
    """'ps-t13' / 't13' → 't13'（小写）。"""
    text = str(slot or "").strip().lower()
    return text[3:] if text.startswith("ps-") else text


def efmi_plan_mirror_slots(layouts, orig_hash, primary_slot):
    """单部件镜像推导 → ``[(ps_hash, target_slot, role_tag)]``（去重、稳定序）。

    - orig_hash：该贴图在**原始游戏**里的内容哈希（贴图标记的 mark_hash）；
    - primary_slot：模组当前绑定的槽位（提取时的标记槽，如 ps-t13）；
    - 规则：原图哈希在**非阴影**（NumViews!=0）且**非主层**（槽位 != primary_slot）
      的 pass 里出现 → 需要把模组贴图镜像到那个槽位，并按该 pass 的 PS 标签门控；
    - 主层自动排除（同槽）；阴影 pass 自动排除（rt_width 门控整段不回画）；
    - 角色标签：NumViews>=3 → 99002（G-buffer 族）；其余可见 → 99003（前向第二层族）。
    """
    orig_hash = str(orig_hash or "").strip().lower()
    primary_slot = _normalize_slot(primary_slot)
    if not orig_hash or not primary_slot:
        return []
    mirrors = []
    seen = set()
    for layout in layouts or ():
        numviews = layout.get("numviews")
        if numviews == 0:
            continue
        ps = str(layout.get("ps") or "").lower()
        target = (layout.get("slots") or {}).get(orig_hash)
        if not ps or not target or target == primary_slot:
            continue
        role = EFMI_PASSC_FILTER_INDEX if (numviews or 0) >= 3 else EFMI_LAYER2_FILTER_INDEX
        key = (ps, target, role)
        if key in seen:
            continue
        seen.add(key)
        mirrors.append(key)
    # 输出与日志顺序无关：按（角色标签, 目标槽位, PS）排序——G-buffer(99002) 在前，
    # 第二层(99003) 在后，发射出的 ini 段落稳定可读。
    mirrors.sort(key=lambda item: (item[2], item[1], item[0]))
    return mirrors


def efmi_scan_foreign_shader_tags(ini_texts, needed_hashes):
    """扫别家 ini 文本 → ``{hash: filter_index_str}``（只借不抢）。

    - ini 文本由调用侧读取（本模块不碰文件 IO 的路径决策，可单测）；
    - 只认 ShaderOverride/ShaderRegex 段里成对出现的 hash + filter_index；
    - 注释掉的段（``;[...]``）与没写 filter_index 的段不算占用；
    - 本工具自己的段（PassMirror/ShadowPS）跳过，防止自我借标签。
    """
    needed = {str(v or "").strip().lower() for v in (needed_hashes or ())} - {""}
    found = {}
    if not needed:
        return found
    for text in ini_texts or ():
        for section in _FOREIGN_SECTION_RE.finditer(text or ""):
            name = section.group(1)
            if any(token in name for token in _OWN_SECTION_TOKENS):
                continue
            body = section.group(2)
            hash_match = _HASH_LINE_RE.search(body)
            filter_match = _FILTER_LINE_RE.search(body)
            if not hash_match or not filter_match:
                continue
            value = hash_match.group(1).lower()
            if value in needed:
                found[value] = filter_match.group(1)
    return found


def efmi_mirror_condition(our_tag, foreign_tags):
    """门控条件文本：本工具私号 + 别家已注册的同哈希标签（谁生效都成立）。

    别家标签若恰好等于本工具私号（例如模组旧输出已被扫描到），只保留一份，
    绝不输出 ``ps == 99002 || ps == 99002`` 这种重复条件。
    """
    tags = [str(our_tag)]
    for value in sorted({str(t) for t in (foreign_tags or ()) if str(t).strip()}):
        if value not in tags:
            tags.append(value)
    return " || ".join("ps == " + tag for tag in tags)


def efmi_scan_regex_taggers(ini_texts):
    """探测 Mods 目录里的**正则打标器** → ``{filter_index_str: 段名}``。

    RabbitFX 这类 ShaderRegex 按**字节码特征**打标签（如 G-buffer 家族 1718.2），
    段里没有 hash 行，成对扫描（efmi_scan_foreign_shader_tags）看不到它们。
    本工具自注册 ShaderOverride 时优先级天然压过 ShaderRegex（d3dx 规则）——
    若被注册的哈希恰好也被某正则打标器命中，那个打标器对该 shader 失效。
    本函数只负责**发现并点名**（发射侧据此打印提醒与手动借标签配方），
    不把正则标签自动借进条件（家族归属不明时借标签 = 99001 类事故方向）。
    """
    found = {}
    for text in ini_texts or ():
        for section in _FOREIGN_SECTION_RE.finditer(text or ""):
            name = section.group(1)
            if any(token in name for token in _OWN_SECTION_TOKENS):
                continue
            body = section.group(2)
            if _HASH_LINE_RE.search(body):
                continue
            filter_match = _FILTER_LINE_RE.search(body)
            if filter_match:
                found[filter_match.group(1)] = name
    return found


def efmi_mirror_block_lines(condition_text, target_slot, resource_name):
    """镜像块：``if <条件>`` / ``ps-t<N> = <资源>`` / ``endif``。"""
    slot = _normalize_slot(target_slot)
    return [
        "if " + str(condition_text),
        "ps-" + slot + " = " + str(resource_name),
        "endif",
    ]


def efmi_pass_override_section_name(mod_name, tag):
    """注册段基名：ShaderOverride_PassMirror<tag>_<模组名 ASCII 化>。

    模组名全非 ASCII（如中文角色名）时退回其 sha1 前 8 位——确定性、无特殊字符。
    """
    raw = str(mod_name or "")
    token = re.sub(r"[^0-9A-Za-z]+", "", raw)[:24]
    if not token:
        token = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return "ShaderOverride_PassMirror%s_%s" % (tag, token)


def efmi_pass_override_lines(mod_name, tag, ps_hashes):
    """ShaderOverride 注册段：把待自注册的 ps 全集都注册成同一个角色标签。

    调用侧必须先剔除已被别家注册的哈希（efmi_scan_foreign_shader_tags）——
    只借不抢（99001 事故教训）。``allow_duplicate_hash = overrule`` 仅为对抗
    未知未来重复段兜底。
    """
    base_name = efmi_pass_override_section_name(mod_name, tag)
    hashes = [
        str(value).strip().lower()
        for value in (ps_hashes or ())
        if str(value or "").strip()
    ]
    lines = []
    for index, ps_hash in enumerate(hashes, start=1):
        name = base_name if len(hashes) == 1 else "%s_%d" % (base_name, index)
        lines.append("[" + name + "]")
        lines.append("hash = " + ps_hash)
        lines.append("filter_index = " + str(tag))
        lines.append("allow_duplicate_hash = overrule")
        lines.append("")
    return lines


# ------------------------------------------------------------------------------
# - 工作空间持久化（导入时算好存下来，生成时不再依赖提取文件还在）
# ------------------------------------------------------------------------------

# 布局缓存文件版本：结构变更时递增，读侧对不上就整份作废重扫。
EFMI_PASS_LAYOUTS_VERSION = 1


def efmi_pass_layouts_file_path(workspace_root):
    """布局缓存位置：``<工作空间>/Config/PassLayouts.json``（与 FrameAnalysisPath 同级）。"""
    return os.path.join(str(workspace_root or "").strip(), "Config", "PassLayouts.json")


def _log_fingerprint(log_path):
    """来源日志指纹（mtime_ns + size）：用于生成侧判断缓存是否已被新抓帧淘汰。"""
    try:
        stat = os.stat(log_path)
    except OSError:
        return None
    return {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}


def efmi_merge_pass_layouts(old_layouts, new_layouts):
    """增量合并：新扫描到的 IB 整组覆盖旧条目（最新抓帧为准），未触及的 IB 保留。

    分批导入同一工作空间时，每批只更新自己那些部件的布局，旧批次部件不丢。
    """
    merged = {str(ib).lower(): list(layouts or []) for ib, layouts in (old_layouts or {}).items()}
    for ib, layouts in (new_layouts or {}).items():
        merged[str(ib).lower()] = list(layouts or [])
    return merged


def efmi_write_pass_layouts(workspace_root, layouts, source_logs):
    """导入时把 pass 布局写回工作空间（含版本与来源指纹）。

    source_logs 只记录**实际参与扫描且存在**的 log 路径与当时指纹；
    写失败返回 False（不阻断导入），成功返回缓存文件路径。
    """
    path = efmi_pass_layouts_file_path(workspace_root)
    if not path.strip(os.sep) or not str(workspace_root or "").strip():
        return False
    sources = []
    for log_path in source_logs or ():
        fingerprint = _log_fingerprint(log_path)
        if fingerprint is None:
            continue
        sources.append({"path": str(log_path), **fingerprint})
    payload = {
        "version": EFMI_PASS_LAYOUTS_VERSION,
        "sources": sources,
        "layouts": {
            str(ib).lower(): [
                {
                    "numviews": layout.get("numviews"),
                    "ps": layout.get("ps"),
                    "slots": dict(layout.get("slots") or {}),
                }
                for layout in (layouts or [])
            ]
            for ib, layouts in (layouts or {}).items()
        },
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        os.replace(tmp_path, path)
    except OSError:
        return False
    return path


def efmi_read_pass_layouts(workspace_root, part_ibs=None):
    """读工作空间的 pass 布局缓存 → ``{ib: [layout]}``（可按 part_ibs 过滤）。

    失效判定（返回 {} → 调用侧回退实时扫描）：
    - 文件缺失/版本不符/JSON 损坏；
    - 任一**仍存在**的来源日志比记录时更新（mtime_ns 变大）→ 有新抓帧，缓存淘汰；
    - 来源日志已被删除/挪动 → 不算失效（这正是缓存存在的意义：提取文件没了也能用）。
    """
    path = efmi_pass_layouts_file_path(workspace_root)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != EFMI_PASS_LAYOUTS_VERSION:
        return {}
    for source in payload.get("sources", []) or []:
        source_path = str(source.get("path", "") or "")
        if not source_path or not os.path.isfile(source_path):
            continue
        fingerprint = _log_fingerprint(source_path)
        if fingerprint and int(fingerprint["mtime_ns"]) > int(source.get("mtime_ns", 0)):
            return {}
    wanted = {str(v or "").strip().lower() for v in (part_ibs or ())} - {""}
    layouts = {}
    for ib, entries in (payload.get("layouts", {}) or {}).items():
        ib_key = str(ib).lower()
        if wanted and ib_key not in wanted:
            continue
        normalized = []
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("ps"):
                continue
            normalized.append({
                "numviews": entry.get("numviews"),
                "ps": str(entry.get("ps")).lower(),
                "slots": {str(k).lower(): str(v) for k, v in (entry.get("slots") or {}).items()},
            })
        if normalized:
            layouts[ib_key] = normalized
    return layouts
