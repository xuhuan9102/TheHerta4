# -*- coding: utf-8 -*-
"""材质转资源 / 材质转资源pro 的 per-object dHash 绑定契约。

覆盖：
1. 全局开关关闭时 ``create_resource_entry`` 返回旧 ``ps-tN = Resource...``，
   ``all_sections`` 里不出现任何 ``[Filter_...]``（默认关零变化）。
2. 全局开关开启但部件 ``binding_mode == "FIXED"``：仍旧写法。
3. 开启 + DHASH + slot_info 带合法 16 位 dhash/format：返回
   ``ps-t[Filter_...] = Resource...``，并在 ``all_sections`` **顶层**注册
   ``[Filter_...]``（``match_format`` 去掉 ``DXGI_FORMAT_`` 前缀，
   ``match_dhash`` 逐字符一致）。
4. 两个物体 dhash 不同 → 两个 Filter 段；dhash 相同 → 复用同一个段。
5. 同名不同 dhash 不互相覆盖（用 dhash 前 8 位加后缀）。
6. ``node_postprocess_object_texture`` 的引用行正则/改写同时支持
   ``ps-t3 = ...`` 与 ``ps-t[Filter_...] = ref ...``，且保留左值原文。
7. pro 节点按 target_item 注入 per-object binding_mode，用完清空。

装载方式沿用 ``tests/test_node_postprocess_material_htmi.py`` 的 fake-package
范式：只桩 bpy 与相对依赖，加载真实业务模块；``common/dhash_metadata.py`` 也
按真实文件加载（纯函数、不依赖 bpy），只有 ``common/global_properties.py``
用开关替身（避免把整个 RNA 属性组拖进测试）。
"""

import importlib.util
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest import mock

PKG = "_dhash_material_binding_test_pkg"
for _package_name in (
    PKG,
    f"{PKG}.blueprint",
    f"{PKG}.common",
    f"{PKG}.toolkit",
    f"{PKG}.utils",
    f"{PKG}.ui",
    f"{PKG}.ui.ntmi_modimp",
):
    _package = types.ModuleType(_package_name)
    _package.__path__ = []
    sys.modules[_package_name] = _package


# ---------------------------------------------------------------------------
# 假 bpy
# ---------------------------------------------------------------------------

_fake_bpy = types.ModuleType("bpy")
_fake_bpy.types = types.SimpleNamespace(
    PropertyGroup=object,
    Operator=object,
    Node=object,
    NodeSocket=object,
    UIList=object,
    Object=object,
)
_fake_bpy.props = types.SimpleNamespace(
    StringProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    FloatProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
    PointerProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: None,
)
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups={})
_fake_bpy.context = types.SimpleNamespace(
    scene=types.SimpleNamespace(texture_tools_props=types.SimpleNamespace()),
    window_manager=None,
)
_fake_bpy.path = types.SimpleNamespace(abspath=lambda value: value)
_fake_bpy.utils = types.SimpleNamespace(
    register_class=lambda _cls: None, unregister_class=lambda _cls: None
)
sys.modules["bpy"] = _fake_bpy


# ---------------------------------------------------------------------------
# 全局总开关替身：测试里用 _DHASH_FLAG["enabled"] 翻转
# ---------------------------------------------------------------------------

_DHASH_FLAG = {"enabled": False}


class _FakeGlobalProterties:
    @classmethod
    def use_dhash_texture(cls):
        return bool(_DHASH_FLAG["enabled"])


_global_properties_module = types.ModuleType(f"{PKG}.common.global_properties")
_global_properties_module.GlobalProterties = _FakeGlobalProterties
sys.modules[_global_properties_module.__name__] = _global_properties_module


# ---------------------------------------------------------------------------
# 其余相对依赖桩
# ---------------------------------------------------------------------------

