import numpy
import bpy
from mathutils import kdtree

from .logic_name import LogicName
from .object_prefix_helper import ObjectPrefixHelper
from ..utils.color_attribute_utils import (
    encode_color_for_unorm_target,
    read_color_attribute_data,
    sample_color_attribute_to_loops,
)
from ..utils.log_utils import LOG


_COLOR_ATTRIBUTE = "COLOR"
_UV_ATTRIBUTES = ("TEXCOORD1.xy", "TEXCOORD2.xy")
_TANGENT_W_ATTRIBUTE = "3DMigoto_TANGENT_W"
_COPY_REQUESTED_PROP = "_ssmt_preprocess_requested"
_COPY_SUFFIX = "_copy"
_DISTANCE_SAMPLES = 512


def _object_prefix(name):
    try:
        info = ObjectPrefixHelper.extract_prefix_info(name)
    except Exception:
        return ""
    return str(info[0] or "") if info else ""


def _requested_name(obj):
    requested = str(obj.get(_COPY_REQUESTED_PROP, "") or "")
    if requested:
        return requested
    if obj.name.endswith(_COPY_SUFFIX):
        return obj.name[:-len(_COPY_SUFFIX)]
    return obj.name


def _has_reference_attributes(obj):
    if obj is None or obj.type != "MESH" or obj.data is None:
        return False
    mesh = obj.data
    if _COLOR_ATTRIBUTE in mesh.color_attributes:
        return True
    return any(name in mesh.uv_layers for name in _UV_ATTRIBUTES)


def _is_simple_color(color_attribute):
    if color_attribute is None or not len(color_attribute.data):
        return True
    values = read_color_attribute_data(color_attribute)
    if not len(values):
        return True
    # A custom mesh commonly has two default values (for example opaque and
    # zero-alpha loops).  Native ZZMI COLOR uses a small discrete mask as well,
    # so low-cardinality custom COLOR is considered replaceable; a painted
    # gradient is left untouched.
    return len(numpy.unique(numpy.round(values, 5), axis=0)) <= 2


def _is_flat_uv(uv_layer):
    if uv_layer is None or not len(uv_layer.data):
        return True
    values = numpy.empty(len(uv_layer.data) * 2, dtype=numpy.float32)
    uv_layer.data.foreach_get("uv", values)
    values = values.reshape(-1, 2)
    return bool(numpy.all(numpy.ptp(values, axis=0) < 1e-5))


def _reference_candidates(target):
    target_name = _requested_name(target)
    prefix = _object_prefix(target_name)
    if not prefix:
        return []

    candidates = []
    for obj in bpy.data.objects:
        if obj is target or obj.type != "MESH" or obj.data is None:
            continue
        if _requested_name(obj) == target_name:
            continue
        if obj.name.endswith(_COPY_SUFFIX):
            continue
        if _object_prefix(obj.name) != prefix:
            continue
        if not _has_reference_attributes(obj):
            continue
        # Custom source meshes may only have TEXCOORD.xy.  A native sibling
        # carries at least one of the additional lighting inputs.
        if not any(name in obj.data.uv_layers for name in _UV_ATTRIBUTES):
            continue
        candidates.append(obj)
    return candidates


def has_native_reference_uv2(obj):
    if obj is None or obj.type != "MESH" or obj.data is None:
        return False
    return any(
        "TEXCOORD2.xy" in candidate.data.uv_layers
        for candidate in _reference_candidates(obj)
    )


