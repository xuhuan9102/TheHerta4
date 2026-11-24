import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace

import numpy as np

MODULE = Path(__file__).resolve().parents[1] / "blueprint/native_expression_transfer.py"
spec = importlib.util.spec_from_file_location("native_expression_transfer", MODULE)
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class NativeExpressionTransferTests(unittest.TestCase):
    def test_reorders_native_vertex_pool_and_rejects_changed_geometry(self):
        native = np.arange(30, dtype="<f4").reshape((3, 10))
        exported_order = np.array([2, 0, 1], dtype="<u4")
        source_ib = np.array([0, 1, 2], dtype="<u4")
        result_ib = np.array([1, 2, 0], dtype="<u4")
        exported = native[exported_order].copy()

        mapping = transfer.match_native_vertex_order(exported, [(native, source_ib, result_ib)])
        np.testing.assert_array_equal(mapping, exported_order)

        exported[1, 0] += 0.125
        self.assertIsNone(transfer.match_native_vertex_order(exported, [(native, source_ib, result_ib)]))

    def test_rejects_conflicting_corner_mappings_even_for_identical_rows(self):
        native = np.zeros((3, 10), dtype="<f4")
        first = np.array([0, 1, 2], dtype="<u4")
        second = np.array([1, 0, 2], dtype="<u4")
        self.assertIsNone(
            transfer.match_native_vertex_order(native, [(native, first, first), (native, first, second)])
        )

    def test_authored_shape_key_keeps_existing_export_path(self):
        basis = np.zeros((3, 10), dtype="<f4")
        mapping = np.array([2, 0, 1], dtype="<u4")
        deltas = SimpleNamespace(
            vertex_ids=np.array([0], dtype="<u4"),
            deltas=np.array([[0.25, 0, 0, 0, 0, 0, 0, 0, 0]], dtype="<f4"),
        )
        slots = {1: {"Deform 0": ["mesh"]}}
        sampled = basis.copy()
        sampled[1, 0] = 0.25
        record = {"export_indices": [0, 1, 2], "position_bytes": sampled.tobytes()}
        args = (basis, mapping, {"Deform 0": deltas}, slots, {1: {"mesh": record}})
        self.assertTrue(transfer.matches_imported_shape_keys(*args))

        sampled[1, 0] = 0.5
        record["position_bytes"] = sampled.tobytes()
        self.assertFalse(transfer.matches_imported_shape_keys(*args))

    def test_moved_vertices_keep_native_order_when_uvs_confirm_identity(self):
        native = np.arange(30, dtype="<f4").reshape((3, 10))
        uv = np.array([[0.125, 0.25], [0.375, 0.5], [0.625, 0.75]], dtype="<f4")
        order = np.array([2, 0, 1], dtype="<u4")
        source_ib = np.array([0, 1, 2], dtype="<u4")
        export_ib = np.array([1, 2, 0], dtype="<u4")
        exported = native[order].copy()
        exported[1, 1] += 0.125
        parts = [(native, source_ib, export_ib)]

        np.testing.assert_array_equal(
            transfer.match_native_vertex_order(exported, parts, (uv, uv[order])),
            order,
        )
        self.assertIsNone(transfer.match_native_vertex_order(exported, parts, (uv, uv)))

    def test_transfer_reads_native_basis_and_preserves_exported_basis_separately(self):
        mesh = transfer.TransferMesh(
            "[TextureOverride_Test]", "ResourceTestPosition", "Meshes/Position.buf",
            np.zeros((3, 10)), np.arange(3, dtype="<u4"), np.ones((3, 10)),
        )
        _, sections = transfer.render_transfer_sections("feed1234", mesh, "Meshes/Map.buf")
        shader = sections["[CustomShaderZZMINativeExpression_feed1234]"]
        self.assertIn("cs-t106 = ResourceZZMINativeExpression_feed1234_NativeBasis", shader)
        self.assertIn("cs-u5 = copy ResourceZZMINativeExpression_feed1234_Basis", shader)
