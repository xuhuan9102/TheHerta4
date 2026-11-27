import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_ntemi_workspace_import_routing_test_pkg"
for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.ntmi_modimp", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeOperatorBase:
    pass


class _FakeCollection:
    def __init__(self, name):
        self.name = name
        self.children = types.SimpleNamespace(link=lambda _child: None)

    def as_pointer(self):
        return id(self)


class _FakeImportedObject(dict):
    def __init__(self, name=""):
        super().__init__()
        self.name = name
        self.data = types.SimpleNamespace(name=name)
        self.type = "MESH"
        self.users_collection = []

    def as_pointer(self):
        return id(self)


class _FakeNode:
    def __init__(self):
        self.inputs = [types.SimpleNamespace(is_linked=False)]
        self.outputs = [object()]
        self.parent = None
        self.location = (0, 0)
        self.label = ""

    def update(self):
        return None


class _FakeNodeGroup:
    def __init__(self, name):
        self.name = name
        self.use_fake_user = False
        self.nodes = types.SimpleNamespace(
            new=lambda _type: _FakeNode(),
            remove=lambda _node: None,
        )
        self.links = types.SimpleNamespace(new=lambda *_args, **_kwargs: None)


_fake_bpy = types.SimpleNamespace(
    data=types.SimpleNamespace(
        node_groups=types.SimpleNamespace(new=lambda name, type: _FakeNodeGroup(name)),
    ),
    context=types.SimpleNamespace(
        scene=types.SimpleNamespace(
            collection=types.SimpleNamespace(children=types.SimpleNamespace(link=lambda _coll: None)),
        )
    ),
    types=types.SimpleNamespace(Operator=_FakeOperatorBase, Collection=object, OperatorFileListElement=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy_extras")


class _FakeImportHelper:
    pass


_install_module("bpy_extras.io_utils", ImportHelper=_FakeImportHelper)

_install_module(
    f"{PKG}.utils.collection_utils",
    CollectionColor=types.SimpleNamespace(Blue="Blue", Orange="Orange", Red="Red"),
    CollectionUtils=types.SimpleNamespace(
        create_new_collection=lambda collection_name, color_tag=None: _FakeCollection(collection_name),
        select_collection_objects=lambda _collection: None,
    ),
)
_install_module(
    f"{PKG}.utils.json_utils",
    JsonUtils=types.SimpleNamespace(SaveToFile=lambda **_kwargs: None),
)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(Start=lambda *_args: None, End=lambda *_args: None),
)
_install_module(
    f"{PKG}.utils.translate_utils",
    TR=types.SimpleNamespace(translate=lambda text: text),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(
        path_workspace_folder=lambda: "X:/Workspace/NTEMI/Test",
        get_workspace_name=lambda: "测试工作空间",
        logic_name="NTEMI",
    ),
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(enable_non_mirror_workflow=lambda: False),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(NTEMI="NTEMI"),
)
_install_module(
    f"{PKG}.common.non_mirror_workflow",
    NonMirrorWorkflowHelper=types.SimpleNamespace(process_imported_objects=lambda _objects: None),
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        extract_prefix_info=lambda _name: None,
        replace_prefix=lambda display_name, workspace_unique_str, *_args: workspace_unique_str if not display_name else workspace_unique_str,
    ),
)
_install_module(
    f"{PKG}.common.ssmt_import_helper",
    SSMTImportHelper=types.SimpleNamespace(create_mesh_from_json=lambda **_kwargs: None),
)
_install_module(
    f"{PKG}.common.workspace_helper",
    WorkSpaceHelper=types.SimpleNamespace(
        create_and_get_workspace_collection=lambda: _FakeCollection("Workspace"),
        get_drawib_aliasname_dict=lambda: {},
        get_drawib_tabname_dict=lambda: {},
    ),
)

_created_mesh_calls = []
_bone_merge_calls = []


def _fake_create_mesh_with_modimp_props(**kwargs):
    _created_mesh_calls.append(kwargs)
    obj = _FakeImportedObject(kwargs.get("workspace_unique_str", ""))
    obj["modimp_workspace_unique_str"] = kwargs.get("workspace_unique_str", "")
    obj["modimp_region_first_index"] = kwargs["draw_call_meta"].first_index
    obj["modimp_region_index_count"] = kwargs["draw_call_meta"].index_count
    return obj


def _fake_bone_merge_postprocess(**kwargs):
    _bone_merge_calls.append(kwargs)


