"""Bind geometry for the opt-in surface garment solver (no Blender dependency)."""

from __future__ import annotations

import heapq
from collections import defaultdict

import numpy as np

INVALID = np.uint32(0xFFFFFFFF)


def shorts_skin_mix(points, faces, lateral, height):
    """Continuous leg ownership across the garment's connected centre seam."""
    left = np.asarray(lateral) < 0
    edges = np.asarray(list(_edges(faces)), dtype=np.int64).reshape(-1, 2)
    seam = np.unique(edges[left[edges[:, 0]] != left[edges[:, 1]]])
    if not len(seam):
        return left.astype(np.float32)
    distance = opening_distances(points, faces, [seam])
    width = max(float(height) * 0.15, 1e-6)
    t = np.clip(0.5 + np.where(left, 1, -1) * distance / (2 * width), 0, 1)
    return (t * t * (3 - 2 * t)).astype(np.float32)


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
    def __init__(self, points, faces, normals=None):
        self.triangles = np.asarray(points, float)[faces]
        self.normals = normals
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

    def nearest(self, p, normal=None, max_distance=None):
        best = np.inf if max_distance is None else max_distance * max_distance
        result = None
        queue = [(0.0, 0)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance > best:
                continue
            _lo, _hi, ids, left, right = self.nodes[node]
            if ids is not None:
                if normal is not None and self.normals is not None:
                    ids = ids[self.normals[ids] @ normal > 0.1]
                    if not len(ids):
                        continue
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


def _boundary_loops(faces):
    boundary = [edge for edge, entries in _edges(faces).items() if len(entries) == 1]
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
    return boundary, loops


def _hem(faces, points, up, side, forward, *, lower=False, budget=128, min_width=0.2):
    boundary, loops = _boundary_loops(faces)
    if not loops:
        raise ValueError("上下拖拽：未识别到衣物开口，请保留袜口或裤腰边界")
    direction = -up if lower else up
    top = max(float((points[ids] @ direction).mean()) for ids in loops)
    span = float(np.ptp(points @ up))
    candidates = [
        ids
        for ids in loops
        if float((points[ids] @ direction).mean()) >= top - span * 0.2
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
    if len(ids) > budget:
        ids = ids[np.linspace(0, len(ids) - 1, budget).astype(int)]
    if len(ids) < 3:
        raise ValueError("上下拖拽：衣物开口不足三个有效顶点")
    cuff_width = max(
        float(np.ptp(points[ids] @ side)), float(np.ptp(points[ids] @ forward))
    )
    cloth_width = max(float(np.ptp(points @ side)), float(np.ptp(points @ forward)))
    if cuff_width < cloth_width * min_width:
        raise ValueError("上下拖拽：只找到小孔或裂缝，未识别到有效袜口/裤腰")
    return ids


def bake_body_seeds(
    positions,
    normals,
    body_faces,
    representatives,
    config,
    owners,
    surface,
    groups=None,
):
    """Initial adhesion on the bind surface; runtime follows its adjacency."""
    p = np.asarray(positions, float)
    n = np.asarray(normals, float)
    normal = n[body_faces].sum(1)
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
    owner = int(config[12, 3])
    manual = config[15, 3] > 0.5 and not 1.5 < config[14, 3] < 2.5
    if groups is None:
        groups = np.full(len(representatives), owner or (3 if manual else 0), int)
        if not owner and not manual:
            side = (p[representatives] - config[8, :3]) @ config[7, :3]
            groups = np.where(
                abs(side) < config[6, 3] * 0.004, 3, np.where(side < 0, 1, 2)
            )
    seeds = np.empty(len(representatives), np.uint32)
    for group in np.unique(groups):
        mask = np.asarray(surface) > 0
        if group == 3 and 1.5 < config[14, 3] < 2.5:
            mask &= owners > 0
        elif group in (4, 5):
            # Shoulder transition points may originally touch the torso.
            # Choose that reference surface once rather than forcing them
            # onto an arm through the sleeve's control-field classification.
            mask &= (owners == group) | (owners == 3)
        elif group == 3 and not manual:
            pass
        elif group:
            mask &= owners == group
        ids = np.flatnonzero(mask)
        if not len(ids):
            raise ValueError("衣物没有匹配的初始身体贴合面")
        tree = _BVH(p, body_faces[ids], normal[ids])
        for at in np.flatnonzero(groups == group):
            row = representatives[at]
            closest = tree.nearest(p[row])
            limit = max(np.linalg.norm(closest[2] - p[row]) * 1.5, 0.01)
            compatible = tree.nearest(p[row], n[row], max_distance=limit)
            hit = (
                compatible
                if compatible is not None
                and np.linalg.norm(compatible[2] - p[row])
                <= max(np.linalg.norm(closest[2] - p[row]) * 1.5, 0.01)
                else closest
            )
            seeds[at] = ids[hit[0]]
    return seeds


def body_channels(positions, body_faces, config, owners, samples=256, levels=None):
    """Reference cross-section centres, never precomputed garment poses."""
    up = config[6, :3]
    height = config[6, 3]
    triangles = positions[body_faces]
    h = (
        (triangles - config[8, :3]) @ up / height
        if levels is None
        else levels[body_faces]
    )
    lo, hi = float(h.min()), float(h.max())
    sections = np.linspace(lo, hi, samples)
    result = np.zeros((1 + samples * 3, 4), np.float32)
    result[0] = [lo, hi, samples, 1]
    side, forward = config[7, :3], config[9, :3]
    for group in (1, 2, 3):
        selected = owners == group
        t, heights = triangles[selected], h[selected]
        centers = np.full((samples, 3), np.nan)
        for i, level in enumerate(sections):
            active = (heights.min(1) <= level) & (heights.max(1) >= level)
            if not active.any():
                continue
            q, z = t[active], heights[active]
            cuts = []
            for a, b in ((0, 1), (1, 2), (2, 0)):
                valid = ((z[:, a] - level) * (z[:, b] - level) <= 0) & (
                    abs(z[:, a] - z[:, b]) > 1e-9
                )
                fraction = (level - z[valid, a]) / (z[valid, b] - z[valid, a])
                cuts.extend(
                    q[valid, a] + (q[valid, b] - q[valid, a]) * fraction[:, None]
                )
            if not cuts:
                continue
            cuts = np.asarray(cuts)
            x, y = cuts @ side, cuts @ forward
            centers[i] = (
                (cuts.min(0) + cuts.max(0)) * 0.5
                if levels is not None
                else (
                    side * (x.min() + x.max()) * 0.5
                    + forward * (y.min() + y.max()) * 0.5
                    + up * (level * height + config[8, :3] @ up)
                )
            )
        valid = np.flatnonzero(np.isfinite(centers[:, 0]))
        if len(valid):
            for axis in range(3):
                centers[:, axis] = np.interp(
                    np.arange(samples), valid, centers[valid, axis]
                )
            result[1 + (group - 1) * samples : 1 + group * samples, :3] = centers
    return result


def body_channel_center(channels, group, level):
    lo, hi, samples, _ = channels[0]
    samples = int(samples)
    at = float(np.clip((level - lo) / max(hi - lo, 1e-8), 0, 1) * (samples - 1))
    index = min(int(at), samples - 2)
    start = 1 + (int(group) - 1) * samples
    return channels[start + index, :3] * (1 - (at - index)) + channels[
        start + index + 1, :3
    ] * (at - index)


def body_directions(
    positions, normals, body_faces, reps, config, owners, seeds, channels=None
):
    if channels is None:
        channels = body_channels(positions, body_faces, config, owners)
    up = config[6, :3]
    height = config[6, 3]
    out = np.zeros((len(reps), 4), np.float32)
    for i, row in enumerate(reps):
        group = max(int(owners[seeds[i]]), 1)
        if group > 3:
            out[i, :3] = _unit(normals[row], config[7, :3])
            continue
        level = (positions[row] - config[8, :3]) @ up / height
        center = body_channel_center(channels, group, level)
        radial = positions[row] - center
        radial -= up * (radial @ up)
        out[i, 3] = float(radial @ config[9, :3]) / height
        fallback = normals[row] - up * (normals[row] @ up)
        out[i, :3] = _unit(radial, _unit(fallback, config[7, :3]))
    return out


def opening_distances(points, faces, openings):
    graph = [[] for _ in points]
    for a, b in _edges(faces):
        length = float(np.linalg.norm(points[a] - points[b]))
        graph[a].append((b, length))
        graph[b].append((a, length))
    distance = np.full(len(points), np.inf)
    queue = []
    for ring in openings:
        for i in ring:
            distance[i] = 0
            heapq.heappush(queue, (0, int(i)))
    while queue:
        d, i = heapq.heappop(queue)
        if d != distance[i]:
            continue
        for j, length in graph[i]:
            value = d + length
            if value < distance[j]:
                distance[j] = value
                heapq.heappush(queue, (value, j))
    return distance


def opening_mobility(points, faces, openings, controlled, height):
    distance = opening_distances(
        points,
        faces,
        [ring for ring in openings if not np.intersect1d(ring, controlled).size],
    )
    t = np.clip((distance / height - 0.12) / 0.13, 0, 1)
    return t * t * (3 - 2 * t)


def attachment_influence(points, faces, controlled, anchors):
    """Harmonic travel influence, with independent cuff/collar boundaries."""
    edge = np.array(list(_edges(faces)), int)
    a, b = edge.T
    weights = 1 / np.maximum(np.linalg.norm(points[a] - points[b], axis=1), 1e-8)
    diagonal = np.bincount(a, weights, minlength=len(points)) + np.bincount(
        b, weights, minlength=len(points)
    )
    free = np.ones(len(points), bool)
    free[np.concatenate(anchors)] = False
    free[controlled] = False
    result = np.zeros(len(points))
    result[controlled] = 1
    if not free.any():
        return result.astype(np.float32)
    if np.any(diagonal[free] <= 0) or not np.isfinite(diagonal[free]).all():
        raise ValueError("衣物控制拓扑包含孤立或退化顶点")

    def laplacian(value):
        return (
            diagonal * value
            - np.bincount(a, weights * value[b], minlength=len(points))
            - np.bincount(b, weights * value[a], minlength=len(points))
        )

    def multiply(value):
        full = np.zeros(len(points))
        full[free] = value
        return laplacian(full)[free]

    rhs = -laplacian(result)[free]
    tolerance = max(np.linalg.norm(rhs) * 1e-10, 1e-9)
    x = np.zeros(free.sum())
    residual = rhs.copy()
    z = residual / diagonal[free]
    direction = z.copy()
    product = float(residual @ z)
    for _ in range(min(len(points) * 4, 4096)):
        if np.linalg.norm(residual) <= tolerance:
            break
        step = multiply(direction)
        alpha = product / max(float(direction @ step), 1e-30)
        x += alpha * direction
        residual -= alpha * step
        z = residual / diagonal[free]
        updated = float(residual @ z)
        direction = z + direction * (updated / product)
        product = updated
    if not np.isfinite(residual).all() or np.linalg.norm(residual) > tolerance:
        raise ValueError("衣物开口的控制影响未能收敛，请检查衣物拓扑")
    result[free] = np.clip(x, 0, 1)
    return result.astype(np.float32)


def surface_track_component(
    positions,
    body_faces,
    adjacency,
    candidates,
    cuff,
    low,
    up,
    height,
    target,
    levels=None,
):
    """Flood the contacted body sheet inside the explicitly authored travel band."""
    tri = positions[body_faces]
    h = (tri - low) @ up / height if levels is None else levels[body_faces]
    mask = candidates & (h.min(1) <= 1.03) & (h.max(1) >= target - 0.015)
    ids = np.flatnonzero(mask)
    if not len(ids):
        raise ValueError("箭头行程没有连续身体表面")
    tree = _BVH(positions, body_faces[ids])
    seeds = {int(ids[tree.nearest(point)[0]]) for point in cuff}
    visited = set()
    stack = list(seeds)
    while stack:
        face = stack.pop()
        if face in visited or not mask[face]:
            continue
        visited.add(face)
        stack.extend(
            int(other)
            for other in adjacency[face]
            if other != INVALID and int(other) not in visited
        )
    result = np.zeros(len(body_faces), bool)
    result[list(visited)] = True
    return result


def torso_surface_partition(
    positions, normals, body_faces, adjacency, torso_points, arm_points
):
    """Reference-topology Voronoi partition; touching surfaces do not join it."""
    triangles = positions[body_faces]
    centers = triangles.mean(1)
    normal = np.cross(
        triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    )
    normal[np.sum(normal * normals[body_faces].sum(1), axis=1) < 0] *= -1
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
    tree = _BVH(positions, body_faces)
    torso = {int(tree.nearest(point)[0]) for point in torso_points}
    arm = {int(tree.nearest(point)[0]) for point in arm_points}
    torso -= arm
    if not torso or not arm:
        raise ValueError("上衣无法建立独立的躯干/袖管贴合种子")

    def distances(seeds):
        distance = np.full(len(body_faces), np.inf)
        queue = []
        for face in seeds:
            distance[face] = 0
            heapq.heappush(queue, (0, face))
        while queue:
            d, face = heapq.heappop(queue)
            if d != distance[face]:
                continue
            for other in adjacency[face]:
                if other >= len(body_faces) or normal[face] @ normal[other] < 0:
                    continue
                value = d + float(np.linalg.norm(centers[face] - centers[other]))
                if value < distance[other]:
                    distance[other] = value
                    heapq.heappush(queue, (value, int(other)))
        return distance

    return distances(torso) < distances(arm)


def intrinsic_levels(positions, body_faces, surface, low, up, height):
    """Smooth longitudinal material coordinate on the reference skin.

    Dirichlet values outside the garment span preserve its existing units.
    Inside the span, the harmonic field has no artificial height-slice extrema
    at a breast overhang or a bent elbow.
    """
    faces = np.asarray(body_faces)[np.asarray(surface) != 0]
    rows = np.unique(faces)
    local = np.searchsorted(rows, faces)
    points = np.asarray(positions, float)[rows]
    edge = np.asarray(list(_edges(local)), np.int64)
    a, b = edge.T
    weight = 1 / np.maximum(np.linalg.norm(points[a] - points[b], axis=1), 1e-8)
    diagonal = np.bincount(a, weight, minlength=len(rows)) + np.bincount(
        b, weight, minlength=len(rows)
    )
    raw = (points - low) @ up / height
    free = (raw > 0.01) & (raw < 0.99)

    def laplace(value):
        return (
            diagonal * value
            - np.bincount(a, weight * value[b], minlength=len(rows))
            - np.bincount(b, weight * value[a], minlength=len(rows))
        )

    base = raw.copy()
    base[free] = 0
    rhs = -laplace(base)[free]

    def multiply(value):
        full = np.zeros(len(rows))
        full[free] = value
        return laplace(full)[free]

    x = raw[free].copy()
    residual = rhs - multiply(x)
    z = residual / np.maximum(diagonal[free], 1e-12)
    direction = z.copy()
    product = float(residual @ z)
    tolerance = max(float(np.linalg.norm(rhs)) * 1e-9, 1e-8)
    for _ in range(min(len(rows) * 4, 4096)):
        if np.linalg.norm(residual) <= tolerance:
            break
        at = multiply(direction)
        denominator = float(direction @ at)
        if denominator <= 1e-30:
            break
        alpha = product / denominator
        x += alpha * direction
        residual -= alpha * at
        z = residual / np.maximum(diagonal[free], 1e-12)
        following = float(residual @ z)
        direction = z + direction * (following / max(product, 1e-30))
        product = following
    if not np.isfinite(x).all() or np.linalg.norm(residual) > tolerance * 2:
        raise ValueError("身体表面行程坐标未收敛，请检查身体分区拓扑")
    result = (np.asarray(positions) - low) @ up / height
    result = result.astype(np.float32)
    base[free] = x
    result[rows] = base
    return result


def bind_skin_partition(positions, body_faces, weights, indices, cuff, side):
    """Body skin support identifies arms without using sleeve opening seeds.

    The controlled torso cuff supplies the trunk scale. Membership uses only
    the body's bind positions and skin weights, so deleting sleeves cannot
    turn chest points into arm attachments.
    """
    if weights is None or indices is None:
        return None
    rows = np.unique(body_faces)
    weight = np.asarray(weights, float)[rows]
    bone = np.asarray(indices, np.uint32)[rows]
    valid = weight > 0
    ids, compact = np.unique(bone[valid], return_inverse=True)
    mass = np.bincount(compact, weight[valid], minlength=len(ids))
    samples = np.broadcast_to(np.asarray(positions)[rows, None], (*weight.shape, 3))[
        valid
    ]
    centers = np.stack(
        [
            np.bincount(compact, weight[valid] * samples[:, axis], minlength=len(ids))
            / np.maximum(mass, 1e-12)
            for axis in range(3)
        ],
        axis=1,
    )
    center = np.asarray(cuff).mean(0)
    radius = float(np.quantile(abs((cuff - center) @ side), 0.98))
    lateral = (centers - center) @ side
    selected = (abs(lateral) > max(radius, 0.005)) & (mass >= 1)
    if not np.any(selected & (lateral < 0)) or not np.any(selected & (lateral > 0)):
        return None
    score = np.sum(np.asarray(weights) * np.isin(indices, ids[selected]), axis=1)
    fraction = float(np.mean(score[rows] > 0.5))
    if not 0.01 < fraction < 0.65:
        return None
    return np.asarray(score[body_faces].mean(1) < 0.5), score


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
    two_ended_rows=None,
    body_surface=None,
    manual_paths=(),
    blend_weights=None,
    blend_indices=None,
    body_domain=None,
    body_branch_weights=None,
    body_skin_faces=0,
    reference_body_faces=None,
):
    """Return one independently controlled runtime per welded cloth component."""
    positions = np.asarray(positions, float)
    directional = (
        np.zeros(len(positions), np.uint8)
        if two_ended_rows is None
        else np.asarray(two_ended_rows, np.uint8)
    )
    if directional.shape != (len(positions),):
        raise ValueError("双端穿脱的顶点标记必须与位置缓冲行数一致")
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
    if manual_paths:
        first_path = manual_paths[0][2]
        up = _unit(first_path[0] - first_path[1])
        for _, _, path in manual_paths:
            if abs(_unit(path[1] - path[0]) @ up) < 0.98:
                raise ValueError(
                    "同一连通衣物的多个穿脱箭头须沿同一轴；不同衣物可使用不同方向"
                )
    elif up_axis == "AUTO":
        _, openings = _boundary_loops(faces)
        if openings:
            ring = max(
                openings, key=lambda ids: np.linalg.norm(np.ptp(points[ids], axis=0))
            )
            _, spread, ring_axes = np.linalg.svd(
                points[ring] - points[ring].mean(0), full_matrices=False
            )
            if (
                spread[1] > 1e-8
                and spread[2] < spread[1] * 0.3
                and abs(up @ ring_axes[2]) < 0.75
            ):
                up = ring_axes[2]
    if not manual_paths and up_axis != "AUTO":
        up = np.zeros(3)
        up["XYZ".index(up_axis[-1])] = -1 if up_axis.startswith("-") else 1
    if (
        not manual_paths
        and up_axis == "AUTO"
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
    # A shorts crotch can join its two lower cloth bands and leave front/back
    # islands. The body's separated feet provide the anatomical left/right axis.
    candidates = body_feet if len(body_feet) >= 2 else cloth_feet
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
    # Clothing height is not a leg radius. A lateral crop by that height
    # discards the outer half of short socks' body cross-sections.
    valid = (bh > -0.1) & (bh < 1.08)
    owners = np.where(
        valid,
        np.where(np.all(bs < 0, axis=1), 1, np.where(np.all(bs >= 0, axis=1), 2, 3)),
        0,
    ).astype(np.uint32)
    surface = (
        np.asarray(body_surface, np.uint32)
        if body_surface is not None
        else (
            _surface_mask(positions, body_faces, normals)
            if exterior
            else np.ones(len(body_faces), np.uint32)
        )
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
        owned_rows = rows[np.isin(weld, component)]
        paths = [
            (zone, path)
            for zone, selected_rows, path in manual_paths
            if np.any(selected_rows[owned_rows])
        ]
        two_ended = bool(np.any(directional[owned_rows]))
        lower_required = bool(np.any(directional[owned_rows] >= 2))
        p, f, reps = _simplify(
            points[component],
            cf,
            representatives[component],
            min(2450, max(32, int(budget))),
        )
        _, openings = _boundary_loops(f)
        if not openings:
            raise ValueError("上下拖拽：未识别到衣物开口，请保留衣物边界")
        opening_height = np.array([float((p[ring] @ up).mean()) for ring in openings])
        span = float(np.ptp(p @ up))
        width = float(np.ptp(p @ side))
        bottom_openings = [
            ring
            for ring, h in zip(openings, opening_height)
            if h < opening_height.min() + span * 0.25
        ]
        sleeves = [
            ring
            for ring, h in zip(openings, opening_height)
            if opening_height.min() + span * 0.25
            < h
            < opening_height.max() - span * 0.15
            and abs(float((p[ring].mean(0) - p.mean(0)) @ side)) > width * 0.25
        ]
        branched = len(openings) >= 4 and len(sleeves) >= 2 and lower_required
        sliding = (
            len(openings) == 3 and len(bottom_openings) == 2 and not lower_required
        )
        if paths:
            two_ended = len(paths) > 1 and any(
                (path[1] - path[0]) @ up > 0 for _, path in paths
            )
            lower_required = two_ended
            branched = False
        if sliding:
            two_ended = False
        upper_cuff = _hem(
            f,
            p,
            up,
            side,
            forward,
            budget=64 if two_ended else 128,
            min_width=0 if paths else (0.08 if branched else 0.2),
        )
        if paths:
            negative_paths = [
                (zone, path) for zone, path in paths if (path[1] - path[0]) @ up < 0
            ]
            if not negative_paths:
                raise ValueError("衣物箭头未指向衣物内部，请检查起点与端点")
            upper_cuff = min(
                openings,
                key=lambda ring: float(
                    np.linalg.norm(p[ring].mean(0) - negative_paths[0][1][0])
                ),
            )
            center = p[upper_cuff].mean(0)
            xy = np.c_[
                (p[upper_cuff] - center) @ side, (p[upper_cuff] - center) @ forward
            ]
            upper_cuff = upper_cuff[
                np.argsort(np.arctan2(xy[:, 1], xy[:, 0]), kind="stable")
            ]
            if len(upper_cuff) > 64:
                upper_cuff = upper_cuff[
                    np.linspace(0, len(upper_cuff) - 1, 64).astype(int)
                ]
        lower_cuff = np.empty(0, int)
        if two_ended:
            try:
                lower_cuff = _hem(f, p, up, side, forward, lower=True, budget=64)
            except ValueError:
                if lower_required:
                    raise
        distinct = len(lower_cuff) and not (
            len(np.intersect1d(upper_cuff, lower_cuff))
            or float((p[upper_cuff].mean(0) - p[lower_cuff].mean(0)) @ up)
            < 0.5 * float(np.ptp(p @ up))
        )
        if two_ended and not distinct and lower_required:
            raise ValueError("双端穿脱需要衣物两端各有一个独立开口，当前未识别到下开口")
        if two_ended and not distinct:
            two_ended = False
            upper_cuff = _hem(f, p, up, side, forward)
            lower_cuff = np.empty(0, int)
        cuff = np.r_[upper_cuff, lower_cuff]
        top = p[upper_cuff].mean(0)
        low = (
            p[lower_cuff].mean(0)
            if two_ended
            else p[(p @ up) <= np.quantile(p @ up, 0.08)].mean(0)
        )
        height = float((top - low) @ up)
        if height <= 1e-6:
            raise ValueError("上下拖拽：衣物开口不是长轴上端，请调整拖拽轴")
        for _, path in paths:
            opening = top if (path[1] - path[0]) @ up < 0 else low
            if abs(float((path[0] - opening) @ up)) > max(height * 0.1, 0.01):
                raise ValueError(
                    "穿脱箭头起点应放在衣物开口平面附近；请移动起点后重新生成"
                )
        hn = (p - low) @ up / height
        branch_height = (
            max(
                0.1,
                min(float(((p[ring] - low) @ up).min()) / height for ring in sleeves)
                - 0.03,
            )
            if branched
            else 1.0
        )
        lat = (p - bottom) @ side
        rest = np.zeros((len(p) * 2, 4), np.float32)
        rest[::2, :3] = p
        rest[::2, 3] = 1 if two_ended else hn >= 0.065
        if branched:
            rest[::2, 3] = hn < branch_height
        elif sliding:
            rest[::2, 3] = 1
        manual_anchor = None
        manual_target = None
        fold_branch = False
        side_openings = []
        collar_openings = []
        attachment_roles = np.zeros(len(p), np.uint32)
        torso_sheet = None
        skin_partition = None
        if paths and not two_ended:
            travel = float(np.linalg.norm(paths[0][1][1] - paths[0][1][0]))
            manual_target = 1 - travel / height
            if not sliding:
                manual_anchor = (
                    max(0.02, manual_target - 0.05)
                    if len(openings) > 2
                    else min(0.07, manual_target * 0.2)
                )
                side_openings = [
                    ring
                    for ring in openings
                    if not np.intersect1d(ring, upper_cuff).size
                    and abs(float((p[ring].mean(0) - p.mean(0)) @ side)) > width * 0.25
                    and 0.05 < float(hn[ring].mean()) < 0.9
                ]
                if len(side_openings) >= 2:
                    # An authored endpoint changes travel, never which sleeves
                    # remain attached. The free panel may roll past this seam.
                    manual_anchor = 0.02
                    fold_branch = True
                    collar_openings = [
                        ring
                        for ring in openings
                        if not np.intersect1d(ring, upper_cuff).size
                        and not any(ring is sleeve for sleeve in side_openings)
                    ]
                    if manual_target < 2 * manual_anchor - 1:
                        raise ValueError("穿脱箭头超过衣片的可折返长度，请缩短行程")
                elif manual_target <= 0.03:
                    raise ValueError(
                        "穿脱箭头超过衣物可压缩长度；请缩短行程或使用可沿身体整体滑移的裤装"
                    )
                rest[::2, 3] = hn > manual_anchor
        if fold_branch:
            skin_partition = (
                (np.ones(len(body_faces), bool), np.zeros(len(positions)))
                if body_domain is not None
                else bind_skin_partition(
                    positions,
                    body_faces,
                    blend_weights,
                    blend_indices,
                    p[upper_cuff],
                    side,
                )
            )
            if skin_partition is not None:
                torso_sheet, skin_score = skin_partition
                manual_anchor = max(0.02, min(0.9, manual_target + 0.05))
        rest[1::2, 2] = hn
        rest[1::2, 3] = lat
        rest[1::2, 0] = 1
        if paths and not sliding and not two_ended and len(openings) > 2:
            if skin_partition is not None:
                distance = opening_distances(p, f, side_openings + collar_openings)
                t = np.clip((distance - 0.001) / 0.025, 0, 1)
                mobility = t * t * (3 - 2 * t)
                rest[1::2, 1] = hn * mobility
                attachment_roles[skin_score[reps] > 0.5] = 1
                native = (hn <= manual_anchor) | (mobility < 1e-4)
                attachment_roles[native & (attachment_roles == 0)] = 2
                rest[::2, 3] = 1
                if collar_openings:
                    rest[np.concatenate(collar_openings) * 2, 3] = 0
            elif fold_branch:
                mobility = opening_mobility(
                    p, f, side_openings + [upper_cuff], upper_cuff, height
                )
                distance = opening_distances(p, f, collar_openings)
                t = np.clip((distance / height - 0.005) / 0.055, 0, 1)
                mobility = np.minimum(mobility, t * t * (3 - 2 * t))
                rest[1::2, 1] = attachment_influence(
                    p,
                    f,
                    upper_cuff,
                    [
                        ring
                        for ring in openings
                        if not np.intersect1d(ring, upper_cuff).size
                    ],
                )
                if collar_openings:
                    collar = np.unique(np.concatenate(collar_openings))
                    collar_influence = attachment_influence(
                        p, f, collar, [upper_cuff] + side_openings
                    )
                    torso_influence = np.clip(rest[1::2, 1] + collar_influence, 0, 1)
                    attachment_roles[torso_influence < 0.65] = 1
                    torso_sheet = torso_surface_partition(
                        positions,
                        normals,
                        body_faces,
                        adj,
                        p[torso_influence >= 0.65],
                        p[np.concatenate(side_openings)],
                    )
            else:
                mobility = opening_mobility(p, f, openings, upper_cuff, height)
                fade = np.clip((hn - manual_anchor) / 0.10, 0, 1)
                mobility *= fade * fade * (3 - 2 * fade)
            rest[1::2, 0] = mobility
            if skin_partition is None:
                rest[::2, 3] *= mobility > 1e-4
            if skin_partition is None and fold_branch and np.any(attachment_roles):
                free = attachment_roles == 1
                pin_distance = opening_distances(
                    p,
                    f,
                    [
                        ring
                        for ring in openings
                        if not np.intersect1d(ring, upper_cuff).size
                    ],
                )
                rest[np.flatnonzero(free) * 2, 3] = pin_distance[free] > 0.005 * height
        rest[cuff * 2 + 1, 0] = 1
        body_levels = (positions - low) @ up / height
        intrinsic = body_domain is not None and (fold_branch or body_domain in (1, 2))
        if intrinsic:
            body_levels = intrinsic_levels(
                positions, body_faces, surface, low, up, height
            )
            normal = normals[body_faces].sum(1)
            normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
            skin_ids = np.flatnonzero(surface)
            tree = _BVH(positions, body_faces[skin_ids], normal[skin_ids])
            for row in owned_rows:
                closest = tree.nearest(positions[row])
                compatible = tree.nearest(
                    positions[row],
                    normals[row],
                    max_distance=max(
                        np.linalg.norm(closest[2] - positions[row]) * 1.5, 0.01
                    ),
                )
                tri, bary, _ = compatible if compatible is not None else closest
                body_levels[row] = float(body_levels[body_faces[skin_ids[tri]]] @ bary)
            rest[1::2, 2] = body_levels[reps]
            rest[cuff * 2 + 1, 2] = 1
            if fold_branch:
                rest[1::2, 1] = rest[1::2, 2] * rest[1::2, 0]
                attachment_roles[:] = 0
                native = (rest[1::2, 2] <= manual_anchor) | (rest[1::2, 0] < 1e-4)
                attachment_roles[native] = 2
        if body_domain is not None:
            if body_branch_weights is not None:
                other = np.argmax(body_branch_weights[reps], axis=1) != body_domain
                attachment_roles[other] = 3
                rest[np.flatnonzero(other) * 2, 3] = 0
            native = (rest[::2, 3] < 0.5) & (attachment_roles == 0)
            attachment_roles[native] = 2
        reference_rows = np.zeros((len(p), 3), np.uint32)
        reference_binding = np.zeros((len(p), 4), np.float32)
        if (
            body_domain is not None
            and reference_body_faces is not None
            and body_branch_weights is not None
        ):
            reference_domains = np.argmax(
                body_branch_weights[reference_body_faces].mean(1), axis=1
            )
            for domain in (0, 1, 2):
                nodes = np.flatnonzero(
                    (attachment_roles == 3)
                    & (np.argmax(body_branch_weights[reps], axis=1) == domain)
                )
                if not len(nodes):
                    continue
                selected = np.asarray(reference_body_faces)[reference_domains == domain]
                normal = normals[selected].sum(1)
                normal /= np.maximum(
                    np.linalg.norm(normal, axis=1, keepdims=True), 1e-12
                )
                tree = _BVH(positions, selected, normal)
                for node in nodes:
                    row = reps[node]
                    closest = tree.nearest(positions[row])
                    compatible = tree.nearest(
                        positions[row],
                        normals[row],
                        max_distance=max(
                            np.linalg.norm(closest[2] - positions[row]) * 1.5, 0.01
                        ),
                    )
                    tri, bary, q = compatible if compatible is not None else closest
                    ids = selected[tri]
                    n = _unit(np.sum(normals[ids] * bary[:, None], axis=0))
                    reference_rows[node] = ids + vertex_base
                    reference_binding[node] = [
                        *bary,
                        max(float((positions[row] - q) @ n), 0),
                    ]
        hem_map = np.full(len(p), INVALID, np.uint32)
        hem_map[cuff] = np.arange(len(cuff))
        params, support_lines, hem_arcs = [], [], []
        for ring in (upper_cuff, lower_cuff):
            if not len(ring):
                continue
            center = p[ring].mean(0)
            lengths = np.linalg.norm(p[ring] - np.roll(p[ring], 1, axis=0), axis=1)
            hem_arcs.append(
                np.r_[0, np.cumsum(lengths[1:])] / max(float(lengths.sum()), 1e-12)
            )
            xy = np.c_[(p[ring] - center) @ side, (p[ring] - center) @ forward]
            xy /= np.maximum(np.linalg.norm(xy, axis=1, keepdims=True), 1e-12)
            params.append(np.c_[xy, (p[ring] - center) @ up, ring])
            previous = xy + np.roll(xy, 1, axis=0)
            following = xy + np.roll(xy, -1, axis=0)
            previous /= np.maximum(
                np.linalg.norm(previous, axis=1, keepdims=True), 1e-12
            )
            following /= np.maximum(
                np.linalg.norm(following, axis=1, keepdims=True), 1e-12
            )
            support_lines.append(np.c_[previous, following])
        hem_params = np.concatenate(params).astype(np.float32)
        lines = np.concatenate(support_lines).astype(np.float32)
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
            body_levels[owned_rows]
            if intrinsic
            else (positions[owned_rows] - low) @ up / height
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
        if branched or sliding:
            single, owner = False, 0
        if body_domain in (1, 2):
            owner = 0
        config = np.zeros((17, 4), np.float32)
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
        if skin_partition is not None:
            config[10, 3] = 0.15
            config[16, 0] = 1
        if body_domain is not None:
            config[10, 3] = 0.15 if fold_branch else 1
            config[16] = [1, body_skin_faces, body_domain, 1]
            if intrinsic:
                config[16, 3] = 2
        config[11] = [1, 0.002, 0.04, 0]
        config[12] = [
            center_side if single else 0,
            center_forward if single else 0,
            int(single),
            owner,
        ]
        if two_ended:
            config[13] = [len(upper_cuff), len(lower_cuff), 1, 0.05]
        local_owners = owners
        if branched or sliding:
            reach = max(width * 0.55, height * 0.2)
            body_sides = (body - bottom) @ side
            local_owners = np.where(
                abs(body_sides.mean(1)) <= reach,
                np.where(
                    np.all(body_sides < 0, 1),
                    1,
                    np.where(np.all(body_sides >= 0, 1), 2, 3),
                ),
                0,
            ).astype(np.uint32)
            config[7, 3] = reach
        if branched:
            config[14] = [branch_height - config[13, 3], 1, branch_height, 1]
        elif sliding:
            # Keep both leg openings free and translate the shorts along the
            # full selected body, rather than gathering into their own crotch.
            bh = positions[body_rows] @ up
            ankle = float(bh.min() + np.ptp(bh) * 0.06)
            config[4, 0] = min(0.0, (ankle - low @ up) / height + 1)
            config[14, 3] = 2
        path_controls = {}
        if paths:
            config[15, 3] = 1
            config[15, 1:3] = 1
            if sliding and len(body_foot_sides) >= 2:
                config[15, 0] = (max(body_foot_sides) - min(body_foot_sides)) * 0.5
            if manual_target is not None:
                config[4, 0] = manual_target
                if not sliding:
                    config[14] = [int(fold_branch), int(fold_branch), manual_anchor, 3]
            if not sliding:
                # Keep a sock's fixed leg ownership. A transverse sleeve track
                # cannot inherit this classifier from the full body's feet.
                body_axis = np.linalg.svd(
                    positions[body_rows] - positions[body_rows].mean(0),
                    full_matrices=False,
                )[2][0]
                if not owner or abs(up @ body_axis) < 0.75:
                    config[12] = 0
                    reach = max(float(np.ptp(p[upper_cuff] @ side)) * 0.6, height * 0.2)
                    local_owners = np.where(
                        abs((body.mean(1) - low) @ side) <= reach, 3, 0
                    ).astype(np.uint32)
                config[7, 3] = max(
                    float(np.ptp(p[upper_cuff] @ side)) * 0.6, height * 0.2
                )
            for zone, path in paths:
                code = 1 if (path[1] - path[0]) @ up < 0 else 2
                maximum = (
                    float(np.linalg.norm(path[1] - path[0]))
                    / height
                    / (1 - config[13, 3])
                    if two_ended
                    else 1.0
                )
                path_controls[zone] = (code, maximum)
            if manual_target is not None and not sliding and config[12, 3] < 0.5:
                connected = surface_track_component(
                    positions,
                    body_faces,
                    adj,
                    (local_owners == 3)
                    & (surface > 0)
                    & (torso_sheet if torso_sheet is not None else True),
                    p[upper_cuff],
                    low,
                    up,
                    height,
                    min(manual_anchor, manual_target),
                    levels=body_levels if intrinsic else None,
                )
                if fold_branch:
                    body_h = (
                        (body[connected] - low) @ up / height
                        if not intrinsic
                        else body_levels[body_faces[connected]]
                    )
                    if not np.any(
                        (body_h.min(1) <= manual_target + 1e-6)
                        & (body_h.max(1) >= manual_target - 1e-6)
                    ):
                        raise ValueError(
                            "穿脱行程没有连通的身体端点截面，请将端点放回碰撞体覆盖范围"
                        )
                local_owners = np.where(connected, 3, 0).astype(np.uint32)
            elif manual_target is not None and sliding:
                connected = surface_track_component(
                    positions,
                    body_faces,
                    adj,
                    (local_owners > 0) & (surface > 0),
                    p[upper_cuff],
                    low,
                    up,
                    height,
                    manual_target - 1 + float(hn.min()),
                )
                local_owners = np.where(connected, local_owners, 0).astype(np.uint32)
        attachment_groups = None
        if torso_sheet is not None:
            # Collision membership is defined in reference topology, never by
            # the closest moving surface at the armpit.
            arm_faces = (~torso_sheet) & (surface > 0)
            arm_side = (body.mean(1) - low) @ side
            local_owners = np.where(
                arm_faces, np.where(arm_side < 0, 4, 5), local_owners
            ).astype(np.uint32)
            attachment_groups = np.where(
                attachment_roles == 1,
                np.where(lat < 0, 4, 5),
                np.where(attachment_roles >= 2, 0, 3),
            )
        contact_seeds = bake_body_seeds(
            positions,
            normals,
            body_faces,
            reps,
            config,
            local_owners,
            surface,
            groups=attachment_groups,
        )
        channels = body_channels(
            positions,
            body_faces,
            config,
            local_owners,
            levels=body_levels if intrinsic else None,
        )
        runtime_levels = np.zeros(runtime_count or len(positions), np.float32)
        runtime_levels[vertex_base : vertex_base + len(positions)] = body_levels
        result.append(
            {
                "rest": rest,
                "attachment_roles": attachment_roles,
                "skin_mix": (
                    shorts_skin_mix(p, f, lat, height)
                    if sliding else np.zeros(len(p), np.float32)
                ),
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
                "hem_arc": np.concatenate(hem_arcs).astype(np.float32),
                "body_adjacency": adj.ravel(),
                "surface_mask": surface,
                "collision_mask": (
                    (np.arange(len(body_faces)) < body_skin_faces).astype(np.uint32)
                    if body_domain is not None
                    else (
                        torso_sheet.astype(np.uint32)
                        if skin_partition is not None
                        else np.ones(len(body_faces), np.uint32)
                    )
                ),
                "body_regions": local_owners,
                "body_levels": runtime_levels,
                "reference_rows": reference_rows.ravel(),
                "reference_binding": reference_binding,
                "display_triangles": display.ravel(),
                "display_weights": weights,
                "path_controls": path_controls,
                "body_seeds": contact_seeds,
                "body_channels": channels,
                "body_directions": body_directions(
                    positions,
                    normals,
                    body_faces,
                    reps,
                    config,
                    local_owners,
                    contact_seeds,
                    channels,
                ),
            }
        )
    if not result:
        raise ValueError("上下拖拽：没有有效衣物连通部分")
    return result
