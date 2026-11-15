"""通道合成器纯色档（光照贴图 / 材质贴图）测试

语义（2026-11-16 改版）：纯色规则一次执行只出一张图，按颜色命名、与选中物体/材质无关，
所有物体共用同一个材质与同一张图 datablock。

覆盖：预设、来源枚举、纯色属性、256×256 写盘字节（两次颜色）、多物体只出两张、
同色幂等（sha 不变/mtime 变）、换色保留旧图、0 物体、共享材质、非纯色命名不变、
面板纯色规则不画通道行。
"""

import hashlib
import importlib.util
import struct
import sys
import tempfile
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

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
    """独立解码 8-bit RGBA PNG：返回 (width, height, bit_depth, color_type, rows)"""
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


def _five_points(path, expected):
    width, height, bit_depth, color_type, rows = _decode_rgba_png(path)
    assert (width, height) == (256, 256), (width, height)
    assert (bit_depth, color_type) == (8, 6), (bit_depth, color_type)
    last = height - 1
    for x, y in ((0, 0), (0, last), (last, 0), (last, last), (last // 2, last // 2)):
        offset = x * 4
        assert tuple(rows[y][offset:offset + 4]) == expected, (x, y, rows[y][offset:offset + 4])
    return True


class _Material:
    def __init__(self, name):
        self.name = name
        self.use_nodes = False


class _MaterialList(list):
    """模拟 obj.data.materials：append 后物体的 material_slots 也看得到"""

    def __init__(self, owner, materials):
        super().__init__(materials)
        self._owner = owner

    def append(self, material):
        super().append(material)
        self._owner.material_slots.append(types.SimpleNamespace(material=material))


class _FakeObject:
    def __init__(self, name, materials=()):
        self.type = "MESH"
        self.name = name
        self.material_slots = [types.SimpleNamespace(material=material) for material in materials]
        self.data = types.SimpleNamespace(materials=_MaterialList(self, materials))


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


def _constant_rule(name="ConstantRule", prefix="Constant_", input_mode="BASE_COLOR", input_rule_name=""):
    return types.SimpleNamespace(
        rule_name=name,
        input_source_mode=input_mode,
        input_rule_name=input_rule_name,
        output_name_prefix=prefix,
        output_channels=[
            types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.1),
            types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.2),
            types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=0.3),
            types.SimpleNamespace(source_type="CONSTANT", invert=False, constant_value=1.0),
        ],
        enabled=True,
    )


def _run(operator, rules, objects, output_dir, create_materials=False):
    reports = []
    operator.report = lambda level, message: reports.append((set(level), message))
    props = types.SimpleNamespace(
        output_dir=str(output_dir),
        composite_rules=list(rules),
        normal_map_create_materials=create_materials,
    )
    context = types.SimpleNamespace(
        selected_objects=list(objects),
        scene=types.SimpleNamespace(texture_tools_props=props),
    )
    return operator.execute(context), reports


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


