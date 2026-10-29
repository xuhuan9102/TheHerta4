"""「顶点命中索引（稀疏查找）」的等价性、着色器注入与 ini 接线测试。

稀疏索引把「顶点数 × 槽位数」的稠密 FREQ 表（``NO_FREQ_INDEX`` = 该顶点不受此槽位影响，
取值在所有合法形态键下标之外）
转置成 CSR：``start``（行偏移，顶点数 + 1 个 uint32）+ ``packed``（位移记录下标，
``-1`` = 无位移数据）+ ``freq``（形态键强度下标）。换表的唯一依据是**逐格等价**，
所以本文件分六层锁死：

1. ``SparseIndexTableTests``：CSR 与稠密表逐格等价（含哨兵、空表、全命中、下标 255 是合法键）；
2. ``SparseIndexWriterTests``：三份缓冲的落盘尺寸、文件名与统计；
3. ``SparseLogicLinesTests``：注入的逻辑块只遍历命中条目（含拖拽变体与缩进）；
4. ``SparseResourceNameTests``：资源名派生（直接加载真实 ``common/mod_path_compat.py``）；
5. ``SparseNodeWiringTests``：模板选择 + 前置项判定 + 真实 v6 模板的注入结果；
6. ``SparseIniWiringTests``：ini 资源段与 Present 绑定改到 t96/t97/t98 且不再绑 t53。
"""
import ast
import importlib.util
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # 单文件运行（``cd tests && pytest test_shapekey_sparse_vertex_index.py``）时
    # 仓库根不在 sys.path 上，``from tests import _real_modules`` 会 ImportError
    sys.path.insert(0, str(REPO_ROOT))

from tests import _real_modules  # noqa: E402


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_real_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --- 真实模块：资源名派生（只依赖 os/re，可安全直接加载，不受下面的 stub 影响） ---
mod_path_compat = _load_real_module(
    "_sparse_index_test_real_mod_path_compat", Path("common") / "mod_path_compat.py"
)

# --- 被测模块本体：纯 numpy/stdlib，无 bpy、无相对导入 ---
sparse_index = _load_real_module(
    "_sparse_index_test_shapekey_sparse_index", Path("blueprint") / "shapekey_sparse_index.py"
)


# ============================================================================
# 节点侧 harness（形态键后处理节点的标准 stub 组合）
# ============================================================================
PKG = "_shapekey_sparse_index_node_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

# 真实 common 子模块按 fake 包前缀注册（空 __path__ 假包解析不了相对导入）
_real_modules.register_real_common_modules(f"{PKG}.common")

_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects={}),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
        },
    ),
)
_install_module(f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda *_args, **_kwargs: None)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_shape_key_variable_name=lambda shape_key_name, **_kwargs: f"Freq_{shape_key_name}",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
    cjk_to_ascii=lambda value: str(value or ""),
    is_pinyin_available=lambda **_kwargs: False,
    reset_pinyin_cache=lambda *_args, **_kwargs: None,
    shape_key_base_variable_name=lambda shape_key_name: f"Freq_{shape_key_name}",
    get_referenced_variable_names=lambda *_args, **_kwargs: set(),
)
_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_base_position_resource_map=lambda *_args, **_kwargs: {},
    derive_shapekey_base_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_freq_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_data_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_vertex_entry_start_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_vertex_entry_packed_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_vertex_entry_freq_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_weight_resource_name=lambda *args, **_kwargs: "ResourceWeightStub",
    ensure_resource_alias_section=lambda *_args, **_kwargs: None,
    resolve_hash_buffer_candidate=lambda *_args, **_kwargs: "",
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        resolve_source_object_name=lambda name: name,
        extract_prefix_info=lambda name: None,
    ),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
    ),
)
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        collect_connected_start_nodes=lambda _tree: [],
        get_current_blueprint_model=lambda: None,
        _resolve_shapekey_object_in_scene=lambda name: _fake_bpy.data.objects.get(name),
    ),
)

node_module = _load_real_module(
    f"{PKG}.blueprint.node_postprocess_shapekey", Path("blueprint") / "node_postprocess_shapekey.py"
)


# ============================================================================
# 直出侧 harness（`DirectShapeKeyOutputMixin._update_ini_sections`）
# ============================================================================
PKG_MIXIN = "_shapekey_sparse_index_mixin_test_pkg"
for package_name in (PKG_MIXIN, f"{PKG_MIXIN}.blueprint", f"{PKG_MIXIN}.common", f"{PKG_MIXIN}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

# 四个被断言的派生函数按**真实实现的命名**给出（真实实现由 SparseResourceNameTests 校验）：
# 基名 "Resourced942b3a7Position" → "..._VertexEntryStart" / "..._VertexEntryPacked" /
# "..._VertexEntryFreq" / "..._freq_indices"。
_install_module(
    f"{PKG_MIXIN}.common.mod_path_compat",
    **{
        name: (lambda *_args, **_kwargs: None)
        for name in (
            "collect_base_position_resource_map",
            "derive_shapekey_base_resource_name",
            "derive_shapekey_merged_data_resource_name",
            "derive_shapekey_merged_map_resource_name",
            "derive_shapekey_slot_map_resource_name",
            "derive_shapekey_slot_resource_name",
            "derive_shapekey_frame_table_resource_name",
            "derive_shapekey_group_map_resource_name",
            "derive_shapekey_weight_resource_name",
            "ensure_resource_alias_section",
        )
    },
    derive_shapekey_freq_resource_name=lambda base, *_args, **_kwargs: f"{base}_freq_indices",
    derive_shapekey_vertex_entry_start_resource_name=lambda base, *_args, **_kwargs: f"{base}_VertexEntryStart",
    derive_shapekey_vertex_entry_packed_resource_name=lambda base, *_args, **_kwargs: f"{base}_VertexEntryPacked",
    derive_shapekey_vertex_entry_freq_resource_name=lambda base, *_args, **_kwargs: f"{base}_VertexEntryFreq",
)
_install_module(f"{PKG_MIXIN}.utils.log_utils", LOG=types.SimpleNamespace())
_install_module(
    f"{PKG_MIXIN}.common.safe_write",
    write_text_if_changed=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG_MIXIN}.blueprint.direct_export_runtime_utils",
    apply_position_override_in_place=lambda *_args, **_kwargs: None,
    extract_position_bytes_by_indices=lambda *_args, **_kwargs: b"",
    assemble_drawib_position_bytes=lambda *_args, **_kwargs: (b"", 0),
    iter_drawib_models=lambda *_args, **_kwargs: [],
)
_install_module(
    f"{PKG_MIXIN}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=RuntimeError,
    _buffer_to_bytes=lambda value: value,
    resolve_use_delta=lambda node: bool(
        getattr(node, "store_deltas", True)
        or getattr(node, "store_all_vertex_channels", False)
    ),
)

