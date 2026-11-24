"""Reconstruct a ZZMI bind-pose armature from workspace buffers."""

from collections import defaultdict
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import pairwise
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector
from mathutils.kdtree import KDTree

from ..utils.format_utils import Fatal, FormatUtils
from .mesh_create_helper import MeshCreateHelper
from .ssmt_import_helper import SSMTImportHelper
from .submesh_json import SubmeshJson
from .workspace_helper import WorkSpaceHelper
from .zzmi_skeleton import ZZMIBoneMapBuilder, ZZMISkeletonMergeHelper

WORKSPACE_IMPORT_RIG_MARKER = "SSMT:WorkspaceImportRig"
WORKSPACE_IMPORT_RIG_MIRROR_X = "SSMT:WorkspaceImportRigMirrorX"
WORKSPACE_IMPORT_RIG_ROOT = "SSMT:WorkspaceImportRigRoot"
WORKSPACE_IMPORT_BONE_PARENT = "SSMT:WorkspaceImportBoneParent"


@dataclass
class WorkspacePart:
    identity: str
    draw_ib: str
    positions: np.ndarray
    triangles: np.ndarray
    indices: np.ndarray
    weights: np.ndarray
    palette: dict[int, np.ndarray]


def _load_part(identity: str, source: SubmeshJson) -> WorkspacePart:
    if not source.MergedSkeletonMetadataValid or not source.VGCount:
        raise Fatal(f"{identity}：缺少有效的 VGMap，请先生成工作区合并骨骼数据")

    vg_map = MeshCreateHelper._normalize_and_validate_vg_map(source.VGMap, source.VGCount)
    matrix_path = Path(ZZMISkeletonMergeHelper._runtime_cache_path(source.JsonDict, source.JsonFilePath, identity))
    if not matrix_path.is_file():
        raise Fatal(f"{identity}：缺少骨骼矩阵文件 {matrix_path.name}")
    matrices = ZZMIBoneMapBuilder.load_palette(str(matrix_path)).reshape(-1, 3, 4)
    if len(matrices) < source.VGCount or not np.isfinite(matrices[:source.VGCount]).all():
        raise Fatal(f"{identity}：骨骼矩阵条目与 VGMap 不一致")

    position = None
    indices_by_channel = {}
    weights_by_channel = {}
    for buffer in source.CategoryBufferList:
        semantics = {element.SemanticName for element in buffer.D3D11ElementList}
        if not semantics.intersection({"POSITION", "BLENDINDICES", "BLENDWEIGHTS", "BLENDWEIGHT"}):
            continue
        elements, vertex_data, _ = SSMTImportHelper.parse_normal_category_buffer(
            buffer, source.VertexOffset, source.VertexCount,
        )
        for element in elements:
            name = element.SemanticName
            decoded = FormatUtils.apply_format_conversion(vertex_data[element.ElementName], element.Format)
            rows = decoded.reshape(len(decoded), -1)
            if name == "POSITION":
                position = rows[:, :3]
            elif name == "BLENDINDICES":
                indices_by_channel[element.SemanticIndex] = rows
            elif name in {"BLENDWEIGHT", "BLENDWEIGHTS"}:
                weights_by_channel[element.SemanticIndex] = rows
    # Match mesh import for rigid inputs that store only BLENDINDICES.
    if not weights_by_channel:
        for channel, rows in indices_by_channel.items():
            weights_by_channel[channel] = np.zeros(rows.shape, dtype=np.float32)
            weights_by_channel[channel][:, 0] = 1
    if (
        position is None or position.shape[1] != 3 or not indices_by_channel
        or indices_by_channel.keys() != weights_by_channel.keys()
    ):
        raise Fatal(f"{identity}：缺少匹配的位置、骨骼索引或权重缓冲")
    vertex_count = len(position)
    faces, count, _ = SSMTImportHelper.parse_index_buffer(source)
    if count % 3 or faces.min() < 0 or faces.max() >= vertex_count:
        raise Fatal(f"{identity}：IB 引用了无效的绑定姿态顶点")
    used, corners = np.unique(faces, return_inverse=True)
    corners = corners.reshape(-1, 3)
    position = position[used]
    if not np.isfinite(position).all():
        raise Fatal(f"{identity}：位置缓冲含有无效坐标")

    indices = []
    weights = []
    local_slots = np.array([vg_map[i] for i in range(source.VGCount)])
    palette = defaultdict(list)
    for slot, matrix in zip(local_slots, matrices[:source.VGCount]):
        palette[int(slot)].append(matrix)
    for channel in sorted(indices_by_channel):
        ids = indices_by_channel[channel]
        values = weights_by_channel[channel]
        if ids.shape != values.shape or len(ids) != vertex_count:
            raise Fatal(f"{identity}：骨骼索引和权重的缓冲布局不一致")
        ids = ids[used]
        values = values[used]
        if not np.isfinite(values).all() or np.any(values < 0):
            raise Fatal(f"{identity}：权重缓冲含有无效数值")
        active = values > 0
        if np.any((ids[active] < 0) | (ids[active] >= len(local_slots))):
            raise Fatal(f"{identity}：权重引用了 VGMap 外的局部骨骼")
        indices.append(local_slots[np.where(active, ids, 0).astype(np.intp)])
        weights.append(values)
    return WorkspacePart(
        identity, WorkSpaceHelper.parse_lod_unique_str(identity)[1].split("-", 1)[0], position, corners,
        np.concatenate(indices, axis=1), np.concatenate(weights, axis=1),
        {slot: np.asarray(samples).mean(axis=0) for slot, samples in palette.items()},
    )


