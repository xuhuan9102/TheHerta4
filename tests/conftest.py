"""pytest 9.x 下从仓库根目录跑测试的收集器兼容垫片。

仓库根 ``__init__.py`` 是真实 Blender 插件包入口（依赖真实 bpy）；pytest 9
（importlib 包前缀收集）会把根目录视为 ``Package`` 收集器，并在测试 setup 时
导入 ``TheHerta4/__init__.py``——无真实 bpy 环境下直接崩在
``class GlobalProterties(bpy.types.PropertyGroup)``。

本 conftest 在收集阶段预注册根包为只读 stub，令 pytest 的包级 setup 命中
``sys.modules`` 而不执行插件真实初始化。测试自身通过各自的 fake-PKG 前缀
加载真实模块（``tests/test_efmi_*`` 等既有模式），不依赖根包被导入；从
``tests/`` 目录内跑（旧式 ``cd tests && pytest``）时根包不被导入，此垫片无副作用。

--- 吸收自 TheHerta4Test_20260915（2026-09-12）-----------------------------

包名改为**从目录名推导**，不再硬编码 ``TheHerta4``。

原实现只在插件目录恰好叫 ``TheHerta4`` 时生效。一旦改目录名（例如为了和已经
安装的正式版共存，改成 ``TheHerta4Test``），pytest 会把 ``__init__.py`` 当成
无名根包直接执行，撞在 ``from .common import global_properties`` 上抛
ImportError，并连带让 40 多个用例报 "ERROR at setup"。推导之后测试套件与
目录名解耦（worktree 目录名如 ``TheHerta4-absorb`` 同理受益）。
"""

import sys
import types
from pathlib import Path

# 插件根目录 = 本文件的上一级；它的目录名就是 Blender 看到的包名。
_ROOT = Path(__file__).resolve().parents[1]
_PKG = _ROOT.name

if _PKG not in sys.modules:
    _root_pkg_stub = types.ModuleType(_PKG)
    # 仅满足 pytest Package.setup 的 importtestmodule；任何真实代码路径
    # 都不会读到它（真实模块以 PKG 前缀加载）。
    _root_pkg_stub.__file__ = str(_ROOT / "__init__.py")
    _root_pkg_stub.__path__ = [str(_ROOT)]
    sys.modules[_PKG] = _root_pkg_stub
