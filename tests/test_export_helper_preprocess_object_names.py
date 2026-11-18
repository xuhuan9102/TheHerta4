import importlib.util
import sys
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


PKG = "_export_helper_preprocess_object_names_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeObjects(dict):
    def __iter__(self):
        return iter(self.values())


class _FakeObject:
    def __init__(self, name, pointer, *, object_type="MESH", linked=True, missing=False):
        self.name = name
        self._pointer = pointer
        self.type = object_type
        self.is_missing = missing
        self.data = types.SimpleNamespace(is_missing=False)
        self.users_scene = ["Scene"] if linked else []

    def as_pointer(self):
        return self._pointer


class _FakeSocket:
    def __init__(self, from_node=None, linked=False, bl_idname=""):
        self.bl_idname = bl_idname
        self.is_linked = linked
        self.links = []
        if from_node is not None:
            self.links.append(types.SimpleNamespace(from_node=from_node, to_node=None))


class _FakeNode:
    def __init__(self, name, bl_idname, mute=False, inputs=None, outputs=None, **attrs):
        self.name = name
        self.bl_idname = bl_idname
        self.mute = mute
        self.inputs = list(inputs or [])
        self.outputs = list(outputs or [])
        self.id_data = None
        for key, value in attrs.items():
            setattr(self, key, value)


class _FakeNodeCollection(list):
    def get(self, name, default=None):
        for node in self:
            if getattr(node, "name", "") == name:
                return node
        return default


def _make_tree(name, *nodes):
    """创建测试用的 Fake 蓝图树"""
    tree = types.SimpleNamespace(
        name=name,
        bl_idname="SSMTBlueprintTreeType",
        nodes=_FakeNodeCollection(nodes),
    )
    for node in tree.nodes:
        node.id_data = tree
        for socket in getattr(node, "inputs", []):
            for link in getattr(socket, "links", []):
                if getattr(link, "to_node", None) is None:
                    link.to_node = node
        for socket in getattr(node, "outputs", []):
            for link in getattr(socket, "links", []):
                if getattr(link, "from_node", None) is None:
                    link.from_node = node
    return tree


class _FakeObjectPrefixHelper:
    @staticmethod
    def resolve_source_object_name(name):
        return {
            "Hash.Body": "Body",
            "Hash.Hair": "Hair",
        }.get(name, name)

    @staticmethod
    def build_virtual_object_name_for_node(node, strict=False):
        return getattr(node, "virtual_object_name", "") or getattr(node, "object_name", "")


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(objects=_FakeObjects(), node_groups=[]),
    types=types.SimpleNamespace(Object=object),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(get_workspace_name=lambda: ""),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(ignore_muted_shape_keys=lambda: False),
)
_install_module(f"{PKG}.common.m_key", M_Key=type("M_Key", (), {}))
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=_FakeObjectPrefixHelper,
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        iter_exportable_shape_keys=lambda obj: (),
    ),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "export_helper.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.export_helper", module_path)
export_helper = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.export_helper"] = export_helper
spec.loader.exec_module(export_helper)


