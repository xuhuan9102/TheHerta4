import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_direct_shapekey_activation_guard_test_pkg"
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
    write_bytes_if_changed=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    apply_position_override_in_place=lambda *_args, **_kwargs: None,
    apply_full_vertex_override_in_place=lambda *_args, **_kwargs: None,
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


class _NodeStub:
    def _extract_hash_prefix(self, logical_hash):
        return str(logical_hash).split("-")[0]


class _Harness(module.DirectShapeKeyOutputMixin):
    def __init__(self, key_map):
        self.blueprint_model = types.SimpleNamespace(keyname_mkey_dict=key_map)
        self.node = _NodeStub()


class DirectShapeKeyActivationGuardTests(unittest.TestCase):
    def test_without_object_switch_keys_runs_unconditionally(self):
        lines = _Harness({})._build_present_run_block(["abc"])

        self.assertIn("run = CustomShader_abc_Anim", lines)
        self.assertNotIn("if $active0 == 1", lines)

    def test_with_object_switch_keys_uses_active_guard(self):
        lines = _Harness({"$swap": object()})._build_present_run_block(["abc"])

        self.assertIn("if $active0 == 1", lines)
        self.assertIn("    run = CustomShader_abc_Anim", lines)
        self.assertIn("endif", lines)


class ShapeKeySignatureGateTests(unittest.TestCase):
    """形态键 dispatch 的脏签名门控：静止帧不重算（省 ~69MB/帧 的缓冲流量）。"""

    def _build(self, **kwargs):
        return _Harness({"$swap": object()})._build_present_run_block(
            ["d942b3a7-39828-0"], **kwargs
        )

    def test_signature_gate_wraps_run_inside_active_guard(self):
        lines = self._build(signature_vars=["$Freq_a", "$Freq_b"])

        self.assertIn("if $active0 == 1", lines)
        self.assertIn(
            "    $ssmt_sk_sig_d942b3a7 = $Freq_a * 1 + $Freq_b * 3",
            lines,
        )
        self.assertIn(
            "    if $ssmt_sk_sig_d942b3a7 != $ssmt_sk_sig_prev_d942b3a7",
            lines,
        )
        self.assertIn("        run = CustomShader_d942b3a7-39828-0_Anim", lines)
        self.assertIn(
            "        $ssmt_sk_sig_prev_d942b3a7 = $ssmt_sk_sig_d942b3a7",
            lines,
        )
        self.assertEqual(
            sum(1 for line in lines if line.strip() == "endif"),
            2,
            "内外两层 if 都要闭合",
        )
        self.assertEqual(lines[0], module.DirectShapeKeyOutputMixin._PRESENT_RUN_BEGIN)
        self.assertEqual(lines[-1], module.DirectShapeKeyOutputMixin._PRESENT_RUN_END)

    def test_drag_mode_forces_dispatch(self):
        lines = self._build(
            signature_vars=["$Freq_a"],
            drag_active_var="$ssmtdrag_mode_A",
        )

        self.assertIn(
            "    if $ssmt_sk_sig_d942b3a7 != $ssmt_sk_sig_prev_d942b3a7"
            " || $ssmtdrag_mode_A == 1",
            lines,
        )

    def test_without_signature_vars_keeps_legacy_form(self):
        lines = self._build()

        self.assertIn("    run = CustomShader_d942b3a7-39828-0_Anim", lines)
        self.assertNotIn("$ssmt_sk_sig", "\n".join(lines))
        self.assertEqual(sum(1 for line in lines if line.strip() == "endif"), 1)

    def test_signature_gate_without_active_guard_still_applies(self):
        lines = _Harness({})._build_present_run_block(
            ["abc"], signature_vars=["$Freq_a"]
        )

        self.assertNotIn("if $active0 == 1", lines)
        self.assertIn("$ssmt_sk_sig_abc = $Freq_a * 1", lines)
        self.assertIn("if $ssmt_sk_sig_abc != $ssmt_sk_sig_prev_abc", lines)
        self.assertIn("    run = CustomShader_abc_Anim", lines)
        self.assertEqual(sum(1 for line in lines if line.strip() == "endif"), 1)

    def test_long_signature_is_split_into_bounded_lines(self):
        params = [f"$Freq_{index}" for index in range(40)]
        lines = self._build(signature_vars=params)
        signature_lines = [line for line in lines if "$ssmt_sk_sig_d942b3a7 = " in line]

        self.assertGreater(len(signature_lines), 1, "超长签名必须拆成多条赋值")
        for line in signature_lines:
            self.assertLess(len(line), 600)

    def test_signature_weights_are_distinct(self):
        weights = module.DirectShapeKeyOutputMixin._SK_SIGNATURE_WEIGHTS
        self.assertEqual(len(weights), len(set(weights)), "权重必须互不相同")
        self.assertTrue(all(weight > 0 for weight in weights))

    def test_drag_mode_variable_derivation(self):
        derive = module.DirectShapeKeyOutputMixin._drag_mode_variable_for

        self.assertEqual(derive("ResourceDragShapeKeyDrive_A"), "$ssmtdrag_mode_A")
        self.assertEqual(derive("ResourceDragShapeKeyDrive_testns"), "$ssmtdrag_mode_testns")
        self.assertEqual(derive(""), "")
        self.assertEqual(derive("ResourceDragShapeKeyClickCount_A"), "")

    def test_signature_var_names_are_per_hash(self):
        harness = _Harness({"$swap": object()})
        first = harness._sk_signature_var_names(["d942b3a7-39828-0"])
        second = harness._sk_signature_var_names(["480eeade-6552-0"])

        self.assertNotEqual(first, second, "多个形态键节点不能共用签名变量")


if __name__ == "__main__":
    unittest.main()
