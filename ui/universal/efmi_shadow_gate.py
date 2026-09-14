# -*- coding: utf-8 -*-
"""EFMI 阴影 pass 单投射源门控（2026-09-13 实机定位；同日改为 ps 口径 + 抓帧推导）。

背景（抓帧实证，FrameAnalysis-2026-09-13-081358）：
同一逻辑部件若同时存在 LOD0/LOD1 两个独立 component，游戏会在**深度/阴影类 pass**里
把该部件画多次（身体 = LOD1 x2 级联 + LOD0 x1）。EFMI 的骨骼矩阵是「按 component 分帧
独立导入」的，两套矩阵在运动时必然错开 → 同一张阴影图里出现两个姿态的同一部件 →
光照 pass 把错位的那份当成遮挡 = 身体上的自遮黑块（与地面影子同形；静止时两套
重合故不可见）。

修法（实机验证：黑块消失、地面影子保留）：同一逻辑部件只让 LOD1 那一份参与投影，
LOD0 入口在这些 pass 内不再回画（handling = skip 仍在门控外，原版网格全 pass 压制）。

三条硬约束（各自都有过实机失效记录，勿回退）：

1. **配对键既不是部件名/IB，也不是原样 obj_name**。实机身体的 LOD0 与 LOD1 是两个
   不同 IB（d6128f13-14664-0 vs 2dca919f-7596-0），按裸名永远配不上；而 drawcall 的
   obj_name 是 SSMT 运行时名（``LOD<n>.<ib>-<index_count>-<first_index>.<物体名>``），
   且 LOD1 由链路分叉复制而来、带 ``_chain<N>`` 后缀
   （blueprint/chain_traverser.py 的 _duplicate_chain_object 生成），
   原样比较同样永远不相等——2026-09-13 首版生成器（98d9856）即因此对一个 16 部件
   的模组**一条门控都没发射**。
   本模块用 ``efmi_normalize_obj_name`` 剥掉 SSMT 前缀 + 归一 ``_chain<N>`` 后缀，
   让同一源物体导出的各 LOD 收敛到同一键（实机 16 部件 → 5 组配对全部命中）。

2. **判 pass 用 ps 标签，不用 vs、也不用 DRAW_TYPE**。
   - ``vs == 200`` 那类写法不是内置阶段号，而是「把某个游戏 shader 打上 filter_index
     标签、再比标签」（参考模组 伊冯.ini：``[ShaderOverridevs1000] hash=<她的VS>
     filter_index = 200`` → ``if vs == 200``）。本模块是同一套机制，锚在 PS 上。
     锚点选择有实测依据：同一帧里阴影 ps 服务 **212 个 IB**（08-31 那次 189 个 IB、
     其中 60 种不同材质 ps 的网格共用它），而 VS 只覆盖 6~28 个 IB（按网格/外形）
     → **PS 是 pass 级公共着色器，角色无关性更强**，比 vs 口径更不容易在换角色时失效。
   - ``DRAW_TYPE`` 是**绘制调用种类**不是阶段号：实测阴影 ps 的 400 笔 draw 与材质 ps
     的几笔**全部**是 ``DrawIndexedInstanced``（全帧还有种类 8）→ 拿它判阶段会误伤
     所有同类绘制（EFMI core 只用它做 ``if DRAW_TYPE % 2 == 0`` 这种粗筛）。
   - ``NumViews`` 能干净分离阶段（实测：本模组部件在深度/阴影 pass 全 NumViews=0，
     在可见 pass 全 2/5），但它只是抓帧日志字段，本机 d3dx.ini 与任何 ini 里都没有
     这个运行期变量；运行期等价物 ``o0`` 的「未绑定资源 = -0.0」按 d3dx.ini:833-834
     必须在**着色器里**用 ``asint(...) == asint(-0.0)`` 判，不是普通 ini 条件。
     所以「无 RT」这个判据只在**导出期**可用——这正是本模块的推导入口。

3. **阶段标签值必须是私有号段（``EFMI_SHADOW_PS_FILTER_INDEX = 99001``），绝不能借用
   别家 mod 的号**。这是一次实机事故换来的教训（2026-09-15，v4.4.45 首版曾写成 1718.2）：
   - RabbitFX 会给它改写的 shader 打 ``filter_index = 1718.2``，而它改写的正是**角色可见
     G-buffer 的 shader**。实测该角色有 **5 个可见 ps**（816908364e93e433 / 6ad403320174e5dc /
     31e969822a004ce4 / f3926abc95fa801a / 00cf31fc5c40c10d）被它打了 1718.2。
   - 于是 ``if ps != 1718.2`` 在**可见 pass** 里也不成立 → 门控关闭 → 该部件 LOD0 那份
     不重画，而 ``handling = skip`` 又压掉了原版绘制 → **可见外壳整块消失，只剩内侧/背面**
     （实机症状："正面被剔除，只剩背面"）。
   - 换回私有 99001 后：RabbitFX 的 1718.x 标签碰不到本门控（只有我们自己的注册段会把
     ``ps`` 置成 99001，而那正是深度/阴影 pass）→ 自遮黑块修复与 FX 共存。
   - 当初"与生态对齐同值"的推理是**方向反了**：风险不是"我们的标签顶掉别家条件"，而是
     **别家的标签会关掉我们的门控**。d3dx 的优先级规则（ShaderOverride 优先于 ShaderRegex）
     只在两边都命中同一个 shader 时才是"同值等价"。
   - 私有号段的唯一代价：若将来别家也用 99001，我们的 override 可能顶掉它的标签；因此
     99001 是「本工具专用」的保留号，改动它前先全机 grep 一遍。

**pass ps 全集是导出期从抓帧推导的，不是硬编常量（2026-09-13 实测缺口）**：
深度/阴影类 pass 不止一个 ps。按「NumViews==0 且绘制了本模组部件」统计 08-31 抓帧：

    阴影 ps=d7bb9dd57f5b70c6  NumViews=0  x14   ← 常量里那一个
    阴影 ps=044b75548e7c9fd7  NumViews=0  x6    ← 只注册一个就漏掉
    阴影 ps=a357a884e01209a0  NumViews=0  x2    ← 只注册一个就漏掉

只门控其中一个 → 其余 pass 里的 LOD0 重复投射不受控。故导出时按抓帧推导全集并**全部**
注册成同一标签（``efmi_derive_shadow_ps_hashes``）；抓帧不可用时才退回常量
``EFMI_SHADOW_FALLBACK_PS_HASHES``。同一 ps 若**也**用于有 RT 的 pass，则剔除不敢门控。

**失效方向**：抓帧推导得到的 pass 若与实机不符，最坏只是门控不生效（自遮黑块回到修复前，
可见、不破坏网格），不会让可见网格消失——因为被门控的 EntryPoint 只匹配本模组自己的 IB。

本模块只放纯判定/纯扫描（无 bpy 依赖，可脱离 Blender 单测）；发射侧见 ui/universal/efmi.py。
"""
import hashlib
import os
import re

