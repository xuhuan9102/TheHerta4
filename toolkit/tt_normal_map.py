import struct
import traceback
import zlib
from pathlib import Path

import bpy
import numpy as np

from .at_utils import apply_dithered_transparency, disable_transparent_overlap

try:
    from scipy import ndimage
    SCIPY_AVAILABLE = True
except Exception:
    ndimage = None
    SCIPY_AVAILABLE = False


COMPOSITE_PRESETS = [
    {
        "name": "标准法线贴图",
        "prefix": "NormalMap_",
        "channels": [
            {"source_type": "GENERATED_NORMAL", "source_channel": "R"},
            {"source_type": "GENERATED_NORMAL", "source_channel": "G"},
            {"source_type": "CONSTANT", "constant_value": 0.5},
            {"source_type": "CONSTANT", "constant_value": 1.0},
        ],
    },
    {
        "name": "ORM贴图 (AO/Rough/Metal)",
        "prefix": "ORMMap_",
        "channels": [
            {"source_type": "GENERATED_AO"},
            {"source_type": "GENERATED_ROUGHNESS"},
            {"source_type": "GENERATED_METALLIC"},
            {"source_type": "CONSTANT", "constant_value": 1.0},
        ],
    },
    {
        "name": "粗糙度贴图",
        "prefix": "RoughnessMap_",
        "channels": [
            {"source_type": "GENERATED_ROUGHNESS"},
            {"source_type": "CONSTANT", "constant_value": 0.5},
            {"source_type": "CONSTANT", "constant_value": 0.5},
            {"source_type": "CONSTANT", "constant_value": 1.0},
        ],
    },
    {
        "name": "通道分离 (RGBA)",
        "prefix": "Split_",
        "channels": [
            {"source_type": "IMAGE_CHANNEL", "source_channel": "R"},
            {"source_type": "IMAGE_CHANNEL", "source_channel": "G"},
            {"source_type": "IMAGE_CHANNEL", "source_channel": "B"},
            {"source_type": "IMAGE_CHANNEL", "source_channel": "A"},
        ],
    },
    {
        "name": "光照贴图",
        "prefix": "LightMap_",
        "channels": [
            {"source_type": "SOLID_COLOR"},
            {"source_type": "SOLID_COLOR"},
            {"source_type": "SOLID_COLOR"},
            {"source_type": "SOLID_COLOR"},
        ],
    },
    {
        "name": "材质贴图",
        "prefix": "MaterialMap_",
        "channels": [
            {"source_type": "SOLID_COLOR"},
            {"source_type": "SOLID_COLOR"},
            {"source_type": "SOLID_COLOR"},
            {"source_type": "SOLID_COLOR"},
        ],
    },
]


SOLID_COLOR_TEXTURE_SIZE = 256


def srgb_encode(value):
    """线性浮点 → sRGB 编码浮点，与 Blender 保存 8-bit PNG 时使用的传递函数一致"""
    v = min(max(float(value), 0.0), 1.0)
    if v <= 0.0031308:
        return v * 12.92
    return 1.055 * (v ** (1.0 / 2.4)) - 0.055


def solid_color_to_rgba_bytes(color):
    """rule.solid_color（subtype='COLOR'，线性 RNA 值）→ 写进 PNG 的 8-bit RGBA 字节"""
    rgba = list(color) + [1.0] * (4 - len(color))
    red, green, blue = (int(round(srgb_encode(rgba[i]) * 255.0)) for i in range(3))
    alpha = int(round(min(max(float(rgba[3]), 0.0), 1.0) * 255.0))
    return (red, green, blue, alpha)


def encode_rgba_png(rgba, width, height):
    """手写 8-bit RGBA PNG（每行 filter 0），绕开 Blender 色彩管理，写盘字节完全可控"""
    rgba = np.asarray(rgba, dtype=np.uint8).reshape(4)
    rows = np.zeros((height, width * 4 + 1), dtype=np.uint8)
    rows[:, 1:] = np.tile(rgba, (1, width))
    compressed = zlib.compress(rows.tobytes(), 9)

    def _chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


