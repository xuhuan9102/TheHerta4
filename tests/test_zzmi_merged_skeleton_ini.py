"""ExportZZMI 合并骨架 INI 生成单测（fake 环境，不依赖 bpy/游戏）。"""

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

from tests import _real_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_merged_skeleton_ini_test_pkg"


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
    def __init__(self, section_type):
        self.SectionType = section_type
        self.SectionName = ""
        self.SectionLineList = []

    def append(self, line):
        self.SectionLineList.append(line)

    def extend(self, lines):
        self.SectionLineList.extend(lines)

    def new_line(self):
        self.SectionLineList.append("")


class _FakeIniBuilder:
    def __init__(self):
        self.sections = []

    def append_section(self, section):
        self.sections.append(section)


class _FakeIniSectionType:
    Constants = "Constants"
    Present = "Present"
    TextureOverrideIB = "TextureOverrideIB"
    TextureOverrideVB = "TextureOverrideVB"
    TextureOverrideVertexLimitRaise = "TextureOverrideVertexLimitRaise"
    ResourceBuffer = "ResourceBuffer"
    MergedSkeleton = "MergedSkeleton"


def _all_builder_lines(builder):
    lines = []
    for section in builder.sections:
        if section.SectionName:
            lines.append(f"[{section.SectionName}]")
        lines.extend(section.SectionLineList)
    return lines


class _FakeExportUnity:
    def __init__(self, blueprint_model):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []

    def add_unity_vs_resource_vb_sections(self, ini_builder, drawib_model):
        pass

    def add_unity_vs_texture_override_vlr_section(
        self, ini_builder, drawib_model, include_uav_byte_stride=True
    ):
        pass


_fake_global_properties = types.SimpleNamespace(
    import_merged_vgmap=lambda: True,
    forbid_auto_texture_ini=lambda: False,
    zzz_use_slot_fix=lambda: False,
)
# vg_map 导出写文件：path_generate_mod_folder 必须指向临时目录，防止测试残留
# 污染仓库根（2026-08-23 曾把 Meshes/zz_vgmap_*.buf 写到仓库根）
_FAKE_MOD_FOLDER = tempfile.mkdtemp(prefix="zzmi_mod_folder_")
_fake_global_config = types.SimpleNamespace(
    path_generatemod_buffer_folder=lambda: "",
    path_generate_mod_folder=lambda: _FAKE_MOD_FOLDER,
    get_workspace_name=lambda: "",
    path_workspace_folder=lambda: "",
)


class _FakeMesh:
    def __init__(self, name):
        self.name = name
        self.users = 0
        self.from_pydata_calls = []
        self.vertices = []

    def from_pydata(self, verts, edges, faces):
        self.from_pydata_calls.append((verts, edges, faces))

    def update(self):
        pass


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


_fake_bpy_data = types.SimpleNamespace(objects=_FakeObjectRegistry(), meshes=_FakeMeshRegistry())
_install_module(
    "bpy",
    data=_fake_bpy_data,
    context=types.SimpleNamespace(
        collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None)),
        scene=types.SimpleNamespace(collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))),
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
# B1：契约判定模块（zzmi.py 的 `_enforce_merged_skeleton_contract` 在调用时按
# 相对导入取它）。这里登记**真实**实现，让「接线 + 各档行为」测试覆盖真实语义；
# 它自身无任何依赖，按 fake 包前缀登记即可。
_zzmi_contract_module = _load_real_module(
    f"{PKG}.common.zzmi_merged_contract", "common/zzmi_merged_contract.py"
)

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
        get_drawindexed_str_list=lambda drawcall_list, obj_name_draw_offset_dict=None, base_vertex=0: [
            line
            for dc in drawcall_list
            for line in (
                f"; [mesh:{dc.obj_name}] [vertex_count:{dc.vertex_count}]",
                f"drawindexed = {dc.index_count},{dc.index_offset},{base_vertex}",
            )
        ],
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
    TimerUtils=types.SimpleNamespace(start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None),
)

# 骨骼池方案模块（`...common.zzmi_cross_group_palette`）由 zzmi.py 在函数内按需
# 相对导入，**不在本文件全局登记**：本文件其余 170 多条契约是在「池路线失活」的
# 旧口径下逐字比对产物的，登记它会让那批产物整体变形（已实测：3 条契约变红）。
# 只有真要验池路线的用例才调 `_enable_pool_route()`。

_module_path = REPO_ROOT / "ui" / "universal" / "zzmi.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.ui.universal.zzmi", _module_path)
_zzmi_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _zzmi_module
_spec.loader.exec_module(_zzmi_module)

# t149：`; ZZMI-MERGE-DIAG …` / `; channel_slot=…` / `; --- 实例对齐 … ---` 这些
# **开发者诊断注释**已改为默认不写进配置表（`ZZMI_MERGE_DIAG_EMIT`）。本文件的
# 契约正是"产物里必须留有这些可数证据"，所以在**本模块自己的 zzmi 副本**上打开
# 开关（不动环境变量，避免影响其它测试文件）。开关关闭时旧输出可逐字节复现。
_zzmi_module.ZZMI_MERGE_DIAG_EMIT = True


class _FakeGameType:
    OrderedCategoryNameList = ["Position", "Texcoord", "Blend"]
    GPU_PreSkinning = True
    CategoryDrawCategoryDict = {
        "Position": "Position",
        "Texcoord": "Texcoord",
        "Blend": "Position",  # ZZZ: Blend 画在 Position 类别（deform pass 同一 draw）
    }
    CategoryExtractSlotDict = {
        "Position": "vb0",
        "Texcoord": "vb1",
        "Blend": "vb2",
    }
    CategoryStrideDict = {
        "Position": 40,
        "Texcoord": 20,
        "Blend": 32,
    }


def _with_channel_plan(components):
    """为手工构造的组件记录补上 t75 通道计划（**用真实实现**推导，不塞假记录）。

    导出侧现在要求「通道计划缺失 = 该部件退出合并骨架」（绝不静默退回出现次
    判据），所以夹具必须像生产链路一样带上它。
    """
    if not components:
        return components
    channel_module = sys.modules[f"{PKG}.common.zzmi_channel"]
    records = channel_module.select_channel_plan(
        [
            {
                "draw_ib": component["draw_ib"],
                "vg_map": dict(component.get("vg_map") or {}),
                "vg_count": int(component.get("vg_count") or 0),
                "vg_offset": int(component.get("vg_offset") or 0),
                "skeleton_group": int(component.get("skeleton_group") or 0),
            }
            for component in components
        ]
    )
    for index, component in enumerate(components):
        record = records.get(component["draw_ib"])
        if record is not None and "channel" not in component:
            component["channel"] = dict(record)
            component["channel_digest"] = ""
    return components


class _FakeSubmesh:
    def __init__(self, unique_str, vg_offset=0, vg_count=0, skeleton_group=0, vg_map=None,
                 deform_draw=0, original_vertex_count=0, vertex_count=0,
                 exported_vertex_count=0, match_first_index=0):
        self.unique_str = unique_str
        self.match_first_index = match_first_index
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.skeleton_group = skeleton_group
        # 缺省 identity：local -> vg_offset + local（与真实反查写回一致）
        self.vg_map = vg_map if vg_map is not None else {
            local: vg_offset + local for local in range(vg_count)
        }
        # t75：通道计划由导出侧 `_merged_resolve_channel_plans` 按整组补算
        self.channel_plan_version = 0
        self.channel_plan = {}
        self.channel_plan_digest = ""
        self.channel_plan_slot_weights = {}
        # 生产侧 draw_ib 来自 `drawib_model.draw_ib`；夹具显式给出同口径的值。
        self.draw_ib = unique_str.split(".", 1)[-1].split("-", 1)[0]
        # ZZMI 导出侧守卫元数据（反查写回）：deform draw 序号 / 原部件顶点数
        self.deform_draw_index = deform_draw
        self.original_vertex_count = original_vertex_count
        self.vertex_count = vertex_count
        # 导出 buffer 顶点数（去重后；_submesh_exported_vertex_count 用）
        self.index_vertex_id_dict = (
            list(range(exported_vertex_count)) if exported_vertex_count else None
        )
        self.category_buffer_dict = {}
        self.drawcall_model_list = []


class _FakeDrawCall:
    """最小 DrawCall 桩：契约的几何判据要按对象名解析 Blender 对象。"""

    def __init__(self, obj_name):
        self.obj_name = obj_name
        self.source_obj_name = ""

    def get_blender_obj_name(self):
        return self.source_obj_name or self.obj_name


class _FakeDrawIBModel:
    def __init__(self, draw_ib, submesh_model_list, part_map=None):
        self.draw_ib = draw_ib
        self.draw_ib_alias = draw_ib
        self.draw_number = 4643
        self.vertex_limit_hash = "dd9c8d5e"
        self.d3d11GameType = _FakeGameType()
        # 游戏类型桩使用类属性作为默认值；每个 DrawIB 必须复制布局字典，
        # 否则异构 BI4/BI16 回归测试会互相污染。
        self.d3d11GameType.CategoryStrideDict = dict(
            _FakeGameType.CategoryStrideDict
        )
        self.category_hash_dict = {
            "Position": "122883aa",
            "Texcoord": "5c0fefda",
            "Blend": "bf543990",
        }
        self.submesh_model_list = submesh_model_list
        self.category_buffer_dict = {}
        self.match_first_index_partname_dict = part_map or {}
        self.submesh_ib_dict = {
            submesh.unique_str: b"\x00\x00\x00\x00" for submesh in submesh_model_list
        }
        self.obj_name_draw_offset = {}

    def get_submesh_texture_override_suffix(self, submesh_model):
        return submesh_model.unique_str.replace("-", "_")

    def get_submesh_ib_resource_name(self, submesh_model):
        return "Resource_" + submesh_model.unique_str.replace("-", "_") + "_Index"

    def get_submesh_texture_markup_info_list(self, submesh_model):
        return []


def _make_exporter(drawib_models, merged_vgmap=True, ordered_drawcalls=None):
    _fake_global_properties.import_merged_vgmap = lambda: merged_vgmap
    blueprint_model = types.SimpleNamespace(
        cross_ib_info_dict={},
        cross_ib_method_dict={},
        cross_ib_mapping_method={},
        has_cross_ib=False,
        cross_ib_object_names=set(),
        keyname_mkey_dict={},
        ordered_draw_obj_data_model_list=(ordered_drawcalls if ordered_drawcalls is not None else []),
    )
    exporter = _zzmi_module.ExportZZMI(blueprint_model)
    exporter.drawib_model_list = drawib_models
    return exporter


def _cross_group_switch(enabled):
    """把跨组开关钉在指定值（其余属性开关保持各自声明的默认）。

    该开关默认已开启，凡是要验「关闭路径」的用例都必须显式钉住它，不能依赖默认值。
    """
    return mock.patch.object(
        sys.modules[f"{PKG}.ui.universal.zzmi"],
        "_zzmi_prop_flag",
        lambda name, default: (
            enabled
            if name == ZZMICrossGroupAttachTests.SWITCH
            else bool(default)
        ),
    )


class ZZSIMergedSkeletonCollectTests(unittest.TestCase):
    def test_collect_gated_by_checkbox(self):
        models = [_FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51)])]
        exporter = _make_exporter(models, merged_vgmap=False)
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})

    def test_collect_dedup_by_drawib_and_sort(self):
        models = [
            _FakeDrawIBModel("84618ee0", [
                _FakeSubmesh("LOD0.84618ee0-22296-0", 105, 49),
                _FakeSubmesh("LOD0.84618ee0-1164-22296", 105, 49),
            ]),
            _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105)]),
            _FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51)]),
        ]
        exporter = _make_exporter(models, merged_vgmap=True)
        components, id_dict = exporter._collect_merged_skeleton_components()
        # 按 vg_offset 排序；84618ee0 两个子网格只收一个
        self.assertEqual([c["draw_ib"] for c in components], ["a23aa8a3", "84618ee0", "b20f90ea"])
        self.assertEqual(id_dict, {"a23aa8a3": 0, "84618ee0": 1, "b20f90ea": 2})
        self.assertEqual(sum(c["vg_count"] for c in components), 205)

    def test_collect_skips_submesh_without_data(self):
        models = [_FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 0)])]
        exporter = _make_exporter(models, merged_vgmap=True)
        components, _ = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])

    def test_collect_rejects_vgmap_slot_outside_component_range(self):
        models = [_FakeDrawIBModel(
            "b20f90ea",
            [_FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 1, vg_map={0: 999})],
        )]
        exporter = _make_exporter(models, merged_vgmap=True)
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})

    def test_collect_rejects_stale_vgmap_algorithm_version(self):
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 1)
        submesh.vg_map_algorithm_version = 1
        exporter = _make_exporter(
            [_FakeDrawIBModel("b20f90ea", [submesh])], merged_vgmap=True
        )
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})

    def test_collect_accepts_migratable_v4_cache_with_complete_vgmap(self):
        """**阻断修复（t80 §2.4）**：v4 缓存内容完整时**不得**再被拒绝。

        可证伪：修复前这里 `components == []`（逐件拒绝）→ 合并骨架契约把
        「全部被拒」升级成整次导出中止，而"重新一键导入"依赖的 FrameAnalysis
        帧可能已被用户删除 ⇒ 用户无路可走。修复后按当前算法就地补算通道计划。
        """
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 4)
        submesh.vg_map_algorithm_version = 4  # 旧版但 VGMap 完整覆盖 0..3
        exporter = _make_exporter(
            [_FakeDrawIBModel("b20f90ea", [submesh])], merged_vgmap=True
        )
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(
            [component["draw_ib"] for component in components],
            ["b20f90ea"],
            "v4 且内容完整的缓存必须放行，不得整次导出中止",
        )
        self.assertEqual(id_dict, {"b20f90ea": 0})
        self.assertEqual(exporter._zzmi_merged_contract_stats["skip_reasons"], {})
        self.assertEqual(
            exporter._zzmi_merged_contract_stats["migrated_cache"], {"b20f90ea": 4}
        )

    def test_collect_rejects_v4_cache_with_incomplete_vgmap(self):
        """反向可证伪：版本旧**且**内容不完整（缺键）时必须仍然拒绝。

        缺键会让 attach CS 的 `vg_map.get(local, 0)` 静默塌缩到槽位 0（整块蒙皮
        炸裂）⇒ 不能靠"版本号旧就放行"换成活下来。
        """
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 4)
        submesh.vg_map_algorithm_version = 4
        submesh.vg_map = {0: 0, 1: 1}  # 缺 2/3
        exporter = _make_exporter(
            [_FakeDrawIBModel("b20f90ea", [submesh])], merged_vgmap=True
        )
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})
        reason = exporter._zzmi_merged_contract_stats["skip_reasons"]["b20f90ea"]
        self.assertIn(
            "内容不完整",
            reason,
            "版本旧**且**内容不完整必须按'内容不完整'拒绝（不是'过旧'就放行）",
        )


