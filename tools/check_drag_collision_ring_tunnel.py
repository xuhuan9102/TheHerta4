"""整环拖拽的穿透复现 / 回归验证（CPU 仿真）。

复现用户报告：
    拖拽范围很大时，一个圆柱网格（丝袜筒）的**整圈**顶点都在拖拽范围内，
    于是全圈共同向一个方向位移；碰撞体在筒中间。要求：顶点在任何情况下都
    不许穿过碰撞体 —— 允许「一部分顶点被拖动、一部分被拦在表面」。

做法：把 GPU 侧的三段逻辑 1:1 移植到 numpy 上跑——
    1. 生成器 `_bake_collider_tri_grid`（blueprint/node_postprocess_draginteraction.py）
       的均匀网格 + 表面壳 + CSR 登记；
    2. shader `DistGridSignedN`（Toolset/drag_interaction/rzm_jiggle_interaction.hlsl）
       的「中心格先扫 + 26 邻格 + 预算 256」最近三角形带符号距离；
    3. shader `CollisionSolveGridCore` / `CollisionSolveGrid`（同文件）的终点预判 /
       CCD / 投影 / 收尾，以及外层「位移不得沿起点法线向内」的最终不变量。

两个开关：
    --gate old|new|both  终点预判的放行条件（old = 只看终点，new = 先看方向）
    每个场景会跑「无 clamp / 有 clamp」两版做对照。
判据是「沿 p0→p 实际位移采样的最大陷入深度」，不是终点 x 符号翻转 ——
切向滑到另一侧但半径仍在外侧属于正常绕行。

用法（在仓库根目录）：
    python tools/check_drag_collision_ring_tunnel.py                # 全变体对照
    python tools/check_drag_collision_ring_tunnel.py --gate old     # 只看现行终点预判

现状（2026-10-23）：gate=old 无 clamp 时 8 个场景共 304 个顶点穿进实体、
最深 74.9mm、333 个顶点跨到另一侧；加 clamp 后陷穿 0、最深 0.00mm。
"""

from __future__ import annotations

import argparse
import math

import numpy as np

# ── shader 常量（与 rzm_jiggle_interaction.hlsl 一一对应）──────────────────
COLLISION_MIN_GAP = 1e-6
COLLISION_GAP_SCALE = 1.0
COLLISION_BASE_SIGN = 1.0
COLLISION_CCD_STEPS = 16
COLLISION_CORE_STEPS = 1
COLLISION_PROJ_STEPS = 64
COLLISION_NUMERIC_EPS = 1e-4
COLLISION_GRID_MAX_TRIS = 4096
COLLISION_QUERY_MAX_TRIS = 256
GUARD_MAX_STEP = 0.05

MISS = 1e30


