"""Multi-object sequence sampling and legacy target migration regressions."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test_anim_driver_base import shapekey_seq_module as module


class Collection(list):
    def add(self):
        item = SimpleNamespace(name='')
        self.append(item)
        return item

    def remove(self, index):
        del self[index]


class KeyBlocks(list):
    def get(self, name):
        return next((item for item in self if item.name == name), None)


class Mesh:
    type = 'MESH'

    def __init__(self, name, key, value):
        self.name = name
        self.key = SimpleNamespace(name=key, value=value)
        self.data = SimpleNamespace(shape_keys=SimpleNamespace(key_blocks=KeyBlocks([self.key])))


def operator(cls):
    value = cls()
    value.node_name = ''
    value.report = lambda *_: None
    return value


@pytest.mark.parametrize('fail', [False, True])
def test_sampling_restores_scene_and_publishes_complete_tracks_only(fail):
    head, body = Mesh('Head', 'Smile', .33), Mesh('Body', 'Blink', .44)
    items = [SimpleNamespace(name=name, frame_values_csv='saved') for name in ('Smile', 'Blink', 'Missing')]
    node = SimpleNamespace(get_target_object_names=lambda: ['Head', 'Body'],
                           frame_start=1, frame_end=3, shapekey_items=items)

    class Scene:
        frame_current = 9
        frame_subframe = .25

        def frame_set(self, frame, subframe=0):
            self.frame_current, self.frame_subframe = frame, subframe
            head.key.value, body.key.value = frame/10, frame/20
            if fail and frame == 2:
                raise RuntimeError('sample evaluation failed')

    scene = Scene()
    context = SimpleNamespace(scene=scene, space_data=SimpleNamespace(edit_tree=SimpleNamespace(
        nodes=SimpleNamespace(active=node))))
    with patch.object(module.bpy.data, 'objects', {'Head': head, 'Body': body}):
        op = operator(module.SSMT_OT_ShapeKeyAnimSample)
        if fail:
            with pytest.raises(RuntimeError, match='sample evaluation failed'):
                op.execute(context)
            assert [item.frame_values_csv for item in items] == ['saved']*3
        else:
            assert op.execute(context) == {'FINISHED'}
            assert [item.frame_values_csv for item in items] == [
                '0.100,0.200,0.300', '0.050,0.100,0.150', '0.000,0.000,0.000']
    assert (scene.frame_current, scene.frame_subframe) == (9, .25)
    assert (head.key.value, body.key.value) == (.33, .44)


def test_removing_migrated_target_cannot_restore_it_from_legacy_field():
    node = module.SSMTNode_AnimDriver_ShapeKeySequence()
    node.target_object = 'Legacy'
    node.target_objects = Collection([SimpleNamespace(name='Legacy'), SimpleNamespace(name='Other')])
    node.target_objects_active = 0
    context = SimpleNamespace(space_data=SimpleNamespace(edit_tree=SimpleNamespace(
        nodes=SimpleNamespace(active=node))))
    assert operator(module.SSMT_OT_ShapeKeyAnimTargetRemove).execute(context) == {'FINISHED'}
    assert node.get_target_object_names() == ['Other']


def test_migration_moves_legacy_target_into_an_existing_list():
    node = module.SSMTNode_AnimDriver_ShapeKeySequence()
    node.name, node.custom_paused_var, node.driven_variable = 'Seq', '$pause', '$seq'
    node.target_object = 'Legacy'
    node.target_objects = Collection([SimpleNamespace(name='Other')])
    tree = SimpleNamespace(bl_idname='SSMTBlueprintTreeType', nodes=[node])
    with patch.object(module.bpy.data, 'node_groups', [tree]), \
         patch.object(module.SSMTNode_AnimDriver_Base, 'migrate_default_play_state_flag'), \
         patch.object(module.SSMTNode_AnimDriver_Base, '_migrate_dynamic_sockets'):
        assert module.migrate_existing_shapekey_seq_nodes() == 1
        assert module.migrate_existing_shapekey_seq_nodes() == 0
    assert [item.name for item in node.target_objects] == ['Other', 'Legacy']
    assert node.target_object == ''
