"""Headless Blender: load a preview npz, build spheres, render, and report metrics.

Run:  blender.exe -b -P tools/preview-render.py -- <npz> <out.png> <mode>
mode: base  = PointsToVolume radius = 1.6r*1.0 (current bake口径)
      grow2 = radius *2
      dense = same radius, density x3 (threshold lowered)
"""
import sys
import numpy as np
import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
npz_path, out_png, mode = argv[0], argv[1], (argv[2] if len(argv) > 2 else "base")

data = np.load(npz_path)
pos = data["positions"].astype(np.float32)
radius = float(data["radius"])
disp = radius * 1.6
if mode == "grow2":
    disp *= 2.0

bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("pts")
mesh.from_pydata([tuple(v) for v in pos], [], [])
mesh.update()
obj = bpy.data.objects.new("pts", mesh)
bpy.context.collection.objects.link(obj)

ng = bpy.data.node_groups.new("pg", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi = ng.nodes.new("NodeGroupInput")
go = ng.nodes.new("NodeGroupOutput")
mtp = ng.nodes.new("GeometryNodeMeshToPoints")
p2v = ng.nodes.new("GeometryNodePointsToVolume")
p2v.inputs["Radius"].default_value = max(disp, 1e-4)
p2v.inputs["Density"].default_value = 3.0 if mode == "dense" else 1.0
v2m = ng.nodes.new("GeometryNodeVolumeToMesh")
v2m.inputs["Threshold"].default_value = 0.1
ng.links.new(gi.outputs[0], mtp.inputs["Mesh"])
ng.links.new(mtp.outputs[0], p2v.inputs["Points"])
ng.links.new(p2v.outputs[0], v2m.inputs["Volume"])
ng.links.new(v2m.outputs[0], go.inputs[0])
mod = obj.modifiers.new("pg", "NODES")
mod.node_group = ng

cam_data = bpy.data.cameras.new("cam")
cam_data.angle = np.radians(40.0)
cam = bpy.data.objects.new("cam", cam_data)
cam.location = (0.0, -3.0, 0.0)
cam.rotation_euler = (np.radians(90), 0, 0)
bpy.context.collection.objects.link(cam)
bpy.context.scene.camera = cam

sun_data = bpy.data.lights.new('sun', 'SUN')
sun_data.energy = 5.0
sun = bpy.data.objects.new('sun', sun_data)
sun.rotation_euler = (np.radians(60), 0, np.radians(30))
bpy.context.collection.objects.link(sun)

bpy.context.scene.render.resolution_x = 1920
bpy.context.scene.render.resolution_y = 1080
bpy.context.scene.render.filepath = out_png
bpy.context.scene.render.engine = "CYCLES"
bpy.context.scene.cycles.samples = 8
bpy.context.scene.cycles.device = "CPU"
bpy.ops.render.render(write_still=True)

# mesh stats after modifiers
dg = bpy.context.evaluated_depsgraph_get()
ev = obj.evaluated_get(dg)
me = ev.to_mesh()
print("STATS verts=%d polys=%d" % (len(me.vertices), len(me.polygons)))
if len(me.vertices):
    co = np.array([v.co[:] for v in me.vertices])
    ext = co.max(axis=0) - co.min(axis=0)
    print("STATS extent=%.4f %.4f %.4f" % tuple(ext))
ev.to_mesh_clear()
print("DONE", out_png)
