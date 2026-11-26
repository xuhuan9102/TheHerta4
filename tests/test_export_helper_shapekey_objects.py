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


PKG = "_export_helper_shapekey_objects_test_pkg"
REPO_ROOT = Path(__file__).resolve().parents[1]
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []
sys.modules[f"{PKG}.blueprint"].__path__ = [str(REPO_ROOT / "blueprint")]


class _FakeObjects(dict):
    def get(self, name, default=None):
        return super().get(name, default)


class _FakeTexts(dict):
    def new(self, name):
        text = types.SimpleNamespace(
            name=name,
            _value="",
            clear=lambda: None,
        )

        def _write(value):
            text._value += value

        def _clear():
            text._value = ""

        text.write = _write
        text.clear = _clear
        self[name] = text
        return text


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


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(objects=_FakeObjects(), node_groups=[], texts=_FakeTexts()),
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
    ObjectPrefixHelper=types.SimpleNamespace(resolve_source_object_name=lambda name: name),
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
        iter_exportable_shape_keys=lambda obj: (
            key_block
            for index, key_block in enumerate(
                getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", []) or []
            )
            if index != 0 and str(getattr(key_block, "name", "") or "").strip().lower() != "basis"
        ),
    ),
)


module_path = REPO_ROOT / "blueprint" / "export_helper.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.export_helper", module_path)
export_helper = importlib.util.module_from_spec(spec)
sys.modules[f"{PKG}.blueprint.export_helper"] = export_helper
spec.loader.exec_module(export_helper)

# 个别测试会临时替换 get_exportable_shape_key_infos 且不恢复，这里保留真实实现供后续测试重置
_real_get_exportable_shape_key_infos = export_helper.BlueprintExportHelper.get_exportable_shape_key_infos