class SolidColorNamingTests(unittest.TestCase):
    """D2/C1/C3：单张出图、按颜色命名、字节精确"""
    def test_solid_png_named_by_hex_and_bytes_match(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        with tempfile.TemporaryDirectory() as temp_dir:
            path, hex_code = operator._apply_solid_color_rule(
                rule=_solid_rule(color=_hex_to_linear_rgba(255, 128, 0)),
                output_dir=Path(temp_dir),
            )
            self.assertEqual(hex_code, "FF8000")
            self.assertEqual(Path(path).name, "LightMap_FF8000.png")
            self.assertTrue(_five_points(path, (255, 128, 0, 255)))

    def test_solid_png_second_color(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        with tempfile.TemporaryDirectory() as temp_dir:
            path, hex_code = operator._apply_solid_color_rule(
                rule=_solid_rule(prefix="MaterialMap_", color=_hex_to_linear_rgba(51, 102, 204)),
                output_dir=Path(temp_dir),
            )
            self.assertEqual(hex_code, "3366CC")
            self.assertEqual(Path(path).name, "MaterialMap_3366CC.png")
            self.assertTrue(_five_points(path, (51, 102, 204, 255)))


class SharedSolidTextureTests(unittest.TestCase):
    """D1/D2/D3/D4：多物体共享、幂等、换色保留、0 物体"""

    def test_D1_multiple_objects_produce_only_two_files(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        objects = [
            _FakeObject("ObjA", [_Material("MatA"), _Material("MatA2")]),
            _FakeObject("ObjB", [_Material("MatB")]),
            _FakeObject("ObjC", [_Material("MatC")]),
        ]
        rules = [
            _solid_rule(color=_hex_to_linear_rgba(255, 128, 0)),
            _solid_rule(prefix="MaterialMap_", name="材质贴图", color=_hex_to_linear_rgba(255, 128, 0)),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result, _reports = _run(operator, rules, objects, temp_dir)
            self.assertEqual(result, {"FINISHED"})
            names = sorted(item.name for item in Path(temp_dir).glob("*.png"))
            self.assertEqual(names, ["LightMap_FF8000.png", "MaterialMap_FF8000.png"])
            material_names = ("MatA", "MatA2", "MatB", "MatC")
            self.assertFalse([name for name in names if any(item in name for item in material_names)])

    def test_D2_same_color_rerun_is_byte_identical_and_touches_mtime(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        objects = [_FakeObject("ObjA", [_Material("MatA")])]
        rules = [_solid_rule(color=_hex_to_linear_rgba(255, 128, 0))]
        with tempfile.TemporaryDirectory() as temp_dir:
            _run(operator, rules, objects, temp_dir)
            target = Path(temp_dir) / "LightMap_FF8000.png"
            first_sha, first_mtime = _sha256(target), target.stat().st_mtime_ns

            time.sleep(0.02)
            _run(operator, rules, objects, temp_dir)
            second_sha, second_mtime = _sha256(target), target.stat().st_mtime_ns

            self.assertEqual(first_sha, second_sha)
            self.assertGreater(second_mtime, first_mtime)
            self.assertEqual(len(list(Path(temp_dir).glob("*.png"))), 1)

    def test_D3_color_change_keeps_old_files(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        objects = [_FakeObject("ObjA", [_Material("MatA")])]
        rules = [
            _solid_rule(color=_hex_to_linear_rgba(255, 128, 0)),
            _solid_rule(prefix="MaterialMap_", name="材质贴图", color=_hex_to_linear_rgba(255, 128, 0)),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            _run(operator, rules, objects, temp_dir)
            for rule in rules:
                rule.solid_color = _hex_to_linear_rgba(51, 102, 204)
            _run(operator, rules, objects, temp_dir)
            names = sorted(item.name for item in Path(temp_dir).glob("*.png"))
            self.assertEqual(names, [
                "LightMap_3366CC.png",
                "LightMap_FF8000.png",
                "MaterialMap_3366CC.png",
                "MaterialMap_FF8000.png",
            ])
            self.assertTrue(_five_points(Path(temp_dir) / "LightMap_3366CC.png", (51, 102, 204, 255)))
            self.assertTrue(_five_points(Path(temp_dir) / "LightMap_FF8000.png", (255, 128, 0, 255)))

    def test_D4_no_selected_objects_still_outputs_two_files(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        rules = [
            _solid_rule(color=_hex_to_linear_rgba(255, 128, 0)),
            _solid_rule(prefix="MaterialMap_", name="材质贴图", color=_hex_to_linear_rgba(255, 128, 0)),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            result, reports = _run(operator, rules, [], temp_dir)
            self.assertEqual(result, {"FINISHED"})
            self.assertEqual([entry for entry in reports if "ERROR" in entry[0]], [])
            names = sorted(item.name for item in Path(temp_dir).glob("*.png"))
            self.assertEqual(names, ["LightMap_FF8000.png", "MaterialMap_FF8000.png"])


class SharedMaterialTests(unittest.TestCase):
    """D5：一条纯色规则只建一个共享材质，append 到每个选中物体且不重复"""

    def test_D5_one_shared_material_per_rule_appended_to_every_object(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        objects = [
            _FakeObject("ObjA", [_Material("MatA")]),
            _FakeObject("ObjB", [_Material("MatB")]),
            _FakeObject("ObjC", [_Material("MatC")]),
        ]
        rules = [
            _solid_rule(color=_hex_to_linear_rgba(255, 128, 0)),
            _solid_rule(prefix="MaterialMap_", name="材质贴图", color=_hex_to_linear_rgba(255, 128, 0)),
        ]
        created = {}
        created_seen = set()

        def fake_create(self, material_name, image_path):
            is_new = material_name not in created_seen
            created_seen.add(material_name)
            material = created.setdefault(material_name, types.SimpleNamespace(name=material_name))
            return material, is_new

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite,
                                   "_create_composite_material", new=fake_create):
                _run(operator, rules, objects, temp_dir, create_materials=True)
                for obj in objects:
                    names = [slot.material.name for slot in obj.material_slots]
                    self.assertEqual(names.count("LightMap_FF8000"), 1)
                    self.assertEqual(names.count("MaterialMap_FF8000"), 1)

                # 再跑一次：共享材质仍只有一个，不会重复 append
                _run(operator, rules, objects, temp_dir, create_materials=True)
                for obj in objects:
                    names = [slot.material.name for slot in obj.material_slots]
                    self.assertEqual(names.count("LightMap_FF8000"), 1)
                    self.assertEqual(names.count("MaterialMap_FF8000"), 1)

            self.assertEqual(sorted(created), ["LightMap_FF8000", "MaterialMap_FF8000"])


class NonSolidRuleTests(unittest.TestCase):
    """D6/C8：非纯色规则命名与上游语义不变"""

    def test_D6_non_solid_presets_keep_material_names(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        objects = [
            _FakeObject("ObjA", [_Material("MatA")]),
            _FakeObject("ObjB", [_Material("MatB")]),
        ]
        rules = []
        for preset in tt_normal_map.COMPOSITE_PRESETS[:4]:
            rules.append(types.SimpleNamespace(
                rule_name=preset["name"],
                input_source_mode="BASE_COLOR",
                input_rule_name="",
                output_name_prefix=preset["prefix"],
                output_channels=[types.SimpleNamespace(
                    source_type=channel["source_type"],
                    source_channel=channel.get("source_channel", "R"),
                    constant_value=channel.get("constant_value", 1.0),
                    invert=False,
                ) for channel in preset["channels"]],
                enabled=True,
            ))

        base_pixels = np.zeros((2, 2, 4), dtype=np.float32)
        base_pixels[:, :, 3] = 1.0

        def fake_apply(self, rule, material, base_pixels, width, height, processor, output_dir):
            safe = "".join(c for c in material.name if c.isalnum() or c in ("-", "_", ".")).rstrip()
            path = Path(output_dir) / f"{rule.output_name_prefix}{safe}.png"
            path.write_bytes(b"stub")
            return str(path), np.zeros((height, width, 4), dtype=np.float32)

        def fake_material(self, material_name, composite_image_path):
            return types.SimpleNamespace(name=material_name), True

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite,
                                   "_find_base_color_texture", return_value=object()), \
                 mock.patch.object(tt_normal_map.ChannelProcessor, "load_image_pixels",
                                   return_value=(base_pixels, 2, 2)), \
                 mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite,
                                   "_apply_composite_rule", new=fake_apply), \
                 mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite,
                                   "_create_composite_material", new=fake_material):
                result, _reports = _run(operator, rules, objects, temp_dir, create_materials=True)

            self.assertEqual(result, {"FINISHED"})
            names = sorted(item.name for item in Path(temp_dir).glob("*.png"))
            expected = sorted(
                f"{preset['prefix']}{material_name}.png"
                for preset in tt_normal_map.COMPOSITE_PRESETS[:4]
                for material_name in ("MatA", "MatB"))
            self.assertEqual(names, expected)

    def test_non_solid_rule_still_skipped_without_base_texture(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        objects = [_FakeObject("ObjD", [_Material("MatD")])]
        with tempfile.TemporaryDirectory() as temp_dir:
            result, _reports = _run(operator, [_constant_rule()], objects, temp_dir)
            self.assertEqual(result, {"FINISHED"})
            self.assertEqual(sorted(Path(temp_dir).glob("*.png")), [])

    def test_no_solid_rule_and_no_objects_still_errors(self):
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        with tempfile.TemporaryDirectory() as temp_dir:
            result, reports = _run(operator, [_constant_rule()], [], temp_dir)
            self.assertEqual(result, {"CANCELLED"})
            self.assertIn(("ERROR", "请选择至少一个网格物体"),
                          [(next(iter(level)), message) for level, message in reports])

    def test_solid_rule_is_not_upstream_for_previous_or_named_output(self):
        """C8：纯色图不进上游；PREVIOUS_OUTPUT 仍拿原图，NAMED_OUTPUT 引用纯色规则跳过不崩"""
        operator = tt_normal_map.TT_OT_execute_channel_composite()
        objects = [_FakeObject("ObjE", [_Material("MatE")])]
        solid = _solid_rule(color=_hex_to_linear_rgba(255, 128, 0))
        previous = _constant_rule(name="Prev", prefix="Prev_", input_mode="PREVIOUS_OUTPUT")
        named = _constant_rule(name="Named", prefix="Named_", input_mode="NAMED_OUTPUT",
                               input_rule_name="SolidRule")

        base_pixels = np.zeros((2, 2, 4), dtype=np.float32)
        base_pixels[:, :, 0] = 0.25
        base_pixels[:, :, 3] = 1.0
        recorded = []

        def fake_apply(self, rule, material, base_pixels, width, height, processor, output_dir):
            recorded.append((rule.rule_name, base_pixels.copy(), width, height))
            path = Path(output_dir) / f"{rule.output_name_prefix}{material.name}.png"
            path.write_bytes(b"stub")
            return str(path), base_pixels

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite,
                                   "_find_base_color_texture", return_value=object()), \
                 mock.patch.object(tt_normal_map.ChannelProcessor, "load_image_pixels",
                                   return_value=(base_pixels, 2, 2)), \
                 mock.patch.object(tt_normal_map.TT_OT_execute_channel_composite,
                                   "_apply_composite_rule", new=fake_apply):
                result, _reports = _run(operator, [solid, previous, named], objects, temp_dir)

            self.assertEqual(result, {"FINISHED"})
            self.assertEqual([entry[0] for entry in recorded], ["Prev"])
            self.assertTrue(np.allclose(recorded[0][1], base_pixels))
            self.assertEqual((recorded[0][2], recorded[0][3]), (2, 2))
            names = sorted(item.name for item in Path(temp_dir).glob("*.png"))
            self.assertEqual(names, ["LightMap_FF8000.png", "Prev_MatE.png"])


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
    """D7/C5：面板纯色规则只画一个颜色框 + 一行固定尺寸"""

    @staticmethod
    def _draw(rule):
        panel = ui_panel_toolkit.TT_ChannelCompositePanel.__new__(ui_panel_toolkit.TT_ChannelCompositePanel)
        log = []
        panel.layout = _RecordingUI(log)
        props = types.SimpleNamespace(composite_rules=[rule], normal_map_create_materials=False)
        context = types.SimpleNamespace(scene=types.SimpleNamespace(texture_tools_props=props))
        panel.draw(context)
        return log

    def test_D7_solid_rule_draws_one_color_box_and_size_label(self):
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
        self.assertEqual(labels.count("256×256（固定）"), 1)
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
