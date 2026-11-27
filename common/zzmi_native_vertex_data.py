"""Build ZZMI native vertex channels from the modified export mesh.

The native vertex layout contains more than POSITION, UV0, and COLOR.  The
missing channels must be reconstructed from the target mesh and, for authored
data such as TEXCOORD1, from the matching native surface:

* TEXCOORD2 is an affine X/Z projection.  Its coefficients are fitted from a
  native reference object and then applied to the modified geometry.
* NORMAL is the normalized per-vertex average of the target mesh's evaluated
  corner normals.
* TANGENT is calculated from the target mesh's final UV0, triangles, and
  reconstructed normals.  The point-domain XYZ/W attributes are then written
  for ObjBufferHelper.
* TEXCOORD1 is authored vertex data, not a function of POSITION.  A constant
  reference layer is copied as that constant; a varying layer is transferred
  by nearest source triangle and barycentric interpolation.

Nothing in this module copies the native per-vertex arrays wholesale.  The
reference object is only used to fit a mechanism or interpolate authored UV
data, while the target geometry supplies all modified positions and normals.
"""

import bmesh
import bpy
import numpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .mesh_attribute_names import (
    IMPORTED_NORMAL_XYZ,
    IMPORTED_TANGENT_W,
    IMPORTED_TANGENT_XYZ,
)
from .object_prefix_helper import ObjectPrefixHelper
from .zzmi_native_flow import UV2Projection, fit_uv2_projection


UV0_LAYER = "TEXCOORD.xy"
UV1_LAYER = "TEXCOORD1.xy"
UV2_LAYER = "TEXCOORD2.xy"
POSE_NORMAL_ATTRIBUTE = "_ssmt_pose_normal"
POSE_TANGENT_ATTRIBUTE = "_ssmt_pose_tangent"
POSE_SIGN_ATTRIBUTE = "_ssmt_pose_sign"


def _read_uv(mesh, name):
    layer = mesh.uv_layers.get(name)
    if layer is None or not len(layer.data):
        return None
    values = numpy.empty((len(layer.data), 2), dtype=numpy.float32)
    layer.data.foreach_get("uv", values.ravel())
    return values


def _read_loop_vertices(mesh):
    values = numpy.empty(len(mesh.loops), dtype=numpy.int32)
    mesh.loops.foreach_get("vertex_index", values)
    return values


def _read_positions(mesh):
    values = numpy.empty((len(mesh.vertices), 3), dtype=numpy.float32)
    mesh.vertices.foreach_get("co", values.ravel())
    return values


def _vector_attribute(mesh, name):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.domain != "POINT" or attribute.data_type != "FLOAT_VECTOR":
        return None
    values = numpy.empty((len(attribute.data), 3), dtype=numpy.float32)
    attribute.data.foreach_get("vector", values.ravel())
    return values


def _scalar_attribute(mesh, name):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.domain != "POINT" or attribute.data_type != "FLOAT":
        return None
    values = numpy.empty(len(attribute.data), dtype=numpy.float32)
    attribute.data.foreach_get("value", values)
    return values


def _valid_directions(values, vertex_count):
    if values is None or len(values) != vertex_count:
        return False
    return bool(
        numpy.isfinite(values).all()
        and numpy.any(numpy.linalg.norm(values, axis=1) > 1e-8)
    )


def _same_uv(a, b):
    return (
        a is not None
        and b is not None
        and a.shape == b.shape
        and numpy.allclose(a, b, rtol=0.0, atol=1e-6)
    )


def _uv_is_present(mesh, name):
    if mesh is None or mesh.uv_layers.get(name) is None:
        return False
    values = _read_uv(mesh, name)
    return values is not None and not numpy.all(numpy.abs(values) < 1e-7, axis=1).all()


def _mesh_prefix(obj):
    if obj is None:
        return ""
    parsed = ObjectPrefixHelper.extract_prefix_info(obj.name)
    if not parsed:
        return ""
    return str(ObjectPrefixHelper.parse_prefix_parts(parsed[0]).get("bare_unique_str", "") or "")