mixin_module = _load_real_module(
    f"{PKG_MIXIN}.blueprint.direct_export_shapekey_output_mixin",
    Path("blueprint") / "direct_export_shapekey_output_mixin.py",
)


# ============================================================================
# 1. CSR 与稠密表逐格等价
# ============================================================================
class SparseIndexTableTests(unittest.TestCase):
    """换表的唯一依据：CSR 还原出来的「每顶点命中项」与稠密表逐格一致。"""

    NO_FREQ = sparse_index.NO_FREQ_INDEX

    @staticmethod
    def _dense_expected(dense, packed):
        """独立实现（纯 Python 双循环）——不复用被测代码的 numpy 向量化路径。"""
        expected = []
        for vertex in range(len(dense)):
            entries = []
            for slot in range(len(dense[vertex])):
                if dense[vertex][slot] == sparse_index.NO_FREQ_INDEX:
                    continue
                entries.append((int(dense[vertex][slot]), int(packed[vertex][slot])))
            expected.append(entries)
        return expected

    def test_csr_matches_dense_table_entry_by_entry(self):
        N = self.NO_FREQ
        dense = [
            [N, 3, N, 7],
            [N, N, N, N],   # 完全不命中：区间为空
            [0, N, 1, N],
            [5, 6, 7, 8],           # 全命中：条目数 == 槽位数
        ]
        packed = [
            [-1, 12, -1, 13],
            [-1, -1, -1, -1],
            [0, -1, 1, -1],
            [20, 21, 22, 23],
        ]

        start, packed_out, freq_out = sparse_index.build_sparse_vertex_index(dense, packed)
        lookup = sparse_index.sparse_entry_lookup(start, packed_out, freq_out)

        self.assertEqual(lookup, self._dense_expected(dense, packed))
        # 行偏移表：非递减、首项 0、末项 = 命中总数
        self.assertEqual(start.tolist(), [0, 2, 2, 4, 8])
        self.assertEqual(int(start[-1]), int(len(freq_out)))
        self.assertEqual(freq_out.tolist(), [3, 7, 0, 1, 5, 6, 7, 8])
        self.assertEqual(packed_out.tolist(), [12, 13, 0, 1, 20, 21, 22, 23])

    def test_sentinel_is_not_a_shape_key_slot(self):
        """哨兵表示「该顶点不受此槽位影响」，绝不能变成一条命中条目。"""
        dense = [[self.NO_FREQ, self.NO_FREQ], [2, self.NO_FREQ]]
        start, packed_out, freq_out = sparse_index.build_sparse_vertex_index(dense)
        self.assertEqual(start.tolist(), [0, 0, 1])
        self.assertEqual(freq_out.tolist(), [2])
        # 未给位移表时全部记 -1（上层据此跳过位移读取）
        self.assertEqual(packed_out.tolist(), [-1])

    def test_index_255_is_a_legal_shape_key_slot(self):
        """回归：形态键总数 > 255 时下标 255 是**合法键**，必须成为命中条目。

        早期用 255 当哨兵，工程里下标恰为 255 的那个键会被写入端当成空槽位跳过、
        被读取端当成空槽位忽略，实机上整体失效且不报错。
        """
        dense = [[255, 2], [self.NO_FREQ, self.NO_FREQ]]
        start, packed_out, freq_out = sparse_index.build_sparse_vertex_index(dense)
        self.assertEqual(start.tolist(), [0, 2, 2])
        self.assertEqual(freq_out.tolist(), [255, 2])
        self.assertEqual(packed_out.tolist(), [-1, -1])

    def test_entries_of_one_vertex_are_contiguous_and_slot_ascending(self):
        N = self.NO_FREQ
        dense = [
            [9, N, 4],
            [N, N, N],
            [N, 1, 2],
        ]
        start, packed_out, freq_out = sparse_index.build_sparse_vertex_index(dense)
        lookup = sparse_index.sparse_entry_lookup(start, packed_out, freq_out)
        # 同一顶点的条目在缓冲里连续，且槽位号递增
        self.assertEqual(lookup[0], [(9, -1), (4, -1)])
        self.assertEqual(lookup[1], [])
        self.assertEqual(lookup[2], [(1, -1), (2, -1)])

    def test_zero_slot_table_is_valid(self):
        """有顶点但一个槽位都没有：行偏移全 0，条目为空。"""
        start, packed_out, freq_out = sparse_index.build_sparse_vertex_index(
            [[], [], []]
        )
        self.assertEqual(start.tolist(), [0, 0, 0, 0])
        self.assertEqual(freq_out.tolist(), [])
        self.assertEqual(packed_out.tolist(), [])

    def test_packed_table_shape_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            sparse_index.build_sparse_vertex_index([[1, 2], [3, 4]], [[1, 2]])

    def test_non_2d_freq_table_is_rejected(self):
        with self.assertRaises(ValueError):
            sparse_index.build_sparse_vertex_index([1, 2, 3])

    def test_lookup_rejects_row_offset_length_mismatch(self):
        start, packed_out, freq_out = sparse_index.build_sparse_vertex_index([[1, 2]])
        with self.assertRaises(ValueError):
            sparse_index.sparse_entry_lookup(start, packed_out, freq_out, vertex_count=5)

    def test_lookup_rejects_non_monotonic_row_offsets(self):
        with self.assertRaises(ValueError):
            sparse_index.sparse_entry_lookup(
                np.array([0, 3, 1], dtype=np.uint32),
                np.array([-1, -1, -1], dtype=np.int32),
                np.array([1, 2, 3], dtype=np.uint32),
            )


