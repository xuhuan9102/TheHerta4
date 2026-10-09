# -*- coding: utf-8 -*-
"""按 SSMT 子网格贴图标记建立贴图材质。

导入侧过去只按文件名后缀在工作空间目录里搜 ``-DiffuseMap.dds`` / ``-NormalMap.dds``，
只做两种类型；而导出 mod 时决定「导出哪些类型贴图、写哪些 ps-tN」的是工作空间
SubmeshJson 里的 ``TextureMarkUpInfoList``——没标记的类型既不会导出贴图也不会写进
ini。这里覆盖导入侧改用同一份标记：标记了才建、标记了几种建几种。
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_marked_texture_material_test_pkg"
for package_name in (PKG, f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeImage:
    def __init__(self, name, filepath=""):
        self.name = name
        self.filepath = filepath
        self.colorspace_settings = types.SimpleNamespace(name="sRGB")


_MATERIALS = []
_IMAGES = []


def _new_material(name=None, **_kwargs):
    material = types.SimpleNamespace(name=name or f"Material_{len(_MATERIALS)}")
    _MATERIALS.append(material)
    return material


def _remove_material(material):
    if material in _MATERIALS:
        _MATERIALS.remove(material)


def _load_image(filepath, **_kwargs):
    image = _FakeImage(os.path.basename(filepath), filepath)
    _IMAGES.append(image)
    return image


def _remove_image(image):
    if image in _IMAGES:
        _IMAGES.remove(image)


_install_module(
    "bpy",
    data=types.SimpleNamespace(
        materials=types.SimpleNamespace(new=_new_material, remove=_remove_material),
        images=types.SimpleNamespace(load=_load_image, remove=_remove_image),
    ),
)

_WORKSPACE = r"E:\game\WorkSpace\ZZMI\叶瞬光\\"
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        logic_name="ZZMI",
        path_workspace_folder=lambda: _WORKSPACE,
    ),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        ZZMI="ZZMI",
        GIMI="GIMI",
        IdentityV="IdentityV",
        is_zzmi_family=lambda name: str(name) in {"ZZMI", "ZZMIDX12"},
    ),
)


class _FakeMetadata:
    def __init__(self, marks, extract_folder):
        self.texture_markup_info_list = marks
        self.extract_gametype_folder_path = extract_folder


_RESOLVE_RESULT = {"marks": [], "extract_folder": "", "error": None}
_RESOLVE_CALLS = []


def _resolve(unique_str):
    _RESOLVE_CALLS.append(unique_str)
    if _RESOLVE_RESULT["error"] is not None:
        raise _RESOLVE_RESULT["error"]
    return _FakeMetadata(_RESOLVE_RESULT["marks"], _RESOLVE_RESULT["extract_folder"])


_install_module(
    f"{PKG}.common.submesh_metadata",
    SubmeshMetadataResolver=types.SimpleNamespace(resolve=_resolve),
)
_install_module(f"{PKG}.common.mesh_create_helper", MeshCreateHelper=object())
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        ignore_texture_alpha=lambda: False,
        import_materials_by_submesh_mark=lambda: True,
    ),
)

ROOT = Path(__file__).resolve().parents[1]


def _load_common_module(module_name):
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.common.{module_name}", ROOT / "common" / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_common_module("texture_metadata_helper")
marked = _load_common_module("marked_texture_material")


def _mark(mark_name, mark_type="Slot", mark_filename=""):
    """SubmeshJson 里 TextureMarkUpInfoList 的原始形态（大写键的 dict）。"""
    return {
        "MarkName": mark_name,
        "MarkType": mark_type,
        "MarkHash": "",
        "MarkSlot": "ps-t3",
        "MarkFileName": mark_filename or f"4a178546-18468-0-{mark_name}.dds",
    }


class _FakeObject:
    def __init__(self, name="4a178546-18468-0", materials=None, custom_props=None):
        self.name = name
        self.data = types.SimpleNamespace(materials=list(materials or []), name=name)
        self._custom_props = dict(custom_props or {})

    def get(self, key, default=None):
        return self._custom_props.get(key, default)

    @property
    def material_slots(self):
        return [types.SimpleNamespace(material=material) for material in self.data.materials]


class PureHelperTests(unittest.TestCase):
    def test_unique_str_keeps_lod_prefix(self):
        path = os.path.join(
            r"E:\ws\LOD1", "0534b536-864-0", "TYPE_GPU_P12", "0534b536-864-0.json"
        )
        self.assertEqual(
            marked.unique_str_from_json_path(path), "LOD1.0534b536-864-0"
        )

    def test_unique_str_without_lod_directory(self):
        path = os.path.join(r"E:\ws", "4a178546-18468-0", "TYPE_A", "4a178546-18468-0.json")
        self.assertEqual(marked.unique_str_from_json_path(path), "4a178546-18468-0")

    def test_unique_str_tolerates_non_lod_parent(self):
        path = os.path.join(r"E:\ws", "somewhere", "TYPE_A", "abc.json")
        self.assertEqual(marked.unique_str_from_json_path(path), "somewhere")

    def test_material_name_is_type_first_segment(self):
        name = marked.mark_material_name("DiffuseMap", "LOD0.4a178546-18468-0")
        self.assertEqual(name, "DiffuseMap_LOD0.4a178546-18468-0")
        # 导出侧按 split("_")[0] 判定类型，首段必须恰好是类型名
        self.assertEqual(name.split("_")[0], "DiffuseMap")

    def test_material_name_replaces_illegal_chars(self):
        name = marked.mark_material_name("BodyMaskMap", "a/b:c")
        self.assertEqual(name, "BodyMaskMap_a_b_c")

    def test_material_name_truncates_to_blender_limit(self):
        name = marked.mark_material_name("DiffuseMap", "x" * 200)
        self.assertLessEqual(len(name.encode("utf-8")), 60)

    def test_socket_type_mapping(self):
        cases = {
            "DiffuseMap": "DIFFUSE",
            "Diffuse": "DIFFUSE",
            "NormalMap": "NORMAL",
            "LightMap": "LIGHTMAP",
            "HairLightMap": "LIGHTMAP",
            "MaterialMap": "SURFACE",
            "BodyMaskMap": "BODYMASK",
        }
        for mark_name, expected in cases.items():
            self.assertEqual(marked.mark_socket_type(mark_name), expected, mark_name)

    def test_socket_type_is_case_insensitive(self):
        self.assertEqual(marked.mark_socket_type("diffusemap"), "DIFFUSE")

    def test_unknown_mark_type_returns_blank(self):
        """SSMT 里标记了别的类型也要建材质，只是不接通道。"""
        self.assertEqual(marked.mark_socket_type("SomeUnknownMap"), "")

    def test_slot_mark_detection(self):
        self.assertTrue(marked.is_slot_mark(_mark("DiffuseMap", "Slot")))
        self.assertTrue(marked.is_slot_mark(_mark("DiffuseMap", "SharedSlot")))
        self.assertFalse(marked.is_slot_mark(_mark("DiffuseMap", "Hash")))

    def test_render_material_name_is_not_a_mark_type(self):
        """渲染材质首段必须不是贴图类型名，导出侧才会完全忽略它。"""
        name = marked.render_material_name("LOD0.4a178546-18468-0")
        self.assertEqual(name, "IMGPV_LOD0.4a178546-18468-0")
        self.assertEqual(marked.mark_socket_type("IMGPV_LOD0.4a178546-18468-0"), "")

    def test_render_material_name_truncates_to_blender_limit(self):
        name = marked.render_material_name("x" * 200)
        self.assertLessEqual(len(name.encode("utf-8")), 60)

    def test_bare_name_strips_lod_prefix(self):
        self.assertEqual(
            marked.bare_name_from_unique_str("LOD0.4a178546-18468-0"), "4a178546-18468-0"
        )
        self.assertEqual(
            marked.bare_name_from_unique_str("LOD12.4a178546-18468-0"), "4a178546-18468-0"
        )
        self.assertEqual(
            marked.bare_name_from_unique_str("4a178546-18468-0"), "4a178546-18468-0"
        )
        # 只认「LOD + 数字」前缀，别的点号开头不能误剥
        self.assertEqual(marked.bare_name_from_unique_str("abc.def"), "abc.def")

    def test_object_unique_str_prefers_custom_prop(self):
        obj = _FakeObject(
            name="SomeObject",
            custom_props={"3DMigoto:WorkspaceUniqueStr": "LOD1.aaaa-1-0"},
        )
        self.assertEqual(marked.object_workspace_unique_str(obj), "LOD1.aaaa-1-0")

    def test_object_unique_str_falls_back_to_mesh_name(self):
        obj = _FakeObject(name="4a178546-18468-0")
        self.assertEqual(marked.object_workspace_unique_str(obj), "4a178546-18468-0")


class ResolveTexturePathTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="marked_texture_")
        self.extract_folder = os.path.join(self.temp_dir, "TYPE_A")
        os.makedirs(self.extract_folder, exist_ok=True)

    def _touch(self, filename):
        path = os.path.join(self.extract_folder, filename)
        with open(path, "wb") as handle:
            handle.write(b"x")
        return path

    def test_prefers_existing_png_over_dds(self):
        """工作空间里已转出无损 png 时优先用 png，重复导入不再解压排不掉的 DDS。"""
        dds_path = self._touch("4a178546-18468-0-DiffuseMap.dds")
        png_path = self._touch("4a178546-18468-0-DiffuseMap.png")
        resolved = marked.resolve_mark_texture_path(
            self.extract_folder, _mark("DiffuseMap")
        )
        self.assertEqual(resolved, png_path)
        self.assertNotEqual(resolved, dds_path)

    def test_falls_back_to_dds_when_no_png(self):
        dds_path = self._touch("4a178546-18468-0-DiffuseMap.dds")
        resolved = marked.resolve_mark_texture_path(
            self.extract_folder, _mark("DiffuseMap")
        )
        self.assertEqual(resolved, dds_path)

    def test_returns_blank_when_nothing_exists(self):
        self.assertEqual(
            marked.resolve_mark_texture_path(self.extract_folder, _mark("DiffuseMap")),
            "",
        )

    def test_returns_blank_without_filename(self):
        mark = types.SimpleNamespace(mark_name="DiffuseMap", mark_filename="")
        self.assertEqual(marked.resolve_mark_texture_path(self.extract_folder, mark), "")


class ReadTextureMarksTests(unittest.TestCase):
    def setUp(self):
        marked.clear_mark_cache()
        _RESOLVE_CALLS.clear()
        _RESOLVE_RESULT.update(marks=[], extract_folder="", error=None)

    def test_reads_marks_and_extract_folder(self):
        _RESOLVE_RESULT["marks"] = [_mark("DiffuseMap")]
        _RESOLVE_RESULT["extract_folder"] = r"E:\ws\TYPE_A\\"
        marks, folder = marked.read_texture_marks("LOD0.4a178546-18468-0")
        self.assertEqual(len(marks), 1)
        self.assertEqual(folder, r"E:\ws\TYPE_A\\")

    def test_raw_dict_marks_are_normalized(self):
        """SubmeshJson 给的是大写键 dict；不 normalize 就会拿 dict 当标记用，
        ``mark_name`` 取到空串 → 一个材质都建不出来（导出侧也是先 normalize）。"""
        _RESOLVE_RESULT["marks"] = [_mark("DiffuseMap")]
        _RESOLVE_RESULT["extract_folder"] = r"E:\ws\TYPE_A\\"
        marks, _folder = marked.read_texture_marks("LOD0.4a178546-18468-0")
        self.assertEqual(marks[0].mark_name, "DiffuseMap")
        self.assertEqual(marks[0].mark_type, "Slot")
        self.assertTrue(marked.is_slot_mark(marks[0]))

    def test_duplicate_marks_are_deduped(self):
        _RESOLVE_RESULT["marks"] = [_mark("DiffuseMap"), _mark("DiffuseMap")]
        _RESOLVE_RESULT["extract_folder"] = r"E:\ws\TYPE_A\\"
        marks, _folder = marked.read_texture_marks("LOD0.4a178546-18468-0")
        self.assertEqual(len(marks), 1)

    def test_resolution_failure_returns_empty(self):
        _RESOLVE_RESULT["error"] = ValueError("no submesh json")
        marks, folder = marked.read_texture_marks("LOD0.missing")
        self.assertEqual((marks, folder), ([], ""))

    def test_blank_unique_str_skips_resolution(self):
        marks, folder = marked.read_texture_marks("   ")
        self.assertEqual(marks, [])
        self.assertEqual(folder, "")
        self.assertEqual(_RESOLVE_CALLS, [])

    def test_result_is_cached_within_batch(self):
        _RESOLVE_RESULT["marks"] = [_mark("DiffuseMap")]
        _RESOLVE_RESULT["extract_folder"] = r"E:\ws\TYPE_A\\"
        marked.read_texture_marks("LOD0.4a178546-18468-0")
        marked.read_texture_marks("LOD0.4a178546-18468-0")
        self.assertEqual(len(_RESOLVE_CALLS), 1)

    def test_clear_cache_forces_reread(self):
        _RESOLVE_RESULT["marks"] = [_mark("DiffuseMap")]
        _RESOLVE_RESULT["extract_folder"] = r"E:\ws\TYPE_A\\"
        marked.read_texture_marks("LOD0.4a178546-18468-0")
        marked.clear_mark_cache()
        marked.read_texture_marks("LOD0.4a178546-18468-0")
        self.assertEqual(len(_RESOLVE_CALLS), 2)


class BuildMarkedMaterialsTests(unittest.TestCase):
    def setUp(self):
        marked.clear_mark_cache()
        _RESOLVE_CALLS.clear()
        _RESOLVE_RESULT.update(marks=[], extract_folder="", error=None)
        _MATERIALS.clear()

        self.temp_dir = tempfile.mkdtemp(prefix="marked_build_")
        self.extract_folder = os.path.join(self.temp_dir, "TYPE_A")
        os.makedirs(self.extract_folder, exist_ok=True)
        self.spec_calls = []
        self.spec_types = []
        self.principled_calls = []
        self.render_calls = []

        def _fake_spec(material, socket_type, texture_path, logic_name):
            # 规范材质的真实路径：_apply_spec_material（原理化 BSDF + 单通道）
            self.spec_calls.append((material.name, texture_path, logic_name))
            self.spec_types.append(socket_type)

        def _fake_principled(material):
            self.principled_calls.append(material.name)
            return types.SimpleNamespace(), types.SimpleNamespace()

        def _fake_channel(node_tree, principled, socket_type, texture_path, logic_name):
            self.principled_calls.append(socket_type)

        def _fake_render(material, entries, logic_name):
            self.render_calls.append((material.name, list(entries), logic_name))

        # 2026-10-09 起规范材质与渲染材质共用「原理化 BSDF + 单通道」这一套
        # （与工作文件里的 MOD 规范材质完全一致），所以这里只验证分派与产出，
        # 不真建节点。
        for target, fake in (
            ("_apply_spec_material", _fake_spec),
            ("_make_principled_material", _fake_principled),
            ("_wire_channel", _fake_channel),
        ):
            patcher = mock.patch.object(marked, target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

        # 渲染材质真的会建节点树，轻量 stub 没有 bpy 节点 API；这里只验证分派。
        self._render_patch = mock.patch.object(
            marked, "_build_render_material", _fake_render
        )
        self._render_patch.start()
        self.addCleanup(self._render_patch.stop)

    def _write(self, filename):
        path = os.path.join(self.extract_folder, filename)
        with open(path, "wb") as handle:
            handle.write(b"x")
        return path

    def _use_marks(self, marks):
        _RESOLVE_RESULT["marks"] = marks
        _RESOLVE_RESULT["extract_folder"] = self.extract_folder + os.sep

    def test_no_marks_returns_zero_without_materials(self):
        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )
        self.assertEqual(created, 0)
        self.assertEqual(_MATERIALS, [])
        self.assertEqual(obj.data.materials, [])

    def test_builds_one_material_per_marked_type(self):
        """标记了 4 种就建 4 个材质——不写死类型数量。"""
        marks = []
        for mark_name in ("DiffuseMap", "NormalMap", "LightMap", "MaterialMap"):
            self._write(f"4a178546-18468-0-{mark_name}.dds")
            marks.append(_mark(mark_name))
        self._use_marks(marks)

        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )

        self.assertEqual(created, 4)
        self.assertEqual(
            [material.name for material in obj.data.materials],
            [
                "IMGPV_4a178546-18468-0",
                "DiffuseMap_4a178546-18468-0",
                "NormalMap_4a178546-18468-0",
                "LightMap_4a178546-18468-0",
                "MaterialMap_4a178546-18468-0",
            ],
        )

    def test_builds_only_marked_types(self):
        """只标记了 2 种时只建 2 个——多出来的类型绝不能凭空补上。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._write("4a178546-18468-0-LightMap.dds")
        self._use_marks([_mark("DiffuseMap"), _mark("LightMap")])

        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )

        self.assertEqual(created, 2)
        self.assertEqual(
            [material.name for material in obj.data.materials],
            [
                "IMGPV_4a178546-18468-0",
                "DiffuseMap_4a178546-18468-0",
                "LightMap_4a178546-18468-0",
            ],
        )

    def test_accepts_types_beyond_the_known_four(self):
        """有些模型还会标记 BodyMaskMap 之类的类型，同样要建。"""
        self._write("4a178546-18468-0-BodyMaskMap.dds")
        self._use_marks([_mark("BodyMaskMap")])

        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )

        self.assertEqual(created, 1)
        self.assertEqual(obj.data.materials[0].name, "IMGPV_4a178546-18468-0")
        self.assertEqual(obj.data.materials[1].name, "BodyMaskMap_4a178546-18468-0")
        self.assertEqual([call[0] for call in self.spec_calls], ["BodyMaskMap_4a178546-18468-0"])

    def test_unknown_mark_name_still_gets_a_material(self):
        self._write("4a178546-18468-0-SomeUnknownMap.dds")
        self._use_marks([_mark("SomeUnknownMap")])

        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )

        self.assertEqual(created, 1)
        self.assertEqual(obj.data.materials[0].name, "IMGPV_4a178546-18468-0")
        self.assertEqual(obj.data.materials[1].name, "SomeUnknownMap_4a178546-18468-0")
        self.assertEqual([call[0] for call in self.spec_calls], ["SomeUnknownMap_4a178546-18468-0"])

    def test_hash_marks_are_skipped(self):
        """Hash 型标记靠贴图 hash 匹配游戏原有纹理，没有固定文件名与槽位。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks(
            [
                _mark("DiffuseMap", "Slot"),
                _mark("HashOnlyMap", "Hash"),
            ]
        )

        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )

        self.assertEqual(created, 1)
        self.assertEqual(len(obj.data.materials), 2)

    def test_lightmap_is_a_plain_channel_in_both_materials(self):
        """LightMap 不再按绝区零拆通道：规范材质与渲染材质都当普通通道接。

        2026-10-09 用户实机确认这张图的语义是折射相关，绝区零那套
        G→金属度 / B→高光的拆法已整体删除。
        """
        self._write("4a178546-18468-0-LightMap.dds")
        self._use_marks([_mark("LightMap")])

        obj = _FakeObject()
        marked.build_marked_materials(obj, "4a178546-18468-0", self.extract_folder, "ZZMI")

        self.assertEqual([call[0] for call in self.spec_calls], ["LightMap_4a178546-18468-0"])
        self.assertEqual(self.spec_calls[0][2], "ZZMI")
        self.assertEqual(self.spec_types, ["LIGHTMAP"])
        render_name, entries, _logic = self.render_calls[0]
        self.assertEqual(render_name, "IMGPV_4a178546-18468-0")
        self.assertEqual([socket_type for socket_type, _path in entries], ["LIGHTMAP"])
        self.assertFalse(hasattr(marked, "_wire_zzz_light"), "绝区零拆通道接法应已删除")

    def test_spec_materials_are_built_for_every_marked_type(self):
        """每种标记类型都建一个规范材质，并按类型分派到对应的通道接法。

        2026-10-09 起规范材质与渲染材质统一为「原理化 BSDF + 单通道」，
        与工作文件里的 MOD 规范材质完全一致。
        """
        mark_names = (
            "DiffuseMap",
            "NormalMap",
            "LightMap",
            "MaterialMap",
            "BodyMaskMap",
            "SomeUnknownMap",
        )
        for mark_name in mark_names:
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks([_mark(mark_name) for mark_name in mark_names])

        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )

        self.assertEqual(created, len(mark_names))
        self.assertEqual(
            [call[0] for call in self.spec_calls],
            [f"{mark_name}_4a178546-18468-0" for mark_name in mark_names],
        )
        self.assertEqual([call[2] for call in self.spec_calls], ["ZZMI"] * len(mark_names))
        self.assertEqual(
            self.spec_types,
            ["DIFFUSE", "NORMAL", "LIGHTMAP", "SURFACE", "BODYMASK", ""],
        )
        # 渲染材质仍然拿到全部通道
        self.assertEqual(len(self.render_calls), 1)
        _render_name, entries, _logic = self.render_calls[0]
        self.assertEqual(
            [socket_type for socket_type, _path in entries],
            ["DIFFUSE", "NORMAL", "LIGHTMAP", "SURFACE", "BODYMASK", ""],
        )

    def test_render_material_gets_every_wired_channel(self):
        """同一面要同时显示多种贴图，只能让它们待在同一个材质里（Blender 没有跨材质）。"""
        mark_names = ("DiffuseMap", "NormalMap", "LightMap", "MaterialMap")
        for mark_name in mark_names:
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks([_mark(mark_name) for mark_name in mark_names])

        obj = _FakeObject()
        marked.build_marked_materials(obj, "4a178546-18468-0", self.extract_folder, "ZZMI")

        self.assertEqual(len(self.render_calls), 1)
        render_name, entries, logic_name = self.render_calls[0]
        self.assertEqual(render_name, "IMGPV_4a178546-18468-0")
        self.assertEqual(
            [socket_type for socket_type, _path in entries],
            ["DIFFUSE", "NORMAL", "LIGHTMAP", "SURFACE"],
        )
        self.assertEqual(logic_name, "ZZMI")

    def test_render_material_is_slot_zero_and_spec_follows(self):
        """网格面 material_index 都是 0，渲染材质必须占槽 0 才真的被渲染。"""
        for mark_name in ("DiffuseMap", "LightMap"):
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks([_mark("DiffuseMap"), _mark("LightMap")])

        obj = _FakeObject()
        marked.build_marked_materials(obj, "4a178546-18468-0", self.extract_folder, "ZZMI")

        self.assertEqual(obj.data.materials[0].name, "IMGPV_4a178546-18468-0")
        self.assertEqual(obj.data.materials[1].name, "DiffuseMap_4a178546-18468-0")
        self.assertEqual(obj.data.materials[2].name, "LightMap_4a178546-18468-0")

    def test_render_material_failure_keeps_spec_materials(self):
        """渲染材质建不出来不能连累规范材质——导出识别靠的是后者。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks([_mark("DiffuseMap")])

        def _boom(_material, _entries, _logic_name):
            raise RuntimeError("node build failed")

        obj = _FakeObject()
        with mock.patch.object(marked, "_build_render_material", _boom):
            created = marked.build_marked_materials(
                obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
            )

        self.assertEqual(created, 1)
        self.assertEqual(
            [material.name for material in obj.data.materials],
            ["DiffuseMap_4a178546-18468-0"],
        )
        self.assertNotIn(
            "IMGPV_4a178546-18468-0", [material.name for material in _MATERIALS]
        )

    def test_missing_texture_file_is_skipped_others_still_built(self):
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks([_mark("DiffuseMap"), _mark("NormalMap")])

        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )

        self.assertEqual(created, 1)
        self.assertEqual(obj.data.materials[1].name, "DiffuseMap_4a178546-18468-0")
        self.assertEqual(len(_MATERIALS), 2)

    def test_all_textures_missing_returns_zero(self):
        self._use_marks([_mark("DiffuseMap")])
        obj = _FakeObject()
        created = marked.build_marked_materials(
            obj, "4a178546-18468-0", self.extract_folder, "ZZMI"
        )
        self.assertEqual(created, 0)
        self.assertEqual(obj.data.materials, [])
        self.assertEqual(_MATERIALS, [])

    def test_slot_assignment_replaces_existing_and_appends_rest(self):
        for mark_name in ("DiffuseMap", "NormalMap", "LightMap"):
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks([_mark("DiffuseMap"), _mark("NormalMap"), _mark("LightMap")])

        existing = types.SimpleNamespace(name="OldMaterial")
        obj = _FakeObject(materials=[existing])
        marked.build_marked_materials(obj, "4a178546-18468-0", self.extract_folder, "ZZMI")

        self.assertEqual(
            [material.name for material in obj.data.materials],
            [
                "IMGPV_4a178546-18468-0",
                "DiffuseMap_4a178546-18468-0",
                "NormalMap_4a178546-18468-0",
                "LightMap_4a178546-18468-0",
            ],
        )

    def test_extra_existing_slots_are_kept(self):
        """只填充/追加，不删减物体原有槽位（面索引才不会错位）。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks([_mark("DiffuseMap")])

        keep_a = types.SimpleNamespace(name="KeepA")
        keep_b = types.SimpleNamespace(name="KeepB")
        keep_c = types.SimpleNamespace(name="KeepC")
        obj = _FakeObject(materials=[keep_a, keep_b, keep_c])
        marked.build_marked_materials(obj, "4a178546-18468-0", self.extract_folder, "ZZMI")

        # 渲染材质占槽 0、规范材质占槽 1；槽 2 与导出无关，原样保留
        self.assertEqual(len(obj.data.materials), 3)
        self.assertEqual(obj.data.materials[0].name, "IMGPV_4a178546-18468-0")
        self.assertEqual(obj.data.materials[1].name, "DiffuseMap_4a178546-18468-0")
        self.assertIs(obj.data.materials[2], keep_c)

    def test_none_object_returns_zero(self):
        self.assertEqual(
            marked.build_marked_materials(None, "x", self.extract_folder, "ZZMI"), 0
        )

    def test_identity_is_derived_from_type_folder(self):
        """身份由 ``TYPE_<类型>`` 目录反推，与导入时写的 WorkspaceUniqueStr 同源。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks([_mark("DiffuseMap")])

        lod_dir = os.path.join(self.temp_dir, "LOD0")
        submesh_dir = os.path.join(lod_dir, "4a178546-18468-0")
        type_dir = os.path.join(submesh_dir, "TYPE_A")
        os.makedirs(type_dir, exist_ok=True)

        obj = _FakeObject()
        marked.build_marked_materials(obj, "4a178546-18468-0", type_dir, "ZZMI")

        self.assertEqual(_RESOLVE_CALLS[-1], "LOD0.4a178546-18468-0")


