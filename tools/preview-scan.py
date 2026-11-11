"""3x3 scan (radius coefficient x threshold) — pure geometry, no rendering.

Run: blender.exe -b -P tools/preview-scan.py -- <npz>
Prints one row per combination:
  scale threshold | mid_halfwidth_m ratio_vs_game polys comps
"""
import sys
from collections import deque

import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:]
npz = argv[0]
data = np.load(npz)
pos = data["positions"].astype(np.float32)
radius = float(data["radius"])
# 游戏口径：可见半径 = r*1.6*slot_width(1)*visual_scale(4)
game_r = radius * 1.6


def build(scale, threshold):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    me = bpy.data.meshes.new("pts")
    me.from_pydata([tuple(v) for v in pos], [], [])
    me.update()
    ob = bpy.data.objects.new("pts", me)
    bpy.context.collection.objects.link(ob)

    ng = bpy.data.node_groups.new("pg", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi = ng.nodes.new("NodeGroupInput")
    go = ng.nodes.new("NodeGroupOutput")
    mtp = ng.nodes.new("GeometryNodeMeshToPoints")
    p2v = ng.nodes.new("GeometryNodePointsToVolume")
    p2v.inputs["Radius"].default_value = max(game_r * scale, 1e-4)
    p2v.inputs["Density"].default_value = 1.0
    v2m = ng.nodes.new("GeometryNodeVolumeToMesh")
    v2m.inputs["Threshold"].default_value = threshold
    ng.links.new(gi.outputs[0], mtp.inputs["Mesh"])
    ng.links.new(mtp.outputs[0], p2v.inputs["Points"])
    ng.links.new(p2v.outputs[0], v2m.inputs["Volume"])
    ng.links.new(v2m.outputs[0], go.inputs[0])
    ob.modifiers.new("pg", "NODES").node_group = ng

    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    m = ev.to_mesh()
    co = np.array([v.co[:] for v in m.vertices]) if len(m.vertices) else np.zeros((0, 3))
    polys = len(m.polygons)
    ev.to_mesh_clear()
    return co, polys


def metrics(co):
    if not len(co):
        return 0.0, 0, 0
    zmin, zmax = co[:, 2].min(), co[:, 2].max()
    mid = co[(co[:, 2] > zmin + 0.45 * (zmax - zmin)) & (co[:, 2] < zmin + 0.55 * (zmax - zmin))]
    half = (mid[:, 0].max() - mid[:, 0].min()) / 2 if len(mid) else 0.0
    # side-view occupancy on 1 mm grid (x,z)
    g = 0.001
    ix = ((co[:, 0] - co[:, 0].min()) / g).astype(int)
    iz = ((co[:, 2] - co[:, 2].min()) / g).astype(int)
    grid = np.zeros((iz.max() + 1, ix.max() + 1), dtype=bool)
    grid[iz, ix] = True
    h, w = grid.shape
    seen = np.zeros_like(grid)
    comps = 0
    for y in range(h):
        for x in range(w):
            if grid[y, x] and not seen[y, x]:
                q = deque([(y, x)])
                seen[y, x] = True
                n = 0
                while q:
                    cy, cx = q.popleft()
                    n += 1
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and grid[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            q.append((ny, nx))
                if n > 30:
                    comps += 1
    return half, comps, len(co)


print("game_r=%.5f  particles=%d" % (game_r, len(pos)))
print("%5s %9s %12s %8s %8s %7s" % ("scale", "thresh", "mid_half_m", "vs_game", "polys", "comps"))
for scale in (1.0,):
    for th in (0.3, 0.5, 1.0, 2.0, 4.0):
        co, polys = build(scale, th)
        half, comps, nverts = metrics(co)
        print("%5.1f %9.2f %12.5f %8.2f %8d %7d" % (scale, th, half, half / game_r if game_r else 0, polys, comps))
