import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_resource_merge_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module("bpy", types=types.SimpleNamespace(), props=types.SimpleNamespace(BoolProperty=lambda **_k: False))
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "SSMTNode_PostProcess_Base",
        (),
        {
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
            "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
            "split_anim_driver_block_content": classmethod(lambda cls, content: ("", content)),
        },
    ),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_resource_merge.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_resource_merge", module_path)
resource_merge_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = resource_merge_module
spec.loader.exec_module(resource_merge_module)


class ResourceMergeTests(unittest.TestCase):
    """测试资源合并后处理：合并内容相同的资源段并清理冗余文件"""

    def test_resource_sections_without_hyphen_are_merged(self):
        """测试无连字符的资源段被合并（相同内容的纹理文件只保留一个）"""
        node = resource_merge_module.SSMTNode_PostProcess_ResourceMerge()

        with tempfile.TemporaryDirectory() as temp_dir:
            textures_dir = os.path.join(temp_dir, "Textures")
            os.makedirs(textures_dir, exist_ok=True)

            texture_a = os.path.join(textures_dir, "a.dds")
            texture_b = os.path.join(textures_dir, "b.dds")
            with open(texture_a, "wb") as file_obj:
                file_obj.write(b"same texture")
            with open(texture_b, "wb") as file_obj:
                file_obj.write(b"same texture")

            ini_path = os.path.join(temp_dir, "Workspace.ini")
            with open(ini_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    "[ResourceDiffuse]\n"
                    "filename = Textures/a.dds\n\n"
                    "[Resource_Light]\n"
                    "filename = Textures/b.dds\n"
                )

            node.process_ini_file(ini_path, temp_dir)

            with open(ini_path, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()

            self.assertIn("filename = Textures/a.dds", content)
            self.assertNotIn("filename = Textures/b.dds", content)
            self.assertFalse(os.path.exists(texture_b))

    def test_multiple_resource_aliases_to_same_dds_keep_canonical_file(self):
        """多个贴图资源别名共享同一DDS时，不得删除唯一的物理文件。"""
        node = resource_merge_module.SSMTNode_PostProcess_ResourceMerge()

        with tempfile.TemporaryDirectory() as temp_dir:
            textures_dir = os.path.join(temp_dir, "Textures")
            os.makedirs(textures_dir, exist_ok=True)

            texture_path = os.path.join(textures_dir, "body.dds")
            with open(texture_path, "wb") as file_obj:
                file_obj.write(b"shared dds texture")

            ini_path = os.path.join(temp_dir, "Workspace.ini")
            with open(ini_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    "[Resource_body_Diffuse]\n"
                    "filename = Textures/body.dds\n\n"
                    "[Resource_body_Diffuse_Alias]\n"
                    "filename = Textures/body.dds\n"
                )

            node.process_ini_file(ini_path, temp_dir)

            self.assertTrue(os.path.exists(texture_path))
            with open(ini_path, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
            self.assertEqual(content.count("filename = Textures/body.dds"), 2)

    def test_merge_preserves_preamble_duplicate_sections_and_line_endings(self):
        """资源引用替换只能改 filename 行，不能重建并吞掉 INI 的其他内容。"""
        node = resource_merge_module.SSMTNode_PostProcess_ResourceMerge()

        with tempfile.TemporaryDirectory() as temp_dir:
            textures_dir = Path(temp_dir) / "Textures"
            textures_dir.mkdir()
            (textures_dir / "a.dds").write_bytes(b"same")
            (textures_dir / "b.dds").write_bytes(b"same")
            ini_path = Path(temp_dir) / "Workspace.ini"
            original = (
                "namespace = Example\\Character\r\n"
                "; preamble must survive\r\n\r\n"
                "[Constants]\r\n"
                "global $first = 1\r\n\r\n"
                "[ResourceA]\r\n"
                "  filename = Textures/a.dds\r\n\r\n"
                "[Constants]\r\n"
                "global $second = 2\r\n\r\n"
                "[ResourceB]\r\n"
                "  filename = Textures/b.dds\r\n"
            )
            ini_path.write_bytes(original.encode("utf-8"))

            node.process_ini_file(str(ini_path), temp_dir)

            content = ini_path.read_bytes().decode("utf-8")
            expected = original.replace("Textures/b.dds", "Textures/a.dds")
            self.assertEqual(content, expected)
            self.assertEqual(content.count("[Constants]"), 2)

    def test_parent_path_resource_is_never_hashed_rewritten_or_deleted(self):
        """INI 中的 ../ 路径不能让资源合并读写模组目录外的文件。"""
        node = resource_merge_module.SSMTNode_PostProcess_ResourceMerge()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mod_dir = root / "Mod"
            mod_dir.mkdir()
            inside = mod_dir / "inside.dds"
            outside = root / "outside.dds"
            inside.write_bytes(b"same")
            outside.write_bytes(b"same")
            ini_path = mod_dir / "Workspace.ini"
            original = (
                "[ResourceInside]\nfilename = inside.dds\n\n"
                "[ResourceOutside]\nfilename = ../outside.dds\n"
            )
            ini_path.write_text(original, encoding="utf-8")

            node.process_ini_file(str(ini_path), str(mod_dir))

            self.assertTrue(outside.exists())
            self.assertEqual(ini_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
