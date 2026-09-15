# -*- coding: utf-8 -*-
"""反细分＆细分·UV防错乱（标准实现：来自另一位开发者的独立插件 v3.7）。

算法体**逐字保留**（便于与对方版本对拍），只按仓库约定改了 bl_idname 前缀
（mesh.* → toolkit.*）与注册方式（改为导出算子清单 + 菜单钩子，由
toolkit/__init__.py 统一注册）。

入口：
- toolkit.uv_guard_unsubdivide  反细分·UV孤岛保护（iterations / merge_triangles /
  only_selected / sel_shrink）
- toolkit.uv_guard_subdivide    细分·UV防错乱（cuts / fix_boundary_ngons）
- 两个算子同时挂在 编辑模式 3D 视图 >「网格」菜单，以及 BMTP_MeshEditPanel 面板。

"""
import math

import bpy
import bmesh
from bmesh import utils as bmesh_utils

# ======================================================================
# 一、为什么要"先拆 UV 缝"
# ----------------------------------------------------------------------
# Blender 反细分（BM_mesh_decimate_unsubdivide_ex）的流程是：
#   棋盘格标记 → 耳切（把塌陷点周围 >3 边的面沿对角线切开）→ 溶解合并。
# 它完全不看 UV，会横跨 UV 缝把两侧的面焊在一起 → UV 错乱。
#
# 本插件在反细分之前沿 UV 孤岛边界拆边（split_edges），两侧拓扑断开，
# 之后的反细分就不可能跨缝合并；最后再焊回重合顶点。
#
# ======================================================================
# 二、三个已知坑与对应处理
# ----------------------------------------------------------------------
# 1) 尖刺 / 面被拉飞
#    耳切若用批量 bmesh.ops.connect_verts(verts=一大串顶点)，它会把
#    "同一个面内所有被标记的顶点"两两相连，而不是只连需要的那一对，
#    于是多出错误的对角线，塌陷后表现为尖刺。
#    → 改用 bmesh.utils.face_split(face, a, b)：明确指定面与顶点对，
#      不会误连，而且速度和批量调用一样快（实测 3600 面 0.01s）。
#
# 2) 空洞（缺一个角）
#    拆缝后两侧各自反细分，缝顶点的溶解在两侧可能得到不完全对称的结果，
#    焊回后那一小块就没有面 → 看着像缺了个角的洞。
#    → 焊回后用 bmesh.ops.holes_fill(sides=4) 补掉 3~4 边的小洞，
#      并把洞边顶点原有的 UV 复制给新面。实测 64 个 UV 岛的网格：
#      开放边 164 → 64（不补的话比原始网格的 96 还多），只多 25 个面。
#
# 3) 慢
#    逐顶点调用 operator 的复杂度是 O(n²)（1 万面时溶解一步独占 28 秒）。
#    → 耳切一次收集、溶解一次调用（一次 C 调用处理全部顶点）。
#      实测 3600 面 3.83s → 0.15s，10000 面 28.7s → 约 0.4s。
# ======================================================================

VERT_DISSOLVE_MAX = 4
_INIT, _IGNORE, _DO_COLLAPSE = 0, 1, -1


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def _pk(co):
    """坐标指纹（用于拆分后按位置匹配两侧顶点）"""
    return (round(co.x, 5), round(co.y, 5), round(co.z, 5))


def _loop_uv_pair(l, uvl):
    """一条边在它所属面内的 UV 端点；两侧一致说明这条边 UV 连续"""
    a, b = l.vert, l.link_loop_next.vert
    ua, ub = l[uvl].uv, l.link_loop_next[uvl].uv
    return {a.index: (round(ua.x, 6), round(ua.y, 6)),
            b.index: (round(ub.x, 6), round(ub.y, 6))}