# ============================================================================
# 2. 三份缓冲落盘
# ============================================================================
class SparseIndexWriterTests(unittest.TestCase):
    def test_writes_three_buffers_with_expected_sizes(self):
        N = sparse_index.NO_FREQ_INDEX
        dense = [
            [N, 1, N],
            [2, N, 3],
        ]
        packed = [
            [-1, 11, -1],
            [12, -1, 13],
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            stats = sparse_index.write_sparse_vertex_index(temp_dir, "d942b3a7", dense, packed)

            self.assertEqual(stats["vertex_count"], 2)
            self.assertEqual(stats["slot_count"], 3)
            self.assertEqual(stats["entry_count"], 3)
            self.assertEqual(stats["packed_count"], 3)

            paths = sparse_index.sparse_buf_paths(temp_dir, "d942b3a7")
            self.assertEqual(
                Path(paths[sparse_index.SPARSE_START_SUFFIX]).name,
                "d942b3a7-Position_vertex_entry_start.buf",
            )
            self.assertEqual(
                Path(paths[sparse_index.SPARSE_PACKED_SUFFIX]).name,
                "d942b3a7-Position_vertex_entry_packed.buf",
            )
            self.assertEqual(
                Path(paths[sparse_index.SPARSE_FREQ_SUFFIX]).name,
                "d942b3a7-Position_vertex_entry_freq.buf",
            )
            self.assertEqual(Path(paths[sparse_index.SPARSE_START_SUFFIX]).stat().st_size, (2 + 1) * 4)
            self.assertEqual(Path(paths[sparse_index.SPARSE_PACKED_SUFFIX]).stat().st_size, 3 * 4)
            self.assertEqual(Path(paths[sparse_index.SPARSE_FREQ_SUFFIX]).stat().st_size, 3 * 4)

            # 落盘内容与内存里的 CSR 逐字节一致（着色器按 stride = 4 直接读）
            start, packed_out, freq_out = sparse_index.build_sparse_vertex_index(dense, packed)
            self.assertEqual(
                Path(paths[sparse_index.SPARSE_START_SUFFIX]).read_bytes(), start.tobytes()
            )
            self.assertEqual(
                Path(paths[sparse_index.SPARSE_PACKED_SUFFIX]).read_bytes(), packed_out.tobytes()
            )
            self.assertEqual(
                Path(paths[sparse_index.SPARSE_FREQ_SUFFIX]).read_bytes(), freq_out.tobytes()
            )

    def test_writer_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            meshes_dir = Path(temp_dir) / "Meshes0000"
            stats = sparse_index.write_sparse_vertex_index(str(meshes_dir), "abc", [[1]])
            self.assertTrue(meshes_dir.is_dir())
            self.assertEqual(stats["entry_count"], 1)

    def test_writer_does_not_emit_the_dense_freq_table(self):
        """稀疏与稠密 FREQ 表互斥：写出方只落三份稀疏缓冲。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            sparse_index.write_sparse_vertex_index(temp_dir, "abc", [[1, 2]])
            names = sorted(path.name for path in Path(temp_dir).iterdir())
            self.assertEqual(
                names,
                [
                    "abc-Position_vertex_entry_freq.buf",
                    "abc-Position_vertex_entry_packed.buf",
                    "abc-Position_vertex_entry_start.buf",
                ],
            )
            self.assertNotIn("abc-Position_freq_indices.buf", names)


# ============================================================================
# 3. 逻辑块文本
# ============================================================================
class _RecordingAccumulationBuilder:
    """记录调用实参并回显可识别的累加行（替代节点自己的累加渲染器）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, level, entry_token, delta_expression, weight_token, channel_names):
        self.calls.append((level, entry_token, delta_expression, weight_token, tuple(channel_names)))
        return [f"{level}total_diff_position += {delta_expression} * {weight_token};"]


