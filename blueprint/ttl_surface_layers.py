"""Infer object-level TTL order from nearby surfaces in exported mesh geometry."""
from collections import defaultdict
import heapq

import numpy as np


_SAMPLES_PER_OBJECT = 128
_RAY_EPSILON = 1e-5
_MIN_NORMAL_DOT = 0.5
_MIN_SUPPORT = 3
_MIN_AGREEMENT = 0.85


def _ordered_layers(names, relations):
    """Prefer consistent constraints, break cycles, then reuse independent layers."""
    children = {name: set() for name in names}
    rejected = []
    for inner, outer, agreement, support in sorted(
        relations, key=lambda edge: (-edge[2], -edge[3], edge[0], edge[1])
    ):
        pending = [outer]
        visited = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            pending.extend(children[current] - visited)
        if inner in visited:
            rejected.append((inner, outer))
        else:
            children[inner].add(outer)

    indegree = dict.fromkeys(names, 0)
    layers = dict.fromkeys(names, 0)
    for targets in children.values():
        for target in targets:
            indegree[target] += 1
    ready = [name for name in names if not indegree[name]]
    heapq.heapify(ready)
    while ready:
        current = heapq.heappop(ready)
        for target in sorted(children[current]):
            layers[target] = max(layers[target], layers[current] + 1)
            indegree[target] -= 1
            if not indegree[target]:
                heapq.heappush(ready, target)
    return layers, rejected


def _world_geometry(obj):
    mesh = getattr(obj, "data", None)
    if mesh is None or not hasattr(mesh, "calc_loop_triangles") or len(mesh.vertices) < 3:
        return None
    mesh.calc_loop_triangles()
    if not mesh.loop_triangles:
        return None
    points = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", points)
    points = points.reshape(-1, 3)
    matrix = np.asarray(obj.matrix_world, dtype=np.float64)
    points = points @ matrix[:3, :3].T + matrix[:3, 3]
    triangles = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    triangles = triangles.reshape(-1, 3)
    if not np.isfinite(points).all():
        return None
    corners = points[triangles]
    cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    areas = np.linalg.norm(cross, axis=1)
    valid = areas > 1e-12
    if not valid.any():
        return None
    triangles, corners, areas, cross = triangles[valid], corners[valid], areas[valid], cross[valid]

    # Equal-area deterministic samples keep dense triangulation from dominating votes.
    cumulative = np.cumsum(areas)
    steps = np.arange(_SAMPLES_PER_OBJECT, dtype=np.float64) + 0.5
    selected = np.searchsorted(cumulative, steps * cumulative[-1] / len(steps))
    u = np.sqrt(np.mod(steps * 0.7548776662466927, 1.0))
    v = np.mod(steps * 0.5698402909980532, 1.0)
    barycentric = np.stack((1 - u, u * (1 - v), u * v), axis=1)
    samples = (corners[selected] * barycentric[:, :, None]).sum(axis=1)
    normals = cross[selected] / areas[selected, None]
    try:
        armature = obj.find_armature()
    except (AttributeError, RuntimeError):
        armature = None
    origin = np.zeros(3)
    if armature is not None:
        origin = np.asarray(armature.matrix_world, dtype=np.float64)[:3, 3]
    # Intersect normal lines in least squares to locate a curved shell's own
    # center. Sleeves need a local reference rather than the character's axis.
    projectors = np.eye(3) - normals[:, :, None] * normals[:, None, :]
    system = projectors.mean(axis=0)
    center = None
    if np.linalg.eigvalsh(system)[0] > 0.03:
        candidate = np.linalg.solve(system, np.einsum("nij,nj->ni", projectors, samples).mean(axis=0))
        padding = np.ptp(points, axis=0) * 0.25 + _RAY_EPSILON
        if np.all(candidate >= points.min(axis=0) - padding) and np.all(candidate <= points.max(axis=0) + padding):
            center = candidate
    reference = origin if center is None else center
    return {
        "points": points, "triangles": triangles, "samples": samples,
        "normals": normals, "origin": origin, "group": id(armature) if armature is not None else None,
        "center": center,
        "radius": float(np.median(np.linalg.norm(samples - reference, axis=1))),
    }


