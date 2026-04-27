import bpy
import numpy as np
import os
import tempfile
import traceback
from pathlib import Path

try:
    from scipy import ndimage
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

from .tt_properties import TT_ChannelSource, TT_CompositeRule


COMPOSITE_PRESETS = [
    {
        "name": "标准法线贴图",
        "prefix": "NormalMap_",
        "channels": [
            {"source_type": "GENERATED_NORMAL", "source_channel": "R"},
            {"source_type": "GENERATED_NORMAL", "source_channel": "G"},
            {"source_type": "CONSTANT", "constant_value": 0.5},
            {"source_type": "CONSTANT", "constant_value": 1.0},
        ]
    },
    {
        "name": "ORM贴图 (AO/Rough/Metal)",
        "prefix": "ORMMap_",
        "channels": [
            {"source_type": "GENERATED_AO"},
            {"source_type": "GENERATED_ROUGHNESS"},
            {"source_type": "GENERATED_METALLIC"},
            {"source_type": "CONSTANT", "constant_value": 1.0},
        ]
    },
    {
        "name": "粗糙度贴图",
        "prefix": "RoughnessMap_",
        "channels": [
            {"source_type": "GENERATED_ROUGHNESS"},
            {"source_type": "CONSTANT", "constant_value": 0.5},
            {"source_type": "CONSTANT", "constant_value": 0.5},
            {"source_type": "CONSTANT", "constant_value": 1.0},
        ]
    },
    {
        "name": "通道分离 (RGBA)",
        "prefix": "Split_",
        "channels": [
            {"source_type": "IMAGE_CHANNEL", "source_channel": "R"},
            {"source_type": "IMAGE_CHANNEL", "source_channel": "G"},
            {"source_type": "IMAGE_CHANNEL", "source_channel": "B"},
            {"source_type": "IMAGE_CHANNEL", "source_channel": "A"},
        ]
    },
]


