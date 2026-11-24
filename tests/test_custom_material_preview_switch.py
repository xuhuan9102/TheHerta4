# -*- coding: utf-8 -*-
"""材质转资源pro「贴图切换预览」：档位口径 / 循环 / 恢复 / 备份 / 视口着色。

需求（用户报告）
----------------
分组里要能一键预览贴图切换：点按钮就把对应部件的网格面切到那一套材质，
在视口里**实时**看到效果。

档位口径（本文件的主回归点）
----------------------------
生成侧 ``generate_material_lines`` 写出 ``if $swapkey == index``，index 是
``find_matching_materials()`` 的序号 —— 后者按**材质槽顺序**收集同前缀材质，
并按 ``_build_material_signature`` 去重。所以：

- 槽顺序 ``[DiffuseMap_b, DiffuseMap_a]`` 时，运行时档位 0 是 b 不是 a
  （扫描写下的 ``bindings`` 是**排序后**的名字，只标识分组身份，不能拿来当档位序）；
- 同签名的重复材质在生成侧会被丢掉，预览也必须跟着跳过，否则档位整体错位。

备份语义
--------
预览改的是 ``polygon.material_index``（真实数据），所以第一次预览某部件前必须
先备份原始索引（游程编码存进 ``node.switch_preview_backup``）；「恢复」写回的
是**预览前**的样子，而不是上一档。面数与备份不符（物体被编辑过）时放弃写回，
绝不写坏数据。
"""
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


PKG = "_custom_material_preview_switch_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint"):
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package


class _FakeOperator:
    def __init__(self):
        self.reports = []

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
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups=[])
sys.modules["bpy"] = _fake_bpy


class _StubMaterialBase:
    """生成侧口径的最小替身：签名默认按材质名（可被 material.signature 覆盖）。"""

    @staticmethod
    def _build_material_signature(material):
        signature = getattr(material, "signature", None)
        return signature if signature is not None else ("__name__", material.name)


_material_stub = types.ModuleType(f"{PKG}.blueprint.node_postprocess_material")
_material_stub.MATERIAL_DETECT_PRESETS = ["DiffuseMap", "NormalMap", "LightMap", "MaterialMap"]
_material_stub.SSMTNode_PostProcess_MaterialBase = _StubMaterialBase
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


# ---------------------------------------------------------------- 假数据模型


class _FakeMaterial:
    def __init__(self, name, signature=None):
        self.name = name
        self.signature = signature


class _FakeMaterialSlot:
    def __init__(self, material):
        self.material = material


class _FakePolygons:
    def __init__(self, indices):
        self.indices = list(indices)

    def __len__(self):
        return len(self.indices)

    def foreach_get(self, attr, seq):
        assert attr == "material_index", attr
        for index, value in enumerate(self.indices):
            seq[index] = value

    def foreach_set(self, attr, seq):
        assert attr == "material_index", attr
        self.indices = list(seq)


class _FakeMesh:
    def __init__(self, face_indices):
        self.polygons = _FakePolygons(face_indices)
        self.updates = 0

    def update(self):
        self.updates += 1


class _FakeObject:
    def __init__(self, name, material_names, face_indices, signatures=None):
        signatures = signatures or {}
        self.name = name
        self.type = "MESH"
        self.material_slots = [
            _FakeMaterialSlot(_FakeMaterial(name, signatures.get(name)))
            for name in material_names
        ]
        self.data = _FakeMesh(face_indices)
        self.hidden = False

    def hide_get(self):
        return self.hidden


PROPERTY_DEFAULTS = {
    "object_name": "",
    "switch_variable": "",
    "key": "N",
    "state_count": 2,
    "enabled": True,
    "comment": "",
    "bindings": "",
    "merge_group_id": "",
    "preview_state": -1,
}


