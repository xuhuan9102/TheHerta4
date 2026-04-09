import bpy
import numpy as np
import os
import tempfile

try:
    from scipy import ndimage
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


class TT_OT_generate_normal_maps(bpy.types.Operator):
    bl_idname = "toolkit.tt_generate_normal_maps"
    bl_label = "生成法线贴图"
    bl_description = "从选中物体的材质中提取高度信息并生成法线贴图"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        if not SCIPY_AVAILABLE:
            self.report({'ERROR'}, "需要安装 scipy 库才能使用法线贴图生成功能")
            return {'CANCELLED'}
        
        props = context.scene.texture_tools_props
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        output_dir = props.output_dir
        if not output_dir:
            output_dir = tempfile.gettempdir()
        
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        strength = props.normal_map_strength
        blur_radius = props.normal_map_blur_radius
        blue_channel_value = props.normal_map_blue_channel_value
        invert = props.normal_map_invert
        create_materials = props.normal_map_create_materials
        material_prefix = props.normal_map_material_prefix
        
        processed_count = 0
        
        for obj in selected_objects:
            if not obj.data.materials:
                continue
            
            for mat_slot in obj.material_slots:
                mat = mat_slot.material
                if not mat or not mat.use_nodes:
                    continue
                
                height_image = None
                height_texture = None
                
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        height_image = node.image
                        height_texture = node
                        break
                
                if height_image is None:
                    continue
                
                normal_image = self._generate_normal_map(
                    height_image, 
                    strength, 
                    blur_radius, 
                    blue_channel_value, 
                    invert,
                    output_dir
                )
                
                if normal_image is None:
                    continue
                
                if create_materials:
                    self._create_normal_material(mat, normal_image, material_prefix)
                
                processed_count += 1
        
        self.report({'INFO'}, f"已生成 {processed_count} 个法线贴图")
        return {'FINISHED'}
    
    def _generate_normal_map(self, height_image, strength, blur_radius, blue_channel_value, invert, output_dir):
        try:
            width = height_image.size[0]
            height = height_image.size[1]
            
            pixels = np.array(height_image.pixels[:])
            pixels = pixels.reshape(-1, 4)
            
            height_data = np.mean(pixels[:, :3], axis=1)
            height_data = height_data.reshape((height, width))
            
            if invert:
                height_data = 1.0 - height_data
            
            if blur_radius > 0:
                height_data = ndimage.gaussian_filter(height_data, sigma=blur_radius)
            
            sobel_x = ndimage.sobel(height_data, axis=1)
            sobel_y = ndimage.sobel(height_data, axis=0)
            
            sobel_x = sobel_x * strength
            sobel_y = sobel_y * strength
            
            normal_x = -sobel_x
            normal_y = -sobel_y
            normal_z = np.ones_like(height_data) * blue_channel_value
            
            length = np.sqrt(normal_x**2 + normal_y**2 + normal_z**2)
            normal_x = normal_x / length
            normal_y = normal_y / length
            normal_z = normal_z / length
            
            normal_x = (normal_x + 1) * 0.5
            normal_y = (normal_y + 1) * 0.5
            normal_z = (normal_z + 1) * 0.5
            
            normal_data = np.zeros((height, width, 4), dtype=np.float32)
            normal_data[:, :, 0] = normal_x
            normal_data[:, :, 1] = normal_y
            normal_data[:, :, 2] = normal_z
            normal_data[:, :, 3] = 1.0
            
            normal_data_flat = normal_data.flatten()
            
            normal_image = bpy.data.images.new(
                name=f"NormalMap_{height_image.name}",
                width=width,
                height=height
            )
            normal_image.pixels = normal_data_flat
            
            output_path = os.path.join(output_dir, f"NormalMap_{height_image.name}.png")
            normal_image.filepath = output_path
            normal_image.file_format = 'PNG'
            normal_image.save()
            
            return normal_image
            
        except Exception as e:
            print(f"生成法线贴图失败: {str(e)}")
            return None
    
    def _create_normal_material(self, original_mat, normal_image, prefix):
        new_mat = bpy.data.materials.new(name=f"{prefix}{original_mat.name}")
        new_mat.use_nodes = True
        
        nodes = new_mat.node_tree.nodes
        links = new_mat.node_tree.links
        
        bsdf = nodes.get("Principled BSDF")
        if not bsdf:
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        
        normal_node = nodes.new('ShaderNodeNormalMap')
        normal_node.location = (-400, 0)
        
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.image = normal_image
        tex_node.location = (-600, 0)
        
        links.new(tex_node.outputs['Color'], normal_node.inputs['Color'])
        links.new(normal_node.outputs['Normal'], bsdf.inputs['Normal'])


class TT_OT_invert_normal_map_green(bpy.types.Operator):
    bl_idname = "toolkit.tt_invert_normal_map_green"
    bl_label = "反转法线贴图绿通道"
    bl_description = "反转法线贴图的绿色通道（用于不同引擎兼容）"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        selected_images = []
        
        for img in bpy.data.images:
            if img.name.startswith("NormalMap"):
                selected_images.append(img)
        
        if not selected_images:
            self.report({'WARNING'}, "未找到法线贴图")
            return {'CANCELLED'}
        
        for img in selected_images:
            pixels = np.array(img.pixels[:])
            pixels = pixels.reshape(-1, 4)
            pixels[:, 1] = 1.0 - pixels[:, 1]
            img.pixels = pixels.flatten()
        
        self.report({'INFO'}, f"已反转 {len(selected_images)} 个法线贴图的绿通道")
        return {'FINISHED'}


class TT_OT_normalize_normal_map(bpy.types.Operator):
    bl_idname = "toolkit.tt_normalize_normal_map"
    bl_label = "标准化法线贴图"
    bl_description = "标准化法线贴图的向量值"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        selected_images = []
        
        for img in bpy.data.images:
            if img.name.startswith("NormalMap"):
                selected_images.append(img)
        
        if not selected_images:
            self.report({'WARNING'}, "未找到法线贴图")
            return {'CANCELLED'}
        
        for img in selected_images:
            pixels = np.array(img.pixels[:])
            pixels = pixels.reshape(-1, 4)
            
            normals = pixels[:, :3] * 2 - 1
            lengths = np.sqrt(np.sum(normals**2, axis=1, keepdims=True))
            lengths = np.maximum(lengths, 1e-6)
            normals = normals / lengths
            pixels[:, :3] = (normals + 1) * 0.5
            
            img.pixels = pixels.flatten()
        
        self.report({'INFO'}, f"已标准化 {len(selected_images)} 个法线贴图")
        return {'FINISHED'}


tt_normal_map_list = (
    TT_OT_generate_normal_maps,
    TT_OT_invert_normal_map_green,
    TT_OT_normalize_normal_map,
)
