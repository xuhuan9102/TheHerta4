import importlib.util
import sys
import types
import unittest
from pathlib import Path

from tests import _real_modules


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_zzmi_cross_ib_ini_layout_test_pkg"
for package_name in (
    PKG,
    f"{PKG}.ui",
    f"{PKG}.ui.universal",
    f"{PKG}.common",
    f"{PKG}.utils",
):
    package = _install_module(package_name)
    package.__path__ = []

# 真实 common 子模块按 fake 包前缀注册（空 __path__ 假包解析不了相对导入）
_real_modules.register_real_common_modules(f"{PKG}.common")


class _FakeIniSection:
    def __init__(self, _section_type):
        self.SectionLineList = []

    def append(self, line):
        self.SectionLineList.append(line)

    def new_line(self):
        self.SectionLineList.append("")

    def empty(self):
        return not any(line not in ("", "\n") for line in self.SectionLineList)


class _FakeIniBuilder:
    def __init__(self):
        self.sections = []

    def append_section(self, section):
        self.sections.append(section)


class _FakeIniSectionType:
    TextureOverrideIB = "TextureOverrideIB"
    TextureOverrideVB = "TextureOverrideVB"
    ResourceBuffer = "ResourceBuffer"


class _FakeExportUnity:
    def __init__(self, blueprint_model):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []

    def add_unity_vs_resource_vb_sections(self, ini_builder, drawib_model):
        section = _FakeIniSection(_FakeIniSectionType.ResourceBuffer)
        section.append("[ResourceBase]")
        ini_builder.append_section(section)


_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        path_workspace_folder=lambda: "",
        path_generatemod_buffer_folder=lambda: "",
        path_generate_mod_folder=lambda: "",
        get_workspace_name=lambda: "",
    ),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        forbid_auto_texture_ini=lambda: True,
        zzz_use_slot_fix=lambda: False,
        generate_branch_mod_gui=lambda: False,
        import_merged_vgmap=lambda: False,
    ),
)

# zzmi.py 依赖链：bpy（假）+ 真实 bpy-free 模块按文件加载
_install_module("bpy")


def _load_real_module(qualname, relpath):
    path = Path(__file__).resolve().parents[1] / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_load_real_module(f"{PKG}.common.m_key", "common/m_key.py")
_load_real_module(f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py")
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(
    f"{PKG}.common.m_ini_helper",
    M_IniHelper=types.SimpleNamespace(
        is_slot_binding_mark_type=lambda _mark_type: False,
        get_drawindexed_str_list=lambda drawcall_list, obj_name_draw_offset_dict=None: [
            f"drawindexed = {item.obj_name}" for item in drawcall_list
        ],
        generate_hash_style_texture_ini=lambda **_kwargs: None,
        generate_shared_slot_style_texture_ini=lambda **_kwargs: None,
        move_slot_style_textures=lambda **_kwargs: None,
        add_branch_key_sections=lambda **_kwargs: None,
        add_shapekey_ini_sections=lambda **_kwargs: None,
    ),
)
_install_module(
    f"{PKG}.common.m_ini_helper_gui",
    M_IniHelperGUI=types.SimpleNamespace(add_branch_mod_gui_section=lambda **_kwargs: None),
)
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=_FakeIniBuilder,
    M_IniSection=_FakeIniSection,
    M_SectionType=_FakeIniSectionType,
)
_install_module(f"{PKG}.ui.universal.unity", ExportUnity=_FakeExportUnity)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(start_stage=lambda *_args, **_kwargs: None, end_stage=lambda *_args, **_kwargs: None),
)


module_path = Path(__file__).resolve().parents[1] / "ui" / "universal" / "zzmi.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.universal.zzmi", module_path)
zzmi_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = zzmi_module
spec.loader.exec_module(zzmi_module)


class _FakeDrawCall:
    def __init__(self, obj_name):
        self.obj_name = obj_name


class _FakeSubmesh:
    def __init__(self, unique_str, match_first_index, drawcall_names):
        self.unique_str = unique_str
        self.match_first_index = match_first_index
        self.drawcall_model_list = [_FakeDrawCall(name) for name in drawcall_names]


class _FakeGameType:
    OrderedCategoryNameList = ["Position", "Texcoord", "Blend"]
    CategoryDrawCategoryDict = {
        "Position": "Position",
        "Texcoord": "Texcoord",
        "Blend": "Blend",
    }
    CategoryExtractSlotDict = {
        "Position": "vb0",
        "Texcoord": "vb1",
        "Blend": "vb2",
    }
    CategoryStrideDict = {
        "Position": 40,
        "Texcoord": 8,
        "Blend": 32,
    }


class _FakeDrawIBModel:
    def __init__(self, draw_ib, submesh_model_list):
        self.draw_ib = draw_ib
        self.draw_ib_alias = draw_ib
        self.draw_number = 123
        self.d3d11GameType = _FakeGameType()
        self.category_hash_dict = {
            "Position": f"{draw_ib}_pos",
            "Texcoord": f"{draw_ib}_tex",
            "Blend": f"{draw_ib}_blend",
        }
        self.submesh_model_list = submesh_model_list
        self.submesh_ib_dict = {submesh.unique_str: [0, 1, 2] for submesh in submesh_model_list}
        self.obj_name_draw_offset = {}

    def get_submesh_texture_override_suffix(self, submesh_model):
        return submesh_model.unique_str.replace("-", "_")

    def get_submesh_ib_resource_name(self, submesh_model):
        return f"Resource_{submesh_model.unique_str.replace('-', '_')}_Index"

    def get_submesh_texture_markup_info_list(self, _submesh_model):
        return []


