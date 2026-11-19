"""Shared-resource reuse for direct shape-key Position, FREQ and frame tables."""

import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_direct_shapekey_resource_reuse_test_pkg"
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
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.common.safe_write",
    write_text_if_changed=lambda *_args, **_kwargs: None,
)


def _apply_position_override(state_bytes, position_bytes, export_indices, position_stride):
    state_rows = np.frombuffer(state_bytes, dtype=np.uint8).reshape(-1, position_stride)
    position_rows = np.frombuffer(position_bytes, dtype=np.uint8).reshape(-1, position_stride)
    indices = np.asarray(export_indices, dtype=np.int64)
    if indices.size != len(position_rows):
        raise ValueError("position override length mismatch")
    state_rows[indices] = position_rows


_install_module(
    f"{PKG}.blueprint.direct_export_runtime_utils",
    apply_position_override_in_place=_apply_position_override,
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

module_path = (
    Path(__file__).resolve().parents[1]
    / "blueprint"
    / "direct_export_shapekey_output_mixin.py"
)
spec = importlib.util.spec_from_file_location(
    f"{PKG}.blueprint.direct_export_shapekey_output_mixin",
    module_path,
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class _Node:
    use_packed_Meshess = True
    store_deltas = True

    @staticmethod
    def _extract_hash_from_name(name):
        return str(name).split("-", 1)[0]

    @staticmethod
    def _extract_hash_prefix(value):
        return str(value).split("-", 1)[0]

    @staticmethod
    def _get_vertex_struct_definition():
        return None

    @staticmethod
    def _detect_vertex_format(base_bytes, _target_bytes, _struct_definition, preferred_stride):
        return int(preferred_stride), int(preferred_stride) // 4, len(base_bytes) // int(preferred_stride)

    @staticmethod
    def _resolve_delta_channel_plan(**_kwargs):
        return [("position", 0, 3)]

    @staticmethod
    def _channel_plan_columns(plan):
        return [
            column
            for _name, start, count in plan
            for column in range(start, start + count)
        ]

    @staticmethod
    def _channel_plan_float_count(plan):
        return sum(count for _name, _start, count in plan)


class _CountingHarness(module.DirectShapeKeyOutputMixin):
    def __init__(self, root):
        self.node = _Node()
        self.meshes_dir = str(root)
        self.counts = {
            "position": 0,
            "freq": 0,
            "frame_table": 0,
        }
        self.call_order = []

    def _write_merged_slot_files(self, **kwargs):
        self.counts["position"] += 1
        self.call_order.append("position")
        return super()._write_merged_slot_files(**kwargs)

    def _write_slot_files(self, **kwargs):
        self.counts["position"] += 1
        self.call_order.append("position")
        return super()._write_slot_files(**kwargs)

    def _write_freq_indices(self, **kwargs):
        self.counts["freq"] += 1
        self.call_order.append("freq")
        return super()._write_freq_indices(**kwargs)

    def _write_sparse_vertex_index(self, **kwargs):
        self.counts["freq"] += 1
        self.call_order.append("freq")
        return super()._write_sparse_vertex_index(**kwargs)

    def _write_frame_table_files(self, **kwargs):
        self.counts["frame_table"] += 1
        self.call_order.append("frame_table")
        return super()._write_frame_table_files(**kwargs)


def _base_coords():
    return np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
        dtype=np.float32,
    )


def _case(
    target_coords=None,
    base_coords=None,
    logical_hash="abc12345-part",
    channel_plan=None,
    frame_plan=None,
):
    base = _base_coords() if base_coords is None else np.asarray(base_coords, dtype=np.float32)
    target = base.copy() if target_coords is None else np.asarray(target_coords, dtype=np.float32)
    channel_plan = channel_plan or [("position", 0, 3)]
    frame_plan = frame_plan or {
        "groups": [
            {
                "group": 0,
                "progress": "$Freq_Group0",
                "members": [0],
            }
        ]
    }
    runtime_info = {
        "logical_hash": logical_hash,
        "actual_hash": "abc12345",
        "base_path": "Meshes0000/abc12345-Position.buf",
        "base_bytes": base.tobytes(),
        "position_stride": 12,
        "vertex_count": len(base),
    }
    objects = ["abc12345-body"]
    hash_slot_data = {1: {"shape_a": list(objects)}}
    slot_position_overrides = {
        1: {
            objects[0]: {
                "export_indices": np.arange(len(base), dtype=np.int32),
                "position_bytes": target.tobytes(),
            }
        }
    }
    calculated_ranges = {objects[0]: (0, len(base) - 1, "part")}
    return {
        "runtime_info": runtime_info,
        "hash_slot_data": hash_slot_data,
        "slot_position_overrides": slot_position_overrides,
        "calculated_ranges": calculated_ranges,
        "unique_names": ["shape_a"],
        "channel_plan": channel_plan,
        "frame_plan": frame_plan,
    }


def _signature(harness, case, shared_overrides):
    return harness._build_shapekey_resource_equivalence_signature(
        actual_hash=case["runtime_info"]["actual_hash"],
        logical_hash=case["runtime_info"]["logical_hash"],
        runtime_info=case["runtime_info"],
        hash_slot_data=case["hash_slot_data"],
        slot_position_overrides=shared_overrides,
        calculated_ranges=case["calculated_ranges"],
        unique_names=case["unique_names"],
        channel_plan=case["channel_plan"],
        frame_plan=case["frame_plan"],
        use_packed=True,
        use_delta=True,
        use_optimized=True,
        merge_slot_files=True,
        use_sparse_vertex_index=False,
        frame_table_mode=True,
    )


def _run_ordered_pipeline(harness, cases, shared_overrides):
    logical_resource_meta = {}
    actual_resource_members = {}
    for case in cases:
        logical_hash = case["runtime_info"]["logical_hash"]
        signature = _signature(harness, case, shared_overrides)
        logical_resource_meta[logical_hash] = {
            "runtime_info": case["runtime_info"],
            "hash_slot_data": case["hash_slot_data"],
            "unique_names": case["unique_names"],
            "frame_plan": case["frame_plan"],
            "channel_plan": case["channel_plan"],
            "signature": signature,
        }
        actual_resource_members.setdefault(
            case["runtime_info"]["actual_hash"], []
        ).append(logical_hash)

    reusable = harness._select_equivalent_shapekey_resources(
        logical_resource_meta,
        actual_resource_members,
        frame_table_mode=True,
    )
    position_cache = {}
    downstream_cache = {}
    for case in cases:
        logical_hash = case["runtime_info"]["logical_hash"]
        actual_hash = case["runtime_info"]["actual_hash"]
        if actual_hash in reusable and actual_hash in position_cache:
            continue
        merged_map = harness._write_merged_slot_files(
            logical_hash=logical_hash,
            runtime_info=case["runtime_info"],
            hash_slot_data=case["hash_slot_data"],
            slot_position_overrides=shared_overrides,
        )
        if actual_hash in reusable:
            position_cache[actual_hash] = merged_map

    for case in cases:
        logical_hash = case["runtime_info"]["logical_hash"]
        actual_hash = case["runtime_info"]["actual_hash"]
        if actual_hash in reusable and actual_hash in downstream_cache:
            continue
        harness._write_freq_indices(
            logical_hash=logical_hash,
            actual_hash=actual_hash,
            hash_slot_data=case["hash_slot_data"],
            unique_names=case["unique_names"],
            vertex_count=case["runtime_info"]["vertex_count"],
            calculated_ranges=case["calculated_ranges"],
            merged_index_map=position_cache.get(actual_hash),
        )
        frame_meta = harness._write_frame_table_files(
            actual_hash=actual_hash,
            vertex_count=case["runtime_info"]["vertex_count"],
            plan=case["frame_plan"],
            inputs=harness._read_frame_table_inputs(actual_hash, case["channel_plan"]),
        )
        if actual_hash in reusable:
            downstream_cache[actual_hash] = frame_meta
    return reusable


def _write_base(root, case):
    (root / "abc12345-Position.buf").write_bytes(case["runtime_info"]["base_bytes"])


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class DirectShapeKeyResourceReuseTests(unittest.TestCase):
    def test_equivalent_resource_builds_once_and_matches_control(self):
        first = _case(
            target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]),
            logical_hash="abc12345-part01",
        )
        second = _case(
            target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]),
            logical_hash="abc12345-part02",
        )
        shared_overrides = first["slot_position_overrides"]
        second["slot_position_overrides"] = shared_overrides

        with tempfile.TemporaryDirectory() as control_temp, tempfile.TemporaryDirectory() as reused_temp:
            control_root = Path(control_temp)
            reused_root = Path(reused_temp)
            _write_base(control_root, first)
            _write_base(reused_root, first)

            control = _CountingHarness(control_root)
            _run_ordered_pipeline(control, [first], shared_overrides)

            reused = _CountingHarness(reused_root)
            reusable = _run_ordered_pipeline(reused, [first, second], shared_overrides)

            self.assertEqual(reusable, {"abc12345"})
            self.assertEqual(reused.counts, {"position": 1, "freq": 1, "frame_table": 1})
            self.assertEqual(control.counts, {"position": 1, "freq": 1, "frame_table": 1})
            self.assertEqual(_snapshot(reused_root), _snapshot(control_root))

    def test_aba_non_equivalent_group_runs_all_builds_and_last_a_wins(self):
        first = _case(
            target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]),
            logical_hash="abc12345-part01",
        )
        second = _case(
            target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]),
            base_coords=_base_coords() + np.float32([2.0, 0.0, 0.0]),
            logical_hash="abc12345-part02",
        )
        third = _case(
            target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]),
            logical_hash="abc12345-part03",
        )
        shared_overrides = first["slot_position_overrides"]
        second["slot_position_overrides"] = shared_overrides
        third["slot_position_overrides"] = shared_overrides

        with tempfile.TemporaryDirectory() as control_temp, tempfile.TemporaryDirectory() as candidate_temp:
            control_root = Path(control_temp)
            candidate_root = Path(candidate_temp)
            _write_base(control_root, third)
            _write_base(candidate_root, first)

            control = _CountingHarness(control_root)
            _run_ordered_pipeline(control, [third], shared_overrides)

            candidate = _CountingHarness(candidate_root)
            reusable = _run_ordered_pipeline(
                candidate,
                [first, second, third],
                shared_overrides,
            )

            self.assertEqual(reusable, set())
            self.assertEqual(candidate.counts, {"position": 3, "freq": 3, "frame_table": 3})
            self.assertEqual(
                candidate.call_order,
                [
                    "position",
                    "position",
                    "position",
                    "freq",
                    "frame_table",
                    "freq",
                    "frame_table",
                    "freq",
                    "frame_table",
                ],
            )
            self.assertEqual(_snapshot(candidate_root), _snapshot(control_root))

    def test_equivalence_signature_covers_base_order_channels_and_frame_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = _CountingHarness(Path(temp))
            baseline = _case(target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]))
            copied = _case(target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]))
            shared_overrides = baseline["slot_position_overrides"]
            copied["slot_position_overrides"] = shared_overrides
            self.assertEqual(
                _signature(harness, baseline, shared_overrides),
                _signature(harness, copied, shared_overrides),
            )

            changed_base = _case(target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]))
            changed_base["runtime_info"]["base_path"] = "Meshes0000/other-Position.buf"
            self.assertNotEqual(
                _signature(harness, baseline, shared_overrides),
                _signature(harness, changed_base, shared_overrides),
            )

            changed_order = _case(target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]))
            changed_order["hash_slot_data"] = {
                1: {
                    "shape_a": ["abc12345-body"],
                    "shape_b": ["abc12345-body"],
                }
            }
            changed_order["unique_names"] = ["shape_a", "shape_b"]
            self.assertNotEqual(
                _signature(harness, baseline, shared_overrides),
                _signature(harness, changed_order, shared_overrides),
            )

            changed_channel = _case(
                target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]),
                channel_plan=[("position", 0, 2), ("normal", 2, 1)],
            )
            changed_channel["slot_position_overrides"] = shared_overrides
            self.assertNotEqual(
                _signature(harness, baseline, shared_overrides),
                _signature(harness, changed_channel, shared_overrides),
            )

            changed_plan = _case(
                target_coords=_base_coords() + np.float32([0.0, 0.25, 0.0]),
                frame_plan={
                    "groups": [
                        {
                            "group": 1,
                            "progress": "$Freq_Group1",
                            "members": [0],
                        }
                    ]
                },
            )
            changed_plan["slot_position_overrides"] = shared_overrides
            self.assertNotEqual(
                _signature(harness, baseline, shared_overrides),
                _signature(harness, changed_plan, shared_overrides),
            )


if __name__ == "__main__":
    unittest.main()
