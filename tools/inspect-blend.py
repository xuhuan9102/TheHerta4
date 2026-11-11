"""Inspect Untitled.blend headlessly: objects, camera, GN instance counts, materials. Read-only."""
import json
import bpy

out = {}
out["blend"] = bpy.data.filepath

objs = []
for o in bpy.data.objects:
    d = {
        "name": o.name,
        "type": o.type,
        "hide_render": bool(o.hide_render),
        "hide_viewport": bool(o.hide_viewport),
        "dims": [round(float(v), 4) for v in o.dimensions],
    }
    if o.type == "MESH" and o.modifiers:
        d["modifiers"] = [(m.name, m.type) for m in o.modifiers]
    objs.append(d)
out["objects"] = objs

cam = bpy.context.scene.camera
if cam and cam.data:
    out["camera"] = {"name": cam.name, "clip_start": float(cam.data.clip_start),
                     "clip_end": float(cam.data.clip_end), "lens": float(cam.data.lens)}

deps = bpy.context.evaluated_depsgraph_get()
inst = []
for o in bpy.data.objects:
    if o.type != "MESH":
        continue
    try:
        ev = o.evaluated_get(deps)
        me = ev.to_mesh()
        n_inst = len(ev.instance_collection.all_objects) if ev.instance_collection else 0
        # Geometry Nodes 实例在 evaluated mesh 里体现为 instances
        try:
            n_geo_inst = len(me.instances) if hasattr(me, "instances") else None
        except Exception:
            n_geo_inst = None
        inst.append({"name": o.name, "verts": len(me.vertices), "polys": len(me.polygons),
                     "mesh_instances": n_geo_inst,
                     "collection_instances": n_inst})
        ev.to_mesh_clear()
    except Exception as exc:
        inst.append({"name": o.name, "error": str(exc)})
out["instances"] = inst

cams = []
for m in bpy.data.materials:
    try:
        cams.append({"name": m.name,
                     "blend_method": getattr(m, "blend_method", None),
                     "surface_render_method": getattr(m, "surface_render_method", None)})
    except Exception as exc:
        cams.append({"name": m.name, "error": str(exc)})
out["materials"] = cams

# GN 树里的 Instance on Points 输入
gn = []
for ng in bpy.data.node_groups:
    for n in ng.nodes:
        if n.bl_idname == "GeometryNodeInstanceOnPoints":
            entry = {"group": ng.name, "node": n.name, "inputs": {}}
            for sock in n.inputs:
                try:
                    entry["inputs"][sock.name] = (sock.default_value
                                                  if not hasattr(sock.default_value, "__len__")
                                                  else list(sock.default_value))
                except Exception:
                    entry["inputs"][sock.name] = "<linked>"
            gn.append(entry)
out["instance_on_points"] = gn

print("JSONLINE " + json.dumps(out, ensure_ascii=False))