# ── 1. 网格烘焙（移植 _bake_collider_tri_grid）────────────────────────────
def bake_tri_grid(A, B, C, nout, *, reach=0.6, shell_cells=1, max_per_cell=4096):
    A = np.asarray(A, dtype=np.float64)
    B = np.asarray(B, dtype=np.float64)
    C = np.asarray(C, dtype=np.float64)
    nout = np.asarray(nout, dtype=np.float64)
    tri_verts = np.zeros((len(A), 4, 4), dtype=np.float64)
    tri_verts[:, 0, 0:3] = A
    tri_verts[:, 1, 0:3] = B
    tri_verts[:, 2, 0:3] = C
    tri_verts[:, 3, 0:3] = nout

    edges = np.concatenate([
        np.linalg.norm(B - A, axis=1),
        np.linalg.norm(C - B, axis=1),
        np.linalg.norm(A - C, axis=1),
    ])
    h = float(np.percentile(edges, 99)) * 1.5
    tri_lo = np.minimum(np.minimum(A, B), C)
    tri_hi = np.maximum(np.maximum(A, B), C)
    bmin = tri_lo.min(axis=0) - h - reach
    pad = int(max(shell_cells, 0))
    lo = np.maximum(np.floor((tri_lo - bmin) / h).astype(np.int64) - pad, 0)
    hi = np.floor((tri_hi - bmin) / h).astype(np.int64) + pad
    dims = np.maximum(hi.max(axis=0) + 2 + int(np.ceil(reach / h)) * 2, 1).astype(np.int64)
    cell_count = int(dims[0] * dims[1] * dims[2])
    span = hi - lo + 1

    tri_c = (A + B + C) / 3.0
    buckets: dict[int, list[int]] = {}
    for t in range(len(A)):
        for ox in range(int(span[t, 0])):
            for oy in range(int(span[t, 1])):
                for oz in range(int(span[t, 2])):
                    key = int(((lo[t, 2] + oz) * dims[1] + (lo[t, 1] + oy)) * dims[0]
                              + (lo[t, 0] + ox))
                    buckets.setdefault(key, []).append(t)
    for key, cell in buckets.items():
        if len(cell) < 2:
            continue
        kx = key % dims[0]
        ky = (key // dims[0]) % dims[1]
        kz = key // (dims[0] * dims[1])
        cc = bmin + (np.array([kx, ky, kz], dtype=np.float64) + 0.5) * h
        arr = np.asarray(cell, dtype=np.int64)
        dd = np.einsum("cd,cd->c", tri_c[arr] - cc, tri_c[arr] - cc)
        order = np.argsort(dd, kind="stable")
        if max_per_cell > 0 and len(order) > max_per_cell:
            order = order[:max_per_cell]
        buckets[key] = arr[order].tolist()

    offsets = np.zeros(cell_count + 1, dtype=np.int64)
    flat: list[int] = []
    for key in range(cell_count):
        offsets[key] = len(flat)
        cell = buckets.get(key)
        if cell:
            flat.extend(cell)
    offsets[cell_count] = len(flat)
    return {
        "tri_verts": tri_verts,
        "bmin": bmin,
        "cell": h,
        "dims": dims,
        "offsets": offsets,
        "flat": np.asarray(flat, dtype=np.int64),
        "cell_count": cell_count,
        "nonempty": len(buckets),
    }


# ── 最近点（Ericson，向量化）──────────────────────────────────────────────
def closest_on_tri(p, A, B, C):
    ab = B - A
    ac = C - A
    ap = p[None, :] - A
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p[None, :] - B
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p[None, :] - C
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        v_ab = d1 / (d1 - d3)
        w_ac = d2 / (d2 - d6)
        w_bc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        inv = 1.0 / denom
    cond = [
        (d1 <= 0) & (d2 <= 0),
        (d3 >= 0) & (d4 <= d3),
        (vc <= 0) & (d1 >= 0) & (d3 <= 0),
        (d6 >= 0) & (d5 <= d6),
        (vb <= 0) & (d2 >= 0) & (d6 <= 0),
        (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0),
    ]
    n = len(A)
    q = np.zeros((n, 3), dtype=np.float64)
    choices = [
        np.broadcast_to(A, (n, 3)),
        np.broadcast_to(B, (n, 3)),
        A + v_ab[:, None] * ab,
        np.broadcast_to(C, (n, 3)),
        A + w_ac[:, None] * ac,
        B + w_bc[:, None] * (C - B),
        A + (vb * inv)[:, None] * ab + (vc * inv)[:, None] * ac,
    ]
    stacked = np.stack([np.nan_to_num(c, nan=0.0) for c in choices], axis=0)
    idx = np.zeros(n, dtype=np.int64)
    for k in range(len(cond) - 1, -1, -1):
        idx = np.where(cond[k], k, idx)
    q = stacked[idx, np.arange(n), :]
    return q


def tri_arrays(grid):
    tv = grid["tri_verts"]
    return tv[:, 0, 0:3], tv[:, 1, 0:3], tv[:, 2, 0:3], tv[:, 3, 0:3]


# ── 2. DistGridSignedN 移植 ───────────────────────────────────────────────
def dist_grid_signed_n(grid, p):
    A, B, C, N = tri_arrays(grid)
    bmin = grid["bmin"]
    cell = grid["cell"]
    dims = np.maximum(grid["dims"], 1)
    c = np.floor((p - bmin) / max(cell, 1e-6)).astype(np.int64)
    c = np.clip(c, np.zeros(3, dtype=np.int64), dims - 1)

    cand: list[int] = []
    checked = 0

    def take(cx, cy, cz):
        nonlocal checked
        ci = int(cx + dims[0] * (cy + dims[1] * cz))
        s = int(grid["offsets"][ci])
        e = int(grid["offsets"][ci + 1])
        e = min(e, s + COLLISION_GRID_MAX_TRIS)
        for k in range(s, e):
            if checked >= COLLISION_QUERY_MAX_TRIS:
                return True
            cand.append(int(grid["flat"][k]))
            checked += 1
        return checked >= COLLISION_QUERY_MAX_TRIS

    if take(c[0], c[1], c[2]):
        pass
    else:
        stop = False
        for oz in (-1, 0, 1):
            if stop:
                break
            for oy in (-1, 0, 1):
                if stop:
                    break
                for ox in (-1, 0, 1):
                    if checked >= COLLISION_QUERY_MAX_TRIS:
                        stop = True
                        break
                    if ox == 0 and oy == 0 and oz == 0:
                        continue
                    nn = c + np.array([ox, oy, oz], dtype=np.int64)
                    if np.any(nn < 0) or np.any(nn >= dims):
                        continue
                    if take(nn[0], nn[1], nn[2]):
                        stop = True
                        break

    if not cand:
        return MISS, np.array([0.0, 1.0, 0.0])
    idx = np.asarray(cand, dtype=np.int64)
    q = closest_on_tri(p, A[idx], B[idx], C[idx])
    diff = p[None, :] - q
    dd = np.linalg.norm(diff, axis=1)
    best = int(np.argmin(dd))          # 严格 < 比较 ⇒ 取第一个最小值
    signed = float(np.dot(diff[best], N[idx[best]]))
    return signed, N[idx[best]].copy()


# ── 3. CollisionSolveGrid 移植 ────────────────────────────────────────────
def clamp_inward(p0, p, n0):
    """最终不变量：相对起点法线的位移不得含向内分量（半空间夹取，零查询）。"""
    disp = p - p0
    rad = float(np.dot(disp, n0))
    if rad < 0.0:
        return p0 + disp - n0 * rad
    return p


def far_side_barrier(p, p0, grab, offset, center, axis, axis_min, axis_max, radius, gap):
    """CPU 复刻 rzm_jiggle_interaction.hlsl::ClampFarSideBarrier（远侧方向性约束）。"""
    d = np.asarray(offset, dtype=np.float64)
    n = float(np.linalg.norm(d))
    if n < 1e-9:
        return p
    d = d / n
    d = d - axis * float(np.dot(d, axis))
    n = float(np.linalg.norm(d))
    if n < 1e-9:
        return p
    d = d / n

    rel0 = p0 - center
    axial = float(np.dot(rel0, axis))
    if axial < axis_min or axial > axis_max:
        return p
    radial = rel0 - axis * axial
    envelope = radius + gap + 1e-4
    if float(np.dot(radial, radial)) > envelope * envelope:
        return p
    grab_side = float(np.dot(grab - center, d))
    if abs(grab_side) < 1e-6:
        return p
    s = 1.0 if grab_side > 0.0 else -1.0
    mid = float(np.dot(center, d))
    side = s * (float(np.dot(p0, d)) - mid)
    w = min(max(side / radius, 0.0), 1.0)
    block = 1.0 - (w * w * (3.0 - 2.0 * w))
    if block <= 1e-4:
        return p
    along = float(np.dot(p - p0, d))
    return p + d * (along * (1.0 - block) - along)


def collision_solve_grid(grid, p0, offset, n0, gap, gate, clamp=False):
    p = p0 + offset
    tolerance = 0.0                              # COLLISION_PARAMS.y 取 0

    dEnd, _ = dist_grid_signed_n(grid, p)
    endpoint_free = bool(np.isfinite(dEnd)) and dEnd <= 1e29 and dEnd >= -1.0 \
        and dEnd >= gap - COLLISION_NUMERIC_EPS
    endpoint_miss = (not np.isfinite(dEnd)) or dEnd > 1e29

    while True:                                  # 让所有出口都经过最终不变量
        if gate == "old":
            # 现行 HEAD：只看终点，不看路径，也不看位移是否有向内分量
            if endpoint_miss:
                tag, src = "endpoint_miss", p
                break
            if endpoint_free:
                tag, src = "endpoint_free", p
                break
        else:
            # 备选：只有「位移不含向内分量」时才允许凭终点放行
            inward = float(np.dot(offset, n0))
            if inward >= 0.0 and (endpoint_free or endpoint_miss):
                tag, src = "outward_free", p
                break
        tag, src = "solved", None
        break

    if src is not None:
        return clamp_inward(p0, src, n0) if clamp else src, tag

    for _ in range(COLLISION_CORE_STEPS):
        tStar = -1.0
        dStar = 0.0
        qStar = p
        for st in range(1, COLLISION_CCD_STEPS + 1):
            t = st / COLLISION_CCD_STEPS
            pt = p0 + (p - p0) * t
            d, hitN = dist_grid_signed_n(grid, pt)
            if (not np.isfinite(d)) or d > 1e29 or d < -1.0:
                break                             # 打空 ⇒ 结束 CCD（2026-10-22）
            if d < gap:
                tStar = t
                dStar = d
                qStar = pt - hitN * d
                break
        if tStar < 0.0:
            break
        pStar = p0 + (p - p0) * tStar
        g = pStar - qStar
        gl = float(np.linalg.norm(g))
        if gl < 1e-9:
            break
        gdir = g / gl
        pushDir = -gdir if float(np.dot(gdir, n0)) < 0.0 else gdir
        guardPush = gap - dStar
        if (not math.isfinite(guardPush)) or abs(guardPush) > GUARD_MAX_STEP:
            guardPush = 0.0
        pStarOut = pStar + pushDir * guardPush
        rem = (p - p0) * (1.0 - tStar)
        dn = float(np.dot(rem, pushDir))
        if dn < 0.0:
            rem = rem - pushDir * dn
        p = pStarOut + rem

    pdPrev = -1e30
    for _ in range(COLLISION_PROJ_STEPS):
        pd, projN = dist_grid_signed_n(grid, p)
        if (not np.isfinite(pd)) or pd > 1e29 or pd < -1.0:
            break
        if pd >= gap - COLLISION_NUMERIC_EPS:
            break
        if pd - pdPrev < COLLISION_NUMERIC_EPS * 0.25:
            break
        pdPrev = pd
        step = projN * (gap - pd)
        if float(np.dot(step, step)) > GUARD_MAX_STEP * GUARD_MAX_STEP:
            break
        p = p + step
    return clamp_inward(p0, p, n0) if clamp else p, "solved"


# ── 场景：圆柱碰撞体 + 整圈丝袜顶点 ───────────────────────────────────────
R_LEG = 0.075
H_LEG = 0.90
NSEG = 48
NLEN = 24


def make_cylinder(R=R_LEG, H=H_LEG, nseg=NSEG, nlen=NLEN, caps=False):
    verts = []
    for j in range(nlen + 1):
        y = H * j / nlen
        for i in range(nseg):
            a = 2.0 * math.pi * i / nseg
            verts.append([R * math.cos(a), y, R * math.sin(a)])
    verts = np.asarray(verts, dtype=np.float64)
    n_verbose = len(verts)
    if caps:
        verts = np.vstack([verts, [[0.0, 0.0, 0.0], [0.0, H, 0.0]]])
    c_lo, c_hi = n_verbose, n_verbose + 1

    tris = []
    for j in range(nlen):
        for i in range(nseg):
            i2 = (i + 1) % nseg
            a = j * nseg + i
            b = j * nseg + i2
            c = (j + 1) * nseg + i2
            d = (j + 1) * nseg + i
            tris.append([a, b, c])
            tris.append([a, c, d])
    if caps:
        for i in range(nseg):
            i2 = (i + 1) % nseg
            tris.append([c_lo, i2, i])
            tris.append([c_hi, nlen * nseg + i, nlen * nseg + i2])
    tris = np.asarray(tris, dtype=np.int64)

    # 顶点法线（外法线；端盖按 ±y）
    normals = []
    for j in range(nlen + 1):
        for i in range(nseg):
            a = 2.0 * math.pi * i / nseg
            normals.append([math.cos(a), 0.0, math.sin(a)])
    normals = np.asarray(normals, dtype=np.float64)
    if caps:
        normals = np.vstack([normals, [[0.0, -1.0, 0.0], [0.0, 1.0, 0.0]]])

    A = verts[tris[:, 0]]
    B = verts[tris[:, 1]]
    C = verts[tris[:, 2]]
    n_face = np.cross(B - A, C - A)
    nn = np.linalg.norm(n_face, axis=1, keepdims=True)
    nn[nn < 1e-12] = 1.0
    n_face = n_face / nn
    ref = (normals[tris[:, 0]] + normals[tris[:, 1]] + normals[tris[:, 2]]) / 3.0
    flip = np.einsum("ij,ij->i", ref, n_face) < 0.0
    n_out = np.where(flip[:, None], -n_face, n_face)
    return A, B, C, n_out


def inside_solid(p, R=R_LEG, H=H_LEG, eps=1e-9):
    r = math.hypot(p[0], p[2])
    return (r < R - eps) and (-eps <= p[1] <= H + eps)


def path_enters_solid(p0, p1, samples=480):
    """所在帧的实际位移 p0→p1 是否进入碰撞体实体（= 视觉上的穿透）。"""
    for k in range(1, samples + 1):
        t = k / samples
        if inside_solid(p0 + (p1 - p0) * t):
            return True
    return False


def path_dip(p0, p1, samples=480):
    """沿实际位移 p0→p1 采样，返回最大「陷进实体」深度（米）。"""
    worst = 0.0
    for k in range(1, samples + 1):
        t = k / samples
        q = p0 + (p1 - p0) * t
        r = math.hypot(q[0], q[2])
        if -1e-9 <= q[1] <= H_LEG + 1e-9 and r < R_LEG:
            worst = max(worst, R_LEG - r)
    return worst


def run(gate, drag, mag, t0, label, clamp=False, barrier=False):
    A, B, C, N = make_cylinder()
    grid = bake_tri_grid(A, B, C, N)
    gap = COLLISION_BASE_SIGN * max(abs(t0), COLLISION_MIN_GAP) * COLLISION_GAP_SCALE

    y_mid = 0.45
    n = 96
    p0s = []
    n0s = []
    for i in range(n):
        a = 2.0 * math.pi * i / n
        rad = R_LEG + t0
        p0s.append([rad * math.cos(a), y_mid, rad * math.sin(a)])
        n0s.append([math.cos(a), 0.0, math.sin(a)])
    p0s = np.asarray(p0s)
    n0s = np.asarray(n0s)
    d = np.asarray(drag, dtype=np.float64)
    d = d / np.linalg.norm(d)
    offset = d * mag

    # 远侧方向性约束的圆柱形状（与导出侧 PCA 对圆柱的结果一致）
    shape_center = np.array([0.0, H_LEG / 2.0, 0.0])
    shape_axis = np.array([0.0, 1.0, 0.0])
    shape_min, shape_max, shape_radius = 0.0, H_LEG, R_LEG
    grab = shape_center + d * (R_LEG + t0)

    tunneled = 0
    inside_end = 0
    flip = 0
    blocked = 0
    moves = []
    dips = []
    tags = {}
    for i in range(n):
        p, tag = collision_solve_grid(grid, p0s[i], offset, n0s[i], gap, gate, clamp)
        if barrier and clamp:
            p = far_side_barrier(
                p, p0s[i], grab, offset,
                shape_center, shape_axis, shape_min, shape_max, shape_radius, gap,
            )
            tag = tag + "+barrier"
        tags[tag] = tags.get(tag, 0) + 1
        moves.append(float(np.linalg.norm(p - p0s[i])))
        if inside_solid(p):
            inside_end += 1
        dip = path_dip(p0s[i], p)
        dips.append(dip)
        if dip > 1e-6:
            tunneled += 1
        if float(np.dot(p0s[i], d)) < 0.0 < float(np.dot(p, d)):
            flip += 1
        if float(np.linalg.norm(p - p0s[i])) < 0.25 * mag:
            blocked += 1
    moves = np.asarray(moves)
    dips = np.asarray(dips)
    print(f"  {label:<30} gate={gate:<3} clamp={str(clamp):<5} barrier={str(barrier):<5} "
          f"陷穿={tunneled:>3}/{n} 最深={dips.max()*1000:6.2f}mm  "
          f"跨侧={flip:>3}  被拦={blocked:>3}  位移中位={np.median(moves):.4f} "
          f"max={moves.max():.4f}  {tags}")
    return tunneled, dips.max(), flip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", choices=["old", "new", "both"], default="both")
    ap.add_argument("--barrier", action="store_true",
                    help="在 clamp=True 的出口上再加远侧方向性约束（新修复）")
    args = ap.parse_args()

    print("圆柱碰撞体 R=0.075 H=0.90 —— 整圈 96 个顶点全部落在拖拽范围内")
    variants = [("old", False, False), ("old", True, False), ("new", True, False)]
    if args.barrier:
        variants += [("old", True, True), ("new", True, True)]
    if args.gate != "both":
        variants = [v for v in variants if v[0] == args.gate]

    cases = [
        ("垂直腿轴 +x, 0.15", (1, 0, 0), 0.15, 3.9e-4),
        ("垂直腿轴 +x, 0.35", (1, 0, 0), 0.35, 3.9e-4),
        ("垂直腿轴 +x, 0.50(=maxOffset)", (1, 0, 0), 0.50, 3.9e-4),
        ("垂直腿轴 +x, 0.50, t0=7e-3", (1, 0, 0), 0.50, 7.0e-3),
        ("沿腿轴 +y, 0.50", (0, 1, 0), 0.50, 3.9e-4),
        ("斜向 (1,0,1), 0.50", (1, 0, 1), 0.50, 3.9e-4),
        ("垂直 -z, 0.50", (0, 0, -1), 0.50, 3.9e-4),
        ("垂直腿轴 +x, 0.50, t0=1e-4", (1, 0, 0), 0.50, 1.0e-4),
    ]
    totals: dict[tuple[str, bool, bool], float] = {}
    for label, drag, mag, t0 in cases:
        for gate, clamp, barrier in variants:
            n, dip, flip = run(gate, drag, mag, t0, label, clamp, barrier)
            key = (gate, clamp, barrier)
            prev = totals.get(key, (0, 0.0, 0))
            totals[key] = (prev[0] + n, max(prev[1], dip), prev[2] + flip)
    print("\n汇总（陷穿顶点数 / 最深 / 跨侧顶点数）：")
    for (gate, clamp, barrier), (n, dip, flip) in totals.items():
        print(f"  gate={gate:<3} clamp={str(clamp):<5} barrier={str(barrier):<5}  "
              f"陷穿={n:>4}  最深={dip*1000:7.2f}mm  跨侧={flip:>4}")


if __name__ == "__main__":
    main()
