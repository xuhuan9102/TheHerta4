import importlib.util
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_d3d11_gametype_blendindices_test_pkg"


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

sys.modules[f"{PKG}.utils.format_utils"] = types.SimpleNamespace(
    FormatUtils=types.SimpleNamespace()
)
_load_module(f"{PKG}.common.d3d11_element", REPO_ROOT / "common" / "d3d11_element.py")
gametype_module = _load_module(
    f"{PKG}.common.d3d11_gametype", REPO_ROOT / "common" / "d3d11_gametype.py"
)
D3D11GameType = gametype_module.D3D11GameType


def _element(semantic, semantic_index, fmt, width):
    return {
        "SemanticName": semantic,
        "SemanticIndex": semantic_index,
        "Format": fmt,
        "ByteWidth": width,
        "ExtractSlot": "vb2",
        "ExtractTechnique": "trianglelist",
        "Category": "Blend",
    }


class D3D11GameTypeBlendIndicesTests(unittest.TestCase):
    def _make(self, elements):
        return D3D11GameType.from_submesh_json_dict({
            "WorkGameType": "TEST",
            "CategoryBufferList": [{"D3D11ElementList": elements}],
        })

    def test_widens_every_narrow_blendindices_semantic(self):
        game_type = self._make([
            _element("BLENDINDICES", 0, "R8G8B8A8_UINT", 4),
            _element("BLENDINDICES", 1, "R8G8B8A8_UINT", 4),
        ])
        self.assertTrue(game_type.widen_blendindices())
        blend_elements = [
            element for element in game_type.D3D11ElementList
            if element.SemanticName == "BLENDINDICES"
        ]
        self.assertEqual(
            [element.Format for element in blend_elements],
            ["R16G16B16A16_UINT", "R16G16B16A16_UINT"],
        )
        self.assertEqual(game_type.CategoryStrideDict["Blend"], 16)

    def test_downcasts_r32_blendindices_to_r16_while_widening_other_semantics(self):
        game_type = self._make([
            _element("BLENDINDICES", 0, "R32G32B32A32_UINT", 16),
            _element("BLENDINDICES", 1, "R8G8B8A8_UINT", 4),
        ])
        self.assertTrue(game_type.widen_blendindices())
        layouts = game_type.get_blendindices_layouts()
        self.assertEqual(layouts, [
            (0, "R16G16B16A16_UINT", "vb2"),
            (1, "R16G16B16A16_UINT", "vb2"),
        ])
        self.assertEqual(game_type.CategoryStrideDict["Blend"], 16)

    def test_downcasts_single_channel_r32_uint(self):
        game_type = self._make([
            _element("BLENDINDICES", 0, "R32_UINT", 4),
        ])
        self.assertTrue(game_type.widen_blendindices())
        self.assertEqual(game_type.get_blendindices_layouts(), [
            (0, "R16_UINT", "vb2"),
        ])
        self.assertEqual(game_type.CategoryStrideDict["Blend"], 2)

    def test_downcasts_r32_sint_preserving_signedness(self):
        game_type = self._make([
            _element("BLENDINDICES", 0, "R32G32B32A32_SINT", 16),
        ])
        self.assertTrue(game_type.widen_blendindices())
        self.assertEqual(game_type.get_blendindices_layouts(), [
            (0, "R16G16B16A16_SINT", "vb2"),
        ])
        self.assertEqual(game_type.CategoryStrideDict["Blend"], 8)

    def test_unifies_r16_and_r32_layouts_for_same_lod(self):
        """回归：同一 LOD 混用 R16/R32 BLENDINDICES（旧"布局不一致"直出失败场景）——
        两侧经 widen 归一化后布局完全一致，EFMI 合并骨架校验可通过。"""
        r16_type = self._make([
            _element("BLENDINDICES", 0, "R16G16B16A16_UINT", 8),
        ])
        r32_type = self._make([
            _element("BLENDINDICES", 0, "R32G32B32A32_UINT", 16),
        ])
        self.assertFalse(r16_type.widen_blendindices())  # R16 已是最终布局
        self.assertTrue(r32_type.widen_blendindices())   # R32 降宽到 R16
        self.assertEqual(
            r16_type.get_blendindices_layouts(),
            r32_type.get_blendindices_layouts(),
        )
        self.assertEqual(
            r16_type.CategoryStrideDict["Blend"],
            r32_type.CategoryStrideDict["Blend"],
        )

    def test_preserves_channel_count_and_existing_r16_layout(self):
        game_type = self._make([
            _element("BLENDINDICES", 0, "R8_UINT", 1),
            _element("BLENDINDICES", 1, "R16G16_UINT", 4),
        ])
        self.assertTrue(game_type.widen_blendindices())
        self.assertEqual(game_type.get_blendindices_layouts(), [
            (0, "R16_UINT", "vb2"),
            (1, "R16G16_UINT", "vb2"),
        ])
        self.assertEqual(game_type.CategoryStrideDict["Blend"], 6)

    def test_rejects_three_channel_r32_downcast_without_valid_dxgi_target(self):
        game_type = self._make([
            _element("BLENDINDICES", 0, "R32G32B32_UINT", 12),
        ])
        with self.assertRaisesRegex(ValueError, "三通道.*无法归一化"):
            game_type.widen_blendindices()

    def test_honors_aligned_byte_offsets_and_overlapping_texcoords(self):
        game_type = D3D11GameType.from_submesh_json_dict({
            "WorkGameType": "GPU_TEST",
            "CategoryBufferList": [{
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
                ],
            }],
        })

        self.assertEqual(game_type.CategoryStrideDict["Texcoord"], 20)
        self.assertEqual(
            [element.AlignedByteOffset for element in game_type.D3D11ElementList],
            [4, 12, 12],
        )


if __name__ == "__main__":
    unittest.main()