_draw_calls = [
    types.SimpleNamespace(
        lod_name="LOD0",
        submesh_folder_name="0bebac08-1002-0",
        folder_path="X:/Workspace/NTEMI/Test/LOD0/0bebac08-1002-0/TYPE_GPU",
        draw_ib="0bebac08",
        first_index=0,
        index_count=1002,
        display_name="0bebac08-1002-0",
        alias_name="",
        component="1002",
    ),
    types.SimpleNamespace(
        lod_name="LOD1",
        submesh_folder_name="a351bef7-4500-0",
        folder_path="X:/Workspace/NTEMI/Test/LOD1/a351bef7-4500-0/TYPE_GPU",
        draw_ib="a351bef7",
        first_index=0,
        index_count=4500,
        display_name="a351bef7-4500-0",
        alias_name="",
        component="4500",
    ),
]

_install_module(
    f"{PKG}.ui.ntmi_modimp.ntemi_importer",
    NTEMIImportHelper=types.SimpleNamespace(create_mesh_with_modimp_props=_fake_create_mesh_with_modimp_props),
    NtemiDrawCallMeta=object,
    _discover_draw_calls=lambda _workspace_root, _drawib_aliasname_dict=None: list(_draw_calls),
    _load_frame_analysis_dir_map=lambda _workspace_root: {
        "0bebac08": "K:/FrameAnalysis/LOD0",
        "a351bef7": "K:/FrameAnalysis/LOD1",
    },
    _resolve_deduped_texture_dir=lambda _workspace_root, lod_name="LOD0": f"X:/Workspace/NTEMI/Test/{lod_name}/DedupedTextures",
    _resolve_frame_analysis_dir=lambda _workspace_root: "K:/FrameAnalysis/Fallback",
    _load_component_name_map=lambda lod_dir: {"lod_dir": lod_dir},
    _perform_bone_merge_postprocess=_fake_bone_merge_postprocess,
    NTEMI_PROFILE_ID="yihuan",
)
_install_module(
    f"{PKG}.ui.ui_prefix_quick_ops",
    PrefixQuickOpsHelper=types.SimpleNamespace(merge_prefixes_from_objects=lambda _context, _objects: None),
)


module_path = Path(__file__).resolve().parents[1] / "ui" / "ui_func_import_ssmt.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.ui.ui_func_import_ssmt", module_path)
ui_func_import_ssmt = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ui_func_import_ssmt
spec.loader.exec_module(ui_func_import_ssmt)


class APMIWorkspaceImportGameTypeTests(unittest.TestCase):
    def _ordered_folders(self, logic_name):
        with tempfile.TemporaryDirectory() as temp_dir:
            component_folder = Path(temp_dir) / "cd7b0837-25761-0"
            no_blend = component_folder / "TYPE_GPU_P12_N12_TA16_C4_T4_T1-8_"
            with_blend = component_folder / (
                "TYPE_GPU_P12_N12_TA16_C4_T4_T1-8_BW16_BI16_"
            )
            no_blend.mkdir(parents=True)
            with_blend.mkdir()
            json_name = "cd7b0837-25761-0.json"
            (no_blend / json_name).write_text("{}", encoding="utf-8")
            (with_blend / json_name).write_text("{}", encoding="utf-8")

            def load_category_map(json_path):
                if "BW16_BI16" in json_path:
                    return {"CategoryDrawCategoryMap": {"Blend": "Blend"}}
                return {"CategoryDrawCategoryMap": {"Position": "Position"}}

            with (
                mock.patch.object(
                    ui_func_import_ssmt.GlobalConfig,
                    "logic_name",
                    logic_name,
                ),
                mock.patch.object(
                    ui_func_import_ssmt.WorkSpaceHelper,
                    "get_ordered_gpu_cpu_import_folderpath_list",
                    return_value=[str(no_blend), str(with_blend)],
                    create=True,
                ),
                mock.patch.object(
                    ui_func_import_ssmt.JsonUtils,
                    "LoadFromFile",
                    side_effect=load_category_map,
                    create=True,
                ),
            ):
                return ui_func_import_ssmt._ordered_workspace_import_folderpath_list(
                    str(component_folder),
                    "cd7b0837-25761-0",
                )

    def test_apmi_prefers_game_type_with_blend(self):
        folders = self._ordered_folders("APMI")
        self.assertIn("BW16_BI16", folders[0])

    def test_non_apmi_keeps_existing_folder_order(self):
        folders = self._ordered_folders("ZZMI")
        self.assertNotIn("BW16_BI16", folders[0])