class _FakeSwitchGroup:
    def __init__(self):
        object.__setattr__(self, "_alive", True)
        object.__setattr__(self, "_data", {})

    def invalidate(self):
        object.__setattr__(self, "_alive", False)
        object.__setattr__(self, "_data", {})

    def __getattr__(self, name):
        if name not in PROPERTY_DEFAULTS:
            raise AttributeError(name)
        if not object.__getattribute__(self, "_alive"):
            return PROPERTY_DEFAULTS[name]
        return object.__getattribute__(self, "_data").get(name, PROPERTY_DEFAULTS[name])

    def __setattr__(self, name, value):
        if name in ("_alive", "_data"):
            object.__setattr__(self, name, value)
            return
        object.__getattribute__(self, "_data")[name] = value


class _FakeGroupCollection:
    def __init__(self):
        self._items = []

    def add(self):
        item = _FakeSwitchGroup()
        self._items.append(item)
        return item

    def clear(self):
        for item in self._items:
            item.invalidate()
        self._items = []

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, index):
        return self._items[index]


class _FakeNode:
    bl_idname = "SSMTNode_PostProcess_CustomMaterialAssign"

    def __init__(self, name="MaterialPro"):
        self.name = name
        self.use_global_assign = True
        self.material_switch_var = "$swapkey150"
        self.global_switch_groups = _FakeGroupCollection()
        self.target_items = []
        self.id_data = None
        self.switch_preview_backup = ""
        self.preview_material_shading = True


class _FakeNodeList(list):
    def __init__(self, nodes):
        super().__init__(nodes)
        self._by_name = {node.name: node for node in nodes}

    def get(self, name):
        return self._by_name.get(name)


class _FakeBlueprintTree:
    def __init__(self, node):
        self.name = "ProbeBlueprint"
        self.nodes = _FakeNodeList([node])


class _FakeSpaceData:
    def __init__(self, tree):
        self.edit_tree = tree
        self.node_tree = tree


class _FakeContext:
    def __init__(self, tree, window_manager=None):
        self.space_data = _FakeSpaceData(tree)
        self.window_manager = window_manager


# ---------------------------------------------------------------- UI 假布局


class _FakeLayoutOperator:
    def __init__(self, idname, kwargs):
        self.idname = idname
        self.kwargs = dict(kwargs)


class _FakeLayoutRow:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.labels = []
        self.operators = []

    def label(self, **kwargs):
        self.labels.append(dict(kwargs))

    def operator(self, idname, **kwargs):
        button = _FakeLayoutOperator(idname, kwargs)
        self.operators.append(button)
        return button


class _FakeLayout:
    def __init__(self):
        self.rows = []
        self.labels = []

    def row(self, **kwargs):
        row = _FakeLayoutRow(**kwargs)
        self.rows.append(row)
        return row

    def label(self, **kwargs):
        self.labels.append(dict(kwargs))


# ---------------------------------------------------------------- 测试夹具


def _install_objects(*objects):
    module.bpy.data.objects.clear()
    for obj in objects:
        module.bpy.data.objects[obj.name] = obj


def _real_node(name="MaterialPro"):
    node = module.SSMTNode_PostProcess_CustomMaterialAssign()
    node.name = name
    node.use_global_assign = True
    node.material_switch_var = "$swapkey150"
    node.global_switch_groups = _FakeGroupCollection()
    node.target_items = []
    node.switch_preview_backup = ""
    node.preview_material_shading = True
    return node


def _seed_group(node, object_name, material_lists, variable="$swapkey150", state_count=None):
    group = node.global_switch_groups.add()
    group.object_name = object_name
    group.switch_variable = variable
    group.merge_group_id = variable
    group.state_count = state_count or max(len(names) for names in material_lists)
    group.bindings = json.dumps([sorted(names) for names in material_lists])
    return group


def _preview(node, variable, state):
    operator = module.SSMT_OT_CustomMaterialPreviewSwitch()
    operator.node_name = node.name
    operator.switch_variable = variable
    operator.state = state
    return operator.execute(_FakeContext(_FakeBlueprintTree(node)))


def _faces(obj):
    return list(obj.data.polygons.indices)


def _backup(node):
    return json.loads(node.switch_preview_backup or "{}")


class PreviewSwitchTestCase(unittest.TestCase):
    def setUp(self):
        module._preview_shading_backup.clear()
        module.bpy.data.objects.clear()
        module.bpy.data.node_groups = []


