"""Bake a continuous single-limb cloth track in garment bind space."""

import numpy as np

from .drag_cloth_proxy_core import BodySections, Followers, SleeveSolver, islands, local_frames


def bake_limb_track(positions, normals, faces, body_faces, weights, bones, path, base, compression_frame=None):
    from mathutils.kdtree import KDTree

    rows = np.unique(faces)
    faces = np.searchsorted(rows, faces)
    body_rows = np.unique(body_faces)
    origin, endpoint = np.asarray(path, float)
    distance = float(np.linalg.norm(origin - endpoint))
    axis = (origin - endpoint) / distance
    side = np.eye(3)[int(np.argmin(np.abs(axis)))]
    motion = "track"
    if compression_frame is not None:
        origin = positions[rows].mean(axis=0)
        axis, side = np.array(compression_frame, dtype=float)
        axis /= np.linalg.norm(axis)
        motion = "compress"
    side -= np.dot(side, axis) * axis
    side /= np.linalg.norm(side)
    frame = np.column_stack((side, np.cross(axis, side), axis))
    vertices = (positions[rows] - origin) @ frame
    body = (positions[body_faces] - origin) @ frame
    lo, hi = vertices[:, 2].min(), vertices[:, 2].max()
    if motion == "compress" and distance >= .97 * (hi - lo):
        raise ValueError("代理穿脱箭头超过衣物的可压缩行程，可改用沿肢体整体滑移")
    lower = lo if motion == "compress" else lo - distance
    sections = BodySections(body, vertices, lower - .001, hi + .001, 64, extend_lower=motion == "track")
    clearance = float(np.clip(np.median(sections.gap(vertices)), .002, .012))
    solver = SleeveSolver(sections, lo, hi, 15, 40, clearance, distance, 0.0, motion)
    labels = islands(vertices, faces)
    rigid = [int(i) for i in np.unique(labels)
             if np.linalg.norm(np.ptp(vertices[labels == i], axis=0)) < (hi - lo) * .16]
    followers = Followers(vertices, labels, solver, rigid)
    used_bones = bones[body_rows][weights[body_rows] > 0]
    if not len(used_bones):
        raise ValueError("沿肢体整体滑移的碰撞体没有有效蒙皮权重")
    palette_size = int(used_bones.max()) + 1
    tree = KDTree(len(body_rows))
    for row in body_rows:
        tree.insert(positions[row], int(row))
    tree.balance()
    row_indices = np.arange(len(rows))
    source_normals = normals[rows] @ frame
    local_normals = np.einsum("ni,nij->nj", source_normals, followers.rest_frames)
    samples = 17
    track = np.zeros((samples, len(rows), 4, 4), "<f4")
    neighbor_count = min(8, len(body_rows))
    for index, level in enumerate(np.linspace(0, 1, samples)):
        for _ in solver.advance(level):
            pass
        mesh = followers.publish(solver.current) @ frame.T + origin
        patches = local_frames(solver.current[followers.ids], followers.u, followers.v)
        display_normals = np.einsum("nij,nj->ni", patches, local_normals) @ frame.T
        if not np.isfinite(mesh).all() or not np.isfinite(display_normals).all():
            raise ValueError("沿肢体整体滑移生成了无效形变")
        neighbors = [tree.find_n(point, neighbor_count) for point in mesh]
        donors = np.asarray([[row for _, row, _ in group] for group in neighbors])
        separation = np.asarray([[distance for _, _, distance in group] for group in neighbors])
        influence = 1 / np.maximum(separation, .002) ** 2
        donor_weights = weights[donors] * influence[:, :, None]
        donor_bones = np.where(donor_weights > 0, bones[donors], 0)
        merged = np.zeros((len(rows), palette_size), np.float32)
        np.add.at(merged, (row_indices[:, None, None], donor_bones), donor_weights)
        picks = np.argsort(-merged, axis=1)[:, :4]
        values = np.take_along_axis(merged, picks, axis=1)
        total = values.sum(axis=1, keepdims=True)
        if np.any(total <= 0):
            raise ValueError("沿肢体整体滑移无法从碰撞体继承蒙皮")
        track[index, :, 0, :3] = mesh
        track[index, :, 1, :3] = display_normals
        track[index, :, 2, :picks.shape[1]] = picks.astype("<u4").view("<f4")
        track[index, :, 3, :picks.shape[1]] = values / total
    return (rows + base).astype("<u4"), track