class SparseLogicLinesTests(unittest.TestCase):
    def test_loop_iterates_only_the_hit_entries(self):
        builder = _RecordingAccumulationBuilder()
        lines = sparse_index.build_sparse_logic_lines(["position"], builder)
        text = "\n".join(lines)

        # 区间来自行偏移表：一个顶点只跑「命中数」次
        self.assertIn("uint sparse_entry_start = vertex_entry_start[i];", text)
        self.assertIn("uint sparse_entry_end = vertex_entry_start[i + 1];", text)
        self.assertIn(
            "for (uint sparse_entry = sparse_entry_start; sparse_entry < sparse_entry_end; ++sparse_entry)",
            text,
        )
        # 强度按条目里的形态键序号取；位移按下标取，-1 直接跳过
        self.assertIn("float anim_weight_entry = ShapeKeyWeight[vertex_entry_freq[sparse_entry]];", text)
        self.assertIn("if (abs(anim_weight_entry) > 1e-5)", text)
        self.assertIn("int packed_index = vertex_entry_packed[sparse_entry];", text)
        self.assertIn("if (packed_index != -1)", text)
        # 累加行来自注入的渲染器（与旧模型共用同一套实现）
        self.assertIn("total_diff_position += merged_shapekey_pos_deltas[packed_index] * anim_weight_entry;", text)
        # 稀疏路径不再读稠密 FREQ 表，也不再按槽位数展开
        self.assertNotIn("vertex_freq_indices", text)
        self.assertNotIn("num_slots", text)

    def test_accumulation_builder_is_called_with_the_entry_token_and_indent(self):
        builder = _RecordingAccumulationBuilder()
        sparse_index.build_sparse_logic_lines(["position", "normal"], builder)

        self.assertEqual(len(builder.calls), 1)
        level, entry_token, delta_expression, weight_token, channel_names = builder.calls[0]
        # 累加器 token 加下划线前缀，避免与循环外的同名变量撞名
        self.assertEqual(entry_token, "_entry")
        self.assertEqual(delta_expression, "merged_shapekey_pos_deltas[packed_index]")
        self.assertEqual(weight_token, "anim_weight_entry")
        self.assertEqual(channel_names, ("position", "normal"))
        self.assertEqual(level, "    " + " " * 12)

    def test_custom_indent_shifts_every_line(self):
        builder = _RecordingAccumulationBuilder()
        lines = sparse_index.build_sparse_logic_lines(["position"], builder, indent="        ")
        for line in lines:
            if line.strip():
                self.assertTrue(line.startswith("        "), line)
        self.assertIn("        uint sparse_entry_start = vertex_entry_start[i];", "\n".join(lines))

    def test_drag_variant_recomputes_weight_from_the_runtime_zone_tables(self):
        """拖拽 ⊕ 稀疏可共存：条目里的 freq 就是形态键序号，三张常量表按它索引。"""
        builder = _RecordingAccumulationBuilder()
        lines = sparse_index.build_sparse_logic_lines(["position"], builder, drag_drive_enabled=True)
        text = "\n".join(lines)

        self.assertIn("uint sparse_freq_entry = vertex_entry_freq[sparse_entry];", text)
        self.assertIn("float anim_weight_entry = ShapeKeyWeight[sparse_freq_entry];", text)
        self.assertIn("uint sk_zone_entry = SHAPEKEY_ZONE_IDS[sparse_freq_entry];", text)
        self.assertIn("uint sk_nd_stage_entry = SHAPEKEY_ND_STAGE_IDS[sparse_freq_entry];", text)
        self.assertIn("uint sk_slot_entry = SHAPEKEY_SLOT_IDS[sparse_freq_entry];", text)
        self.assertIn(
            "if (sk_zone_entry != 0xFFFFFFFFu && (sk_nd_stage_entry == 0xFFFFFFFFu "
            "|| ShapeKeyClickCount[sk_zone_entry] == sk_nd_stage_entry))",
            text,
        )
        self.assertIn("anim_weight_entry = ShapeKeyDrive[sk_slot_entry];", text)
        # 覆盖后仍走同一条位移读取路径
        self.assertIn("if (abs(anim_weight_entry) > 1e-5)", text)
        self.assertIn("int packed_index = vertex_entry_packed[sparse_entry];", text)

    def test_drag_variant_keeps_the_same_entry_loop(self):
        builder = _RecordingAccumulationBuilder()
        plain = sparse_index.build_sparse_logic_lines(["position"], builder)
        drag = sparse_index.build_sparse_logic_lines(["position"], builder, drag_drive_enabled=True)
        # 拖拽只多出「重算权重」那几行，遍历骨架完全一致
        self.assertEqual(drag[:5], plain[:5])
        self.assertEqual(drag[-3:], plain[-3:])


# ============================================================================
# 4. 资源名派生（真实 common/mod_path_compat.py）
# ============================================================================
class SparseResourceNameTests(unittest.TestCase):
    def test_vertex_entry_resource_names_follow_the_position_suffix_rule(self):
        base = "Resourced942b3a7Position"
        self.assertEqual(
            mod_path_compat.derive_shapekey_vertex_entry_start_resource_name(base),
            "Resourced942b3a7Position_VertexEntryStart",
        )
        self.assertEqual(
            mod_path_compat.derive_shapekey_vertex_entry_packed_resource_name(base),
            "Resourced942b3a7Position_VertexEntryPacked",
        )
        self.assertEqual(
            mod_path_compat.derive_shapekey_vertex_entry_freq_resource_name(base),
            "Resourced942b3a7Position_VertexEntryFreq",
        )

    def test_section_brackets_are_stripped_and_missing_position_is_appended(self):
        self.assertEqual(
            mod_path_compat.derive_shapekey_vertex_entry_start_resource_name("[Resource_abc_Position]"),
            "Resource_abc_Position_VertexEntryStart",
        )
        # 基名没有 _Position 时补上，保证与稠密 FREQ 表（..._Position_freq_indices）同级
        self.assertEqual(
            mod_path_compat.derive_shapekey_vertex_entry_freq_resource_name("Resource_abc"),
            "Resource_abc_Position_VertexEntryFreq",
        )

    def test_three_names_never_collide_with_the_dense_freq_table(self):
        base = "Resourced942b3a7Position"
        names = {
            mod_path_compat.derive_shapekey_freq_resource_name(base),
            mod_path_compat.derive_shapekey_vertex_entry_start_resource_name(base),
            mod_path_compat.derive_shapekey_vertex_entry_packed_resource_name(base),
            mod_path_compat.derive_shapekey_vertex_entry_freq_resource_name(base),
        }
        self.assertEqual(len(names), 4)


# ============================================================================
# 5. 节点侧：模板选择、前置项判定、真实模板的注入结果
# ============================================================================
def _make_shape_key_node(**overrides):
    node = node_module.SSMTNode_PostProcess_ShapeKey()
    node.use_packed_Meshess = True
    node.store_deltas = True
    node.store_all_vertex_channels = False
    node.use_optimized_lookup = True
    node.merge_slot_files = True
    node.use_sparse_vertex_index = False
    for key, value in overrides.items():
        setattr(node, key, value)
    return node


