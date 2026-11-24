import importlib.util
import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "blueprint" / "native_morph_driver.py"
SPEC = importlib.util.spec_from_file_location("native_morph_driver", MODULE_PATH)
DRIVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DRIVER)


class NativeMorphDriverTests(unittest.TestCase):
    def test_shape_key_names_map_to_native_deform_indices(self):
        self.assertEqual(
            DRIVER.shape_key_runtime_slot_by_index(
                ["Basis", "Deform 0", "Deform 12", "custom"]
            ),
            {0: 1, 12: 2},
        )

    def test_parse_keys_deduplicates_and_rejects_invalid_ranges(self):
        info = {
            "native_morph_keys": [
                {
                    "shader_hash": "aabbccddeeff0011",
                    "source_hash": "12345678",
                    "constant_buffer_hash": "90abcdef",
                    "offset": 10,
                    "count": 20,
                    "shape_key_index": 3,
                },
                {
                    "shader_hash": "AABBCCDDEEFF0011",
                    "source_hash": "12345678",
                    "constant_buffer_hash": "90ABCDEF",
                    "offset": 10,
                    "count": 20,
                    "shape_key_index": 3,
                },
                {
                    "shader_hash": "not-a-hash",
                    "source_hash": "12345678",
                    "constant_buffer_hash": "90abcdef",
                    "offset": 0,
                    "count": 1,
                    "shape_key_index": 0,
                },
            ]
        }

        keys = DRIVER.parse_native_morph_keys(info)

        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0].shader_hash, "aabbccddeeff0011")
        self.assertEqual(keys[0].shape_key_index, 3)

    def test_build_plans_maps_cb0_ranges_to_runtime_shapekey_slots(self):
        keys_by_hash = {
            "draw-a": [
                DRIVER.NativeMorphKey(
                    shader_hash="1111111111111111",
                    source_hash="aaaaaaaa",
                    constant_buffer_hash="bbbbbbbb",
                    offset=100,
                    count=10,
                    shape_key_index=0,
                ),
                DRIVER.NativeMorphKey(
                    shader_hash="1111111111111111",
                    source_hash="aaaaaaaa",
                    constant_buffer_hash="cccccccc",
                    offset=110,
                    count=11,
                    shape_key_index=1,
                ),
                DRIVER.NativeMorphKey(
                    shader_hash="2222222222222222",
                    source_hash="dddddddd",
                    constant_buffer_hash="eeeeeeee",
                    offset=0,
                    count=7,
                    shape_key_index=2,
                ),
            ]
        }
        runtime_slot_by_native_index = {"draw-a": {0: 4, 1: 2, 2: 7}}

        plans = DRIVER.build_native_morph_plans(
            keys_by_hash,
            runtime_slot_by_native_index,
            reserved_filter_indices={},
        )

        plan = plans["draw-a"]
        self.assertEqual(plan.shader_hashes, (
            "1111111111111111",
            "2222222222222222",
        ))
        self.assertEqual(plan.lookup_count, 3)
        self.assertEqual(set(plan.source_filters), {"aaaaaaaa", "dddddddd"})
        self.assertEqual(len(set(plan.source_filters.values())), 2)
        self.assertEqual(len(plan.lookup_buffer), 3 * 16)

        rows = [
            struct.unpack_from("<IIII", plan.lookup_buffer, index * 16)
            for index in range(plan.lookup_count)
        ]
        self.assertEqual(
            {(source_filter, offset, count, slot) for source_filter, offset, count, slot in rows},
            {
                (plan.source_filters["aaaaaaaa"], 100, 10, 4),
                (plan.source_filters["aaaaaaaa"], 110, 11, 2),
                (plan.source_filters["dddddddd"], 0, 7, 7),
            },
        )

    def test_ini_sections_capture_native_cb0_without_shader_specific_constants(self):
        plan = DRIVER.NativeMorphPlan(
            logical_hash="draw-a",
            entries=(
                DRIVER.NativeMorphLookupEntry(
                    source_filter=120001,
                    source_hash="aaaaaaaa",
                    shader_hash="1111111111111111",
                    offset=100,
                    count=10,
                    shape_key_slot=4,
                ),
            ),
            source_filters={"aaaaaaaa": 120001},
            shader_hashes=("1111111111111111",),
            lookup_count=1,
            lookup_buffer=struct.pack("<IIII", 120001, 100, 10, 4),
        )
        sections = DRIVER.render_native_morph_ini_sections(
            plans_by_hash={"draw-a": plan},
            lookup_resource_by_hash={
                "draw-a": "ResourceDrawPosition_NativeMorphLookup"
            },
            weight_resource_by_hash={
                "draw-a": "ResourceDrawPosition_ShapeKeyWeight"
            },
            lookup_filename_by_hash={
                "draw-a": "Meshes0000/zzmi_native_morph_draw_a.buf"
            },
            capture_shader_rel="./res/zzmi_native_morph_capture.hlsl",
            clear_shader_rel="./res/zzmi_native_morph_clear.hlsl",
        )

        self.assertIn(
            "[TextureOverrideZZMINativeMorphSource_aaaaaaaa]", sections
        )
        self.assertIn("[ShaderOverrideZZMINativeMorphCapture_1111111111111111]", sections)
        command_text = "\n".join(
            sections[
                "[CommandListZZMINativeMorphCaptureGeneric]"
            ]
        )
        self.assertIn("x95 = cs-t0", command_text)
        self.assertIn("if $zzmi_morph_source_filter == 120001", command_text)
        self.assertIn(
            "run = CustomShaderZZMINativeMorphCapture_draw_a", command_text
        )
        capture_text = "\n".join(
            sections["[CustomShaderZZMINativeMorphCapture_draw_a]"]
        )
        self.assertIn(
            "cs-t0 = copy ResourceDrawPosition_NativeMorphLookup",
            capture_text,
        )
        self.assertIn(
            "cs-u0 = ResourceDrawPosition_ShapeKeyWeight",
            capture_text,
        )
        clear_text = "\n".join(
            sections["[CustomShaderZZMINativeMorphClear_draw_a]"]
        )
        self.assertIn(
            "cs-t0 = copy ResourceDrawPosition_NativeMorphLookup",
            clear_text,
        )
        self.assertIn(
            "cs-u0 = ResourceDrawPosition_ShapeKeyWeight",
            clear_text,
        )
        self.assertIn(
            "post cs-t0 = ref ResourceZZMINativeMorphSavedCST0_draw_a",
            clear_text,
        )
        self.assertNotIn("743108cc03f39cbf", repr(sections))

    def test_reserved_filter_indices_and_generic_resource_name(self):
        sections = {
            "[TextureOverride_A]": [
                "hash = 11111111",
                "filter_index = 123456",
            ],
            "[Other]": ["filter_index=654321"],
            "[TextureOverrideZZMINativeMorphSource_aaaaaaaa]": [
                "hash = aaaaaaaa",
                "filter_index = 300001",
            ],
        }
        self.assertEqual(
            DRIVER.collect_reserved_filter_indices(sections),
            {123456, 654321},
        )
        self.assertEqual(
            DRIVER.derive_native_morph_lookup_resource_name(
                "ResourceABCDEF12Position"
            ),
            "ResourceABCDEF12Position_NativeMorphLookup",
        )
        self.assertEqual(
            DRIVER.derive_native_morph_lookup_resource_name(
                "ResourceABCDEF12Position",
                "head-a",
            ),
            "ResourceABCDEF12Position_NativeMorphLookup_head_a",
        )

    def test_same_source_buffer_shares_one_filter_across_submeshes(self):
        key = DRIVER.NativeMorphKey(
            shader_hash="1111111111111111",
            source_hash="aaaaaaaa",
            constant_buffer_hash="bbbbbbbb",
            offset=10,
            count=5,
            shape_key_index=0,
        )

        plans = DRIVER.build_native_morph_plans(
            {"head-a": [key], "head-b": [key]},
            {"head-a": {0: 0}, "head-b": {0: 2}},
        )

        self.assertEqual(
            plans["head-a"].source_filters["aaaaaaaa"],
            plans["head-b"].source_filters["aaaaaaaa"],
        )
        sections = DRIVER.render_native_morph_ini_sections(
            plans_by_hash=plans,
            lookup_resource_by_hash={
                "head-a": "ResourceHeadA_NativeMorphLookup",
                "head-b": "ResourceHeadB_NativeMorphLookup",
            },
            weight_resource_by_hash={
                "head-a": "ResourceHeadA_ShapeKeyWeight",
                "head-b": "ResourceHeadB_ShapeKeyWeight",
            },
            lookup_filename_by_hash={
                "head-a": "Meshes0000/head_a.buf",
                "head-b": "Meshes0000/head_b.buf",
            },
            capture_shader_rel="./res/zzmi_native_morph_capture.hlsl",
            clear_shader_rel="./res/zzmi_native_morph_clear.hlsl",
        )
        self.assertEqual(
            sum(
                name.startswith("[TextureOverrideZZMINativeMorphSource_")
                for name in sections
            ),
            1,
        )

    def test_generic_hlsl_bridges_native_cb0_to_existing_weight_buffer(self):
        capture = (
            ROOT / "Toolset" / "zzmi_native_morph_capture.hlsl"
        ).read_text(encoding="utf-8")
        clear = (
            ROOT / "Toolset" / "zzmi_native_morph_clear.hlsl"
        ).read_text(encoding="utf-8")
        guarded_sync = (
            ROOT / "Toolset" / "shapekey_weight_sync_guarded.hlsl"
        ).read_text(encoding="utf-8")

        self.assertIn("cbuffer ZZMINativeMorphCB0 : register(b0)", capture)
        self.assertIn("uint SourceOffset;", capture)
        self.assertIn("uint RecordCount;", capture)
        self.assertIn("float NativeWeight;", capture)
        self.assertIn("NativeMorphLookup : register(t0)", capture)
        self.assertIn("ShapeKeyWeight : register(u0)", capture)
        self.assertIn("ShapeKeyWeight[entry.w]", capture)
        self.assertIn("ShapeKeyWeight[NativeMorphLookup[i].w] = 0.0", clear)
        self.assertIn("IsNativeMorphSlot(i)", guarded_sync)
        self.assertNotIn(
            "743108cc03f39cbf",
            capture + clear + guarded_sync,
        )


if __name__ == "__main__":
    unittest.main()
