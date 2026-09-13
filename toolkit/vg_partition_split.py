# -*- coding: utf-8 -*-
"""顶点组分区拆分（实验）——从独立插件 Vertex Group Partition Splitter v1.1.0 原样迁入工具集。

来源插件:
    Vertex Group Partition Splitter
    author: OpenAI  /  version: 1.1.0  /  blender: 4.5.0
    location: View3D > Sidebar > VG Split

用途:
    记录多个来源网格物体的顶点组分区，合并后用于 Data Transfer 传权重，
    再把传递完成的目标物体按记录拆回多个物体，且保留组的权重数值不变。

迁移说明（仅包装层改动，算法与行为保持原样）:
    * 去掉 bl_info 与插件级 register()/unregister()，改为工具集内注册。
    * 算子 bl_idname 由 object.vgsplit_* 改为 toolkit.vgsplit_*（工具集命名约定，
      同时避免与独立插件同时启用时 idname 冲突）。
    * 面板由独立侧栏页签 VG Split 改为直接挂在工具集主面板下，标注（实验），
      bl_order=102 紧跟「高斯权重球（实验）」，收起的工具集树里也能直接看到。
    * Scene 属性名保持 vgsplit_* 原样，由工具集注册/注销。
"""

import json
import re
import uuid

import numpy as np

import bpy
from bpy.props import StringProperty, EnumProperty, FloatProperty, IntProperty
from mathutils.kdtree import KDTree
from mathutils.bvhtree import BVHTree


MANIFEST_VERSION = 2
SEAM_TOLERANCE = 1.0e-4
SEAM_GAP_FACTOR = 2.5
WEIGHT_EPSILON = 1.0e-8
MIN_OVERLAP_MASS = 1.0e-7


def _selected_mesh_objects(context):
    """Return selected mesh objects, with the active object first."""
    selected = [obj for obj in context.selected_objects if obj.type == "MESH"]
    active = context.view_layer.objects.active
    if active in selected:
        selected.remove(active)
        selected.insert(0, active)
    return selected


def _read_manifest(scene):
    raw = scene.vgsplit_manifest
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION or not data.get("sources"):
        return None
    return data


def _write_manifest(scene, objects, group_lists=None, original_group_lists=None):
    if group_lists is None:
        group_lists = [[group.name for group in obj.vertex_groups] for obj in objects]
    if original_group_lists is None:
        original_group_lists = group_lists
    data = {
        "version": MANIFEST_VERSION,
        "merged_order": list(dict.fromkeys(name for names in group_lists for name in names)),
        "sources": [
            {
                "name": obj.name,
                # Blender preserves vertex-group order; this is also the order
                # shown to the user and is useful when groups are numbered.
                "groups": group_lists[index],
                "original_groups": original_group_lists[index],
            }
            for index, obj in enumerate(objects)
        ],
    }
    scene.vgsplit_manifest = json.dumps(data, ensure_ascii=False)
    return data


def _safe_object_name(desired):
    """Generate a readable, unique object name in a collection."""
    if bpy.data.objects.get(desired) is None:
        return desired
    index = 1
    while bpy.data.objects.get(f"{desired}.{index:03d}") is not None:
        index += 1
    return f"{desired}.{index:03d}"


def _seam_vertex_pairs(obj_a, obj_b):
    """Find closest cross-object vertex pairs around a non-overlapping seam."""
    points_a = [obj_a.matrix_world @ vertex.co for vertex in obj_a.data.vertices]
    points_b = [obj_b.matrix_world @ vertex.co for vertex in obj_b.data.vertices]
    if not points_a or not points_b:
        return []
    tree_a = KDTree(len(points_a))
    tree_b = KDTree(len(points_b))
    for index, point in enumerate(points_a):
        tree_a.insert(point, index)
    for index, point in enumerate(points_b):
        tree_b.insert(point, index)
    tree_a.balance()
    tree_b.balance()
    nearest = []
    for index, point in enumerate(points_a):
        _, other, distance = tree_b.find(point)
        nearest.append((distance, index, other))
    for index, point in enumerate(points_b):
        _, other, distance = tree_a.find(point)
        nearest.append((distance, other, index))
    minimum = min(item[0] for item in nearest)
    limit = max(SEAM_TOLERANCE, minimum * SEAM_GAP_FACTOR)
    return {(a, b) for distance, a, b in nearest if distance <= limit}


