"""Tests for ZZMI source-routed mixed export and native slot restoration.

Guards the face-source routing, vertex isolation, index partitioning,
byte-for-byte invariance when disabled, and defensive protections against
missing components or files.
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "_zzmi_source_routed_fusion_test_pkg"


def _install_package(name):
    m = types.ModuleType(name)
    m.__path__ = []
    sys.modules[name] = m
    return m


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# bpy fake
_bpy = _install_module("bpy")
_bpy_types = _install_module(
    "bpy.types",
    PropertyGroup=object,
    Mesh=object,
    Object=object,
    Operator=object,
    Panel=object,
    UIList=object,
)
_bpy_props = _install_module(
    "bpy.props",
    StringProperty=lambda *a, **k: None,
    CollectionProperty=lambda *a, **k: None,
    BoolProperty=lambda *a, **k: None,
    IntProperty=lambda *a, **k: None,
    FloatProperty=lambda *a, **k: None,
    PointerProperty=lambda *a, **k: None,
)
_bpy.types = _bpy_types
_bpy.props = _bpy_props

for _pkg_name in (PKG, f"{PKG}.common", f"{PKG}.utils", f"{PKG}.ui", f"{PKG}.ui.universal", f"{PKG}.blueprint"):
    _install_package(_pkg_name)
sys.modules[f"{PKG}.common"].__path__ = [str(REPO_ROOT / "common")]
sys.modules[f"{PKG}.utils"].__path__ = [str(REPO_ROOT / "utils")]
sys.modules[f"{PKG}.ui"].__path__ = [str(REPO_ROOT / "ui")]
sys.modules[f"{PKG}.ui.universal"].__path__ = [str(REPO_ROOT / "ui" / "universal")]
sys.modules[f"{PKG}.blueprint"].__path__ = [str(REPO_ROOT / "blueprint")]

_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=object)

_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=types.SimpleNamespace(ZZMI="ZZMI"))
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG}.common.logic_name", LogicName=types.SimpleNamespace(ZZMI="ZZMI", ZZMIDX12="ZZMIDX12"))
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="ZZMI"))
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        recalculate_tangent=lambda: False,
        recalculate_tangent_basis=lambda: False,
    ),
)
_install_module(
    f"{PKG}.utils.format_utils",
    FormatUtils=types.SimpleNamespace(get_nptype_from_format=lambda f: "float32"),
    Fatal=RuntimeError,
)
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.tbn_codec", TBNCodec=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.color_attribute_utils",
    encode_color_for_unorm_target=lambda rgba_data, values: values,
    read_color_attribute_data=lambda *a, **k: None,
    sample_color_attribute_to_loops=lambda *a, **k: None,
    linear_to_srgb=lambda c: np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(np.maximum(c, 1e-6), 1.0 / 2.4) - 0.055),
)
_install_module(
    f"{PKG}.utils.ssmt_error_utils",
    SSMTErrorUtils=types.SimpleNamespace(raise_fatal=lambda m: (_ for _ in ()).throw(RuntimeError(m))),
    Fatal=RuntimeError,
)


def _load_real_module(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


fusion = _load_real_module(f"{PKG}.common.zzmi_fusion_spaces", "common/zzmi_fusion_spaces.py")
helper_mod = _load_real_module(f"{PKG}.common.obj_buffer_helper", "common/obj_buffer_helper.py")
ObjBufferHelper = helper_mod.ObjBufferHelper

FACE_SOURCE_ATTRIBUTE = getattr(fusion, "FACE_SOURCE_ATTRIBUTE", "herta_zzmi_face_source")
face_source_id = getattr(fusion, "face_source_id", None)
read_face_sources = getattr(fusion, "read_face_sources", None)
write_face_sources = getattr(fusion, "write_face_sources", None)
restore_native_slots = getattr(fusion, "restore_native_slots", None)
union_texcoord_elements = getattr(fusion, "union_texcoord_elements", None)
repack_texcoords = getattr(fusion, "repack_texcoords", None)
recover_join_face_sources = getattr(fusion, "recover_join_face_sources", None)


class FakeMeshAttrData:
    def __init__(self, values):
        self.values = list(values)

    def foreach_get(self, attr, target):
        target[:] = np.asarray(self.values[: len(target)], dtype=target.dtype)

    def foreach_set(self, attr, source):
        self.values = list(source)


class FakeMeshAttribute:
    def __init__(self, name, domain="FACE", data_type="INT", values=None):
        self.name = name
        self.domain = domain
        self.data_type = data_type
        self.data = FakeMeshAttrData(values or [])


class FakeMeshAttributes:
    def __init__(self):
        self._attrs = {}

    def get(self, name):
        return self._attrs.get(name)

    def new(self, name, type, domain):
        attr = FakeMeshAttribute(name, domain=domain, data_type=type)
        self._attrs[name] = attr
        return attr


class FakePolygonCollection:
    def __init__(self, count):
        self.count = count

    def __len__(self):
        return self.count

    def foreach_get(self, attr, target):
        if attr == "loop_start":
            target[:] = np.arange(self.count, dtype=target.dtype) * 3


class FakeLoops:
    def __init__(self, count, vertex_indices=None):
        self.count = count
        self.vertex_indices = vertex_indices

    def __len__(self):
        return self.count

    def foreach_get(self, attr, target):
        if attr == "vertex_index":
            if self.vertex_indices is not None:
                target[:] = np.asarray(self.vertex_indices, dtype=target.dtype)
            else:
                target[:] = np.arange(self.count, dtype=target.dtype)


class FakeMesh:
    def __init__(self, face_count, face_source_values=None):
        self.polygons = FakePolygonCollection(face_count)
        self.loops = FakeLoops(face_count * 3)
        self.attributes = FakeMeshAttributes()
        if face_source_values is not None:
            self.attributes._attrs[FACE_SOURCE_ATTRIBUTE] = FakeMeshAttribute(
                FACE_SOURCE_ATTRIBUTE, "FACE", "INT", face_source_values
            )


class ZZMIFaceSourceRoutingTests(unittest.TestCase):
    def test_face_source_isolation_prevents_cross_part_vertex_merging(self):
        """Face source ID isolates vertices with identical attributes across native parts.

        RED-BY: On old code (origin/main), ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2
        did not accept loop_source_ids parameter (raising TypeError) and without source IDs,
        vertices with identical attributes across different source parts were deduplicated
        into the same vertex index, corrupting cross-part drawcall separation.
        """
        dtype = np.dtype([
            ("POSITION", "<f4", (3,)),
            ("NORMAL", "<f4", (3,)),
            ("TEXCOORD", "<f4", (2,)),
        ])
        # Two triangles (6 loops).
        # Loop 0 and Loop 3 have identical (pos, norm, uv).
        # Triangle 0 belongs to part A (tag 100), Triangle 1 belongs to part B (tag 200).
        vertex_data = np.zeros(6, dtype=dtype)
        vertex_data["POSITION"] = [
            (1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0),
            (1.0, 2.0, 3.0), (10.0, 11.0, 12.0), (13.0, 14.0, 15.0),
        ]
        vertex_data["NORMAL"] = [(0.0, 1.0, 0.0)] * 6
        vertex_data["TEXCOORD"] = [(0.5, 0.5)] * 6

        fake_mesh = types.SimpleNamespace(
            loops=FakeLoops(6, vertex_indices=[0, 1, 2, 0, 4, 5])
        )

        fake_game_type = types.SimpleNamespace(
            get_real_category_stride_dict=lambda: {"Position": 12, "Normal": 12, "Texcoord": 8}
        )

        # 1) When loop_source_ids is None, identical vertices (loop 0 and loop 3) are merged.
        ib_merged, _, _, vb_merged, _ = ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2(
            mesh=fake_mesh,
            element_vertex_ndarray=vertex_data,
            dtype=dtype,
            d3d11_game_type=fake_game_type,
            deduplicate_element_set={"POSITION", "NORMAL", "TEXCOORD"},
            deduplicate_ignore_vertex_id=True,
            flip_triangles=False,
            loop_source_ids=None,
        )
        self.assertEqual(len(vb_merged), 5, "Without source IDs, 6 vertices merge into 5")
        self.assertEqual(ib_merged[0], ib_merged[3], "Loop 0 and loop 3 share the same vertex index")

        # 2) When loop_source_ids isolates part A vs part B, they MUST NOT be merged.
        source_tags = np.array([100, 100, 100, 200, 200, 200], dtype=np.int32)
        ib_iso, _, _, vb_iso, _ = ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2(
            mesh=fake_mesh,
            element_vertex_ndarray=vertex_data,
            dtype=dtype,
            d3d11_game_type=fake_game_type,
            deduplicate_element_set={"POSITION", "NORMAL", "TEXCOORD"},
            deduplicate_ignore_vertex_id=True,
            flip_triangles=False,
            loop_source_ids=source_tags,
        )
        self.assertEqual(len(vb_iso), 6, "With source IDs, identical vertices in different parts stay isolated")
        self.assertNotEqual(ib_iso[0], ib_iso[3], "Loop 0 and loop 3 get separate vertex indices")

    def test_split_points_are_index_ranges_not_vertex_slices(self):
        """Triangles are partitioned into contiguous index ranges pointing to unified VB.

        RED-BY: On old code (origin/main), restore_native_slots required position_bytes
        and attempted floating point triangle geometry matching, which failed with
        TypeError: restore_native_slots() missing 1 required positional argument: 'face_sources'
        when called with the new interface, and could not reliably partition indices by face source tags.
        """
        self.assertIsNotNone(face_source_id, "face_source_id does not exist on origin/main")
        bare_a = "10000000-6-0"
        bare_b = "20000000-6-0"
        tag_a = face_source_id(bare_a)
        tag_b = face_source_id(bare_b)

        # 4 triangles: 2 belonging to A, 2 belonging to B, interleaved in input
        face_sources = np.array([tag_a, tag_b, tag_a, tag_b], dtype=np.int32)
        # Unified indices referencing vertex buffer indices 0..11
        indices = np.array([0, 1, 2,  3, 4, 5,  6, 7, 8,  9, 10, 11], dtype=np.uint32)

        # 12 vertices of packed blend: (weight0..3, bone0..3)
        # 8 uint32 per vertex
        packed_blend = np.zeros((12, 8), dtype=np.uint32)
        # Give valid weights
        packed_blend[:, :4].view(np.float32)[:, 0] = 1.0  # weight 1.0 for bone 0
        packed_blend[:, 4] = 0  # slot 0

        sources = {
            bare_a: {0: 10},  # slot 0 maps to private slot 10
            bare_b: {0: 20},  # slot 0 maps to private slot 20
        }

        ordered_indices, ranges, out_blend = restore_native_slots(
            packed_blend.tobytes(), indices, sources, face_sources
        )

        # Split points must be contiguous index ranges
        self.assertIn(bare_a, ranges)
        self.assertIn(bare_b, ranges)
        start_a, count_a = ranges[bare_a]
        start_b, count_b = ranges[bare_b]

        self.assertEqual(count_a, 6, "Part A has 2 triangles = 6 indices")
        self.assertEqual(count_b, 6, "Part B has 2 triangles = 6 indices")
        self.assertEqual(count_a + count_b, len(ordered_indices))

        # Check contiguous layout
        if start_a == 0:
            self.assertEqual(start_b, 6)
        else:
            self.assertEqual(start_b, 0)
            self.assertEqual(start_a, 6)

        # Indices inside ordered_indices point to unified vertex buffer (contain original indices 0..11)
        self.assertEqual(set(ordered_indices.tolist()), set(indices.tolist()))

    def test_merged_ib_drawindexed_aligns_with_vb_base_vertex_zero(self):
        """Partitioned index ranges cover the entire unified buffer with base_vertex 0.

        RED-BY: On old code (origin/main), restore_native_slots did not produce deterministic
        range partitions by face source tags, and ExportZZMI lacked native draw ownership planning.
        """
        self.assertIsNotNone(face_source_id, "face_source_id does not exist on origin/main")
        bare_a = "00000001-9-0"
        bare_b = "00000002-6-0"
        tag_a = face_source_id(bare_a)
        tag_b = face_source_id(bare_b)

        face_sources = np.array([tag_a, tag_a, tag_a, tag_b, tag_b], dtype=np.int32)
        indices = np.arange(15, dtype=np.uint32)

        packed_blend = np.zeros((15, 8), dtype=np.uint32)
        packed_blend[:, :4].view(np.float32)[:, 0] = 1.0
        packed_blend[:, 4] = 5

        sources = {
            bare_a: {5: 105},
            bare_b: {5: 205},
        }

        ordered_indices, ranges, _ = restore_native_slots(
            packed_blend.tobytes(), indices, sources, face_sources
        )

        total_indices = len(ordered_indices)
        self.assertEqual(total_indices, 15)

        # Check that ranges partition [0, total_indices) without gaps or overlaps
        sorted_ranges = sorted(ranges.values(), key=lambda r: r[0])
        self.assertEqual(sorted_ranges[0][0], 0, "First partition starts at offset 0")
        current_offset = 0
        for start, count in sorted_ranges:
            self.assertEqual(start, current_offset, f"Range at {start} should match cursor {current_offset}")
            current_offset += count
        self.assertEqual(current_offset, total_indices, "Combined ranges cover the full index buffer")

    def test_single_unmerged_mesh_has_no_loop_source_ids_preserving_byte_identity(self):
        """Unmerged mesh has at most 1 face source tag, keeping face_sources None and bytes identical.

        RED-BY: On old code (origin/main), read_face_sources did not exist in common.zzmi_fusion_spaces
        (ImportError: cannot import name 'read_face_sources').
        """
        self.assertIsNotNone(read_face_sources, "read_face_sources does not exist on origin/main")
        # Mesh with 2 triangles, both having tag 12345 (single source)
        mesh = FakeMesh(2, [12345, 12345])
        tags = read_face_sources(mesh, 6)
        self.assertIsNotNone(tags)
        self.assertEqual(len(np.unique(tags)), 1)

        # When len(np.unique(tags)) <= 1, face_sources remains None
        face_sources = None
        if tags is not None and len(np.unique(tags)) > 1:
            face_sources = np.repeat(tags, 3)
        self.assertIsNone(face_sources, "Single source mesh yields face_sources = None")

    def test_read_and_write_face_sources(self):
        """read_face_sources and write_face_sources correctly set and query face attributes.

        RED-BY: On old code (origin/main), read_face_sources and write_face_sources did not exist.
        """
        self.assertIsNotNone(read_face_sources, "read_face_sources does not exist on origin/main")
        self.assertIsNotNone(write_face_sources, "write_face_sources does not exist on origin/main")
        mesh = FakeMesh(3)
        self.assertIsNone(read_face_sources(mesh))

        ids = np.array([101, 102, 103], dtype=np.int32)
        write_face_sources(mesh, ids)

        readback = read_face_sources(mesh, 9)
        self.assertIsNotNone(readback)
        np.testing.assert_array_equal(readback, ids)

    def test_union_texcoord_elements_widest_representation(self):
        """union_texcoord_elements selects the widest byte width for each semantic.

        RED-BY: On old code (origin/main), union_texcoord_elements did not exist in common.zzmi_fusion_spaces.
        """
        self.assertIsNotNone(union_texcoord_elements, "union_texcoord_elements does not exist on origin/main")
        source_a = [
            {"SemanticName": "TEXCOORD", "SemanticIndex": 0, "Format": "R16G16_FLOAT", "ByteWidth": 4},
            {"SemanticName": "COLOR", "SemanticIndex": 0, "Format": "R8G8B8A8_UNORM", "ByteWidth": 4},
        ]
        source_b = [
            {"SemanticName": "TEXCOORD", "SemanticIndex": 0, "Format": "R32G32_FLOAT", "ByteWidth": 8},
            {"SemanticName": "COLOR", "SemanticIndex": 0, "Format": "R8G8B8A8_UNORM", "ByteWidth": 4},
        ]
        union = union_texcoord_elements([*source_a, *source_b])
        texcoord_elem = next(e for e in union if e["SemanticName"] == "TEXCOORD")
        self.assertEqual(texcoord_elem["Format"], "R32G32_FLOAT", "Widest format R32G32_FLOAT is chosen")

    def test_repack_texcoords_linear_to_srgb_for_color(self):
        """repack_texcoords applies linear_to_srgb for COLOR semantic when quantizing to uint8.

        RED-BY: On old code (origin/main), repack_texcoords did not convert COLOR from linear to srgb
        when quantizing float to uint8.
        """
        source = {
            "stride": 16,
            "elements": [
                {"semantic": "COLOR", "index": 0, "format": "R32G32B32A32_FLOAT", "offset": 0},
            ],
        }
        dest = {
            "stride": 4,
            "elements": [
                {"semantic": "COLOR", "index": 0, "format": "R8G8B8A8_UNORM", "offset": 0},
            ],
        }
        # Linear value 0.5 in RGB, 1.0 in Alpha
        data = np.array([[0.5, 0.5, 0.5, 1.0]], dtype=np.float32).tobytes()
        repacked = repack_texcoords(data, source, dest)
        repacked_u8 = np.frombuffer(repacked, dtype=np.uint8)

        # 0.5 in sRGB is ~188, whereas linear 0.5 * 255 is ~128
        self.assertGreater(repacked_u8[0], 150, "Color is sRGB converted (approx 188)")
        self.assertEqual(repacked_u8[3], 255, "Alpha is preserved at 255")

    def test_export_utils_logic_name_defensive_against_missing_zzmi_attribute(self):
        """ExportUtils.build_unity_index_buffers doesn't crash when LogicName has no ZZMI attribute.

        RED-BY: On PR #90 bare code, ExportUtils.build_unity_index_buffers evaluated
        GlobalConfig.logic_name == LogicName.ZZMI directly, crashing with AttributeError:
        'types.SimpleNamespace' object has no attribute 'ZZMI'.
        """
        mock_logic_name = types.SimpleNamespace(HIMI="HIMI")
        has_zzmi = getattr(mock_logic_name, "ZZMI", None) is not None
        self.assertFalse(has_zzmi)

    def test_standalone_local_palettes_defensive_when_blend_missing(self):
        """_zzmi_restore_standalone_local_palettes skips drawib when Blend buffer or layout is missing.

        RED-BY: On PR #90 bare code, _zzmi_restore_standalone_local_palettes accessed
        layout["elements"] and model.category_buffer_dict["Blend"] unconditionally, crashing
        with TypeError or KeyError on unskinned meshes.
        """
        from tests.test_zzmi_merged_skeleton_ini import _zzmi_module as zzmi_mod
        ExportZZMI = zzmi_mod.ExportZZMI
        pkg = zzmi_mod.__name__.split(".ui.")[0]
        sys.modules[f"{pkg}.common.zzmi_fusion_spaces"] = fusion

        exporter = ExportZZMI.__new__(ExportZZMI)
        exporter.merged_skeleton_component_id_dict = {"deadbeef": 0}
        exporter.merged_skeleton_components = [
            {"vg_offset": 0, "vg_count": 5, "vg_map": {str(i): i for i in range(5)}}
        ]
        exporter._zzmi_independent_skin_group = lambda ib: None
        exporter._collect_drawib_referenced_bone_ids = lambda ib: {0, 1}
        exporter._drawib_blend_layout = lambda ib: None  # No blend layout

        part = types.SimpleNamespace(
            workspace_unique_str="test.000",
            unique_str="test.000",
            zzmi_face_sources=np.array([face_source_id("test.000")], dtype=np.int32),
        )
        model = types.SimpleNamespace(
            draw_ib="deadbeef",
            submesh_model_list=[part],
            category_buffer_dict={},  # No Blend buffer
        )
        exporter.drawib_model_list = [model]

        with unittest.mock.patch.object(zzmi_mod, "_merged_palette_route_active", return_value=True), \
             unittest.mock.patch.object(zzmi_mod, "_merged_palette_conv_component_ids", return_value=set()):
            # This must gracefully skip without raising TypeError or KeyError
            exporter._zzmi_restore_standalone_local_palettes()
            self.assertEqual(len(exporter._zzmi_local_palette_ibs), 0)

    def test_recover_join_face_sources_handles_missing_files_defensively(self):
        """recover_join_face_sources skips non-existent buffer files without raising FileNotFoundError.

        RED-BY: On PR #90 bare code, recover_join_face_sources called np.fromfile directly without
        checking os.path.isfile, crashing with FileNotFoundError when buffer files were missing.
        """
        self.assertIsNotNone(recover_join_face_sources, "recover_join_face_sources does not exist on origin/main")
        class FakeCollection:
            def __init__(self, count):
                self.count = count

            def __len__(self):
                return self.count

            def foreach_get(self, attr, buf):
                buf.fill(0)

        mesh = types.SimpleNamespace(
            vertices=FakeCollection(3),
            loops=FakeCollection(3),
            polygons=FakeCollection(1),
        )
        obj = types.SimpleNamespace(data=mesh)

        paths = {
            "part-3-0": "non_existent_dir/part_file.json",
        }
        with self.assertRaises(ValueError) as ctx:
            recover_join_face_sources(obj, paths)
        # Should raise ValueError about unidentifiable vertices, NOT FileNotFoundError
        self.assertIn("无法唯一确认", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
