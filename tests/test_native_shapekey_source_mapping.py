import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "_native_shapekey_source_mapping_test"
spec = importlib.util.spec_from_file_location(
    MODULE_NAME,
    REPO_ROOT / "common" / "native_shapekey_attributes.py",
)
module = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = module
spec.loader.exec_module(module)


def _game_type():
    return SimpleNamespace(
        D3D11ElementList=[
            SimpleNamespace(
                Category="Position",
                SemanticName="POSITION",
                ByteWidth=12,
            ),
            SimpleNamespace(
                Category="Position",
                SemanticName="NORMAL",
                ByteWidth=12,
            ),
            SimpleNamespace(
                Category="Position",
                SemanticName="TANGENT",
                ByteWidth=16,
            ),
        ]
    )


class NativeShapeKeySourceMappingTests(unittest.TestCase):
    def test_native_delta_uses_source_vertex_ids_not_export_rows(self):
        stride = 40
        base = np.zeros((4, 10), dtype=np.float32)
        native_delta = module.NativeShapeKeyAttributeDelta(
            vertex_ids=np.asarray([10, 20, 30], dtype=np.uint32),
            deltas=np.asarray(
                [
                    [10, 11, 12, 13, 14, 15, 16, 17, 18],
                    [20, 21, 22, 23, 24, 25, 26, 27, 28],
                    [30, 31, 32, 33, 34, 35, 36, 37, 38],
                ],
                dtype=np.float32,
            ),
        )

        result = module.apply_native_shapekey_delta(
            base.tobytes(),
            base.tobytes(),
            export_indices=np.arange(4, dtype=np.int32),
            vertex_stride=stride,
            game_type=_game_type(),
            native_delta=native_delta,
            source_vertex_ids=np.asarray([30, 10, 20, 40], dtype=np.int32),
        )
        values = np.frombuffer(result, dtype=np.float32).reshape(4, 10)

        np.testing.assert_array_equal(values[0, :9], native_delta.deltas[2])
        np.testing.assert_array_equal(values[1, :9], native_delta.deltas[0])
        np.testing.assert_array_equal(values[2, :9], native_delta.deltas[1])
        np.testing.assert_array_equal(values[3], base[3])

    def test_duplicate_output_rows_share_the_same_source_delta(self):
        stride = 40
        base = np.zeros((2, 10), dtype=np.float32)
        native_delta = module.NativeShapeKeyAttributeDelta(
            vertex_ids=np.asarray([7], dtype=np.uint32),
            deltas=np.asarray([[1, 2, 3, 4, 5, 6, 7, 8, 9]], dtype=np.float32),
        )

        result = module.apply_native_shapekey_delta(
            base.tobytes(),
            base.tobytes(),
            export_indices=np.asarray([4, 5], dtype=np.int32),
            vertex_stride=stride,
            game_type=_game_type(),
            native_delta=native_delta,
            source_vertex_ids=np.asarray([7, 7], dtype=np.int32),
        )
        values = np.frombuffer(result, dtype=np.float32).reshape(2, 10)

        np.testing.assert_array_equal(values[0, :9], native_delta.deltas[0])
        np.testing.assert_array_equal(values[1, :9], native_delta.deltas[0])

    def test_source_id_count_mismatch_keeps_original_bytes(self):
        stride = 40
        base = np.arange(20, dtype=np.float32).reshape(2, 10)
        native_delta = module.NativeShapeKeyAttributeDelta(
            vertex_ids=np.asarray([0], dtype=np.uint32),
            deltas=np.asarray([[1, 1, 1, 1, 1, 1, 1, 1, 1]], dtype=np.float32),
        )

        result = module.apply_native_shapekey_delta(
            base.tobytes(),
            base.tobytes(),
            export_indices=np.arange(2, dtype=np.int32),
            vertex_stride=stride,
            game_type=_game_type(),
            native_delta=native_delta,
            source_vertex_ids=np.asarray([0], dtype=np.int32),
        )

        self.assertEqual(result, base.tobytes())


if __name__ == "__main__":
    unittest.main()