class ChannelProcessor:
    """高级通道处理器 - 从颜色贴图生成各种属性"""
    
    @staticmethod
    def load_image_pixels(image):
        """加载图像像素数据为numpy数组"""
        if not image or not image.pixels:
            return None, 0, 0
        
        width = image.size[0]
        height = image.size[1]
        
        pixels_np = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels_np)
        pixels = pixels_np.reshape((height, width, 4))
        
        return pixels, width, height
    
    @staticmethod
    def save_image_pixels(image, pixels):
        """将numpy数组写入图像像素"""
        image.pixels.foreach_set(pixels.flatten())
    
    @staticmethod
    def extract_channel(pixels, channel):
        """从像素数据中提取指定通道"""
        channel_map = {'R': 0, 'G': 1, 'B': 2, 'A': 3}
        
        if channel == 'LUMINANCE':
            return 0.299 * pixels[:, :, 0] + 0.587 * pixels[:, :, 1] + 0.114 * pixels[:, :, 2]
        elif channel == 'SATURATION':
            r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            lum = np.maximum(lum, 1e-6)
            return np.sqrt(
                ((r - lum) ** 2 + (g - lum) ** 2 + (b - lum) ** 2) / 3
            )
        elif channel == 'HUE_WARMTH':
            r, g, b = pixels[:, :, 0].copy(), pixels[:, :, 1].copy(), pixels[:, :, 2].copy()
            return (r * 2.0 - g - b) / 2.0 * 0.5 + 0.5
        elif channel in channel_map:
            return pixels[:, :, channel_map[channel]].copy()
        else:
            return pixels[:, :, 0].copy()
    
    @staticmethod
    def extract_color_variance(pixels):
        """提取每个像素的颜色方差（用于金属度检测）"""
        r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
        mean_rgb = (r + g + b) / 3.0
        variance = ((r - mean_rgb)**2 + (g - mean_rgb)**2 + (b - mean_rgb)**2) / 3.0
        return variance
    
    @staticmethod
    def generate_normal_map_advanced(height_data, strength=5.0, blur_radius=1.0, invert=False):
        """法线贴图生成算法（与参考脚本一致）
        
        核心公式：
        - z = 1 / strength （Z分量与强度成反比）
        - 对 (dx, dy, z) 整体归一化
        - 映射到 [0, 1] 范围
        """
        if invert:
            height_data = 1.0 - height_data
        
        if blur_radius > 0 and SCIPY_AVAILABLE:
            height_data = ndimage.gaussian_filter(height_data, sigma=blur_radius)
        
        dx = ndimage.sobel(height_data, axis=1)
        dy = ndimage.sobel(height_data, axis=0)
        
        z = np.ones_like(dx) / max(strength, 0.01)
        
        length = np.sqrt(dx**2 + dy**2 + z**2)
        length = np.maximum(length, 1e-6)
        
        normal_x = -dx / length
        normal_y = -dy / length
        normal_z = z / length
        
        normal_x = (normal_x + 1) * 0.5
        normal_y = (normal_y + 1) * 0.5
        
        return normal_x, normal_y, normal_z
    
    @staticmethod
    def generate_height_from_color(pixels):
        """从颜色贴图生成高度信息（直接用灰度值，与参考脚本一致）"""
        return ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
    
    @staticmethod
    def generate_roughness(pixels, method='LUMINANCE_INVERT', invert=False):
        """从颜色贴图生成粗糙度贴图
        
        方法：
        - LUMINANCE_INVERT: 亮度反转（亮→光滑，暗→粗糙）
        - SATURATION: 高饱和度区域更光滑
        - VARIANCE: 低方差区域更光滑（绝缘体特征）
        - EDGE_BASED: 边缘区域更粗糙，平坦区域更光滑
        """
        if method == 'LUMINANCE_INVERT':
            gray = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
            roughness = 1.0 - gray
            
        elif method == 'SATURATION':
            sat = ChannelProcessor.extract_channel(pixels, 'SATURATION')
            roughness = 1.0 - np.clip(sat * 3.0, 0, 1)
            
        elif method == 'VARIANCE':
            var = ChannelProcessor.extract_color_variance(pixels)
            var_norm = var / (np.max(var) + 1e-6)
            roughness = 1.0 - var_norm
            
        elif method == 'EDGE_BASED':
            gray = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
            if SCIPY_AVAILABLE:
                edges = ndimage.sobel(gray)
                edge_mag = np.abs(edges)
                edge_norm = edge_mag / (np.max(edge_mag) + 1e-6)
                roughness = gray * 0.5 + edge_norm * 0.8
            else:
                roughness = 1.0 - gray
                
        elif method == 'COMBINED':
            gray = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
            sat = ChannelProcessor.extract_channel(pixels, 'SATURATION')
            roughness = (1.0 - gray) * 0.6 + (1.0 - np.clip(sat * 2.0, 0, 1)) * 0.4
            
        else:
            gray = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
            roughness = 1.0 - gray
        
        if invert:
            roughness = 1.0 - roughness
        
        return np.clip(roughness, 0.02, 1.0)
    
    @staticmethod
    def generate_ao(pixels, radius=10, power=1.5):
        """从颜色贴图生成环境光遮蔽(AO)效果
        
        基于亮度分布模拟AO：暗部区域遮蔽更强
        """
        luminance = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
        
        if SCIPY_AVAILABLE and radius > 0:
            blurred = ndimage.uniform_filter(luminance, size=radius * 2 + 1)
            ao = np.minimum(luminance / (blurred + 1e-6), 1.0)
        else:
            ao = luminance
        
        ao = np.power(np.clip(ao, 0, 1), power)
        
        return ao
    
    @staticmethod
    def generate_metallic(pixels, threshold=0.15, use_color_analysis=True):
        """从颜色贴图生成金属度贴图
        
        启用颜色分析时：
        - 高饱和度 + 低亮度变化 → 可能是金属
        - 低饱和度 + 统一颜色 → 可能是非金属（绝缘体）
        """
        if use_color_analysis:
            saturation = ChannelProcessor.extract_channel(pixels, 'SATURATION')
            variance = ChannelProcessor.extract_color_variance(pixels)
            
            sat_normalized = saturation / (np.max(saturation) + 1e-6)
            var_normalized = variance / (np.max(variance) + 1e-6)
            
            metal_score = sat_normalized * 0.5 + var_normalized * 0.5
            metallic = (metal_score >= threshold).astype(np.float32)
        else:
            luminance = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
            metallic = (luminance < (1.0 - threshold)).astype(np.float32)
        
        return metallic
    
    @staticmethod
    def generate_glossiness(pixels):
        """光泽度贴图（粗糙度的反义词）"""
        roughness = ChannelProcessor.generate_roughness(pixels, 'LUMINANCE_INVERT')
        return 1.0 - roughness
    
    @staticmethod
    def generate_specular(pixels, base_value=0.5):
        """高光/镜面强度"""
        luminance = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
        spec = luminance * 0.5 + base_value * 0.5
        return np.clip(spec, 0, 1)
    
    @staticmethod
    def generate_emission(pixels, brightness_threshold=0.85):
        """自发光贴图 - 检测过亮的区域"""
        luminance = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
        emission = (luminance > brightness_threshold).astype(np.float32) * luminance
        return emission
    
    @staticmethod
    def generate_detail(pixels, strength=2.0):
        """细节/凹凸贴图 - 增强高频细节"""
        luminance = ChannelProcessor.extract_channel(pixels, 'LUMINANCE')
        
        if SCIPY_AVAILABLE:
            detail = ndimage.laplace(luminance) * strength
            detail = (detail - detail.min()) / (detail.max() - detail.min() + 1e-6)
        else:
            h, w = luminance.shape
            detail = np.zeros_like(luminance)
            for i in range(1, h - 1):
                for j in range(1, w - 1):
                    detail[i, j] = (
                        4 * luminance[i, j] 
                        - luminance[i-1, j] - luminance[i+1, j]
                        - luminance[i, j-1] - luminance[i, j+1]
                    ) * strength * 0.25 + 0.5
        
        return np.clip(detail, 0, 1)


