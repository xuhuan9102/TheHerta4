import bpy


class TT_DDSConversionRule(bpy.types.PropertyGroup):
    pattern: bpy.props.StringProperty(name="正则表达式", description="用于匹配文件名的正则表达式", default="")
    format: bpy.props.StringProperty(name="DDS格式", description="对应的DDS转换格式", default="bc7_unorm")
    enabled: bpy.props.BoolProperty(name="启用", description="是否启用此规则", default=True)


class TT_BakeResolutionRule(bpy.types.PropertyGroup):
    pattern: bpy.props.StringProperty(name="正则表达式", description="用于匹配材质名称的正则表达式", default="")
    resolution: bpy.props.IntProperty(name="分辨率", description="对应的烘焙分辨率", default=2048, min=256, max=8192)
    enabled: bpy.props.BoolProperty(name="启用", description="是否启用此规则", default=True)


class TT_ChannelSource(bpy.types.PropertyGroup):
    """定义单个通道的数据来源"""

    source_type: bpy.props.EnumProperty(
        name="来源类型",
        description="选择此通道的数据来源",
        items=[
            ('IMAGE_CHANNEL', "图像通道", "从图像的指定通道获取"),
            ('GRAYSCALE', "灰度值", "RGB亮度平均值"),
            ('INVERT', "反转值", "1.0 - 亮度"),
            ('CONSTANT', "固定值", "使用固定的常量值"),
            ('GENERATED_NORMAL', "法线贴图", "从颜色图计算的法线 (R=X, G=Y, B=Z)"),
            ('GENERATED_HEIGHT', "高度图", "从颜色图推导的高度信息"),
            ('GENERATED_ROUGHNESS', "粗糙度", "从颜色图计算粗糙度（亮→光滑，暗→粗糙）"),
            ('GENERATED_GLOSSINESS', "光泽度", "粗糙度的反义词"),
            ('GENERATED_AO', "环境光遮蔽", "由置换+法线计算的地平线式 AO（凹陷处遮蔽更强）"),
            ('GENERATED_METALLIC', "金属度", "基于颜色分析检测金属区域"),
            ('GENERATED_SPECULAR', "高光强度", "镜面反射强度"),
            ('GENERATED_EMISSION', "自发光", "检测过亮的自发光区域"),
            ('GENERATED_DETAIL', "细节/凹凸", "增强高频细节纹理"),
        ],
        default='IMAGE_CHANNEL'
    )
    source_image_name: bpy.props.StringProperty(name="源图像名称", description="来源图像的名称", default="")
    source_channel: bpy.props.EnumProperty(
        name="源通道",
        description="从源图像中提取的通道",
        items=[
            ('R', "红色通道 (R)", ""),
            ('G', "绿色通道 (G)", ""),
            ('B', "蓝色通道 (B)", ""),
            ('A', "Alpha通道 (A)", ""),
            ('LUMINANCE', "亮度", ""),
        ],
        default='R'
    )
    constant_value: bpy.props.FloatProperty(
        name="常量值",
        description="当来源为固定值时使用的数值",
        default=1.0,
        min=0.0,
        max=1.0
    )
    invert: bpy.props.BoolProperty(
        name="反转",
        description="反转此通道的值",
        default=False
    )