class _FakeCollectionRegistry(dict):
    pass


class _FakeCollectionChildren:
    def __init__(self):
        self.linked = []

    def link(self, collection):
        self.linked.append(collection)


class _FakeCollectionParent:
    def __init__(self):
        self.children = _FakeCollectionChildren()

    def as_pointer(self):
        return id(self)


class _FakeOperator:
    def __init__(self):
        self.reports = []

    def report(self, level, message):
        self.reports.append((set(level), message))


class ZZMISkeletonGroupCollectionTests(unittest.TestCase):
    def test_existing_group_name_creates_dot_001_and_reuses_it_within_batch(self):
        registry = _FakeCollectionRegistry({
            "SkeletonGroup_3": _FakeCollection("SkeletonGroup_3"),
        })
        parent = _FakeCollectionParent()
        batch_cache = {}

        def create_collection(collection_name, color_tag=None):
            collection = _FakeCollection(collection_name)
            collection.color_tag = color_tag
            registry[collection_name] = collection
            return collection

        with (
            mock.patch.object(ui_func_import_ssmt.bpy.data, "collections", registry, create=True),
            mock.patch.object(
                ui_func_import_ssmt.CollectionUtils,
                "create_new_collection",
                side_effect=create_collection,
            ) as create_mock,
        ):
            first = ui_func_import_ssmt._zzmi_get_or_create_skeleton_group_collection(
                parent, 3, batch_cache
            )
            second = ui_func_import_ssmt._zzmi_get_or_create_skeleton_group_collection(
                parent, 3, batch_cache
            )

        self.assertEqual(first.name, "SkeletonGroup_3.001")
        self.assertIs(second, first)
        self.assertEqual(parent.children.linked, [first])
        self.assertEqual(create_mock.call_count, 1)

    def test_next_import_batch_creates_next_suffix(self):
        registry = _FakeCollectionRegistry({
            "SkeletonGroup_3": _FakeCollection("SkeletonGroup_3"),
            "SkeletonGroup_3.001": _FakeCollection("SkeletonGroup_3.001"),
        })
        parent = _FakeCollectionParent()

        def create_collection(collection_name, color_tag=None):
            collection = _FakeCollection(collection_name)
            registry[collection_name] = collection
            return collection

        with (
            mock.patch.object(ui_func_import_ssmt.bpy.data, "collections", registry, create=True),
            mock.patch.object(
                ui_func_import_ssmt.CollectionUtils,
                "create_new_collection",
                side_effect=create_collection,
            ),
        ):
            collection = ui_func_import_ssmt._zzmi_get_or_create_skeleton_group_collection(
                parent, 3, {}
            )

        self.assertEqual(collection.name, "SkeletonGroup_3.002")


