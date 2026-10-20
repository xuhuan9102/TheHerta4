# -*- coding: utf-8 -*-
"""材质转资源pro「清除未指定部件的游戏贴图」（``clear_non_target_textures``）。

被测契约
--------
1. **逐网格归属**：绑定行按「该网格的绘制点之前最后一次设置的值」归到具体部件。
   基础导出把绑定写在 ``; [mesh:...]`` 之前、材质转资源重建后写在注释之后，
   两种排布都必须归对。
2. **只删非目标部件在用的**：目标部件（或归属不明部件）绘制时用到的绑定行
   一律保留，即使它同时也被非目标部件继承。
3. **资源定义段回收**：仅当被解除引用的 ``[Resource-...]`` 段不再被任何位置
   引用时才删除。
4. **贴图文件**：只删这些定义段指向的、且不再被任何 INI 引用的文件；
   越界路径（``../``）绝不删。

装载沿用 ``tests/test_custom_material_assign_scan_objects.py`` 的 fake-package 范式。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


PKG = "_custom_material_assign_clear_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package


_fake_bpy = types.ModuleType("bpy")
_fake_bpy.types = types.SimpleNamespace(
    PropertyGroup=object,
    Operator=object,
    Object=object,
)
_fake_bpy.props = types.SimpleNamespace(
    StringProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
    PointerProperty=lambda **_kwargs: None,
)
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups={})
sys.modules["bpy"] = _fake_bpy


def _parse_ini_content(content):
    preamble_lines = []
    sections = {}
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
    """只补上被测方法依赖的既有设施；解析/序列化照抄真实实现。"""

    @staticmethod
    def _parse_ini_content(content):
        return _parse_ini_content(content)

    @classmethod
    def _serialize_ini_content(
        cls,
        preamble_lines,
        sections,
        transparency_sections=None,
        preserved_tail_content="",
        preserved_driver_content="",
    ):
        return _serialize_ini_content(
            preamble_lines,
            sections,
            transparency_sections=transparency_sections,
            preserved_tail_content=preserved_tail_content,
            preserved_driver_content=preserved_driver_content,
        )

    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", str(content or "")

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return str(content or ""), ""

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        """简化版查找：精确名 + 去掉 ``_copy/_dupN/_chainN`` 后缀。"""
        import re as _re

        candidates = [mesh_name]
        stripped = _re.sub(r"_(?:copy|dup\d+|chain\d+)$", "", mesh_name)
        if stripped != mesh_name:
            candidates.append(stripped)
        for name in candidates:
            obj = _fake_bpy.data.objects.get(name)
            if obj is None:
                continue
            if object_filter is not None and not object_filter(obj):
                continue
            return obj
        return None

    #: 测试可切换：False 表示该部件读不到材质
    MATERIAL_READABLE = True

    def find_matching_materials(self, obj, texture_type):
        if not _StubMaterialBase.MATERIAL_READABLE:
            return []
        return [types.SimpleNamespace(name="DiffuseMap")]


_material_stub = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_stub.MATERIAL_DETECT_PRESETS = ["DiffuseMap", "NormalMap", "LightMap", "MaterialMap"]
_material_stub.SSMTNode_PostProcess_MaterialBase = _StubMaterialBase
sys.modules[_material_stub.__name__] = _material_stub

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


class _FakeNode(module.SSMTNode_PostProcess_CustomMaterialAssign):
    """借真实类的方法，只替换 bpy 收集来的部件列表。"""

    def __init__(self, target_names):
        self.target_items = [
            types.SimpleNamespace(
                target_object=types.SimpleNamespace(name=name, type="MESH")
            )
            for name in target_names
        ]
        self.use_global_assign = False
        self.clear_non_target_textures = True


class _Base(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.objects.clear()
        _StubMaterialBase.MATERIAL_READABLE = True
        self.addCleanup(setattr, _StubMaterialBase, "MATERIAL_READABLE", True)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mod_root = self._tmp.name

    # ---- 工具 -------------------------------------------------------- #

    def _register_objects(self, *names):
        for name in names:
            _fake_bpy.data.objects[name] = types.SimpleNamespace(name=name, type="MESH")

    def _write_ini(self, content, file_name="test.ini"):
        path = os.path.join(self.mod_root, file_name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        return path

    def _read(self, path="test.ini"):
        with open(os.path.join(self.mod_root, path), "r", encoding="utf-8") as handle:
            return handle.read()

    def _write_texture(self, relative):
        path = os.path.join(self.mod_root, relative.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"dds")
        return path

    def _run(self, target_names):
        node = _FakeNode(list(target_names))
        return node._clear_non_target_textures(self.mod_root)


_BINDINGS = (
    r"Resource\ZZMI\Diffuse = ref Resource-aaa-1-0-DiffuseMap",
    r"Resource\ZZMI\NormalMap = ref Resource-aaa-1-0-NormalMap",
)


def _ini_bindings_before_mesh(mesh_name):
    return "\n".join(
        [
            "[TextureOverride_LOD0.aaa_1_0]",
            "hash = aaa",
            _BINDINGS[0],
            _BINDINGS[1],
            "; [mesh:%s] [vertex_count:8]" % mesh_name,
            "drawindexed = 6,0,0",
            "",
            "[Resource-aaa-1-0-DiffuseMap]",
            "filename = Textures/aaa-1-0-DiffuseMap.dds",
            "",
            "[Resource-aaa-1-0-NormalMap]",
            "filename = Textures/aaa-1-0-NormalMap.dds",
            "",
        ]
    )


class NonTargetCleanupTests(_Base):
    def test_removes_bindings_sections_and_files(self):
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(_ini_bindings_before_mesh("LOD0.aaa-1-0.杂项_copy"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        self._write_texture("Textures/aaa-1-0-NormalMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource\\ZZMI\\Diffuse = null", content)
        self.assertIn("Resource\\ZZMI\\NormalMap = null", content)
        self.assertNotIn("ref Resource-aaa-1-0-DiffuseMap", content)
        self.assertNotIn("ref Resource-aaa-1-0-NormalMap", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertNotIn("[Resource-aaa-1-0-NormalMap]", content)
        # 绝不写 ps-t 解绑：那是显式解绑，会让部件变纯黑（实测）
        self.assertNotIn("ps-t0 = null", content)
        self.assertNotIn("ps-t9 = null", content)
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-NormalMap.dds"))
        )
        # 段头与绘制行必须保留
        self.assertIn("[TextureOverride_LOD0.aaa_1_0]", content)
        self.assertIn("drawindexed = 6,0,0", content)

    def test_keeps_target_part_untouched(self):
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(_ini_bindings_before_mesh("LOD0.aaa-1-0.杂项_copy"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        self._write_texture("Textures/aaa-1-0-NormalMap.dds")

        self._run(["LOD0.aaa-1-0.杂项"])

        content = self._read()
        self.assertIn("Resource\\ZZMI\\Diffuse", content)
        self.assertIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertTrue(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )

    def test_bindings_after_mesh_comment_are_attributed(self):
        """材质转资源重建后的排布：绑定写在 ``; [mesh:...]`` 之后。"""
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "hash = aaa",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy] [vertex_count:8]",
                    _BINDINGS[0],
                    "drawindexed = 6,0,0",
                    "",
                    "[Resource-aaa-1-0-DiffuseMap]",
                    "filename = Textures/aaa-1-0-DiffuseMap.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource\\ZZMI\\Diffuse = null", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)

    def test_mesh_before_mod_coverage_gets_null(self):
        """同段内非目标网格排在 MOD 覆盖之前：它可以安全回退原生。"""
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy]",
                    r"Resource\ZZMI\Diffuse = ref Resource-aaa-1-0-DiffuseMap",
                    "drawindexed = 3,0,0",
                    "",
                    "; [mesh:LOD0.bbb-2-0.主体_copy]",
                    r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_DiffuseMap",
                    "drawindexed = 6,0,0",
                    "",
                    "[Resource-aaa-1-0-DiffuseMap]",
                    "filename = Textures/aaa-1-0-DiffuseMap.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource\\ZZMI\\Diffuse = null", content)
        self.assertNotIn("ref Resource-aaa-1-0-DiffuseMap", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertNotIn("ps-t0 = null", content)
        self.assertIn(
            r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_DiffuseMap", content
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )

    def test_mesh_after_mod_coverage_is_left_untouched(self):
        """排在 MOD 覆盖之后的非目标网格必须保持原样。

        同 hash 段内前面的块已经占住槽位，这时置 null 只是「不覆盖」，拿不到
        原生贴图；而 ``ps-tN = null`` 会显式解绑导致纯黑（实测）。所以只能放弃。
        """
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "; [mesh:LOD0.bbb-2-0.主体_copy]",
                    r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_DiffuseMap",
                    "drawindexed = 6,0,0",
                    "",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy]",
                    r"Resource\ZZMI\Diffuse = ref Resource-aaa-1-0-DiffuseMap",
                    "drawindexed = 3,0,0",
                    "",
                    "[Resource-aaa-1-0-DiffuseMap]",
                    "filename = Textures/aaa-1-0-DiffuseMap.dds",
                    "",
                ]
            )
        )

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        # 该网格排在 MOD 覆盖之后：保持原样，既不置 null 也不解绑
        self.assertIn(r"Resource\ZZMI\Diffuse = ref Resource-aaa-1-0-DiffuseMap", content)
        self.assertNotIn("ps-t0 = null", content)
        self.assertIn("[Resource-aaa-1-0-DiffuseMap]", content)
        # 目标部件自己的绑定不能被改动
        self.assertIn(
            r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_DiffuseMap", content
        )

    def test_two_meshes_target_first_keeps_non_target(self):
        """目标部件在前、非目标部件在后：后者拿不到原生贴图，整套保持原样。"""
        self._register_objects(
            "LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体", "LOD0.bbb-2-0.主体.尾巴"
        )
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "hash = aaa",
                    "; [mesh:LOD0.bbb-2-0.主体_copy]",
                    r"Resource\ZZMI\Diffuse = ref Resource_KeepMe",
                    "drawindexed = 6,0,0",
                    "",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy]",
                    r"Resource\ZZMI\Diffuse = ref Resource-DropMe",
                    "drawindexed = 3,0,0",
                    "",
                    "[Resource_KeepMe]",
                    "filename = Textures/keep.dds",
                    "",
                    "[Resource-DropMe]",
                    "filename = Textures/drop.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/keep.dds")
        self._write_texture("Textures/drop.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource_KeepMe", content)
        # 排在 MOD 覆盖之后，无法安全回退：绑定、定义段、贴图文件全部保留
        self.assertIn("Resource-DropMe", content)
        self.assertIn("[Resource-DropMe]", content)
        self.assertNotIn("ps-t0 = null", content)
        self.assertTrue(os.path.exists(os.path.join(self.mod_root, "Textures", "keep.dds")))
        self.assertTrue(os.path.exists(os.path.join(self.mod_root, "Textures", "drop.dds")))

    def test_orphan_section_default_is_nulled(self):
        """每块都有自己的绑定时，段首那份无人继承的默认绑定也要置空。

        来源：基础导出按子网格顺序写绑定，第一个子网格那份落在它的注释之前；
        材质转资源随后给每个块补一份自己的，段首这份就成了没有任何绘制点用到
        的孤儿 —— 它引用着的贴图因此永远回收不掉。
        """
        self._register_objects("LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.bbb_2_0]",
                    "hash = bbb",
                    r"Resource\ZZMI\Diffuse = ref Resource-bbb-2-0-DiffuseMap",
                    r"run = CommandList\ZZMI\SetTextures",
                    "; [mesh:LOD0.bbb-2-0.主体_copy]",
                    r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_DiffuseMap",
                    "drawindexed = 6,0,0",
                    "",
                    "[Resource-bbb-2-0-DiffuseMap]",
                    "filename = Textures/bbb-2-0-DiffuseMap.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/bbb-2-0-DiffuseMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertNotIn("ref Resource-bbb-2-0-DiffuseMap", content)
        self.assertIn(
            r"Resource\ZZMI\Diffuse = ref Resource_DiffuseMap_DiffuseMap", content
        )
        self.assertNotIn("[Resource-bbb-2-0-DiffuseMap]", content)
        self.assertNotIn("ps-t0 = null", content)
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "bbb-2-0-DiffuseMap.dds"))
        )

    def test_target_without_material_is_treated_as_non_target(self):
        """目标部件读不到材质时按非目标处理：置空引用并回收贴图。

        没设材质 / 材质节点配置不正确时，材质转资源不会给它写资源引用，它只会
        继承同段别的绑定 —— 贴图是错的。所以必须回退原生并在结束时点名提示。
        """
        self._register_objects("LOD0.bbb-2-0.主体")
        _StubMaterialBase.MATERIAL_READABLE = False
        self._write_ini(_ini_bindings_before_mesh("LOD0.bbb-2-0.主体_copy"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        self._write_texture("Textures/aaa-1-0-NormalMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource\\ZZMI\\Diffuse = null", content)
        self.assertNotIn("ref Resource-aaa-1-0-DiffuseMap", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertNotIn("ps-t0 = null", content)
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )

    def test_readable_material_keeps_target_untouched(self):
        """材质正常时目标部件绝不因探测而被误伤。"""
        self._register_objects("LOD0.bbb-2-0.主体")
        _StubMaterialBase.MATERIAL_READABLE = True
        self._write_ini(_ini_bindings_before_mesh("LOD0.bbb-2-0.主体_copy"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("ref Resource-aaa-1-0-DiffuseMap", content)
        self.assertIn("[Resource-aaa-1-0-DiffuseMap]", content)

    def test_inherited_binding_used_by_target_is_protected(self):
        """段首绑定被目标部件继承时必须保留，哪怕非目标部件也在用。"""
        self._register_objects(
            "LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体", "LOD0.bbb-2-0.主体.尾巴"
        )
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "hash = aaa",
                    r"Resource\ZZMI\Diffuse = ref Resource-Shared",
                    "; [mesh:LOD0.bbb-2-0.主体_copy]",
                    "drawindexed = 6,0,0",
                    "",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy]",
                    "drawindexed = 3,0,0",
                    "",
                    "[Resource-Shared]",
                    "filename = Textures/shared.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/shared.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource-Shared", content)
        self.assertTrue(os.path.exists(os.path.join(self.mod_root, "Textures", "shared.dds")))

    def test_unknown_mesh_name_protects_bindings(self):
        """网格名对不上任何导出物体时不删，避免误伤。"""
        self._register_objects("LOD0.bbb-2-0.主体")
        self._write_ini(_ini_bindings_before_mesh("LOD0.zzz-9-9.未知"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource-aaa-1-0-DiffuseMap", content)

    def test_section_without_mesh_comment_is_untouched(self):
        """Hash 风格段（无 ``; [mesh:...]``）无从判定归属，保持原样。"""
        self._register_objects("LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_deadbeef]",
                    "; 贴图.dds",
                    "hash = deadbeef",
                    "this = Resource_Texture_deadbeef",
                    "",
                    "[Resource_Texture_deadbeef]",
                    "filename = Textures/tex_deadbeef.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/tex_deadbeef.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("this = Resource_Texture_deadbeef", content)
        self.assertIn("[Resource_Texture_deadbeef]", content)
        self.assertTrue(
            os.path.exists(os.path.join(self.mod_root, "Textures", "tex_deadbeef.dds"))
        )

    def test_resource_kept_when_other_section_still_uses_it(self):
        self._register_objects(
            "LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体", "LOD0.bbb-2-0.主体.尾巴"
        )
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    r"Resource\ZZMI\Diffuse = ref Resource-Shared",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy]",
                    "drawindexed = 6,0,0",
                    "",
                    "[TextureOverride_LOD0.bbb_2_0]",
                    r"Resource\ZZMI\Diffuse = ref Resource-Shared",
                    "; [mesh:LOD0.bbb-2-0.主体_copy]",
                    "drawindexed = 3,0,0",
                    "",
                    "[Resource-Shared]",
                    "filename = Textures/shared.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/shared.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertNotIn(
            "Resource\\ZZMI\\Diffuse = ref Resource-Shared", content.split("[TextureOverride_LOD0.bbb_2_0]")[0]
        )
        self.assertIn("[Resource-Shared]", content)
        self.assertTrue(os.path.exists(os.path.join(self.mod_root, "Textures", "shared.dds")))

    def test_texture_kept_when_referenced_by_other_ini(self):
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(_ini_bindings_before_mesh("LOD0.aaa-1-0.杂项_copy"), "a.ini")
        self._write_ini(
            "[Resource-aaa-1-0-DiffuseMap]\nfilename = Textures/aaa-1-0-DiffuseMap.dds\n",
            "b.ini",
        )
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        self.assertTrue(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )

    def test_path_traversal_is_not_deleted(self):
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    r"Resource\ZZMI\Diffuse = ref Resource-Escape",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy]",
                    "drawindexed = 6,0,0",
                    "",
                    "[Resource-Escape]",
                    "filename = ../outside.dds",
                    "",
                ]
            )
        )
        outside = os.path.join(os.path.dirname(self.mod_root), "outside.dds")
        with open(outside, "wb") as handle:
            handle.write(b"dds")
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))

        self._run(["LOD0.bbb-2-0.主体"])

        self.assertTrue(os.path.exists(outside))

    def test_no_targets_does_nothing(self):
        self._register_objects("LOD0.aaa-1-0.杂项")
        path = self._write_ini(_ini_bindings_before_mesh("LOD0.aaa-1-0.杂项_copy"))
        before = self._read()

        self._run([])

        self.assertEqual(before, self._read())
        self.assertTrue(os.path.exists(path))


class SlotStyleBindingTests(_Base):
    """回退绑定必须按参数类型分流（PR #23 的原始实现漏了这一步）。

    ``Resource\\<ns>\\Xxx`` 这类别名写 ``= null``；而 ``ps-tN`` / ``this`` 直接
    指向游戏槽位，写 ``= null`` 是**显式解绑**，会让该部件变纯黑（实测结论见
    ``blueprint/node_postprocess_custom_material_assign.py`` 顶部备忘），只能整行
    删除——不写赋值，游戏自己绑定的那张纹理就还在槽位里。
    """

    def _write_slot_ini(self, binding_line):
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "hash = aaa",
                    binding_line,
                    "; [mesh:LOD0.aaa-1-0.杂项_copy] [vertex_count:8]",
                    "drawindexed = 6,0,0",
                    "",
                    "[Resource-aaa-1-0-DiffuseMap]",
                    "filename = Textures/aaa-1-0-DiffuseMap.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")

    def test_ps_t_binding_is_deleted_instead_of_nulled(self):
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_slot_ini("ps-t0 = Resource-aaa-1-0-DiffuseMap")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertNotIn("ps-t0", content)
        self.assertNotIn("ps-t0 = null", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )
        # 段头与绘制行必须保留，段本身不能消失
        self.assertIn("[TextureOverride_LOD0.aaa_1_0]", content)
        self.assertIn("drawindexed = 6,0,0", content)

    def test_this_binding_is_deleted_instead_of_nulled(self):
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_slot_ini("this = Resource-aaa-1-0-DiffuseMap")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertNotIn("this", content)
        self.assertNotIn("this = null", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertIn("drawindexed = 6,0,0", content)

    def test_alias_and_slot_bindings_split_by_param_type(self):
        """同一段里两种形态并存：别名置 null、槽位整行删除。"""
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "hash = aaa",
                    r"Resource\ZZMI\Diffuse = ref Resource-aaa-1-0-DiffuseMap",
                    "ps-t0 = Resource-bbb-2-0-DiffuseMap",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy] [vertex_count:8]",
                    "drawindexed = 6,0,0",
                    "",
                    "[Resource-aaa-1-0-DiffuseMap]",
                    "filename = Textures/aaa-1-0-DiffuseMap.dds",
                    "",
                    "[Resource-bbb-2-0-DiffuseMap]",
                    "filename = Textures/bbb-2-0-DiffuseMap.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        self._write_texture("Textures/bbb-2-0-DiffuseMap.dds")

        self._run(["LOD0.bbb-2-0.主体"])

        content = self._read()
        self.assertIn("Resource\\ZZMI\\Diffuse = null", content)
        self.assertNotIn("ps-t0", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertNotIn("[Resource-bbb-2-0-DiffuseMap]", content)
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "bbb-2-0-DiffuseMap.dds"))
        )

    def test_slot_style_binding_of_target_part_is_kept(self):
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_slot_ini("ps-t0 = Resource-aaa-1-0-DiffuseMap")

        self._run(["LOD0.aaa-1-0.杂项"])

        content = self._read()
        self.assertIn("ps-t0 = Resource-aaa-1-0-DiffuseMap", content)
        self.assertIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertTrue(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )

    def test_slot_style_binding_after_mod_coverage_is_left_untouched(self):
        """排在 MOD 覆盖之后的槽位绑定动不了，必须原样留着（与别名分支同规则）。"""
        self._register_objects("LOD0.aaa-1-0.杂项", "LOD0.bbb-2-0.主体")
        self._write_ini(
            "\n".join(
                [
                    "[TextureOverride_LOD0.aaa_1_0]",
                    "hash = aaa",
                    "ps-t0 = Resource-aaa-1-0-DiffuseMap",
                    "; [mesh:LOD0.bbb-2-0.主体_copy] [vertex_count:8]",
                    "drawindexed = 6,0,0",
                    "ps-t0 = Resource-ccc-3-0-DiffuseMap",
                    "; [mesh:LOD0.aaa-1-0.杂项_copy] [vertex_count:8]",
                    "drawindexed = 6,0,0",
                    "",
                    "[Resource-aaa-1-0-DiffuseMap]",
                    "filename = Textures/aaa-1-0-DiffuseMap.dds",
                    "",
                    "[Resource-ccc-3-0-DiffuseMap]",
                    "filename = Textures/ccc-3-0-DiffuseMap.dds",
                    "",
                ]
            )
        )
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        self._write_texture("Textures/ccc-3-0-DiffuseMap.dds")
        before = self._read()

        self._run(["LOD0.bbb-2-0.主体"])

        self.assertEqual(before, self._read())


class SlotStyleParamHelperTests(unittest.TestCase):
    """``_is_slot_style_param`` 只认直接指向槽位的形态。"""

    def test_slot_style_params_are_recognised(self):
        for name in ("ps-t0", "ps-t9", "ps-t12", "PS-T3", " this ", "this", "THIS"):
            self.assertTrue(module._is_slot_style_param(name), name)

    def test_alias_and_other_params_are_not_slot_style(self):
        for name in (
            r"Resource\ZZMI\Diffuse",
            "Resource-aaa-1-0-DiffuseMap",
            "fxmap_ref",
            "",
            None,
        ):
            self.assertFalse(module._is_slot_style_param(name), name)


class IbKeyFromNameTests(unittest.TestCase):
    """``_ib_key_from_name``：从名字里提取 ``8位hex-数字-数字`` 的 IB 键。"""

    def test_extracts_key_with_lod_prefix_and_part_suffix(self):
        cls = module.SSMTNode_PostProcess_CustomMaterialAssign
        self.assertEqual(
            cls._ib_key_from_name("LOD0.6ef3d666-888-0.武器翅膀花纹贴片"),
            "6ef3d666-888-0",
        )
        self.assertEqual(
            cls._ib_key_from_name("LOD0.24a512cb-114-15546"), "24a512cb-114-15546"
        )
        self.assertEqual(
            cls._ib_key_from_name("LOD0.92cb56c9-64626-0.目标部件_copy"),
            "92cb56c9-64626-0",
        )

    def test_returns_lowercase(self):
        cls = module.SSMTNode_PostProcess_CustomMaterialAssign
        self.assertEqual(cls._ib_key_from_name("LOD0.6EF3D666-888-0"), "6ef3d666-888-0")

    def test_returns_none_when_key_is_absent(self):
        cls = module.SSMTNode_PostProcess_CustomMaterialAssign
        for name in (
            "LOD0.aaa-1-0.杂项",
            "LOD0.zzz-9-9.未知",
            "未知部件",
            "",
            None,
        ):
            self.assertIsNone(cls._ib_key_from_name(name), name)

    def test_longer_hex_run_is_not_truncated_into_a_key(self):
        cls = module.SSMTNode_PostProcess_CustomMaterialAssign
        self.assertIsNone(cls._ib_key_from_name("LOD0.6ef3d6666-888-0"))


class IbKeyVerdictTests(unittest.TestCase):
    """``_mesh_name_verdict`` 在找不到物体时按 IB 键回退判定。"""

    def setUp(self):
        _fake_bpy.data.objects.clear()
        self.addCleanup(_fake_bpy.data.objects.clear)

    def test_verdict_uses_target_ib_set_when_argument_is_omitted(self):
        node = _FakeNode(["LOD0.6ef3d666-888-0.目标部件"])
        cache = {}

        self.assertEqual(
            node._mesh_name_verdict("LOD0.6ef3d666-888-0", cache), "target"
        )
        self.assertEqual(
            node._mesh_name_verdict("LOD0.24a512cb-114-15546", cache), "non_target"
        )
        self.assertEqual(node._mesh_name_verdict("LOD0.未知部件", cache), "unknown")

    def test_found_object_still_wins_over_ib_key(self):
        """能找到物体时沿用原来的 target/non_target 判定，不因同名 IB 改变。"""
        _fake_bpy.data.objects["LOD0.aaa-1-0.杂项"] = types.SimpleNamespace(
            name="LOD0.aaa-1-0.杂项", type="MESH"
        )
        node = _FakeNode(["LOD0.6ef3d666-888-0.目标部件"])
        cache = {}

        self.assertEqual(
            node._mesh_name_verdict("LOD0.aaa-1-0.杂项", cache), "non_target"
        )

    def test_target_ib_set_collects_keys_from_target_objects(self):
        node = _FakeNode(
            [
                "LOD0.92cb56c9-64626-0.部件A",
                "LOD0.ca717c18-34338-0.部件B",
                "没有IB键的物体",
            ]
        )
        self.assertEqual(
            node._target_ib_set(), {"92cb56c9-64626-0", "ca717c18-34338-0"}
        )


class NamelessMeshIbFallbackTests(_Base):
    """导出 ini 的 mesh 注释只有 IB 键、物体名带部件名时的整段清理行为。"""

    def test_foreign_ib_without_part_name_is_cleaned(self):
        """无部件名的 mesh 段，IB 不在目标集合 -> 置 null、回收定义段与贴图。"""
        self._register_objects("LOD0.92cb56c9-64626-0.目标部件")
        # 物体名带部件名，精确查找对不上这条只有 IB 键的注释
        self._write_ini(_ini_bindings_before_mesh("LOD0.6ef3d666-888-0"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        self._write_texture("Textures/aaa-1-0-NormalMap.dds")

        self._run(["LOD0.92cb56c9-64626-0.目标部件"])

        content = self._read()
        self.assertIn("Resource\\ZZMI\\Diffuse = null", content)
        self.assertIn("Resource\\ZZMI\\NormalMap = null", content)
        self.assertNotIn("ref Resource-aaa-1-0-DiffuseMap", content)
        self.assertNotIn("[Resource-aaa-1-0-DiffuseMap]", content)
        self.assertNotIn("[Resource-aaa-1-0-NormalMap]", content)
        self.assertNotIn("ps-t0 = null", content)
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-NormalMap.dds"))
        )
        # 段头与绘制行保留
        self.assertIn("[TextureOverride_LOD0.aaa_1_0]", content)
        self.assertIn("drawindexed = 6,0,0", content)

    def test_target_ib_without_part_name_is_kept(self):
        """无部件名的 mesh 段，IB 在目标集合 -> 保守按目标保留原样。"""
        self._register_objects("LOD0.92cb56c9-64626-0.目标部件")
        self._write_ini(_ini_bindings_before_mesh("LOD0.92cb56c9-64626-0"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        self._write_texture("Textures/aaa-1-0-NormalMap.dds")
        before = self._read()

        self._run(["LOD0.92cb56c9-64626-0.目标部件"])

        self.assertEqual(before, self._read())
        self.assertTrue(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )

    def test_name_without_ib_key_stays_unknown(self):
        """提取不到 IB 键 -> unknown，绑定、定义段、贴图全部原样保留。"""
        self._register_objects("LOD0.92cb56c9-64626-0.目标部件")
        self._write_ini(_ini_bindings_before_mesh("LOD0.未知部件"))
        self._write_texture("Textures/aaa-1-0-DiffuseMap.dds")
        before = self._read()

        self._run(["LOD0.92cb56c9-64626-0.目标部件"])

        self.assertEqual(before, self._read())
        self.assertIn("[Resource-aaa-1-0-DiffuseMap]", self._read())
        self.assertTrue(
            os.path.exists(os.path.join(self.mod_root, "Textures", "aaa-1-0-DiffuseMap.dds"))
        )


if __name__ == "__main__":
    unittest.main()