class ZZSIMergedSkeletonIniTests(unittest.TestCase):
    def _build_vb_section(self, with_merged=True):
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51)
        model = _FakeDrawIBModel("b20f90ea", [submesh])
        exporter = _make_exporter([model], merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        exporter.has_merged_skeleton = len(exporter.merged_skeleton_components) > 0
        if not with_merged:
            exporter.merged_skeleton_component_id_dict = {}
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        lines = _all_builder_lines(builder)
        return lines

    def test_vb_section_injects_copy_and_swap(self):
        lines = self._build_vb_section(with_merged=True)
        text = "\n".join(lines)

        # v9：出现次槽位（s1/s2）——deform 段顶层自增出现次、顶层 sticky 累加
        # 到达标记、按出现次把当帧 palette 复制进对应槽、再顶层无条件 run 全部
        # (部件, 槽) attach。
        idx_occ_inc = text.index("$zz_ms_occ_0 = $zz_ms_occ_0 + 1")
        idx_occ_wrap = text.index("$zz_ms_occ_0 >= 3")
        idx_seen1 = text.index("$zz_ms_seen_01 = $zz_ms_seen_01 + ($zz_ms_occ_0 == 1)")
        idx_seen2 = text.index("$zz_ms_seen_02 = $zz_ms_seen_02 + ($zz_ms_occ_0 == 2)")
        idx_copy = text.index(
            "if $zz_ms_occ_0 == 1\n"
            "    ResourceZZPalette_b20f90ea_s1 = copy vs-t0 unless_null"
        )
        idx_run = text.index("run = CustomShaderZZMIMergedSkeletonAttach_C0_s1")
        idx_swap = text.index("vs-t0 = ResourceZZMergedSkeleton_G0_s1")
        self.assertLess(idx_occ_inc, idx_occ_wrap)
        self.assertLess(idx_occ_wrap, idx_seen1)
        self.assertLess(idx_seen1, idx_seen2)
        self.assertLess(idx_seen2, idx_copy)
        self.assertLess(idx_copy, idx_run)
        self.assertLess(idx_run, idx_swap)
        # 运行时不重放持久 palette，避免脏数据。
        self.assertNotIn("$zz_ms_attach_offset", text)

    def test_vb_section_without_merged_stays_legacy(self):
        """非合并路径必须保留「抑制原 deform draw + 按原顶点数重绘」的旧契约。

        B3/C1 回归修复（用户裁决 2026-09-11）：本用例此前被反转成 assertNotIn，
        但 HEAD（`git show HEAD:ui/universal/zzmi.py` 809-865）在 Blend 类别分支内
        **无条件**发 `handling = skip` + `draw = <draw_number>, 0`；真实导出留档
        `.dbg/backup-20260817-090654/SSMTGeneratedMod/蕾米埃尔·影池独舞.ini:496-501`
        亦为该形态。故断言恢复为 assertIn，并保留"不注入合并骨架语句"的部分。
        """
        lines = self._build_vb_section(with_merged=False)
        text = "\n".join(lines)
        self.assertNotIn("ResourceZZMergedSkeleton", text)
        self.assertNotIn("CustomShaderZZMIMergedSkeletonAttach", text)
        self.assertNotIn("$zz_ms_occ_", text)
        self.assertNotIn("$zz_ms_seen_", text)
        self.assertNotIn("ResourceZZPalette", text)
        # 非合并路径：deform 段必须含「抑制原 draw + 重发」两条指令（旧契约）
        self.assertIn("handling = skip", text)
        self.assertIn("draw = 4643, 0", text)
        self.assertIn("vb0 = Resourceb20f90eaPosition", text)

    def test_merged_skeleton_sections_content(self):
        submesh_a = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105)
        submesh_b = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51)
        exporter = _make_exporter(
            [_FakeDrawIBModel("a23aa8a3", [submesh_a]), _FakeDrawIBModel("b20f90ea", [submesh_b])],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        with _cross_group_switch(False):
            exporter.add_merged_skeleton_sections(builder)
        lines = _all_builder_lines(builder)
        text = "\n".join(lines)

        self.assertIn("global $zz_ms_occ_0 = 0", text)
        self.assertIn("global $zz_ms_occ_1 = 0", text)
        self.assertIn("global $zz_ms_seen_01 = 0", text)
        self.assertIn("global $zz_ms_seen_02 = 0", text)
        self.assertIn("global $zz_ms_seen_11 = 0", text)
        self.assertIn("global $zz_ms_seen_12 = 0", text)
        # 按槽「上一帧到达」预测值也要在 [Constants] 声明（守卫条件引用它们）
        for cid in (0, 1):
            for slot in (1, 2):
                # 初值 1：首帧保守等待（没有上一帧可参考时按「都会到」处理）
                self.assertIn(f"global $zz_ms_prev_{cid}{slot} = 1", text)
        # 每槽一份合并骨架
        self.assertIn("[ResourceZZMergedSkeleton_G0_s1]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G0_s2]", text)
        # 【2026-09-22 跨显卡修复】合并骨架必须是 **typed** RWBuffer，不能是结构化。
        # 游戏的 deform VS 按 typed 索引 `palette[3*bone+m]` 读本缓冲；若描述符是
        # 结构化（SBS=48）则「声明步长 vs 视图元素尺寸」不匹配，落进 D3D11 未定义区：
        # AMD 驱动改用 SBS(48) 算字节偏移 → 索引被放大 3 倍 → 骨号越过写入区间时
        # 越界读返回精确 0（实测 A 卡 5939/11111 个顶点为零，阈值恰为 ⌈156/3⌉）。
        self.assertIn("type = RWBuffer", text)
        self.assertIn("format = R32G32B32A32_FLOAT", text)
        self.assertIn("array = 468", text)  # 3 × 156（全宽 = max(0+105, 105+51) = 156）
        # bind_flags 必须显式声明（EFMI 的 ResourceMergedSkeletonDataRW 同款坑）：
        # RWBuffer 缺 bind_flags 会导致 SRV 创建静默失败 → 绑 NULL → deform 读全零。
        self.assertIn("bind_flags = shader_resource unordered_access", text)
        self.assertNotIn("type = RWStructuredBuffer", text)
        # 骨架资源段内不得再有结构化 stride（array 已按 3× 全宽声明）
        for group in ("G0_s1", "G0_s2"):
            block = text.split(f"[ResourceZZMergedSkeleton_{group}]", 1)[1].split("\n[", 1)[0]
            self.assertIn("type = RWBuffer", block)
            self.assertIn("bind_flags = shader_resource unordered_access", block)
            self.assertNotIn("stride = ", block)
        # 无 CB1 校准：不出捕获资源/捕获段/校准引用
        self.assertNotIn("ResourceZZCb1", text)
        self.assertNotIn("Cb1Capture", text)
        self.assertNotIn("cs-cb1", text)
        self.assertNotIn("cs-cb2", text)
        # palette 持久副本资源：每部件每槽一份
        self.assertIn("[ResourceZZPalette_a23aa8a3_s1]", text)
        self.assertIn("[ResourceZZPalette_a23aa8a3_s2]", text)
        self.assertIn("[ResourceZZPalette_b20f90ea_s1]", text)
        self.assertIn("[ResourceZZPalette_b20f90ea_s2]", text)
        # identity 映射：a23aa8a3 槽位 0..104（vg_map 用 filename 二进制加载——
        # 多行 data 在本 3DMigoto fork 上只写第 0 个元素，2026-08-23 实证）
        self.assertIn("[ResourceZZVgMap_a23aa8a3]", text)
        self.assertIn("type = Buffer", text)
        self.assertIn("format = R32G32B32A32_UINT", text)
        self.assertIn("filename = Meshes/zz_vgmap_a23aa8a3.buf", text)
        self.assertIn("[ResourceZZVgMap_b20f90ea]", text)
        self.assertIn("filename = Meshes/zz_vgmap_b20f90ea.buf", text)
        # 每槽一份 SO 重定向资源
        self.assertIn("[ResourceZZRedirectSO_G0_s1]", text)
        self.assertIn("[ResourceZZRedirectSO_G0_s2]", text)
        self.assertNotIn("[ResourceZZRedirectSO_a23aa8a3]", text)
        # 逐 (部件, 槽) attach 段（x1=0 / y1=vg_count；cs-t0 = 本槽 palette；
        # cs-u0 = 本组本槽骨架；Dispatch 动态取整）
        for cid in (0, 1):
            for slot in (1, 2):
                self.assertIn(
                    f"[CustomShaderZZMIMergedSkeletonAttach_C{cid}_s{slot}]", lines
                )
        self.assertIn("cs = ./res/zzmi_merged_skeleton_attach.hlsl", text)
        self.assertIn("x1 = 0", text)
        self.assertIn("y1 = 105", text)
        self.assertIn("cs-t0 = ref ResourceZZPalette_a23aa8a3_s1", text)
        self.assertIn("cs-t0 = ref ResourceZZPalette_a23aa8a3_s2", text)
        self.assertIn("cs-t1 = ref ResourceZZVgMap_a23aa8a3", text)
        self.assertIn("cs-t0 = ref ResourceZZPalette_b20f90ea_s1", text)
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G0_s1", text)
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G0_s2", text)
        self.assertIn("Dispatch = 2, 1, 1", text)  # ceil(105 / 64)
        # [Present] 先把 seen 抄进 prev（下一帧守卫的按槽「期望集合」预测值），
        # 再清零 occ/seen；不重放 attach、不复位任何资源。
        self.assertIn("[Present]", text)
        present_text = text.split("[Present]")[1]
        self.assertIn("$zz_ms_occ_0 = 0", present_text)
        self.assertIn("$zz_ms_occ_1 = 0", present_text)
        self.assertIn("$zz_ms_seen_01 = 0", present_text)
        self.assertIn("$zz_ms_seen_12 = 0", present_text)
        # 抄录语句必须在清零语句**之前**（否则 prev 恒为 0 → 帧首守卫提前落笔）
        for cid in (0, 1):
            for slot in (1, 2):
                copy_line = f"$zz_ms_prev_{cid}{slot} = $zz_ms_seen_{cid}{slot}"
                clear_line = f"$zz_ms_seen_{cid}{slot} = 0"
                self.assertIn(copy_line, present_text)
                self.assertLess(
                    present_text.index(copy_line),
                    present_text.index(clear_line),
                    "prev 抄录必须早于 seen 清零",
                )
        # 「本帧出现过」口径（$zz_ms_any_*）已被按槽预测取代：帧首恒真会让守卫
        # 在载体自己的 deform pass 就落笔（2026-09-17 dump 实证）。
        self.assertNotIn("$zz_ms_any_", text)
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach_", present_text)
        self.assertNotIn("$zz_ms_attach_offset", present_text)
        self.assertNotIn("$zz_ms_attach_count", present_text)
        self.assertNotIn("ResourceZZRedirectSO", present_text)
        self.assertNotIn("ResourceZZPalette", present_text)
        self.assertNotIn("ResourceZZMergedSkeleton", present_text)
        self.assertNotIn("= null", present_text)

    def test_every_attach_section_carries_freshness_guard(self):
        """每个 attach 段都必须带「本帧是否捕获过这份 palette」守卫（2026-10-20）。

        根因（丽娜槽 97 运动时漂移，实机复现）：跨部件按骨骼矩阵 bitwise 去重后，
        同一个 canonical 槽位会被**多个部件**写，而每个部件的 palette 只在**它自己
        的 deform 段**捕获。某部件本帧没画（实测：只剩 3 个顶点的子网格被裁掉）
        时它的 palette 停在上一帧，attach 又每个段都无条件跑一遍 ⇒ 用上一帧矩阵
        盖掉本帧刚写好的共享槽位。修法是让 attach CS 自己拒绝陈旧输入：
        `y2 = 1` 启用守卫、`z2 = $zz_ms_seen_<部件><槽>` 传本帧到达计数
        （与 `_merged_seen_arrived_condition` 同一口径）。
        """
        submesh_a = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105)
        submesh_b = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51)
        exporter = _make_exporter(
            [_FakeDrawIBModel("a23aa8a3", [submesh_a]), _FakeDrawIBModel("b20f90ea", [submesh_b])],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        for cid in (0, 1):
            for slot in (1, 2):
                block = text.split(
                    f"[CustomShaderZZMIMergedSkeletonAttach_C{cid}_s{slot}]", 1
                )[1].split("\n[", 1)[0]
                self.assertIn("y2 = 1", block, f"C{cid}_s{slot} 缺守卫开关")
                self.assertIn(
                    f"z2 = $zz_ms_seen_{cid}{slot}", block, f"C{cid}_s{slot} 守卫计数错槽"
                )
                # 第 1 组参数被 y1=骨骼数与蒙皮 CS 的 z1/w1 占用，attach 不许碰
                self.assertNotIn("z1 = ", block)
                self.assertNotIn("w1 = ", block)

        # 陈旧 palette 的读取必须在 CS 里被拒（run 仍是顶层无条件，
        # 计划书 §8「run 绝不进 if」不变）
        shader = (REPO_ROOT / "Toolset" / "zzmi_merged_skeleton_attach.hlsl").read_text(
            encoding="utf-8"
        )
        self.assertIn("#define ZZ_ATTACH_SEEN IniParams[2].z", shader)
        self.assertIn("#define ZZ_ATTACH_GUARD IniParams[2].y", shader)
        self.assertIn("ZZ_ATTACH_GUARD > 0.5f && ZZ_ATTACH_SEEN < 0.5f", shader)
        # 守卫必须排在任何资源读取之前（陈旧 palette 连长度都可能不对）
        guard_at = shader.index("ZZ_ATTACH_GUARD > 0.5f && ZZ_ATTACH_SEEN < 0.5f")
        self.assertLess(guard_at, shader.index("src_palette.GetDimensions"))

    def test_merged_skeleton_resource_is_typed_rwbuffer(self):
        """合并骨架资源必须是 typed RWBuffer + 显式 bind_flags（2026-09-22 跨显卡修复）。

        背景（A 卡模型爆炸的根因）：游戏的 deform VS 按 **typed 索引**
        `palette[3*bone + m]` 读取合并骨架。若本资源声明成结构化（SBS=48），
        则「视图元素尺寸(16) vs 声明步长(48)」不匹配，落进 D3D11 未定义区 ——
        实测 AMD 驱动改用 SBS(48) 算字节偏移，索引被放大 3 倍：骨号 b 实际读到
        槽位 3b/3b+1/3b+2 的矩阵混合，越过写入区间时越界读返回精确 0
        （A 卡 5939/11111 个顶点位置为零，阈值恰为 ⌈bones_count/3⌉）；
        NVIDIA 驱动按视图元素尺寸取址，因此正常。

        修法：改成 typed（format + array = 3 × bones_count，字节数不变），
        与 `palette[3b+m]` 精确匹配 → 两卡取址一致。

        bind_flags 必须显式写（EFMI 的 ResourceMergedSkeletonDataRW 同款坑）：
        RWBuffer 缺 bind_flags 会导致 SRV 创建**静默失败**并绑 NULL ——
        实测过一次：骨架内容完全正确但 4 个 SO 全部 100% 零。
        """
        submesh_a = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105)
        submesh_b = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51)
        exporter = _make_exporter(
            [_FakeDrawIBModel("a23aa8a3", [submesh_a]), _FakeDrawIBModel("b20f90ea", [submesh_b])],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        # 全宽 = max(0+105, 105+51) = 156 槽；typed 元素数 = 156 × 3 = 468
        # （本 fixture 只有 G0 组：两个部件同组）
        for group in ("G0_s1", "G0_s2"):
            self.assertIn(f"[ResourceZZMergedSkeleton_{group}]", text)
            block = text.split(f"[ResourceZZMergedSkeleton_{group}]", 1)[1].split("\n[", 1)[0]
            self.assertIn("type = RWBuffer", block)
            self.assertIn("format = R32G32B32A32_FLOAT", block)
            self.assertIn("array = 468", block)
            self.assertIn("bind_flags = shader_resource unordered_access", block)
            # 不得再有结构化声明残留
            self.assertNotIn("RWStructuredBuffer", block)
            self.assertNotIn("stride = ", block)
        # 声明顺序：type → format → array → bind_flags
        block = text.split("[ResourceZZMergedSkeleton_G0_s1]", 1)[1].split("\n[", 1)[0]
        self.assertLess(block.index("type = RWBuffer"), block.index("format = "))
        self.assertLess(block.index("format = "), block.index("array = "))
        self.assertLess(block.index("array = "), block.index("bind_flags = "))
        # palette 侧不得加 format（加 format 会把 SBS 改成 16、ByteWidth 缩到
        # 16×vg_count，而 copy vs-t0 要拷 48×vg_count 字节 → 溢出）
        pal = text.split("[ResourceZZPalette_a23aa8a3_s1]", 1)[1].split("\n[", 1)[0]
        self.assertIn("stride = 48", pal)
        self.assertNotIn("format = ", pal)

    def test_merged_skeleton_sections_per_group(self):
        """组内统一骨架版：每组一套全宽骨架资源；逐部件直拷 attach 到本组；无任何捕获/校准段。"""
        # 组 0（身体）：a23aa8a3(0,105) + b20f90ea(105,51)
        # 组 1（头部）：64d7d56f(156,1) + b51bdd59(157,11)
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)]),
                _FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)]),
                _FakeDrawIBModel("64d7d56f", [_FakeSubmesh("LOD0.64d7d56f-900-0", 156, 1, 1)]),
                _FakeDrawIBModel("b51bdd59", [_FakeSubmesh("LOD0.b51bdd59-864-0", 157, 11, 1)]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        with _cross_group_switch(False):
            exporter.add_merged_skeleton_sections(builder)
        lines = _all_builder_lines(builder)
        text = "\n".join(lines)

        self.assertIn("[ResourceZZMergedSkeleton_G0_s1]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G0_s2]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G1_s1]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G1_s2]", text)
        # 两组各两槽、全宽 = 全局 max(157+11) = 168 槽；typed 元素数 = 3 × 168 = 504
        # （只数骨架资源的 array 行；palette 副本资源自带 array=vg_count 行，需排除——
        #  骨架段结构：header / type / format / array / bind_flags，故取 i+3）
        skeleton_arrays = [
            lines[i + 3]
            for i, line in enumerate(lines)
            if line.startswith("[ResourceZZMergedSkeleton_G")
        ]
        self.assertEqual(skeleton_arrays, ["array = 504"] * 4)
        # 无捕获段、无校准资源、无 cb 引用
        self.assertNotIn("Cb1Capture", text)
        self.assertNotIn("ResourceZZCb1", text)
        self.assertNotIn("cs-cb1", text)
        self.assertNotIn("cs-cb2", text)
        # 逐 (部件, 槽) attach：4 部件 × 2 槽 = 8 段，各自写回**本组本槽**骨架
        for cid in range(4):
            for slot in (1, 2):
                self.assertIn(
                    f"[CustomShaderZZMIMergedSkeletonAttach_C{cid}_s{slot}]", lines
                )
        c0 = text.split("[CustomShaderZZMIMergedSkeletonAttach_C0_s1]")[1].split("[")[0]
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G0_s1", c0)  # C0 属组 0
        self.assertIn("cs-t1 = ref ResourceZZVgMap_a23aa8a3", c0)
        c2 = text.split("[CustomShaderZZMIMergedSkeletonAttach_C2_s2]")[1].split("[")[0]
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G1_s2", c2)  # C2 属组 1
        self.assertIn("cs-t1 = ref ResourceZZVgMap_64d7d56f", c2)
        # [Present] 只清零 occ/seen，不重放 attach
        present_text = text.split("[Present]")[1]
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach_", present_text)

    def test_vb_section_rebinds_to_own_group_resource(self):
        """每个 deform VB 段按出现次捕获 palette 并顶层 attach 到本组本槽骨架。"""
        model_g1 = _FakeDrawIBModel("64d7d56f", [_FakeSubmesh("LOD0.64d7d56f-900-0", 156, 1, 1)])
        model_g0 = _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)])
        exporter = _make_exporter([model_g1, model_g0], merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )

        builder0 = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder0, model_g0)
        text0 = "\n".join(builder0.sections[0].SectionLineList)
        self.assertIn("ResourceZZPalette_a23aa8a3_s1 = copy vs-t0 unless_null", text0)
        self.assertIn("ResourceZZPalette_a23aa8a3_s2 = copy vs-t0 unless_null", text0)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C0_s1", text0)
        self.assertIn("vs-t0 = ResourceZZMergedSkeleton_G0_s1", text0)

        builder1 = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder1, model_g1)
        text1 = "\n".join(builder1.sections[0].SectionLineList)
        self.assertIn("ResourceZZPalette_64d7d56f_s1 = copy vs-t0 unless_null", text1)
        self.assertIn("ResourceZZPalette_64d7d56f_s2 = copy vs-t0 unless_null", text1)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C1_s1", text1)
        self.assertIn("vs-t0 = ResourceZZMergedSkeleton_G1_s1", text1)

        # [Present] 只清零 occ/seen，不重放 attach。
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        present_text = "\n".join(_all_builder_lines(builder)).split("[Present]")[1]
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach_", present_text)
        self.assertIn("$zz_ms_occ_0 = 0", present_text)
        self.assertIn("$zz_ms_occ_1 = 0", present_text)
        self.assertIn("$zz_ms_seen_01 = 0", present_text)
        self.assertIn("$zz_ms_seen_12 = 0", present_text)

    def test_merged_skeleton_buffer_covers_offset_gap(self):
        """回归：中间部件缺失时 buffer 必须按 max(vg_offset+vg_count) 声明，而非 sum(vg_count)。

        场景（用户实测）：3 个部件统一顶点组 0~10 / 11~30 / 31~50，
        用户 join 部件 1+3、部件 2 不生成 → 导出组件 (0,11) + (31,20)。
        sum(vg_count)=31 会让部件 3 的 attach（offset=31）与顶点全局 id 31~50 越界；
        正确口径 max(vg_offset+vg_count)=51。
        """
        submesh_1 = _FakeSubmesh("LOD0.aaaaaaaa-100-0", 0, 11)    # 部件 1：0~10
        submesh_3 = _FakeSubmesh("LOD0.cccccccc-300-0", 31, 20)   # 部件 3：31~50（部件 2 缺席）
        exporter = _make_exporter(
            [_FakeDrawIBModel("aaaaaaaa", [submesh_1]), _FakeDrawIBModel("cccccccc", [submesh_3])],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        # typed：元素数 = 3 × 槽位数（max(0+11, 31+20) = 51，而非 sum=31）
        self.assertIn("array = 153", text)

    def test_g4_slots_use_runtime_merged_skeleton_bounds(self):
        """G4 的 249..265 槽必须由实际 UAV 长度放行，不能被角色专用常量截断。"""
        shader = (REPO_ROOT / "Toolset" / "zzmi_merged_skeleton_attach.hlsl").read_text(
            encoding="utf-8"
        )

        threads = _zzmi_module.ExportZZMI.MERGED_SKELETON_ATTACH_THREADS
        self.assertIn(f"[numthreads({threads}, 1, 1)]", shader)
        # 【2026-09-22 跨显卡修复】palette 与骨架都改成了 typed（Buffer<float4>），
        # GetDimensions 只返回元素数，骨骼/槽位数需 /3 还原。
        self.assertIn("src_palette.GetDimensions(palette_count)", shader)
        self.assertIn("palette_count /= 3u", shader)
        self.assertIn("vg_map.GetDimensions(vg_map_count)", shader)
        self.assertIn("merged_skeleton.GetDimensions(merged_count)", shader)
        self.assertIn("merged_count /= 3u", shader)
        self.assertIn("slot < merged_count", shader)
        self.assertNotIn("slot < 249", shader)

        submesh_add = _FakeSubmesh(
            "LOD0.add6ff13-624-0",
            249,
            1,
            4,
            vg_map={0: 249},
        )
        submesh_d892 = _FakeSubmesh(
            "LOD0.d892c658-2256-0",
            250,
            16,
            4,
            vg_map={local: 250 + local for local in range(16)},
        )
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("add6ff13", [submesh_add]),
                _FakeDrawIBModel("d892c658", [submesh_d892]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        self.assertIn("[ResourceZZMergedSkeleton_G4_s1]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G4_s2]", text)
        self.assertEqual(text.count("[ResourceZZMergedSkeleton_G4_s"), 2)
        # typed：元素数 = 3 × 槽位数（266 槽 = 全局 max(249+1, 245+21)）
        self.assertIn("array = 798", text)
        meshes_path = Path(_FAKE_MOD_FOLDER) / "Meshes"
        add_slots = [
            value[0]
            for value in struct.iter_unpack(
                "<4I", (meshes_path / "zz_vgmap_add6ff13.buf").read_bytes()
            )
        ]
        d892_slots = [
            value[0]
            for value in struct.iter_unpack(
                "<4I", (meshes_path / "zz_vgmap_d892c658.buf").read_bytes()
            )
        ]
        self.assertEqual(add_slots + d892_slots, list(range(249, 266)))

    def test_attach_dispatch_scales_past_512_bones(self):
        """numthreads=64 时，513 根 palette 必须生成 9 个 dispatch group。"""
        count = 513
        submesh = _FakeSubmesh("LOD0.aaaaaaaa-100-0", 0, count)
        exporter = _make_exporter(
            [_FakeDrawIBModel("aaaaaaaa", [submesh])], merged_vgmap=True
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        self.assertIn("y1 = 513", text)
        self.assertIn("Dispatch = 9, 1, 1", text)

    def test_vgmap_publish_failure_aborts_and_preserves_previous_file(self):
        submesh = _FakeSubmesh("LOD0.aaaaaaaa-100-0", 0, 1)
        exporter = _make_exporter(
            [_FakeDrawIBModel("aaaaaaaa", [submesh])], merged_vgmap=True
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        target = Path(_FAKE_MOD_FOLDER) / "Meshes" / "zz_vgmap_aaaaaaaa.buf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"previous")

        with mock.patch.object(_zzmi_module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                exporter.add_merged_skeleton_sections(_FakeIniBuilder())
        self.assertEqual(target.read_bytes(), b"previous")

    def test_skin_publish_shader_matches_generator_parameters(self):
        """蒙皮 CS 与生成器参数必须同源（行布局 / 线程数 / 参数槽位）。

        回归（2026-09-17 FrameAnalysis-025058「一直闪」）：draw 版重放只能在
        Blend 布局与载体一致的锚点落笔，窄布局必需部件最后到达时无人落笔 →
        该槽 SO 整帧不写。修复 = 新增 `res/zzmi_merged_skin.hlsl`：完全绕开 IA
        输入布局（顶点属性按 SV_DispatchThreadID 从 SRV 读），**任意必需部件**
        都能发布，且按索引写 = 幂等（帧内最后一次派发即最终内容）。
        """
        shader = (REPO_ROOT / "Toolset" / "zzmi_merged_skin.hlsl").read_text(
            encoding="utf-8"
        )
        # 行布局：位置 0..2 / 法线 3..5 / 切线 6..9（40 字节 = 10 floats）
        self.assertIn("struct ZZVertex40", shader)
        self.assertIn("RWStructuredBuffer<float> dst_rows", shader)
        self.assertIn("[numthreads(64, 1, 1)]", shader)
        self.assertIn("SV_DispatchThreadID", shader)
        self.assertIn("src_rows.GetDimensions(src_count, src_stride)", shader)
        # 【2026-09-22 跨显卡修复】骨架改成 typed 后，本 CS 也必须同步：
        # Buffer<float4> 声明 + GetDimensions 单参 + /3 还原骨骼数 + bone*3 取址
        self.assertIn("Buffer<float4> merged_skeleton : register(t2);", shader)
        self.assertNotIn("StructuredBuffer<ZZBone3x4> merged_skeleton", shader)
        self.assertIn("merged_skeleton.GetDimensions(bone_count)", shader)
        self.assertIn("bone_count /= 3u", shader)
        self.assertIn("bone * 3u", shader)
        self.assertIn("bone >= bone_count", shader)
        # ini 参数按「资源行数」不变量在运行时选组（[0]/[1] 归属随 fork 变化）；
        # 只用 y1/z1 两个逻辑参数，行数与每行 float 数改从资源取。
        self.assertIn("IniParams[1]", shader)
        self.assertIn("IniParams[0]", shader)
        self.assertIn("param.y", shader)
        self.assertIn("param.z", shader)
        self.assertIn("float4 t = float4(v.b.z, v.b.w, v.c.x, v.c.y);", shader)
        self.assertEqual(
            _zzmi_module.ExportZZMI._merged_skin_dispatch_count(18776, 3), 294
        )
        self.assertEqual(_zzmi_module.ExportZZMI._merged_skin_dispatch_count(0, 3), 1)
        self.assertEqual(
            _zzmi_module.ExportZZMI._merged_skin_shader_filename(),
            "zzmi_merged_skin.hlsl",
        )

    def test_missing_attach_shader_aborts_export(self):
        exporter = _make_exporter([], merged_vgmap=True)
        with mock.patch.object(_zzmi_module.os.path, "isfile", return_value=False):
            with self.assertRaises(FileNotFoundError):
                exporter._copy_merged_skeleton_shader_to_mod()


class ZZMICrossGroupGuardTests(unittest.TestCase):
    """跨组别引用守卫（无校准模式）：引用非本组骨骼 id 必须大声报警。"""

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, bone_ids, stub=False):
        """注册 fake 对象：顶点 i 权重挂顶点组 i，组名 = bone_ids[i]（全局骨骼 id）。"""
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[types.SimpleNamespace(group=i, weight=1.0)]
            )
            for i in range(len(bone_ids))
        ]
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        for bone_id in bone_ids:
            obj.vertex_groups.append(_FakeVertexGroup(str(bone_id)))
        if stub:
            obj["ZZMI_STUB"] = 1
        return obj

    def _make_component_exporter(self, draw_ib, submesh, components):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        submesh.drawcall_model_list = [dcm(obj_name=submesh.unique_str)]
        exporter = _make_exporter([_FakeDrawIBModel(draw_ib, [submesh])], merged_vgmap=True)
        components = _with_channel_plan(components)
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(components)
        }
        return exporter

    def _capture_warnings(self, exporter):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exporter._warn_cross_group_bone_references()
        return buf.getvalue()

    def test_cross_group_reference_warns(self):
        """a23aa8a3（组 0，槽 0~104）的顶点引用了组 1 的骨骼 id 105 -> 报警。"""
        self._register_obj("LOD0.a23aa8a3-42759-0", [0, 100, 105])
        submesh = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51, "skeleton_group": 1},
        ]
        exporter = self._make_component_exporter("a23aa8a3", submesh, components)
        with _cross_group_switch(False):
            out = self._capture_warnings(exporter)
        self.assertIn("禁止跨组别骨骼合并", out)
        self.assertIn("a23aa8a3", out)
        self.assertIn("骨架组 G0", out)
        self.assertIn("105", out)
        self.assertIn("归属组: [1]", out)

    def test_same_group_reference_silent(self):
        """同组骨骼引用（含并入本组其它部件的骨骼 id）不报警。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [105, 106, 0])
        submesh = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51, "skeleton_group": 0},
        ]
        exporter = self._make_component_exporter("b20f90ea", submesh, components)
        out = self._capture_warnings(exporter)
        self.assertEqual(out, "")

    def test_stub_object_skipped(self):
        """占位小三角面（ZZMI_STUB，权重挂已注册槽；无反查数据时 "0"）不触发跨组报警。"""
        # 组 1 的范围是 [156,157)：stub 引用骨骼 0（组外）——若不跳过会误报
        self._register_obj("LOD0.64d7d56f-900-0", [0], stub=True)
        submesh = _FakeSubmesh("LOD0.64d7d56f-900-0", 156, 1, 1)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
            {"draw_ib": "64d7d56f", "vg_offset": 156, "vg_count": 1, "skeleton_group": 1},
        ]
        exporter = self._make_component_exporter("64d7d56f", submesh, components)
        out = self._capture_warnings(exporter)
        self.assertEqual(out, "")

    def test_mixed_known_and_unknown_owners_do_not_crash(self):
        """回归：越界骨骼里混有「别组骨骼」与「组外未知骨骼」时不得抛 TypeError。

        旧实现在这里对集合 `{1, "未知（不在导出组件范围）"}` 调 `sorted` ⇒
        `TypeError: '<' not supported between instances of 'str' and 'int'`，
        整次导出中止（实测用户报错：导出失败横幅 + 该 TypeError）。

        夹具口径（`_register_obj`）：`bone_ids[i]` 是**顶点组 i 的名字**，顶点 i
        挂在顶点组 i 上 ⇒ 顶点组名集合 == 该对象引用的骨骼 id 集合。
        """
        # 组 0 的合法槽 = 0..104；引用 105（组 1 的槽）与 300（不属于任何导出组件）
        self._register_obj("LOD0.a23aa8a3-42759-0", [0, 105, 300])
        submesh = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 2, "skeleton_group": 1},
        ]
        exporter = self._make_component_exporter("a23aa8a3", submesh, components)
        try:
            with _cross_group_switch(False):
                out = self._capture_warnings(exporter)
        except TypeError as exc:  # pragma: no cover - 修复前走这条
            self.fail(f"跨组守卫不应因类型混排抛 TypeError: {exc}")
        self.assertIn("禁止跨组别骨骼合并", out)
        self.assertIn("[105, 300]", out)
        # 1（组 1，int）与「未知」（str 哨兵）同列一份 —— 修复前正是 `sorted`
        # 对这两种类型混排炸掉；顺带把多个未知归属去重成一条。
        self.assertIn("归属组: ['未知（不在导出组件范围）', 1]", out)

    def test_unknown_owner_only_warns_with_unknown_label(self):
        """只有组外未知骨骼时：文案仍为「未知」，且使用同一个 int 哨兵，不混类型。"""
        self._register_obj("LOD0.a23aa8a3-42759-0", [0, 300])
        submesh = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0},
        ]
        exporter = self._make_component_exporter("a23aa8a3", submesh, components)
        with _cross_group_switch(False):
            out = self._capture_warnings(exporter)
        self.assertIn("禁止跨组别骨骼合并", out)
        self.assertIn("[300]", out)
        self.assertIn("['未知（不在导出组件范围）']", out)

    def test_string_skeleton_group_is_normalized(self):
        """组号是数字字符串（历史缓存记录）时：守卫与组列表都按 int 归一，不抛 TypeError。"""
        self._register_obj("LOD0.a23aa8a3-42759-0", [105, 300])
        submesh = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, "0")
        components = [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": "0"},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 2, "skeleton_group": "1"},
        ]
        exporter = self._make_component_exporter("a23aa8a3", submesh, components)
        try:
            with _cross_group_switch(False):
                out = self._capture_warnings(exporter)
                groups = exporter._merged_skeleton_groups()
        except TypeError as exc:  # pragma: no cover - 修复前走这条
            self.fail(f"组号归一后不应抛 TypeError: {exc}")
        self.assertIn("禁止跨组别骨骼合并", out)
        self.assertIn("归属组: ['未知（不在导出组件范围）', 1]", out)
        self.assertEqual(groups, [0, 1])


class ZZMICrossGroupAttachTests(unittest.TestCase):
    """F1（2026-10-10，实机复现件根因）：被吸收进本组网格的**别组**部件，其骨骼
    必须写进**宿主组**骨架缓冲，而不是只写它自己组。

    夹具对应实机形态：G0 的 `a23aa8a3`（槽 0..104）是宿主网格，其顶点引用了 G1 的
    骨骼 id 105（= `b20f90ea` 的槽）⇒ 105 必须写进 **G0** 缓冲。
    实机对照：G0 的 `4e6d8882-1704-0`（骨 id {1,2}，624 顶点）被合进 G2 宿主
    `4e6e989e-11700-0` 的网格（合并 Blend 4562 行，异组 624 行位于 [3066,3689]）；
    旧产物把 {1,2} 只写进 G0 缓冲，而宿主 deform 段绑 `..._G2_s1` ⇒ 624 顶点读
    未初始化矩阵 ⇒ 部件消失。

    开关关闭时（默认）逐字节保持历史行为：不发射任何 `AttachX_` 段，守卫照旧报警。
    """

    SWITCH = "cross_group_merged_vgmap_test"

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, bone_ids):
        """顶点 i 权重挂顶点组 i，组名 = bone_ids[i]（= 该对象引用的全局骨骼 id）。"""
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[types.SimpleNamespace(group=i, weight=1.0)]
            )
            for i in range(len(bone_ids))
        ]
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        for bone_id in bone_ids:
            obj.vertex_groups.append(_FakeVertexGroup(str(bone_id)))
        return obj

    def _make_fixture(self):
        """(exporter, model)：G0 宿主引用 G1 的骨骼 105。"""
        self._register_obj("LOD0.a23aa8a3-42759-0", [0, 100, 105])
        submesh = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        submesh.drawcall_model_list = [dcm(obj_name=submesh.unique_str)]
        model = _FakeDrawIBModel("a23aa8a3", [submesh])
        exporter = _make_exporter([model], merged_vgmap=True)
        components = _with_channel_plan(
            [
                {
                    "draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105,
                    "skeleton_group": 0,
                    "vg_map": {local: local for local in range(105)},
                },
                {
                    "draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51,
                    "skeleton_group": 1,
                    "vg_map": {local: 105 + local for local in range(51)},
                },
            ]
        )
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            component["draw_ib"]: index for index, component in enumerate(components)
        }
        exporter.has_merged_skeleton = True
        return exporter, model

    @staticmethod
    def _switch(enabled: bool):
        return _cross_group_switch(enabled)

    def _render_attach_sections(self, exporter):
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        return "\n".join(_all_builder_lines(builder))

    def _render_vb_section(self, exporter, model):
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        return "\n".join(_all_builder_lines(builder))

    def _render_ib_section(self, exporter, model):
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        return "\n".join(_all_builder_lines(builder))

    def test_disabled_emits_no_cross_attach(self):
        """关掉开关：检测直接返回空 ⇒ 不发射 AttachX_ 段（零行为变化）。"""
        exporter, _model = self._make_fixture()
        with self._switch(False):
            self.assertEqual(exporter._merged_cross_group_component_ids(0), [])
            text = self._render_attach_sections(exporter)
        self.assertNotIn("AttachX_", text)
        self.assertNotIn("CROSS_GROUP_ATTACH", str(exporter._zzmi_merge_diagnostics))

    def test_detection_identifies_foreign_owner_component(self):
        exporter, _model = self._make_fixture()
        with self._switch(True):
            # G0 的几何引用了 G1 的槽 105 ⇒ 跨组件（宿主侧视角）= 组件 1
            self.assertEqual(exporter._merged_cross_group_component_ids(0), [1])
            self.assertEqual(exporter._merged_cross_group_offending_bone_ids(0), {105})
            # G1 自身没有跨组引用（其对象未注册 ⇒ 无引用记录）
            self.assertEqual(exporter._merged_cross_group_component_ids(1), [])

    def test_attach_writes_foreign_palette_into_host_group_buffer(self):
        exporter, _model = self._make_fixture()
        with self._switch(True):
            text = self._render_attach_sections(exporter)
        self.assertIn("[CustomShaderZZMIMergedSkeletonAttachX_C1_g0_s1]", text)
        self.assertIn("[CustomShaderZZMIMergedSkeletonAttachX_C1_g0_s2]", text)
        block = text.split(
            "[CustomShaderZZMIMergedSkeletonAttachX_C1_g0_s1]", 1
        )[1].split("\n[", 1)[0]
        # 拆分绘制路线：attach 一律直拷（源件骨骼留在自己的对象空间，由它自己的 cb1 摆出去）
        self.assertIn("cs = ./res/zzmi_merged_skeleton_attach.hlsl", block)
        self.assertIn("y1 = 51", block)
        # 同组 attach 仍走直拷版 CS（同组=同一对象空间，校准矩阵恒为单位阵）
        same_group_block = text.split("[CustomShaderZZMIMergedSkeletonAttach_C0_s1]", 1)[1].split(
            "\n[", 1
        )[0]
        self.assertIn("cs = ./res/zzmi_merged_skeleton_attach.hlsl", same_group_block)
        self.assertNotIn("attach_calibrated", same_group_block)
        # 调色板/VgMap 取跨组件自己的资源；写入目标 = **宿主组**缓冲
        self.assertIn("cs-t0 = ref ResourceZZPalette_b20f90ea_s1", block)
        self.assertIn("cs-t1 = ref ResourceZZVgMap_b20f90ea", block)
        self.assertIn("cs-u0 = ref ResourceZZMergedSkeleton_G0_s1", block)
        # 拆分绘制路线不再需要校准：attach 段不绑 cb1
        self.assertNotIn("cs-cb1", block)
        self.assertNotIn("cs-cb2", block)
        self.assertIn("Dispatch = 1, 1, 1", block)  # ceil(51 / 64)
        # 诊断：列出写入的骨骼 id 与归属组（不谎称"已修复对象空间"）
        records = [
            record
            for record in exporter._zzmi_merge_diagnostics
            if record.get("code") == "CROSS_GROUP_ATTACH"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["group"], "G0")
        self.assertEqual(records[0]["components"], "1")
        self.assertEqual(records[0]["bone_ids"], "105")
        self.assertEqual(records[0]["owner_groups"], "G1")

    def test_host_deform_section_runs_cross_attach(self):
        exporter, model = self._make_fixture()
        with self._switch(True):
            text = self._render_vb_section(exporter, model)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttachX_C1_g0_s1", text)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttachX_C1_g0_s2", text)
        # 同组 attach 仍在（跨组 attach 是叠加，不替换）
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C0_s1", text)

    # ------------------------------------------------------------------
    # F3（2026-10-10）：跨组吸收宿主的**一帧延迟** —— 蒙皮 CS 重发布
    # ------------------------------------------------------------------

    def _make_fixture_with_geometry(self, host_exported=4562, host_blend_stride=32):
        """宿主网格带真实导出顶点（合并几何挂在它身上）。"""
        exporter, model = self._make_fixture()
        model.submesh_model_list[0].index_vertex_id_dict = list(
            range(host_exported)
        )
        if host_blend_stride != 32:
            model.d3d11GameType.CategoryStrideDict["Blend"] = host_blend_stride
        return exporter, model

    def _make_source_model(self, exported=51):
        source = _FakeDrawIBModel(
            "b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-51-105", 105, 51, 1)]
        )
        source.submesh_model_list[0].index_vertex_id_dict = list(range(exported))
        return source

    def test_host_component_detected_and_publish_plan_synthesized(self):
        """跨组吸收宿主 = 本组里引用了别组骨骼的组件；发布计划按它的导出网格合成。"""
        exporter, _model = self._make_fixture_with_geometry()
        with self._switch(False):
            self.assertEqual(exporter._merged_cross_group_host_component_ids(0), set())
            self.assertIsNone(exporter._merged_cross_group_publish_plan(0))
        with self._switch(True):
            self.assertEqual(exporter._merged_cross_group_host_component_ids(0), {0})
            plan = exporter._merged_cross_group_publish_plan(0)
        self.assertIsNotNone(plan)
        self.assertTrue(plan["cross_group"])
        self.assertEqual(plan["target_ib"], "a23aa8a3")
        self.assertEqual(
            plan["deform_draws"],
            [("Resourcea23aa8a3Position", "Resourcea23aa8a3Blend", 4562)],
        )
        self.assertEqual(plan["so_stride"], 40)
        self.assertEqual(plan["so_prefix_rows"], 0)
        # 守卫要等**全部**供骨件（宿主 0 + 跨组供骨件 1）
        self.assertEqual(plan["required_component_ids"], [0, 1])
        # 行布局与蒙皮 CS 写死的一致 ⇒ CS 可用
        self.assertTrue(exporter._merged_skin_publish_supported(plan))

    def test_host_deform_section_emits_cs_publish_gated_on_all_providers(self):
        """宿主自己那段也要发 CS 重发布，且守卫含跨组供骨件的到达项。"""
        exporter, model = self._make_fixture_with_geometry()
        with self._switch(True):
            text = self._render_vb_section(exporter, model)
        self.assertIn("run = CustomShaderZZMISkin_G0_s1", text)
        # 捕获本轮自己的 SO 引用（CS 的落点）
        self.assertIn("ResourceZZRedirectSO_G0_s1 = ref so0", text)
        guard_line = [
            line
            for line in text.splitlines()
            if line.startswith("if ") and "zz_ms_seen_01" in line
        ]
        self.assertTrue(guard_line)
        # 宿主(0) 与跨组供骨件(1) 都必须当帧到达，守卫才会闭合
        self.assertTrue(any("zz_ms_seen_11" in line for line in guard_line))
        # 直连路径的兜底 draw 仍在（CS 绑定失败时行为与旧版一致）
        self.assertIn("draw = 4562, 0", text)

    def test_cross_source_section_publishes_host_geometry(self):
        """跨组供骨件自己那段也要发一份（守卫相同 ⇒ 最后一个到达者落笔）。"""
        exporter, _host = self._make_fixture_with_geometry()
        source = self._make_source_model()
        exporter.drawib_model_list.append(source)
        with self._switch(True):
            text = self._render_vb_section(exporter, source)
        self.assertIn("run = CustomShaderZZMISkin_G0_s1", text)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttachX_C1_g0_s1", text)
        # 守卫含宿主(0)与供骨件(1) 两侧 ⇒ 只在最后一个到达者处闭合
        guard_line = [
            line
            for line in text.splitlines()
            if line.startswith("if ") and "zz_ms_seen_11" in line
        ]
        self.assertTrue(guard_line)
        self.assertTrue(any("zz_ms_seen_01" in line for line in guard_line))

    def test_palette_capture_stays_before_attach(self):
        """palette 捕获必须落在 attach **之前**（attach 读到当帧 palette）。

        ⚠ 2026-10-10 教训：曾把捕获后移到 attach 之后（F5「跨组同帧化」），想让
        attach 读到的 palette 与它读到的（必然是上一帧的）cb1 同源。但合并骨架缓冲
        是**宿主与跨组供骨件共用**的 ⇒ 整块骨架（含宿主自己的骨骼）一起慢一帧 ⇒
        宿主自身几何也变陈旧 ⇒ 用户实测「无论哪边都在抖、骨骼矩阵不对」。故回退：
        开关开/关都必须保持「捕获 → attach → 换绑骨架」这一历史次序。
        """
        for enabled in (False, True):
            exporter, model = self._make_fixture_with_geometry()
            with self._switch(enabled):
                text = self._render_vb_section(exporter, model)
            capture = text.index(
                "ResourceZZPalette_a23aa8a3_s1 = copy vs-t0 unless_null"
            )
            attach = text.index("run = CustomShaderZZMIMergedSkeletonAttach")
            rebind = text.index("vs-t0 = ResourceZZMergedSkeleton_G0_s1")
            self.assertLess(
                capture, attach, f"捕获必须在 attach 之前（开关={enabled}）"
            )
            self.assertLess(rebind, len(text))

    def test_split_draws_diagnose_when_merged_buffers_unavailable(self):
        """合并 Blend/Index 读不到时：退回单次绘制并落一条诊断，不抛异常。"""
        exporter, model = self._make_fixture()
        with self._switch(True):
            text = self._render_ib_section(exporter, model)
        self.assertNotIn("vs-cb1 = ResourceZZCb1_G", text)

    def test_source_index_runs_split_by_bone_range(self):
        """真实读盘路径：按骨索引值域把合并网格切成按来源的连续索引段。"""
        import struct

        exporter, _model = self._make_fixture_with_geometry()
        components = exporter.merged_skeleton_components
        first = components[0]
        last = components[-1]
        bone_a = int(next(iter(first["vg_map"].values())))
        bone_b = int(next(iter(last["vg_map"].values())))
        self.assertNotEqual(
            int(first.get("skeleton_group", 0)),
            int(last.get("skeleton_group", 0)),
        )
        draw_ib = str(first["draw_ib"])
        meshes = Path(_FAKE_MOD_FOLDER) / "Meshes"
        meshes.mkdir(parents=True, exist_ok=True)
        blend = bytearray()
        for bone in (bone_a, bone_a, bone_a, bone_b, bone_b, bone_b):
            blend += struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
            blend += struct.pack("<4I", bone, 0, 0, 0)
        (meshes / (draw_ib + "-Blend.buf")).write_bytes(bytes(blend))
        (meshes / ("LOD0." + draw_ib + "-6-0-Index.buf")).write_bytes(
            struct.pack("<6I", 0, 1, 2, 3, 4, 5)
        )
        runs = exporter._merged_source_index_runs(draw_ib)
        self.assertEqual([(start, count) for _g, start, count in runs], [(0, 3), (3, 3)])
        self.assertNotEqual(runs[0][0], runs[1][0])

    def test_split_draws_absent_when_disabled(self):
        """开关关闭：不发拆分绘制注释、不发诊断。"""
        exporter, model = self._make_fixture()
        with self._switch(False):
            text = self._render_ib_section(exporter, model)
        self.assertNotIn("vs-cb1 = ResourceZZCb1_G", text)
        self.assertNotIn("CROSS_GROUP_ATTACH", text)

    def _write_split_buffers(self, host_ib, host_bone, foreign_bone):
        """合成一份合并 Blend/Index：前 3 个索引属宿主组，后 3 个属跨组。"""
        import struct

        meshes = Path(_FAKE_MOD_FOLDER) / "Meshes"
        meshes.mkdir(parents=True, exist_ok=True)
        blend = bytearray()
        for bone in (
            host_bone,
            host_bone,
            host_bone,
            foreign_bone,
            foreign_bone,
            foreign_bone,
        ):
            blend += struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
            blend += struct.pack("<4I", bone, 0, 0, 0)
        (meshes / (host_ib + "-Blend.buf")).write_bytes(bytes(blend))
        (meshes / ("LOD0." + host_ib + "-6-0-Index.buf")).write_bytes(
            struct.pack("<6I", 0, 1, 2, 3, 4, 5)
        )

    def test_split_draws_go_back_to_their_own_render_sections(self):
        """按组拆出的段必须发回**各自来源**的渲染段：那里的 vs-cb1 天然是当帧的。

        旧做法把全部段塞进宿主段 ⇒ 跨组 cb1 在宿主段执行时可能还是上一帧的值 ⇒
        影子/镜像物体（用户实测）。这条测试钉死「各回各家」+ 非宿主段自己绑
        合并 IB / Texcoord / 宿主 SO + mesh 标注统一写合并物体的名字。
        """
        exporter, host_model = self._make_fixture_with_geometry()
        source_model = self._make_source_model()
        host_component = exporter.merged_skeleton_components[0]
        source_component = exporter.merged_skeleton_components[-1]
        host_bone = int(next(iter(host_component["vg_map"].values())))
        foreign_bone = int(next(iter(source_component["vg_map"].values())))
        self._write_split_buffers("a23aa8a3", host_bone, foreign_bone)
        with self._switch(True):
            host_text = self._render_ib_section(exporter, host_model)
            source_text = self._render_ib_section(exporter, source_model)

        # 宿主段：只画宿主自己那一段，且不做 ib/vb 重绑（本来就是它自己的）
        self.assertIn("vs-cb1 = ResourceZZCb1_G0", host_text)
        self.assertIn("drawindexed = 3, 0, 0", host_text)
        self.assertNotIn("drawindexed = 3, 3, 0", host_text)
        self.assertNotIn("ib = Resource_LOD0.a23aa8a3_6_0_Index", host_text)

        # 跨组段：搬回它自己的渲染段，自己绑合并 IB / 合并 Texcoord / 自建蒙皮输出
        self.assertIn("ib = Resource_LOD0.a23aa8a3_6_0_Index", source_text)
        self.assertIn("vb1 = Resourcea23aa8a3Texcoord", source_text)
        self.assertIn("vb0 = ResourceZZSkinOut_G0_s1", source_text)
        self.assertIn("vb0 = ResourceZZSkinOut_G0_s2", source_text)
        self.assertNotIn("vb0 = ResourceZZRedirectSO", source_text)
        self.assertIn("vs-cb1 = ResourceZZCb1_G1", source_text)
        self.assertIn("drawindexed = 3, 3, 0", source_text)
        self.assertNotIn("drawindexed = 3, 0, 0", source_text)

        # mesh 标注：所有来源段统一写**合并物体**（宿主）的名字，拖拽交互才认得出
        host_comments = [
            line
            for line in host_text.splitlines()
            if line.strip().startswith("; [mesh:")
        ]
        self.assertTrue(host_comments)
        self.assertIn(host_comments[0], source_text)


    def test_cs_section_definitions_emitted_for_cross_group_host(self):
        """CS 段定义要指向宿主的 VB / 骨架 / **自建**蒙皮输出。

        跨组路径的 UAV 写目标必须是自建缓冲（`ResourceZZSkinOut_*`）而不是
        `ResourceZZRedirectSO_*`（= `ref so0` 别名）：别名要被拆分绘制当 `vb0`
        读 ⇒ 目标带 `unordered_access vertex_buffer`，而游戏 SO 只有
        `vertex_buffer shader_resource stream_output` ⇒ 视图创建失败 ⇒
        展示页整屏卡死（日志铁证见 `_merged_skin_out_name`）。
        """
        exporter, _model = self._make_fixture_with_geometry()
        with self._switch(True):
            text = self._render_attach_sections(exporter)
        self.assertIn("[CustomShaderZZMISkin_G0_s1]", text)
        block = text.split("[CustomShaderZZMISkin_G0_s1]", 1)[1].split("\n[", 1)[0]
        self.assertIn("cs = ./res/zzmi_merged_skin.hlsl", block)
        self.assertIn("x1 = 4562", block)
        self.assertIn("cs-t0 = ref Resourcea23aa8a3Position", block)
        self.assertIn("cs-t1 = ref Resourcea23aa8a3Blend", block)
        self.assertIn("cs-t2 = ref ResourceZZMergedSkeleton_G0_s1", block)
        self.assertIn("cs-u0 = ref ResourceZZSkinOut_G0_s1", block)
        self.assertNotIn("cs-u0 = ref ResourceZZRedirectSO", block)
        # 自建输出必须有声明：stride = SO 行宽、array = 合并几何行数、
        # bind_flags 同时允许 UAV 写与 VB 读
        self.assertIn("[ResourceZZSkinOut_G0_s1]", text)
        out_block = text.split("[ResourceZZSkinOut_G0_s1]", 1)[1].split("\n[", 1)[0]
        self.assertIn("type = Buffer", out_block)
        self.assertIn("stride = 40", out_block)
        self.assertIn("array = 4562", out_block)
        self.assertIn("bind_flags = unordered_access vertex_buffer", out_block)

    def test_disabled_emits_no_cross_publish(self):
        """关掉开关：零行为变化（无跨组发布段、无 SO 捕获、无诊断）。"""
        exporter, model = self._make_fixture_with_geometry()
        with self._switch(False):
            text = self._render_attach_sections(exporter) + self._render_vb_section(
                exporter, model
            )
        self.assertNotIn("CustomShaderZZMISkin_G0", text)
        self.assertNotIn("ResourceZZRedirectSO_G0_s1 = ref so0", text)
        self.assertNotIn("CROSS_GROUP_PUBLISH", str(exporter._zzmi_merge_diagnostics))

    def test_narrow_host_layout_skips_cs_and_diagnoses(self):
        """宿主 Blend 行布局不在锚点内 ⇒ 不发 CS（宁可退回宿主自己的 draw）。"""
        exporter, model = self._make_fixture_with_geometry(host_blend_stride=16)
        with self._switch(True):
            text = self._render_vb_section(exporter, model)
            attach_text = self._render_attach_sections(exporter)
        self.assertNotIn("run = CustomShaderZZMISkin_G0", text)
        self.assertNotIn("[CustomShaderZZMISkin_G0_s1]", attach_text)
        codes = {
            record.get("code") for record in exporter._zzmi_merge_diagnostics
        }
        self.assertIn("SKIN_LAYOUT_UNSUPPORTED", codes)

    def test_guard_reports_handled_instead_of_forbidden_when_enabled(self):
        exporter, _model = self._make_fixture()
        with self._switch(True):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exporter._warn_cross_group_bone_references()
        out = buf.getvalue()
        self.assertIn("跨组骨骼合并已接入", out)
        self.assertNotIn("禁止跨组别骨骼合并", out)

    def test_guard_keeps_forbidden_warning_when_disabled(self):
        exporter, _model = self._make_fixture()
        buf = io.StringIO()
        with self._switch(False), contextlib.redirect_stdout(buf):
            exporter._warn_cross_group_bone_references()
        out = buf.getvalue()
        self.assertIn("禁止跨组别骨骼合并", out)

    # ------------------------------------------------------------------
    # F2（2026-10-10）：跨组校准 —— 对象→世界 cb1 捕获 + 校准版 CS
    # ------------------------------------------------------------------

    def test_cb1_capture_resources_declared_only_when_enabled(self):
        """每骨架组一份 `ResourceZZCb1_G<N>` 捕获资源；关闭时不发射（零行为变化）。"""
        exporter, _model = self._make_fixture()
        with self._switch(False):
            self.assertNotIn("ResourceZZCb1_G", self._render_attach_sections(exporter))
        with self._switch(True):
            text = self._render_attach_sections(exporter)
        self.assertIn("[ResourceZZCb1_G0]", text)
        self.assertIn("[ResourceZZCb1_G1]", text)
        # 捕获资源不声明 type/stride：`copy vs-cb1` 由源视图决定尺寸与布局
        block = text.split("[ResourceZZCb1_G0]", 1)[1].split("\n[", 1)[0]
        self.assertNotIn("type =", block)
        self.assertNotIn("stride =", block)

    def test_render_section_captures_group_cb1_when_enabled(self):
        """渲染段（本部件自己的 draw）捕获本骨架组的对象→世界 cb1。

        为什么必须是渲染段：只有这里能确定「此刻 vs-cb1 就是这个物体的对象→世界
        矩阵」。历史失败版按 IB hash 匹配（`[TextureOverrideIB_*]`），同一个 IB 被
        多个 pass 匹配 ⇒ 抓到别的 pass 的常量块（实测同帧两 pass 差 8.2°）。
        """
        exporter, model = self._make_fixture()
        with self._switch(False):
            self.assertNotIn(
                "copy vs-cb1", self._render_ib_section(exporter, model)
            )
        with self._switch(True):
            text = self._render_ib_section(exporter, model)
        self.assertIn("ResourceZZCb1_G0 = copy vs-cb1 unless_null", text)
        # 单条：同一 DrawIB 的多条子网格共用一个渲染段对象，不重复追加
        self.assertEqual(text.count("copy vs-cb1 unless_null"), 1)

    def test_attach_and_skin_shaders_are_copied(self):
        """合并骨架的 attach CS 与蒙皮 CS 无条件复制进 mod 的 res/。"""
        exporter = _make_exporter([], merged_vgmap=True)
        res_dir = Path(_FAKE_MOD_FOLDER) / "res"
        with self._switch(False):
            exporter._copy_merged_skeleton_shader_to_mod()
        self.assertTrue((res_dir / "zzmi_merged_skeleton_attach.hlsl").is_file())
        self.assertTrue((res_dir / "zzmi_merged_skin.hlsl").is_file())
        with self._switch(True):
            exporter._copy_merged_skeleton_shader_to_mod()
        self.assertTrue((res_dir / "zzmi_merged_skeleton_attach.hlsl").is_file())
        self.assertTrue((res_dir / "zzmi_merged_skin.hlsl").is_file())


class ZZSIMissingPartsGuardTests(unittest.TestCase):
    def test_warns_when_part_missing(self):
        # 工作空间有两个部件（first_index 0 / 22296），导出只找到第一个的对象
        model = _FakeDrawIBModel(
            "84618ee0",
            [_FakeSubmesh("LOD0.84618ee0-22296-0", 105, 49)],
            part_map={0: "1", 22296: "2"},
        )
        # _FakeSubmesh.match_first_index 默认 0，即只有部件 "1" 有对象
        exporter = _make_exporter([model], merged_vgmap=True)
        report = exporter._warn_missing_drawib_parts()
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["draw_ib"], "84618ee0")
        self.assertEqual(report[0]["missing"], [(22296, "2")])
        self.assertEqual(report[0]["present_count"], 1)
        self.assertEqual(report[0]["expected_count"], 2)

    def test_no_warning_when_complete(self):
        model = _FakeDrawIBModel(
            "84618ee0",
            [_FakeSubmesh("LOD0.84618ee0-22296-0", 105, 49)],
            part_map={0: "1"},
        )
        exporter = _make_exporter([model], merged_vgmap=True)
        report = exporter._warn_missing_drawib_parts()
        self.assertEqual(report, [])


class ZZSIMultiInstanceGuardTests(unittest.TestCase):
    """多实例（同一 IB 在场景中被画多次）就绪守卫回归（v9 出现次槽位）。

    v9（用户游戏内实测通过的手改版 `浮波柚叶.ini` 为语义基准）：同一 IB 的 deform
    pass 每帧会跑多次（每个实例一次）。若只保留**一份** palette / 骨架 / SO，两个
    实例会交错覆盖同一份资源，守卫成立时消费到的可能是**半帧拼接**的骨架 →
    运动时抖动。v9 把资源扩成 2 个槽位：每个部件按自己的出现次把当帧 palette /
    SO 写进 s1 或 s2，attach 也只写该槽；每槽守卫要求「本组全部部件在该槽都已
    当帧到达」。旧实现的帧闩锁 `$zz_ms_group_ready_g<N>` /
    `$zz_ms_redirect_drawn_<IB>` 与相位计数 `$zz_ms_group_phase_g<N>` 均已废除。
    """

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, weighted_groups):
        """注册假对象：weighted_groups = [(vertex_group_index, weight), ...] 展平到顶点 0。

        组名 = 数字骨骼 id（导出约定）。顶点组列表按最大索引补足（padding 组用
        非数字名，_collect_drawib_referenced_bone_ids 会跳过）。
        """
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[
                    types.SimpleNamespace(group=gid, weight=weight)
                    for gid, weight in weighted_groups
                ]
            )
        ]
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        max_index = max(gid for gid, _weight in weighted_groups)
        named = {gid: str(gid) for gid, _weight in weighted_groups}
        for index in range(max_index + 1):
            obj.vertex_groups.append(
                _FakeVertexGroup(named.get(index, f"pad{index}"))
            )
        return obj

    def _make_exporter(self, components):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        sub_a = _FakeSubmesh(
            "LOD0.a23aa8a3-42759-0", 0, 105, 0,
            deform_draw=20, original_vertex_count=12314,
            exported_vertex_count=12314, match_first_index=0,
        )
        sub_a.drawcall_model_list = [dcm(
            obj_name="LOD0.a23aa8a3-42759-0",
            source_obj_name="LOD0.a23aa8a3-42759-0",
        )]
        sub_b = _FakeSubmesh(
            "LOD0.b20f90ea-19182-0", 105, 51, 0,
            deform_draw=2, original_vertex_count=4643,
            exported_vertex_count=4643, match_first_index=19182,
        )
        sub_b.drawcall_model_list = [dcm(
            obj_name="LOD0.b20f90ea-19182-0",
            source_obj_name="LOD0.b20f90ea-19182-0",
        )]
        models = [
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
            _FakeDrawIBModel("b20f90ea", [sub_b]),
        ]
        exporter = _make_exporter(models, merged_vgmap=True)
        components = _with_channel_plan(components)
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(components)
        }
        # 假对象必须在构建重定向计划**之前**注册：计划要读顶点权重收集
        # 各部件实际引用的骨骼 id（_collect_drawib_referenced_bone_ids）。
        # a23aa8a3（target，deform 20）自属骨骼 0..104；b20f90ea（carrier，deform 2）
        # 的顶点引用 105（= a23aa8a3 的末槽）-> 被吸收，必须重定向。
        self._register_obj("LOD0.a23aa8a3-42759-0", [(0, 1.0)])
        self._register_obj("LOD0.b20f90ea-19182-0", [(105, 1.0)])
        (
            exporter._redirect_carrier_map,
            exporter._redirect_target_map,
            _unredirected,  # A-opt1：实例字段已删，仅占位对齐解包
        ) = exporter._build_merged_mesh_redirect_plan()
        return exporter, models

    def _components(self):
        return [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105,
             "skeleton_group": 0, "deform_draw": 20, "original_vertex_count": 12314,
             "vg_map": {i: i for i in range(105)}},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51,
             "skeleton_group": 0, "deform_draw": 2, "original_vertex_count": 4643,
             "vg_map": {i: 105 + i for i in range(51)}},
        ]

    def test_occurrence_slot_counter_is_incremented_per_attach(self):
        """同一 IB 每个实例的 deform pass 都必须让本部件出现次 +1。"""
        exporter, models = self._make_exporter(self._components())
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, models[1])
        text = "\n".join(_all_builder_lines(builder))

        # 出现次顶层自增 + 回绕为槽位 1（1/2 循环）
        self.assertIn("$zz_ms_occ_1 = $zz_ms_occ_1 + 1", text)
        self.assertIn("if $zz_ms_occ_1 >= 3", text)
        self.assertIn("    $zz_ms_occ_1 = 1", text)
        # 到达标记顶层 sticky 累加（绝不在 if 体内赋值）
        self.assertIn("$zz_ms_seen_11 = $zz_ms_seen_11 + ($zz_ms_occ_1 == 1)", text)
        self.assertIn("$zz_ms_seen_12 = $zz_ms_seen_12 + ($zz_ms_occ_1 == 2)", text)
        # 按出现次把当帧 palette 复制进对应槽
        self.assertIn("ResourceZZPalette_b20f90ea_s1 = copy vs-t0 unless_null", text)
        self.assertIn("ResourceZZPalette_b20f90ea_s2 = copy vs-t0 unless_null", text)
        # 顶层无条件 run 全部 (部件, 槽)
        for cid in (0, 1):
            for slot in (1, 2):
                self.assertIn(
                    f"run = CustomShaderZZMIMergedSkeletonAttach_C{cid}_s{slot}", text
                )

    def test_direct_path_draw_gated_by_own_occurrence_only(self):
        """v9.1：直连路径按**本部件**出现次绑槽绘制，绝不等组内其它部件。

        回归背景（2026-09-13 FrameAnalysis 实证）：组级「本组全部部件当帧到达」
        门控只可能在组内**最后一个**到达的部件那段成立，而每个部件的几何只有它
        自己的 deform 段能画（该段已被 `handling = skip` 吃掉原 draw）⇒ 先到的
        部件整帧没有变形输出，渲染读到旧内容/零值 → 模型随引擎提交顺序逐帧闪/
        消失（三部件 deform 顺序实测为 B→C→A 与 A→B→C 两种，后者最后到的是 3
        顶点占位桩 → 整帧无可见几何 = 用户看到的"模型消失"帧）。
        自足挂点（几何只采样自己 vg_map 覆盖的槽位）自己的槽位已由本段 attach
        用当帧 palette 写全，等其它部件没有任何正确性收益。
        """
        exporter, models = self._make_exporter(self._components())
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, models[1])
        lines = _all_builder_lines(builder)
        text = "\n".join(lines)

        # 本部件（b20f90ea = 组件 1）每槽一条绘制，条件只读自己的出现次
        for slot, skeleton in ((1, "ResourceZZMergedSkeleton_G0_s1"),
                               (2, "ResourceZZMergedSkeleton_G0_s2")):
            self.assertIn(
                f"if $zz_ms_occ_1 == {slot}\n"
                f"    vs-t0 = {skeleton}\n"
                "    draw = 4643, 0\n"
                "endif",
                text,
            )
        # 任何 if 条件都不得再挂组级 seen（那正是"先到的部件整帧不画"的成因）
        for line in lines:
            if line.startswith("if "):
                self.assertNotIn("$zz_ms_seen_", line, line)
        self.assertNotIn("$zz_ms_group_phase", text)
        self.assertNotIn("$zz_ms_redirect_drawn", text)
        self.assertNotIn("$zz_ms_group_ready", text)
        # 槽 1 绘制在槽 2 绘制之前，且各自换绑自己的槽骨架
        self.assertLess(
            text.index("if $zz_ms_occ_1 == 1"),
            text.index("if $zz_ms_occ_1 == 2"),
        )

    def test_guard_body_contains_only_bindings_and_draw(self):
        """v9 硬约束：绘制体内只允许绑定与 draw；不得出现 run、不得给 $变量赋值。

        直连路径自足挂点的绘制体内只有 `vs-t0` 与 `draw`（没有 SO 重定向）。
        """
        exporter, models = self._make_exporter(self._components())
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, models[0])
        lines = _all_builder_lines(builder)

        # 出现次 palette 捕获与自足绘制各有一条 `if $zz_ms_occ_0 == 1`
        guard_starts = [
            index for index, line in enumerate(lines)
            if line.startswith("if $zz_ms_occ_0 == 1")
        ]
        self.assertEqual(len(guard_starts), 2, "palette 捕获 + 自足绘制各一条")
        start = guard_starts[-1]
        end = lines.index("endif", start)
        body = lines[start + 1 : end]
        self.assertTrue(body, "守卫体内必须有绑定与 draw")
        for line in body:
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("run "), f"守卫体内不得有 run: {line!r}"
            )
            self.assertFalse(
                stripped.startswith("$"), f"守卫体内不得给 $变量赋值: {line!r}"
            )
            self.assertTrue(
                stripped.startswith(
                    ("vs-t0 =", "so0 =", "vb0 =", "vb2 =", "draw =")
                ),
                f"守卫体内只允许绑定与 draw: {line!r}",
            )
        self.assertIn("    vs-t0 = ResourceZZMergedSkeleton_G0_s1", body)
        self.assertIn("    draw = 12314, 0", body)

    def test_redirect_guard_body_contains_so_binding_and_draw(self):
        """重定向路径的守卫体：SO 绑定 + carrier 的 vb0/vb2 + draw + so0 = null。"""
        exporter, models = self._make_exporter(self._components())
        # 手工注入一份重定向计划（占位 target 由 carrier 承载重放）
        exporter._redirect_carrier_map = {
            "b20f90ea": {
                "target": "a23aa8a3",
                "base_vertex": 3,
                "target_first_index": 0,
                "vertex_count": 4643,
            }
        }
        exporter._redirect_target_map = {
            "a23aa8a3": {
                "target_ib": "a23aa8a3",
                "target_component_id": 0,
                "deform_draws": [("Resourceb20f90eaPosition", "Resourceb20f90eaBlend", 4643)],
                "so_vertex_count": 4646,
                "target_own_vertices": 3,
                "so_prefix_rows": 3,
                "target_has_real_geometry": False,
                "so_owner_ib": "b20f90ea",
                "required_component_ids": [0, 1],
                "compatible_component_ids": [0, 1],
                "target_viable": True,
                "so_stride": 40,
            }
        }
        text = self._vb_text(exporter, models[0])
        lines = text.splitlines()
        start = next(
            index for index, line in enumerate(lines)
            if "($zz_ms_prev_01 == 0" in line
        )
        end = lines.index("endif", start)
        body = [line.strip() for line in lines[start + 1 : end]]
        self.assertEqual(
            body,
            [
                "vs-t0 = ResourceZZMergedSkeleton_G0_s1",
                "so0 = ref ResourceZZRedirectSO_G0_s1",
                "vb2 = Resourceb20f90eaBlend",
                "vb0 = Resourceb20f90eaPosition",
                "draw = 4643, 0",
                "so0 = null",
            ],
        )

    def test_guard_uses_previous_frame_slot_prediction(self):
        """重放时机回归（2026-09-17 FrameAnalysis-022132 实证）。

        守卫的豁免项必须是**按槽的上一帧预测**（`$zz_ms_prev_<i><k>`），不能用
        「本帧至今是否出现过」（旧 `$zz_ms_any_<i>`）：后者在帧首对**所有尚未
        deform** 的部件恒真 → 守卫在载体自己的 deform pass 就落笔，排在载体
        之后的部件用**上一帧** palette。dump 比对（叶瞬光01 单实例帧，载体
        999bff94 是第 2 个 deform pass、最后到达是 38b3bd13 第 6 个）：
        载体 SO 行 3..13673 与「载体 pass 时刻的 vs-t0」重算蒙皮 13671/13671
        吻合（最大误差 2.2e-07），与「最后一个必需部件 pass 时刻的 vs-t0」
        最大误差 0.0226（G2 同样 1.9e-07 vs 0.0857）→ 用户实测「身体莫名其妙的
        一卡一卡」（慢的部件集合随提交顺序逐帧变化）。

        另一条同源回归：`any == 0` 对「整帧只出现一次」的部件永远不成立，
        却要求它 `seen_<i><2> == 1`（它没有第 2 次）→ 第 2 槽守卫永不闭合 →
        该实例的 SO 永不写（用户实测「只有那个实例化的物体有问题」）。
        按槽预测下这种部件在 s2 的预测值为 0 ⇒ 豁免 ⇒ 第 2 槽守卫正常闭合。
        """
        exporter, models = self._make_exporter(self._components())
        # 与 test_redirect_guard_body_contains_so_binding_and_draw 同一份重定向计划：
        # b20f90ea = carrier（SO owner），a23aa8a3 = 纯占位 target 挂点。
        exporter.merged_skeleton_component_id_dict = {
            m.draw_ib: i for i, m in enumerate(models)
        }
        exporter._redirect_carrier_map = {
            "b20f90ea": {
                "target": "a23aa8a3",
                "base_vertex": 3,
                "target_first_index": 0,
                "vertex_count": 4643,
            }
        }
        exporter._redirect_target_map = {
            "a23aa8a3": {
                "target_ib": "a23aa8a3",
                "target_component_id": 0,
                "deform_draws": [
                    ("Resourceb20f90eaPosition", "Resourceb20f90eaBlend", 4643)
                ],
                "so_vertex_count": 4646,
                "target_own_vertices": 3,
                "so_prefix_rows": 3,
                "target_has_real_geometry": False,
                "so_owner_ib": "b20f90ea",
                "required_component_ids": [0, 1],
                "compatible_component_ids": [0, 1],
                "target_viable": True,
                "so_stride": 40,
            }
        }
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, models[0])
        builder_carrier = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_carrier, models[1])
        text = "\n".join(_all_builder_lines(builder))
        text_carrier = "\n".join(_all_builder_lines(builder_carrier))

        self.assertNotIn("$zz_ms_any_", text + text_carrier)
        # s1 与 s2 用各自的上一帧预测值（不是同一个「本帧出现过」标记）
        for chunk in (text, text_carrier):
            self.assertIn("$zz_ms_prev_01 == 0 || $zz_ms_seen_01 >= 1", chunk)
            self.assertIn("$zz_ms_prev_02 == 0 || $zz_ms_seen_02 >= 1", chunk)
            # t149：捕获者 comp 1 自己的 cull_aware 子句被**布尔吸收**删除——
            # SO 别名就绪门 `($zz_ms_seen_11 >= 1)` 已经断言了它（`A && (P||A) ≡ A`），
            # 该子句恒真。非捕获者部件（0/2）的「上一帧本槽没到」豁免原样保留。
            self.assertIn("($zz_ms_seen_11 >= 1)", chunk)
            self.assertNotIn("$zz_ms_prev_11 == 0 || $zz_ms_seen_11 >= 1", chunk)
            self.assertNotIn("$zz_ms_prev_12 == 0 || $zz_ms_seen_12 >= 1", chunk)

    def test_all_attach_runs_are_top_level(self):
        """v9 硬约束：所有 attach run 必须在 deform 段**顶层**（if 内 run 不执行）。

        if 内的 run 在本 3DMigoto fork 上不执行 → 骨架为空 → 模型整体消失。
        """
        exporter, models = self._make_exporter(self._components())
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, models[1])
        lines = _all_builder_lines(builder)

        guard_depth = 0
        run_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("if "):
                guard_depth += 1
                continue
            if stripped == "endif":
                guard_depth -= 1
                continue
            if stripped.startswith("run = CustomShaderZZMIMergedSkeletonAttach_"):
                run_lines.append((guard_depth, line))
        self.assertTrue(run_lines)
        for depth, line in run_lines:
            self.assertEqual(depth, 0, f"attach run 进了 if 体内: {line!r}")
        # 每部件每槽各一条 ⇒ 2 部件 × 2 槽
        self.assertEqual(len(run_lines), 4)

    def test_slot_locals_are_shared_across_group_deform_sections(self):
        """同一 IB 被画多次（10>2 的多实例）：组内部件的 run 序列逐字相同。

        槽资源与 attach run 序列都是**组级**的：每个部件的 deform 段都发出同一套
        (组内全部部件 × 全部槽) attach，因此同一帧内无论实例提交顺序如何，每个
        部件都会按自己的出现次把当帧 palette 落到对应槽；绘制则各画各的几何
        （v9.1：按本部件出现次绑本槽，不等其它部件）。
        """
        exporter, models = self._make_exporter(self._components())
        text_a = self._vb_text(exporter, models[0])
        text_b = self._vb_text(exporter, models[1])

        run_a = [line.strip() for line in text_a.splitlines() if line.strip().startswith("run = CustomShaderZZMIMergedSkeletonAttach_")]
        run_b = [line.strip() for line in text_b.splitlines() if line.strip().startswith("run = CustomShaderZZMIMergedSkeletonAttach_")]
        self.assertEqual(run_a, run_b)
        self.assertEqual(
            run_a,
            [
                "run = CustomShaderZZMIMergedSkeletonAttach_C0_s1",
                "run = CustomShaderZZMIMergedSkeletonAttach_C1_s1",
                "run = CustomShaderZZMIMergedSkeletonAttach_C0_s2",
                "run = CustomShaderZZMIMergedSkeletonAttach_C1_s2",
            ],
        )
        # 绘制门控按**本部件自己**的出现次（v9.1）：每个部件只画自己的几何，
        # 不等组内其它部件——组级 seen 门控只会在最后到达的部件那段成立。
        self.assertIn(
            "if $zz_ms_occ_0 == 1\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n"
            "    draw = 12314, 0\n"
            "endif",
            text_a,
        )
        self.assertIn(
            "if $zz_ms_occ_1 == 1\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n"
            "    draw = 4643, 0\n"
            "endif",
            text_b,
        )
        for text in (text_a, text_b):
            for line in text.splitlines():
                if line.startswith("if "):
                    self.assertNotIn("$zz_ms_seen_", line, line)

    def _vb_text(self, exporter, model):
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        return "\n".join(_all_builder_lines(builder))

    def test_generated_skeleton_sections_have_no_phase_or_latch(self):
        """合并骨架段：只声明 occ/seen；[Present] 只清零 occ/seen；无相位/闩锁。"""
        sub_a = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        sub_b = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [sub_a]),
                _FakeDrawIBModel("b20f90ea", [sub_b]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))

        constants_text, present_text = text.split("[Present]")[0], text.split("[Present]")[1]
        for slot in (1, 2):
            for cid in (0, 1):
                self.assertIn(f"global $zz_ms_occ_{cid} = 0", constants_text)
                self.assertIn(
                    f"global $zz_ms_seen_{cid}{slot} = 0", constants_text
                )
                self.assertIn(f"$zz_ms_occ_{cid} = 0", present_text)
                self.assertIn(f"$zz_ms_seen_{cid}{slot} = 0", present_text)
        # 已废除的相位/闩锁变量一律不得出现
        for forbidden in (
            "$zz_ms_group_phase",
            "$zz_ms_group_ready",
            "$zz_ms_redirect_drawn",
        ):
            self.assertNotIn(forbidden, text)
        # [Present] 只清零变量，不写任何资源复位
        self.assertNotIn("ResourceZZRedirectSO", present_text)
        self.assertNotIn("ResourceZZPalette", present_text)
        self.assertNotIn("ResourceZZMergedSkeleton", present_text)
        self.assertNotIn("= null", present_text)

    def test_same_ib_same_vb_twice_uses_two_slots(self):
        """同一 IB、VB/Blend 也相同的两个实例：共用一条 VB 段与一套槽资源。

        段内每个槽每个部件各一条 attach（实例数不体现在 INI 段里）；出现次
        决定本段这次经过写哪个槽，两个实例分别落到 s1/s2，互不覆盖。
        """
        sub_a = _FakeSubmesh("LOD0.5144c409-17364-0", 41, 106, 2)
        sub_b = _FakeSubmesh("LOD0.73757570-26007-0", 147, 102, 2)
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("5144c409", [sub_a]),
                _FakeDrawIBModel("73757570", [sub_b]),
            ],
            merged_vgmap=True,
        )
        # 组件字典需带 vg_map（add_merged_skeleton_sections 要写 vg_map 二进制）
        exporter.merged_skeleton_components = _with_channel_plan([
            {
                "draw_ib": "5144c409", "unique_str": "LOD0.5144c409-17364-0",
                "vg_offset": 41, "vg_count": 106, "skeleton_group": 2,
                "vg_map": {i: 41 + i for i in range(106)}, "deform_draw": 0,
                "original_vertex_count": 0,
            },
            {
                "draw_ib": "73757570", "unique_str": "LOD0.73757570-26007-0",
                "vg_offset": 147, "vg_count": 102, "skeleton_group": 2,
                "vg_map": {i: 147 + i for i in range(102)}, "deform_draw": 0,
                "original_vertex_count": 0,
            },
        ])
        exporter.merged_skeleton_component_id_dict = {"5144c409": 0, "73757570": 1}
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(
            builder, exporter.drawib_model_list[0]
        )
        lines = _all_builder_lines(builder)

        # 两个槽各捕获一次 palette（同一条 if/else 的两个分支，行内带缩进）
        stripped = [line.strip() for line in lines]
        self.assertEqual(
            stripped.count("ResourceZZPalette_5144c409_s1 = copy vs-t0 unless_null"), 1
        )
        self.assertEqual(
            stripped.count("ResourceZZPalette_5144c409_s2 = copy vs-t0 unless_null"), 1
        )
        self.assertIn("if $zz_ms_occ_0 == 1", lines)
        self.assertIn("else", lines)
        # 本组 2 个组件 × 2 个槽 = 4 条顶层 attach
        self.assertEqual(exporter._merged_group_component_ids(2), [0, 1])
        for cid in (0, 1):
            for slot in (1, 2):
                self.assertIn(
                    f"run = CustomShaderZZMIMergedSkeletonAttach_C{cid}_s{slot}", lines
                )
        # 骨架资源按槽分份
        builder2 = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder2)
        text2 = "\n".join(_all_builder_lines(builder2))
        self.assertIn("[ResourceZZMergedSkeleton_G2_s1]", text2)
        self.assertIn("[ResourceZZMergedSkeleton_G2_s2]", text2)
        self.assertNotIn("$zz_ms_group_phase", text2)


class ZZSIDirectPathGuardTests(unittest.TestCase):
    """直连路径（无 SO 重定向）的 v9 槽守卫回归。

    直连路径 = 该骨架组**没有**任何重定向 target：合并几何就挂在承载部件自己的
    deform draw 上，由本挂点的每槽守卫绘制（没有 RedirectSO 重放段）。v9 语义：
    只有「本组全部部件在该槽都已当帧到达」时才画，且守卫体内只有绑定与 draw
    （不得出现 run、不得给 $变量赋值）。帧闩锁 `$zz_ms_redirect_drawn` /
    相位 `$zz_ms_group_phase` 均已废除。
    """

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, weighted_groups):
        """注册假对象；weighted_groups = [(vertex_group_index, weight), ...]。"""
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[
                    types.SimpleNamespace(group=gid, weight=weight)
                    for gid, weight in weighted_groups
                ]
            )
        ]
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        max_index = max(gid for gid, _weight in weighted_groups)
        named = {gid: str(gid) for gid, _weight in weighted_groups}
        for index in range(max_index + 1):
            obj.vertex_groups.append(_FakeVertexGroup(named.get(index, f"pad{index}")))
        return obj

    def _components(self):
        # 组 G0 = C0（宿主，最后 deform pass，真实合并几何）+ C1（几何被吸收进 C0）
        return [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105,
             "skeleton_group": 0, "deform_draw": 20, "original_vertex_count": 12314,
             "vg_map": {i: i for i in range(105)}},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51,
             "skeleton_group": 0, "deform_draw": 2, "original_vertex_count": 4643,
             "vg_map": {i: 105 + i for i in range(51)}},
        ]

    def _make_exporter(self, components):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        sub_host = _FakeSubmesh(
            "LOD0.a23aa8a3-42759-0", 0, 105, 0,
            deform_draw=20, original_vertex_count=12314,
            exported_vertex_count=12314, match_first_index=0,
        )
        sub_host.drawcall_model_list = [dcm(
            obj_name="LOD0.a23aa8a3-42759-0",
            source_obj_name="LOD0.a23aa8a3-42759-0",
        )]
        sub_absorbed = _FakeSubmesh(
            "LOD0.b20f90ea-19182-0", 105, 51, 0,
            deform_draw=2, original_vertex_count=4643,
            exported_vertex_count=4643, match_first_index=19182,
        )
        models = [
            _FakeDrawIBModel("a23aa8a3", [sub_host]),
            _FakeDrawIBModel("b20f90ea", [sub_absorbed]),
        ]
        exporter = _make_exporter(models, merged_vgmap=True)
        components = _with_channel_plan(components)
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(components)
        }
        # C0 的顶点引用 0..104（自己），C1 引用 105（C0 的末槽）=> C1 被 C0 吸收。
        # C0 的 deform pass 晚于 C1（20 > 2），因此合并几何已在组内最后一个 deform
        # pass 上，自动重定向不产生 carrier/target 条目 = 直连路径。
        self._register_obj("LOD0.a23aa8a3-42759-0", [(0, 1.0)])
        self._register_obj("LOD0.b20f90ea-19182-0", [(105, 1.0)])
        (
            exporter._redirect_carrier_map,
            exporter._redirect_target_map,
            _unredirected,  # A-opt1：实例字段已删，仅占位对齐解包
        ) = exporter._build_merged_mesh_redirect_plan()
        return exporter, models

    def _vb_text(self, exporter, model):
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        return "\n".join(_all_builder_lines(builder))

    def test_fixture_really_is_direct_path(self):
        """前置断言：该夹具没有产生任何 carrier/target 重定向条目。"""
        exporter, _models = self._make_exporter(self._components())
        self.assertEqual(exporter._redirect_carrier_map, {})
        self.assertEqual(exporter._redirect_target_map, {})

    def test_direct_path_emits_per_slot_guard(self):
        """直连路径：每槽按**本部件出现次**绑槽绘制（v9.1，不等组内其它部件）。"""
        exporter, models = self._make_exporter(self._components())
        text = self._vb_text(exporter, models[0])

        # 1) 每槽绘制条件只读本部件出现次（无组级 seen、无 phase / drawn / ready）
        self.assertIn(
            "if $zz_ms_occ_0 == 1\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n"
            "    draw = 12314, 0\n"
            "endif",
            text,
        )
        self.assertIn(
            "if $zz_ms_occ_0 == 2\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s2\n"
            "    draw = 12314, 0\n"
            "endif",
            text,
        )
        for line in text.splitlines():
            if line.startswith("if "):
                self.assertNotIn("$zz_ms_seen_", line, line)
        self.assertNotIn("$zz_ms_group_phase", text)
        self.assertNotIn("$zz_ms_redirect_drawn", text)
        self.assertNotIn("$zz_ms_group_ready", text)
        # 2) 旧 `if !$zz_ms_redirect_drawn` 闩锁必须消失
        self.assertNotIn("if !$zz_ms_redirect_drawn", text)
        # 3) 绘制只在守卫内（组未齐不画，绝不用半帧骨架画合并几何）
        bare_draws = [
            line for line in text.splitlines()
            if line.strip().startswith("draw = ") and not line.startswith("    ")
        ]
        self.assertEqual(bare_draws, [], f"直连路径不得有顶层无条件 draw: {bare_draws}")
        # 4) 顶点数取导出 buffer 实际行数（合并几何从导出 VB 读）
        self.assertEqual(text.count("    draw = 12314, 0"), 2)
        # 5) 直连路径不写 SO 重定向资源
        self.assertNotIn("ResourceZZRedirectSO_", text)
        self.assertNotIn("so0 = ref", text)

    def test_direct_path_guard_draw_count_uses_exported_vertices(self):
        """回归：直连路径 draw 顶点数必须覆盖合并进来的其它部件几何。

        历史口径 `draw = draw_number`（原部件顶点数）会截掉被合并的部分；合并
        几何是从**导出** VB 读的，必须按导出顶点数画。
        """
        exporter, models = self._make_exporter(self._components())
        text = self._vb_text(exporter, models[0])
        # draw_number 桩值 4643 不得再出现（导出顶点数 = 12314）
        self.assertNotIn("draw = 4643, 0", text)
        self.assertIn("draw = 12314, 0", text)

    def test_absorbed_direct_hangpoint_keeps_group_gate(self):
        """边界：几何被吸收、又没有可用重放宿主的挂点仍保留组级 seen 门控。

        这类挂点画的是含组内其它部件顶点的合并几何——用半帧拼接的骨架画会得到
        错位/塌陷的模型。只有「几何只采样自己 vg_map 覆盖的槽位」的自足挂点才
        可以按本部件出现次直接绘制（v9.1）。
        """
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        sub_host = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0,
                                exported_vertex_count=12314, match_first_index=0)
        sub_absorbed = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0,
                                    exported_vertex_count=4643, match_first_index=19182)
        # 引用骨骼从**源对象**权重反查：必须挂上 drawcall（obj_name）才有数据可读
        sub_host.drawcall_model_list = [dcm(
            obj_name="LOD0.a23aa8a3-42759-0",
            source_obj_name="LOD0.a23aa8a3-42759-0",
        )]
        sub_absorbed.drawcall_model_list = [dcm(
            obj_name="LOD0.b20f90ea-19182-0",
            source_obj_name="LOD0.b20f90ea-19182-0",
        )]
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [sub_host]),
                _FakeDrawIBModel("b20f90ea", [sub_absorbed]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components = _with_channel_plan(self._components())
        exporter.merged_skeleton_component_id_dict = {"a23aa8a3": 0, "b20f90ea": 1}
        # b20f90ea 的顶点权重挂在 a23aa8a3 的槽位 0 上 ⇒ 几何被吸收；再把重定向
        # 计划清空（模拟"本轮没有任何可行的重放宿主"）⇒ 只能在本挂点用组级门控重放。
        self._register_obj("LOD0.a23aa8a3-42759-0", [(0, 1.0)])
        self._register_obj("LOD0.b20f90ea-19182-0", [(0, 1.0)])
        exporter._redirect_carrier_map = {}
        exporter._redirect_target_map = {}
        self.assertTrue(
            exporter._merged_component_geometry_absorbed(0, "b20f90ea"),
            "夹具失效：b20f90ea 应当被判为几何被吸收",
        )
        text = self._vb_text(exporter, exporter.drawib_model_list[1])

        self.assertIn(
            'if ($zz_ms_seen_11 >= 1) && ($zz_ms_prev_01 == 0 || $zz_ms_seen_01 >= 1)',
            text,
        )
        self.assertIn(
            'if ($zz_ms_seen_12 >= 1) && ($zz_ms_prev_02 == 0 || $zz_ms_seen_02 >= 1)',
            text,
        )
        self.assertNotIn("if $zz_ms_occ_1 == 1\n    vs-t0 = ResourceZZMergedSkeleton", text)

    def test_no_frame_latch_variables_in_constants_or_present(self):
        """直连/重定向路径都不再声明或复位帧闩锁/相位变量（v9 契约）。"""
        sub_host = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        sub_absorbed = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [sub_host]),
                _FakeDrawIBModel("b20f90ea", [sub_absorbed]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        exporter._redirect_carrier_map, exporter._redirect_target_map, _un = (
            exporter._build_merged_mesh_redirect_plan()
        )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(_all_builder_lines(builder))
        constants_text, present_text = text.split("[Present]")[0], text.split("[Present]")[1]

        for forbidden in (
            "$zz_ms_redirect_drawn_",
            "$zz_ms_group_ready_",
            "$zz_ms_group_phase_",
        ):
            self.assertNotIn(forbidden, constants_text)
            self.assertNotIn(forbidden, present_text)
        # v9 的唯一标记是 occ/seen：声明 + [Present] 清零
        self.assertIn("global $zz_ms_occ_0 = 0", constants_text)
        self.assertIn("global $zz_ms_seen_01 = 0", constants_text)
        self.assertIn("$zz_ms_occ_0 = 0", present_text)
        self.assertIn("$zz_ms_seen_01 = 0", present_text)
        # 已废除的辅助接口不得再存在（防止再次生成闩锁声明）
        self.assertFalse(hasattr(exporter, "_zz_ms_drawn_marker_draw_ibs"))

    def test_all_merged_drawibs_use_slot_guard(self):
        """契约：所有合并 DrawIB（重定向 + 直连）的守卫只用 occ/seen 项。

        `$zz_ms_redirect_drawn_<IB>` / `$zz_ms_group_ready_g<N>` /
        `$zz_ms_group_phase_g<N>` 已废除；VB 段里出现的每个 `$zz_ms_*` 仍必须已在
        [Constants] 声明（未声明的名字在 3DMigoto 里是部件级局部变量，跨 override
        段失效 -> 守卫形同虚设）。
        """
        import re

        sub_a = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        sub_b = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)
        sub_c = _FakeSubmesh("LOD0.64d7d56f-900-0", 156, 1, 1)
        exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [sub_a]),
                _FakeDrawIBModel("b20f90ea", [sub_b]),
                _FakeDrawIBModel("64d7d56f", [sub_c]),
            ],
            merged_vgmap=True,
        )
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        exporter._redirect_carrier_map, exporter._redirect_target_map, _un = (
            exporter._build_merged_mesh_redirect_plan()
        )

        vb_builder = _FakeIniBuilder()
        for model in exporter.drawib_model_list:
            exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        vb_text = "\n".join(_all_builder_lines(vb_builder))
        for forbidden in (
            "$zz_ms_redirect_drawn_",
            "$zz_ms_group_ready_",
            "$zz_ms_group_phase_",
        ):
            self.assertNotIn(forbidden, vb_text)
        self.assertIn("$zz_ms_occ_", vb_text)

        full_builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(full_builder)
        full = "\n".join(_all_builder_lines(full_builder))
        declared = set(re.findall(r"^\s*global\s+(\$zz_ms_[A-Za-z0-9_]+)", full, re.M))
        used = set(re.findall(r"(\$zz_ms_[A-Za-z0-9_]+)", vb_text))
        self.assertEqual(sorted(used - declared), [])
        # 出现在 if 条件里的 $变量必须在声明过（否则会被优化器静态折叠）
        conditions = re.findall(r"^if\s+(.*)$", vb_text, re.M)
        self.assertTrue(conditions)
        for condition in conditions:
            for var in re.findall(r"(\$zz_ms_[A-Za-z0-9_]+)", condition):
                self.assertIn(var, declared, f"if 条件里的变量未在 [Constants] 声明: {var}")

    def test_guarded_output_has_no_undeclared_dollar_vars(self):
        """生成的 VB 段不得出现未声明的 $zz_ms_* 变量（INI 契约）。"""
        import re
        exporter, models = self._make_exporter(self._components())
        text = self._vb_text(exporter, models[0])
        used = set(re.findall(r"(\$zz_ms_[A-Za-z0-9_]+)", text))

        sub_host = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0)
        sub_absorbed = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0)
        full_exporter = _make_exporter(
            [
                _FakeDrawIBModel("a23aa8a3", [sub_host]),
                _FakeDrawIBModel("b20f90ea", [sub_absorbed]),
            ],
            merged_vgmap=True,
        )
        (
            full_exporter.merged_skeleton_components,
            full_exporter.merged_skeleton_component_id_dict,
        ) = full_exporter._collect_merged_skeleton_components()
        builder = _FakeIniBuilder()
        full_exporter.add_merged_skeleton_sections(builder)
        full = "\n".join(_all_builder_lines(builder))
        declared = set(re.findall(r"^\s*global\s+(\$zz_ms_[A-Za-z0-9_]+)", full, re.M))
        present = full.split("[Present]")[1]
        undeclared = sorted(used - declared)
        self.assertEqual(undeclared, [])
        # 帧标记必须在 Present 清零（v9 只清零 occ/seen）
        present_vars = set(re.findall(r"(\$zz_ms_[A-Za-z0-9_]+)\s*=", present))
        self.assertTrue(
            used <= (declared | present_vars | {"$zz_ms_seen_11", "$zz_ms_seen_12"})
        )

    def test_single_component_group_draws_without_cross_part_gate(self):
        """单部件组：直接按出现次绘制，不需要任何跨部件条件（与用户实测口径一致）。"""
        submesh = _FakeSubmesh("LOD0.aaaaaaaa-100-0", 0, 11, 0)
        model = _FakeDrawIBModel("aaaaaaaa", [submesh])
        exporter = _make_exporter([model], merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        text = self._vb_text(exporter, model)

        # 单部件组：出现次恒为 1 的那一轮直接绘制（组件号 0）
        self.assertIn("if $zz_ms_occ_0 == 1", text)
        self.assertIn("if $zz_ms_occ_0 == 2", text)
        self.assertIn(
            "if $zz_ms_occ_0 == 1\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n"
            "    draw = 4643, 0\n"
            "endif",
            text,
        )
        # 单部件组不得出现其它部件的变量（曾把组级 seen 当门控）
        self.assertNotIn("$zz_ms_seen_11", text)
        self.assertNotIn("$zz_ms_group_phase", text)


class ZZSIMergedHostDirectPathTests(unittest.TestCase):
    """合并（join 成一个物体）导出的直连路径回归：合并几何由**任意兼容挂点**重放。

    背景（用户实测 2026-09-13）：合并几何挂在自己的导出 VB 上（宿主，几何引用组内
    其它部件的骨骼），旧口径只在宿主**自己**的 deform 段用组级守卫画它。而引擎把
    同组部件的 deform pass 排成什么顺序每帧都可能不同（dump 实证：同一批部件在
    075255/075506 是 B→C→A、080752 是 A→B→C）——宿主排在前面的帧里守卫永不成立，
    合并几何整段不写 → 用户看到的"合并之后还在闪"。

    v9.1 口径：宿主在自己段顶层把本轮 SO 引用捕获到 `ResourceZZRedirectSO_s<k>`，
    **本组每个 Blend 布局兼容的挂点**都发一条组级守卫重放（绑定宿主的 vb0/vb2 +
    该 SO + 宿主导出顶点数）——哪个挂点最后到达都能写，与提交顺序无关。
    """

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, weighted_groups):
        """注册假对象；weighted_groups = [(vertex_group_index, weight), ...]。"""
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[
                    types.SimpleNamespace(group=gid, weight=weight)
                    for gid, weight in weighted_groups
                ]
            )
        ]
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        max_index = max(gid for gid, _weight in weighted_groups)
        named = {gid: str(gid) for gid, _weight in weighted_groups}
        for index in range(max_index + 1):
            obj.vertex_groups.append(_FakeVertexGroup(named.get(index, f"pad{index}")))
        return obj

    def _components(self):
        """宿主 a23aa8a3（deform 20 = 组内最后一个，故不产生重定向计划）+ 两个占位部件。"""
        return [
            {"draw_ib": "a23aa8a3", "vg_offset": 0, "vg_count": 105, "skeleton_group": 0,
             "deform_draw": 20, "vg_map": {i: i for i in range(105)}},
            {"draw_ib": "b20f90ea", "vg_offset": 105, "vg_count": 51, "skeleton_group": 0,
             "deform_draw": 2, "vg_map": {i: 105 + i for i in range(51)}},
            {"draw_ib": "b30db54e", "vg_offset": 156, "vg_count": 14, "skeleton_group": 0,
             "deform_draw": 8, "vg_map": {i: 156 + i for i in range(14)}},
        ]

    def _make_exporter(self, sibling_blend_stride=32, host_groups=(0, 105, 156)):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        sub_host = _FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105, 0,
                                deform_draw=20, exported_vertex_count=12482)
        sub_b = _FakeSubmesh("LOD0.b20f90ea-19182-0", 105, 51, 0,
                             deform_draw=2, exported_vertex_count=3)
        sub_c = _FakeSubmesh("LOD0.b30db54e-7383-0", 156, 14, 0,
                             deform_draw=8, exported_vertex_count=3)
        for sub in (sub_host, sub_b, sub_c):
            sub.drawcall_model_list = [dcm(
                obj_name=sub.unique_str, source_obj_name=sub.unique_str
            )]
        models = [
            _FakeDrawIBModel("a23aa8a3", [sub_host]),
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("b30db54e", [sub_c]),
        ]
        for sibling in models[1:]:
            sibling.d3d11GameType.CategoryStrideDict["Blend"] = sibling_blend_stride
        exporter = _make_exporter(models, merged_vgmap=True)
        exporter.merged_skeleton_components = _with_channel_plan(self._components())
        exporter.merged_skeleton_component_id_dict = {
            "a23aa8a3": 0, "b20f90ea": 1, "b30db54e": 2
        }
        # 宿主的顶点引用了兄弟部件的槽位（105/156）⇒ 几何被吸收 = 合并宿主
        self._register_obj("LOD0.a23aa8a3-42759-0", [(gid, 1.0) for gid in host_groups])
        self._register_obj("LOD0.b20f90ea-19182-0", [(105, 1.0)])
        self._register_obj("LOD0.b30db54e-7383-0", [(156, 1.0)])
        (
            exporter._redirect_carrier_map,
            exporter._redirect_target_map,
            _unredirected,
        ) = exporter._build_merged_mesh_redirect_plan()
        return exporter, models

    def _vb_text(self, exporter, model):
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        return "\n".join(_all_builder_lines(builder))

    def test_fixture_is_direct_path_with_absorbed_host(self):
        """前置断言：宿主被判定为几何被吸收，且本轮没有产生重定向计划。"""
        exporter, _models = self._make_exporter()
        self.assertTrue(exporter._merged_component_geometry_absorbed(0, "a23aa8a3"))
        self.assertFalse(exporter._merged_component_geometry_absorbed(0, "b20f90ea"))
        self.assertEqual(exporter._redirect_carrier_map, {})
        self.assertEqual(exporter._redirect_target_map, {})
        self.assertEqual(
            [host["draw_ib"] for host in exporter._merged_group_absorbed_hosts(0)],
            ["a23aa8a3"],
        )

    def test_host_captures_own_so_and_replays_own_geometry(self):
        """宿主段：顶层捕获本轮 SO 引用 + 组级守卫重放自己的合并几何。"""
        exporter, models = self._make_exporter()
        text = self._vb_text(exporter, models[0])

        # 两槽各捕获一次 SO 引用（在自己的 deform 段里，so0 就是本部件的 SO）
        self.assertEqual(text.count("    ResourceZZRedirectSO_G0_s1 = ref so0"), 1)
        self.assertEqual(text.count("    ResourceZZRedirectSO_G0_s2 = ref so0"), 1)
        # 组级守卫 + 显式绑定 SO/宿主的 vb0/vb2 + 宿主导出顶点数
        self.assertIn(
            'if ($zz_ms_seen_01 >= 1) && ($zz_ms_prev_11 == 0 || $zz_ms_seen_11 >= 1) && ($zz_ms_prev_21 == 0 || $zz_ms_seen_21 >= 1)\n    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n    so0 = ref ResourceZZRedirectSO_G0_s1\n    vb2 = Resourcea23aa8a3Blend\n    vb0 = Resourcea23aa8a3Position\n    draw = 12482, 0\n    so0 = null\nendif',
            text,
        )
        # 宿主的几何**不得**自足绘制（它跨部件、必须等全组到位）
        self.assertNotIn("if $zz_ms_occ_0 == 1\n    vs-t0 = ResourceZZMergedSkeleton", text)

    def test_compatible_sibling_also_replays_host_geometry(self):
        """关键回归：布局兼容的兄弟挂点也要发同一条重放（与提交顺序无关）。"""
        exporter, models = self._make_exporter()
        host_text = self._vb_text(exporter, models[0])
        sib_text = self._vb_text(exporter, models[1])

        # 兄弟挂点：先自足画自己的几何（3 顶点占位）……
        self.assertIn(
            "if $zz_ms_occ_1 == 1\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n"
            "    draw = 3, 0\n"
            "endif",
            sib_text,
        )
        # ……再发宿主合并几何的组级守卫重放（宿主排在前面的帧由它闭合）
        self.assertIn(
            'if ($zz_ms_seen_01 >= 1) && ($zz_ms_prev_11 == 0 || $zz_ms_seen_11 >= 1) && ($zz_ms_prev_21 == 0 || $zz_ms_seen_21 >= 1)\n    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n    so0 = ref ResourceZZRedirectSO_G0_s1\n    vb2 = Resourcea23aa8a3Blend\n    vb0 = Resourcea23aa8a3Position\n    draw = 12482, 0\n    so0 = null\nendif',
            sib_text,
        )
        self.assertIn("so0 = ref ResourceZZRedirectSO_G0_s2", sib_text)
        # 宿主段不能捕获兄弟的 SO：捕获只在宿主自己段发生
        self.assertNotIn("ResourceZZRedirectSO_G0_s1 = ref so0", sib_text)
        self.assertIn("ResourceZZRedirectSO_G0_s1 = ref so0", host_text)

    def test_incompatible_sibling_does_not_replay(self):
        """Blend 布局不兼容的兄弟挂点不得重放（BI4 与 BW16_BI16 不能混用）。"""
        exporter, models = self._make_exporter(sibling_blend_stride=16)
        sib_text = self._vb_text(exporter, models[1])
        # 自己那段照旧自足绘制
        self.assertIn("    draw = 3, 0", sib_text)
        # 但不发宿主重放（否则会把权重按错误格式解释 → 流输出全零）
        self.assertNotIn("so0 = ref ResourceZZRedirectSO", sib_text)
        self.assertNotIn("vb0 = Resourcea23aa8a3Position", sib_text)

    def test_last_narrow_donor_publishes_host_without_waiting_for_unused_sibling(self):
        exporter, models = self._make_exporter(sibling_blend_stride=16, host_groups=(0, 105))

        host_text = self._vb_text(exporter, models[0])
        donor_text = self._vb_text(exporter, models[1])
        unused_text = self._vb_text(exporter, models[2])
        guard = (
            "if ($zz_ms_seen_01 >= 1) && "
            "($zz_ms_prev_11 == 0 || $zz_ms_seen_11 >= 1)"
        )
        self.assertIn(f"{guard}\n    vs-t0", host_text)
        self.assertIn(
            f"{guard} && $zz_ms_occ_1 == 1\n"
            "    run = CustomShaderZZMISkin_G0_s1_a23aa8a3",
            donor_text,
        )
        self.assertNotIn("CustomShaderZZMISkin_G0_s1_a23aa8a3", unused_text)
        self.assertNotIn("vb2 = Resourcea23aa8a3Blend", donor_text)

        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        skeleton_text = "\n".join(_all_builder_lines(builder))
        self.assertIn("[CustomShaderZZMISkin_G0_s1_a23aa8a3]", skeleton_text)
        self.assertIn("cs-u0 = ref ResourceZZSkinWrite_a23aa8a3_s1", skeleton_text)

        render_lines = []
        exporter._append_merged_absorbed_skin_render(render_lines, "a23aa8a3")
        self.assertIn("    vb0 = ref ResourceZZSkinOut_a23aa8a3_s1", render_lines)

    def test_disconnected_donor_uses_fresh_render_cb1_before_skinning(self):
        exporter, models = self._make_exporter(sibling_blend_stride=16, host_groups=(0, 105))
        exporter._zzmi_direct_cb1_donors = {"a23aa8a3": ("b20f90ea",)}
        host_text = self._vb_text(exporter, models[0])
        donor_text = self._vb_text(exporter, models[1])
        self.assertIn("ResourceZZDirectCB1SO_a23aa8a3_s1 = ref so0", host_text)
        self.assertIn("ResourceZZDirectCB1SO_b20f90ea_s1 = ref so0", donor_text)
        self.assertNotIn("vb0 = Resourcea23aa8a3Position", host_text)
        self.assertNotIn("run = CustomShaderZZMISkin_G0_s1_a23aa8a3", donor_text)

        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        ini = "\n".join(_all_builder_lines(builder))
        self.assertIn("cs-t2 = ref ResourceZZDirectCB1Palette_a23aa8a3_s1", ini)
        self.assertIn("    run = CustomShaderZZDirectCB1Rebase_a23aa8a3_b20f90ea_s1\n"
                      "    local $zz_cb1_saved_x1 = x1", ini)
        self.assertIn("if $zz_cb1_published_a23aa8a3_s1 == 0", ini)
        self.assertIn("$zz_cb1_ready_b20f90ea_s1 = 0", ini)

        render = []
        self.assertTrue(exporter._append_direct_cb1_draw_begin(render, "a23aa8a3"))
        render_text = "\n".join(render)
        self.assertIn("@vb0 == @ResourceZZDirectCB1SO_a23aa8a3_s1", render_text)
        self.assertIn("$zz_cb1_ready_b20f90ea_s1 != 0", render_text)
        self.assertIn("$zz_cb1_pose_b20f90ea_s2 == 0", render_text)
        self.assertIn("run = CommandListZZDirectCB1Skin_a23aa8a3_s1", render_text)

    def test_host_without_supported_skin_layout_prints_diagnostic(self):
        import contextlib
        import io

        exporter, models = self._make_exporter(sibling_blend_stride=16)
        models[0].d3d11GameType.CategoryStrideDict["Position"] = 28
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._vb_text(exporter, models[0])
        self.assertIn("SKIN_LAYOUT_UNSUPPORTED", buf.getvalue())

    def test_calibrated_fallback_keeps_the_body_draw_when_the_donor_is_absent(self):
        exporter, _ = self._make_exporter(sibling_blend_stride=16, host_groups=(0, 105))
        exporter._zzmi_direct_cb1_donors = {"a23aa8a3": ("b20f90ea",)}
        reference = bytes(range(64))
        exporter._zzmi_direct_cb1_references = {"a23aa8a3": {"b20f90ea": reference}}
        written = {}
        exporter._atomic_write_binary = lambda path, contents: written.update({os.path.basename(path): contents})

        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        ini = "\n".join(_all_builder_lines(builder))
        self.assertEqual(written["zz_cb1_reference_a23aa8a3_b20f90ea.buf"], reference)
        self.assertIn("cs = ./res/zzmi_cb1_anchor_rebase.hlsl", ini)
        self.assertIn("if $zz_cb1_live_b20f90ea != 0\n        run = CustomShaderZZDirectCB1Rebase", ini)
        self.assertIn("else\n        run = CustomShaderZZDirectCB1Anchor_a23aa8a3_b20f90ea_s1", ini)

        render = []
        self.assertTrue(exporter._append_direct_cb1_draw_begin(render, "a23aa8a3"))
        render_text = "\n".join(render)
        self.assertIn("if @vb0 == @ResourceZZDirectCB1SO_a23aa8a3_s1", render_text)
        self.assertNotIn("$zz_cb1_ready_b20f90ea_s1", render_text)


class ZZMIStubObjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zzmi_stub_ws_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        lod0 = os.path.join(self.tmp, "LOD0")
        os.makedirs(lod0, exist_ok=True)
        component_map = {
            "84618ee0": {"0": "84618ee0-22296-0", "1": "84618ee0-1164-22296"},
            "b20f90ea": {"0": "b20f90ea-19182-0"},
        }
        with open(os.path.join(lod0, "DrawIB-Component.json"), "w", encoding="utf-8") as f:
            json.dump(component_map, f)
        _fake_global_config.path_workspace_folder = lambda: self.tmp
        self.addCleanup(lambda: setattr(_fake_global_config, "path_workspace_folder", lambda: ""))
        # 清 fake bpy 注册表
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def test_present_object_in_scene_does_not_suppress_missing_sibling_stub(self):
        """**阻断修复（t80 §2.3）**：`present` 必须按**场景里真实存在的对象**判定。

        形态：同一 DrawIB 的一个组件有真实对象、另一个组件只在蓝图/工作区里声明
        （没有场景对象）。修复前 `present` 取自 DrawCall 的**声明名**集合 ⇒ 缺席的
        兄弟组件被判成"存在" ⇒ 不插桩 ⇒ 紧随其后的 SubMeshModel 找不到 Blender
        对象直接 Fatal（实测 19 件声明 / 4 个 mesh 的真实工程）。

        本用例把两个组件都注册成真实对象，再删掉其中一个的对象（模拟"声明了、
        但场景里没有"），断言另一个组件补了占位。
        """
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        home = self._register_present_object_with_groups(
            "LOD0.84618ee0-22296-0", [7], group_names=["7"]
        )
        sibling = self._register_present_object_with_groups(
            "LOD0.84618ee0-1164-22296", [7], group_names=["7"]
        )
        _fake_bpy_data.objects.remove(sibling)

        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        stub = _fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296")
        self.assertIsNotNone(
            stub, "场景里缺席的兄弟组件必须由生成器注入占位，否则 SubMeshModel Fatal"
        )
        self.assertEqual(stub.get("ZZMI_STUB"), 1)
        exporter._cleanup_stub_objects()
        # 真实存在的那个对象必须原样保留（不得被当成占位清理掉）
        self.assertIsNotNone(_fake_bpy_data.objects.get(home.name))

    def test_stub_created_for_missing_component(self):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(len(exporter._zzmi_stub_object_names), 1)

        stub = _fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296")
        self.assertIsNotNone(stub)
        self.assertEqual(stub.get("ZZMI_STUB"), 1)
        self.assertEqual(stub.vertex_groups[0].name, "0")
        self.assertEqual(stub.vertex_groups[0].add_calls[0][1], 1.0)
        stub_draw_call = next(
            dc for dc in ordered
            if dc.get_workspace_unique_str() == "LOD0.84618ee0-1164-22296"
        )
        # 占位段即使在 SubMeshModel 之前被消费，也必须是可绘制的 3 索引。
        self.assertEqual(stub_draw_call.vertex_count, 3)
        self.assertEqual(stub_draw_call.index_count, 3)
        self.assertEqual(stub_draw_call.index_offset, 0)
        # 极限小三角面
        verts, _edges, faces = stub.data.from_pydata_calls[0]
        self.assertEqual(len(verts), 3)
        self.assertEqual(faces, [(0, 1, 2)])
        self.assertLess(max(abs(c) for v in verts for c in v), 1e-3)

        exporter._cleanup_stub_objects()
        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296"))
        self.assertEqual(exporter._zzmi_stub_object_names, [])
        self.assertNotIn(
            "LOD0.84618ee0-1164-22296",
            [str(dc.get_workspace_unique_str()) for dc in ordered],
        )

    def test_constructor_failure_after_stub_injection_cleans_all_stub_state(self):
        """基类构造失败时 export() 尚未运行，也必须清理对象、mesh 和注入 DrawCall。"""
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        blueprint_model = types.SimpleNamespace(
            cross_ib_info_dict={},
            cross_ib_method_dict={},
            cross_ib_mapping_method={},
            has_cross_ib=False,
            cross_ib_object_names=set(),
            keyname_mkey_dict={},
            ordered_draw_obj_data_model_list=ordered,
        )

        with mock.patch.object(
            _FakeExportUnity,
            "__init__",
            side_effect=RuntimeError("forced base constructor failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced base constructor failure"):
                _zzmi_module.ExportZZMI(blueprint_model)

        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296"))
        self.assertEqual(_fake_bpy_data.meshes._items, {})
        self.assertNotIn(
            "LOD0.84618ee0-1164-22296",
            [str(dc.get_workspace_unique_str()) for dc in ordered],
        )

    def test_stub_creation_failure_mid_batch_rolls_back_earlier_stub(self):
        """批量补占位中途失败时，已创建但尚未从 helper 返回的占位也必须回滚。"""
        lod0 = os.path.join(self.tmp, "LOD0")
        with open(os.path.join(lod0, "DrawIB-Component.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "84618ee0": {
                        "0": "84618ee0-22296-0",
                        "1": "84618ee0-1164-22296",
                        "2": "84618ee0-300-23460",
                    }
                },
                f,
            )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        blueprint_model = types.SimpleNamespace(
            cross_ib_info_dict={},
            cross_ib_method_dict={},
            cross_ib_mapping_method={},
            has_cross_ib=False,
            cross_ib_object_names=set(),
            keyname_mkey_dict={},
            ordered_draw_obj_data_model_list=ordered,
        )
        original_create = _zzmi_module.ExportZZMI._create_stub_object
        create_count = 0

        def fail_second_create(exporter, bare_unique_str):
            nonlocal create_count
            create_count += 1
            if create_count == 2:
                raise RuntimeError("forced second stub failure")
            return original_create(exporter, bare_unique_str)

        with mock.patch.object(
            _zzmi_module.ExportZZMI,
            "_create_stub_object",
            new=fail_second_create,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced second stub failure"):
                _zzmi_module.ExportZZMI(blueprint_model)

        self.assertEqual(_fake_bpy_data.objects._items, {})
        self.assertEqual(_fake_bpy_data.meshes._items, {})
        self.assertEqual(
            [str(dc.get_workspace_unique_str()) for dc in ordered],
            ["LOD0.84618ee0-22296-0"],
        )

    def test_buffers_only_failure_still_cleans_stub_transaction(self):
        """多轮导出的 buffer-only 路径也必须在失败时清理占位事务。"""
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        with mock.patch.object(
            _FakeExportUnity,
            "export_buffers_only",
            side_effect=RuntimeError("forced buffer export failure"),
            create=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced buffer export failure"):
                exporter.export_buffers_only()

        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296"))
        self.assertEqual(_fake_bpy_data.meshes._items, {})
        self.assertEqual(
            [str(dc.get_workspace_unique_str()) for dc in ordered],
            ["LOD0.84618ee0-22296-0"],
        )

    def test_same_blueprint_can_inject_and_cleanup_stub_twice(self):
        """一次导出清理后，同一 BluePrintModel 再导出不能引用已删除的旧 DrawCall。"""
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]

        first = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
        first._cleanup_stub_objects()
        self.assertEqual(len(ordered), 1)

        second = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
        self.assertEqual(len(second._zzmi_stub_object_names), 1)
        self.assertEqual(len(ordered), 2)
        second._cleanup_stub_objects()
        self.assertEqual(len(ordered), 1)

    def test_no_stub_when_checkbox_off(self):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=False, ordered_drawcalls=ordered)
        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_no_stub_when_whole_drawib_absent(self):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
        # 84618ee0 整个 DrawIB 不在蓝图且无 VGMap 数据 = 不生成，不插桩
        self.assertEqual(exporter._zzmi_stub_object_names, [])
        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertEqual(names, ["LOD0.b20f90ea-19182-0"])

    def _enable_pool_route(self):
        """登记真实方案模块并复位池缓存，供「池路线」用例使用。

        逐用例登记（而非模块级）是为了不改变同文件其它契约的产物基准；用例结束
        把缓存与解析结果还原，避免把本用例的临时工作区带进下一条用例。
        """
        module = _load_real_module(
            f"{PKG}.common.zzmi_cross_group_palette", "common/zzmi_cross_group_palette.py"
        )
        helper = sys.modules[f"{PKG}.common.zzmi_skeleton"].ZZMISkeletonMergeHelper
        previous_profile = getattr(helper, "cross_group_profile", None)
        self.addCleanup(setattr, helper, "cross_group_profile", previous_profile)
        self.addCleanup(setattr, _zzmi_module, "_MERGED_POOL_SEGMENTS_CACHE", None)
        _zzmi_module._MERGED_POOL_SEGMENTS_CACHE = None
        return module

    def _write_pool_profile(self):
        """写出骨骼池方案（形状取自菲欧妮实测，池缩到 9 根）。

        三段：``aaaa0001`` [0,4) 是根，``aaaa0002`` [4,7) 借位使用 1、2 号槽位，
        ``aaaa0003`` [7,9) 两根骨**全部**借位到前两段 —— 自属段为空，正是实测
        ``b11ff137-6078-0`` 被吸收时的形态。走生产写盘口，方案不合法会让用例
        立刻炸，而不是静默退回旧口径。
        """
        profile = {
            "format_version": 1,
            "mode": "ZZMI_CROSS_GROUP_PALETTE",
            "components": [
                {
                    "component_id": 0,
                    "ib_hash": "aaaa0001",
                    "role": "merged",
                    "vg_offset": 0,
                    "vg_count": 4,
                    "vg_map": {"0": 0, "1": 1, "2": 2, "3": 3},
                    "parent_component_id": 0,
                    "anchor_parent_slot": 0,
                    "anchor_self_slot": 0,
                },
                {
                    "component_id": 1,
                    "ib_hash": "aaaa0002",
                    "role": "merged",
                    "vg_offset": 4,
                    "vg_count": 3,
                    "vg_map": {"0": 1, "1": 4, "2": 2},
                    "parent_component_id": 0,
                    "anchor_parent_slot": 1,
                    "anchor_self_slot": 4,
                },
                {
                    "component_id": 2,
                    "ib_hash": "aaaa0003",
                    "role": "merged",
                    "vg_offset": 7,
                    "vg_count": 2,
                    "vg_map": {"0": 2, "1": 1},
                    "parent_component_id": 1,
                    "anchor_parent_slot": 5,
                    "anchor_self_slot": 7,
                },
            ],
        }
        _palette_module = self._enable_pool_route()
        _palette_module.write_profile(self.tmp, profile)

    def _write_pool_workspace(self):
        """池方案 + 三族部件 json + 组件表（载体对象由用例自己登记）。"""
        self._write_pool_profile()
        self._write_vgmap_json_full(
            "aaaa0001-100-0", {0: 0, 1: 1, 2: 2, 3: 3}, vg_offset=0, vg_count=4
        )
        self._write_vgmap_json_full(
            "aaaa0002-200-0", {0: 1, 1: 4, 2: 2}, vg_offset=4, vg_count=3
        )
        self._write_vgmap_json_full(
            "aaaa0003-300-0", {0: 2, 1: 1}, vg_offset=7, vg_count=2
        )
        self._write_component_map(
            {
                "aaaa0001": {"0": "aaaa0001-100-0"},
                "aaaa0002": {"0": "aaaa0002-200-0"},
                "aaaa0003": {"0": "aaaa0003-300-0"},
            }
        )

    def test_pool_plan_stubs_absorbed_part_whose_slots_are_all_borrowed(self):
        """**骨骼池路线**：整份 VGMap 都借位的被吸收部件仍必须补占位。

        实测（2026-10-23 菲欧妮）：``b11ff137-6078-0`` 的 16 根骨全部借位到宿主
        池段，自属段为空 ⇒ 走 `_is_drawib_absorbed` 的退化分支；而宿主的池级
        VGMap 把这些借位槽位一并声明了 ⇒ 旧口径「引用方声明过的不算外来」把证据
        吃干净 ⇒ 判「没被吸收」⇒ 整族缺席的部件既不插桩也不报错，游戏里整块
        几何不见。

        可证伪：修复前 `_zzmi_stub_object_names == []`（必红）。
        """
        self._write_pool_workspace()
        # 在场：aaaa0001 自己（引用全在自属段内）、aaaa0002（引用自属段里的 5
        # 加借位来的 1、2 —— 1、2 正是被吸收部件 aaaa0003 的两根骨）
        self._register_present_object_with_groups("LOD0.aaaa0001-100-0", [0, 1, 2, 3])
        self._register_present_object_with_groups("LOD0.aaaa0002-200-0", [5, 2, 1])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [
            dcm(obj_name="LOD0.aaaa0002-200-0"),
            dcm(obj_name="LOD0.aaaa0001-100-0"),
        ]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        self.assertEqual(
            exporter._zzmi_stub_object_names,
            ["LOD0.aaaa0003-300-0"],
            "池方案下被吸收部件的槽位全被宿主声明过，也只能靠池段口径认出吸收",
        )
        stub = _fake_bpy_data.objects.get("LOD0.aaaa0003-300-0")
        self.assertIsNotNone(stub)
        self.assertEqual(stub.get("ZZMI_STUB"), 1)
        # 占位权重必须挂在被吸收部件自己 json 的已注册槽位（VGMap 首值 2）
        self.assertEqual(stub.vertex_groups[0].name, "2")
        exporter._cleanup_stub_objects()

    def test_pool_plan_treats_references_inside_referrer_segment_as_own(self):
        """池路线不得把「段内引用」当吸收证据（对照组，防止一刀切成乱插桩）。

        载体只引用自己池段 [4,7) 里的槽位 ⇒ 外来集合为空 ⇒ 缺席的 aaaa0003 不被
        判「已被吸收」⇒ 不插桩。
        """
        self._write_pool_workspace()
        self._register_present_object_with_groups("LOD0.aaaa0001-100-0", [0, 1, 2, 3])
        self._register_present_object_with_groups("LOD0.aaaa0002-200-0", [4, 5])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [
            dcm(obj_name="LOD0.aaaa0002-200-0"),
            dcm(obj_name="LOD0.aaaa0001-100-0"),
        ]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        self.assertEqual(exporter._zzmi_stub_object_names, [])
        self.assertIsNone(_fake_bpy_data.objects.get("LOD0.aaaa0003-300-0"))

    def _write_vgmap_json(self, bare, gid, group=None, excluded=False):
        type_dir = os.path.join(self.tmp, "LOD0", bare, "TYPE_GPU_TEST_")
        os.makedirs(type_dir, exist_ok=True)
        payload = {"VGMap": {"0": str(gid)}, "VGOffset": 0, "VGCount": 1}
        if group is not None:
            payload["SkeletonGroup"] = group
        if excluded:
            payload["VGMapDedupExcluded"] = True
        with open(os.path.join(type_dir, bare + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _register_present_object_with_groups(self, name, used_gids, group_names=None):
        mesh = _fake_bpy_data.meshes.new(name=name + "_mesh")
        group_names = list(group_names or [str(gid) for gid in used_gids])
        obj = _fake_bpy_data.objects.new(name=name, object_data=mesh)
        obj.vertex_groups = _FakeVertexGroups()
        for group_name in group_names:
            obj.vertex_groups.append(_FakeVertexGroup(str(group_name)))
        group_indices = [group_names.index(str(gid)) for gid in used_gids]
        mesh.vertices = [
            types.SimpleNamespace(
                groups=[types.SimpleNamespace(group=group_index, weight=1.0)]
            )
            for group_index in group_indices
        ]
        return obj

    def _write_vgmap_json_full(self, bare, vg_map, vg_offset, vg_count, group=None):
        """写出带真实 VGOffset/VGCount 的部件 json（复刻工作空间真实布局）。

        VGMap 值允许借位落在别的部件声明段（跨部件 bitwise 去重的 canonical），
        自属声明段 = [vg_offset, vg_offset + vg_count)。
        """
        type_dir = os.path.join(self.tmp, "LOD0", bare, "TYPE_GPU_TEST_")
        os.makedirs(type_dir, exist_ok=True)
        payload = {
            "VGMap": {str(k): str(v) for k, v in vg_map.items()},
            "VGOffset": int(vg_offset),
            "VGCount": int(vg_count),
        }
        if group is not None:
            payload["SkeletonGroup"] = group
        with open(os.path.join(type_dir, bare + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _write_component_map(self, component_map):
        lod0 = os.path.join(self.tmp, "LOD0")
        os.makedirs(lod0, exist_ok=True)
        with open(os.path.join(lod0, "DrawIB-Component.json"), "w", encoding="utf-8") as f:
            json.dump(component_map, f)

    def test_skipped_part_is_removed_from_ordered_draw_calls(self):
        """**阻断修复（t81 阻断④）**：判「按用户意图不生成」的部件必须从模型列表摘除。

        现象：`_ensure_stub_objects_for_missing_parts` 只 `continue`（不建占位），
        DrawCall 仍留在 `ordered_draw_obj_data_model_list` 里 ⇒ 下游
        `SubMeshModel` 去 `bpy.data.objects` 找那个**不存在**的对象并 `Fatal:
        找不到 Blender 对象: 'LOD0.c28e6303-7308-0'`（实测真实工程里
        `{611df76d, 93c3c2b7, c28e6303×3}` 五件如此）。手改参考版里这些部件
        total=0 ⇒ 决策对，缺的只是「决策 → 模型列表」的传播。

        可证伪：修复前 `names` 里仍会有 `LOD0.c28e6303-7308-0`（必红）。
        """
        # 缺席 + 专属槽位无人引用 ⇒ 判「不生成」
        self._write_component_map({"c28e6303": {"0": "c28e6303-7308-0"}})
        vg_map = {"0": 0}
        for index, slot in enumerate(range(168, 177), start=1):
            vg_map[str(index)] = slot
        self._write_vgmap_json_full(
            "c28e6303-7308-0", vg_map, vg_offset=167, vg_count=10, group=0
        )
        # 场景里有另一个真实对象（载体），但只引用借位槽位 0
        self._register_present_object_with_groups("LOD0.8c8de427-798-0", [0])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        skipped = dcm(obj_name="LOD0.c28e6303-7308-0")   # 声明了，场景里没有
        carrier = dcm(obj_name="LOD0.8c8de427-798-0")    # 真实存在
        ordered = [carrier, skipped]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn(
            "LOD0.c28e6303-7308-0",
            names,
            "判不生成的部件不得留在模型列表里（否则下游按不存在对象构造 ⇒ Fatal）",
        )
        self.assertIn("LOD0.8c8de427-798-0", names, "真实存在的部件不得被误摘")
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_empty_scene_fallback_never_removes_draw_calls(self):
        """边界钉子：拿不到任何真实场景对象时（轻量宿主 / 空注册表）**不得**摘除。

        `scene_present` 为空 ⇒ 兼容兜底退回按**声明名**判定（宁可不插桩，也不在
        信息不足时把全部部件判成缺席并摘掉）⇒ `ordered` 原样保留。这条钉住
        「摘除只在**正向判定为缺席**时发生」，防止把 UI/无头环境差异变成静默丢件。
        """
        self._write_component_map({"611df76d": {"0": "611df76d-132-0"}})
        self._write_vgmap_json("611df76d-132-0", 7)
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.611df76d-132-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
        self.assertEqual(
            [str(dc.get_workspace_unique_str()) for dc in ordered],
            ["LOD0.611df76d-132-0"],
            "空注册表 ⇒ 无法证明缺席 ⇒ 必须原样保留（不静默丢件）",
        )
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_no_stub_when_only_dedup_borrowed_slots_are_referenced(self):
        """回归（2026-09-16 叶瞬光01 脸部被误插占位事故，同类样本 869976a3-5202-0）。

        只被「借位 canonical 槽位」引用的缺席部件不得判成被吸收。

        复刻真实数据：c28e6303（自属声明段 [167,177)，原顶点 2789）的 VGMap 里
        既有自属段槽位 168..176，也有借位到 01ef4403 声明段 [0,34) 的全身共享
        根骨骼槽位 0；载体对象 8c8de427-798-0 只引用了借位槽位 0。
        旧判据（全量 VGMap 值域 ∩ used）会因槽位 0 命中而注入占位；新判据只看
        自属声明段 → 判定「未被吸收」→ 不插桩（游戏内保留原版绘制）。
        """
        self._write_component_map({"c28e6303": {"0": "c28e6303-7308-0"}})
        vg_map = {"0": 0}  # 借位：0 属于别的部件声明段
        for index, slot in enumerate(range(168, 177), start=1):
            vg_map[str(index)] = slot
        self._write_vgmap_json_full(
            "c28e6303-7308-0", vg_map, vg_offset=167, vg_count=10, group=0
        )
        # 载体对象引用了借位槽位 0（真实数据里 0 是全身共享的根骨骼）
        self._register_present_object_with_groups("LOD0.8c8de427-798-0", [0])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.8c8de427-798-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn("LOD0.c28e6303-7308-0", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_stub_when_absent_drawib_own_segment_slots_are_referenced(self):
        """R7 ③ 行为保留：真被 join 走的部件仍必须补占位。

        复刻真实数据：4a178546（自属声明段 [209,256)，原顶点 3859）被 join 进
        载体对象 869976a3-5202-0（16350 顶点）→ 载体的顶点带上了 209..254 的
        权重（同时它的 VGMap 里还有借位槽位 185）→ 自属段命中 → 补占位。
        """
        self._write_component_map({"4a178546": {"0": "4a178546-18468-0"}})
        vg_map = {str(slot - 209): slot for slot in range(209, 255)}
        vg_map["45"] = 185  # 借位槽位（3b1b73fe 声明段），与吸收证据无关
        self._write_vgmap_json_full(
            "4a178546-18468-0", vg_map, vg_offset=209, vg_count=47, group=2
        )
        # 载体对象：既引用借位槽位 185，也引用 4a178546 的自属段槽位 209/210
        self._register_present_object_with_groups(
            "LOD0.869976a3-5202-0", [185, 209, 210]
        )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.869976a3-5202-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.4a178546-18468-0", names)
        self.assertEqual(
            exporter._zzmi_stub_object_names, ["LOD0.4a178546-18468-0"]
        )
        exporter._cleanup_stub_objects()

    def test_stub_when_absent_drawib_absorbed_into_other_object(self):
        # 84618ee0 全缺，但其 VGMap 全局 id=7 被现存对象（b20f90ea）的顶点引用 = 被合并
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        self._register_present_object_with_groups("LOD0.b20f90ea-19182-0", [7])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.84618ee0-22296-0", names)
        self.assertIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(len(exporter._zzmi_stub_object_names), 2)
        exporter._cleanup_stub_objects()

    def test_no_stub_when_absent_drawib_not_referenced(self):
        # 84618ee0 全缺，其 VGMap 全局 id=250 没有任何对象引用 = 用户故意不生成
        self._write_vgmap_json("84618ee0-22296-0", 250)
        self._write_vgmap_json("84618ee0-1164-22296", 250)
        self._register_present_object_with_groups("LOD0.b20f90ea-19182-0", [7])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn("LOD0.84618ee0-22296-0", names)
        self.assertNotIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_no_stub_when_shared_dedup_group_is_declared_by_referrer(self):
        """用户报告（2026-10-14）：去重合并组不得把**未 join** 的部件误判成被吸收。

        A 的顶点组 {1,2,3,4,5}、B 的 {5,6,7,8}，其中 5 是顶点组去重时合并的
        「合并组」且 canonical 落在 **B** 的自属声明段 [5,9)；只导出 A 时 A 确实
        引用了 5，但 5 在 **A 自己的** VGMap 值域里（A 借位使用 canonical 槽）
        ⇒ 该引用能用 A 自己的局部顶点组解释 ⇒ B 没被 join 进 A ⇒ 不得给 B 补占位
        （游戏内保留原版绘制）。

        可证伪：修复前 foreign 集合 = 全部引用（{1,2,3,4,5}）⇒ 命中 B 自属段槽位
        5 ⇒ 误插占位且把 B 从「不生成」名单里摘掉（必红）。
        """
        # B：自属声明段 [5,9)，VGMap 5..8
        self._write_vgmap_json_full(
            "b2222222-2-0",
            {str(i): 5 + i for i in range(4)},
            vg_offset=5,
            vg_count=4,
        )
        # A：自属声明段 [1,6)，VGMap 1..5（5 = 借位使用 B 的 canonical 槽）
        self._write_vgmap_json_full(
            "a1111111-1-0",
            {str(i): 1 + i for i in range(5)},
            vg_offset=1,
            vg_count=5,
        )
        self._write_component_map(
            {
                "a1111111": {"0": "a1111111-1-0"},
                "b2222222": {"0": "b2222222-2-0"},
            }
        )
        self._register_present_object_with_groups("LOD0.a1111111-1-0", [1, 2, 3, 4, 5])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        carrier = dcm(obj_name="LOD0.a1111111-1-0")  # 场景里真实存在
        absent = dcm(obj_name="LOD0.b2222222-2-0")  # 声明了，场景里没有
        ordered = [carrier, absent]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn(
            "LOD0.b2222222-2-0",
            names,
            "未被 join 的部件不得被插桩（游戏内应显示原版）",
        )
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_stub_when_referrer_uses_group_it_never_declared(self):
        """对照组：引用方引用了**自己没声明过**的槽位 ⇒ 真吸收 ⇒ 仍必须补占位。

        A 的对象引用 {1,2,3,4,5}，但 A 的 json 只声明 {1,2,3,4}（第 5 组来自被
        join 进来的 B 的几何）⇒ 5 无法用 A 自己的局部顶点组解释 ⇒ B 补占位抑制
        原版 draw 防止重影（计划书 R7 ③）。
        """
        self._write_vgmap_json_full(
            "b2222222-2-0",
            {str(i): 5 + i for i in range(4)},
            vg_offset=5,
            vg_count=4,
        )
        self._write_vgmap_json_full(
            "a1111111-1-0",
            {str(i): 1 + i for i in range(4)},
            vg_offset=1,
            vg_count=4,
        )
        self._write_component_map(
            {
                "a1111111": {"0": "a1111111-1-0"},
                "b2222222": {"0": "b2222222-2-0"},
            }
        )
        self._register_present_object_with_groups("LOD0.a1111111-1-0", [1, 2, 3, 4, 5])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        carrier = dcm(obj_name="LOD0.a1111111-1-0")
        absent = dcm(obj_name="LOD0.b2222222-2-0")
        ordered = [carrier, absent]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.b2222222-2-0", names)
        self.assertEqual(exporter._zzmi_stub_object_names, ["LOD0.b2222222-2-0"])
        exporter._cleanup_stub_objects()

    def test_absorption_uses_numeric_vertex_group_name_not_blender_index(self):
        """替换模型组名稀疏时，吸收判定必须读取组名而不是内部索引。"""
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        self._register_present_object_with_groups(
            "LOD0.b20f90ea-19182-0",
            [7],
            group_names=["unused", "7"],
        )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.84618ee0-22296-0", names)
        self.assertIn("LOD0.84618ee0-1164-22296", names)
        exporter._cleanup_stub_objects()

    def test_stub_weight_group_uses_registered_slot_when_vgmap_present(self):
        """合并骨架模式：缺部件有 VGMap 时，占位权重组必须是已注册槽（首个 VGMap
        值），而不是局部命名空间的 "0"——组名 = 全局骨骼 id 才能通过导出侧数字组
        检查（对齐 EFMI test_stub_weight_group_uses_registered_slot_when_vgmap_present）。"""
        self._write_vgmap_json("84618ee0-1164-22296", 371)
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        stub = _fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296")
        self.assertIsNotNone(stub)
        self.assertEqual(stub.get("ZZMI_STUB"), 1)
        # 权重组挂已注册槽 371，而不是局部命名空间的 "0"
        self.assertEqual(stub.vertex_groups[0].name, "371")
        self.assertEqual(stub.vertex_groups[0].add_calls[0][1], 1.0)
        exporter._cleanup_stub_objects()

    def test_stub_weight_group_falls_back_to_zero_without_vgmap(self):
        """部件 json 存在但无 VGMap（无反查数据）时占位权重组保持 "0"（旧行为，
        与 EFMI _resolve_stub_registered_slot 的局部命名空间兼容语义一致）。"""
        type_dir = os.path.join(self.tmp, "LOD0", "84618ee0-1164-22296", "TYPE_GPU_TEST_")
        os.makedirs(type_dir, exist_ok=True)
        with open(
            os.path.join(type_dir, "84618ee0-1164-22296.json"), "w", encoding="utf-8"
        ) as f:
            json.dump({}, f)  # 无 VGMap 的部件 json
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        stub = _fake_bpy_data.objects.get("LOD0.84618ee0-1164-22296")
        self.assertIsNotNone(stub)
        self.assertEqual(stub.vertex_groups[0].name, "0")
        exporter._cleanup_stub_objects()

    def test_dedup_excluded_missing_component_still_skips_stub(self):
        """VGMapDedupExcluded=True 的缺失部件即使槽被现存对象引用也按用户意图
        不生成占位——显式排除优先于 absorbed 判定（对齐 EFMI
        test_dedup_excluded_missing_component_still_skips_stub）。"""
        self._write_vgmap_json("84618ee0-22296-0", 7, excluded=True)
        self._write_vgmap_json("84618ee0-1164-22296", 7, excluded=True)
        self._register_present_object_with_groups("LOD0.b20f90ea-19182-0", [7])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn("LOD0.84618ee0-22296-0", names)
        self.assertNotIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])
        exporter._cleanup_stub_objects()

    def test_dedup_excluded_partially_missing_component_skips_stub(self):
        """部分缺失 DrawIB 中，被排除成员同样跳过占位（其余缺失成员照常补占位）。"""
        self._write_vgmap_json("84618ee0-1164-22296", 7, excluded=True)
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.84618ee0-22296-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        # 84618ee0-1164-22296 被排除 -> 不插桩；DrawIB 部分缺失且无其它缺席成员
        self.assertNotIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])
        exporter._cleanup_stub_objects()


class _ZZMIGroup3RedirectFixture:
    """组 3 重定向夹具：合并网格自动重定向（2026-08-25 设计兑现：可挂在任意 DrawIB）。

    逐 pass attach 只在各部件自己的 deform draw 前写入**本部件**骨骼；palette 是
    per-pass 独立上传的 ring scratch，早 pass 时刻读不到晚 pass 部件的当帧骨骼。
    因此挂在早 pass 的合并网格由导出器**自动**把 deform+render 挪到组内最后一个
    deform draw——用户无感，任意 IB 挂载均正确。

    本 mixin 不继承 TestCase：供 ZZSIMergedMeshRedirectTests（重定向行为）与
    ZZMIMultiInstanceLatchRemovalTests（多实例分离 v2 验收）共用。
    """

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()

    def _register_obj(self, name, bone_ids):
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
        return obj

    def _attach_drawcalls(self, submesh, index_count=0):
        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        draw_call = dcm(obj_name=submesh.unique_str)
        draw_call.index_count = index_count
        submesh.drawcall_model_list = [draw_call]
        return submesh

    def _make_exporter(self, models, components):
        exporter = _make_exporter(models, merged_vgmap=True)
        components = _with_channel_plan(components)
        exporter.merged_skeleton_components = components
        exporter.merged_skeleton_component_id_dict = {
            c["draw_ib"]: i for i, c in enumerate(components)
        }
        return exporter

    def _group3_components(self):
        """组 3 实测（按 vg_offset 升序 = _collect_merged_skeleton_components 排序）：
        a23aa8a3(draw 20) b20f90ea(draw 2) b30db54e(draw 8)。"""
        return [
            {
                "draw_ib": "a23aa8a3", "unique_str": "LOD0.a23aa8a3-42759-0",
                "vg_offset": 79, "vg_count": 105, "skeleton_group": 3,
                "vg_map": {i: 79 + i for i in range(105)}, "deform_draw": 20,
            },
            {
                "draw_ib": "b20f90ea", "unique_str": "LOD0.b20f90ea-19182-0",
                "vg_offset": 184, "vg_count": 51, "skeleton_group": 3,
                "vg_map": {i: 184 + i for i in range(51)}, "deform_draw": 2,
            },
            {
                "draw_ib": "b30db54e", "unique_str": "LOD0.b30db54e-7383-0",
                "vg_offset": 235, "vg_count": 14, "skeleton_group": 3,
                "vg_map": {i: 235 + i for i in range(14)}, "deform_draw": 8,
            },
        ]

    def _build_and_apply_plan(self, exporter):
        """构建重定向计划并写回 exporter 字段（模拟 _export_impl 的接线）。"""
        carrier_map, target_map, unredirected = exporter._build_merged_mesh_redirect_plan()
        exporter._redirect_carrier_map = carrier_map
        exporter._redirect_target_map = target_map
        return carrier_map, target_map, unredirected

    def _group3_exporter(self, merged_vertex_count=18776, target_real_vertices=0,
                         target_registered=False, cross_ib=(), extra_bones=()):
        """构造用户实测场景（合并网格挂最早 draw 的 b20f90ea）的 exporter。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [79, 88, 105, 229, 248, *extra_bones])
        if target_registered:
            self._register_obj("LOD0.a23aa8a3-42759-0", [79, 80])
            target_exported_vertices = target_real_vertices
        else:
            target_stub = self._register_obj("LOD0.a23aa8a3-42759-0", [79, 79, 79])
            target_stub["ZZMI_STUB"] = 1
            target_exported_vertices = 3
        sub_b = self._attach_drawcalls(
            _FakeSubmesh(
                "LOD0.b20f90ea-19182-0", 184, 51,
                vertex_count=31015, original_vertex_count=4643,
                exported_vertex_count=merged_vertex_count,
            ),
            index_count=69612,
        )
        sub_a = self._attach_drawcalls(
            _FakeSubmesh(
                "LOD0.a23aa8a3-42759-0", 79, 105,
                exported_vertex_count=target_exported_vertices,
            )
        )
        sub_c = self._attach_drawcalls(
            _FakeSubmesh("LOD0.b30db54e-7383-0", 235, 14)
        )
        models = [
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
            _FakeDrawIBModel("b30db54e", [sub_c]),
        ]
        exporter = self._make_exporter(models, self._group3_components())
        if cross_ib:
            exporter.cross_ib_info_dict = dict(cross_ib)
        return exporter, models

    def _capture_stdout(self, fn):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

