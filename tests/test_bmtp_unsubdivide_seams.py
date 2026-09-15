"""反细分算子「多物体编辑」缝合线保护（toolkit/bmtp_mesh_tools.py）的回归测试。

背景（2026-09-15 审查实机复现，Blender 5.0.1）：

``bpy.ops.mesh.mark_seam(clear=True)`` 与 ``bpy.ops.uv.seams_from_islands`` 都是
**全局**算子——它们作用于编辑模式内的**所有**对象；而反细分起初只快照/还原活动网格
的 seam，于是同处编辑模式的其它对象 seam 被 clear 后再无还原路径：

    seams before op: A=12 B=12 → seams after op: A=12 B=0（B 丢 12 条）

被测对象是两个纯 Python 辅助函数（不依赖 Blender）：
- ``_us_snapshot_other_edit_meshes_seams(context, active_mesh)``：按
  ``objects_in_mode_unique_data`` 遍历，跳过活动网格、按网格身份去重、取不到编辑态
  bmesh 的网格跳过；
- ``_us_restore_other_edit_meshes_seams(snapshots)``：按顶点索引对写回（写回范围与
  ``_us_restore_seams`` 一致：快照里没有的边一律清 seam）。

另有源码级回归，锁「快照必须在全局 seam 算子之前、还原必须在任何提前 return 之前」。
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "toolkit" / "bmtp_mesh_tools.py"
PKG = "_bmtp_unsubdivide_seams_test_pkg"

_ORIGINAL_GLOBAL_MODULES = {name: sys.modules.get(name) for name in ("bpy", "bmesh")}


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


# ---------------------------------------------------------------------------
# 轻量 fake：只做到「模块能加载 + 三个 seam 辅助函数能跑」
# ---------------------------------------------------------------------------
class _AnyProps:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _AnyTypes:
    def __getattr__(self, name):
        return type(name, (object,), {})


_install_module("bpy", props=_AnyProps(), types=_AnyTypes())
_install_module(f"{PKG}.utils.color_attribute_utils", read_color_attribute_data=None, write_color_attribute_data=None)
_install_module(f"{PKG}.utils.vertex_color_utils", convert_color_srgb_to_linear=None, ensure_color_attribute=None)


class _MeshEdge:
    """网格数据层的一条边（与 bmesh 边共享同一个 seam 值）。"""

    def __init__(self, a, b, seam=False):
        self.verts = (int(a), int(b))
        self.seam = bool(seam)


class _FakeVert:
    def __init__(self, index):
        self.index = int(index)


class _BMEdge:
    def __init__(self, verts, mesh_edge):
        self.verts = verts
        self._mesh_edge = mesh_edge

    @property
    def seam(self):
        return self._mesh_edge.seam

    @seam.setter
    def seam(self, value):
        self._mesh_edge.seam = bool(value)


class _LookupList(list):
    def ensure_lookup_table(self):
        return None

    def index_update(self):
        return None


class _FakeBM:
    def __init__(self, mesh):
        self.mesh = mesh
        self.verts = _LookupList(
            _FakeVert(index) for index in sorted({v for edge in mesh.edges for v in edge.verts})
        )
        self.edges = _LookupList(
            _BMEdge((_FakeVert(edge.verts[0]), _FakeVert(edge.verts[1])), edge) for edge in mesh.edges
        )


class _FakeMesh:
    def __init__(self, edges, name="mesh"):
        self.name = name
        self.edges = list(edges)
        self.fail_edit_state = False

    def seam_flags(self):
        return [edge.seam for edge in self.edges]


class _FakeBmeshModule(types.ModuleType):
    def __init__(self, name="bmesh"):
        super().__init__(name)
        self._cache = {}

    def from_edit_mesh(self, mesh):
        if getattr(mesh, "fail_edit_state", False):
            raise RuntimeError("Object not in edit mode")
        cached = self._cache.get(id(mesh))
        # 边数变化 = 拓扑变了：像真实 bmesh 一样重新按网格数据派生
        if cached is None or len(cached.edges) != len(mesh.edges):
            cached = _FakeBM(mesh)
            self._cache[id(mesh)] = cached
        return cached

    def update_edit_mesh(self, _mesh, **_kwargs):
        return None


sys.modules["bmesh"] = _FakeBmeshModule()

_spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.bmtp_mesh_tools", MODULE_PATH)
MOD = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = MOD
_spec.loader.exec_module(MOD)


def _mesh_with_seams(seed_edge_pairs, seam_keys):
    """构造网格：seam_keys 里的顶点对 seam=True，其余 False。"""
    edges = [
        _MeshEdge(a, b, seam=((min(a, b), max(a, b)) in seam_keys))
        for a, b in seed_edge_pairs
    ]
    return _FakeMesh(edges)


def _context(objects, active_object):
    return types.SimpleNamespace(
        mode="EDIT_MESH",
        active_object=active_object,
        objects_in_mode_unique_data=list(objects),
    )


class SnapshotTests(unittest.TestCase):
    def test_snapshots_cover_non_active_edit_meshes(self):
        mesh_a = _mesh_with_seams([(0, 1), (1, 2)], {(0, 1)})
        mesh_b = _mesh_with_seams([(0, 1), (1, 2), (2, 3)], {(0, 1), (2, 3)})
        obj_a = types.SimpleNamespace(type="MESH", data=mesh_a)
        obj_b = types.SimpleNamespace(type="MESH", data=mesh_b)

        snapshots = MOD._us_snapshot_other_edit_meshes_seams(_context([obj_a, obj_b], obj_a), mesh_a)

        self.assertEqual([mesh for mesh, _seams in snapshots], [mesh_b])
        self.assertEqual(snapshots[0][1], {(0, 1): True, (1, 2): False, (2, 3): True})

    def test_shared_mesh_data_is_snapshotted_once(self):
        shared = _mesh_with_seams([(0, 1)], {(0, 1)})
        active = _mesh_with_seams([(0, 1)], set())
        obj_a = types.SimpleNamespace(type="MESH", data=active)
        obj_b1 = types.SimpleNamespace(type="MESH", data=shared)
        obj_b2 = types.SimpleNamespace(type="MESH", data=shared)

        snapshots = MOD._us_snapshot_other_edit_meshes_seams(
            _context([obj_a, obj_b1, obj_b2], obj_a), active
        )

        self.assertEqual(len(snapshots), 1)
        self.assertIs(snapshots[0][0], shared)

    def test_active_mesh_is_excluded(self):
        active = _mesh_with_seams([(0, 1)], {(0, 1)})
        obj_a = types.SimpleNamespace(type="MESH", data=active)
        obj_b = types.SimpleNamespace(type="MESH", data=active)

        snapshots = MOD._us_snapshot_other_edit_meshes_seams(_context([obj_a, obj_b], obj_a), active)

        self.assertEqual(snapshots, [])

    def test_mesh_without_edit_state_is_skipped(self):
        active = _mesh_with_seams([(0, 1)], set())
        broken = _mesh_with_seams([(0, 1)], {(0, 1)})
        broken.fail_edit_state = True
        obj_a = types.SimpleNamespace(type="MESH", data=active)
        obj_b = types.SimpleNamespace(type="MESH", data=broken)

        snapshots = MOD._us_snapshot_other_edit_meshes_seams(_context([obj_a, obj_b], active), active)

        self.assertEqual(snapshots, [])
        self.assertEqual(MOD._us_restore_other_edit_meshes_seams(snapshots), 0)
        self.assertEqual(broken.seam_flags(), [True])  # 取不到编辑态 ⇒ 绝不误写

    def test_non_mesh_objects_are_ignored(self):
        active = _mesh_with_seams([(0, 1)], set())
        obj_a = types.SimpleNamespace(type="MESH", data=active)
        obj_empty = types.SimpleNamespace(type="EMPTY", data=None)

        snapshots = MOD._us_snapshot_other_edit_meshes_seams(_context([obj_a, obj_empty], active), active)

        self.assertEqual(snapshots, [])


class RoundTripTests(unittest.TestCase):
    """复刻实机事故路径：快照 → 全局 clear → 还原。"""

    def _setup(self):
        pairs = [(0, 1), (1, 2), (2, 3), (3, 4)]
        mesh_a = _mesh_with_seams(pairs, {(0, 1)})
        mesh_b = _mesh_with_seams(pairs, {(0, 1), (2, 3)})
        obj_a = types.SimpleNamespace(type="MESH", data=mesh_a)
        obj_b = types.SimpleNamespace(type="MESH", data=mesh_b)
        return _context([obj_a, obj_b], obj_a), mesh_a, mesh_b

    def test_restore_keeps_other_object_seams_after_global_clear(self):
        context, mesh_a, mesh_b = self._setup()

        snapshots = MOD._us_snapshot_other_edit_meshes_seams(context, mesh_a)
        for mesh in (mesh_a, mesh_b):  # 模拟 bpy.ops.mesh.mark_seam(clear=True) 的全局效果
            for edge in mesh.edges:
                edge.seam = False
        self.assertEqual(mesh_b.seam_flags(), [False, False, False, False])  # 事故现场

        restored = MOD._us_restore_other_edit_meshes_seams(snapshots)

        self.assertEqual(restored, 1)
        self.assertEqual(mesh_b.seam_flags(), [True, False, True, False])

    def test_restore_clears_seams_on_edges_added_after_snapshot(self):
        context, mesh_a, mesh_b = self._setup()
        snapshots = MOD._us_snapshot_other_edit_meshes_seams(context, mesh_a)

        mesh_b.edges.append(_MeshEdge(4, 5, seam=True))  # 快照后新增的边
        MOD._us_restore_other_edit_meshes_seams(snapshots)

        self.assertEqual(mesh_b.seam_flags(), [True, False, True, False, False])

    def test_active_mesh_state_is_not_touched_by_helper(self):
        context, mesh_a, mesh_b = self._setup()
        snapshots = MOD._us_snapshot_other_edit_meshes_seams(context, mesh_a)
        before = mesh_a.seam_flags()

        MOD._us_restore_other_edit_meshes_seams(snapshots)

        self.assertEqual(mesh_a.seam_flags(), before)


class SourceLevelRegressionTests(unittest.TestCase):
    """锁接线顺序：快照在全局 seam 算子之前，还原在任何提前 return 之前。"""

    @classmethod
    def setUpClass(cls):
        cls.source = MODULE_PATH.read_text(encoding="utf-8")
        start = cls.source.index("def _do_unsubdivide")
        cls.body = cls.source[start:]

    def test_snapshot_happens_before_global_seam_operators(self):
        snapshot_pos = self.body.index("_us_snapshot_other_edit_meshes_seams(context, me)")
        clear_pos = self.body.index("bpy.ops.mesh.mark_seam(clear=True)")
        islands_pos = self.body.index("bpy.ops.uv.seams_from_islands()")
        self.assertLess(snapshot_pos, clear_pos)
        self.assertLess(snapshot_pos, islands_pos)

    def test_restore_happens_before_first_early_return(self):
        restore_pos = self.body.index("_us_restore_other_edit_meshes_seams(other_seam_snapshots)")
        cancel_pos = self.body.index("return {'CANCELLED'}")
        self.assertLess(restore_pos, cancel_pos)

    def test_helpers_exist_and_are_used(self):
        self.assertIn("def _us_snapshot_other_edit_meshes_seams(", self.source)
        self.assertIn("def _us_restore_other_edit_meshes_seams(", self.source)
        self.assertIn("def _us_read_bmesh_seams(", self.source)


if __name__ == "__main__":
    unittest.main()
