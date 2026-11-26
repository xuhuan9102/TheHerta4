import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock


PKG = "_test_key_type_pkg"
for package_name in (PKG, f"{PKG}.common"):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package

global_config = types.SimpleNamespace(logic_name="ZZMI")
sys.modules[f"{PKG}.common.global_config"] = types.SimpleNamespace(
    GlobalConfig=global_config
)
sys.modules[f"{PKG}.common.logic_name"] = types.SimpleNamespace(
    LogicName=types.SimpleNamespace(APMI="APMI")
)

module_path = Path(__file__).resolve().parents[1] / "common" / "key_type.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.common.key_type", module_path)
key_type = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = key_type
spec.loader.exec_module(key_type)


def test_apmi_uses_activate_for_one_shot_keys():
    with mock.patch.object(global_config, "logic_name", "APMI"):
        assert key_type.one_shot_key_type() == "activate"


def test_other_presets_keep_press():
    with mock.patch.object(global_config, "logic_name", "ZZMI"):
        assert key_type.one_shot_key_type() == "press"