class TT_CompositeRule(bpy.types.PropertyGroup):
    """通道合成规则：定义如何将多个通道组合成一张输出贴图"""

    rule_name: bpy.props.StringProperty(name="规则名称", description="用于标识当前通道合成规则的名称", default="新建规则")
    input_source_mode: bpy.props.EnumProperty(
        name="输入来源",
        description="选择当前规则使用基础贴图、上一条输出还是指定规则输出作为输入",
        items=[
            ('BASE_COLOR', "基础贴图", "使用当前材质解析到的基础颜色贴图作为输入"),
            ('PREVIOUS_OUTPUT', "上一条输出", "使用上一条成功执行的规则输出作为输入"),
            ('NAMED_OUTPUT', "指定规则输出", "使用指定规则名称对应的输出结果作为输入"),
        ],
        default='BASE_COLOR',
    )
    input_rule_name: bpy.props.StringProperty(
        name="引用规则",
        description="当输入来源为指定规则输出时，填写要引用的规则名称",
        default="",
    )
    output_name_prefix: bpy.props.StringProperty(
        name="输出前缀",
        description="生成文件时附加在材质名之前的输出前缀",
        default="Composite_"
    )
    output_channels: bpy.props.CollectionProperty(
        type=TT_ChannelSource,
        name="输出通道配置",
        description="R/G/B/A 四个通道的来源配置"
    )
    enabled: bpy.props.BoolProperty(name="启用", description="控制当前规则是否参与通道合成执行", default=True)
    normal_strength: bpy.props.FloatProperty(
        name="法线强度",
        description="使用高度图生成法线时的强度参数",
        default=5.0,
        min=0.1,
        max=50.0
    )
    normal_blur_radius: bpy.props.FloatProperty(
        name="法线模糊",
        description="法线生成前对高度信息进行模糊，减轻噪点",
        default=1.0,
        min=0.0,
        max=10.0
    )
    normal_invert_height: bpy.props.BoolProperty(
        name="反转高度",
        description="生成法线时反转高度方向",
        default=False
    )
    ao_radius: bpy.props.IntProperty(
        name="AO 采样半径",
        description="地平线式 AO 向外步进采样的最大距离（像素）",
        default=16,
        min=1,
        max=128
    )
    ao_height_scale: bpy.props.FloatProperty(
        name="AO 高度比例",
        description="把 0-1 高度差换算成像素单位计算遮蔽仰角，值越大遮蔽越强",
        default=16.0,
        min=0.1,
        max=128.0
    )
    ao_power: bpy.props.FloatProperty(
        name="AO 对比度",
        description="对 AO 结果施加的幂次，值越大遮蔽区域越暗",
        default=1.0,
        min=0.1,
        max=4.0
    )


class TT_AtlasMaterialItem(bpy.types.PropertyGroup):
    material: bpy.props.PointerProperty(name="图集材质", type=bpy.types.Material)
    enabled: bpy.props.BoolProperty(name="启用", default=True)
    source_objects: bpy.props.StringProperty(name="来源物体", default="")
    skip_reason: bpy.props.StringProperty(name="跳过原因", default="")


