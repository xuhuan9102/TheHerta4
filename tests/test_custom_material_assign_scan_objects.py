# -*- coding: utf-8 -*-
"""材质转资源pro「扫描蓝图物体」按钮（`SSMT_OT_CustomMaterialAssignScanObjects`）。

被测的三件事
------------
1. **链路方向**：后处理节点的输入口（``SSMTSocketPostProcess``）连着结果输出节点，
   所以从本节点沿 **inputs** 向上走才能找到 ``SSMTNode_Result_Output``。
   这是 ``SSMTNode_Result_Output.init`` 的既定拓扑
   （``inputs.new('SSMTSocketObject', "Group 1")`` +
   ``outputs.new('SSMTSocketPostProcess', "Post Process")``），
   与 ``node_postprocess_material.py`` 的 Material Detect 同方向。
   本文件用「反方向连的图」把它钉死，防止有人"顺手改成 outputs"。
2. **收集口径**：物体信息节点取 ``object_name``、多文件导出节点取 ``object_list``、
   嵌套蓝图进它的结果输出节点继续走；按出现顺序去重。
3. **静音节点必须跳过**：``mute`` 在 Blender 里等同停用、不参与导出，
   同文件既有的 ``_connected_blueprint_object_names`` 正是
   ``if node.bl_idname not in SOURCE_IDS or node.mute: continue``。
   新扫描器最初漏了这条 —— 静音的物体信息 / 多文件导出 / 嵌套蓝图会被一并收进
   部件输入框（列表里多出永远导不出的死条目），本文件即该回归。

装载方式沿用 ``tests/test_custom_material_scan_switches.py`` 的 fake-package 范式：
只打桩 ``bpy`` 与 ``blueprint.node_postprocess_material``，其余加载真实模块。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


PKG = "_custom_material_assign_scan_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package


class _FakeOperator:
    def report(self, levels, message):
        self.reports.append((set(levels), message))


_fake_bpy = types.ModuleType("bpy")
_fake_bpy.types = types.SimpleNamespace(
    PropertyGroup=object,
    Operator=_FakeOperator,
    Object=object,
)
_fake_bpy.props = types.SimpleNamespace(
    StringProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
    PointerProperty=lambda **_kwargs: None,
)
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups={})
sys.modules["bpy"] = _fake_bpy

_material_stub = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_stub.MATERIAL_DETECT_PRESETS = ["DiffuseMap", "NormalMap", "LightMap", "MaterialMap"]
_material_stub.SSMTNode_PostProcess_MaterialBase = type(
    "_StubMaterialBase",
    (object,),
    {"define_swapkeys_in_sections": lambda self, sections, keys_to_define: None},
)
sys.modules[_material_stub.__name__] = _material_stub

_module_path = (
    Path(__file__).resolve().parents[1]
    / "blueprint"
    / "node_postprocess_custom_material_assign.py"
)
_spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.node_postprocess_custom_material_assign", _module_path
)
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)


POST_PROCESS_SOCKET = "SSMTSocketPostProcess"
OBJECT_SOCKET = "SSMTSocketObject"
RESULT_OUTPUT_IDNAME = "SSMTNode_Result_Output"
OBJECT_INFO_IDNAME = "SSMTNode_Object_Info"
MULTIFILE_IDNAME = "SSMTNode_MultiFile_Export"
NEST_IDNAME = "SSMTNode_Blueprint_Nest"
OBJECT_GROUP_IDNAME = "SSMTNode_Object_Group"


# --- 假节点图 ---------------------------------------------------------------


class _FakeNodes(list):
    """Blender 的 ``tree.nodes``：既能迭代，又有 ``.get(name)``。"""

    def get(self, name):
        for node in self:
            if getattr(node, "name", "") == name:
                return node
        return None


class _FakeLink:
    def __init__(self, from_node):
        self.from_node = from_node


class _FakeSocket:
    def __init__(self, bl_idname, upstream=None):
        self.bl_idname = bl_idname
        self.links = [_FakeLink(upstream)] if upstream is not None else []

    @property
    def is_linked(self):
        return bool(self.links)


class _FakeNode:
    def __init__(self, bl_idname, name, mute=False, **attrs):
        self.bl_idname = bl_idname
        self.name = name
        self.mute = mute
        self.inputs = []
        self.outputs = []
        self.id_data = None
        for key, value in attrs.items():
            setattr(self, key, value)


class _FakeTree:
    bl_idname = "SSMTBlueprintTreeType"

    def __init__(self, name, nodes=()):
        self.name = name
        self.nodes = _FakeNodes()
        for node in nodes:
            self.add(node)

    def add(self, node):
        node.id_data = self
        self.nodes.append(node)
        return node


class _FakeTargetItem:
    def __init__(self, target_object=None):
        self.target_object = target_object


class _FakeTargetItems(list):
    def add(self):
        item = _FakeTargetItem()
        self.append(item)
        return item


class _FakeObject:
    """真实 Blender 物体是可哈希的（`{item.target_object for ...}` 依赖这一点）。"""

    def __init__(self, name, obj_type="MESH"):
        self.name = name
        self.type = obj_type


# --- 构图辅助 ---------------------------------------------------------------


def _link_upstream(upstream, downstream, socket_name=POST_PROCESS_SOCKET):
    """把 upstream 接到 downstream 的输入口（= 数据流向 downstream）。"""
    downstream.inputs.append(_FakeSocket(socket_name, upstream))
    return downstream


def _object_info(name, object_name, mute=False):
    return _FakeNode(OBJECT_INFO_IDNAME, name, mute=mute, object_name=object_name)


def _multifile(name, names, mute=False):
    node = _FakeNode(MULTIFILE_IDNAME, name, mute=mute)
    node.object_list = [types.SimpleNamespace(object_name=item) for item in names]
    return node


def _nest(name, blueprint_name, mute=False):
    return _FakeNode(NEST_IDNAME, name, mute=mute, blueprint_name=blueprint_name)


def _group(name, mute=False):
    return _FakeNode(OBJECT_GROUP_IDNAME, name, mute=mute)


def _result_output(name="Result_Output"):
    return _FakeNode(RESULT_OUTPUT_IDNAME, name)


def _assign(name="Assign", use_global_assign=False, items=()):
    node = _FakeNode(
        module.NODE_IDNAME,
        name,
        use_global_assign=use_global_assign,
    )
    node.target_items = _FakeTargetItems(items)
    node.active_target_index = -1
    return node


def _context(tree):
    return types.SimpleNamespace(space_data=types.SimpleNamespace(edit_tree=tree))


def _run_scan(tree, node_name="Assign"):
    operator = module.SSMT_OT_CustomMaterialAssignScanObjects()
    operator.reports = []
    operator.node_name = node_name
    result = operator.execute(_context(tree))
    return operator, result


def _last_report(operator):
    return operator.reports[-1][1] if operator.reports else ""


class _BaseCase(unittest.TestCase):
    def setUp(self):
        _fake_bpy.data.objects.clear()
        _fake_bpy.data.node_groups.clear()

    def add_object(self, name, obj_type="MESH"):
        obj = _FakeObject(name, obj_type)
        _fake_bpy.data.objects[name] = obj
        return obj


# --- 1. 链路方向 ------------------------------------------------------------


class FindResultOutputTests(_BaseCase):
    def test_walks_up_inputs_through_postprocess_nodes(self):
        result_output = _result_output()
        cleanup = _FakeNode("SSMTNode_PostProcess_BufferCleanup", "Cleanup")
        assign = _assign()
        _link_upstream(result_output, cleanup)
        _link_upstream(cleanup, assign)

        tree = _FakeTree("蓝图A", [result_output, cleanup, assign])
        self.assertIs(module._find_connected_result_output(assign), result_output)
        self.assertIs(module._find_connected_result_output(cleanup), result_output)
        self.assertTrue(tree)  # 树只是让 id_data 就位

    def test_reversed_connection_方向反了不应命中(self):
        """把结果输出接在 *输出* 口（方向反）时必须找不到 —— 锁死拓扑假设。"""
        result_output = _result_output()
        assign = _assign()
        # 反方向：assign 的「输入」口连着 result_output 的下游（即没有上游）
        assign.inputs.append(_FakeSocket(POST_PROCESS_SOCKET, None))
        _FakeTree("蓝图A", [result_output, assign])

        self.assertIsNone(module._find_connected_result_output(assign))

    def test_returns_none_when_node_is_not_in_chain(self):
        result_output = _result_output()
        assign = _assign()
        _FakeTree("蓝图A", [result_output, assign])

        self.assertIsNone(module._find_connected_result_output(assign))

    def test_all_result_output_idnames_are_recognised(self):
        for idname in sorted(module._RESULT_OUTPUT_IDNAMES):
            with self.subTest(idname=idname):
                start = _FakeNode("SSMTNode_PostProcess_BufferCleanup", "Cleanup")
                output = _FakeNode(idname, "Out")
                _link_upstream(output, start)
                _FakeTree("蓝图A", [output, start])
                self.assertIs(module._find_connected_result_output(start), output)

    def test_find_tree_result_output_uses_first_match(self):
        first = _result_output("First")
        second = _result_output("Second")
        tree = _FakeTree("蓝图A", [first, second])

        self.assertIs(module._find_tree_result_output(tree), first)

    def test_find_tree_result_output_returns_none_without_output_node(self):
        tree = _FakeTree("蓝图A", [_FakeNode(OBJECT_GROUP_IDNAME, "G")])

        self.assertIsNone(module._find_tree_result_output(tree))


# --- 2. 收集口径 ------------------------------------------------------------


class CollectObjectNamesTests(_BaseCase):
    def _graph_owner(self, group):
        """把 group 挂到结果输出下面，返回结果输出节点。"""
        result_output = _result_output()
        _link_upstream(group, result_output, socket_name=OBJECT_SOCKET)
        _FakeTree("蓝图A", [result_output, group])
        return result_output

    def test_collects_object_info_multifile_and_nested_in_order(self):
        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I1", "Body")))
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _multifile("MF", ["Cloth", "Body", "Light"])))

        nested_output = _result_output("NestedOut")
        nested_output.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I2", "Wing")))
        nested_tree = _FakeTree("Nested", [nested_output])
        _fake_bpy.data.node_groups["Nested"] = nested_tree
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _nest("N", "Nested")))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, ["Body", "Cloth", "Light", "Wing"])

    def test_skips_muted_object_info(self):
        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I1", "Body")))
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I2", "Horn", mute=True)))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, ["Body"])

    def test_skips_muted_multifile_export(self):
        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _multifile("MF", ["Body"], mute=True)))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, [])

    def test_skips_muted_nested_blueprint(self):
        nested_output = _result_output("NestedOut")
        nested_output.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I2", "Wing")))
        _fake_bpy.data.node_groups["Nested"] = _FakeTree("Nested", [nested_output])

        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _nest("N", "Nested", mute=True)))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, [])

    def test_muted_intermediate_node_does_not_cut_traversal(self):
        """静音的是"中转"（物体组）时，链路仍要走通，只是它自己不贡献名字。"""
        group = _group("G")
        muted_group = _group("G2", mute=True)
        muted_group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I1", "Body")))
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, muted_group))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, ["Body"])

    def test_ignores_empty_and_whitespace_names(self):
        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I1", "  ")))
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I2", "")))
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I3", " Body ")))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, ["Body"])

    def test_ignores_nest_with_none_blueprint(self):
        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _nest("N", "NONE")))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, [])

    def test_cycle_between_groups_terminates(self):
        group = _group("G1")
        second = _group("G2")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I1", "Body")))
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, second))
        second.inputs.append(_FakeSocket(OBJECT_SOCKET, group))

        names = module._collect_connected_object_names(self._graph_owner(group))

        self.assertEqual(names, ["Body"])


# --- 3. 算子行为 ------------------------------------------------------------


class ScanObjectsOperatorTests(_BaseCase):
    def _tree_with_chain(self, assign=None, use_global_assign=False):
        result_output = _result_output()
        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I1", "Body")))
        _link_upstream(group, result_output, socket_name=OBJECT_SOCKET)

        assign = assign or _assign(use_global_assign=use_global_assign)
        _link_upstream(result_output, assign)
        return _FakeTree("蓝图A", [result_output, group, assign])

    def test_adds_mesh_objects_in_order(self):
        self.add_object("Body")
        self.add_object("Horn")
        assign = _assign()
        assign.inputs.append(_FakeSocket(POST_PROCESS_SOCKET, None))
        tree = self._tree_with_chain(assign=assign)

        operator, result = _run_scan(tree)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(
            [item.target_object.name for item in assign.target_items],
            ["Body"],
        )
        self.assertEqual(_last_report(operator), "扫描到 1 个物体，新增 1 个部件")

    def test_skips_missing_and_non_mesh_and_reports_both_reasons(self):
        self.add_object("Body")
        self.add_object("Light", obj_type="LIGHT")
        result_output = _result_output()
        group = _group("G")
        for name in ("Body", "Ghost", "Light"):
            group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I" + name, name)))
        _link_upstream(group, result_output, socket_name=OBJECT_SOCKET)
        assign = _assign()
        _link_upstream(result_output, assign)
        tree = _FakeTree("蓝图A", [result_output, group, assign])

        operator, result = _run_scan(tree)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([item.target_object.name for item in assign.target_items], ["Body"])
        self.assertEqual(
            _last_report(operator),
            "扫描到 3 个物体，新增 1 个部件（1 个已不在场景中，1 个不是网格，跳过）",
        )

    def test_reuses_empty_slots_instead_of_appending(self):
        body = self.add_object("Body")
        existing_item = _FakeTargetItem(target_object=None)
        assign = _assign(items=[_FakeTargetItem(target_object=body), existing_item])
        tree = self._tree_with_chain(assign=assign)

        operator, result = _run_scan(tree)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(len(assign.target_items), 2, "不应追加新条目")
        self.assertEqual(_last_report(operator), "扫描到 1 个物体，新增 0 个部件")

    def test_second_run_is_idempotent(self):
        self.add_object("Body")
        assign = _assign()
        tree = self._tree_with_chain(assign=assign)

        _run_scan(tree)
        first = [item.target_object.name for item in assign.target_items]
        operator, _ = _run_scan(tree)

        self.assertEqual(first, ["Body"])
        self.assertEqual([item.target_object.name for item in assign.target_items], ["Body"])
        self.assertEqual(_last_report(operator), "扫描到 1 个物体，新增 0 个部件")

    def test_active_index_points_at_last_item(self):
        self.add_object("Body")
        assign = _assign(items=[_FakeTargetItem(target_object=None), _FakeTargetItem()])
        tree = self._tree_with_chain(assign=assign)

        _run_scan(tree)

        self.assertEqual(assign.active_target_index, len(assign.target_items) - 1)

    def test_global_assign_mode_cancels(self):
        self.add_object("Body")
        assign = _assign(use_global_assign=True)
        tree = self._tree_with_chain(assign=assign)

        operator, result = _run_scan(tree)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(assign.target_items, [])
        self.assertEqual(_last_report(operator), "全局指定模式下不需要部件输入框")

    def test_falls_back_to_tree_result_output_when_unconnected(self):
        self.add_object("Body")
        result_output = _result_output()
        group = _group("G")
        group.inputs.append(_FakeSocket(OBJECT_SOCKET, _object_info("I1", "Body")))
        _link_upstream(group, result_output, socket_name=OBJECT_SOCKET)
        assign = _assign()  # 故意不接进链路
        tree = _FakeTree("蓝图A", [result_output, group, assign])

        operator, result = _run_scan(tree)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual([item.target_object.name for item in assign.target_items], ["Body"])
        self.assertIn("新增 1 个部件", _last_report(operator))

    def test_cancels_when_tree_has_no_result_output(self):
        assign = _assign()
        tree = _FakeTree("蓝图A", [assign])

        operator, result = _run_scan(tree)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(_last_report(operator), "当前蓝图里没有结果输出节点，无法扫描")

    def test_cancels_when_result_output_has_no_object_nodes(self):
        result_output = _result_output()
        assign = _assign()
        _link_upstream(result_output, assign)
        tree = _FakeTree("蓝图A", [result_output, assign])

        operator, result = _run_scan(tree)

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(_last_report(operator), "蓝图里没有已连接的物体信息节点")

    def test_cancels_when_node_name_not_found(self):
        tree = self._tree_with_chain()

        operator, result = _run_scan(tree, node_name="NotThere")

        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(_last_report(operator), "没有找到材质转资源pro 节点")


if __name__ == "__main__":
    unittest.main()
