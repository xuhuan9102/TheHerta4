# -*- coding: utf-8 -*-
"""「贴图资源去重」节点像素级去重模式的集成测试。

覆盖：
- 关闭复选框：像素相同但字节/格式不同的 DDS 不合并（维持原 MD5 行为）
- 开启复选框：像素完全相同的不同格式 DDS 判定为重复 -> 引用改写 + 删除重复文件
- 像素不同 / 尺寸不同：不合并
- BC6H 等无法解码的格式：优雅跳过、不崩溃、不误删
- 多个重复文件只保留链序第一份
"""

import importlib.util
import os
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_resource_merge_pixel_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for pkg_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    _pkg = _install_module(pkg_name)
    _pkg.__path__ = []

_install_module("bpy", types=types.SimpleNamespace(), props=types.SimpleNamespace(BoolProperty=lambda **_k: False))

dds_pixel = _load_real(f"{PKG}.common.dds_pixel", "common/dds_pixel.py")
sys.modules[f"{PKG}.common"].dds_pixel = dds_pixel


class _StubBase:
    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", str(content)

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return str(content), ""

    def _create_cumulative_backup(self, ini_file, mod_export_path):
        return None


_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=_StubBase)

_node_mod = _load_real(f"{PKG}.blueprint.node_postprocess_resource_merge",
                       "blueprint/node_postprocess_resource_merge.py")
ResourceMergeNode = _node_mod.SSMTNode_PostProcess_ResourceMerge


# ---------------------------------------------------------------------------
# DDS 构造工具
# ---------------------------------------------------------------------------
def _dds_header(fourcc, width, height):
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, 0x1007)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 76, 32)
    struct.pack_into("<I", header, 80, 0x4)  # FOURCC
    header[84:88] = fourcc
    struct.pack_into("<I", header, 108, 0x1000)
    return bytes(header)


def _bc1_block(c0, c1, idx_pattern):
    """idx_pattern: 长度为 16 的 0..3 列表。"""
    idx = 0
    for i in range(16):
        idx |= idx_pattern[i] << (2 * i)
    return struct.pack("<HHI", c0, c1, idx)


def _dxt1_dds(width, height, blocks):
    return _dds_header(b"DXT1", width, height) + b"".join(blocks)


def _dxt3_dds(width, height, alpha_bytes, color_blocks):
    return _dds_header(b"DXT3", width, height) + alpha_bytes + b"".join(color_blocks)


def _dxt5_dds(width, height, alpha_block, color_blocks):
    """alpha_block: 8 字节 (a0,a1,idx48)。"""
    return _dds_header(b"DXT5", width, height) + alpha_block + b"".join(color_blocks)


GREY = _bc1_block(0xFFFF, 0x0000, [2] * 16)       # 白黑端点 -> 灰 170
RED = _bc1_block(0xF800, 0x0000, [2] * 16)        # 红/黑 -> 红 85? (2*255+0)/3 只算 R 通道
BLUE = _bc1_block(0x001F, 0x0000, [2] * 16)

# DXT5 opaque alpha：a0=255 > a1=0 -> 8 值表，选索引 0 -> 255（全不透明）
OPAQUE_ALPHA = bytes([255, 0]) + bytes(6)


def _simple_ini(resources):
    """resources: [(section, filename)]"""
    parts = []
    for section, filename in resources:
        parts.append(f"[{section}]\nfilename = {filename}\n\n")
    return "".join(parts)


class PixelDedupNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pixel_dedup_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.textures = Path(self.tmp) / "Textures"
        self.textures.mkdir()
        self.ini = Path(self.tmp) / "Workspace.ini"

    def _run(self, node, ini_body):
        self.ini.write_text(ini_body, encoding="utf-8")
        node.process_ini_file(str(self.ini), self.tmp)
        return self.ini.read_text(encoding="utf-8")

    def test_off_pixel_dedup_keeps_different_format_duplicates(self):
        """关闭像素去重：内容像素相同但格式/字节不同的贴图不合并（MD5 不同）。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        a.write_bytes(_dxt1_dds(4, 4, [GREY]))
        b.write_bytes(_dxt5_dds(4, 4, OPAQUE_ALPHA, [GREY]))
        node = ResourceMergeNode()
        node.use_pixel_dedup = False
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/a.dds", content)
        self.assertIn("Textures/b.dds", content)

    def test_on_pixel_dedup_merges_different_format_duplicates(self):
        """开启像素去重：DXT1 与 DXT5（不透明 alpha）像素完全一致 -> 合并删除 b。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        a.write_bytes(_dxt1_dds(4, 4, [GREY]))
        b.write_bytes(_dxt5_dds(4, 4, OPAQUE_ALPHA, [GREY]))
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertFalse(b.exists(), "像素重复文件应被删除")
        self.assertIn("Textures/a.dds", content)
        self.assertNotIn("Textures/b.dds", content)
        self.assertEqual(content.count("Textures/a.dds"), 2)

    def test_pixel_dedup_keeps_different_pixels(self):
        """像素不同：即使同尺寸也不合并。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        a.write_bytes(_dxt1_dds(4, 4, [GREY]))
        b.write_bytes(_dxt1_dds(4, 4, [RED]))
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    def test_pixel_dedup_keeps_different_dimensions(self):
        """尺寸不同：不合并。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        a.write_bytes(_dxt1_dds(4, 4, [GREY]))
        b.write_bytes(_dxt1_dds(8, 4, [GREY, GREY]))
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    def test_pixel_dedup_skips_bc6h_gracefully(self):
        """BC6H 无法解码：跳过并保留文件，不崩溃。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        a.write_bytes(_dxt1_dds(4, 4, [GREY]))
        b.write_bytes(_dds_header(b"BC6H", 4, 4) + b"\x00" * 16)
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)

    def test_pixel_dedup_three_duplicates_keep_first(self):
        """三个像素相同的文件只保留链序第一份。"""
        paths = []
        for i, name in enumerate(("a", "b", "c")):
            p = self.textures / f"{name}.dds"
            p.write_bytes(_dxt1_dds(4, 4, [GREY]))
            paths.append(p)
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds"),
                                               ("ResourceC", "Textures/c.dds")]))
        self.assertTrue(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertFalse(paths[2].exists())
        self.assertEqual(content.count("Textures/a.dds"), 3)

    def test_md5_dedup_still_works_without_pixel_mode(self):
        """回归：MD5 相同（字节一致）时仍按原逻辑合并。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        data = _dxt1_dds(4, 4, [GREY])
        a.write_bytes(data)
        b.write_bytes(data)
        node = ResourceMergeNode()
        node.use_pixel_dedup = False
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertNotIn("Textures/b.dds", content)

    def test_md5_duplicates_skip_pixel_decode(self):
        """MD5 已判重的文件不参与像素阶段（不重复解码）。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        data = _dxt1_dds(4, 4, [GREY])
        a.write_bytes(data)
        b.write_bytes(data)
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertNotIn("Textures/b.dds", content)

    def test_dxt1_vs_dxt3_opaque_merge(self):
        """DXT1 与 DXT3（alpha 全 0xF）像素一致 -> 合并。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        a.write_bytes(_dxt1_dds(4, 4, [GREY]))
        b.write_bytes(_dxt3_dds(4, 4, b"\xff" * 8, [GREY]))
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertIn("Textures/a.dds", content)
        self.assertNotIn("Textures/b.dds", content)

    def test_alpha_actually_transparent_not_merged(self):
        """DXT5 真实透明（alpha 不是全 255）与不透明内容像素不同 -> 不合并。"""
        a = self.textures / "a.dds"
        b = self.textures / "b.dds"
        a.write_bytes(_dxt1_dds(4, 4, [GREY]))
        # alpha：a0=0 <= a1=0 -> 6值+0/255 分支；索引全 2 -> (4*0+0)//5 = 0 全透明
        alpha_block = bytes([0, 0]) + (0x492492492492).to_bytes(6, "little")  # 48 位 3bpp 索引全=2
        b.write_bytes(_dxt5_dds(4, 4, alpha_block, [GREY]))
        node = ResourceMergeNode()
        node.use_pixel_dedup = True
        content = self._run(node, _simple_ini([("ResourceA", "Textures/a.dds"),
                                               ("ResourceB", "Textures/b.dds")]))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertIn("Textures/b.dds", content)


if __name__ == "__main__":
    unittest.main()