# -*- coding: utf-8 -*-
"""配置文件清理节点：目标 ini 定位时排除 `*_PureTexture.ini` 派生产物。

被测文件：环境变量 `SSMT_CLEANUP_MODULE` 优先，其次同目录 work 副本，
最后回退仓库 `blueprint/node_postprocess_comment_cleanup.py`（复制到 tests\ 也能跑）。
`find_config_table_files` 用桩替代（本补丁不改它，备份 / 清理仍要能看到片段）。
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent


def _find_repo_root(start):
    for candidate in (start, *start.parents):
        if (candidate / "blueprint" / "node_postprocess_comment_cleanup.py").is_file():
            return candidate
    raise RuntimeError(
        f"找不到仓库根目录（从 {start} 往上没有 blueprint/node_postprocess_comment_cleanup.py）"
    )


REPO_ROOT = _find_repo_root(TEST_DIR)


def _module_file():
    """被测文件：环境变量优先 → 同目录 work 副本 → 仓库 blueprint 原件。

    这样测试无论放在 work 目录还是复制到 tests\\ 都能找到实现。
    """
    override = os.environ.get("SSMT_CLEANUP_MODULE", "")
    if override and Path(override).is_file():
        return Path(override)
    sibling = TEST_DIR / "node_postprocess_comment_cleanup.py"
    if sibling.is_file():
        return sibling
    return REPO_ROOT / "blueprint" / "node_postprocess_comment_cleanup.py"


MODULE_FILE = _module_file()

PKG = "_comment_cleanup_test_pkg"
for _name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    _package = types.ModuleType(_name)
    _package.__path__ = []
    sys.modules[_name] = _package


class _FakeOperator:
    pass


def _build_fake_bpy():
    bpy_types = types.ModuleType("bpy.types")
    bpy_types.Operator = _FakeOperator
    bpy_types.PropertyGroup = type("PropertyGroup", (), {})

    bpy_props = types.ModuleType("bpy.props")
    bpy_props.StringProperty = lambda **kwargs: kwargs.get("default", "")
    bpy_props.IntProperty = lambda **kwargs: kwargs.get("default", 0)
    bpy_props.BoolProperty = lambda **kwargs: kwargs.get("default", False)

    bpy_utils = types.ModuleType("bpy.utils")
    bpy_utils.register_class = lambda _cls: None
    bpy_utils.unregister_class = lambda _cls: None

    bpy_module = types.ModuleType("bpy")
    bpy_module.types = bpy_types
    bpy_module.props = bpy_props
    bpy_module.utils = bpy_utils
    return bpy_module, {
        "bpy.types": bpy_types,
        "bpy.props": bpy_props,
        "bpy.utils": bpy_utils,
    }


_fake_bpy, _fake_bpy_submodules = _build_fake_bpy()


class _State:
    workspace_name = ""


def _find_config_table_files(directory):
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(str(path) for path in root.glob("*.ini") if path.is_file())


class _GlobalConfig:
    @classmethod
    def get_workspace_name(cls):
        return _State.workspace_name

    @classmethod
    def read_from_main_json_ssmt4(cls):
        return None

    @classmethod
    def path_generate_mod_folder(cls):
        return ""


class _MaterialBase:
    @staticmethod
    def _replace_non_ascii_runs(text):
        return text


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_SAVED_BPY = {name: sys.modules.get(name) for name in _fake_bpy_submodules}
_SAVED_BPY_MODULE = sys.modules.get("bpy")
sys.modules["bpy"] = _fake_bpy
sys.modules.update(_fake_bpy_submodules)

try:
    _install_module(
        f"{PKG}.common.config_table_backup",
        find_config_table_files=_find_config_table_files,
    )
    _install_module(f"{PKG}.common.global_config", GlobalConfig=_GlobalConfig)
    _install_module(f"{PKG}.blueprint.node_postprocess_base", SSMTNode_PostProcess_Base=object)
    _install_module(
        f"{PKG}.blueprint.node_postprocess_material",
        SSMTNode_PostProcess_MaterialBase=_MaterialBase,
    )
    _spec = importlib.util.spec_from_file_location(
        f"{PKG}.blueprint.node_postprocess_comment_cleanup", MODULE_FILE
    )
    module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = module
    _spec.loader.exec_module(module)
finally:
    for _name, _previous in _SAVED_BPY.items():
        if _previous is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _previous
    if _SAVED_BPY_MODULE is None:
        sys.modules.pop("bpy", None)
    else:
        sys.modules["bpy"] = _SAVED_BPY_MODULE


class TargetIniTests(unittest.TestCase):
    def setUp(self):
        _State.workspace_name = ""

    def _touch(self, directory, names):
        for name in names:
            (Path(directory) / name).write_text("[TextureOverride_IB_7fbbcf0d]\n", encoding="utf-8")

    def _find(self, directory):
        return module.SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(directory)

    def test_puretexture_is_filtered_before_disambiguation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, ["工作空间.ini", "abcdef12-1-0_PureTexture.ini"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                path, error = self._find(tmp)
            self.assertEqual("", error)
            self.assertEqual("工作空间.ini", os.path.basename(path))
            self.assertIn(
                "已排除材质转资源pro 的派生产物 *_PureTexture.ini",
                output.getvalue(),
            )

    def test_only_puretexture_reports_derived_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, ["abcdef12-1-0_PureTexture.ini"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                path, error = self._find(tmp)
            self.assertEqual("", path)
            self.assertIn("派生产物", error)
            self.assertIn("_PureTexture.ini", error)

    def test_workspace_disambiguation_is_unchanged(self):
        _State.workspace_name = "工作空间"
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, ["工作空间.ini", "Other.ini", "abcdef12-1-0_PureTexture.ini"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                path, error = self._find(tmp)
            self.assertEqual("", error)
            self.assertEqual("工作空间.ini", os.path.basename(path))


if __name__ == "__main__":
    unittest.main()
