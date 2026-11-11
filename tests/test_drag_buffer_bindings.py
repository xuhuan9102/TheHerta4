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
