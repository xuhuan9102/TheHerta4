import bpy
import numpy as np
import os

class TT_OT_extract_alpha_channel(bpy.types.Operator):
    bl_idname = "toolkit.tt_extract_alpha_channel"
    bl_label = "提取透明通道"
    bl_description = "从选中物体的材质中提取贴图的Alpha通道，并将其存储到顶点色或UV通道中"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        allow_semitransparency = props.alpha_extract_allow_semitransparency
        threshold = props.alpha_extract_threshold
        create_materials = props.alpha_extract_create_materials
        material_prefix = props.alpha_extract_material_prefix
        
        processed_count = 0
        
        for obj in selected_objects:
            if not obj.data.materials:
                continue
            
            for mat_slot in obj.material_slots:
                mat = mat_slot.material
                if not mat or not mat.use_nodes:
                    continue
                
                alpha_texture = None
                alpha_image = None
                
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        img = node.image
                        if img.channels == 4:
                            alpha_texture = node
                            alpha_image = img
                            break
                
                if alpha_image is None:
                    continue
                
                pixels = np.array(alpha_image.pixels[:])
                pixels = pixels.reshape(-1, 4)
                alpha_channel = pixels[:, 3].copy()
                
                if not allow_semitransparency:
                    alpha_channel = np.where(alpha_channel > threshold, 1.0, 0.0)
                
                mesh = obj.data
                
                if not mesh.color_attributes:
                    color_attr = mesh.color_attributes.new(name="Alpha", type='BYTE_COLOR', domain='CORNER')
                else:
                    color_attr = mesh.color_attributes.active_color
                
                num_loops = len(mesh.loops)
                alpha_data = np.zeros(num_loops * 4, dtype=np.float32)
                alpha_data[0::4] = 1.0
                alpha_data[1::4] = 1.0
                alpha_data[2::4] = 1.0
                alpha_data[3::4] = np.tile(alpha_channel, num_loops // len(alpha_channel) + 1)[:num_loops]
                
                color_attr.data.foreach_set("color", alpha_data)
                mesh.update()
                
                if create_materials:
                    new_mat = bpy.data.materials.new(name=f"{material_prefix}{mat.name}")
                    new_mat.use_nodes = True
                    new_mat.blend_method = 'CLIP'
                    new_mat.shadow_method = 'CLIP'
                    
                    nodes = new_mat.node_tree.nodes
                    links = new_mat.node_tree.links
                    
                    bsdf = nodes.get("Principled BSDF")
                    if not bsdf:
                        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
                    
                    tex_node = nodes.new('ShaderNodeTexImage')
                    tex_node.image = alpha_image
                    tex_node.location = (-400, 0)
                    
                    links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
                    
                    mat_slot.material = new_mat
                
                processed_count += 1
        
        self.report({'INFO'}, f"已处理 {processed_count} 个材质的透明通道")
        return {'FINISHED'}


tt_alpha_extract_list = (
    TT_OT_extract_alpha_channel,
)
