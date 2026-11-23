import importlib.util
import sys
import types
import unittest
import tempfile
from unittest import mock
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_preprocess_cache_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

from tests import _real_modules as _real_modules
_real_modules.register_real_common_modules(f"{PKG}.common")


class _FakeObjectStore(dict):
    def get(self, key, default=None):
        return super().get(key, default)


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(
        objects=_FakeObjectStore(),
        filepath="C:/tmp/test.blend",
    ),
)
_install_module("bpy", **_fake_bpy.__dict__)

_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        enable_non_mirror_workflow=lambda: False,
        apply_all_modifiers=lambda: False,
    ),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    ),
)
_install_module(
    f"{PKG}.utils.color_attribute_utils",
    read_color_attribute_data=lambda color_attr, count=None: np.asarray(color_attr.data.records, dtype=np.float32),
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(resolve_source_object_name=lambda name: name),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "preprocess_cache.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.preprocess_cache", module_path)
preprocess_cache = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preprocess_cache
spec.loader.exec_module(preprocess_cache)

PreProcessCache = preprocess_cache.PreProcessCache


class _ForeachData:
    def __init__(self, field_name: str, records):
        self.field_name = field_name
        self.records = [tuple(record) for record in records]

    def foreach_get(self, field_name: str, output):
        if field_name != self.field_name:
            raise AssertionError(f"unexpected foreach_get field: {field_name}")
        flattened = np.asarray(self.records, dtype=np.float32).reshape(-1)
        output[:] = flattened


class _Vertices:
    def __init__(self, coords):
        self._coords = [tuple(coord) for coord in coords]

    def __len__(self):
        return len(self._coords)

    def foreach_get(self, field_name: str, output):
        if field_name != "co":
            raise AssertionError(f"unexpected vertex field: {field_name}")
        output[:] = np.asarray(self._coords, dtype=np.float32).reshape(-1)


class _Loops:
    def __init__(self, count: int):
        self._count = count

    def __len__(self):
        return self._count


class _UVLayer:
    def __init__(self, name: str, uvs):
        self.name = name
        self.data = _ForeachData("uv", uvs)


class _ColorAttribute:
    def __init__(self, name: str, colors):
        self.name = name
        self.data = _ForeachData("color", colors)


class _LegacyVertexColorLayer(_ColorAttribute):
    pass


class _Mesh:
    def __init__(self, coords, uvs, color_attributes, vertex_colors=None):
        self.vertices = _Vertices(coords)
        self.loops = _Loops(len(uvs))
        self.uv_layers = [_UVLayer("TEXCOORD.xy", uvs)]
        self.color_attributes = list(color_attributes)
        self.vertex_colors = list(vertex_colors or [])
        self.shape_keys = None


class _Object:
    def __init__(self, name: str, mesh: _Mesh):
        self.name = name
        self.type = "MESH"
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)
        self.data = mesh
        self.modifiers = []
        self.vertex_groups = []
        self.constraints = []


class PreProcessCacheHashTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.objects.clear()

    def test_nested_cache_stats_and_clear(self):
        with tempfile.TemporaryDirectory(prefix='herta_preprocess_clear_') as temp:
            root=Path(temp)
            (root/'nested').mkdir()
            (root/'object.blend').write_bytes(b'cached')
            (root/'nested'/'object.json').write_bytes(b'metadata')
            with mock.patch.object(PreProcessCache,'get_cache_dir',return_value=str(root)):
                self.assertEqual(PreProcessCache.get_cache_stats(),{'file_count':2,'total_size':14})
                self.assertEqual(PreProcessCache.clear_cache(),2)
                self.assertEqual(PreProcessCache.get_cache_stats(),{'file_count':0,'total_size':0})

    def test_ntmi_custom_color_attribute_is_ignored_in_hash(self):
        mesh = _Mesh(
            coords=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            uvs=[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
            color_attributes=[
                _ColorAttribute(
                    "COLOR",
                    [(0.1, 0.2, 0.3, 1.0), (0.2, 0.3, 0.4, 1.0), (0.3, 0.4, 0.5, 1.0)],
                ),
                _ColorAttribute(
                    "NTMI_OutlineParam",
                    [(0.0, 0.0, 0.0, 1.0), (0.1, 0.1, 0.1, 1.0), (0.2, 0.2, 0.2, 1.0)],
                ),
            ],
        )
        _fake_bpy.data.objects["Body"] = _Object("Body", mesh)

        original_hash = PreProcessCache.compute_object_hash("Body")

        mesh.color_attributes[1] = _ColorAttribute(
            "NTMI_OutlineParam",
            [(1.0, 0.0, 0.0, 1.0), (0.0, 1.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0)],
        )
        updated_hash = PreProcessCache.compute_object_hash("Body")

        self.assertEqual(updated_hash, original_hash)

    def test_export_color_attribute_still_affects_hash(self):
        mesh = _Mesh(
            coords=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            uvs=[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
            color_attributes=[
                _ColorAttribute(
                    "COLOR",
                    [(0.1, 0.2, 0.3, 1.0), (0.2, 0.3, 0.4, 1.0), (0.3, 0.4, 0.5, 1.0)],
                ),
                _ColorAttribute(
                    "NTMI_OutlineParam",
                    [(0.0, 0.0, 0.0, 1.0), (0.1, 0.1, 0.1, 1.0), (0.2, 0.2, 0.2, 1.0)],
                ),
            ],
        )
        _fake_bpy.data.objects["Body"] = _Object("Body", mesh)

        original_hash = PreProcessCache.compute_object_hash("Body")

        mesh.color_attributes[0] = _ColorAttribute(
            "COLOR",
            [(0.9, 0.2, 0.3, 1.0), (0.2, 0.8, 0.4, 1.0), (0.3, 0.4, 0.7, 1.0)],
        )
        updated_hash = PreProcessCache.compute_object_hash("Body")

        self.assertNotEqual(updated_hash, original_hash)

    def test_legacy_vertex_colors_still_affect_hash(self):
        mesh = _Mesh(
            coords=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            uvs=[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
            color_attributes=[],
            vertex_colors=[
                _LegacyVertexColorLayer(
                    "COLOR",
                    [(0.1, 0.2, 0.3, 1.0), (0.2, 0.3, 0.4, 1.0), (0.3, 0.4, 0.5, 1.0)],
                )
            ],
        )
        _fake_bpy.data.objects["Body"] = _Object("Body", mesh)

        original_hash = PreProcessCache.compute_object_hash("Body")

        mesh.vertex_colors[0] = _LegacyVertexColorLayer(
            "COLOR",
            [(0.9, 0.2, 0.3, 1.0), (0.2, 0.8, 0.4, 1.0), (0.3, 0.4, 0.7, 1.0)],
        )
        updated_hash = PreProcessCache.compute_object_hash("Body")

        self.assertNotEqual(updated_hash, original_hash)


if __name__ == "__main__":
    unittest.main()
