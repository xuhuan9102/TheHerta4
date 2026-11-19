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
    write_text_if_changed=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
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

    # --- 增量通道计划（本测试只关心签名门控，这里固定为旧版「仅位置」） ---
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


class _Harness(module.DirectShapeKeyOutputMixin):
    def __init__(self, key_map=None):
        self.blueprint_model = types.SimpleNamespace(keyname_mkey_dict=key_map or {"$swap": object()})
        self.node = _NodeStub()


def _sections():
    return OrderedDict([
        ("[Constants]", []),
        ("[Present]", []),
        (
            "[Resourced942b3a7Position]",
            ["type = Buffer", "stride = 40", "filename = Meshes0000/d942b3a7-Position.buf"],
        ),
    ])


def _run_update(sections, drag_drive_resource=None, frame_table_meta_map=None):
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
        hash_to_actual_file_hash={"d942b3a7": "d942b3a7"},
        hash_to_vertex_count={"d942b3a7": 32},
        hash_slot_data_map={"d942b3a7-39828-0": {1: {"shape_a": ["body"], "shape_b": ["body"]}}},
        hash_to_base_resources={"d942b3a7": ["Resourced942b3a7Position"]},
        use_packed=True,
        use_delta=True,
        use_optimized=True,
        merge_slot_files=True,
        drag_drive_resource=drag_drive_resource,
        frame_table_meta_map=frame_table_meta_map,
    )
    return harness


class ShapeKeyPresentSignatureWiringTests(unittest.TestCase):
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