class SparseNodeWiringTests(unittest.TestCase):
    def test_template_is_the_v6_sparse_skeleton_when_all_preconditions_hold(self):
        node = _make_shape_key_node(use_sparse_vertex_index=True)
        self.assertTrue(node.effective_use_sparse_index())
        self.assertEqual(node._sparse_index_blockers(), [])
        self.assertEqual(node._get_shader_template_name(), "shapekey_anim_packed_delta_v6_sparse.hlsl")

    def test_each_missing_precondition_falls_back_to_the_old_skeleton(self):
        """缺前置项时**不能**用 v6 稀疏骨架：骨架声明了 t96/t97/t98，逻辑块却不会读它们。"""
        cases = (
            # (被关掉的项, 期望的缺失清单, 期望回落的模板)
            # 「紧凑缓冲区」关掉会连带让「合并槽位文件」失效（合并以紧凑为前提）
            (
                "merge_slot_files",
                ["合并槽位文件"],
                "shapekey_anim_packed_delta_v4_optimized.hlsl",
            ),
            (
                "use_packed_Meshess",
                ["合并槽位文件", "使用紧凑缓冲区"],
                "shapekey_anim_standard_delta_v3.hlsl",
            ),
            (
                "store_deltas",
                ["存储顶点增量"],
                "shapekey_anim_packed_v5_merged.hlsl",
            ),
            (
                "use_optimized_lookup",
                ["优化查找性能"],
                "shapekey_anim_packed_delta_v5_merged.hlsl",
            ),
        )
        for attribute, expected_blockers, expected_template in cases:
            with self.subTest(missing=attribute):
                node = _make_shape_key_node(use_sparse_vertex_index=True, **{attribute: False})
                self.assertFalse(node.effective_use_sparse_index())
                self.assertEqual(node._sparse_index_blockers(), expected_blockers)
                self.assertEqual(node._get_shader_template_name(), expected_template)

    def test_unchecked_option_is_silently_inert(self):
        node = _make_shape_key_node(use_sparse_vertex_index=False)
        self.assertEqual(node._sparse_index_blockers(), [])
        self.assertFalse(node.effective_use_sparse_index())
        self.assertEqual(node._get_shader_template_name(), "shapekey_anim_packed_delta_v5_merged.hlsl")

    def test_all_four_preconditions_are_reported_at_once(self):
        node = _make_shape_key_node(
            use_sparse_vertex_index=True,
            merge_slot_files=False,
            use_packed_Meshess=False,
            store_deltas=False,
            use_optimized_lookup=False,
        )
        self.assertEqual(
            node._sparse_index_blockers(),
            ["合并槽位文件", "使用紧凑缓冲区", "存储顶点增量", "优化查找性能"],
        )

    def test_injected_shader_iterates_only_hit_entries(self):
        node = _make_shape_key_node(use_sparse_vertex_index=True)
        node.INTENSITY_START_INDEX = 100
        node.VERTEX_RANGE_START_INDEX = 200
        node._get_vertex_struct_definition = lambda **_kwargs: (
            "struct VertexAttributes {\n"
            "    float3 position;\n"
            "    float3 normal;\n"
            "    float4 tangent;\n"
            "};"
        )

        template_path = REPO_ROOT / "Toolset" / "shapekey_anim_packed_delta_v6_sparse.hlsl"
        with tempfile.TemporaryDirectory() as temp_dir:
            shader_path = Path(temp_dir) / "shader.hlsl"
            shader_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")

            success = node._update_shader_file(
                str(shader_path),
                hash_slot_data={1: {"Smile": ["ObjA"]}},
                use_packed=True,
                use_delta=True,
                unique_names=["Smile"],
                unique_objects=["ObjA"],
                use_optimized=True,
                merge_slot_files=True,
                source_path=str(template_path),
                use_sparse_vertex_index=True,
            )

            self.assertTrue(success)
            shader_source = shader_path.read_text(encoding="utf-8")
            self.assertIn("vertex_entry_start[i]", shader_source)
            self.assertIn("vertex_entry_packed[sparse_entry]", shader_source)
            self.assertIn("ShapeKeyWeight[vertex_entry_freq[sparse_entry]]", shader_source)
            self.assertIn("merged_shapekey_pos_deltas[packed_index]", shader_source)
            # 稠密逐槽位展开（旧模型）不再出现在注入的逻辑块里。
            # 注意模板头部注释本身会提到 vertex_freq_indices 这个名字（说明它被取代了），
            # 所以断言必须限定在 LOGIC 块内，而不是整个文件。
            logic_block = shader_source.split("// --- [PYTHON-MANAGED LOGIC START] ---")[1].split(
                "// --- [PYTHON-MANAGED LOGIC END] ---"
            )[0]
            self.assertNotIn("vertex_freq_indices", logic_block)
            self.assertNotIn("num_slots", logic_block)
            # 骨架声明的三份稀疏缓冲仍然在（注入不能把它们抹掉）
            self.assertIn("StructuredBuffer<uint> vertex_entry_start : register(t96);", shader_source)
            self.assertIn("StructuredBuffer<int> vertex_entry_packed : register(t97);", shader_source)
            self.assertIn("StructuredBuffer<uint> vertex_entry_freq : register(t98);", shader_source)

    def test_injected_shader_falls_back_to_the_dense_unroll_without_the_option(self):
        node = _make_shape_key_node(use_sparse_vertex_index=False)
        node.INTENSITY_START_INDEX = 100
        node.VERTEX_RANGE_START_INDEX = 200
        node._get_vertex_struct_definition = lambda **_kwargs: (
            "struct VertexAttributes {\n"
            "    float3 position;\n"
            "    float3 normal;\n"
            "    float4 tangent;\n"
            "};"
        )

        template_path = REPO_ROOT / "Toolset" / "shapekey_anim_packed_delta_v5_merged.hlsl"
        with tempfile.TemporaryDirectory() as temp_dir:
            shader_path = Path(temp_dir) / "shader.hlsl"
            shader_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")

            success = node._update_shader_file(
                str(shader_path),
                hash_slot_data={1: {"Smile": ["ObjA"]}},
                use_packed=True,
                use_delta=True,
                unique_names=["Smile"],
                unique_objects=["ObjA"],
                use_optimized=True,
                merge_slot_files=True,
                source_path=str(template_path),
                use_sparse_vertex_index=False,
            )

            self.assertTrue(success)
            shader_source = shader_path.read_text(encoding="utf-8")
            self.assertIn("vertex_freq_indices", shader_source)
            self.assertNotIn("vertex_entry_start", shader_source)

    def test_sparse_flag_cannot_leak_into_the_dense_branch(self):
        """勾选但前置项不满足时，逻辑块必须回落到稠密展开（骨架/逻辑同进同退）。"""
        node = _make_shape_key_node(use_sparse_vertex_index=True, merge_slot_files=False)
        node.INTENSITY_START_INDEX = 100
        node.VERTEX_RANGE_START_INDEX = 200
        node._get_vertex_struct_definition = lambda **_kwargs: (
            "struct VertexAttributes {\n"
            "    float3 position;\n"
            "    float3 normal;\n"
            "    float4 tangent;\n"
            "};"
        )

        template_path = REPO_ROOT / "Toolset" / "shapekey_anim_packed_delta_v4_optimized.hlsl"
        with tempfile.TemporaryDirectory() as temp_dir:
            shader_path = Path(temp_dir) / "shader.hlsl"
            shader_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")

            success = node._update_shader_file(
                str(shader_path),
                hash_slot_data={1: {"Smile": ["ObjA"]}},
                use_packed=True,
                use_delta=True,
                unique_names=["Smile"],
                unique_objects=["ObjA"],
                use_optimized=True,
                merge_slot_files=False,
                source_path=str(template_path),
                use_sparse_vertex_index=True,
            )

            self.assertTrue(success)
            shader_source = shader_path.read_text(encoding="utf-8")
            self.assertNotIn("vertex_entry_start", shader_source)
            self.assertNotIn("sparse_entry", shader_source)


