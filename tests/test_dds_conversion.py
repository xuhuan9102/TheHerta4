import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_dds_conversion_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object, Panel=object),
    props=types.SimpleNamespace(
        BoolProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
        FloatProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        StringProperty=lambda **_kwargs: None,
    ),
    context=types.SimpleNamespace(scene=types.SimpleNamespace(texture_tools_props=types.SimpleNamespace())),
    data=types.SimpleNamespace(images=[]),
    # 真实 Blender 里 "//" 指工程目录；重连用例只用绝对路径，这里原样返回即可，
    # 但必须让 "//" 不落进输出目录前缀，否则源文件会被当成工程内文件跳过。
    path=types.SimpleNamespace(abspath=lambda value: "" if value == "//" else value),
)
_install_module("bpy", **_fake_bpy.__dict__)

module_path = Path(__file__).resolve().parents[1] / "toolkit" / "tt_dds_conversion.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.tt_dds_conversion", module_path)
dds_conversion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dds_conversion
spec.loader.exec_module(dds_conversion)


class DDSConversionTests(unittest.TestCase):
    """测试 DDS 转换工具的格式解析与值保留转换策略"""

    def test_flags_format_aware(self):
        """色彩空间标志随输出格式走：sRGB 输出用 --srgb-in（PNG 这类无色彩空间信息的
        输入先按 sRGB 解码、再由 sRGB 输出编码回去 = 恒等），线性输出用 --ignore-srgb。

        注意边界：--ignore-srgb 只管 WIC 输入/输出，管不了 DDS 自身的 _SRGB 格式语义
        —— 把已经是 _SRGB 的 DDS 按非 sRGB 规则重编码会净压暗一次（2026-09-27 实测）。"""
        self.assertEqual(["--srgb-in"], dds_conversion._texconv_colorspace_flags("bc7_unorm_srgb"))
        self.assertEqual(["--srgb-in"], dds_conversion._texconv_colorspace_flags("B8G8R8A8_UNORM_SRGB"))
        self.assertEqual(["--ignore-srgb"], dds_conversion._texconv_colorspace_flags("bc7_unorm"))
        self.assertEqual(["--ignore-srgb"], dds_conversion._texconv_colorspace_flags("r8g8b8a8_unorm"))
        # 大小写不敏感
        self.assertEqual(["--srgb-in"], dds_conversion._texconv_colorspace_flags("BC7_UNORM_SRGB"))

    def test_custom_rule_format_is_authoritative(self):
        """自定义规则命中：格式完全由规则决定，不再按文件名推断类型"""
        props = types.SimpleNamespace(
            dds_use_custom_rules=True,
            dds_rules=[
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
                    format="r8g8b8a8_unorm",
                )
            ],
        )
        texture_type, dds_format, matched_by = dds_conversion.resolve_dds_target("DiffuseMap_Body.png", props)
        self.assertEqual(texture_type, "custom")
        self.assertEqual(dds_format, "r8g8b8a8_unorm")
        self.assertEqual(matched_by, r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)")

    def test_custom_rule_ignores_filename_keywords(self):
        """文件名含多个已知类型关键词时，不影响自定义规则的判定结果"""
        props = types.SimpleNamespace(
            dds_use_custom_rules=True,
            dds_rules=[
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])DiffuseMap_high(?:[_\-. ]|$)",
                    format="bc7_unorm_srgb",
                ),
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
                    format="bc7_unorm_srgb",
                ),
                types.SimpleNamespace(
                    enabled=True,
                    pattern=r"(?i)(?:^|[_\-. ])NormalMap(?:[_\-. ]|$)",
                    format="r8g8b8a8_unorm",
                ),
            ],
        )
        texture_type, dds_format, matched_by = dds_conversion.resolve_dds_target(
            "NormalMap_DiffuseMap_high.png", props
        )
        self.assertEqual(texture_type, "custom")
        self.assertEqual(dds_format, "bc7_unorm_srgb")
        self.assertEqual(matched_by, r"(?i)(?:^|[_\-. ])DiffuseMap_high(?:[_\-. ]|$)")

    def test_default_rules_pick_first_matching_keyword(self):
        """测试文件名同时包含 NormalMap 和 DiffuseMap 时，取最先出现的匹配规则"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target(
            "NormalMap_DiffuseMap_high_丝袜.png", props
        )
        self.assertEqual(texture_type, "NormalMap")
        self.assertEqual(dds_format, "r8g8b8a8_unorm")

    def test_default_rules_single_keyword_normalmap(self):
        """测试仅包含 NormalMap 时正常匹配"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target("cloth_NormalMap.png", props)
        self.assertEqual(texture_type, "NormalMap")
        self.assertEqual(dds_format, "r8g8b8a8_unorm")

    def test_default_rules_recognize_ttlmap(self):
        """测试 TTLMap 前缀与 FXMap 一样按遮罩格式识别（bc7_unorm）"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target("TTLMap_BaseTex.png", props)
        self.assertEqual(texture_type, "TTLMap")
        self.assertEqual(dds_format, "bc7_unorm")
        self.assertIn("--ignore-srgb", dds_conversion._texconv_colorspace_flags("bc7_unorm"))

    def test_unmatched_falls_back_to_default_format(self):
        """未命中任何规则时走默认 bc7_unorm"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, matched_by = dds_conversion.resolve_dds_target("UnknownMask.png", props)
        self.assertEqual(texture_type, "default")
        self.assertEqual(dds_format, "bc7_unorm")
        self.assertEqual(matched_by, "Default")

    def test_default_rules_materialmap_is_srgb(self):
        """MaterialMap 规则必须是 bc7_unorm_srgb（用户 2026-10-20 明确要求）"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target(
            "MaterialMap_VivianGemAMaterialMap.png", props
        )
        self.assertEqual(texture_type, "MaterialMap")
        self.assertEqual(dds_format, "bc7_unorm_srgb")

    def test_vivian_diffuse_without_hint_keeps_legacy_default(self):
        """源图名只有 Diffuse、没有 DiffuseMap：不传 hint 时维持原行为（默认线性）"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, matched_by = dds_conversion.resolve_dds_target(
            "VivianGemADiffuse.png", props
        )
        self.assertEqual(texture_type, "default")
        self.assertEqual(dds_format, "bc7_unorm")
        self.assertEqual(matched_by, "Default")

    def test_format_for_texture_type_by_material_prefix(self):
        """材质类型名（材质名前缀）直接决定格式；带后缀时只看首个 token、大小写不敏感。"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        self.assertEqual("bc7_unorm_srgb", dds_conversion.format_for_texture_type("DiffuseMap", props))
        self.assertEqual("bc7_unorm_srgb", dds_conversion.format_for_texture_type("MaterialMap", props))
        self.assertEqual("r8g8b8a8_unorm", dds_conversion.format_for_texture_type("NormalMap", props))
        self.assertEqual("bc7_unorm", dds_conversion.format_for_texture_type("LightMap", props))
        self.assertEqual("", dds_conversion.format_for_texture_type("UnknownMask", props))
        self.assertEqual(
            "bc7_unorm_srgb",
            dds_conversion.format_for_texture_type("DiffuseMap_VivianGemADiffuse", props),
        )
        self.assertEqual(
            "bc7_unorm_srgb", dds_conversion.format_for_texture_type("diffusemap_xxx", props)
        )

    def test_texture_type_decides_format_not_source_filename(self):
        """鞋子端到端：VivianGemADiffuse.png 不含 DiffuseMap 关键字，但材质类型
        DiffuseMap 必须让输出走 bc7_unorm_srgb（DXGI 99），不能再按源文件名兜回线性。"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        self.assertEqual(
            "bc7_unorm_srgb",
            dds_conversion.resolve_texture_dds_format(
                "C:/mod/Textures/VivianGemADiffuse.png",
                props,
                texture_type="DiffuseMap",
            ),
        )
        self.assertEqual(
            "bc7_unorm_srgb",
            dds_conversion.resolve_texture_dds_format(
                "C:/mod/Textures/VivianGemAMaterialMap.png",
                props,
                texture_type="MaterialMap",
            ),
        )

    def test_vivian_diffuse_uses_name_hint(self):
        """源文件名没命中时，用 name_hints（材质名 / 槽位标签）重试得到 sRGB 格式"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target(
            "VivianGemADiffuse.png", props, name_hints=["DiffuseMap_VivianGemADiffuse"]
        )
        self.assertEqual(texture_type, "DiffuseMap")
        self.assertEqual(dds_format, "bc7_unorm_srgb")

    def test_name_hints_try_in_order_and_stop_at_first_hit(self):
        """hints 按顺序逐个重试，第一个命中即返回；前面的无效候选被跳过"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        texture_type, dds_format, _matched_by = dds_conversion.resolve_dds_target(
            "3.png", props, name_hints=["", "not_a_map", "NormalMap_Body", "DiffuseMap_Body"]
        )
        self.assertEqual(texture_type, "NormalMap")
        self.assertEqual(dds_format, "r8g8b8a8_unorm")

    def test_resolve_texture_dds_format_override_wins(self):
        """显式 format_override 非空时直接返回它，不再看文件名 / hints"""
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        self.assertEqual(
            "bc3_unorm_srgb",
            dds_conversion.resolve_texture_dds_format(
                "VivianGemADiffuse.png", props, name_hints=["DiffuseMap"], format_override="bc3_unorm_srgb"
            ),
        )

    def test_resolve_texture_dds_format_uses_hint_and_defaults(self):
        props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])
        self.assertEqual(
            "bc7_unorm_srgb",
            dds_conversion.resolve_texture_dds_format(
                "VivianGemADiffuse.png", props, name_hints=["DiffuseMap_VivianGemADiffuse"]
            ),
        )
        self.assertEqual(
            "bc7_unorm",
            dds_conversion.resolve_texture_dds_format("VivianGemADiffuse.png", props),
        )


