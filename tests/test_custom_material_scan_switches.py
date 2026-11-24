# -*- coding: utf-8 -*-
"""材质转资源pro「全局扫描贴图切换」：切换身份 / 重扫幂等 / 旧设置保留 / 变量号不撞车。

切换身份（本文件的主回归点）
----------------------------
原版「材质转资源」（现 ``SSMTNode_PostProcess_MaterialBase.generate_material_lines``）
用 ``tuple(sorted(mat.name ...))`` 作 ``material_group_to_swapkey`` 的 key：

    mat_names_tuple = tuple(sorted([mat.name for mat in matching_materials]))
    if mat_names_tuple not in material_group_to_swapkey: ... 领新号 ...

即**只有同一套材质**才复用同一个 ``$swapkeyN``，材质不同就各自领号。
pro 节点的扫描在 18ab9ba 把身份换成了"套数形状" ``_group_shape``
（``(state_count, 各前缀长度的排序元组)``），于是**套数相同但贴图完全不同**
的部件被并成一组 —— 实机（露西·公主假日）里 脸部 与 身体 都是 2 套 DiffuseMap，
于是共用 ``$swapkey150``，按一次键连脸一起换。

Blender 语义（另一条回归线，实测见 .dbg/cg_probe_fields.py，Blender 5.0.1 headless）
-------------------------------------------------------------------------------
全局分支原先在 ``node.global_switch_groups.clear()`` **之后**才读旧组字段做快照。
CollectionProperty 移除项后，残留的 Python 引用既不抛 ``ReferenceError`` 也不保留
数据，而是**静默读回属性默认值**：

    state_count -> 2, bindings -> "", comment -> "", key -> "",
    enabled -> True, object_name -> ""
    （若期间 add 了新项，旧引用还会"附身"到新项内存上）

于是第二次扫描时每个旧组身份退化成空，旧变量沿用与同材质合并全部失效、
备注/按键/停用状态被抹掉。本文件用忠实复刻该语义的假集合把这条线也锁住。
"""
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


PKG = "_custom_material_scan_switches_test_pkg"
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
_fake_bpy.data = types.SimpleNamespace(objects={}, node_groups=[])
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


PROPERTY_DEFAULTS = {
    "object_name": "",
    "switch_variable": "",
    "key": "N",
    "state_count": 2,
    "enabled": True,
    "comment": "",
    "bindings": "",
    "merge_group_id": "",
}


class _FakeSwitchGroup:
    """忠实复刻 Blender 语义：被移出集合后读回属性默认值，且不抛异常。"""

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
        # Blender 的 update 回调：key/comment/enabled 改动会同步同变量的其它组
        if name in ("key", "comment", "enabled") and not module._switch_sync_guard:
            module._sync_switch_variable_fields(self, None)


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


class _FakeMaterial:
    def __init__(self, name):
        self.name = name


class _FakeMaterialSlot:
    def __init__(self, material):
        self.material = material


class _FakeObject:
    def __init__(self, name, material_names):
        self.name = name
        self.type = "MESH"
        self.material_slots = [
            _FakeMaterialSlot(_FakeMaterial(material_name))
            for material_name in material_names
        ]


# ---- 实机（露西·公主假日）扫描结果的最小复刻 -------------------------------
# 脸部：DiffuseMap_颜 / DiffuseMap_颜.001（2 套）；LightMap_身体.001、MaterialMap_身体 只有单张 → 不算切换
FACE_NAME = "LOD0.ba402095-9933-0.000_颜"
FACE = _FakeObject(FACE_NAME, [
    "DiffuseMap_颜",
    "NormalMap_颜",
    "LightMap_身体.001",
    "MaterialMap_身体",
    "DiffuseMap_颜.001",
])
# 身体：DiffuseMap_KK cf_m_body / .001（2 套）—— 与脸部档数相同、贴图不同
BODY_MATERIALS = [
    "DiffuseMap_KK cf_m_body",
    "NormalMap_KK cf_m_body",
    "LightMap_身体",
    "MaterialMap_身体",
    "DiffuseMap_KK cf_m_body.001",
]
BODY = _FakeObject("LOD0.30abbad1-64878-0.身体", BODY_MATERIALS)
BODY_001 = _FakeObject("LOD0.30abbad1-64878-0.身体.001", list(BODY_MATERIALS))
BODY_002 = _FakeObject("LOD0.30abbad1-64878-0.身体.002", list(BODY_MATERIALS))
BODY_PARTS = (BODY, BODY_001, BODY_002)
LIVE_PARTS = (FACE, BODY, BODY_001, BODY_002)