class TT_OT_channel_composite_add_preset(bpy.types.Operator):
    bl_idname = "toolkit.tt_channel_composite_add_preset"
    bl_label = "添加预设模板"
    bl_options = {'REGISTER', 'UNDO'}
    
    preset_index: bpy.props.IntProperty(default=0)
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        
        if self.preset_index < len(COMPOSITE_PRESETS):
            preset = COMPOSITE_PRESETS[self.preset_index]
            
            rule = props.composite_rules.add()
            rule.rule_name = preset["name"]
            rule.output_name_prefix = preset["prefix"]
            
            for ch_data in preset.get("channels", []):
                ch = rule.output_channels.add()
                ch.source_type = ch_data.get("source_type", "IMAGE_CHANNEL")
                ch.source_channel = ch_data.get("source_channel", "R")
                ch.constant_value = ch_data.get("constant_value", 1.0)
                ch.invert = ch_data.get("invert", False)
            
            while len(rule.output_channels) < 4:
                rule.output_channels.add()
        
        return {'FINISHED'}


class TT_OT_channel_composite_remove_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_channel_composite_remove_rule"
    bl_label = "移除合成规则"
    bl_options = {'REGISTER', 'UNDO'}
    
    index: bpy.props.IntProperty()
    
    def execute(self, context):
        context.scene.texture_tools_props.composite_rules.remove(self.index)
        return {'FINISHED'}


