# -*- coding: utf-8 -*-
"""「材质转资源pro」节点的导出贴图 DDS 化：编辑期用无损 PNG，导出时转 DDS。

编辑期贴图保持 PNG 是为了避免反复保存 DDS 时被过度压缩（暗色马赛克/色斑），但游戏
只认 DDS。这里覆盖 :meth:`copy_texture_file` 的决策：开关开 → 按现有规则转 DDS 并让
INI 的 ``filename`` 指向 ``.dds``；开关关或转换失败 → 退回原样复制，导出不中断。
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


PKG = "_export_texture_dds_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, Node=object),
    props=types.SimpleNamespace(
        BoolProperty=lambda **_kwargs: None,
        StringProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        FloatProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects={}),
    path=types.SimpleNamespace(abspath=lambda value: value),
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(texture_tools_props=types.SimpleNamespace()),
    ),
    utils=types.SimpleNamespace(
        register_class=lambda _cls: None, unregister_class=lambda _cls: None
    ),
)
_install_module("bpy", **_fake_bpy.__dict__)

_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "AUTO_APPENDED_SECTION_MARKERS": (),
            "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
            "split_anim_driver_block_content": classmethod(lambda cls, content: ("", content)),
        },
    ),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="ZZMI"),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(EFMI="EFMI", NTEMI="NTEMI", ZZMI="ZZMI", HIMI="HIMI"),
)
_install_module(
    f"{PKG}.toolkit.tt_dds_conversion",
    convert_texture_to_dds=lambda *_args, **_kwargs: (True, ""),
    # 期望格式 / 已有 DDS 头校验：本文件的用例把它们当可替换的决策点，
    # 真实文件头解析在 test_dds_conversion.py 里用最小 DDS 头单独覆盖。
    resolve_texture_dds_format=lambda *_args, **_kwargs: "bc7_unorm",
    existing_dds_matches_format=lambda *_args, **_kwargs: True,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_blueprint_module(module_name):
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.blueprint.{module_name}", ROOT / "blueprint" / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# FX 命名空间档案要先于材质转资源加载（后者从档案取命名空间前缀）。
_load_blueprint_module("fx_namespace")
node_postprocess_material = _load_blueprint_module("node_postprocess_material")
# PS绑定与贴图切换各自独立加载（都只依赖上面的桩）
node_postprocess_psbinding = _load_blueprint_module("node_postprocess_psbinding")
node_postprocess_diffuse_switch = _load_blueprint_module("node_postprocess_diffuse_switch")
node_postprocess_object_texture = _load_blueprint_module("node_postprocess_object_texture")


class _FakeImage:
    def __init__(self, filepath):
        self.filepath = filepath


class _FakeMaterial:
    def __init__(self, name="Body_Material"):
        self.name = name


class ExportTextureAsDDSTests(unittest.TestCase):
    """导出时把非 DDS 贴图转成 DDS（开关在「材质转资源pro」节点上）"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="export_texture_dds_")
        self.source_dir = os.path.join(self.temp_dir, "workspace")
        self.target_dir = os.path.join(self.temp_dir, "Mod", "Textures")
        os.makedirs(self.source_dir, exist_ok=True)

    def _make_source(self, name):
        path = os.path.join(self.source_dir, name)
        with open(path, "wb") as handle:
            handle.write(b"fake image payload")
        return path

    def _make_node(self, export_as_dds=True, override=False):
        """借用真实的 copy_texture_file，属性模拟 pro 节点上的开关。"""
        node = types.SimpleNamespace()
        node.export_textures_as_dds = export_as_dds
        node.material_to_resource_override = override
        base = node_postprocess_material.SSMTNode_PostProcess_MaterialBase
        node.copy_texture_file = types.MethodType(base.copy_texture_file, node)
        return node

    def test_png_source_is_converted_to_dds(self):
        """核心：开关开启时 PNG 源转成 DDS 写进 Mod，返回 .dds 文件名。

        同时锁定导出侧接线：hints 里必须带材质名与资源 stem，
        这样源图名不含 DiffuseMap 关键字时也能按材质名得到 sRGB 规则。"""
        source = self._make_source("VivianGemADiffuse.png")
        node = self._make_node()
        calls = []

        def fake_convert(source_path, target_path, props=None, texture_type="", name_hints=(), format_override=""):
            calls.append((source_path, target_path, tuple(name_hints), format_override))
            with open(target_path, "wb") as handle:
                handle.write(b"DDS payload")
            return True, ""

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds", side_effect=fake_convert
        ):
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.dds", result)
        self.assertEqual(
            [
                (
                    source,
                    os.path.join(self.target_dir, "Body_Material.dds"),
                    ("Body_Material", "Body_Material"),
                    "",
                )
            ],
            calls,
        )
        # 源 PNG 必须原样保留：编辑期还要继续用它
        self.assertTrue(os.path.exists(source))

    def test_switch_off_copies_source_as_is(self):
        """开关关闭时保持改动前的行为：原样复制源文件，不调用转换"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = self._make_node(export_as_dds=False)

        with mock.patch.object(node_postprocess_material, "convert_texture_to_dds") as convert_mock:
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.png", result)
        convert_mock.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.target_dir, "Body_Material.png")))

    def test_legacy_node_without_switch_keeps_old_behavior(self):
        """旧版弃用壳没有该属性，getattr 兜底为 False，行为不得改变"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = types.SimpleNamespace()
        node.material_to_resource_override = False
        base = node_postprocess_material.SSMTNode_PostProcess_MaterialBase
        node.copy_texture_file = types.MethodType(base.copy_texture_file, node)

        with mock.patch.object(node_postprocess_material, "convert_texture_to_dds") as convert_mock:
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.png", result)
        convert_mock.assert_not_called()

    def test_dds_source_is_copied_without_conversion(self):
        """源已经是 DDS 时不重复转码，直接复制"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.dds")
        node = self._make_node()

        with mock.patch.object(node_postprocess_material, "convert_texture_to_dds") as convert_mock:
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.dds", result)
        convert_mock.assert_not_called()
        with open(os.path.join(self.target_dir, "Body_Material.dds"), "rb") as handle:
            self.assertEqual(b"fake image payload", handle.read())

    def test_conversion_failure_falls_back_to_copying_png(self):
        """转换失败不能中断导出，也不能留下指向不存在 DDS 的 INI 行"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds", return_value=(False, "texconv 崩了")
        ):
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.png", result)
        self.assertTrue(os.path.exists(os.path.join(self.target_dir, "Body_Material.png")))

    def test_stale_forced_filename_dds_is_not_reused_after_failure(self):
        """转换失败时不得沿用缓存里的 .dds 名：此刻没有对应文件，照抄会把 PNG
        字节写成 .dds，INI 指过去就是一张读不出来的贴图。"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds", return_value=(False, "texconv 崩了")
        ):
            result = node.copy_texture_file(
                _FakeImage(source),
                self.target_dir,
                _FakeMaterial(),
                forced_filename="Body_Material.dds",
            )

        self.assertEqual("Body_Material.png", result)
        self.assertTrue(os.path.exists(os.path.join(self.target_dir, "Body_Material.png")))
        self.assertFalse(os.path.exists(os.path.join(self.target_dir, "Body_Material.dds")))

    def test_matching_forced_filename_is_still_reused(self):
        """缓存名扩展名与源一致时照旧复用（同一张贴图只写一份文件）"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = self._make_node(export_as_dds=False)

        result = node.copy_texture_file(
            _FakeImage(source), self.target_dir, _FakeMaterial(), forced_filename="Body_Material.png"
        )

        self.assertEqual("Body_Material.png", result)

    def test_existing_target_dds_is_reused(self):
        """目标 DDS 已存在且格式与期望一致 → 复用，不重复跑 texconv（重复导出要快）"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        os.makedirs(self.target_dir, exist_ok=True)
        target = os.path.join(self.target_dir, "Body_Material.dds")
        with open(target, "wb") as handle:
            handle.write(b"already converted")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_material, "existing_dds_matches_format", return_value=True
        ) as match_mock, mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds"
        ) as convert_mock:
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.dds", result)
        convert_mock.assert_not_called()
        match_mock.assert_called_once_with(target, "bc7_unorm")

    def test_existing_dds_with_wrong_format_is_reconverted(self):
        """旧线性 DDS 与当前规则期望的格式不一致 → 必须重转覆盖，不能「存在即跳过」"""
        source = self._make_source("VivianGemADiffuse.png")
        os.makedirs(self.target_dir, exist_ok=True)
        target = os.path.join(self.target_dir, "Body_Material.dds")
        with open(target, "wb") as handle:
            handle.write(b"stale linear dds")
        node = self._make_node()
        converted = []

        def fake_convert(source_path, target_path, props=None, texture_type="", name_hints=(), format_override=""):
            converted.append((source_path, target_path, tuple(name_hints)))
            with open(target_path, "wb") as handle:
                handle.write(b"fresh srgb dds")
            return True, ""

        with mock.patch.object(
            node_postprocess_material,
            "resolve_texture_dds_format",
            return_value="bc7_unorm_srgb",
        ) as resolve_mock, mock.patch.object(
            node_postprocess_material, "existing_dds_matches_format", return_value=False
        ) as match_mock, mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds", side_effect=fake_convert
        ) as convert_mock:
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.dds", result)
        # 期望格式由 resolve 得到，再拿它去校验已有文件头
        match_mock.assert_called_once_with(target, "bc7_unorm_srgb")
        self.assertEqual("bc7_unorm_srgb", resolve_mock.return_value)
        convert_mock.assert_called_once()
        # 重转也必须带上 hints（材质名 + stem），否则又会被源图名带偏
        self.assertEqual(
            [(source, target, ("Body_Material", "Body_Material"))], converted
        )
        with open(target, "rb") as handle:
            self.assertEqual(b"fresh srgb dds", handle.read())

    def test_material_prefix_is_passed_as_texture_type(self):
        """材质名前缀（DiffuseMap）必须作为 texture_type 传给格式解析：
        源图名 VivianGemADiffuse.png 不含 DiffuseMap 关键字，也不能靠文件名决定格式。"""
        source = self._make_source("VivianGemADiffuse.png")
        os.makedirs(self.target_dir, exist_ok=True)
        target = os.path.join(self.target_dir, "DiffuseMap_VivianGemADiffuse.dds")
        with open(target, "wb") as handle:
            handle.write(b"stale linear dds")
        node = self._make_node()
        calls = []

        def fake_convert(
            source_path, target_path, props=None, texture_type="", name_hints=(), format_override=""
        ):
            calls.append((texture_type, tuple(name_hints)))
            with open(target_path, "wb") as handle:
                handle.write(b"fresh srgb dds")
            return True, ""

        with mock.patch.object(
            node_postprocess_material,
            "resolve_texture_dds_format",
            return_value="bc7_unorm_srgb",
        ) as resolve_mock, mock.patch.object(
            node_postprocess_material, "existing_dds_matches_format", return_value=False
        ), mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds", side_effect=fake_convert
        ):
            result = node.copy_texture_file(
                _FakeImage(source),
                self.target_dir,
                _FakeMaterial("DiffuseMap_VivianGemADiffuse"),
            )

        self.assertEqual("DiffuseMap_VivianGemADiffuse.dds", result)
        self.assertEqual(
            [("DiffuseMap", ("DiffuseMap_VivianGemADiffuse", "DiffuseMap_VivianGemADiffuse"))],
            calls,
        )
        self.assertEqual(
            "DiffuseMap", resolve_mock.call_args.kwargs.get("texture_type")
        )
        with open(target, "rb") as handle:
            self.assertEqual(b"fresh srgb dds", handle.read())

    def test_existing_dds_is_still_reconverted_when_override_requested(self):
        """保留 material_to_resource_override 既有语义：覆盖模式下永远重转"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        os.makedirs(self.target_dir, exist_ok=True)
        target = os.path.join(self.target_dir, "Body_Material.dds")
        with open(target, "wb") as handle:
            handle.write(b"already converted")
        node = self._make_node(override=True)

        with mock.patch.object(
            node_postprocess_material, "existing_dds_matches_format", return_value=True
        ), mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds", return_value=(True, "")
        ) as convert_mock:
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.dds", result)
        convert_mock.assert_called_once()

    def test_missing_source_returns_none(self):
        """源文件不存在时返回 None，不产生 INI 引用"""
        node = self._make_node()
        missing = os.path.join(self.source_dir, "not_there.png")
        self.assertIsNone(
            node.copy_texture_file(_FakeImage(missing), self.target_dir, _FakeMaterial())
        )


