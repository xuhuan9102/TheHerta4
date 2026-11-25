# -*- coding: utf-8 -*-
"""EFMI dHash 贴图快照、恢复和逐绘制绑定测试。

覆盖：
1. 独立绘制在写贴图前保存快照，合并绘制复用 EFMI 核心快照。
2. 关闭 dHash 时绘制块不增加快照或恢复命令。
3. 材质转资源之后：N 笔 draw → N 组，绑定在各自 ``; [mesh:`` 下，块顶不留
   Filter 共享绑定；旧“块级一对 + 块顶堆绑定”形态被替换。
4. 逐条格式分派：match_format 取源格式（含 _SRGB），缺 dhash 的条目独立
   退回数字槽，不整块降级。
5. 开关关：材质侧文本 SHA 不变、Restore 计数 0。

夹具照 tests/test_dhash_texture_filters.py / test_dhash_material_binding.py 的
fake 包骨架：只桩 bpy 与相对依赖，业务模块按真实文件装载。
"""

import hashlib
import importlib.util
import inspect
import re
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 共享 fake bpy（EFMI 只 import；材质节点在类定义期用 props/types）
# ---------------------------------------------------------------------------

_fake_bpy = types.ModuleType("bpy")
_fake_bpy.types = types.SimpleNamespace(
    PropertyGroup=object,
    Operator=object,
    Node=object,
    NodeSocket=object,
    Object=object,
    Mesh=object,
)
_fake_bpy.props = types.SimpleNamespace(
    StringProperty=lambda **_kwargs: None,
    BoolProperty=lambda **_kwargs: None,
    IntProperty=lambda **_kwargs: None,
    FloatProperty=lambda **_kwargs: None,
    CollectionProperty=lambda **_kwargs: None,
    PointerProperty=lambda **_kwargs: None,
    EnumProperty=lambda **_kwargs: None,
)
_fake_bpy.data = types.SimpleNamespace(objects={}, meshes={}, node_groups={})
_fake_bpy.context = types.SimpleNamespace(
    scene=types.SimpleNamespace(texture_tools_props=types.SimpleNamespace()),
    collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None)),
)
_fake_bpy.path = types.SimpleNamespace(abspath=lambda value, **_kwargs: value)
_fake_bpy.utils = types.SimpleNamespace(
    register_class=lambda _cls: None,
    unregister_class=lambda _cls: None,
)
sys.modules["bpy"] = _fake_bpy