class ExportHelperPreprocessObjectNameTests(unittest.TestCase):
    """测试 ExportHelper 预处理对象名称收集功能"""

    def setUp(self):
        """每个测试前清空 Fake 数据"""
        _fake_bpy.data.objects.clear()
        _fake_bpy.data.node_groups[:] = []
        export_helper.BlueprintExportHelper.runtime_result_output_node_type = ""

    def test_export_reference_check_reports_invalid_connected_nodes_and_pro_entries(self):
        objects = _fake_bpy.data.objects
        sibling_name = "LOD0.92cb56c9-64626-0.同一部件"
        objects[sibling_name] = _FakeObject(sibling_name, 1)
        objects["Unlinked"] = _FakeObject("Unlinked", 2, linked=False)
        objects["Lamp"] = _FakeObject("Lamp", 3, object_type="LIGHT")
        objects["No mesh"] = _FakeObject("No mesh", 4)
        objects["No mesh"].data = None
        objects["Missing library"] = _FakeObject("Missing library", 5, missing=True)
        deleted_name = "LOD0.92cb56c9-64626-0.已删除"
        missing = _FakeNode("旧物体", "SSMTNode_Object_Info", object_name=deleted_name, object_id="9")
        pro = _FakeNode(
            "衣服列表",
            "SSMTNode_Object_Info_Pro",
            object_list=[
                types.SimpleNamespace(object_name=name)
                for name in ("Unlinked", "Lamp", "No mesh", "Missing library", "")
            ],
        )
        nested_output = _FakeNode(
            "Output", "SSMTNode_Result_Output",
            inputs=[_FakeSocket(pro, True, "SSMTSocketObject")],
        )
        nested = _make_tree("子蓝图", pro, nested_output)
        _fake_bpy.data.node_groups = {nested.name: nested}
        self.addCleanup(setattr, _fake_bpy.data, "node_groups", [])
        nest = _FakeNode("Nested", "SSMTNode_Blueprint_Nest", blueprint_name=nested.name)
        muted = _FakeNode("已静音", "SSMTNode_Object_Info", mute=True, object_name="Missing")
        disconnected = _FakeNode("未连接", "SSMTNode_Object_Info", object_name="Missing")
        output = _FakeNode(
            "Output", "SSMTNode_Result_Output",
            inputs=[_FakeSocket(node, True, "SSMTSocketObject") for node in (missing, nest, muted)],
        )
        tree = _make_tree("主蓝图", missing, nest, muted, disconnected, output)

        with self.assertRaises(ValueError) as err:
            export_helper.BlueprintExportHelper.validate_object_info_references(tree)

        message = str(err.exception)
        self.assertIn(f"主蓝图 / 旧物体（{deleted_name}）— 物体不存在", message)
        self.assertIn("子蓝图 / 衣服列表 第 1 项（Unlinked）— 物体已从场景移除", message)
        self.assertIn("子蓝图 / 衣服列表 第 2 项（Lamp）— 引用的物体不是网格", message)
        self.assertIn("子蓝图 / 衣服列表 第 3 项（No mesh）— 网格数据缺失", message)
        self.assertIn("子蓝图 / 衣服列表 第 4 项（Missing library）— 链接物体缺失", message)
        self.assertIn("子蓝图 / 衣服列表 第 5 项（空引用）— 未设置物体", message)
        self.assertNotIn("已静音", message)
        self.assertNotIn("未连接", message)

    def test_export_reference_check_accepts_renamed_mesh_for_selected_output(self):
        _fake_bpy.data.objects["New name"] = _FakeObject("New name", 1001)
        missing = _FakeNode("Missing", "SSMTNode_Object_Info", object_name="Deleted")
        renamed = _FakeNode(
            "Renamed", "SSMTNode_Object_Info_Pro", object_name="Old name", object_id="1001",
        )
        output = _FakeNode(
            "Output", "SSMTNode_Result_Output", inputs=[_FakeSocket(missing, True, "SSMTSocketObject")],
        )
        selected_output = _FakeNode(
            "NTMI", "SSMTNode_Result_Output_NTMIModImp",
            inputs=[_FakeSocket(renamed, True, "SSMTSocketObject")],
        )
        tree = _make_tree("TestTree", missing, renamed, output, selected_output)

        export_helper.BlueprintExportHelper.validate_object_info_references(tree, selected_output)

    def test_collect_connected_preprocess_object_names_prefers_object_id_resolution(self):
        """测试 collect_connected_preprocess_object_names 优先通过 object_id 解析真实对象名"""
        _fake_bpy.data.objects["Body"] = _FakeObject("Body", 1001)

        object_info = _FakeNode(
            "BodyInfo",
            "SSMTNode_Object_Info",
            object_name="Hash.Body",
            original_object_name="StaleBody",
            object_id="1001",
            virtual_object_name="Hash.Body",
        )
        output_node = _FakeNode(
            "Output",
            "SSMTNode_Result_Output",
            inputs=[_FakeSocket(from_node=object_info, linked=True, bl_idname="SSMTSocketObject")],
        )
        tree = _make_tree("TestTree", object_info, output_node)

        result = export_helper.BlueprintExportHelper.collect_connected_preprocess_object_names(tree)

        self.assertEqual(result, ["Body"])

    def test_collect_connected_preprocess_object_names_resolves_multifile_items_to_real_names(self):
        """测试 collect_connected_preprocess_object_names 能解析多文件导出项的真实对象名"""
        _fake_bpy.data.objects["Hair"] = _FakeObject("Hair", 2002)

        multifile_item = types.SimpleNamespace(
            object_name="Hash.Hair",
            original_object_name="Hair",
            object_id="2002",
        )
        multifile_node = _FakeNode(
            "MultiFile",
            "SSMTNode_MultiFile_Export",
            object_list=[multifile_item],
        )
        output_node = _FakeNode(
            "Output",
            "SSMTNode_Result_Output",
            inputs=[_FakeSocket(from_node=multifile_node, linked=True, bl_idname="SSMTSocketObject")],
        )
        tree = _make_tree("TestTree", multifile_node, output_node)

        result = export_helper.BlueprintExportHelper.collect_connected_preprocess_object_names(tree)

        self.assertEqual(result, ["Hair"])

    def test_find_preprocess_copy_name_matches_virtual_and_real_names(self):
        """测试 find_preprocess_copy_name 能匹配虚拟名称和真实名称"""
        _fake_bpy.data.objects["Body"] = _FakeObject("Body", 3003)

        copy_map = {
            "Body": "Body_copy",
            "123456-789-100.Body": "VirtualBody_copy",
        }

        matched_name, copy_name = export_helper.BlueprintExportHelper.find_preprocess_copy_name(
            copy_map,
            "Hash.Body",
            object_id="3003",
            original_object_name="StaleBody",
            virtual_object_name="123456-789-100.Body",
        )

        self.assertEqual(matched_name, "Body")
        self.assertEqual(copy_name, "Body_copy")


if __name__ == "__main__":
    unittest.main()
