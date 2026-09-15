"""ExportZZMI 合并骨架 INI 生成单测（fake 环境，不依赖 bpy/游戏）。"""

import importlib.util
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


class _FakeIniSection:
    def __init__(self, section_type):
        self.SectionType = section_type
        self.SectionName = ""
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
    # 2026-09-13 新增的两个 ZZMI 开关。这里**显式给 False**：本文件既有的用例
    # 全部锁定"旧行为"（整缺 DrawIB 被引用 → 补占位；表情部件也照常替换几何），
    # 新开关的行为由下面 ZZMIRemovedPartSwitchTests / ZZMIMorphPartTests 覆盖。
    zzmi_removed_parts_keep_original=lambda: False,
    zzmi_morph_parts_keep_geometry=lambda: False,
    zzmi_morph_parts_list=lambda: "",
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

# 段名安全化 + 合并骨架契约判定：**装载真实模块**（2026-09-13）。
# 这两个模块是纯函数、无 bpy 依赖，如果只给空桩，下面的集成用例就验不到
# 真行为；而 zzmi.py 里的 try/except 兜底会让"模块缺失"静默退化 —— 那样
# 这一层安全网就永远不会被测试覆盖。
for _dep_name, _dep_rel in (
    ("ini_name_safety", ("common", "ini_name_safety.py")),
    ("zzmi_merged_contract", ("common", "zzmi_merged_contract.py")),
):
    _dep_path = REPO_ROOT.joinpath(*_dep_rel)
    _dep_spec = importlib.util.spec_from_file_location(
        f"{PKG}.common.{_dep_name}", _dep_path
    )
    _dep_module = importlib.util.module_from_spec(_dep_spec)
    sys.modules[_dep_spec.name] = _dep_module
    _dep_spec.loader.exec_module(_dep_module)

_module_path = REPO_ROOT / "ui" / "universal" / "zzmi.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.ui.universal.zzmi", _module_path)
_zzmi_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _zzmi_module
_spec.loader.exec_module(_zzmi_module)

# 2026-09-15：槽数从 2 扩到 5（见 notes/softbody-m1-status.md §20.9 —— 用户角色
# 一帧里身体/腿各出现 5 次，2 槽会在 `>= 3` 处回绕，第 4/5 次复用第 1/2 次捕获的
# 调色板 ⇒ 接缝差帧裂开）。测试不再写死"2 个槽/回绕 3"，一律从被测模块推导，
# 以后调槽数只需改 `ui/universal/zzmi.py` 里的元组。
_SLOT_COUNT = len(_zzmi_module.ZZMI_MERGED_SKELETON_SLOTS)
_OCC_WRAP = _SLOT_COUNT + 1
_OCC_SLOTS = tuple(_zzmi_module.ZZMI_MERGED_SKELETON_SLOTS)

def _slot_expr(cid, slot):
    """与生成器同一函数取的槽位键（键 = 出现次序），避免两边各写一份。"""
    return _zzmi_module.zzmi_merged_slot_key_expr(cid, slot)

def _occ_draw_regex(slot, skeleton, draw_count):
    """匹配"按本部件第 slot 次出现直接绘制 skeleton（draw = draw_count）"的块（组件号无关）。"""
    return re.compile(
        rf"if \$zz_ms_occ_\d+ == {slot}\n"
        rf"    vs-t0 = {skeleton}\n"
        rf"    draw = {draw_count}, 0\n"
        "endif"
    )


def _occ_cond_index(text, slot):
    """返回"第 slot 次出现"条件在文本中的下标（组件号无关）。"""
    match = re.search(rf"if \$zz_ms_occ_\d+ == {slot}\b", text)
    if not match:
        raise AssertionError(f"未找到第 {slot} 次出现条件")
    return match.start()


def _has_occ_slot(text, slot):
    """文本里是否存在"某个部件的第 slot 次出现"条件（组件号无关，供 fixture 编号不确定处用）。"""
    blob = text if isinstance(text, str) else "\n".join(text)
    return re.search(rf"if \$zz_ms_occ_\d+ == {slot}\b", blob) is not None


def _has_occ_gated_merged_draw(text):
    """是否存在"按本部件出现次直接绘制合并骨架"的条件（用于断言宿主**不得**自足绘制）。"""
    blob = text if isinstance(text, str) else "\n".join(text)
    return re.search(
        r"if \$zz_ms_occ_\d+ == \d+\n    vs-t0 = ResourceZZMergedSkeleton", blob
    ) is not None