def _add_evidence(parts):
    slots = sorted({slot for part in parts for slot in part.palette})
    masses = np.zeros(len(slots))
    centroids = np.zeros((len(slots), 3))
    memberships = defaultdict(set)
    shared = {}
    contacts = defaultdict(float)
    for part in parts:
        for slot in part.palette:
            memberships[slot].add(part.draw_ib)
        indices = np.searchsorted(slots, part.indices)
        for column, weights in zip(indices.T, part.weights.T):
            np.add.at(masses, column, weights)
            np.add.at(centroids, column, part.positions * weights[:, None])
        for first in range(indices.shape[1]):
            for second in range(first):
                a, b = indices[:, first], indices[:, second]
                weights = np.minimum(part.weights[:, first], part.weights[:, second])
                valid = (a != b) & (weights > 0)
                if not valid.any():
                    continue
                keys = np.sort(np.column_stack((a[valid], b[valid])), axis=1)
                keys, inverse = np.unique(keys, axis=0, return_inverse=True)
                totals = np.bincount(inverse, weights[valid])
                positions = np.zeros((len(keys), 3))
                np.add.at(positions, inverse, part.positions[valid] * weights[valid, None])
                for (a, b), total, point in zip(keys, totals, positions):
                    key = (int(a), int(b))
                    tally = shared.setdefault(key, np.zeros(4))
                    tally[:3] += point
                    tally[3] += total
        dominant = indices[np.arange(len(indices)), part.weights.argmax(axis=1)]
        valid = part.weights.max(axis=1) > 0
        for first, second in ((0, 1), (1, 2), (2, 0)):
            a, b = part.triangles[:, first], part.triangles[:, second]
            different = valid[a] & valid[b] & (dominant[a] != dominant[b])
            edges = np.sort(np.column_stack((dominant[a[different]], dominant[b[different]])), axis=1)
            edges, count = np.unique(edges, axis=0, return_counts=True)
            for (a, b), contribution in zip(edges, count):
                contacts[int(a), int(b)] += contribution * .025

    # A slot omitted by every IB's indices has no location evidence. Use the
    # support center of its IB so it still exists under its original global ID.
    ib_positions = defaultdict(list)
    for part in parts:
        ib_positions[part.draw_ib].append(part.positions)
    for index, slot in enumerate(slots):
        if masses[index] > 0:
            centroids[index] /= masses[index]
        else:
            points = [rows for ib in sorted(memberships[slot]) for rows in ib_positions[ib]]
            centroids[index] = np.concatenate(points).mean(axis=0)
    return slots, masses, centroids, memberships, shared, contacts


