"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""

import json
import math
import sys
import types
from pathlib import Path

import addon_utils
import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
addon_utils.enable(ROOT.name, default_set=True, persistent=False)
tree = None
zone = None
path_objects = []
mesh = None
garment = None
try:
    tree = bpy.data.node_groups.new(
        "Cloth generator RNA check", "SSMTBlueprintTreeType"
    )
    node = tree.nodes.new("SSMTNode_PostProcess_DragInteraction")
    assert node.tear_enabled is False
    assert node.cloth_slide_enabled is False
    assert node.cloth_drag_sensitivity == 2.0
    assert node.cloth_surface_gap_mm == 1.0
    assert node.cloth_sleep_enabled is True
    assert node.cloth_self_collision is False
    assert abs(node.cloth_bend_compliance - 0.0008) < 1e-7
    node.cloth_slide_enabled = True
    node.tear_enabled = True
    zone = bpy.data.objects.new("Directional cloth RNA zone", None)
    item = node.zone_objects.add()
    item.zone_id = 0
    item.zone_object = zone
    item.expanded = True
    assert zone.ssmt_drag_zone.cloth_direction == "AUTO"
    zone.ssmt_drag_zone.cloth_direction = "POSITIVE"
    labels = []
    properties = []
    icons = {
        item.identifier
        for item in bpy.types.UILayout.bl_rna.functions["label"]
        .parameters["icon"]
        .enum_items
    }

    class Layout:
        enabled = True

        def row(self, *args, **kwargs):
            return self

        def column(self, *args, **kwargs):
            return self

        def box(self, *args, **kwargs):
            return self

        def split(self, *args, **kwargs):
            return self

        def label(self, **kwargs):
            labels.append(kwargs.get("text", ""))
            assert kwargs.get("icon", "NONE") in icons, kwargs

        def prop(self, data, name, **kwargs):
            assert name in data.bl_rna.properties, (type(data), name)
            properties.append(name)
            if "text" in kwargs:
                labels.append(kwargs["text"])

        def operator(self, *args, **kwargs):
            return types.SimpleNamespace()

        def separator(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            return lambda *args, **kwargs: self

    node.draw_buttons(bpy.context, Layout())
    for name in (
        "tear_enabled",
        "collision_enabled",
        "cloth_slide_enabled",
        "cloth_drag_sensitivity",
        "cloth_surface_gap_mm",
        "cloth_sleep_enabled",
        "cloth_self_collision",
        "cloth_fold_height",
        "cloth_fold_spacing",
        "cloth_up_axis",
        "cloth_min_span_ratio",
        "cloth_direction",
    ):
        assert name in properties, name
    for name in ("cloth_collection", "cloth_collider_collection"):
        assert name not in node.bl_rna.properties
    assert not any("未完善" in text for text in labels)
    # Exercise the actual editable viewport handles and distance driver.
    mesh = bpy.data.meshes.new("Path test cylinder")
    vertices = [
        (0.2 * math.cos(i * math.tau / 12), 0.2 * math.sin(i * math.tau / 12), z)
        for z in (0, 1)
        for i in range(12)
    ]
    faces = [(i, (i + 1) % 12, (i + 1) % 12 + 12, i + 12) for i in range(12)]
    mesh.from_pydata(vertices, [], faces)
    garment = bpy.data.objects.new("Path test garment", mesh)
    bpy.context.scene.collection.objects.link(garment)
    zone.ssmt_drag_zone.include_objects.add().object = garment
    assert bpy.ops.ssmt.drag_cloth_path(empty_name=zone.name, action="CREATE") == {
        "FINISHED"
    }
    start = zone.ssmt_drag_zone.cloth_path_start
    end = zone.ssmt_drag_zone.cloth_path_end
    path_objects.extend((start, end))
    bpy.context.view_layer.update()
    assert start.empty_display_type == "SINGLE_ARROW"
    assert abs(start.empty_display_size - (end.location - start.location).length) < 1e-5
    end.location.z = 0.4
    bpy.context.view_layer.update()
    assert abs(start.empty_display_size - 0.4) < 1e-5, start.empty_display_size
    from mathutils import Vector

    tip = start.matrix_world @ Vector((0, 0, start.empty_display_size))
    assert (tip - end.matrix_world.translation).length < 1e-5
    end.location = (.2, .1, .4)
    bpy.context.view_layer.update()
    tip = start.matrix_world @ Vector((0, 0, start.empty_display_size))
    assert (tip - end.matrix_world.translation).length < 1e-5
    print(
        json.dumps(
            {
                "blender": bpy.app.version_string,
                "rna_registration": True,
                "node_ui_properties": len(properties),
                "cloth_default_off": True,
                "tear_default_off": True,
                "independent_collision_and_cloth_switches": True,
                "unfinished_label_removed": True,
                "per_zone_direction": True,
                "editable_arrow_endpoints": True,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
finally:
    for obj in path_objects:
        bpy.data.objects.remove(obj, do_unlink=True)
    if garment is not None:
        bpy.data.objects.remove(garment, do_unlink=True)
    if mesh is not None:
        bpy.data.meshes.remove(mesh)
    if tree is not None:
        bpy.data.node_groups.remove(tree)
    if zone is not None:
        bpy.data.objects.remove(zone)
    addon_utils.disable(ROOT.name, default_set=True)
