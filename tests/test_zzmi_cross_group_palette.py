# -*- coding: utf-8 -*-
"""ZZMI 跨组骨骼合并（调色板归一）的纯数学回归。

只测 ``common/zzmi_cross_group_palette.py``，不需要 bpy、不需要 dump、不读工作区。

这套用例对应「参考实现里被论证过的那部分」：两块部件的骨骼表表达在各自根骨的
空间里，共用骨（锚点）给出空间换算，归一之后同一根 canonical 骨在两个部件里
的世界矩阵必须一致。测试用合成数据把 C 造出来，再要求实现把它解回去——解不回
去就是错，不是「近似」。
"""

import os
import sys
import unittest

import numpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import zzmi_cross_group_palette as cgp  # noqa: E402


def _affine44(rows):
    rows = numpy.asarray(rows, dtype=numpy.float64)
    matrix = numpy.zeros(rows.shape[:-2] + (4, 4), dtype=numpy.float64)
    matrix[..., :3, :] = rows
    matrix[..., 3, 3] = 1.0
    return matrix


def _rotation(axis, angle):
    axis = numpy.asarray(axis, dtype=numpy.float64)
    axis = axis / numpy.linalg.norm(axis)
    x, y, z = axis
    c, s = numpy.cos(angle), numpy.sin(angle)
    t = 1.0 - c
    return numpy.array([
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ], dtype=numpy.float64)


def _skeleton(count, rotation_axis=(0.0, 1.0, 0.0), rotation_angle=0.35, seed=7):
    """造一份「每根骨都动过」的骨骼表（形状 (count, 3, 4)）。

    每根骨的旋转各不相同，这样世界空间匹配才有唯一解——参考实现里正是靠这条
    丢掉「采集帧里没动」的骨。
    """
    rng = numpy.random.default_rng(seed)
    palette = numpy.zeros((count, 3, 4), dtype=numpy.float32)
    for index in range(count):
        axis = rng.normal(size=3)
        angle = rotation_angle + 0.01 * index
        rotation = _rotation(axis, angle)
        translation = numpy.array([
            rotation_axis[0] + 0.5 * (index % 7),
            rotation_axis[1] + 0.3 * (index % 5),
            rotation_axis[2] + 0.7 * (index % 3),
        ])
        palette[index, :, :3] = rotation.astype(numpy.float32)
        palette[index, :, 3] = translation.astype(numpy.float32)
    return palette


def _conversion(translation=(1.5, -0.25, 0.75), angle=0.6):
    matrix = numpy.eye(4)
    matrix[:3, :3] = _rotation((0.2, 0.9, -0.4), angle)
    matrix[:3, 3] = translation
    return matrix


def _world(palette, object_matrix):
    return numpy.einsum("ij,bjk->bik", object_matrix, _affine44(palette))


def test_draw_palette_uses_live_anchors_for_donor_slots():
    owner = {"draw_ib": "029c1f5a", "vg_offset": 2, "vg_count": 3}
    donor = {"draw_ib": "8c2fc05e", "vg_offset": 5, "vg_count": 3}
    separate = _conversion()
    body_rows = _skeleton(3)
    donor_rows = (numpy.linalg.inv(separate) @ _affine44(body_rows))[:, :3]
    samples = {
        owner["draw_ib"]: ("same-frame", numpy.eye(4), body_rows),
        donor["draw_ib"]: ("same-frame", separate, donor_rows),
    }

    rows, params = cgp.direct_draw_palette_tables({"components": []}, owner, [donor], samples, 8)

    assert params == (0, 2, 8, 0)
    assert rows[:8] == [2, 3, 0, 1, 0, 0, 0, 0]
    assert rows[8:12] == [5, 3, 0, 1]
    parent_slot, donor_slot = rows[12:14]
    assert parent_slot - 2 == donor_slot - 5

    live_conversion = _conversion((0.2, 0.8, -0.5), angle=0.2)
    live_rows = _skeleton(3, seed=8)
    live_donor_rows = numpy.linalg.inv(live_conversion) @ _affine44(live_rows)
    recovered = _affine44(live_rows)[parent_slot - 2] @ numpy.linalg.inv(live_donor_rows[donor_slot - 5])
    numpy.testing.assert_allclose(recovered @ live_donor_rows, _affine44(live_rows), atol=1e-7)