class TestPreviewStateMapping(PreviewSwitchTestCase):
    def test_faces_follow_the_selected_state(self):
        obj = _FakeObject(
            "Body",
            ["DiffuseMap_body", "NormalMap_body", "DiffuseMap_body.001", "NormalMap_body.001"],
            [0, 1, 0, 1],
        )
        _install_objects(obj)
        node = _real_node()
        _seed_group(
            node,
            obj.name,
            [["DiffuseMap_body", "DiffuseMap_body.001"], ["NormalMap_body", "NormalMap_body.001"]],
        )

        result = _preview(node, "$swapkey150", 1)

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(_faces(obj), [2, 3, 2, 3])
        self.assertEqual(obj.data.updates, 1)

    def test_state_index_follows_material_slot_order_not_name_order(self):
        """槽顺序 [b, a] → 档位 0 是 b（按名字排序会误判成 a，预览整体错位）。"""
        obj = _FakeObject(
            "Cape",
            ["DiffuseMap_b", "DiffuseMap_a"],
            [1, 1],
        )
        _install_objects(obj)
        node = _real_node()
        _seed_group(node, obj.name, [["DiffuseMap_b", "DiffuseMap_a"]])

        _preview(node, "$swapkey150", 0)

        self.assertEqual(_faces(obj), [0, 0])

    def test_duplicate_signature_materials_are_skipped_like_the_exporter(self):
        """同签名材质在生成侧会被丢掉，档位序必须跟着跳，否则第 2 档指错贴图。"""
        obj = _FakeObject(
            "Face",
            ["DiffuseMap_颜", "DiffuseMap_颜.001", "DiffuseMap_颜.002"],
            [0, 0],
            signatures={
                "DiffuseMap_颜": ("same",),
                "DiffuseMap_颜.001": ("same",),
                "DiffuseMap_颜.002": ("other",),
            },
        )
        _install_objects(obj)
        node = _real_node()
        _seed_group(
            node,
            obj.name,
            [["DiffuseMap_颜", "DiffuseMap_颜.001", "DiffuseMap_颜.002"]],
        )

        _preview(node, "$swapkey150", 1)

        # 生成侧第 1 档 = 去重后的第二个材质 = 槽 2
        self.assertEqual(_faces(obj), [2, 2])

    def test_prefixes_outside_the_group_binding_are_left_alone(self):
        obj = _FakeObject(
            "Body",
            ["DiffuseMap_body", "LightMap_body", "DiffuseMap_body.001", "LightMap_body.001"],
            [0, 1],
        )
        _install_objects(obj)
        node = _real_node()
        _seed_group(node, obj.name, [["DiffuseMap_body", "DiffuseMap_body.001"]])

        _preview(node, "$swapkey150", 1)

        self.assertEqual(_faces(obj), [2, 1])

    def test_single_state_prefix_keeps_last_material_instead_of_overflowing(self):
        obj = _FakeObject(
            "Body",
            ["DiffuseMap_body", "DiffuseMap_body.001", "NormalMap_body"],
            [0, 2],
        )
        _install_objects(obj)
        node = _real_node()
        _seed_group(
            node,
            obj.name,
            [["DiffuseMap_body", "DiffuseMap_body.001"], ["NormalMap_body"]],
        )

        _preview(node, "$swapkey150", 1)

        self.assertEqual(_faces(obj), [1, 2])  # NormalMap 只有一套 → 保持不变