def _fit_reference_projection(obj):
    mesh = getattr(obj, "data", None)
    if mesh is None or not len(mesh.vertices) or not len(mesh.loops):
        return None
    uv = _read_uv(mesh, UV2_LAYER)
    if uv is None:
        return None
    vertices = _read_positions(mesh)
    loops = _read_loop_vertices(mesh)
    # The importer applies the non-mirror X flip to the mesh, while the
    # exported vertex buffer uses the original game-space X direction.
    # Convert the reference positions to buffer space before fitting.
    reference_positions = vertices[loops].copy()
    reference_positions[:, 0] *= -1.0
    return fit_uv2_projection(reference_positions, uv)


def _reference_candidates(target):
    target_prefix = _mesh_prefix(target)
    candidates = []
    for obj in bpy.data.objects:
        if obj == target or obj.type != "MESH" or not getattr(obj, "data", None):
            continue
        if target_prefix and _mesh_prefix(obj) != target_prefix:
            continue
        if not _uv_is_present(obj.data, UV2_LAYER):
            continue
        projection = _fit_reference_projection(obj)
        if projection is None:
            continue
        score = min(projection.r2_u, projection.r2_v)
        has_native = any(
            name in obj.data.attributes
            for name in (IMPORTED_NORMAL_XYZ, IMPORTED_TANGENT_XYZ, IMPORTED_TANGENT_W)
        )
        vertex_distance = abs(len(obj.data.vertices) - len(target.data.vertices))
        candidates.append((-vertex_distance, score, 1 if has_native else 0, obj.name, obj))
    candidates.sort(reverse=True, key=lambda item: (item[0], item[1], item[2], item[3]))
    return [item[4] for item in candidates]


def find_native_reference(target):
    """Return an object with the same ZZMI identity and a valid UV2 sample."""
    for candidate in _reference_candidates(target):
        return candidate
    return None


def _uv2_is_missing(mesh):
    uv2 = _read_uv(mesh, UV2_LAYER)
    if uv2 is None:
        return True
    if numpy.all(numpy.abs(uv2) < 1e-7, axis=1).all():
        return True
    uv0 = _read_uv(mesh, UV0_LAYER)
    return _same_uv(uv2, uv0)


def build_uv2_projection_map(blueprint_model):
    """Fit UV2 projections for draw objects that have no usable UV2 layer."""
    projections = {}
    for draw in getattr(blueprint_model, "ordered_draw_obj_data_model_list", ()) or ():
        if getattr(draw, "zzmi_stub", False):
            continue
        obj = bpy.data.objects.get(getattr(draw, "get_blender_obj_name", lambda: "")())
        if obj is None:
            obj = bpy.data.objects.get(getattr(draw, "source_obj_name", "") or "")
        if obj is None or obj.type != "MESH" or not _uv2_is_missing(obj.data):
            continue
        reference = find_native_reference(obj)
        if reference is None:
            continue
        projection = _fit_reference_projection(reference)
        if projection is None:
            continue
        for key in (
            getattr(draw, "match_unique_str", ""),
            getattr(draw, "get_unique_str", lambda: "")(),
            getattr(draw, "obj_name", ""),
            getattr(draw, "source_obj_name", ""),
        ):
            if key:
                projections[key] = projection
    return projections


def _normalize(values):
    lengths = numpy.linalg.norm(values, axis=1, keepdims=True)
    return values / numpy.where(lengths > 1e-8, lengths, 1.0)


