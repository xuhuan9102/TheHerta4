"""Sample viewport shading for export without changing the exported geometry."""

from collections import defaultdict
from dataclasses import dataclass

import bpy
import numpy

from ..common.mesh_attribute_names import IMPORTED_TANGENT_W
from .export_space import position_export_matrix


NORMAL_ATTRIBUTE = "_ssmt_pose_normal"
TANGENT_ATTRIBUTE = "_ssmt_pose_tangent"
SIGN_ATTRIBUTE = "_ssmt_pose_sign"


def _read(collection, field, dtype, width=1):
    values = numpy.empty(len(collection) * width, dtype=dtype)
    collection.foreach_get(field, values)
    return values.reshape(-1, width) if width > 1 else values


def _normalize(vectors):
    lengths = numpy.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / numpy.where(lengths > 0, lengths, 1)


@dataclass
class _PoseMesh:
    positions: numpy.ndarray
    normals: numpy.ndarray
    corner_vertices: numpy.ndarray
    face_sizes: numpy.ndarray
    corner_uvs: numpy.ndarray | None


def capture(tree, object_names):
    if not getattr(tree, "use_current_pose_shading", False):
        return {}

    from ..blueprint.preprocess_cache import PreProcessCache

    depsgraph = bpy.context.evaluated_depsgraph_get()
    result = {}
    for name in object_names:
        source, source_name = PreProcessCache.resolve_source_object(name)
        if source is None or source.type != 'MESH' or source_name in result:
            continue
        evaluated = source.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            # World space lets the corner attributes survive joins between
            # differently transformed objects without changing their values.
            linear = numpy.asarray(evaluated.matrix_world.to_3x3(), dtype=numpy.float32)
            positions = _read(mesh.vertices, "co", numpy.float32, 3) @ linear.T
            normals = _read(mesh.corner_normals, "vector", numpy.float32, 3)
            normals = _normalize(normals @ numpy.linalg.pinv(linear))
            uv = mesh.uv_layers.active
            result[source_name] = _PoseMesh(
                positions, normals,
                _read(mesh.loops, "vertex_index", numpy.int32),
                _read(mesh.polygons, "loop_total", numpy.int32),
                _read(uv.uv, "vector", numpy.float32, 2) if uv is not None else None,
            )
        finally:
            evaluated.to_mesh_clear()
    return result


def _same_winding(indices):
    return sum(a > b for a, b in zip(indices, indices[1:] + indices[:1])) == 1


def _match_corners(mesh, pose):
    if len(mesh.vertices) != len(pose.positions):
        raise ValueError("当前姿态和基础网格的顶点数不同")
    sizes = _read(mesh.polygons, "loop_total", numpy.int32)
    vertices = _read(mesh.loops, "vertex_index", numpy.int32)
    if numpy.array_equal(sizes, pose.face_sizes) and numpy.array_equal(vertices, pose.corner_vertices):
        return numpy.arange(len(vertices))

    faces = defaultdict(list)
    larger_faces = defaultdict(list)
    start = 0
    for size in pose.face_sizes:
        corners = pose.corner_vertices[start:start + size].tolist()
        corner_indices = dict(zip(corners, range(start, start + size)))
        faces[frozenset(corners)].append(corner_indices)
        if size > 3:
            for vertex in corners:
                larger_faces[vertex].append(corner_indices)
        start += size

    uv = mesh.uv_layers.get("TEXCOORD.xy") or mesh.uv_layers.active
    corner_uvs = _read(uv.uv, "vector", numpy.float32, 2) if uv is not None else None
    indices = numpy.empty(len(vertices), dtype=numpy.int32)
    start = 0
    for size in sizes:
        corners = vertices[start:start + size].tolist()
        key = frozenset(corners)
        candidates = list(faces.get(key, ()))
        # A cached triangle can come from a larger face even when an original
        # triangle shares its vertices, for example on layered hair cards.
        candidates.extend(face for face in larger_faces.get(corners[0], ())
                          if len(face) > size and key.issubset(face))
        candidates = [[face[vertex] for vertex in corners] for face in candidates]
        if len(candidates) > 1 and pose.corner_uvs is not None and corner_uvs is not None:
            uv_matches = [candidate for candidate in candidates
                          if numpy.allclose(pose.corner_uvs[candidate], corner_uvs[start:start + size],
                                            rtol=0, atol=1e-6)]
            if uv_matches:
                candidates = uv_matches
        if len(candidates) > 1:
            candidates = [candidate for candidate in candidates if _same_winding(candidate)]
        if len(candidates) != 1:
            raise ValueError("当前姿态和基础网格的面角无法一一对应")
        indices[start:start + size] = candidates[0]
        start += size
    return indices


