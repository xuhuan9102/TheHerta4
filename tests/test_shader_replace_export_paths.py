import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeIniSection:
    def __init__(self, section_type):
        self.SectionType = section_type
        self.SectionName = ""
        self.SectionLineList = []

    def append(self, line):
        self.SectionLineList.append(line)

    def extend(self, lines):
        # 与 common/m_ini_builder.M_IniSection 同 API（阴影门控发射侧用 extend）
        for line in lines:
            self.append(line)

    def new_line(self):
        self.SectionLineList.append("")

    def empty(self):
        return not any(line not in ("", "\n") for line in self.SectionLineList)


class _FakeIniBuilder:
    def __init__(self):
        self.ini_section_list = []
        self.saved_path = None

    def append_section(self, section):
        self.ini_section_list.append(section)

    def save_to_file(self, path):
        self.saved_path = path


class _FakeSectionType:
    TextureOverrideIB = "TextureOverrideIB"
    TextureOverrideVB = "TextureOverrideVB"
    ResourceBuffer = "ResourceBuffer"
    ResourceTexture = "ResourceTexture"
    Constants = "Constants"
    Key = "Key"
    Present = "Present"
    ShaderReplace = "ShaderReplace"
    CrossIBPresent = "CrossIBPresent"
    ResourceID = "ResourceID"
    # 本轮 ZZMI/EFMI 骨骼合并新依赖的段类型
    CommandList = "CommandList"
    MergedSkeleton = "MergedSkeleton"
    TextureOverrideVertexLimitRaise = "TextureOverrideVertexLimitRaise"
    # EFMI 阴影 pass 单投射源门控依赖的段类型（阴影 PS 的 ShaderOverride 注册段）
    ShaderOverride = "ShaderOverride"


class _FakeExportUnity:
    def __init__(self, blueprint_model):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []

    def add_unity_vs_texture_override_vlr_section(self, ini_builder, drawib_model):
        return None

    def add_unity_vs_texture_override_vb_sections(self, ini_builder, drawib_model):
        return None

    def add_unity_vs_resource_vb_sections(self, ini_builder, drawib_model):
        return None

    def add_resource_texture_sections(self, ini_builder, drawib_model):
        return None

    def generate_buffer_files(self, *_args, **_kwargs):
        return None


class _FakeDrawCall:
    def __init__(
        self,
        obj_name,
        *,
        condition="",
        shader_replace_info_list=None,
        shader_replace_info_resolved=False,
    ):
        self.obj_name = obj_name
        self.vertex_count = 77
        self.index_count = 12
        self.index_offset = 34
        self.match_draw_ib = "drawhash"
        self.match_first_index = "56"
        self._condition = condition
        self.shader_replace_info_list = list(shader_replace_info_list or [])
        self.shader_replace_info_resolved = shader_replace_info_resolved

    def get_condition_str(self):
        return self._condition

    def get_drawindexed_str(self, _offset_dict=None):
        return "drawindexed = 12,34,0"

    def get_drawindexed_instanced_str(self, _offset_dict=None):
        return "drawindexedinstanced = 12,INSTANCE_COUNT,34,0,FIRST_INSTANCE"


class _FakeSubmesh:
    def __init__(self, drawcalls):
        self.unique_str = "drawhash-12-56"
        self.match_draw_ib = "drawhash"
        self.match_first_index = "56"
        self.match_index_count = "12"
        self.drawcall_model_list = drawcalls
        self.category_buffer_dict = {"Position": [1], "Texcoord": [2], "Blend": [3]}
        self.d3d11_game_type = types.SimpleNamespace(
            CategoryExtractSlotDict={"Position": "vb0", "Texcoord": "vb1", "Blend": "vb2"},
            CategoryStrideDict={"Position": 40, "Texcoord": 8, "Blend": 32},
        )


class _FakeDrawIBModel:
    def __init__(self, drawcalls):
        self.draw_ib = "drawhash"
        self.draw_ib_alias = "drawhash"
        self.draw_number = 123
        self.d3d11GameType = types.SimpleNamespace(
            OrderedCategoryNameList=["Position", "Texcoord", "Blend"],
            CategoryDrawCategoryDict={"Position": "Position", "Texcoord": "Texcoord", "Blend": "Blend"},
            CategoryExtractSlotDict={"Position": "vb0", "Texcoord": "vb1", "Blend": "vb2"},
            CategoryStrideDict={"Position": 40, "Texcoord": 8, "Blend": 32},
        )
        self.category_hash_dict = {"Position": "p", "Texcoord": "t", "Blend": "b"}
        self.submesh_model_list = [_FakeSubmesh(drawcalls)]
        self.submesh_ib_dict = {"drawhash-12-56": [0, 1, 2]}
        self.obj_name_draw_offset = {}
        self.d3d11_game_type = self.submesh_model_list[0].d3d11_game_type

    def get_submesh_texture_override_suffix(self, submesh_model):
        return submesh_model.unique_str.replace("-", "_")

    def get_submesh_ib_resource_name(self, submesh_model):
        return f"Resource_{submesh_model.unique_str.replace('-', '_')}_Index"

    def get_submesh_texture_markup_info_list(self, _submesh_model):
        return []


