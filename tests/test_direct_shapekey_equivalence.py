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


PKG = "_direct_shapekey_equivalence_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

sys.modules[f"{PKG}.utils"].__path__ = [str(Path(__file__).resolve().parents[1] / "utils")]


def _link_noop(_obj):
    return None


_install_module(
    "bpy",
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(
            collection=types.SimpleNamespace(
                objects=types.SimpleNamespace(link=_link_noop),
            )
        ),
        view_layer=types.SimpleNamespace(
            update=lambda: None,
            objects=types.SimpleNamespace(active=None),
        ),
    ),
    data=types.SimpleNamespace(
        objects={},
        meshes=types.SimpleNamespace(remove=lambda *_args, **_kwargs: None),
    ),
    types=types.SimpleNamespace(Object=object, Mesh=object),
)

global_config_module = _install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="GIMI"),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(enable_non_mirror_workflow=lambda: False),
)
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
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.obj_buffer_helper", ObjBufferHelper=types.SimpleNamespace())
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace(select_obj=lambda *_args, **_kwargs: None))
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(transform_apply_preserve_shape_keys=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(get_direct_shapekey_position_records=lambda: {}),
)
_install_module(f"{PKG}.blueprint.preprocess", PreProcessHelper=types.SimpleNamespace())


def _extract_position_bytes_by_indices(base_bytes, position_stride, export_indices):
    byte_rows = np.frombuffer(base_bytes, dtype=np.uint8).reshape(-1, position_stride)
    return byte_rows[np.asarray(export_indices, dtype=np.int64)].tobytes()


_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    extract_position_bytes_by_indices=_extract_position_bytes_by_indices,
    normalize_runtime_name=lambda name: str(name or ""),
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=RuntimeError,
)


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


export_utils_module = _load_module("utils.export_utils", "utils/export_utils.py")
sampling_module = _load_module(
    "blueprint.direct_export_shapekey_sampling_mixin",
    "blueprint/direct_export_shapekey_sampling_mixin.py",
)


class _PositionElement:
    def __init__(self):
        self.Format = "R32G32B32_FLOAT"
        self.ByteWidth = 12
        self.AlignedByteOffset = 0
        self.Category = "Position"
        self.ElementName = "POSITION"


class _GameType:
    def __init__(self):
        position_element = _PositionElement()
        self.ElementNameD3D11ElementDict = {"POSITION": position_element}
        self.D3D11ElementList = [position_element]


class _Node:
    @staticmethod
    def _extract_hash_from_name(_name):
        return "part00"

    @staticmethod
    def _extract_hash_prefix(value):
        return str(value or "") or None


class _RecordedEquivalenceHarness(sampling_module.DirectShapeKeySamplingMixin):
    def __init__(self, object_context, recorded_data):
        self.node = _Node()
        self.object_context = object_context
        self.recorded_data = recorded_data
        self.merged_name_members = {}

    def _resolve_object_export_context_with_merged_members(self, _runtime_info, _obj_name):
        return self.object_context

    def _resolve_recorded_shape_key_data_from_records(self, resolved_records, shapekey_name):
        del resolved_records, shapekey_name
        coords, basis_coords, loop_vertex_indices = self.recorded_data
        return coords, basis_coords, loop_vertex_indices, "RecordedObject"


class DirectShapeKeyEquivalenceTests(unittest.TestCase):
    def test_preprocess_record_path_matches_non_direct_position_bytes_across_logics(self):
        basis_coords = np.asarray(
            [
                [1.0, 2.0, 3.0],
                [4.0, 5.0, 6.0],
            ],
            dtype=np.float32,
        )
        shape_coords = np.asarray(
            [
                [1.5, 3.0, 4.5],
                [5.5, 4.0, 8.0],
            ],
            dtype=np.float32,
        )
        loop_vertex_indices = np.asarray([0, 1], dtype=np.int32)
        game_type = _GameType()
        object_context = {
            "local_loop_indices": np.asarray([0, 1], dtype=np.int32),
            "export_indices": np.asarray([0, 1], dtype=np.int32),
            "d3d11_game_type": game_type,
        }

        for logic_name in ("GIMI", "SnowBreak", "HTMI"):
            with self.subTest(logic_name=logic_name):
                global_config_module.GlobalConfig.logic_name = logic_name
                base_bytes = export_utils_module.ExportUtils.format_position_bytes_from_coords(
                    basis_coords,
                    d3d11_game_type=game_type,
                    position_stride=12,
                    logic_name=logic_name,
                )
                expected_bytes = export_utils_module.ExportUtils.format_position_bytes_from_coords(
                    shape_coords,
                    d3d11_game_type=game_type,
                    position_stride=12,
                    logic_name=logic_name,
                )

                harness = _RecordedEquivalenceHarness(
                    object_context=object_context,
                    recorded_data=(shape_coords, basis_coords, loop_vertex_indices),
                )
                overrides = harness._build_slot_position_overrides_from_preprocess_records(
                    slot_to_name_to_objects={1: {"Smile": ["RuntimeObject"]}},
                    calculated_ranges={"RuntimeObject": (0, 1, "")},
                    runtime_infos={
                        "part00": {
                            "logical_hash": "part00",
                            "base_bytes": base_bytes,
                            "position_stride": 12,
                        }
                    },
                    source_object_map={},
                )

                direct_bytes = overrides[1]["RuntimeObject"]["position_bytes"]
                np.testing.assert_allclose(
                    np.frombuffer(direct_bytes, dtype=np.float32).reshape(-1, 3),
                    np.frombuffer(expected_bytes, dtype=np.float32).reshape(-1, 3),
                )


if __name__ == "__main__":
    unittest.main()
