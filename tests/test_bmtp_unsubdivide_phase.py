# -*- coding: utf-8 -*-
"""反细分「几何相位」回归测试（toolkit/bmtp_mesh_tools.py，纯 Python，不需要 Blender）。

背景（2026-09-15 Blender 5.0.1 实测）：原相位由 _us_assign_grid_coords 的链行走给出
——从每条边界边各自 r=0 向岛内走，起点/方向取决于 bmesh 枚举顺序 ⇒ **同一几何、
不同造法结果相反**：

    直接建 9x9     ：25 个理想格点全在、残留 16 个非格点
    5x5 细分出来的 9x9：内圈 9 个理想格点被删、36 个非格点全留

本轮换成「几何量化」：坐标字典序 + 几何角点定原点、孤岛局部坐标系（不是世界 XY）
量化出 (r,c)，与元素顺序无关。被测：
- _us_boundary_corner_verts / _us_canonical_origin
- _us_lattice_frame / _us_grid_coords_by_geometry
- _us_straight_unsubdivide 的接线（几何优先、链行走兜底，源码级锁）
"""
import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "toolkit" / "bmtp_mesh_tools.py"
PKG = "_bmtp_unsubdivide_phase_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _AnyProps:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _AnyTypes:
    def __getattr__(self, name):
        return type(name, (object,), {})


_install_module("bpy", props=_AnyProps(), types=_AnyTypes())
# 几何相位是纯坐标运算，不需要真 bmesh；模块级 import bmesh 才要这个桩
_install_module(
    "bmesh",
    from_edit_mesh=lambda _mesh: None,
    update_edit_mesh=lambda _mesh, **_kwargs: None,
    ops=types.SimpleNamespace(),
)
_install_module(f"{PKG}.utils.color_attribute_utils", read_color_attribute_data=None, write_color_attribute_data=None)
_install_module(f"{PKG}.utils.vertex_color_utils", convert_color_srgb_to_linear=None, ensure_color_attribute=None)

_spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.bmtp_mesh_tools", MODULE_PATH)
MOD = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = MOD
_spec.loader.exec_module(MOD)


