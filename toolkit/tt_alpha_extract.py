import bpy
import numpy as np
import os
from pathlib import Path
from collections import defaultdict


class TT_OT_extract_alpha_channel(bpy.types.Operator):
    bl_idname = "toolkit.tt_extract_alpha_channel"
    bl_label = "提取透明通道"
    bl_description = "从选中物体的材质中提取贴图的Alpha通道，保存为独立贴图并存储到顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    def _find_base_color_texture(self, material):
        if not material or not material.use_nodes:
            return None
        output_node = next((n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
        if not output_node:
            return None
        nodes_to_visit = {link.from_node for inp in output_node.inputs if inp.is_linked for link in inp.links}
        visited_nodes = {output_node}
        fallback_image = None
        while nodes_to_visit:
            current_node = nodes_to_visit.pop()
            if current_node in visited_nodes:
                continue
            visited_nodes.add(current_node)
            if 'Base Color' in current_node.inputs:
                base_color_input = current_node.inputs['Base Color']
                if base_color_input.is_linked:
                    from_node = base_color_input.links[0].from_node
                    if from_node.type == 'TEX_IMAGE' and from_node.image:
                        return from_node.image
            if current_node.type == 'TEX_IMAGE' and current_node.image and fallback_image is None:
                fallback_image = current_node.image
            for inp in current_node.inputs:
                if inp.is_linked:
                    for link in inp.links:
                        if link.from_node not in visited_nodes:
                            nodes_to_visit.add(link.from_node)
        return fallback_image

    def _create_alpha_material(self, material_name, alpha_map_image_path):
        mat = bpy.data.materials.get(material_name)
        created_new = False
        if not mat:
            mat = bpy.data.materials.new(name=material_name)
            created_new = True
        mat.use_nodes = True
        node_tree = mat.node_tree
        node_tree.nodes.clear()

        tex_node = node_tree.nodes.new('ShaderNodeTexImage')
        tex_node.location = (-400, 0)
        image = bpy.data.images.load(alpha_map_image_path, check_existing=True)
        tex_node.image = image
        image.colorspace_settings.name = 'sRGB'

        transparent_bsdf = node_tree.nodes.new('ShaderNodeBsdfTransparent')
        transparent_bsdf.location = (-200, 100)

        principled_bsdf = node_tree.nodes.new('ShaderNodeBsdfPrincipled')
        principled_bsdf.location = (-200, -100)

        mix_shader = node_tree.nodes.new('ShaderNodeMixShader')
        mix_shader.location = (0, 0)

        output_node = node_tree.nodes.new('ShaderNodeOutputMaterial')
        output_node.location = (200, 0)

        node_tree.links.new(tex_node.outputs['Color'], principled_bsdf.inputs['Base Color'])
        node_tree.links.new(tex_node.outputs['Alpha'], mix_shader.inputs['Fac'])
        node_tree.links.new(transparent_bsdf.outputs['BSDF'], mix_shader.inputs[1])
        node_tree.links.new(principled_bsdf.outputs['BSDF'], mix_shader.inputs[2])
        node_tree.links.new(mix_shader.outputs['Shader'], output_node.inputs['Surface'])

        return mat, created_new

    def _write_alpha_to_vertex_colors(self, obj, alpha_channel, width, height):
        mesh = obj.data
        uv_layer = mesh.uv_layers.active
        if not uv_layer:
            return False

        alpha_attr = mesh.color_attributes.get("Alpha")
        if not alpha_attr:
            alpha_attr = mesh.color_attributes.new(name="Alpha", type='BYTE_COLOR', domain='CORNER')

        num_loops = len(mesh.loops)
        uvs = np.zeros(num_loops * 2, dtype=np.float32)
        uv_layer.data.foreach_get('uv', uvs)
        uvs = uvs.reshape(-1, 2)

        x_coords = np.clip((uvs[:, 0] * width).astype(int), 0, width - 1)
        y_coords = np.clip((uvs[:, 1] * height).astype(int), 0, height - 1)

        pixel_indices = y_coords * width + x_coords
        flat_alpha = alpha_channel.flatten()
        sampled_alpha = flat_alpha[pixel_indices]

        alpha_data = np.zeros(num_loops * 4, dtype=np.float32)
        alpha_data[0::4] = 1.0
        alpha_data[1::4] = 1.0
        alpha_data[2::4] = 1.0
        alpha_data[3::4] = sampled_alpha

        alpha_attr.data.foreach_set("color", alpha_data)
        mesh.update()
        return True

    def execute(self, context):
        props = context.scene.texture_tools_props
        if not props.output_dir:
            self.report({'ERROR'}, "请先设置输出目录")
            return {'CANCELLED'}
        output_dir = bpy.path.abspath(props.output_dir)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}

        material_map = defaultdict(list)
        for obj in selected_objects:
            if obj.material_slots and obj.material_slots[0].material:
                material_map[obj.material_slots[0].material].append(obj)

        if not material_map:
            self.report({'ERROR'}, "选中的物体上没有找到有效材质")
            return {'CANCELLED'}

        allow_semitransparency = props.alpha_extract_allow_semitransparency
        threshold = props.alpha_extract_threshold
        create_materials = props.alpha_extract_create_materials
        material_prefix = props.alpha_extract_material_prefix

        processed_textures = {}
        created_materials_count = 0

        for original_material, objects in material_map.items():
            base_texture = self._find_base_color_texture(original_material)
            if not base_texture:
                continue

            if base_texture.name in processed_textures:
                tex_info = processed_textures[base_texture.name]
                alpha_map_path = tex_info['path']
                alpha_channel = tex_info['alpha']
                width = tex_info['width']
                height = tex_info['height']
            else:
                try:
                    width, height = base_texture.size
                    pixels_np = np.empty(width * height * 4, dtype=np.float32)
                    base_texture.pixels.foreach_get(pixels_np)
                    pixels_np = pixels_np.reshape((height, width, 4))
                    alpha_channel = pixels_np[..., 3].copy()

                    if not allow_semitransparency:
                        alpha_channel = np.where(alpha_channel > threshold, 1.0, 0.0)

                    if np.all(alpha_channel == 1.0):
                        self.report({'INFO'}, f"纹理 {base_texture.name} 的透明通道为全白，跳过")
                        continue

                    alpha_map_pixels = np.zeros((height, width, 4), dtype=np.float32)
                    alpha_map_pixels[..., 0] = alpha_channel
                    alpha_map_pixels[..., 1] = alpha_channel
                    alpha_map_pixels[..., 2] = alpha_channel
                    alpha_map_pixels[..., 3] = alpha_channel

                    safe_name = "".join(c for c in os.path.splitext(base_texture.name)[0] if c.isalnum() or c in ('-', '_', '.'))
                    output_filename = f"FXMap_{safe_name}.png"
                    output_path = os.path.join(output_dir, output_filename)

                    alpha_image = bpy.data.images.new(name=output_filename, width=width, height=height, alpha=True)
                    alpha_image.pixels.foreach_set(alpha_map_pixels.flatten())
                    alpha_image.filepath_raw = output_path
                    alpha_image.file_format = 'PNG'
                    alpha_image.save()
                    bpy.data.images.remove(alpha_image)

                    alpha_map_path = output_path
                    processed_textures[base_texture.name] = {
                        'path': alpha_map_path,
                        'alpha': alpha_channel,
                        'width': width,
                        'height': height
                    }
                except Exception:
                    self.report({'WARNING'}, f"处理纹理 {base_texture.name} 时失败")
                    continue

            for obj in objects:
                self._write_alpha_to_vertex_colors(obj, alpha_channel, width, height)

            if create_materials and alpha_map_path:
                new_mat_name = f"{material_prefix}{original_material.name}"
                new_mat, created_new = self._create_alpha_material(new_mat_name, alpha_map_path)
                if created_new:
                    created_materials_count += 1
                for obj in objects:
                    obj.data.materials.append(new_mat)

        self.report({'INFO'}, f"完成！共提取 {len(processed_textures)} 张透明通道贴图。")
        if create_materials:
            self.report({'INFO'}, f"成功创建并追加了 {created_materials_count} 个新材质。")
        return {'FINISHED'}


tt_alpha_extract_list = (
    TT_OT_extract_alpha_channel,
)
