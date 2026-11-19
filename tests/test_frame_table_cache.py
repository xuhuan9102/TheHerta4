import importlib.util
from pathlib import Path
import tempfile
import unittest


def load_cache():
    path = Path(__file__).resolve().parents[1] / "blueprint/frame_table_cache.py"
    spec = importlib.util.spec_from_file_location("frame_table_cache_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FrameTableCache


class FrameTableCacheTests(unittest.TestCase):
    def test_pose_groups_survive_a_new_table_context_and_unrelated_edits(self):
        import numpy

        cache_type = load_cache()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "table.zip"
            shape = (2, 3, 10)
            poses = numpy.arange(numpy.prod(shape), dtype=numpy.float32).reshape(shape)
            cold = cache_type(path, "old configuration")
            cold.store_pose_group("mesh-a", 0, "group-a", poses)
            cold.store_pose_group("mesh-a", 1, "group-b", poses + 2)
            cold.store_pose_group("mesh-b", 0, "group-c", poses + 3)

            warm = cache_type(path, "new configuration")
            numpy.testing.assert_array_equal(warm.load_pose_group("mesh-a", 0, "group-a", shape), poses)
            self.assertIsNone(warm.load_pose_group("mesh-a", 1, "changed-group-b", shape))
            numpy.testing.assert_array_equal(warm.load_pose_group("mesh-b", 0, "group-c", shape), poses + 3)
            self.assertIsNone(warm.load_pose_group("mesh-a", 0, "group-a", (2, 5, 10)))

            warm.store_pose_group("mesh-a", 1, "changed-group-b", poses + 4)
            self.assertIsNone(cold.load_pose_group("mesh-a", 1, "group-b", shape))
            numpy.testing.assert_array_equal(cold.load_pose_group("mesh-a", 0, "group-a", shape), poses)

    def test_reuses_tangent_baseline_with_the_deltas_that_were_built_from_it(self):
        import struct

        cache_type = load_cache()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "Meshes0000"
            folder.mkdir()
            base = folder / "hash-Position.buf"
            table = folder / "hash-Position_frame_table.buf"
            original = struct.pack("<5f", 1.0, 0.1, 0.2, 0.3, -1.0)
            base.write_bytes(original)
            path = root / "frames.zip"
            cold = cache_type(path, "profile")
            cold.record_sample("sample")
            cold.finish_probe(root)
            table.write_bytes(b"table")
            cold.store(root, [table.relative_to(root)], {"layout": 20}, {base.name: (20, 1, 3)})
            table.unlink()
            base.write_bytes(struct.pack("<5f", 1.0, 0.10001, 0.2, 0.3, -1.0))
            warm = cache_type(path, "profile")
            self.assertTrue(warm.record_sample("sample"))
            self.assertTrue(warm.finish_probe(root))
            self.assertEqual(base.read_bytes(), original)
            self.assertEqual(table.read_bytes(), b"table")

    def test_reuses_packed_files_only_for_matching_sample_and_base(self):
        cache_type = load_cache()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "table.zip"
            original = root / "original"
            buffers = original / "Meshes0000"
            buffers.mkdir(parents=True)
            base = Path("Meshes0000/hash-Position.buf")
            frames = Path("Meshes0000/hash-Position_frame_table.buf")
            (original / base).write_bytes(b"base")
            cache = cache_type(bundle, "blueprint-1")
            self.assertFalse(cache.record_sample("geometry"))
            self.assertFalse(cache.finish_probe(original))
            (original / frames).write_bytes(b"frames")
            cache.store(original, [frames], {"position_stride": 40})

            output = root / "output"
            (output / base.parent).mkdir(parents=True)
            (output / base).write_bytes(b"base")
            cached = cache_type(bundle, "blueprint-1")
            self.assertTrue(cached.record_sample("geometry"))
            self.assertTrue(cached.finish_probe(output))
            self.assertEqual((output / frames).read_bytes(), b"frames")
            self.assertEqual(cached.ini_data, {"position_stride": 40})

            changed = cache_type(bundle, "blueprint-1")
            self.assertFalse(changed.record_sample("changed shape"))
            self.assertFalse(changed.finish_probe(output))
            (output / base).write_bytes(b"different vertex order")
            mismatched_base = cache_type(bundle, "blueprint-1")
            self.assertTrue(mismatched_base.record_sample("geometry"))
            self.assertFalse(mismatched_base.finish_probe(output))

            reordered = cache_type(bundle, "different sequence")
            self.assertFalse(reordered.record_sample("geometry"))
            self.assertFalse(reordered.finish_probe(output))