class TestPreviewCycleAndRestore(PreviewSwitchTestCase):
    def _triangle_object(self):
        obj = _FakeObject(
            "Skirt",
            ["DiffuseMap_skirt_0", "DiffuseMap_skirt_1", "DiffuseMap_skirt_2"],
            [0, 0, 0],
        )
        _install_objects(obj)
        node = _real_node()
        group = _seed_group(
            node,
            obj.name,
            [["DiffuseMap_skirt_0", "DiffuseMap_skirt_1", "DiffuseMap_skirt_2"]],
        )
        return node, group, obj

    def test_cycle_skips_the_default_state_then_wraps(self):
        node, group, obj = self._triangle_object()

        _preview(node, "$swapkey150", module.PREVIEW_STATE_CYCLE)
        self.assertEqual(_faces(obj), [1, 1, 1])
        self.assertEqual(group.preview_state, 1)

        _preview(node, "$swapkey150", module.PREVIEW_STATE_CYCLE)
        self.assertEqual(_faces(obj), [2, 2, 2])
        self.assertEqual(group.preview_state, 2)

        _preview(node, "$swapkey150", module.PREVIEW_STATE_CYCLE)
        self.assertEqual(_faces(obj), [0, 0, 0])
        self.assertEqual(group.preview_state, 0)

    def test_restore_writes_back_before_preview_not_previous_state(self):
        node, group, obj = self._triangle_object()

        _preview(node, "$swapkey150", 1)
        _preview(node, "$swapkey150", 2)
        self.assertEqual(_faces(obj), [2, 2, 2])

        _preview(node, "$swapkey150", module.PREVIEW_STATE_NONE)

        self.assertEqual(_faces(obj), [0, 0, 0])
        self.assertEqual(group.preview_state, module.PREVIEW_STATE_NONE)
        self.assertEqual(node.switch_preview_backup, "")

    def test_backup_is_written_once_for_the_first_preview(self):
        node, _group, _obj = self._triangle_object()

        _preview(node, "$swapkey150", 1)
        first_backup = node.switch_preview_backup
        _preview(node, "$swapkey150", 2)

        self.assertTrue(first_backup)
        self.assertEqual(node.switch_preview_backup, first_backup)
        self.assertEqual(_backup(node)["Skirt"], [[0, 3]])

    def test_restore_all_clears_every_group_and_object(self):
        body = _FakeObject("Body", ["DiffuseMap_body", "DiffuseMap_body.001"], [0, 0])
        cape = _FakeObject("Cape", ["DiffuseMap_cape", "DiffuseMap_cape.001"], [0, 0])
        _install_objects(body, cape)
        node = _real_node()
        _seed_group(node, body.name, [["DiffuseMap_body", "DiffuseMap_body.001"]], "$swapkey150")
        _seed_group(node, cape.name, [["DiffuseMap_cape", "DiffuseMap_cape.001"]], "$swapkey151")

        _preview(node, "$swapkey150", 1)
        _preview(node, "$swapkey151", 1)
        self.assertEqual(_faces(body), [1, 1])
        self.assertEqual(_faces(cape), [1, 1])

        operator = module.SSMT_OT_CustomMaterialPreviewRestoreAll()
        operator.node_name = node.name
        result = operator.execute(_FakeContext(_FakeBlueprintTree(node)))

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(_faces(body), [0, 0])
        self.assertEqual(_faces(cape), [0, 0])
        self.assertEqual(node.switch_preview_backup, "")
        self.assertTrue(
            all(group.preview_state == module.PREVIEW_STATE_NONE for group in node.global_switch_groups)
        )

    def test_preview_reports_missing_and_hidden_parts_without_crashing(self):
        ghost = _FakeObject("Ghost", ["DiffuseMap_ghost", "DiffuseMap_ghost.001"], [0, 0])
        ghost.hidden = True
        _install_objects(ghost)
        node = _real_node()
        _seed_group(node, ghost.name, [["DiffuseMap_ghost", "DiffuseMap_ghost.001"]])
        _seed_group(node, "NeverScanned", [["DiffuseMap_x", "DiffuseMap_x.001"]], "$swapkey151")

        operator = module.SSMT_OT_CustomMaterialPreviewSwitch()
        operator.node_name = node.name
        operator.switch_variable = "$swapkey151"
        operator.state = 1
        result = operator.execute(_FakeContext(_FakeBlueprintTree(node)))

        self.assertEqual(result, {"FINISHED"})
        self.assertTrue(operator.reports)
        self.assertIn("未找到", operator.reports[-1][1])
        # 找不到的部件不该被标成"预览中"（否则 UI 会显示一个假档位）
        self.assertEqual(
            node.global_switch_groups[1].preview_state, module.PREVIEW_STATE_NONE
        )

        _preview(node, "$swapkey150", 1)
        self.assertEqual(_faces(ghost), [1, 1])

    def test_preview_of_unknown_variable_is_cancelled(self):
        node = _real_node()
        _seed_group(node, "Body", [["DiffuseMap_body", "DiffuseMap_body.001"]])

        result = _preview(node, "$swapkey999", 1)

        self.assertEqual(result, {"CANCELLED"})

    def test_restore_drops_backup_when_mesh_was_edited(self):
        obj = _FakeObject("Body", ["DiffuseMap_body", "DiffuseMap_body.001"], [0, 0, 0])
        _install_objects(obj)
        node = _real_node()
        _seed_group(node, obj.name, [["DiffuseMap_body", "DiffuseMap_body.001"]])
        _preview(node, "$swapkey150", 1)

        obj.data.polygons.indices = [1, 1]  # 用户删面：面数与备份不符
        _preview(node, "$swapkey150", module.PREVIEW_STATE_NONE)

        self.assertEqual(_faces(obj), [1, 1])  # 不写坏数据
        self.assertEqual(node.switch_preview_backup, "")
        self.assertEqual(node.global_switch_groups[0].preview_state, module.PREVIEW_STATE_NONE)


