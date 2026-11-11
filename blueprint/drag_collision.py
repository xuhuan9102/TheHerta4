"""Static ordering for the live drag-collision BVH (geometry is refit on GPU)."""

import numpy as np


def bake_cloth_adjacency(triangles, participation):
    """CSR incident faces, including fixed vertices on the controlled boundary."""
    mask = np.asarray(participation, dtype=bool)
    triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if len(triangles) and (triangles.min() < 0 or triangles.max() >= len(mask)):
        raise ValueError("Cloth triangle outside runtime vertex domain")
    faces = triangles[np.any(mask[triangles], axis=1)].astype(np.uint32)
    flat = faces.ravel()
    counts = np.bincount(flat, minlength=len(mask))
    offsets = np.r_[0, np.cumsum(counts)].astype(np.uint32)
    refs = np.repeat(np.arange(len(faces), dtype=np.uint32), 3)[np.argsort(flat, kind="stable")]
    # Header: vertex count, face count, followed by V+1 offsets and 3*F face ids.
    csr = np.r_[np.uint32(len(mask)), np.uint32(len(faces)), offsets, refs].astype(np.uint32)
    return faces, csr


def sweep_shader_sections(suffix, leaves, dispatch_n, shader_dir="res/drag_interaction"):
    """INI dispatch chain shared by the exporter and existing-mod installer."""
    tiles = leaves // 32
    order = f"ResourceDragCollisionOrder_{suffix}"
    bounds = f"ResourceDragCollisionBounds_{suffix}"
    sections = {}
    for name, roots, groups in (("Leaves", 0, tiles), ("Roots", 1, max(1, (tiles + 30) // 32))):
        # Later dispatches consume these UAV writes through the named resource.
        # A copied UAV would lose the updated bounds when it is unbound.
        sections[f"[CustomShaderDragCollision{name}{suffix}]"] = [
            f"cs = {shader_dir}/rzm_drag_collision_bounds.hlsl",
            "x115 = 1", f"y115 = {leaves}", f"z115 = {roots}",
            f"cs-t89 = copy {order}", "cs-t91 = null", f"cs-u4 = ref {bounds}",
            f"Dispatch = {groups}, 1, 1", "cs-u4 = null",
        ]
    sections[f"[CustomShaderDragCollision{suffix}]"] = [
        f"cs = {shader_dir}/rzm_drag_collision.hlsl",
        "x115 = 1", f"y115 = {leaves}", "z115 = 0",
        f"cs-t89 = copy {order}", f"cs-t91 = copy {bounds}",
        f"Dispatch = {dispatch_n}, 1, 1", "cs-t89 = null", "cs-t91 = null",
    ]
    return sections


def surface_shader_sections(suffix, leaves, face_count, dispatch_n, shader_dir="res/drag_interaction"):
    sections = sweep_shader_sections(suffix, leaves, dispatch_n, shader_dir)
    del sections[f"[CustomShaderDragCollision{suffix}]"]
    for stage, phase, groups in (("Surface", 0, (face_count + 255) // 256), ("Apply", 2, dispatch_n)):
        sections[f"[CustomShaderDragCollision{stage}{suffix}]"] = [
            f"cs = {shader_dir}/rzm_drag_collision_surface.hlsl",
            "x115 = 1", f"y115 = {leaves}", f"z115 = {phase}",
            f"cs-t89 = copy ResourceDragCollisionOrder_{suffix}",
            f"cs-t91 = copy ResourceDragCollisionBounds_{suffix}",
            f"cs-t92 = copy ResourceDragCollisionCloth_{suffix}",
            f"cs-t93 = copy ResourceDragCollisionAdjacency_{suffix}",
            f"cs-u4 = ref ResourceDragCollisionFaceLimits_{suffix}",
            f"Dispatch = {max(groups, 1)}, 1, 1", "cs-u4 = null",
            "cs-t89 = null", "cs-t91 = null", "cs-t92 = null", "cs-t93 = null",
        ]
    return sections


def bake_sweep_order(triangle_positions):
    """Return a complete Morton-ordered permutation and the padded heap size.

    No triangles are discarded. The shader rebuilds bounds from the current
    pre-drag VB0, so this ordering affects traversal cost, never coverage.
    """
    triangles = np.asarray(triangle_positions, dtype=np.float64)
    count = len(triangles)
    if count == 0:
        return np.empty(0, dtype=np.uint32), 0
    centers = triangles[:, :3, :3].mean(axis=1)
    if not np.all(np.isfinite(centers)):
        raise ValueError("Collision geometry contains non-finite positions")
    lo = centers.min(axis=0)
    extent = np.maximum(centers.max(axis=0) - lo, 1e-12)
    coordinates = np.clip((centers - lo) / extent * 1023, 0, 1023).astype(np.uint32)
    keys = np.zeros(count, dtype=np.uint32)
    for bit in range(10):
        for axis in range(3):
            keys |= ((coordinates[:, axis] >> bit) & 1) << (3 * bit + axis)
    order = np.argsort(keys, kind="stable").astype(np.uint32)
    # Each leaf-build group owns a complete subtree of 32 triangles.
    leaf_count = max(32, 1 << (count - 1).bit_length())
    return order, leaf_count
