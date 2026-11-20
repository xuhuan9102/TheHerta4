"""ZZMI 原生流光/UV2 投影的纯 Python 测试（不引真 bpy）。

按仓库既有范式：空 ``__path__`` 假包 + ``tests/_real_modules.py`` 登记真实
``common/zzmi_native_flow.py``，再直接用它的函数做数值断言。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy

from tests import _real_modules


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_zzmi_native_flow_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_real_modules.register_real_common_modules(f"{PKG}.common")

_spec = importlib.util.spec_from_file_location(
    f"{PKG}.common.zzmi_native_flow",
    REPO_ROOT / "common" / "zzmi_native_flow.py",
)
flow = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = flow
_spec.loader.exec_module(flow)


def _element(category, name, fmt, width):
    return types.SimpleNamespace(
        Category=category, ElementName=name, Format=fmt, ByteWidth=width,
    )


def _game_type(elements, strides):
    return types.SimpleNamespace(
        D3D11ElementList=elements, CategoryStrideDict=strides,
    )


def _position_buffers(values):
    """values: list[(x, y, z)] → Position 缓冲 + game_type。"""
    data = numpy.asarray(values, dtype="<f4")
    buffers = {"Position": numpy.frombuffer(data.tobytes(), dtype=numpy.uint8)}
    game_type = _game_type(
        [_element("Position", "POSITION", "R32G32B32_FLOAT", 12)],
        {"Position": 12},
    )
    return buffers, game_type


def _uv_buffer(count):
    raw = numpy.zeros(count * 4, dtype=numpy.uint8)
    uv = numpy.ndarray((count, 2), dtype="<f2", buffer=raw, strides=(4, 2))
    return uv, {"Texcoord": raw}


def _uv_game_type():
    return _game_type(
        [_element("Texcoord", "TEXCOORD2", "R16G16_FLOAT", 4)],
        {"Texcoord": 4},
    )


class ElementViewTests(unittest.TestCase):
    def test_half_precision_two_components_returns_view(self):
        uv, buffers = _uv_buffer(4)
        buffers["Texcoord"][0:4] = numpy.asarray([0.25, -0.5], dtype="<f2").view(numpy.uint8)
        view = flow._element_view(buffers, _uv_game_type(), "TEXCOORD2", 2)
        self.assertIsNotNone(view, "R16G16_FLOAT(ByteWidth=4) 必须能读出 2 个 half")
        self.assertEqual(view.shape, (4, 2))
        self.assertEqual(view.dtype, numpy.dtype("<f2"))
        self.assertAlmostEqual(float(view[0, 0]), 0.25)

    def test_stride_mismatch_raises_named_error(self):
        buffers, _game = _position_buffers([(0.0, 0.0, 0.0)])
        buffers["Position"] = numpy.frombuffer(b"\x00" * 10, dtype=numpy.uint8)
        with self.assertRaises(ValueError) as ctx:
            flow._element_view(buffers, _game_type(
                [_element("Position", "POSITION", "R32G32B32_FLOAT", 12)],
                {"Position": 12},
            ), "POSITION", 3)
        message = str(ctx.exception)
        self.assertIn("Position", message)
        self.assertIn("12", message)

    def test_unknown_format_or_missing_buffer_returns_none(self):
        buffers, _game = _position_buffers([(0.0, 0.0, 0.0)])
        unknown = _game_type(
            [_element("Position", "POSITION", "R8G8B8A8_UNORM", 4)],
            {"Position": 4},
        )
        self.assertIsNone(flow._element_view(buffers, unknown, "POSITION", 3))
        self.assertIsNone(flow._element_view({}, _uv_game_type(), "TEXCOORD2", 2))


class PrepareNativeFlowTests(unittest.TestCase):
    def test_without_position_returns_none(self):
        uv, buffers = _uv_buffer(3)
        model = types.SimpleNamespace(
            category_buffer_dict=buffers,
            d3d11GameType=_uv_game_type(),
            submesh_model_list=[types.SimpleNamespace(
                category_buffer_dict=buffers,
                d3d11_game_type=_uv_game_type(),
                drawcall_model_list=[],
            )],
        )
        self.assertIsNone(flow.prepare_native_flow([model], False))

    def test_flat_height_returns_none(self):
        buffers, game_type = _position_buffers([
            (0.0, 0.0, 5.0), (1.0, 0.0, 5.0), (0.0, 1.0, 5.0),
        ])
        model = types.SimpleNamespace(
            category_buffer_dict=buffers,
            d3d11GameType=game_type,
            submesh_model_list=[types.SimpleNamespace(
                category_buffer_dict=buffers,
                d3d11_game_type=game_type,
                drawcall_model_list=[],
            )],
        )
        self.assertIsNone(flow.prepare_native_flow([model], False))

    def test_without_reproject_keeps_uv_bytes(self):
        positions, pos_type = _position_buffers([
            (0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 4.0),
        ])
        uv, uv_buffers = _uv_buffer(3)
        positions.update(uv_buffers)
        game_type = _game_type(
            [_element("Position", "POSITION", "R32G32B32_FLOAT", 12),
             _element("Texcoord", "TEXCOORD2", "R16G16_FLOAT", 4)],
            {"Position": 12, "Texcoord": 4},
        )
        before = uv.tobytes()
        model = types.SimpleNamespace(
            category_buffer_dict=positions,
            d3d11GameType=game_type,
            submesh_model_list=[types.SimpleNamespace(
                category_buffer_dict=positions,
                d3d11_game_type=game_type,
                drawcall_model_list=[],
            )],
        )
        height = flow.prepare_native_flow([model], False)
        self.assertIsNotNone(height)
        self.assertEqual(uv.tobytes(), before)

    def test_reproject_flips_v_and_is_idempotent(self):
        positions, pos_type = _position_buffers([
            (0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 4.0),
        ])
        uv, uv_buffers = _uv_buffer(3)
        buffers = dict(positions)
        buffers.update(uv_buffers)
        game_type = _game_type(
            [_element("Position", "POSITION", "R32G32B32_FLOAT", 12),
             _element("Texcoord", "TEXCOORD2", "R16G16_FLOAT", 4)],
            {"Position": 12, "Texcoord": 4},
        )
        shared_submesh = types.SimpleNamespace(
            category_buffer_dict=buffers,
            d3d11_game_type=game_type,
            drawcall_model_list=[],
        )
        model = types.SimpleNamespace(
            category_buffer_dict=buffers,
            d3d11GameType=game_type,
            submesh_model_list=[shared_submesh, shared_submesh],
        )
        flow.prepare_native_flow([model], True)
        first = uv.tobytes()
        flow.prepare_native_flow([model], True)
        self.assertEqual(uv.tobytes(), first, "共享缓冲重复投影必须幂等")
        self.assertTrue(bool(((uv >= 0.0) & (uv <= 1.0)).all()))
        # 顶点 (0,0,0)：x/z 范围 [0,2]/[0,4]，center=(1,2)，size=4
        # u = (0-1)/4+0.5 = 0.25；v = -((0-2)/4)+0.5 = 1.0
        self.assertAlmostEqual(float(uv[0, 0]), 0.25, places=3)
        self.assertAlmostEqual(float(uv[0, 1]), 1.0, places=3)


class FlowScopeTests(unittest.TestCase):
    def test_setup_and_restore_text(self):
        setup, restore = flow.flow_draw_scope((0.25, -1.5))
        self.assertEqual(setup, (
            "local $zzmi_flow_saved_x = x499",
            "local $zzmi_flow_saved_y = y499",
            "local $zzmi_flow_saved_z = z499",
            "pre x499 = 7312401",
            "pre y499 = 0.25",
            "pre z499 = -1.5",
        ))
        self.assertEqual(restore[0], ";MARK:ZZMINativeFlowRestore")
        self.assertEqual(restore[1:], (
            "pre x499 = $zzmi_flow_saved_x",
            "pre y499 = $zzmi_flow_saved_y",
            "pre z499 = $zzmi_flow_saved_z",
        ))

    def test_scale_bias_use_nine_significant_digits(self):
        setup, _restore = flow.flow_draw_scope((1.0 / 3.0, -0.1234567890123))
        self.assertEqual(setup[4], "pre y499 = 0.333333333")
        self.assertEqual(setup[5], "pre z499 = -0.123456789")


class FlowShaderSectionsTests(unittest.TestCase):
    def test_anchors(self):
        lines = flow.flow_shader_sections("abc123")
        text = "\n".join(lines)
        self.assertIn("[ShaderRegexTheHertaZZMINativeHeightFlow_abc123]", text)
        self.assertIn(
            "dcl_resource_texture1d (float,float,float,float) t120", text
        )
        self.assertIn("l(7312401.0)", text)

    def test_primary_material_uv_override_clears_secondary_selector(self):
        text = "\n".join(flow.flow_shader_sections("abc123"))
        self.assertIn("[ShaderRegexTheHertaZZMIPrimaryMaterialUV_abc123]", text)
        self.assertIn("eq r0.w, r0.w, l(7312401.0)", text)
        self.assertIn("mov r0.yz, l(0.0, 0.0, 0.0, 0.0)", text)
        self.assertIn("${sample_uv}", text)


if __name__ == "__main__":
    unittest.main()
