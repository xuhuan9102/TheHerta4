"""通道合成器纯色档（光照贴图 / 材质贴图）测试

覆盖：预设、来源枚举、纯色属性、256×256 写盘字节、无基础贴图材质照样出图、
面板在纯色规则下不绘制任何通道行。
"""

import importlib.util
import struct
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_channel_composite_solid_color_test_pkg"
for package_name in (PKG, f"{PKG}.toolkit", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(Operator=object, Panel=object, PropertyGroup=object, Material=object),
    props=types.SimpleNamespace(
        IntProperty=lambda **kwargs: dict(kwargs),
        FloatProperty=lambda **kwargs: dict(kwargs),
        FloatVectorProperty=lambda **kwargs: dict(kwargs),
        StringProperty=lambda **kwargs: dict(kwargs),
        BoolProperty=lambda **kwargs: dict(kwargs),
        EnumProperty=lambda **kwargs: dict(kwargs),
        PointerProperty=lambda **kwargs: dict(kwargs),
        CollectionProperty=lambda **kwargs: dict(kwargs),
    ),
    path=types.SimpleNamespace(abspath=lambda value: value),
)
_install_module("bpy", **_fake_bpy.__dict__)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_real_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_real_module(f"{PKG}.toolkit.at_utils", _REPO_ROOT / "toolkit" / "at_utils.py")
tt_normal_map = _load_real_module(f"{PKG}.toolkit.tt_normal_map", _REPO_ROOT / "toolkit" / "tt_normal_map.py")
tt_properties = _load_real_module(f"{PKG}.toolkit.tt_properties", _REPO_ROOT / "toolkit" / "tt_properties.py")

_install_module(f"{PKG}.common.global_config", GlobalConfig=type("GlobalConfig", (), {}))
ui_panel_toolkit = _load_real_module(
    f"{PKG}.toolkit.ui_panel_toolkit", _REPO_ROOT / "toolkit" / "ui_panel_toolkit.py")


def _srgb_to_linear(channel_255):
    c = channel_255 / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _hex_to_linear_rgba(red, green, blue, alpha=255):
    """模拟用户在取色器里输入 hex：subtype='COLOR' 的 RNA 值拿到的就是这套线性值"""
    return (
        _srgb_to_linear(red),
        _srgb_to_linear(green),
        _srgb_to_linear(blue),
        alpha / 255.0,
    )


