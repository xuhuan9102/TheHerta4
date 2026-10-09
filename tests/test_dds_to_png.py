import importlib.util
import os
import struct
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


PKG = "_dds_to_png_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


_registered_timers = []

_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object, Panel=object),
    # tt_dds_conversion 的算子类体在注解里取 bpy.props.IntProperty() 等构造器
    # （该模块没有 from __future__ import annotations），桩必须与
    # tests/test_dds_conversion.py 同口径，否则本文件连收集都过不去。
    props=types.SimpleNamespace(
        BoolProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
        FloatProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        StringProperty=lambda **_kwargs: None,
    ),
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(texture_tools_props=types.SimpleNamespace())
    ),
    data=types.SimpleNamespace(images=[], scenes=[]),
    path=types.SimpleNamespace(abspath=lambda value: value),
    app=types.SimpleNamespace(
        timers=types.SimpleNamespace(
            register=lambda fn, first_interval=0: _registered_timers.append((fn, first_interval))
        )
    ),
)
_install_module("bpy", **_fake_bpy.__dict__)


def _load(submodule):
    path = Path(__file__).resolve().parents[1] / "toolkit" / f"{submodule}.py"
    spec = importlib.util.spec_from_file_location(f"{PKG}.toolkit.{submodule}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load("tt_dds_conversion")
dds_to_png = _load("tt_dds_to_png")


class FakeImage:
    """最小 image 桩：只保留重连路径会碰到的那几个字段。"""

    def __init__(self, filepath, source="FILE", colorspace="sRGB", alpha_mode="STRAIGHT"):
        self.source = source
        self.filepath = filepath
        self.filepath_raw = filepath
        self.colorspace_settings = types.SimpleNamespace(name=colorspace)
        self.alpha_mode = alpha_mode
        self.reload_count = 0

    def reload(self):
        self.reload_count += 1


def _write_png(path, bit_depth=8, gama=None, srgb=False):
    """写一个最小 PNG（IHDR + 可选 gAMA/sRGB + IEND），供位深与色彩元数据判读。"""

    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", 0)

    body = bytearray(b"\x89PNG\r\n\x1a\n")
    body += chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, bit_depth, 6, 0, 0, 0))
    if gama is not None:
        body += chunk(b"gAMA", struct.pack(">I", gama))
    if srgb:
        body += chunk(b"sRGB", b"\x00")
    body += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(bytes(body))


