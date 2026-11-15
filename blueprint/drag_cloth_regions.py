"""Anatomical contact domains from the initial reference rig and skin groups."""

import numpy as np


def bone_regions(parents):
    """0: trunk and both legs, 1/2: separate arms, including their fingers."""

    def side(name):
        text = name.casefold()
        if "left" in text or text.endswith(("_l", ".l")) or "左" in text:
            return 1
        if "right" in text or text.endswith(("_r", ".r")) or "右" in text:
            return 2
        return 0

    def arm(name):
        text = name.casefold()
        return any(
            word in text
            for word in (
                "shoulder",
                "upperarm",
                "upper_arm",
                " arm",
                "-arm",
                "_arm",
                "elbow",
                "elbo",
                "forearm",
                "wrist",
                "hand",
                "finger",
                "腕",
                "肩",
                "ひじ",
            )
        )

    result = {}
    for name in parents:
        at, visited, region = name, set(), 0
        while at is not None and at not in visited:
            visited.add(at)
            if arm(at) and side(at):
                region = side(at)
                break
            at = parents.get(at)
        result[name] = region
    return result


def reference_branches(node, comp, positions, body_rows, objects, query_rows=()):
    """Map reference skin domains through the same transforms as drag handles.

    The input objects precede numeric game-weight matching. No game animation
    matrices or current runtime nearest-surface searches participate in labels.
    """
    objects = list(
        {
            id(obj): obj
            for obj in objects
            if getattr(obj, "type", None) == "MESH" and hasattr(obj, "data")
        }.values()
    )
    if not objects:
        return None
    import bpy

    table = getattr(bpy.data, "objects", ())
    scene_objects = table.values() if hasattr(table, "values") else table
    rigs = [obj for obj in scene_objects if getattr(obj, "type", None) == "ARMATURE"]
    if not rigs:
        return None
    from mathutils.kdtree import KDTree

    transform = np.eye(4)
    for matrix in (
        node._get_non_mirror_mirror(),
        node._get_reference_matrix_inv(comp),
        node._get_export_space_matrix(),
    ):
        if matrix is not None:
            transform = np.asarray(matrix, float) @ transform
    points, labels = [], []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        names = set(obj.vertex_groups.keys())
        rig = max(
            rigs, key=lambda candidate: len(names & set(candidate.data.bones.keys()))
        )
        common = names & set(rig.data.bones.keys())
        if not common:
            continue
        regions = bone_regions(
            {
                bone.name: bone.parent.name if bone.parent else None
                for bone in rig.data.bones
            }
        )
        if not any(regions[name] for name in common):
            continue
        groups = {
            group.index: regions[group.name]
            for group in obj.vertex_groups
            if group.name in common
        }
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            co = np.empty((len(mesh.vertices), 3), float)
            mesh.vertices.foreach_get("co", co.ravel())
            world = transform @ np.asarray(evaluated.matrix_world, float)
            points.extend((np.c_[co, np.ones(len(co))] @ world.T)[:, :3])
            score = np.zeros((len(co), 3), np.float32)
            for vertex in mesh.vertices:
                for group in vertex.groups:
                    if group.group in groups:
                        score[vertex.index, groups[group.group]] += group.weight
            total = score.sum(axis=1)
            score /= np.maximum(total[:, None], 1e-8)
            score[total <= 1e-8, 0] = 1
            labels.extend(score)
        finally:
            evaluated.to_mesh_clear()
    if not points:
        return None
    points, labels = np.asarray(points), np.asarray(labels)
    tree = KDTree(len(points))
    for index, point in enumerate(points):
        tree.insert(point, index)
    tree.balance()
    mapped = np.zeros((len(positions), 3), np.float32)
    tolerance = max(
        float(np.linalg.norm(np.ptp(positions[body_rows], axis=0))) * 1e-5, 1e-6
    )
    for row in body_rows:
        _, index, distance = tree.find(positions[row])
        if distance > tolerance:
            raise ValueError("穿脱人体分区与导出身体不一致；请检查参考物体和初始形态")
        mapped[row] = labels[index]
    for row in query_rows:
        _, index, _ = tree.find(positions[row])
        mapped[row] = labels[index]
    return mapped


