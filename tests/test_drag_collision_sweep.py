"""Continuous collision must cover the path even when both endpoints are outside."""

import importlib.util
from pathlib import Path
import unittest
import sys
import tempfile
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("drag_collision", ROOT / "blueprint/drag_collision.py")
collision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collision)


class SweepOrderingTests(unittest.TestCase):
    def test_all_triangles_survive_ordering_and_padding(self):
        rng = np.random.default_rng(419)
        for count in (0, 1, 32, 33, 16398):
            order, leaves = collision.bake_sweep_order(rng.normal(size=(count, 3, 3)))
            np.testing.assert_array_equal(np.sort(order), np.arange(count))
            if count:
                self.assertGreaterEqual(leaves, count)
                self.assertEqual(leaves & (leaves - 1), 0)
                self.assertEqual(leaves % 32, 0)
            else:
                self.assertEqual(leaves, 0)

    def test_ordering_is_deterministic_for_coincident_centers(self):
        order, _ = collision.bake_sweep_order(np.zeros((70, 3, 3)))
        np.testing.assert_array_equal(order, np.arange(70))

    def test_rejects_nonfinite_geometry(self):
        with self.assertRaises(ValueError):
            collision.bake_sweep_order(np.full((1, 3, 3), np.nan))


class SweepInstallerTests(unittest.TestCase):
    def test_install_plan_is_idempotent_and_removes_post_solve_probe(self):
        spec = importlib.util.spec_from_file_location("sweep_installer", ROOT / "tools/install_drag_collision_sweep.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temp:
            mod = Path(temp).resolve()
            ini = mod / "model.ini"
            ini.write_text("""[ResourceTris]
filename = TriColliderTriGridTris.buf

[CustomShaderDragJiggleBody_A]
cs = jiggle.hlsl
x101 = 1
cs-t84 = ResourceTris
Dispatch = 2, 1, 1
vb0 = null
ResourceTemp = copy cs-u5
cs-u5 = null
""", encoding="utf-8")
            (mod / "jiggle.hlsl").write_text("""struct VertexAttributes { float3 position; float3 normal; float4 tangent; };
#define COLLISION_META      IniParams[104]
#define LINA_PROBE_MODE 1
if (COLLISION_PARAMS.x > 0.5 && COLLISION_META.x >= 2.0 && collisionDue)
""", encoding="utf-8")
            np.zeros((1, 4, 4), dtype=np.float32).tofile(mod / "TriColliderTriGridTris.buf")
            # Include the actual resource/draw chain the installer must follow.
            text = ini.read_text("utf-8").replace("cs-t84 = ResourceTris", "cs-t84 = ResourceTris\ncs-t79 = ResourceL0")
            text += "\n[ResourceL0]\nfilename = L0.buf\n\n[ResourceIB]\nfilename = Index.buf\n\n[TextureOverrideDraw]\nib = ResourceIB\nrun = CustomShaderDragJiggleBody_A\ndrawindexed = 3, 0, 0\n"
            ini.write_text(text, encoding="utf-8")
            l0 = np.zeros((3, 2, 4), dtype=np.float32); l0[:, 1, 3] = 1
            l0.tofile(mod / "L0.buf")
            np.array([0, 1, 2], dtype=np.uint32).tofile(mod / "Index.buf")
            payloads, _, _ = installer.prepare(mod, ini)
            for path, data in payloads.items():
                path.write_bytes(data)
            self.assertIn("#define LINA_PROBE_MODE 0", (mod / "jiggle.hlsl").read_text("utf-8"))
            repeat, _, _ = installer.prepare(mod, ini)
            for path, data in repeat.items():
                self.assertEqual(path.read_bytes(), data, f"Repeated installation rewrites {path.name}")

    def test_backup_ini_is_not_loaded_as_another_mod(self):
        spec = importlib.util.spec_from_file_location("sweep_backup_installer", ROOT / "tools/install_drag_collision_sweep.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temp:
            mod = Path(temp).resolve()
            ini = mod / "model.ini"
            ini.write_bytes(b"[Original]\n")
            payload = {ini: b"[Modified]\n"}
            with patch.object(installer, "prepare", return_value=(payload, {}, [("Body_A", 1, 32)])):
                backup = installer.install(mod, ini, apply=True)
            self.assertEqual((backup / "model.ini").read_bytes(), b"[Original]\n")
            # Exact include/exclude policy from d3dx.ini: recurse Mods, exclude DISABLED*.
            included = [p for p in mod.rglob("*.ini")
                        if not any(part.upper().startswith("DISABLED") for part in p.relative_to(mod).parts)]
            self.assertEqual(included, [ini])
            installer.restore(mod, backup)
            self.assertEqual(ini.read_bytes(), b"[Original]\n")


class SweepExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "tests/test_node_postprocess_draginteraction.py"
        spec = importlib.util.spec_from_file_location("drag_test_stubs", path)
        cls.stubs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.stubs)
        cls.mod = cls.stubs._load_drag_module()

    def test_normal_exports_do_not_activate_experimental_sweep(self):
        node = self.stubs._make_node(self.mod, collision_enabled=True)
        sections = self.stubs._base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        comp["collision_grid"] = {"bmin": (0, 0, 0), "h": .1, "h_c": .4,
                                  "dims": (1, 1, 1), "cdims": (1, 1, 1)}
        comp["collision_trigrid"] = {"bmin": (0, 0, 0), "cell": .1,
                                     "dims": (1, 1, 1), "cell_count": 1}
        comp["collision_sweep"] = {"leaves": 64}
        node._emit_sections(sections, [comp], "testns")
        cn = comp["comp_name"]
        jiggle = sections[f"[CustomShaderDragJiggle{cn}_testns]"]
        self.assertFalse(any("run = CustomShaderDragCollision" in line for line in jiggle))
        self.assertEqual(sum(line.startswith("Dispatch =") for line in jiggle), 1)
        self.assertNotIn(f"[ResourceDragCollisionBounds_{cn}_testns]", sections)
        self.assertNotIn(f"[ResourceDragCollisionOrder_{cn}_testns]", sections)

    def test_legacy_export_explicitly_disables_sweep(self):
        node = self.stubs._make_node(self.mod, collision_enabled=False)
        sections = self.stubs._base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        jiggle = sections[f"[CustomShaderDragJiggle{comps[0]['comp_name']}_testns]"]
        self.assertFalse(any("run = CustomShaderDragCollision" in line for line in jiggle))

    def test_surface_export_publishes_only_after_all_contact_passes(self):
        node = self.stubs._make_node(self.mod, collision_enabled=True)
        sections = self.stubs._base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        comp["collision_grid"] = {"bmin": (0, 0, 0), "h": .1, "h_c": .4,
                                  "dims": (1, 1, 1), "cdims": (1, 1, 1)}
        comp["collision_trigrid"] = {"bmin": (0, 0, 0), "cell": .1,
                                     "dims": (1, 1, 1), "cell_count": 1}
        comp["collision_surface"] = {"leaves": 64, "face_count": 6}
        node._emit_sections(sections, [comp], "testns")
        suffix = f"{comp['comp_name']}_testns"
        jiggle = sections[f"[CustomShaderDragJiggle{suffix}]"]
        self.assertIn("x115 = 1", jiggle)
        order = [next(i for i, line in enumerate(jiggle) if line.startswith("Dispatch ="))]
        for phase in ("Leaves", "Roots", "Surface", "Apply"):
            call = f"CustomShaderDragCollision{phase}{suffix}"
            self.assertIn(f"[{call}]", sections)
            order.append(jiggle.index(f"run = {call}"))
        order.append(jiggle.index("vb0 = null"))
        self.assertEqual(order, sorted(order))
        for resource in ("Order", "Bounds", "Cloth", "Adjacency", "FaceLimits"):
            self.assertIn(f"[ResourceDragCollision{resource}_{suffix}]", sections)


@unittest.skipUnless(sys.platform == "win32", "D3D11 shader execution requires Windows")
class SweepShaderExecutionTests(unittest.TestCase):
    def test_shipped_shaders_against_independent_triangle_oracle(self):
        spec = importlib.util.spec_from_file_location("sweep_gpu", ROOT / "tools/check_drag_collision_sweep.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        gpu = runner.D3D()
        try:
            runner.synthetic(gpu)
            runner.synthetic_surface(gpu)
        finally:
            gpu.release()


if __name__ == "__main__":
    unittest.main()
