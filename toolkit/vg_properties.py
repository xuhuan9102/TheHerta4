import bpy


class VGBackupItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="备份名称", default="New Backup")
    timestamp: bpy.props.FloatProperty(name="时间戳", default=0.0)
    data: bpy.props.StringProperty(name="权重数据", subtype='BYTE_STRING')


class VGAdjustItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="顶点组名称", default="")
    is_selected: bpy.props.BoolProperty(name="是否选中", default=False)


class VGProperties(bpy.types.PropertyGroup):
    vg_create_full_name: bpy.props.StringProperty(name="全权重组名", default="FullWeight")
    vg_create_empty_name: bpy.props.StringProperty(name="空权重组名", default="EmptyWeight")
    vg_create_delete_existing: bpy.props.BoolProperty(name="删除现有组", default=False)

    vg_cleanup_remove_zero: bpy.props.BoolProperty(name="删除零权重组", default=True)
    vg_cleanup_names: bpy.props.StringProperty(
        name="指定名称列表",
        default="Body without Gag eyes,Body without Tears,mmd_vertex_order,mmd_edge_scale"
    )

    vg_rename_old_name: bpy.props.StringProperty(name="旧名称", default="OldName")
    vg_rename_new_name: bpy.props.StringProperty(name="新名称", default="NewName")

    vg_merge_sync_bones: bpy.props.BoolProperty(name="同步合并骨骼", default=False)

    vg_adjust_selected_groups: bpy.props.CollectionProperty(type=VGAdjustItem)
    vg_adjust_selected_groups_index: bpy.props.IntProperty(name="选中顶点组索引", default=0)
    vg_adjust_available_groups: bpy.props.CollectionProperty(type=VGAdjustItem)
    vg_adjust_available_groups_index: bpy.props.IntProperty(name="可用顶点组索引", default=0)
    vg_adjust_amount: bpy.props.FloatProperty(name="调整量", default=0.1, min=-1.0, max=1.0)
    vg_adjust_mode: bpy.props.EnumProperty(
        name="调整模式",
        items=[
            ('ADD', "增加/减少", "直接增加或减少权重值"),
            ('MULTIPLY', "乘法", "按比例调整权重值"),
        ],
        default='ADD'
    )
    vg_normalize_mode: bpy.props.EnumProperty(
        name="规格化模式",
        items=[
            ('SELECTED', "仅选中", "只规格化选中的顶点组"),
            ('ALL', "全部", "规格化所有顶点组"),
        ],
        default='SELECTED'
    )


vg_properties_list = [
    VGBackupItem,
    VGAdjustItem,
    VGProperties,
]
