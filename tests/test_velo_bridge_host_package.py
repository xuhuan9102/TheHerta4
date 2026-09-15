# -*- coding: utf-8 -*-
"""Velo 桥的**主包名解析**测试。

背景（2026-09-13，测试版修复）
------------------------------
`TheHerta4_Velo_Bridge/__init__.py` 里原先有两处写死的

    from TheHerta4.blueprint... import ...

主包目录名**是可以被改的**：本项目的测试版就叫 `TheHerta4Test`（必须与用户
已安装的正式版共存）。写死之后两处都会抛 `ModuleNotFoundError`，而且**后果不同**：

* `_swap_bindings` 里被 `except Exception` 吞掉 ⇒ **静默降级**，
  变量名不再与主插件后处理节点共享同一身份；
* 导出路径里被包成 `raise ValueError('后处理节点执行失败: …')`
  ⇒ 用户看到一句与真正原因无关的报错，**导出直接取消**。

上游新增的 `tests/test_velo_bridge_efmi.py` **盖不住这个问题**：它用
`_test_velo_bridge` 这样的假包名加载模块，并且给主包模块打了桩，
所以"主包叫什么名字"这条路径从来没有被真正走过。

这个文件补的就是那一格。

负对照（证明这套测试真的抓得到旧写法）
--------------------------------------

    $env:VELO_BRIDGE_PATH = '<指向未修复的桥 __init__.py>'
    python tests/test_velo_bridge_host_package.py

测未修复版本时应当 **FAILED**。没有这一步，"通过"只说明"没找到问题"。
"""

import contextlib
import importlib.util
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
# 默认测本测试版；`VELO_BRIDGE_PATH` 用来指向上游那份，做负对照。
BRIDGE_PATH = Path(
    os.environ.get('VELO_BRIDGE_PATH') or (ROOT / 'TheHerta4_Velo_Bridge' / '__init__.py')
)

# 主包内部模块的后缀 —— 解析器靠它们反推主包名。
_HOST_SUFFIXES = ('.blueprint.export_helper', '.blueprint.variable_registry')


def _fake_bpy():
    module = types.ModuleType('bpy')
    module.types = types.SimpleNamespace(Node=object, Operator=object, PropertyGroup=object)
    prop = lambda **_kwargs: None
    props = types.ModuleType('bpy.props')
    props.StringProperty = prop
    props.BoolProperty = prop
    props.IntProperty = prop
    props.FloatProperty = prop
    module.props = props
    module.path = types.SimpleNamespace(abspath=lambda path: path)
    return module