class TestPreviewBackupEncoding(PreviewSwitchTestCase):
    def test_run_length_round_trip(self):
        indices = [0, 0, 1, 1, 1, 2, 0]
        runs = module._material_runs(indices)

        self.assertEqual(runs, [[0, 2], [1, 3], [2, 1], [0, 1]])
        self.assertEqual(module._runs_to_indices(runs, len(indices)), indices)

    def test_run_length_rejects_wrong_face_count_and_bad_shapes(self):
        self.assertIsNone(module._runs_to_indices([[0, 2]], 3))
        self.assertIsNone(module._runs_to_indices([[0, 0]], 0))
        self.assertIsNone(module._runs_to_indices([[0]], 1))
        self.assertIsNone(module._runs_to_indices("nope", 1))


class TestPreviewButtons(PreviewSwitchTestCase):
    def test_every_state_gets_a_button_with_the_group_variable(self):
        node = _real_node()
        group = _seed_group(
            node,
            "Body",
            [["DiffuseMap_body", "DiffuseMap_body.001", "DiffuseMap_body.002"]],
        )
        layout = _FakeLayout()

        node._draw_switch_preview_row(layout, group)

        row = layout.rows[0]
        self.assertEqual(row.labels[0]["text"], "贴图预览")
        self.assertEqual(
            [button.kwargs["text"] for button in row.operators], ["1", "2", "3"]
        )
        for index, button in enumerate(row.operators):
            self.assertEqual(button.idname, "ssmt.custom_material_preview_switch")
            self.assertEqual(button.node_name, node.name)
            self.assertEqual(button.switch_variable, "$swapkey150")
            self.assertEqual(button.state, index)
            self.assertFalse(button.kwargs["depress"])

    def test_current_state_is_depressed_and_restore_button_appears(self):
        node = _real_node()
        group = _seed_group(
            node,
            "Body",
            [["DiffuseMap_body", "DiffuseMap_body.001"]],
        )
        group.preview_state = 1
        layout = _FakeLayout()

        node._draw_switch_preview_row(layout, group)

        row = layout.rows[0]
        self.assertTrue(row.operators[1].kwargs["depress"])
        restore = row.operators[-1]
        self.assertEqual(restore.kwargs["icon"], "LOOP_BACK")
        self.assertEqual(restore.state, module.PREVIEW_STATE_NONE)
        self.assertTrue(any("预览中" in label["text"] for label in layout.labels))

    def test_many_states_fall_back_to_one_cycle_button(self):
        node = _real_node()
        group = _seed_group(
            node,
            "Cape",
            [["DiffuseMap_cape_%d" % index for index in range(8)]],
            state_count=8,
        )
        layout = _FakeLayout()

        node._draw_switch_preview_row(layout, group)

        row = layout.rows[0]
        self.assertEqual(len(row.operators), 1)
        self.assertEqual(row.operators[0].state, module.PREVIEW_STATE_CYCLE)
        self.assertEqual(row.operators[0].kwargs["text"], "1/8")

    def test_group_without_variable_draws_nothing(self):
        node = _real_node()
        group = _FakeSwitchGroup()
        layout = _FakeLayout()

        node._draw_switch_preview_row(layout, group)

        self.assertEqual(layout.rows, [])


