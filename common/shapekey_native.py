"""Optional native array arithmetic for shape sampling; RNA stays on the caller thread."""

import ctypes as ct
import os
from pathlib import Path
import threading

import numpy as np


_library = None
_checked = False
_lock = threading.Lock()


def native_library():
    global _library, _checked
    if os.environ.get("HERTA_SHAPEKEY_NATIVE", "1") == "0":
        return None
    with _lock:
        if not _checked:
            try:
                lib = ct.CDLL(str(Path(__file__).with_name("native") / "herta_shapekey_windows_x64.dll"))
                lib.herta_shapekey_abi.restype = ct.c_uint
                if lib.herta_shapekey_abi() != 1:
                    raise ValueError("ShapeKey native ABI")
                lib.herta_relative_shapes.argtypes = [ct.c_void_p] * 3 + [ct.c_uint64] * 2 + [ct.c_uint, ct.c_int, ct.c_void_p]
                lib.herta_relative_shapes.restype = ct.c_int
                _library = lib
            except (OSError, AttributeError, ValueError):
                pass
            _checked = True
    return _library


def relative_shapes(basis, targets, relatives, *, mirror=False, threads=4):
    """Match float32 basis + (target - relative), with a separate subtraction."""
    basis = np.ascontiguousarray(basis, dtype=np.float32)
    targets = [np.ascontiguousarray(a, dtype=np.float32) for a in targets]
    relatives = [np.ascontiguousarray(a, dtype=np.float32) for a in relatives]
    if basis.ndim != 2 or basis.shape[1] != 3 or len(targets) != len(relatives):
        raise ValueError("Invalid relative shape layout")
    if any(a.shape != basis.shape for a in targets + relatives):
        raise ValueError("Relative shape vertex counts differ")
    output = np.empty((len(targets), *basis.shape), dtype=np.float32)
    lib = native_library()
    if lib is not None and basis.size and targets:
        pointers = ct.c_void_p * len(targets)
        ok = lib.herta_relative_shapes(basis.ctypes.data,
                                      pointers(*(a.ctypes.data for a in targets)),
                                      pointers(*(a.ctypes.data for a in relatives)),
                                      basis.size, len(targets), max(1, min(int(threads), 32)),
                                      mirror, output.ctypes.data)
        if ok:
            return output
    for index, (target, relative) in enumerate(zip(targets, relatives)):
        np.subtract(target, relative, out=output[index])
        np.add(basis, output[index], out=output[index])
    if mirror:
        output[:, :, 0] *= -1.0
    return output