# 其它部件
SKIRT = _FakeObject("Skirt", [
    "DiffuseMap_Skirt_0", "DiffuseMap_Skirt_1", "DiffuseMap_Skirt_2",
    "NormalMap_Skirt_0", "NormalMap_Skirt_1", "NormalMap_Skirt_2",
])
CAPE = _FakeObject("Cape", [
    "DiffuseMap_Cape_0", "DiffuseMap_Cape_1", "DiffuseMap_Cape_2", "DiffuseMap_Cape_3",
])


class _FakeNode:
    bl_idname = "SSMTNode_PostProcess_CustomMaterialAssign"

    def __init__(self, name="MaterialPro"):
        self.name = name
        self.use_global_assign = True
        self.material_switch_var = "$swapkey150"
        self.global_switch_groups = _FakeGroupCollection()
        self.target_items = []
        self.id_data = None


class _FakeNodeList(list):
    """既可按名查找（_find_node）又可迭代（同变量同步回调）。"""

    def __init__(self, nodes):
        super().__init__(nodes)
        self._by_name = {node.name: node for node in nodes}

    def get(self, name):
        return self._by_name.get(name)


class _FakeBlueprintTree:
    def __init__(self, node):
        self.nodes = _FakeNodeList([node])


class _FakeSpaceData:
    def __init__(self, tree):
        self.edit_tree = tree
        self.node_tree = tree


class _FakeContext:
    def __init__(self, tree):
        self.space_data = _FakeSpaceData(tree)


def _run_scan(node, parts):
    module.bpy.data.objects.clear()
    for part in parts:
        module.bpy.data.objects[part.name] = part
    tree = _FakeBlueprintTree(node)
    node.id_data = tree
    module.bpy.data.node_groups = [tree]
    with mock.patch.object(
        module,
        "_connected_blueprint_object_names",
        return_value=[part.name for part in parts],
    ):
        operator = module.SSMT_OT_CustomMaterialScanSwitches()
        operator.reports = []
        operator.node_name = node.name
        result = operator.execute(_FakeContext(tree))
    assert result == {"FINISHED"}, result
    return operator.reports


def _real_node():
    """材质转资源pro 节点实例（借用真实类方法，例如 KeySwap 段写出）。"""
    node = module.SSMTNode_PostProcess_CustomMaterialAssign()
    node.name = "MaterialPro"
    node.use_global_assign = True
    node.material_switch_var = "$swapkey150"
    node.global_switch_groups = _FakeGroupCollection()
    node.target_items = []
    return node


def _section_key(sections, variable):
    lines = sections.get(f"[KeySwap_Diffuse_{variable}]") or []
    key_lines = [line for line in lines if str(line).startswith("key =")]
    return key_lines[0] if key_lines else None


def _variable_map(node):
    mapping = {}
    for group in node.global_switch_groups:
        mapping.setdefault(str(group.switch_variable), []).append(str(group.object_name))
    return {variable: sorted(objects) for variable, objects in mapping.items()}


def _seed_group(node, object_name, variable, material_lists, key="N", comment="", enabled=True):
    """写入一个"上一次扫描留下的"切换组（用于重扫回归）。"""
    group = node.global_switch_groups.add()
    group.object_name = object_name
    group.switch_variable = variable
    group.merge_group_id = variable
    group.state_count = max(len(names) for names in material_lists)
    group.bindings = json.dumps([sorted(names) for names in material_lists])
    group.key = key
    group.comment = comment
    group.enabled = enabled
    return group


def _seed_legacy(node, specs):
    """模拟从 .blend 载入的历史数据：加载不会触发 update 回调。"""
    previous_guard = module._switch_sync_guard
    module._switch_sync_guard = True
    try:
        return [_seed_group(node, **spec) for spec in specs]
    finally:
        module._switch_sync_guard = previous_guard


def _set_legacy(group, **fields):
    """直接改字段且不触发 update 回调（等价于打开 .blend 时的既有数据）。"""
    previous_guard = module._switch_sync_guard
    module._switch_sync_guard = True
    try:
        for name, value in fields.items():
            setattr(group, name, value)
    finally:
        module._switch_sync_guard = previous_guard


