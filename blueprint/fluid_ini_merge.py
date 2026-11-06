# -*- coding: utf-8 -*-
"""流体探针的 INI 文本加工（纯函数，不依赖 bpy）。

把「宿主 ini 拆成三段 / 注入常量与 Present / 追加段块」这套纯文本操作
从节点里拆出来，节点只负责读盘、写盘与调用，测试可以直接 import 本模块。
"""

from __future__ import annotations

import re
from collections import OrderedDict

TAIL_START = "; --- AUTO-APPENDED FLUID PROBE MODULE ---"
TAIL_END = "; --- END AUTO-APPENDED FLUID PROBE MODULE ---"
CONST_START = "; FluidProbe:Constants:Start"
CONST_END = "; FluidProbe:Constants:End"
PRESENT_START = "; FluidProbe:Present:Start"
PRESENT_END = "; FluidProbe:Present:End"

DRIVER_START = "; --- ANIMATION DRIVER SECTION ---"
DRIVER_END = "; --- END ANIMATION DRIVER SECTION ---"

#: 宿主 ini 里其它后处理模块写下的 AUTO-APPENDED 尾巴起点。
AUTO_APPENDED_MARKERS = (
    "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
    "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
    "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---",
)
AUTO_APPENDED_PREFIXES = (
    "; --- AUTO-APPENDED UI PANEL ",
    "; --- AUTO-APPENDED CUSTOM TEXT ",
    "; --- AUTO-APPENDED SKIP ",
)

#: 原版 Probe.ini 在每个关键判断里都带 EFMI 的命名空间总闸变量。ZZMI 没有这个
#: 命名空间变量，条件恒假 ⇒ 流体永远不渲染。导出时换成下面这个自有变量。
EFMI_GATE_VAR = "$\\EFMIv1\\enable_mods"
ENGINE_GATE_VAR = "$fluid_enable_mods"


def apply_engine_gate(text):
    """把 EFMI 的总闸变量换成自有变量（初值由节点写进 ``[Constants]``）。"""
    return text.replace(EFMI_GATE_VAR, ENGINE_GATE_VAR)


#: 3Dmigoto 的 ``$var`` 没有命名空间隔离：同一个进程里所有 ini 共用一个变量表。
#: 模板里的 ``$enabled`` / ``$drawn`` / ``$seen`` / ``$capture`` / ``$slot_count``
#: 这种名字，和别的 mod 撞上就是互相覆写，而且症状极隐蔽——不是报错，是
#: 「开了没反应」或者「别人的开关能控制我的水」。
#:
#: 所以导出时把**每一个**变量都加上 ``fluid_`` 前缀。``$`` 后面紧跟 ``\`` 的
#: 命名空间引用（``$\EFMIv1\enable_mods``）不匹配这个正则，不会被误改。
VARIABLE_PREFIX = "fluid_"

_VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def prefix_variables(text):
    """给文本里所有 ``$var`` 加 ``fluid_`` 前缀，已带前缀的不动。"""
    def _sub(match):
        name = match.group(1)
        if name.startswith(VARIABLE_PREFIX):
            return match.group(0)
        return "$" + VARIABLE_PREFIX + name

    return _VAR_RE.sub(_sub, text)


def parse_sections(text):
    """按 `[段名]` 切段，返回 OrderedDict[str, list[str]]。"""
    sections = OrderedDict()
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def strip_marked_text(text, start, end):
    """删掉 `start` 与 `end` 之间的所有行（含两个标记本身）。"""
    if not text:
        return text
    out = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == start:
            inside = True
            continue
        if stripped == end:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)


def split_driver(text):
    """切出动画驱动块，返回 (driver, 其余文本)。"""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if DRIVER_START in line), None)
    if start is None:
        return "", text
    end = next((i for i in range(start + 1, len(lines)) if DRIVER_END in lines[i]), None)
    if end is None:
        return "", text
    driver = "".join(lines[start:end + 1])
    before = "".join(lines[:start]).rstrip("\r\n")
    after = "".join(lines[end + 1:]).lstrip("\r\n")
    return driver, "\n\n".join(part for part in (before, after) if part)


def split_tail(text):
    """切出其它模块的 AUTO-APPENDED 尾巴，返回 (body, tail)。"""
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped in AUTO_APPENDED_MARKERS or any(
            stripped.startswith(prefix) for prefix in AUTO_APPENDED_PREFIXES
        ):
            return text[:offset], text[offset:]
        offset += len(line)
    return text, ""


def read_host(text):
    """把宿主 ini 拆成 (body, tail, driver)，并剥掉上一轮注入的流体块。"""
    driver, rest = split_driver(text)
    body, tail = split_tail(rest)
    body = strip_marked_text(body, CONST_START, CONST_END)
    body = strip_marked_text(body, PRESENT_START, PRESENT_END)
    # 宿主没有 AUTO-APPENDED 尾巴时，流体段块会落在 body 里
    body = strip_marked_text(body, TAIL_START, TAIL_END)
    tail = strip_marked_text(tail, TAIL_START, TAIL_END)
    return body, tail, driver