def _decode_rgba_png(path):
    """独立解码 8-bit RGBA PNG：返回 (width, height, rows)，rows 为每行原始字节"""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("不是 PNG 文件")

    width = height = bit_depth = color_type = None
    compressed = b""
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        if crc != (zlib.crc32(tag + body) & 0xFFFFFFFF):
            raise AssertionError(f"chunk {tag!r} CRC 校验失败")
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            compressed += body
        pos += 12 + length

    raw = zlib.decompress(compressed)
    stride = width * 4
    rows = []
    offset = 0
    for _ in range(height):
        if raw[offset] != 0:
            raise AssertionError("预期 filter 0")
        rows.append(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
    return width, height, bit_depth, color_type, rows


class SolidColorPresetTests(unittest.TestCase):
    """预设与属性定义"""

    def test_solid_color_presets_are_appended(self):
        presets = tt_normal_map.COMPOSITE_PRESETS
        self.assertGreaterEqual(len(presets), 6)
        self.assertEqual(presets[4]["name"], "光照贴图")
        self.assertEqual(presets[4]["prefix"], "LightMap_")
        self.assertEqual(presets[5]["name"], "材质贴图")
        self.assertEqual(presets[5]["prefix"], "MaterialMap_")
        for preset in presets[4:6]:
            self.assertEqual(len(preset["channels"]), 4)
            for channel in preset["channels"]:
                self.assertEqual(channel["source_type"], "SOLID_COLOR")

        # 原有四条一字不改
        self.assertEqual(
            [(item["name"], item["prefix"]) for item in presets[:4]],
            [
                ("标准法线贴图", "NormalMap_"),
                ("ORM贴图 (AO/Rough/Metal)", "ORMMap_"),
                ("粗糙度贴图", "RoughnessMap_"),
                ("通道分离 (RGBA)", "Split_"),
            ],
        )

    def test_source_type_has_solid_color_at_end(self):
        items = tt_properties.TT_ChannelSource.__annotations__["source_type"]["items"]
        self.assertEqual(len(items), 14)
        self.assertEqual([item[0] for item in items[:13]], [
            "IMAGE_CHANNEL",
            "GRAYSCALE",
            "INVERT",
            "CONSTANT",
            "GENERATED_NORMAL",
            "GENERATED_HEIGHT",
            "GENERATED_ROUGHNESS",
            "GENERATED_GLOSSINESS",
            "GENERATED_AO",
            "GENERATED_METALLIC",
            "GENERATED_SPECULAR",
            "GENERATED_EMISSION",
            "GENERATED_DETAIL",
        ])
        self.assertEqual(items[13], ("SOLID_COLOR", "纯色", "整张贴图由「纯色」颜色填充"))

    def test_solid_color_property_is_chinese_color_vector(self):
        # Blender 的 bpy.props 定义写在类注解里，注册时由 __annotations__ 读取
        definition = tt_properties.TT_CompositeRule.__annotations__["solid_color"]
        self.assertEqual(definition["name"], "纯色")
        self.assertEqual(definition["subtype"], "COLOR")
        self.assertEqual(definition["size"], 4)
        self.assertEqual(definition["default"], (1.0, 1.0, 1.0, 1.0))


class SolidColorComposeTests(unittest.TestCase):
    """执行与写盘"""

    @staticmethod
    def _solid_rule(prefix="LightMap_", color=(1.0, 1.0, 1.0, 1.0), name="SolidRule"):
        return types.SimpleNamespace(
            rule_name=name,
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            output_name_prefix=prefix,
            output_channels=[
                types.SimpleNamespace(source_type="SOLID_COLOR", invert=False),
                types.SimpleNamespace(source_type="SOLID_COLOR", invert=False),
                types.SimpleNamespace(source_type="SOLID_COLOR", invert=False),
                types.SimpleNamespace(source_type="SOLID_COLOR", invert=False),
            ],
            solid_color=color,
            enabled=True,
        )

    def test_solid_png_bytes_match_picked_hex(self):
        """#FF8000 → PNG 五点字节 (255,128,0,255)"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        material = types.SimpleNamespace(name="Mat A")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            path, pixels, width, height = operator._apply_solid_color_rule(
                rule=self._solid_rule(color=_hex_to_linear_rgba(255, 128, 0)),
                material=material,
                output_dir=output_dir,
            )
            # 命名沿用 _apply_composite_rule 的清洗规则：空格被剔除
            self.assertEqual(Path(path).name, "LightMap_MatA.png")
            self.assertEqual((width, height), (256, 256))
            self.assertEqual(pixels.shape, (256, 256, 4))

            file_width, file_height, bit_depth, color_type, rows = _decode_rgba_png(path)
            self.assertEqual((file_width, file_height), (256, 256))
            self.assertEqual(bit_depth, 8)
            self.assertEqual(color_type, 6)

            for row_index in (0, 127, 255):
                row = rows[row_index]
                for column_index in (0, 127, 255):
                    offset = column_index * 4
                    self.assertEqual(
                        tuple(row[offset:offset + 4]),
                        (255, 128, 0, 255),
                        f"像素 ({row_index},{column_index}) 颜色不符",
                    )

    def test_solid_png_second_color(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        material = types.SimpleNamespace(name="MatC")

        with tempfile.TemporaryDirectory() as temp_dir:
            path, _pixels, _width, _height = operator._apply_solid_color_rule(
                rule=self._solid_rule(prefix="MaterialMap_", color=_hex_to_linear_rgba(51, 102, 204)),
                material=material,
                output_dir=Path(temp_dir),
            )
            _w, _h, _depth, _ctype, rows = _decode_rgba_png(path)
            self.assertEqual(tuple(rows[0][0:4]), (51, 102, 204, 255))

    def test_execute_outputs_solid_texture_without_any_image_node(self):
        """材质没有任何图像节点时，纯色规则照样出图（判据④）"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        operator.report = lambda *_args, **_kwargs: None

        class _Material:
            def __init__(self, name):
                self.name = name
                self.use_nodes = False

        material = _Material("MatB")
        obj = types.SimpleNamespace(
            type="MESH",
            material_slots=[types.SimpleNamespace(material=material)],
            data=types.SimpleNamespace(materials=[]),
        )
        props = types.SimpleNamespace(
            output_dir="",
            composite_rules=[self._solid_rule(color=_hex_to_linear_rgba(255, 128, 0))],
            normal_map_create_materials=False,
        )
        context = types.SimpleNamespace(
            selected_objects=[obj],
            scene=types.SimpleNamespace(texture_tools_props=props),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            props.output_dir = temp_dir
            self.assertEqual(operator.execute(context), {"FINISHED"})
            files = sorted(Path(temp_dir).glob("*.png"))
            self.assertEqual([item.name for item in files], ["LightMap_MatB.png"])
            file_width, file_height, _depth, _ctype, rows = _decode_rgba_png(files[0])
            self.assertEqual((file_width, file_height), (256, 256))
            self.assertEqual(tuple(rows[0][0:4]), (255, 128, 0, 255))

    def test_non_solid_rule_still_skipped_without_base_texture(self):
        """同一份无图像材质上，非纯色规则保持原有跳过行为"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        operator.report = lambda *_args, **_kwargs: None

        class _Material:
            def __init__(self, name):
                self.name = name
                self.use_nodes = False

        material = _Material("MatD")
        obj = types.SimpleNamespace(
            type="MESH",
            material_slots=[types.SimpleNamespace(material=material)],
            data=types.SimpleNamespace(materials=[]),
        )
        normal_rule = types.SimpleNamespace(
            rule_name="NormalRule",
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            output_name_prefix="NormalMap_",
            output_channels=[
                types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.5),
                types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.5),
                types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.5),
                types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=1.0),
            ],
            enabled=True,
        )
        props = types.SimpleNamespace(
            output_dir="",
            composite_rules=[normal_rule],
            normal_map_create_materials=False,
        )
        context = types.SimpleNamespace(
            selected_objects=[obj],
            scene=types.SimpleNamespace(texture_tools_props=props),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            props.output_dir = temp_dir
            self.assertEqual(operator.execute(context), {"FINISHED"})
            self.assertEqual(sorted(Path(temp_dir).glob("*.png")), [])

    def test_solid_rule_is_not_registered_as_upstream_output(self):
        """纯色规则不出现在 outputs_by_rule 里，NAMED_OUTPUT 引用它必须落空"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()

        with tempfile.TemporaryDirectory() as temp_dir:
            operator.report = lambda *_args, **_kwargs: None
            solid_rule = self._solid_rule(name="LightMap")
            downstream = types.SimpleNamespace(
                rule_name="Downstream",
                input_source_mode="NAMED_OUTPUT",
                input_rule_name="LightMap",
                output_name_prefix="Down_",
                output_channels=[
                    types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.5),
                    types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.5),
                    types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.5),
                    types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=1.0),
                ],
                enabled=True,
            )

            class _Material:
                def __init__(self, name):
                    self.name = name
                    self.use_nodes = False

            material = _Material("MatE")
            obj = types.SimpleNamespace(
                type="MESH",
                material_slots=[types.SimpleNamespace(material=material)],
                data=types.SimpleNamespace(materials=[]),
            )
            props = types.SimpleNamespace(
                output_dir=temp_dir,
                composite_rules=[solid_rule, downstream],
                normal_map_create_materials=False,
            )
            context = types.SimpleNamespace(
                selected_objects=[obj],
                scene=types.SimpleNamespace(texture_tools_props=props),
            )
            self.assertEqual(operator.execute(context), {"FINISHED"})
            self.assertEqual(sorted(item.name for item in Path(temp_dir).glob("*.png")),
                             ["LightMap_MatE.png"])


