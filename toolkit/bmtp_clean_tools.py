import bpy
import bmesh


class BMTP_OT_ClearVertexCreases(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clear_vertex_creases"
    bl_label = "清理顶点折痕"
    bl_description = "清理选中物体的顶点折痕"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        total_objects = 0
        total_vertices = 0
        
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not mesh_objects:
            self.report({'INFO'}, "没有选中的网格物体")
            return {'FINISHED'}
        
        original_active = context.view_layer.objects.active
        original_mode = original_active.mode if original_active else 'OBJECT'
        
        for obj in mesh_objects:
            if obj.mode != 'OBJECT':
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
            
            vertex_count = 0
            
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            
            crease_vert_layer = bm.verts.layers.float.get('crease')
            if crease_vert_layer is not None:
                for vertex in bm.verts:
                    if vertex[crease_vert_layer] > 0.0:
                        vertex[crease_vert_layer] = 0.0
                        vertex_count += 1
            
            bm.to_mesh(obj.data)
            bm.free()
            
            total_objects += 1
            total_vertices += vertex_count
        
        if original_active and original_active.mode != original_mode:
            bpy.context.view_layer.objects.active = original_active
            bpy.ops.object.mode_set(mode=original_mode)
        
        if total_vertices > 0:
            self.report({'INFO'}, 
                       f"已清理 {total_objects} 个物体的顶点折痕: {total_vertices} 个顶点")
        else:
            self.report({'INFO'}, "选中的物体中没有找到需要清理的顶点折痕")
            
        return {'FINISHED'}


class BMTP_OT_ClearEdgeCreases(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clear_edge_creases"
    bl_label = "清理边线折痕"
    bl_description = "清理选中物体的边线折痕"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        total_objects = 0
        total_edges = 0
        
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not mesh_objects:
            self.report({'INFO'}, "没有选中的网格物体")
            return {'FINISHED'}
        
        original_active = context.view_layer.objects.active
        original_mode = original_active.mode if original_active else 'OBJECT'
        
        for obj in mesh_objects:
            if obj.mode != 'OBJECT':
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
            
            edge_count = 0
            
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            
            crease_edge_layer = bm.edges.layers.float.get('crease')
            if crease_edge_layer is not None:
                for edge in bm.edges:
                    if edge[crease_edge_layer] > 0.0:
                        edge[crease_edge_layer] = 0.0
                        edge_count += 1
            
            bm.to_mesh(obj.data)
            bm.free()
            
            total_objects += 1
            total_edges += edge_count
        
        if original_active and original_active.mode != original_mode:
            bpy.context.view_layer.objects.active = original_active
            bpy.ops.object.mode_set(mode=original_mode)
        
        if total_edges > 0:
            self.report({'INFO'}, 
                       f"已清理 {total_objects} 个物体的边线折痕: {total_edges} 条边线")
        else:
            self.report({'INFO'}, "选中的物体中没有找到需要清理的边线折痕")
            
        return {'FINISHED'}


class BMTP_OT_ClearSharpEdges(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clear_sharp_edges"
    bl_label = "清理锐边标记"
    bl_description = "清理选中物体的锐边标记"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        total_objects = 0
        total_edges = 0
        
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not mesh_objects:
            self.report({'INFO'}, "没有选中的网格物体")
            return {'FINISHED'}
        
        original_active = context.view_layer.objects.active
        original_mode = original_active.mode if original_active else 'OBJECT'
        
        for obj in mesh_objects:
            if obj.mode != 'OBJECT':
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
            
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            
            edge_count = 0
            for edge in bm.edges:
                if not edge.smooth:
                    edge.smooth = True
                    edge_count += 1
            
            bm.to_mesh(obj.data)
            bm.free()
            
            total_objects += 1
            total_edges += edge_count
        
        if original_active and original_active.mode != original_mode:
            bpy.context.view_layer.objects.active = original_active
            bpy.ops.object.mode_set(mode=original_mode)
        
        if total_edges > 0:
            self.report({'INFO'}, 
                       f"已清理 {total_objects} 个物体的锐边标记: {total_edges} 条边线")
        else:
            self.report({'INFO'}, "选中的物体中没有找到需要清理的锐边标记")
            
        return {'FINISHED'}


class BMTP_OT_ClearSeams(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_clear_seams"
    bl_label = "清理UV接缝标记"
    bl_description = "清理选中物体的UV接缝标记"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        total_objects = 0
        total_edges = 0
        
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not mesh_objects:
            self.report({'INFO'}, "没有选中的网格物体")
            return {'FINISHED'}
        
        original_active = context.view_layer.objects.active
        original_mode = original_active.mode if original_active else 'OBJECT'
        
        for obj in mesh_objects:
            if obj.mode != 'OBJECT':
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
            
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            
            edge_count = 0
            for edge in bm.edges:
                if edge.seam:
                    edge.seam = False
                    edge_count += 1
            
            bm.to_mesh(obj.data)
            bm.free()
            
            total_objects += 1
            total_edges += edge_count
        
        if original_active and original_active.mode != original_mode:
            bpy.context.view_layer.objects.active = original_active
            bpy.ops.object.mode_set(mode=original_mode)
        
        if total_edges > 0:
            self.report({'INFO'}, 
                       f"已清理 {total_objects} 个物体的UV接缝标记: {total_edges} 条边线")
        else:
            self.report({'INFO'}, "选中的物体中没有找到需要清理的UV接缝标记")
            
        return {'FINISHED'}


class BMTP_OT_BatchClearAll(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_batch_clear_all"
    bl_label = "批量清理所有属性"
    bl_description = "一次性清理选中物体的所有属性（折痕、锐边、接缝、自定义拆边法向等）"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not mesh_objects:
            self.report({'INFO'}, "没有选中的网格物体")
            return {'FINISHED'}
        
        original_active = context.view_layer.objects.active
        original_mode = original_active.mode if original_active else 'OBJECT'
        
        total_objects = 0
        total_vertices = 0
        total_edges = 0
        total_seams = 0
        total_sharps = 0
        total_normals = 0
        
        for obj in mesh_objects:
            if obj.mode != 'OBJECT':
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
            
            mesh = obj.data
            
            vertex_count = 0
            edge_crease_count = 0
            edge_sharp_count = 0
            seam_count = 0
            normal_cleared = False
            
            bm = bmesh.new()
            bm.from_mesh(mesh)
            
            crease_vert_layer = bm.verts.layers.float.get('crease')
            if crease_vert_layer is not None:
                for vertex in bm.verts:
                    if vertex[crease_vert_layer] > 0.0:
                        vertex[crease_vert_layer] = 0.0
                        vertex_count += 1
            
            crease_edge_layer = bm.edges.layers.float.get('crease')
            for edge in bm.edges:
                if crease_edge_layer is not None and edge[crease_edge_layer] > 0.0:
                    edge[crease_edge_layer] = 0.0
                    edge_crease_count += 1
                
                if not edge.smooth:
                    edge.smooth = True
                    edge_sharp_count += 1
                
                if edge.seam:
                    edge.seam = False
                    seam_count += 1
            
            bm.to_mesh(mesh)
            bm.free()
            
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            
            try:
                bpy.ops.mesh.customdata_custom_splitnormals_clear()
                normal_cleared = True
            except AttributeError:
                try:
                    bpy.ops.mesh.customdata_custom_splitnormals_delete()
                    normal_cleared = True
                except AttributeError:
                    pass
            
            bpy.ops.object.mode_set(mode='OBJECT')
            
            total_objects += 1
            total_vertices += vertex_count
            total_edges += edge_crease_count
            total_seams += seam_count
            total_sharps += edge_sharp_count
            if normal_cleared:
                total_normals += 1
        
        if original_active and original_active.mode != original_mode:
            bpy.context.view_layer.objects.active = original_active
            bpy.ops.object.mode_set(mode=original_mode)
        
        if total_vertices > 0 or total_edges > 0 or total_seams > 0 or total_sharps > 0 or total_normals > 0:
            message = f"已批量清理 {total_objects} 个物体的属性: "
            parts = []
            if total_vertices > 0:
                parts.append(f"{total_vertices} 个顶点折痕")
            if total_edges > 0:
                parts.append(f"{total_edges} 条边线折痕")
            if total_sharps > 0:
                parts.append(f"{total_sharps} 条锐边")
            if total_seams > 0:
                parts.append(f"{total_seams} 条接缝")
            if total_normals > 0:
                parts.append(f"{total_normals} 个自定义拆边法向")
            
            message += ", ".join(parts)
            self.report({'INFO'}, message)
        else:
            self.report({'INFO'}, "选中的物体中没有找到需要清理的属性")
            
        return {'FINISHED'}


bmtp_clean_tools_list = (
    BMTP_OT_ClearVertexCreases,
    BMTP_OT_ClearEdgeCreases,
    BMTP_OT_ClearSharpEdges,
    BMTP_OT_ClearSeams,
    BMTP_OT_BatchClearAll,
)
