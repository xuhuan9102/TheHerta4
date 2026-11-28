import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_submesh_aligned_offsets_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(package_name)

sys.modules[f"{PKG}.utils.json_utils"] = types.SimpleNamespace(
    JsonUtils=types.SimpleNamespace(
        LoadFromFile=lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    )
)
_load_module(f"{PKG}.common.d3d11_element", REPO_ROOT / "common" / "d3d11_element.py")
submesh_module = _load_module(
    f"{PKG}.common.submesh_json",
    REPO_ROOT / "common" / "submesh_json.py",
)


class SubmeshAlignedOffsetTests(unittest.TestCase):
    def test_category_stride_uses_offsets_and_allows_texcoord_alias(self):
        payload = {
            "GamePreset": "APMI",
            "WorkGameType": "GPU_TEST",
            "CategoryBufferList": [
                {
                    "FileName": "texcoord.buf",
                    "Type": "Normal",
                    "D3D11ElementList": [
                        {
                            "SemanticName": "TEXCOORD",
                            "SemanticIndex": 0,
                            "Format": "R32G32_FLOAT",
                            "ByteWidth": 8,
                            "AlignedByteOffset": 4,
                            "ExtractSlot": "vb1",
                            "ExtractTechnique": "trianglelist",
                            "Category": "Texcoord",
                        },
                        {
                            "SemanticName": "TEXCOORD",
                            "SemanticIndex": 1,
                            "Format": "R32G32_FLOAT",
                            "ByteWidth": 8,
                            "AlignedByteOffset": 12,
                            "ExtractSlot": "vb1",
                            "ExtractTechnique": "trianglelist",
                            "Category": "Texcoord",
                        },
                        {
                            "SemanticName": "TEXCOORD",
                            "SemanticIndex": 2,
                            "Format": "R32G32_FLOAT",
                            "ByteWidth": 8,
                            "AlignedByteOffset": 12,
                            "ExtractSlot": "vb1",
                            "ExtractTechnique": "trianglelist",
                            "Category": "Texcoord",
                        },
                        {
                            "SemanticName": "TEXCOORD",
                            "SemanticIndex": 3,
                            "Format": "R32G32_FLOAT",
                            "ByteWidth": 8,
                            "AlignedByteOffset": None,
                            "ExtractSlot": "vb1",
                            "ExtractTechnique": "trianglelist",
                            "Category": "Texcoord",
                        },
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            submesh = submesh_module.SubmeshJson(str(path))

        category = submesh.CategoryBufferList[0]
        self.assertEqual(category.Stride, 28)
        self.assertEqual(
            [element.AlignedByteOffset for element in category.D3D11ElementList],
            [4, 12, 12, 20],
        )


if __name__ == "__main__":
    unittest.main()