def _seam_group_pairs(obj_a, obj_b, names_a, names_b):
    """Pair groups whose partial weights continue across a nearby seam.

    No coincident mesh vertices are required. Matching uses the weighted
    profiles on nearest seam vertices, so several low-weight groups at one
    seam can each be paired independently.
    """
    pairs = _seam_vertex_pairs(obj_a, obj_b)
    if not pairs:
        return set()
    # Same matching rule as v1.0, but read sparse assignments once. Blender's
    # group.weight() raises and logs for every unassigned vertex.
    weights_a = {i: {g.group: g.weight for g in obj_a.data.vertices[i].groups} for i, _ in pairs}
    weights_b = {i: {g.group: g.weight for g in obj_b.data.vertices[i].groups} for _, i in pairs}
    candidates = []
    for ia, name_a in enumerate(names_a):
        group_a = obj_a.vertex_groups.get(name_a)
        if group_a is None:
            continue
        for ib, name_b in enumerate(names_b):
            group_b = obj_b.vertex_groups.get(name_b)
            if group_b is None:
                continue
            overlap = mass_a = mass_b = 0.0
            support = 0
            for vertex_a, vertex_b in pairs:
                weight_a = weights_a[vertex_a].get(group_a.index, 0.0)
                weight_b = weights_b[vertex_b].get(group_b.index, 0.0)
                if weight_a <= WEIGHT_EPSILON or weight_b <= WEIGHT_EPSILON:
                    continue
                support += 1
                overlap += min(weight_a, weight_b)
                mass_a += weight_a
                mass_b += weight_b
            if support and overlap / max(mass_a, WEIGHT_EPSILON) >= 0.2 and overlap / max(mass_b, WEIGHT_EPSILON) >= 0.2:
                candidates.append((overlap, ia, ib))
    result = set()
    used_a = set()
    used_b = set()
    for _, ia, ib in sorted(candidates, reverse=True):
        if ia not in used_a and ib not in used_b:
            result.add((ia, ib))
            used_a.add(ia)
            used_b.add(ib)
    return result


def _diffusion_options(scene=None):
    return {
        "threshold": getattr(scene, "vgsplit_diffusion_threshold", 0.88),
        "margin": getattr(scene, "vgsplit_diffusion_margin", 0.04),
        "distance": getattr(scene, "vgsplit_diffusion_distance", 0.0),
        "samples": getattr(scene, "vgsplit_diffusion_samples", 2048),
    }


def _spatial_samples(points, area, limit):
    """Deterministic area-stratified sampling in Morton (spatial) order."""
    if len(points) <= limit:
        return np.arange(len(points))
    lower = points.min(axis=0)
    span = np.maximum(points.max(axis=0) - lower, 1.0e-12)
    grid = np.clip((points - lower) / span * 1023, 0, 1023).astype(np.uint64)
    code = np.zeros(len(points), dtype=np.uint64)
    for bit in range(10):
        for axis in range(3):
            code |= ((grid[:, axis] >> bit) & 1) << (3 * bit + axis)
    order = np.argsort(code, kind="stable")
    cumulative = np.cumsum(area[order])
    marks = (np.arange(limit) + 0.5) * cumulative[-1] / limit
    return np.unique(order[np.searchsorted(cumulative, marks)])


def _diffusion_geometry(obj, limit):
    """Cache undeformed world-space geometry and fields, without writing them."""
    mesh = obj.data
    if len(mesh.vertices) < 4 or not mesh.polygons:
        raise ValueError(f"{obj.name}：扩散分析至少需要 4 个顶点和面")
    points = np.array([tuple(obj.matrix_world @ v.co) for v in mesh.vertices], dtype=np.float64)
    mesh.calc_loop_triangles()
    triangles = np.array([tuple(t.vertices) for t in mesh.loop_triangles], dtype=np.int32)
    field = np.zeros((len(points), len(obj.vertex_groups)), dtype=np.float32)
    for vertex in mesh.vertices:
        for assignment in vertex.groups:
            if assignment.group < field.shape[1]:
                field[vertex.index, assignment.group] = assignment.weight
    corners = points[triangles]
    triangle_area = np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0],
                                           corners[:, 2] - corners[:, 0]), axis=1) / 6.0
    area = np.zeros(len(points), dtype=np.float64)
    for corner in range(3):
        np.add.at(area, triangles[:, corner], triangle_area)
    if area.sum() <= 1.0e-20:
        raise ValueError(f"{obj.name}：没有可分析的有效表面")
    sample = _spatial_samples(points, area, limit)
    # Area quantiles approximate equal-area samples; small meshes use exact
    # lumped triangle areas. Do not count dense tessellation as extra evidence.
    sample_area = area[sample] if len(points) <= limit else np.ones(len(sample))
    sample_area = sample_area / sample_area.sum()
    tree = BVHTree.FromPolygons(points.tolist(), triangles.tolist(), all_triangles=True)
    return {"points": points, "triangles": triangles, "weights": field,
            "peak": field.max(axis=0) if field.shape[1] else np.empty(0),
            "sample": sample, "area": sample_area, "tree": tree,
            "diagonal": float(np.linalg.norm(np.ptp(points, axis=0)))}


