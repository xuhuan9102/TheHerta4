"""TTL 协议解析只有一份实现：两个后处理节点都调 common.ttl_protocol。"""

import unittest

from common.ttl_protocol import parse_ttl_draw_args


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