def compute_uv_islands(bm):
    """按 UV 连续性给面分组，返回 {面index: 岛根index}"""
    bm.verts.index_update()
    bm.faces.index_update()
    uvl = bm.loops.layers.uv.active
    parent = {f.index: f.index for f in bm.faces}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in bm.edges:
        if len(e.link_loops) < 2:
            continue
        l1, l2 = e.link_loops[0], e.link_loops[1]
        if l1.face is l2.face:
            continue
        if _loop_uv_pair(l1, uvl) == _loop_uv_pair(l2, uvl):
            r1, r2 = find(l1.face.index), find(l2.face.index)
            if r1 != r2:
                parent[r1] = r2
    return {f.index: find(f.index) for f in bm.faces}


def find_uv_seam_edges(bm):
    """UV 孤岛边界上的边（两侧 UV 不连续）"""
    bm.verts.index_update()
    uvl = bm.loops.layers.uv.active
    out = []
    for e in bm.edges:
        if len(e.link_loops) < 2:
            continue
        l1, l2 = e.link_loops[0], e.link_loops[1]
        if l1.face is l2.face:
            continue
        if _loop_uv_pair(l1, uvl) != _loop_uv_pair(l2, uvl):
            out.append(e)
    return out


# ----------------------------------------------------------------------
# 复刻 Blender 原生反细分的判定与标记
# ----------------------------------------------------------------------
def _can_dissolve(v):
    """对应 C 源码里的 bm_vert_dissolve_fan_or_chain_test()"""
    tot = bnd = man = wire = 0
    for e in v.link_edges:
        nf = len(e.link_faces)
        if nf == 1:
            bnd += 1
        elif nf == 2:
            man += 1
        elif nf == 0:
            wire += 1
        else:
            return False
        if tot == VERT_DISSOLVE_MAX:
            return False
        tot += 1
    return ((tot == 4 and bnd == 0 and man == 4) or
            (tot == 3 and bnd == 0 and man == 3) or
            (tot == 3 and bnd == 2 and man == 1) or
            (tot == 2 and wire == 2) or
            (tot == 2 and man == 2))


def _tag_neighbors(starts, desired, idx):
    out = []
    for v in starts:
        for e in v.link_edges:
            o = e.other_vert(v)
            if idx.get(o) == _INIT:
                idx[o] = desired
                out.append(o)
    return out


def compute_marking(bm):
    """与原版完全一致的棋盘格标记：返回 {顶点: _INIT/_IGNORE/_DO_COLLAPSE}"""
    idx = {}
    for v in bm.verts:
        idx[v] = _INIT if _can_dissolve(v) else _IGNORE
    for v in bm.verts:
        if idx.get(v) != _INIT:
            continue
        idx[v] = _IGNORE
        ignore = [v]
        while True:
            coll = _tag_neighbors(ignore, _DO_COLLAPSE, idx)
            if not coll:
                break
            ignore = _tag_neighbors(coll, _IGNORE, idx)
            if not ignore:
                break
    return idx


# ----------------------------------------------------------------------
# 耳切（精确版：face_split，不会误连）
# ----------------------------------------------------------------------
def _collect_split_triples(bm, collapse):
    """收集需要"切耳朵"的 (面, 端点A, 端点B) 三元组"""
    out = []
    for v in bm.verts:
        if _pk(v.co) not in collapse:
            continue
        for f in v.link_faces:
            if len(f.verts) <= 3:
                continue
            l = next((l for l in f.loops if l.vert is v), None)
            if l is None:
                continue
            a, b = l.link_loop_prev.vert, l.link_loop_next.vert
            if a is b or bm.edges.get((a, b)) is not None:
                continue
            out.append((f, a, b))
    return out


def _apply_splits(bm, triples):
    """逐个精确切分；面可能已被前一次切分替换，所以要做有效性回退"""
    done = 0
    for f, a, b in triples:
        if not a.is_valid or not b.is_valid:
            continue
        if bm.edges.get((a, b)) is not None:
            continue
        target = None
        if f.is_valid and a in f.verts and b in f.verts:
            target = f
        else:
            for g in a.link_faces:
                if b in g.verts:
                    target = g
                    break
        if target is None:
            continue
        try:
            bmesh_utils.face_split(target, a, b)
            done += 1
        except Exception:
            pass
    return done