def _project_fields(geometry, locations, distance):
    """Barycentric nearest-surface evaluation; no weights are transferred."""
    result = np.zeros((len(locations), geometry["weights"].shape[1]), dtype=np.float64)
    valid = np.zeros(len(locations), dtype=bool)
    for row, point in enumerate(locations):
        hit, _, face, gap = geometry["tree"].find_nearest(tuple(point), distance)
        if hit is None:
            continue
        indices = geometry["triangles"][face]
        a, b, c = geometry["points"][indices]
        u, v, q = b - a, c - a, np.array(hit) - a
        uu, uv, vv = float(u @ u), float(u @ v), float(v @ v)
        determinant = uu * vv - uv * uv
        if determinant <= 1.0e-30:
            continue
        beta = (vv * (q @ u) - uv * (q @ v)) / determinant
        gamma = (uu * (q @ v) - uv * (q @ u)) / determinant
        bary = np.clip([1 - beta - gamma, beta, gamma], 0, 1)
        bary /= bary.sum()
        result[row] = bary @ geometry["weights"][indices]
        valid[row] = True
    return result, valid


def _sample_graph(points, domains):
    """Nearby edges within each sampling surface, never between the shells."""
    edges = []
    for domain in (0, 1):
        indices = np.flatnonzero(domains == domain)
        if len(indices) < 3:
            continue
        tree = KDTree(len(indices))
        for local, index in enumerate(indices):
            tree.insert(tuple(points[index]), local)
        tree.balance()
        neighbours, nearest = [], []
        for local, index in enumerate(indices):
            found = [(indices[j], d) for _, j, d in tree.find_n(tuple(points[index]), min(7, len(indices)))
                     if j != local and d > 1.0e-10]
            neighbours.append(found)
            if found:
                nearest.append(min(d for _, d in found))
        cutoff = 4 * float(np.median(nearest)) if nearest else 0.0
        for index, found in zip(indices, neighbours):
            for other, gap in found:
                if gap <= cutoff:
                    edges.append((index, other, gap))
    if not edges:
        return np.empty(0, dtype=int), np.empty(0, dtype=int), np.empty(0)
    e = np.array(edges)
    return e[:, 0].astype(int), e[:, 1].astype(int), e[:, 2]


def _cosine_matrix(a, b, measure):
    a = a * np.sqrt(measure[:, None])
    b = b * np.sqrt(measure[:, None])
    na = np.linalg.norm(a, axis=0)
    nb = np.linalg.norm(b, axis=0)
    return np.clip((a.T @ b) / np.maximum(na[:, None] * nb[None, :], 1.0e-20), -1, 1)


def _diffuse_samples(field, edges):
    """One lazy random-walk step on analysis samples only."""
    start, end, length = edges
    strength = 1.0 / np.maximum(length, 1.0e-12)
    total = np.bincount(start, weights=strength, minlength=len(field))
    neighbour = np.zeros_like(field)
    np.add.at(neighbour, start, field[end] * strength[:, None])
    active = total > 0
    result = field.copy()
    result[active] = 0.5 * field[active] + 0.5 * neighbour[active] / total[active, None]
    return result


def _reciprocal_matches(scores, threshold, margin):
    """Compare all alternatives before accepting either endpoint. Ties abstain."""
    accepted, candidates = [], []
    if not scores.size:
        return accepted, candidates
    for ia in range(scores.shape[0]):
        ib = int(np.argmax(scores[ia]))
        best = float(scores[ia, ib])
        if best < 0:
            continue
        row_rest = np.delete(scores[ia], ib)
        col_rest = np.delete(scores[:, ib], ia)
        runner_a = max(0.0, float(row_rest.max())) if row_rest.size else 0.0
        runner_b = max(0.0, float(col_rest.max())) if col_rest.size else 0.0
        gap = best - max(runner_a, runner_b)
        reciprocal = int(np.argmax(scores[:, ib])) == ia
        reason = ("低于相似度阈值" if best < threshold else
                  "不是双向最佳" if not reciprocal else
                  "候选过于相似" if gap < max(margin, 1.0e-6) else "接受")
        item = {"a": ia, "b": ib, "score": best, "gap": gap, "reason": reason}
        candidates.append(item)
        if reason == "接受":
            accepted.append(item)
    return accepted, candidates