class CustomMaterialScanSwitchesTests(unittest.TestCase):
    def test_parts_sharing_the_same_materials_share_one_switch_variable(self):
        """同材质（身体 LOD 三件共用同一套贴图）合并；贴图不同的脸部独立成组。"""
        node = _FakeNode()
        _run_scan(node, LIVE_PARTS)

        mapping = _variable_map(node)
        self.assertEqual(len(mapping), 2, mapping)
        grouped = sorted(sorted(objects) for objects in mapping.values())
        self.assertEqual(grouped, sorted([
            [FACE_NAME],
            sorted(part.name for part in BODY_PARTS),
        ]))

    def test_different_textures_with_same_set_count_are_not_merged(self):
        """回归：套数相同但贴图不同（脸部 2 套 / 身体 2 套）不得并成一组。"""
        node = _FakeNode()
        _run_scan(node, [FACE, BODY])

        mapping = _variable_map(node)
        self.assertEqual(
            mapping,
            {"$swapkey150": [FACE_NAME], "$swapkey151": [BODY.name]},
        )

    def test_rescan_splits_stale_shape_merged_groups(self):
        """回归：修好身份规则后，重扫必须把历史"按档数误合并"的状态拆开。"""
        node = _FakeNode()
        # 复刻旧实现留下的状态：脸部与身体都挂在 $swapkey150
        _seed_legacy(node, [
            dict(object_name=FACE.name, variable="$swapkey150",
                 material_lists=[["DiffuseMap_颜", "DiffuseMap_颜.001"]]),
            *[
                dict(object_name=part.name, variable="$swapkey150",
                     material_lists=[["DiffuseMap_KK cf_m_body", "DiffuseMap_KK cf_m_body.001"]])
                for part in BODY_PARTS
            ],
        ])

        _run_scan(node, LIVE_PARTS)

        mapping = _variable_map(node)
        self.assertEqual(len(mapping), 2, mapping)
        self.assertEqual(
            sorted(len(objects) for objects in mapping.values()),
            [1, 3],
        )

    def test_rescan_with_unchanged_materials_is_idempotent(self):
        """回归：材质没变时，第二次扫描必须与第一次给出完全相同的分组。"""
        node = _FakeNode()
        _run_scan(node, LIVE_PARTS)
        first = _variable_map(node)

        _run_scan(node, LIVE_PARTS)
        second = _variable_map(node)

        self.assertEqual(len(first), 2, first)
        self.assertEqual(second, first)

    def test_rescan_keeps_user_switch_settings(self):
        """回归：重扫后 备注/切换按键/停用状态 必须保留（不得回落默认值）。"""
        node = _FakeNode()
        _run_scan(node, LIVE_PARTS)
        for group in node.global_switch_groups:
            group.key = "M"
            group.comment = "身体"
            group.enabled = False

        _run_scan(node, LIVE_PARTS)

        self.assertEqual(len(node.global_switch_groups), len(LIVE_PARTS))
        for group in node.global_switch_groups:
            self.assertEqual(str(group.key), "M", group.object_name)
            self.assertEqual(str(group.comment), "身体", group.object_name)
            self.assertFalse(bool(group.enabled), group.object_name)

    def test_new_part_does_not_take_a_variable_already_in_use(self):
        """回归：链路里新增的部件必须领新号，不能占用旧组已保留的变量。"""
        node = _FakeNode()
        _run_scan(node, [FACE, BODY])
        first = _variable_map(node)
        self.assertEqual(
            first,
            {"$swapkey150": [FACE_NAME], "$swapkey151": [BODY.name]},
        )

        _run_scan(node, [CAPE, FACE, BODY])
        second = _variable_map(node)

        self.assertEqual(second.get("$swapkey150"), [FACE_NAME])
        self.assertEqual(second.get("$swapkey151"), [BODY.name])
        cape_variables = [
            variable for variable, objects in second.items() if objects == [CAPE.name]
        ]
        self.assertEqual(len(cape_variables), 1)
        self.assertNotIn(cape_variables[0], {"$swapkey150", "$swapkey151"})

    def test_one_variable_never_mixes_two_material_sets(self):
        """不变量：任何切换变量下只能有一种材质集合（否则按键会串联切换）。"""
        node = _FakeNode()
        _run_scan(node, [FACE, BODY, BODY_001, SKIRT, CAPE])

        per_variable = {}
        for group in node.global_switch_groups:
            per_variable.setdefault(str(group.switch_variable), set()).add(
                json.dumps(json.loads(str(group.bindings)), sort_keys=True)
            )
        for variable, material_sets in per_variable.items():
            self.assertEqual(len(material_sets), 1, f"{variable} 混入 {material_sets}")

    def test_editing_the_shared_key_syncs_to_all_parts_of_that_variable(self):
        """回归：共享框里填的按键/备注必须落到该变量的每一个部件组。

        UI 对一个变量只暴露一套控件（``_draw_global_switch_panel`` 只取
        ``entries[0]``），编辑只写进第一件；同伴若停在默认 ``N``，KeySwap 段
        会被同伴的值覆盖 —— 实机表现就是"填了键，其中一个回退成 N"。
        """
        node = _FakeNode()
        _run_scan(node, LIVE_PARTS)
        groups = list(node.global_switch_groups)
        shared = [g for g in groups if str(g.switch_variable) == "$swapkey151"]
        self.assertEqual(len(shared), 3, _variable_map(node))

        target = shared[0]
        target.key = "No_Ctrl Alt Numpad9"
        target.comment = "身体涂鸦"
        module._sync_switch_variable_fields(target, None)

        for group in shared:
            self.assertEqual(str(group.key), "No_Ctrl Alt Numpad9", group.object_name)
            self.assertEqual(str(group.comment), "身体涂鸦", group.object_name)

    def test_keyswap_section_uses_the_filled_key_of_the_variable(self):
        """回归：一个变量只写一段 KeySwap，键取该变量里用户填过的那个。"""
        node = _real_node()
        _seed_legacy(node, [
            dict(object_name=FACE_NAME, variable="$swapkey150",
                 material_lists=[["DiffuseMap_颜", "DiffuseMap_颜.001"]],
                 key="No_Ctrl Alt Numpad1", comment="脸部切换"),
            *[
                dict(object_name=part.name, variable="$swapkey151",
                     material_lists=[["DiffuseMap_KK cf_m_body", "DiffuseMap_KK cf_m_body.001"]],
                     key="No_Ctrl Alt Numpad9" if index == 0 else "N",
                     comment="身体涂鸦")
                for index, part in enumerate(BODY_PARTS)
            ],
        ])

        sections = {}
        node.define_swapkeys_in_sections(sections, {"$swapkey150", "$swapkey151"})

        self.assertEqual(_section_key(sections, "swapkey150"), "key = No_Ctrl Alt Numpad1")
        self.assertEqual(_section_key(sections, "swapkey151"), "key = No_Ctrl Alt Numpad9")
        self.assertEqual(
            len([name for name in sections if name.startswith("[KeySwap_Diffuse_")]),
            2,
        )

    def test_empty_key_does_not_generate_keyswap_section(self):
        node = _real_node()
        _seed_legacy(
            node,
            [
                dict(
                    object_name=FACE_NAME,
                    variable="$swapkey150",
                    material_lists=[["DiffuseMap_颜", "DiffuseMap_颜.001"]],
                    key="",
                    comment="脸部切换",
                )
            ],
        )

        sections = {}
        node.define_swapkeys_in_sections(sections, {"$swapkey150"})

        self.assertNotIn("[KeySwap_Diffuse_swapkey150]", sections)

    def test_rescan_normalizes_shared_variable_metadata(self):
        """回归：重扫后同一变量的 备注/按键 必须归一，不能有一半停在 N。"""
        node = _FakeNode()
        _run_scan(node, LIVE_PARTS)
        shared = [g for g in node.global_switch_groups if str(g.switch_variable) == "$swapkey151"]
        # 历史数据：用户只在共享框（第一件）里填过键，同伴停在默认 N
        _set_legacy(shared[0], key="No_Ctrl Alt Numpad9", comment="身体涂鸦")

        _run_scan(node, LIVE_PARTS)

        for group in node.global_switch_groups:
            if str(group.switch_variable) == "$swapkey151":
                self.assertEqual(str(group.key), "No_Ctrl Alt Numpad9", group.object_name)
                self.assertEqual(str(group.comment), "身体涂鸦", group.object_name)
            else:
                # 归一按变量进行：脸部变量不受身体变量影响
                self.assertEqual(str(group.key), "", group.object_name)
                self.assertEqual(str(group.comment), "", group.object_name)


if __name__ == "__main__":
    unittest.main()
