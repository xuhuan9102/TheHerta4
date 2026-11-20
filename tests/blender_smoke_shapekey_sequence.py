"""Headless Blender: sequence target collections, sampling and INI playback."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import bpy

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sequence_smoke_setup', ROOT/'tests/blender_smoke_anim_driver_preview.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
sequence = setup._load('blueprint.anim_driver_shapekey_seq')
for name in ('blueprint.node_base', 'blueprint.anim_driver_base', 'blueprint.anim_driver_runtime'):
    sys.modules[setup.PKG+'.'+name].register()
sequence.register()

tree = bpy.data.node_groups.new('SequenceSmoke', 'SSMTBlueprintTreeType')
tree['is_animation_driver'] = True
runtime = tree.nodes.new('SSMTNode_AnimDriver_Runtime')
runtime.fps = 30
node = tree.nodes.new('SSMTNode_AnimDriver_ShapeKeySequence')
tree.links.new(runtime.outputs[0], node.inputs[0])


def mesh(name, key):
    data = bpy.data.meshes.new(name)
    data.from_pydata([(0, 0, 0)], [], [])
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.shape_key_add(name='Basis')
    return obj, obj.shape_key_add(name=key)


head, smile = mesh('Head', 'Smile')
body, blink = mesh('Body', 'Blink')
node.target_object = head.name
node.target_objects.add().name = body.name
assert sequence.migrate_existing_shapekey_seq_nodes() == 1
assert node.get_target_object_names() == ['Body', 'Head']
assert node.target_object == ''
assert sequence.migrate_existing_shapekey_seq_nodes() == 0
for name in ('Smile', 'Blink'):
    item = node.shapekey_items.add()
    item.name, item.variable = name, '$Freq_'+name
node.frame_start, node.frame_end = 1, 3
scene = bpy.context.scene
scene.frame_set(9, subframe=.25)
smile.value, blink.value = .33, .44


def values_at_frame(scene):
    smile.value, blink.value = scene.frame_current/10, scene.frame_current/20


context = SimpleNamespace(scene=scene, space_data=SimpleNamespace(edit_tree=tree))
tree.nodes.active = node
call = SimpleNamespace(node_name='', report=lambda *_: None)
bpy.app.handlers.frame_change_post.append(values_at_frame)
try:
    assert sequence.SSMT_OT_ShapeKeyAnimSample.execute(call, context) == {'FINISHED'}
finally:
    bpy.app.handlers.frame_change_post.remove(values_at_frame)
assert [item.frame_values_csv for item in node.shapekey_items] == ['0.100,0.200,0.300', '0.050,0.100,0.150']
assert (scene.frame_current, scene.frame_subframe) == (9, .25)
assert abs(smile.value-.33) < 1e-6 and abs(blink.value-.44) < 1e-6
node.pingpong_playback = True
preview = sys.modules[setup.PKG+'.blueprint.anim_driver_preview']
result = preview.simulate_anim_driver_paragraphs([
    {'ini_content': runtime.generate_ini_segment()}, {'ini_content': node.generate_ini_segment()}], 8, 30)
assert result['skipped_commands'] == [], result['skipped_commands']
assert result['variables']['$Freq_Smile'] == [.1, .2, .3, .2, .1, .2, .3, .2]
print('SHAPEKEY_SEQUENCE_SMOKE_OK: registration, migration, multi-object sampling, state restore, pingpong')