# 兜底用的阴影/深度 pass 的像素着色器 hash（实机抓帧：阴影图里身体被画 3 次的那一笔 ps；
# 与 blueprint/node_postprocess_draginteraction_efmi.py 的 DEFAULT_PROBE_PASS_HASH 同源）。
EFMI_SHADOW_PASS_PS_HASH = "d7bb9dd57f5b70c6"
EFMI_SHADOW_FALLBACK_PS_HASHES = (EFMI_SHADOW_PASS_PS_HASH,)

# 阶段标签：**本工具专用的私有号段**（见模块文档第 3 条——实机事故：借用 RabbitFX 的
# 1718.2 会让它的可见 pass 标签把本门控关掉，导致可见外壳消失）。
# 改动前先全机 grep 一遍，确认没有别的 mod 用这个号。
EFMI_SHADOW_PS_FILTER_INDEX = 99001

# 参与投影的 LOD 与需要被门控的 LOD（实机验证的组合：留 LOD1，门控 LOD0）
EFMI_SHADOW_KEEP_LOD = 1
EFMI_SHADOW_GATE_LOD = 0

EFMI_SHADOW_SOURCE_FRAME_ANALYSIS = "frame_analysis"
EFMI_SHADOW_SOURCE_FALLBACK = "fallback"
# 抓帧已证明「兜底常量也用于有 RT 的 pass」→ 宁可完全不门控（fail-open），
# 绝不注册一个会命中可见 pass 的标签。
EFMI_SHADOW_SOURCE_NOT_GATED = "not_gated"