class FindWorldMatchesTest(unittest.TestCase):
    def test_shared_bones_are_matched_and_converted(self):
        shared = _skeleton(6)
        object_a = numpy.eye(4)
        # 子部件：把共用骨搬到自己根骨的空间，再给它两根独占骨。
        conversion = _conversion()
        own = _skeleton(2, seed=11)
        palette_b = numpy.concatenate(
            (own, (numpy.linalg.inv(conversion)[None] @ _affine44(shared))[:, :3, :])
        ).astype(numpy.float32)
        world_a = _world(shared, object_a)
        # 子部件的物体矩阵就是那个换算：world = C @ (C^-1 @ shared) = shared。
        world_b = _world(palette_b, conversion)

        pairs, all_count = cgp.find_world_matches(world_a, world_b)
        self.assertGreater(all_count, 0)
        matched_b = sorted(j for _, j in pairs)
        self.assertEqual(len(pairs), 6)
        # 独占骨对不上任何一根共用骨。
        self.assertNotIn(0, matched_b)
        self.assertNotIn(1, matched_b)

    def test_bones_that_do_not_move_are_discarded(self):
        # 三根骨完全重合（采集帧里没动）⇒ 一对多，全部丢掉。
        palette = numpy.zeros((3, 3, 4), dtype=numpy.float32)
        palette[:] = numpy.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], numpy.float32)
        pairs, all_count = cgp.find_world_matches(
            _world(palette, numpy.eye(4)), _world(palette, numpy.eye(4))
        )
        self.assertEqual(pairs, [])
        self.assertEqual(all_count, 9)

    def test_zero_slots_never_match(self):
        palette = _skeleton(4)
        palette[3] = 0.0
        pairs, _ = cgp.find_world_matches(
            _world(palette, numpy.eye(4)), _world(palette, numpy.eye(4))
        )
        self.assertNotIn(3, [i for i, _ in pairs])