class ZZMICrossIBIniLayoutTests(unittest.TestCase):
    def _make_exporter(self, mapping_method):
        blueprint_model = types.SimpleNamespace(
            cross_ib_info_dict={"sourcehash_0": ["targethash_0"]},
            cross_ib_method_dict={"CrossIBNode": mapping_method},
            cross_ib_mapping_method={("sourcehash_0", "targethash_0"): mapping_method},
            has_cross_ib=True,
            cross_ib_object_names={"cross_obj"},
            keyname_mkey_dict={},
        )
        exporter = zzmi_module.ExportZZMI(blueprint_model)
        source_submesh = _FakeSubmesh("sourcehash-0", 0, ["cross_obj", "local_obj"])
        target_submesh = _FakeSubmesh("targethash-0", 0, ["target_local"])
        source_drawib_model = _FakeDrawIBModel("sourcehash", [source_submesh])
        target_drawib_model = _FakeDrawIBModel("targethash", [target_submesh])
        exporter.drawib_model_list = [source_drawib_model, target_drawib_model]
        return exporter, source_drawib_model, target_drawib_model

    def test_source_resource_body_section_is_followed_by_capture_override(self):
        exporter, source_drawib_model, _target_drawib_model = self._make_exporter(
            zzmi_module.ExportZZMI.CROSS_IB_METHOD_VB_COPY
        )
        ini_builder = _FakeIniBuilder()

        exporter.add_unity_vs_texture_override_ib_sections(ini_builder, source_drawib_model)

        lines = ini_builder.sections[0].SectionLineList
        resource_index = lines.index("[ResourceBodyVB_sourcehash_0]")
        capture_override_index = lines.index("[TextureOverride_sourcehash_0_copy]")
        original_override_index = lines.index("[TextureOverride_sourcehash_0]")
        self.assertLess(resource_index, capture_override_index)
        self.assertLess(capture_override_index, original_override_index)

    def test_source_cross_ib_capture_filters_out_instanced_draws(self):
        exporter, source_drawib_model, target_drawib_model = self._make_exporter(
            zzmi_module.ExportZZMI.CROSS_IB_METHOD_VB_COPY_CB1
        )

        source_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(source_builder, source_drawib_model)
        source_lines = source_builder.sections[0].SectionLineList

        capture_override_index = source_lines.index("[TextureOverride_sourcehash_0_copy]")
        original_override_index = source_lines.index("[TextureOverride_sourcehash_0]")
        match_first_index = source_lines.index("match_first_index = 0", capture_override_index)
        match_instance_index = source_lines.index("match_instance_count = 0", capture_override_index)
        capture_vb_index = source_lines.index("ResourceBodyVB_sourcehash_0 = copy vb0")
        capture_cb1_index = source_lines.index("ResourceCaptureCB1_sourcehash_0 = copy vs-cb1 unless_null")
        self.assertLess(capture_override_index, match_first_index)
        self.assertLess(match_first_index, match_instance_index)
        self.assertLess(match_instance_index, capture_vb_index)
        self.assertLess(match_instance_index, capture_cb1_index)
        self.assertLess(capture_cb1_index, original_override_index)
        self.assertNotIn("match_instance_count = 0", source_lines[original_override_index:])

        target_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(target_builder, target_drawib_model)
        target_lines = target_builder.sections[0].SectionLineList
        self.assertNotIn("match_instance_count = 0", target_lines)

    def test_cb1_method_captures_and_restores_vs_cb1(self):
        exporter, _source_drawib_model, target_drawib_model = self._make_exporter(
            zzmi_module.ExportZZMI.CROSS_IB_METHOD_VB_COPY_CB1
        )
        ini_builder = _FakeIniBuilder()

        exporter.add_unity_vs_texture_override_ib_sections(ini_builder, target_drawib_model)

        lines = ini_builder.sections[0].SectionLineList
        temp_index = lines.index("[ResourceTempCB1_targethash_0]")
        target_override_index = lines.index("[TextureOverride_targethash_0]")
        self.assertLess(temp_index, target_override_index)
        self.assertIn("vs-cb1 = ResourceCaptureCB1_sourcehash_0", lines)
        self.assertIn("ResourceTempCB1_targethash_0 = ref vs-cb1", lines)
        self.assertIn("vs-cb1 = ref ResourceTempCB1_targethash_0", lines)

    def test_so0_method_adds_so0_resource_and_uses_it_for_target_draw(self):
        exporter, source_drawib_model, target_drawib_model = self._make_exporter(
            zzmi_module.ExportZZMI.CROSS_IB_METHOD_VB_REF_SO0
        )

        vb_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(vb_builder, source_drawib_model)
        vb_lines = vb_builder.sections[0].SectionLineList
        self.assertIn("ResourceBodyVB0_sourcehash_0 = ref so0", vb_lines)

        resource_builder = _FakeIniBuilder()
        exporter.add_unity_vs_resource_vb_sections(resource_builder, source_drawib_model)
        resource_lines = resource_builder.sections[-1].SectionLineList
        self.assertIn("[ResourceBodyVB0_sourcehash_0]", resource_lines)

        ib_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(ib_builder, target_drawib_model)
        ib_lines = ib_builder.sections[0].SectionLineList
        self.assertIn("vb0 = ResourceBodyVB0_sourcehash_0", ib_lines)
        self.assertIn("vb3 = ResourceBodyVB0_sourcehash_0", ib_lines)


if __name__ == "__main__":
    unittest.main()
