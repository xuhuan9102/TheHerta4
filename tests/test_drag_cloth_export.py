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


if __name__ == "__main__":
    unittest.main()
