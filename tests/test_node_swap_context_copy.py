# -*- coding: utf-8 -*-
import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_swap_context_copy_test_pkg"
REPO_ROOT = Path(__file__).resolve().parents[1]
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    package = _install_module(package_name)
    package.__path__ = []
sys.modules[f"{PKG}.common"].__path__ = [str(REPO_ROOT / "common")]


class _FakeMenu:
    @staticmethod
    def append(_fn):
        return None

    @staticmethod
    def remove(_fn):
        return None


_fake_bpy = types.SimpleNamespace(
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
    ),
    types=types.SimpleNamespace(
        Operator=object,
        Node=object,
        UI_MT_button_context_menu=_FakeMenu,
    ),
    data=types.SimpleNamespace(node_groups=[]),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(f"{PKG}.common.object_prefix_helper", ObjectPrefixHelper=types.SimpleNamespace())
_install_module(f"{PKG}.blueprint.node_base", SSMTNodeBase=object)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    ensure_object_swap_variable_name=lambda *_args, **_kwargs: "swapkey0",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip().lstrip("$"),
)


module_path = REPO_ROOT / "blueprint" / "node_swap.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_swap", module_path)
node_swap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = node_swap
spec.loader.exec_module(node_swap)


class NodeSwapContextCopyTests(unittest.TestCase):
    def test_copy_to_same_hotkey_nodes_updates_matching_object_swap_nodes_only(self):
        source = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            name="Source",
            hotkey="No_Modifiers Numpad3",
            comment="同步内容",
            id_data=None,
        )
        target_same = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            name="TargetSame",
            hotkey="No_Modifiers Numpad3",
            comment="旧内容",
            id_data=None,
        )
        target_other = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            name="TargetOther",
            hotkey="Ctrl A",
            comment="别改我",
            id_data=None,
        )
        other_node = types.SimpleNamespace(
            bl_idname="SSMTNode_Object_Info",
            name="OtherType",
            hotkey="No_Modifiers Numpad3",
            comment="别改我",
            id_data=None,
        )

        tree = types.SimpleNamespace(
            bl_idname="SSMTBlueprintTreeType",
            name="TreeA",
            nodes={
                "Source": source,
                "TargetSame": target_same,
                "TargetOther": target_other,
                "OtherType": other_node,
            },
        )
        source.id_data = tree
        target_same.id_data = tree
        target_other.id_data = tree
        other_node.id_data = tree
        _fake_bpy.data.node_groups[:] = [tree]

        operator = node_swap.SSMT_OT_CopySwapPropertyToSameHotkeyNodes()
        operator.source_tree_name = "TreeA"
        operator.source_node_name = "Source"
        operator.property_name = "comment"
        reports = []
        operator.report = lambda level, message: reports.append((level, message))

        result = operator.execute(types.SimpleNamespace())

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(target_same.comment, "同步内容")
        self.assertEqual(target_other.comment, "别改我")
        self.assertEqual(other_node.comment, "别改我")
        self.assertTrue(any("已同步 1 个相同快捷键节点" in str(message) for _level, message in reports))


if __name__ == "__main__":
    unittest.main()
