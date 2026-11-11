"""Surface garment topology, optional generation and render-routing regressions."""

import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path

import numpy as np

from tests.test_node_postprocess_draginteraction import _load_drag_module, _make_node


def cylinder(center, radius, rings=10, sides=12, height=1.0, cap_top=False):
    p = []
    n = []
    f = []
    for z in np.linspace(0, height, rings):
        for angle in np.arange(sides) * 2 * np.pi / sides:
            radial = np.array([np.cos(angle), np.sin(angle), 0])
            p.append(radial * radius + [center, 0, z])
            n.append(radial)
    for row in range(rings - 1):
        for col in range(sides):
            a = row * sides + col
            b = row * sides + (col + 1) % sides
            c = a + sides
            d = b + sides
            f.extend(((a, b, c), (b, d, c)))
    bottom = len(p)
    p.append([center, 0, 0])
    n.append([0, 0, -1])
    for col in range(sides):
        f.append((bottom, (col + 1) % sides, col))
    if cap_top:
        top = len(p)
        p.append([center, 0, height])
        n.append([0, 0, 1])
        start = (rings - 1) * sides
        for col in range(sides):
            f.append((top, start + col, start + (col + 1) % sides))
    return np.array(p, float), np.array(n, float), np.array(f, np.uint32)


def fixture(two=True, dense=False):
    pieces = []
    names = []
    cloth = []
    body = []
    offset = 0
    for index, center in enumerate((-0.12, 0.12) if two else (-0.12,)):
        p, n, f = cylinder(
            center, 0.055, rings=24 if dense else 10, sides=24 if dense else 12
        )
        pieces.append((p, n, f + offset))
        cloth.append(f + offset)
        names.extend(["Stocking" + str(index)] * len(f))
        offset += len(p)
    for center in (-0.12, 0.12):
        p, n, f = cylinder(center, 0.05, height=1.1, cap_top=True)
        pieces.append((p, n, f + offset))
        body.append(f + offset)
        names.extend(["Body"] * len(f))
        offset += len(p)
    return (
        np.concatenate([p[0] for p in pieces]),
        np.concatenate([p[1] for p in pieces]),
        np.concatenate([p[2] for p in pieces]),
        np.array(names, object),
        np.concatenate(cloth),
        np.concatenate(body),
    )


class GarmentGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()
        cls.geom = importlib.import_module("_ssmt_root.blueprint.drag_cloth_geometry")

    def test_disconnected_stockings_have_independent_maps_and_own_leg_hulls(self):
        p, n, _, _, cloth, body = fixture()
        data = self.geom.bake_garments(p, n, cloth, body, exterior=False)
        self.assertEqual(len(data), 2)
        masks = [d["map"] != 0xFFFFFFFF for d in data]
        self.assertFalse(np.any(masks[0] & masks[1]))
        self.assertEqual({int(d["config"][12, 3]) for d in data}, {1, 2})
        for d in data:
            self.assertLessEqual(len(d["representatives"]), 2450)
            np.testing.assert_allclose(
                d["display_weights"][d["map"] != 0xFFFFFFFF, :3].sum(1), 1
            )
            self.assertTrue(np.all(d["map"][np.unique(body)] == 0xFFFFFFFF))

    def test_connected_garment_emits_one_synchronized_runtime(self):
        p, n, f = cylinder(0, 0.18)
        b, bn, bf = cylinder(0, 0.15, height=1.1, cap_top=True)
        data = self.geom.bake_garments(
            np.r_[p, b], np.r_[n, bn], f, bf + len(p), exterior=False
        )
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["config"][0, 3], 1)

    def test_simplification_retains_render_mapping_and_coloring(self):
        p, n, _, _, cloth, body = fixture(two=False, dense=True)
        data = self.geom.bake_garments(p, n, cloth, body, budget=160, exterior=False)[0]
        self.assertLessEqual(len(data["representatives"]), 160)
        mapped = data["map"] != 0xFFFFFFFF
        np.testing.assert_allclose(
            data["display_weights"][mapped, :3].sum(1), 1, atol=1e-6
        )
        self.assertTrue(
            np.all(
                data["display_triangles"].reshape(-1, 3)[mapped]
                < len(data["representatives"])
            )
        )
        edges = data["edges"][:, :2].copy().view(np.uint32)
        for begin, end in zip(data["edge_offsets"][:-1], data["edge_offsets"][1:]):
            self.assertEqual(len(np.unique(edges[begin:end])), 2 * (end - begin))

    def test_body_surface_mask_does_not_discard_an_exterior_cylinder(self):
        p, n, _, _, cloth, body = fixture(two=False)
        data = self.geom.bake_garments(p, n, cloth, body)[0]
        self.assertGreater(np.mean(data["surface_mask"]), 0.98)

    def test_shared_body_and_cloth_rows_fail_instead_of_deforming_body(self):
        p, n, _, _, cloth, body = fixture()
        with self.assertRaisesRegex(ValueError, "相同顶点"):
            self.geom.bake_garments(p, n, cloth, np.r_[body, cloth[:1]], exterior=False)


class GarmentExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()
        cls.exporter = importlib.import_module("_ssmt_root.blueprint.drag_cloth")

    def prepare(
        self,
        folder,
        collision=False,
        dense=False,
        extra=False,
        budget=2450,
        bake=True,
        prefix=0,
    ):
        p, n, f, names, _, _ = fixture(dense=dense)
        v = np.zeros((len(p), 12 if extra else 10), np.float32)
        v[:, :3] = p
        v[:, 3:6] = n
        v[:, 6] = 1
        v[:, 9] = 1
        if extra:
            v[:, 10:] = np.arange(len(p) * 2, dtype=np.float32).reshape(-1, 2) / 7
        meshes = folder / "Meshes"
        meshes.mkdir(exist_ok=True)
        v.tofile(meshes / "abc123-Position.buf")
        blend = np.zeros((len(p), 8), np.float32)
        blend[:, 0] = 1
        blend.tofile(meshes / "abc123-Blend.buf")
        f.tofile(meshes / "Index.buf")
        lines = ["hash = abc123", "match_first_index = 0", "ib = ResourceIB"]
        offset = 0
        for name in dict.fromkeys(names):
            count = int(np.sum(names == name)) * 3
            lines += [
                f"; [mesh:{name}] [vertex_count:{len(p)}]",
                f"drawindexed = {count}, {offset}, 0",
            ]
            offset += count
        sections = OrderedDict(
            {
                "[Constants]": ["global $active = 0"],
                "[Present]": [],
                "[TextureOverride_abc123_abc123_VertexLimitRaise]": [
                    "hash = abc123",
                    f"override_vertex_count = {len(p)}",
                    "override_byte_stride = 40",
                ],
                "[TextureOverride_abc123_Draw]": lines,
                "[ResourceIB]": [
                    "type = Buffer",
                    "format = R32_UINT",
                    "filename = Meshes/Index.buf",
                ],
                "[Resource_abc123_Position]": [
                    "type = Buffer",
                    "stride = 40",
                    "filename = Meshes/abc123-Position.buf",
                ],
                "[TextureOverride_VB_abc123_Position]": [
                    "vb0 = Resource_abc123_Position",
                    "$zz_ms_occ_7 = $zz_ms_occ_7 + 1",
                    "handling = skip",
                    "if $zz_ms_occ_7 == 1",
                    "    vs-t0 = ResourceZZMergedSkeleton_G3_s1",
                    f"    draw = {len(p)}, 0",
                    "else",
                    "    vs-t0 = ResourceZZMergedSkeleton_G3_s2",
                    f"    draw = {len(p)}, 0",
                    "endif",
                ],
            }
        )
        for slot in (1, 2):
            sections[f"[ResourceZZMergedSkeleton_G3_s{slot}]"] = [
                "type = RWBuffer",
                "format = R32G32B32A32_FLOAT",
                "array = 3",
            ]
            sections[f"[ResourceZZRedirectSO_G3_s{slot}]"] = [
                "type = Buffer",
                "stride = 40",
            ]
        node = _make_node(
            self.mod,
            cloth_slide_enabled=True,
            collision_enabled=collision,
            cloth_proxy_budget=budget,
        )
        if extra:
            definition = self.mod.DEFAULT_VERTEX_STRUCT.replace(
                "};", "    float2 extra;\n};"
            )
            node._get_vertex_struct_definition = lambda: definition
            sections["[Resource_abc123_Position]"][1] = "stride = 48"
            vlr = sections["[TextureOverride_abc123_abc123_VertexLimitRaise]"]
            vlr[:] = [
                line.replace("override_byte_stride = 40", "override_byte_stride = 48")
                for line in vlr
            ]
            for slot in (1, 2):
                sections[f"[ResourceZZRedirectSO_G3_s{slot}]"][1] = "stride = 48"
        if prefix:
            vlr = sections["[TextureOverride_abc123_abc123_VertexLimitRaise]"]
            vlr[:] = [
                (
                    f"override_vertex_count = {len(p)+prefix}"
                    if line.startswith("override_vertex_count")
                    else line
                )
                for line in vlr
            ]
            lines[:] = [
                (
                    line.rsplit(", ", 1)[0] + f", {prefix}"
                    if line.startswith("drawindexed =")
                    else line
                )
                for line in lines
            ]
            deform = sections["[TextureOverride_VB_abc123_Position]"]
            deform[3:3] = ["vs-t0 = ResourceZZMergedSkeleton_G3_s1", "draw = 3, 0"]
            for slot in (1, 2):
                index = deform.index(f"    vs-t0 = ResourceZZMergedSkeleton_G3_s{slot}")
                deform.insert(
                    index + 1, f"    so0 = ref ResourceZZRedirectSO_G3_s{slot}"
                )
        refs = lambda values: [
            types.SimpleNamespace(
                object=types.SimpleNamespace(name=value, name_full=value, type="MESH")
            )
            for value in values
        ]
        settings = types.SimpleNamespace(
            include_objects=refs(["Stocking0", "Stocking1"]),
            collider_objects=refs(["Body"]),
        )
        node._collect_enabled_zone_entries = lambda: [
            (0, types.SimpleNamespace(ssmt_drag_zone=settings))
        ]
        comp = node._locate_components(sections, ["abc123"])[0]
        cloth_names, body_names = node._cloth_object_names(required=True)
        if bake:
            comp["cloth"] = self.exporter.bake_component(
                node,
                str(folder),
                sections,
                comp,
                "unit",
                cloth_names,
                body_names,
                self.mod._blender_object_key,
            )
        return node, sections, comp

    def test_full_node_reexport_and_switch_off_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, _comp = self.prepare(root, bake=False)
            node.hash_values = "abc123"
            node.enable_viewport_probe = False
            node.feature_panel_link = False
            node.feature_shapekey_link = False
            node.feature_variable_link = False
            settings = node._collect_enabled_zone_entries()[0][1].ssmt_drag_zone
            for name, value in {
                "radius": 0.2,
                "max_offset": 0.5,
                "strength": 1.0,
                "spring": 0.1,
                "falloff": 0.5,
                "damping": 0.95,
                "grabbable": True,
                "brush_strength": 1.0,
                "brush_falloff_k": 1.0,
                "propagate": False,
            }.items():
                setattr(settings, name, value)
            matrix = np.eye(4) * 2
            matrix[3, 3] = 1
            matrix[2, 3] = 0.5
            empty = types.SimpleNamespace(
                name="ExportZone", matrix_world=matrix, ssmt_drag_zone=settings
            )
            node._collect_enabled_zone_entries = lambda: [(0, empty)]
            node._get_reference_matrix_inv = lambda comp: None
            node._get_non_mirror_mirror = lambda: None
            node._get_export_space_matrix = lambda *args: np.eye(4)
            ini = root / "model.ini"
            node._write_ordered_dict_to_ini(sections, str(ini))
            node.execute_postprocess(str(root))
            first = ini.read_text("utf8")
            node.execute_postprocess(str(root))
            second = ini.read_text("utf8")
            import difflib

            self.assertEqual(
                first,
                second,
                "\n".join(
                    difflib.unified_diff(first.splitlines(), second.splitlines())
                )[:5000],
            )
            node.cloth_slide_enabled = False
            node.execute_postprocess(str(root))
            self.assertNotIn("SSMTCloth", ini.read_text("utf8"))
            self.assertNotIn("$ssmtcloth_", ini.read_text("utf8"))
            self.assertFalse((root / "res/drag_cloth").exists())

    def test_cloth_can_generate_while_ordinary_collision_is_off(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.assertFalse(node._collision_enabled_for_component())
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            self.exporter.publish_before_draws(sections, comp)
            text = "\n".join("\n".join(lines) for lines in sections.values())
            self.assertIn("CommandListSSMTClothBind_abc123_unit", text)
            self.assertIn("R32_FLOAT", text)
            self.assertIn("stride = 4", text)
            self.assertIn("SkinPalette_s1 = copy ResourceZZMergedSkeleton_G3_s1", text)
            self.assertIn("SkinSource_s1 = ref so0", text)
            self.assertNotIn("CustomShaderDragCollisionLeaves", text)
            self.assertEqual(len(comp["cloth"]["garments"]), 2)
            for source in Path(temp).rglob("cloth_nodes.hlsl"):
                data = source.read_text("utf8")
                self.assertNotIn("#include", data)
                self.assertIn("geometryReady", data)

    def test_skinout_snapshots_the_converted_palette_at_each_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, bake=False)
            sections[comp["parts"][0]["section"]].insert(
                3, "vb0 = ref ResourceZZSkinOut_G3_s1"
            )
            for slot in (1, 2):
                output = f"ResourceZZSkinOut_G3_s{slot}"
                palette = f"ResourceZZPaletteConv_C7_s{slot}"
                sections[f"[{output}]"] = ["type = Buffer", "stride = 40"]
                sections[f"[{palette}]"] = [
                    "type = RWBuffer",
                    "format = R32G32B32A32_FLOAT",
                    "array = 6",
                ]
                sections[f"[CustomShaderZZMISkin_G3_s{slot}]"] = [
                    "cs = ./res/zzmi_merged_skin.hlsl",
                    f'x1 = {comp["vertex_count"]}',
                    "cs-t0 = ref Resource_abc123_Position",
                    f"cs-t2 = ref {palette}",
                    f"cs-u0 = ref {output}",
                    "Dispatch = 8, 1, 1",
                ]
            cloth, body = node._cloth_object_names(required=True)
            comp["cloth"] = self.exporter.bake_component(
                node,
                str(root),
                sections,
                comp,
                "unit",
                cloth,
                body,
                self.mod._blender_object_key,
            )
            node._emit_sections(sections, [comp], "unit")
            for slot in (1, 2):
                lines = sections[f"[CustomShaderZZMISkin_G3_s{slot}]"]
                dispatch = lines.index("Dispatch = 8, 1, 1")
                self.assertEqual(
                    lines[dispatch - 5 : dispatch],
                    [
                        self.exporter.DRAW_BEGIN,
                        f"ResourceSSMTCloth_abc123_unit_SkinSource_s{slot} = ref ResourceZZSkinOut_G3_s{slot}",
                        f"ResourceSSMTCloth_abc123_unit_SkinPalette_s{slot} = copy ResourceZZPaletteConv_C7_s{slot}",
                        f"$ssmtcloth_unit_abc123_ready_s{slot} = -1",
                        self.exporter.DRAW_END,
                    ],
                )
            self.assertFalse(
                any(
                    "SkinPalette" in line
                    for line in sections["[TextureOverride_VB_abc123_Position]"]
                )
            )

    def test_native_restore_and_ttl_publish_survive_legacy_rebinding(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.exporter.emit_components(node, sections, [comp], "unit")
            wrapper = "CommandListSSMTTTLDraw_unit"
            sections["[" + wrapper + "]"] = [
                "vb0 = ResourceDragJiggleTempVB0_abc123_unit",
                "run = CommandList\\TTL\\Draw",
            ]
            part = comp["parts"][0]
            sections[part["section"]].append("run = " + wrapper)
            self.exporter.publish_before_draws(sections, comp)
            block = sections["[" + wrapper + "]"]
            self.assertLess(
                block.index("vb0 = ResourceDragJiggleTempVB0_abc123_unit"),
                block.index("run = " + comp["cloth_publish"]),
            )
            self.assertLess(
                block.index("run = " + comp["cloth_publish"]),
                block.index("run = CommandList\\TTL\\Draw"),
            )
            node._inject_draw_hooks(sections, comp, "unit")
            self.assertIn(
                "post run = " + comp["cloth_restore"],
                "\n".join(sections[part["section"]]),
            )

    def test_turning_off_removes_owned_assets_and_inline_draw_references(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(folder)
            self.exporter.emit_components(node, sections, [comp], "unit")
            self.exporter.publish_before_draws(sections, comp)
            self.exporter.clean_sections(sections, "unit")
            self.exporter.prune_assets(folder, "unit", set())
            self.assertFalse((folder / "res/drag_cloth").exists())
            self.assertNotIn(
                "run = " + comp["cloth_publish"],
                "\n".join("\n".join(v) for v in sections.values()),
            )

    def test_repeated_export_keeps_shaders_and_buffers_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.prepare(folder)
            before = {
                str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (folder / "res/drag_cloth").rglob("*")
                if p.is_file()
            }
            self.prepare(folder)
            after = {
                str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (folder / "res/drag_cloth").rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)

    def test_material_copy_sections_have_bind_publish_and_native_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.exporter.emit_components(node, sections, [comp], "unit")
            copy = "[TextureOverrideMaterialCopy]"
            original = ["hash = abc123", "ib = ResourceIB", "drawindexed = 3, 0, 0"]
            sections[copy] = original.copy()
            comp["parts"][0]["draw_records"].append({"section": copy})
            self.exporter.publish_before_draws(sections, comp)
            self.assertIn("run = " + comp["cloth_bind"], sections[copy])
            self.assertIn("post run = " + comp["cloth_restore"], sections[copy])
            self.assertLess(
                sections[copy].index("run = " + comp["cloth_publish"]),
                sections[copy].index("drawindexed = 3, 0, 0"),
            )
            first = list(sections[copy])
            self.exporter.publish_before_draws(sections, comp)
            self.assertEqual(first, sections[copy])
            self.exporter.clean_sections(sections, "unit")
            self.assertEqual(sections[copy], original)

    def test_surface_collision_keeps_far_controlled_faces_without_point_anchors(self):
        node = _make_node(
            self.mod,
            collision_enabled=True,
            collision_tri_count=0,
            collision_point_budget=32,
        )
        p = np.array(
            [
                [-1, -1, 0],
                [1, -1, 0],
                [1, 1, 0],
                [-1, 1, 0],
                [-1, -1, 0.6],
                [1, -1, 0.6],
                [1, 1, 0.6],
                [-1, 1, 0.6],
            ],
            np.float32,
        )
        faces = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]], np.uint32)
        names = np.array(["Body", "Body", "Cloth", "Cloth"], object)
        settings = types.SimpleNamespace(
            collider_objects=[
                types.SimpleNamespace(
                    object=types.SimpleNamespace(
                        name="Body", name_full="Body", type="MESH"
                    )
                )
            ]
        )
        node._collect_enabled_zone_entries = lambda: [
            (0, types.SimpleNamespace(ssmt_drag_zone=settings))
        ]
        node._read_position_buf = lambda *args: p
        node._read_vertex_normals = lambda *args: np.tile([0, 0, 1], (8, 1))
        node._read_component_triangles = lambda *args: (faces, names)
        node._buffer_dir = lambda *args: "Meshes"
        node._collider_l0_mask = lambda *args: np.array([False] * 4 + [True] * 4)
        node._collider_contact_exact = lambda *args, **kw: np.zeros((16, 4), np.float32)
        comp = {
            "vertex_count": 8,
            "base_name": "sample",
            "comp_name": "sample",
            "parts": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(node._write_collision_resources(temp, {}, comp, "unit"))
            np.testing.assert_array_equal(
                np.fromfile(
                    Path(temp) / "Meshes/sampleColliderCloth.buf", "<u4"
                ).reshape(-1, 3),
                faces[2:],
            )
            self.assertEqual(comp["collision_surface"]["face_count"], 2)

    def test_preserved_ttl_tail_is_republished_and_can_be_cleaned(self):
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            self.exporter.emit_components(node, sections, [comp], "unit")
            name = "CommandListSSMTTTLDraw_old"
            sections[comp["parts"][0]["section"]].append("run = " + name)
            original = (
                "[SomeUI]\ndata = 1\n["
                + name
                + "]\nvb0 = ResourceDragJiggleTempVB0_abc123_unit\nrun = CommandList\\TTL\\Draw\n"
            )
            patched = self.exporter.publish_in_tail(original, sections, [comp])
            self.assertIn("run = " + comp["cloth_publish"], patched)
            self.assertEqual(self.exporter.clean_tail(patched), original)
            self.assertEqual(
                self.exporter.publish_in_tail(patched, sections, [comp]), patched
            )

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_generated_proxy_mapping_keeps_extra_vertex_fields(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("mapped_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(
                folder, dense=True, extra=True, budget=160
            )
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            runner = gpu.GeneratedCloth(folder, sections, comp)
            try:
                runner.prepare_identity()
                native = runner.native.copy()
                g = comp["cloth"]["garments"][0]
                mask = g["map"] != 0xFFFFFFFF
                self.assertLessEqual(len(g["representatives"]), 160)
                row = int(np.flatnonzero(mask)[len(np.flatnonzero(mask)) // 2])
                runner.frame(hit_row=row)
                runner.frame(600, True, row)
                for k in range(1, 15):
                    out = runner.frame(600 - k * 4, True, row)
                np.testing.assert_array_equal(out[:, 40:], native[:, 40:])
                np.testing.assert_array_equal(out[~mask], native[~mask])
                self.assertGreater(
                    np.max(
                        np.linalg.norm(
                            out.copy().view("<f4").reshape(-1, 12)[mask, :3]
                            - native.copy().view("<f4").reshape(-1, 12)[mask, :3],
                            axis=1,
                        )
                    ),
                    0.02,
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_unready_native_frame_does_not_poison_first_generated_drag(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("startup_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(folder)
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            runner = gpu.GeneratedCloth(folder, sections, comp)
            try:
                runner.prepare_identity()
                native = runner.native.copy()
                meta = comp["cloth"]
                masks = [g["map"] != 0xFFFFFFFF for g in meta["garments"]]
                all_cloth = np.logical_or.reduce(masks)
                source = runner.resources[
                    f'{meta["native_family"]}_G{meta["group"]}_s1'
                ]
                broken = native.copy()
                broken[all_cloth] = 0

                def upload(value):
                    runner.gpu.method(
                        runner.gpu.context,
                        48,
                        [
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_void_p,
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_uint,
                        ],
                        source.pointer,
                        0,
                        None,
                        value.ctypes.data,
                        0,
                        0,
                        result=None,
                    )

                row = int(np.flatnonzero(masks[0])[30])
                upload(broken)
                runner.frame(hit_row=row)
                for name, value in runner.resources.items():
                    if name.endswith("_State"):
                        state = (
                            runner.gpu.read_bytes(value.pointer, value.size)
                            .copy()
                            .view("<f4")
                            .reshape(-1, 4)
                        )
                        self.assertEqual(state[23, 3], 0)
                        self.assertEqual(state[17, 2], 0)
                upload(native)
                np.testing.assert_array_equal(runner.frame(hit_row=row), native)
                runner.frame(600, True, row)
                for k in range(1, 16):
                    first = runner.frame(600 - k * 4, True, row)
                runner.variables[runner.prefix + "reset"] = 1
                runner.frame(540, False, row)
                runner.frame(600, True, row)
                for k in range(1, 16):
                    reset = runner.frame(600 - k * 4, True, row)
                np.testing.assert_allclose(
                    first.copy().view("<f4").reshape(-1, 10)[:, :3],
                    reset.copy().view("<f4").reshape(-1, 10)[:, :3],
                    atol=2e-5,
                )
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_emitted_pipeline_runs_independent_stockings_and_preserves_body(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("generated_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            node, sections, comp = self.prepare(folder)
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            runner = gpu.GeneratedCloth(folder, sections, comp)
            try:
                runner.prepare_identity()
                native = runner.native.copy()
                garments = comp["cloth"]["garments"]
                left = next(d for d in garments if d["config"][12, 3] == 1)
                right = next(d for d in garments if d["config"][12, 3] == 2)
                left_mask = left["map"] != 0xFFFFFFFF
                right_mask = right["map"] != 0xFFFFFFFF
                row = int(
                    np.flatnonzero(left_mask)[len(np.flatnonzero(left_mask)) // 2]
                )
                idle = runner.frame(hit_row=row)
                np.testing.assert_array_equal(idle, native)
                runner.frame(600, True, row)
                for k in range(1, 20):
                    out = runner.frame(600 - k * 4, True, row)
                out_float = out.copy().view("<f4").reshape(-1, 10)
                native_float = native.copy().view("<f4").reshape(-1, 10)
                self.assertGreater(
                    np.max(
                        np.linalg.norm(
                            out_float[left_mask, :3] - native_float[left_mask, :3],
                            axis=1,
                        )
                    ),
                    0.02,
                )
                np.testing.assert_array_equal(out[right_mask], native[right_mask])
                np.testing.assert_array_equal(
                    out[~(left_mask | right_mask)], native[~(left_mask | right_mask)]
                )
                runner.external(
                    "vb2", np.zeros((len(native), 16), np.uint8), 16, flags=1
                )
                # Re-capture an actual render VB2 stride, independently of blend data.
                runner.frame(524, False, row)
                source = runner.resources[
                    f'{comp["cloth"]["native_family"]}_G{comp["cloth"]["group"]}_s1'
                ]
                runner.external("legacy_temp", np.zeros_like(native), 40, flags=1)
                runner.resources["vb0"] = runner.resources["legacy_temp"]
                runner.execute(comp["cloth_publish"])
                self.assertNotEqual(
                    runner.resources["vb0"].pointer,
                    runner.resources["legacy_temp"].pointer,
                )
                runner.execute(comp["cloth_restore"])
                self.assertEqual(runner.resources["vb0"].pointer, source.pointer)
                self.assertEqual(runner.resources["vb2"].stride, 16)
                count = runner.dispatches
                runner.variables[runner.prefix + "enabled"] = 0
                np.testing.assert_array_equal(runner.frame(), native)
                self.assertEqual(runner.dispatches, count)
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_generated_cloth_uses_the_palette_that_skinned_the_vertices(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("cloth_palette_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            node, sections, comp = self.prepare(Path(temp))
            node._emit_sections(sections, [comp], "unit")
            runner = gpu.GeneratedCloth(temp, sections, comp)
            try:
                runner.prepare_identity()
                mask = comp["cloth"]["garments"][0]["map"] != 0xFFFFFFFF
                row = int(np.flatnonzero(mask)[30])
                runner.frame(hit_row=row)
                runner.frame(600, True, row)
                for step in range(1, 23):
                    dressed = runner.frame(600 - step * 4, True, row)
                self.assertFalse(np.array_equal(dressed[mask], runner.native[mask]))

                # Simulate a later attach writing a different pose into the live
                # shared palette after the native SO has already been skinned.
                updated = np.eye(4, dtype=np.float32)[:3].copy()
                updated[0, 3] = 0.15
                for slot in (1, 2):
                    runner.external(
                        f"ResourceZZMergedSkeleton_G3_s{slot}", updated, 16, 2
                    )
                out = runner.frame(512, False, row)
                self.assertFalse(
                    np.array_equal(out[mask], runner.native[mask]),
                    "a late bone write must not flash back to the fully worn mesh",
                )
                np.testing.assert_array_equal(out[~mask], runner.native[~mask])
            finally:
                runner.close()

    @unittest.skipUnless(sys.platform == "win32", "D3D11 execution requires Windows")
    def test_prefix_reference_copy_follows_current_bind_changes(self):
        path = Path(__file__).resolve().parents[1] / "tools/check_drag_cloth.py"
        spec = importlib.util.spec_from_file_location("prefix_cloth_gpu", path)
        gpu = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gpu)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node, sections, comp = self.prepare(root, prefix=3)
            node._emit_sections(sections, [comp], "unit")
            node._inject_draw_hooks(sections, comp, "unit")
            self.assertIsNone(comp["cloth"]["dynamic_bind"])
            self.assertEqual(len(comp["cloth"]["skin_writes"]), 2)
            runner = gpu.GeneratedCloth(root, sections, comp)
            try:
                runner.prepare_identity()
                g = comp["cloth"]["garments"][0]
                mask = g["map"] != 0xFFFFFFFF
                row = int(np.flatnonzero(mask)[30])
                runner.frame(hit_row=row)
                runner.frame(600, True, row)
                for k in range(1, 10):
                    runner.frame(600 - k * 4, True, row)
                local = np.fromfile(root / "Meshes/abc123-Position.buf", "<f4").reshape(
                    -1, 10
                )
                local[:, :3] += [0, 0, 0.04]
                native = runner.native.copy()
                native[3:] = local.view(np.uint8).reshape(-1, 40)
                native_source = runner.resources[
                    f'{comp["cloth"]["native_family"]}_G{comp["cloth"]["group"]}_s1'
                ]
                raw_source = runner.resources[comp["base_resource"]]
                for target, value in ((native_source, native), (raw_source, local)):
                    runner.gpu.method(
                        runner.gpu.context,
                        48,
                        [
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_void_p,
                            gpu.ct.c_void_p,
                            gpu.ct.c_uint,
                            gpu.ct.c_uint,
                        ],
                        target.pointer,
                        0,
                        None,
                        value.ctypes.data,
                        0,
                        0,
                        result=None,
                    )
                out = runner.frame(564, False, row)
                decisions = [
                    value
                    for name, value in runner.resources.items()
                    if name.endswith("_Decision")
                ]
                for value in decisions:
                    self.assertEqual(
                        runner.gpu.read_bytes(value.pointer, value.size)
                        .view("<f4")
                        .reshape(-1, 4)[0, 3],
                        1,
                    )
                np.testing.assert_array_equal(out[:3], native[:3])
                np.testing.assert_array_equal(
                    out[~mask & (g["map"] == 0xFFFFFFFF)],
                    native[~mask & (g["map"] == 0xFFFFFFFF)],
                )
                # Selected cloth remains dressed rather than falling back to full native output.
                self.assertGreater(
                    np.max(abs(out[mask].view("<f4") - native[mask].view("<f4"))), 0.01
                )
            finally:
                runner.close()


if __name__ == "__main__":
    unittest.main()