_prefix_cache_state = {"props": {}}
_prefix_cache_module = types.ModuleType(f"{PKG}.ui.ntmi_modimp.prefix_property_cache")
_prefix_cache_module.get_prefix_record_props = lambda name: dict(
    _prefix_cache_state["props"].get(str(name or ""), {})
)
_prefix_cache_module.has_prefix_record = lambda name: bool(
    _prefix_cache_state["props"].get(str(name or ""))
)
sys.modules[_prefix_cache_module.__name__] = _prefix_cache_module

_base_module = types.ModuleType(f"{PKG}.blueprint.node_postprocess_base")
_base_module.SSMTNode_PostProcess_Base = type(
    "_FakePostProcessBase",
    (object,),
    {
        "AUTO_APPENDED_SECTION_MARKERS": (),
        "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
        "split_anim_driver_block_content": classmethod(lambda cls, content: ("", content)),
    },
)
sys.modules[_base_module.__name__] = _base_module

_global_config_module = types.ModuleType(f"{PKG}.common.global_config")
_global_config_module.GlobalConfig = types.SimpleNamespace(logic_name="ZZMI")
sys.modules[_global_config_module.__name__] = _global_config_module

_logic_name_module = types.ModuleType(f"{PKG}.common.logic_name")
_logic_name_module.LogicName = types.SimpleNamespace(
    EFMI="EFMI", NTEMI="NTEMI", ZZMI="ZZMI", HIMI="HIMI"
)
sys.modules[_logic_name_module.__name__] = _logic_name_module

_dds_module = types.ModuleType(f"{PKG}.toolkit.tt_dds_conversion")
_dds_module.convert_texture_to_dds_cached = lambda *_args, **_kwargs: (True, "")
_dds_module.is_dds_file = lambda path: str(path or "").lower().endswith(".dds")
sys.modules[_dds_module.__name__] = _dds_module

_log_module = types.ModuleType(f"{PKG}.utils.log_utils")
_log_module.LOG = types.SimpleNamespace(
    debug=lambda *_args, **_kwargs: None,
    info=lambda *_args, **_kwargs: None,
    warning=lambda *_args, **_kwargs: None,
)
sys.modules[_log_module.__name__] = _log_module

ROOT = Path(__file__).resolve().parents[1]
sys.modules[f"{PKG}.toolkit"].__path__ = [str(ROOT / "toolkit")]


def _load_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.{module_name}", ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# 命名/段体共用真实契约（纯函数，不依赖 bpy），保证与 EFMI 侧生成同样的 Filter 名。
_load_module("common.dhash_metadata", Path("common") / "dhash_metadata.py")


