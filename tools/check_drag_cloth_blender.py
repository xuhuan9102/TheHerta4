"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""

import json
import sys
import types
from pathlib import Path

import addon_utils
import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
addon_utils.enable(ROOT.name, default_set=True, persistent=False)
tree = None
try:
    tree = bpy.data.node_groups.new(
        "Cloth generator RNA check", "SSMTBlueprintTreeType"
    )
    node = tree.nodes.new("SSMTNode_PostProcess_DragInteraction")
    assert node.cloth_slide_enabled is False
    assert node.cloth_drag_sensitivity == 2.0
    assert abs(node.cloth_bend_compliance - 0.0008) < 1e-7
    node.cloth_slide_enabled = True
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
        "collision_enabled",
        "cloth_slide_enabled",
        "cloth_collection",
        "cloth_collider_collection",
        "cloth_drag_sensitivity",
        "cloth_fold_height",
        "cloth_fold_spacing",
        "cloth_up_axis",
    ):
        assert name in properties, name
    assert not any("未完善" in text for text in labels)
    print(
        json.dumps(
            {
                "blender": bpy.app.version_string,
                "rna_registration": True,
                "node_ui_properties": len(properties),
                "cloth_default_off": True,
                "independent_collision_and_cloth_switches": True,
                "unfinished_label_removed": True,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
finally:
    if tree is not None:
        bpy.data.node_groups.remove(tree)
    addon_utils.disable(ROOT.name, default_set=True)