def _dds_header(fourcc=b"DX10", dxgi_format=None, magic=b"DDS "):
    """最小 DDS 头：148 字节（DX10）/ 128 字节（legacy 也可补齐到 148）。"""
    header = bytearray(148)
    header[0:4] = magic
    struct.pack_into("<I", header, 4, 124)
    header[84:88] = fourcc
    if dxgi_format is not None:
        struct.pack_into("<I", header, 128, dxgi_format)
    return bytes(header)


class DDSHeaderFormatTests(unittest.TestCase):
    """已有 DDS 的格式校验：只读文件头，决定「存在即跳过」能否成立。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dds_header_")
        self.addCleanup(shutil.rmtree, self.temp_dir, True)

    def _write(self, name, payload):
        path = os.path.join(self.temp_dir, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def test_dxgi_format_read_from_dx10_header(self):
        path = self._write("srgb.dds", _dds_header(b"DX10", 99))
        self.assertEqual(99, dds_conversion.dds_header_dxgi_format(path))

    def test_legacy_fourcc_has_no_dxgi_format(self):
        path = self._write("legacy.dds", _dds_header(b"DXT5", None))
        self.assertIsNone(dds_conversion.dds_header_dxgi_format(path))

    def test_existing_dds_matches_srgb_vs_linear(self):
        """DXGI 98 ≠ bc7_unorm_srgb，DXGI 99 = bc7_unorm_srgb"""
        path = self._write("linear.dds", _dds_header(b"DX10", 98))
        self.assertTrue(dds_conversion.existing_dds_matches_format(path, "bc7_unorm"))
        self.assertFalse(dds_conversion.existing_dds_matches_format(path, "bc7_unorm_srgb"))

        path = self._write("srgb.dds", _dds_header(b"DX10", 99))
        self.assertTrue(dds_conversion.existing_dds_matches_format(path, "BC7_UNORM_SRGB"))
        self.assertFalse(dds_conversion.existing_dds_matches_format(path, "bc7_unorm"))

    def test_legacy_fourcc_never_matches_srgb_target(self):
        """legacy FOURCC 头没有 sRGB 变体 → 任何 _srgb 目标都必须重转"""
        path = self._write("legacy_bc3.dds", _dds_header(b"DXT5", None))
        self.assertFalse(dds_conversion.existing_dds_matches_format(path, "bc3_unorm_srgb"))
        self.assertTrue(dds_conversion.existing_dds_matches_format(path, "bc3_unorm"))

    def test_unknown_or_broken_files_force_reconversion(self):
        """文件不存在 / 短头 / magic 不对 / 未知格式 都返回 False（重转安全默认）"""
        missing = os.path.join(self.temp_dir, "missing.dds")
        self.assertFalse(dds_conversion.existing_dds_matches_format(missing, "bc7_unorm"))

        short = self._write("short.dds", b"DDS " + b"\x00" * 20)
        self.assertFalse(dds_conversion.existing_dds_matches_format(short, "bc7_unorm"))

        bad_magic = self._write("bad.dds", _dds_header(b"DX10", 99, magic=b"NOPE"))
        self.assertFalse(dds_conversion.existing_dds_matches_format(bad_magic, "bc7_unorm_srgb"))

        unknown = self._write("unknown.dds", _dds_header(b"ZZZZ", None))
        self.assertFalse(dds_conversion.existing_dds_matches_format(unknown, "bc1_unorm"))


class _FakeColorspaceSettings:
    """image.colorspace_settings 替身：可分别让读/写抛异常，用于覆盖兜底路径。"""

    def __init__(self, name="sRGB", fail_on_get=False, fail_on_set=False):
        self._name = name
        self.fail_on_get = fail_on_get
        self.fail_on_set = fail_on_set
        self.write_count = 0

    @property
    def name(self):
        if self.fail_on_get:
            raise RuntimeError("colorspace 读取失败")
        return self._name

    @name.setter
    def name(self, value):
        self.write_count += 1
        if self.fail_on_set:
            raise RuntimeError("colorspace 写入失败")
        self._name = value

    def peek(self):
        """绕过可能抛异常的读取口，直接看落到的值。"""
        return self._name


class _FakeImage:
    """bpy.data.images 元素替身：filepath_raw 跟随 filepath（与真实行为一致）。"""

    def __init__(self, name, filepath, colorspace="sRGB", alpha_mode="NONE", reload_error=None):
        self.name = name
        self.source = "FILE"
        self.filepath = filepath
        self.colorspace_settings = _FakeColorspaceSettings(colorspace)
        self.alpha_mode = alpha_mode
        self.reload_error = reload_error
        self.reload_count = 0

    @property
    def filepath_raw(self):
        return self.filepath

    def reload(self):
        self.reload_count += 1
        if self.reload_error:
            raise self.reload_error


class _FakeTexNode:
    """引用贴图的 ShaderNodeTexImage 替身（刻意用非默认档位，锁定不被重连改写）。"""

    def __init__(self, image, interpolation="Closest", extension="MIRROR", projection="FLAT"):
        self.type = "TEX_IMAGE"
        self.image = image
        self.interpolation = interpolation
        self.extension = extension
        self.projection = projection


class DDSRelinkPreservesSettingsTests(unittest.TestCase):
    """重连契约：转换只换容器格式与文件路径，不改动转换前已存在的 Blender 侧配置。

    用户 2026-09-27 明确要求「重连时沿用原本的纹理节点配置」，本条推翻了 b7b1992 的
    「Blender 显示色彩空间按输出格式的 _srgb 后缀设置」：不再按目标 DDS 格式硬设色彩
    空间，而是在重连前后做快照/恢复。

    注：改动前本文件没有任何用例断言那条强制覆盖（这正是它能无声存在的原因），所以
    这里没有「旧断言」需要改写，全部是新增的重连行为锁定。
    """

    def setUp(self):
        self.output_dir = tempfile.mkdtemp(prefix="dds_relink_")
        self.addCleanup(shutil.rmtree, self.output_dir, True)
        self.addCleanup(setattr, _fake_bpy.data, "images", [])

    def _write_sources(self, *filenames):
        """在输出目录里造出待转换的源贴图，返回它们的绝对路径。"""
        paths = []
        for filename in filenames:
            target = Path(self.output_dir) / filename
            target.write_bytes(b"fake-texture-bytes")
            paths.append(os.path.normpath(str(target)))
        return paths

    def _run_operator(self, images):
        """用假 bpy 跑一遍真实 execute()，texconv 换成「直接成功」的桩。"""
        _fake_bpy.data.images = list(images)
        props = types.SimpleNamespace(
            output_dir=self.output_dir,
            texconv_path="",
            dds_delete_originals=False,
            dds_reencode_existing_dds=False,
            dds_use_custom_rules=False,
            dds_rules=[],
        )
        context = types.SimpleNamespace(scene=types.SimpleNamespace(texture_tools_props=props))

        operator = dds_conversion.TT_OT_convert_to_dds()
        reports = []
        operator.report = lambda level, message: reports.append((level, message))

        with mock.patch.object(dds_conversion, "find_texconv", return_value="texconv.exe"), \
                mock.patch.object(
                    dds_conversion.subprocess,
                    "run",
                    return_value=types.SimpleNamespace(returncode=0, stdout="", stderr=""),
                ):
            result = operator.execute(context)

        return result, reports

    @staticmethod
    def _report_text(reports):
        return " | ".join(str(message) for _level, message in reports)

    def test_non_color_image_stays_non_color_when_target_is_srgb_format(self):
        """Non-Color 的图转成 bc7_unorm_srgb 目标后仍是 Non-Color（不再被改成 sRGB）。"""
        (source_path,) = self._write_sources("DiffuseMap_Body.png")
        image = _FakeImage("DiffuseMap_Body.png", source_path, colorspace="Non-Color")

        result, _reports = self._run_operator([image])

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(image.reload_count, 1)
        self.assertEqual(image.colorspace_settings.name, "Non-Color")
        self.assertTrue(image.filepath.endswith(".dds"))

    def test_srgb_image_stays_srgb_when_target_is_linear_format(self):
        """sRGB 的图转成 bc7_unorm 目标后仍是 sRGB（不再被改成 Non-Color）。"""
        (source_path,) = self._write_sources("LightMap_Hair.png")
        image = _FakeImage("LightMap_Hair.png", source_path, colorspace="sRGB")

        result, _reports = self._run_operator([image])

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(image.colorspace_settings.name, "sRGB")

    def test_alpha_mode_survives_relink(self):
        """alpha_mode 在重连前后不变（回归锁定）。"""
        source_a, source_b = self._write_sources("NormalMap_Face.png", "DiffuseMap_Body.png")
        packed = _FakeImage("NormalMap_Face.png", source_a, colorspace="Non-Color", alpha_mode="CHANNEL_PACKED")
        plain = _FakeImage("DiffuseMap_Body.png", source_b, colorspace="sRGB", alpha_mode="NONE")

        result, _reports = self._run_operator([packed, plain])

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(packed.alpha_mode, "CHANNEL_PACKED")
        self.assertEqual(plain.alpha_mode, "NONE")

    def test_texture_node_display_settings_survive_relink(self):
        """引用该 image 的 ShaderNodeTexImage 节点 interpolation / extension / projection 不变。"""
        (source_path,) = self._write_sources("DiffuseMap_Body.png")
        image = _FakeImage("DiffuseMap_Body.png", source_path, colorspace="Non-Color")
        node = _FakeTexNode(image)

        result, _reports = self._run_operator([image])

        self.assertEqual(result, {"FINISHED"})
        self.assertIs(node.image, image)
        self.assertEqual(node.interpolation, "Closest")
        self.assertEqual(node.extension, "MIRROR")
        self.assertEqual(node.projection, "FLAT")

    def test_single_image_failure_does_not_block_other_images(self):
        """转换循环里某张图抛异常时不影响其它图片（原有语义，重连改造后保持）。"""
        source_a, source_b, source_c = self._write_sources(
            "DiffuseMap_A.png", "NormalMap_B.png", "LightMap_C.png"
        )
        ok_a = _FakeImage("DiffuseMap_A.png", source_a, colorspace="sRGB")
        broken = _FakeImage(
            "NormalMap_B.png",
            source_b,
            colorspace="Non-Color",
            alpha_mode="CHANNEL_PACKED",
            reload_error=RuntimeError("模拟重载失败"),
        )
        ok_c = _FakeImage("LightMap_C.png", source_c, colorspace="Non-Color")

        result, reports = self._run_operator([ok_a, broken, ok_c])

        self.assertEqual(result, {"FINISHED"})
        self.assertTrue(ok_a.filepath.endswith(".dds"))
        self.assertTrue(ok_c.filepath.endswith(".dds"))
        self.assertEqual(ok_a.reload_count, 1)
        self.assertEqual(ok_c.reload_count, 1)
        # 失败的图不计入「更新了 N 个图片引用」，并且要有一条指名道姓的警告
        report_text = self._report_text(reports)
        self.assertIn("更新了 2 个图片引用", report_text)
        self.assertIn("NormalMap_B.png", report_text)
        # 重载失败也不该顺手改写它的既有配置（filepath 已经换过了）
        self.assertEqual(broken.colorspace_settings.name, "Non-Color")
        self.assertEqual(broken.alpha_mode, "CHANNEL_PACKED")

    def test_fallback_uses_format_colorspace_only_when_snapshot_unreadable(self):
        """快照取不到有效值时，才退回「按格式设置」的兜底。"""
        source_srgb, source_linear = self._write_sources("DiffuseMap_Fb.png", "LightMap_Fb.png")
        unreadable_srgb = _FakeImage("DiffuseMap_Fb.png", source_srgb, colorspace="Non-Color")
        unreadable_srgb.colorspace_settings.fail_on_get = True
        unreadable_linear = _FakeImage("LightMap_Fb.png", source_linear, colorspace="sRGB")
        unreadable_linear.colorspace_settings.fail_on_get = True

        result, _reports = self._run_operator([unreadable_srgb, unreadable_linear])

        self.assertEqual(result, {"FINISHED"})
        # DiffuseMap -> bc7_unorm_srgb -> 兜底 sRGB；LightMap -> bc7_unorm -> 兜底 Non-Color
        self.assertEqual(unreadable_srgb.colorspace_settings.peek(), "sRGB")
        self.assertEqual(unreadable_linear.colorspace_settings.peek(), "Non-Color")

    def test_colorspace_write_failure_is_swallowed(self):
        """快照值写回失败时按格式兜底；两条路径都失败也不能中断转换循环。"""
        (source_path,) = self._write_sources("DiffuseMap_Body.png")
        image = _FakeImage("DiffuseMap_Body.png", source_path, colorspace="Non-Color")
        image.colorspace_settings.fail_on_set = True

        result, reports = self._run_operator([image])

        self.assertEqual(result, {"FINISHED"})
        self.assertTrue(image.filepath.endswith(".dds"))
        self.assertIn("更新了 1 个图片引用", self._report_text(reports))


class ConvertTextureToDDSTests(unittest.TestCase):
    """导出流程复用的单文件转换：口径必须与「批量转换为 .dds」算子一致。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="convert_one_")
        self.source = os.path.join(self.temp_dir, "c209c22b-45087-0-DiffuseMap.png")
        Path(self.source).write_bytes(b"png payload")
        self.target = os.path.join(self.temp_dir, "out", "Body_Material.dds")
        self.props = types.SimpleNamespace(dds_use_custom_rules=False, dds_rules=[])

    def _run(self, capture=None, returncode=0):
        def fake_run(command, **_kwargs):
            if capture is not None:
                capture.append(command)
            if returncode == 0:
                out_dir = command[command.index("-o") + 1]
                produced = os.path.join(
                    out_dir, os.path.splitext(os.path.basename(command[-1]))[0] + ".dds"
                )
                Path(produced).write_bytes(b"DDS payload")
            return types.SimpleNamespace(
                returncode=returncode, stdout="", stderr="boom" if returncode else ""
            )

        with mock.patch.object(dds_conversion, "find_texconv", return_value="texconv.exe"), \
                mock.patch.object(dds_conversion.subprocess, "run", side_effect=fake_run):
            return dds_conversion.convert_texture_to_dds(self.source, self.target, self.props)

    def test_diffuse_map_uses_srgb_rule_and_writes_target(self):
        """DiffuseMap 按规则表走 bc7_unorm_srgb，并用 --srgb-in 保证颜色不变"""
        commands = []
        ok, reason = self._run(capture=commands)

        self.assertTrue(ok, reason)
        self.assertTrue(os.path.exists(self.target))
        command = commands[0]
        self.assertEqual("bc7_unorm_srgb", command[command.index("-f") + 1])
        self.assertIn("--srgb-in", command)
        self.assertEqual(self.source, command[-1])

    def test_normal_map_uses_linear_rule(self):
        """NormalMap 走线性格式，用 --ignore-srgb 按原始数值读写"""
        self.source = os.path.join(self.temp_dir, "c209c22b-45087-0-NormalMap.png")
        Path(self.source).write_bytes(b"png payload")
        commands = []
        ok, reason = self._run(capture=commands)

        self.assertTrue(ok, reason)
        command = commands[0]
        self.assertEqual("r8g8b8a8_unorm", command[command.index("-f") + 1])
        self.assertIn("--ignore-srgb", command)

    def test_source_file_is_untouched(self):
        """编辑期的源 PNG 必须原样保留，导出只是另外产出一份 DDS"""
        ok, _reason = self._run()

        self.assertTrue(ok)
        self.assertEqual(b"png payload", Path(self.source).read_bytes())

    def test_missing_texconv_reports_failure(self):
        with mock.patch.object(dds_conversion, "find_texconv", return_value=None):
            ok, reason = dds_conversion.convert_texture_to_dds(self.source, self.target, self.props)

        self.assertFalse(ok)
        self.assertIn("texconv", reason)

    def test_texconv_failure_reports_reason_and_leaves_no_target(self):
        """失败时返回原因且不得留下半个目标文件"""
        ok, reason = self._run(returncode=1)

        self.assertFalse(ok)
        self.assertIn("boom", reason)
        self.assertFalse(os.path.exists(self.target))


if __name__ == "__main__":
    unittest.main()
