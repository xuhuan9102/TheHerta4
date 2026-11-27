import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from tests import test_drag_cloth as cloth_tests


def _tear_fields(render, texture):
    rows = render.gpu.read_texture(texture.pointer, *texture.texture, texture.fmt, texture.stride)
    return (rows.view("<u4")[..., None] >> np.arange(0, 32, 8, dtype=np.uint32)) & 255


class DragTearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cloth_tests.GarmentExportTests.setUpClass()
        cls.tear = importlib.import_module("_ssmt_root.blueprint.drag_tear")

    @staticmethod
    def add_uv(root, sections):
        positions = np.fromfile(root / "Meshes/abc123-Position.buf", dtype="<f4").reshape(-1, 10)
        uv = np.column_stack((positions[:, 0] + 0.5, 0.1 + positions[:, 2] * 0.7)).astype("<f4")
        uv.tofile(root / "Meshes/abc123-Texcoord.buf")
        sections["[Resourceabc123Texcoord]"] = ["type = Buffer", "stride = 8", "filename = Meshes/abc123-Texcoord.buf"]
        sections["[TextureOverride_abc123_Draw]"].append("; SSMT tear TEXCOORD0 = 0, R32G32_FLOAT")

    @staticmethod
    def install_ttl_fixture(root):
        ini = root / "Core/ZZMI/Libraries/TTLib/TTLib.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text("namespace = TTL\n[CommandListDraw]\n", "utf8")

    def generate(self, root, cloth=False, ttl=False, authored_ttl_names=()):
        fixture = cloth_tests.GarmentExportTests()
        node, sections, comp = fixture.prepare(root, bake=cloth, prefix=3 if cloth else 0)
        self.add_uv(root, sections)
        node.cloth_slide_enabled = cloth
        draw = "[TextureOverride_abc123_Draw]"
        tail = ""
        if ttl:
            lines = sections[draw]
            original = lines[4]
            count, start, base = original.split(" = ")[1].split(", ")
            lines[4:5] = [
                "; [SSMT TTL 已接管] " + original,
                f"$\\TTL\\_1 = {count}", f"$\\TTL\\_2 = {start}", f"$\\TTL\\_3 = {base}",
                r"run = CommandList\TTL\Draw",
            ]
            count, start, base = lines[10].split(" = ")[1].split(", ")
            lines[10:11] = [
                f"$\\TTL\\_1 = {count}", f"$\\TTL\\_2 = {start}", f"$\\TTL\\_3 = {base}",
                "run = CommandListSSMTTTLDraw_legacy",
            ]
            tail = "[CommandListSSMTTTLDraw_legacy]\nib = ResourceIB\nrun = CommandList\\TTL\\Draw\n"
            comp["parts"] = node._collect_draw_parts(sections, comp["hash"])
        assets = self.tear.bake(
            node, root, sections, [comp], "unit", "Mod.ini", {"Stocking0", "Stocking1"},
            fixture.mod._blender_object_key, authored_ttl_names,
        )
        self.assertIsNotNone(assets)
        node._emit_sections(sections, [comp], "unit")
        node._inject_draw_hooks(sections, comp, "unit")
        if cloth:
            fixture.exporter.publish_before_draws(sections, comp)
            tail = fixture.exporter.publish_in_tail(tail, sections, [comp])
        node._emit_present_and_constants(sections, [comp], "unit")
        existing = {name: lines.copy() for name, lines in sections.items()}
        wrapped = self.tear.emit(node, sections, tail, [comp], "unit", assets)
        return sections, comp, wrapped, existing, tail

    def test_wraps_only_draws_and_keeps_cloth_and_ttl_routing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.install_ttl_fixture(root)
            sections, comp, tail, original, original_tail = self.generate(root, cloth=True, ttl=True)
            binding = f"{self.tear.PIXEL_SLOT} = ref ResourceDragTearMask_abc123P0M1_unit"
            tail_binding = f"{self.tear.PIXEL_SLOT} = ref ResourceDragTearMask_abc123P0M2_unit"
            draw = sections["[TextureOverride_abc123_Draw]"]
            inline = next(i for i, line in enumerate(draw) if line.strip() == r"run = CommandList\TTL\Draw")
            self.assertIn(binding, [line.strip() for line in draw[inline - 10:inline]])
            self.assertTrue(any(comp["cloth_publish"] in line for line in draw[:inline - 8]))
            self.assertLess(tail.index("ib = ResourceIB"), tail.index(tail_binding))
            self.assertLess(tail.index(comp["cloth_publish"]), tail.index(tail_binding))
            self.assertIn("run = CommandListDragTearTTLRestore_unit", tail)
            self.assertIn(r"Resource\TTL\TransparencyTex = ref ResourceDragTearTTL_abc123P0M2_unit unless_null",
                          sections["[CommandListDragTearTTLBind_abc123P0M2_unit]"])
            self.assertIn(r"if $\TTL\enabled && @ResourceDragTearTTL_abc123P0M2_unit",
                          sections["[CommandListDragTearTTLBind_abc123P0M2_unit]"])
            self.assertEqual(sum(line.strip() == binding for line in draw), 1)
            self.assertFalse(any(name.startswith("[CommandListDragTearNativeTTL") for name in sections))
            self.assertEqual(self.tear.clean_existing(sections, tail, "unit"), original_tail)
            self.assertEqual(sections, original)
            self.tear.prune(root, "unit", set())
            self.assertFalse((root / "res/drag_tear").exists())
            self.assertTrue((root / "res/drag_cloth").exists())

    def test_export_switch_adds_and_removes_the_scoped_tear_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.install_ttl_fixture(root)
            node, sections, comp = cloth_tests.GarmentExportTests().prepare(root, bake=False)
            self.add_uv(root, sections)
            ini = root / "Mod.ini"
            node._write_ordered_dict_to_ini(sections, ini)
            node.mod_namespace = "unit"
            node.hash_values = "abc123"
            node.cloth_slide_enabled = False
            node._write_jiggle_masks = lambda *args: True  # No Blender volume or preview in this fixture.
            node._write_zone_resources = lambda *args: None
            settings = node._collect_enabled_zones()[0].ssmt_drag_zone
            settings.include_objects.append(SimpleNamespace(object=SimpleNamespace(name="Body_copy")))
            node.tear_enabled = True
            node.tear_reset_key = "Ctrl+Alt+K"
            node.execute_postprocess(temp)
            enabled = ini.read_text("utf8")
            self.assertIn(
                "[KeyDragTearReset_unit]\ncondition = $ssmtdrag_drawn_unit == 1\n"
                "key = Ctrl Alt K\ntype = activate\n$ssmtdragtear_reset_unit = 1",
                enabled,
            )
            self.assertNotIn("[KeyDragTear_unit]", enabled)
            self.assertNotIn("$ssmtdragtear_enabled_unit", enabled)
            self.assertIn(f"{self.tear.PIXEL_SLOT} = ref ResourceDragTearMask_abc123P0M1_unit", enabled)
            self.assertIn("run = CommandListDragTearNativeTTL0_unit", enabled)
            self.assertNotIn("ib = ref ResourceDragTear", enabled)
            self.assertTrue((root / "res/drag_tear").is_dir())
            owners_path = next((root / "res/drag_tear").glob("*/*-owners.buf"))
            owners = np.fromfile(owners_path, dtype="<u4")[2:]
            body_offset, body_count, _ = next(
                entry for entry in comp["parts"][0]["name_ranges"] if entry[2] == "Body"
            )
            self.assertFalse(owners[body_offset // 3:(body_offset + body_count) // 3].any())
            node.execute_postprocess(temp)
            self.assertEqual(ini.read_text("utf8").count("[KeyDragTearReset_unit]"), 1)
            self.assertEqual(ini.read_text("utf8").count("[CommandListDragTearNativeTTL0_unit]"), 1)
            (root / "Core/ZZMI/Libraries/TTLib/TTLib.ini").unlink()
            node.execute_postprocess(temp)
            self.assertNotIn("CommandListDragTearNativeTTL", ini.read_text("utf8"))
            node.tear_enabled = False
            node.hash_values = "deadbeef"
            node.execute_postprocess(temp)
            self.assertTrue((root / "res/drag_tear").is_dir())
            node.hash_values = "abc123"
            node.execute_postprocess(temp)
            disabled = ini.read_text("utf8")
            self.assertNotIn("DragTear", disabled)
            self.assertNotIn(self.tear.BEGIN, disabled)
            self.assertIn("CustomShaderDragJiggleabc123_unit", disabled)
            self.assertFalse((root / "res/drag_tear").exists())

    def test_prepares_ttl_compositor_before_authored_material_replaces_draw(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.install_ttl_fixture(root)
            sections, _, _, _, _ = self.generate(root, authored_ttl_names={"Stocking0"})
            draw = sections["[TextureOverride_abc123_Draw]"]
            self.assertFalse(any("run = CommandListDragTearNativeTTL1_unit" in line for line in draw))
            self.assertTrue(any("ps-t116 = ref ResourceDragTearMask_abc123P0M1_unit" in line for line in draw))
            self.assertIn(
                "cs-t106 = ref ResourceDragTearMask_abc123P0M1_unit",
                sections["[CustomShaderDragTearTTLCompose_abc123P0M1_unit]"],
            )

    def test_direct_stocking_draw_uses_installed_ttl_without_changing_body_or_cloth(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.install_ttl_fixture(root)
            sections, comp, _, original, original_tail = self.generate(root, cloth=True)
            draw = sections["[TextureOverride_abc123_Draw]"]
            calls = [line.strip() for line in draw if line.strip().startswith("run = CommandListDragTearNativeTTL")]
            self.assertEqual(len(calls), 2)
            self.assertEqual(sum(line.strip() == comp["cloth_publish"] for line in draw),
                             sum(line.strip() == comp["cloth_publish"] for line in original["[TextureOverride_abc123_Draw]"]))
            self.assertIn(r"if $\TTL\enabled", [line.strip() for line in draw])
            for call in calls:
                lines = sections["[" + call.split(" = ")[1] + "]"]
                self.assertIn(r"Resource\TTL\TransparencyTex = null", lines)
                self.assertIn(r"run = CommandList\TTL\Draw", lines)
                self.assertTrue(any("CommandListDragTearTTLBind_abc123P0M" in line for line in lines))
                self.assertTrue(any(line.strip() == "run = CommandListDragTearTTLRestore_unit" for line in lines))
            self.assertEqual(self.tear.clean_existing(sections, original_tail, "unit"), original_tail)
            self.assertEqual(sections, original)

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_stocking_render_keeps_separate_opening_thinning_yarn_and_rim(self):
        from tools.check_drag_cloth import GeneratedCloth

        size = 256
        coordinates = np.linspace(-0.1, 0.1, size, dtype=np.float32)
        atlas = np.zeros((size, size, 4), dtype="<f2")
        atlas[:, :, 0] = coordinates[None, :]
        atlas[:, :, 2] = coordinates[:, None]
        atlas[:, :, 3] = 1
        gesture = np.array([[0, 0, 0, 1], [0, 1, 0, 0.009], [0, 0, -1, 0.051], [0, 0, 0.16, 0]], np.float32)
        sections = {
            "[ResourceMask]": ["type = Texture2D", "format = R32_UINT", f"width = {size}",
                               f"height = {size}", "array = 1", "msaa = 1", "mips = 1"],
            "[CustomShaderPaint]": [
                f"cs = {self.tear.SHADER_DIR / 'rzm_tear_mask.hlsl'}", "y494 = 1",
                "clear = ResourceMask",
                "cs-t106 = ResourceGesture", "cs-t108 = ResourceAtlas", "cs-u0 = ResourceMask",
                f"dispatch = {size // 8}, {size // 8}, 1", "cs-u0 = null",
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            render = GeneratedCloth(temp, sections, {})
            try:
                render.external("ResourceAtlas", atlas, 8, 17)
                render.external("ResourceGesture", gesture, 16, 2)
                render.execute("CustomShaderPaint")
                result = render.resource("ResourceMask")
                opening, thinning, yarn, rim = np.moveaxis(_tear_fields(render, result), -1, 0)
                waist = (opening[np.abs(coordinates) < 0.005] >= 128).any(axis=0)
                self.assertLess(float(np.ptp(coordinates[waist])), 0.012)
                self.assertGreater(np.unique(opening[(opening > 0) & (opening < 255)]).size, 12)
                outside = (coordinates < -0.044) & (coordinates > -0.065)
                self.assertFalse(opening[outside].any())
                self.assertGreater(np.unique(thinning[outside]).size, 8)
                self.assertTrue(((opening >= 128) & (yarn > 0)).any())
                self.assertTrue(((opening < 128) & (rim > 0)).any())
            finally:
                render.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_gpu_hole_grows_at_grab_and_leaves_source_and_other_objects_intact(self):
        import ctypes

        from tools.check_drag_cloth import GeneratedCloth

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sections, comp, _, _, _ = self.generate(root, ttl=True)
            render = GeneratedCloth(root, sections, comp)
            update = "CommandListDragTearUpdate_unit"
            display = render.resource("ResourceDragTearMask_abc123P0M1_unit")
            other = render.resource("ResourceDragTearMask_abc123P0M2_unit")
            source = render.resource("ResourceDragTearOriginal_abc123P0_unit")
            for slot in range(106, 111):
                render.resources[f"cs-t{slot}"] = source
                render.gpu.bind(slot, render.view(source, False))
            initial = np.fromfile(root / "Meshes/Index.buf", dtype="<u4").reshape(-1, 3)
            owners = comp["parts"][0]["tear"][1]
            eligible = np.flatnonzero(owners)
            face = eligible[len(eligible) // 4]
            hit = np.zeros((15, 4), np.float32)
            hit[1, 3] = 7
            hit[2, 3] = face
            # The detector accepts hits within its cursor padding and projects
            # them onto a triangle edge, with the inside flag left at zero.
            hit[4] = (0.12837, 0, 0.87163, 0)
            hit[13, 0] = 1
            grab = np.zeros((15, 4), np.float32)
            grab[3, 3] = 1
            grab[6, 0] = 1
            grab[7, 1] = 1

            def read_mask(resource):
                return render.gpu.read_texture(resource.pointer, *resource.texture, resource.fmt, resource.stride).view("<f4")

            def move(amount):
                render.params[69, 0] += amount
                render.variables["$cursorX"] = render.params[69, 0]
                render.execute(update)

            try:
                render.external("ResourceDragJiggleScreenState_unit", grab, 16, 2)
                render.external("ResourceDragPinnedDetectInfo_unit", hit, 16, structured=True)
                render.params[69] = (500, 400, 1920, 1080)
                render.execute(update)
                self.assertEqual(render.dispatches, 0)
                render.variables["$isMouseButtonDown"] = 1
                render.execute(update)
                move(20)
                self.assertFalse(_tear_fields(render, display).any())

                move(15)
                near = _tear_fields(render, display)
                self.assertTrue(near.any())
                move(140)
                far = _tear_fields(render, display)
                self.assertGreater(far.sum(), near.sum())
                self.assertTrue((far[:, :, :2] >= near[:, :, :2]).all())
                self.assertFalse(far[:, :, 3][far[:, :, 0] == 255].any())
                self.assertFalse(_tear_fields(render, other).any())
                kept = render.gpu.read_bytes(source.pointer, source.size).view("<u4").reshape(-1, 3)
                np.testing.assert_array_equal(kept, initial)
                for slot in range(106, 111):
                    restored = render.resources[f"cs-t{slot}"]
                    self.assertEqual(restored.pointer, source.pointer)
                    self.assertEqual(restored.fmt, source.fmt)
                render.variables["$isMouseButtonDown"] = 0
                dispatches = render.dispatches
                render.execute(update)
                self.assertEqual(render.dispatches, dispatches)
                np.testing.assert_array_equal(_tear_fields(render, display), far)

                # TTL writes depth from the combined opacity texture before its color pass.
                saved_cs = {slot: render.resources[f"cs-t{slot}"] for slot in (106, 107)}
                output = render.resource("ResourceDragTearTTL_abc123P0M1_unit")
                render.sections["[ResourceDragTearSavedTTL_unit]"] = [
                    "type = Texture2D", "width = 4", "height = 4", "format = R32G32B32A32_FLOAT",
                    "array = 1", "msaa = 1", "mips = 1",
                ]
                original_mask = render.resource("ResourceDragTearSavedTTL_unit")
                channels = (ctypes.c_float * 4)(0.2, 0.4, 0.6, 0.8)
                render.gpu.method(render.gpu.context, 52, [ctypes.c_void_p, ctypes.c_void_p],
                                  render.view(original_mask, True), channels, result=None)
                unchanged = ~far.any(axis=-1)
                empty = (far[:, :, 0] == 255) & (far[:, :, 2] == 0)
                run = (far[:, :, 0] == 0) & (far[:, :, 1] > 30) & (far[:, :, 3] == 0)
                rim = (far[:, :, 0] == 0) & (far[:, :, 1] == 0) & (far[:, :, 3] > 0)
                self.assertTrue(empty.any() and run.any())
                for invert in (0, 1):
                    render.params[494, 2] = invert
                    render.execute("CustomShaderDragTearTTLCompose_abc123P0M1_unit")
                    composed = read_mask(output).reshape(*output.texture, 4)
                    np.testing.assert_allclose(composed[unchanged], np.broadcast_to(channels, (unchanged.sum(), 4)), atol=1e-6)
                    self.assertTrue((composed[empty] == 1 - invert).all())
                    intact = np.array(channels) if invert else 1 - np.array(channels)
                    coverage = composed if invert else 1 - composed
                    self.assertTrue((coverage[run] > 0).all() and (coverage[run] < intact).all())
                    if rim.any():
                        self.assertTrue((coverage[rim] >= intact).all())

                # A previously opaque draw has no original TTL texture.
                compose = "CustomShaderDragTearTTLCompose_abc123P0M1_unit"
                lines = render.sections[f"[{compose}]"]
                lines[lines.index("cs-t107 = ref ResourceDragTearSavedTTL_unit")] = "cs-t107 = null"
                render.params[494, 2] = 0
                render.execute(compose)
                composed = read_mask(output).reshape(*output.texture, 4)
                self.assertFalse(composed[unchanged].any())
                self.assertTrue((composed[empty] == 1).all())
                self.assertTrue((composed[run] > 0).all() and (composed[run] < 1).all())
                for slot, resource in saved_cs.items():
                    render.resources[f"cs-t{slot}"] = resource
                    render.gpu.bind(slot, render.view(resource, False))

                end_frame = "CommandListDragTearEndFrame_unit"
                reset = "$ssmtdragtear_reset_unit"
                drawn = "$ssmtdrag_drawn_unit"
                ready = "$ssmtdragtear_ready_unit"
                epoch = "$ssmtdragtear_epoch_unit"
                before_reset = render.variables[epoch]
                render.variables[drawn] = 0
                render.variables[reset] = 1
                render.execute(end_frame)
                self.assertEqual(render.variables[reset], 0)
                self.assertEqual(render.variables[epoch], before_reset)
                np.testing.assert_array_equal(_tear_fields(render, display), far)

                render.variables[drawn] = 1
                render.variables["$isMouseButtonDown"] = 1
                render.variables[reset] = 1
                render.execute(end_frame)
                self.assertEqual(render.variables[ready], 0)
                self.assertEqual(render.variables[epoch], before_reset + 1)
                self.assertFalse(_tear_fields(render, display).any())
                dispatches = render.dispatches
                move(50)
                self.assertEqual(render.dispatches, dispatches)
                render.variables["$isMouseButtonDown"] = 0
                render.execute(end_frame)
                render.variables["$isMouseButtonDown"] = 1
                render.execute(update)
                move(35)
                self.assertTrue(_tear_fields(render, display).any())
            finally:
                render.close()