_LOD_PREFIX_RE = re.compile(r"^LOD(\d+)\.")
# SSMT 运行时名前缀：LOD<n>.<ib>-<index_count>-<first_index>.
_SSMT_PREFIX_RE = re.compile(r"^LOD\d+\.[^-]+-\d+-\d+\.")
# 链路分叉复制后缀 _chain<N>（可能后接 _copy）
_CHAIN_SUFFIX_RE = re.compile(r"_chain\d+(?=_copy$|$)")

# FrameAnalysis 日志行（格式见 EFMILogParser 与实机日志）：
#   `000015 OMSetRenderTargets(NumViews:0, ppRenderTargetViews:0x..., ...)`
#   `000015 DrawIndexedInstanced(IndexCountPerInstance:..., ...)`
#   `000015 3DMigoto Dumping Buffer <dir>\000015-ib=d6128f13(1d6a6186)-vs=...-ps=....buf -> ...`
_OM_NUMVIEWS_RE = re.compile(r"^(\d{6}) OMSetRenderTargets\(NumViews:(\d+)")
_DRAW_CALL_RE = re.compile(r"^(\d{6}) (Draw[A-Za-z]*)\(")
_DUMP_RE = re.compile(r"^\d{6} 3DMigoto Dumping \S+ (.+?) -> ")
_DUMP_IB_RE = re.compile(r"(\d{6})-ib=([0-9a-f]{8})(?:\(([0-9a-f]{8})\))?")
_DUMP_PS_RE = re.compile(r"(\d{6})-.*?ps=([0-9a-f]{16})")


def efmi_bare_part_key(unique_str):
    """去掉 LOD<n>. 前缀后的部件裸名（LOD0.x-y-z → x-y-z）。

    只用于日志/诊断：**不可**作为跨 LOD 配对键（不同 LOD 可以是不同 IB）。
    """
    return _LOD_PREFIX_RE.sub("", str(unique_str or ""))


def efmi_lod_index(unique_str):
    """LOD 序号；无 LOD 前缀时返回 None。"""
    match = _LOD_PREFIX_RE.match(str(unique_str or ""))
    return int(match.group(1)) if match else None


def efmi_normalize_obj_name(obj_name):
    """drawcall 的 obj_name → 逻辑部件键（同一源物体各 LOD 收敛到同一字符串）。

    两步：
    1. 有 SSMT 前缀（``LOD<n>.<ib>-<index_count>-<first_index>.``）时剥掉，
       只留物体名（皮肤.001_copy / 皮肤.001_chain1_copy）；
    2. 去掉链路分叉复制后缀 ``_chain<N>``（皮肤.001_chain1_copy → 皮肤.001_copy）。

    前缀不匹配时保底只做第 2 步：没有 SSMT 前缀的工程（无前缀命名）里，
    各 LOD 仍能靠 ``_chain<N>`` 归一收敛；而**仍带 LOD 前缀**的名字原样保留，
    于是不同 LOD 的占位件永远不会被误当成同一个逻辑部件。
    """
    name = str(obj_name or "").strip()
    if not name:
        return ""
    match = _SSMT_PREFIX_RE.match(name)
    if match:
        name = name[match.end():]
    name = _CHAIN_SUFFIX_RE.sub("", name)
    return name.strip()


def efmi_part_key(obj_names):
    """逻辑部件键 = 该部件各 drawcall obj_name 归一化后的集合。

    键是 frozenset（同部件多 drawcall 时要求整组一致，避免单件撞名误配）。
    归一化后为空（无 drawcall 物体名）时返回空 frozenset = 无法配对。
    """
    keys = set()
    for name in (obj_names or ()):
        normalized = efmi_normalize_obj_name(name)
        if normalized:
            keys.add(normalized)
    return frozenset(keys)