class PSBindingExportAsDDSTests(unittest.TestCase):
    """PS绑定节点：绑定的非 DDS 贴图在导出时转成 DDS"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="psbinding_dds_")
        self.source_dir = os.path.join(self.temp_dir, "src")
        self.output_dir = os.path.join(self.temp_dir, "Mod")
        os.makedirs(self.source_dir, exist_ok=True)

    def _make_source(self, name):
        path = os.path.join(self.source_dir, name)
        with open(path, "wb") as handle:
            handle.write(b"image payload")
        return path

    def _make_node(self, export_as_dds=True):
        node = types.SimpleNamespace()
        node.export_textures_as_dds = export_as_dds
        cls = node_postprocess_psbinding.SSMTNode_PostProcess_PSBinding
        node._copy_texture_file = types.MethodType(cls._copy_texture_file, node)
        return node

    def test_png_is_converted_to_dds(self):
        source = self._make_source("body-DiffuseMap.png")
        node = self._make_node()

        def fake_convert(source_path, target_path, props=None, **_kwargs):
            with open(target_path, "wb") as handle:
                handle.write(b"DDS payload")
            return True, ""

        with mock.patch.object(
            node_postprocess_psbinding, "convert_texture_to_dds", side_effect=fake_convert
        ):
            result = node._copy_texture_file(source, Path(self.output_dir))

        self.assertEqual("Textures/body-DiffuseMap.dds", result)
        self.assertTrue(
            os.path.exists(os.path.join(self.output_dir, "Textures", "body-DiffuseMap.dds"))
        )
        self.assertTrue(os.path.exists(source))

    def test_switch_off_copies_png(self):
        source = self._make_source("body-DiffuseMap.png")
        node = self._make_node(export_as_dds=False)

        with mock.patch.object(node_postprocess_psbinding, "convert_texture_to_dds") as convert_mock:
            result = node._copy_texture_file(source, Path(self.output_dir))

        self.assertEqual("Textures/body-DiffuseMap.png", result)
        convert_mock.assert_not_called()

    def test_failure_falls_back_to_copying_source(self):
        source = self._make_source("body-DiffuseMap.png")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_psbinding, "convert_texture_to_dds", return_value=(False, "boom")
        ):
            result = node._copy_texture_file(source, Path(self.output_dir))

        self.assertEqual("Textures/body-DiffuseMap.png", result)


class DiffuseSwitchExportAsDDSTests(unittest.TestCase):
    """贴图切换节点：切换贴图在生成 INI 时转成 DDS"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="diffuse_switch_dds_")
        self.ini_path = os.path.join(self.temp_dir, "mod.ini")
        with open(self.ini_path, "w", encoding="utf-8") as handle:
            handle.write("[Constants]\n")

    def _make_source(self, name):
        path = os.path.join(self.temp_dir, name)
        with open(path, "wb") as handle:
            handle.write(b"image payload")
        return path

    def test_png_is_converted_to_dds(self):
        source = self._make_source("alt-DiffuseMap.png")

        def fake_convert(source_path, target_path, props=None, **_kwargs):
            with open(target_path, "wb") as handle:
                handle.write(b"DDS payload")
            return True, ""

        with mock.patch.object(
            node_postprocess_diffuse_switch,
            "convert_texture_to_dds",
            side_effect=fake_convert,
        ):
            result = node_postprocess_diffuse_switch.copy_texture(
                source, self.ini_path, export_as_dds=True
            )

        self.assertEqual("Textures/alt-DiffuseMap.dds", result)
        self.assertTrue(os.path.exists(source))

    def test_disabled_keeps_png_and_uses_old_signature(self):
        """默认参数保持向后兼容：不传开关时行为与改动前一致"""
        source = self._make_source("alt-DiffuseMap.png")

        with mock.patch.object(
            node_postprocess_diffuse_switch, "convert_texture_to_dds"
        ) as convert_mock:
            result = node_postprocess_diffuse_switch.copy_texture(source, self.ini_path)

        self.assertEqual("Textures/alt-DiffuseMap.png", result)
        convert_mock.assert_not_called()

    def test_missing_texture_raises(self):
        with self.assertRaises(FileNotFoundError):
            node_postprocess_diffuse_switch.copy_texture(
                os.path.join(self.temp_dir, "nope.png"), self.ini_path, export_as_dds=True
            )


