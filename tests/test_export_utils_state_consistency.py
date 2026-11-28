import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_export_utils_state_consistency_test_pkg"
for package_name in (PKG, f"{PKG}.utils", f"{PKG}.common", f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []

sys.modules[f"{PKG}.utils"].__path__ = [str(Path(__file__).resolve().parents[1] / "utils")]


class _FakeShapeKey:
    def __init__(self, name, value):
        self.name = name
        self.value = value
        self.mute = False
        self.data = []


class _FakeMesh:
    def __init__(self):
        self.polygons = []
        self.uv_layers = {}


class _FakeObject:
    def __init__(self):
        self.name = "Mesh"
        self.type = "MESH"
        self.data = types.SimpleNamespace(
            shape_keys=types.SimpleNamespace(
                key_blocks=[
                    _FakeShapeKey("Basis", 0.0),
                    _FakeShapeKey("Smile", 1.0),
                    _FakeShapeKey("Blink", 0.25),
                ]
            )
        )
        self._mesh = _FakeMesh()

    def evaluated_get(self, _depsgraph):
        return types.SimpleNamespace(to_mesh=lambda: self._mesh)


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
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace(GF2="GF2", YYSLS="YYSLS", SnowBreak="SnowBreak"))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.obj_buffer_helper", ObjBufferHelper=types.SimpleNamespace(
    parse_elementname_data_dict=lambda mesh, d3d11_game_type, vertex_group_names=None, **kwargs: {"POSITION": mesh},
))
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace(
    get_obj_by_name=lambda name: None,
    get_mesh_evaluate_from_obj=lambda obj: obj._mesh,
    mesh_triangulate=lambda mesh: None,
))


class _FakeShapeKeyUtils:
    @staticmethod
    def iter_exportable_shape_keys(obj):
        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None) or []
        for index, key_block in enumerate(key_blocks):
            if index == 0:
                continue
            if str(getattr(key_block, "name", "") or "").strip().lower() == "basis":
                continue
            yield key_block

    @classmethod
    def count_exportable_shape_keys(cls, obj):
        return sum(1 for _key_block in cls.iter_exportable_shape_keys(obj))

    @classmethod
    def has_exportable_shape_keys(cls, obj):
        return cls.count_exportable_shape_keys(obj) > 0

    @staticmethod
    def preserve_shape_key_state(obj):
        class _Ctx:
            def __enter__(self_inner):
                self_inner.state = [(kb, kb.value) for kb in obj.data.shape_keys.key_blocks]
                return self_inner.state

            def __exit__(self_inner, exc_type, exc, tb):
                for key_block, value in self_inner.state:
                    key_block.value = value
                return False

        return _Ctx()

    @staticmethod
    def reset_shapekey_values(obj):
        for key_block in obj.data.shape_keys.key_blocks:
            if key_block.name != "Basis":
                key_block.value = 0.0

    @staticmethod
    def _update_view_layer():
        return None

    @staticmethod
    def extract_shapekey_data(merged_obj, index_vertex_id_dict):
        return [1], [2], [3]


_install_module(f"{PKG}.utils.shapekey_utils", ShapeKeyUtils=_FakeShapeKeyUtils)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace(
    should_preserve_current_shapekey_mix_for_export=lambda: False,
))


module_path = Path(__file__).resolve().parents[1] / "utils" / "export_utils.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.utils.export_utils", module_path)
export_utils = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = export_utils
spec.loader.exec_module(export_utils)


class ExportUtilsStateConsistencyTests(unittest.TestCase):
    """测试导出工具中形态键值的状态一致性和构建函数"""

    def test_pose_cache_signature_accepts_overlapping_field_offsets(self):
        basis = _FakeShapeKey("Basis", 0.0)
        shape = _FakeShapeKey("Smile", 1.0)
        shape.relative_key = basis
        mesh = types.SimpleNamespace(shape_keys=types.SimpleNamespace(reference_key=basis))
        obj = types.SimpleNamespace(data=mesh)
        mods = {
            f"{PKG}.blueprint.frame_table_cache": types.SimpleNamespace(
                hash_array=lambda digest, data: digest.update(numpy.asarray(data).tobytes()),
                hash_mesh_topology=lambda digest, source: True,
            ),
            f"{PKG}.common.shapekey_native": types.SimpleNamespace(read_coords=lambda key: numpy.zeros(3)),
        }
        signatures = []
        with (
            mock.patch.dict(sys.modules, mods),
            mock.patch.object(export_utils.bpy, "app", types.SimpleNamespace(version_string="test"), create=True),
        ):
            for offsets in ([0, 0], [4, 4]):
                dtype = numpy.dtype({
                    "names": ["TEXCOORD1", "TEXCOORD2"],
                    "formats": ["<f4", "<f4"],
                    "offsets": offsets,
                    "itemsize": 8,
                })
                signature, _ = export_utils.ExportUtils._frame_table_sample_signatures(
                    obj, [[shape]], numpy.arange(1), numpy.zeros(1, dtype=dtype), 1,
                )
                signatures.append(signature)
        self.assertNotEqual(*signatures)

    def test_build_obj_element_context_restores_source_shape_key_values(self):
        """测试 build_obj_element_context 在上下文退出后恢复原始形态键值"""
        obj = _FakeObject()

        context = export_utils.ExportUtils.build_obj_element_context(
            d3d11_game_type=types.SimpleNamespace(get_total_structured_dtype=lambda: object()),
            obj=obj,
        )

        self.assertIs(context.obj, obj)
        self.assertEqual(obj.data.shape_keys.key_blocks[1].value, 1.0)
        self.assertEqual(obj.data.shape_keys.key_blocks[2].value, 0.25)

    def test_build_wwmi_shapekey_payload_ignores_basis_only_shape_keys(self):
        """测试仅含 Basis 形态键时 build_wwmi_shapekey_payload 返回空结果"""
        obj = _FakeObject()
        obj.data.shape_keys.key_blocks = [_FakeShapeKey("Basis", 0.0)]

        result = export_utils.ExportUtils.build_wwmi_shapekey_payload(
            obj=obj,
            index_vertex_id_dict={},
        )

        self.assertEqual(result, ([], [], [], False))

    def test_build_wwmi_shapekey_payload_exports_when_non_basis_shape_key_exists(self):
        """测试存在非 Basis 形态键时 build_wwmi_shapekey_payload 正常导出"""
        obj = _FakeObject()

        result = export_utils.ExportUtils.build_wwmi_shapekey_payload(
            obj=obj,
            index_vertex_id_dict={},
        )

        self.assertEqual(result, ([1], [2], [3], True))

    def test_build_wwmi_shapekey_payload_supports_batch_layout(self):
        """测试 WWMI ShapeKey 载荷支持 batch 化 offsets 布局"""
        obj = _FakeObject()
        original_extract = export_utils.ShapeKeyUtils.extract_shapekey_data
        try:
            export_utils.ShapeKeyUtils.extract_shapekey_data = lambda *_args, **_kwargs: ([0] * 128 + [0] * 128, [2], [3])
            result = export_utils.ExportUtils.build_wwmi_shapekey_payload(
                obj=obj,
                index_vertex_id_dict={},
            )
        finally:
            export_utils.ShapeKeyUtils.extract_shapekey_data = original_extract

        self.assertEqual(result, ([0] * 128 + [0] * 128, [2], [3], True))


if __name__ == "__main__":
    unittest.main()
