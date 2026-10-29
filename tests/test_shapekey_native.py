import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest import mock

import numpy as np


spec = importlib.util.spec_from_file_location("shapekey_native_test", Path(__file__).resolve().parents[1] / "common/shapekey_native.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class ShapeKeyNativeTests(unittest.TestCase):
    def test_packed_deltas_match_numpy_mask_order_channels_and_nonfinite_values(self):
        lib = native.native_library()
        if lib is None or not hasattr(lib, "herta_pack_deltas"):
            self.skipTest("optional native delta packer unavailable")
        rng = np.random.default_rng(981)
        basis = rng.normal(size=(40000, 10)).astype(np.float32)
        target = basis.copy()
        target[:, 0] += rng.normal(0, 1e-5, len(target)).astype(np.float32)
        target[::17, 2] *= -1
        target[::23, 6] = np.nan
        basis[0, :3] = target[0, :3] = np.inf
        basis[1, :3], target[1, :3] = np.inf, -np.inf
        basis[2, 0], target[2, 0] = 0.0, -0.0
        # Preserve subtraction operand order even for distinct NaN payloads.
        basis[3:7, 0] = np.array([0x7fc00001, 0xffc00002, 0x7f800001, 0x7fc00003], dtype=np.uint32).view(np.float32)
        target[3:7, 0] = np.array([0x7fc00002, 0x7fc00001, 0x7fc00101, 0xffc00004], dtype=np.uint32).view(np.float32)
        for columns in ([0, 1, 2], [6, 7, 8], list(range(10))):
            with np.errstate(invalid="ignore"):
                delta = target[:, columns] - basis[:, columns]
                delta[delta == 0] = 0.0
                mask = ~np.isclose(basis[:, columns], target[:, columns], atol=1e-6).all(axis=1)
            expected_map = np.full(len(basis), -1, dtype=np.int32)
            expected_map[mask] = np.arange(131, 131 + mask.sum(), dtype=np.int32)
            for threads in (1, 4, 8):
                packed, index_map = native.packed_slot_deltas(basis, target, columns,
                                                            global_start=131, threads=threads)
                self.assertEqual(packed.tobytes(), delta[mask].tobytes())
                self.assertEqual(index_map.tobytes(), expected_map.tobytes())

    def test_half_packer_matches_numpy_for_all_half_patterns_and_random_float_bits(self):
        lib = native.native_library()
        if lib is None or not hasattr(lib, "herta_pack_half_float3"):
            self.skipTest("optional native packer unavailable")
        rng = np.random.default_rng(555)
        with np.errstate(over="ignore", invalid="ignore"):
            known = np.arange(65536, dtype=np.uint16).view(np.float16).astype(np.float32)
            random = rng.integers(0, 2**32, size=300003, dtype=np.uint32).view(np.float32)
            values = np.concatenate((known, random))
            values = np.pad(values, (0, (-len(values)) % 3)).reshape(-1, 3)
            bits = values.astype(np.float16).reshape(-1).view(np.uint16)
            expected = np.pad(bits, (0, (-len(bits)) % 6)).view(np.uint32)
        for threads in (1, 4, 8):
            self.assertEqual(native.pack_half_float3(values, threads=threads).tobytes(), expected.tobytes())

    def test_frame_groups_share_pair_alignment_even_at_odd_boundaries(self):
        lib = native.native_library()
        if lib is None or not hasattr(lib, "herta_pack_half_float3"):
            self.skipTest("optional native packer unavailable")
        rng = np.random.default_rng(41)
        for sizes in ((1,), (1, 1), (3, 0, 5, 2), (0, 0), (7, 3, 1), (2, 4, 3)):
            parts = [rng.normal(size=(count, 3)).astype(np.float32) for count in sizes]
            values = np.concatenate(parts)
            self.assertEqual(native.pack_half_float3_parts(parts).tobytes(), native.pack_half_float3(values).tobytes())

    def test_native_and_numpy_match_float32_reference_for_relative_keys(self):
        rng = np.random.default_rng(231)
        basis = rng.normal(size=(5000, 3)).astype(np.float32)
        targets = [rng.normal(size=basis.shape).astype(np.float32) for _ in range(32)]
        relatives = [basis if index % 2 else targets[(index + 1) % 32] for index in range(32)]
        expected = np.stack([basis + (target - relative) for target, relative in zip(targets, relatives)])
        for disabled in ("0", "1"):
            with mock.patch.dict(os.environ, {"HERTA_SHAPEKEY_NATIVE": disabled}):
                for threads in (1, 4, 8):
                    for mirror in (False, True):
                        reference = expected.copy()
                        if mirror:
                            reference[:, :, 0] *= -1.0
                        actual = native.relative_shapes(basis, targets, relatives, mirror=mirror, threads=threads)
                        self.assertEqual(actual.tobytes(), reference.tobytes())

    def test_invalid_layout_is_rejected(self):
        with self.assertRaises(ValueError):
            native.relative_shapes(np.zeros((2, 4)), [], [])
        with self.assertRaises(ValueError):
            native.relative_shapes(np.zeros((2, 3)), [np.zeros((3, 3))], [np.zeros((2, 3))])

    def test_missing_library_retains_numpy_path(self):
        basis = np.ones((3, 3), np.float32)
        target = basis * 2
        with mock.patch.object(native, "native_library", return_value=None):
            self.assertEqual(native.relative_shapes(basis, [target], [basis])[0].tobytes(), target.tobytes())

    def object(self):
        key = NS(mute=False, vertex_group="", data=[None] * 3)
        key.relative_key = key
        keys = NS(use_relative=True, reference_key=key, key_blocks=[key], animation_data=None)
        return NS(mode="OBJECT", show_only_shape_key=False, data=NS(shape_keys=keys, vertices=[None] * 3),
                  modifiers=[], animation_data=None)

    def test_complex_blender_evaluation_is_never_bypassed(self):
        changes = (
            lambda obj: setattr(obj.data.shape_keys, "use_relative", False),
            lambda obj: setattr(obj, "show_only_shape_key", True),
            lambda obj: setattr(obj, "mode", "EDIT"),
            lambda obj: obj.modifiers.append(NS(show_viewport=True)),
            lambda obj: setattr(obj.data.shape_keys, "animation_data", NS()),
            lambda obj: setattr(obj, "animation_data", NS()),
            lambda obj: setattr(obj.data.shape_keys.reference_key, "vertex_group", "mask"),
            lambda obj: setattr(obj.data.shape_keys.reference_key, "mute", True),
            lambda obj: setattr(obj.data.shape_keys.reference_key, "data", [None]),
        )
        for change in changes:
            obj = self.object()
            self.assertEqual(native.fast_path_reason(obj), "")
            change(obj)
            self.assertTrue(native.fast_path_reason(obj))

    def test_disabled_modifier_can_use_fast_path_and_environment_can_disable_it(self):
        obj = self.object()
        obj.modifiers.append(NS(show_viewport=False))
        self.assertEqual(native.fast_path_reason(obj), "")
        with mock.patch.dict(os.environ, {"HERTA_SHAPEKEY_FAST": "0"}):
            self.assertEqual(native.fast_path_reason(obj), "disabled")


if __name__ == "__main__":
    unittest.main()