class PlanCrossGroupPaletteTest(unittest.TestCase):
    def _two_components(self):
        shared = _skeleton(8)
        conversion = _conversion()
        parent = numpy.concatenate((shared, _skeleton(3, seed=3))).astype(numpy.float32)
        child = (
            numpy.linalg.inv(conversion)[None] @ _affine44(shared)
        )[:, :3, :].astype(numpy.float32)
        candidates = [
            {
                "ib_hash": "aaaa", "palette": parent, "object_matrix": numpy.eye(4),
                "pose_order": 0, "vertex_count": 400, "shape_keyed": False,
                "weighted_counts": numpy.arange(len(parent)) + 1,
            },
            {
                "ib_hash": "bbbb", "palette": child,
                # 骨骼表在子部件自己根骨的空间里（上面乘了 C^-1），物体矩阵就是 C。
                "object_matrix": conversion,
                "pose_order": 1, "vertex_count": 200, "shape_keyed": False,
                "weighted_counts": numpy.arange(len(child)) + 1,
            },
        ]
        return candidates, len(shared)

    def test_tree_anchor_and_canonical_ids(self):
        candidates, shared_count = self._two_components()
        profile, warnings = cgp.plan_cross_group_palette(candidates)
        self.assertEqual(warnings, [])
        profile = cgp.validate_profile(profile)

        self.assertEqual(profile["bones_count"], 19)
        self.assertEqual(profile["root_component_id"], 0)
        child = profile["components"][1]
        self.assertEqual(child["role"], cgp.ROLE_MERGED)
        self.assertEqual(child["parent_component_id"], 0)
        self.assertEqual(child["vg_offset"], 11)
        # 锚点落在两边都真用到的骨上（权重计数最大），且位置合法。
        self.assertLess(child["anchor_parent_slot"], 11)
        self.assertTrue(11 <= child["anchor_self_slot"] < 19)

        common = set(profile["components"][0]["vg_map"].values()) & set(child["vg_map"].values())
        self.assertEqual(len(common), shared_count)

    def test_normalized_palettes_agree_in_world_space(self):
        """归一之后，同一根 canonical 骨的世界矩阵在各部件里必须一致。"""
        candidates, _ = self._two_components()
        profile = cgp.validate_profile(cgp.plan_cross_group_palette(candidates)[0])
        pool = numpy.zeros((profile["bones_count"], 3, 4), dtype=numpy.float64)
        for component, candidate in zip(cgp.merged_components(profile), candidates):
            start = component["vg_offset"]
            pool[start:start + component["vg_count"]] = candidate["palette"]

        merged = cgp.merged_components(profile)
        worlds = []
        for component, candidate in zip(merged, candidates):
            palette = cgp.reference_palette(profile, pool, component["component_id"])
            worlds.append(
                numpy.einsum(
                    "ij,bjk->bik", candidate["object_matrix"], _affine44(palette)
                )
            )
        shared = set(merged[0]["vg_map"].values()) & set(merged[1]["vg_map"].values())
        self.assertTrue(shared)
        worst = max(
            float(numpy.abs(worlds[0][slot] - worlds[1][slot]).max()) for slot in shared
        )
        # 锚点链上用到的逆矩阵带来 float32 级别的残差；1e-4 是匹配阈值量级。
        self.assertLess(worst, cgp.MATCH_ROTATION_TOLERANCE)

    def test_component_without_shared_bones_is_skipped(self):
        candidates, _ = self._two_components()
        candidates.append({
            "ib_hash": "cccc", "palette": _skeleton(5, seed=99),
            "object_matrix": numpy.eye(4), "pose_order": 2, "vertex_count": 50,
            "shape_keyed": False, "weighted_counts": numpy.arange(5) + 1,
        })
        profile, warnings = cgp.plan_cross_group_palette(candidates)
        profile = cgp.validate_profile(profile)
        roles = {component["ib_hash"]: component["role"] for component in profile["components"]}
        # 一根骨都不共用的部件整块跳过（画面里的邦布、武器或别的角色）。
        self.assertNotIn("cccc", roles)
        self.assertTrue(any("cccc" in warning for warning in warnings))

    def test_shape_keyed_component_never_merges(self):
        candidates, _ = self._two_components()
        candidates[1]["shape_keyed"] = True
        profile, warnings = cgp.plan_cross_group_palette(candidates)
        self.assertEqual([c["ib_hash"] for c in cgp.merged_components(profile)], ["aaaa"])
        self.assertTrue(any("形态键" in warning for warning in warnings))


class ProfileContractTest(unittest.TestCase):
    def _profile(self):
        candidates, _ = PlanCrossGroupPaletteTest()._two_components()
        return cgp.validate_profile(cgp.plan_cross_group_palette(candidates)[0])

    def test_runtime_tables(self):
        profile = self._profile()
        table = cgp.runtime_component_table(profile)
        self.assertEqual(len(table), 2 * 4 * len(cgp.merged_components(profile)))
        # 每个合并部件占两个 uint4：骨骼范围 + 锚点。根本件指向自己。
        child = cgp.merged_components(profile)[1]
        self.assertEqual(table[0:4], [0, child["vg_offset"], 0, 1])
        self.assertEqual(table[12], child["anchor_parent_slot"])
        self.assertEqual(table[13], child["anchor_self_slot"])

        params = cgp.runtime_params(profile, child["component_id"])
        self.assertEqual(params[0], 1)
        self.assertEqual(params[1], 2)
        self.assertEqual(params[2], profile["bones_count"])
        self.assertEqual(params[3], 0)

    def test_vg_map_must_be_complete(self):
        profile = self._profile()
        broken = dict(profile)
        broken["components"] = [dict(component) for component in profile["components"]]
        broken["components"][1]["vg_map"].pop("0")
        with self.assertRaises(cgp.ZZMICrossGroupPaletteError):
            cgp.validate_profile(broken)

    def test_anchor_chain_must_reach_root(self):
        profile = self._profile()
        broken = dict(profile)
        broken["components"] = [dict(component) for component in profile["components"]]
        broken["components"][1]["parent_component_id"] = 1
        with self.assertRaises(cgp.ZZMICrossGroupPaletteError):
            cgp.validate_profile(broken)

    def test_pool_larger_than_game_limit_is_refused(self):
        candidates, _ = PlanCrossGroupPaletteTest()._two_components()
        candidates[0]["palette"] = _skeleton(cgp.MAX_POOL_BONES + 1)
        with self.assertRaises(cgp.ZZMICrossGroupPaletteError):
            cgp.plan_cross_group_palette(candidates)

    def test_build_private_remap_stays_inside_own_range(self):
        profile = self._profile()
        child = cgp.merged_components(profile)[1]
        remap = cgp.build_private_remap(child)
        offset, count = child["vg_offset"], child["vg_count"]
        self.assertTrue(remap)
        for canonical, slot in remap.items():
            self.assertLess(slot, profile["bones_count"])
            # 独占骨（canonical 落在自己的范围内）必须读自己那份，逐位精确。
            if offset <= canonical < offset + count:
                self.assertEqual(slot, canonical)