def _write_attribute(mesh, name, kind, values):
    attribute = mesh.attributes.get(name)
    if attribute is None:
        attribute = mesh.attributes.new(name=name, type=kind, domain='CORNER')
    field = "vector" if kind == 'FLOAT_VECTOR' else "value"
    attribute.data.foreach_set(field, numpy.ascontiguousarray(values).ravel())


def _apply_pose(mesh, pose):
    if not mesh.loops:
        return
    normals = pose.normals[_match_corners(mesh, pose)]
    # Use the copy's triangulation and UVs so the tangent basis matches
    # the triangles and normal-map coordinates that actually get exported.
    reference = bpy.data.meshes.new("SSMT Current Pose Shading")
    try:
        reference.from_pydata(pose.positions.tolist(), [], [tuple(poly.vertices) for poly in mesh.polygons])
        reference.polygons.foreach_set("use_smooth", numpy.ones(len(reference.polygons), dtype=bool))
        reference.normals_split_custom_set(normals.tolist())
        uv = mesh.uv_layers.get("TEXCOORD.xy") or mesh.uv_layers.active
        if uv is not None:
            reference_uv = reference.uv_layers.new(name=uv.name)
            reference_uv.uv.foreach_set("vector", _read(uv.uv, "vector", numpy.float32, 2).ravel())
            reference.calc_tangents(uvmap=uv.name)
        tangents = _read(reference.loops, "tangent", numpy.float32, 3)
        signs = _read(reference.loops, "bitangent_sign", numpy.float32)
    finally:
        bpy.data.meshes.remove(reference)

    _write_attribute(mesh, NORMAL_ATTRIBUTE, 'FLOAT_VECTOR', normals)
    _write_attribute(mesh, TANGENT_ATTRIBUTE, 'FLOAT_VECTOR', tangents)
    _write_attribute(mesh, SIGN_ATTRIBUTE, 'FLOAT', signs)


def apply_to_copies(copy_map, poses):
    if not poses:
        return

    from ..blueprint.preprocess_cache import PreProcessCache

    for copy_name in copy_map.values():
        copy = bpy.data.objects.get(copy_name)
        if copy is None or copy.type != 'MESH':
            continue
        source_name = copy.get(PreProcessCache.COPY_SOURCE_PROP)
        if source_name in poses:
            try:
                _apply_pose(copy.data, poses[source_name])
            except ValueError as error:
                raise RuntimeError(f"以当前状态计算法线、切线：{source_name}，{error}") from error


class PoseLoops:
    """Expose the sampled directions through the existing format encoders."""

    def __init__(self, obj, logic_name):
        attrs = obj.data.attributes
        world_to_local = numpy.linalg.pinv(numpy.asarray(obj.matrix_world.to_3x3(), dtype=numpy.float32))
        linear = position_export_matrix(logic_name, dtype=numpy.float32)[:3, :3] @ world_to_local
        self.normals = _normalize(
            _read(attrs[NORMAL_ATTRIBUTE].data, "vector", numpy.float32, 3) @ numpy.linalg.pinv(linear)
        )
        tangents = _read(attrs[TANGENT_ATTRIBUTE].data, "vector", numpy.float32, 3) @ linear.T
        self.tangents = _normalize(tangents - (tangents * self.normals).sum(axis=1, keepdims=True) * self.normals)
        self.signs = _read(attrs[SIGN_ATTRIBUTE].data, "value", numpy.float32)
        native_w = attrs.get(IMPORTED_TANGENT_W)
        if native_w is not None and len(native_w.data) == len(obj.data.vertices):
            vertex_signs = _read(native_w.data, "value", numpy.float32)
            loop_vertices = _read(obj.data.loops, "vertex_index", numpy.int32)
            self.signs = vertex_signs[loop_vertices]
        if numpy.linalg.det(linear) < 0:
            self.signs *= -1

    def foreach_get(self, field, output):
        if field == "normal":
            values = self.normals
        elif field == "tangent":
            values = self.tangents
        elif field == "bitangent_sign":
            values = self.signs
        else:
            values = numpy.cross(self.normals, self.tangents) * self.signs[:, None]
        output[:] = values.ravel()
