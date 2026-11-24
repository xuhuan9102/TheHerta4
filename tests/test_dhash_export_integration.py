# -*- coding: utf-8 -*-
"""dHash Filter 段走真实 ExportEFMI.generate_ini_file 的集成回归。

使用 tests/test_shader_replace_export_paths.py 的 fake 模型骨架 + 真实
common/m_ini_builder.py；断言 Filter 段确实由 generate_ini_file 发射、类型为
ResourceTexture、绑定在 TextureOverrideIB 段体、且 match_* 不会落进绘制段。
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dhash_integration_m_ini_builder", ROOT / "common" / "m_ini_builder.py"
)
real_ini = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = real_ini
spec.loader.exec_module(real_ini)


class _RecordingBuilder(real_ini.M_IniBuilder):
    last = None

    def __init__(self):
        super().__init__()
        _RecordingBuilder.last = self


class _Mark:
    mark_type = "Slot"

    def __init__(self, name, dhash, fmt, slot, filename):
        self.mark_name = name
        self.mark_slot = slot
        self.mark_filename = filename
        self.source_dhash = dhash
        self.source_format = fmt
        self.binding_mode = "filter"

    def get_resource_name(self):
        return "Resource-" + self.mark_filename.split(".")[0]


class DHashGenerateIniIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("DHASH_RUN_INTEGRATION"), "standalone integration only")
    def test_generate_ini_file_emits_top_level_filter(self):
        from tests import test_shader_replace_export_paths as shader_tests
        case = shader_tests.ShaderReplaceExportPathTests(
            methodName="test_efmi_generate_ini_file_emits_shader_replace_sections"
        )
        case.setUp()
        em = case.efmi_module
        em.M_IniBuilder = _RecordingBuilder
        em.M_IniSection = real_ini.M_IniSection
        em.M_SectionType = real_ini.M_SectionType
        em.GlobalProterties.use_dhash_texture = lambda: True
        em.GlobalProterties.forbid_auto_texture_ini = lambda: False
        em.GlobalProterties.use_rabbitfx_slot = lambda: False
        em.GlobalProterties.generate_branch_mod_gui = lambda: False
        em.M_IniHelper.is_slot_binding_mark_type = lambda _t: True

        drawcall = shader_tests._FakeDrawCall("body")
        submesh = shader_tests._FakeSubmesh([drawcall])
        submesh.workspace_unique_str = submesh.unique_str
        submesh.match_draw_ib = "drawhash"
        drawib = shader_tests._FakeDrawIBModel([drawcall])
        drawib.submesh_model_list = [submesh]
        marks = [_Mark("DiffuseMap", "0123456789abcdef", "BC7_UNORM_SRGB", "ps-t3",
                       "drawhash-12-56-DiffuseMap.dds")]
        drawib.get_submesh_texture_markup_info_list = lambda _sm: marks

        exporter = em.ExportEFMI.__new__(em.ExportEFMI)
        blueprint_model = case._make_blueprint_model()
        exporter.blueprint_model = blueprint_model
        exporter.submesh_model_list = [submesh]
        exporter.drawib_model_list = [drawib]
        exporter.cross_ib_info_dict = {}
        exporter.cross_ib_method_dict = {}
        exporter.has_cross_ib = False
        exporter.cross_ib_mapping_objects = {}
        exporter.cross_ib_vb_condition_mapping = {}
        exporter.cross_ib_source_to_target_dict = {}
        exporter.cross_ib_object_vb_condition = {}
        exporter.cross_ib_target_info = {}
        exporter.cross_ib_match_mode = "IB_HASH"
        exporter.cross_ib_object_names = set()
        exporter.shader_replace_info_list = []
        exporter.shader_replace_object_names = set()
        exporter.shader_replace_object_info_map = {}
        exporter.has_shader_replace = False
        exporter.has_merged_skeleton = False
        exporter.merged_skeleton_components = []
        exporter.merged_skeleton_component_id_dict = {}
        exporter._integrate_object_swap_ini_hook = lambda _b: None

        with tempfile.TemporaryDirectory(prefix="dhash_generate_ini_") as tmp:
            em.GlobalConfig.path_generate_mod_folder = lambda: tmp
            em.GlobalConfig.path_generate_mod_ini = lambda suffix="": str(Path(tmp) / "Workspace.ini")
            em.GlobalConfig.path_generatemod_buffer_folder = lambda: str(Path(tmp) / "Buffer") + "/"
            em.GlobalConfig.get_workspace_name = lambda: "Workspace"
            exporter.generate_ini_file()
            ini_path = Path(tmp) / "Workspace.ini"
            self.assertTrue(ini_path.exists())

        sections = _RecordingBuilder.last.ini_section_list
        texture_override = next(s for s in sections if s.SectionType == real_ini.M_SectionType.TextureOverrideIB)
        filter_sections = [
            s for s in sections
            if s.SectionType == real_ini.M_SectionType.ResourceTexture
            and any(line.startswith("[Filter_") for line in s.SectionLineList)
        ]
        self.assertEqual(len(filter_sections), 1)
        self.assertTrue(any(line.startswith("ps-t[Filter_") for line in texture_override.SectionLineList))
        self.assertFalse(any(
            line.strip().startswith(("match_format", "match_dhash", "match_confidence"))
            for line in texture_override.SectionLineList
        ))
        filter_index = sections.index(filter_sections[0])
        first_resource_texture = next(
            i for i, s in enumerate(sections)
            if s.SectionType == real_ini.M_SectionType.ResourceTexture
        )
        self.assertGreater(filter_index, first_resource_texture)


if __name__ == "__main__":
    unittest.main()