class ChannelProcessor:
    @staticmethod
    def load_image_pixels(image):
        if not image or not image.pixels:
            return None, 0, 0

        width, height = image.size
        pixels_np = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels_np)
        pixels = pixels_np.reshape((height, width, 4))
        return pixels, width, height

    @staticmethod
    def extract_channel(pixels, channel):
        channel_map = {"R": 0, "G": 1, "B": 2, "A": 3}

        if channel == "LUMINANCE":
            return 0.299 * pixels[:, :, 0] + 0.587 * pixels[:, :, 1] + 0.114 * pixels[:, :, 2]
        if channel == "SATURATION":
            r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            lum = np.maximum(lum, 1e-6)
            return np.sqrt(((r - lum) ** 2 + (g - lum) ** 2 + (b - lum) ** 2) / 3.0)
        if channel == "HUE_WARMTH":
            r = pixels[:, :, 0].copy()
            g = pixels[:, :, 1].copy()
            b = pixels[:, :, 2].copy()
            return (r * 2.0 - g - b) / 2.0 * 0.5 + 0.5
        if channel in channel_map:
            return pixels[:, :, channel_map[channel]].copy()
        return pixels[:, :, 0].copy()

    @staticmethod
    def extract_color_variance(pixels):
        r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
        mean_rgb = (r + g + b) / 3.0
        return ((r - mean_rgb) ** 2 + (g - mean_rgb) ** 2 + (b - mean_rgb) ** 2) / 3.0

    @staticmethod
    def _box_blur(data, radius):
        if radius <= 0:
            return data

        radius = max(1, int(round(radius)))
        padded = np.pad(data, radius, mode="edge")
        height, width = data.shape
        result = np.zeros_like(data)
        kernel_size = (radius * 2 + 1) ** 2

        for y in range(height):
            for x in range(width):
                region = padded[y:y + radius * 2 + 1, x:x + radius * 2 + 1]
                result[y, x] = np.sum(region) / kernel_size
        return result

    @staticmethod
    def blur_height_data(height_data, blur_radius):
        if blur_radius <= 0:
            return height_data
        if SCIPY_AVAILABLE:
            return ndimage.gaussian_filter(height_data, sigma=blur_radius)
        return ChannelProcessor._box_blur(height_data, blur_radius)

    @staticmethod
    def sobel_xy(height_data):
        if SCIPY_AVAILABLE:
            return ndimage.sobel(height_data, axis=1), ndimage.sobel(height_data, axis=0)

        padded = np.pad(height_data, 1, mode="edge")
        kernel_x = np.array([[-1.0, 0.0, 1.0], [-2.0, 0.0, 2.0], [-1.0, 0.0, 1.0]], dtype=np.float32)
        kernel_y = np.array([[-1.0, -2.0, -1.0], [0.0, 0.0, 0.0], [1.0, 2.0, 1.0]], dtype=np.float32)

        height, width = height_data.shape
        dx = np.zeros_like(height_data)
        dy = np.zeros_like(height_data)

        for y in range(height):
            for x in range(width):
                region = padded[y:y + 3, x:x + 3]
                dx[y, x] = np.sum(region * kernel_x)
                dy[y, x] = np.sum(region * kernel_y)
        return dx, dy

    @staticmethod
    def generate_normal_map_advanced(height_data, strength=5.0, blur_radius=1.0, invert=False):
        if invert:
            height_data = 1.0 - height_data

        height_data = ChannelProcessor.blur_height_data(height_data, blur_radius)
        dx, dy = ChannelProcessor.sobel_xy(height_data)

        z = np.ones_like(dx) / max(strength, 0.01)
        length = np.sqrt(dx ** 2 + dy ** 2 + z ** 2)
        length = np.maximum(length, 1e-6)

        normal_x = -dx / length
        normal_y = -dy / length
        normal_z = z / length

        normal_x = (normal_x + 1.0) * 0.5
        normal_y = (normal_y + 1.0) * 0.5
        return normal_x, normal_y, normal_z

    @staticmethod
    def generate_height_from_color(pixels):
        return ChannelProcessor.extract_channel(pixels, "LUMINANCE")

    @staticmethod
    def generate_roughness(pixels, method="LUMINANCE_INVERT", invert=False):
        if method == "LUMINANCE_INVERT":
            gray = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
            roughness = 1.0 - gray
        elif method == "SATURATION":
            sat = ChannelProcessor.extract_channel(pixels, "SATURATION")
            roughness = 1.0 - np.clip(sat * 3.0, 0.0, 1.0)
        elif method == "VARIANCE":
            var = ChannelProcessor.extract_color_variance(pixels)
            roughness = 1.0 - (var / (np.max(var) + 1e-6))
        elif method == "EDGE_BASED":
            gray = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
            if SCIPY_AVAILABLE:
                edges = ndimage.sobel(gray)
                edge_mag = np.abs(edges)
                edge_norm = edge_mag / (np.max(edge_mag) + 1e-6)
                roughness = gray * 0.5 + edge_norm * 0.8
            else:
                roughness = 1.0 - gray
        elif method == "COMBINED":
            gray = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
            sat = ChannelProcessor.extract_channel(pixels, "SATURATION")
            roughness = (1.0 - gray) * 0.6 + (1.0 - np.clip(sat * 2.0, 0.0, 1.0)) * 0.4
        else:
            gray = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
            roughness = 1.0 - gray

        if invert:
            roughness = 1.0 - roughness
        return np.clip(roughness, 0.02, 1.0)

    @staticmethod
    def generate_ao(pixels, radius=10, power=1.5):
        """旧的亮度对比伪 AO（保留兼容；新管线的 AO 走 generate_ao_from_geometry）"""
        luminance = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
        if SCIPY_AVAILABLE and radius > 0:
            blurred = ndimage.uniform_filter(luminance, size=radius * 2 + 1)
            ao = np.minimum(luminance / (blurred + 1e-6), 1.0)
        else:
            ao = luminance
        return np.power(np.clip(ao, 0.0, 1.0), power)

    @staticmethod
    def _shift_with_edge(data, offset_y, offset_x):
        """把数组平移 (offset_y, offset_x) 像素，空出区域用边缘值填充。

        用 numpy.pad(mode="edge") + 切片实现：偏移量可以大于图像尺寸
        （此时结果整体等于对应方向最边缘的行/列），不会再像旧的手工
        切片那样在 |offset| >= size 时拿到空切片而广播崩溃。
        """
        height, width = data.shape
        pad_y = abs(int(offset_y))
        pad_x = abs(int(offset_x))
        padded = np.pad(data, ((pad_y, pad_y), (pad_x, pad_x)), mode="edge")
        return padded[
            pad_y - int(offset_y): pad_y - int(offset_y) + height,
            pad_x - int(offset_x): pad_x - int(offset_x) + width,
        ]

    @staticmethod
    def generate_ao_from_geometry(height_data, normal_map, radius=16, height_scale=16.0, power=1.0, directions=8):
        """地平线式高度场 AO：由置换（高度）+ 法线共同推导。

        逐方向从每个像素向外步进采样，取最大仰角作为地平线角，
        余弦项给出该方向的天空可见度；法线朝向决定各方向的权重
        （表面朝向哪个方向，那个方向的遮蔽对该像素影响更大）。
        height_scale 把 0-1 高度差换算成像素单位，控制遮蔽强度。
        """
        normal_x, normal_y, _normal_z = normal_map
        ndx = normal_x * 2.0 - 1.0
        ndy = normal_y * 2.0 - 1.0

        radius = max(1, int(round(radius)))
        ao_accum = np.zeros_like(height_data)
        weight_accum = np.zeros_like(height_data)

        for k in range(directions):
            angle = 2.0 * np.pi * k / directions
            dir_x = float(np.cos(angle))
            dir_y = float(np.sin(angle))

            horizon = np.zeros_like(height_data)
            last_offset = None
            for step in range(1, radius + 1):
                offset_x = int(round(dir_x * step))
                offset_y = int(round(dir_y * step))
                if (offset_y, offset_x) == last_offset:
                    continue
                last_offset = (offset_y, offset_x)

                shifted = ChannelProcessor._shift_with_edge(height_data, offset_y, offset_x)
                distance = float(np.hypot(offset_y, offset_x))
                if distance <= 0:
                    continue
                elevation = (shifted - height_data) * height_scale / distance
                np.maximum(horizon, elevation, out=horizon)

            theta = np.arctan(horizon)
            sky_visibility = np.cos(np.clip(theta, 0.0, np.pi / 2.0))

            weight = np.maximum(ndx * dir_x + ndy * dir_y, 0.0) + 0.1
            ao_accum += sky_visibility * weight
            weight_accum += weight

        ao = ao_accum / np.maximum(weight_accum, 1e-6)
        return np.power(np.clip(ao, 0.0, 1.0), power)

    @staticmethod
    def generate_metallic(pixels, threshold=0.15, use_color_analysis=True):
        if use_color_analysis:
            saturation = ChannelProcessor.extract_channel(pixels, "SATURATION")
            variance = ChannelProcessor.extract_color_variance(pixels)
            sat_normalized = saturation / (np.max(saturation) + 1e-6)
            var_normalized = variance / (np.max(variance) + 1e-6)
            metal_score = sat_normalized * 0.5 + var_normalized * 0.5
            return (metal_score >= threshold).astype(np.float32)

        luminance = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
        return (luminance < (1.0 - threshold)).astype(np.float32)

    @staticmethod
    def generate_glossiness(pixels):
        return 1.0 - ChannelProcessor.generate_roughness(pixels, "LUMINANCE_INVERT")

    @staticmethod
    def generate_specular(pixels, base_value=0.5):
        luminance = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
        return np.clip(luminance * 0.5 + base_value * 0.5, 0.0, 1.0)

    @staticmethod
    def generate_emission(pixels, brightness_threshold=0.85):
        luminance = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
        return (luminance > brightness_threshold).astype(np.float32) * luminance

    @staticmethod
    def generate_detail(pixels, strength=2.0):
        luminance = ChannelProcessor.extract_channel(pixels, "LUMINANCE")
        if SCIPY_AVAILABLE:
            detail = ndimage.laplace(luminance) * strength
            detail = (detail - detail.min()) / (detail.max() - detail.min() + 1e-6)
            return np.clip(detail, 0.0, 1.0)

        padded = np.pad(luminance, 1, mode="edge")
        detail = np.zeros_like(luminance)
        height, width = luminance.shape
        for y in range(height):
            for x in range(width):
                center = padded[y + 1, x + 1]
                detail[y, x] = (
                    4 * center
                    - padded[y, x + 1]
                    - padded[y + 2, x + 1]
                    - padded[y + 1, x]
                    - padded[y + 1, x + 2]
                ) * strength * 0.25 + 0.5
        return np.clip(detail, 0.0, 1.0)


