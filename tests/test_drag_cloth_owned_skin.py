import tempfile
import unittest
from pathlib import Path

from tests import test_drag_cloth as fixtures


class OwnedSkinClothTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.GarmentExportTests.setUpClass()

    def owned_fixture(self, folder):
        fixture = fixtures.GarmentExportTests()
        node, sections, comp = fixture.prepare(folder, bake=False, prefix=3)
        position = "ResourceZZRedirectSkinPosition_G3_c0"
        sections[f"[{position}]"] = [
            "type = StructuredBuffer", "stride = 40", "filename = ./Meshes\\abc123-Position.buf",
        ]
        draw = sections[comp["parts"][0]["section"]]
        for slot in (1, 2):
            output = f"ResourceZZSkinOut_G3_s{slot}"
            alias = f"ResourceZZRedirectSkinWrite_G3_s{slot}"
            palette = f"ResourceZZRedirectPalette_G3_s{slot}"
            draw.insert(3, f"vb0 = ref {output}")
            sections[f"[{output}]"] = ["type = Buffer", "stride = 40"]
            sections[f"[{alias}]"] = ["type = Buffer", "stride = 4", "format = R32_FLOAT"]
            sections[f"[{palette}]"] = ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 6"]
            sections[f"[CustomShaderZZMISkin_G3_s{slot}]"] = [
                "cs = ./res/zzmi_merged_skin_owned_output.hlsl",
                f"x1 = {comp['vertex_count'] - 3}",
                "y1 = 3", "z1 = 3", "w1 = 10",
                f"cs-t0 = ref {position}",
                f"{alias} = ref {output}",
                f"cs-t2 = ref {palette}",
                f"cs-u0 = ref {alias}",
                "Dispatch = 8, 1, 1", "cs-u0 = null",
            ]
        return fixture, node, sections, comp

    def test_bakes_and_captures_the_calibrated_palette_in_both_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture, node, sections, comp = self.owned_fixture(Path(temp))
            cloth, body = node._cloth_object_names(required=True)
            comp["cloth"] = fixture.exporter.bake_component(
                node, temp, sections, comp, "unit", cloth, body, fixture.mod._blender_object_key,
            )
            self.assertEqual(comp["cloth"]["palette_rows"], {1: 6, 2: 6})
            node._emit_sections(sections, [comp], "unit")
            for slot in (1, 2):
                lines = [line.strip() for line in sections[f"[CustomShaderZZMISkin_G3_s{slot}]"]]
                dispatch = lines.index("Dispatch = 8, 1, 1")
                source = f"ResourceSSMTCloth_abc123_unit_SkinSource_s{slot} = ref cs-u0"
                palette = f"ResourceSSMTCloth_abc123_unit_SkinPalette_s{slot} = copy ResourceZZRedirectPalette_G3_s{slot}"
                self.assertLess(lines.index(source), dispatch)
                self.assertLess(lines.index(palette), dispatch)

    def test_does_not_pair_an_unrelated_position_file_or_a_copy_of_the_output(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture, _, sections, comp = self.owned_fixture(Path(temp))
            vertex_count = comp["vertex_count"] - 3
            found = lambda: fixture.exporter._skin_writes(sections, comp, 3, "ResourceZZSkinOut", vertex_count)
            self.assertEqual({entry[2] for entry in found()}, {1, 2})
            lines = sections["[CustomShaderZZMISkin_G3_s2]"]
            index = lines.index("ResourceZZRedirectSkinWrite_G3_s2 = ref ResourceZZSkinOut_G3_s2")
            lines[index] = "ResourceZZRedirectSkinWrite_G3_s2 = copy ResourceZZSkinOut_G3_s2"
            self.assertEqual({entry[2] for entry in found()}, {1})
            sections["[ResourceZZRedirectSkinPosition_G3_c0]"][-1] = "filename = Meshes/unrelated-Position.buf"
            self.assertEqual(found(), [])

    def test_follows_per_draw_skin_output_through_a_render_command_list(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture, node, sections, comp = self.owned_fixture(Path(temp))
            draw_output = f"ResourceZZSkinOut_{comp['hash']}"
            for section, lines in list(sections.items()):
                del sections[section]
                sections[section.replace("ResourceZZSkinOut_G3", draw_output)] = [
                    line.replace("ResourceZZSkinOut_G3", draw_output) for line in lines
                ]
            draw = sections[comp["parts"][0]["section"]]
            sections["[CommandListSkinDraw]"] = [line for line in draw if f"vb0 = ref {draw_output}" in line]
            draw[:] = [line for line in draw if f"vb0 = ref {draw_output}" not in line]
            draw.insert(0, "run = CommandListSkinDraw")
            cloth, body = node._cloth_object_names(required=True)
            result = fixture.exporter.bake_component(
                node, temp, sections, comp, "unit", cloth, body, fixture.mod._blender_object_key,
            )
            self.assertEqual(result["native_family"], draw_output)
            self.assertEqual(result["palette_rows"], {1: 6, 2: 6})