def _shading_context(types_per_space):
    spaces = [
        types.SimpleNamespace(shading=types.SimpleNamespace(type=shading_type))
        for shading_type in types_per_space
    ]
    area = types.SimpleNamespace(type="VIEW_3D", name="View3D", spaces=spaces)
    screen = types.SimpleNamespace(name="Layout", areas=[area])
    window = types.SimpleNamespace(screen=screen)
    return types.SimpleNamespace(
        window_manager=types.SimpleNamespace(windows=[window])
    ), spaces


class TestPreviewViewportShading(PreviewSwitchTestCase):
    def test_solid_viewport_is_switched_to_material_and_restored(self):
        node = _real_node()
        context, spaces = _shading_context(["SOLID"])

        module._ensure_preview_material_shading(node, context)
        self.assertEqual(spaces[0].shading.type, "MATERIAL")

        module._restore_preview_material_shading(node, context)
        self.assertEqual(spaces[0].shading.type, "SOLID")

    def test_existing_material_viewport_leaves_user_shading_untouched(self):
        node = _real_node()
        context, spaces = _shading_context(["MATERIAL", "SOLID"])

        module._ensure_preview_material_shading(node, context)

        self.assertEqual([space.shading.type for space in spaces], ["MATERIAL", "SOLID"])

    def test_manual_switch_during_preview_is_not_recorded_as_original(self):
        node = _real_node()
        context, spaces = _shading_context(["SOLID"])

        module._ensure_preview_material_shading(node, context)
        spaces[0].shading.type = "SOLID"  # 用户手动切回去
        module._ensure_preview_material_shading(node, context)
        module._restore_preview_material_shading(node, context)

        self.assertEqual(spaces[0].shading.type, "SOLID")

    def test_option_off_keeps_shading(self):
        node = _real_node()
        node.preview_material_shading = False
        context, spaces = _shading_context(["SOLID"])

        module._ensure_preview_material_shading(node, context)

        self.assertEqual(spaces[0].shading.type, "SOLID")


class TestScanRestoresPreview(PreviewSwitchTestCase):
    """重新扫描会重建分组：预览过的部件必须先还原，否则预览档位被带进新分组。"""

    def test_scan_restores_previewed_faces(self):
        obj = _FakeObject("Body", ["DiffuseMap_body", "DiffuseMap_body.001"], [0, 0])
        _install_objects(obj)
        node = _real_node()
        tree = _FakeBlueprintTree(node)
        node.id_data = tree
        module.bpy.data.node_groups = [tree]
        _seed_group(node, obj.name, [["DiffuseMap_body", "DiffuseMap_body.001"]])

        _preview(node, "$swapkey150", 1)
        self.assertEqual(_faces(obj), [1, 1])

        with mock.patch.object(
            module, "_connected_blueprint_object_names", return_value=[obj.name]
        ):
            scan = module.SSMT_OT_CustomMaterialScanSwitches()
            scan.node_name = node.name
            result = scan.execute(_FakeContext(tree))

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(_faces(obj), [0, 0])
        self.assertEqual(node.switch_preview_backup, "")
        self.assertEqual(node.global_switch_groups[0].preview_state, module.PREVIEW_STATE_NONE)

    def test_clear_restores_previewed_faces(self):
        obj = _FakeObject("Body", ["DiffuseMap_body", "DiffuseMap_body.001"], [0, 0])
        _install_objects(obj)
        node = _real_node()
        tree = _FakeBlueprintTree(node)
        node.id_data = tree
        module.bpy.data.node_groups = [tree]
        _seed_group(node, obj.name, [["DiffuseMap_body", "DiffuseMap_body.001"]])

        _preview(node, "$swapkey150", 1)

        clear = module.SSMT_OT_CustomMaterialClearSwitches()
        clear.node_name = node.name
        result = clear.execute(_FakeContext(tree))

        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(_faces(obj), [0, 0])
        self.assertEqual(node.switch_preview_backup, "")


if __name__ == "__main__":
    unittest.main()
