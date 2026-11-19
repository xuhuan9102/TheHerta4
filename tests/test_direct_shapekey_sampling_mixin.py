import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_direct_shapekey_sampling_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

sys.modules.setdefault(
    "bpy",
    types.SimpleNamespace(
        data=types.SimpleNamespace(objects={}),
        types=types.SimpleNamespace(Object=object),
    ),
)

_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(enable_non_mirror_workflow=lambda: False),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(SRMI="SRMI", GIMI="GIMI", HIMI="HIMI", YYSLS="YYSLS", IdentityV="IdentityV", SnowBreak="SnowBreak", EFMI="EFMI"),
)
_install_module(f"{PKG}.utils.log_utils", LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None))
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace(select_obj=lambda *_args, **_kwargs: None))
_install_module(f"{PKG}.utils.shapekey_utils", ShapeKeyUtils=types.SimpleNamespace(transform_apply_preserve_shape_keys=lambda *_args, **_kwargs: None))
_install_module(f"{PKG}.utils.export_utils", ExportUtils=types.SimpleNamespace())


class _FakeBlueprintExportHelper:
    records = {}

    @staticmethod
    def get_direct_shapekey_position_records():
        return _FakeBlueprintExportHelper.records


_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=_FakeBlueprintExportHelper)
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace())


def _extract_position_bytes_by_indices(base_bytes, position_stride, export_indices):
    return np.frombuffer(base_bytes, dtype=np.uint8).reshape(-1, position_stride)[np.asarray(export_indices, dtype=np.int64)].tobytes()


_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    extract_position_bytes_by_indices=_extract_position_bytes_by_indices,
    normalize_runtime_name=lambda name: name[:-5] if str(name).endswith("_copy") else str(name or ""),
)


class _ShapeKeyDirectExportError(RuntimeError):
    pass


_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=_ShapeKeyDirectExportError,
_buffer_to_bytes=lambda data: b"" if data is None else (data.tobytes() if hasattr(data, "tobytes") else bytes(data)),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_sampling_mixin.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_sampling_mixin",
    module_path,
)
sampling_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sampling_module
spec.loader.exec_module(sampling_module)


class _PositionElement:
    Format = "R32G32B32_FLOAT"
    ByteWidth = 12
    AlignedByteOffset = 0
    Category = "Position"
    ElementName = "POSITION"


class _GameType:
    D3D11ElementList = [_PositionElement()]


class _Node:
    @staticmethod
    def _extract_hash_from_name(_name):
        return "part00"

    @staticmethod
    def _extract_hash_prefix(value):
        return str(value or "") or None


class _SamplingHarness(sampling_module.DirectShapeKeySamplingMixin):
    """测试桩：实现 DirectShapeKeySamplingMixin 的抽象方法"""

    def __init__(self, object_context):
        self.node = _Node()
        self.object_context = object_context
        self.merged_name_members = {}
        self.converted_deltas = []

    def _resolve_object_export_context_with_merged_members(self, _runtime_info, _obj_name):
        return self.object_context

    def _iter_record_candidate_names(self, _obj_name, _object_context, _source_object_map):
        return ["RecordedObject"]

    def _convert_position_deltas_for_export(self, sampled_deltas):
        converted = np.asarray(sampled_deltas, dtype=np.float32) * np.asarray([10.0, 20.0, 30.0], dtype=np.float32)
        self.converted_deltas.append(converted)
        return converted


class DirectShapeKeySamplingMixinTests(unittest.TestCase):
    """测试直接导出形态键采样 Mixin 的位置覆盖计算逻辑"""

    def test_preprocess_record_path_rebuilds_target_from_base_plus_converted_basis_delta(self):
        """测试基于预处理记录重建目标位置：基础位置 + 转换后的偏移量"""
        base_positions = np.asarray(
            [
                [100.0, 200.0, 300.0],
                [400.0, 500.0, 600.0],
            ],
            dtype=np.float32,
        )
        basis_coords = np.asarray(
            [
                [1.0, 2.0, 3.0],
                [10.0, 20.0, 30.0],
            ],
            dtype=np.float32,
        )
        shape_coords = np.asarray(
            [
                [2.0, 4.0, 6.0],
                [11.0, 19.0, 32.0],
            ],
            dtype=np.float32,
        )
        _FakeBlueprintExportHelper.records = {
            "RecordedObject": {
                "basis_coords": basis_coords,
                "loop_vertex_indices": np.asarray([0, 1], dtype=np.int32),
                "shape_keys": {"Smile": shape_coords},
            }
        }

        harness = _SamplingHarness(
            {
                "local_loop_indices": np.asarray([0, 1], dtype=np.int32),
                "export_indices": np.asarray([0, 1], dtype=np.int32),
                "d3d11_game_type": _GameType(),
            }
        )

        overrides = harness._build_slot_position_overrides_from_preprocess_records(
            slot_to_name_to_objects={1: {"Smile": ["RuntimeObject"]}},
            calculated_ranges={"RuntimeObject": (0, 1, "")},
            runtime_infos={
                "part00": {
                    "logical_hash": "part00",
                    "base_bytes": base_positions.tobytes(),
                    "position_stride": 12,
                }
            },
            source_object_map={},
        )

        position_bytes = overrides[1]["RuntimeObject"]["position_bytes"]
        target_positions = np.frombuffer(position_bytes, dtype=np.float32).reshape(-1, 3)

        expected_delta = (shape_coords - basis_coords) * np.asarray([10.0, 20.0, 30.0], dtype=np.float32)
        np.testing.assert_allclose(target_positions, base_positions + expected_delta)
        np.testing.assert_allclose(harness.converted_deltas[0], expected_delta)


if __name__ == "__main__":
    unittest.main()