def _as_text(value):
    """模板里的段落既可能是文本，也可能是行列表，统一成文本。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return "\n".join(str(line) for line in value)


def merge_constants(body, const_text):
    """把流体常量块插到宿主第一个 [Constants] 之后。"""
    block = [CONST_START] + _as_text(const_text).splitlines() + [CONST_END]
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "[Constants]":
            lines[index + 1:index + 1] = block
            return "\n".join(lines)
    return "\n".join(["[Constants]"] + block + lines)


def merge_present(body, present_text):
    """把流体 Present 命令块插到宿主 [Present] 段尾。"""
    block = [PRESENT_START] + _as_text(present_text).splitlines() + [PRESENT_END]
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "[Present]":
            continue
        insert_at = len(lines)
        for probe in range(index + 1, len(lines)):
            stripped = lines[probe].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                insert_at = probe
                break
        lines[insert_at:insert_at] = block
        return "\n".join(lines)
    return "\n".join(lines + ["", "[Present]"] + block)


def compose_host(body, tail, driver, sections):
    """拼回完整 ini：driver + body + 宿主尾巴 + 流体段块。

    `sections` 是 OrderedDict[str, str]，Constants / Present 已在 body 里处理，
    这里只追加其余段。每段先剥行尾空行，否则反复导出会在段间累积空行。
    """
    body = merge_constants(body, sections.get("Constants", ""))
    body = merge_present(body, sections.get("Present", ""))

    parts = [TAIL_START]
    for name, text in sections.items():
        if name in ("Constants", "Present"):
            continue
        parts.append("")
        parts.append("[{}]".format(name))
        parts.extend(_as_text(text).splitlines())
    parts.append(TAIL_END)

    segments = []
    if driver.strip():
        segments.append(driver)
    if body.strip():
        segments.append(body)
    if tail.strip():
        segments.append(tail)
    segments.append("\n".join(parts))

    text = "\n\n".join(piece.rstrip("\r\n") for piece in segments if piece.strip()) + "\n"
    # 同一次导出里每个 apply_* 都可能新开一个 [Constants]；3Dmigoto 对同名段的
    # 合并行为不值得赌，统一并成一段（见 collapse_constants 的注释）。
    return collapse_constants(text)


# ---------------------------------------------------------------------------
# 宿主 ini 的绘制单元 → (物体, 面序号) 反算首索引
# ---------------------------------------------------------------------------
#: 段体里的部件锚点注释 ``; [mesh:物体名]``（导出期由 SSMT 写进绘制单元起点，
#: 见 ``blueprint\\ini_scope.py:44``）。这里只读不写。
MESH_COMMENT_RE = re.compile(r"^;\s*\[mesh:(?P<name>[^\]]+)\]", re.IGNORECASE)

#: ``drawindexed = count, start, baseVertex``。
DRAW_INDEXED_RE = re.compile(
    r"^[ \t]*drawindexed\s*=\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)[ \t]*$", re.M
)


def locate_draw_line(ini_text, object_name, first_index):
    """找到该插 hook 的那一行 ``drawindexed``，返回 ``(行尾偏移, 该行缩进)``。

    两个条件必须**同时**成立：
    1. 这一行前面最后一条 ``; [mesh:名]`` 注释与 ``object_name`` 指同一个物体；
    2. ``first_index`` 落在这行的 ``[start, start+count)`` 区间里。

    为什么不能只按区间：多部件角色每个部件有自己的 IB，``drawindexed`` 的 start
    常常都是 0。实测 ``菲欧妮.ini`` 里 ``LOD0.9040e2c4-16194-56844_copy`` 是
    ``drawindexed = 72336, 0, 0``、``LOD0.b11ff137-6078-0_copy`` 是
    ``drawindexed = 38448, 0, 0`` —— 面序号 12516 反算出 37548 后**两个区间都命中**，
    只按区间取第一个会把那股水喷到别的部件上，而画面里看起来"也在喷"，
    极难查。所以这里先用 mesh 注释锁行，再验区间。
    """
    cur = None
    offset = 0
    for line in ini_text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        m = MESH_COMMENT_RE.match(body.strip())
        if m:
            cur = m.group("name")
            offset += len(line)
            continue
        d = DRAW_INDEXED_RE.match(body)
        if d and cur:
            count = int(d.group(1))
            start = int(d.group(2))
            if mesh_names_match(cur, object_name) and start <= first_index < start + count:
                indent = body[:len(body) - len(body.lstrip())]
                return offset + len(body), indent
        offset += len(line)
    return None


def scan_mesh_draws(ini_text):
    """把宿主 ini 里的 draw 语句按 ``; [mesh:名]`` 归属到网格。

    返回 ``[(mesh_name, start, count, base_vertex), ...]``，按出现顺序。
    ``mesh_name`` 为 ``None`` 表示这段 draw 前面没有 mesh 注释。

    为什么需要这层：``drawindexed`` 的 ``start`` 是**整个 IB 的绝对索引**，
    而 Blender 的 ``polygon.index`` 是**物体内**的序号。两者差一个 ``start``
    偏移，所以 ``first_index // 3`` 并不是物体内的面号——只有在 draw 从索引 0
    起时才碰巧相等。参考实现里 ``y243 = 883239`` 落在 ``723363..892827``
    这段 draw 中间，换算成物体内面号是 ``(883239-723363)//3 = 53292``，
    而不是 ``883239//3``。
    """
    out = []
    cur = None
    for line in ini_text.splitlines():
        m = MESH_COMMENT_RE.match(line.strip())
        if m:
            cur = m.group("name")
            continue
        d = DRAW_INDEXED_RE.match(line)
        if d:
            # group(1)=count, group(2)=start, group(3)=baseVertex
            out.append((cur, int(d.group(2)), int(d.group(1)), int(d.group(3))))
    return out


#: 导出时给物体名追加的后缀。实测 SSMT 导出会把 ``Body 菲欧妮`` 写成
#: ``Body 菲欧妮_copy``（`菲欧妮.ini:839` 的 ``; [mesh:LOD0.9040e2c4-41253-0.Body 菲欧妮_copy]``），
#: 而 Blender 里那个物体还叫原名。`.001` / `.002` 是 Blender 自己的重名后缀。
_EXPORT_SUFFIX_RE = re.compile(r"(?:_copy\d*|\.\d{3,})$", re.IGNORECASE)


def _strip_export_suffix(name):
    """剥掉导出/重名后缀，直到稳定。

    为什么循环：Blender 可能给出 ``xxx_copy.001`` 这种叠加后缀。
    """
    cur = name.strip().lower()
    while True:
        nxt = _EXPORT_SUFFIX_RE.sub("", cur).rstrip()
        if nxt == cur or not nxt:
            return cur if not nxt else nxt
        cur = nxt


def mesh_names_match(host_name, blender_name):
    """宿主 ini 里的 mesh 名与 Blender 物体名是否指同一个东西。

    导出常给物体名加后缀（``_copy``、``.001``），也会加前缀（``SSMT_``、角色名），
    所以先剥后缀、再允许前后缀互含。两边转小写比较——Blender 物体名大小写不稳定，
    而 ini 注释是导出时写的。

    为什么不用原来的 ``endswith`` 互含：``_copy`` 挂在末尾会把后缀匹配整个打断，
    ``'...-56844_copy'.endswith('...-56844')`` 是 False，于是明明同名却匹配不上、
    ``resolve_first_index`` 返回 ``None``、注入块一个都写不出去。
    """
    if not host_name or not blender_name:
        return False
    a = _strip_export_suffix(host_name)
    b = _strip_export_suffix(blender_name)
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith(b) or b.endswith(a)


def resolve_first_index(poly_index, object_name, first_index, ib_file, mesh_draws):
    """算出要写进 ``y243`` 的首索引；返回 ``None`` 表示算不出来。

    优先用「物体 + 面序号」反算：``首索引 = 该物体 draw 起始索引 + 面序号×3``。
    这样用户在 Blender 里点一个面就够了，不需要知道游戏 IB 的绝对偏移。
    只有面序号为负（未指定）时才退回手填的 ``first_index``。

    算不出来时返回 ``None`` 而不是硬写一个号：写错号不会报错，
    只会让那一股水从别的三角形喷出来，比不喷更难查。
    """
    poly = int(poly_index)
    if poly >= 0 and mesh_draws:
        ib_lower = (ib_file or "").lower()
        cands = [d for d in mesh_draws if mesh_names_match(d[0], object_name)]
        if not cands and ib_lower:
            cands = [d for d in mesh_draws if d[0] and ib_lower in d[0].lower()]
        for _mesh, start, count, _base in cands:
            fi = start + poly * 3
            if start <= fi < start + count:
                return fi
        return None
    fi = int(first_index)
    return fi if fi >= 0 else None

#: tint 的上限。参考实现把纯白 `#ffffff` 写成 0.98 而不是 1.0
#: （`Profiles/62c0dcc05f11.json` gpu_data 第 4、5 行的 18..20 列是 0.98/0.98/0.98），
#: 而 `#e6bf08` 连着三行都是精确的 230/255、191/255、8/255（0.901961 / 0.749020 / 0.031373）。
#: ⇒ 不是统一乘 0.98，而是封顶在 0.98。纯白在液体里会把高光糊成一片，这是有意压的。
TINT_MAX = 0.98


def clamp_tint(rgb):
    """把 tint 三分量钳到 [0, TINT_MAX]。

    数据来源同上：`#e6bf08` 原样落盘（/255），`#ffffff` 被压到 0.98。
    """
    out = []
    for c in (rgb[0], rgb[1], rgb[2]):
        c = float(c)
        if c != c:        # NaN
            c = 0.0
        out.append(max(0.0, min(TINT_MAX, c)))
    return tuple(out)


def emitter_direction(tilt_deg, yaw_deg, roll_deg):
    """发射器方向 -> 三角形 TBN 下的 ``(forward, up)`` 单位系数。

    消费端 ``surface\\ResolveSurface.hlsl:61-64``：

        forward = normalize(tangent*f.x + bitangent*f.y + n*f.z)
        upHint  = normalize(tangent*u.x + bitangent*u.y + n*u.z)
        right   = normalize(cross(upHint, forward))
        up      = normalize(cross(forward, right))

    所以 ``f`` / ``u`` 是**三角形切空间里的系数**，不是模型空间向量；
    而且 ``u`` 必须在写进去之前就与 ``f`` 正交——否则 ``normalize`` 会把
    ``upHint`` 拉回 forward 方向，``cross(upHint, forward)`` 退化成零向量。

    参数
    ----
    tilt_deg : 铰链角。0 = 沿三角形法线喷出；**正值朝表面「上」摆，负值朝下摆**，
               ±90 = 贴表面朝上/朝下吹。
    yaw_deg  : 绕法线的方位角。0 = 该铰链平面；±180 扫过一整圈。
    roll_deg : 绕 forward 旋转 up hint 的基准。

    基准取 ``(0,1,0)``（= bitangent）：``tilt=0`` 且 ``roll=0`` 时
    ``forward=(0,0,1)``、``up=(0,1,0)``，经 ResolveSurface 得
    ``right=cross(bitangent,n)=tangent``、``up=cross(n,tangent)=bitangent``，
    即「上」贴住表面向上。参考实现的 7 条实测里 ``v[9..11]`` 全部非零，
    说明这个自由度确实被用到了；``roll`` 就是补它的那一个。
    """
    import math

    t = math.radians(float(tilt_deg))
    y = math.radians(float(yaw_deg))
    r = math.radians(float(roll_deg))

    # 铰链式：tilt 是从法线朝「表面向上」摆的正负角，yaw 是绕法线的方位。
    # 写成 ``(sin t cos y, sin t sin y, cos t)`` 的极角形式时，tilt 取负只
    # 把平面内方向翻转 180°，看上去像「符号没生效」——所以这里用
    # ``n*cos(t) + up*sin(t)`` 再绕 n 转 y 的写法，使 +tilt 朝上、-tilt 朝下。
    st, ct = math.sin(t), math.cos(t)
    fwd = (-st * math.sin(y), st * math.cos(y), ct)

    def orthonormal_base(f):
        for cand in ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)):
            d = f[0] * cand[0] + f[1] * cand[1] + f[2] * cand[2]
            a = (cand[0] - d * f[0], cand[1] - d * f[1], cand[2] - d * f[2])
            ln = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
            if ln > 1e-6:
                return (a[0] / ln, a[1] / ln, a[2] / ln)
        return (1.0, 0.0, 0.0)  # 理论上到不了这里

    a = orthonormal_base(fwd)
    b = (fwd[1] * a[2] - fwd[2] * a[1],
         fwd[2] * a[0] - fwd[0] * a[2],
         fwd[0] * a[1] - fwd[1] * a[0])
    cr, sr = math.cos(r), math.sin(r)
    up = (a[0] * cr + b[0] * sr, a[1] * cr + b[1] * sr, a[2] * cr + b[2] * sr)
    return fwd, up


# --------------------------------------------------------------------------
# v[8] / v[12] 的编码（发射模式与 trick 参数）
#
# 唯一权威是消费端两个文件，node 侧只是把它们的判据镜像成纯函数：
#
#   Slots.hlsl:33-36
#     slotVisOn(s)        = slots[..+SLOT_TRICKLE].w >  .5
#     slotTrickle(s)      = slots[..+SLOT_TRICKLE].x >  .05
#     slotTrickleScale(s) = clamp(slots[..+SLOT_TRICKLE].x, .1, 8)
#
#   AssignSlots.hlsl:83
#     x = profiles[row+1].w > 1.05 ? min(profiles[row+1].w - 1, 8) : 0
#     y = floor(fmod(profiles[row+2].w, 10000) / 100) * .05    <- bendDelay
#     z = fmod(profiles[row+2].w, 100) * .4                     <- bendStr
#     w = profiles[row+1].w < -.5 ? 1 : 0                       <- vis
# --------------------------------------------------------------------------

#: ``slotTrickleScale`` 的 clamp 区间，写进去的值必须落在这里面。
TRICK_SPEED_MIN = 0.1
TRICK_SPEED_MAX = 8.0

#: ``v[8] > 1.05`` 才被认成 trick（AssignSlots:83 的判据），所以下限写 0.1
#: 而不是任意小数——0.01 会算出 v8=1.01，`slotTrickle` 判定 `x > .05` 直接失败，
#: trick 模式静默退化成普通模式，游戏里表现是「勾了 trick 但没反应」。
#: AssignSlots:83 的判据是 ``v8 < -.5``（**严格小于**），所以边界值 -0.5 不算
#: vis；参考实现写的是 -1。节点也用 -1.0，避免踩在边界上。
_VIS_CODE = -1.0
_VIS_THRESHOLD = -0.5
_TRICK_THRESHOLD = 1.05


def emitter_mode_code(trick, vis, trick_sp=TRICK_SPEED_MIN):
    """发射模式 -> ``v[8]``。

    三态（互斥，vis 优先于 trick）::

        vis         -> -1.0                  slotVisOn 真，部件被画时就喷
        trick       -> 1.0 + clamp(sp, .1, 8) slotTrickle 真，速度倍率 = v8-1
        否则        -> 0.0                   普通模式（NUMPAD1 + 脉冲态）

    ``trick_sp`` 被夹到 [0.1, 8]：消费端 ``slotTrickleScale`` 就是 clamp(x, .1, 8)，
    而 ``slotTrickle`` 的判据是 ``x > .05`` —— 写 0.01 会让 trick 静默失效。
    """
    if vis:
        return _VIS_CODE
    if not trick:
        return 0.0
    sp = float(trick_sp)
    if sp != sp:            # NaN
        sp = TRICK_SPEED_MIN
    sp = max(TRICK_SPEED_MIN, min(TRICK_SPEED_MAX, sp))
    return 1.0 + sp


def emitter_pack_code(trick, vis, tlen, bend_delay, bend_str):
    """tlen / bendDelay / bendStr -> ``v[12]``，**只在 trick 模式非零**。

    打包公式（``AssignSlots:83`` 的逆运算）::

        a = round(tlen      / 0.05)   高两位  (0..99)
        b = round(bendDelay / 0.05)   中两位  (0..99)
        c = round(bendStr)            低两位  (0..99)
        v[12] = a * 10000 + b * 100 + c

    为什么非 trick 一定要写 0：参考实现里 ``trick=False`` 的四条
    （``waterpick/62c0dcc05f11.json`` 的 #0..#3、#6）tlen/bendDelay/bendStr 分别是
    0.35/0.15/15，**三个数都不为 0，但落盘 v[12] 全是 0**。
    原因在消费端：``FluidSolver.hlsl:266-272`` 只在 ``if(trickle && ...)`` 里读
    y/z 分量，非 trick 发射器这三个数根本没人看。写 0 是与参考实现逐位一致的
    唯一选择（写别的值不报错，但会让产物和参考实现对不上）。
    """
    if vis or not trick:
        return 0.0

    def two(v):
        v = float(v)
        if v != v:          # NaN
            return 0
        return max(0, min(99, int(round(v))))

    a = two(float(tlen) / 0.05 if tlen == tlen else 0.0)
    b = two(float(bend_delay) / 0.05 if bend_delay == bend_delay else 0.0)
    c = two(bend_str)
    return float(a * 10000 + b * 100 + c)


#: 旧版 ``profiles.buf`` 一条记录的字节数（12 个 float32）。
#: 来源：旧版 ``-zh-CN`` 目录里 ``profiles.buf`` 1056 B = 22 条 × 48 B。
LEGACY_PROFILE_RECORD_BYTES = 48

#: 旧版 ``attachment-offsets.buf`` 一条记录的字节数（4 个 float32）。
#: 来源：同目录 ``attachment-offsets.buf`` 352 B = 22 条 × 16 B。
LEGACY_OFFSET_RECORD_BYTES = 16


def legacy_profile_record(gpu):
    """把一条 23 浮点记录压成旧版 ``profiles.buf`` 的 12 个 float。

    旧版编辑器（``AttachmentEditor/server.py::apply_anchor``）写的是::

        struct.pack('<12f', priority, *bary, *f, 0., *u, 0.)

    而新版 ``LoadProfilesJSON.hlsl:59-61`` 把同样的东西拆进三行 float4，
    只是顺序不同（先 forward+trick，再 up+pack）。这里的映射::

        [0]     <- v[1]       priority
        [1..3]  <- v[2..4]    bary
        [4..6]  <- v[5..7]    forward
        [7]     <- 0
        [8..10] <- v[9..11]   up
        [11]    <- 0

    ``v[8]``（trick）与 ``v[12]``（pack）是旧版没有的字段，丢弃。
    """
    g = [float(x) for x in gpu]
    if len(g) < 23:
        raise ValueError("gpu 记录需要 23 个浮点，实际 %d" % len(g))
    return (
        g[1], g[2], g[3], g[4],
        g[5], g[6], g[7], 0.0,
        g[9], g[10], g[11], 0.0,
    )


def legacy_offset_record(gpu):
    """把一条 23 浮点记录压成旧版 ``attachment-offsets.buf`` 的 4 个 float。

    旧版写的是 ``struct.pack('<4f', *local_offset, 0)``；对应新版 ``v[13..15]``
    （``LoadProfilesJSON.hlsl:64`` 的 ``offsets[id] = float4(v[13],v[14],v[15],0)``）。
    """
    g = [float(x) for x in gpu]
    if len(g) < 23:
        raise ValueError("gpu 记录需要 23 个浮点，实际 %d" % len(g))
    return (g[13], g[14], g[15], 0.0)


def legacy_buffers(gpu_rows):
    """生成旧版两个 .buf 的完整字节。

    返回 ``(profiles_buf_bytes, attachment_offsets_buf_bytes)``。

    **默认不该用**——新版前置改由 ``LoadProfilesJSON.hlsl`` 直读 JSON，
    这两个文件在 ``Probe.ini`` 里零引用。只有还在用旧版 ``-zh-CN`` 前置、
    或第三方工具链着它们时才需要。
    """
    import struct

    prof = bytearray()
    offs = bytearray()
    for gpu in gpu_rows:
        prof += struct.pack("<12f", *legacy_profile_record(gpu))
        offs += struct.pack("<4f", *legacy_offset_record(gpu))
    return bytes(prof), bytes(offs)

# --- 多面聚合 ---------------------------------------------------------------
#
# 为什么需要：运行期一次只抓一个三角形（`CaptureTriangle.hlsl` 用
# `drawindexedinstanced = 3,1,y243,z243` 发一次 3 顶点的 draw，写 3x4 像素），
# 所以「选 4 个面当一整个面用」不能真的让运行期抓 4 个三角形。
#
# 这里做的是：把 4 个面按面积加权合成一个等效面（中心 + 法线），
# 然后挑**离等效中心最近**的那个三角形去抓，位置/方向的差用重心坐标
# 与局部偏移补回来。外观上与「一整个面」等价，代价是中心处的曲率抓不到。


def face_cluster_stats(verts, tris, targets):
    """把一簇三角形合成一个等效面：面积加权中心 + 面积加权法线。

    ``verts``   顶点世界坐标 ``[(x, y, z), ...]``
    ``tris``    三角形顶点索引 ``[(i0, i1, i2), ...]``
    ``targets`` 要聚合的三角形序号列表

    返回 ``(center, normal, total_area)``；全退化时 ``center`` 为 ``None``。

    按面积加权的理由：几个面积差很多的面直接平均中心会被小面拖偏，法线同理。
    """
    import math

    cx = cy = cz = 0.0
    nx = ny = nz = 0.0
    total = 0.0
    for t in targets:
        if t < 0 or t >= len(tris):
            continue
        i0, i1, i2 = tris[t]
        ax, ay, az = verts[i0]
        bx, by, bz = verts[i1]
        dx, dy, dz = verts[i2]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = dx - ax, dy - ay, dz - az
        nx0 = uy * vz - uz * vy
        ny0 = uz * vx - ux * vz
        nz0 = ux * vy - uy * vx
        area = 0.5 * math.sqrt(nx0 * nx0 + ny0 * ny0 + nz0 * nz0)
        if area <= 1e-12:
            continue
        total += area
        cx += (ax + bx + dx) / 3.0 * area
        cy += (ay + by + dy) / 3.0 * area
        cz += (az + bz + dz) / 3.0 * area
        nx += nx0
        ny += ny0
        nz += nz0
    if total <= 1e-12:
        return None, None, 0.0
    center = (cx / total, cy / total, cz / total)
    ln = math.sqrt(nx * nx + ny * ny + nz * nz)
    if ln <= 1e-12:
        return center, None, total
    return center, (nx / ln, ny / ln, nz / ln), total


def triangle_barycentric(p, a, b, c):
    """点 ``p`` 在三角形 ``(a,b,c)`` 内的重心坐标，**不 clamp**。

    用面积比：``u = area(pbc)/area(abc)`` 等。越界（点不在三角形内）时
    分量会变成负数或大于 1——调用方必须自己 clamp 并重新归一。
    """
    import math

    def sub(x, y):
        return (x[0] - y[0], x[1] - y[1], x[2] - y[2])

    def cross(u, v):
        return (u[1] * v[2] - u[2] * v[1],
                u[2] * v[0] - u[0] * v[2],
                u[0] * v[1] - u[1] * v[0])

    def area2(u, v):
        return math.sqrt(sum(x * x for x in cross(u, v)))

    total = area2(sub(b, a), sub(c, a))
    if total <= 1e-14:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    u = area2(sub(b, p), sub(c, p)) / total
    v = area2(sub(p, a), sub(c, a)) / total
    w = 1.0 - u - v
    return (u, v, w)


def clamp_barycentric(bary):
    """把重心坐标夹到 [0,1] 并重新归一到和为 1。

    ``LoadProfilesJSON.hlsl:59-64`` 会校验 ``abs(alpha+beta+gamma-1) <= .001``，
    不满足整份 profile 被丢弃——所以这里必须保证和为 1，不能只夹不归一。
    """
    u, v, w = (max(0.0, float(x)) for x in bary)
    s = u + v + w
    if s <= 1e-12:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    return (u / s, v / s, w / s)


def nearest_triangle(verts, tris, targets, center):
    """挑中心离 ``center`` 最近的那个三角形，返回 ``(triangle_index, distance)``。

    运行期只能抓一个三角形，所以「等效面」最终要落到某一个真实三角形上。
    """
    import math

    best = (-1, float("inf"))
    for t in targets:
        if t < 0 or t >= len(tris):
            continue
        i0, i1, i2 = tris[t]
        ax, ay, az = verts[i0]
        bx, by, bz = verts[i1]
        dx, dy, dz = verts[i2]
        gx = (ax + bx + dx) / 3.0 - center[0]
        gy = (ay + by + dy) / 3.0 - center[1]
        gz = (az + bz + dz) / 3.0 - center[2]
        d = math.sqrt(gx * gx + gy * gy + gz * gz)
        if d < best[1]:
            best = (t, d)
    return best


#: SSMT「非镜像工作流」在对象上的标记。导入时对 mesh 做 X 镜像（顶点翻、物体矩阵不动），
#: 导出时再翻回去 —— 于是 Blender 里是镜像的、游戏里是原样的。
NON_MIRROR_MARKER = "_ssmt_non_mirror_workflow_processed"


def needs_x_mirror(obj_keys):
    """对象的自定义属性里有没有非镜像工作流标记。"""
    return any(NON_MIRROR_MARKER in str(k) for k in obj_keys)


def game_triangle_order(mirrored):
    """游戏侧三角形的顶点顺序。

    不镜像： (0, 1, 2) —— 游戏里和 Blender 里是同一个三角形同一个顺序。
    镜像：   (0, 2, 1) —— X 翻转把绕序反过来，导出时再翻回去只改坐标不改顶点顺序，
             所以 Blender 的 (a, b, c) 在游戏里是 (a, M*c, M*b)。

    实测（菲欧妮.ini 的 LOD0.9040e2c4-16194-56844 / 面 12516）：

        Blender  poly.vertices -> v0=(0,-0.060273,0.845665)
                                  v1=(-0,-0.050626,0.831562)
                                  v2=(+0.008466,-0.050356,0.831329)
        IB[37548..37550]      -> (9947, 9930, 9932)
            idx 9947 -> (0,         -0.060273, 0.845665) = v0
            idx 9930 -> (-0.008466, -0.050356, 0.831329) = M*v2
            idx 9932 -> (0,         -0.050626, 0.831562) = M*v1

    同一组顶点，顺序变成 (v0, M*v2, M*v1)，重心 x 从 +0.002822 变成 -0.002822。

    这个顺序决定了重心坐标要跟着换：配方 (u, v, w) 对应 (a, b, c)，
    游戏里那颗三角形的第二、三顶点是 M*c 与 M*b，所以要把 v 和 w 对调，
    否则重心会落在 M*(b-c)*(v-w) 的偏差上。
    """
    return (0, 2, 1) if mirrored else (0, 1, 2)


def collect_gate_hashes(ini_text):
    """收集 ``[TextureOverride*]`` 段内的 ``hash =``，只留**出现次数最多**的那些。

    为什么不收全文：``[ShaderOverride*]`` 里的 hash 是 64 位**着色器** hash，
    而 ``TextureOverride`` 的 ``hash`` 是 VB/IB/Texture 的 CRC。两者值域不同，
    把着色器 hash 写进门控段等于写一个永远匹配不上的 hash——那段不报错，
    只是永不触发，门控于是恒为 0。所以按段过滤，不靠位宽。

    为什么不返回全部：一个角色有几十个部件、每个部件一个 IB hash，全贴上门控段
    既让产物臃肿（``菲欧妮`` 从 1 段涨到 15 段），也没有意义——主体部件被画时
    角色就在场，主体部件没被画时角色基本也不显示。同一个 IB hash 会出现在多个
    ``[TextureOverride_*]`` 段里（LOD0 / shadow / rim 各一段），出现最多的那个
    就是每帧被画最多次的主体 mesh。

    并列第一的全要：数量相同说明它们同为主体，缺一个就可能漏门控。

    实测 ``菲欧妮.ini``：``9040e2c4`` 出现 5 次（最多）、``ad90e710`` 3 次、
    ``b11ff137`` 2 次 ⇒ 返回 ``['9040e2c4']``。
    """
    counts = {}
    order = []
    in_tex_override = False
    for line in ini_text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_tex_override = s[1:-1].lower().startswith("textureoverride")
            continue
        if not in_tex_override:
            continue
        m = re.match(r"hash\s*=\s*([0-9a-fA-F]+)\s*$", s, re.IGNORECASE)
        if m:
            h = m.group(1).lower()
            if h not in counts:
                counts[h] = 0
                order.append(h)
            counts[h] += 1
    if not counts:
        return []
    top = max(counts.values())
    return [h for h in order if counts[h] == top]


# ------------------------------------------------------------------------------
# 力场（ForceField.hlsl）
# ------------------------------------------------------------------------------

#: ``[ResourceFluidForceField]`` 的容量：``array = 96``。
#: ``ForceField.hlsl:47`` 按 ``row = (slot * FORCE_FIELDS_PER_SLOT + k) * 3`` 取行，
#: 于是 96 行 = 8 槽 × 4 字段 × 3 行。8 与 ``fluid/ParticleConfig.hlsl`` 的
#: ``SLOT_COUNT`` 是同一个数，而槽号就是发射器序号（节点里 ``x243 = idx``）。
FORCE_FIELD_SLOT_COUNT = 8

#: 每个槽位最多几个力场。改这里必须同时改 ``ForceField.hlsl:20``。
FORCE_FIELDS_PER_SLOT = 4

#: 缓冲行数。
FORCE_FIELD_ROWS = FORCE_FIELD_SLOT_COUNT * FORCE_FIELDS_PER_SLOT * 3

#: 力场类型编码，与 ``ForceField.hlsl:21-23`` 的 ``FF_*`` 一一对应。
FORCE_FIELD_KINDS = {"DIRECTIONAL": 0, "RADIAL": 1, "VORTEX": 2}

#: 力场段的段名，用来定位 ``data`` 行插在哪。
FORCE_FIELD_SECTION = "[ResourceFluidForceField]"


def _fmt_float(value):
    """``data =`` 只认十进制小数。

    3Dmigoto 解析 ``data =`` 用的是普通浮点扫描，而参考实现那一行全是
    ``0.8600000143051147`` 这类原始展开，一次都没出现过 ``1e-05``。
    所以这里也展开成定点写法，避免「值写进去了但读出来是 0」这种静默失效。

    整数写成 ``1`` 而不是 ``1.0``：``global $force_scale = 1`` 是模板原文，
    回退到「没有力场」时必须逐字节还原，否则幂等测试与 diff 都会出现假阳性。
    """
    text = "{:.9f}".format(float(value)).rstrip("0")
    if text.endswith("."):
        text = text[:-1]
    return text


def build_force_field_floats(fields_by_slot):
    """把 ``{槽号: [字段字典, ...]}`` 摊平成 ``ForceField.hlsl`` 要的 384 个 float。

    每个字段三行：``(offset, type)`` / ``(axis, radius)`` / ``(strength, falloff, 0, enabled)``。
    布局必须与 ``ForceField.hlsl:49-53`` 逐字对齐，否则表现是「参数有值但力是零」。
    超出 ``FORCE_FIELDS_PER_SLOT`` 的字段、超出 ``FORCE_FIELD_SLOT_COUNT`` 的槽位在这里
    直接丢弃，由调用方给警告。
    """
    out = [0.0] * (FORCE_FIELD_ROWS * 4)
    for slot, fields in (fields_by_slot or {}).items():
        slot = int(slot)
        if slot < 0 or slot >= FORCE_FIELD_SLOT_COUNT:
            continue
        for k, field in enumerate(list(fields)[:FORCE_FIELDS_PER_SLOT]):
            base = ((slot * FORCE_FIELDS_PER_SLOT) + k) * 3 * 4
            # X 镜像工作流（NEEDS_X_MIRROR 标记）下，游戏侧的 right = -M(Blender right)：
            # 镜像翻转手性，而 ResolveSurface.hlsl:63-64 的 right=up×forward 必须保持右手，
            # 所以 right 是唯一整体翻号的那根轴（up/forward 都等于 Blender 侧的镜像
            # M(up)/M(forward)）。U 是沿 right 的分量 ⇒ 同一面板系数在游戏里方向相反。
            # 只翻 U，V/W 不动；判据与顶点顺序同一处（_game_frame 的 mirrored）。
            mirror_u = -1.0 if field.get("mirror_u", False) else 1.0
            out[base + 0] = mirror_u * float(field.get("offset_u", 0.0))
            out[base + 1] = float(field.get("offset_v", 0.0))
            out[base + 2] = float(field.get("offset_w", 0.0))
            out[base + 3] = float(
                FORCE_FIELD_KINDS.get(field.get("kind", "DIRECTIONAL"), 0)
            )
            out[base + 4] = mirror_u * float(field.get("axis_u", 0.0))
            out[base + 5] = float(field.get("axis_v", 0.0))
            out[base + 6] = float(field.get("axis_w", 1.0))
            out[base + 7] = float(field.get("radius", 0.15))
            out[base + 8] = float(field.get("strength", 0.0))
            out[base + 9] = float(field.get("falloff", 2.0))
            out[base + 11] = 1.0 if field.get("enabled", True) else 0.0
    return out


def force_fields_enabled(floats):
    """任意一个字段的 enabled 位为真（每字段第三行的第四个分量）。"""
    return any(floats[i] > 0.5 for i in range(11, len(floats), 12))


def _section_bounds(lines, name):
    """返回段 ``name`` 在 ``lines`` 里的 ``[start, end)``，找不到返回 ``None``。"""
    stripped = [line.strip() for line in lines]
    try:
        start = stripped.index(name)
    except ValueError:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        text = lines[i].strip()
        if text.startswith("[") and text.endswith("]"):
            end = i
            break
    return start, end


def apply_force_fields(text, floats, gain=1.0):
    """把力场数据写进 ``[ResourceFluidForceField]`` 并拨 ``$force_enabled``。

    没有启用的字段时**删掉** ``data`` 行、把开关归零：这样「不加力场」的导出
    与加力场之前逐字节一致（注入幂等测试依赖这个性质）。
    """
    enabled = force_fields_enabled(floats)
    lines = text.split("\n")
    span = _section_bounds(lines, FORCE_FIELD_SECTION)
    if span is None:
        return text
    start, end = span

    kept = [
        line
        for line in lines[start:end]
        if not re.match(r"\s*data\s*=", line, re.IGNORECASE)
    ]

    if enabled:
        data_line = "data = " + " ".join(_fmt_float(value) for value in floats)
        at = len(kept)
        for i, line in enumerate(kept):
            if re.match(r"\s*array\s*=\s*%d\s*$" % FORCE_FIELD_ROWS, line, re.I):
                at = i + 1
                break
        kept.insert(at, data_line)

    lines[start:end] = kept
    text = "\n".join(lines)

    text = re.sub(
        r"global\s+\$((?:%s)?force_enabled)\s*=\s*[^\s;]+" % VARIABLE_PREFIX,
        lambda m: "global $%s = %d" % (m.group(1), 1 if enabled else 0),
        text,
    )
    text = re.sub(
        r"global\s+\$((?:%s)?force_scale)\s*=\s*[^\s;]+" % VARIABLE_PREFIX,
        lambda m: "global $%s = %s" % (m.group(1), _fmt_float(gain)),
        text,
    )
    return text


def apply_particle_radius(text, radius):
    """把粒子半径写进 ``$fluid_particle_radius``（``IniParams[44].x``）。

    ``ParticleConfig.hlsl`` 的 ``fluidRadius()`` 读它，0 表示沿用编译期默认值
    0.0028。这样面板里的「粒子半径」就是游戏里真正在用的那个数，不再是只
    影响 Blender 预览的独立旋钮。
    """
    value = float(radius)
    if value < 0.0:
        value = 0.0
    return re.sub(
        r"global\s+\$((?:%s)?fluid_particle_radius)\s*=\s*[^\s;]+" % VARIABLE_PREFIX,
        lambda m: "global $%s = %s" % (m.group(1), _fmt_float(value)),
        text,
    )



# ---------------------------------------------------------------------------
# 每个发射器一个开关快捷键
# ---------------------------------------------------------------------------
#: 发射器门控写进 ``IniParams[42]`` / ``[43]``，见 ``Slots.hlsl`` 的 slotEmitOn。
EMITTER_KEY_MAX = 8

#: 发射器数量上限 = ``fluid/ParticleConfig.hlsl`` 的 ``SLOT_COUNT=8``，与
#: ``EMITTER_KEY_MAX`` 同值：第 9 个发射器既没有槽位、也没有 ``$emit_8`` 可拨，
#: 加进面板只会多一条什么都不写的条目，所以算子和按钮直接拦住。
EMITTER_CAPACITY = 8
EMITTER_KEY_MARKER = "; --- FLUID EMITTER HOTKEYS BEGIN ---"

_EMIT_KEY_SECTION = re.compile(
    r"^\[KeyFluidEmit(\d+)\][ \t]*\r?\n"
    r"(?:[ \t][^\r\n]*(?:\r?\n|\Z)|[ \t]*\r?\n)*",
    re.MULTILINE,
)
_EMIT_KEY_MARKER = re.compile(
    r"^; --- FLUID EMITTER HOTKEYS BEGIN ---[ \t]*\r?\n", re.MULTILINE)


def normalise_key(spec):
    """把一个快捷键写法规范成 3Dmigoto 的 ``key =`` 值。

    只写 ``VK_F1`` 时补上 ``no_alt no_ctrl no_shift``（和模板里其它 Key 段一致）；
    已经写了修饰词的整串原样保留。空串表示这个发射器不绑键。
    """
    text = " ".join(str(spec or "").split())
    if not text:
        return ""
    if " " in text:
        return text
    return "no_alt no_ctrl no_shift " + text


def build_emitter_key_block(entries, warnings=None):
    """``[(slot, spec)]`` -> ``[KeyFluidEmitN]`` 段文本；没有绑键返回空串。

    槽号超出 ``EMITTER_KEY_MAX`` 的发射器**跳过并记一条告警**（追加进
    ``warnings``），其余发射器照常生成。不抛异常：3Dmigoto 的 Key 段按槽号
    命名、且只有 ``$emit_N`` 那几个变量可拨，第 9 个发射器没有对应变量；
    在这里硬抛会让整次导出连一个文件都不落地（异常发生在
    ``_build_main_ini`` 里，早于任何写盘），表现成「导出没反应」。
    """
    rows = []
    skipped = []
    for slot, spec in entries:
        slot_i = int(slot)
        if not 0 <= slot_i < EMITTER_KEY_MAX:
            skipped.append(slot_i)
            continue
        key = normalise_key(spec)
        if key:
            rows.append((slot_i, key))
    if skipped and warnings is not None:
        shown = ", ".join(str(s) for s in skipped[:5])
        more = "…（共 %d 个）" % len(skipped) if len(skipped) > 5 else ""
        warnings.append(
            "发射器 #%s%s 超出按键槽上限 %d，已跳过它们的按键绑定；其余发射器照常导出"
            % (shown, more, EMITTER_KEY_MAX)
        )
    if not rows:
        return ""
    out = [EMITTER_KEY_MARKER]
    for slot_i, key in sorted(rows):
        out.append("[KeyFluidEmit%d]" % slot_i)
        out.append("    key = %s" % key)
        out.append("    type = cycle")
        out.append("    $%semit_%d = 1,0" % (VARIABLE_PREFIX, slot_i))
        out.append("")
    return "\n".join(out).rstrip("\n")


def apply_emitter_keys(text, entries, warnings=None):
    """重写全部 ``[KeyFluidEmitN]`` 段。

    先无条件删掉旧段再按当前发射器列表重建 —— 删掉一个绑了键的发射器时，
    它的 Key 段必须跟着消失，否则那个键会一直去拨一个不存在的槽。

    ``warnings`` 非 ``None`` 时，槽号超限之类的跳过原因会追加进去（调用方
    负责打印），不再靠异常打断整次导出。
    """
    body = _EMIT_KEY_SECTION.sub("", text)
    body = _EMIT_KEY_MARKER.sub("", body)
    body = body.rstrip("\r\n")
    block = build_emitter_key_block(entries, warnings)
    if not block:
        return body + "\n"
    return body + "\n\n" + block + "\n"


# ---------------------------------------------------------------------------
# 每个发射器的运行期控制变量（可被动画驱动逐帧写）
# ---------------------------------------------------------------------------
#: 与 ``fluid/ParticleConfig.hlsl`` 的 ``SLOT_COUNT`` 同值：力场表与槽位都是 8 格。
EMITTER_CONTROL_MAX = 8

#: 每个控制量占两个 IniParams 号：一个 float4 装 4 个发射器（x..w = 发射器 0..3）。
#:   45 / 48 = 发射**器**速度（m/s，0 = 沿用内部兜底 ``$fluid_launch_speed``）
#:   58 / 59 = 力场**字段**开关的位掩码（bit0..3 = 该发射器的第 0..3 个力场）
#: 这 4 个号在本节点的 ini 与 ``Toolset/fluid`` 的 hlsl 里都没有别的写点/读点
#: （全树 grep：``IniParams[45|48|58|59]`` 只出现在本轮新增的 accessor 里，
#: ``^ *[xyzw](45|48|58|59) *=`` 在 Probe.ini 里为空），与已有的 41..47 同属
#: 「nhd-pick 自有号」。换号只改这张表与 ``Slots.hlsl`` / ``ForceField.hlsl``。
EMITTER_SPEED_REGS = (45, 48)
#: 力场字段级开关的位掩码：每个发射器一个 float，58 = 发射器 0-3、59 = 4-7。
#: ini 侧用 `$a + 2*$b + 4*$c + 8*$d` 拼出 0..15，shader 侧 `(mask>>k)&1`。
#: 这样一个号段就能装下 8×4=32 个开关，而变量名仍然是「一个力场一个」。
EMITTER_FIELD_REGS = (58, 59)
#: 每发射器寿命（秒）：51 = 发射器 0-3、52 = 4-7，值 0 = 沿用内部兜底 1.1
#: （`ParticleConfig.hlsl` 的 `FLUID_LIFETIME_DEFAULT`）。0 秒**不是**合法值：
#: `FluidSolver.hlsl` 拿 lifetime 做除数。
EMITTER_LIFETIME_REGS = (51, 52)

#: 「力场字段级开关这一族存在」的标记：``IniParams[41].w``（41.x 是派生的总闸
#: 「有没有力场」、41.y 是全局增益）。模板里固定有一行 ``w41 = 0``，导出每次都把它
#: **改写成 literal 0/1**（老模板没有这一行时插在 ``y41`` 之后）：只写 1、不写 0
#: 的话，``IniParams`` 跨 mod 粘滞（3Dmigoto 的变量表没有命名空间），上一份 mod
#: 留下的 ``w41=1`` 会让这份没写 58/59 的导出把力场全判 OFF，而且不报错。
#: 0 = 每个字段都按自己导出的 enabled 位走（旧行为）。
def force_field_switchable(field_switches):
    """任一发射器（前 :data:`EMITTER_CONTROL_MAX` 个）有字段被关 ⇒ 掩码会被读。

    与 :func:`build_emitter_control` 的 ``want_field`` 同一口径（同样截到 8 个
    发射器、每个只看前 :data:`FORCE_FIELDS_PER_SLOT` 个），保证「写了 1」与
    「写了掩码」永远成对出现。
    """
    rows = list(field_switches or ())[:EMITTER_CONTROL_MAX]
    return any(
        any(int(v) != 1 for v in list(row or ())[:FORCE_FIELDS_PER_SLOT])
        for row in rows
    )

#: 插入块的哨兵。已经在文本里就说明这一段插过了（``_build_main_ini`` 每次导出
#: 只调一次，这里只是防「有人再调一遍」时插出两份）。
EMITTER_CONTROL_MARKER = "; --- FLUID EMITTER CONTROLS BEGIN ---"

#: 插入/改写锚点。**两种写法都要认**：模板现在的原文已经是带前缀的
#: ``global $fluid_emit_7 = 1`` / ``w43 = $fluid_emit_7``（Probe.ini:964-971 /
#: :205-212），但这一层在 ``prefix_variables`` 之前跑，别人也可能塞进一份不带
#: 前缀的模板，所以 ``(?:fluid_)?`` 两边都吃。找不到就告警跳过，绝不静默改结构。
_EMITTER_GLOBALS_ANCHOR = re.compile(
    r"(?m)^global[ \t]+(?:persist[ \t]+)?\$(?:fluid_)?emit_7[ \t]*="
    r"[^\r\n]*$")
_EMITTER_WRITES_ANCHOR = re.compile(
    r"(?m)^[ \t]*w43[ \t]*=[ \t]*\$(?:fluid_)?emit_7[ \t]*\r?$")
#: ``w41`` 那一行：有就改写成 literal 值（保留缩进），没有就插在 ``y41`` 之后。
_FORCE_FLAG_LINE = re.compile(r"(?m)^([ \t]*)w41[ \t]*=[^\r\n]*")
_FORCE_FLAG_ANCHOR = re.compile(
    r"(?m)^[ \t]*y41[ \t]*=[ \t]*\$(?:fluid_)?force_scale[ \t]*\r?$")
#: 每发射器开关的声明行：`global [persist] $[fluid_]emit_N = <值>`。
#: 导出时把存在的发射器改成 `global …`（值 = 面板），其余号不动。**不用 persist**：它的跨启动记忆会盖掉面板值。
_EMIT_PERSIST_RE = re.compile(
    r"(?m)^([ \t]*)global[ \t]+(?:persist[ \t]+)?\$(?:fluid_)?emit_(\d+)"
    r"[ \t]*=[^\r\n]*$")
#: 初始化块末尾那一行（开场赋一次面板值插在它前面）。
_SEED_ANCHOR = re.compile(r"(?m)^([ \t]*)\$initialized[ \t]*=[ \t]*1[ \t]*\r?$")


def _control_lane(regs, index):
    """``(45, 48), 5`` → ``"y48"``：第 ``index`` 个发射器落在哪个号段的哪一位。"""
    reg = regs[0] if index < 4 else regs[1]
    return "%s%d" % ("xyzw"[index % 4], reg)


def _insert_after_match(text, match, new_lines):
    """在 ``match`` 那一行之后插入若干行，缩进沿用那一行自己的缩进。"""
    head = match.group(0)
    indent = head[:len(head) - len(head.lstrip())]
    insert = "".join("\n" + (indent + line if line else "") for line in new_lines)
    return text[:match.end()] + insert + text[match.end():]


def build_emitter_control(speeds, field_switches=None, lifetimes=None):
    """``(每发射器速度, 每个力场自己的开关, 每发射器寿命)`` → 四段文本。

    返回 ``(global_lines, write_lines, flag_lines, warnings)``：

    * ``global_lines`` —— ``[Constants]`` 里的 ``global $fluid_emitter_speed_N`` /
      ``global $fluid_force_enable_E_K``；
    * ``write_lines``  —— ``[CommandListDrawWorld]`` 里把变量抄进 IniParams 的行
      （``x45 = $fluid_emitter_speed_0``、``x58 = $fluid_force_enable_0_0 + 2*…``）；
    * ``flag_lines``   —— 已弃用，恒为空段（族标记改由 :func:`apply_emitter_controls`
      无条件写 ``global $fluid_force_fields_switchable``，见那里的注释）；
    * ``warnings``     —— 发射器数超过 8 时的告警。

    **一个功能一个变量**：力场没有「发射器级」和「倍率」两套变量了 —— 面板上那个
    「启用」勾选框就是 :data:`fluid_ini_merge` 这一族唯一的开关，它同时决定
    导出数据行里的 enabled 位与这里的 ``$fluid_force_enable_E_K``。强度只有
    力场自己的 ``strength``（绝对值 m/s²），不再有倍率。

    **缺省不写**：speed 全 0、开关全 1 时 ``global_lines`` / ``write_lines`` /
    ``flag_lines`` 都是空（速度与掩码这两族不出现）；唯一总会写的是族标记
    ``$fluid_force_fields_switchable``，由 :func:`apply_emitter_controls` 负责，
    缺省写 0——那是「不粘滞」的代价，不是遗漏。
    """
    speeds = list(speeds or ())
    field_rows = list(field_switches or ())
    lifetimes = list(lifetimes or ())
    count = max(len(speeds), len(field_rows), len(lifetimes))
    warnings = []
    if count > EMITTER_CONTROL_MAX:
        warnings.append(
            "共 %d 个发射器，超过每发射器变量的容量 %d：第 %d 个起无法单独控制，"
            "只能跟着全局走（着色器槽位也只有 %d 个）"
            % (count, EMITTER_CONTROL_MAX, EMITTER_CONTROL_MAX + 1, EMITTER_CONTROL_MAX))
        count = EMITTER_CONTROL_MAX
    speeds = (speeds + [0.0] * count)[:count]
    field_rows = (field_rows + [None] * count)[:count]
    lifetimes = (lifetimes + [0.0] * count)[:count]

    # nhd-pick: 寿命的「不填」有两个等价写法 —— <=0，或者面板默认的 1.1。
    # 面板的默认值就是 1.1（主人口径：不填就是原来的行为，不要「默认 3 秒」这种语义），
    # 所以 1.1 绝不能写进产物；统一归一成 0（shader 的 0 → 回落 1.1）。
    lifetimes = [0.0 if (float(v) <= 0.0 or abs(float(v) - 1.1) <= 1e-6)
                 else float(v) for v in lifetimes]
    want_speed = any(float(v) > 0.0 for v in speeds)
    want_life = any(float(v) > 0.0 for v in lifetimes)
    want_field = force_field_switchable(field_rows)

    global_lines, write_lines, flag_lines = [], [], []
    if want_speed:
        for i, value in enumerate(speeds):
            global_lines.append(
                "; 发射器 %d 发射速度 (m/s)，0 = 沿用内部兜底 $fluid_launch_speed"
                " → $fluid_emitter_speed_%d" % (i, i))
            global_lines.append(
                "global $fluid_emitter_speed_%d = %s" % (i, _fmt_float(float(value))))
            write_lines.append(
                "%s = $fluid_emitter_speed_%d" % (_control_lane(EMITTER_SPEED_REGS, i), i))
    if want_life:
        for i, value in enumerate(lifetimes):
            global_lines.append(
                "; 发射器 %d 生命周期 (s)，0 = 沿用内部兜底 1.1（不填 / 填 1.1 都不写）"
                " → $fluid_emitter_lifetime_%d" % (i, i))
            global_lines.append(
                "global $fluid_emitter_lifetime_%d = %s"
                % (i, _fmt_float(float(value))))
            write_lines.append(
                "%s = $fluid_emitter_lifetime_%d"
                % (_control_lane(EMITTER_LIFETIME_REGS, i), i))

    # ---- 力场：一个发射器可以有多个力场，每个力场一个开关变量 ------------------
    # ``field_rows[i]`` 是第 i 个发射器每个力场的 0/1 列表，下标 = 面板上
    # ``em.force_fields`` 的下标 = ResourceFluidForceField 里那组三行的位置
    # （导出时被「参与导出」摘掉的字段**仍然占位**，只是 enabled 位写 0，
    # 见 fluid_ini_merge.build_force_field_floats 的 :852）。打包成位掩码写进
    # 一个 lane：bit k = 第 k 个力场，8 个发射器只占两个号。
    if want_field:
        write_lines.append(
            "; 每个力场的开关（位掩码：bit0..bit3 = 该发射器第 0..3 个力场）")
        for i, row in enumerate(field_rows):
            values = [1 if int(v) == 1 else 0
                      for v in list(row or ())[:FORCE_FIELDS_PER_SLOT]]
            if not values:
                continue
            for k, value in enumerate(values):
                global_lines.append(
                    "; 发射器 %d 力场 %d 开关，1 = 生效 / 0 = 不生效 → $fluid_force_enable_%d_%d"
                    % (i, k, i, k))
                global_lines.append(
                    "global $fluid_force_enable_%d_%d = %d" % (i, k, value))
            terms = ["$fluid_force_enable_%d_0" % i]
            for k in range(1, len(values)):
                terms.append("%d*$fluid_force_enable_%d_%d" % (1 << k, i, k))
            write_lines.append(
                "%s = %s" % (_control_lane(EMITTER_FIELD_REGS, i), " + ".join(terms)))
    return global_lines, write_lines, flag_lines, warnings


def apply_emitter_controls(text, speeds, field_switches=None, enables=(),
                          lifetimes=None):
    """把每发射器的控制变量写进模板，返回 ``(text, warnings)``。

    ``enables`` 是每个发射器的手动开关（1 = 开、0 = 关）。开关**本身**的变量
    ``$fluid_emit_N`` 模板里已经有（``global $emit_0..7 = 1``），所以这里只把
    关掉的那几条的初值改成 0 —— 键位（``[KeyFluidEmitN]`` 的 ``$emit_N = 1,0``）
    与动画驱动写的都是同一个变量。

    另有两处插入：``[Constants]`` 的声明（含力场族标记，缺省写 0）与
    ``[CommandListDrawWorld]`` 里抄进 IniParams 的行。一个值都没改时，速度与
    掩码这两族不出现，只多出族标记那一条 ``global $fluid_force_fields_switchable = 0``
    —— 它必须每次都写，理由见下面的注释（``IniParams`` 跨 mod 粘滞）。
    """
    global_lines, write_lines, _flag_lines, warnings = build_emitter_control(
        speeds, field_switches, lifetimes)

    # 力场族标记：**每次导出都写 literal 0/1**。模板里已经有 `w41 = 0`
    # （Probe.ini 的 x41/y41 那一块），所以这里改写；老模板没有这一行时插在
    # y41 之后。只写 1 不写 0 的话，上一份 mod 留下的 `w41 = 1` 会让这份没写
    # 58/59 的导出把每个力场都判 OFF（`ForceField.hlsl` 的 emitterFieldOn 先看
    # 41.w），而且不报错——IniParams 没有命名空间，这一行必须被显式覆盖。
    switchable = 1 if force_field_switchable(field_switches) else 0
    flag_line = "w41 = %d" % switchable
    if _FORCE_FLAG_LINE.search(text):
        text = _FORCE_FLAG_LINE.sub(
            lambda m: m.group(1) + flag_line, text, count=1)
    else:
        match = _FORCE_FLAG_ANCHOR.search(text)
        if match is None:
            warnings.append(
                "找不到模板里的 `y41 = $force_scale`，力场族标记 w41 没生成"
                "（旧模板的另一份 mod 可能把它留在 1，力场会被静默判 OFF）")
        else:
            text = _insert_after_match(text, match, [flag_line])

    if EMITTER_CONTROL_MARKER in text:
        return text, warnings

    # ---- 每发射器开关：`global` + 开场只赋一次（不用 persist，理由见下） ----------------------
    # 3Dmigoto 把 persist 变量的最后运行值记进 d3dx_user.ini，下次加载**覆盖** ini
    # 初值。主人的口径是「每次进游戏的起点 = 面板值」，所以在初始化块里再用
    # `$fluid_emit_seeded` 这个一次性标志把面板值盖回去；本场内的按键切换
    # 仍会一直生效到退出（seeded 置 1 之后不再写）。
    enables_list = [1 if int(v) == 1 else 0
                    for v in list(enables or ())[:EMITTER_CONTROL_MAX]]
    if enables_list:
        if global_lines:
            global_lines = [EMITTER_CONTROL_MARKER] + global_lines
        else:
            global_lines = [EMITTER_CONTROL_MARKER]
        global_lines.append("; 开场赋一次面板值用的标志（0 = 还没赋过）")
        global_lines.append("global $fluid_emit_seeded = 0")

        def _persist_line(match):
            index = int(match.group(2))
            if index >= len(enables_list):
                # 不存在的发射器：**必须显式写 0**。模板里 `$fluid_emit_3..7 = 1`
                # 是「永久通行证」——槽里记的 id 越界到 3..7 时，那个槽会一直开着
                # （幽灵发射器，2026-11-05 查了很久）。靠「没用到就不写」不行。
                return "%sglobal $fluid_emit_%d = 0" % (match.group(1), index)
            return "%sglobal $fluid_emit_%d = %d" % (
                match.group(1), index, enables_list[index])

        text, count = _EMIT_PERSIST_RE.subn(_persist_line, text)
        if count <= 0:
            warnings.append("找不到模板里的 `global $emit_N = 1`，开关没能改成面板值")
        match = _SEED_ANCHOR.search(text)
        if match is None:
            warnings.append("找不到模板里的 `$initialized = 1`，开场赋值没生成")
        else:
            indent = match.group(1)
            seed = ["%sif !$fluid_emit_seeded" % indent]
            for i, value in enumerate(enables_list):
                seed.append("%s    $fluid_emit_%d = %d" % (indent, i, value))
            seed.append("%s    $fluid_emit_seeded = 1" % indent)
            seed.append("%sendif" % indent)
            text = _insert_after_match(text, match, [""] + seed)
    elif global_lines:
        global_lines = [EMITTER_CONTROL_MARKER] + global_lines

    if global_lines:
        match = _EMITTER_GLOBALS_ANCHOR.search(text)
        if match is None:
            warnings.append(
                "找不到模板里的 `global $emit_7 = 1`，每发射器变量的声明没写进 [Constants]")
        else:
            text = _insert_after_match(text, match, global_lines)
    if write_lines:
        match = _EMITTER_WRITES_ANCHOR.search(text)
        if match is None:
            warnings.append(
                "找不到模板里的 `w43 = $emit_7`，每发射器变量的 IniParams 写入没生成")
        else:
            text = _insert_after_match(text, match, write_lines)
    return text, warnings


# ---------------------------------------------------------------------------
# 碰撞/可见性「排除列表」（按 draw 的 IB 哈希）
# ---------------------------------------------------------------------------
#: 列表里的每一项 = 一次 draw 的 IB 哈希。空列表**什么都不生成**（导出逐字节不变）。
#: 号段：`x53` = 掩码生效（shader 读它决定要不要采样，见 `fluid/ExcludeMaskRead.hlsl`）。
#: 每项一个开关变量 `$fluid_exclude_N`（由 ini 的 if 消费，shader 不需要）。
_HASH_RE = re.compile(r"^[0-9a-fA-F]{8,16}$")


def _section(name, body):
    return "[%s]\n%s\n" % (name, "\n".join(body))






# ---------------------------------------------------------------------------
# 全局快捷键改名
# ---------------------------------------------------------------------------
#: 模板里那 3 个全局 Key 段，(段名, 节点上的属性名)。
#: 顺序就是面板上的顺序：自动喷射、按住喷、缠流。
#: 2026-11-03 按主人要求删了两批：
#:   1. 另外六段（强度 +/-, 更白/更黄, 更不透明/更透明）——它们按下时会把
#:      ``$strength_level`` / ``$liquid_tint_r/g/b`` / ``$liquid_absorb_mm``
#:      覆盖成段内表值，跟面板导出值打架；
#:   2. 总开关键 ``KeyToggle``——总开关改成硬门控（角色不在场＝零计算），
#:      不再有手动总开关，``$toggle_pulse_request`` 改由 ``[Present]`` 写。
#: 段和这里的条目都是同一批删的。
GLOBAL_KEY_SECTIONS = (
    ("KeyAutoPulse", "auto_pulse"),
    ("KeyManualPulse", "manual_pulse"),
    ("KeyTrickle9", "trickle"),
)

#: 每个全局键的出厂值，必须与 ``Probe.ini`` 里那几段逐字一致。
#: 节点直接把这串填进输入栏：用户看到的就是「现在按什么」，不用回头翻 ini。
GLOBAL_KEY_DEFAULTS = {
    "auto_pulse": "no_alt no_ctrl no_shift VK_NUMPAD4",
    "manual_pulse": "no_alt no_ctrl no_shift VK_NUMPAD0",
    "trickle": "no_alt no_ctrl no_shift VK_NUMPAD9",
}

#: 段名 -> 属性名，反查用。
_SECTION_BY_FIELD = {field: section for section, field in GLOBAL_KEY_SECTIONS}

_KEY_SECTION_HEAD = re.compile(r"^\[([^\]]+)\][ \t]*$")
_KEY_LINE = re.compile(r"^([ \t]*)key\s*=\s*(.*)$")


def _replace_key_line(lines, start, stop, spec):
    """把 ``lines[start:stop]`` 里唯一那行 ``key =`` 换掉；没有就插在段首后一行。"""
    for idx in range(start, stop):
        m = _KEY_LINE.match(lines[idx])
        if m:
            lines[idx] = "%skey = %s" % (m.group(1), spec)
            return True
    lines.insert(start, "    key = %s" % spec)
    return False


def apply_global_keys(text, mapping, emitter_keys=()):
    """按 ``mapping`` 改写模板里 3 个全局 Key 段的 ``key =``。

    * 2026-11-03 按主人要求删了两批段与它们的表条目：六段会抢 ``$strength_level`` /
      ``$liquid_tint_r/g/b`` / ``$liquid_absorb_mm`` 的（强度 +/-, 更白/更黄,
      更不透明/更透明），以及总开关键 ``KeyToggle``——总开关已改成「角色不在场
      ＝零计算」的硬门控，没有手动开关了。现在模板里只剩 3 段，表里也只有 3 条。
    * ``mapping`` 里空串、缺项、或者**等于 ``GLOBAL_KEY_DEFAULTS`` 的出厂值**，
      都表示「保持模板默认」。后者是必须的：节点现在把出厂键直接填在输入栏里，
      如果按「非空就改」处理，每个字段都会去重写一遍自己，输出虽然一样但
      告警（同一个键挂了两个段）会全部误报。
    * ``emitter_keys`` 是各发射器已经绑的键，用来查重 —— 同一个键同时挂在
      总开关和某个发射器上，按下时两处都会动，那是设计错误而不是巧合。
    返回 ``(新文本, 告警列表)``。
    """
    wanted = {}
    for section, field in GLOBAL_KEY_SECTIONS:
        spec = normalise_key(mapping.get(field, ""))
        if spec and spec != normalise_key(GLOBAL_KEY_DEFAULTS.get(field, "")):
            wanted[section] = spec

    lines = text.splitlines()
    warnings = []
    if wanted:
        heads = []
        for idx, line in enumerate(lines):
            m = _KEY_SECTION_HEAD.match(line)
            if m:
                heads.append((idx, m.group(1)))
        # 从后往前改，前面段的行号才不会失效。
        for pos in range(len(heads) - 1, -1, -1):
            start, name = heads[pos]
            if name not in wanted:
                continue
            stop = heads[pos + 1][0] if pos + 1 < len(heads) else len(lines)
            _replace_key_line(lines, start + 1, stop, wanted[name])

    seen = {}
    for section, spec in wanted.items():
        seen.setdefault(spec, []).append(section)
    for spec, owners in seen.items():
        if len(owners) > 1:
            warnings.append("同一个键 %s 挂了 %s，按下去会一起动" % (spec, "、".join(owners)))
    for spec in [normalise_key(k) for k in emitter_keys if normalise_key(k)]:
        if spec in seen:
            warnings.append("发射器键 %s 与 %s 撞了" % (spec, "、".join(seen[spec])))

    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), warnings


# ---------------------------------------------------------------------------
# 发射速度 <-> 强度档
# ---------------------------------------------------------------------------
#: 稳态喷速公式，逐式来自 ``fluid/SoftNozzle.hlsl:9-51``（pulseSpeed=1、稳态开口）。
def speed_for_level(level):
    """强度档（0..7，可为小数）→ 稳态喷速，单位 m/s。"""
    setting = 0.28 if level < 0.5 else 0.565 + (level - 1.0) * 0.5625
    setting = max(0.28, min(3.94, setting))
    pressure = setting
    opening = max(0.80, min(1.75, 0.65 + 0.35 * pressure ** 0.5))
    discharge = 0.78 + 0.22 * max(0.0, min(1.0, (opening - 0.7) / 0.7))
    reference = 0.78 + 0.22 * (0.3 / 0.7)
    t = max(0.0, min(1.0, (setting - 1.69) / (3.94 - 1.69)))
    boost = 1.0 + 0.35 * (t * t * (3.0 - 2.0 * t))
    return 0.3807886553 * pressure ** 0.5 * discharge / reference * boost


def level_for_speed(speed):
    """稳态喷速（m/s）→ 强度档（0..7，小数）。二分，因为上面那条曲线不可解析求逆。"""
    speed = max(0.0, float(speed))
    if speed <= 1e-6:
        return 0.0
    lo, hi = 0.0, 7.0
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        if speed_for_level(mid) < speed:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 4)


# ---------------------------------------------------------------------------
# [Constants] 去重
# ---------------------------------------------------------------------------
#: 3Dmigoto 对同名段的合并行为不值得赌：一次导出里出现多个 [Constants] 时，
#: 只要它按"后者覆盖"处理，前面声明过的全局变量就全部消失，表现为整份 mod
#: 静默不工作。这里把所有 [Constants] 的正文并进第一个出现的段，其余段头删掉，
#: 正文按"去空白后的整行"去重（重复的 global 声明只留第一条）。
def collapse_constants(text):
    lines = text.splitlines()
    blocks = []
    cur_name, cur = None, []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("[") and "]" in stripped:
            blocks.append((cur_name, cur))
            cur_name, cur = stripped, []
            continue
        cur.append(line)
    blocks.append((cur_name, cur))

    # 先把所有 Constants 的正文收集起来（按出现顺序）
    bodies = []
    for name, body in blocks:
        if name is not None and name.strip().lower() == "[constants]":
            bodies.append(body)
    if not bodies:
        return text

    merged, seen = [], set()
    for body in bodies:
        for bline in body:
            key = bline.strip()
            if key and not key.startswith(";") and not key.startswith("//"):
                if key in seen:
                    continue
                seen.add(key)
            merged.append(bline)

    out, emitted = [], False
    for name, body in blocks:
        if name is not None and name.strip().lower() == "[constants]":
            if emitted:
                continue
            emitted = True
            out.append("[Constants]")
            out.extend(merged)
            continue
        if name is not None:
            out.append(name)
        out.extend(body)
    return chr(10).join(out) + chr(10)


# ---------------------------------------------------------------------------
# 槽位数
# ---------------------------------------------------------------------------
#: 与 ``ParticleConfig.hlsl`` 的 ``SLOT_COUNT=8`` 同值：池子只装 8 个槽。
SLOT_COUNT_MAX = 8


def apply_slot_count(text, emitter_count, warnings=None):
    """把 ``$slot_count`` 写成**本轮要跟踪的发射器数**（1..8）。

    一个发射器占一个槽：``surface/ResolveSurface.hlsl:41-48`` 按「root + 发射
    器 id」分组产生候选，``fluid/AssignSlots.hlsl:29`` 又拿 ``IniParams[253].z``
    当槽位上限（``:42``/``:61`` 用它筛槽）。写死 1 时只有 slot 0 能被填，
    第 2 个及以后的发射器静默不喷。

    下限 1：没有发射器时也留 1 个槽，避免上界变 0 让 ``clamp(..,1,N)``
    与空 pass 出怪。上限 ``SLOT_COUNT_MAX``：池子只有 8 槽，写多了也是空转
    —— 但**超出就真的要告警**：第 9 个发射器不是「少一个热键」，是整帧
    拿不到槽位、根本不出水，光靠按键那条告警会让人以为是按键问题。
    """
    count = max(1, min(SLOT_COUNT_MAX, int(emitter_count)))
    if int(emitter_count) > SLOT_COUNT_MAX and warnings is not None:
        warnings.append(
            "共 %d 个发射器，超过槽位上限 %d：第 %d 个起在游戏里拿不到槽位、不会出水"
            "（着色器池子只有 %d 槽），请拆成多份 mod 或减少发射器"
            % (int(emitter_count), SLOT_COUNT_MAX, SLOT_COUNT_MAX + 1, SLOT_COUNT_MAX)
        )
    return re.sub(
        r"global\s+\$((?:%s)?slot_count)\s*=\s*[^\s;]+" % VARIABLE_PREFIX,
        lambda m: "global $%s = %d" % (m.group(1), count),
        text,
    )


# ---------------------------------------------------------------------------
# 重力倍率与粘度
# ---------------------------------------------------------------------------
def apply_gravity_scale(text, scale):
    """写 ``$gravity_scale``（``IniParams[47].x``，1 = 参考实现的 9.81）。"""
    return re.sub(
        r"global\s+\$((?:%s)?gravity_scale)\s*=\s*[^\s;]+" % VARIABLE_PREFIX,
        lambda m: "global $%s = %s" % (m.group(1), _fmt_float(scale)),
        text,
    )


def apply_viscosity(text, viscosity):
    """写 ``$viscosity``（``IniParams[47].y``）。

    0 表示沿用 profile 里的 ``PARAM.w``，所以要写成 0 而不是省略，否则上一份
    导出留下的值会继续生效。
    """
    return re.sub(
        r"global\s+\$((?:%s)?viscosity)\s*=\s*[^\s;]+" % VARIABLE_PREFIX,
        lambda m: "global $%s = %s" % (m.group(1), _fmt_float(viscosity)),
        text,
    )


EMIT_FLAGS_LANE = "x60"


def apply_emit_flags(text, hard_off=True, tail=True):
    """按面板写 ``x60`` 位掩码：bit0 = 关掉即清空该槽，bit1 = 尾流。

    两者**默认都是开**：都保持默认时不写任何行（产物与旧版逐字节相同）；
    一旦有一个关掉，就写 ``x60 = <mask>``，由 ``FluidSolver.hlsl`` 的
    ``EMITFLAGS`` 解码（``IniParams[60].x``，0 视为默认 3）。

    返回 ``(text, warnings)``。
    """
    warnings = []
    mask = (1 if hard_off else 0) | (2 if tail else 0)
    if mask == 3:
        return text, warnings
    anchor = r"(?m)^([ \t]*)x47[ \t]*=[ \t]*\$fluid_gravity_scale[ \t]*"
    if not re.search(anchor, text):
        warnings.append("找不到 `x47 = $fluid_gravity_scale`，x60 未写")
        return text, warnings
    text = re.sub(
        anchor,
        lambda m: m.group(0) + "\n" + m.group(1) + "x60 = %d" % mask,
        text,
        count=1,
    )
    return text, warnings
