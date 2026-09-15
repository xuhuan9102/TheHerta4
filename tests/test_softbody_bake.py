"""M1-2 字节层烘焙单测（common/softbody_bake.py）。

这一层把"导出的交错顶点缓冲 + 索引缓冲"变成求解器要的邻接 buffer。
它是最容易出错、也最好验证的一层：**字节进、字节出**，所以这里用真实布局
（stride 40 的交错缓冲、u32 索引）对着写一遍，并逐个检查边界情况。

边界情况都是"宁可报错也不要静默产出"：
  * stride 非法 / 位置偏移放不下 xyz
  * 索引缓冲长度不是元素大小整数倍
  * 索引数不是 3 的倍数（组不成三角形）
  * 索引越界（交给下层 build_adjacency 抛 SoftBodyAdjacencyError）
"""

import importlib.util
import struct
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PATH = REPO_ROOT / "common" / "softbody_bake.py"
_SPEC = importlib.util.spec_from_file_location("softbody_bake_under_test", _PATH)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

bake_softbody_adjacency = _mod.bake_softbody_adjacency
parse_positions = _mod.parse_positions
parse_indices = _mod.parse_indices
SoftBodyBakeError = _mod.SoftBodyBakeError
SoftBodyAdjacencyError = _mod.SoftBodyAdjacencyError


def _pack_positions(points, stride=40, offset=0, filler=b"\x00"):
    """把 xyz 列表打成 stride=40 的交错缓冲（12 字节位置 + 28 字节填充）。"""
    payload = bytearray()
    pad = stride - 12 - offset
    for x, y, z in points:
        payload += filler * offset
        payload += struct.pack("<3f", x, y, z)
        payload += filler * pad
    return bytes(payload)


def _pack_indices(indices, fmt="u32"):
    code = "I" if fmt == "u32" else "H"
    return struct.pack("<%d%s" % (len(indices), code), *indices)


class ParseTests(unittest.TestCase):
    def test_parse_positions_reads_xyz_from_interleaved(self):
        payload = _pack_positions([(1.0, 2.0, 3.0), (-4.0, 5.5, 6.25)])
        points = parse_positions(payload, 40)
        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[0][0], 1.0, places=6)
        self.assertAlmostEqual(points[1][2], 6.25, places=6)

    def test_parse_positions_rejects_bad_stride(self):
        with self.assertRaises(SoftBodyBakeError):
            parse_positions(b"\x00" * 40, 0)
        with self.assertRaises(SoftBodyBakeError):
            parse_positions(b"\x00" * 40, 8)  # 放不下 12 字节

    def test_parse_indices_supports_u32_and_u16(self):
        self.assertEqual(parse_indices(_pack_indices([0, 1, 2]), "u32"), [0, 1, 2])
        self.assertEqual(parse_indices(_pack_indices([3, 4, 5], "u16"), "u16"), [3, 4, 5])

    def test_parse_indices_rejects_ragged_and_unknown(self):
        with self.assertRaises(SoftBodyBakeError):
            parse_indices(b"\x00\x01\x02", "u32")
        with self.assertRaises(SoftBodyBakeError):
            parse_indices(_pack_indices([0, 1, 2]), "u8")


class BakeTests(unittest.TestCase):
    def test_bake_single_triangle(self):
        positions = _pack_positions([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
        indices = _pack_indices([0, 1, 2])
        adj_bytes, edge_bytes, report = bake_softbody_adjacency(positions, 40, indices)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["vertex_count"], 3)
        self.assertEqual(report["unique_edge_count"], 3)
        self.assertEqual(len(adj_bytes), 3 * 16)
        self.assertEqual(len(edge_bytes), 6 * 16)  # 双向

    def test_bake_rejects_ragged_triangle_list(self):
        positions = _pack_positions([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
        indices = _pack_indices([0, 1, 2, 0])
        with self.assertRaises(SoftBodyBakeError):
            bake_softbody_adjacency(positions, 40, indices)

    def test_bake_propagates_out_of_range_index(self):
        positions = _pack_positions([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
        indices = _pack_indices([0, 1, 9])
        with self.assertRaises(SoftBodyAdjacencyError):
            bake_softbody_adjacency(positions, 40, indices)

    def test_bake_respects_position_offset(self):
        # 位置不在缓冲开头（offgrid 布局）：offset=4 时也能正确取到 xyz
        points = [(1.5, 2.5, -3.5), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]
        payload = _pack_positions(points, stride=40, offset=4)
        got = parse_positions(payload, 40, 4)
        for want, actual in zip(points, got):
            for a, b in zip(want, actual):
                self.assertAlmostEqual(a, b, places=6)

    def test_bake_matches_real_export_layout_sizes(self):
        """对着真实导出的字节数关系做一次规模自检：顶点 N / 索引 3N 时能对上。"""
        grid = []
        size = 8
        for r in range(size):
            for c in range(size):
                grid.append((float(c), float(r), 0.0))
        tris = []
        for r in range(size - 1):
            for c in range(size - 1):
                i0 = r * size + c
                tris += [i0, i0 + 1, i0 + size + 1, i0, i0 + size + 1, i0 + size]
        positions = _pack_positions(grid)
        indices = _pack_indices(tris)

        adj_bytes, edge_bytes, report = bake_softbody_adjacency(positions, 40, indices)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["vertex_count"], size * size)
        self.assertEqual(len(adj_bytes), size * size * 16)
        self.assertEqual(len(edge_bytes), report["edge_count"] * 16)


if __name__ == "__main__":
    unittest.main()
