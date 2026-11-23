"""Optional native pairwise distances for ZZMI bone diffusion samples."""

import ctypes
from functools import lru_cache
from pathlib import Path

import numpy


@lru_cache(maxsize=1)
def _distance_function():
    library_path = Path(__file__).with_name("native") / "herta_skeleton_fusion_windows_x64.dll"
    try:
        library = ctypes.CDLL(str(library_path))
        function = library.herta_skeleton_distances_v1
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    return function


def squared_point_distances(points):
    """Calculate the symmetric squared-distance matrix for a fusion sample."""
    count = len(points)
    if not 4 <= count <= 256:
        return None
    compute_distances = _distance_function()
    if compute_distances is None:
        return None
    points = numpy.ascontiguousarray(points, dtype=numpy.float32)
    if points.shape != (count, 3) or not numpy.isfinite(points).all():
        return None
    distances = numpy.empty((count, count), dtype=numpy.float32)
    if not compute_distances(points.ctypes.data, count, distances.ctypes.data):
        return None
    return distances
