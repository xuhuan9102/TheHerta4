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


def _is_dds_file_stub(path):
    """本测试用假字节造贴图，按扩展名判定 DDS（生产侧按文件头判定）。"""
    return str(path or "").lower().endswith(".dds")


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
    convert_texture_to_dds_cached=lambda *_args, **_kwargs: (True, ""),
    is_dds_file=lambda path: _is_dds_file_stub(path),
)

# 细分进度上报桩：真实实现在 blueprint/export_progress.py（无 stage 文件时静默），
# 这里换成记录列表，便于断言「转换贴图 i/total」文本。
_PROGRESS_REPORTS = []


def _record_progress_stage(text=None, *_args, **_kwargs):
    _PROGRESS_REPORTS.append(text)


_install_module(f"{PKG}.blueprint.export_progress", report_stage=_record_progress_stage)


ROOT = Path(__file__).resolve().parents[1]
sys.modules[f"{PKG}.toolkit"].__path__ = [str(ROOT / "toolkit")]


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

        同时锁定导出侧接线：材质资源名与类型名必须传给转换入口，这样源图名不含
        类型关键字时也能按材质名得到正确规则。"""
        source = self._make_source("VivianGemADiffuse.png")
        node = self._make_node()
        calls = []

        def fake_convert(source_path, target_path, **kwargs):
            calls.append((source_path, target_path, kwargs))
            with open(target_path, "wb") as handle:
                handle.write(b"DDS payload")
            return True, ""

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds_cached", side_effect=fake_convert
        ):
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.dds", result)
        self.assertEqual(1, len(calls))
        source_path, target_path, kwargs = calls[0]
        self.assertEqual(source, source_path)
        self.assertEqual(os.path.join(self.target_dir, "Body_Material.dds"), target_path)
        self.assertEqual("Body_Material", kwargs.get("match_name"))
        self.assertEqual("Body", kwargs.get("texture_type"))
        self.assertEqual(("Body_Material", "Body_Material"), kwargs.get("name_hints"))
        self.assertEqual("", kwargs.get("workspace_source_path"))
        # 源 PNG 必须原样保留：编辑期还要继续用它
        self.assertTrue(os.path.exists(source))

    def test_material_name_is_used_for_format_resolution(self):
        """格式判定串是材质名（= 目标 .dds 名），不是源 PNG 名"""
        source = self._make_source("c209c22b-45087-0-NormalMap.png")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds_cached"
        ) as convert_mock:
            convert_mock.return_value = (True, "")
            node.copy_texture_file(
                _FakeImage(source), self.target_dir, _FakeMaterial("DiffuseMap_Body")
            )

        self.assertEqual("DiffuseMap_Body", convert_mock.call_args.kwargs["match_name"])

    def test_switch_off_copies_source_as_is(self):
        """开关关闭时保持改动前的行为：原样复制源文件，不调用转换"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = self._make_node(export_as_dds=False)

        with mock.patch.object(node_postprocess_material, "convert_texture_to_dds_cached") as convert_mock:
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

        with mock.patch.object(node_postprocess_material, "convert_texture_to_dds_cached") as convert_mock:
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.png", result)
        convert_mock.assert_not_called()

    def test_dds_source_is_copied_without_conversion(self):
        """源已经是 DDS 时不重复转码，直接复制"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.dds")
        node = self._make_node()

        with mock.patch.object(node_postprocess_material, "convert_texture_to_dds_cached") as convert_mock:
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
            node_postprocess_material, "convert_texture_to_dds_cached", return_value=(False, "texconv 崩了")
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
            node_postprocess_material, "convert_texture_to_dds_cached", return_value=(False, "texconv 崩了")
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
        """目标 DDS 已存在时由带缓存的入口决定复用，节点层不再自己判断存在性。

        真正的「目标比源新就直接复用、不跑 texconv」由
        ``test_dds_conversion.DDSConversionCacheTests`` 覆盖；这里只确认节点层
        确实把决定权交了出去，而不是又自己转一遍。
        """
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        os.makedirs(self.target_dir, exist_ok=True)
        target = os.path.join(self.target_dir, "Body_Material.dds")
        with open(target, "wb") as handle:
            handle.write(b"already converted")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds_cached"
        ) as convert_mock:
            convert_mock.return_value = (True, "")
            result = node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual("Body_Material.dds", result)
        self.assertFalse(convert_mock.call_args.kwargs["force"])

    def test_override_flag_passes_force_to_converter(self):
        """「覆盖现有资源」把 force 交给带缓存的入口：愿意重新生成，但同一目标
        在一次导出里仍只重生成一次 —— 一个材质被 N 个物体引用不会转 N 次。"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = self._make_node(override=True)

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds_cached"
        ) as convert_mock:
            convert_mock.return_value = (True, "")
            node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertEqual(1, convert_mock.call_count)
        self.assertTrue(convert_mock.call_args.kwargs["force"])

    def test_default_does_not_force_regeneration(self):
        """默认（不覆盖）时 force 必须为假，让入口按「目标比源新」复用磁盘产物"""
        source = self._make_source("c209c22b-45087-0-DiffuseMap.png")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_material, "convert_texture_to_dds_cached"
        ) as convert_mock:
            convert_mock.return_value = (True, "")
            node.copy_texture_file(_FakeImage(source), self.target_dir, _FakeMaterial())

        self.assertFalse(convert_mock.call_args.kwargs["force"])

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

        def fake_convert(source_path, target_path, props=None, force=False, **_kwargs):
            with open(target_path, "wb") as handle:
                handle.write(b"DDS payload")
            return True, ""

        with mock.patch.object(
            node_postprocess_psbinding, "convert_texture_to_dds_cached", side_effect=fake_convert
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

        with mock.patch.object(node_postprocess_psbinding, "convert_texture_to_dds_cached") as convert_mock:
            result = node._copy_texture_file(source, Path(self.output_dir))

        self.assertEqual("Textures/body-DiffuseMap.png", result)
        convert_mock.assert_not_called()

    def test_failure_falls_back_to_copying_source(self):
        source = self._make_source("body-DiffuseMap.png")
        node = self._make_node()

        with mock.patch.object(
            node_postprocess_psbinding, "convert_texture_to_dds_cached", return_value=(False, "boom")
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

        def fake_convert(source_path, target_path, props=None, force=False, **_kwargs):
            with open(target_path, "wb") as handle:
                handle.write(b"DDS payload")
            return True, ""

        with mock.patch.object(
            node_postprocess_diffuse_switch,
            "convert_texture_to_dds_cached",
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
            node_postprocess_diffuse_switch, "convert_texture_to_dds_cached"
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
            node_postprocess_object_texture, "convert_texture_to_dds_cached", converter
        ):
            return node_postprocess_object_texture._apply_object_texture_replacements(
                self._ini_text(), "LOD0.5eb66b57-26148-0", "", group, None, self.mod_dir
            )

    def test_png_slot_is_converted_to_dds(self):
        source = self._make_source("body-DiffuseMap.png")

        def fake_convert(source_path, target_path, props=None, force=False, **_kwargs):
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
            node_postprocess_object_texture, "convert_texture_to_dds_cached"
        ) as convert_mock:
            text, modified, _message = self._run(group)

        self.assertGreater(modified, 0)
        convert_mock.assert_not_called()
        self.assertIn("filename = Textures/body-DiffuseMap.png", text)

    def test_conversion_failure_keeps_source_name(self):
        source = self._make_source("body-DiffuseMap.png")

        with mock.patch.object(
            node_postprocess_object_texture, "convert_texture_to_dds_cached", return_value=(False, "boom")
        ):
            text, modified, _message = self._run(self._make_group(diffuse_path=source))

        self.assertGreater(modified, 0)
        self.assertIn("filename = Textures/body-DiffuseMap.png", text)


class TextureProgressReportTests(unittest.TestCase):
    """材质转资源pro 贴图转换的细分进度文本（后台导出状态栏 i/total）。"""

    _METHODS = ("_reset_texture_progress", "_plan_texture_progress", "_report_texture_progress")

    def setUp(self):
        _PROGRESS_REPORTS.clear()
        base = node_postprocess_material.SSMTNode_PostProcess_MaterialBase
        self.node = types.SimpleNamespace()
        for name in self._METHODS:
            setattr(self.node, name, types.MethodType(getattr(base, name), self.node))

    def test_each_texture_reports_increment(self):
        self.node._reset_texture_progress()
        self.node._plan_texture_progress(2)

        self.node._report_texture_progress()
        self.node._report_texture_progress()

        self.assertEqual(
            _PROGRESS_REPORTS,
            ["材质转资源pro：转换贴图 1/2", "材质转资源pro：转换贴图 2/2"],
        )

    def test_total_never_smaller_than_done(self):
        """没有批次预算（直接调 create_resource_entry）时也不能出现 i>total。"""
        self.node._reset_texture_progress()

        self.node._report_texture_progress()

        self.assertEqual(_PROGRESS_REPORTS, ["材质转资源pro：转换贴图 1/1"])

    def test_reset_restarts_counter_for_next_export(self):
        self.node._reset_texture_progress()
        self.node._plan_texture_progress(1)
        self.node._report_texture_progress()

        self.node._reset_texture_progress()
        self.node._plan_texture_progress(1)
        self.node._report_texture_progress()

        self.assertEqual(
            _PROGRESS_REPORTS,
            ["材质转资源pro：转换贴图 1/1", "材质转资源pro：转换贴图 1/1"],
        )


if __name__ == "__main__":
    unittest.main()
