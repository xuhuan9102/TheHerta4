"""直出形态键 Position 步长推断：工作空间优先回归测试。

回归背景：用户配置了 16 字节的顶点属性定义节点后，9 个 16 字节 IB 正常、
1 个 40 字节 IB 被统一按 16 字节写出导致游戏内顶点爆炸。修复后按 IB 从
工作空间（DrawIB 模型的 d3d11GameType，源自 SubmeshJson）取真实步长，
用户手填的顶点属性定义仅作回退。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_direct_shapekey_stride_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    "bpy",
    types=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=RuntimeError,
    _buffer_to_bytes=lambda buf: bytes(buf),
)

_runtime_utils_state = {"vertex_count": 0}
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    get_model_vertex_count=lambda _model: _runtime_utils_state["vertex_count"],
    iter_drawib_models=lambda _exporter: [],
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_runtime_mixin.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.direct_export_shapekey_runtime_mixin", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class _FakeGameType:
    def __init__(self, position_stride):
        self.CategoryStrideDict = {"Position": position_stride}


class _FakeDrawIBModel:
    def __init__(self, position_stride=0, draw_ib="aaaa0000"):
        self.draw_ib = draw_ib
        if position_stride:
            self.d3d11_game_type = _FakeGameType(position_stride)
            self.d3d11GameType = self.d3d11_game_type
        else:
            self.d3d11_game_type = None
            self.d3d11GameType = None


class _Harness(module.DirectShapeKeyRuntimeMixin):
    def __init__(self, configured_stride=0):
        self._configured_stride = configured_stride

    def _get_configured_vertex_stride(self):
        return self._configured_stride


class DirectShapeKeyStrideInferenceTests(unittest.TestCase):
    def setUp(self):
        _runtime_utils_state["vertex_count"] = 0

    def test_workspace_stride_wins_over_conflicting_configured_stride(self):
        """核心回归：40B 工作空间 IB + 用户手填 16B 定义 -> 采用工作空间 40B。"""
        harness = _Harness(configured_stride=16)
        model = _FakeDrawIBModel(position_stride=40)
        base_bytes = bytes(40 * 3)
        self.assertEqual(harness._infer_position_stride(model, base_bytes), 40)

    def test_workspace_stride_used_when_no_configured_stride(self):
        harness = _Harness(configured_stride=0)
        model = _FakeDrawIBModel(position_stride=16)
        base_bytes = bytes(16 * 5)
        self.assertEqual(harness._infer_position_stride(model, base_bytes), 16)

    def test_configured_stride_fallback_when_workspace_absent(self):
        harness = _Harness(configured_stride=16)
        model = _FakeDrawIBModel(position_stride=0)
        base_bytes = bytes(16 * 5)
        self.assertEqual(harness._infer_position_stride(model, base_bytes), 16)

    def test_workspace_stride_not_divisible_falls_through(self):
        """工作空间步长与缓冲大小不整除时回退，不静默错位。"""
        harness = _Harness(configured_stride=16)
        model = _FakeDrawIBModel(position_stride=32)  # 80 % 32 != 0
        base_bytes = bytes(80)  # 80 % 16 == 0
        self.assertEqual(harness._infer_position_stride(model, base_bytes), 16)

    def test_vertex_count_inference_fallback(self):
        harness = _Harness(configured_stride=0)
        model = _FakeDrawIBModel(position_stride=0)
        _runtime_utils_state["vertex_count"] = 5
        base_bytes = bytes(100)  # 100 / 5 = 20
        self.assertEqual(harness._infer_position_stride(model, base_bytes), 20)

    def test_error_when_nothing_resolves(self):
        harness = _Harness(configured_stride=0)
        model = _FakeDrawIBModel(position_stride=0)
        with self.assertRaises(RuntimeError):
            harness._infer_position_stride(model, bytes(26))  # 26 无法整除 12/16


if __name__ == "__main__":
    unittest.main()