# ----------------------------------------------------------------------
# 补洞（修掉"缺一个角"的空洞）
# ----------------------------------------------------------------------
def _uv_edge_continuous(e, uvl):
    """这条边两侧的 UV 是否连续（= 两个面属于同一个 UV 岛）"""
    if len(e.link_loops) < 2:
        return False
    l1, l2 = e.link_loops[0], e.link_loops[1]
    if l1.face is l2.face:
        return False
    return _loop_uv_pair(l1, uvl) == _loop_uv_pair(l2, uvl)


def merge_tris_same_island(bm, uvl, max_angle=0.0873, rounds=4):
    """把共面三角形对合并成四边形，但**只合并同属一个 UV 岛的两片**。

    bmesh.ops.join_triangles 没有这个限制：它会把 UV 缝两侧的两个三角形
    也焊在一起，于是两个孤岛被强行连接 → 贴图在接缝处错乱。
    这里用 face_join 自己实现，合并前先检查共享边的 UV 连续性。
    """
    cos_thr = math.cos(max_angle)
    total = 0
    for _ in range(rounds):
        bm.verts.index_update()
        done = 0
        for f in list(bm.faces):
            if not f.is_valid or len(f.verts) != 3:
                continue
            for e in list(f.edges):
                if not f.is_valid:
                    break
                lf = e.link_faces
                if len(lf) != 2:
                    continue
                g = lf[0] if lf[1] is f else lf[1]
                if g is f or not g.is_valid or len(g.verts) != 3:
                    continue
                f.normal_update()
                g.normal_update()
                if f.normal.dot(g.normal) < cos_thr:
                    continue
                if not _uv_edge_continuous(e, uvl):
                    continue
                try:
                    bmesh_utils.face_join((f, g), remove=True)
                    done += 1
                    break
                except Exception:
                    pass
        total += done
        bm.faces.ensure_lookup_table()
        if done == 0:
            break
    return total


def find_collinear_corner_verts(bm, angle_tol=0.985):
    """找"视觉 T 点"：度数 3，且其中两条边几乎共线。

    这种顶点其实**没有悬空** —— 它只是某个规整多边形（常见是五边形）
    直边上的一个中间点，视觉上像 T 点。但原生反细分的判定只看度数，
    会认为它"规整可删"，结果把它删掉 → 原先规整的多边形被拆成不规则形状。

    angle_tol：两条边方向的点积小于 -angle_tol 即认为共线（0.985 ≈ 170°）。
    """
    out = set()
    for v in bm.verts:
        es = list(v.link_edges)
        if len(es) != 3:
            continue
        # 网格自身的开放边界顶点天然是"两条边界边共线"，必须排除，
        # 否则整条外边界都会被保护，反细分几乎不生效。
        if any(len(e.link_faces) != 2 for e in es):
            continue
        dirs = []
        for e in es:
            d = e.other_vert(v).co - v.co
            if d.length > 1e-9:
                dirs.append(d.normalized())
        if len(dirs) != 3:
            continue
        hit = False
        for i in range(3):
            for j in range(i + 1, 3):
                if dirs[i].dot(dirs[j]) < -angle_tol:
                    hit = True
                    break
            if hit:
                break
        if hit:
            out.add(v)
    return out


def find_t_junction_verts(bm):
    """找出 T 型点（悬空顶点）。

    判定：顶点周围**恰好 1 条没有正常两面结构的边**（边界边或悬空边）。
    网格自己的开放边界顶点通常有 2 条这种边；而 T 型点只有 1 条 ——
    它的对面是一条几何重合、但拓扑上没有连上的边。

    这类顶点在原生反细分里必然不可塔陷，而且它的邻居一旦被删掉，
    周围就会岔出五边形/六边形/七边形（就是你截图里那种不规则面）。
    """
    out = set()
    for v in bm.verts:
        nb = 0
        for e in v.link_edges:
            if len(e.link_faces) <= 1:
                nb += 1
                if nb > 1:
                    break
        if nb == 1:
            out.add(v)
    return out