class TT_OT_execute_channel_composite(bpy.types.Operator):
    bl_idname = "toolkit.tt_execute_channel_composite"
    bl_label = "执行通道合成"
    bl_description = "根据设定的规则对所有选中物体的材质进行通道合成处理"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        props = context.scene.texture_tools_props
        
        if not props.output_dir:
            self.report({'ERROR'}, "请先设置输出目录")
            return {'CANCELLED'}
        
        output_dir = Path(bpy.path.abspath(props.output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)
        
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "请选择至少一个网格物体")
            return {'CANCELLED'}
        
        active_rules = [r for r in props.composite_rules if r.enabled]
        if not active_rules:
            self.report({'ERROR'}, "请至少添加一条启用的合成规则")
            return {'CANCELLED'}
        
        processor = ChannelProcessor()
        processed_count = 0
        created_materials_count = 0
        
        material_map = {}
        for obj in selected_objects:
            if obj.material_slots and obj.material_slots[0].material:
                mat = obj.material_slots[0].material
                if mat not in material_map:
                    material_map[mat] = []
                material_map[mat].append(obj)
        
        if not material_map:
            self.report({'ERROR'}, "选中的物体上没有找到有效材质")
            return {'CANCELLED'}
        
        for original_material, objects in material_map.items():
            base_texture = self._find_base_color_texture(original_material)
            if not base_texture:
                continue
            
            base_pixels, width, height = processor.load_image_pixels(base_texture)
            if base_pixels is None:
                continue
            
            for rule in active_rules:
                try:
                    result_path = self._apply_composite_rule(
                        rule, original_material, base_pixels,
                        width, height, processor, output_dir
                    )
                    
                    if result_path:
                        processed_count += 1
                        
                        if props.normal_map_create_materials:
                            new_mat_name = f"{rule.output_name_prefix}{original_material.name}"
                            new_mat, created_new = self._create_composite_material(new_mat_name, result_path)
                            if created_new:
                                created_materials_count += 1
                            for obj in objects:
                                obj.data.materials.append(new_mat)
                
                except Exception as e:
                    print(f"处理材质 '{original_material.name}' 的规则 '{rule.rule_name}' 失败: {e}")
                    traceback.print_exc()
        
        self.report({'INFO'}, f"完成！共生成 {processed_count} 张合成贴图。")
        if props.normal_map_create_materials:
            self.report({'INFO'}, f"成功创建并追加了 {created_materials_count} 个新材质。")
        return {'FINISHED'}
    
    def _find_base_color_texture(self, material):
        if not material or not material.use_nodes:
            return None
        output_node = next((n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and getattr(n, 'is_active_output', False)), None)
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
    
    def _apply_composite_rule(self, rule, material, base_pixels, width, height, processor, output_dir):
        channels_data = [None, None, None, None]
        
        for i, ch_config in enumerate(rule.output_channels):
            if i >= 4:
                break
            
            ch_data = self._resolve_channel(
                ch_config, base_pixels,
                width, height, processor, rule
            )
            
            if ch_config.invert and ch_data is not None:
                ch_data = 1.0 - ch_data
            
            channels_data[i] = ch_data if ch_data is not None else np.zeros((height, width), dtype=np.float32)
        
        output = np.zeros((height, width, 4), dtype=np.float32)
        for i in range(4):
            if channels_data[i] is not None:
                output[:, :, i] = channels_data[i]
            else:
                output[:, :, i] = 1.0
        
        safe_mat_name = "".join(c for c in material.name if c.isalnum() or c in ('-', '_', '.')).rstrip()
        output_name = f"{rule.output_name_prefix}{safe_mat_name}"
        output_filename = f"{output_name}.png"
        output_path = output_dir / output_filename
        
        blender_img = bpy.data.images.new(
            name=output_filename,
            width=width,
            height=height,
            alpha=True
        )
        blender_img.pixels.foreach_set(output.flatten())
        blender_img.filepath_raw = str(output_path)
        blender_img.file_format = 'PNG'
        blender_img.save()
        bpy.data.images.remove(blender_img)
        
        return str(output_path)
    
    def _resolve_channel(self, ch_config, base_pixels, width, height, processor, rule):
        source_type = ch_config.source_type
        
        if source_type == 'CONSTANT':
            h, w = base_pixels.shape[:2]
            return np.full((h, w), ch_config.constant_value, dtype=np.float32)
        
        elif source_type == 'IMAGE_CHANNEL':
            return processor.extract_channel(base_pixels, ch_config.source_channel)
        
        elif source_type == 'GENERATED_NORMAL':
            grayscale = processor.extract_channel(base_pixels, 'LUMINANCE')
            
            if rule.normal_invert_height:
                grayscale = 1.0 - grayscale
            
            if rule.normal_blur_radius > 0 and SCIPY_AVAILABLE:
                grayscale = ndimage.gaussian_filter(grayscale, sigma=rule.normal_blur_radius)
            
            dx = ndimage.sobel(grayscale, axis=1)
            dy = ndimage.sobel(grayscale, axis=0)
            
            z = np.ones_like(dx) / max(rule.normal_strength, 0.01)
            
            length = np.sqrt(dx**2 + dy**2 + z**2)
            length = np.maximum(length, 1e-6)
            
            nx = (-dx / length) * 0.5 + 0.5
            ny = (-dy / length) * 0.5 + 0.5
            nz = z / length
            
            channel_map = {'R': nx, 'G': ny, 'B': nz}
            return channel_map.get(ch_config.source_channel, nz)
        
        elif source_type == 'GENERATED_ROUGHNESS':
            return processor.generate_roughness(base_pixels, invert=ch_config.invert)
        
        elif source_type == 'GENERATED_AO':
            return processor.generate_ao(base_pixels)
        
        elif source_type == 'GENERATED_METALLIC':
            return processor.generate_metallic(base_pixels)
        
        elif source_type == 'GRAYSCALE':
            return processor.extract_channel(base_pixels, 'LUMINANCE')
        
        elif source_type == 'INVERT':
            gray = processor.extract_channel(base_pixels, 'LUMINANCE')
            return 1.0 - gray
        
        elif source_type == 'GENERATED_GLOSSINESS':
            return processor.generate_glossiness(base_pixels)
        
        elif source_type == 'GENERATED_SPECULAR':
            return processor.generate_specular(base_pixels)
        
        elif source_type == 'GENERATED_EMISSION':
            return processor.generate_emission(base_pixels)
        
        elif source_type == 'GENERATED_DETAIL':
            return processor.generate_detail(base_pixels)
        
        elif source_type == 'GENERATED_HEIGHT':
            return processor.generate_height_from_color(base_pixels)
        
        return None
    
    def _create_composite_material(self, material_name, composite_image_path):
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
        image = bpy.data.images.load(composite_image_path, check_existing=True)
        tex_node.image = image
        
        transparent_bsdf = node_tree.nodes.new('ShaderNodeBsdfTransparent')
        transparent_bsdf.location = (-200, 100)
        
        mix_shader = node_tree.nodes.new('ShaderNodeMixShader')
        mix_shader.location = (0, 0)
        
        output_node = node_tree.nodes.new('ShaderNodeOutputMaterial')
        output_node.location = (200, 0)
        
        node_tree.links.new(tex_node.outputs['Alpha'], mix_shader.inputs['Fac'])
        node_tree.links.new(transparent_bsdf.outputs['BSDF'], mix_shader.inputs[1])
        node_tree.links.new(tex_node.outputs['Color'], mix_shader.inputs[2])
        node_tree.links.new(mix_shader.outputs['Shader'], output_node.inputs['Surface'])
        
        return mat, created_new


tt_normal_map_list = (
    TT_OT_channel_composite_add_preset,
    TT_OT_channel_composite_remove_rule,
    TT_OT_execute_channel_composite,
)