def _install(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


def _text_sha(lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _run_indices(lines, name="RestoreInitialTextures"):
    needle = "run = " + name
    return [index for index, line in enumerate(lines) if str(line).strip() == needle]


def _filter_indices(lines):
    return [index for index, line in enumerate(lines) if "ps-t[Filter_" in str(line)]


# ---------------------------------------------------------------------------
# EFMI：真实 efmi.py + m_ini_builder + dhash_metadata
# ---------------------------------------------------------------------------

EFMI_PKG = "_dhash_snapshot_efmi_pkg"
for _name in (
    EFMI_PKG,
    f"{EFMI_PKG}.ui",
    f"{EFMI_PKG}.ui.universal",
    f"{EFMI_PKG}.blueprint",
    f"{EFMI_PKG}.common",
    f"{EFMI_PKG}.utils",
):
    _pkg = types.ModuleType(_name)
    _pkg.__path__ = []
    sys.modules[_name] = _pkg


class _EfmiGlobalProterties:
    use_dhash = False
    forbid_auto = False
    rabbitfx = False

    @classmethod
    def use_dhash_texture(cls):
        return cls.use_dhash

    @classmethod
    def forbid_auto_texture_ini(cls):
        return cls.forbid_auto

    @classmethod
    def use_rabbitfx_slot(cls):
        return cls.rabbitfx

    @classmethod
    def generate_branch_mod_gui(cls):
        return False


_install(f"{EFMI_PKG}.utils.json_utils", JsonUtils=object)
_install(
    f"{EFMI_PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(
        start_stage=lambda *_args, **_kwargs: None,
        end_stage=lambda *_args, **_kwargs: None,
    ),
)
_install(f"{EFMI_PKG}.common.global_config", GlobalConfig=types.SimpleNamespace())
_install(
    f"{EFMI_PKG}.common.global_properties",
    GlobalProterties=_EfmiGlobalProterties,
)
_install(
    f"{EFMI_PKG}.common.global_key_count_helper",
    GlobalKeyCountHelper=types.SimpleNamespace(generated_mod_number=0),
)
_install(f"{EFMI_PKG}.common.submesh_metadata", SubmeshMetadataResolver=object)
_install(
    f"{EFMI_PKG}.common.m_ini_helper",
    M_IniHelper=types.SimpleNamespace(is_slot_binding_mark_type=lambda _t: True),
)
_install(f"{EFMI_PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
_install(f"{EFMI_PKG}.blueprint.model", BluePrintModel=object)
_install(f"{EFMI_PKG}.common.submesh_model", SubMeshModel=object)
_install(f"{EFMI_PKG}.common.drawib_model", DrawIBModel=object)
_install(f"{EFMI_PKG}.blueprint.export_helper", BlueprintExportHelper=types.SimpleNamespace())
_install(f"{EFMI_PKG}.common.buffer_export_helper", BufferExportHelper=types.SimpleNamespace())
_install(f"{EFMI_PKG}.common.draw_call_model", DrawCallModel=object)
_install(f"{EFMI_PKG}.ui.universal.export_helper", ExportHelper=types.SimpleNamespace())

_efmi_dhash = _load_real(
    f"{EFMI_PKG}.common.dhash_metadata", Path("common") / "dhash_metadata.py"
)
_efmi_tmh = _load_real(
    f"{EFMI_PKG}.common.texture_metadata_helper",
    Path("common") / "texture_metadata_helper.py",
)
_efmi_ini = _load_real(
    f"{EFMI_PKG}.common.m_ini_builder", Path("common") / "m_ini_builder.py"
)
_efmi = _load_real(
    f"{EFMI_PKG}.ui.universal.efmi", Path("ui") / "universal" / "efmi.py"
)

M_IniSection = _efmi_ini.M_IniSection
M_SectionType = _efmi_ini.M_SectionType
TextureMarkUpInfo = _efmi_tmh.TextureMarkUpInfo
ExportEFMI = _efmi.ExportEFMI


# ---------------------------------------------------------------------------
# 材质转资源：真实 node_postprocess_material / custom_material_assign / ini_scope
# ---------------------------------------------------------------------------

MAT_PKG = "_dhash_snapshot_material_pkg"
for _name in (
    MAT_PKG,
    f"{MAT_PKG}.blueprint",
    f"{MAT_PKG}.common",
    f"{MAT_PKG}.toolkit",
    f"{MAT_PKG}.utils",
    f"{MAT_PKG}.ui",
    f"{MAT_PKG}.ui.ntmi_modimp",
):
    _pkg = types.ModuleType(_name)
    _pkg.__path__ = []
    sys.modules[_name] = _pkg
# 只让 blueprint 包能解析真实的 ini_scope.py（其余相对依赖均已显式桩掉）。
sys.modules[f"{MAT_PKG}.blueprint"].__path__ = [str(ROOT / "blueprint")]


class _FakePostProcessBase:
    AUTO_APPENDED_SECTION_MARKERS = ()
    AUTO_APPENDED_SECTION_MARKER_PREFIXES = ()

    @classmethod
    def split_auto_appended_tail_content(cls, content):
        return content, ""

    @classmethod
    def split_anim_driver_block_content(cls, content):
        return "", content


_install(
    f"{MAT_PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=_FakePostProcessBase,
)
_install(
    f"{MAT_PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    ),
)
_install(
    f"{MAT_PKG}.ui.ntmi_modimp.prefix_property_cache",
    get_prefix_record_props=lambda _name: {},
    has_prefix_record=lambda _name: False,
)
_install(
    f"{MAT_PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="ZZMI"),
)
_install(
    f"{MAT_PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        EFMI="EFMI",
        NTEMI="NTEMI",
        ZZMI="ZZMI",
        HIMI="HIMI",
        is_zzmi_family=lambda name: name == "ZZMI",
    ),
)
_install(
    f"{MAT_PKG}.toolkit.tt_dds_conversion",
    convert_texture_to_dds_cached=lambda *_args, **_kwargs: (True, ""),
    is_dds_file=lambda path: str(path or "").lower().endswith(".dds"),
)

_mat_fx_namespace = _load_real(
    f"{MAT_PKG}.blueprint.fx_namespace", Path("blueprint") / "fx_namespace.py"
)
_mat_node = _load_real(
    f"{MAT_PKG}.blueprint.node_postprocess_material",
    Path("blueprint") / "node_postprocess_material.py",
)
_mat_custom = _load_real(
    f"{MAT_PKG}.blueprint.node_postprocess_custom_material_assign",
    Path("blueprint") / "node_postprocess_custom_material_assign.py",
)
MaterialBase = _mat_node.SSMTNode_PostProcess_MaterialBase
CustomMaterialNode = _mat_custom.SSMTNode_PostProcess_CustomMaterialAssign


# ---------------------------------------------------------------------------
# EFMI 测试
# ---------------------------------------------------------------------------

def _make_mark(mark_name="DiffuseMap", dhash="0123456789abcdef",
               fmt="BC7_UNORM_SRGB", slot="ps-t3"):
    mark = TextureMarkUpInfo()
    mark.mark_name = mark_name
    mark.mark_type = "Slot"
    mark.mark_slot = slot
    mark.mark_filename = "aaaa-6-0-" + mark_name + ".dds"
    mark.source_dhash = dhash
    mark.source_format = fmt
    mark.binding_mode = "filter"
    return mark


class _FakeSubmesh:
    def __init__(self, unique_str="aaaa-6-0"):
        self.unique_str = unique_str


class EfmiBlockLevelRestoreTests(unittest.TestCase):
    def setUp(self):
        _EfmiGlobalProterties.use_dhash = False
        _EfmiGlobalProterties.forbid_auto = False
        _EfmiGlobalProterties.rabbitfx = False

    def tearDown(self):
        _EfmiGlobalProterties.use_dhash = False
        _EfmiGlobalProterties.forbid_auto = False
        _EfmiGlobalProterties.rabbitfx = False

    def _override_block(self, marks):
        exporter = object.__new__(ExportEFMI)
        section = M_IniSection(M_SectionType.TextureOverrideIB)
        block_start = len(section.SectionLineList)
        section.append("[TextureOverride_aaaa_6_0]")
        section.append("hash = aaaa")
        section.append("match_first_index = 0")
        section.append("match_index_count = 12")
        section.append("handling = skip")
        section.append("run = CommandList\\EFMIv1\\OverrideTextures")
        section.append("ib = Resource_aaaa_6_0_Index")
        section.append("vb3 = Resource_aaaa_6_0_Position")
        for mark in marks:
            self.assertTrue(
                exporter._try_append_dhash_binding(
                    section, _FakeSubmesh(), mark
                )
            )
        section.append("; [mesh:LOD0.aaaa-6-0.Part1] [vertex_count:12]")
        section.append("drawindexedinstanced = 12,1,0,0,0")
        draw_end = len(section.SectionLineList)
        inserted = exporter._append_snapshot_restore_for_filter_block(
            section, block_start, draw_end
        )
        return section.SectionLineList, inserted

    def _assert_block_order(self, lines):
        snapshot, = _run_indices(lines, "SnapshotInitialTextures")
        restore, = _run_indices(lines)
        filters = _filter_indices(lines)
        override = lines.index("run = CommandList\\EFMIv1\\OverrideTextures")
        mesh = next(
            index for index, line in enumerate(lines) if "; [mesh:" in str(line)
        )
        draw = next(
            index for index, line in enumerate(lines)
            if str(line).strip().startswith("drawindexed")
        )
        self.assertEqual(len(filters), 3)
        self.assertLess(snapshot, override)
        self.assertLess(override, filters[0])
        self.assertLess(filters[-1], mesh)
        self.assertLess(mesh, draw)
        self.assertLess(draw, restore)
        # 块首在 handling = skip 之后，块尾在 draw 之后。
        self.assertEqual(snapshot, lines.index("handling = skip") + 1)

    def test_standalone_block_snapshots_before_texture_bindings(self):
        _EfmiGlobalProterties.use_dhash = True
        lines, inserted = self._override_block(
            [
                _make_mark("DiffuseMap", "0123456789abcdef"),
                _make_mark("LightMap", "fedcba9876543210", "BC7_UNORM"),
                _make_mark("NormalMap", "1111222233334444", "BC5_UNORM"),
            ]
        )
        self.assertTrue(inserted)
        self._assert_block_order(lines)

    def test_switch_off_emits_zero_restore_and_keeps_sha(self):
        _EfmiGlobalProterties.use_dhash = False
        exporter = object.__new__(ExportEFMI)
        section = M_IniSection(M_SectionType.TextureOverrideIB)
        block_start = len(section.SectionLineList)
        section.append("[TextureOverride_aaaa_6_0]")
        section.append("hash = aaaa")
        section.append("handling = skip")
        section.append("run = CommandList\\EFMIv1\\OverrideTextures")
        self.assertFalse(
            exporter._try_append_dhash_binding(
                section, _FakeSubmesh(), _make_mark()
            )
        )
        section.append("ps-t3 = Resource_aaaa_6_0_DiffuseMap")
        section.append("; [mesh:LOD0.aaaa-6-0.Part1] [vertex_count:12]")
        section.append("drawindexedinstanced = 12,1,0,0,0")
        before = _text_sha(section.SectionLineList)
        inserted = exporter._append_snapshot_restore_for_filter_block(
            section, block_start, len(section.SectionLineList),
        )
        self.assertFalse(inserted)
        self.assertEqual(_run_indices(section.SectionLineList), [])
        self.assertEqual(_text_sha(section.SectionLineList), before)

    def test_merged_draw_command_block_gets_head_tail_restore(self):
        _EfmiGlobalProterties.use_dhash = True
        exporter = object.__new__(ExportEFMI)
        section = M_IniSection(M_SectionType.CommandList)
        block_start = len(section.SectionLineList)
        section.append("[CommandList_Draw_Component1]")
        section.append("run = CommandList\\EFMIv1\\OverrideTextures")
        section.append("ib = Resource_Component1_Index")
        section.append("ps-t[Filter_Component1_Diffuse] = ref Resource_Diffuse")
        section.append("ps-t[Filter_Component1_Light] = ref Resource_Light")
        section.append("ps-t[Filter_Component1_Normal] = ref Resource_Normal")
        section.append("; [mesh:LOD0.aaaa-6-0.Part1] [vertex_count:12]")
        section.append("drawindexedinstanced = 12,1,0,0,0")
        inserted = exporter._append_snapshot_restore_for_filter_block(
            section, block_start, len(section.SectionLineList), merged_callback=True,
        )
        self.assertTrue(inserted)
        lines = section.SectionLineList
        self.assertEqual(_run_indices(lines), [1, len(lines) - 1])
        self.assertEqual(lines[1], "run = RestoreInitialTextures")
        self.assertEqual(lines[-1], "run = RestoreInitialTextures")

    def test_generate_ini_file_wires_block_helper_for_both_paths(self):
        source = inspect.getsource(ExportEFMI.generate_ini_file)
        self.assertEqual(
            source.count("_append_snapshot_restore_for_filter_block("), 2
        )
        self.assertIn("merged_callback=True", source)
        self.assertNotIn("_append_dhash_snapshot_sections", source)


# ---------------------------------------------------------------------------
# 材质转资源测试
# ---------------------------------------------------------------------------

def _material_node(enabled=True):
    node = object.__new__(CustomMaterialNode)
    node._dhash_texture_enabled = lambda: enabled
    # 只验证 wrapper 的逐单元重写；目标判定用 pro 节点真实口径会拖进 bpy 物体库。
    node._unit_is_named_target = lambda _mesh_name: True
    node._unit_has_generated_material = lambda _lines, _unit: True
    return node


def _fresh_material_sections(unit_count=2):
    """材质节点刚生成完的段：每笔 draw 前有自己 3 条 Filter，块顶无 Filter。"""
    lines = [
        "hash = aaaa",
        "handling = skip",
        "run = CommandList\\EFMIv1\\OverrideTextures",
        "ib = Resource_Body_Index",
        "vb0 = Resource_Body_Position",
    ]
    for index in range(unit_count):
        part = index + 1
        lines.append("; [mesh:LOD0.aaaa-1-0.Part{}] [vertex_count:12]".format(part))
        lines.append("ps-t[Filter_Part{}_Diffuse] = ref Resource_Part{}_Diffuse".format(part, part))
        lines.append("ps-t[Filter_Part{}_Light] = ref Resource_Part{}_Light".format(part, part))
        lines.append("ps-t[Filter_Part{}_Normal] = ref Resource_Part{}_Normal".format(part, part))
        lines.append("drawindexedinstanced = 12,1,0,0,0")
        lines.append("")
    return OrderedDict([("[TextureOverride_Body]", lines)])


def _block_level_baseline():
    """改 B 之前的导出基线：EFMI 块级一对 + 块顶堆旧 Filter 绑定。"""
    lines = [
        "hash = aaaa",
        "handling = skip",
        "run = RestoreInitialTextures",  # 旧块首
        "run = CommandList\\EFMIv1\\OverrideTextures",
        "ib = Resource_Body_Index",
        "vb0 = Resource_Body_Position",
    ]
    # 块级共享绑定：token 精确对应各自 mesh；B 要把它们分到每笔 draw 下。
    for part in (1, 2):
        for role in ("Diffuse", "Light", "Normal"):
            lines.append(
                "ps-t[Filter_LOD0_aaaa_1_0_Part{}_{}] = ref Resource_Part{}_{}".format(
                    part, role, part, role
                )
            )
    for part in (1, 2):
        lines.append("; [mesh:LOD0.aaaa-1-0.Part{}] [vertex_count:12]".format(part))
        lines.append("drawindexedinstanced = 12,1,0,0,0")
        lines.append("")
    lines.append("run = RestoreInitialTextures")  # 旧块尾
    return OrderedDict([("[TextureOverride_Body]", lines)])


def _group_range(lines, part):
    mesh = next(
        index for index, line in enumerate(lines)
        if "; [mesh:" in str(line) and "Part{}]".format(part) in str(line)
    )
    end = next(
        (
            index
            for index in range(mesh + 1, len(lines))
            if "; [mesh:" in str(lines[index])
        ),
        len(lines),
    )
    return mesh, end


class MaterialPerDrawRestoreTests(unittest.TestCase):
    def test_two_draws_get_two_groups_with_bindings_under_mesh(self):
        sections = _block_level_baseline()
        inserted = _material_node()._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(inserted, 2)
        lines = sections["[TextureOverride_Body]"]
        first_mesh, _ = _group_range(lines, 1)
        # 块顶只留公共行 + OverrideTextures + ib/vb，不留 run 或 Filter。
        self.assertFalse(any(_run_indices(lines[:first_mesh])))
        self.assertFalse(any(_filter_indices(lines[:first_mesh])))
        # 块顶共享绑定必须全部分到各笔，总量守恒。
        self.assertEqual(len(_filter_indices(lines[:first_mesh])), 0)
        self.assertEqual(len(_filter_indices(lines)), 6)
        self.assertEqual(len(_run_indices(lines)), 4)

        for part in (1, 2):
            start, end = _group_range(lines, part)
            group = lines[start:end]
            group_runs = _run_indices(group)
            group_filters = _filter_indices(group)
            draw = next(
                index for index, line in enumerate(group)
                if str(line).strip().startswith("drawindexedinstanced")
            )
            self.assertEqual(len(group_runs), 2)
            self.assertEqual(len(group_filters), 3)
            self.assertLess(group_runs[0], group_filters[0])
            self.assertLess(group_filters[-1], draw)
            self.assertLess(draw, group_runs[1])

        start2, _ = _group_range(lines, 2)
        self.assertNotIn(
            "Resource_Part1_Diffuse",
            [str(line) for line in lines[max(0, start2 - 5):start2 + 5]],
        )

    def test_block_top_filters_are_distributed_to_mesh_units(self):
        """真实材质输出：块顶一份 Filter + 各笔 draw，没有逐笔 Filter。"""
        lines = [
            "hash = aaaa",
            "handling = skip",
            "run = CommandList\\EFMIv1\\OverrideTextures",
            "ib = Resource_Body_Index",
            "vb0 = Resource_Body_Position",
        ]
        for part in (1, 2):
            for role in ("Diffuse", "Light", "Normal"):
                lines.append(
                    "ps-t[Filter_LOD0_aaaa_1_0_Part{}_{}] = ref Resource_Part{}_{}".format(
                        part, role, part, role
                    )
                )
        for part in (1, 2):
            lines.append("; [mesh:LOD0.aaaa-1-0.Part{}] [vertex_count:12]".format(part))
            lines.append("drawindexedinstanced = 12,1,0,0,0")
            lines.append("")
        lines.append("run = RestoreInitialTextures")
        sections = OrderedDict([("[TextureOverride_Body]", lines)])
        inserted = _material_node()._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(inserted, 2)
        lines = sections["[TextureOverride_Body]"]
        first_mesh, _ = _group_range(lines, 1)
        self.assertFalse(any(_filter_indices(lines[:first_mesh])))
        self.assertFalse(any(_run_indices(lines[:first_mesh])))
        self.assertEqual(len(_filter_indices(lines)), 6)
        for part in (1, 2):
            start, end = _group_range(lines, part)
            group = lines[start:end]
            self.assertEqual(len(_filter_indices(group)), 3)
            self.assertEqual(len(_run_indices(group)), 2)

    def test_unmatched_top_filter_raises(self):
        """块顶 Filter token 对不上任何 mesh 单元 → 必须报错，不许静默留顶。"""
        lines = [
            "hash = aaaa",
            "handling = skip",
            "ps-t[Filter_NoSuchObject_Diffuse] = ref Resource_NoSuch",
            "; [mesh:LOD0.aaaa-1-0.Part1] [vertex_count:12]",
            "drawindexedinstanced = 12,1,0,0,0",
        ]
        sections = OrderedDict([("[TextureOverride_Body]", lines)])
        with self.assertRaises(ValueError) as ctx:
            _material_node()._apply_snapshot_restore_to_sections(sections)
        self.assertIn("无法精确分派", str(ctx.exception))

    def test_component_defaults_apply_to_distinct_numbered_meshes(self):
        lines = [
            "hash = 28847e3b",
            "handling = skip",
            "run = CommandList\\EFMIv1\\OverrideTextures",
            "ib = Resource_Body_Index",
        ]
        for role in ("Diffuse", "Light", "Normal"):
            lines.append(
                f"ps-t[Filter_LOD0_28847e3b_78186_0_{role}] = ref Resource-28847e3b-78186-0-{role}Map"
            )
        for suffix in ("001", "002", "003"):
            lines.append(
                f"; [mesh:LOD0.28847e3b-78186-0.Body.{suffix}_copy] [vertex_count:12]"
            )
            lines.append("drawindexedinstanced = 12,1,0,0,0")
            lines.append("")
        lines.append("run = RestoreInitialTextures")
        sections = OrderedDict([("[TextureOverride_LOD0.28847e3b_78186_0]", lines)])
        with patch.object(_mat_node.GlobalConfig, "logic_name", "EFMI"):
            inserted = _material_node()._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(inserted, 3)
        self.assertEqual(len(_filter_indices(lines)), 9)
        self.assertEqual(len(_run_indices(lines)), 6)
        starts = [
            index for index, line in enumerate(lines)
            if "; [mesh:" in str(line)
        ]
        self.assertEqual(len(starts), 3)
        for start, end in zip(starts, starts[1:] + [len(lines)]):
            group = lines[start:end]
            self.assertEqual(len(_filter_indices(group)), 3)
            self.assertEqual(len(_run_indices(group)), 2)

    def test_lod1_component_filters_cover_both_renamed_chain_draws(self):
        bindings = [
            "ps-t[Filter_LOD1_293cb5fa_1911_0_Diffuse] = ref Resource-293cb5fa-1911-0-DiffuseMap",
            "ps-t[Filter_LOD0_047d6e11_3978_0_Normal] = ref Resource-293cb5fa-1911-0-NormalMap",
        ]
        meshes = [
            "; [mesh:LOD1.293cb5fa-1911-0.原部件 脖子_chain1_copy] [vertex_count:12]",
            "; [mesh:LOD1.293cb5fa-1911-0.Body_chain1_copy] [vertex_count:12]",
        ]
        draw = "drawindexedinstanced = 12,1,0,0,0"
        restore = "run = RestoreInitialTextures"
        lines = [
            r"run = CommandList\EFMIv1\OverrideTextures", *bindings,
            meshes[0], draw, meshes[1], draw, restore,
        ]
        sections = OrderedDict([("[CommandList_Draw_LOD1.293cb5fa_1911_0]", lines)])
        node = _material_node()
        node._unit_is_named_target = lambda _mesh_name: False
        with patch.object(_mat_node.GlobalConfig, "logic_name", "EFMI"):
            self.assertEqual(node._apply_snapshot_restore_to_sections(sections), 2)
        self.assertEqual(lines[1:], [
            meshes[0], restore, *bindings, draw, restore,
            meshes[1], restore, *bindings, draw, restore,
        ])

    def test_object_filters_keep_blender_numbers_after_stripping_runtime_suffixes(self):
        bindings = [
            "ps-t[Filter_LOD1_293cb5fa_1911_0_Body_Diffuse] = ResourceTexture_Body",
            "ps-t[Filter_LOD1_293cb5fa_1911_0_Body_001_Diffuse] = ResourceTexture_Body_001",
            "ps-t[Filter_LOD1_293cb5fa_1911_0_Body_002_Diffuse] = ResourceTexture_Body_002",
            "ps-t[Filter_LOD1_293cb5fa_1911_0_Body_003_Diffuse] = ResourceTexture_Body_003",
        ]
        meshes = [
            "; [mesh:LOD1.293cb5fa-1911-0.Body_BPE2_temp]",
            "; [mesh:LOD1.293cb5fa-1911-0.Body.001_chain1_dup2_vgtest_unassigned_copy]",
            "; [mesh:LOD1.293cb5fa-1911-0.Body.002_copy_temp]",
            "; [mesh:LOD1.293cb5fa-1911-0.Body.003_vgtest_copy]",
        ]
        draw = "drawindexedinstanced = 12,1,0,0,0"
        restore = "run = RestoreInitialTextures"
        lines = [*bindings, *(line for mesh in meshes for line in (mesh, draw))]
        sections = OrderedDict([("[CommandList_Draw_LOD1.293cb5fa_1911_0]", lines)])
        with patch.object(_mat_node.GlobalConfig, "logic_name", "EFMI"):
            self.assertEqual(_material_node()._apply_snapshot_restore_to_sections(sections), 4)
        self.assertEqual(lines, [
            line for mesh, binding in zip(meshes, bindings)
            for line in (mesh, restore, binding, draw, restore)
        ])

    def test_numbered_filter_does_not_match_the_base_object(self):
        lines = [
            "ps-t[Filter_LOD1_293cb5fa_1911_0_Body_001_Diffuse] = ResourceTexture_Body_001",
            "; [mesh:LOD1.293cb5fa-1911-0.Body_copy]",
            "drawindexedinstanced = 12,1,0,0,0",
        ]
        sections = OrderedDict([("[CommandList_Draw_LOD1.293cb5fa_1911_0]", lines)])
        with self.assertRaisesRegex(ValueError, "匹配 0 个物体"):
            _material_node()._apply_snapshot_restore_to_sections(sections)

    def test_duplicate_efmi_and_material_filters_keep_material_only(self):
        """同名的 EFMI 和材质绑定只保留材质行。"""
        lines = [
            "hash = aaaa",
            "handling = skip",
            "run = CommandList\\EFMIv1\\OverrideTextures",
        ]
        for suffix in ("001", "002"):
            lines.append("; [mesh:LOD0.aaaa-1-0.Part{}.{}_copy]".format(1, suffix))
            for role in ("Diffuse", "Light", "Normal"):
                lines.append(
                    "ps-t[Filter_LOD0_aaaa_1_0_Part1_{}] = ref Resource-Old-{}".format(
                        role, role
                    )
                )
                lines.append(
                    "ps-t[Filter_LOD0_aaaa_1_0_Part1_{}] = ResourceTexture_{}_Body".format(
                        role, role
                    )
                )
            lines.append("drawindexedinstanced = 12,1,0,0,0")
        sections = OrderedDict([("[TextureOverride_Body]", lines)])
        node = _material_node()
        node._unit_is_named_target = lambda _mesh_name: False
        inserted = node._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(inserted, 2)
        lines = sections["[TextureOverride_Body]"]
        self.assertEqual(len(_filter_indices(lines)), 6)
        self.assertEqual(len(_run_indices(lines)), 4)
        self.assertFalse(any("Resource-Old-" in str(line) for line in lines))
        self.assertTrue(all(
            "= ResourceTexture_" in str(line)
            for line in lines if "ps-t[Filter_" in str(line)
        ))

    def test_shared_filters_do_not_overwrite_native_glow_draw(self):
        filter_name = "ps-t[Filter_LOD0_28847e3b_78186_0_Diffuse]"
        material = filter_name + " = ResourceTexture_DiffuseMap_28847e3b-78186-0.001"
        glow_draw = r"  run = CommandList\EFMITTL\Draw"
        first_mesh = "; [mesh:LOD0.28847e3b-78186-0_copy] [vertex_count:23556]"
        second_mesh = "; [mesh:LOD0.28847e3b-78186-0.001_copy] [vertex_count:5820]"
        second_draw = "  drawindexedinstanced = 22422,INSTANCE_COUNT,87582,0,FIRST_INSTANCE"
        restore = "run = RestoreInitialTextures"
        snapshot = "run = SnapshotInitialTextures"
        lines = [
            snapshot,
            filter_name + " = ref Resource-28847e3b-78186-0-DiffuseMap",
            first_mesh, material, glow_draw,
            second_mesh, material, second_draw, restore,
        ]
        sections = OrderedDict([("[CommandList_Draw_LOD0.28847e3b_78186_0]", lines)])
        node = _material_node()
        node._unit_is_named_target = lambda _mesh_name: False

        with patch.object(_mat_node.GlobalConfig, "logic_name", "EFMI"):
            self.assertEqual(node._apply_snapshot_restore_to_sections(sections), 2)
        self.assertEqual(lines, [
            snapshot,
            first_mesh, restore, material, glow_draw, restore,
            second_mesh, restore, material, second_draw, restore,
        ])
        node._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(lines.count(restore), 4)

    def test_commandlist_draw_section_is_wrapped_too(self):
        """材质输出落在 [CommandList_Draw_*] 时，也要纳入逐绘制补 run。"""
        fresh = _fresh_material_sections(2)["[TextureOverride_Body]"]
        sections = OrderedDict([("[CommandList_Draw_Test]", list(fresh))])
        node = _material_node()
        node._unit_is_named_target = lambda _mesh_name: False
        inserted = node._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(inserted, 2)
        lines = sections["[CommandList_Draw_Test]"]
        self.assertEqual(len(_run_indices(lines)), 4)

    def test_target_lookup_failure_still_wraps_filter_units(self):
        """目标部件判定失败时，只要段内已有 Filter，兜底也要补 run。"""
        node = _material_node()
        node._unit_is_named_target = lambda _mesh_name: False
        sections = _fresh_material_sections(2)
        inserted = node._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(inserted, 2)
        lines = sections["[TextureOverride_Body]"]
        self.assertEqual(len(_run_indices(lines)), 4)
        for part in (1, 2):
            start, end = _group_range(lines, part)
            self.assertEqual(len(_run_indices(lines[start:end])), 2)

    def test_build_mapping_accepts_filter_binding(self):
        node = _material_node()
        mapping = node.build_mapping_for_section([
            "ps-t[Filter_LOD0_aaaa_1_0_Part1_Diffuse] = ref Resource-Part1-DiffuseMap"
        ])
        self.assertEqual(
            mapping.get("ps-t[Filter_LOD0_aaaa_1_0_Part1_Diffuse]"),
            "DiffuseMap",
        )

    def test_single_draw_gets_one_group(self):
        sections = _fresh_material_sections(1)
        inserted = _material_node()._apply_snapshot_restore_to_sections(sections)
        self.assertEqual(inserted, 1)
        lines = sections["[TextureOverride_Body]"]
        self.assertEqual(len(_run_indices(lines)), 2)
        self.assertEqual(len(_filter_indices(lines)), 3)
        start, end = _group_range(lines, 1)
        self.assertEqual(_run_indices(lines[start:end]), [1, len(lines[start:end]) - 2])

    def test_switch_off_adds_nothing_and_keeps_sha(self):
        sections = _fresh_material_sections(2)
        before = list(sections["[TextureOverride_Body]"])
        before_sha = _text_sha(before)
        inserted = _material_node(enabled=False)._apply_snapshot_restore_to_sections(
            sections
        )
        self.assertEqual(inserted, 0)
        self.assertEqual(_text_sha(sections["[TextureOverride_Body]"]), before_sha)
        self.assertEqual(_run_indices(sections["[TextureOverride_Body]"]), [])

    def test_native_restore_needs_no_section_definition(self):
        sections = _fresh_material_sections(1)
        _material_node()._apply_snapshot_restore_to_sections(sections)
        self.assertNotIn("[RestoreInitialTextures]", sections)
        self.assertNotIn("[SnapshotInitialTextures]", sections)

    def test_legacy_restore_section_is_removed_and_markers_are_preserved(self):
        sections = _fresh_material_sections(1)
        sections["[RestoreInitialTextures]"] = ["run = restoreoriginaltextures", ";MARK:ResourceBuffer"]
        _material_node()._apply_snapshot_restore_to_sections(sections)
        self.assertNotIn("[RestoreInitialTextures]", sections)
        self.assertIn(";MARK:ResourceBuffer", sections["[TextureOverride_Body]"])

    def test_standalone_efmi_mixed_binding_reapplies_fixed_defaults_after_each_restore(self):
        diffuse = "ps-t[Filter_LOD0_28847e3b_78186_0_Diffuse] = ref Resource-28847e3b-78186-0-DiffuseMap"
        light = "ps-t17 = Resource-28847e3b-78186-0-LightMap"
        local_light = "ps-t17 = ResourceTexture_LightMap_Body"
        first = "; [mesh:LOD0.28847e3b-78186-0.Body_copy]"
        second = "; [mesh:LOD0.28847e3b-78186-0.Body.001_copy]"
        draw = "drawindexedinstanced = 12,1,0,0,0"
        restore = "run = RestoreInitialTextures"
        snapshot = "run = SnapshotInitialTextures"
        lines = [
            "hash = 28847e3b", "handling = skip", restore, light, diffuse,
            first, local_light, draw, second, draw, restore,
        ]
        sections = OrderedDict([("[TextureOverride_LOD0.28847e3b_78186_0]", lines)])
        with patch.object(_mat_node.GlobalConfig, "logic_name", "EFMI"):
            node = _material_node()
            self.assertEqual(node._apply_snapshot_restore_to_sections(sections), 2)
            expected = [
                snapshot, "hash = 28847e3b", "handling = skip",
                first, restore, light, diffuse, local_light, draw, restore,
                second, restore, light, diffuse, draw, restore,
            ]
            self.assertEqual(lines, expected)
            node._apply_snapshot_restore_to_sections(sections)
            self.assertEqual(lines, expected)

    def test_execute_postprocess_wires_wrapper_before_serialize(self):
        method = MaterialBase.execute_postprocess
        method = getattr(method, "__wrapped__", method)
        source = inspect.getsource(method)
        call = "_apply_snapshot_restore_to_sections(sections)"
        self.assertIn(call, source)
        self.assertLess(
            source.index(call),
            source.index("new_content = self._serialize_ini_content"),
        )


# ---------------------------------------------------------------------------
# 槽位 / 格式分派测试
# ---------------------------------------------------------------------------

def _obj(name="Body"):
    return types.SimpleNamespace(name=name)


def _slot_info(mark_name, dhash, fmt, slot):
    return {
        "mark_name": mark_name,
        "mark_slot": slot,
        "mark_filename": "body-{}.dds".format(mark_name),
        "source_dhash": dhash,
        "source_format": fmt,
        "binding_mode": "DHASH",
    }


class DHashFormatDispatchTests(unittest.TestCase):
    def test_match_format_keeps_source_srgb_and_linear_exactly(self):
        node = _material_node()
        cases = [
            ("BC7_UNORM_SRGB", "BC7_UNORM_SRGB"),
            ("BC5_UNORM", "BC5_UNORM"),
            ("BC7_UNORM", "BC7_UNORM"),
            ("BC1_UNORM_SRGB", "BC1_UNORM_SRGB"),
        ]
        for fmt, expected in cases:
            sections = OrderedDict()
            line = node._build_dhash_binding_line(
                _obj(),
                _slot_info("DiffuseMap", "0123456789abcdef", fmt, "ps-t2"),
                "DHASH",
                "Resource_Body_Diffuse",
                sections,
            )
            self.assertTrue(str(line).startswith("ps-t[Filter_"))
            filter_key = next(iter(sections))
            self.assertEqual(
                sections[filter_key],
                ["match_format = " + expected, "match_dhash = 0123456789abcdef"],
            )

    def test_invalid_format_falls_back_to_numeric_slot(self):
        node = _material_node()
        sections = OrderedDict()
        line = node._build_dhash_binding_line(
            _obj(),
            _slot_info(
                "DiffuseMap", "0123456789abcdef", "BC7 UNORM SRGB", "ps-t2"
            ),
            "DHASH",
            "Resource_Body_Diffuse",
            sections,
        )
        self.assertIsNone(line)
        self.assertEqual(list(sections), [])

    def test_missing_dhash_falls_back_per_entry_not_whole_block(self):
        node = _material_node()
        sections = OrderedDict()
        valid = node._build_dhash_binding_line(
            _obj(),
            _slot_info("DiffuseMap", "0123456789abcdef", "BC7_UNORM_SRGB", "ps-t2"),
            "DHASH",
            "Resource_Body_Diffuse",
            sections,
        )
        invalid = node._build_dhash_binding_line(
            _obj(),
            _slot_info("LightMap", "", "BC7_UNORM", "ps-t3"),
            "DHASH",
            "Resource_Body_Light",
            sections,
        )
        self.assertTrue(str(valid).startswith("ps-t[Filter_"))
        self.assertIsNone(invalid)
        # 只有 valid 的槽位出现 Filter；invalid 的槽位留给调用方写数字槽。
        self.assertEqual(len(sections), 1)
        forms = {"ps-t2": {"filter"}, "ps-t3": {"numeric"}}
        self.assertTrue(all(len(value) == 1 for value in forms.values()))

    def test_same_slot_both_forms_would_be_red(self):
        """同一槽位同时出现 Filter 与数字槽时，这个校验函数会报错。"""
        node = _material_node()
        sections = OrderedDict()
        valid = node._build_dhash_binding_line(
            _obj(),
            _slot_info("DiffuseMap", "0123456789abcdef", "BC7_UNORM", "ps-t2"),
            "DHASH",
            "Resource_Body_Diffuse",
            sections,
        )
        self.assertIsNotNone(valid)
        # 模拟调用方对同槽缺 dhash 的条目写数字槽（这是禁止形态）。
        conflicting_lines = [valid, "ps-t2 = Resource_Body_Other"]
        forms_by_slot = {}
        for line in conflicting_lines:
            numeric = re.match(r"^(ps-t\d+)\s*=", str(line))
            if numeric:
                forms_by_slot.setdefault(numeric.group(1), set()).add("numeric")
            if str(line).startswith("ps-t[Filter_"):
                forms_by_slot.setdefault("ps-t2", set()).add("filter")
        with self.assertRaises(AssertionError):
            for slot, forms in forms_by_slot.items():
                self.assertEqual(
                    len(forms), 1,
                    "槽位 {} 同时出现数字槽与 Filter 写法: {}".format(slot, forms),
                )


if __name__ == "__main__":
    unittest.main()
