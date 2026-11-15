"""Independent geometry audit of a generated-cloth GPU replay snapshot.

    python tools/check_drag_cloth_mesh.py replay.npz --report result.json

Snapshots contain source/output vertex rows, a cloth mask, body index triples
and render index triples. This tool checks the rendered mesh, not solver health
or just its proxy particles. Input intersections are reported separately and
never make a penetrating output pass.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.check_drag_collision_sweep import surface_intersections


def winding_numbers(points, triangles, batch_size=16):
    """Solid-angle oracle, without the solver's BVH, normals or region labels.

    Consistently oriented closed overlapping solids retain winding > 0.5 in
    their union. Parity and the nearest face's normal do not have this property.
    Open/corrupt collider meshes need separate inspection of their boundaries.
    """
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    triangles = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    result = np.empty(len(points), dtype=np.float64)
    for begin in range(0, len(points), batch_size):
        relative = triangles[None] - points[begin : begin + batch_size, None, None]
        a, b, c = (relative[:, :, k] for k in range(3))
        la, lb, lc = (np.linalg.norm(v, axis=2) for v in (a, b, c))
        numerator = np.einsum("bti,bti->bt", a, np.cross(b, c))
        denominator = (
            la * lb * lc
            + np.einsum("bti,bti->bt", a, b) * lc
            + np.einsum("bti,bti->bt", b, c) * la
            + np.einsum("bti,bti->bt", c, a) * lb
        )
        result[begin : begin + len(relative)] = np.arctan2(numerator, denominator).sum(
            axis=1
        ) / (2 * np.pi)
    return result


def audit(
    source,
    output,
    mask,
    body,
    triangles,
    *,
    maximum_stretch=3.0,
    volume_body=None,
    collision_vertices=None,
    material_reference=None,
    material_scale=1.0,
):
    source, output = np.asarray(source), np.asarray(output)
    mask = np.asarray(mask, dtype=bool)
    body, triangles = (
        np.asarray(value, dtype=np.int64).reshape(-1, 3) for value in (body, triangles)
    )
    if (
        source.ndim != 2
        or source.shape != output.shape
        or source.shape[1] < 3
        or mask.shape != (len(source),)
        or not np.any(mask)
        or not len(body)
        or not len(triangles)
    ):
        raise ValueError("Snapshot has incompatible or empty vertex/index arrays")
    if min(body.min(), triangles.min()) < 0 or max(body.max(), triangles.max()) >= len(
        source
    ):
        raise ValueError("Snapshot indices are outside the vertex buffer")
    mixed = np.any(mask[triangles], axis=1) & ~np.all(mask[triangles], axis=1)
    if np.any(mixed):
        raise ValueError("Render triangles share cloth and non-cloth vertices")
    faces = triangles[np.all(mask[triangles], axis=1)]
    if not len(faces):
        raise ValueError("Snapshot has no cloth render triangles")
    if np.any(mask[body]):
        raise ValueError("Collider and cloth use overlapping vertex rows")
    collision_vertices = (
        mask if collision_vertices is None else np.asarray(collision_vertices, bool)
    )
    if collision_vertices.shape != mask.shape or np.any(collision_vertices & ~mask):
        raise ValueError("Collision vertices must be a subset of the cloth rows")
    tested_faces = faces[np.any(collision_vertices[faces], axis=1)]
    volume_body = (
        body
        if volume_body is None
        else np.asarray(volume_body, np.int64).reshape(-1, 3)
    )
    if (
        not len(volume_body)
        or volume_body.min() < 0
        or volume_body.max() >= len(source)
    ):
        raise ValueError("Volume indices are outside the vertex buffer")
    result = {
        "finite": bool(np.isfinite(source).all() and np.isfinite(output).all()),
        "noncloth_unchanged": bool(np.array_equal(source[~mask], output[~mask])),
        "cloth_vertices": int(mask.sum()),
        "cloth_faces": len(faces),
        "body_faces": len(body),
        "collision_faces": len(tested_faces),
        "ignored_cloth_faces": len(faces) - len(tested_faces),
        "passed": False,
    }
    if not result["finite"]:
        return result
    collider = source[body, :3]
    source_hits = surface_intersections(source[tested_faces, :3], collider)
    output_hits = surface_intersections(output[tested_faces, :3], collider)
    volume = source[volume_body, :3]
    source_winding = winding_numbers(source[collision_vertices, :3], volume)
    output_winding = winding_numbers(output[collision_vertices, :3], volume)
    source_inside = np.abs(source_winding) > 0.50001
    output_inside = np.abs(output_winding) > 0.50001
    reference = source if material_reference is None else np.asarray(material_reference)
    if reference.shape != source.shape or not np.isfinite(reference).all():
        raise ValueError("Material reference must match the vertex buffer")
    old_length = (
        np.linalg.norm(
            reference[faces, :3] - reference[faces[:, [1, 2, 0]], :3], axis=2
        )
        * material_scale
    )
    pose_length = np.linalg.norm(
        source[faces, :3] - source[faces[:, [1, 2, 0]], :3], axis=2
    )
    new_length = np.linalg.norm(
        output[faces, :3] - output[faces[:, [1, 2, 0]], :3], axis=2
    )
    stretch = np.max(new_length / np.maximum(old_length, 1e-5), axis=1)
    motion = np.linalg.norm(output[mask, :3] - source[mask, :3], axis=1) * 1000
    result.update(
        source_intersections=len(source_hits),
        output_intersections=len(output_hits),
        new_intersections=len(output_hits - source_hits),
        new_intersection_face_ids=sorted(output_hits - source_hits),
        source_inside_vertices=int(source_inside.sum()),
        output_inside_vertices=int(output_inside.sum()),
        new_inside_vertices=int((output_inside & ~source_inside).sum()),
        maximum_edge_stretch=float(stretch.max()),
        maximum_pose_edge_change=float(
            np.max(new_length / np.maximum(pose_length, 1e-5))
        ),
        stretched_faces=int((stretch > maximum_stretch).sum()),
        median_motion_mm=float(np.median(motion)),
        p95_motion_mm=float(np.percentile(motion, 95)),
    )
    result["passed"] = bool(
        result["noncloth_unchanged"]
        and not output_hits
        and not output_inside.any()
        and not result["stretched_faces"]
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--maximum-stretch", type=float, default=3.0)
    args = parser.parse_args()
    if not np.isfinite(args.maximum_stretch) or args.maximum_stretch <= 1:
        parser.error("--maximum-stretch must be finite and greater than one")
    with np.load(args.snapshot, allow_pickle=False) as data:
        result = audit(
            *(data[name] for name in ("source", "output", "mask", "body", "triangles")),
            maximum_stretch=args.maximum_stretch,
            volume_body=data["volume_body"] if "volume_body" in data else None,
            collision_vertices=data["collision_vertices"]
            if "collision_vertices" in data
            else None,
            material_reference=data["bind"] if "bind" in data else None,
            material_scale=float(data["state"][8, 2]) if "state" in data else 1.0,
        )
        if "collision_scope" in data:
            result["collision_scope"] = str(data["collision_scope"])
        if "state" in data and len(data["state"]) > 23:
            result["solver_health"] = data["state"][23].tolist()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf8")
    print(text)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