def efmi_shadow_part_keys(part_obj_names):
    """(unique_str -> 逻辑部件键, 参与投影那一档的键集合)。

    part_obj_names: 可迭代的 ``(unique_str, 该部件的 drawcall obj_name 可迭代)``。
    同一 unique_str 出现多次时合并（取并集）。无键的部件不进 by_part
    → 调用侧拿不到键 → 不门控（宁可少改）。
    """
    merged = {}
    for unique_str, obj_names in (part_obj_names or ()):
        name = str(unique_str or "")
        if not name:
            continue
        merged[name] = merged.get(name, frozenset()) | efmi_part_key(obj_names)
    by_part = {key: value for key, value in merged.items() if value}
    keep_keys = frozenset(
        value
        for key, value in by_part.items()
        if efmi_lod_index(key) == EFMI_SHADOW_KEEP_LOD
    )
    return by_part, keep_keys


def efmi_shadow_gate_needed(
    part_lod,
    part_key,
    keep_lod_keys,
    keep_lod=EFMI_SHADOW_KEEP_LOD,
    gate_lod=EFMI_SHADOW_GATE_LOD,
):
    """该部件入口是否需要发射「阴影 pass 不重复投射」门控。

    参数
    ----
    part_lod : 本部件的 LOD 序号（无 LOD 前缀传 None）。
    part_key : 本部件的**逻辑部件键**（同一源物体各 LOD 共用同一个键，需可哈希）。
    keep_lod_keys : 参与投影那一档（默认 LOD1）全部部件的逻辑部件键集合。

    判定：本部件 LOD == gate_lod，且其逻辑部件键出现在 keep_lod_keys 中
    —— 即该逻辑部件在阴影 pass 里确实会被两套 component 各投一遍。
    没有对应 LOD 兄弟的部件返回 False：行为与修复前一致，无回归。
    """
    if part_lod != gate_lod:
        return False
    if part_key is None:
        return False
    return part_key in set(keep_lod_keys or ())


def efmi_shadow_filter_index_text():
    """阶段标签的 ini 字面量（整数直接输出；将来若改回小数，也能输出 1718.2 那样的形式）。"""
    value = EFMI_SHADOW_PS_FILTER_INDEX
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else ("%g" % number)


def efmi_scan_shadow_ps_hashes(log_path, part_ibs):
    """扫一份 FrameAnalysis log.txt，返回 (无RT pass 的 ps 集合, 有RT pass 的 ps 集合)。

    规则（实机抓帧实测）：
    - ``OMSetRenderTargets(NumViews:N)`` 是**状态变更**日志，逐 draw 记录最近一次的值
      → 在 draw 调用行上取当前值即该 draw 的 RT 情况；N==0 = 无颜色输出 = 深度/阴影类 pass。
    - dump 文件名带 ``ib=<hash>(<原始hash>)`` 与 ``ps=<hash>``，按 draw 序号归并。
    - 只收「该 draw 的 IB 命中本模组部件（part_ibs）」的 ps。

    文件缺失/不可读/无记录 → 返回两个空集合（调用侧据此回退常量）。
    """
    part_ibs = {
        str(value or "").strip().lower() for value in (part_ibs or ())
    } - {""}
    if not part_ibs or not log_path or not os.path.isfile(log_path):
        return set(), set()

    draw_numviews = {}
    current_numviews = None
    draw_ibs = {}
    draw_ps = {}
    try:
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
                name = os.path.basename(match.group(1))
                ib_match = _DUMP_IB_RE.search(name)
                if ib_match:
                    hashes = {
                        value.lower()
                        for value in ib_match.groups()
                        if value
                    }
                    matched = hashes & part_ibs
                    if matched:
                        draw_ibs.setdefault(ib_match.group(1), set()).update(matched)
                ps_match = _DUMP_PS_RE.search(name)
                if ps_match:
                    draw_ps.setdefault(ps_match.group(1), set()).add(
                        ps_match.group(2).lower()
                    )
    except OSError:
        return set(), set()

    no_rt, with_rt = set(), set()
    for draw, hashes in draw_ibs.items():
        if not hashes:
            continue
        for ps in draw_ps.get(draw, ()):
            if draw_numviews.get(draw) == 0:
                no_rt.add(ps)
            else:
                with_rt.add(ps)
    return no_rt, with_rt