def _load_blueprint_module(module_name):
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.blueprint.{module_name}", ROOT / "blueprint" / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fx_namespace = _load_blueprint_module("fx_namespace")
node_postprocess_material = _load_blueprint_module("node_postprocess_material")
node_postprocess_object_texture = _load_blueprint_module("node_postprocess_object_texture")
node_postprocess_custom_material_assign = _load_blueprint_module(
    "node_postprocess_custom_material_assign"
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

class _FakeMaterial:
    def __init__(self, name="DiffuseMap_Body"):
        self.name = name


def _make_resource_node():
    node_cls = node_postprocess_material.SSMTNode_PostProcess_MaterialBase
    node = object.__new__(node_cls)
    node.material_to_resource_override = False
    node.export_textures_as_dds = False
    node._report_texture_progress = lambda: None
    node.get_texture_from_material = lambda material: types.SimpleNamespace(
        filepath="fake.png"
    )
    node._build_material_signature = lambda material: ("sig", material.name)
    node._material_resource_stem = lambda material: material.name
    node.copy_texture_file = lambda *args, **kwargs: "{}.dds".format(args[2].name)
    return node


def _obj(name):
    return types.SimpleNamespace(name=name)


def _slot_info(mark_name="DiffuseMap", dhash="0123456789abcdef",
               fmt="DXGI_FORMAT_BC7_UNORM"):
    return {
        "mark_name": mark_name,
        "mark_slot": "ps-t2",
        "mark_hash": "deadbeefdeadbeef",
        "mark_filename": "body-DiffuseMap.dds",
        "source_path": "C:/workspace/body-DiffuseMap.dds",
        "source_dhash": dhash,
        "source_format": fmt,
    }


def _filter_keys(sections):
    return [
        key
        for key in sections
        if str(key).startswith("[Filter_") and str(key).endswith("]")
    ]


class DHashMaterialBindingTests(unittest.TestCase):
    def setUp(self):
        _DHASH_FLAG["enabled"] = False
        node_postprocess_material._material_resource_cache.clear()

    def tearDown(self):
        _DHASH_FLAG["enabled"] = False
        node_postprocess_material._material_resource_cache.clear()

    # -- a. 默认关 ---------------------------------------------------------
    def test_default_off_keeps_legacy_line_and_registers_no_filter(self):
        node = _make_resource_node()
        sections = OrderedDict()
        line = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("LOD0.abcd-1-0.Body"),
            slot_info=_slot_info(),
            binding_mode="DHASH",
        )
        self.assertEqual(line, "ps-t2 = Resource_DiffuseMap_Body")
        self.assertEqual(_filter_keys(sections), [])
        self.assertFalse(any("match_dhash" in str(body) for body in sections.values()))

    # -- b. 开启 + FIXED ---------------------------------------------------
    def test_enabled_fixed_keeps_legacy_line(self):
        _DHASH_FLAG["enabled"] = True
        node = _make_resource_node()
        sections = OrderedDict()
        line = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("LOD0.abcd-1-0.Body"),
            slot_info=_slot_info(),
            binding_mode="FIXED",
        )
        self.assertEqual(line, "ps-t2 = Resource_DiffuseMap_Body")
        self.assertEqual(_filter_keys(sections), [])

    # -- c. 开启 + DHASH ---------------------------------------------------
    def test_enabled_dhash_registers_top_level_filter_section(self):
        _DHASH_FLAG["enabled"] = True
        node = _make_resource_node()
        sections = OrderedDict()
        line = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("LOD0.abcd-1-0.Body"),
            slot_info=_slot_info(),
            binding_mode="DHASH",
        )
        filter_name = "Filter_LOD0_abcd_1_0_Body_Diffuse"
        self.assertEqual(
            line, "ps-t[{}] = Resource_DiffuseMap_Body".format(filter_name)
        )
        filter_key = "[" + filter_name + "]"
        self.assertEqual(_filter_keys(sections), [filter_key])
        self.assertEqual(
            sections[filter_key],
            ["match_format = BC7_UNORM", "match_dhash = 0123456789abcdef"],
        )
        # 资源段仍只有 filename；Filter 是顶层段，不是 TextureOverride 段体里的行。
        self.assertEqual(
            sections["[Resource_DiffuseMap_Body]"],
            ["filename = Textures/DiffuseMap_Body.dds"],
        )
        self.assertNotIn("match_dhash", "\n".join(sections["[Resource_DiffuseMap_Body]"]))

    def test_plain_dhash_and_format_keys_are_accepted(self):
        _DHASH_FLAG["enabled"] = True
        node = _make_resource_node()
        sections = OrderedDict()
        line = node.create_resource_entry(
            _FakeMaterial("NormalMap_Body"),
            "ps-t3",
            "Textures",
            sections,
            obj=_obj("Body"),
            slot_info={
                "mark_name": "NormalMap",
                "dhash": "fedcba9876543210",
                "format": "DXGI_FORMAT_BC5_UNORM",
            },
            binding_mode="DHASH",
        )
        self.assertIn("Filter_Body_Normal", line)
        self.assertEqual(
            sections["[Filter_Body_Normal]"],
            ["match_format = BC5_UNORM", "match_dhash = fedcba9876543210"],
        )

    def test_enabled_dhash_with_invalid_slot_info_falls_back(self):
        _DHASH_FLAG["enabled"] = True
        node = _make_resource_node()
        sections = OrderedDict()
        line = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("Body"),
            slot_info={"mark_name": "DiffuseMap", "dhash": "not-a-dhash",
                       "format": "BC7_UNORM"},
            binding_mode="DHASH",
        )
        self.assertEqual(line, "ps-t2 = Resource_DiffuseMap_Body")
        self.assertEqual(_filter_keys(sections), [])

    # -- d. 两个物体 -------------------------------------------------------
    def test_two_objects_with_different_dhash_get_two_filters(self):
        _DHASH_FLAG["enabled"] = True
        node = _make_resource_node()
        sections = OrderedDict()
        first = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("Body"),
            slot_info=_slot_info(dhash="0123456789abcdef"),
            binding_mode="DHASH",
        )
        second = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Hair"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("Hair"),
            slot_info=_slot_info(dhash="fedcba9876543210"),
            binding_mode="DHASH",
        )
        self.assertEqual(len(_filter_keys(sections)), 2)
        self.assertIn("Filter_Body_Diffuse", first)
        self.assertIn("Filter_Hair_Diffuse", second)
        self.assertNotEqual(first, second)

    def test_two_objects_with_same_dhash_share_one_filter(self):
        _DHASH_FLAG["enabled"] = True
        node = _make_resource_node()
        sections = OrderedDict()
        first = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("Body"),
            slot_info=_slot_info(),
            binding_mode="DHASH",
        )
        second = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Hair"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("Hair"),
            slot_info=_slot_info(),
            binding_mode="DHASH",
        )
        self.assertEqual(len(_filter_keys(sections)), 1)
        self.assertIn("Filter_Body_Diffuse", first)
        self.assertIn("Filter_Body_Diffuse", second)

    def test_same_token_different_dhash_does_not_overwrite(self):
        _DHASH_FLAG["enabled"] = True
        node = _make_resource_node()
        sections = OrderedDict()
        first = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("Body"),
            slot_info=_slot_info(dhash="0123456789abcdef"),
            binding_mode="DHASH",
        )
        second = node.create_resource_entry(
            _FakeMaterial("DiffuseMap_Body_Alt"),
            "ps-t2",
            "Textures",
            sections,
            obj=_obj("Body"),
            slot_info=_slot_info(dhash="fedcba9876543210"),
            binding_mode="DHASH",
        )
        self.assertIn("Filter_Body_Diffuse", first)
        self.assertIn("Filter_Body_Diffuse_fedcba98", second)
        self.assertEqual(len(_filter_keys(sections)), 2)
        self.assertEqual(
            sections["[Filter_Body_Diffuse]"][1],
            "match_dhash = 0123456789abcdef",
        )
        self.assertEqual(
            sections["[Filter_Body_Diffuse_fedcba98]"][1],
            "match_dhash = fedcba9876543210",
        )

    def test_workspace_dds_lookup_recovers_stale_paths_and_keeps_valid_sources(self):
        identity = "LOD0.aaaabbbb-100-0"
        light = _FakeMaterial("LightMap_Body")
        diffuse = _FakeMaterial("DiffuseMap_Body")
        obj = _obj(identity + ".001")
        obj.material_slots = [types.SimpleNamespace(material=m) for m in (light, diffuse)]
        node = _make_resource_node()
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "DiffuseMap.dds"
            fallback = Path(directory) / "LightMap.dds"
            for path in (valid, fallback):
                path.write_bytes(b"DDS ")
            node._collect_modimp_texture_slots = lambda _: {
                "ps-t1": {"mark_name": "LightMap", "source_path": str(Path(directory) / "moved.dds")},
                "ps-t2": {"mark_name": "DiffuseMap", "source_path": str(valid)},
            }
            lookup = mock.Mock(return_value=str(fallback))
            prefix = types.SimpleNamespace(
                extract_prefix_info=lambda name: (name,),
                parse_prefix_parts=lambda _: {"unique_str": identity},
            )
            with mock.patch.dict(sys.modules, {
                f"{PKG}.common.object_prefix_helper": types.SimpleNamespace(ObjectPrefixHelper=prefix),
                f"{PKG}.common.workspace_helper": types.SimpleNamespace(WorkSpaceHelper=types.SimpleNamespace(
                    find_submesh_texture_source=lookup,
                )),
            }), mock.patch.object(node_postprocess_material, "is_dds_file", side_effect=lambda path: Path(path).is_file()):
                resolved = node._workspace_source_paths_for_materials(obj)

            self.assertEqual(resolved, {light.name: str(fallback), diffuse.name: str(valid)})
            lookup.assert_called_once_with(identity, "LightMap")

    # -- 保留整个 slot_info（MarkFileName/MarkSlot/MarkHash/SourcePath）----
    def test_collect_modimp_texture_slots_keeps_mark_fields(self):
        _prefix_cache_state["props"] = {
            "Body": {
                "modimp_workspace_unique_str": "LOD0.abcd-1-0",
                "modimp_texture_slots": (
                    '{"ps-t2": {"mark_name": "DiffuseMap", "mark_slot": "ps-t2",'
                    ' "mark_hash": "deadbeefdeadbeef",'
                    ' "mark_filename": "body-DiffuseMap.dds",'
                    ' "source_path": "C:/workspace/body-DiffuseMap.dds",'
                    ' "source_dhash": "0123456789abcdef",'
                    ' "source_format": "BC7_UNORM"}}'
                ),
            }
        }
        try:
            node = _make_resource_node()
            slots = node._collect_modimp_texture_slots(_obj("Body"))
        finally:
            _prefix_cache_state["props"] = {}
        info = slots["ps-t2"]
        self.assertEqual(info["mark_filename"], "body-DiffuseMap.dds")
        self.assertEqual(info["mark_slot"], "ps-t2")
        self.assertEqual(info["mark_hash"], "deadbeefdeadbeef")
        self.assertEqual(info["source_path"], "C:/workspace/body-DiffuseMap.dds")
        self.assertEqual(info["source_dhash"], "0123456789abcdef")
        self.assertEqual(info["source_format"], "BC7_UNORM")