def _outward(point, normal, record, vector_type):
    origin = record["origin"]
    # Curved shells face away from their local center. Flat patches use the
    # skeleton's vertical axis, or height for nearly horizontal surfaces.
    if record["center"] is not None:
        reference = point - vector_type(record["center"])
    elif normal.x * normal.x + normal.y * normal.y >= 0.09:
        reference = vector_type((point.x - origin[0], point.y - origin[1], 0))
    else:
        reference = vector_type((0, 0, point.z - origin[2]))
    projection = normal.dot(reference)
    if abs(projection) <= _RAY_EPSILON:
        dominant = max(range(3), key=lambda axis: abs(normal[axis]))
        direction = normal if normal[dominant] >= 0 else -normal
    else:
        direction = normal if projection > 0 else -normal
    return direction, abs(projection)


def _surface_relations(geometry):
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    points, triangles, owners = [], [], []
    for name, record in geometry.items():
        offset = len(points)
        points.extend(record["points"].tolist())
        triangles.extend((record["triangles"] + offset).tolist())
        owners.extend([name] * len(record["triangles"]))
    tree = BVHTree.FromPolygons(points, triangles, all_triangles=True)
    maximum = float(np.linalg.norm(np.ptp(np.asarray(points), axis=0))) + _RAY_EPSILON
    votes = defaultdict(lambda: [0, 0])

    def neighbor(source, point, direction, expected_normal, limit):
        remaining = limit - _RAY_EPSILON
        start = point + direction * _RAY_EPSILON
        # Skip self-folds and transverse surfaces, but never cross the skeleton
        # axis when looking inward: the back of a skirt is a different surface.
        for _ in range(16):
            if remaining <= _RAY_EPSILON:
                return None
            location, normal, index, distance = tree.ray_cast(start, direction, remaining)
            if index is None:
                return None
            target = owners[index]
            if target != source:
                outward, _ = _outward(location, normal, geometry[target], Vector)
                if outward.dot(expected_normal) >= _MIN_NORMAL_DOT:
                    return target
            advance = distance + _RAY_EPSILON
            start = location + direction * _RAY_EPSILON
            remaining -= advance
        return None

    for name, record in geometry.items():
        for sample, normal in zip(record["samples"], record["normals"]):
            point = Vector(sample)
            outward, inward_limit = _outward(point, Vector(normal), record, Vector)
            for sign, limit in ((1, maximum), (-1, inward_limit)):
                target = neighbor(name, point, outward * sign, outward, limit)
                if target is None:
                    continue
                inner, outer = (name, target) if sign > 0 else (target, name)
                pair = tuple(sorted((inner, outer)))
                votes[pair][0 if inner == pair[0] else 1] += 1

    relations, issues = [], defaultdict(list)
    for (a, b), counts in sorted(votes.items()):
        support = max(counts)
        if support < _MIN_SUPPORT:
            continue
        agreement = support / sum(counts)
        if agreement >= _MIN_AGREEMENT:
            inner, outer = (a, b) if counts[0] >= counts[1] else (b, a)
        else:
            inner, outer = sorted((a, b), key=lambda name: (geometry[name]["radius"], name))
            agreement = 0.0
            for name in (a, b):
                issues[name].append("局部表面内外关系不一致，按中位半径回退；可用 _TTL层N 明确层号")
        relations.append((inner, outer, agreement, support))
    return relations, issues


def infer_surface_layers(objects):
    """Return uncapped layers and warnings, keyed by name; unreadable meshes are omitted."""
    groups = defaultdict(dict)
    for name, obj in sorted(objects, key=lambda item: item[0]):
        record = _world_geometry(obj)
        if record is not None:
            groups[record["group"]][name] = record
    layers, issues = {}, {}
    for geometry in groups.values():
        relations, group_issues = _surface_relations(geometry)
        resolved, rejected = _ordered_layers(geometry, relations)
        for inner, outer in rejected:
            for name in (inner, outer):
                group_issues[name].append("表面层级存在循环，忽略较弱约束；可用 _TTL层N 明确层号")
        layers.update(resolved)
        issues.update(group_issues)
    return layers, issues