class BuildMissingMarkedMaterialsTests(unittest.TestCase):
    """给**已有**旧物体补齐贴图材质（按钮那条路径）"""

    def setUp(self):
        marked.clear_mark_cache()
        _RESOLVE_CALLS.clear()
        _RESOLVE_RESULT.update(marks=[], extract_folder="", error=None)
        _MATERIALS.clear()

        self.temp_dir = tempfile.mkdtemp(prefix="marked_fill_")
        self.extract_folder = os.path.join(self.temp_dir, "TYPE_A")
        os.makedirs(self.extract_folder, exist_ok=True)
        self.spec_calls = []
        self.render_calls = []
        self.unique_str = "LOD0.4a178546-18468-0"

        def _fake_spec(material, socket_type, texture_path, logic_name):
            self.spec_calls.append((material.name, texture_path, logic_name))

        def _fake_render(material, entries, logic_name):
            self.render_calls.append((material.name, list(entries), logic_name))

        for target, fake in (
            ("_apply_spec_material", _fake_spec),
            ("_build_render_material", _fake_render),
        ):
            patcher = mock.patch.object(marked, target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, filename):
        path = os.path.join(self.extract_folder, filename)
        with open(path, "wb") as handle:
            handle.write(b"x")
        return path

    def _use_marks(self, mark_names):
        _RESOLVE_RESULT["marks"] = [_mark(name) for name in mark_names]
        _RESOLVE_RESULT["extract_folder"] = self.extract_folder + os.sep

    def _obj(self, materials=None, unique_str=None):
        props = {"3DMigoto:WorkspaceUniqueStr": unique_str or self.unique_str}
        return _FakeObject(materials=materials, custom_props=props)

    def test_fills_only_missing_types(self):
        for mark_name in ("DiffuseMap", "NormalMap", "LightMap"):
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks(["DiffuseMap", "NormalMap", "LightMap"])

        existing = types.SimpleNamespace(name="DiffuseMap_4a178546-18468-0")
        obj = self._obj(materials=[existing])
        created, reused, warnings = marked.build_missing_marked_materials(obj)

        self.assertEqual((created, reused), (2, 1))
        self.assertEqual(warnings, [])
        self.assertEqual(
            [material.name for material in obj.data.materials],
            [
                "IMGPV_4a178546-18468-0",
                "DiffuseMap_4a178546-18468-0",
                "NormalMap_4a178546-18468-0",
                "LightMap_4a178546-18468-0",
            ],
        )
        # 已有的那个材质原样留在槽里（同一个对象），没有被新建的顶替
        self.assertIs(obj.data.materials[1], existing)

    def test_existing_material_is_not_rebuilt(self):
        """「只补缺的」：已有同名词同类型材质不能重建节点（会抹掉手工修改）。"""
        for mark_name in ("DiffuseMap", "NormalMap"):
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks(["DiffuseMap", "NormalMap"])

        existing = types.SimpleNamespace(name="DiffuseMap_4a178546-18468-0")
        obj = self._obj(materials=[existing])
        marked.build_missing_marked_materials(obj)

        self.assertNotIn(
            "DiffuseMap_4a178546-18468-0", [name for name, _p, _l in self.spec_calls]
        )
        self.assertIn(
            "NormalMap_4a178546-18468-0", [name for name, _p, _l in self.spec_calls]
        )

    def test_fully_populated_object_creates_nothing(self):
        """物体已备齐全部类型时不新建任何材质数据块（用户反馈的「重复添加」）。"""
        for mark_name in ("DiffuseMap", "NormalMap", "LightMap"):
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks(["DiffuseMap", "NormalMap", "LightMap"])

        obj = self._obj(
            materials=[
                types.SimpleNamespace(name="IMGPV_4a178546-18468-0"),
                types.SimpleNamespace(name="DiffuseMap_4a178546-18468-0"),
                types.SimpleNamespace(name="NormalMap_4a178546-18468-0"),
                types.SimpleNamespace(name="LightMap_4a178546-18468-0"),
            ]
        )
        created, reused, warnings = marked.build_missing_marked_materials(obj)

        self.assertEqual((created, reused), (0, 3))
        self.assertEqual(warnings, [])
        self.assertEqual(self.spec_calls, [])
        self.assertEqual(self.render_calls, [])
        self.assertEqual(_MATERIALS, [])
        self.assertEqual(
            [material.name for material in obj.data.materials],
            [
                "IMGPV_4a178546-18468-0",
                "DiffuseMap_4a178546-18468-0",
                "NormalMap_4a178546-18468-0",
                "LightMap_4a178546-18468-0",
            ],
        )

    def test_existing_material_with_a_different_mesh_suffix_is_reused(self):
        """名字口径不同（网格被复制成 .001）时按类型复用，不再重复添加。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks(["DiffuseMap"])

        existing = types.SimpleNamespace(name="DiffuseMap_4a178546-18468-0")
        obj = self._obj(materials=[existing])
        created, reused, warnings = marked.build_missing_marked_materials(
            obj, mesh_name="4a178546-18468-0.001"
        )

        self.assertEqual((created, reused), (0, 1))
        self.assertEqual(warnings, [])
        self.assertEqual(self.spec_calls, [])
        self.assertIn(existing, obj.data.materials)

    def test_existing_render_material_with_a_different_mesh_suffix_is_reused(self):
        """渲染材质同理：已有 IMGPV_ 材质时不再新建一份。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks(["DiffuseMap"])

        render = types.SimpleNamespace(name="IMGPV_4a178546-18468-0")
        obj = self._obj(materials=[render])
        marked.build_missing_marked_materials(obj, mesh_name="4a178546-18468-0.001")

        self.assertEqual(self.render_calls, [])
        self.assertIs(obj.data.materials[0], render)

    def test_identity_comes_from_workspace_unique_str_prop(self):
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks(["DiffuseMap"])
        obj = self._obj(unique_str="LOD1.4a178546-18468-0")
        marked.build_missing_marked_materials(obj)
        self.assertEqual(_RESOLVE_CALLS[-1], "LOD1.4a178546-18468-0")

    def test_falls_back_to_mesh_name_without_prop(self):
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks(["DiffuseMap"])
        obj = _FakeObject(name="4a178546-18468-0")
        created, _reused, _warnings = marked.build_missing_marked_materials(obj)
        self.assertEqual(created, 1)
        self.assertEqual(_RESOLVE_CALLS[-1], "4a178546-18468-0")

    def test_render_material_is_slot_zero_and_holds_all_channels(self):
        for mark_name in ("DiffuseMap", "NormalMap", "LightMap", "MaterialMap"):
            self._write(f"4a178546-18468-0-{mark_name}.dds")
        self._use_marks(["DiffuseMap", "NormalMap", "LightMap", "MaterialMap"])

        obj = self._obj()
        marked.build_missing_marked_materials(obj)

        self.assertEqual(obj.data.materials[0].name, "IMGPV_4a178546-18468-0")
        self.assertEqual(len(self.render_calls), 1)
        self.assertEqual(
            [socket_type for socket_type, _path in self.render_calls[0][1]],
            ["DIFFUSE", "NORMAL", "LIGHTMAP", "SURFACE"],
        )

    def test_existing_render_material_is_reused(self):
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks(["DiffuseMap"])

        existing_render = types.SimpleNamespace(name="IMGPV_4a178546-18468-0")
        obj = self._obj(materials=[existing_render])
        created, _reused, _warnings = marked.build_missing_marked_materials(obj)

        self.assertEqual(created, 1)
        self.assertEqual(self.render_calls, [])
        self.assertIs(obj.data.materials[0], existing_render)

    def test_render_material_skips_channels_whose_texture_is_gone(self):
        self._use_marks(["DiffuseMap", "NormalMap"])
        self._write("4a178546-18468-0-NormalMap.dds")

        existing = types.SimpleNamespace(name="DiffuseMap_4a178546-18468-0")
        obj = self._obj(materials=[existing])
        created, reused, warnings = marked.build_missing_marked_materials(obj)

        self.assertEqual((created, reused), (1, 1))
        self.assertTrue(warnings)
        self.assertEqual(
            [socket_type for socket_type, _path in self.render_calls[0][1]], ["NORMAL"]
        )

    def test_missing_texture_warns_and_continues(self):
        self._use_marks(["DiffuseMap", "NormalMap"])
        self._write("4a178546-18468-0-DiffuseMap.dds")

        obj = self._obj()
        created, reused, warnings = marked.build_missing_marked_materials(obj)

        self.assertEqual((created, reused), (1, 0))
        self.assertEqual(len(warnings), 1)
        self.assertIn("NormalMap", warnings[0])

    def test_no_marks_returns_warning(self):
        obj = self._obj()
        created, reused, warnings = marked.build_missing_marked_materials(obj)
        self.assertEqual((created, reused), (0, 0))
        self.assertEqual(len(warnings), 1)
        self.assertIn("读不到贴图标记", warnings[0])

    def test_none_object_returns_warning(self):
        self.assertEqual(marked.build_missing_marked_materials(None), (0, 0, ["物体为空"]))

    def test_unrelated_existing_slots_are_kept(self):
        """用户自己加的、标记里没有的材质不能被删掉。"""
        self._write("4a178546-18468-0-DiffuseMap.dds")
        self._use_marks(["DiffuseMap"])

        custom = types.SimpleNamespace(name="BodyMaskMap_4a178546-18468-0")
        obj = self._obj(materials=[custom])
        marked.build_missing_marked_materials(obj)

        self.assertEqual(
            [material.name for material in obj.data.materials],
            [
                "IMGPV_4a178546-18468-0",
                "DiffuseMap_4a178546-18468-0",
                "BodyMaskMap_4a178546-18468-0",
            ],
        )
        self.assertIs(obj.data.materials[2], custom)

    def test_same_name_material_on_another_object_is_not_reused(self):
        """跨 LOD 的同身份部件材质名相同，绝不能跨物体复用同一个材质。"""
        other_object_material = types.SimpleNamespace(name="NormalMap_4a178546-18468-0")
        _MATERIALS.append(other_object_material)
        self._write("4a178546-18468-0-NormalMap.dds")
        self._use_marks(["NormalMap"])

        obj = self._obj()
        created, reused, _warnings = marked.build_missing_marked_materials(obj)

        self.assertEqual((created, reused), (1, 0))
        self.assertIsNot(obj.data.materials[1], other_object_material)