class ZZSIMergedMeshRedirectTests(_ZZMIGroup3RedirectFixture, unittest.TestCase):
    """合并网格自动重定向用例（夹具见 _ZZMIGroup3RedirectFixture）。"""

    def test_early_carrier_auto_redirects_to_last_pass(self):
        """用户实测场景：合并网格挂 b20f90ea（draw 2，最早）-> 自动重定向到
        a23aa8a3（draw 20，最后）；target 的 3 个 stub 顶点必须先写入 SO。"""
        exporter, _models = self._group3_exporter()
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)

        self.assertEqual(carrier_map["b20f90ea"]["target"], "a23aa8a3")
        self.assertEqual(carrier_map["b20f90ea"]["base_vertex"], 3)
        self.assertEqual(carrier_map["b20f90ea"]["vertex_count"], 18776)
        self.assertEqual(carrier_map["b20f90ea"]["target_first_index"], 0)
        self.assertEqual(target_map["a23aa8a3"]["so_vertex_count"], 3 + 18776)
        self.assertEqual(target_map["a23aa8a3"]["target_own_vertices"], 3)
        self.assertEqual(target_map["a23aa8a3"]["deform_draws"],
                         [
                             ("Resourceb20f90eaPosition", "Resourceb20f90eaBlend", 18776),
                         ])
        self.assertFalse(target_map["a23aa8a3"]["target_has_real_geometry"])
        self.assertEqual(target_map["a23aa8a3"]["so_owner_ib"], "b20f90ea")
        self.assertEqual(unredirected, {})
        # 已自动重定向 -> 不再报警
        out = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertEqual(out, "")

    def test_redirect_keeps_carrier_with_custom_bone_weights_on_own_ib(self):
        exporter, _models = self._group3_exporter(extra_bones=(251,))
        bone_matrix = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_BoneMatrix", mute=False, armature=object(),
        )
        exporter.blueprint_model.postprocess_nodes = [bone_matrix]

        carrier_map, target_map, _unredirected = self._build_and_apply_plan(exporter)

        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})

        bone_matrix.mute = True
        carrier_map, _, _ = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map["b20f90ea"]["target"], "a23aa8a3")

    def test_merged_on_last_pass_no_redirect(self):
        """合并网格已挂在组内最后一个 deform draw（a23aa8a3，draw 20）：无需重定向。"""
        self._register_obj("LOD0.a23aa8a3-42759-0", [79, 88, 105, 188, 229])
        sub_a = self._attach_drawcalls(_FakeSubmesh("LOD0.a23aa8a3-42759-0", 79, 105))
        sub_b = self._attach_drawcalls(_FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51))
        sub_c = self._attach_drawcalls(_FakeSubmesh("LOD0.b30db54e-7383-0", 235, 14))
        models = [
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("b30db54e", [sub_c]),
        ]
        exporter = self._make_exporter(models, self._group3_components())
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(unredirected, {})

    def test_own_component_only_no_redirect(self):
        """未合并（只引用自己 vg_map 值集合内的骨骼，含共享 canonical）不重定向。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [184, 185, 186])
        sub_b = self._attach_drawcalls(_FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51))
        sub_a = self._attach_drawcalls(_FakeSubmesh("LOD0.a23aa8a3-42759-0", 79, 105))
        sub_c = self._attach_drawcalls(_FakeSubmesh("LOD0.b30db54e-7383-0", 235, 14))
        models = [
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
            _FakeDrawIBModel("b30db54e", [sub_c]),
        ]
        exporter = self._make_exporter(models, self._group3_components())
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(unredirected, {})

    def test_missing_deform_draw_not_redirected(self):
        """反查缓存缺 DeformDrawIndex：无法重定向 -> unredirected 报警。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [79, 105, 229])
        sub_b = self._attach_drawcalls(_FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51))
        sub_a = self._attach_drawcalls(_FakeSubmesh("LOD0.a23aa8a3-42759-0", 79, 105))
        components = [
            {**c, "deform_draw": 0} for c in self._group3_components()
        ]
        models = [
            _FakeDrawIBModel("b20f90ea", [sub_b]),
            _FakeDrawIBModel("a23aa8a3", [sub_a]),
        ]
        exporter = self._make_exporter(models, components)
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(unredirected["b20f90ea"]["reason"], "missing-deform-draw")
        out = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("无法自动修复", out)
        self.assertIn("骨骼合并反查", out)

    def test_cross_ib_carrier_not_redirected(self):
        """跨 IB 配置与自动重定向暂不兼容 -> unredirected 报警。"""
        exporter, _models = self._group3_exporter(
            cross_ib={("b20f90ea_0",): ["a23aa8a3_0"]}
        )
        # cross_ib_info_dict 键是 ib_key（hash_firstindex），这里直接标记 DrawIB 为源
        exporter.cross_ib_info_dict = {"b20f90ea_0": ["a23aa8a3_0"]}
        carrier_map, _target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(unredirected["b20f90ea"]["reason"], "cross-ib")

    def test_target_with_own_geometry_keeps_native_sos(self):
        """target 自身可见时，各 DrawIB 保留自己的 SO 和零顶点基址。"""
        exporter, _models = self._group3_exporter(
            merged_vertex_count=18776, target_real_vertices=12314,
            target_registered=True,
        )
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(unredirected, {})

    def test_redirect_vb_sections(self):
        """carrier 的 deform 保留 3 顶点 stub，合并几何由 target 挂点的每槽守卫重放；
        target 不捕获 SO（纯占位 target 的 SO owner 是第一个 carrier）。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        builder_b = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_b, models[0])
        text_b = "\n".join(builder_b.sections[0].SectionLineList)
        # carrier（b20f90ea，组件 C1）：按槽 copy palette + 顶层 attach + 3 顶点前缀 stub。
        # v9：**每个布局兼容的组内部件挂点都发同一套每槽守卫** —— 守卫的触发时机可能
        # 落在组内任意部件的 deform 段（取决于引擎提交次序），因此 carrier 段同样要发
        # 守卫并重放合并几何（曾在"只让 target 单挂点持有守卫"时整段消失）。
        self.assertIn("ResourceZZPalette_b20f90ea_s1 = copy vs-t0 unless_null", text_b)
        self.assertIn("ResourceZZPalette_b20f90ea_s2 = copy vs-t0 unless_null", text_b)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C1_s1", text_b)
        self.assertIn("draw = 3, 0", text_b)
        self.assertIn("draw = 18776, 0", text_b)
        self.assertIn("so0 = ref ResourceZZRedirectSO_G3_s1", text_b)
        # SO owner = carrier：两个槽各捕获一次
        self.assertIn("ResourceZZRedirectSO_G3_s1 = ref so0", text_b)
        self.assertIn("ResourceZZRedirectSO_G3_s2 = ref so0", text_b)
        self.assertNotIn("$zz_ms_redirect_drawn", text_b)
        self.assertNotIn("$zz_ms_group_ready", text_b)
        self.assertNotIn("$zz_ms_group_phase", text_b)

        builder_a = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_a, models[1])
        text_a = "\n".join(builder_a.sections[0].SectionLineList)
        # target（a23aa8a3，纯占位）：attach C0；每槽守卫用 carrier 的 vb0/vb2 重放。
        # 纯占位 target 的 SO owner 是 carrier，因此 target 段不捕获 SO。
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C0_s1", text_a)
        self.assertIn("vb2 = Resourceb20f90eaBlend", text_a)
        self.assertIn("vb0 = Resourceb20f90eaPosition", text_a)
        self.assertIn("draw = 18776, 0", text_a)
        self.assertIn("so0 = ref ResourceZZRedirectSO_G3_s1", text_a)
        self.assertIn("so0 = ref ResourceZZRedirectSO_G3_s2", text_a)
        self.assertNotIn("ResourceZZRedirectSO_G3_s1 = ref so0", text_a)

        # 同组的第三个部件 b30db54e（布局兼容）：v9 起同样发每槽守卫（触发时机可能
        # 落在它的 deform 段；重复重放同槽是幂等写入），绑定的是 carrier 的 vb0/vb2、
        # 写的是同一槽 SO 引用。
        builder_c = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_c, models[2])
        text_c = "\n".join(builder_c.sections[0].SectionLineList)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C2_s1", text_c)
        self.assertIn("draw = 18776, 0", text_c)
        self.assertIn("vb0 = Resourceb20f90eaPosition", text_c)
        self.assertIn('if ($zz_ms_seen_11 >= 1) && ($zz_ms_prev_01 == 0 || $zz_ms_seen_01 >= 1) && ($zz_ms_prev_21 == 0 || $zz_ms_seen_21 >= 1)', text_c)
        self.assertIn('if ($zz_ms_seen_12 >= 1) && ($zz_ms_prev_02 == 0 || $zz_ms_seen_02 >= 1) && ($zz_ms_prev_22 == 0 || $zz_ms_seen_22 >= 1)', text_c)

    def test_redirect_draw_waits_for_dependencies_in_both_frame_orders(self):
        """回归 2026-08-26 实测：target 可能在 carrier 前或后到达；两种
        顺序都只能在最后一个依赖 palette attach 后绘制，不能读半成品骨架。"""
        exporter, _models = self._group3_exporter()
        _carrier_map, target_map, _unredirected = self._build_and_apply_plan(exporter)
        required = set(target_map["a23aa8a3"]["required_component_ids"])

        def first_ready_draw(draw_ib_order):
            seen = set()
            for draw_ib in draw_ib_order:
                seen.add(exporter.merged_skeleton_component_id_dict[draw_ib])
                if required <= seen:
                    return draw_ib
            return None

        # target 后到：在 target 挂点绘制；target 先到：延后到最后一个 carrier。
        self.assertEqual(
            first_ready_draw(["b20f90ea", "b30db54e", "a23aa8a3"]),
            "a23aa8a3",
        )
        self.assertEqual(
            first_ready_draw(["a23aa8a3", "b30db54e", "b20f90ea"]),
            "b20f90ea",
        )

    def test_redirect_dependencies_omit_stub_target_when_not_referenced(self):
        """纯占位 target 且 carrier 未引用其骨骼时，不应阻塞兼容 carrier。"""
        exporter, _models = self._group3_exporter()
        # b20f90ea 的合并几何改为引用自身 + b30db54e，故旧实现不会把
        # a23aa8a3(target) 加入 required_component_ids。
        self._register_obj("LOD0.b20f90ea-19182-0", [184, 235])
        _carrier_map, target_map, _unredirected = self._build_and_apply_plan(exporter)
        required = set(target_map["a23aa8a3"]["required_component_ids"])
        target_component_id = exporter.merged_skeleton_component_id_dict["a23aa8a3"]
        self.assertNotIn(target_component_id, required)

    def test_redirect_does_not_use_incompatible_stub_target_as_host(self):
        """BI4 的占位 target 即使依赖齐全，也不能执行 BI16 carrier 重放。"""
        exporter, models = self._group3_exporter()
        models[1].d3d11GameType.CategoryStrideDict["Blend"] = 4
        _carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)

        target_component_id = exporter.merged_skeleton_component_id_dict["a23aa8a3"]
        carrier_component_id = exporter.merged_skeleton_component_id_dict["b20f90ea"]
        self.assertNotIn(target_component_id, target_map["a23aa8a3"]["compatible_component_ids"])
        self.assertIn(carrier_component_id, target_map["a23aa8a3"]["compatible_component_ids"])
        self.assertEqual(
            unredirected["b20f90ea"]["reason"],
            "required-dependency-after-compatible-host",
        )

        warning = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("必需骨骼依赖到达晚于所有兼容重放宿主", warning)

        builder_target = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_target, models[1])
        text_target = "\n".join(builder_target.sections[0].SectionLineList)
        self.assertNotIn("ResourceZZRedirectSO_G3_s1 = ref so0", text_target)
        # 不兼容宿主不产生 **draw 版** 重放守卫（也不再有相位/闩锁条件）；
        # 但它仍然发 **蒙皮 CS 发布块**（2026-09-17「闪」修复）：draw 版重放受 IA
        # 输入布局限制，窄布局必需部件排在最后到达时没有任何锚点能落笔 →
        # 该槽 SO 整帧不写（FrameAnalysis-025058 实证：SO 与完整骨架仅 ~3100/13671
        # 行吻合）= 用户实测「一个实例/两个实例都在闪」。CS 绕开 IA 布局，
        # 因此**任意必需部件都能发布**。
        self.assertNotIn("$zz_ms_group_phase", text_target)
        self.assertNotIn("$zz_ms_redirect_drawn", text_target)
        self.assertNotIn("$zz_ms_group_ready", text_target)
        self.assertIn("run = CustomShaderZZMISkin_G3_s1", text_target)
        self.assertIn("run = CustomShaderZZMISkin_G3_s2", text_target)
        self.assertIn("so0 = null", text_target)

        # carrier（b20f90ea）是唯一兼容的重放宿主：它自己承载直连守卫重放合并
        # 几何（同一套槽门控），绝不出现"两个挂点都不画"把合并几何整个丢掉。
        builder_carrier = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_carrier, models[0])
        text_carrier = "\n".join(builder_carrier.sections[0].SectionLineList)
        self.assertIn("ResourceZZRedirectSO_G3_s1 = ref so0", text_carrier)
        self.assertIn('if ($zz_ms_seen_11 >= 1) && ($zz_ms_prev_01 == 0 || $zz_ms_seen_01 >= 1)', text_carrier)
        self.assertIn("draw = 18776, 0", text_carrier)
        # 回退路径**必须**写 3 顶点前缀 stub：base_vertex 由
        # `_redirect_plan_prefix_rows`（纯占位 target = 3）决定，渲染从 SO 第 3 行
        # 读本段；漏写就整体错位 3 行（2026-09-16 复核修正，旧实现此处漏写）。
        self.assertIn("draw = 3, 0", text_carrier)

    def test_redirect_render_draw_is_unconditional_per_instance(self):
        """渲染段不再有帧闩锁：每个实例的渲染 draw 各画一次本实例的 SO。

        2026-09 多实例分离 v2（用户实测通过）：旧实现用
        `if $zz_ms_redirect_drawn_<target> == 1` 包住 drawindexed，同一帧只有第一个
        实例能画；且 vb0 被覆写成同一个 SO 资源变量，把两个实例钉在一起。现在
        渲染侧保留游戏原生 per-instance vb0（本实例 deform SO），drawindexed
        无条件执行。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, models[0])
        lines = builder.sections[0].SectionLineList
        text = "\n".join(lines)
        draw_lines = [line for line in lines if line.strip().startswith("drawindexed = ")]
        self.assertEqual(len(draw_lines), 1)
        self.assertFalse(draw_lines[0].startswith("    "), "drawindexed 不得被 if 包住")
        self.assertIn("drawindexed = 69612,0,3", text)
        self.assertNotIn("$zz_ms_redirect_drawn", text)
        self.assertNotIn("$zz_ms_group_ready", text)
        self.assertNotIn("vb0 = ResourceZZRedirectSO", text)
        # C10 收紧：原 `assertNotIn("endif", text)` 过宽（会连带禁止物体切换等
        # 无关条件块）。改为**只**断言 drawindexed 行本身无条件门控。
        draw_index = lines.index(draw_lines[0])
        preceding = [ln for ln in lines[:draw_index] if ln.strip()]
        self.assertTrue(preceding, "drawindexed 前必须有绑定内容")
        self.assertNotEqual(
            preceding[-1].strip(),
            "endif",
            f"drawindexed 不得被 endif 紧跟包住: {preceding[-1]!r}",
        )
        self.assertFalse(
            preceding[-1].strip().startswith(("if ", "$")),
            f"drawindexed 不得被条件块包住: {preceding[-1]!r}",
        )

    def test_real_target_with_incompatible_blend_layout_is_not_redirected(self):
        """真实 target 的 Blend 布局不是组内多数布局时不能重定向。

        2026-09-16 收紧：锚点集合改为"组内多数布局"，因此这类组合以
        `target-layout-not-anchor` 显式拒绝（旧口径报 `incompatible-blend-layout`）。
        前缀行必须与合并行在同一段写出，target 自身不在锚点集合时无法保证顺序。
        """
        exporter, models = self._group3_exporter(
            target_real_vertices=12314,
            target_registered=True,
        )
        models[1].d3d11GameType.CategoryStrideDict["Blend"] = 4

        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)

        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(
            unredirected["b20f90ea"]["reason"],
            "target-layout-not-anchor",
        )
        warning = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("不是组内多数布局", warning)

    def test_missing_blend_layout_is_not_assumed_compatible(self):
        """布局元数据缺失时必须显式拒绝，不能让换角色后的未知格式静默重放。"""
        exporter, models = self._group3_exporter()
        models[0].d3d11GameType.CategoryStrideDict.pop("Blend")

        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)

        self.assertEqual(carrier_map, {})
        self.assertEqual(target_map, {})
        self.assertEqual(
            unredirected["b20f90ea"]["reason"],
            "missing-blend-layout",
        )
        warning = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("缺少可验证的 Blend 输入布局", warning)

    def test_redirect_ib_sections(self):
        """carrier/target 各自保留 render 身份；carrier 只换绑合并 SO，target
        的占位 IB 仍然输出，避免共享 hash 导致物体串扰或被静默跳过。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        text = "\n".join(
            line for section in builder.sections for line in section.SectionLineList
        )

        # carrier 的 render override：hash/first_index 仍是 b20f90ea，索引和 mesh
        # 备注仍属于 carrier；vb0 **不再覆写**（游戏原生 per-instance deform SO），
        # drawindexed 无条件执行（每个实例各画一次，无帧闩锁）。
        self.assertIn("[TextureOverride_LOD0.b20f90ea_19182_0]", text)
        self.assertIn("hash = b20f90ea", text)
        self.assertNotIn("vb0 = ResourceZZRedirectSO", text)
        self.assertIn("ib = Resource_LOD0.b20f90ea_19182_0_Index", text)
        self.assertIn("vb1 = ResourceZZRedirectTexcoord_a23aa8a3_b20f90ea_3", text)
        self.assertIn("drawindexed = 69612,0,3", text)
        self.assertIn("; [mesh:LOD0.b20f90ea-19182-0]", text)
        self.assertNotIn("$zz_ms_redirect_drawn", text)
        # carrier 的原 render draw 被 IB 级 skip 抑制
        self.assertIn("[TextureOverride_IB_b20f90ea]", text)
        # target 的 stub 子网格保留自己的 hash/IB；占位三角由导出阶段写入。
        self.assertIn("[TextureOverride_LOD0.a23aa8a3_42759_0]", text)
        self.assertIn("hash = a23aa8a3", text)
        self.assertIn("ib = Resource_LOD0.a23aa8a3_42759_0_Index", text)
        self.assertNotIn("ib = null", text)

    def test_redirect_texcoord_payload_matches_so_base_vertex(self):
        """carrier 的 UV 前缀必须与 RedirectSO 的 base_vertex 完全相同。"""
        submesh = self._attach_drawcalls(
            _FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51, exported_vertex_count=5)
        )
        model = _FakeDrawIBModel("b20f90ea", [submesh])
        source_bytes = bytes(range(5 * 20))
        model.category_buffer_dict["Texcoord"] = source_bytes
        exporter = self._make_exporter([model], self._group3_components())

        payload, stride = exporter._build_redirect_texcoord_payload(
            "b20f90ea",
            {"target": "a23aa8a3", "base_vertex": 3, "vertex_count": 5},
        )

        self.assertEqual(stride, 20)
        self.assertEqual(payload[: 3 * stride], b"\x00" * (3 * stride))
        self.assertEqual(payload[3 * stride :], source_bytes)

    def test_redirect_texcoord_resource_is_declared_and_written(self):
        submesh = self._attach_drawcalls(
            _FakeSubmesh("LOD0.b20f90ea-19182-0", 184, 51, exported_vertex_count=5)
        )
        model = _FakeDrawIBModel("b20f90ea", [submesh])
        source_bytes = bytes(range(5 * 20))
        model.category_buffer_dict["Texcoord"] = source_bytes
        exporter = self._make_exporter([model], self._group3_components())
        exporter._redirect_carrier_map = {
            "b20f90ea": {
                "target": "a23aa8a3",
                "base_vertex": 3,
                "vertex_count": 5,
            }
        }
        exporter._redirect_target_map = {
            "a23aa8a3": {"so_stride": 40}
        }

        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        text = "\n".join(
            line for section in builder.sections for line in section.SectionLineList
        )
        filename = "zz_redirect_texcoord_a23aa8a3_b20f90ea_3.buf"
        self.assertIn(
            "[ResourceZZRedirectTexcoord_a23aa8a3_b20f90ea_3]", text
        )
        self.assertIn("stride = 20", text)
        self.assertIn(f"filename = Meshes/{filename}", text)
        payload = (Path(_FAKE_MOD_FOLDER) / "Meshes" / filename).read_bytes()
        self.assertEqual(payload, (b"\x00" * (3 * 20)) + source_bytes)

    def test_redirect_keeps_each_submesh_first_index(self):
        """同一 DrawIB 的多个子网格不能共用 target 首索引，否则会再次串台。"""
        exporter, models = self._group3_exporter()
        second_target = self._attach_drawcalls(
            _FakeSubmesh(
                "LOD0.a23aa8a3-288-42759",
                79,
                105,
                match_first_index=42759,
            )
        )
        models[1].submesh_model_list.append(second_target)
        models[1].submesh_ib_dict[second_target.unique_str] = b"\x00\x00\x00\x00"
        self._build_and_apply_plan(exporter)

        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, models[1])
        text = "\n".join(builder.sections[0].SectionLineList)
        self.assertIn("[TextureOverride_LOD0.a23aa8a3_288_42759]", text)
        self.assertIn("hash = a23aa8a3\nmatch_first_index = 42759", text)
        self.assertIn("ib = Resource_LOD0.a23aa8a3_288_42759_Index", text)

    def test_merged_skeleton_refuses_empty_index_buffer(self):
        """合并骨架下不能退回 ib=null；缺失占位索引必须让导出显式失败。"""
        exporter, models = self._group3_exporter()
        exporter.has_merged_skeleton = True
        models[1].submesh_ib_dict["LOD0.a23aa8a3-42759-0"] = b""

        with self.assertRaisesRegex(RuntimeError, "禁止以 ib=null/IB skip"):
            exporter.add_unity_vs_texture_override_ib_sections(
                _FakeIniBuilder(), models[1]
            )

    def test_redirect_vlr_section(self):
        """VertexLimitRaise：纯占位 target 的 SO 由 carrier 拥有并声明总容量。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        builder_b = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vlr_section(builder_b, models[0])
        text_b = "\n".join(builder_b.sections[0].SectionLineList)
        self.assertIn("override_vertex_count = 18779", text_b)

        builder_a = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vlr_section(builder_a, models[1])
        text_a = "\n".join(builder_a.sections[0].SectionLineList)
        self.assertIn("override_vertex_count = 18779", text_a)