def _nearest_vertex_ids(source, target, sample_limit=None):
    source_positions = numpy.asarray(
        [tuple(source.matrix_world @ vertex.co) for vertex in source.data.vertices],
        dtype=numpy.float32,
    )
    if not len(source_positions) or not len(target.data.vertices):
        return numpy.zeros(len(target.data.vertices), dtype=numpy.int32), float("inf")

    tree = kdtree.KDTree(len(source_positions))
    for index, position in enumerate(source_positions):
        tree.insert(tuple(position), index)
    tree.balance()

    target_positions = numpy.asarray(
        [tuple(target.matrix_world @ vertex.co) for vertex in target.data.vertices],
        dtype=numpy.float32,
    )
    if sample_limit is not None and len(target_positions) > sample_limit:
        sample_indices = numpy.linspace(
            0, len(target_positions) - 1, sample_limit, dtype=numpy.int32
        )
        sample_positions = target_positions[sample_indices]
    else:
        sample_positions = target_positions

    nearest_ids = numpy.empty(len(target_positions), dtype=numpy.int32)
    distances = numpy.empty(len(sample_positions), dtype=numpy.float32)
    sampled_ids = numpy.empty(len(sample_positions), dtype=numpy.int32)
    for index, position in enumerate(sample_positions):
        _co, source_index, distance = tree.find(tuple(position))
        sampled_ids[index] = source_index
        distances[index] = distance

    if sample_limit is not None and len(target_positions) > sample_limit:
        for index, position in enumerate(target_positions):
            _co, source_index, _distance = tree.find(tuple(position))
            nearest_ids[index] = source_index
    else:
        nearest_ids[:] = sampled_ids
    return nearest_ids, float(numpy.median(distances))


def _loop_vertex_indices(mesh):
    indices = numpy.empty(len(mesh.loops), dtype=numpy.int32)
    mesh.loops.foreach_get("vertex_index", indices)
    return indices


def _first_value_per_vertex(mesh, values, width):
    values = numpy.asarray(values, dtype=numpy.float32).reshape(-1, width)
    result = numpy.zeros((len(mesh.vertices), width), dtype=numpy.float32)
    assigned = numpy.zeros(len(mesh.vertices), dtype=bool)
    loop_vertices = _loop_vertex_indices(mesh)
    for loop_index, vertex_index in enumerate(loop_vertices):
        if not assigned[vertex_index]:
            result[vertex_index] = values[loop_index]
            assigned[vertex_index] = True
    return result


def _mapped_loop_values(source, target, source_loop_values, nearest_ids, width):
    source_values = _first_value_per_vertex(source.data, source_loop_values, width)
    target_loop_vertices = _loop_vertex_indices(target.data)
    return source_values[nearest_ids[target_loop_vertices]]


def _write_uv(target, name, values):
    active = target.data.uv_layers.active
    layer = target.data.uv_layers.get(name)
    if layer is None:
        layer = target.data.uv_layers.new(name=name)
    layer.data.foreach_set("uv", numpy.ascontiguousarray(values).ravel())
    if active is not None:
        target.data.uv_layers.active = active


def _srgb_to_linear_rgb(values):
    values = numpy.asarray(values, dtype=numpy.float32)
    result = values.copy()
    rgb = numpy.clip(result[..., :3], 0.0, 1.0)
    result[..., :3] = numpy.where(
        rgb <= 0.04045,
        rgb / 12.92,
        numpy.power((rgb + 0.055) / 1.055, 2.4),
    )
    return result


def _write_color(target, values):
    color_attribute = target.data.color_attributes.get(_COLOR_ATTRIBUTE)
    if color_attribute is None:
        color_attribute = target.data.color_attributes.new(
            name=_COLOR_ATTRIBUTE,
            type="BYTE_COLOR",
            domain="CORNER",
        )
    if color_attribute.data_type == "FLOAT_COLOR":
        payload = _srgb_to_linear_rgb(values)
        color_attribute.data.foreach_set("color", payload.ravel())
    else:
        field = "color_srgb" if hasattr(color_attribute.data[0], "color_srgb") else "color"
        color_attribute.data.foreach_set(field, values.ravel())


def _source_color_srgb(source):
    color_attribute = source.data.color_attributes.get(_COLOR_ATTRIBUTE)
    if color_attribute is None:
        return None
    values = sample_color_attribute_to_loops(source.data, color_attribute)
    return encode_color_for_unorm_target(color_attribute, values)


def _source_tangent_w(source):
    attribute = source.data.attributes.get(_TANGENT_W_ATTRIBUTE)
    if attribute is None:
        return None
    values = numpy.empty(len(attribute.data), dtype=numpy.float32)
    attribute.data.foreach_get("value", values)
    if attribute.domain == "POINT" and len(values) == len(source.data.vertices):
        return values
    if attribute.domain == "CORNER":
        return _first_value_per_vertex(source.data, values, 1).reshape(-1)
    return None


