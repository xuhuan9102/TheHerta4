"""Test ZZMI import worker bundled python path resolution."""

from pathlib import Path
import sys
import unittest

from common.zzmi_import_worker import bundled_python


class ZZMIImportWorkerBundledPythonTests(unittest.TestCase):
    # RED-BY: 旧代码仅检查 Path(sys.prefix) / "bin" / name，在独立 Python 环境下返回不存在的文件路径
    def test_bundled_python_exists_and_is_file(self):
        resolved = bundled_python()
        self.assertIsInstance(resolved, Path)
        self.assertTrue(resolved.is_file(), f"resolved python binary does not exist: {resolved}")


if __name__ == "__main__":
    unittest.main()
