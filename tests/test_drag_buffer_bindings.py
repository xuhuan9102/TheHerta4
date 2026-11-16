"""Execute the generated persistent-state and draw-copy bindings on D3D11."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tests.test_node_postprocess_draginteraction import (
    _base_sections,
    _load_drag_module,
    _make_node,
)


@unittest.skipUnless(sys.platform == "win32", "D3D11 requires Windows")
class DragBufferBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("drag_binding_gpu", path)
        cls.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runtime)

    def prepare(self, root):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        comp["vertex_count"] = 6
        comp["collision_grid"] = True
        comp["collision_trigrid"] = True
        meshes = root / "Meshes"
        meshes.mkdir()
        blend = np.zeros((6, 8), "<f4")
        blend[:, 0] = 1
        blend[:, 4:].view("<u4")[:, 0] = np.arange(6) + 37
        blend.tofile(meshes / (comp["base_name"] + "-Blend.buf"))
        node._emit_component_resources(sections, comp, "unit")
        return node, sections, comp, blend

    def test_component_pin_matches_its_structured_consumers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp, _ = self.prepare(root)
            cn = comp["comp_name"]
            node._emit_pin_component_section(sections, comp, "unit")
            section = f"[CustomShaderDragPinComponent{cn}_unit]"
            # Post lines only unbind the registers after the dispatch.
            sections[section] = [
                line for line in sections[section] if not line.startswith("post ")
            ]
            shader = (
                Path(__file__).resolve().parents[1]
                / "Toolset/drag_interaction/rzm_pin_detected.hlsl"
            )
            sections[section][0] = "cs = " + shader.as_posix()
            runner = self.runtime.GeneratedCloth(root, sections, {})
            try:
                hit = np.arange(60, dtype=np.float32).reshape(15, 4)
                hit[0] = [3, 0.4, 6, 2]
                runner.external(
                    f"ResourceDragComponentDetect_{cn}_unit", hit, 16, 2, flags=136
                )
                runner.execute(section.strip("[]"))
                info = runner.resource(f"ResourceDragPinnedComponentInfo_{cn}_unit")
                ids = runner.resource(f"ResourceDragPinnedComponentID_{cn}_unit")
                self.assertTrue(info.structured)
                self.assertEqual(info.stride, 16)
                self.assertTrue(ids.structured)
                actual = (
                    runner.gpu.read_bytes(info.pointer, info.size)
                    .view("<f4")
                    .reshape(-1, 4)
                )
                np.testing.assert_array_equal(actual[:10], hit[:10])
                np.testing.assert_array_equal(actual[11:], hit[11:])
                actual_ids = runner.gpu.read_bytes(ids.pointer, ids.size).view("<f4")
                np.testing.assert_array_equal(actual_ids, [hit[0, 0], hit[7, 3]])
            finally:
                runner.close()

    def test_inline_object_parameters_are_loaded_instead_of_zero_filled(self):
        sections = {
            "[ResourceParams]": ["type = Buffer", "format = R32G32B32A32_FLOAT", "array = 2", "data = 0 .25 1 1.5"],
            "[ResourceIDs]": ["type = Buffer", "format = R32G32B32A32_UINT", "array = 1", "data = 0 2 0xffffffff 7"],
        }
        with tempfile.TemporaryDirectory() as temp:
            runner = self.runtime.GeneratedCloth(temp, sections, {})
            try:
                resource = runner.resource("ResourceParams")
                values = runner.gpu.read_bytes(resource.pointer, resource.size).view("<f4")
                np.testing.assert_array_equal(values, [0, .25, 1, 1.5, 0, 0, 0, 0])
                resource = runner.resource("ResourceIDs")
                values = runner.gpu.read_bytes(resource.pointer, resource.size).view("<u4")
                np.testing.assert_array_equal(values, [0, 2, 0xffffffff, 7])
            finally:
                runner.close()

    def test_weight_state_is_initialized_and_published_as_a_plain_vertex_buffer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, sections, comp, blend = self.prepare(root)
            cn = comp["comp_name"]
            source_name = f"ResourceDragJiggleBlendOut_{cn}_unit"
            target_name = f"ResourceDragJiggleBlendVB_{cn}_unit"
            sections["[CopyWeights]"] = [f"{target_name} = copy {source_name}"]
            runner = self.runtime.GeneratedCloth(root, sections, {})
            try:
                source = runner.resource(source_name)
                self.assertTrue(source.structured)
                self.assertEqual(source.stride, 32)
                runner.execute("CopyWeights")
                target = runner.resource(target_name)
                self.assertFalse(target.structured)
                self.assertEqual(target.stride, 32)
                self.assertNotEqual(target.pointer, source.pointer)
                actual = runner.gpu.read_bytes(target.pointer, target.size)
                np.testing.assert_array_equal(actual, blend.view(np.uint8).ravel())
            finally:
                runner.close()

    def test_structured_deformation_copies_back_to_a_vertex_compatible_buffer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, sections, comp, _ = self.prepare(root)
            name = f"ResourceDragJiggleTempVB0_{comp['comp_name']}_unit"
            sections["[CopyVertices]"] = [f"{name} = copy cs-u5"]
            runner = self.runtime.GeneratedCloth(root, sections, {})
            try:
                vertices = np.arange(60, dtype="<f4").reshape(6, 10)
                source = runner.external(
                    "cs-u5", vertices, 40, structured=True, flags=136
                )
                runner.execute("CopyVertices")
                result = runner.resource(name)
                self.assertFalse(result.structured)
                self.assertEqual(result.stride, 40)
                self.assertNotEqual(result.pointer, source.pointer)
                np.testing.assert_array_equal(
                    runner.gpu.read_bytes(result.pointer, result.size),
                    vertices.view(np.uint8).ravel(),
                )
            finally:
                runner.close()

    def test_jiggle_copy_roundtrip_tracks_native_capacity_without_truncating_it(self):
        for implicit in (False, True):
            with self.subTest(implicit=implicit), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                _, sections, comp, _ = self.prepare(root)
                target = f"ResourceDragJiggleTempVB0_{comp['comp_name']}_unit"
                solved = f"ResourceDragJiggleSolved_{comp['comp_name']}_unit"
                writer = f"ResourceDragJiggleWrite_{comp['comp_name']}_unit"
                snapshot = f"ResourceDragJiggleNative_{comp['comp_name']}_unit"
                seed_writer = f"ResourceDragJiggleSeedWrite_{comp['comp_name']}_unit"
                shader = root / "deform.hlsl"
                shader.write_text(
                    "struct V {float3 p;float3 n;float4 t;};\n"
                    "StructuredBuffer<V> Source:register(t24);\n"
                    "RWStructuredBuffer<V> Target:register(u5);\n"
                    "[numthreads(64,1,1)]void main(uint3 id:SV_DispatchThreadID){"
                    "if(id.x>=6)return;V v=Source[id.x];"
                    "v.p+=float3(.125,-.25,.0625);Target[id.x]=v;}",
                    "utf8",
                )
                copy = "" if implicit else "copy "
                publish = Path(__file__).resolve().parents[1] / "Toolset/drag_interaction/rzm_jiggle_publish.hlsl"
                seed = publish.with_name("rzm_jiggle_seed.hlsl")
                sections["[RoundTrip]"] = [
                    f"{snapshot} = {copy}vb0", f"{seed_writer} = ref {target}",
                    "cs = " + str(seed), "x115 = 60", f"cs-t0 = ref {snapshot}",
                    f"cs-u0 = ref {seed_writer}", "Dispatch = 1, 1, 1", "cs-u0 = null", "cs-t0 = null",
                    "cs = deform.hlsl",
                    f"cs-t24 = {copy}vb0", f"cs-u5 = ref {solved}",
                    "Dispatch = 1, 1, 1", "cs-t24 = null", "vb0 = null",
                    "cs-u5 = null", f"{writer} = ref {target}",
                    "cs = " + str(publish), "x115 = 0", "y115 = 6",
                    f"cs-t24 = ref {solved}", f"cs-u0 = ref {writer}",
                    "Dispatch = 1, 1, 1", "cs-u0 = null", "cs-t24 = null", f"vb0 = {target}",
                ]
                runner = self.runtime.GeneratedCloth(root, sections, {})
                try:
                    for capacity in (9, 13, 6, 9):
                        native = np.arange(capacity*10, dtype=np.float32).reshape(capacity, 10) / 16
                        original = runner.external("vb0", native, 40, flags=1)
                        runner.execute("RoundTrip")
                        output = runner.resources["vb0"]
                        self.assertFalse(output.structured)
                        self.assertEqual(output.stride, 40)
                        self.assertEqual(output.size, 6*40)
                        expected = native[:6].copy()
                        expected[:6, :3] += [.125, -.25, .0625]
                        actual = runner.gpu.read_bytes(output.pointer, output.size).view("<f4").reshape(-1, 10)
                        np.testing.assert_array_equal(actual, expected)
                        np.testing.assert_array_equal(runner.gpu.read_bytes(original.pointer, original.size), native.view(np.uint8).ravel())
                finally:
                    runner.close()

    def test_all_persistent_uav_bindings_keep_the_named_resource(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        components = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, components, "unit")
        outputs = []
        for section, lines in sections.items():
            for raw in lines:
                register, _, value = raw.strip().partition(" = ")
                if not register.startswith("cs-u") or value == "null":
                    continue
                if value.startswith("copy "):
                    self.assertEqual(register, "cs-u5")
                    self.assertTrue(value.startswith("copy ResourceDragJiggleTempVB0_"))
                    self.assertTrue(any(" = copy cs-u5" in line for line in lines))
                else:
                    self.assertTrue(
                        value.removeprefix("ref ").startswith("ResourceDrag")
                    )
                outputs.append((section, register))
        self.assertGreater(len(outputs), 15)

    def test_screen_grab_accepts_detector_valid_conditioning_below_half(self):
        shader = Path(__file__).resolve().parents[1] / "Toolset/drag_interaction/rzm_jiggle_screen_state.hlsl"
        sections = {"[Screen]": [
            "cs = " + str(shader), "cs-t67 = ref ResourceHit",
            "cs-t75 = ref ResourceZones", "cs-u0 = ref ResourceScreen",
            "cs-u1 = ref ResourcePath", "Dispatch = 1, 1, 1",
        ]}
        with tempfile.TemporaryDirectory() as temp:
            runner = self.runtime.GeneratedCloth(temp, sections, {})
            try:
                zones = np.zeros((2, 4), np.float32)
                zones[0, 0] = .5
                zones[1, 1] = 1
                runner.external("ResourceZones", zones, 16, 2)
                runner.external("ResourcePath", np.zeros(1, np.float32), 4, 41, flags=136)
                runner.params[67] = [400, 500, 0, 1]
                runner.params[68] = [.25, 1, 1.5, 1]
                runner.params[70] = [.86, .176, .96, .055]
                runner.params[71] = [.5, 1.18, 1, .12]
                runner.params[72] = [0, 1, 1, 1]
                runner.params[73] = [1, 1, .5, 1]
                for conditioning in (1.0, .2, 1e-7):
                    with self.subTest(conditioning=conditioning):
                        hit = np.zeros((15, 4), np.float32)
                        hit[0] = [0, .01, 0, 1]
                        hit[1, 3] = 7
                        hit[5, 2] = 1
                        hit[8] = [1, 0, 0, 1]
                        hit[9] = [0, 1, 0, conditioning]
                        runner.external("ResourceHit", hit, 16, structured=True)
                        state = runner.external("ResourceScreen", np.zeros((15, 4), np.float32), 16, 2, flags=136)
                        for frame in range(24):
                            runner.params[69] = [400 if frame == 0 else 500, 500, 1920, 1080]
                            runner.execute("Screen")
                        result = runner.gpu.read_bytes(state.pointer, state.size).view("<f4").reshape(-1, 4)
                        length = float(np.linalg.norm(result[0, :3]))
                        if conditioning > 1e-5:
                            self.assertGreater(length, .001)
                        else:
                            self.assertEqual(length, 0)
            finally:
                runner.close()