class ZZSIMergedMeshRenderRebindTests(unittest.TestCase):
    """合并网格渲染换绑：导出顶点数超过原部件顶点数时，渲染 draw 必须把 vb1
    换绑为本 mod 的 Texcoord buffer（游戏原 vb1 只覆盖原部件顶点数，合并网格
    索引会越界读 -> UV 糊到 (0,0) 角落）。"""

    def _render_override_text(self, submesh, draw_ib="b20f90ea"):
        model = _FakeDrawIBModel(draw_ib, [submesh])
        exporter = _make_exporter([model], merged_vgmap=True)
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        return "\n".join(builder.sections[0].SectionLineList)

    def test_oversized_mesh_binds_vb1(self):
        submesh = _FakeSubmesh(
            "LOD0.b20f90ea-19182-0", 184, 51,
            vertex_count=31015, original_vertex_count=4643,
        )
        text = self._render_override_text(submesh)
        self.assertIn("ib = Resource_LOD0.b20f90ea_19182_0_Index", text)
        self.assertIn("vb1 = Resourceb20f90eaTexcoord", text)
        self.assertLess(
            text.index("ib = "), text.index("vb1 = Resourceb20f90eaTexcoord")
        )

    def test_same_size_mesh_keeps_game_vb1(self):
        submesh = _FakeSubmesh(
            "LOD0.b20f90ea-19182-0", 184, 51,
            vertex_count=4643, original_vertex_count=4643,
        )
        text = self._render_override_text(submesh)
        self.assertNotIn("vb1 = Resource", text)

    def test_stub_smaller_than_original_keeps_game_vb1(self):
        submesh = _FakeSubmesh(
            "LOD0.a23aa8a3-42759-0", 79, 105,
            vertex_count=3, original_vertex_count=12314,
        )
        text = self._render_override_text(submesh, draw_ib="a23aa8a3")
        self.assertNotIn("vb1 = Resource", text)


