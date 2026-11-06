"""TTL 表面分层回归；用 Blender 后台运行，覆盖真实网格与完整 INI 导出。"""
import math
from pathlib import Path
import runpy
import sys
import tempfile
import unittest

try:
    import _bpy  # Only Blender's real runtime provides this extension.
    import bpy as real_bpy
    from mathutils import Matrix
    if not hasattr(real_bpy.data, "meshes"):
        real_bpy = None
except (ImportError, AttributeError):
    real_bpy = None


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(real_bpy is None, "requires Blender's bpy and mathutils")
class TTLSurfaceLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.support = runpy.run_path(str(ROOT / "tests" / "test_ttl_layer.py"))
        sys.modules["bpy"] = real_bpy
        cls.support["_load_blueprint_module"]("ttl_surface_layers")

    def setUp(self):
        self.support["_reset_fake_scene"]()
        self.meshes = []
        self.objects = []

    def tearDown(self):
        for obj in self.objects:
            real_bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in self.meshes:
            real_bpy.data.meshes.remove(mesh)

    def _object(self, name, points, faces, matrix=None):
        mesh = real_bpy.data.meshes.new(name)
        mesh.from_pydata(points, [], faces)
        mesh.update()
        self.meshes.append(mesh)
        actual = real_bpy.data.objects.new(name, mesh)
        self.objects.append(actual)
        obj = self.support["_FakeObject"](
            name, [("TTLMap_Generic", "")],
            bound_box=list(actual.bound_box),
            matrix_world=matrix or Matrix.Identity(4),
        )
        obj.data = mesh
        self.support["_fake_bpy"].data.objects[name] = obj
        return obj

    def _skirt(self, name, offset=0, lower=0, upper=1, start=0, end=2 * math.pi):
        points = []
        segments = 48
        for height in (lower, upper):
            radius = 0.4 - 0.3 * height + offset
            for i in range(segments + 1):
                angle = start + (end - start) * i / segments
                points.append((radius * math.cos(angle), radius * math.sin(angle), height))
        stride = segments + 1
        faces = [(i, i + 1, i + 1 + stride, i + stride) for i in range(segments)]
        return self._object(name, points, faces)

    def _plane(self, name, radius, lower=0, upper=0.2, reverse=False, matrix=None):
        points = [(radius, -.04, lower), (radius, .04, lower),
                  (radius, .04, upper), (radius, -.04, upper)]
        face = (3, 2, 1, 0) if reverse else (0, 1, 2, 3)
        return self._object(name, points, [face], matrix)

    def _layers(self, objects):
        node = self.support["_ttl_node"]()
        sections = {"[TextureOverride_Parts]": [f"; [mesh:{obj.name}]" for obj in objects]}
        node._ttl_prepare_auto_layers(sections)
        return {obj.name: node._ttl_auto_layer_assignments[id(obj)][0] for obj in objects}, node

    def test_shorter_outer_skirts_stay_outside(self):
        objects = [self._skirt("Inner"),
                   self._skirt("Middle", .01, lower=.15),
                   self._skirt("Outer", .02, lower=.3)]
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {"Inner": 0, "Middle": 1, "Outer": 2})

    def test_one_millimeter_gaps_get_separate_layers(self):
        objects = [self._skirt(f"Skirt{i}", i * .001) for i in range(3)]
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {"Skirt0": 0, "Skirt1": 1, "Skirt2": 2})

    def test_panels_of_the_same_shell_share_a_layer(self):
        objects = []
        for layer in range(2):
            for part in range(4):
                objects.append(self._skirt(
                    f"L{layer}Part{part}", layer * .001,
                    start=part * math.pi / 2, end=(part + 1) * math.pi / 2,
                ))
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {obj.name: int(obj.name[1]) for obj in objects})

    def test_independent_structures_reuse_layers(self):
        objects = [self._plane("SkirtInner", .3), self._plane("SkirtOuter", .31),
                   self._plane("SleeveInner", .1, lower=1, upper=1.2),
                   self._plane("SleeveOuter", .11, lower=1, upper=1.2)]
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {"SkirtInner": 0, "SkirtOuter": 1,
                                  "SleeveInner": 0, "SleeveOuter": 1})

    def test_winding_and_section_order_do_not_change_layers(self):
        objects = [self._plane("Inner", .2, reverse=True), self._plane("Outer", .201)]
        forward, _ = self._layers(objects)
        backward, _ = self._layers(list(reversed(objects)))
        self.assertEqual(forward, {"Inner": 0, "Outer": 1})
        self.assertEqual(forward, backward)

    def test_offset_sleeves_have_a_local_inside_and_outside(self):
        objects = []
        for layer in range(2):
            points = []
            segments = 48
            for x in (.6, 1.0):
                for i in range(segments):
                    angle = 2 * math.pi * i / segments
                    radius = .08 + layer * .001
                    points.append((x, .5 + radius * math.cos(angle), 1.2 + radius * math.sin(angle)))
            faces = [(i, (i + 1) % segments, (i + 1) % segments + segments, i + segments)
                     for i in range(segments)]
            objects.append(self._object(f"Sleeve{layer}", points, faces))
        layers, node = self._layers(objects)
        self.assertEqual(layers, {"Sleeve0": 0, "Sleeve1": 1})
        self.assertEqual(getattr(node, "_ttl_auto_layer_skipped", []), [])

    def test_horizontal_stacked_panels(self):
        objects = []
        for layer in range(3):
            z = .5 + layer * .001
            objects.append(self._object(f"Panel{layer}",
                [(-.1, -.1, z), (.1, -.1, z), (.1, .1, z), (-.1, .1, z)], [(0, 1, 2, 3)]))
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {"Panel0": 0, "Panel1": 1, "Panel2": 2})

    def test_mirrored_rotated_translated_meshes_use_world_geometry(self):
        transform = (Matrix.Translation((.5, .2, .3))
                     @ Matrix.Rotation(.7, 4, "Z") @ Matrix.Diagonal((-1, 1.2, .8, 1)))
        armature = self.support["_FakeArmature"](transform)
        objects = [self._plane("Inner", .2, matrix=transform),
                   self._plane("Outer", .201, reverse=True, matrix=transform)]
        for obj in objects:
            obj.armature = armature
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {"Inner": 0, "Outer": 1})

    def test_distinct_skeletons_do_not_share_order_constraints(self):
        objects = [self._plane("A", .2), self._plane("B", .201)]
        for obj in objects:
            obj.armature = self.support["_FakeArmature"](Matrix.Identity(4), name=obj.name)
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {"A": 0, "B": 0})

    def test_degenerate_faces_keep_radius_fallback(self):
        obj = self._object("Degenerate", [(.2, 0, 0), (.2, 0, .1), (.2, 0, .2)], [(0, 1, 2)])
        layers, node = self._layers([obj])
        self.assertEqual(layers, {"Degenerate": 0})
        self.assertNotIn("surface", node._ttl_auto_layer_assignments[id(obj)][1])

    def test_coincident_alternative_meshes_do_not_consume_layers(self):
        objects = [self._skirt("AlternativeA"), self._skirt("AlternativeB")]
        layers, _ = self._layers(objects)
        self.assertEqual(layers, {"AlternativeA": 0, "AlternativeB": 0})

    def test_surface_overflow_clamps_and_warns(self):
        objects = [self._plane(f"Layer{i}", .2 + i * .001) for i in range(6)]
        layers, node = self._layers(objects)
        self.assertEqual(layers, {obj.name: min(i, 4) for i, obj in enumerate(objects)})
        self.assertIn("Layer5", node._ttl_auto_layer_skipped)

    def test_crossing_surfaces_report_ambiguous_order(self):
        flat = self._plane("Flat", .2)
        crossing = self._object("Crossing",
            [(.19, -.04, 0), (.21, .04, 0), (.21, .04, .2), (.19, -.04, .2)], [(0, 1, 2, 3)])
        layers, node = self._layers([flat, crossing])
        self.assertNotEqual(layers[flat.name], layers[crossing.name])
        self.assertEqual(set(node._ttl_auto_layer_skipped), {flat.name, crossing.name})

    def test_cycle_breaking_is_deterministic(self):
        module = sys.modules[self.support["PKG"] + ".blueprint.ttl_surface_layers"]
        relations = [("A", "B", 1.0, 10), ("B", "C", 1.0, 8), ("C", "A", .9, 5)]
        first = module._ordered_layers(["A", "B", "C"], relations)
        second = module._ordered_layers(["C", "B", "A"], list(reversed(relations)))
        self.assertEqual(first, second)
        self.assertEqual(first, ({"A": 0, "B": 1, "C": 2}, [("C", "A")]))

    def test_full_export_writes_four_plane_layers(self):
        objects = [self._plane(name, radius) for name, radius in (
            ("Plane", .145), ("Plane.001", .192), ("Plane.002", .182), ("Plane.003", .201),
        )]
        with tempfile.TemporaryDirectory() as directory:
            path = self.support["_write_png"](directory, "ttl.png")
            for obj in objects:
                obj.material_slots[0].material = self.support["_FakeMaterial"]("TTLMap_Generic", path)
            ini = "\n".join(self.support["_section_text"](f"TextureOverride_{i}", obj.name)
                            for i, obj in enumerate(objects))
            body = self.support["_export_bytes"](directory, ini).decode("utf-8")
        written = {}
        current = None
        for line in body.splitlines():
            if "[mesh:" in line:
                current = line.split("[mesh:", 1)[1].split("]", 1)[0]
            elif "layer_index =" in line:
                written[current] = int(line.rsplit("=", 1)[1])
        self.assertEqual(written, {"Plane": 0, "Plane.001": 2, "Plane.002": 1, "Plane.003": 3})
        self.assertIn("surface", body)

    def test_manual_layer_remains_authoritative(self):
        objects = [self._plane("Inner_TTL层3", .2), self._plane("Outer", .201)]
        node = self.support["_ttl_node"]()
        node._ttl_prepare_auto_layers({"[Part]": [f"; [mesh:{obj.name}]" for obj in objects]})
        self.assertNotIn(id(objects[0]), node._ttl_auto_layer_assignments)
        self.assertEqual(node._ttl_layer_settings_for_mesh(objects[0].name, objects[0]), (3, 1.0))


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    unittest.main(argv=[sys.argv[0]] + args)
