"""Multiple visible DrawIBs must render the SO that received their geometry."""

import ast
import re
import struct
import sys
import unittest
from unittest import mock

if "test_zzmi_merged_skeleton_ini" in sys.modules:
    base = sys.modules["test_zzmi_merged_skeleton_ini"]
else:
    from tests import test_zzmi_merged_skeleton_ini as base


def _unity_vlr_method():
    """Use the real native VLR emitter instead of the fixture's no-op parent."""
    path = base.REPO_ROOT / "ui/universal/unity.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "add_unity_vs_texture_override_vlr_section"
    )
    namespace = {
        "M_IniBuilder": base._FakeIniBuilder,
        "M_IniSection": base._FakeIniSection,
        "M_SectionType": base._FakeIniSectionType,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[method.name]


class ZZMINativeSOTests(base._ZZMIGroup3RedirectFixture, unittest.TestCase):
    def _visible_parts(self, mutual_weights=False):
        exporter, models = self._group3_exporter(
            merged_vertex_count=20660,
            target_real_vertices=378,
            target_registered=True,
        )
        for model in models:
            model.draw_number = exporter._drawib_exported_vertex_count(model.draw_ib)
            model.category_buffer_dict["Texcoord"] = bytes(model.draw_number * 20)
            for submesh in model.submesh_model_list:
                submesh.vertex_count = model.draw_number
                submesh.original_vertex_count = 7246 if model is models[0] else 20274
                submesh.drawcall_model_list[0].index_count = 3
                model.submesh_ib_dict[submesh.unique_str] = struct.pack(
                    "<3I", 0, max(model.draw_number - 1, 0), 0
                )
        if mutual_weights:
            self._register_obj("LOD0.a23aa8a3-42759-0", [79, 184])
        self._build_and_apply_plan(exporter)
        return exporter, models

    def _vb_text(self, exporter, model):
        builder = base._FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        return "\n".join(base._all_builder_lines(builder))

    def test_visible_parts_keep_native_capacity_indices_and_uv(self):
        exporter, models = self._visible_parts()
        with mock.patch.object(
            base._FakeExportUnity,
            "add_unity_vs_texture_override_vlr_section",
            _unity_vlr_method(),
        ):
            for model in models[:2]:
                with self.subTest(draw_ib=model.draw_ib):
                    builder = base._FakeIniBuilder()
                    exporter.add_unity_vs_texture_override_vlr_section(builder, model)
                    vlr = "\n".join(base._all_builder_lines(builder))
                    capacity = int(re.search(r"override_vertex_count = (\d+)", vlr)[1])
                    self.assertEqual(capacity, model.draw_number)
                    builder = base._FakeIniBuilder()
                    exporter.add_unity_vs_texture_override_ib_sections(builder, model)
                    render = "\n".join(base._all_builder_lines(builder))
                    base_vertices = re.findall(r"drawindexed = \d+,\d+,(\d+)", render)
                    self.assertEqual(base_vertices, ["0"])
                    self.assertNotIn("ResourceZZRedirectTexcoord", render)
                    if model is models[0]:
                        self.assertIn(f"vb1 = Resource{model.draw_ib}Texcoord", render)
                    self.assertLess(model.draw_number - 1, capacity)

    def test_replay_writes_the_visible_carriers_own_so(self):
        exporter, models = self._visible_parts()
        carrier, target = models[:2]
        alias = "ResourceZZRedirectSO_G3_s1"
        carrier_text = self._vb_text(exporter, carrier)
        target_text = self._vb_text(exporter, target)
        self.assertIn(f"{alias} = ref so0", carrier_text)
        self.assertNotIn(f"{alias} = ref so0", target_text)
        for text in (carrier_text, target_text):
            self.assertIn(
                f"so0 = ref {alias}\n"
                f"    vb2 = Resource{carrier.draw_ib}Blend\n"
                f"    vb0 = Resource{carrier.draw_ib}Position\n"
                "    draw = 20660, 0",
                text,
            )
            self.assertNotIn("run = CustomShaderZZMISkin_", text)

    def test_two_merged_hosts_do_not_overwrite_each_others_so_alias(self):
        exporter, models = self._visible_parts(mutual_weights=True)
        aliases = []
        for model in models[:2]:
            text = self._vb_text(exporter, model)
            captured = set(re.findall(r"(ResourceZZRedirectSO_\w+_s1) = ref so0", text))
            self.assertEqual(len(captured), 1)
            aliases.append(captured.pop())
        self.assertNotEqual(*aliases)
        builder = base._FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        resources = "\n".join(base._all_builder_lines(builder))
        for model, alias in zip(models, aliases):
            self.assertIn(f"[{alias}]", resources)
            for consumer in models[:2]:
                self.assertIn(
                    f"so0 = ref {alias}\n"
                    f"    vb2 = Resource{model.draw_ib}Blend\n"
                    f"    vb0 = Resource{model.draw_ib}Position\n"
                    f"    draw = {model.draw_number}, 0",
                    self._vb_text(exporter, consumer),
                )

    def test_multiple_carriers_keep_separate_native_sos_with_a_stub_target(self):
        exporter, models = self._group3_exporter()
        other = models[2].submesh_model_list[0]
        other.index_vertex_id_dict = list(range(12))
        self._register_obj(other.unique_str, [79, 184, 235])
        carriers, targets, _ = self._build_and_apply_plan(exporter)
        self.assertEqual(carriers, {})
        self.assertEqual(targets, {})


if __name__ == "__main__":
    unittest.main()