class RuleMapGenerator:
    """单条合成规则输入上的派生贴图工厂：按依赖链惰性生成并缓存。

    依赖链（上游先生成，下游引用上游结果）：

        输入图 ──LUMINANCE──▶ HEIGHT（置换：含反转 + 模糊降噪）
                                  │
                                  ├── Sobel + strength ──▶ NORMAL（XYZ 三元组）
                                  │
                                  ├── 颜色分析 ──▶ ROUGHNESS / METALLIC / GLOSSINESS /
                                  │                SPECULAR / EMISSION / DETAIL
                                  │
                                  └── HEIGHT + NORMAL ──▶ AO（地平线式高度场遮蔽）

    同一次规则执行中每种贴图只计算一次：例如 R/G 都要法线时，
    NORMAL 只生成一回，两个通道各自取分量。
    """

    def __init__(self, processor, pixels, width, height, rule):
        self.processor = processor
        self.pixels = pixels
        self.width = width
        self.height = height
        self.rule = rule
        self._cache = {}

    def get(self, kind):
        if kind not in self._cache:
            self._cache[kind] = self._generate(kind)
        return self._cache[kind]

    def _generate(self, kind):
        processor = self.processor
        rule = self.rule

        if kind == "HEIGHT":
            grayscale = processor.extract_channel(self.pixels, "LUMINANCE")
            if getattr(rule, "normal_invert_height", False):
                grayscale = 1.0 - grayscale
            return processor.blur_height_data(grayscale, getattr(rule, "normal_blur_radius", 1.0))

        if kind == "NORMAL":
            height = self.get("HEIGHT")
            dx, dy = processor.sobel_xy(height)
            strength = max(getattr(rule, "normal_strength", 5.0), 0.01)
            z = np.ones_like(dx) / strength
            length = np.sqrt(dx ** 2 + dy ** 2 + z ** 2)
            length = np.maximum(length, 1e-6)
            normal_x = (-dx / length + 1.0) * 0.5
            normal_y = (-dy / length + 1.0) * 0.5
            normal_z = z / length
            return normal_x, normal_y, normal_z

        if kind == "AO":
            height = self.get("HEIGHT")
            normal = self.get("NORMAL")
            return processor.generate_ao_from_geometry(
                height,
                normal,
                radius=getattr(rule, "ao_radius", 16),
                height_scale=getattr(rule, "ao_height_scale", 16.0),
                power=getattr(rule, "ao_power", 1.0),
            )

        if kind == "ROUGHNESS":
            return processor.generate_roughness(self.pixels)
        if kind == "METALLIC":
            return processor.generate_metallic(self.pixels)
        if kind == "GLOSSINESS":
            return processor.generate_glossiness(self.pixels)
        if kind == "SPECULAR":
            return processor.generate_specular(self.pixels)
        if kind == "EMISSION":
            return processor.generate_emission(self.pixels)
        if kind == "DETAIL":
            return processor.generate_detail(self.pixels)
        return None

    def get_channel(self, kind, channel):
        """从已生成的贴图中取出一个通道分量（法线按 R/G/B 取 X/Y/Z，灰度图任意通道同名返回）"""
        data = self.get(kind)
        if data is None:
            return None
        if kind == "NORMAL":
            component = {"R": data[0], "G": data[1], "B": data[2]}
            return component.get(channel, data[2])
        return data


