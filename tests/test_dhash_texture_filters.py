# -*- coding: utf-8 -*-
"""dHash 贴图 filter 导出单测。

夹具照 tests/test_dualset_merged_mode.py / test_efmi_merge_active_collision.py：
用 fake 包骨架加载真实 ``ui/universal/efmi.py``、``common/m_ini_builder.py``、
``common/dhash_metadata.py``、``common/texture_metadata_helper.py``，不依赖 bpy。

覆盖：
1. 默认关：两个标记仍走原固定槽位行，且不产生任何 Filter 段。
2. 开启但 mark 无 dhash：不绑定、不产生 Filter。
3. 开启 + 单 mark：`ps-t[Filter_...] = ref` + 一段 `match_format`/`match_dhash`。
4. 同一节点两个子网格、dhash 不同：恰好 2 段、2 条绑定、名字不同、
   match_dhash 与输入逐字符一致、match_format 不带 DXGI_FORMAT_ 前缀。
5. 同一 dhash 的两个物体：只生成 1 段（按 (role, format, dhash) 去重）。
6. 手写两个 dict → normalize → 导出：dhash 不同 → 2 段；同 dhash → 1 段。
7. 同一 token+role 出现不同 dhash：加后缀，段名仍不同。
8. normalize 契约：缺字段默认 slot、非法 dhash 归空、DXGI_FORMAT_ 前缀被去掉。
9. filter_name_for / role_for_mark_name / filter_section_lines 共享契约。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "dhash_texture_filters_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for _pkg_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.blueprint",
                  f"{PKG}.common", f"{PKG}.utils"):
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = []
    sys.modules[_pkg_name] = _pkg

# ---- fake bpy / 其余重模块：只提供名字，不实例化 ----
_install_module("bpy", data=types.SimpleNamespace(),
                types=types.SimpleNamespace(Object=object, Mesh=object))
_install_module(
    f"{PKG}.utils.json_utils",
    JsonUtils=_load_real(f"{PKG}.utils.json_utils_", "utils/json_utils.py").JsonUtils,
)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None
    ),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())


class _FakeGlobalProterties:
    """只实现 efmi 导出路径用到的三个开关，测试里直接改类属性。"""

    use_dhash = False
    forbid_auto = False
    rabbitfx = False

    @classmethod
    def use_dhash_texture(cls):
        return cls.use_dhash

    @classmethod
    def forbid_auto_texture_ini(cls):
        return cls.forbid_auto

    @classmethod
    def use_rabbitfx_slot(cls):
        return cls.rabbitfx


_install_module(f"{PKG}.common.global_properties", GlobalProterties=_FakeGlobalProterties)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(f"{PKG}.common.submesh_metadata", SubmeshMetadataResolver=object)
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
_install_module(f"{PKG}.blueprint.model", BluePrintModel=object)
_install_module(f"{PKG}.common.submesh_model", SubMeshModel=object)
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.ui.universal.export_helper", ExportHelper=types.SimpleNamespace())

# ---- 真实产品模块（顺序：dhash_metadata → texture_metadata_helper → m_ini_builder → efmi）----
_dhash = _load_real(f"{PKG}.common.dhash_metadata", "common/dhash_metadata.py")
normalize_dhash = _dhash.normalize_dhash
normalize_format = _dhash.normalize_format
normalize_binding_mode = _dhash.normalize_binding_mode
filter_name_for = _dhash.filter_name_for
filter_section_lines = _dhash.filter_section_lines
role_for_mark_name = _dhash.role_for_mark_name

_tmh = _load_real(f"{PKG}.common.texture_metadata_helper", "common/texture_metadata_helper.py")
TextureMarkUpInfo = _tmh.TextureMarkUpInfo
TextureMetadataResolver = _tmh.TextureMetadataResolver

_real_ini_builder = _load_real(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
M_IniBuilder = _real_ini_builder.M_IniBuilder
M_IniSection = _real_ini_builder.M_IniSection
M_SectionType = _real_ini_builder.M_SectionType

_efmi = _load_real(f"{PKG}.ui.universal.efmi", "ui/universal/efmi.py")
ExportEFMI = _efmi.ExportEFMI


# ---------------------------------------------------------------------------
# 合成标记 / 子网格 / DrawIB
# ---------------------------------------------------------------------------

def make_mark(mark_name="DiffuseMap", dhash="", fmt="", binding_mode="filter",
              slot="ps-t3", filename=None):
    mark = TextureMarkUpInfo()
    mark.mark_name = mark_name
    mark.mark_type = "Slot"
    mark.mark_slot = slot
    mark.mark_filename = filename or ("aaaa-6-0-" + mark_name + ".dds")
    mark.source_dhash = dhash
    mark.source_format = fmt
    mark.binding_mode = binding_mode
    return mark


class _FakeSubmesh:
    def __init__(self, unique_str):
        self.unique_str = unique_str


class _FakeDrawIB:
    """一个节点挂多个子网格，每个子网格一份贴图标记。"""

    def __init__(self, entries):
        self._entries = entries
        self.submesh_model_list = [entry[0] for entry in entries]

    def get_submesh_texture_markup_info_list(self, submesh_model):
        for submesh, marks in self._entries:
            if submesh is submesh_model:
                return marks
        return []


def make_exporter(entries):
    exporter = object.__new__(ExportEFMI)
    exporter.drawib_model_list = [_FakeDrawIB(entries)]
    exporter.submesh_model_list = [entry[0] for entry in entries]
    return exporter


def run_export_path(exporter):
    """按 generate_ini_file 的绑定/ResourceTexture 循环顺序跑 dHash 两段。

    返回 (绑定段, builder)；未命中 dHash 的标记走原固定槽位行，和线上一致。
    """
    binding_section = M_IniSection(M_SectionType.TextureOverrideIB)
    for drawib_model in exporter.drawib_model_list:
        for submesh_model in drawib_model.submesh_model_list:
            for mark in drawib_model.get_submesh_texture_markup_info_list(submesh_model):
                if getattr(mark, "mark_type", "") != "Slot":
                    continue
                if exporter._try_append_dhash_binding(binding_section, submesh_model, mark):
                    continue
                binding_section.append(mark.mark_slot + " = " + mark.get_resource_name())
    builder = M_IniBuilder()
    builder.append_section(binding_section)
    exporter._append_dhash_filter_sections(builder)
    return binding_section, builder


def texture_section_lines(builder):
    lines = []
    for section in builder.ini_section_list:
        if section.SectionType != M_SectionType.ResourceTexture:
            continue
        lines.extend(section.SectionLineList)
    return lines


def filter_headers(builder):
    return [line for line in texture_section_lines(builder) if line.startswith("[Filter_")]


def filter_bindings(binding_section):
    return [line for line in binding_section.SectionLineList if line.startswith("ps-t[Filter_")]


def filter_line_value(builder, key):
    values = []
    for line in texture_section_lines(builder):
        if line.startswith(key + " = "):
            values.append(line.split(" = ", 1)[1])
    return values


class TestDHashTextureFilters(unittest.TestCase):
    def setUp(self):
        _FakeGlobalProterties.use_dhash = False
        _FakeGlobalProterties.forbid_auto = False
        _FakeGlobalProterties.rabbitfx = False

    def tearDown(self):
        self.setUp()

    # 1. 默认关：两行固定槽位原样，零 Filter 段 -------------------------------
    def test_default_off_keeps_fixed_slot_bindings_and_emits_no_filter(self):
        submesh = _FakeSubmesh("aaaa-6-0")
        marks = [
            make_mark("DiffuseMap", "0123456789abcdef", "BC7_UNORM_SRGB", "filter", "ps-t3"),
            make_mark("LightMap", "fedcba9876543210", "BC7_UNORM_SRGB", "filter", "ps-t5"),
        ]
        exporter = make_exporter([(submesh, marks)])

        for mark in marks:
            self.assertFalse(exporter._try_append_dhash_binding(M_IniSection(
                M_SectionType.TextureOverrideIB), submesh, mark))

        binding_section, builder = run_export_path(exporter)
        self.assertEqual(
            binding_section.SectionLineList,
            [
                "ps-t3 = Resource-aaaa-6-0-DiffuseMap",
                "ps-t5 = Resource-aaaa-6-0-LightMap",
            ],
        )
        self.assertEqual(texture_section_lines(builder), [])
        self.assertEqual(filter_headers(builder), [])

    # 2. 开启但无 dhash：不绑定、不产生 Filter ------------------------------
    def test_enabled_without_dhash_emits_nothing(self):
        _FakeGlobalProterties.use_dhash = True
        submesh = _FakeSubmesh("aaaa-6-0")
        mark = make_mark("DiffuseMap", "", "BC7_UNORM_SRGB", "filter", "ps-t3")
        exporter = make_exporter([(submesh, [mark])])

        binding_section, builder = run_export_path(exporter)
        self.assertEqual(
            binding_section.SectionLineList,
            ["ps-t3 = Resource-aaaa-6-0-DiffuseMap"],
        )
        self.assertEqual(filter_headers(builder), [])
        self.assertEqual(getattr(exporter, "_dhash_filters", {}), {})

    # 3. 开启 + 单 mark：绑定行 + filter 段 --------------------------------
    def test_enabled_single_mark_emits_filter(self):
        _FakeGlobalProterties.use_dhash = True
        submesh = _FakeSubmesh("aaaa-6-0")
        mark = make_mark(
            "DiffuseMap", "0123456789abcdef", "DXGI_FORMAT_BC7_UNORM_SRGB", "filter", "ps-t3"
        )
        exporter = make_exporter([(submesh, [mark])])

        binding_section, builder = run_export_path(exporter)
        self.assertEqual(
            binding_section.SectionLineList,
            ["ps-t[Filter_aaaa_6_0_Diffuse] = ref Resource-aaaa-6-0-DiffuseMap"],
        )
        self.assertEqual(
            texture_section_lines(builder),
            [
                "[Filter_aaaa_6_0_Diffuse]",
                "match_format = BC7_UNORM_SRGB",
                "match_dhash = 0123456789abcdef",
                "",
            ],
        )
        self.assertEqual(len(exporter._dhash_filters), 1)

    # 4. 同一节点、两个子网格、dhash 不同：2 段 / 2 条绑定 ------------------
    def test_two_objects_two_dhashes_emit_two_filters(self):
        _FakeGlobalProterties.use_dhash = True
        dhash_a = "0123456789abcdef"
        dhash_b = "fedcba9876543210"
        submesh_a = _FakeSubmesh("aaaa-6-0")
        submesh_b = _FakeSubmesh("bbbb-6-0")
        exporter = make_exporter([
            (submesh_a, [make_mark("DiffuseMap", dhash_a, "BC7_UNORM_SRGB", "filter", "ps-t3")]),
            (submesh_b, [make_mark("DiffuseMap", dhash_b, "BC7_UNORM_SRGB", "filter", "ps-t3")]),
        ])

        binding_section, builder = run_export_path(exporter)
        headers = filter_headers(builder)
        bindings = filter_bindings(binding_section)
        self.assertEqual(len(headers), 2)
        self.assertEqual(len(bindings), 2)
        self.assertEqual(len(set(headers)), 2)
        self.assertEqual(sorted(filter_line_value(builder, "match_dhash")), sorted([dhash_a, dhash_b]))
        for fmt in filter_line_value(builder, "match_format"):
            self.assertNotIn("DXGI_FORMAT_", fmt)
            self.assertEqual(fmt, "BC7_UNORM_SRGB")

    # 5. 同一 dhash 的两个物体：只生成 1 段（不同子网格也去重）--------------
    def test_same_dhash_two_objects_dedupe_to_one_filter(self):
        _FakeGlobalProterties.use_dhash = True
        same_dhash = "0123456789abcdef"
        submesh_a = _FakeSubmesh("aaaa-6-0")
        submesh_b = _FakeSubmesh("bbbb-6-0")
        exporter = make_exporter([
            (submesh_a, [make_mark("DiffuseMap", same_dhash, "BC7_UNORM_SRGB", "filter", "ps-t3")]),
            (submesh_b, [make_mark("DiffuseMap", same_dhash, "BC7_UNORM_SRGB", "filter", "ps-t3")]),
        ])

        _, builder = run_export_path(exporter)
        headers = filter_headers(builder)
        self.assertEqual(len(headers), 1)
        self.assertEqual(filter_line_value(builder, "match_dhash"), [same_dhash])
        self.assertEqual(len(exporter._dhash_filters), 1)

    # 6. 手写 dict → normalize → 导出：同一节点、两个子网格、dhash 不同 -----
    def test_normalized_dicts_two_dhashes_emit_two_filters(self):
        _FakeGlobalProterties.use_dhash = True
        dhash_a = "0123456789abcdef"
        dhash_b = "fedcba9876543210"
        raw_a = {
            "MarkName": "DiffuseMap",
            "MarkType": "Slot",
            "MarkSlot": "ps-t3",
            "MarkFileName": "aaaa-6-0-DiffuseMap.dds",
            "dhash": dhash_a,
            "format": "DXGI_FORMAT_BC7_UNORM_SRGB",
            "binding_mode": "filter",
        }
        raw_b = dict(raw_a)
        raw_b["dhash"] = dhash_b
        marks = TextureMetadataResolver.normalize_texture_markup_info_list([raw_a, raw_b])
        self.assertEqual(len(marks), 2)
        submesh_a = _FakeSubmesh("aaaa-6-0")
        submesh_b = _FakeSubmesh("bbbb-6-0")
        exporter = make_exporter([
            (submesh_a, [marks[0]]),
            (submesh_b, [marks[1]]),
        ])

        binding_section, builder = run_export_path(exporter)
        headers = filter_headers(builder)
        bindings = filter_bindings(binding_section)
        self.assertEqual(len(headers), 2)
        self.assertEqual(len(bindings), 2)
        self.assertEqual(len(set(headers)), 2)
        self.assertEqual(filter_line_value(builder, "match_dhash"), [dhash_a, dhash_b])
        for fmt in filter_line_value(builder, "match_format"):
            self.assertNotIn("DXGI_FORMAT_", fmt)
            self.assertEqual(fmt, "BC7_UNORM_SRGB")

    # 7. 手写 dict 同 dhash、两个子网格：只生成 1 段（去重）-----------------
    def test_normalized_dicts_same_dhash_dedupe_to_one_filter(self):
        _FakeGlobalProterties.use_dhash = True
        same_dhash = "0123456789abcdef"
        raw_a = {
            "MarkName": "DiffuseMap",
            "MarkType": "Slot",
            "MarkSlot": "ps-t3",
            "MarkFileName": "aaaa-6-0-DiffuseMap.dds",
            "dhash": same_dhash,
            "format": "DXGI_FORMAT_BC7_UNORM_SRGB",
            "binding_mode": "filter",
        }
        raw_b = dict(raw_a)
        marks = TextureMetadataResolver.normalize_texture_markup_info_list([raw_a, raw_b])
        submesh_a = _FakeSubmesh("aaaa-6-0")
        submesh_b = _FakeSubmesh("bbbb-6-0")
        exporter = make_exporter([
            (submesh_a, [marks[0]]),
            (submesh_b, [marks[1]]),
        ])

        _, builder = run_export_path(exporter)
        self.assertEqual(len(filter_headers(builder)), 1)
        self.assertEqual(filter_line_value(builder, "match_dhash"), [same_dhash])
        self.assertEqual(len(exporter._dhash_filters), 1)

    # 8. 同一 token+role、不同 dhash：加后缀，段名不同 ----------------------
    def test_same_token_different_dhash_gets_distinct_names(self):
        _FakeGlobalProterties.use_dhash = True
        submesh = _FakeSubmesh("aaaa-6-0")
        exporter = make_exporter([(submesh, [
            make_mark("DiffuseMap", "0123456789abcdef", "BC7_UNORM_SRGB", "filter", "ps-t3"),
            make_mark("DiffuseMap", "fedcba9876543210", "BC7_UNORM_SRGB", "filter", "ps-t7"),
        ])])

        binding_section, builder = run_export_path(exporter)
        headers = filter_headers(builder)
        self.assertEqual(len(headers), 2)
        self.assertEqual(len(set(headers)), 2)
        self.assertEqual(len(filter_bindings(binding_section)), 2)

    # 9. normalize 契约 -----------------------------------------------------
    def test_normalize_contract(self):
        self.assertEqual(normalize_binding_mode(""), "slot")
        self.assertEqual(normalize_binding_mode("Filter"), "filter")
        self.assertEqual(normalize_binding_mode("dhash"), "filter")
        self.assertEqual(normalize_binding_mode("slot"), "slot")

        self.assertEqual(normalize_dhash(" 0123456789ABCDEF "), "0123456789abcdef")
        self.assertEqual(normalize_dhash("not-a-dhash"), "")
        self.assertEqual(normalize_dhash("0123456789abcde"), "")

        self.assertEqual(normalize_format(" dxgi_format_bc7_unorm_srgb "), "BC7_UNORM_SRGB")
        self.assertEqual(normalize_format("BC7_UNORM_SRGB"), "BC7_UNORM_SRGB")
        self.assertEqual(normalize_format("bad format!"), "")

        old_style = {
            "MarkName": "DiffuseMap",
            "MarkType": "Slot",
            "MarkSlot": "ps-t3",
            "MarkFileName": "aaaa-6-0-DiffuseMap.dds",
        }
        normalized = TextureMetadataResolver.normalize_texture_markup_info_list([old_style])
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0].binding_mode, "slot")
        self.assertEqual(normalized[0].source_dhash, "")
        self.assertEqual(normalized[0].source_format, "")

        with_dhash = dict(old_style)
        with_dhash.update({
            "dhash": "0123456789abcdef",
            "format": "dxgi_format_bc7_unorm_srgb",
            "binding_mode": "Filter",
        })
        normalized_dhash = TextureMetadataResolver.normalize_texture_markup_info_list([with_dhash])[0]
        self.assertEqual(normalized_dhash.source_dhash, "0123456789abcdef")
        self.assertEqual(normalized_dhash.source_format, "BC7_UNORM_SRGB")
        self.assertEqual(normalized_dhash.binding_mode, "filter")

        # 旧 5 键相同、dhash 不同：不能被去重成一条。
        other = dict(with_dhash)
        other["dhash"] = "fedcba9876543210"
        deduped = TextureMetadataResolver._dedupe_texture_markup_info_list(
            TextureMetadataResolver.normalize_texture_markup_info_list([with_dhash, other])
        )
        self.assertEqual(len(deduped), 2)

    # 10. 共享命名/角色/段体契约 --------------------------------------------
    def test_filter_name_and_section_lines_contract(self):
        self.assertEqual(role_for_mark_name("DiffuseMap"), "Diffuse")
        self.assertEqual(role_for_mark_name("Diffuse"), "Diffuse")
        self.assertEqual(role_for_mark_name("LightMap"), "Light")
        self.assertEqual(role_for_mark_name("NormalMap"), "Normal")
        self.assertEqual(role_for_mark_name("MaterialMap"), "Material")
        self.assertEqual(role_for_mark_name("diffusemap"), "Diffuse")
        self.assertEqual(role_for_mark_name("自定义 角色"), "Tex")
        self.assertEqual(role_for_mark_name("My Map"), "My_Map")

        self.assertEqual(
            filter_name_for("LOD0.aaa-0", role_for_mark_name("DiffuseMap")),
            "Filter_LOD0_aaa_0_Diffuse",
        )
        self.assertEqual(filter_name_for("x", "自定义 角色"), "Filter_x_Tex")
        self.assertEqual(
            filter_section_lines("DXGI_FORMAT_BC7_UNORM_SRGB", "0123456789ABCDEF"),
            [
                "match_format = BC7_UNORM_SRGB",
                "match_dhash = 0123456789abcdef",
            ],
        )


if __name__ == "__main__":
    unittest.main()