class RuntimeShaderTest(unittest.TestCase):
    def test_shader_sources_match_component_table_layout(self):
        import_source = cgp.IMPORT_SHADER_SOURCE
        palette_source = cgp.PALETTE_SHADER_SOURCE

        # 两张表、两个入口，绑定槽位必须和 ini 里声明的一致。
        self.assertIn("Buffer<uint4> Components : register(t1);", import_source)
        self.assertIn("Buffer<uint4> Components : register(t1);", palette_source)
        self.assertIn("float4 ComponentBoneRows[3072];", import_source)
        self.assertIn("RWBuffer<float4> Pool : register(u0);", import_source)
        self.assertIn("Buffer<float4> Pool : register(t0);", palette_source)
        self.assertIn("uint4 component = Components[params.x * 2];", import_source)

        # 锚点链：edge = P_parent[anchor] * inverse(P_self[anchor])，逐级走到根。
        self.assertIn("uint4 anchor = Components[k * 2 + 1];", palette_source)
        self.assertIn(
            "Affine edge = Mul(LoadSlot(anchor.x), Inverse(LoadSlot(anchor.y)));",
            palette_source,
        )
        self.assertIn("Affine to_self = Mul(Inverse(ToRoot(params.x)), ToRoot(owner));",
                      palette_source)