class TT_OT_channel_composite_add_preset(bpy.types.Operator):
    bl_idname = "toolkit.tt_channel_composite_add_preset"
    bl_label = "添加预设模板"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        props = context.scene.texture_tools_props
        if self.preset_index >= len(COMPOSITE_PRESETS):
            return {"CANCELLED"}

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

        return {"FINISHED"}


class TT_OT_channel_composite_remove_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_channel_composite_remove_rule"
    bl_label = "移除合成规则"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.texture_tools_props.composite_rules.remove(self.index)
        return {"FINISHED"}


class TT_OT_execute_channel_composite(bpy.types.Operator):
    bl_idname = "toolkit.tt_execute_channel_composite"
    bl_label = "执行通道合成"
    bl_description = "根据设定规则，对选中物体材质生成通道合成贴图"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        if not props.output_dir:
            self.report({"ERROR"}, "请先设置输出目录")
            return {"CANCELLED"}

        output_dir = Path(bpy.path.abspath(props.output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)

        active_rules = [rule for rule in props.composite_rules if rule.enabled]
        if not active_rules:
            self.report({"ERROR"}, "请至少添加一条启用的合成规则")
            return {"CANCELLED"}

        selected_objects = [obj for obj in context.selected_objects if obj.type == "MESH"]
        solid_rules = [rule for rule in active_rules if self._is_solid_color_rule(rule)]
        composite_rules = [rule for rule in active_rules if not self._is_solid_color_rule(rule)]

        if not selected_objects and not solid_rules:
            self.report({"ERROR"}, "请选择至少一个网格物体")
            return {"CANCELLED"}

        processor = ChannelProcessor()
        processed_count = 0
        created_materials_count = 0
        skipped_materials = []
        failed_rules = []
        generated_paths_by_material = {}

        material_map = {}
        for obj in selected_objects:
            if not obj.material_slots:
                continue

            material = getattr(obj.material_slots[0], "material", None)
            if not material:
                continue

            material_map.setdefault(material, [])
            if obj not in material_map[material]:
                material_map[material].append(obj)

        if composite_rules and selected_objects and not material_map:
            self.report({"ERROR"}, "选中物体上没有找到有效材质")
            return {"CANCELLED"}

        # 纯色规则先出图：一次执行每档只出一张，与选中几个物体、几个材质无关
        solid_outputs = []
        for rule in solid_rules:
            try:
                result_path, hex_code = self._apply_solid_color_rule(rule=rule, output_dir=output_dir)
                if not result_path:
                    continue
                solid_outputs.append((rule, result_path, hex_code))
                processed_count += 1
            except Exception as exc:
                failed_rules.append(f"{rule.rule_name}: {exc}")
                print(f"[TT Channel Composite] Failed: {rule.rule_name}: {exc}")
                traceback.print_exc()

        # 所有选中物体共用同一个材质、同一张图 datablock
        if props.normal_map_create_materials and selected_objects:
            for rule, result_path, hex_code in solid_outputs:
                try:
                    shared_material, created_new = self._create_composite_material(
                        f"{rule.output_name_prefix}{hex_code}", result_path)
                    if created_new:
                        created_materials_count += 1
                    for obj in selected_objects:
                        if any(
                            slot.material and slot.material.name == shared_material.name
                            for slot in obj.material_slots
                        ):
                            continue
                        obj.data.materials.append(shared_material)
                except Exception as exc:
                    failed_rules.append(f"{rule.rule_name}: {exc}")
                    print(f"[TT Channel Composite] Failed: {rule.rule_name}: {exc}")
                    traceback.print_exc()

        # 没有非纯色规则时不进材质循环：纯色档与材质无关，也不该打出误导性的 skip 日志
        for material, objects in (material_map.items() if composite_rules else ()):
            base_texture = self._find_base_color_texture(material)
            if not base_texture:
                skipped_materials.append(f"{material.name}: no base color texture")
                continue

            base_pixels, width, height = processor.load_image_pixels(base_texture)
            if base_pixels is None:
                skipped_materials.append(f"{material.name}: failed to read pixels from {base_texture.name}")
                continue

            outputs_by_rule = {}
            last_pixels = base_pixels
            last_width = width
            last_height = height
            generated_paths = []

            for rule in composite_rules:
                try:
                    input_pixels = self._resolve_rule_input_pixels(
                        rule=rule,
                        outputs_by_rule=outputs_by_rule,
                        base_pixels=base_pixels,
                        width=width,
                        height=height,
                        fallback_pixels=last_pixels,
                        fallback_width=last_width,
                        fallback_height=last_height,
                    )
                    if input_pixels is None:
                        skipped_materials.append(f"{material.name}/{rule.rule_name}: missing input source")
                        continue

                    input_pixels, input_width, input_height = input_pixels
                    result_path, result_pixels = self._apply_composite_rule(
                        rule=rule,
                        material=material,
                        base_pixels=input_pixels,
                        width=input_width,
                        height=input_height,
                        processor=processor,
                        output_dir=output_dir,
                    )
                    if not result_path:
                        continue

                    outputs_by_rule[rule.rule_name] = (result_pixels, input_width, input_height)
                    last_pixels = result_pixels
                    last_width = input_width
                    last_height = input_height
                    generated_paths.append(Path(result_path))
                    processed_count += 1

                    if props.normal_map_create_materials:
                        new_mat_name = f"{rule.output_name_prefix}{material.name}"
                        new_mat, created_new = self._create_composite_material(new_mat_name, result_path)
                        if created_new:
                            created_materials_count += 1
                        for obj in objects:
                            obj.data.materials.append(new_mat)

                except Exception as exc:
                    failed_rules.append(f"{material.name}/{rule.rule_name}: {exc}")
                    print(f"[TT Channel Composite] Failed: {material.name}/{rule.rule_name}: {exc}")
                    traceback.print_exc()

            generated_paths_by_material[material.name] = generated_paths

        keep_paths = set()
        for paths in generated_paths_by_material.values():
            if not paths:
                continue
            if props.normal_map_create_materials:
                keep_paths.update(path.resolve() for path in paths)
            else:
                keep_paths.add(paths[-1].resolve())
        for paths in generated_paths_by_material.values():
            for path in paths[:-1]:
                try:
                    resolved_path = path.resolve()
                    if resolved_path in keep_paths:
                        continue
                    if path.exists():
                        path.unlink()
                except Exception as exc:
                    print(f"[TT Channel Composite] Failed to cleanup intermediate file {path}: {exc}")

        if skipped_materials:
            print("[TT Channel Composite] Skipped materials:")
            for item in skipped_materials:
                print(f"  - {item}")

        if failed_rules:
            print("[TT Channel Composite] Failed rules:")
            for item in failed_rules:
                print(f"  - {item}")
            self.report({"WARNING"}, f"有 {len(failed_rules)} 个规则执行失败，详见控制台")
        elif skipped_materials and processed_count == 0:
            self.report({"WARNING"}, f"没有生成贴图；有 {len(skipped_materials)} 个材质被跳过，详见控制台")

        self.report({"INFO"}, f"完成，共生成 {processed_count} 张合成贴图")
        if props.normal_map_create_materials:
            self.report({"INFO"}, f"成功创建并追加了 {created_materials_count} 个新材质")
        return {"FINISHED"}

    def _find_base_color_texture(self, material):
        if not material or not material.use_nodes:
            return None

        output_node = next(
            (
                node
                for node in material.node_tree.nodes
                if node.type == "OUTPUT_MATERIAL" and getattr(node, "is_active_output", False)
            ),
            None,
        )
        if not output_node:
            output_node = next((node for node in material.node_tree.nodes if node.type == "OUTPUT_MATERIAL"), None)
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

            if "Base Color" in current_node.inputs:
                base_color_input = current_node.inputs["Base Color"]
                if base_color_input.is_linked:
                    from_node = base_color_input.links[0].from_node
                    if from_node.type == "TEX_IMAGE" and from_node.image:
                        return from_node.image

            if current_node.type == "TEX_IMAGE" and current_node.image and fallback_image is None:
                fallback_image = current_node.image

            for inp in current_node.inputs:
                if inp.is_linked:
                    for link in inp.links:
                        if link.from_node not in visited_nodes:
                            nodes_to_visit.add(link.from_node)

        return fallback_image

    def _resolve_rule_input_pixels(self, rule, outputs_by_rule, base_pixels, width, height, fallback_pixels, fallback_width, fallback_height):
        mode = getattr(rule, "input_source_mode", "BASE_COLOR") or "BASE_COLOR"
        if mode == "BASE_COLOR":
            if base_pixels is None:
                return None
            return base_pixels, width, height
        if mode == "PREVIOUS_OUTPUT":
            if fallback_pixels is None:
                return None
            return fallback_pixels, fallback_width, fallback_height
        if mode == "NAMED_OUTPUT":
            ref_name = str(getattr(rule, "input_rule_name", "") or "").strip()
            if not ref_name:
                return None
            matched = outputs_by_rule.get(ref_name)
            if not matched:
                return None
            return matched
        if base_pixels is None:
            return None
        return base_pixels, width, height

    def _compose_rule_pixels(self, rule, base_pixels, width, height, processor):
        map_generator = RuleMapGenerator(processor, base_pixels, width, height, rule)
        channels_data = [None, None, None, None]

        for i, ch_config in enumerate(rule.output_channels):
            if i >= 4:
                break

            ch_data = self._resolve_channel(ch_config, base_pixels, width, height, processor, rule, map_generator)
            if ch_config.invert and ch_data is not None:
                ch_data = 1.0 - ch_data
            channels_data[i] = ch_data if ch_data is not None else np.zeros((height, width), dtype=np.float32)

        output = np.zeros((height, width, 4), dtype=np.float32)
        for i in range(4):
            output[:, :, i] = channels_data[i] if channels_data[i] is not None else 1.0
        return output

    @staticmethod
    def _is_solid_color_rule(rule):
        """规则是否走纯色分支：任一个输出通道的来源是 SOLID_COLOR 即算"""
        return any(
            getattr(ch, "source_type", "") == "SOLID_COLOR"
            for ch in getattr(rule, "output_channels", [])
        )

    def _apply_solid_color_rule(self, rule, output_dir):
        """纯色规则：一次执行只出一张 256×256 图，文件名用最终写进 PNG 的 sRGB 十六进制

        与选中物体、材质都无关；同色重跑同名同内容（每次都照写，mtime 会变），换色出新文件、旧图保留。
        返回 (路径, 六位大写 hex)。
        """
        color = tuple(float(c) for c in getattr(rule, "solid_color", (1.0, 1.0, 1.0, 1.0)))
        rgba_bytes = solid_color_to_rgba_bytes(color)
        hex_code = "".join(f"{value:02X}" for value in rgba_bytes[:3])
        size = SOLID_COLOR_TEXTURE_SIZE

        output_path = output_dir / f"{rule.output_name_prefix}{hex_code}.png"
        output_path.write_bytes(encode_rgba_png(rgba_bytes, size, size))
        return str(output_path), hex_code

    def _apply_composite_rule(self, rule, material, base_pixels, width, height, processor, output_dir):
        output = self._compose_rule_pixels(rule, base_pixels, width, height, processor)

        safe_mat_name = "".join(c for c in material.name if c.isalnum() or c in ("-", "_", ".")).rstrip()
        output_name = f"{rule.output_name_prefix}{safe_mat_name}"
        output_path = output_dir / f"{output_name}.png"

        blender_img = bpy.data.images.new(name=output_path.name, width=width, height=height, alpha=True)
        blender_img.pixels.foreach_set(output.flatten())
        blender_img.filepath_raw = str(output_path)
        blender_img.file_format = "PNG"
        blender_img.save()
        bpy.data.images.remove(blender_img)
        return str(output_path), output

    def _resolve_channel(self, ch_config, base_pixels, width, height, processor, rule, map_generator=None):
        source_type = ch_config.source_type

        if source_type == "CONSTANT":
            return np.full((height, width), ch_config.constant_value, dtype=np.float32)
        if source_type == "IMAGE_CHANNEL":
            return processor.extract_channel(base_pixels, ch_config.source_channel)
        if source_type == "GRAYSCALE":
            return processor.extract_channel(base_pixels, "LUMINANCE")
        if source_type == "INVERT":
            return 1.0 - processor.extract_channel(base_pixels, "LUMINANCE")

        # 派生贴图统一走依赖图：HEIGHT → NORMAL → AO 等，按需生成、只算一次
        if map_generator is None:
            map_generator = RuleMapGenerator(processor, base_pixels, width, height, rule)

        if source_type == "GENERATED_NORMAL":
            return map_generator.get_channel("NORMAL", ch_config.source_channel)
        if source_type == "GENERATED_HEIGHT":
            return map_generator.get("HEIGHT")
        if source_type == "GENERATED_AO":
            return map_generator.get("AO")
        if source_type == "GENERATED_ROUGHNESS":
            return map_generator.get("ROUGHNESS")
        if source_type == "GENERATED_METALLIC":
            return map_generator.get("METALLIC")
        if source_type == "GENERATED_GLOSSINESS":
            return map_generator.get("GLOSSINESS")
        if source_type == "GENERATED_SPECULAR":
            return map_generator.get("SPECULAR")
        if source_type == "GENERATED_EMISSION":
            return map_generator.get("EMISSION")
        if source_type == "GENERATED_DETAIL":
            return map_generator.get("DETAIL")
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

        tex_node = node_tree.nodes.new("ShaderNodeTexImage")
        tex_node.location = (-500, 0)
        image = bpy.data.images.load(composite_image_path, check_existing=True)
        tex_node.image = image
        image.colorspace_settings.name = "sRGB"
        image.alpha_mode = "CHANNEL_PACKED"

        transparent_bsdf = node_tree.nodes.new("ShaderNodeBsdfTransparent")
        transparent_bsdf.location = (-250, 120)

        diffuse_bsdf = node_tree.nodes.new("ShaderNodeBsdfDiffuse")
        diffuse_bsdf.location = (-250, -80)

        mix_shader = node_tree.nodes.new("ShaderNodeMixShader")
        mix_shader.location = (0, 0)

        output_node = node_tree.nodes.new("ShaderNodeOutputMaterial")
        output_node.location = (220, 0)

        apply_dithered_transparency(mat)
        disable_transparent_overlap(mat)
        node_tree.links.new(tex_node.outputs["Color"], diffuse_bsdf.inputs["Color"])
        node_tree.links.new(tex_node.outputs["Alpha"], mix_shader.inputs["Fac"])
        node_tree.links.new(transparent_bsdf.outputs["BSDF"], mix_shader.inputs[1])
        node_tree.links.new(diffuse_bsdf.outputs["BSDF"], mix_shader.inputs[2])
        node_tree.links.new(mix_shader.outputs["Shader"], output_node.inputs["Surface"])

        return mat, created_new


tt_normal_map_list = (
    TT_OT_channel_composite_add_preset,
    TT_OT_channel_composite_remove_rule,
    TT_OT_execute_channel_composite,
)
