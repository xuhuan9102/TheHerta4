"""Check node sizing with Blender's actual RNA width limits."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo.parent))
addon = importlib.import_module(repo.name)


class LayoutRecorder:
    def __init__(self):
        self.split_factors = []

    def row(self, **_kwargs):
        return self

    def split(self, *, factor, **_kwargs):
        self.split_factors.append(factor)
        return self

    def operator(self, *_args, **_kwargs):
        return SimpleNamespace()

    def prop(self, *_args, **_kwargs):
        pass

    def separator(self):
        pass

    def label(self, **_kwargs):
        pass


addon.register()
tree = bpy.data.node_groups.new("Node Width Regression", "SSMTBlueprintTreeType")

long_text = "节点宽度验证" * 20
for node_type, field in (
    ("SSMTNode_ObjectSwap", "comment"),
    ("SSMTNode_Blueprint_Nest", "blueprint_name"),
):
    node = tree.nodes.new(node_type)
    setattr(node, field, long_text)
    required_width = node.calculate_text_width(long_text)
    assert required_width > 700
    assert abs(node.width - required_width) < 1, (node_type, node.width, required_width)

swap = tree.nodes.new("SSMTNode_ObjectSwap")
initial_width = swap.width
swap.comment = "Short"
assert swap.width >= initial_width, (swap.width, initial_width)

swap.hotkey = "Ctrl Shift Alt " + "NumpadDecimal " * 7
layout = LayoutRecorder()
swap.draw_buttons(bpy.context, layout)
factor, = layout.split_factors
assert 0 < factor < 1
field_width = swap.width * factor
assert field_width >= swap.calculate_text_width(swap.hotkey) - 1, (field_width, swap.width)

print("PASS: long node fields, initial width and key picker spacing")
