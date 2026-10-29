"""形态键帧表「分组计划」的构建契约测试。

覆盖 `DirectShapeKeyOutputMixin._build_frame_table_plan`（帧表整条路径里唯一
纯逻辑、且是「信息不全就放弃」的强约束入口）：

- 组内成员数与 ini 侧完整成员数一致才算可用；
- 整个组都不属于本网格时跳过（不是错误）；
- 只命中组内一部分时放弃帧表（否则时间轴 `t * M` 与 `t * N` 不再等价）；
- 未分组的键各自成为单帧组；
- 键没有强度变量、组数超过 t 寄存器上限时放弃帧表。

放弃（返回 None）在上层意味着回退旧模型，不是失败，所以这里逐条钉住边界。
"""
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


PKG = "_frame_table_plan_test_pkg"
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
            "derive_shapekey_slot_map_resource_name",
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
    resolve_use_delta=lambda node: bool(getattr(node, "store_deltas", True)),
)

module_path = Path(__file__).resolve().parents[1] / "blueprint" / "direct_export_shapekey_output_mixin.py"
spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_output_mixin",
    module_path,
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class _PlanOwner:
    """`_build_frame_table_plan` 只读一个类属性，不需要完整节点。"""

    def __init__(self, max_groups=8):
        self.FRAME_TABLE_MAX_GROUPS = max_groups


def _params(*names):
    return {name: f"$Freq_{name}" for name in names}


def _plan(unique_names, freq_params, sequence_groups, group_totals=None, max_groups=8):
    return module.DirectShapeKeyOutputMixin._build_frame_table_plan(
        _PlanOwner(max_groups),
        unique_names,
        freq_params,
        sequence_groups,
        group_totals,
    )


class FrameTablePlanTests(unittest.TestCase):
    def test_full_group_then_ungrouped_keys_as_single_frame_groups(self):
        plan = _plan(
            ["a", "b", "c", "d"],
            _params("a", "b", "c", "d"),
            {0: ["Freq_a", "Freq_b"]},
            {0: 2},
        )
        self.assertEqual(
            [
                {"group": 0, "progress": "$Freq_Group0", "members": [0, 1]},
                {"group": None, "progress": "$Freq_c", "members": [2]},
                {"group": None, "progress": "$Freq_d", "members": [3]},
            ],
            plan["groups"],
        )

    def test_group_order_follows_ini_sequence_not_index_order(self):
        # 组内顺序决定帧表的时刻顺序，必须按 ini 里写下的序列，而不是按键索引重排。
        plan = _plan(
            ["a", "b", "c"],
            _params("a", "b", "c"),
            {0: ["Freq_c", "Freq_a"]},
            {0: 2},
        )
        self.assertEqual([2, 0], plan["groups"][0]["members"])

    def test_group_of_another_mesh_is_skipped_without_abandoning_plan(self):
        # 例：尿道组只在上半身网格里；本网格一个成员都没命中，不是错误。
        plan = _plan(
            ["a"],
            _params("a"),
            {0: ["Freq_only_in_other_mesh"]},
            {0: 3},
        )
        self.assertEqual([{"group": None, "progress": "$Freq_a", "members": [0]}], plan["groups"])

    def test_partial_group_match_abandons_frame_table(self):
        # 命中 2 个但 ini 侧该组有 3 个成员 → `t * M` 与 `t * N` 不再等价，回退旧模型。
        self.assertIsNone(
            _plan(
                ["a", "b", "c"],
                _params("a", "b", "c"),
                {0: ["Freq_a", "Freq_b"]},
                {0: 3},
            )
        )

    def test_group_without_known_total_is_accepted(self):
        plan = _plan(
            ["a", "b"],
            _params("a", "b"),
            {0: ["Freq_a"]},
            None,
        )
        self.assertEqual(
            [
                {"group": 0, "progress": "$Freq_Group0", "members": [0]},
                {"group": None, "progress": "$Freq_b", "members": [1]},
            ],
            plan["groups"],
        )

    def test_key_without_strength_variable_abandons_frame_table(self):
        self.assertIsNone(_plan(["a", "b"], _params("a"), {}))

    def test_group_count_at_limit_is_kept_and_over_limit_abandons(self):
        names = ["a", "b", "c"]
        self.assertIsNotNone(_plan(names, _params(*names), {}, max_groups=3))
        self.assertIsNone(_plan(names, _params(*names), {}, max_groups=2))

    def test_empty_key_set_abandons_frame_table(self):
        self.assertIsNone(_plan([], {}, {}))


if __name__ == "__main__":
    unittest.main()