def _find_mirrors(centers, masses, memberships, slots, middle, height):
    sides = np.sign(centers[:, 0] - middle)
    candidates = {side: KDTree(len(centers)) for side in (-1, 1)}
    for index, point in enumerate(centers):
        side = sides[index]
        if abs(point[0] - middle) > height * .009 and masses[index] > 0:
            candidates[side].insert(point, index)
    for tree in candidates.values():
        tree.balance()

    matches = {}
    for index, point in enumerate(centers):
        side = sides[index]
        if abs(point[0] - middle) <= height * .009 or masses[index] == 0:
            continue
        reflected = (2 * middle - point[0], point[1], point[2])
        options = []
        for _, other, dist in candidates[-side].find_range(reflected, height * .011):
            if not memberships[slots[index]].intersection(memberships[slots[other]]):
                continue
            mass_ratio = min(masses[index], masses[other]) / max(masses[index], masses[other])
            if mass_ratio >= .72:
                options.append((dist / height + .012 * (1 - mass_ratio), other))
        if options:
            matches[index] = min(options)[1]
    return {a: b for a, b in matches.items() if matches.get(b) == a}


def _symmetry_plane(positions, centers, masses, memberships, slots, height):
    low, high = np.min(positions[:, 0]), np.max(positions[:, 0])
    box_center = (low + high) * .5
    median = float(np.median(positions[:, 0]))
    tree = KDTree(len(centers))
    for index, (_, y, z) in enumerate(centers):
        tree.insert((0., y, z), index)
    tree.balance()
    steps = .004 * height
    votes = defaultdict(list)
    for i, (x, y, z) in enumerate(centers):
        if masses[i] == 0:
            continue
        for _, j, _ in tree.find_range((0., y, z), .012 * height):
            if (
                j <= i or abs(x - centers[j, 0]) < .045 * height
                or min(masses[i], masses[j]) < .65 * max(masses[i], masses[j])
                or not memberships[slots[i]].intersection(memberships[slots[j]])
            ):
                continue
            midpoint = (x + centers[j, 0]) * .5
            if low + .23 * (high - low) < midpoint < high - .23 * (high - low):
                votes[round((midpoint - median) / steps)].append(midpoint)
    leading = sorted(votes.values(), key=lambda points: (-len(points), abs(np.median(points) - median)))[:4]
    planes = [box_center, median, *(float(np.median(points)) for points in leading)]
    evaluations = [(_find_mirrors(centers, masses, memberships, slots, x, height), x) for x in planes]
    mirrors, plane = max(evaluations, key=lambda evaluation: len(evaluation[0]))
    if len(mirrors) < 4:
        return box_center, _find_mirrors(centers, masses, memberships, slots, box_center, height)
    return plane, mirrors


def _find_limb_chains(positions, centers, masses, spine, mirrors, shared, middle, height):
    """Identify mirrored limb landmarks on upright humanoid bind poses."""
    low, high = positions.min(axis=0), positions.max(axis=0)
    width = min(middle - low[0], high[0] - middle)
    if len(spine) < 4 or not .17 < width / height < .65:
        return [], []
    side = np.array([i for i in sorted(mirrors) if centers[i, 0] > middle])
    if len(side) < 10:
        return [], []
    lateral = (centers[side, 0] - middle) / width
    vertical = (centers[side, 2] - low[2]) / height
    depth = centers[side, 1]
    importance = .035 * np.log1p(masses[side] / max(np.median(masses[side]), 1))

    # Estimate both sets of landmarks independently. A workspace may contain
    # only the upper body, or have toes weighted to the foot alone.
    arm, leg = [], []
    reach = np.abs(positions[:, 0] - middle)
    end = positions[(reach > .89 * width) & (reach < 1.05 * width) & (positions[:, 2] > low[2] + .43 * height)]
    if len(end) > 8:
        tip_depth, tip_height = np.median(end[:, 1:], axis=0)
        shoulder_height = centers[spine[-2], 2] - .03 * height
        for fraction in (.16, .26, .45, .745, .815):
            expected_height = shoulder_height + (tip_height - shoulder_height) * max(0., (fraction - .17) / .78)
            dz = (centers[side, 2] - expected_height) / height
            dx = lateral - fraction
            dy = (depth - tip_depth) / height
            valid = (np.abs(dx) < .11) & (np.abs(dz) < .115) & (np.abs(dy) < .115)
            if arm:
                valid &= centers[side, 0] > centers[arm[-1], 0] + .025 * width
            if not valid.any():
                arm = []
                break
            score = (dx / .09) ** 2 + (dz / .10) ** 2 + (dy / .075) ** 2 - importance
            arm.append(int(side[np.argmin(np.where(valid, score, np.inf))]))
        if arm:
            arm.insert(0, spine[-3])

    feet = positions[
        (positions[:, 2] < low[2] + .10 * height)
        & (positions[:, 0] > middle + .02 * width) & (positions[:, 0] < middle + .62 * width)
    ]
    if len(feet) > 8:
        leg_x = float(np.median(feet[:, 0]))
        foot_depth = float(np.median(feet[:, 1]))
        toe_depth = float(np.quantile(feet[:, 1], .12))
        for fraction in (.55, .43, .23, .038, .015):
            toe = fraction < .02
            width_target = middle + .65 * (leg_x - middle) if not leg else leg_x
            dx = (centers[side, 0] - width_target) / height
            dy = (depth - (toe_depth if toe else foot_depth)) / height
            dz = vertical - fraction
            tolerance = .053 if fraction < .10 else .103
            valid = (np.abs(dx) < .10) & (np.abs(dy) < .11) & (np.abs(dz) < tolerance)
            if leg:
                valid &= (side != leg[-1]) & (centers[side, 2] < centers[leg[-1], 2])
            if toe:
                valid &= depth < centers[leg[-1], 1] - .012 * height
            if not valid.any():
                if not toe:
                    leg = []
                break
            score = (dx / .065) ** 2 + (dy / (.024 if toe else .075)) ** 2 + (dz / tolerance) ** 2 - importance
            if not leg:
                support = np.array([
                    shared.get(tuple(sorted((spine[0], i))), np.zeros(4))[3] for i in side
                ])
                score -= .55 * np.log1p(12 * support / np.maximum(masses[side], 1))
            leg.append(int(side[np.argmin(np.where(valid, score, np.inf))]))
        if leg:
            leg.insert(0, spine[0])
    if set(leg[1:]).intersection(arm[1:]):
        arm = []
    return arm, leg