def _vertex_normals_from_corner_normals(mesh):
    result = numpy.zeros((len(mesh.vertices), 3), dtype=numpy.float64)
    corners = getattr(mesh, "corner_normals", None)
    if corners is not None and len(corners) == len(mesh.loops):
        normals = numpy.empty((len(corners), 3), dtype=numpy.float32)
        corners.foreach_get("vector", normals.ravel())
        loops = _read_loop_vertices(mesh)
        accumulated = numpy.zeros((len(mesh.vertices), 3), dtype=numpy.float64)
        numpy.add.at(accumulated, loops, normals)
        result[:] = _normalize(accumulated)

    vertex_normals = getattr(mesh, "vertex_normals", None)
    if vertex_normals is not None and len(vertex_normals) == len(mesh.vertices):
        fallback = numpy.empty((len(mesh.vertices), 3), dtype=numpy.float32)
        vertex_normals.foreach_get("vector", fallback.ravel())
        fallback = _normalize(fallback)
        invalid = ~numpy.isfinite(result).all(axis=1) | (numpy.linalg.norm(result, axis=1) <= 1e-8)
        result[invalid] = fallback[invalid]

    face_accumulated = numpy.zeros((len(mesh.vertices), 3), dtype=numpy.float64)
    positions = _read_positions(mesh)
    for polygon in mesh.polygons:
        indices = list(polygon.vertices)
        for index in range(1, len(indices) - 1):
            a, b, c = positions[indices[0]], positions[indices[index]], positions[indices[index + 1]]
            normal = numpy.cross(b - a, c - a)
            for vertex_index in (indices[0], indices[index], indices[index + 1]):
                face_accumulated[vertex_index] += normal
    face_fallback = _normalize(face_accumulated)
    invalid = ~numpy.isfinite(result).all(axis=1) | (numpy.linalg.norm(result, axis=1) <= 1e-8)
    result[invalid] = face_fallback[invalid]
    return result


def _write_vector_attribute(mesh, name, values):
    attribute = mesh.attributes.get(name)
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "FLOAT_VECTOR"
        or len(attribute.data) != len(values)
    ):
        if attribute is not None:
            mesh.attributes.remove(attribute)
        attribute = mesh.attributes.new(name=name, type="FLOAT_VECTOR", domain="POINT")
    attribute.data.foreach_set("vector", numpy.ascontiguousarray(values, dtype=numpy.float32).ravel())


def _write_scalar_attribute(mesh, name, values):
    attribute = mesh.attributes.get(name)
    if (
        attribute is None
        or attribute.domain != "POINT"
        or attribute.data_type != "FLOAT"
        or len(attribute.data) != len(values)
    ):
        if attribute is not None:
            mesh.attributes.remove(attribute)
        attribute = mesh.attributes.new(name=name, type="FLOAT", domain="POINT")
    attribute.data.foreach_set("value", numpy.ascontiguousarray(values, dtype=numpy.float32))


def _write_custom_normals_if_needed(mesh, normals):
    if "custom_normal" in mesh.attributes:
        return
    try:
        mesh.normals_split_custom_set_from_vertices(normals.tolist())
    except Exception:
        pass


def remove_zzmi_current_pose_overrides(target):
    """Keep reconstructed ZZMI surface directions from being overwritten."""
    mesh = getattr(target, "data", target)
    if mesh is None:
        return False
    removed = False
    for name in (POSE_NORMAL_ATTRIBUTE, POSE_TANGENT_ATTRIBUTE, POSE_SIGN_ATTRIBUTE):
        attribute = mesh.attributes.get(name)
        if attribute is not None:
            mesh.attributes.remove(attribute)
            removed = True
    return removed


def _triangulated_copy(mesh):
    copy = mesh.copy()
    if any(polygon.loop_total != 3 for polygon in copy.polygons):
        bm = bmesh.new()
        try:
            bm.from_mesh(copy)
            bmesh.ops.triangulate(bm, faces=list(bm.faces), quad_method="BEAUTY", ngon_method="BEAUTY")
            bm.to_mesh(copy)
        finally:
            bm.free()
        copy.update()
    return copy


