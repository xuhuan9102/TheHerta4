"""Shape-key buffer layout helpers shared by import paths."""


def infer_shapekey_vertex_offset_stride(
    vertex_id_count: int,
    vertex_offset_scalar_count: int,
) -> int:
    """Return the float16 stride used by one ShapeKeyVertexOffset record.

    Existing WWMI data stores position, normal, and tangent deltas (6 floats).
    ZZMI native morph data stores position deltas only (3 floats).
    """
    vertex_id_count = int(vertex_id_count)
    vertex_offset_scalar_count = int(vertex_offset_scalar_count)
    if vertex_id_count <= 0 or vertex_offset_scalar_count <= 0:
        return 0
    if vertex_offset_scalar_count == vertex_id_count * 3:
        return 3
    if vertex_offset_scalar_count == vertex_id_count * 6:
        return 6
    return 0