def _layout_hierarchy(centers, masses, spine, mirrors, shared, contacts, middle, height, slot_ibs, limbs=()):
    representative = [min(i, mirrors.get(i, i)) for i in range(len(centers))]
    adjacency = defaultdict(dict)

    def link(a, b, cost):
        a, b = representative[a], representative[b]
        if a != b and cost < adjacency[a].get(b, float("inf")):
            adjacency[a][b] = adjacency[b][a] = cost

    for a, b in shared.keys() | contacts.keys():
        overlap = shared[a, b][3] if (a, b) in shared else 0
        overlap += contacts.get((a, b), 0)
        strength = overlap / max((masses[a] * masses[b]) ** .5, 1)
        distance = np.linalg.norm(centers[a] - centers[b])
        link(a, b, (distance + .012 * height) / (.08 + min(strength, 1) ** .5))

    ib_slots = defaultdict(list)
    for index, draw_ibs in enumerate(slot_ibs):
        for draw_ib in draw_ibs:
            ib_slots[draw_ib].append(index)
    for local in ib_slots.values():
        tree = KDTree(len(local))
        for index in local:
            tree.insert(centers[index], index)
        tree.balance()
        for index in local:
            for _, other, distance in tree.find_n(centers[index], min(len(local), 7)):
                link(index, other, 4.5 * (distance + .024 * height))

    parents = {child: parent for parent, child in pairwise(spine)}
    parents[spine[0]] = None
    distances = dict.fromkeys(spine, 0.0)
    for chain in limbs:
        for parent, child in pairwise(chain):
            a, b = representative[parent], representative[child]
            parents[b] = a
            distances[b] = 0.0
    anchors = tuple(sorted(distances))
    anchor_points = centers.copy()
    anchor_points[spine, 0] = middle
    anchor_points[spine, 1] = np.median(centers[spine, 1])
    unattached = set(representative) - distances.keys()
    median_mass = max(1., np.median(masses[masses > 0]))

    # Join a separate weighted region once at a supported root. A hair tip
    # should stay in its hair chain even if it is nearer the sleeve.
    attachments = {}
    for child in unattached:
        allowed = [
            parent for parent in anchors
            if child not in mirrors or parent in mirrors or parent in spine
        ]
        parent = min(allowed, key=lambda i: np.linalg.norm(centers[child] - anchor_points[i]))
        gap = np.linalg.norm(centers[child] - anchor_points[parent])
        support = np.log1p(masses[child] / median_mass)
        attachments[child] = ((gap + .06 * height) / (1 + .45 * support), parent)

    pending = [(0., root) for root in sorted(distances)]
    while pending or unattached:
        if not pending:
            root = min(unattached, key=lambda i: (attachments[i][0], i))
            distances[root], parents[root] = attachments[root]
            heappush(pending, (distances[root], root))
            unattached.remove(root)
        while pending:
            distance, current = heappop(pending)
            if distance != distances[current]:
                continue
            for child, cost in adjacency[current].items():
                if (
                    child in mirrors and current not in mirrors and current not in spine
                    and abs(centers[current, 0] - middle) > height * .009
                ):
                    continue
                new_distance = distance + cost
                if new_distance < distances.get(child, float("inf")):
                    distances[child] = new_distance
                    parents[child] = current
                    unattached.discard(child)
                    heappush(pending, (new_distance, child))

    result = {}
    for index, rep in enumerate(representative):
        parent = parents[rep]
        if parent in mirrors and (centers[index, 0] - middle) * (centers[parent, 0] - middle) < 0:
            parent = mirrors[parent]
        result[index] = parent
    return result