class ShaderReplaceExportPathTests(unittest.TestCase):
    def setUp(self):
        self.pkg = "_shader_replace_export_path_test_pkg"
        for package_name in (
            self.pkg,
            f"{self.pkg}.ui",
            f"{self.pkg}.ui.universal",
            f"{self.pkg}.common",
            f"{self.pkg}.utils",
            f"{self.pkg}.blueprint",
        ):
            package = _install_module(package_name)
            package.__path__ = []

        _install_module(
            f"{self.pkg}.common.global_config",
            GlobalConfig=types.SimpleNamespace(
                path_generate_mod_folder=lambda: "E:/mod",
                path_generatemod_buffer_folder=lambda: "E:/mod/Buffer/",
                get_workspace_name=lambda: "Workspace",
            ),
        )
        _install_module(
            f"{self.pkg}.common.global_properties",
            GlobalProterties=types.SimpleNamespace(
                forbid_auto_texture_ini=lambda: True,
                generate_branch_mod_gui=lambda: False,
                use_rabbitfx_slot=lambda: False,
                import_merged_vgmap=lambda: False,
                zzz_use_slot_fix=lambda: False,
            ),
        )
        _install_module(
            f"{self.pkg}.common.global_key_count_helper",
            GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
        )
        _install_module(
            f"{self.pkg}.common.m_ini_builder",
            M_IniBuilder=_FakeIniBuilder,
            M_IniSection=_FakeIniSection,
            M_SectionType=_FakeSectionType,
        )
        # zzmi.py / efmi.py 新增依赖：utils.json_utils（真实模块，仅 JSON 读写）
        _load_module(f"{self.pkg}.utils.json_utils", "utils/json_utils.py")

        self.shader_replace_section_calls = []

        def _record_shader_replace_sections(**kwargs):
            self.shader_replace_section_calls.append(kwargs)

        def _resolve_draw_call_shader_infos(
            draw_call,
            shader_replace_object_names=None,
            shader_replace_object_info_map=None,
            shader_replace_info_list=None,
        ):
            direct_infos = list(getattr(draw_call, "shader_replace_info_list", []) or [])
            if getattr(draw_call, "shader_replace_info_resolved", False) or direct_infos:
                return direct_infos
            if draw_call.obj_name not in (shader_replace_object_names or set()):
                return []
            return list(
                (shader_replace_object_info_map or {}).get(draw_call.obj_name, [])
                or shader_replace_info_list
                or []
            )

        _install_module(
            f"{self.pkg}.common.m_ini_helper",
            M_IniHelper=types.SimpleNamespace(
                get_drawindexed_str_list=lambda drawcall_list, obj_name_draw_offset_dict=None, base_vertex=0: [
                    line
                    for item in drawcall_list
                    for line in (
                        f"; [mesh:{item.obj_name}] [vertex_count:{item.vertex_count}]",
                        (
                            f"drawindexed = {item.obj_name}"
                            if base_vertex == 0
                            else f"drawindexed = {item.obj_name},base={base_vertex}"
                        ),
                    )
                ],
                get_drawindexed_instanced_str_list=lambda drawcall_list, obj_name_draw_offset_dict=None: [
                    f"drawindexedinstanced = {item.obj_name}" for item in drawcall_list
                ],
                get_draw_call_shader_replace_info_list=_resolve_draw_call_shader_infos,
                get_shader_replace_run_logic=lambda info, ib_hash, first_index, component, index_count, index_offset, base_vertex=0: [
                    f"if ${info['name_prefix']}_ps_replace == 1",
                    f"    run = CustomShader_{info['name_prefix']}_{ib_hash}_{first_index}_{component}_{index_count}_{index_offset}_{base_vertex}_World",
                    "else",
                    f"    run = CustomShader_{info['name_prefix']}_{ib_hash}_{first_index}_{component}_{index_count}_{index_offset}_{base_vertex}_Normal",
                    "endif",
                ],
                build_draw_call_offset_map=lambda _drawib_models: {},
                add_shader_replace_sections=_record_shader_replace_sections,
                generate_hash_style_texture_ini=lambda **_kwargs: None,
                generate_shared_slot_style_texture_ini=lambda **_kwargs: None,
                move_slot_style_textures=lambda **_kwargs: None,
                add_branch_key_sections=lambda **_kwargs: None,
                add_shapekey_ini_sections=lambda **_kwargs: None,
                is_slot_binding_mark_type=lambda _mark_type: False,
            ),
        )
        _install_module(
            f"{self.pkg}.common.m_ini_helper_gui",
            M_IniHelperGUI=types.SimpleNamespace(add_branch_mod_gui_section=lambda **_kwargs: None),
        )
        _install_module(f"{self.pkg}.common.drawib_model", DrawIBModel=object)
        _install_module(f"{self.pkg}.common.draw_call_model", DrawCallModel=object)
        _install_module(f"{self.pkg}.common.m_key", M_Key=object)
        _install_module(f"{self.pkg}.common.logic_name", LogicName=types.SimpleNamespace())
        _install_module(f"{self.pkg}.common.workspace_helper", WorkSpaceHelper=types.SimpleNamespace())
        _install_module(f"{self.pkg}.common.submesh_model", SubMeshModel=object)
        _install_module(f"{self.pkg}.blueprint.model", BluePrintModel=object)
        _install_module(
            f"{self.pkg}.blueprint.export_helper",
            BlueprintExportHelper=types.SimpleNamespace(get_current_buffer_folder_name=lambda: "Buffer"),
        )
        _install_module(
            f"{self.pkg}.ui.universal.export_helper",
            ExportHelper=types.SimpleNamespace(parse_submesh_model_list_from_blueprint_model=lambda _bp: []),
        )
        _install_module(
            f"{self.pkg}.utils.timer_utils",
            TimerUtils=types.SimpleNamespace(start_stage=lambda *_args, **_kwargs: None, end_stage=lambda *_args, **_kwargs: None),
        )
        _install_module(f"{self.pkg}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
        _install_module(f"{self.pkg}.ui.universal.unity", ExportUnity=_FakeExportUnity)
        _install_module("bpy", context=types.SimpleNamespace())

        self.actual_m_ini_module = _load_module(
            f"{self.pkg}.common.m_ini_helper_actual",
            "common/m_ini_helper.py",
        )

        self.zzmi_module = _load_module(
            f"{self.pkg}.ui.universal.zzmi",
            "ui/universal/zzmi.py",
        )
        # EFMI 阴影门控判定/行构造模块（efmi.py 的 generate_ini_file 按需导入）
        self.shadow_gate_module = _load_module(
            f"{self.pkg}.ui.universal.efmi_shadow_gate",
            "ui/universal/efmi_shadow_gate.py",
        )
        self.efmi_module = _load_module(
            f"{self.pkg}.ui.universal.efmi",
            "ui/universal/efmi.py",
        )

    def _make_blueprint_model(self):
        return types.SimpleNamespace(
            cross_ib_info_dict={},
            cross_ib_method_dict={},
            cross_ib_mapping_method={},
            has_cross_ib=False,
            cross_ib_object_names=set(),
            cross_ib_mapping_objects={},
            cross_ib_vb_condition_mapping={},
            cross_ib_source_to_target_dict={},
            cross_ib_object_vb_condition={},
            cross_ib_target_info={},
            cross_ib_match_mode="IB_HASH",
            shader_replace_info_list=[{
                "name_prefix": "Rain",
                "toggle_key": "VK_F5",
                "component_index": 0,
                "shaders": [{"variant_name": "World", "shader_hash": "abcd", "env_value": 1, "shader_file_path": ""}],
            }],
            shader_replace_object_names={"mesh_sr"},
            shader_replace_object_info_map={
                "mesh_sr": [{
                    "name_prefix": "Rain",
                    "toggle_key": "VK_F5",
                    "component_index": 0,
                    "shaders": [{"variant_name": "World", "shader_hash": "abcd", "env_value": 1, "shader_file_path": ""}],
                }]
            },
            has_shader_replace=True,
            keyname_mkey_dict={},
            ordered_draw_obj_data_model_list=[_FakeDrawCall("mesh_sr"), _FakeDrawCall("mesh_normal")],
        )

    def test_zzmi_shader_replace_replaces_drawindexed_for_marked_objects(self):
        exporter = self.zzmi_module.ExportZZMI(self._make_blueprint_model())
        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_drawindexed_with_shader_replace(
            section,
            [_FakeDrawCall("mesh_sr"), _FakeDrawCall("mesh_normal")],
            {},
        )

        lines = section.SectionLineList
        self.assertIn("drawindexed = mesh_normal", lines)
        self.assertTrue(any("run = CustomShader_Rain_drawhash_56_0_12_34_0_World" in line for line in lines))
        self.assertFalse(any(line == "drawindexed = mesh_sr" for line in lines))

    def test_zzmi_redirect_uses_same_drawindexed_path_with_base_vertex(self):
        """合并网格重定向仍走统一输出路径：备注、base_vertex 和 shader replace 都保留。"""
        exporter = self.zzmi_module.ExportZZMI(self._make_blueprint_model())
        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_drawindexed_with_shader_replace(
            section,
            [_FakeDrawCall("mesh_sr"), _FakeDrawCall("mesh_normal")],
            {},
            base_vertex=123,
        )

        lines = section.SectionLineList
        self.assertIn("; [mesh:mesh_sr] [vertex_count:77]", lines)
        self.assertIn("; [mesh:mesh_normal] [vertex_count:77]", lines)
        self.assertIn("drawindexed = mesh_normal,base=123", lines)
        self.assertTrue(any("_34_123_World" in line for line in lines))

    def test_efmi_shader_replace_replaces_drawindexedinstanced_for_marked_objects(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        exporter.blueprint_model = self._make_blueprint_model()
        exporter.shader_replace_info_list = exporter.blueprint_model.shader_replace_info_list
        exporter.shader_replace_object_names = exporter.blueprint_model.shader_replace_object_names
        exporter.shader_replace_object_info_map = exporter.blueprint_model.shader_replace_object_info_map
        exporter.has_shader_replace = True

        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)
        exporter._append_drawindexed_instanced_with_shader_replace(
            section,
            [_FakeDrawCall("mesh_sr"), _FakeDrawCall("mesh_normal")],
            {},
        )

        lines = section.SectionLineList
        self.assertIn("drawindexedinstanced = mesh_normal", lines)
        self.assertTrue(any("run = CustomShader_Rain_drawhash_56_0_12_34_0_World" in line for line in lines))
        self.assertFalse(any(line == "drawindexedinstanced = mesh_sr" for line in lines))

    def test_efmi_cross_ib_uses_current_vs_hashes(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        exporter.has_cross_ib = True
        exporter._get_all_cross_ib_identifiers = lambda: {"source"}
        ini_builder = _FakeIniBuilder()

        exporter._add_cross_ib_present_section(ini_builder)

        lines = ini_builder.ini_section_list[0].SectionLineList
        self.assertNotIn("[Present]", lines)
        self.assertNotIn("ResourcePrev_SRV = ResourceFakeT0_SRV", lines)
        self.assertIn("[ResourcePrev_SRV]", lines)
        hashes = [line.removeprefix("hash = ") for line in lines if line.startswith("hash = ")]
        section_names = [line for line in lines if line.startswith("[ShaderOverridevs")]
        self.assertEqual(
            hashes,
            [
                "f11c7e1dbf876a69",
                "303f45d5266d0369",
                "7b3a141f99cd9b39",
                "1479b2b594b9c91a",
                "c6e55aaa8f4b3218",
                "784f11ae11c97112",
                "f1b10202c73c72c3",
                "12ad3cc5f56f853c",
                "86cb3bc0a3e2e013",
                "906a3976f3e33cfb",
                "0ba16985f9f74f8d",
                "06c94dd56f447210",
                "f47b1f797f5831d0",
            ],
        )
        self.assertEqual(
            section_names,
            [f"[ShaderOverridevs{index}]" for index in range(1000, 1013)],
        )
        self.assertIn("[CustomShader_ExtractCaptureCB1]", lines)
        capture_section_index = lines.index("[CustomShader_ExtractCaptureCB1]")
        self.assertEqual(
            lines[capture_section_index + 1],
            "vs = ./res/extract_capture_cb1_vs.hlsl",
        )

        referenced_hlsl = {
            Path(line.split("=", 1)[1].strip()).name
            for line in lines
            if line.lstrip().startswith(("vs =", "ps =", "cs =")) and ".hlsl" in line
        }
        expected_hlsl = {
            "extract_cb1_ps.hlsl",
            "extract_cb1_vs.hlsl",
            "extract_capture_cb1_vs.hlsl",
            "record_bones_cs.hlsl",
            "redirect_cb1_cs.hlsl",
        }
        self.assertEqual(referenced_hlsl, expected_hlsl)
        source_dir = Path(__file__).resolve().parents[1] / "Toolset"
        self.assertTrue(all((source_dir / filename).is_file() for filename in expected_hlsl))

    def test_efmi_cross_ib_splits_capture_cb1_from_cb2_replay(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        drawcall = _FakeDrawCall("cross_obj")
        exporter.cross_ib_source_to_target_dict = {"source": ["target"]}
        exporter._split_drawcalls_by_cross_ib = lambda *_args, **_kwargs: ([drawcall], [])
        exporter._group_drawcalls_by_cross_ib_target = (
            lambda *_args, **_kwargs: {
                ("target", "if vs == 200 || vs == 201 || vs == 204"): [drawcall]
            }
        )
        exporter._append_drawindexed_instanced_with_shader_replace = (
            lambda section, *_args, **_kwargs: section.append("drawindexedinstanced = test")
        )

        source_lines = exporter._generate_cross_ib_block_for_source(
            "source", [drawcall], source_ib_key="source", target_ib_key="target"
        )

        self.assertIn("if vs == 200", source_lines)
        self.assertIn("    run = CustomShader_ExtractCaptureCB1", source_lines)
        self.assertIn("    vs-cb1 = ResourceFakeCB1_source", source_lines)
        self.assertIn("if vs == 201 || vs == 204", source_lines)
        self.assertIn("    run = CustomShader_ExtractCB1", source_lines)
        self.assertIn("    vs-cb2 = ResourceFakeCB1_source", source_lines)
        self.assertEqual(source_lines.count("drawindexedinstanced = test"), 2)
        self.assertIn("post vs-cb1 = null", source_lines)
        self.assertIn("post vs-cb2 = null", source_lines)

        exporter.cross_ib_match_mode = "IB_HASH"
        exporter._find_source_submesh_by_ib_key = lambda _key: types.SimpleNamespace(
            unique_str="source-12-0",
            drawcall_model_list=[drawcall],
        )
        exporter._find_source_drawib_by_ib_key = lambda _key: types.SimpleNamespace(
            obj_name_draw_offset={}
        )
        exporter._get_vb_condition_for_object = lambda *_args, **_kwargs: "if vs == 202"
        target_section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_target_cross_ib_blocks(target_section, ["source_0"], "target_0")

        self.assertIn("    vs-cb2 = ResourceFakeCB1_source", target_section.SectionLineList)
        self.assertFalse(any("vs-cb1" in line for line in target_section.SectionLineList))

    def test_efmi_cross_ib_single_capture_filter_uses_cb1_only(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)

        self.assertEqual(
            exporter._get_source_cross_ib_variants("if vs == 200"),
            [("if vs == 200", "CustomShader_ExtractCaptureCB1", 1)],
        )
        self.assertEqual(
            exporter._get_source_cross_ib_variants("if vs == 201 || vs == 204"),
            [("if vs == 201 || vs == 204", "CustomShader_ExtractCB1", 2)],
        )
        self.assertEqual(exporter._get_source_cross_ib_variants(""), [])

        exporter.cross_ib_match_mode = "IB_HASH"
        exporter._find_source_submesh_by_ib_key = lambda _key: types.SimpleNamespace(
            unique_str="source-12-0",
            drawcall_model_list=[_FakeDrawCall("cross_obj")],
        )
        exporter._find_source_drawib_by_ib_key = lambda _key: types.SimpleNamespace(
            obj_name_draw_offset={}
        )
        exporter._split_drawcalls_by_cross_ib = lambda *_args, **_kwargs: (
            [_FakeDrawCall("cross_obj")],
            [],
        )
        exporter._get_vb_condition_for_object = lambda *_args, **_kwargs: ""
        exporter._append_drawindexed_instanced_with_shader_replace = (
            lambda section, *_args, **_kwargs: section.append("unexpected draw")
        )
        target_section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_target_cross_ib_blocks(target_section, ["source_0"], "target_0")

        self.assertEqual(target_section.SectionLineList, [])

    def test_efmi_cross_ib_extract_shader_reads_cb2(self):
        repo_root = Path(__file__).resolve().parents[1]

        for relative_path in (
            "Toolset/extract_cb1_vs.hlsl",
        ):
            shader = (repo_root / relative_path).read_text(encoding="utf-8")
            self.assertIn("cbuffer CB2 : register(b2)", shader)
            self.assertIn("asuint(cb2_data[id])", shader)
            self.assertNotIn("register(b1)", shader)
            self.assertNotIn("cb1_data", shader)

        for relative_path in (
            "Toolset/extract_capture_cb1_vs.hlsl",
        ):
            shader = (repo_root / relative_path).read_text(encoding="utf-8")
            self.assertIn("cbuffer CaptureCB1 : register(b1)", shader)
            self.assertIn("asuint(capture_cb1_data[id])", shader)
            self.assertNotIn("register(b2)", shader)

    def test_efmi_cross_ib_refreshes_existing_extract_shader(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        original_mod_folder = self.efmi_module.GlobalConfig.path_generate_mod_folder

        with tempfile.TemporaryDirectory() as directory:
            export_root = Path(directory)
            extract_shader = export_root / "res" / "extract_cb1_vs.hlsl"
            extract_shader.parent.mkdir(parents=True)
            extract_shader.write_text("cbuffer CB1 : register(b1) {}\n", encoding="utf-8")
            capture_shader = export_root / "res" / "extract_capture_cb1_vs.hlsl"
            capture_shader.write_text("stale capture shader\n", encoding="utf-8")
            self.efmi_module.GlobalConfig.path_generate_mod_folder = lambda: str(export_root)
            try:
                exporter._copy_cross_ib_hlsl_files()
            finally:
                self.efmi_module.GlobalConfig.path_generate_mod_folder = original_mod_folder

            refreshed = extract_shader.read_text(encoding="utf-8")
            refreshed_capture = capture_shader.read_text(encoding="utf-8")
            copied_hlsl = {path.name for path in (export_root / "res").glob("*.hlsl")}

        self.assertIn("cbuffer CB2 : register(b2)", refreshed)
        self.assertNotIn("register(b1)", refreshed)
        self.assertIn("cbuffer CaptureCB1 : register(b1)", refreshed_capture)
        self.assertNotIn("stale capture shader", refreshed_capture)
        self.assertEqual(
            copied_hlsl,
            {
                "extract_cb1_ps.hlsl",
                "extract_cb1_vs.hlsl",
                "extract_capture_cb1_vs.hlsl",
                "record_bones_cs.hlsl",
                "redirect_cb1_cs.hlsl",
            },
        )


    def test_efmi_generate_ini_file_emits_shader_replace_sections(self):
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        blueprint_model = self._make_blueprint_model()
        exporter.blueprint_model = blueprint_model
        exporter.submesh_model_list = []
        exporter.drawib_model_list = []
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
        exporter.shader_replace_info_list = blueprint_model.shader_replace_info_list
        exporter.shader_replace_object_names = blueprint_model.shader_replace_object_names
        exporter.shader_replace_object_info_map = blueprint_model.shader_replace_object_info_map
        exporter.has_shader_replace = True
        exporter._integrate_object_swap_ini_hook = lambda _ini_builder: None

        exporter.generate_ini_file()

        self.assertEqual(len(self.shader_replace_section_calls), 1)
        call = self.shader_replace_section_calls[0]
        self.assertEqual(call["shader_replace_info_list"], blueprint_model.shader_replace_info_list)
        self.assertEqual(call["shader_replace_object_names"], blueprint_model.shader_replace_object_names)
        self.assertEqual(call["draw_call_models"], blueprint_model.ordered_draw_obj_data_model_list)
        self.assertTrue(call["use_instanced_draw"])

    def test_shader_replace_validation_rejects_duplicate_prefixes(self):
        infos = [
            {"name_prefix": "Rain", "shaders": [{"variant_name": "World"}]},
            {"name_prefix": "rain", "shaders": [{"variant_name": "NonWorld"}]},
        ]

        with self.assertRaisesRegex(ValueError, "名称前缀.*重复"):
            self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(infos)

    def test_shader_replace_validation_allows_same_variant_in_multiple_groups(self):
        infos = [{
            "name_prefix": "Rain",
            "shaders": [
                {"variant_name": "World", "shader_hash": "aa"},
                {"variant_name": "world", "shader_hash": "AA"},
            ],
        }]

        self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(infos)

    def test_shader_replace_validation_rejects_reserved_normal_variant(self):
        infos = [{
            "name_prefix": "Rain",
            "shaders": [{"variant_name": "normal"}],
        }]

        with self.assertRaisesRegex(ValueError, "保留名称"):
            self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(infos)

    def test_shader_replace_validation_rejects_hash_shared_by_different_variants_and_multiline_key(self):
        duplicate_hashes = [{
            "name_prefix": "Rain",
            "shaders": [
                {"variant_name": "World", "shader_hash": "AABB"},
                {"variant_name": "NonWorld", "shader_hash": "aabb"},
            ],
        }]
        multiline_key = [{
            "name_prefix": "Rain",
            "toggle_key": "VK_F5\ntype = hold",
            "shaders": [{"variant_name": "World"}],
        }]

        with self.assertRaisesRegex(ValueError, "哈希.*不同变体"):
            self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(duplicate_hashes)
        with self.assertRaisesRegex(ValueError, "快捷键.*换行"):
            self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(multiline_key)

    def test_shader_replace_validation_rejects_invalid_identifiers_and_hashes(self):
        invalid_prefix = [{"name_prefix": "Rain Effect", "shaders": [{"variant_name": "World"}]}]
        invalid_variant = [{"name_prefix": "Rain", "shaders": [{"variant_name": "World]"}]}]
        invalid_hash = [{
            "name_prefix": "Rain",
            "shaders": [{"variant_name": "World", "shader_hash": "not-a-hash"}],
        }]

        with self.assertRaisesRegex(ValueError, "名称前缀.*非法"):
            self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(invalid_prefix)
        with self.assertRaisesRegex(ValueError, "变体名称.*非法"):
            self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(invalid_variant)
        with self.assertRaisesRegex(ValueError, "哈希.*非法"):
            self.actual_m_ini_module.M_IniHelper._validate_shader_replace_info_list(invalid_hash)

    def test_shader_replace_duplicate_variants_generate_independent_switch_groups(self):
        info = {
            "name_prefix": "Rain",
            "toggle_key": "VK_F5",
            "component_index": 0,
            "shaders": [
                {"variant_name": "World", "shader_hash": "aa", "shader_file_path": ""},
                {"variant_name": "NonWorld", "shader_hash": "bb", "shader_file_path": ""},
                {"variant_name": "World", "shader_hash": "aa", "shader_file_path": ""},
                {"variant_name": "NonWorld", "shader_hash": "bb", "shader_file_path": ""},
            ],
        }
        draw_call = _FakeDrawCall(
            "mesh",
            shader_replace_info_list=[info],
            shader_replace_info_resolved=True,
        )
        builder = _FakeIniBuilder()

        with tempfile.TemporaryDirectory() as temp_dir:
            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info],
                shader_replace_object_names={"mesh"},
                draw_call_models=[draw_call],
                mod_export_path=temp_dir,
            )

        sections = {section.SectionName: section.SectionLineList for section in builder.ini_section_list}
        self.assertIn("$Rain_ps_replace = 0,1,2,", sections["KeyToggle_Rain"])
        self.assertIn(
            "if $Rain_ps_replace == 1 || $Rain_ps_replace == 2",
            sections["ShaderOverride_RainEnvA_World"],
        )
        self.assertIn("CustomShader_Rain_drawhash_56_0_12_34_0_World", sections)
        self.assertIn("CustomShader_Rain_drawhash_56_0_12_34_0_World_Group2", sections)
        self.assertIn("CustomShader_Rain_drawhash_56_0_12_34_0_NonWorld_Group2", sections)

        run_lines = self.actual_m_ini_module.M_IniHelper.get_shader_replace_run_logic(
            info, "drawhash", "56", 0, 12, 34
        )
        self.assertIn("else if $Rain_ps_replace == 2", run_lines)
        self.assertTrue(any(line.endswith("_World_Group2") for line in run_lines))
        self.assertTrue(any(line.endswith("_NonWorld_Group2") for line in run_lines))

    def test_shader_replace_incomplete_group_resets_unmatched_variant_state(self):
        info = {
            "name_prefix": "Rain",
            "toggle_key": "VK_F5",
            "component_index": 0,
            "shaders": [
                {"variant_name": "World", "shader_hash": "aa", "shader_file_path": ""},
                {"variant_name": "NonWorld", "shader_hash": "bb", "shader_file_path": ""},
                {"variant_name": "World", "shader_hash": "aa", "shader_file_path": ""},
            ],
        }
        builder = _FakeIniBuilder()

        with tempfile.TemporaryDirectory() as temp_dir:
            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info],
                shader_replace_object_names={"mesh"},
                draw_call_models=[_FakeDrawCall("mesh", shader_replace_info_list=[info])],
                mod_export_path=temp_dir,
            )

        sections = {section.SectionName: section.SectionLineList for section in builder.ini_section_list}
        nonworld_override = sections["ShaderOverride_RainEnvA_NonWorld"]
        self.assertIn("if $Rain_ps_replace == 1", nonworld_override)
        self.assertIn("else", nonworld_override)
        self.assertIn("    $Rain_env_a = 0", nonworld_override)

        run_lines = self.actual_m_ini_module.M_IniHelper.get_shader_replace_run_logic(
            info, "drawhash", "56", 0, 12, 34
        )
        group2_index = run_lines.index("else if $Rain_ps_replace == 2")
        self.assertIn(
            "        run = CustomShader_Rain_drawhash_56_0_12_34_0_Normal",
            run_lines[group2_index:],
        )

    def test_shader_files_with_same_basename_export_to_unique_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_a = root / "A" / "shader.txt"
            source_b = root / "B" / "shader.txt"
            source_a.parent.mkdir()
            source_b.parent.mkdir()
            source_a.write_text("shader-a", encoding="utf-8")
            source_b.write_text("shader-b", encoding="utf-8")
            info_a = {
                "name_prefix": "RainA",
                "toggle_key": "",
                "component_index": 0,
                "shaders": [{
                    "variant_name": "World",
                    "shader_hash": "aa",
                    "env_value": 1,
                    "shader_file_path": str(source_a),
                }],
            }
            info_b = {
                "name_prefix": "RainB",
                "toggle_key": "",
                "component_index": 0,
                "shaders": [{
                    "variant_name": "World",
                    "shader_hash": "bb",
                    "env_value": 1,
                    "shader_file_path": str(source_b),
                }],
            }
            draw_a = _FakeDrawCall("mesh_a", shader_replace_info_list=[info_a])
            draw_b = _FakeDrawCall("mesh_b", shader_replace_info_list=[info_b])
            builder = _FakeIniBuilder()

            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info_a, info_b],
                shader_replace_object_names={"mesh_a", "mesh_b"},
                draw_call_models=[draw_a, draw_b],
                mod_export_path=temp_dir,
            )

            self.assertEqual((root / "Shaders" / "RainA_World_aa.txt").read_text(encoding="utf-8"), "shader-a")
            self.assertEqual((root / "Shaders" / "RainB_World_bb.txt").read_text(encoding="utf-8"), "shader-b")
            key_sections = [s for s in builder.ini_section_list if s.SectionName.startswith("KeyToggle_")]
            self.assertEqual(key_sections, [])
            custom_lines = [
                line
                for section in builder.ini_section_list
                if section.SectionName.startswith("CustomShader_")
                for line in section.SectionLineList
            ]
            self.assertIn("ps = ./Shaders/RainA_World_aa.txt", custom_lines)
            self.assertIn("ps = ./Shaders/RainB_World_bb.txt", custom_lines)

    def test_missing_shader_file_aborts_section_generation(self):
        info = {
            "name_prefix": "Rain",
            "toggle_key": "",
            "component_index": 0,
            "shaders": [{
                "variant_name": "World",
                "shader_hash": "aa",
                "env_value": 1,
                "shader_file_path": "Z:/missing/shader.txt",
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "文件不存在"):
                self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                    ini_builder=_FakeIniBuilder(),
                    shader_replace_info_list=[info],
                    shader_replace_object_names={"mesh"},
                    draw_call_models=[_FakeDrawCall("mesh", shader_replace_info_list=[info])],
                    mod_export_path=temp_dir,
                )

    def test_shader_replace_generation_normalizes_whitespace(self):
        info = {
            "name_prefix": " Rain ",
            "toggle_key": "   ",
            "component_index": 0,
            "shaders": [{
                "variant_name": " World ",
                "shader_hash": " aa ",
                "env_value": 1,
                "shader_file_path": "",
            }],
        }
        builder = _FakeIniBuilder()

        with tempfile.TemporaryDirectory() as temp_dir:
            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info],
                shader_replace_object_names={"mesh"},
                draw_call_models=[_FakeDrawCall("mesh", shader_replace_info_list=[info])],
                mod_export_path=temp_dir,
            )

        section_names = [section.SectionName for section in builder.ini_section_list]
        all_lines = [line for section in builder.ini_section_list for line in section.SectionLineList]
        self.assertNotIn("KeyToggle_Rain", section_names)
        self.assertIn("ShaderOverride_RainEnvA_World", section_names)
        self.assertIn("hash = aa", all_lines)
        self.assertIn("global persist $Rain_ps_replace = 0", all_lines)
        self.assertTrue(any(name.startswith("CustomShader_Rain_") for name in section_names))
        self.assertFalse(any(" Rain " in name or " World " in name for name in section_names))

        run_lines = self.actual_m_ini_module.M_IniHelper.get_shader_replace_run_logic(
            info,
            "drawhash",
            "56",
            0,
            12,
            34,
        )
        self.assertTrue(any("CustomShader_Rain_drawhash_56_0_12_34_0_World" in line for line in run_lines))

    def test_draw_call_shader_info_takes_precedence_over_object_fallback(self):
        info_a = {"name_prefix": "RainA", "component_index": 0, "shaders": []}
        info_b = {"name_prefix": "RainB", "component_index": 0, "shaders": []}
        exporter = self.zzmi_module.ExportZZMI(self._make_blueprint_model())
        exporter.shader_replace_info_list = [info_a, info_b]
        exporter.shader_replace_object_names = {"shared"}
        exporter.shader_replace_object_info_map = {"shared": [info_b]}
        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_drawindexed_with_shader_replace(
            section,
            [
                _FakeDrawCall("shared", shader_replace_info_list=[info_a]),
                _FakeDrawCall("shared", shader_replace_info_list=[info_b]),
            ],
            {},
        )

        joined = "\n".join(section.SectionLineList)
        self.assertIn("$RainA_ps_replace", joined)
        self.assertIn("$RainB_ps_replace", joined)

    def test_same_export_name_keeps_explicit_non_shader_chain_normal(self):
        info = {
            "name_prefix": "Rain",
            "component_index": 0,
            "shaders": [{"variant_name": "World", "env_value": 1}],
        }
        exporter = self.zzmi_module.ExportZZMI(self._make_blueprint_model())
        exporter.shader_replace_info_list = [info]
        exporter.shader_replace_object_names = {"shared"}
        exporter.shader_replace_object_info_map = {"shared": [info]}
        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_drawindexed_with_shader_replace(
            section,
            [
                _FakeDrawCall(
                    "shared",
                    shader_replace_info_list=[],
                    shader_replace_info_resolved=True,
                ),
                _FakeDrawCall(
                    "shared",
                    shader_replace_info_list=[info],
                    shader_replace_info_resolved=True,
                ),
            ],
            {},
        )

        self.assertEqual(section.SectionLineList.count("drawindexed = shared"), 1)
        self.assertEqual(
            sum("CustomShader_Rain_" in line and "_World" in line for line in section.SectionLineList),
            1,
        )

    def test_efmi_same_export_name_keeps_explicit_non_shader_chain_normal(self):
        info = {
            "name_prefix": "Rain",
            "component_index": 0,
            "shaders": [{"variant_name": "World", "env_value": 1}],
        }
        exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        exporter.has_shader_replace = True
        exporter.shader_replace_info_list = [info]
        exporter.shader_replace_object_names = {"shared"}
        exporter.shader_replace_object_info_map = {"shared": [info]}
        section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)

        exporter._append_drawindexed_instanced_with_shader_replace(
            section,
            [
                _FakeDrawCall(
                    "shared",
                    shader_replace_info_list=[],
                    shader_replace_info_resolved=True,
                ),
                _FakeDrawCall(
                    "shared",
                    shader_replace_info_list=[info],
                    shader_replace_info_resolved=True,
                ),
            ],
            {},
        )

        self.assertEqual(
            section.SectionLineList.count("drawindexedinstanced = shared"),
            1,
        )
        self.assertEqual(
            sum("CustomShader_Rain_" in line and "_World" in line for line in section.SectionLineList),
            1,
        )

    def test_shader_replace_preserves_interleaved_draw_call_order(self):
        info = {
            "name_prefix": "Rain",
            "component_index": 0,
            "shaders": [{"variant_name": "World", "env_value": 1}],
        }
        shader_draw = _FakeDrawCall(
            "shader",
            shader_replace_info_list=[info],
            shader_replace_info_resolved=True,
        )
        normal_draw = _FakeDrawCall(
            "normal",
            shader_replace_info_list=[],
            shader_replace_info_resolved=True,
        )

        zzmi_exporter = self.zzmi_module.ExportZZMI(self._make_blueprint_model())
        zzmi_exporter.shader_replace_info_list = [info]
        zzmi_exporter.shader_replace_object_names = {"shader"}
        zzmi_exporter.shader_replace_object_info_map = {"shader": [info]}
        zzmi_section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)
        zzmi_exporter._append_drawindexed_with_shader_replace(
            zzmi_section,
            [shader_draw, normal_draw],
            {},
        )

        efmi_exporter = self.efmi_module.ExportEFMI.__new__(self.efmi_module.ExportEFMI)
        efmi_exporter.has_shader_replace = True
        efmi_exporter.shader_replace_info_list = [info]
        efmi_exporter.shader_replace_object_names = {"shader"}
        efmi_exporter.shader_replace_object_info_map = {"shader": [info]}
        efmi_section = _FakeIniSection(_FakeSectionType.TextureOverrideIB)
        efmi_exporter._append_drawindexed_instanced_with_shader_replace(
            efmi_section,
            [shader_draw, normal_draw],
            {},
        )

        self.assertLess(
            zzmi_section.SectionLineList.index("; [mesh:shader] [vertex_count:77]"),
            zzmi_section.SectionLineList.index("drawindexed = normal"),
        )
        self.assertLess(
            efmi_section.SectionLineList.index("; [mesh:shader] [vertex_count:77]"),
            efmi_section.SectionLineList.index("drawindexedinstanced = normal"),
        )

    def test_shader_sections_use_final_drawib_offset(self):
        info = {
            "name_prefix": "Rain",
            "toggle_key": "",
            "component_index": 0,
            "shaders": [{
                "variant_name": "World",
                "shader_file_path": "",
                "shader_hash": "",
                "env_value": 1,
            }],
        }
        draw_call = _FakeDrawCall(
            "mesh",
            shader_replace_info_list=[info],
            shader_replace_info_resolved=True,
        )
        builder = _FakeIniBuilder()

        with tempfile.TemporaryDirectory() as tmpdir:
            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info],
                shader_replace_object_names={"mesh"},
                draw_call_models=[draw_call],
                mod_export_path=tmpdir,
                draw_call_offset_map={id(draw_call): 91},
            )

        section_names = {section.SectionName for section in builder.ini_section_list}
        self.assertIn("CustomShader_Rain_drawhash_56_0_12_91_0_World", section_names)
        self.assertNotIn("CustomShader_Rain_drawhash_56_0_12_34_0_World", section_names)

    def test_shader_sections_use_redirect_base_vertex_and_close_run_references(self):
        """重定向 draw 的 run 引用必须对应生成同一 base_vertex 的 CustomShader 段。"""
        info = {
            "name_prefix": "Rain",
            "toggle_key": "",
            "component_index": 0,
            "shaders": [{
                "variant_name": "World",
                "shader_file_path": "",
                "shader_hash": "",
                "env_value": 1,
            }],
        }
        draw_call = _FakeDrawCall(
            "mesh",
            shader_replace_info_list=[info],
            shader_replace_info_resolved=True,
        )
        builder = _FakeIniBuilder()

        with tempfile.TemporaryDirectory() as tmpdir:
            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info],
                shader_replace_object_names={"mesh"},
                draw_call_models=[draw_call],
                mod_export_path=tmpdir,
                draw_call_base_vertex_map={id(draw_call): 123},
            )

        sections = {section.SectionName: section.SectionLineList for section in builder.ini_section_list}
        target_name = "CustomShader_Rain_drawhash_56_0_12_34_123_World"
        self.assertIn(target_name, sections)
        self.assertIn("drawindexed = 12,34,123", sections[target_name])

    def test_shader_export_allows_source_file_already_at_destination(self):
        builder = _FakeIniBuilder()
        draw_call = _FakeDrawCall(
            "mesh",
            shader_replace_info_resolved=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            shaders_dir = Path(tmpdir) / "Shaders"
            shaders_dir.mkdir()
            shader_path = shaders_dir / "Rain_World_aa.txt"
            shader_path.write_text("shader", encoding="utf-8")
            info = {
                "name_prefix": "Rain",
                "toggle_key": "",
                "component_index": 0,
                "shaders": [{
                    "variant_name": "World",
                    "shader_file_path": str(shader_path),
                    "shader_hash": "aa",
                    "env_value": 1,
                }],
            }
            draw_call.shader_replace_info_list = [info]

            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info],
                shader_replace_object_names={"mesh"},
                draw_call_models=[draw_call],
                mod_export_path=tmpdir,
            )

            self.assertEqual(shader_path.read_text(encoding="utf-8"), "shader")

    def test_shader_constants_ignore_comments_and_reuse_real_declarations(self):
        constants = _FakeIniSection(_FakeSectionType.Constants)
        constants.SectionName = "Constants"
        constants.append("; global persist $Rain_ps_replace = 0")
        constants.append("global persist $Rain_env_a = 9")
        builder = _FakeIniBuilder()
        builder.append_section(constants)
        info = {
            "name_prefix": "Rain",
            "toggle_key": "",
            "component_index": 0,
            "shaders": [],
        }
        draw_call = _FakeDrawCall(
            "mesh",
            shader_replace_info_list=[info],
            shader_replace_info_resolved=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            self.actual_m_ini_module.M_IniHelper.add_shader_replace_sections(
                ini_builder=builder,
                shader_replace_info_list=[info],
                shader_replace_object_names={"mesh"},
                draw_call_models=[draw_call],
                mod_export_path=tmpdir,
            )

        self.assertIn("global persist $Rain_ps_replace = 0", constants.SectionLineList)
        self.assertEqual(
            sum(
                line.strip().lower().startswith("global persist $rain_env_a")
                for line in constants.SectionLineList
            ),
            1,
        )

if __name__ == "__main__":
    unittest.main()
