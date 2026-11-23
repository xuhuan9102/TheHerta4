import unittest

from common.shapekey_import_layout import infer_shapekey_vertex_offset_stride


class ShapeKeyImportLayoutTests(unittest.TestCase):
    def test_detects_position_only_zzmi_layout(self):
        self.assertEqual(
            infer_shapekey_vertex_offset_stride(7067, 7067 * 3),
            3,
        )

    def test_detects_position_normal_tangent_wwmi_layout(self):
        self.assertEqual(
            infer_shapekey_vertex_offset_stride(100, 100 * 6),
            6,
        )

    def test_rejects_unknown_layout(self):
        self.assertEqual(infer_shapekey_vertex_offset_stride(100, 400), 0)
        self.assertEqual(infer_shapekey_vertex_offset_stride(0, 0), 0)


if __name__ == "__main__":
    unittest.main()
