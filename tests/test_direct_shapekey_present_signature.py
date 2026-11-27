"""形态键 dispatch 脏签名门控的端到端接线测试。

覆盖 `DirectShapeKeyOutputMixin._update_ini_sections`：
- [Present] 的 `run = CustomShader_*_Anim` 被包进「签名变化才 dispatch」的内层 if；
- [Constants] 里补上签名变量声明（`= 0` / `= -1`）；
- 带拖拽驱动资源时，签名条件里追加 `$ssmtdrag_mode_{ns} == 1` 强制项。
"""
import importlib.util
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_direct_shapekey_present_signature_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

_install_module(
    f"{PKG}.common.mod_path_compat",
    **{
        name: (lambda *_args, **_kwargs: None)
        for name in (
            "collect_base_position_resource_map",
            "derive_shapekey_base_resource_name",
            "derive_shapekey_freq_resource_name",
            "derive_shapekey_merged_data_resource_name",
            "derive_shapekey_merged_map_resource_name",
            "derive_shapekey_slot_map_resource_name",
            "derive_shapekey_slot_resource_name",
            "derive_shapekey_frame_table_resource_name",
            "derive_shapekey_group_map_resource_name",
            "derive_shapekey_vertex_entry_start_resource_name",
            "derive_shapekey_vertex_entry_packed_resource_name",
            "derive_shapekey_vertex_entry_freq_resource_name",
            "derive_shapekey_weight_resource_name",
            "ensure_resource_alias_section",
        )
    },
)
_install_module(f"{PKG}.utils.log_utils", LOG=types.SimpleNamespace())
_install_module(
    f"{PKG}.common.safe_write",
    write_bytes_if_changed=lambda *_args, **_kwargs: None,
    write_text_if_changed=lambda path, source: path.write_text(source, encoding="utf-8"),
)
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    apply_full_vertex_override_in_place=lambda *_args, **_kwargs: None,
    apply_position_override_in_place=lambda *_args, **_kwargs: None,
    extract_position_bytes_by_indices=lambda *_args, **_kwargs: b"",
    assemble_drawib_position_bytes=lambda *_args, **_kwargs: (b"", 0),
    iter_drawib_models=lambda *_args, **_kwargs: [],
)
_install_module(
    f"{PKG}.blueprint.direct_export_shapekey_shared",
    ShapeKeyDirectExportError=RuntimeError,
    _buffer_to_bytes=lambda value: value,
    resolve_use_delta=lambda node: bool(
        getattr(node, "store_deltas", True)
        or getattr(node, "store_all_vertex_channels", False)
    ),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_output_mixin.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_output_mixin",
    module_path,
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.derive_shapekey_frame_table_resource_name = lambda base: f"{base}_FrameTable"
module.derive_shapekey_group_map_resource_name = lambda base, index: f"{base}_GroupMap{index}"
module.derive_shapekey_weight_resource_name = lambda base: f"{base}_ShapeKeyWeight"


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
        return "$Freq_" + name.replace(" ", "_")

    def _compute_dispatch_group_count(self, vertex_count, threads_per_group=16):
        return max(1, int(vertex_count) // int(threads_per_group))

    def _get_merged_data_file_suffix(self, use_delta):
        return "_merged_packed_pos_delta" if use_delta else "_merged_pos_delta"

    # --- 增量通道计划（本测试只关心签名门控，这里固定为旧版「仅位置」） ---
    def _describe_delta_scope(self, use_delta, hash_val=None):
        return "否" if not use_delta else "仅位置"

    def _resolve_delta_stride(self, hash_val=None, vertex_stride=None, struct_definition=None, *, force_all_vertex_channels=False):
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


class _Harness(module.DirectShapeKeyOutputMixin):
    def __init__(self, key_map=None):
        self.blueprint_model = types.SimpleNamespace(keyname_mkey_dict=key_map or {"$swap": object()})
        self.node = _NodeStub()

    def _merged_native_morph_carriers(self, _logical_hash):
        return []


def _sections():
    return OrderedDict([
        ("[Constants]", []),
        ("[Present]", []),
        (
            "[Resourced942b3a7Position]",
            ["type = Buffer", "stride = 40", "filename = Meshes0000/d942b3a7-Position.buf"],
        ),
    ])


def _run_update(sections, drag_drive_resource=None, frame_table_meta_map=None,
                target_ini_file="mod.ini", logical_hashes=("d942b3a7-39828-0",),
                frame_shader_sources=None, shape_names=("shape_a", "shape_b"),
                native_morph_plans=None, transfer_locks=None):
    harness = _Harness()
    harness._native_expression_locks = transfer_locks or {}
    harness._update_ini_sections(
        sections,
        preserved_tail_content="",
        target_ini_file=target_ini_file,
        slot_to_name_to_objects={},
        unique_hashes=list(logical_hashes),
        hash_to_objects={key: ["body"] for key in logical_hashes},
        all_unique_names=list(shape_names),
        all_unique_objects=["body"],
        calculated_ranges={"body": (0, 10)},
        hash_to_stride={"d942b3a7": 40},
        hash_to_actual_file_hash={key: "d942b3a7" for key in logical_hashes},
        hash_to_vertex_count={"d942b3a7": 32},
        hash_slot_data_map={key: {1: {name: ["body"] for name in shape_names}} for key in logical_hashes},
        hash_to_base_resources={"d942b3a7": ["Resourced942b3a7Position"]},
        use_packed=True,
        use_delta=True,
        use_optimized=True,
        merge_slot_files=True,
        drag_drive_resource=drag_drive_resource,
        frame_table_meta_map=frame_table_meta_map,
        frame_shader_sources=frame_shader_sources,
        native_morph_plans=native_morph_plans,
        native_morph_lookup_files={key: f"Meshes0000/{key}-Lookup.buf" for key in native_morph_plans or {}},
    )
    return harness


class ShapeKeyPresentSignatureWiringTests(unittest.TestCase):
    def test_native_morph_bridge_moves_merged_preskin_to_stable_blend(self):
        logical_hash = "d942b3a7-39828-0"
        position = "[TextureOverride_VB_d942b3a7_d942b3a7_Position]"
        blend = "[TextureOverride_VB_d942b3a7_d942b3a7_Blend]"
        deform = [
            "vb0 = Resourced942b3a7Position",
            "vb2 = Resourced942b3a7Blend",
            "if $zz_ms_occ_2 == 1",
            "    ResourceZZPalette_d942b3a7_s1 = copy vs-t0 unless_null",
            "endif",
            "handling = skip",
            "draw = 3773, 0",
        ]
        sections = _sections()
        sections[position] = ["hash = 12345678", *deform]
        sections[blend] = ["hash = 87654321", ";MARK:TextureOverrideVB"]
        model = types.SimpleNamespace(original_vertex_count=3466)
        h = _Harness()
        h.meshes_dir = "."
        h._native_morph_keys_for_export = lambda *_args: [
            module.native_morph.NativeMorphKey("0123456789abcdef", "12345678", "87654321", 4, 2, 2)
        ]
        shader_source = module_path.parents[1] / "Toolset" / "unused_template_name.hlsl"
        with patch.object(module, "LOG"), patch.object(module, "write_text_if_changed"):
            for _ in range(2):
                plans, _ = h._prepare_native_morph_bridge(
                    {logical_hash: {"drawib_model": model}}, [logical_hash],
                    {logical_hash: {1: {"Deform 2": ["face"]}}}, sections, ".", str(shader_source),
                )
                self.assertIn(logical_hash, plans)
        self.assertEqual(sections[position], ["hash = 12345678"])
        self.assertEqual(sections[blend], [
            "hash = 87654321", "match_vertex_count = 3466", *deform, ";MARK:TextureOverrideVB",
        ])

    def test_reanchored_face_capture_and_deform_match_the_same_blend_draw(self):
        draw_ib = "f137c27d"
        prefix = f"[TextureOverride_VB_{draw_ib}_{draw_ib}_"
        position, blend = prefix + "Position]", prefix + "Blend]"
        capture = ["match_priority = -100", f"ResourceZZPalette_{draw_ib}_s1 = copy vs-t0 unless_null"]
        sections = {
            position: ["hash = 12345678", "handling = skip", "draw = 180, 0"],
            blend: ["hash = 87654321"],
            prefix + "Position_NativePalette]": ["hash = 12345678", *capture],
            prefix + "Position_SkinInput_abcdef01]": ["hash = abcdef01", "handling = skip"],
            prefix + "Position_SkinInput_abcdef01_NativePalette]": ["hash = abcdef01", *capture],
        }
        self.assertTrue(module.native_morph.reanchor_merged_morph_preskin(sections, draw_ib, 90))
        self.assertEqual(sections, {
            position: ["hash = 12345678"],
            blend: ["hash = 87654321", "match_vertex_count = 90", "handling = skip", "draw = 180, 0"],
            prefix + "Blend_NativePalette]": ["hash = 87654321", "match_vertex_count = 90", *capture],
        })

    def test_stock_and_edited_faces_keep_their_control_and_capture_paths(self):
        edited = "d942b3a7-39828-0"
        stock = "8b067f99"
        key = module.native_morph.NativeMorphKey

        def channel(index, offset):
            return key("0123456789abcdef", "12345678", "87654321", offset, 2, index)

        stock_plan = module.native_morph.build_native_morph_plans(
            {stock: [channel(15, 10)]}, {stock: {15: 0}},
        )[stock]
        plans = module.native_morph.build_native_morph_plans(
            {edited: [channel(2, 20), channel(3, 30)]}, {edited: {2: 0, 3: 1}},
            source_filter_indices=stock_plan.source_filters,
        )
        sections = _sections()
        sections["[Constants]"].append("global persist $Freq_Deform_15 = 0.25")
        transfers = {
            stock: ({"Deform 15": "$Freq_Deform_1"}, stock_plan, "ResourceStockLockWeight", "Meshes0000/StockLookup.buf")
        }
        for _ in range(2):
            _run_update(
                sections, shape_names=("Deform 2", "Deform 3", "authored"),
                native_morph_plans=plans, transfer_locks=transfers,
            )

        constants = sections["[Constants]"]
        self.assertEqual(constants.count("global persist $Freq_Deform_1 = 0.0"), 1)
        self.assertIn("global persist $Freq_Deform_15 = 0.25", constants)
        sync = "\n".join(sections[f"[CustomShaderShapeKeyWeightSync_{edited}]"])
        self.assertIn("x100 = $Freq_Deform_2", sync)
        self.assertIn("y100 = $Freq_Deform_3", sync)
        self.assertIn("z100 = $Freq_authored", sync)
        gate = f"$ssmt_native_expression_override_{stock}"
        self.assertEqual(constants.count(f"global {gate} = 0"), 1)
        present = [line.strip() for line in sections["[Present]"]]
        for logical_hash in (edited, stock):
            self.assertEqual(present.count(f"run = {module.native_morph.custom_shader_clear_name(logical_hash)}"), 1)
        self.assertIn(f"post {gate} = $Freq_Deform_1 != 0", present)
        self.assertIn(f"run = CustomShaderShapeKeyWeightSync_{edited}", present)
        self.assertNotIn("if $Freq_Deform_2 != 0 || $Freq_Deform_3 != 0", present)
        self.assertIn(f"    if {gate} != 0", sections["[CommandListZZMINativeMorphCaptureGeneric]"])
        self.assertIn(f"[CustomShaderZZMINativeMorphCapture_{stock}]", sections)
        self.assertIn(f"[CustomShaderZZMINativeMorphCapture_{edited.replace('-', '_')}]", sections)
        self.assertEqual(sum(name.startswith("[TextureOverrideZZMINativeMorphSource_") for name in sections), 1)

    def test_native_only_face_skips_manual_sync_but_still_animates_when_zero(self):
        edited = "d942b3a7-39828-0"
        key = module.native_morph.NativeMorphKey
        plans = module.native_morph.build_native_morph_plans(
            {edited: [key("0123456789abcdef", "12345678", "87654321", 4, 2, 2)]},
            {edited: {2: 0}},
        )
        sections = _sections()
        _run_update(sections, shape_names=("Deform 2",), native_morph_plans=plans)

        present = [line.strip() for line in sections["[Present]"]]
        start = present.index("if $Freq_Deform_2 != 0")
        self.assertEqual(present[start + 1:start + 4], [
            f"run = CustomShaderShapeKeyWeightSync_{edited}", "endif", f"run = CustomShader_{edited}_Anim",
        ])

    def test_frame_table_inputs_use_compatible_shader_resource_references(self):
        sections = _sections()
        table = "Resourced942b3a7Position_FrameTable"
        group_map = "Resourced942b3a7Position_GroupMap0"
        sections[f"[{table}]"] = ["type = Buffer", "stride = 4", "filename = old.buf"]
        _run_update(sections, frame_table_meta_map={
            "d942b3a7-39828-0": {
                "groups": [{"map_register": 52, "progress": "$Freq_Group1", "group": 1}],
            },
        })
        shader = "\n".join(sections["[CustomShader_d942b3a7-39828-0_Anim]"])
        self.assertIn(f"cs-t51 = ref {table}", shader)
        self.assertIn(f"cs-t52 = ref {group_map}", shader)
        self.assertIn("cs-u5 = copy ", shader)
        self.assertNotIn(f"[{table}]", sections)
        resources = "\n".join(sections[";; --- Generated Shape Key Meshess ---"])
        for resource in (table, group_map):
            self.assertIn(f"[{resource}]\ntype = StructuredBuffer\nstride = 4\nbind_flags = shader_resource", resources)

    def test_equivalent_frame_table_dispatches_run_once(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        first, second = "d942b3a7-39828-0", "d942b3a7-300-39828"
        meta = {"groups": [{"map_register": 52, "progress": "$Freq_Group1", "group": 1}]}
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "res").mkdir()
            for second_shader, expected_count in (("matching", 1), ("different", 2)):
                with self.subTest(expected_count=expected_count):
                    sections = _sections()
                    _run_update(sections, target_ini_file=str(root / "mod.ini"),
                                logical_hashes=(first, second),
                                frame_table_meta_map={first: meta, second: meta},
                                frame_shader_sources={first: "matching", second: second_shader})
                    self.assertEqual(len(list((root / "res").glob("*.hlsl"))), expected_count)
                    present = "\n".join(sections["[Present]"])
                    constants = "\n".join(sections["[Constants]"])
                    self.assertEqual(present.count("run = CustomShaderShapeKeyWeightSync_"), expected_count)
                    self.assertEqual(present.count("run = CustomShader_d942b3a7-"), expected_count)
                    self.assertEqual(constants.count("post run = CustomShader_d942b3a7-"), expected_count)
                    self.assertEqual(constants.count(" = copy_desc "), expected_count)

    def test_present_block_is_gated_by_signature(self):
        sections = _sections()
        _run_update(sections)

        present = "\n".join(sections["[Present]"])
        self.assertIn("; --- SSMT DIRECT SHAPEKEY PRESENT BEGIN ---", present)
        self.assertIn("if $active0 == 1", present)
        self.assertIn(
            "$ssmt_sk_sig_d942b3a7 = $Freq_shape_a * 1 + $Freq_shape_b * 3",
            present,
        )
        self.assertIn(
            "if $ssmt_sk_sig_d942b3a7 != $ssmt_sk_sig_prev_d942b3a7",
            present,
        )
        self.assertIn("run = CustomShader_d942b3a7-39828-0_Anim", present)
        self.assertIn("$ssmt_sk_sig_prev_d942b3a7 = $ssmt_sk_sig_d942b3a7", present)

    def test_file_backed_inputs_must_use_copy_not_ref(self):
        """回归（实机结论）：这些**文件型**缓冲必须用 `copy` 绑定，`ref` 会让形态键整体失效。

        曾经把只读绑定改成 `ref`（EFMI 同族写法，可省掉每次执行的全量拷贝），实机结果：
        **动画直接不动**，且监控面板的 `Full resource copies` 也没降到 1 —— 说明本 fork
        下 `ref` 不会把文件型缓冲就绪到着色器槽位，`copy` 才是必需的（它顺带完成首次上传）。
        故这里锁死为 copy。
        """
        sections = _sections()
        _run_update(sections)

        shader = "\n".join(sections["[CustomShader_d942b3a7-39828-0_Anim]"])
        for register in ("cs-t51", "cs-t52", "cs-t53"):
            self.assertIn(f"{register} = copy ", shader)
            self.assertNotIn(f"{register} = ref ", shader)
        self.assertIn("cs-u5 = copy ", shader, "可写输出必须是 copy（CS 要写进去）")
        self.assertIn("cs-u5 = null", shader)

    def test_signature_variables_are_declared_in_constants(self):
        sections = _sections()
        _run_update(sections)

        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmt_sk_sig_d942b3a7 = 0", constants)
        self.assertIn("global $ssmt_sk_sig_prev_d942b3a7 = -1", constants)
        # 强度声明仍然照常发射
        self.assertIn("global persist $Freq_shape_a = 0.0", constants)

    def test_drag_drive_forces_dispatch_and_is_declared(self):
        sections = _sections()
        _run_update(sections, drag_drive_resource="ResourceDragShapeKeyDrive_A")

        present = "\n".join(sections["[Present]"])
        self.assertIn(
            "if $ssmt_sk_sig_d942b3a7 != $ssmt_sk_sig_prev_d942b3a7"
            " || $ssmtdrag_mode_A == 1",
            present,
        )

    def test_reexport_does_not_duplicate_signature_declarations(self):
        sections = _sections()
        _run_update(sections)
        _run_update(sections)

        constants = "\n".join(sections["[Constants]"])
        self.assertEqual(constants.count("global $ssmt_sk_sig_d942b3a7 = 0"), 1)
        self.assertEqual(constants.count("global $ssmt_sk_sig_prev_d942b3a7 = -1"), 1)
        present = "\n".join(sections["[Present]"])
        self.assertEqual(present.count("; --- SSMT DIRECT SHAPEKEY PRESENT BEGIN ---"), 1)
        self.assertEqual(present.count("run = CustomShader_d942b3a7-39828-0_Anim"), 1)


if __name__ == "__main__":
    unittest.main()
