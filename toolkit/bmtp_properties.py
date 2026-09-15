import bpy


class BMTP_UL_ListItem(bpy.types.PropertyGroup):
    item: bpy.props.PointerProperty(name="Item", type=bpy.types.ID)


class BMTP_UL_VertexGroupItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="顶点组名称")
    selected: bpy.props.BoolProperty(name="选中", default=False)
    index: bpy.props.IntProperty(name="索引")


class BMTP_MergeSplitItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")
    marker_group_name: bpy.props.StringProperty(name="来源标记", default="")
    face_start: bpy.props.IntProperty(name="起始面", default=0, min=0)
    face_count: bpy.props.IntProperty(name="面数量", default=0, min=0)
    vertex_count: bpy.props.IntProperty(name="顶点数量", default=0, min=0)


class BMTP_Properties(bpy.types.PropertyGroup):
    vc_mode: bpy.props.EnumProperty(
        name="模式",
        items=[('FULL_COLOR', "设置完整颜色", "设置完整的RGBA颜色，会覆盖原有颜色"),
               ('ALPHA_ONLY', "仅修改透明度", "保留原有的RGB值，只修改Alpha(透明度)通道")],
        default='FULL_COLOR'
    )
    vc_color: bpy.props.FloatVectorProperty(name="颜色", subtype='COLOR_GAMMA', size=4, default=(1.0, 0.25, 0.25, 0.5),
                                            min=0.0, max=1.0)
    vc_attr_name: bpy.props.StringProperty(
        name="颜色名称",
        default="COLOR",
        description="要添加的颜色属性名称"
    )
    vc_attr_domain: bpy.props.EnumProperty(
        name="颜色域",
        items=[
            ('CORNER', "CORNER", "按循环/面角写入颜色"),
            ('POINT', "POINT", "按顶点写入颜色"),
        ],
        default='CORNER',
        description="颜色属性的域"
    )
    vc_attr_data_type: bpy.props.EnumProperty(
        name="颜色类型",
        items=[
            ('BYTE_COLOR', "BYTE_COLOR", "8位量化颜色"),
            ('FLOAT_COLOR', "FLOAT_COLOR", "32位浮点颜色"),
        ],
        default='BYTE_COLOR',
        description="颜色属性的数据类型"
    )

    uv_delete_pattern: bpy.props.StringProperty(name="匹配模式", default=r'^TEXCOORD\d*\.xy$',
                                                description="用于匹配要删除的UV贴图名称的正则表达式")
    
    uv_rename_old_pattern: bpy.props.StringProperty(name="旧名称模式", default=r'UVMap',
                                                   description="用于匹配要重命名的UV贴图名称的正则表达式")
    uv_rename_new_template: bpy.props.StringProperty(name="新名称模板", default="UVMap_{index}",
                                                     description="新UV贴图名称的模板，{index}将被替换为索引号")
    
    uv_add_count: bpy.props.IntProperty(name="添加数量", default=1, min=1, max=10,
                                        description="要添加的UV贴图数量")
    uv_add_name_template: bpy.props.StringProperty(name="名称模板", default="UVMap_{index}",
                                                   description="新UV贴图名称的模板，{index}将被替换为索引号")

    mod_delete_names: bpy.props.StringProperty(name="名称", default="Left Eye UV warp,Right Eye UV warp,Normal Smoothing,Outline Modifier",
                                               description="要删除的修改器或约束名称列表，用逗号分隔")
    
    mod_apply_names: bpy.props.StringProperty(name="名称", default="Subdivision,Mirror,Solidify",
                                             description="要应用的修改器或约束名称列表，用逗号分隔")

    wt_source_obj: bpy.props.PointerProperty(type=bpy.types.Object, name="源物体", description="作为权重来源的标准物体")
    wt_cleanup: bpy.props.BoolProperty(name="清理目标顶点组", default=True,
                                       description="在传递权重前，清空目标物体上所有现有的顶点组")
    wt_use_selected_groups: bpy.props.BoolProperty(name="只传递列表中选中的顶点组", default=False,
                                                    description="只传递列表中选中的顶点组，其他顶点组会被暂时排除")
    wt_vertex_groups: bpy.props.CollectionProperty(type=BMTP_UL_VertexGroupItem, name="顶点组列表")
    wt_vertex_groups_index: bpy.props.IntProperty(name="顶点组列表索引", default=0)
    wt_use_shapekey_positions: bpy.props.BoolProperty(name="使用形态键位置", default=False,
                                                      description="在传递权重时使用形态键位置而非基础网格位置")
    wt_use_armature_positions: bpy.props.BoolProperty(name="使用骨骼位置", default=False,
                                                       description="在传递权重时使用骨骼修改器变形后的位置（需要模型有骨骼修改器）")
                                       
    wt_merge_vertex_groups: bpy.props.CollectionProperty(type=BMTP_UL_VertexGroupItem, name="合并顶点组列表")
    wt_merge_vertex_groups_index: bpy.props.IntProperty(name="合并顶点组列表索引", default=0)
    wt_merge_source_object: bpy.props.PointerProperty(
        name="合并列表所属物体",
        type=bpy.types.Object,
        options={'HIDDEN'},
    )
    wt_merge_source_object_name: bpy.props.StringProperty(
        name="合并列表所属物体",
        default="",
        options={'HIDDEN'},
    )
    wt_merge_target_name: bpy.props.StringProperty(
        name="合并后顶点组名",
        default="",
        description="留空时使用列表中第一个选中的顶点组作为目标组"
    )

    wt_smooth_factor: bpy.props.FloatProperty(
        name="平滑系数",
        description="平滑操作的强度",
        default=0.5,
        min=0.0,
        max=1.0
    )
    wt_smooth_repeat: bpy.props.IntProperty(
        name="迭代次数",
        description="重复平滑操作的次数",
        default=5,
        min=1,
        max=100
    )
    
    wt_spread_iterations: bpy.props.IntProperty(
        name="扩散迭代次数",
        description="权重扩散操作的迭代次数",
        default=10,
        min=1,
        max=50
    )
                                       
    align_axis: bpy.props.EnumProperty(
        name="对齐轴向",
        items=[('POSITIVE_X', "+X", "将骨骼的'Y'轴对齐到其局部'+X'轴"),
               ('NEGATIVE_X', "-X", "将骨骼的'Y'轴对齐到其局部'-X'轴"),
               ('POSITIVE_Y', "+Y", "将骨骼的'Y'轴对齐到其局部'+Y'轴 (无变化)"),
               ('NEGATIVE_Y', "-Y", "将骨骼的'Y'轴对齐到其局部'-Y'轴 (反转)"),
               ('POSITIVE_Z', "+Z", "将骨骼的'Y'轴对齐到其局部'+Z'轴"),
               ('NEGATIVE_Z', "-Z", "将骨骼的'Y'轴对齐到其局部'-Z'轴")],
        default='POSITIVE_Y', description="选择骨骼统一朝向的局部轴向"
    )

    restore_map_text: bpy.props.PointerProperty(
        type=bpy.types.Text,
        name="映射表",
        description="选择用于恢复骨骼名称的.json映射表（位于文本编辑器中）"
    )

    link_source_list: bpy.props.CollectionProperty(type=BMTP_UL_ListItem, name="源项目列表")
    link_source_list_index: bpy.props.IntProperty(name="源项目列表索引", default=0)
    link_target_collection: bpy.props.PointerProperty(
        name="目标集合",
        type=bpy.types.Collection,
        description="要将源项目关联到的目标集合"
    )
        
    bridge_segments: bpy.props.IntProperty(
        name="分段数",
        description="桥接的分段数，用于控制桥接的平滑度",
        default=5,
        min=1,
        max=100
    )
    bridge_reverse: bpy.props.BoolProperty(
        name="反转方向",
        description="反转第二个循环边的方向",
        default=False
    )
    bridge_smooth: bpy.props.FloatProperty(
        name="平滑度",
        description="桥接面的平滑度",
        default=0.0,
        min=0.0,
        max=1.0
    )
    limit_surface_subdiv_levels: bpy.props.IntProperty(
        name="细分级别",
        description="临时极限表面求值时使用的细分级别",
        default=2,
        min=1,
        max=6,
    )

    # 反细分＆细分·UV防错乱已换成标准实现（toolkit/uv_guard_unsubdivide.py）：
    # 参数是算子自带属性（iterations / merge_triangles / only_selected / sel_shrink
    # 与 cuts / fix_boundary_ngons），不再占用场景属性。

    merge_split_items: bpy.props.CollectionProperty(type=BMTP_MergeSplitItem, name="合并拆分列表")
    merge_split_index: bpy.props.IntProperty(name="合并拆分索引", default=0, min=0)
    merge_split_target_name: bpy.props.StringProperty(
        name="合并物体名称",
        default="MergedObject",
        description="执行合并后目标物体使用的名称",
    )
    merge_split_weld_vertices: bpy.props.BoolProperty(
        name="合并后按距离 0.00001 合并顶点",
        default=False,
        description="开启后会焊接重合顶点，但不保证后续能完美恢复原始网格",
    )

    shapekey_cleanup_threshold: bpy.props.FloatProperty(
        name="形态键清理阈值",
        description="顶点坐标差值的最大允许值，超过该值才视为有效变形。值越大，判定越宽松，更多看似没动的形态键会被清理",
        default=1e-4,
        min=0.0,
        soft_max=1.0,
        precision=6,
    )


class BMTP_UL_CollectionLinkList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        list_item = item.item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            if isinstance(list_item, bpy.types.Collection):
                layout.label(text=list_item.name, icon='OUTLINER_COLLECTION')
            elif isinstance(list_item, bpy.types.Object):
                layout.label(text=list_item.name, icon='OUTLINER_OB_MESH')
            else:
                layout.label(text="[无效项目]", icon='ERROR')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)


class BMTP_UL_VertexGroupList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row()
            row.prop(item, "selected", text="")
            row.label(text=item.name, icon='GROUP_VERTEX')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)


class BMTP_UL_MergeSplitList(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.object_name or "[未设置]", icon='MESH_DATA')
            row.label(text=f"{max(0, int(item.vertex_count))} verts")
            row.label(text="已记录来源" if item.marker_group_name else "未记录")
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='MESH_DATA')


bmtp_properties_list = (
    BMTP_UL_ListItem,
    BMTP_UL_VertexGroupItem,
    BMTP_MergeSplitItem,
    BMTP_Properties,
    BMTP_UL_CollectionLinkList,
    BMTP_UL_VertexGroupList,
    BMTP_UL_MergeSplitList,
)
