import importlib.util
from pathlib import Path
import unittest

import numpy as np

from tools.check_drag_cloth_mesh import winding_numbers
from tests.test_drag_cloth_mesh import cube


path = Path(__file__).resolve().parents[1] / "blueprint/drag_cloth_regions.py"
spec = importlib.util.spec_from_file_location("anatomical_cloth_regions", path)
regions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(regions)


class AnatomicalRegionTests(unittest.TestCase):
    def test_arms_follow_hierarchy_while_breasts_and_both_legs_remain_trunk(self):
        parents = {
            "DEF-Hips": None,
            "DEF-Chest": "DEF-Hips",
            "Left shoulder": "DEF-Chest",
            "DEF-Left arm": "Left shoulder",
            "DEF-Left elbow": "DEF-Left arm",
            "DEF-Left wrist": "DEF-Left elbow",
            "cf_s_elboback_L": "DEF-Left elbow",
            "Right shoulder": "DEF-Chest",
            "DEF-Right arm": "Right shoulder",
            "DEF-Right wrist": "DEF-Right arm",
            "cf_s_bust03_L": "DEF-Chest",
            "cf_s_bust03_R": "DEF-Chest",
            "DEF-Left leg": "DEF-Hips",
            "DEF-Right leg": "DEF-Hips",
        }
        labels = regions.bone_regions(parents)
        for name in (
            "DEF-Left arm",
            "DEF-Left elbow",
            "DEF-Left wrist",
            "cf_s_elboback_L",
        ):
            self.assertEqual(labels[name], 1)
        self.assertEqual(labels["DEF-Right wrist"], 2)
        for name in (
            "DEF-Hips",
            "DEF-Chest",
            "cf_s_bust03_L",
            "cf_s_bust03_R",
            "DEF-Left leg",
            "DEF-Right leg",
        ):
            self.assertEqual(labels[name], 0)

    def test_cut_proxy_closes_socket_and_preserves_outward_volume(self):
        vertices, faces = cube()
        # The top is the cut shoulder socket. Also exercise inconsistent
        # imported winding, without relying on render vertex normals.
        open_faces = np.delete(faces, [2, 3], axis=0)
        open_faces[3] = open_faces[3, ::-1]
        closed, skin_faces = regions.cap_domain(vertices, open_faces)
        self.assertEqual(skin_faces, 10)
        self.assertEqual(len(closed), 12)
        edge = np.sort(
            np.r_[closed[:, [0, 1]], closed[:, [1, 2]], closed[:, [2, 0]]], axis=1
        )
        _, counts = np.unique(edge, axis=0, return_counts=True)
        np.testing.assert_array_equal(counts, 2)
        np.testing.assert_allclose(
            winding_numbers([[0, 0, 0], [0, 0, 2]], vertices[closed]),
            [1, 0],
            atol=1e-12,
        )
        self.assertLess(int(closed.max()), len(vertices))

    def test_closed_proxy_needs_no_extra_faces(self):
        vertices, faces = cube()
        closed, skin_faces = regions.cap_domain(vertices, faces)
        self.assertEqual(skin_faces, len(faces))
        self.assertEqual(len(closed), len(faces))


if __name__ == "__main__":
    unittest.main()
