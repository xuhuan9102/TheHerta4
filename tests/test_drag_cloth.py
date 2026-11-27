"""Surface garment topology, optional generation and render-routing regressions."""

import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tests.test_node_postprocess_draginteraction import _load_drag_module, _make_node


def cylinder(
    center, radius, rings=10, sides=12, height=1.0, cap_top=False, cap_bottom=True
):
    p = []
    n = []
    f = []
    for z in np.linspace(0, height, rings):
        for angle in np.arange(sides) * 2 * np.pi / sides:
            radial = np.array([np.cos(angle), np.sin(angle), 0])
            p.append(radial * radius + [center, 0, z])
            n.append(radial)
    for row in range(rings - 1):
        for col in range(sides):
            a = row * sides + col
            b = row * sides + (col + 1) % sides
            c = a + sides
            d = b + sides
            f.extend(((a, b, c), (b, d, c)))
    if cap_bottom:
        bottom = len(p)
        p.append([center, 0, 0])
        n.append([0, 0, -1])
        for col in range(sides):
            f.append((bottom, (col + 1) % sides, col))
    if cap_top:
        top = len(p)
        p.append([center, 0, height])
        n.append([0, 0, 1])
        start = (rings - 1) * sides
        for col in range(sides):
            f.append((top, start + col, start + (col + 1) % sides))
    return np.array(p, float), np.array(n, float), np.array(f, np.uint32)


def fixture(two=True, dense=False, open_ends=False):
    pieces = []
    names = []
    cloth = []
    body = []
    offset = 0
    for index, center in enumerate((-0.12, 0.12) if two else (-0.12,)):
        p, n, f = cylinder(
            center,
            0.055,
            rings=24 if dense else 10,
            sides=24 if dense else 12,
            cap_bottom=not open_ends,
        )
        pieces.append((p, n, f + offset))
        cloth.append(f + offset)
        names.extend(["Stocking" + str(index)] * len(f))
        offset += len(p)
    for center in (-0.12, 0.12):
        p, n, f = cylinder(center, 0.05, height=1.1, cap_top=True)
        pieces.append((p, n, f + offset))
        body.append(f + offset)
        names.extend(["Body"] * len(f))
        offset += len(p)
    return (
        np.concatenate([p[0] for p in pieces]),
        np.concatenate([p[1] for p in pieces]),
        np.concatenate([p[2] for p in pieces]),
        np.array(names, object),
        np.concatenate(cloth),
        np.concatenate(body),
    )


class GarmentGeometryTests(unittest.TestCase):
    def test_short_skin_mix_is_continuous_across_the_connected_centre(self):
        _load_drag_module()
        geometry = importlib.import_module('_ssmt_root.blueprint.drag_cloth_geometry')
        points = np.array([[x, y, 0] for y in (0, 1) for x in (-1, -.01, .01, 1)])
        faces = np.array([[i, i+1, i+4] for i in range(3)] + [[i+1, i+5, i+4] for i in range(3)], np.uint32)
        mix = geometry.shorts_skin_mix(points, faces, points[:, 0], 1)
        np.testing.assert_array_equal(mix[[0, 3, 4, 7]], [1, 0, 1, 0])
        np.testing.assert_array_equal(mix[[1, 2, 5, 6]], .5)

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()
        cls.geom = importlib.import_module("_ssmt_root.blueprint.drag_cloth_geometry")

    def test_limb_guide_stays_on_its_branch_past_the_end(self):
        proxy = importlib.import_module("_ssmt_root.blueprint.drag_cloth_proxy_core")
        left, _, left_faces = cylinder(-.12, .05, height=1.0, cap_bottom=True)
        right, _, right_faces = cylinder(.12, .05, height=1.1, cap_bottom=True)
        right[:, 2] -= .25
        cloth, _, _ = cylinder(-.12, .055, height=.15)
        cloth[:, 2] += .55
        body = np.concatenate((left[left_faces], right[right_faces]), axis=0)
        sections = proxy.BodySections(body, cloth, -.15, .75, 40, extend_lower=True)
        np.testing.assert_allclose(sections.centers[:, 0], -.12, atol=.01)
        np.testing.assert_array_equal(sections.centers[0], sections.centers[1])
        self.assertTrue(np.isfinite(sections.radii).all())

    def test_compression_guide_fits_a_small_endpoint_overhang(self):
        proxy = importlib.import_module("_ssmt_root.blueprint.drag_cloth_proxy_core")
        body, _, faces = cylinder(0, .05, height=1, cap_bottom=True, cap_top=True)
        cloth, _, _ = cylinder(0, .055, height=.8015)
        cloth[:, 2] -= .0015
        sections = proxy.BodySections(body[faces], cloth, -.0025, .801, 40, fit_ends=True)
        self.assertAlmostEqual(float(sections.heights[0]), -.0025)
        self.assertTrue(np.isfinite(sections.radii).all())
        np.testing.assert_allclose(sections.centers[0], (0, 0), atol=.01)
        self.assertGreater(float(sections.radii[0].min()), .04)
        with self.assertRaises(proxy.ProxySectionCoverageError):
            proxy.BodySections(body[faces], cloth, -.08, .801, 40, fit_ends=True)

    def test_intrinsic_surface_coordinate_retains_tube_boundaries(self):
        p, _n, f = cylinder(0, 0.1, rings=21, sides=24, cap_bottom=True, cap_top=True)
        field = self.geom.intrinsic_levels(
            p, f, np.ones(len(f), np.uint32), np.zeros(3), np.array([0, 0, 1.0]), 1
        )
        np.testing.assert_allclose(field, p[:, 2], atol=1e-6)
        self.assertTrue(np.isfinite(field).all())

    def test_intrinsic_surface_coordinate_does_not_inherit_an_overhang_extremum(self):
        p, _n, f = cylinder(0, 0.1, rings=31, sides=24, cap_bottom=True, cap_top=True)
        original = p[:, 2].copy()
        front = np.maximum(p[:, 1] / 0.1, 0) ** 2
        bulge = front * np.exp(-(((original - 0.55) / 0.10) ** 2))
        p[:, 1] += bulge * 0.18
        p[:, 2] += bulge * 0.24
        field = self.geom.intrinsic_levels(
            p, f, np.ones(len(f), np.uint32), np.zeros(3), np.array([0, 0, 1.0]), 1
        )
        self.assertTrue(np.isfinite(field).all())
        self.assertGreaterEqual(float(field.min()), -1e-6)
        self.assertLessEqual(float(field.max()), 1 + 1e-6)
        self.assertGreater(float(np.max(abs(field - p[:, 2]))), 0.03)
        np.testing.assert_allclose(field[original == 0], 0, atol=1e-6)
        np.testing.assert_allclose(field[original == 1], 1, atol=1e-6)

    def test_disconnected_stockings_have_independent_maps_and_own_leg_hulls(self):
        p, n, _, _, cloth, body = fixture()
        data = self.geom.bake_garments(p, n, cloth, body, exterior=False)
        self.assertEqual(len(data), 2)
        masks = [d["map"] != 0xFFFFFFFF for d in data]
        self.assertFalse(np.any(masks[0] & masks[1]))
        self.assertEqual({int(d["config"][12, 3]) for d in data}, {1, 2})
        for d in data:
            self.assertLessEqual(len(d["representatives"]), 2450)
            np.testing.assert_allclose(
                d["display_weights"][d["map"] != 0xFFFFFFFF, :3].sum(1), 1
            )
            self.assertTrue(np.all(d["map"][np.unique(body)] == 0xFFFFFFFF))

    def test_connected_garment_emits_one_synchronized_runtime(self):
        p, n, f = cylinder(0, 0.18)
        b, bn, bf = cylinder(0, 0.15, height=1.1, cap_top=True)
        data = self.geom.bake_garments(
            np.r_[p, b], np.r_[n, bn], f, bf + len(p), exterior=False
        )
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["config"][0, 3], 1)

    def test_simplification_retains_render_mapping_and_coloring(self):
        p, n, _, _, cloth, body = fixture(two=False, dense=True)
        data = self.geom.bake_garments(p, n, cloth, body, budget=160, exterior=False)[0]
        self.assertLessEqual(len(data["representatives"]), 160)
        mapped = data["map"] != 0xFFFFFFFF
        np.testing.assert_allclose(
            data["display_weights"][mapped, :3].sum(1), 1, atol=1e-6
        )
        self.assertTrue(
            np.all(
                data["display_triangles"].reshape(-1, 3)[mapped]
                < len(data["representatives"])
            )
        )
        edges = data["edges"][:, :2].copy().view(np.uint32)
        for begin, end in zip(data["edge_offsets"][:-1], data["edge_offsets"][1:]):
            self.assertEqual(len(np.unique(edges[begin:end])), 2 * (end - begin))

    def test_body_surface_mask_does_not_discard_an_exterior_cylinder(self):
        p, n, _, _, cloth, body = fixture(two=False)
        data = self.geom.bake_garments(p, n, cloth, body)[0]
        self.assertGreater(np.mean(data["surface_mask"]), 0.98)

    def test_shared_body_and_cloth_rows_fail_instead_of_deforming_body(self):
        p, n, _, _, cloth, body = fixture()
        with self.assertRaisesRegex(ValueError, "相同顶点"):
            self.geom.bake_garments(p, n, cloth, np.r_[body, cloth[:1]], exterior=False)

    def test_two_end_geometry_has_independent_rims_and_no_fixed_foot(self):
        p, n, _, _, cloth, body = fixture(two=False, open_ends=True)
        marked = np.zeros(len(p), np.uint8)
        marked[np.unique(cloth)] = 2
        data = self.geom.bake_garments(
            p, n, cloth, body, exterior=False, two_ended_rows=marked
        )[0]
        upper, lower = data["config"][13, :2].astype(int)
        self.assertGreater(upper, 2)
        self.assertGreater(lower, 2)
        self.assertLessEqual(upper + lower, 128)
        self.assertTrue(np.all(data["rest"][::2, 3] == 1))
        top = data["hem_params"][:upper, 3].astype(int)
        bottom = data["hem_params"][upper:, 3].astype(int)
        self.assertFalse(np.intersect1d(top, bottom).size)
        np.testing.assert_allclose(data["rest"][top * 2 + 1, 2], 1, atol=1e-6)
        np.testing.assert_allclose(data["rest"][bottom * 2 + 1, 2], 0, atol=1e-6)

    def test_closed_foot_keeps_downward_mode_and_rejects_lower_rim_control(self):
        p, n, _, _, cloth, body = fixture(two=False)
        marked = np.zeros(len(p), np.uint8)
        marked[np.unique(cloth)] = 1
        data = self.geom.bake_garments(
            p, n, cloth, body, exterior=False, two_ended_rows=marked
        )[0]
        self.assertEqual(data["config"][13, 2], 0)
        marked[np.unique(cloth)] = 2
        with self.assertRaisesRegex(ValueError, "下开口"):
            self.geom.bake_garments(
                p, n, cloth, body, exterior=False, two_ended_rows=marked
            )

    def test_short_wide_pants_follow_opening_axis_and_slide_beyond_original_length(
        self,
    ):
        p, n, f = cylinder(0, 0.18, height=0.25, cap_bottom=False)
        p[:, 2] += 0.8
        # A crotch bridge splits the bottom into two leg openings.
        f = np.r_[f, np.array([[2, 3, 8], [2, 8, 9]], np.uint32)]
        b, bn, bf = cylinder(0, 0.15, height=1.5, cap_top=True)
        data = self.geom.bake_garments(
            np.r_[p, b], np.r_[n, bn], f, bf + len(p), exterior=False
        )[0]
        cfg = data["config"]
        np.testing.assert_allclose(cfg[6, :3], [0, 0, 1], atol=1e-6)
        self.assertEqual(cfg[14, 3], 2)
        self.assertLess(cfg[4, 0], -1)
        self.assertTrue(np.all(data["rest"][::2, 3] == 1))
        self.assertGreater(cfg[7, 3], 0.17)

    def test_shirt_lower_hem_stops_below_sleeves_and_keeps_upper_anchors(self):
        p, n, f = cylinder(0, 0.2, height=0.5, sides=24, rings=11, cap_bottom=False)
        # Two side openings represent the sleeves of this topology fixture.
        centers = p[f].mean(1)
        f = f[
            ~(
                (centers[:, 2] > 0.30)
                & (centers[:, 2] < 0.40)
                & (abs(centers[:, 0]) > 0.18)
            )
        ]
        b, bn, bf = cylinder(0, 0.16, height=0.6, cap_top=True)
        marked = np.r_[np.full(len(p), 2, np.uint8), np.zeros(len(b), np.uint8)]
        data = self.geom.bake_garments(
            np.r_[p, b],
            np.r_[n, bn],
            f,
            bf + len(p),
            exterior=False,
            up_axis="Z",
            two_ended_rows=marked,
        )[0]
        cfg = data["config"]
        self.assertEqual(cfg[14, 3], 1)
        self.assertGreater(cfg[14, 0], 0.4)
        self.assertLess(cfg[14, 0], cfg[14, 2])
        h = data["rest"][1::2, 2]
        self.assertTrue(np.all(data["rest"][::2, 3][h >= cfg[14, 2]] == 0))
        self.assertTrue(np.any(data["rest"][::2, 3][h < cfg[14, 2]] == 1))

    def test_explicit_arrow_controls_axis_and_length_instead_of_auto_direction(self):
        for axis in (0, 2):
            with self.subTest(axis=axis):
                p, n, _, _, cloth, body = fixture(two=False)
                if axis == 0:
                    p, n = p[:, [2, 1, 0]], n[:, [2, 1, 0]]
                rows = np.zeros(len(p), bool)
                rows[np.unique(cloth)] = True
                path = np.array([p[np.unique(cloth)][-12:].mean(0)] * 2)
                path[:, axis] = [1, 0.65]
                data = self.geom.bake_garments(
                    p, n, cloth, body, exterior=False, manual_paths=[(7, rows, path)]
                )[0]
                expected = np.zeros(3)
                expected[axis] = 1
                np.testing.assert_allclose(data["config"][6, :3], expected, atol=1e-6)
                self.assertAlmostEqual(data["config"][4, 0], 0.65, places=4)
                self.assertEqual(data["config"][14, 3], 3)
                self.assertEqual(data["config"][12, 3], 1)
                self.assertEqual(data["path_controls"][7], (1, 1))

    def test_shirt_sleeve_attachments_do_not_pin_the_front_chest(self):
        p, n, f = cylinder(0, 0.2, height=0.5, sides=24, rings=11, cap_bottom=False)
        centers = p[f].mean(1)
        f = f[
            ~(
                (centers[:, 2] > 0.30)
                & (centers[:, 2] < 0.40)
                & (abs(centers[:, 0]) > 0.18)
            )
        ]
        b, bn, bf = cylinder(0, 0.16, height=0.6, cap_top=True)
        rows = np.r_[np.ones(len(p), bool), np.zeros(len(b), bool)]
        data = self.geom.bake_garments(
            np.r_[p, b],
            np.r_[n, bn],
            f,
            bf + len(p),
            exterior=False,
            manual_paths=[(0, rows, np.array([[0, 0, 0], [0, 0, 0.55]]))],
        )[0]
        points = data["rest"][::2, :3]
        chest = (
            (abs(points[:, 0]) < 0.03)
            & (points[:, 1] < -0.18)
            & (points[:, 2] > 0.32)
            & (points[:, 2] < 0.42)
        )
        self.assertTrue(chest.any())
        self.assertTrue(np.all(data["rest"][::2, 3][chest] > 0.5))
        self.assertTrue(np.all(data["rest"][1::2, 0][chest] > 0.95))
        roles = data["attachment_roles"]
        seed_regions = data["body_regions"][data["body_seeds"]]
        self.assertTrue(np.all(seed_regions[roles == 0] == 3))
        side = data["rest"][1::2, 3]
        self.assertTrue(
            np.all(np.isin(seed_regions[(roles == 1) & (side < 0)], [3, 4]))
        )
        self.assertTrue(
            np.all(np.isin(seed_regions[(roles == 1) & (side >= 0)], [3, 5]))
        )

    def test_attachment_influence_is_continuous_and_respects_both_boundaries(self):
        p, _n, f = cylinder(0, 0.2, height=0.5, rings=11, sides=24, cap_bottom=False)
        bottom = np.arange(24)
        top = np.arange(len(p) - 24, len(p))
        influence = self.geom.attachment_influence(p, f, top, [bottom])
        np.testing.assert_array_equal(influence[bottom], 0)
        np.testing.assert_array_equal(influence[top], 1)
        np.testing.assert_allclose(influence, p[:, 2] / 0.5, atol=1e-6)
        with self.assertRaisesRegex(ValueError, "孤立"):
            self.geom.attachment_influence(np.r_[p, [[0, 0, 0.25]]], f, top, [bottom])

    def test_torso_partition_separates_nearby_connected_arm_surface(self):
        p = np.array(
            [
                [-1, 0, 0],
                [1, 0, 0],
                [-1, 0, 2],
                [1, 0, 2],
                [0.8, 0.001, 0],
                [2.8, 0.001, 0],
                [0.8, 0.001, 2],
                [2.8, 0.001, 2],
            ],
            float,
        )
        f = np.array(
            [[0, 1, 2], [1, 3, 2], [4, 5, 6], [5, 7, 6], [1, 4, 3], [4, 6, 3]],
            np.uint32,
        )
        adj = np.full((len(f), 3), 0xFFFFFFFF, np.uint32)
        edges = {}
        for i, face in enumerate(f):
            for k in range(3):
                edges.setdefault(
                    tuple(sorted([int(face[(k + 1) % 3]), int(face[(k + 2) % 3])])), []
                ).append((i, k))
        for values in edges.values():
            if len(values) == 2:
                (a, ak), (b, bk) = values
                adj[a, ak] = b
                adj[b, bk] = a
        mask = self.geom.torso_surface_partition(
            p,
            np.tile([0, 1, 0], (len(p), 1)),
            f,
            adj,
            np.array([[0, 0, 1]]),
            np.array([[1.8, 0.001, 1]]),
        )
        self.assertTrue(np.all(mask[:2]))
        self.assertFalse(np.any(mask[2:4]))

    def test_extending_shirt_arrow_preserves_sleeve_anchors_and_mobility(self):
        p, n, f = cylinder(0, 0.2, height=0.5, sides=24, rings=11, cap_bottom=False)
        centers = p[f].mean(1)
        f = f[
            ~(
                (centers[:, 2] > 0.30)
                & (centers[:, 2] < 0.40)
                & (abs(centers[:, 0]) > 0.18)
            )
        ]
        b, bn, bf = cylinder(0, 0.16, height=0.6, cap_top=True)
        rows = np.r_[np.ones(len(p), bool), np.zeros(len(b), bool)]
        results = [
            self.geom.bake_garments(
                np.r_[p, b],
                np.r_[n, bn],
                f,
                bf + len(p),
                exterior=False,
                manual_paths=[(0, rows, np.array([[0, 0, 0], [0, 0, end]]))],
            )[0]
            for end in (0.25, 0.45, 0.55)
        ]
        np.testing.assert_array_equal(results[0]["rest"], results[1]["rest"])
        np.testing.assert_array_equal(results[0]["rest"], results[2]["rest"])
        self.assertLess(float(results[2]["config"][4, 0]), 0)
        # Sparse region weights identify grab points, not sleeve pin weights.
        rows[: len(p)] = p[:, 2] < 0.05
        sparse = self.geom.bake_garments(
            np.r_[p, b],
            np.r_[n, bn],
            f,
            bf + len(p),
            exterior=False,
            manual_paths=[(0, rows, np.array([[0, 0, 0], [0, 0, 0.45]]))],
        )[0]
        np.testing.assert_array_equal(results[1]["rest"], sparse["rest"])
        self.assertEqual(float(results[0]["config"][14, 1]), 1)
        self.assertAlmostEqual(
            float(results[0]["config"][14, 2]), float(results[1]["config"][14, 2])
        )
        with self.assertRaisesRegex(ValueError, "可折返长度"):
            self.geom.bake_garments(
                np.r_[p, b],
                np.r_[n, bn],
                f,
                bf + len(p),
                exterior=False,
                manual_paths=[(0, rows, np.array([[0, 0, 0], [0, 0, 1.2]]))],
            )
        with self.assertRaisesRegex(ValueError, "端点截面"):
            self.geom.bake_garments(
                np.r_[p, b],
                np.r_[n, bn],
                f,
                bf + len(p),
                exterior=False,
                manual_paths=[(0, rows, np.array([[0, 0, 0], [0, 0, 0.65]]))],
            )

    def test_two_manual_handles_keep_separate_endpoint_limits(self):
        p, n, _, _, cloth, body = fixture(two=False, open_ends=True)
        rows = np.zeros(len(p), bool)
        rows[np.unique(cloth)] = True
        paths = [
            (0, rows, np.array([[-0.12, 0, 1], [-0.12, 0, 0.7]])),
            (1, rows, np.array([[-0.12, 0, 0], [-0.12, 0, 0.4]])),
        ]
        data = self.geom.bake_garments(
            p, n, cloth, body, exterior=False, manual_paths=paths
        )[0]
        self.assertEqual(data["config"][13, 2], 1)
        self.assertEqual(data["path_controls"][0][0], 1)
        self.assertEqual(data["path_controls"][1][0], 2)
        self.assertAlmostEqual(data["path_controls"][0][1], 0.3 / 0.95, places=5)
        self.assertAlmostEqual(data["path_controls"][1][1], 0.4 / 0.95, places=5)

    def test_two_angled_arrows_on_one_opening_share_the_longest_route(self):
        p, n, _, _, cloth, body = fixture(two=False, open_ends=True)
        rows = np.zeros(len(p), bool)
        rows[np.unique(cloth)] = True
        left = np.array([[-0.16, 0, 1], [-0.10, 0, 0.80]])
        right = np.array([[-0.08, 0, 1], [-0.20, 0, 0.60]])
        paths = [(0, rows & (p[:, 0] <= -0.10), left), (1, rows & (p[:, 0] >= -0.14), right)]
        data, = self.geom.bake_garments(p, n, cloth, body, exterior=False, manual_paths=paths)

        for zone, arrow, maximum in ((0, left, 0.5), (1, right, 1.0)):
            self.assertEqual(data["path_controls"][zone][0], 1)
            self.assertAlmostEqual(data["path_controls"][zone][1], maximum)
            delta = arrow[1] - arrow[0]
            np.testing.assert_allclose(data["path_axes"][zone][:3], delta / np.linalg.norm(delta))

    def test_tiny_hole_above_the_waist_does_not_hide_the_real_opening(self):
        cloth, _, cloth_faces = cylinder(0, 0.15, height=0.8, cap_top=False)
        trim, _, trim_faces = cylinder(0, 0.003, height=0.01, cap_top=False)
        trim[:, 2] += 1.2
        points = np.r_[cloth, trim]
        faces = np.r_[cloth_faces, trim_faces + len(cloth)]
        cuff = self.geom._hem(faces, points, np.array([0, 0, 1]), np.array([1, 0, 0]), np.array([0, 1, 0]))
        self.assertTrue(np.all(cuff < len(cloth)))
        np.testing.assert_allclose(points[cuff, 2], 0.8)

    def test_arrow_world_reference_export_and_mirror_match_region_transform(self):
        paths = importlib.import_module("_ssmt_root.blueprint.drag_cloth_path")
        matrix = np.eye(4)
        matrix[:3, 3] = [1, 2, 3]
        endpoint = matrix.copy()
        endpoint[:3, 3] += [0, 0, 0.5]
        settings = types.SimpleNamespace(
            cloth_path_start=types.SimpleNamespace(matrix_world=matrix),
            cloth_path_end=types.SimpleNamespace(matrix_world=endpoint),
        )
        reference = np.eye(4)
        reference[0, 3] = 4
        export = np.diag([2, 2, 2, 1])
        mirror = np.diag([-1, 1, 1, 1])
        actual = paths.exported_path(settings, reference, export, mirror)
        np.testing.assert_allclose(actual, [[6, 4, 6], [6, 4, 7]])
        settings.cloth_path_end = None
        with self.assertRaisesRegex(ValueError, "起点和端点"):
            paths.path_points(settings)

    def test_manual_shirt_track_does_not_adhere_to_nearby_arms(self):
        p, n, f = cylinder(0, 0.2, height=0.5, cap_bottom=False)
        body, bn, bf = cylinder(0, 0.16, height=0.6, cap_top=True)
        pieces = [p, body]
        normals = [n, bn]
        faces = [bf + len(p)]
        for x in (-0.15, 0.15):
            arm, an, af = cylinder(x, 0.025, height=0.3, cap_top=True)
            arm[:, 2] += 0.2
            faces.append(af + sum(len(q) for q in pieces))
            pieces.append(arm)
            normals.append(an)
        positions = np.concatenate(pieces)
        rows = np.zeros(len(positions), bool)
        rows[: len(p)] = True
        data = self.geom.bake_garments(
            positions,
            np.concatenate(normals),
            f,
            np.concatenate(faces),
            exterior=False,
            manual_paths=[(0, rows, np.array([[0, 0, 0], [0, 0, 0.24]]))],
        )[0]
        self.assertEqual(data["config"][12, 3], 0)
        self.assertTrue(np.any(data["body_regions"][: len(bf)] == 3))
        self.assertTrue(np.all(data["body_regions"][len(bf) :] == 0))

    def test_short_sock_body_ownership_covers_outer_and_inner_leg_surfaces(self):
        p, n, _, _, cloth, body = fixture()
        p[np.unique(cloth), 2] *= 0.4
        garments = self.geom.bake_garments(p, n, cloth, body, exterior=False)
        self.assertEqual(len(garments), 2)
        centers = p[body].mean(1)
        for data in garments:
            owner = int(data["config"][12, 3])
            x = centers[data["body_regions"] == owner, 0]
            if owner == 1:
                self.assertLess(float(x.min()), -0.15)
                self.assertGreater(float(x.max()), -0.09)
            else:
                self.assertGreater(float(x.max()), 0.15)
                self.assertLess(float(x.min()), 0.09)
            self.assertTrue(np.all(data["body_regions"][data["body_seeds"]] == owner))

    def test_short_pants_take_left_right_axis_from_body_feet(self):
        p, n, f = cylinder(0, 0.18, height=0.25, cap_bottom=False)
        p[:, 2] += 0.8
        f = np.r_[f, np.array([[2, 3, 8], [2, 8, 9]], np.uint32)]
        pieces = [p]
        normals = [n]
        body = []
        for x in (-0.12, 0.12):
            q, qn, qf = cylinder(x, 0.05, height=1.5, cap_top=True)
            body.append(qf + sum(len(item) for item in pieces))
            pieces.append(q)
            normals.append(qn)
        data = self.geom.bake_garments(
            np.concatenate(pieces),
            np.concatenate(normals),
            f,
            np.concatenate(body),
            exterior=False,
        )[0]
        np.testing.assert_allclose(data["config"][7, :3], [1, 0, 0], atol=1e-6)

    def test_reversed_cloth_normals_do_not_seed_the_far_side_of_a_leg(self):
        p, n, _, _, cloth, body = fixture()
        n[np.unique(cloth)] *= -1
        data = self.geom.bake_garments(p, n, cloth, body, exterior=False)
        for item in data:
            seed_tri = p[body[item["body_seeds"]]]
            point = p[item["representatives"]]
            _, _, distance = self.geom._closest(point, seed_tri)
            self.assertLess(float(np.sqrt(distance).max()), 0.025)


class GarmentExportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_cold_enable_and_reset_do_not_submit_cloth_compute(self):
        spec = importlib.util.spec_from_file_location(
            "cold_cloth_gpu", Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        )
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.force_full_solver = False
                activity = "$ssmtcloth_unit_abc123_activated"
                r.variables[activity] = 0
                before = r.dispatches
                for _ in range(3):
                    np.testing.assert_array_equal(r.frame(), r.native)
                self.assertEqual(r.dispatches, before)
                r.frame(held=True, hit_row=0)
                self.assertGreater(r.dispatches, before)
                r.variables["$ssmtcloth_unit_reset"] += 1
                before = r.dispatches
                np.testing.assert_array_equal(r.frame(), r.native)
                self.assertEqual(r.dispatches, before)
                self.assertEqual(r.variables[activity], 0)
            finally:
                r.close()

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()
        cls.exporter = importlib.import_module("_ssmt_root.blueprint.drag_cloth")

    def prepare(
        self,
        folder,
        collision=False,
        dense=False,
        extra=False,
        budget=2450,
        bake=True,
        prefix=0,
        directions=None,
        axis="Z",
        self_collision=False,
    ):
        p, n, f, names, _, _ = fixture(dense=dense, open_ends=bool(directions))
        if axis == "X":
            p, n = p[:, [2, 1, 0]], n[:, [2, 1, 0]]
        v = np.zeros((len(p), 12 if extra else 10), np.float32)
        v[:, :3] = p
        v[:, 3:6] = n
        v[:, 6] = 1
        v[:, 9] = 1
        if extra:
            v[:, 10:] = np.arange(len(p) * 2, dtype=np.float32).reshape(-1, 2) / 7
        meshes = folder / "Meshes"
        meshes.mkdir(exist_ok=True)
        v.tofile(meshes / "abc123-Position.buf")
        blend = np.zeros((len(p), 8), np.float32)
        blend[:, 0] = 1
        blend.tofile(meshes / "abc123-Blend.buf")
        f.tofile(meshes / "Index.buf")
        lines = ["hash = abc123", "match_first_index = 0", "ib = ResourceIB"]
        offset = 0
        for name in dict.fromkeys(names):
            count = int(np.sum(names == name)) * 3
            lines += [
                f"; [mesh:{name}] [vertex_count:{len(p)}]",
                f"drawindexed = {count}, {offset}, 0",
            ]
            offset += count
        sections = OrderedDict(
            {
                "[Constants]": ["global $active = 0"],
                "[Present]": [],
                "[TextureOverride_abc123_abc123_VertexLimitRaise]": [
                    "hash = abc123",
                    f"override_vertex_count = {len(p)}",
                    "override_byte_stride = 40",
                ],
                "[TextureOverride_abc123_Draw]": lines,
                "[ResourceIB]": [
                    "type = Buffer",
                    "format = R32_UINT",
                    "filename = Meshes/Index.buf",
                ],
                "[Resource_abc123_Position]": [
                    "type = Buffer",
                    "stride = 40",
                    "filename = Meshes/abc123-Position.buf",
                ],
                "[TextureOverride_VB_abc123_Position]": [
                    "vb0 = Resource_abc123_Position",
                    "$zz_ms_occ_7 = $zz_ms_occ_7 + 1",
                    "handling = skip",
                    "if $zz_ms_occ_7 == 1",
                    "    vs-t0 = ResourceZZMergedSkeleton_G3_s1",
                    f"    draw = {len(p)}, 0",
                    "else",
                    "    vs-t0 = ResourceZZMergedSkeleton_G3_s2",
                    f"    draw = {len(p)}, 0",
                    "endif",
                ],
            }
        )
        for slot in (1, 2):
            sections[f"[ResourceZZMergedSkeleton_G3_s{slot}]"] = [
                "type = RWBuffer",
                "format = R32G32B32A32_FLOAT",
                "array = 3",
            ]
            sections[f"[ResourceZZRedirectSO_G3_s{slot}]"] = [
                "type = Buffer",
                "stride = 40",
            ]
        node = _make_node(
            self.mod,
            cloth_slide_enabled=True,
            collision_enabled=collision,
            cloth_proxy_budget=budget,
            cloth_up_axis=axis,
            cloth_self_collision=self_collision,
        )
        if extra:
            definition = self.mod.DEFAULT_VERTEX_STRUCT.replace(
                "};", "    float2 extra;\n};"
            )
            node._get_vertex_struct_definition = lambda: definition
            sections["[Resource_abc123_Position]"][1] = "stride = 48"
            vlr = sections["[TextureOverride_abc123_abc123_VertexLimitRaise]"]
            vlr[:] = [
                line.replace("override_byte_stride = 40", "override_byte_stride = 48")
                for line in vlr
            ]
            for slot in (1, 2):
                sections[f"[ResourceZZRedirectSO_G3_s{slot}]"][1] = "stride = 48"
        if prefix:
            vlr = sections["[TextureOverride_abc123_abc123_VertexLimitRaise]"]
            vlr[:] = [
                (
                    f"override_vertex_count = {len(p) + prefix}"
                    if line.startswith("override_vertex_count")
                    else line
                )
                for line in vlr
            ]
            lines[:] = [
                (
                    line.rsplit(", ", 1)[0] + f", {prefix}"
                    if line.startswith("drawindexed =")
                    else line
                )
                for line in lines
            ]
            deform = sections["[TextureOverride_VB_abc123_Position]"]
            deform[3:3] = ["vs-t0 = ResourceZZMergedSkeleton_G3_s1", "draw = 3, 0"]
            for slot in (1, 2):
                index = deform.index(f"    vs-t0 = ResourceZZMergedSkeleton_G3_s{slot}")
                deform.insert(
                    index + 1, f"    so0 = ref ResourceZZRedirectSO_G3_s{slot}"
                )
        refs = lambda values: [
            types.SimpleNamespace(
                object=types.SimpleNamespace(name=value, name_full=value, type="MESH")
            )
            for value in values
        ]
        settings = types.SimpleNamespace(
            include_objects=refs(["Stocking0", "Stocking1"]),
            collider_objects=refs(["Body"]),
        )
        node._collect_enabled_zone_entries = lambda: [
            (0, types.SimpleNamespace(ssmt_drag_zone=settings))
        ]
        if directions:
            zones = [
                (
                    index,
                    types.SimpleNamespace(
                        ssmt_drag_zone=types.SimpleNamespace(
                            **vars(settings), cloth_direction=direction
                        )
                    ),
                )
                for index, direction in enumerate(directions)
            ]
            node._collect_enabled_zone_entries = lambda: zones
        comp = node._locate_components(sections, ["abc123"])[0]
        cloth_names, body_names = node._cloth_object_names(required=True)
        if bake:
            comp["cloth"] = self.exporter.bake_component(
                node,
                str(folder),
                sections,
                comp,
                "unit",
                cloth_names,
                body_names,
                self.mod._blender_object_key,
            )
        return node, sections, comp

    def test_self_collision_switch_omits_only_its_passes_and_assets(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                node, sections, comp = self.prepare(root, self_collision=enabled)
                node._emit_sections(sections, [comp], "unit")
                prefix = "ResourceSSMTCloth_abc123_unit_G0_"
                for slot in (1, 2):
                    for name in ("Heads", "Links", "Snapshot"):
                        self.assertEqual(
                            f"[{prefix}{name}_s{slot}]" in sections, enabled
                        )
                    key = f"CommandListSSMTClothG0Simulate_s{slot}_abc123_unit"
                    lines = "\n".join(sections["[" + key + "]"])
                    self.assertEqual("SelfHash" in lines, enabled)
                    self.assertIn(f"G0Collision_s{slot}", lines)
                    self.assertIn(f"G0Body_s{slot}", lines)
                asset = root / comp["cloth"]["directory"] / "g0/self.hlsl"
                self.assertEqual(asset.exists(), enabled)

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_stretch_limit_does_not_reverse_an_overextended_edge(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("edge_direction_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.frame()
                data = comp["cloth"]["garments"][0]
                prefix = "ResourceSSMTCloth_abc123_unit_G0_"

                def read(name, width):
                    resource = r.resource(prefix + name)
                    return (
                        r.gpu.read_bytes(resource.pointer, resource.size)
                        .copy()
                        .view("<f4")
                        .reshape(-1, width)
                    )

                cfg = data["config"].copy()
                cfg[3, 0], cfg[3, 1], cfg[5, 3] = 1, 1, 1.8
                cfg[14] = 0
                r.external(prefix + "Config", cfg, 16, 2)
                rest = data["rest"].copy()
                rest[::2, 3] = 0
                rest[[0, 2], 3] = 1
                r.external(prefix + "Rest", rest, 16, 2)
                r.external(
                    prefix + "HemMap",
                    np.full(len(rest) // 2, 0xFFFFFFFF, np.uint32),
                    4,
                    42,
                )
                constraints = data["edges"].copy()
                constraints[:, :2] = 0
                constraints[:, 2:] = [0.001, 0]
                constraints[0, :2] = np.array([0, 1], np.uint32).view("<f4")
                r.external(prefix + "Edges", constraints, 16, 2)
                state = read("State", 4)
                state[0, :2], state[8, 2:], state[23, 3], state[24, 0] = (
                    0.5,
                    [1, 1 / 60],
                    1,
                    0,
                )
                r.external(prefix + "State", state, 16, 2, flags=136)
                r.variables["$ssmtcloth_unit_stretch_compliance"] = 0.000001
                particle = read("Particles_s1", 8)
                particle[0, :3], particle[1, :3] = [-0.003, 0, 0.5], [0.003, 0, 0.5]
                original = particle[1, :3] - particle[0, :3]
                r.external(
                    prefix + "Particles_s1", particle, 32, structured=True, flags=136
                )
                r.execute("CustomShaderSSMTClothG0Edges_s1_abc123_unit")
                solved = read("Particles_s1", 8)
                edge = solved[1, :3] - solved[0, :3]
                self.assertGreater(
                    float(edge @ original), 0, "Stretch projection reversed the edge"
                )
                self.assertLessEqual(float(np.linalg.norm(edge)), 0.0018 + 1e-7)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_intrinsic_release_sleeps_only_after_geometry_validation(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("validated_sleep_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root)
            data = comp["cloth"]["garments"][0]
            data["config"][16] = [1, len(data["surface_mask"]), 0, 2]
            data["config"][10, 3] = 0.15
            folder = root / comp["cloth"]["directory"] / "g0"
            data["config"].tofile(folder / "config.buf")
            text = (folder / "cloth_nodes.hlsl").read_text("utf8")
            constants = text[: text.index("struct Vertex")]
            for name in (
                "CLOTH_REFERENCE_FLOW",
                "CLOTH_ANATOMICAL_SCOPE",
                "CLOTH_CLOSED_DOMAIN",
            ):
                constants = constants.replace(f"#define {name} 0", f"#define {name} 1")
            for name in self.exporter.SHADERS:
                if not (folder / name).exists() and name not in ("parallel_faces.hlsl", "route.hlsl", "precision.hlsl", "retain.hlsl"):
                    continue
                shader = self.exporter._shader(node, name, constants)
                (folder / name).write_text(shader, "utf8")
                if name == "cloth_nodes.hlsl":
                    for phase in (0, 1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):
                        (folder / f"cloth_nodes_p{phase}.hlsl").write_text(
                            f"#define STATIC_PHASE {phase}\n" + shader, "utf8"
                        )
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            prefix = "ResourceSSMTCloth_abc123_unit_G0_"
            try:
                r.prepare_identity()
                r.frame()

                def read(name, width):
                    value = r.resource(prefix + name)
                    return (
                        r.gpu.read_bytes(value.pointer, value.size)
                        .copy()
                        .view("<f4")
                        .reshape(-1, width)
                    )

                state = read("State", 4)
                state[0, :2] = 0.1
                r.external(prefix + "State", state, 16, 2, flags=136)
                for _ in range(12):
                    r.frame()
                    if read("State", 4)[24, 0] > 0.5:
                        break
                state = read("State", 4)
                self.assertGreater(state[24, 0], 0.5)
                self.assertGreater(state[3, 0], 0.5)
                previous = read("Particles_s1", 8)
                r.frame()
                np.testing.assert_array_equal(
                    read("Particles_s1", 8)[:, :3], previous[:, :3]
                )
                self.assertGreater(read("State", 4)[24, 0], 0.5)
                self.assertGreater(read("State", 4)[3, 0], 0.5)
                # An embedded point must invalidate the actual compute result
                # and wake contact solving even with an unchanged cursor.
                particle = read("Particles_s1", 8)
                moving = np.flatnonzero(
                    (data["rest"][::2, 3] > 0.5) & (data["hem_map"] == 0xFFFFFFFF)
                )[0]
                particle[moving, :3] = [-0.12, 0, 0.3]
                r.external(
                    prefix + "Particles_s1", particle, 32, structured=True, flags=136
                )
                state = read("State", 4)
                state[24, 0] = 0
                r.external(prefix + "State", state, 16, 2, flags=136)
                r.execute("CustomShaderSSMTClothG0Validate_s1_abc123_unit")
                state = read("State", 4)
                self.assertGreater(state[220 + moving, 0], 0.5)
                r.execute("CustomShaderSSMTClothG0Quality_s1_abc123_unit")
                self.assertEqual(read("State", 4)[3, 0], 0)
                r.frame()
                self.assertLess(read("State", 4)[24, 0], 0.5)

                # The reduction must include the tail and preserve the other
                # instance's result. Invalid geometry can never go to sleep.
                state = read("State", 4)
                count = (len(state) - 220) // 2
                for slot in (1, 2):
                    for index, column in ((0, 0), (count - 1, 2), (count // 2, 3)):
                        state[24:28, 0] = 0
                        state[3, :2] = [0.25, 0.75]
                        state[220:] = [0, 1, 0, 1]
                        state[220 + (slot - 1) * count + index, column] = (
                            0 if column == 3 else 1
                        )
                        r.external(prefix + "State", state, 16, 2, flags=136)
                        r.execute(
                            f"CustomShaderSSMTClothG0Quality_s{slot}_abc123_unit"
                        )
                        actual = read("State", 4)
                        self.assertEqual(actual[3, slot - 1], 0)
                        self.assertEqual(actual[3, 2 - slot], state[3, 2 - slot])
                for ratio, expected in ((2.8, [3, 8, 0]), (2.1, [1, 2, 1])):
                    state = read("State", 4)
                    state[24:28, 0] = 0
                    state[12] = [1, 2, 1, 0]
                    state[13] = [2, 4, 1, 0]
                    state[220:] = [0, 1, 0, 1]
                    state[220 + count - 1, 1] = ratio
                    r.external(prefix + "State", state, 16, 2, flags=136)
                    r.execute("CustomShaderSSMTClothG0Precision_s1_abc123_unit")
                    actual = read("State", 4)
                    np.testing.assert_array_equal(actual[12, :3], expected)
                    np.testing.assert_array_equal(actual[13], state[13])
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_body_blocker_repels_cloth_without_changing_adhesion_or_weights(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("blocker_vs_adhesion_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.frame()
                data = comp["cloth"]["garments"][0]
                base = "ResourceSSMTCloth_abc123_unit_"
                prefix = base + "G0_"

                def read(name, width):
                    v = r.resource(prefix + name)
                    return (
                        r.gpu.read_bytes(v.pointer, v.size)
                        .view("<f4")
                        .copy()
                        .reshape(-1, width)
                    )

                native = r.native.copy().view("<f4").reshape(-1, 10)
                count = r.resource(base + "BodyOrder").size // 4
                body = np.tile([0, 1, 2], (count, 1)).astype(np.uint32)
                body[count // 2 :] = [3, 4, 5]
                r.external(base + "BodyVerts", body, 4, 42)
                for name in ("SurfaceMask",):
                    r.external(prefix + name, np.ones(count, np.uint32), 4, 42)
                r.external(base + "Exterior_s1", np.ones(count, np.uint32), 4, 42)
                r.external(prefix + "BodyOwnerBounds", np.zeros(1, np.uint32), 4, 42)
                r.external(
                    prefix + "BodySeeds",
                    np.zeros(len(data["representatives"]), np.uint32),
                    4,
                    42,
                )
                r.external(
                    prefix + "BodyAdjacency",
                    np.full(count * 3, 0xFFFFFFFF, np.uint32),
                    4,
                    42,
                )
                blend = (
                    np.fromfile(Path(temp) / comp["cloth"]["paths"]["blend"], "<u4")
                    .reshape(-1, 8)
                    .copy()
                )
                blend[3:6, 4] = 1
                r.external(base + "Blend", blend, 16, 2)
                ids = data["faces"][0].astype(int)
                rest = data["rest"].copy()
                rest[ids * 2, 3] = 1
                hem = data["hem_map"].copy()
                hem[ids] = 0xFFFFFFFF
                r.external(prefix + "Rest", rest, 16, 2)
                r.external(prefix + "HemMap", hem, 4, 42)
                state = read("State", 4)
                state[0, :2], state[8, 2], state[24, 0] = 0.5, 1, 0
                r.external(prefix + "State", state, 16, 2, flags=136)
                cfg = data["config"].copy()
                cfg[12, 3], cfg[14, 0], cfg[14, 3], cfg[15, 2], cfg[15, 3] = (
                    0,
                    1,
                    3,
                    1,
                    1,
                )
                r.external(prefix + "Config", cfg, 16, 2)
                r.variables["$ssmtcloth_unit_surface_gap_mm"] = 3
                particle = read("Particles_s1", 8)
                slot = int(np.flatnonzero(data["face_colors"] == 0)[0])
                color = int(
                    np.searchsorted(data["face_offsets"], slot, side="right") - 1
                )
                for own in (3, 4, 5):
                    roles = np.zeros(len(data["representatives"]), np.uint32)
                    roles[ids] = int(own != 3)
                    r.external(prefix + "AttachmentRoles", roles, 4, 42)
                    owners = np.full(count, own, np.uint32)
                    owners[count // 2 :] = 4 if own == 3 else 3
                    r.external(prefix + "BodyOwners", owners, 4, 42)
                    for floor, initial, tags in (
                        (0, 0.009, ("Collision", f"Face{color}")),
                        (-1, -0.02, ("Collision",)),
                    ):
                        for k, z in enumerate((floor, 0.01)):
                            native[k * 3 : k * 3 + 3, :3] = [
                                [-1, -1, z],
                                [1, -1, z],
                                [0, 1, z],
                            ]
                            native[k * 3 : k * 3 + 3, 3:6] = [0, 0, 1]
                        r.external(base + "NativeInput", native, 40, structured=True)
                        bounds = np.empty(
                            (r.resource(base + "Bounds_s1").size // 16, 4), np.float32
                        )
                        bounds[::2], bounds[1::2] = [-1, -1, floor, 0], [1, 1, 0.01, 0]
                        r.external(base + "Bounds_s1", bounds, 16, 2)
                        points = np.array(
                            [
                                [-0.1, -0.1, initial],
                                [0.1, -0.1, initial],
                                [0, 0.1, initial],
                            ],
                            np.float32,
                        )
                        for tag in tags:
                            with self.subTest(owner=own, initial_z=initial, shader=tag):
                                particle[ids, :3] = particle[ids, 4:7] = points
                                r.external(
                                    prefix + "Particles_s1",
                                    particle,
                                    32,
                                    structured=True,
                                    flags=136,
                                )
                                contacts = np.zeros(
                                    (len(data["representatives"]), 3, 4), np.float32
                                )
                                contacts[:, 1] = [0, 0, 1, 0.25]
                                contacts[:, 2] = [0, 0.25, 0.5, 1]
                                r.external(
                                    prefix + "NodeContacts_s1",
                                    contacts,
                                    16,
                                    2,
                                    flags=136,
                                )
                                r.execute(
                                    f"CustomShaderSSMTClothG0{tag}_s1_abc123_unit"
                                )
                                self.assertGreaterEqual(
                                    float(read("Particles_s1", 8)[ids, 2].min()),
                                    0.013 - 1e-6,
                                )
                                r.execute(
                                    "CustomShaderSSMTClothG0Weights_s1_abc123_unit"
                                )
                                after = read("NodeContacts_s1", 4).reshape(-1, 3, 4)
                                np.testing.assert_array_equal(
                                    after[ids, 2, 0].copy().view("<u4"), 0
                                )
                                indices = (
                                    read("Weights_s1", 8)[ids, 4:].copy().view("<u4")
                                )
                                np.testing.assert_array_equal(indices, 0)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_wrinkle_projection_does_not_cross_an_opposing_body_surface(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("fold_blocker_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.frame()
                data = comp["cloth"]["garments"][0]
                base = "ResourceSSMTCloth_abc123_unit_"
                prefix = base + "G0_"
                native = r.native.copy().view("<f4").reshape(-1, 10)
                bound = native.copy()
                bound[:, 3:6] = [1, 0, 0]
                r.external(base + "BindInput", bound, 40, structured=True)
                for level, z in enumerate((0, 0.01)):
                    native[level * 3 : level * 3 + 3, :3] = [
                        [-1, -1, z],
                        [1, -1, z],
                        [0, 1, z],
                    ]
                    native[level * 3 : level * 3 + 3, 3:6] = [0, 0, 1 - level * 2]
                r.external(base + "NativeInput", native, 40, structured=True)
                count = r.resource(base + "BodyOrder").size // 4
                triangles = np.tile([0, 1, 2], (count, 1)).astype(np.uint32)
                triangles[count // 2 :] = [3, 5, 4]
                owners = np.ones(count, np.uint32)
                r.external(base + "BodyVerts", triangles, 4, 42)
                r.external(prefix + "BodyOwners", owners, 4, 42)
                r.external(prefix + "BodyOwnerBounds", np.zeros(1, np.uint32), 4, 42)
                r.external(prefix + "SurfaceMask", np.ones(count, np.uint32), 4, 42)
                r.external(base + "Exterior_s1", np.ones(count, np.uint32), 4, 42)
                bounds = np.empty(
                    (r.resource(base + "Bounds_s1").size // 16, 4), np.float32
                )
                bounds[::2] = [-1, -1, 0, 0]
                bounds[1::2] = [1, 1, 0.01, 0]
                r.external(base + "Bounds_s1", bounds, 16, 2)
                cfg = data["config"].copy()
                cfg[6, 3] = 1
                cfg[7, :3], cfg[9, :3] = [1, 0, 0], [0, 1, 0]
                cfg[12, 3], cfg[15, 2], cfg[15, 3] = 1, 0, 1
                r.external(prefix + "Config", cfg, 16, 2)
                i = int(np.flatnonzero(data["rest"][::2, 3] > 0.5)[10])
                rest = data["rest"].copy()
                rest[i * 2 + 1, 2:] = [0.6, 0.1]
                r.external(prefix + "Rest", rest, 16, 2)
                hm = data["hem_map"].copy()
                hm[i] = 0xFFFFFFFF
                r.external(prefix + "HemMap", hm, 4, 42)

                def read(name, width):
                    v = r.resource(prefix + name)
                    return (
                        r.gpu.read_bytes(v.pointer, v.size)
                        .view("<f4")
                        .copy()
                        .reshape(-1, width)
                    )

                s = read("State", 4)
                s[0, :2], s[8, :3], s[24, 0] = 0.5, [0.5, 0.5, 1], 0
                s[4], s[5] = [0, 0, 0, 1], [0, 0, 1, 0]
                r.external(prefix + "State", s, 16, 2, flags=136)
                particles = read("Particles_s1", 8)
                particles[i, :3] = particles[i, 4:7] = [0, 0, 0.003]
                r.external(
                    prefix + "Particles_s1", particles, 32, structured=True, flags=136
                )
                r.variables["$ssmtcloth_unit_surface_gap_mm"] = 3
                r.variables["$ssmtcloth_unit_fold_height"] = 0.025
                r.execute("CustomShaderSSMTClothG0Body_s1_abc123_unit")
                z = float(read("Particles_s1", 8)[i, 2])
                self.assertGreaterEqual(z, 0.003 - 1e-6)
                self.assertLess(z, 0.01 - 1e-4)
                # The torso chart keeps the folded cloth's existing offset;
                # this target must obey the same opposing-surface limit.
                cfg[12, 3] = 3
                owners[:] = 3
                r.external(prefix + "Config", cfg, 16, 2)
                r.external(prefix + "BodyOwners", owners, 4, 42)
                particles[i, :3] = particles[i, 4:7] = [0, 0, 0.024]
                r.external(
                    prefix + "Particles_s1", particles, 32, structured=True, flags=136
                )
                r.execute("CustomShaderSSMTClothG0Body_s1_abc123_unit")
                z = float(read("Particles_s1", 8)[i, 2])
                self.assertGreaterEqual(z, 0.003 - 1e-6)
                self.assertLess(z, 0.01 - 1e-4)
            finally:
                r.close()

    def test_disjoint_zone_clothes_share_only_the_same_body_acceleration(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp), bake=False)
            original = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            settings = []
            for index, ref in enumerate(original.include_objects):
                settings.append(
                    (
                        index,
                        types.SimpleNamespace(
                            ssmt_drag_zone=types.SimpleNamespace(
                                include_objects=[ref],
                                collider_objects=original.collider_objects,
                                cloth_direction="NEGATIVE",
                            )
                        ),
                    )
                )
            node._collect_enabled_zone_entries = lambda: settings
            cloth, body = node._cloth_object_names(required=True)
            comp["cloth"] = self.exporter.bake_component(
                node,
                temp,
                sections,
                comp,
                "unit",
                cloth,
                body,
                self.mod._blender_object_key,
            )
            meta = comp["cloth"]
            self.assertEqual(len(meta["bodies"]), 1)
            self.assertEqual(len(meta["garments"]), 2)
            np.testing.assert_array_equal(
                meta["garments"][0]["control_zones"][:, 0], [1, 0]
            )
            np.testing.assert_array_equal(
                meta["garments"][1]["control_zones"][:, 0], [0, 1]
            )
            self.assertFalse(
                np.any(
                    (meta["garments"][0]["map"] != 0xFFFFFFFF)
                    & (meta["garments"][1]["map"] != 0xFFFFFFFF)
                )
            )

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_generated_shirt_moves_chest_above_sleeves_without_moving_attachments(self):
        p, n, f = cylinder(0, 0.2, height=0.5, sides=24, rings=11, cap_bottom=False)
        centers = p[f].mean(1)
        f = f[
            ~(
                (centers[:, 2] > 0.30)
                & (centers[:, 2] < 0.40)
                & (abs(centers[:, 0]) > 0.18)
            )
        ]
        b, bn, bf = cylinder(0, 0.16, height=0.6, cap_top=True)
        values = (
            np.r_[p, b],
            np.r_[n, bn],
            np.r_[f, bf + len(p)],
            np.array(["Stocking0"] * len(f) + ["Body"] * len(bf), object),
            f,
            bf + len(p),
        )
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("shirt_chest_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(__name__ + ".fixture", return_value=values),
        ):
            node, sections, comp = self.prepare(Path(temp), bake=False)
            settings = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            a = np.eye(4)
            z = a.copy()
            z[2, 3] = 0.55
            settings.cloth_path_start = types.SimpleNamespace(matrix_world=a)
            settings.cloth_path_end = types.SimpleNamespace(matrix_world=z)
            node._get_reference_matrix_inv = lambda comp: None
            node._get_export_space_matrix = lambda: None
            node._get_non_mirror_mirror = lambda: None
            clothes, body = node._cloth_object_names(required=True)
            comp["cloth"] = self.exporter.bake_component(
                node,
                temp,
                sections,
                comp,
                "unit",
                clothes,
                body,
                self.mod._blender_object_key,
            )
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.frame()
                r.frame()
                data = comp["cloth"]["garments"][0]
                mask = data["map"] != 0xFFFFFFFF
                prefix = "ResourceSSMTCloth_abc123_unit_G0_"
                for step in range(1, 21):
                    resource = r.resource(prefix + "State")
                    state = (
                        r.gpu.read_bytes(resource.pointer, resource.size)
                        .copy()
                        .view("<f4")
                        .reshape(-1, 4)
                    )
                    state[0, :2] = step / 20
                    r.external(prefix + "State", state, 16, 2, flags=136)
                    out = r.frame()
                native = r.native.copy().view("<f4").reshape(-1, 10)
                result = out.copy().view("<f4").reshape(-1, 10)
                chest = (
                    mask
                    & (abs(native[:, 0]) < 0.03)
                    & (native[:, 1] < -0.18)
                    & (native[:, 2] > 0.32)
                    & (native[:, 2] < 0.42)
                )
                self.assertTrue(chest.any())
                self.assertGreater(float(result[chest, 2].min()), 0.40)
                owned = np.flatnonzero(mask)
                fixed = owned[data["rest"][data["map"][owned] * 2, 3] < 0.5]
                self.assertGreater(len(fixed), 0)
                np.testing.assert_array_equal(result[fixed, :3], native[fixed, :3])
                np.testing.assert_array_equal(out[~mask], r.native[~mask])
                r.variables[r.prefix + "reset"] += 1
                np.testing.assert_array_equal(r.frame(), r.native)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_manual_arrow_range_and_mismatched_zone_do_not_move_other_clothes(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("manual_path_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp), bake=False)
            old = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            zones = []
            for i, ref in enumerate(old.include_objects):
                a = np.eye(4)
                a[:3, 3] = [-0.12 + i * 0.24, 0, 1]
                b = a.copy()
                b[2, 3] = 0.65
                settings = types.SimpleNamespace(
                    include_objects=[ref],
                    collider_objects=old.collider_objects,
                    cloth_direction="AUTO",
                    cloth_path_start=types.SimpleNamespace(matrix_world=a),
                    cloth_path_end=types.SimpleNamespace(matrix_world=b),
                )
                zones.append((i, types.SimpleNamespace(ssmt_drag_zone=settings)))
            node._collect_enabled_zone_entries = lambda: zones
            node._get_reference_matrix_inv = lambda comp: None
            node._get_export_space_matrix = lambda: None
            node._get_non_mirror_mirror = lambda: None
            clothes, body = node._cloth_object_names(required=True)
            comp["cloth"] = self.exporter.bake_component(
                node,
                temp,
                sections,
                comp,
                "unit",
                clothes,
                body,
                self.mod._blender_object_key,
            )
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                data = comp["cloth"]["garments"][0]
                row = int(data["representatives"][20])
                runner.frame(hit_row=row)
                runner.frame(600, True, row, zone=1)
                for i in range(1, 4):
                    out = runner.frame(600 - i * 10, True, row, zone=1)
                np.testing.assert_array_equal(out, runner.native)
                runner.frame(600, False, row, zone=0)
                runner.frame(600, True, row, zone=0)
                for i in range(1, 70):
                    out = runner.frame(600 - i * 8, True, row, zone=0)
                mask = data["map"] != 0xFFFFFFFF
                np.testing.assert_array_equal(out[~mask], runner.native[~mask])
                resource = runner.resource(
                    "ResourceSSMTCloth_abc123_unit_G0_Particles_s1"
                )
                particles = (
                    runner.gpu.read_bytes(resource.pointer, resource.size)
                    .view("<f4")
                    .reshape(-1, 8)
                )
                cuff = data["hem_params"][:, 3].astype(int)
                self.assertAlmostEqual(
                    float(particles[cuff, 2].mean()), 0.65, delta=0.015
                )
            finally:
                runner.close()

    def test_separate_zone_colliders_emit_separate_body_views_and_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp), bake=False)
            read = node._read_component_triangles

            def topology(*args):
                faces, names = read(*args)
                p = node._read_position_buf(temp, sections, comp, comp["vertex_count"])
                names = np.array(names, object)
                body = names == "Body"
                names[body] = np.where(
                    p[faces[body], 0].mean(1) < 0, "BodyLeft", "BodyRight"
                )
                return faces, names

            node._read_component_triangles = topology
            settings = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            entries = []
            for index, ref in enumerate(settings.include_objects):
                collider = types.SimpleNamespace(
                    object=types.SimpleNamespace(
                        name="BodyLeft" if index == 0 else "BodyRight", type="MESH"
                    )
                )
                entries.append(
                    (
                        index,
                        types.SimpleNamespace(
                            ssmt_drag_zone=types.SimpleNamespace(
                                include_objects=[ref],
                                collider_objects=[collider],
                                cloth_direction="NEGATIVE",
                            )
                        ),
                    )
                )
            node._collect_enabled_zone_entries = lambda: entries
            cloth, body = node._cloth_object_names(required=True)
            comp["cloth"] = self.exporter.bake_component(
                node,
                temp,
                sections,
                comp,
                "unit",
                cloth,
                body,
                self.mod._blender_object_key,
            )
            self.assertEqual(len(comp["cloth"]["bodies"]), 2)
            node._emit_sections(sections, [comp], "unit")
            second = sections["[CustomShaderSSMTClothG1Body_s1_abc123_unit]"]
            self.assertIn(
                "cs-t87 = ref ResourceSSMTCloth_abc123_unit_B1_BodyVerts", second
            )
            self.assertIn(
                "cs-t91 = ref ResourceSSMTCloth_abc123_unit_B1_Bounds_s1", second
            )

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_short_pants_publish_long_travel_to_render_vertices(self):
        p, n, f = cylinder(0, 0.18, height=0.25, cap_bottom=False)
        p[:, 2] += 0.8
        f = np.r_[f, np.array([[2, 3, 8], [2, 8, 9]], np.uint32)]
        b, bn, bf = cylinder(0, 0.15, height=1.5, cap_top=True)
        bf += len(p)
        values = (
            np.r_[p, b],
            np.r_[n, bn],
            np.r_[f, bf],
            np.array(["Stocking0"] * len(f) + ["Body"] * len(bf), object),
            f,
            bf,
        )
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("short_pants_publish_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(__name__ + ".fixture", return_value=values),
        ):
            node, sections, comp = self.prepare(Path(temp), directions=("NEGATIVE",))
            node._emit_sections(sections, [comp], "unit")
            data = comp["cloth"]["garments"][0]
            self.assertEqual(data["config"][14, 3], 2)
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                row = int(data["representatives"][20])
                runner.frame(hit_row=row)
                runner.frame(600, True, row)
                for i in range(1, 70):
                    out = runner.frame(600 - i * 8, True, row)
                mask = data["map"] != 0xFFFFFFFF
                np.testing.assert_array_equal(out[~mask], runner.native[~mask])
                xyz = out.copy().view("<f4").reshape(-1, 10)[:, :3]
                self.assertLess(float(xyz[mask, 2].mean()), 0.5)
                self.assertGreater(
                    int(np.sum(np.any(out[mask] != runner.native[mask], axis=1))),
                    int(mask.sum() * 0.9),
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_oversized_native_so_preserves_visible_rows_before_any_drag(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("native_capacity_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            # Isolate the real draw-entry copy/publication, before cloth moves.
            bind = "[" + comp["cloth_bind"] + "]"
            sections[bind] = [
                line
                for line in sections[bind]
                if not line.strip().startswith("run = ") or "ClothSeed_s" in line
            ]
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                for slot in (1, 2):
                    capacity = np.r_[
                        runner.native, np.full((17 * slot, 40), 0xCD, np.uint8)
                    ]
                    runner.external(
                        f"ResourceZZRedirectSO_G3_s{slot}", capacity, 40, flags=25
                    )
                runner.capture_skin_inputs()
                for slot in (1, 2):
                    np.testing.assert_array_equal(
                        runner.frame(slot=slot), runner.native
                    )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_uncaptured_skin_source_keeps_original_draw_and_skips_publication(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("missing_skin_source_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                for slot in (1, 2):
                    runner.variables[runner.prefix + f"abc123_source_valid_s{slot}"] = 0
                    del runner.resources[
                        f"ResourceSSMTCloth_abc123_unit_SkinSource_s{slot}"
                    ]
                np.testing.assert_array_equal(runner.frame(), runner.native)
                self.assertEqual(runner.dispatches, 0)
                runner.execute(comp["cloth_publish"])
                output = runner.resources["vb0"]
                np.testing.assert_array_equal(
                    runner.gpu.read_bytes(output.pointer, output.size).reshape(-1, 40),
                    runner.native,
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_structured_skin_source_is_converted_to_plain_draw_output(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location(
            "structured_skin_source_gpu", path
        )
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            bind = "[" + comp["cloth_bind"] + "]"
            sections[bind] = [
                line
                for line in sections[bind]
                if not line.strip().startswith("run = ") or "ClothSeed_s" in line
            ]
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                for slot in (1, 2):
                    runner.external(
                        f"ResourceZZRedirectSO_G3_s{slot}",
                        runner.native,
                        40,
                        structured=True,
                        flags=136,
                    )
                runner.capture_skin_inputs()
                np.testing.assert_array_equal(runner.frame(), runner.native)
                self.assertFalse(
                    runner.resources[
                        "ResourceSSMTCloth_abc123_unit_Output_s1"
                    ].structured
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_body_level_intersection_through_vertex_retains_the_entire_segment(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("body_level_vertex_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _node, sections, comp = self.prepare(root)
            runner = gpu.GeneratedCloth(root, sections, comp)
            try:
                vertices = np.zeros((3, 10), np.float32)
                vertices[:, :3] = [[0, 0, 1], [-1, 1, 0], [1, 1, 2]]
                vertices[:, 4] = 1
                source = runner.external("TestBody", vertices, 40, structured=True)
                cfg = np.zeros((16, 4), np.float32)
                cfg[6] = [0, 0, 1, 1]
                cfg[14, 3] = 3
                cfg[15, 3] = 1
                bindings = {
                    24: source,
                    111: source,
                    82: runner.external("TestConfig", cfg, 16, 2),
                }
                for slot, name, values in (
                    (87, "TestIDs", [0, 1, 2]),
                    (89, "TestOrder", [0]),
                    (108, "TestSurface", [1]),
                    (109, "TestOwners", [3]),
                    (106, "TestExterior", [1]),
                    (112, "TestContact", [0]),
                ):
                    bindings[slot] = runner.external(
                        name, np.array(values, np.uint32), 4, 42
                    )
                for slot, resource in bindings.items():
                    runner.gpu.bind(slot, runner.view(resource, False))
                output = runner.external(
                    "TestResult", np.zeros((2, 4), np.float32), 16, 2, flags=136
                )
                runner.gpu.bind(0, runner.view(output, True), True)
                code = (
                    (self.exporter.TOOLSET / "common.hlsli").read_text("utf8")
                    + """\nRWBuffer<float4> Result : register(u0);
[numthreads(1,1,1)]void main(){float3 q,b,n;bool hit=BodyLevelPoint(0,3,float3(0,.75,1),1,float3(0,1,0),q,b,n);Result[0]=float4(q,hit?1:0);Result[1]=float4(b,0);}
"""
                )
                shader_path = root / "level_vertex.hlsl"
                shader_path.write_text(code, "utf8")
                runner.gpu.dispatch(runner.gpu.shader(shader_path), 1)
                result = (
                    runner.gpu.read_bytes(output.pointer, output.size)
                    .view("<f4")
                    .reshape(-1, 4)
                )
                np.testing.assert_allclose(result[0], [0, 0.75, 1, 1], atol=1e-6)
                np.testing.assert_allclose(
                    result[1, :3], [0.25, 0.375, 0.375], atol=1e-6
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_face_centroid_respects_clearance_before_triangles_intersect(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("face_centre_gap_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.frame()
                r.variables["$ssmtcloth_unit_surface_gap_mm"] = 3
                data = comp["cloth"]["garments"][0]
                base = "ResourceSSMTCloth_abc123_unit_"
                prefix = base + "G0_"
                face = 0
                vertices = data["faces"][face].astype(int)
                native = r.native.copy().view("<f4").reshape(-1, 10)
                native[:3, :3] = [[-0.2, -0.2, 0], [0.2, -0.2, 0], [0, 0.3, 0]]
                native[:3, 3:6] = [0, 0, 1]
                r.external(base + "NativeInput", native, 40, structured=True)
                resource = r.resource(base + "BodyOrder")
                count = resource.size // 4
                r.external(
                    base + "BodyVerts",
                    np.tile([0, 1, 2], (count, 1)).astype(np.uint32),
                    4,
                    42,
                )
                rest = data["rest"].copy()
                rest[vertices * 2, 3] = 1
                hm = data["hem_map"].copy()
                hm[vertices] = 0xFFFFFFFF
                r.external(prefix + "Rest", rest, 16, 2)
                r.external(prefix + "HemMap", hm, 4, 42)
                resource = r.resource(prefix + "State")
                s = (
                    r.gpu.read_bytes(resource.pointer, resource.size)
                    .copy()
                    .view("<f4")
                    .reshape(-1, 4)
                )
                s[0, :2] = 0.5
                s[24, 0] = 0
                r.external(prefix + "State", s, 16, 2, flags=136)
                resource = r.resource(prefix + "Particles_s1")
                p = (
                    r.gpu.read_bytes(resource.pointer, resource.size)
                    .copy()
                    .view("<f4")
                    .reshape(-1, 8)
                )
                p[vertices, :3] = [
                    [-0.1, -0.1, 0.001],
                    [0.1, -0.1, 0.001],
                    [0, 0.2, 0.001],
                ]
                p[vertices, 4:7] = p[vertices, :3]
                r.external(prefix + "Particles_s1", p, 32, structured=True, flags=136)
                position = int(np.flatnonzero(data["face_colors"] == face)[0])
                color = int(
                    np.searchsorted(data["face_offsets"], position, side="right") - 1
                )
                r.execute(f"CustomShaderSSMTClothG0Face{color}_s1_abc123_unit")
                resource = r.resource(prefix + "Particles_s1")
                after = (
                    r.gpu.read_bytes(resource.pointer, resource.size)
                    .view("<f4")
                    .reshape(-1, 8)[vertices, :3]
                )
                self.assertGreaterEqual(float(after[:, 2].mean()), 0.003 - 1e-6)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_body_branch_pruning_preserves_the_generated_simulation(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("branch_pruning_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            outputs = []
            for pruned in (False, True):
                r = gpu.GeneratedCloth(temp, sections, comp)
                try:
                    r.prepare_identity()
                    r.variables["$ssmtcloth_unit_sleep_enabled"] = 0
                    if not pruned:
                        for g in range(len(comp["cloth"]["garments"])):
                            r.external(
                                f"ResourceSSMTCloth_abc123_unit_G{g}_BodyOwnerBounds",
                                np.zeros(1, np.uint32),
                                4,
                                42,
                            )
                    data = comp["cloth"]["garments"][0]
                    row = int(data["representatives"][20])
                    r.frame(hit_row=row)
                    r.frame(600, True, row)
                    for step in range(1, 8):
                        out = r.frame(600 - step * 5, True, row)
                    outputs.append(out.copy())
                finally:
                    r.close()
            np.testing.assert_array_equal(outputs[0], outputs[1])

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_surface_gap_in_millimetres_changes_the_actual_cuff_clearance(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("cloth_gap_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.frame()
                r.frame()
                prefix = "ResourceSSMTCloth_abc123_unit_G0_"
                data = comp["cloth"]["garments"][0]
                v = r.resource(prefix + "State")
                s = (
                    r.gpu.read_bytes(v.pointer, v.size)
                    .copy()
                    .view("<f4")
                    .reshape(-1, 4)
                )
                s[0, :2] = 0.4
                r.external(prefix + "State", s, 16, 2, flags=136)
                hem = data["hem_params"][:, 3].astype(int)
                center = float(data["config"][8, 0])

                def radius():
                    p = r.resource(prefix + "Particles_s1")
                    p = (
                        r.gpu.read_bytes(p.pointer, p.size)
                        .view("<f4")
                        .reshape(-1, 8)[hem, :3]
                    )
                    return float(
                        np.mean(np.linalg.norm(p[:, :2] - [center, 0], axis=1))
                    )

                for _ in range(12):
                    r.frame()
                small = radius()
                r.variables["$ssmtcloth_unit_surface_gap_mm"] = 6
                for _ in range(8):
                    r.frame()
                large = radius()
                self.assertGreater(large - small, 0.003)
                v = r.resource(prefix + "State")
                s = r.gpu.read_bytes(v.pointer, v.size).view("<f4").reshape(-1, 4)
                self.assertAlmostEqual(float(s[28, 3]), 0.006, places=6)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_settled_cloth_sleeps_follows_rigid_motion_and_wakes_for_shape_or_input(
        self,
    ):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("sleep_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.variables["$ssmtcloth_unit_sleep_recheck_s"] = 10
                r.frame()
                r.frame()
                prefix = "ResourceSSMTCloth_abc123_unit_G0_"
                data = comp["cloth"]["garments"][0]

                def state():
                    v = r.resource(prefix + "State")
                    return (
                        r.gpu.read_bytes(v.pointer, v.size)
                        .copy()
                        .view("<f4")
                        .reshape(-1, 4)
                    )

                s = state()
                s[0, :2] = 0.45
                r.external(prefix + "State", s, 16, 2, flags=136)
                sleeping = 0
                for _ in range(40):
                    before = r.frame()
                    s = state()
                    sleeping += int(s[24, 0] > 0.5)
                self.assertGreater(sleeping, 10)
                for _ in range(12):
                    if state()[24, 0] > 0.5:
                        break
                    before = r.frame()
                s = state()
                self.assertGreater(s[24, 0], 0.5)
                full = s[25, 2]
                original = r.native.copy().view("<f4").reshape(-1, 10)
                moved = original.copy()
                moved[:, 0] += 0.1
                r.native = moved.view(np.uint8).reshape(-1, 40).copy()
                for slot in (1, 2):
                    r.external(
                        "ResourceZZRedirectSO_G3_s" + str(slot), r.native, 40, flags=25
                    )
                    matrix = np.array(
                        [[1, 0, 0, 0.1], [0, 1, 0, 0], [0, 0, 1, 0]], np.float32
                    )
                    r.external(
                        "ResourceZZMergedSkeleton_G3_s" + str(slot), matrix, 16, 2
                    )
                r.capture_skin_inputs()
                out = r.frame()
                s = state()
                self.assertGreater(s[24, 0], 0.5)
                self.assertEqual(s[25, 2], full)
                p = out.copy().view("<f4").reshape(-1, 10)
                old = before.copy().view("<f4").reshape(-1, 10)
                mask = data["map"] != 0xFFFFFFFF
                np.testing.assert_allclose(
                    p[mask, :3], old[mask, :3] + [0.1, 0, 0], atol=1e-5
                )
                faces = np.fromfile(
                    Path(temp) / comp["cloth"]["paths"]["body_vertices"], "<u4"
                ).reshape(-1, 3)
                bodyrows = np.unique(faces[data["body_regions"] == 1])
                moved[bodyrows, 1] += 0.025
                morph_bind = original.copy()
                morph_bind[bodyrows, 1] += 0.025
                r.external(comp["cloth"]["dynamic_bind"], morph_bind, 40, flags=1)
                r.native = moved.view(np.uint8).reshape(-1, 40).copy()
                for slot in (1, 2):
                    r.external(
                        "ResourceZZRedirectSO_G3_s" + str(slot), r.native, 40, flags=25
                    )
                r.capture_skin_inputs()
                r.frame()
                s = state()
                self.assertEqual(s[23, 3], 1)
                self.assertEqual(s[24, 0], 0, msg=str(s[19]))
                self.assertGreater(s[25, 2], full)
                for _ in range(25):
                    r.frame()
                r.variables["$ssmtcloth_unit_surface_gap_mm"] = 4
                r.frame()
                self.assertEqual(state()[24, 0], 0)
                hit = int(data["representatives"][20])
                r.frame(600, True, hit)
                self.assertEqual(state()[24, 0], 0)
                r.variables[r.prefix + "reset"] += 1
                np.testing.assert_array_equal(r.frame(), r.native)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_surface_weights_keep_the_material_anchor_after_blocker_pushes_a_point(
        self,
    ):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("locked_contact_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                r.frame()
                data = comp["cloth"]["garments"][0]
                base = "ResourceSSMTCloth_abc123_unit_"
                prefix = base + "G0_"
                cfg = data["config"].copy()
                cfg[12, 3] = 1
                cfg[15, 2:] = 1
                r.external(prefix + "Config", cfg, 16, 2)
                native = r.native.copy().view("<f4").reshape(-1, 10)
                native[:4, :3] = [[-1, 0, 0], [0, 0, 0], [0, 1, 0], [1, 0, 0]]
                native[:4, 3:6] = [0, 0, 1]
                r.external(base + "NativeInput", native, 40, structured=True)
                order = r.resource(base + "BodyOrder")
                count = order.size // 4
                body = np.tile([1, 3, 2], (count, 1)).astype(np.uint32)
                body[0] = [0, 1, 2]
                r.external(base + "BodyVerts", body, 4, 42)
                r.external(prefix + "BodyOwners", np.ones(count, np.uint32), 4, 42)
                r.external(prefix + "SurfaceMask", np.ones(count, np.uint32), 4, 42)
                adj = np.full((count, 3), 0xFFFFFFFF, np.uint32)
                adj[0, 0] = 1
                adj[1, 1] = 0
                r.external(prefix + "BodyAdjacency", adj, 4, 42)
                particle = int(np.flatnonzero(data["rest"][::2, 3] > 0.5)[10])
                pcount = len(data["representatives"])
                con = np.zeros((pcount, 3, 4), np.float32)
                con[:, 0, :3] = [-0.5, 0.25, 0]
                con[:, 1] = [0, 0, 1, 0.5]
                con[:, 2] = [0, 0.25, 0.25, 1]
                r.external(prefix + "NodeContacts_s1", con, 16, 2, flags=136)
                resource = r.resource(prefix + "Particles_s1")
                points = (
                    r.gpu.read_bytes(resource.pointer, resource.size)
                    .copy()
                    .view("<f4")
                    .reshape(-1, 8)
                )
                points[particle, :3] = [0.5, 0.25, 0.01]
                r.external(
                    prefix + "Particles_s1", points, 32, structured=True, flags=136
                )
                resource = r.resource(prefix + "State")
                state = (
                    r.gpu.read_bytes(resource.pointer, resource.size)
                    .copy()
                    .view("<f4")
                    .reshape(-1, 4)
                )
                state[0, :2] = 0.5
                r.external(prefix + "State", state, 16, 2, flags=136)
                r.execute("CustomShaderSSMTClothG0Weights_s1_abc123_unit")
                resource = r.resource(prefix + "NodeContacts_s1")
                after = (
                    r.gpu.read_bytes(resource.pointer, resource.size)
                    .view("<f4")
                    .reshape(-1, 3, 4)
                )
                self.assertEqual(int(after[particle, 2, 0:1].copy().view("<u4")[0]), 0)
                np.testing.assert_allclose(
                    after[particle, 0, :3], [-0.5, 0.25, 0], atol=1e-6
                )
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_short_freeze_keeps_two_leg_frames_instead_of_inverting_their_average(self):
        spec = importlib.util.spec_from_file_location("leg_frames_gpu", Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py")
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root)
            data = comp["cloth"]["garments"][0]
            data["config"][14, 3] = 2
            directory = root / comp["cloth"]["directory"] / "g0"
            data["config"].astype("<f4").tofile(directory / "config.buf")
            source = (directory / "cloth_nodes.hlsl").read_text("utf8")
            cloth = importlib.import_module("_ssmt_root.blueprint.drag_cloth")
            (directory / "shorts_bind.hlsl").write_text(cloth._shader(node, "shorts_bind.hlsl", source[:source.index("struct Vertex")].strip()), "utf8")
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(root, sections, comp)
            prefix = "ResourceSSMTCloth_abc123_unit_G0_"
            def read(name, width):
                v = r.resource(prefix + name)
                return r.gpu.read_bytes(v.pointer, v.size).copy().view("<f4").reshape(-1, width)
            try:
                r.prepare_identity()
                r.frame()
                state = read("State", 4)
                state[0, 0] = .5
                state[2, 0] = 1
                state[9, 1] = 1
                state[23, 3] = 1
                r.external(prefix + "State", state, 16, 2, flags=136)
                r.external(prefix + "Decision", np.array([[1, 0, 0, 1]], np.float32), 16, 2, flags=136)
                count = int(data["config"][0, 0])
                points = np.zeros((count, 8), np.float32)
                points[:, :3] = np.array([.1, .2, .3])
                points[:, 3] = 1
                points[:, 4:7] = points[:, :3]
                r.external(prefix + "Particles_s1", points, 32, structured=True, flags=136)
                weights = np.zeros((count, 8), np.float32)
                weights[:, :2] = .5
                weights[:, 4:].view("<u4")[:, 1] = 1
                r.external(prefix + "Weights_s1", weights, 32, structured=True, flags=136)
                cache = np.zeros((count, 17, 4), np.float32)
                cache[:, 3] = [.5, .5, 0, 1]
                cache[:, 5, 0] = cache[:, 7, 0] = 1
                cache[:, 8].view("<u4")[:, 0] = 1
                r.external(prefix + "ShortsBinding_s1", cache, 16, 2, flags=136)
                palette = np.tile(np.eye(4, dtype=np.float32)[:3], (2, 1, 1))
                palette[0, 0, 3] = -1
                palette[1, 0, 3] = 1
                r.external("ResourceSSMTCloth_abc123_unit_SkinPalette_s1", palette, 16, 2, flags=136)
                r.execute("CustomShaderSSMTClothG0Capture_s1_abc123_unit")
                frozen = read("ShortsBinding_s1", 4).reshape(count, 17, 4)[:, :11].copy()
                angle = .6
                rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]], np.float32)
                palette[0, :, :3] = rotation
                r.external("ResourceSSMTCloth_abc123_unit_SkinPalette_s1", palette, 16, 2, flags=136)
                r.execute("CustomShaderSSMTClothG0Skin_s1_abc123_unit")
                left = (points[:, :3] + [1, 0, 0]) @ rotation.T - [1, 0, 0]
                expected = (left + points[:, :3]) * .5
                np.testing.assert_allclose(read("Particles_s1", 8)[:, :3], expected, atol=2e-6)
                saved = read("Particles_s1", 8)
                for _ in range(5):
                    r.execute("CustomShaderSSMTClothG0Skin_s1_abc123_unit")
                    np.testing.assert_array_equal(read("Particles_s1", 8), saved)
                np.testing.assert_array_equal(read("ShortsBinding_s1", 4).reshape(count, 17, 4)[:, :11], frozen)
                # A failed new contact query must not recapture the previous
                # valid leg cache as if it belonged to this solve step.
                state = read("State", 4)
                state[9, 1] = 1
                r.external(prefix + "State", state, 16, 2, flags=136)
                r.external(prefix + "BodyOwners", np.zeros_like(data["body_regions"]), 4, 42)
                r.execute("CustomShaderSSMTClothG0Weights_s1_abc123_unit")
                invalid = read("ShortsBinding_s1", 4).reshape(count, 17, 4)
                self.assertTrue(np.all(invalid[:, 3, 3] != state[2, 0]))
                r.execute("CustomShaderSSMTClothG0Capture_s1_abc123_unit")
                self.assertTrue(np.all(read("ShortsBinding_s1", 4).reshape(count, 17, 4)[:, 10, 3] != state[2, 0]))
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_still_grab_freezes_last_mesh_skins_and_resumes_without_calibration(self):
        spec = importlib.util.spec_from_file_location(
            "frozen_input_gpu", Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        )
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, dense=True, budget=160)
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(root, sections, comp)
            prefix = "ResourceSSMTCloth_abc123_unit_G0_"
            data = comp["cloth"]["garments"][0]
            mask = data["map"] != 0xFFFFFFFF

            def read(name, width):
                v = r.resource(prefix + name)
                return r.gpu.read_bytes(v.pointer, v.size).copy().view("<f4").reshape(-1, width)

            try:
                r.prepare_identity()
                original = r.native.copy().view("<f4").reshape(-1, 10)
                r.frame()
                r.frame(slot=2)
                self.assertEqual(float(read("Decision", 4)[0, 0]), 2)
                r.frame(slot=1)
                state = read("State", 4)
                state[0, :2] = .45
                r.external(prefix + "State", state, 16, 2, flags=136)
                r.frame()
                r.force_full_solver = False
                r.variables["$ssmtcloth_unit_abc123_solve_until"] = -1
                hit = int(data["representatives"][20])
                before = r.frame(600, True, hit).copy()
                saved = {name: read(name, width) for name, width in (
                    ("FrozenMesh_s1", 4), ("Weights_s1", 8),
                    ("FrozenDisplay_s1", 4),
                    ("NodeContacts_s1", 4), ("Lambdas_s1", 1),
                )}
                traces = []
                dispatch = r.gpu.dispatch

                def trace(shader, groups, groups_y=1, groups_z=1):
                    path = next(path for path, value in r.shaders.items() if value == shader)
                    traces.append((path.name, int(r.params[127, 0])))
                    return dispatch(shader, groups, groups_y, groups_z)

                r.gpu.dispatch = trace
                for _ in range(12):
                    np.testing.assert_array_equal(r.frame(600, True), before)
                for name, value in saved.items():
                    np.testing.assert_array_equal(read(name, value.shape[1]), value)
                self.assertTrue(traces)
                self.assertTrue(all(name in ("copy_native.hlsl", "select.hlsl", "frozen_skin.hlsl", "cloth_nodes_p0.hlsl", "stabilize.hlsl", "display_normals.hlsl", "apply.hlsl") for name, _ in traces), traces)
                self.assertFalse(any(name == "frozen_skin.hlsl" and mode == 1 for name, mode in traces))
                # A render row omitted from the proxy must not be reprojected
                # from the native, fully worn garment while the pointer rests.
                detail = np.flatnonzero(mask & ~np.isin(np.arange(len(mask)), data["representatives"]))
                self.assertGreater(len(detail), 0)
                warped = original.copy()
                warped[int(detail[0]), :3] += [.03, -.02, .01]
                r.native = warped.view(np.uint8).reshape(-1, 40).copy()
                for slot in (1, 2):
                    r.external(f"ResourceZZRedirectSO_G3_s{slot}", warped, 40, flags=25)
                r.capture_skin_inputs()
                frozen = r.frame(600, True).copy().view("<f4").reshape(-1, 10)
                baseline = before.copy().view("<f4").reshape(-1, 10)
                np.testing.assert_allclose(frozen[mask, :3], baseline[mask, :3], atol=1e-6)
                angle = .12
                rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]], np.float32)
                translation = np.array([.1, -.02, .03], np.float32)
                moved = original.copy()
                moved[:, :3] = original[:, :3] @ rotation.T + translation
                moved[:, 3:6] = original[:, 3:6] @ rotation.T
                moved[:, 6:9] = original[:, 6:9] @ rotation.T
                r.native = moved.view(np.uint8).reshape(-1, 40).copy()
                matrix = np.c_[rotation, translation].astype(np.float32)
                for slot in (1, 2):
                    r.external(f"ResourceZZMergedSkeleton_G3_s{slot}", matrix, 16, 2)
                    r.external(f"ResourceZZRedirectSO_G3_s{slot}", moved, 40, flags=25)
                r.capture_skin_inputs()
                traces.clear()
                out = r.frame(600, True).copy().view("<f4").reshape(-1, 10)
                old = before.copy().view("<f4").reshape(-1, 10)
                np.testing.assert_allclose(out[mask, :3], old[mask, :3] @ rotation.T + translation, atol=1e-6)
                for name, value in saved.items():
                    np.testing.assert_array_equal(read(name, value.shape[1]), value)
                self.assertTrue(all(name in ("copy_native.hlsl", "select.hlsl", "frozen_skin.hlsl", "cloth_nodes_p0.hlsl", "stabilize.hlsl", "display_normals.hlsl", "apply.hlsl") for name, _ in traces), traces)
                traces.clear()
                r.frame(760, True, hit)
                self.assertTrue(any(name == "cloth_nodes_p4.hlsl" for name, _ in traces), traces)
                self.assertFalse(np.array_equal(read("FrozenMesh_s1", 4), saved["FrozenMesh_s1"]))
                resumed = r.frame(760, False).copy()
                traces.clear()
                for _ in range(8):
                    np.testing.assert_array_equal(r.frame(760, False), resumed)
                self.assertTrue(all(name in ("copy_native.hlsl", "select.hlsl", "frozen_skin.hlsl", "cloth_nodes_p0.hlsl", "stabilize.hlsl", "display_normals.hlsl", "apply.hlsl") for name, _ in traces), traces)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_dragging_another_region_cannot_recalibrate_frozen_self_collision(self):
        spec = importlib.util.spec_from_file_location(
            "frozen_other_region_gpu", Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        )
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, self_collision=True)
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(root, sections, comp)

            def read(group, name, width):
                v = r.resource(f"ResourceSSMTCloth_abc123_unit_G{group}_" + name)
                return r.gpu.read_bytes(v.pointer, v.size).copy().view("<f4").reshape(-1, width)

            try:
                r.prepare_identity()
                r.frame()
                for group in (0, 1):
                    state = read(group, "State", 4)
                    state[0, :2] = .45
                    r.external(f"ResourceSSMTCloth_abc123_unit_G{group}_State", state, 16, 2, flags=136)
                r.frame()
                state = read(1, "State", 4)
                state[3] = 0
                r.external("ResourceSSMTCloth_abc123_unit_G1_State", state, 16, 2, flags=136)
                saved = {name: read(1, name, width) for name, width in (
                    ("Particles_s1", 8), ("Weights_s1", 8), ("FrozenMesh_s1", 4),
                    ("FrozenDisplay_s1", 4), ("NodeContacts_s1", 4), ("Lambdas_s1", 1),
                )}
                r.force_full_solver = False
                r.variables["$ssmtcloth_unit_abc123_solve_until"] = -1
                hit = int(comp["cloth"]["garments"][0]["representatives"][20])
                r.frame(600, True, hit)
                for cursor in (650, 700, 750):
                    r.frame(cursor, True, hit)
                    for name, value in saved.items():
                        actual = read(1, name, value.shape[1])
                        if name == "Particles_s1":
                            np.testing.assert_array_equal(actual[:, :4], value[:, :4])
                            np.testing.assert_array_equal(actual[:, 4:7], actual[:, :3])
                        else:
                            np.testing.assert_array_equal(actual, value)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_triangle_contact_validation_preserves_all_three_corners(self):
        spec = importlib.util.spec_from_file_location(
            "triangle_validation_gpu", Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        )
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _node, sections, comp = self.prepare(root)
            r = gpu.GeneratedCloth(root, sections, comp)
            try:
                vertices = np.zeros((3, 10), np.float32)
                vertices[:, :3] = [[.13829404, .18135238, .12287094], [.10716498, .15720043, .12810786], [.11345845, .14670148, .15255499]]
                particles = np.zeros((3, 8), np.float32)
                particles[:, :3] = [[.12900451, .17777538, .11505076], [.13378957, .17150171, .13994354], [.14272240, .18492459, .12708080]]
                particles[:, 3] = 1
                bounds = np.zeros((4, 4), np.float32)
                bounds[2, :3], bounds[3, :3] = vertices[:, :3].min(0), vertices[:, :3].max(0)
                for slot, name, values, stride, fmt, structured in (
                    (24, "ContactSource", vertices, 40, 0, True),
                    (87, "ContactVerts", np.arange(3, dtype=np.uint32), 4, 42, False),
                    (89, "ContactOrder", np.zeros(1, np.uint32), 4, 42, False),
                    (91, "ContactBounds", bounds, 16, 2, False),
                    (127, "ContactMask", np.ones(1, np.uint32), 4, 42, False),
                ):
                    r.gpu.bind(slot, r.view(r.external(name, values, stride, fmt, structured=structured), False))
                ini = np.zeros((128, 4), np.float32)
                ini[115, 1] = 1
                _texture, view = r.gpu.ini(ini)
                r.gpu.bind(120, view)
                r.gpu.bind(0, r.view(r.external("ContactParticles", particles, 32, structured=True, flags=136), True), True)
                output = r.external("ContactResult", np.zeros((1, 4), np.float32), 16, 2, flags=136)
                r.gpu.bind(1, r.view(output, True), True)
                nodes = (self.exporter.TOOLSET / "cloth_nodes.hlsl").read_text("utf8")
                hit = nodes[nodes.index("bool Hit("):nodes.index("void SeparationAxis(")]
                validate = nodes[nodes.index("bool FaceBodyIntersection("):nodes.index("void FaceGlobal(")]
                code = (self.exporter.TOOLSET / "common.hlsli").read_text("utf8")
                code += "\nRWStructuredBuffer<Particle> Particles : register(u0);\n" + hit + validate + """
RWBuffer<float4> Result : register(u1);
[numthreads(1,1,1)]void main(){Result[0]=float4(FaceBodyIntersection(uint3(0,1,2))?1:0,0,0,0);}
"""
                shader = root / "contact_corners.hlsl"
                shader.write_text(code, "utf8")
                r.gpu.dispatch(r.gpu.shader(shader), 1)
                result = r.gpu.read_bytes(output.pointer, output.size).view("<f4")
                self.assertEqual(float(result[0]), 1, "GPU validation missed an actual triangle intersection")
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_material_ray_exits_the_union_of_overlapping_legs(self):
        spec = importlib.util.spec_from_file_location(
            "shorts_union_gpu", Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        )
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root)
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(root, sections, comp)
            base = "ResourceSSMTCloth_abc123_unit_"
            try:
                r.prepare_identity()
                original = r.native.copy().view("<f4").reshape(-1, 10)
                body = np.fromfile(root / comp["cloth"]["paths"]["body_vertices"], "<u4").reshape(-1, 3)
                owners = comp["cloth"]["garments"][0]["body_regions"]
                original[np.unique(body[owners == 1]), 0] += .1
                original[np.unique(body[owners == 2]), 0] -= .1
                r.native = original.view(np.uint8).reshape(-1, 40).copy()
                for slot in (1, 2):
                    r.external(f"ResourceZZRedirectSO_G3_s{slot}", original, 40, flags=25)
                r.capture_skin_inputs()
                r.frame()
                for slot, name in ((24, "NativeInput"), (87, "BodyVerts"), (89, "BodyOrder"), (91, "Bounds_s1")):
                    r.gpu.bind(slot, r.view(r.resource(base + name), False))
                output = r.external("UnionExitResult", np.zeros((2, 4), np.float32), 16, 2, flags=136)
                r.gpu.bind(0, r.view(output, True), True)
                code = (self.exporter.TOOLSET / "common.hlsli").read_text("utf8") + """
RWBuffer<float4> Result : register(u0);
[numthreads(1,1,1)]void main()
{
 float3 p=float3(0,0,.55),q;bool hit=DomainExitAlong(p,normalize(float3(1,.137,0)),.003,.2,q);
 Result[0]=float4(q,hit?1:0);Result[1]=float4(DomainInside(p)?1:0,DomainInside(q)?1:0,0,0);
}
"""
                shader = root / "union_exit.hlsl"
                shader.write_text(code, "utf8")
                r.gpu.dispatch(r.gpu.shader(shader), 1)
                result = r.gpu.read_bytes(output.pointer, output.size).view("<f4").reshape(-1, 4)
                self.assertEqual(float(result[1, 0]), 1)
                self.assertEqual(float(result[0, 3]), 1)
                self.assertEqual(float(result[1, 1]), 0)
                self.assertGreater(float(result[0, 0]), .068)
                self.assertLess(float(result[0, 0]), .08)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_sliding_pants_centre_seam_uses_both_legs(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("pants_centre_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                runner.frame()
                data = comp["cloth"]["garments"][0]
                prefix = "ResourceSSMTCloth_abc123_unit_G0_"
                config = data["config"].copy()
                config[12, 3] = 0
                config[14, 3] = 2
                config[15] = [0.12, 1, 1, 1]
                runner.external(prefix + "Config", config, 16, 2)
                rest = data["rest"].copy()
                particle = int(
                    np.flatnonzero(
                        (rest[::2, 3] > 0.5) & (data["hem_map"] == 0xFFFFFFFF)
                    )[10]
                )
                rest[particle * 2 + 1, 2:] = [0.6, 0]
                runner.external(prefix + "Rest", rest, 16, 2)
                state = runner.resource(prefix + "State")
                state = (
                    runner.gpu.read_bytes(state.pointer, state.size)
                    .copy()
                    .view("<f4")
                    .reshape(-1, 4)
                )
                state[0, :2] = 0.3
                state[8, :2] = 0.3
                runner.external(prefix + "State", state, 16, 2, flags=136)
                resource = runner.resource(prefix + "Particles_s1")
                values = (
                    runner.gpu.read_bytes(resource.pointer, resource.size)
                    .copy()
                    .view("<f4")
                    .reshape(-1, 8)
                )
                directions = data["body_directions"].copy()
                for sign in (-1, 1):
                    values[particle, :3] = [0, sign * 0.025, 0.366]
                    values[particle, 4:7] = values[particle, :3]
                    directions[particle] = [0, sign, 0, sign * 0.1]
                    runner.external(prefix + "BodyDirections", directions, 16, 2)
                    runner.external(
                        prefix + "Particles_s1", values, 32, structured=True, flags=136
                    )
                    runner.execute("CustomShaderSSMTClothG0Body_s1_abc123_unit")
                    resource = runner.resource(prefix + "Particles_s1")
                    out = (
                        runner.gpu.read_bytes(resource.pointer, resource.size)
                        .view("<f4")
                        .reshape(-1, 8)[particle, :3]
                    )
                    self.assertAlmostEqual(float(out[0]), 0, delta=0.003)
                    self.assertGreater(float(out[1]) * sign, 0.01)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_body_level_route_rejects_a_nearby_side_outside_its_radial_track(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("body_route_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _node, sections, comp = self.prepare(root)
            runner = gpu.GeneratedCloth(root, sections, comp)
            try:
                vertices = np.zeros((3, 10), np.float32)
                vertices[:, :3] = [[1, 1, 0], [2, 1, 1], [1, 1, 1]]
                vertices[:, 4] = 1
                source = runner.external("RouteBody", vertices, 40, structured=True)
                cfg = np.zeros((16, 4), np.float32)
                cfg[6] = [0, 0, 1, 1]
                cfg[14, 3] = 3
                cfg[15, 2:] = 1
                channels = np.array(
                    [[0, 1, 2, 1]] + [[0, 0, z, 0] for _ in range(3) for z in (0, 1)],
                    np.float32,
                )
                bindings = {
                    24: source,
                    111: source,
                    82: runner.external("RouteConfig", cfg, 16, 2),
                    124: runner.external("RouteChannels", channels, 16, 2),
                }
                for slot, name, values in (
                    (87, "RouteIDs", [0, 1, 2]),
                    (89, "RouteOrder", [0]),
                    (108, "RouteSurface", [1]),
                    (109, "RouteOwners", [3]),
                    (106, "RouteExterior", [1]),
                    (112, "RouteContact", [0]),
                ):
                    bindings[slot] = runner.external(
                        name, np.array(values, np.uint32), 4, 42
                    )
                for slot, resource in bindings.items():
                    runner.gpu.bind(slot, runner.view(resource, False))
                output = runner.external(
                    "RouteResult", np.zeros((1, 4), np.float32), 16, 2, flags=136
                )
                runner.gpu.bind(0, runner.view(output, True), True)
                code = (
                    (self.exporter.TOOLSET / "common.hlsli").read_text("utf8")
                    + """
RWBuffer<float4> Result : register(u0);
[numthreads(1,1,1)]void main(){float3 q,b,n;bool hit=BodyLevelPoint(0,3,float3(1.5,1,.5),.5,float3(1,0,0),q,b,n);Result[0]=float4(q,hit?1:0);}
"""
                )
                shader = root / "body_route.hlsl"
                shader.write_text(code, "utf8")
                runner.gpu.dispatch(runner.gpu.shader(shader), 1)
                result = runner.gpu.read_bytes(output.pointer, output.size).view("<f4")
                self.assertEqual(float(result[3]), 0)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_cuff_arc_sampling_distributes_a_flat_bridge_between_two_legs(self):
        p, n, f = cylinder(0, 0.2, rings=8, sides=96, cap_bottom=False)
        pieces = [p]
        normal_parts = [n]
        body = []
        for center in (-0.12, 0.12):
            q, qn, qf = cylinder(center, 0.05, height=1.1, cap_top=True)
            body.append(qf + sum(len(x) for x in pieces))
            pieces.append(q)
            normal_parts.append(qn)
        bf = np.concatenate(body)
        values = (
            np.concatenate(pieces),
            np.concatenate(normal_parts),
            np.r_[f, bf],
            np.array(["Stocking0"] * len(f) + ["Body"] * len(bf), object),
            f,
            bf,
        )
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("flat_cuff_bridge_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(__name__ + ".fixture", return_value=values),
        ):
            node, sections, comp = self.prepare(Path(temp), directions=("NEGATIVE",))
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(temp, sections, comp)
            try:
                r.prepare_identity()
                data = comp["cloth"]["garments"][0]
                row = int(data["representatives"][20])
                r.frame(hit_row=row)
                r.frame(600, True, row)
                for step in range(1, 16):
                    r.frame(600 - step * 5, True, row)
                resource = r.resource("ResourceSSMTCloth_abc123_unit_G0_Particles_s1")
                particles = (
                    r.gpu.read_bytes(resource.pointer, resource.size)
                    .view("<f4")
                    .reshape(-1, 8)
                )
                upper = int(data["config"][13, 0])
                ids = data["hem_params"][:upper, 3].astype(int)
                cuff = particles[ids, :3]
                self.assertLess(
                    float(
                        np.linalg.norm(cuff - np.roll(cuff, 1, axis=0), axis=1).max()
                    ),
                    0.06,
                )
            finally:
                r.close()

    def test_full_node_reexport_and_switch_off_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, _comp = self.prepare(root, bake=False)
            node.hash_values = "abc123"
            node.enable_viewport_probe = False
            node.feature_panel_link = False
            node.feature_shapekey_link = False
            node.feature_variable_link = False
            settings = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            for name, value in {
                "radius": 0.2,
                "max_offset": 0.5,
                "strength": 1.0,
                "spring": 0.1,
                "falloff": 0.5,
                "damping": 0.95,
                "grabbable": True,
                "brush_strength": 1.0,
                "brush_falloff_k": 1.0,
                "propagate": False,
            }.items():
                setattr(settings, name, value)
            matrix = np.eye(4) * 2
            matrix[3, 3] = 1
            matrix[2, 3] = 0.5
            empty = types.SimpleNamespace(
                name="ExportZone", matrix_world=matrix, ssmt_drag_zone=settings
            )
            node._collect_enabled_zone_entries = lambda: [(0, empty)]
            node._get_reference_matrix_inv = lambda comp: None
            node._get_non_mirror_mirror = lambda: None
            node._get_export_space_matrix = lambda *args: np.eye(4)
            ini = root / "model.ini"
            node._write_ordered_dict_to_ini(sections, str(ini))
            node.execute_postprocess(str(root))
            first = ini.read_text("utf8")
            node.execute_postprocess(str(root))
            second = ini.read_text("utf8")
            import difflib

            self.assertEqual(
                first,
                second,
                "\n".join(
                    difflib.unified_diff(first.splitlines(), second.splitlines())
                )[:5000],
            )
            node.cloth_slide_enabled = False
            node.execute_postprocess(str(root))
            self.assertNotIn("SSMTCloth", ini.read_text("utf8"))
            self.assertNotIn("$ssmtcloth_", ini.read_text("utf8"))
            self.assertFalse((root / "res/drag_cloth").exists())

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_cloth_uses_private_bone_matrix_vertices_and_palette(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("cloth_bone_matrix_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(folder)
            output = "ResourceSSMTBoneMatrixRender0Output"
            palette = "ResourceSSMTBoneMatrixRender0ResolvedPalette"
            sections["[TextureOverride_abc123_Draw]"].append(f"vb0 = ref {output}")
            sections[f"[{output}]"] = ["; SSMT bone matrix slot 1"]
            sections[f"[{palette}]"] = ["format = R32G32B32A32_FLOAT", "array = 6"]
            node._emit_sections(sections, [comp], "unit")
            self.assertTrue(comp["cloth_bone_input"])
            runner = gpu.GeneratedCloth(folder, sections, comp)
            try:
                runner.prepare_identity()
                runner.frame()
                runner.variables["time"] += 1 / 60
                coords = runner.native.copy().view("<f4").reshape(-1, 10)
                coords[:, 2] += 0.12
                output_resource = runner.external(output, coords, 40, flags=25)
                matrices = np.repeat(np.eye(4, dtype=np.float32)[None, :3], 2, axis=0)
                matrices[0, 2, 3] = 0.12
                palette_resource = runner.external(palette, matrices, 16, 2)
                runner.resources["vb0"] = output_resource
                sections["[TestBoneClothDraw]"] = [
                    *self.exporter.bind_bone_matrix_input(comp, "unit", {output: (1, palette)}),
                    f"run = {comp['cloth_bind']}",
                ]
                runner.execute("TestBoneClothDraw")
                native = runner.resources["ResourceSSMTCloth_abc123_unit_NativeSnapshot"]
                self.assertEqual(runner.resource("ResourceSSMTCloth_abc123_unit_EffectivePalette_s1").pointer,
                                 palette_resource.pointer)
                np.testing.assert_array_equal(
                    runner.gpu.read_bytes(native.pointer, native.size).view("<f4").reshape(-1, 10),
                    coords,
                )
            finally:
                runner.close()

    def test_cloth_can_generate_while_ordinary_collision_is_off(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.assertFalse(node._collision_enabled_for_component())
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            self.exporter.publish_before_draws(sections, comp)
            text = "\n".join("\n".join(lines) for lines in sections.values())
            self.assertIn("CommandListSSMTClothBind_abc123_unit", text)
            self.assertIn("R32_FLOAT", text)
            self.assertIn("stride = 4", text)
            self.assertIn("SkinPalette_s1 = copy ResourceZZMergedSkeleton_G3_s1", text)
            self.assertIn("SkinSource_s1 = ref so0", text)
            self.assertNotIn("CustomShaderDragCollisionLeaves", text)
            self.assertEqual(len(comp["cloth"]["garments"]), 2)
            for source in Path(temp).rglob("cloth_nodes.hlsl"):
                data = source.read_text("utf8")
                self.assertNotIn("#include", data)
                self.assertIn("geometryReady", data)

    def test_region_wear_switch_leaves_ordinary_drag_and_collision_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(
                root, bake=False, collision=True, directions=("NEGATIVE", "NEGATIVE")
            )
            entries = node._collect_enabled_zone_entries()
            disabled = entries[0][1].ssmt_drag_zone
            disabled.cloth_wear_enabled = False
            disabled.cloth_path_start = object()
            self.assertTrue(node._cloth_export_enabled())
            self.assertEqual(len(node._collect_enabled_zone_entries()), 2)
            self.assertTrue(node._collision_enabled_for_component())
            clothes, bodies = node._cloth_object_names(required=True)
            cloth = self.exporter.bake_component(
                node, temp, sections, comp, "unit", clothes, bodies, self.mod._blender_object_key,
            )
            for index in range(len(cloth["garments"])):
                controls = np.fromfile(
                    root / cloth["directory"] / f"g{index}/control_zones.buf", "<f4"
                ).reshape(-1, 4)
                self.assertEqual(controls[0, 0], 0)
                self.assertEqual(controls[1, 0], 1)
            entries[1][1].ssmt_drag_zone.cloth_wear_enabled = False
            self.assertFalse(node._cloth_export_enabled())
            self.assertEqual(node._cloth_object_names(), (set(), set()))
            self.assertTrue(node._collision_enabled_for_component())

    def test_overlong_compression_arrow_reports_its_zone_and_travel(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp), bake=False)
            _, entry = node._collect_enabled_zone_entries()[0]
            node._collect_enabled_zone_entries = lambda: [(3, entry)]
            settings = entry.ssmt_drag_zone
            settings.include_objects = settings.include_objects[:1]
            start, end = np.eye(4), np.eye(4)
            start[:3, 3] = (-.12, 0, 1)
            end[:3, 3] = (-.12, 0, -.1)
            settings.cloth_path_start = types.SimpleNamespace(matrix_world=start)
            settings.cloth_path_end = types.SimpleNamespace(matrix_world=end)
            node._get_reference_matrix_inv = lambda _comp: None
            node._get_export_space_matrix = lambda: None
            node._get_non_mirror_mirror = lambda: None
            clothes, body = node._cloth_object_names(required=True)
            with self.assertRaisesRegex(ValueError, r"区域 3.*行程 1.1000.*上限.*沿肢体整体滑移"):
                self.exporter.bake_component(
                    node, temp, sections, comp, "unit", clothes, body, self.mod._blender_object_key,
                )

    def test_single_mesh_proxy_exports_with_full_arrow_in_both_modes(self):
        for limb_slide in (False, True):
            with self.subTest(limb_slide=limb_slide), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                node, sections, comp = self.prepare(root, bake=False)
                settings = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
                settings.include_objects = settings.include_objects[:1]
                settings.cloth_proxy_enabled = True
                settings.cloth_limb_slide = limb_slide
                start, end = np.eye(4), np.eye(4)
                start[:3, 3] = (-.12, 0, 1)
                end[:3, 3] = (-.12, 0, .8)
                settings.cloth_path_start = types.SimpleNamespace(matrix_world=start)
                settings.cloth_path_end = types.SimpleNamespace(matrix_world=end)
                node._get_reference_matrix_inv = lambda _comp: None
                node._get_export_space_matrix = lambda: None
                node._get_non_mirror_mirror = lambda: None
                clothes, body = node._cloth_object_names(required=True)
                with patch.object(
                    self.exporter, "bake_limb_track",
                    return_value=(np.arange(3, dtype="<u4"), np.zeros((17, 3, 4, 4), "<f4")),
                ):
                    comp["cloth"] = self.exporter.bake_component(
                        node, temp, sections, comp, "unit", clothes, body, self.mod._blender_object_key,
                    )
                node._emit_sections(sections, [comp], "unit")
                directory = root / comp["cloth"]["directory"] / "g0"
                shader = directory / "proxy_apply.hlsl"
                lines = sections["[CustomShaderSSMTClothG0Apply_s1_abc123_unit]"]
                self.assertEqual(lines[0], "cs = " + shader.relative_to(root).as_posix())
                self.assertIn("#define PROXY_SAMPLES 17", shader.read_text("utf8"))
                axes = np.fromfile(directory / "control_axes.buf", "<f4").reshape(-1, 4)
                np.testing.assert_allclose(axes[0], (0, 0, -1, .2))

    def test_compression_proxy_binds_other_included_meshes_away_from_the_cuff(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp), bake=False)
            settings = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            settings.cloth_proxy_enabled = True
            start, end = np.eye(4), np.eye(4)
            start[:3, 3], end[:3, 3] = (-.12, 0, 1), (-.12, 0, .08)
            settings.cloth_path_start = types.SimpleNamespace(matrix_world=start)
            settings.cloth_path_end = types.SimpleNamespace(matrix_world=end)
            node._get_reference_matrix_inv = lambda _comp: None
            node._get_export_space_matrix = lambda: None
            node._get_non_mirror_mirror = lambda: None
            clothes, body = node._cloth_object_names(required=True)
            faces, names = node._read_component_triangles(temp, sections, comp, comp["vertex_count"])
            expected = faces[np.isin(names, ["Stocking0", "Stocking1"])]
            self.assertGreater(len(expected), len(faces[names == "Stocking0"]))
            rows = np.unique(expected).astype("<u4")
            with patch.object(self.exporter, "bake_limb_track", return_value=(
                rows, np.zeros((17, len(rows), 4, 4), "<f4"),
            )) as bake:
                cloth = self.exporter.bake_component(
                    node, temp, sections, comp, "unit", clothes, body, self.mod._blender_object_key,
                )
            bake.assert_called_once()
            np.testing.assert_array_equal(bake.call_args.args[2], expected)
            np.testing.assert_allclose(bake.call_args.args[6], [start[:3, 3], end[:3, 3]])
            np.testing.assert_allclose(cloth["garments"][0]["path_axes"][0], [0, 0, -1, .92])
            np.testing.assert_array_equal(cloth["garments"][0]["proxy_rows"], rows)

    def test_proxy_default_keeps_automatic_and_shared_arrow_controls_exportable(self):
        for shared_arrows in (False, True):
            with self.subTest(shared_arrows=shared_arrows), tempfile.TemporaryDirectory() as temp:
                directions = ("NEGATIVE", "NEGATIVE") if shared_arrows else ()
                node, sections, comp = self.prepare(Path(temp), bake=False, directions=directions)
                node._get_reference_matrix_inv = lambda _comp: None
                node._get_export_space_matrix = lambda: None
                node._get_non_mirror_mirror = lambda: None
                for zone, entry in node._collect_enabled_zone_entries():
                    settings = entry.ssmt_drag_zone
                    settings.cloth_proxy_enabled = True
                    settings.include_objects = settings.include_objects[:1]
                    if shared_arrows:
                        start, end = np.eye(4), np.eye(4)
                        start[:3, 3] = (-.12, 0, 1)
                        end[:3, 3] = (-.12, 0, .8 - zone * .1)
                        settings.cloth_path_start = types.SimpleNamespace(matrix_world=start)
                        settings.cloth_path_end = types.SimpleNamespace(matrix_world=end)
                clothes, body = node._cloth_object_names(required=True)
                with patch.object(self.exporter, "bake_limb_track") as bake:
                    cloth = self.exporter.bake_component(
                        node, temp, sections, comp, "unit", clothes, body, self.mod._blender_object_key,
                    )
                bake.assert_not_called()
                self.assertTrue(cloth["garments"])
                self.assertTrue(all("proxy_track" not in group for group in cloth["garments"]))

    def test_shared_mesh_with_independent_islands_uses_each_zones_arrow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, bake=False, directions=("NEGATIVE", "NEGATIVE"))
            positions = np.fromfile(root / "Meshes/abc123-Position.buf", "<f4").reshape(-1, 10)[:, :3]
            right = positions[:, 0] > 0
            ids = np.tile(np.array([0, 1, 0xFFFFFFFF, 0xFFFFFFFF], "<u4"), (len(positions), 1))
            weights = np.zeros_like(ids, dtype="<f4")
            weights[:, 0] = np.where(right, .2, .8)
            weights[:, 1] = np.where(right, .8, .2)
            ids.tofile(root / "Meshes/abc123JiggleZoneIDs.buf")
            weights.tofile(root / "Meshes/abc123JiggleZoneWeights.buf")
            authored = {}
            for zone, entry in node._collect_enabled_zone_entries():
                settings = entry.ssmt_drag_zone
                settings.cloth_proxy_enabled = True
                start, end = np.eye(4), np.eye(4)
                start[:3, 3] = (-.12 if zone == 0 else .12, 0, 1)
                end[:3, 3] = (-.12 if zone == 0 else .135, 0, .8 if zone == 0 else .72)
                settings.cloth_path_start = types.SimpleNamespace(matrix_world=start)
                settings.cloth_path_end = types.SimpleNamespace(matrix_world=end)
                vector = end[:3, 3] - start[:3, 3]
                length = np.linalg.norm(vector)
                authored[zone] = (*vector / length, length)
            node._get_reference_matrix_inv = lambda _comp: None
            node._get_export_space_matrix = lambda: None
            node._get_non_mirror_mirror = lambda: None

            def stub_track(positions, normals, faces, body, weights, indices, path, base, **kwargs):
                rows = np.unique(faces).astype("<u4") + base
                return rows, np.zeros((17, len(rows), 4, 4), "<f4")

            clothes, body = node._cloth_object_names(required=True)
            with patch.object(self.exporter, "bake_limb_track", side_effect=stub_track):
                result = self.exporter.bake_component(
                    node, root, sections, comp, "unit", clothes, body, self.mod._blender_object_key,
                )
            self.assertEqual(len(result["garments"]), 2)
            for garment in result["garments"]:
                zone, = np.flatnonzero(garment["control_zones"][:, 0])
                np.testing.assert_allclose(garment["control_axes"][zone], authored[zone], atol=1e-6)
                np.testing.assert_array_equal(right[garment["proxy_rows"]], zone == 1)

    def test_proxy_without_a_body_section_keeps_the_authored_wear_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, bake=False)
            _, empty = node._collect_enabled_zone_entries()[0]
            settings = empty.ssmt_drag_zone
            settings.cloth_proxy_enabled = True
            settings.include_objects = settings.include_objects[:1]
            start, end = np.eye(4), np.eye(4)
            start[:3, 3] = (-.12, 0, 1)
            end[:3, 3] = (-.12, 0, .8)
            settings.cloth_path_start = types.SimpleNamespace(matrix_world=start)
            settings.cloth_path_end = types.SimpleNamespace(matrix_world=end)
            node._get_reference_matrix_inv = lambda _comp: None
            node._get_export_space_matrix = lambda: None
            node._get_non_mirror_mirror = lambda: None
            clothes, body = node._cloth_object_names(required=True)
            with patch.object(self.exporter, "bake_limb_track", side_effect=self.exporter.ProxySectionCoverageError):
                cloth = self.exporter.bake_component(
                    node, root, sections, comp, "unit", clothes, body, self.mod._blender_object_key,
                )
            self.assertTrue(cloth["garments"])
            for garment in cloth["garments"]:
                self.assertNotIn("proxy_track", garment)
                np.testing.assert_allclose(garment["path_axes"][0], (0, 0, -1, .2))

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_single_zone_can_pull_either_end_through_the_full_travel(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("single_direction_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        for direction in ("NEGATIVE", "POSITIVE"):
            with (
                self.subTest(direction=direction),
                tempfile.TemporaryDirectory() as temp,
            ):
                node, sections, comp = self.prepare(Path(temp), directions=(direction,))
                node._emit_sections(sections, [comp], "unit")
                runner = gpu.GeneratedCloth(temp, sections, comp)
                try:
                    runner.prepare_identity()
                    data = comp["cloth"]["garments"][0]
                    row = int(data["representatives"][20])
                    runner.frame(hit_row=row)
                    runner.frame(600, True, row)
                    sign = -1 if direction == "NEGATIVE" else 1
                    for step in range(1, 76):
                        out = runner.frame(600 + sign * step * 5, True, row)
                    prefix = "ResourceSSMTCloth_abc123_unit_G0_"
                    resource = runner.resource(prefix + "State")
                    state = (
                        runner.gpu.read_bytes(resource.pointer, resource.size)
                        .view("<f4")
                        .reshape(-1, 4)
                    )
                    target = [1, 0] if sign < 0 else [0, 1]
                    np.testing.assert_allclose(state[0, :2], target, atol=1e-5)
                    self.assertEqual(state[23, 3], 1)
                    self.assertEqual(state[9, 0], 0)
                    particle_buffer = runner.resource(prefix + "Particles_s1")
                    particles = (
                        runner.gpu.read_bytes(
                            particle_buffer.pointer, particle_buffer.size
                        )
                        .view("<f4")
                        .reshape(-1, 8)
                    )
                    upper = int(data["config"][13, 0])
                    ids = data["hem_params"][:, 3].astype(int)
                    upper_height = particles[ids[:upper], 2].mean()
                    lower_height = particles[ids[upper:], 2].mean()
                    expected = [0.05, 0] if sign < 0 else [1, 0.95]
                    np.testing.assert_allclose(
                        [upper_height, lower_height], expected, atol=0.01
                    )
                    mask = data["map"] != 0xFFFFFFFF
                    self.assertFalse(np.array_equal(out[mask], runner.native[mask]))
                    runner.frame(600 + sign * 375, False, row, slot=2)
                    runner.frame(600 + sign * 375, False, row, slot=2)
                    state = (
                        runner.gpu.read_bytes(resource.pointer, resource.size)
                        .view("<f4")
                        .reshape(-1, 4)
                    )
                    np.testing.assert_allclose(state[0, :2], target, atol=1e-5)
                    self.assertEqual(state[9, 0], 0)
                    runner.frame(600 + sign * 375, False, row)
                    runner.variables[runner.prefix + "reset"] = 1
                    np.testing.assert_array_equal(runner.frame(), runner.native)
                finally:
                    runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_directional_vertical_input_follows_the_existing_drag_basis(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("vertical_basis_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        for direction, sign in (("NEGATIVE", -1), ("POSITIVE", 1)):
            with (
                self.subTest(direction=direction),
                tempfile.TemporaryDirectory() as temp,
            ):
                node, sections, comp = self.prepare(Path(temp), directions=(direction,))
                node._emit_sections(sections, [comp], "unit")
                runner = gpu.GeneratedCloth(temp, sections, comp)
                try:
                    runner.prepare_identity()
                    row = int(comp["cloth"]["garments"][0]["representatives"][20])
                    # Ordinary dragging consumes right*dx + basis[9]*dy.
                    # Here positive cursor Y therefore means positive local Z.
                    basis = ([1, 0, 0], [0, 0, 1])
                    runner.frame(hit_row=row)
                    runner.frame(600, True, row, camera_basis=basis)
                    for step in range(1, 16):
                        runner.frame(
                            600 + sign * step * 5, True, row, camera_basis=basis
                        )
                    resource = runner.resource("ResourceSSMTCloth_abc123_unit_G0_State")
                    state = (
                        runner.gpu.read_bytes(resource.pointer, resource.size)
                        .view("<f4")
                        .reshape(-1, 4)
                    )
                    endpoint = 0 if direction == "NEGATIVE" else 1
                    self.assertGreater(state[0, endpoint], 0.1)
                    self.assertEqual(state[0, 1 - endpoint], 0)
                    for step in range(14, -1, -1):
                        runner.frame(
                            600 + sign * step * 5, True, row, camera_basis=basis
                        )
                    state = (
                        runner.gpu.read_bytes(resource.pointer, resource.size)
                        .view("<f4")
                        .reshape(-1, 4)
                    )
                    self.assertLess(state[0, endpoint], 1e-5)
                finally:
                    runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_invalid_nonzero_basis_does_not_override_the_direction_fallback(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("invalid_basis_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(
                Path(temp), directions=("POSITIVE",), axis="X"
            )
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                row = int(comp["cloth"]["garments"][0]["representatives"][20])
                # The detector leaves nonzero defaults when calibration fails.
                basis = ([1, 0, 0, 0], [0, 1, 0, 0])
                runner.frame(hit_row=row)
                runner.frame(600, True, row, camera_basis=basis)
                for step in range(1, 16):
                    runner.frame(600 + step * 5, True, row, camera_basis=basis)
                resource = runner.resource("ResourceSSMTCloth_abc123_unit_G0_State")
                state = (
                    runner.gpu.read_bytes(resource.pointer, resource.size)
                    .view("<f4")
                    .reshape(-1, 4)
                )
                self.assertGreater(state[0, 1], 0.1)
                self.assertEqual(state[0, 0], 0)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_directional_mouse_input_projects_onto_a_horizontal_garment_axis(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("horizontal_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(
                Path(temp), directions=("POSITIVE",), axis="X"
            )
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                row = int(comp["cloth"]["garments"][0]["representatives"][20])
                basis = ([1, 0, 0], [0, 0, -1])
                runner.frame(hit_row=row)
                runner.frame(600, True, row, cursor_x=800, camera_basis=basis)
                for step in range(1, 6):
                    runner.frame(
                        600 + step * 5, True, row, cursor_x=800, camera_basis=basis
                    )
                state_resource = runner.resource(
                    "ResourceSSMTCloth_abc123_unit_G0_State"
                )

                def read():
                    return (
                        runner.gpu.read_bytes(
                            state_resource.pointer, state_resource.size
                        )
                        .view("<f4")
                        .reshape(-1, 4)
                    )

                self.assertEqual(read()[0, 1], 0)
                for step in range(1, 21):
                    runner.frame(
                        625, True, row, cursor_x=800 + step * 5, camera_basis=basis
                    )
                self.assertGreater(read()[0, 1], 0.1)
                self.assertEqual(read()[0, 0], 0)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_switching_to_an_angled_shorter_control_preserves_the_shared_pose(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("angled_controls_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, directions=("NEGATIVE", "NEGATIVE"))
            meta = comp["cloth"]
            assets = root / meta["directory"] / "g0"
            np.array([[1, 0.9, 0, 1], [1, 0.025, 0, 1]], np.float32).tofile(assets / "control_zones.buf")
            np.array([[1, 0, 0, 0.9], [0, 0, -1, 0.025]], np.float32).tofile(assets / "control_axes.buf")
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(root, sections, comp)
            try:
                runner.prepare_identity()
                row = int(meta["garments"][0]["representatives"][20])
                resource = runner.resource("ResourceSSMTCloth_abc123_unit_G0_State")

                def state():
                    return runner.gpu.read_bytes(resource.pointer, resource.size).view("<f4").reshape(-1, 4)

                basis = ([1, 0, 0], [0, 0, 1])
                runner.frame(hit_row=row)
                runner.frame(600, True, row, zone=0, cursor_x=800, camera_basis=basis)
                for step in range(1, 9):
                    runner.frame(600, True, row, zone=0, cursor_x=800 + 8 * step, camera_basis=basis)
                before = state()[0, 0]
                epoch = state()[2, 0]
                self.assertGreater(before, 0.05)
                runner.frame(600, False, row, zone=0, cursor_x=864, camera_basis=basis)
                runner.frame(600, True, row, zone=1, cursor_x=864, camera_basis=basis)
                np.testing.assert_allclose(state()[14, :2], [0, -1], atol=1e-5)
                runner.frame(592, True, row, zone=1, cursor_x=864, camera_basis=basis)
                self.assertAlmostEqual(state()[0, 0], before, places=6)
                self.assertEqual(state()[2, 0], epoch)
                runner.frame(600, True, row, zone=1, cursor_x=864, camera_basis=basis)
                self.assertGreater(state()[0, 0], 0.025)
                self.assertLess(state()[0, 0], before)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_two_regions_keep_separate_targets_and_carry_a_compressed_garment(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("two_end_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(
                Path(temp), directions=("NEGATIVE", "POSITIVE")
            )
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                row = int(comp["cloth"]["garments"][0]["representatives"][20])
                state_buffer = runner.resource("ResourceSSMTCloth_abc123_unit_G0_State")

                def state():
                    return (
                        runner.gpu.read_bytes(state_buffer.pointer, state_buffer.size)
                        .view("<f4")
                        .reshape(-1, 4)
                    )

                runner.frame(hit_row=row)
                runner.frame(600, True, row, zone=0)
                for step in range(1, 21):
                    runner.frame(600 - step * 5, True, row, zone=0)
                top = float(state()[0, 0])
                self.assertGreater(top, 0.2)
                self.assertEqual(state()[0, 1], 0)
                runner.frame(500, False, row)
                runner.frame(600, True, row, zone=1)
                for step in range(1, 21):
                    runner.frame(600 + step * 5, True, row, zone=1)
                self.assertAlmostEqual(float(state()[0, 0]), top, places=5)
                self.assertGreater(state()[0, 1], 0.2)
                for step in range(21, 76):
                    runner.frame(600 + step * 5, True, row, zone=1)
                    self.assertLessEqual(float(state()[0, :2].sum()), 1.00001)
                np.testing.assert_allclose(state()[0, :2], [0, 1], atol=1e-5)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_both_features_route_the_draw_to_one_solver_at_a_time(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("cloth_draw_gate_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, collision=True)
            node._write_collision_resources(str(root), sections, comp, "unit")
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            lines = sections[comp["parts"][0]["section"]]
            start = next(i for i, line in enumerate(lines) if "DRAG HOOK BEGIN" in line)
            end = next(i for i, line in enumerate(lines) if "DRAG HOOK END" in line)
            sections["[TestDrawGate]"] = [
                line
                for line in lines[start:end]
                if not line.strip().startswith("post ")
            ]
            cn = comp["comp_name"]
            sections[f"[CustomShaderDragJiggle{cn}_unit]"] = ["$ordinary_called = 1"]
            runner = gpu.GeneratedCloth(root, sections, comp)
            try:
                runner.prepare_identity()
                runner.external(
                    f"ResourceDragJiggleTempVB0_{cn}_unit", runner.native, 40, flags=1
                )
                runner.external(
                    f"Resource{cn}Blend",
                    np.fromfile(root / "Meshes/abc123-Blend.buf", np.uint8),
                    32,
                    flags=1,
                )
                runner.variables.update(
                    {
                        "$ssmtdrag_mode_unit": 1,
                        "$ObjectDetectAllowed_unit": 0,
                        "$ssmtdrag_viewport_probe_armed_unit": 0,
                        "$ordinary_called": 0,
                        "time": 1,
                    }
                )
                cloth = runner.prefix + cn + "_"
                runner.variables[cloth + "activated"] = 0
                draw_source = runner.resources["vb0"].pointer
                runner.execute("TestDrawGate")
                self.assertEqual(runner.variables["$ordinary_called"], 1)
                self.assertEqual(runner.dispatches, 0)
                runner.execute(comp["cloth_restore"])
                self.assertEqual(runner.resources["vb0"].pointer, draw_source)
                runner.resources[f"ResourceSSMTCloth_{cn}_unit_GestureHit"] = runner.resource(
                    f"ResourceDragPinnedComponentInfo_{cn}_unit"
                )
                runner.variables.update({
                    "$ordinary_called": 0,
                    "$isMouseButtonDown": 1,
                    cloth + "pointer_held": 1,
                    cloth + "intent": 2,
                    "time": 2,
                })
                runner.execute("TestDrawGate")
                self.assertEqual(runner.variables["$ordinary_called"], 0)
                self.assertGreater(runner.dispatches, 0)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_cloth_contact_blocks_other_surfaces_and_resolves_anchored_faces(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("cloth_contact_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        for anchored, authored in ((False, False), (True, False), (True, True)):
            with (
                self.subTest(anchored=anchored, authored=authored),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                node, sections, comp = self.prepare(root)
                node._emit_sections(sections, [comp], "unit")
                runner = gpu.GeneratedCloth(root, sections, comp)
                try:
                    runner.prepare_identity()
                    runner.frame()
                    data = comp["cloth"]["garments"][0]
                    particle_ids = data["faces"][0].astype(int)
                    rest = data["rest"].copy()
                    hem = data["hem_map"].copy()
                    hem[particle_ids] = 0xFFFFFFFF
                    rest[particle_ids * 2, 3] = 0 if anchored else 1
                    prefix = "ResourceSSMTCloth_abc123_unit_G0_"
                    state_resource = runner.resource(prefix + "State")
                    state_values = (
                        runner.gpu.read_bytes(
                            state_resource.pointer, state_resource.size
                        )
                        .view("<f4")
                        .copy()
                        .reshape(-1, 4)
                    )
                    state_values[0, 0] = 0.1
                    runner.external(prefix + "State", state_values, 16, 2, flags=136)
                    runner.external(prefix + "Rest", rest, 16, 2)
                    runner.external(prefix + "HemMap", hem, 4, 42)
                    if authored:
                        config = data["config"].copy()
                        config[15, 3] = 1
                        runner.external(prefix + "Config", config, 16, 2)
                    other_owner = 2 if int(data["config"][12, 3]) == 1 else 1
                    triangle = int(
                        np.flatnonzero(
                            (data["body_regions"] == other_owner)
                            & (data["surface_mask"] != 0)
                        )[0]
                    )
                    body_ids = np.fromfile(
                        root / comp["cloth"]["paths"]["body_vertices"], "<u4"
                    ).reshape(-1, 3)[triangle]
                    source = runner.native.view("<f4").reshape(-1, 10)
                    body = source[body_ids, :3]
                    normal = source[body_ids, 3:6].mean(0)
                    normal /= np.linalg.norm(normal)
                    center = body.mean(0)
                    tangent = body[1] - body[0]
                    tangent /= np.linalg.norm(tangent)
                    points = np.array(
                        [
                            center - normal * 0.01,
                            center + normal * 0.01 + tangent * 0.002,
                            center + normal * 0.01 - tangent * 0.002,
                        ],
                        np.float32,
                    )
                    particles = runner.resources[prefix + "Particles_s1"]
                    values = runner.gpu.read_bytes(
                        particles.pointer, particles.size
                    ).copy()
                    values = values.view("<f4").reshape(-1, 8)
                    values[particle_ids, :3] = points
                    values[particle_ids, 4:7] = points
                    runner.external(
                        prefix + "Particles_s1", values, 32, structured=True, flags=136
                    )
                    slot = int(np.flatnonzero(data["face_colors"] == 0)[0])
                    color = int(
                        np.searchsorted(data["face_offsets"], slot, side="right") - 1
                    )
                    runner.execute(f"CustomShaderSSMTClothG0Face{color}_s1_abc123_unit")
                    particles = runner.resources[prefix + "Particles_s1"]
                    after = runner.gpu.read_bytes(particles.pointer, particles.size)
                    after = after.view("<f4").reshape(-1, 8)[particle_ids, :3]
                    if authored:
                        np.testing.assert_array_equal(after, points)
                        continue
                    self.assertGreater(
                        float(np.max(np.linalg.norm(after - points, axis=1))), 1e-5
                    )
                    self.assertGreater(float((after[0] - points[0]) @ normal), 0)
                finally:
                    runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_cold_render_slot_keeps_the_current_wear_grab(self):
        from tools.check_drag_cloth import GeneratedCloth

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root)
            node._emit_sections(sections, [comp], "unit")
            runner = GeneratedCloth(root, sections, comp)
            try:
                runner.prepare_identity()
                runner.force_full_solver = False
                wear_map = comp["cloth"]["garments"][0]["map"]
                rows = np.flatnonzero(wear_map != 0xFFFFFFFF)
                hit = int(rows[len(rows) // 2])
                name = "ResourceSSMTCloth_abc123_unit_G0_State"

                def read_state():
                    resource = runner.resource(name)
                    return runner.gpu.read_bytes(resource.pointer, resource.size).view("<f4").reshape(-1, 4)

                runner.frame(600, False, hit)
                runner.frame(600, True, hit)
                runner.frame(580, True, hit)
                before = read_state()
                self.assertGreater(float(before[0, 0]), 0)
                self.assertGreaterEqual(float(before[1, 0]), 0)
                self.assertEqual(float(before[18, 2]), 0)

                runner.frame(570, True, hit, slot=2)
                switched = read_state()
                self.assertEqual(float(switched[0, 2]), 1)
                self.assertEqual(float(switched[1, 0]), float(before[1, 0]))
                self.assertGreater(float(switched[0, 0]), float(before[0, 0]))

                runner.frame(560, True, hit, slot=1)
                self.assertGreater(float(read_state()[0, 0]), float(switched[0, 0]))
            finally:
                runner.close()

    def test_host_scoped_stream_output_snapshots_only_its_own_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, bake=False, prefix=3)
            count = comp["vertex_count"] - 3
            deform = "[TextureOverride_VB_abc123_Position]"
            lines = sections[deform]
            del lines[3:]
            for slot in (1, 2):
                host = f"ResourceZZRedirectSO_G3_abc123_s{slot}"
                other = f"ResourceZZRedirectSO_G3_def456_s{slot}"
                palette = f"ResourceZZPaletteConv_C7_s{slot}"
                for output in (host, other):
                    sections[f"[{output}]"] = ["type = Buffer", "stride = 40"]
                sections[f"[{palette}]"] = ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 6"]
                for output in (other, f"ResourceZZRedirectSO_G3_s{slot}", host):
                    lines.extend([
                        "if $seen == 1", f"    vs-t0 = {palette}", f"    so0 = ref {output}",
                        f"    draw = {count}, 0", "    so0 = null", "endif",
                    ])

            cloth, body = node._cloth_object_names(required=True)
            comp["cloth"] = self.exporter.bake_component(
                node, root, sections, comp, "unit", cloth, body, self.mod._blender_object_key,
            )
            writes = comp["cloth"]["skin_writes"]
            self.assertEqual(comp["cloth"]["palette_rows"], {1: 6, 2: 6})
            self.assertEqual([(slot, palette, source) for _, _, slot, palette, source in writes], [
                (slot, f"ResourceZZPaletteConv_C7_s{slot}", "so0") for slot in (1, 2)
            ])
            for section, index, slot, _, _ in writes:
                self.assertEqual(sections[section][index - 1].strip(),
                                 f"so0 = ref ResourceZZRedirectSO_G3_abc123_s{slot}")
            node._emit_sections(sections, [comp], "unit")
            self.assertEqual(sum(" = ref so0" in line for line in sections[deform]), 2)

    def test_skinout_snapshots_the_converted_palette_at_each_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, bake=False)
            sections[comp["parts"][0]["section"]].insert(
                3, "vb0 = ref ResourceZZSkinOut_G3_s1"
            )
            for slot in (1, 2):
                output = f"ResourceZZSkinOut_G3_s{slot}"
                palette = f"ResourceZZPaletteConv_C7_s{slot}"
                sections[f"[{output}]"] = ["type = Buffer", "stride = 40"]
                sections[f"[{palette}]"] = [
                    "type = RWBuffer",
                    "format = R32G32B32A32_FLOAT",
                    "array = 6",
                ]
                sections[f"[CustomShaderZZMISkin_G3_s{slot}]"] = [
                    "cs = ./res/zzmi_merged_skin.hlsl",
                    f"x1 = {comp['vertex_count']}",
                    "cs-t0 = ref Resource_abc123_Position",
                    f"cs-t2 = ref {palette}",
                    f"cs-u0 = ref {output}",
                    "Dispatch = 8, 1, 1",
                ]
            cloth, body = node._cloth_object_names(required=True)
            comp["cloth"] = self.exporter.bake_component(
                node,
                str(root),
                sections,
                comp,
                "unit",
                cloth,
                body,
                self.mod._blender_object_key,
            )
            node._emit_sections(sections, [comp], "unit")
            for slot in (1, 2):
                lines = sections[f"[CustomShaderZZMISkin_G3_s{slot}]"]
                dispatch = lines.index("Dispatch = 8, 1, 1")
                self.assertEqual(
                    [line.strip() for line in lines[dispatch - 12 : dispatch]],
                    [
                        self.exporter.DRAW_BEGIN,
                        f"$ssmtcloth_unit_abc123_source_valid_s{slot} = 0",
                        "if cs-u0 !== null",
                        f"ResourceSSMTCloth_abc123_unit_SkinSource_s{slot} = ref cs-u0",
                        f"ResourceSSMTCloth_abc123_unit_SkinPalette_s{slot} = copy ResourceZZPaletteConv_C7_s{slot}",
                        f"$ssmtcloth_unit_abc123_ready_s{slot} = -1",
                        f"$ssmtcloth_unit_abc123_source_valid_s{slot} = 1",
                        f"$ssmtcloth_unit_abc123_last_skin_write_slot = {slot}",
                        "$ssmtcloth_unit_abc123_seen = 1",
                        "$ssmtdrag_last_dispatch_abc123_unit = -1",
                        "endif",
                        self.exporter.DRAW_END,
                    ],
                )
            self.assertFalse(
                any(
                    "SkinPalette" in line
                    for line in sections["[TextureOverride_VB_abc123_Position]"]
                )
            )

    def test_native_restore_and_ttl_publish_survive_legacy_rebinding(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.exporter.emit_components(node, sections, [comp], "unit")
            wrapper = "CommandListSSMTTTLDraw_unit"
            sections["[" + wrapper + "]"] = [
                "vb0 = ResourceDragJiggleTempVB0_abc123_unit",
                "run = CommandList\\TTL\\Draw",
            ]
            part = comp["parts"][0]
            sections[part["section"]].append("run = " + wrapper)
            self.exporter.publish_before_draws(sections, comp)
            block = sections["[" + wrapper + "]"]
            self.assertLess(
                block.index("vb0 = ResourceDragJiggleTempVB0_abc123_unit"),
                block.index("run = " + comp["cloth_publish"]),
            )
            self.assertLess(
                block.index("run = " + comp["cloth_publish"]),
                block.index("run = CommandList\\TTL\\Draw"),
            )
            node._inject_draw_hooks(sections, comp, "unit")
            self.assertIn(
                "post run = " + comp["cloth_restore"],
                "\n".join(sections[part["section"]]),
            )

    def test_turning_off_removes_owned_assets_and_inline_draw_references(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(folder)
            self.exporter.emit_components(node, sections, [comp], "unit")
            self.exporter.publish_before_draws(sections, comp)
            self.exporter.clean_sections(sections, "unit")
            self.exporter.prune_assets(folder, "unit", set())
            self.assertFalse((folder / "res/drag_cloth").exists())
            self.assertNotIn(
                "run = " + comp["cloth_publish"],
                "\n".join("\n".join(v) for v in sections.values()),
            )

    def test_repeated_export_keeps_shaders_and_buffers_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.prepare(folder)
            before = {
                str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (folder / "res/drag_cloth").rglob("*")
                if p.is_file()
            }
            self.prepare(folder)
            after = {
                str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (folder / "res/drag_cloth").rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)

    def test_material_copy_sections_have_bind_publish_and_native_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.exporter.emit_components(node, sections, [comp], "unit")
            copy = "[TextureOverrideMaterialCopy]"
            original = ["hash = abc123", "ib = ResourceIB", "drawindexed = 3, 0, 0"]
            sections[copy] = original.copy()
            comp["parts"][0]["draw_records"].append({"section": copy})
            self.exporter.publish_before_draws(sections, comp)
            self.assertIn("run = " + comp["cloth_bind"], sections[copy])
            self.assertIn("post run = " + comp["cloth_restore"], sections[copy])
            self.assertLess(
                sections[copy].index("run = " + comp["cloth_publish"]),
                sections[copy].index("drawindexed = 3, 0, 0"),
            )
            first = list(sections[copy])
            self.exporter.publish_before_draws(sections, comp)
            self.assertEqual(first, sections[copy])
            self.exporter.clean_sections(sections, "unit")
            self.assertEqual(sections[copy], original)

    def test_surface_collision_keeps_far_controlled_faces_without_point_anchors(self):
        node = _make_node(
            self.mod,
            collision_enabled=True,
            collision_tri_count=0,
            collision_point_budget=32,
        )
        p = np.array(
            [
                [-1, -1, 0],
                [1, -1, 0],
                [1, 1, 0],
                [-1, 1, 0],
                [-1, -1, 0.6],
                [1, -1, 0.6],
                [1, 1, 0.6],
                [-1, 1, 0.6],
            ],
            np.float32,
        )
        faces = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]], np.uint32)
        names = np.array(["Body", "Body", "Cloth", "Cloth"], object)
        settings = types.SimpleNamespace(
            collider_objects=[
                types.SimpleNamespace(
                    object=types.SimpleNamespace(
                        name="Body", name_full="Body", type="MESH"
                    )
                )
            ]
        )
        node._collect_enabled_zone_entries = lambda: [
            (0, types.SimpleNamespace(ssmt_drag_zone=settings))
        ]
        node._read_position_buf = lambda *args: p
        node._read_vertex_normals = lambda *args: np.tile([0, 0, 1], (8, 1))
        node._read_component_triangles = lambda *args: (faces, names)
        node._buffer_dir = lambda *args: "Meshes"
        node._collider_l0_mask = lambda *args: np.array([False] * 4 + [True] * 4)
        node._collider_contact_exact = lambda *args, **kw: np.zeros((16, 4), np.float32)
        comp = {
            "vertex_count": 8,
            "base_name": "sample",
            "comp_name": "sample",
            "parts": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(node._write_collision_resources(temp, {}, comp, "unit"))
            np.testing.assert_array_equal(
                np.fromfile(
                    Path(temp) / "Meshes/sampleColliderCloth.buf", "<u4"
                ).reshape(-1, 3),
                faces[2:],
            )
            self.assertEqual(comp["collision_surface"]["face_count"], 2)

    def test_preserved_ttl_tail_is_republished_and_can_be_cleaned(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.exporter.emit_components(node, sections, [comp], "unit")
            name = "CommandListSSMTTTLDraw_old"
            sections[comp["parts"][0]["section"]].append("run = " + name)
            original = (
                "[SomeUI]\ndata = 1\n["
                + name
                + "]\nvb0 = ResourceDragJiggleTempVB0_abc123_unit\nrun = CommandList\\TTL\\Draw\n"
            )
            patched = self.exporter.publish_in_tail(original, sections, [comp])
            self.assertIn("run = " + comp["cloth_publish"], patched)
            self.assertEqual(self.exporter.clean_tail(patched), original)
            self.assertEqual(
                self.exporter.publish_in_tail(patched, sections, [comp]), patched
            )

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_generated_proxy_mapping_keeps_extra_vertex_fields(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("mapped_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(
                folder, dense=True, extra=True, budget=160
            )
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            runner = gpu.GeneratedCloth(folder, sections, comp)
            try:
                runner.prepare_identity()
                native = runner.native.copy()
                g = comp["cloth"]["garments"][0]
                mask = g["map"] != 0xFFFFFFFF
                self.assertLessEqual(len(g["representatives"]), 160)
                row = int(np.flatnonzero(mask)[len(np.flatnonzero(mask)) // 2])
                runner.frame(hit_row=row)
                runner.frame(600, True, row)
                for k in range(1, 15):
                    out = runner.frame(600 - k * 4, True, row)
                np.testing.assert_array_equal(out[:, 40:], native[:, 40:])
                np.testing.assert_array_equal(out[~mask], native[~mask])
                self.assertGreater(
                    np.max(
                        np.linalg.norm(
                            out.copy().view("<f4").reshape(-1, 12)[mask, :3]
                            - native.copy().view("<f4").reshape(-1, 12)[mask, :3],
                            axis=1,
                        )
                    ),
                    0.02,
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_empty_native_cannot_match_an_unused_zero_palette(self):
        spec = importlib.util.spec_from_file_location(
            "empty_palette_gpu", Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        )
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root)
            node._emit_sections(sections, [comp], "unit")
            r = gpu.GeneratedCloth(root, sections, comp)
            prefix = "ResourceSSMTCloth_abc123_unit_"
            try:
                bind = np.fromfile(root / comp["cloth"]["directory"] / "bind.buf", "<f4").reshape(-1, 10)
                r.external(prefix + "BindInput", bind, 40, structured=True)
                r.external(prefix + "NativeInput", np.zeros_like(bind), 40, structured=True)
                r.external(prefix + "SkinPalette_s1", np.eye(4, dtype=np.float32)[:3], 16, 2)
                r.external(prefix + "SkinPalette_s2", np.zeros((3, 4), np.float32), 16, 2)
                r.execute("CustomShaderSSMTClothG0Select_abc123_unit")
                resource = r.resource(prefix + "G0_Decision")
                decision = r.gpu.read_bytes(resource.pointer, resource.size).view("<f4").reshape(-1, 4)
                self.assertEqual(decision[0, 3], 0, "an empty output must not match an unused zero skeleton")
                r.external(prefix + "NativeInput", bind, 40, structured=True)
                r.execute("CustomShaderSSMTClothG0Select_abc123_unit")
                decision = r.gpu.read_bytes(resource.pointer, resource.size).view("<f4").reshape(-1, 4)
                self.assertEqual(decision[0, 0], 1)
                self.assertEqual(decision[0, 3], 1)
            finally:
                r.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_unready_native_frame_does_not_poison_first_generated_drag(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("startup_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(folder)
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            runner = gpu.GeneratedCloth(folder, sections, comp)
            try:
                runner.prepare_identity()
                native = runner.native.copy()
                meta = comp["cloth"]
                masks = [g["map"] != 0xFFFFFFFF for g in meta["garments"]]
                all_cloth = np.logical_or.reduce(masks)
                source = runner.resources[
                    f"{meta['native_family']}_G{meta['group']}_s1"
                ]
                broken = native.copy()
                broken[all_cloth] = 0

                def upload(value):
                    runner.gpu.method(
                        runner.gpu.context,
                        48,
                        [
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_void_p,
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_uint,
                        ],
                        source.pointer,
                        0,
                        None,
                        value.ctypes.data,
                        0,
                        0,
                        result=None,
                    )

                row = int(np.flatnonzero(masks[0])[30])
                upload(broken)
                runner.frame(hit_row=row)
                for name, value in runner.resources.items():
                    if name.endswith("_State"):
                        state = (
                            runner.gpu.read_bytes(value.pointer, value.size)
                            .copy()
                            .view("<f4")
                            .reshape(-1, 4)
                        )
                        self.assertEqual(state[23, 3], 0)
                        self.assertEqual(state[17, 2], 0)
                upload(native)
                np.testing.assert_array_equal(runner.frame(hit_row=row), native)
                runner.frame(600, True, row)
                for k in range(1, 16):
                    first = runner.frame(600 - k * 4, True, row)
                runner.variables[runner.prefix + "reset"] = 1
                runner.frame(540, False, row)
                runner.frame(600, True, row)
                for k in range(1, 16):
                    reset = runner.frame(600 - k * 4, True, row)
                np.testing.assert_allclose(
                    first.copy().view("<f4").reshape(-1, 10)[:, :3],
                    reset.copy().view("<f4").reshape(-1, 10)[:, :3],
                    atol=2e-5,
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_emitted_pipeline_runs_independent_stockings_and_preserves_body(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("generated_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(folder)
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            runner = gpu.GeneratedCloth(folder, sections, comp)
            try:
                runner.prepare_identity()
                native = runner.native.copy()
                garments = comp["cloth"]["garments"]
                left = next(d for d in garments if d["config"][12, 3] == 1)
                right = next(d for d in garments if d["config"][12, 3] == 2)
                left_mask = left["map"] != 0xFFFFFFFF
                right_mask = right["map"] != 0xFFFFFFFF
                row = int(
                    np.flatnonzero(left_mask)[len(np.flatnonzero(left_mask)) // 2]
                )
                idle = runner.frame(hit_row=row)
                np.testing.assert_array_equal(idle, native)
                runner.frame(600, True, row)
                for k in range(1, 20):
                    out = runner.frame(600 - k * 4, True, row)
                out_float = out.copy().view("<f4").reshape(-1, 10)
                native_float = native.copy().view("<f4").reshape(-1, 10)
                self.assertGreater(
                    np.max(
                        np.linalg.norm(
                            out_float[left_mask, :3] - native_float[left_mask, :3],
                            axis=1,
                        )
                    ),
                    0.02,
                )
                np.testing.assert_array_equal(out[right_mask], native[right_mask])
                np.testing.assert_array_equal(
                    out[~(left_mask | right_mask)], native[~(left_mask | right_mask)]
                )
                runner.external(
                    "vb2", np.zeros((len(native), 16), np.uint8), 16, flags=1
                )
                # Re-capture an actual render VB2 stride, independently of blend data.
                runner.frame(524, False, row)
                source = runner.resources[
                    f"{comp['cloth']['native_family']}_G{comp['cloth']['group']}_s1"
                ]
                runner.external("legacy_temp", np.zeros_like(native), 40, flags=1)
                runner.resources["vb0"] = runner.resources["legacy_temp"]
                runner.execute(comp["cloth_publish"])
                self.assertNotEqual(
                    runner.resources["vb0"].pointer,
                    runner.resources["legacy_temp"].pointer,
                )
                runner.execute(comp["cloth_restore"])
                self.assertEqual(runner.resources["vb0"].pointer, source.pointer)
                self.assertEqual(runner.resources["vb2"].stride, 16)
                count = runner.dispatches
                runner.variables["$ssmtdrag_drag_enabled_unit"] = 1
                np.testing.assert_array_equal(runner.frame(), native)
                self.assertEqual(runner.dispatches, count)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_generated_cloth_uses_the_palette_that_skinned_the_vertices(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("cloth_palette_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                mask = comp["cloth"]["garments"][0]["map"] != 0xFFFFFFFF
                row = int(np.flatnonzero(mask)[30])
                runner.frame(hit_row=row)
                runner.frame(600, True, row)
                for step in range(1, 23):
                    dressed = runner.frame(600 - step * 4, True, row)
                self.assertFalse(np.array_equal(dressed[mask], runner.native[mask]))

                # Simulate a later attach writing a different pose into the live
                # shared palette after the native SO has already been skinned.
                updated = np.eye(4, dtype=np.float32)[:3].copy()
                updated[0, 3] = 0.15
                for slot in (1, 2):
                    runner.external(
                        f"ResourceZZMergedSkeleton_G3_s{slot}", updated, 16, 2
                    )
                out = runner.frame(512, False, row)
                self.assertFalse(
                    np.array_equal(out[mask], runner.native[mask]),
                    "a late bone write must not flash back to the fully worn mesh",
                )
                np.testing.assert_array_equal(out[~mask], runner.native[~mask])
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_prefix_reference_copy_follows_current_bind_changes(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("prefix_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, prefix=3)
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            self.assertIsNone(comp["cloth"]["dynamic_bind"])
            self.assertEqual(len(comp["cloth"]["skin_writes"]), 2)
            runner = gpu.GeneratedCloth(root, sections, comp)
            try:
                runner.prepare_identity()
                g = comp["cloth"]["garments"][0]
                mask = g["map"] != 0xFFFFFFFF
                row = int(np.flatnonzero(mask)[30])
                runner.frame(hit_row=row)
                runner.frame(600, True, row)
                for k in range(1, 10):
                    runner.frame(600 - k * 4, True, row)
                local = np.fromfile(root / "Meshes/abc123-Position.buf", "<f4").reshape(
                    -1, 10
                )
                local[:, :3] += [0, 0, 0.04]
                native = runner.native.copy()
                native[3:] = local.view(np.uint8).reshape(-1, 40)
                native_source = runner.resources[
                    f"{comp['cloth']['native_family']}_G{comp['cloth']['group']}_s1"
                ]
                raw_source = runner.resources[comp["base_resource"]]
                for target, value in ((native_source, native), (raw_source, local)):
                    runner.gpu.method(
                        runner.gpu.context,
                        48,
                        [
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_void_p,
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_uint,
                        ],
                        target.pointer,
                        0,
                        None,
                        value.ctypes.data,
                        0,
                        0,
                        result=None,
                    )
                out = runner.frame(564, False, row)
                decisions = [
                    value
                    for name, value in runner.resources.items()
                    if name.endswith("_Decision")
                ]
                for value in decisions:
                    self.assertEqual(
                        runner.gpu.read_bytes(value.pointer, value.size)
                        .view("<f4")
                        .reshape(-1, 4)[0, 3],
                        1,
                    )
                np.testing.assert_array_equal(out[:3], native[:3])
                np.testing.assert_array_equal(
                    out[~mask & (g["map"] == 0xFFFFFFFF)],
                    native[~mask & (g["map"] == 0xFFFFFFFF)],
                )
                # Selected cloth remains dressed rather than falling back to full native output.
                self.assertGreater(
                    np.max(abs(out[mask].view("<f4") - native[mask].view("<f4"))), 0.01
                )
            finally:
                runner.close()


if __name__ == "__main__":
    unittest.main()