class EFMIAutoLODMatchNodeTests(unittest.TestCase):
    def test_generated_node_recomputes_mapping_from_actual_blender_objects(self):
        calls = []

        class FakeMatchNode:
            def execute_match(self, context):
                calls.append(context)
                return {"0": "371"}, "匹配完成"

        node = FakeMatchNode()
        context = object()
        source = types.SimpleNamespace(name="LOD0.source")
        target = types.SimpleNamespace(name="LOD1.target")

        mapping, message = ui_func_import_ssmt._configure_and_execute_efmi_lod_match(
            node, source, target, context
        )

        self.assertEqual(mapping, {"0": "371"})
        self.assertEqual(message, "匹配完成")
        self.assertEqual(calls, [context])
        self.assertEqual(node.source_object, source.name)
        self.assertEqual(node.target_object, target.name)
        # t14 回退：t12 曾把 target_hash 改为专属目标前缀；用户指令确认非本案
        # 病根（t11 §6.6 键不相交 + t13-D 用户构建不含 t12），恢复为 ""（与
        # t12 前一致）。
        self.assertEqual(node.target_hash, "")
        self.assertFalse(node.exact_hash_match)

    def test_efmi_auto_output_defaults_to_fx_style(self):
        output_node = types.SimpleNamespace()
        ui_func_import_ssmt._configure_efmi_auto_output_node(output_node, "EFMI")
        self.assertTrue(output_node.use_rabbitfx_slot)

    def test_generated_match_defaults_are_debug_friendly(self):
        calls = []

        class FakeMatchNode:
            def execute_match(self, context):
                calls.append(context)
                return {"0": "371"}, "匹配完成"

        node = FakeMatchNode()
        mapping, message = ui_func_import_ssmt._configure_and_execute_efmi_lod_match(
            node,
            types.SimpleNamespace(name="LOD0.source"),
            types.SimpleNamespace(name="LOD1.target"),
            object(),
        )
        self.assertEqual(mapping, {"0": "371"})
        self.assertEqual(message, "匹配完成")
        self.assertTrue(node.create_debug_objects)
        self.assertFalse(node.use_chamfer_matching)


    def test_generated_main_chain_uses_node_dimensions_without_overlap(self):
        group = types.SimpleNamespace(
            bl_idname="SSMTNode_Object_Group",
            location=types.SimpleNamespace(x=100.0, y=500.0),
            width=220.0,
            height=420.0,
        )
        rename = types.SimpleNamespace(bl_idname="SSMTNode_Object_Rename", width=380.0, height=300.0)
        process = types.SimpleNamespace(bl_idname="SSMTNode_VertexGroupProcess", width=300.0, height=520.0)
        output = types.SimpleNamespace(bl_idname="SSMTNode_Result_Output", width=240.0, height=260.0)

        positions = ui_func_import_ssmt._layout_efmi_auto_main_chain(
            group, rename, process, output
        )

        self.assertEqual(positions["group"], (100.0, 500.0))
        self.assertEqual(rename.width, 380.0)
        self.assertEqual(process.width, 700.0)
        self.assertEqual(output.width, 400.0)
        self.assertGreaterEqual(
            positions["rename"][0], positions["group"][0] + group.width + 60.0
        )
        self.assertGreaterEqual(
            positions["process"][0], positions["rename"][0] + rename.width + 60.0
        )
        self.assertGreaterEqual(
            positions["output"][0], positions["process"][0] + process.width + 60.0
        )

    def test_match_nodes_wrap_after_six_using_actual_dimensions(self):
        group = types.SimpleNamespace(
            location=types.SimpleNamespace(x=100.0, y=500.0),
            width=220.0,
            height=420.0,
        )
        match_nodes = [
            types.SimpleNamespace(width=300.0 + index * 10.0, height=360.0 + index * 5.0)
            for index in range(8)
        ]

        positions = ui_func_import_ssmt._layout_efmi_match_nodes(
            group, match_nodes, max_per_row=6
        )

        self.assertEqual(len(positions), 8)
        self.assertEqual(positions[0][0], 600.0)
        self.assertEqual(positions[0][1], -1120.0)
        self.assertEqual(positions[0][1], positions[5][1])
        self.assertLess(positions[6][1], positions[0][1] - match_nodes[0].height)
        self.assertEqual(positions[6][1], positions[7][1])
        for left_index in range(5):
            self.assertGreaterEqual(
                positions[left_index + 1][0],
                positions[left_index][0] + match_nodes[left_index].width + 80.0,
            )
        self.assertGreaterEqual(
            positions[7][0], positions[6][0] + match_nodes[6].width + 80.0
        )

    def test_generated_rename_node_does_not_defer_until_after_vg_processing(self):
        node = types.SimpleNamespace(defer_until_after_vertex_group_process=True)
        ui_func_import_ssmt._configure_efmi_auto_rename_node(node)
        self.assertFalse(node.defer_until_after_vertex_group_process)


class NTEMIWorkspaceImportRoutingTests(unittest.TestCase):
    def setUp(self):
        _created_mesh_calls.clear()
        _bone_merge_calls.clear()

    def test_import_routes_each_draw_ib_to_its_tab_frame_analysis(self):
        operator = _FakeOperator()
        workspace_collection = _FakeCollection("Workspace")
        context = types.SimpleNamespace(scene=types.SimpleNamespace(global_properties=types.SimpleNamespace(selected_blueprint_name="")))

        result = ui_func_import_ssmt._import_workspace_full_ntemi(operator, context, workspace_collection)

        self.assertTrue(result)
        self.assertEqual(
            [call["frame_analysis_dir"] for call in _created_mesh_calls],
            ["K:/FrameAnalysis/LOD0", "K:/FrameAnalysis/LOD1"],
        )
        self.assertEqual(
            [(call["draw_ib"], call["frame_analysis_dir"]) for call in _bone_merge_calls],
            [("0bebac08", "K:/FrameAnalysis/LOD0"), ("a351bef7", "K:/FrameAnalysis/LOD1")],
        )
        self.assertEqual(
            [call["workspace_unique_str"] for call in _created_mesh_calls],
            ["LOD0.0bebac08-1002-0", "LOD1.a351bef7-4500-0"],
        )


