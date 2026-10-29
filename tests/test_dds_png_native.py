"""Native conversion boundaries and atomic publication, independent of bpy."""

import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "_herta_png_native_test", Path(__file__).resolve().parents[1] / "toolkit" / "_dds_png_native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativePngTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "贴图.dds"
        self.output = self.source.with_suffix(".png")

    def test_missing_engine_allows_fallback_without_touching_files(self):
        self.output.write_bytes(b"existing")
        with mock.patch.object(native, "native_library", return_value=None):
            self.assertIsNone(native.convert(self.source, self.output))
        self.assertEqual(self.output.read_bytes(), b"existing")

    def test_failed_partial_output_never_replaces_existing_png(self):
        self.output.write_bytes(b"existing")
        class BrokenEngine:
            def herta_dds_to_png(self, source, destination, level, stats):
                Path(destination).write_bytes(b"partial")
                return 2
        with mock.patch.object(native, "native_library", return_value=BrokenEngine()):
            with self.assertRaises(RuntimeError):
                native.convert(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"existing")
        self.assertEqual(sorted(p.name for p in Path(self.directory.name).iterdir()), [self.output.name])

    def test_unsupported_removes_temporary_and_allows_fallback(self):
        engine = mock.Mock()
        engine.herta_dds_to_png.return_value = 1
        with mock.patch.object(native, "native_library", return_value=engine):
            self.assertIsNone(native.convert(self.source, self.output))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_real_rgba_engine_pixels_unicode_paths_and_source_retention(self):
        if native.native_library() is None:
            self.skipTest("Optional Windows native DLL unavailable")
        from PIL import Image
        # Odd width, padded DDS row pitch, and varying alpha guard layout bugs.
        width, height, pitch = 7, 5, 32
        pixels = bytes((i * 37) % 256 for i in range(width * height * 4))
        header = bytearray(148)
        header[:4] = b"DDS "
        struct.pack_into("<I", header, 4, 124)
        struct.pack_into("<I", header, 8, 0x100f)
        struct.pack_into("<III", header, 12, height, width, pitch)
        struct.pack_into("<II", header, 76, 32, 4)
        header[84:88] = b"DX10"
        struct.pack_into("<5I", header, 128, 29, 3, 0, 1, 0)
        data = bytes(header) + b"".join(pixels[y*width*4:(y+1)*width*4] + b"PAD!" for y in range(height))
        self.source.write_bytes(data)
        stats = native.convert(self.source, self.output)
        self.assertEqual((stats["width"], stats["height"], stats["depth"]), (width, height, 8))
        with Image.open(self.output) as image:
            self.assertEqual(image.tobytes(), pixels)
            self.assertIn("srgb", image.info)
        self.assertEqual(self.source.read_bytes(), data)
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 2)

    def test_truncated_dds_is_rejected_without_replacing_output(self):
        if native.native_library() is None:
            self.skipTest("Optional Windows native DLL unavailable")
        self.source.write_bytes(b"DDS " + bytes(124))
        self.output.write_bytes(b"existing")
        self.assertIsNone(native.convert(self.source, self.output))
        self.assertEqual(self.output.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