class ZZMIMultiInstanceLatchRemovalTests(_ZZMIGroup3RedirectFixture, unittest.TestCase):
    """验收：生成的 ini 与实测通过的 v9 手修版 `浮波柚叶.ini` 语义一致。

    手修版（K:\\SSMT-Package-master\\...\\浮波柚叶\\浮波柚叶.ini，用户游戏内实测通过）
    的 v9 语义：
    1. 每个部件 deform 段**顶层**自增出现次 `$zz_ms_occ_<i>`，`>= 3` 回绕为 1；
    2. 到达标记 `$zz_ms_seen_<i><k>` 全部**顶层 sticky 累加**（绝不在 if 内赋值）；
    3. 用 `if occ == 1 ... else ... endif` 按槽捕获 palette；SO 捕获只由 owner 做；
    4. 所有 attach `run` 都在段**顶层**无条件执行（if 内的 run 不执行 → 骨架为空）；
    5. 骨架按槽分份 `_s1`/`_s2`，SO 资源按槽 `ResourceZZRedirectSO_s<k>`；
    6. 每槽守卫条件 = 组内全部部件的 seen 相与；守卫体内**只有绑定与 draw**
       （不得出现 run、不得给 $变量赋值）；
    7. `[Constants]` 只声明 occ/seen，`[Present]` 只把它们清零——无任何闩锁变量、
       无任何资源复位（F8 已回退）。
    """

    def _all_sections_text(self, exporter, models):
        # carrier 的 Redirect Texcoord 需要真实导出 buffer（长度 = 合并顶点数 * stride）
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next(
                (m for m in models if m.draw_ib == carrier_ib), None
            )
            if carrier_model is None:
                continue
            stride = int(
                (getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {}).get(
                    "Texcoord", 0
                )
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get("Texcoord"):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )

        vb_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        vb_text = "\n".join(_all_builder_lines(vb_builder))

        ib_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_ib_sections(ib_builder, model)
        ib_text = "\n".join(_all_builder_lines(ib_builder))

        skeleton_builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(skeleton_builder)
        skeleton_text = "\n".join(_all_builder_lines(skeleton_builder))
        return vb_text, ib_text, skeleton_text

    def test_generated_sections_match_hand_fixed_v9_semantics(self):
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        vb_text, ib_text, skeleton_text = self._all_sections_text(exporter, models)

        # 1) 出现次顶层自增 + 回绕为槽位 1（组 3 有 3 个部件）
        for cid in range(3):
            self.assertIn(f"$zz_ms_occ_{cid} = $zz_ms_occ_{cid} + 1", vb_text)
            self.assertIn(f"if $zz_ms_occ_{cid} >= 3", vb_text)
            self.assertIn(f"    $zz_ms_occ_{cid} = 1", vb_text)
        # 2) 到达标记顶层 sticky 累加
        for cid in range(3):
            for slot in (1, 2):
                self.assertIn(
                    f"$zz_ms_seen_{cid}{slot} = $zz_ms_seen_{cid}{slot}"
                    f" + ($zz_ms_occ_{cid} == {slot})",
                    vb_text,
                )
        # 3) 按槽捕获 palette；SO 捕获只在 owner（carrier b20f90ea）段
        self.assertIn("if $zz_ms_occ_1 == 1", vb_text)
        self.assertIn(
            "    ResourceZZPalette_b20f90ea_s1 = copy vs-t0 unless_null", vb_text
        )
        self.assertIn(
            "    ResourceZZPalette_b20f90ea_s2 = copy vs-t0 unless_null", vb_text
        )
        self.assertNotIn("ResourceZZPalette_a23aa8a3_s1 = copy vs-t0 unless_null\n    ResourceZZRedirectSO", vb_text)
        # 4) 每槽守卫条件 =（SO 别名当帧已由捕获者刷新 b20f90ea=comp1）&&
        #    （必需部件「上一帧本槽没到」或「本帧本槽已到」相与）。
        #    **契约变更（2026-09-17，见 reports/zzmi-fix/05-implementation-notes.md）**：
        #    消费点谓词由 `seen == 1` 改为 `seen >= 1`——`seen` 是帧内单调累加的顶层
        #    计数，`== 1` 在同帧出现 ≥3 次（occ 回绕 ⇒ seen 到 2）时恒假 ⇒ 守卫集体
        #    关闭（P-13）。断言强度不变（仍是逐字符的整条守卫），只随语义更新。
        for slot in (1, 2):
            # t149：捕获者 comp 1 自己的 cull_aware 子句 `(prev_1{slot} == 0 ||
            # seen_1{slot} >= 1)` 被布尔吸收删除（SO 就绪门已断言 `seen_1{slot} >= 1`）；
            # 其余部件的豁免项逐字符不变。
            self.assertIn(
                f"if ($zz_ms_seen_1{slot} >= 1) && ($zz_ms_prev_0{slot} == 0 || $zz_ms_seen_0{slot} >= 1)"
                f" && ($zz_ms_prev_2{slot} == 0 || $zz_ms_seen_2{slot} >= 1)",
                vb_text,
            )
        # 5) attach run 全在顶层（含全部 部件 × 槽）
        run_entries = []
        depth = 0
        for line in vb_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("if "):
                depth += 1
            elif stripped == "endif":
                depth -= 1
            elif stripped.startswith("run = CustomShaderZZMIMergedSkeletonAttach_"):
                run_entries.append((depth, stripped))
        self.assertTrue(run_entries)
        for entry_depth, entry in run_entries:
            self.assertEqual(entry_depth, 0, f"attach run 进了 if: {entry}")
        self.assertEqual(
            sorted({entry.split()[-1] for _d, entry in run_entries}),
            sorted(
                f"CustomShaderZZMIMergedSkeletonAttach_C{cid}_s{slot}"
                for cid in range(3)
                for slot in (1, 2)
            ),
        )
        # 6) 守卫体内只有绑定与 draw
        for slot in (1, 2):
            lines = vb_text.splitlines()
            start = next(
                i for i, line in enumerate(lines)
                if f"($zz_ms_prev_01 == 0" in line
            )
            end = lines.index("endif", start)
            body = lines[start + 1 : end]
            self.assertTrue(body)
            for line in body:
                stripped = line.strip()
                self.assertFalse(stripped.startswith("run "), line)
                self.assertFalse(stripped.startswith("$"), line)
                self.assertTrue(
                    stripped.startswith(("vs-t0 =", "so0 =", "vb0 =", "vb2 =", "draw =")),
                    line,
                )
        self.assertIn("    so0 = null", vb_text)
        # 骨架/资源按槽分份
        self.assertIn("[ResourceZZMergedSkeleton_G3_s1]", skeleton_text)
        self.assertIn("[ResourceZZMergedSkeleton_G3_s2]", skeleton_text)
        self.assertIn("[ResourceZZRedirectSO_G3_s1]", skeleton_text)
        self.assertIn("[ResourceZZRedirectSO_G3_s2]", skeleton_text)
        # 7) 渲染段：无 vb0 覆写、无 if 包装、drawindexed 无条件
        self.assertNotIn("vb0 = ResourceZZRedirectSO", ib_text)
        self.assertIn("drawindexed = 69612,0,3", ib_text)
        # C10 收紧：原 `assertNotIn("endif", ib_text)` 过宽。改为**只**定位
        # drawindexed 行、断言其前一非空行不是 if / $变量 / endif（即该 draw
        # 本身无条件门控），不整段禁 endif（避免无关条件块造成假失败）。
        ib_lines = ib_text.splitlines()
        ib_draw_indexes = [
            i for i, ln in enumerate(ib_lines)
            if ln.strip().startswith("drawindexed = 69612,0,3")
        ]
        self.assertEqual(len(ib_draw_indexes), 1, "IB 段应恰好一条目标 drawindexed")
        ib_preceding = [ln for ln in ib_lines[: ib_draw_indexes[0]] if ln.strip()]
        self.assertTrue(ib_preceding, "drawindexed 前必须有绑定内容")
        self.assertNotEqual(
            ib_preceding[-1].strip(),
            "endif",
            f"drawindexed 不得被 endif 紧跟包住: {ib_preceding[-1]!r}",
        )
        self.assertFalse(
            ib_preceding[-1].strip().startswith(("if ", "$")),
            f"drawindexed 不得被条件块包住: {ib_preceding[-1]!r}",
        )
        # 8) Constants / [Present]：只声明/清零 occ 与 seen；无闩锁、无资源复位
        constants_text, present_text = (
            skeleton_text.split("[Present]")[0],
            skeleton_text.split("[Present]")[1],
        )
        for forbidden in (
            "$zz_ms_redirect_drawn_",
            "$zz_ms_group_ready_",
            "$zz_ms_group_phase_",
        ):
            self.assertNotIn(forbidden, constants_text)
            self.assertNotIn(forbidden, present_text)
        for cid in range(3):
            self.assertIn(f"global $zz_ms_occ_{cid} = 0", constants_text)
            self.assertIn(f"$zz_ms_occ_{cid} = 0", present_text)
            for slot in (1, 2):
                self.assertIn(f"global $zz_ms_seen_{cid}{slot} = 0", constants_text)
                self.assertIn(f"$zz_ms_seen_{cid}{slot} = 0", present_text)
        # 9) F8 已回退（2026-09 实测：Present 写资源 null 会废掉 [Present] 清场，
        #    导致实例加入/剔除过渡帧错槽重放 → 闪烁卡死）：RedirectSO 资源声明仍在，
        #    但 [Present] 不得再出现任何 RedirectSO 复位语句
        self.assertIn("[ResourceZZRedirectSO_G3_s1]", skeleton_text)
        self.assertNotIn("ResourceZZRedirectSO", present_text)
        self.assertNotIn("ResourceZZPalette", present_text)
        self.assertNotIn("ResourceZZMergedSkeleton", present_text)

    def test_two_slots_are_written_independently(self):
        """两个槽互不覆盖：每个部件每次经过写**一个**槽，另一个槽保留上一实例内容。

        一轮 = 本条 deform pass 依次 attach 组内部件（occ +1 → 只落一个槽），
        组齐时该槽守卫重放一次；下一轮（下一个实例）occ 回绕到另一个槽。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        vb_text, _ib_text, _skeleton_text = self._all_sections_text(exporter, models)

        # v9：每个布局兼容的组内部件挂点都发同一套每槽守卫（触发时机可能落在任意
        # 部件段），故合并 draw 按"槽 × 发守卫的部件段"成套出现（每段每槽恰好一次）。
        draw_count = vb_text.count("    draw = 18776, 0")
        self.assertGreaterEqual(draw_count, 2)
        self.assertEqual(draw_count % 2, 0)
        for slot_cond in (
            'if ($zz_ms_seen_11 >= 1) && ($zz_ms_prev_01 == 0 || $zz_ms_seen_01 >= 1) && ($zz_ms_prev_21 == 0 || $zz_ms_seen_21 >= 1)',
            'if ($zz_ms_seen_12 >= 1) && ($zz_ms_prev_02 == 0 || $zz_ms_seen_02 >= 1) && ($zz_ms_prev_22 == 0 || $zz_ms_seen_22 >= 1)',
        ):
            self.assertIn(slot_cond, vb_text)
        # SO 引用按槽分别捕获（carrier 段），target 段按槽分别绑定
        group_b = next(m for m in models if m.draw_ib == "b20f90ea")
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, group_b)
        b_text = "\n".join(_all_builder_lines(builder))
        self.assertIn("ResourceZZRedirectSO_G3_s1 = ref so0", b_text)
        self.assertIn("ResourceZZRedirectSO_G3_s2 = ref so0", b_text)
        # 回归：**载体段也必须发守卫**（只让单挂点持有守卫时，该挂点先 deform 的帧
        # 里守卫永不触发 → 该槽 SO 只剩 3 顶点前缀 → 合并几何整段消失）
        self.assertIn(
            'if ($zz_ms_seen_11 >= 1) && ($zz_ms_prev_01 == 0 || $zz_ms_seen_01 >= 1) && ($zz_ms_prev_21 == 0 || $zz_ms_seen_21 >= 1)',
            b_text,
        )
        self.assertIn("    draw = 18776, 0", b_text)
        self.assertIn('if ($zz_ms_seen_12 >= 1) && ($zz_ms_prev_02 == 0 || $zz_ms_seen_02 >= 1) && ($zz_ms_prev_22 == 0 || $zz_ms_seen_22 >= 1)', b_text)

    def test_pose_key_alignment_uses_shared_canonical_bone(self):
        """双实例对齐（2026-09-17「动画混在一起」修复）：出现次只是位置标签。

        引擎按 mesh+instance 排序提交 deform，两个实例的相对先后**可以逐部件不同**
        （实测 033520 G2：A 的顺序 c209c22b→3b1b73fe→869976a3→4a178546，
        B 是 3b1b73fe→c209c22b→…）⇒「第 1 次出现」对某些部件是 A、对另一些是 B，
        同一槽骨架混进两份姿态 = 用户看到的「两边混在一起、没按实例分开」。
        修复口径：全组共享 canonical 骨骼（真实导出 = 槽位 0）的当帧矩阵位置当指纹，
        同实例同帧逐位相同 ⇒ 同槽；不同实例姿态不同 ⇒ 不同槽。
        """
        exporter, models = self._group3_exporter()
        # 先按夹具（无共享骨骼）建计划：Texcoord/VLR 等产物与 vg_map 无关，
        # 之后再改 vg_map（真实形态：槽位 0 根骨被去重合并 → 三个部件都引用它），
        # 并按**整组**重算通道计划（t75：通道骨是共享骨连通分量的性质）。
        _carrier_map, target_map, _unredirected = self._build_and_apply_plan(exporter)
        for component in exporter.merged_skeleton_components:
            vg_map = dict(component["vg_map"])
            vg_map[min(vg_map)] = 0
            component["vg_map"] = vg_map
            component.pop("channel", None)
        _with_channel_plan(exporter.merged_skeleton_components)
        target_map["a23aa8a3"]["pose_anchor_slot"] = 0

        self.assertEqual(target_map["a23aa8a3"]["pose_anchor_slot"], 0)

        carrier = next(m for m in models if m.draw_ib == "b20f90ea")
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, carrier)
        text = "\n".join(builder.sections[0].SectionLineList)
        self.assertIn("ResourceZZPoseKeySrc = ref vs-t0", text)
        # t75：键 = 通道骨的 **48 字节矩阵**整块哈希（逐字节精确，无容差）。
        # 本部件（b20f90ea）映射到共享槽 79 的本地骨是 0 ⇒ HashRegion(0, 48)。
        self.assertIn("->HashRegion(0, 48)", text)
        self.assertNotIn("SpatialHash", text)
        # 守卫必须 > 0：HashRegion 失败返回 -1/-2/-3，`!= 0` 挡不住
        self.assertIn("if $zz_ms_pose_key_3 > 0", text)
        self.assertNotIn("if $zz_ms_pose_key_3 != 0", text)
        self.assertIn("$PoolZZMISlotOfKey_G3[$zz_ms_pose_key_3] = 1", text)
        self.assertIn("$PoolZZMIG_Taken_G3[1] = 1", text)
        # t75：不再有"出现次 ↔ 键"互搬（emit_move）；捕获直接写在键算出的槽上
        self.assertNotIn(
            "ResourceZZPalette_b20f90ea_s1 = copy ResourceZZPalette_b20f90ea_s2", text
        )
        self.assertIn(
            "ResourceZZPalette_b20f90ea_s1 = copy vs-t0 unless_null", text
        )
        self.assertIn("ResourceZZRedirectSO_G3_s1 = ref so0", text)

    def test_pose_alignment_skipped_without_shared_bone_is_diagnosed(self):
        """**契约变更 C-2 / D3**（captain 裁定 `reports/zzmi-fix/02-captain-decisions.md` §2/§3）。

        旧契约（本测试原名 `test_pose_alignment_skipped_without_shared_bone`）：
        组内没有共享骨骼（vg_map 值无交集）时**静默**不发指纹块——「静默」本身被
        固化成契约。
        新契约：同一行为（不发指纹块）**必须留下导出期显式诊断**，两条不可达路径
        都不得静默：无共享锚点 → `reason=no_shared_canonical_bone`；
        有锚点但没有重定向计划 → `reason=no_redirect_plan`（用例见
        `ZZMIMergedOrderingContractTests::test_pose_alignment_unavailable_is_declared_not_silent`）。
        断言方向不变（仍**不得**出现 SpatialHash / pose_key），只是把「静默」正名为
        「已诊断」——不是放宽，是补上缺失的可观测性。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        # t75：手工把 b20f90ea 的通道记录清掉 = 模拟"无通道部件"（缓存缺失且
        # 组级补算也没能给出通道骨）。
        for component in exporter.merged_skeleton_components:
            if component["draw_ib"] == "b20f90ea":
                component["channel"] = {}
        carrier = next(m for m in models if m.draw_ib == "b20f90ea")
        builder = _FakeIniBuilder()
        captured = self._capture_stdout(
            lambda: exporter.add_unity_vs_texture_override_vb_sections(builder, carrier)
        )
        text = "\n".join(builder.sections[0].SectionLineList)
        # 无通道部件：不发键块（也不再有 SpatialHash 这种量化哈希）
        self.assertNotIn("SpatialHash", text)
        self.assertNotIn("$zz_ms_pose_key_", text)
        # 但必须落显式诊断（绝不静默）
        self.assertIn("; ZZMI-MERGE-DIAG POSE_ALIGNMENT_UNAVAILABLE", text)
        self.assertIn("reason=no_channel_plan", text)
        self.assertIn("POSE_ALIGNMENT_UNAVAILABLE", captured)
        codes = [record["code"] for record in exporter._merged_diag_sink()]
        self.assertIn("POSE_ALIGNMENT_UNAVAILABLE", codes)
        # 供骨必须保留：palette 捕获与 attach run 都还在
        self.assertIn("ResourceZZPalette_b20f90ea_s1 = copy vs-t0 unless_null", text)
        self.assertIn(
            "run = CustomShaderZZMIMergedSkeletonAttach_C1_s1", text
        )
        # 判定必须摘掉：本部件不得出现在任何门控表达式的 seen/prev 项里
        for line in text.split("\n"):
            if line.lstrip().startswith("if ") and "seen_" in line:
                self.assertNotIn("$zz_ms_seen_11", line)
                self.assertNotIn("$zz_ms_prev_11", line)

    def test_skin_publish_emitted_for_every_required_component(self):
        """每个必需部件（含不能重放 draw 的窄布局部件）都能发布合并几何。

        回归（2026-09-17「一直闪」）：draw 版重放只能在布局兼容锚点落笔；若
        required 里的窄布局部件最后到达，则没有锚点能落笔 → 该槽 SO 整帧不写。
        蒙皮 CS 发布块必须对**所有**部件发（姿态/绑定见上面 test_*_shader_matches）。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)

        for model in models:
            builder = _FakeIniBuilder()
            exporter.add_unity_vs_texture_override_vb_sections(builder, model)
            text = "\n".join(builder.sections[0].SectionLineList)
            with self.subTest(draw_ib=model.draw_ib):
                self.assertIn("run = CustomShaderZZMISkin_G3_s1", text)
                self.assertIn("run = CustomShaderZZMISkin_G3_s2", text)
                self.assertIn("    so0 = null", text)
                # 发布块必须排在 draw 版重放之后：CS 按索引写，晚写才作数
                if "draw = 18776, 0" in text:
                    self.assertLess(
                        text.rindex("draw = 18776, 0"),
                        text.index("run = CustomShaderZZMISkin_G3_s1"),
                    )
                # 发布块用与 draw 重放同一套守卫条件（别名就绪门 + 期望集合门）
                self.assertIn(
                    "if ($zz_ms_seen_11 >= 1) && ($zz_ms_prev_01 == 0 || $zz_ms_seen_01 >= 1)",
                    text,
                )

        _vb, _ib, skeleton_text = self._all_sections_text(exporter, models)
        self.assertIn("[CustomShaderZZMISkin_G3_s1]", skeleton_text)
        self.assertIn("[CustomShaderZZMISkin_G3_s2]", skeleton_text)
        self.assertIn("cs = ./res/zzmi_merged_skin.hlsl", skeleton_text)
        self.assertIn("x1 = 18776", skeleton_text)
        self.assertIn("y1 = 3", skeleton_text)
        self.assertIn("z1 = 3", skeleton_text)
        self.assertIn("w1 = 10", skeleton_text)
        self.assertIn("cs-t0 = ref Resourceb20f90eaPosition", skeleton_text)
        self.assertIn("cs-t1 = ref Resourceb20f90eaBlend", skeleton_text)
        self.assertIn("cs-t2 = ref ResourceZZMergedSkeleton_G3_s1", skeleton_text)
        self.assertIn("cs-u0 = ref ResourceZZRedirectSO_G3_s1", skeleton_text)
        self.assertIn("Dispatch = 294, 1, 1", skeleton_text)

    def test_latch_helpers_and_variables_removed_from_generator(self):
        """生成器层面：帧闩锁/相位辅助接口与变量已彻底移除（防止回归）。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        self.assertFalse(hasattr(exporter, "_zz_ms_drawn_marker_draw_ibs"))
        self.assertFalse(hasattr(exporter, "_append_ready_gated_render_draws"))
        vb_text, ib_text, skeleton_text = self._all_sections_text(exporter, models)
        combined = "\n".join((vb_text, ib_text, skeleton_text))
        self.assertNotIn("$zz_ms_redirect_drawn", combined)
        self.assertNotIn("$zz_ms_group_ready", combined)
        self.assertNotIn("$zz_ms_group_phase", combined)


