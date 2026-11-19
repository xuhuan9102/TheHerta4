"""Compare fluid profiles with real export-preprocessed triangles in Blender."""
import importlib
import json
import struct
import sys
import tempfile
import types
from pathlib import Path

import bpy
from mathutils import Vector

REPO = Path(__file__).resolve().parents[1]
PKG = '_fluid_export_frame'
for suffix, path in (('', REPO), ('.blueprint', REPO / 'blueprint'),
                     ('.common', REPO / 'common'), ('.utils', REPO / 'utils')):
    package = types.ModuleType(PKG + suffix)
    package.__path__ = [str(path)]
    sys.modules[package.__name__] = package
base = types.ModuleType(PKG + '.blueprint.node_postprocess_base')
base.SSMTNode_PostProcess_Base = bpy.types.Node
sys.modules[base.__name__] = base


class Settings:
    non_mirror = True

    @classmethod
    def enable_non_mirror_workflow(cls):
        return cls.non_mirror


properties = types.ModuleType(PKG + '.common.global_properties')
properties.GlobalProterties = Settings
sys.modules[properties.__name__] = properties
log = types.ModuleType(PKG + '.utils.log_utils')
log.LOG = types.SimpleNamespace(info=lambda *_: None, warning=lambda *_: None)
sys.modules[log.__name__] = log
fluid = importlib.import_module(PKG + '.blueprint.node_postprocess_fluid')
workflow = importlib.import_module(PKG + '.common.non_mirror_workflow')


class FluidFrameTree(bpy.types.NodeTree):
    bl_idname = 'FluidFrameTree'
    bl_label = 'Fluid export frame'
    bl_icon = 'NODETREE'


for cls in (FluidFrameTree, fluid.SSMTFluidForceField, fluid.SSMTFluidEmitter,
            fluid.SSMTNode_PostProcess_FluidProbe):
    bpy.utils.register_class(cls)
tree = bpy.data.node_groups.new('FluidFrameTest', 'FluidFrameTree')
node = tree.nodes.new('SSMTNode_PostProcess_FluidProbe')

# Real triangles/angles from the affected project, including the tiny third face.
fixtures = [
    ([(.0984670818, -.141938746, 1.21801078),
      (.0977215096, -.142220870, 1.21803200),
      (.0977530703, -.142238230, 1.21782231)], 1.443186, 103.358635),
    ([(-.0977215394, -.142220661, 1.21803248),
      (-.0984671116, -.141938567, 1.21801102),
      (-.0977531150, -.142238051, 1.21782243)], 1.443319, -103.546425),
    ([(-.00022693655, -.0056334324, .872744262),
      (.00000000301, -.0055787228, .872738957),
      (.00000083962, -.0072826557, .872879505)], .277560, -122.127396),
    ([(.0002455083, .0276499279, 1.07786012),
      (.0002475445, .0273123235, 1.07891595),
      (.0005471077, .0284982510, 1.07790112)], 69.803444, 3.225949),
]


def mirror(vector):
    return Vector((-vector.x, vector.y, vector.z)) if Settings.non_mirror else vector


def check_profile(obj, exported, em):
    row = node._emitter_to_gpu(0, em, (1, 1, 1))
    origin, right, up, forward, _ = node._emitter_basis(em)
    a, b, c = [exported.data.vertices[i].co for i in exported.data.polygons[0].vertices]
    tangent = (b - a).normalized()
    normal = (b - a).cross(c - a).normalized()
    bitangent = normal.cross(tangent)

    def restore(coefficients):
        return (tangent * coefficients[0] + bitangent * coefficients[1]
                + normal * coefficients[2]).normalized()

    game_forward, up_hint = restore(row[5:8]), restore(row[9:12])
    game_right = up_hint.cross(game_forward).normalized()
    game_up = game_forward.cross(game_right).normalized()
    game_origin = (a * row[2] + b * row[3] + c * row[4]
                   + game_right * row[13] + game_up * row[14] + game_forward * row[15])
    for label, actual, expected in (
        ('forward', game_forward, mirror(forward)),
        ('up', game_up, mirror(up)),
        ('right', game_right, -mirror(right) if Settings.non_mirror else right),
        ('origin', game_origin, mirror(origin)),
    ):
        assert (actual - expected).length < 2e-5, (obj.name, Settings.non_mirror, label,
                                                   tuple(actual), tuple(expected))
    fields, warnings = node._force_fields_by_slot()
    assert not warnings
    assert fields[0][0]['mirror_u'] == Settings.non_mirror