def efmi_derive_shadow_ps_hashes(
    log_paths,
    part_ibs,
    fallback=EFMI_SHADOW_FALLBACK_PS_HASHES,
):
    """导出期推导「深度/阴影类 pass」的 ps 全集 → ``(ps 元组, 来源)``。

    - 多份 log（工作空间级 + 各 LOD tab）取并集；
    - **剔除同时出现在有 RT pass 的 ps**：同一 ps 跨两类 pass 时不敢门控
      （若它在可见 pass 也生效，门控会让可见网格缺件）；
    - 推导为空：抓帧不可用/本模组部件没出现在无 RT pass → 回退 ``fallback``；
      但**兜底项同样要过一遍剔除**——若抓帧已证明某个兜底常量也用于有 RT 的 pass，
      就不能再注册它（否则正是"可见 pass 被门控"的灾难方向）；若剔除后什么都不剩，
      返回**空元组 + NOT_GATED**，调用侧据此完全不发射门控（fail-open）。
    """
    no_rt, with_rt = set(), set()
    for log_path in (log_paths or ()):
        scan_no_rt, scan_with_rt = efmi_scan_shadow_ps_hashes(log_path, part_ibs)
        no_rt |= scan_no_rt
        with_rt |= scan_with_rt

    derived = tuple(sorted(no_rt - with_rt))
    if derived:
        return derived, EFMI_SHADOW_SOURCE_FRAME_ANALYSIS

    fallback_hashes = tuple(
        str(value).strip().lower()
        for value in (fallback or ())
        if str(value or "").strip()
    )
    kept = tuple(value for value in fallback_hashes if value not in with_rt)
    if fallback_hashes and not kept:
        # 抓帧已证明兜底常量落在有 RT 的 pass 上 → 不门控
        return (), EFMI_SHADOW_SOURCE_NOT_GATED
    return kept, EFMI_SHADOW_SOURCE_FALLBACK


def efmi_shadow_gate_open_lines():
    """门控开启行（配套发射 ``endif`` 关闭）。

    条件用 ps 阶段标签——标签由本模组发射的 ``[ShaderOverride...]``
    （见 efmi_shadow_override_lines）注册；可见 pass 的 ps 不命中它，故只影响
    深度/阴影类 pass。
    """
    return [
        "; [shadow-gate] 同部件 LOD%d 已在深度/阴影 pass 投射，本入口不重复投射"
        % EFMI_SHADOW_KEEP_LOD,
        "if ps != %s" % efmi_shadow_filter_index_text(),
    ]


def efmi_shadow_override_section_name(mod_name):
    """本模组阴影阶段标签的 ShaderOverride 段名基名（模组间唯一）。

    用模组名（工作空间名）的 ASCII 部分做后缀；模组名全非 ASCII（如中文角色名）
    时退回其 sha1 前 8 位——确定性、无特殊字符，也不会因为改名以外的原因漂移。
    """
    raw = str(mod_name or "")
    token = re.sub(r"[^0-9A-Za-z]+", "", raw)[:24]
    if not token:
        token = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return "ShaderOverride_ShadowPS_" + token


def efmi_shadow_override_lines(section_name, ps_hashes=None):
    """``[ShaderOverride...]`` 段：把推导出的 ps 全集都注册成同一个阶段标签。

    - 每个 hash 一个段（一段只能匹配一个 hash）；多个 hash 时段名加 ``_N`` 后缀。
    - ``allow_duplicate_hash = overrule``：同 hash 可能另有其它 ShaderOverride
      （参考模组/框架/后处理导出），允许重复注册并由本段生效。标签是本工具私有号段
      （见模块文档第 3 条），不要改成别家 mod 用的号（如 1718.x）——那会让别家的阶段
      标签把本门控在可见 pass 里关掉。
    """
    base_name = str(section_name or "").strip() or "ShaderOverride_ShadowPS"
    hashes = [
        str(value).strip().lower()
        for value in (ps_hashes if ps_hashes is not None else EFMI_SHADOW_FALLBACK_PS_HASHES)
        if str(value or "").strip()
    ]
    if not hashes:
        hashes = list(EFMI_SHADOW_FALLBACK_PS_HASHES)
    filter_text = efmi_shadow_filter_index_text()
    lines = []
    for index, ps_hash in enumerate(hashes, start=1):
        name = base_name if len(hashes) == 1 else "%s_%d" % (base_name, index)
        lines.append("[" + name + "]")
        lines.append("hash = " + ps_hash)
        lines.append("filter_index = " + filter_text)
        lines.append("allow_duplicate_hash = overrule")
        lines.append("")
    return lines