def _tangent_per_vertex(mesh):
    uv = UV0_LAYER if UV0_LAYER in mesh.uv_layers else (mesh.uv_layers.active.name if mesh.uv_layers.active else "")
    if not uv:
        return None, None
    calc_mesh = _triangulated_copy(mesh)
    try:
        calc_mesh.calc_tangents(uvmap=uv)
        loops = _read_loop_vertices(calc_mesh)
        tangents = numpy.empty((len(calc_mesh.loops), 3), dtype=numpy.float32)
        calc_mesh.loops.foreach_get("tangent", tangents.ravel())
        signs = numpy.empty(len(calc_mesh.loops), dtype=numpy.float32)
        calc_mesh.loops.foreach_get("bitangent_sign", signs)
    except Exception:
        return None, None
    finally:
        bpy.data.meshes.remove(calc_mesh)

    tangent_sum = numpy.zeros((len(mesh.vertices), 3), dtype=numpy.float64)
    sign_sum = numpy.zeros(len(mesh.vertices), dtype=numpy.float64)
    count = numpy.zeros(len(mesh.vertices), dtype=numpy.int32)
    numpy.add.at(tangent_sum, loops, tangents)
    numpy.add.at(sign_sum, loops, signs)
    numpy.add.at(count, loops, 1)
    tangents = _normalize(tangent_sum)
    signs = numpy.sign(sign_sum)
    signs[signs == 0] = 1.0
    return tangents, signs


def _project_to_surface(reference_obj, target_obj):
    """Return a target-loop sampler for authored corner UV data."""
    source_mesh = reference_obj.data
    target_mesh = target_obj.data
    source_uv = _read_uv(source_mesh, UV1_LAYER)
    if source_uv is None:
        return None
    source_mesh.calc_loop_triangles()
    source_vertices = _read_positions(source_mesh)
    source_world = numpy.array(reference_obj.matrix_world, dtype=numpy.float64)
    target_world = numpy.array(target_obj.matrix_world, dtype=numpy.float64)
    world_vertices = (
        source_vertices @ source_world[:3, :3].T + source_world[:3, 3]
    )
    triangles = []
    triangle_loops = []
    for triangle in source_mesh.loop_triangles:
        loops = tuple(int(index) for index in triangle.loops)
        vertices = tuple(int(index) for index in triangle.vertices)
        if len(loops) != 3 or len(vertices) != 3:
            continue
        triangles.append(vertices)
        triangle_loops.append(loops)
    if not triangles:
        return None
    tree = BVHTree.FromPolygons(world_vertices.tolist(), triangles, all_triangles=True)
    target_vertices = _read_positions(target_mesh)
    target_loops = _read_loop_vertices(target_mesh)
    target_positions = (
        target_vertices[target_loops] @ target_world[:3, :3].T + target_world[:3, 3]
    )
    result = numpy.zeros((len(target_loops), 2), dtype=numpy.float32)
    for index, position in enumerate(target_positions):
        nearest, _normal, triangle_index, _distance = tree.find_nearest(Vector(position))
        if nearest is None or triangle_index is None:
            return None
        triangle = triangles[triangle_index]
        a, b, c = world_vertices[list(triangle)]
        edge0 = b - a
        edge1 = c - a
        point = numpy.asarray(nearest, dtype=numpy.float64) - a
        d00 = float(numpy.dot(edge0, edge0))
        d01 = float(numpy.dot(edge0, edge1))
        d11 = float(numpy.dot(edge1, edge1))
        d20 = float(numpy.dot(point, edge0))
        d21 = float(numpy.dot(point, edge1))
        denominator = d00 * d11 - d01 * d01
        if abs(denominator) < 1e-15:
            weights = numpy.array((1.0, 0.0, 0.0), dtype=numpy.float64)
        else:
            v = (d11 * d20 - d01 * d21) / denominator
            w = (d00 * d21 - d01 * d20) / denominator
            weights = numpy.array((1.0 - v - w, v, w), dtype=numpy.float64)
        result[index] = weights @ source_uv[list(triangle_loops[triangle_index])]
    return result


def _same_mesh_layout(source_obj, target_obj):
    source_mesh = source_obj.data
    target_mesh = target_obj.data
    if (
        len(source_mesh.vertices) != len(target_mesh.vertices)
        or len(source_mesh.loops) != len(target_mesh.loops)
        or len(source_mesh.polygons) != len(target_mesh.polygons)
    ):
        return False
    source_loops = _read_loop_vertices(source_mesh)
    target_loops = _read_loop_vertices(target_mesh)
    if not numpy.array_equal(source_loops, target_loops):
        return False
    source_sizes = numpy.asarray([polygon.loop_total for polygon in source_mesh.polygons], dtype=numpy.int32)
    target_sizes = numpy.asarray([polygon.loop_total for polygon in target_mesh.polygons], dtype=numpy.int32)
    if not numpy.array_equal(source_sizes, target_sizes):
        return False
    source_positions = _read_positions(source_mesh)
    target_positions = _read_positions(target_mesh)
    return bool(numpy.allclose(source_positions, target_positions, rtol=0.0, atol=1e-6))