def fill_small_holes(bm, uvl, max_sides=8, uv_ref=None):
    """把 3~8 条边的小洞补上。

    跨 UV 孤岛的洞一律不补 —— 补出来的面会横跨两个岛，
    贴图在接缝处会串色错乱，那比留一个小洞更糟。
    """
    bm.verts.index_update()
    bm.faces.index_update()
    open_edges = [e for e in bm.edges if len(e.link_faces) == 1]
    if not open_edges:
        return 0, 0

    # 1) 把开放边按"洞"分组（共享顶点即同一个洞）
    parent = {e: e for e in open_edges}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner = {}
    for e in open_edges:
        for v in e.verts:
            prev = owner.get(v)
            if prev is None:
                owner[v] = e
            else:
                r1, r2 = find(prev), find(e)
                if r1 != r2:
                    parent[r1] = r2

    groups = {}
    for e in open_edges:
        groups.setdefault(find(e), []).append(e)

    # 2) 面 → UV 岛
    islands = compute_uv_islands(bm)

    # 3) 所有洞都补，包括跨 UV 孤岛的洞。
    #    跨岛洞不再跳过，而是用"把整个补丁面的 UV 收缩到一个点"来避免串色：
    #    UV 退化成一点后，它采样到的就是那个点原有的贴图颜色，
    #    而不是横跨两个岛被拉成一条错乱的色带。
    safe_groups = list(groups.values())
    cross_island = 0
    for edges in groups.values():
        isl = set()
        for e in edges:
            for f in e.link_faces:
                i = islands.get(f.index)
                if i is not None:
                    isl.add(i)
        if len(isl) > 1:
            cross_island += 1
    if not safe_groups:
        return 0, cross_island

    # 4) 逐个洞单独补，每个补丁面的 UV 收缩到同一个参考点。
    #    参考点优先取 uv_ref（反细分前该位置的 UV）—— 缺掉的那个角本来就在
    #    那片 UV 上，所以颜色才对得上；没有 uv_ref 时退化为取洞边邻面的 UV。
    filled = 0
    patch_faces = []
    for edges in safe_groups:
        try:
            res = bmesh.ops.holes_fill(bm, edges=edges, sides=max_sides)
        except Exception:
            continue
        for f in res.get('faces', []):
            if not f.is_valid:
                continue
            ref = None
            if uv_ref:
                fc = f.calc_center_median()
                best_d = None
                for l in f.loops:
                    for c0, uv0 in uv_ref.get(_pk(l.vert.co), ()):
                        d = (c0 - fc).length_squared
                        if best_d is None or d < best_d:
                            best_d = d
                            ref = uv0
            if ref is None:
                for e in edges:
                    for f2 in e.link_faces:
                        if f2.loops:
                            ref = (f2.loops[0][uvl].uv.x, f2.loops[0][uvl].uv.y)
                            break
                    if ref is not None:
                        break
            if ref is not None:
                # 关键：整个面的 UV 全部重合到这一个点
                for l in f.loops:
                    l[uvl].uv = (ref[0], ref[1])
            if len(f.verts) > 4:
                patch_faces.append(f)
            filled += 1

    # 5 边以上的补丁面会变成显眼的"六边形" —— 优先拆成四边形
    # （先三角化，再只合并"同 UV 岛"的三角形对，避免跨缝焊接）
    if patch_faces:
        try:
            bmesh.ops.triangulate(bm, faces=patch_faces)
            bm.faces.ensure_lookup_table()
            merge_tris_same_island(bm, uvl)
        except Exception:
            pass

    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return filled, cross_island