class ObjectTextureAssignmentRegexTests(unittest.TestCase):
    def test_assign_line_re_matches_ps_t_index_and_filter_lhs(self):
        match = node_postprocess_object_texture._ASSIGN_LINE_RE.match(
            "ps-t3 = Resource_old_DiffuseMap"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group("ps_index"), "ps-t3")
        self.assertEqual(match.group("target"), "Resource_old_DiffuseMap")

        match = node_postprocess_object_texture._ASSIGN_LINE_RE.match(
            "ps-t[Filter_Body_Diffuse] = ref Resource_old_DiffuseMap"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group("ps_filter"), "Filter_Body_Diffuse")
        self.assertTrue(match.group("ref"))

    def test_resource_for_map_type_finds_ps_t_bindings(self):
        lines = [
            "[TextureOverride_X]",
            "ps-t2 = Resource_DiffuseMap_Body",
            "ps-t[Filter_Body_Normal] = ref Resource_NormalMap_Body",
        ]
        self.assertEqual(
            node_postprocess_object_texture._resource_for_map_type(
                lines, [1, 2], "DiffuseMap"
            ),
            "Resource_DiffuseMap_Body",
        )
        self.assertEqual(
            node_postprocess_object_texture._resource_for_map_type(
                lines, [1, 2], "NormalMap"
            ),
            "Resource_NormalMap_Body",
        )

    def test_replace_assignment_ref_preserves_ps_t_lhs(self):
        lines = [
            "ps-t3 = Resource_old_DiffuseMap\n",
            "ps-t[Filter_Body_Diffuse] = ref Resource_old_DiffuseMap\n",
            "Resource\\ZZMI\\Diffuse = ref Resource_old_DiffuseMap\n",
            "ps-t[Filter_Body_Normal] = ref Resource_old_NormalMap\n",
            "ps-t[Filter_Body_Diffuse_fedcba98] = ref Resource_old_DiffuseMap\n",
        ]
        changed = node_postprocess_object_texture._replace_assignment_ref(
            lines, [0, 1, 2, 3, 4], "Diffuse", "Resource_new", "\n"
        )
        self.assertEqual(changed, 4)
        self.assertEqual(lines[0], "ps-t3 = Resource_new\n")
        self.assertEqual(
            lines[1], "ps-t[Filter_Body_Diffuse] = ref Resource_new\n"
        )
        self.assertEqual(
            lines[2], "Resource\\ZZMI\\Diffuse = ref Resource_new\n"
        )
        self.assertEqual(
            lines[3], "ps-t[Filter_Body_Normal] = ref Resource_old_NormalMap\n"
        )
        self.assertEqual(
            lines[4], "ps-t[Filter_Body_Diffuse_fedcba98] = ref Resource_new\n"
        )


