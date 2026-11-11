"""Bind geometry for the opt-in surface garment solver (no Blender dependency)."""

from __future__ import annotations

import heapq
from collections import defaultdict

import numpy as np

INVALID = np.uint32(0xFFFFFFFF)


def _unit(v, fallback=None):
    length = np.linalg.norm(v)
    if length > 1e-12:
        return v / length
    if fallback is None:
        raise ValueError("上下拖拽：衣物几何退化，无法确定方向")
    return np.asarray(fallback, dtype=float)


def _edges(faces):
    edges = defaultdict(list)
    for index, (a, b, c) in enumerate(faces.tolist()):
        for u, v, opposite in ((a, b, c), (b, c, a), (c, a, b)):
            edges[tuple(sorted((u, v)))].append((index, opposite))
    return edges


def _components(faces, count):
    parent = np.arange(count)

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = int(parent[i])
        return i

    for a, b, c in faces:
        a = root(int(a))
        for v in (b, c):
            b = root(int(v))
            if a != b:
                parent[max(a, b)] = min(a, b)
                a = min(a, b)
    labels = np.array([root(i) for i in range(count)])
    return [np.flatnonzero(labels == label) for label in np.unique(labels)]


def _lower_centers(points, faces, up):
    height = points @ up
    lower = set(np.flatnonzero(height <= height.min() + np.ptp(height) * 0.2).tolist())
    graph = {i: set() for i in lower}
    for a, b in _edges(faces):
        if a in lower and b in lower:
            graph[a].add(b)
            graph[b].add(a)
    centers = []
    while lower:
        stack = [min(lower)]
        island = set()
        while stack:
            i = stack.pop()
            if i in island:
                continue
            island.add(i)
            stack.extend(graph[i] - island)
        lower -= island
        if len(island) >= 3:
            centers.append(points[list(island)].mean(0))
    return centers