class SparseFrameTableExclusionTests(unittest.TestCase):
    """「顶点命中索引」与「帧表插值」在面板层就是硬互斥：勾一个自动取消另一个。

    与「存储顶点增量 / 储存全部顶点属性增量」同一套做法（``update=`` 回调），
    而不是只在面板上写一行提醒——两套渲染模型各有自己的骨架与资源段，
    同时勾选会把稀疏的三条缓冲绑到没有这些寄存器声明的帧表骨架上。
    """

    def test_checking_sparse_index_clears_the_frame_table(self):
        node = _make_shape_key_node(use_sparse_vertex_index=True, use_frame_table=True)
        node_module.sync_shapekey_sparse_index_mode(node, None)
        self.assertTrue(node.use_sparse_vertex_index)
        self.assertFalse(node.use_frame_table)

    def test_checking_the_frame_table_clears_sparse_index(self):
        node = _make_shape_key_node(use_sparse_vertex_index=True, use_frame_table=True)
        node_module.sync_shapekey_frame_table_mode(node, None)
        self.assertTrue(node.use_frame_table)
        self.assertFalse(node.use_sparse_vertex_index)

    def test_unchecking_leaves_the_other_switch_alone(self):
        node = _make_shape_key_node(use_sparse_vertex_index=False, use_frame_table=False)
        node_module.sync_shapekey_sparse_index_mode(node, None)
        node_module.sync_shapekey_frame_table_mode(node, None)
        self.assertFalse(node.use_sparse_vertex_index)
        self.assertFalse(node.use_frame_table)

    def test_both_switches_carry_the_exclusion_callback(self):
        source = (REPO_ROOT / "blueprint" / "node_postprocess_shapekey.py").read_text(encoding="utf-8")
        self.assertIn("update=sync_shapekey_sparse_index_mode", source)
        self.assertIn("update=sync_shapekey_frame_table_mode", source)
        self.assertIn("def _enforce_exclusive_sparse_frame_table(node, keep: str):", source)


class FrameTableDragExclusionTests(unittest.TestCase):
    """帧表插值与「拖拽交互」互斥：链路里有拖拽时帧表根本勾不上。

    导出期本来就有硬互斥（``direct_export_shapekey.py`` 的 ``raise``），但那是用户
    点完导出才吃到的报错；这里把闸移到勾选当场，和稀疏索引同一套 ``update=`` 做法。
    """

    class _DragNode:
        name = "拖拽交互"

    def test_checking_the_frame_table_with_a_drag_node_bounces_back(self):
        node = _make_shape_key_node()
        node._find_drag_drive_node = lambda: self._DragNode()
        node.use_frame_table = True
        node_module.sync_shapekey_frame_table_mode(node, None)
        self.assertFalse(node.use_frame_table)

    def test_without_a_drag_node_the_frame_table_stays_checked(self):
        node = _make_shape_key_node()
        node._find_drag_drive_node = lambda: None
        node.use_frame_table = True
        node_module.sync_shapekey_frame_table_mode(node, None)
        self.assertTrue(node.use_frame_table)

    def test_the_switch_is_greyed_out_in_the_panel(self):
        source = (REPO_ROOT / "blueprint" / "node_postprocess_shapekey.py").read_text(encoding="utf-8")
        self.assertIn("def _enforce_exclusive_drag_frame_table(node):", source)
        self.assertIn("_frame_table_row.enabled = self._find_drag_drive_node() is None", source)
        self.assertIn("链路里有拖拽交互，帧表插值不可用（两者互斥）", source)


