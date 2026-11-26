import numpy


_X_NEGATIVE_90_LOGICS = {
    "APMI",
    "SRMI",
    "GIMI",
    "HIMI",
    "YYSLS",
    "IdentityV",
}


def effective_export_logic_name(logic_name=None) -> str:
    resolved = str(logic_name or "")
    return "EFMI" if resolved == "HTMI" else resolved


def position_export_matrix(logic_name=None, dtype=numpy.float64) -> numpy.ndarray:
    """Return the Blender-space to exported-POSITION affine transform."""
    resolved = effective_export_logic_name(logic_name)
    matrix = numpy.eye(4, dtype=dtype)
    if resolved in _X_NEGATIVE_90_LOGICS:
        matrix[:3, :3] = (
            (1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0),
            (0.0, -1.0, 0.0),
        )
    elif resolved == "SnowBreak":
        matrix[:3, :3] = (
            (-100.0, 0.0, 0.0),
            (0.0, -100.0, 0.0),
            (0.0, 0.0, 100.0),
        )
    return matrix


def convert_position_coords(sampled_coords, logic_name=None) -> numpy.ndarray:
    coords = numpy.asarray(sampled_coords, dtype=numpy.float32)
    if coords.size == 0:
        return coords
    linear = position_export_matrix(logic_name, dtype=numpy.float32)[:3, :3]
    return coords @ linear.T
