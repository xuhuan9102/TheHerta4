"""TTL 协议解析只有一份实现：两个后处理节点都调 common.ttl_protocol。"""

import unittest

from common.ttl_protocol import build_transparency_draw_lines, parse_ttl_draw_args


class ParseTtlDrawArgsTests(unittest.TestCase):
    def test_reads_three_adjacent_params(self):
        lines = [
            "Resource\\TTL\\TransparencyTex = ref Resource_TTLMap_A",
            "$\\TTL\\_1 = 56376",
            "$\\TTL\\_2 = 0",
            "$\\TTL\\_3 = 3",
            "run = CommandList\\TTL\\Draw",
        ]
        self.assertEqual(parse_ttl_draw_args(lines, 4), (56376, 0, 3))

    def test_stops_at_mesh_anchor(self):
        lines = [
            "$\\TTL\\_1 = 1",
            "$\\TTL\\_2 = 2",
            "$\\TTL\\_3 = 3",
            "; [mesh:LOD0.aaaa-1-0.Other]",
            "$\\TTL\\_1 = 9",
            "run = CommandList\\TTL\\Draw",
        ]
        self.assertIsNone(parse_ttl_draw_args(lines, 5))

    def test_rejects_non_positive_count(self):
        lines = [
            "$\\TTL\\_1 = 0",
            "$\\TTL\\_2 = 0",
            "$\\TTL\\_3 = 0",
            "run = CommandList\\TTL\\Draw",
        ]
        self.assertIsNone(parse_ttl_draw_args(lines, 3))

    def test_block_start_index_is_respected(self):
        lines = [
            "$\\TTL\\_1 = 7",
            "$\\TTL\\_2 = 8",
            "$\\TTL\\_3 = 9",
            "; boundary",
            "$\\TTL\\_1 = 1",
            "$\\TTL\\_2 = 2",
            "$\\TTL\\_3 = 3",
            "run = CommandList\\TTL\\Draw",
        ]
        self.assertEqual(parse_ttl_draw_args(lines, 7, 4), (1, 2, 3))

    def test_nodes_do_not_keep_private_copies(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for name in ("node_postprocess_shapekey.py", "node_postprocess_uv_offset.py"):
            source = (root / "blueprint" / name).read_text(encoding="utf-8")
            self.assertNotIn("def _parse_ttl_draw_args", source, name)
            self.assertIn("from ..common.ttl_protocol import parse_ttl_draw_args", source, name)
            self.assertIn("parse_ttl_draw_args(lines, line_index", source, name)


class BuildTransparencyDrawLinesTests(unittest.TestCase):
    """「透明贴图」节点的 TTL 参数组。"""

    def _info(self, mode, **overrides):
        info = {
            "mode": mode,
            "mask_channel": 3,
            "mask_invert": True,
            "alpha": 0.5,
            "brightness": 1.5,
        }
        info.update(overrides)
        return info

    def test_texture_mode_writes_alpha_one(self):
        lines = build_transparency_draw_lines(
            self._info("TEXTURE"), "Resource_Diffuse_A", 100, 20, 3,
        )
        self.assertIn(r"$\TTL\alpha = 1", lines)
        self.assertLess(
            lines.index(r"$\TTL\mask_invert = 1"),
            lines.index(r"$\TTL\alpha = 1"),
        )
        self.assertLess(
            lines.index(r"$\TTL\alpha = 1"),
            lines.index(r"$\TTL\V = 1.5"),
        )
        self.assertEqual(lines[-1], r"run = CommandList\TTL\Draw")

    def test_alpha_mode_uses_node_value(self):
        lines = build_transparency_draw_lines(
            self._info("ALPHA", alpha=0.25), "", 100, 20, 0,
        )
        self.assertIn(r"$\TTL\alpha = 0.25", lines)
        self.assertIn(r"Resource\TTL\TransparencyTex = null", lines)