checks = 0
for enabled in (True, False):
    Settings.non_mirror = enabled
    for marker in (False, True):
        for index, (coords, tilt, yaw) in enumerate(fixtures):
            mesh = bpy.data.meshes.new('FluidSource')
            mesh.from_pydata(coords, [], [(0, 1, 2)])
            obj = bpy.data.objects.new('FluidSource%d' % index, mesh)
            bpy.context.scene.collection.objects.link(obj)
            if marker:
                obj[workflow.NonMirrorWorkflowHelper._WORKFLOW_MARKER_PROP] = True
            exported = obj.copy()
            exported.data = mesh.copy()
            bpy.context.scene.collection.objects.link(exported)
            if enabled:
                workflow.NonMirrorWorkflowHelper.restore_export_objects([exported])
            node.emitters.clear()
            em = node.emitters.add()
            em.object_name = obj.name
            em.poly_index = 0
            em.bx, em.by, em.bz = .57, .31, .12
            em.tilt, em.yaw, em.roll = tilt, yaw, 27
            em.ox, em.oy, em.oz = .012, -.008, .023
            em.force_fields.add()
            check_profile(obj, exported, em)
            checks += 1
            bpy.data.objects.remove(exported, do_unlink=True)
            bpy.data.objects.remove(obj, do_unlink=True)

# Exercise the real export reader and coefficient builder together.
config = types.ModuleType(PKG + '.common.global_config')
config.GlobalConfig = types.SimpleNamespace(logic_name='ZZMI')
sys.modules[config.__name__] = config
Settings.non_mirror = True
mesh = bpy.data.meshes.new('FrameExportSource')
mesh.from_pydata(fixtures[2][0], [], [(0, 1, 2)])
obj = bpy.data.objects.new('LOD0.12345678-3-0.Body', mesh)
bpy.context.scene.collection.objects.link(obj)
node.emitters.clear()
em = node.emitters.add()
em.object_name, em.poly_index = obj.name, 0
em.bx, em.by, em.bz = .57, .31, .12
em.tilt, em.yaw, em.roll = 17, -42, 27
_, _, up, forward, normal = node._emitter_basis(em)
tangent = (mesh.vertices[1].co-mesh.vertices[0].co).normalized()
normal, tangent = mirror(normal), mirror(tangent)
bitangent = normal.cross(tangent)
with tempfile.TemporaryDirectory(prefix='fluid-frame-export-') as directory:
    target = Path(directory)
    rows = [tuple(mirror(v.co))+tuple(normal)+tuple(tangent)+(1.0,) for v in mesh.vertices]
    def write_rows(values):
        (target/'position.buf').write_bytes(b''.join(struct.pack('<10f', *row) for row in values))
    write_rows(rows)
    (target/'index.buf').write_bytes(struct.pack('<3I', 0, 2, 1))
    (target/'model.ini').write_text('''[Resource12345678Position]
stride = 40
filename = position.buf
[ResourceIndex]
format = DXGI_FORMAT_R32_UINT
filename = index.buf
[TextureOverrideBody]
ib = ResourceIndex
; [mesh:LOD0.12345678-3-0.Body_copy]
drawindexed = 3,0,0
''')
    frame = node._build_surface_frames(directory)
    assert frame[3] == 1
    for coefficients, expected in ((frame[:3], mirror(forward)), (frame[4:7], mirror(up))):
        actual = tangent*coefficients[0]+bitangent*coefficients[1]+normal*coefficients[2]
        assert (actual-expected).length < 2e-5, (actual, expected)
    for value in (float('nan'), 1e30):
        invalid = [list(row) for row in rows]
        invalid[0][3:6] = [value]*3
        write_rows(invalid)
        assert node._build_surface_frames(directory) == [0.0]*64

node.emitters.clear()
node.tail_emission = False
node.splash_enable = True
first, second = node.emitters.add(), node.emitters.add()
assert first.tail_emission is False and second.tail_emission is False
assert first.splash_enable is True and second.splash_enable is True
first.tail_emission = True
second.splash_enable = False
assert first.tail_emission is True and second.tail_emission is False
assert first.splash_enable is True and second.splash_enable is False
assert node.tail_emission is False and node.splash_enable is True
text = node._build_main_ini('FluidFrameTest', [])
assert 'data = 3 0 0 0 0 0 0 0' in text
print('FLUID_EXPORT_FRAME_OK ' + json.dumps({'cases': checks, 'artist_angles_preserved': True,
                                           'legacy_features_inherited': True, 'independent_features': True,
                                           'exported_shading_frame': True, 'invalid_frame_fallback': True}))