def _diffusion_group_pairs(a, b, options):
    """Compare multiscale decay fields across two separated, nested surfaces."""
    distance = options["distance"] or 0.25 * min(a["diagonal"], b["diagonal"])
    pa = a["points"][a["sample"]]
    pb = b["points"][b["sample"]]
    wa = a["weights"][a["sample"]].astype(np.float64)
    wb = b["weights"][b["sample"]].astype(np.float64)
    ba, valid_a = _project_fields(b, pa, distance)
    ab, valid_b = _project_fields(a, pb, distance)
    if valid_a.sum() < 6 or valid_b.sum() < 6:
        return [], [], {"distance": distance, "note": "共同可采样表面不足"}
    # Require substantial positive mass on both sides, rather than comparing
    # just the intersection of nonzero weights (which would hide mismatches).
    coverage_a = ((wa[valid_a] * a["area"][valid_a, None]).sum(axis=0)
                  / np.maximum((wa * a["area"][:, None]).sum(axis=0), 1.0e-20))
    coverage_b = ((wb[valid_b] * b["area"][valid_b, None]).sum(axis=0)
                  / np.maximum((wb * b["area"][:, None]).sum(axis=0), 1.0e-20))
    x = np.vstack((wa[valid_a], ab[valid_b]))
    y = np.vstack((ba[valid_a], wb[valid_b]))
    measure = np.concatenate((0.5 * a["area"][valid_a] / a["area"][valid_a].sum(),
                              0.5 * b["area"][valid_b] / b["area"][valid_b].sum()))
    positions = np.vstack((pa[valid_a], pb[valid_b]))
    domains = np.concatenate((np.zeros(valid_a.sum()), np.ones(valid_b.sum())))
    x /= np.maximum(a["peak"], 1.0e-20)
    y /= np.maximum(b["peak"], 1.0e-20)
    edges = _sample_graph(positions, domains)
    start, end, length = edges
    if len(start) < 6:
        return [], [], {"distance": distance, "note": "趋势采样不足"}
    raw = np.maximum(_cosine_matrix(x, y, measure), 0)
    mean_x, mean_y = measure @ x, measure @ y
    centered_x, centered_y = x - mean_x, y - mean_y
    correlation = np.maximum(_cosine_matrix(centered_x, centered_y, measure), 0)
    high = np.maximum(_cosine_matrix(np.maximum(x - 0.6, 0), np.maximum(y - 0.6, 0), measure), 0)
    # Edge differences retain the sign of the falloff. Opposite gradients do
    # not match even when their support overlaps in the same spatial region.
    gx = (x[end] - x[start]) / length[:, None]
    gy = (y[end] - y[start]) / length[:, None]
    gradient = np.maximum(_cosine_matrix(gx, gy, measure[start]), 0)
    dx, dy = x.copy(), y.copy()
    multiscale = np.zeros_like(raw)
    for step in range(4):
        dx = _diffuse_samples(dx, edges)
        dy = _diffuse_samples(dy, edges)
        if step in (0, 3):
            multiscale += 0.5 * np.maximum(_cosine_matrix(dx, dy, measure), 0)
    magnitude = np.minimum(a["peak"][:, None], b["peak"][None, :]) / np.maximum(
        np.maximum(a["peak"][:, None], b["peak"][None, :]), 1.0e-20)
    scores = 0.28 * raw + 0.18 * correlation + 0.14 * multiscale + 0.22 * gradient + 0.10 * high + 0.08 * magnitude
    std_x = np.sqrt(measure @ (centered_x ** 2))
    std_y = np.sqrt(measure @ (centered_y ** 2))
    good_a = (coverage_a >= 0.35) & (std_x >= 0.015) & (a["peak"] > WEIGHT_EPSILON) & ((x > 0.05).sum(axis=0) >= 6)
    good_b = (coverage_b >= 0.35) & (std_y >= 0.015) & (b["peak"] > WEIGHT_EPSILON) & ((y > 0.05).sum(axis=0) >= 6)
    scores[~(good_a[:, None] & good_b[None, :]) | (gradient < 0.3)] = -1
    accepted, candidates = _reciprocal_matches(scores, options["threshold"], options["margin"])
    return accepted, candidates, {"distance": distance, "samples_a": int(valid_a.sum()),
                                  "samples_b": int(valid_b.sum()), "note": ""}


