"""Explicit garment travel handles, independent of the Blender UI."""

import numpy as np


def path_points(settings):
    start = getattr(settings, "cloth_path_start", None)
    end = getattr(settings, "cloth_path_end", None)
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError("穿脱箭头需要同时指定起点和端点")
    points = np.array(
        [np.asarray(obj.matrix_world, float)[:3, 3] for obj in (start, end)]
    )
    if not np.isfinite(points).all() or np.linalg.norm(points[1] - points[0]) < 1e-6:
        raise ValueError("穿脱箭头的起点和端点不能重合，坐标必须有效")
    return points


def exported_path(settings, reference=None, export=None, mirror=None):
    points = path_points(settings)
    if points is None:
        return None
    matrix = np.eye(4)
    # Match _evaluate_zone_field: export * reference^-1 * mirror * world.
    for transform in (mirror, reference, export):
        if transform is not None:
            matrix = np.asarray(transform, float).reshape(4, 4) @ matrix
    transformed = np.c_[points, np.ones(2)] @ matrix.T
    return transformed[:, :3]


def suggest_path(points, faces, center, direction="NEGATIVE", body=None):
    """Initial suggestion only; endpoint handles remain the user's authority."""
    from .drag_cloth_geometry import _boundary_loops

    points = np.asarray(points, float)
    _, loops = _boundary_loops(faces)
    if not loops:
        raise ValueError("未找到衣物开口，请手动指定箭头起点和端点")
    centers = np.array([points[ring].mean(0) for ring in loops])
    distance = np.linalg.norm(centers - center, axis=1)
    chosen = int(np.argmin(distance))
    start = centers[chosen]
    # Pair symmetric sock cuffs at the same level for a shared region handle.
    nearby = abs(centers[:, 2] - start[2]) < max(np.ptp(points[:, 2]) * 0.03, 0.001)
    start = centers[nearby].mean(0)
    up = np.array([0.0, 0.0, 1.0])
    sign = 1 if direction == "POSITIVE" else -1
    height = max(float(np.ptp(points @ up)), 0.01)
    length = height * 0.75
    if sign > 0 and len(loops) >= 4:
        above = centers[:, 2] > start[2] + height * 0.2
        if above.any():
            length = max(
                0.01, float(centers[above, 2].min() - start[2] - height * 0.08)
            )
    elif sign < 0 and len(loops) == 3 and body is not None:
        body = np.asarray(body, float)
        calf = body[:, 2].min() + np.ptp(body[:, 2]) * 0.06 + height
        length = max(0.01, float(start[2] - calf))
    return np.array([start, start + sign * up * length])
