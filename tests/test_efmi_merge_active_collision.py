"""EFMI 合并骨架运行时、单池、布局与事务写出的回归测试。

使用 fake-bpy 加载真实 ``ui/universal/efmi.py``。``$active<N>`` 是 DrawIB、
交换与动画节点共同遵守的角色激活协议，不是需要命名空间隔离的独立变量；这里
只验证真正的合并骨架不变量。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_efmi_merged_skeleton_runtime_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for pkg_name in (PKG, f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.blueprint",
                 f"{PKG}.common", f"{PKG}.utils"):
    _pkg = _install_module(pkg_name)
    _pkg.__path__ = []

# --- 轻量 fake：efmi.py 依赖的其余模块名（绝不实例化重类）---
_install_module("bpy", data=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.json_utils",
    JsonUtils=_load_real(f"{PKG}.utils.json_utils_", "utils/json_utils.py").JsonUtils,
)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None
    ),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        import_merged_vgmap=lambda: True, forbid_auto_texture_ini=lambda: False
    ),
)
_install_module(
    f"{PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
# 真实 m_ini_builder（纯 python）；efmi.py 从 ...common.m_ini_builder 导入，故直接用真模块。
_real_ini_builder = _load_real(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
M_IniBuilder = _real_ini_builder.M_IniBuilder
M_IniSection = _real_ini_builder.M_IniSection
M_SectionType = _real_ini_builder.M_SectionType
_install_module(f"{PKG}.common.m_ini_helper", M_IniHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
# 重类只提供类名，不触发其 __post_init__
_install_module(f"{PKG}.blueprint.model", BluePrintModel=object)
_install_module(f"{PKG}.common.submesh_model", SubMeshModel=object)
_install_module(f"{PKG}.common.drawib_model", DrawIBModel=object)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.ui.universal.export_helper", ExportHelper=types.SimpleNamespace())

# --- 真实 efmi.py ---
_efmi = _load_real(f"{PKG}.ui.universal.efmi", "ui/universal/efmi.py")
ExportEFMI = _efmi.ExportEFMI


class _FakeGameType:
    def get_blendindices_layouts(self):
        return [(0, "R16G16B16A16_UINT", "vb2")]


class _FakeSubmesh:
    def __init__(self, unique_str):
        self.unique_str = unique_str
        self.d3d11_game_type = _FakeGameType()


def _make_merge_exporter(unique_str="LOD0.aaa-0", vg_offset=0, vg_count=50):
    """构造一个免 __post_init__ 的最小 ExportEFMI，只填 _add_merged_skeleton_section 需要的东西。"""
    exporter = object.__new__(ExportEFMI)
    exporter.submesh_model_list = [_FakeSubmesh(unique_str)]
    exporter.merged_skeleton_components = [
        {
            "unique_str": unique_str,
            "lod": "",
            "vg_offset": vg_offset,
            "vg_count": vg_count,
            "draws": [
                {"unique_str": unique_str, "lod_level": 0, "remap": None}
            ],
        }
    ]
    return exporter


def _make_multi_lod_exporters():
    """两个 LOD 的部件（LOD0 段在前、LOD1 段后），用于断言单池结构。"""
    exporter = object.__new__(ExportEFMI)
    exporter.submesh_model_list = [
        _FakeSubmesh("LOD0.aaa-0"),
        _FakeSubmesh("LOD1.bbb-0"),
    ]
    exporter.merged_skeleton_components = [
        {
            "unique_str": "LOD0.aaa-0",
            "lod": "LOD0",
            "vg_offset": 0,
            "vg_count": 50,
            "draws": [
                {"unique_str": "LOD0.aaa-0", "lod_level": 0, "remap": None},
                {"unique_str": "LOD1.bbb-0", "lod_level": 1, "remap": list(range(50))},
            ],
        },
        {
            "unique_str": "LOD1.extra-0",
            "lod": "LOD1",
            "vg_offset": 50,
            "vg_count": 40,
            "draws": [
                {"unique_str": "LOD1.extra-0", "lod_level": 1, "remap": None},
            ],
        },
    ]
    exporter.has_merged_skeleton = True
    exporter.merged_skeleton_component_id_dict = {
        "LOD0.aaa-0": 0,
        "LOD1.bbb-0": 0,
        "LOD1.extra-0": 1,
    }
    return exporter


class _FakePreSkindSubmesh:
    def __init__(self, unique_str, vg_offset, vg_count, match_draw_ib="", ref_component="",
                 corr=None, match_index_count="1", match_first_index="0",
                 layouts=(0, "R16G16B16A16_UINT", "vb2"), vg_map=None,
                 layout_version=13):
        self.unique_str = unique_str
        self.vg_offset = vg_offset
        self.vg_count = vg_count
        self.merged_skeleton_metadata_valid = True
        self.d3d11_game_type = types.SimpleNamespace(
            GPU_PreSkinning=True,
            get_blendindices_layouts=lambda: [layouts],
        )
        self.match_draw_ib = match_draw_ib
        self.match_index_count = match_index_count
        self.match_first_index = match_first_index
        self.efmi_lod_reference_component = ref_component
        self.efmi_lod_correspondence = corr or {}
        self.efmi_lod_layout_version = layout_version
        self.vg_map = vg_map or {}


def _merge_section_lines(exporter):
    builder = M_IniBuilder()
    exporter._add_merged_skeleton_section(builder, command_lists_section=None)
    # 真实 M_IniBuilder 把非空 section 存进 ini_section_list
    if not builder.ini_section_list:
        raise AssertionError("expected non-empty section list")
    return "\n".join(
        line
        for section in builder.ini_section_list
        for line in (
            ([f"[{section.SectionName}]"] if section.SectionName else [])
            + section.SectionLineList
        )
    )


class EFMIMergeRuntimeTests(unittest.TestCase):
    def test_merge_section_emits_globals(self):
        """真实 efmi._add_merged_skeleton_section 发出合并侧全局 $component_count/$bones_count。"""
        lines = _merge_section_lines(_make_merge_exporter())
        self.assertIn("global $component_count = 1", lines)
        self.assertIn("global $bones_count = 50", lines)

class EFMIMergedSkeletonSinglePoolTests(unittest.TestCase):
    """2026-08-28 单池改版：多 LOD 组件共用一套骨架缓冲/池/粘合层（对齐参考插件 mod.ini.j2）。

    去重/投影数据（VGMap/VGOffset 分段平移）保持不变，仅运行时配置合并为单套：
    组件 id 全局递增、bones_count = 全池 max(vg_offset+vg_count)，
    Initialize 逐组件写全局槽位。任何 `_LOD0/_LOD1` 后缀残留都视为回归。
    """

    def _merge_lines(self, exporter, with_glue=False):
        builder = M_IniBuilder()
        glue = M_IniSection(M_SectionType.CommandList)
        exporter._add_merged_skeleton_section(builder, command_lists_section=glue if with_glue else None)
        lines = "\n".join(
            line
            for section in builder.ini_section_list
            for line in (
                ([f"[{section.SectionName}]"] if section.SectionName else [])
                + section.SectionLineList
            )
        )
        if with_glue:
            lines += "\n" + "\n".join(glue.SectionLineList)
        return lines

    def test_multi_lod_single_pool_no_suffixes(self):
        """多 LOD 只生成一套 Pool/RW/CommandList，无 `_LOD*` 后缀（资源名含 unique_str 除外）。"""
        lines = self._merge_lines(_make_multi_lod_exporters(), with_glue=True)
        self.assertEqual(lines.count("[Pool_MergedSkeleton_Component_VertexGroupOffsets]"), 1)
        self.assertEqual(lines.count("[Pool_MergedSkeleton_Component_VertexGroupCounts]"), 1)
        self.assertEqual(lines.count("[Pool_MergedSkeleton_Component_LodRemaps]"), 1)
        self.assertEqual(lines.count("[Pool_MergedSkeleton_Instance_UpdateFrame]"), 1)
        self.assertEqual(lines.count("[Pool_MergedSkeleton_Instance_LodLevel]"), 1)
        self.assertEqual(lines.count("[ResourceMergedSkeletonDataRW]"), 1)
        self.assertEqual(lines.count("[CommandList_MergedSkeleton_ConnectComponent]"), 1)
        self.assertEqual(lines.count("[CommandListInitializeMergedSkeleton]"), 1)
        self.assertEqual(lines.count("[CommandList_Component_DrawInstances]"), 1)
        # 任何段头（[ ... ]）都不允许带 _LOD 后缀（单池无逐 LOD 段）
        for line in lines.splitlines():
            if line.startswith("[") and line.rstrip().endswith("]"):
                self.assertNotIn("_LOD", line, f"发现带 LOD 后缀的段头: {line}")

    def test_merged_skeleton_rwbuffer_declares_bind_flags(self):
        """[ResourceMergedSkeletonDataRW] 必须声明 bind_flags。

        RWBuffer 缺 bind_flags 会导致资源创建失败（与 ResourceLLBakeRT 同类坑）；
        缺失即 RW 绑定失效 → 合并骨架写不进去。
        """
        lines = self._merge_lines(_make_multi_lod_exporters())
        block = lines.split("[ResourceMergedSkeletonDataRW]", 1)[1]
        section_body = block.split("\n[", 1)[0]
        self.assertIn("type = RWBuffer", section_body)
        self.assertIn("bind_flags = shader_resource unordered_access", section_body)
        # 顺序：type → format → bind_flags → array
        self.assertLess(
            section_body.index("format = R32G32B32A32_FLOAT"),
            section_body.index("bind_flags = shader_resource unordered_access"),
        )
        self.assertLess(
            section_body.index("bind_flags = shader_resource unordered_access"),
            section_body.index("array = "),
        )

    def test_multi_lod_global_bones_count_and_offsets(self):
        """bones_count = 全池 max(vg_offset+vg_count)；Initialize 写全局组件 id 及其槽位。"""
        lines = self._merge_lines(_make_multi_lod_exporters())
        self.assertIn("global $component_count = 2", lines)
        self.assertIn("global $bones_count = 90", lines)
        # Initialize 以 $component_id 变量逐组件写全局槽位（全局 id 0/1）
        self.assertIn(
            "$Pool_MergedSkeleton_Component_VertexGroupOffsets[$component_id] = 0\n"
            "$Pool_MergedSkeleton_Component_VertexGroupCounts[$component_id] = 50", lines)
        self.assertIn(
            "$Pool_MergedSkeleton_Component_VertexGroupOffsets[$component_id] = 50\n"
            "$Pool_MergedSkeleton_Component_VertexGroupCounts[$component_id] = 40", lines)
        self.assertIn("$component_id = 0", lines)
        self.assertIn("$component_id = 1", lines)

    def test_multi_lod_glue_single(self):
        """粘合层单套且命名空间赋值一次性（component_count/bones_count/instance_count）。"""
        lines = self._merge_lines(_make_multi_lod_exporters(), with_glue=True)
        self.assertIn("[CommandList_Component_DrawInstances]", lines)
        self.assertIn("$\\EFMIv1\\component_count = $component_count", lines)
        self.assertIn("$\\EFMIv1\\bones_count = $bones_count", lines)
        self.assertEqual(lines.count("$\\EFMIv1\\bones_count = $bones_count"), 1)

    def test_collect_components_global_ids(self):
        """_get_merged_skeleton_component_info：未配对 LOD1 部件独立成部件（追加段）。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh("LOD0.aaa-0", 0, 50),
            _FakePreSkindSubmesh("LOD1.bbb-0", 50, 40),
        ]
        components, id_dict = exporter._get_merged_skeleton_component_info()
        self.assertEqual(id_dict["LOD0.aaa-0"], 0)
        self.assertEqual(id_dict["LOD1.bbb-0"], 1)
        self.assertEqual([c["component_id"] for c in components], [0, 1])

    def test_same_ib_parts_collapse_into_one_component(self):
        """参考插件 same-IB 处理：LOD0/LOD1 同 IB 部件 = 一个逻辑部件（一个 draw）。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh("LOD0.face-0", 264, 53, match_draw_ib="fade", ref_component=""),
            _FakePreSkindSubmesh(
                "LOD1.face-0", 264, 53, match_draw_ib="fade",
                ref_component="LOD0.face-0",
                corr={"0": {"local_vg_id": 1}, "1": {"local_vg_id": 0}},
            ),
        ]
        parts, id_dict = exporter._get_merged_skeleton_component_info()
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(parts[0]["draws"]), 1)
        self.assertEqual(parts[0]["draws"][0]["lod_level"], 0)
        self.assertEqual(id_dict["LOD0.face-0"], 0)
        self.assertEqual(id_dict["LOD1.face-0"], 0)

    def test_same_ib_collapse_aliases_referenced_lod_slots_to_baseline(self):
        """被折叠槽位投影到基准连续导入槽；其它存活组件槽位保持不变。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh(
                "LOD0.face-0", 264, 3, match_draw_ib="fade",
                vg_map={"0": 264, "1": 175, "2": 266},
            ),
            _FakePreSkindSubmesh(
                "LOD1.face-0", 517, 3, match_draw_ib="fade",
                ref_component="LOD0.face-0",
                corr={
                    "0": {"unique_str": "LOD0.face-0", "local_vg_id": 0},
                    "1": {"unique_str": "LOD0.face-0", "local_vg_id": 1},
                    "2": {"unique_str": "LOD0.face-0", "local_vg_id": 2},
                },
                # local 1 的基准 VGMap 去重到了 175，但运行时基准 component
                # 实际把 local 1 写到 264 + 1 = 265，别名必须使用后者。
                # 600 属于另一个仍存活 component，不应被重定向。
                vg_map={"0": 517, "1": 518, "2": 519, "3": 600},
            ),
        ]
        parts, _id_dict = exporter._get_merged_skeleton_component_info()
        self.assertEqual(len(parts), 1)
        self.assertEqual(
            exporter._efmi_merged_skeleton_bone_aliases,
            {517: 264, 518: 265, 519: 266},
        )
        self.assertEqual(
            exporter._efmi_same_ib_alias_targets_by_lod,
            {"LOD1": frozenset({264, 265, 266})},
        )

    def test_same_ib_aliases_patch_r16_blend_buffer_without_mutating_source(self):
        """写盘阶段只改 R16 BLENDINDICES 字段，权重和原始内存缓冲保持不变。"""
        element_weight = types.SimpleNamespace(
            Category="Blend", SemanticName="BLENDWEIGHTS",
            Format="R16G16B16A16_UNORM", ByteWidth=8,
        )
        element_indices = types.SimpleNamespace(
            Category="Blend", SemanticName="BLENDINDICES",
            Format="R16G16B16A16_UINT", ByteWidth=8,
        )
        game_type = types.SimpleNamespace(
            CategoryStrideDict={"Blend": 16},
            D3D11ElementList=[element_weight, element_indices],
        )
        source = __import__("numpy").array(
            [
                # 8 bytes weights, then four uint16 indices: 517, 600, 519, 0
                1, 2, 3, 4, 5, 6, 7, 8,
                5, 2, 88, 2, 7, 2, 0, 0,
            ],
            dtype=__import__("numpy").uint8,
        )
        original = source.copy()
        patched, count = ExportEFMI._remap_blendindices_category_buffer(
            source, "Blend", game_type, {517: 264, 519: 266}
        )
        self.assertEqual(count, 2)
        self.assertEqual(source.tolist(), original.tolist())
        self.assertEqual(patched[:8].tolist(), original[:8].tolist())
        self.assertEqual(
            patched[8:].view("<u2").tolist(),
            [264, 600, 266, 0],
        )

    def test_same_ib_owned_slot_requires_cross_lod_correspondence(self):
        """same-IB 自有槽位缺少对应账本时不可猜 identity，必须停止导出。"""
        baseline = _FakePreSkindSubmesh(
            "LOD0.face-0", 100, 2, match_draw_ib="face",
            vg_map={"0": 100, "1": 101},
        )
        lod = _FakePreSkindSubmesh(
            "LOD1.face-0", 200, 2, match_draw_ib="face",
            ref_component="LOD0.face-0",
            vg_map={"0": 200, "1": 201},
            corr={},
        )
        with self.assertRaisesRegex(RuntimeError, "缺少跨 LOD 对应"):
            ExportEFMI._build_same_ib_bone_aliases(baseline, lod)

    def test_combined_lods_reject_stale_layout_metadata(self):
        """旧版跨 LOD 编号缓存不能进入新单池运行时。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh("LOD0.body-0", 0, 2, match_draw_ib="a", layout_version=3),
            _FakePreSkindSubmesh("LOD1.body-0", 2, 2, match_draw_ib="b", layout_version=3),
        ]
        with self.assertRaisesRegex(RuntimeError, "跨 LOD 骨骼缓存版本"):
            exporter._get_merged_skeleton_component_info()

    def test_non_conforming_blendindices_layout_rejected(self):
        """同一导出里混用 1×u16/4×u16 不能静默退回普通绘制。"""
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh("LOD0.aaa-0", 0, 50),
            _FakePreSkindSubmesh(
                "LOD0.odd-0", 50, 5, layouts=(0, "R16_UINT", "vb2"),
            ),
        ]
        with self.assertRaisesRegex(RuntimeError, "BLENDINDICES 布局不一致"):
            exporter._get_merged_skeleton_component_info()

    def test_different_ib_lod_draw_is_own_component_no_remap(self):
        """异 IB 的 LOD1 版本 = 自己的 component + 自己的绘制入口（v10：无 remap）。

        v10（撤销 v9 共享槽位投影）：LOD1 槽位段与 LOD0 不相交，
        运行时把当前 LOD 自己的矩阵写入自己的槽位段（remap=None 恒等路径）；
        不再生成 full→lod BlendRemap（v9 共用槽位 + 每 component 每帧单次导入
        导致 LOD1 网格读到 LOD0 矩阵、模型爆炸）。
        """
        exporter = object.__new__(ExportEFMI)
        exporter.submesh_model_list = [
            _FakePreSkindSubmesh("LOD0.body-0", 0, 53, match_draw_ib="b0", ref_component=""),
            _FakePreSkindSubmesh(
                "LOD1.body-1", 53, 47, match_draw_ib="b1",
                ref_component="LOD0.body-0",
                corr={"0": {"local_vg_id": 1}, "1": {"local_vg_id": 0}},
            ),
        ]
        parts, id_dict = exporter._get_merged_skeleton_component_info()
        self.assertEqual(len(parts), 2)
        self.assertEqual(id_dict["LOD0.body-0"], 0)
        self.assertEqual(id_dict["LOD1.body-1"], 1)
        lod_part = parts[1]
        self.assertEqual(lod_part["lod"], "LOD1")
        self.assertEqual(lod_part["vg_offset"], 53)
        self.assertEqual(lod_part["vg_count"], 47)
        self.assertEqual(len(lod_part["draws"]), 1)
        lod_draw = lod_part["draws"][0]
        self.assertEqual(lod_draw["lod_level"], 1)
        self.assertEqual(lod_draw["match_draw_ib"], "b1")
        self.assertIsNone(lod_draw["remap"])
        # 两部件槽位段不相交（v10 分段平移）：
        self.assertEqual(parts[0]["vg_offset"], 0)
        self.assertGreaterEqual(parts[1]["vg_offset"], parts[0]["vg_offset"] + parts[0]["vg_count"])

    def test_lod1_buffer_write_keeps_unified_global_blendindices(self):
        """局部导出不能用当前 LOD 的元数据区间否决统一全局顶点组编号。"""
        np = __import__("numpy")
        indices = np.array([0, 1, 3, 541], dtype="<u2").view(np.uint8)
        exporter = object.__new__(ExportEFMI)
        exporter.has_merged_skeleton = False
        exporter._efmi_merged_skeleton_bone_aliases = {}
        exporter.submesh_model_list = [types.SimpleNamespace(
            unique_str="LOD1.body-0",
            workspace_unique_str="LOD1.body-0",
            vg_offset=596,
            vg_count=67,
            ib=np.array([0, 1, 2], dtype=np.uint32),
            category_buffer_dict={"Blend": indices},
            d3d11_game_type=types.SimpleNamespace(),
        )]
        BufferExportHelper = sys.modules[
            f"{PKG}.common.buffer_export_helper"
        ].BufferExportHelper
        BufferExportHelper.write_buf_ib_r32_uint = (
            lambda values, path: np.asarray(values, dtype=np.uint32).tofile(path)
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            exporter._write_buffer_files_to_folder(temp_dir)
            written = np.fromfile(
                Path(temp_dir) / "LOD1.body-0-Blend.buf", dtype=np.uint8
            )

        self.assertEqual(written.tolist(), indices.tolist())

    def test_staging_failure_preserves_previous_export(self):
        """任一新缓冲写出失败时，旧 .buf 集合必须原封不动。"""
        np = __import__("numpy")
        exporter = object.__new__(ExportEFMI)
        exporter.prepare_merged_skeleton = lambda: None
        exporter.has_merged_skeleton = True
        exporter.merged_skeleton_components = [{"vg_offset": 0, "vg_count": 1, "draws": []}]
        exporter.merged_skeleton_component_id_dict = {"LOD1.bad-0": 0}
        exporter._efmi_merged_skeleton_bone_aliases = {}
        exporter.submesh_model_list = [types.SimpleNamespace(
            unique_str="LOD1.bad-0",
            workspace_unique_str="LOD1.bad-0",
            ib=np.array([0, 1, 2], dtype=np.uint32),
            category_buffer_dict={"Blend": np.zeros(16, dtype=np.uint8)},
            d3d11_game_type=types.SimpleNamespace(),
        )]
        def _fail_after_staging(staging_folder):
            (Path(staging_folder) / "partial.buf").write_bytes(b"partial")
            raise RuntimeError("invalid blend")

        exporter._write_buffer_files_to_folder = _fail_after_staging

        with tempfile.TemporaryDirectory() as temp_dir:
            old_path = Path(temp_dir) / "old.buf"
            old_path.write_bytes(b"previous-good-export")
            GlobalConfig = sys.modules[f"{PKG}.common.global_config"].GlobalConfig
            GlobalConfig.path_generatemod_buffer_folder = lambda: temp_dir
            BufferExportHelper = sys.modules[
                f"{PKG}.common.buffer_export_helper"
            ].BufferExportHelper
            BufferExportHelper.write_buf_ib_r32_uint = (
                lambda values, path: np.asarray(values, dtype=np.uint32).tofile(path)
            )

            with self.assertRaisesRegex(RuntimeError, "invalid blend"):
                exporter.generate_buffer_files()

            self.assertEqual(old_path.read_bytes(), b"previous-good-export")
            self.assertEqual(sorted(os.listdir(temp_dir)), ["old.buf"])


if __name__ == "__main__":
    unittest.main()