def _estimated_joint(prior, parent_slot, slot, part_lookup, height):
    # A joint shared by two transforms maps to approximately the same point.
    # Retain the weight prior along rotation axes and when a capture is static.
    sources = [part.palette for part in part_lookup[slot] if parent_slot in part.palette]
    if not sources:
        return prior
    differences = np.unique([p[parent_slot] - p[slot] for p in sources], axis=0)
    linear = differences[:, :, :3].reshape(-1, 3)
    translation = -differences[:, :, 3].ravel()
    singular = np.linalg.svd(linear, compute_uv=False)
    if singular[1] < .035:
        return prior
    damping = max(.03, .12 * singular[0])
    candidate = np.linalg.lstsq(
        np.vstack((linear, damping * np.eye(3))),
        np.r_[translation, damping * prior], rcond=None,
    )[0]
    if np.linalg.norm(candidate - prior) <= height * .038:
        return candidate
    return prior


def _split_elbow_landmark(arm, leg, mirrors, centers, masses, shared, parts, slots, height):
    """Keep an upper-arm segment when the adjacent weights identify a distinct forearm."""
    if not arm:
        return arm
    shoulder, landmark, wrist = arm[2:5]
    by_side = (
        (shoulder, landmark, wrist),
        (mirrors[shoulder], mirrors[landmark], mirrors[wrist]),
    )
    downstream = np.zeros((2, len(slots)))
    for side, (start, middle, end) in enumerate(by_side):
        axis = centers[end] - centers[start]
        length_sq = float(axis @ axis)
        if length_sq < (.10 * height) ** 2:
            return arm
        if (centers[middle] - centers[start]) @ axis / length_sq >= .50:
            return arm
        for part in parts:
            offsets = part.positions - centers[start]
            t = offsets @ axis / length_sq
            radial = np.linalg.norm(offsets - t[:, None] * axis, axis=1)
            mask = (t > .51) & (t < .85) & (radial < .04 * height)
            indices = np.searchsorted(slots, part.indices[mask])
            downstream[side] += np.bincount(
                indices.ravel(), part.weights[mask].ravel(), minlength=len(slots),
            )

    reserved = set(arm) | set(leg)
    candidates = []
    for candidate, mate in mirrors.items():
        if candidate in reserved or mate in reserved:
            continue
        scores = []
        for side, (near, far) in enumerate(((landmark, candidate), (mirrors[landmark], mate))):
            overlap = shared.get(tuple(sorted((near, far))))
            minimum_mass = min(masses[near], masses[far])
            if minimum_mass <= 0 or overlap is None or overlap[3] < .08 * minimum_mass:
                break
            coverage = downstream[side, far] / masses[far]
            if coverage < .65 or downstream[side, near] > .25 * masses[near]:
                break
            scores.append(coverage * overlap[3] / np.sqrt(masses[near] * masses[far]))
        if len(scores) == 2:
            candidates.append((min(scores), candidate))
    if not candidates:
        return arm
    _, forearm = max(candidates)
    return [*arm[:4], forearm, *arm[4:]]


