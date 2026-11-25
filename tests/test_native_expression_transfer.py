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

    def test_accepts_duplicate_export_rows_for_shared_native_pool(self):
        native = np.arange(30, dtype="<f4").reshape((3, 10))
        source_ib = np.array([0, 1, 2, 0, 1], dtype="<u4")
        export_ib = np.arange(len(source_ib), dtype="<u4")
        exported = native[source_ib].copy()

        mapping = transfer.match_native_vertex_order(
            exported, [(native, source_ib, export_ib)]
        )

        np.testing.assert_array_equal(mapping, source_ib)

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

    def test_duplicate_export_rows_use_uvs_for_shared_native_pool(self):
        native = np.arange(30, dtype="<f4").reshape((3, 10))
        uv = np.array([[0.125, 0.25], [0.375, 0.5], [0.625, 0.75]], dtype="<f4")
        source_ib = np.array([0, 1, 2, 0, 1], dtype="<u4")
        export_ib = np.arange(len(source_ib), dtype="<u4")
        exported = native[source_ib].copy()
        exported[3, 1] += 0.125

        mapping = transfer.match_native_vertex_order(
            exported,
            [(native, source_ib, export_ib)],
            (uv, uv[source_ib]),
        )

        np.testing.assert_array_equal(mapping, source_ib)

    def test_transfer_reads_native_basis_and_preserves_exported_basis_separately(self):
        mesh = transfer.TransferMesh(
            "[TextureOverride_Test_Position]", "ResourceTestPosition", "Meshes/Position.buf",
            "[TextureOverride_Test_Blend]", "ResourceTestBlend",
            np.zeros((3, 10)), np.arange(3, dtype="<u4"), np.ones((3, 10)),
        )
        _, sections = transfer.render_transfer_sections("feed1234", mesh, "Meshes/Map.buf")
        shader = sections["[CustomShaderZZMINativeExpression_feed1234]"]
        self.assertIn("cs-t106 = ResourceZZMINativeExpression_feed1234_NativeBasis", shader)
        self.assertIn("cs-t108 = ResourceZZMINativeExpression_feed1234_StaticPosition", shader)
        self.assertIn("cs-u5 = copy ResourceZZMINativeExpression_feed1234_AdjustedPosition", shader)
        self.assertIn("vb0 = ResourceZZMINativeExpression_feed1234_AdjustedPosition", shader)
        self.assertNotIn("ResourceTestPosition = copy cs-u5", shader)

    def test_dispatch_result_is_written_back_before_binding_vertex_buffer(self):
        mesh = transfer.TransferMesh(
            "[TextureOverride_Test_Position]", "ResourceTestPosition", "Meshes/Position.buf",
            "[TextureOverride_Test_Blend]", "ResourceTestBlend",
            np.zeros((3, 10)), np.arange(3, dtype="<u4"), np.ones((3, 10)),
        )
        _, sections = transfer.render_transfer_sections("feed1234", mesh, "Meshes/Map.buf")
        shader = sections["[CustomShaderZZMINativeExpression_feed1234]"]
        dispatch_index = shader.index("Dispatch = 1, 1, 1")
        writeback_index = shader.index(
            "ResourceZZMINativeExpression_feed1234_AdjustedPosition = copy cs-u5"
        )
        bind_index = shader.index("vb0 = ResourceZZMINativeExpression_feed1234_AdjustedPosition")
        self.assertLess(dispatch_index, writeback_index)
        self.assertLess(writeback_index, bind_index)
        self.assertNotIn("ResourceTestPosition = copy cs-u5", shader)

    def test_transfer_uses_blend_anchor_and_leaves_static_position_untouched(self):
        position_override = "[TextureOverride_VB_feed1234_head_Position]"
        blend_override = "[TextureOverride_VB_feed1234_head_Blend]"
        sections = {
            position_override: [
                "hash = dynamic123",
                "vb2 = ResourceTestBlend",
                "vb0 = ResourceTestPosition",
                "handling = skip",
                "draw = 3, 0",
            ],
            blend_override: ["hash = stable456"],
        }
        mesh = transfer.TransferMesh(
            position_override, "ResourceTestPosition", "Meshes/Position.buf",
            blend_override, "ResourceTestBlend",
            np.zeros((3, 10)), np.arange(3, dtype="<u4"), np.ones((3, 10)),
        )

        transfer.install_transfer_sections(sections, "feed1234", mesh, "Meshes/Map.buf")
        transfer.install_transfer_sections(sections, "feed1234", mesh, "Meshes/Map.buf")

        self.assertEqual(sections[blend_override], [
            "hash = stable456",
            "match_vertex_count = 3",
            "run = CustomShaderZZMINativeExpression_feed1234",
            "vb2 = ResourceTestBlend",
            "handling = skip",
            "draw = 3, 0",
        ])
        self.assertEqual(sections[position_override], ["hash = dynamic123"])
        shader = sections["[CustomShaderZZMINativeExpression_feed1234]"]
        self.assertIn("vb0 = ResourceZZMINativeExpression_feed1234_AdjustedPosition", shader)
        self.assertNotIn("ResourceTestPosition = copy cs-u5", shader)

    def test_blend_filter_uses_native_vertex_count(self):
        position_override = "[TextureOverride_VB_feed1234_head_Position]"
        blend_override = "[TextureOverride_VB_feed1234_head_Blend]"
        sections = {
            position_override: ["hash = dynamic123", "vb2 = ResourceTestBlend"],
            blend_override: ["hash = stable456"],
        }
        mesh = transfer.TransferMesh(
            position_override,
            "ResourceTestPosition",
            "Meshes/Position.buf",
            blend_override,
            "ResourceTestBlend",
            np.zeros((5, 10)),
            np.array([0, 1, 2, 0, 1], dtype="<u4"),
            np.ones((3, 10)),
        )

        transfer.install_transfer_sections(sections, "feed1234", mesh, "Meshes/Map.buf")

        blend = sections[blend_override]
        self.assertIn("match_vertex_count = 3", blend)
        self.assertIn("draw = 5, 0", blend)