class TT_TextureToolsProperties(bpy.types.PropertyGroup):
    output_dir: bpy.props.StringProperty(name="输出目录", description="所有生成贴图的统一输出文件夹", subtype='DIR_PATH')
    normal_map_strength: bpy.props.FloatProperty(name="强度", description="法线贴图的效果强度。值越大，凹凸感越强", default=5.0, min=0.1, max=50.0)
    normal_map_blur_radius: bpy.props.FloatProperty(name="高斯模糊", description="对原始灰度图进行高斯模糊以减少噪点。值为0则不模糊", default=1.0, min=0.0, max=10.0)
    normal_map_blue_channel_value: bpy.props.FloatProperty(name="蓝通道(Z)强度", description="直接设置法线贴图中蓝色通道的固定值 (0-1)。用于特殊渲染目的", default=0.5, min=0.0, max=1.0)
    normal_map_invert: bpy.props.BoolProperty(name="反转高度", description="反转灰度图的黑白，实现凹凸反转", default=False)
    normal_map_create_materials: bpy.props.BoolProperty(name="创建法线材质", description="为每个处理过的材质创建一个带法线贴图的新材质", default=True)
    normal_map_material_prefix: bpy.props.StringProperty(name="材质前缀", description="新创建的法线材质的名称前缀", default="NormalMap_")
    color_bake_preview_type: bpy.props.EnumProperty(name="预览类型", description="渲染预览时使用的基本体形状", items=[('FLAT', "平面", ""), ('SPHERE', "球体", ""), ('CUBE', "立方体", ""), ('MONKEY', "猴头", "")], default='FLAT')
    color_bake_size: bpy.props.IntProperty(name="贴图尺寸", description="最终渲染出的颜色贴图的分辨率", default=2048, min=256, max=8192)
    color_bake_unfold_by_uv: bpy.props.BoolProperty(name="按UV展开顶点", description="将物体的顶点按照UV坐标位置展开到3D空间，然后进行烘焙", default=True)
    color_bake_import_to_material: bpy.props.BoolProperty(name="导入到材质", description="将烘焙好的颜色贴图旁路掉原来的复杂节点网络", default=True)
    color_bake_node_types: bpy.props.EnumProperty(name="烘焙节点类型", description="选择需要被烘焙的材质的特征", items=[('ALL', "所有节点", ""), ('MIX_SHADER', "混合着色器", ""), ('MIX_COLOR', "混合颜色", ""), ('COMPLEX', "复杂节点", "")], default='COMPLEX')
    material_to_assign: bpy.props.PointerProperty(name="指定材质", description="选择一个材质，用于批量赋予给选中的所有物体", type=bpy.types.Material)
    mat_rename_search: bpy.props.StringProperty(
        name="查找片段",
        default="",
        description="材质球名称中要查找并替换的片段，留空则不执行",
    )
    mat_rename_replace: bpy.props.StringProperty(
        name="替换为",
        default="",
        description="用于替换查找片段的字符串，留空表示删除该片段",
    )
    alpha_extract_allow_semitransparency: bpy.props.BoolProperty(name="允许半透明", description="开启时，保留灰度过渡；关闭时，所有非纯黑区域都将变为纯白（二值化）", default=False)
    alpha_extract_threshold: bpy.props.FloatProperty(name="二值化阈值", description="当'允许半透明'关闭时，低于此值的透明度将被视为完全透明，高于此值的将被视为完全不透明", default=0.1, min=0.01, max=0.5, step=0.01)
    alpha_extract_create_materials: bpy.props.BoolProperty(name="创建透明材质", description="为每个处理过的材质创建一个展示透明通道的新材质", default=True)
    texconv_path: bpy.props.StringProperty(name="texconv.exe 路径", description="指定 texconv.exe 文件的完整路径。这是进行DDS格式转换所必需的工具", subtype='FILE_PATH')
    dds_delete_originals: bpy.props.BoolProperty(name="转换后删除原图", description="在成功将图片转换为.dds格式后，删除原始的.png, .jpg等文件", default=True)
    dds_reencode_existing_dds: bpy.props.BoolProperty(name="处理现有DDS", description="现有的 .dds 文件也会按目标格式重新编码，可用于更换DDS格式", default=False)
    dds_auto_convert_png_after_import: bpy.props.BoolProperty(name="导入后自动转为PNG", description="勾选后，导入工作空间内容结束时把工程里指向 .dds 的图片改到同目录同名 .png，避免 Blender 无法解码 BC7/BC6H 压缩 DDS 时反复刷警告。\n同名 .png 已存在：只把引用换过去，不重复转换（重复导入不会卡好一会儿）。\n不存在：才用 texconv 生成一次。\n有问题的 png 请自己删除，删除后下次导入会重新生成那一张。\n原 .dds 文件一律保留不动", default=False)
    dds_use_custom_rules: bpy.props.BoolProperty(name="使用自定义规则", description="启用自定义DDS转换规则，覆盖默认规则", default=False)
    dds_rules_file_path: bpy.props.StringProperty(name="规则配置文件", description="DDS转换规则的配置文件路径", subtype='FILE_PATH')
    dds_show_advanced: bpy.props.BoolProperty(name="显示高级选项", description="显示DDS转换的高级选项", default=False)
    dds_rules: bpy.props.CollectionProperty(type=TT_DDSConversionRule, name="DDS转换规则", description="DDS转换规则列表")
    bake_resolution_use_rules: bpy.props.BoolProperty(name="使用分辨率规则", description="启用材质名称匹配分辨率规则，覆盖默认设置", default=False)
    bake_resolution_show_advanced: bpy.props.BoolProperty(name="显示高级选项", description="显示烘焙分辨率规则的高级选项", default=False)
    bake_resolution_rules: bpy.props.CollectionProperty(type=TT_BakeResolutionRule, name="烘焙分辨率规则", description="烘焙分辨率规则列表")
    lightmap_mode: bpy.props.EnumProperty(
        name="模式",
        description="光照模板生成模式",
        items=[
            ('APPEND', "追加", "在现有材质槽后添加光照材质"),
            ('REPLACE', "替换", "替换现有材质为光照材质")
        ],
        default='APPEND'
    )
    lightmap_generate_lightmap: bpy.props.BoolProperty(name="生成LightMap", description="生成LightMap材质模板", default=False)
    lightmap_generate_highlightmap: bpy.props.BoolProperty(name="生成HighLightMap", description="生成HighLightMap材质模板", default=False)
    lightmap_generate_rampmap: bpy.props.BoolProperty(name="生成RampMap", description="生成RampMap材质模板", default=False)
    lightmap_generate_materialmap: bpy.props.BoolProperty(name="生成MaterialMap", description="生成MaterialMap材质模板", default=False)
    lightmap_generate_stockingmap: bpy.props.BoolProperty(name="生成WengineFX", description="生成WengineFX材质模板", default=False)
    lightmap_custom_name: bpy.props.StringProperty(
        name="名称",
        description="自定义材质名称。前缀为上面勾选的生成类型，此名称接在前缀之后，例如 LightMap_名称；留空则使用活动物体名",
        default=""
    )
    material_preview_pattern: bpy.props.StringProperty(name="材质名称模式", description="用于匹配材质名称的正则表达式", default=".*")
    material_preview_base_resolution: bpy.props.IntProperty(name="基础分辨率", description="基础分辨率参数（仅存储）", default=1024, min=256, max=8192)
    material_preview_active_index: bpy.props.IntProperty(name="活动索引", description="当前选中的材质预览项索引", default=0)
    composite_rules: bpy.props.CollectionProperty(type=TT_CompositeRule, name="通道合成规则", description="通道合成规则列表")
    composite_active_rule_index: bpy.props.IntProperty(name="活动规则索引", description="当前选中的合成规则索引", default=-1)
    atlas_output_name: bpy.props.StringProperty(name="图集输出名称", description="保存图集时使用的基础文件名", default="TextureAtlas")
    atlas_padding: bpy.props.IntProperty(name="图集边距", description="图集中各贴图块之间的像素间距", default=8, min=0, max=128)
    atlas_color_size: bpy.props.IntProperty(name="纯色兜底尺寸", description="纯色材质在图集中使用的基础尺寸", default=32, min=4, max=1024)
    atlas_include_extra_textures: bpy.props.BoolProperty(name="包含附加贴图", description="同时尝试合并 Metallic/Roughness/Normal/Emission 贴图", default=True)
    atlas_packer_type: bpy.props.EnumProperty(
        name="打包策略",
        description="控制图集块的打包方式",
        items=[
            ('RECTPACK', "紧凑打包", "使用紧凑矩形打包"),
            ('GRID', "网格打包", "按统一网格顺序打包"),
        ],
        default='RECTPACK',
    )
    atlas_force_uniform_size: bpy.props.BoolProperty(
        name="统一尺寸",
        description="将所有贴图块扩展到相同尺寸后再打包",
        default=False,
    )
    atlas_crop_transparent: bpy.props.BoolProperty(
        name="裁剪透明边",
        description="在打包前裁剪贴图四周的透明区域",
        default=False,
    )
    atlas_pixel_art_scale: bpy.props.BoolProperty(
        name="像素风缩放",
        description="缩放图集时使用最近邻，保留像素风格",
        default=False,
    )
    atlas_image_format: bpy.props.EnumProperty(
        name="图集输出格式",
        description="图集贴图的输出格式",
        items=[
            ('PNG', "PNG", "保存为 PNG"),
            ('TGA', "TGA", "保存为 TGA"),
            ('TIFF', "TIFF", "保存为 TIFF"),
            ('BMP', "BMP", "保存为 BMP"),
        ],
        default='PNG',
    )
    atlas_size_mode: bpy.props.EnumProperty(
        name="图集尺寸模式",
        description="控制图集输出尺寸的调整方式",
        items=[
            ('AUTO', "自动", "使用打包后的原始尺寸"),
            ('PO2', "二次幂", "将宽高扩展到 2 的幂"),
            ('QUAD', "正方形", "强制输出为正方形图集"),
            ('CUSTOM', "自定义", "使用固定的自定义尺寸"),
        ],
        default='PO2',
    )
    atlas_custom_width: bpy.props.IntProperty(name="自定义宽度", description="自定义图集输出宽度", default=2048, min=1, max=16384)
    atlas_custom_height: bpy.props.IntProperty(name="自定义高度", description="自定义图集输出高度", default=2048, min=1, max=16384)
    atlas_max_size: bpy.props.IntProperty(name="最大图集尺寸", description="允许生成的最大图集边长", default=16384, min=512, max=32768)
    atlas_materials: bpy.props.CollectionProperty(type=TT_AtlasMaterialItem, name="图集材质列表", description="当前可用于生成图集的材质列表")
    atlas_material_index: bpy.props.IntProperty(name="图集材质索引", default=0)


tt_properties_list = (
    TT_DDSConversionRule,
    TT_BakeResolutionRule,
    TT_ChannelSource,
    TT_CompositeRule,
    TT_AtlasMaterialItem,
    TT_TextureToolsProperties,
)
