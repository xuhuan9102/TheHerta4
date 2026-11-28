"""测试装载公共件：把真实的 ``common`` / ``utils`` 子模块按 fake 包前缀注册。

仓库既有范式是每个测试文件自己造一套 ``{PKG}.blueprint`` / ``{PKG}.common`` 等
空 ``__path__`` 的假包，再用 ``spec_from_file_location`` 把被测的真实模块加载
进来。空 ``__path__`` 的假包**无法解析相对导入**，所以生产模块每新增一个
``from ..common.xxx import ...`` / ``from ...utils.xxx import ...``，所有装载
它的测试都得补一份对应 fake。

本模块把这件事收成一处：登记真实的 ``common/safe_write.py``、``utils/format_utils.py``
等（含它们的相对依赖 ``utils/tbn_codec.py``），就算假包 ``__path__`` 为空也能
被正常导入。

为什么不用 stub 而是加载真实文件：待登记的都是纯标准库/numpy 依赖的叶子模块，
加载真实实现才能让测试覆盖到真实语义（例如 ``safe_write`` 的"内容没变就不写"），
而不是被一个永远返回 True 的假货骗过去。
"""

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 要按 fake 包前缀登记的真实模块，**按依赖顺序**排列：
# 被依赖的兄弟模块必须排在前面（例：format_utils 执行时 `from .tbn_codec import
# TBNCodec` 需要 tbn_codec 已经执行完）。
# 新增生产模块依赖时在这里追加一行。
_REAL_MODULES = (
	("utils", "tbn_codec"),
	("utils", "format_utils"),
	("common", "safe_write"),
	("common", "mesh_attribute_names"),
	("common", "shapekey_import_layout"),
	("common", "native_shapekey_attributes"),
	# t75：共享骨图 / 通道骨判定的唯一实现（zzmi_skeleton 与 zzmi.py 都依赖它）。
	("common", "zzmi_channel"),
	("common", "zzmi_native_flow"),
	("common", "zzmi_native_vertex_data"),
	# 跨组骨骼合并：zzmi.py 的 cross-group 分支运行期导入 zzmi_skeleton，缺这一条
	# 就让 4 个 ZZMICrossGroupAttachTests 全部 ModuleNotFoundError（按依赖顺序追加）。
	("utils", "json_utils"),
	("common", "efmi_skeleton"),
	("common", "zzmi_skeleton"),
	# 形态键「顶点命中索引」：纯 numpy/标准库的叶子模块，blueprint 侧的
	# node_postprocess_shapekey.py 与 direct_export_shapekey_output_mixin.py 都依赖它。
	("blueprint", "shapekey_sparse_index"),
)


def _ensure_parent_package(package_name):
	"""确保父包存在于 sys.modules（相对导入的前提）。

	测试通常只造了 ``.common`` 假包，没有 ``.utils``，这里补一个占位包。
	已存在的包（哪怕 ``__path__`` 为空）一律不动。
	"""
	if package_name in sys.modules:
		return sys.modules[package_name]
	parent = types.ModuleType(package_name)
	parent.__path__ = []
	sys.modules[package_name] = parent
	return parent


def _install_native_vertex_data_stub(target_name):
	"""Supply the minimal surface ZZMI tests need without importing Blender."""
	module = types.ModuleType(target_name)
	module.UV2_LAYER = "TEXCOORD2.xy"
	module.build_uv2_projection_map = lambda _blueprint_model: {}
	sys.modules[target_name] = module


def register_real_common_modules(fake_common_package: str):
	"""登记真实 ``common`` / ``utils`` 子模块到对应的 fake 包前缀下。

	参数形如 ``"_my_test_pkg.common"``；包前缀同样用于推导 ``utils``：
	``"_my_test_pkg.common"`` → ``"_my_test_pkg.utils"``。

	先为所有目标模块建壳并放进 ``sys.modules``（满足相互引用的相对导入），
	再按 :data:`_REAL_MODULES` 的依赖顺序逐个执行。

	已登记过的模块会被跳过，因此重复调用安全（``setUp`` 里每次调用都没问题）。
	"""
	package_prefix = fake_common_package.rsplit(".", 1)[0]

	pending = []
	for top_dir, module_name in _REAL_MODULES:
		package_name = f"{package_prefix}.{top_dir}"
		target_name = f"{package_name}.{module_name}"
		if target_name in sys.modules:
			continue
		source_path = REPO_ROOT / top_dir / f"{module_name}.py"
		if not source_path.is_file():
			continue

		if module_name == "zzmi_native_vertex_data":
			_install_native_vertex_data_stub(target_name)
			continue
		_ensure_parent_package(package_name)
		spec = importlib.util.spec_from_file_location(target_name, source_path)
		module = importlib.util.module_from_spec(spec)
		sys.modules[target_name] = module
		pending.append((module_name, module, spec))

	loaded = {}
	for module_name, module, spec in pending:
		spec.loader.exec_module(module)
		loaded[module_name] = module
	return loaded
