# -*- coding: utf-8 -*-
"""材质转资源pro「纯贴图模式」与「条件判断」门控的对接契约。

被测契约
--------
1. 纯贴图段自带顶层 ``Resource\\<ns>\\<槽位> = null`` 兜底行，且位于 ``= ref``
   切换块之前（条件关闭时回落原生贴图，不残留同帧别的部件贴图）。
2. 生成后把段登记进 ``common/export_section_registry``（段类型
   ``TextureOverrideMaterialPureTexture``、draw_ib = 该 IB hash），条件判断节点
   才能按「物体 -> DrawIB -> 段名」反查到它。
3. 登记过的纯贴图段不会被 ``_pure_texture_conflict_with_export`` 误判成
   「该 IB 在导出链上有网格段」：同一进程里第二次生成仍然重建段，不会删旧段后
   不再生成。
4. 贴图覆盖、资源和切换变量写入主 INI。同 IB 的多个物体使用不同的段名，
   只有槽位段登记到该文件。重跑重建自己的段并清理旧版独立片段。
5. 开「使用全局指定」后照样生成：主路径只看 ini 里已有的 mesh 段，导出链外的
   物体只能靠这条路径补段，不能在全局模式下被整体关掉。
6. 这些物体的贴图切换组取自它们自己的 ``target_items[].switch_groups``：全局模式下
   ``global_switch_groups`` 是链路部件的扫描结果，和导出链外的物体不是同一批。
7. 写法跟贴图标记走：``MarkType == "Hash"`` 的贴图写 ``[TextureOverride_<贴图hash>]``
   （``hash`` / ``match_priority`` / ``this = Resource_Texture_<贴图hash>``，对齐
   ``M_IniHelper.generate_hash_style_texture_ini`` 与 SSMT4 的 Hash 风格模板）；``Slot``
   / ``SharedSlot`` / 没有标记的仍走槽位写法。``MarkHash`` 缺失时退回槽位写法。
8. 哈希段名与导出目录里已有段重复时跳过并告警（重复段在 3Dmigoto 里是后者覆盖前者）。

装载沿用 ``tests/test_custom_material_assign_clear_textures.py`` 的 fake-package 范式。
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path

PKG = "_pure_texture_gate_test_pkg"
for _package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
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
    StringProperty=lambda **_kwargs: "",
    BoolProperty=lambda **_kwargs: False,
    IntProperty=lambda **_kwargs: 0,
    CollectionProperty=lambda **_kwargs: [],
    PointerProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: "",
    FloatProperty=lambda **_kwargs: 0.0,
)
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups={})
_fake_bpy.context = types.SimpleNamespace(window_manager=None)
_fake_bpy.ops = types.SimpleNamespace()
_fake_bpy.utils = types.SimpleNamespace(
    register_class=lambda _cls: None, unregister_class=lambda _cls: None
)
sys.modules["bpy"] = _fake_bpy


# ---------------------------------------------------------------------------
# 段登记表替身（模块级函数，供 ``from ..common import export_section_registry``）
# ---------------------------------------------------------------------------

_REGISTRY_ENTRIES = []


def _register_section(section_name, section_type="", draw_ib="", ini_path=""):
    key = (str(section_name), str(section_type), str(draw_ib))
    if any((e["name"], e["type"], e["draw_ib"]) == key for e in _REGISTRY_ENTRIES):
        return
    _REGISTRY_ENTRIES.append(
        {
            "name": str(section_name),
            "type": str(section_type),
            "draw_ib": str(draw_ib),
            "ini_path": str(ini_path),
        }
    )


def _sections_for_draw_ib(draw_ib, ini_path=""):
    wanted = str(draw_ib or "").casefold()
    return [dict(e) for e in _REGISTRY_ENTRIES if e["draw_ib"].casefold() == wanted]


def _sections_for_draw_ibs(draw_ibs, ini_path=""):
    wanted = {str(value or "").casefold() for value in (draw_ibs or ())}
    return [dict(e) for e in _REGISTRY_ENTRIES if e["draw_ib"].casefold() in wanted]


_registry_module = types.ModuleType(f"{PKG}.common.export_section_registry")
_registry_module.register_section = _register_section
_registry_module.get_sections_for_draw_ib = _sections_for_draw_ib
_registry_module.get_sections_for_draw_ibs = _sections_for_draw_ibs
_registry_module.get_all = lambda: {}
_registry_module.get_ini_paths = lambda: []
sys.modules[_registry_module.__name__] = _registry_module


# ---------------------------------------------------------------------------
# 材质基类替身（只补被测路径用到的设施）
# ---------------------------------------------------------------------------

def _parse_ini_content(content):
    preamble_lines = []
    sections = OrderedDict()
    current_section = None
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped
            sections.setdefault(current_section, [])
        elif current_section is None:
            preamble_lines.append(line)
        elif stripped:
            sections[current_section].append(line)
    return preamble_lines, sections


def _serialize_ini_content(
    preamble_lines,
    sections,
    transparency_sections=None,
    preserved_tail_content="",
    preserved_driver_content="",
):
    new_content = []
    if preserved_driver_content:
        new_content.append(preserved_driver_content.rstrip())
        new_content.append("")
    new_content.extend(preamble_lines or [])
    if new_content and sections and new_content[-1].strip():
        new_content.append("")
    for section_name, lines in sections.items():
        new_content.append(section_name)
        new_content.extend(lines)
        new_content.append("")
    if preserved_tail_content:
        new_content.append("")
        new_content.append(preserved_tail_content)
    return "\n".join(new_content)


class _StubMaterialBase:
    TRANSPARENCY_SECTION_MARKER = ";MARK:TestTransparency"

    @staticmethod
    def _parse_ini_content(content):
        return _parse_ini_content(content)

    @classmethod
    def _serialize_ini_content(cls, preamble_lines, sections, **kwargs):
        return _serialize_ini_content(preamble_lines, sections, **kwargs)

    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", str(content or "")

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return str(content or ""), ""

    @classmethod
    def _strip_previous_transparency_sections(cls, content):
        return str(content or "")

    def _prepare_material_group_switch_map(self, mapping):
        mapping.clear()

    def create_resource_entry(
        self,
        material,
        param_name,
        texture_folder,
        all_sections,
        resource_name_override=None,
        workspace_source_path=None,
        dedup_group=None,
    ):
        """哈希风格用 ``param_name="this"``；槽位风格的真实实现另有分支。"""
        resource_name = resource_name_override or f"Resource_{material.name}"
        all_sections[f"[{resource_name}]"] = [
            f"filename = Textures/{material.name}.dds"
        ]
        return f"{param_name} = {resource_name}"

    def _max_ui_switch_number(self):
        return 0

    def _iter_switch_group_defs(self):
        return []

    def define_swapkeys_in_sections(self, sections, used_swap_keys):
        """纯贴图模式会在段里声明 global persist；这里只记录，不实际写入。"""
        return len(used_swap_keys)

    def find_matching_materials(self, obj, texture_type):
        if texture_type == "DiffuseMap":
            return [
                types.SimpleNamespace(name="DiffuseMap_A"),
                types.SimpleNamespace(name="DiffuseMap_B"),
            ]
        return []

    def generate_material_lines(
        self,
        matching_materials,
        param_name,
        texture_type,
        obj,
        texture_folder,
        all_sections,
        object_to_diffuse_swapkey,
        material_group_to_swapkey,
        swap_key_prefix,
        next_swap_key_num,
        used_swap_keys,
        resource_name_provider=None,
    ):
        used_swap_keys.add("$swapkey150")
        return (
            [
                f"; {texture_type} 材质切换 (组: $swapkey150)",
                "if $swapkey150 == 0",
                f"    {param_name} = ref Resource_A",
                "endif",
                "if $swapkey150 == 1",
                f"    {param_name} = ref Resource_B",
                "endif",
                "",
            ],
            next_swap_key_num,
        )


_material_module = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_module.MATERIAL_DETECT_PRESETS = [
    "DiffuseMap",
    "NormalMap",
    "LightMap",
    "MaterialMap",
]
_material_module.SSMTNode_PostProcess_MaterialBase = _StubMaterialBase
_material_module.resolve_keyswap_key = lambda *args, **kwargs: ""
sys.modules[_material_module.__name__] = _material_module


# ---------------------------------------------------------------------------
# 加载被测模块
# ---------------------------------------------------------------------------

_module_path = (
    Path(__file__).resolve().parents[1]
    / "blueprint"
    / "node_postprocess_custom_material_assign.py"
)
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_custom_material_assign", _module_path
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)

#: 纯贴图模式用到的切换变量基址（真实实现从节点属性解析，这里固定）。
module._parse_switch_var_base = lambda _node: ("$swapkey", 150)


class _FakeNode(module.SSMTNode_PostProcess_CustomMaterialAssign):
    """借真实类的方法，只替换 bpy 收集来的部件列表与槽位表。"""

    def __init__(
        self,
        target_names,
        global_mode=False,
        item_groups=None,
        global_groups=None,
        marks=None,
    ):
        item_groups = list(item_groups or [])
        self.target_items = []
        for index, name in enumerate(target_names):
            groups = item_groups[index] if index < len(item_groups) else []
            self.target_items.append(
                types.SimpleNamespace(
                    target_object=types.SimpleNamespace(name=name, type="MESH"),
                    switch_groups=list(groups or []),
                )
            )
        self.global_switch_groups = list(global_groups or [])
        self.use_global_assign = bool(global_mode)
        self.clear_non_target_textures = False
        self.material_switch_var = "$swapkey"
        self._marks_by_object = dict(marks or {})

    def _pure_texture_marks(self, obj_name, cache):
        """贴图标记用假数据（真实实现要读工作空间 SubmeshJson）。"""
        return dict(self._marks_by_object.get(obj_name) or {})

    @staticmethod
    def _pure_texture_slot_layout():
        return (
            module._MATERIAL_PURE_TEXTURE_SLOTS_ZZMI,
            module._MATERIAL_PURE_TEXTURE_SET_TEXTURES_ZZMI,
        )

    # 真实类在 pro 里重写了这两个入口（加目标过滤 / 禁用切换过滤）；
    # 本测试只验证纯贴图模式的产物，直接退回基类替身的行为。
    def find_matching_materials(self, obj, texture_type):
        return _StubMaterialBase.find_matching_materials(self, obj, texture_type)

    def generate_material_lines(self, *args, **kwargs):
        return _StubMaterialBase.generate_material_lines(self, *args, **kwargs)


BASE_INI = "[Constants]\nglobal persist $mod_enabled = 1\n"


def _switch_group(variable, bindings, key="N", enabled=True, comment="", object_name=""):
    """造一个切换组（``bindings`` 在真实节点上存的是 JSON 字符串）。"""
    names = [list(binding) for binding in bindings]
    return types.SimpleNamespace(
        switch_variable=variable,
        bindings=json.dumps(names, ensure_ascii=False),
        key=key,
        enabled=enabled,
        state_count=max(len(binding) for binding in names),
        comment=comment,
        object_name=object_name,
        merge_group_id=variable,
    )


def _mark(name, mark_type="Hash", mark_hash="18fa939b", slot="ps-t0", filename=""):
    """一个贴图标记（真实实现读工作空间 SubmeshJson 的 TextureMarkUpInfoList）。"""
    return types.SimpleNamespace(
        mark_name=name,
        mark_type=mark_type,
        mark_hash=mark_hash,
        mark_slot=slot,
        mark_filename=filename or f"16420b5e-3180-2238-{name}.dds",
    )


class PureTextureSectionTests(unittest.TestCase):
    def setUp(self):
        _REGISTRY_ENTRIES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ini_path = os.path.join(self.root, "mod.ini")
        with open(self.ini_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_INI)

    def _run(self, target_names, **kwargs):
        node = _FakeNode(list(target_names), **kwargs)
        return node._apply_pure_texture_sections(self.root)

    def _content(self):
        with open(self.ini_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _fragment_files(self):
        return sorted(
            name
            for name in os.listdir(self.root)
            if name.endswith("_PureTexture.ini")
        )

    def _registry_view(self):
        return [
            {
                "name": entry["name"],
                "type": entry["type"],
                "draw_ib": entry["draw_ib"],
                "ini_path": entry["ini_path"],
            }
            for entry in _REGISTRY_ENTRIES
        ]

    def test_writes_overrides_to_main_ini(self):
        count = self._run(["LOD0.7fbbcf0d-7116-0.脸"])

        self.assertEqual(1, count)
        self.assertEqual([], self._fragment_files())
        fragment = self._content()
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", fragment)
        self.assertIn("hash = 7fbbcf0d", fragment)
        self.assertIn("Resource\\ZZMI\\Diffuse = null", fragment)
        self.assertIn("run = CommandList\\ZZMI\\SetTextures", fragment)
        # null 兜底必须在 ref 之前
        self.assertLess(
            fragment.index("Resource\\ZZMI\\Diffuse = null"),
            fragment.index("Resource\\ZZMI\\Diffuse = ref Resource_A"),
        )
        # 后处理节点按 DrawIB 反查时应定位主 INI。
        self.assertEqual(
            [
                {
                    "name": "TextureOverride_MaterialPureTexture_7fbbcf0d",
                    "type": "TextureOverrideMaterialPureTexture",
                    "draw_ib": "7fbbcf0d",
                    "ini_path": self.ini_path,
                }
            ],
            self._registry_view(),
        )

    def test_second_run_regenerates_and_does_not_duplicate(self):
        self._run(["LOD0.7fbbcf0d-7116-0.脸"])
        self._run(["LOD0.7fbbcf0d-7116-0.脸"])

        # 登记过的纯贴图段不能被当成「导出链上的网格段」而跳过重新生成
        self.assertEqual([], self._fragment_files())
        fragment = self._content()
        self.assertEqual(
            1, fragment.count("[TextureOverride_MaterialPureTexture_7fbbcf0d]")
        )
        self.assertIn("hash = 7fbbcf0d", fragment)
        self.assertIn("Resource\\ZZMI\\Diffuse = ref Resource_A", fragment)

    def test_two_objects_same_ib_get_separate_sections_all_registered(self):
        count = self._run(
            ["LOD0.7fbbcf0d-7116-0.脸", "LOD0.7fbbcf0d-1260-9402.脸"]
        )

        self.assertEqual(2, count)
        self.assertEqual([], self._fragment_files())
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", self._content())
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d_2]", self._content())
        self.assertEqual(
            [
                "TextureOverride_MaterialPureTexture_7fbbcf0d",
                "TextureOverride_MaterialPureTexture_7fbbcf0d_2",
            ],
            sorted(entry["name"] for entry in _REGISTRY_ENTRIES),
        )

    def test_stale_fragments_are_cleared_on_regenerate(self):
        stale = os.path.join(self.root, "abcdef12-1-2_PureTexture.ini")
        with open(stale, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("[TextureOverride_MaterialPureTexture_abcdef12]\nhash = abcdef12\n")

        self._run(["LOD0.7fbbcf0d-7116-0.脸"])

        self.assertEqual([], self._fragment_files())

    def test_legacy_cleanup_preserves_unowned_ini(self):
        path = Path(self.root) / "abcdef12-1-2_PureTexture.ini"
        path.write_text("[Constants]\nglobal $custom = 1\n", encoding="utf-8")
        self.assertEqual(0, _FakeNode._clear_pure_texture_fragments(self.root))
        self.assertTrue(path.exists())

    def test_resource_collision_preserves_existing_definition(self):
        sections = {"[Resource_A]": ["filename = Textures/model.dds"]}
        overrides = {"[TextureOverride_Test]": ["this = Resource_A", "ps-t0 = ref Resource_A"]}
        resources = {"[Resource_A]": ["filename = Textures/pure.dds"]}
        _FakeNode._merge_pure_texture_resources(sections, overrides, resources, {"resource_a"})
        self.assertEqual(["filename = Textures/model.dds"], sections["[Resource_A]"])
        self.assertIn("filename = Textures/pure.dds", sections["[Resource_A_2]"])
        self.assertEqual(
            ["this = Resource_A_2", "ps-t0 = ref Resource_A_2"],
            overrides["[TextureOverride_Test]"],
        )

    def test_stale_fragment_with_constants_does_not_win_main_selection(self):
        stale = Path(self.root) / "00000000-1-0_PureTexture.ini"
        stale.write_text(BASE_INI + "\n[TextureOverride_MaterialPureTexture_00000000]\nhash = 00000000\n", encoding="utf-8")
        self.assertEqual(1, self._run(["LOD0.7fbbcf0d-7116-0.脸"]))
        self.assertFalse(stale.exists())
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", self._content())

    def test_registered_pure_section_is_not_export_conflict(self):
        _register_section(
            "TextureOverride_MaterialPureTexture_7fbbcf0d",
            section_type="TextureOverrideMaterialPureTexture",
            draw_ib="7fbbcf0d",
        )
        self.assertFalse(
            module.SSMTNode_PostProcess_CustomMaterialAssign._pure_texture_conflict_with_export(
                {}, "7fbbcf0d"
            )
        )

        # 同一 IB 真的产出网格段时仍然判冲突（纯贴图模式必须让位）
        _register_section("TextureOverride_IB_7fbbcf0d", draw_ib="7fbbcf0d")
        self.assertTrue(
            module.SSMTNode_PostProcess_CustomMaterialAssign._pure_texture_conflict_with_export(
                {}, "7fbbcf0d"
            )
        )


class GlobalModePureTextureTests(unittest.TestCase):
    """「使用全局指定」打开后，导出链外的物体照样要出贴图替换段。"""

    def setUp(self):
        _REGISTRY_ENTRIES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ini_path = os.path.join(self.root, "mod.ini")
        with open(self.ini_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_INI)

    def _run(self, target_names, **kwargs):
        node = _FakeNode(list(target_names), **kwargs)
        return node._apply_pure_texture_sections(self.root)

    def _content(self):
        with open(self.ini_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _fragment_files(self):
        return sorted(
            name
            for name in os.listdir(self.root)
            if name.endswith("_PureTexture.ini")
        )

    def test_global_mode_still_generates_sections(self):
        count = self._run(["LOD0.7fbbcf0d-7116-0.脸"], global_mode=True)

        self.assertEqual(1, count)
        self.assertEqual([], self._fragment_files())
        fragment = self._content()
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", fragment)
        self.assertIn("Resource\\ZZMI\\Diffuse = ref Resource_A", fragment)

    def test_global_mode_without_extra_objects_does_nothing(self):
        self.assertEqual(0, self._run([], global_mode=True))
        self.assertEqual(BASE_INI, self._content())
        self.assertEqual([], self._fragment_files())

    def test_global_mode_skips_objects_that_are_exported(self):
        # 该 IB 已经有网格段（主路径处理），纯贴图段必须让出去
        _register_section("TextureOverride_IB_7fbbcf0d", draw_ib="7fbbcf0d")

        self.assertEqual(0, self._run(["LOD0.7fbbcf0d-7116-0.脸"], global_mode=True))
        self.assertNotIn("MaterialPureTexture", self._content())
        self.assertEqual([], self._fragment_files())

    def test_global_mode_takes_switch_groups_from_the_object_itself(self):
        module.resolve_keyswap_key = lambda *args, **kwargs: "N"
        self.addCleanup(setattr, module, "resolve_keyswap_key", lambda *a, **k: "")
        item_group = _switch_group(
            "$swapkey150", [["DiffuseMap_A"], ["DiffuseMap_B"]]
        )
        chain_group = _switch_group(
            "$swapkey160", [["BodyMap_C"], ["BodyMap_D"]], key="M"
        )

        count = self._run(
            ["LOD0.7fbbcf0d-7116-0.脸"],
            global_mode=True,
            item_groups=[[item_group]],
            global_groups=[chain_group],
        )

        self.assertEqual(1, count)
        content = self._content()
        self.assertIn("[KeySwap_Diffuse_swapkey150]", content)
        self.assertNotIn("[KeySwap_Diffuse_swapkey160]", content)

    def test_merged_switch_map_separates_global_and_object_groups(self):
        node = _FakeNode(
            ["LOD0.7fbbcf0d-7116-0.脸"],
            global_mode=True,
            item_groups=[
                [_switch_group("$swapkey150", [["DiffuseMap_A"], ["DiffuseMap_B"]])]
            ],
            global_groups=[
                [_switch_group("$swapkey160", [["BodyMap_C"], ["BodyMap_D"]])]
            ][0],
        )
        mapping = {}
        node._prepare_material_group_switch_map(mapping)
        self.assertEqual("$swapkey160", mapping[("BodyMap_C",)])

        node._merge_object_switch_groups(node.target_items[0], mapping)

        self.assertEqual("$swapkey150", mapping[("DiffuseMap_A",)])
        # 档位顺序跟着物体自己的组走，否则第 N 档会和面板上的第 N 档错开
        self.assertEqual(
            ["DiffuseMap_A"],
            node._material_group_order[("DiffuseMap_A",)],
        )


class HashStylePureTextureTests(unittest.TestCase):
    """贴图标记是 Hash 的走哈希写法，其余（Slot / 无标记）走插槽写法。"""

    OBJ = "LOD0.7fbbcf0d-7116-0.脸"

    def setUp(self):
        _REGISTRY_ENTRIES.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.ini_path = os.path.join(self.root, "mod.ini")
        with open(self.ini_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_INI)

    def _run(self, marks):
        node = _FakeNode([self.OBJ], marks={self.OBJ: marks})
        return node._apply_pure_texture_sections(self.root)

    def _content(self):
        return Path(self.ini_path).read_text(encoding="utf-8")

    def test_hash_mark_writes_hash_style_section(self):
        count = self._run({"DiffuseMap": _mark("DiffuseMap", "Hash", "18fa939b")})

        self.assertEqual(1, count)
        fragment = self._content()
        self.assertIn("[TextureOverride_18fa939b]", fragment)
        self.assertIn("hash = 18fa939b", fragment)
        self.assertIn("match_priority = 0", fragment)
        self.assertIn("this = Resource_Texture_18fa939b", fragment)
        self.assertIn("[Resource_Texture_18fa939b]", fragment)
        self.assertIn("filename = Textures/DiffuseMap_A.dds", fragment)
        # 哈希写法不需要槽位段（也不需要 run CommandList）
        self.assertNotIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", fragment)
        self.assertNotIn("run = CommandList", fragment)
        self.assertEqual(["mod.ini"], sorted(os.listdir(self.root)))

    def test_slot_mark_keeps_slot_style(self):
        self._run({"DiffuseMap": _mark("DiffuseMap", "Slot", "18fa939b")})

        fragment = self._content()
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", fragment)
        self.assertIn("Resource\\ZZMI\\Diffuse = ref Resource_A", fragment)
        self.assertNotIn("[TextureOverride_18fa939b]", fragment)

    def test_hash_mark_without_hash_falls_back_to_slot_style(self):
        self._run({"DiffuseMap": _mark("DiffuseMap", "Hash", "")})

        fragment = self._content()
        self.assertIn("[TextureOverride_MaterialPureTexture_7fbbcf0d]", fragment)
        self.assertNotIn("this = ", fragment)

    def test_hash_section_already_in_directory_is_skipped(self):
        with open(self.ini_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(BASE_INI + "\n[TextureOverride_18fa939b]\nhash = 18fa939b\n")

        self.assertEqual(
            0, self._run({"DiffuseMap": _mark("DiffuseMap", "Hash", "18fa939b")})
        )
        self.assertEqual(["mod.ini"], sorted(os.listdir(self.root)))

    def _content(self):
        with open(self.ini_path, "r", encoding="utf-8") as handle:
            return handle.read()


if __name__ == "__main__":
    unittest.main()
