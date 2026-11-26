import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_export_space_transform_test_pkg"
for package_name in (PKG, f"{PKG}.utils", f"{PKG}.common", f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []

sys.modules[f"{PKG}.utils"].__path__ = [str(Path(__file__).resolve().parents[1] / "utils")]


_install_module(
    "bpy",
    context=types.SimpleNamespace(
        evaluated_depsgraph_get=lambda: object(),
        view_layer=types.SimpleNamespace(update=lambda: None),
    ),
    data=types.SimpleNamespace(objects={}),
    types=types.SimpleNamespace(Object=object, Mesh=object),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        SRMI="SRMI",
        GIMI="GIMI",
        HIMI="HIMI",
        YYSLS="YYSLS",
        IdentityV="IdentityV",
        SnowBreak="SnowBreak",
        EFMI="EFMI",
        HTMI="HTMI",
    ),
)
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.obj_buffer_helper", ObjBufferHelper=types.SimpleNamespace())
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace(select_obj=lambda *_args, **_kwargs: None))
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(transform_apply_preserve_shape_keys=lambda *_args, **_kwargs: None),
)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace(
    should_preserve_current_shapekey_mix_for_export=lambda: False,
    get_current_shapekeyname_mkey_dict=lambda: {},
    get_runtime_shapekey_buffer_names=lambda *_args, **_kwargs: [],
))


module_path = Path(__file__).resolve().parents[1] / "utils" / "export_utils.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.utils.export_utils", module_path)
export_utils = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = export_utils
spec.loader.exec_module(export_utils)


class _PositionElement:
    def __init__(self, fmt="R32G32B32_FLOAT", byte_width=12, aligned_byte_offset=0):
        self.Category = "Position"
        self.ElementName = "POSITION"
        self.Format = fmt
        self.ByteWidth = byte_width
        self.AlignedByteOffset = aligned_byte_offset


class _GameType:
    def __init__(self, position_element=None):
        self.ElementNameD3D11ElementDict = {"POSITION": position_element or _PositionElement()}
        self.D3D11ElementList = [self.ElementNameD3D11ElementDict["POSITION"]]


class ExportSpaceTransformTests(unittest.TestCase):
    def test_convert_position_coords_uses_global_logic_when_not_explicit(self):
        coords = np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)

        converted = export_utils.ExportUtils.convert_position_coords_for_export(coords)

        np.testing.assert_allclose(converted, np.asarray([[1.0, 3.0, -2.0]], dtype=np.float32))

    def test_convert_position_coords_for_export_rotates_gimi_like_logics(self):
        coords = np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)

        converted = export_utils.ExportUtils.convert_position_coords_for_export(coords, logic_name="GIMI")

        np.testing.assert_allclose(converted, np.asarray([[1.0, 3.0, -2.0]], dtype=np.float32))

    def test_export_matrix_maps_columbina_empty_into_exported_bounds(self):
        empty_center = np.asarray([[0.069, -0.139, 1.143]], dtype=np.float32)

        converted = export_utils.ExportUtils.convert_position_coords_for_export(
            empty_center,
            logic_name="GIMI",
        )

        np.testing.assert_allclose(converted, np.asarray([[0.069, 1.143, 0.139]], dtype=np.float32))

    def test_all_game_logic_coordinate_families(self):
        coords = np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)
        for logic_name in ("APMI", "SRMI", "GIMI", "HIMI", "YYSLS", "IdentityV"):
            with self.subTest(logic_name=logic_name):
                np.testing.assert_allclose(
                    export_utils.ExportUtils.convert_position_coords_for_export(coords, logic_name=logic_name),
                    [[1.0, 3.0, -2.0]],
                )
        for logic_name in (
            "ZZMI", "ZZMIDX12", "WWMI", "EFMI", "HTMI", "GF2", "AILIMIT",
            "DOAV", "Naraka", "NarakaM", "NTEMI", "NEMI",
        ):
            with self.subTest(logic_name=logic_name):
                np.testing.assert_allclose(
                    export_utils.ExportUtils.convert_position_coords_for_export(coords, logic_name=logic_name),
                    coords,
                )

    def test_convert_position_coords_for_export_scales_and_rotates_snowbreak(self):
        coords = np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)

        converted = export_utils.ExportUtils.convert_position_coords_for_export(coords, logic_name="SnowBreak")

        np.testing.assert_allclose(converted, np.asarray([[-100.0, -200.0, 300.0]], dtype=np.float32))

    def test_htmi_uses_efmi_effective_logic_name(self):
        coords = np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32)

        converted_htmi = export_utils.ExportUtils.convert_position_coords_for_export(coords, logic_name="HTMI")
        converted_efmi = export_utils.ExportUtils.convert_position_coords_for_export(coords, logic_name="EFMI")

        np.testing.assert_allclose(converted_htmi, converted_efmi)

    def test_convert_position_buffer_bytes_for_export_rewrites_float3_positions(self):
        coords = np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
        game_type = _GameType(_PositionElement(fmt="R32G32B32_FLOAT", byte_width=12))

        converted = export_utils.ExportUtils.convert_position_buffer_bytes_for_export(
            coords.tobytes(),
            d3d11_game_type=game_type,
            position_stride=12,
            logic_name="GIMI",
        )

        np.testing.assert_allclose(
            np.frombuffer(converted, dtype=np.float32).reshape(-1, 3),
            np.asarray([[1.0, 3.0, -2.0], [4.0, 6.0, -5.0]], dtype=np.float32),
        )


if __name__ == "__main__":
    unittest.main()
