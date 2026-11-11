"""Second pass: how many instances are actually drawn, and what the per-instance mesh is."""
import json
import bpy

out = {}
obj = bpy.data.objects.get("SSMT_FluidParticles")
deps = bpy.context.evaluated_depsgraph_get()

if obj:
    ev = obj.evaluated_get(deps)
    me = ev.to_mesh()
    out["evaluated"] = {"verts": len(me.vertices), "polys": len(me.polygons)}
    # 找 GN 树里 Ico Sphere 的顶点数
    ng = None
    for m in obj.modifiers:
        if m.type == "NODES" and m.node_group:
            ng = m.node_group
            break
    per = None
    if ng:
        for n in ng.nodes:
            if n.bl_idname in ("GeometryNodeMeshIcoSphere", "GeometryNodeMeshUVSphere"):
                per = {"node": n.name, "bl_idname": n.bl_idname,
                       "subdivisions": getattr(n.inputs.get("Subdivisions"), "default_value", None)
                       if n.inputs.get("Subdivisions") else None,
                       "radius": getattr(n.inputs.get("Radius"), "default_value", None)
                       if n.inputs.get("Radius") else None}
        out["ico"] = per
    # 顶点空间分布：数一数多少个“团”（按 z 分箱、统计聚集）
    import collections
    zs = [v.co.z for v in me.vertices]
    out["z_range"] = [round(min(zs), 3), round(max(zs), 3)]
    far = sum(1 for z in zs if abs(z) > 1e5)
    out["parked_vertices"] = far
    ev.to_mesh_clear()

print("JSONLINE2 " + json.dumps(out, ensure_ascii=False))