class ZZMISkinPublishBlendLayoutGuardTests(_ZZMIGroup3RedirectFixture, unittest.TestCase):
    """B2 守卫：锚点 Blend 行布局 ≠ CS 写死的 32 字节时**不得发蒙皮 CS**。

    真实 dump 实证（`K:\\SSMT-Package-master\\3Dmigoto\\ZZZ\\FrameAnalysis-2026-09-17-184431`
    与 `...-185933` 的 `deduped/`）：同一个输入布局里 slot 0（Position）= 40 字节，
    而 slot 2（Blend）有三种形态 ——
      · layout=d8224520 / 73022f2f → BLENDWEIGHTS R32G32_FLOAT(8B) +
        BLENDINDICES R32G32_UINT(8B) = **16 字节**（每个 dump 各 2 个文件）；
      · layout=e4dfea81 → 只有 BLENDINDICES R32_UINT = **4 字节**（各 1 个）；
      · layout=e805604d 等 5 种 → R32G32B32A32 ×2 = **32 字节**（各 13 个）。
    `Toolset/zzmi_merged_skin.hlsl` 把 `cs-t1` 声明成 `StructuredBuffer<ZZBlend32>`
    （结构体 32 字节，SRV 步长由结构体决定、与底层 stride 无关）⇒ 16 字节锚点会被
    跨行错读权重/索引且**不报错**。守卫 = 只在锚点行布局 32 字节时发 CS。
    """

    def _narrow_blend_exporter(self):
        """组 3 全部部件的 Blend 布局改成 16 字节 ⇒ 锚点只可能是 16 字节。"""
        exporter, models = self._group3_exporter()
        for model in models:
            model.d3d11GameType.CategoryStrideDict["Blend"] = 16
        self._build_and_apply_plan(exporter)
        return exporter, models

    def _all_sections_text(self, exporter, models):
        """跑齐三类产段（与 ZZMIMultiInstanceLatchRemovalTests 同口径）。"""
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next(
                (m for m in models if m.draw_ib == carrier_ib), None
            )
            if carrier_model is None:
                continue
            stride = int(
                (
                    getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {})
                    or {}
                ).get("Texcoord", 0)
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get(
                "Texcoord"
            ):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )

        vb_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        vb_text = "\n".join(_all_builder_lines(vb_builder))

        ib_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_ib_sections(ib_builder, model)
        ib_text = "\n".join(_all_builder_lines(ib_builder))

        skeleton_builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(skeleton_builder)
        skeleton_text = "\n".join(_all_builder_lines(skeleton_builder))
        return vb_text, ib_text, skeleton_text

    def _generate(self, exporter, models):
        """跑齐三类产段，返回 (vb_text, ib_text, skeleton_text, stdout)。"""
        box = {}
        captured = self._capture_stdout(
            lambda: box.update(
                zip(
                    ("vb", "ib", "skeleton"),
                    self._all_sections_text(exporter, models),
                )
            )
        )
        return box["vb"], box["ib"], box["skeleton"], captured

    def test_narrow_fixture_really_lands_on_the_narrow_anchor(self):
        """先证明夹具真的把锚点推成 16 字节（否则下面的守卫用例毫无意义）。"""
        exporter, _models = self._narrow_blend_exporter()
        plan = exporter._merged_group_redirect_plan(3)
        self.assertIsNotNone(plan, "16 字节布局的组 3 仍应产出重定向计划")
        self.assertEqual(
            exporter._blend_layout_width(plan["anchor_layout_key"]), 16
        )
        self.assertFalse(exporter._merged_skin_publish_supported(plan))
        # 对照：32 字节夹具的锚点就是 CS 假设的布局
        wide_exporter, _wide_models = self._group3_exporter()
        self._build_and_apply_plan(wide_exporter)
        wide_plan = wide_exporter._merged_group_redirect_plan(3)
        self.assertEqual(
            wide_exporter._blend_layout_width(wide_plan["anchor_layout_key"]), 32
        )
        self.assertTrue(wide_exporter._merged_skin_publish_supported(wide_plan))

    def test_narrow_anchor_emits_no_cs_falls_back_to_draw_and_diagnoses(self):
        exporter, models = self._narrow_blend_exporter()
        vb_text, ib_text, skeleton_text, captured = self._generate(exporter, models)
        combined = "\n".join((vb_text, ib_text, skeleton_text))

        # ① 不发 CS：既无 CS 段定义，也无任何 run 引用、无蒙皮 CS 专有的绑定参数
        #（`cs-t2` = 合并骨架、`w1` = 每行 float 数只有蒙皮 CS 用；骨架 attach CS
        # 用的是 cs-t0=palette / cs-t1=vgmap，两者不受本守卫影响，不可拿来断言）
        self.assertNotIn("cs = ./res/zzmi_merged_skin.hlsl", combined)
        self.assertNotIn("CustomShaderZZMISkin_G3_s1", combined)
        self.assertNotIn("CustomShaderZZMISkin_G3_s2", combined)
        self.assertNotIn("cs-t2 = ref ", combined)
        self.assertNotIn("w1 = ", combined)
        self.assertNotIn("Dispatch = 294, 1, 1", combined)

        # ② 发出可读诊断（ini 注释行 + stdout + 诊断 sink 三处落点）
        self.assertIn("; ZZMI-MERGE-DIAG SKIN_LAYOUT_UNSUPPORTED", combined)
        self.assertIn("anchor_blend_bytes=16", combined)
        self.assertIn("required_blend_bytes=32", combined)
        self.assertIn("SKIN_LAYOUT_UNSUPPORTED", captured)
        codes = [record["code"] for record in exporter._merged_diag_sink()]
        self.assertIn("SKIN_LAYOUT_UNSUPPORTED", codes)

        # ③ 走回退：既有 draw 版重放（目标挂点守卫内绑定 carrier 的 vb0/vb2 后 draw）
        self.assertIn("    vb2 = Resourceb20f90eaBlend", vb_text)
        self.assertIn("    vb0 = Resourceb20f90eaPosition", vb_text)
        self.assertIn("    draw = 18776, 0", vb_text)
        self.assertIn("    so0 = ref ResourceZZRedirectSO_G3_s1", vb_text)

    def test_wide_anchor_keeps_the_unchanged_cs_path_and_no_layout_diag(self):
        """32 字节锚点：CS 段照旧（逐处断言既有参数），且不得出现本守卫的诊断。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        vb_text, ib_text, skeleton_text, captured = self._generate(exporter, models)
        combined = "\n".join((vb_text, ib_text, skeleton_text))

        self.assertIn("[CustomShaderZZMISkin_G3_s1]", skeleton_text)
        self.assertIn("[CustomShaderZZMISkin_G3_s2]", skeleton_text)
        self.assertIn("cs = ./res/zzmi_merged_skin.hlsl", skeleton_text)
        self.assertIn("cs-t1 = ref Resourceb20f90eaBlend", skeleton_text)
        self.assertIn("cs-t0 = ref Resourceb20f90eaPosition", skeleton_text)
        self.assertIn("cs-t2 = ref ResourceZZMergedSkeleton_G3_s1", skeleton_text)
        self.assertIn("cs-u0 = ref ResourceZZRedirectSO_G3_s1", skeleton_text)
        self.assertIn("x1 = 18776", skeleton_text)
        self.assertIn("y1 = 3", skeleton_text)
        self.assertIn("z1 = 3", skeleton_text)
        self.assertIn("w1 = 10", skeleton_text)
        self.assertIn("Dispatch = 294, 1, 1", skeleton_text)
        self.assertIn("run = CustomShaderZZMISkin_G3_s1", vb_text)
        self.assertIn("run = CustomShaderZZMISkin_G3_s2", vb_text)
        # 守卫本身不得误报：32 字节布局既无诊断行也无 stdout 提示
        self.assertNotIn("SKIN_LAYOUT_UNSUPPORTED", combined)
        self.assertNotIn("SKIN_LAYOUT_UNSUPPORTED", captured)
        self.assertNotIn(
            "SKIN_LAYOUT_UNSUPPORTED",
            [record["code"] for record in exporter._merged_diag_sink()],
        )


class ZZMIJoinedObjectIdentityTests(unittest.TestCase):
    """Joined Blender object identity must survive the ZZMI export front-end.

    A joined object keeps the target IB's workspace identity in
    ``3DMigoto:WorkspaceUniqueStr`` while its visible Blender name contains the
    new merged vertex count.  The exporter must use the joined object as the
    source and must not replace it with a three-vertex missing-part stub.
    """

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        self.old_workspace_folder = _fake_global_config.path_workspace_folder

    def tearDown(self):
        _fake_bpy_data.objects._items.clear()
        _fake_global_config.path_workspace_folder = self.old_workspace_folder

    def _merged_object(self):
        merged = _fake_bpy_data.objects.new("LOD0.8c8de427-24180-0")
        merged.props["3DMigoto:WorkspaceUniqueStr"] = "LOD0.8c8de427-798-0"
        merged.props["ZZMI_MergeSources"] = json.dumps(
            [
                {
                    "name": "LOD0.01ef4403-2286-9846.ZZMI_SOURCE",
                    "workspace_unique_str": "LOD0.01ef4403-2286-9846",
                },
                {
                    "name": "LOD0.8c8de427-798-0.ZZMI_SOURCE",
                    "workspace_unique_str": "LOD0.8c8de427-798-0",
                },
            ]
        )
        _fake_bpy_data.objects.new("LOD0.01ef4403-2286-9846.ZZMI_SOURCE")
        _fake_bpy_data.objects.new("LOD0.8c8de427-798-0.ZZMI_SOURCE")
        return merged

    def test_joined_target_rebinds_to_workspace_prefix_and_deduplicates_sources(self):
        merged = self._merged_object()
        target = _zzmi_module.DrawCallModel(
            obj_name="LOD0.8c8de427-24180-0",
            source_obj_name=merged.name,
        )
        source = _zzmi_module.DrawCallModel(
            obj_name="LOD0.01ef4403-2286-9846",
            source_obj_name="LOD0.01ef4403-2286-9846.ZZMI_SOURCE",
        )
        blueprint = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=[source, target]
        )
        exporter = object.__new__(_zzmi_module.ExportZZMI)

        exporter._normalize_merged_object_drawcalls(blueprint)

        self.assertEqual(blueprint.ordered_draw_obj_data_model_list, [target])
        self.assertEqual(target.obj_name, "LOD0.8c8de427-798-0")
        self.assertEqual(target.source_obj_name, merged.name)
        self.assertEqual(target.get_workspace_unique_str(), "LOD0.8c8de427-798-0")
        self.assertEqual(target.match_draw_ib, "8c8de427")
        self.assertEqual(target.match_index_count, "798")

    def test_active_joined_target_marks_all_source_components_present(self):
        merged = self._merged_object()
        target = _zzmi_module.DrawCallModel(
            obj_name="LOD0.8c8de427-24180-0",
            source_obj_name=merged.name,
        )
        blueprint = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=[target]
        )
        exporter = object.__new__(_zzmi_module.ExportZZMI)

        exporter._normalize_merged_object_drawcalls(blueprint)

        with tempfile.TemporaryDirectory() as workspace:
            lod0 = Path(workspace) / "LOD0"
            lod0.mkdir()
            (lod0 / "DrawIB-Component.json").write_text(
                json.dumps(
                    {
                        "8c8de427": {"0": "8c8de427-798-0"},
                        "01ef4403": {"0": "01ef4403-2286-9846"},
                    }
                ),
                encoding="utf-8",
            )
            _fake_global_config.path_workspace_folder = lambda: str(workspace)

            created = exporter._ensure_stub_objects_for_missing_parts(blueprint)

        self.assertEqual(created, [])
        self.assertFalse(
            any(
                bool(obj.get("ZZMI_STUB"))
                for obj in _fake_bpy_data.objects
            )
        )


class ZZMIMergedOrderingContractTests(_ZZMIGroup3RedirectFixture, unittest.TestCase):
    """主症①（顺序/延迟错位）与主症②（多实例混用）的**可证伪验收断言**。

    对应契约 `reports/zzmi-fix/01-fix-contract.md` 的 AC-A1 / AC-A2 / AC-A3 /
    AC-B2 / AC-B5，以及防回归的 AC-A4 / AC-A5。

    断言都针对**生成的 ini 原文**，且不是字符串同义反复：
    · `_parse_guards` 把生成器真正写出的守卫条件解析成 (槽, 部件) 上的原子，
      无法解析的原子直接判失败（守卫形态一变就炸）；
    · 单调性与「帧内最后一次闭合的时机」用解析出来的谓词**推演**（不是另写一份
      逻辑），因此把消费点改回 `== 1` 会让 n ≥ 3 场景的推演断言失败。
    """

    SLOTS = (1, 2)
    CAPTURE_CLAUSE = "copy vs-t0 unless_null"

    def _sections_text(self, exporter, models):
        """生成 VB 段与骨架段原文（与 `ZZMIMultiInstanceLatchRemovalTests` 同口径）。

        carrier 的 Redirect Texcoord 需要按 stride 补真实缓冲，否则生成器按
        `_write_redirect_texcoord_resources` 的契约直接抛错（不得静默降级）。
        """
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next((m for m in models if m.draw_ib == carrier_ib), None)
            if carrier_model is None:
                continue
            stride = int(
                (getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {}).get(
                    "Texcoord", 0
                )
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get(
                "Texcoord"
            ):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )
        vb_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        vb_text = "\n".join(_all_builder_lines(vb_builder))
        skeleton_builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(skeleton_builder)
        skeleton_text = "\n".join(_all_builder_lines(skeleton_builder))
        return vb_text, skeleton_text

    # ------------------------------------------------------------------ 解析
    def _parse_guards(self, text, component_ids):
        """把 ini 里每条守卫 `if (...) && (...)` 解析成原子列表。

        原子 = (kind, operator, component_id, slot)，kind ∈ {"gate", "cull"}：
        · "gate"：`($zz_ms_seen_<c><k> <op> 1)`——SO 别名当帧已由捕获者刷新；
        · "cull"：`($zz_ms_prev_<c><k> == 0 || $zz_ms_seen_<c><k> <op> 1)`——期望集合门。

        解析器**故意同时接受** `>=` 与 `==` 两种算子：这样把消费点改回 `== 1`
        时，失败发生在「推演出的真值不单调」这条**语义**断言上，而不是只在
        「字符串形状变了」上——测试才真正测语义，不是同义反复。
        任何别的形状（多槽混写、未知算子/变量）都直接判失败。
        """
        operators = (">=", "==")
        gate_body = {
            (c, s, op): f"$zz_ms_seen_{c}{s} {op} 1"
            for c in component_ids
            for s in self.SLOTS
            for op in operators
        }
        cull_body = {
            (c, s, op): f"($zz_ms_prev_{c}{s} == 0 || $zz_ms_seen_{c}{s} {op} 1)"
            for c in component_ids
            for s in self.SLOTS
            for op in operators
        }
        guards = []
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("if (") and "$zz_ms_seen_" in stripped):
                continue
            parts = stripped[len("if ") :].split(" && ")
            atoms = []
            for part in parts:
                part = part.strip()
                if part.startswith("(") and part.endswith(")") and " || " not in part:
                    part = part[1:-1]
                matched_gate = [
                    key for key, body in gate_body.items() if body == part
                ]
                matched_cull = [
                    key for key, body in cull_body.items() if body == part
                ]
                self.assertEqual(
                    len(matched_gate) + len(matched_cull),
                    1,
                    f"守卫原子既不是 SO-ready 门也不是期望集合门: {part!r}",
                )
                if matched_gate:
                    component_id, slot, operator = matched_gate[0]
                    atoms.append(("gate", operator, component_id, slot))
                else:
                    component_id, slot, operator = matched_cull[0]
                    atoms.append(("cull", operator, component_id, slot))
            self.assertTrue(atoms, f"空守卫: {stripped!r}")
            slots = {atom[3] for atom in atoms}
            self.assertEqual(len(slots), 1, f"一条守卫混了多个槽: {stripped!r}")
            guards.append({"slot": slots.pop(), "atoms": atoms, "text": stripped})
        self.assertTrue(guards, "生成结果里没有任何 seen 守卫")
        return guards

    @staticmethod
    def _guard_truth(guard, seen, prev):
        for kind, operator, component_id, slot in guard["atoms"]:
            seen_value = seen[(component_id, slot)]
            arrived = seen_value >= 1 if operator == ">=" else seen_value == 1
            if kind == "gate":
                if not arrived:
                    return False
            else:
                if not (prev[(component_id, slot)] == 0 or arrived):
                    return False
        return True

    def _simulate(self, guards, component_ids, draws, prev):
        """按状态机逐笔推演守卫真值。

        draws = 部件号序列（每笔 = 该部件的一次 deform draw）。状态机形状与
        `_append_merged_skeleton_deform_block_body` 步骤 1/2 逐字一致：
        `occ += 1` → `if occ >= 3: occ = SLOTS[0]` → 每槽 `seen += (occ == slot)`。
        """
        occ = {c: 0 for c in component_ids}
        seen = {(c, s): 0 for c in component_ids for s in self.SLOTS}
        captures = {(c, s): [] for c in component_ids for s in self.SLOTS}
        truth = []
        for draw_index, component_id in enumerate(draws):
            occ[component_id] += 1
            if occ[component_id] >= 3:
                occ[component_id] = 1
            for slot in self.SLOTS:
                if occ[component_id] == slot:
                    seen[(component_id, slot)] += 1
                    captures[(component_id, slot)].append(draw_index)
            truth.append([self._guard_truth(g, seen, prev) for g in guards])
        return truth, captures, seen

    @staticmethod
    def _order(component_ids, instances, reverse_every_other=False):
        """实例内提交顺序：偶数实例升序、奇数实例降序（模拟"逐部件先后翻转"）。"""
        draws = []
        for instance in range(instances):
            order = list(component_ids)
            if reverse_every_other and instance % 2 == 1:
                order = list(reversed(order))
            draws.extend(order)
        return draws

    # ------------------------------------------------------- AC-A2 / AC-A1
    def test_consumer_predicate_is_frame_monotone(self):
        """AC-A2：消费点谓词必须是「本帧出现过」语义（`>= 1`），且状态机不变。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        vb_text, skeleton_text = self._sections_text(exporter, models)
        component_ids = list(range(len(exporter.merged_skeleton_components)))

        guards = self._parse_guards(vb_text, component_ids)
        self.assertGreaterEqual(len(guards), 2)
        # 每条守卫都必须能被解析成两种已知原子形态之一（解析器已在内部断言）。
        for guard in guards:
            self.assertTrue(
                any(atom[0] == "gate" for atom in guard["atoms"]),
                f"每条守卫都应有 SO-ready 门: {guard['text']}",
            )
        # 反向断言：`== 1` 形式在生成器里**彻底消失**（只在 occ 捕获条件里保留 ==）
        combined = "\n".join((vb_text, skeleton_text))
        self.assertNotRegex(combined, r"\$zz_ms_seen_\d+=? ?== 1")
        # SO-ready 门的槽与 guard["slot"] 一致（解析器已断言单槽）。
        for guard in guards:
            for kind, operator, _c, slot in guard["atoms"]:
                if kind == "gate":
                    self.assertEqual(slot, guard["slot"])
                # 契约要求：所有消费点都用单调谓词 `>=`
                self.assertEqual(operator, ">=")
        # 状态机原样保留（禁止项）：occ 自增 + 回绕 + 每槽顶层 sticky 累加
        for component_id in component_ids:
            self.assertIn(f"$zz_ms_occ_{component_id} = $zz_ms_occ_{component_id} + 1", vb_text)
            self.assertIn(f"if $zz_ms_occ_{component_id} >= 3", vb_text)
            self.assertIn(f"    $zz_ms_occ_{component_id} = 1", vb_text)
            for slot in self.SLOTS:
                self.assertIn(
                    f"$zz_ms_seen_{component_id}{slot} = $zz_ms_seen_{component_id}{slot}"
                    f" + ($zz_ms_occ_{component_id} == {slot})",
                    vb_text,
                )

    def test_guard_truth_is_monotone_and_last_closure_follows_captures(self):
        """AC-A1 + AC-A2：守卫真值帧内单调；帧内**最后一次闭合**必在全部必需部件
        完成该槽捕获之后（⇒ 提前闭合对最终帧内容无害）。

        推演输入取自**生成器真正写出的守卫**（`_parse_guards`），状态机逐字复刻
        生成器步骤 1/2；场景覆盖 n=1/2/3/6 与「逐部件提交顺序翻转」的混合顺序。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        vb_text, _sk = self._sections_text(exporter, models)
        component_ids = list(range(len(exporter.merged_skeleton_components)))
        guards = self._parse_guards(vb_text, component_ids)

        # prev 场景：(a) 上一帧单实例（增长帧）、(b) 上一帧同实例数（稳态）、
        # (c) 上一帧全缺席/首帧（初值 1 的极端）——按槽预测由 [Present] 抄录决定。
        prev_scenarios = {
            "first_frame_all_expected": {
                (c, s): 1 for c in component_ids for s in self.SLOTS
            },
            "prev_single_instance": {
                (c, s): (1 if s == 1 else 0)
                for c in component_ids
                for s in self.SLOTS
            },
            "prev_two_instances": {
                (c, s): 1 for c in component_ids for s in self.SLOTS
            },
            "prev_all_absent": {
                (c, s): 0 for c in component_ids for s in self.SLOTS
            },
        }
        scenarios = []
        for instances in (1, 2, 3, 6):
            for mixed in (False, True):
                scenarios.append((instances, mixed))
        checked = 0
        for name, prev in prev_scenarios.items():
            for instances, mixed in scenarios:
                draws = self._order(component_ids, instances, reverse_every_other=mixed)
                truth, captures, _seen = self._simulate(
                    guards, component_ids, draws, prev
                )
                label = f"{name}/n={instances}/mixed={mixed}"
                for guard_index, guard in enumerate(guards):
                    series = [frame[guard_index] for frame in truth]
                    # (i) 帧内单调：真值只可能由假转真
                    self.assertEqual(
                        series,
                        sorted(series),
                        f"守卫真值帧内转假（谓词非单调）: {label} slot={guard['slot']} "
                        f"{guard['text']} → {series}",
                    )
                    if not any(series):
                        continue
                    last_true = max(i for i, value in enumerate(series) if value)
                    required = {
                        atom[2] for atom in guard["atoms"] if atom[3] == guard["slot"]
                    }
                    # (ii) 最后一次闭合时，该槽的所有必需部件都已当帧捕获过
                    for component_id in required:
                        capture_draws = captures[(component_id, guard["slot"])]
                        if not capture_draws:
                            continue
                        self.assertGreaterEqual(
                            last_true,
                            max(capture_draws),
                            f"帧内最后一次闭合早于必需部件的槽捕获: {label} "
                            f"slot={guard['slot']} component={component_id} "
                            f"last_true={last_true} captures={capture_draws}",
                        )
                    checked += 1
        self.assertGreater(checked, 0, "至少应有一个守卫在某个场景里闭合")

    def test_required_component_arriving_after_first_closure_is_covered(self):
        """AC-A1 的原始场景（契约 §3.1 ①a / S2）必须被上面的口径覆盖：

        上一帧单实例、本帧 2 实例的过渡帧里，**首次**闭合确实可能早于某些必需
        部件的槽 2 捕获（这是 `prev` 预测的固有分辨率）；该判据只要求
        「帧内最后一次闭合」晚于全部捕获，据此提前闭合对最终帧内容无害。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        vb_text, _sk = self._sections_text(exporter, models)
        component_ids = list(range(len(exporter.merged_skeleton_components)))
        guards = self._parse_guards(vb_text, component_ids)
        prev = {(c, s): (1 if s == 1 else 0) for c in component_ids for s in self.SLOTS}
        draws = self._order(component_ids, 2)
        truth, captures, _seen = self._simulate(guards, component_ids, draws, prev)
        slot2 = [g for g in guards if g["slot"] == 2]
        self.assertTrue(slot2)
        guard_index = guards.index(slot2[0])
        series = [frame[guard_index] for frame in truth]
        self.assertIn(True, series, "槽 2 守卫在 2 实例帧里必须闭合")
        first_true = series.index(True)
        last_true = max(i for i, value in enumerate(series) if value)
        required = {atom[2] for atom in slot2[0]["atoms"] if atom[3] == 2}
        # 该场景确实存在"首次闭合时还有必需部件没捕获"的情形（契约所述的机制），
        # 但最后一次闭合必须已经覆盖全部捕获 —— 这就是「提前闭合无害」的可复现推演。
        later_captures = [
            max(captures[(c, 2)]) for c in required if captures[(c, 2)]
        ]
        self.assertLessEqual(first_true, max(later_captures))
        self.assertGreaterEqual(last_true, max(later_captures))
        self.assertEqual(last_true, len(draws) - 1)

    # ------------------------------------------------------------- AC-A3
    def test_slot_bound_is_declared_and_runtime_overflow_is_detectable(self):
        """AC-A3：超出 `SLOTS` 上界不得静默——静态声明 + 运行时探针 + 语义一致。

        探针值与状态机推演必须一致：`x3` 读的 `$zz_ms_seen_<i>1` 恰在
        「同帧同部件出现次数 = 3」时等于 2，因此帧分析日志里读到 2 就是超界的
        显式失败标记（不是另写一套判据）。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        captured = self._capture_stdout(
            lambda: self._sections_text(exporter, models)
        )
        _vb_text, skeleton_text = self._sections_text(exporter, models)

        self.assertIn("; ZZMI-MERGE-DIAG SLOT_BOUND", skeleton_text)
        # D1 强制字段：组号 / 部件数 / 设计上界（逐组声明，不是全局一句）
        self.assertIn("group=G3", skeleton_text)
        self.assertIn("components=3", skeleton_text)
        self.assertIn("slots=1,2", skeleton_text)
        self.assertIn("occ_wrap=3", skeleton_text)
        self.assertIn("max_same_frame_occurrences=2", skeleton_text)
        self.assertIn("overflow=occurrence>2_wraps_to_slot_1", skeleton_text)
        self.assertIn("SLOT_BOUND", captured)
        # 运行时探针：槽 1 的 attach 段把 seen 透到 IniParams
        self.assertIn("x3 = $zz_ms_seen_01", skeleton_text)
        # 诊断同时进机器可读记录表
        codes = [record["code"] for record in exporter._merged_diag_sink()]
        self.assertIn("SLOT_BOUND", codes)

        # 探针值 ↔ 超界语义一致（用状态机推演，不另写判据）
        prev = {(c, s): 0 for c in (0, 1, 2) for s in self.SLOTS}
        guards = [
            {"slot": 1, "atoms": [("gate", ">=", 0, 1), ("cull", ">=", 0, 1)]}
        ]
        for instances, expected_probe in ((1, 1), (2, 1), (3, 2), (4, 2)):
            draws = self._order([0], instances)
            _truth, _captures, seen = self._simulate(guards, [0], draws, prev)
            self.assertEqual(
                seen[(0, 1)],
                expected_probe,
                f"n={instances} 时探针值应为 {expected_probe}（= 超界标记）",
            )

    # -------------------------------------------------------- AC-B2 / AC-B5
    def test_pose_alignment_unavailable_is_declared_not_silent(self):
        """AC-B2(b) / AC-B5：姿态对齐不可达的三个分支都必须有**可断言**的输出。

        分支①（有计划的组没有共享锚点 = 契约 §2.5 的 G0 情形）→ 诊断行；
        分支②（有锚点的组没有重定向计划 = G2 情形）→ 诊断行；
        分支③（可达）→ 诊断行**必须消失**且对齐块真的发射（证明诊断是条件性的，
        不是无条件噪声）。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        carrier = next(m for m in models if m.draw_ib == "b20f90ea")

        def _carrier_text(current_exporter):
            builder = _FakeIniBuilder()
            current_exporter.add_unity_vs_texture_override_vb_sections(builder, carrier)
            return "\n".join(builder.sections[0].SectionLineList)

        # 分支①：t75 起"组内无共享骨"不再让整组没有通道（按连通分量各自取通道），
        # 所以这里显式清掉通道记录来代表「无通道部件」这条不可达路径。
        for component in exporter.merged_skeleton_components:
            component["channel"] = {}
        stdout = self._capture_stdout(lambda: _carrier_text(exporter))
        text = _carrier_text(exporter)
        self.assertIn("; ZZMI-MERGE-DIAG POSE_ALIGNMENT_UNAVAILABLE", text)
        self.assertIn("reason=no_channel_plan", text)
        self.assertIn("POSE_ALIGNMENT_UNAVAILABLE", stdout)
        self.assertNotIn("SpatialHash", text)

        # 分支③：造出整组共享的通道骨 ⇒ 必须发射（且没有任何 UNAVAILABLE 诊断）
        for component in exporter.merged_skeleton_components:
            vg_map = dict(component["vg_map"])
            vg_map[min(vg_map)] = 0
            component["vg_map"] = vg_map
            component.pop("channel", None)
        _with_channel_plan(exporter.merged_skeleton_components)
        exporter._redirect_target_map["a23aa8a3"]["pose_anchor_slot"] = 0
        reachable_text = _carrier_text(exporter)
        self.assertIn("ResourceZZPoseKeySrc = ref vs-t0", reachable_text)
        self.assertIn("->HashRegion(0, 48)", reachable_text)
        self.assertNotIn("POSE_ALIGNMENT_UNAVAILABLE", reachable_text)

        # 分支②：有锚点但本轮没有重定向计划（G2 情形）
        exporter2, models2 = self._group3_exporter()
        for component in exporter2.merged_skeleton_components:
            vg_map = dict(component["vg_map"])
            vg_map[min(vg_map)] = 0
            component["vg_map"] = vg_map
        exporter2._redirect_carrier_map = {}
        exporter2._redirect_target_map = {}
        carrier2 = next(m for m in models2 if m.draw_ib == "b20f90ea")
        builder2 = _FakeIniBuilder()
        exporter2.add_unity_vs_texture_override_vb_sections(builder2, carrier2)
        text2 = "\n".join(builder2.sections[0].SectionLineList)
        self.assertIn("; ZZMI-MERGE-DIAG POSE_ALIGNMENT_UNAVAILABLE", text2)
        self.assertIn("reason=no_redirect_plan", text2)

    def test_single_component_group_is_not_reported_as_alignment_gap(self):
        """单部件组没有跨部件混用风险 ⇒ 不得被诊断成缺口（防噪声/防误报）。"""
        components = [
            {
                "draw_ib": "b20f90ea",
                "unique_str": "LOD0.b20f90ea-19182-0",
                "vg_offset": 184,
                "vg_count": 51,
                "skeleton_group": 3,
                "vg_map": {i: 184 + i for i in range(51)},
                "deform_draw": 2,
            }
        ]
        models = [
            _FakeDrawIBModel(
                "b20f90ea",
                [
                    self._attach_drawcalls(
                        _FakeSubmesh(
                            "LOD0.b20f90ea-19182-0", 184, 51, exported_vertex_count=18776
                        ),
                        index_count=69612,
                    )
                ],
            )
        ]
        exporter = self._make_exporter(models, components)
        self._build_and_apply_plan(exporter)
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, models[0])
        text = "\n".join(builder.sections[0].SectionLineList)
        self.assertNotIn("POSE_ALIGNMENT_UNAVAILABLE", text)

    # ------------------------------------------------- 防回归（AC-A4 / AC-A5）
    def test_protected_state_machine_and_present_resets_are_intact(self):
        """AC-A4 / AC-A5：写序不变量、`[Present]` 三处重置、`prev` 初值 1、
        禁止项不得回潮（`seen_*` 与 occ 回绕也必须在）。"""
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        vb_text, skeleton_text = self._sections_text(exporter, models)
        component_ids = list(range(len(exporter.merged_skeleton_components)))

        constants_text, present_text = skeleton_text.split("[Present]")[0], skeleton_text.split("[Present]")[1]

        # 禁止项不得回潮
        for forbidden in ("group_ready", "group_phase", "redirect_drawn", "$zz_ms_any_"):
            self.assertNotIn(forbidden, "\n".join((vb_text, skeleton_text)))

        # prev 初值仍为 1、seen/occ 声明仍在
        for component_id in component_ids:
            self.assertIn(f"global $zz_ms_occ_{component_id} = 0", constants_text)
            for slot in self.SLOTS:
                self.assertIn(
                    f"global $zz_ms_seen_{component_id}{slot} = 0", constants_text
                )
                self.assertIn(
                    f"global $zz_ms_prev_{component_id}{slot} = 1", constants_text
                )

        # [Present]：每个 (i,k) 的 prev = seen 必须早于同 (i,k) 的 seen = 0；
        # occ = 0 齐全；且不得写任何资源复位
        present_lines = [line.strip() for line in present_text.splitlines()]
        for component_id in component_ids:
            self.assertIn(f"$zz_ms_occ_{component_id} = 0", present_lines)
            for slot in self.SLOTS:
                prev_line = f"$zz_ms_prev_{component_id}{slot} = $zz_ms_seen_{component_id}{slot}"
                seen_line = f"$zz_ms_seen_{component_id}{slot} = 0"
                self.assertIn(prev_line, present_lines)
                self.assertIn(seen_line, present_lines)
                self.assertLess(
                    present_lines.index(prev_line),
                    present_lines.index(seen_line),
                    "[Present] 写序必须 prev = seen 早于 seen = 0",
                )
        for resource_prefix in ("ResourceZZRedirectSO", "ResourceZZPalette", "ResourceZZMergedSkeleton"):
            self.assertNotIn(resource_prefix, present_text)

        # 守卫体内只有绑定与 draw（不得 run、不得给 $ 变量赋值；允许嵌套的
        # `if $zz_ms_occ_<i> == <k>` 按出现次绑定块）
        lines = vb_text.splitlines()
        checked_guards = 0
        for index, line in enumerate(lines):
            if not line.strip().startswith("if ($zz_ms_seen_"):
                continue
            depth = 1
            body = []
            for follower in lines[index + 1 :]:
                stripped_follower = follower.strip()
                if stripped_follower.startswith("if "):
                    depth += 1
                elif stripped_follower == "endif":
                    depth -= 1
                    if depth == 0:
                        break
                else:
                    body.append((depth, stripped_follower))
            self.assertTrue(body, f"空守卫体: {line!r}")
            for body_depth, body_line in body:
                self.assertFalse(body_line.startswith("$"), body_line)
                # attach 的 run 必须全在段顶层（if 内的 run 在本 fork 上不执行）。
                # 唯一的例外是**刻意**的蒙皮 CS 发布（`run = CustomShaderZZMISkin_*`
                # 在守卫体内，官方库同用法，见 `_append_merged_skin_publish_block`）。
                self.assertFalse(
                    body_line.startswith("run = CustomShaderZZMIMergedSkeletonAttach_"),
                    f"attach run 进了 if 体: {body_line!r}",
                )
                if body_line.startswith("if "):
                    self.assertIn("$zz_ms_occ_", body_line)
                    continue
                if body_line.startswith("run = CustomShaderZZMISkin_"):
                    continue
                self.assertTrue(
                    body_line.startswith(
                        ("vs-t0 =", "so0 =", "vb0 =", "vb2 =", "draw =")
                    ),
                    f"守卫体内出现非法语句: {body_line!r}",
                )
            checked_guards += 1
        self.assertGreater(checked_guards, 0)