def _expected_attach_runs(component_ids, indent=""):
    """按槽数展开的 attach run 列表（外层槽、内层部件；与生成器一致）。"""
    return [
        f"{indent}run = CustomShaderZZMIMergedSkeletonAttach_C{cid}_s{slot}"
        for slot in _OCC_SLOTS
        for cid in component_ids
    ]


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
        idx_occ_wrap = text.index(f"$zz_ms_occ_0 >= {_OCC_WRAP}")
        idx_seen1 = text.index("$zz_ms_seen_01 = $zz_ms_seen_01 + ($zz_ms_occ_0 == 1)")
        idx_seen2 = text.index("$zz_ms_seen_02 = $zz_ms_seen_02 + ($zz_ms_occ_0 == 2)")
        # 槽位键由生成器统一给出，断言只认"palette 捕获这一行"的存在与顺序
        idx_copy = text.index(
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
        exporter.add_merged_skeleton_sections(builder)
        lines = _all_builder_lines(builder)
        text = "\n".join(lines)

        self.assertIn("global $zz_ms_occ_0 = 0", text)
        self.assertIn("global $zz_ms_occ_1 = 0", text)
        self.assertIn("global $zz_ms_seen_01 = 0", text)
        self.assertIn("global $zz_ms_seen_02 = 0", text)
        self.assertIn("global $zz_ms_seen_11 = 0", text)
        self.assertIn("global $zz_ms_seen_12 = 0", text)
        # 每槽一份合并骨架
        self.assertIn("[ResourceZZMergedSkeleton_G0_s1]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G0_s2]", text)
        self.assertIn("type = RWStructuredBuffer", text)
        self.assertIn("stride = 48", text)
        self.assertIn("array = 156", text)  # 全宽 = max(0+105, 105+51) = 156
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
        self.assertIn("[ResourceZZRedirectSO_s1]", text)
        self.assertIn("[ResourceZZRedirectSO_s2]", text)
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
        # [Present] 只清零 occ/seen，不重放 attach、不复位任何资源。
        self.assertIn("[Present]", text)
        present_text = text.split("[Present]")[1]
        self.assertIn("$zz_ms_occ_0 = 0", present_text)
        self.assertIn("$zz_ms_occ_1 = 0", present_text)
        self.assertIn("$zz_ms_seen_01 = 0", present_text)
        self.assertIn("$zz_ms_seen_12 = 0", present_text)
        self.assertNotIn("run = CustomShaderZZMIMergedSkeletonAttach_", present_text)
        self.assertNotIn("$zz_ms_attach_offset", present_text)
        self.assertNotIn("$zz_ms_attach_count", present_text)
        self.assertNotIn("ResourceZZRedirectSO", present_text)
        self.assertNotIn("ResourceZZPalette", present_text)
        self.assertNotIn("ResourceZZMergedSkeleton", present_text)
        self.assertNotIn("= null", present_text)

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
        exporter.add_merged_skeleton_sections(builder)
        lines = _all_builder_lines(builder)
        text = "\n".join(lines)

        self.assertIn("[ResourceZZMergedSkeleton_G0_s1]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G0_s2]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G1_s1]", text)
        self.assertIn("[ResourceZZMergedSkeleton_G1_s2]", text)
        # 两组各两槽、全宽 array = 全局 max(157+11) = 168（只数骨架资源的 array 行；
        # palette 副本资源自带 array=vg_count 行，需排除——骨架段结构：header/type/stride/array）
        skeleton_arrays = [
            lines[i + 3]
            for i, line in enumerate(lines)
            if line.startswith("[ResourceZZMergedSkeleton_G")
        ]
        self.assertEqual(skeleton_arrays, ["array = 168"] * (2 * _SLOT_COUNT))
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

        self.assertIn("array = 51", text)  # max(0+11, 31+20) = 51，而非 sum=31

    def test_g4_slots_use_runtime_merged_skeleton_bounds(self):
        """G4 的 249..265 槽必须由实际 UAV 长度放行，不能被角色专用常量截断。"""
        shader = (REPO_ROOT / "Toolset" / "zzmi_merged_skeleton_attach.hlsl").read_text(
            encoding="utf-8"
        )

        threads = _zzmi_module.ExportZZMI.MERGED_SKELETON_ATTACH_THREADS
        self.assertIn(f"[numthreads({threads}, 1, 1)]", shader)
        self.assertIn(
            "src_palette.GetDimensions(palette_count, palette_stride)", shader
        )
        self.assertIn("vg_map.GetDimensions(vg_map_count)", shader)
        self.assertIn(
            "merged_skeleton.GetDimensions(merged_count, merged_stride)", shader
        )
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
        self.assertEqual(text.count("[ResourceZZMergedSkeleton_G4_s"), _SLOT_COUNT)
        self.assertIn("array = 266", text)
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
        self.assertIn(f"if $zz_ms_occ_1 >= {_OCC_WRAP}", text)
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

        # 本部件（b20f90ea）每槽一条绘制，条件只读**本部件自己的出现次**
        for slot in _OCC_SLOTS:
            self.assertRegex(
                text,
                _occ_draw_regex(slot, f"ResourceZZMergedSkeleton_G0_s{slot}", 4643),
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
            _occ_cond_index(text, 1),
            _occ_cond_index(text, 2),
        )

    def test_guard_body_contains_only_bindings_and_draw(self):
        """v9 硬约束：绘制体内只允许绑定与 draw；不得出现 run、不得给 $变量赋值。

        直连路径自足挂点的绘制体内只有 `vs-t0` 与 `draw`（没有 SO 重定向）。
        """
        exporter, models = self._make_exporter(self._components())
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, models[0])
        lines = _all_builder_lines(builder)

        # 出现次 palette 捕获与自足绘制各有一条"第 1 次"条件
        guard_starts = [
            index for index, line in enumerate(lines)
            if re.match(r"if \$zz_ms_occ_\d+ == 1$", line)
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
            if line.startswith("if $zz_ms_seen_01 >= 1")
        )
        end = lines.index("endif", start)
        body = [line.strip() for line in lines[start + 1 : end]]
        self.assertEqual(
            body,
            [
                "vs-t0 = ResourceZZMergedSkeleton_G0_s1",
                "so0 = ref ResourceZZRedirectSO_s1",
                "vb2 = Resourceb20f90eaBlend",
                "vb0 = Resourceb20f90eaPosition",
                "draw = 4643, 0",
                "so0 = null",
            ],
        )

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
        # 每部件每槽各一条 = 2 部件 * 槽数
        self.assertEqual(len(run_lines), _SLOT_COUNT * 2)

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
            _expected_attach_runs([0, 1]),
        )
        # 绘制门控按**本部件自己**的出现次（v9.1）：每个部件只画自己的几何，
        # 不等组内其它部件——组级 seen 门控只会在最后到达的部件那段成立。
        self.assertRegex(
            text_a, _occ_draw_regex(1, "ResourceZZMergedSkeleton_G0_s1", 12314)
        )
        self.assertRegex(
            text_b, _occ_draw_regex(1, "ResourceZZMergedSkeleton_G0_s1", 4643)
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
        exporter.merged_skeleton_components = [
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
        ]
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
        self.assertTrue(_has_occ_slot(lines, 1))
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
        self.assertRegex(
            text, _occ_draw_regex(1, "ResourceZZMergedSkeleton_G0_s1", 12314)
        )
        self.assertRegex(
            text, _occ_draw_regex(2, "ResourceZZMergedSkeleton_G0_s2", 12314)
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
        self.assertEqual(text.count("    draw = 12314, 0"), _SLOT_COUNT)
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
        exporter.merged_skeleton_components = self._components()
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

        self.assertIn("if $zz_ms_seen_01 >= 1 && $zz_ms_seen_11 >= 1", text)
        self.assertIn("if $zz_ms_seen_02 >= 1 && $zz_ms_seen_12 >= 1", text)
        self.assertFalse(_has_occ_gated_merged_draw(text))

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
        self.assertTrue(_has_occ_slot(text, 1))
        self.assertTrue(_has_occ_slot(text, 2))
        self.assertRegex(
            text, _occ_draw_regex(1, "ResourceZZMergedSkeleton_G0_s1", 4643)
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

    def _make_exporter(self, sibling_blend_stride=32):
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
        exporter.merged_skeleton_components = self._components()
        exporter.merged_skeleton_component_id_dict = {
            "a23aa8a3": 0, "b20f90ea": 1, "b30db54e": 2
        }
        # 宿主的顶点引用了兄弟部件的槽位（105/156）⇒ 几何被吸收 = 合并宿主
        self._register_obj("LOD0.a23aa8a3-42759-0", [(0, 1.0), (105, 1.0), (156, 1.0)])
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
        self.assertEqual(text.count("    ResourceZZRedirectSO_s1 = ref so0"), 1)
        self.assertEqual(text.count("    ResourceZZRedirectSO_s2 = ref so0"), 1)
        # 组级守卫 + 显式绑定 SO/宿主的 vb0/vb2 + 宿主导出顶点数
        self.assertIn(
            "if $zz_ms_seen_01 >= 1 && $zz_ms_seen_11 >= 1 && $zz_ms_seen_21 >= 1\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n"
            "    so0 = ref ResourceZZRedirectSO_s1\n"
            "    vb2 = Resourcea23aa8a3Blend\n"
            "    vb0 = Resourcea23aa8a3Position\n"
            "    draw = 12482, 0\n"
            "    so0 = null\n"
            "endif",
            text,
        )
        # 宿主的几何**不得**自足绘制（它跨部件、必须等全组到位）
        self.assertFalse(_has_occ_gated_merged_draw(text))

    def test_compatible_sibling_also_replays_host_geometry(self):
        """关键回归：布局兼容的兄弟挂点也要发同一条重放（与提交顺序无关）。"""
        exporter, models = self._make_exporter()
        host_text = self._vb_text(exporter, models[0])
        sib_text = self._vb_text(exporter, models[1])

        # 兄弟挂点：先自足画自己的几何（3 顶点占位）……
        self.assertRegex(
            sib_text,
            _occ_draw_regex(1, "ResourceZZMergedSkeleton_G0_s1", 3),
        )
        # ……再发宿主合并几何的组级守卫重放（宿主排在前面的帧由它闭合）
        self.assertIn(
            "if $zz_ms_seen_01 >= 1 && $zz_ms_seen_11 >= 1 && $zz_ms_seen_21 >= 1\n"
            "    vs-t0 = ResourceZZMergedSkeleton_G0_s1\n"
            "    so0 = ref ResourceZZRedirectSO_s1\n"
            "    vb2 = Resourcea23aa8a3Blend\n"
            "    vb0 = Resourcea23aa8a3Position\n"
            "    draw = 12482, 0\n"
            "    so0 = null\n"
            "endif",
            sib_text,
        )
        self.assertIn("so0 = ref ResourceZZRedirectSO_s2", sib_text)
        # 宿主段不能捕获兄弟的 SO：捕获只在宿主自己段发生
        self.assertNotIn("ResourceZZRedirectSO_s1 = ref so0", sib_text)
        self.assertIn("ResourceZZRedirectSO_s1 = ref so0", host_text)

    def test_incompatible_sibling_does_not_replay(self):
        """Blend 布局不兼容的兄弟挂点不得重放（BI4 与 BW16_BI16 不能混用）。"""
        exporter, models = self._make_exporter(sibling_blend_stride=16)
        sib_text = self._vb_text(exporter, models[1])
        # 自己那段照旧自足绘制
        self.assertIn("    draw = 3, 0", sib_text)
        # 但不发宿主重放（否则会把权重按错误格式解释 → 流输出全零）
        self.assertNotIn("so0 = ref ResourceZZRedirectSO", sib_text)
        self.assertNotIn("vb0 = Resourcea23aa8a3Position", sib_text)

    def test_host_without_compatible_sibling_prints_diagnostic(self):
        """没有兼容兄弟时大声报警（该组合仍依赖提交顺序，需要开发者介入）。"""
        import contextlib
        import io

        exporter, models = self._make_exporter(sibling_blend_stride=16)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._vb_text(exporter, models[0])
        self.assertIn("没有 Blend 布局兼容的其它挂点", buf.getvalue())


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

    def _write_vgmap_json(self, bare, gid, group=None, excluded=False, original_vertex_count=None):
        type_dir = os.path.join(self.tmp, "LOD0", bare, "TYPE_GPU_TEST_")
        os.makedirs(type_dir, exist_ok=True)
        payload = {"VGMap": {"0": str(gid)}, "VGOffset": 0, "VGCount": 1}
        if group is not None:
            payload["SkeletonGroup"] = group
        if excluded:
            payload["VGMapDedupExcluded"] = True
        # 导入时记录的原部件顶点数：用于区分"join 合并(宿主变大)"与"删除部件"
        if original_vertex_count is not None:
            payload["OriginalVertexCount"] = int(original_vertex_count)
        with open(os.path.join(type_dir, bare + ".json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _register_present_object_with_groups(self, name, used_gids, group_names=None, extra_vertices=0):
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
        # extra_vertices：模拟"join 合并把别的部件顶点并进宿主"后宿主顶点数变大
        for _ in range(int(extra_vertices or 0)):
            mesh.vertices.append(
                types.SimpleNamespace(
                    groups=[types.SimpleNamespace(group=0, weight=1.0)]
                )
            )
        return obj

    def test_stub_when_absent_drawib_absorbed_into_other_object(self):
        # 84618ee0 全缺，但其 VGMap 全局 id=7 被现存对象（b20f90ea）的顶点引用 = 被合并
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        # ★ 2026-09-14：join 合并的判据是"宿主顶点数变大"（删除部件不会变大）
        #   ⇒ 这里显式建模：宿主（b20f90ea）json 记录 OriginalVertexCount=1，
        #     而现存对象有 2 个顶点（extra_vertices=1）。
        self._write_vgmap_json("b20f90ea-19182-0", 7, original_vertex_count=1)
        self._register_present_object_with_groups(
            "LOD0.b20f90ea-19182-0", [7], extra_vertices=1
        )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        # 宿主 b20f90ea 导入时 1 个顶点、现在 2 个 ⇒ 判据认定为"被 join 合并"
        models = [
            _FakeDrawIBModel(
                "b20f90ea",
                [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51, original_vertex_count=1)],
            )
        ]
        exporter = _make_exporter(models, merged_vgmap=True, ordered_drawcalls=ordered)

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

    def test_stub_when_present_object_borrows_only_missing_drawib_bones(self):
        """回归（2026-09-14 用户实测：join 合并上下半身后下半身整块消失）。

        真实数据：身体 json OriginalVertexCount=10859、现存 92870 顶点，其顶点引用的
        26 个全局槽 [209..254] 是身体自己的 VGMap 盖不住的，全部属于被并掉的
        DrawIB 4a178546（同骨架组 G2、47 槽）——没有任何部件能把它们写进合并骨架。

        本用例把"宿主顶点数不变"单独建模出来，专门压 foreign 判据本身，并顺带压
        `_copy` 对象名（导出期对象名 = `LOD0.<submesh>.<部件名>_copy`，不能按对象名
        切分子网格）。
        """
        # 缺席 DrawIB：全局槽 200（宿主自己的 VGMap 里没有）
        self._write_vgmap_json("84618ee0-22296-0", 200)
        self._write_vgmap_json("84618ee0-1164-22296", 200)
        # 宿主：导入期 VGMap 只有槽 7，但现存顶点引用了 7 和外来槽 200
        host_dir = os.path.join(self.tmp, "LOD0", "b20f90ea-19182-0", "TYPE_GPU_TEST_")
        os.makedirs(host_dir, exist_ok=True)
        with open(
            os.path.join(host_dir, "b20f90ea-19182-0.json"), "w", encoding="utf-8"
        ) as f:
            json.dump({"VGMap": {"0": "7"}, "VGOffset": 0, "VGCount": 1,
                       "OriginalVertexCount": 2}, f)
        self._register_present_object_with_groups(
            "LOD0.b20f90ea-19182-0.身体_copy", [7, 200]
        )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0.身体_copy")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertIn("LOD0.84618ee0-22296-0", names)
        self.assertIn("LOD0.84618ee0-1164-22296", names)
        self.assertEqual(len(exporter._zzmi_stub_object_names), 2)
        exporter._cleanup_stub_objects()

    def test_no_stub_when_present_object_only_shares_own_bones(self):
        """幸存部件只引用双方共享的槽（自己 VGMap 里也有）⇒ 不是"被 join 合并"，不插桩。

        这正是"删掉脸"那条历史路径：头发引用的槽在自己的 VGMap 里 ⇒ 不补占位
        ⇒ 游戏继续画原版脸。
        """
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        self._write_vgmap_json("b20f90ea-19182-0", 7, original_vertex_count=1)
        self._register_present_object_with_groups("LOD0.b20f90ea-19182-0", [7])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)

        names = [str(dc.get_workspace_unique_str()) for dc in ordered]
        self.assertNotIn("LOD0.84618ee0-22296-0", names)
        self.assertEqual(exporter._zzmi_stub_object_names, [])

    def test_absorption_uses_numeric_vertex_group_name_not_blender_index(self):
        """替换模型组名稀疏时，吸收判定必须读取组名而不是内部索引。"""
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        # 宿主 json 记录 OriginalVertexCount=1 + 现存 2 顶点 ⇒ 判为"join 合并"
        self._write_vgmap_json("b20f90ea-19182-0", 7, original_vertex_count=1)
        self._register_present_object_with_groups(
            "LOD0.b20f90ea-19182-0",
            [7],
            group_names=["unused", "7"],
            extra_vertices=1,
        )

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        models = [
            _FakeDrawIBModel(
                "b20f90ea",
                [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51, original_vertex_count=1)],
            )
        ]
        exporter = _make_exporter(models, merged_vgmap=True, ordered_drawcalls=ordered)

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

    def test_switch_on_keeps_original_for_absorbed_absent_drawib(self):
        """开关勾选（默认）：整缺且骨骼被引用的 DrawIB 也不插桩 —— 游戏画原版。

        用户实测场景：删掉「脸」以后，脸是整缺 DrawIB，但它的一部分骨骼（槽 0 等）
        仍被头发/头饰那些幸存部件引用，旧逻辑据此补了隐形占位 → 原版脸被顶掉、
        脸上什么都没有。勾选本开关后必须**不插桩**。
        """
        self._write_vgmap_json("84618ee0-22296-0", 7)
        self._write_vgmap_json("84618ee0-1164-22296", 7)
        self._register_present_object_with_groups("LOD0.b20f90ea-19182-0", [7])

        dcm = sys.modules[f"{PKG}.common.draw_call_model"].DrawCallModel
        ordered = [dcm(obj_name="LOD0.b20f90ea-19182-0")]
        # 注意：占位对象是在 ExportZZMI.__init__ 里创建的，开关必须在构造前设好。
        _fake_global_properties.zzmi_removed_parts_keep_original = lambda: True
        try:
            exporter = _make_exporter([], merged_vgmap=True, ordered_drawcalls=ordered)
            names = [str(dc.get_workspace_unique_str()) for dc in ordered]
            self.assertNotIn("LOD0.84618ee0-22296-0", names)
            self.assertNotIn("LOD0.84618ee0-1164-22296", names)
            self.assertEqual(exporter._zzmi_stub_object_names, [])
        finally:
            _fake_global_properties.zzmi_removed_parts_keep_original = lambda: False
            exporter._cleanup_stub_objects()


class ZZMIMorphPartTests(unittest.TestCase):
    """表情部件（脸）走『只换贴图不改几何』（2026-09-13）。

    背景证据（用户机器上的抓帧 log.txt）：
        000006 CopyResource(Dst=池 41554b66, Src=基础顶点 31aa5dc2)
        000006 CSSetShader(743108cc03f39cbf); Dispatch(10,1,1)      ← 算表情
        000010 CopyResource(Dst=变形 vb0 153d04c7, Src=池)          ← 结果进顶点缓冲
    导出器原来把 153d04c7 的 vb0 换成 mod 静态顶点 ⇒ 表情整帧被丢掉。
    """

    def setUp(self):
        _fake_global_properties.zzmi_morph_parts_keep_geometry = lambda: True
        _fake_global_properties.zzmi_morph_parts_list = lambda: ""
        _fake_global_properties.zzmi_morph_parts_texture_only = lambda: False

    def tearDown(self):
        _fake_global_properties.zzmi_morph_parts_keep_geometry = lambda: False
        _fake_global_properties.zzmi_morph_parts_list = lambda: ""
        _fake_global_properties.zzmi_morph_parts_texture_only = lambda: False

    def _morph_exporter(self):
        submesh = _FakeSubmesh("LOD0.c28e6303-7308-0", 0, 10)
        submesh.match_cs = "743108cc03f39cbf"
        model = _FakeDrawIBModel("c28e6303", [submesh])
        model.draw_ib_alias = "脸"
        exporter = _make_exporter([model], merged_vgmap=True)
        return exporter, model

    def test_morph_part_skips_deform_and_vertex_limit_sections(self):
        exporter, model = self._morph_exporter()
        self.assertTrue(exporter._is_morph_part("c28e6303"))

        vb_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        self.assertEqual(
            _all_builder_lines(vb_builder), [],
            "表情部件不得生成变形阶段的 vb0/vb2 覆写段（会把表情顶掉）",
        )

        vlr_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vlr_section(vlr_builder, model)
        self.assertEqual(
            _all_builder_lines(vlr_builder), [],
            "表情部件不得改变游戏顶点缓冲分配大小",
        )

        ib_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(ib_builder, model)
        self.assertEqual(
            _all_builder_lines(ib_builder), [],
            "表情部件整份交回游戏：IB/贴图段也不得生成",
        )

    def test_texture_only_mode_emits_ib_and_texture_but_no_geometry(self):
        """第 1 层需求：只换贴图 —— 变形段不发，渲染段（IB/贴图）照发。"""
        _fake_global_properties.zzmi_morph_parts_texture_only = lambda: True
        exporter, model = self._morph_exporter()

        vb_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(vb_builder, model)
        self.assertEqual(
            _all_builder_lines(vb_builder), [],
            "只换贴图模式下仍不得生成变形阶段覆写段",
        )

        ib_builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(ib_builder, model)
        lines = _all_builder_lines(ib_builder)
        self.assertTrue(lines, "只换贴图模式必须生成渲染/贴图段")
        self.assertTrue(
            any(line.startswith("[TextureOverride_IB_c28e6303]") for line in lines)
        )

    def test_morph_part_excluded_from_merged_skeleton(self):
        exporter, _model = self._morph_exporter()
        components, id_dict = exporter._collect_merged_skeleton_components()
        self.assertEqual(components, [])
        self.assertEqual(id_dict, {})

    def test_manual_list_can_mark_part_without_match_cs(self):
        submesh = _FakeSubmesh("LOD0.c28e6303-7308-0", 0, 10)
        model = _FakeDrawIBModel("c28e6303", [submesh])
        model.draw_ib_alias = "脸_copy"
        exporter = _make_exporter([model], merged_vgmap=True)
        # 自动判断拿不到 match_cs 时，手填名字片段照样命中
        _fake_global_properties.zzmi_morph_parts_list = lambda: "脸"
        exporter._morph_draw_ibs_cache = None
        self.assertTrue(exporter._is_morph_part("c28e6303"))

    def test_switch_off_keeps_old_geometry_replacement(self):
        _fake_global_properties.zzmi_morph_parts_keep_geometry = lambda: False
        exporter, model = self._morph_exporter()
        exporter._morph_draw_ibs_cache = None
        self.assertFalse(exporter._is_morph_part("c28e6303"))
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        lines = _all_builder_lines(builder)
        self.assertTrue(any("Resourcec28e6303Position" in line for line in lines))


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
                         target_registered=False, cross_ib=()):
        """构造用户实测场景（合并网格挂最早 draw 的 b20f90ea）的 exporter。"""
        self._register_obj("LOD0.b20f90ea-19182-0", [79, 88, 105, 229, 248])
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

    def test_redirect_target_with_own_geometry_offsets(self):
        """target（a23aa8a3）自身还有真实几何：合并网格 base_vertex = 其 SO 偏移。"""
        exporter, _models = self._group3_exporter(
            merged_vertex_count=18776, target_real_vertices=12314,
            target_registered=True,
        )
        carrier_map, target_map, unredirected = self._build_and_apply_plan(exporter)
        self.assertEqual(carrier_map["b20f90ea"]["base_vertex"], 12314)
        self.assertEqual(target_map["a23aa8a3"]["so_vertex_count"], 12314 + 18776)
        self.assertEqual(target_map["a23aa8a3"]["target_own_vertices"], 12314)
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
        self.assertIn("so0 = ref ResourceZZRedirectSO_s1", text_b)
        # SO owner = carrier：两个槽各捕获一次
        self.assertIn("ResourceZZRedirectSO_s1 = ref so0", text_b)
        self.assertIn("ResourceZZRedirectSO_s2 = ref so0", text_b)
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
        self.assertIn("so0 = ref ResourceZZRedirectSO_s1", text_a)
        self.assertIn("so0 = ref ResourceZZRedirectSO_s2", text_a)
        self.assertNotIn("ResourceZZRedirectSO_s1 = ref so0", text_a)

        # 同组的第三个部件 b30db54e（布局兼容）：v9 起同样发每槽守卫（触发时机可能
        # 落在它的 deform 段；重复重放同槽是幂等写入），绑定的是 carrier 的 vb0/vb2、
        # 写的是同一槽 SO 引用。
        builder_c = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_c, models[2])
        text_c = "\n".join(builder_c.sections[0].SectionLineList)
        self.assertIn("run = CustomShaderZZMIMergedSkeletonAttach_C2_s1", text_c)
        self.assertIn("draw = 18776, 0", text_c)
        self.assertIn("vb0 = Resourceb20f90eaPosition", text_c)
        self.assertIn("if $zz_ms_seen_01 >= 1 && $zz_ms_seen_11 >= 1 && $zz_ms_seen_21 >= 1", text_c)
        self.assertIn("if $zz_ms_seen_02 >= 1 && $zz_ms_seen_12 >= 1 && $zz_ms_seen_22 >= 1", text_c)

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
        self.assertNotIn("ResourceZZRedirectSO_s1 = ref so0", text_target)
        # 不兼容宿主不产生重放守卫（也不再有相位/闩锁条件）
        self.assertNotIn("ResourceZZRedirectSO_s1", text_target)
        self.assertNotIn("$zz_ms_group_phase", text_target)
        self.assertNotIn("$zz_ms_redirect_drawn", text_target)
        self.assertNotIn("$zz_ms_group_ready", text_target)

        # carrier（b20f90ea）是唯一兼容的重放宿主：它自己承载直连守卫重放合并
        # 几何（同一套槽门控），绝不出现"两个挂点都不画"把合并几何整个丢掉。
        builder_carrier = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder_carrier, models[0])
        text_carrier = "\n".join(builder_carrier.sections[0].SectionLineList)
        self.assertIn("ResourceZZRedirectSO_s1 = ref so0", text_carrier)
        self.assertIn("if $zz_ms_seen_01 >= 1 && $zz_ms_seen_11 >= 1", text_carrier)
        self.assertIn("draw = 18776, 0", text_carrier)
        self.assertNotIn("draw = 3, 0", text_carrier)

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
        """真实 target 与 carrier 的 Blend 布局不同，不能把整段重放伪装成兼容。"""
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
            "incompatible-blend-layout",
        )
        warning = self._capture_stdout(lambda: exporter._warn_merged_mesh_timing(unredirected))
        self.assertIn("Blend 输入布局不兼容", warning)

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
            self.assertIn(f"if $zz_ms_occ_{cid} >= {_OCC_WRAP}", vb_text)
            self.assertIn(f"    $zz_ms_occ_{cid} = 1", vb_text)
        # 2) 到达标记顶层 sticky 累加
        for cid in range(3):
            for slot in _OCC_SLOTS:
                self.assertIn(
                    f"$zz_ms_seen_{cid}{slot} = $zz_ms_seen_{cid}{slot}"
                    f" + ({_slot_expr(cid, slot)})",
                    vb_text,
                )
        # 3) 按槽捕获 palette；SO 捕获只在 owner（carrier b20f90ea）段
        self.assertTrue(_has_occ_slot(vb_text, 1))
        self.assertIn(
            "    ResourceZZPalette_b20f90ea_s1 = copy vs-t0 unless_null", vb_text
        )
        self.assertIn(
            "    ResourceZZPalette_b20f90ea_s2 = copy vs-t0 unless_null", vb_text
        )
        self.assertNotIn("ResourceZZPalette_a23aa8a3_s1 = copy vs-t0 unless_null\n    ResourceZZRedirectSO", vb_text)
        # 4) 每槽守卫条件 = 本槽轮次条件 && 组内全部部件 seen 相与
        for slot in _OCC_SLOTS:
            self.assertIn(
                f"if $zz_ms_seen_0{slot} >= 1"
                f" && $zz_ms_seen_1{slot} >= 1 && $zz_ms_seen_2{slot} >= 1",
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
                for slot in _OCC_SLOTS
            ),
        )
        # 6) 守卫体内只有绑定与 draw（守卫行现在带轮次条件前缀）
        for slot in _OCC_SLOTS:
            lines = vb_text.splitlines()
            guard_prefix = f"if $zz_ms_seen_0{slot} >= 1"
            start = next(
                i for i, line in enumerate(lines)
                if line.startswith(guard_prefix)
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
        self.assertIn("[ResourceZZRedirectSO_s1]", skeleton_text)
        self.assertIn("[ResourceZZRedirectSO_s2]", skeleton_text)
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
        self.assertIn("[ResourceZZRedirectSO_s1]", skeleton_text)
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
        self.assertEqual(draw_count, _SLOT_COUNT * 3)
        for slot_cond in (
            "if $zz_ms_seen_01 >= 1 && $zz_ms_seen_11 >= 1 && $zz_ms_seen_21 >= 1",
            "if $zz_ms_seen_02 >= 1 && $zz_ms_seen_12 >= 1 && $zz_ms_seen_22 >= 1",
        ):
            self.assertIn(slot_cond, vb_text)
        # SO 引用按槽分别捕获（carrier 段），target 段按槽分别绑定
        group_b = next(m for m in models if m.draw_ib == "b20f90ea")
        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, group_b)
        b_text = "\n".join(_all_builder_lines(builder))
        self.assertIn("ResourceZZRedirectSO_s1 = ref so0", b_text)
        self.assertIn("ResourceZZRedirectSO_s2 = ref so0", b_text)
        # 回归：**载体段也必须发守卫**（只让单挂点持有守卫时，该挂点先 deform 的帧
        # 里守卫永不触发 → 该槽 SO 只剩 3 顶点前缀 → 合并几何整段消失）
        self.assertIn(
            "if $zz_ms_seen_01 >= 1 && $zz_ms_seen_11 >= 1 && $zz_ms_seen_21 >= 1",
            b_text,
        )
        self.assertIn("    draw = 18776, 0", b_text)
        self.assertIn("if $zz_ms_seen_02 >= 1 && $zz_ms_seen_12 >= 1 && $zz_ms_seen_22 >= 1", b_text)

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