def _write_dds(path, dxgi_format=None, legacy_d3dfmt=None):
    """写一个合法 DDS 头。

    ``dxgi_format`` 非 None 时补上 DX10 扩展头；``legacy_d3dfmt`` 非 None 时模拟
    legacy 头把 D3DFMT 枚举值写在 dwFourCC 位置（实测真实文件里 113 =
    D3DFMT_A16B16G16R16F = R16G16B16A16_FLOAT）。
    """
    header = bytearray(148 if dxgi_format is not None else 128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<II", header, 12, 16, 16)  # height, width
    if dxgi_format is not None:
        header[84:88] = b"DX10"
        struct.pack_into("<I", header, 128, dxgi_format)
    if legacy_d3dfmt is not None:
        struct.pack_into("<I", header, 80, 0x4)  # DDPF_FOURCC
        struct.pack_into("<I", header, 84, legacy_d3dfmt)
    with open(path, "wb") as handle:
        handle.write(bytes(header))


def _fake_texconv_run(command, gama=None, srgb=False, **_kwargs):
    """按命令里的 -o/-y 真的落一个 png，让 convert_dds_to_png 的存在性检查通过。

    输出位深跟着命令走：带 16bit 格式的就是 16bit PNG，否则 8bit。
    ``gama`` / ``srgb`` 用来模拟 texconv 写进产物的色彩元数据。
    """
    dds_path = command[-1]
    out_dir = command[command.index("-o") + 1]
    png_path = os.path.join(out_dir, os.path.splitext(os.path.basename(dds_path))[0] + ".png")
    bit_depth = 16 if "R16G16B16A16_UNORM" in command else 8
    _write_png(png_path, bit_depth, gama=gama, srgb=srgb)
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class DDSToPNGTests(unittest.TestCase):
    """DDS 引用转 PNG：色彩空间、保留原文件、重连显示配置、自动调度。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dds_to_png_")
        _fake_bpy.data.images = []
        _fake_bpy.context.scene.texture_tools_props = types.SimpleNamespace(
            dds_auto_convert_png_after_import=False
        )
        _registered_timers.clear()
        dds_to_png._AUTO_JOB.update(
            {"images": None, "force": False, "converted": 0, "skipped": 0, "failed": 0}
        )
        # find_texconv 是模块级名字，patch 它即可，不碰真实 Toolset 目录
        patcher = mock.patch.object(dds_to_png, "find_texconv", return_value="texconv.exe")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _make_dds(self, name="X-LightMap.dds", dxgi_format=None, legacy_d3dfmt=None):
        path = os.path.join(self.temp_dir, name)
        _write_dds(path, dxgi_format, legacy_d3dfmt)
        return path

    def test_png_path_is_same_name_with_png_extension(self):
        """同名换扩展名，且保持所在目录不变（转换是原地进行的）"""
        self.assertEqual(
            dds_to_png.png_path_for(os.path.join("a", "b", "X-LightMap.dds")),
            os.path.join("a", "b", "X-LightMap.png"),
        )

    def test_command_must_not_pin_output_format(self):
        """核心回归：命令里绝不能出现 -f。

        色彩空间语义靠 texconv 依输入 DDS 自行保持（BC7_UNORM_SRGB ->
        R8G8B8A8_UNORM_SRGB，BC7_UNORM -> R8G8B8A8_UNORM）。一旦用 -f 手工钉死格式，
        写错就会让 sRGB 贴图被线性解码一次，表现为整片颜色变深 —— 这正是
        tt_dds_conversion 的注释里记着的旧坑。
        """
        dds_path = self._make_dds()
        captured = []

        def capture(command, **kwargs):
            captured.append(command)
            return _fake_texconv_run(command, **kwargs)

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=capture):
            dds_to_png.convert_dds_to_png("texconv.exe", dds_path)

        self.assertEqual(1, len(captured))
        self.assertNotIn("-f", captured[0])
        self.assertEqual(
            ["texconv.exe", "-ft", "png", "-o", self.temp_dir, "-y", dds_path],
            captured[0],
        )

    def test_original_dds_is_never_deleted(self):
        """用户明确要求保留原文件：转换只新增 .png，不动 .dds"""
        dds_path = self._make_dds()
        image = FakeImage(dds_path)
        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=_fake_texconv_run):
            status, _detail = dds_to_png.process_one(image, dds_path)

        self.assertEqual("converted", status)
        self.assertTrue(os.path.exists(dds_path), "原 .dds 必须保留")
        self.assertTrue(os.path.exists(dds_to_png.png_path_for(dds_path)))
        self.assertTrue(image.filepath.endswith(".png"))
        self.assertEqual(1, image.reload_count)

    def test_relink_keeps_colorspace_and_alpha_mode(self):
        """重连只换容器与路径，用户已设好的显示配置必须原样保留"""
        dds_path = self._make_dds()
        image = FakeImage(dds_path, colorspace="Non-Color", alpha_mode="PREMUL")
        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=_fake_texconv_run):
            dds_to_png.process_one(image, dds_path)

        self.assertEqual("Non-Color", image.colorspace_settings.name)
        self.assertEqual("PREMUL", image.alpha_mode)

    def test_existing_png_skips_conversion_but_still_relinks(self):
        """已有同名 png 就跳过 texconv，只重连 —— 重复导入不该反复转换"""
        dds_path = self._make_dds()
        png_path = dds_to_png.png_path_for(dds_path)
        _write_png(png_path, 8)

        image = FakeImage(dds_path)
        with mock.patch.object(dds_to_png.subprocess, "run") as run_mock:
            status, _detail = dds_to_png.process_one(image, dds_path, force=False)

        self.assertEqual("skipped", status)
        run_mock.assert_not_called()
        self.assertTrue(image.filepath.endswith(".png"))

    def test_force_reconverts_even_when_png_exists(self):
        """force=True 用于修正磁盘上颜色不对的旧 png，必须真的重跑 texconv"""
        dds_path = self._make_dds()
        png_path = dds_to_png.png_path_for(dds_path)
        with open(png_path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nstale")
        os.utime(png_path, (os.path.getmtime(dds_path) + 10, os.path.getmtime(dds_path) + 10))

        image = FakeImage(dds_path)
        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=_fake_texconv_run) as run_mock:
            status, _detail = dds_to_png.process_one(image, dds_path, force=True)

        self.assertEqual("converted", status)
        run_mock.assert_called_once()

    def test_texconv_failure_leaves_reference_untouched(self):
        """转换失败不能让引用变成指向不存在的 png"""
        dds_path = self._make_dds()
        image = FakeImage(dds_path)
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

        with mock.patch.object(dds_to_png.subprocess, "run", return_value=failed):
            status, detail = dds_to_png.process_one(image, dds_path)

        self.assertEqual("failed", status)
        self.assertIn("boom", detail)
        self.assertEqual(dds_path, image.filepath)
        self.assertEqual(0, image.reload_count)

    def test_missing_texconv_reports_failure(self):
        """找不到 texconv 时给出明确失败原因，而不是抛异常中断整个导入"""
        dds_path = self._make_dds()
        image = FakeImage(dds_path)
        with mock.patch.object(dds_to_png, "find_texconv", return_value=None):
            status, detail = dds_to_png.process_one(image, dds_path)

        self.assertEqual("failed", status)
        self.assertIn("texconv", detail)

    def test_iter_only_returns_file_images_with_dds_extension(self):
        """只挑 source=FILE 且路径以 .dds 结尾的图片"""
        dds_path = self._make_dds("A-DiffuseMap.dds")
        png_path = os.path.join(self.temp_dir, "B-DiffuseMap.png")
        _fake_bpy.data.images = [
            FakeImage(dds_path),
            FakeImage(png_path),
            FakeImage(dds_path, source="GENERATED"),
            FakeImage(""),
        ]

        found = dds_to_png.iter_dds_images()

        self.assertEqual(1, len(found))
        self.assertEqual(dds_path, found[0][1])

    def test_convert_all_sums_up_statuses(self):
        """批量统计：转换 / 沿用 / 失败三类都要计上"""
        ok_path = self._make_dds("OK-LightMap.dds")
        bad_path = self._make_dds("BAD-LightMap.dds")
        good = FakeImage(ok_path)
        bad = FakeImage(bad_path)
        _fake_bpy.data.images = [good, bad]

        boom = types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
        calls = {"n": 0}

        def run(command, **kwargs):
            calls["n"] += 1
            if command[-1] == bad_path:
                return boom
            return _fake_texconv_run(command, **kwargs)

        messages = []
        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=run):
            stats = dds_to_png.convert_all(reporter=lambda kind, text: messages.append((kind, text)))

        self.assertEqual(2, stats["total"])
        self.assertEqual(1, stats["converted"])
        self.assertEqual(1, stats["failed"])
        self.assertTrue(any(kind == "WARNING" for kind, _text in messages))
        self.assertTrue(any("保留" in text for _kind, text in messages))

    def test_schedule_does_nothing_when_unchecked(self):
        """没勾选时，导入流程结束后不得启动任何后台任务"""
        dds_path = self._make_dds()
        _fake_bpy.data.images = [FakeImage(dds_path)]

        started = dds_to_png.schedule_auto_convert()

        self.assertFalse(started)
        self.assertEqual([], _registered_timers)
        self.assertIsNone(dds_to_png._AUTO_JOB["images"])

    def test_schedule_returns_false_without_any_dds(self):
        """勾选了但工程里没有 .dds 引用时不做无意义调度"""
        _fake_bpy.context.scene.texture_tools_props = types.SimpleNamespace(
            dds_auto_convert_png_after_import=True
        )
        _fake_bpy.data.images = [FakeImage(os.path.join(self.temp_dir, "a.png"))]

        self.assertFalse(dds_to_png.schedule_auto_convert())
        self.assertEqual([], _registered_timers)

    def test_auto_tick_processes_one_image_per_frame_then_finishes(self):
        """自动流程每帧只处理一张，避免长耗时卡住界面；队列清空后返回 None 注销 timer"""
        _fake_bpy.context.scene.texture_tools_props = types.SimpleNamespace(
            dds_auto_convert_png_after_import=True
        )
        paths = [self._make_dds(f"P{i}-LightMap.dds") for i in range(3)]
        _fake_bpy.data.images = [FakeImage(p) for p in paths]

        self.assertTrue(dds_to_png.schedule_auto_convert())
        self.assertEqual(1, len(_registered_timers))
        _tick, _interval = _registered_timers[0]

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=_fake_texconv_run) as run_mock:
            for _ in range(3):
                self.assertEqual(dds_to_png._AUTO_INTERVAL, _tick())
            self.assertIsNone(_tick())

        self.assertEqual(3, run_mock.call_count)
        self.assertEqual(3, dds_to_png._AUTO_JOB["converted"])
        self.assertIsNone(dds_to_png._AUTO_JOB["images"])

    def test_schedule_is_not_reentrant(self):
        """同一批任务已在跑时，第二次导入不得再叠一个定时器"""
        _fake_bpy.context.scene.texture_tools_props = types.SimpleNamespace(
            dds_auto_convert_png_after_import=True
        )
        dds_path = self._make_dds()
        _fake_bpy.data.images = [FakeImage(dds_path)]

        self.assertTrue(dds_to_png.schedule_auto_convert())
        self.assertFalse(dds_to_png.schedule_auto_convert())
        self.assertEqual(1, len(_registered_timers))

    def test_is_hdr_dds_only_matches_float_formats(self):
        """HDR 判据：带 DX10 头的浮点格式（BC6H=95）为真，sRGB 整型与 legacy 为假"""
        hdr = self._make_dds("A-LightMap.dds", dxgi_format=95)
        srgb = self._make_dds("B-DiffuseMap.dds", dxgi_format=99)
        linear = self._make_dds("C-NormalMap.dds", dxgi_format=98)
        legacy = self._make_dds("D-DiffuseMap.dds")

        self.assertTrue(dds_to_png.is_hdr_dds(hdr))
        self.assertFalse(dds_to_png.is_hdr_dds(srgb))
        self.assertFalse(dds_to_png.is_hdr_dds(linear))
        self.assertFalse(dds_to_png.is_hdr_dds(legacy))

    def test_is_hdr_dds_recognizes_legacy_d3dfmt_float(self):
        """legacy 头把 D3DFMT 枚举值写在 dwFourCC 位置时，也必须是 HDR 源

        用户报的「线性贴图转换后颜色变淡」就是这个漏判：实测真实文件
        611df76d-132-0-LightMap.dds 是 legacy 头、dwFourCC=113
        (D3DFMT_A16B16G16R16F = R16G16B16A16_FLOAT)、G 通道真值 0.5020。
        漏判 -> texconv 走 8bit PNG 的 sRGB 编码路径写出 188 而不是 128，整片变亮变淡。
        """
        for d3dfmt in (111, 112, 113, 114, 115, 116):
            path = self._make_dds("F%d-LightMap.dds" % d3dfmt, legacy_d3dfmt=d3dfmt)
            self.assertTrue(dds_to_png.is_hdr_dds(path), "D3DFMT %d 应判为 HDR" % d3dfmt)

    def test_is_hdr_dds_rejects_legacy_non_float_fourcc(self):
        """legacy 的 DXT1/DXT5 以及未标 DDPF_FOURCC 的未压缩头仍是普通整型源"""
        dxt1 = self._make_dds("A-DiffuseMap.dds", legacy_d3dfmt=int.from_bytes(b"DXT1", "little"))
        dxt5 = self._make_dds("B-DiffuseMap.dds", legacy_d3dfmt=int.from_bytes(b"DXT5", "little"))
        uncompressed = self._make_dds("C-DiffuseMap.dds")

        self.assertFalse(dds_to_png.is_hdr_dds(dxt1))
        self.assertFalse(dds_to_png.is_hdr_dds(dxt5))
        self.assertFalse(dds_to_png.is_hdr_dds(uncompressed))

    def test_legacy_float_dds_pins_16bit_output(self):
        """核心回归：legacy 浮点源同样必须显式指定 16bit 输出"""
        dds_path = self._make_dds("HDR-LightMap.dds", legacy_d3dfmt=113)
        captured = []

        def capture(command, **kwargs):
            captured.append(command)
            return _fake_texconv_run(command, **kwargs)

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=capture):
            dds_to_png.convert_dds_to_png("texconv.exe", dds_path)

        self.assertEqual(
            ["texconv.exe", "-f", "R16G16B16A16_UNORM", "-ft", "png",
             "-o", self.temp_dir, "-y", dds_path],
            captured[0],
        )

    def test_legacy_float_source_png_is_reused(self):
        """legacy 浮点源同样：已有同名 png 就沿用，不再每次导入都重转"""
        dds_path = self._make_dds("HDR-LightMap.dds", legacy_d3dfmt=113)
        png_path = dds_to_png.png_path_for(dds_path)
        _write_png(png_path, 16)

        image = FakeImage(dds_path)
        with mock.patch.object(dds_to_png.subprocess, "run") as run_mock:
            status, _detail = dds_to_png.process_one(image, dds_path, force=False)

        self.assertEqual("skipped", status)
        run_mock.assert_not_called()

    def test_png_bit_depth_reads_ihdr(self):
        """位深要从 PNG 头里读出来，用于识别历史错误的 8bit 产物"""
        path = os.path.join(self.temp_dir, "probe.png")
        _write_png(path, 16)
        self.assertEqual(16, dds_to_png.png_bit_depth(path))
        _write_png(path, 8)
        self.assertEqual(8, dds_to_png.png_bit_depth(path))
        self.assertIsNone(dds_to_png.png_bit_depth(os.path.join(self.temp_dir, "missing.png")))

    def test_png_color_metadata_and_strip(self):
        """色彩元数据能读出来，也能被清掉（清第二次不应再算改动）"""
        path = os.path.join(self.temp_dir, "meta.png")
        _write_png(path, 8, gama=100000, srgb=False)
        self.assertEqual((8, ["gAMA=100000"]), dds_to_png.png_color_metadata(path))

        self.assertTrue(dds_to_png.strip_color_metadata(path))
        self.assertEqual((8, []), dds_to_png.png_color_metadata(path))
        self.assertFalse(dds_to_png.strip_color_metadata(path))

    def test_linear_conversion_strips_color_metadata(self):
        """核心回归：线性源产出的 png 必须被清掉 texconv 打的 gAMA

        真实踩到过：texconv 给线性源写 gAMA=1.0，Photoshop CC 2017 / Paint.NET 会
        据此把它从"线性"转到工作空间 sRGB（重新编码一次），整片变亮；DDS 没有这种
        元数据，于是用户一眼就看出转换后的 png 不对。sRGB 源的 sRGB chunk 是正确
        的，不能动（见下个用例）。
        """
        dds_path = self._make_dds("X-LightMap.dds")  # legacy raw，线性
        image = FakeImage(dds_path)

        def run(command, **kwargs):
            return _fake_texconv_run(command, gama=100000, **kwargs)

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=run):
            status, _detail = dds_to_png.process_one(image, dds_path)

        self.assertEqual("converted", status)
        _depth, chunks = dds_to_png.png_color_metadata(dds_to_png.png_path_for(dds_path))
        self.assertEqual([], chunks)

    def test_srgb_conversion_keeps_srgb_chunk(self):
        """sRGB 源的产物保持 texconv 写的色彩元数据（显示本来就是对的）"""
        dds_path = self._make_dds("SRGB-DiffuseMap.dds", dxgi_format=99)
        image = FakeImage(dds_path)

        def run(command, **kwargs):
            return _fake_texconv_run(command, gama=45455, srgb=True, **kwargs)

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=run):
            status, _detail = dds_to_png.process_one(image, dds_path)

        self.assertEqual("converted", status)
        _depth, chunks = dds_to_png.png_color_metadata(dds_to_png.png_path_for(dds_path))
        self.assertIn("sRGB", chunks)

    def test_hdr_source_pins_16bit_output(self):
        """核心回归：BC6H 这类 float 源必须显式指定 16bit 输出。

        否则 texconv 走 8bit 路径时会把数值按 sRGB 编码整体提亮
        （LightMap 实测 0.2172 -> 0.2993，低端放大 3.8 倍），颜色就变了。
        """
        dds_path = self._make_dds("HDR-LightMap.dds", dxgi_format=95)
        captured = []

        def capture(command, **kwargs):
            captured.append(command)
            return _fake_texconv_run(command, **kwargs)

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=capture):
            dds_to_png.convert_dds_to_png("texconv.exe", dds_path)

        self.assertEqual(
            ["texconv.exe", "-f", "R16G16B16A16_UNORM", "-ft", "png", "-o", self.temp_dir, "-y", dds_path],
            captured[0],
        )

    def test_any_existing_png_is_reused_even_if_suspect(self):
        """只看存在性：哪怕是 8bit 的历史产物也照样沿用，由用户自己删

        用户明确要求不要自动判定内容并重转 —— 有问题的 png 他手动删，删掉后下次导入
        只会补转缺的那几张。
        """
        dds_path = self._make_dds("HDR-LightMap.dds", dxgi_format=95)
        png_path = dds_to_png.png_path_for(dds_path)
        _write_png(png_path, 8, gama=100000)

        image = FakeImage(dds_path)
        with mock.patch.object(dds_to_png.subprocess, "run") as run_mock:
            status, _detail = dds_to_png.process_one(image, dds_path, force=False)

        self.assertEqual("skipped", status)
        run_mock.assert_not_called()

    def test_correct_png_is_reused_without_rerunning_texconv(self):
        """浮点 / sRGB / 线性三类源，只要有同名 png 就沿用，绝不再跑一遍 texconv

        用户抱怨每次导入都卡一会儿 —— 浮点/HDR 源原本被无条件重转。
        """
        cases = (("HDR-LightMap.dds", 95, 16), ("SRGB-DiffuseMap.dds", 99, 8),
                 ("X-LightMap.dds", None, 8))
        for name, dxgi, depth in cases:
            dds_path = self._make_dds(name, dxgi_format=dxgi)
            png_path = dds_to_png.png_path_for(dds_path)
            _write_png(png_path, depth)
            stamp = os.path.getmtime(dds_path) + 10
            os.utime(png_path, (stamp, stamp))

            image = FakeImage(dds_path)
            with mock.patch.object(dds_to_png.subprocess, "run") as run_mock:
                status, _detail = dds_to_png.process_one(image, dds_path, force=False)

            self.assertEqual("skipped", status, name)
            run_mock.assert_not_called()
            self.assertTrue(image.filepath.endswith(".png"))

    def test_hdr_source_without_16bit_output_reports_failure(self):
        """结果校验：HDR 源没产出 16bit PNG 时报失败，绝不把错图重连上去"""
        dds_path = self._make_dds("HDR-LightMap.dds", dxgi_format=95)
        image = FakeImage(dds_path)

        def wrong_depth(command, **_kwargs):
            out_dir = command[command.index("-o") + 1]
            png_path = os.path.join(
                out_dir, os.path.splitext(os.path.basename(command[-1]))[0] + ".png"
            )
            _write_png(png_path, 8)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=wrong_depth):
            status, detail = dds_to_png.process_one(image, dds_path)

        self.assertEqual("failed", status)
        self.assertIn("16bit", detail)
        self.assertEqual(dds_path, image.filepath)

    def test_non_hdr_source_still_uses_8bit_without_pinned_format(self):
        """整型源不受影响：仍走 8bit 且不钉死格式，保住 texconv 的色彩空间判断"""
        dds_path = self._make_dds("SRGB-DiffuseMap.dds", dxgi_format=99)
        captured = []

        def capture(command, **kwargs):
            captured.append(command)
            return _fake_texconv_run(command, **kwargs)

        with mock.patch.object(dds_to_png.subprocess, "run", side_effect=capture):
            dds_to_png.convert_dds_to_png("texconv.exe", dds_path)

        self.assertNotIn("-f", captured[0])
        self.assertEqual(8, dds_to_png.png_bit_depth(dds_to_png.png_path_for(dds_path)))


if __name__ == "__main__":
    unittest.main()