def _diffusion_links(objects, options):
    """Confidence-sorted unions with no two groups from the same source."""
    geometry = [_diffusion_geometry(obj, options["samples"]) for obj in objects]
    nodes = [(i, g.index) for i, obj in enumerate(objects) for g in obj.vertex_groups]
    parent = {node: node for node in nodes}
    members = {node: {node[0]} for node in nodes}
    all_edges, audit = [], []
    def root(node):
        while node != parent[node]:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node
    for ia in range(len(objects)):
        for ib in range(ia + 1, len(objects)):
            accepted, candidates, info = _diffusion_group_pairs(geometry[ia], geometry[ib], options)
            for item in candidates:
                item.update(source_a=ia, source_b=ib)
            audit.append({"source_a": ia, "source_b": ib, **info, "candidates": candidates})
            all_edges.extend((item["score"], (ia, item["a"]), (ib, item["b"]), item) for item in accepted)
    for _, na, nb, item in sorted(all_edges, key=lambda edge: (-edge[0], edge[1], edge[2])):
        ra, rb = root(na), root(nb)
        if ra == rb:
            continue
        if members[ra] & members[rb]:
            item["reason"] = "多物体映射冲突，保留独立组"
            continue
        first, second = sorted((ra, rb))
        parent[second] = first
        members[first] |= members[second]
    links = {node: root(node) for node in nodes if root(node) != node}
    return links, audit


def _publish_diffusion_report(context, objects, links, audit, options):
    """Keep an inspectable report, including ambiguous candidates left apart."""
    lines = ["扩散趋势去重分析 v1.1.0", "相似度分数是启发式评分，并非骨骼身份的概率。",
             f"阈值 {options['threshold']:.3f}；候选差距 {options['margin']:.3f}",
             f"原始组数 {sum(len(o.vertex_groups) for o in objects)}；预计减少 {len(links)} 组", ""]
    for pair in audit:
        oa, ob = objects[pair["source_a"]], objects[pair["source_b"]]
        lines.append(f"{oa.name} ↔ {ob.name} | 最大距离 {pair['distance']:.6g} | {pair['note']}")
        for item in pair["candidates"]:
            ga, gb = oa.vertex_groups[item["a"]].name, ob.vertex_groups[item["b"]].name
            lines.append(f"  {ga} ↔ {gb} | {item['score']:.4f} | 差距 {item['gap']:.4f} | {item['reason']}")
    text = bpy.data.texts.get("VG Split 扩散分析报告") or bpy.data.texts.new("VG Split 扩散分析报告")
    text.clear()
    text.write("\n".join(lines))
    context.scene.vgsplit_last_analysis = f"预计合并 {len(links)} 对；详细结果见文本编辑器中的扩散分析报告"


