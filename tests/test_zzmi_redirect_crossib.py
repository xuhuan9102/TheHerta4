"""ZZMI 合并网格自动重定向 × Cross-IB 节点交互回归单测（fake 环境）。

覆盖 t3 假设 H8 (P1 wrong output + loud warning):
- _build_merged_mesh_redirect_plan（zzmi.py:1140）在组件或其 target 配置了跨 IB
  （_drawib_is_cross_ib，zzmi.py:1126-1138）时，把该 draw_ib 记入 unredirected，
  reason="cross-ib"（zzmi.py:1211-1216），并 continue —— **不会崩**，但该组件的
  合并网格不会挪到组内最后一个 deform draw（_redirect_carrier_map 不含它）。
- 后果：该组件（引用其它部件骨骼、deform pass 又早）在早 pass 读不到晚 pass 部件
  的当帧 palette -> 读陈旧骨骼 -> 合并几何错位/缺失。_warn_merged_mesh_timing
  （zzmi.py:1000-1035）大声报警并提供手动修复（非静默）。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

from tests import _real_modules


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_zzmi_redirect_crossib_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


for package_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

# 真实 common 子模块按 fake 包前缀注册（空 __path__ 假包解析不了相对导入）
_real_modules.register_real_common_modules(f"{PKG}.common")


class _FakeIniSection:
    def __init__(self, _section_type):
        self.SectionLineList = []

    def append(self, line):
        self.SectionLineList.append(line)

    def new_line(self):
        self.SectionLineList.append("")


class _FakeIniBuilder:
    def __init__(self):
        self.sections = []

    def append_section(self, section):
        self.sections.append(section)


class _FakeIniSectionType:
    TextureOverrideIB = "TextureOverrideIB"
    ResourceBuffer = "ResourceBuffer"
    MergedSkeleton = "MergedSkeleton"


class _FakeExportUnity:
    def __init__(self, blueprint_model):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []


_fake_global_properties = types.SimpleNamespace(
    import_merged_vgmap=lambda: True,
    forbid_auto_texture_ini=lambda: False,
)
_fake_global_config = types.SimpleNamespace(
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: "",
    get_workspace_name=lambda: "",
    path_workspace_folder=lambda: "",
)


class _FakeVertexGroup:
    def __init__(self, name):
        self.name = name
        self.add_calls = []

    def add(self, indices, weight, mode):
        self.add_calls.append((list(indices), weight, mode))


class _FakeVertexGroups(list):
    def new(self, name):
        vg = _FakeVertexGroup(name)
        self.append(vg)
        return vg


class _FakeMesh:
    def __init__(self, name):
        self.name = name
        self.vertices = []


class _FakeObject:
    def __init__(self, name, object_data=None):
        self.name = name
        self.data = object_data
        self.vertex_groups = _FakeVertexGroups()
        self.props = {}

    def __setitem__(self, key, value):
        self.props[key] = value

    def get(self, key, default=None):
        return self.props.get(key, default)


class _FakeObjectRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name, object_data=None):
        obj = _FakeObject(name, object_data)
        self._items[name] = obj
        return obj

    def get(self, name):
        return self._items.get(name)

    def remove(self, obj, do_unlink=False):
        self._items.pop(obj.name, None)

    def __iter__(self):
        return iter(list(self._items.values()))


class _FakeMeshRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name):
        mesh = _FakeMesh(name)
        self._items[name] = mesh
        return mesh

    def remove(self, mesh):
        self._items.pop(mesh.name, None)


_fake_bpy_data = types.SimpleNamespace(
    objects=_FakeObjectRegistry(), meshes=_FakeMeshRegistry()
)
_install_module(
    "bpy",
    data=_fake_bpy_data,
    context=types.SimpleNamespace(
        collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _o: None)),
        scene=types.SimpleNamespace(collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _o: None))),
    ),
)


def _load_real_module(qualname, relpath):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_load_real_module(f"{PKG}.common.m_key", "common/m_key.py")
_load_real_module(f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py")
_load_real_module(f"{PKG}.common.draw_call_model", "common/draw_call_model.py")

_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=_fake_global_config,
)
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=_fake_global_properties,
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install_module(
    f"{PKG}.common.m_ini_helper",
    M_IniHelper=types.SimpleNamespace(
        get_drawindexed_str_list=lambda *a, **k: [],
        is_slot_binding_mark_type=lambda mark_type: False,
    ),
)
_install_module(
    f"{PKG}.common.m_ini_helper_gui",
    M_IniHelperGUI=types.SimpleNamespace(),
)
_install_module(
    f"{PKG}.common.m_ini_builder",
    M_IniBuilder=_FakeIniBuilder,
    M_IniSection=_FakeIniSection,
    M_SectionType=_FakeIniSectionType,
)
_install_module(f"{PKG}.ui.universal.unity", ExportUnity=_FakeExportUnity)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None
    ),
)

_module_path = REPO_ROOT / "ui" / "universal" / "zzmi.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.ui.universal.zzmi", _module_path)
_zzmi_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _zzmi_module
_spec.loader.exec_module(_zzmi_module)

DrawCallModel = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel


class _FakeGameType:
    OrderedCategoryNameList = ["Position", "Texcoord", "Blend"]
    GPU_PreSkinning = True
    CategoryStrideDict = {"Position": 40, "Texcoord": 20, "Blend": 32}
    CategoryExtractSlotDict = {"Position": "vb0", "Texcoord": "vb1", "Blend": "vb2"}


class _FakeSubmesh:
    def __init__(self, unique_str, vg_offset=0, vg_count=0, skeleton_group=0, vg_map=None,
                 deform_draw=0, match_first_index=0, exported_vertex_count=0):
        self.unique_str = unique_str
        self.match_first_index = match_first_index
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.skeleton_group = skeleton_group
        self.vg_map = vg_map if vg_map is not None else {
            local: vg_offset + local for local in range(vg_count)
        }
        self.deform_draw_index = deform_draw
        self.index_vertex_id_dict = (
            list(range(exported_vertex_count)) if exported_vertex_count else None
        )
        self.category_buffer_dict = {}
        self.drawcall_model_list = []


class _FakeDrawIBModel:
    def __init__(self, draw_ib, submesh_model_list):
        self.draw_ib = draw_ib
        self.draw_ib_alias = draw_ib
        self.draw_number = 4643
        self.d3d11GameType = _FakeGameType()
        self.category_hash_dict = {}
        self.submesh_model_list = submesh_model_list
        self.category_buffer_dict = {}
        self.submesh_ib_dict = {s.unique_str: b"\x00\x00\x00\x00" for s in submesh_model_list}
        self.match_first_index_partname_dict = {}

    def get_submesh_texture_markup_info_list(self, submesh_model):
        return []


def _register_obj(name, bone_ids, stub=False):
    """fake 对象：顶点 i 权重挂顶点组 i，组名 = bone_ids[i]（全局骨骼 id）。"""
    mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
    mesh.vertices = [
        types.SimpleNamespace(groups=[types.SimpleNamespace(group=i, weight=1.0)])
        for i in range(len(bone_ids))
    ]
    obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
    obj.vertex_groups = _FakeVertexGroups()
    for bone_id in bone_ids:
        obj.vertex_groups.append(_FakeVertexGroup(str(bone_id)))
    if stub:
        obj["ZZMI_STUB"] = 1
    return obj


def _attach_drawcall(submesh):
    dc = DrawCallModel(obj_name=submesh.unique_str)
    submesh.drawcall_model_list = [dc]
    return submesh


def _make_exporter(drawib_models, components, cross_ib_info_dict=None):
    blueprint_model = types.SimpleNamespace(
        cross_ib_info_dict=cross_ib_info_dict or {},
        cross_ib_method_dict={},
        cross_ib_mapping_method={},
        has_cross_ib=bool(cross_ib_info_dict),
        cross_ib_object_names=set(),
        keyname_mkey_dict={},
        ordered_draw_obj_data_model_list=[],
    )
    exporter = _zzmi_module.ExportZZMI(blueprint_model)
    exporter.drawib_model_list = drawib_models
    exporter.merged_skeleton_components = components
    exporter.merged_skeleton_component_id_dict = {
        c["draw_ib"]: i for i, c in enumerate(components)
    }
    return exporter


class ZZMICrossIbRedirectTests(unittest.TestCase):
    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _group3_components(self):
        """组 3：a23aa8a3(最后 deform draw=20) target；b20f90ea(deform=2) carrier。"""
        return [
            {
                "draw_ib": "b20f90ea", "unique_str": "LOD0.b20f90ea-19182-0",
                "vg_offset": 184, "vg_count": 45, "skeleton_group": 3,
                "vg_map": {i: 184 + i for i in range(45)}, "deform_draw": 2,
            },
            {
                "draw_ib": "a23aa8a3", "unique_str": "LOD0.a23aa8a3-42759-0",
                "vg_offset": 79, "vg_count": 105, "skeleton_group": 3,
                "vg_map": {i: 79 + i for i in range(105)}, "deform_draw": 20,
            },
        ]

    def _components_exporter(self, cross_ib_info_dict=None):
        """b20f90ea 顶点引用 79（a23aa8a3 的槽位）-> absorbed 非空 -> carrier 候选。"""
        # b20f90ea：自身 vg_map 槽 184..228；顶点还引用 79（组内其它部件槽位）。
        _register_obj("LOD0.b20f90ea-19182-0", [79, 184, 185, 186])
        sub_b = _attach_drawcall(
            _FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 45, 3, deform_draw=2,
                         match_first_index=0, exported_vertex_count=1000)
        )
        # a23aa8a3 是最后 deform draw（target），需有导出顶点数与 first_index。
        sub_a = _attach_drawcall(
            _FakeSubmesh("LOD0.a23aa8a3-42759-0", 79, 105, 3, deform_draw=20,
                         match_first_index=0, exported_vertex_count=500)
        )
        models = [
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
        ]
        return _make_exporter(models, self._group3_components(), cross_ib_info_dict)

    def test_cross_ib_carrier_is_unredirected_not_in_carrier_map(self):
        """H8: 配置了跨 IB 的 carrier 进 unredirected(reason="cross-ib")，且不在 carrier_map。"""
        exporter = self._components_exporter(cross_ib_info_dict={"b20f90ea_0": ["x"]})
        carrier_map, target_map, unredirected = exporter._build_merged_mesh_redirect_plan()
        self.assertNotIn("b20f90ea", carrier_map)
        self.assertIn("b20f90ea", unredirected)
        self.assertEqual(unredirected["b20f90ea"]["reason"], "cross-ib")
        self.assertEqual(unredirected["b20f90ea"]["target"], "LOD0.a23aa8a3-42759-0")

    def test_no_cross_ib_keeps_visible_parts_on_native_sos(self):
        """无跨 IB 且 target 有导出几何时，直连重放保留各自 SO，不报跨 IB 错误。"""
        exporter = self._components_exporter(cross_ib_info_dict={})
        carrier_map, target_map, unredirected = exporter._build_merged_mesh_redirect_plan()
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertNotIn("b20f90ea", unredirected)

    def test_warn_merged_mesh_timing_reports_cross_ib(self):
        """H8: _warn_merged_mesh_timing 对 cross-ib 的 unredirected 大声报警（含手动修复）。"""
        exporter = self._components_exporter(cross_ib_info_dict={"b20f90ea_0": ["x"]})
        _carrier_map, _target_map, unredirected = exporter._build_merged_mesh_redirect_plan()
        with __import__("contextlib").redirect_stdout(
            __import__("io").StringIO()
        ) as buf:
            exporter._warn_merged_mesh_timing(unredirected)
        out = buf.getvalue()
        self.assertIn("合并网格时序无法自动修复", out)
        self.assertIn("跨 IB 重定向", out)          # 说明该部件配置了跨 IB
        self.assertIn("暂不与自动重定向兼容", out)
        self.assertIn("手动修复", out)


if __name__ == "__main__":
    unittest.main()
