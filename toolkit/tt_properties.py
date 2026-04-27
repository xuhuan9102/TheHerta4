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
            ('GENERATED_AO', "环境光遮蔽", "模拟AO效果（暗部遮蔽更强）"),
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
    """合成规则：定义如何将多个通道组合成一张输出贴图"""
    
    rule_name: bpy.props.StringProperty(name="规则名称", description="此规则的名称", default="新规则")
    
    output_name_prefix: bpy.props.StringProperty(
        name="输出前缀",
        description="输出贴图的文件名前缀",
        default="Composite_"
    )
    
    output_channels: bpy.props.CollectionProperty(
        type=TT_ChannelSource,
        name="输出通道配置",
        description="R/G/B/A 四个通道的来源"
    )
    
    enabled: bpy.props.BoolProperty(name="启用", description="是否启用此规则", default=True)
    
    normal_strength: bpy.props.FloatProperty(
        name="法线强度",
        description="法线计算的强度参数",
        default=5.0,
        min=0.1,
        max=50.0
    )
    
    normal_blur_radius: bpy.props.FloatProperty(
        name="高斯模糊半径",
        description="法线计算时的高斯模糊半径",
        default=1.0,
        min=0.0,
        max=10.0
    )
    
    normal_invert_height: bpy.props.BoolProperty(
        name="反转高度",
        description="反转高度图",
        default=False
    )


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
    alpha_extract_allow_semitransparency: bpy.props.BoolProperty(name="允许半透明", description="开启时，保留灰度过渡；关闭时，所有非纯黑区域都将变为纯白（二值化）", default=False)
    alpha_extract_threshold: bpy.props.FloatProperty(name="二值化阈值", description="当'允许半透明'关闭时，低于此值的透明度将被视为完全透明，高于此值的将被视为完全不透明", default=0.1, min=0.01, max=0.5, step=0.01)
    alpha_extract_create_materials: bpy.props.BoolProperty(name="创建透明材质", description="为每个处理过的材质创建一个展示透明通道的新材质", default=True)
    alpha_extract_material_prefix: bpy.props.StringProperty(name="材质前缀", description="新创建的透明材质的名称前缀", default="FXMap_")
    texconv_path: bpy.props.StringProperty(name="texconv.exe 路径", description="指定 texconv.exe 文件的完整路径。这是进行DDS格式转换所必需的工具", subtype='FILE_PATH')
    dds_delete_originals: bpy.props.BoolProperty(name="转换后删除原图", description="在成功将图片转换为.dds格式后，删除原始的.png, .jpg等文件", default=True)
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
    
    lightmap_generate_lightmap: bpy.props.BoolProperty(
        name="生成LightMap",
        description="生成LightMap材质模板",
        default=True
    )
    
    lightmap_generate_materialmap: bpy.props.BoolProperty(
        name="生成MaterialMap", 
        description="生成MaterialMap材质模板",
        default=True
    )
    
    material_preview_pattern: bpy.props.StringProperty(name="材质名称模式", description="用于匹配材质名称的正则表达式", default=".*")
    material_preview_base_resolution: bpy.props.IntProperty(name="基础分辨率", description="基础分辨率参数（仅存储）", default=1024, min=256, max=8192)
    material_preview_active_index: bpy.props.IntProperty(name="活动索引", description="当前选中的材质预览项索引", default=0)
    
    composite_rules: bpy.props.CollectionProperty(type=TT_CompositeRule, name="通道合成规则", description="通道合成规则列表")
    composite_active_rule_index: bpy.props.IntProperty(name="活动规则索引", description="当前选中的合成规则索引", default=-1)


tt_properties_list = (
    TT_DDSConversionRule,
    TT_BakeResolutionRule,
    TT_ChannelSource,
    TT_CompositeRule,
    TT_TextureToolsProperties,
)
