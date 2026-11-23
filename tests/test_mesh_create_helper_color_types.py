import importlib.util
import os
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


PKG = "_mesh_create_helper_color_types_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils", f"{PKG}.ui", f"{PKG}.ui.wwmi"):
    package = _install_module(package_name)
    package.__path__ = []

from tests import _real_modules as _real_modules
_real_modules.register_real_common_modules(f"{PKG}.common")


_install_module("bpy", data=types.SimpleNamespace(), types=types.SimpleNamespace(Collection=object))
_install_module("bpy_extras", io_utils=types.SimpleNamespace(unpack_list=lambda seq: seq, axis_conversion=lambda **_kwargs: None))
_install_module("bpy_extras.io_utils", unpack_list=lambda seq: seq, axis_conversion=lambda **_kwargs: None)
_install_module(f"{PKG}.utils.format_utils", Fatal=RuntimeError, FormatUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.mesh_utils", MeshUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.obj_utils", ObjUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.color_attribute_utils", write_color_attribute_data=lambda *_args, **_kwargs: None)
_install_module(f"{PKG}.utils.texture_utils", TextureUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(f"{PKG}.common.global_properties", GlobalProterties=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        GIMI="GIMI",
        ZZMI="ZZMI",
        IdentityV="IdentityV",
        NTEMI="NTEMI",
        SnowBreak="SnowBreak",
        WWMI="WWMI",
        Naraka="Naraka",
        EFMI="EFMI",
        YYSLS="YYSLS",
    ),
)
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.d3d11_element", D3D11Element=object)
_install_module(f"{PKG}.ui.wwmi.extracted_object", ExtractedObjectHelper=types.SimpleNamespace())


module_path = Path(__file__).resolve().parents[1] / "common" / "mesh_create_helper.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.common.mesh_create_helper", module_path)
mesh_create_helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mesh_create_helper
spec.loader.exec_module(mesh_create_helper)


class MeshCreateHelperColorTypeTests(unittest.TestCase):
    def test_unorm8_color_stays_byte_color(self):
        self.assertEqual(
            mesh_create_helper.MeshCreateHelper._get_color_attribute_type_by_format("R8G8B8A8_UNORM"),
            "BYTE_COLOR",
        )

    def test_snorm8_color_uses_float_color(self):
        self.assertEqual(
            mesh_create_helper.MeshCreateHelper._get_color_attribute_type_by_format("R8G8B8A8_SNORM"),
            "FLOAT_COLOR",
        )

    def test_unorm16_color_uses_float_color(self):
        self.assertEqual(
            mesh_create_helper.MeshCreateHelper._get_color_attribute_type_by_format("R16G16B16A16_UNORM"),
            "FLOAT_COLOR",
        )

    def test_float_color_uses_float_color(self):
        self.assertEqual(
            mesh_create_helper.MeshCreateHelper._get_color_attribute_type_by_format("R16G16_FLOAT"),
            "FLOAT_COLOR",
        )


if __name__ == "__main__":
    unittest.main()