class NormalWiringTests(unittest.TestCase):
    """法线统一走固定链：sRGB 读 → 「sRGB->Non-Color」→「法线贴图_补Z」→ Normal。

    2026-10-09 用户要求与工作文件里的 MOD 规范材质完全一致：不再按游戏类型分流
    （原 IdentityV 反相 G / 标准 / ZZMI·GIMI 由 R/G 重建 Z 三种接法已删除）。
    """

    def test_normal_wiring_is_the_fixed_group_chain(self):
        import inspect

        source = inspect.getsource(marked._wire_normal)
        self.assertIn("_ensure_normal_group_z", source)
        self.assertIn("_ensure_node_group", source)
        self.assertNotIn("apply_normal_texture", source)

    def test_channel_dispatch_passes_logic_name_to_normal(self):
        """logic_name 仍一路传到法线接法（签名保持不变，接法本身已与游戏无关）。"""
        seen = []
        original = marked._wire_normal
        marked._wire_normal = lambda *args: seen.append(args)
        self.addCleanup(setattr, marked, "_wire_normal", original)

        marked._wire_channel("TREE", "BSDF", "NORMAL", "n.dds", "ZZMI")

        self.assertEqual(seen, [("TREE", "BSDF", "n.dds", "ZZMI")])

    def test_stale_z_probe_is_gone(self):
        """旧的「缺 Z 补 1」缩略图探测已随接法统一移除，不要复活。"""
        self.assertFalse(hasattr(marked, "_needs_z_rebuild"))
        self.assertFalse(hasattr(marked, "_PROBE_CACHE"))


if __name__ == "__main__":
    unittest.main()