class ZZMISectionNameSafetyTests(unittest.TestCase):
    """段名安全化真的接进了 ZZMI 导出器（不是只加了模块）。"""

    def test_sanitizer_module_is_the_real_one(self):
        # 如果 zzmi.py 走了 try/except 兜底，这里会失败——正好当作"模块图没接上"的哨兵。
        self.assertEqual(
            _zzmi_module.sanitize_section_name_part("头饰[丝带]"), "头饰_丝带_"
        )

    def test_bracket_alias_emits_single_bracket_section_names(self):
        submesh = _FakeSubmesh("LOD0.ae840e72-1446-0", 0, 5)
        model = _FakeDrawIBModel("ae840e72", [submesh])
        model.draw_ib_alias = "头饰[丝带]"
        exporter = _make_exporter([model], merged_vgmap=True)
        exporter.merged_skeleton_components = []
        exporter.merged_skeleton_component_id_dict = {}

        builder = _FakeIniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        headers = [
            line
            for section in builder.sections
            for line in section.SectionLineList
            if line.startswith("[") and line.endswith("]")
        ]
        self.assertTrue(headers)
        for header in headers:
            self.assertEqual(header.count("["), 1, header)
            self.assertEqual(header.count("]"), 1, header)
        self.assertIn(
            "[TextureOverride_VB_ae840e72_头饰_丝带__Position]", headers
        )

    def test_two_bracket_parts_do_not_collide(self):
        model_a = _FakeDrawIBModel(
            "ae840e72", [_FakeSubmesh("LOD0.ae840e72-1446-0", 0, 5)]
        )
        model_a.draw_ib_alias = "头饰[丝带]"
        model_b = _FakeDrawIBModel(
            "9258d5f8", [_FakeSubmesh("LOD0.9258d5f8-4578-0", 5, 5)]
        )
        model_b.draw_ib_alias = "头饰[丝带1]"
        exporter = _make_exporter([model_a, model_b], merged_vgmap=True)
        exporter.merged_skeleton_components = []
        exporter.merged_skeleton_component_id_dict = {}

        names = []
        for model in (model_a, model_b):
            builder = _FakeIniBuilder()
            exporter.add_unity_vs_texture_override_vb_sections(builder, model)
            names.extend(
                line
                for section in builder.sections
                for line in section.SectionLineList
                if line.startswith("[TextureOverride_VB_")
            )
        self.assertEqual(len(names), len(set(names)), names)