class PlanFromPartsTest(unittest.TestCase):
    """导出侧真正会调的那条路：手上只有 palette / transform / 权重。"""

    def _parts(self):
        shared = _skeleton(8)
        conversion = _conversion()
        parent_palette = numpy.concatenate((shared, _skeleton(3, seed=3)))
        child_palette = (numpy.linalg.inv(conversion)[None] @ _affine44(shared))[:, :3, :]
        # load_palette 的产物是 (n, 12) 的扁平行向量，这里刻意用那个形状喂进去。
        return {
            "11111111": {
                "palette": parent_palette.reshape(-1, 12),
                "transform": numpy.eye(4).reshape(16),
                "slot_weights": [10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1],
                "vertex_count": 900,
            },
            "22222222": {
                "palette": child_palette.astype(numpy.float32).reshape(-1, 12),
                "transform": conversion.reshape(16),
                "slot_weights": [3, 2, 1, 1, 1, 1, 1, 1],
                "vertex_count": 300,
            },
        }

    def test_vg_maps_are_canonical_and_offsets_are_contiguous(self):
        vg_maps, vg_offsets, profile, warnings = cgp.plan_from_parts(self._parts())
        self.assertEqual(warnings, [])
        self.assertEqual(vg_offsets, {"11111111": 0, "22222222": 11})
        self.assertEqual(len(vg_maps["11111111"]), 11)
        self.assertEqual(len(vg_maps["22222222"]), 8)
        # 共用骨落在同一批 canonical 号上，独占骨各占自己的号。
        shared_child = {vg_maps["22222222"][local] for local in range(8)}
        self.assertTrue(shared_child & set(vg_maps["11111111"].values()))
        self.assertEqual(profile["bones_count"], 19)

    def test_parent_choice_follows_vertex_count(self):
        parts = self._parts()
        # 把子部件说成更大的网格，根应该翻过去。
        parts["22222222"]["vertex_count"] = 5000
        _, vg_offsets, profile, _ = cgp.plan_from_parts(parts)
        self.assertEqual(profile["root_component_id"], 0)
        self.assertEqual(list(vg_offsets), ["22222222", "11111111"])

    def test_part_without_transform_gets_private_block(self):
        parts = self._parts()
        parts["33333333"] = {"palette": _skeleton(4).reshape(-1, 12), "transform": None}
        vg_maps, vg_offsets, profile, warnings = cgp.plan_from_parts(parts)
        self.assertTrue(any("33333333" in warning for warning in warnings))
        # 换算不了的部件不硬塞进合并树，但必须有自己一段私有槽位，本地骨号原样。
        self.assertEqual(vg_offsets["33333333"], 19)
        self.assertEqual(vg_maps["33333333"], {0: 19, 1: 20, 2: 21, 3: 22})
        self.assertEqual(profile["pool_bones"], 23)

    def test_row_vector_transform_from_parser_is_transposed(self):
        parts = self._parts()
        # parse_object_transform 返回行向量式 4×4 摊平成的 16 个 float。
        parts["22222222"]["transform"] = _conversion().T.reshape(16)
        vg_maps, vg_offsets, _, warnings = cgp.plan_from_parts(parts)
        self.assertEqual(warnings, [])
        self.assertEqual(vg_offsets, {"11111111": 0, "22222222": 11})
        self.assertEqual(len(vg_maps["22222222"]), 8)

    def test_weights_shorter_than_palette_are_padded(self):
        parts = self._parts()
        parts["11111111"]["slot_weights"] = [1, 1]
        vg_maps, _, _, _ = cgp.plan_from_parts(parts)
        self.assertEqual(len(vg_maps["11111111"]), 11)

    def test_non_finite_weights_no_longer_veto_the_whole_plan(self):
        # 实测现场：脸部那颗 1 骨部件（dbbf0aee）的 Blend 权重是 NaN，于是
        # weighted.sum() 也是 NaN，int(round(NaN)) 抛 ValueError —— 整池方案当场
        # 算不出来，整只角色退回历史分组，看上去就是「带脸部导入就退回非跨组合并」。
        parts = self._parts()
        parts["33333333"] = {
            "palette": _skeleton(1, seed=9).reshape(-1, 12),
            "transform": numpy.eye(4).reshape(16),
            "slot_weights": [float("nan")],
            "vertex_count": None,
        }
        vg_maps, _, profile, warnings = cgp.plan_from_parts(parts)
        self.assertTrue(
            any("33333333" in warning and "非有限值" in warning for warning in warnings)
        )
        # 计数按 0 算只影响挑锚点与选根，槽位照占：池尾那一段就是它。
        self.assertEqual(list(vg_maps["33333333"].values()), [profile["pool_bones"] - 1])

    def test_palette_with_non_finite_matrix_gets_private_block(self):
        parts = self._parts()
        broken = _skeleton(4, seed=11).reshape(-1, 12).astype(numpy.float32)
        broken[0][3] = float("nan")
        parts["44444444"] = {"palette": broken, "transform": numpy.eye(4).reshape(16)}
        vg_maps, vg_offsets, profile, warnings = cgp.plan_from_parts(parts)
        self.assertTrue(
            any("44444444" in warning and "NaN" in warning for warning in warnings)
        )
        # 骨骼矩阵带 NaN 认不出身份，但也不许把整池否决：池尾私有段。
        self.assertEqual(vg_offsets["44444444"], 19)
        self.assertEqual(vg_maps["44444444"], {0: 19, 1: 20, 2: 21, 3: 22})
        self.assertEqual(profile["pool_bones"], 23)


if __name__ == "__main__":
    unittest.main()