class VGSPLIT_OT_analyze_diffusion(bpy.types.Operator):
    bl_idname = "toolkit.vgsplit_analyze_diffusion"
    bl_label = "分析扩散候选"
    bl_description = "仅分析；在文本编辑器中生成包含接受和未接受候选的报告"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT" and len(_selected_mesh_objects(context)) >= 2

    def execute(self, context):
        objects = _selected_mesh_objects(context)
        options = _diffusion_options(context.scene)
        try:
            links, audit = _diffusion_links(objects, options)
        except (ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        _publish_diffusion_report(context, objects, links, audit, options)
        self.report({"INFO"}, context.scene.vgsplit_last_analysis)
        return {"FINISHED"}



def _rename_groups_for_merge(objects, mode="SEAM", diffusion_links=None):
    """Make group names unique before join, which otherwise folds same names."""
    original = [[group.name for group in obj.vertex_groups] for obj in objects]
    groups = [group for obj in objects for group in obj.vertex_groups]
    numeric = bool(groups) and all(re.fullmatch(r"\d+", group.name or "") for group in groups)
    mapped = []
    used = set()
    serial = 1
    original_groups = [[group.name for group in obj.vertex_groups] for obj in objects]
    # Determine seam pairings while original names are still available. Names
    # are not used as an identity: any two groups can pair when their weighted
    # seam profiles match in space.
    seam_links = {}
    if mode == "DIFFUSION":
        seam_links = diffusion_links or {}
    elif mode == "SEAM":
        seam_links = {}
        for source_index in range(1, len(objects)):
            for previous_index in range(source_index):
                pairs = _seam_group_pairs(
                    objects[previous_index], objects[source_index],
                    original[previous_index], original[source_index],
                )
                for previous_group_index, current_group_index in pairs:
                    seam_links.setdefault((source_index, current_group_index),
                                          (previous_index, previous_group_index))


    # First move every group to a guaranteed temporary namespace. Blender
    # otherwise auto-suffixes a name when the destination name already exists
    # on the same object (for example, renaming 1..112 to 104..215).
    token = uuid.uuid4().hex[:12]
    for object_index, obj in enumerate(objects):
        for group_index, group in enumerate(obj.vertex_groups):
            group.name = f"__VG_{token}_{object_index}_{group_index}__"

    canonical_by_original = {}
    for source_index, obj in enumerate(objects, 1):
        source_groups = []
        for group_index, group in enumerate(obj.vertex_groups):
            original_name = original_groups[source_index - 1][group_index]
            shared_name = None
            link = seam_links.get((source_index - 1, group_index))
            if link:
                previous_index, previous_group_index = link
                previous_name = original[previous_index][previous_group_index]
                shared_name = canonical_by_original.get((previous_index, previous_name))
            if shared_name:
                new_name = shared_name
            elif numeric:
                new_name = str(serial)
                serial += 1
            else:
                new_name = original_name
                if new_name in used:
                    suffix = 2
                    candidate = f"{new_name}__src{source_index}"
                    while candidate in used:
                        candidate = f"{new_name}__src{source_index}_{suffix}"
                        suffix += 1
                    new_name = candidate
            group.name = new_name
            used.add(new_name)
            canonical_by_original[(source_index - 1, original_name)] = new_name
            source_groups.append(new_name)
        mapped.append(source_groups)
    return mapped, original_groups


def _reorder_vertex_groups(obj, ordered_names):
    """Move existing groups into an exact order without touching weights."""
    if len(obj.vertex_groups) != len(ordered_names):
        return False
    if {group.name for group in obj.vertex_groups} != set(ordered_names):
        return False
    previous_active = bpy.context.view_layer.objects.active
    previous_selected = list(bpy.context.selected_objects)
    for selected in previous_selected:
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    for destination_index, desired_name in enumerate(ordered_names):
        current_index = next(
            index for index, group in enumerate(obj.vertex_groups) if group.name == desired_name
        )
        while current_index > destination_index:
            obj.vertex_groups.active_index = current_index
            bpy.ops.object.vertex_group_move(direction="UP")
            current_index -= 1
    result = [group.name for group in obj.vertex_groups] == list(ordered_names)
    obj.select_set(False)
    for selected in previous_selected:
        if selected.name in bpy.data.objects:
            selected.select_set(True)
    bpy.context.view_layer.objects.active = previous_active
    return result


def _rebuild_groups_from_mapping(obj, merged_names, original_names):
    """Recreate groups in source order, duplicating deduplicated groups.

    A seam deduplication can map one merged group to one group in each source.
    The target therefore has fewer groups than the sum of source records; a
    simple count check is invalid. We snapshot weights and recreate the exact
    source list, so every retained weight value is copied unchanged.
    """
    if len(merged_names) != len(original_names):
        return False
    existing = {group.name: group for group in obj.vertex_groups}
    snapshots = {name: [] for name in existing}
    index_names = {group.index: name for name, group in existing.items()}
    for vertex in obj.data.vertices:
        for assignment in vertex.groups:
            name = index_names.get(assignment.group)
            if name is not None:
                snapshots[name].append((vertex.index, assignment.weight))
    for group in list(obj.vertex_groups)[::-1]:
        obj.vertex_groups.remove(group)
    for merged_name, original_name in zip(merged_names, original_names):
        new_group = obj.vertex_groups.new(name=original_name)
        for vertex_index, weight in snapshots.get(merged_name, []):
            new_group.add([vertex_index], weight, "REPLACE")
    return [group.name for group in obj.vertex_groups] == list(original_names)


class VGSPLIT_OT_record(bpy.types.Operator):
    bl_idname = "toolkit.vgsplit_record"
    bl_label = "记录顶点组分区"
    bl_description = "记录当前选中的两个或多个网格物体及其顶点组"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return len(_selected_mesh_objects(context)) >= 2

    def execute(self, context):
        objects = _selected_mesh_objects(context)
        _write_manifest(context.scene, objects)
        self.report({"INFO"}, f"已记录 {len(objects)} 个物体的顶点组分区")
        return {"FINISHED"}


class VGSPLIT_OT_record_and_merge(bpy.types.Operator):
    bl_idname = "toolkit.vgsplit_record_and_merge"
    bl_label = "记录并合并物体"
    bl_description = "记录分区，然后将所选网格合并为一个物体，用于后续顶点组传递"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "OBJECT"
            and len(_selected_mesh_objects(context)) >= 2
            and context.view_layer.objects.active is not None
        )

    def execute(self, context):
        objects = _selected_mesh_objects(context)
        mode = context.scene.vgsplit_dedup_mode
        options = _diffusion_options(context.scene)
        links, audit = {}, []
        if mode == "DIFFUSION":
            try:
                links, audit = _diffusion_links(objects, options)
            except (RuntimeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            _publish_diffusion_report(context, objects, links, audit, options)
        # Save locks before changing names; they follow original source order.
        locks = [[g.lock_weight for g in obj.vertex_groups] for obj in objects]
        mapped_groups, original_groups = _rename_groups_for_merge(objects, mode, links)
        manifest = _write_manifest(context.scene, objects, mapped_groups, original_groups)
        manifest.update(dedup_mode=mode, diffusion_options=options if mode == "DIFFUSION" else {},
                        diffusion_audit=audit)
        for source, source_locks in zip(manifest["sources"], locks):
            source["locks"] = source_locks
        context.scene.vgsplit_manifest = json.dumps(manifest, ensure_ascii=False)
        active = objects[0]
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = active
        try:
            bpy.ops.object.join()
        except RuntimeError as exc:
            self.report({"ERROR"}, f"合并失败：{exc}")
            return {"CANCELLED"}
        # Join can carry over unexpected groups from Blender's active-object
        # state. Enforce the exact recorded union so a 103 + 112 setup can
        # never become 217 groups.
        expected_names = set(name for names in mapped_groups for name in names)
        extras = [group.name for group in active.vertex_groups if group.name not in expected_names]
        for group in list(active.vertex_groups)[::-1]:
            if group.name not in expected_names:
                active.vertex_groups.remove(group)
        if extras:
            self.report({"WARNING"}, "已移除合并时产生的额外顶点组：" + ", ".join(extras))
        expected_order = list(dict.fromkeys(name for names in mapped_groups for name in names))
        if not _reorder_vertex_groups(active, expected_order):
            self.report({"ERROR"}, "合并后的顶点组序列校验失败，已撤销操作")
            bpy.ops.ed.undo()
            return {"CANCELLED"}
        original_count = sum(len(names) for names in original_groups)
        self.report({"INFO"}, f"已记录 {original_count} 组 → 合并后 {len(active.vertex_groups)} 组")
        return {"FINISHED"}


class VGSPLIT_OT_split(bpy.types.Operator):
    bl_idname = "toolkit.vgsplit_split"
    bl_label = "按记录拆分顶点组"
    bl_description = "复制当前物体，并为每个来源保留对应的顶点组；保留组的权重不变"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "OBJECT"
            and
            context.active_object is not None
            and context.active_object.type == "MESH"
            and _read_manifest(context.scene) is not None
        )

    def execute(self, context):
        manifest = _read_manifest(context.scene)
        target = context.active_object
        sources = manifest["sources"]
        if len(sources) < 2:
            self.report({"ERROR"}, "记录中至少需要两个来源物体")
            return {"CANCELLED"}

        target_group_names = {group.name for group in target.vertex_groups}
        created = []
        original_name = target.name
        collection = target.users_collection[0] if target.users_collection else context.scene.collection
        for source in sources:
            duplicate = target.copy()
            duplicate.data = target.data.copy()
            duplicate.name = _safe_object_name(f"{original_name}_{source['name']}")
            collection.objects.link(duplicate)

            merged_names = list(source["groups"])
            original_names = source.get("original_groups", merged_names)
            # Keep one mapping entry per source group, including entries that
            # point to a shared deduplicated group.
            if not _rebuild_groups_from_mapping(duplicate, merged_names, original_names):
                self.report({"ERROR"}, f"副本“{duplicate.name}”的顶点组顺序校验失败")
                bpy.data.objects.remove(duplicate, do_unlink=True)
                for created_object in created:
                    bpy.data.objects.remove(created_object, do_unlink=True)
                return {"CANCELLED"}
            for group, locked in zip(duplicate.vertex_groups, source.get("locks", [])):
                group.lock_weight = locked
            duplicate.select_set(False)
            created.append(duplicate)

        # Replacing the transferred object avoids leaving an extra unsplit copy.
        bpy.data.objects.remove(target, do_unlink=True)
        for duplicate in created:
            duplicate.select_set(True)
        context.view_layer.objects.active = created[0]

        missing = sorted(set().union(*(set(source["groups"]) for source in sources)) - target_group_names)
        if missing:
            self.report({"WARNING"}, "目标物体中未找到部分记录的顶点组：" + ", ".join(missing))
        self.report({"INFO"}, f"已拆分为 {len(created)} 个物体；保留组的权重未修改")
        return {"FINISHED"}