class ZZMIMergedContractGuardTests(unittest.TestCase):
    """导出前守卫：契约不满足必须抛错中止，不能静默产出坏 mod。"""

    def test_raises_when_workspace_has_data_but_checkbox_off(self):
        models = [
            _FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51)])
        ]
        exporter = _make_exporter(models, merged_vgmap=False)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        self.assertEqual(exporter._merged_parts_with_data, 1)
        with self.assertRaises(RuntimeError) as ctx:
            exporter._enforce_merged_skeleton_contract()
        self.assertIn("关闭", str(ctx.exception))

    def test_raises_when_every_component_is_rejected(self):
        # VGMap 不完整覆盖 0..50 ⇒ 收集阶段整部件拒绝，但工作空间确实带数据。
        bad_map = {0: 1}
        models = [
            _FakeDrawIBModel(
                "b20f90ea",
                [_FakeSubmesh("LOD0.b20f90ea-19182-0", 154, 51, vg_map=bad_map)],
            )
        ]
        exporter = _make_exporter(models, merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        self.assertEqual(exporter.merged_skeleton_components, [])
        self.assertTrue(exporter._merged_skip_reasons)
        with self.assertRaises(RuntimeError) as ctx:
            exporter._enforce_merged_skeleton_contract()
        self.assertIn("全部被导出器拒绝", str(ctx.exception))
        self.assertIn("b20f90ea", str(ctx.exception))

    def test_healthy_merge_passes_the_guard(self):
        models = [
            _FakeDrawIBModel("84618ee0", [_FakeSubmesh("LOD0.84618ee0-22296-0", 105, 49)]),
            _FakeDrawIBModel("a23aa8a3", [_FakeSubmesh("LOD0.a23aa8a3-42759-0", 0, 105)]),
        ]
        exporter = _make_exporter(models, merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        self.assertTrue(exporter.merged_skeleton_components)
        exporter._enforce_merged_skeleton_contract()  # 不抛
        self.assertEqual(exporter._merged_contract["level"], "ok")

    def test_no_merged_data_is_a_notice_not_an_error(self):
        models = [_FakeDrawIBModel("b20f90ea", [_FakeSubmesh("LOD0.b20f90ea-19182-0")])]
        exporter = _make_exporter(models, merged_vgmap=True)
        exporter.merged_skeleton_components, exporter.merged_skeleton_component_id_dict = (
            exporter._collect_merged_skeleton_components()
        )
        exporter._enforce_merged_skeleton_contract()  # 不抛
        self.assertEqual(exporter._merged_contract["level"], "notice")


if __name__ == "__main__":
    unittest.main()