class _RecordingUI:
    """记录布局调用，用来断言纯色规则下没有任何通道行"""

    def __init__(self, log):
        self.log = log

    def prop(self, owner, attr, **kwargs):
        self.log.append(("prop", owner, attr, kwargs))
        return None

    def label(self, text="", **kwargs):
        self.log.append(("label", None, text, kwargs))
        return None

    def operator(self, idname, **kwargs):
        self.log.append(("operator", None, idname, kwargs))
        return types.SimpleNamespace()

    def row(self, **_kwargs):
        return _RecordingUI(self.log)

    def column(self, **_kwargs):
        return _RecordingUI(self.log)

    def box(self, **_kwargs):
        return _RecordingUI(self.log)

    def separator(self, **_kwargs):
        return None


class SolidColorPanelTests(unittest.TestCase):
    """面板：纯色分支只画一行取色器 + 一行固定尺寸"""

    @staticmethod
    def _draw(rule):
        panel = ui_panel_toolkit.TT_ChannelCompositePanel.__new__(ui_panel_toolkit.TT_ChannelCompositePanel)
        log = []
        panel.layout = _RecordingUI(log)
        props = types.SimpleNamespace(composite_rules=[rule], normal_map_create_materials=False)
        context = types.SimpleNamespace(scene=types.SimpleNamespace(texture_tools_props=props))
        panel.draw(context)
        return log

    def test_solid_rule_draws_no_channel_rows(self):
        channels = [types.SimpleNamespace(source_type="SOLID_COLOR", invert=False) for _ in range(4)]
        rule = types.SimpleNamespace(
            enabled=True,
            rule_name="光照贴图",
            output_name_prefix="LightMap_",
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            solid_color=(1.0, 0.21586050011389926, 0.0, 1.0),
            output_channels=channels,
        )
        log = self._draw(rule)

        channel_props = [entry for entry in log if entry[0] == "prop" and entry[1] in channels]
        self.assertEqual(channel_props, [])

        channel_attrs = {"source_type", "invert", "source_image_name", "source_channel", "constant_value"}
        for entry in log:
            if entry[0] == "prop":
                self.assertNotIn(entry[2], channel_attrs)

        solid_props = [entry for entry in log if entry[0] == "prop" and entry[2] == "solid_color"]
        self.assertEqual(len(solid_props), 1)
        self.assertEqual(solid_props[0][3].get("text"), "纯色")

        labels = [entry[2] for entry in log if entry[0] == "label"]
        self.assertIn("256×256（固定）", labels)
        for row_label in ("R:", "G:", "B:", "A:"):
            self.assertNotIn(row_label, labels)

    def test_non_solid_rule_still_draws_four_channel_rows(self):
        channels = [
            types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.5)
            for _ in range(4)
        ]
        rule = types.SimpleNamespace(
            enabled=True,
            rule_name="粗糙度贴图",
            output_name_prefix="RoughnessMap_",
            input_source_mode="BASE_COLOR",
            input_rule_name="",
            normal_strength=5.0,
            normal_blur_radius=1.0,
            ao_radius=16,
            ao_height_scale=16.0,
            ao_power=1.0,
            solid_color=(1.0, 1.0, 1.0, 1.0),
            output_channels=channels,
        )
        log = self._draw(rule)

        source_type_props = [entry for entry in log if entry[0] == "prop" and entry[2] == "source_type"]
        self.assertEqual(len(source_type_props), 4)
        labels = [entry[2] for entry in log if entry[0] == "label"]
        for row_label in ("R:", "G:", "B:", "A:"):
            self.assertIn(row_label, labels)


if __name__ == "__main__":
    unittest.main()