def cap_domain(positions, faces):
    """Close cut arm sockets using existing SO vertices, without render edits."""
    positions, faces = np.asarray(positions), np.asarray(faces, np.int64)
    rows = np.unique(faces)
    epsilon = max(float(np.linalg.norm(np.ptp(positions[rows], axis=0))) * 1e-7, 1e-8)
    _, first, inverse = np.unique(
        np.rint(positions[rows] / epsilon).astype(np.int64),
        axis=0,
        return_index=True,
        return_inverse=True,
    )
    canonical = rows[first][inverse]
    faces = canonical[np.searchsorted(rows, faces)]
    linked_edges = {}
    for index, face in enumerate(faces):
        for a, b in zip(face, np.roll(face, -1)):
            linked_edges.setdefault(tuple(sorted((int(a), int(b)))), []).append(
                (index, int(a), int(b))
            )
    graph = [[] for _ in faces]
    for linked in linked_edges.values():
        if len(linked) == 2:
            (a, x, y), (b, u, v) = linked
            sign = -1 if (x, y) == (u, v) else 1
            graph[a].append((b, sign))
            graph[b].append((a, sign))
    flips = np.zeros(len(faces), np.int8)
    for seed in range(len(faces)):
        if flips[seed]:
            continue
        flips[seed] = 1
        stack = [seed]
        while stack:
            at = stack.pop()
            for other, sign in graph[at]:
                wanted = flips[at] * sign
                if flips[other] and flips[other] != wanted:
                    raise ValueError("人体碰撞分区包含不可定向的表面")
                if not flips[other]:
                    flips[other] = wanted
                    stack.append(other)
    faces[flips < 0] = faces[flips < 0][:, [0, 2, 1]]
    directed = {}
    for face in faces:
        for a, b in zip(face, np.roll(face, -1)):
            key = tuple(sorted((int(a), int(b))))
            directed.setdefault(key, []).append((int(a), int(b)))
    boundary = {linked[0] for linked in directed.values() if len(linked) == 1}
    # The cap must run opposite to the open surface's boundary winding.
    pending = {(b, a) for a, b in boundary}
    caps = []
    while pending:
        start, at = min(pending)
        pending.remove((start, at))
        ring = [start]
        while at != start:
            ring.append(at)
            choices = sorted(b for a, b in pending if a == at)
            if not choices:
                raise ValueError("人体分区边界无法闭合，请检查碰撞体拓扑")
            following = choices[0]
            pending.remove((at, following))
            at = following
        if len(ring) < 3:
            continue
        xyz = positions[ring].astype(float)
        _, _, axes = np.linalg.svd(xyz - xyz.mean(axis=0), full_matrices=False)
        uv = (xyz - xyz.mean(axis=0)) @ axes[:2].T
        area = np.sum(
            uv[:, 0] * np.roll(uv[:, 1], -1) - uv[:, 1] * np.roll(uv[:, 0], -1)
        )
        orientation = 1 if area >= 0 else -1
        remaining = list(range(len(ring)))
        while len(remaining) > 3:
            for k, index in enumerate(remaining):
                a, b, c = remaining[k - 1], index, remaining[(k + 1) % len(remaining)]
                ab, ac = uv[b] - uv[a], uv[c] - uv[a]
                cross = ab[0] * ac[1] - ab[1] * ac[0]
                if cross * orientation <= 1e-14:
                    continue
                other = [v for v in remaining if v not in (a, b, c)]
                q = uv[other] - uv[a]
                x = (q[:, 0] * ac[1] - q[:, 1] * ac[0]) / cross
                y = (ab[0] * q[:, 1] - ab[1] * q[:, 0]) / cross
                if np.any((x > 1e-9) & (y > 1e-9) & (x + y < 1 - 1e-9)):
                    continue
                caps.append([ring[a], ring[b], ring[c]])
                remaining.pop(k)
                break
            else:
                raise ValueError("人体分区封口不能稳定三角化，请检查分区边界")
        caps.append([ring[index] for index in remaining])
    result = np.r_[faces, np.asarray(caps, np.int64).reshape(-1, 3)]
    tri = positions[result].astype(float)
    origin = tri.mean(axis=(0, 1))
    volume = np.einsum(
        "ij,ij->i", tri[:, 0] - origin, np.cross(tri[:, 1] - origin, tri[:, 2] - origin)
    ).sum()
    if volume < 0:
        result = result[:, [0, 2, 1]]
    return result.astype(np.uint32), len(faces)
