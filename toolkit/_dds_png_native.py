"""Optional DDS -> PNG C pipeline. Filesystem only; safe on conversion workers."""

import ctypes as ct
import os
from pathlib import Path
import tempfile
import threading


class _Stats(ct.Structure):
    _fields_ = (
        [(name, ct.c_uint32) for name in ("width", "height", "depth", "dxgi")]
        + [(name, ct.c_uint64) for name in ("input_bytes", "output_bytes")]
        + [(name, ct.c_double) for name in
           ("read_ms", "decode_ms", "filter_ms", "compress_ms", "write_ms")]
    )


_library = None
_checked = False
_load_lock = threading.Lock()
# CPU work needs fewer simultaneous calls than texconv's process/IO waits.
# No inner native thread pool: eight independent file pipelines at most.
_slots = threading.BoundedSemaphore(8)


def native_library():
    global _library, _checked
    if os.environ.get("HERTA_DDS_PNG_NATIVE", "1") == "0":
        return None
    with _load_lock:
        if not _checked:
            try:
                path = Path(__file__).resolve().parents[1] / "common" / "native" / "herta_dds_png_windows_x64.dll"
                lib = ct.CDLL(str(path))
                lib.herta_dds_png_abi_version.restype = ct.c_uint
                if lib.herta_dds_png_abi_version() != 1:
                    raise ValueError("DDS PNG native ABI")
                lib.herta_dds_to_png.argtypes = [ct.c_wchar_p, ct.c_wchar_p, ct.c_int, ct.POINTER(_Stats)]
                lib.herta_dds_to_png.restype = ct.c_int
                _library = lib
            except (OSError, AttributeError, ValueError):
                pass
            _checked = True
    return _library


def convert(source, destination):
    """Return stage statistics, or None to use texconv for an unsupported DDS.

    The caller implements the existing-PNG reuse policy. Publish only a fully
    written PNG; errors never replace a previous destination or delete the DDS.
    """
    lib = native_library()
    if lib is None:
        return None
    with _slots:
        handle, temporary = tempfile.mkstemp(prefix=".herta_png_", suffix=".tmp", dir=os.path.dirname(destination))
        os.close(handle)
        try:
            stats = _Stats()
            # Level 1 is lossless; changes compression size, never image data.
            status = lib.herta_dds_to_png(os.fspath(source), os.fspath(temporary), 1, ct.byref(stats))
            if status == 1:
                return None
            if status:
                raise RuntimeError("DDS PNG native status %d" % status)
            with open(temporary, "rb") as output:
                header = output.read(26)
            if len(header) != 26 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[24] != stats.depth:
                raise RuntimeError("DDS PNG native output header")
            os.replace(temporary, destination)
            return {name: getattr(stats, name) for name, _kind in stats._fields_}
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
