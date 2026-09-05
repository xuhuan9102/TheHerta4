"""资源合并仅处理 DDS，不能改写或删除任何 `.buf` 缓冲区。

`_is_resource_section` 仍按段名识别 Resource；真正进入 MD5 去重前还必须通过 `.dds`
扩展名过滤。合并骨骼使用的 vgmap、redirect texcoord、Position 等 `.buf` 即使字节相同，
也不得进入贴图去重池。

本单测加载**真实** node_postprocess_resource_merge.py（fake-bpy 允许），构造两条字节相同的
Resource 缓冲引用，断言两个 `.buf` 均保留且 `filename =` 不被改写。
不编辑任何源文件。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_resource_merge_md5_dedup_test_pkg"


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


for pkg_name in (PKG, f"{PKG}.blueprint"):
    _pkg = _install_module(pkg_name)
    _pkg.__path__ = []

_install_module("bpy", types=types.SimpleNamespace(), props=types.SimpleNamespace(BoolProperty=lambda **_k: False))


class _StubBase:
    """资源合并继承的 SSMTNode_PostProcess_Base；为本单测提供实际会调用的方法（no-op/透传）。"""

    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", str(content)

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return str(content), ""

    def _create_cumulative_backup(self, ini_file, mod_export_path):
        return None


_install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=_StubBase)

_rm = _load_real(f"{PKG}.blueprint.node_postprocess_resource_merge", "blueprint/node_postprocess_resource_merge.py")
ResourceMergeNode = _rm.SSMTNode_PostProcess_ResourceMerge


class ResourceMergeMd5DedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resmerge_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.meshes = Path(self.tmp) / "Meshes"
        self.meshes.mkdir(parents=True, exist_ok=True)

    def _buf(self, rel, data):
        p = Path(self.tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def _ini(self, rel, body):
        p = Path(self.tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def test_is_resource_section_true_for_merge_resources(self):
        """合并侧的 `[ResourceZZVgMap_*]`/`[ResourceZZRedirectTexcoord_*]`/`[ResourceMergedSkeletonDataRW*]`
        仍属于 Resource 段，但其 `.buf` filename 会在后续扩展名过滤中跳过。"""
        node = ResourceMergeNode()
        self.assertTrue(node._is_resource_section("[ResourceZZVgMap_a23aa8a3]"))
        self.assertTrue(node._is_resource_section("[ResourceZZRedirectTexcoord_a_2_0]"))
        self.assertTrue(node._is_resource_section("[ResourceMergedSkeletonDataRW_0]"))
        self.assertTrue(node._is_resource_section("[ResourceZZPalette_aaa]"))
        self.assertFalse(node._is_resource_section("[CustomShaderZZMIAttach_C0]"))

    def test_duplicate_buf_bytes_are_not_rewritten_or_deleted(self):
        """即使两个 .buf 字节完全相同，资源合并节点也必须跳过。"""
        a = self._buf("Meshes/compileA.buf", b"AAAABBBB")
        b = self._buf("Meshes/compileB.buf", b"AAAABBBB")  # 与 a 字节相同
        ini = self._ini("mod.ini", (
            "[ResourceA]\n"
            "type = Buffer\n"
            "filename = Meshes/compileA.buf\n"
            "\n"
            "[ResourceB]\n"
            "type = Buffer\n"
            "filename = Meshes/compileB.buf\n"
        ))
        node = ResourceMergeNode()
        node.process_ini_file(str(ini), self.tmp)

        self.assertTrue(b.exists(), "资源合并不得删除 .buf")
        self.assertTrue(a.exists())
        new_text = ini.read_text(encoding="utf-8")
        self.assertIn("filename = Meshes/compileA.buf", new_text)
        self.assertIn("filename = Meshes/compileB.buf", new_text)

    def test_different_bytes_not_deduped(self):
        """字节不同的 .buf 不参与去重（良性：不误删）。"""
        a = self._buf("Meshes/uniqueA.buf", b"AAAA")
        b = self._buf("Meshes/uniqueB.buf", b"BBBB")
        ini = self._ini("mod.ini", (
            "[ResourceA]\n"
            "filename = Meshes/uniqueA.buf\n"
            "\n"
            "[ResourceB]\n"
            "filename = Meshes/uniqueB.buf\n"
        ))
        ResourceMergeNode().process_ini_file(str(ini), self.tmp)
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        new_text = ini.read_text(encoding="utf-8")
        self.assertIn("Meshes/uniqueA.buf", new_text)
        self.assertIn("Meshes/uniqueB.buf", new_text)


if __name__ == "__main__":
    unittest.main()
