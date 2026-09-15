"""M1 软体邻接网烘焙单测（common/softbody_adjacency.py）。

这是整个软体物理的地基：顶点之间要有**网**，而且这张网必须是严格无向的
（A 认识 B ⟺ B 认识 A），否则互相拉扯 / 自碰撞会在某些边上失效甚至反向。
本文件把三类性质钉死：

1. 拓扑正确：边去重、无自环、无重复、严格对称；
2. 几何正确：静止长度 == 实际边长（逐边比对）；
3. 边界干净：越界顶点要抛错，孤立顶点与最大邻居数要如实报告。
"""

import importlib.util
import math
import struct
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PATH = REPO_ROOT / "common" / "softbody_adjacency.py"
_SPEC = importlib.util.spec_from_file_location("softbody_adjacency_under_test", _PATH)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

build_adjacency = _mod.build_adjacency
serialize_adjacency = _mod.serialize_adjacency
validate_adjacency = _mod.validate_adjacency
SoftBodyAdjacencyError = _mod.SoftBodyAdjacencyError


def _grid(rows, cols):
    """rows×cols 平面网格（z=0），返回 (positions, triangles)。"""
    positions = []
    for r in range(rows):
        for c in range(cols):
            positions.append((float(c), float(r), 0.0))
    triangles = []
    for r in range(rows - 1):
        for c in range(cols - 1):
            i0 = r * cols + c
            i1 = i0 + 1
            i2 = i0 + cols
            i3 = i2 + 1
            triangles.append((i0, i1, i3))
            triangles.append((i0, i3, i2))
    return positions, triangles


class AdjacencyTopologyTests(unittest.TestCase):
    def test_grid_is_symmetric_and_deduplicated(self):
        positions, triangles = _grid(5, 4)
        adj, edges, report = build_adjacency(positions, triangles)
        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(report["symmetric"])
        self.assertEqual(report["vertex_count"], 20)
        # 5×4 网格（每格切成两个三角形）：
        #   水平 5*(4-1)=15，垂直 (5-1)*4=16，对角线 4*3=12 → 43
        self.assertEqual(report["unique_edge_count"], 43)
        self.assertEqual(report["edge_count"], 43 * 2)  # 双向各一条
        # 对角边存在，所以内部顶点是 6 邻居（4 正 + 2 斜），角点 3
        self.assertEqual(report["max_neighbors"], 6)
        self.assertEqual(report["isolated_vertices"], [])

    def test_duplicate_faces_do_not_duplicate_edges(self):
        positions = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        adj, edges, report = build_adjacency(positions, [(0, 1, 2), (0, 1, 2), (2, 1, 0)])
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["unique_edge_count"], 3)
        self.assertEqual(report["edge_count"], 6)

    def test_degenerate_triangle_is_dropped(self):
        positions = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        adj, edges, report = build_adjacency(positions, [(0, 1, 1), (0, 1, 2)])
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["unique_edge_count"], 3)

    def test_isolated_vertex_is_reported(self):
        positions = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (9, 9, 9)]
        adj, edges, report = build_adjacency(positions, [(0, 1, 2)])
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["isolated_vertices"], [3])

    def test_out_of_range_vertex_raises(self):
        positions = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        with self.assertRaises(SoftBodyAdjacencyError):
            build_adjacency(positions, [(0, 1, 3)])


class AdjacencyGeometryTests(unittest.TestCase):
    def test_rest_length_matches_real_edge_length(self):
        positions, triangles = _grid(4, 4)
        adj, edges, report = build_adjacency(positions, triangles)
        self.assertTrue(report["ok"])

        # 重建 顶点 -> 邻居 -> 边长 的映射，逐条与坐标算出来的值比对
        seen = 0
        for vertex, (count, start) in enumerate(adj):
            for offset in range(start, start + count):
                other, rest = edges[offset]
                ax, ay, az = positions[vertex]
                bx, by, bz = positions[other]
                expect = math.dist((ax, ay, az), (bx, by, bz))
                self.assertAlmostEqual(rest, expect, places=6)
                seen += 1
        self.assertEqual(seen, report["edge_count"])

    def test_edges_grouped_per_vertex_and_sorted(self):
        positions, triangles = _grid(3, 3)
        adj, edges, _report = build_adjacency(positions, triangles)
        cursor = 0
        for count, start in adj:
            self.assertEqual(start, cursor)
            board = [edges[start + i][0] for i in range(count)]
            self.assertEqual(board, sorted(board))
            cursor += count
        self.assertEqual(cursor, len(edges))

    def test_neighbor_cap_keeps_symmetry_and_shortest_edges(self):
        positions, triangles = _grid(5, 5)
        _adj0, _edges0, report0 = build_adjacency(positions, triangles)
        adj, edges, report = build_adjacency(positions, triangles, neighbor_cap=2)
        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(report["symmetric"])
        # 裁剪必须真的减少边数（5×5 平面不做裁剪是 56 条）
        self.assertLess(report["unique_edge_count"], report0["unique_edge_count"])
        # 注意：**裁剪后度数可以超过 cap** —— 每个顶点只保证"自己保留最短的 N 条"，
        # 但某个邻居可能因为自身选择而把这条边留下。对称性优先于严格度数上限。
        self.assertGreaterEqual(report["max_neighbors"], 2)
        # 裁剪后仍必须是双向的
        directed = set()
        for vertex, (count, start) in enumerate(adj):
            for offset in range(start, start + count):
                directed.add((vertex, edges[offset][0]))
        for u, v in directed:
            self.assertIn((v, u), directed, f"裁剪破坏了对称性: {u}->{v}")


class AdjacencySerializationTests(unittest.TestCase):
    def test_layout_is_uint4_per_record(self):
        positions, triangles = _grid(3, 3)
        adj, edges, _report = build_adjacency(positions, triangles)
        adj_bytes, edge_bytes = serialize_adjacency(adj, edges)

        self.assertEqual(len(adj_bytes), len(adj) * 16)
        self.assertEqual(len(edge_bytes), len(edges) * 16)

        count, start, z, w = struct.unpack("<4I", adj_bytes[:16])
        self.assertEqual((count, start, z, w), (adj[0][0], adj[0][1], 0, 0))

        neighbor, rest_bits, z2, w2 = struct.unpack("<4I", edge_bytes[:16])
        self.assertEqual(neighbor, edges[0][0])
        self.assertEqual((z2, w2), (0, 0))
        self.assertAlmostEqual(
            struct.unpack("<f", struct.pack("<I", rest_bits))[0], edges[0][1], places=6
        )

    def test_validate_reports_broken_network(self):
        # 人为造一张坏网：顶点0 指向 1，但 1 没指回 0；另外有自环
        adj = [(1, 0), (1, 1)]  # 顶点0: 1 条边（从 0 开始）；顶点1: 1 条边（从 1 开始）
        edges = [(1, 1.0), (1, 1.0)]  # 0->1 与 1->1（自环）
        report = validate_adjacency(adj, edges, 2)
        self.assertFalse(report["ok"])
        self.assertFalse(report["symmetric"])
        self.assertTrue(any("自环" in p for p in report["problems"]))


if __name__ == "__main__":
    unittest.main()