class MergedSkeletonFallbackRoutingTests(unittest.TestCase):
    def _run_efmi_import(self, ensure_ok):
        target = {
            "import_key": "LOD0.aaaabbbb-100-0",
            "submesh_folder_path": "X:/Workspace/EFMI/LOD0/aaaabbbb-100-0",
            "submesh_folder_name": "aaaabbbb-100-0",
            "display_name": "LOD0.aaaabbbb-100-0",
            "import_collection": _FakeCollection("LOD0"),
        }
        create_calls = []
        option_updates = []
        skeleton_module_name = f"{PKG}.common.efmi_skeleton"
        previous_skeleton_module = sys.modules.get(skeleton_module_name)
        _install_module(
            skeleton_module_name,
            EFMISkeletonMergeHelper=types.SimpleNamespace(
                ensure_skeleton_data=lambda **_kwargs: (
                    ensure_ok,
                    "完整" if ensure_ok else "一个目标未生成",
                )
            ),
        )

        global_properties = sys.modules[
            f"{PKG}.common.global_properties"
        ].GlobalProterties
        logic_names = sys.modules[f"{PKG}.common.logic_name"].LogicName
        workspace_helper = sys.modules[
            f"{PKG}.common.workspace_helper"
        ].WorkSpaceHelper
        import_helper = sys.modules[
            f"{PKG}.common.ssmt_import_helper"
        ].SSMTImportHelper

        try:
            with (
                mock.patch.object(ui_func_import_ssmt, "_detect_ntemi_workspace", return_value=False),
                mock.patch.object(
                    ui_func_import_ssmt,
                    "_build_workspace_import_targets",
                    return_value=iter([target]),
                ),
                mock.patch.object(
                    ui_func_import_ssmt.GlobalConfig, "logic_name", "EFMI"
                ),
                mock.patch.object(logic_names, "EFMI", "EFMI", create=True),
                mock.patch.object(logic_names, "ZZMI", "ZZMI", create=True),
                mock.patch.object(
                    global_properties,
                    "import_merged_vgmap",
                    side_effect=lambda: True,
                    create=True,
                ),
                mock.patch.object(
                    global_properties,
                    "efmi_lod_group_projection",
                    side_effect=lambda: True,
                    create=True,
                ),
                mock.patch.object(
                    global_properties,
                    "efmi_lod_group_dedup",
                    side_effect=lambda: True,
                    create=True,
                ),
                mock.patch.object(
                    global_properties,
                    "set_import_merged_vgmap",
                    side_effect=lambda value: option_updates.append(bool(value)),
                    create=True,
                ),
                mock.patch.object(
                    workspace_helper,
                    "get_ordered_gpu_cpu_import_folderpath_list",
                    side_effect=lambda _path: ["X:/Workspace/EFMI/TYPE_GPU_TEST"],
                    create=True,
                ),
                mock.patch.object(
                    import_helper,
                    "create_mesh_from_json",
                    side_effect=lambda **kwargs: create_calls.append(kwargs),
                    create=True,
                ),
            ):
                operator = _FakeOperator()
                context = types.SimpleNamespace(scene=types.SimpleNamespace())
                result = ui_func_import_ssmt.ImprotFromWorkSpaceFull(operator, context)
        finally:
            if previous_skeleton_module is None:
                sys.modules.pop(skeleton_module_name, None)
            else:
                sys.modules[skeleton_module_name] = previous_skeleton_module
        return result, create_calls, option_updates

    def test_failed_pre_generation_forces_whole_batch_ordinary_and_disables_option(self):
        result, create_calls, option_updates = self._run_efmi_import(False)
        self.assertFalse(result)  # fake importer returns None; routing assertions remain valid
        self.assertEqual(len(create_calls), 1)
        self.assertIs(create_calls[0]["use_merged_vgmap"], False)
        self.assertEqual(option_updates, [False])

    def test_successful_pre_generation_keeps_merged_batch(self):
        result, create_calls, option_updates = self._run_efmi_import(True)
        self.assertFalse(result)
        self.assertEqual(len(create_calls), 1)
        self.assertIs(create_calls[0]["use_merged_vgmap"], True)
        self.assertEqual(option_updates, [])


if __name__ == "__main__":
    unittest.main()