class CustomMaterialAssignBindingModeTests(unittest.TestCase):
    def _node(self, items):
        node = object.__new__(
            node_postprocess_custom_material_assign.SSMTNode_PostProcess_CustomMaterialAssign
        )
        node.target_items = items
        return node

    def test_target_binding_mode_defaults_to_fixed(self):
        node = self._node(
            [
                types.SimpleNamespace(
                    target_object=types.SimpleNamespace(name="Body"),
                    binding_mode="DHASH",
                ),
                types.SimpleNamespace(
                    target_object=types.SimpleNamespace(name="Hair"),
                ),
            ]
        )
        self.assertEqual(node._target_binding_mode_for(_obj("Body")), "DHASH")
        self.assertEqual(node._target_binding_mode_for(_obj("Hair")), "FIXED")
        self.assertEqual(node._target_binding_mode_for(_obj("Unknown")), "FIXED")
        self.assertEqual(node._target_binding_mode_for(None), "FIXED")

    def test_generate_material_lines_injects_and_clears_active_mode(self):
        node = self._node(
            [
                types.SimpleNamespace(
                    target_object=types.SimpleNamespace(name="Body"),
                    binding_mode="DHASH",
                )
            ]
        )
        captured = {}
        base_cls = node_postprocess_material.SSMTNode_PostProcess_MaterialBase
        original = base_cls.generate_material_lines

        def fake_generate(self, *args, **kwargs):
            captured["mode"] = getattr(self, "_active_binding_mode", None)
            captured["slot_info"] = kwargs.get("slot_info")
            return [], kwargs.get("next_swap_key_num", 0)

        base_cls.generate_material_lines = fake_generate
        try:
            slot_info = _slot_info()
            node.generate_material_lines(
                [], "ps-t2", "DiffuseMap", _obj("Body"), "Textures",
                OrderedDict(), {}, {}, "$swapkey", 0, set(),
                slot_info=slot_info,
            )
        finally:
            base_cls.generate_material_lines = original
        self.assertEqual(captured["mode"], "DHASH")
        self.assertIs(captured["slot_info"], slot_info)
        self.assertIsNone(getattr(node, "_active_binding_mode", None))


class DHashProducerWiringTests(unittest.TestCase):
    def test_slot_dhash_fields_normalizes_good_alias(self):
        spec = importlib.util.spec_from_file_location(
            "dhash_meta_for_slot_test", ROOT / "common" / "dhash_metadata.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        fields = mod.slot_dhash_fields({
            "dhash": "bad",
            "SourceDHash": "0123456789ABCDEF",
            "format": "bad format!",
            "SourceFormat": "DXGI_FORMAT_BC7_UNORM_SRGB",
            "binding_mode": "Filter",
        })
        self.assertEqual(fields["source_dhash"], "0123456789abcdef")
        self.assertEqual(fields["source_format"], "BC7_UNORM_SRGB")
        self.assertEqual(fields["binding_mode"], "filter")

    def test_slot_builders_carry_source_fields(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "ui/ntmi_modimp/texture_slot_refresh.py",
            "ui/ntmi_modimp/ntemi_importer.py",
        ):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertIn("slot_dhash_fields", text, relative)


if __name__ == "__main__":
    unittest.main()
