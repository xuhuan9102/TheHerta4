"""Audit real fixture exports, including corrupted buffers and broken UAV aliases."""

from pathlib import Path
import tempfile
import unittest

from tools.check_drag_cloth_export import audit


class ClothExportAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_drag_cloth

        fixture = test_drag_cloth.GarmentExportTests
        fixture.setUpClass()
        cls.fixture = fixture("test_self_collision_switch_omits_only_its_passes_and_assets")

    def export(self, root):
        node, sections, comp = self.fixture.prepare(root)
        self.fixture.exporter.emit_components(node, sections, [comp], "unit")
        ini = root / "fixture.ini"
        ini.write_text("\n".join(name + "\n" + "\n".join(lines) for name, lines in sections.items()), "utf8")
        return ini, comp

    def test_generated_fixture_passes_package_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ini, _ = self.export(root)
            result = audit(root)
            self.assertTrue(result["passed"])
            self.assertEqual(len(result["garments"]), 2)
            self.assertGreater(result["owned_assets"], 0)
            self.assertFalse(result["geometry_certified"])
            text = ini.read_text("utf8").replace(
                "cs-t2 = ref ResourceSSMTCloth_abc123_unit_SkinPalette_s1",
                "cs-t2 = ref ResourceSSMTCloth_Missing",
            )
            ini.write_text(text, "utf8")
            with self.assertRaisesRegex(ValueError, "Undefined cloth reference"):
                audit(root)

    def test_modified_owned_buffer_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, comp = self.export(root)
            data = root / comp["cloth"]["directory"] / "g0/rest.buf"
            data.write_bytes(data.read_bytes() + b"\0")
            with self.assertRaisesRegex(ValueError, "Manifest hash mismatch"):
                audit(root)

    def test_cloth_audit_allows_other_postprocessors_to_manage_textures(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ini, _ = self.export(root)
            with ini.open("a", encoding="utf-8") as handle:
                handle.write("\n[Constants]\n\n[ResourceOtherTexture]\nfilename = Textures/removed.dds\n")
            self.assertTrue(audit(root, cloth_only=True)["passed"])
            with self.assertRaisesRegex(ValueError, "Missing filename file"):
                audit(root)

    def test_copying_the_write_alias_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ini, _ = self.export(root)
            text = ini.read_text("utf8")
            bad = text.replace("_Write_s1 = ref ResourceSSMTCloth_", "_Write_s1 = copy ResourceSSMTCloth_")
            self.assertNotEqual(text, bad)
            ini.write_text(bad, "utf8")
            with self.assertRaisesRegex(ValueError, "Output alias must reference"):
                audit(root)

    def test_truncated_frozen_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ini, _ = self.export(root)
            text = ini.read_text("utf8")
            start = text.index("[ResourceSSMTCloth_abc123_unit_G0_FrozenMesh_s1]")
            end = text.index("\n[", start + 1)
            block = text[start:end]
            import re

            broken = re.sub(r"(?m)^array = \d+$", "array = 0", block)
            ini.write_text(text[:start] + broken + text[end:], "utf8")
            with self.assertRaisesRegex(ValueError, "FrozenMesh_s1: expected array"):
                audit(root)

    def test_ordinary_draw_buffer_cannot_pin_the_native_copy_capacity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ini, _ = self.export(root)
            text = ini.read_text("utf8") + (
                "\n[ResourceDragJiggleSolved_native]\n"
                "type = RWStructuredBuffer\nstride = 40\n"
                "array = 100\n"
                "bind_flags = shader_resource unordered_access\n"
                "[ResourceDragJiggleWrite_native]\n"
                "type = Buffer\nstride = 4\nformat = R32_FLOAT\n"
                "[CustomShaderDragPublishnative]\n"
                "cs-t24 = ref ResourceDragJiggleSolved_native\n"
                "cs-u0 = ref ResourceDragJiggleWrite_native\n"
                "cs-u0 = null\ncs-t24 = null\n"
                "[CommandListPublishNative]\n"
                "ResourceDragJiggleWrite_native = ref ResourceDragJiggleTempVB0_native\n"
                "[ResourceDragJiggleSeedWrite_native]\n"
                "type = Buffer\nstride = 4\nformat = R32_UINT\n"
                "[CustomShaderDragSeednative]\nx115 = 1000\n"
                "cs-t0 = ref ResourceDragJiggleNative_native\n"
                "cs-u0 = ref ResourceDragJiggleSeedWrite_native\n"
                "\n[ResourceDragJiggleTempVB0_native]\n"
                "type = Buffer\nstride = 40\narray = 100\n"
                "bind_flags = unordered_access vertex_buffer\nmisc_flags = none\n"
                "[ResourceDragJiggleNative_native]\n"
                "type = Buffer\nstride = 4\nformat = R32_UINT\nmisc_flags = none\n"
            )
            ini.write_text(text, "utf8")
            self.assertTrue(audit(root)["passed"])
            for override in ("array = 100", "byte_width = 4000"):
                ini.write_text(text + override + "\n", "utf8")
                with self.assertRaisesRegex(ValueError, "inherit native capacity"):
                    audit(root)


if __name__ == "__main__":
    unittest.main()