def _upper_arm_helpers(parts, slots, heads, parents, arm, mirrors, height):
    """Find downstream helpers whose original weight is on the upper arm."""
    if not arm:
        return {}
    shoulder = arm[2]
    elbow, wrist, palm = arm[-3:]
    by_side = ((shoulder, elbow, wrist, palm), tuple(mirrors[i] for i in (shoulder, elbow, wrist, palm)))
    main = set(arm) | {mirrors[i] for i in arm[1:]}
    total = np.zeros(len(slots))
    proximal = np.zeros((2, len(slots)))
    for part in parts:
        indices = np.searchsorted(slots, part.indices)
        weights = part.weights
        total += np.bincount(indices.ravel(), weights.ravel(), minlength=len(slots))
        for side, (upper, lower, _, _) in enumerate(by_side):
            axis = heads[lower] - heads[upper]
            length_sq = float(axis @ axis)
            if length_sq < (height * .035) ** 2:
                continue
            offsets = part.positions - heads[upper]
            t = offsets @ axis / length_sq
            radial = np.linalg.norm(offsets - t[:, None] * axis, axis=1)
            mask = (t > .08) & (t < .96) & (radial < .025 * height)
            proximal[side] += np.bincount(
                indices[mask].ravel(), weights[mask].ravel(), minlength=len(slots),
            )

    proposals = {}
    for side, (upper, lower, wrist, palm) in enumerate(by_side):
        for index in np.flatnonzero((proximal[side] >= .72 * total) & (total > 0)):
            if index in main:
                continue
            ancestor = parents[index]
            while ancestor is not None and ancestor not in main:
                ancestor = parents[ancestor]
            if ancestor in (lower, wrist, palm):
                proposals[int(index)] = upper
    return {
        index: parent for index, parent in proposals.items()
        if index not in mirrors or proposals.get(mirrors[index]) == mirrors[parent]
    }