class ObjectTextureExportAsDDSTests(unittest.TestCase):
    """物体贴图替换节点：分组里填的非 DDS 贴图在导出时转成 DDS"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="object_texture_dds_")
        self.source_dir = os.path.join(self.temp_dir, "src")
        self.mod_dir = os.path.join(self.temp_dir, "Mod")
        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.mod_dir, exist_ok=True)

    def _make_source(self, name):
        path = os.path.join(self.source_dir, name)
        with open(path, "wb") as handle:
            handle.write(b"image payload")
        return path

    @staticmethod
    def _ini_text():
        return (
            "[TextureOverride_LOD0.5eb66b57-26148-0]\n"
            "; [mesh:LOD0.5eb66b57-26148-0]\n"
            "hash = aaaa\n"
            "Resource\\ZZMI\\Diffuse = ref Resource-LOD0.5eb66b57-26148-0-DiffuseMap\n"
            "\n"
            "[Resource-LOD0.5eb66b57-26148-0-DiffuseMap]\n"
            "filename = Textures/LOD0.5eb66b57-26148-0-DiffuseMap.dds\n"
        )

    @staticmethod
    def _make_group(**kwargs):
        group = types.SimpleNamespace(
            diffuse_path="",
            normal_path="",
            light_path="",
            material_path="",
            export_as_dds=True,
        )
        for key, value in kwargs.items():
            setattr(group, key, value)
        return group

    def _run(self, group, converter=None):
        """converter 为 None 时不替换转换函数，交由调用方自己的 patch 生效。"""
        if converter is None:
            return node_postprocess_object_texture._apply_object_texture_replacements(
                self._ini_text(), "LOD0.5eb66b57-26148-0", "", group, None, self.mod_dir
            )
        with mock.patch.object(
            node_postprocess_object_texture, "convert_texture_to_dds", converter
        ):
            return node_postprocess_object_texture._apply_object_texture_replacements(
                self._ini_text(), "LOD0.5eb66b57-26148-0", "", group, None, self.mod_dir
            )

    def test_png_slot_is_converted_to_dds(self):
        source = self._make_source("body-DiffuseMap.png")

        def fake_convert(source_path, target_path, props=None, **_kwargs):
            with open(target_path, "wb") as handle:
                handle.write(b"DDS payload")
            return True, ""

        text, modified, message = self._run(self._make_group(diffuse_path=source), fake_convert)

        self.assertEqual("", message)
        self.assertGreater(modified, 0)
        self.assertIn("filename = Textures/body-DiffuseMap.dds", text)
        self.assertTrue(os.path.exists(os.path.join(self.mod_dir, "Textures", "body-DiffuseMap.dds")))
        # 编辑期的源 PNG 保留
        self.assertTrue(os.path.exists(source))

    def test_switch_off_copies_png_through(self):
        source = self._make_source("body-DiffuseMap.png")
        group = self._make_group(diffuse_path=source, export_as_dds=False)

        with mock.patch.object(
            node_postprocess_object_texture, "convert_texture_to_dds"
        ) as convert_mock:
            text, modified, _message = self._run(group)

        self.assertGreater(modified, 0)
        convert_mock.assert_not_called()
        self.assertIn("filename = Textures/body-DiffuseMap.png", text)

    def test_conversion_failure_keeps_source_name(self):
        source = self._make_source("body-DiffuseMap.png")

        with mock.patch.object(
            node_postprocess_object_texture, "convert_texture_to_dds", return_value=(False, "boom")
        ):
            text, modified, _message = self._run(self._make_group(diffuse_path=source))

        self.assertGreater(modified, 0)
        self.assertIn("filename = Textures/body-DiffuseMap.png", text)


if __name__ == "__main__":
    unittest.main()