def _closest(p, triangles):
    """Exact closest points on a small triangle batch, with barycentrics."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    ab, ac, bc = b - a, c - a, c - b

    def segment(start, edge, first, second):
        t = np.clip(
            np.sum((p - start) * edge, axis=1)
            / np.maximum(np.sum(edge * edge, axis=1), 1e-24),
            0,
            1,
        )
        bary = np.zeros((len(t), 3))
        bary[:, first] = 1 - t
        bary[:, second] = t
        return start + edge * t[:, None], bary

    candidates = [segment(a, ab, 0, 1), segment(a, ac, 0, 2), segment(b, bc, 1, 2)]
    aa, bb, cc = np.sum(ab * ab, 1), np.sum(ab * ac, 1), np.sum(ac * ac, 1)
    den = aa * cc - bb * bb
    ap = p - a
    u = np.sum(ap * ab, 1)
    v = np.sum(ap * ac, 1)
    y = (cc * u - bb * v) / np.maximum(den, 1e-24)
    z = (aa * v - bb * u) / np.maximum(den, 1e-24)
    inside = (den > 1e-24) & (y >= 0) & (z >= 0) & (y + z <= 1)
    projected = a + ab * y[:, None] + ac * z[:, None]
    projected[~inside] = np.inf
    candidates.append((projected, np.c_[1 - y - z, y, z]))
    distances = np.stack([np.sum((q - p) ** 2, 1) for q, _ in candidates])
    which = np.argmin(distances, axis=0)
    rows = np.arange(len(triangles))
    q = np.stack([item[0] for item in candidates])[which, rows]
    bary = np.stack([item[1] for item in candidates])[which, rows]
    return q, bary, distances[which, rows]


class _BVH:
    def __init__(self, points, faces):
        self.triangles = np.asarray(points, float)[faces]
        self.lo = self.triangles.min(1)
        self.hi = self.triangles.max(1)
        self.nodes = []

        def build(ids):
            index = len(self.nodes)
            lo = self.lo[ids].min(0)
            hi = self.hi[ids].max(0)
            self.nodes.append((lo, hi, None, None, None))
            if len(ids) <= 8:
                self.nodes[index] = (lo, hi, ids, None, None)
            else:
                axis = int(np.argmax(hi - lo))
                order = np.argsort(
                    (self.lo[ids, axis] + self.hi[ids, axis]), kind="stable"
                )
                half = len(ids) // 2
                left = build(ids[order[:half]])
                right = build(ids[order[half:]])
                self.nodes[index] = (lo, hi, None, left, right)
            return index

        build(np.arange(len(faces)))

    def nearest(self, p):
        best = np.inf
        result = None
        queue = [(0.0, 0)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance > best:
                continue
            _lo, _hi, ids, left, right = self.nodes[node]
            if ids is not None:
                q, bary, distances = _closest(p, self.triangles[ids])
                at = int(np.argmin(distances))
                if distances[at] < best:
                    best = float(distances[at])
                    result = (int(ids[at]), bary[at], q[at])
            else:
                for child in (left, right):
                    a, b = self.nodes[child][:2]
                    d = np.maximum(np.maximum(a - p, p - b), 0)
                    heapq.heappush(queue, (float(d @ d), child))
        return result

    def winding(self, p, direction, orientation):
        stack = [0]
        value = 0
        inverse = 1 / direction
        while stack:
            lo, hi, ids, left, right = self.nodes[stack.pop()]
            x, y = (lo - p) * inverse, (hi - p) * inverse
            if max(float(np.minimum(x, y).max()), 0) > float(np.maximum(x, y).min()):
                continue
            if ids is None:
                stack.extend((left, right))
                continue
            tri = self.triangles[ids]
            a = tri[:, 0]
            ab = tri[:, 1] - a
            ac = tri[:, 2] - a
            h = np.cross(direction, ac)
            den = np.sum(ab * h, 1)
            valid = abs(den) > 1e-15
            inv = np.divide(1, den, out=np.zeros_like(den), where=valid)
            v = p - a
            q = np.cross(v, ab)
            u = np.sum(v * h, 1) * inv
            w = (q @ direction) * inv
            t = np.sum(ac * q, 1) * inv
            valid &= (u >= 0) & (w >= 0) & (u + w <= 1) & (t > 1e-7)
            sign = np.where(np.cross(ab, ac) @ direction > 0, 1, -1)
            value += int(np.sum(sign[valid] * orientation[ids[valid]]))
        return value


def _surface_mask(points, faces, normals):
    tri = points[faces]
    area = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    orientation = np.where(np.sum(area * normals[faces].sum(1), 1) < 0, -1, 1)
    n = area * orientation[:, None]
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-20)
    epsilon = max(float(np.linalg.norm(np.ptp(points, axis=0))) * 1e-5, 1e-7)
    bvh = _BVH(points, faces)
    direction = _unit(np.array([0.173, 1, 0.379]))
    return np.array(
        [
            bvh.winding(p + normal * epsilon, direction, orientation) <= 0
            for p, normal in zip(tri.mean(1), n)
        ],
        dtype=np.uint32,
    )


def _simplify(points, faces, representatives, budget):
    """Topology-preserving endpoint QEM; representatives remain original rows."""
    if len(points) <= budget:
        return points, faces, representatives
    points = points.copy()
    faces = faces.copy()
    alive = np.ones(len(points), bool)
    active = np.ones(len(faces), bool)
    incident = [set() for _ in points]
    for i, face in enumerate(faces):
        for v in face:
            incident[v].add(i)
    edge_map = _edges(faces)
    boundary = np.zeros(len(points), bool)
    for (a, b), entries in edge_map.items():
        if len(entries) == 1:
            boundary[[a, b]] = True
    tri = points[faces]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-20)
    planes = np.c_[n, -np.sum(n * tri[:, 0], 1)]
    q = np.zeros((len(points), 4, 4))
    for k in range(3):
        np.add.at(q, faces[:, k], planes[:, :, None] * planes[:, None, :])
    version = np.zeros(len(points), int)
    queue = []

    def neighbors(v):
        result = set()
        for f in incident[v]:
            if active[f]:
                result.update(faces[f])
        result.discard(v)
        return result

    def enqueue(a, b):
        if a > b:
            a, b = b, a
        if not alive[a] or not alive[b] or boundary[a] != boundary[b]:
            return
        quadric = q[a] + q[b]
        pa = np.r_[points[a], 1]
        pb = np.r_[points[b], 1]
        ca = float(pa @ quadric @ pa)
        cb = float(pb @ quadric @ pb)
        keep, remove = (a, b) if ca <= cb else (b, a)
        cost = min(ca, cb) + float(np.sum((points[a] - points[b]) ** 2)) * 0.05
        heapq.heappush(
            queue, (cost, keep, remove, int(version[keep]), int(version[remove]))
        )

    for a, b in edge_map:
        enqueue(a, b)
    count = len(points)
    while count > budget and queue:
        _, a, b, va, vb = heapq.heappop(queue)
        if not alive[a] or not alive[b] or va != version[a] or vb != version[b]:
            continue
        shared = incident[a] & incident[b]
        opposite = set()
        for f in shared:
            if active[f]:
                opposite.update(int(v) for v in faces[f] if v not in (a, b))
        if (neighbors(a) & neighbors(b)) != opposite:
            continue
        if boundary[a] and len(shared) != 1:
            continue
        affected = incident[a] | incident[b]
        bad = False
        for f in affected - shared:
            old = faces[f]
            new = np.where(old == b, a, old)
            x = points[old]
            y = points[new]
            n0 = np.cross(x[1] - x[0], x[2] - x[0])
            n1 = np.cross(y[1] - y[0], y[2] - y[0])
            if n0 @ n1 < 0.15 * np.linalg.norm(n0) * np.linalg.norm(n1):
                bad = True
                break
        if bad:
            continue
        touched = neighbors(a) | neighbors(b) | {a, b}
        for f in list(affected):
            for v in faces[f]:
                incident[v].discard(f)
            faces[f] = np.where(faces[f] == b, a, faces[f])
            if len(set(faces[f])) < 3:
                active[f] = False
            else:
                for v in faces[f]:
                    incident[v].add(f)
        q[a] += q[b]
        alive[b] = False
        count -= 1
        for v in touched:
            version[v] += 1
        for v in touched:
            if alive[v]:
                for other in neighbors(v):
                    enqueue(v, int(other))
    if count > budget:
        raise ValueError(
            f"上下拖拽：代理无法保持拓扑并降至 {budget} 点（剩余 {count}），请简化衣物网格"
        )
    ids = np.flatnonzero(alive)
    remap = np.full(len(points), -1, int)
    remap[ids] = np.arange(len(ids))
    return points[ids], remap[faces[active]], representatives[ids]


def _colors(items, count):
    used = [set() for _ in range(count)]
    groups = []
    for index, vertices in enumerate(items):
        occupied = set().union(*(used[int(v)] for v in vertices))
        color = 0
        while color in occupied:
            color += 1
        while len(groups) <= color:
            groups.append([])
        groups[color].append(index)
        for v in vertices:
            used[int(v)].add(color)
    order = np.array([v for group in groups for v in group], np.uint32)
    return order, np.cumsum([0] + [len(group) for group in groups]).astype(np.uint32)


def _hem(faces, points, up, side, forward):
    boundary = [edge for edge, entries in _edges(faces).items() if len(entries) == 1]
    if not boundary:
        raise ValueError("上下拖拽：未识别到衣物开口，请保留袜口或裤腰边界")
    graph = defaultdict(set)
    for a, b in boundary:
        graph[a].add(b)
        graph[b].add(a)
    loops = []
    remaining = set(graph)
    while remaining:
        stack = [min(remaining)]
        group = set()
        while stack:
            v = stack.pop()
            if v in group:
                continue
            group.add(v)
            stack.extend(graph[v] - group)
        remaining -= group
        loops.append(np.array(sorted(group), int))
    top = max(float((points[ids] @ up).mean()) for ids in loops)
    span = float(np.ptp(points @ up))
    candidates = [
        ids for ids in loops if float((points[ids] @ up).mean()) >= top - span * 0.2
    ]

    def perimeter(ids):
        selected = set(ids.tolist())
        return sum(
            float(np.linalg.norm(points[a] - points[b]))
            for a, b in boundary
            if a in selected and b in selected
        )

    ids = max(candidates, key=perimeter)
    center = points[ids].mean(0)
    xy = np.c_[(points[ids] - center) @ side, (points[ids] - center) @ forward]
    angle = np.arctan2(xy[:, 1], xy[:, 0])
    order = np.argsort(angle, kind="stable")
    ids = ids[order]
    # Very dense cuffs do not need one pin per render vertex.
    if len(ids) > 128:
        ids = ids[np.linspace(0, len(ids) - 1, 128).astype(int)]
    if len(ids) < 3:
        raise ValueError("上下拖拽：衣物开口不足三个有效顶点")
    cuff_width = max(
        float(np.ptp(points[ids] @ side)), float(np.ptp(points[ids] @ forward))
    )
    cloth_width = max(float(np.ptp(points @ side)), float(np.ptp(points @ forward)))
    if cuff_width < cloth_width * 0.2:
        raise ValueError("上下拖拽：只找到小孔或裂缝，未识别到有效袜口/裤腰")
    return ids


def bake_garments(
    positions,
    normals,
    cloth_faces,
    body_faces,
    runtime_count=None,
    vertex_base=0,
    budget=2450,
    up_axis="AUTO",
    exterior=True,
):
    """Return one independently controlled runtime per welded cloth component."""
    positions = np.asarray(positions, float)
    normals = np.asarray(normals, float)
    cloth_faces = np.asarray(cloth_faces, np.int64).reshape(-1, 3)
    body_faces = np.asarray(body_faces, np.int64).reshape(-1, 3)
    if not len(cloth_faces) or not len(body_faces):
        raise ValueError("上下拖拽：衣物和碰撞体都必须包含三角面")
    if (
        positions.ndim != 2
        or positions.shape[1] != 3
        or normals.shape != positions.shape
    ):
        raise ValueError("上下拖拽：位置与法线必须是相同大小的Nx3数组")
    if min(int(cloth_faces.min()), int(body_faces.min())) < 0 or max(
        int(cloth_faces.max()), int(body_faces.max())
    ) >= len(positions):
        raise ValueError("上下拖拽：三角形引用了位置缓冲之外的顶点")
    if not np.isfinite(positions).all():
        raise ValueError("上下拖拽：模型包含非有限坐标")
    if not np.isfinite(
        normals[np.unique(np.r_[cloth_faces.ravel(), body_faces.ravel()])]
    ).all():
        raise ValueError("上下拖拽：衣物或身体法线包含非有限数据")
    body_points = positions[body_faces]
    if not np.any(
        np.linalg.norm(
            np.cross(
                body_points[:, 1] - body_points[:, 0],
                body_points[:, 2] - body_points[:, 0],
            ),
            axis=1,
        )
        > 1e-12
    ):
        raise ValueError("上下拖拽：身体碰撞体没有有效面积的三角面")
    if np.any(np.isin(np.unique(cloth_faces), np.unique(body_faces))):
        raise ValueError("上下拖拽：衣物与碰撞体不能使用相同顶点")
    vertex_base = int(vertex_base)
    runtime_count = int(runtime_count or len(positions) + vertex_base)
    if vertex_base < 0 or runtime_count < vertex_base + len(positions):
        raise ValueError("上下拖拽：运行时顶点容量无法容纳绑定缓冲与前缀")
    rows = np.unique(cloth_faces)
    epsilon = max(float(np.linalg.norm(np.ptp(positions[rows], axis=0))) * 1e-7, 1e-6)
    _, first, weld = np.unique(
        np.rint(positions[rows] / epsilon).astype(np.int64),
        axis=0,
        return_index=True,
        return_inverse=True,
    )
    representatives = rows[first]
    points = positions[representatives]
    faces = weld[np.searchsorted(rows, cloth_faces)]
    keep = np.all(np.diff(np.sort(faces, axis=1), axis=1) != 0, axis=1)
    faces = faces[keep]
    _, indices = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
    faces = faces[np.sort(indices)]
    _, _, axes = np.linalg.svd(points - points.mean(0), full_matrices=False)
    up = axes[0]
    if up_axis != "AUTO":
        up = np.zeros(3)
        up["XYZ".index(up_axis[-1])] = -1 if up_axis.startswith("-") else 1
    if (
        up_axis == "AUTO"
        and (up[2] if abs(up[2]) > 0.2 else up[np.argmax(abs(up))]) < 0
    ):
        up = -up
    global_height = float(np.ptp(points @ up))
    foot_points = points[(points @ up) <= np.quantile(points @ up, 0.08)]
    body_rows = np.unique(body_faces)
    _, body_first, body_weld = np.unique(
        np.rint(positions[body_rows] / epsilon).astype(np.int64),
        axis=0,
        return_index=True,
        return_inverse=True,
    )
    body_topology = body_weld[np.searchsorted(body_rows, body_faces)]
    body_feet = _lower_centers(positions[body_rows[body_first]], body_topology, up)
    cloth_feet = _lower_centers(points, faces, up)
    candidates = cloth_feet if len(cloth_feet) >= 2 else body_feet
    side = np.array([1.0, 0, 0])
    side -= up * (side @ up)
    if np.linalg.norm(side) < 0.2:
        side = axes[1] - up * (axes[1] @ up)
    if len(candidates) >= 2:
        pairs = [
            (np.linalg.norm((a - b) - up * ((a - b) @ up)), a - b)
            for i, a in enumerate(candidates)
            for b in candidates[i + 1 :]
        ]
        distance, direction = max(pairs, key=lambda pair: pair[0])
        if distance > global_height * 0.03:
            side = direction - up * (direction @ up)
    side = _unit(side)
    if side[np.argmax(abs(side))] < 0:
        side = -side
    forward = _unit(np.cross(up, side))
    bottom = foot_points.mean(0)
    body_foot_sides = [float(p @ side) for p in body_feet]
    centerline = (
        (min(body_foot_sides) + max(body_foot_sides)) * 0.5
        if len(body_foot_sides) >= 2
        else float(np.median(positions[body_rows] @ side))
    )
    bottom += side * (centerline - bottom @ side)
    body = positions[body_faces]
    body_center = body.mean(1) - bottom
    bh = body_center @ up / max(global_height, 1e-8)
    bs = (body - bottom) @ side / max(global_height, 1e-8)
    valid = (bh > -0.1) & (bh < 1.08) & (np.abs(bs.mean(1)) < 0.25)
    owners = np.where(
        valid,
        np.where(np.all(bs < 0, axis=1), 1, np.where(np.all(bs >= 0, axis=1), 2, 3)),
        0,
    ).astype(np.uint32)
    surface = (
        _surface_mask(positions, body_faces, normals)
        if exterior
        else np.ones(len(body_faces), np.uint32)
    )
    # Weld body seams before constructing adjacency; preserve normal-compatible sheets.
    adj = np.full((len(body_faces), 3), INVALID, np.uint32)
    edge_map = defaultdict(list)
    n = np.cross(body[:, 1] - body[:, 0], body[:, 2] - body[:, 0])
    n[np.sum(n * normals[body_faces].sum(1), 1) < 0] *= -1
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-20)
    for i, face in enumerate(body_topology):
        for j in range(3):
            edge_map[tuple(sorted((face[(j + 1) % 3], face[(j + 2) % 3])))].append(
                (i, j)
            )
    for linked in edge_map.values():
        for face, slot in linked:
            choices = [other for other, _ in linked if other != face]
            if choices:
                adj[face, slot] = max(
                    choices, key=lambda other: float(n[face] @ n[other])
                )
    result = []
    for component in _components(faces, len(points)):
        if len(component) < 3:
            continue
        mask = np.zeros(len(points), bool)
        mask[component] = True
        cf = faces[np.all(mask[faces], 1)]
        local = np.full(len(points), -1, int)
        local[component] = np.arange(len(component))
        cf = local[cf]
        p, f, reps = _simplify(
            points[component],
            cf,
            representatives[component],
            min(2450, max(32, int(budget))),
        )
        cuff = _hem(f, p, up, side, forward)
        top = p[cuff].mean(0)
        low = p[(p @ up) <= np.quantile(p @ up, 0.08)].mean(0)
        height = float((top - low) @ up)
        if height <= 1e-6:
            raise ValueError("上下拖拽：衣物开口不是长轴上端，请调整拖拽轴")
        hn = (p - low) @ up / height
        lat = (p - bottom) @ side
        rest = np.zeros((len(p) * 2, 4), np.float32)
        rest[::2, :3] = p
        rest[::2, 3] = hn >= 0.065
        rest[1::2, 2] = hn
        rest[1::2, 3] = lat
        rest[cuff * 2 + 1, 0] = 1
        hem_map = np.full(len(p), INVALID, np.uint32)
        hem_map[cuff] = np.arange(len(cuff))
        xy = np.c_[(p[cuff] - top) @ side, (p[cuff] - top) @ forward]
        xy /= np.maximum(np.linalg.norm(xy, axis=1, keepdims=True), 1e-12)
        hem_params = np.c_[xy, (p[cuff] - top) @ up, cuff].astype(np.float32)
        previous = xy + np.roll(xy, 1, axis=0)
        following = xy + np.roll(xy, -1, axis=0)
        previous /= np.maximum(np.linalg.norm(previous, axis=1, keepdims=True), 1e-12)
        following /= np.maximum(np.linalg.norm(following, axis=1, keepdims=True), 1e-12)
        lines = np.c_[previous, following].astype(np.float32)
        if np.any(abs(lines[:, 0] * lines[:, 3] - lines[:, 1] * lines[:, 2]) < 1e-6):
            raise ValueError("上下拖拽：袜口方向退化，请检查重叠边界")
        mesh_edges = _edges(f)
        constraints = []
        for a, b in mesh_edges:
            constraints.append((a, b, float(np.linalg.norm(p[a] - p[b])), 0))
        for linked in mesh_edges.values():
            opposite = [entry[1] for entry in linked]
            if (
                len(opposite) == 2
                and opposite[0] != opposite[1]
                and tuple(sorted(opposite)) not in mesh_edges
            ):
                a, b = opposite
                constraints.append((a, b, float(np.linalg.norm(p[a] - p[b])), 1))
        edges = np.array(constraints, np.float32)
        edge_order, edge_offsets = _colors(edges[:, :2].astype(int), len(p))
        edges = edges[edge_order]
        edges[:, :2] = edges[:, :2].astype(np.uint32).view(np.float32)
        face_order, face_offsets = _colors(f, len(p))
        counts = np.bincount(f.ravel(), minlength=len(p))
        offsets = np.r_[0, np.cumsum(counts)].astype(np.uint32)
        refs = np.repeat(np.arange(len(f), dtype=np.uint32), 3)[
            np.argsort(f.ravel(), kind="stable")
        ]
        face_adj = np.r_[offsets, refs].astype(np.uint32)
        owned_rows = rows[np.isin(weld, component)]
        display = np.full((runtime_count, 3), INVALID, np.uint32)
        weights = np.zeros((runtime_count, 4), np.float32)
        mapping = np.full(runtime_count, INVALID, np.uint32)
        bvh = None
        if len(component) == len(p):
            old_to_new = {int(old): i for i, old in enumerate(component)}
            for row in owned_rows:
                particle = old_to_new[int(weld[np.searchsorted(rows, row)])]
                display[row + vertex_base] = particle
                weights[row + vertex_base, 0] = 1
                mapping[row + vertex_base] = particle
        else:
            bvh = _BVH(p, f)
            for row in owned_rows:
                face, bary, _ = bvh.nearest(positions[row])
                ids = f[face]
                display[row + vertex_base] = ids
                weights[row + vertex_base, :3] = bary
                mapping[row + vertex_base] = ids[int(np.argmax(bary))]
        weights[owned_rows + vertex_base, 3] = (
            (positions[owned_rows] - low) @ up / height
        )
        lower = set(np.flatnonzero(hn < 0.2).tolist())
        lower_graph = {i: set() for i in lower}
        for a, b in mesh_edges:
            if a in lower and b in lower:
                lower_graph[a].add(b)
                lower_graph[b].add(a)
        foot_centers = []
        while lower:
            stack = [min(lower)]
            island = set()
            while stack:
                i = stack.pop()
                if i in island:
                    continue
                island.add(i)
                stack.extend(lower_graph[i] - island)
            lower -= island
            if len(island) >= 3:
                foot_centers.append(float(lat[list(island)].mean()))
        paired_feet = (
            len(foot_centers) >= 2
            and min(foot_centers) < 0 < max(foot_centers)
            and max(foot_centers) - min(foot_centers) > 0.03 * height
        )
        single = not paired_feet
        middle = (hn > 0.4) & (hn < 0.75)
        center_side = float(lat[middle].mean()) if middle.any() else float(lat.mean())
        center_forward = (
            float(((p - low) @ forward)[middle].mean()) if middle.any() else 0
        )
        owner = 0
        if single and len(body_foot_sides) >= 2:
            left = min(body_foot_sides) - bottom @ side
            right = max(body_foot_sides) - bottom @ side
            center = float(lat[hn < 0.2].mean()) if np.any(hn < 0.2) else center_side
            if abs(center - left) < (right - left) * 0.4:
                owner = 1
            elif abs(center - right) < (right - left) * 0.4:
                owner = 2
        config = np.zeros((16, 4), np.float32)
        config[0] = [len(p), runtime_count, len(f), 1]
        config[1] = [
            len(edges),
            len(edge_offsets) - 1,
            len(face_offsets) - 1,
            len(cuff),
        ]
        config[2] = [-0.6, 0.85, 0.001, 0.0012]
        config[3] = [3, 3, 0.00003, 0.0008]
        config[4] = [0.22, 0.4, 0.016, 0.002]
        config[5] = [4096, 0.004, 0.2, 1.8]
        config[6] = [*up, height]
        config[7] = [*side, 0]
        config[8] = [*low, 0]
        config[9] = [*forward, 0]
        config[10] = [0, 1, 0.02, 0]
        config[11] = [1, 0.002, 0.04, 0]
        config[12] = [
            center_side if single else 0,
            center_forward if single else 0,
            int(single),
            owner,
        ]
        result.append(
            {
                "rest": rest,
                "config": config,
                "map": mapping,
                "representatives": (reps + vertex_base).astype(np.uint32),
                "faces": f.astype(np.uint32),
                "face_adjacency": face_adj,
                "edges": edges,
                "edge_offsets": edge_offsets,
                "face_colors": face_order,
                "face_offsets": face_offsets,
                "hem_map": hem_map,
                "hem_params": hem_params,
                "hem_lines": lines,
                "body_adjacency": adj.ravel(),
                "surface_mask": surface,
                "body_regions": owners,
                "display_triangles": display.ravel(),
                "display_weights": weights,
            }
        )
    if not result:
        raise ValueError("上下拖拽：没有有效衣物连通部分")
    return result
