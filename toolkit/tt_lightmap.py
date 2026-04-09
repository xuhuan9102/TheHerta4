import bpy

class TT_OT_generate_lightmap_template(bpy.types.Operator):
    bl_idname = "toolkit.tt_generate_lightmap_template"
    bl_label = "生成光照模板"
    bl_description = "为选中的物体创建LightMap和MaterialMap材质模板"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        generate_lightmap = props.lightmap_generate_lightmap
        generate_materialmap = props.lightmap_generate_materialmap
        
        if not generate_lightmap and not generate_materialmap:
            self.report({'ERROR'}, "请至少选择一种材质类型")
            return {'CANCELLED'}
        
        mode = props.lightmap_mode
        processed_count = 0
        
        for obj in selected_objects:
            if mode == 'REPLACE':
                obj.data.materials.clear()
            
            if generate_lightmap:
                lightmap_mat = self._create_lightmap_material(obj.name)
                obj.data.materials.append(lightmap_mat)
            
            if generate_materialmap:
                materialmap_mat = self._create_materialmap_material(obj.name)
                obj.data.materials.append(materialmap_mat)
            
            processed_count += 1
        
        generated_types = []
        if generate_lightmap:
            generated_types.append("LightMap")
        if generate_materialmap:
            generated_types.append("MaterialMap")
        
        self.report({'INFO'}, f"已为 {processed_count} 个物体生成 {' + '.join(generated_types)} 模板")
        return {'FINISHED'}
    
    def _create_lightmap_material(self, obj_name):
        mat_name = f"LightMap_{obj_name}"
        
        if mat_name in bpy.data.materials:
            return bpy.data.materials[mat_name]
        
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        nodes.clear()
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (400, 0)
        
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.label = "LightMap"
        tex_node.location = (-400, 0)
        
        links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        return mat
    
    def _create_materialmap_material(self, obj_name):
        mat_name = f"MaterialMap_{obj_name}"
        
        if mat_name in bpy.data.materials:
            return bpy.data.materials[mat_name]
        
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        nodes.clear()
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (400, 0)
        
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.label = "MaterialMap"
        tex_node.location = (-400, 0)
        
        links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        return mat


tt_lightmap_list = (
    TT_OT_generate_lightmap_template,
)
