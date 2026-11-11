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


class SweepBindingTests(unittest.TestCase):
    """Stage outputs must remain visible to later collision dispatches."""

    REGISTERS = ("cs-t89", "cs-t91", "cs-t92", "cs-t93", "cs-u4")

    def _bindings(self, sections):
        for section, lines in sections.items():
            for raw in lines:
                register, _, value = raw.strip().partition(" = ")
                if register in self.REGISTERS:
                    yield section, register, value

    def _assert_bindings(self, sections, expected):
        # Teardown lines (``= null``) are not bindings and stay untouched.
        bindings = [item for item in self._bindings(sections) if item[2] != "null"]
        self.assertEqual(len(bindings), expected)
        for section, register, value in bindings:
            mode = "ref " if register == "cs-u4" else "copy "
            self.assertTrue(value.startswith(mode),
                            f"{section}: {register} = {value} (expected {mode.strip()})")
        return bindings

    def test_collision_outputs_are_shared_between_dispatches(self):
        # Each builder is checked on its own dict: merging them would keep the
        # sweep-only base section that surface_shader_sections() deletes.
        sweep = self._assert_bindings(
            collision.sweep_shader_sections("abc123_ns", 64, 3), 6)
        surface = self._assert_bindings(
            collision.surface_shader_sections("abc123_ns", 64, 6, 3), 14)
        orders = [value for _, register, value in sweep + surface
                  if register == "cs-t89" and value == "copy ResourceDragCollisionOrder_abc123_ns"]
        self.assertTrue(orders, "sweep and surface must bind the Morton order with copy")

    @unittest.skipUnless(sys.platform == "win32", "D3D11 requires Windows")
    def test_emitted_collision_chain_preserves_outward_drag_and_blocks_entry(self):
        spec = importlib.util.spec_from_file_location(
            "collision_command_gpu", ROOT / "tools/check_drag_cloth.py"
        )
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        source = np.zeros((6, 10), np.float32)
        source[:, :3] = [
            [0, -2, -2], [0, 0, 2], [0, 2, -2],
            [-.1, -.2, -.2], [-.1, .2, -.2], [-.1, 0, .2],
        ]
        source[:, 3] = -1
        body = np.array([[0, 1, 2]], np.uint32)
        order, leaves = collision.bake_sweep_order(source[body, :3])
        cloth, adjacency = collision.bake_cloth_adjacency(
            [[3, 4, 5]], [False, False, False, True, True, True]
        )
        suffix = "body_ns"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            arrays = {"Order": order, "Cloth": cloth, "Adjacency": adjacency}
            sections = {}
            for name, array in arrays.items():
                array.tofile(root / (name + ".buf"))
                sections[f"[ResourceDragCollision{name}_{suffix}]"] = [
                    "type = Buffer", "format = R32_UINT",
                    f"filename = {name}.buf",
                ]
            for name, count, fmt in [
                ("Bounds", leaves * 4, "R32G32B32A32_FLOAT"),
                ("FaceLimits", len(cloth), "R32_FLOAT"),
            ]:
                sections[f"[ResourceDragCollision{name}_{suffix}]"] = [
                    "type = RWBuffer", f"format = {fmt}", f"array = {count}",
                ]
            chain = collision.surface_shader_sections(
                suffix, leaves, len(cloth), 1,
                shader_dir=(ROOT / "Toolset/drag_interaction").as_posix(),
            )
            sections.update(chain)
            for travel in (-.3, .5):
                runner = runtime.GeneratedCloth(root, sections, {})
                try:
                    candidate = source.copy()
                    candidate[3:, 0] += travel
                    inputs = runner.external("Input", source, 40, structured=True)
                    indices = runner.external("Body", body, 4, 42)
                    output = runner.external(
                        "Output", candidate, 40, structured=True, flags=136
                    )
                    runner.gpu.bind(24, runner.view(inputs, False))
                    runner.gpu.bind(87, runner.view(indices, False))
                    runner.gpu.bind(5, runner.view(output, True), True)
                    runner.params[101, 1] = .002
                    for section in chain:
                        runner.execute(section.strip("[]"))
                    result = runner.gpu.read_bytes(output.pointer, output.size)
                    result = result.view("<f4").reshape(-1, 10)
                    np.testing.assert_array_equal(result[:3], source[:3])
                    if travel < 0:
                        np.testing.assert_allclose(result[3:, 0], candidate[3:, 0], atol=1e-6)
                    else:
                        self.assertTrue(np.all(result[3:, 0] < 0))
                        self.assertTrue(np.all(result[3:, 0] > source[3:, 0] + .05))
                finally:
                    runner.close()


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
