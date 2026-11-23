import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

from tests import test_drag_cloth as fixtures


class ClothGestureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.GarmentExportTests.setUpClass()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_projected_arrow_routes_grab_and_normal_drag_uses_frozen_output(self):
        from tools.check_drag_cloth import GeneratedCloth

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = fixtures.GarmentExportTests()
            node, sections, comp = fixture.prepare(root, bake=False)
            settings = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            settings.include_objects = settings.include_objects[:1]
            start, end = np.eye(4), np.eye(4)
            start[:3, 3] = [-0.12, 0, 1.0]
            end[:3, 3] = [-0.12, 0, 0.65]
            settings.cloth_path_start = types.SimpleNamespace(matrix_world=start)
            settings.cloth_path_end = types.SimpleNamespace(matrix_world=end)
            node._get_reference_matrix_inv = lambda _comp: None
            node._get_export_space_matrix = lambda: None
            node._get_non_mirror_mirror = lambda: None
            clothes, bodies = node._cloth_object_names(required=True)
            comp["cloth"] = fixture.exporter.bake_component(
                node, temp, sections, comp, "unit", clothes, bodies, fixture.mod._blender_object_key,
            )
            node._emit_sections(sections, [comp], "unit")
            self.assertNotIn("[KeySSMTClothMode_unit]", sections)
            self.assertIn("condition = $ssmtdrag_drawn_unit == 1", sections["[KeySSMTClothReset_unit]"])
            runner = GeneratedCloth(temp, sections, comp)
            var = "$ssmtcloth_unit_abc123_"
            row = int(comp["cloth"]["garments"][0]["representatives"][20])

            # The downward path projects horizontally. This view also has
            # different horizontal and vertical calibration scales.
            camera = [(0, 0, 0.5), (2, 0, 0)]
            try:
                runner.prepare_identity()
                runner.force_full_solver = False
                runner.variables[var + "activated"] = 0
                for x in (800, 796):
                    np.testing.assert_array_equal(
                        runner.frame(600, True, row, cursor_x=x, camera_basis=camera, route_gesture=True),
                        runner.native,
                    )
                    self.assertEqual(runner.variables[var + "intent"], 0)
                for x in (780, 766, 752, 738, 724):
                    runner.frame(600, True, row, cursor_x=x, camera_basis=camera, route_gesture=True)
                self.assertEqual(runner.variables[var + "intent"], 2)
                runner.frame(630, True, row, cursor_x=724, camera_basis=camera, route_gesture=True)
                self.assertEqual(runner.variables[var + "intent"], 2)
                frozen = runner.frame(630, False, route_gesture=True).copy()
                self.assertFalse(np.array_equal(frozen, runner.native))

                runner.frame(630, True, row, cursor_x=724, camera_basis=camera, route_gesture=True)
                out = runner.frame(645, True, row, cursor_x=724, camera_basis=camera, route_gesture=True)
                self.assertEqual(runner.variables[var + "intent"], 1)
                np.testing.assert_array_equal(out, frozen)

                # Substitute a known displacement for the spring solver to
                # check the emitted draw handoff and TTL re-publication.
                displaced = frozen.copy().view("<f4").reshape(-1, 10)
                displaced[row, 0] += 0.01
                runner.external("ResourceTestDisplaced", displaced, 40, flags=1)
                sections["[ResourceTestNormalInput]"] = ["type = Buffer", "stride = 40"]
                sections["[CustomShaderDragJiggleabc123_unit]"] = [
                    "ResourceTestNormalInput = copy vb0",
                    "ResourceDragJiggleTempVB0_abc123_unit = ref ResourceTestDisplaced",
                ]
                sections["[CommandListTestClothDraw]"] = [
                    line for line in node._build_hook_block(comp, 0, "unit", include_detect=False)
                    if not line.strip().startswith("post ")
                ]
                runner.variables.update({
                    "$ssmtdrag_mode_unit": 1, "$ssmtdrag_last_dispatch_abc123_unit": -1,
                    "$ssmtdrag_viewport_probe_armed_unit": 0,
                })
                runner.execute("CommandListTestClothDraw")
                base = runner.resources["ResourceTestNormalInput"]
                actual = runner.gpu.read_bytes(base.pointer, base.size).reshape(-1, 40)
                np.testing.assert_array_equal(actual, frozen)
                runner.execute(comp["cloth_publish"])
                self.assertEqual(runner.resources["vb0"].pointer, runner.resources["ResourceTestDisplaced"].pointer)
                runner.execute(comp["cloth_restore"])
                np.testing.assert_array_equal(runner.frame(645, False, route_gesture=True), frozen)
            finally:
                runner.close()