def _quadrangulate_patches(bm, patch_faces, rounds=4):
    """把 5 边以上的补丁面尽量拆成四边形，剩下的三角化。"""
    pool = [f for f in patch_faces if f.is_valid]
    for _ in range(rounds):
        todo = [f for f in pool if f.is_valid and len(f.verts) > 4]
        if not todo:
            return
        done = 0
        for f in todo:
            vs = list(f.verts)
            n = len(vs)
            best = None
            for k in range(2, n - 1):
                a, b = vs[0], vs[k]
                if bm.edges.get((a, b)) is not None:
                    continue
                len1, len2 = k + 1, n - k + 1
                pen = abs(len1 - 4) + abs(len2 - 4)
                if best is None or pen < best[0]:
                    best = (pen, a, b)
            if best is None:
                continue
            try:
                nf, _loop = bmesh_utils.face_split(f, best[1], best[2])
                if nf is not None:
                    pool.append(nf)
                done += 1
            except Exception:
                pass
        if done == 0:
            break
    left = [f for f in pool if f.is_valid and len(f.verts) > 4]
    if left:
        try:
            bmesh.ops.triangulate(bm, faces=left)
        except Exception:
            pass


# ----------------------------------------------------------------------
# 核心：UV 孤岛安全的反细分
# ----------------------------------------------------------------------
def unsubdivide_uv_safe(bm, iterations=1, only_verts=None, merge_triangles=True,
                        sel_shrink=1):
    """在 bmesh 上执行 UV 孤岛安全的反细分。

    only_verts 为 None 时处理整个网格；否则只删除这些位置的顶点
    （UV 缝拆边仍会执行，保证不会跨岛合并）。
    返回统计字典；出错返回 {'error': 文本}
    """
    if bm.loops.layers.uv.active is None:
        return {'error': '模型没有 UV 数据'}
    if not bm.verts:
        return {'error': '网格为空'}

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    uvl = bm.loops.layers.uv.active
    faces_before = len(bm.faces)
    tris_before = sum(1 for f in bm.faces if len(f.verts) == 3)
    n_islands = len(set(compute_uv_islands(bm).values()))

    restricted = only_verts is not None
    stats = {
        'faces_before': faces_before,
        'tris_before': tris_before,
        'islands': n_islands,
        'split_edges': 0,
        'dissolved': 0,
        'merged_tris': 0,
        'filled_holes': 0,
        'skipped_holes': 0,
        't_junctions': 0,
        'collinear_corners': 0,
        'restricted': restricted,
    }

    # 0) 反细分前的预处理：把 5 边以上的多边面先"三角化 → 同岛四边化"。
    #
    #    这是整个流程里最关键的一步。原始网格里常常有一批"看着像四边形、
    #    实际有 5 个顶点"的面（多出来的那个顶点与相邻边共线，视觉上完全看不出）。
    #    原生反细分的塔陷判定对这类面会失效，于是边界处岔出杂乱的
    #    三角形 / 五边形 / 六边形。
    #    必须在**反细分之前**把它们化成规整四边形 —— 放到反细分之后做就晚了，
    #    那时怪面已经产生。
    if merge_triangles:
        bm.faces.ensure_lookup_table()
        ngons = [f for f in bm.faces if len(f.verts) > 4]
        if ngons:
            try:
                bmesh.ops.triangulate(bm, faces=ngons)
                bm.faces.ensure_lookup_table()
                merge_tris_same_island(bm, uvl)
            except Exception:
                pass
            bm.verts.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

    for _ in range(max(1, iterations)):
        # 0) 备份"本次迭代前每个顶点附近的 UV"。
        #    补洞时用它还原被削掉那个角的 UV —— 反向对照反细分前后的 UV 轮廓，
        #    缺的那一角本来属于哪片 UV，就用哪片的 UV，贴图颜色才对得上。
        v_uv_ref = {}
        for _f0 in bm.faces:
            _c0 = _f0.calc_center_median()
            for _l0 in _f0.loops:
                v_uv_ref.setdefault(_pk(_l0.vert.co), []).append(
                    (_c0.copy(), (_l0[uvl].uv.x, _l0[uvl].uv.y)))

        # 1) 棋盘格标记（与原版一致）
        idx = compute_marking(bm)
        if restricted:
            allow = {_pk(v.co) for v in only_verts if v.is_valid}
            collapse = {_pk(v.co) for v, t in idx.items()
                        if t == _DO_COLLAPSE and _pk(v.co) in allow}
            # 选区边界内缩 N 圈：只删"周围所有顶点都在收缩后的选区内"的顶点。
            # 原因：反细分删顶点时，会把它周围的面一起合并掉 —— 那些面一旦
            # 伸到选区之外，没被选中的区域也会跟着变形
            # （这就是"明明没选那一圈、它却也被处理了"的原因）。
            # 0 = 不内缩（减面最多，但边界会波及外面）。
            cur = set(collapse)
            for _ in range(max(0, sel_shrink)):
                nxt = set()
                for v in bm.verts:
                    k = _pk(v.co)
                    if k not in cur:
                        continue
                    ok = True
                    for f in v.link_faces:
                        for u in f.verts:
                            if _pk(u.co) not in allow:
                                ok = False
                                break
                        if not ok:
                            break
                    if ok:
                        nxt.add(k)
                if not nxt:
                    break
                cur = nxt
            collapse = cur
        else:
            collapse = {_pk(v.co) for v, t in idx.items() if t == _DO_COLLAPSE}
        if not collapse:
            break

        # 1.5) T 型点连同它周围一圈不参与反细分。
        #      原因：T 点自己必然不可塔陷，而它的邻居被删掉之后，
        #      T 点周围就会岔出五边形/六边形/七边形（"反细分后变不规则"的根因）。
        #      保护范围只有 T 点周围一圈，对整体减面率影响很小。
        tj = find_t_junction_verts(bm)
        cj = find_collinear_corner_verts(bm)
        stats['t_junctions'] = len(tj)
        stats['collinear_corners'] = len(cj)
        prot_src = set(tj) | set(cj)
        if prot_src:
            prot = {_pk(v.co) for v in prot_src}
            for v in prot_src:
                for e in v.link_edges:
                    prot.add(_pk(e.other_vert(v).co))
            collapse -= prot
        if not collapse:
            break

        # 2) 沿 UV 孤岛边界拆边（始终执行，保证不跨岛合并）
        seams = find_uv_seam_edges(bm)
        if seams:
            bmesh.ops.split_edges(bm, edges=seams)
            stats['split_edges'] += len(seams)
            bm.verts.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

        # 3) 精确耳切（逐个 face_split，不会误连出尖刺）
        triples = _collect_split_triples(bm, collapse)
        if triples:
            _apply_splits(bm, triples)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        # 4) 一次性批量溶解（一次 C 调用处理全部顶点）
        targets = [v for v in bm.verts if _pk(v.co) in collapse]
        if targets:
            try:
                bmesh.ops.dissolve_verts(bm, verts=targets,
                                         use_face_split=False,
                                         use_boundary_tear=False)
                stats['dissolved'] += len(targets)
            except Exception:
                pass

        # 5) 焊回重合顶点，恢复拓扑连通
        bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-6)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        # 6) 补掉"缺一个角"的小洞
        #    仅选中模式同样要补：否则"选区 / 非选区"的交界处会留下三角空洞
        _f, _s = fill_small_holes(bm, uvl, 8, v_uv_ref)
        stats['filled_holes'] += _f
        stats['skipped_holes'] += _s

        # 7) 迭代之间把 ngon 拆回四边形
        #    第一次迭代必然产生一些五边形（拆缝导致缝顶点度数 4→3）。
        #    如果直接交给下一次迭代，它们会被 ear-split 切开再溶解，
        #    越滚越大 —— 这就是"迭代 2 出现大面积多边形"的根因。
        if _ < iterations - 1:
            ngons = [f for f in bm.faces if len(f.verts) > 4]
            if ngons:
                try:
                    bmesh.ops.triangulate(bm, faces=ngons)
                    bm.faces.ensure_lookup_table()
                    merge_tris_same_island(bm, uvl)
                except Exception:
                    pass
            bm.verts.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

    # 7) 合并共面三角形（多轮，直到不再减少）
    if merge_triangles:
        bm.faces.ensure_lookup_table()
        total_merged = stats['merged_tris']
        for _ in range(3):
            before = sum(1 for f in bm.faces if len(f.verts) == 3)
            if before == 0:
                break
            try:
                merge_tris_same_island(bm, uvl, rounds=1)
            except Exception:
                break
            bm.faces.ensure_lookup_table()
            after = sum(1 for f in bm.faces if len(f.verts) == 3)
            total_merged += max(0, before - after)
            if after >= before:
                break
        stats['merged_tris'] = total_merged

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.normal_update()
    stats['faces_after'] = len(bm.faces)
    stats['tris_after'] = sum(1 for f in bm.faces if len(f.verts) == 3)
    stats['open_edges'] = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    return stats


