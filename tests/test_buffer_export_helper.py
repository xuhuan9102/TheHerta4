"""BufferExportHelper 写索引缓冲的路径回归。"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


PKG = "_buffer_export_helper_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeGlobalConfig:
    buffer_folder = ""

    @classmethod
    def path_generatemod_buffer_folder(cls):
        return cls.buffer_folder


_install_module(f"{PKG}.common.global_config", GlobalConfig=_FakeGlobalConfig)

module_path = Path(__file__).resolve().parents[1] / "common" / "buffer_export_helper.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.common.buffer_export_helper", module_path
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
BufferExportHelper = module.BufferExportHelper


class BufferExportHelperPathTests(unittest.TestCase):
    def test_relative_full_path_is_not_joined_to_buffer_folder_twice(self):
        previous_cwd = os.getcwd()
        directory = tempfile.mkdtemp(prefix="buffer_helper_")
        self.addCleanup(os.chdir, previous_cwd)
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        os.chdir(directory)
        os.mkdir("Meshes")

        _FakeGlobalConfig.buffer_folder = os.path.join("Meshes", "")
        target_path = os.path.join("Meshes", "LOD0.test-Index.buf")

        BufferExportHelper.write_buf_ib_r32_uint([1, 2, 3], target_path)

        self.assertTrue(os.path.isfile(target_path))
        self.assertFalse(os.path.exists(os.path.join("Meshes", "Meshes")))


if __name__ == "__main__":
    unittest.main()