def reconstruct_bones(parts: list[WorkspacePart]):
    slots, masses, centers, memberships, shared, contacts = _add_evidence(parts)
    if not np.any(masses > 0):
        raise Fatal("所选 LOD 的 IB 没有正权重顶点")
    by_ib = defaultdict(list)
    part_lookup = defaultdict(list)
    for part in parts:
        by_ib[part.draw_ib].append(part.positions)
        for slot in part.palette:
            part_lookup[slot].append(part)
    geometry = {ib: np.concatenate(rows) for ib, rows in by_ib.items()}
    body = max(geometry, key=lambda ib: np.ptp(geometry[ib][:, 2]))
    positions = geometry[body]
    low, high = positions.min(axis=0), positions.max(axis=0)
    height = float(high[2] - low[2])
    if height < 1e-5:
        raise Fatal("工作区网格没有可用于推断骨架的高度")
    middle, mirrors = _symmetry_plane(positions, centers, masses, memberships, slots, height)
    pelvis = low[2] + height * .53
    spine = [i for i, slot in enumerate(slots) if i not in mirrors
             and body in memberships[slot] and masses[i] > 0
             and abs(centers[i, 0] - middle) < .018 * height and centers[i, 2] > pelvis]
    spine.sort(key=lambda i: centers[i, 2])
    if not spine:
        spine = [int(np.argmin(np.linalg.norm(centers - [middle, centers[:, 1].mean(), pelvis], axis=1)))]
        mate = mirrors.pop(spine[0], None)
        if mate is not None:
            del mirrors[mate]
    arm, leg = _find_limb_chains(positions, centers, masses, spine, mirrors, shared, middle, height)
    arm = _split_elbow_landmark(arm, leg, mirrors, centers, masses, shared, parts, slots, height)
    # Fold both sides into the same graph paths. Auxiliary bones may branch
    # from a landmark, but cannot become parents between those landmarks.
    limbs = [chain for chain in (arm, leg) if chain]
    parents = _layout_hierarchy(
        centers, masses, spine, mirrors, shared, contacts, middle, height,
        [memberships[slot] for slot in slots], limbs,
    )

    heads = centers.copy()
    def estimate(index, parent):
        edge = shared.get(tuple(sorted((index, parent))))
        prior = edge[:3] / edge[3] if edge is not None else .64 * centers[index] + .36 * centers[parent]
        return _estimated_joint(prior, slots[parent], slots[index], part_lookup, height)

    for index, parent in parents.items():
        if parent is not None:
            heads[index] = estimate(index, parent)
    # The weighted surface centroid is usually inside the segment, past the
    # joint. The shared-weight transition gives a better spine head height.
    depth = float(np.median(centers[spine, 1]))
    if leg:
        root = spine[0]
        hips = (leg[1], mirrors[leg[1]])
        boundaries = [shared[tuple(sorted((root, hip)))] for hip in hips if tuple(sorted((root, hip))) in shared]
        if boundaries:
            z = sum(edge[2] for edge in boundaries) / sum(edge[3] for edge in boundaries)
            heads[root, 2] = .65 * heads[root, 2] + .35 * z
    for previous, index in pairwise(spine):
        if heads[index, 2] <= heads[previous, 2] + .01 * height:
            heads[index, 2] = max(centers[index, 2], heads[previous, 2] + .01 * height)
    heads[spine, 0] = middle
    heads[spine, 1] = depth
    if arm:
        shoulder = arm[2]
        elbow, wrist, palm = arm[-3:]
        for side in (lambda i: i, mirrors.__getitem__):
            upper, lower = heads[side(shoulder)], heads[side(palm)]
            # Fix ambiguous single-frame pivots on a straight arm using the
            # span of the supported upper arm and palm.
            elbow_target = upper + .56 * (lower - upper)
            wrist_target = upper + .89 * (lower - upper)
            for idx, target in ((side(elbow), elbow_target), (side(wrist), wrist_target)):
                delta = heads[idx] - target
                limit = .018 * height
                heads[idx] = target + delta * min(1., limit / max(np.linalg.norm(delta), 1e-9))
    if leg:
        for hip in (leg[1], mirrors[leg[1]]):
            heads[hip, 0] = .75 * centers[hip, 0] + .25 * heads[hip, 0]
        if len(leg) == 6:
            for toe in (leg[-1], mirrors[leg[-1]]):
                edge = shared.get(tuple(sorted((toe, parents[toe]))))
                if edge is not None:
                    heads[toe, 2] = edge[2] / edge[3]
    for index, parent in _upper_arm_helpers(parts, slots, heads, parents, arm, mirrors, height).items():
        parents[index] = parent
        heads[index] = estimate(index, parent)
    for left, right in mirrors.items():
        if centers[left, 0] > middle:
            point = (heads[left] + [-heads[right, 0] + 2 * middle, *heads[right, 1:]]) / 2
            heads[left] = point
            heads[right] = (2 * middle - point[0], *point[1:])

    tails = heads.copy()
    children = defaultdict(list)
    for index, parent in parents.items():
        if parent is not None:
            children[parent].append(index)
    leading = dict(pairwise(spine))
    for chain in limbs:
        for a, b in pairwise(chain[1:]):
            leading[a] = b
            leading[mirrors[a]] = mirrors[b]
    for index in parents:
        if index in leading:
            direction = heads[leading[index]] - heads[index]
        elif children[index] and index not in spine:
            descendants = children[index]
            vectors = heads[descendants] - heads[index]
            target = centers[index] - heads[index]
            lengths = np.linalg.norm(vectors, axis=1) * np.linalg.norm(target)
            alignment = (vectors @ target) / np.maximum(lengths, 1e-9)
            scores = alignment + .25 * np.log1p(masses[descendants])
            direction = vectors[scores.argmax()]
        elif index in spine:
            direction = np.array([0., 0., height * .055])
        else:
            direction = centers[index] - heads[index]
        length = np.linalg.norm(direction)
        if length < height * .002:
            direction = np.array([0., 0., height * .009])
        tails[index] += direction
    for left, right in mirrors.items():
        if centers[left, 0] > middle:
            point = (tails[left] + [-tails[right, 0] + 2 * middle, *tails[right, 1:]]) / 2
            if np.linalg.norm(point - heads[left]) < height * .002:
                point = heads[left] + [0., 0., height * .009]
            tails[left] = point
            tails[right] = (2 * middle - point[0], *point[1:])
    return slots, heads, tails, parents, mirrors, memberships