class BlueprintExportShapeKeyObjectsTests(unittest.TestCase):
    """测试 ExportHelper 的形态键对象收集和分类功能"""

    def setUp(self):
        """每个测试前清空 Fake 数据"""
        _fake_bpy.data.objects.clear()
        _fake_bpy.data.node_groups[:] = []
        _fake_bpy.data.texts.clear()
        export_helper.BlueprintExportHelper.shapekey_objects = []
        export_helper.BlueprintExportHelper.shapekey_postprocess_nodes = []
        export_helper.BlueprintExportHelper.runtime_result_output_node_type = ""

    def test_collect_shapekey_objects_resolves_virtual_name_back_to_real_source_object(self):
        """测试 collect_shapekey_objects 将虚拟名称解析回真实源对象名"""
        _fake_bpy.data.objects["Body"] = types.SimpleNamespace(name="Body")
        export_helper.ObjectPrefixHelper.resolve_source_object_name = lambda name: "Body" if name == "Hash.Body" else name
        export_helper.BlueprintExportHelper.collect_connected_object_names = staticmethod(lambda _tree: ["Hash.Body"])

        result = export_helper.BlueprintExportHelper.collect_shapekey_objects(tree=object())

        self.assertEqual(result, ["Body"])

    def test_resolve_shapekey_object_strips_suffixes_after_virtual_name_resolution(self):
        body = types.SimpleNamespace(name="Body")
        _fake_bpy.data.objects["Body"] = body
        export_helper.ObjectPrefixHelper.resolve_source_object_name = (
            lambda name: "Body_chain1_copy" if name == "Hash.Body_chain1_copy" else name
        )

        result = export_helper.BlueprintExportHelper._resolve_shapekey_object_in_scene(
            "Hash.Body_chain1_copy"
        )

        self.assertIs(result, body)

    def test_collect_shapekey_objects_uses_combined_suffix_resolution(self):
        body = types.SimpleNamespace(name="Body")
        _fake_bpy.data.objects["Body"] = body
        export_helper.ObjectPrefixHelper.resolve_source_object_name = (
            lambda name: "Body_chain1_copy" if name == "Hash.Body_chain1_copy" else name
        )
        export_helper.BlueprintExportHelper.collect_connected_object_names = staticmethod(
            lambda _tree: ["Hash.Body_chain1_copy"]
        )

        result = export_helper.BlueprintExportHelper.collect_shapekey_objects(object())

        self.assertEqual(result, ["Body"])

    def test_ntmi_result_output_enables_shapekey_postprocess_scan(self):
        """测试 NTMI 结果输出节点启用形态键后处理扫描"""
        shapekey_node = _FakeNode("ShapeKeyPP", "SSMTNode_PostProcess_ShapeKey")
        output_node = _FakeNode(
            "NTMI Output",
            "SSMTNode_Result_Output_NTMIModImp",
            inputs=[_FakeSocket(from_node=shapekey_node, linked=True, bl_idname="SSMTSocketPostProcess")],
        )
        tree = _make_tree("NTMI_BP", output_node, shapekey_node)
        export_helper.BlueprintExportHelper.set_runtime_result_output_node_type("SSMTNode_Result_Output_NTMIModImp")

        self.assertTrue(export_helper.BlueprintExportHelper.has_shapekey_postprocess_node(tree))

        nodes = export_helper.BlueprintExportHelper.collect_shapekey_postprocess_nodes(tree)
        self.assertEqual([node.name for node in nodes], ["ShapeKeyPP"])

    def test_ntmi_result_output_prefix_fallback_works_without_runtime_override(self):
        """测试 NTMI 输出节点前缀回退在无运行时覆盖时正常工作"""
        shapekey_node = _FakeNode("ShapeKeyPP", "SSMTNode_PostProcess_ShapeKey")
        output_node = _FakeNode(
            "NTMI Output",
            "SSMTNode_Result_Output_NTMIModImp",
            inputs=[_FakeSocket(from_node=shapekey_node, linked=True, bl_idname="SSMTSocketPostProcess")],
        )
        tree = _make_tree("NTMI_BP", output_node, shapekey_node)
        export_helper.BlueprintExportHelper.runtime_result_output_node_type = ""

        self.assertTrue(export_helper.BlueprintExportHelper.has_shapekey_postprocess_node(tree))

    def test_ntmi_result_output_is_used_when_collecting_connected_shapekey_objects(self):
        """测试收集连接的形态键对象时使用 NTMI 输出节点"""
        body_obj = types.SimpleNamespace(name="Body")
        _fake_bpy.data.objects["Body"] = body_obj
        export_helper.ObjectPrefixHelper.resolve_source_object_name = lambda name: name

        object_info = _FakeNode("BodyInfo", "SSMTNode_Object_Info", object_name="Body")
        output_node = _FakeNode(
            "NTMI Output",
            "SSMTNode_Result_Output_NTMIModImp",
            inputs=[_FakeSocket(from_node=object_info, linked=True, bl_idname="SSMTSocketObject")],
        )
        tree = _make_tree("NTMI_BP", object_info, output_node)
        export_helper.BlueprintExportHelper.set_runtime_result_output_node_type("SSMTNode_Result_Output_NTMIModImp")
        export_helper.BlueprintExportHelper.collect_connected_object_names = staticmethod(lambda _tree: ["Body"])

        result = export_helper.BlueprintExportHelper.collect_shapekey_objects(tree)

        self.assertEqual(result, ["Body"])

    def test_collect_shapekey_objects_deduplicates_resolved_source_names(self):
        """测试 collect_shapekey_objects 对解析后的源名称去重"""
        _fake_bpy.data.objects["Body"] = types.SimpleNamespace(name="Body")
        export_helper.ObjectPrefixHelper.resolve_source_object_name = lambda name: "Body"
        export_helper.BlueprintExportHelper.collect_connected_object_names = staticmethod(
            lambda _tree: ["Hash.Body", "Body_copy"]
        )

        result = export_helper.BlueprintExportHelper.collect_shapekey_objects(tree=object())

        self.assertEqual(result, ["Body"])

    def test_generate_shapekey_classification_report_keeps_nested_blueprint_and_split_chain_outputs(self):
        """测试生成形态键分类报告包含嵌套蓝图和分割链的输出"""
        _fake_bpy.data.objects["Body"] = types.SimpleNamespace(
            name="Body",
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(
                    key_blocks=[
                        types.SimpleNamespace(name="Basis"),
                        types.SimpleNamespace(name="Smile"),
                    ]
                )
            ),
        )
        export_helper.BlueprintExportHelper.max_shapekey_slot_count = 0
        export_helper.BlueprintExportHelper.get_exportable_shape_key_infos = staticmethod(
            lambda obj, slot_limit=None: [(1, "Smile", object())] if getattr(obj, "name", "") == "Body" else []
        )

        chain_nested = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            object_name="LOD0.hash-0.Body_chain1_copy",
            original_object_name="Body",
            virtual_object_name="LOD0.hash-0.Body_chain1_copy",
            export_object_name_override="",
            rename_history=[],
            get_export_object_name=lambda: "LOD0.hash-0.Body_chain1_copy",
        )
        chain_split = types.SimpleNamespace(
            is_valid=True,
            reached_output=True,
            object_name="LOD0.hash-0.Body_chain2_copy",
            original_object_name="Body",
            virtual_object_name="LOD0.hash-0.Body_chain2_copy",
            export_object_name_override="",
            rename_history=[
                {"old_name": "Body", "new_name": "LOD0.hash-0.Body_chain2_copy"},
            ],
            get_export_object_name=lambda: "LOD0.hash-0.Body_chain2_copy",
        )
        blueprint_model = types.SimpleNamespace(processing_chains=[chain_nested, chain_split])

        report_generated = export_helper.BlueprintExportHelper.generate_shapekey_classification_report(blueprint_model)

        self.assertTrue(report_generated)
        report_text = _fake_bpy.data.texts["Shape_Key_Classification"]._value
        self.assertIn("物体: LOD0.hash-0.Body_chain1_copy", report_text)
        self.assertIn("物体: LOD0.hash-0.Body_chain2_copy", report_text)

    def test_get_exportable_shape_key_infos_excludes_basis_name(self):
        """测试 get_exportable_shape_key_infos 排除 Basis 形态键"""
        obj = types.SimpleNamespace(
            name="Body",
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(
                    key_blocks=[
                        types.SimpleNamespace(name="Basis", mute=False),
                        types.SimpleNamespace(name="Smile", mute=False),
                    ]
                )
            )
        )

        result = export_helper.BlueprintExportHelper.get_exportable_shape_key_infos(obj)

        self.assertEqual([(slot_index, shape_key_name) for slot_index, shape_key_name, _ in result], [(1, "Smile")])

    def test_get_exportable_shape_key_infos_skips_unchecked_node_items(self):
        """测试 get_exportable_shape_key_infos 跳过形态键配置节点中取消勾选的形态键"""
        shapekey_node = _FakeNode(
            "ShapeKeyPP",
            "SSMTNode_PostProcess_ShapeKey",
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="Frown", export_enabled=False),
            ],
        )
        output_node = _FakeNode(
            "Output",
            "SSMTNode_Result_Output",
            inputs=[_FakeSocket(from_node=shapekey_node, linked=True, bl_idname="SSMTSocketPostProcess")],
        )
        tree = _make_tree("BP_Filter", output_node, shapekey_node)

        export_helper.BlueprintExportHelper.get_exportable_shape_key_infos = staticmethod(
            _real_get_exportable_shape_key_infos
        )
        original_get_tree = export_helper.BlueprintExportHelper.get_current_blueprint_tree
        export_helper.BlueprintExportHelper.get_current_blueprint_tree = staticmethod(lambda context=None: tree)
        try:
            obj = types.SimpleNamespace(
                name="Body",
                data=types.SimpleNamespace(
                    shape_keys=types.SimpleNamespace(
                        key_blocks=[
                            types.SimpleNamespace(name="Basis", mute=False),
                            types.SimpleNamespace(name="Frown", mute=False),
                            types.SimpleNamespace(name="Smile", mute=False),
                        ]
                    )
                ),
            )

            result = export_helper.BlueprintExportHelper.get_exportable_shape_key_infos(obj)
        finally:
            export_helper.BlueprintExportHelper.get_current_blueprint_tree = original_get_tree

        # Frown 未勾选被跳过，槽位索引保持紧密
        self.assertEqual(
            [(slot_index, shape_key_name) for slot_index, shape_key_name, _ in result],
            [(1, "Smile")],
        )

    def test_get_exportable_shape_key_infos_unfiltered_without_blueprint_tree(self):
        """测试无有效蓝图树时 get_exportable_shape_key_infos 不过滤形态键"""
        export_helper.BlueprintExportHelper.get_exportable_shape_key_infos = staticmethod(
            _real_get_exportable_shape_key_infos
        )
        obj = types.SimpleNamespace(
            name="Body",
            data=types.SimpleNamespace(
                shape_keys=types.SimpleNamespace(
                    key_blocks=[
                        types.SimpleNamespace(name="Basis", mute=False),
                        types.SimpleNamespace(name="Frown", mute=False),
                    ]
                )
            ),
        )

        result = export_helper.BlueprintExportHelper.get_exportable_shape_key_infos(obj)

        self.assertEqual(
            [(slot_index, shape_key_name) for slot_index, shape_key_name, _ in result],
            [(1, "Frown")],
        )


    def test_collect_postprocess_nodes_preserves_chain_order(self):
        b_input = _FakeSocket(linked=True, bl_idname="SSMTSocketPostProcess")
        b = _FakeNode("B", "SSMTNode_PostProcess_B", inputs=[b_input])
        a_output = _FakeSocket(linked=True, bl_idname="SSMTSocketPostProcess")
        a = _FakeNode("A", "SSMTNode_PostProcess_A", outputs=[a_output])
        out_output = _FakeSocket(linked=True, bl_idname="SSMTSocketPostProcess")
        out = _FakeNode("Output", "SSMTNode_Result_Output", outputs=[out_output])
        out_output.links.append(types.SimpleNamespace(from_node=out, to_node=a))
        a_output.links.append(types.SimpleNamespace(from_node=a, to_node=b))
        tree = _make_tree("Chain", out, a, b)

        result = export_helper.BlueprintExportHelper._collect_postprocess_nodes(tree)

        self.assertEqual([node.name for node in result], ["A", "B"])

    def test_collect_postprocess_nodes_follows_reroute(self):
        reroute_input = _FakeSocket(linked=True, bl_idname="SSMTSocketPostProcess")
        reroute_output = _FakeSocket(linked=True, bl_idname="SSMTSocketPostProcess")
        reroute = _FakeNode(
            "Reroute",
            "NodeReroute",
            inputs=[reroute_input],
            outputs=[reroute_output],
        )
        a_input = _FakeSocket(linked=True, bl_idname="SSMTSocketPostProcess")
        a = _FakeNode("A", "SSMTNode_PostProcess_A", inputs=[a_input])
        out_output = _FakeSocket(linked=True, bl_idname="SSMTSocketPostProcess")
        out = _FakeNode("Output", "SSMTNode_Result_Output", outputs=[out_output])
        out_output.links.append(types.SimpleNamespace(from_node=out, to_node=reroute))
        reroute_output.links.append(types.SimpleNamespace(from_node=reroute, to_node=a))
        tree = _make_tree("RerouteChain", out, reroute, a)

        result = export_helper.BlueprintExportHelper._collect_postprocess_nodes(tree)

        self.assertEqual([node.name for node in result], ["A"])


if __name__ == "__main__":
    unittest.main()