# ============================================================================
# 6. 直出侧：ini 资源段与 Present 绑定
# ============================================================================
class _NodeStub:
    INTENSITY_START_INDEX = 100
    VERTEX_RANGE_START_INDEX = 60
    DRAG_DRIVE_REGISTER = 100
    DRAG_CLICK_COUNT_REGISTER = 101
    WEIGHT_BUFFER_REGISTER = 102

    def _extract_hash_prefix(self, logical_hash):
        return str(logical_hash).split("-")[0]

    def _hash_to_resource_prefix(self, logical_hash):
        return str(logical_hash).replace("-", "_")

    def get_shape_key_export_variable_name(self, name):
        return f"$Freq_{name}"

    def _compute_dispatch_group_count(self, vertex_count, threads_per_group=16):
        return max(1, int(vertex_count) // int(threads_per_group))

    def _get_merged_data_file_suffix(self, use_delta):
        return "_merged_packed_pos_delta" if use_delta else "_merged_pos_delta"

    def _describe_delta_scope(self, use_delta, hash_val=None):
        return "否" if not use_delta else "仅位置"

    def _resolve_delta_stride(self, hash_val=None, vertex_stride=None, struct_definition=None):
        return 12

    def _resolve_delta_channel_plan(self, hash_val=None, struct_definition=None, num_floats_per_vertex=None):
        return [("position", 0, 3)]

    @staticmethod
    def _channel_plan_columns(plan):
        return [0, 1, 2]

    @staticmethod
    def _channel_plan_names(plan):
        return [name for name, _start, _count in plan]

    @staticmethod
    def _channel_plan_float_count(plan):
        return sum(count for _name, _start, count in plan)

    def _drag_shapekey_click_count_resource_name(self, target_ini_file=None):
        return "ResourceDragShapeKeyClickCount_A"

    def _get_merged_map_file_suffix(self):
        return "_merged_map"

    def _get_freq_file_suffix(self):
        return "_freq_indices"

    def _write_ordered_dict_to_ini(self, *_args, **_kwargs):
        return None


class _Harness(mixin_module.DirectShapeKeyOutputMixin):
    def __init__(self, key_map=None):
        self.blueprint_model = types.SimpleNamespace(keyname_mkey_dict=key_map or {"$swap": object()})
        self.node = _NodeStub()


def _sections(extra=None):
    sections = OrderedDict([
        ("[Constants]", []),
        ("[Present]", []),
        (
            "[Resourced942b3a7Position]",
            ["type = Buffer", "stride = 40", "filename = Meshes0000/d942b3a7-Position.buf"],
        ),
    ])
    for name, lines in (extra or {}).items():
        sections[name] = lines
    return sections


def _run_update(sections, use_sparse_index=False):
    harness = _Harness()
    harness._update_ini_sections(
        sections,
        preserved_tail_content="",
        target_ini_file="mod.ini",
        slot_to_name_to_objects={},
        unique_hashes=["d942b3a7-39828-0"],
        hash_to_objects={"d942b3a7-39828-0": ["body"]},
        all_unique_names=["shape_a", "shape_b"],
        all_unique_objects=["body"],
        calculated_ranges={"body": (0, 10)},
        hash_to_stride={"d942b3a7": 40},
        # 生产侧 direct_export_shapekey.py:332 按**逻辑哈希**建表
        # （``hash_to_actual_file_hash[logical_hash] = actual_hash``），
        # 因此这里的键必须是完整逻辑哈希，否则会走 ``.get(logical_hash, logical_hash)`` 兜底。
        hash_to_actual_file_hash={"d942b3a7-39828-0": "d942b3a7"},
        hash_to_vertex_count={"d942b3a7": 32},
        hash_slot_data_map={"d942b3a7-39828-0": {1: {"shape_a": ["body"], "shape_b": ["body"]}}},
        hash_to_base_resources={"d942b3a7": ["Resourced942b3a7Position"]},
        use_packed=True,
        use_delta=True,
        use_optimized=True,
        merge_slot_files=True,
        use_sparse_index=use_sparse_index,
    )
    return harness


class SparseIniWiringTests(unittest.TestCase):
    def test_present_binds_the_three_sparse_buffers_instead_of_the_dense_freq_table(self):
        sections = _sections()
        _run_update(sections, use_sparse_index=True)

        shader = "\n".join(sections["[CustomShader_d942b3a7-39828-0_Anim]"])
        self.assertIn("cs-t96 = copy Resourced942b3a7Position_VertexEntryStart", shader)
        self.assertIn("cs-t97 = copy Resourced942b3a7Position_VertexEntryPacked", shader)
        self.assertIn("cs-t98 = copy Resourced942b3a7Position_VertexEntryFreq", shader)
        # 文件型缓冲必须 copy（ref 会让形态键整体失效），与稠密路径同一约束
        for register in ("cs-t96", "cs-t97", "cs-t98"):
            self.assertNotIn(f"{register} = ref ", shader)
        # 稀疏取代稠密 FREQ 表：t53 不再绑定
        self.assertNotIn("cs-t53", shader)
        # 合并数据/映射仍然照旧（稀疏只换 FREQ 表）
        self.assertIn("cs-t51 = copy ", shader)
        self.assertIn("cs-t52 = copy ", shader)

    def test_sparse_buffers_are_nulled_after_dispatch(self):
        sections = _sections()
        _run_update(sections, use_sparse_index=True)

        shader = "\n".join(sections["[CustomShader_d942b3a7-39828-0_Anim]"])
        for register in ("cs-t96", "cs-t97", "cs-t98"):
            self.assertIn(f"{register} = null", shader)

    def test_dense_mode_still_binds_the_freq_table(self):
        sections = _sections()
        _run_update(sections, use_sparse_index=False)

        shader = "\n".join(sections["[CustomShader_d942b3a7-39828-0_Anim]"])
        self.assertIn("cs-t53 = copy ", shader)
        self.assertNotIn("cs-t96", shader)
        self.assertNotIn("cs-t97", shader)
        self.assertNotIn("cs-t98", shader)

    def test_resource_sections_are_emitted_for_the_three_sparse_buffers(self):
        sections = _sections()
        _run_update(sections, use_sparse_index=True)

        generated = "\n".join(sections[";; --- Generated Shape Key Meshess ---"])
        for section_name, suffix in (
            ("[Resourced942b3a7Position_VertexEntryStart]", "_vertex_entry_start"),
            ("[Resourced942b3a7Position_VertexEntryPacked]", "_vertex_entry_packed"),
            ("[Resourced942b3a7Position_VertexEntryFreq]", "_vertex_entry_freq"),
        ):
            self.assertIn(section_name, generated)
            self.assertIn(f"filename = Meshes0000/d942b3a7-Position{suffix}.buf", generated)
        self.assertIn("type = Buffer", generated)
        self.assertIn("stride = 4", generated)
        # 稠密 FREQ 段不再生成
        self.assertNotIn("_freq_indices.buf", generated)

    def test_stale_dense_freq_section_is_removed_when_sparse_is_on(self):
        """sections 来自旧 ini：只「不新增」不够，旧稠密段必须显式移除（否则照样被加载）。"""
        sections = _sections({"[Resourced942b3a7Position_freq_indices]": ["type = Buffer", "stride = 4"]})
        _run_update(sections, use_sparse_index=True)

        self.assertNotIn("[Resourced942b3a7Position_freq_indices]", sections)

    def test_stale_sparse_sections_are_removed_when_sparse_is_off(self):
        sections = _sections({
            "[Resourced942b3a7Position_VertexEntryStart]": ["type = Buffer", "stride = 4"],
            "[Resourced942b3a7Position_VertexEntryPacked]": ["type = Buffer", "stride = 4"],
            "[Resourced942b3a7Position_VertexEntryFreq]": ["type = Buffer", "stride = 4"],
        })
        _run_update(sections, use_sparse_index=False)

        for stale in (
            "[Resourced942b3a7Position_VertexEntryStart]",
            "[Resourced942b3a7Position_VertexEntryPacked]",
            "[Resourced942b3a7Position_VertexEntryFreq]",
        ):
            self.assertNotIn(stale, sections)

    def test_reexport_does_not_duplicate_sparse_resource_sections(self):
        sections = _sections()
        _run_update(sections, use_sparse_index=True)
        _run_update(sections, use_sparse_index=True)

        generated = "\n".join(sections[";; --- Generated Shape Key Meshess ---"])
        self.assertEqual(generated.count("[Resourced942b3a7Position_VertexEntryStart]"), 1)
        self.assertEqual(generated.count("[Resourced942b3a7Position_VertexEntryPacked]"), 1)
        self.assertEqual(generated.count("[Resourced942b3a7Position_VertexEntryFreq]"), 1)


class SparseDriverWiringTests(unittest.TestCase):
    """直出驱动的接线（``blueprint/direct_export_shapekey.py``）。

    驱动本身太大，起不了轻量 harness，但它的三件事必须被锁住：
    ① 勾选却未生效时**明确失败**（与帧表同策略，不静默回退到稠密）；
    ② 标志一路传到着色器与 ini 两个阶段；
    ③ FREQ 表的写出器按标志二选一。
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "blueprint" / "direct_export_shapekey.py").read_text(
            encoding="utf-8"
        )
        cls.tree = ast.parse(cls.source)

    @staticmethod
    def _called_attributes(nodes):
        names = set()
        for node in nodes:
            for sub_node in ast.walk(node):
                if isinstance(sub_node, ast.Call) and isinstance(sub_node.func, ast.Attribute):
                    names.add(sub_node.func.attr)
        return names

    def _sparse_guard(self):
        """找到「勾选了稀疏索引就做前置校验」的那个分支。"""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.If) and "sparse_requested" in ast.unparse(node.test):
                return node
        return None

    def test_blocked_option_raises_instead_of_silently_falling_back(self):
        guard = self._sparse_guard()

        self.assertIsNotNone(guard, "必须存在「勾选但未生效」的前置校验分支")
        raises = [node for node in ast.walk(guard) if isinstance(node, ast.Raise)]
        self.assertTrue(raises, "该分支必须 raise，静默回退会让用户以为加速已生效")
        self.assertIn("ShapeKeyDirectExportError", ast.unparse(raises[0]))
        self.assertIn("「顶点命中索引（稀疏查找）」无法启用", self.source)
        self.assertIn("_sparse_index_blockers", self.source)

    def test_frame_table_conflict_is_explained_to_the_user(self):
        self.assertIn("与顶点命中索引是互斥的两套渲染模型", self.source)

    def test_frame_table_conflict_is_judged_inside_the_same_guard(self):
        """帧表与稀疏同时勾选时必须报错，哪怕四项前置齐全。

        所以「帧表冲突」要在同一个前置校验分支里追加，而不能只挂在「未生效」的
        附属判断下——否则两条路都成立时，稀疏资源段与 cs-t96/97/98 会被绑到帧表
        着色器上（它没有这些寄存器），加载即出错。
        """
        guard = self._sparse_guard()
        self.assertIsNotNone(guard)
        guard_source = ast.unparse(guard)
        self.assertIn("frame_table_mode", guard_source)
        self.assertIn("与顶点命中索引是互斥的两套渲染模型", guard_source)
        self.assertIn("sparse_blockers", guard_source)

    def test_flag_reaches_the_shader_and_ini_stages(self):
        keywords = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("_update_shader_file", "_update_ini_sections"):
                    keywords.setdefault(node.func.attr, set()).update(
                        keyword.arg for keyword in node.keywords
                    )

        self.assertIn(
            "use_sparse_vertex_index",
            keywords.get("_update_shader_file", set()),
            "标志没传到着色器阶段 ⇒ 骨架与逻辑块会不一致（顶点塌到原点）",
        )
        self.assertIn(
            "use_sparse_index",
            keywords.get("_update_ini_sections", set()),
            "标志没传到 ini 阶段 ⇒ 会绑错寄存器",
        )

    def test_freq_writer_is_dispatched_by_the_flag(self):
        branch = None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
                if node.test.id != "use_sparse_vertex_index":
                    continue
                if "_write_sparse_vertex_index" in self._called_attributes(node.body):
                    branch = node
                    break

        self.assertIsNotNone(branch, "必须按 use_sparse_vertex_index 分派 FREQ 表写出器")
        self.assertIn(
            "_write_freq_indices",
            self._called_attributes(branch.orelse),
            "未命中稀疏分支时必须仍然写出稠密 FREQ 表",
        )


if __name__ == "__main__":
    unittest.main()