class ZZMIReuseDiagnosticsO3Tests(_ZZMIGroup3RedirectFixture, unittest.TestCase):
    """O3（用户裁定 A：**诊断先行、零行为变更**）的可证伪断言。

    背景（登记册 §59/§61/§64 + t8 块口径结论，代次 `184431` / `log.txt` sha256 `fd6efcc7…a18471`）：
    该帧 `999bff94` 的**捕获 : 重放 = 1 : 1**（部件口径），组口径 **6 : 1 属设计行为**；
    §52 的「1 次捕获 : 7 次重放」**不成立**（7 = IB 绑定/渲染绘制，是**消费端**）。
    ⇒ O3 的交付物是**可数证据**：每个消费点旁落一行 `; ZZMI-MERGE-DIAG REUSE_SITE …` 注释
    + 导出期一行 `REUSE_RATIO` 结构比汇总；**硬不变量 = 不碰任何 ini 语义**。

    本类逐条断言（全部针对生成 ini **原文**，不读 `K:`）：
    ① 注释数 == 消费点数，且注释紧跟其守卫行（未侵入块体）；
    ② `REUSE_RATIO` 汇总存在、`captures/consumers/ratio` 与注释自洽、且**明写"非运行次数"**；
    ③ **未新增任何 `$zz_ms_*` 状态变量**（变量名集合 == t2 代次集合）；
    ④ IB 覆盖段**无**诊断注释、**无** `$zz_ms_*`（逐字节不变量在证据脚本里另有 A/B 证明）；
    ⑤ 重放/发布块的守卫行形态与块体语句集合不变（只有注释新增）。
    """

    MARKER = "; ZZMI-MERGE-DIAG REUSE_SITE"

    def _sections_text(self, exporter, models):
        """生成 VB 段与骨架段原文（与 ZZMIMergedOrderingContractTests 同口径：
        carrier 的 Redirect Texcoord 需按 stride 补真实缓冲，否则生成器按契约抛错）。"""
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next((m for m in models if m.draw_ib == carrier_ib), None)
            if carrier_model is None:
                continue
            stride = int(
                (getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {}).get(
                    "Texcoord", 0
                )
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get("Texcoord"):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )
        vb_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        vb_text = "\n".join(_all_builder_lines(vb_builder))
        skeleton_builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(skeleton_builder)
        skeleton_text = "\n".join(_all_builder_lines(skeleton_builder))
        return vb_text, skeleton_text

    def _render(self):
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        captured = self._capture_stdout(
            lambda: self._sections_text(exporter, models)
        )
        vb_text, skeleton_text = self._sections_text(exporter, models)
        ib_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_ib_sections(ib_builder, model)
        ib_text = "\n".join(_all_builder_lines(ib_builder))
        return exporter, vb_text, ib_text, skeleton_text, captured

    def test_o3_reuse_site_markers_match_consumer_sites(self):
        """① 注释数 == 消费点数；且每条注释的下一非注释行是守卫 `if (…)`。"""
        _exporter, vb_text, _ib, _skel, _captured = self._render()
        lines = vb_text.splitlines()
        markers = [
            (index, line.strip())
            for index, line in enumerate(lines)
            if line.strip().startswith(self.MARKER)
        ]
        consumer_sites = [
            line for line in lines
            if line.strip().startswith("so0 = ref ResourceZZRedirectSO_")
            or line.strip().startswith("run = CustomShaderZZMISkin_")
        ]
        self.assertTrue(markers)
        self.assertEqual(
            len(markers),
            len(consumer_sites),
            f"注释数 {len(markers)} 必须等于消费点数 {len(consumer_sites)}",
        )
        for index, marker in markers:
            self.assertIn("group=G", marker)
            self.assertIn("slot=", marker)
            self.assertIn("consumer=", marker)
            self.assertIn("kind=", marker)
            follower = lines[index + 1].strip()
            self.assertTrue(
                follower.startswith("if ($zz_ms_seen_"),
                f"注释后必须紧跟守卫行（不得侵入块体）: {follower!r}",
            )

    def test_o3_reuse_ratio_stdout_matches_markers(self):
        """② REUSE_RATIO 汇总存在、与注释自洽、且明写口径（非运行次数）。"""
        _exporter, vb_text, _ib, _skel, captured = self._render()
        ratio_lines = [line for line in captured.splitlines() if "REUSE_RATIO" in line]
        self.assertTrue(ratio_lines, "导出期必须打印 REUSE_RATIO 汇总")
        markers = [line for line in vb_text.splitlines() if line.strip().startswith(self.MARKER)]
        # 该夹具：组 G3、2 槽、3 个部件节 ⇒ 每槽 consumers = 3(replay-draw) + 3(publish-cs) = 6
        self.assertEqual(len(markers), 12)
        for line in ratio_lines:
            self.assertIn("captures=1", line)
            self.assertIn("consumers=6", line)
            self.assertIn("ratio=6:1", line)
            self.assertIn("非运行次数", line)
            self.assertIn("不得读成复用次数", line)
        self.assertEqual(len(ratio_lines), 2, "每 (组,槽) 一行")

    def test_o3_no_new_state_variables(self):
        """③ 未新增任何 `$zz_ms_*` 状态变量（§61：禁止新状态变量/闩锁/if 体内 `$` 写）。

        t75：唯一允许新增的是「每组一条」的通道骨键变量，而它只在**跨部件共享通道骨**
        存在时才发（默认夹具三件互不共骨 ⇒ 都按"只供骨"处置，不发键块）。这里按
        生产形态造出共享通道骨（与 `test_pose_key_alignment_uses_shared_canonical_bone`
        同法：槽位 0 被三件同时引用），再核对变量全集。
        """
        exporter, models = self._group3_exporter()
        self._build_and_apply_plan(exporter)
        for component in exporter.merged_skeleton_components:
            vg_map = dict(component["vg_map"])
            vg_map[min(vg_map)] = 0
            component["vg_map"] = vg_map
            component.pop("channel", None)
        _with_channel_plan(exporter.merged_skeleton_components)
        self._capture_stdout(lambda: self._sections_text(exporter, models))
        vb_text, skeleton_text = self._sections_text(exporter, models)
        # 排除诊断注释行（其字段里出现 `$zz_ms_seen_<i>1` 这种**文档占位**，不是变量实例）
        lines = [
            line
            for line in "\n".join((vb_text, skeleton_text)).splitlines()
            if "ZZMI-MERGE-DIAG" not in line
        ]
        names = sorted(set(re.findall(r"\$zz_ms_[A-Za-z0-9_]+", "\n".join(lines))))
        component_ids = (0, 1, 2)
        expected = sorted(
            {f"$zz_ms_occ_{c}" for c in component_ids}
            | {f"$zz_ms_seen_{c}{s}" for c in component_ids for s in (1, 2)}
            | {f"$zz_ms_prev_{c}{s}" for c in component_ids for s in (1, 2)}
            # t75：唯一新增的是「每组一条」的通道骨键变量（读 HashRegion 的值）。
            # 不新增任何闩锁/相位/消费标记变量。
            | {"$zz_ms_pose_key_3"}
        )
        self.assertEqual(names, expected, "`$zz_ms_*` 变量集合必须与 t2 代次完全相同")
        for banned in ("consumed", "reuse", "gate", "ready", "phase", "drawn", "any_"):
            self.assertNotIn(f"$zz_ms_{banned}", "\n".join(lines))

    def test_o3_ib_override_section_is_marker_free(self):
        """④ IB 覆盖段无诊断注释、无 `$zz_ms_*`（消费端不被诊断化改动）。"""
        _exporter, _vb, ib_text, _skel, _captured = self._render()
        self.assertNotIn(self.MARKER, ib_text)
        self.assertNotIn("$zz_ms_", ib_text)
        self.assertNotIn("ZZMI-MERGE-DIAG", ib_text)
        self.assertIn("drawindexed = ", ib_text)

    def test_o3_replay_block_bodies_unchanged(self):
        """⑤ 每条注释对应的块：守卫行 + 绑定/draw 语句集合不变（只多注释）。"""
        _exporter, vb_text, _ib, _skel, _captured = self._render()
        lines = vb_text.splitlines()
        checked = 0
        for index, line in enumerate(lines):
            if not line.strip().startswith(self.MARKER):
                continue
            depth = 0
            guard_seen = False
            body: list[str] = []
            for follower in lines[index + 1:]:
                stripped = follower.strip()
                if stripped.startswith("if "):
                    depth += 1
                    if not guard_seen:
                        guard_seen = True
                        self.assertTrue(stripped.startswith("if ($zz_ms_seen_"))
                    continue
                if stripped == "endif":
                    depth -= 1
                    if depth == 0:
                        break
                    continue
                body.append(stripped)
            self.assertTrue(guard_seen, f"注释后缺少守卫: {line!r}")
            for entry in body:
                self.assertFalse(entry.startswith("$"), entry)
                self.assertTrue(
                    entry.startswith(
                        ("vs-t0 =", "so0 =", "vb0 =", "vb2 =", "draw =", "run = CustomShaderZZMISkin_")
                    ),
                    f"块体语句集合被改动: {entry!r}",
                )
            checked += 1
        self.assertEqual(checked, 12)


class _StopExportSentinel(RuntimeError):
    """哨兵：代表「导出已走到第一个落盘点」，用来证明契约判定先于任何写盘。"""


class ZZMIMergedContractWiringTests(unittest.TestCase):
    """B1：合并骨架契约必须**真的接进导出流程**，且 error 在任何写盘之前中止。

    关键点（去掉接线即变红）：
    - ``_export_impl`` 必须先 ``_collect_merged_skeleton_components()`` 再
      ``_enforce_merged_skeleton_contract()``，然后才碰第一个落盘点
      ``generate_buffer_files``；本测试把落盘点换成「会写文件的哨兵」并断言
      契约 error 时哨兵**从未被调用**、临时输出目录**为空**。
    - 判定输入来自真实契约模块（按 fake 包前缀登记的真实实现），用
      ``mock.patch.object`` 包装成间谍记录 kwargs，再交回真函数算 level。
    """

    def setUp(self):
        _fake_bpy_data.objects._items.clear()
        _fake_bpy_data.meshes._items.clear()
        self._tmp_dir = tempfile.mkdtemp(prefix="zzmi_contract_out_")
        self._prev_mod_folder = _fake_global_config.path_generate_mod_folder
        self._prev_import_merged = _fake_global_properties.import_merged_vgmap
        _fake_global_config.path_generate_mod_folder = lambda: self._tmp_dir

    def tearDown(self):
        _fake_global_config.path_generate_mod_folder = self._prev_mod_folder
        _fake_global_properties.import_merged_vgmap = self._prev_import_merged
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    # ---- 夹具 -----------------------------------------------------------
    def _exporter(self, models, merged_vgmap=True):
        exporter = _make_exporter(models, merged_vgmap=merged_vgmap)
        exporter.drawib_model_list = models
        return exporter

    def _data_model(self, draw_ib="b20f90ea", vg_count=51, vg_offset=154,
                    unique_str=None):
        return _FakeDrawIBModel(
            draw_ib,
            [_FakeSubmesh(unique_str or f"LOD0.{draw_ib}-19182-0", vg_offset, vg_count)],
        )

    def _stale_version_model(self, draw_ib="8c8de427"):
        submesh = _FakeSubmesh(f"LOD0.{draw_ib}-19182-0", 0, 2)
        submesh.vg_map_algorithm_version = 1  # != ZZMI_VG_MAP_ALGORITHM_VERSION
        return _FakeDrawIBModel(draw_ib, [submesh])

    def _run_export_impl(self, exporter):
        """跑 ``_export_impl``：记录契约判定 kwargs 与「首个落盘点」是否到达。

        返回 ``(decisions, write_reached, raised, stdout)``。落盘点被替换成「先写一个
        产物文件、再抛哨兵」——因此哨兵未触发就等于「没有写出任何输出文件」。
        """
        decisions = []
        real_evaluate = _zzmi_contract_module.evaluate_merged_skeleton_contract

        def _spy(**kwargs):
            decisions.append(kwargs)
            return real_evaluate(**kwargs)

        marker = os.path.join(self._tmp_dir, "Meshes_first_write.buf")

        def _first_write_point(*_args, **_kwargs):
            with open(marker, "wb") as handle:
                handle.write(b"written")
            raise _StopExportSentinel("已到达第一个落盘点")

        raised = None
        buf = io.StringIO()
        with mock.patch.object(
            _zzmi_contract_module,
            "evaluate_merged_skeleton_contract",
            side_effect=_spy,
        ), mock.patch.object(
            exporter,
            "generate_buffer_files",
            _first_write_point,
            create=True,  # fake 宿主的 ExportUnity 桩没有该方法，生产实现有
        ):
            try:
                with contextlib.redirect_stdout(buf):
                    exporter._export_impl()
            except BaseException as error:  # noqa: BLE001 - 测试要区分 Fatal/哨兵
                raised = error
        return decisions, os.path.isfile(marker), raised, buf.getvalue()

    def _function_source(self, name: str) -> str:
        """取生产实现里某个函数的源码段（AST 权威，避开 PowerShell 行号错位）。"""
        source = (REPO_ROOT / "ui" / "universal" / "zzmi.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(source, node) or ""
        self.fail(f"未在 ui/universal/zzmi.py 找到函数 {name}")

    # ---- 1) 真的接线了 ---------------------------------------------------
    def test_export_path_really_calls_contract_before_any_write(self):
        # (a) 源码级接线断言：去除接线（删掉 _enforce_* 调用 / 把它挪到落盘点之后）
        #     本用例必红。
        body = self._function_source("_export_impl")
        self.assertIn("self._collect_merged_skeleton_components()", body)
        self.assertIn("self._enforce_merged_skeleton_contract()", body)
        self.assertIn("self.generate_buffer_files(", body)
        self.assertLess(
            body.index("self._collect_merged_skeleton_components()"),
            body.index("self._enforce_merged_skeleton_contract()"),
        )
        self.assertLess(
            body.index("self._enforce_merged_skeleton_contract()"),
            body.index("self.generate_buffer_files("),
            "契约判定必须发生在第一个落盘点之前",
        )
        guard_body = self._function_source("_enforce_merged_skeleton_contract")
        self.assertIn("evaluate_merged_skeleton_contract(", guard_body)
        self.assertIn("raise Fatal(", guard_body)

        # (b) 间谍注入：导出确实调用契约，且 kwargs 是真实统计值
        exporter = self._exporter([self._data_model()], merged_vgmap=True)
        decisions, write_reached, raised, _stdout = self._run_export_impl(exporter)

        self.assertEqual(len(decisions), 1, "导出流程必须调用一次契约判定")
        self.assertEqual(
            decisions[0],
            {
                "checkbox_enabled": True,
                "parts_with_data": 1,
                "parts_with_global_ids": 0,
                "component_count": 1,
                "skip_reasons": {},
            },
        )
        # 契约在先、落盘点在后：本用例 level=ok 因此确实走到了落盘点；
        # 「判定发生在写盘之前」由下面的 error 用例做强断言（落盘点从未到达）。
        self.assertTrue(write_reached)
        self.assertIsInstance(raised, _StopExportSentinel)

    # ---- 2) 开关关闭 + 有数据 ⇒ 普通导出，不中止 ---------------------------
    def _model_with_object(self, group_names):
        """给 `_data_model()` 的代表对象挂上给定数字顶点组名（几何判据输入）。"""
        model = self._data_model()  # vg_count=51, vg_offset=154
        submesh = model.submesh_model_list[0]
        obj = _fake_bpy_data.objects.new(submesh.unique_str)
        for group_name in group_names:
            obj.vertex_groups.new(name=group_name)
        submesh.drawcall_model_list = [_FakeDrawCall(submesh.unique_str)]
        return model

    def test_disabled_checkbox_with_data_exports_plainly(self):
        """关闭复选框时几何是部件局部编号（合并预处理被该开关门控），

        因此工作区残留的 VGMap 缓存（导入侧无条件生成）不参与本次导出，
        契约按普通导出放行：必须走到落盘点，且**不得**抛 Fatal。
        """
        exporter = self._exporter([self._data_model()], merged_vgmap=False)
        decisions, write_reached, raised, stdout = self._run_export_impl(exporter)

        self.assertEqual(
            decisions,
            [{
                "checkbox_enabled": False,
                "parts_with_data": 1,
                "parts_with_global_ids": 0,
                "component_count": 0,
                "skip_reasons": {},
            }],
        )
        self.assertTrue(write_reached, "普通导出必须继续（要走到落盘点）")
        self.assertIsInstance(raised, _StopExportSentinel)
        self.assertIn("普通导出", stdout)

    def test_disabled_checkbox_with_local_vertex_groups_exports_plainly(self):
        """几何顶点组是部件局部编号 ``0..vg_count-1`` ⇒ 判为普通导出。"""
        model = self._model_with_object(str(i) for i in range(51))
        exporter = self._exporter([model], merged_vgmap=False)
        decisions, write_reached, raised, _stdout = self._run_export_impl(exporter)

        self.assertEqual(decisions[0]["parts_with_global_ids"], 0)
        self.assertTrue(write_reached, "局部编号必须继续导出")
        self.assertIsInstance(raised, _StopExportSentinel)

    def test_disabled_checkbox_with_global_vertex_groups_still_aborts(self):
        """几何顶点组**确实**落在全局骨骼编号空间 ⇒ 仍中止（保护未被削弱）。

        「导入时开着开关、导出前把开关关掉」才有的真危险场景。本部件
        vg_offset=154 / vg_count=51，局部空间只可能是 `0..50`，组名从 154 起
        只能解释为全局骨骼 id。
        """
        model = self._model_with_object(str(154 + i) for i in range(51))
        exporter = self._exporter([model], merged_vgmap=False)
        decisions, write_reached, raised, _stdout = self._run_export_impl(exporter)

        self.assertEqual(decisions[0]["parts_with_global_ids"], 1)
        self.assertFalse(write_reached, "确证几何是全局编号时不得写出任何输出文件")
        self.assertEqual(os.listdir(self._tmp_dir), [])
        self.assertEqual(type(raised).__name__, "Fatal")
        self.assertIn("确实", str(raised))

    def test_missing_vgmap_rejects_weights_outside_native_palette_before_writing(self):
        model = self._model_with_object(["146"])
        submesh = model.submesh_model_list[0]
        submesh.vg_count = 0
        submesh.vg_map = {}
        obj = _fake_bpy_data.objects.get(submesh.unique_str)
        obj.data = types.SimpleNamespace(vertices=[types.SimpleNamespace(groups=[
            types.SimpleNamespace(group=0, weight=1.0),
        ])])

        for merged_vgmap in (False, True):
            with self.subTest(merged_vgmap=merged_vgmap):
                exporter = self._exporter([model], merged_vgmap=merged_vgmap)
                with mock.patch.object(exporter, "_zzmi_native_palette_capacity", return_value=16, create=True):
                    _, write_reached, raised, _ = self._run_export_impl(exporter)
                self.assertIsInstance(raised, _zzmi_module.Fatal)
                self.assertIn("146", str(raised))
                self.assertIn(model.draw_ib, str(raised))
                self.assertFalse(write_reached)
                self.assertEqual(os.listdir(self._tmp_dir), [])

    def test_missing_vgmap_accepts_locally_weighted_vertices_with_unused_global_group(self):
        model = self._model_with_object(["15", "146"])
        submesh = model.submesh_model_list[0]
        submesh.vg_count = 0
        submesh.vg_map = {}
        obj = _fake_bpy_data.objects.get(submesh.unique_str)
        obj.data = types.SimpleNamespace(vertices=[types.SimpleNamespace(groups=[
            types.SimpleNamespace(group=0, weight=1.0),
            types.SimpleNamespace(group=1, weight=0.0),
        ])])
        exporter = self._exporter([model], merged_vgmap=False)
        with mock.patch.object(exporter, "_zzmi_native_palette_capacity", return_value=16, create=True):
            _, write_reached, raised, _ = self._run_export_impl(exporter)

        self.assertTrue(write_reached)
        self.assertIsInstance(raised, _StopExportSentinel)

    # ---- 3) error 场景 B：有数据但全部被拒 ⇒ 写盘前中止 + 列出原因 --------
    def test_error_all_parts_rejected_lists_reasons_and_aborts(self):
        exporter = self._exporter(
            [self._stale_version_model()], merged_vgmap=True
        )
        decisions, write_reached, raised, _stdout = self._run_export_impl(exporter)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["parts_with_data"], 1)
        self.assertEqual(decisions[0]["component_count"], 0)
        self.assertEqual(
            decisions[0]["skip_reasons"],
            {"8c8de427": "VGMap 缓存版本 1 过旧且内容不完整（需重新一键导入）"},
        )
        self.assertFalse(write_reached, "契约 error 时不得到达任何落盘点")
        self.assertEqual(os.listdir(self._tmp_dir), [], "不得写出任何输出文件")
        self.assertEqual(type(raised).__name__, "Fatal")
        text = str(raised)
        self.assertIn("全部被导出器拒绝", text)
        self.assertIn("8c8de427", text)              # hint 列出被拒部件
        self.assertIn("缓存版本", text)               # hint 列出被拒原因

    # ---- 4) notice 不打断普通导出 ----------------------------------------
    def test_notice_does_not_interrupt_normal_export(self):
        exporter = self._exporter(
            [_FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 0, 0)])],
            merged_vgmap=False,
        )
        decisions, write_reached, raised, _stdout = self._run_export_impl(exporter)

        self.assertEqual(
            decisions,
            [{
                "checkbox_enabled": False,
                "parts_with_data": 0,
                "parts_with_global_ids": 0,
                "component_count": 0,
                "skip_reasons": {},
            }],
        )
        self.assertTrue(write_reached, "notice 必须继续导出（要走到落盘点）")
        self.assertIsInstance(raised, _StopExportSentinel)
        self.assertEqual(exporter._merged_diag_sink(), [])  # 不新增任何诊断弹窗

    # ---- 5) warning 提示后继续导出 ---------------------------------------
    def test_warning_continues_export_and_lists_rejected_parts(self):
        exporter = self._exporter(
            [self._data_model(), self._stale_version_model()], merged_vgmap=True
        )
        decisions, write_reached, raised, stdout = self._run_export_impl(exporter)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["parts_with_data"], 2)
        self.assertEqual(decisions[0]["component_count"], 1)
        self.assertEqual(decisions[0]["skip_reasons"], {"8c8de427": "VGMap 缓存版本 1 过旧且内容不完整（需重新一键导入）"})

        # 继续导出：确实走到落盘点，且抛的是哨兵而不是 Fatal
        self.assertTrue(write_reached)
        self.assertIsInstance(raised, _StopExportSentinel)
        # 用户可见提示（控制台 + 导出日志），含被拒部件与原因
        self.assertIn("骨骼合并不完整", stdout)
        self.assertIn("8c8de427", stdout)
        self.assertIn("缓存版本", stdout)


def _skin_element(category, semantic, index, fmt, byte_width, slot=""):
    """输入布局元素替身（`D3D11ElementList` 的最小字段集）。"""
    return types.SimpleNamespace(
        Category=category,
        SemanticName=semantic,
        SemanticIndex=index,
        Format=fmt,
        ByteWidth=byte_width,
        ExtractSlot=slot,
    )