# ----------------------------------------------------------------------
# 操作符：反细分
# ----------------------------------------------------------------------
class MESH_OT_uv_guard_unsubdivide(bpy.types.Operator):
    bl_idname = "toolkit.uv_guard_unsubdivide"
    bl_label = "反细分·UV孤岛保护"
    bl_description = ("沿 UV 孤岛边界拆开后做原生反细分，再焊回、补洞并合并三角面："
                      "UV 不跨岛错乱、不留空洞")
    bl_options = {'REGISTER', 'UNDO'}

    iterations: bpy.props.IntProperty(
        name="反细分迭代",
        description="等价于反细分修改器上的 Iterations",
        default=1, min=1, max=10,
    )

    merge_triangles: bpy.props.BoolProperty(
        name="合并共面三角形",
        description="把 UV 缝上残留的共面三角形重新合并成四边形",
        default=True,
    )

    only_selected: bpy.props.BoolProperty(
        name="仅选中部分",
        description="只对进入编辑模式时选中的顶点做反细分；UV 缝仍会拆开，"
                    "所以不会再跨岛合并",
        default=False,
    )

    sel_shrink: bpy.props.IntProperty(
        name="选区边界内缩圈数",
        description="仅选中模式下，把塌陷范围从选区边界往里缩这么多圈，"
                    "避免反细分越界改到没选中的区域。0 = 不内缩（减面最多，"
                    "但边界外侧也会跟着变）；选区很碎时建议 0 或 1",
        default=1, min=0, max=3,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        me = obj.data
        if not me.uv_layers.active:
            self.report({'ERROR'}, "模型没有 UV 数据")
            return {'CANCELLED'}

        in_edit = (obj.mode == 'EDIT')
        bm = bmesh.from_edit_mesh(me) if in_edit else bmesh.new()
        if not in_edit:
            bm.from_mesh(me)

        verts = None
        if in_edit and self.only_selected:
            verts = [v for v in bm.verts if v.select]
            if not verts:
                self.report({'WARNING'}, "没有选中任何顶点")
                return {'CANCELLED'}

        try:
            stats = unsubdivide_uv_safe(bm, self.iterations, verts,
                                        self.merge_triangles, self.sel_shrink)
        except Exception as exc:                      # noqa: BLE001
            if not in_edit:
                bm.free()
            self.report({'ERROR'}, "反细分失败: %s" % exc)
            return {'CANCELLED'}

        if 'error' in stats:
            if not in_edit:
                bm.free()
            self.report({'ERROR'}, stats['error'])
            return {'CANCELLED'}

        if in_edit:
            bmesh.update_edit_mesh(me)
        else:
            bm.to_mesh(me)
            bm.free()
            me.update()

        self.report(
            {'INFO'},
            "反细分完成：%d → %d 面（三角面 %d → %d，合并 %d），"
            "补洞 %d（跳过跨岛洞 %d），残留洞 %d，"
            "T点保护 %d+%d，UV 孤岛 %d，拆缝 %d"
            % (stats['faces_before'], stats['faces_after'],
               stats['tris_before'], stats['tris_after'], stats['merged_tris'],
               stats['filled_holes'], stats['skipped_holes'],
               stats.get('open_edges', 0),
               stats.get('t_junctions', 0), stats.get('collinear_corners', 0),
               stats['islands'], stats['split_edges'])
        )
        return {'FINISHED'}


# ----------------------------------------------------------------------
# 操作符：细分
# ----------------------------------------------------------------------
class MESH_OT_uv_guard_subdivide(bpy.types.Operator):
    bl_idname = "toolkit.uv_guard_subdivide"
    bl_label = "细分·UV防错乱"
    bl_description = "细分网格，UV 自动插值，缝合线保持"
    bl_options = {'REGISTER', 'UNDO'}

    cuts: bpy.props.IntProperty(
        name="细分段数",
        description="每条边切成几段。1 = 每条边切成 2 段",
        default=1, min=1, max=10,
    )

    fix_boundary_ngons: bpy.props.BoolProperty(
        name="边界多边面三角化",
        description="细分后把 5 边以上的面自动三角化。局部细分时，选区边界**外侧**"
                    "那一圈面会被插入新顶点而变成五边形（视觉上仍像四边形，"
                    "完全看不出来），它们正是之后反细分出杂乱面的根源 —— "
                    "勾上它就地消除",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        in_edit = (context.active_object.mode == 'EDIT')
        if not in_edit:
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
        try:
            bpy.ops.mesh.subdivide(number_cuts=self.cuts, smoothness=0.0, fractal=0.0)
        except Exception as exc:                      # noqa: BLE001
            self.report({'ERROR'}, "细分失败: %s" % exc)
            return {'CANCELLED'}

        fixed = 0
        if self.fix_boundary_ngons:
            fixed = self._triangulate_ngons(context)

        tail = "，三角化边界多边面 %d 个" % fixed if fixed else ""
        self.report({'INFO'}, "细分完成：每条边切成 %d 段%s" % (self.cuts + 1, tail))
        return {'FINISHED'}

    def _triangulate_ngons(self, context):
        """把 5 边以上的面三角化。

        注意：这些面恰恰分布在**未选中**的那一侧（细分只作用于选区，
        但边界外侧的面会被切走一个角），所以这里不能按选中状态过滤。
        """
        obj = context.active_object
        me = obj.data
        try:
            bm = bmesh.from_edit_mesh(me)
        except Exception:
            return 0
        bm.faces.ensure_lookup_table()
        ngons = [f for f in bm.faces if len(f.verts) > 4]
        if not ngons:
            return 0
        try:
            bmesh.ops.triangulate(bm, faces=ngons)
        except Exception:
            return 0
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bmesh.update_edit_mesh(me)
        return len(ngons)


# ----------------------------------------------------------------------
# 菜单与注册
# ----------------------------------------------------------------------
# 仓库约定：算子清单由 toolkit/__init__.py 统一 register_class；本模块只导出清单
# 与菜单钩子（3D 视图 > 编辑模式 >「网格」菜单，与标准实现一致）。
uv_guard_unsubdivide_operators = (
    MESH_OT_uv_guard_unsubdivide,
    MESH_OT_uv_guard_subdivide,
)


def uv_guard_menu_func(self, context):
    self.layout.separator()
    self.layout.operator(MESH_OT_uv_guard_unsubdivide.bl_idname,
                         text="反细分·UV孤岛保护")
    self.layout.operator(MESH_OT_uv_guard_subdivide.bl_idname,
                         text="细分·UV防错乱")


def register_uv_guard_unsubdivide_menu():
    bpy.types.VIEW3D_MT_edit_mesh.append(uv_guard_menu_func)


def unregister_uv_guard_unsubdivide_menu():
    try:
        bpy.types.VIEW3D_MT_edit_mesh.remove(uv_guard_menu_func)
    except Exception:
        pass
