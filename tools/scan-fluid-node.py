"""List any fluid-related node in the .blend (node trees, materials, world)."""
import bpy

def scan(trees, label):
    for ng in trees:
        for nd in ng.nodes:
            ident = getattr(nd, "bl_idname", "") or nd.bl_rna.identifier
            if "fluid" in ident.lower() or "Fluid" in nd.name:
                print("HIT %s | tree=%s | node=%s | type=%s | emitters=%s"
                      % (label, ng.name, nd.name, ident,
                         len(getattr(nd, "emitters", []))))

scan(bpy.data.node_groups, "nodetree")
scan([g.node_tree for g in bpy.data.node_groups if getattr(g, "node_tree", None)], "nested")
for mat in bpy.data.materials:
    if mat.node_tree:
        scan([mat.node_tree], "mat:" + mat.name)
for w in bpy.data.worlds:
    if w.node_tree:
        scan([w.node_tree], "world")
print("TOTAL_NODETREES", len(bpy.data.node_groups))