# 与单一事实源一致（真实数据佐证：工作空间 json CategoryBufferList + 抓帧 slot 元素表）
POSITION_ELEMENTS_40B = [
    _skin_element("Position", "POSITION", 0, "R32G32B32_FLOAT", 12, "vb0"),
    _skin_element("Position", "NORMAL", 0, "R32G32B32_FLOAT", 12, "vb0"),
    _skin_element("Position", "TANGENT", 0, "R32G32B32A32_FLOAT", 16, "vb0"),
]
BLEND_ELEMENTS_32B = [
    _skin_element("Blend", "BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, "vb2"),
    _skin_element("Blend", "BLENDINDICES", 0, "R32G32B32A32_UINT", 16, "vb2"),
]


class ZZMISkinRowLayoutElementTests(_ZZMIGroup3RedirectFixture, unittest.TestCase):
    """t40：蒙皮 CS 行布局的**逐元素**动态识别（旧实现只比总宽度，会放行这些）。

    覆盖 `ui/universal/zzmi.py`：
    - `_merged_skin_row_layout_mismatches`（逐元素判据：声明 stride + 语义/索引/格式/宽/偏移/extract_slot）
    - `_merged_skin_layout_mismatches`（Blend 锚点 + 每个 deform_draws 条目的 Position + 目的侧 so_stride）
    - `_merged_skin_publish_supported`（两道 CS 闸门共用）
    - `_merged_skin_layout_diag`（诊断点名不匹配的元素）
    - `_drawib_category_layout`（类目参数化的布局读取，位置侧复用）
    """

    def _exporter_with_elements(
        self,
        *,
        blend=None,
        position=None,
        blend_stride=None,
        position_stride=None,
    ):
        """组 3 夹具 + 注入元素表；`blend`/`position`/`*_stride` 支持按 draw_ib 覆盖。"""
        exporter, models = self._group3_exporter()
        for model in models:
            game_type = model.d3d11GameType
            position_elements = (
                position.get(model.draw_ib, POSITION_ELEMENTS_40B)
                if isinstance(position, dict)
                else (position or POSITION_ELEMENTS_40B)
            )
            blend_elements = (
                blend.get(model.draw_ib, BLEND_ELEMENTS_32B)
                if isinstance(blend, dict)
                else (blend or BLEND_ELEMENTS_32B)
            )
            game_type.D3D11ElementList = list(position_elements) + list(blend_elements)
            for category, override in (
                ("Position", position_stride),
                ("Blend", blend_stride),
            ):
                if override is None:
                    continue
                value = (
                    override.get(model.draw_ib)
                    if isinstance(override, dict)
                    else override
                )
                if value is not None:
                    game_type.CategoryStrideDict[category] = int(value)
        self._build_and_apply_plan(exporter)
        return exporter, models

    def _text(self, exporter, models):
        """生成 vb / ib / skeleton 全文（与 t38 同口径，含 carrier 的 vb1 缓冲预填）。"""
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next(
                (m for m in models if m.draw_ib == carrier_ib), None
            )
            if carrier_model is None:
                continue
            stride = int(
                (getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {})
                .get("Texcoord", 0)
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get(
                "Texcoord"
            ):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )
        vb_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        vb_text = "\n".join(_all_builder_lines(vb_builder))
        ib_builder = _FakeIniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_ib_sections(ib_builder, model)
        ib_text = "\n".join(_all_builder_lines(ib_builder))
        skeleton_builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(skeleton_builder)
        skeleton_text = "\n".join(_all_builder_lines(skeleton_builder))
        return vb_text, ib_text, skeleton_text

    # ---- 匹配路径 --------------------------------------------------------
    def test_matching_elements_emit_cs_and_no_layout_diag(self):
        """元素表与单一事实源一致 ⇒ 发 CS、无布局诊断（旧行为的等价面）。"""
        exporter, models = self._exporter_with_elements()
        plan = exporter._merged_group_redirect_plan(3)
        self.assertEqual(exporter._merged_skin_layout_mismatches(plan), [])
        self.assertTrue(exporter._merged_skin_publish_supported(plan))
        vb_text, ib_text, skeleton_text = self._text(exporter, models)
        combined = "\n".join((vb_text, ib_text, skeleton_text))
        self.assertIn("cs = ./res/zzmi_merged_skin.hlsl", skeleton_text)
        self.assertIn("[CustomShaderZZMISkin_G3_s1]", skeleton_text)
        self.assertIn("run = CustomShaderZZMISkin_G3_s1", vb_text)
        self.assertNotIn("SKIN_LAYOUT_UNSUPPORTED", combined)

    def test_generated_cs_slots_follow_the_declaration(self):
        """生成产物里的 cs-t0/cs-t1 绑定必须与声明里的 hlsl_slot 一致。"""
        exporter, models = self._exporter_with_elements()
        _vb_text, _ib_text, skeleton_text = self._text(exporter, models)
        position_slot = _zzmi_module.ZZMI_MERGED_SKIN_ROW_LAYOUT["position"]["hlsl_slot"]
        blend_slot = _zzmi_module.ZZMI_MERGED_SKIN_ROW_LAYOUT["blend"]["hlsl_slot"]
        self.assertIn(f"{position_slot} = ref Resourceb20f90eaPosition", skeleton_text)
        self.assertIn(f"{blend_slot} = ref Resourceb20f90eaBlend", skeleton_text)

    # ---- ① 总宽 32B 但元素顺序/语义不同（旧守卫会放行） --------------------
    def test_blend_32b_with_swapped_element_order_is_rejected(self):
        swapped = [
            _skin_element("Blend", "BLENDINDICES", 0, "R32G32B32A32_UINT", 16, "vb2"),
            _skin_element("Blend", "BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, "vb2"),
        ]
        exporter, models = self._exporter_with_elements(blend=swapped)
        plan = exporter._merged_group_redirect_plan(3)
        # 总宽仍 32B（旧判据会放行）但逐元素不匹配
        self.assertEqual(
            exporter._drawib_blend_layout("b20f90ea")["stride"], 32
        )
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertTrue(
            any(m.startswith("blend:anchor:elements[0].semantic") for m in mismatches),
            mismatches,
        )
        self.assertFalse(exporter._merged_skin_publish_supported(plan))

        vb_text, ib_text, skeleton_text = self._text(exporter, models)
        combined = "\n".join((vb_text, ib_text, skeleton_text))
        # 不发 CS（段定义 + run 引用都没有）
        self.assertNotIn("cs = ./res/zzmi_merged_skin.hlsl", combined)
        self.assertNotIn("CustomShaderZZMISkin_G3_s1", combined)
        # 诊断点名具体元素
        self.assertIn("; ZZMI-MERGE-DIAG SKIN_LAYOUT_UNSUPPORTED", combined)
        self.assertIn("blend:anchor:elements[0].semantic", combined)
        # 退回既有 draw 重放
        self.assertIn("    draw = 18776, 0", vb_text)
        self.assertIn("    so0 = ref ResourceZZRedirectSO_G3_s1", vb_text)

    def test_blend_32b_with_indices_recorded_as_float_is_rejected(self):
        """同一语义/顺序、总宽 32B，但索引格式被记成 FLOAT ⇒ 必须拦。"""
        wrong_type = [
            _skin_element("Blend", "BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, "vb2"),
            _skin_element("Blend", "BLENDINDICES", 0, "R32G32B32A32_FLOAT", 16, "vb2"),
        ]
        exporter, _models = self._exporter_with_elements(blend=wrong_type)
        plan = exporter._merged_group_redirect_plan(3)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertTrue(
            any(
                m.startswith("blend:anchor:elements[1].format")
                and "R32G32B32A32_INT" in m
                and "R32G32B32A32_FLOAT" in m
                for m in mismatches
            ),
            mismatches,
        )
        self.assertFalse(exporter._merged_skin_publish_supported(plan))

    # ---- A：声明 stride 才是权威（元素宽度求和不一致时） -------------------
    def test_declared_stride_wins_over_element_width_sum(self):
        """元素求和 32B 但**声明 stride 48** ⇒ 必须按声明拦下（旧判据比 Σ 会放行）。"""
        exporter, _models = self._exporter_with_elements(blend_stride=48)
        plan = exporter._merged_group_redirect_plan(3)
        layout = plan.get("anchor_layout")
        self.assertEqual(sum(e["byte_width"] for e in layout["elements"]), 32)
        self.assertEqual(layout["stride"], 48)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertIn("blend:anchor:stride:expected=32:actual=48", mismatches)
        self.assertFalse(exporter._merged_skin_publish_supported(plan))
        # 诊断报告的值也必须是声明 stride（不是 Σ）
        diag = exporter._merged_skin_layout_diag(3, plan)
        self.assertIn("anchor_blend_bytes=48", diag)

    def test_declared_stride_32b_but_elements_sum_48b_is_rejected(self):
        """反向分叉：声明 stride 32 而元素求和 48 ⇒ 元素逐项比对必须拦下。"""
        wide = [
            _skin_element("Blend", "BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, "vb2"),
            _skin_element("Blend", "BLENDINDICES", 0, "R32G32B32A32_UINT", 16, "vb2"),
            _skin_element("Blend", "BLENDINDICES", 1, "R32G32B32A32_UINT", 16, "vb2"),
        ]
        exporter, _models = self._exporter_with_elements(
            blend=wide, blend_stride=32
        )
        plan = exporter._merged_group_redirect_plan(3)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertTrue(
            any(m.startswith("blend:anchor:element-count") for m in mismatches),
            mismatches,
        )
        self.assertFalse(exporter._merged_skin_publish_supported(plan))

    # ---- ② Position 侧（同类缺陷第三处） ---------------------------------
    def test_position_40b_with_different_elements_is_rejected(self):
        """总宽 40B 但元素构成不同（POSITION 4 分量） ⇒ 必须拦（旧实现无任何 Position 判据）。"""
        bad_position = [
            _skin_element("Position", "POSITION", 0, "R32G32B32A32_FLOAT", 16, "vb0"),
            _skin_element("Position", "NORMAL", 0, "R32G32B32_FLOAT", 12, "vb0"),
            _skin_element("Position", "TANGENT", 0, "R32G32B32_FLOAT", 12, "vb0"),
        ]
        exporter, models = self._exporter_with_elements(
            position={"b20f90ea": bad_position}
        )
        plan = exporter._merged_group_redirect_plan(3)
        # 载体 Position 总宽仍 40B
        carrier_layout = exporter._drawib_category_layout("b20f90ea", "Position")
        self.assertEqual(carrier_layout["stride"], 40)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertTrue(
            any(
                m.startswith("position:b20f90ea:elements[0].format")
                or m.startswith("position:b20f90ea:elements[0].byte_width")
                for m in mismatches
            ),
            mismatches,
        )
        vb_text, ib_text, skeleton_text = self._text(exporter, models)
        combined = "\n".join((vb_text, ib_text, skeleton_text))
        self.assertNotIn("cs = ./res/zzmi_merged_skin.hlsl", combined)
        self.assertIn("position:b20f90ea:elements[0]", combined)
        self.assertIn("    draw = 18776, 0", vb_text)

    def test_position_stride_mismatch_is_rejected(self):
        """Position 声明 stride ≠ 40（元素表仍是 40B 构成）⇒ 拦。"""
        exporter, _models = self._exporter_with_elements(position_stride=48)
        plan = exporter._merged_group_redirect_plan(3)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertTrue(
            any(
                m.startswith("position:b20f90ea:stride:expected=40:actual=48")
                for m in mismatches
            ),
            mismatches,
        )
        self.assertFalse(exporter._merged_skin_publish_supported(plan))

    def test_destination_so_stride_mismatch_is_rejected(self):
        """目的侧行宽（CS 的 w1*4 / RedirectSO 声明 stride）同样纳入判据。

        target（纯占位）自身不产生前缀 draw ⇒ 只会在目的侧暴露；
        载体 Position 仍 40B，故这是纯粹的"目的行宽"用例。
        """
        exporter, _models = self._exporter_with_elements(
            position_stride={"a23aa8a3": 48}
        )
        plan = exporter._merged_group_redirect_plan(3)
        self.assertEqual(int(plan["so_stride"]), 48)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertIn("dest:so_stride:expected=40:actual=48", mismatches)
        self.assertFalse(exporter._merged_skin_publish_supported(plan))

    # ---- ④ ExtractSlot 溯源（F3）：同偏移/同语义但 slot 漂移也必须拦 --------
    def test_blend_anchor_extract_slot_drift_is_rejected(self):
        """Blend 锚点 slot 由 vb2 漂到 vb9（其余全同）⇒ 报 extract_slot 且停发 CS。

        这是「同偏移不同 slot」的判据面：总宽/语义/格式/字节宽/偏移**全部相同**，
        旧判据会放行；Slot 是唯一差异 ⇒ 不匹配列表必须**恰好**只有这一条。
        """
        drifted = [
            _skin_element("Blend", "BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, "vb9"),
            _skin_element("Blend", "BLENDINDICES", 0, "R32G32B32A32_UINT", 16, "vb2"),
        ]
        exporter, models = self._exporter_with_elements(blend=drifted)
        plan = exporter._merged_group_redirect_plan(3)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertEqual(
            mismatches,
            ["blend:anchor:elements[0].extract_slot:expected=VB2:actual=VB9"],
        )
        self.assertFalse(exporter._merged_skin_publish_supported(plan))

        vb_text, ib_text, skeleton_text = self._text(exporter, models)
        combined = "\n".join((vb_text, ib_text, skeleton_text))
        # 不发 CS（段定义 + run 引用都没有），落具名诊断并点名 slot
        self.assertNotIn("cs = ./res/zzmi_merged_skin.hlsl", combined)
        self.assertNotIn("CustomShaderZZMISkin_G3_s1", combined)
        self.assertIn("; ZZMI-MERGE-DIAG SKIN_LAYOUT_UNSUPPORTED", combined)
        self.assertIn("blend:anchor:elements[0].extract_slot", combined)
        # 回退到既有 draw 版重放
        self.assertIn("    draw = 18776, 0", vb_text)
        self.assertIn("    so0 = ref ResourceZZRedirectSO_G3_s1", vb_text)

    def test_position_extract_slot_drift_is_rejected(self):
        """Position 侧 slot 由 vb0 漂到 vb9 ⇒ 同样被拦（同偏移不同 slot）。"""
        drifted = [
            _skin_element("Position", "POSITION", 0, "R32G32B32_FLOAT", 12, "vb0"),
            _skin_element("Position", "NORMAL", 0, "R32G32B32_FLOAT", 12, "vb9"),
            _skin_element("Position", "TANGENT", 0, "R32G32B32A32_FLOAT", 16, "vb0"),
        ]
        exporter, _models = self._exporter_with_elements(
            position={"b20f90ea": drifted}
        )
        plan = exporter._merged_group_redirect_plan(3)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertEqual(
            mismatches,
            ["position:b20f90ea:elements[1].extract_slot:expected=VB0:actual=VB9"],
        )
        self.assertFalse(exporter._merged_skin_publish_supported(plan))

    def test_empty_or_missing_extract_slot_is_conservatively_allowed(self):
        """实际 ExtractSlot 为空/缺失（旧缓存不带 slot 溯源）⇒ 不算不匹配，仍发 CS。"""
        untracked = [
            _skin_element("Blend", "BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, ""),
            _skin_element("Blend", "BLENDINDICES", 0, "R32G32B32A32_UINT", 16, ""),
        ]
        exporter, models = self._exporter_with_elements(blend=untracked)
        plan = exporter._merged_group_redirect_plan(3)
        mismatches = exporter._merged_skin_layout_mismatches(plan)
        self.assertEqual(mismatches, [])
        self.assertTrue(exporter._merged_skin_publish_supported(plan))
        _vb_text, _ib_text, skeleton_text = self._text(exporter, models)
        self.assertIn("cs = ./res/zzmi_merged_skin.hlsl", skeleton_text)

        # 「缺失」路径（元素表里**没有**该键）与空串同口径：保守放行
        missing_key = exporter._merged_skin_row_layout_mismatches(
            "blend",
            {
                "stride": 32,
                "elements": [
                    {
                        "semantic": "BLENDWEIGHTS",
                        "index": 0,
                        "format": "R32G32B32A32_FLOAT",
                        "byte_width": 16,
                        "offset": 0,
                    },
                    {
                        "semantic": "BLENDINDICES",
                        "index": 0,
                        "format": "R32G32B32A32_UINT",
                        "byte_width": 16,
                        "offset": 16,
                    },
                ],
            },
            "blend:anchor",
        )
        self.assertEqual(missing_key, [])

    def test_extract_slot_comparison_ignores_case_and_whitespace(self):
        """`VB2` / `Vb2` / ` vb2 ` 与 `vb2` 等价 ⇒ 不得误报（大小写/空白归一）。"""
        for slot in ("VB2", "Vb2", " vb2 ", "vb2"):
            with self.subTest(slot=slot):
                noisy = [
                    _skin_element(
                        "Blend", "BLENDWEIGHTS", 0, "R32G32B32A32_FLOAT", 16, slot
                    ),
                    _skin_element(
                        "Blend", "BLENDINDICES", 0, "R32G32B32A32_UINT", 16, slot
                    ),
                ]
                exporter, _models = self._exporter_with_elements(blend=noisy)
                plan = exporter._merged_group_redirect_plan(3)
                self.assertEqual(exporter._merged_skin_layout_mismatches(plan), [])
                self.assertTrue(exporter._merged_skin_publish_supported(plan))

    def test_declared_extract_slots_match_generated_bindings(self):
        """反向对照：期望 slot 与生成端绑定源一致（Position→vb0 / Blend→vb2）⇒ 比对为空。

        断言的期望值不是凭空写的：CS 段把 `cs-t0` 绑到该部件的 **vb0** 资源、
        `cs-t1` 绑到 **vb2** 资源（工作空间复核口径见 `ui/universal/zzmi.py` 模块头
        「对齐规则依据」：`主角` 12 + `叶瞬光01` 20 个部件的 `ExtractSlot` 全为 vb0/vb2）。
        """
        layout = _zzmi_module.ZZMI_MERGED_SKIN_ROW_LAYOUT
        self.assertEqual(
            [e["extract_slot"] for e in layout["position"]["elements"]],
            ["vb0"] * 3,
        )
        self.assertEqual(
            [e["extract_slot"] for e in layout["blend"]["elements"]],
            ["vb2"] * 2,
        )
        exporter, _models = self._exporter_with_elements()
        plan = exporter._merged_group_redirect_plan(3)
        self.assertEqual(exporter._merged_skin_layout_mismatches(plan), [])
        self.assertTrue(exporter._merged_skin_publish_supported(plan))


class ZZMISkinRowLayoutContractTests(unittest.TestCase):
    """t40 ③：HLSL ↔ Python 期望布局的**一致性测试**（禁止将来任一侧静默漂移）。

    解析 `Toolset/zzmi_merged_skin.hlsl` 的结构体与 SRV 声明，与
    `zzmi.py` 的单一事实源 `ZZMI_MERGED_SKIN_ROW_LAYOUT` 逐字段比对：
    改 HLSL（字段类型/数量/顺序/结构体名/SRV/寄存器）或改 Python 声明（元素
    语义/格式/宽度/偏移/读取区间）**都会让本类变红**。
    """

    HLSL_TYPE_BYTES = {
        "float": 4, "float2": 8, "float3": 12, "float4": 16,
        "uint": 4, "uint2": 8, "uint3": 12, "uint4": 16,
        "int": 4, "int2": 8, "int3": 12, "int4": 16,
    }
    # HLSL 字段类型 ↔ 期望元素的 (格式, 分量数) 耦合（仅 1:1 的 blend 行成立）
    HLSL_TYPE_TO_FORMAT = {
        "float4": ("R32G32B32A32_FLOAT", 4),
        "uint4": ("R32G32B32A32_UINT", 4),
    }

    def _hlsl_source(self):
        return (REPO_ROOT / "Toolset" / "zzmi_merged_skin.hlsl").read_text(
            encoding="utf-8"
        )

    def _parse_struct(self, source, struct_name):
        match = re.search(
            rf"struct\s+{re.escape(struct_name)}\s*\{{(?P<body>.*?)\}}\s*;",
            source,
            re.S,
        )
        self.assertIsNotNone(
            match, f"HLSL 中找不到结构体 {struct_name}（改名/删除都会红）"
        )
        fields = []
        offset = 0
        for line in match.group("body").splitlines():
            field = re.match(
                r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", line
            )
            if not field:
                continue
            hlsl_type, hlsl_name = field.group(1), field.group(2)
            self.assertIn(
                hlsl_type,
                self.HLSL_TYPE_BYTES,
                f"{struct_name}.{hlsl_name} 的类型 {hlsl_type} 不在已知宽度表",
            )
            width = self.HLSL_TYPE_BYTES[hlsl_type]
            fields.append(
                {
                    "hlsl_type": hlsl_type,
                    "hlsl_name": hlsl_name,
                    "byte_width": width,
                    "offset": offset,
                }
            )
            offset += width
        return fields, offset

    def _spec(self, kind):
        return _zzmi_module.ZZMI_MERGED_SKIN_ROW_LAYOUT[kind]

    def test_hlsl_structs_match_the_declared_row_layout(self):
        source = self._hlsl_source()
        for kind in ("position", "blend"):
            spec = self._spec(kind)
            with self.subTest(kind=kind):
                fields, size = self._parse_struct(source, spec["hlsl_struct"])
                self.assertEqual(
                    fields,
                    list(spec["hlsl_fields"]),
                    f"{kind}: HLSL 结构体字段与 Python 声明不一致",
                )
                self.assertEqual(size, _zzmi_module._zzmi_skin_row_bytes(kind))
                self.assertIn(
                    f"StructuredBuffer<{spec['hlsl_struct']}> {spec['hlsl_srv']}"
                    f" : register({spec['hlsl_register']});",
                    source,
                    "SRV/结构体/寄存器绑定与声明不一致",
                )

    def test_hlsl_reads_cover_the_declared_elements(self):
        source = self._hlsl_source()
        for kind in ("position", "blend"):
            spec = self._spec(kind)
            with self.subTest(kind=kind):
                reads = list(spec["hlsl_reads"])
                for read in reads:
                    self.assertIn(
                        read["expr"],
                        source,
                        f"{kind}: CS 读取表达式被改动/删除: {read['expr']}",
                    )
                self.assertEqual(
                    [(r["offset"], r["byte_width"]) for r in reads],
                    [(e["offset"], e["byte_width"]) for e in spec["elements"]],
                    f"{kind}: 读取字节区间与期望元素区间不一致",
                )
                # 元素按声明顺序**紧凑铺满**整行（对齐规则：类目内偏移 = 累加 ByteWidth）
                running = 0
                for element in spec["elements"]:
                    self.assertEqual(element["offset"], running)
                    running += int(element["byte_width"])
                self.assertEqual(running, _zzmi_module._zzmi_skin_row_bytes(kind))

    def test_blend_field_types_match_element_formats(self):
        spec = self._spec("blend")
        fields = list(spec["hlsl_fields"])
        elements = list(spec["elements"])
        self.assertEqual(len(fields), len(elements))
        for field, element in zip(fields, elements):
            with self.subTest(field=field["hlsl_name"]):
                expected_format, components = self.HLSL_TYPE_TO_FORMAT[field["hlsl_type"]]
                self.assertEqual(element["format"], expected_format)
                self.assertEqual(int(field["byte_width"]), int(element["byte_width"]))
                self.assertEqual(4, components)
                # 语义名与分量数一致：BLENDWEIGHTS/4 通道、BLENDINDICES/4 通道
                self.assertIn(
                    element["semantic"], ("BLENDWEIGHTS", "BLENDINDICES")
                )

    def test_row_bytes_are_derived_not_hardcoded(self):
        """派生量：常量必须等于声明求和（改声明而忘了改常量会被这里拦住）。"""
        self.assertEqual(
            _zzmi_module.ZZMI_MERGED_SKIN_BLEND_ROW_BYTES,
            sum(int(e["byte_width"]) for e in self._spec("blend")["elements"]),
        )
        self.assertEqual(
            _zzmi_module.ZZMI_MERGED_SKIN_POSITION_ROW_BYTES,
            sum(int(e["byte_width"]) for e in self._spec("position")["elements"]),
        )
        self.assertEqual(_zzmi_module.ZZMI_MERGED_SKIN_BLEND_ROW_BYTES, 32)
        self.assertEqual(_zzmi_module.ZZMI_MERGED_SKIN_POSITION_ROW_BYTES, 40)


class ZZMISkinGuardFallbackCoverageTests(_ZZMIGroup3RedirectFixture, unittest.TestCase):
    """FR-2：守卫拦下 CS 时，发布覆盖面必须**如实分档**报告（不能说"已退回 draw 重放"）。

    本类内实测的事实（夹具断言，非推断）：
    - CS 发布块（`_append_merged_skin_publish_block`）是「Blend 布局与锚点不一致、完全不能
      重放 draw 的必需部件」**唯一**的发布者（计划书 §7 修复链 10）；
    - draw 版重放只在 `compatible_component_ids`（Blend 布局签名 == 锚点布局）的挂点落笔
      （`_merged_component_layout_compatible` / `_merged_component_can_host_replay`）；
    - ⇒ 守卫拦下 CS 后：锚点布局内的必需部件仍有人发布（每个兼容挂点都发同一条宿主重放）；
      **不在锚点布局内的必需部件本帧没有自己的发布点**（既无 draw 重放、也无 CS）——
      若它排在最后一个到达，该槽 SO 本帧可能不写 ⇒ 闪烁/缺失可能复现。

    `_merged_skin_replay_coverage` 的四态在这里各自有实测用例：
    `gap`（混合布局）/ `complete`（全部同锚点）/ `legacy`（锚点签名与全部必需部件都不符）/
    `unknown`（计划缺必需部件集合或锚点签名）。
    """

    BLEND_16B = [
        _skin_element("Blend", "BLENDWEIGHTS", 0, "R32G32_FLOAT", 8, "vb2"),
        _skin_element("Blend", "BLENDINDICES", 0, "R32G32_UINT", 8, "vb2"),
    ]

    def _fixture(self, blend_by_ib, stride_by_ib):
        exporter, models = self._group3_exporter()
        for model in models:
            game_type = model.d3d11GameType
            game_type.D3D11ElementList = list(POSITION_ELEMENTS_40B) + list(
                blend_by_ib[model.draw_ib]
            )
            game_type.CategoryStrideDict["Blend"] = int(stride_by_ib[model.draw_ib])
        self._build_and_apply_plan(exporter)
        return exporter, models

    def _mixed(self):
        """carrier + sibling = 16B（多数派）；target = 32B（必需，但不在锚点布局内）。"""
        return self._fixture(
            blend_by_ib={
                "b20f90ea": self.BLEND_16B,
                "b30db54e": self.BLEND_16B,
                "a23aa8a3": BLEND_ELEMENTS_32B,
            },
            stride_by_ib={"b20f90ea": 16, "b30db54e": 16, "a23aa8a3": 32},
        )

    def _all_narrow(self):
        return self._fixture(
            blend_by_ib={
                "b20f90ea": self.BLEND_16B,
                "b30db54e": self.BLEND_16B,
                "a23aa8a3": self.BLEND_16B,
            },
            stride_by_ib={"b20f90ea": 16, "b30db54e": 16, "a23aa8a3": 16},
        )

    @staticmethod
    def _per_model_text(exporter, models):
        """逐部件 VB 段文本（发布者 / 非发布者事实只能按段判）。"""
        text_by_ib = {}
        for model in models:
            builder = _FakeIniBuilder()
            exporter.add_unity_vs_texture_override_vb_sections(builder, model)
            text_by_ib[model.draw_ib] = "\n".join(_all_builder_lines(builder))
        return text_by_ib

    @staticmethod
    def _skeleton_text(exporter, models):
        for carrier_ib, carrier_info in (exporter._redirect_carrier_map or {}).items():
            carrier_model = next(
                (m for m in models if m.draw_ib == carrier_ib), None
            )
            if carrier_model is None:
                continue
            stride = int(
                (getattr(carrier_model.d3d11GameType, "CategoryStrideDict", {}) or {})
                .get("Texcoord", 0)
                or 0
            )
            if stride > 0 and not (carrier_model.category_buffer_dict or {}).get(
                "Texcoord"
            ):
                carrier_model.category_buffer_dict["Texcoord"] = bytes(
                    int(carrier_info.get("vertex_count", 0) or 0) * stride
                )
        builder = _FakeIniBuilder()
        exporter.add_merged_skeleton_sections(builder)
        return "\n".join(_all_builder_lines(builder))

    # ---- 事实：非锚点布局的必需部件没有任何发布者 -------------------------
    def test_blocked_required_part_has_no_publisher_when_guard_fires(self):
        exporter, models = self._mixed()
        plan = exporter._merged_group_redirect_plan(3)
        # 锚点 = 16B 多数派 ⇒ 守卫拦下（CS 期望 32B Blend）
        self.assertFalse(exporter._merged_skin_publish_supported(plan))
        self.assertEqual(plan["required_component_ids"], [0, 1, 2])
        self.assertEqual(plan["compatible_component_ids"], [1, 2])
        self.assertEqual(
            exporter._merged_skin_replay_coverage(3, plan), (["a23aa8a3"], "gap")
        )
        # 反例保护：把判据换成「已存 compatible 集合差」也要给出同一答案
        plan_without_anchor = dict(plan)
        plan_without_anchor.pop("anchor_layout_key")
        self.assertEqual(
            exporter._merged_skin_replay_coverage(3, plan_without_anchor),
            (["a23aa8a3"], "gap"),
        )

        text_by_ib = self._per_model_text(exporter, models)
        # 锚点布局内的必需部件：draw 版重放照常发布
        self.assertIn("draw = 18776, 0", text_by_ib["b20f90ea"])
        self.assertIn("draw = 18776, 0", text_by_ib["b30db54e"])
        # 非锚点布局的必需部件：自己段里既没有 draw 重放、也没有 CS 发布 ⇒ 本帧无人发布
        self.assertNotIn("draw = 18776, 0", text_by_ib["a23aa8a3"])
        self.assertNotIn("CustomShaderZZMISkin", text_by_ib["a23aa8a3"])
        # 且整个产物的 CS 段定义确实没发
        skeleton_text = self._skeleton_text(exporter, models)
        self.assertNotIn("cs = ./res/zzmi_merged_skin.hlsl", skeleton_text)

    # ---- 文案：如实分档（缺口 vs 完整覆盖） -------------------------------
    def test_diag_reports_the_publish_gap_and_names_the_blocked_part(self):
        exporter, models = self._mixed()
        captured = self._capture_stdout(
            lambda: (
                self._per_model_text(exporter, models),
                self._skeleton_text(exporter, models),
            )
        )
        text_by_ib = self._per_model_text(exporter, models)
        combined = "\n".join(text_by_ib.values()) + "\n" + self._skeleton_text(
            exporter, models
        )

        self.assertIn("; ZZMI-MERGE-DIAG SKIN_LAYOUT_UNSUPPORTED", combined)
        self.assertIn("replay_coverage=gap", combined)
        self.assertIn("blocked_required=a23aa8a3", combined)
        self.assertIn("publish_gap=1", combined)
        # 人读文案必须如实说明缺口与后果 + 给指引
        self.assertIn("本组有 1 个必需部件的 Blend 布局不在锚点布局内", captured)
        self.assertIn("这些部件本帧不发布合并几何（自身不是重放挂点）", captured)
        self.assertIn("仅当最后一个到达的必需部件属于锚点布局时", captured)
        self.assertIn("该槽 SO 本帧可能不写", captured)
        self.assertIn("闪烁/缺失可能复现", captured)
        self.assertIn("计划书 §7 修复链 10", captured)
        # 不得把「跳过 CS」说成完整回退
        self.assertNotIn("覆盖完整", captured)
        # FR-2 核心：不得再有"已退回 draw 版重放"这种过度承诺
        for text in (combined, captured):
            self.assertNotIn("退回 draw 版重放", text)
            self.assertNotIn("退回既有 draw", text)

    def test_all_compatible_group_reports_no_publish_gap(self):
        exporter, models = self._all_narrow()
        plan = exporter._merged_group_redirect_plan(3)
        self.assertFalse(exporter._merged_skin_publish_supported(plan))
        self.assertEqual(plan["compatible_component_ids"], [0, 1, 2])
        self.assertEqual(
            exporter._merged_skin_replay_coverage(3, plan), ([], "complete")
        )

        # 诊断只打一次（sink 按 code+字段去重）⇒ 捕获必须包住**第一次**触发发射的调用
        packed = {}
        captured = self._capture_stdout(
            lambda: (
                packed.update({"vb": self._per_model_text(exporter, models)}),
                packed.update({"skeleton": self._skeleton_text(exporter, models)}),
            )
        )
        text_by_ib = packed["vb"]
        for draw_ib, text in text_by_ib.items():
            with self.subTest(draw_ib=draw_ib):
                self.assertIn("draw = 18776, 0", text)
        combined = "\n".join(text_by_ib.values()) + "\n" + packed["skeleton"]
        self.assertIn("replay_coverage=complete", combined)
        self.assertIn("publish_gap=0", combined)
        self.assertIn("blocked_required=-", combined)
        self.assertIn("本组全部必需部件的 Blend 布局都在锚点布局内", captured)
        self.assertIn("draw 版重放覆盖完整", captured)
        self.assertNotIn("本帧不发布合并几何", captured)

    def test_diag_three_landing_points_agree(self):
        exporter, models = self._mixed()
        packed = {}
        captured = self._capture_stdout(
            lambda: packed.update(
                {"skeleton": self._skeleton_text(exporter, models)}
            )
        )
        combined = "\n".join(self._per_model_text(exporter, models).values())
        combined += "\n" + packed["skeleton"]

        records = [
            record
            for record in exporter._merged_diag_sink()
            if record.get("code") == "SKIN_LAYOUT_UNSUPPORTED"
        ]
        self.assertEqual(len(records), 1, records)
        # 三落点：产物 ini 注释 / stdout / sink 的机器可读字段一致
        for text in (combined, captured):
            self.assertIn("SKIN_LAYOUT_UNSUPPORTED", text)
            self.assertIn("replay_coverage=gap", text)
            self.assertIn("blocked_required=a23aa8a3", text)
            self.assertIn("publish_gap=1", text)
        sink_record = records[0]
        self.assertEqual(sink_record["replay_coverage"], "gap")
        self.assertEqual(sink_record["blocked_required"], "a23aa8a3")
        self.assertEqual(sink_record["publish_gap"], "1")
        self.assertEqual(sink_record["group"], "G3")
        # 人读结论只在 stdout（既有 DIAG 机制：注释行只带 code+字段）
        self.assertIn("本帧不发布合并几何", captured)

    # ---- 边界态：legacy（空集回退）与 unknown 都不能写成「覆盖完整」 ------
    def test_legacy_fallback_state_does_not_claim_complete_coverage(self):
        exporter, models = self._mixed()
        plan = dict(exporter._merged_group_redirect_plan(3))
        # 构造 legacy：锚点签名与**全部**必需部件都不一致（空集回退已把
        # compatible 写成 required），且必须满足 `_merged_skin_publish_supported` 为假
        plan["anchor_layout_key"] = ("stride", 48)
        plan["anchor_layout"] = {"stride": 48, "elements": []}
        plan["so_stride"] = 0
        plan["deform_draw_ibs"] = list(plan.get("deform_draw_ibs") or [])
        # 空集回退的痕迹：compatible 被写成 required（否则计划会直接把不兼容部件排除）
        plan["compatible_component_ids"] = [0, 1, 2]
        self.assertFalse(exporter._merged_skin_publish_supported(plan))
        self.assertEqual(
            exporter._merged_skin_replay_coverage(3, plan),
            (["a23aa8a3", "b20f90ea", "b30db54e"], "legacy"),
        )
        captured = self._capture_stdout(
            lambda: exporter._merged_skin_layout_diag(3, plan)
        )
        self.assertIn("replay_coverage=legacy", self._last_diag_line(exporter))
        self.assertIn("计划已按旧行为把全部必需部件都当重放挂点", captured)
        # 不得声称「全部都在锚点布局内 / 覆盖完整」（那正是 FR-2 要消灭的过度承诺）
        self.assertNotIn("全部必需部件的 Blend 布局都在锚点布局内", captured)
        self.assertNotIn("覆盖完整", captured)
        self.assertIn("计划书 §7 修复链 10", captured)

    def test_unknown_coverage_state_does_not_claim_complete_coverage(self):
        exporter, models = self._mixed()
        plan = dict(exporter._merged_group_redirect_plan(3))
        plan.pop("anchor_layout_key", None)
        plan.pop("compatible_component_ids", None)
        self.assertEqual(exporter._merged_skin_replay_coverage(3, plan), ([], "unknown"))
        captured = self._capture_stdout(
            lambda: exporter._merged_skin_layout_diag(3, plan)
        )
        self.assertIn("replay_coverage=unknown", self._last_diag_line(exporter))
        self.assertIn("发布覆盖面无法判定", captured)
        self.assertNotIn("覆盖完整", captured)
        # 无必需部件集合也要落到 unknown，而不是「完整覆盖」
        self.assertEqual(
            exporter._merged_skin_replay_coverage(
                3, {"required_component_ids": [], "compatible_component_ids": []}
            ),
            ([], "unknown"),
        )

    @staticmethod
    def _last_diag_line(exporter):
        """sink 里 SKIN_LAYOUT_UNSUPPORTED 记录 → 机器可读字段行（= ini 注释内容）。"""
        records = [
            record
            for record in exporter._merged_diag_sink()
            if record.get("code") == "SKIN_LAYOUT_UNSUPPORTED"
        ]
        assert records, "no SKIN_LAYOUT_UNSUPPORTED record"
        record = records[-1]
        fields = " ".join(
            f"{key}={value}" for key, value in record.items() if key != "code"
        )
        return f"{record['code']} {fields}"


class ZZMIDevCommentGatingTests(unittest.TestCase):
    """t149：开发者诊断注释默认不发射 + cull_aware 子句布尔吸收（行为等价）。

    两条不变量：
    1. 配置表默认**不含**开发者诊断注释（开关关闭时 `_merged_diag` /
       `_merged_reuse_site` 返回空串，`_append_merged_diag` 连空行都不追加）；
    2. `_merged_absorb_redundant_seen_clauses` 只删**恒真**的 cull_aware 子句
       （`A && (P || A) ≡ A`），真值表逐点不变。
    """

    def setUp(self):
        self._saved_emit = _zzmi_module.ZZMI_MERGE_DIAG_EMIT

    def tearDown(self):
        _zzmi_module.ZZMI_MERGE_DIAG_EMIT = self._saved_emit

    def _bare_exporter(self):
        return _zzmi_module.ExportZZMI.__new__(_zzmi_module.ExportZZMI)

    def test_default_is_off_when_env_unset(self):
        """开关默认值必须由环境变量决定，且未设时为空集判定 ⇒ 默认关闭。"""
        source = (REPO_ROOT / "ui" / "universal" / "zzmi.py").read_text(
            encoding="utf-8"
        )
        assert 'os.environ.get("ZZMI_MERGE_DIAG", "")' in source, (
            "ZZMI_MERGE_DIAG_EMIT 必须由环境变量 ZZMI_MERGE_DIAG 决定"
        )
        assert "ZZMI_MERGE_DIAG_EMIT = " in source

    def test_diag_off_returns_empty_and_appends_nothing(self):
        _zzmi_module.ZZMI_MERGE_DIAG_EMIT = False
        exporter = self._bare_exporter()
        assert exporter._merged_diag("CODE", "msg", group="G0") == ""
        assert exporter._merged_reuse_site("ib", 0, 1, "publish-cs") == ""
        sink = []
        exporter._append_merged_diag(sink, "")
        exporter._append_merged_diag(sink, "", indent="    ")
        assert sink == [], "开关关闭时不得追加任何行（含空行）"

    def test_diag_on_restores_old_output(self):
        _zzmi_module.ZZMI_MERGE_DIAG_EMIT = True
        exporter = self._bare_exporter()
        line = exporter._merged_diag("CODE", "msg", group="G0")
        assert line.startswith("; ZZMI-MERGE-DIAG CODE"), line
        assert line.endswith("group=G0"), line
        sink = []
        exporter._append_merged_diag(sink, line, indent="    ")
        assert sink == ["    " + line]

    def test_absorb_drops_only_implied_clause(self):
        absorb = _zzmi_module.ExportZZMI._merged_absorb_redundant_seen_clauses
        cond = (
            '($zz_ms_seen_71 >= 1) && ($zz_ms_prev_81 == 0 || $zz_ms_seen_81 >= 1)'
        )
        assert absorb(cond) == (
            "($zz_ms_seen_71 >= 1) && ($zz_ms_prev_81 == 0 || $zz_ms_seen_81 >= 1)"
        )
        # 没有任何原子被断言 ⇒ 一字不改
        lone = "($zz_ms_prev_71 == 0 || $zz_ms_seen_71 >= 1)"
        assert absorb(lone) == lone
        # 单合取项 / 空串 ⇒ 原样返回
        assert absorb("($zz_ms_seen_71 >= 1)") == "($zz_ms_seen_71 >= 1)"
        assert absorb("") == ""

    def test_absorb_is_truth_preserving_exhaustively(self):
        """穷举三变量真值表：吸收前后逐点同值（纯比较，无副作用）。"""
        import itertools

        absorb = _zzmi_module.ExportZZMI._merged_absorb_redundant_seen_clauses
        cond = "($a >= 1) && ($b == 0 || $a >= 1) && ($c == 0 || $b >= 1)"
        new = absorb(cond)
        assert new != cond, "该样例应当发生吸收"
        assert new == "($a >= 1) && ($c == 0 || $b >= 1)"

        def evaluate(expr, env):
            for part in [p.strip() for p in expr.split(" && ")]:
                core = part[1:-1] if part.startswith("(") and part.endswith(")") else part
                ok = False
                for disjunct in [d.strip() for d in core.split(" || ")]:
                    if disjunct.endswith(" >= 1"):
                        ok = ok or env[disjunct[:-5].strip()] >= 1
                    elif disjunct.endswith(" == 0"):
                        ok = ok or env[disjunct[:-5].strip()] == 0
                    else:  # pragma: no cover - 形态受控
                        raise AssertionError(disjunct)
                if not ok:
                    return False
            return True

        for a, b, c in itertools.product([0, 1, 2], repeat=3):
            env = {"$a": a, "$b": b, "$c": c}
            assert evaluate(cond, env) == evaluate(new, env), (a, b, c)


if __name__ == "__main__":
    unittest.main()
