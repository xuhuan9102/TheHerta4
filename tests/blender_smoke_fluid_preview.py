"""Run with Blender --background --factory-startup --python this_file."""
import importlib
import json
import sys
import types
import tempfile
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

REPO = Path(__file__).resolve().parents[1]
PKG = '_fluid_smoke'
for suffix, path in (('', REPO), ('.blueprint', REPO / 'blueprint'),
                     ('.common', REPO / 'common')):
    package = types.ModuleType(PKG + suffix)
    package.__path__ = [str(path)]
    sys.modules[package.__name__] = package
base = types.ModuleType(PKG + '.blueprint.node_postprocess_base')
base.SSMTNode_PostProcess_Base = bpy.types.Node
sys.modules[base.__name__] = base
fluid = importlib.import_module(PKG + '.blueprint.node_postprocess_fluid')


class FluidSmokeTree(bpy.types.NodeTree):
    bl_idname = 'FluidSmokeTree'
    bl_label = 'Fluid smoke'
    bl_icon = 'NODETREE'


for cls in (FluidSmokeTree, fluid.SSMTFluidForceField, fluid.SSMTFluidEmitter,
            fluid.SSMTNode_PostProcess_FluidProbe, fluid.SSMT_OT_FluidForceFieldAdd,
            fluid.SSMT_OT_FluidForceFieldRemove):
    bpy.utils.register_class(cls)

mesh = bpy.data.meshes.new('FluidSmokeSource')
mesh.from_pydata([(0, 0, 1), (1, 0, 1), (0, 1, 1)], [], [(0, 1, 2)])
obj = bpy.data.objects.new('FluidSmokeSource', mesh)
bpy.context.scene.collection.objects.link(obj)
tree = bpy.data.node_groups.new('FluidSmoke', 'FluidSmokeTree')
node = tree.nodes.new('SSMTNode_PostProcess_FluidProbe')
em = node.emitters.add()
em.object_name = obj.name
em.poly_index = 0
em.bx = em.by = em.bz = 1 / 3
em.tilt = 80
em.roll = 35
em.ox = 0.12
em.speed = 1.8
em.lifetime = 2
context = types.SimpleNamespace(node=node, scene=bpy.context.scene)
fluid._active_fluid_node = lambda context: node

# Add must create the first field's extent without requiring Generate Preview.
assert bpy.ops.ssmt.fluid_force_field_add() == {'FINISHED'}
marker = bpy.data.objects.get('FluidFF0_0')
assert marker is not None and marker.name in bpy.context.scene.objects
assert marker.parent == obj and marker.hide_render
field = em.force_fields[0]
field.offset_u = 0.15
field.offset_v = -0.08
field.radius = 0.25
assert len(bpy.data.collections[fluid.FORCE_FIELD_COLLECTION].objects) == 1

# Both marker kinds and the simulator must use the same emitter frame.
fluid.build_preview_markers(node, bpy.context.scene)
basis = node._emitter_basis(em)
origin, right, up, forward, _ = basis
ball = bpy.data.objects['FluidEm0_ball']
center = sum((v.co for v in ball.data.vertices), Vector()) / len(ball.data.vertices)
assert (center - origin).length < 1e-5, (center, origin)
sim, bake, kwargs, finish = fluid._sim_setup(node, frames=2, capacity=8, solid=False)
cfg = kwargs['emitters'][0]
assert (Vector(cfg['up']) - up).length < 1e-6
assert (Vector(cfg['direction']) - forward).length < 1e-6
assert cfg['speed_override'] == em.speed and cfg['lifetime'] == em.lifetime
ff = kwargs['force_fields'][0]
expected = origin + right * field.offset_u + up * field.offset_v + forward * field.offset_w
assert (Vector(ff['center']) - expected).length < 1e-6
frames = sim.simulate(**kwargs)
assert len(frames) == 2 and any(len(frame[0]) for frame in frames)

field.enabled = False
assert bpy.data.objects.get('FluidFF0_0') is None
field.enabled = True
assert bpy.data.objects.get('FluidFF0_0') is not None
assert bpy.ops.ssmt.fluid_force_field_remove() == {'FINISHED'}
assert bpy.data.objects.get('FluidFF0_0') is None
with tempfile.TemporaryDirectory(prefix='fluid-smoke-') as directory:
    cache = str(Path(directory) / 'fluid.npz')
    samples = [(np.array([[0, 0, 1], [0.1, 0, 1]], dtype=np.float32),
                np.array([0, 1]), np.array([0.1, 0.1])),
               (np.empty((0, 3)), np.empty(0), np.empty(0))]
    sim.save_npz(cache, samples, 4)
    bake.bake(cache, solid=False, emitter_radii={0: 0.003, 1: 0.006})
    particles = bpy.data.objects['SSMT_FluidParticles']
    group = particles.modifiers['FluidParticles'].node_group
    output = next(n for n in group.nodes if n.type == 'GROUP_OUTPUT')
    instances = next(n for n in group.nodes if n.bl_idname == 'GeometryNodeInstanceOnPoints')
    realize = group.nodes.new('GeometryNodeRealizeInstances')
    group.links.new(instances.outputs['Instances'], realize.inputs[0])
    group.links.new(realize.outputs[0], output.inputs[0])
    for frame, expected_vertices in ((1, 24), (2, 0)):
        bpy.context.scene.frame_set(frame)
        evaluated = particles.evaluated_get(bpy.context.evaluated_depsgraph_get())
        result = evaluated.to_mesh()
        assert len(result.vertices) == expected_vertices, (frame, len(result.vertices))
        if frame == 1:
            left = [v.co.x for v in result.vertices if v.co.x < 0.05]
            right = [v.co.x for v in result.vertices if v.co.x > 0.05]
            assert abs((max(right) - min(right)) / (max(left) - min(left)) - 2) < 1e-4
        evaluated.to_mesh_clear()
    bake.bake(cache, solid=True, voxel=0.0008,
              emitter_radii={0: 0.003, 1: 0.006})
    particles = bpy.data.objects['SSMT_FluidParticles']
    for frame in (1, 2):
        bpy.context.scene.frame_set(frame)
        evaluated = particles.evaluated_get(bpy.context.evaluated_depsgraph_get())
        result = evaluated.to_mesh()
        assert bool(len(result.polygons)) == (frame == 1), (frame, len(result.polygons))
        evaluated.to_mesh_clear()
print('FLUID_SMOKE_OK ' + json.dumps(dict(markers=True, force_updates=True,
      emitter_basis=True, frames=len(frames), baked_live_counts=True, baked_widths=True)))