def _load_bridge():
    fake_bpy = _fake_bpy()
    with mock.patch.dict(sys.modules, {'bpy': fake_bpy, 'bpy.props': fake_bpy.props}):
        spec = importlib.util.spec_from_file_location('_test_velo_bridge_host', BRIDGE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules['_test_velo_bridge_host'] = module
        spec.loader.exec_module(module)
        return module


@contextlib.contextmanager
def _only_these_host_modules(names):
    """临时把 sys.modules 里所有「主包内部模块」换成给定的这几个。"""
    saved = {}
    for key in list(sys.modules):
        if key.endswith(_HOST_SUFFIXES):
            saved[key] = sys.modules.pop(key)
    for name in names:
        sys.modules[name] = types.ModuleType(name)
    try:
        yield
    finally:
        for key in list(sys.modules):
            if key.endswith(_HOST_SUFFIXES):
                del sys.modules[key]
        sys.modules.update(saved)


class HostPackageResolutionTests(unittest.TestCase):

    def test_subpackage_layout_resolves_parent_as_host(self):
        """布局 A：作为子包安装 ⇒ 主包名就是父级，**不管它叫什么**。"""
        bridge = _load_bridge()
        for addon_name in ('TheHerta4', 'TheHerta4Test', 'AnyNameAtAll'):
            bridge.__package__ = addon_name + '.TheHerta4_Velo_Bridge'
            self.assertEqual(bridge._host_package_name(), addon_name)

    def test_standalone_layout_finds_host_in_sys_modules(self):
        """布局 B：单独复制进 addons ⇒ 从已加载模块反推主包名。"""
        bridge = _load_bridge()
        bridge.__package__ = 'TheHerta4_Velo_Bridge'
        with _only_these_host_modules(['RenamedAddon.blueprint.export_helper']):
            self.assertEqual(bridge._host_package_name(), 'RenamedAddon')

    def test_standalone_layout_also_accepts_variable_registry(self):
        bridge = _load_bridge()
        bridge.__package__ = 'TheHerta4_Velo_Bridge'
        with _only_these_host_modules(['OtherName.blueprint.variable_registry']):
            self.assertEqual(bridge._host_package_name(), 'OtherName')

    def test_falls_back_to_canonical_name(self):
        """两条线索都没有时退回规范名，而不是抛异常。"""
        bridge = _load_bridge()
        bridge.__package__ = ''
        with _only_these_host_modules([]):
            self.assertEqual(bridge._host_package_name(), 'TheHerta4')

    def test_host_import_prefers_the_resolved_host(self):
        """★ 关键断言：主包**不叫 TheHerta4** 时，导入依然打到那个主包。"""
        bridge = _load_bridge()
        bridge.__package__ = 'TheHerta4Test.TheHerta4_Velo_Bridge'
        sentinel = object()
        with mock.patch.dict(sys.modules, {
            'TheHerta4Test': types.ModuleType('TheHerta4Test'),
            'TheHerta4Test.blueprint': types.ModuleType('TheHerta4Test.blueprint'),
            'TheHerta4Test.blueprint.export_helper': types.SimpleNamespace(
                BlueprintExportHelper=sentinel),
        }):
            got = bridge._host_import('.blueprint.export_helper').BlueprintExportHelper
        self.assertIs(got, sentinel)


class NoHardcodedHostPackageTests(unittest.TestCase):
    """★ 防回归网：桥里不得再出现写死主包名的导入。"""

    def test_bridge_has_no_hardcoded_host_import(self):
        source = BRIDGE_PATH.read_text(encoding='utf-8')
        bad = []
        for pattern, label in (
            # 标签保持**纯 ASCII**：这段文字会被 qa.ps1 在 GBK 控制台上读回来，
            # 带省略号之类的非 ASCII 字符会变成乱码，反而看不清抓到的是哪一行。
            (r'^\s*from\s+TheHerta4\.', 'hardcoded: from TheHerta4.<module>'),
            (r'^\s*import\s+TheHerta4\.', 'hardcoded: import TheHerta4.<module>'),
            (r'import_module\(\s*[\'"]TheHerta4\.', 'hardcoded: import_module("TheHerta4.<module>")'),
        ):
            for match in re.finditer(pattern, source, re.MULTILINE):
                line_no = source[:match.start()].count('\n') + 1
                bad.append(f'line {line_no}: {label}')
        self.assertEqual(
            bad, [],
            '桥里又出现了写死主包名的导入 —— 主包目录名一改就会坏：\n  '
            + '\n  '.join(bad),
        )

    def test_the_scan_would_catch_a_regression(self):
        """负对照：把写死的那一行塞回去，上面的扫描**必须**报出来。"""
        source = BRIDGE_PATH.read_text(encoding='utf-8')
        pattern = r'^\s*from\s+TheHerta4\.'
        # 用「前后差值」而不是绝对值：指向未修复的旧文件时，
        # 它本来就已经有若干处写死导入，绝对值断言会误判。
        before = len(re.findall(pattern, source, re.MULTILINE))
        poisoned = source + '\nfrom TheHerta4.blueprint.model import X\n'
        after = len(re.findall(pattern, poisoned, re.MULTILINE))
        self.assertEqual(after, before + 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