class VGSPLIT_OT_clear(bpy.types.Operator):
    bl_idname = "toolkit.vgsplit_clear"
    bl_label = "清除记录"
    bl_description = "清除当前场景保存的顶点组分区记录"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        context.scene.vgsplit_manifest = ""
        self.report({"INFO"}, "已清除顶点组分区记录")
        return {"FINISHED"}


class VGSPLIT_PT_panel(bpy.types.Panel):
    bl_label = "顶点组分区拆分（实验）"
    bl_idname = "VIEW3D_PT_Herta_VGPS_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TheHerta4"
    bl_parent_id = "VIEW3D_PT_Herta_Toolkit_Panel"
    bl_options = {"DEFAULT_CLOSED"}
    # 顶层实验功能排在主面板之后：高斯权重球(实验)=101，本面板=102。
    bl_order = 102

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        manifest = _read_manifest(scene)

        box = layout.box()
        box.label(text="1. 记录来源分区 · v1.1.0", icon="GROUP_VERTEX")
        box.prop(scene, "vgsplit_dedup_mode", text="去重方式")
        if scene.vgsplit_dedup_mode == "DIFFUSION":
            settings = box.column(align=True)
            settings.prop(scene, "vgsplit_diffusion_threshold")
            settings.prop(scene, "vgsplit_diffusion_margin")
            settings.prop(scene, "vgsplit_diffusion_distance")
            settings.label(text="距离为 0：按模型尺寸自动估算")
            settings.prop(scene, "vgsplit_diffusion_samples")
            settings.operator(VGSPLIT_OT_analyze_diffusion.bl_idname, icon="VIEWZOOM")
            settings.label(text="分析报告：文本编辑器 → VG Split 扩散分析报告")
            if scene.vgsplit_last_analysis:
                settings.label(text=scene.vgsplit_last_analysis.split("；")[0])
        box.operator(VGSPLIT_OT_record.bl_idname, icon="REC")
        box.operator(VGSPLIT_OT_record_and_merge.bl_idname, icon="AUTOMERGE_ON")

        box = layout.box()
        box.label(text="2. 将合并物体传递到目标物体")
        box.label(text="使用 Blender 的 Data Transfer / Transfer Weights")

        box = layout.box()
        box.label(text="3. 选中目标物体并拆分", icon="MOD_VERTEX_WEIGHT")
        row = box.row()
        row.enabled = manifest is not None
        row.operator(VGSPLIT_OT_split.bl_idname, icon="DUPLICATE")
        box.operator(VGSPLIT_OT_clear.bl_idname, icon="X")

        if manifest:
            box = layout.box()
            box.label(text=f"当前记录：{len(manifest['sources'])} 个来源")
            for source in manifest["sources"]:
                box.label(text=f"{source['name']}：{len(source['groups'])} 组")
        else:
            layout.label(text="尚未记录来源分区", icon="INFO")