# ---------------------------------------------------------------------------
# 最小向量 / bmesh 假件（只覆盖几何相位需要的接口）
# ---------------------------------------------------------------------------
class Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __sub__(self, other):
        return Vec(self.x - other.x, self.y - other.y, self.z - other.z)

    def __add__(self, other):
        return Vec(self.x + other.x, self.y + other.y, self.z + other.z)

    def __mul__(self, scalar):
        return Vec(self.x * scalar, self.y * scalar, self.z * scalar)

    __rmul__ = __mul__

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other):
        return Vec(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    @property
    def length(self):
        return math.sqrt(self.dot(self))

    def normalized(self):
        length = self.length
        if length <= 0.0:
            return Vec(0.0, 0.0, 0.0)
        return Vec(self.x / length, self.y / length, self.z / length)


class FakeVert:
    def __init__(self, co):
        self.co = co
        self.link_edges = []
        self.is_valid = True

    @property
    def key(self):
        return (round(self.co.x, 1), round(self.co.y, 1), round(self.co.z, 1))


class FakeEdge:
    def __init__(self, a, b):
        self.verts = [a, b]
        a.link_edges.append(self)
        b.link_edges.append(self)
        self.link_faces = []

    def other_vert(self, vert):
        if self.verts[0] is vert:
            return self.verts[1]
        if self.verts[1] is vert:
            return self.verts[0]
        return None


class FakeBM:
    def __init__(self):
        self.verts = []
        self.edges = []


def _rotate(vec, degrees):
    angle = math.radians(degrees)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return Vec(vec.x * cos_a - vec.y * sin_a, vec.x * sin_a + vec.y * cos_a, vec.z)


def build_grid(side_verts, spacing=1.0, rotate=0.0, shift=(0.0, 0.0), reverse=False):
    """side_verts x side_verts 四边形网格；reverse=True 反转顶点/边的插入顺序。

    边界边按**拓扑**判定（某条边两端都在最外圈），不用 x/y 极值——那样对旋转网格无效。
    """
    bm = FakeBM()
    verts = {}
    order = list(range(side_verts))
    if reverse:
        order = list(reversed(order))
    for i in order:
        for j in order:
            co = Vec(j * spacing, i * spacing, 0.0)
            if rotate:
                co = _rotate(co, rotate)
            co = Vec(co.x + shift[0], co.y + shift[1], co.z)
            vert = FakeVert(co)
            verts[(i, j)] = vert
            bm.verts.append(vert)
    pairs = []
    for i in range(side_verts):
        for j in range(side_verts):
            if j + 1 < side_verts:
                pairs.append(((i, j), (i, j + 1)))
            if i + 1 < side_verts:
                pairs.append(((i, j), (i + 1, j)))
    if reverse:
        pairs = list(reversed(pairs))
    boundary = []
    for (i, j), (i2, j2) in pairs:
        edge = FakeEdge(verts[(i, j)], verts[(i2, j2)])
        bm.edges.append(edge)
        outermost = side_verts - 1
        if (i == 0 and i2 == 0) or (i == outermost and i2 == outermost):
            boundary.append(edge)
        elif (j == 0 and j2 == 0) or (j == outermost and j2 == outermost):
            boundary.append(edge)
    return bm, boundary, verts


def kept_lattice(coords, step):
    return {
        vert.key for vert, (_chain, r, c) in coords.items()
        if r % step == 0 and c % step == 0
    }


def kept_keys(coords, step):
    """保留格点的 (r, c) 集合（注意：不是顶点坐标键）。"""
    return {
        (r, c) for (_chain, r, c) in coords.values()
        if r % step == 0 and c % step == 0
    }


def normalized_lattice(kept, extent):
    """把格点键归一化到 [0,1]，便于比较不同间距/尺寸的同构网格。"""
    return {(round(x / extent, 4), round(y / extent, 4)) for x, y, _z in kept}


class CornersAndOriginTests(unittest.TestCase):
    def test_corners_are_the_four_grid_corners(self):
        _bm, boundary_edges, verts = build_grid(9, spacing=1.0)
        corners = MOD._us_boundary_corner_verts(boundary_edges)
        self.assertEqual(
            {vert.key for vert in corners},
            {(0.0, 0.0, 0.0), (8.0, 0.0, 0.0), (0.0, 8.0, 0.0), (8.0, 8.0, 0.0)},
        )

    def test_origin_is_a_lattice_corner_not_a_mid_edge_vert(self):
        _bm, boundary_edges, _verts = build_grid(9, spacing=1.0)
        origin = MOD._us_canonical_origin(boundary_edges)
        self.assertEqual(origin.key, (0.0, 0.0, 0.0))


class GeometricPhaseTests(unittest.TestCase):
    def test_regular_grid_keys_are_the_fine_lattice(self):
        bm, boundary_edges, verts = build_grid(9, spacing=1.0)
        coords = MOD._us_grid_coords_by_geometry(bm, boundary_edges)
        self.assertIsNotNone(coords)
        for (i, j), vert in verts.items():
            self.assertEqual(coords[vert][1:], (i, j), (i, j))
        self.assertEqual(len(kept_lattice(coords, 2)), 25)  # 5x5 理想格点

    def test_half_spacing_grid_uses_same_lattice_as_coarse_grid(self):
        """5x5 细分一次（间距 0.5，范围 0..4）与直接建 9x9（间距 1.0，范围 0..8）
        必须给出同一套**归一化**理想格点（两者表示同一个 5x5 粗网格）。"""
        fine_bm, fine_boundary, _fine = build_grid(9, spacing=0.5)
        fine_kept = kept_lattice(MOD._us_grid_coords_by_geometry(fine_bm, fine_boundary), 2)
        coarse_bm, coarse_boundary, _coarse = build_grid(9, spacing=1.0)
        coarse_kept = kept_lattice(MOD._us_grid_coords_by_geometry(coarse_bm, coarse_boundary), 2)
        self.assertEqual(normalized_lattice(fine_kept, 4.0), normalized_lattice(coarse_kept, 8.0))
        self.assertEqual(len(fine_kept), 25)
        self.assertEqual(len(coarse_kept), 25)

    def test_result_is_independent_of_element_order(self):
        """核心回归：同一几何、反转顶点与边的插入顺序，相位必须逐点一致。"""
        bm_a, boundary_a, verts_a = build_grid(9, spacing=0.5)
        bm_b, boundary_b, verts_b = build_grid(9, spacing=0.5, reverse=True)
        coords_a = MOD._us_grid_coords_by_geometry(bm_a, boundary_a)
        coords_b = MOD._us_grid_coords_by_geometry(bm_b, boundary_b)
        self.assertIsNotNone(coords_a)
        self.assertIsNotNone(coords_b)
        by_key_a = {vert.key: coords_a[vert][1:] for vert in coords_a}
        by_key_b = {vert.key: coords_b[vert][1:] for vert in coords_b}
        self.assertEqual(by_key_a, by_key_b)
        self.assertEqual(kept_lattice(coords_a, 2), kept_lattice(coords_b, 2))
        self.assertEqual(len(kept_lattice(coords_a, 2)), 25)

    def test_frame_is_local_not_world_xy(self):
        """旋转 37° + 平移：岛局部坐标系下仍是**整数格点**（世界 XY 量化会得到分数）。

        选中的角点可能不是网格 (0,0)，键值因此整体平移/镜像——所以断言的是"9x9 完整
        整数格点"这一不变量（r 取值集合 × c 取值集合 == 键集合），不是具体数值。
        """
        bm, boundary_edges, verts = build_grid(9, spacing=0.5, rotate=37.0, shift=(11.0, -4.0))
        coords = MOD._us_grid_coords_by_geometry(bm, boundary_edges)
        self.assertIsNotNone(coords)
        keys = {coords[vert][1:] for vert in coords}
        self.assertEqual(len(keys), 81)
        rows = {r for r, _c in keys}
        cols = {c for _r, c in keys}
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(cols), 9)
        self.assertEqual({(r, c) for r in rows for c in cols}, keys)
        kept = kept_keys(coords, 2)
        self.assertEqual(len(kept), 25)
        kept_rows = {r for r, _c in kept}
        kept_cols = {c for _r, c in kept}
        self.assertEqual({(r, c) for r in kept_rows for c in kept_cols}, kept)

    def test_irregular_island_falls_back(self):
        """非规则岛（残差超限）必须返回 None，让调用方回退链行走，绝不硬套相位。"""
        bm = FakeBM()
        verts = [FakeVert(Vec(x * 0.31 + (0.07 if x % 2 else 0.0), y * 0.17, 0.0))
                 for y in range(5) for x in range(5)]
        bm.verts.extend(verts)
        for i in range(5):
            for j in range(5):
                if j + 1 < 5:
                    bm.edges.append(FakeEdge(verts[i * 5 + j], verts[i * 5 + j + 1]))
                if i + 1 < 5:
                    bm.edges.append(FakeEdge(verts[i * 5 + j], verts[(i + 1) * 5 + j]))
        self.assertIsNone(MOD._us_grid_coords_by_geometry(bm, list(bm.edges)))

    def test_degenerate_frame_returns_none(self):
        bm = FakeBM()
        a, b, c = FakeVert(Vec(0, 0, 0)), FakeVert(Vec(1, 0, 0)), FakeVert(Vec(2, 0, 0))
        bm.verts.extend([a, b, c])
        e1, e2 = FakeEdge(a, b), FakeEdge(b, c)
        bm.edges.extend([e1, e2])
        self.assertIsNone(MOD._us_lattice_frame(bm, [e1, e2]))  # 共线边界：没有第二条轴


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MODULE_PATH.read_text(encoding="utf-8")
        start = cls.source.index("def _us_straight_unsubdivide")
        cls.body = cls.source[start:]

    def test_geometry_first_then_chain_walk_fallback(self):
        geometry_pos = self.body.index("coords = _us_grid_coords_by_geometry(bm, boundary_edges)")
        fallback_pos = self.body.index("coords = _us_assign_grid_coords(bm, boundary_edges, set(boundary_edges))")
        self.assertLess(geometry_pos, fallback_pos)

    def test_chain_walk_is_still_available_as_fallback(self):
        self.assertIn("def _us_assign_grid_coords(", self.source)
        self.assertIn("def _us_grid_coords_by_geometry(", self.source)
        self.assertIn("def _us_lattice_frame(", self.source)
        self.assertIn("def _us_boundary_corner_verts(", self.source)


if __name__ == "__main__":
    unittest.main()