def _create_armature(context, collection, parts, workspace: Path, *, mirror_x=False):
    if context.mode != "OBJECT":
        raise Fatal("请切换到物体模式后重建骨架")
    slots, heads, tails, parents, mirrors, memberships = reconstruct_bones(parts)
    if mirror_x:
        heads[:, 0] *= -1
        tails[:, 0] *= -1

    name = f"{collection.name} · 骨架"
    armature = bpy.data.armatures.new(name)
    rig = bpy.data.objects.new(name, armature)
    collection.objects.link(rig)
    active = context.view_layer.objects.active
    selected = list(context.selected_objects)
    try:
        for obj in selected:
            obj.select_set(False)
        rig.select_set(True)
        context.view_layer.objects.active = rig
        bpy.ops.object.mode_set(mode="EDIT")
        for index, slot in enumerate(slots):
            bone = armature.edit_bones.new(str(slot))
            bone.head = tuple(heads[index])
            bone.tail = tuple(tails[index])
            direction = (bone.tail - bone.head).normalized()
            reference = (0., -1., 0.) if abs(direction.y) < .93 else (0., 0., 1.)
            bone.align_roll(Vector(reference))
        for index, parent in parents.items():
            if parent is not None:
                armature.edit_bones[str(slots[index])].parent = armature.edit_bones[str(slots[parent])]
        bpy.ops.object.mode_set(mode="OBJECT")

        armature.display_type = "STICK"
        rig.show_in_front = True
        bone_collections = {}
        if hasattr(armature, "collections"):
            for ib in sorted({part.draw_ib for part in parts}):
                bone_collections[ib] = armature.collections.new(ib)
        for index, slot in enumerate(slots):
            bone = armature.bones[str(slot)]
            parent = parents[index]
            bone[WORKSPACE_IMPORT_BONE_PARENT] = slots[parent] if parent is not None else -1
            bone["DrawIB"] = ", ".join(sorted(memberships[slot]))
            if index in mirrors:
                bone["Mirror slot"] = slots[mirrors[index]]
            for ib in memberships[slot]:
                if ib in bone_collections:
                    bone_collections[ib].assign(bone)
        rig["Workspace"] = str(workspace)
        rig[WORKSPACE_IMPORT_RIG_ROOT] = slots[next(index for index, parent in parents.items() if parent is None)]
        rig["IB count"] = len({part.draw_ib for part in parts})
        rig["Submesh count"] = len(parts)
        context.view_layer.update()
    except Exception:
        if rig.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(rig, do_unlink=True)
        bpy.data.armatures.remove(armature)
        context.view_layer.objects.active = active
        for obj in selected:
            obj.select_set(True)
        raise
    return rig


def _bind_meshes(rig, meshes):
    attached = []
    try:
        for obj in meshes:
            world = obj.matrix_world.copy()
            modifier = obj.modifiers.new("原始权重", "ARMATURE")
            attached.append((obj, modifier, obj.parent, obj.matrix_parent_inverse.copy(), world))
            modifier.object = rig
            obj.parent = rig
            obj.matrix_world = world
    except Exception:
        for obj, modifier, parent, inverse, world in reversed(attached):
            obj.modifiers.remove(modifier)
            obj.parent = parent
            obj.matrix_parent_inverse = inverse
            obj.matrix_world = world
        raise


def bind_imported_workspace_rigs(context, workspace: Path, records):
    """Build one rig per imported LOD collection and bind the imported meshes."""
    from .non_mirror_workflow import NonMirrorWorkflowHelper

    groups = defaultdict(list)
    for record in records:
        source = SubmeshJson(record["json_file_path"])
        if source.GPU_PreSkinning and source.VGCount and source.MergedSkeletonMetadataValid:
            groups[record["import_collection"]].append(
                (_load_part(record["import_key"], source), record["imported_obj"])
            )

    rigs = []
    for collection, entries in groups.items():
        parts, meshes = zip(*entries)
        mirror_states = {NonMirrorWorkflowHelper._should_restore_export_object(obj) for obj in meshes}
        if len(mirror_states) != 1:
            raise Fatal(f"{collection.name}：导入网格的非镜像处理结果不一致")
        mirror_x = mirror_states.pop()
        rig = _create_armature(context, collection, parts, workspace, mirror_x=mirror_x)
        try:
            _bind_meshes(rig, meshes)
        except Exception:
            armature = rig.data
            bpy.data.objects.remove(rig, do_unlink=True)
            bpy.data.armatures.remove(armature)
            raise
        rig[WORKSPACE_IMPORT_RIG_MARKER] = True
        rig[WORKSPACE_IMPORT_RIG_MIRROR_X] = mirror_x
        rigs.append(rig)
    return rigs