vg_partition_split_operators = [
    VGSPLIT_OT_analyze_diffusion,
    VGSPLIT_OT_record,
    VGSPLIT_OT_record_and_merge,
    VGSPLIT_OT_split,
    VGSPLIT_OT_clear,
]

vg_partition_split_panels = [
    VGSPLIT_PT_panel,
]


SETTING_NAMES = (
    "vgsplit_dedup_mode", "vgsplit_diffusion_threshold", "vgsplit_diffusion_margin",
    "vgsplit_diffusion_distance", "vgsplit_diffusion_samples", "vgsplit_last_analysis",
)


def register_vg_partition_split_properties():
    """注册 Scene 级设置。必须在算子/面板之前调用。"""
    bpy.types.Scene.vgsplit_dedup_mode = EnumProperty(
        name="去重方式",
        items=[("SEAM", "普通接缝去重", "保留已验证的接缝算法，适用于上下半身等相邻分块"),
               ("DIFFUSION", "扩散趋势去重", "比较分离表面的权重衰减趋势，适用于宽袖子与手臂"),
               ("NONE", "不去重", "独立记录每个来源组")], default="SEAM")
    bpy.types.Scene.vgsplit_diffusion_threshold = FloatProperty(
        name="最低相似度", description="启发式评分；越高越保守，并非准确率", default=0.88, min=0.5, max=1.0, precision=3)
    bpy.types.Scene.vgsplit_diffusion_margin = FloatProperty(
        name="领先第二名", description="双向最佳须领先所有其他候选的最小分数差；平局永不自动合并",
        default=0.04, min=0.0, max=0.5, precision=3)
    bpy.types.Scene.vgsplit_diffusion_distance = FloatProperty(
        name="最大投影距离", description="世界坐标距离；0 使用两物体较小包围盒对角线的 25%",
        default=0.0, min=0.0, soft_max=10.0, precision=4, subtype="DISTANCE")
    bpy.types.Scene.vgsplit_diffusion_samples = IntProperty(
        name="每个物体采样数", description="采样上限；小网格使用所有顶点，大网格按面积和空间分层采样",
        default=2048, min=128, max=8192)
    bpy.types.Scene.vgsplit_last_analysis = StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})
    bpy.types.Scene.vgsplit_manifest = StringProperty(
        name="Vertex Group Split Manifest",
        description="Internal JSON record used by Vertex Group Partition Splitter",
        default="",
        options={"HIDDEN"},
    )


def unregister_vg_partition_split_properties():
    """注销 Scene 级设置。幂等，可重复调用。"""
    for name in SETTING_NAMES:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    if hasattr(bpy.types.Scene, "vgsplit_manifest"):
        del bpy.types.Scene.vgsplit_manifest
