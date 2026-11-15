"""Reject geometry failures that a successful compute dispatch cannot detect."""

import unittest

import numpy as np

from tools.check_drag_cloth_mesh import audit, winding_numbers


def cube(offset=0):
    vertices = np.array(
        [
            [-1, -1, -1],
            [1, -1, -1],
            [1, 1, -1],
            [-1, 1, -1],
            [-1, -1, 1],
            [1, -1, 1],
            [1, 1, 1],
            [-1, 1, 1],
        ],
        dtype=float,
    )
    vertices[:, 0] += offset
    faces = np.array(
        [
            [0, 2, 1],
            [0, 3, 2],
            [4, 5, 6],
            [4, 6, 7],
            [0, 1, 5],
            [0, 5, 4],
            [1, 2, 6],
            [1, 6, 5],
            [2, 3, 7],
            [2, 7, 6],
            [3, 0, 4],
            [3, 4, 7],
        ],
        dtype=np.uint32,
    )
    return vertices, faces


def snapshot(cloth):
    body, faces = cube()
    source = np.r_[body, np.asarray(cloth, float)]
    return (
        source,
        np.r_[np.zeros(len(body), bool), np.ones(3, bool)],
        faces,
        [[8, 9, 10]],
    )


class MeshAuditTests(unittest.TestCase):
    def test_overlapping_solids_do_not_cancel_as_parity_does(self):
        a, f = cube()
        b, g = cube(1)
        triangles = np.r_[a[f], b[g]]
        # The first point is inside A but just outside B. Its closest face is
        # B's left face and incorrectly suggests that it is outside the union.
        winding = winding_numbers([[-0.01, 0, 0], [0.5, 0, 0], [3, 0, 0]], triangles)
        np.testing.assert_allclose(winding, [1, 2, 0], atol=1e-12)
        np.testing.assert_allclose(
            winding_numbers([[0.5, 0, 0]], triangles[:, ::-1]), [-2]
        )

    def test_fully_embedded_triangle_fails_without_a_surface_intersection(self):
        source, mask, body, faces = snapshot([[2, 0, 0], [2, 0.2, 0], [2, 0, 0.2]])
        output = source.copy()
        output[mask, 0] = 0
        result = audit(source, output, mask, body, faces)
        self.assertEqual(result["output_intersections"], 0)
        self.assertEqual(result["new_inside_vertices"], 3)
        self.assertFalse(result["passed"])

    def test_face_piercing_fails_even_when_all_cloth_vertices_are_outside(self):
        source, mask, body, faces = snapshot([[-3, -3, 2], [3, -3, 2], [0, 3, 2]])
        output = source.copy()
        output[mask, 2] = 0
        result = audit(source, output, mask, body, faces)
        self.assertEqual(result["output_inside_vertices"], 0)
        self.assertEqual(result["new_intersections"], 1)
        self.assertFalse(result["passed"])

    def test_native_intersections_are_reported_but_do_not_excuse_the_output(self):
        source, mask, body, faces = snapshot([[-3, -3, 0], [3, -3, 0], [0, 3, 0]])
        result = audit(source, source.copy(), mask, body, faces)
        self.assertEqual(result["source_intersections"], 1)
        self.assertEqual(result["new_intersections"], 0)
        self.assertFalse(result["passed"])

    def test_safe_motion_passes_and_body_changes_or_spikes_fail(self):
        source, mask, body, faces = snapshot([[2, 0, 0], [2, 0.2, 0], [2, 0, 0.2]])
        output = source.copy()
        output[mask, 0] += 0.1
        self.assertTrue(audit(source, output, mask, body, faces)["passed"])
        output[0, 0] += 0.01
        self.assertFalse(audit(source, output, mask, body, faces)["passed"])
        output = source.copy()
        output[-1, 2] = 5
        self.assertFalse(audit(source, output, mask, body, faces)["passed"])

    def test_invalid_numbers_fail_without_running_geometry_queries(self):
        source, mask, body, faces = snapshot([[2, 0, 0], [2, 0.2, 0], [2, 0, 0.2]])
        output = source.copy()
        output[-1, 0] = np.nan
        result = audit(source, output, mask, body, faces)
        self.assertFalse(result["finite"])
        self.assertFalse(result["passed"])

    def test_material_stretch_uses_rest_shape_instead_of_a_compressed_pose(self):
        rest, mask, body, faces = snapshot([[2, 0, 0], [2, 0.2, 0], [2, 0, 0.2]])
        posed = rest.copy()
        posed[-1, 2] = 0.04
        output = rest.copy()
        result = audit(posed, output, mask, body, faces, material_reference=rest)
        self.assertTrue(result["passed"])
        self.assertAlmostEqual(result["maximum_edge_stretch"], 1)
        self.assertAlmostEqual(result["maximum_pose_edge_change"], 5)
        self.assertFalse(audit(posed, output, mask, body, faces)["passed"])


if __name__ == "__main__":
    unittest.main()