def _write_tangent_w(target, values):
    attribute = target.data.attributes.get(_TANGENT_W_ATTRIBUTE)
    if attribute is None:
        attribute = target.data.attributes.new(
            name=_TANGENT_W_ATTRIBUTE,
            type="FLOAT",
            domain="POINT",
        )
    attribute.data.foreach_set("value", numpy.ascontiguousarray(values).ravel())


def inherit_native_render_attributes(copy_objects):
    """Fill missing ZZZ lighting inputs on export copies from a native sibling mesh.

    The custom body only carries TEXCOORD.xy.  The game shader also reads
    TEXCOORD1/2 and COLOR; the old exporter created constant defaults for the
    missing layers.  Pick the native sibling with the closest geometry under
    the same object prefix and copy only the attributes that are absent or
    constant on the export copy.
    """
    from .global_config import GlobalConfig

    if str(getattr(GlobalConfig, "logic_name", "")) != LogicName.ZZMI:
        return

    for item in copy_objects:
        target = bpy.data.objects.get(item) if isinstance(item, str) else item
        if target is None or target.type != "MESH" or target.data is None:
            continue
        candidates = _reference_candidates(target)
        if not candidates:
            continue

        target_color = target.data.color_attributes.get(_COLOR_ATTRIBUTE)
        needs_color = (
            target_color is None
            and any(_COLOR_ATTRIBUTE in c.data.color_attributes for c in candidates)
        ) or (
            target_color is not None
            and _is_simple_color(target_color)
            and any(_COLOR_ATTRIBUTE in c.data.color_attributes for c in candidates)
        )
        needed_uvs = [
            name
            for name in _UV_ATTRIBUTES
            if any(name in c.data.uv_layers for c in candidates)
            and (name not in target.data.uv_layers or _is_flat_uv(target.data.uv_layers.get(name)))
        ]
        target_tangent_w = target.data.attributes.get(_TANGENT_W_ATTRIBUTE)
        needs_tangent_w = (
            target_tangent_w is None
            and any(_TANGENT_W_ATTRIBUTE in c.data.attributes for c in candidates)
        )
        if not needs_color and not needed_uvs and not needs_tangent_w:
            continue

        best_source = None
        best_ids = None
        best_distance = float("inf")
        target_size = max(1.0, max(target.dimensions))
        max_distance = max(0.05, target_size * 0.12)
        for candidate in candidates:
            nearest_ids, median_distance = _nearest_vertex_ids(
                candidate, target, sample_limit=_DISTANCE_SAMPLES
            )
            if median_distance < best_distance:
                best_source = candidate
                best_ids = nearest_ids
                best_distance = median_distance

        if best_source is None or best_ids is None or best_distance > max_distance:
            continue

        changed = []
        if needs_color:
            color_values = _source_color_srgb(best_source)
            if color_values is not None:
                mapped = _mapped_loop_values(
                    best_source,
                    target,
                    color_values,
                    best_ids,
                    4,
                )
                _write_color(target, mapped)
                changed.append(_COLOR_ATTRIBUTE)

        for name in needed_uvs:
            layer = best_source.data.uv_layers.get(name)
            if layer is None:
                continue
            values = numpy.empty(len(layer.data) * 2, dtype=numpy.float32)
            layer.data.foreach_get("uv", values)
            mapped = _mapped_loop_values(best_source, target, values, best_ids, 2)
            _write_uv(target, name, mapped)
            changed.append(name)

        if needs_tangent_w:
            tangent_w = _source_tangent_w(best_source)
            if tangent_w is not None:
                _write_tangent_w(target, tangent_w[best_ids])
                changed.append(_TANGENT_W_ATTRIBUTE)

        if changed:
            LOG.info(
                f"   前处理原生属性继承: {target.name} <- {best_source.name} "
                f"({', '.join(changed)}, 中位距离 {best_distance:.6f})"
            )