def _repair_uv1(target_obj, reference_obj):
    target_mesh = target_obj.data
    reference_uv = _read_uv(reference_obj.data, UV1_LAYER) if reference_obj is not None else None
    if reference_uv is None:
        return False
    target_uv = _read_uv(target_mesh, UV1_LAYER)
    target_uv0 = _read_uv(target_mesh, UV0_LAYER)
    missing = (
        target_uv is None
        or numpy.all(numpy.abs(target_uv) < 1e-7, axis=1).all()
        or _same_uv(target_uv, target_uv0)
    )
    if not missing:
        return False
    if reference_obj is not None and _same_mesh_layout(reference_obj, target_obj):
        values = reference_uv.copy()
    elif float(numpy.ptp(reference_uv, axis=0).max()) < 1e-6:
        values = numpy.repeat(reference_uv[:1], len(target_mesh.loops), axis=0)
    else:
        values = _project_to_surface(reference_obj, target_obj)
        if values is None:
            return False
    layer = target_mesh.uv_layers.get(UV1_LAYER)
    if layer is None:
        layer = target_mesh.uv_layers.new(name=UV1_LAYER)
    layer.data.foreach_set("uv", numpy.ascontiguousarray(values, dtype=numpy.float32).ravel())
    return True


def ensure_zzmi_missing_vertex_data(target_obj, source_obj=None):
    """Fill missing native channels on a temporary ZZMI export object.

    The caller must pass a temporary/copy object.  Existing valid channels are
    preserved; the function never writes COLOR.
    """
    mesh = getattr(target_obj, "data", None)
    if mesh is None or target_obj.type != "MESH" or not len(mesh.vertices):
        return {}
    reference = find_native_reference(target_obj)
    if reference is None and source_obj is not None and source_obj != target_obj:
        reference = find_native_reference(source_obj)

    report = {}
    if _repair_uv1(target_obj, reference):
        report["TEXCOORD1.xy"] = "reference-surface"

    normals = _vertex_normals_from_corner_normals(mesh)
    existing_normal = _vector_attribute(mesh, IMPORTED_NORMAL_XYZ)
    if not _valid_directions(existing_normal, len(mesh.vertices)):
        _write_vector_attribute(mesh, IMPORTED_NORMAL_XYZ, normals)
        report[IMPORTED_NORMAL_XYZ] = "target-corner-average"
    _write_custom_normals_if_needed(mesh, normals)

    existing_tangent = _vector_attribute(mesh, IMPORTED_TANGENT_XYZ)
    existing_sign = _scalar_attribute(mesh, IMPORTED_TANGENT_W)
    if not _valid_directions(existing_tangent, len(mesh.vertices)) or existing_sign is None:
        tangents, signs = _tangent_per_vertex(mesh)
        if tangents is not None and signs is not None:
            _write_vector_attribute(mesh, IMPORTED_TANGENT_XYZ, tangents)
            _write_scalar_attribute(mesh, IMPORTED_TANGENT_W, signs)
            report[IMPORTED_TANGENT_XYZ] = "target-uv0-tbn"
            report[IMPORTED_TANGENT_W] = "target-tbn-handness"

    if any(
        name in report
        for name in (IMPORTED_NORMAL_XYZ, IMPORTED_TANGENT_XYZ, IMPORTED_TANGENT_W)
    ):
        remove_zzmi_current_pose_overrides(mesh)

    mesh.update()
    return report


__all__ = [
    "UV2_LAYER",
    "UV2Projection",
    "build_uv2_projection_map",
    "ensure_zzmi_missing_vertex_data",
    "find_native_reference",
    "remove_zzmi_current_pose_overrides",
]