def pack_half_float3(values, *, threads=4, out=None):
    """Pack pairs directly without full-size uint32/index intermediates; None falls back."""
    lib = native_library()
    if lib is None:
        return None
    try:
        function = lib.herta_pack_half_float3
    except AttributeError:
        return None
    records = np.ascontiguousarray(values, dtype=np.float32)
    if records.ndim != 2 or records.shape[1] != 3:
        raise ValueError("Expected float3 records")
    count = len(records)
    size = ((count + 1) // 2) * 3
    result = np.empty(size, dtype=np.uint32) if out is None else out
    if result.dtype != np.uint32 or result.shape != (size,) or not result.flags.c_contiguous:
        raise ValueError("Invalid packed output layout")
    function.argtypes = [ct.c_void_p, ct.c_uint64, ct.c_uint, ct.c_void_p]
    function.restype = ct.c_int
    if function(records.ctypes.data, count, max(1, min(int(threads), 32)), result.ctypes.data):
        return result
    return None


def pack_half_float3_parts(parts):
    """Preserve global pair alignment between groups without concatenating float records."""
    lib = native_library()
    if lib is None or not hasattr(lib, "herta_pack_half_float3"):
        return None
    total = sum(len(part) for part in parts)
    output = np.empty(((total + 1) // 2) * 3, dtype=np.uint32)
    cursor = 0
    pending = None
    for values in parts:
        records = np.ascontiguousarray(values, dtype=np.float32)
        if not len(records):
            continue
        start = 0
        if pending is not None:
            pair = np.stack((pending, records[0]))
            if pack_half_float3(pair, out=output[cursor:cursor + 3]) is None:
                return None
            cursor += 3
            start = 1
            pending = None
        end = start + ((len(records) - start) // 2) * 2
        if end > start:
            words = (end - start) // 2 * 3
            if pack_half_float3(records[start:end], out=output[cursor:cursor + words]) is None:
                return None
            cursor += words
        if end < len(records):
            pending = records[-1].copy()
    if pending is not None:
        if pack_half_float3(pending.reshape(1, 3), out=output[cursor:cursor + 3]) is None:
            return None
    return output


def fast_path_reason(obj):
    """Conservative eligibility: anything requiring dependency evaluation falls back."""
    if os.environ.get("HERTA_SHAPEKEY_FAST", "1") == "0":
        return "disabled"
    if getattr(obj, "mode", "OBJECT") != "OBJECT" or getattr(obj, "show_only_shape_key", False):
        return "pinned/edit mode"
    keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    if keys is None or not keys.use_relative or keys.reference_key is None:
        return "absolute/no reference key"
    if getattr(keys, "animation_data", None) is not None or getattr(obj, "animation_data", None) is not None:
        return "animation/drivers"
    if any(getattr(modifier, "show_viewport", True) for modifier in obj.modifiers):
        return "evaluated modifiers"
    count = len(obj.data.vertices)
    for key in keys.key_blocks:
        if key.mute or key.vertex_group:
            return "muted/masked shape"
        if len(key.data) != count or key.relative_key is None or len(key.relative_key.data) != count:
            return "different topology"
    return ""


def packed_slot_deltas(base, target, columns, *, global_start=0, threads=4):
    """Fuse delta/isclose/zero canonicalization/stable packing; missing native returns None."""
    lib = native_library()
    if lib is None or not hasattr(lib, "herta_pack_deltas"):
        return None
    base = np.ascontiguousarray(base, dtype=np.float32)
    target = np.ascontiguousarray(target, dtype=np.float32)
    columns = np.ascontiguousarray(columns, dtype=np.uint32)
    if base.ndim != 2 or target.shape != base.shape or columns.ndim != 1:
        raise ValueError("Invalid slot delta layout")
    if not len(columns) or np.any(columns >= base.shape[1]):
        return None
    count = len(base)
    if global_start < 0 or global_start + count > np.iinfo(np.int32).max:
        return None
    packed = np.empty((count, len(columns)), dtype=np.float32)
    index_map = np.empty(count, dtype=np.int32)
    active_count = ct.c_uint64()
    function = lib.herta_pack_deltas
    function.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_uint64, ct.c_uint,
                         ct.c_void_p, ct.c_uint, ct.c_int64, ct.c_uint,
                         ct.c_void_p, ct.c_void_p, ct.c_void_p]
    function.restype = ct.c_int
    if function(base.ctypes.data, target.ctypes.data, count, base.shape[1], columns.ctypes.data,
                len(columns), global_start, max(1, min(int(threads), 32)),
                packed.ctypes.data, index_map.ctypes.data, ct.byref(active_count)):
        # Slots can be very sparse. Retaining the full-capacity array through a view
        # would pin vertices*channels bytes per slot until all slots have completed.
        return packed[:active_count.value].copy(), index_map
    return None


def read_coords(key):
    result = np.empty((len(key.data), 3), dtype=np.float32)
    key.data.foreach_get("co", result.ravel())
    return result


def capture_relative_shapes(obj, shape_keys, *, mirror=False):
    """Read bounded batches through RNA, then send only owned arrays to native threads."""
    reference = obj.data.shape_keys.reference_key
    raw_basis = read_coords(reference)
    basis = raw_basis.copy()
    if mirror:
        basis[:, 0] *= -1.0
    # Keep transient target/output arrays under roughly 64 MiB, up to 32 keys.
    batch_size = max(1, min(32, (32 * 1024 * 1024) // max(raw_basis.nbytes, 1)))

    def batches():
        for start in range(0, len(shape_keys), batch_size):
            keys = shape_keys[start:start + batch_size]
            targets, relatives = [], []
            refs = {reference.as_pointer(): raw_basis}
            for key in keys:
                targets.append(read_coords(key))
                relative = key.relative_key
                identity = relative.as_pointer()
                if identity not in refs:
                    refs[identity] = read_coords(relative)
                relatives.append(refs[identity])
            output = relative_shapes(raw_basis, targets, relatives, mirror=mirror)
            for index, key in enumerate(keys):
                yield key.name, output[index]

    return basis, batches()
